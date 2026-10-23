"""邮件同步循环：游标恢复、幂等去重、失败状态化（SPEC §7.1–§7.3、§21.1、§21.2、AT-05）。

职责边界
========

本模块是「接收侧」的编排层，只依赖两个冻结契约：

- `asistent.contracts.mail.MailSource`（列邮箱 / 取新邮件元数据 / 取原始邮件）；
- `asistent.contracts.store_api.StoreProtocol`（共享对象 + 事件 + 审计，构造注入）。

它不做任何模型调用、不解析业务语义、不发信（发送在 MVP-5，本模块没有发送路径）。

四条硬性行为
============

1. **游标持久化与断点续传**：游标与身份索引写入 `SyncStateStore`（默认为 0600 的
   JSON 文件，原子替换）。只有整批邮件都到达终态（成功或已隔离）才推进游标；
   否则保留旧游标，下一轮重取同一批 —— 重取靠身份键去重，不会重复入库。
2. **游标失效可检测、可重建**：状态文件损坏、来源抛出 `CursorInvalid`、
   来源游标回退（`cursor_position` 钩子）都会被识别，记一条 FAILED 审计并
   **从头重建**（epoch +1），重建过程依然靠身份键去重，不产生第二份原始对象。
3. **失败写成状态而不是静默吞掉**：每次失败都写审计（result=FAILED + failure_class）
   并写进状态文件的 failures/identities，可在 `engine.failures()` / `engine.status()` 查询；
   同一封邮件连续失败达到 `max_attempts` 后标记 QUARANTINED（终态），不再阻塞整批。
4. **可重入幂等**：一封邮件的所有对象 ID 都由身份键确定性派生，
   并且写入时带 `unique=("mail_identity", identity_key)` /
   `unique=("mail_case", identity_key)` / `unique=("mail_thread", thread_key)`。
   因此重复投递、崩溃后重放、游标重建都只会得到
   **一个 SourceDocument + 一个默认 Case**（外加一个线程 Conversation）。

对象 ID 的确定性派生（本模块的实现决策，不改契约）
================================================

`new_id()` 支持 `token`（契约里就是为确定性测试准备的），本模块用
「身份键/线程键的摘要」作为 token。收益是崩溃重放时不需要任何额外索引就能收敛：
已经写入的对象按同一个 ID 命中；即使 Crash 发生在「文档已写、Case 未写」之间，
重放也会用同一个 case_id 补建，而不会产生孤儿。

已知限制见 `docs/mvp/mvp-2.md`。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ..contracts.enums import ActorKind, ArchiveState, AuditAction, FailureClass, IndexState
from ..contracts.errors import AsistentError, DuplicateObject, NotFound, ValidationError
from ..contracts.ids import digest_of, new_id, sha256_bytes
from ..contracts.mail import MailMessageRef, MailSource
from ..contracts.objects import AuditEvent, Case, Conversation, Event, SourceDocument
from ..contracts.store_api import StoreProtocol
from ..contracts.timeutil import utcnow_iso
from .errors import (
    ChannelUnavailable,
    CursorInvalid,
    CursorRegression,
    MailChannelError,
    MailContentError,
    RawContentMismatch,
)

__all__ = [
    "SyncFailure",
    "SyncReport",
    "RawStore",
    "SyncStateStore",
    "MailSyncEngine",
    "normalize_received_at",
    "safe_text",
    "STATE_VERSION",
]

STATE_VERSION = 1
#: 身份索引里表示「已隔离，不再重试，但必须可查询」的状态
IDENTITY_DONE = "DONE"
IDENTITY_FAILED = "FAILED"
IDENTITY_QUARANTINED = "QUARANTINED"

_SURROGATE_SAFE_LIMIT = 1000


def safe_text(value: Any, *, limit: int = _SURROGATE_SAFE_LIMIT) -> str:
    """把任意文本变成「可安全写入 JSON / 审计 / 状态文件」的字符串。

    异常编码的邮件（未知 charset、损坏的 RFC 2047 头部）在 `email` 解析后可能带上
    代理区字符（surrogates）；直接 `json.dumps(..., ensure_ascii=False)` 会在
    编码到 UTF-8 时抛 `UnicodeEncodeError`。这里统一替换掉不可编码的字符。
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if any("\ud800" <= ch <= "\udfff" for ch in text):
        text = text.encode("utf-8", "replace").decode("utf-8", "replace")
    text = text.replace("\x00", "")
    if limit and len(text) > limit:
        text = text[:limit]
    return text


