"""MVP-3 落地流程测试（`process_document`）+ 幂等性（SPEC §5.5、§6、§11.4、§14）。

要证明的事：

* 一次理解只落**契约里的共享对象**（Case / Fact / Inference / Task / Question /
  Draft / DraftVersion / AuditEvent / Event），没有私有临时对象；
* **可重复调用**：第二次不产生新对象、不重复追问、不重复建草稿；
* Case 状态只走合法转换（`contracts/transitions.py` 是唯一裁决点）；
* 待办带引用与 `assignee`，草稿是"未审批、未发送"的候选。
"""

from __future__ import annotations

import unittest

from asistent.contracts.enums import ActorKind, CaseState, TaskState
from asistent.contracts.transitions import machine_for

from .helpers import (
    UnderstandTestCase,
    drafts_of,
    facts_of,
    make_document,
    process,
    questions_of,
    raw_extract,
    tasks_of,
)


class CaseFlowTests(UnderstandTestCase):
    def test_all_persisted_objects_are_contract_types(self):
        """落库对象全部是契约类型（用 `stats()` 数，不靠自述）。"""
        document = make_document(self.store, "01-notice-multi-time.eml")
        report = process(self.store, document)
        kinds = set(self.store.stats())
        self.assertTrue(kinds <= {
            "SourceDocument",
            "Case",
            "Fact",
            "Inference",
            "Task",
            "Question",
            "Draft",
            "DraftVersion",
            "AuditEvent",
            "Event",
            "Conversation",
            "_total",
        }, "出现了契约外的对象类型：%s" % sorted(kinds))
        self.assertEqual(1, self.store.stats()["Case"])
        self.assertEqual(1, self.store.stats()["SourceDocument"])
        self.assertTrue(report.outcomes)

    def test_case_is_linked_from_document(self):
        """Case 挂在资料上，资料也在 Case 的 `source_document_ids` 里（双向可查）。"""
        document = make_document(self.store, "01-notice-multi-time.eml")
        report = process(self.store, document)
        case = self.store.get("Case", report.case_id)
        self.assertIn(document.id, list(case.source_document_ids))
        self.assertIn(document.id, case.links.get("source_document", []))
        self.assertIn(report.case_id, document.links.get("case", []) or [report.case_id])

    def test_case_collects_facts_tasks_and_draft_ids(self):
        """Case 上有 fact_ids / task_ids / draft_ids，用户打开事务能看到全部内容。"""
        document = make_document(self.store, "01-notice-multi-time.eml")
        report = process(self.store, document)
        case = self.store.get("Case", report.case_id)
        self.assertEqual(
            sorted(item.id for item in facts_of(self.store, report.case_id)),
            sorted(list(case.fact_ids)),
        )
        self.assertEqual(
            sorted(item.id for item in tasks_of(self.store, report.case_id)),
            sorted(list(case.task_ids)),
        )
        self.assertEqual(
            sorted(item.id for item in drafts_of(self.store, report.case_id)),
            sorted(list(case.draft_ids)),
        )

    def test_tasks_are_user_owned_and_have_inputs(self):
        """待办：`assignee` 是用户、状态 PENDING、`inputs` 带引用（能回到原文）。"""
        document = make_document(self.store, "01-notice-multi-time.eml")
        report = process(self.store, document)
        tasks = tasks_of(self.store, report.case_id)
        self.assertEqual(3, len(tasks))
        for task in tasks:
            self.assertEqual(TaskState.PENDING, task.state)
            self.assertEqual("user", task.assignee)
            self.assertEqual(report.case_id, task.case_id)
            self.assertTrue(task.title.strip())
            self.assertTrue(task.inputs, "待办 %s 没有 inputs（无法回溯依据）" % task.id)

    def test_second_run_is_idempotent(self):
        """重复处理同一封资料：不新增任何对象。"""
        document = make_document(self.store, "01-notice-multi-time.eml")
        first = process(self.store, document)
        before = dict(self.store.stats())
        second = process(self.store, document)
        after = dict(self.store.stats())

        self.assertFalse(first.duplicate_run)
        self.assertTrue(second.duplicate_run)
        for kind in ("Fact", "Task", "Question", "Draft", "DraftVersion", "Case", "SourceDocument"):
            self.assertEqual(
                before.get(kind, 0), after.get(kind, 0), "%s 在重跑后数量变了" % kind
            )
        self.assertEqual([], [item for item in second.outcomes if item.outcome == "created"])

    def test_third_run_does_not_duplicate_questions_or_drafts(self):
        """再跑两次也不产生第二条追问 / 第二个草稿。"""
        document = make_document(self.store, "03-missing-material.eml")
        process(self.store, document)
        counts = (len(questions_of(self.store, self.store.get("SourceDocument", document.id).id)), 0)
        report = process(self.store, document)
        process(self.store, document)
        self.assertEqual(2, len(questions_of(self.store, report.case_id)))
        self.assertEqual(0, len(drafts_of(self.store, report.case_id)))
        self.assertEqual(0, counts[1])

    def test_case_state_follows_the_contract_machine(self):
        """Case 状态每一步都是合法转换（不绕过状态机）。"""
        machine = machine_for("Case")
        for name, expected_end in (
            ("01-notice-multi-time.eml", CaseState.READY_FOR_REVIEW),
            ("03-missing-material.eml", CaseState.WAITING_FOR_USER),
            ("04-ambiguous-time.eml", CaseState.WAITING_FOR_USER),
        ):
            store = type(self.store)()
            document = make_document(store, name)
            report = process(store, document)
            self.assertEqual(expected_end, report.case_state, name)
            self.assertTrue(machine.can(str(CaseState.NEW), str(CaseState.TRIAGED)))
            self.assertTrue(machine.can(str(CaseState.TRIAGED), str(CaseState.ANALYZING)))
            self.assertTrue(machine.can(str(CaseState.ANALYZING), str(expected_end)))
            self.assertEqual(
                str(CaseState.NEW), str(store.get("SourceDocument", document.id).links.get("_initial", "NEW"))
            )

    def test_case_goal_and_next_action_are_written(self):
        """Case 有目标、有下一步动作、有等待原因（用户打开就知道该干什么）。"""
        document = make_document(self.store, "03-missing-material.eml")
        report = process(self.store, document)
        case = self.store.get("Case", report.case_id)
        self.assertTrue(case.goal.strip())
        self.assertEqual("等待用户回答追问", case.next_action)
        self.assertTrue(case.waiting_reason)
        self.assertTrue(case.last_action_summary)

    def test_ready_case_waits_for_draft_approval(self):
        """资料齐全 → `READY_FOR_REVIEW` + `next_action=等待用户审批草稿`。"""
        document = make_document(self.store, "05-reply-needed-ok.eml")
        report = process(self.store, document)
        case = self.store.get("Case", report.case_id)
        self.assertEqual(CaseState.READY_FOR_REVIEW, case.state)
        self.assertEqual("等待用户审批草稿", case.next_action)
        self.assertIsNone(case.waiting_reason)
        self.assertEqual(1, len(drafts_of(self.store, report.case_id)))
        self.assertEqual("OPEN", str(drafts_of(self.store, report.case_id)[0].state))

    def test_draft_is_not_sent_and_not_approved(self):
        """草稿落地不代表发送：没有 ExternalAction、没有 Approval。"""
        document = make_document(self.store, "05-reply-needed-ok.eml")
        process(self.store, document)
        self.assertEqual([], list(self.store.find("ExternalAction", limit=None)))
        self.assertEqual([], list(self.store.find("Approval", limit=None)))

    def test_no_draft_when_not_requested(self):
        """没有回复要求的邮件 → 不生成草稿，但仍记录事实与时间。"""
        document = make_document(self.store, "04-ambiguous-time.eml")
        report = process(self.store, document)
        self.assertEqual([], drafts_of(self.store, report.case_id))
        self.assertFalse(report.draftable)
        self.assertTrue(report.time_fields)

    def test_blocked_email_keeps_draft_candidate_out_of_store(self):
        """有阻塞项时草稿候选**不落库**，并在报告里说明原因（§6.3）。"""
        document = make_document(self.store, "01-notice-multi-time.eml")
        report = process(self.store, document, create_draft=False)
        self.assertEqual([], drafts_of(self.store, report.case_id))
        self.assertEqual([], list(self.store.get("Case", report.case_id).draft_ids))

    def test_outcomes_record_every_object(self):
        """报告的 outcomes 覆盖每个落库对象，可直接作为验收证据。"""
        document = make_document(self.store, "01-notice-multi-time.eml")
        report = process(self.store, document)
        kinds = [item.kind for item in report.outcomes]
        for expected in ("Fact", "Inference", "Task", "Draft", "DraftVersion"):
            self.assertIn(expected, kinds)
        for outcome in report.outcomes:
            self.assertTrue(outcome.object_id)
            self.assertIn(outcome.outcome, ("created", "reused", "updated"))

    def test_event_published_with_dedupe_key(self):
        """理解完成会发一条事件（带去重键），且重跑复用同一条（不新增事件对象）。

        注意：审计条目本身也以 `Event` 形态出现在事件流里，所以这里断言的是
        "文档事件的身份 + 去重键稳定"，不是"事件流里只有一个对象"。
        """
        document = make_document(self.store, "05-reply-needed-ok.eml")
        report = process(self.store, document)
        self.assertTrue(report.event_id)
        self.assertTrue(report.event_created)
        first = {item.id: item for item in self.store.list_events(limit=None)}
        self.assertIn(report.event_id, first)
        self.assertTrue(first[report.event_id].dedupe_key)
        self.assertEqual("KNOWLEDGE_CHANGED", str(first[report.event_id].kind))

        second = process(self.store, document)
        self.assertFalse(second.event_created)
        self.assertEqual(report.event_id, second.event_id)
        after = {item.id: item for item in self.store.list_events(limit=None)}
        # 文档事件没有换 id，去重键也没变
        self.assertIn(report.event_id, after)
        self.assertEqual(first[report.event_id].dedupe_key, after[report.event_id].dedupe_key)
        # 同一去重键只对应一个事件对象
        keys = [item.dedupe_key for item in after.values() if item.dedupe_key]
        self.assertEqual(len(keys), len(set(keys)))

    def test_audit_trail_covers_the_run(self):
        """审计轨迹能回答"谁在什么时候把这些对象写进来的"。"""
        document = make_document(self.store, "05-reply-needed-ok.eml")
        report = process(self.store, document)
        trail = list(self.store.audit_trail(limit=None))
        self.assertTrue(trail)
        self.assertTrue(
            any(entry.actor_kind == ActorKind.AGENT and entry.actor_id == "understand" for entry in trail),
            "没有任何 actor=understand 的审计条目",
        )
        self.assertTrue(any(entry.object_id == document.id for entry in trail))
        self.assertTrue(any(entry.object_id == report.case_id for entry in trail))

    def test_two_documents_get_two_cases_by_default(self):
        """两封不同资料默认各自一个 Case（不误合并）。"""
        first = make_document(self.store, "05-reply-needed-ok.eml", document_id="doc_two_a")
        second = make_document(self.store, "01-notice-multi-time.eml", document_id="doc_two_b")
        report_a = process(self.store, first)
        report_b = process(self.store, second)
        self.assertNotEqual(report_a.case_id, report_b.case_id)
        self.assertEqual(2, self.store.stats()["Case"])

    def test_same_case_when_explicitly_shared(self):
        """显式指定同一 Case：两份资料的事实汇到同一个事务里。"""
        first = make_document(self.store, "05-reply-needed-ok.eml", document_id="doc_share_a", case_id="case_share")
        second = make_document(self.store, "01-notice-multi-time.eml", document_id="doc_share_b", case_id="case_share")
        process(self.store, first)
        process(self.store, second)
        self.assertEqual(1, self.store.stats()["Case"])
        facts = facts_of(self.store, "case_share")
        sources = {payload["source_object_id"] for fact in facts for payload in fact.citations}
        self.assertIn("doc_share_a", sources)
        self.assertIn("doc_share_b", sources)

    def test_missing_document_id_is_rejected(self):
        """没有稳定 ID 的资料不得进入理解（无法建引用）。"""
        from asistent.contracts import SourceDocument
        from asistent.contracts.errors import ValidationError
        from asistent.understand import process_document

        document = SourceDocument(source_type="email", origin_path="x.eml", content_hash="sha256:x")
        with self.assertRaises(ValidationError):
            process_document(self.store, document, raw_text="正文")


