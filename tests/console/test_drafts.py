"""改：编辑草稿 → 新 DraftVersion；旧 Approval 失效；版本差异可见（MVP-4 §846）。

这是"旧版本编辑必须被明确拒绝"的主战场：

* 用旧 `version_no` 保存 → 409，且**库里不产生新版本**（不静默覆盖）；
* 用旧 `expected_revision` 保存 → 409，同样不写；
* 成功保存 → 新版本带作者/时间/内容哈希/修改原因，历史版本一字不改；
* 有任何绑定了该草稿的 ACTIVE 审批 → 保存后必须变成 INVALIDATED。
"""

from __future__ import annotations

import unittest

from asistent.contracts.enums import ApprovalState

from .helpers import ConsoleTestCase


class DraftEditTests(ConsoleTestCase):
    """草稿详情 / 编辑 / 冲突。"""

    def setUp(self) -> None:
        super().setUp()
        _doc, self.report, self.case = self.process_fixture("01-notice-multi-time.eml")
        drafts = self.store.find("Draft", where={"case_id": self.case.id}, limit=None)
        self.assertEqual(1, len(drafts), "夹具应产生 1 个草稿")
        self.draft = drafts[0]

    def _detail(self):
        recorded = self.client.get("/api/drafts/%s" % self.draft.id)
        self.assertStatus(200, recorded)
        return recorded.json

    def test_draft_detail_shows_versions_and_current(self):
        payload = self._detail()
        self.assertEqual(1, len(payload["versions"]))
        current = payload["current"]
        self.assertEqual(1, current["version_no"])
        self.assertTrue(current["body"])
        self.assertTrue(current["content_hash"])
        self.assertEqual(current["id"], payload["current_version_id"])
        self.assertIn(self.case.id, [self.case.id])

    def test_edit_creates_new_version_with_author_and_reason(self):
        before = self._detail()
        recorded = self.client.post(
            "/api/drafts/%s/edit" % self.draft.id,
            {
                "body": "老师您好：\n我会在 10 月 20 日前确认参加。\n（手机端修改）",
                "change_reason": "补一句确认时间",
                "version_no": before["current"]["version_no"],
                "expected_revision": before["revision"],
            },
        )
        self.assertStatus(200, recorded)
        payload = recorded.json
        created = payload["created_version"]
        self.assertEqual(2, created["version_no"])
        self.assertEqual("USER", created["author_kind"])
        self.assertEqual("console", created["author_id"])
        self.assertEqual("补一句确认时间", created["change_reason"])
        self.assertTrue(created["created_at"])
        self.assertTrue(created["content_hash"])
        self.assertNotEqual(before["current"]["content_hash"], created["content_hash"])

        stored = self.store.get("DraftVersion", created["id"])
        self.assertEqual("console", stored.author_id)
        # 历史版本一个字节都不能变
        original = self.store.get("DraftVersion", before["current"]["id"])
        self.assertEqual(before["current"]["body"], original.body)
        self.assertEqual(before["current"]["content_hash"], original.content_hash)

    def test_edit_with_stale_version_no_is_409_and_writes_nothing(self):
        before = self._detail()
        first = self.client.post(
            "/api/drafts/%s/edit" % self.draft.id,
            {
                "body": "第一次修改",
                "change_reason": "第一次",
                "version_no": before["current"]["version_no"],
                "expected_revision": before["revision"],
            },
        )
        self.assertStatus(200, first)
        versions_after_first = len(self.store.find("DraftVersion", where={"draft_id": self.draft.id}, limit=None))

        # 手机还停在 v1 上再保存一次：必须被明确拒绝
        stale = self.client.post(
            "/api/drafts/%s/edit" % self.draft.id,
            {
                "body": "拿着旧版本号的第二次提交",
                "change_reason": "过期编辑",
                "version_no": before["current"]["version_no"],
                "expected_revision": before["revision"],
            },
        )
        self.assertStatus(409, stale)
        self.assertEqual("REVISION_CONFLICT", stale.json["error"]["code"])
        self.assertIn("已不是当前版本", stale.json["error"]["message"])
        self.assertIn("v1", stale.json["error"]["message"])
        self.assertEqual(
            versions_after_first,
            len(self.store.find("DraftVersion", where={"draft_id": self.draft.id}, limit=None)),
            "409 时不得写入新版本",
        )
        self.assertEqual(before["current"]["body"], self.store.get("DraftVersion", before["current"]["id"]).body)

    def test_edit_with_stale_expected_revision_is_409(self):
        before = self._detail()
        self.assertStatus(
            200,
            self.client.post(
                "/api/drafts/%s/edit" % self.draft.id,
                {
                    "body": "A",
                    "version_no": before["current"]["version_no"],
                    "expected_revision": before["revision"],
                },
            ),
        )
        detail = self._detail()
        stale = self.client.post(
            "/api/drafts/%s/edit" % self.draft.id,
            {
                "body": "B",
                "version_no": detail["current"]["version_no"],
                "expected_revision": before["revision"],  # 旧 revision
            },
        )
        self.assertStatus(409, stale)
        self.assertEqual(before["revision"], stale.json["error"]["detail"]["expected_revision"])
        self.assertEqual(detail["revision"], stale.json["error"]["detail"]["current_revision"])
        self.assertIn("请刷新", stale.json["error"]["detail"]["action"])

    def test_edit_requires_version_or_revision(self):
        """没有任何版本信息的编辑必须被拒：否则就是无保护覆盖。"""
        before = self._detail()
        no_version = self.client.post("/api/drafts/%s/edit" % self.draft.id, {"body": "x"})
        self.assertStatus(400, no_version)
        self.assertIn("没有版本就没有并发保护", no_version.json["error"]["message"])
        # 只有版本号也够了（version_no 本身就是并发保护）
        only_version = self.client.post(
            "/api/drafts/%s/edit" % self.draft.id,
            {"body": "只带 version_no 的编辑", "version_no": before["current"]["version_no"]},
        )
        self.assertStatus(200, only_version)
        # 只有 expected_revision 也够了
        detail = self._detail()
        only_revision = self.client.post(
            "/api/drafts/%s/edit" % self.draft.id,
            {"body": "只带 expected_revision 的编辑", "expected_revision": detail["revision"]},
        )
        self.assertStatus(200, only_revision)
        self.assertEqual(3, only_revision.json["created_version"]["version_no"])

    def test_edit_rejects_empty_body_and_oversized_body(self):
        before = self._detail()
        self.assertStatus(
            400,
            self.client.post(
                "/api/drafts/%s/edit" % self.draft.id,
                {"body": "   ", "version_no": 1, "expected_revision": before["revision"]},
            ),
        )
        self.assertStatus(
            400,
            self.client.post(
                "/api/drafts/%s/edit" % self.draft.id,
                {"body": "x" * 30000, "version_no": 1, "expected_revision": before["revision"]},
            ),
        )

    def test_unchanged_body_does_not_create_a_duplicate_version(self):
        before = self._detail()
        recorded = self.client.post(
            "/api/drafts/%s/edit" % self.draft.id,
            {
                "body": before["current"]["body"],
                "change_reason": "手滑点两次保存",
                "version_no": before["current"]["version_no"],
                "expected_revision": before["revision"],
            },
        )
        self.assertStatus(200, recorded)
        self.assertEqual(before["current"]["version_no"], recorded.json["created_version"]["version_no"])
        self.assertEqual(1, len(self.store.find("DraftVersion", where={"draft_id": self.draft.id}, limit=None)))

    def test_diff_is_visible_after_edit(self):
        before = self._detail()
        self.client.post(
            "/api/drafts/%s/edit" % self.draft.id,
            {
                "body": before["current"]["body"] + "\n补充一行：需要带身份证。",
                "change_reason": "补充材料",
                "version_no": before["current"]["version_no"],
                "expected_revision": before["revision"],
            },
        )
        delta = self.client.get("/api/drafts/%s/diff?from=1&to=2" % self.draft.id)
        self.assertStatus(200, delta)
        self.assertFalse(delta.json["identical"])
        self.assertTrue(delta.json["added_lines"])
        self.assertIn("身份证", delta.json["unified"])

    def test_restore_version_creates_a_new_version_not_a_rollback(self):
        before = self._detail()
        body_v1 = before["current"]["body"]
        self.client.post(
            "/api/drafts/%s/edit" % self.draft.id,
            {
                "body": "改坏了的内容",
                "change_reason": "改坏了",
                "version_no": 1,
                "expected_revision": before["revision"],
            },
        )
        detail = self._detail()
        restored = self.client.post(
            "/api/drafts/%s/restore" % self.draft.id,
            {"version_no": 1, "expected_revision": detail["revision"], "change_reason": "回到 v1 的正文"},
        )
        self.assertStatus(200, restored)
        self.assertEqual(3, restored.json["created_version"]["version_no"])
        self.assertEqual(body_v1, restored.json["created_version"]["body"])
        versions = self.store.find("DraftVersion", where={"draft_id": self.draft.id}, limit=None)
        self.assertEqual([1, 2, 3], sorted(int(item.version_no) for item in versions), "恢复也必须留下历史")

    def test_restore_unknown_version_is_404(self):
        detail = self._detail()
        self.assertStatus(
            404,
            self.client.post(
                "/api/drafts/%s/restore" % self.draft.id,
                {"version_no": 99, "expected_revision": detail["revision"]},
            ),
        )

    def test_draft_list_and_missing_draft(self):
        recorded = self.client.get("/api/drafts")
        self.assertStatus(200, recorded)
        self.assertEqual(1, recorded.json["total"])
        self.assertEqual(self.draft.id, recorded.json["drafts"][0]["id"])
        self.assertStatus(404, self.client.get("/api/drafts/draft_nope"))


