"""控制台会话：HttpOnly Cookie 让手机 PWA 打开即用，同时不把令牌交给浏览器存储。

安全性质（逐条对应测试）：
1. 会话值只出现在 `Set-Cookie`，**不出现在响应体**；Cookie 带 `HttpOnly` / `SameSite=Strict`；
2. 拿到 Cookie 就能访问（不需要令牌），令牌本身可以完全离开客户端内存；
3. 会话鉴权的**写操作**必须带 `X-Asistent-Client` 头（防 CSRF 的第二道闸）；
4. 退出登录 = 吊销会话（服务端立即失效，不只是删 Cookie）；
5. 过期、伪造的会话值一律 401，且不区分"过期 vs 伪造"（不给探测信息）；
6. 登录时轮换：旧会话值立刻作废；
7. Bearer 令牌仍然可用（兼容旧客户端与 curl 排障）。
"""

from __future__ import annotations

import http.client
import json
import tempfile
import unittest

from asistent.console.sessions import ConsoleSessions

from .helpers import ConsoleTestCase, TEST_TOKEN, make_token_file

WRONG = "wrong-token-000000000000000000000000"


class ConsoleSessionsUnitTests(unittest.TestCase):
    """纯逻辑：假时钟验证 TTL / 滑动续期 / 吊销。"""

    def make(self, ttl: float = 60.0):
        clock = {"now": 1000.0}
        sessions = ConsoleSessions(ttl_seconds=ttl, now=lambda: clock["now"])
        return sessions, clock

    def test_create_and_validate(self):
        sessions, _ = self.make()
        value = sessions.create()
        self.assertTrue(value)
        self.assertTrue(sessions.valid(value))
        self.assertEqual(1, sessions.count())

    def test_expiry(self):
        sessions, clock = self.make(ttl=60.0)
        value = sessions.create()
        clock["now"] += 61.0
        self.assertFalse(sessions.valid(value))
        self.assertEqual(0, sessions.count(), "过期条目在校验时应被清掉")

    def test_sliding_renewal(self):
        sessions, clock = self.make(ttl=60.0)
        value = sessions.create()
        for _ in range(5):
            clock["now"] += 50.0  # 每次都在过期前用一下
            self.assertTrue(sessions.valid(value))
        clock["now"] += 61.0  # 停止使用后才过期
        self.assertFalse(sessions.valid(value))

    def test_forged_value_rejected(self):
        sessions, _ = self.make()
        sessions.create()
        self.assertFalse(sessions.valid("not-a-real-session"))
        self.assertFalse(sessions.valid(""))

    def test_revoke(self):
        sessions, _ = self.make()
        value = sessions.create()
        self.assertTrue(sessions.revoke(value))
        self.assertFalse(sessions.valid(value))
        self.assertFalse(sessions.revoke(value), "重复吊销返回 False，不报错")

    def test_stores_only_hashes(self):
        sessions, _ = self.make()
        value = sessions.create()
        # 内存里不能出现明文会话值：否则内存快照/调试输出就是可用凭据
        internal = "".join(sessions._sessions.keys())  # noqa: SLF001 - 这里就是要检查内部状态
        self.assertNotIn(value, internal)
        self.assertTrue(sessions.valid(value))

    def test_invalid_ttl_rejected(self):
        with self.assertRaises(ValueError):
            ConsoleSessions(ttl_seconds=0)


