"""`allow_remote` 必须真的管用（它曾经是个假开关）。

背景：`ConsoleSettings.allow_remote` 与 `console.allow_remote` 被解析、被 `describe()`
写进配置快照，但**全仓库没有任何地方执行它**。于是"bind=0.0.0.0 + allow_remote=false"
看起来像"我限制了远程访问"，实际是把令牌与全部待办明文暴露到整个网络（控制台没有 TLS）。

这组测试锁住三件事：
1. 非回环 bind + allow_remote=false -> **拒绝启动**（响亮失败，而不是安静地暴露）；
2. 非回环 bind + allow_remote=true -> 允许（用户显式承担风险）；
3. 回环 bind -> 永远允许（默认路径不受影响）。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from asistent.console import ConsoleSettings, build_server
from asistent.console.settings import is_loopback_bind

from .helpers import TEST_TOKEN, make_token_file


class LoopbackDetectTests(unittest.TestCase):
    def test_loopback_forms(self):
        for value in ("127.0.0.1", "127.0.0.5", "::1", "[::1]", "localhost", "LocalHost", " 127.0.0.1 "):
            with self.subTest(bind=value):
                self.assertTrue(is_loopback_bind(value), "%r 应视为回环" % value)

    def test_non_loopback_forms(self):
        for value in ("", "0.0.0.0", "::", "[::]", "192.0.2.10", "192.168.1.5", "example.com"):
            with self.subTest(bind=value):
                self.assertFalse(is_loopback_bind(value), "%r 不应视为回环" % value)


class BindGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="console-bind-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmpdir, ignore_errors=True))
        self.token_path = make_token_file(self.tmpdir, TEST_TOKEN)
        self.servers: list = []

    def _settings(self, bind: str, allow_remote: bool) -> ConsoleSettings:
        return ConsoleSettings(
            store=object(),
            token=TEST_TOKEN,
            bind=bind,
            port=0,
            token_path=self.token_path,
            allow_remote=allow_remote,
        )

    def _start(self, settings: ConsoleSettings):
        server = build_server(settings, log_sink=lambda line: None)
        self.servers.append(server)
        self.addCleanup(server.server_close)
        return server

    def test_remote_bind_without_optin_is_refused(self):
        for bind in ("0.0.0.0", "::", "192.0.2.10", ""):
            with self.subTest(bind=bind):
                with self.assertRaises(ValueError) as ctx:
                    self._start(self._settings(bind, allow_remote=False))
                message = str(ctx.exception)
                self.assertIn("allow_remote", message, "错误信息必须告诉人怎么修")
                self.assertIn("console.bind", message)

    def test_remote_bind_with_optin_is_allowed(self):
        server = self._start(self._settings("0.0.0.0", allow_remote=True))
        self.assertGreater(server.bound_port, 0)
        self.assertEqual("0.0.0.0", server.bound_host)

    def test_loopback_bind_needs_no_optin(self):
        server = self._start(self._settings("127.0.0.1", allow_remote=False))
        self.assertGreater(server.bound_port, 0)
        self.assertEqual("127.0.0.1", server.bound_host)

    def test_guard_happens_before_binding(self):
        """拒绝必须发生在 socket 绑定之前：不能先占了端口再抛异常（会留下半初始化状态）。"""
        settings = self._settings("0.0.0.0", allow_remote=False)
        with self.assertRaises(ValueError):
            build_server(settings, log_sink=lambda line: None)


class AllowRemoteInRuntimeTests(unittest.TestCase):
    """服务层：配置写错时控制台必须"响亮地缺席"，并在 notes 里留痕。"""

    def setUp(self) -> None:
        from tests.service.support import ServiceWorkbench

        self._tmp = tempfile.TemporaryDirectory(prefix="console-bind-svc-", ignore_cleanup_errors=True)
        self.bench = ServiceWorkbench(self._tmp.name)

    def tearDown(self) -> None:
        self.bench.close_runtimes()
        self._tmp.cleanup()

    def test_runtime_degrades_visibly_when_remote_bind_not_opted_in(self):
        config_path = self.bench.write_config(
            "bind-bad.json", console={"bind": "0.0.0.0", "allow_remote": False}
        )
        runtime = self.bench.runtime(config_path, start_console=True)
        handle = runtime.start_console()
        self.assertEqual("", handle.url, "拒绝监听时不能报出一个假 URL")
        self.assertIn("allow_remote", handle.error)
        self.assertTrue(
            any("控制台启动失败" in note for note in runtime.state.notes),
            "失败必须写进 notes，否则运维看不出控制台为什么不见了：%r" % (runtime.state.notes,),
        )
        # 服务本体不受影响：控制台缺席不该拖垮 24h 助手
        self.assertIsNone(handle.server)

    def test_runtime_starts_console_when_opt_in(self):
        config_path = self.bench.write_config(
            "bind-ok.json", console={"bind": "0.0.0.0", "allow_remote": True}
        )
        runtime = self.bench.runtime(config_path, start_console=True)
        handle = runtime.start_console()
        self.assertTrue(handle.url.startswith("http://"), handle.error)
        handle.stop()


if __name__ == "__main__":
    unittest.main()
