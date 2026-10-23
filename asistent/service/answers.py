"""把"用户已回答的追问"接进下一轮理解（MVP-8 兜底；SPEC §8.1、§21.7）。

为什么需要这一层
================

契约 v0.7 的 `Question` 没有 `kind` 字段，MVP-3 的抽取器又是**纯函数**：
对同一封邮件重跑 `process_document` 永远得到同一批 `QuestionCandidate`。
因此"用户在手机上答完追问 → 该阻塞项解除 → 产出可发送草稿"这条路径在下层
**不存在**——`asistent/console/service.py::_refresh_case_waiting_state` 把事务置为
`WAITING_FOR_CONTEXT`，并写明"等待 Agent 基于回答重新分析"。那就是本包要干的活。

做法（只在**输出**上兜底，不碰下层）
====================================

`AnswerAwareUnderstander` 包住真正的理解器（通常是 `growth.bridge.GrowthUnderstander`）：

1. 先让被包的理解器正常跑（规则、命中审计一律照旧）；
2. 算出哪些阻塞型追问**已经被用户回答过**——依据是存储里真实存在的
   `Question(state=ANSWERED)`，比对 `question` 文本 + `affects` 组成的**结构化键**，
   不是猜（CP-20261010-33：这条以前没做到，曾经有一条"只看文本"的退路）；
3. 若有，把结果换成 `AnswerAwareUnderstandingResult`：`questions` 列表**原样保留**
   （`process_document` 的 upsert 因此完全不变，不会新建/改名任何 `Question`，
   也不会因为下标移动把两条追问串在一起），只覆写 `draftable()` / `blocking_reasons()`
   把已解除的那些下标排除。

为什么不用"从候选里删掉已答项"
------------------------------

`process_document` 用 `_derive("Question", document_id, kind, index)` 生成追问 ID ——
**下标参与身份**。删掉第 1 条会把后面的候选整体前移，于是第 2 条的候选拿到第 1 条的 ID，
把用户的回答"改"成另一个问题（实测：`_upsert_question` 会 `UPDATE` 已有对象）。
所以本层保留候选列表、只改"哪些还算阻塞"的判断。

**不解除冲突**：`kind="conflict"`（规则冲突）永远留给 MVP-6 的裁决路径，
用户回答一条普通追问不构成对规则冲突的裁决（SPEC §861）。

> Lead 已宣布契约 v0.8 补 `Question.kind`。届时本兜底可简化为
> "按 `kind` + `state` 直接过滤候选并跳过已答下标"，不必再比对问题文本。
"""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..contracts.citations import Citation
from ..contracts.enums import ActorKind, AuditAction, QuestionState
from ..contracts.ids import digest_of, new_id
from ..contracts.objects import AuditEvent, Fact
from ..contracts.store_api import StoreProtocol
from ..contracts.timeutil import utcnow_iso
from ..understand.model import QuestionCandidate, UnderstandingResult
from ..understand.understander import AnalysisInput, RuleBasedUnderstander

#: 必须由用户澄清才放行的追问类型（与 `UnderstandingResult.draftable()` 同一口径）
BLOCKING_KINDS: Tuple[str, ...] = ("ambiguity", "conflict", "missing_material", "missing_time")

#: 本层**允许**被"用户已回答"解除的类型：`conflict` 不在其中（那是 MVP-6 的裁决路径）
ANSWER_RESOLVABLE_KINDS: Tuple[str, ...] = ("ambiguity", "missing_material", "missing_time")

#: 回答转事实时用的来源类型
ANSWER_SOURCE_TYPE = "user_answer"


def answered_questions(store: StoreProtocol, case_id: str) -> List[Any]:
    """该 Case 下所有已回答（`ANSWERED`）的追问。"""
    if not case_id:
        return []
    try:
        rows = store.find("Question", where={"case_id": case_id}, limit=None)
    except Exception:  # noqa: BLE001 - 观测/兜底路径不因一次查询失败而中断一轮
        return []
    return [item for item in rows if str(item.state) == str(QuestionState.ANSWERED)]


