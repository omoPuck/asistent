"""执行主路径：未批准不执行 / 先持久化后外呼 / 成功幂等 / 结果分类。

覆盖 SPEC §849-855 的 MVP-5 条目：

- Given 未批准的发送动作，When Agent 尝试执行，Then 动作被拒绝且无外部副作用；
- Given 发送成功后重复轮询或重启，When 恢复任务，Then 不再发送第二次。

以及 SPEC §984（外呼之前必须已持久化 attempt_id + IN_FLIGHT，禁止"先查询、后标记"）
和 §21.2 的结果分类表。
"""

from __future__ import annotations

import unittest

from asistent.actions import (
    ActionEngine,
    OutboundRejected,
    OutboundTimeout,
    OutboundUnavailable,
    SendScriptStep,
    classify_sender_exception,
)
from asistent.actions.approval import revoke_approval
from asistent.contracts import ActionNotClaimable, MailSendRequest, MailSendResult

from .support import ActionCase, dump, engine_for


class _ObservingSender:
    """外呼**当刻**回读持久状态的自证通道：用来证明"先持久化、后外呼"。

    它不是 mock 断言，而是"在真正调用外部的这一瞬间，库里到底是什么样"的快照。
    """

    name = "observing"

    def __init__(self, store, action_id: str) -> None:
        self.store = store
        self.action_id = action_id
        self.call_count = 0
        self.snapshots = []

    def send(self, request: MailSendRequest) -> MailSendResult:
        self.call_count += 1
        action = self.store.get("ExternalAction", self.action_id)
        attempts = self.store.find("ActionAttempt", where={"action_id": self.action_id}, limit=10)
        self.snapshots.append(
            {
                "request_idempotency_key": request.idempotency_key,
                "action_state": str(action.state),
                "claimed_by": action.claimed_by,
                "attempt_states": [str(item.state) for item in attempts],
                "attempt_keys": [item.request_idempotency_key for item in attempts],
                "attempt_ids": [item.id for item in attempts],
            }
        )
        return MailSendResult.succeeded("observed-ref@local")


