"""索引写入、状态机、删除/改名、重建、失败不丢资料（SPEC §6.2、§16 MVP-1）。

契约层原文：
```text
PENDING → INDEXED | STALE | FAILED
INDEXED → STALE | PENDING
STALE   → INDEXED | FAILED | REBUILD_REQUIRED
FAILED  → PENDING | INDEXED | REBUILD_REQUIRED
REBUILD_REQUIRED → PENDING | INDEXED | FAILED
```
"""

from __future__ import annotations

import os
import unittest

from asistent.contracts.enums import IndexState
from asistent.contracts.errors import NotFound
from asistent.index import fts
from asistent.index.indexer import Indexer

from .support import MVP1TestCase


class IndexWriteTest(MVP1TestCase):
    def test_indexed_document_is_searchable_from_sqlite(self) -> None:
        self.bench.write("notes/a.md", "# 标题\n设备常驻与部署方案说明\n")
        run = self.bench.run()

        self.assertEqual(1, run.indexing.indexed)
        document = self.bench.document_by_relative("notes/a.md")
        self.assertEqual(str(IndexState.INDEXED), str(document.index_state))
        self.assertIsNotNone(document.last_indexed_at)
        self.assertEqual(1, fts.count_rows(self.bench.connection, fts.DOCUMENT_TABLE))
        self.assertEqual(1, fts.count_rows(self.bench.connection, fts.SEARCH_TABLE))

    def test_both_fts_tables_exist_and_stay_in_sync(self) -> None:
        """契约表 `search_index` 与文档表 `document_index` 必须同列集、同内容。"""
        self.bench.write("notes/a.md", "双表一致：部署\n")
        self.bench.write("notes/b.md", "双表一致：归档\n")
        self.bench.run()
        search_rows = self.bench.connection.execute(
            f"SELECT doc_id, path, body, raw_body FROM {fts.SEARCH_TABLE} ORDER BY doc_id"
        ).fetchall()
        document_rows = self.bench.connection.execute(
            f"SELECT doc_id, path, body, raw_body FROM {fts.DOCUMENT_TABLE} ORDER BY doc_id"
        ).fetchall()
        self.assertEqual(2, len(search_rows))
        self.assertEqual(search_rows, document_rows)
        # 契约 DDL 的列表必须是我们列集的子集（表名一致，只是多了 UNINDEXED 列）
        self.assertIn("search_index", fts.FTS_DDL)
        self.assertIn("tokenize='unicode61 remove_diacritics 2'", fts.FTS_DDL)

    def test_search_rows_contain_normalized_body(self) -> None:
        from asistent.contracts.search import normalize_for_index

        self.bench.write("notes/a.md", "部署 Deploy Plan\n")
        self.bench.collect()
        row = self.bench.connection.execute(
            f"SELECT body, raw_body FROM {fts.SEARCH_TABLE} LIMIT 1"
        ).fetchone()
        self.assertEqual(normalize_for_index("部署 Deploy Plan\n"), row[0])
        self.assertEqual("部署 Deploy Plan\n", row[1], "raw_body 必须保留原文用于引用展示")

    def test_reindex_of_unchanged_document_does_not_rewrite(self) -> None:
        self.bench.write("notes/a.md", "不变内容\n")
        self.bench.collect()
        second = self.bench.run()
        self.assertEqual(0, second.indexing.indexed)
        self.assertEqual(1, second.indexing.unchanged)
        self.assertEqual(0, second.indexing.failed)

    def test_forced_reindex_is_idempotent(self) -> None:
        self.bench.write("notes/a.md", "强制重建也要幂等\n")
        self.bench.collect()
        before = fts.count_rows(self.bench.connection, fts.DOCUMENT_TABLE)
        report = self.bench.run(force_reindex=True)
        self.assertEqual(1, report.indexing.indexed)
        self.assertEqual(before, fts.count_rows(self.bench.connection, fts.DOCUMENT_TABLE))

    def test_index_survives_process_restart_of_the_index_file(self) -> None:
        """索引库是真落盘的：换一个连接（模拟重启）仍然搜得到。"""
        self.bench.write("notes/a.md", "落盘验证：百度网盘归档\n")
        self.bench.collect()
        self.bench.refresh()
        response = self.bench.search("百度网盘")
        self.assertEqual(1, response.count)

    def test_deleted_source_file_marks_document_stale_and_keeps_record(self) -> None:
        self.bench.write("notes/a.md", "会被删除的内容\n")
        self.bench.collect()
        document_id = self.bench.document_by_relative("notes/a.md").id

        self.bench.remove("notes/a.md")
        report = self.bench.collect_again()

        refreshed = self.bench.store.get("SourceDocument", document_id)
        self.assertEqual(str(IndexState.STALE), str(refreshed.index_state))
        self.assertEqual(1, report.stale_documents)
        self.assertTrue(any(outcome.status == "TRANSITIONED" for outcome in report.outcomes))
        events = [event for event in self.bench.store.list_events(limit=100) if event.kind == "CONTENT_REMOVED"]
        self.assertTrue(events, "删除必须发布 CONTENT_REMOVED 事件（不静默）")
        # 旧检索记录还在（直到重建/同步时变成"源文件不存在"的可解释状态）
        self.assertEqual(1, fts.count_rows(self.bench.connection, fts.DOCUMENT_TABLE))

    def test_renamed_file_leaves_old_object_stale_and_indexes_new_path(self) -> None:
        self.bench.write("notes/old.md", "改名测试：会议纪要\n")
        self.bench.collect()
        old_id = self.bench.document_by_relative("notes/old.md").id

        self.bench.rename("notes/old.md", "notes/new.md")
        report = self.bench.collect_again()

        self.assertEqual(1, report.new_documents)
        self.assertEqual(str(IndexState.STALE), str(self.bench.store.get("SourceDocument", old_id).index_state))
        new_document = self.bench.document_by_relative("notes/new.md")
        self.assertNotEqual(old_id, new_document.id, "改名是新路径 → 新对象；旧对象保留并标记 STALE")
        self.assertEqual(str(IndexState.INDEXED), str(new_document.index_state))

    def test_stale_flag_is_visible_in_the_collection_report(self) -> None:
        """源文件消失时，采集阶段就要把"旧索引已过期"写进报告（阶段可观测）。"""
        self.bench.write("notes/a.md", "会被删除的内容\n")
        self.bench.collect()
        document = self.bench.document_by_relative("notes/a.md")

        self.bench.remove("notes/a.md")
        report = self.bench.collect_again()

        stale = [outcome for outcome in report.outcomes if outcome.status == "TRANSITIONED"]
        self.assertEqual(1, len(stale))
        self.assertEqual("notes/a.md", stale[0].relative_path)
        self.assertEqual(document.id, stale[0].document_id)
        self.assertIn("STALE", stale[0].reason)
        self.assertEqual(1, report.stale_documents)

    def test_stale_documents_are_reindexed_on_next_sync(self) -> None:
        self.bench.write("notes/a.md", "第一版内容\n")
        self.bench.collect()
        self.bench.write("notes/a.md", "第二版内容，部署方案\n")
        report = self.bench.run()
        self.assertEqual(1, report.indexing.indexed, "内容变化后必须重算索引")
        self.assertEqual(1, self.bench.search("第二版").count)
        self.assertEqual(0, self.bench.search("第一版").count, "旧内容不得残留可检索结果")

    def test_orphan_index_rows_are_removed(self) -> None:
        fts.upsert_document(
            self.bench.connection, doc_id="doc_orphan", path="dangling.md", raw_body="孤儿记录"
        )
        report = self.bench.pipeline.indexer.sync()
        self.assertEqual(["doc_orphan"], report.removed_orphans)
        self.assertEqual(0, fts.count_rows(self.bench.connection, fts.DOCUMENT_TABLE))