def _answer_text(store: StoreProtocol, answer_id: Optional[str]) -> str:
    if not answer_id:
        return ""
    try:
        answer = store.get("Answer", answer_id)
    except Exception:  # noqa: BLE001
        return ""
    return str(getattr(answer, "answer_text", "") or "")


def record_answer_facts(
    store: StoreProtocol,
    *,
    case_id: str,
    actor_id: str = "service",
) -> List[str]:
    """把用户的回答落成**带引用的事实**（SPEC §5.4："来自用户明确输入的事实"）。

    引用指向 `Answer` 对象本身（`locator="answer:1"`、`content_hash=sha256(正文)`）——
    既有可追溯来源，又不冒充"原文里写过的内容"。已落过的回答不会重复落（ID 由回答 ID 派生）。
    """
    created: List[str] = []
    for question in answered_questions(store, case_id):
        answer_id = str(getattr(question, "answer_id", "") or "")
        if not answer_id:
            continue
        fact_id = new_id("Fact", token=digest_of(["answer_fact", answer_id])[:24])
        try:
            existing = store.get("Fact", fact_id)
        except Exception:  # noqa: BLE001 - NotFound 或后端差异
            existing = None
        if existing is not None:
            continue
        text = _answer_text(store, answer_id)
        citation = Citation(
            source_object_id=answer_id,
            source_object_type="Answer",
            path="answer:%s" % answer_id,
            locator="answer:1",
            content_hash=digest_of(["answer_text", text]),
            supports="支持这条事实：用户在手机控制台对该追问的明确回答",
            excerpt=text[:200],
        )
        fact = Fact(
            id=fact_id,
            case_id=case_id,
            subject="用户回答:%s" % str(getattr(question, "affects", ["?"])[0] if question.affects else "?"),
            statement="用户回答：%s" % text,
            citations=[citation.to_dict()],
            confirmed_by_user=True,
            source_system=ANSWER_SOURCE_TYPE,
            links={"case": [case_id], "question": [question.id]},
        )
        store.put(
            fact,
            unique=("answer_fact", answer_id),
            actor_kind=str(ActorKind.USER),
            actor_id=actor_id,
            reason="用户回答转事实（SPEC §5.4：用户明确输入）",
            audit_action=str(AuditAction.CREATE),
            audit_detail={"answer_id": answer_id, "question_id": question.id, "case_id": case_id},
        )
        created.append(fact_id)
    return created


class AnswerAwareUnderstandingResult(UnderstandingResult):
    """`UnderstandingResult` + "哪些追问已由用户回答解除"。

    只在 `resolved_question_indexes` 非空时使用（否则理解结果原样返回，行为零变化）。
    """

    resolved_question_indexes: Tuple[int, ...] = ()
    #: 人类可读的解除说明（进报告 `warnings`，供文档/日志取证）
    resolution_notes: Tuple[str, ...] = ()

    def _is_resolved(self, index: int) -> bool:
        return index in set(self.resolved_question_indexes)

    def draftable(self) -> bool:
        if not self.drafts or self.conflicts:
            return False
        for index, question in enumerate(self.questions):
            if self._is_resolved(index):
                continue
            if question.kind in BLOCKING_KINDS:
                return False
        for draft in self.drafts:
            if not draft.citations:
                return False
        return True

    def blocking_reasons(self) -> List[str]:
        reasons: List[str] = []
        for conflict in self.conflicts:
            reasons.append("冲突：%s" % (conflict.detail or conflict.semantic))
        for index, question in enumerate(self.questions):
            if self._is_resolved(index):
                continue
            if question.kind in BLOCKING_KINDS:
                reasons.append("待回答（%s）：%s" % (question.kind, question.question))
        if not self.drafts and not reasons:
            reasons.append("本封邮件没有被识别为需要回复")
        return reasons


def as_answer_aware(
    result: UnderstandingResult,
    resolved: Sequence[int],
    notes: Sequence[str] = (),
) -> AnswerAwareUnderstandingResult:
    """把普通理解结果转成"带解除项"的结果（逐字段复制，不共享可变状态）。"""
    payload: Dict[str, Any] = {item.name: getattr(result, item.name) for item in fields(result)}
    aware = AnswerAwareUnderstandingResult(**payload)
    aware.resolved_question_indexes = tuple(sorted(int(item) for item in resolved))
    aware.resolution_notes = tuple(notes)
    return aware