class NoApprovalNoSideEffectTest(ActionCase):
    """语义 1：没有有效审批 → 拒绝执行，且外部调用次数 = 0。"""

    def test_unapproved_action_is_rejected_with_zero_calls(self) -> None:
        case, action, _summary, _approval, _draft, version = self.fixture(self.store, approve=False)
        self.assertEqual("PREPARED", str(action.state))
        engine, sender = engine_for(self.store, steps=[SendScriptStep.succeed()])

        outcome = engine.execute(action.id)

        self.assertEqual("REJECTED", outcome.result_class)
        self.assertEqual("ACTION_NOT_CLAIMABLE", outcome.code)
        self.assertFalse(outcome.claimed)
        self.assertFalse(outcome.sender_called)
        self.assertEqual(0, sender.call_count, "未批准的动作不得调用外发通道")
        self.assertEqual(0, len(sender.log_entries()), "外发账本必须为空")
        self.assertEqual([], self.store.find("ActionAttempt", limit=10), "不得建立任何 attempt")
        self.assertEqual([], self.store.find("Execution", limit=10), "不得产生 Execution")
        self.assertEqual("PREPARED", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual("READY_FOR_REVIEW", str(self.store.get("Case", case.id).state))
        print("[evidence] 未批准拒绝 =", dump(outcome.to_dict()))

    def test_unapproved_action_raises_when_asked(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store, approve=False)
        engine, sender = engine_for(self.store)
        with self.assertRaises(ActionNotClaimable):
            engine.execute(action.id, raise_on_reject=True)
        self.assertEqual(0, sender.call_count)

    def test_revoked_approval_is_rejected_with_zero_calls(self) -> None:
        _case, action, _summary, approval, _draft, _version = self.fixture(self.store)
        state, rolled = revoke_approval(self.store, approval_id=approval.id, reason="用户撤销")
        self.assertEqual("REVOKED", state)
        self.assertEqual([action.id], rolled)
        engine, sender = engine_for(self.store)

        outcome = engine.execute(action.id)

        self.assertEqual("REJECTED", outcome.result_class)
        self.assertEqual("ACTION_NOT_CLAIMABLE", outcome.code, "撤销后动作已回落 PREPARED")
        self.assertEqual(0, sender.call_count)

    def test_approval_revoked_out_of_band_is_caught_by_atomic_claim(self) -> None:
        """审批被直接改成 REVOKED（动作仍停在 AUTHORIZED）→ 领取本身必须挡住。"""
        _case, action, _summary, approval, _draft, _version = self.fixture(self.store)
        self.store.invalidate_approval(approval.id, reason="用户撤销", state="REVOKED")
        engine, sender = engine_for(self.store)

        outcome = engine.execute(action.id)

        self.assertEqual("REJECTED", outcome.result_class)
        self.assertEqual("APPROVAL_INVALID", outcome.code)
        self.assertEqual("AUTHORIZED", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual(0, sender.call_count)

    def test_missing_channel_refuses_without_claiming(self) -> None:
        """没有受控适配器时宁可拒绝，也不建立 IN_FLIGHT（否则会留下假在途动作）。"""
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        engine = ActionEngine(self.store, None)
        outcome = engine.execute(action.id)
        self.assertEqual("REJECTED", outcome.result_class)
        self.assertEqual("OUTBOUND_CHANNEL_MISSING", outcome.code)
        self.assertEqual("AUTHORIZED", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual([], self.store.find("ActionAttempt", limit=10))

    def test_local_kind_is_not_sent_out(self) -> None:
        """本地/自动类动作不得走外呼通道（§21.3 表格）——拒绝而不是误发。"""
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store, kind="local_search")
        engine, sender = engine_for(self.store)
        outcome = engine.execute(action.id)
        self.assertEqual("REJECTED", outcome.result_class)
        self.assertEqual("VALIDATION_ERROR", outcome.code)
        self.assertEqual(0, sender.call_count)


class PersistBeforeOutboundTest(ActionCase):
    """语义 5：外呼之前 attempt_id + IN_FLIGHT 必须已经在库里（SPEC §984）。"""

    def test_store_is_already_in_flight_when_channel_is_called(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        sender = _ObservingSender(self.store, action.id)
        engine = ActionEngine(self.store, sender, node="executor-observe")

        outcome = engine.execute(action.id)

        self.assertEqual("SUCCEEDED", outcome.result_class)
        self.assertEqual(1, len(sender.snapshots))
        snapshot = sender.snapshots[0]
        print("[evidence] 外呼当刻的持久状态 =", dump(snapshot))
        self.assertEqual("IN_FLIGHT", snapshot["action_state"], "外呼时动作必须已标 IN_FLIGHT")
        self.assertEqual("executor-observe", snapshot["claimed_by"])
        self.assertEqual(["IN_FLIGHT"], snapshot["attempt_states"], "attempt 必须先落库再外呼")
        self.assertEqual(1, len(snapshot["attempt_ids"]))
        self.assertEqual(action.idempotency_key, snapshot["request_idempotency_key"])
        self.assertEqual([action.idempotency_key], snapshot["attempt_keys"])
        self.assertEqual(outcome.attempt_id, snapshot["attempt_ids"][0])


class SuccessIdempotenceTest(ActionCase):
    """语义 7：成功之后重复轮询 / 重启 → 不得第二次发送。"""

    def test_repeated_polling_after_success_never_sends_again(self) -> None:
        case, action, _summary, approval, _draft, _version = self.fixture(self.store)
        engine, sender = engine_for(self.store, steps=[SendScriptStep.succeed("ref-once")])

        first = engine.execute(action.id)
        self.assertEqual("SUCCEEDED", first.result_class)
        self.assertEqual(1, sender.call_count)
        self.assertEqual("SUCCEEDED", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual("SUCCEEDED", str(self.store.get("Case", case.id).state))
        self.assertEqual("CONSUMED", str(self.store.get("Approval", approval.id).state))

        polls = [engine.execute(action.id).result_class for _ in range(3)]
        recovery = engine.recover()
        again = engine.recover()

        self.assertEqual(["REJECTED"] * 3, polls)
        self.assertEqual([], recovery.demoted_action_ids)
        self.assertEqual([], recovery.resent_action_ids)
        self.assertEqual([], again.resent_action_ids)
        self.assertEqual(1, sender.call_count, "重复轮询与重启都不得再调用通道")
        self.assertEqual(1, len(sender.log_entries()))
        self.assertEqual(1, len(self.store.find("ActionAttempt", where={"action_id": action.id}, limit=10)))
        self.assertEqual("SUCCEEDED", str(self.store.get("ExternalAction", action.id).state))
        print("[evidence] 成功幂等 =", dump({"polls": polls, "recovery": recovery.to_dict(), "calls": sender.call_count}))

    def test_success_records_execution_and_audit(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        engine, _sender = engine_for(self.store, steps=[SendScriptStep.succeed("ref-audit")])
        outcome = engine.execute(action.id)
        executions = self.store.find("Execution", limit=10)
        self.assertEqual(1, len(executions))
        self.assertEqual("SUCCEEDED", str(executions[0].state))
        self.assertEqual(outcome.execution_id, executions[0].id)
        self.assertIn(action.id, executions[0].read_object_ids)
        audit_actions = [str(item.action) for item in self.store.audit_trail(object_id=action.id, limit=50)]
        self.assertIn("EXECUTE", audit_actions)


class ResultClassificationTest(ActionCase):
    """结果分类：SUCCEEDED / FAILED_SAFE / UNKNOWN 与失败分类映射（契约 §8、§21.2）。"""

    def test_timeout_becomes_unknown(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        engine, sender = engine_for(self.store, steps=[SendScriptStep.timeout()])

        outcome = engine.execute(action.id)

        self.assertEqual("UNKNOWN", outcome.result_class)
        self.assertEqual("UNKNOWN", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual("NETWORK_FAILURE", outcome.failure_class)
        self.assertTrue(outcome.sender_called)
        self.assertEqual(1, sender.call_count)

    def test_rejected_is_failed_safe_and_clears_claim(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        engine, _sender = engine_for(self.store, steps=[SendScriptStep.rejected()])

        outcome = engine.execute(action.id)

        self.assertEqual("FAILED_SAFE", outcome.result_class)
        final = self.store.get("ExternalAction", action.id)
        self.assertEqual("FAILED_SAFE", str(final.state))
        self.assertIsNone(final.claimed_by, "确定未发生副作用才允许解除占位")

    def test_channel_unavailable_is_failed_safe(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        engine, _sender = engine_for(self.store, steps=[SendScriptStep.unavailable()])
        outcome = engine.execute(action.id)
        self.assertEqual("FAILED_SAFE", outcome.result_class)
        self.assertEqual("NETWORK_FAILURE", outcome.failure_class)

    def test_channel_declared_unknown_stays_unknown(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        engine, _sender = engine_for(self.store, steps=[SendScriptStep.unknown()])
        outcome = engine.execute(action.id)
        self.assertEqual("UNKNOWN", outcome.result_class)
        self.assertEqual("EXTERNAL_UNKNOWN", outcome.failure_class)

    def test_contradictory_result_is_downgraded_to_unknown(self) -> None:
        """通道自称 SUCCEEDED 但 accepted=False → 不允许当成功用（fail-closed）。"""

        class Liar:
            name = "liar"
            call_count = 0

            def send(self, request):
                self.call_count += 1
                return MailSendResult(result_class="SUCCEEDED", accepted=False, external_ref="x")

        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        liar = Liar()
        engine = ActionEngine(self.store, liar)
        outcome = engine.execute(action.id)
        self.assertEqual("UNKNOWN", outcome.result_class)
        self.assertEqual("UNKNOWN", str(self.store.get("ExternalAction", action.id).state))
        self.assertIn("mismatch", outcome.detail["external_response"])

    def test_unknown_result_is_never_auto_retried_by_poll_or_recover(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        engine, sender = engine_for(self.store, steps=[SendScriptStep.unknown()])
        engine.execute(action.id)

        polls = [engine.execute(action.id).code for _ in range(2)]
        recovery = engine.recover()

        self.assertEqual(["ACTION_NOT_CLAIMABLE"] * 2, polls)
        self.assertEqual([action.id], recovery.unknown_action_ids)
        self.assertEqual([action.id], recovery.pending_decision_action_ids)
        self.assertEqual([], recovery.demoted_action_ids)
        self.assertEqual([], recovery.resent_action_ids)
        self.assertEqual(1, sender.call_count)

    def test_classify_sender_exception_table(self) -> None:
        cases = [
            (OutboundRejected("前置拒绝"), "FAILED_SAFE"),
            (OutboundUnavailable("连接失败"), "FAILED_SAFE"),
            (OutboundTimeout("超时"), "UNKNOWN"),
            (ValueError("未知异常"), "UNKNOWN"),
            (KeyError("未知异常"), "UNKNOWN"),
        ]
        observed = []
        for exc, expected in cases:
            actual, failure_class = classify_sender_exception(exc)
            observed.append((type(exc).__name__, actual, failure_class))
            self.assertEqual(expected, actual, type(exc).__name__)
        print("[evidence] 异常分类表 =", dump(observed))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
