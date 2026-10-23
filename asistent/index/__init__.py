"""MVP-1 索引与检索层（SPEC §6.2、§16 MVP-1）。

- `fts`：FTS5 建表/写入/删除/索引状态表（**只**用契约的 `FTS_DDL` 与切分函数）；
- `indexer`：把 `SourceDocument` 变成可检索记录，维护 `IndexState` 状态机；
- `query`：安全 `MATCH` 查询 → 命中 + `Citation`（含 `path/locator/version/hash/supports`）；
- `citation`：把命中定位到原文行号区间与摘录；
- `snapshots`：内容寻址的**原文快照**，保证索引失败/源文件消失也不丢资料；
- `pipeline`：采集 → 索引 → 检索 的端到端组合与实测计时。

中文检索的唯一正确路径（`unicode61` 直接查中文会静默命中 0 行）：

    入库 normalize_for_index()  →  查询 to_fts_query()  →  MATCH
"""

from __future__ import annotations

from .citation import QuoteLocation, build_citation, locate_quote, query_terms
from .fts import (
    DOCUMENT_INDEX_DDL,
    DOCUMENT_TABLE,
    INDEX_STATE_DDL,
    SEARCH_TABLE,
    FtsUnavailableError,
    IndexedDoc,
    IndexWriteResult,
    count_rows,
    delete_document,
    fetch_raw_body,
    index_state_rows,
    indexed_hashes,
    mark_index_state,
    open_index_db,
    upsert_document,
)
from .indexer import IndexResult, IndexSyncReport, Indexer, RebuildReport
from .pipeline import CollectionRun, SearchPipeline, Stopwatch
from .query import InvalidQueryError, SearchHit, SearchResponse, search
from .snapshots import SnapshotStore, StoredSnapshot

__all__ = [
    # fts
    "open_index_db",
    "upsert_document",
    "delete_document",
    "mark_index_state",
    "index_state_rows",
    "indexed_hashes",
    "count_rows",
    "fetch_raw_body",
    "FtsUnavailableError",
    "DOCUMENT_INDEX_DDL",
    "INDEX_STATE_DDL",
    "SEARCH_TABLE",
    "DOCUMENT_TABLE",
    "IndexedDoc",
    "IndexWriteResult",
    # indexer
    "Indexer",
    "IndexResult",
    "IndexSyncReport",
    "RebuildReport",
    # query
    "search",
    "SearchHit",
    "SearchResponse",
    "InvalidQueryError",
    # citation
    "build_citation",
    "locate_quote",
    "query_terms",
    "QuoteLocation",
    # snapshots
    "SnapshotStore",
    "StoredSnapshot",
    # pipeline
    "SearchPipeline",
    "CollectionRun",
    "Stopwatch",
]