def parse_moment(raw: Any) -> Optional[datetime]:
    """宽容时间解析：同时接受 RFC 2822（IMAP 原文 Date 头）与 ISO-8601（MCP 元数据）。

    两种来源的格式不同（`imap_raw_to_ref` 与 `McpMailSource` 各自给 `received_at`），
    统一在这里解析，避免下游因为格式差异把「同一封邮件」判成「更新的邮件」。
    Python 3.10 的 `fromisoformat` 不接受结尾 `Z`，需要先换成 `+00:00`。
    """
    text = safe_text(raw, limit=200).strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        pass
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    for probe in (candidate, candidate.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(probe)
        except (TypeError, ValueError):
            continue
    return None


def normalize_received_at(raw: str, *, fallback: str) -> str:
    """邮件时间归一化（SPEC §21.1：保留原文 + 归一化时间）。

    解析失败**不**抛异常：邮件头可能是非法日期，此时退回 `fallback`（处理时刻），
    原始文本由调用方放进事件 payload 供追溯。
    """
    moment = parse_moment(raw)
    if moment is not None:
        try:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return fallback


def is_later_moment(left: Optional[str], right: Optional[str]) -> bool:
    """`left` 是否晚于 `right`；无法解析时退回字符串比较（不抛异常）。"""
    if not left:
        return False
    if not right:
        return True
    left_moment, right_moment = parse_moment(left), parse_moment(right)
    if left_moment is None or right_moment is None:
        return str(left) > str(right)
    if left_moment.tzinfo is None:
        left_moment = left_moment.replace(tzinfo=timezone.utc)
    if right_moment.tzinfo is None:
        right_moment = right_moment.replace(tzinfo=timezone.utc)
    return left_moment > right_moment


# --------------------------------------------------------------------------------------
# 原始内容存储（写一次，绝不覆盖）
# --------------------------------------------------------------------------------------


class RawStore:
    """原始邮件字节的落盘存储。

    布局：`<root>/<account>/<identity_digest[:32]>/g<generation>-<raw_digest[:16]>.eml`

    - 文件名里带内容摘要：同身份内容变化会得到**新文件**（新代次），旧原文永不被覆盖
      （SPEC §5.1「原始内容不得被模型输出覆盖」）；
    - 重复投递命中同一路径时校验摘要，一致则复用，不一致直接 `RawContentMismatch`。
    """

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)

    def identity_dir(self, account: str, identity_key: str) -> str:
        return os.path.join(self.root, _account_slug(account), digest_of(identity_key)[:32])

    def path_for(self, account: str, identity_key: str, generation: int, raw_digest: str) -> str:
        name = "g%d-%s.eml" % (int(generation), raw_digest[:16])
        return os.path.join(self.identity_dir(account, identity_key), name)

    def store(self, *, account: str, identity_key: str, generation: int, raw: bytes) -> Tuple[str, bool]:
        """写入原始内容。返回 `(path, created)`；已存在且摘要一致则 `created=False`。"""
        if not raw:
            raise MailContentError("原始邮件内容为空，拒绝入库", detail={"identity_key": identity_key})
        digest = sha256_bytes(raw)
        path = self.path_for(account, identity_key, generation, digest)
        directory = os.path.dirname(path)
        _ensure_dir(directory)
        if os.path.exists(path):
            with open(path, "rb") as handle:
                existing = handle.read()
            if sha256_bytes(existing) == digest:
                return path, False
            raise RawContentMismatch(
                "同一声明摘要下的原始内容不一致，拒绝覆盖",
                detail={"path": path, "expected": digest, "actual": sha256_bytes(existing)},
            )
        _atomic_write_bytes(path, raw)
        return path, True

    def find(self, *, account: str, identity_key: str, generation: int = 1) -> Optional[str]:
        """按身份 + 代次找已保存的原文（不写任何东西）。"""
        directory = self.identity_dir(account, identity_key)
        prefix = "g%d-" % int(generation)
        if not os.path.isdir(directory):
            return None
        for name in sorted(os.listdir(directory)):
            if name.startswith(prefix) and name.endswith(".eml"):
                return os.path.join(directory, name)
        return None

    def read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()


def _account_slug(account: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in account)
    return slug[:48] or "account"


def _ensure_dir(path: str) -> None:
    if not os.path.isdir(path):
        os.makedirs(path, mode=0o700, exist_ok=True)


def _atomic_write_bytes(path: str, data: bytes) -> None:
    directory = os.path.dirname(path)
    _ensure_dir(directory)
    tmp = path + ".tmp-%d" % os.getpid()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    handle = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        _silent_remove(tmp)
        raise
    os.replace(tmp, path)


