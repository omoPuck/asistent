"""文件分类与文本抽取（纯函数，无副作用，可单独测试）。

分类策略是**明确的**，不是"猜"：
- 空文件 → `EMPTY`（无内容可索引，跳过但记录）；
- 二进制探测（NUL 字节 / 解码失败）→ `BINARY`（不索引，记录原因）；
- 超过 `max_bytes` → `TOO_LARGE`（不索引，记录原因 + 截断原因）；
- 否则 → `TEXT`，按候选编码（UTF-8 → UTF-8-SIG → GB18030）解码。

抽出的"待索引文本"是**过滤后的干净文本**，用于喂给
`asistent.contracts.search.normalize_for_index()`；原始字节本身在采集阶段已另存快照，
本模块不负责保存原文。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import List, Optional, Tuple

#: 解码候选顺序：先 UTF-8（BOM 友好），再中文常见遗留编码 GB18030（兼容 GBK/GB2312）
TEXT_CODECS: Tuple[str, ...] = ("utf-8-sig", "utf-8", "gb18030")

#: 探测二进制时最多读取的字节数
SNIFF_BYTES = 8192


class FileKind(str, enum.Enum):
    """文件内容分类结果。"""

    TEXT = "TEXT"
    EMPTY = "EMPTY"
    BINARY = "BINARY"
    TOO_LARGE = "TOO_LARGE"
    UNREADABLE = "UNREADABLE"


@dataclass(frozen=True)
class DecodedText:
    """解码结果：文本 + 实际使用的编码 + 是否发生替换字符丢失。"""

    text: str
    codec: str
    lossy: bool = False


def looks_binary(sample: bytes) -> bool:
    """二进制探测：出现 NUL 字节即视为二进制（与 git 的启发式一致且无歧义）。"""
    return b"\x00" in sample


def classify_bytes(data: bytes, *, max_bytes: int) -> FileKind:
    """按探测结果给字节流分类（不修改任何东西）。"""
    if not data:
        return FileKind.EMPTY
    if len(data) > max_bytes:
        return FileKind.TOO_LARGE
    if looks_binary(data[:SNIFF_BYTES]):
        return FileKind.BINARY
    return FileKind.TEXT


def decode_text(data: bytes, *, codecs: Tuple[str, ...] = TEXT_CODECS) -> Optional[DecodedText]:
    """尝试候选编码解码；全部失败返回 `None`（调用方按二进制处理并记录原因）。

    用 `errors="strict"`：宁可判为二进制，也不静默产生 U+FFFD 造成"看起来成功其实内容错"。
    """
    for codec in codecs:
        try:
            return DecodedText(text=data.decode(codec), codec=codec, lossy=False)
        except UnicodeDecodeError:
            continue
    return None


def extract_index_text(text: str, *, max_chars: int) -> Tuple[str, bool]:
    """从解码后的文本里取出待索引文本。

    返回 `(text, truncated)`。截断只发生在极端长文（默认 2,000,000 字符）上，
    且返回值会明确告诉调用方"被截断了"，由调用方写入跳过原因/备注，绝不静默。
    """
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def unsupported_reason(kind: FileKind, *, size_bytes: int, max_bytes: int) -> str:
    """给跳过动作生成人类可读原因（写入审计与事件，便于用户在手机端看到）。"""
    if kind is FileKind.BINARY:
        return f"binary_content_detected: NUL byte in first {SNIFF_BYTES} bytes"
    if kind is FileKind.TOO_LARGE:
        return f"file_too_large: size_bytes={size_bytes} > max_bytes={max_bytes}"
    if kind is FileKind.EMPTY:
        return "empty_file: size_bytes=0, no indexable text"
    if kind is FileKind.UNREADABLE:
        return "unreadable: 打开文件被操作系统拒绝（权限/占用/损坏）"
    return "unknown"


__all__ = [
    "TEXT_CODECS",
    "SNIFF_BYTES",
    "FileKind",
    "DecodedText",
    "looks_binary",
    "classify_bytes",
    "decode_text",
    "extract_index_text",
    "unsupported_reason",
]
