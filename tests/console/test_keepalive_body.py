"""keep-alive 上的请求体消费：一个"只在反代后面才出现"的 501。

现场是怎么发现的
----------------
手机经 `Caddy -> frps -> frpc -> 控制台(18090)` 访问时，偶尔 501，重试又好了：

    Error code: 501
    Message: Unsupported method ('{}GET').

根因：`POST /api/session` 的请求体是 `{}`（2 字节），但那个端点不需要参数，于是**没读**。
Caddy 会复用到底座的 keep-alive 连接；残留的 `{}` 和下一个请求行粘在一起，
Python 的 `http.server` 把 `{}GET` 当成了方法名。

这条 bug 在本机测不出来（每个请求开新连接），只在"反代复用连接"时出现，
所以必须有针对性回归：**同一个连接**上先发带 body 的请求，再发下一个请求。

修复方式：`_handle` 在分发之前一次性把请求体读完（`_consume_request_body`），
所有端点共用 `_body_bytes`；长度非法/超限时标记关闭连接，绝不让残字节留在连接里。
"""

from __future__ import annotations

import http.client
import json
import unittest

from .helpers import ConsoleTestCase, TEST_TOKEN


class KeepAliveBodyTests(ConsoleTestCase):
    """同一连接上的连续请求：修复前第二个会看到 `{}GET` 而回 501。"""

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.client.host, self.client.port, timeout=15)

    def _host_header(self) -> str:
        return "%s:%d" % (self.client.host, self.client.port)

    @staticmethod
    def _read(response) -> tuple:
        return response.status, response.read().decode("utf-8", errors="replace")

    def test_body_ignoring_post_does_not_desync_connection(self):
        """POST /api/session（端点不读 body）之后的请求必须正常。"""
        connection = self._connection()
        try:
            connection.request(
                "POST", "/api/session", body=b"{}",
                headers={
                    "Host": self._host_header(),
                    "Accept": "application/json",
                    "Authorization": "Bearer %s" % TEST_TOKEN,
                    "Content-Type": "application/json",
                    "Content-Length": "2",
                },
            )
            status, body = self._read(connection.getresponse())
            self.assertEqual(200, status, body)

            # 关键：同一个连接（Caddy 复用的就是这种连接）
            connection.request(
                "GET", "/api/overview",
                headers={"Host": self._host_header(), "Accept": "application/json",
                         "Authorization": "Bearer %s" % TEST_TOKEN},
            )
            status, body = self._read(connection.getresponse())
            self.assertEqual(200, status, "同一连接上的第二个请求被 501 了：%r" % (body[:200],))
            self.assertIn("pending", json.loads(body))
        finally:
            connection.close()

    def test_two_body_posts_on_one_connection(self):
        """连续两个带 body 的 POST（两个端点）也必须各自正常。"""
        connection = self._connection()
        try:
            for path, payload in (("/api/session", b"{}"), ("/api/cases", b"{}")):
                connection.request(
                    "POST", path, body=payload,
                    headers={
                        "Host": self._host_header(),
                        "Accept": "application/json",
                        "Authorization": "Bearer %s" % TEST_TOKEN,
                        "X-Asistent-Client": "console",
                        "Content-Type": "application/json",
                        "Content-Length": str(len(payload)),
                    },
                )
                status, body = self._read(connection.getresponse())
                self.assertNotEqual(501, status, "%s 之后的请求行被污染：%r" % (path, body[:200]))
        finally:
            connection.close()

    def test_oversize_body_closes_connection(self):
        """超限 body 不硬读，但要明确关连接（否则残字节会污染下一个请求）。"""
        connection = self._connection()
        try:
            connection.request(
                "POST", "/api/session", body=b"x",
                headers={
                    "Host": self._host_header(),
                    "Accept": "application/json",
                    "Authorization": "Bearer %s" % TEST_TOKEN,
                    "Content-Type": "application/json",
                    "Content-Length": "999999999",  # 远超上限
                },
            )
            response = connection.getresponse()
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
            self.assertEqual(400, status, body[:200])
            self.assertEqual("close", (response.getheader("Connection") or "").lower())
        finally:
            connection.close()

    def test_invalid_content_length_closes_connection(self):
        connection = self._connection()
        try:
            connection.putrequest("POST", "/api/session", skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", self._host_header())
            connection.putheader("Accept", "application/json")
            connection.putheader("Authorization", "Bearer %s" % TEST_TOKEN)
            connection.putheader("Content-Length", "not-a-number")
            connection.endheaders()
            response = connection.getresponse()
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
            self.assertEqual(400, status, body[:200])
            self.assertEqual("close", (response.getheader("Connection") or "").lower())
        finally:
            connection.close()

    def test_empty_body_post_for_json_endpoint_still_400(self):
        """回归保护：把"必须带 JSON 体"的校验留在语义层（400），不是读坏连接。"""
        connection = self._connection()
        try:
            connection.request(
                "POST", "/api/cases", body=b"",
                headers={
                    "Host": self._host_header(),
                    "Accept": "application/json",
                    "Authorization": "Bearer %s" % TEST_TOKEN,
                    "Content-Length": "0",
                },
            )
            status, body = self._read(connection.getresponse())
            self.assertEqual(400, status, body[:200])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
