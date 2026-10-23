"""MVP-6 成长资产 — 流程版本：启动时钉版本，在途事务用旧版本（SPEC §10.4、§10.5、§863）。

要解决的问题
============

"改了流程不背刺在途事务"：用户认可一次成功流程后，系统把它沉淀成 `WorkflowVersion`；
之后流程还会继续演进（v2、v3…）。已经**在跑**的 Case 必须继续用它**启动时那一版**，
新 Case 用最新版。两侧同时可观察，才算真的做到了。

实现
====

- `Workflow.current_version_id` 指向当前生效版本（演进 = 新增版本 + 更新这个指针）；
- `pin_case_workflow()` 在 Case **启动**时把版本 ID 写进 `Case.links["workflow_version"]`
  （`links` 是契约的"关联 ID 索引（跨对象反查用）"，这里存的是对象 ID，正规用法）；
- **已有绑定绝不改写**：`pin_case_workflow()` 对已经钉过版本的 Case 直接返回原版本；
  要改绑定得显式调用 `repin_case_workflow(..., reason=...)`，并在审计里留痕；
- `case_workflow_version()` / `case_workflow_steps()` 读的就是 Case 上那一版，
  所以"新版本发布"这件事对在途 Case 完全不可见。

回滚
====

`rollback_workflow()` 用历史版本的内容**产生一个新版本**（`version = max+1`），
`links["rollback_of"]` 指向被回滚到的版本；历史版本对象一个都不删、一个都不改
（SPEC §10.5"成长资产必须支持查看差异、回滚、废止和恢复"）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..contracts.enums import ActorKind, RuleState
from ..contracts.errors import DuplicateObject, NotFound, ValidationError
from ..contracts.ids import digest_of, new_id
from ..contracts.objects import Case, Workflow, WorkflowVersion
from ..contracts.store_api import StoreProtocol

#: 唯一键命名空间（幂等：同一流程 + 同一版本只有一个对象）
WORKFLOW_UNIQUE_NAMESPACE = "growth_workflow"
WORKFLOW_VERSION_UNIQUE_NAMESPACE = "growth_workflow_version"

#: `Case.links` 里登记"启动时钉住哪一版流程"的关联名
CASE_WORKFLOW_LINK = "workflow_version"

AUDIT_WORKFLOW_CREATED = "WORKFLOW_CREATED"
AUDIT_WORKFLOW_VERSION_ENABLED = "WORKFLOW_VERSION_ENABLED"
AUDIT_WORKFLOW_VERSION_PUBLISHED = "WORKFLOW_VERSION_PUBLISHED"
AUDIT_WORKFLOW_PIN = "WORKFLOW_PIN"
AUDIT_WORKFLOW_REPIN = "WORKFLOW_REPIN"
AUDIT_WORKFLOW_ROLLBACK = "WORKFLOW_ROLLBACK"

#: SPEC §10.4 要求"抽象必须保留"的八个部分
REQUIRED_PARTS: Tuple[str, ...] = (
    "trigger_conditions",
    "steps",
    "question_points",
    "approval_points",
    "outputs",
    "archive_requirements",
    "failure_handling",
    "scope",
)


@dataclass
class WorkflowSpec:
    """一次成功流程的抽象（SPEC §10.4 要求的八件事，缺一项即拒绝）。"""

    name: str
    description: str = ""
    trigger_conditions: List[str] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    question_points: List[str] = field(default_factory=list)
    approval_points: List[str] = field(default_factory=list)
    outputs: Dict[str, Any] = field(default_factory=dict)
    archive_requirements: List[str] = field(default_factory=list)
    failure_handling: List[str] = field(default_factory=list)
    scope: str = "global"
    inputs: Dict[str, Any] = field(default_factory=dict)
    source_case_id: Optional[str] = None

    def validate(self) -> None:
        if not str(self.name or "").strip():
            raise ValidationError("流程必须有名字")
        missing: List[str] = []
        for name in REQUIRED_PARTS:
            value = getattr(self, name)
            if isinstance(value, str):
                if not value.strip():
                    missing.append(name)
            elif not value:
                missing.append(name)
        if missing:
            raise ValidationError(
                "流程抽象必须保留 %s（SPEC §10.4），缺少：%s" % (", ".join(REQUIRED_PARTS), ", ".join(missing))
            )
        for index, step in enumerate(self.steps):
            if not isinstance(step, dict) or not str(step.get("action", "")).strip():
                raise ValidationError("第 %d 步缺少 action（步骤必须可执行、可审计）: %r" % (index + 1, step))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "trigger_conditions": list(self.trigger_conditions),
            "inputs": dict(self.inputs),
            "steps": [dict(item) for item in self.steps],
            "question_points": list(self.question_points),
            "approval_points": list(self.approval_points),
            "outputs": dict(self.outputs),
            "archive_requirements": list(self.archive_requirements),
            "failure_handling": list(self.failure_handling),
            "scope": self.scope,
            "source_case_id": self.source_case_id,
        }

    @property
    def digest(self) -> str:
        return digest_of(self.to_dict())


def workflow_id_for(spec: WorkflowSpec) -> str:
    """确定性流程 ID：同名同范围 = 同一个流程（版本在此基础上叠加）。"""
    return new_id("Workflow", token=digest_of([spec.name.strip(), spec.scope.strip()])[:32])


def version_id_for(workflow_id: str, version: int, spec: WorkflowSpec) -> str:
    return new_id("WorkflowVersion", token=digest_of([workflow_id, int(version), spec.digest])[:32])


def _version_object(workflow_id: str, version: int, spec: WorkflowSpec, *, state: RuleState, moment: str) -> WorkflowVersion:
    return WorkflowVersion(
        id=version_id_for(workflow_id, version, spec),
        workflow_id=workflow_id,
        version=int(version),
        trigger_conditions=list(spec.trigger_conditions),
        inputs=dict(spec.inputs),
        steps=[dict(item) for item in spec.steps],
        question_points=list(spec.question_points),
        approval_points=list(spec.approval_points),
        outputs=dict(spec.outputs),
        archive_requirements=list(spec.archive_requirements),
        failure_handling=list(spec.failure_handling),
        scope=spec.scope,
        source_case_id=spec.source_case_id,
        state=state,
        created_at=moment,
        updated_at=moment,
    )


# --------------------------------------------------------------------------------------
# 创建 / 生效 / 演进
# --------------------------------------------------------------------------------------


def create_workflow(
    store: StoreProtocol,
    spec: WorkflowSpec,
    *,
    actor_id: str = "user",
    now: Optional[str] = None,
) -> Tuple[Workflow, WorkflowVersion]:
    """沉淀一个流程（`Workflow` + v1 `WorkflowVersion`，都是 `CANDIDATE`）。"""
    spec.validate()
    moment = now or store.now()
    workflow_id = workflow_id_for(spec)
    existing = store.get_by_unique(WORKFLOW_UNIQUE_NAMESPACE, spec.name.strip())
    if existing is not None and isinstance(existing, Workflow):
        version = _latest_version(store, existing.id)
        return existing, version  # type: ignore[return-value]
    workflow = Workflow(
        id=workflow_id,
        name=spec.name.strip(),
        description=spec.description,
        state=RuleState.CANDIDATE,
        created_at=moment,
        updated_at=moment,
    )
    detail = {
        "workflow_id": workflow_id,
        "spec": spec.to_dict(),
        "source_case_id": spec.source_case_id or "",
        "reason": "用户认可的成功流程沉淀为流程版本（SPEC §10.4）",
    }
    try:
        saved = store.put(
            workflow,
            unique=(WORKFLOW_UNIQUE_NAMESPACE, spec.name.strip()),
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="沉淀成功流程（候选，待确认）",
            audit_action=AUDIT_WORKFLOW_CREATED,
            audit_detail=detail,
        )
    except DuplicateObject:  # pragma: no cover - 并发同名
        saved = store.get_by_unique(WORKFLOW_UNIQUE_NAMESPACE, spec.name.strip())
        if saved is None:  # pragma: no cover
            raise
    assert isinstance(saved, Workflow)  # pragma: no cover
    version = _publish_version(store, saved, version=1, spec=spec, actor_id=actor_id, moment=moment)
    return saved, version


def _publish_version(
    store: StoreProtocol,
    workflow: Workflow,
    *,
    version: int,
    spec: WorkflowSpec,
    actor_id: str,
    moment: str,
) -> WorkflowVersion:
    candidate = _version_object(workflow.id, version, spec, state=RuleState.CANDIDATE, moment=moment)
    try:
        stored = store.put(
            candidate,
            unique=(WORKFLOW_VERSION_UNIQUE_NAMESPACE, "%s#v%d" % (workflow.id, version)),
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="生成流程版本 v%d（候选）" % version,
            audit_action=AUDIT_WORKFLOW_VERSION_PUBLISHED,
            audit_detail={"workflow_id": workflow.id, "version": version, "spec": spec.to_dict()},
        )
    except DuplicateObject:  # pragma: no cover - 同版本重复发布
        again = store.get_by_unique(WORKFLOW_VERSION_UNIQUE_NAMESPACE, "%s#v%d" % (workflow.id, version))
        if again is None:  # pragma: no cover
            raise
        return again  # type: ignore[return-value]
    assert isinstance(stored, WorkflowVersion)  # pragma: no cover
    return stored


def enable_workflow_version(
    store: StoreProtocol,
    version_id: str,
    *,
    actor_id: str = "user",
    now: Optional[str] = None,
) -> WorkflowVersion:
    """把某个流程版本推到 `ENABLED`（只走状态机的合法转换）。"""
    moment = now or store.now()
    version = get_version(store, version_id)
    if str(version.state) == str(RuleState.ENABLED):
        return version
    pending = version
    if str(version.state) != str(RuleState.PENDING_CONFIRMATION):
        pending = store.transition(
            "WorkflowVersion",
            version.id,
            str(RuleState.PENDING_CONFIRMATION),
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="流程版本 v%d 待生效" % int(version.version),
        )  # type: ignore[assignment]
    if not pending.confirmed_by:
        pending.confirmed_by = actor_id
    pending = store.put(
        pending,
        expected_revision=pending.revision,
        actor_kind=ActorKind.USER,
        actor_id=actor_id,
        reason="记录流程版本确认者",
        audit_action=AUDIT_WORKFLOW_VERSION_ENABLED,
        audit_detail={"workflow_version_id": pending.id, "version": int(pending.version)},
    )  # type: ignore[assignment]
    enabled = store.transition(
        "WorkflowVersion",
        pending.id,
        str(RuleState.ENABLED),
        actor_kind=ActorKind.USER,
        actor_id=actor_id,
        reason="流程版本 v%d 生效" % int(pending.version),
    )
    return enabled  # type: ignore[return-value]


def confirm_workflow(
    store: StoreProtocol,
    workflow_id: str,
    *,
    actor_id: str = "user",
    now: Optional[str] = None,
) -> Tuple[Workflow, WorkflowVersion]:
    """确认流程：最新版本生效 + `Workflow.current_version_id` 指向它（`Workflow` 也进 `ENABLED`）。"""
    moment = now or store.now()
    workflow = get_workflow(store, workflow_id)
    latest = _latest_version(store, workflow_id)
    if latest is None:  # pragma: no cover - create_workflow 一定建了 v1
        raise NotFound("流程 %s 还没有任何版本" % workflow_id)
    enabled = enable_workflow_version(store, latest.id, actor_id=actor_id, now=moment)
    if str(workflow.state) != str(RuleState.ENABLED):
        if str(workflow.state) != str(RuleState.PENDING_CONFIRMATION):
            workflow = store.transition(
                "Workflow",
                workflow.id,
                str(RuleState.PENDING_CONFIRMATION),
                actor_kind=ActorKind.USER,
                actor_id=actor_id,
                reason="确认流程：待生效",
            )  # type: ignore[assignment]
        workflow = store.transition(
            "Workflow",
            workflow.id,
            str(RuleState.ENABLED),
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="流程生效（当前版本 v%d）" % int(enabled.version),
        )  # type: ignore[assignment]
    if workflow.current_version_id != enabled.id:
        workflow.current_version_id = enabled.id
        workflow = store.put(
            workflow,
            expected_revision=workflow.revision,
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="指向当前生效版本 v%d" % int(enabled.version),
        )  # type: ignore[assignment]
    return workflow, enabled  # type: ignore[return-value]


def publish_workflow_version(
    store: StoreProtocol,
    workflow_id: str,
    spec: WorkflowSpec,
    *,
    actor_id: str = "user",
    now: Optional[str] = None,
) -> WorkflowVersion:
    """流程演进：新版本生效，旧版本转 `SUPERSEDED`（旧对象不删、内容不改）。

    **在途 Case 不受影响**：它们绑定的是启动时那一版的 ID（见 `pin_case_workflow`）。
    """
    spec.validate()
    moment = now or store.now()
    workflow = get_workflow(store, workflow_id)
    previous = effective_workflow_version(store, workflow_id)
    version_no = int(_latest_version(store, workflow_id).version) + 1  # type: ignore[union-attr]
    candidate = _publish_version(store, workflow, version=version_no, spec=spec, actor_id=actor_id, moment=moment)
    enabled = enable_workflow_version(store, candidate.id, actor_id=actor_id, now=moment)
    if previous is not None and previous.id != enabled.id:
        superseded = store.transition(
            "WorkflowVersion",
            previous.id,
            str(RuleState.SUPERSEDED),
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="新版本 v%d 生效，旧版本转为历史（不删、不改内容）" % version_no,
        )
        assert superseded is not None
    if workflow.current_version_id != enabled.id:
        workflow.current_version_id = enabled.id
        store.put(
            workflow,
            expected_revision=workflow.revision,
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="流程演进到 v%d" % version_no,
            audit_action=AUDIT_WORKFLOW_VERSION_PUBLISHED,
            audit_detail={"workflow_id": workflow.id, "version": version_no, "workflow_version_id": enabled.id},
        )
    return enabled


def rollback_workflow(
    store: StoreProtocol,
    workflow_id: str,
    *,
    to_version: int,
    reason: str,
    actor_id: str = "user",
    now: Optional[str] = None,
) -> WorkflowVersion:
    """回滚到历史流程版本：**产生新版本**，历史一个都不改。"""
    if not str(reason or "").strip():
        raise ValidationError("流程回滚必须给出原因")
    moment = now or store.now()
    target = next(
        (item for item in workflow_versions(store, workflow_id) if int(item.version) == int(to_version)),
        None,
    )
    if target is None:
        raise NotFound("流程 %s 没有 v%d" % (workflow_id, to_version))
    spec = spec_from_version(target)
    spec.source_case_id = target.source_case_id
    workflow = get_workflow(store, workflow_id)
    version_no = int(_latest_version(store, workflow_id).version) + 1  # type: ignore[union-attr]
    candidate = _publish_version(store, workflow, version=version_no, spec=spec, actor_id=actor_id, moment=moment)
    if target.id not in (candidate.links or {}).get("rollback_of", []):
        candidate.links = dict(candidate.links or {})
        candidate.links["rollback_of"] = [target.id]
        store.put(
            candidate,
            expected_revision=candidate.revision,
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="记录回滚来源版本",
            audit_action=AUDIT_WORKFLOW_ROLLBACK,
            audit_detail={
                "rollback_to_version": int(target.version),
                "rollback_to_workflow_version_id": target.id,
                "new_version": version_no,
                "reason": reason,
            },
        )
    previous = effective_workflow_version(store, workflow_id)
    enabled = enable_workflow_version(store, candidate.id, actor_id=actor_id, now=moment)
    if previous is not None and previous.id != enabled.id:
        store.transition(
            "WorkflowVersion",
            previous.id,
            str(RuleState.SUPERSEDED),
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="回滚到 v%d：当前版本转为历史（不删、不改内容）" % int(target.version),
        )
    return enabled


# --------------------------------------------------------------------------------------
# 启动钉版本 / 在途读版本
# --------------------------------------------------------------------------------------


def pin_case_workflow(
    store: StoreProtocol,
    *,
    case_id: str,
    workflow_id: Optional[str] = None,
    actor_id: str = "growth",
    now: Optional[str] = None,
) -> WorkflowVersion:
    """Case **启动**时钉住流程版本（只钉一次）。

    - 已经钉过 → 直接返回原版本（**绝不改写在途事务的绑定**）；
    - 没钉过 → 读 `Workflow.current_version_id` 并写进 `Case.links["workflow_version"]` + 审计。
    """
    moment = now or store.now()
    pinned = case_workflow_version(store, case_id)
    if pinned is not None:
        return pinned
    case = store.get("Case", case_id)
    if not isinstance(case, Case):  # pragma: no cover - 契约保证
        raise NotFound("Case 不存在: %s" % case_id)
    if workflow_id is None:
        raise ValidationError("首次钉版本必须给出 workflow_id（没有绑定就没有可用的版本指针）")
    version = effective_workflow_version(store, workflow_id)
    if version is None:
        raise ValidationError("流程 %s 还没有生效版本（先 confirm_workflow）" % workflow_id)
    links = dict(case.links or {})
    links[CASE_WORKFLOW_LINK] = [version.id]
    case.links = links
    store.put(
        case,
        expected_revision=case.revision,
        actor_kind=ActorKind.AGENT,
        actor_id=actor_id,
        reason="启动时钉住流程版本 v%d（在途事务不受后续演进影响）" % int(version.version),
        audit_action=AUDIT_WORKFLOW_PIN,
        audit_detail={
            "case_id": case_id,
            "workflow_id": workflow_id,
            "workflow_version_id": version.id,
            "version": int(version.version),
            "pinned_at": moment,
        },
    )
    return version


def repin_case_workflow(
    store: StoreProtocol,
    *,
    case_id: str,
    workflow_version_id: str,
    reason: str,
    actor_id: str = "user",
) -> WorkflowVersion:
    """显式改绑定（默认路径**不允许**改在途事务的版本）。

    只有用户明确要求并给出原因时才走这里，且一定会留审计；这是"不背刺在途事务"的例外通道。
    """
    if not str(reason or "").strip():
        raise ValidationError("改绑定必须给出原因（否则在途事务的版本就被悄悄换掉了）")
    case = store.get("Case", case_id)
    previous = case_workflow_version(store, case_id)
    version = get_version(store, workflow_version_id)
    links = dict(case.links or {})
    links[CASE_WORKFLOW_LINK] = [version.id]
    case.links = links
    store.put(
        case,
        expected_revision=case.revision,
        actor_kind=ActorKind.USER,
        actor_id=actor_id,
        reason="用户显式改流程版本绑定：%s" % reason,
        audit_action=AUDIT_WORKFLOW_REPIN,
        audit_detail={
            "case_id": case_id,
            "from_workflow_version_id": previous.id if previous is not None else "",
            "to_workflow_version_id": version.id,
            "reason": reason,
        },
    )
    return version


# --------------------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------------------


def get_workflow(store: StoreProtocol, workflow_id: str) -> Workflow:
    obj = store.get("Workflow", workflow_id)
    if not isinstance(obj, Workflow):  # pragma: no cover - 契约保证
        raise NotFound("Workflow 不存在: %s" % workflow_id)
    return obj


def get_version(store: StoreProtocol, version_id: str) -> WorkflowVersion:
    obj = store.get("WorkflowVersion", version_id)
    if not isinstance(obj, WorkflowVersion):  # pragma: no cover - 契约保证
        raise NotFound("WorkflowVersion 不存在: %s" % version_id)
    return obj


def workflow_versions(store: StoreProtocol, workflow_id: str) -> List[WorkflowVersion]:
    """全部流程版本（含被替代的历史版本），按 `version` 升序。"""
    rows = store.find("WorkflowVersion", where={"workflow_id": workflow_id}, limit=None)
    items = [item for item in rows if isinstance(item, WorkflowVersion)]
    return sorted(items, key=lambda item: int(item.version))


def _latest_version(store: StoreProtocol, workflow_id: str) -> Optional[WorkflowVersion]:
    items = workflow_versions(store, workflow_id)
    return items[-1] if items else None


def effective_workflow_version(store: StoreProtocol, workflow_id: str) -> Optional[WorkflowVersion]:
    """当前生效版本：`Workflow.current_version_id` 指向且状态为 `ENABLED` 的那一版。"""
    workflow = get_workflow(store, workflow_id)
    if not workflow.current_version_id:
        return None
    try:
        version = get_version(store, workflow.current_version_id)
    except NotFound:  # pragma: no cover - 指针指向不存在的版本
        return None
    if str(version.state) != str(RuleState.ENABLED):
        return None
    return version


def case_workflow_version(store: StoreProtocol, case_id: str) -> Optional[WorkflowVersion]:
    """该 Case **启动时钉住**的流程版本（在途 Case 用的就是它，与"最新版"无关）。"""
    case = store.get("Case", case_id)
    holders = list((getattr(case, "links", None) or {}).get(CASE_WORKFLOW_LINK, []))
    if not holders:
        return None
    try:
        return get_version(store, holders[0])
    except NotFound:  # pragma: no cover - 绑定指向不存在的版本
        return None


def case_workflow_steps(store: StoreProtocol, case_id: str) -> List[Dict[str, Any]]:
    """在途 Case 要执行的步骤序列（来自它绑定那一版，不是最新版）。"""
    version = case_workflow_version(store, case_id)
    if version is None:
        return []
    return [dict(item) for item in version.steps]


def spec_from_version(version: WorkflowVersion) -> WorkflowSpec:
    """从版本对象还原 spec（回滚与"查看差异"用）。"""
    return WorkflowSpec(
        name=version.workflow_id,
        description="",
        trigger_conditions=list(version.trigger_conditions),
        steps=[dict(item) for item in version.steps],
        question_points=list(version.question_points),
        approval_points=list(version.approval_points),
        outputs=dict(version.outputs),
        archive_requirements=list(version.archive_requirements),
        failure_handling=list(version.failure_handling),
        inputs=dict(version.inputs),
        scope=version.scope or "global",
        source_case_id=version.source_case_id,
    )


def diff_versions(left: WorkflowVersion, right: WorkflowVersion) -> Dict[str, Any]:
    """两个流程版本的差异（"查看差异"是可观察能力，不靠人读 JSON 猜）。"""
    left_map = left.to_dict()
    right_map = right.to_dict()
    changed: Dict[str, Any] = {}
    for key in sorted(set(left_map) | set(right_map)):
        if key in ("id", "revision", "created_at", "updated_at", "state", "links", "schema_version"):
            continue
        if left_map.get(key) != right_map.get(key):
            changed[key] = {"left": left_map.get(key), "right": right_map.get(key)}
    return {
        "left": {"id": left.id, "version": int(left.version), "state": str(left.state)},
        "right": {"id": right.id, "version": int(right.version), "state": str(right.state)},
        "changed": changed,
    }


__all__ = [
    "AUDIT_WORKFLOW_CREATED",
    "AUDIT_WORKFLOW_PIN",
    "AUDIT_WORKFLOW_REPIN",
    "AUDIT_WORKFLOW_ROLLBACK",
    "AUDIT_WORKFLOW_VERSION_ENABLED",
    "AUDIT_WORKFLOW_VERSION_PUBLISHED",
    "CASE_WORKFLOW_LINK",
    "REQUIRED_PARTS",
    "WORKFLOW_UNIQUE_NAMESPACE",
    "WORKFLOW_VERSION_UNIQUE_NAMESPACE",
    "WorkflowSpec",
    "case_workflow_steps",
    "case_workflow_version",
    "confirm_workflow",
    "create_workflow",
    "diff_versions",
    "effective_workflow_version",
    "enable_workflow_version",
    "get_version",
    "get_workflow",
    "pin_case_workflow",
    "publish_workflow_version",
    "repin_case_workflow",
    "rollback_workflow",
    "spec_from_version",
    "version_id_for",
    "workflow_id_for",
    "workflow_versions",
]
