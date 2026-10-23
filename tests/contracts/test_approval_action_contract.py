"""审批与外部动作契约：一次性发送保护、并发领取、UNKNOWN 不重发（SPEC §7.5、§8、§21.2）。"""

from __future__ import annotations

import threading
import unittest

from asistent.contracts import (
    ActionNotClaimable,
    ActionSummary,
    Approval,
    ApprovalInvalid,
    AttachmentRef,
    check_approval_valid,
    utcnow_iso,
)
from asistent.contracts.timeutil import add_seconds

from .helpers import make_action, make_case, store_factory


class ApprovalAndActionContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = store_factory()

    def test_action_without_approval_cannot_be_claimed(self) -> None:
        case = make_case(self.store)
        action, approval, _summary = make_action(self.store, case=case, approve=False)
        self.assertIsNone(approval)
        self.assertEqual("PREPARED", str(action.state))
        with self.assertRaises(ActionNotClaimable):
            self.store.claim_action(action.id, worker="executor-1")

    def test_claim_consumes_approval_and_only_one_attempt_wins(self) -> None:
        case = make_case(self.store)
        action, approval, _summary = make_action(self.store, case=case)
        attempt = self.store.claim_action(action.id, worker="executor-1")
        self.assertEqual("IN_FLIGHT", attempt.state)
        self.assertEqual(action.idempotency_key, attempt.request_idempotency_key)
        self.assertEqual("CONSUMED", str(self.store.get("Approval", approval.id).state))
        with self.assertRaises(ActionNotClaimable):
            self.store.claim_action(action.id, worker="executor-2")

    def test_concurrent_claim_has_single_winner(self) -> None:
        case = make_case(self.store)
        action, _approval, _summary = make_action(self.store, case=case)
        barrier = threading.Barrier(6)
        outcomes = []

        def worker(index: int) -> None:
            barrier.wait()
            try:
                attempt = self.store.claim_action(action.id, worker=f"executor-{index}")
                outcomes.append(("claimed", attempt.id))
            except ActionNotClaimable as exc:
                outcomes.append(("rejected", exc.code))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        claimed = [o for o in outcomes if o[0] == "claimed"]
        self.assertEqual(1, len(claimed), f"只能有一个执行者拿到执行权，实际: {outcomes}")
        self.assertEqual("IN_FLIGHT", str(self.store.get("ExternalAction", action.id).state))

    def test_content_change_invalidates_approval(self) -> None:
        case = make_case(self.store)
        action, approval, summary = make_action(self.store, case=case, body="我确认参加。")
        changed = ActionSummary(
            kind=summary.kind,
            from_account=summary.from_account,
            to=list(summary.to),
            cc=list(summary.cc),
            bcc=list(summary.bcc),
            subject=summary.subject,
            body="我确认参加（正文被改）。",
            draft_version_id=summary.draft_version_id,
            target_thread_ref=summary.target_thread_ref,
            evidence_versions=dict(summary.evidence_versions),
            attachments=list(summary.attachments),
        )
        action.action_summary = changed.normalized()
        action.summary_digest = changed.digest()
        self.store.put(action, expected_revision=action.revision, reason="草稿被编辑")
        with self.assertRaises(ApprovalInvalid) as ctx:
            self.store.claim_action(action.id, worker="executor-1")
        self.assertEqual(approval.id, ctx.exception.detail["approval_id"])

    def test_bcc_and_attachment_changes_change_digest(self) -> None:
        base = ActionSummary(kind="send_email", from_account="a@b.c", to=["x@y.z"], subject="s", body="body", draft_version_id="dv_1")
        with_bcc = ActionSummary(
            kind="send_email", from_account="a@b.c", to=["x@y.z"], bcc=["hidden@y.z"], subject="s", body="body", draft_version_id="dv_1"
        )
        self.assertNotEqual(base.digest(), with_bcc.digest())
        diff = with_bcc.diff_keys(base)
        self.assertIn("bcc", diff)

        with_attachment = ActionSummary(
            kind="send_email",
            from_account="a@b.c",
            to=["x@y.z"],
            subject="s",
            body="body",
            draft_version_id="dv_1",
            attachments=[AttachmentRef(name="表格.xlsx", content_hash="sha-1")],
        )
        self.assertNotEqual(base.digest(), with_attachment.digest())
        self.assertIn("attachments", with_attachment.diff_keys(base))

    def test_body_whitespace_only_difference_does_not_invalidate(self) -> None:
        """规范化：仅换行/行尾空白差异不算内容变化（否则会频繁误判审批失效）。"""
        left = ActionSummary(kind="send_email", from_account="a@b.c", to=["x@y.z"], subject="s", body="第一行\r\n第二行  ", draft_version_id="dv_1")
        right = ActionSummary(kind="send_email", from_account="a@b.c", to=["X@Y.Z"], subject=" s ", body="第一行\n第二行", draft_version_id="dv_1")
        self.assertEqual(left.digest(), right.digest())

    def test_expired_approval_is_rejected(self) -> None:
        case = make_case(self.store)
        action, approval, summary = make_action(self.store, case=case, expires_in=-60)
        with self.assertRaises(ApprovalInvalid):
            self.store.claim_action(action.id, worker="executor-1")
        with self.assertRaises(ApprovalInvalid):
            check_approval_valid(
                approval_state="ACTIVE",
                approved_digest=summary.digest(),
                current_summary=summary,
                expires_at=approval.expires_at,
            )

    def test_revoked_approval_is_rejected(self) -> None:
        case = make_case(self.store)
        action, approval, _summary = make_action(self.store, case=case)
        self.store.invalidate_approval(approval.id, reason="用户撤销", state="REVOKED")
        with self.assertRaises(ApprovalInvalid):
            self.store.claim_action(action.id, worker="executor-1")

    def test_unknown_result_blocks_automatic_retry(self) -> None:
        case = make_case(self.store)
        action, _approval, _summary = make_action(self.store, case=case)
        attempt = self.store.claim_action(action.id, worker="executor-1")
        self.store.record_attempt(
            attempt.id,
            state="UNKNOWN",
            external_response={"error": "connection reset"},
            failure_class="EXTERNAL_UNKNOWN",
            error_text="超时，无法确认是否已接受",
        )
        self.assertEqual("UNKNOWN", str(self.store.get("ExternalAction", action.id).state))
        with self.assertRaises(ActionNotClaimable):
            self.store.claim_action(action.id, worker="executor-2")
        # 重启扫描不会把 UNKNOWN 当成 IN_FLIGHT 重新发送
        self.assertEqual([], self.store.demote_in_flight_to_unknown())
        self.assertEqual([action.id], self.store.recovery_scan().unknown_action_ids)

    def test_failed_safe_can_be_reapproved_and_retried_with_same_key(self) -> None:
        case = make_case(self.store)
        action, _approval, summary = make_action(self.store, case=case)
        attempt = self.store.claim_action(action.id, worker="executor-1")
        self.store.record_attempt(attempt.id, state="FAILED_SAFE", external_response={"smtp_code": 550}, failure_class="EXTERNAL_UNKNOWN")
        self.assertEqual("FAILED_SAFE", str(self.store.get("ExternalAction", action.id).state))

        reapproval = self.store.approve(
            Approval(
                case_id=case.id,
                action_id=action.id,
                approver="owner",
                approved_action_digest=summary.digest(),
                created_at_approval=utcnow_iso(),
                expires_at=add_seconds(utcnow_iso(), 3600),
                auth_evidence="console-session:test-2",
            )
        )
        refreshed = self.store.get("ExternalAction", action.id)
        refreshed.approval_id = reapproval.id
        self.store.put(refreshed, expected_revision=refreshed.revision, reason="重新绑定审批")
        self.store.transition("ExternalAction", action.id, "AUTHORIZED", actor_kind="USER", actor_id="owner", reason="重新授权")

        second_attempt = self.store.claim_action(action.id, worker="executor-2")
        self.assertEqual(2, second_attempt.attempt_no)
        self.assertEqual(action.idempotency_key, second_attempt.request_idempotency_key, "重试必须沿用同一幂等键")

    def test_succeeded_action_is_terminal(self) -> None:
        case = make_case(self.store)
        action, _approval, _summary = make_action(self.store, case=case)
        attempt = self.store.claim_action(action.id, worker="executor-1")
        self.store.record_attempt(attempt.id, state="SUCCEEDED", external_response={"smtp_code": 250}, external_ref="msgid@remote")
        final = self.store.get("ExternalAction", action.id)
        self.assertEqual("SUCCEEDED", str(final.state))
        self.assertTrue(final.finalized_at)
        with self.assertRaises(ActionNotClaimable):
            self.store.claim_action(action.id, worker="executor-3")

    def test_approval_requires_authenticated_channel_evidence(self) -> None:
        case = make_case(self.store)
        with self.assertRaises(Exception):
            self.store.approve(
                Approval(case_id=case.id, approver="owner", approved_action_digest="abc", auth_evidence=None)
            )

    def test_no_approval_without_action_digest(self) -> None:
        case = make_case(self.store)
        with self.assertRaises(Exception):
            self.store.approve(Approval(case_id=case.id, approver="owner", approved_action_digest="", auth_evidence="x"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
