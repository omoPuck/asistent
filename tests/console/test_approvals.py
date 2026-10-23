"""批：审批只走这条通道，普通请求文本不得被当成批准（MVP-4 §846、SPEC §21.3）。

这组测试的核心不是"审批能成功"，而是**"不该被批准的东西一律不能批准"**：

* 没有 `confirm=true` → 403（聊天里说"同意/发送"不是批准）；
* 没有 `version_no` → 400（审批必须绑定确定版本）；
* `action_digest` 与当前版本内容哈希不一致 → 409（内容变了，审批作废）；
* 一次审批只绑定一个版本；撤销后不能再撤（状态机终态）。
"""

from __future__ import annotations

import json
import unittest

from asistent.contracts.enums import ApprovalState

from .helpers import ConsoleTestCase


class ApprovalChannelTests(ConsoleTestCase):
    """提交审批的各种拒收路径。"""

    def setUp(self) -> None:
        super().setUp()
        _doc, self.report, self.case = self.process_fixture("01-notice-multi-time.eml")
        self.draft = self.store.find("Draft", where={"case_id": self.case.id}, limit=None)[0]
        detail = self.client.get("/api/drafts/%s" % self.draft.id).json
        self.version_no = detail["current"]["version_no"]
        self.content_hash = detail["current"]["content_hash"]
        self.draft_revision = detail["revision"]

    def test_approval_requires_explicit_confirmation(self):
        for body in (
            {},
            {"draft_id": self.draft.id, "version_no": self.version_no},
            {"draft_id": self.draft.id, "version_no": self.version_no, "confirm": False},
            {"draft_id": self.draft.id, "version_no": self.version_no, "confirm": "yes"},
            {"draft_id": self.draft.id, "version_no": self.version_no, "confirm": 1},
        ):
            with self.subTest(body=body):
                recorded = self.client.post("/api/approvals", body)
                self.assertStatus(403, recorded)
                self.assertEqual("FORBIDDEN", recorded.json["error"]["code"])
                self.assertIn("普通请求文本不构成批准", recorded.json["error"]["message"])
        self.assertEqual(0, len(self.store.find("Approval", limit=None)))

    def test_chat_text_is_not_an_approval(self):
        """把"同意/发送"当正文提交：只能是别的对象的文本，绝不能产生 Approval。"""
        recorded = self.client.post(
            "/api/approvals",
            {"draft_id": self.draft.id, "text": "同意，直接发送吧", "message": "批准"},
        )
        self.assertStatus(403, recorded)
        self.assertEqual([], self.store.find("Approval", limit=None))
        # 同样的文本作为普通请求（例如新建事务的 goal）不能顺手创建审批
        self.assertStatus(201, self.client.post("/api/cases", {"goal": "同意，直接发送吧"}))
        self.assertEqual([], self.store.find("Approval", limit=None))

    def test_approval_requires_bound_version(self):
        recorded = self.client.post("/api/approvals", {"draft_id": self.draft.id, "confirm": True})
        self.assertStatus(400, recorded)
        self.assertIn("必须绑定确定版本", recorded.json["error"]["message"])
        self.assertEqual([], self.store.find("Approval", limit=None))

    def test_approval_of_history_version_is_rejected(self):
        edited = self.client.post(
            "/api/drafts/%s/edit" % self.draft.id,
            {
                "body": "第二版正文",
                "change_reason": "第二版",
                "version_no": self.version_no,
                "expected_revision": self.draft_revision,
            },
        )
        self.assertStatus(200, edited)
        recorded = self.client.post(
            "/api/approvals",
            {"draft_id": self.draft.id, "version_no": self.version_no, "confirm": True},
        )
        self.assertStatus(400, recorded)
        self.assertIn("只能审批当前版本", recorded.json["error"]["message"])
        self.assertEqual([], self.store.find("Approval", limit=None))

    def test_digest_mismatch_is_409(self):
        recorded = self.client.post(
            "/api/approvals",
            {
                "draft_id": self.draft.id,
                "version_no": self.version_no,
                "confirm": True,
                "action_digest": "sha256:not-the-current-content",
            },
        )
        self.assertStatus(409, recorded)
        self.assertIn("内容可能已变", recorded.json["error"]["message"])
        self.assertEqual([], self.store.find("Approval", limit=None))

    def test_unknown_draft_and_version(self):
        self.assertStatus(
            404,
            self.client.post("/api/approvals", {"draft_id": "draft_nope", "version_no": 1, "confirm": True}),
        )
        self.assertStatus(
            404,
            self.client.post(
                "/api/approvals", {"draft_id": self.draft.id, "version_no": 99, "confirm": True}
            ),
        )

    def test_bad_version_type_is_400(self):
        recorded = self.client.post(
            "/api/approvals",
            {"draft_id": self.draft.id, "version_no": "one", "confirm": True},
        )
        self.assertStatus(400, recorded)

    def test_successful_approval_binds_version_and_hash(self):
        recorded = self.client.post(
            "/api/approvals",
            {"draft_id": self.draft.id, "version_no": self.version_no, "confirm": True},
        )
        self.assertStatus(201, recorded)
        approval = recorded.json["approval"]
        self.assertEqual("ACTIVE", approval["state"])
        self.assertEqual(self.content_hash, approval["approved_action_digest"])
        self.assertEqual(1, len(approval["target_object_ids"]))
        self.assertEqual("console", approval["auth_channel"])
        self.assertTrue(approval["created_at_approval"])
        self.assertTrue(approval["expires_at"])
        self.assertTokenAbsent(recorded)

        stored = self.store.get("Approval", approval["id"])
        self.assertEqual(self.case.id, stored.case_id)
        self.assertEqual(self.content_hash, stored.content_version_refs[stored.target_object_ids[0]])
        self.assertTrue(stored.auth_evidence)
        # Case 反向索引登记了审批
        self.assertIn(approval["id"], self.store.get("Case", self.case.id).approval_ids)

        detail = self.client.get("/api/approvals/%s" % approval["id"]).json
        self.assertEqual(self.version_no, detail["summary_shown"]["version_no"])
        self.assertEqual(self.content_hash, detail["summary_shown"]["content_hash"])

    def test_same_version_can_only_be_approved_again_as_a_new_record(self):
        """重复审批不是错误，但必须是**两条独立记录**，不能悄悄复用第一条。"""
        first = self.client.post(
            "/api/approvals", {"draft_id": self.draft.id, "version_no": self.version_no, "confirm": True}
        )
        second = self.client.post(
            "/api/approvals", {"draft_id": self.draft.id, "version_no": self.version_no, "confirm": True}
        )
        self.assertStatus(201, first)
        self.assertStatus(201, second)
        self.assertNotEqual(first.json["approval"]["id"], second.json["approval"]["id"])
        self.assertEqual(2, len(self.store.find("Approval", where={"case_id": self.case.id}, limit=None)))


