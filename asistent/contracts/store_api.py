"""存储接口契约（SPEC §4.3、§15.1）。

这些签名是**冻结合同**：MVP-0 实现 SQLite 版本，必须原样通过 `tests/contracts/**` 的一致性测试；
任何签名变更都要先提案、由主 Agent 裁决并发布新契约版本。

设计要点：
- 更新一律携带 `expected_revision`，旧版本写入拒绝为冲突（§21.1）；
- 事件发布按去重键幂等（§21.1、§7.3）；
- 审计只追加（§11.3）；
- 动作领取是**一个原子调用**，禁止"先查询、后标记"（§21.2）；
- 重启后恢复必须能扫出未完成清单，且 IN_FLIGHT 先转 UNKNOWN 查证（§13.3、§21.2）。

`limit` 语义（契约 v0.3 明确，见 `contracts/CHANGELOG.md` CP-20261010-03）：
- `None` → 不限制；
- `>= 0` → 最多返回 limit 条（`0` 返回空列表）；
- `< 0` → 抛 `ValidationError`：**不允许**靠 Python 负切片"从尾部取"这种隐式行为。

`limit` 截取排序后的**哪一端**同样必须明确（契约 v0.4，CP-20261010-06）：
- `find(...)`：先按 `(created_at, id)` **升序**排序，再取前 limit 条 → 得到**最早**的 limit 条；
- `list_events(...)`：先按 `(received_at, id)` 升序，再取前 limit 条 → 即 **FIFO 队列**语义（最早的先处理）；
- `audit_trail(...)`：取**最新**的 limit 条（尾部）——审计关心"最近发生了什么"。

三者不对称是**故意的**，调用方不得假设 `find` 返回"最近 N 条"；需要最近 N 条时必须显式排序
或改用 `audit_trail` / 事件表（MVP-1 的幂等测试曾因默认这个假设而"假绿"，见 CP-20261010-06）。

`get_by_unique` / `now` / `put(audit_detail=...)` 原本只存在于参考实现，
但已被冻结测试与调用方使用，契约 v0.3 把它们登记为协议的一部分（CP-20261010-04/05）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from .enums import ActorKind
from .objects import (
    ActionAttempt,
    Approval,
    AuditEvent,
    Case,
    Event,
    Execution,
    SharedObject,
)


@dataclass
class RecoveryScan:
    """重启恢复扫描结果（SPEC §13.3）。"""

    open_case_ids: List[str] = field(default_factory=list)
    waiting_user_case_ids: List[str] = field(default_factory=list)
    pending_question_ids: List[str] = field(default_factory=list)
    pending_approval_ids: List[str] = field(default_factory=list)
    in_flight_action_ids: List[str] = field(default_factory=list)
    unknown_action_ids: List[str] = field(default_factory=list)
    pending_index_document_ids: List[str] = field(default_factory=list)
    pending_archive_job_ids: List[str] = field(default_factory=list)
    failed_restore_job_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "open_case_ids": self.open_case_ids,
            "waiting_user_case_ids": self.waiting_user_case_ids,
            "pending_question_ids": self.pending_question_ids,
            "pending_approval_ids": self.pending_approval_ids,
            "in_flight_action_ids": self.in_flight_action_ids,
            "unknown_action_ids": self.unknown_action_ids,
            "pending_index_document_ids": self.pending_index_document_ids,
            "pending_archive_job_ids": self.pending_archive_job_ids,
            "failed_restore_job_ids": self.failed_restore_job_ids,
        }

    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in self.__dataclass_fields__)


@runtime_checkable
class StoreProtocol(Protocol):
    """共享对象存储与运行时的最小契约。"""

    # ---- 通用对象 ----
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
    ) -> SharedObject: ...

    def get(self, object_type: str, object_id: str) -> SharedObject: ...

    def find(
        self,
        object_type: str,
        *,
        where: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = 100,
    ) -> List[SharedObject]: ...

    def find_one(self, object_type: str, **where: Any) -> Optional[SharedObject]: ...

    def get_by_unique(self, namespace: str, value: str) -> Optional[SharedObject]:
        """按 `put(unique=(namespace, value))` 登记的唯一键取对象；不存在返回 `None`。

        返回 `None` 而不是抛错，是为了让"重复投递"的处理保持廉价：
        调用方拿到既有对象后直接复用，无需把正常路径写成异常路径（§7.3、§21.2）。
        """
        ...

    def now(self) -> str:
        """当前时间，UTC ISO-8601 毫秒 + `Z`。

        单独暴露是为了让时间可注入：时钟守门（`clock.ClockGuard`）与测试都需要
        把"当前时间"作为依赖，而不是到处直接调用 `utcnow()`。
        """
        ...

    # ---- 状态转换 ----
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
    ) -> SharedObject: ...

    def set_index_state(
        self,
        document_id: str,
        target_state: str,
        *,
        expected_revision: Optional[int] = None,
        reason: str = "",
        error: Optional[str] = None,
    ) -> SharedObject: ...

    # ---- 审计 ----
    def append_audit(self, audit: AuditEvent) -> AuditEvent: ...

    def audit_trail(
        self,
        *,
        object_id: Optional[str] = None,
        case_id: Optional[str] = None,
        limit: Optional[int] = 200,
    ) -> List[AuditEvent]: ...

    # ---- 事件 ----
    def publish_event(self, event: Event) -> Tuple[Event, bool]: ...

    def claim_event(self, event_id: str, *, worker: str) -> bool: ...

    def mark_event_processed(self, event_id: str, *, result: str) -> Event: ...

    def list_events(self, *, processed: Optional[bool] = None, limit: Optional[int] = 100) -> List[Event]: ...

    # ---- 审批与外部动作 ----
    def approve(self, approval: Approval) -> Approval:
        """批准一张审批并使其 ACTIVE。契约 v0.6 明确两条语义（CP-20261010-15/16）：

        - **同一动作至多一个 ACTIVE 审批**：批准新审批时，同 `action_id` 上其它 ACTIVE 审批
          必须在**同一事务内**被置为 INVALIDATED（原因写明"被新审批取代"，记录保留不删）。
          这样"这条动作由哪张审批授权"永远唯一；执行侧的一次性执行权（`claim_action`）是
          另一道独立的兜底，不能拿它当"重复批准无害"的理由。
        - **默认有效期是契约的一部分**：`expires_at is None` 时按
          `asistent.contracts.actions.DEFAULT_APPROVAL_TTL_SECONDS` 从 `created_at_approval`
          起算；调用方显式传入时以传入值为准。实现不得再各自硬编码 TTL。
        """
        ...

    def invalidate_approval(self, approval_id: str, *, reason: str, state: str = "INVALIDATED") -> Approval: ...

    def claim_action(
        self,
        action_id: str,
        *,
        worker: str,
        attempt_id: Optional[str] = None,
        now: Optional[str] = None,
    ) -> ActionAttempt: ...

    def record_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        external_response: Optional[Dict[str, Any]] = None,
        external_ref: Optional[str] = None,
        failure_class: Optional[str] = None,
        error_text: Optional[str] = None,
    ) -> ActionAttempt: ...

    def demote_in_flight_to_unknown(self, *, reason: str = "进程重启时动作仍为 IN_FLIGHT") -> List[str]: ...

    # ---- 执行记录 ----
    def record_execution(self, execution: Execution) -> Execution: ...

    # ---- 恢复与观测 ----
    def recovery_scan(self) -> RecoveryScan: ...

    def stats(self) -> Dict[str, int]: ...


def case_transition_trigger(actor_kind: str) -> str:
    """把操作者类型映射为状态机触发者标识。"""
    mapping = {
        str(ActorKind.USER): "USER",
        str(ActorKind.AGENT): "AGENT",
        str(ActorKind.SYSTEM): "SYSTEM",
        str(ActorKind.EXTERNAL): "EXTERNAL",
    }
    return mapping.get(str(actor_kind), "SYSTEM")


__all__ = ["RecoveryScan", "StoreProtocol", "case_transition_trigger", "Case", "Event", "Approval", "ActionAttempt", "AuditEvent"]
