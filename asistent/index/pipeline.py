"""MVP-1 端到端流水线：采集 → 索引 → 中文检索（一条可重复运行的路径）。

这一步是"60 秒内可检索"的可执行定义：

```text
新文件落盘
  ├─ collect_directory()  扫描 + 判重 + 写 SourceDocument（含原文快照）
  ├─ Indexer.sync()       未变化的不重写，变化的重算 FTS5 索引
  └─ search()             normalize 入库 + to_fts_query 查询 → 命中 + Citation
```

计时用 `time.monotonic()`（不受墙钟回跳影响，SPEC §11.2 的时钟异常不会污染耗时结论）。
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..contracts.store_api import StoreProtocol
from ..contracts.timeutil import utcnow_iso
from ..ingest.collector import CollectionPolicy, CollectionReport, collect_directory
from . import fts
from .indexer import IndexSyncReport, Indexer, RebuildReport
from .query import SearchResponse, search, search_not_indexed
from .snapshots import SnapshotStore


class Stopwatch:
    """单调计时器：`elapsed_ms()` / `elapsed_seconds()`。"""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self.started = clock()

    def elapsed_seconds(self) -> float:
        return self._clock() - self.started

    def elapsed_ms(self) -> float:
        return self.elapsed_seconds() * 1000.0

    def restart(self) -> None:
        self.started = self._clock()


@dataclass
class CollectionRun:
    """一次"落盘 → 可检索"的完整记录（验收证据结构）。"""

    collect_seconds: float
    index_seconds: float
    search_seconds: float
    ingest_total_seconds: float
    collection: CollectionReport
    indexing: IndexSyncReport
    query_text: str = ""
    response: Optional[SearchResponse] = None
    started_at: str = ""
    finished_at: str = ""

    @property
    def total_seconds(self) -> float:
        return self.collect_seconds + self.index_seconds

    @property
    def normalized_bodies_char_counts(self) -> Dict[str, int]:
        return {result.doc_id: result.normalized_chars for result in self.indexing.results}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "collect_seconds": round(self.collect_seconds, 6),
            "index_seconds": round(self.index_seconds, 6),
            "search_seconds": round(self.search_seconds, 6),
            "ingest_total_seconds": round(self.ingest_total_seconds, 6),
            "collection": self.collection.to_dict(),
            "indexing": self.indexing.to_dict(),
        }


class SearchPipeline:
    """采集 + 索引 + 检索的组合（构造注入 store 与 SQLite 连接）。"""

    def __init__(
        self,
        store: StoreProtocol,
        connection: sqlite3.Connection,
        *,
        snapshot_root: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> None:
        self.store = store
        self.connection = connection
        self.snapshots = SnapshotStore(snapshot_root) if snapshot_root else None
        self.indexer = Indexer(
            store,
            connection,
            **({"max_bytes": max_bytes} if max_bytes is not None else {}),
            snapshot_reader=self.snapshots.get if self.snapshots else None,
        )

    # ------------------------------------------------------------------ 采集
    def policy(self, **overrides: Any) -> CollectionPolicy:
        """生成采集策略；默认挂上原文快照 sink（有快照才能"索引失败不丢资料"）。"""
        base: Dict[str, Any] = {
            "raw_sink": self._snapshot_sink if self.snapshots is not None else None,
        }
        if "max_bytes" in overrides:
            base["max_bytes"] = overrides.pop("max_bytes")
        base.update(overrides)
        return CollectionPolicy(**base)

    def _snapshot_sink(self, absolute_path: str, relative_path: str, content_hash: str, raw: bytes) -> None:
        assert self.snapshots is not None  # pragma: no cover - 由构造条件保证
        self.snapshots.put(raw, content_hash=content_hash, relative_path=relative_path)

    def collect(
        self,
        roots: Sequence[str],
        *,
        policy: Optional[CollectionPolicy] = None,
        force_reindex: bool = False,
        **policy_overrides: Any,
    ) -> CollectionRun:
        """采集 + 索引。返回带实测耗时的运行记录。"""
        active = policy or self.policy(**policy_overrides)
        started_at = utcnow_iso()
        stopwatch = Stopwatch()
        collection = collect_directory(self.store, roots, policy=active)
        collect_seconds = stopwatch.elapsed_seconds()
        stopwatch.restart()
        indexing = self.indexer.sync(force=force_reindex)
        index_seconds = stopwatch.elapsed_seconds()
        return CollectionRun(
            collect_seconds=collect_seconds,
            index_seconds=index_seconds,
            search_seconds=0.0,
            ingest_total_seconds=collect_seconds + index_seconds,
            collection=collection,
            indexing=indexing,
            started_at=started_at,
            finished_at=utcnow_iso(),
        )

    # ------------------------------------------------------------------ 检索
    def search(self, query_text: str, *, limit: int = 20, execution_id: Optional[str] = None) -> SearchResponse:
        stopwatch = Stopwatch()
        response = search(
            self.connection,
            query_text,
            store=self.store,
            limit=limit,
            execution_id=execution_id,
        )
        self._last_search_seconds = stopwatch.elapsed_seconds()
        return response

    @property
    def last_search_seconds(self) -> float:
        return float(getattr(self, "_last_search_seconds", 0.0))

    def search_with_timing(self, query_text: str, *, limit: int = 20) -> tuple:
        """返回 `(SearchResponse, 实测秒数)`，供"检索耗时"取证。"""
        stopwatch = Stopwatch()
        response = search(self.connection, query_text, store=self.store, limit=limit)
        return response, stopwatch.elapsed_seconds()

    def not_indexed(self, query_text: str, *, limit: int = 20):
        """能看到"有资料但索引没完成/失败"，而不是让用户以为资料不存在。"""
        return search_not_indexed(self.store, query_text, connection=self.connection, limit=limit)

    # ------------------------------------------------------------------ 维护
    def rebuild_index(self) -> RebuildReport:
        return self.indexer.rebuild_index()

    def freshness(self) -> Dict[str, Any]:
        """索引新鲜度：契约 `recovery_scan` 的待索引清单 + 索引库状态表。"""
        scan = self.store.recovery_scan()
        rows = fts.index_state_rows(self.connection)
        return {
            "pending_index_document_ids": list(scan.pending_index_document_ids),
            "index_state_rows": rows,
            "fts_documents": fts.count_rows(self.connection, fts.DOCUMENT_TABLE),
            "fts_search_rows": fts.count_rows(self.connection, fts.SEARCH_TABLE),
            "snapshot_files": 0 if self.snapshots is None else len(self.snapshots.list_hashes()),
            "snapshot_bytes": 0 if self.snapshots is None else self.snapshots.total_bytes(),
        }


__all__ = ["SearchPipeline", "CollectionRun", "Stopwatch", "fts"]
