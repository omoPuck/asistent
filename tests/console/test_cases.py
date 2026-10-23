"""Case 列表与详情：手机上"看"的部分（SPEC §9.2 第 1-3 条、MVP-4 §844）。"""

from __future__ import annotations

import unittest

from .helpers import ConsoleTestCase, store_is_sqlite


class CaseListTests(ConsoleTestCase):
    """列表：状态、等待原因、下一步、最近动作。"""

    def setUp(self) -> None:
        super().setUp()
        _doc, self.report, self.case = self.process_fixture("03-missing-material.eml")

    def test_list_cases_returns_waiting_reason_and_next_step(self):
        recorded = self.client.get("/api/cases")
        self.assertStatus(200, recorded)
        self.assertEqual(1, recorded.json["total"])
        row = recorded.json["cases"][0]
        self.assertEqual(self.case.id, row["id"])
        self.assertEqual("WAITING_FOR_USER", row["state"])
        self.assertIn("待回答", row["waiting_reason"])
        self.assertTrue(row["next_action"])
        self.assertTrue(row["last_action_summary"])
        self.assertTokenAbsent(recorded)

    def test_list_cases_filters_by_state(self):
        self.assertEqual(1, self.client.get("/api/cases?state=WAITING_FOR_USER").json["total"])
        self.assertEqual(0, self.client.get("/api/cases?state=COMPLETED").json["total"])

    def test_list_cases_rejects_unknown_state(self):
        recorded = self.client.get("/api/cases?state=NOPE")
        self.assertStatus(400, recorded)
        self.assertEqual("BAD_REQUEST", recorded.json["error"]["code"])

    def test_list_cases_limit_and_bad_limit(self):
        # `total` 是"匹配总数"，`cases` 是截断后的列表 —— 两者语义不同，不要混用
        limited = self.client.get("/api/cases?limit=0")
        self.assertEqual(1, limited.json["total"])
        self.assertEqual([], limited.json["cases"])
        self.assertStatus(400, self.client.get("/api/cases?limit=abc"))

    def test_closed_case_is_hidden_unless_requested(self):
        """终态 Case 默认不出现在列表里，`include_closed=true` 才带出来。"""
        from asistent.contracts.enums import CaseState

        case = self.store.get("Case", self.case.id)
        self.store.transition(
            "Case", case.id, str(CaseState.CANCELLED), expected_revision=case.revision,
            actor_kind="USER", actor_id="test", reason="测试用：取消", trigger="USER",
        )
        self.assertEqual(0, self.client.get("/api/cases").json["total"])
        self.assertEqual(1, self.client.get("/api/cases?include_closed=true").json["total"])

    def test_get_missing_case_is_404_when_authenticated(self):
        recorded = self.client.get("/api/cases/case_nope")
        self.assertStatus(404, recorded)
        self.assertEqual("NOT_FOUND", recorded.json["error"]["code"])


