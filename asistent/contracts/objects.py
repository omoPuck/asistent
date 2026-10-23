"""共享对象定义（SPEC §4.2、§5、§21.1）。

这是**唯一业务语义来源**：任何 MVP 都不得定义同义对象或绕过这些字段。
所有对象都带通用字段：稳定 ID、schema_version、revision、created_at、updated_at、links。
"""

from __future__ import annotations

import dataclasses
import enum
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from .enums import (
    ActionState,
    ActorKind,
    ApprovalState,
    ArchiveState,
    AssetKind,
    CaseState,
    ContentKind,
    DraftState,
    ExecutionState,
    IndexState,
    QuestionState,
    RestoreState,
    RuleState,
    StrEnum,
    TaskState,
)
from .timeutil import TemporalField, utcnow_iso
from .version import SCHEMA_VERSION

__all__ = [
    "CONTENT_ORIGIN_METADATA_PROJECTION",
    "CONTENT_ORIGIN_RAW_BYTES",
    "VALID_CONTENT_ORIGINS",
    "SharedObject",
    "SourceDocument",
    "Person",
    "Organization",
    "ContactPoint",
    "Conversation",
    "Case",
    "Task",
    "Fact",
    "Inference",
    "Rule",
    "Memory",
    "Workflow",
    "WorkflowVersion",
    "Draft",
    "DraftVersion",
    "Question",
    "Answer",
    "Approval",
    "ExternalAction",
    "ActionAttempt",
    "ArchiveEntry",
    "ArchiveManifest",
    "ArchiveJob",
    "RestoreJob",
    "AuditEvent",
    "Execution",
    "ToolCall",
    "Event",
    "FileArtifact",
    "snapshot",
]


