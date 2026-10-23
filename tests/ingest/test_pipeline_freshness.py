"""端到端与时效性：新文件落盘 → 采集 → 中文可检索，并打印实测耗时（SPEC §16 MVP-1）。

计时口径：
- 用 `time.monotonic()`（不受墙钟回跳影响，SPEC §11.2）；
- 分成 collect / index / total 三段，目标是"落盘后总耗时 < 60s，目标 < 5s"。
"""

from __future__ import annotations

import os
import time
import unittest

from asistent.contracts.enums import IndexState
from asistent.index.pipeline import SearchPipeline, Stopwatch

from .support import MVP1TestCase, fixture_path


class EndToEndFreshnessTest(MVP1TestCase):
    def test_new_file_is_searchable_within_budget(self) -> None:
        self.bench.write("资料/原有.md", "原有资料：无关内容\n")
        self.bench.collect()

        stopwatch = Stopwatch()
        self.bench.write("资料/新资料.md", "# 新资料\n本次部署方案已确认，百度网盘归档通道正常。\n")
        run = self.bench.run()
        total_seconds = stopwatch.elapsed_seconds()

        self.assertEqual(1, run.collection.new_documents)
        self.assertGreaterEqual(run.indexing.indexed, 1)
        self.assertLess(total_seconds, 60.0, f"落盘到可检索必须 < 60s，实测 {total_seconds:.4f}s")
        self.assertLess(total_seconds, 5.0, f"目标 < 5s，实测 {total_seconds:.4f}s")

        response = self.bench.search("部署")
        self.assertEqual(["资料/新资料.md"], [hit.path for hit in response.hits])
        self.assertEqual(1, self.bench.search("百度网盘").count)

    def test_timing_is_reported_for_both_phases(self) -> None:
        self.bench.write("资料/a.md", "计时\n")
        run = self.bench.run()
        self.assertGreaterEqual(run.collect_seconds, 0.0)
        self.assertGreaterEqual(run.index_seconds, 0.0)
        self.assertAlmostEqual(
            run.collect_seconds + run.index_seconds, run.total_seconds, places=9
        )
        payload = run.to_dict()
        for key in ("collect_seconds", "index_seconds", "ingest_total_seconds"):
            self.assertIn(key, payload)

    def test_stopwatch_measures_real_elapsed_time(self) -> None:
        stopwatch = Stopwatch()
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            pass
        self.assertGreater(stopwatch.elapsed_seconds(), 0.04)
        self.assertGreater(stopwatch.elapsed_ms(), 40.0)

    def test_repeated_collect_of_unchanged_tree_is_fast_and_writes_nothing(self) -> None:
        for index in range(10):
            self.bench.write(f"资料/doc-{index}.md", f"第 {index} 份资料：部署相关内容\n")
        self.bench.collect()
        counts_before = self.bench.counts()

        stopwatch = Stopwatch()
        run = self.bench.run()
        elapsed = stopwatch.elapsed_seconds()

        self.assertEqual(10, run.collection.unchanged_files)
        self.assertEqual(0, run.indexing.indexed)
        self.assertEqual(counts_before, self.bench.counts())
        self.assertLess(elapsed, 60.0, f"重复采集也必须快，实测 {elapsed:.4f}s")

    def test_public_fixtures_are_searchable_by_chinese_keywords(self) -> None:
        run = self.bench.run(roots=[fixture_path("notes")])
        self.assertGreaterEqual(run.collection.new_documents, 4)

        expectations = {
            "部署": 3,          # deployment-plan / meeting-minutes / mixed-language
            "百度网盘": 2,      # archive-channels / mixed-language
            "会议纪要": 3,      # meeting-minutes / deployment-plan / mixed-language
            "归档": 2,          # archive-channels / meeting-minutes
        }
        for query, minimum in expectations.items():
            with self.subTest(query=query):
                response = self.bench.search(query)
                self.assertGreaterEqual(
                    response.count, minimum,
                    f"{query!r} 至少应命中 {minimum} 份，实际 {response.count}: {[h.path for h in response.hits]}",
                )
                for hit in response.hits:
                    self.assertTrue(hit.locator.startswith("line:"))
                    self.assertTrue(hit.citation.content_hash)

    def test_update_is_visible_within_budget(self) -> None:
        """用户改一份资料 → 新内容可检索，且记录版本/时间（SPEC §16 MVP-1 第一条）。"""
        self.bench.write("资料/a.md", "第一版：旧关键词-A\n")
        self.bench.collect()

        stopwatch = Stopwatch()
        self.bench.write("资料/a.md", "第二版：新关键词-B 部署\n")
        self.bench.collect()
        elapsed = stopwatch.elapsed_seconds()

        self.assertLess(elapsed, 60.0)
        self.assertEqual(1, self.bench.search("新关键词-B").count)
        self.assertEqual(0, self.bench.search("旧关键词-A").count)
        document = self.bench.document_by_relative("资料/a.md")
        self.assertEqual(2, document.content_version)
        self.assertTrue(document.last_indexed_at, "必须记录索引时间")
        self.assertEqual(str(IndexState.INDEXED), str(document.index_state))

    def test_search_latency_is_small(self) -> None:
        for index in range(50):
            self.bench.write(f"资料/doc-{index}.md", f"第 {index} 份资料：部署与归档\n")
        self.bench.collect()
        stopwatch = Stopwatch()
        response = self.bench.search("部署", limit=10)
        elapsed = stopwatch.elapsed_seconds()
        self.assertEqual(10, response.count)
        self.assertLess(elapsed, 1.0, f"单次检索应远小于 1s，实测 {elapsed:.6f}s")

    def test_pipeline_can_be_rebuilt_from_scratch_without_touching_sources(self) -> None:
        self.bench.write("资料/a.md", "重建：部署\n")
        self.bench.collect()
        path = self.bench.path("资料/a.md")
        mtime_before = os.stat(path).st_mtime_ns

        self.bench.refresh()
        report = self.bench.pipeline.rebuild_index()
        self.assertEqual(1, report.indexed)
        self.assertEqual(1, self.bench.search("部署").count)
        self.assertEqual(mtime_before, os.stat(path).st_mtime_ns, "重建索引不得改动源文件")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
