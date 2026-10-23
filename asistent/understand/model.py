"""MVP-3 理解结果的数据结构（SPEC §7.4、§5.4、§6.2、§21.1）。

本模块只定义**中间结果**：抽取器产出、写入器消费。它不碰存储、不碰模型。
三条硬约束在这里用数据结构本身表达，而不是靠调用方自觉：

1. **引用守卫**（SPEC §6.2、MVP-3 硬约束）：`:class:`TimeField`、`:class:`TodoItem`、
   `:class:`MaterialRequirement`、`:class:`DraftCandidate` 都通过
   `:meth:`CitationRef.require` 生成，**没有可定位引用就构造不出来**；
   给不出引用的内容只能落成 `Inference`（推断/建议）或 `Question`（追问）。
2. **时间语义不丢**（SPEC §7.4、§16 MVP-3 第 1 条）：时间值一律是
   `TemporalField`（原文标签 + 语义槽位 + 原文文本），**不存在"裸时间戳"这种表示**。
3. **不确定不等于事实**（SPEC §5.4、§21.5）：`:class:`UnderstandingResult.facts` 里的每一项
   都必须带引用；抽取器若无法给出引用，只能放进 `inferences` / `questions`。

本模块**不做**任何判断型工作（不比对冲突、不转换状态、不调模型），
以便单独测试"抽取结果长什么样"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..contracts.citations import Citation, EvidenceSet
from ..contracts.errors import ValidationError
from ..contracts.timeutil import TemporalField

__all__ = [
    "CitationRef",
    "TimeField",
    "TodoItem",
    "MaterialRequirement",
    "ActionSuggestion",
    "DraftCandidate",
    "QuestionCandidate",
    "Conflict",
    "SourceText",
    "UnderstandingResult",
    "content_hash",
    "hash_text",
    "merge_time_fields",
]


def hash_text(text: str) -> str:
    """文本内容的 sha256（UTF-8 字节，跨平台稳定）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: 草稿正文哈希的别名：语义与 `hash_text` 相同，单独命名是为了让调用点读起来是"内容哈希"
content_hash = hash_text


def _clip(text: str, *, limit: int = 300) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


