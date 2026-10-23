"""交付复核：文档里的每条"证据"到底存不存在、验的是不是当前这份代码。

为什么需要机械核对：验收矩阵里 17 条 AT 的证据是几轮里陆续写进去的，
路径会随重构改名、测试会随代码增长。**声称的证据不存在**比没有证据更糟——
它看起来像已经验过了，实际没人再去看。
"""

import io
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根（脚本在 scripts/ 下）
DOCS = ["docs/ACCEPTANCE.md", "docs/STATUS.md", "README.md"]

# 只在仓库里找这些形态的引用
PATTERNS = [
    re.compile(r"`((?:tests|asistent|scripts|docs|contracts)/[^`]+?)`"),
    re.compile(r"\]\(([^)]+)\)"),
]


def read(path):
    with io.open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return handle.read()


def main():
    missing = []
    checked = 0
    for doc in DOCS:
        text = read(doc)
        doc_dir = os.path.dirname(os.path.join(REPO, doc))
        for pattern in PATTERNS:
            for match in pattern.finditer(text):
                ref = match.group(1).strip()
                if ref.startswith(("http://", "https://", "#")):
                    continue
                ref = ref.split("#")[0].strip()
                # 通配与"成员引用"不是文件路径，跳过（否则审计会一直误报，就没人看了）
                if any(ch in ref for ch in "*?") or "::" in ref:
                    continue
                checked += 1
                # 文档里的相对链接是相对**文档所在目录**，不是仓库根
                if not (os.path.exists(os.path.join(REPO, ref))
                        or os.path.exists(os.path.join(doc_dir, ref))):
                    missing.append((doc, ref))

    print("=" * 66)
    print("A) 文档引用的路径是否存在")
    print("=" * 66)
    print("核对了 %d 处引用" % checked)
    if missing:
        for doc, ref in missing:
            print("  [缺失] %s -> %s" % (doc, ref))
    else:
        print("  [OK] 全部存在")

    print()
    print("=" * 66)
    print("B) 验收矩阵的状态分布")
    print("=" * 66)
    acc = read("docs/ACCEPTANCE.md")
    rows = [line for line in acc.splitlines() if re.match(r"^\| \*\*AT-\d+\*\*", line)]
    marks = {}
    for row in rows:
        cells = row.split("|")
        status = cells[3].strip() if len(cells) > 3 else "?"
        marks[status] = marks.get(status, 0) + 1
    print("  AT 条目数：%d" % len(rows))
    for status, count in sorted(marks.items(), key=lambda kv: -kv[1]):
        print("    %-14s %d" % (status.replace("**", ""), count))
    blocked = [line.split("|")[1].strip() for line in rows
               if ("🟡" in line or "🔴" in line or "⏳" in line)]
    print("  尚未写「通过」的：%s" % ("、".join(blocked) if blocked else "（无）"))

    print()
    print("=" * 66)
    print("C) 契约冻结状态")
    print("=" * 66)
    version_py = read("asistent/contracts/version.py")
    found = re.search(r'CONTRACT_VERSION\s*=\s*"([^"]+)"', version_py)
    print("  version.py 里的版本：%s" % (found.group(1) if found else "??"))
    tags = subprocess.run(["git", "tag", "--list", "contracts-v*"], cwd=REPO,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    names = sorted(tags.stdout.decode().split())
    print("  冻结标签（%d 个）：%s" % (len(names), " ".join(names)))
    snapshot = subprocess.run([sys.executable, "-X", "utf8", "scripts/gen_contract_snapshot.py",
                               "--check"], cwd=REPO, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
    text = snapshot.stdout.decode("utf-8", "replace").strip()
    print("  快照自检（退出码 %d）：%s" % (snapshot.returncode, text.splitlines()[-1] if text else ""))

    print()
    print("=" * 66)
    print("D) 部署仓库状态")
    print("=" * 66)
    head = subprocess.run(["git", "log", "-1", "--format=%H %cI %s"], cwd=REPO,
                          stdout=subprocess.PIPE)
    print("  本地 HEAD：%s" % head.stdout.decode("utf-8", "replace").strip())
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, stdout=subprocess.PIPE)
    print("  工作区脏文件数：%d" % len(dirty.stdout.decode().strip().splitlines()))

    print()
    print("AUDIT-DONE")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