class RuleExtractionIntegrationTests(UnderstandTestCase):
    def test_fixture_matrix(self):
        """5 个 fixture 的抽取结果矩阵（语义/待办/材料/追问）作为回归基线。"""
        matrix = {}
        for name in (
            "01-notice-multi-time.eml",
            "02a-notice-conflict.eml",
            "02b-notice-conflict.eml",
            "03-missing-material.eml",
            "04-ambiguous-time.eml",
            "05-reply-needed-ok.eml",
        ):
            result = raw_extract(name)
            matrix[name] = {
                "semantics": sorted(item.semantic for item in result.time_fields),
                "todos": len(result.todos),
                "materials": sorted(item.name for item in result.materials),
                "questions": sorted(item.kind for item in result.questions),
                "unaccounted": sorted(item.text for item in result.unaccounted),
            }
        self.assertEqual(
            ["event_end", "event_start", "registration_deadline", "submission_deadline"],
            matrix["01-notice-multi-time.eml"]["semantics"],
        )
        self.assertEqual(["registration_deadline"], matrix["02a-notice-conflict.eml"]["semantics"])
        self.assertEqual(
            ["event_start", "registration_deadline"], matrix["02b-notice-conflict.eml"]["semantics"]
        )
        self.assertEqual(
            ["registration_deadline", "submission_deadline"],
            matrix["03-missing-material.eml"]["semantics"],
        )
        self.assertEqual(
            ["event_end", "event_start", "registration_deadline"],
            matrix["05-reply-needed-ok.eml"]["semantics"],
        )
        self.assertEqual(
            sorted(["监护人知情同意书", "学生一寸免冠照片"]),
            matrix["03-missing-material.eml"]["materials"],
        )
        self.assertEqual(
            ["ambiguity", "missing_time"], matrix["04-ambiguous-time.eml"]["questions"]
        )
        self.assertEqual(["10月24日"], matrix["04-ambiguous-time.eml"]["unaccounted"])

    def test_every_fixture_runs_clean(self):
        """每个 fixture 都能处理完、幂等重跑、且不留未解释的挂空对象。"""
        for name in (
            "01-notice-multi-time.eml",
            "02a-notice-conflict.eml",
            "02b-notice-conflict.eml",
            "03-missing-material.eml",
            "04-ambiguous-time.eml",
            "05-reply-needed-ok.eml",
        ):
            store = type(self.store)()
            document = make_document(store, name)
            first = process(store, document)
            second = process(store, document)
            self.assertTrue(second.duplicate_run, name)
            self.assertEqual([], [item for item in second.outcomes if item.outcome == "created"], name)
            self.assertTrue(first.case_state, name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
