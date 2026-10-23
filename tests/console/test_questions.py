"""答：列出待回答追问、提交 Answer、依赖任务重新评估（MVP-4 §845）。

关键断言：

* Answer 落进**正确的 Case**（不是"最后一个 Case"，不是"当前会话"）；
* 依赖任务被重新评估（`WAITING_USER`/`BLOCKED` → `READY`），并且这次变化在**审计**里能看到；
* 旧 `expected_revision` 提交 → 409，且**不产生**第二条 Answer；
* 回答文本永远只是回答，不会被当成审批。
"""

from __future__ import annotations

import unittest

from asistent.contracts.enums import TaskState
from asistent.contracts.objects import Task

from .helpers import ConsoleTestCase


class QuestionListTests(ConsoleTestCase):
    """待回答追问的列表与详情。"""

    def setUp(self) -> None:
        super().setUp()
        _doc, self.report, self.case = self.process_fixture("03-missing-material.eml")
        self.questions = self.store.find("Question", where={"case_id": self.case.id}, limit=None)
        self.assertEqual(2, len(self.questions), "夹具应产生 2 条追问")

    def test_default_list_only_open_questions(self):
        recorded = self.client.get("/api/questions")
        self.assertStatus(200, recorded)
        self.assertEqual(2, recorded.json["total"])
        for row in recorded.json["questions"]:
            self.assertEqual("OPEN", row["state"])
            self.assertEqual(self.case.id, row["case_id"])
            self.assertTrue(row["question"])
            self.assertTrue(row["required_format"])

    def test_list_filter_includes_answered_after_reply(self):
        question = self.questions[0]
        self.client.post(
            "/api/questions/%s/answer" % question.id,
            {"answer_text": "已准备好", "expected_revision": question.revision},
        )
        self.assertEqual(1, self.client.get("/api/questions").json["total"])
        self.assertEqual(1, self.client.get("/api/questions?state=ANSWERED").json["total"])
        self.assertEqual(2, self.client.get("/api/questions?state=").json["total"])

    def test_question_detail_includes_case_and_answers(self):
        recorded = self.client.get("/api/questions/%s" % self.questions[0].id)
        self.assertStatus(200, recorded)
        self.assertEqual(self.case.id, recorded.json["case"]["id"])
        self.assertEqual([], recorded.json["answers"])
        self.assertEqual("OPEN", recorded.json["state"])

    def test_missing_question_is_404(self):
        self.assertStatus(404, self.client.get("/api/questions/q_nope"))

    def test_submitting_an_answer_puts_it_in_the_right_case(self):
        question = self.questions[0]
        recorded = self.client.post(
            "/api/questions/%s/answer" % question.id,
            {"answer_text": "已准备好，周二前提交", "expected_revision": question.revision},
        )
        self.assertStatus(200, recorded)
        payload = recorded.json
        self.assertEqual(self.case.id, payload["answer"]["case_id"])
        self.assertEqual(question.id, payload["answer"]["question_id"])
        self.assertEqual("已准备好，周二前提交", payload["answer"]["answer_text"])
        self.assertEqual("ANSWERED", payload["question"]["state"])
        self.assertEqual(payload["answer"]["id"], payload["question"]["answer_id"])

        # 落盘：store 里查得到，而且 Case 反向索引登记了
        stored = self.store.get("Answer", payload["answer"]["id"])
        self.assertEqual(self.case.id, stored.case_id)
        self.assertIn(question.id, self.store.get("Case", self.case.id).question_ids)
        self.assertTokenAbsent(recorded)

    def test_answer_revision_conflict_is_409_and_writes_nothing(self):
        question = self.questions[0]
        stale = question.revision
        first = self.client.post(
            "/api/questions/%s/answer" % question.id,
            {"answer_text": "第一次回答", "expected_revision": stale},
        )
        self.assertStatus(200, first)
        answers_before = len(self.store.find("Answer", limit=None))

        second = self.client.post(
            "/api/questions/%s/answer" % question.id,
            {"answer_text": "拿着旧 revision 再来一次", "expected_revision": stale},
        )
        self.assertStatus(409, second)
        self.assertIn("revision", second.json["error"]["message"])
        self.assertEqual(stale, second.json["error"]["detail"]["expected_revision"])
        self.assertEqual(answers_before, len(self.store.find("Answer", limit=None)), "冲突时不得写入 Answer")
        self.assertEqual("第一次回答", self.store.get("Answer", first.json["answer"]["id"]).answer_text)

    def test_second_answer_on_answered_question_is_409(self):
        question = self.questions[0]
        self.client.post(
            "/api/questions/%s/answer" % question.id,
            {"answer_text": "A", "expected_revision": question.revision},
        )
        fresh = self.store.get("Question", question.id)
        recorded = self.client.post(
            "/api/questions/%s/answer" % question.id,
            {"answer_text": "B", "expected_revision": fresh.revision},
        )
        self.assertStatus(409, recorded)
        self.assertIn("不是待回答状态", recorded.json["error"]["message"])

    def test_answer_requires_revision_and_text(self):
        question = self.questions[0]
        self.assertStatus(400, self.client.post("/api/questions/%s/answer" % question.id, {"answer_text": "hi"}))
        self.assertStatus(
            400,
            self.client.post(
                "/api/questions/%s/answer" % question.id, {"expected_revision": question.revision}
            ),
        )
        self.assertStatus(
            400,
            self.client.post(
                "/api/questions/%s/answer" % question.id,
                {"answer_text": "  ", "expected_revision": question.revision},
            ),
        )

    def test_bad_revision_type_is_400(self):
        question = self.questions[0]
        recorded = self.client.post(
            "/api/questions/%s/answer" % question.id,
            {"answer_text": "hi", "expected_revision": "not-a-number"},
        )
        self.assertStatus(400, recorded)

    def test_answering_one_question_does_not_touch_the_other(self):
        first, second = self.questions
        self.client.post(
            "/api/questions/%s/answer" % first.id,
            {"answer_text": "已准备好", "expected_revision": first.revision},
        )
        self.assertEqual("OPEN", str(self.store.get("Question", second.id).state))
        case = self.store.get("Case", self.case.id)
        self.assertEqual("WAITING_FOR_USER", str(case.state))
        self.assertIn("1 条追问", case.waiting_reason)

    def test_answering_the_last_question_replaces_the_waiting_reason(self):
        """答完最后一条追问后，"等待用户"的原因必须消失（否则手机上永远显示在等自己）。"""
        for question in self.questions:
            fresh = self.store.get("Question", question.id)
            recorded = self.client.post(
                "/api/questions/%s/answer" % question.id,
                {"answer_text": "已准备好", "expected_revision": fresh.revision},
            )
            self.assertStatus(200, recorded)
        case = self.store.get("Case", self.case.id)
        self.assertNotIn("等待用户回答", case.waiting_reason)
        self.assertIn("重新分析", case.waiting_reason)
        self.assertEqual("重新分析事务", case.next_action)

    def test_answering_last_question_without_a_draft_stays_in_a_legal_state(self):
        """没有草稿可审批时不得伪造"等待用户"：只改等待原因，状态留给 Agent 走 ANALYZING。"""
        _doc, _report, case = self.process_fixture("04-ambiguous-time.eml")
        questions = self.store.find("Question", where={"case_id": case.id}, limit=None)
        for question in questions:
            fresh = self.store.get("Question", question.id)
            self.assertStatus(
                200,
                self.client.post(
                    "/api/questions/%s/answer" % question.id,
                    {"answer_text": "10月24日 18:00", "expected_revision": fresh.revision},
                ),
            )
        stored = self.store.get("Case", case.id)
        self.assertIn("重新分析", stored.next_action)
        self.assertIn("等待 Agent", stored.waiting_reason)
        from asistent.contracts.transitions import machine_for

        # 状态必须仍然合法（要么保持 WAITING_FOR_USER，要么已经走到允许的下一站）
        self.assertIn(
            str(stored.state),
            ["WAITING_FOR_USER"] + machine_for("Case").next_states("WAITING_FOR_USER"),
        )

    def test_answering_one_of_two_updates_waiting_reason_count(self):
        question = self.questions[0]
        self.assertStatus(
            200,
            self.client.post(
                "/api/questions/%s/answer" % question.id,
                {"answer_text": "已准备好", "expected_revision": question.revision},
            ),
        )
        case = self.store.get("Case", self.case.id)
        self.assertIn("1 条追问", case.waiting_reason)
        self.assertEqual("WAITING_FOR_USER", str(case.state))