class IndexFailureTest(MVP1TestCase):
    def _collect_without_snapshot(self, **_overrides):
        """采集但不存原文快照：模拟"原始内容确实拿不到"的场景。"""
        from asistent.ingest.collector import CollectionPolicy

        return self.bench.pipeline.collect(
            [self.bench.data_dir], policy=CollectionPolicy(suffixes=None, raw_sink=None)
        )

    def test_missing_source_with_snapshot_still_indexes(self) -> None:
        """快照兜底：源文件被删了，索引依然能从快照拿到原始内容——但状态必须如实报"待重建"。"""
        self.bench.write("notes/a.md", "快照里的内容：部署方案\n")
        self.bench.collect()
        document_id = self.bench.document_by_relative("notes/a.md").id

        fts.delete_document(self.bench.connection, document_id)
        fts.mark_index_state(
            self.bench.connection, doc_id=document_id, path="notes/a.md", index_state="FAILED", error="模拟索引丢失"
        )
        self.bench.store.set_index_state(document_id, str(IndexState.STALE), reason="模拟索引失效")
        self.bench.remove("notes/a.md")

        result = self.bench.pipeline.indexer.index_document(document_id, force=True)
        self.assertEqual(str(IndexState.REBUILD_REQUIRED), result.index_state)
        self.assertEqual("MISSING_DATA", result.failure_class)
        self.assertIn("source_unavailable", result.error or "")
        # 关键：内容仍然搜得到（原文快照兜底），且状态如实告知"源文件不在原位"
        self.assertEqual(1, self.bench.search("部署方案").count)
        self.assertEqual(
            str(IndexState.REBUILD_REQUIRED),
            str(self.bench.store.get("SourceDocument", document_id).index_state),
        )

    def test_missing_source_without_snapshot_fails_with_reason(self) -> None:
        self.bench.write("notes/a.md", "没有快照的内容\n")
        self._collect_without_snapshot()
        self.assertEqual(0, self.bench.pipeline.snapshots.total_bytes(), "本次采集刻意不产生快照")

        document_id = self.bench.document_by_relative("notes/a.md").id
        self.bench.remove("notes/a.md")

        result = self.bench.pipeline.indexer.index_document(document_id, force=True)
        self.assertFalse(result.ok)
        self.assertIn("源文件不存在", result.error or "")
        document = self.bench.store.get("SourceDocument", document_id)
        self.assertEqual(str(IndexState.REBUILD_REQUIRED), str(document.index_state))
        self.assertTrue(document.index_error, "失败原因必须写回 SourceDocument.index_error")
        # 原始资料对象与审计都还在
        self.assertIsNotNone(self.bench.store.get("SourceDocument", document_id))
        self.assertTrue(self.bench.store.audit_trail(object_id=document_id, limit=100))

    def test_undecodable_content_is_skipped_at_collection_with_reason(self) -> None:
        """不可解码内容在采集阶段就被识别（不是等到索引阶段才发现）。"""
        self.bench.write_bytes("notes/weird.md", b"abc\xff\xfe def")
        report = self._collect_without_snapshot().collection
        skipped = [outcome for outcome in report.skipped if outcome.relative_path == "notes/weird.md"]
        self.assertEqual(1, len(skipped), f"必须跳过并记录原因: {report.skip_reasons()}")
        self.assertTrue(
            "undecodable_text" in skipped[0].reason or "binary_content_detected" in skipped[0].reason,
            skipped[0].reason,
        )
        trail = [event for event in self.bench.store.audit_trail(limit=1000) if event.action == "COLLECT_SKIP"]
        self.assertTrue(any("notes/weird.md" in event.reason or "notes/weird.md" in str(event.detail) for event in trail))

    def test_index_failure_does_not_lose_source_document_or_audit(self) -> None:
        """索引失败后：SourceDocument 在、原文快照在、审计在、可重建。"""
        self.bench.write("notes/a.md", "失败后必须还能重建：百度网盘\n")
        self.bench.collect()
        document_id = self.bench.document_by_relative("notes/a.md").id
        snapshot_hash = self.bench.store.get("SourceDocument", document_id).content_hash

        self.assertTrue(self.bench.pipeline.snapshots.has(snapshot_hash))
        fts.delete_document(self.bench.connection, document_id)
        fts.mark_index_state(
            self.bench.connection,
            doc_id=document_id,
            path="notes/a.md",
            index_state="FAILED",
            error="模拟写检索表失败",
        )
        # 契约状态机：INDEXED → FAILED 不合法，必须先 STALE
        self.bench.store.set_index_state(document_id, str(IndexState.STALE), reason="模拟索引失效")
        self.bench.store.set_index_state(
            document_id, str(IndexState.FAILED), reason="模拟索引失败", error="模拟写检索表失败"
        )

        failed_document = self.bench.store.get("SourceDocument", document_id)
        self.assertEqual(str(IndexState.FAILED), str(failed_document.index_state))
        self.assertTrue(failed_document.index_error)

        report = self.bench.pipeline.rebuild_index()
        self.assertEqual(1, report.indexed)
        self.assertEqual(1, self.bench.search("百度网盘").count)
        self.assertEqual(str(IndexState.INDEXED), str(self.bench.store.get("SourceDocument", document_id).index_state))

    def test_indexer_on_unknown_document_reports_missing_data(self) -> None:
        result = self.bench.pipeline.indexer.index_document("doc_does_not_exist")
        self.assertFalse(result.ok)
        self.assertEqual("MISSING_DATA", result.failure_class)
        self.assertEqual("索引对象不存在", result.reason)

    def test_indexer_does_not_modify_source_file(self) -> None:
        path = self.bench.write("notes/a.md", "索引不得改源文件\n")
        stat_before = os.stat(path)
        self.bench.collect()
        self.bench.pipeline.rebuild_index()
        stat_after = os.stat(path)
        self.assertEqual(stat_before.st_mtime_ns, stat_after.st_mtime_ns)
        self.assertEqual(stat_before.st_size, stat_after.st_size)


