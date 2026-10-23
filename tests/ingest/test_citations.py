"""引用完整性：每个命中都必须能定位到原文对象与位置（SPEC §6.2、§16 MVP-1）。"""

from __future__ import annotations

import unittest

from asistent.contracts.citations import Citation
from asistent.contracts.search import normalize_for_index
from asistent.index.citation import build_citation, locate_quote, query_terms

from .support import MVP1TestCase


class CitationTestCase(MVP1TestCase):
    def test_every_required_field_is_present(self) -> None:
        self.bench.write("资料/唯一.md", "第一行无关\n第二行：设备常驻与部署方案说明\n第三行无关\n")
        self.bench.collect()

        response = self.bench.search("部署方案说明")
        self.assertEqual(1, response.count, [hit.path for hit in response.hits])
        hit = response.hits[0]
        self.assertIsNotNone(hit.citation)

        payload = hit.citation.to_dict()  # validate() 会在缺字段时抛错
        for field in (
            "source_object_id",
            "source_object_type",
            "path",
            "locator",
            "source_version",
            "content_hash",
            "supports",
        ):
            self.assertIn(field, payload)
            self.assertIsNotNone(payload[field], field)
        self.assertTrue(payload["content_hash"], "content_hash 必填")
        self.assertTrue(payload["supports"])
        self.assertEqual("SourceDocument", payload["source_object_type"])
        self.assertEqual(hit.doc_id, payload["source_object_id"])
        self.assertEqual(1, hit.content_version, "本文只采集过一次，content_version 应为 1")

    def test_locator_points_to_the_matching_line(self) -> None:
        self.bench.write(
            "资料/lines.md",
            "第1行：无关\n第2行：无关\n第3行：这里有百度网盘归档说明\n第4行：无关\n",
        )
        self.bench.collect()
        hit = self.bench.search("百度网盘").hits[0]
        self.assertEqual("line:3-3", hit.locator)
        self.assertIn("百度网盘", hit.excerpt)
        self.assertIn("百度网盘", hit.citation.excerpt)

    def test_locator_spans_multiple_lines(self) -> None:
        self.bench.write("资料/span.md", "部署\n方案\n落地\n")
        self.bench.collect()
        hit = self.bench.search("部署方案").hits[0]
        self.assertTrue(hit.locator.startswith("line:1-"), hit.locator)

    def test_no_quote_location_degrades_explicitly(self) -> None:
        """定位失败时不许伪造行号：明确退化为整篇并标注 locator_kind=document。"""
        location = locate_quote("完全无关的正文\n", "部署")
        self.assertEqual("document", location.locator_kind)
        self.assertEqual("line:1-2", location.locator)

    def test_citation_describes_human_readable_location(self) -> None:
        self.bench.write("资料/a.md", "张三的邮箱地址\n部署方案\n")
        self.bench.collect()
        hit = self.bench.search("部署").hits[0]
        described = hit.describe()
        self.assertIn("SourceDocument", described)
        self.assertIn("资料/a.md", described)
        self.assertIn("line:", described)

    def test_query_terms_helper_matches_contract_grouping(self) -> None:
        self.assertEqual(["部署方案"], query_terms("部署方案"))
        self.assertEqual(["部署", "deploy"], query_terms("部署 deploy"))
        # 连写的中英串在契约层被拆成"英文词 + 中文短语"两项
        self.assertEqual(["deploy", "部署"], query_terms("deploy部署"))
        self.assertEqual([], query_terms("!!!"))

    def test_citation_hash_matches_document_content_hash(self) -> None:
        self.bench.write("资料/a.md", "哈希一致性：部署\n")
        self.bench.collect()
        document = self.bench.document_by_relative("资料/a.md")
        hit = self.bench.search("部署").hits[0]
        self.assertEqual(document.content_hash, hit.content_hash)
        self.assertEqual(document.content_hash, hit.citation.content_hash)

    def test_citation_carries_source_time_from_document(self) -> None:
        self.bench.write("资料/a.md", "时间来源：部署\n")
        self.bench.collect()
        document = self.bench.document_by_relative("资料/a.md")
        hit = self.bench.search("部署").hits[0]
        self.assertEqual(document.received_at, hit.citation.source_time)

    def test_citation_without_required_fields_is_rejected_by_contract(self) -> None:
        response = self.bench.search("nonexistent")
        self.assertEqual([], response.hits, "无命中时不应生成引用")
        with self.assertRaises(ValueError):
            Citation(
                source_object_id="doc_x",
                source_object_type="SourceDocument",
                path="a.md",
                locator="",
                content_hash="deadbeef",
                supports="缺少 locator",
            ).validate()

    def test_raw_body_for_citation_is_original_not_normalized(self) -> None:
        """引用必须基于原文：normalized 版本丢了大小写与连写形态。"""
        raw = "Deploy 部署 Checklist\n"
        self.bench.write("资料/case.md", raw)
        self.bench.collect()
        stored = self.bench.connection.execute(
            "SELECT body, raw_body FROM document_index LIMIT 1"
        ).fetchone()
        self.assertEqual(normalize_for_index(raw), stored[0])
        self.assertEqual(raw, stored[1])
        hit = self.bench.search("部署").hits[0]
        self.assertIn("Checklist", hit.excerpt, "引用摘录必须来自原文，而不是小写切分后的版本")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
