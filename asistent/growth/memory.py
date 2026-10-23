"""MVP-6 成长资产 — 记忆的有效期（SPEC §10.1、§862 的"历史保留"要求）。

问题
====

`Memory` 是"经确认可长期使用的知识或偏好"，但**长期 ≠ 永久**：一次会议时间、一次出行偏好
过一段时间就不该再驱动结论了。要求是：

1. 过期后**不再被当作事实使用**（不进入 `Fact`、不驱动结论）；
2. **历史保留、可查**（对象不删、审计不删、当时写的 `Fact` 也不删）。

契约现状（所以这里有一处 CP）
============================

`Memory` 的字段里**没有** `valid_until` / `expires_at`（`Rule` 有 `effective_from`/`revoked_at`，
`Memory` 没有），也没有 `EXPIRED` 状态（`RuleState` 只有 `CANDIDATE/PENDING_CONFIRMATION/ENABLED/
SUPERSEDED/REVOKED`）。在不改契约的前提下，本模块这样做：

- **有效期窗口**落在一条 `Task` 上：`kind="memory_validity"`、`assignee="system"`、
  `timeout_at=<valid_until>`、`outputs={"memory_id": ..., "valid_until": ...}`、
  `links["memory"]=[<memory_id>]`。这不是硬凑：`Task.timeout_at` 本来就是"到点必须处理"的工作项，
  而"到点撤销这条记忆"确实是一件系统工作；`links` 存的是对象 ID，符合契约对 `links` 的定义。
- **是否可用**是 `now` 的纯函数：`state == ENABLED` 且（没有窗口 或 `now < valid_until`）。
  所以"过期"**不需要等清扫**就已经生效；`sweep_expired_memories()` 只是把这件事
  物化成 `REVOKED` + 审计，方便控制台与巡检。
- `sweep` 之后：对象仍在、`statement` 未改、当时写的 `Fact` 未删、审计里能读回
  "什么时候因为什么被停用"。

CP 提案（见 `docs/mvp/mvp-6.md` §8）：建议给 `Memory` 补 `valid_until`（以及 `revoked_at`）
与 `EXPIRED` 状态；本模块的 Task 方案可平滑迁移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..contracts.citations import Citation
from ..contracts.enums import ActorKind, AssetKind, RuleState, TaskState
from ..contracts.errors import DuplicateObject, NotFound, ValidationError
from ..contracts.ids import digest_of, new_id
from ..contracts.objects import AuditEvent, Case, Fact, Memory, SourceDocument, Task
from ..contracts.store_api import StoreProtocol
from ..contracts.timeutil import add_seconds, parse_iso

#: 有效期任务的工作项种类（可 `find("Task", where={"kind": ...})` 巡检）
MEMORY_VALIDITY_KIND = "memory_validity"

MEMORY_UNIQUE_NAMESPACE = "growth_memory"
MEMORY_FACT_UNIQUE_NAMESPACE = "growth_memory_fact"

AUDIT_MEMORY_REMEMBERED = "MEMORY_REMEMBERED"
AUDIT_MEMORY_VALIDITY = "MEMORY_VALIDITY_GRANTED"
AUDIT_MEMORY_EXPIRED = "MEMORY_EXPIRED"
AUDIT_MEMORY_FACT = "MEMORY_FACT_LINKED"

#: `Task.links` / `Memory.links` 里用到的关联名
MEMORY_LINK = "memory"
MEMORY_SOURCE_DOCUMENT_LINK = "source_document"


def _asset_kind(value: Any) -> AssetKind:
    """把 `AssetKind` 词表外的分类**拒绝**掉（不认识的分类不许静默存进去）。"""
    if isinstance(value, AssetKind):
        return value
    try:
        return AssetKind(str(value))
    except ValueError as exc:
        raise ValidationError(
            "未知长期资产分类 %r（允许：%s）" % (value, ", ".join(str(item) for item in AssetKind))
        ) from exc


def memory_id_for(statement: str, scope: str, scope_ref: Optional[str]) -> str:
    """确定性记忆 ID：同一条陈述 + 同一适用范围 = 同一个记忆（重复记住是幂等的）。"""
    return new_id("Memory", token=digest_of([statement.strip(), scope, scope_ref or ""])[:32])


def validity_task_id_for(memory_id: str) -> str:
    return new_id("Task", token=digest_of(["memory_validity", memory_id])[:32])


def fact_id_for_memory(case_id: str, memory_id: str) -> str:
    return new_id("Fact", token=digest_of(["memory_fact", case_id, memory_id])[:32])


@dataclass
class MemoryFactResult:
    """把可用记忆落成事实的结果（可观察：写了哪些、复用了哪些、跳过了哪些及原因）。"""

    created: List[str] = field(default_factory=list)
    reused: List[str] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "created": list(self.created),
            "reused": list(self.reused),
            "skipped": [{"memory_id": item[0], "reason": item[1]} for item in self.skipped],
        }


# --------------------------------------------------------------------------------------
# 记住 / 确认 / 有效期
# --------------------------------------------------------------------------------------


def remember(
    store: StoreProtocol,
    *,
    statement: str,
    asset_kind: str = str(AssetKind.PREFERENCE),
    scope: str = "global",
    scope_ref: Optional[str] = None,
    source_case_id: Optional[str] = None,
    source_document_id: str = "",
    confirmed_by: str = "user",
    ttl_seconds: Optional[int] = None,
    actor_id: str = "user",
    now: Optional[str] = None,
    reason: str = "",
) -> Memory:
    """记住一条长期资产（`AssetKind`），可选给一个有效期。

    - 默认**没有** `ttl_seconds` → 长期有效（"长期"不等于"永久"由调用方决定：给了 TTL 就有边界）；
    - 给了 TTL → 同时落一条有效期任务（`timeout_at = 确认时间 + ttl`）；
    - 幂等：同一条陈述 + 同一适用范围重复记住 → 返回既有对象（不重复建、不覆盖）。
    """
    text = str(statement or "").strip()
    if not text:
        raise ValidationError("记忆必须有陈述内容")
    if ttl_seconds is not None and int(ttl_seconds) <= 0:
        raise ValidationError("有效期必须为正秒数（要给永久记忆就别传 ttl_seconds）")
    moment = now or store.now()
    memory_id = memory_id_for(text, scope, scope_ref)
    existing = store.get("Memory", memory_id) if store.get_by_unique(MEMORY_UNIQUE_NAMESPACE, memory_id) else None
    if isinstance(existing, Memory):
        return existing

    memory = Memory(
        id=memory_id,
        asset_kind=_asset_kind(asset_kind),
        statement=text,
        state=RuleState.CANDIDATE,
        scope=scope,
        scope_ref=scope_ref,
        source_case_id=source_case_id,
        links={MEMORY_SOURCE_DOCUMENT_LINK: [source_document_id] if source_document_id else []},
        created_at=moment,
        updated_at=moment,
    )
    try:
        saved = store.put(
            memory,
            unique=(MEMORY_UNIQUE_NAMESPACE, memory_id),
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason=reason or "用户确认的长期资产（候选）",
            audit_action=AUDIT_MEMORY_REMEMBERED,
            audit_detail={
                "memory_id": memory_id,
                "asset_kind": str(asset_kind),
                "scope": scope,
                "scope_ref": scope_ref or "",
                "source_case_id": source_case_id or "",
                "source_document_id": source_document_id,
                "ttl_seconds": ttl_seconds,
            },
        )
    except DuplicateObject:  # pragma: no cover - 并发重复记住
        again = store.get("Memory", memory_id)
        if not isinstance(again, Memory):  # pragma: no cover
            raise
        return again
    assert isinstance(saved, Memory)  # pragma: no cover
    # `confirmed_by` 为空 = 只是候选：候选记忆**不生效**（与规则同一口径：没确认就不算数）
    enabled = (
        confirm_memory(store, saved.id, confirmed_by=confirmed_by, actor_id=actor_id, now=moment)
        if str(confirmed_by or "").strip()
        else saved
    )
    if ttl_seconds is not None:
        grant_validity(
            store,
            memory_id=enabled.id,
            ttl_seconds=int(ttl_seconds),
            actor_id=actor_id,
            now=moment,
        )
    return enabled


def confirm_memory(
    store: StoreProtocol,
    memory_id: str,
    *,
    confirmed_by: str = "user",
    actor_id: str = "user",
    now: Optional[str] = None,
) -> Memory:
    """记忆生效（`CANDIDATE → PENDING_CONFIRMATION → ENABLED`，只走合法转换）。"""
    moment = now or store.now()
    memory = get_memory(store, memory_id)
    if str(memory.state) == str(RuleState.ENABLED):
        return memory
    pending = memory
    if str(memory.state) != str(RuleState.PENDING_CONFIRMATION):
        pending = store.transition(
            "Memory",
            memory.id,
            str(RuleState.PENDING_CONFIRMATION),
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="用户确认长期资产：待生效",
        )  # type: ignore[assignment]
    pending.confirmed_by = confirmed_by
    pending.confirmed_at = moment
    pending = store.put(
        pending,
        expected_revision=pending.revision,
        actor_kind=ActorKind.USER,
        actor_id=actor_id,
        reason="记录确认者与时间",
    )  # type: ignore[assignment]
    enabled = store.transition(
        "Memory",
        pending.id,
        str(RuleState.ENABLED),
        actor_kind=ActorKind.USER,
        actor_id=actor_id,
        reason="长期资产生效",
    )
    return enabled  # type: ignore[return-value]


def grant_validity(
    store: StoreProtocol,
    *,
    memory_id: str,
    ttl_seconds: int,
    actor_id: str = "user",
    now: Optional[str] = None,
) -> Task:
    """给出/更新有效期（落在一条 `Task(kind="memory_validity")` 上；重复调用是幂等的）。"""
    moment = now or store.now()
    valid_until = _add_seconds(moment, int(ttl_seconds))
    task_id = validity_task_id_for(memory_id)
    existing = store.get("Task", task_id) if store.get_by_unique(MEMORY_UNIQUE_NAMESPACE + "_validity", memory_id) else None
    if isinstance(existing, Task):
        if str(existing.outputs.get("valid_until", "")) == valid_until:
            return existing
        raise ValidationError(
            "该记忆已有有效期 %s；改期必须走显式流程（避免悄悄延长记忆寿命）"
            % existing.outputs.get("valid_until")
        )
    memory = get_memory(store, memory_id)
    task = Task(
        id=task_id,
        case_id=memory.source_case_id or "",
        kind=MEMORY_VALIDITY_KIND,
        title="记忆有效期：%s" % memory.statement[:40],
        state=TaskState.PENDING,
        assignee="system",
        inputs={"valid_from": moment, "ttl_seconds": int(ttl_seconds)},
        outputs={"memory_id": memory_id, "valid_until": valid_until, "ttl_seconds": int(ttl_seconds)},
        links={MEMORY_LINK: [memory_id]},
        timeout_at=valid_until,
        created_at=moment,
        updated_at=moment,
    )
    stored = store.put(
        task,
        unique=(MEMORY_UNIQUE_NAMESPACE + "_validity", memory_id),
        actor_kind=ActorKind.USER,
        actor_id=actor_id,
        reason="给出记忆有效期（到点不再作为事实使用，历史保留）",
        audit_action=AUDIT_MEMORY_VALIDITY,
        audit_detail={"memory_id": memory_id, "valid_until": valid_until, "ttl_seconds": int(ttl_seconds)},
    )
    return stored  # type: ignore[return-value]


def validity_task(store: StoreProtocol, memory_id: str) -> Optional[Task]:
    try:
        obj = store.get("Task", validity_task_id_for(memory_id))
    except NotFound:
        return None
    return obj if isinstance(obj, Task) else None


def memory_valid_until(store: StoreProtocol, memory_id: str) -> Optional[str]:
    """该记忆的有效期终点（没有就是 None = 长期有效）。"""
    task = validity_task(store, memory_id)
    if task is None:
        return None
    until = str((task.outputs or {}).get("valid_until") or task.timeout_at or "")
    return until or None


# --------------------------------------------------------------------------------------
# 是否可用（"过期后不再被当作事实使用"）
# --------------------------------------------------------------------------------------


def is_expired(store: StoreProtocol, memory: Memory, *, now: Optional[str] = None) -> bool:
    moment = now or store.now()
    until = memory_valid_until(store, memory.id)
    return bool(until) and _not_before(moment, str(until))


def is_usable(store: StoreProtocol, memory: Memory, *, now: Optional[str] = None) -> bool:
    """可用 = 已生效 且 未过期。`now` 一过窗口就立刻不可用（不依赖清扫）。"""
    if str(memory.state) != str(RuleState.ENABLED):
        return False
    return not is_expired(store, memory, now=now)


def usable_memories(
    store: StoreProtocol,
    *,
    scope: Optional[str] = None,
    scope_ref: Optional[str] = None,
    asset_kinds: Optional[Sequence[str]] = None,
    now: Optional[str] = None,
) -> List[Memory]:
    """当前**可用**的记忆（已生效、未过期、范围匹配）。"""
    rows = store.find("Memory", where={"state": str(RuleState.ENABLED)}, limit=None)
    out: List[Memory] = []
    for item in rows:
        if not isinstance(item, Memory):
            continue
        if scope is not None and str(item.scope) != str(scope):
            continue
        if scope_ref is not None and (item.scope_ref or "") != (scope_ref or ""):
            continue
        if asset_kinds is not None and str(item.asset_kind) not in {str(x) for x in asset_kinds}:
            continue
        if is_usable(store, item, now=now):
            out.append(item)
    return out


def usable_statements(
    store: StoreProtocol,
    *,
    scope: Optional[str] = None,
    scope_ref: Optional[str] = None,
    now: Optional[str] = None,
) -> List[str]:
    """可用的记忆陈述（长期结论只能从这里取，过期的一律不在里面）。"""
    return [item.statement for item in usable_memories(store, scope=scope, scope_ref=scope_ref, now=now)]


# --------------------------------------------------------------------------------------
# 清扫（物化"过期"这件事）+ 历史保留
# --------------------------------------------------------------------------------------


def sweep_expired_memories(
    store: StoreProtocol,
    *,
    now: Optional[str] = None,
    actor_id: str = "system",
) -> List[str]:
    """把已过期的记忆推到 `REVOKED`，并结束它的有效期任务。**不删任何东西。**

    幂等：再跑一次不会重复写；返回本次被停用的记忆 ID。
    """
    moment = now or store.now()
    revoked: List[str] = []
    rows = store.find("Memory", where={"state": str(RuleState.ENABLED)}, limit=None)
    for item in rows:
        if not isinstance(item, Memory):
            continue
        until = memory_valid_until(store, item.id)
        if not until or not _not_before(moment, str(until)):
            continue
        store.transition(
            "Memory",
            item.id,
            str(RuleState.REVOKED),
            actor_kind="SYSTEM",
            actor_id=actor_id,
            reason="记忆有效期已过（valid_until=%s）：不再作为事实使用，历史保留" % until,
        )
        revoked.append(item.id)
        task = validity_task(store, item.id)
        if task is not None and str(task.state) == str(TaskState.PENDING):
            for target in (TaskState.READY, TaskState.RUNNING, TaskState.DONE):
                store.transition(
                    "Task",
                    task.id,
                    str(target),
                    actor_kind="SYSTEM",
                    actor_id=actor_id,
                    reason="记忆有效期到期：任务处理完成（记忆 %s 已停用）" % item.id,
                )
        store.append_audit(
            AuditEvent(
                at=moment,
                action=AUDIT_MEMORY_EXPIRED,
                actor_kind=ActorKind.SYSTEM,
                actor_id=actor_id,
                case_id=item.source_case_id or None,
                object_type="Memory",
                object_id=item.id,
                result="OK",
                reason="记忆有效期已过，已停用（历史保留）",
                detail={"memory_id": item.id, "valid_until": until, "swept_at": moment},
            )
        )
    return revoked


def revoke_memory(
    store: StoreProtocol,
    memory_id: str,
    *,
    reason: str,
    actor_id: str = "user",
) -> Memory:
    """用户主动撤销一条记忆（同样不删历史）。"""
    if not str(reason or "").strip():
        raise ValidationError("撤销记忆必须给出原因")
    memory = get_memory(store, memory_id)
    if str(memory.state) == str(RuleState.REVOKED):
        return memory
    revoked = store.transition(
        "Memory",
        memory.id,
        str(RuleState.REVOKED),
        actor_kind=ActorKind.USER,
        actor_id=actor_id,
        reason=reason,
    )
    return revoked  # type: ignore[return-value]


# --------------------------------------------------------------------------------------
# 记忆 → 事实（过期的不进入 Fact）
# --------------------------------------------------------------------------------------


def memory_citation(store: StoreProtocol, memory: Memory) -> Optional[Citation]:
    """记忆的来源引用：指向**来源资料原文**（没有来源资料就不编造引用，返回 None）。"""
    holders = list((memory.links or {}).get(MEMORY_SOURCE_DOCUMENT_LINK, []))
    if not holders:
        return None
    try:
        doc = store.get("SourceDocument", holders[0])
    except NotFound:
        return None
    if not isinstance(doc, SourceDocument):
        return None
    try:
        return Citation(
            source_object_id=doc.id,
            source_object_type="SourceDocument",
            path=doc.origin_path,
            locator="kv:memory",
            source_time=doc.received_at or None,
            source_version=doc.content_version,
            content_hash=doc.content_hash,
            supports="用户确认的长期记忆（可回到来源资料）",
        )
    except ValueError:  # pragma: no cover - 来源资料字段不全
        return None


def fact_from_memory(store: StoreProtocol, memory: Memory, *, case_id: str, actor_id: str = "growth") -> Optional[str]:
    """把一条**可用**记忆落成 `Fact`（必须能回到来源资料，否则不落）。"""
    citation = memory_citation(store, memory)
    if citation is None:
        return None
    fact_id = fact_id_for_memory(case_id, memory.id)
    existing = store.get_by_unique(MEMORY_FACT_UNIQUE_NAMESPACE, "%s:%s" % (case_id, memory.id))
    if existing is not None:
        return None
    fact = Fact(
        id=fact_id,
        case_id=case_id,
        subject="记忆:%s" % memory.id,
        statement=memory.statement,
        citations=[citation.to_dict()],
        confirmed_by_user=bool(memory.confirmed_by),
        source_system="growth:memory",
        links={"memory": [memory.id]},
    )
    try:
        stored = store.put(
            fact,
            unique=(MEMORY_FACT_UNIQUE_NAMESPACE, "%s:%s" % (case_id, memory.id)),
            actor_kind=ActorKind.AGENT,
            actor_id=actor_id,
            reason="可用记忆落成事实（带来源引用）",
            audit_action=AUDIT_MEMORY_FACT,
            audit_detail={"memory_id": memory.id, "case_id": case_id},
        )
    except DuplicateObject:  # pragma: no cover - 并发
        return None
    return stored.id  # type: ignore[union-attr]


def facts_from_memories(
    store: StoreProtocol,
    *,
    case_id: str,
    scope: Optional[str] = None,
    scope_ref: Optional[str] = None,
    now: Optional[str] = None,
    actor_id: str = "growth",
) -> MemoryFactResult:
    """把当前**可用**的记忆落成事实；过期的跳过（不进入 `Fact`），并在结果里说明原因。

    已写过的事实不会被删除——"过期后不再使用"与"历史保留"同时成立：
    旧事实是历史记录（带引用、可查），新事实只从可用记忆产生。
    """
    case = store.get("Case", case_id)
    if not isinstance(case, Case):  # pragma: no cover - 契约保证
        raise NotFound("Case 不存在: %s" % case_id)
    result = MemoryFactResult()
    moment = now or store.now()
    usable = usable_memories(store, scope=scope, scope_ref=scope_ref, now=moment)
    usable_ids = set()
    for memory in usable:
        usable_ids.add(memory.id)
        fact_id = fact_from_memory(store, memory, case_id=case_id, actor_id=actor_id)
        if fact_id is None:
            if store.get_by_unique(MEMORY_FACT_UNIQUE_NAMESPACE, "%s:%s" % (case_id, memory.id)) is not None:
                result.reused.append(memory.id)
            else:
                result.skipped.append((memory.id, "无法回到来源资料（不编造引用）"))
        else:
            result.created.append(fact_id)
    # 没被用上的记忆要能说清"为什么没用"（未生效 / 已过期），而不是静默忽略
    for item in all_memories(store):
        if item.id in usable_ids:
            continue
        if str(item.state) != str(RuleState.ENABLED):
            result.skipped.append((item.id, "未生效（state=%s）" % str(item.state)))
        elif is_expired(store, item, now=moment):
            result.skipped.append((item.id, "已过期（%s）" % memory_valid_until(store, item.id)))
    return result


def get_memory(store: StoreProtocol, memory_id: str) -> Memory:
    obj = store.get("Memory", memory_id)
    if not isinstance(obj, Memory):  # pragma: no cover - 契约保证
        raise NotFound("Memory 不存在: %s" % memory_id)
    return obj


def all_memories(store: StoreProtocol) -> List[Memory]:
    """全部记忆（**含已过期/已撤销**）：历史保留、可查。"""
    return [item for item in store.find("Memory", limit=None) if isinstance(item, Memory)]


def _add_seconds(moment: str, seconds: int) -> str:
    return add_seconds(moment, int(seconds))


def _not_before(left: str, right: str) -> bool:
    """`left >= right`，按**解析后的时刻**比较（字符串比较会在时区/精度上出错）。

    时间戳格式不对就让它报错（`ValueError`）——不猜、不吞。
    """
    return parse_iso(left) >= parse_iso(right)


__all__ = [
    "AUDIT_MEMORY_EXPIRED",
    "AUDIT_MEMORY_FACT",
    "AUDIT_MEMORY_REMEMBERED",
    "AUDIT_MEMORY_VALIDITY",
    "MEMORY_FACT_UNIQUE_NAMESPACE",
    "MEMORY_LINK",
    "MEMORY_SOURCE_DOCUMENT_LINK",
    "MEMORY_UNIQUE_NAMESPACE",
    "MEMORY_VALIDITY_KIND",
    "MemoryFactResult",
    "all_memories",
    "confirm_memory",
    "fact_from_memory",
    "fact_id_for_memory",
    "facts_from_memories",
    "get_memory",
    "grant_validity",
    "is_expired",
    "is_usable",
    "memory_citation",
    "memory_id_for",
    "memory_valid_until",
    "remember",
    "revoke_memory",
    "sweep_expired_memories",
    "usable_memories",
    "usable_statements",
    "validity_task",
    "validity_task_id_for",
]
