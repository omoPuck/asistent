"""FTS5 检索：可用性、中文子串检索、无效查询不得退化成全量。

背景（设备实测，见 docs/env/report.md §B5）：FTS5 的 `unicode61` 把连续中文当成
**一个 token**，`"部署方案"` 入库后查 `部署` 返回 0 行且不报错——检索静默失效。
所以契约要求入库前 `normalize_for_index()` 逐字切分、查询走 `to_fts_query()` 加引号。
"""

from __future__ import annotations

import unittest

from asistent.contracts.search import normalize_for_index, to_fts_query

from .support import SqliteStoreTestCase


class Fts5SearchTest(SqliteStoreTestCase):
    def test_fts5_available_and_index_table_is_built_by_migration(self) -> None:
        store = self.open_store()
        self.assertTrue(store.fts5_available(), "本机 SQLite 必须编译了 FTS5")
        self.assertTrue(store.search_index_ready(), "迁移 v3 必须建出 search_index 表")
        self.assert_object_present("search_index", store)
        self.assert_object_present("trg_audit_append_only_update", store)

    def test_cjk_substring_search_works(self) -> None:
        store = self.open_store()
        store.index_document(document_id="doc_1", path="资料/部署方案.md", body="部署方案与验收标准")
        store.index_document(document_id="doc_2", path="资料/其他.md", body="与运维无关的闲聊内容")

        hits = store.search("部署")
        self.assertEqual(["doc_1"], [item["doc_id"] for item in hits], "中文 2 字查询必须能命中")
        self.assertEqual(["资料/部署方案.md"], [item["path"] for item in hits])
        self.assertEqual(["doc_2"], [item["doc_id"] for item in store.search("闲聊")])
        self.assertEqual([], store.search("完全不存在的词"))

    def test_body_is_stored_normalized_not_raw(self) -> None:
        """入库必须走契约的规范化（CJK 逐字切分），否则 FTS5 会静默失效。"""
        store = self.open_store()
        raw = "部署方案 Deploy"
        store.index_document(document_id="doc_norm", path="a.md", body=raw)
        stored = str(
            store.connection.execute("SELECT body FROM search_index WHERE doc_id = ?", ("doc_norm",)).fetchone()[0]
        )
        self.assertEqual(normalize_for_index(raw), stored)
        self.assertIn(" ", stored)
        self.assertNotEqual(raw, stored)

    def test_query_is_built_by_contract_helper(self) -> None:
        store = self.open_store()
        store.index_document(document_id="doc_q", path="b.md", body="报名截止时间是 10月12日")
        self.assertIsNotNone(to_fts_query("报名截止"))
        self.assertEqual(["doc_q"], [item["doc_id"] for item in store.search("报名截止")])

    def test_invalid_query_returns_empty_and_never_everything(self) -> None:
        store = self.open_store()
        store.index_document(document_id="doc_x", path="c.md", body="任何内容")
        for query in ("", "   ", "!!!", "***", "''\""):
            self.assertIsNone(to_fts_query(query), query)
            self.assertEqual([], store.search(query), "无效查询 %r 不得返回全部结果" % query)

    def test_index_write_is_idempotent_per_document(self) -> None:
        store = self.open_store()
        store.index_document(document_id="doc_i", path="d.md", body="第一版内容")
        store.index_document(document_id="doc_i", path="d.md", body="第二版内容")
        count = int(
            store.connection.execute("SELECT COUNT(*) FROM search_index WHERE doc_id = ?", ("doc_i",)).fetchone()[0]
        )
        self.assertEqual(1, count, "同一文档重复入库必须覆盖，而不是堆行")
        self.assertEqual([], store.search("第一版"))
        self.assertEqual(["doc_i"], [item["doc_id"] for item in store.search("第二版")])

    def test_search_index_survives_reopen(self) -> None:
        store = self.open_store()
        store.index_document(document_id="doc_r", path="e.md", body="重启后仍然可以检索")
        reopened = self.restart(store)
        self.assertTrue(reopened.search_index_ready())
        self.assertEqual(["doc_r"], [item["doc_id"] for item in reopened.search("检索")])

    def test_rebuild_path_after_drop(self) -> None:
        """索引是派生数据：删掉重建后，重新入库即可恢复（切分规则变化时的重建路径）。"""
        from asistent.storage import search_index

        store = self.open_store()
        store.index_document(document_id="doc_d", path="f.md", body="重建索引内容")
        search_index.drop(store.connection)
        self.assertFalse(store.search_index_ready())
        with self.assertRaises(search_index.SearchIndexUnavailable):
            store.search("重建")
        search_index.ensure(store.connection)
        store.index_document(document_id="doc_d", path="f.md", body="重建索引内容")
        self.assertEqual(["doc_d"], [item["doc_id"] for item in store.search("重建")])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