class SessionCookieHTTPTests(ConsoleTestCase):
    """HTTP 层：真服务、真 Cookie 头。"""

    def _login(self, client, token: str = TEST_TOKEN, cookie: str = ""):
        """用令牌换会话，返回 Set-Cookie 里的 cookie 值（cookie 非空 = 带着旧会话重登）。"""
        headers = {"Cookie": "asistent_session=" + cookie} if cookie else None
        recorded = self.client.call("POST", "/api/session", token=token, body={}, headers=headers)
        self.assertStatus(200, recorded)
        set_cookie = recorded.response_headers.get("Set-Cookie", "")
        self.assertIn("asistent_session=", set_cookie)
        return set_cookie.split(";")[0].split("=", 1)[1], recorded

    def test_set_cookie_attributes(self):
        _value, recorded = self._login(self.client)
        header = recorded.response_headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Strict", header)
        self.assertIn("Path=/", header)
        self.assertIn("Max-Age=", header)
        self.assertIn("Secure", header, "HTTPS 部署下必须带 Secure")
        # 会话值不能出现在响应体里（否则 JS 就拿到了）
        body = json.dumps(recorded.json, ensure_ascii=False)
        self.assertNotIn("asistent_session=", body)
        self.assertTrue(recorded.json.get("ok"))

    def test_cookie_alone_authenticates(self):
        value, _ = self._login(self.client)
        recorded = self.client.call("GET", "/api/overview", token=None, headers={"Cookie": "asistent_session=" + value})
        self.assertStatus(200, recorded)
        self.assertIn("pending", recorded.json)

    def test_cookie_write_needs_client_header(self):
        """Cookie 会自动随请求发送，所以写操作必须再要一个跨站发不出的头。"""
        value, _ = self._login(self.client)
        cookie = {"Cookie": "asistent_session=" + value}
        # 不带 X-Asistent-Client：拒绝
        recorded = self.client.call("POST", "/api/cases", token=None, headers=cookie, body={"title": "x"})
        self.assertStatus(403, recorded)
        self.assertEqual("FORBIDDEN", recorded.json["error"]["code"])
        # 带上：放行（201/200 由业务决定，这里只要求不是 403）
        recorded = self.client.call(
            "POST", "/api/cases", token=None,
            headers={**cookie, "X-Asistent-Client": "console"},
            body={"title": "会话写入的事务"},
        )
        self.assertNotEqual(403, recorded.status, recorded.response_body)

    def test_read_with_cookie_does_not_need_client_header(self):
        value, _ = self._login(self.client)
        recorded = self.client.call("GET", "/api/health", token=None, headers={"Cookie": "asistent_session=" + value})
        self.assertStatus(200, recorded)

    def test_logout_revokes_session_server_side(self):
        value, _ = self._login(self.client)
        cookie = {"Cookie": "asistent_session=" + value}
        recorded = self.client.call(
            "DELETE", "/api/session", token=None,
            headers={**cookie, "X-Asistent-Client": "console"},
        )
        self.assertStatus(200, recorded)
        self.assertIn("Max-Age=0", recorded.response_headers.get("Set-Cookie", ""), "退出时要清掉 Cookie")
        # 服务端已吊销：拿旧 Cookie 再来就是 401（不只是浏览器删了 Cookie）
        recorded = self.client.call("GET", "/api/overview", token=None, headers=cookie)
        self.assertStatus(401, recorded)

    def test_reauth_rotates_presented_session(self):
        """带着旧会话再登录：旧会话值立刻作废（同一设备别攒一堆长期有效会话）。"""
        first, _ = self._login(self.client)
        second, _ = self._login(self.client, cookie=first)
        self.assertNotEqual(first, second, "重新登录必须换一个新的会话值")
        recorded = self.client.call("GET", "/api/health", token=None, headers={"Cookie": "asistent_session=" + first})
        self.assertStatus(401, recorded, "被轮换掉的旧会话必须立刻失效")
        recorded = self.client.call("GET", "/api/health", token=None, headers={"Cookie": "asistent_session=" + second})
        self.assertStatus(200, recorded)

    def test_multiple_devices_have_independent_sessions(self):
        """两次不带旧 Cookie 的登录 = 两台设备：各自长期有效，互不踢下线。"""
        phone, _ = self._login(self.client)
        laptop, _ = self._login(self.client)
        self.assertNotEqual(phone, laptop)
        for value in (phone, laptop):
            recorded = self.client.call("GET", "/api/health", token=None, headers={"Cookie": "asistent_session=" + value})
            self.assertStatus(200, recorded)

    def test_forged_cookie_is_401_and_leaks_nothing(self):
        recorded = self.client.call("GET", "/api/overview", token=None, headers={"Cookie": "asistent_session=forged"})
        self.assertStatus(401, recorded)
        self.assertEqual("UNAUTHORIZED", recorded.json["error"]["code"])
        self.assertNotIn("forged", recorded.response_body)

    def test_bearer_token_still_works(self):
        recorded = self.client.call("GET", "/api/health", token=TEST_TOKEN)
        self.assertStatus(200, recorded)

    def test_health_reports_session_count_without_values(self):
        self._login(self.client)
        recorded = self.client.call("GET", "/api/health", token=TEST_TOKEN)
        self.assertStatus(200, recorded)
        self.assertEqual(1, recorded.json["sessions"]["active"])
        self.assertGreater(int(recorded.json["sessions"]["ttl_seconds"]), 0)
        self.assertNotIn("asistent_session", json.dumps(recorded.json, ensure_ascii=False))

    def test_wrong_token_does_not_create_session(self):
        recorded = self.client.call("POST", "/api/session", token=WRONG, body={})
        self.assertStatus(401, recorded)
        self.assertNotIn("Set-Cookie", recorded.response_headers, "鉴权失败绝不能发会话")


if __name__ == "__main__":
    unittest.main()
