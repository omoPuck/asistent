"""把理解结果落到共享对象上（SPEC §5、§7.3、§8.1、§8.2、§6.3）。

流程（`process_document`）
=========================

```text
SourceDocument（原始邮件，**绝不被覆盖**）
  └─ eml/source_text 解析
       └─ Understander.analyze(AnalysisInput)   ← 默认规则抽取器，零模型调用
            ├─ TimeField  → Fact(temporal_fields=...)     每个都带 Citation
            ├─ TodoItem   → Task(kind=...)                 挂在同一个 Case 下
            ├─ 建议       → Inference(candidate_only=True) 必须带引用，否则不生成
            ├─ 材料       → Question(kind=missing_material)
            ├─ 冲突       → Question(kind=conflict) + Case.blocked_reason，**不产出可发送结论**
            └─ DraftCandidate → Draft + DraftVersion(OPEN，未审批)
```

幂等（契约 §12.5 口径）
======================

- **对象计数不变**：所有对象 ID 都由 `(document_id, 槽位)` 确定性派生 ——
  重复处理同一封邮件命中的是同一个对象，只做 `UPDATE`（内容确实变化时）或原样复用；
- **事件不重复处理**：`publish_event` 由 `dedupe_key` 去重（事件走事件接口，不混进 `get`）；
- **允许新增审计行**：每次处理都留一条审计，这是"重复投递客观发生"的事实，不算幂等失败。

原始对象不被覆盖（SPEC §5.1/§284）
=================================

本模块**只读** `SourceDocument`（不 `put` 它），所有整理结果都是新对象并引用它。
`tests/understand/test_no_overwrite.py` 用"处理前后 `SourceDocument` 的 `revision`/`content_hash`
/`to_dict()` 完全一致"证明这一点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..contracts.citations import EvidenceSet
from ..contracts.enums import ActorKind, AuditAction, CaseState, DraftState, QuestionState, TaskState
from ..contracts.errors import AsistentError, NotFound, ValidationError
from ..contracts.ids import digest_of, event_dedupe_key, new_id
from ..contracts.objects import (
    Case,
    Event,
    Fact,
    Inference,
    Question,
    SourceDocument,
    Task,
)
from ..contracts.store_api import StoreProtocol
from ..contracts.timeutil import utcnow_iso
from ..contracts.transitions import machine_for
from . import drafts as draft_api
from . import extract as extraction
from .model import SourceText, TimeField, UnderstandingResult
from .understander import AnalysisInput, RuleBasedUnderstander, Understander

__all__ = ["EntryOutcome", "CaseFlowReport", "process_document", "load_source_text", "CASE_GOAL_PREFIX"]

CASE_GOAL_PREFIX = "理解并处理邮件："

#: 对象 ID 派生命名空间
_ID_NAMESPACE = "mvp3-understand"


def _derive(object_type: str, *parts: Any) -> str:
    return new_id(object_type, token=digest_of([_ID_NAMESPACE, object_type, *parts])[:24])


@dataclass
class EntryOutcome:
    """单个对象的落库结果（可观察：id + created/reused + 状态）。"""

    kind: str
    object_id: str
    outcome: str = "created"   # created / reused / updated
    state: str = ""
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "object_id": self.object_id,
            "outcome": self.outcome,
            "state": self.state,
            "detail": self.detail,
        }


@dataclass
class CaseFlowReport:
    """一次处理的完整记录（验收证据结构，`to_dict()` 即可打印）。"""

    document_id: str = ""
    case_id: str = ""
    case_state: str = ""
    content_hash: str = ""
    subject: str = ""
    producer: str = ""
    duplicate_run: bool = False
    draftable: bool = False
    blocking_reasons: List[str] = field(default_factory=list)
    outcomes: List[EntryOutcome] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    time_fields: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    event_id: str = ""
    event_created: bool = False
    case_waited_reason: Optional[str] = None
    started_at: str = ""
    finished_at: str = ""

    def add(self, kind: str, object_id: str, outcome: str, *, state: str = "", detail: str = "") -> None:
        self.outcomes.append(EntryOutcome(kind=kind, object_id=object_id, outcome=outcome, state=state, detail=detail))
        self.counts[kind] = self.counts.get(kind, 0) + (0 if outcome != "created" else 1)

    def summary_line(self) -> str:
        pieces = ["case=%s state=%s doc=%s" % (self.case_id, self.case_state, self.document_id)]
        for kind in sorted(self.counts):
            pieces.append("%s+%d" % (kind, self.counts[kind]))
        pieces.append("draftable=%s" % self.draftable)
        pieces.append("duplicate=%s" % self.duplicate_run)
        return " ".join(pieces)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "case_id": self.case_id,
            "case_state": self.case_state,
            "content_hash": self.content_hash,
            "subject": self.subject,
            "producer": self.producer,
            "duplicate_run": self.duplicate_run,
            "draftable": self.draftable,
            "blocking_reasons": list(self.blocking_reasons),
            "outcomes": [item.to_dict() for item in self.outcomes],
            "counts": dict(self.counts),
            "time_fields": list(self.time_fields),
            "conflicts": list(self.conflicts),
            "warnings": list(self.warnings),
            "event_id": self.event_id,
            "event_created": self.event_created,
            "case_waited_reason": self.case_waited_reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# --------------------------------------------------------------------------------------
# 原文解析
# --------------------------------------------------------------------------------------


def load_source_text(
    document: SourceDocument,
    *,
    eml_bytes: Optional[bytes] = None,
    raw_text: Optional[str] = None,
    clock: Any = None,
) -> SourceText:
    """解析被分析的原文。

    优先用调用方给的 `eml_bytes` / `raw_text`（测试与真实同步都会传），
    否则按 `document.origin_path` 读取原始文件 —— 读取失败时抛 `ValidationError`，
    **不猜正文**（没有原文就不该产出事实）。
    """
    anchor = document.received_at or (clock() if callable(clock) else utcnow_iso())
    if eml_bytes is not None:
        return extraction.decode_eml_bytes(eml_bytes, anchor_utc=anchor)
    if raw_text is not None:
        return extraction.parse_source_text(raw_text, anchor_utc=anchor)
    path = document.origin_path
    if not path:
        raise ValidationError("缺少原始内容：既没有 eml_bytes/raw_text，SourceDocument.origin_path 也是空的")
    try:
        with open(path, "rb") as handle:
            payload = handle.read()
    except OSError as exc:
        raise ValidationError("无法读取原始内容: %s（%s）" % (path, exc)) from exc
    if document.source_type == "email" or payload[:1] == b"" or b"\n" in payload[:200]:
        try:
            return extraction.decode_eml_bytes(payload, anchor_utc=anchor)
        except Exception:  # pragma: no cover - 非邮件文本退化
            pass
    return extraction.parse_source_text(payload.decode("utf-8", "replace"), anchor_utc=anchor)


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------


def process_document(
    store: StoreProtocol,
    document: SourceDocument,
    *,
    understander: Optional[Understander] = None,
    eml_bytes: Optional[bytes] = None,
    raw_text: Optional[str] = None,
    create_draft: bool = True,
    clock: Any = None,
    execution_id: Optional[str] = None,
    actor_kind: str = ActorKind.AGENT,
    actor_id: str = "understand",
    known_documents: Optional[Sequence[SourceDocument]] = None,
) -> CaseFlowReport:
    """理解一封邮件/一份资料，并把结果落成共享对象。**可重复调用**（幂等）。

    `known_documents` 用于跨文档冲突检测（例如同一 Case 下另一封资料给了不同的截止时间）。
    """
    if not document.id:
        raise ValidationError("SourceDocument 必须有稳定 ID（先同步入库再理解）")
    resolved_understander: Understander = understander or RuleBasedUnderstander()
    now = clock or utcnow_iso

    source_text = load_source_text(document, eml_bytes=eml_bytes, raw_text=raw_text, clock=now)
    case_id = document.case_id or _derive("Case", document.id)
    case = _ensure_case(store, case_id=case_id, document=document, actor_kind=actor_kind, actor_id=actor_id)
    _backfill_document_case(store, document, case.id, actor_kind=actor_kind, actor_id=actor_id)

    known_fields = _known_time_fields(store, case_id, exclude_document_id=document.id)
    for extra in known_documents or []:
        known_fields.extend(_time_fields_of_document(store, extra.id))

    request = AnalysisInput(
        document_id=document.id,
        path=document.origin_path,
        content_hash=document.content_hash,
        source_text=source_text,
        source_version=document.content_version,
        source_time=document.received_at,
        known_time_fields=known_fields,
        target_ref=source_text.from_addr,
    )

    report = CaseFlowReport(
        document_id=document.id,
        case_id=case.id,
        content_hash=document.content_hash,
        subject=source_text.subject,
        started_at=now(),
    )
    result = resolved_understander.analyze(request)
    report.producer = result.producer
    report.warnings.extend(result.warnings)
    report.time_fields = [item.to_dict() for item in result.time_fields]
    report.conflicts = [item.to_dict() for item in result.conflicts]
    report.draftable = result.draftable()
    report.blocking_reasons = result.blocking_reasons()

    # ---- 事实（时间字段）：每个语义槽位一条，全部带引用
    for field in result.time_fields:
        statement = "%s：%s（原文标签：%s）" % (
            field.label_text or field.original_label,
            field.original_text,
            field.original_label,
        )
        fact_id = _derive("Fact", document.id, "time", _time_slot_key(field))
        outcome = _upsert_fact(
            store,
            fact_id=fact_id,
            case_id=case.id,
            subject="时间字段:%s" % field.semantic,
            statement=statement,
            citations=[item.citation for item in field.citations],
            temporal_fields=[field.temporal.to_dict()],
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        report.add("Fact", fact_id, outcome, detail=field.semantic)
        _link_model(store, case.id, "fact_ids", fact_id, actor_kind=actor_kind, actor_id=actor_id)

    # ---- 事实（待办 / 待补材料）
    for index, todo in enumerate(result.todos):
        fact_id = _derive("Fact", document.id, "todo", index)
        outcome = _upsert_fact(
            store,
            fact_id=fact_id,
            case_id=case.id,
            subject="待办:%d" % index,
            statement="待办：%s" % todo.title,
            citations=[item.citation for item in todo.citations],
            temporal_fields=[todo.due.temporal.to_dict()] if todo.due is not None else [],
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        report.add("Fact", fact_id, outcome, detail="todo")
        _link_model(store, case.id, "fact_ids", fact_id, actor_kind=actor_kind, actor_id=actor_id)

    for index, material in enumerate(result.materials):
        fact_id = _derive("Fact", document.id, "material", index)
        outcome = _upsert_fact(
            store,
            fact_id=fact_id,
            case_id=case.id,
            subject="待补材料:%d" % index,
            statement="待补材料：%s（原文表述：%s）" % (material.name, material.semantic_hint),
            citations=[item.citation for item in material.citations],
            temporal_fields=[],
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        report.add("Fact", fact_id, outcome, detail="material")
        _link_model(store, case.id, "fact_ids", fact_id, actor_kind=actor_kind, actor_id=actor_id)

    # ---- 推断（行动建议）：永远是候选区，且必须带引用
    for index, suggestion in enumerate(result.inferences):
        if not suggestion.citations:
            report.warnings.append("行动建议没有引用，已丢弃：%s" % suggestion.statement)
            continue
        inference_id = _derive("Inference", document.id, "suggestion", index)
        outcome = _upsert_inference(
            store,
            inference_id=inference_id,
            case_id=case.id,
            statement=suggestion.statement,
            citations=[item.citation for item in suggestion.citations],
            confidence=suggestion.confidence,
            alternatives=suggestion.alternatives,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        report.add("Inference", inference_id, outcome, detail=suggestion.kind)
        _link_model(store, case.id, "inference_ids", inference_id, actor_kind=actor_kind, actor_id=actor_id)

    # ---- 追问
    for index, candidate in enumerate(result.questions):
        question_id = _derive("Question", document.id, candidate.kind, index)
        existed = _get_soft(store, "Question", question_id)
        unchanged = existed is not None and existed.question == candidate.question
        question = _upsert_question(
            store,
            question_id=question_id,
            case_id=case.id,
            question=candidate.question,
            background=candidate.background,
            kind=candidate.kind,
            required_format=candidate.required_format,
            affects=candidate.affects,
            citations=[item.citation for item in candidate.citations],
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        report.add(
            "Question",
            question.id,
            "reused" if unchanged else ("updated" if existed is not None else "created"),
            state=str(question.state),
            detail=candidate.kind,
        )
        _link_model(store, case.id, "question_ids", question.id, actor_kind=actor_kind, actor_id=actor_id)

    # ---- 待办任务
    for index, todo in enumerate(result.todos):
        task_id = _derive("Task", document.id, "todo_task", index)
        task, outcome = _upsert_task(
            store,
            task_id=task_id,
            case_id=case.id,
            title=todo.title,
            detail=todo.detail,
            document=document,
            todo_index=index,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        report.add("Task", task.id, outcome, state=str(task.state), detail=todo.due_semantic or "")
        _link_model(store, case.id, "task_ids", task.id, actor_kind=actor_kind, actor_id=actor_id)

    # ---- 冲突处理：把已经存在的"待发送草稿"标成 STALE（不删除、不隐藏）
    if result.conflicts:
        for draft in _stale_drafts(
            store,
            case_id=case.id,
            reason="；".join(item.detail for item in result.conflicts),
            actor_kind=actor_kind,
            actor_id=actor_id,
        ):
            report.add("Draft", draft.id, "updated", state=str(draft.state), detail="冲突→STALE")

    # ---- 草稿 + 版本
    if create_draft and result.drafts and result.draftable():
        for candidate in result.drafts:
            draft, version, created = draft_api.create_draft(
                store,
                case_id=case.id,
                candidate=candidate,
                target_kind="email_reply",
                target_ref=candidate.target_ref or source_text.from_addr,
                actor_kind=actor_kind,
                actor_id=actor_id,
                reason="依据原文回复要求生成草稿",
            )
            # 用 store 里的最新实例报告状态（`put` 的返回值不保证是同一实例）
            event = "created" if created else "reused"
            try:
                current = store.get("Draft", draft.id)
            except Exception:  # pragma: no cover - 取不到就退回返回值
                current = draft
            report.add("Draft", draft.id, event, state=str(current.state))
            report.add(
                "DraftVersion",
                version.id,
                event,
                state="OPEN",
                detail="v%d" % version.version_no,
            )
            _link_model(store, case.id, "draft_ids", draft.id, actor_kind=actor_kind, actor_id=actor_id)
    elif result.drafts and not result.draftable():
        report.warnings.append(
            "存在阻塞项（%s），按 SPEC §6.3 不产出可发送结论；草稿候选未落库"
            % "；".join(report.blocking_reasons)
        )

    # ---- Case 状态推进
    report.case_waited_reason = _waiting_reason(result)
    report.case_state = _advance_case(
        store,
        case_id=case.id,
        result=result,
        waited_reason=report.case_waited_reason,
        actor_kind=actor_kind,
        actor_id=actor_id,
    )

    # ---- 事件（重复处理只留审计，不重复建事件）
    event, created = _publish_event(
        store,
        document=document,
        case_id=case.id,
        producer=result.producer,
        clock=now,
    )
    report.event_id = event.id if event is not None else ""
    report.event_created = bool(created)
    report.duplicate_run = not created

    store.append_audit(
        _audit(
            case_id=case.id,
            document=document,
            action=str(AuditAction.WRITE),
            reason="MVP-3 理解：处理第 %d 次" % (1 if created else 2),
            detail={
                "document_id": document.id,
                "producer": result.producer,
                "time_fields": len(result.time_fields),
                "questions": len(result.questions),
                "conflicts": len(result.conflicts),
                "draftable": report.draftable,
                "event_created": bool(created),
            },
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
    )
    report.finished_at = now()
    return report


# --------------------------------------------------------------------------------------
# 辅助：落库
# --------------------------------------------------------------------------------------


def _time_slot_key(field: TimeField) -> str:
    return "%s|%s" % (field.semantic, field.original_label)


def _ensure_case(
    store: StoreProtocol,
    *,
    case_id: str,
    document: SourceDocument,
    actor_kind: str,
    actor_id: str,
) -> Case:
    try:
        case = store.get("Case", case_id)
    except NotFound:
        case = None
    if case is not None:
        return case  # type: ignore[return-value]
    return store.put(  # type: ignore[return-value]
        Case(
            id=case_id,
            goal=(CASE_GOAL_PREFIX + (document.source_id or document.id)),
            participants=[],
            source_document_ids=[document.id] if document.id else [],
            links={"source_document": [document.id]} if document.id else {},
        ),
        unique=("understand_case", document.id),
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason="为一封待理解邮件创建事务容器",
        audit_action=str(AuditAction.CREATE),
        audit_detail={"document_id": document.id},
    )


def _backfill_document_case(
    store: StoreProtocol,
    document: SourceDocument,
    case_id: str,
    *,
    actor_kind: str,
    actor_id: str,
) -> None:
    """把「文档 ↔ 事务」的关联**双向**补齐（契约 v0.8，CP-20261010-28）。

    一次补两个洞：

    1. `SourceDocument.case_id` 在 `process_document` 之后**一直是空的**——关联只写在
       `Case.source_document_ids` 里，于是任何按 `case_id` 过滤 SourceDocument 的下游
       （控制台、归档范围、检索过滤）都会漏掉全部文档，且**不报错**（静默空集）。
    2. `_ensure_case` 只在**新建**事务时写 `source_document_ids`；同一个事务里处理第二份资料
       （邮件正文 + 附件、或调用方预先指定了 `document.case_id`）时直接 `return`，
       不会把新资料挂上去——多资料事务少报成员。

    权威仍是 `Case.source_document_ids`（一份资料可以属于多个事务）；
    `SourceDocument.case_id` 是"主事务"的冗余字段，用于单跳查询。
    两个方向都**只增不减、可重复调用**（幂等）。
    """
    stored_doc = _get_soft(store, "SourceDocument", document.id)
    case = _get_soft(store, "Case", case_id)
    if case is None:
        return
    if document.id and document.id not in (case.source_document_ids or []):
        case.source_document_ids = list(case.source_document_ids or []) + [document.id]
        store.put(
            case,
            expected_revision=case.revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason="把资料挂到事务上（多资料事务的成员关系必须完整）",
            audit_action=str(AuditAction.UPDATE),
            audit_detail={"document_id": document.id},
        )
    if stored_doc is not None and stored_doc.case_id != case_id:
        stored_doc.case_id = case_id
        store.put(
            stored_doc,
            expected_revision=stored_doc.revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason="回填文档的主事务引用（此前该字段始终为空，按 case_id 过滤会漏）",
            audit_action=str(AuditAction.UPDATE),
            audit_detail={"case_id": case_id},
        )


def _upsert_fact(
    store: StoreProtocol,
    *,
    fact_id: str,
    case_id: str,
    subject: str,
    statement: str,
    citations: Sequence[Any],
    temporal_fields: Sequence[Dict[str, Any]],
    actor_kind: str,
    actor_id: str,
) -> str:
    payload = [item.to_dict() for item in citations]
    existing = _get_soft(store, "Fact", fact_id)
    if existing is None:
        store.put(
            Fact(
                id=fact_id,
                case_id=case_id,
                subject=subject,
                statement=statement,
                citations=payload,
                temporal_fields=list(temporal_fields),
                links={"case": [case_id]},
            ),
            unique=("understand_fact", fact_id),
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason="落库理解结果（带原文引用）",
            audit_action=str(AuditAction.CREATE),
            audit_detail={"subject": subject},
        )
        return "created"
    if existing.statement == statement and existing.temporal_fields == list(temporal_fields):
        _refresh_citation_hash(store, existing, payload, actor_kind=actor_kind, actor_id=actor_id)
        return "reused"
    existing.statement = statement
    existing.citations = payload
    existing.temporal_fields = list(temporal_fields)
    store.put(
        existing,
        expected_revision=existing.revision,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason="同一语义槽位的内容按新原文更新（旧值由审计与引用版本保留）",
        audit_action=str(AuditAction.UPDATE),
        audit_detail={"subject": subject},
    )
    return "updated"


def _refresh_citation_hash(
    store: StoreProtocol,
    existing: Any,
    payload: Sequence[Dict[str, Any]],
    *,
    actor_kind: str,
    actor_id: str,
) -> None:
    """引用内容与既有完全一致时**不写库**；只有哈希/版本变了才更新（避免无意义 revision 递增）。"""
    if existing.citations == list(payload):
        return
    existing.citations = list(payload)
    store.put(
        existing,
        expected_revision=existing.revision,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason="引用指向的原文版本变化，刷新引用（结论未变）",
        audit_action=str(AuditAction.UPDATE),
        audit_detail={"citations": len(payload)},
    )


def _upsert_inference(
    store: StoreProtocol,
    *,
    inference_id: str,
    case_id: str,
    statement: str,
    citations: Sequence[Any],
    confidence: float,
    alternatives: Sequence[str],
    actor_kind: str,
    actor_id: str,
) -> str:
    payload = [item.to_dict() for item in citations]
    existing = _get_soft(store, "Inference", inference_id)
    if existing is None:
        store.put(
            Inference(
                id=inference_id,
                case_id=case_id,
                statement=statement,
                content_kind="INFERENCE",
                confidence=confidence,
                alternatives=list(alternatives),
                candidate_only=True,
                links={"case": [case_id], "evidence": [item["source_object_id"] for item in payload]},
            ),
            unique=("understand_inference", inference_id),
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason="落库行动建议（推断，候选区）",
            audit_action=str(AuditAction.CREATE),
            audit_detail={"citations": len(payload)},
        )
        return "created"
    if existing.statement == statement and existing.confidence == confidence:
        return "reused"
    existing.statement = statement
    existing.confidence = confidence
    existing.alternatives = list(alternatives)
    store.put(
        existing,
        expected_revision=existing.revision,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason="行动建议随原文更新（仍为推断）",
        audit_action=str(AuditAction.UPDATE),
    )
    return "updated"


def _upsert_question(
    store: StoreProtocol,
    *,
    question_id: str,
    case_id: str,
    question: str,
    background: str,
    required_format: str,
    affects: Sequence[str],
    citations: Sequence[Any],
    actor_kind: str,
    actor_id: str,
    kind: str = "",
) -> Question:
    """落库追问。

    契约 v0.8（CP-20261010-25）：`kind` 必须落库。以前 `QuestionCandidate.kind` 只用来派生 ID
    和写报告 detail，对象上没有对应字段 → 类别在落库时被丢掉，下游只能猜文字
    （§14.5 明文写的 `Question(kind="conflict")` 因此压根表达不出来）。
    """
    existing = _get_soft(store, "Question", question_id)
    if existing is None:
        return store.put(  # type: ignore[return-value]
            Question(
                id=question_id,
                case_id=case_id,
                question=question,
                background=background,
                kind=kind,
                required_format=required_format,
                affects=list(affects),
                state=QuestionState.OPEN,
                asked_by_execution_id=None,
                links={"case": [case_id], "evidence": [item.source_object_id for item in citations]},
            ),
            unique=("understand_question", question_id),
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason="理解发现缺失/冲突/歧义信息，创建追问",
            audit_action=str(AuditAction.CREATE),
            audit_detail={"affects": list(affects), "kind": kind},
        )
    if existing.question != question or existing.background != background or existing.kind != kind:
        existing.question = question
        existing.background = background
        existing.kind = kind
        existing.affects = list(affects)
        store.put(
            existing,
            expected_revision=existing.revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason="追问随原文更新",
            audit_action=str(AuditAction.UPDATE),
        )
    return existing  # type: ignore[return-value]


def _upsert_task(
    store: StoreProtocol,
    *,
    task_id: str,
    case_id: str,
    title: str,
    detail: str,
    document: SourceDocument,
    todo_index: int,
    actor_kind: str,
    actor_id: str,
) -> Tuple[Task, str]:
    existing = _get_soft(store, "Task", task_id)
    inputs = {"document_id": document.id, "todo_index": todo_index, "detail": detail}
    if existing is None:
        task = store.put(  # type: ignore[assignment]
            Task(
                id=task_id,
                case_id=case_id,
                kind="understand_todo",
                title=title,
                state=TaskState.PENDING,
                assignee="user",
                inputs=inputs,
                links={"case": [case_id], "source_document": [document.id]},
            ),
            unique=("understand_task", task_id),
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason="理解抽出待办项，创建任务（不能因页面关闭而丢失）",
            audit_action=str(AuditAction.CREATE),
            audit_detail={"document_id": document.id},
        )
        return task, "created"  # type: ignore[return-value]
    if existing.title == title and existing.inputs == inputs:
        return existing, "reused"  # type: ignore[return-value]
    existing.title = title
    existing.inputs = inputs
    saved = store.put(
        existing,
        expected_revision=existing.revision,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason="待办随原文更新",
        audit_action=str(AuditAction.UPDATE),
    )
    return saved, "updated"  # type: ignore[return-value]


def _link_model(
    store: StoreProtocol,
    case_id: str,
    field_name: str,
    object_id: str,
    *,
    actor_kind: str,
    actor_id: str,
) -> None:
    case = _get_soft(store, "Case", case_id)
    if case is None:  # pragma: no cover - 由调用顺序保证
        return
    current = list(getattr(case, field_name) or [])
    if object_id in current:
        return
    current.append(object_id)
    setattr(case, field_name, current)
    store.put(
        case,
        expected_revision=case.revision,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason="关联理解结果到事务",
        audit_action=str(AuditAction.UPDATE),
        audit_detail={"field": field_name, "object_id": object_id},
    )


def _publish_event(
    store: StoreProtocol,
    *,
    document: SourceDocument,
    case_id: str,
    producer: str,
    clock: Any,
) -> Tuple[Optional[Event], bool]:
    """发布"邮件已被理解"事件；去重键由文档 ID + 内容哈希派生。

    这样"重复处理同一封邮件"不会产生第二个事件（幂等口径：事件不重复处理），
    而"原文内容发生变化"会产生新事件（那是新的事实，应当可见）。
    """
    source = "understand:%s" % (document.source_system or "local")
    source_event_id = "%s@%s" % (document.id, document.content_hash or "nohash")
    event = Event(
        id=_derive("Event", source_event_id, producer),
        kind="KNOWLEDGE_CHANGED",
        source=source,
        source_event_id=source_event_id,
        occurred_at=document.received_at or clock(),
        received_at=clock(),
        case_id=case_id,
        dedupe_key=event_dedupe_key(source, source_event_id),
        payload={
            "document_id": document.id,
            "content_hash": document.content_hash,
            "content_version": document.content_version,
            "producer": producer,
        },
    )
    saved, created = store.publish_event(event)
    return saved, bool(created)


def _stale_drafts(
    store: StoreProtocol,
    *,
    case_id: str,
    reason: str,
    actor_kind: str,
    actor_id: str,
) -> List[Any]:
    """把 Case 下所有 `OPEN` 草稿置为 `STALE`，并从 `draft_ids` 摘掉。

    SPEC §6.3 / §16 MVP-3 第 4 条："事实与资料冲突 → **不生成可发送结论**"。
    现实序列里草稿可能已经先生成（先到的资料没有冲突），随后第二份资料才暴露冲突；
    这时不能让那个草稿继续躺在 Case 上被当成"待发送结论"。

    做法是**标记为过期**而不是删除、也不清 `version_ids`：历史版本仍可审计，
    只是不再挂在事务的可发送集合里（`DraftState.STALE`，见 `contracts/enums.py`）。
    """
    case = _get_soft(store, "Case", case_id)
    if case is None:
        return []
    stale: List[Any] = []
    remaining: List[str] = []
    for draft_id in list(case.draft_ids):
        draft = _get_soft(store, "Draft", draft_id)
        if draft is None:
            continue
        if str(draft.state) == str(DraftState.STALE):
            continue
        draft.state = DraftState.STALE
        saved = store.put(
            draft,
            expected_revision=draft.revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason="资料冲突，作废待发送草稿：%s" % reason,
            audit_action=str(AuditAction.UPDATE),
            audit_detail={"case_id": case_id, "reason": reason},
        )
        stale.append(saved)
        remaining = [item for item in case.draft_ids if item != draft_id]
    if stale:
        fresh = _get_soft(store, "Case", case_id)
        if fresh is not None:
            fresh.draft_ids = remaining
            store.put(
                fresh,
                expected_revision=fresh.revision,
                actor_kind=actor_kind,
                actor_id=actor_id,
                reason="把作废草稿从事务的可发送集合中摘除",
                audit_action=str(AuditAction.UPDATE),
                audit_detail={"stale_draft_ids": [item.id for item in stale]},
            )
    return stale


def _advance_case(
    store: StoreProtocol,
    *,
    case_id: str,
    result: UnderstandingResult,
    waited_reason: Optional[str],
    actor_kind: str,
    actor_id: str,
) -> str:
    """把 Case 状态推进到"分析完成"，并按是否有阻塞项分流。

    - 有待回答/冲突 → `WAITING_FOR_USER`（等待用户裁决，绝不自动生成可发送结论）；
    - 有可发送草稿 → `READY_FOR_REVIEW`（等待用户审批）；
    - 两者都没有 → 停在 `ANALYZING` 并由 `waiting_reason`/`next_action` 说明原因。

    只走合法转换：非法转换直接跳过（状态机是唯一裁决点，见 `contracts/transitions.py`）。
    `NEW` 不能直接进 `ANALYZING`（契约允许 `NEW → TRIAGED`），所以路径是
    `NEW → TRIAGED → ANALYZING → {WAITING_FOR_USER | READY_FOR_REVIEW}`。
    """
    route = [
        CaseState.TRIAGED,
        CaseState.ANALYZING,
        CaseState.WAITING_FOR_USER if waited_reason else CaseState.READY_FOR_REVIEW,
    ]
    state = str(_get_soft(store, "Case", case_id).state)  # type: ignore[union-attr]
    for target in route:
        if str(target) == state:
            continue
        if not machine_for("Case").can(state, str(target)):
            continue
        saved = store.transition(
            "Case",
            case_id,
            str(target),
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason="理解完成：%s" % ("存在待回答项" if waited_reason else "进入复核"),
        )
        state = str(saved.state)

    case = _get_soft(store, "Case", case_id)
    if case is not None:
        next_action = (
            "等待用户回答追问" if waited_reason else ("等待用户审批草稿" if result.draftable() else "等待用户确认是否需要处理")
        )
        # 契约 v0.8（CP-20261010-29）：`blocked_reason` 必须**随事实更新，包括清除**。
        # 以前它被写在"等待原因/下一步变了"这个不相关的条件里，于是：冲突被裁决（规则废止、
        # 或新资料不再冲突）之后重新理解同一个事务时，只要"等待原因"恰好没变，
        # `blocked_reason` 就**留着不清**——事务永久卡在阻塞态。
        # 与审批门闸（CP-27）叠加后这会是死锁：既不能发，也不能批准。
        desired_blocked = (
            "；".join(item.detail for item in result.conflicts) if result.conflicts else None
        )
        reason_changed = case.waiting_reason != waited_reason or case.next_action != next_action
        blocked_changed = (case.blocked_reason or None) != desired_blocked
        if reason_changed or blocked_changed:
            if reason_changed:
                case.waiting_reason = waited_reason
                case.next_action = next_action
                case.last_action_summary = "MVP-3 理解：抽出 %d 个时间字段、%d 条待办、%d 个追问" % (
                    len(result.time_fields),
                    len(result.todos),
                    len(result.questions),
                )
            if blocked_changed:
                case.blocked_reason = desired_blocked
            saved = store.put(
                case,
                expected_revision=case.revision,
                actor_kind=actor_kind,
                actor_id=actor_id,
                reason="写明等待原因、下一步与阻塞原因（SPEC §11.4、§14.5）",
                audit_action=str(AuditAction.UPDATE),
                audit_detail={"blocked_reason": case.blocked_reason or "", "blocked": bool(case.blocked_reason)},
            )
            state = str(saved.state)
    return state


def _waiting_reason(result: UnderstandingResult) -> Optional[str]:
    """必须停下来等用户的情形：冲突、缺材料、语义歧义/时间缺失。"""
    reasons = result.blocking_reasons()
    if not reasons:
        return None
    return "；".join(reasons)


def _known_time_fields(
    store: StoreProtocol,
    case_id: str,
    *,
    exclude_document_id: str = "",
) -> List[TimeField]:
    """同一 Case 下**已经存在**的时间字段（跨文档冲突检测的输入）。"""
    out: List[TimeField] = []
    for fact in store.find("Fact", where={"case_id": case_id}, limit=None):
        for payload in fact.temporal_fields or []:
            try:
                from ..contracts.timeutil import TemporalField

                temporal = TemporalField.from_dict(payload)
            except Exception:
                continue
            citations = []
            for item in fact.citations or []:
                if exclude_document_id and item.get("source_object_id") == exclude_document_id:
                    continue
                try:
                    from ..contracts.citations import Citation

                    citations.append(_citation_ref(Citation.from_dict(item)))
                except Exception:
                    continue
            out.append(TimeField(temporal=temporal, citations=citations or [_synthetic_citation(fact)], label_text=temporal.original_label))
    return out


def _time_fields_of_document(store: StoreProtocol, document_id: str) -> List[TimeField]:
    fields: List[TimeField] = []
    for fact in store.find("Fact", limit=None):
        payloads = fact.citations or []
        if not any(item.get("source_object_id") == document_id for item in payloads):
            continue
        for payload in fact.temporal_fields or []:
            try:
                from ..contracts.timeutil import TemporalField

                temporal = TemporalField.from_dict(payload)
            except Exception:
                continue
            fields.append(TimeField(temporal=temporal, citations=[], label_text=temporal.original_label))
    return fields


def _synthetic_citation(fact: Any) -> Any:
    """Fact 的引用被过滤掉时的兜底引用（保证 `TimeField.validate()` 不因缺引用而崩）。

    `Fact.citations` 在正常路径下必非空（引用守卫），这里只是让"跨文档比较"在
    极端输入下也能安全退化，而不是抛异常。
    """
    try:
        return _citation_ref_from_payload(fact.citations[0])
    except Exception:
        from ..contracts.citations import Citation

        return _citation_ref(
            Citation(
                source_object_id=fact.id,
                source_object_type="Fact",
                path="",
                locator="fact:%s" % fact.id,
                content_hash="0" * 64,
                supports="（Fact 引用缺失时的兜底定位）",
                excerpt="",
            )
        )


def _citation_ref(citation: Any) -> Any:
    from .model import CitationRef

    return CitationRef.require(citation)


def _citation_ref_from_payload(payload: Dict[str, Any]) -> Any:
    from ..contracts.citations import Citation
    from .model import CitationRef

    return CitationRef.require(Citation.from_dict(payload))


def _get_soft(store: StoreProtocol, object_type: str, object_id: str) -> Optional[Any]:
    try:
        return store.get(object_type, object_id)
    except NotFound:
        return None


def _audit(
    *,
    case_id: str,
    document: SourceDocument,
    action: str,
    reason: str,
    detail: Dict[str, Any],
    actor_kind: str,
    actor_id: str,
) -> Any:
    """构造本次处理的审计事件（只追加；审计是"处理发生过"的客观证据）。"""
    from ..contracts.objects import AuditEvent

    return AuditEvent(
        case_id=case_id,
        action=action,
        actor_kind=actor_kind,
        actor_id=actor_id,
        object_type="SourceDocument",
        object_id=document.id,
        result="OK",
        reason=reason,
        detail=detail,
    )
