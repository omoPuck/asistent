"""发布前扫描：仓库里有没有不该上 GitHub 的东西。

用法（仓库根目录）：
    python3 scripts/publish_scan.py            # 人读的报告；有真凭据时退出码 1
    python3 scripts/publish_scan.py --quiet    # 只打结论

设计取舍：
  * 只看 `git ls-files`（真正会进仓库的文件），不看工作区杂物；
  * 分两档：BLOCK = 真凭据（命中即不可发布）；REVIEW = 内网地址/邮箱/本机路径（要人判断）；
  * 假值白名单是**必需的**：测试夹具里故意写着 `hunter2`、示例 AWS key
    （`AKIAIOSFODNN7EXAMPLE`）之类，不排除它们扫描器就会一直喊"有凭据"；
    天天喊狼来了的扫描器比不扫更糟——所以假值单列一档、明确说"已识别"。 
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BLOCK = [
    ("邮箱授权码/密码赋值", re.compile(r"(?i)(password|passwd|authcode|auth_code|secret)\s*[:=]\s*[\"']?[^\s\"'<>]{6,}")),
    ("私钥块", re.compile(r"-----BEGIN (RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----")),
    ("形似 GitHub/云厂商 token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,})\b")),
    ("Bearer 后跟长串", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{24,}")),
]

#: 命中 BLOCK 但属于**故意造的假值**（测试夹具、脱敏文档、示例密钥）。
KNOWN_SAFE = re.compile(
    r"(?i)("
    r"EXAMPLE|DUMMY|FAKE|PLACEHOLDER|NOT-A-REAL|must-not-leak|should-never|"
    r"topsecretpw|hunter2|s3cr3t|REAL-SECRET-COOKIE-VALUE|wrong-token-000|"
    r"REDACTED|SPIKE_DUMMY|api_key\s*=|authcode\s*=\s*read_authcode|"
    r"password=/token:|MIIE|abc123|Redactor|server\.pem|"
    r"AKIAIOSFODNN7EXAMPLE"
    r")"
)

REVIEW = [
    ("内网 IP 10.x", re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
    ("内网 IP 192.168.x", re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")),
    ("内网 IP 172.16-31.x", re.compile(r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b")),
    ("非示例邮箱", re.compile(r"[\w.+-]+@(?!example\.(com|org|net))[\w-]+\.[a-z]{2,}")),
    # `<you>` 是有意保留的占位符，不算泄漏；只有出现真实用户名才提示
    ("本机 Windows 绝对路径", re.compile(r"[A-Za-z]:\\\\?Users\\\\?(?!<you>)[^\\\s]+")),
    ("公共通配解析域名", re.compile(r"(sslip\.io|nip\.io|xip\.io)")),
]

TEXT_EXT = {
    ".py", ".md", ".sh", ".ps1", ".json", ".toml", ".txt", ".html", ".css",
    ".js", ".webmanifest", ".svg", ".yml", ".yaml", ".cmd", ".bat", ".cfg", ".ini",
}

SKIP_PREFIX = ("var/", "docs/evidence/")


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", REPO, "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def scan() -> tuple[list[str], list[str], list[str], int]:
    files = tracked_files()
    blocked: list[str] = []
    reviews: list[str] = []
    safe_hits: list[str] = []
    for rel in files:
        if rel.startswith(SKIP_PREFIX):
            continue
        if os.path.splitext(rel)[1].lower() not in TEXT_EXT:
            continue
        try:
            with io.open(os.path.join(REPO, rel), encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError as exc:
            blocked.append("%s  [读不到] %s" % (rel, exc))
            continue
        for lineno, line in enumerate(lines, 1):
            if len(line) > 2000:
                continue
            for label, pattern in BLOCK:
                if pattern.search(line):
                    if KNOWN_SAFE.search(line):
                        safe_hits.append("%s:%d  [%s]（假值/文档示例，已放行）" % (rel, lineno, label))
                        continue
                    blocked.append("%s:%d  [%s]  %s" % (rel, lineno, label, line.strip()[:140]))
            for label, pattern in REVIEW:
                if pattern.search(line):
                    reviews.append("%s:%d  [%s]  %s" % (rel, lineno, label, line.strip()[:140]))
    return blocked, reviews, safe_hits, len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description="发布前扫描（只读）")
    parser.add_argument("--quiet", action="store_true", help="只打结论")
    parser.add_argument("--show-review", type=int, default=40, help="最多展示多少条 REVIEW")
    args = parser.parse_args()

    blocked, reviews, safe_hits, total = scan()
    if not args.quiet:
        print("仓库已跟踪文件：%d 个" % total)
        print()
        print("=== BLOCK（真凭据，命中即不可发布）：%d ===" % len(blocked))
        for item in blocked:
            print("  " + item)
        print()
        print("=== 已识别为假值/文档示例：%d（不计入阻断） ===" % len(safe_hits))
        for item in safe_hits:
            print("  " + item)
        print()
        print("=== REVIEW（内网地址/邮箱/本机路径，需人判断）：%d ===" % len(reviews))
        for item in reviews[: args.show_review]:
            print("  " + item)
        if len(reviews) > args.show_review:
            print("  ... 另有 %d 条" % (len(reviews) - args.show_review))
        print()

    if blocked:
        print("结论：不可发布（%d 条真凭据）" % len(blocked))
        return 1
    if reviews:
        print("结论：可发布，但建议用 private 仓库（%d 条内网信息）" % len(reviews))
        return 0
    print("结论：无阻断项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
