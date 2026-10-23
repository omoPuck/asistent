"""UNKNOWN 的处置：只由用户裁决驱动，绝不自动重试（SPEC §854、§995、§998、§21.2）。

- `abandon` → `UNKNOWN → CANCELLED`（永不外发）；
- `retry`   → 必须显式确认重复风险 + 重新过认证审批，
              走 `UNKNOWN → FAILED_SAFE → AUTHORIZED → 新 attempt`，
              **沿用同一幂等键**，并保留原 UNKNOWN 记录。
"""

from __future__ import annotations

import unittest

from asistent.actions import (
    ActionEngine,
    MailCallLog,
    ScriptedMailSender,
    SendScriptStep,
    check_action_approval,
)
from asistent.contracts import ApprovalInvalid, PermissionDenied, ValidationError
from asistent.contracts.errors import ApprovalRequired

from .support import ActionCase, backend_kind, dump, engine_for


class UnknownDecisionTest(ActionCase):
    def _unknown_fixture(self, *, refuse_reused_key: bool = True, steps=None):
        case, action, summary, _approval, _draft, _version = self.fixture(self.store)
        engine, sender = engine_for(
            self.store,
            steps=list(steps or [SendScriptStep.unknown()]),
            refuse_reused_key=refuse_reused_key,
        )
        first = engine.execute(action.id)
        self.assertEqual("UNKNOWN", first.result_class)
        self.assertEqual(1, sender.call_count)
        return case, action, summary, engine, sender, first

    # ------------------------------------------------------------------ 不自动重试
    def test_unknown_is_never_retried_automatically(self) -> None:
        case, action, _summary, engine, sender, _first = self._unknown_fixture()

        polls = [engine.execute(action.id).code for _ in range(3)]
        recoveries = [engine.recover() for _ in range(2)]

        self.assertEqual(["ACTION_NOT_CLAIMABLE"] * 3, polls)
        self.assertTrue(all(item.resent_action_ids == [] for item in recoveries))
        self.assertTrue(all(item.demoted_action_ids == [] for item in recoveries))
        self.assertEqual(1, sender.call_count, "轮询与重启都不得重发 UNKNOWN")
        self.assertEqual(1, sum(1 for item in sender.log_entries() if item["event"] == "submit"))
        self.assertEqual(1, len(self.store.find("ActionAttempt", where={"action_id": action.id}, limit=10)))
        self.assertEqual("UNKNOWN", str(self.store.get("Case", case.id).state))

        pending = engine.pending_decisions()
        self.assertEqual([action.id], [item.action_id for item in pending])
        self.assertIn("未发送证明 ≠ 未发送", pending[0].warning)
        print("[evidence] UNKNOWN 待裁决 =", dump([item.to_dict() for item in pending]))

    # ------------------------------------------------------------------ retry 的两个闸门
    def test_retry_requires_duplicate_risk_acknowledgement(self) -> None:
        _case, action, _summary, engine, sender, _first = self._unknown_fixture()
        with self.assertRaises(ApprovalRequired) as ctx:
            engine.resolve_unknown(
                action.id,
                decision="retry",
                auth_evidence="console-session:mvp5",
                acknowledged_duplicate_risk=False,
            )
        self.assertEqual("APPROVAL_REQUIRED", ctx.exception.code)
        self.assertEqual("UNKNOWN", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual(1, sender.call_count)

    def test_retry_requires_authenticated_approval(self) -> None:
        _case, action, _summary, engine, sender, _first = self._unknown_fixture()
        with self.assertRaises(PermissionDenied):
            engine.resolve_unknown(
                action.id,
                decision="retry",
                auth_channel="chat",
                auth_evidence="用户在聊天里说：再发一次",
                acknowledged_duplicate_risk=True,
            )
        with self.assertRaises(ValidationError):
            engine.resolve_unknown(
                action.id,
                decision="retry",
                auth_channel="console",
                auth_evidence=None,
                acknowledged_duplicate_risk=True,
            )
        self.assertEqual("UNKNOWN", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual(1, sender.call_count)

    def test_unknown_decision_rejects_unknown_verb(self) -> None:
        _case, action, _summary, engine, _sender, _first = self._unknown_fixture()
        with self.assertRaises(ValidationError):
            engine.resolve_unknown(action.id, decision="whatever", acknowledged_duplicate_risk=True)

    def test_resolve_unknown_only_accepts_unknown_state(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        engine, _sender = engine_for(self.store, steps=[SendScriptStep.succeed()])
        engine.execute(action.id)
        with self.assertRaises(ValidationError):
            engine.resolve_unknown(
                action.id, decision="abandon", acknowledged_duplicate_risk=False
            )
        with self.assertRaises(ValidationError):
            engine.resolve_unknown(
                action.id, decision="retry", acknowledged_duplicate_risk=True, auth_evidence="x"
            )

    # ------------------------------------------------------------------ abandon
    def test_abandon_cancels_and_never_sends(self) -> None:
        case, action, _summary, engine, sender, _first = self._unknown_fixture()

        resolution = engine.resolve_unknown(
            action.id, decision="abandon", actor_id="owner", reason="用户核对发件箱后选择放弃"
        )

        self.assertEqual("abandon", resolution.decision)
        self.assertEqual("CANCELLED", str(self.store.get("ExternalAction", action.id).state))
        afterwards = engine.execute(action.id)
        self.assertEqual("REJECTED", afterwards.result_class)
        self.assertEqual(1, sender.call_count, "放弃后永不外发")
        self.assertEqual([], engine.pending_decisions(action_id=action.id))
        self.assertEqual("UNKNOWN", str(self.store.get("Case", case.id).state))
        print("[evidence] 放弃裁决 =", dump(resolution.to_dict()))

    # ------------------------------------------------------------------ retry 的正路
    def test_retry_creates_new_attempt_reusing_the_same_idempotency_key(self) -> None:
        _case, action, _summary, engine, sender, first = self._unknown_fixture(refuse_reused_key=False)

        resolution = engine.resolve_unknown(
            action.id,
            decision="retry",
            actor_id="owner",
            auth_evidence="console-session:mvp5-retry",
            acknowledged_duplicate_risk=True,
            reason="用户核对发件箱确认未发送",
        )
        second = engine.execute(action.id)

        self.assertEqual("retry", resolution.decision)
        self.assertTrue(resolution.duplicate_risk_acknowledged)
        self.assertIsNotNone(resolution.approval_id)
        self.assertEqual("SUCCEEDED", second.result_class)
        self.assertEqual(2, second.attempt_no, "重试必须走新的 attempt")
        self.assertEqual(first.idempotency_key, second.idempotency_key, "重试必须沿用同一幂等键")
        self.assertNotEqual(first.attempt_id, second.attempt_id)

        attempts = self.store.find("ActionAttempt", where={"action_id": action.id}, limit=10)
        self.assertEqual(["UNKNOWN", "SUCCEEDED"], [str(item.state) for item in attempts], "原 UNKNOWN 记录必须保留")
        self.assertEqual(
            [first.idempotency_key, first.idempotency_key],
            [item.request_idempotency_key for item in attempts],
            "两次 attempt 的请求幂等键必须一致",
        )
        submits = [item for item in sender.log_entries() if item["event"] == "submit"]
        self.assertEqual(2, len(submits))
        self.assertEqual(1, len({item["idempotency_key"] for item in submits}))
        check = check_action_approval(self.store, action.id)
        self.assertFalse(check.ok, "新的那次审批也已被这一次执行消费")
        self.assertIn("消费", check.reason_text())
        print(
            "[evidence] 重试 =",
            dump(
                {
                    "resolution": resolution.to_dict(),
                    "second": second.to_dict(),
                    "attempts": [str(item.state) for item in attempts],
                    "submitted_keys": sorted({item["idempotency_key"] for item in submits}),
                }
            ),
        )

    def test_retry_against_a_key_deduping_channel_is_reported_truthfully(self) -> None:
        """通道按幂等键去重时，第二次提交会被拒 → 引擎必须如实报 FAILED_SAFE，不许假装成功。"""
        _case, action, _summary, engine, sender, _first = self._unknown_fixture(refuse_reused_key=True)

        engine.resolve_unknown(
            action.id,
            decision="retry",
            auth_evidence="console-session:mvp5-retry",
            acknowledged_duplicate_risk=True,
        )
        second = engine.execute(action.id)

        self.assertEqual("FAILED_SAFE", second.result_class)
        self.assertEqual("FAILED_SAFE", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual(2, second.attempt_no)
        events = [item["event"] for item in sender.log_entries()]
        self.assertEqual(["submit", "refused"], events, "第二次是通道侧兜底拒绝")
        print("[evidence] 通道侧去重兜底 =", dump({"second": second.to_dict(), "events": events}))

    def test_second_cycle_of_unknown_needs_another_decision(self) -> None:
        _case, action, _summary, engine, sender, first = self._unknown_fixture(
            refuse_reused_key=False,
            steps=[SendScriptStep.unknown(note="第一次未知"), SendScriptStep.unknown(note="第二次也未知"), SendScriptStep.succeed()],
        )
        engine.resolve_unknown(
            action.id, decision="retry", auth_evidence="console-session:1", acknowledged_duplicate_risk=True
        )
        second = engine.execute(action.id)
        self.assertEqual("UNKNOWN", second.result_class)
        self.assertEqual(2, second.attempt_no)

        # 又回到 UNKNOWN：必须再裁决一次，而且仍然不能自动重试
        blocked = engine.execute(action.id)
        self.assertEqual("REJECTED", blocked.result_class)
        third_resolution = engine.resolve_unknown(
            action.id, decision="retry", auth_evidence="console-session:2", acknowledged_duplicate_risk=True
        )
        third = engine.execute(action.id)
        self.assertEqual("SUCCEEDED", third.result_class)
        self.assertEqual(3, third.attempt_no)
        self.assertEqual(first.idempotency_key, third.idempotency_key)
        self.assertEqual("retry", third_resolution.decision)
        self.assertEqual(3, sender.call_count)
        attempts = self.store.find("ActionAttempt", where={"action_id": action.id}, limit=10)
        self.assertEqual(
            ["UNKNOWN", "UNKNOWN", "SUCCEEDED"], [str(item.state) for item in attempts]
        )
        print("[evidence] 两轮裁决 =", dump([str(item.state) for item in attempts]))

    @unittest.skipUnless(backend_kind() == "sqlite", "重启语义需要持久化后端")
    def test_unknown_survives_restart_and_stays_blocked(self) -> None:
        case, action, _summary, engine, sender, _first = self._unknown_fixture()
        call_log = self.path("unknown-restart.log")
        del engine

        store = self.restart_store()
        engine2 = ActionEngine(store, ScriptedMailSender(log=MailCallLog(call_log)), node="restarted")
        recovery = engine2.recover()

        self.assertEqual([], recovery.demoted_action_ids, "UNKNOWN 不是 IN_FLIGHT，不得被再次降级")
        self.assertEqual([action.id], recovery.unknown_action_ids)
        self.assertEqual([], recovery.resent_action_ids)
        self.assertEqual("UNKNOWN", str(store.get("ExternalAction", action.id).state))
        self.assertEqual("UNKNOWN", str(store.get("Case", case.id).state))
        self.assertEqual(0, MailCallLog.submitted_count_on_disk(call_log), "重启后不得有任何提交")
        self.assertEqual(1, sum(1 for item in sender.log_entries() if item["event"] == "submit"))
        self.assertEqual(1, len(engine2.pending_decisions()))
        print("[evidence] 重启后仍待裁决 =", dump(recovery.to_dict()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
