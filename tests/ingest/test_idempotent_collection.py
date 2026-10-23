"""采集幂等与增量：对象数、审计条数、事件数在重复扫描后必须不变（SPEC §16 MVP-1）。

"幂等"在本项目里的定义是**不产生第二个对象、不重复写审计**，
不是"写一遍同样的内容"。所以这里直接对 `stats()` 与 `audit_trail()` 计数取证。
"""

from __future__ import annotations

import os
import time
import unittest

from asistent.contracts.enums import IndexState
from asistent.ingest.collector import CollectionPolicy, FileStatus, source_document_id

from .support import MVP1TestCase, fixture_path, make_png


class IdempotentCollectionTest(MVP1TestCase):
    def test_two_scans_produce_no_second_object_or_audit(self) -> None:
        self.bench.write("notes/a.md", "# 甲\n部署方案确认\n")
        self.bench.write("notes/b.md", "# 乙\n百度网盘归档\n")

        first = self.bench.collect()
        after_first = self.bench.counts()
        audit_first = self.bench.audit_count()

        self.assertEqual(2, first.new_documents)
        self.assertEqual(2, len(self.bench.documents()))

        second = self.bench.collect_again()
        after_second = self.bench.counts()
        audit_second = self.bench.audit_count()

        self.assertEqual(2, second.unchanged_files)
        self.assertEqual(0, second.new_documents)
        self.assertEqual(0, second.updated_documents)
        self.assertEqual(after_first, after_second, "重复扫描后对象数必须不变")
        self.assertEqual(audit_first, audit_second, "重复扫描不得产生新的审计事件")
        self.assertEqual(first.fingerprint(), second.fingerprint(), "重复扫描指纹必须一致")

    def test_second_scan_is_unchanged_even_when_mtime_moves(self) -> None:
        """哪怕 mtime 变了（touch 不改内容），sha256 相同就是未变化：不重写、不升版本。"""
        path = self.bench.write("notes/a.md", "内容不变\n")
        self.bench.collect()
        before = self.bench.document_by_relative("notes/a.md")
        audit_before = self.bench.audit_count()

        os.utime(path, (time.time() + 10, time.time() + 10))
        report = self.bench.collect_again()
        after = self.bench.document_by_relative("notes/a.md")

        self.assertEqual(1, report.unchanged_files)
        self.assertEqual(0, report.updated_documents)
        self.assertEqual(before.content_version, after.content_version)
        self.assertEqual(before.content_hash, after.content_hash)
        self.assertEqual(audit_before, self.bench.audit_count())

    def test_incremental_scan_only_touches_changed_files(self) -> None:
        self.bench.write("notes/a.md", "甲内容 v1\n")
        self.bench.write("notes/b.md", "乙内容 v1\n")
        self.bench.collect()

        self.bench.write("notes/a.md", "甲内容 v2 改过了\n")
        report = self.bench.collect_again()

        self.assertEqual(1, report.updated_documents)
        self.assertEqual(1, report.unchanged_files)
        document = self.bench.document_by_relative("notes/a.md")
        self.assertEqual(2, document.content_version, "内容变化必须升 content_version")
        self.assertEqual(
            str(IndexState.INDEXED), str(document.index_state),
            "采集把旧索引标记 STALE，随后索引器必须把它重算回 INDEXED",
        )
        self.assertEqual(1, self.bench.search("v2").count)

    def test_same_size_same_second_rewrite_is_detected_by_hash(self) -> None:
        """mtime 与 size 都没变的改写，只能靠 sha256 发现——必须先哈希再判重。"""
        path = self.bench.write("notes/same-size.md", "AAAA1111")
        self.bench.collect()
        stat = os.stat(path)

        with open(path, "wb") as handle:
            handle.write(b"BBBB2222")  # 同长度
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # 把 mtime 改回去

        report = self.bench.collect_again()
        self.assertEqual(1, report.updated_documents, "同长度同 mtime 的改写必须靠 sha256 识别")
        self.assertEqual(2, self.bench.document_by_relative("notes/same-size.md").content_version)

    def test_object_id_is_deterministic_for_a_path(self) -> None:
        from asistent.contracts.ids import idempotency_key

        expected = source_document_id("notes/a.md")
        self.assertTrue(expected.startswith("doc_"))
        self.assertEqual(expected, source_document_id("./notes/a.md"))
        self.assertEqual(expected, source_document_id("notes\\a.md"))
        self.assertNotEqual(expected, source_document_id("notes/b.md"))
        # 身份键是确定性派生的（同一路径 → 同一键）
        self.assertEqual(
            idempotency_key("source_document", "local_fs", "notes/a.md"),
            idempotency_key("source_document", "local_fs", "notes/a.md"),
        )

    def test_collecting_two_roots_does_not_duplicate_documents(self) -> None:
        self.bench.write("notes/a.md", "跨根不重复\n")
        report = self.bench.collect(roots=[self.bench.data_dir, os.path.join(self.bench.data_dir, "notes")])
        self.assertEqual(1, report.new_documents)
        self.assertEqual(1, len(self.bench.documents()))

    def test_public_fixtures_are_collected_repeatedly_without_growth(self) -> None:
        """直接用仓库里的可提交样例资料跑一遍幂等（同一份资料，两次扫描不增长）。"""
        report = self.bench.collect(roots=[fixture_path("notes")])
        self.assertGreaterEqual(report.new_documents, 4)
        first = self.bench.counts()
        audit_first = self.bench.audit_count()

        again = self.bench.collect(roots=[fixture_path("notes")])
        self.assertEqual(0, again.new_documents)
        self.assertEqual(first, self.bench.counts())
        self.assertEqual(audit_first, self.bench.audit_count())


