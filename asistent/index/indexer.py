"""索引器：把 SourceDocument 变成可检索记录，并维护索引状态机（SPEC §6.2、§16 MVP-1）。

三条硬规则：

1. **原始内容不可被覆盖**：索引只读源文件 / 只读 `SourceDocument` 快照，
   所有写操作都落在索引库（派生数据）与 `SourceDocument.index_state` 上；
2. **索引失败不丢原始资料**：`index_state=FAILED` + 失败原因落库，
   原始资料对象、原文、审计全部保留，可重建（`rebuild_index`）；
3. **状态只走契约状态机**：所有状态变化都经 `StoreProtocol.set_index_state()`，
   非法转换会被拒绝（`PENDING/INDEXED/STALE/FAILED/REBUILD_REQUIRED`）。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..contracts.enums import FailureClass, IndexState
from ..contracts.errors import AsistentError, NotFound
from ..contracts.objects import SourceDocument
from ..contracts.store_api import StoreProtocol
from ..contracts.timeutil import utcnow_iso
from ..ingest.files import FileKind, classify_bytes, decode_text
from . import fts

#: 索引读取源文件时的上限（与采集策略保持一致，避免把超大文件再读进内存）
DEFAULT_MAX_BYTES = 5 * 1024 * 1024

#: `STALE` / `REBUILD_REQUIRED` 意味着"源文件已不在原位"：此时只允许用原文快照恢复索引，
#: 不允许"偷偷"把状态改回 INDEXED 掩盖源文件消失的事实。
SOURCE_GONE_STATES = (str(IndexState.STALE), str(IndexState.REBUILD_REQUIRED))


@dataclass
class SourceRead:
    """一次正文读取的结果。"""

    data: bytes
    #: 正文来自哪里：`file`（源文件，权威）/ `snapshot`（内容寻址快照，兜底）
    origin: str = "file"
    from_file: bool = True
    #: 源文件是否已不可得（不存在/不可读）。为 True 时不得把状态改回 INDEXED。
    source_unavailable: bool = False
    error: Optional[str] = None


@dataclass
class IndexResult:
    """单个文档的索引结果。失败也返回结果对象（不抛异常打断整批），原因必须可读。"""

    doc_id: str
    path: str = ""
    ok: bool = False
    skipped: bool = False
    index_state: Optional[str] = None
    content_version: Optional[int] = None
    content_hash: Optional[str] = None
    revision: Optional[int] = None
    normalized_chars: int = 0
    failure_class: Optional[str] = None
    error: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "path": self.path,
            "ok": self.ok,
            "skipped": self.skipped,
            "index_state": self.index_state,
            "content_version": self.content_version,
            "content_hash": self.content_hash,
            "revision": self.revision,
            "normalized_chars": self.normalized_chars,
            "failure_class": self.failure_class,
            "error": self.error,
            "reason": self.reason,
        }


@dataclass
class RebuildReport:
    """重建索引的汇总（SPEC §6.2：切分规则变了必须能整体重建）。"""

    removed_orphans: List[str] = field(default_factory=list)
    reset_to_pending: List[str] = field(default_factory=list)
    results: List[IndexResult] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def indexed(self) -> int:
        return sum(1 for result in self.results if result.ok and not result.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for result in self.results if result.skipped)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if not result.ok and not result.skipped)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "removed_orphans": list(self.removed_orphans),
            "reset_to_pending": list(self.reset_to_pending),
            "indexed": self.indexed,
            "skipped": self.skipped,
            "failed": self.failed,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "results": [result.to_dict() for result in self.results],
        }


@dataclass
class IndexSyncReport:
    """一次增量同步的汇总。"""

    results: List[IndexResult] = field(default_factory=list)
    removed_orphans: List[str] = field(default_factory=list)
    unchanged: int = 0
    started_at: str = ""
    finished_at: str = ""

    @property
    def indexed(self) -> int:
        return sum(1 for result in self.results if result.ok and not result.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for result in self.results if result.skipped)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if not result.ok and not result.skipped)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "indexed": self.indexed,
            "skipped": self.skipped,
            "failed": self.failed,
            "unchanged": self.unchanged,
            "removed_orphans": list(self.removed_orphans),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "results": [result.to_dict() for result in self.results],
        }


class Indexer:
    """索引器：构造注入 `StoreProtocol` + SQLite 连接。"""

    def __init__(
        self,
        store: StoreProtocol,
        connection: sqlite3.Connection,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        missing_source_state: str = str(IndexState.REBUILD_REQUIRED),
        snapshot_reader: Optional[Callable[[str], Optional[bytes]]] = None,
    ) -> None:
        self._store = store
        self._connection = connection
        self._max_bytes = max_bytes
        self._missing_source_state = missing_source_state
        #: 原文快照读取器（`content_hash -> bytes`）。有快照时**优先用快照**：
        #: 源文件被删/改名也不会丢索引内容（"索引失败不丢原始资料"）。
        self._snapshot_reader = snapshot_reader

    # ------------------------------------------------------------------ 单文档
    def index_document(self, document_id: str, *, force: bool = False) -> IndexResult:
        """索引一个文档。任何失败都以"状态 + 原因"返回，绝不静默。"""
        document = self._load_document(document_id)
        if document is None:
            return IndexResult(
                doc_id=document_id,
                ok=False,
                failure_class=str(FailureClass.MISSING_DATA),
                reason="索引对象不存在",
            )
        path = document.origin_path or document.source_id or document.id
        indexed_before = fts.indexed_hashes(self._connection).get(document.id)

        if not force and self._is_current(document, {document.id: indexed_before} if indexed_before else {}):
            return IndexResult(
                doc_id=document.id,
                path=path,
                ok=str(document.index_state) == str(IndexState.INDEXED),
                index_state=str(document.index_state),
                content_version=document.content_version,
                content_hash=document.content_hash,
                revision=document.revision,
                reason="索引内容已是最新（未变化不重写）",
            )

        try:
            read = self._read_source(document)
        except (OSError, ValueError) as exc:
            return self._fail(
                document,
                path=path,
                error=f"{exc.__class__.__name__}: {exc}",
                reason="源文件不可读；原始资料保留，可稍后重建索引",
                target_state=self._missing_source_state if isinstance(exc, OSError) else str(IndexState.FAILED),
                failure_class=str(FailureClass.MISSING_DATA if isinstance(exc, OSError) else FailureClass.INVALID_INPUT),
            )
        raw_bytes = read.data
        # 源文件已消失、只靠快照兜底：内容仍写进检索表（用户还搜得到），但**状态必须是"待重建"**，
        # 绝不"偷偷"改回 INDEXED 把"文件没了"掩盖成"索引正常"。
        # 注意：`INDEXED → REBUILD_REQUIRED` 不是合法转换，所以这里直接迁到目标态，不先迁 INDEXED。
        source_gone = read.source_unavailable and str(document.index_state) in SOURCE_GONE_STATES
        success_state = str(IndexState.REBUILD_REQUIRED) if source_gone else str(IndexState.INDEXED)

        kind = classify_bytes(raw_bytes, max_bytes=self._max_bytes)
        if kind is not FileKind.TEXT:
            return self._fail(
                document,
                path=path,
                error=f"unsupported_content:{kind.value}",
                reason="内容不是可索引文本（二进制/超大/空）：索引跳过但保留原始资料",
                target_state=str(IndexState.FAILED),
                failure_class=str(FailureClass.INVALID_INPUT),
                skipped=True,
            )

        decoded = decode_text(raw_bytes)
        if decoded is None:
            return self._fail(
                document,
                path=path,
                error="undecodable_text",
                reason="编码无法解码（utf-8/gb18030 均失败）：索引跳过但保留原始资料",
                target_state=str(IndexState.FAILED),
                failure_class=str(FailureClass.INVALID_INPUT),
                skipped=True,
            )

        try:
            indexed = fts.upsert_document(
                self._connection,
                doc_id=document.id,
                path=path,
                raw_body=decoded.text,
                content_version=document.content_version,
                content_hash=document.content_hash,
            )
        except sqlite3.Error as exc:
            return self._fail(
                document,
                path=path,
                error=f"{exc.__class__.__name__}: {exc}",
                reason="写检索表失败：原始资料保留，索引标记失败可重建",
                target_state=str(IndexState.FAILED),
                failure_class=str(FailureClass.INDEX_FAILURE),
            )

        saved = _try_transit(
            self._store,
            document,
            success_state,
            reason=(
                "内容已从原文快照恢复索引，但源文件缺失：状态保持待重建（用户可见）"
                if source_gone
                else "索引写入成功"
            ),
            error=(f"source_unavailable: {read.error}" if source_gone else None),
        )
        if source_gone:
            return IndexResult(
                doc_id=document.id,
                path=path,
                ok=False,
                index_state=str(saved.index_state) if saved is not None else str(IndexState.REBUILD_REQUIRED),
                content_version=document.content_version,
                content_hash=document.content_hash,
                revision=saved.revision if saved is not None else document.revision,
                normalized_chars=len(indexed.body),
                failure_class=str(FailureClass.MISSING_DATA),
                error=f"source_unavailable: {read.error}",
                reason="内容已从快照恢复索引，但源文件缺失：状态保持待重建（用户可见）",
            )
        return IndexResult(
            doc_id=document.id,
            path=path,
            ok=True,
            index_state=str(saved.index_state) if saved is not None else str(IndexState.INDEXED),
            content_version=document.content_version,
            content_hash=document.content_hash,
            revision=saved.revision if saved is not None else document.revision,
            normalized_chars=len(indexed.body),
            reason="索引写入成功",
        )

    # ------------------------------------------------------------------ 批量
    def index_all(
        self, documents: Sequence[SourceDocument], *, force: bool = False
    ) -> List[IndexResult]:
        results: List[IndexResult] = []
        for document in documents:
            results.append(self.index_document(document.id, force=force))
        return results

    def sync(self, *, force: bool = False, limit: int = 100000) -> IndexSyncReport:
        """增量同步：只处理"未索引 / 内容哈希变了 / STALE / FAILED / REBUILD_REQUIRED"的文档。

        同时清掉**孤儿索引**（库里存在、但 SourceDocument 已不存在的检索记录）。
        """
        report = IndexSyncReport(started_at=utcnow_iso())
        documents = [doc for doc in self._store.find("SourceDocument", limit=limit) if isinstance(doc, SourceDocument)]
        known_ids = {document.id for document in documents}
        indexed = fts.indexed_hashes(self._connection)

        for document_id in sorted(set(indexed) - known_ids):
            fts.delete_document(self._connection, document_id)
            fts.mark_index_state(
                self._connection,
                doc_id=document_id,
                path=indexed[document_id][0],
                index_state=str(IndexState.STALE),
                content_hash=indexed[document_id][1],
                error="SourceDocument 已不存在，检索记录已删除",
            )
            report.removed_orphans.append(document_id)

        for document in documents:
            if not force and self._is_current(document, indexed):
                report.unchanged += 1
                continue
            report.results.append(self.index_document(document.id, force=force))

        report.finished_at = utcnow_iso()
        return report

    def rebuild_index(self, *, limit: int = 100000) -> RebuildReport:
        """重建索引：清空派生索引 → 全部转 PENDING → 全量重算（§6.2）。

        重建前**不动**任何原始资料：`SourceDocument`、源文件、审计都保留。
        """
        report = RebuildReport(started_at=utcnow_iso())
        indexed = fts.indexed_hashes(self._connection)
        documents = [doc for doc in self._store.find("SourceDocument", limit=limit) if isinstance(doc, SourceDocument)]
        known_ids = {document.id for document in documents}

        for document_id in sorted(set(indexed) - known_ids):
            fts.delete_document(self._connection, document_id)
            report.removed_orphans.append(document_id)

        for document in documents:
            fts.delete_document(self._connection, document.id)
            fts.mark_index_state(
                self._connection,
                doc_id=document.id,
                path=document.origin_path or document.source_id,
                index_state=str(IndexState.PENDING),
                content_version=document.content_version,
                content_hash=document.content_hash,
                error=None,
            )
            if str(document.index_state) != str(IndexState.PENDING):
                _try_transit(self._store, document, str(IndexState.PENDING), reason="重建索引：重置为待索引")
                report.reset_to_pending.append(document.id)
            else:
                report.reset_to_pending.append(document.id)

        for document in documents:
            report.results.append(self.index_document(document.id, force=True))

        report.finished_at = utcnow_iso()
        return report

    # ------------------------------------------------------------------ 内部
    def _is_current(self, document: SourceDocument, indexed: Optional[Dict[str, Tuple[str, str]]] = None) -> bool:
        """索引内容是否已是最新（不需要重算）。

        `indexed` 参数允许调用方传入已查好的快照（避免同一轮同步里反复查表）。

        - `INDEXED`：内容一致即可（正常路径）；
        - `STALE`：**内容仍然一致**时保持 STALE、不重算——STALE 表达的是"源文件不在原位"，
          这是用户需要看到的事实，不能被一次重算抹掉；内容确实变了才重算（回到 INDEXED）。
        """
        entries = indexed if indexed is not None else fts.indexed_hashes(self._connection)
        entry = entries.get(document.id)
        if entry is None or entry[1] != document.content_hash:
            return False
        return str(document.index_state) in (str(IndexState.INDEXED), str(IndexState.STALE))

    def _load_document(self, document_id: str) -> Optional[SourceDocument]:
        try:
            document = self._store.get("SourceDocument", document_id)
        except NotFound:
            return None
        return document if isinstance(document, SourceDocument) else None  # pragma: no cover - 类型守卫

    def _read_source(self, document: SourceDocument) -> SourceRead:
        """读取待索引正文：**先看源文件**（它是"这份资料还存在"的真凭据），
        源文件不可得时才退到原文快照（内容寻址，保证索引失败/源文件消失也不丢正文）。

        顺序很重要：源文件被删除的资料必须能被识别出来（转 `REBUILD_REQUIRED`），
        否则"文件没了"会被快照悄悄掩盖成"索引正常"。
        """
        path = document.origin_path
        failure: Optional[str] = None
        if path:
            try:
                if not os.path.isfile(path):
                    raise OSError(f"源文件不存在: {path}")
                size = os.path.getsize(path)
                if size > self._max_bytes:
                    raise ValueError(f"源文件超过索引上限: size_bytes={size} > max_bytes={self._max_bytes}")
                with open(path, "rb") as handle:  # 只读；索引绝不回写源文件
                    return SourceRead(data=handle.read(), origin="file", from_file=True)
            except (OSError, ValueError) as exc:
                failure = f"{exc.__class__.__name__}: {exc}"
        else:
            failure = "SourceDocument 缺少 origin_path"

        if self._snapshot_reader is not None and document.content_hash:
            try:
                snapped = self._snapshot_reader(document.content_hash)
            except Exception:  # pragma: no cover - 快照读取失败不能阻断
                snapped = None
            if snapped is not None:
                return SourceRead(
                    data=snapped, origin="snapshot", from_file=False, source_unavailable=True, error=failure
                )
        raise OSError(failure or "源文件不存在且无快照")

    def _fail(
        self,
        document: SourceDocument,
        *,
        path: str,
        error: str,
        reason: str,
        target_state: str,
        failure_class: str,
        skipped: bool = False,
    ) -> IndexResult:
        fts.mark_index_state(
            self._connection,
            doc_id=document.id,
            path=path,
            index_state=str(target_state),
            content_version=document.content_version,
            content_hash=document.content_hash,
            error=error,
        )
        saved = _transit_via_stale(self._store, document, str(target_state), reason=reason, error=error)
        return IndexResult(
            doc_id=document.id,
            path=path,
            ok=False,
            skipped=skipped,
            index_state=str(saved.index_state) if saved is not None else str(target_state),
            content_version=document.content_version,
            content_hash=document.content_hash,
            revision=saved.revision if saved is not None else document.revision,
            failure_class=failure_class,
            error=error,
            reason=reason,
        )


def _transit_via_stale(
    store: StoreProtocol,
    document: SourceDocument,
    target_state: str,
    *,
    reason: str,
    error: Optional[str] = None,
) -> SourceDocument:
    """把文档迁到 `FAILED` / `REBUILD_REQUIRED`。

    `INDEXED → FAILED` / `INDEXED → REBUILD_REQUIRED` 在契约状态机里**不是**合法转换：
    必须先经 `STALE`（契约表：`INDEXED → STALE`、`STALE → FAILED | REBUILD_REQUIRED`）。
    这里显式走两步，绝不为了省事绕过状态机。
    """
    current = str(document.index_state)
    if target_state in (str(IndexState.FAILED), str(IndexState.REBUILD_REQUIRED)) and current in (
        str(IndexState.INDEXED),
        str(IndexState.PENDING),
    ):
        staged = _try_transit(store, document, str(IndexState.STALE), reason="索引重算前的中间状态") or document
        return _try_transit(store, staged, target_state, reason=reason, error=error) or staged
    return _try_transit(store, document, target_state, reason=reason, error=error) or document


def _try_transit(
    store: StoreProtocol,
    document: SourceDocument,
    target_state: str,
    *,
    reason: str,
    error: Optional[str] = None,
) -> Optional[SourceDocument]:
    """状态迁移（走契约状态机）。失败时返回 None，并把原因写进 original 的返回信息里。

    这里**不抛异常**：单个文档状态非法不能打断整批索引；调用方从 `IndexResult.error`
    或 `index_state` 看到真实结果。实现上仍通过 `set_index_state()`，因此非法转换
    一定被契约拒绝，不会被绕过。
    """
    if str(document.index_state) == target_state and target_state != str(IndexState.INDEXED):
        return document
    try:
        saved = store.set_index_state(
            document.id,
            target_state,
            expected_revision=document.revision,
            reason=reason,
            error=error,
        )
    except (AsistentError, ValueError):
        return None
    return saved if isinstance(saved, SourceDocument) else None  # pragma: no cover - 类型守卫


def index_path(connection: sqlite3.Connection, document_id: str) -> Optional[str]:
    """索引库里的路径（用于排查"索引里还有、源文件没了"的情况）。"""
    entry = fts.indexed_hashes(connection).get(document_id)
    return None if entry is None else entry[0]


__all__ = [
    "Indexer",
    "IndexResult",
    "SourceRead",
    "RebuildReport",
    "IndexSyncReport",
    "DEFAULT_MAX_BYTES",
    "SOURCE_GONE_STATES",
    "index_path",
]
