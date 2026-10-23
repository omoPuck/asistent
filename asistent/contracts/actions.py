"""外部动作的规范化摘要与审批绑定（SPEC §5.6、§8.3、§8.4、§21.2）。

要点：
- Approval 绑定的是**规范化动作摘要**，不是"这个草稿"这个概念；
- 摘要至少含：发件账号、To/Cc/Bcc、主题、正文、附件内容哈希、目标线程、
  草稿版本、关键依据版本、有效期；
- 任何一个字段变化 → 摘要变化 → 旧审批必须失效。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .enums import ActionState, ApprovalState, PermissionDecision
from .errors import ApprovalInvalid, ValidationError
from .ids import digest_of
from .timeutil import is_expired

#: 需要用户逐次批准的副作用类型（SPEC §21.3）
PER_ACTION_APPROVAL_KINDS = frozenset({"send_email", "submit_form", "external_delete", "external_overwrite"})
#: 可在预先授权范围内自动执行的动作（本地计算类）
AUTO_KINDS = frozenset({"local_search", "local_analyze", "draft_candidate", "index_update", "audit_write", "archive_upload"})
#: 允许的外部动作类型白名单
ACTION_KINDS = PER_ACTION_APPROVAL_KINDS | AUTO_KINDS

#: 审批默认有效期（秒）。契约 v0.6（CP-20261010-16）：这个默认值是**契约的一部分**，
#: 不再由各实现各自硬编码；调用方显式传入 `expires_at` 时以传入值为准。
#: 显式常量的意义：`approve()` 的签名看不出 TTL，"审批多久后失效"曾经只存在于实现细节里。
DEFAULT_APPROVAL_TTL_SECONDS = 24 * 3600


def normalize_text(value: str) -> str:
    """正文规范化：统一换行、去行尾空白、去首尾空白。用于摘要比较。"""
    lines = [line.rstrip() for line in (value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


def normalize_address_list(values: Optional[List[str]]) -> List[str]:
    """地址列表规范化：小写、去空白、去重、保持首次出现顺序。"""
    seen: List[str] = []
    for raw in values or []:
        item = (raw or "").strip().lower()
        if item and item not in seen:
            seen.append(item)
    return seen


@dataclass
class AttachmentRef:
    """附件引用：以**内容哈希**参与审批绑定（不依赖文件名/大小）。"""

    name: str
    content_hash: str
    size_bytes: int = 0
    content_type: str = "application/octet-stream"
    source_object_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        if not self.content_hash:
            raise ValidationError("附件必须有内容哈希才能参与审批绑定")
        return {
            "name": self.name,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "source_object_id": self.source_object_id,
        }


@dataclass
class ActionSummary:
    """规范化动作摘要：审批绑定与"是否发生变化"判定的唯一依据。"""

    kind: str
    target_ref: str = ""
    from_account: str = ""
    to: List[str] = field(default_factory=list)
    cc: List[str] = field(default_factory=list)
    bcc: List[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""
    attachments: List[AttachmentRef] = field(default_factory=list)
    target_thread_ref: str = ""
    draft_version_id: Optional[str] = None
    evidence_versions: Dict[str, str] = field(default_factory=dict)
    #: 关键参数（例如表单字段、目标目录）
    parameters: Dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "target_ref": (self.target_ref or "").strip(),
            "from_account": (self.from_account or "").strip().lower(),
            "to": normalize_address_list(self.to),
            "cc": normalize_address_list(self.cc),
            "bcc": normalize_address_list(self.bcc),
            "subject": normalize_text(self.subject),
            "body": normalize_text(self.body),
            "attachments": sorted((a.to_dict() for a in self.attachments), key=lambda a: (a["name"], a["content_hash"])),
            "target_thread_ref": (self.target_thread_ref or "").strip(),
            "draft_version_id": self.draft_version_id,
            "evidence_versions": {k: str(v) for k, v in sorted(self.evidence_versions.items())},
            "parameters": {k: self.parameters[k] for k in sorted(self.parameters)},
        }

    def digest(self) -> str:
        """规范化摘要的 sha256 —— 审批绑定值。"""
        return digest_of(self.normalized())

    def diff_keys(self, other: "ActionSummary") -> List[str]:
        """列出两个摘要之间发生变化的字段名（用于解释审批为何失效）。"""
        left, right = self.normalized(), other.normalized()
        return sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))

    def validate(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise ValidationError(f"未知的动作类型: {self.kind}")
        if self.kind == "send_email":
            if not self.to:
                raise ValidationError("发送邮件动作必须至少有 1 个收件人")
            if not self.from_account:
                raise ValidationError("发送邮件动作必须绑定发件账号")
            if not self.draft_version_id:
                raise ValidationError("发送邮件动作必须绑定确定 DraftVersion")
        for attachment in self.attachments:
            attachment.to_dict()


@dataclass
class ApprovalBinding:
    """审批绑定关系：把 Approval 与"具体内容版本"钉在一起。"""

    action_summary: ActionSummary
    target_object_ids: List[str]
    approval_id: Optional[str] = None
    approved_digest: str = ""
    expires_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "approved_digest": self.approved_digest,
            "expires_at": self.expires_at,
            "target_object_ids": list(self.target_object_ids),
            "action_summary": self.action_summary.normalized(),
        }


def check_approval_valid(
    *,
    approval_state: str,
    approved_digest: str,
    current_summary: ActionSummary,
    expires_at: Optional[str],
    now: Optional[str] = None,
    revoked_reason: Optional[str] = None,
    approved_summary: Optional[ActionSummary] = None,
) -> None:
    """审批可用性检查（SPEC §8.4、§21.2）。任一条件不满足即抛 ApprovalInvalid。

    契约 v0.6（CP-20261010-13）：`detail["changed_fields"]` 只有在**同时拿到**"获批摘要"
    与"当前摘要"时才能算出来。旧实现只拿到 `approved_digest`（一个字符串），于是把自己的
    摘要和自己求差，该字段**恒为 `[]`**——调用方看到"没有字段变化"这个错误信息，而真实情况
    恰恰是内容变了。现在：传入 `approved_summary` 就给出真实的变化字段；不传则该字段为
    `None`，**显式表示"未知"**（绝不用空列表冒充"没有变化"）。
    """
    if ApprovalState.coerce(approval_state) != ApprovalState.ACTIVE:
        raise ApprovalInvalid(
            f"审批状态不可用: {approval_state}",
            detail={"state": approval_state, "revoked_reason": revoked_reason},
        )
    if expires_at and is_expired(expires_at, now=now):
        raise ApprovalInvalid("审批已过期", detail={"expires_at": expires_at})
    current = current_summary.digest()
    if approved_digest != current:
        changed: Optional[List[str]] = None
        if approved_summary is not None:
            changed = approved_summary.diff_keys(current_summary)
        raise ApprovalInvalid(
            "审批绑定的内容已变化，必须重新确认",
            detail={"approved_digest": approved_digest, "current_digest": current, "changed_fields": changed},
        )


def permission_decision(kind: str, *, archive_policy_covers: bool = False, script_authorized: bool = False) -> PermissionDecision:
    """按动作类型给出权限裁决（SPEC §21.3 表格）。"""
    if kind in PER_ACTION_APPROVAL_KINDS:
        return PermissionDecision.REQUIRE_APPROVAL
    if kind in {"archive_upload"}:
        return PermissionDecision.AUTO if archive_policy_covers else PermissionDecision.REQUIRE_APPROVAL
    if kind in {"local_search", "local_analyze", "draft_candidate", "index_update", "audit_write"}:
        return PermissionDecision.AUTO
    if kind.startswith("script:") and not script_authorized:
        return PermissionDecision.FORBIDDEN
    return PermissionDecision.REQUIRE_APPROVAL


def can_claim_action(state: str) -> bool:
    """只有 AUTHORIZED 才能被原子领取外呼（SPEC §21.2 状态表）。"""
    return ActionState.coerce(state) == ActionState.AUTHORIZED