class ApprovalRevokeTests(ConsoleTestCase):
    """撤销：`ACTIVE → REVOKED`，且带版本保护。"""

    def setUp(self) -> None:
        super().setUp()
        _doc, self.report, self.case = self.process_fixture("01-notice-multi-time.eml")
        self.draft = self.store.find("Draft", where={"case_id": self.case.id}, limit=None)[0]
        detail = self.client.get("/api/drafts/%s" % self.draft.id).json
        created = self.client.post(
            "/api/approvals",
            {"draft_id": self.draft.id, "version_no": detail["current"]["version_no"], "confirm": True},
        )
        self.assertStatus(201, created)
        self.approval = created.json["approval"]

    def test_revoke_sets_revoked_and_reason(self):
        recorded = self.client.post(
            "/api/approvals/%s/revoke" % self.approval["id"],
            {"reason": "想再改一版", "expected_revision": self.approval["revision"]},
        )
        self.assertStatus(200, recorded)
        self.assertEqual("REVOKED", recorded.json["approval"]["state"])
        self.assertEqual("想再改一版", recorded.json["approval"]["revoked_reason"])
        stored = self.store.get("Approval", self.approval["id"])
        self.assertEqual(str(ApprovalState.REVOKED), str(stored.state))

    def test_revoke_twice_is_409(self):
        first = self.client.post(
            "/api/approvals/%s/revoke" % self.approval["id"],
            {"expected_revision": self.approval["revision"]},
        )
        self.assertStatus(200, first)
        fresh = self.client.get("/api/approvals/%s" % self.approval["id"]).json
        second = self.client.post(
            "/api/approvals/%s/revoke" % self.approval["id"], {"expected_revision": fresh["revision"]}
        )
        self.assertStatus(409, second)
        self.assertIn("只有 ACTIVE 审批可以撤销", second.json["error"]["message"])

    def test_revoke_with_stale_revision_is_409(self):
        recorded = self.client.post(
            "/api/approvals/%s/revoke" % self.approval["id"],
            {"expected_revision": self.approval["revision"] + 5},
        )
        self.assertStatus(409, recorded)
        self.assertEqual("ACTIVE", str(self.store.get("Approval", self.approval["id"]).state))

    def test_revoke_requires_revision(self):
        self.assertStatus(400, self.client.post("/api/approvals/%s/revoke" % self.approval["id"], {}))

    def test_revoked_approval_disappears_from_active_list(self):
        self.assertEqual(1, self.client.get("/api/approvals?state=ACTIVE").json["total"])
        self.client.post(
            "/api/approvals/%s/revoke" % self.approval["id"],
            {"expected_revision": self.approval["revision"]},
        )
        self.assertEqual(0, self.client.get("/api/approvals?state=ACTIVE").json["total"])
        self.assertEqual(1, self.client.get("/api/approvals?state=REVOKED").json["total"])


