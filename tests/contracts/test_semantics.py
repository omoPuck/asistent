"""语义契约测试：时间字段、引用、后端可替换（SPEC §6.2、§7.4、§10.2、§21.1、§21.4、AT-02/AT-06）。"""

from __future__ import annotations

import unittest

from asistent.contracts import (
    Citation,
    EchoBackend,
    EvidenceSet,
    ExecutionRequest,
    ExecutionResult,
    FailureClass,
    PermissionDecision,
    ScriptedBackend,
    TemporalField,
    lines_citation,
    permission_decision,
)
from asistent.contracts.timeutil import display_tz, resolve_relative_date


class TemporalSemanticsTest(unittest.TestCase):
    def test_deadline_and_event_start_are_different_slots(self) -> None:
        """SPEC §7.4 回归规则：不得把报名截止时间当成活动开始时间。"""
        deadline = TemporalField(
            original_label="报名截止时间",
            semantic="registration_deadline",
            original_text="10月12日 23:59",
            normalized_utc="2026-10-12T15:59:00.000Z",
            evidence=["doc_1"],
        )
        deadline.validate()
        self.assertNotEqual(deadline.semantic, "event_start")

        missing_start = TemporalField(
            original_label="活动开始时间",
            semantic="unknown",
            original_text="待定",
        )
        missing_start.validate()
        self.assertIsNone(missing_start.normalized_utc)
        self.assertNotEqual(deadline.to_dict()["semantic"], missing_start.to_dict()["semantic"])

    def test_unknown_semantic_may_not_carry_normalized_time(self) -> None:
        field = TemporalField(
            original_label="时间",
            semantic="unknown",
            original_text="稍后通知",
            normalized_utc="2026-10-12T15:59:00.000Z",
        )
        with self.assertRaises(ValueError):
            field.validate()

    def test_original_label_is_mandatory(self) -> None:
        field = TemporalField(original_label="", semantic="event_start", original_text="10月12日 10:00")
        with self.assertRaises(ValueError):
            field.validate()

    def test_relative_dates_anchor_to_email_time_not_processing_day(self) -> None:
        """「明天」按原文发出时间解释（SPEC §21.1）。"""
        # 原文发出于 2026-10-10 23:30（上海）= 2026-10-10T15:30Z
        resolved = resolve_relative_date("明天", anchor_iso="2026-10-10T15:30:00.000Z", tz_name="Asia/Shanghai")
        self.assertEqual("2026-10-11", resolved)
        # 若按 UTC 当天理解会得到 10-11 之外的日期；这里再验证跨日边界
        resolved_late_night = resolve_relative_date("今天", anchor_iso="2026-10-10T16:30:00.000Z", tz_name="Asia/Shanghai")
        self.assertEqual("2026-10-11", resolved_late_night)

    def test_undeterminable_relative_date_raises_for_question(self) -> None:
        with self.assertRaises(ValueError):
            resolve_relative_date("下下周某天", anchor_iso="2026-10-10T15:30:00.000Z", tz_name="Asia/Shanghai")

    def test_display_timezone_defaults_to_shanghai(self) -> None:
        self.assertIsNotNone(display_tz())


class CitationSemanticsTest(unittest.TestCase):
    def test_citation_requires_locator_hash_and_conclusion(self) -> None:
        with self.assertRaises(ValueError):
            Citation(source_object_id="doc_1", source_object_type="SourceDocument", path="a.md", locator="", supports="x", content_hash="h").validate()
        with self.assertRaises(ValueError):
            Citation(source_object_id="doc_1", source_object_type="SourceDocument", path="a.md", locator="line:1", supports="", content_hash="h").validate()
        with self.assertRaises(ValueError):
            Citation(source_object_id="doc_1", source_object_type="SourceDocument", path="a.md", locator="line:1", supports="x", content_hash="").validate()

    def test_citation_describe_is_locatable(self) -> None:
        citation = lines_citation(
            source_object_id="doc_9",
            path="资料库/联系人/张三.md",
            start_line=12,
            end_line=18,
            content_hash="abc123",
            supports="张三的手机号是 13800000000",
            source_version=3,
        )
        text = citation.describe()
        self.assertIn("doc_9", text)
        self.assertIn("line:12-18", text)
        self.assertIn("v3", text)

    def test_evidence_set_separates_inference_from_fact(self) -> None:
        citation = lines_citation(
            source_object_id="doc_1", path="a.md", start_line=1, end_line=2, content_hash="h", supports="结论"
        )
        evidence = EvidenceSet(conclusion="建议直接回复确认", citations=[citation], contains_inference=True)
        payload = evidence.to_dict()
        self.assertTrue(payload["contains_inference"])
        self.assertEqual(1, len(payload["citations"]))


class PermissionAndBackendTest(unittest.TestCase):
    def test_permission_decisions_follow_spec_table(self) -> None:
        self.assertEqual(PermissionDecision.REQUIRE_APPROVAL, permission_decision("send_email"))
        self.assertEqual(PermissionDecision.REQUIRE_APPROVAL, permission_decision("submit_form"))
        self.assertEqual(PermissionDecision.REQUIRE_APPROVAL, permission_decision("external_delete"))
        self.assertEqual(PermissionDecision.AUTO, permission_decision("local_search"))
        self.assertEqual(PermissionDecision.AUTO, permission_decision("index_update"))
        self.assertEqual(PermissionDecision.REQUIRE_APPROVAL, permission_decision("archive_upload"))
        self.assertEqual(PermissionDecision.AUTO, permission_decision("archive_upload", archive_policy_covers=True))
        self.assertEqual(PermissionDecision.FORBIDDEN, permission_decision("script:cleanup"))

    def test_backends_are_interchangeable(self) -> None:
        """AT-02：换后端不影响对象/审批语义。"""
        request = ExecutionRequest(purpose="extract_temporal_fields", inputs={"text": "报名截止 10月12日"})
        echo = EchoBackend()
        first = echo.execute(request)
        scripted = ScriptedBackend([])
        second = scripted.execute(request)
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(request.digest(), first.structured["inputs_digest"])
        self.assertEqual(1, len(scripted.calls))
        self.assertEqual("echo", echo.name)

    def test_execution_request_digest_is_stable(self) -> None:
        left = ExecutionRequest(purpose="analyze", inputs={"a": 1, "b": [2, 3]}, allowed_tools=["read", "write"])
        right = ExecutionRequest(purpose="analyze", inputs={"b": [2, 3], "a": 1}, allowed_tools=["write", "read"])
        self.assertEqual(left.digest(), right.digest())

    def test_failure_result_carries_failure_class(self) -> None:
        result = ExecutionResult.failure(FailureClass.NETWORK_FAILURE, "连接被重置")
        self.assertFalse(result.ok)
        self.assertEqual("NETWORK_FAILURE", result.failure_class)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
