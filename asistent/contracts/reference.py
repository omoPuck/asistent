"""参考实现（最小样例）：内存存储 + 契约行为（SPEC §15.1 "契约测试、最小样例"）。

用途：
- 作为**可执行契约**的参照物：`tests/contracts/**` 先在这里跑通；
- MVP-0 的 SQLite 实现必须通过同一组一致性测试，行为一致才算符合契约；
- 它不是运行时存储（不持久化），只是语义基准。

语义要点：
- `put` 会修改传入对象（写入分配到的 id/revision/时间戳），并**保存一份深拷贝**；
- `get`/`find` 返回深拷贝：调用方必须自己持有版本号再 `put`，这样 CAS 才是真的 CAS；
- 真实实现必须用数据库事务/行锁保证 `claim_action`、`claim_event`、`put` 的 CAS 原子性。
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import transitions as tr
from .actions import DEFAULT_APPROVAL_TTL_SECONDS
from .enums import ActionState, ActorKind, ApprovalState, AuditAction, IndexState
from .errors import (
    ActionNotClaimable,
    ApprovalInvalid,
    DuplicateObject,
    NotFound,
    RevisionConflict,
    ValidationError,
)
from .ids import event_dedupe_key, new_id


def apply_limit(rows: List[Any], limit: Optional[int]) -> List[Any]:
    """统一 `limit` 语义（契约 v0.3，CP-20261010-03）。

    - `None` → 不限制；
    - `>= 0` → 最多 limit 条（`0` → 空列表）；
    - `< 0` → `ValidationError`：不允许靠 Python 负切片隐式"从尾部取"。
    """
    if limit is None:
        return rows
    if limit < 0:
        raise ValidationError(f"limit 不允许为负数: {limit}")
    return rows[:limit]


def apply_limit_tail(rows: List[Any], limit: Optional[int]) -> List[Any]:
    """同 `apply_limit`，但取**最新**的 limit 条（审计轨迹按时间正序存放）。"""
    if limit is None:
        return rows
    if limit < 0:
        raise ValidationError(f"limit 不允许为负数: {limit}")
    if limit == 0:
        return []
    return rows[-limit:]


def order_key(stamp: str, sequence: int) -> Tuple[str, int]:
    """定序键：先按时间，再按**写入序号**（契约 v0.5，CP-20261010-11）。

    为什么不能拿对象 id 当平局判据：`created_at` 只有毫秒精度，快速连续写入的多个对象
    经常落在同一毫秒里；id 是随机/散列的，用它平局等于"顺序随机"——
    分页会漏项、`limit=1` 会随机取到不同对象，而且**不同后端会给出不同答案**
    （参考实现因为 Python 慢、常常跨毫秒，反而看不出问题；SQLite 写入快，问题立刻暴露）。
    """
    return (stamp, sequence)
from .objects import (
    ActionAttempt,
    Approval,
    AuditEvent,
    Case,
    Event,
    Execution,
    ExternalAction,
    SharedObject,
)
from .store_api import RecoveryScan
from .timeutil import add_seconds, is_expired, utcnow_iso

OPEN_CASE_STATES = (
    "NEW",
    "TRIAGED",
    "WAITING_FOR_CONTEXT",
    "ANALYZING",
    "WAITING_FOR_USER",
    "READY_FOR_REVIEW",
    "APPROVED",
    "EXECUTING",
    "UNKNOWN",
    "ARCHIVING",
    "ARCHIVE_PENDING",
    "BLOCKED",
)

STATE_FIELD_BY_TYPE = {
    "Case": "state",
    "Task": "state",
    "Approval": "state",
    "ExternalAction": "state",
    "ArchiveJob": "state",
    "RestoreJob": "state",
    "Rule": "state",
    "Memory": "state",
    "Question": "state",
    "Draft": "state",
    "Execution": "state",
    "Workflow": "state",
    "WorkflowVersion": "state",
}


class ReferenceStore:
    """内存参考实现：语义基准，不做持久化。"""

    def __init__(self, *, clock: Optional[Callable[[], str]] = None) -> None:
        self._lock = threading.RLock()
        self._objects: Dict[Tuple[str, str], SharedObject] = {}
        self._unique: Dict[Tuple[str, str], Tuple[str, str]] = {}
        self._audit: List[AuditEvent] = []
        self._events: Dict[str, Event] = {}
        self._event_claims: Dict[str, str] = {}
        #: 写入序号（契约 v0.5，CP-20261010-11）：同一时间戳时用它平局，保证定序与写入顺序一致
        self._write_seq: Dict[Tuple[str, str], int] = {}
        self._seq_counter: int = 0
        self._clock = clock or utcnow_iso
        self._id_token_counter = 0

    # ------------------------------------------------------------------ helpers
    def now(self) -> str:
        return self._clock()

    def _next_id(self, object_type: str) -> str:
        self._id_token_counter += 1
        return new_id(object_type, token=f"{self._id_token_counter:06d}{utcnow_iso().replace(':', '').replace('-', '').replace('.', '')[:14]}")

    @staticmethod
    def _state_of(obj: SharedObject) -> str:
        field_name = STATE_FIELD_BY_TYPE.get(obj.OBJECT_TYPE)
        if field_name is None or not hasattr(obj, field_name):
            raise ValidationError(f"{obj.OBJECT_TYPE} 没有可转换的 state 字段")
        return str(getattr(obj, field_name))

    @staticmethod
    def _set_state(obj: SharedObject, target: str) -> None:
        field_name = STATE_FIELD_BY_TYPE.get(obj.OBJECT_TYPE, "state")
        setattr(obj, field_name, target)

    @staticmethod
    def _key(object_type: str, object_id: str) -> Tuple[str, str]:
        return (object_type, object_id)

    def _resolve_case_id(self, obj: SharedObject) -> Optional[str]:
        if obj.OBJECT_TYPE == "Case":
            return obj.id
        candidate = getattr(obj, "case_id", None)
        if isinstance(candidate, str) and candidate:
            return candidate
        linked = obj.links.get("case") if isinstance(obj.links, dict) else None
        return linked[0] if linked else None

    # ------------------------------------------------------------------ 对象 CRUD
    def put(
        self,
        obj: SharedObject,
        *,
        expected_revision: Optional[int] = None,
        unique: Optional[Tuple[str, str]] = None,
        actor_kind: str = ActorKind.SYSTEM,
        actor_id: str = "system",
        reason: str = "",
        audit_action: str = "WRITE",
        audit_detail: Optional[Dict[str, Any]] = None,
    ) -> SharedObject:
        with self._lock:
            if not obj.id:
                obj.id = self._next_id(obj.OBJECT_TYPE)
            obj.validate()
            key = self._key(obj.OBJECT_TYPE, obj.id)
            existing = self._objects.get(key)

            if unique is not None:
                namespace, value = unique
                if not namespace or value in (None, ""):
                    raise ValidationError("unique 需要 (namespace, 非空值)")
                holder = self._unique.get((namespace, str(value)))
                if holder is not None and not (holder[0] == obj.OBJECT_TYPE and holder[1] == obj.id):
                    raise DuplicateObject(
                        f"幂等键已存在，拒绝创建第二份对象：{namespace}={value}",
                        detail={
                            "namespace": namespace,
                            "value": str(value),
                            "existing_type": holder[0],
                            "existing_id": holder[1],
                        },
                    )

            if existing is None:
                obj.revision = 1
                obj.created_at = obj.created_at or self.now()
                obj.updated_at = obj.created_at
                action = str(AuditAction.CREATE) if audit_action == "WRITE" else audit_action
                before_revision: Optional[int] = None
            else:
                if expected_revision is None:
                    raise ValidationError(
                        "更新必须携带 expected_revision（禁止静默覆盖，SPEC §21.1）",
                        detail={"object_id": obj.id, "current_revision": existing.revision},
                    )
                if existing.revision != expected_revision:
                    raise RevisionConflict(
                        "revision 冲突：对象已被其他写入者更新",
                        detail={"object_id": obj.id, "expected": expected_revision, "actual": existing.revision},
                    )
                before_revision = existing.revision
                obj.revision = before_revision + 1
                obj.created_at = existing.created_at
                obj.updated_at = self.now()
                action = audit_action

            self._objects[key] = copy.deepcopy(obj)
            self._seq_counter += 1
            self._write_seq[key] = self._seq_counter
            if unique is not None:
                self._unique[(unique[0], str(unique[1]))] = (obj.OBJECT_TYPE, obj.id)
            self._write_audit(
                obj,
                action=action,
                actor_kind=actor_kind,
                actor_id=actor_id,
                reason=reason,
                before=before_revision,
                after=obj.revision,
                detail=audit_detail,
            )
            return obj

    def get(self, object_type: str, object_id: str) -> SharedObject:
        with self._lock:
            try:
                return copy.deepcopy(self._objects[self._key(object_type, object_id)])
            except KeyError as exc:
                raise NotFound(f"对象不存在: {object_type}/{object_id}") from exc

    def find(
        self,
        object_type: str,
        *,
        where: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = 100,
    ) -> List[SharedObject]:
        with self._lock:
            matches: List[SharedObject] = []
            for (kind, _), obj in self._objects.items():
                if kind != object_type:
                    continue
                if where and any(str(getattr(obj, field, None)) != str(value) for field, value in where.items()):
                    continue
                matches.append(obj)
            matches.sort(key=lambda item: order_key(item.created_at, self._write_seq.get(self._key(item.OBJECT_TYPE, item.id), 0)))
            return [copy.deepcopy(item) for item in apply_limit(matches, limit)]

    def find_one(self, object_type: str, **where: Any) -> Optional[SharedObject]:
        found = self.find(object_type, where=where or None, limit=1)
        return found[0] if found else None

    def get_by_unique(self, namespace: str, value: str) -> Optional[SharedObject]:
        with self._lock:
            holder = self._unique.get((namespace, str(value)))
            if holder is None:
                return None
            return self.get(holder[0], holder[1])

    # ------------------------------------------------------------------ 状态转换
    def transition(
        self,
        object_type: str,
        object_id: str,
        target_state: str,
        *,
        expected_revision: Optional[int] = None,
        actor_kind: str = ActorKind.AGENT,
        actor_id: str = "agent",
        reason: str = "",
        trigger: Optional[str] = None,
    ) -> SharedObject:
        with self._lock:
            obj = self.get(object_type, object_id)
            current = self._state_of(obj)
            rule = tr.require_transition(
                object_type, current, target_state, trigger=trigger or actor_kind, object_id=object_id
            )
            if object_type == "Case" and target_state in ("CANCELLED", "BLOCKED"):
                self._require_no_irreversible_action(obj, target_state)
            revision = obj.revision
            if expected_revision is not None and revision != expected_revision:
                raise RevisionConflict(
                    "revision 冲突：状态转换被拒绝",
                    detail={"object_id": object_id, "expected": expected_revision, "actual": revision},
                )
            self._set_state(obj, target_state)
            saved = self.put(
                obj,
                expected_revision=revision,
                actor_kind=actor_kind,
                actor_id=actor_id,
                reason=reason or rule.note,
                audit_action=str(AuditAction.TRANSITION),
                audit_detail={"from": current, "to": target_state},
            )
            self._emit_state_event(obj, current, target_state)
            return saved

    def _require_no_irreversible_action(self, case_obj: SharedObject, target: str) -> None:
        actions = self.find("ExternalAction", where={"case_id": case_obj.id}, limit=1000)
        blocking = [a for a in actions if str(a.state) in tr.IRREVERSIBLE_ACTION_STATES]
        if blocking:
            raise tr.IllegalTransition(
                f"事务存在不可逆外部动作，不能转换为 {target}",
                detail={"case_id": case_obj.id, "actions": [{"id": a.id, "state": str(a.state)} for a in blocking]},
            )

    def set_index_state(
        self,
        document_id: str,
        target_state: str,
        *,
        expected_revision: Optional[int] = None,
        reason: str = "",
        error: Optional[str] = None,
    ) -> SharedObject:
        with self._lock:
            document = self.get("SourceDocument", document_id)
            current = str(document.index_state)
            tr.machine_for("IndexState").require(current, target_state, object_type="IndexState", object_id=document_id)
            revision = document.revision
            if expected_revision is not None and revision != expected_revision:
                raise RevisionConflict("revision 冲突：索引状态更新被拒绝", detail={"document_id": document_id})
            document.index_state = IndexState.coerce(target_state)  # type: ignore[assignment]
            document.index_error = error
            if target_state == "INDEXED":
                document.last_indexed_at = self.now()
            return self.put(
                document,
                expected_revision=revision,
                actor_kind=ActorKind.SYSTEM,
                actor_id="indexer",
                reason=reason,
                audit_action=str(AuditAction.UPDATE),
                audit_detail={"index_state": target_state, "error": error},
            )

    # ------------------------------------------------------------------ 审计
    def append_audit(self, audit: AuditEvent) -> AuditEvent:
        with self._lock:
            if not audit.id:
                audit.id = self._next_id("AuditEvent")
            audit.at = audit.at or self.now()
            if not audit.action:
                raise ValidationError("审计事件必须带 action")
            audit.revision = max(1, audit.revision)
            audit.created_at = audit.created_at or audit.at
            audit.updated_at = audit.at
            self._audit.append(copy.deepcopy(audit))
            return audit

    def audit_trail(
        self,
        *,
        object_id: Optional[str] = None,
        case_id: Optional[str] = None,
        limit: Optional[int] = 200,
    ) -> List[AuditEvent]:
        with self._lock:
            rows = [
                event
                for event in self._audit
                if (object_id is None or event.object_id == object_id)
                and (case_id is None or event.case_id == case_id)
            ]
            return [copy.deepcopy(item) for item in apply_limit_tail(rows, limit)]

    def _write_audit(
        self,
        obj: SharedObject,
        *,
        action: str,
        actor_kind: str,
        actor_id: str,
        reason: str,
        before: Optional[int],
        after: Optional[int],
        detail: Optional[Dict[str, Any]] = None,
    ) -> AuditEvent:
        return self.append_audit(
            AuditEvent(
                id="",
                at=self.now(),
                action=action,
                actor_kind=ActorKind.coerce(actor_kind),
                actor_id=actor_id,
                case_id=self._resolve_case_id(obj),
                object_type=obj.OBJECT_TYPE,
                object_id=obj.id,
                result="OK",
                version_before=before,
                version_after=after,
                content_hash=getattr(obj, "content_hash", None) or getattr(obj, "sha256", None),
                reason=reason,
                caller=actor_id,
                correlation_id=getattr(obj, "id", None),
                detail=dict(detail or {}),
            )
        )

    # ------------------------------------------------------------------ 事件
    def publish_event(self, event: Event) -> Tuple[Event, bool]:
        with self._lock:
            if not event.dedupe_key:
                event.dedupe_key = event_dedupe_key(event.source, event.source_event_id)
            for existing in self._events.values():
                if existing.dedupe_key == event.dedupe_key:
                    return copy.deepcopy(existing), False
            if not event.id:
                event.id = self._next_id("Event")
            event.received_at = event.received_at or self.now()
            event.occurred_at = event.occurred_at or event.received_at
            event.created_at = event.created_at or event.received_at
            event.updated_at = event.received_at
            self._events[event.id] = copy.deepcopy(event)
            self._seq_counter += 1
            self._write_seq[self._key("Event", event.id)] = self._seq_counter
            return event, True

    def claim_event(self, event_id: str, *, worker: str) -> bool:
        with self._lock:
            event = self._events.get(event_id)
            if event is None:
                raise NotFound(f"事件不存在: {event_id}")
            if event.processed_at is not None or event_id in self._event_claims:
                return False
            self._event_claims[event_id] = worker
            return True

    def mark_event_processed(self, event_id: str, *, result: str) -> Event:
        with self._lock:
            event = self._events.get(event_id)
            if event is None:
                raise NotFound(f"事件不存在: {event_id}")
            event.processed_at = self.now()
            event.processing_result = result
            return copy.deepcopy(event)

    def list_events(self, *, processed: Optional[bool] = None, limit: Optional[int] = 100) -> List[Event]:
        with self._lock:
            rows = [
                event
                for event in self._events.values()
                if processed is None or (event.processed_at is not None) == processed
            ]
            rows.sort(key=lambda item: order_key(item.received_at, self._write_seq.get(self._key("Event", item.id), 0)))
            return [copy.deepcopy(item) for item in apply_limit(rows, limit)]

    def _emit_state_event(self, obj: SharedObject, before: str, after: str) -> None:
        moment = self.now()
        self.publish_event(
            Event(
                id="",
                kind="STATE_CHANGED",
                source="store",
                source_event_id=f"{obj.OBJECT_TYPE}:{obj.id}:{obj.revision}",
                occurred_at=moment,
                received_at=moment,
                case_id=self._resolve_case_id(obj),
                payload={"object_type": obj.OBJECT_TYPE, "object_id": obj.id, "from": before, "to": after},
            )
        )

    # ------------------------------------------------------------------ 审批与动作
    def approve(self, approval: Approval) -> Approval:
        with self._lock:
            if not approval.approved_action_digest:
                raise ValidationError("审批必须绑定规范化动作摘要（approved_action_digest）")
            if not approval.auth_evidence:
                raise ValidationError("审批必须由认证控制操作产生（缺少 auth_evidence）")
            approval.created_at_approval = approval.created_at_approval or self.now()
            if approval.expires_at is None:
                approval.expires_at = add_seconds(approval.created_at_approval, DEFAULT_APPROVAL_TTL_SECONDS)
            approval.state = ApprovalState.ACTIVE
            self._supersede_competing_approvals(approval)
            saved = self.put(
                approval,
                actor_kind=ActorKind.USER,
                actor_id=approval.approver,
                reason="用户批准",
                audit_action=str(AuditAction.APPROVE),
            )
            case = self.find_one("Case", id=approval.case_id)
            if case is not None and approval.id not in case.approval_ids:
                case.approval_ids.append(approval.id)
                self.put(
                    case,
                    expected_revision=case.revision,
                    actor_kind=ActorKind.USER,
                    actor_id=approval.approver,
                    reason="关联审批",
                    audit_action=str(AuditAction.APPROVE),
                )
            return saved  # type: ignore[return-value]

    def _supersede_competing_approvals(self, approval: Approval) -> List[str]:
        """同一动作至多一个 ACTIVE 审批（契约 v0.6，CP-20261010-15）。

        旧行为允许同一动作留下多条 ACTIVE 审批：执行侧虽然由 `claim_action` 兜住
        （只有一个赢家），但"这条动作到底由哪张审批授权"变得有歧义，审计上也多出无意义记录。
        现在批准新审批时，同动作上其它 ACTIVE 审批在同一事务内被 INVALIDATED
        （记录与原因都保留，不删除）。
        """
        action_id = getattr(approval, "action_id", None)
        if not action_id:
            return []
        superseded: List[str] = []
        for other in self.find("Approval", where={"action_id": action_id}, limit=None):
            if other.id == approval.id:
                continue
            if ApprovalState.coerce(str(other.state)) != ApprovalState.ACTIVE:
                continue
            self.invalidate_approval(other.id, reason="同一动作已产生新的有效审批，旧审批被取代")
            superseded.append(other.id)
        return superseded

    def invalidate_approval(self, approval_id: str, *, reason: str, state: str = "INVALIDATED") -> Approval:
        with self._lock:
            approval = self.get("Approval", approval_id)  # type: ignore[assignment]
            target = "REVOKED" if state == "REVOKED" else str(state)
            tr.machine_for("Approval").require(
                str(approval.state), target, object_type="Approval", object_id=approval_id
            )
            approval.state = ApprovalState.coerce(target)  # type: ignore[assignment]
            if target == "REVOKED":
                approval.revoked_reason = reason
            else:
                approval.invalidated_reason = reason
            return self.put(
                approval,
                expected_revision=approval.revision,
                actor_kind=ActorKind.SYSTEM,
                actor_id="system",
                reason=reason,
                audit_action=str(AuditAction.REVOKE) if target == "REVOKED" else str(AuditAction.CORRECTION),
                audit_detail={"approval_state": target},
            )  # type: ignore[return-value]

    def claim_action(
        self,
        action_id: str,
        *,
        worker: str,
        attempt_id: Optional[str] = None,
        now: Optional[str] = None,
    ) -> ActionAttempt:
        """原子领取：校验 + 预留一次性 Approval + 写 IN_FLIGHT + 建 attempt（SPEC §21.2）。

        这是**单次调用**：调用方不得先查询状态再标记（禁止 TOCTOU）。
        """
        with self._lock:
            moment = now or self.now()
            action = self.get("ExternalAction", action_id)  # type: ignore[assignment]
            if str(action.state) != str(ActionState.AUTHORIZED):
                raise ActionNotClaimable(
                    f"动作当前状态不可领取: {action.state}",
                    detail={"action_id": action_id, "state": str(action.state)},
                )
            if action.claimed_by:
                raise ActionNotClaimable(
                    "动作已被其他执行器占位，禁止重复外呼",
                    detail={"action_id": action_id, "claimed_by": action.claimed_by, "claimed_at": action.claimed_at},
                )
            if not action.approval_id:
                raise ActionNotClaimable("动作没有绑定有效审批，禁止外呼", detail={"action_id": action_id})
            approval = self.get("Approval", action.approval_id)  # type: ignore[assignment]
            if str(approval.state) != str(ApprovalState.ACTIVE):
                raise ApprovalInvalid(
                    "审批已被消费、撤销或失效",
                    detail={"approval_id": approval.id, "state": str(approval.state), "reason": approval.revoked_reason or approval.invalidated_reason},
                )
            if approval.expires_at and is_expired(approval.expires_at, now=moment):
                raise ApprovalInvalid("审批已过期", detail={"approval_id": approval.id, "expires_at": approval.expires_at})
            if approval.approved_action_digest != action.summary_digest:
                raise ApprovalInvalid(
                    "审批绑定的内容与当前动作不一致",
                    detail={
                        "approval_id": approval.id,
                        "approved": approval.approved_action_digest,
                        "current": action.summary_digest,
                    },
                )

            new_attempt_id = attempt_id or self._next_id("ActionAttempt")
            attempt = ActionAttempt(
                id=new_attempt_id,
                action_id=action_id,
                attempt_no=len(action.attempt_ids) + 1,
                request_idempotency_key=action.idempotency_key,
                state="IN_FLIGHT",
                started_at=moment,
                executor_node=worker,
            )
            self.put(
                attempt,
                actor_kind=ActorKind.SYSTEM,
                actor_id=worker,
                reason="原子领取动作",
                audit_action=str(AuditAction.EXECUTE),
            )

            # 预留并立即消费一次性审批：预留后不得再授权第二个动作
            approval.state = ApprovalState.CONSUMED
            approval.consumed_by_attempt_id = new_attempt_id
            approval.reserved_at = moment
            self.put(
                approval,
                expected_revision=approval.revision,
                actor_kind=ActorKind.SYSTEM,
                actor_id=worker,
                reason="审批被原子预留",
                audit_action=str(AuditAction.EXECUTE),
                audit_detail={"attempt_id": new_attempt_id},
            )

            action.state = ActionState.IN_FLIGHT
            action.claimed_by = worker
            action.claimed_at = moment
            action.attempt_ids.append(new_attempt_id)
            saved = self.put(
                action,
                expected_revision=action.revision,
                actor_kind=ActorKind.SYSTEM,
                actor_id=worker,
                reason="动作进入 IN_FLIGHT",
                audit_action=str(AuditAction.EXECUTE),
                audit_detail={"attempt_id": new_attempt_id, "idempotency_key": action.idempotency_key},
            )
            self._touch_case(saved, worker, note="执行外部动作")
            return attempt

    def record_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        external_response: Optional[Dict[str, Any]] = None,
        external_ref: Optional[str] = None,
        failure_class: Optional[str] = None,
        error_text: Optional[str] = None,
    ) -> ActionAttempt:
        with self._lock:
            attempt = self.get("ActionAttempt", attempt_id)  # type: ignore[assignment]
            action = self.get("ExternalAction", attempt.action_id)  # type: ignore[assignment]
            target = str(state)
            tr.machine_for("ExternalAction").require(
                str(action.state), target, object_type="ExternalAction", object_id=action.id
            )
            moment = self.now()
            attempt.state = target
            attempt.finished_at = moment
            attempt.external_response = dict(external_response or {})
            attempt.external_ref = external_ref
            attempt.failure_class = failure_class
            attempt.error_text = error_text
            saved_attempt = self.put(
                attempt,
                expected_revision=attempt.revision,
                actor_kind=ActorKind.SYSTEM,
                actor_id=attempt.executor_node or "executor",
                reason=f"尝试结果: {target}",
                audit_action=str(AuditAction.EXECUTE),
                audit_detail={"external_ref": external_ref, "failure_class": failure_class},
            )

            action.state = ActionState.coerce(target)  # type: ignore[assignment]
            action.result_class = target
            if target != "IN_FLIGHT":
                action.finalized_at = moment
                if target == "FAILED_SAFE":
                    action.claimed_by = None
            saved_action = self.put(
                action,
                expected_revision=action.revision,
                actor_kind=ActorKind.SYSTEM,
                actor_id=attempt.executor_node or "executor",
                reason=f"动作结果: {target}",
                audit_action=str(AuditAction.EXECUTE),
                audit_detail={"attempt_id": attempt_id, "failure_class": failure_class},
            )
            self._sync_case_after_action(saved_action)
            return saved_attempt  # type: ignore[return-value]

    def demote_in_flight_to_unknown(self, *, reason: str = "进程重启时动作仍为 IN_FLIGHT") -> List[str]:
        """重启恢复：IN_FLIGHT 先转 UNKNOWN 并查证，绝不按租约到期直接重发（SPEC §21.2）。"""
        demoted: List[str] = []
        with self._lock:
            for action in self.find("ExternalAction", where={"state": "IN_FLIGHT"}, limit=1000):
                for attempt in self.find("ActionAttempt", where={"action_id": action.id}, limit=100):
                    if str(attempt.state) == "IN_FLIGHT":
                        self.record_attempt(
                            attempt.id,
                            state="UNKNOWN",
                            external_response={"note": reason},
                            failure_class="EXTERNAL_UNKNOWN",
                            error_text=reason,
                        )
                demoted.append(action.id)
        return demoted

    def _sync_case_after_action(self, action: SharedObject) -> None:
        case = self.find_one("Case", id=action.case_id)
        if case is None or str(case.state) != "EXECUTING":
            return
        target = {"SUCCEEDED": "SUCCEEDED", "FAILED_SAFE": "FAILED", "UNKNOWN": "UNKNOWN"}.get(str(action.state))
        if target:
            self.transition(
                "Case",
                case.id,
                target,
                actor_kind=ActorKind.SYSTEM,
                actor_id="executor",
                reason="动作结果回写事务状态",
            )

    def _touch_case(self, action: SharedObject, actor_id: str, *, note: str) -> None:
        case = self.find_one("Case", id=action.case_id)
        if case is None:
            return
        if str(case.state) in ("APPROVED", "READY_FOR_REVIEW"):
            self.transition("Case", case.id, "EXECUTING", actor_kind=ActorKind.SYSTEM, actor_id=actor_id, reason=note)

    # ------------------------------------------------------------------ 执行记录
    def record_execution(self, execution: Execution) -> Execution:
        with self._lock:
            if not execution.id:
                execution.id = self._next_id("Execution")
            existing = self._objects.get(self._key("Execution", execution.id))
            if existing is None:
                return self.put(
                    execution,
                    actor_kind=ActorKind.AGENT,
                    actor_id="agent",
                    reason="记录执行",
                    audit_action=str(AuditAction.WRITE),
                )  # type: ignore[return-value]
            return self.put(
                execution,
                expected_revision=existing.revision,
                actor_kind=ActorKind.AGENT,
                actor_id="agent",
                reason="执行结束",
                audit_action=str(AuditAction.WRITE),
            )  # type: ignore[return-value]

    # ------------------------------------------------------------------ 恢复与观测
    def recovery_scan(self) -> RecoveryScan:
        with self._lock:
            scan = RecoveryScan()
            for case in self.find("Case", limit=10000):
                state = str(case.state)
                if state in OPEN_CASE_STATES:
                    scan.open_case_ids.append(case.id)
                if state in ("WAITING_FOR_USER", "UNKNOWN"):
                    scan.waiting_user_case_ids.append(case.id)
            scan.pending_question_ids = [q.id for q in self.find("Question", where={"state": "OPEN"}, limit=10000)]
            scan.pending_approval_ids = [a.id for a in self.find("Approval", where={"state": "ACTIVE"}, limit=10000)]
            scan.in_flight_action_ids = [a.id for a in self.find("ExternalAction", where={"state": "IN_FLIGHT"}, limit=10000)]
            scan.unknown_action_ids = [a.id for a in self.find("ExternalAction", where={"state": "UNKNOWN"}, limit=10000)]
            scan.pending_index_document_ids = [
                d.id
                for d in self.find("SourceDocument", limit=10000)
                if str(d.index_state) in ("PENDING", "STALE", "FAILED", "REBUILD_REQUIRED")
            ]
            scan.pending_archive_job_ids = [
                j.id
                for j in self.find("ArchiveJob", limit=10000)
                if str(j.state) in ("PENDING", "UPLOADING", "PARTIAL", "VERIFYING", "PENDING_VERIFICATION", "FAILED")
            ]
            scan.failed_restore_job_ids = [
                r.id for r in self.find("RestoreJob", where={"state": "FAILED"}, limit=10000)
            ]
            return scan

    def stats(self) -> Dict[str, int]:
        with self._lock:
            counts: Dict[str, int] = {}
            for (kind, _) in self._objects:
                counts[kind] = counts.get(kind, 0) + 1
            counts["AuditEvent"] = len(self._audit)
            counts["Event"] = len(self._events)
            counts["_total"] = len(self._objects)
            return counts


__all__ = ["ReferenceStore", "RecoveryScan", "OPEN_CASE_STATES"]
