"""鉴权与"未鉴权零信息泄露"。

这是 SPEC §13.4 与 AT-08 的硬要求：无 token / 错 token 必须 401，
且**响应体里不能有任何"某个 Case 是否存在"的线索**。

做法（关键，不要改成"先查再判"）：

* 401 判定发生在任何 store 读取之前 —— 所以"存在的 ID"和"不存在的 ID"
  在未鉴权时返回的**字节级完全一致**，攻击者无法用响应差异枚举对象；
* token 只从 `Authorization: Bearer` 读，URL 里的 `?token=` 一律无效；
* 静态壳（index.html 等）同样要鉴权，否则连"这台设备在跑什么"都会泄露。
"""

from __future__ import annotations

import json
import os
import unittest

from asistent.console import ConsoleService, ConsoleSettings, TokenAuth, build_server
from asistent.console.auth import AuthError, MIN_TOKEN_LENGTH, load_token
from asistent.console.settings import DEFAULT_PORT

from .helpers import ConsoleTestCase, TEST_TOKEN, make_token_file, posix_permissions_supported


class AuthHeaderTests(ConsoleTestCase):
    """请求头里的 token 处理。"""

    def test_health_without_token_is_401_and_leaks_nothing(self):
        recorded = self.client.get("/api/health", token=None)
        self.assertStatus(401, recorded)
        payload = recorded.json
        self.assertEqual("UNAUTHORIZED", payload["error"]["code"])
        self.assertEqual("需要有效的控制台令牌", payload["error"]["message"])
        # 没有 detail、没有对象类型、没有数量
        self.assertNotIn("detail", payload["error"])
        self.assertEqual('Bearer realm="asistent-console"', recorded.response_headers.get("WWW-Authenticate"))

    def test_health_with_correct_token_is_200(self):
        recorded = self.client.get("/api/health")
        self.assertStatus(200, recorded)
        self.assertTrue(recorded.json["ok"])
        self.assertIn("counts", recorded.json)
        self.assertTokenAbsent(recorded)

    def test_wrong_token_is_401(self):
        recorded = self.client.get("/api/health", token="wrong-token-000000000000000000000000")
        self.assertStatus(401, recorded)
        self.assertEqual("UNAUTHORIZED", recorded.json["error"]["code"])
        # 错误响应里也不能回显提交上来的 token
        self.assertNotIn("wrong-token", recorded.response_body)

    def test_empty_and_malformed_authorization_headers(self):
        cases = [
            {"Authorization": "Bearer "},
            {"Authorization": "Bearer"},
            {"Authorization": "Basic dXNlcjpwYXNz"},
            {"Authorization": TEST_TOKEN},  # 少了 scheme
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                recorded = self.client.get("/api/health", token=None, headers=headers)
                self.assertStatus(401, recorded)
                self.assertEqual("UNAUTHORIZED", recorded.json["error"]["code"])

    def test_token_in_url_is_ignored(self):
        """token 不允许出现在 URL 里：带了也当没带（否则会进日志/历史/Referer）。"""
        recorded = self.client.get("/api/health?token=%s" % TEST_TOKEN, token=None)
        self.assertStatus(401, recorded)


class NoInformationLeakTests(ConsoleTestCase):
    """未鉴权时不能靠响应差异区分"对象存在"与"对象不存在"。"""

    def setUp(self) -> None:
        super().setUp()
        _document, _report, self.case = self.process_fixture("01-notice-multi-time.eml")
        self.assertTrue(self.case is not None, "夹具应产生 Case")

    def test_existing_and_missing_case_are_indistinguishable_when_unauthenticated(self):
        existing = self.client.get("/api/cases/%s" % self.case.id, token=None)
        missing = self.client.get("/api/cases/case_does_not_exist_at_all", token=None)
        self.assertStatus(401, existing)
        self.assertStatus(401, missing)
        self.assertEqual(existing.response_body, missing.response_body)
        self.assertEqual(
            {key: value for key, value in existing.response_headers.items() if key != "Date"},
            {key: value for key, value in missing.response_headers.items() if key != "Date"},
        )

    def test_wrong_token_cannot_confirm_case_existence(self):
        existing = self.client.get("/api/cases/%s" % self.case.id, token="wrong-0000000000000000000000000000")
        missing = self.client.get("/api/cases/case_does_not_exist_at_all", token="wrong-0000000000000000000000000000")
        self.assertEqual(existing.response_body, missing.response_body)
        self.assertNotIn(self.case.id, existing.response_body)

    def test_unauthenticated_overview_has_no_counts(self):
        recorded = self.client.get("/api/overview", token=None)
        self.assertStatus(401, recorded)
        for token_text in ("case", "question", "approval", "pending", "count"):
            self.assertNotIn(token_text, recorded.response_body)

    def test_static_shell_requires_token_too(self):
        """静态页也要鉴权：否则任何人能看出这台设备在跑什么控制台。"""
        for path in ("/", "/index.html", "/app.js", "/styles.css", "/manifest.webmanifest", "/icon.svg"):
            with self.subTest(path=path):
                self.assertStatus(401, self.client.get(path, token=None))

    def test_unauthenticated_post_is_rejected_before_touching_store(self):
        before = dict(self.store.stats())
        recorded = self.client.post("/api/approvals", {"draft_id": "draft_x", "version_no": 1, "confirm": True}, token=None)
        self.assertStatus(401, recorded)
        self.assertEqual(before, dict(self.store.stats()))

    def test_wrong_token_gets_401_before_404(self):
        """错 token 访问不存在的资源：必须是 401 而不是 404（404 会泄露存在性）。"""
        self.assertStatus(401, self.client.get("/api/cases/case_nope", token="bad-0000000000000000000000000000000"))
        self.assertStatus(200, self.client.get("/api/health"))

    def test_401_body_shape_is_stable(self):
        for path in ("/api/health", "/api/cases", "/api/artifacts/xyz", "/api/approvals"):
            with self.subTest(path=path):
                recorded = self.client.get(path, token=None)
                self.assertEqual(
                    {"error": {"code": "UNAUTHORIZED", "message": "需要有效的控制台令牌"}},
                    json.loads(recorded.response_body),
                )


class TokenFileTests(ConsoleTestCase):
    """token 文件：位置在仓库外、权限必须 600/700，否则拒绝启动。"""

    def test_load_token_reads_file(self):
        self.assertEqual(TEST_TOKEN, load_token(self.token_path))

    def test_relative_and_missing_path(self):
        with self.assertRaises(AuthError):
            load_token("")
        with self.assertRaises(AuthError):
            load_token(os.path.join(self.tmpdir, "nope", "console-token"))

    def test_short_token_is_rejected(self):
        directory = os.path.join(self.tmpdir, "short")
        os.makedirs(directory, exist_ok=True)
        path = make_token_file(directory, "abc")
        with self.assertRaises(AuthError) as ctx:
            load_token(path)
        self.assertIn(str(MIN_TOKEN_LENGTH), str(ctx.exception))

    def test_empty_token_file_is_rejected(self):
        directory = os.path.join(self.tmpdir, "empty")
        os.makedirs(directory, exist_ok=True)
        path = make_token_file(directory, TEST_TOKEN)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n")
        with self.assertRaises(AuthError):
            load_token(path)

    @unittest.skipUnless(posix_permissions_supported(), "POSIX 权限位在 Windows 上不可用")
    def test_world_readable_token_file_is_rejected(self):
        directory = os.path.join(self.tmpdir, "loose")
        os.makedirs(directory, exist_ok=True)
        path = make_token_file(directory, TEST_TOKEN)
        os.chmod(path, 0o644)
        with self.assertRaises(AuthError) as ctx:
            load_token(path)
        self.assertIn("600", str(ctx.exception))

    @unittest.skipUnless(posix_permissions_supported(), "POSIX 权限位在 Windows 上不可用")
    def test_group_readable_token_file_is_rejected(self):
        directory = os.path.join(self.tmpdir, "grouped")
        os.makedirs(directory, exist_ok=True)
        path = make_token_file(directory, TEST_TOKEN)
        os.chmod(path, 0o640)
        with self.assertRaises(AuthError):
            load_token(path)

    @unittest.skipUnless(posix_permissions_supported(), "POSIX 权限位在 Windows 上不可用")
    def test_loose_directory_is_rejected(self):
        directory = os.path.join(self.tmpdir, "loosedir")
        os.makedirs(directory, exist_ok=True)
        path = make_token_file(directory, TEST_TOKEN)
        os.chmod(os.path.dirname(path), 0o755)
        with self.assertRaises(AuthError) as ctx:
            load_token(path)
        self.assertIn("700", str(ctx.exception))

    @unittest.skipUnless(posix_permissions_supported(), "POSIX 符号链接在 Windows 上受限")
    def test_symlink_token_is_rejected(self):
        directory = os.path.join(self.tmpdir, "link")
        os.makedirs(directory, exist_ok=True)
        target = make_token_file(directory, TEST_TOKEN)
        link = os.path.join(directory, "link-token")
        os.symlink(target, link)
        with self.assertRaises(AuthError) as ctx:
            load_token(link)
        self.assertIn("符号链接", str(ctx.exception))

    def test_token_never_appears_in_calls(self):
        """`TokenAuth` 只提供 matches/redact，没有"把 token 打出来"的接口。"""
        auth = TokenAuth(TEST_TOKEN)
        self.assertTrue(auth.matches(TEST_TOKEN))
        self.assertFalse(auth.matches(TEST_TOKEN[:-1]))
        self.assertFalse(auth.matches(""))
        self.assertFalse(auth.matches(None))
        self.assertEqual("<redacted>", auth.redact(TEST_TOKEN))
        self.assertEqual("token=<redacted>", auth.redact("token=%s" % TEST_TOKEN))
        self.assertNotIn(TEST_TOKEN, repr(auth))
        self.assertNotIn(TEST_TOKEN, str(auth.fingerprint))

    def test_token_never_in_default_paths(self):
        """默认路径必须在仓库外（`$ASISTENT_VAR` 或 `~/asistent-var`）。"""
        from asistent.console.settings import default_token_path

        path = default_token_path()
        self.assertNotIn(os.path.join("asistent", "console"), path)
        self.assertTrue(os.path.isabs(path))

    def test_default_port_is_18090(self):
        self.assertEqual(18090, DEFAULT_PORT)

    def test_service_refuses_to_start_without_token(self):
        with self.assertRaises(AuthError):
            TokenAuth("")
        settings = ConsoleSettings(store=self.store, token="", bind="127.0.0.1", port=0)
        with self.assertRaises(AuthError):
            build_server(settings)


__all__ = [
    "AuthHeaderTests",
    "NoInformationLeakTests",
    "TokenFileTests",
]
