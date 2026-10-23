"""契约 v0.8 在理解层的两个洞（CP-20261010-25 / CP-20261010-28）。

两条都不是"实现写错"，而是**对象模型缺东西**，于是下游只能绕，且绕法不一致：

1. 追问类别（`QuestionCandidate.kind`）以前只用来派生 ID 和写报告，**落库时被丢掉**——
   契约 §14.5 明文写的 `Question(kind="conflict")` 因此压根表达不出来，控制台/成长层只能读文字猜；
2. `SourceDocument.case_id` 从不回填 + `_ensure_case` 只在新建事务时挂资料 →
   多资料事务少报成员，且任何按 `case_id` 过滤文档的下游会**静默**拿到空集。
"""

from __future__ import annotations

import unittest

from .helpers import UnderstandTestCase, make_document, process, questions_of


class QuestionKindPersistedTest(UnderstandTestCase):
    def test_kind_is_persisted_not_just_derived(self) -> None:
        document = make_document(self.store, "03-missing-material.eml")
        report = process(self.store, document)
        questions = questions_of(self.store, report.case_id)
        self.assertTrue(questions, "这封邮件必须产生追问")
        for item in questions:
            self.assertTrue(
                item.kind,
                f"追问 {item.id} 的 kind 是空的：类别没有落库，下游只能猜文字",
            )
        kinds = sorted({item.kind for item in questions})
        self.assertIn("missing_material", kinds)

    def test_kind_matches_the_reported_candidate_kind(self) -> None:
        """报告里写的 detail 与库里对象的 kind 必须一致（否则两处口径会漂移）。"""
        document = make_document(self.store, "03-missing-material.eml")
        report = process(self.store, document)
        reported = sorted(
            str(item.detail) for item in report.outcomes if item.kind == "Question"
        )
        stored = sorted(item.kind for item in questions_of(self.store, report.case_id))
        self.assertEqual(reported, stored)


class DocumentCaseBackfillTest(UnderstandTestCase):
    def test_document_gets_its_case_id_backfilled(self) -> None:
        document = make_document(self.store, "02a-notice-conflict.eml")
        self.assertFalse(document.case_id, "前置条件：文档入库时还没有归属事务")
        report = process(self.store, document)
        stored = self.store.get("SourceDocument", document.id)
        self.assertEqual(report.case_id, stored.case_id)
        # 按 case_id 过滤必须能查到（这正是以前会静默漏掉的那类查询）
        found = list(self.store.find("SourceDocument", where={"case_id": report.case_id}, limit=None))
        self.assertEqual([document.id], [item.id for item in found])

    def test_second_document_in_the_same_case_is_attached(self) -> None:
        """同一个事务里的第二份资料必须出现在 `Case.source_document_ids` 里。"""
        first = make_document(self.store, "02a-notice-conflict.eml", document_id="doc_first")
        report = process(self.store, first)
        case_before = self.store.get("Case", report.case_id)
        self.assertEqual(["doc_first"], list(case_before.source_document_ids))

        second = make_document(
            self.store,
            "03-missing-material.eml",
            document_id="doc_second",
            case_id=report.case_id,
        )
        second_report = process(self.store, second, name="03-missing-material.eml")
        self.assertEqual(report.case_id, second_report.case_id)
        case_after = self.store.get("Case", report.case_id)
        self.assertEqual(
            ["doc_first", "doc_second"],
            list(case_after.source_document_ids),
            "第二份资料没有被挂到事务上：多资料事务会少报成员",
        )
        self.assertEqual(report.case_id, self.store.get("SourceDocument", "doc_second").case_id)

    def test_backfill_is_idempotent(self) -> None:
        """重复处理同一份资料不得重复挂成员（幂等是可重复调用的前提）。"""
        document = make_document(self.store, "02a-notice-conflict.eml")
        first = process(self.store, document)
        second = process(self.store, document)
        self.assertEqual(first.case_id, second.case_id)
        case = self.store.get("Case", first.case_id)
        self.assertEqual([document.id], list(case.source_document_ids))


if __name__ == "__main__":
    unittest.main()
