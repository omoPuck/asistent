"""审批的生成、校验与失效（SPEC §5.6、§8.4、§21.2、§21.3；契约 `asistent.contracts.actions`）。

冻结契约已经钉住的部分（本模块**复用**，不重写、不改动）：

- `ActionSummary.digest()` 是审批绑定的**唯一**摘要口径；
- `check_approval_valid(...)` 是"审批能不能用"的契约判定（状态 / 过期 / 摘要一致）；
- `store.approve` / `store.invalidate_approval` 负责持久化与状态机合法性。

本模块补的是契约没有覆盖、但 MVP-5 必须有的三件事：

1. **审批只能由认证控制操作产生**：`chat` / 通知正文里的"同意/发送"必须被拒绝（§21.3 第 1017 行）；
2. **绑定"确定的 DraftVersion"**：把草稿版本 ID + 草稿 ID + 版本号 + 正文规范化摘要
   一起写进 `Approval.content_version_refs`，这样"草稿又改了一版"能被**读出来**，
   而不是只靠一个 `draft_version_id` 字符串自证；
3. **可读的失效原因**：`check_action_approval` 返回"为什么不可用"的列表。

> 关于 `changed_fields`（CP-20261010-13，契约 v0.6 已修）：
> 旧契约实现在摘要不一致时调用 `current_summary.diff_keys(current_summary)` —— **拿自己和自己比**，
> 因此 `detail["changed_fields"]` 永远为空，调用方读到的是"没有字段变化"这个相反的结论。
> 契约 v0.6 起 `check_approval_valid(..., approved_summary=...)` 会给出真实变化字段，
> 拿不到获批摘要时该字段为 `None`（显式未知，不再用空列表冒充"没有变化"）。
> 本模块仍保留基于 `Approval.summary_shown` 的本地还原：`ApprovalCheck.changed_fields` 是
> 面向控制台的**稳定输出字段**，需要在"审批根本不可用"（被撤销/过期）时也能给出上下文，
> 而契约函数只在摘要不一致这一条分支上给得出。两条路径由
> `tests/actions/test_approval_binding.py::ContractChangedFieldsHoleTest` 同时钉住。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..contracts.actions import ActionSummary, AttachmentRef, check_approval_valid
from ..contracts.enums import ActorKind, ApprovalState
from ..contracts.errors import ApprovalInvalid, NotFound, PermissionDenied, ValidationError
from ..contracts.ids import digest_of, sha256_bytes
from ..contracts.objects import Approval, DraftVersion, ExternalAction
from ..contracts.timeutil import add_seconds, is_expired

__all__ = [
    "APPROVAL_TTL_SECONDS",
    "NON_AUTHENTICATED_CHANNELS",
    "ApprovalCheck",
    "summary_from_normalized",
    "normalized_body_sha256",
    "draft_version_binding",
    "require_draft_version",
    "assert_summary_matches_draft_version",
    "assert_authenticated_channel",
    "create_approval",
    "bind_and_authorize",
    "check_action_approval",
    "revoke_approval",
    "enforce_version_binding",
    "update_action_content",
    "sweep_expired_approvals",
    "invalidate_stale_approvals",
    "content_binding_digest",
    "is_outbound_kind",
]

#: 默认有效期 24 小时（SPEC §21.2 / 契约 §5）
APPROVAL_TTL_SECONDS = 24 * 3600

#: 不构成审批的认证渠道：聊天、通知正文、纯文本上下文（§21.3）
NON_AUTHENTICATED_CHANNELS = frozenset(
    {
        "chat",
        "conversation",
        "chat_text",
        "plain_text",
        "text",
        "message",
        "mail_body",
        "notification",
        "email_body",
    }
)

_ATTACHMENT_FIELDS = ("name", "content_hash", "size_bytes", "content_type", "source_object_id")


# --------------------------------------------------------------------------------------
# 摘要还原与"确定版本"绑定
# --------------------------------------------------------------------------------------


def summary_from_normalized(payload: Dict[str, Any]) -> ActionSummary:
    """把 `ActionSummary.normalized()` 的产物还原成对象（用于比较与解释失效原因）。

    刻意用 `AttachmentRef(**...)` 逐字段重建，而不是直接 `ActionSummary(**payload)`：
    这样新增字段时不会静默吞掉未知键。
    """
    if not payload:
        raise ValidationError("动作摘要为空，无法还原（动作必须先持久化规范化摘要）")
    unknown = set(payload) - {
        "kind",
        "target_ref",
        "from_account",
        "to",
        "cc",
        "bcc",
        "subject",
        "body",
        "attachments",
        "target_thread_ref",
        "draft_version_id",
        "evidence_versions",
        "parameters",
    }
    if unknown:
        raise ValidationError("动作摘要含未登记字段: %s" % (sorted(unknown),))
    attachments: List[AttachmentRef] = []
    for item in payload.get("attachments") or []:
        missing = [name for name in ("name", "content_hash") if not item.get(name)]
        if missing:
            raise ValidationError("附件引用缺少 %s" % (missing,))
        attachments.append(
            AttachmentRef(**{name: item.get(name) for name in _ATTACHMENT_FIELDS})
        )
    return ActionSummary(
        kind=str(payload.get("kind", "")),
        target_ref=str(payload.get("target_ref", "") or ""),
        from_account=str(payload.get("from_account", "") or ""),
        to=list(payload.get("to") or []),
        cc=list(payload.get("cc") or []),
        bcc=list(payload.get("bcc") or []),
        subject=str(payload.get("subject", "") or ""),
        body=str(payload.get("body", "") or ""),
        attachments=attachments,
        target_thread_ref=str(payload.get("target_thread_ref", "") or ""),
        draft_version_id=payload.get("draft_version_id"),
        evidence_versions=dict(payload.get("evidence_versions") or {}),
        parameters=dict(payload.get("parameters") or {}),
    )


def normalized_body_sha256(body: str) -> str:
    """正文的规范化摘要：与 `ActionSummary.normalized()["body"]` 同口径。

    统一换行、去行尾空白、去首尾空白后再哈希 —— 因此"只改换行"不会让审批失效
    （冻结契约测试 `test_body_whitespace_only_difference_does_not_invalidate` 已钉住这一点）。
    """
    from ..contracts.actions import normalize_text

    return sha256_bytes(normalize_text(body).encode("utf-8"))


def require_draft_version(store: Any, draft_version_id: Optional[str]) -> DraftVersion:
    """取确定版本的 `DraftVersion`；取不到直接拒绝（宁可拒绝，不猜）。"""
    if not draft_version_id:
        raise ValidationError("审批必须绑定确定的 DraftVersion（draft_version_id 不能为空）")
    found = store.find_one("DraftVersion", id=draft_version_id)
    if found is None:
        raise NotFound(
            "审批绑定的 DraftVersion 不存在：不能对抽象草稿审批",
            detail={"draft_version_id": draft_version_id},
        )
    return found


def draft_version_binding(store: Any, draft_version_id: Optional[str]) -> Dict[str, str]:
    """确定版本的持久绑定材料（写进 `Approval.content_version_refs`）。

    含：草稿 ID、版本 ID、版本号、正文规范化 sha256、该草稿当前指向的版本。
    这样"批准的是哪个版本"不靠调用方自觉，而是**可回读、可复核**的记录。
    """
    version = require_draft_version(store, draft_version_id)
    draft = store.find_one("Draft", id=version.draft_id)
    return {
        "draft_id": str(version.draft_id),
        "draft_version_id": str(version.id),
        "draft_version_no": str(version.version_no),
        "draft_body_sha256": normalized_body_sha256(version.body),
        "draft_current_version_id": str(getattr(draft, "current_version_id", "") or ""),
    }


def assert_summary_matches_draft_version(store: Any, summary: ActionSummary) -> DraftVersion:
    """校验"要发的正文"就是"该 DraftVersion 的正文"，且该版本仍是草稿当前版本。

    fail-closed：任何一项对不上都抛 `ApprovalInvalid`，绝不"顺手放宽"。
    """
    version = require_draft_version(store, summary.draft_version_id)
    draft = store.find_one("Draft", id=version.draft_id)
    current = str(getattr(draft, "current_version_id", "") or "")
    if current and current != version.id:
        raise ApprovalInvalid(
            "审批必须绑定草稿的当前版本：草稿已产生新版本，请重新确认",
            detail={"draft_id": version.draft_id, "current_version_id": current, "approved_version_id": version.id},
        )
    if normalized_body_sha256(version.body) != normalized_body_sha256(summary.body):
        raise ApprovalInvalid(
            "动作正文与所绑定 DraftVersion 的正文不一致：必须重新确认",
            detail={
                "draft_version_id": version.id,
                "expected_body_sha256": normalized_body_sha256(version.body),
                "actual_body_sha256": normalized_body_sha256(summary.body),
            },
        )
    return version


# --------------------------------------------------------------------------------------
# 审批检查结果
# --------------------------------------------------------------------------------------


@dataclass
class ApprovalCheck:
    """审批可用性检查结果：既能给程序判分支，也能给人看原因。"""

    ok: bool
    approval_id: Optional[str] = None
    state: str = ""
    approved_digest: str = ""
    current_digest: str = ""
    expires_at: Optional[str] = None
    changed_fields: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    code: Optional[str] = None
    version_binding: Dict[str, str] = field(default_factory=dict)

    def reason_text(self) -> str:
        return "；".join(self.reasons) if self.reasons else "审批有效"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "approval_id": self.approval_id,
            "state": self.state,
            "approved_digest": self.approved_digest,
            "current_digest": self.current_digest,
            "expires_at": self.expires_at,
            "changed_fields": list(self.changed_fields),
            "reasons": list(self.reasons),
            "code": self.code,
            "version_binding": dict(self.version_binding),
        }


# --------------------------------------------------------------------------------------
# 生成 / 校验 / 失效
# --------------------------------------------------------------------------------------


def assert_authenticated_channel(auth_channel: str, auth_evidence: Optional[str]) -> None:
    """审批必须来自认证控制操作；聊天/通知正文一律不算（SPEC §21.3 第 1017 行）。"""
    channel = (auth_channel or "").strip().lower()
    if not channel:
        raise ValidationError("审批必须声明认证渠道（auth_channel）")
    if channel in NON_AUTHENTICATED_CHANNELS:
        raise PermissionDenied(
            "聊天/通知文本不构成审批：必须由认证控制操作批准具体版本（SPEC §21.3）",
            detail={"auth_channel": auth_channel},
        )
    if not auth_evidence:
        raise ValidationError(
            "审批必须由认证控制操作产生（缺少 auth_evidence）",
            detail={"auth_channel": auth_channel},
        )


def require_case_not_blocked(store: Any, *, case_id: str, draft_version_id: Optional[str] = None) -> None:
    """事务处于**阻塞**状态时，拒绝生成审批（契约 v0.8，CP-20261010-27）。

    这是跨层门闸，补的是完整链路里的最后一个缺口：成长层（`asistent.growth`）在规则冲突时
    不产出可发送结论、把原因写进 `Case.blocked_reason`、并让旧草稿 `OPEN→STALE`；
    但**审批路径以前完全不读这个字段**，于是"先制造冲突、再拿旧草稿直调审批"可以绕过门闸。

    为什么必须在**审批**这一层拦：审批是"人已经同意发出去"的唯一入口，任何"更早一层拦住了"
    的说法都不能覆盖这一层——只要还有一种调用顺序能走到审批，就必须在审批处判定。

    `case_id` 缺失时从 `DraftVersion → Draft.case_id` 兜底解析；两边都没有就不拦（例如
    只做摘要绑定、没有关联事务的单元场景），但**有 case 就一定检查**。
    """
    resolved = case_id
    if not resolved and draft_version_id:
        version = store.find_one("DraftVersion", id=draft_version_id)
        draft = store.find_one("Draft", id=version.draft_id) if version is not None else None
        resolved = str(getattr(draft, "case_id", "") or "")
    if not resolved:
        return
    case = store.find_one("Case", id=resolved)
    if case is None:
        return
    blocked = str(getattr(case, "blocked_reason", "") or "").strip()
    if blocked:
        raise PermissionDenied(
            "事务处于阻塞状态，不能生成审批：" + blocked,
            detail={"case_id": resolved, "case_state": str(case.state), "blocked_reason": blocked},
        )


def create_approval(
    store: Any,
    *,
    summary: ActionSummary,
    action: Optional[ExternalAction] = None,
    case_id: str = "",
    approver: str = "owner",
    auth_channel: str = "console",
    auth_evidence: Optional[str] = None,
    ttl_seconds: int = APPROVAL_TTL_SECONDS,
    verify_draft_version: bool = True,
) -> Approval:
    """由认证控制操作生成一个绑定**确定版本**的审批。

    `verify_draft_version=True`（默认）时要求：
    摘要绑定的 `DraftVersion` 已持久化、是草稿当前版本、且正文与摘要一致。
    只有复用冻结夹具（没有 DraftVersion 对象）时才显式关闭。

    v0.8（CP-20261010-27）：入口先查 `Case.blocked_reason`——阻塞中的事务**不允许**被批准，
    否则"冲突不得产出可发送结论"这条不变量可以靠换一条调用路径绕过去。
    """
    assert_authenticated_channel(auth_channel, auth_evidence)
    summary.validate()
    require_case_not_blocked(
        store,
        case_id=case_id or (action.case_id if action is not None else ""),
        draft_version_id=summary.draft_version_id,
    )
    if action is not None:
        if action.summary_digest and action.summary_digest != summary.digest():
            raise ValidationError(
                "审批摘要与动作当前摘要不一致：先把动作持久化到要批准的内容上",
                detail={"action_id": action.id, "action_summary_digest": action.summary_digest, "given_digest": summary.digest()},
            )

    refs: Dict[str, str] = {}
    if summary.kind == "send_email":
        if verify_draft_version:
            assert_summary_matches_draft_version(store, summary)
            refs = draft_version_binding(store, summary.draft_version_id)

    moment = store.now()
    approval = Approval(
        case_id=case_id or (action.case_id if action is not None else ""),
        action_id=action.id if action is not None else None,
        approver=approver,
        approved_action_digest=summary.digest(),
        target_object_ids=[action.id] if action is not None else [],
        content_version_refs=refs,
        created_at_approval=moment,
        expires_at=add_seconds(moment, ttl_seconds),
        auth_channel=auth_channel,
        auth_evidence=auth_evidence,
        summary_shown=summary.normalized(),
    )
    return store.approve(approval)


def bind_and_authorize(
    store: Any,
    *,
    action_id: str,
    approval_id: str,
    actor_id: str = "owner",
    actor_kind: str = ActorKind.USER,
    reason: str = "审批通过后授权",
) -> ExternalAction:
    """把审批绑到动作上并进入 `AUTHORIZED`（PREPARED → AUTHORIZED 只能由 USER/SYSTEM 触发）。"""
    action = store.get("ExternalAction", action_id)
    approval = store.get("Approval", approval_id)
    if approval.action_id and approval.action_id != action_id:
        raise ApprovalInvalid(
            "该审批已绑定到另一个动作：一次性审批不得复用",
            detail={"approval_id": approval_id, "bound_action_id": approval.action_id, "action_id": action_id},
        )
    if approval.approved_action_digest != action.summary_digest:
        raise ApprovalInvalid(
            "审批绑定的内容与动作当前摘要不一致",
            detail={"approval_id": approval_id, "approved": approval.approved_action_digest, "current": action.summary_digest},
        )
    action.approval_id = approval_id
    store.put(
        action,
        expected_revision=action.revision,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason="绑定审批",
    )
    saved = store.transition(
        "ExternalAction",
        action_id,
        "AUTHORIZED",
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason=reason,
    )
    return saved  # type: ignore[return-value]


def check_action_approval(store: Any, action_id: str, *, now: Optional[str] = None) -> ApprovalCheck:
    """只读检查：这个动作现在有没有"可用的一次性审批"，不可用就给出可读原因。

    **不**做领取、不做任何写入，因此可以安全地在控制台/巡检里随时调用。
    """
    action = store.get("ExternalAction", action_id)
    check = ApprovalCheck(
        ok=False,
        approved_digest="",
        current_digest=action.summary_digest,
    )
    if not action.approval_id:
        check.code = "APPROVAL_REQUIRED"
        check.reasons.append("动作没有绑定任何审批：不得执行（APPROVAL_REQUIRED）")
        return check

    approval = store.get("Approval", action.approval_id)
    check.approval_id = approval.id
    check.state = str(approval.state)
    check.approved_digest = approval.approved_action_digest
    check.expires_at = approval.expires_at
    check.version_binding = {str(k): str(v) for k, v in (approval.content_version_refs or {}).items()}

    reasons: List[str] = []

    # 1) 一次性：审批只能服务于它绑定的那一个动作
    if approval.action_id and approval.action_id != action.id:
        reasons.append("该审批已绑定到另一个动作（%s），不得复用" % approval.action_id)

    # 2) 契约判定：状态 / 过期 / 摘要一致
    try:
        current_summary = summary_from_normalized(action.action_summary)
    except ValidationError as exc:
        current_summary = None
        reasons.append("动作摘要不可用：%s" % exc.message)

    if current_summary is not None:
        try:
            check_approval_valid(
                approval_state=str(approval.state),
                approved_digest=approval.approved_action_digest,
                current_summary=current_summary,
                expires_at=approval.expires_at,
                now=now,
                revoked_reason=approval.revoked_reason,
            )
        except ApprovalInvalid as exc:
            check.code = exc.code
            reasons.append(_readable_invalid_reason(exc, approval))
            check.changed_fields = _changed_fields(approval, current_summary)
        # 3) 确定版本绑定：审批记录的草稿版本必须仍是"这一版"
        reasons.extend(_version_binding_reasons(store, approval))

    check.reasons = reasons
    check.ok = not reasons
    return check


def _readable_invalid_reason(exc: ApprovalInvalid, approval: Approval) -> str:
    state = str(approval.state)
    if state == str(ApprovalState.CONSUMED):
        return "审批已被消费（consumed_by_attempt_id=%s）：一次性审批不得二次使用" % approval.consumed_by_attempt_id
    if state == str(ApprovalState.REVOKED):
        return "审批已被用户撤销：%s" % (approval.revoked_reason or "未填原因")
    if state == str(ApprovalState.INVALIDATED):
        return "审批已失效：%s" % (approval.invalidated_reason or "内容或依据发生变化")
    if state == str(ApprovalState.EXPIRED):
        return "审批已过期：%s" % (approval.expires_at or "未设有效期")
    return exc.message


def _changed_fields(approval: Approval, current: ActionSummary) -> List[str]:
    """真实的变化字段：用批准当时的 `summary_shown` 快照与当前摘要对比。

    契约的 `check_approval_valid` 里 `diff_keys(current_summary)` 是自比自，永远为空，
    所以这里自己算（不修改契约，见模块 docstring 的 CP 记录）。
    """
    if not approval.summary_shown:
        return []
    try:
        approved = summary_from_normalized(approval.summary_shown)
    except ValidationError:
        return []
    return current.diff_keys(approved)


def _version_binding_reasons(store: Any, approval: Approval) -> List[str]:
    refs = {str(k): str(v) for k, v in (approval.content_version_refs or {}).items()}
    version_id = refs.get("draft_version_id")
    if not version_id:
        return []
    version = store.find_one("DraftVersion", id=version_id)
    if version is None:
        return ["审批绑定的 DraftVersion %s 已不存在：必须重新确认" % version_id]
    reasons: List[str] = []
    draft = store.find_one("Draft", id=version.draft_id)
    current = str(getattr(draft, "current_version_id", "") or "")
    if current and current != version.id:
        reasons.append("草稿已产生新版本（当前 %s，已批准 %s）：必须重新确认" % (current, version.id))
    recorded = refs.get("draft_body_sha256")
    actual = normalized_body_sha256(version.body)
    if recorded and recorded != actual:
        reasons.append("草稿版本内容与审批时不一致（正文摘要变化）：必须重新确认")
    return reasons


def _rollback_authorized(store: Any, *, approval_id: str, reason: str, actor_id: str) -> List[str]:
    """审批作废后，把仍停在 `AUTHORIZED` 的动作回落 `PREPARED`（等待重新确认）。

    `AUTHORIZED` 的含义是"当前批准有效且未消费"（§21.2 表格）；批准作废后继续停在
    `AUTHORIZED` 就是一个自相矛盾的状态，而且会挡住"重新批准"（`AUTHORIZED → AUTHORIZED`
    不是合法转换）。回落只缩小权限，不放宽任何东西。
    """
    rolled_back: List[str] = []
    for action in store.find("ExternalAction", where={"approval_id": approval_id}, limit=100):
        if str(action.state) != "AUTHORIZED":
            continue
        store.transition(
            "ExternalAction",
            action.id,
            "PREPARED",
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason=reason,
        )
        rolled_back.append(action.id)
    return rolled_back


def revoke_approval(store: Any, *, approval_id: str, reason: str, actor_id: str = "owner") -> Tuple[str, List[str]]:
    """用户撤销审批：审批转 `REVOKED`，仍处于 `AUTHORIZED` 的相关动作回落 `PREPARED`。

    返回 `(审批状态, 回落的动作 ID 列表)`。
    """
    approval = store.invalidate_approval(approval_id, reason=reason, state=str(ApprovalState.REVOKED))
    rolled_back = _rollback_authorized(
        store,
        approval_id=approval_id,
        reason="审批被撤销，动作回落等待重新批准",
        actor_id=actor_id,
    )
    return str(approval.state), rolled_back


def enforce_version_binding(store: Any, action_id: str) -> List[str]:
    """收紧检查：审批绑定的 DraftVersion 是否还是这个动作要发的那一版。

    不是 → 把审批落成 `INVALIDATED`、把仍在 `AUTHORIZED` 的动作回落 `PREPARED`，
    并返回可读原因。**只收紧，不放宽**：这个函数永远不会让一个被挡住的动作变得可执行。

    为什么要它：`claim_action` 只比较"动作摘要"，看不见"草稿已经又改了一版"。
    SPEC §16 MVP-5 要求"草稿变化 → 旧 Approval 失效且必须重新确认"，
    因此这一步必须在**外呼之前**执行，并且把失效落成可观察的状态而不是现算。
    """
    action = store.get("ExternalAction", action_id)
    if not action.approval_id:
        return []
    approval = store.find_one("Approval", id=action.approval_id)
    if approval is None:
        return ["动作绑定的审批 %s 已不存在" % action.approval_id]
    reasons = _version_binding_reasons(store, approval)
    if not reasons:
        return []
    if str(approval.state) == str(ApprovalState.ACTIVE):
        store.invalidate_approval(
            approval.id, reason="；".join(reasons), state=str(ApprovalState.INVALIDATED)
        )
    if str(action.state) == "AUTHORIZED":
        store.transition(
            "ExternalAction",
            action_id,
            "PREPARED",
            actor_kind=ActorKind.USER,
            actor_id="system",
            reason="审批绑定的草稿版本已变化，动作回落等待重新确认",
        )
    return reasons


def update_action_content(
    store: Any,
    *,
    action_id: str,
    summary: ActionSummary,
    actor_kind: str = ActorKind.AGENT,
    actor_id: str = "agent",
    reason: str = "草稿内容更新",
) -> List[Tuple[str, str]]:
    """动作内容变化入口（"草稿又改了一版"）：先更新摘要，再让旧审批显式失效。

    已进入不可逆状态（`IN_FLIGHT` / `SUCCEEDED` / `UNKNOWN`）的动作**拒绝**改内容：
    那已经不是"改草稿"，而是"换了另一桩外部动作"，必须新建动作（§21.2 第 998 行）。
    """
    action = store.get("ExternalAction", action_id)
    if str(action.state) in ("IN_FLIGHT", "SUCCEEDED", "UNKNOWN"):
        raise ValidationError(
            "动作已进入不可逆状态（%s），不得改内容；需要再发必须新建动作" % str(action.state),
            detail={"action_id": action_id, "state": str(action.state)},
        )
    summary.validate()
    action.action_summary = summary.normalized()
    action.summary_digest = summary.digest()
    store.put(
        action,
        expected_revision=action.revision,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason=reason,
    )
    return invalidate_stale_approvals(store, action_id=action_id, summary=summary)


def sweep_expired_approvals(store: Any, *, now: Optional[str] = None, limit: int = 1000) -> List[str]:
    """把"已过期但仍标记 ACTIVE"的审批落成 `EXPIRED`（只收紧，不放宽）。

    执行路径不依赖它（`claim_action` 自己会拒绝过期审批）；它的价值是让
    "为什么现在不能发"在控制台里是一个**持久状态**，而不是每次现算。
    """
    expired: List[str] = []
    for approval in store.find(
        "Approval", where={"state": str(ApprovalState.ACTIVE)}, limit=limit
    ):
        if approval.expires_at and is_expired(approval.expires_at, now=now):
            store.invalidate_approval(
                approval.id,
                reason="审批已过期（%s）" % approval.expires_at,
                state=str(ApprovalState.EXPIRED),
            )
            _rollback_authorized(
                store,
                approval_id=approval.id,
                reason="审批已过期，动作回落等待重新确认",
                actor_id="system",
            )
            expired.append(approval.id)
    return expired


def invalidate_stale_approvals(
    store: Any,
    *,
    action_id: str,
    summary: ActionSummary,
    now: Optional[str] = None,
    limit: int = 100,
) -> List[Tuple[str, str]]:
    """内容/收件人/依据变化后，把该动作上绑定的旧审批显式失效。

    返回 `[(approval_id, 可读原因)]`。理由：SPEC §16 MVP-5 要求"旧 Approval 失效且必须
    重新确认"——失效必须是**可观察的状态变化**，不能只是执行时才发现摘要对不上。
    """
    action = store.get("ExternalAction", action_id)
    current_digest = summary.digest()
    stale: List[Tuple[str, str]] = []
    candidates: List[Approval] = []
    if action.approval_id:
        bound = store.find_one("Approval", id=action.approval_id)
        if bound is not None:
            candidates.append(bound)
    for approval in store.find("Approval", where={"action_id": action_id}, limit=limit):
        if all(approval.id != item.id for item in candidates):
            candidates.append(approval)
    for approval in candidates:
        if str(approval.state) != str(ApprovalState.ACTIVE):
            continue
        if approval.approved_action_digest == current_digest:
            continue
        reason = _stale_reason(approval, summary, current_digest, now=now)
        store.invalidate_approval(approval.id, reason=reason, state=str(ApprovalState.INVALIDATED))
        stale.append((approval.id, reason))
    if stale:
        refreshed = store.get("ExternalAction", action_id)
        if str(refreshed.state) == "AUTHORIZED":
            store.transition(
                "ExternalAction",
                action_id,
                "PREPARED",
                actor_kind=ActorKind.USER,
                actor_id="system",
                reason="已批准内容发生变化，动作回落等待重新确认",
            )
    return stale


def _stale_reason(approval: Approval, summary: ActionSummary, current_digest: str, *, now: Optional[str] = None) -> str:
    parts: List[str] = ["审批绑定的内容已变化，必须重新确认"]
    changed = _changed_fields(approval, summary)
    if changed:
        parts.append("变化字段: %s" % (",".join(changed),))
    else:
        parts.append("摘要 %s → %s" % (approval.approved_action_digest[:12], current_digest[:12]))
    if approval.expires_at and is_expired(approval.expires_at, now=now):
        parts.append("同时已过期(%s)" % approval.expires_at)
    return "；".join(parts)


def content_binding_digest(refs: Dict[str, str]) -> str:
    """版本绑定的整体摘要（便于在报告/审计里用一行表示"批准了哪一版内容"）。"""
    return digest_of({str(k): str(v) for k, v in sorted((refs or {}).items())})


def is_outbound_kind(kind: str) -> bool:
    """该动作是否需要逐次审批并在外呼前持久化（SPEC §21.3 表格）。"""
    from ..contracts.actions import PER_ACTION_APPROVAL_KINDS

    return kind in PER_ACTION_APPROVAL_KINDS
