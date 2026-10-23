"""ID 与幂等键（SPEC §7.3、§21.1、§21.2）。

规则：
- 对象 ID 由核心分配，模型不得自造；
- 幂等键必须是**确定性派生**的（同一业务输入 → 同一键），重试沿用同一个键；
- 邮件身份优先用供应商不可变 ID；Message-ID 只能作为线程/重复候选，不能单独合并不同账号的邮件。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, Iterable, Optional

#: 对象 ID 前缀表（新增对象类型必须在这里登记，由基础/集成角色维护）
ID_PREFIXES: Dict[str, str] = {
    "SourceDocument": "doc",
    "Person": "per",
    "Organization": "org",
    "ContactPoint": "cp",
    "Conversation": "conv",
    "Case": "case",
    "Task": "task",
    "Fact": "fact",
    "Inference": "inf",
    "Rule": "rule",
    "Memory": "mem",
    "Workflow": "wf",
    "WorkflowVersion": "wfv",
    "Draft": "draft",
    "DraftVersion": "dv",
    "Question": "q",
    "Answer": "ans",
    "Approval": "appr",
    "ExternalAction": "act",
    "ActionAttempt": "att",
    "ArchiveJob": "arc",
    "ArchiveManifest": "arcm",
    "RestoreJob": "rstr",
    "AuditEvent": "audit",
    "Execution": "exec",
    "ToolCall": "tc",
    "Event": "evt",
    "FileArtifact": "file",
}


def new_id(object_type: str, *, token: Optional[str] = None) -> str:
    """分配对象 ID。测试可传 `token` 得到确定性 ID。"""
    try:
        prefix = ID_PREFIXES[object_type]
    except KeyError as exc:  # pragma: no cover - 契约误用
        raise KeyError(f"未登记的对象类型: {object_type}") from exc
    suffix = token if token is not None else uuid.uuid4().hex
    return f"{prefix}_{suffix}"


def canonical_json(payload: Any) -> str:
    """规范化 JSON：排序键、无多余空白、UTF-8 保真 —— 所有摘要都基于它。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def digest_of(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def idempotency_key(namespace: str, *parts: Any) -> str:
    """确定性幂等键：`namespace` + 规范化部件。"""
    if not namespace:
        raise ValueError("幂等键必须带 namespace")
    return f"{namespace}:" + digest_of([namespace, *parts])


def event_dedupe_key(source: str, source_event_id: str) -> str:
    """事件去重键（SPEC §21.1）：同一来源的同一事件重复投递只处理一次。"""
    if not source or not source_event_id:
        raise ValueError("事件去重需要 source 与 source_event_id")
    return idempotency_key("event", source, source_event_id)


def mail_identity_key(
    *,
    account_scope: str,
    vendor_message_id: Optional[str] = None,
    mailbox_scope_id: Optional[str] = None,
    content_digest: Optional[str] = None,
    message_id: Optional[str] = None,
) -> str:
    """账号作用域内的稳定邮件身份（SPEC §21.2 接收去重）。

    优先级：
    1. `vendor_message_id`（供应商不可变 ID）；
    2. `mailbox_scope_id`（邮箱范围 ID + 有效代次）+ `content_digest`；
    `message_id` **只**参与最后一级弱身份，且不能跨账号合并。
    """
    if vendor_message_id:
        return idempotency_key("mail", account_scope, "vendor", vendor_message_id)
    if mailbox_scope_id and content_digest:
        return idempotency_key("mail", account_scope, "scope", mailbox_scope_id, content_digest)
    if message_id and content_digest:
        # 仅作弱身份：同账号内 Message-ID + 内容摘要；跨账号绝不合并
        return idempotency_key("mail", account_scope, "weak", message_id, content_digest)
    raise ValueError("邮件身份不足：缺少供应商 ID，也缺少 (邮箱范围 ID + 内容摘要)")


def action_idempotency_key(action_id: str) -> str:
    """已批准动作的稳定幂等键：同一 action_id 的所有重试沿用（SPEC §21.2）。"""
    return idempotency_key("action", action_id)


def archive_id_for(*, scope: str, scope_ref: str, snapshot_digest: str) -> str:
    """归档 ID 由「范围 + 内容快照摘要」派生（SPEC §21.4）：

    - 同一 archive_id 重试 → 复用未完成对象，不覆盖已校验内容；
    - 内容变化 → 新 archive_id。
    """
    return idempotency_key("archive", scope, scope_ref, snapshot_digest)


def joined(*values: Iterable[Any]) -> str:  # pragma: no cover - 语法糖
    return "|".join("" if v is None else str(v) for v in values)
