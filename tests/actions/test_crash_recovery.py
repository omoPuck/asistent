"""崩溃恢复：IN_FLIGHT → UNKNOWN，重启后绝不重发（SPEC §853-854、§998、§21.2；AT-07）。

三种注入方式，覆盖同一语义的不同侧面：

1. **真子进程强杀**（sqlite）：`crash_child.py` 在"已提交、结果未落库"时 `os._exit(9)`；
   父进程重启后 `recover()` → `UNKNOWN`，而落盘调用账本的提交次数**仍然是 1**。
2. **同进程模拟崩溃**：通道抛 `BaseException`（与 `KeyboardInterrupt` 同类），
   引擎必须原样上抛、不得吞掉，更不能"顺手补一个 FAILED_SAFE"。
3. **库里残留 IN_FLIGHT**：不经过通道，直接在库里留一个在途动作，走恢复路径。
"""

from __future__ import annotations

import unittest

from asistent.actions import (
    ActionEngine,
    MailCallLog,
    RecoveryOutcome,
    ScriptedMailSender,
    SendScriptStep,
    SimulatedCrash,
)
from asistent.contracts import ActionState

from .support import ActionCase, backend_kind, dump, engine_for, run_child

CRASH_EXIT_CODE = 9


class CrashRecoveryTest(ActionCase):
    # ---------------------------------------------------------------- 注入点 1：真子进程
    @unittest.skipUnless(backend_kind() == "sqlite", "跨进程崩溃注入需要持久化后端")
    def test_subprocess_killed_after_submit_recovered_as_unknown_without_resend(self) -> None:
        case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        call_log = self.path("crash-calls.log")
        self.store.close()  # 子进程要自己打开同一个库

        code, output = run_child("crash_child.py", self.db_path(), call_log, action.id)
        print("[evidence] 子进程退出码 =", code, "输出 =", output.strip())
        self.assertEqual(CRASH_EXIT_CODE, code, "子进程必须以「被强杀」的退出码结束")
        self.assertEqual(1, MailCallLog.submitted_count_on_disk(call_log), "被强杀前已提交 1 次")

        # ---- 重启（新进程 / 新连接）
        store = self.make_store()
        killed_action = store.get("ExternalAction", action.id)
        attempts = store.find("ActionAttempt", where={"action_id": action.id}, limit=10)
        print(
            "[evidence] 崩溃后残留状态 =",
            dump(
                {
                    "action_state": str(killed_action.state),
                    "claimed_by": killed_action.claimed_by,
                    "attempt_states": [str(item.state) for item in attempts],
                }
            ),
        )
        self.assertEqual("IN_FLIGHT", str(killed_action.state), "崩溃时动作必须停在 IN_FLIGHT")
        self.assertEqual(["IN_FLIGHT"], [str(item.state) for item in attempts])

        # ---- 恢复：只转 UNKNOWN，不重发
        engine = ActionEngine(store, ScriptedMailSender(log=MailCallLog(call_log)), node="restarted")
        recovery = engine.recover()
        print("[evidence] 恢复结论 =", dump(recovery.to_dict()))
        self.assertEqual([action.id], recovery.demoted_action_ids)
        self.assertEqual([], recovery.resent_action_ids, "恢复路径绝不允许重发")
        self.assertEqual(1, MailCallLog.submitted_count_on_disk(call_log), "恢复不得产生第二次提交")

        recovered = store.get("ExternalAction", action.id)
        self.assertEqual("UNKNOWN", str(recovered.state))
        attempts = store.find("ActionAttempt", where={"action_id": action.id}, limit=10)
        self.assertEqual(["UNKNOWN"], [str(item.state) for item in attempts])
        self.assertEqual("EXTERNAL_UNKNOWN", attempts[0].failure_class)
        self.assertIn("重启", attempts[0].error_text)
        self.assertEqual("UNKNOWN", str(store.get("Case", case.id).state), "Case 必须显示 UNKNOWN")

        # 重复恢复是幂等的；UNKNOWN 不会被当成 IN_FLIGHT
        again = engine.recover()
        self.assertEqual([], again.demoted_action_ids)
        self.assertEqual([action.id], again.unknown_action_ids)
        self.assertEqual(1, MailCallLog.submitted_count_on_disk(call_log))

        # 轮询/再次执行都不放行
        retry = engine.execute(action.id)
        self.assertEqual("REJECTED", retry.result_class)
        self.assertEqual("ACTION_NOT_CLAIMABLE", retry.code)
        self.assertEqual(1, MailCallLog.submitted_count_on_disk(call_log), "UNKNOWN 不得被自动重发")

        pending = engine.pending_decisions()
        print("[evidence] 待用户裁决 =", dump([item.to_dict() for item in pending]))
        self.assertEqual([action.id], [item.action_id for item in pending])
        self.assertEqual("UNKNOWN", pending[0].action_state)
        self.assertEqual(("retry", "abandon"), tuple(pending[0].allowed_decisions))

        # ---- 用户裁决"重试"：新 attempt + 同一幂等键
        resolution = engine.resolve_unknown(
            action.id,
            decision="retry",
            actor_id="owner",
            auth_evidence="console-session:mvp5-retry",
            acknowledged_duplicate_risk=True,
            reason="用户核对发件箱确认未发送",
        )
        print("[evidence] 用户裁决 =", dump(resolution.to_dict()))
        self.assertEqual("AUTHORIZED", resolution.action_state)

        sender = ScriptedMailSender(
            log=MailCallLog(call_log), script=[SendScriptStep.succeed("retry-ref")]
        )
        engine2 = ActionEngine(store, sender, node="restarted")
        second = engine2.execute(action.id)
        print("[evidence] 裁决后重试 =", dump(second.to_dict()))
        self.assertEqual("SUCCEEDED", second.result_class)
        self.assertEqual(2, second.attempt_no, "重试必须走**新的** attempt")

        entries = MailCallLog.read_entries(call_log)
        submitted = [item for item in entries if item["event"] == "submit"]
        self.assertEqual(2, len(submitted), "第一次崩溃前 1 次 + 用户裁决重试 1 次")
        keys = {item["idempotency_key"] for item in submitted}
        print("[evidence] 提交过的幂等键 =", dump(sorted(keys)))
        self.assertEqual(1, len(keys), "重试必须沿用同一个幂等键（不得自造新键逃过去重）")
        self.assertEqual(second.idempotency_key, list(keys)[0])

        plan = store.find("ActionAttempt", where={"action_id": action.id}, limit=10)
        self.assertEqual(["UNKNOWN", "SUCCEEDED"], [str(item.state) for item in plan], "原 UNKNOWN 记录必须保留")

    # ---------------------------------------------------------------- 注入点 2：同进程
    def test_in_process_crash_leaves_in_flight_and_recovery_demotes(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        engine, sender = engine_for(self.store, steps=[SendScriptStep.crash_after_submit()])

        with self.assertRaises(SimulatedCrash):
            engine.execute(action.id)

        self.assertEqual(1, sender.call_count, "崩溃发生在提交之后")
        self.assertEqual(1, sum(1 for item in sender.log_entries() if item["event"] == "submit"))
        self.assertEqual("IN_FLIGHT", str(self.store.get("ExternalAction", action.id).state))
        attempts = self.store.find("ActionAttempt", where={"action_id": action.id}, limit=10)
        self.assertEqual(["IN_FLIGHT"], [str(item.state) for item in attempts])
        self.assertEqual([], self.store.find("Execution", limit=10), "崩溃时不得留下假结论")

        recovery = engine.recover()
        self.assertEqual([action.id], recovery.demoted_action_ids)
        self.assertEqual("UNKNOWN", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual(1, sender.call_count, "恢复不得再次调用通道")
        self.assertEqual(1, sum(1 for item in sender.log_entries() if item["event"] == "submit"))
        print("[evidence] 同进程崩溃恢复 =", dump(recovery.to_dict()))

    def test_crash_exception_is_not_swallowed(self) -> None:
        """`BaseException` 不得被当成普通通道错误分类（否则会写成 FAILED_SAFE 而丢掉在途事实）。"""
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        engine, sender = engine_for(self.store, steps=[SendScriptStep.crash_after_submit()])
        try:
            engine.execute(action.id)
            self.fail("崩溃必须原样上抛")
        except SimulatedCrash:
            pass
        self.assertEqual("IN_FLIGHT", str(self.store.get("ExternalAction", action.id).state))
        self.assertIsNone(self.store.get("ExternalAction", action.id).result_class)
        self.assertEqual(1, sum(1 for item in sender.log_entries() if item["event"] == "submit"))

    # ---------------------------------------------------------------- 注入点 3：库里残留
    def test_manifest_in_flight_in_store_is_demoted(self) -> None:
        case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        attempt = self.store.claim_action(action.id, worker="crashed-worker")
        self.assertEqual("IN_FLIGHT", attempt.state)
        self.assertEqual(0, len(self.store.find("Execution", limit=10)))

        engine, sender = engine_for(self.store)
        recovery = engine.recover()

        self.assertEqual([action.id], recovery.demoted_action_ids)
        self.assertEqual(0, sender.call_count)
        self.assertEqual("UNKNOWN", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual("UNKNOWN", str(self.store.get("Case", case.id).state))
        print("[evidence] 残留 IN_FLIGHT 恢复 =", dump(recovery.to_dict()))

    def test_recover_on_clean_store_does_nothing(self) -> None:
        _case, _action, _summary, _approval, _draft, _version = self.fixture(self.store)
        engine, sender = engine_for(self.store)
        recovery = engine.recover()
        self.assertIsInstance(recovery, RecoveryOutcome)
        self.assertTrue(recovery.to_dict()["demoted_action_ids"] == [])
        self.assertEqual(0, sender.call_count)
        self.assertEqual("AUTHORIZED", str(self.store.get("ExternalAction", _action.id).state))

    def test_unknown_state_blocks_case_cancel(self) -> None:
        """UNKNOWN 属于不可逆状态：事务不得被"取消"掩盖掉可能已发出的邮件。"""
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        engine, _sender = engine_for(self.store, steps=[SendScriptStep.unknown()])
        engine.execute(action.id)
        self.assertEqual(ActionState.UNKNOWN, ActionState.coerce(self.store.get("ExternalAction", action.id).state))
        with self.assertRaises(Exception):
            self.store.transition("Case", action.case_id, "CANCELLED", actor_kind="USER", actor_id="owner")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