class AnswerAwareUnderstander:
    """在任意理解器之上应用"已回答的追问不再阻塞"（默认包住 MVP-3 的规则理解器）。"""

    name = "answer-aware"

    def __init__(
        self,
        store: StoreProtocol,
        *,
        case_id: str = "",
        base: Optional[Any] = None,
        clock: Optional[Any] = None,
        actor_id: str = "service",
    ) -> None:
        self.store = store
        self.case_id = case_id
        self.base = base or RuleBasedUnderstander()
        self.clock = clock
        self.actor_id = actor_id
        #: 最近一次解释（可观察：文档/日志里要能说清"凭什么是已解除"）
        self.resolved: List[Dict[str, Any]] = []
        self.last_result: Optional[UnderstandingResult] = None

    # ------------------------------------------------------------------ 协议
    def call_log(self) -> Any:
        getter = getattr(self.base, "call_log", None)
        if callable(getter):
            return getter()
        from ..understand.understander import ModelCallLog

        return ModelCallLog()

    def analyze(self, request: AnalysisInput) -> UnderstandingResult:
        result = self.base.analyze(request)
        self.last_result = result
        self.resolved = []
        case_id = self.case_id or getattr(result, "case_id", "") or ""
        if not case_id:
            case_id = self._case_id_from_request(request)
        if not case_id:
            return result

        answered = answered_questions(self.store, case_id)
        if not answered:
            return result

        by_key: Dict[Tuple[str, str], Any] = {}
        for item in answered:
            for slot in list(getattr(item, "affects", []) or []) or [""]:
                by_key[(str(getattr(item, "question", "") or ""), str(slot))] = item

        resolved: List[int] = []
        notes: List[str] = []
        for index, candidate in enumerate(result.questions):
            if candidate.kind not in ANSWER_RESOLVABLE_KINDS:
                continue
            match = self._match(candidate, by_key)
            if match is None:
                # 结构上没配上：**不解除阻塞**，但如果同一句文本另有回答，要说清原因。
                # 否则用户看到的是"我回答了，追问还在"——一个不解释的失败。
                near = self._near_miss(candidate, by_key)
                if near is not None:
                    notes.append(
                        "同一句追问在 affects 上不是同一处，未自动解除阻塞（请人工确认）：question=%s"
                        % (getattr(near, "id", "") or "-")
                    )
                    self._audit(
                        case_id=case_id,
                        candidate=candidate,
                        question=near,
                        result="REJECTED",
                        reason="affects 槽位不同：不按问题文本猜测配对，追问继续阻塞",
                    )
                continue
            resolved.append(index)
            self.resolved.append(
                {
                    "index": index,
                    "kind": candidate.kind,
                    "question_id": str(getattr(match, "id", "")),
                    "answer_id": str(getattr(match, "answer_id", "") or ""),
                    "affects": list(getattr(candidate, "affects", []) or []),
                }
            )
            notes.append(
                "已由用户回答解除（SPEC §8.1）：question=%s answer=%s"
                % (getattr(match, "id", ""), getattr(match, "answer_id", "") or "-")
            )
            self._audit(case_id=case_id, candidate=candidate, question=match)

        if not resolved and not notes:
            return result
        # 注意：即使一条都没解除，只要 `notes` 有内容也要返回（不然"为什么没解除"这句话
        # 就永远传不出去）。`as_answer_aware` 是逐字段复制，空 `resolved` 等价于原结果 + 说明。
        aware = as_answer_aware(result, resolved, notes)
        aware.warnings = list(result.warnings) + notes
        return aware

    # ------------------------------------------------------------------ 内部
    def _case_id_from_request(self, request: AnalysisInput) -> str:
        """`AnalysisInput` 上没有 case_id；按 `process_document` 的口径从文档反查。"""
        document_id = str(getattr(request, "document_id", "") or "")
        if not document_id:
            return ""
        try:
            document = self.store.get("SourceDocument", document_id)
        except Exception:  # noqa: BLE001
            return ""
        return str(getattr(document, "case_id", "") or "")

    @staticmethod
    def _keys(candidate: QuestionCandidate) -> List[Tuple[str, str]]:
        """候选追问的结构化键：`(问题文本, affects 槽位)`。

        `affects` 为空时用空串当槽位——这样"两边都没有 affects"仍然算同一件事，
        但"一边有一边没有"就不再算匹配（以前会算，见 `_match` 的说明）。
        """
        text = str(candidate.question or "")
        slotted = list(candidate.affects or []) or [""]
        return [(text, str(slot)) for slot in slotted]

    @staticmethod
    def _match(candidate: QuestionCandidate, by_key: Dict[Tuple[str, str], Any]) -> Optional[Any]:
        """只在**结构化键**上匹配。

        这里以前还有一条"退路：只按问题文本匹配"的分支，已删除（CP-20261010-33）。
        两条理由：

        1. **它会猜，而且猜错的方向最危险**。同一句追问可以针对不同对象
           （两个草稿都可能问"活动开始时间？"）。只看文本匹配会把"对 A 的回答"
           当成"B 已回答"，而 `Question(state=ANSWERED)` 一旦成立，B 就不再是阻塞项——
           也就是说**一个真实的缺口会静默消失**。宁可让一条追问继续挡着（看得见、能处理），
           也不能让不该放行的草稿被放行。
        2. **它保护的旧数据不存在**：设备实测 `Question` 对象 **0 个**
           （2026-10-12，`~/asistent-var/data/asistent.db`），而结构化 `affects`
           从 v0.8 起就在契约里。

        匹配不上时的正确行为是"失败但说清楚"，见 `_near_miss` 与 `analyze` 里的记账。
        """
        for key in AnswerAwareUnderstander._keys(candidate):
            found = by_key.get(key)
            if found is not None:
                # 同一个键有多条回答时，后来写入的覆盖先前的（`by_key` 的构造顺序即写入顺序），
                # 即"以最新的回答为准"。这是有意的：用户改主意时应该以最后一次为准。
                return found
        return None

    @staticmethod
    def _near_miss(candidate: QuestionCandidate, by_key: Dict[Tuple[str, str], Any]) -> Optional[Any]:
        """同一句文本、但 `affects` 槽位不同的那条回答（用于解释"为什么没解除阻塞"）。

        没有它的话，用户会看到"我明明回答了，追问还在"——**静默不生效**和"答错对象"一样糟：
        前者让人以为系统坏了，后者让人以为系统懂了。
        """
        text = str(candidate.question or "")
        for (known_text, _slot), item in by_key.items():
            if known_text and known_text == text:
                return item
        return None

    def _audit(
        self,
        *,
        case_id: str,
        candidate: QuestionCandidate,
        question: Any,
        result: str = "OK",
        reason: str = "用户已回答：本次重新分析不再把该追问算作阻塞项",
    ) -> None:
        moment = self.clock() if callable(self.clock) else utcnow_iso()
        self.store.append_audit(
            AuditEvent(
                at=moment,
                action=str(AuditAction.UPDATE),
                actor_kind=ActorKind.AGENT,
                actor_id=self.actor_id,
                case_id=case_id,
                object_type="Question",
                object_id=str(getattr(question, "id", "")),
                result=result,
                reason=reason,
                detail={
                    "question_kind": candidate.kind,
                    "answer_id": str(getattr(question, "answer_id", "") or ""),
                    "affects": list(candidate.affects or []),
                    "resolved_by": "service.answers.AnswerAwareUnderstander",
                },
            )
        )


__all__ = [
    "ANSWER_RESOLVABLE_KINDS",
    "ANSWER_SOURCE_TYPE",
    "AnswerAwareUnderstander",
    "AnswerAwareUnderstandingResult",
    "BLOCKING_KINDS",
    "answered_questions",
    "as_answer_aware",
    "record_answer_facts",
]