def _atomic_write_text(path: str, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows / 只读文件系统
        pass


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# 同步状态（游标 + 身份索引 + 失败记录）
# --------------------------------------------------------------------------------------


class SyncStateStore:
    """同步状态：游标、身份索引、失败记录、最近报告。

    `path=None` 时是纯内存实现（测试用）；给定路径时是原子替换的 JSON 文件，
    权限 0600，且**只存元数据**（不含邮件正文、不含任何凭据）。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = os.path.abspath(path) if path else None
        self.load_error: Optional[str] = None
        self.corrupted = False
        self._lock = threading.RLock()
        self._state = self._blank()
        self._loaded = False

    # ---------------------------------------------------------------- 载入/保存
    @staticmethod
    def _blank() -> Dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "runs": 0,
            "cursors": {},
            "identities": {},
            "failures": {},
            "last_report": None,
            "recent_runs": [],
        }

    def load(self) -> Dict[str, Any]:
        with self._lock:
            self.load_error = None
            self.corrupted = False
            if not self.path or not os.path.exists(self.path):
                self._state = self._blank()
                self._loaded = True
                return self.snapshot(force=False)
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (ValueError, OSError, UnicodeDecodeError) as exc:
                self.load_error = "%s: %s" % (type(exc).__name__, exc)
                self.corrupted = True
                self._state = self._blank()
                self._loaded = True
                return self.snapshot(force=False)
            if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
                self.load_error = "状态文件版本不可识别：%r" % (
                    data.get("version") if isinstance(data, dict) else type(data).__name__,
                )
                self.corrupted = True
                self._state = self._blank()
                self._loaded = True
                return self.snapshot(force=False)
            blank = self._blank()
            for key in blank:
                if key not in data or not isinstance(data[key], type(blank[key])) or data[key] is None:
                    if blank[key] is not None:
                        data[key] = blank[key]
            self._state = data
            self._loaded = True
            return self.snapshot()

    def save(self) -> None:
        with self._lock:
            if self.path is None:
                return
            payload = json.dumps(self._state, ensure_ascii=False, sort_keys=True, indent=1)
            _atomic_write_text(self.path, payload)

    def snapshot(self, *, force: bool = True) -> Dict[str, Any]:
        if force and not self._loaded:
            return self.load()
        with self._lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False, default=str))

    # ---------------------------------------------------------------- 游标
    def cursor_for(self, account: str, folder: str) -> Optional[str]:
        entry = self._state["cursors"].get(self.cursor_key(account, folder))
        if not entry:
            return None
        return entry.get("cursor")

    def epoch_for(self, account: str, folder: str) -> int:
        entry = self._state["cursors"].get(self.cursor_key(account, folder)) or {}
        return int(entry.get("epoch") or 0)

    def bump_epoch(self, account: str, folder: str, *, reason: str = "") -> int:
        with self._lock:
            key = self.cursor_key(account, folder)
            entry = self._state["cursors"].setdefault(key, {"cursor": None, "epoch": 0})
            entry["epoch"] = int(entry.get("epoch") or 0) + 1
            entry["cursor"] = None
            entry["invalidated_reason"] = safe_text(reason, limit=500)
            entry["invalidated_at"] = utcnow_iso()
            return int(entry["epoch"])

    def set_cursor(self, account: str, folder: str, cursor: str, *, epoch: Optional[int] = None) -> None:
        with self._lock:
            key = self.cursor_key(account, folder)
            entry = self._state["cursors"].setdefault(key, {"cursor": None, "epoch": 0})
            entry["cursor"] = cursor
            entry["advanced_at"] = utcnow_iso()
            if epoch is not None:
                entry["epoch"] = int(epoch)
            self.save()

    @staticmethod
    def cursor_key(account: str, folder: str) -> str:
        return "%s|%s" % (safe_text(account, limit=200), safe_text(folder, limit=200))

    # ---------------------------------------------------------------- 身份索引
    def identity(self, identity_key: str) -> Optional[Dict[str, Any]]:
        return self._state["identities"].get(identity_key)

    def mark_done(
        self,
        identity_key: str,
        *,
        document_id: str = "",
        case_id: str = "",
        conversation_id: Optional[str] = None,
        uid: str = "",
        folder: str = "",
        message_id: Optional[str] = None,
        content_hash: str = "",
        outcome: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            entry = self._state["identities"].setdefault(identity_key, {})
            entry.update(
                {
                    "state": IDENTITY_DONE,
                    "attempts": int(entry.get("attempts") or 0),
                    "document_id": document_id,
                    "case_id": case_id,
                    "conversation_id": conversation_id,
                    "uid": safe_text(uid, limit=200),
                    "folder": safe_text(folder, limit=200),
                    "message_id": safe_text(message_id, limit=500) if message_id else None,
                    "content_hash": content_hash,
                    "last_outcome": outcome,
                    "synced_at": utcnow_iso(),
                    "failure_class": None,
                    "error": None,
                }
            )
            self._state["failures"].pop(identity_key, None)
            return dict(entry)

    def increment_attempts(self, identity_key: str) -> int:
        with self._lock:
            entry = self._state["identities"].setdefault(identity_key, {})
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            return int(entry["attempts"])

    def record_failure(self, identity_key: str, record: Dict[str, Any], *, attempts: int, terminal: bool) -> None:
        with self._lock:
            entry = self._state["identities"].setdefault(identity_key, {})
            entry["state"] = IDENTITY_QUARANTINED if terminal else IDENTITY_FAILED
            entry["attempts"] = int(attempts)
            entry["last_failure"] = record
            entry["failure_class"] = record.get("failure_class")
            entry["error"] = record.get("message")
            entry["failed_at"] = record.get("occurred_at")
            self._state["failures"][identity_key] = record

    def failures(self) -> List[Dict[str, Any]]:
        rows = [dict(value) for value in self._state["failures"].values()]
        rows.sort(key=lambda item: str(item.get("occurred_at") or ""))
        return rows

    def quarantined(self) -> List[str]:
        return [
            key
            for key, value in self._state["identities"].items()
            if value.get("state") == IDENTITY_QUARANTINED
        ]

    def release(self, identity_key: str) -> bool:
        """解除隔离（人工运维）：允许下一轮重新取该邮件。"""
        with self._lock:
            entry = self._state["identities"].get(identity_key)
            if not entry or entry.get("state") != IDENTITY_QUARANTINED:
                return False
            entry["state"] = IDENTITY_FAILED
            entry["attempts"] = 0
            entry["released_at"] = utcnow_iso()
            self._state["failures"].pop(identity_key, None)
            self.save()
            return True

    def identities(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._state["identities"], ensure_ascii=False, default=str))

    def cursors(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._state["cursors"], ensure_ascii=False, default=str))

    # ---------------------------------------------------------------- 运行报告
    def record_run(self, report: "SyncReport", *, keep: int = 20) -> None:
        with self._lock:
            self._state["runs"] = int(self._state.get("runs") or 0) + 1
            self._state["last_report"] = report.to_dict()
            recent = self._state.setdefault("recent_runs", [])
            recent.append(report.to_dict())
            del recent[:-keep]
            self.save()


# --------------------------------------------------------------------------------------
# 报告与失败记录
# --------------------------------------------------------------------------------------


@dataclass
class SyncFailure:
    """一封邮件/一次列取失败的可查询记录。"""

    stage: str
    error_code: str
    failure_class: str
    message: str
    identity_key: str = ""
    uid: str = ""
    attempt: int = 1
    terminal: bool = False
    occurred_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "error_code": self.error_code,
            "failure_class": self.failure_class,
            "message": self.message,
            "identity_key": self.identity_key,
            "uid": self.uid,
            "attempt": self.attempt,
            "terminal": self.terminal,
            "occurred_at": self.occurred_at,
        }


@dataclass
class SyncReport:
    """一次同步运行的可观察结果（不写正文、不写凭据）。"""

    account: str
    folder: str
    worker: str = "mail-sync"
    started_at: str = ""
    finished_at: str = ""
    cursor_before: Optional[str] = None
    cursor_after: Optional[str] = None
    cursor_changed: bool = False
    cursor_invalidated: bool = False
    invalid_reason: str = ""
    epoch: int = 0
    list_attempts: int = 0
    listed: int = 0
    documents_created: int = 0
    documents_updated: int = 0
    cases_created: int = 0
    conversations_created: int = 0
    conversations_updated: int = 0
    duplicates_skipped: int = 0
    events_published: int = 0
    events_duplicate: int = 0
    quarantined: List[str] = field(default_factory=list)
    failures: List[SyncFailure] = field(default_factory=list)
    aborted_reason: Optional[str] = None
    complete: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account": self.account,
            "folder": self.folder,
            "worker": self.worker,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            "cursor_changed": self.cursor_changed,
            "cursor_invalidated": self.cursor_invalidated,
            "invalid_reason": self.invalid_reason,
            "epoch": self.epoch,
            "list_attempts": self.list_attempts,
            "listed": self.listed,
            "documents_created": self.documents_created,
            "documents_updated": self.documents_updated,
            "cases_created": self.cases_created,
            "conversations_created": self.conversations_created,
            "conversations_updated": self.conversations_updated,
            "duplicates_skipped": self.duplicates_skipped,
            "events_published": self.events_published,
            "events_duplicate": self.events_duplicate,
            "quarantined": list(self.quarantined),
            "failures": [item.to_dict() for item in self.failures],
            "aborted_reason": self.aborted_reason,
            "complete": self.complete,
        }

    def summary_line(self) -> str:
        return (
            "sync account=%s folder=%s listed=%d new_doc=%d dup=%d cases=%d events=%d "
            "failures=%d quarantined=%d cursor_changed=%s rebuilt=%s complete=%s"
            % (
                self.account,
                self.folder,
                self.listed,
                self.documents_created,
                self.duplicates_skipped,
                self.cases_created,
                self.events_published,
                len(self.failures),
                len(self.quarantined),
                self.cursor_changed,
                self.cursor_invalidated,
                self.complete,
            )
        )


# --------------------------------------------------------------------------------------
# 同步引擎
# --------------------------------------------------------------------------------------


class MailSyncEngine:
    """把 `MailSource` 的新邮件落到共享对象里的幂等同步器。"""

    def __init__(
        self,
        store: StoreProtocol,
        source: MailSource,
        *,
        account: str,
        state_store: SyncStateStore,
        raw_store: RawStore,
        folder: str = "INBOX",
        worker: str = "mail-sync",
        max_attempts: int = 3,
        create_conversation: bool = True,
        source_system: Optional[str] = None,
        redact: Optional[Callable[[str], str]] = None,
    ) -> None:
        if not account:
            raise ValidationError("邮件同步必须指定账号（身份按账号作用域隔离）")
        if max_attempts < 1:
            raise ValidationError("max_attempts 必须 >= 1")
        self.store = store
        self.source = source
        self.account = account
        self.folder = folder
        self.worker = worker
        self.max_attempts = int(max_attempts)
        self.create_conversation = bool(create_conversation)
        self.state = state_store
        self.engine_state = self.state  # 兼容别名（避免与 store 状态混淆）
        self.raw_store = raw_store
        self.source_system = source_system or "%s:%s" % (getattr(source, "name", "source"), account)
        self._redact = redact or (lambda text: text)
        self.state.load()

    # ------------------------------------------------------------------ 公开接口
    def sync_once(self, *, rebuild: bool = False, reason: str = "") -> SyncReport:
        """跑一轮同步。可重入：没有新邮件时不会产生任何写入。"""
        report = SyncReport(
            account=self.account,
            folder=self.folder,
            worker=self.worker,
            started_at=utcnow_iso(),
        )
        cursor = self.state.cursor_for(self.account, self.folder)
        report.cursor_before = cursor
        report.epoch = self.state.epoch_for(self.account, self.folder)

        invalid_reason = ""
        if self.state.corrupted:
            invalid_reason = "同步状态文件不可解析：%s" % (self.state.load_error or "")
        elif rebuild:
            invalid_reason = reason or "调用方显式要求重建游标"

        if invalid_reason:
            cursor = self._invalidate(report, invalid_reason, cursor)

        listed = self._list_with_recovery(cursor, report)
        if listed is None:
            report.finished_at = utcnow_iso()
            self.state.record_run(report)
            return report
        refs, new_cursor = listed
        report.listed = len(refs)

        for ref in refs:
            self._process_ref(ref, report)

        non_terminal = [item for item in report.failures if not item.terminal]
        if non_terminal:
            report.complete = False
            report.aborted_reason = "%d 封邮件失败且未达隔离阈值（%d 次），游标不推进" % (
                len(non_terminal),
                self.max_attempts,
            )
            report.cursor_after = report.cursor_before
            report.epoch = self.state.epoch_for(self.account, self.folder)
        elif new_cursor is not None and (
            new_cursor != report.cursor_before or report.cursor_invalidated
        ):
            # 重建之后必须把恢复点写回，即使它与旧值相同（否则下一轮又会重建一次）
            report.cursor_after = new_cursor
            report.cursor_changed = True
            self.state.set_cursor(
                self.account, self.folder, new_cursor, epoch=report.epoch
            )
            self._audit(
                object_type="MailSyncCursor",
                object_id=self.state.cursor_key(self.account, self.folder),
                result="OK",
                reason="游标重建后推进" if report.cursor_invalidated else "游标推进",
                detail={
                    "cursor_before": report.cursor_before,
                    "cursor_after": new_cursor,
                    "epoch": report.epoch,
                    "listed": report.listed,
                    "documents_created": report.documents_created,
                    "duplicates_skipped": report.duplicates_skipped,
                },
            )
        else:
            report.cursor_after = report.cursor_before
            report.epoch = self.state.epoch_for(self.account, self.folder)

        report.finished_at = utcnow_iso()
        self.state.record_run(report)
        return report

    def rebuild_cursor(self, reason: str = "人工要求重建游标") -> SyncReport:
        """显式重建：丢掉游标后从头列取一次（幂等，不产生重复对象）。"""
        return self.sync_once(rebuild=True, reason=reason)

    def status(self) -> Dict[str, Any]:
        """可查询的运行状态（失败、隔离、游标、来源位置、最近一次报告）。"""
        snapshot = self.state.snapshot()
        return {
            "account": self.account,
            "folder": self.folder,
            "worker": self.worker,
            "runs": snapshot.get("runs", 0),
            "cursor": self.state.cursor_for(self.account, self.folder),
            "epoch": self.state.epoch_for(self.account, self.folder),
            "identities": len(snapshot.get("identities", {})),
            "failures": self.state.failures(),
            "quarantined": self.state.quarantined(),
            "state_path": self.state.path,
            "state_corrupted": self.state.corrupted,
            "state_load_error": self.state.load_error,
            "source_position": self.source_position(),
            "last_report": snapshot.get("last_report"),
        }

    def source_position(self) -> Optional[Dict[str, Any]]:
        """契约 v0.4 可选能力（CP-20261010-08）：来源自报的「同步到哪了」。

        来源不提供该能力（或没有信息）时返回 `None`——**不得**假装 0 进度。
        """
        probe = getattr(self.source, "cursor_position", None)
        if not callable(probe):
            return None
        try:
            value = probe()
        except Exception:  # 观测钩子不能拖垮状态查询
            return None
        if not isinstance(value, dict):
            return None
        return {str(key): item for key, item in value.items()}

    def failures(self) -> List[Dict[str, Any]]:
        return self.state.failures()

    def quarantine(self, identity_key: str, *, reason: str = "人工隔离") -> None:
        record = {
            "stage": "manual",
            "error_code": "MANUAL_QUARANTINE",
            "failure_class": str(FailureClass.MISSING_DATA),
            "message": safe_text(reason, limit=500),
            "identity_key": identity_key,
            "uid": "",
            "attempt": self.max_attempts,
            "terminal": True,
            "occurred_at": utcnow_iso(),
        }
        self.state.record_failure(identity_key, record, attempts=self.max_attempts, terminal=True)

    def release_quarantine(self, identity_key: str) -> bool:
        """人工解除隔离（例如服务端异常已修复）：允许下一轮重新取该邮件。"""
        return self.state.release(identity_key)

    # ------------------------------------------------------------------ 列取与恢复
    def _list_with_recovery(
        self, cursor: Optional[str], report: SyncReport
    ) -> Optional[Tuple[List[MailMessageRef], str]]:
        """列取新邮件；最多两次尝试（第二次是从头重建）。"""
        current = cursor
        for attempt in (0, 1):
            report.list_attempts += 1
            try:
                refs, new_cursor = self.source.list_new(current)
            except (CursorInvalid, CursorRegression) as exc:
                if attempt == 1:
                    self._record_list_failure(report, exc)
                    return None
                current = self._invalidate(report, str(exc), current)
                continue
            except AsistentError as exc:
                self._record_list_failure(report, exc)
                return None
            except OSError as exc:
                self._record_list_failure(report, ChannelUnavailable("通道 I/O 失败：%s" % exc))
                return None
            except Exception as exc:  # 通道实现缺陷也必须变成状态，而不是让进程崩掉
                self._record_list_failure(
                    report,
                    ChannelUnavailable("列取新邮件未预期失败：%s: %s" % (type(exc).__name__, exc)),
                )
                return None

            regression = self._detect_regression(current, new_cursor)
            if regression and attempt == 0:
                current = self._invalidate(report, regression, current)
                continue
            return list(refs), new_cursor
        return None

    def _detect_regression(self, old: Optional[str], new: Optional[str]) -> str:
        if old is None or new is None:
            return ""
        # 注意：游标"先后"比较不是契约能力（契约 v0.4 的可选能力是无参 cursor_position()，
        # 用于观测）。这里的数值比较钩子叫 `cursor_rank`，属于实现级的额外能力：
        # 探测不到就跳过回退检测（降级为"不可比较"），而不是假装没有回退。
        rank = getattr(self.source, "cursor_rank", None)
        if not callable(rank):
            return ""
        try:
            old_pos = rank(old)
            new_pos = rank(new)
        except Exception:  # 来源钩子自己出错 → 不作为失效证据
            return ""
        if old_pos is None or new_pos is None:
            return ""
        if new_pos < old_pos:
            return "来源游标回退：%r -> %r" % (old_pos, new_pos)
        return ""

    def _invalidate(self, report: SyncReport, reason: str, cursor: Optional[str]) -> Optional[str]:
        """识别游标失效：记录原因、epoch +1、写审计、返回 None（表示从头列取）。"""
        report.cursor_invalidated = True
        report.invalid_reason = safe_text(reason, limit=500)
        report.epoch = self.state.bump_epoch(
            self.account, self.folder, reason=self._redact_text(reject_credentials(reason))
        )
        self._audit(
            object_type="MailSyncCursor",
            object_id=self.state.cursor_key(self.account, self.folder),
            result="FAILED",
            reason="游标失效，已重建（epoch=%d）：%s" % (report.epoch, report.invalid_reason),
            detail={
                "cursor_before": cursor,
                "epoch": report.epoch,
                "failure_class": str(FailureClass.MISSING_DATA),
            },
        )
        return None

    def _record_list_failure(self, report: SyncReport, exc: BaseException) -> None:
        failure = SyncFailure(
            stage="list",
            error_code=str(getattr(exc, "code", type(exc).__name__)),
            failure_class=str(getattr(exc, "failure_class", FailureClass.TOOL_FAILURE)),
            message=safe_text(self._redact_text(str(exc)), limit=500),
            occurred_at=utcnow_iso(),
            terminal=False,
        )
        report.failures.append(failure)
        report.complete = False
        report.aborted_reason = "列取新邮件失败：%s" % failure.message
        report.cursor_after = report.cursor_before
        self._audit(
            object_type="MailSync",
            object_id=self.state.cursor_key(self.account, self.folder),
            result="FAILED",
            reason="列取新邮件失败：%s" % failure.message,
            detail={
                "stage": "list",
                "error_code": failure.error_code,
                "failure_class": failure.failure_class,
            },
        )

    # ------------------------------------------------------------------ 单封邮件
    def _process_ref(self, ref: MailMessageRef, report: SyncReport) -> None:
        identity = ""
        try:
            identity = ref.identity_key()
        except ValueError as exc:
            self._record_ref_failure(
                report, None, ref, "identity", MailContentError(str(exc)), attempts=1
            )
            return

        known = self.state.identity(identity)
        if known and known.get("state") == IDENTITY_QUARANTINED:
            # 已隔离：不再重复取正文，但保留可查询状态
            report.quarantined.append(identity)
            report.duplicates_skipped += 1
            return

        stage = "fetch"
        try:
            raw = self.source.fetch_raw(ref)
        except AsistentError as exc:
            self._fail_ref(report, identity, ref, "fetch", exc)
            return
        except OSError as exc:
            self._fail_ref(report, identity, ref, "fetch", ChannelUnavailable("取原文 I/O 失败：%s" % exc))
            return
        except Exception as exc:
            self._fail_ref(
                report,
                identity,
                ref,
                "fetch",
                ChannelUnavailable("取原文未预期失败：%s: %s" % (type(exc).__name__, exc)),
            )
            return

        raw_digest = sha256_bytes(raw)
        stage = "document"
        try:
            document, outcome = self._ensure_document(ref, identity, raw, raw_digest)
            stage = "conversation"
            conversation, conv_outcome = self._ensure_conversation(ref, identity, document)
            stage = "case"
            case, case_outcome = self._ensure_case(ref, identity, document, conversation)
            stage = "event"
            event_created = self._publish_event(ref, identity, document, case, conversation)
        except AsistentError as exc:
            self._fail_ref(report, identity, ref, stage, exc)
            return
        except OSError as exc:
            self._fail_ref(report, identity, ref, stage, ChannelUnavailable("落盘失败：%s" % exc))
            return
        except Exception as exc:
            self._fail_ref(
                report,
                identity,
                ref,
                stage,
                ChannelUnavailable("入库未预期失败：%s: %s" % (type(exc).__name__, exc)),
            )
            return

        if outcome == "created":
            report.documents_created += 1
        elif outcome == "updated":
            report.documents_updated += 1
        else:
            report.duplicates_skipped += 1
        if conv_outcome == "created":
            report.conversations_created += 1
        elif conv_outcome == "updated":
            report.conversations_updated += 1
        if case_outcome == "created":
            report.cases_created += 1
        if event_created:
            report.events_published += 1
        else:
            report.events_duplicate += 1

        self.state.mark_done(
            identity,
            document_id=document.id,
            case_id=case.id,
            conversation_id=conversation.id if conversation is not None else None,
            uid=ref.uid,
            folder=ref.folder,
            message_id=ref.message_id,
            content_hash=raw_digest,
            outcome=outcome,
        )

    def _content_origin(self, ref: MailMessageRef) -> Optional[str]:
        """契约 v0.4（CP-20261010-07）：把来源声明的摘要语义显式带到文档上。

        `RAW_BYTES`（IMAP 直连/离线夹具给的是原始字节）与 `METADATA_PROJECTION`
        （MCP 只给元数据，摘要只是投影）必须原样传递；来源没声明（空串）时记
        `None`＝未声明，下游**不得**把它当字节级完整性证明。
        """
        return safe_text(ref.content_origin, limit=64) or None

    def _ensure_document(
        self, ref: MailMessageRef, identity: str, raw: bytes, raw_digest: str
    ) -> Tuple[SourceDocument, str]:
        existing = self.store.find_one("SourceDocument", identity_key=identity)
        if existing is not None:
            doc = existing  # type: ignore[assignment]
            if doc.content_hash == raw_digest:
                current_folder = safe_text(ref.folder, limit=200)
                if current_folder and doc.mailbox_folder and current_folder != doc.mailbox_folder:
                    # 跨文件夹移动（身份靠供应商不可变 ID 保持稳定）：只更新来源位置
                    fresh = self.store.get("SourceDocument", doc.id)  # type: ignore[assignment]
                    fresh.mailbox_folder = current_folder
                    fresh.source_id = self._mailbox_scope_id(ref)
                    fresh.updated_at = utcnow_iso()
                    saved = self.store.put(
                        fresh,
                        expected_revision=fresh.revision,
                        actor_kind=ActorKind.SYSTEM,
                        actor_id=self.worker,
                        reason="邮件跨文件夹移动，更新来源位置（身份不变）",
                        audit_action=str(AuditAction.UPDATE),
                        audit_detail={"from_folder": doc.mailbox_folder, "to_folder": current_folder},
                    )
                    return saved, "updated"  # type: ignore[return-value]
                return doc, "duplicate"
            # 同一身份、内容变化：新代次 + 内容版本 +1，旧原文不覆盖
            generation = int(doc.content_version or 1) + 1
            path, _ = self.raw_store.store(
                account=self.account, identity_key=identity, generation=generation, raw=raw
            )
            fresh = self.store.get("SourceDocument", doc.id)  # type: ignore[assignment]
            fresh.origin_path = path
            fresh.content_hash = raw_digest
            fresh.content_origin = self._content_origin(ref)
            fresh.content_version = generation
            fresh.size_bytes = len(raw)
            fresh.mailbox_folder = safe_text(ref.folder, limit=200) or fresh.mailbox_folder
            fresh.source_id = self._mailbox_scope_id(ref)
            fresh.updated_at = utcnow_iso()
            saved = self.store.put(
                fresh,
                expected_revision=fresh.revision,
                actor_kind=ActorKind.SYSTEM,
                actor_id=self.worker,
                reason="同一邮件身份下内容摘要变化，登记新内容代次",
                audit_action=str(AuditAction.UPDATE),
            )
            return saved, "updated"  # type: ignore[return-value]

        path, _ = self.raw_store.store(
            account=self.account, identity_key=identity, generation=1, raw=raw
        )
        document = SourceDocument(
            id=self._object_id("SourceDocument", identity),
            source_type="email",
            source_system=safe_text(self.source_system, limit=200),
            source_id=self._mailbox_scope_id(ref),
            identity_key=identity,
            origin_path=path,
            mime_type="message/rfc822",
            size_bytes=len(raw),
            content_hash=raw_digest,
            content_origin=self._content_origin(ref),
            content_version=1,
            received_at=self._received_at(ref),
            readable=True,
            index_state=IndexState.PENDING,
            archive_state=ArchiveState.PENDING,
            conversation_id=self._conversation_id(ref) if self.create_conversation else None,
            case_id=self._case_id(identity),
            thread_keys=[safe_text(key, limit=500) for key in ref.thread_keys][:32],
            mailbox_folder=safe_text(ref.folder, limit=200) or None,
            vendor_message_id=safe_text(ref.vendor_message_id, limit=500) or None,
        )
        try:
            saved = self.store.put(
                document,
                unique=("mail_identity", identity),
                actor_kind=ActorKind.SYSTEM,
                actor_id=self.worker,
                reason="同步入库存原始邮件（账号作用域稳定身份）",
                audit_action=str(AuditAction.CREATE),
                audit_detail={"identity_key": identity, "uid": ref.uid, "folder": ref.folder},
            )
            return saved, "created"  # type: ignore[return-value]
        except DuplicateObject as exc:
            found = self._existing_from_duplicate(exc, "SourceDocument")
            return found, "duplicate"  # type: ignore[return-value]

    def _ensure_conversation(
        self, ref: MailMessageRef, identity: str, document: SourceDocument
    ) -> Tuple[Optional[Conversation], str]:
        if not self.create_conversation:
            return None, "skipped"
        thread_key = self._thread_key(ref)
        expected_id = self._object_id("Conversation", thread_key)
        # 邮件自身的时间（不是本地抓取时刻）：重放同一封邮件必须得到同一个值，
        # 否则每次同步都会因为"本地时间更新"改写会话，破坏幂等与审计可比性。
        received = self._message_time(ref)
        conversation: Optional[Conversation] = None
        try:
            existing = self.store.get("Conversation", expected_id)
            conversation = existing  # type: ignore[assignment]
        except NotFound:
            existing = None
        if conversation is None:
            # 也可能存在别人创建的、不同 id 的同线程会话：按 thread_key 查一次
            found = self.store.find_one("Conversation", thread_key=thread_key)
            if found is not None:
                conversation = found  # type: ignore[assignment]

        if conversation is None:
            obj = Conversation(
                id=expected_id,
                channel="email_thread",
                subject=safe_text(ref.subject, limit=500),
                thread_key=thread_key,
                participant_ids=self._participants(ref),
                case_ids=[self._case_id(identity)],
                source_document_ids=[document.id],
                message_count=1,
                last_message_at=received,
            )
            try:
                saved = self.store.put(
                    obj,
                    unique=("mail_thread", thread_key),
                    actor_kind=ActorKind.SYSTEM,
                    actor_id=self.worker,
                    reason="登记邮件线程会话",
                    audit_action=str(AuditAction.CREATE),
                    audit_detail={"thread_key": thread_key},
                )
                return saved, "created"  # type: ignore[return-value]
            except DuplicateObject as exc:
                return self._existing_from_duplicate(exc, "Conversation"), "duplicate"

        changed = False
        if document.id not in conversation.source_document_ids:
            conversation.source_document_ids.append(document.id)
            conversation.message_count = int(conversation.message_count or 0) + 1
            changed = True
        case_id = self._case_id(identity)
        if case_id not in conversation.case_ids:
            conversation.case_ids.append(case_id)
            changed = True
        if not conversation.thread_key:
            conversation.thread_key = thread_key
            changed = True
        if is_later_moment(received, conversation.last_message_at):
            conversation.last_message_at = received
            changed = True
        if not changed:
            return conversation, "duplicate"
        conversation.updated_at = utcnow_iso()
        saved = self.store.put(
            conversation,
            expected_revision=conversation.revision,
            actor_kind=ActorKind.SYSTEM,
            actor_id=self.worker,
            reason="线程新增邮件",
            audit_action=str(AuditAction.UPDATE),
        )
        return saved, "updated"  # type: ignore[return-value]

    def _ensure_case(
        self,
        ref: MailMessageRef,
        identity: str,
        document: SourceDocument,
        conversation: Optional[Conversation],
    ) -> Tuple[Case, str]:
        case_id = self._case_id(identity)
        existing: Optional[Case] = None
        try:
            found = self.store.get("Case", case_id)
            existing = found  # type: ignore[assignment]
        except NotFound:
            existing = None

        if existing is None and document.case_id and document.case_id != case_id:
            case_id = document.case_id
            try:
                found = self.store.get("Case", case_id)
                existing = found  # type: ignore[assignment]
            except NotFound:
                existing = None

        if existing is None:
            case = Case(
                id=case_id,
                trigger_event_id=self._event_id(identity),
                goal=self._goal(ref),
                participants=self._participants(ref),
                source_document_ids=[document.id],
                conversation_ids=[conversation.id] if conversation is not None else [],
                links={"mail_identity": [identity], "source_document": [document.id]},
            )
            try:
                saved = self.store.put(
                    case,
                    unique=("mail_case", identity),
                    actor_kind=ActorKind.SYSTEM,
                    actor_id=self.worker,
                    reason="为一封新邮件创建默认事务",
                    audit_action=str(AuditAction.CREATE),
                    audit_detail={"identity_key": identity, "document_id": document.id},
                )
                return saved, "created"  # type: ignore[return-value]
            except DuplicateObject as exc:
                return self._existing_from_duplicate(exc, "Case"), "duplicate"

        if document.id in existing.source_document_ids:
            return existing, "duplicate"
        existing.source_document_ids.append(document.id)
        if conversation is not None and conversation.id not in existing.conversation_ids:
            existing.conversation_ids.append(conversation.id)
        existing.updated_at = utcnow_iso()
        saved = self.store.put(
            existing,
            expected_revision=existing.revision,
            actor_kind=ActorKind.SYSTEM,
            actor_id=self.worker,
            reason="事务追加来源邮件",
            audit_action=str(AuditAction.UPDATE),
        )
        return saved, "updated"  # type: ignore[return-value]

    def _publish_event(
        self,
        ref: MailMessageRef,
        identity: str,
        document: SourceDocument,
        case: Case,
        conversation: Optional[Conversation],
    ) -> bool:
        moment = utcnow_iso()
        event = Event(
            id=self._event_id(identity),
            kind="MAIL_RECEIVED",
            source="mail:%s" % self.account,
            source_event_id=identity,
            occurred_at=self._received_at(ref),
            received_at=moment,
            case_id=case.id,
            conversation_id=conversation.id if conversation is not None else None,
            payload={
                "identity_key": identity,
                "account": safe_text(self.account, limit=200),
                "folder": safe_text(ref.folder, limit=200),
                "uid": safe_text(ref.uid, limit=200),
                "message_id": safe_text(ref.message_id, limit=500) if ref.message_id else None,
                "vendor_message_id": (
                    safe_text(ref.vendor_message_id, limit=500) if ref.vendor_message_id else None
                ),
                "subject": safe_text(ref.subject, limit=500),
                "from_addr": safe_text(ref.from_addr, limit=500),
                "to_addrs": [safe_text(item, limit=500) for item in ref.to_addrs][:64],
                "received_at_header": safe_text(ref.received_at, limit=200),
                "received_at": self._received_at(ref),
                "size_bytes": int(ref.size_bytes or 0),
                "has_attachments": bool(ref.has_attachments),
                "source_content_digest": safe_text(ref.content_digest, limit=128),
                "raw_content_hash": document.content_hash,
                "document_id": document.id,
                "content_version": int(document.content_version or 1),
            },
        )
        saved, created = self.store.publish_event(event)
        if created and saved.id:
            self.store.claim_event(saved.id, worker=self.worker)
            self.store.mark_event_processed(saved.id, result="PROCESSED")
        return bool(created)

    # ------------------------------------------------------------------ 辅助
    def _fail_ref(
        self,
        report: SyncReport,
        identity: str,
        ref: MailMessageRef,
        stage: str,
        exc: BaseException,
    ) -> None:
        attempts = self.state.increment_attempts(identity)
        # 不可重试的失败（身份不足、内容冲突、摘要不一致）立即隔离：
        # 否则游标永远不推进，整批邮件被一封毒邮件卡死。
        retryable = bool(getattr(exc, "retryable", True))
        terminal = (attempts >= self.max_attempts) or not retryable
        self._record_ref_failure(
            report, identity, ref, stage, exc, attempts=attempts, terminal=terminal
        )

    def _record_ref_failure(
        self,
        report: SyncReport,
        identity: Optional[str],
        ref: MailMessageRef,
        stage: str,
        exc: BaseException,
        *,
        attempts: int = 1,
        terminal: bool = False,
    ) -> None:
        key = identity or "uid:%s:%s" % (safe_text(ref.folder, limit=100), safe_text(ref.uid, limit=100))
        if identity is None:
            terminal = True  # 连身份都算不出来：隔离该封，不要卡住整批
        failure = SyncFailure(
            stage=stage,
            error_code=str(getattr(exc, "code", type(exc).__name__)),
            failure_class=str(getattr(exc, "failure_class", FailureClass.TOOL_FAILURE)),
            message=safe_text(self._redact_text(str(exc)), limit=500),
            identity_key=key,
            uid=safe_text(ref.uid, limit=200),
            attempt=int(attempts),
            terminal=bool(terminal),
            occurred_at=utcnow_iso(),
        )
        report.failures.append(failure)
        if failure.terminal:
            report.quarantined.append(key)
        self.state.record_failure(
            key,
            failure.to_dict(),
            attempts=int(attempts),
            terminal=bool(terminal),
        )
        self._audit(
            object_type="MailSyncFailure",
            object_id=key,
            result="FAILED",
            reason="邮件同步失败（stage=%s，第 %d 次）：%s" % (stage, attempts, failure.message),
            detail={
                "stage": stage,
                "error_code": failure.error_code,
                "failure_class": failure.failure_class,
                "attempts": int(attempts),
                "terminal": bool(terminal),
                "uid": failure.uid,
                "folder": safe_text(ref.folder, limit=200),
            },
        )

    def _existing_from_duplicate(self, exc: DuplicateObject, default_type: str) -> Any:
        detail = exc.detail or {}
        existing_id = detail.get("existing_id")
        existing_type = detail.get("existing_type") or default_type
        if not existing_id:
            raise exc
        return self.store.get(existing_type, existing_id)

    def _audit(
        self,
        *,
        object_type: str,
        object_id: str,
        result: str,
        reason: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        event = AuditEvent(
            at=utcnow_iso(),
            action=str(AuditAction.WRITE),
            actor_kind=ActorKind.SYSTEM,
            actor_id=self.worker,
            object_type=object_type,
            object_id=object_id,
            result=result,
            reason=safe_text(self._redact_text(reason), limit=1000),
            detail=_jsonable(detail or {}),
        )
        self.store.append_audit(event)

    def _redact_text(self, text: str) -> str:
        try:
            return self._redact(text)
        except Exception:  # 脱敏器不能反过来让同步崩掉
            return text

    @staticmethod
    def _object_id(object_type: str, seed: str) -> str:
        return new_id(object_type, token=digest_of(["mvp2-mail", object_type, seed])[:24])

    def _case_id(self, identity: str) -> str:
        return self._object_id("Case", identity)

    def _event_id(self, identity: str) -> str:
        return self._object_id("Event", "mail_received:" + identity)

    def _conversation_id(self, ref: MailMessageRef) -> str:
        return self._object_id("Conversation", self._thread_key(ref))

    def _thread_key(self, ref: MailMessageRef) -> str:
        """线程键必须账号作用域化：不同账号的同名 Message-ID 绝不合并（SPEC §21.2）。"""
        thread = ""
        for candidate in ref.thread_keys:
            if candidate:
                thread = safe_text(candidate, limit=500)
                break
        if not thread:
            # 没有 Message-ID：退化为「该封邮件自己的线程」，不猜别人的线程
            thread = "uid:%s:%s" % (safe_text(ref.folder, limit=100), safe_text(ref.uid, limit=100))
        return "%s|%s" % (safe_text(self.account, limit=200), thread)

    @staticmethod
    def _mailbox_scope_id(ref: MailMessageRef) -> str:
        return "%s:%s" % (safe_text(ref.folder, limit=200), safe_text(ref.uid, limit=200))

    def _received_at(self, ref: MailMessageRef) -> str:
        return normalize_received_at(ref.received_at, fallback=utcnow_iso())

    def _message_time(self, ref: MailMessageRef) -> str:
        """邮件时间戳，**重放稳定**：解析不出日期时才退回处理时刻。"""
        raw = getattr(ref, "received_at", None) or ""
        return normalize_received_at(raw, fallback=self._received_at(ref))

    def _goal(self, ref: MailMessageRef) -> str:
        subject = safe_text(ref.subject, limit=200).strip()
        if subject:
            return "处理邮件：%s" % subject
        return "处理一封新邮件（无主题）"

    def _participants(self, ref: MailMessageRef) -> List[str]:
        people: List[str] = []
        for value in [ref.from_addr] + list(ref.to_addrs):
            text = safe_text(value, limit=500).strip()
            if text and text not in people:
                people.append(text)
        return people[:32]


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {safe_text(key, limit=200): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, str):
        return safe_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return safe_text(str(value))


def reject_credentials(text: str) -> str:
    """保守兜底：任何看起来像 `key=value` 的凭据片段都不进审计原因。

    同步层不做凭据处理（凭据只存在于 MCP 子进程自己的配置里），
    这里只保证异常消息不会被原样带进审计。
    """
    lowered = text.lower()
    for marker in ("password", "passwd", "secret", "token", "bearer ", "api_key", "apikey"):
        if marker in lowered:
            return "（已脱敏：异常消息疑似包含凭据）"
    return text
