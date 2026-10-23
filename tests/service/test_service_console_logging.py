"""控制台日志必须真的接到服务日志（`service.log`）。

为什么单独有这组测试
--------------------
真实缺陷（2026-10-23 在设备上发现）：`runtime.start_console()` 调 `build_server()` 时
**没有传 `log_sink`**，于是部署环境里所有 HTTP 请求日志都是空操作——包括 401、429 和
"鉴权失败达阈值、开始全局冷却"这条安全事件。而 `tests/console/**` 全绿，因为测试
helpers 自己传了 `log_sink`：**单测覆盖了控制台内部，却没覆盖"谁把 sink 接上去"**。

这组测试从 `ServiceRuntime` 这一层断言，专堵"单测绿、线上瞎"。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from .support import ServiceWorkbench

WRONG_TOKEN = "wrong-token-000000000000000000000000"


class ConsoleLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="console-log-", ignore_cleanup_errors=True)
        self.bench = ServiceWorkbench(self._tmp.name)
        self.lines: list[str] = []

    def tearDown(self) -> None:
        self.bench.close_runtimes()
        self._tmp.cleanup()

    # ---- 工具 ----
    def _await_line(self, needle: str, *, timeout: float = 5.0) -> bool:
        """等某行日志出现。

        为什么必须等：请求日志写在 HTTP 处理线程的 `finally` 里，**响应可能先于日志**
        到达客户端。设备上（RK3588，比开发机慢）实测就卡在这里——客户端拿到 401 立刻
        断言，append 还没发生。这不是"偶发"，是确定的时序问题，所以要有界轮询而不是裸 sleep。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(needle in line for line in list(self.lines)):
                return True
            time.sleep(0.01)
        return any(needle in line for line in list(self.lines))
    def _start(self):
        runtime = self.bench.runtime(
            self.bench.write_config("service-log.json"),
            log=self.lines.append,
            start_console=True,
        )
        handle = runtime.start_console()
        self.assertTrue(handle.url.startswith("http://127.0.0.1:"), "控制台没起来：%r" % (handle.error,))
        return runtime, handle

    def _call(self, handle, *, token: str = "") -> int:
        request = urllib.request.Request(handle.url.rstrip("/") + "/api/health")
        if token:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    # ---- 测试 ----
    def test_unauthenticated_request_line_reaches_service_log(self) -> None:
        runtime, handle = self._start()
        self.assertEqual(401, self._call(handle))
        self.assertTrue(
            self._await_line("auth=denied"), "401 必须写进服务日志，否则线上看不见有人在敲门：%r" % (self.lines,)
        )
        joined = "\n".join(self.lines)
        self.assertIn("/api/health", joined)
        # 日志里不能出现令牌（这是既有约定，接上 sink 之后必须继续成立）
        self.assertNotIn(runtime.token, joined)

    def test_throttle_trigger_reaches_service_log(self) -> None:
        runtime, handle = self._start()
        for _ in range(20):
            self.assertEqual(401, self._call(handle, token=WRONG_TOKEN))
        self.assertEqual(429, self._call(handle))  # 冷却中，正确令牌也进不来
        self.assertTrue(self._await_line("auth-throttle"), "冷却触发必须写进服务日志：%r" % (self.lines,))
        self.assertTrue(self._await_line("-> 429"), "429 响应本身也要进日志：%r" % (self.lines,))
        joined = "\n".join(self.lines)
        self.assertNotIn(WRONG_TOKEN, joined)
        self.assertNotIn(runtime.token, joined)

    def test_health_snapshot_is_still_clean_after_logging_wired(self) -> None:
        runtime, handle = self._start()
        request = urllib.request.Request(handle.url.rstrip("/") + "/api/health")
        request.add_header("Authorization", "Bearer " + runtime.token)
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertIn("auth", payload)
        self.assertNotIn(runtime.token, json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
