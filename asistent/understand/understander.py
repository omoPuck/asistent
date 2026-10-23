"""理解层入口：`Understander` 协议 + 规则实现 + 可选模型适配器（SPEC §12、§21.7、§1064）。

三层结构
========

```text
UnderstandingResult
   ▲
   │ analyze(AnalysisInput)
   ├── RuleBasedUnderstander    默认实现：确定性、离线、零模型调用、零成本
   └── ModelBackedUnderstander  可选适配器：**必须显式配置预算**，否则拒绝启用
                                  预算耗尽 → 记录状态并停下（不继续烧）
```

为什么默认必须是规则抽取器（SPEC §12 不锁定技术栈 + §1064）：

1. 没有模型也能产出事实/时间/待办/材料/草稿/追问，系统离线可用；
2. 模型输出不可复现，而审计与"同一输入同一输出"是本项目的可验收前提；
3. §1064 明确"未设定预算前禁止无人值守无限调用" —— 那么**默认路径就不能包含模型调用**，
   否则按默认配置启动即为违规。因此 `RuleBasedUnderstander` 里根本没有任何模型引用：
   连"忘记配置"导致的调用都不可能发生（测试用"一旦被调用就失败"的假后端证明这一点）。

跨文档冲突（SPEC §6.3、MVP-3 验收第 4 条）
========================================

`:meth:`RuleBasedUnderstander.cross_check`` 只比较**同一语义槽位**：
`registration_deadline` 的两个不同值才是冲突；`registration_deadline` 与 `event_start`
不同值**不是**冲突 —— 那正是"必须区分时间语义"的意义。冲突结果会写进
`UnderstandingResult.conflicts`，由写入层转成追问并阻止可发送结论。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from ..contracts.citations import Citation, EvidenceSet
from ..contracts.enums import FailureClass
from ..contracts.errors import AsistentError, ValidationError
from ..contracts.timeutil import utcnow_iso
from . import extract as extraction
from .model import (
    ActionSuggestion,
    CitationRef,
    Conflict,
    DraftCandidate,
    MaterialRequirement,
    QuestionCandidate,
    SourceText,
    TimeField,
    TodoItem,
    UnderstandingResult,
    hash_text,
    merge_time_fields,
)

__all__ = [
    "UnderstandError",
    "BudgetExceeded",
    "ModelDisabled",
    "AnalysisInput",
    "ModelCallResult",
    "ModelBackend",
    "ModelBudget",
    "ModelCallLog",
    "Understander",
    "RuleBasedUnderstander",
    "ModelBackedUnderstander",
    "cross_check",
    "budget_from_mapping",
]


class UnderstandError(AsistentError):
    """理解层错误：抽取/配置层面的失败（可分类、可审计）。"""

    code = "UNDERSTAND_ERROR"
    failure_class = FailureClass.MODEL_FAILURE


class BudgetExceeded(UnderstandError):
    """预算耗尽：必须停下并记录状态，**不继续烧**（SPEC §1064、§21.7）。"""

    code = "BUDGET_EXCEEDED"
    failure_class = FailureClass.MODEL_FAILURE


class ModelDisabled(UnderstandError):
    """模型后端未显式启用（或未配置预算）：任何路径都不得尝试联网调用。"""

    code = "MODEL_DISABLED"
    failure_class = FailureClass.MODEL_FAILURE


# --------------------------------------------------------------------------------------
# 输入与模型抽象
# --------------------------------------------------------------------------------------


@dataclass
class AnalysisInput:
    """一次理解的输入（纯数据，不含凭据）。"""

    document_id: str
    path: str
    content_hash: str
    source_text: SourceText
    source_version: Optional[int] = None
    source_time: Optional[str] = None
    #: 同一 Case 内已存在的带语义时间字段（跨文档冲突检测用，由写入层提供）
    known_time_fields: List[TimeField] = field(default_factory=list)
    #: 目标对象（例如回复的邮件线程），写进 `DraftCandidate.target_ref`
    target_ref: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "path": self.path,
            "content_hash": self.content_hash,
            "source_version": self.source_version,
            "source_time": self.source_time,
            "subject": self.source_text.subject,
            "body_chars": len(self.source_text.body_text),
            "known_time_fields": [item.to_dict() for item in self.known_time_fields],
            "target_ref": self.target_ref,
        }


@dataclass
class ModelCallResult:
    """一次模型调用的返回值（**只**接受结构化文本，不接受"就直接发信吧"式指令）。"""

    text: str
    model: str = ""
    tokens: int = 0
    cost: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ModelBackend(Protocol):
    """模型后端协议：**只**拿纯数据，不持有邮件/网盘凭据（SPEC §21.3）。

    实现可以是 OpenAI 兼容 HTTP、dsh/pi 后端或测试替身。调用方必须：
    显式启用 + 有预算 + 记调用日志。
    """

    name: str

    def complete(self, *, prompt: str, system: str = "") -> ModelCallResult: ...


@dataclass
class ModelBudget:
    """预算守门（SPEC §21.7、§1064）。

    - `configured=False`（默认）→ 模型路径**直接拒绝启用**，"未设定预算前禁止无人值守无限调用"；
    - `max_calls_per_day` / `max_cost_per_day`：任一超限即 `BudgetExceeded` 并停下；
    - 计数按 `day_key`（本地日期）滚动，跨日自动重置。
    """

    configured: bool = False
    max_calls_per_day: int = 0
    max_cost_per_day: float = 0.0

    def validate(self) -> None:
        if not self.configured:
            raise ValidationError("预算未配置：模型后端必须显式配置预算后才能启用（SPEC §1064）")
        if self.max_calls_per_day < 1:
            raise ValidationError("预算 max_calls_per_day 必须 >= 1")
        if self.max_cost_per_day <= 0:
            raise ValidationError("预算 max_cost_per_day 必须 > 0")

    @property
    def daily_call_limit(self) -> int:
        return int(self.max_calls_per_day)

    @property
    def daily_cost_limit(self) -> float:
        return float(self.max_cost_per_day)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "configured": self.configured,
            "max_calls_per_day": self.max_calls_per_day,
            "max_cost_per_day": self.max_cost_per_day,
        }


@dataclass
class ModelCallLog:
    """调用次数与花费（可观察；预算判定与验收证据都用它）。"""

    calls: int = 0
    cost: float = 0.0
    tokens: int = 0
    blocked_reason: str = ""
    last_call_at: str = ""
    day_key: str = ""
    #: 最近一次调用失败的分类（模型失败不得让整条流程崩掉）
    last_failure: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "cost": round(self.cost, 6),
            "tokens": self.tokens,
            "blocked_reason": self.blocked_reason,
            "last_call_at": self.last_call_at,
            "day_key": self.day_key,
            "last_failure": self.last_failure,
            "history_len": len(self.history),
        }


@runtime_checkable
class Understander(Protocol):
    """理解器协议：输入纯数据，输出 `UnderstandingResult`（结构化、带引用）。"""

    name: str

    def analyze(self, request: AnalysisInput) -> UnderstandingResult: ...

    def call_log(self) -> ModelCallLog: ...


# --------------------------------------------------------------------------------------
# 规则实现（默认）
# --------------------------------------------------------------------------------------


class RuleBasedUnderstander:
    """确定性规则/模板理解器：默认实现。

    **不持有任何模型对象**，因此"默认路径零模型调用"不是靠运行时判断，而是结构上不可能。
    """

    name = "rules"

    def __init__(self, *, author: str = "rules", versions: Optional[Dict[str, Any]] = None) -> None:
        self.author = author
        self.versions = dict(versions or {})
        self._log = ModelCallLog()
        self.analyze_count = 0

    # ------------------------------------------------------------------ 协议
    def analyze(self, request: AnalysisInput) -> UnderstandingResult:
        self.analyze_count += 1
        extraction_result = extraction.extract(
            request.source_text,
            source_object_id=request.document_id,
            path=request.path,
            content_hash=request.content_hash,
            source_version=request.source_version,
            source_time=request.source_time,
        )
        result = UnderstandingResult(
            source_object_id=request.document_id,
            path=request.path,
            content_hash=request.content_hash,
            source_time=request.source_time,
            source_version=request.source_version,
            subject=request.source_text.subject,
            time_fields=list(extraction_result.time_fields),
            todos=list(extraction_result.todos),
            materials=list(extraction_result.materials),
            questions=list(extraction_result.questions),
            producer=extraction.RULESET_VERSION,
            source_text=request.source_text,
            warnings=list(extraction_result.warnings),
        )
        result.facts = self._facts(request, result)
        result.inferences = self._inferences(request, result)
        result.conflicts = cross_check(
            local_fields=result.time_fields,
            known_fields=request.known_time_fields,
        )
        for conflict in result.conflicts:
            result.questions.append(
                QuestionCandidate(
                    question="关于「%s」：不同资料给出的时间不一致（%s）。以哪一个为准？"
                    % (conflict.semantic, " / ".join(conflict.values)),
                    kind="conflict",
                    background=conflict.detail,
                    affects=["时间字段:%s" % conflict.semantic],
                    citations=list(conflict.citations),
                )
            )
        result.drafts = self._drafts(request, result)
        result.validate()
        return result

    def call_log(self) -> ModelCallLog:
        return _clone_log(self._log)

    # ------------------------------------------------------------------ 事实
    def _facts(self, request: AnalysisInput, result: UnderstandingResult) -> List[EvidenceSet]:
        """把"有引用"的内容落成事实：时间字段、待办、待补材料。

        每一项都带**至少一条** `Citation`；没有引用的内容不会走到这里
        （它们只可能是 `inferences` 或 `questions`）。
        """
        facts: List[EvidenceSet] = []
        for field in result.time_fields:
            statement = "「%s」是 %s（原文：%s）" % (
                field.label_text or field.original_label,
                field.original_text,
                field.original_label,
            )
            facts.append(
                EvidenceSet(
                    conclusion=statement,
                    citations=[item.citation for item in field.citations],
                    contains_inference=False,
                    uncertainty_notes=(
                        ["解析置信度 %.2f：时刻未写明，需确认" % field.temporal.confidence]
                        if field.temporal.confidence < 1.0
                        else []
                    ),
                )
            )
        for todo in result.todos:
            facts.append(
                EvidenceSet(
                    conclusion="待办：%s" % todo.title,
                    citations=[item.citation for item in todo.citations],
                    contains_inference=False,
                    uncertainty_notes=[],
                )
            )
        for material in result.materials:
            facts.append(
                EvidenceSet(
                    conclusion="待补材料：%s（原文表述：%s）" % (material.name, material.semantic_hint),
                    citations=[item.citation for item in material.citations],
                    contains_inference=False,
                    uncertainty_notes=["未看到已提交证据，已生成追问"],
                )
            )
        for suggestion in result.inferences:
            for item in suggestion.citations:
                if not item.citation.content_hash:  # pragma: no cover - Citation 已校验
                    raise ValidationError("推断引用的 content_hash 不能为空")
        return facts

    # ------------------------------------------------------------------ 推断
    def _inferences(self, request: AnalysisInput, result: UnderstandingResult) -> List[ActionSuggestion]:
        """行动建议**永远是推断**（SPEC §5.4、§21.5），并且必须能回到原文引用。"""
        body = request.source_text.body_text
        citations = _clause_citations(request, needles=("回复", "确认", "报名", "截止"), limit=2)
        if not citations:
            citations = [_whole_document_citation(request, supports="支持「本封邮件是否需要回复」的判断")]
        suggestions: List[ActionSuggestion] = []
        requested = extraction.reply_requested(body)
        decline = any(marker in body for marker in extraction.DECLINE_MARKERS)
        if requested and not decline:
            suggestions.append(
                ActionSuggestion(
                    statement="建议回复：原文出现明确的回复/确认要求，且没有「不参加」类表述。",
                    kind="reply",
                    citations=[CitationRef.require(item) for item in citations],
                    confidence=0.7,
                    alternatives=["先追问缺失材料再回复", "由用户手工回复"],
                )
            )
        elif decline:
            suggestions.append(
                ActionSuggestion(
                    statement="原文含「无法参加」类表述：是否回复由用户裁决，未生成可发送结论。",
                    kind="needs_user_decision",
                    citations=[CitationRef.require(item) for item in citations],
                    confidence=0.5,
                    alternatives=["仍生成草稿", "不回复"],
                )
            )
        else:
            suggestions.append(
                ActionSuggestion(
                    statement="未发现明确的回复要求：本封邮件可能只需记录，无需回复。",
                    kind="no_reply",
                    citations=[CitationRef.require(item) for item in citations],
                    confidence=0.6,
                    alternatives=["用户明确要求时再生成草稿"],
                )
            )
        return suggestions

    # ------------------------------------------------------------------ 草稿
    def _drafts(self, request: AnalysisInput, result: UnderstandingResult) -> List[DraftCandidate]:
        """只有在"有回复依据、无冲突、无缺材料"时才产出草稿候选。

        冲突/缺资料时**不产出可发送结论**（SPEC §6.3、MVP-3 验收第 4 条），
        此时 `UnderstandingResult.draftable()` 为 False，写入层会把原因写进 Case。
        """
        body = request.source_text.body_text
        if any(marker in body for marker in extraction.DECLINE_MARKERS):
            return []
        if result.conflicts:
            return []
        if any(item.kind == "missing_material" for item in result.questions):
            return []
        if any(item.kind in ("ambiguity", "missing_time") for item in result.questions):
            return []
        if not extraction.reply_requested(body):
            return []
        citations = _clause_citations(request, needles=("回复", "确认", "报名", "截止"), limit=3)
        if not citations:
            return []
        lines = ["您好：", "", "已收到贵方关于「%s」的通知，现回复如下：" % (request.source_text.subject or "（无主题）")]
        lines.extend(_time_field_lines(result.time_fields))
        lines.append("- 我方已知悉上述时间安排，将按要求处理。")
        lines.append("")
        lines.append("如有变动请回复告知，谢谢。")
        draft_body = "\n".join(lines)
        return [
            DraftCandidate(
                body=draft_body,
                target_ref=request.target_ref,
                citations=[CitationRef.require(item) for item in citations],
                reason="依据原文的回复要求与时间字段生成（模板，未发送）",
                summary="回复%s：确认知悉时间安排" % (request.source_text.subject or "通知"),
            )
        ]

def _time_field_lines(time_fields: Sequence[Any]) -> List[str]:
    """把时间字段渲染成草稿里的行；同一句里的**区间**只占一行。

    `14:00 至 16:30` 在抽取层会落成 `event_start` + `event_end` 两个语义槽位（各自带引用），
    但用户读到的是一句原文；草稿里把它写成两行，看起来会像两个不同的安排。
    判据是**两个槽位的 `original_text` 相同**：同一次时间表达被两个语义解释。
    """
    slots: List[Tuple[str, str, str, str]] = []
    for field in time_fields:
        if field.normalized_utc is None:
            continue
        slots.append(
            (
                field.semantic,
                field.original_text,
                field.label_text or field.semantic,
                field.original_label,
            )
        )
    lines: List[str] = []
    merged: List[str] = []
    for index, (semantic, original, label, original_label) in enumerate(slots):
        if semantic in merged:
            continue
        partners = [
            item for item in slots if item[1] == original and item[0] not in (semantic,)
        ]
        if partners:
            merged.append(partners[0][0])
            lines.append(
                "- %s / %s：%s（原文：%s）"
                % (label, partners[0][2], original, original_label)
            )
            continue
        lines.append("- %s：%s（原文：%s）" % (label, original, original_label))
    return lines


def _clause_citations(request: AnalysisInput, *, needles: Sequence[str], limit: int) -> List[Citation]:
    """为指定关键词取原文引用（找不到就返回空列表，绝不返回"无定位"的引用）。"""
    out: List[Citation] = []
    for needle in needles:
        position = request.source_text.body_text.find(needle)
        if position < 0:
            continue
        start_line = request.source_text.body_text.count("\n", 0, position) + 1
        end_line = request.source_text.body_text.count("\n", 0, position + len(needle)) + 1
        absolute_start = request.source_text.line_of(start_line)
        absolute_end = request.source_text.line_of(end_line)
        excerpt = " ".join(
            request.source_text.body_text[max(0, position - 20) : position + len(needle) + 20].split()
        )
        try:
            citation = Citation(
                source_object_id=request.document_id,
                source_object_type="SourceDocument",
                path=request.path,
                locator="line:%d-%d" % (absolute_start, absolute_end),
                source_time=request.source_time,
                source_version=request.source_version,
                content_hash=request.content_hash,
                supports="支持对「%s」的处理判断" % needle,
                excerpt=excerpt,
            )
            citation.validate()
        except Exception:  # pragma: no cover - 字段已填全，防御性保留
            continue
        out.append(citation)
        if len(out) >= limit:
            break
    return out


def _whole_document_citation(request: AnalysisInput, *, supports: str) -> Citation:
    lines = max(1, request.source_text.raw_text.count("\n") + 1)
    citation = Citation(
        source_object_id=request.document_id,
        source_object_type="SourceDocument",
        path=request.path,
        locator="line:1-%d" % lines,
        source_time=request.source_time,
        source_version=request.source_version,
        content_hash=request.content_hash,
        supports=supports,
        excerpt=" ".join(request.source_text.raw_text.split())[:160],
    )
    citation.validate()
    return citation


# --------------------------------------------------------------------------------------
# 跨文档冲突检测
# --------------------------------------------------------------------------------------


def cross_check(
    *,
    local_fields: Sequence[TimeField],
    known_fields: Sequence[TimeField],
) -> List[Conflict]:
    """同一语义槽位上出现两个不同时间值 → 冲突（SPEC §6.3）。

    - 只比较**有归一化时间**的字段（`None` 是"时间未定"，那属于"缺失/歧义"，不是冲突）；
    - 同一文档内同槽位多值也算冲突（"报名截止时间前后不一致"）；
    - 语义不同（`registration_deadline` vs `event_start`）**不算**冲突。
    """
    buckets: Dict[str, List[TimeField]] = {}
    for field in list(local_fields) + list(known_fields):
        if not field.normalized_utc:
            continue
        buckets.setdefault(field.semantic, []).append(field)

    conflicts: List[Conflict] = []
    for semantic in sorted(buckets):
        fields = buckets[semantic]
        values: List[str] = []
        sources: List[Dict[str, Any]] = []
        citations: List[CitationRef] = []
        for field in fields:
            value = field.normalized_utc or ""
            if value not in values:
                values.append(value)
            sources.append(
                {
                    "source_object_id": field.citations[0].citation.source_object_id if field.citations else "",
                    "path": field.citations[0].citation.path if field.citations else "",
                    "label": field.original_label,
                    "text": field.original_text,
                    "normalized_utc": value,
                }
            )
            for item in field.citations:
                if len(citations) < 4:
                    citations.append(item)
        if len(values) <= 1:
            continue
        conflicts.append(
            Conflict(
                semantic=semantic,
                values=values,
                sources=sources,
                citations=citations,
                detail="同一语义槽位 %s 出现 %d 个不同时间值：%s" % (semantic, len(values), " / ".join(values)),
            )
        )
    return conflicts


# --------------------------------------------------------------------------------------
# 可选模型适配器
# --------------------------------------------------------------------------------------


def budget_from_mapping(payload: Optional[Dict[str, Any]]) -> ModelBudget:
    """从配置字典构造预算。`None`/空 → 未配置（模型路径将被拒绝启用）。"""
    if not payload:
        return ModelBudget(configured=False)
    return ModelBudget(
        configured=bool(payload.get("configured", True)),
        max_calls_per_day=int(payload.get("max_calls_per_day", 0) or 0),
        max_cost_per_day=float(payload.get("max_cost_per_day", 0.0) or 0.0),
    )


def _coerce_budget(budget: Optional[Any]) -> ModelBudget:
    """把 `None` / `dict` / `ModelBudget` 统一成 `ModelBudget`。

    `dict` 必须走 `budget_from_mapping`：否则 `configured=False` 的字典会被当成
    一个"存在的对象"而绕过预算校验（实测踩过：`budget={"max_calls_per_day": 0}`
    抛的是 `AttributeError: 'dict' object has no attribute 'validate'`）。
    """
    if isinstance(budget, ModelBudget):
        return budget
    if isinstance(budget, Mapping) or budget is None:
        return budget_from_mapping(dict(budget) if budget else None)
    raise ValidationError("budget 必须是 ModelBudget / dict / None，收到 %r" % (type(budget).__name__,))


class ModelBackedUnderstander:
    """可选模型适配器：**必须显式配置后端与预算**，否则构造即失败。

    守门顺序（任何一步不满足都**不会**发生网络调用）：

    1. `backend is None` → `ModelDisabled`（"未配置"就是"不用"，不是"试着用一下"）；
    2. 预算未配置 / 非法 → `ValidationError`（SPEC §1064：未设预算禁止无人值守无限调用）；
    3. 当日调用数或花费已达上限 → `BudgetExceeded`，写 `blocked_reason`，**停下**；
    4. 模型调用本身失败 → 记 `last_failure`，按 `fallback` 决定是否退回规则抽取器；
       退回时结果会带 `warnings`，**不会**把模型失败伪装成成功。

    最终产物仍然必须通过 `UnderstandingResult.validate()`：模型给的"没有引用的结论"
    一样过不了引用守卫（模型不能绕过 §6.2）。
    """

    name = "model"

    def __init__(
        self,
        *,
        backend: Optional[ModelBackend] = None,
        budget: Optional[Any] = None,
        fallback: Optional[Understander] = None,
        clock: Any = None,
        author: str = "model",
        model_name: str = "",
    ) -> None:
        self.backend = backend
        self.budget = _coerce_budget(budget)
        self.fallback = fallback
        self.author = author
        self.model_name = model_name
        self._clock = clock or utcnow_iso
        self._log = ModelCallLog()
        # 构造即守门：未配置后端/预算的"模型理解器"根本不该被创建出来
        # （SPEC §1064「未设预算前禁止无人值守无限调用」）。
        self._ensure_enabled()

    # ------------------------------------------------------------------ 协议
    def _ensure_enabled(self) -> None:
        if self.backend is None:
            raise ModelDisabled("模型后端未配置：未显式启用时任何路径都不得尝试联网调用（SPEC §1064）")
        self.budget.validate()

    def _today(self) -> str:
        return str(self._clock())[:10]

    def _roll_day(self) -> None:
        today = self._today()
        if self._log.day_key != today:
            self._log.day_key = today
            self._log.calls = 0
            self._log.cost = 0.0
            self._log.tokens = 0
            self._log.blocked_reason = ""

    def _guard_budget(self) -> None:
        self._roll_day()
        if self._log.calls >= self.budget.daily_call_limit:
            self._log.blocked_reason = "当日调用数已达上限 %d" % self.budget.daily_call_limit
            raise BudgetExceeded(
                "模型调用预算已耗尽（调用数 %d/%d），任务停下等待用户调整预算（SPEC §1064）"
                % (self._log.calls, self.budget.daily_call_limit),
                detail={"budget": self.budget.to_dict(), "log": self._log.to_dict()},
            )
        if self._log.cost >= self.budget.daily_cost_limit:
            self._log.blocked_reason = "当日花费已达上限 %.4f" % self.budget.daily_cost_limit
            raise BudgetExceeded(
                "模型调用预算已耗尽（花费 %.4f/%.4f），任务停下等待用户调整预算（SPEC §1064）"
                % (self._log.cost, self.budget.daily_cost_limit),
                detail={"budget": self.budget.to_dict(), "log": self._log.to_dict()},
            )

    def analyze(self, request: AnalysisInput) -> UnderstandingResult:
        self._ensure_enabled()
        self._guard_budget()
        prompt = self._build_prompt(request)
        try:
            call = self.backend.complete(prompt=prompt, system="")
        except Exception as exc:
            self._log.last_failure = "%s: %s" % (type(exc).__name__, exc)
            if self.fallback is None:
                raise UnderstandError(
                    "模型调用失败且未配置回退实现：%s: %s" % (type(exc).__name__, exc)
                ) from exc
            fallback_result = self.fallback.analyze(request)
            fallback_result.warnings.append("模型调用失败，已回退规则抽取器：%s" % self._log.last_failure)
            fallback_result.producer = "%s->fallback:%s" % (self.name, fallback_result.producer)
            return fallback_result
        self._log.calls += 1
        self._log.cost += float(call.cost or 0.0)
        self._log.tokens += int(call.tokens or 0)
        self._log.last_call_at = self._clock()
        self._log.history.append(
            {"at": self._log.last_call_at, "model": call.model or self.model_name, "tokens": int(call.tokens or 0)}
        )
        payload = _parse_model_payload(call.text)
        return self._to_result(request, payload, call)

    def call_log(self) -> ModelCallLog:
        return _clone_log(self._log)

    # ------------------------------------------------------------------ 内部
    def _build_prompt(self, request: AnalysisInput) -> str:
        payload = {
            "task": "把一份中文通知/资料整理成结构化理解结果",
            "output_format": "只输出一个 JSON 对象，不要 Markdown、不要解释文字",
            "output_schema": {
                "facts": [{"statement": "带引用的结论", "citation": "见 constraints 的引用字段"}],
                "questions": [{"question": "需要用户回答的问题", "kind": "ambiguity|missing_material|missing_time|conflict"}],
                "drafts": [{"body": "草稿正文（只做候选，不得发送）"}],
            },
            "constraints": [
                "每个结论都必须带原文引用（citation 里含 path/locator/content_hash/supports），给不出引用的只能放进 questions",
                "时间必须带语义槽位（registration_deadline / event_start / event_end / submission_deadline / reply_deadline / meeting_time / payment_deadline / other / unknown）",
                "不要输出任何发送动作，不要自造对象 ID",
            ],
            "document": {
                "id": request.document_id,
                "path": request.path,
                "content_hash": request.content_hash,
                "subject": request.source_text.subject,
                "body": request.source_text.body_text,
            },
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _to_result(self, request: AnalysisInput, payload: Dict[str, Any], call: ModelCallResult) -> UnderstandingResult:
        result = UnderstandingResult(
            source_object_id=request.document_id,
            path=request.path,
            content_hash=request.content_hash,
            source_time=request.source_time,
            source_version=request.source_version,
            subject=request.source_text.subject,
            producer="%s:%s" % (self.name, call.model or self.model_name or "unknown"),
            source_text=request.source_text,
        )
        for item in payload.get("facts", []) or []:
            statement = extraction.safe_text(item.get("statement"), limit=300)
            citation_payload = item.get("citation") or {}
            try:
                citation = Citation.from_dict(citation_payload)
            except Exception as exc:
                result.warnings.append(
                    "模型给出的结论缺引用，已按引用守卫降级为追问：%s（%s）" % (statement, exc)
                )
                result.questions.append(
                    QuestionCandidate(
                        question="（模型结论缺引用，需人工确认）%s" % statement,
                        kind="ambiguity",
                        background="模型输出未带可定位引用，按引用守卫不得成为事实（SPEC §6.2）",
                    )
                )
                continue
            result.facts.append(
                EvidenceSet(conclusion=statement, citations=[citation], contains_inference=False)
            )
        for item in payload.get("questions", []) or []:
            result.questions.append(
                QuestionCandidate(
                    question=extraction.safe_text(item.get("question"), limit=300),
                    kind=extraction.safe_text(item.get("kind") or "ambiguity", limit=32),
                    background=extraction.safe_text(item.get("background"), limit=500),
                )
            )
        for item in payload.get("drafts", []) or []:
            body = extraction.safe_text(item.get("body"), limit=4000)
            if not body.strip():
                continue
            result.drafts.append(
                DraftCandidate(
                    body=body,
                    target_ref=request.target_ref,
                    reason="模型草稿（未发送，需用户审批）",
                    summary=extraction.safe_text(item.get("summary"), limit=200),
                )
            )
        result.time_fields = merge_time_fields(result.time_fields)
        result.validate()
        return result


def _parse_model_payload(text: str) -> Dict[str, Any]:
    """解析模型返回的 JSON；解析不出来就返回空结构（**不**把自由文本当结论）。"""
    raw = extraction.safe_text(text)
    try:
        payload = json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return {"facts": [], "questions": []}
        try:
            payload = json.loads(raw[start : end + 1])
        except Exception:
            return {"facts": [], "questions": []}
    if not isinstance(payload, dict):  # pragma: no cover - 契约外输入
        return {"facts": [], "questions": []}
    return payload


def _clone_log(log: ModelCallLog) -> ModelCallLog:
    """返回调用日志的副本：调用方**不能**通过改返回值篡改内部计数。"""
    return ModelCallLog(
        calls=log.calls,
        cost=log.cost,
        tokens=log.tokens,
        blocked_reason=log.blocked_reason,
        last_call_at=log.last_call_at,
        day_key=log.day_key,
        last_failure=log.last_failure,
        history=[dict(item) for item in log.history],
    )