class RebuildIndexTest(MVP1TestCase):
    def test_rebuild_resets_and_reindexes_everything(self) -> None:
        self.bench.write("notes/a.md", "重建测试：部署\n")
        self.bench.write("notes/b.md", "重建测试：会议纪要\n")
        self.bench.collect()
        self.assertEqual(2, fts.count_rows(self.bench.connection, fts.DOCUMENT_TABLE))

        report = self.bench.pipeline.rebuild_index()
        self.assertEqual(2, report.indexed)
        self.assertEqual(0, report.failed)
        self.assertEqual(2, len(report.reset_to_pending))
        self.assertEqual(2, fts.count_rows(self.bench.connection, fts.DOCUMENT_TABLE))
        self.assertEqual(1, self.bench.search("部署").count)
        self.assertEqual(1, self.bench.search("会议纪要").count)

    def test_rebuild_is_idempotent(self) -> None:
        self.bench.write("notes/a.md", "重复重建\n")
        self.bench.collect()
        self.bench.pipeline.rebuild_index()
        first = fts.count_rows(self.bench.connection, fts.DOCUMENT_TABLE)
        self.bench.pipeline.rebuild_index()
        self.assertEqual(first, fts.count_rows(self.bench.connection, fts.DOCUMENT_TABLE))
        self.assertEqual(1, self.bench.search("重复重建").count)

    def test_rebuild_after_lexer_rule_change_reflects_new_rules(self) -> None:
        """切分规则变了必须能整体重建（这里用"删掉索引再重建"模拟规则变更后的重算）。"""
        self.bench.write("notes/a.md", "部署 deploy 计划\n")
        self.bench.collect()
        fts.delete_document(self.bench.connection, self.bench.document_by_relative("notes/a.md").id)
        self.assertEqual(0, self.bench.search("部署").count)

        report = self.bench.pipeline.rebuild_index()
        self.assertEqual(1, report.indexed)
        self.assertEqual(1, self.bench.search("部署").count)
        self.assertEqual(1, self.bench.search("deploy").count)

    def test_freshness_reports_pending_documents(self) -> None:
        self.bench.write("notes/a.md", "新鲜度\n")
        self.bench.collect()
        pending = self.bench.pipeline.freshness()
        self.assertEqual([], pending["pending_index_document_ids"])
        self.assertEqual(1, pending["fts_documents"])
        self.assertGreaterEqual(pending["snapshot_files"], 1)

        # 制造一个"待索引"状态：直接写回 STALE 后必须出现在待办清单里
        document = self.bench.document_by_relative("notes/a.md")
        self.bench.store.set_index_state(document.id, str(IndexState.STALE), reason="测试")
        pending_again = self.bench.pipeline.freshness()
        self.assertEqual([document.id], pending_again["pending_index_document_ids"])

    def test_index_state_machine_rejects_illegal_transition(self) -> None:
        self.bench.write("notes/a.md", "状态机\n")
        self.bench.collect()
        document = self.bench.document_by_relative("notes/a.md")
        self.assertEqual(str(IndexState.INDEXED), str(document.index_state))
        # INDEXED → REBUILD_REQUIRED 不是合法转换（必须先 STALE）
        with self.assertRaises(Exception):
            self.bench.store.set_index_state(
                document.id, str(IndexState.REBUILD_REQUIRED), reason="非法转换必须被拒绝"
            )
        self.assertEqual(
            str(IndexState.INDEXED),
            str(self.bench.store.get("SourceDocument", document.id).index_state),
            "被拒绝的转换不得改动状态",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
