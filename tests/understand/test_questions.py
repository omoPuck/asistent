"""MVP-3 追问测试（SPEC §8.1、§21.1、§21.2）。

要证明的事：**不明确就追问**。缺材料、时间不明、语义歧义、跨资料冲突
四种情况都必须落成 `Question` 对象（可被用户回答），并且此时 Case 停下等用户。
"""

from __future__ import annotations

import unittest

from asistent.contracts.enums import CaseState, QuestionState

from .helpers import (
    UnderstandTestCase,
    drafts_of,
    make_document,
    open_drafts,
    process,
    questions_of,
    raw_extract,
)


class QuestionCreationTests(UnderstandTestCase):
    def test_missing_material_produces_question_per_item(self):
        """缺材料：每个材料一条 Question，`affects` 指向具体材料，且都带背景说明。"""
        document = make_document(self.store, "03-missing-material.eml")
        report = process(self.store, document)
        questions = questions_of(self.store, report.case_id)
        self.assertEqual(2, len(questions))
        affects = sorted(item for question in questions for item in question.affects)
        self.assertEqual(["材料:学生一寸免冠照片", "材料:监护人知情同意书"], affects)
        for question in questions:
            self.assertEqual(QuestionState.OPEN, question.state)
            self.assertIn("已提交或已具备的证据", question.question)
            self.assertTrue(question.background.strip())
            self.assertEqual(report.case_id, question.case_id)

    def test_pending_time_produces_missing_time_question(self):
        """「待定」的时间字段：一条 missing_time 追问，`affects` 指向该语义槽位。"""
        document = make_document(self.store, "04-ambiguous-time.eml")
        report = process(self.store, document)
        questions = questions_of(self.store, report.case_id)
        affects = sorted(item for question in questions for item in question.affects)
        self.assertIn("时间字段:registration_deadline", affects)
        self.assertIn("时间字段:unknown", affects)
        pending = [item for item in questions if "时间字段:registration_deadline" in item.affects][0]
        self.assertIn("待定", pending.question)
        self.assertIn("报名截止时间", pending.question)

    def test_ambiguous_date_produces_question_not_a_time_field(self):
        """语义不明的裸日期：追问「它属于哪一类」，并且不落成时间字段。"""
        result = raw_extract("04-ambiguous-time.eml")
        ambiguous = [item for item in result.questions if item.kind == "ambiguity"]
        self.assertEqual(1, len(ambiguous))
        self.assertIn("10月24日", ambiguous[0].question)
        self.assertIn("哪一类", ambiguous[0].question)
        for field in result.time_fields:
            self.assertNotEqual("10月24日", field.original_text)

    def test_conflict_produces_question_with_both_values(self):
        """跨资料冲突：一条追问里**同时**给出两个取值，让用户裁决。"""
        case_id = "case_conflict_test"
        first = make_document(
            self.store, "02a-notice-conflict.eml", document_id="doc_c_a", case_id=case_id
        )
        process(self.store, first, name="02a-notice-conflict.eml")
        second = make_document(
            self.store, "02b-notice-conflict.eml", document_id="doc_c_b", case_id=case_id
        )
        report = process(self.store, second, name="02b-notice-conflict.eml")

        questions = questions_of(self.store, case_id)
        self.assertEqual(1, len(questions))
        question = questions[0]
        self.assertIn("2026-10-20T09:00:00.000Z", question.question)
        self.assertIn("2026-10-25T09:00:00.000Z", question.question)
        self.assertIn("以哪一个为准", question.question)
        self.assertEqual(["时间字段:registration_deadline"], list(question.affects))
        self.assertEqual(1, len(report.conflicts))
        self.assertEqual(
            ["2026-10-25T09:00:00.000Z", "2026-10-20T09:00:00.000Z"],
            report.conflicts[0]["values"],
        )

    def test_conflict_question_is_idempotent(self):
        """同一冲突重复处理不产生第二条追问（追问 ID 由内容派生）。"""
        case_id = "case_conflict_idem"
        first = make_document(
            self.store, "02a-notice-conflict.eml", document_id="doc_i_a", case_id=case_id
        )
        process(self.store, first, name="02a-notice-conflict.eml")
        second = make_document(
            self.store, "02b-notice-conflict.eml", document_id="doc_i_b", case_id=case_id
        )
        process(self.store, second, name="02b-notice-conflict.eml")
        before = len(questions_of(self.store, case_id))
        process(self.store, second, name="02b-notice-conflict.eml")
        process(self.store, second, name="02b-notice-conflict.eml")
        self.assertEqual(before, len(questions_of(self.store, case_id)))

    def test_conflict_has_no_sendable_conclusion(self):
        """冲突时**不生成可发送结论**：没有 OPEN 草稿，Case 停下等用户。"""
        case_id = "case_conflict_block"
        first = make_document(
            self.store, "02a-notice-conflict.eml", document_id="doc_b_a", case_id=case_id
        )
        process(self.store, first, name="02a-notice-conflict.eml")
        second = make_document(
            self.store, "02b-notice-conflict.eml", document_id="doc_b_b", case_id=case_id
        )
        report = process(self.store, second, name="02b-notice-conflict.eml")

        self.assertFalse(report.draftable)
        self.assertEqual([], open_drafts(self.store))
        self.assertEqual([], list(self.store.get("Case", case_id).draft_ids))
        self.assertEqual(CaseState.WAITING_FOR_USER, self.store.get("Case", case_id).state)
        # 冲突详情写进 Case，用户不用翻日志
        blocked = self.store.get("Case", case_id).blocked_reason or ""
        self.assertIn("registration_deadline", blocked)

    def test_draft_from_before_the_conflict_is_marked_stale(self):
        """先到的资料生成了草稿 → 冲突出现后草稿必须被标 STALE（历史仍可审计）。"""
        case_id = "case_conflict_stale"
        first = make_document(
            self.store, "02a-notice-conflict.eml", document_id="doc_s_a", case_id=case_id
        )
        first_report = process(self.store, first, name="02a-notice-conflict.eml")
        self.assertTrue(first_report.draftable)
        drafts_before = drafts_of(self.store, case_id)
        self.assertEqual(1, len(drafts_before))
        self.assertEqual("OPEN", str(drafts_before[0].state))

        second = make_document(
            self.store, "02b-notice-conflict.eml", document_id="doc_s_b", case_id=case_id
        )
        process(self.store, second, name="02b-notice-conflict.eml")

        drafts_after = drafts_of(self.store, case_id)
        self.assertEqual(1, len(drafts_after))
        self.assertEqual("STALE", str(drafts_after[0].state))
        # 版本历史没有被删除（审计需要）
        self.assertTrue(drafts_after[0].version_ids)
        self.assertEqual(1, len(self.store.find("DraftVersion", limit=None)))

    def test_questions_are_open_and_answerable(self):
        """追问是 `OPEN` 且带 `required_format`，用户能直接答（§8.1 异步追问）。"""
        document = make_document(self.store, "03-missing-material.eml")
        report = process(self.store, document)
        for question in questions_of(self.store, report.case_id):
            self.assertEqual(QuestionState.OPEN, question.state)
            self.assertEqual("text", question.required_format)
            self.assertIsNone(question.answer_id)

    def test_resolved_document_needs_no_questions(self):
        """资料齐全、时间明确的邮件：不产生任何追问（不噪声化）。"""
        document = make_document(self.store, "05-reply-needed-ok.eml")
        report = process(self.store, document)
        self.assertEqual([], questions_of(self.store, report.case_id))
        self.assertEqual([], report.blocking_reasons)

    def test_case_waits_for_user_when_blocked(self):
        """有阻塞项 → `WAITING_FOR_USER` 且 `waiting_reason` 写明原因（§11.4）。"""
        for name, keyword in (
            ("03-missing-material.eml", "missing_material"),
            ("04-ambiguous-time.eml", "missing_time"),
        ):
            store = type(self.store)()
            document = make_document(store, name)
            report = process(store, document)
            case = store.get("Case", report.case_id)
            self.assertEqual(CaseState.WAITING_FOR_USER, case.state)
            self.assertIn(keyword, case.waiting_reason or "")
            self.assertEqual("等待用户回答追问", case.next_action)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
