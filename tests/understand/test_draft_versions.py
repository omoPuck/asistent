"""MVP-3 草稿版本化测试（SPEC §5.5、§8.2、§8.4、§1064）。

要证明的事：草稿**只增不改**（编辑产生新版本、回复不覆盖历史）、
任意两版可比差异、恢复旧版本会**再产生一个新版本**、
审批只能绑定一个确定的版本 + 该版本的内容哈希。
"""

from __future__ import annotations

import unittest

from asistent.contracts.errors import ValidationError
from asistent.understand import (
    approval_binding,
    current_version,
    diff_versions,
    new_version,
    restore_version,
    version_by_number,
    versions_of,
)

from .helpers import UnderstandTestCase, drafts_of, make_document, process


class DraftVersioningTests(UnderstandTestCase):
    def setUp(self) -> None:
        super().setUp()
        document = make_document(self.store, "05-reply-needed-ok.eml")
        self.report = process(self.store, document)
        self.assertTrue(self.report.draftable)
        drafts = drafts_of(self.store, self.report.case_id)
        self.assertEqual(1, len(drafts))
        self.draft = drafts[0]
        self.draft_id = self.draft.id
        self.v1 = versions_of(self.store, self.draft_id)[0]

    def test_first_version_is_open_and_linked(self):
        """草稿初始状态 OPEN，只有 v1，且 v1 有内容哈希与引用。"""
        self.assertEqual("OPEN", str(self.draft.state))
        self.assertEqual([self.v1.id], list(self.draft.version_ids))
        self.assertEqual([self.v1.id], [item.id for item in versions_of(self.store, self.draft_id)])
        self.assertEqual(self.v1.id, version_by_number(self.store, self.draft_id, 1).id)
        self.assertEqual(1, int(self.v1.version_no))
        self.assertTrue(self.v1.content_hash)
        self.assertTrue(self.v1.citations)

    def test_edit_creates_new_version_and_keeps_v1(self):
        """编辑 → 新版本；v1 的正文与哈希**不变**（历史只增不改）。"""
        v1_body = self.v1.body
        v1_hash = self.v1.content_hash
        edited = v1_body + "\n补充：如需调整，请告知。"
        v2 = new_version(self.store, self.draft_id, body=edited, author_id="user", change_reason="用户追加一句")

        self.assertEqual(2, int(v2.version_no))
        self.assertEqual(edited, v2.body)
        self.assertNotEqual(v1_hash, v2.content_hash)

        history = versions_of(self.store, self.draft_id)
        self.assertEqual([1, 2], [int(item.version_no) for item in history])
        self.assertEqual(v1_body, history[0].body)
        self.assertEqual(v1_hash, history[0].content_hash)
        self.assertEqual(2, int(current_version(self.store, self.draft_id).version_no))

    def test_same_body_does_not_create_a_second_version(self):
        """正文没变 → 幂等，不产生新版本（避免"点两次保存"造出两条历史）。"""
        before = len(versions_of(self.store, self.draft_id))
        again = new_version(self.store, self.draft_id, body=self.v1.body, author_id="user")
        self.assertEqual(int(self.v1.version_no), int(again.version_no))
        self.assertEqual(before, len(versions_of(self.store, self.draft_id)))

    def test_empty_body_is_rejected(self):
        """空正文不得成为版本（否则审批绑定的内容哈希没有意义）。"""
        with self.assertRaises(ValidationError):
            new_version(self.store, self.draft_id, body="   ")

    def test_diff_between_versions(self):
        """任意两版可比差异：给出增删行与 unified diff。"""
        v2 = new_version(
            self.store,
            self.draft_id,
            body=self.v1.body.replace("谢谢。", "谢谢，期待回复。"),
            author_id="user",
        )
        delta = diff_versions(self.store, self.draft_id, 1, 2)
        self.assertFalse(delta.identical)
        self.assertEqual(1, int(delta.from_version_no))
        self.assertEqual(2, int(delta.to_version_no))
        self.assertEqual(v2.id, delta.to_version_id)
        self.assertTrue(delta.added_lines)
        self.assertTrue(delta.removed_lines)
        self.assertIn("-", delta.unified)
        self.assertIn("+", delta.unified)
        self.assertNotEqual(delta.from_content_hash, delta.to_content_hash)

    def test_diff_of_identical_versions(self):
        """同一版本自比：`identical=True`、没有增删行。"""
        delta = diff_versions(self.store, self.draft_id, 1, 1)
        self.assertTrue(delta.identical)
        self.assertEqual([], list(delta.added_lines))
        self.assertEqual([], list(delta.removed_lines))

    def test_restore_creates_a_new_version(self):
        """恢复 v1 → 产生 v2（不是回滚指针），v1 仍然原样存在。"""
        v1_body = self.v1.body
        restored = restore_version(self.store, self.draft_id, 1, author_id="user")

        self.assertEqual(2, int(restored.version_no))
        self.assertEqual(v1_body, restored.body)
        self.assertEqual(self.v1.content_hash, restored.content_hash)
        self.assertIn("恢复自 v1", restored.change_reason)
        # v1 未被覆盖
        self.assertEqual(v1_body, version_by_number(self.store, self.draft_id, 1).body)
        # 历史链：v2 的父版本是恢复时的当前版本（v1）
        self.assertEqual(self.v1.id, restored.parent_version_id)

    def test_restore_from_an_edited_draft(self):
        """先改一版再恢复 v1：v2 是编辑，v3 是恢复，v1 始终不变。"""
        new_version(self.store, self.draft_id, body=self.v1.body + "\n改一下", author_id="user")
        restored = restore_version(self.store, self.draft_id, 1, author_id="user", reason="改回去")
        self.assertEqual(3, int(restored.version_no))
        self.assertEqual(self.v1.body, restored.body)
        self.assertEqual("改回去", restored.change_reason)
        self.assertEqual([1, 2, 3], [int(item.version_no) for item in versions_of(self.store, self.draft_id)])

    def test_restore_a_middle_version_keeps_all_history(self):
        """v1 → v2 → v3 → 恢复 v1 得到 v4；四版全部可读。"""
        new_version(self.store, self.draft_id, body=self.v1.body + "\nA", author_id="user")
        new_version(self.store, self.draft_id, body=self.v1.body + "\nB", author_id="user")
        restore_version(self.store, self.draft_id, 1, author_id="user")
        history = versions_of(self.store, self.draft_id)
        self.assertEqual([1, 2, 3, 4], [int(item.version_no) for item in history])
        for item in history:
            self.assertTrue(item.body.strip())
        self.assertEqual(self.v1.body, history[3].body)

    def test_approval_binds_a_definite_version(self):
        """审批绑定：草稿 + 版本 + 内容哈希三元组齐全，且指向当前版本。"""
        binding = approval_binding(self.store, self.draft_id)
        self.assertEqual(self.draft_id, binding.draft_id)
        self.assertEqual(self.v1.id, binding.version_id)
        self.assertEqual(1, binding.version_no)
        self.assertEqual(self.v1.content_hash, binding.content_hash)

    def test_approval_of_history_version_is_rejected(self):
        """历史版本不是草稿当前内容 → 审批必须被拒绝（否则审批与内容脱钩）。"""
        new_version(self.store, self.draft_id, body=self.v1.body + "\n改一下", author_id="user")
        with self.assertRaises(ValidationError):
            approval_binding(self.store, self.draft_id, 1)

    def test_approval_after_restore_points_to_restored_version(self):
        """恢复后再取审批绑定：指向恢复出的新版本，而不是被恢复的旧版本。"""
        restored = restore_version(self.store, self.draft_id, 1, author_id="user")
        binding = approval_binding(self.store, self.draft_id)
        self.assertEqual(restored.id, binding.version_id)
        self.assertEqual(2, binding.version_no)

    def test_draft_is_not_sendable_by_itself(self):
        """草稿只是草稿：`target_object_id` 为空 → 没有生成任何外部动作对象。"""
        self.assertIsNone(self.v1.target_object_id)
        self.assertEqual([], list(self.store.find("ExternalAction", limit=None)))
        self.assertEqual([], list(self.store.find("Approval", limit=None)))

    def test_range_is_rendered_as_one_line(self):
        """原文的一个时间区间（`14:00 至 16:30`）在草稿里只占一行。"""
        body = self.v1.body
        range_lines = [line for line in body.splitlines() if "至" in line]
        self.assertEqual(1, len(range_lines), "区间被写成多行：%r" % (range_lines,))
        self.assertIn("活动开始时间", range_lines[0])
        self.assertIn("活动结束时间", range_lines[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
