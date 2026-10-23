"""鉴权失败节流：公网暴露下"令牌是唯一门禁"必须有爆破成本上限。

分两层测：
1. `AuthThrottle` 纯逻辑 —— 用**假时钟**验证窗口、冷却、指数升级、成功清零，
   不靠 sleep（sleep 会让测试又慢又飘）；
2. HTTP 层 —— 连续错令牌后必须真的返回 429（连正确令牌也拦住），带 `Retry-After`，
   且响应体与日志都不泄露令牌。
"""

from __future__ import annotations

import unittest

from asistent.console.throttle import AuthThrottle

from .helpers import ConsoleTestCase, TEST_TOKEN

WRONG = "wrong-token-000000000000000000000000"


class _FakeClock:
    """可控时钟：`now()` 返回秒数，`advance()` 推进。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class AuthThrottleLogicTests(unittest.TestCase):
    """纯逻辑：不启服务、不 sleep。"""

    def make(self, *, max_failures=3, window=600.0, base=60.0, cap=300.0):
        clock = _FakeClock()
        return AuthThrottle(
            max_failures=max_failures,
            window_seconds=window,
            base_lockout_seconds=base,
            max_lockout_seconds=cap,
            now=clock,
        ), clock

    def test_below_threshold_does_not_lock(self):
        throttle, _ = self.make()
        self.assertIsNone(throttle.record_failure())
        self.assertIsNone(throttle.record_failure())
        self.assertIsNone(throttle.check())
        self.assertEqual(2, throttle.snapshot()["failures_in_window"])

    def test_threshold_triggers_lockout(self):
        throttle, _ = self.make(max_failures=3, base=60.0)
        throttle.record_failure()
        throttle.record_failure()
        self.assertEqual(60.0, throttle.record_failure())  # 第 3 次触发冷却
        remaining = throttle.check()
        self.assertIsNotNone(remaining)
        self.assertAlmostEqual(60.0, remaining, places=3)
        # 触发后窗口计数清零，不会因为"再错一次"立刻翻倍
        self.assertEqual(0, throttle.snapshot()["failures_in_window"])

    def test_lockout_expires(self):
        throttle, clock = self.make(max_failures=2, base=60.0)
        throttle.record_failure()
        self.assertEqual(60.0, throttle.record_failure())
        self.assertIsNotNone(throttle.check())
        clock.advance(59.0)
        self.assertIsNotNone(throttle.check())
        clock.advance(2.0)
        self.assertIsNone(throttle.check())
        # 冷却结束后计数已清零：再错一次不该立刻又锁
        self.assertIsNone(throttle.record_failure())

    def test_lockout_escalates_and_is_capped(self):
        throttle, clock = self.make(max_failures=1, base=60.0, cap=300.0)
        self.assertEqual(60.0, throttle.record_failure())
        clock.advance(61.0)
        self.assertEqual(120.0, throttle.record_failure())  # 连续第二次：翻倍
        clock.advance(121.0)
        self.assertEqual(240.0, throttle.record_failure())
        clock.advance(241.0)
        self.assertEqual(300.0, throttle.record_failure())  # 触顶，不能无限涨
        clock.advance(301.0)
        self.assertEqual(300.0, throttle.record_failure())

    def test_success_resets_failures_and_level(self):
        throttle, clock = self.make(max_failures=2, base=60.0)
        throttle.record_failure()
        throttle.record_failure()  # 触发一次冷却，等级 = 1
        clock.advance(61.0)
        throttle.record_success()
        snap = throttle.snapshot()
        self.assertEqual(0, snap["failures_in_window"])
        self.assertEqual(0, snap["lockout_level"])
        self.assertEqual(0.0, snap["lockout_seconds_remaining"])
        # 清零后再失败，仍从基础时长开始（不继承之前的升级）
        throttle.record_failure()
        self.assertEqual(60.0, throttle.record_failure())

    def test_failures_outside_window_are_forgotten(self):
        throttle, clock = self.make(max_failures=3, window=600.0)
        throttle.record_failure()
        clock.advance(601.0)
        throttle.record_failure()
        self.assertEqual(1, throttle.snapshot()["failures_in_window"])  # 老的那次被清掉
        self.assertIsNone(throttle.record_failure())

    def test_snapshot_has_no_token_or_client_fields(self):
        throttle, _ = self.make()
        throttle.record_failure()
        keys = set(throttle.snapshot())
        # 只允许出现计数/时长/等级，不允许出现任何"凭据"或"客户端标识"字段
        self.assertEqual(
            {"failures_in_window", "max_failures", "window_seconds",
             "lockout_seconds_remaining", "lockout_level"},
            keys,
        )
        for banned in ("token", "authorization", "client", "ip", "remote"):
            self.assertNotIn(banned, " ".join(keys).lower())

    def test_invalid_parameters_are_rejected(self):
        with self.assertRaises(ValueError):
            AuthThrottle(max_failures=0)
        with self.assertRaises(ValueError):
            AuthThrottle(base_lockout_seconds=0)


class AuthThrottleHTTPTests(ConsoleTestCase):
    """HTTP 层：连续错令牌 -> 429，且不泄露令牌。"""

    def test_repeated_wrong_tokens_lead_to_429(self):
        client, _server = self.start_server(
            settings_overrides={"max_auth_failures": 3, "auth_lockout_seconds": 30.0}
        )
        for _ in range(3):
            self.assertStatus(401, client.get("/api/health", token=WRONG))

        recorded = client.get("/api/health")  # 此时连**正确**令牌也被冷却挡住
        self.assertStatus(429, recorded)
        self.assertEqual("AUTH_THROTTLED", recorded.json["error"]["code"])
        self.assertIn("Retry-After", recorded.response_headers)
        self.assertGreaterEqual(int(recorded.response_headers["Retry-After"]), 1)
        # 冷却响应里既没有令牌，也没有任何对象维度信息
        self.assertNotIn(TEST_TOKEN, recorded.response_body)
        self.assertNotIn(WRONG, recorded.response_body)
        self.assertEqual(
            int(recorded.response_headers["Retry-After"]),
            recorded.json["error"]["detail"]["retry_after_seconds"],
        )

    def test_throttle_trip_is_logged_without_secrets(self):
        client, _server = self.start_server(
            settings_overrides={"max_auth_failures": 2, "auth_lockout_seconds": 30.0}
        )
        for _ in range(2):
            client.get("/api/health", token=WRONG)
        client.get("/api/health")
        joined = "\n".join(self.log_lines)
        self.assertIn("auth-throttle", joined)
        self.assertNotIn(TEST_TOKEN, joined)
        self.assertNotIn(WRONG, joined)

    def test_health_exposes_throttle_state(self):
        recorded = self.client.get("/api/health")
        self.assertStatus(200, recorded)
        auth = recorded.json["auth"]
        self.assertEqual(0, auth["failures_in_window"])
        self.assertGreaterEqual(auth["max_failures"], 1)
        self.assertEqual(0.0, auth["lockout_seconds_remaining"])
        # 健康信息里也不能出现令牌
        self.assertTokenAbsent(recorded)

    def test_success_resets_window(self):
        client, _server = self.start_server(settings_overrides={"max_auth_failures": 3})
        self.assertStatus(401, client.get("/api/health", token=WRONG))
        self.assertStatus(401, client.get("/api/health", token=WRONG))
        self.assertStatus(200, client.get("/api/health"))
        self.assertEqual(0, client.get("/api/health").json["auth"]["failures_in_window"])


if __name__ == "__main__":
    unittest.main()