class NonDestructiveTest(MVP1TestCase):
    def test_source_files_are_never_modified(self) -> None:
        path = self.bench.write("notes/a.md", "原始内容，采集不得修改\n")
        before = os.stat(path)
        with open(path, "rb") as handle:
            before_bytes = handle.read()

        self.bench.collect()
        self.bench.search("原始内容")

        after = os.stat(path)
        with open(path, "rb") as handle:
            after_bytes = handle.read()
        self.assertEqual(before_bytes, after_bytes)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns, "采集不得改动源文件 mtime")
        self.assertEqual(before.st_size, after.st_size)

    def test_binary_file_is_skipped_with_reason_and_audit(self) -> None:
        png = make_png()
        self.bench.write_bytes("assets/logo.png", png)
        self.bench.write("notes/a.md", "有文字的笔记\n")

        # 默认后缀白名单会先过滤掉 .png：跳过原因必须是"后缀不在白名单"
        report = self.bench.collect()
        self.assertEqual(1, report.skipped_files)
        skipped = report.skipped[0]
        self.assertEqual("assets/logo.png", skipped.relative_path)
        self.assertEqual(FileStatus.SKIPPED, skipped.status)
        self.assertIn("suffix_not_in_allowlist", skipped.reason)

        # 放开后缀后，内容探测必须判为二进制并给出原因
        report2 = self.bench.collect(policy=CollectionPolicy(suffixes=None))
        reasons = report2.skip_reasons()
        self.assertTrue(any("binary_content_detected" in key for key in reasons), reasons)
        skipped2 = [outcome for outcome in report2.skipped if outcome.relative_path == "assets/logo.png"]
        self.assertEqual(1, len(skipped2))
        self.assertIn("binary_content_detected", skipped2[0].reason)

    def test_binary_content_detection_is_not_silent(self) -> None:
        self.bench.write_bytes("assets/blob.dat", b"\x00\x01\x02binary\x00")
        report = self.bench.collect(policy=CollectionPolicy(suffixes=None))
        skipped = [outcome for outcome in report.skipped if outcome.relative_path == "assets/blob.dat"]
        self.assertEqual(1, len(skipped))
        self.assertIn("binary_content_detected", skipped[0].reason)

        trail = self.bench.store.audit_trail(limit=10000)
        skip_events = [event for event in trail if event.action == "COLLECT_SKIP"]
        self.assertTrue(skip_events, "跳过必须留下审计事件（不静默丢弃）")
        self.assertTrue(any(event.result == "SKIPPED" for event in skip_events))
        self.assertTrue(any("binary_content_detected" in event.reason for event in skip_events))

        events = [event for event in self.bench.store.list_events(limit=100) if event.kind == "CONTENT_SKIPPED"]
        self.assertTrue(events, "跳过必须发布 CONTENT_SKIPPED 事件")

        # 源文件必须原封不动
        with open(self.bench.path("assets/blob.dat"), "rb") as handle:
            self.assertEqual(b"\x00\x01\x02binary\x00", handle.read())

    def test_oversized_file_is_skipped_with_reason(self) -> None:
        self.bench.write("notes/big.txt", "x" * 4096)
        report = self.bench.collect(policy=CollectionPolicy(max_bytes=1024))
        skipped = [outcome for outcome in report.skipped if outcome.relative_path == "notes/big.txt"]
        self.assertEqual(1, len(skipped))
        self.assertIn("file_too_large", skipped[0].reason)
        self.assertIn("size_bytes=4096", skipped[0].reason)
        self.assertEqual(0, len(self.bench.documents()))

    def test_empty_file_is_skipped_with_reason(self) -> None:
        self.bench.write_bytes("notes/empty.md", b"")
        report = self.bench.collect()
        skipped = [outcome for outcome in report.skipped if outcome.relative_path == "notes/empty.md"]
        self.assertEqual(1, len(skipped))
        self.assertIn("empty_file", skipped[0].reason)

    def test_case_insensitive_suffix_and_non_allowlisted_suffix_is_reported(self) -> None:
        self.bench.write("notes/a.MD", "大写后缀也要收\n")
        self.bench.write("notes/b.pdf", "%PDF-1.4 假装是 PDF\n")
        report = self.bench.collect()
        self.assertEqual(1, report.new_documents)
        reasons = report.skip_reasons()
        self.assertTrue(any("suffix_not_in_allowlist" in key for key in reasons), reasons)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
