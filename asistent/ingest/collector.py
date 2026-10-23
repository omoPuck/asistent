"""资料采集：目录扫描 → 判重 → 写入 SourceDocument（SPEC §6.1、§16 MVP-1）。

关键不变量（每条都有对应测试）：
1. **原始内容不被覆盖**：`SourceDocument` 只登记身份、路径、大小、哈希与版本；
   可检索正文存在索引侧的只读快照表里，模型输出永远写不到原始资料上；
2. **幂等**：同一目录扫描两次，第二次对未变化文件**一个写操作都不做**——
   对象数不变、审计条数不变（不是"写同样的内容"）；
3. **增量**：只有 `mtime + size + sha256` 有变化的文件才重新入库（sha256 是真值，
   mtime/size 只是先筛，避免同一秒内的同长度改写被漏掉）；
4. **不静默丢弃**：二进制/超大/空/不可读/后缀不匹配一律产生
   `AuditEvent(action=COLLECT_SKIP, result=SKIPPED)` + `Event(CONTENT_SKIPPED)`，
   并在报告里带上原因；
5. **只读源文件**：采集全程只以二进制只读方式打开源文件；
6. **源文件改名/删除可解释**：旧对象转 `STALE`（索引层再 `REBUILD_REQUIRED`），
   不删除、不覆盖、不遗留"看不见的检索结果"。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..contracts.enums import ActorKind, AuditAction, EventKind, IndexState
from ..contracts.errors import NotFound
from ..contracts.ids import idempotency_key, sha256_bytes
from ..contracts.objects import AuditEvent, Event, SourceDocument
from ..contracts.search import normalize_for_index
from ..contracts.store_api import StoreProtocol
from ..contracts.timeutil import utcnow_iso
from .files import FileKind, classify_bytes, decode_text, extract_index_text, unsupported_reason
from .scanner import ScanReport, normalize_relative, scan_roots

#: SourceDocument 身份命名空间：`(SOURCE_NAMESPACE, source_system, relative_path)`
SOURCE_NAMESPACE = "source_document"

#: 采集阶段自定义审计动作（不新增契约枚举，用字符串标注具体动作）
AUDIT_COLLECT = "COLLECT"
AUDIT_COLLECT_SKIP = "COLLECT_SKIP"

#: 采集阶段事件种类（事件 `kind` 是自由字符串，`CONTENT_CHANGED`/`CONTENT_SKIPPED` 为本层扩展）
EVENT_CONTENT_CHANGED = "CONTENT_CHANGED"
EVENT_CONTENT_SKIPPED = "CONTENT_SKIPPED"
EVENT_CONTENT_UNCHANGED = "CONTENT_UNCHANGED"
EVENT_CONTENT_REMOVED = "CONTENT_REMOVED"


class FileStatus(str):
    """采集结果状态（字符串常量，便于直接写审计与报告）。"""

    INGESTED = "INGESTED"
    UNCHANGED = "UNCHANGED"
    SKIPPED = "SKIPPED"
    SKIPPED_UNREADABLE = "SKIPPED_UNREADABLE"
    TRANSITIONED = "TRANSITIONED"


@dataclass(frozen=True)
class FileOutcome:
    """单个文件的采集结果。跳过也必须给出原因。"""

    relative_path: str
    status: str
    size_bytes: int = 0
    content_hash: str = ""
    document_id: Optional[str] = None
    reason: str = ""
    error_class: Optional[str] = None
    previous_document_id: Optional[str] = None
    index_state: Optional[str] = None

    @property
    def is_ingested(self) -> bool:
        return self.status in (FileStatus.INGESTED, FileStatus.TRANSITIONED)

    @property
    def is_skipped(self) -> bool:
        return self.status in (FileStatus.SKIPPED, FileStatus.SKIPPED_UNREADABLE)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "status": self.status,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
            "document_id": self.document_id,
            "reason": self.reason,
            "error_class": self.error_class,
            "previous_document_id": self.previous_document_id,
            "index_state": self.index_state,
        }


@dataclass
class CollectionPolicy:
    """采集策略。默认值是保守的（不跟随链接、只收文本资料、5MiB 上限）。"""

    #: 默认后缀白名单；`None` 表示不按后缀过滤（全部交给内容探测）
    suffixes: Optional[Sequence[str]] = (
        ".md",
        ".markdown",
        ".txt",
        ".text",
        ".json",
        ".csv",
        ".yaml",
        ".yml",
        ".log",
        ".rst",
        ".org",
    )
    #: 单文件上限（字节），超过则不索引并记录原因
    max_bytes: int = 5 * 1024 * 1024
    #: 待索引文本长度上限（字符），超过则截断并在原因里注明
    max_chars: int = 2_000_000
    #: 是否跟随符号链接（默认否；开启也不会越出扫描根）
    follow_symlinks: bool = False
    #: 目录深度上限
    max_depth: int = 64
    #: 顶层目录名/文件名忽略名单（大小写不敏感）
    ignore_names: Tuple[str, ...] = (".git", ".svn", "__pycache__", ".DS_Store", "node_modules", ".mypy_cache", ".pytest_cache")
    #: 后缀不匹配的文件是否也留跳过记录（默认留，取证完整）
    report_suffix_skips: bool = True
    #: 采集到新内容时的回调：`(absolute_path, relative_path, content_hash, raw_bytes)`
    #: 用于把原始字节快照交给索引侧（保持只读快照，防止后续索引改写原始资料）
    raw_sink: Optional[Callable[[str, str, str, bytes], None]] = None
    #: 采集来源系统标识，写入 `SourceDocument.source_system`
    source_system: str = "local_fs"
    #: 是否在删除/改名时把旧对象标记 STALE（默认是）
    mark_removed_stale: bool = True


@dataclass
class CollectionReport:
    """一次采集的汇总（可 JSON 化，直接贴进验收文档）。"""

    roots: List[str] = field(default_factory=list)
    outcomes: List[FileOutcome] = field(default_factory=list)
    new_documents: int = 0
    updated_documents: int = 0
    unchanged_files: int = 0
    skipped_files: int = 0
    unreadable_files: int = 0
    transitioned_documents: int = 0
    stale_documents: int = 0
    index_pending_documents: int = 0
    errors: List[Dict[str, str]] = field(default_factory=list)
    scanned_files: int = 0
    started_at: str = ""
    finished_at: str = ""

    @property
    def ingested_files(self) -> List[FileOutcome]:
        return [outcome for outcome in self.outcomes if outcome.is_ingested]

    @property
    def skipped(self) -> List[FileOutcome]:
        return [outcome for outcome in self.outcomes if outcome.is_skipped]

    def fingerprint(self) -> str:
        """扫描内容指纹：只由**排序后的相对路径 + 内容哈希**决定。

        重复扫描（内容未变）必须给出同一个指纹；空目录给出空结构指纹。
        """
        pairs = sorted(
            (outcome.relative_path, outcome.content_hash)
            for outcome in self.outcomes
            if outcome.content_hash
        )
        return idempotency_key("ingest_scan", [[path, digest] for path, digest in pairs])

    def skip_reasons(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for outcome in self.skipped:
            counts[outcome.reason] = counts.get(outcome.reason, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "roots": list(self.roots),
            "scanned_files": self.scanned_files,
            "new_documents": self.new_documents,
            "updated_documents": self.updated_documents,
            "unchanged_files": self.unchanged_files,
            "skipped_files": self.skipped_files,
            "unreadable_files": self.unreadable_files,
            "transitioned_documents": self.transitioned_documents,
            "stale_documents": self.stale_documents,
            "index_pending_documents": self.index_pending_documents,
            "fingerprint": self.fingerprint(),
            "skip_reasons": self.skip_reasons(),
            "errors": list(self.errors),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


# --------------------------------------------------------------------------------------
# 身份
# --------------------------------------------------------------------------------------


def source_identity_key(relative_path: str, *, source_system: str = "local_fs") -> str:
    """来源身份键：账号/来源作用域内的稳定路径身份（SPEC §21.1）。

    路径是**唯一自然身份**：内容改了还是同一份资料（`content_version` 递增），
    只有路径变了才是另一份资料。
    """
    return idempotency_key(SOURCE_NAMESPACE, source_system, normalize_relative(relative_path))


def source_document_id(relative_path: str, *, source_system: str = "local_fs") -> str:
    """确定性对象 ID：同一路径 → 同一 ID（重扫不会创建第二份对象）。"""
    token = idempotency_key("doc_id", source_system, normalize_relative(relative_path)).split(":", 1)[1][:32]
    return f"doc_{token}"


def _identity_from_source_id(source_id: str, *, source_system: str) -> str:
    """从 `source_id` 反推身份键（用于解析历史遗留的随机 ID 对象）。"""
    marker = "identity:"
    if marker in source_id:
        return source_id.split(marker, 1)[1]
    return idempotency_key(SOURCE_NAMESPACE, source_system, source_id)


def _mtime_iso(mtime_ns: int) -> str:
    seconds, nanos = divmod(max(0, int(mtime_ns)), 1_000_000_000)
    from datetime import datetime, timezone

    moment = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=nanos // 1000)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _event(
    kind: str,
    source_event_id: str,
    *,
    document_id: str,
    case_id: Optional[str],
    payload: Dict[str, Any],
) -> Event:
    moment = utcnow_iso()
    return Event(
        id="",
        kind=kind,
        source="ingest:local_fs",
        source_event_id=source_event_id,
        occurred_at=moment,
        received_at=moment,
        case_id=case_id,
        payload=payload,
    )


def _emit_skip_event(
    store: StoreProtocol,
    *,
    relative_path: str,
    reason: str,
    size_bytes: int,
    content_hash: str,
    document_id: Optional[str],
) -> None:
    """跳过必须留下**审计 + 事件**两条痕迹（SPEC §11.3、§13.1 "不静默丢弃"）。"""
    event_id = uuid.uuid4().hex
    store.publish_event(
        _event(
            EVENT_CONTENT_SKIPPED,
            source_event_id=f"skip:{relative_path}:{content_hash}",
            document_id=document_id or "",
            case_id=None,
            payload={
                "relative_path": relative_path,
                "reason": reason,
                "size_bytes": size_bytes,
                "content_hash": content_hash,
                "document_id": document_id,
            },
        )
    )
    store.append_audit(
        AuditEvent(
            id="",
            at=utcnow_iso(),
            action=AUDIT_COLLECT_SKIP,
            actor_kind=ActorKind.SYSTEM,
            actor_id="ingest",
            object_type="SourceDocument",
            object_id=document_id or f"unregistered:{relative_path}",
            result="SKIPPED",
            content_hash=content_hash or None,
            reason=reason,
            caller="ingest",
            correlation_id=event_id,
            detail={
                "relative_path": relative_path,
                "size_bytes": size_bytes,
                "reason": reason,
            },
        )
    )


def _get_existing(store: StoreProtocol, document_id: str) -> Optional[SourceDocument]:
    try:
        existing = store.get("SourceDocument", document_id)
    except NotFound:
        return None
    if isinstance(existing, SourceDocument):
        return existing
    return None  # pragma: no cover - 类型守卫


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------


def collect_directory(
    store: StoreProtocol,
    roots: Sequence[str],
    *,
    policy: Optional[CollectionPolicy] = None,
    now: Optional[str] = None,
) -> CollectionReport:
    """采集一个或多个目录。返回带原因的报告；源文件不被修改。"""
    active = policy or CollectionPolicy()
    report = CollectionReport(roots=[os.path.abspath(root) for root in roots], started_at=now or utcnow_iso())

    scans: List[ScanReport] = scan_roots(
        list(roots),
        follow_symlinks=active.follow_symlinks,
        suffixes=active.suffixes,
        max_depth=active.max_depth,
    )
    ignore = {name.casefold() for name in active.ignore_names}

    items: List[Tuple] = []
    for scan in scans:
        report.errors.extend(scan.errors)
        if active.report_suffix_skips:
            for entry in scan.skipped:
                report.outcomes.append(
                    FileOutcome(
                        relative_path=normalize_relative(os.path.relpath(entry["path"], scan.root)),
                        status=FileStatus.SKIPPED,
                        reason=entry["reason"],
                    )
                )
                report.skipped_files += 1
        for entry in scan.pruned:
            if entry["reason"] == "symlink_not_followed":
                report.outcomes.append(
                    FileOutcome(
                        relative_path=normalize_relative(os.path.relpath(entry["path"], scan.root)),
                        status=FileStatus.SKIPPED,
                        reason="symlink_not_followed（策略默认不跟随符号链接）",
                    )
                )
                report.skipped_files += 1
            else:
                report.errors.append({"path": entry["path"], "reason": entry["reason"]})
        for item in scan.items:
            if any(part.casefold() in ignore for part in item.relative_path.split("/")):
                report.outcomes.append(
                    FileOutcome(relative_path=item.relative_path, status=FileStatus.SKIPPED, reason="ignored_name")
                )
                report.skipped_files += 1
                continue
            items.append(item)

    items.sort(key=lambda entry: (entry.root, entry.relative_path.casefold()))
    report.scanned_files = len(items)

    seen_now: set = set()
    for item in items:
        outcome = _ingest_one(store, item, active, report)
        report.outcomes.append(outcome)
        if not outcome.is_skipped:
            seen_now.add(outcome.relative_path)

    if active.mark_removed_stale:
        _mark_missing_stale(store, report, seen_now, active, roots=[os.path.abspath(root) for root in roots])

    report.index_pending_documents = sum(
        1
        for outcome in report.outcomes
        if outcome.status == FileStatus.INGESTED and outcome.index_state == str(IndexState.PENDING)
    )
    report.finished_at = utcnow_iso()
    return report


def _ingest_one(store: StoreProtocol, item, policy: CollectionPolicy, report: CollectionReport) -> FileOutcome:
    document_id = source_document_id(item.relative_path, source_system=policy.source_system)
    identity = source_identity_key(item.relative_path, source_system=policy.source_system)
    existing = _get_existing(store, document_id)
    if existing is None:
        # 兼容历史遗留对象：按 source_id/identity 查一遍，避免重复造对象
        legacy = store.find_one("SourceDocument", source_id=item.relative_path) or store.find_one(
            "SourceDocument", identity_key=identity
        )
        if isinstance(legacy, SourceDocument):
            existing = legacy
            document_id = legacy.id

    raw: Optional[bytes] = None
    read_error: Optional[BaseException] = None
    try:
        with open(item.absolute_path, "rb") as handle:  # 只读；不写、不改时间戳
            raw = handle.read()
    except OSError as exc:
        read_error = exc

    if read_error is not None:
        synthetic_hash = sha256_bytes(f"unreadable:{item.relative_path}:{item.size_bytes}:{item.mtime_ns}".encode("utf-8"))
        if existing is not None and existing.content_hash == synthetic_hash:
            report.unreadable_files += 1
            report.unchanged_files += 1
            return FileOutcome(
                relative_path=item.relative_path,
                status=FileStatus.UNCHANGED,
                size_bytes=item.size_bytes,
                content_hash=synthetic_hash,
                document_id=existing.id,
                reason="upstream_read_denied_still_unreadable",
                index_state=str(existing.index_state),
            )
        reason = unsupported_reason(FileKind.UNREADABLE, size_bytes=item.size_bytes, max_bytes=policy.max_bytes)
        reason = f"{reason} ({read_error.__class__.__name__}: {read_error})"
        _emit_skip_event(
            store,
            relative_path=item.relative_path,
            reason=reason,
            size_bytes=item.size_bytes,
            content_hash=synthetic_hash,
            document_id=existing.id if existing else None,
        )
        report.unreadable_files += 1
        report.skipped_files += 1
        return FileOutcome(
            relative_path=item.relative_path,
            status=FileStatus.SKIPPED_UNREADABLE,
            size_bytes=item.size_bytes,
            content_hash=synthetic_hash,
            document_id=existing.id if existing else None,
            reason=reason,
            error_class=read_error.__class__.__name__,
        )

    content_hash = sha256_bytes(raw)

    # ---- 未变化：什么都不写（幂等的关键）----
    if existing is not None and existing.content_hash == content_hash:
        report.unchanged_files += 1
        return FileOutcome(
            relative_path=item.relative_path,
            status=FileStatus.UNCHANGED,
            size_bytes=item.size_bytes,
            content_hash=content_hash,
            document_id=existing.id,
            reason="mtime+size+sha256 与库内一致",
            index_state=str(existing.index_state),
        )

    kind = classify_bytes(raw, max_bytes=policy.max_bytes)
    if kind is not FileKind.TEXT:
        reason = unsupported_reason(kind, size_bytes=item.size_bytes, max_bytes=policy.max_bytes)
        _emit_skip_event(
            store,
            relative_path=item.relative_path,
            reason=reason,
            size_bytes=item.size_bytes,
            content_hash=content_hash,
            document_id=existing.id if existing else None,
        )
        report.skipped_files += 1
        return FileOutcome(
            relative_path=item.relative_path,
            status=FileStatus.SKIPPED,
            size_bytes=item.size_bytes,
            content_hash=content_hash,
            document_id=existing.id if existing else None,
            reason=reason,
        )

    decoded = decode_text(raw)
    if decoded is None:
        reason = "undecodable_text: " + ", ".join(("utf-8-sig", "utf-8", "gb18030")) + " 全部解码失败"
        _emit_skip_event(
            store,
            relative_path=item.relative_path,
            reason=reason,
            size_bytes=item.size_bytes,
            content_hash=content_hash,
            document_id=existing.id if existing else None,
        )
        report.skipped_files += 1
        return FileOutcome(
            relative_path=item.relative_path,
            status=FileStatus.SKIPPED,
            size_bytes=item.size_bytes,
            content_hash=content_hash,
            document_id=existing.id if existing else None,
            reason=reason,
        )

    index_text, truncated = extract_index_text(decoded.text, max_chars=policy.max_chars)
    note_parts = [f"codec={decoded.codec}"]
    if truncated:
        note_parts.append(f"truncated_at_chars={policy.max_chars}")

    if policy.raw_sink is not None:
        try:
            policy.raw_sink(item.absolute_path, item.relative_path, content_hash, raw)
        except Exception as exc:  # pragma: no cover - 下游快照失败不能阻断原始资料入库
            report.errors.append({"path": item.absolute_path, "reason": f"raw_sink_failed:{exc.__class__.__name__}:{exc}"})

    content_version = (existing.content_version + 1) if existing is not None else 1
    document = SourceDocument(
        id=document_id,
        source_type=_source_type_for(item.relative_path),
        source_system=policy.source_system,
        source_id=item.relative_path,
        identity_key=identity,
        origin_path=item.absolute_path,
        mime_type=_mime_for(item.relative_path),
        size_bytes=item.size_bytes,
        content_hash=content_hash,
        content_version=content_version,
        received_at=_mtime_iso(item.mtime_ns),
        readable=True,
        index_state=IndexState.STALE if existing is not None else IndexState.PENDING,
        links={"ingest": [item.root]},
    )
    reason = "采集新资料" if existing is None else f"内容变化：content_version={content_version}"
    store.put(
        document,
        expected_revision=existing.revision if existing is not None else None,
        unique=(SOURCE_NAMESPACE, identity),
        actor_kind=ActorKind.SYSTEM,
        actor_id="ingest",
        reason=reason,
        audit_action=AUDIT_COLLECT,
        audit_detail={
            "relative_path": item.relative_path,
            "absolute_path": item.absolute_path,
            "size_bytes": item.size_bytes,
            "mtime_ns": item.mtime_ns,
            "content_hash": content_hash,
            "content_version": content_version,
            "decode": ",".join(note_parts),
            "index_text_chars": len(index_text),
            "normalized_chars": len(normalize_for_index(index_text)),
            "previous_content_hash": existing.content_hash if existing is not None else None,
        },
    )

    store.publish_event(
        _event(
            EVENT_CONTENT_CHANGED if existing is not None else "CONTENT_ADDED",
            source_event_id=f"content:{item.relative_path}:{content_hash}",
            document_id=document_id,
            case_id=None,
            payload={
                "relative_path": item.relative_path,
                "content_hash": content_hash,
                "content_version": content_version,
                "size_bytes": item.size_bytes,
                "index_state": str(document.index_state),
            },
        )
    )

    if existing is None:
        report.new_documents += 1
    else:
        report.updated_documents += 1
    return FileOutcome(
        relative_path=item.relative_path,
        status=FileStatus.INGESTED,
        size_bytes=item.size_bytes,
        content_hash=content_hash,
        document_id=document_id,
        reason=reason,
        index_state=str(document.index_state),
    )


def _mark_missing_stale(
    store: StoreProtocol,
    report: CollectionReport,
    seen_now: set,
    policy: CollectionPolicy,
    *,
    roots: Sequence[str],
) -> None:
    """把"上次在库里、这次不在扫描结果里"的对象标记 STALE（删除/改名/移出白名单）。

    只处理**位于本次扫描根之内**的对象：扫描根之外的资料不属于本次采集的范围，
    不能被误判为"源文件消失"。
    """
    ignore = {name.casefold() for name in policy.ignore_names}
    if policy.suffixes is not None:
        allowed = tuple(suffix.lower() for suffix in policy.suffixes)
    else:
        allowed = ()
    known = store.find("SourceDocument", where={"source_system": policy.source_system}, limit=100000)
    for document in known:
        if not isinstance(document, SourceDocument):  # pragma: no cover - 类型守卫
            continue
        relative = normalize_relative(document.source_id or document.id)
        if relative in seen_now:
            continue
        if not any(_under_root(document.origin_path, root) for root in roots):
            continue
        if any(part.casefold() in ignore for part in relative.split("/")):
            continue
        if allowed and not relative.lower().endswith(allowed):
            continue
        if str(document.index_state) == str(IndexState.STALE):
            # 已经是 STALE：不再重复写库（重复扫描必须幂等）
            continue
        try:
            updated = store.set_index_state(
                document.id,
                str(IndexState.STALE),
                expected_revision=document.revision,
                reason="源文件已不存在（删除/改名/移出白名单），索引标记为待重算",
                error=None,
            )
        except Exception as exc:  # pragma: no cover - 状态机拒绝时记录而不是中断整批
            report.errors.append({"path": document.id, "reason": f"mark_stale_failed:{exc.__class__.__name__}:{exc}"})
            continue
        report.stale_documents += 1
        report.transitioned_documents += 1
        report.outcomes.append(
            FileOutcome(
                relative_path=relative,
                status=FileStatus.TRANSITIONED,
                size_bytes=document.size_bytes,
                content_hash=document.content_hash,
                document_id=document.id,
                reason=f"源文件缺失 → {updated.index_state}",
                index_state=str(updated.index_state),
            )
        )
        store.publish_event(
            _event(
                EVENT_CONTENT_REMOVED,
                source_event_id=f"removed:{relative}:{document.content_hash}",
                document_id=document.id,
                case_id=None,
                payload={
                    "relative_path": relative,
                    "content_hash": document.content_hash,
                    "index_state": str(updated.index_state),
                },
            )
        )


def _under_root(origin_path: str, root: str) -> bool:
    """对象原始路径是否位于某个扫描根之内（用真实路径比较，避免链接绕过）。"""
    if not origin_path:
        return False
    try:
        real_path = os.path.realpath(origin_path)
        real_root = os.path.realpath(root)
        common = os.path.commonpath([real_path, real_root])
    except ValueError:  # 不同驱动器
        return False
    return os.path.normcase(common) == os.path.normcase(real_root)


def _source_type_for(relative_path: str) -> str:
    lowered = relative_path.lower()
    if lowered.endswith((".md", ".markdown")):
        return "markdown"
    return "user_file"


def _mime_for(relative_path: str) -> str:
    lowered = relative_path.lower()
    if lowered.endswith((".md", ".markdown")):
        return "text/markdown"
    if lowered.endswith((".yaml", ".yml")):
        return "text/yaml"
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith(".csv"):
        return "text/csv"
    return "text/plain"


__all__ = [
    "CollectionPolicy",
    "CollectionReport",
    "FileOutcome",
    "FileStatus",
    "SOURCE_NAMESPACE",
    "source_document_id",
    "source_identity_key",
    "collect_directory",
    "AUDIT_COLLECT",
    "AUDIT_COLLECT_SKIP",
    "EVENT_CONTENT_CHANGED",
    "EVENT_CONTENT_SKIPPED",
    "EVENT_CONTENT_REMOVED",
]
