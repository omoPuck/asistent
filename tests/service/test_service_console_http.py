"""手机端那条路：控制台 HTTP（同一份存储、同一个进程在跑）。

§877 要的是"能在手机控制台看到待处理与审批状态"。这一组用**真的 HTTP 请求**证明：

1. 服务**没人连也在轮转**（先连着跑两轮，期间客户端请求数为 0）；
2. 健康快照能被手机端**下载**（`GET /api/artifacts` → `service-status.json` → `GET .../content`），
   里面能看出"跑了几轮 / 在等什么 / 每一路为什么没干活"；
3. 审批是**从手机端提交**的（`POST /api/approvals`），提交后服务下一轮就能接着走；
4. 没令牌一律 401（同一台设备上不开后门）。

只在 sqlite 后端跑：跨进程的"真的在跑"必须有落盘存储可查（reference 是内存实现）。
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import time
import unittest
import urllib.parse
from typing import Any, Dict, List, Optional

from asistent.contracts.enums import ApprovalState
from asistent.service.status_artifact import STATUS_ARTIFACT_NAME
from tests.service.support import (
    FAKE_TOKEN,
    ServiceProcess,
    ServiceWorkbench,
    store_is_sqlite,
)


def _console_config(bench: ServiceWorkbench, *, name: str = "service-console.json", **overrides: Any) -> str:
    """控制台配置：**把 state 目录也加进下载白名单**，否则健康快照读出来是 404。

    这是真机配置必须照抄的一条：`console.artifact_roots` 默认只覆盖 `mail.raw_root`，
    而健康快照在 `state_dir` 下。少这一条，手机端就"点得到文件名、打不开内容"。
    """
    console: Dict[str, Any] = {
        "enabled": True,
        "bind": "127.0.0.1",
        "port": 0,
        "token_path": bench.token_path,
        "artifact_roots": [bench.state_dir, os.path.join(bench.var_root, "raw")],
    }
    console.update(overrides.pop("console", {}) or {})
    return bench.write_config(name, console=console, **overrides)


class ConsoleClient:
    """最小 HTTP 客户端（Bearer 令牌；不理解业务，只发请求）。"""

    def __init__(self, port: int, *, token: Optional[str] = FAKE_TOKEN, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = int(port)
        self.token = token
        self.requests = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        token: Optional[str] = None,
    ) -> Dict[str, Any]:
        use = self.token if token is None else token
        headers: Dict[str, str] = {"Accept": "application/json"}
        if use:
            headers["Authorization"] = "Bearer " + use
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(self.host, self.port, timeout=15)
        self.requests += 1
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return {
                "status": response.status,
                "headers": dict(response.getheaders()),
                "raw": raw,
                "text": raw.decode("utf-8", "replace"),
            }
        finally:
            connection.close()

    def json(self, path: str, *, token: Optional[str] = None) -> Dict[str, Any]:
        result = self.request("GET", path, token=token)
        if result["status"] != 200:
            raise AssertionError("GET %s → %s\n%s" % (path, result["status"], result["text"][:500]))
        return json.loads(result["text"])


@unittest.skipUnless(store_is_sqlite(), "跨进程常驻 + 存储读回需要 sqlite 后端（ASISTENT_STORE=sqlite）")
class TestConsoleHttp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mvp8-console-", ignore_cleanup_errors=True)
        self.bench = ServiceWorkbench(self._tmp.name)
        self.process: Optional[ServiceProcess] = None

    def tearDown(self) -> None:
        if self.process is not None:
            self.process.close()
        self.bench.close_runtimes()
        self._tmp.cleanup()

    # ------------------------------------------------------------------ 起服务
    def _start(self, config_path: str, *, interval: int = 1, extra: tuple = ()) -> ServiceProcess:
        """起常驻进程：`--max-rounds 0` = 一直跑（配置里的 max_rounds 只影响 `--once` 之外的默认值）。"""
        self.process = self.bench.spawn(
            "--loop",
            "--console",
            "--max-rounds",
            "0",
            "--interval",
            str(interval),
            "--config",
            config_path,
            *extra,
        )
        return self.process

    def _snapshot_via_http(self, client: ConsoleClient) -> Dict[str, Any]:
        """手机端拿到健康快照的完整路径（列表 → 下载内容 → 解析 JSON）。"""
        listing = client.json("/api/artifacts")
        rows = [row for row in listing.get("artifacts") or [] if row.get("name") == STATUS_ARTIFACT_NAME]
        self.assertTrue(rows, "手机端在 /api/artifacts 里看不到健康快照：%s" % json.dumps(listing, ensure_ascii=False)[:600])
        row = rows[0]
        content = client.request("GET", "/api/artifacts/%s/content" % urllib.parse.quote(str(row["id"])))
        self.assertEqual(content["status"], 200, content["text"][:400])
        self.assertEqual(content["headers"].get("X-Artifact-Id"), row["id"])
        self.assertEqual(len(content["raw"]), int(row.get("size_bytes") or len(content["raw"])))
        return json.loads(content["text"])

    # ------------------------------------------------------------------ 1
    def test_resident_service_keeps_rounding_with_zero_clients_and_publishes_health(self) -> None:
        self.bench.add_notice("notice-01.eml", subject="周末科普讲座报名确认（含活动时间）")
        config_path = _console_config(self.bench)
        process = self._start(config_path)

        # (a) 启动即打印（未缓冲）：证明"服务真的起来了"，而不是卡在启动里
        process.wait_for(r"^\[service\] 启动 run_id=")
        process.wait_for(r"^\[service\] 配置 ")
        port = process.console_port()
        # (b) 连续两轮，期间**没有任何客户端连接**
        process.wait_for_rounds(2, timeout=90)
        client = ConsoleClient(port)
        self.assertEqual(client.requests, 0, "取快照之前不该有任何请求")

        # (c) 手机端：列表 → 下载 → 解析（这就是 §877 的"能看到状态"）
        snapshot = self._snapshot_via_http(client)
        self.assertGreaterEqual(snapshot.get("rounds_completed", 0), 2, snapshot)
        self.assertIn("pending", snapshot)
        self.assertIn("waiting_on", snapshot)
        self.assertIn("notes", snapshot)
        self.assertIn("last_sync", snapshot)
        self.assertIn("status_artifact", snapshot)
        self.assertTrue(snapshot["status_artifact"]["published"])
        self.assertTrue(str(snapshot["status_artifact"]["sha256"]))

        # (d) 手机端也能看到事务/草稿/待审批（不是只能看一个自述文件）
        cases = client.json("/api/cases")
        self.assertGreaterEqual(int(cases.get("total") or 0), 1)
        drafts = client.json("/api/drafts")
        self.assertGreaterEqual(int(drafts.get("total") or 0), 1)
        questions = client.json("/api/questions")
        self.assertIn("total", questions)
        overview = client.json("/api/overview")
        self.assertTrue(overview.get("etag"))

        # (e) 关键数字与"另一条进程自己看到的"一致：快照不是编的
        status_from_cli = self.bench.status(config_path)
        self.assertEqual(int(snapshot["rounds_completed"]), int(status_from_cli["rounds_completed"]))

    # ------------------------------------------------------------------ 2
    def test_approval_can_be_submitted_from_the_console_and_the_service_continues(self) -> None:
        self.bench.add_notice("notice-01.eml", subject="周末科普讲座报名确认（含活动时间）")
        config_path = _console_config(
            self.bench,
            name="service-console-send.json",
            outbound={"enabled": True, "channel": "scripted", "from_account": "parent@example.com"},
        )
        process = self._start(config_path)
        port = process.console_port()
        process.wait_for_rounds(1, timeout=60)
        client = ConsoleClient(port)

        drafts = client.json("/api/drafts")["drafts"]
        draft_id = str(drafts[0]["id"])
        detail = client.json("/api/drafts/%s" % urllib.parse.quote(draft_id))
        current = detail.get("current") or {}
        version_no = int(current.get("version_no") or 0)
        self.assertGreaterEqual(version_no, 1, "手机端要能拿到当前版本号才能提交审批")
        submitted = client.request(
            "POST", "/api/approvals", body={"draft_id": draft_id, "version_no": version_no, "confirm": True}
        )
        self.assertEqual(submitted["status"], 201, submitted["text"][:400])
        approval = json.loads(submitted["text"])["approval"]
        self.assertEqual(approval["state"], str(ApprovalState.ACTIVE))

        # 服务下一轮把批准变成外发（受控假通道），并且**只发一次**
        rounds_before = process.rounds_seen()
        process.wait_for_rounds(rounds_before + 2, timeout=120)
        submissions = self._submit_lines_from_log()
        self.assertEqual(len(submissions), 1, "同一封回信只能提交一次：%s" % submissions)

        # 手机端能读到"批准被谁消费掉了"：动作级审批 CONSUMED，且带着 attempt id
        listed = client.json("/api/approvals")["approvals"]
        consumed = [row for row in listed if row.get("state") == str(ApprovalState.CONSUMED)]
        self.assertEqual(len(consumed), 1, "只应有一个动作级审批被消费：%s" % listed)
        self.assertTrue(consumed[0].get("consumed_by_attempt_id"), "消费必须留痕（attempt id）")
        # 用户从手机端提交的那条审批仍在，且**绑定了具体草稿版本**（不是"批准了某类事"）
        user_row = client.json("/api/approvals/%s" % urllib.parse.quote(str(approval["id"])))  # 单条详情直接返回该行
        self.assertEqual(user_row["target_object_ids"], [current["id"]], "审批必须绑定到具体版本")

        # 事务已闭环：手机端看得到已完成（含已关闭）的事务
        cases = client.json("/api/cases?include_closed=1")["cases"]
        self.assertTrue(cases, "闭环后的事务（含已关闭）也要能查到")
        case_id = str(cases[0]["id"])
        detail = client.json("/api/cases/%s?include=audit" % urllib.parse.quote(case_id))
        self.assertTrue(detail, "事务详情不该是空")
        snapshot = self._snapshot_via_http(client)
        self.assertGreaterEqual(snapshot.get("rounds_completed", 0), rounds_before + 2)
        self.assertEqual(int(snapshot.get("submit_count_on_disk") or 0), 1, "快照里的账本计数也要是 1")
        self.assertEqual(int(snapshot["pending"]["actions_unknown"]), 0, "这次外发结果明确，不该有未知动作")

    # ------------------------------------------------------------------ 3
    def test_without_token_everything_is_401(self) -> None:
        self.bench.add_notice("notice-01.eml", subject="周末科普讲座报名确认（含活动时间）")
        config_path = _console_config(self.bench, name="service-console-auth.json")
        process = self._start(config_path)
        port = process.console_port()
        process.wait_for_rounds(1, timeout=60)
        client = ConsoleClient(port)
        for path in ("/api/overview", "/api/cases", "/api/artifacts", "/api/approvals"):
            result = client.request("GET", path, token="")
            self.assertEqual(result["status"], 401, "%s 无令牌却返回 %s" % (path, result["status"]))
            self.assertNotIn(FAKE_TOKEN, result["text"])
        # 令牌**错**也不行
        self.assertEqual(client.request("GET", "/api/cases", token="wrong-token-000000")["status"], 401)

    # ------------------------------------------------------------------ 4
    def test_graceful_stop_on_sigterm_finishes_the_round_then_exits(self) -> None:
        if os.name == "nt":  # pragma: no cover - Windows 没有真正的 SIGTERM
            self.skipTest("Windows 上 terminate() 是强杀，优雅停止只能在 POSIX/设备上验证")
        self.bench.add_notice("notice-01.eml", subject="周末科普讲座报名确认（含活动时间）")
        config_path = _console_config(self.bench, name="service-console-stop.json")
        process = self._start(config_path, interval=2)
        process.wait_for_rounds(1, timeout=60)
        code = process.stop(timeout=30)
        self.assertEqual(code, 0, "优雅停止必须是 0：\n%s" % process.tail(40))
        self.assertIn("收到停止请求", process.output())
        rounds_before = process.rounds_seen()
        self.assertGreaterEqual(rounds_before, 1)
        # 停止后不再跑新轮：等一会儿，轮次行数不变
        time.sleep(3)
        self.assertEqual(process.rounds_seen(), rounds_before, "停止后不该再开新轮")

    # ------------------------------------------------------------------ 5
    def test_console_disabled_means_no_server_and_no_token(self) -> None:
        """配置里关掉控制台，就一个监听都不开（也绝不偷偷生成令牌文件）。"""
        token_path = os.path.join(self.bench.var_root, "config", "no-such-token")
        config_path = self.bench.write_config(
            "service-console-off.json",
            console={"enabled": False, "bind": "127.0.0.1", "port": 0, "token_path": token_path},
        )
        process = self.bench.spawn("--once", "--console", "--config", config_path)
        self.process = process
        process.wait_for(r"^\[service\] 退出", timeout=60)
        self.assertNotIn("控制台监听", process.output())
        self.assertFalse(os.path.exists(token_path), "关掉控制台就不该生成令牌文件")

    # ------------------------------------------------------------------ 工具
    def _submit_lines_from_log(self) -> List[Dict[str, Any]]:
        """外发账本（`<state_dir>/outbound-calls.jsonl`）：同一封回信只应有 1 行 submit。"""
        from tests.service.support import submit_lines

        path = os.path.join(self.bench.state_dir, "outbound-calls.jsonl")
        return submit_lines(path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
