"""检索查询：中文 2 字/3 字必须真实命中，且每个命中都能定位到原文（SPEC §6.2、§16 MVP-1）。

强制路径（不许绕过）：

```text
用户查询 --to_fts_query()--> 安全 MATCH 表达式 --MATCH--> FTS5 --命中--> 原文定位 --> Citation
```

- 查询串**绝不**原样拼进 `MATCH`：邮件正文与资料是不可信输入（§13.5），
  未加引号的 FTS5 语法会被内容破坏甚至变成注入；
- 空串/纯符号 → `to_fts_query()` 返回 `None` → 结果标记 `valid_query=False` 且**返回空集**，
  绝不放任"退化成返回全部结果"；
- 命中里带 `Citation`（`path/locator/source_version/content_hash/supports` 必填），
  由 `asistent.contracts.citations.Citation.validate()` 强制。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..contracts.citations import Citation
from ..contracts.enums import IndexState
from ..contracts.objects import SourceDocument
from ..contracts.search import normalize_for_index, to_fts_query
from ..contracts.store_api import StoreProtocol
from . import fts
from .citation import build_citation, location_detail

#: 未索引但语义上"应该能搜到"的状态：命中时明确告知用户，而不是假装已入库
NOT_INDEXED_STATES = (
    str(IndexState.PENDING),
    str(IndexState.STALE),
    str(IndexState.FAILED),
    str(IndexState.REBUILD_REQUIRED),
)


@dataclass
class SearchHit:
    """一条检索命中：索引信息 + 引用 + 跳过原因（可解释）。"""

    doc_id: str
    path: str
    locator: str = ""
    content_version: Optional[int] = None
    content_hash: str = ""
    excerpt: str = ""
    matched_term: str = ""
    rank: float = 0.0
    citation: Optional[Citation] = None
    #: 非空表示"文档存在但没进索引"，值为原因（例如二进制/超大/索引失败）
    not_indexed_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "path": self.path,
            "locator": self.locator,
            "content_version": self.content_version,
            "content_hash": self.content_hash,
            "excerpt": self.excerpt,
            "matched_term": self.matched_term,
            "rank": self.rank,
            "not_indexed_reason": self.not_indexed_reason,
            "citation": None if self.citation is None else self.citation.to_dict(),
        }

    def describe(self) -> str:
        if self.citation is not None:
            return self.citation.describe()
        return f"[SourceDocument:{self.doc_id} {self.path}#{self.locator}] {self.not_indexed_reason or ''}"


@dataclass
class SearchResponse:
    """一次检索的完整回答，包含"查询是否有效"与"索引是否可用"。"""

    query_text: str
    fts_query: Optional[str]
    valid_query: bool
    fts5_available: bool
    hits: List[SearchHit] = field(default_factory=list)
    invalid_reason: str = ""
    index_error: Optional[str] = None
    strategies: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def doc_ids(self) -> List[str]:
        ordered: List[str] = []
        for hit in self.hits:
            if hit.doc_id not in ordered:
                ordered.append(hit.doc_id)
        return ordered

    @property
    def count(self) -> int:
        return len(self.hits)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query_text": self.query_text,
            "fts_query": self.fts_query,
            "valid_query": self.valid_query,
            "fts5_available": self.fts5_available,
            "invalid_reason": self.invalid_reason,
            "index_error": self.index_error,
            "count": self.count,
            "doc_ids": self.doc_ids,
            "strategies": list(self.strategies),
            "hits": [hit.to_dict() for hit in self.hits],
        }


class InvalidQueryError(ValueError):
    """空串/纯符号查询：调用方必须显式处理，不得当作"查全部"。"""

    code = "INVALID_QUERY"

    def __init__(self, message: str = "查询为空或只有符号：按无效查询处理（禁止返回全部结果）") -> None:
        super().__init__(message)


def build_match_expression(text: str) -> str:
    """把用户查询转成安全 MATCH 表达式；无效查询抛 `InvalidQueryError`。"""
    expression = to_fts_query(text)
    if expression is None:
        raise InvalidQueryError(f"查询 {text!r} 规范化后为空（无任何可检索字符）")
    return expression


def search(
    connection: sqlite3.Connection,
    query_text: str,
    *,
    store: Optional[StoreProtocol] = None,
    limit: int = 20,
    execution_id: Optional[str] = None,
    table: str = fts.DOCUMENT_TABLE,
) -> SearchResponse:
    """执行检索。

    - `query_text` 无效 → `valid_query=False`、`hits=[]`（不报错、不返回全部）；
    - `store` 可选：传了就能带上 `SourceDocument` 的版本/时间/状态；不传则用索引侧快照。
    """
    if not _fts_ready(connection):
        return SearchResponse(
            query_text=query_text,
            fts_query=None,
            valid_query=False,
            fts5_available=False,
            invalid_reason="SQLite 未编译 FTS5：不允许静默降级为全表扫描",
            index_error="FTS5_UNAVAILABLE",
        )

    expression = to_fts_query(query_text)
    if expression is None:
        return SearchResponse(
            query_text=query_text,
            fts_query=None,
            valid_query=False,
            fts5_available=True,
            invalid_reason=f"查询 {query_text!r} 规范化后为空（无任何可检索字符）",
        )

    try:
        rows = connection.execute(
            f"SELECT doc_id, path, raw_body, content_version, content_hash "
            f"FROM {table} WHERE {table} MATCH ? ORDER BY rank, path, doc_id LIMIT ?",
            (expression, max(1, int(limit))),
        ).fetchall()
    except sqlite3.Error as exc:
        return SearchResponse(
            query_text=query_text,
            fts_query=expression,
            valid_query=True,
            fts5_available=True,
            index_error=f"{exc.__class__.__name__}: {exc}",
        )

    hits: List[SearchHit] = []
    for index, row in enumerate(rows):
        doc_id, path, raw_body, content_version, content_hash = (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            int(row[3]),
            str(row[4]),
        )
        document = _load_document(store, doc_id)
        if document is not None:
            content_version = document.content_version
            content_hash = document.content_hash or content_hash
            path = display_path(document, fallback=path)
        detail = location_detail(raw_body, query_text)
        citation = build_citation(
            document_id=doc_id,
            path=path,
            content_hash=content_hash,
            content_version=content_version,
            raw_body=raw_body,
            query_text=query_text,
            supports=f"检索词 {query_text!r} 出现在 {detail['locator']}",
            source_time=document.received_at if document is not None else None,
            used_by_execution_id=execution_id,
        )
        hits.append(
            SearchHit(
                doc_id=doc_id,
                path=path,
                locator=detail["locator"],
                content_version=content_version,
                content_hash=content_hash,
                excerpt=detail["excerpt"],
                matched_term=detail["matched_term"],
                rank=float(index),
                citation=citation,
            )
        )
    return SearchResponse(
        query_text=query_text,
        fts_query=expression,
        valid_query=True,
        fts5_available=True,
        hits=hits,
    )


def search_not_indexed(
    store: StoreProtocol,
    query_text: str,
    *,
    connection: Optional[sqlite3.Connection] = None,
    limit: int = 20,
) -> List[SearchHit]:
    """找出"内容里其实有这个词、但当前搜不到"的对象（PENDING/STALE/FAILED/REBUILD_REQUIRED）。

    这是"不静默丢弃"的检索侧体现：用户搜不到东西时，能看到"有资料但索引没完成/失败"，
    而不是以为资料根本不存在。

    候选来源有两类，合并去重：
    1. **索引里残留的正文**（文档之前被索引过，现在是 STALE/FAILED/REBUILD_REQUIRED）；
    2. **路径/标识命中**的文档（连正文都没进过索引，例如采集时就跳过了）。
    """
    needle = normalize_for_index(query_text).replace(" ", "")
    if not needle:
        return []

    matched_ids: set = set()
    if connection is not None:
        for doc_id, (raw_body, _hash) in _cached_bodies(connection).items():
            if needle in normalize_for_index(raw_body).replace(" ", ""):
                matched_ids.add(doc_id)

    matches: List[SearchHit] = []
    for document in store.find("SourceDocument", limit=100000):
        if not isinstance(document, SourceDocument):  # pragma: no cover - 类型守卫
            continue
        state = str(document.index_state)
        if state not in NOT_INDEXED_STATES:
            continue
        identity = normalize_for_index(f"{document.source_id} {document.origin_path}").replace(" ", "")
        if document.id not in matched_ids and needle not in identity:
            continue
        matches.append(
            SearchHit(
                doc_id=document.id,
                path=display_path(document, fallback=document.origin_path or document.source_id),
                content_version=document.content_version,
                content_hash=document.content_hash,
                not_indexed_reason=f"index_state={state}"
                + (f", error={document.index_error}" if document.index_error else "")
                + (", 索引里残留内容仍可定位" if document.id in matched_ids else ""),
            )
        )
        if len(matches) >= limit:
            break
    return matches


def _cached_bodies(connection: sqlite3.Connection) -> Dict[str, tuple]:
    """索引库里残留的 `{doc_id: (raw_body, content_hash)}`（用于"有内容但搜不到"的排查）。"""
    try:
        rows = connection.execute(
            f"SELECT doc_id, raw_body, content_hash FROM {fts.DOCUMENT_TABLE}"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(row[0]): (str(row[1]), str(row[2])) for row in rows}


def display_path(document: SourceDocument, *, fallback: str = "") -> str:
    """引用里展示的路径：**优先资料库相对路径**（用户看得懂），其次原始绝对路径。

    绝对路径仍然可以从 `SourceDocument.origin_path` 取到，用于真正打开文件。
    """
    relative = document.source_id
    if relative and not os.path.isabs(relative):
        roots = []
        links = document.links if isinstance(document.links, dict) else {}
        for entry in links.get("ingest") or []:
            if isinstance(entry, str):
                roots.append(entry)
        if roots:
            candidate = os.path.join(roots[0], relative.replace("/", os.sep))
            if os.path.exists(candidate):
                return relative.replace("\\", "/")
            return relative.replace("\\", "/")
        return relative.replace("\\", "/")
    return document.origin_path or fallback


def _fts_ready(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute(f"SELECT doc_id FROM {fts.DOCUMENT_TABLE} LIMIT 0")
        return True
    except sqlite3.Error:
        return False


def _load_document(store: Optional[StoreProtocol], doc_id: str) -> Optional[SourceDocument]:
    if store is None:
        return None
    try:
        document = store.get("SourceDocument", doc_id)
    except Exception:
        return None
    return document if isinstance(document, SourceDocument) else None  # pragma: no cover - 类型守卫


__all__ = [
    "SearchHit",
    "SearchResponse",
    "InvalidQueryError",
    "search",
    "search_not_indexed",
    "build_match_expression",
    "NOT_INDEXED_STATES",
]
