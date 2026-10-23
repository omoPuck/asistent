"""契约测试公共夹具。

存储实现的切换点：环境变量 `ASISTENT_STORE`（默认 `reference`）。
MVP-0 完成后实现 `sqlite`，同一组测试会对两个实现各跑一遍，
以此证明「原型没有用临时私有对象替代共享对象」。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from asistent.contracts import (
    ActionSummary,
    Approval,
    AttachmentRef,
    Case,
    ExternalAction,
    ReferenceStore,
    SourceDocument,
    action_idempotency_key,
    utcnow_iso,
)
from asistent.contracts.timeutil import add_seconds


def store_factory(**kwargs: Any):
    """按环境变量选择被测存储实现。"""
    kind = os.environ.get("ASISTENT_STORE", "reference").strip().lower()
    if kind in ("", "reference", "memory", "mem"):
        return ReferenceStore(**kwargs)
    if kind == "sqlite":
        from asistent.storage.sqlite_store import SqliteStore  # type: ignore

        return SqliteStore(**kwargs)
    raise RuntimeError(f"未知存储实现: {kind}")


def make_case(store, *, goal: str = "处理一封需要回复的通知邮件") -> Case:
    return store.put(Case(goal=goal))


def make_action(
    store,
    *,
    case: Case,
    body: str = "您好，我确认参加。",
    to: Optional[list] = None,
    bcc: Optional[list] = None,
    subject: str = "Re: 活动通知",
    attachment: Optional[AttachmentRef] = None,
    kind: str = "send_email",
    approve: bool = True,
    expires_in: int = 24 * 3600,
) -> Tuple[ExternalAction, Optional[Approval], ActionSummary]:
    """构造「已准备 + 已批准」的外部动作，供幂等/审批测试使用。"""
    summary = ActionSummary(
        kind=kind,
        from_account="owner@example.com",
        to=to if to is not None else ["organizer@example.org"],
        bcc=bcc or [],
        subject=subject,
        body=body,
        draft_version_id="dv_0001",
        target_thread_ref="thread-1",
        evidence_versions={"policy": "rule-1@v1"},
        attachments=[attachment] if attachment else [],
    )
    summary.validate()
    action = store.put(
        ExternalAction(
            case_id=case.id,
            kind=kind,
            idempotency_key="pending",
            action_summary=summary.normalized(),
            summary_digest=summary.digest(),
            target_ref=summary.to[0] if summary.to else "",
            connector="mail:mcp-email-server",
            prepared_at=utcnow_iso(),
        )
    )
    action.idempotency_key = action_idempotency_key(action.id)
    store.put(action, expected_revision=action.revision, reason="分配稳定幂等键")

    case_obj = store.get("Case", case.id)
    if action.id not in case_obj.action_ids:
        case_obj.action_ids.append(action.id)
        store.put(case_obj, expected_revision=case_obj.revision, reason="关联动作")

    approval: Optional[Approval] = None
    if approve:
        approval = store.approve(
            Approval(
                case_id=case.id,
                action_id=action.id,
                approver="owner",
                approved_action_digest=summary.digest(),
                target_object_ids=[action.id],
                content_version_refs={"draft": "dv_0001"},
                created_at_approval=utcnow_iso(),
                expires_at=add_seconds(utcnow_iso(), expires_in),
                auth_channel="console",
                auth_evidence="console-session:test-session",
                summary_shown=summary.normalized(),
            )
        )
        action.approval_id = approval.id
        store.put(action, expected_revision=action.revision, reason="绑定审批")
        store.transition("ExternalAction", action.id, "AUTHORIZED", actor_kind="USER", actor_id="owner", reason="审批通过后授权")
    return store.get("ExternalAction", action.id), approval, summary


def make_document(store, *, path: str = "资料/通知.md", content_hash: str = "hash-1", identity: str = "mail-1") -> SourceDocument:
    return store.put(
        SourceDocument(
            source_type="email",
            source_system="imap:owner@example.com",
            source_id=identity,
            identity_key=identity,
            origin_path=path,
            content_hash=content_hash,
            received_at=utcnow_iso(),
        ),
        unique=("mail_identity", identity),
    )