@dataclass(frozen=True)
class CitationRef:
    """一条已验证的引用（`Citation` 的只读包装）。

    `require()` 会跑契约的 `Citation.validate()`：缺 `path`/`locator`/`content_hash`/`supports`
    的引用**构造即失败**。因此"没有证据的 Fact"在下游是写不出来的，不是靠 code review 拦。
    """

    citation: Citation

    @classmethod
    def require(cls, citation: Citation) -> "CitationRef":
        citation.validate()
        return cls(citation=citation)

    @classmethod
    def lines(
        cls,
        *,
        source_object_id: str,
        source_object_type: str,
        path: str,
        locator: str,
        content_hash: str,
        supports: str,
        source_time: Optional[str] = None,
        source_version: Optional[int] = None,
        excerpt: str = "",
        used_by_execution_id: Optional[str] = None,
    ) -> "CitationRef":
        return cls.require(
            Citation(
                source_object_id=source_object_id,
                source_object_type=source_object_type,
                path=path,
                locator=locator,
                source_time=source_time,
                source_version=source_version,
                content_hash=content_hash,
                supports=supports,
                excerpt=excerpt,
                used_by_execution_id=used_by_execution_id,
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        return self.citation.to_dict()

    def describe(self) -> str:
        return self.citation.describe()


@dataclass
class TimeField:
    """一个带语义的时间字段：契约 `TemporalField` + 证据引用。

    `original_label`（原文里怎么写）与 `semantic`（它是哪一类时间）**分开保存**，
    这是"不得把报名截止当活动开始"的实现方式：改写语义必须显式发生，不能悄悄发生。
    """

    temporal: TemporalField
    #: 支持该字段的引用（至少一条）
    citations: List[CitationRef] = field(default_factory=list)
    #: 只用于人类可读展示，例如「报名截止时间」
    label_text: str = ""

    def validate(self) -> None:
        self.temporal.validate()
        if not self.citations:
            raise ValidationError("时间字段必须带引用（SPEC §6.2）: %r" % (self.temporal.original_label,))

    @property
    def semantic(self) -> str:
        return self.temporal.semantic

    @property
    def original_label(self) -> str:
        return self.temporal.original_label

    @property
    def original_text(self) -> str:
        return self.temporal.original_text

    @property
    def normalized_utc(self) -> Optional[str]:
        return self.temporal.normalized_utc

    def is_ambiguous(self) -> bool:
        """语义未判定，或解析置信度 < 1：调用方必须追问而不是猜。"""
        return self.semantic == "unknown" or self.temporal.confidence < 1.0

    def to_dict(self) -> Dict[str, Any]:
        """**扁平**表示：语义与原文标签分开两个键，便于直接断言"没被改写"。"""
        self.validate()
        return {
            "semantic": self.temporal.semantic,
            "original_label": self.temporal.original_label,
            "original_text": self.temporal.original_text,
            "normalized_utc": self.temporal.normalized_utc,
            "confidence": self.temporal.confidence,
            "source_time_zone": self.temporal.source_time_zone,
            "label_text": self.label_text,
            "citations": [item.to_dict() for item in self.citations],
            "temporal": self.temporal.to_dict(),
        }


@dataclass
class TodoItem:
    """待办项（SPEC §7.4「待办项」）：必有引用，否则只能作为推断/追问。"""

    title: str
    citations: List[CitationRef]
    due: Optional[TimeField] = None
    #: 该待办是谁的活：agent / user
    assignee: str = "user"
    detail: str = ""
    #: 依据的语义槽位（有 due 时才有值），供冲突检测使用
    due_semantic: Optional[str] = None

    def validate(self) -> None:
        if not self.title:
            raise ValidationError("待办项必须有问题描述")
        if not self.citations:
            raise ValidationError("待办项必须带引用（SPEC §6.2）: %r" % (self.title,))
        if self.due is not None:
            self.due.validate()

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "title": self.title,
            "citations": [item.to_dict() for item in self.citations],
            "due": None if self.due is None else self.due.to_dict(),
            "assignee": self.assignee,
            "detail": self.detail,
            "due_semantic": self.due_semantic,
        }


@dataclass
class MaterialRequirement:
    """待补材料（SPEC §7.4「待补材料清单」）：原文提到但**未被证明已提供**。"""

    name: str
    citations: List[CitationRef]
    #: "missing"（原文明确说缺） / "to_submit"（原文要求提交）
    status: str = "to_submit"
    semantic_hint: str = ""
    note: str = ""

    def validate(self) -> None:
        if not self.name:
            raise ValidationError("材料项必须有名称")
        if not self.citations:
            raise ValidationError("材料项必须带引用（SPEC §6.2）: %r" % (self.name,))
        if self.status not in ("missing", "to_submit"):
            raise ValidationError("材料项状态只能是 missing / to_submit: %r" % (self.status,))

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "name": self.name,
            "citations": [item.to_dict() for item in self.citations],
            "status": self.status,
            "semantic_hint": self.semantic_hint,
            "note": self.note,
        }


@dataclass
class ActionSuggestion:
    """行动建议（SPEC §7.4）：**永远是推断**，不冒充事实（§5.4、§21.5）。"""

    statement: str
    kind: str = "reply"
    citations: List[CitationRef] = field(default_factory=list)
    confidence: float = 0.5
    alternatives: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "statement": self.statement,
            "kind": self.kind,
            "citations": [item.to_dict() for item in self.citations],
            "confidence": self.confidence,
            "alternatives": list(self.alternatives),
        }


