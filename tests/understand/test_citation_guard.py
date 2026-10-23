"""MVP-3 引用守卫测试（SPEC §6.2、§7.4：结论必须有可定位证据）。

要证明的事：
* 没有引用的内容**不可能**成为 `Fact`（缺 path/locator/content_hash/supports 直接失败）；
* 落库的每条 `Fact` 至少一条 `Citation`，且引用能回到原文行；
* 原文对象**不被覆盖**（内容版本只增不改，身份不变）。
"""

from __future__ import annotations

import unittest

from asistent.contracts import Citation
from asistent.contracts.citations import EvidenceSet
from asistent.contracts.errors import ValidationError
from asistent.understand import (
    CitationRef,
    TimeField,
    UnderstandingResult,
    extract,
)
from asistent.contracts.timeutil import TemporalField

from .helpers import (
    UnderstandTestCase,
    facts_of,
    fixture_path,
    make_document,
    parse_locator,
    process,
    raw_extract,
)


def _good_citation() -> Citation:
    return Citation(
        source_object_id="doc_1",
        source_object_type="SourceDocument",
        path="fixtures/public/understand/01-notice-multi-time.eml",
        locator="1-3",
        content_hash="sha256:001",
        supports="支持「报名截止时间」这一时间字段",
    )


class CitationGuardTests(UnderstandTestCase):
    def test_citation_ref_require_rejects_incomplete(self):
        """缺 path / locator / content_hash / supports 的引用构造即失败。

        契约的 `Citation.validate()` 抛 `ValueError`（`contracts/citations.py`），
        理解层的 `CitationRef.require()` 是**唯一入口**，所以这里断言 `ValueError`。
        """
        for field, value in (
            ("path", ""),
            ("locator", ""),
            ("content_hash", ""),
            ("supports", ""),
        ):
            payload = _good_citation()
            setattr(payload, field, value)
            with self.assertRaises(ValueError, msg="%s 为空时未被拦住" % field):
                CitationRef.require(payload)

    def test_time_field_requires_citation(self):
        """时间字段没有引用 → `TimeField.validate()` 失败。"""
        field = TimeField(
            temporal=TemporalField(
                original_label="报名截止时间",
                semantic="registration_deadline",
                original_text="10月20日 17:00",
                normalized_utc="2026-10-20T09:00:00.000Z",
            ),
            citations=[],
        )
        with self.assertRaises(ValidationError):
            field.validate()

    def test_understanding_result_rejects_fact_without_citation(self):
        """`UnderstandingResult.validate()` 拒绝"没有引用的结论"。"""
        result = UnderstandingResult(
            source_object_id="doc_1",
            path="x.eml",
            content_hash="sha256:x",
        )
        result.facts.append(EvidenceSet(conclusion="报名截止时间是 10月20日", citations=[]))
        with self.assertRaises(ValidationError):
            result.validate()

    def test_every_persisted_fact_has_citation_with_locator(self):
        """落库的每条 Fact：至少 1 条引用，且 path/locator/content_hash/supports 齐全。"""
        document = make_document(self.store, "01-notice-multi-time.eml")
        report = process(self.store, document)
        facts = facts_of(self.store, report.case_id)
        self.assertTrue(facts)
        for fact in facts:
            self.assertTrue(fact.citations, "Fact %s 没有引用" % fact.id)
            for payload in fact.citations:
                self.assertHasCitation(payload, owner=fact.id)
                start, end = parse_locator(payload["locator"])
                self.assertGreaterEqual(start, 1)
                self.assertGreaterEqual(end, start)

    def test_citation_locator_points_into_the_original_file(self):
        """引用行号必须落在原始文件的行数范围内（能被打开、能定位）。"""
        import io

        name = "01-notice-multi-time.eml"
        with io.open(fixture_path(name), "r", encoding="utf-8") as handle:
            total_lines = len(handle.read().splitlines())
        result = raw_extract(name)
        self.assertTrue(result.time_fields)
        for field in result.time_fields:
            for ref in field.citations:
                start, end = parse_locator(ref.citation.locator)
                self.assertLessEqual(start, total_lines)
                self.assertLessEqual(end, total_lines)

    def test_citations_can_be_opened_to_the_same_text(self):
        """引用的 locator 指向原文里真的含有该字段的那几行。"""
        import io

        name = "01-notice-multi-time.eml"
        with io.open(fixture_path(name), "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        result = raw_extract(name)
        field = [item for item in result.time_fields if item.semantic == "registration_deadline"][0]
        start, end = parse_locator(field.citations[0].citation.locator)
        text = "\n".join(lines[start - 1 : end])
        self.assertIn("报名截止时间", text)
        self.assertIn("10月20日", text)

    def test_inference_allowed_without_citation(self):
        """没有引用的内容只能落成 Inference（不是 Fact）。"""
        document = make_document(self.store, "05-reply-needed-ok.eml")
        report = process(self.store, document)
        inference_kinds = [item.detail for item in report.outcomes if item.kind == "Inference"]
        self.assertTrue(inference_kinds)
        # 推断对象在 store 里存在，且**不**出现在 Fact 的集合里
        inferences = self.store.find("Inference", where={"case_id": report.case_id}, limit=None)
        self.assertTrue(inferences)
        for item in inferences:
            self.assertNotIn(item.id, [fact.id for fact in facts_of(self.store, report.case_id)])

    def test_original_document_is_not_overwritten(self):
        """处理前后 `SourceDocument` 的身份与内容哈希不变（原始资料只读）。"""
        document = make_document(self.store, "05-reply-needed-ok.eml")
        before = self.store.get("SourceDocument", document.id)
        process(self.store, document)
        after = self.store.get("SourceDocument", document.id)
        self.assertEqual(before.content_hash, after.content_hash)
        self.assertEqual(before.origin_path, after.origin_path)
        self.assertEqual(before.identity_key, after.identity_key)
        self.assertEqual(1, after.content_version)

    def test_extract_is_a_pure_function(self):
        """同一输入 → 同一输出（确定性规则集，没有随机/时间依赖）。"""
        first = raw_extract("01-notice-multi-time.eml")
        second = raw_extract("01-notice-multi-time.eml")
        self.assertEqual(first.to_dict()["time_fields"], second.to_dict()["time_fields"])
        self.assertEqual(first.to_dict()["questions"], second.to_dict()["questions"])

    def test_ruleset_is_versioned_and_recorded(self):
        """抽取结果带规则集版本：换了规则，旧结论可被识别出来。"""
        from asistent.understand import RULESET_VERSION

        document = make_document(self.store, "05-reply-needed-ok.eml")
        report = process(self.store, document)
        self.assertEqual(RULESET_VERSION, report.producer)
        self.assertIn("rules:", RULESET_VERSION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
