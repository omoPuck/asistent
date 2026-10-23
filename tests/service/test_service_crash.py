"""重启安全：**提交了但没落库**的那一刻被杀，重启后不许重发（SPEC §13.3、§21.2）。

这一组只在 `ASISTENT_STORE=sqlite` 上跑：要证明"服务重启后还知道自己在哪"，就必须让
状态真的落在磁盘上、并且换一个**新进程**去读它。`reference` 是内存库，跨进程什么都读不到，
在它上面跑这类测试只会得到假结论。

两个方向都要钉：

* **提交后被杀** → 动作 `IN_FLIGHT`（在库里）→ 重启后 `UNKNOWN`、**0 次重发**；
  因为"可能已经发出去了"，自动重发是最大的恶。
* **已授权但还没执行** → 重启后必须**补上**（恰好一次）：不能因为怕重发就把用户批准的动作扔掉。
"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any, Dict, List

from asistent.contracts.enums import ActionState, ApprovalState, CaseState
from asistent.console.auth import TokenAuth
from asistent.console.service import ConsoleService
from tests.service.support import (
    FAKE_TOKEN,
    ServiceWorkbench,
    find_phase,
    parse_round_report,
    store_is_sqlite,
    submit_lines,
)

CRASH_CHILD = os.path.join("tests", "service", "crash_child_service.py")
CRASH_EXIT_CODE = 9


@unittest.skipUnless(store_is_sqlite(), "重启安全必须换进程：只在落盘存储（sqlite）上跑")
class TestCrashRestartSafety(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="mvp8-crash-", ignore_cleanup_errors=True)
        self.bench = ServiceWorkbench(self._tmp.name)
        self.bench.default_config()
        self.send_config = self.bench.sending_config()
        self.call_log = os.path.join(self.bench.state_dir, "outbound-calls.jsonl")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------ 工具
    def _approve(self) -> Dict[str, str]:
        """跑一轮拿到草稿，然后以"手机端"的身份批准它。返回 {"draft_id","version_id","approval_id"}。"""
        self.bench.add_notice("notice-01.eml", subject="周末科普讲座报名确认（含活动时间）")
        self.bench.run_round()
        from asistent.storage.sqlite_store import open_store

        store = open_store(self.bench.db_path)
        try:
            draft = list(store.find("Draft", limit=None))[0]
            version = store.get("DraftVersion", draft.current_version_id)
            console = ConsoleService(store, TokenAuth(FAKE_TOKEN), server_name="mvp8-test")
            approved = console.submit_approval({"draft_id": draft.id, "version_no": int(version.version_no), "confirm": True})
            return {
                "draft_id": draft.id,
                "version_id": version.id,
                "approval_id": approved["approval"]["id"],
            }
        finally:
            store.close()

    def _store(self) -> Any:
        from asistent.storage.sqlite_store import open_store

        return open_store(self.bench.db_path)

    def _action_state(self) -> Dict[str, Any]:
        store = self._store()
        try:
            actions = list(store.find("ExternalAction", limit=None))
            attempts = list(store.find("ActionAttempt", limit=None))
            cases = list(store.find("Case", limit=None))
            return {
                "actions": [{"id": item.id, "state": str(item.state), "attempt_ids": list(item.attempt_ids or [])} for item in actions],
                "attempts": [{"id": item.id, "state": str(item.state)} for item in attempts],
                "cases": [{"id": item.id, "state": str(item.state)} for item in cases],
            }
        finally:
            store.close()

    # ------------------------------------------------------------------ 场景 1
    def test_killed_after_submit_becomes_unknown_and_never_resends(self) -> None:
        approved = self._approve()

        # ---- 强杀：外发已提交、结果未落库
        crashed = self.bench.run_script(CRASH_CHILD, self.send_config, self.call_log)
        self.assertEqual(
            crashed.returncode,
            CRASH_EXIT_CODE,
            "子进程必须是被 os._exit(%d) 强杀的，而不是自己捕获异常退出：\n%s" % (CRASH_EXIT_CODE, crashed.raw()),
        )
        self.assertNotIn("Traceback", crashed.stderr, "被强杀不该有 Python 回溯（那不是被杀死，是崩了）")
        self.assertIn("crash-child", crashed.stdout + crashed.stderr, "子进程必须真的跑到了外发那一步")
        after_crash = self._action_state()
        self.assertEqual(after_crash["actions"][0]["state"], str(ActionState.IN_FLIGHT), "领取已经落库（在库里，不在内存）")
        self.assertEqual(after_crash["cases"][0]["state"], str(CaseState.EXECUTING))
        self.assertEqual(len(submit_lines(self.call_log)), 1, "通道账本：确实已经提交过一次")

        # ---- 重启：新进程跑一轮，先恢复再决定
        restarted = self.bench.run_round(self.send_config, expect_ok=True)
        report = parse_round_report(restarted.stdout)
        recover = find_phase(report, "recover")["detail"]
        self.assertEqual(len(recover["demoted_action_ids"]), 1, "IN_FLIGHT 必须被降级为 UNKNOWN：结果未知")
        self.assertEqual(recover["resent_action_ids"], [], "恢复路径绝不允许重发")
        self.assertEqual(recover["sender_call_count"], 0, "恢复用掉的通道调用次数必须是 0")
        # 事务回写由引擎或恢复阶段完成（二者都写审计）；这里断言**结果**，不绑实现分工
        self.assertEqual(
            self._action_state()["cases"][0]["state"],
            str(CaseState.UNKNOWN),
            "结果未知的动作所在事务必须落 UNKNOWN 等用户裁决",
        )

        outbound = find_phase(report, "outbound")["detail"]
        self.assertEqual(outbound.get("executed", []), [], "结果未知的动作不得再次执行")
        self.assertEqual(len(submit_lines(self.call_log)), 1, "**通道只被提交过一次**（没有重发）")

        settled = self._action_state()
        self.assertEqual(settled["actions"][0]["state"], str(ActionState.UNKNOWN))
        self.assertEqual(settled["cases"][0]["state"], str(CaseState.UNKNOWN), "事务也必须落 UNKNOWN 等用户裁决")
        self.assertEqual(report["pending"]["actions_unknown"], 1)
        self.assertIn("action_unknown", report["waiting_on"], "健康快照要明确说'有动作结果未知'")

        # ---- 再跑一轮：永远不自动重发
        again = self.bench.run_round(self.send_config)
        again_report = parse_round_report(again.stdout)
        self.assertEqual(find_phase(again_report, "outbound")["detail"].get("executed", []), [])
        self.assertEqual(len(submit_lines(self.call_log)), 1, "第二、第三轮也不得重发")
        self.assertEqual(again_report["pending"]["actions_unknown"], 1)

        # ---- 用户的审批仍然在，且没有被偷偷"重开"
        store = self._store()
        try:
            approval = store.get("Approval", approved["approval_id"])
            self.assertEqual(str(approval.state), str(ApprovalState.ACTIVE))
            self.assertEqual(str(approval.target_object_ids[0]), approved["version_id"])
        finally:
            store.close()

    # ------------------------------------------------------------------ 场景 2
    def test_authorized_action_survives_restart_and_is_sent_exactly_once(self) -> None:
        """已经授权、但还没执行的动作不能丢：重启后补发，且只有一次。"""
        self._approve()
        # 第一轮：通道关闭（outbound.enabled=false）→ 动作落库成 AUTHORIZED，但绝不外呼
        held = self.bench.holding_config()
        self.bench.run_round(held)
        held_state = self._action_state()
        self.assertEqual(held_state["actions"][0]["state"], str(ActionState.AUTHORIZED))
        self.assertEqual(submit_lines(self.call_log), [], "通道关着的时候一次都不能调")

        # 第二轮（新进程、换成真通道）：补上这一次执行，恰好一次
        executed = self.bench.run_round(self.send_config)
        report = parse_round_report(executed.stdout)
        self.assertEqual(find_phase(report, "recover")["detail"]["demoted_action_ids"], [])
        self.assertEqual(len(find_phase(report, "outbound")["detail"]["executed"]), 1)
        self.assertEqual(len(submit_lines(self.call_log)), 1)

        # 第三轮：不许第二次
        self.bench.run_round(self.send_config)
        self.assertEqual(len(submit_lines(self.call_log)), 1, "补发之后不得再发")
        final = self._action_state()
        self.assertEqual(final["actions"][0]["state"], str(ActionState.SUCCEEDED))
        self.assertEqual(len(final["attempts"]), 1)





if __name__ == "__main__":  # pragma: no cover
    unittest.main()
