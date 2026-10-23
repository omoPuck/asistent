"""引用定位：从"命中"到"用户能打开原文的那一行"（SPEC §6.2、R-006、§16 MVP-1）。

契约要求引用至少给全：来源对象与路径、**原文位置**、原文时间、使用时的版本/哈希、
以及该引用支持的结论（`asistent.contracts.citations.Citation.validate()` 会强制校验）。

本模块只做一件事：把检索词定位到原文的**行号区间 + 摘录**，然后交给契约的
`Citation` 去校验。找不到精确行号时**不伪造**：退化为 `line:1-<last>` 并标明
`locator_kind=document`，宁可粗也不能假。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from ..contracts.citations import Citation
from ..contracts.search import tokenize_query

#: 摘录窗口（字符），命中点前后各取一段
EXCERPT_RADIUS = 40

#: 单行摘录上限，避免一行几万字把报告和 UI 打爆
MAX_EXCERPT_CHARS = 160


@dataclass(frozen=True)
class QuoteLocation:
    """原文定位结果。"""

    locator: str
    start_line: int
    end_line: int
    excerpt: str
    #: `exact`（按查询字面量找到） / `term`（按查询的分段找到） / `document`（找不到，退化到整篇）
    locator_kind: str
    matched_term: str = ""


def query_terms(text: str) -> List[str]:
    """把查询拆成"相邻即可匹配"的字面量，用于在原文里定位。

    - CJK 连续串 → `"部 署"` → 去掉空格得到 `部署`（原文里就是连写的）；
    - 非 CJK → 直接是单词；
    - 混合查询 `部署 deploy` → `["部署", "deploy"]`。
    """
    terms: List[str] = []
    for segment in tokenize_query(text):
        literal = segment.replace(" ", "")
        if literal and literal not in terms:
            terms.append(literal)
    return terms


def _collapsed_with_map(text: str) -> tuple:
    """折叠空白（含换行）并保留"折叠后位置 → 原文位置"的映射。"""
    chars: List[str] = []
    mapping: List[int] = []
    pending_space = False
    for index, char in enumerate(text):
        if char.isspace():
            pending_space = True
            continue
        if pending_space and chars:
            chars.append(" ")
            mapping.append(index)
        pending_space = False
        chars.append(char)
        mapping.append(index)
    return "".join(chars), mapping


def _line_of(text: str, index: int) -> int:
    """字符偏移 → 1 起的行号。"""
    if index <= 0:
        return 1
    return text.count("\n", 0, index) + 1


def locate_quote(raw_body: str, query_text: str) -> QuoteLocation:
    """在原文里定位查询字面量，返回行号区间与摘录。

    先试完整查询（保留"相邻"语义），再按分段逐个试；都不中则退化为整篇区间。
    """
    total_lines = max(1, raw_body.count("\n") + 1)
    if not raw_body:
        return QuoteLocation(
            locator=f"line:1-{total_lines}", start_line=1, end_line=total_lines,
            excerpt="", locator_kind="document",
        )
    collapsed, mapping = _collapsed_with_map(raw_body)

    candidates: List[tuple] = []
    whole = " ".join(query_text.split())
    if whole:
        candidates.append(("exact", whole))
    for term in query_terms(query_text):
        candidates.append(("term", term))

    for kind, needle in candidates:
        if not needle:
            continue
        position = collapsed.casefold().find(needle.casefold())
        if position < 0:
            continue
        start_char = mapping[position] if position < len(mapping) else len(raw_body) - 1
        end_index = min(len(mapping) - 1, position + len(needle) - 1)
        end_char = mapping[end_index] if end_index >= 0 else start_char
        start_line = _line_of(raw_body, start_char)
        end_line = _line_of(raw_body, end_char)
        excerpt = _excerpt(raw_body, start_char, end_char)
        return QuoteLocation(
            locator=f"line:{start_line}-{end_line}",
            start_line=start_line,
            end_line=end_line,
            excerpt=excerpt,
            locator_kind=kind,
            matched_term=needle,
        )

    return QuoteLocation(
        locator=f"line:1-{total_lines}",
        start_line=1,
        end_line=total_lines,
        excerpt=_excerpt(raw_body, 0, min(len(raw_body) - 1, MAX_EXCERPT_CHARS)),
        locator_kind="document",
    )


def _excerpt(raw_body: str, start_char: int, end_char: int) -> str:
    left = max(0, start_char - EXCERPT_RADIUS)
    right = min(len(raw_body), end_char + 1 + EXCERPT_RADIUS)
    snippet = " ".join(raw_body[left:right].split())
    if len(snippet) > MAX_EXCERPT_CHARS:
        snippet = snippet[:MAX_EXCERPT_CHARS] + "…"
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(raw_body) else ""
    return f"{prefix}{snippet}{suffix}"


def build_citation(
    *,
    document_id: str,
    path: str,
    content_hash: str,
    content_version: Optional[int],
    raw_body: str,
    query_text: str,
    supports: str = "",
    source_time: Optional[str] = None,
    source_object_type: str = "SourceDocument",
    used_by_execution_id: Optional[str] = None,
) -> Citation:
    """构造并通过契约校验的引用。

    `supports` 默认写成"命中查询词的位置"，`Citation.validate()` 会拦住任何缺字段的引用。
    """
    location = locate_quote(raw_body, query_text)
    statement = supports or f"命中检索词 {query_text!r} 的原文位置"
    citation = Citation(
        source_object_id=document_id,
        source_object_type=source_object_type,
        path=path,
        locator=location.locator,
        source_time=source_time,
        source_version=content_version,
        content_hash=content_hash,
        supports=statement,
        used_by_execution_id=used_by_execution_id,
        excerpt=location.excerpt,
    )
    citation.validate()
    return citation


def location_detail(raw_body: str, query_text: str) -> Dict[str, Any]:
    """定位细节（写入命中报告的附加信息，便于排查"为什么定位到这一行"）。"""
    location = locate_quote(raw_body, query_text)
    return {
        "locator": location.locator,
        "locator_kind": location.locator_kind,
        "matched_term": location.matched_term,
        "excerpt": location.excerpt,
    }


__all__ = [
    "QuoteLocation",
    "EXCERPT_RADIUS",
    "MAX_EXCERPT_CHARS",
    "query_terms",
    "locate_quote",
    "build_citation",
    "location_detail",
]