def _to_plain(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if is_dataclass(value):
        return {f.name: _to_plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value


def _coerce(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = typing.get_origin(annotation)
    if origin is typing.Union:  # Optional[X] / Union[...]
        for arg in typing.get_args(annotation):
            if arg is type(None):
                continue
            try:
                return _coerce(arg, value)
            except Exception:
                continue
        return value
    if origin in (list, List):
        args = typing.get_args(annotation)
        item_type = args[0] if args else Any
        return [_coerce(item_type, item) for item in value]
    if origin in (dict, Dict):
        args = typing.get_args(annotation)
        value_type = args[1] if len(args) > 1 else Any
        return {str(k): _coerce(value_type, v) for k, v in dict(value).items()}
    if isinstance(annotation, type):
        if issubclass(annotation, StrEnum):
            return annotation.coerce(value)
        if is_dataclass(annotation) and hasattr(annotation, "from_dict"):
            return annotation.from_dict(value)
    return value


@dataclass
class SharedObject:
    """所有共享对象的基类：通用字段 + 通用序列化。"""

    id: str = ""
    schema_version: int = SCHEMA_VERSION
    revision: int = 1
    created_at: str = ""
    updated_at: str = ""
    #: 关联 ID 索引：`{"case": [...], "conversation": [...]}`，用于跨对象反查
    links: Dict[str, List[str]] = field(default_factory=dict)

    OBJECT_TYPE: ClassVar[str] = "SharedObject"

    def touch(self, *, at: Optional[str] = None) -> None:
        """更新 updated_at。revision 的递增由存储层在 CAS 成功时负责。"""
        self.updated_at = at or utcnow_iso()

    def to_dict(self) -> Dict[str, Any]:
        payload = _to_plain(self)
        payload["object_type"] = self.OBJECT_TYPE
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SharedObject":
        payload = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        hints = typing.get_type_hints(cls)
        kwargs = {key: _coerce(hints.get(key, Any), value) for key, value in payload.items()}
        return cls(**kwargs)

    def validate(self) -> None:
        if not self.id:
            raise ValueError(f"{self.OBJECT_TYPE} 缺少稳定 ID")
        if self.revision < 1:
            raise ValueError(f"{self.OBJECT_TYPE}.revision 必须 >= 1")


# --------------------------------------------------------------------------------------
# 契约常量：`content_origin` 的规范取值（契约 v0.7，CP-20261010-18）
# --------------------------------------------------------------------------------------

#: 本地字节**就是**来源字节 → `content_hash` 可用于**字节级**完整性校验。
CONTENT_ORIGIN_RAW_BYTES = "RAW_BYTES"
#: 通道不暴露原始字节（例如只给元数据），`content_hash` 是"元数据投影"的规范化摘要
#: → 回读一致**只能证明投影一致**，不能证明与远端原始字节一致。
CONTENT_ORIGIN_METADATA_PROJECTION = "METADATA_PROJECTION"
#: 合法取值集合。
#:
#: 这两个字符串以前只以**字面量**形式散落在各模块里（`asistent/mail/mcp_stdio.py`、
#: `asistent/archive/manifest.py` 各定义一份），契约只描述了语义、没有给出规范取值。
#: 后果：任何一处拼写/大小写漂移都会让"投影 vs 字节级"的判据静默错配——最坏情况是把
#: 投影当成字节级证明（正是 §12.2 要禁止的事）。现在取值由契约定义，模块一律引用。
VALID_CONTENT_ORIGINS: Tuple[str, ...] = (
    CONTENT_ORIGIN_RAW_BYTES,
    CONTENT_ORIGIN_METADATA_PROJECTION,
)


# --------------------------------------------------------------------------------------
# 资料与联系人
# --------------------------------------------------------------------------------------


@dataclass
class SourceDocument(SharedObject):
    """原始文档、邮件、附件或用户文件（SPEC §5.1）。原始内容不得被模型输出覆盖。"""

    OBJECT_TYPE: ClassVar[str] = "SourceDocument"

    source_type: str = ""            # email / attachment / markdown / user_file / web
    source_system: str = ""          # imap:account / local_fs / mcpserver...
    source_id: str = ""              # 来源系统内的稳定标识
    identity_key: str = ""           # 账号作用域稳定身份（mail_identity_key）
    origin_path: str = ""            # 原始内容位置（本地路径或外部 URI）
    mime_type: str = ""
    size_bytes: int = 0
    content_hash: str = ""           # 原始内容 sha256（含义由 content_origin 决定，见下）
    content_origin: Optional[str] = None
    #: `content_origin`（契约 v0.4，CP-20261010-07）声明 `content_hash` 到底哈希的是什么：
    #: - `RAW_BYTES`：原始字节的 sha256 —— 可用于字节级完整性校验（归档回读校验用这个）；
    #: - `METADATA_PROJECTION`：通道不暴露原始字节（例如 mcp-email-server 只给元数据），
    #:   `content_hash` 是"元数据投影"的规范化摘要 —— **只能证明投影一致，不能证明字节一致**；
    #: - `None`：未声明。判据未知时不得把 content_hash 当字节级证明（宁可拒绝也不要"看起来通过"）。
    content_version: int = 1         # 内容版本（同一对象的多次修订）
    received_at: str = ""            # 接收/创建时间（UTC ISO）
    readable: bool = True            # 用户是否可不依赖 Agent 读懂
    index_state: IndexState = IndexState.PENDING
    index_error: Optional[str] = None
    last_indexed_at: Optional[str] = None
    archive_state: ArchiveState = ArchiveState.PENDING
    conversation_id: Optional[str] = None
    case_id: Optional[str] = None
    #: 邮件专有：线程依据（Message-ID / References）与来源文件夹
    thread_keys: List[str] = field(default_factory=list)
    mailbox_folder: Optional[str] = None
    vendor_message_id: Optional[str] = None


@dataclass
class Person(SharedObject):
    OBJECT_TYPE: ClassVar[str] = "Person"

    display_name: str = ""
    contact_point_ids: List[str] = field(default_factory=list)
    organization_ids: List[str] = field(default_factory=list)
    notes_path: Optional[str] = None
    #: 关联置信度与"是否已被用户确认"，禁止把猜测当事实（SPEC §7.2）
    confidence: float = 1.0
    confirmed_by_user: bool = False


@dataclass
class Organization(SharedObject):
    OBJECT_TYPE: ClassVar[str] = "Organization"

    name: str = ""
    domains: List[str] = field(default_factory=list)
    notes_path: Optional[str] = None
    confidence: float = 1.0
    confirmed_by_user: bool = False


@dataclass
class ContactPoint(SharedObject):
    OBJECT_TYPE: ClassVar[str] = "ContactPoint"

    kind: str = "email"          # email / phone / im / other
    value: str = ""
    person_id: Optional[str] = None
    verified: bool = False


@dataclass
class Conversation(SharedObject):
    """持久化会话 ID（SPEC §21.1）：不等同于进程、页面或模型上下文窗口。"""

    OBJECT_TYPE: ClassVar[str] = "Conversation"

    channel: str = "console"     # email_thread / console / other
    subject: str = ""
    thread_key: Optional[str] = None
    participant_ids: List[str] = field(default_factory=list)
    case_ids: List[str] = field(default_factory=list)
    source_document_ids: List[str] = field(default_factory=list)
    message_count: int = 0
    last_message_at: Optional[str] = None


# --------------------------------------------------------------------------------------
# 事务与任务
# --------------------------------------------------------------------------------------


@dataclass
class Case(SharedObject):
    """围绕一个外部事务形成的完整事务容器（SPEC §5.2）。"""

    OBJECT_TYPE: ClassVar[str] = "Case"

    trigger_event_id: Optional[str] = None
    goal: str = ""
    state: CaseState = CaseState.NEW
    #: 与业务状态独立持久化的归档状态（SPEC §21.1）
    archive_state: ArchiveState = ArchiveState.PENDING
    participants: List[str] = field(default_factory=list)
    organization_ids: List[str] = field(default_factory=list)
    conversation_ids: List[str] = field(default_factory=list)
    source_document_ids: List[str] = field(default_factory=list)
    fact_ids: List[str] = field(default_factory=list)
    inference_ids: List[str] = field(default_factory=list)
    rule_ids: List[str] = field(default_factory=list)
    task_ids: List[str] = field(default_factory=list)
    draft_ids: List[str] = field(default_factory=list)
    question_ids: List[str] = field(default_factory=list)
    approval_ids: List[str] = field(default_factory=list)
    action_ids: List[str] = field(default_factory=list)
    attempt_ids: List[str] = field(default_factory=list)
    archive_job_ids: List[str] = field(default_factory=list)
    audit_event_ids: List[str] = field(default_factory=list)
    #: 可观察性：等待原因、下一步、最近动作（SPEC §11.4）
    waiting_reason: Optional[str] = None
    next_action: Optional[str] = None
    last_action_summary: Optional[str] = None
    blocked_reason: Optional[str] = None
    failure_class: Optional[str] = None
    result_summary: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class Task(SharedObject):
    """Case 内的一项工作（SPEC §5.3）。不能因页面关闭而丢失。"""

    OBJECT_TYPE: ClassVar[str] = "Task"

    case_id: str = ""
    kind: str = ""
    title: str = ""
    state: TaskState = TaskState.PENDING
    assignee: str = "agent"              # agent / user / system
    inputs: Dict[str, Any] = field(default_factory=dict)
    preconditions: List[str] = field(default_factory=list)
    outputs: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    retry_policy: Dict[str, Any] = field(default_factory=lambda: {"max_attempts": 3, "backoff_seconds": 60})
    attempt_count: int = 0
    timeout_at: Optional[str] = None
    next_run_at: Optional[str] = None
    last_error: Optional[str] = None
    failure_class: Optional[str] = None
    execution_ids: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# 事实 / 推断 / 规则 / 记忆 / 流程
# --------------------------------------------------------------------------------------


@dataclass
class Fact(SharedObject):
    """可回到原文、用户输入或外部确认的事实（SPEC §5.4）。"""

    OBJECT_TYPE: ClassVar[str] = "Fact"

    case_id: Optional[str] = None
    subject: str = ""
    statement: str = ""
    content_kind: ContentKind = ContentKind.FACT
    citations: List[dict] = field(default_factory=list)   # Citation.to_dict()
    temporal_fields: List[dict] = field(default_factory=list)  # TemporalField.to_dict()
    confirmed_by_user: bool = False
    source_system: Optional[str] = None


@dataclass
class Inference(SharedObject):
    """模型或 Agent 的推断：不得冒充事实（SPEC §5.4、§21.5）。"""

    OBJECT_TYPE: ClassVar[str] = "Inference"

    case_id: Optional[str] = None
    statement: str = ""
    content_kind: ContentKind = ContentKind.INFERENCE
    based_on_fact_ids: List[str] = field(default_factory=list)
    confidence: float = 0.5
    alternatives: List[str] = field(default_factory=list)
    produced_by_execution_id: Optional[str] = None
    #: 候选区标记：推断 / 摘要 / 偏好默认只进候选区，不进事实库
    candidate_only: bool = True


@dataclass
class Rule(SharedObject):
    """用户确认后可用于未来事务的判断/行为约束（SPEC §5.8、§10.3）。"""

    OBJECT_TYPE: ClassVar[str] = "Rule"

    statement: str = ""
    state: RuleState = RuleState.CANDIDATE
    version: int = 1
    scope: str = "global"          # global / contact / project / case_type
    scope_ref: Optional[str] = None
    priority: int = 100
    source_case_id: Optional[str] = None
    source_user_message: Optional[str] = None
    examples: List[str] = field(default_factory=list)
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None
    effective_from: Optional[str] = None
    revoked_at: Optional[str] = None
    superseded_by: Optional[str] = None
    git_commit: Optional[str] = None


@dataclass
class Memory(SharedObject):
    """经确认可长期使用的知识或偏好（SPEC §5.8、§10.1）。"""

    OBJECT_TYPE: ClassVar[str] = "Memory"

    asset_kind: AssetKind = AssetKind.EPHEMERAL
    statement: str = ""
    state: RuleState = RuleState.CANDIDATE
    scope: str = "global"
    scope_ref: Optional[str] = None
    source_case_id: Optional[str] = None
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None
    version: int = 1
    superseded_by: Optional[str] = None
    #: 契约 v0.8（CP-20261010-26）：有效期是**数据**，不能只存在于某层自己造的附属对象里。
    #: `None` = 长期有效；到点后不再注入上下文（历史事实与记忆对象本身保留可查）。
    #: 过期的**效果**复用 `state=REVOKED`，"是过期还是用户撤销"由 `links`/原因区分——
    #: 为同一效果再造一个状态会让"这条记忆现在还能不能用"出现两种判据。
    valid_until: Optional[str] = None


@dataclass
class Workflow(SharedObject):
    OBJECT_TYPE: ClassVar[str] = "Workflow"

    name: str = ""
    description: str = ""
    current_version_id: Optional[str] = None
    state: RuleState = RuleState.CANDIDATE


@dataclass
class WorkflowVersion(SharedObject):
    """可复用流程版本（SPEC §5.8、§10.4）：必须保留触发条件、步骤、追问点、审批点、输出、归档要求、失败处理、适用范围。"""

    OBJECT_TYPE: ClassVar[str] = "WorkflowVersion"

    workflow_id: str = ""
    version: int = 1
    trigger_conditions: List[str] = field(default_factory=list)
    inputs: Dict[str, Any] = field(default_factory=dict)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    question_points: List[str] = field(default_factory=list)
    approval_points: List[str] = field(default_factory=list)
    outputs: Dict[str, Any] = field(default_factory=dict)
    archive_requirements: List[str] = field(default_factory=list)
    failure_handling: List[str] = field(default_factory=list)
    scope: str = "global"
    source_case_id: Optional[str] = None
    confirmed_by: Optional[str] = None
    state: RuleState = RuleState.CANDIDATE


# --------------------------------------------------------------------------------------
# 草稿 / 追问 / 审批
# --------------------------------------------------------------------------------------


@dataclass
class Draft(SharedObject):
    OBJECT_TYPE: ClassVar[str] = "Draft"

    case_id: str = ""
    target_kind: str = "email_reply"
    target_ref: str = ""
    state: DraftState = DraftState.OPEN
    current_version_id: Optional[str] = None
    version_ids: List[str] = field(default_factory=list)


@dataclass
class DraftVersion(SharedObject):
    """草稿版本（SPEC §5.5）：审批只能绑定确定版本。"""

    OBJECT_TYPE: ClassVar[str] = "DraftVersion"

    draft_id: str = ""
    version_no: int = 1
    body: str = ""
    content_hash: str = ""
    author_kind: ActorKind = ActorKind.AGENT
    author_id: str = "agent"
    change_reason: str = ""
    target_object_id: Optional[str] = None
    summary: str = ""
    citations: List[dict] = field(default_factory=list)
    parent_version_id: Optional[str] = None


@dataclass
class Question(SharedObject):
    """异步追问（SPEC §8.1）。"""

    OBJECT_TYPE: ClassVar[str] = "Question"

    case_id: str = ""
    question: str = ""
    background: str = ""
    #: 契约 v0.8（CP-20261010-25）：追问**类别**必须是数据，不能靠读 `background` 的文字猜。
    #: 取值与理解层的追问候选一致：`ambiguity` / `conflict` / `missing_material` /
    #: `missing_time` / `missing_recipient`；空串 = 未分类（历史对象）。
    #:
    #: 这条是为 §14.5 补的洞：契约明文写 `Question(kind="conflict")`，但对象里**没有 kind 字段**，
    #: 而理解层的 `QuestionCandidate.validate()` 又强制 kind 非空——于是类别在落库时被丢掉，
    #: 下游（控制台分组、成长层的冲突门闸）只能重新用文字匹配来复原，属于"契约自己的文字实现不了"。
    kind: str = ""
    required_format: str = "text"
    affects: List[str] = field(default_factory=list)
    deadline: Optional[str] = None
    state: QuestionState = QuestionState.OPEN
    answer_id: Optional[str] = None
    asked_by_execution_id: Optional[str] = None


@dataclass
class Answer(SharedObject):
    OBJECT_TYPE: ClassVar[str] = "Answer"

    question_id: str = ""
    case_id: str = ""
    answer_text: str = ""
    answered_by: str = "user"
    attachments: List[str] = field(default_factory=list)


@dataclass
class Approval(SharedObject):
    """用户批准（SPEC §5.6、§8.4、§21.2）。绑定规范化动作摘要。"""

    OBJECT_TYPE: ClassVar[str] = "Approval"

    case_id: str = ""
    action_id: Optional[str] = None
    approver: str = "user"
    approved_action_digest: str = ""
    target_object_ids: List[str] = field(default_factory=list)
    content_version_refs: Dict[str, str] = field(default_factory=dict)
    evidence_digest: Optional[str] = None
    created_at_approval: str = ""
    expires_at: Optional[str] = None
    state: ApprovalState = ApprovalState.ACTIVE
    consumed_by_attempt_id: Optional[str] = None
    reserved_at: Optional[str] = None
    revoked_reason: Optional[str] = None
    invalidated_reason: Optional[str] = None
    #: 认证来源：必须是认证控制操作，不能是聊天文本（SPEC §21.3）
    auth_channel: str = "console"
    auth_evidence: Optional[str] = None
    summary_shown: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# 外部动作
# --------------------------------------------------------------------------------------


@dataclass
class ExternalAction(SharedObject):
    """可能产生外部副作用的动作（SPEC §5.7、§21.2）。"""

    OBJECT_TYPE: ClassVar[str] = "ExternalAction"

    case_id: str = ""
    kind: str = ""
    state: ActionState = ActionState.PREPARED
    idempotency_key: str = ""
    action_summary: Dict[str, Any] = field(default_factory=dict)
    summary_digest: str = ""
    target_ref: str = ""
    connector: str = ""
    approval_id: Optional[str] = None
    attempt_ids: List[str] = field(default_factory=list)
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None
    prepared_at: str = ""
    finalized_at: Optional[str] = None
    result_class: Optional[str] = None
    blocking_reason: Optional[str] = None


@dataclass
class ActionAttempt(SharedObject):
    """每次实际尝试：请求幂等键、外部响应、时间、执行节点、结果分类（SPEC §5.7）。"""

    OBJECT_TYPE: ClassVar[str] = "ActionAttempt"

    action_id: str = ""
    attempt_no: int = 1
    request_idempotency_key: str = ""
    state: str = "IN_FLIGHT"          # IN_FLIGHT / SUCCEEDED / FAILED_SAFE / UNKNOWN
    started_at: str = ""
    finished_at: Optional[str] = None
    executor_node: str = ""
    external_response: Dict[str, Any] = field(default_factory=dict)
    external_ref: Optional[str] = None
    failure_class: Optional[str] = None
    error_text: Optional[str] = None


# --------------------------------------------------------------------------------------
# 归档与恢复
# --------------------------------------------------------------------------------------


@dataclass
class ArchiveEntry:
    """归档清单中的一条文件记录（SPEC §12.4、§21.4）。"""

    relative_path: str
    sha256: str
    size_bytes: int
    source_object_id: Optional[str] = None
    source_version: Optional[int] = None
    kind: str = "file"                # file / manifest / bundle / index_state
    content_origin: Optional[str] = None
    #: 归档条目也必须带来源类型（契约 v0.4，CP-20261010-07）：校验方据此选择校验方式——
    #: `RAW_BYTES` 走逐文件字节 sha256 回读校验；`METADATA_PROJECTION` 只能校验"投影摘要一致"，
    #: 必须在归档报告里如实标注，不得宣称做了字节级校验。

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "source_object_id": self.source_object_id,
            "source_version": self.source_version,
            "kind": self.kind,
            "content_origin": self.content_origin,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchiveEntry":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ArchiveManifest(SharedObject):
    """不可变归档清单（SPEC §12.4、§21.4）。archive_id 由内容快照派生。"""

    OBJECT_TYPE: ClassVar[str] = "ArchiveManifest"

    archive_id: str = ""
    scope: str = "snapshot"           # snapshot / case / chat / git_bundle
    scope_ref: str = ""
    created_at_manifest: str = ""
    entries: List[ArchiveEntry] = field(default_factory=list)
    source_versions: Dict[str, str] = field(default_factory=dict)
    policy_id: Optional[str] = None
    policy_version: Optional[int] = None
    code_version: Optional[str] = None
    contract_version: Optional[str] = None
    rule_version: Optional[str] = None
    workflow_version: Optional[str] = None
    excluded: List[Dict[str, Any]] = field(default_factory=list)
    secret_scan: Dict[str, Any] = field(default_factory=dict)
    manifest_digest: str = ""
    remote_root: Optional[str] = None


@dataclass
class ArchiveJob(SharedObject):
    """归档任务（SPEC §12.5、§21.4）。"""

    OBJECT_TYPE: ClassVar[str] = "ArchiveJob"

    archive_id: str = ""
    scope: str = "snapshot"
    scope_ref: str = ""
    manifest_id: Optional[str] = None
    state: ArchiveState = ArchiveState.PENDING
    policy_id: Optional[str] = None
    policy_version: Optional[int] = None
    trigger: str = "scheduled"        # scheduled / case_closed / user_request
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_attempt_at: Optional[str] = None
    attempt_count: int = 0
    uploaded: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    verified_files: int = 0
    unverified_reason: Optional[str] = None
    remote_root: Optional[str] = None
    error_text: Optional[str] = None


@dataclass
class RestoreJob(SharedObject):
    """恢复任务（SPEC §12.5、§21.4）：默认恢复到隔离位置，绝不直接覆盖主数据。"""

    OBJECT_TYPE: ClassVar[str] = "RestoreJob"

    archive_id: str = ""
    state: RestoreState = RestoreState.PENDING
    isolate_dir: str = ""
    read_only_mode: bool = True
    #: 契约 v0.7（CP-20261010-22）：恢复的三条安全属性从"散落在 note 里的散文"升为**可机检字段**。
    #: 默认值即安全值——恢复**默认只读、默认使审批失效、默认禁止重放**归档中的外发动作。
    approvals_invalidated: bool = True
    replay_forbidden: bool = True
    verified_files: List[str] = field(default_factory=list)
    hash_mismatches: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    requested_by: str = "user"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    reindex_done: bool = False
    git_history_restored: bool = False
    note: Optional[str] = None


# --------------------------------------------------------------------------------------
# 审计与执行
# --------------------------------------------------------------------------------------


@dataclass
class AuditEvent(SharedObject):
    """不可静默修改的审计事件（SPEC §11.3）。只能追加、作废或通过更正事件修正。"""

    OBJECT_TYPE: ClassVar[str] = "AuditEvent"

    at: str = ""
    action: str = ""
    actor_kind: ActorKind = ActorKind.SYSTEM
    actor_id: str = "system"
    execution_id: Optional[str] = None
    case_id: Optional[str] = None
    object_type: Optional[str] = None
    object_id: Optional[str] = None
    result: str = "OK"                # OK / REJECTED / FAILED
    version_before: Optional[int] = None
    version_after: Optional[int] = None
    content_hash: Optional[str] = None
    reason: str = ""
    caller: Optional[str] = None
    correlation_id: Optional[str] = None
    voids_event_id: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall(SharedObject):
    OBJECT_TYPE: ClassVar[str] = "ToolCall"

    execution_id: str = ""
    tool: str = ""
    args_digest: str = ""
    started_at: str = ""
    finished_at: Optional[str] = None
    result: str = "OK"
    failure_class: Optional[str] = None
    note: Optional[str] = None


@dataclass
class Execution(SharedObject):
    """一次 Agent 执行及其输入、输出、工具调用和结果（SPEC §4.2）。"""

    OBJECT_TYPE: ClassVar[str] = "Execution"

    case_id: Optional[str] = None
    task_id: Optional[str] = None
    backend: str = "echo"
    model: Optional[str] = None
    state: ExecutionState = ExecutionState.RUNNING
    inputs_digest: Optional[str] = None
    input_summary: str = ""
    output_summary: str = ""
    tool_call_ids: List[str] = field(default_factory=list)
    read_object_ids: List[str] = field(default_factory=list)
    write_object_ids: List[str] = field(default_factory=list)
    token_usage: Dict[str, int] = field(default_factory=dict)
    cost: float = 0.0
    started_at: str = ""
    finished_at: Optional[str] = None
    failure_class: Optional[str] = None
    error_text: Optional[str] = None


@dataclass
class Event(SharedObject):
    """事件信封（SPEC §21.1）：必须含 event_id/source/source_event_id/occurred_at/received_at/schema_version。"""

    OBJECT_TYPE: ClassVar[str] = "Event"

    kind: str = ""
    source: str = ""
    source_event_id: str = ""
    occurred_at: str = ""
    received_at: str = ""
    event_schema_version: int = 1
    case_id: Optional[str] = None
    conversation_id: Optional[str] = None
    dedupe_key: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    processed_at: Optional[str] = None
    processing_result: Optional[str] = None


@dataclass
class FileArtifact(SharedObject):
    """生成文件（SPEC §9.3）：必须有文件身份、来源 Case、生成时间、版本、校验信息和权限。"""

    OBJECT_TYPE: ClassVar[str] = "FileArtifact"

    case_id: str = ""
    path: str = ""
    name: str = ""
    size_bytes: int = 0
    sha256: str = ""
    content_type: str = "application/octet-stream"
    version: int = 1
    generated_at: str = ""
    permission: str = "owner_read"
    download_count: int = 0
    transfer_failures: int = 0


def snapshot() -> Dict[str, Any]:
    """对象字段快照：用于契约一致性校验（contracts/contracts-v0.1.json）。"""
    out: Dict[str, Any] = {}
    for name in __all__:
        value = globals().get(name)
        if isinstance(value, type) and is_dataclass(value) and issubclass(value, SharedObject):
            out[name] = {
                "object_type": value.OBJECT_TYPE,
                "fields": [f.name for f in dataclasses.fields(value)],
            }
    return out
