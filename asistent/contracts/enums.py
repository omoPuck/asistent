"""状态、枚举与失败分类（SPEC §4.4、§5、§13.1、§21.1、§21.2）。

这些枚举是**唯一**的业务状态来源：不同 MVP 不得各自定义同义状态。
所有状态值是持久化的字符串常量，改名等于契约变更。
"""

from __future__ import annotations

import enum
from typing import Any, Dict, List


class StrEnum(str, enum.Enum):
    """字符串枚举：`json.dumps` 直接可用，`CaseState.NEW == "NEW"` 成立。"""

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return str(self.value)

    @classmethod
    def values(cls) -> List[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value: "str | StrEnum") -> "StrEnum":
        if isinstance(value, cls):
            return value
        return cls(str(value))


class ActorKind(StrEnum):
    USER = "USER"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"
    EXTERNAL = "EXTERNAL"


class CaseState(StrEnum):
    """Case 状态（SPEC §5.2，非严格单一路径，见 §21.1）。"""

    NEW = "NEW"
    TRIAGED = "TRIAGED"
    WAITING_FOR_CONTEXT = "WAITING_FOR_CONTEXT"
    ANALYZING = "ANALYZING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    ARCHIVING = "ARCHIVING"
    ARCHIVE_PENDING = "ARCHIVE_PENDING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class TaskState(StrEnum):
    """Case 内的一项工作（SPEC §5.3）。"""

    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    RETRY_WAIT = "RETRY_WAIT"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class QuestionState(StrEnum):
    """异步追问（SPEC §8.1）。"""

    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"


class DraftState(StrEnum):
    """草稿状态（SPEC §8.2）。审批始终绑定具体 DraftVersion。"""

    OPEN = "OPEN"
    LOCKED = "LOCKED"
    STALE = "STALE"
    REVOKED = "REVOKED"


class ApprovalState(StrEnum):
    """审批状态（SPEC §5.6、§8.4、§21.2）。"""

    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class ActionState(StrEnum):
    """外部动作状态（SPEC §21.2 表格，逐字对应）。"""

    PREPARED = "PREPARED"
    AUTHORIZED = "AUTHORIZED"
    IN_FLIGHT = "IN_FLIGHT"
    SUCCEEDED = "SUCCEEDED"
    FAILED_SAFE = "FAILED_SAFE"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class ContentKind(StrEnum):
    """事实 / 推断 / 规则内容性质（SPEC §5.4，禁止把推断冒充事实）。"""

    FACT = "FACT"
    INFERENCE = "INFERENCE"
    UNCERTAIN = "UNCERTAIN"


class RuleState(StrEnum):
    """规则生命周期（SPEC §10.3）。"""

    CANDIDATE = "CANDIDATE"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    ENABLED = "ENABLED"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


class AssetKind(StrEnum):
    """长期资产分类（SPEC §10.1）。"""

    PERSONAL_FACT = "PERSONAL_FACT"
    CONTACT_PROFILE = "CONTACT_PROFILE"
    PREFERENCE = "PREFERENCE"
    BEHAVIOR_RULE = "BEHAVIOR_RULE"
    CASE_MEMORY = "CASE_MEMORY"
    WORKFLOW = "WORKFLOW"
    SKILL = "SKILL"
    EPHEMERAL = "EPHEMERAL"


class ExecutionState(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class IndexState(StrEnum):
    """索引是派生数据（SPEC §6.2、§21.1）。"""

    PENDING = "PENDING"
    STALE = "STALE"
    INDEXED = "INDEXED"
    FAILED = "FAILED"
    REBUILD_REQUIRED = "REBUILD_REQUIRED"


class ArchiveState(StrEnum):
    """归档状态（SPEC §12.5、§21.4）。与业务状态独立持久化。"""

    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    PARTIAL = "PARTIAL"
    VERIFYING = "VERIFYING"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class RestoreState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    VERIFIED = "VERIFIED"
    CONFLICT = "CONFLICT"
    FAILED = "FAILED"


class PermissionDecision(StrEnum):
    """权限裁决结果（SPEC §21.3）。"""

    AUTO = "AUTO"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    FORBIDDEN = "FORBIDDEN"


class FailureClass(StrEnum):
    """失败分类（SPEC §13.1）。"""

    INVALID_INPUT = "INVALID_INPUT"
    MISSING_DATA = "MISSING_DATA"
    RULE_CONFLICT = "RULE_CONFLICT"
    MODEL_FAILURE = "MODEL_FAILURE"
    TOOL_FAILURE = "TOOL_FAILURE"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    PERMISSION_FAILURE = "PERMISSION_FAILURE"
    EXTERNAL_UNKNOWN = "EXTERNAL_UNKNOWN"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    INDEX_FAILURE = "INDEX_FAILURE"
    ARCHIVE_FAILURE = "ARCHIVE_FAILURE"
    SYSTEM_CRASH = "SYSTEM_CRASH"


class AuditAction(StrEnum):
    """审计动作类型（SPEC §11.2、§11.3）。审计只能追加或作废/更正。"""

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    TRANSITION = "TRANSITION"
    REJECT = "REJECT"
    READ = "READ"
    WRITE = "WRITE"
    APPROVE = "APPROVE"
    REVOKE = "REVOKE"
    EXECUTE = "EXECUTE"
    RETRY = "RETRY"
    ARCHIVE = "ARCHIVE"
    RESTORE = "RESTORE"
    CORRECTION = "CORRECTION"
    VOID = "VOID"


class EventKind(StrEnum):
    """首阶段四类事件 + 内部事件（SPEC §21.1）。"""

    MAIL_RECEIVED = "MAIL_RECEIVED"
    CONSOLE_MESSAGE = "CONSOLE_MESSAGE"
    KNOWLEDGE_CHANGED = "KNOWLEDGE_CHANGED"
    SCHEDULED_ARCHIVE = "SCHEDULED_ARCHIVE"
    CASE_CREATED = "CASE_CREATED"
    STATE_CHANGED = "STATE_CHANGED"


class MailboxRole(StrEnum):
    FACTS = "FACTS"
    INFERENCE = "INFERENCE"
    CANDIDATE = "CANDIDATE"


def snapshot() -> Dict[str, Any]:
    """枚举快照：代码与 contracts/contracts-v0.1.json 的一致性校验依据。"""
    out: Dict[str, Any] = {}
    for name, value in sorted(globals().items()):
        if isinstance(value, type) and issubclass(value, StrEnum) and value is not StrEnum:
            out[name] = value.values()
    return out