@dataclass
class DraftCandidate:
    """回复草稿候选（SPEC §5.5、§8.2）。

    只是**候选**：写入器把它落成 `Draft` + `DraftVersion`（OPEN，未审批）。
    草稿正文里的关键结论必须能回到 `citations`，否则它只是一段没有依据的文本。
    """

    body: str
    target_ref: str = ""
    citations: List[CitationRef] = field(default_factory=list)
    reason: str = "agent 初稿"
    summary: str = ""

    def validate(self) -> None:
        if not self.body.strip():
            raise ValidationError("草稿候选正文不能为空")
        for item in self.citations:
            item.citation.validate()

    def content_hash(self) -> str:
        return hash_text(self.body)

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "body": self.body,
            "target_ref": self.target_ref,
            "citations": [item.to_dict() for item in self.citations],
            "reason": self.reason,
            "summary": self.summary,
            "content_hash": self.content_hash(),
        }


@dataclass
class QuestionCandidate:
    """追问候选（SPEC §8.1、§9）。写入器落成 `Question`（OPEN）。"""

    question: str
    #: 追问类别：ambiguity / conflict / missing_material / missing_time / missing_recipient
    kind: str = "ambiguity"
    background: str = ""
    affects: List[str] = field(default_factory=list)
    required_format: str = "text"
    citations: List[CitationRef] = field(default_factory=list)
    deadline: Optional[str] = None

    def validate(self) -> None:
        if not self.question:
            raise ValidationError("追问必须有问题描述")
        if not self.kind:
            raise ValidationError("追问必须有类别（用于去重与展示分组）")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "question": self.question,
            "kind": self.kind,
            "background": self.background,
            "affects": list(self.affects),
            "required_format": self.required_format,
            "citations": [item.to_dict() for item in self.citations],
            "deadline": self.deadline,
        }


@dataclass
class Conflict:
    """事实与资料冲突（SPEC §6.3、§16 MVP-3 第 4 条）。

    只在**同一语义槽位**上比较：`registration_deadline` 与 `event_start` 不同值不算冲突，
    那正是"必须区分语义"的意义。
    """

    semantic: str
    values: List[str] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    citations: List[CitationRef] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "semantic": self.semantic,
            "values": list(self.values),
            "sources": [dict(item) for item in self.sources],
            "citations": [item.to_dict() for item in self.citations],
            "detail": self.detail,
        }


@dataclass
class SourceText:
    """被分析的原文文本 + 定位信息（正文行号基准是**原始文件**，不是裸正文）。

    引用里的 `locator` 必须能在原始对象里打开，所以这里保存 `body_start_line`：
    正文第 1 行对应原始文件的第几行。`SourceDocument.origin_path` 指向原始邮件，
    用户打开它应当能落在同一行。
    """

    raw_text: str
    body_text: str
    body_start_line: int = 1
    subject: str = ""
    from_addr: str = ""
    to_addrs: List[str] = field(default_factory=list)
    date_header: str = ""
    #: 邮件声明的时区（`Asia/Shanghai` 或 `+08:00` 形式），用于解释无时区的时间表达
    source_time_zone: str = "Asia/Shanghai"
    #: 邮件原文时间（UTC ISO）；相对日期（"明天"）以它为锚（SPEC §21.1）
    anchor_utc: str = ""
    anchor_original: str = ""

    def line_of(self, body_line: int) -> int:
        return max(1, self.body_start_line + max(1, body_line) - 1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "from_addr": self.from_addr,
            "to_addrs": list(self.to_addrs),
            "date_header": self.date_header,
            "source_time_zone": self.source_time_zone,
            "anchor_utc": self.anchor_utc,
            "body_start_line": self.body_start_line,
            "body_chars": len(self.body_text),
        }


