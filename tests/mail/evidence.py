"""证据输出工具：让 `[evidence]` 行在任何控制台代码页下都不炸。

背景：开发机默认控制台是 cp936(gbk)，测试里的 `print("[evidence] ...")` 一旦遇到
gbk 编不出来的字符（例如编码损坏邮件里的 `\\u0123`）就会抛 `UnicodeEncodeError`，
把"产品断言失败"和"证据打印失败"混在一起。

这里的做法是**保留控制台编码**、只把错误策略改成 `replace`（编不出的字符退化成 `?`），
因此：
- 中文照常可读；
- 极端字符不会中断测试；
- 证据文件/终端仍然是当前平台编码，不强行改 stdout 编码（避免把终端刷成乱码）。
"""

from __future__ import annotations

import sys
from typing import Any


def make_evidence_safe() -> bool:
    """把当前 stdout 的错误策略改成不抛异常。返回是否成功（失败也不影响测试）。"""
    stream = sys.stdout
    try:
        stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        return True
    except Exception:  # pragma: no cover - 非 TextIOWrapper（如被重定向到自定义流）
        return False


def evidence(*parts: Any) -> str:
    """打印一行证据并返回该行文本（返回值便于断言/二次记录）。"""
    text = " ".join(str(part) for part in parts)
    line = text if text.startswith("[evidence]") else "[evidence] " + text
    try:
        print(line)
    except UnicodeEncodeError:  # pragma: no cover - make_evidence_safe 之后不该发生
        print(line.encode(sys.stdout.encoding or "ascii", "replace").decode(
            sys.stdout.encoding or "ascii", "replace"))
    return line


EVIDENCE_STDOUT_SAFE = make_evidence_safe()
