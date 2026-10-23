"""MVP-6 ↔ MVP-3 的**真实串联**：已生效的成长规则真的参与理解，并留下可查的命中记录。

这一层不做"另一个抽取器"，它做三件事：

```text
process_with_growth(store, document, ...)
  1. GrowthUnderstander.analyze(request)     包住 MVP-3 的 RuleBasedUnderstander：
       命中已生效规则 → 应用效果（或发现冲突）
  2. process_document(...)                   照旧把结果落成 Case/Fact/Question/Draft
  3. apply_growth(store, report, understander)
       写命中审计（rule_id + rule_version）→ 关联 Case.rule_ids / Fact.links["rule_version"]
```

三条不可让渡的约束
==================

1. **命中可证**："遵守了规则"不是自称——审计事件 `RULE_HIT` 里带规则**版本号**，
   `Fact.links["rule_version"]` 与 `Case.rule_ids` 能反查到具体是哪一版（`rules.py`）。
2. **冲突不选边**：两条已生效规则对同一目标槽位给出不同效果时，**两个效果都不应用**，
   改成 `Conflict` + `Question(kind="conflict")`（契约 §14.5 的约定），
   于是 `UnderstandingResult.draftable()` 为假 → 管线不会产出草稿 → 没有可发送结论。
3. **不猜**：命中不了的规则就是命中不了（发件人/主题不匹配就不应用）；
   规则说明串解析失败、效果不在词表里、槽位不在契约槽位表里，一律拒绝（`ValidationError`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..contracts.errors import PermissionDenied, ValidationError
from ..contracts.objects import Case, SourceDocument
from ..contracts.store_api import StoreProtocol
from ..contracts.timeutil import TemporalField
from ..understand import (
    AnalysisInput,
    CaseFlowReport,
    Conflict,
    ModelCallLog,
    QuestionCandidate,
    RuleBasedUnderstander,
    TimeField,
    UnderstandingResult,
    process_document,
)
from .rules import (
    Correction,
    RuleHit,
    effective_rules,
    link_case_rule,
    link_facts_to_rule,
    record_rule_hits,
)
from .scope import (
    EFFECT_ALLOW_ALIAS,
    EFFECT_REQUIRE_QUESTION,
    MatchInput,
    RuleScope,
    describe_effects,
    parse_scope,
    slot_label,
)

#: 本层写入的 `Case.blocked_reason` 前缀（便于控制台/测试识别来源）
RULE_CONFLICT_PREFIX = "规则冲突"


# --------------------------------------------------------------------------------------
# 决策记录
# --------------------------------------------------------------------------------------


@dataclass
class GrowthDecision:
    """一条规则在这次理解里的处置（可观察：应用了没有、为什么）。"""

    rule_id: str
    rule_version: int
    logical_key: str
    effect: str
    slots: Tuple[str, str]
    applied: bool = False
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_version": int(self.rule_version),
            "logical_key": self.logical_key,
            "effect": self.effect,
            "slots": list(self.slots),
            "applied": bool(self.applied),
            "detail": self.detail,
        }

    def line(self) -> str:
        return "规则 %s v%d：%s（%s）" % (
            self.rule_id,
            int(self.rule_version),
            self.effect,
            self.detail or ("已应用" if self.applied else "未应用"),
        )


@dataclass
class GrowthReport:
    """一次"带成长资产的理解"的完整记录（原始证据结构，`to_dict()` 可直接打印）。"""

    document_id: str = ""
    case_id: str = ""
    case_state: str = ""
    hits: List[RuleHit] = field(default_factory=list)
    decisions: List[GrowthDecision] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    audit_event_ids: List[str] = field(default_factory=list)
    linked_fact_ids: List[str] = field(default_factory=list)
    linked_case_rule_ids: List[str] = field(default_factory=list)
    applied_rule_ids: List[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def hit_lines(self) -> List[str]:
        return [item.summary_line() for item in self.hits]

    def summary_line(self) -> str:
        return "growth doc=%s case=%s 命中=%d 应用=%d 冲突=%d" % (
            self.document_id,
            self.case_id,
            len(self.hits),
            len(self.applied_rule_ids),
            len(self.conflicts),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "case_id": self.case_id,
            "case_state": self.case_state,
            "hits": [item.to_dict() for item in self.hits],
            "decisions": [item.to_dict() for item in self.decisions],
            "conflicts": [dict(item) for item in self.conflicts],
            "audit_event_ids": list(self.audit_event_ids),
            "linked_fact_ids": list(self.linked_fact_ids),
            "linked_case_rule_ids": list(self.linked_case_rule_ids),
            "applied_rule_ids": list(self.applied_rule_ids),
            "blocked_reason": self.blocked_reason,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------------------
# 理解器：包住 MVP-3，应用已生效的规则
# --------------------------------------------------------------------------------------


class GrowthUnderstander:
    """在 MVP-3 的规则理解器之上应用**已生效的成长规则**。

    - 只有 `ENABLED` 且适用范围匹配、且 `effective_from <= now` 的规则会命中；
    - 规则不适用（缺来源槽位 / 目标槽位本来就有）时**不算命中**，不写审计；
    - 命中记录在 `analyze()` 后可从 `hits` / `decisions` / `conflicts` 读走，
      持久化由 `apply_growth()` 负责（这样命中记录里能带上 `case_id`）。
    """

    name = "growth+rules"

    def __init__(
        self,
        store: StoreProtocol,
        *,
        base: Optional[Any] = None,
        clock: Optional[Callable[[], str]] = None,
        actor_id: str = "growth",
        match_refs: Sequence[str] = (),
    ) -> None:
        self.store = store
        self.base = base or RuleBasedUnderstander()
        self.clock = clock
        self.actor_id = actor_id
        self.match_refs = tuple(match_refs)
        self.hits: List[RuleHit] = []
        self.decisions: List[GrowthDecision] = []
        self.conflicts: List[Dict[str, Any]] = []
        self.matched: List[Tuple[str, int]] = []
        self.last_match = MatchInput()

    # ------------------------------------------------------------------ 协议
    def call_log(self) -> ModelCallLog:
        getter = getattr(self.base, "call_log", None)
        if callable(getter):
            return getter()
        return ModelCallLog()

    def analyze(self, request: AnalysisInput) -> UnderstandingResult:
        result = self.base.analyze(request)
        self.hits = []
        self.decisions = []
        self.conflicts = []
        self.matched = []
        moment = self.clock() if callable(self.clock) else self.store.now()
        subject = request.source_text.subject if request.source_text is not None else ""
        match = MatchInput(sender=request.target_ref, subject=subject, refs=self.match_refs)
        self.last_match = match

        applicable: List[Tuple[Any, RuleScope, TimeField]] = []
        for rule, scope in effective_rules(self.store, match=match, now=moment):
            source_field = self._field(result, scope.source_slot)
            target_field = self._field(result, scope.target_slot)
            if source_field is None or target_field is not None:
                # 不适用：没有来源槽位，或目标槽位本来就有（没有分歧）→ 不算命中
                continue
            applicable.append((rule, scope, source_field))

        by_target: Dict[str, List[Tuple[Any, RuleScope, TimeField]]] = {}
        for item in applicable:
            by_target.setdefault(item[1].target_slot, []).append(item)

        for target_slot, items in sorted(by_target.items()):
            effects = sorted({item[1].effect for item in items})
            if len(effects) > 1:
                self._record_rule_conflict(result, target_slot=target_slot, items=items)
                continue
            self._apply_effect(result, target_slot=target_slot, items=items)
        result.validate()
        return result

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _field(result: UnderstandingResult, semantic: str) -> Optional[TimeField]:
        for item in result.time_fields:
            if item.semantic == semantic:
                return item
        return None

    @staticmethod
    def _has_question(result: UnderstandingResult, *, kind: str, affects: str) -> bool:
        for item in result.questions:
            if item.kind == kind and affects in list(item.affects or []):
                return True
        return False

    def _hit(
        self,
        rule: Any,
        scope: RuleScope,
        *,
        applied: bool,
        detail: str,
    ) -> None:
        self.matched.append((rule.id, int(rule.version)))
        self.hits.append(
            RuleHit(
                rule_id=rule.id,
                rule_version=int(rule.version),
                logical_key=scope.logical_key,
                effect=scope.effect,
                slots=scope.slots,
                applied=applied,
                detail=detail,
            )
        )
        self.decisions.append(
            GrowthDecision(
                rule_id=rule.id,
                rule_version=int(rule.version),
                logical_key=scope.logical_key,
                effect=scope.effect,
                slots=scope.slots,
                applied=applied,
                detail=detail,
            )
        )

    def _apply_effect(
        self,
        result: UnderstandingResult,
        *,
        target_slot: str,
        items: List[Tuple[Any, RuleScope, TimeField]],
    ) -> None:
        rule, scope, source_field = items[0]
        citations = list(source_field.citations or [])
        affects = "时间字段:%s" % target_slot
        if scope.effect == EFFECT_REQUIRE_QUESTION:
            detail = "缺「%s」→ 按规则追问（不得用「%s」顶替）" % (
                slot_label(target_slot),
                slot_label(scope.source_slot),
            )
            if not self._has_question(result, kind="missing_time", affects=affects):
                result.questions.append(
                    QuestionCandidate(
                        question=(
                            "通知里只有「%s」，没有「%s」。按已生效规则 %s v%d（用户纠正：%s），"
                            "不能用前者顶替后者。请确认「%s」是哪一天/哪一刻。"
                            % (
                                slot_label(scope.source_slot),
                                slot_label(target_slot),
                                rule.id,
                                int(rule.version),
                                str(rule.source_user_message or "")[:40],
                                slot_label(target_slot),
                            )
                        ),
                        kind="missing_time",
                        background="%s\n规则来源：用户纠正「%s」（来源 Case %s）"
                        % (scope.describe(), str(rule.source_user_message or "")[:120], rule.source_case_id or "-"),
                        affects=[affects],
                        citations=citations,
                    )
                )
        elif scope.effect == EFFECT_ALLOW_ALIAS:
            detail = "缺「%s」→ 按规则用「%s」认定" % (slot_label(target_slot), slot_label(scope.source_slot))
            if self._field(result, target_slot) is None:
                source_temporal = source_field.temporal
                result.time_fields.append(
                    TimeField(
                        temporal=TemporalField(
                            original_label=source_temporal.original_label,
                            semantic=target_slot,
                            original_text=source_temporal.original_text,
                            source_time_zone=source_temporal.source_time_zone,
                            normalized_utc=source_temporal.normalized_utc,
                            confidence=source_temporal.confidence,
                            evidence=list(source_temporal.evidence or []),
                        ),
                        citations=citations,
                        label_text="%s（按规则 %s v%d 认定）" % (slot_label(target_slot), rule.id, int(rule.version)),
                    )
                )
        else:  # pragma: no cover - RuleScope 构造时已拒绝未知效果
            raise ValidationError("未知规则效果: %s" % scope.effect)

        for extra_rule, extra_scope, _ in items:
            self._hit(
                extra_rule,
                extra_scope,
                applied=True,
                detail=detail
                if extra_rule.id == rule.id
                else "%s（与 %s v%d 效果一致，同一次处理即满足）" % (detail, rule.id, int(rule.version)),
            )

    def _record_rule_conflict(
        self,
        result: UnderstandingResult,
        *,
        target_slot: str,
        items: List[Tuple[Any, RuleScope, TimeField]],
    ) -> None:
        effects = describe_effects(sorted({item[1].effect for item in items}))
        sources = [
            {
                "rule_id": rule.id,
                "rule_version": int(rule.version),
                "effect": scope.effect,
                "scope": scope.spec,
                "user_message": str(rule.source_user_message or ""),
            }
            for rule, scope, _ in items
        ]
        detail = "%s：槽位「%s」被两条已生效规则给出不同结论（%s）。未裁决前两个效果都不应用，也不产出可发送结论。" % (
            RULE_CONFLICT_PREFIX,
            slot_label(target_slot),
            effects,
        )
        citations = list(items[0][2].citations or [])
        result.conflicts.append(
            Conflict(
                semantic=target_slot,
                values=sorted({item[1].effect for item in items}),
                sources=sources,
                citations=citations,
                detail=detail,
            )
        )
        affects = "时间字段:%s" % target_slot
        if not self._has_question(result, kind="conflict", affects=affects):
            result.questions.append(
                QuestionCandidate(
                    question="关于「%s」：两条已生效规则给出不同结论（%s）。以哪一条为准？"
                    % (
                        slot_label(target_slot),
                        "；".join(
                            "%s v%d → %s" % (rule.id, int(rule.version), scope.effect_label)
                            for rule, scope, _ in items
                        ),
                    ),
                    kind="conflict",
                    background=detail
                    + "\n"
                    + "\n".join(
                        "规则 %s v%d：%s（用户纠正：%s）"
                        % (rule.id, int(rule.version), scope.describe(), str(rule.source_user_message or "")[:80])
                        for rule, scope, _ in items
                    ),
                    affects=[affects],
                    citations=citations,
                )
            )
        self.conflicts.append(
            {
                "semantic": target_slot,
                "detail": detail,
                "rule_ids": [rule.id for rule, _, _ in items],
                "rule_versions": [int(rule.version) for rule, _, _ in items],
                "effects": sorted({scope.effect for _, scope, _ in items}),
            }
        )
        for rule, scope, _ in items:
            self._hit(rule, scope, applied=False, detail="%s（等待用户裁决，未应用）" % RULE_CONFLICT_PREFIX)


# --------------------------------------------------------------------------------------
# 落地：命中审计 + Case/Fact 关联
# --------------------------------------------------------------------------------------


def ensure_blocked_reason(
    store: StoreProtocol,
    *,
    case_id: str,
    reasons: Sequence[str],
    actor_id: str = "growth",
) -> Optional[str]:
    """把规则冲突的原因写进 `Case.blocked_reason`（已经写过就不重复写）。

    MVP-3 的写入层会用 `result.conflicts` 的 `detail` 自动填 `blocked_reason`；
    这里只是**兜底并明确化**，保证"冲突 → 事务被挡住"这件事在 Case 上一定看得见。
    """
    missing = [str(item) for item in reasons if str(item or "").strip()]
    if not missing:
        return None
    case = store.get("Case", case_id)
    if not isinstance(case, Case):  # pragma: no cover - 契约保证
        return None
    existing = str(case.blocked_reason or "")
    todo = [item for item in missing if item not in existing]
    if not todo:
        return None
    case.blocked_reason = "；".join([part for part in [existing] if part] + todo)
    store.put(
        case,
        expected_revision=case.revision,
        actor_kind="AGENT",
        actor_id=actor_id,
        reason="规则冲突未裁决：写明阻塞原因（契约 §14.5）",
        audit_detail={"blocked_reason": case.blocked_reason, "rule_conflict": True},
    )
    return case.blocked_reason


def apply_growth(
    store: StoreProtocol,
    report: CaseFlowReport,
    understander: GrowthUnderstander,
    *,
    actor_id: str = "growth",
    now: Optional[str] = None,
) -> GrowthReport:
    """把一次理解的命中**持久化**：审计（带规则版本号）+ `Case.rule_ids` + `Fact.links`。"""
    moment = now or store.now()
    hits: List[RuleHit] = []
    for item in understander.hits:
        hits.append(
            RuleHit(
                rule_id=item.rule_id,
                rule_version=int(item.rule_version),
                logical_key=item.logical_key,
                effect=item.effect,
                slots=item.slots,
                document_id=report.document_id,
                case_id=report.case_id,
                applied=bool(item.applied),
                detail=item.detail,
            )
        )
    event_ids = record_rule_hits(store, hits, actor_id=actor_id, at=moment)

    linked_facts: List[str] = []
    linked_case_rules: List[str] = []
    applied_rule_ids: List[str] = []
    for decision in understander.decisions:
        if decision.applied:
            fact_ids = [
                item.object_id
                for item in report.outcomes
                if item.kind == "Fact" and item.detail in tuple(decision.slots)
            ]
            linked_facts.extend(link_facts_to_rule(store, fact_ids=fact_ids, rule_id=decision.rule_id, actor_id=actor_id))
            applied_rule_ids.append(decision.rule_id)
        if link_case_rule(store, case_id=report.case_id, rule_id=decision.rule_id, actor_id=actor_id):
            linked_case_rules.append(decision.rule_id)

    blocked = None
    if understander.conflicts:
        blocked = ensure_blocked_reason(
            store,
            case_id=report.case_id,
            reasons=[str(item.get("detail", "")) for item in understander.conflicts],
            actor_id=actor_id,
        )
    case = store.get("Case", report.case_id)
    return GrowthReport(
        document_id=report.document_id,
        case_id=report.case_id,
        case_state=str(getattr(case, "state", "")),
        hits=hits,
        decisions=list(understander.decisions),
        conflicts=[dict(item) for item in understander.conflicts],
        audit_event_ids=event_ids,
        linked_fact_ids=linked_facts,
        linked_case_rule_ids=linked_case_rules,
        applied_rule_ids=applied_rule_ids,
        blocked_reason=str(getattr(case, "blocked_reason", "") or "") or None,
        notes=list(report.warnings),
    )


def process_with_growth(
    store: StoreProtocol,
    document: SourceDocument,
    *,
    eml_bytes: Optional[bytes] = None,
    raw_text: Optional[str] = None,
    base: Optional[Any] = None,
    clock: Optional[Callable[[], str]] = None,
    actor_id: str = "growth",
    match_refs: Sequence[str] = (),
    create_draft: bool = True,
    **kwargs: Any,
) -> Tuple[CaseFlowReport, GrowthReport]:
    """一次"带成长资产"的完整理解：MVP-3 落对象 + MVP-6 记命中。

    这是文档与测试里唯一推荐的入口——它保证"规则真的参与了这次理解"且"命中真的落了库"。
    """
    understander = GrowthUnderstander(store, base=base, clock=clock, actor_id=actor_id, match_refs=match_refs)
    report = process_document(
        store,
        document,
        understander=understander,
        eml_bytes=eml_bytes,
        raw_text=raw_text,
        create_draft=create_draft,
        **kwargs,
    )
    growth_report = apply_growth(store, report, understander, actor_id=actor_id)
    return report, growth_report


# --------------------------------------------------------------------------------------
# 门闸：冲突未解决前不得产出可发送结论（契约 §14.5）
# --------------------------------------------------------------------------------------


def case_is_blocked(store: StoreProtocol, case_id: str) -> bool:
    """事务是否被**规则冲突**挡住（`Case.blocked_reason` 里有规则冲突）。"""
    case = store.get("Case", case_id)
    return RULE_CONFLICT_PREFIX in str(getattr(case, "blocked_reason", "") or "")


def require_sendable(store: StoreProtocol, case_id: str) -> None:
    """要求"可以产出可发送结论"；被挡住就抛 `PermissionDenied`（带可读原因）。

    口径与契约 §14.5 一致：**只要 `Case.blocked_reason` 非空**（规则冲突、资料之间时间不一致、
    缺材料……），就不允许产出可发送结论——不区分是谁写的这个原因，宁可挡住也不发错。
    """
    case = store.get("Case", case_id)
    reason = str(getattr(case, "blocked_reason", "") or "")
    if reason:
        kind = "规则冲突" if RULE_CONFLICT_PREFIX in reason else "未解决的冲突"
        raise PermissionDenied("%s未裁决，不得产出可发送结论：%s" % (kind, reason))


def rule_conflict_reasons(store: StoreProtocol, case_id: str) -> List[str]:
    """把该 Case 的规则冲突原因拆出来（控制台展示用）。"""
    case = store.get("Case", case_id)
    reason = str(getattr(case, "blocked_reason", "") or "")
    return [part for part in reason.split("；") if RULE_CONFLICT_PREFIX in part]


def rule_conflict_questions(store: StoreProtocol, case_id: str) -> List[Any]:
    """该 Case 里"由规则冲突产生的"追问。

    `Question` 对象没有 `kind` 字段（这是契约与实现之间的一处不一致，见 `docs/mvp/mvp-6.md` §8），
    所以这里按 `background` 里的规则冲突标记来定位；模型层 `QuestionCandidate.kind == "conflict"`
    仍然是我们产出它的依据。
    """
    rows = store.find("Question", where={"case_id": case_id}, limit=None)
    return [item for item in rows if RULE_CONFLICT_PREFIX in str(getattr(item, "background", "") or "")]


__all__ = [
    "RULE_CONFLICT_PREFIX",
    "GrowthDecision",
    "GrowthReport",
    "GrowthUnderstander",
    "apply_growth",
    "case_is_blocked",
    "ensure_blocked_reason",
    "process_with_growth",
    "require_sendable",
    "rule_conflict_questions",
    "rule_conflict_reasons",
]
