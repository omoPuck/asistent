"""健康/状态快照（SPEC §11.4、§21.7 第 1063 行、AT-01）。

这一份快照要回答手机端最关心的四个问题（不打开电脑、不看日志也答得出来）：

| 问题 | 字段 |
|---|---|
| 服务还活着吗？最后一次干活是什么时候？ | `rounds_completed` / `last_round_at` / `interval_seconds` |
| 邮件同步到哪了？成功了吗？ | `last_sync.*` |
| 有多少东西在等我？ | `pending.*` |
| **正在等什么**（等我回答/等我审批/等网盘/结果未知）？ | `waiting_on[]` |

两条纪律：

* **计数现场从存储算**：`pending` 由 `snapshot_pending(store)` 现查 `StoreProtocol` 得出，
  不是进程里累加出来的计数器 —— 所以"重启后看到的数字"和"崩溃前"是同一套口径；
* **快照只写仓库外的 state 目录**（原子替换），内容里**没有** token、密码、授权码、
  邮件正文；只有 id、状态、计数与错误文本。

`waiting_on` 的取值是固定的四个字符串，控制台/手机可按它渲染成"等你/等审批/等网盘/结果未知"。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..contracts.enums import (
    ActionState,
    ApprovalState,
    ArchiveState,
    CaseState,
    DraftState,
    QuestionState,
    TaskState,
)
from ..contracts.timeutil import utcnow_iso

#: `waiting_on` 的合法取值（顺序即展示优先级）
WAITING_USER = "user"
WAITING_APPROVAL = "approval"
WAITING_PAN = "pan"
WAITING_UNKNOWN = "action_unknown"
WAITING_CHANNEL = "channel"
WAITING_NONE = "none"

WAITING_ORDER = (WAITING_USER, WAITING_APPROVAL, WAITING_UNKNOWN, WAITING_CHANNEL, WAITING_PAN, WAITING_NONE)

#: 非终态 Case 状态（"还在等"的事务）
_OPEN_CASE_STATES = tuple(
    str(item)
    for item in (
        CaseState.NEW,
        CaseState.TRIAGED,
        CaseState.WAITING_FOR_CONTEXT,
        CaseState.ANALYZING,
        CaseState.WAITING_FOR_USER,
        CaseState.READY_FOR_REVIEW,
        CaseState.APPROVED,
        CaseState.EXECUTING,
        CaseState.UNKNOWN,
        CaseState.ARCHIVING,
        CaseState.ARCHIVE_PENDING,
        CaseState.BLOCKED,
    )
)
_TASK_TERMINAL = (str(TaskState.DONE), str(TaskState.CANCELLED))
_PENDING_ARCHIVE = (str(ArchiveState.PENDING), str(ArchiveState.PARTIAL), str(ArchiveState.UPLOADING))


def _count(store: Any, object_type: str, where: Optional[Dict[str, Any]] = None) -> int:
    """现场数一次；存储不支持就返回 0（快照不能因为一个查询失败就整轮崩）。"""
    try:
        return len(list(store.find(object_type, where=where, limit=None)))
    except Exception:  # noqa: BLE001 - 观测路径必须比被测对象更皮实
        return 0




def snapshot_pending(store: Any) -> Dict[str, int]:
    """当前所有"需要继续处理"的对象计数：**全部现场查存储**。

    只统计**仍然开着的事务**名下的东西：一个已经 `SUCCEEDED` 的事务留下的
    OPEN 草稿 / ACTIVE 审批 / 未完结任务都是历史，不该出现在"有多少事在等我"里。
    """
    cases = list(store.find("Case", limit=None))
    open_case_ids = {case.id for case in cases if str(case.state) in _OPEN_CASE_STATES}
    open_cases = len(open_case_ids)

    def _open_scope(item: Any) -> bool:
        owner = str(getattr(item, "case_id", "") or "")
        return (not owner) or (owner in open_case_ids)

    # "等待网盘"只认**真的发起过归档**的证据（ArchiveJob 行）。
    # `Case.archive_state` 的默认值就是 PENDING，拿它当"待归档"会把每个新事务都算成等网盘。
    archive_pending = len(
        [job for job in store.find("ArchiveJob", limit=None) if str(job.state) in _PENDING_ARCHIVE]
    )
    open_questions = len(
        [
            item
            for item in store.find("Question", limit=None)
            if str(item.state) == str(QuestionState.OPEN) and _open_scope(item)
        ]
    )
    active_approvals = len(
        [
            item
            for item in store.find("Approval", limit=None)
            if str(item.state) == str(ApprovalState.ACTIVE) and _open_scope(item)
        ]
    )
    open_drafts = len(
        [
            item
            for item in store.find("Draft", limit=None)
            if str(item.state) == str(DraftState.OPEN) and _open_scope(item)
        ]
    )
    open_tasks = len(
        [
            item
            for item in store.find("Task", limit=None)
            if str(item.state) not in _TASK_TERMINAL and _open_scope(item)
        ]
    )
    authorized = len(
        [item for item in store.find("ExternalAction", limit=None) if str(item.state) == str(ActionState.AUTHORIZED)]
    )
    unknown = len(
        [item for item in store.find("ExternalAction", limit=None) if str(item.state) == str(ActionState.UNKNOWN)]
    )
    in_flight = len(
        [item for item in store.find("ExternalAction", limit=None) if str(item.state) == str(ActionState.IN_FLIGHT)]
    )
    return {
        "cases_total": len(cases),
        "cases_open": open_cases,
        "cases_awaiting_approval": _cases_awaiting_approval(store, open_case_ids),
        "questions_open": open_questions,
        "approvals_active": active_approvals,
        "drafts_open": open_drafts,
        "tasks_open": open_tasks,
        "actions_authorized": authorized,
        "actions_unknown": unknown,
        "actions_in_flight": in_flight,
        "archive_pending": archive_pending,
    }


def _cases_awaiting_approval(store: Any, open_case_ids: Any) -> int:
    """还**等用户点同意**的事务数：`READY_FOR_REVIEW` 且当前草稿版本没有 ACTIVE 审批。

    只看"用户还欠我们一步"的；已经批准过（审批 ACTIVE）或已经批准并执行完的都不算。
    """
    active_versions: set = set()
    for approval in store.find("Approval", limit=None):
        if str(approval.state) != str(ApprovalState.ACTIVE):
            continue
        for version_id in list(approval.target_object_ids or []):
            active_versions.add(str(version_id))
        for version_id in (approval.content_version_refs or {}):
            active_versions.add(str(version_id))
    count = 0
    for case in store.find("Case", limit=None):
        if case.id not in open_case_ids or str(case.state) != str(CaseState.READY_FOR_REVIEW):
            continue
        pending = False
        for draft_id in list(case.draft_ids or []):
            try:
                draft = store.get("Draft", draft_id)
            except Exception:  # noqa: BLE001
                continue
            if str(draft.state) != str(DraftState.OPEN):
                continue
            current = str(getattr(draft, "current_version_id", "") or "")
            if current and current not in active_versions:
                pending = True
        if pending:
            count += 1
    return count


def infer_waiting_on(
    pending: Dict[str, int],
    *,
    channel_ready: bool,
    archive_blocked: bool = False,
) -> List[str]:
    """把计数翻译成"正在等什么"。顺序固定，便于手机端稳定渲染。"""
    waiting: List[str] = []
    if pending.get("questions_open"):
        waiting.append(WAITING_USER)
    if pending.get("cases_awaiting_approval"):
        waiting.append(WAITING_APPROVAL)
    if pending.get("actions_unknown"):
        waiting.append(WAITING_UNKNOWN)
    if pending.get("actions_authorized") and not channel_ready:
        waiting.append(WAITING_CHANNEL)
    if archive_blocked or pending.get("archive_pending"):
        waiting.append(WAITING_PAN)
    if not waiting:
        waiting.append(WAITING_NONE)
    return [item for item in WAITING_ORDER if item in waiting]


@dataclass
class WaitingOn:
    """结构化的一行"正在等什么"（给文档/日志用；快照里存字符串列表）。"""

    kind: str
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail}


@dataclass
class ServiceState:
    """服务健康快照（可落盘、可打印、可进日志：不含秘密）。"""

    #: 服务进程标识（每次启动新值；用来分辨"是同一个进程还是重启过"）
    run_id: str = ""
    #: 仓库外 state 文件路径（空串 = 只放内存）
    state_path: str = ""
    started_at: str = ""
    last_round_at: str = ""
    rounds_completed: int = 0
    rounds_failed: int = 0
    consecutive_failures: int = 0
    interval_seconds: int = 60
    #: 是否正在跑（`--once` 跑完置 False；常驻进程运行期间恒 True）
    running: bool = False

    last_sync: Dict[str, Any] = field(default_factory=dict)
    last_ingest: Dict[str, Any] = field(default_factory=dict)
    last_recovery: Dict[str, Any] = field(default_factory=dict)
    last_round: Dict[str, Any] = field(default_factory=dict)
    pending: Dict[str, int] = field(default_factory=dict)
    waiting_on: List[str] = field(default_factory=list)
    #: 最近一次错误（阶段 + 时间 + 文本），成功一轮**不会**清掉它（"最近错误"要留着）
    last_error: Dict[str, Any] = field(default_factory=dict)
    phase_failures: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    updated_at: str = ""

    # ------------------------------------------------------------------ 更新
    def begin(self, *, run_id: str, interval_seconds: int, state_path: str = "") -> "ServiceState":
        self.run_id = run_id
        self.interval_seconds = int(interval_seconds)
        self.state_path = state_path
        self.started_at = self.started_at or utcnow_iso()
        self.running = True
        self.updated_at = self.started_at
        return self

    def record_error(self, phase: str, error: BaseException, *, at: str = "") -> None:
        moment = at or utcnow_iso()
        self.last_error = {
            "at": moment,
            "phase": phase,
            "error_class": type(error).__name__,
            "text": str(error)[:800],
        }
        self.phase_failures[phase] = self.phase_failures.get(phase, 0) + 1

    def record_round(self, payload: Dict[str, Any], *, at: str = "") -> None:
        moment = at or utcnow_iso()
        self.last_round = dict(payload)
        self.last_round_at = moment
        self.rounds_completed += 1
        if payload.get("ok"):
            self.consecutive_failures = 0
        else:
            self.rounds_failed += 1
            self.consecutive_failures += 1
        self.running = True
        self.updated_at = moment

    # ------------------------------------------------------------------ 观察
    def summary_line(self) -> str:
        sync = self.last_sync or {}
        pending = self.pending or {}
        return (
            "rounds=%d failed=%d last_round=%s sync=%s waiting=%s open_cases=%d questions=%d approvals=%d unknown=%d"
            % (
                self.rounds_completed,
                self.rounds_failed,
                self.last_round_at or "-",
                sync.get("result") or "-",
                ",".join(self.waiting_on) or "-",
                int(pending.get("cases_open", 0)),
                int(pending.get("questions_open", 0)),
                int(pending.get("approvals_active", 0)),
                int(pending.get("actions_unknown", 0)),
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state_path": self.state_path,
            "started_at": self.started_at,
            "last_round_at": self.last_round_at,
            "rounds_completed": self.rounds_completed,
            "rounds_failed": self.rounds_failed,
            "consecutive_failures": self.consecutive_failures,
            "interval_seconds": self.interval_seconds,
            "running": self.running,
            "last_sync": dict(self.last_sync),
            "last_ingest": dict(self.last_ingest),
            "last_recovery": dict(self.last_recovery),
            "last_round": dict(self.last_round),
            "pending": dict(self.pending),
            "waiting_on": list(self.waiting_on),
            "last_error": dict(self.last_error),
            "phase_failures": dict(self.phase_failures),
            "notes": list(self.notes),
            "updated_at": self.updated_at,
        }

    # ------------------------------------------------------------------ 落盘
    def save(self, path: Optional[str] = None) -> str:
        """原子写快照（`tmp` + `os.replace`）：读到一半的 JSON 不是合法的健康证据。"""
        target = path or self.state_path
        if not target:
            return ""
        self.updated_at = utcnow_iso()
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=directory or None, prefix=".service-state-", suffix=".tmp"
        )
        try:
            json.dump(self.to_dict(), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, target)
        return target

    @classmethod
    def load(cls, path: str) -> "ServiceState":
        """读快照；文件不存在或不可解析时返回"空快照 + 一条 note"（不抛错）。"""
        state = cls(state_path=path)
        if not path or not os.path.isfile(path):
            return state
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            state.notes.append("状态文件不可解析：%s（%s）" % (path, exc))
            return state
        if not isinstance(payload, dict):
            state.notes.append("状态文件顶层不是对象：%s" % path)
            return state
        for key, value in payload.items():
            if hasattr(state, key):
                setattr(state, key, value)
        state.state_path = path
        state.running = False
        return state


__all__ = [
    "ServiceState",
    "WAITING_APPROVAL",
    "WAITING_CHANNEL",
    "WAITING_NONE",
    "WAITING_ORDER",
    "WAITING_PAN",
    "WAITING_UNKNOWN",
    "WAITING_USER",
    "WaitingOn",
    "infer_waiting_on",
    "snapshot_pending",
]
