"""连接器契约测试：邮件身份/发送结果分类、网盘回读校验（SPEC §7.3、§21.2、§21.4）。"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from asistent.contracts import (
    ArchiveIntegrityError,
    FixtureMailSource,
    LocalDirPanClient,
    MailSendRequest,
    RecordingMailSender,
    build_manifest,
    capture_files,
    verify_readback,
)

EML = """From: 通知 <notice@example.org>
To: owner@example.com
Message-ID: <notice-1@example.org>
Date: Sat, 10 Oct 2026 09:00:00 +0800
Subject: 活动通知

报名截止时间：10月12日 23:59
活动开始时间：未写明
"""


class MailConnectorContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="asistent-mail-")
        with open(os.path.join(self.root, "001.eml"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(EML)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_cursor_makes_sync_idempotent(self) -> None:
        source = FixtureMailSource(self.root)
        first, cursor = source.list_new(None)
        self.assertEqual(1, len(first))
        second, cursor2 = source.list_new(cursor)
        self.assertEqual([], second)
        self.assertEqual(cursor, cursor2)

    def test_identity_key_is_stable_and_account_scoped(self) -> None:
        source = FixtureMailSource(self.root, account="a@example.com")
        refs, _ = source.list_new(None)
        ref = refs[0]
        self.assertEqual(ref.identity_key(), ref.identity_key())
        other = FixtureMailSource(self.root, account="b@example.com")
        other_ref, _ = other.list_new(None)
        self.assertNotEqual(ref.identity_key(), other_ref[0].identity_key())

    def test_raw_body_is_recoverable(self) -> None:
        source = FixtureMailSource(self.root)
        refs, _ = source.list_new(None)
        raw = source.fetch_raw(refs[0])
        self.assertIn("报名截止时间".encode("utf-8"), raw)

    def test_sender_rejects_replayed_idempotency_key(self) -> None:
        outbox = tempfile.mkdtemp(prefix="asistent-outbox-")
        try:
            sender = RecordingMailSender(outbox)
            request = MailSendRequest(
                from_account="owner@example.com",
                to=["notice@example.org"],
                subject="Re: 活动通知",
                body="我确认参加。",
                idempotency_key="action:abc123",
                action_id="act_1",
            )
            first = sender.send(request)
            self.assertEqual("SUCCEEDED", first.result_class)
            self.assertTrue(first.accepted)

            replay = MailSendRequest(
                from_account="owner@example.com",
                to=["notice@example.org"],
                subject="Re: 活动通知",
                body="我确认参加。",
                idempotency_key="action:abc123",
                action_id="act_1",
            )
            second = sender.send(replay)
            self.assertEqual("FAILED_SAFE", second.result_class)
            self.assertFalse(second.accepted)
            self.assertEqual(1, len(os.listdir(outbox)))
        finally:
            shutil.rmtree(outbox, ignore_errors=True)

    def test_send_requires_idempotency_key(self) -> None:
        sender = RecordingMailSender(tempfile.mkdtemp(prefix="asistent-outbox-"))
        with self.assertRaises(Exception):
            sender.send(
                MailSendRequest(from_account="a@example.com", to=["b@example.com"], subject="s", body="b")
            )

    def test_unknown_result_is_not_accepted(self) -> None:
        from asistent.contracts import MailSendResult

        result = MailSendResult.unknown("连接中断，无法确认是否已接受")
        self.assertFalse(result.accepted)
        self.assertEqual("UNKNOWN", result.result_class)


class PanConnectorContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="asistent-pan-local-")
        self.source = tempfile.mkdtemp(prefix="asistent-pan-src-")
        self.scratch = tempfile.mkdtemp(prefix="asistent-pan-scratch-")
        self.pan = LocalDirPanClient(self.root)

    def tearDown(self) -> None:
        for path in (self.root, self.source, self.scratch):
            shutil.rmtree(path, ignore_errors=True)

    def _local_file(self, relative: str, content: str) -> None:
        absolute = os.path.join(self.source, relative)
        os.makedirs(os.path.dirname(absolute), exist_ok=True)
        with open(absolute, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)

    def test_round_trip_and_readback_verification(self) -> None:
        self._local_file("资料库/笔记.md", "内容一\n")
        self._local_file("资料库/联系人.md", "张三\n")
        files, _ = capture_files(self.source)
        manifest = build_manifest(scope="snapshot", scope_ref="", files=files)

        for item in files:
            result = self.pan.put(item.absolute_path, f"/个人助手归档/{item.relative_path}")
            self.assertTrue(result.ok)
            self.assertEqual(item.sha256, result.content_hash)

        readback = verify_readback(
            pan=self.pan,
            remote_root="/个人助手归档",
            entries=manifest["entries"],
            scratch_dir=self.scratch,
            local_root=self.source,
        )
        self.assertTrue(readback.ok)
        self.assertEqual("VERIFIED", readback.state)
        self.assertEqual(2, len(readback.verified))

    def test_readback_detects_tampered_remote_content(self) -> None:
        self._local_file("资料库/笔记.md", "内容一\n")
        files, _ = capture_files(self.source)
        manifest = build_manifest(scope="snapshot", scope_ref="", files=files)
        for item in files:
            self.pan.put(item.absolute_path, f"/归档/{item.relative_path}")

        # 远端内容被替换成同样长度的不同内容：只比大小会漏掉，必须靠哈希发现
        with open(os.path.join(self.root, "归档/资料库/笔记.md"), "wb") as handle:
            handle.write("内容二\n".encode("utf-8"))
        readback = verify_readback(
            pan=self.pan,
            remote_root="/归档",
            entries=manifest["entries"],
            scratch_dir=self.scratch,
            local_root=self.source,
        )
        self.assertFalse(readback.ok)
        self.assertEqual("PARTIAL", readback.state)
        self.assertEqual(1, len(readback.mismatched))
        with self.assertRaises(ArchiveIntegrityError):
            readback.require_ok()

    def test_missing_remote_file_is_not_verified(self) -> None:
        self._local_file("资料库/笔记.md", "内容一\n")
        files, _ = capture_files(self.source)
        manifest = build_manifest(scope="snapshot", scope_ref="", files=files)
        readback = verify_readback(
            pan=self.pan,
            remote_root="/空的",
            entries=manifest["entries"],
            scratch_dir=self.scratch,
            local_root=self.source,
        )
        self.assertFalse(readback.ok)
        self.assertEqual(["资料库/笔记.md"], readback.missing)

    def test_path_traversal_is_refused(self) -> None:
        with self.assertRaises(Exception):
            self.pan.put(__file__, "/../../逃逸.md")

    def test_list_and_quota_are_available_for_retention(self) -> None:
        self._local_file("资料库/笔记.md", "内容一\n")
        files, _ = capture_files(self.source)
        self.pan.put(files[0].absolute_path, "/归档/资料库/笔记.md")
        rows = self.pan.list("/归档/资料库")
        self.assertEqual(1, len(rows))
        self.assertIn("free_bytes", self.pan.quota())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