class DependentTaskReevaluationTests(ConsoleTestCase):
    """Answers 触发依赖任务重新评估（MVP-4 §845 后半句）。"""

    def setUp(self) -> None:
        super().setUp()
        _doc, self.report, self.case = self.process_fixture("03-missing-material.eml")
        self.question = self.store.find("Question", where={"case_id": self.case.id}, limit=None)[0]
        # 造一个"正在等这条追问"的任务：`preconditions` 里提到该追问 ID
        self.task = self.store.put(
            Task(
                id="task_waiting_on_question",
                case_id=self.case.id,
                kind="ask_user",
                title="等用户确认材料是否齐备",
                state=TaskState.WAITING_USER,
                inputs={"question_id": self.question.id},
                preconditions=["question:%s" % self.question.id],
                links={"case": [self.case.id], "question": [self.question.id]},
            )
        )

    def test_waiting_task_becomes_ready_and_is_audited(self):
        recorded = self.client.post(
            "/api/questions/%s/answer" % self.question.id,
            {"answer_text": "已准备好", "expected_revision": self.question.revision},
        )
        self.assertStatus(200, recorded)
        rows = {row["task_id"]: row for row in recorded.json["reevaluated_tasks"]}
        self.assertIn(self.task.id, rows)
        self.assertTrue(rows[self.task.id]["changed"])
        self.assertEqual("WAITING_USER", rows[self.task.id]["from"])
        self.assertEqual("READY", rows[self.task.id]["to"])
        self.assertEqual("READY", str(self.store.get("Task", self.task.id).state))

        trail = self.store.audit_trail(case_id=self.case.id, limit=50)
        transitions = [entry for entry in trail if entry.object_id == self.task.id and entry.action == "TRANSITION"]
        self.assertTrue(transitions, "状态转换必须留审计")
        self.assertEqual("console", transitions[-1].actor_id)

    def test_blocked_task_is_reevaluated_to_ready(self):
        task = self.store.get("Task", self.task.id)
        task.state = TaskState.BLOCKED
        self.store.put(task, expected_revision=task.revision, actor_kind="SYSTEM", actor_id="test", reason="测试用：阻塞")
        recorded = self.client.post(
            "/api/questions/%s/answer" % self.question.id,
            {"answer_text": "已准备好", "expected_revision": self.question.revision},
        )
        self.assertStatus(200, recorded)
        self.assertEqual("READY", str(self.store.get("Task", self.task.id).state))

    def test_unrelated_task_is_left_alone(self):
        other = self.store.put(
            Task(
                id="task_unrelated",
                case_id=self.case.id,
                kind="draft",
                title="与追问无关的任务",
                state=TaskState.WAITING_USER,
                links={"case": [self.case.id]},
            )
        )
        recorded = self.client.post(
            "/api/questions/%s/answer" % self.question.id,
            {"answer_text": "已准备好", "expected_revision": self.question.revision},
        )
        self.assertStatus(200, recorded)
        self.assertEqual("WAITING_USER", str(self.store.get("Task", other.id).state))
        self.assertNotIn(other.id, [row["task_id"] for row in recorded.json["reevaluated_tasks"]])

    def test_case_agent_state_is_not_faked_after_reevaluation(self):
        """重新评估不能顺手把 Case 说成"已完成"：状态机之外的自造转换是禁止的。"""
        self.client.post(
            "/api/questions/%s/answer" % self.question.id,
            {"answer_text": "已准备好", "expected_revision": self.question.revision},
        )
        payload = self.client.get("/api/cases/%s" % self.case.id).json
        self.assertIn(payload["state"], ("WAITING_FOR_USER", "READY_FOR_REVIEW", "TRIAGED", "ANALYZING"))
        self.assertNotIn("SUCCEEDED", payload["state"])


__all__ = ["DependentTaskReevaluationTests", "QuestionListTests"]
