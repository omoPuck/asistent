"""MVP-3 抽取层测试（SPEC §7.4、§21.1）。

要证明的事：依赖规则/模板的默认路径是**确定性**的，而且"抽不出来"时**不猜**：
材料只从清单语境里取、待办的 `due` 必须绑到明确的时间字段、解释不清的时间一律转追问。
"""

from __future__ import annotations

import unittest

from asistent.understand import (
    MATERIAL_MARKERS,
    NO_REPLY_MARKERS,
    REQUIREMENT_MARKERS,
    extract,
    parse_source_text,
    reply_requested,
)

from .helpers import UnderstandTestCase, fixture_path, make_document, process, raw_extract


class RequirementExtractionTests(UnderstandTestCase):
    def test_todos_from_fixture_1(self):
        """01 号通知的 3 条待办都被抽出，并且每条都带引用。"""
        result = raw_extract("01-notice-multi-time.eml")
        self.assertEqual(3, len(result.todos))
        for todo in result.todos:
            self.assertTrue(todo.citations, "待办 %r 没有引用" % todo.title)
            self.assertTrue(todo.title.strip())
            self.assertEqual("user", todo.assignee)

    def test_todo_fragment_is_not_duplicated(self):
        """同一个要求命中两个标记时只保留更完整的一条（不产出碎片）。"""
        result = raw_extract("01-notice-multi-time.eml")
        titles = [" ".join(item.title.split()) for item in result.todos]
        for index, title in enumerate(titles):
            for other_index, other in enumerate(titles):
                if index == other_index:
                    continue
                self.assertFalse(
                    other and other in title and len(other) < len(title),
                    "短碎片 %r 与长条目 %r 同时存在" % (other, title),
                )

    def test_todo_due_binds_to_semantic_slot(self):
        """`于报名截止前回复` 的 due 绑到 registration_deadline，而不是留空或绑错槽位。"""
        result = raw_extract("01-notice-multi-time.eml")
        bound = [item for item in result.todos if item.due is not None]
        self.assertTrue(bound, "没有任何待办绑定了截止时间")
        semantics = {item.due_semantic for item in bound}
        self.assertIn("registration_deadline", semantics)
        for item in bound:
            self.assertTrue(item.due.normalized_utc, "待办 due 字段没有归一化时间")
            self.assertEqual(item.due_semantic, item.due.semantic)

    def test_reply_requested_detects_and_rejects(self):
        """回复要求识别：有「请于…前回复」是 True；「无需回复」必须被读成 False。"""
        self.assertTrue(reply_requested(parse_source_text("请于报名截止前回复本邮件确认参加。").body_text))
        self.assertFalse(reply_requested(parse_source_text("现将活动安排通知如下，无需回复。").body_text))
        self.assertFalse(reply_requested(parse_source_text("本邮件为系统通知，不必回复。").body_text))

    def test_markers_are_declared_data_not_inline_regex(self):
        """要求/材料标记是**声明式表**（可审阅、可扩展），不是散落的行内判断。"""
        self.assertIn("请回复", REQUIREMENT_MARKERS)
        self.assertIn("缺少", MATERIAL_MARKERS)
        self.assertIn("无需回复", NO_REPLY_MARKERS)
        self.assertTrue(all(isinstance(item, str) and item for item in REQUIREMENT_MARKERS))
        # 否定式整体是一个标记，调用方先查否定表再查要求表（不再靠子串巧合）
        self.assertTrue(any("回复" in marker for marker in NO_REPLY_MARKERS))


class MaterialExtractionTests(UnderstandTestCase):
    def test_materials_only_from_list_context(self):
        """材料只在清单语境里取：清单外的动宾短语（`说明补交计划`）不得成为材料。"""
        result = raw_extract("03-missing-material.eml")
        names = [item.name for item in result.materials]
        self.assertEqual(["监护人知情同意书", "学生一寸免冠照片"], names)
        for bad in ("的材料", "计划", "材料"):
            self.assertNotIn(bad, names, "%r 被当成了材料名" % bad)

    def test_material_status_distinguishes_missing_and_to_submit(self):
        """`缺少…` → missing；`请提交…` → to_submit（两种状态语义不同）。"""
        missing = raw_extract("03-missing-material.eml")
        statuses = {item.name: item.status for item in missing.materials}
        self.assertEqual("missing", statuses["监护人知情同意书"])
        submitted = raw_extract("01-notice-multi-time.eml")
        self.assertEqual([], [item.name for item in submitted.materials])

    def test_material_items_have_citations(self):
        result = raw_extract("03-missing-material.eml")
        self.assertTrue(result.materials)
        for item in result.materials:
            self.assertTrue(item.citations, "材料 %r 没有引用" % item.name)

    def test_no_material_marker_means_no_material(self):
        """没有任何材料标记时不得凭空产出材料项。"""
        result = extract(
            parse_source_text("尊敬的家长：活动安排如下。请于10月20日前回复本邮件确认参加。"),
            source_object_id="doc_x",
            path="x.eml",
            content_hash="sha256:x",
        )
        self.assertEqual([], list(result.materials))


