"""MVP-6 成长资产 — 规则的候选 / 生效 / 废止 / 回滚 / 命中可证（SPEC §10.2、§10.3）。

一句话：**"Agent 学会了"必须能在库里查到**——哪一条规则、哪一版、依据哪次纠正、哪封原始邮件、
谁确认的；并且**废止不清史、回滚出新版**。

生命周期（走契约 `Rule` 状态机，见 `contracts/transitions.py` 的 `_RULE_TABLE`）
================================================================================

```text
propose_rule    CANDIDATE            候选：带来源 + 适用范围，**未确认不得生效**
confirm_rule    CANDIDATE → PENDING_CONFIRMATION → ENABLED
enable_new_version  旧版 ENABLED → SUPERSEDED（superseded_by 指向新版本）
revoke_rule     ENABLED|SUPERSEDED → REVOKED（revoked_at 留痕，对象不删）
rollback_rule   产生**新版本**（内容取自历史版本），历史版本一个都不改
```

三条刻意的设计选择
------------------

1. **未确认不得生效**：`ENABLED` 只能由 `confirm_rule` 经过 `PENDING_CONFIRMATION` 走到；
   想从 `CANDIDATE` 直接写 `ENABLED` 会被状态机拒绝（`ILLEGAL_TRANSITION`），
   本模块也不提供"跳过确认"的开关。
2. **回滚 = 新版本，不是"把旧版重新点亮"**：契约状态机允许 `REVOKED → ENABLED`，
   但那样做审计上就无法回答"第 3 周时生效的是哪一版"（旧版本会重新变成"当前生效"）。
   所以 `rollback_rule` 新建一个版本（`version = max+1`），内容取自目标历史版本，
   并用 `links["rollback_of"]` 指向它。历史版本保持 `REVOKED`/`SUPERSEDED` 不动。
   （状态机是超集，我们只走更严格的那条路径。）
3. **废止与替代都不改历史事务**：旧 Case 的 `Case.rule_ids`、旧 `Fact.links["rule_version"]`、
   旧审计事件一律不动——本模块没有任何"重写历史"的代码路径。

命中可证（SPEC §10.3"每次命中规则都要记录规则版本和对结果的影响"）
==================================================================

一次命中会落三处**持久化**证据（都不在进程内字典里）：

- 审计事件 `action="RULE_HIT"`，`detail` 里带 `rule_id` / `rule_version` / `effect` / `slots` /
  `applied`（`record_rule_hits`）；
- `Case.rule_ids` 追加命中的规则版本 ID（`link_case_rule`）；
- 受影响的 `Fact.links["rule_version"]` 指向规则版本 ID（`link_facts_to_rule`）——
  `links` 是契约文档里"关联 ID 索引（跨对象反查用）"的正规用法。

幂等
====

`propose_rule` 用 `put(unique=("growth_rule_version", "<logical_key>#v<N>"))`：
同一次纠正重复提交只会得到同一个对象（`DUPLICATE_OBJECT` 会被翻译成"取回既有对象"）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..contracts.enums import ActorKind, AuditAction, RuleState
from ..contracts.errors import DuplicateObject, NotFound, ValidationError
from ..contracts.ids import digest_of, idempotency_key, new_id
from ..contracts.objects import AuditEvent, Case, Fact, Rule
from ..contracts.store_api import StoreProtocol
from ..contracts.timeutil import parse_iso
from .scope import MatchInput, RuleScope, parse_scope, sort_by_version

#: 规则版本唯一键的命名空间（幂等：同一逻辑规则的同一版本只有一个对象）
RULE_UNIQUE_NAMESPACE = "growth_rule_version"

#: 审计动作名（可 grep、可断言；不在契约的 AuditAction 枚举里，用字符串明确表达）
AUDIT_PROPOSE = "PROPOSE_RULE"
AUDIT_CONFIRM = "CONFIRM_RULE"
AUDIT_SUPERSEDE = "SUPERSEDE_RULE"
AUDIT_REVOKE = "REVOKE_RULE"
AUDIT_ROLLBACK = "ROLLBACK_RULE"
AUDIT_HIT = "RULE_HIT"

#: `Fact.links` 里登记"依据哪一版规则得出"的关联名
FACT_RULE_LINK = "rule_version"

#: 规则来源里"哪封原始邮件"的关联名
RULE_SOURCE_DOCUMENT_LINK = "source_document"


# --------------------------------------------------------------------------------------
# 输入
# --------------------------------------------------------------------------------------


@dataclass
class Correction:
    """一次**用户明确纠正**（SPEC §10.2 要求的七件事：原判断、纠正、范围、示例、来源事务、建议规则、影响）。

    这是"规则从哪来"的唯一入口：没有明确纠正就没有规则候选。
    """

    #: 用户确认后的规则文本（人可读）
    statement: str
    #: 用户原话（可追溯"哪次纠正"）
    user_message: str
    #: 被纠正的原判断（例如"把报名截止当成活动开始"）
    correction_of: str
    #: 机器可读的适用范围 + 效果
    scope: RuleScope
    #: 来源事务（哪个 Case）
    source_case_id: str = ""
    #: 来源原始邮件（哪封邮件）
    source_document_id: str = ""
    #: 谁确认
    confirmed_by: str = "user"
    #: 示例（原文片段）
    examples: List[str] = field(default_factory=list)
    priority: int = 100

    def validate(self) -> None:
        for name in ("statement", "user_message", "correction_of"):
            if not str(getattr(self, name) or "").strip():
                raise ValidationError("规则候选缺少 %s（来源必须完整，SPEC §10.2）" % name)
        if not isinstance(self.scope, RuleScope):  # pragma: no cover - 类型误用
            raise ValidationError("correction.scope 必须是 RuleScope")


@dataclass
class RuleHit:
    """一次规则命中（持久化前先在内存里成形，落库由 `record_rule_hits` 负责）。"""

    rule_id: str
    rule_version: int
    logical_key: str
    effect: str
    slots: Tuple[str, str]
    document_id: str = ""
    case_id: str = ""
    applied: bool = False
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "logical_key": self.logical_key,
            "effect": self.effect,
            "slots": list(self.slots),
            "document_id": self.document_id,
            "case_id": self.case_id,
            "applied": self.applied,
            "detail": self.detail,
        }

    def summary_line(self) -> str:
        return "命中规则 %s v%d（%s，%s）→ %s" % (
            self.rule_id,
            self.rule_version,
            self.effect,
            "已应用" if self.applied else "未应用",
            self.detail or "-",
        )


# --------------------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------------------


def rule_id_for(scope: RuleScope, version: int) -> str:
    """确定性规则版本 ID：同一逻辑规则的同一版本永远同一个 ID（幂等与可复现的基础）。"""
    token = digest_of([scope.logical_key, int(version)])[:32]
    return new_id("Rule", token=token)


def unique_value(scope: RuleScope, version: int) -> str:
    return "%s#v%d" % (scope.logical_key, int(version))


def rule_versions(store: StoreProtocol, scope: RuleScope) -> List[Rule]:
    """同一逻辑规则的全部版本（**含已废止/已被替代的**），按 `version` 升序。

    版本历史是"废止不清史"的另一半：任何时刻都能回答"这条规则经历了几版、每版什么内容"。
    """
    rows = store.find("Rule", where={"scope": scope.kind, "scope_ref": scope.spec}, limit=None)
    return list(sort_by_version([item for item in rows if isinstance(item, Rule)]))


def versions_by_logical_key(store: StoreProtocol, logical_key: str) -> List[Rule]:
    """按逻辑键取全部版本（扫描 `Rule`，用说明串反解；扫描范围有界且结果按版本排序）。"""
    out: List[Rule] = []
    for item in store.find("Rule", limit=None):
        if not isinstance(item, Rule):
            continue
        try:
            scope = parse_scope(item.scope_ref)
        except ValidationError:
            continue  # 不是成长模块写的规则（比如别的模块手写的 Rule）：跳过，不猜
        if scope.logical_key == logical_key:
            out.append(item)
    return list(sort_by_version(out))


def get_rule(store: StoreProtocol, rule_id: str) -> Rule:
    obj = store.get("Rule", rule_id)
    if not isinstance(obj, Rule):  # pragma: no cover - 契约保证
        raise NotFound("Rule 不存在: %s" % rule_id)
    return obj


def current_version(store: StoreProtocol, scope: RuleScope) -> Optional[Rule]:
    """该逻辑规则当前**生效**的版本（没有就是 None）。"""
    enabled = store.find("Rule", where={"state": str(RuleState.ENABLED)}, limit=None)
    for item in enabled:
        if not isinstance(item, Rule):
            continue
        try:
            other = parse_scope(item.scope_ref)
        except ValidationError:
            continue
        if other.logical_key == scope.logical_key:
            return item
    return None


def enabled_rules(store: StoreProtocol) -> List[Rule]:
    """全部生效版本（按版本升序）；调用方再用 `matches()` 做适用性过滤。"""
    rows = store.find("Rule", where={"state": str(RuleState.ENABLED)}, limit=None)
    return list(sort_by_version([item for item in rows if isinstance(item, Rule)]))


def effective_rules(
    store: StoreProtocol,
    *,
    match: MatchInput,
    now: Optional[str] = None,
) -> List[Tuple[Rule, RuleScope]]:
    """本次输入**真正命中**的规则版本（已生效 + 适用范围匹配 + 已到生效时间）。

    返回 `(Rule, RuleScope)` 对，避免调用方重复解析说明串。
    """
    moment = now or store.now()
    out: List[Tuple[Rule, RuleScope]] = []
    for item in enabled_rules(store):
        try:
            scope = parse_scope(item.scope_ref)
        except ValidationError:
            continue
        if item.effective_from and parse_iso(str(item.effective_from)) > parse_iso(moment):
            continue  # 还没到生效时间：不命中（也不删、不报错）
        if scope.matches(sender=match.sender, subject=match.subject, refs=match.refs):
            out.append((item, scope))
    return out


# --------------------------------------------------------------------------------------
# 写：候选 / 确认 / 生效 / 替代 / 废止 / 回滚
# --------------------------------------------------------------------------------------


def _next_version(store: StoreProtocol, scope: RuleScope) -> int:
    existing = rule_versions(store, scope)
    return (max((int(item.version) for item in existing), default=0)) + 1


def _source_document_id(rule: Rule) -> str:
    holders = list((rule.links or {}).get(RULE_SOURCE_DOCUMENT_LINK, []))
    return str(holders[0]) if holders else ""


def _same_correction(store: StoreProtocol, scope: RuleScope, correction: Correction) -> Optional[Rule]:
    """同一次纠正是否已经落过版本（幂等的判据：陈述 + 用户原话 + 来源都相同）。

    只有"同一次纠正重复提交"才算重复；**改变措辞或来源**是一次新的纠正，应当产生新版本
    （否则"成长"就没有版本可谈了）。
    """
    for item in rule_versions(store, scope):
        if (
            item.statement == str(correction.statement).strip()
            and str(item.source_user_message or "") == str(correction.user_message)
            and str(item.source_case_id or "") == str(correction.source_case_id or "")
            and _source_document_id(item) == str(correction.source_document_id or "")
        ):
            return item
    return None


def propose_rule(
    store: StoreProtocol,
    correction: Correction,
    *,
    actor_id: str = "user",
    now: Optional[str] = None,
) -> Rule:
    """把一次明确纠正落成**规则候选**（`CANDIDATE`，未确认不得生效）。

    幂等：同一逻辑规则的同一版本重复提交 → 返回既有对象（不重复建、不改内容）。
    """
    correction.validate()
    moment = now or store.now()
    scope = correction.scope
    existing_rule = _same_correction(store, scope, correction)
    if existing_rule is not None:
        return existing_rule  # 同一次纠正重复提交：幂等返回，不产生第二个版本
    version = _next_version(store, scope)
    existing = store.get_by_unique(RULE_UNIQUE_NAMESPACE, unique_value(scope, version))
    if existing is not None and isinstance(existing, Rule):
        return existing

    rule = Rule(
        id=rule_id_for(scope, version),
        statement=str(correction.statement).strip(),
        state=RuleState.CANDIDATE,
        version=version,
        scope=scope.kind,
        scope_ref=scope.spec,
        priority=int(correction.priority),
        source_case_id=correction.source_case_id or None,
        source_user_message=correction.user_message,
        examples=[scope.describe(), *[str(item) for item in correction.examples]],
        confirmed_by=None,
        confirmed_at=None,
        links={
            "source_document": [correction.source_document_id] if correction.source_document_id else [],
            "case": [correction.source_case_id] if correction.source_case_id else [],
        },
        created_at=moment,
        updated_at=moment,
    )
    detail: Dict[str, Any] = {
        "rule_spec": scope.spec,
        "rule_version": version,
        "effect": scope.effect,
        "slots": list(scope.slots),
        "correction_of": correction.correction_of,
        "user_message": correction.user_message,
        "source_document_id": correction.source_document_id,
        "source_case_id": correction.source_case_id,
    }
    try:
        saved = store.put(
            rule,
            unique=(RULE_UNIQUE_NAMESPACE, unique_value(scope, version)),
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="用户纠正 → 规则候选 v%d（未确认不得生效）" % version,
            audit_action=AUDIT_PROPOSE,
            audit_detail=detail,
        )
    except DuplicateObject:  # pragma: no cover - 并发同版本提交
        again = store.get_by_unique(RULE_UNIQUE_NAMESPACE, unique_value(scope, version))
        if again is None:  # pragma: no cover - 不可达
            raise
        return again
    if not isinstance(saved, Rule):  # pragma: no cover - 契约保证
        raise ValidationError("put 返回的不是 Rule: %r" % (saved,))
    return saved


def confirm_rule(
    store: StoreProtocol,
    rule_id: str,
    *,
    confirmed_by: str = "user",
    actor_id: str = "user",
    now: Optional[str] = None,
    reason: str = "",
) -> Rule:
    """用户确认后生效（`CANDIDATE → PENDING_CONFIRMATION → ENABLED`）。

    只走状态机的合法转换：直接写 `ENABLED` 是不可能的（这是"未确认不得生效"的实现方式）。
    """
    moment = now or store.now()
    rule = get_rule(store, rule_id)
    if str(rule.state) == str(RuleState.ENABLED):
        return rule  # 已生效：幂等返回，不重复写
    if str(rule.state) == str(RuleState.PENDING_CONFIRMATION):
        pending = rule
    else:
        pending = store.transition(
            "Rule",
            rule.id,
            str(RuleState.PENDING_CONFIRMATION),
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="用户确认规则 v%d：进入待生效" % int(rule.version),
        )
    assert isinstance(pending, Rule)  # pragma: no cover - 契约保证
    if pending.confirmed_by != confirmed_by or pending.confirmed_at != moment:
        pending.confirmed_by = confirmed_by
        pending.confirmed_at = moment
        pending.effective_from = moment
        pending = store.put(
            pending,
            expected_revision=pending.revision,
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="记录确认者与生效时间（SPEC §10.2）",
            audit_action=AUDIT_CONFIRM,
            audit_detail={"rule_id": pending.id, "rule_version": int(pending.version), "confirmed_by": confirmed_by},
        )
    assert isinstance(pending, Rule)  # pragma: no cover
    if str(pending.state) == str(RuleState.ENABLED):  # pragma: no cover - 上面的幂等分支已覆盖
        return pending
    enabled = store.transition(
        "Rule",
        pending.id,
        str(RuleState.ENABLED),
        actor_kind=ActorKind.USER,
        actor_id=actor_id,
        reason=reason or "规则生效 v%d：%s" % (int(pending.version), pending.statement[:60]),
    )
    assert isinstance(enabled, Rule)  # pragma: no cover
    # 同一条逻辑规则只允许一个生效版本：确认新版本时把同 key 的其它生效版本转为历史
    # （不删、不改内容；旧 Case 的引用照旧指向旧版本）
    logical_key = parse_scope(enabled.scope_ref).logical_key
    for other in versions_by_logical_key(store, logical_key):
        if other.id == enabled.id or str(other.state) != str(RuleState.ENABLED):
            continue
        supersede_rule(
            store,
            old_id=other.id,
            new_id_=enabled.id,
            actor_id=actor_id,
            reason="同一条规则的新版本 v%d 生效，旧版本 v%d 转为历史（不删、不改内容）"
            % (int(enabled.version), int(other.version)),
        )
    return enabled


def supersede_rule(
    store: StoreProtocol,
    *,
    old_id: str,
    new_id_: str,
    actor_id: str = "user",
    reason: str = "",
) -> Rule:
    """旧版本 → `SUPERSEDED`，`superseded_by` 指向新版本（旧对象只改这两个字段，历史内容不动）。"""
    old = get_rule(store, old_id)
    if str(old.state) == str(RuleState.SUPERSEDED) and old.superseded_by == new_id_:
        return old
    superseded = store.transition(
        "Rule",
        old.id,
        str(RuleState.SUPERSEDED),
        actor_kind=ActorKind.USER,
        actor_id=actor_id,
        reason=reason or "被新版本 %s 取代（历史保留）" % new_id_,
    )
    assert isinstance(superseded, Rule)  # pragma: no cover
    superseded.superseded_by = new_id_
    saved = store.put(
        superseded,
        expected_revision=superseded.revision,
        actor_kind=ActorKind.USER,
        actor_id=actor_id,
        reason="记录取代关系（superseded_by）",
        audit_action=AUDIT_SUPERSEDE,
        audit_detail={"old_rule_id": old.id, "old_rule_version": int(old.version), "new_rule_id": new_id_},
    )
    assert isinstance(saved, Rule)  # pragma: no cover
    return saved


def enable_new_version(
    store: StoreProtocol,
    correction: Correction,
    *,
    actor_id: str = "user",
    now: Optional[str] = None,
) -> Rule:
    """"成长"的主路径：一次新纠正 → 新版本候选 → 确认生效 → 旧版本 `SUPERSEDED`。

    旧版本不会被删除、内容不会被改写；旧 Case 的引用照旧指向旧版本。
    """
    moment = now or store.now()
    scope = correction.scope
    previous = current_version(store, scope)
    new_rule = propose_rule(store, correction, actor_id=actor_id, now=moment)
    enabled = confirm_rule(store, new_rule.id, confirmed_by=correction.confirmed_by, actor_id=actor_id, now=moment)
    if previous is not None and previous.id != enabled.id:
        supersede_rule(
            store,
            old_id=previous.id,
            new_id_=enabled.id,
            actor_id=actor_id,
            reason="新版本 v%d 生效，旧版本转为历史（不删、不改内容）" % int(enabled.version),
        )
    return enabled


def replace_rule(
    store: StoreProtocol,
    *,
    old_rule_id: str,
    correction: Correction,
    actor_id: str = "user",
    now: Optional[str] = None,
) -> Rule:
    """用户纠正**已有规则**：新规则生效 + 旧规则废止（不改旧记录、不删历史）。

    这是"规则演进"的推荐路径。注意它与"冲突"的区别：

    - **纠正**（本函数）：用户明确说"以后按新的来" → 旧规则废止、新规则生效，没有冲突；
    - **冲突**（`bridge.py`）：两条**当前都生效**的规则对同一槽位给出不同结论 →
      不静默选一个，转为 `Question(kind="conflict")` 并阻塞（契约 §14.5）。
    """
    moment = now or store.now()
    old = get_rule(store, old_rule_id)
    new_rule = propose_rule(store, correction, actor_id=actor_id, now=moment)
    enabled = confirm_rule(store, new_rule.id, confirmed_by=correction.confirmed_by, actor_id=actor_id, now=moment)
    revoke_rule(
        store,
        old.id,
        reason="被新规则 %s（v%d）取代：%s" % (enabled.id, int(enabled.version), correction.correction_of),
        actor_id=actor_id,
        now=moment,
    )
    return enabled


def revoke_rule(
    store: StoreProtocol,
    rule_id: str,
    *,
    reason: str,
    actor_id: str = "user",
    now: Optional[str] = None,
) -> Rule:
    """废止某版本：后续事务不再命中它，**历史照旧**。

    - 只把状态推到 `REVOKED` 并写 `revoked_at`；
    - 已 `REVOKED` 时幂等返回（不重复写审计）；
    - 不碰任何旧 Case / 旧 Fact / 旧审计事件。
    """
    if not str(reason or "").strip():
        raise ValidationError("废止规则必须给出原因（审计要能回答为什么停用）")
    moment = now or store.now()
    rule = get_rule(store, rule_id)
    if str(rule.state) == str(RuleState.REVOKED):
        return rule
    if str(rule.state) not in (str(RuleState.ENABLED), str(RuleState.SUPERSEDED)):
        raise ValidationError(
            "只有已生效/已被替代的规则可以废止，当前状态 %s（候选请直接拒绝，不必废止）" % str(rule.state)
        )
    revoked = store.transition(
        "Rule",
        rule.id,
        str(RuleState.REVOKED),
        actor_kind=ActorKind.USER,
        actor_id=actor_id,
        reason=reason,
    )
    assert isinstance(revoked, Rule)  # pragma: no cover
    revoked.revoked_at = moment
    saved = store.put(
        revoked,
        expected_revision=revoked.revision,
        actor_kind=ActorKind.USER,
        actor_id=actor_id,
        reason="记录废止时间（历史保留，不删除）",
        audit_action=AUDIT_REVOKE,
        audit_detail={"rule_id": rule.id, "rule_version": int(rule.version), "reason": reason},
    )
    assert isinstance(saved, Rule)  # pragma: no cover
    return saved


def rollback_rule(
    store: StoreProtocol,
    scope: RuleScope,
    *,
    to_version: int,
    reason: str,
    actor_id: str = "user",
    now: Optional[str] = None,
    confirmed_by: str = "user",
) -> Rule:
    """回滚到某个历史版本：**产生新版本**，历史一个都不改（SPEC §10.3"可恢复"、§10.5"支持回滚"）。

    - 新版本内容 = 目标历史版本的 `statement` / 适用范围 / 效果；
    - 新版本用 `links["rollback_of"]` 指向被回滚到的那个版本；
    - 当前生效版本转 `SUPERSEDED`；目标历史版本保持它原来的状态（通常是 `REVOKED`/`SUPERSEDED`）。
    """
    if not str(reason or "").strip():
        raise ValidationError("回滚必须给出原因（审计要能回答为什么回滚）")
    moment = now or store.now()
    versions = rule_versions(store, scope)
    target = next((item for item in versions if int(item.version) == int(to_version)), None)
    if target is None:
        raise NotFound("该规则没有 v%d（现有版本：%s）" % (to_version, [int(i.version) for i in versions]))
    previous = current_version(store, scope)
    correction = Correction(
        statement=target.statement,
        user_message="回滚到 v%d：%s" % (int(target.version), reason),
        correction_of="当前生效版本 %s（v%s）"
        % (previous.id if previous is not None else "-", getattr(previous, "version", "-")),
        scope=scope,
        source_case_id=target.source_case_id or "",
        confirmed_by=confirmed_by,
        examples=["回滚目标：%s（v%d）" % (target.id, int(target.version))],
        priority=int(target.priority),
    )
    new_rule = propose_rule(store, correction, actor_id=actor_id, now=moment)
    # 记下"回滚自哪一版"（links 是契约的关联 ID 索引，正规用法）
    if target.id not in (new_rule.links or {}).get("rollback_of", []):
        new_rule.links = dict(new_rule.links or {})
        new_rule.links["rollback_of"] = [target.id]
        new_rule = store.put(
            new_rule,
            expected_revision=new_rule.revision,
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            reason="记录回滚来源版本",
            audit_action=AUDIT_ROLLBACK,
            audit_detail={
                "rollback_to_rule_id": target.id,
                "rollback_to_version": int(target.version),
                "new_rule_id": new_rule.id,
                "new_rule_version": int(new_rule.version),
                "reason": reason,
            },
        )
    enabled = confirm_rule(store, new_rule.id, confirmed_by=confirmed_by, actor_id=actor_id, now=moment)
    if previous is not None and previous.id != enabled.id:
        supersede_rule(
            store,
            old_id=previous.id,
            new_id_=enabled.id,
            actor_id=actor_id,
            reason="回滚到 v%d：当前版本转为历史（不删、不改内容）" % int(target.version),
        )
    return enabled


# --------------------------------------------------------------------------------------
# 命中可证：审计 + Case/Fact 关联
# --------------------------------------------------------------------------------------


def record_rule_hits(
    store: StoreProtocol,
    hits: Sequence[RuleHit],
    *,
    actor_id: str = "growth",
    at: Optional[str] = None,
) -> List[str]:
    """把命中写进**只追加**的审计（"遵守了规则"从此可查：能看到命中的规则版本号）。

    返回审计事件 ID 列表（测试直接断言它们，而不是断言内部返回值）。
    """
    moment = at or store.now()
    event_ids: List[str] = []
    for hit in hits:
        event = store.append_audit(
            AuditEvent(
                at=moment,
                action=AUDIT_HIT,
                actor_kind=ActorKind.AGENT,
                actor_id=actor_id,
                case_id=hit.case_id or None,
                object_type="SourceDocument" if hit.document_id else None,
                object_id=hit.document_id or None,
                result="OK",
                reason="命中规则 %s v%d：%s" % (hit.rule_id, hit.rule_version, hit.detail or hit.effect),
                detail={
                    "rule_id": hit.rule_id,
                    "rule_version": int(hit.rule_version),
                    "logical_key": hit.logical_key,
                    "effect": hit.effect,
                    "slots": list(hit.slots),
                    "applied": bool(hit.applied),
                    "document_id": hit.document_id,
                    "case_id": hit.case_id,
                    "detail": hit.detail,
                },
            )
        )
        event_ids.append(event.id)
    return event_ids


def rule_hits(
    store: StoreProtocol,
    *,
    document_id: Optional[str] = None,
    case_id: Optional[str] = None,
    rule_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[RuleHit]:
    """读回命中记录（从审计里读；进程重启后一样能读到）。"""
    if document_id is None and case_id is None:
        raise ValidationError("查命中记录至少要给 document_id 或 case_id（避免整表扫描）")
    events = store.audit_trail(object_id=document_id, case_id=case_id, limit=limit)
    out: List[RuleHit] = []
    for event in events:
        if str(event.action) != AUDIT_HIT:
            continue
        detail = dict(event.detail or {})
        if rule_id is not None and detail.get("rule_id") != rule_id:
            continue
        slots = tuple(detail.get("slots") or ())
        out.append(
            RuleHit(
                rule_id=str(detail.get("rule_id", "")),
                rule_version=int(detail.get("rule_version", 0)),
                logical_key=str(detail.get("logical_key", "")),
                effect=str(detail.get("effect", "")),
                slots=slots,  # type: ignore[arg-type]
                document_id=str(detail.get("document_id", "")),
                case_id=str(detail.get("case_id", "")),
                applied=bool(detail.get("applied")),
                detail=str(detail.get("detail", "")),
            )
        )
    return out


def link_case_rule(
    store: StoreProtocol,
    *,
    case_id: str,
    rule_id: str,
    actor_id: str = "growth",
) -> Optional[str]:
    """把规则版本登记到 `Case.rule_ids`（幂等）。返回 Case ID（没变就返回 None）。"""
    case = store.get("Case", case_id)
    if not isinstance(case, Case):  # pragma: no cover - 契约保证
        return None
    if rule_id in list(case.rule_ids or []):
        return None
    case.rule_ids = list(case.rule_ids or []) + [rule_id]
    store.put(
        case,
        expected_revision=case.revision,
        actor_kind=ActorKind.AGENT,
        actor_id=actor_id,
        reason="登记本事务依据的规则版本（SPEC §10.3）",
        audit_detail={"rule_id": rule_id},
    )
    return case.id


def link_facts_to_rule(
    store: StoreProtocol,
    *,
    fact_ids: Iterable[str],
    rule_id: str,
    actor_id: str = "growth",
) -> List[str]:
    """把"依据哪一版规则得出"写进 `Fact.links["rule_version"]`（幂等）。"""
    changed: List[str] = []
    for fact_id in fact_ids:
        obj = store.get("Fact", fact_id)
        if not isinstance(obj, Fact):
            continue
        links = dict(obj.links or {})
        holders = list(links.get(FACT_RULE_LINK, []))
        if rule_id in holders:
            continue
        holders.append(rule_id)
        links[FACT_RULE_LINK] = holders
        obj.links = links
        store.put(
            obj,
            expected_revision=obj.revision,
            actor_kind=ActorKind.AGENT,
            actor_id=actor_id,
            reason="登记该事实依据的规则版本（%s）" % rule_id,
            audit_detail={"rule_id": rule_id, FACT_RULE_LINK: holders},
        )
        changed.append(fact_id)
    return changed


def facts_for_rule(store: StoreProtocol, rule_id: str, *, case_id: Optional[str] = None) -> List[Fact]:
    """反查：哪些事实是依据这个规则版本得出的（`links` 反向查询的用法示例）。"""
    rows = store.find("Fact", where={"case_id": case_id} if case_id else None, limit=None)
    out: List[Fact] = []
    for item in rows:
        if not isinstance(item, Fact):
            continue
        if rule_id in list((item.links or {}).get(FACT_RULE_LINK, [])):
            out.append(item)
    return out


def rule_hit_key(rule_id: str, document_id: str) -> str:
    """命中记录的稳定键（同一份文档命中的同一条规则只算一次，便于幂等校验）。"""
    return idempotency_key("rule_hit", rule_id, document_id)


def rule_asset_path(store_path: str, rule_id: str) -> str:
    """长期资产的落盘位置建议（SPEC §10.5：规则要能被 Git 管理）。

    只是**建议路径**（本模块不写文件）：`var/growth/rules/<rule_id>.md`。
    """
    return os.path.join(os.path.dirname(store_path), "growth", "rules", "%s.md" % rule_id)


__all__ = [
    "AUDIT_CONFIRM",
    "AUDIT_HIT",
    "AUDIT_PROPOSE",
    "AUDIT_REVOKE",
    "AUDIT_ROLLBACK",
    "AUDIT_SUPERSEDE",
    "FACT_RULE_LINK",
    "RULE_SOURCE_DOCUMENT_LINK",
    "RULE_UNIQUE_NAMESPACE",
    "Correction",
    "RuleHit",
    "confirm_rule",
    "current_version",
    "effective_rules",
    "enable_new_version",
    "enabled_rules",
    "facts_for_rule",
    "get_rule",
    "link_case_rule",
    "link_facts_to_rule",
    "propose_rule",
    "record_rule_hits",
    "replace_rule",
    "revoke_rule",
    "rollback_rule",
    "rule_asset_path",
    "rule_hit_key",
    "rule_hits",
    "rule_id_for",
    "rule_versions",
    "supersede_rule",
    "unique_value",
    "versions_by_logical_key",
]
