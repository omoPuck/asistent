"""中文/混合语料检索契约（SPEC §6.2、§21.1、MVP-1 验收）。

## 为什么需要这一层

SQLite FTS5 的 `unicode61` 分词器把**连续中文当成一个 token**：
建索引时 `"部署方案"` 是一个 token，用 `MATCH '部署'` 查**返回 0 行且不报错**——
检索会静默失效（设备实测，见 docs/env/report.md §B5）。

## 契约规定

1. 索引入库前，文本必须经过 `normalize_for_index()`：NFKC 归一、ASCII 小写、**CJK 逐字切分**、
   空白折叠；
2. 查询必须经过 `to_fts_query()`：同样的切分规则，且**每个 token 都加引号**，
   CJK 连续串转成 FTS5 短语查询（`"部 署"`），从而支持任意长度的中文子串匹配；
3. 查询串**绝不能**原样拼进 `MATCH`：邮件正文和资料是不可信输入（§13.5），
   未加引号的 FTS5 语法会被用户内容破坏甚至变成注入；
4. `to_fts_query()` 输入的纯符号/空串返回 `None`，调用方必须当作"无效查询"处理，
   不允许退化成"返回全部结果"。

索引是**派生数据**：切分规则变化后必须重建索引，并保留"待重建"状态（§6.2）。
"""

from __future__ import annotations

import unicodedata
from typing import List, Optional, Sequence

#: 需要逐字切分的表意文字区间（CJK 统一表意、扩展 A、兼容表意、假名、谚文音节）
_CJK_RANGES: Sequence[tuple] = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7A3),
)


def is_cjk(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def split_cjk(text: str, *, separator: str = " ") -> str:
    """把 CJK 字符逐个切开，并在 CJK↔非 CJK 边界插入分隔符。

    `部署方案` → `部 署 方 案`；`deploy部署` → `deploy 部 署`。

    边界也要切开：`unicode61` 把 `deploy部署` 整体当成**一个** token，
    不切边界的话查 `部署` 依然命中 0 行。
    """
    parts: List[str] = []
    previous_cjk: Optional[bool] = None
    for char in text:
        current_cjk = is_cjk(char)
        if previous_cjk is not None and current_cjk != previous_cjk:
            parts.append(separator)
        elif current_cjk and previous_cjk:
            parts.append(separator)
        parts.append(char)
        previous_cjk = current_cjk
    return "".join(parts)


def normalize_for_index(text: str) -> str:
    """入库规范化：NFKC → 小写 → CJK 逐字切分（含边界）→ 空白折叠。"""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).lower()
    return " ".join(split_cjk(folded).split())


def _quote(token: str) -> str:
    """FTS5 字符串字面量：双引号包裹，内部双引号翻倍。"""
    return '"' + token.replace('"', '""') + '"'


def _has_word_char(text: str) -> bool:
    """是否含有可检索的词字符（CJK 与字母数字）；纯符号不是有效查询。"""
    return any(char.isalnum() for char in text)


#: 连续 CJK 片段 或 连续非 CJK 片段
_SEGMENT_RE = None


def _segments(text: str) -> List[tuple]:
    """把文本切成 `(is_cjk_run, text)` 片段，**保留原始分组**。

    分组很重要：`部署方案` 是一个短语（四个字必须相邻），
    而 `部署 方案` 是两个短语（两个词各自相邻即可）。
    归一化后的文本丢失了空白边界（都是从空格分隔），所以必须看原始文本。
    """
    global _SEGMENT_RE
    if _SEGMENT_RE is None:
        import re

        _SEGMENT_RE = re.compile(r"([\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7a3\uf900-\ufaff]+)|([^\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7a3\uf900-\ufaff]+)")
    folded = unicodedata.normalize("NFKC", text).lower()
    result: List[tuple] = []
    for match in _SEGMENT_RE.finditer(folded):
        cjk_run, other = match.group(1), match.group(2)
        if cjk_run:
            result.append((True, cjk_run))
        elif other:
            result.append((False, other))
    return result


def tokenize_query(text: str) -> List[str]:
    """返回查询的"必须相邻"片段列表，用于构造短语查询与人工排查。"""
    terms: List[str] = []
    for is_cjk_run, chunk in _segments(text):
        if is_cjk_run:
            terms.append(" ".join(chunk))
        else:
            terms.extend(word for word in chunk.split() if _has_word_char(word))
    return terms


def to_fts_query(text: str, *, require_all: bool = True) -> Optional[str]:
    """构造安全的 FTS5 MATCH 表达式。

    - 连续 CJK 片段 → 短语（`"部 署"`），因此 2 字中文查询可用，且保持"相邻"语义；
    - 非 CJK 片段 → 逐个空白分隔的词，加引号；
    - 全部片段用 `AND` 连接（默认要求同时命中每一项）；
    - 空串/纯符号 → `None`：调用方必须当作无效查询，**不得**退化成返回全部结果。
    """
    terms = [_quote(term) for term in tokenize_query(text)]
    if not terms:
        return None
    joiner = " AND " if require_all else " OR "
    return joiner.join(terms)


def is_valid_query(text: str) -> bool:
    return to_fts_query(text) is not None


#: 建立 FTS5 检索表的标准 DDL（MVP-1 必须使用同一份 DDL）
FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5("
    "doc_id UNINDEXED, path UNINDEXED, body, tokenize='unicode61 remove_diacritics 2');"
)


def fts5_available(connection) -> bool:
    """探测当前 SQLite 是否编译了 FTS5（拿不到时可安全跳过，不得静默降级为全表扫描）。"""
    try:
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS __fts5_probe USING fts5(x);")
        connection.execute("DROP TABLE IF EXISTS __fts5_probe;")
        return True
    except Exception:
        return False


__all__ = [
    "is_cjk",
    "split_cjk",
    "normalize_for_index",
    "tokenize_query",
    "to_fts_query",
    "is_valid_query",
    "fts5_available",
    "FTS_DDL",
]