@dataclass
class UnderstandingResult:
    """一次理解的完整产物（determinstic 抽取器的输出）。

    不变量（`:meth:`validate` 强制）：

    - `facts` 的每一项都能回到原文（带引用）；
    - `inferences` 默认 **不是** 事实（`candidate_only=True`），写入器只会把它们放进候选区；
    - `drafts` / `questions` 只是候选，最终由写入器落对象。
    """

    source_object_id: str = ""
    path: str = ""
    content_hash: str = ""
    source_time: Optional[str] = None
    source_version: Optional[int] = None
    subject: str = ""

    time_fields: List[TimeField] = field(default_factory=list)
    facts: List[EvidenceSet] = field(default_factory=list)
    inferences: List[ActionSuggestion] = field(default_factory=list)
    todos: List[TodoItem] = field(default_factory=list)
    materials: List[MaterialRequirement] = field(default_factory=list)
    drafts: List[DraftCandidate] = field(default_factory=list)
    questions: List[QuestionCandidate] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)

    #: 抽取器标识（审计用）；规则抽取器写 "rules:<version>"
    producer: str = "rules:v1"
    #: 被分析的原文文本
    source_text: Optional[SourceText] = None
    #: 非致命告警（例如"HTML 正文已降级为纯文本"），必须可观察
    warnings: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 不变式
    def validate(self) -> None:
        for fact in self.facts:
            for citation in fact.citations:
                citation.validate()
            if not fact.citations:
                raise ValidationError(
                    "理解结果里的 Fact 必须带引用，否则只能落成 Inference/Question（SPEC §5.4、§6.2）: %r"
                    % (fact.conclusion,)
                )
        for item in self.time_fields:
            item.validate()
        for todo in self.todos:
            todo.validate()
        for material in self.materials:
            material.validate()
        for draft in self.drafts:
            draft.validate()
        for question in self.questions:
            question.validate()

    # ------------------------------------------------------------------ 便利视图
    @property
    def fact_statements(self) -> List[str]:
        return [item.conclusion for item in self.facts]

    def time_fields_by_semantic(self, semantic: str) -> List[TimeField]:
        return [item for item in self.time_fields if item.semantic == semantic]

    def has_blocking_questions(self) -> bool:
        return any(item.kind in ("ambiguity", "conflict", "missing_time") for item in self.questions)

    def draftable(self) -> bool:
        """能否生成"可发送结论"：有草稿候选、且没有冲突/歧义/缺材料类追问。

        冲突与缺资料时**不产出可发送结论**（SPEC §6.3）。
        """
        if not self.drafts or self.conflicts:
            return False
        for question in self.questions:
            if question.kind in ("ambiguity", "conflict", "missing_material", "missing_time"):
                return False
        for draft in self.drafts:
            if not draft.citations:
                return False
        return True

    def blocking_reasons(self) -> List[str]:
        reasons: List[str] = []
        for conflict in self.conflicts:
            reasons.append("冲突：%s" % (conflict.detail or conflict.semantic))
        for question in self.questions:
            if question.kind in ("ambiguity", "conflict", "missing_material", "missing_time"):
                reasons.append("待回答（%s）：%s" % (question.kind, question.question))
        if not self.drafts and not reasons:
            reasons.append("本封邮件没有被识别为需要回复")
        return reasons

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "source_object_id": self.source_object_id,
            "path": self.path,
            "content_hash": self.content_hash,
            "source_time": self.source_time,
            "source_version": self.source_version,
            "subject": self.subject,
            "producer": self.producer,
            "time_fields": [item.to_dict() for item in self.time_fields],
            "facts": [item.to_dict() for item in self.facts],
            "inferences": [item.to_dict() for item in self.inferences],
            "todos": [item.to_dict() for item in self.todos],
            "materials": [item.to_dict() for item in self.materials],
            "drafts": [item.to_dict() for item in self.drafts],
            "questions": [item.to_dict() for item in self.questions],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "source_text": None if self.source_text is None else self.source_text.to_dict(),
            "warnings": list(self.warnings),
        }

    @staticmethod
    def clip(text: str, *, limit: int = 300) -> str:
        return _clip(text, limit=limit)


def merge_time_fields(fields: Sequence[TimeField]) -> List[TimeField]:
    """按（语义, 归一化时间）去重，保留首次出现顺序。

    抽取器与写入器都用它，保证"同一份原文跑两次得到同一个结果"。
    """
    seen = set()
    out: List[TimeField] = []
    for item in fields:
        key = (item.semantic, item.normalized_utc, item.temporal.original_label)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