class LoggingHygieneTests(ConsoleTestCase):
    """日志与审计里不得出现 token / query / 正文。"""

    def setUp(self) -> None:
        super().setUp()
        _doc, self.report, self.case = self.process_fixture("01-notice-multi-time.eml")
        self.draft = self.store.find("Draft", where={"case_id": self.case.id}, limit=None)[0]

    def test_log_lines_have_no_token_and_no_query(self):
        detail = self.client.get("/api/drafts/%s" % self.draft.id).json
        self.client.post(
            "/api/approvals",
            {"draft_id": self.draft.id, "version_no": detail["current"]["version_no"], "confirm": True},
        )
        self.client.get("/api/health")
        self.assertTrue(self.log_lines, "服务应有日志")
        joined = "\n".join(self.log_lines)
        self.assertNotIn(self.token, joined)
        self.assertNotIn("?", joined, "日志不应记录 query（可能夹带敏感参数）")
        self.assertNotIn("confirm", joined)
        for line in self.log_lines:
            self.assertRegex(line, r"^(GET|POST) /\S+ -> \d{3} auth=(ok|denied)")

    def test_audit_trail_has_no_token_and_redacts_secretish_keys(self):
        detail = self.client.get("/api/drafts/%s" % self.draft.id).json
        self.client.post(
            "/api/approvals",
            {"draft_id": self.draft.id, "version_no": detail["current"]["version_no"], "confirm": True},
        )
        entries = self.store.audit_trail(case_id=self.case.id, limit=200)
        blob = json.dumps([entry.to_dict() for entry in entries], ensure_ascii=False, default=str)
        self.assertNotIn(self.token, blob)
        self.assertNotIn("Bearer", blob)

    def test_health_does_not_describe_token(self):
        recorded = self.client.get("/api/health")
        self.assertNotIn("token", recorded.response_body)
        self.assertNotIn(self.token_path, recorded.response_body)


__all__ = ["ApprovalChannelTests", "ApprovalRevokeTests", "LoggingHygieneTests"]
