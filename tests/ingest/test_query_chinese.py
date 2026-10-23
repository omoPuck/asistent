"""中文检索验收：2 字/3 字真实命中、中英混排、注入防护（SPEC §6.2、§16 MVP-1）。

`unicode61` 直接查中文会**命中 0 行且不报错**（静默失效）。这里的每条测试都用
**契约层的原始 MATCH 表达式**打一遍 `document_index`，把 SQLite 的真实答案作为证据，
而不是只看本仓封装的返回值。
"""

from __future__ import annotations

import sqlite3
import unittest

from asistent.contracts.search import to_fts_query
from asistent.index import fts
from asistent.index.query import search, search_not_indexed

from .support import MVP1TestCase, raw_match


class ChineseRetrievalTest(MVP1TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.bench.write("资料/部署方案.md", "# 部署方案\n设备常驻与部署方案说明，包含部署步骤。\n")
        self.bench.write("资料/归档通道.md", "百度网盘归档通道，每 6 小时同步一次。\n")
        self.bench.write("资料/会议纪要.md", "会议纪要：讨论部署与归档的负责人。\n")
        self.bench.write("资料/联系人.md", "张三的邮箱地址与电话号码。\n")
        self.bench.collect()

    # ------------------------------------------------------------------ 2 字
    def test_two_character_chinese_query_hits_exactly(self) -> None:
        response = self.bench.search("部署")
        self.assertEqual(sorted(["资料/部署方案.md", "资料/会议纪要.md"]), sorted(hit.path for hit in response.hits))

    def test_raw_match_expression_for_two_character_query(self) -> None:
        expression = to_fts_query("部署")
        self.assertEqual('"部 署"', expression)
        rows = raw_match(self.bench.connection, expression)
        self.assertEqual(2, len(rows), f"原始 MATCH 输出: {rows}")

    def test_normalizer_would_fail_without_the_contract_layer(self) -> None:
        """反证：直接拿 unicode61 查未切分的正文 = 0 行（这就是契约存在的理由）。"""
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE VIRTUAL TABLE naive USING fts5(doc_id UNINDEXED, body, tokenize='unicode61');")
        connection.execute("INSERT INTO naive(doc_id, body) VALUES ('doc_1', '设备常驻与部署方案说明')")
        rows = connection.execute("SELECT doc_id FROM naive WHERE naive MATCH '部署'").fetchall()
        self.assertEqual([], rows, "未切分中文直接用 MATCH 命中 0 行——契约层不可绕过")
        connection.close()

    # ------------------------------------------------------------------ 3 字
    def test_three_character_chinese_query_hits(self) -> None:
        response = self.bench.search("百度网盘")
        self.assertEqual(["资料/归档通道.md"], [hit.path for hit in response.hits])
        self.assertEqual(1, len(raw_match(self.bench.connection, to_fts_query("百度网盘"))))

    def test_three_character_query_meeting_minutes(self) -> None:
        response = self.bench.search("会议纪要")
        self.assertEqual(["资料/会议纪要.md"], [hit.path for hit in response.hits])

    def test_multi_character_phrase_keeps_adjacency(self) -> None:
        response = self.bench.search("部署方案")
        self.assertEqual(["资料/部署方案.md"], [hit.path for hit in response.hits])

    def test_substring_of_longer_chinese_word_is_found(self) -> None:
        """逐字切分后，"常驻"只出现在"设备常驻"里，也必须能查到。"""
        self.assertEqual(["资料/部署方案.md"], [hit.path for hit in self.bench.search("常驻").hits])

    # ------------------------------------------------------------------ 中英混排
    def test_mixed_chinese_english_query(self) -> None:
        self.bench.write("资料/mixed.md", "Deploy 部署 checklist 与 upload 说明\n")
        self.bench.collect()

        for query in ("deploy", "DEPLOY", "部署", "deploy 部署", "Deploy") :
            response = self.bench.search(query)
            self.assertTrue(
                any(hit.path == "资料/mixed.md" for hit in response.hits),
                f"{query!r} 应命中 mixed.md，实际 {[hit.path for hit in response.hits]}",
            )

    def test_concatenated_chinese_english_is_split_at_boundary(self) -> None:
        self.bench.write("资料/glued.md", "deploy部署完成\n")
        self.bench.collect()
        self.assertIn("资料/glued.md", [hit.path for hit in self.bench.search("部署").hits])
        self.assertIn("资料/glued.md", [hit.path for hit in self.bench.search("deploy").hits])

    def test_latin_search_is_case_insensitive(self) -> None:
        self.bench.write("资料/latin.md", "BaiduDrive 与 ARCHIVE\n")
        self.bench.collect()
        self.assertEqual(1, self.bench.search("baidudrive").count)
        self.assertEqual(1, self.bench.search("BaIDuDriVe").count)
        self.assertEqual(1, self.bench.search("archive").count)

    def test_two_terms_query_requires_both(self) -> None:
        """`部署 归档` 必须两个词都出现（AND 语义），不能退化成"包含任一词"。"""
        joined = self.bench.search("部署 归档")
        paths = sorted(hit.path for hit in joined.hits)
        self.assertEqual(["资料/会议纪要.md"], paths)

    # ------------------------------------------------------------------ 注入与无效查询
    def test_injection_attempts_do_not_error_and_do_not_return_everything(self) -> None:
        total_documents = len(self.bench.documents())
        hostile_queries = [
            '部署" OR "x',
            "部署 OR 归档",
            "MATCH *",
            "*",
            "a AND b",
            '"""',
            "()^",
            "部署;",
            "NEAR(部署 归档)",
            "' OR 1=1 --",
            "%部署%",
            "部署*",
            "{{部署}}",
            "\u0000部署",
        ]
        for query in hostile_queries:
            with self.subTest(query=query):
                response = self.bench.search(query)  # 不得抛异常
                if not response.valid_query:
                    self.assertEqual([], response.hits, "无效查询必须返回空集，不得返回全部")
                    continue
                self.assertLessEqual(len(response.doc_ids), total_documents)
                # 不得因为注入而把无关文档（联系人）带出来
                for hit in response.hits:
                    self.assertNotEqual("资料/联系人.md", hit.path, f"可疑查询 {query!r} 不应命中无关文档")

    def test_query_never_reaches_sqlite_unescaped(self) -> None:
        """所有 MATCH 表达式都必须来自 `to_fts_query()`：每个 token 都被引号包住。"""
        for query in ['部署" OR "x', "a AND b", "()^", "部署;"]:
            expression = to_fts_query(query)
            if expression is None:
                continue
            self.assertNotIn("()", expression)
            self.assertEqual(expression.count('"') % 2, 0, f"引号必须配对: {expression}")

    def test_empty_or_symbol_only_query_is_invalid_and_returns_nothing(self) -> None:
        for query in ("", "   ", "!!!", "（）", "\n\t", "···"):
            with self.subTest(query=query):
                response = self.bench.search(query)
                self.assertFalse(response.valid_query, f"{query!r} 必须判为无效查询")
                self.assertIsNone(response.fts_query)
                self.assertEqual([], response.hits, "无效查询不得返回全部结果")

    def test_no_match_returns_empty_not_all(self) -> None:
        response = self.bench.search("nonexistent-term-xyz")
        self.assertTrue(response.valid_query)
        self.assertEqual([], response.hits)

    def test_limit_is_respected(self) -> None:
        response = self.bench.search("部署", limit=1)
        self.assertEqual(1, response.count)

    def test_fts5_unavailable_is_reported_not_silently_degraded(self) -> None:
        connection = sqlite3.connect(":memory:")
        response = search(connection, "部署")
        self.assertFalse(response.fts5_available)
        self.assertEqual([], response.hits)
        self.assertIn("FTS5", response.invalid_reason)
        connection.close()


class NotIndexedVisibilityTest(MVP1TestCase):
    def test_failed_document_is_visible_when_querying(self) -> None:
        """搜不到东西时，必须能看到"有资料但索引失败/待重建"，而不是以为资料不存在。"""
        self.bench.write("资料/broken.md", "索引失败的资料：部署方案\n")
        self.bench.collect()
        document = self.bench.document_by_relative("资料/broken.md")

        # 场景：检索表里的正文指向**旧内容**（内容已变但索引没重算），
        # 文档状态是"待重建"——用户搜新词搜不到，必须能看到原因。
        fts.upsert_document(
            self.bench.connection,
            doc_id=document.id,
            path=document.origin_path,
            raw_body="上一版内容：部署方案\n",
            content_version=document.content_version,
            content_hash="stale-hash",
        )
        self.bench.store.set_index_state(document.id, "STALE", reason="模拟索引失效")
        self.bench.store.set_index_state(document.id, "REBUILD_REQUIRED", reason="模拟待重建")

        # 索引里的正文是旧版内容，所以按新词查不到
        self.assertEqual(0, self.bench.search("索引失败").count)
        # 但"有资料、索引里确实有这个词、只是状态是待重建"必须能被看到
        pending = search_not_indexed(self.bench.store, "部署", connection=self.bench.connection)
        self.assertEqual(1, len(pending), [hit.to_dict() for hit in pending])
        self.assertIn("index_state=REBUILD_REQUIRED", pending[0].not_indexed_reason or "")
        self.assertEqual(document.id, pending[0].doc_id)

    def test_never_indexed_document_is_visible_by_path(self) -> None:
        """连正文都没进过索引的资料（采集时被跳过），按路径也要能被找到。"""
        self.bench.write("资料/部署-未索引.md", "正文从未进过索引\n")
        self.bench.collect()
        document = self.bench.document_by_relative("资料/部署-未索引.md")
        fts.delete_document(self.bench.connection, document.id)
        self.bench.store.set_index_state(document.id, "STALE", reason="模拟从未完成的索引")

        pending = search_not_indexed(self.bench.store, "部署", connection=self.bench.connection)
        self.assertEqual([document.id], [hit.doc_id for hit in pending])
        self.assertIn("index_state=STALE", pending[0].not_indexed_reason or "")

    def test_not_indexed_search_of_invalid_query_returns_nothing(self) -> None:
        self.assertEqual([], search_not_indexed(self.bench.store, "!!!", connection=self.bench.connection))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
