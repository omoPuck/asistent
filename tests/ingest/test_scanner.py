"""扫描器测试：确定性顺序、符号链接白名单、环保护（SPEC §6.1）。"""

from __future__ import annotations

import os
import sys
import unittest

from asistent.ingest.scanner import normalize_relative, scan_directory, scan_roots

from .support import MVP1TestCase


class ScanOrderTest(MVP1TestCase):
    def test_scan_order_is_deterministic(self) -> None:
        for name in ("b.md", "a.md", "C.md", "0.md"):
            self.bench.write(f"notes/{name}", name)
        first = [item.relative_path for item in scan_directory(self.bench.data_dir).items]
        second = [item.relative_path for item in scan_directory(self.bench.data_dir).items]
        self.assertEqual(first, second)
        self.assertEqual(sorted(first, key=str.casefold), first, "必须按 casefold 排序保证确定性")

    def test_relative_path_uses_forward_slashes(self) -> None:
        self.bench.write("deep/nested/notes/a.md", "内容\n")
        items = scan_directory(self.bench.data_dir).items
        self.assertEqual(["deep/nested/notes/a.md"], [item.relative_path for item in items])
        self.assertEqual("deep/nested/notes/a.md", normalize_relative(r".\deep\nested\notes\a.md"))

    def test_suffix_filter_records_skipped_files(self) -> None:
        self.bench.write("notes/a.md", "甲\n")
        self.bench.write("notes/b.bin", "乙\n")
        report = scan_directory(self.bench.data_dir, suffixes=(".md",))
        self.assertEqual(["notes/a.md"], [item.relative_path for item in report.items])
        self.assertEqual(1, len(report.skipped))
        self.assertIn("suffix_not_in_allowlist", report.skipped[0]["reason"])

    def test_non_regular_file_is_reported(self) -> None:
        if sys.platform == "win32":
            self.skipTest("Windows 无 POSIX 命名管道（os.mkfifo 不可用）")
        fifo = os.path.join(self.bench.data_dir, "pipe.dat")
        os.mkfifo(fifo)
        report = scan_directory(self.bench.data_dir)
        self.assertTrue(any("not_a_regular_file" in entry["reason"] for entry in report.skipped))

    def test_missing_root_is_reported_as_error(self) -> None:
        report = scan_directory(os.path.join(self.bench.root, "does-not-exist"))
        self.assertTrue(report.errors)
        self.assertIn("not_a_directory", report.errors[0]["reason"])


class SymlinkTest(MVP1TestCase):
    def _symlink(self, link: str, target: str, *, directory: bool) -> bool:
        try:
            os.symlink(target, link, target_is_directory=directory)
            return True
        except (OSError, NotImplementedError, AttributeError):
            return False

    def test_symlink_is_not_followed_by_default(self) -> None:
        self.bench.write("notes/a.md", "甲\n")
        outside = os.path.join(self.bench.root, "outside")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "secret.md"), "w", encoding="utf-8") as handle:
            handle.write("白名单外的秘密\n")

        if not self._symlink(self.bench.path("link"), outside, directory=True):
            self.skipTest("当前平台/权限不允许创建符号链接")

        report = scan_directory(self.bench.data_dir)
        relative = [item.relative_path for item in report.items]
        self.assertEqual(["notes/a.md"], relative, "默认不跟随符号链接")
        self.assertTrue(any(entry["reason"] == "symlink_not_followed" for entry in report.pruned))

    def test_followed_symlink_cannot_escape_the_root(self) -> None:
        outside = os.path.join(self.bench.root, "outside")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "secret.md"), "w", encoding="utf-8") as handle:
            handle.write("白名单外的秘密\n")
        self.bench.write("notes/a.md", "甲\n")

        if not self._symlink(self.bench.path("escape"), outside, directory=True):
            self.skipTest("当前平台/权限不允许创建符号链接")

        report = scan_directory(self.bench.data_dir, follow_symlinks=True)
        relative = [item.relative_path for item in report.items]
        self.assertEqual(["notes/a.md"], relative, "跟随链接也不得越出白名单根")
        self.assertTrue(any(entry["reason"] == "symlink_escapes_root" for entry in report.pruned))

    def test_followed_symlink_inside_root_does_not_duplicate(self) -> None:
        self.bench.write("notes/a.md", "甲\n")
        if not self._symlink(self.bench.path("alias"), self.bench.path("notes"), directory=True):
            self.skipTest("当前平台/权限不允许创建符号链接")

        report = scan_directory(self.bench.data_dir, follow_symlinks=True)
        relative = [item.relative_path for item in report.items]
        self.assertEqual(1, len(relative), f"同一物理文件不得出现两次: {relative}")
        self.assertEqual(["notes/a.md"], relative)
        # 目录级去重：别名目录要么被识别为"已访问"，要么被识别为"重复文件"，但必须有记录
        self.assertEqual(1, len(report.pruned), report.pruned)
        reason = report.pruned[0]["reason"]
        self.assertTrue(
            "directory_already_visited" in reason or "duplicate_of" in reason, report.pruned
        )
        self.assertTrue(
            any(name in report.pruned[0]["path"] for name in ("alias", "notes")), report.pruned
        )

    def test_directory_cycle_is_pruned(self) -> None:
        self.bench.write("notes/a.md", "甲\n")
        # 让 notes 指向自己：无论平台是否支持 symlink，都不允许无限扫描
        if not self._symlink(os.path.join(self.bench.path("notes"), "self"), self.bench.path("notes"), directory=True):
            self.skipTest("当前平台/权限不允许创建符号链接")

        report = scan_directory(self.bench.data_dir, follow_symlinks=True)
        relative = sorted(item.relative_path for item in report.items)
        self.assertEqual(["notes/a.md"], relative)
        self.assertTrue(any(entry["reason"] == "directory_already_visited" for entry in report.pruned))

    def test_multiple_roots_do_not_double_count(self) -> None:
        self.bench.write("notes/a.md", "跨根\n")
        reports = scan_roots([self.bench.data_dir, self.bench.path("notes")])
        total = sum(len(report.items) for report in reports)
        self.assertEqual(1, total, "跨根去重：同一物理文件只采集一次")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