class ApprovalInvalidationOnEditTests(ConsoleTestCase):
    """编辑后旧审批必须失效（SPEC §846、MVP-5 §855 的前置条件）。"""

    def setUp(self) -> None:
        super().setUp()
        _doc, self.report, self.case = self.process_fixture("01-notice-multi-time.eml")
        self.draft = self.store.find("Draft", where={"case_id": self.case.id}, limit=None)[0]
        detail = self.client.get("/api/drafts/%s" % self.draft.id).json
        self.version_no = detail["current"]["version_no"]
        self.draft_revision = detail["revision"]
        approved = self.client.post(
            "/api/approvals",
            {"draft_id": self.draft.id, "version_no": self.version_no, "confirm": True},
        )
        self.assertStatus(201, approved)
        self.approval_id = approved.json["approval"]["id"]

    def test_edit_invalidates_existing_approval(self):
        edited = self.client.post(
            "/api/drafts/%s/edit" % self.draft.id,
            {
                "body": "改了正文之后，旧审批必须失效",
                "change_reason": "内容变化",
                "version_no": self.version_no,
                "expected_revision": self.draft_revision,
            },
        )
        self.assertStatus(200, edited)
        self.assertIn(self.approval_id, edited.json["invalidated_approval_ids"])
        stored = self.store.get("Approval", self.approval_id)
        self.assertEqual(str(ApprovalState.INVALIDATED), str(stored.state))
        self.assertIn("草稿内容已改变", stored.invalidated_reason)

        listed = self.client.get("/api/approvals?state=ACTIVE").json
        self.assertEqual(0, listed["total"], "失效后不应再出现在待审批列表里")

    def test_restore_also_invalidates_existing_approval(self):
        before = self._detail_body()
        self.client.post(
            "/api/drafts/%s/edit" % self.draft.id,
            {
                "body": "第二版",
                "change_reason": "第二版",
                "version_no": self.version_no,
                "expected_revision": self.draft_revision,
            },
        )
        self.abort_approval_reason()  # 第二版会把第一版的审批作废，这里显式确认
        detail = self.client.get("/api/drafts/%s" % self.draft.id).json
        second = self.client.post(
            "/api/approvals",
            {"draft_id": self.draft.id, "version_no": detail["current"]["version_no"], "confirm": True},
        )
        self.assertStatus(201, second)
        restored = self.client.post(
            "/api/drafts/%s/restore" % self.draft.id,
            {"version_no": 1, "expected_revision": detail["revision"]},
        )
        self.assertStatus(200, restored)
        self.assertIn(second.json["approval"]["id"], restored.json["invalidated_approval_ids"])
        self.assertEqual(before, restored.json["created_version"]["body"])

    def _detail_body(self):
        detail = self.client.get("/api/drafts/%s" % self.draft.id).json
        return detail["current"]["body"]

    def abort_approval_reason(self):
        """辅助断言：第一版的审批确实已经因为编辑而失效。"""
        stored = self.store.get("Approval", self.approval_id)
        self.assertEqual(str(ApprovalState.INVALIDATED), str(stored.state))

    def test_approval_survives_when_draft_untouched(self):
        self.assertEqual(str(ApprovalState.ACTIVE), str(self.store.get("Approval", self.approval_id).state))
        listed = self.client.get("/api/approvals?state=ACTIVE").json
        self.assertEqual(1, listed["total"])


__all__ = ["ApprovalInvalidationOnEditTests", "DraftEditTests"]
