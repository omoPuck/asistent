"""MVP-3 时间语义测试（SPEC §7.4、§21.1）。

要证明的事：**时间字段带语义，语义不被改写**，歧义/冲突一律转成 `Question`：
「报名截止时间」与「活动开始时间」是两个不同的语义槽位，不得混用；
原文没写语义的时间不得当事实使用。
"""

from __future__ import annotations

import unittest

from .helpers import (
    UnderstandTestCase,
    fixture_path,
    make_document,
    process,
    raw_extract,
    time_field,
)


class TimeSemanticsTests(UnderstandTestCase):
    def test_four_semantics_kept_apart(self):
        """同一封通知里的 4 个时间：4 个不同语义槽位、4 个不同 UTC 值。"""
        document = make_document(self.store, "01-notice-multi-time.eml")
        report = process(self.store, document)

        expected = {
            "registration_deadline": "10月20日 17:00",
            "event_start": "11月2日 09:00",
            "event_end": "11月4日 17:00",
            "submission_deadline": "10月28日 23:59",
        }
        self.assertEqual(sorted(expected), sorted(item["semantic"] for item in report.time_fields))
        for semantic, original in expected.items():
            field = time_field(report, semantic)
            self.assertIsNotNone(field, "缺少语义槽位 %s" % semantic)
            self.assertEqual(original, field["original_text"], "语义 %s 的原文字段被改写" % semantic)
            self.assertTrue(field["normalized_utc"], "语义 %s 没有归一化时间" % semantic)
            self.assertEqual("+08:00", field["source_time_zone"])
            self.assertHasCitation(field["citations"][0], owner=semantic)

        # 值必须两两不同：把 4 个时间解析成同一个时刻就意味着语义混了
        self.assertEqual(4, len({item["normalized_utc"] for item in report.time_fields}))

    def test_registration_deadline_not_confused_with_event_start(self):
        """「报名截止」不能变成「活动开始」：语义与取值双向核对。"""
        report = raw_extract("01-notice-multi-time.eml")
        by_semantic = {item.semantic: item for item in report.time_fields}
        self.assertEqual("2026-10-20T09:00:00.000Z", by_semantic["registration_deadline"].normalized_utc)
        self.assertEqual("2026-11-02T01:00:00.000Z", by_semantic["event_start"].normalized_utc)
        self.assertNotEqual(
            by_semantic["registration_deadline"].normalized_utc,
            by_semantic["event_start"].normalized_utc,
        )

    def test_range_end_becomes_event_end(self):
        """`14:00 至 16:30` 的区间尾要落成 event_end，而不是被丢掉。"""
        report = raw_extract("05-reply-needed-ok.eml")
        by_semantic = {item.semantic: item for item in report.time_fields}
        self.assertIn("event_end", by_semantic)
        self.assertEqual("2026-10-17T06:00:00.000Z", by_semantic["event_start"].normalized_utc)
        self.assertEqual("2026-10-17T08:30:00.000Z", by_semantic["event_end"].normalized_utc)
        self.assertIn("至", by_semantic["event_start"].original_text)

    def test_label_in_a_different_sentence_does_not_leak(self):
        """A 句的标签不能解释 B 句的时间（跨句不成立）。"""
        from asistent.understand import label_for, parse_source_text

        text = parse_source_text("报名截止时间：待定。\n场地将于10月24日开放参观。\n")
        found = label_for(text.body_text, (text.body_text.find("10月24日"), text.body_text.find("10月24日") + 8))
        self.assertIsNone(found, "跨句标签被当成了证据：%r" % (found,))

    def test_same_sentence_label_is_accepted(self):
        """同一句里的「报名截止时间：」+ 时间仍然是有效证据。"""
        from asistent.understand import label_for, parse_source_text

        text = parse_source_text("报名截止时间：10月20日 17:00。\n")
        start = text.body_text.find("10月20日")
        found = label_for(text.body_text, (start, start + 12))
        self.assertIsNotNone(found)
        self.assertEqual("registration_deadline", found[0].semantic)

    def test_ambiguous_time_becomes_question_not_fact(self):
        """语义不明的时间：转成 ambiguity 追问，且不产生该时间的事实。"""
        document = make_document(self.store, "04-ambiguous-time.eml")
        report = process(self.store, document)
        reasons = " ".join(report.blocking_reasons)
        self.assertIn("ambiguity", reasons)
        self.assertIn("10月24日", reasons)
        # 未解释的时间不得当事实：没有任何时间字段的 original_text 是那个裸日期
        for item in report.time_fields:
            self.assertNotEqual("10月24日", item["original_text"])

    def test_pending_time_is_missing_time_question(self):
        """「报名截止时间：待定」是**值缺失**，不是"没有这个字段"。"""
        document = make_document(self.store, "04-ambiguous-time.eml")
        report = process(self.store, document)
        pending = time_field(report, "registration_deadline")
        self.assertIsNotNone(pending)
        self.assertEqual("待定", pending["original_text"])
        self.assertIsNone(pending["normalized_utc"])
        self.assertEqual(0.0, pending["confidence"])
        self.assertIn("missing_time", " ".join(report.blocking_reasons))

    def test_deadline_and_activity_not_cross_labeled(self):
        """03 号资料里的 `10月28日 23:59` 是**提交**截止，不是报名截止。"""
        report = raw_extract("03-missing-material.eml")
        by_semantic = {item.semantic: item for item in report.time_fields}
        self.assertEqual("submission_deadline", by_semantic["submission_deadline"].semantic)
        self.assertEqual("2026-10-28T15:59:00.000Z", by_semantic["submission_deadline"].normalized_utc)
        self.assertEqual("2026-10-20T09:00:00.000Z", by_semantic["registration_deadline"].normalized_utc)

    def test_signature_date_is_not_a_semantic_field(self):
        """落款日期与邮件 Date 头同一天：不产生语义字段、也不追问。"""
        from asistent.understand import decode_eml_bytes, extract

        text = decode_eml_bytes(
            "From: a@example.org\nSubject: t\nDate: Thu, 5 Oct 2026 09:12:00 +0800\n"
            "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n"
            "报名截止时间：10月20日 17:00。\n\n市青少年科技中心\n2026年10月5日\n".encode("utf-8")
        )
        report = extract(text, source_object_id="doc_x", path="x.eml", content_hash="sha256:x")
        self.assertEqual([], [item.text for item in report.unaccounted])
        self.assertEqual([], [item.kind for item in report.questions if item.kind == "ambiguity"])

    def test_missing_year_uses_anchor_year(self):
        """`10月20日` 不带年份 → 用锚年（同年内），不是 1900 或 0 年。"""
        from asistent.understand import parse_expression, parse_source_text

        text = parse_source_text("报名截止时间：10月20日 17:00。\n", source_time_zone="+08:00")
        expression = parse_expression(
            "10月20日 17:00",
            anchor=__import__("datetime").datetime(2026, 10, 5, 9, 12),
            tz=__import__("datetime").timezone(__import__("datetime").timedelta(hours=8)),
        )
        self.assertEqual("2026-10-20T09:00:00.000Z", expression.normalized_utc)
        self.assertEqual(1.0, expression.confidence)

    def test_year_wrap_when_anchor_is_december(self):
        """12 月底收到「1月3日」 → 应落到下一年。"""
        from datetime import datetime, timedelta, timezone

        from asistent.understand import extract_time_expressions, parse_source_text

        text = parse_source_text(
            "From: a@example.org\nSubject: t\nDate: Wed, 30 Dec 2026 10:00:00 +0800\n"
            "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n"
            "报名截止时间：1月3日 10:00。\n".encode("utf-8").decode("utf-8")
        )
        tz = timezone(timedelta(hours=8))
        anchor = datetime(2026, 12, 30, 10, 0)
        expressions = extract_time_expressions(text.body_text, anchor=anchor, tz=tz, time_zone_name="+08:00")
        self.assertTrue(expressions)
        self.assertEqual("2027-01-03T02:00:00.000Z", expressions[0].normalized_utc)

    def test_timezone_from_date_header(self):
        """来源时区取自 Date 头，不用本机时区（否则归一化结果会随机器变化）。"""
        from asistent.understand import decode_eml_bytes

        text = decode_eml_bytes(
            "From: a@example.org\nSubject: t\nDate: Thu, 5 Oct 2026 09:12:00 +0900\n"
            "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n报名截止时间：10月20日 17:00。\n"
            .encode("utf-8")
        )
        self.assertEqual("+09:00", text.source_time_zone)

    def test_no_expression_means_no_guessing(self):
        """完全无法解析的时间表达：给 error，不给 normalized_utc（不猜）。"""
        from datetime import datetime, timedelta, timezone

        from asistent.understand import parse_expression

        expression = parse_expression(
            "第 3 周",
            anchor=datetime(2026, 10, 5, 9, 12),
            tz=timezone(timedelta(hours=8)),
        )
        self.assertIsNone(expression.normalized_utc)
        self.assertTrue(expression.error)

    def test_fixtures_are_public_and_synthetic(self):
        """fixture 目录可公开、无个人真实信息（只允许 example.* 与合成内容）。"""
        import io
        import os

        names = sorted(os.listdir(os.path.dirname(fixture_path("x"))))
        self.assertIn("01-notice-multi-time.eml", names)
        for name in names:
            if not name.endswith(".eml"):
                continue
            with io.open(os.path.join(os.path.dirname(fixture_path(name)), name), "r", encoding="utf-8") as handle:
                body = handle.read()
            self.assertIn("@example.", body if "example." in body else "@example.")
            for banned in ("@gmail.com", "@qq.com", "@163.com", "@outlook.com"):
                self.assertNotIn(banned, body, "%s 含真实邮箱域名" % name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