class AmbiguityExtractionTests(UnderstandTestCase):
    def test_bare_date_outside_signature_becomes_ambiguity(self):
        """正文里出现、没有标签的裸日期 → 转成 ambiguity 追问，不当事实。"""
        result = extract(
            parse_source_text(
                "From: a@example.org\nDate: Thu, 1 Oct 2026 09:00:00 +0800\n"
                "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n"
                "各位家长：场地将于10月24日开放参观。\n\n中心\n2026年10月1日\n",
                source_time_zone="+08:00",
                anchor_utc="2026-10-01T01:00:00.000Z",
            ),
            source_object_id="doc_x",
            path="x.eml",
            content_hash="sha256:x",
        )
        kinds = [item.kind for item in result.questions]
        self.assertIn("ambiguity", kinds)
        self.assertEqual(1, len(result.unaccounted))
        self.assertEqual("10月24日", result.unaccounted[0].text)

    def test_same_date_as_anchor_is_signature(self):
        """与锚同日期的裸日期视为落款，不追问（否则每封邮件都多一条噪声追问）。"""
        result = extract(
            parse_source_text(
                "From: a@example.org\nDate: Thu, 1 Oct 2026 09:00:00 +0800\n"
                "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n"
                "各位家长：活动安排如下。\n\n中心\n2026年10月1日\n",
                source_time_zone="+08:00",
                anchor_utc="2026-10-01T01:00:00.000Z",
            ),
            source_object_id="doc_x",
            path="x.eml",
            content_hash="sha256:x",
        )
        self.assertEqual([], [item.text for item in result.unaccounted])

    def test_same_date_in_the_display_timezone_is_signature(self):
        """锚在 UTC 是前一天、但在来源时区与落款同日 → 仍然识别为落款。

        `Date: 8 Oct 2026 00:30 +0800` 换成 UTC 是 2026-10-07T16:30Z；如果按 UTC 比日期，
        落款「2026年10月8日」会被误判成"另一个时间"，从而多出一条噪声追问。
        """
        result = extract(
            parse_source_text(
                "From: a@example.org\nDate: Thu, 8 Oct 2026 00:30:00 +0800\n"
                "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n"
                "各位家长：场地将于10月20日开放参观。\n\n中心\n2026年10月8日\n",
                source_time_zone="+08:00",
            ),
            source_object_id="doc_x",
            path="x.eml",
            content_hash="sha256:x",
        )
        self.assertEqual(["10月20日"], [item.text for item in result.unaccounted])
        # 落款（与锚在来源时区同一天）不得出现在任何追问里
        for question in result.questions:
            self.assertNotIn("2026年10月8日", question.question)
            self.assertNotIn("10月8日", question.question)


class HeaderDecodingTests(UnderstandTestCase):
    def test_non_ascii_subject_is_decoded(self):
        """非 ASCII 直写 Subject 不能被解成一串 U+FFFD（实测踩过）。"""
        from asistent.understand import decode_eml_bytes

        text = decode_eml_bytes(
            "From: \"市图书馆少儿部\" <kids@example.org>\nSubject: 周末科普讲座报名确认（含活动时间）\n"
            "Date: Thu, 8 Oct 2026 16:45:00 +0800\nMIME-Version: 1.0\n"
            "Content-Type: text/plain; charset=utf-8\n\n正文\n".encode("utf-8")
        )
        self.assertEqual("周末科普讲座报名确认（含活动时间）", text.subject)
        self.assertNotIn("\ufffd", text.subject)
        self.assertIn("市图书馆少儿部", text.from_addr)

    def test_rfc2047_subject_is_decoded(self):
        """RFC 2047 编码头同样要能解开。"""
        from asistent.understand import decode_eml_bytes

        text = decode_eml_bytes(
            "From: a@example.org\nSubject: =?utf-8?B?5rS75Yqo6YCa55+l?=\n"
            "Date: Thu, 8 Oct 2026 16:45:00 +0800\nMIME-Version: 1.0\n"
            "Content-Type: text/plain; charset=utf-8\n\n正文\n".encode("utf-8")
        )
        self.assertEqual("活动通知", text.subject)

    def test_fixture_subject_is_readable(self):
        """每个 fixture 的 Subject 都能读出来（没有编码残留）。"""
        for name in (
            "01-notice-multi-time.eml",
            "03-missing-material.eml",
            "04-ambiguous-time.eml",
            "05-reply-needed-ok.eml",
        ):
            result = raw_extract(name)
            self.assertTrue(result.source.subject.strip(), "%s 的 Subject 为空" % name)
            self.assertNotIn("\ufffd", result.source.subject, "%s 的 Subject 含替换字符" % name)

    def test_body_line_numbers_are_relative_to_original_file(self):
        """`body_start_line` 让引用行号能落回**原始文件**，而不是裸正文。"""
        import io

        result = raw_extract("01-notice-multi-time.eml")
        with io.open(fixture_path("01-notice-multi-time.eml"), "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertGreater(result.source.body_start_line, 1)
        offset = result.source.body_start_line - 1
        first_body_line = result.source.body_text.splitlines()[0]
        self.assertEqual(first_body_line, lines[offset])
        # `line_of(正文行号)` 必须映射回原始文件里含该文本的那一行
        body_lines = result.source.body_text.splitlines()
        target = [index for index, line in enumerate(body_lines, start=1) if "报名截止时间" in line][0]
        self.assertIn("报名截止时间", lines[result.source.line_of(target) - 1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
