"""外部动作执行引擎（SPEC §5.2/§5.4/§5.7、§13.1、§21.2、§21.4；契约 §5、§8）。

一句话目标（SPEC §1002 的承诺边界）：

> **不因自身重复轮询、并发、重启或重试而再次主动提交同一不安全动作。**

因此执行路径被刻意压成"一个原子调用 + 一段外呼 + 一次结果落库"，中间不留任何
"先查一下再决定"的空窗：

```text
① 只读预检（构造外发请求、确认有道道）        ← 不产生任何副作用
② store.claim_action(...)   ← 唯一的线性化点：校验 + 预留一次性审批 + attempt_id + IN_FLIGHT
③ sender.send(request)      ← 到这里才允许碰外部；请求携带核心分配的幂等键
④ store.record_attempt(...) ← 落一个确定结论：SUCCEEDED / FAILED_SAFE / UNKNOWN
```

②之前不碰外部，②之后不"回头再确认一遍"：SPEC §984 明令禁止"先查询、后标记"的非原子检查。

崩溃语义：③~④ 之间被杀 → attempt 与 action 都停在 `IN_FLIGHT`（**可能已经触达外部**），
重启后 `recover()` 只做一件事：`demote_in_flight_to_unknown()`，把结果标成 `UNKNOWN`
并等待用户裁决；**绝不**按超时/租约自动重发（§21.2 第 998 行）。

`UNKNOWN` 只能由用户裁决推进（`resolve_unknown`）：

- `abandon` → `UNKNOWN → CANCELLED`（永不重发）；
- `retry`   → 必须显式确认重复风险 + 重新过认证审批，
             走 `UNKNOWN → FAILED_SAFE → AUTHORIZED → 新 attempt`，
             并且**沿用同一个幂等键**（`claim_action` 从动作上取键，重试无法自造新键）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..contracts import transitions as tr
from ..contracts.actions import ActionSummary, PER_ACTION_APPROVAL_KINDS
from ..contracts.enums import ActorKind, ApprovalState, AuditAction, ExecutionState, FailureClass
from ..contracts.errors import (
    ActionNotClaimable,
    ApprovalInvalid,
    ApprovalRequired,
    AsistentError,
    NotFound,
    ValidationError,
)
from ..contracts.mail import MailSendRequest, MailSendResult, MailSender
from ..contracts.objects import ActionAttempt, AuditEvent, Execution, ExternalAction

from . import approval as approval_mod
from .senders import RESULT_CLASSES, ScriptedMailSender, classify_sender_exception

__all__ = [
    "ExecutionOutcome",
    "RecoveryOutcome",
    "ResolutionOutcome",
    "PendingDecision",
    "ActionEngine",
    "CASE_TARGET_FOR_ACTION",
    "approve_and_authorize",
    "make_engine",
]

#: 动作结论 → Case 结论（§21.2 表格与 §5.2 状态机）
CASE_TARGET_FOR_ACTION: Dict[str, str] = {
    "SUCCEEDED": "SUCCEEDED",
    "FAILED_SAFE": "FAILED",
    "UNKNOWN": "UNKNOWN",
}


@dataclass
class ExecutionOutcome:
    """一次执行尝试的完整结论（成功、被拒、未知都要能解释清楚）。"""

    action_id: str
    action_state: str
    result_class: str          # SUCCEEDED / FAILED_SAFE / UNKNOWN / REJECTED / NO_CHANNEL
    attempt_id: Optional[str] = None
    attempt_no: int = 0
    sender_called: bool = False
    sender_call_count: int = 0
    external_ref: Optional[str] = None
    failure_class: Optional[str] = None
    reason: str = ""
    code: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    execution_id: Optional[str] = None
    idempotency_key: str = ""

    @property
    def claimed(self) -> bool:
        """是否真的拿到了执行权（= 是否可能产生外部副作用）。"""
        return self.attempt_id is not None

    @property
    def resend(self) -> bool:
        """本次尝试是不是"重试"（attempt_no > 1）。"""
        return self.attempt_no > 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_state": self.action_state,
            "result_class": self.result_class,
            "attempt_id": self.attempt_id,
            "attempt_no": self.attempt_no,
            "sender_called": self.sender_called,
            "sender_call_count": self.sender_call_count,
            "external_ref": self.external_ref,
            "failure_class": self.failure_class,
            "reason": self.reason,
            "code": self.code,
            "detail": self.detail,
            "execution_id": self.execution_id,
            "idempotency_key": self.idempotency_key,
        }


@dataclass
class RecoveryOutcome:
    """重启恢复结论：重点在 `resent_action_ids` 必须**永远为空**。"""

    demoted_action_ids: List[str] = field(default_factory=list)
    unknown_action_ids: List[str] = field(default_factory=list)
    pending_decision_action_ids: List[str] = field(default_factory=list)
    resent_action_ids: List[str] = field(default_factory=list)
    sender_call_count: int = 0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "demoted_action_ids": list(self.demoted_action_ids),
            "unknown_action_ids": list(self.unknown_action_ids),
            "pending_decision_action_ids": list(self.pending_decision_action_ids),
            "resent_action_ids": list(self.resent_action_ids),
            "sender_call_count": self.sender_call_count,
            "reason": self.reason,
        }


@dataclass
class ResolutionOutcome:
    """用户对 `UNKNOWN` 的裁决结果。"""

    action_id: str
    decision: str                       # retry / abandon
    action_state: str = ""
    approval_id: Optional[str] = None
    duplicate_risk_acknowledged: bool = False
    reason: str = ""
    next_step: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "decision": self.decision,
            "action_state": self.action_state,
            "approval_id": self.approval_id,
            "duplicate_risk_acknowledged": self.duplicate_risk_acknowledged,
            "reason": self.reason,
            "next_step": self.next_step,
        }


@dataclass
class PendingDecision:
    """等待用户裁决的一条待办（控制台/手机要显示的东西，不是内部状态）。"""

    action_id: str
    case_id: str
    kind: str
    action_state: str
    attempt_id: Optional[str] = None
    attempt_no: int = 0
    failure_class: Optional[str] = None
    error_text: str = ""
    idempotency_key: str = ""
    allowed_decisions: Tuple[str, ...] = ("retry", "abandon")
    warning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "case_id": self.case_id,
            "kind": self.kind,
            "action_state": self.action_state,
            "attempt_id": self.attempt_id,
            "attempt_no": self.attempt_no,
            "failure_class": self.failure_class,
            "error_text": self.error_text,
            "idempotency_key": self.idempotency_key,
            "allowed_decisions": list(self.allowed_decisions),
            "warning": self.warning,
        }


class ActionEngine:
    """外部动作执行引擎：领取 → 持久化 → 外呼 → 分类 → 记录。

    `sender` 可以按调用覆盖（`execute(..., sender=...)`），但**同一邮箱只允许一个受控
    外发适配器**（§21.2 第 1000 行）—— 本引擎不提供"多通道竞争提交"，只提供单通道注入。
    """

    def __init__(
        self,
        store: Any,
        sender: Optional[MailSender] = None,
        *,
        node: str = "executor-1",
        execution_backend: str = "actions",
    ) -> None:
        self.store = store
        self.sender = sender
        self.node = node
        self.execution_backend = execution_backend

    # ------------------------------------------------------------------ 观测
    def sender_call_count(self, sender: Optional[MailSender] = None) -> int:
        channel = sender if sender is not None else self.sender
        return _channel_calls(channel) or 0

    def action_state(self, action_id: str) -> str:
        return str(self.store.get("ExternalAction", action_id).state)

    def case_state(self, case_id: str) -> str:
        case = self.store.find_one("Case", id=case_id)
        return "" if case is None else str(case.state)

    # ------------------------------------------------------------------ 主路径
    def execute(
        self,
        action_id: str,
        *,
        worker: Optional[str] = None,
        sender: Optional[MailSender] = None,
        raise_on_reject: bool = False,
    ) -> ExecutionOutcome:
        """尝试执行一次。返回结论；`raise_on_reject=True` 时把契约错误原样抛出。"""
        node = worker or self.node
        channel = sender if sender is not None else self.sender
        action = self.store.get("ExternalAction", action_id)

        # ---- ① 只读预检：绝不产生副作用，也绝不把"能不能发"交给非原子判断
        if channel is None:
            return self._reject(
                action,
                code="OUTBOUND_CHANNEL_MISSING",
                reason="没有可用外发通道：拒绝执行（缺少受控适配器时不外呼）",
                detail={},
                raise_on_reject=raise_on_reject,
            )
        # 审批绑定的"确定 DraftVersion"是否还是这一版（草稿又改了一版 → 旧审批必须失效）
        stale_reasons = approval_mod.enforce_version_binding(self.store, action_id)
        if stale_reasons:
            return self._reject(
                self.store.get("ExternalAction", action_id),
                code="APPROVAL_INVALID",
                reason="审批失效，必须重新确认：%s" % ("；".join(stale_reasons),),
                detail={"action_id": action_id, "reasons": list(stale_reasons)},
                raise_on_reject=raise_on_reject,
            )
        try:
            request = self.build_request(action)
        except AsistentError as exc:
            return self._reject(
                action, code=exc.code, reason=exc.message, detail=exc.detail, raise_on_reject=raise_on_reject
            )

        # ---- ② 原子领取：校验 + 预留一次性审批 + attempt_id + IN_FLIGHT
        try:
            attempt = self.store.claim_action(action_id, worker=node)
        except (ActionNotClaimable, ApprovalInvalid) as exc:
            return self._reject(
                action, code=exc.code, reason=exc.message, detail=exc.detail, raise_on_reject=raise_on_reject
            )

        # 到这里动作已经"可能触达外部"：只允许走向确定结论
        return self._perform(action, attempt, request, channel, node)

    def _perform(
        self,
        action: ExternalAction,
        attempt: ActionAttempt,
        request: MailSendRequest,
        channel: MailSender,
        node: str,
    ) -> ExecutionOutcome:
        calls_before = _channel_calls(channel)
        started_at = self.store.now()
        try:
            result = channel.send(request)
        except BaseException as exc:  # noqa: BLE001 - 崩溃类异常必须原样上抛，不许被吞
            if not isinstance(exc, Exception):
                # 被强杀/中断：attempt 与 action 停在 IN_FLIGHT，交给 recover() 转 UNKNOWN
                raise
            result_class, failure_class = classify_sender_exception(exc)
            return self._finalize(
                action=action,
                attempt=attempt,
                node=node,
                started_at=started_at,
                result_class=result_class,
                failure_class=failure_class,
                external_ref=None,
                external_response={
                    "channel": getattr(channel, "name", ""),
                    "error_code": getattr(exc, "code", type(exc).__name__),
                    "error": getattr(exc, "message", str(exc)),
                },
                error_text=str(getattr(exc, "message", str(exc))),
                calls_before=calls_before,
                channel=channel,
            )
        result_class, mismatch = _normalize_send_result(result)
        if mismatch:
            failure_class = str(FailureClass.EXTERNAL_UNKNOWN)
        elif result_class == "SUCCEEDED":
            failure_class = None
        elif result_class == "FAILED_SAFE":
            failure_class = str(FailureClass.TOOL_FAILURE)
        else:
            failure_class = str(FailureClass.EXTERNAL_UNKNOWN)

        external_response = {
            "channel": getattr(channel, "name", ""),
            "result_class": str(getattr(result, "result_class", "")),
            "accepted": bool(getattr(result, "accepted", False)),
            "raw": dict(getattr(result, "raw", {}) or {}),
        }
        if mismatch:
            external_response["mismatch"] = mismatch
        return self._finalize(
            action=action,
            attempt=attempt,
            node=node,
            started_at=started_at,
            result_class=result_class,
            failure_class=failure_class,
            external_ref=getattr(result, "external_ref", None),
            external_response=external_response,
            error_text=str(getattr(result, "error", "") or mismatch or ""),
            calls_before=calls_before,
            channel=channel,
        )

    def _finalize(
        self,
        *,
        action: ExternalAction,
        attempt: ActionAttempt,
        node: str,
        started_at: str,
        result_class: str,
        failure_class: Optional[str],
        external_ref: Optional[str],
        external_response: Dict[str, Any],
        error_text: str,
        calls_before: Optional[int],
        channel: MailSender,
    ) -> ExecutionOutcome:
        """④ 落库确定结论 + 记录 Execution。顺序固定：先落状态，再回写 Case。"""
        saved_attempt = self.store.record_attempt(
            attempt.id,
            state=result_class,
            external_response=external_response,
            external_ref=external_ref,
            failure_class=failure_class,
            error_text=error_text or None,
        )
        refreshed = self.store.get("ExternalAction", action.id)
        case_id = self._reconcile_case(refreshed, node=node)
        execution = self._record_execution(
            action=refreshed,
            attempt=saved_attempt,
            node=node,
            started_at=started_at,
            result_class=result_class,
            failure_class=failure_class,
            error_text=error_text,
        )
        calls_after = _channel_calls(channel)
        if calls_before is None or calls_after is None:
            # 通道没有提供调用计数：我们确实调用了 send()，如实标"已调用"，计数留 0
            sender_called = True
            calls_after = calls_after or 0
        else:
            sender_called = calls_after > calls_before
        return ExecutionOutcome(
            action_id=action.id,
            action_state=str(refreshed.state),
            result_class=result_class,
            attempt_id=saved_attempt.id,
            attempt_no=saved_attempt.attempt_no,
            sender_called=sender_called,
            sender_call_count=calls_after,
            external_ref=external_ref,
            failure_class=failure_class,
            reason=_result_reason(result_class, external_response, error_text),
            code=None,
            detail={"case_id": case_id, "external_response": external_response},
            execution_id=execution.id,
            idempotency_key=saved_attempt.request_idempotency_key,
        )

    # ------------------------------------------------------------------ 请求构造
    def build_request(self, action: ExternalAction) -> MailSendRequest:
        """从持久化的规范化摘要构造外发请求（纯本地，无副作用）。

        - 只接受逐次审批类动作（`PER_ACTION_APPROVAL_KINDS`）：本地/自动类动作不该走外呼；
        - 幂等键**只能**取自动作上已持久化的 `idempotency_key`（模型不得自造，§21.2）；
        - 附件只有内容哈希，没有真实路径：本轮不提供附件外发（拒绝而不是静默丢附件）。
        """
        if action.kind not in PER_ACTION_APPROVAL_KINDS:
            raise ValidationError(
                "该动作类型不走外呼执行路径（需要逐次审批的外部动作才允许）",
                detail={"action_id": action.id, "kind": action.kind},
            )
        if action.connector and not str(action.connector).startswith("mail:"):
            raise ValidationError(
                "本轮只接入受控邮件外发适配器（connector 必须声明 mail:* 通道）",
                detail={"action_id": action.id, "connector": action.connector},
            )
        if not action.connector:
            raise ValidationError(
                "动作未声明外发通道（connector）：拒绝外呼",
                detail={"action_id": action.id},
            )
        if not action.idempotency_key:
            raise ValidationError(
                "动作缺少核心分配的稳定幂等键（不得外呼）",
                detail={"action_id": action.id},
            )
        summary = approval_mod.summary_from_normalized(action.action_summary)
        if action.summary_digest and summary.digest() != action.summary_digest:
            raise ValidationError(
                "动作摘要与 action_summary_digest 不一致：先修复动作再外呼",
                detail={"action_id": action.id, "stored": action.summary_digest, "recomputed": summary.digest()},
            )
        if summary.attachments:
            raise ValidationError(
                "附件内容哈希已在审批摘要内，但本轮不提供附件外发通道：拒绝而不是静默丢附件",
                detail={"action_id": action.id, "attachments": len(summary.attachments)},
            )
        request = MailSendRequest(
            from_account=summary.from_account,
            to=list(summary.to),
            cc=list(summary.cc),
            bcc=list(summary.bcc),
            subject=summary.subject,
            body=summary.body,
            idempotency_key=action.idempotency_key,
            action_id=action.id,
        )
        request.validate()
        return request

    # ------------------------------------------------------------------ 拒绝路径
    def _reject(
        self,
        action: ExternalAction,
        *,
        code: str,
        reason: str,
        detail: Dict[str, Any],
        raise_on_reject: bool,
    ) -> ExecutionOutcome:
        if raise_on_reject:
            raise _error_for(code, reason, detail)
        self.store.append_audit(
            AuditEvent(
                at=self.store.now(),
                action=str(AuditAction.REJECT),
                actor_kind=ActorKind.SYSTEM,
                actor_id=self.node,
                case_id=action.case_id or None,
                object_type="ExternalAction",
                object_id=action.id,
                result="REJECTED",
                reason=reason,
                detail={"code": code, **(detail or {})},
            )
        )
        return ExecutionOutcome(
            action_id=action.id,
            action_state=str(action.state),
            result_class="REJECTED",
            attempt_id=None,
            attempt_no=0,
            sender_called=False,
            sender_call_count=self.sender_call_count(),
            reason=reason,
            code=code,
            detail=dict(detail or {}),
            idempotency_key=action.idempotency_key,
        )

    # ------------------------------------------------------------------ 恢复
    def recover(self, *, reason: str = "进程重启时动作仍为 IN_FLIGHT") -> RecoveryOutcome:
        """重启入口：`IN_FLIGHT` → `UNKNOWN`，等待用户裁决；**不重发任何东西**。

        `resent_action_ids` 永远为空：本方法内没有任何 `send` 调用路径。
        """
        demoted = self.store.demote_in_flight_to_unknown(reason=reason)
        scan = self.store.recovery_scan()
        confirmed: List[str] = []
        for action_id in demoted:
            state = self.store.get("ExternalAction", action_id).state
            if str(state) == "UNKNOWN":
                confirmed.append(action_id)
        return RecoveryOutcome(
            demoted_action_ids=confirmed,
            unknown_action_ids=list(scan.unknown_action_ids),
            pending_decision_action_ids=list(scan.unknown_action_ids),
            resent_action_ids=[],
            sender_call_count=self.sender_call_count(),
            reason=reason,
        )

    def pending_decisions(self, *, action_id: Optional[str] = None) -> List[PendingDecision]:
        """列出等用户裁决的动作（`UNKNOWN`）：就是控制台要显示的那一条。"""
        if action_id is not None:
            action = self.store.get("ExternalAction", action_id)
            actions = [action] if str(action.state) == "UNKNOWN" else []
        else:
            actions = [a for a in self.store.find("ExternalAction", where={"state": "UNKNOWN"}, limit=1000)]
        out: List[PendingDecision] = []
        for action in actions:
            attempts = self.store.find("ActionAttempt", where={"action_id": action.id}, limit=100)
            last = attempts[-1] if attempts else None
            out.append(
                PendingDecision(
                    action_id=action.id,
                    case_id=action.case_id,
                    kind=action.kind,
                    action_state=str(action.state),
                    attempt_id=getattr(last, "id", None),
                    attempt_no=int(getattr(last, "attempt_no", 0) or 0),
                    failure_class=getattr(last, "failure_class", None),
                    error_text=str(getattr(last, "error_text", "") or ""),
                    idempotency_key=action.idempotency_key,
                    warning=(
                        "结果未知：未发送证明 ≠ 未发送。重试可能造成重复发送，"
                        "必须由用户显式确认重复风险并重新审批（SPEC §21.2）。"
                    ),
                )
            )
        return out

    def resolve_unknown(
        self,
        action_id: str,
        *,
        decision: str,
        actor_id: str = "owner",
        auth_channel: str = "console",
        auth_evidence: Optional[str] = None,
        acknowledged_duplicate_risk: bool = False,
        ttl_seconds: int = approval_mod.APPROVAL_TTL_SECONDS,
        reason: str = "",
    ) -> ResolutionOutcome:
        """用户对 `UNKNOWN` 的裁决（唯一的推进方式）。

        - `abandon`：`UNKNOWN → CANCELLED`，动作永久不会外发；
        - `retry`：必须 `acknowledged_duplicate_risk=True` + 认证审批，
          路径 `UNKNOWN → FAILED_SAFE → AUTHORIZED`，随后由 `execute()` 建**新 attempt**
          并**沿用同一幂等键**（保留 UNKNOWN 原记录，不伪装成安全重试）。
        """
        action = self.store.get("ExternalAction", action_id)
        if str(action.state) != "UNKNOWN":
            raise ValidationError(
                "只有 UNKNOWN 结果才需要用户裁决",
                detail={"action_id": action_id, "state": str(action.state)},
            )
        decision = (decision or "").strip().lower()

        if decision in ("abandon", "cancel", "放弃"):
            saved = self.store.transition(
                "ExternalAction",
                action_id,
                "CANCELLED",
                actor_kind=ActorKind.USER,
                actor_id=actor_id,
                reason=reason or "用户裁决：放弃重试（不接受重复发送风险）",
            )
            return ResolutionOutcome(
                action_id=action_id,
                decision="abandon",
                action_state=str(saved.state),
                reason=reason or "用户裁决：放弃重试",
                next_step="无需进一步动作；原 UNKNOWN 尝试记录保留可查",
            )

        if decision not in ("retry", "resend", "重试"):
            raise ValidationError(
                "未知裁决：只允许 retry / abandon",
                detail={"action_id": action_id, "decision": decision},
            )

        if not acknowledged_duplicate_risk:
            raise ApprovalRequired(
                "重试可能重复发送：必须先向用户显式提示重复风险并取得确认",
                detail={"action_id": action_id, "required_flag": "acknowledged_duplicate_risk"},
            )
        approval_mod.assert_authenticated_channel(auth_channel, auth_evidence)

        summary = approval_mod.summary_from_normalized(action.action_summary)
        summary.validate()
        # 重新审批：内容仍是同一份内容，但**审批是新的**（旧的一次性审批已随 UNKNOWN 作废）
        new_approval = approval_mod.create_approval(
            self.store,
            summary=summary,
            action=action,
            case_id=action.case_id,
            approver=actor_id,
            auth_channel=auth_channel,
            auth_evidence=auth_evidence,
            ttl_seconds=ttl_seconds,
        )

        armed = self.store.transition(
            "ExternalAction",
            action_id,
            "FAILED_SAFE",
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason=reason or "用户裁决：确认上次未发送（承担重复发送风险）",
        )
        armed.approval_id = new_approval.id
        # 上一任执行器已经不在了：解除占位，让新的原子领取能拿到执行权
        armed.claimed_by = None
        armed.claimed_at = None
        armed.result_class = "FAILED_SAFE"
        armed.blocking_reason = "UNKNOWN 已由用户裁决：重新审批后按同一幂等键重试"
        self.store.put(
            armed,
            expected_revision=armed.revision,
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="用户裁决后重新绑定审批并解除执行器占位",
            audit_action=str(AuditAction.RETRY),
            audit_detail={"approval_id": new_approval.id, "idempotency_key": action.idempotency_key},
        )
        saved = self.store.transition(
            "ExternalAction",
            action_id,
            "AUTHORIZED",
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="用户裁决重试：重新授权（沿用同一幂等键）",
        )
        return ResolutionOutcome(
            action_id=action_id,
            decision="retry",
            action_state=str(saved.state),
            approval_id=new_approval.id,
            duplicate_risk_acknowledged=True,
            reason=reason or "用户裁决：确认未发送并承担重复发送风险",
            next_step="调用 execute() 建新 attempt；请求沿用同一幂等键 %s" % action.idempotency_key,
        )

    # ------------------------------------------------------------------ 内部
    def _reconcile_case(self, action: ExternalAction, *, node: str) -> str:
        """把动作结论回写到 Case（只走合法转换；非法就**不**硬来）。"""
        target = CASE_TARGET_FOR_ACTION.get(str(action.state))
        if target is None:
            return ""
        case = self.store.find_one("Case", id=action.case_id)
        if case is None:
            return ""
        current = str(case.state)
        if current == target:
            return case.id
        # FAILED_SAFE 只在"正在执行"的事务上落 FAILED；UNKNOWN 裁决期间不改 Case，
        # 这样重试成功后 Case 还能从 UNKNOWN 走到 SUCCEEDED。
        if str(action.state) == "FAILED_SAFE" and current != "EXECUTING":
            return case.id
        if not tr.MACHINES["Case"].can(current, target):
            return case.id
        self.store.transition(
            "Case",
            case.id,
            target,
            actor_kind=ActorKind.SYSTEM,
            actor_id=node,
            reason="动作结论回写事务状态（%s）" % str(action.state),
        )
        return case.id

    def _record_execution(
        self,
        *,
        action: ExternalAction,
        attempt: ActionAttempt,
        node: str,
        started_at: str,
        result_class: str,
        failure_class: Optional[str],
        error_text: str,
    ) -> Execution:
        state = {
            "SUCCEEDED": ExecutionState.SUCCEEDED,
            "FAILED_SAFE": ExecutionState.FAILED,
            "UNKNOWN": ExecutionState.TIMEOUT,
        }.get(result_class, ExecutionState.FAILED)
        execution = Execution(
            case_id=action.case_id or None,
            backend=self.execution_backend,
            state=state,
            input_summary="外发动作 %s（attempt #%d）" % (action.id, attempt.attempt_no),
            output_summary="结果 %s%s" % (result_class, ("：%s" % error_text) if error_text else ""),
            read_object_ids=[action.id, action.approval_id or ""],
            write_object_ids=[attempt.id],
            started_at=started_at,
            finished_at=self.store.now(),
            failure_class=failure_class,
            error_text=error_text or None,
        )
        return self.store.record_execution(execution)


def _channel_calls(channel: Any) -> Optional[int]:
    """尽最大努力拿到"这个通道被调用了几次"；拿不到返回 `None`（不猜）。

    优先 `call_count`（本模块的适配器都提供），其次 `sent` 列表长度
    （冻结契约的 `RecordingMailSender` 就是这个形状）。
    """
    if channel is None:
        return None
    counter = getattr(channel, "call_count", None)
    if isinstance(counter, int):
        return counter
    sent = getattr(channel, "sent", None)
    if isinstance(sent, list):
        return len(sent)
    return None


def _normalize_send_result(result: Any) -> Tuple[str, str]:
    """把通道返回值归一成 `(result_class, mismatch)`；不一致时**降级**到 UNKNOWN。

    "说成功了但 `accepted=False`"这种自相矛盾的返回值不能当成功用（fail-closed）。
    """
    if isinstance(result, MailSendResult):
        raw_class = str(result.result_class)
        accepted = bool(result.accepted)
    else:
        raw_class = str(getattr(result, "result_class", "") or "")
        accepted = bool(getattr(result, "accepted", False))
    if raw_class not in RESULT_CLASSES:
        return "UNKNOWN", "通道返回了未登记的结果分类: %r" % (raw_class,)
    if raw_class == "SUCCEEDED" and not accepted:
        return "UNKNOWN", "通道自称 SUCCEEDED 但 accepted=False：按结果未知处理"
    if raw_class != "SUCCEEDED" and accepted:
        return "UNKNOWN", "通道自称 %s 但 accepted=True：按结果未知处理" % raw_class
    return raw_class, ""


def _result_reason(result_class: str, external_response: Dict[str, Any], error_text: str) -> str:
    if result_class == "SUCCEEDED":
        return "远端已接受（有接受证据）"
    if result_class == "FAILED_SAFE":
        return "确定未发生副作用：%s" % (error_text or external_response.get("note") or "通道证明未提交")
    return "结果未知：禁止自动重试，等待用户裁决；%s" % (error_text or "通道无法证明是否被接受")


def _error_for(code: str, reason: str, detail: Dict[str, Any]) -> AsistentError:
    """把拒绝码还原成契约错误对象（`raise_on_reject=True` 时用）。"""
    mapping = {
        "ACTION_NOT_CLAIMABLE": ActionNotClaimable,
        "APPROVAL_INVALID": ApprovalInvalid,
        "APPROVAL_REQUIRED": ApprovalRequired,
        "VALIDATION_ERROR": ValidationError,
        "NOT_FOUND": NotFound,
        "EXTERNAL_RESULT_UNKNOWN": ValidationError,
        "OUTBOUND_CHANNEL_MISSING": ValidationError,
    }
    cls = mapping.get(code)
    if cls is None:
        return AsistentError(reason, detail=detail)
    return cls(reason, detail=detail)


def approve_and_authorize(
    store: Any,
    *,
    summary: ActionSummary,
    action: ExternalAction,
    approver: str = "owner",
    auth_channel: str = "console",
    auth_evidence: Optional[str] = None,
    ttl_seconds: int = approval_mod.APPROVAL_TTL_SECONDS,
) -> ExternalAction:
    """便捷组合：生成审批 → 绑定 → `AUTHORIZED`（等价于控制台点"批准并发"）。"""
    approval = approval_mod.create_approval(
        store,
        summary=summary,
        action=action,
        case_id=action.case_id,
        approver=approver,
        auth_channel=auth_channel,
        auth_evidence=auth_evidence,
        ttl_seconds=ttl_seconds,
    )
    return approval_mod.bind_and_authorize(
        store, action_id=action.id, approval_id=approval.id, actor_id=approver
    )


def make_engine(store: Any, **kwargs: Any) -> ActionEngine:
    """默认装配：脚本型假通道 + 引擎（真实通道接入必须显式传入 `sender`）。"""
    sender = kwargs.pop("sender", None)
    if sender is None:
        sender = ScriptedMailSender()
    return ActionEngine(store, sender, **kwargs)
