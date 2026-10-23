"""外部动作与幂等（MVP-5；SPEC §5.2/§5.4、§21.2、§21.4；契约 §5、§8）。

模块边界：

| 模块 | 职责 | 不做的事 |
|---|---|---|
| `senders` | 可插拔受控外发适配器 + 外发调用账本 | 不决定能不能发、不做真去重 |
| `approval` | 审批生成/校验/失效（确定 DraftVersion 绑定、可读原因） | 不领取、不外呼 |
| `engine` | 原子领取 → 持久化 → 外呼 → 分类 → 恢复 → 用户裁决 | 不绕过 `store.claim_action` 自己搞锁 |

最硬的一条：**外呼之前动作必须已持久化（attempt_id + IN_FLIGHT）**，
且领取必须是一次原子调用（SPEC §984、§21.2）。见 `engine.ActionEngine.execute`。
"""

from __future__ import annotations

from . import approval, engine, senders
from .approval import (
    APPROVAL_TTL_SECONDS,
    ApprovalCheck,
    assert_authenticated_channel,
    assert_summary_matches_draft_version,
    bind_and_authorize,
    check_action_approval,
    create_approval,
    draft_version_binding,
    enforce_version_binding,
    invalidate_stale_approvals,
    normalized_body_sha256,
    revoke_approval,
    summary_from_normalized,
    sweep_expired_approvals,
    update_action_content,
)
from .engine import (
    ActionEngine,
    ExecutionOutcome,
    PendingDecision,
    RecoveryOutcome,
    ResolutionOutcome,
    approve_and_authorize,
    make_engine,
)
from .senders import (
    MailCallLog,
    OutboundError,
    OutboundRejected,
    OutboundTimeout,
    OutboundUnavailable,
    RESULT_CLASSES,
    ScriptedMailSender,
    SendScriptStep,
    SimulatedCrash,
    classify_sender_exception,
)

__all__ = [
    "approval",
    "engine",
    "senders",
    "ActionEngine",
    "ExecutionOutcome",
    "RecoveryOutcome",
    "ResolutionOutcome",
    "PendingDecision",
    "make_engine",
    "approve_and_authorize",
    "APPROVAL_TTL_SECONDS",
    "ApprovalCheck",
    "create_approval",
    "bind_and_authorize",
    "check_action_approval",
    "draft_version_binding",
    "assert_summary_matches_draft_version",
    "assert_authenticated_channel",
    "enforce_version_binding",
    "update_action_content",
    "revoke_approval",
    "invalidate_stale_approvals",
    "sweep_expired_approvals",
    "summary_from_normalized",
    "normalized_body_sha256",
    "MailCallLog",
    "ScriptedMailSender",
    "SendScriptStep",
    "SimulatedCrash",
    "OutboundError",
    "OutboundRejected",
    "OutboundTimeout",
    "OutboundUnavailable",
    "classify_sender_exception",
    "RESULT_CLASSES",
]