class CaseDetailTests(ConsoleTestCase):
    """详情：Agent 状态、关联对象、可定位的引用。"""

    def setUp(self) -> None:
        super().setUp()
        _doc, self.report, self.case = self.process_fixture("01-notice-multi-time.eml")

    def test_detail_has_status_agent_and_modelling_sections(self):
        recorded = self.client.get("/api/cases/%s?include=documents,audit" % self.case.id)
        self.assertStatus(200, recorded)
        payload = recorded.json
        for key in ("state", "waiting_reason", "next_action", "last_action_summary", "counts"):
            self.assertIn(key, payload)
        self.assertEqual("READY_FOR_REVIEW", payload["state"])
        self.assertIn("agent", payload)
        self.assertFalse(payload["agent"]["running"])
        self.assertIn("recent_executions", payload["agent"])
        self.assertTrue(payload["tasks"])
        self.assertTrue(payload["drafts"])
        self.assertTrue(payload["facts"])
        self.assertTrue(payload["documents"])

    def test_detail_citations_are_locatable(self):
        """引用必须能定位到原文：path + locator + content_hash + supports 都在。"""
        payload = self.client.get("/api/cases/%s" % self.case.id).json
        fact = payload["facts"][0]
        self.assertTrue(fact["citations"], "事实必须带引用")
        for citation in fact["citations"]:
            for key in ("source_object_id", "source_object_type", "path", "locator", "content_hash", "supports"):
                self.assertTrue(citation.get(key), "引用缺 %s：%r" % (key, citation))
            self.assertRegex(citation["locator"], r"line:\d+")

    def test_detail_never_exposes_local_absolute_path(self):
        """本机路径字段不外泄；**只有** Citation.path 例外（它是契约里"能定位原文"的字段）。"""
        recorded = self.client.get("/api/cases/%s?include=documents" % self.case.id)
        document = recorded.json["documents"][0]
        for forbidden in ("origin_path", "path", "archive_path"):
            self.assertNotIn(forbidden, document, "原始对象的 %s 不应出现在控制台响应里" % forbidden)
        self.assertNotIn(self.tmpdir, recorded.response_body)
        # 事实引用里的 path 是契约字段（locator 的可定位依据），必须保留
        citation = recorded.json["facts"][0]["citations"][0]
        self.assertTrue(citation["path"])

    def test_detail_omits_documents_and_audit_by_default(self):
        payload = self.client.get("/api/cases/%s" % self.case.id).json
        self.assertNotIn("documents", payload)
        self.assertNotIn("audit", payload)

    def test_audit_section_shows_actor_and_reason(self):
        payload = self.client.get("/api/cases/%s?include=audit" % self.case.id).json
        self.assertTrue(payload["audit"])
        entry = payload["audit"][0]
        for key in ("at", "action", "actor_kind", "actor_id", "result", "reason"):
            self.assertIn(key, entry)
        self.assertIn("detail", entry)

    def test_agent_status_reports_running_execution(self):
        """Agent 真的在跑时，详情必须显示当前执行 ID（SPEC §844 的 "Agent 状态"）。"""
        from asistent.contracts.objects import Execution
        from asistent.contracts.enums import ExecutionState

        execution = self.store.put(
            Execution(
                id="exec_console_running",
                case_id=self.case.id,
                backend="echo",
                state=ExecutionState.RUNNING,
                input_summary="正在读原文",
                started_at=self.store.now(),
                links={"case": [self.case.id]},
            )
        )
        payload = self.client.get("/api/cases/%s" % self.case.id).json
        self.assertTrue(payload["agent"]["running"])
        self.assertEqual(execution.id, payload["agent"]["current_execution_id"])
        self.assertEqual(1, len(payload["agent"]["recent_executions"]))

    def test_case_creation_from_phone(self):
        """手机可以独立发起任务（SPEC §9.2 第 1 条）。"""
        recorded = self.client.post("/api/cases", {"goal": "去问一下下周的场地能不能改期"})
        self.assertStatus(201, recorded)
        payload = recorded.json
        self.assertTrue(payload["id"].startswith("case_"))
        self.assertEqual("NEW", payload["state"])
        stored = self.store.get("Case", payload["id"])
        self.assertEqual("去问一下下周的场地能不能改期", stored.goal)
        # 落盘了才算数：列表里能查到
        self.assertIn(payload["id"], [row["id"] for row in self.client.get("/api/cases?include_closed=true").json["cases"]])

    def test_case_creation_requires_goal(self):
        self.assertStatus(400, self.client.post("/api/cases", {}))
        self.assertStatus(400, self.client.post("/api/cases", {"goal": "   "}))
        self.assertStatus(400, self.client.post("/api/cases", {"goal": "x" * 5000}))

    def test_malformed_json_is_400_not_500(self):
        recorded = self.client.post("/api/cases", raw_body="{not json")
        self.assertStatus(400, recorded)
        self.assertEqual("BAD_REQUEST", recorded.json["error"]["code"])

    def test_empty_body_is_400(self):
        """带 Content-Length: 0 的 POST：拒绝，而不是当成空对象默默接受。"""
        recorded = self.client.call("POST", "/api/cases", token=self.token, headers={"Content-Length": "0"})
        self.assertStatus(400, recorded)
        self.assertEqual("BAD_REQUEST", recorded.json["error"]["code"])
        self.assertIn("请求体为空", recorded.json["error"]["message"])

    def test_oversized_body_is_400(self):
        recorded = self.client.post("/api/cases", {"goal": "x" * 300000})
        self.assertStatus(400, recorded)
        self.assertIn("请求体过大", recorded.json["error"]["message"])

    def test_unknown_api_route_is_404(self):
        self.assertStatus(404, self.client.get("/api/nope"))
        self.assertStatus(404, self.client.get("/api/cases/case_a/extra"))

    def test_cors_preflight_is_refused(self):
        """不开放跨域：开放 CORS 等于把 token 交给任意网页。"""
        self.assertStatus(405, self.client.call("OPTIONS", "/api/cases", token=None))

    def test_static_shell_is_served_with_token_and_blocks_traversal(self):
        index = self.client.get("/")
        self.assertStatus(200, index)
        self.assertIn("<!DOCTYPE html>", index.response_body)
        self.assertStatus(404, self.client.get("/../SPEC.md"))
        self.assertStatus(404, self.client.get("/..%2FSPEC.md"))
        self.assertStatus(404, self.client.get("/nope.txt"))

    def test_manifest_is_installable(self):
        recorded = self.client.get("/manifest.webmanifest")
        self.assertStatus(200, recorded)
        self.assertEqual("application/manifest+json", recorded.response_headers.get("Content-Type"))
        self.assertEqual("standalone", recorded.json["display"])
        self.assertTrue(recorded.json["icons"])


__all__ = ["CaseDetailTests", "CaseListTests"]
