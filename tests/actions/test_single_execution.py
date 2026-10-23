"""一次性执行权：并发只有一个赢家（SPEC §852、§984、§21.2；AT-07）。

两种**真并发**都做：

1. 同进程 8 线程（`threading.Barrier` 对齐后同时冲）；
2. 跨进程 4 个子进程（sqlite 后端；`BEGIN IMMEDIATE` 拿写锁）。

断言口径统一为"外部提交次数 = 1"：赢家拿到执行权并调用通道一次，
其余全部被拒且留下可解释原因（错误码 + 原因文本 + 审计条目）。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from typing import Any, Dict, List, Tuple

from asistent.actions import ActionEngine, MailCallLog, ScriptedMailSender, SendScriptStep
from asistent.contracts import MailSendResult

from .support import ActionCase, backend_kind, dump, engine_for, start_child

#: 同进程并发 worker 数（验收要求 N=8）
THREAD_WORKERS = 8
#: 跨进程并发 worker 数
PROCESS_WORKERS = 4


class _SpyStore:
    """存储代理：记录调用序列，用来证明领取是"一个原子调用"而不是"先查后标记"。"""

    def __init__(self, inner: Any, sink: List[Tuple[str, Tuple[Any, ...]]]) -> None:
        self._inner = inner
        self._sink = sink

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self._sink.append((name, tuple(args)))
            return attribute(*args, **kwargs)

        return wrapper


class SingleExecutionTest(ActionCase):
    def test_thread_concurrency_has_exactly_one_winner(self) -> None:
        case, action, _summary, approval, _draft, _version = self.fixture(self.store)
        engine, sender = engine_for(self.store, steps=[SendScriptStep.succeed("only-winner")])
        barrier = threading.Barrier(THREAD_WORKERS)
        outcomes: List[Dict[str, Any]] = []
        guard = threading.Lock()

        def worker(index: int) -> None:
            barrier.wait()
            outcome = engine.execute(action.id, worker="executor-%d" % index)
            with guard:
                outcomes.append(
                    {
                        "worker": "executor-%d" % index,
                        "claimed": outcome.claimed,
                        "result_class": outcome.result_class,
                        "code": outcome.code,
                        "reason": outcome.reason,
                        "attempt_id": outcome.attempt_id,
                        "attempt_no": outcome.attempt_no,
                    }
                )

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(THREAD_WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [item for item in outcomes if item["claimed"]]
        losers = [item for item in outcomes if not item["claimed"]]
        print("[evidence] 并发执行结果 =", dump(sorted(outcomes, key=lambda item: item["worker"])))

        self.assertEqual(1, len(winners), "只能有一个执行者拿到执行权")
        self.assertEqual(THREAD_WORKERS - 1, len(losers))
        self.assertEqual("SUCCEEDED", winners[0]["result_class"])
        for loser in losers:
            self.assertIn(loser["code"], ("ACTION_NOT_CLAIMABLE", "APPROVAL_INVALID"))
            self.assertTrue(loser["reason"], "被拒必须留下可解释原因")
            self.assertIsNone(loser["attempt_id"])
        self.assertEqual(1, sender.call_count, "外部提交次数必须恰好是 1")
        self.assertEqual(1, sum(1 for item in sender.log_entries() if item["event"] == "submit"))

        attempts = self.store.find("ActionAttempt", where={"action_id": action.id}, limit=10)
        self.assertEqual(1, len(attempts))
        self.assertEqual(1, attempts[0].attempt_no)
        final_approval = self.store.get("Approval", approval.id)
        self.assertEqual("CONSUMED", str(final_approval.state))
        self.assertEqual(attempts[0].id, final_approval.consumed_by_attempt_id)
        self.assertEqual("SUCCEEDED", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual("SUCCEEDED", str(self.store.get("Case", case.id).state))

    @unittest.skipUnless(backend_kind() == "sqlite", "跨进程共享状态需要持久化后端")
    def test_cross_process_concurrency_has_exactly_one_winner(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        call_log = self.path("cross-process-calls.log")
        self.store.close()  # 子进程要自己打开同一个库

        children = [
            start_child("claim_child.py", self.db_path(), call_log, action.id, "worker-%d" % index)
            for index in range(PROCESS_WORKERS)
        ]
        rows: List[Dict[str, Any]] = []
        for child in children:
            raw, _ = child.communicate(timeout=240)
            rows.append(json.loads(raw.decode("utf-8", "replace").strip().splitlines()[-1]))

        print("[evidence] 跨进程竞争 =", dump(sorted(rows, key=lambda item: item["worker"])))
        winners = [item for item in rows if item["claimed"]]
        losers = [item for item in rows if not item["claimed"]]
        self.assertEqual(1, len(winners), "跨进程也只能有一个赢家")
        self.assertEqual(PROCESS_WORKERS - 1, len(losers))
        for loser in losers:
            self.assertTrue(loser["reason"])
        submitted = MailCallLog.submitted_count_on_disk(call_log)
        print("[evidence] 跨进程落盘提交次数 =", submitted)
        self.assertEqual(1, submitted, "跨进程也不得出现第二次外部提交")

        store = self.make_store()
        attempts = store.find("ActionAttempt", where={"action_id": action.id}, limit=10)
        self.assertEqual(1, len(attempts))
        self.assertEqual("SUCCEEDED", str(store.get("ExternalAction", action.id).state))

    def test_losers_are_rejected_while_the_winner_is_still_in_flight(self) -> None:
        """确定性复现"在途窗口"：赢家停在 `send()` 里，其余 worker 必须看到 IN_FLIGHT 并被拒。

        真并发测试的失败者可能恰好在赢家收尾之后才发起领取（那时看到的是 SUCCEEDED），
        这条用例把窗口**钉住**：动作在库里确实是 IN_FLIGHT 时，其它人一律不得进入。
        """
        case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        gate = threading.Event()

        class GatedSender:
            name = "gated"
            call_count = 0

            def send(self, request):
                GatedSender.call_count += 1
                gate.wait(timeout=15)  # 赢家停在这里，动作保持 IN_FLIGHT
                return MailSendResult.succeeded("gated-ref")

        engine = ActionEngine(self.store, GatedSender(), node="executor-gated")
        winner: List[Any] = []

        def run_winner() -> None:
            winner.append(engine.execute(action.id, worker="winner"))

        thread = threading.Thread(target=run_winner)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and str(self.store.get("ExternalAction", action.id).state) != "IN_FLIGHT":
            time.sleep(0.005)
        state_in_flight = str(self.store.get("ExternalAction", action.id).state)
        self.assertEqual("IN_FLIGHT", state_in_flight)

        in_flight_attempts = self.store.find("ActionAttempt", where={"action_id": action.id}, limit=10)
        self.assertEqual(["IN_FLIGHT"], [str(item.state) for item in in_flight_attempts], "结果此时还没落库")

        losers = [engine.execute(action.id, worker="late-%d" % index) for index in range(7)]
        gate.set()
        thread.join(timeout=15)

        self.assertEqual(1, len(winner))
        self.assertEqual("SUCCEEDED", winner[0].result_class)
        self.assertEqual(1, GatedSender.call_count, "在途窗口内没有任何第二次提交")
        for loser in losers:
            self.assertEqual("REJECTED", loser.result_class)
            self.assertEqual("ACTION_NOT_CLAIMABLE", loser.code)
            self.assertIn("IN_FLIGHT", loser.reason, "失败者必须看到「动作正在途」这个事实")
        self.assertEqual("SUCCEEDED", str(self.store.get("ExternalAction", action.id).state))
        final_attempts = self.store.find("ActionAttempt", where={"action_id": action.id}, limit=10)
        self.assertEqual(1, len(final_attempts), "全程只有一条 attempt：赢家从 IN_FLIGHT 走到 SUCCEEDED")
        self.assertEqual("SUCCEEDED", str(final_attempts[0].state))
        self.assertEqual(1, final_attempts[0].attempt_no)
        print(
            "[evidence] 在途窗口内的失败者 =",
            dump({"winner": winner[0].to_dict(), "losers": [item.to_dict() for item in losers]}),
        )
        self.assertEqual("SUCCEEDED", str(self.store.get("Case", case.id).state))

    def test_claim_is_one_atomic_call_not_query_then_mark(self) -> None:
        """领取必须在**一次** `claim_action` 调用里完成（SPEC §984）。"""
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        trace: List[Tuple[str, Tuple[Any, ...]]] = []

        class TracingSender:
            name = "tracing"
            call_count = 0

            def send(self, request):
                trace.append(("sender.send", (request.idempotency_key,)))
                TracingSender.call_count += 1
                return MailSendResult.succeeded("tracing-ref")

        engine = ActionEngine(_SpyStore(self.store, trace), TracingSender(), node="executor-spy")
        outcome = engine.execute(action.id)

        self.assertEqual("SUCCEEDED", outcome.result_class)
        names = [name for name, _args in trace]
        print("[evidence] 存储+通道调用序列 =", json.dumps(names, ensure_ascii=False))
        self.assertEqual(1, names.count("claim_action"), "领取只能是一次原子调用")
        self.assertLess(names.index("claim_action"), names.index("sender.send"), "先领取再外呼")
        self.assertLess(names.index("sender.send"), names.index("record_attempt"), "外呼后落结果")
        # 引擎不得自己打状态：IN_FLIGHT 只能由 claim_action 打（否则就是"先查后标记"）
        self.assertNotIn("put", names, "引擎不得绕过专用接口直接写对象")
        self.assertNotIn("transition", names, "状态转换只由 claim_action / record_attempt 负责")
        self.assertLessEqual(
            set(names),
            {
                "get",
                "find",
                "find_one",
                "now",
                "claim_action",
                "record_attempt",
                "record_execution",
                "append_audit",
                "sender.send",
            },
            "执行路径只允许通过专用接口改状态",
        )

    def test_single_approval_cannot_authorize_a_second_action(self) -> None:
        """批准一旦被预留，就不得再服务第二个动作（§21.2 第 986 行）。"""
        case, action, summary, approval, _draft, version = self.fixture(self.store)
        second = self.make_outbound_action(self.store, case_id=case.id, version=version, approve=False)[0]
        # 绕过 bind_and_authorize 的显式检查，直接把同一个审批绑到第二个动作上
        raw = self.store.get("ExternalAction", second.id)
        raw.approval_id = approval.id
        self.store.put(raw, expected_revision=raw.revision, reason="测试：违规复用审批")
        self.store.transition("ExternalAction", second.id, "AUTHORIZED", actor_kind="USER", actor_id="owner")

        engine, sender = engine_for(self.store, steps=[SendScriptStep.succeed()])
        first = engine.execute(action.id)
        second_outcome = engine.execute(second.id)

        self.assertEqual("SUCCEEDED", first.result_class)
        self.assertEqual("REJECTED", second_outcome.result_class)
        self.assertEqual("APPROVAL_INVALID", second_outcome.code)
        self.assertEqual(1, sender.call_count, "同一审批只能支撑一次外呼")
        print("[evidence] 审批复用被拒 =", dump(second_outcome.to_dict()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
