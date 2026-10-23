"""控制台"晚到"也要能自己恢复。

真实故障（2026-10-12，板子上发生了 11 天）：服务先于 DHCP 地址就绪启动，控制台
`bind()` 报 `OSError: [Errno 99] Cannot assign requested address`；旧实现只在循环开始前
试一次，失败后再没有第二次尝试——服务本体照常跑了 246 轮，手机端一直连不上，而状态里
只有一条陈旧的 note。

这里不靠"抢端口"来复现（Windows 与 POSIX 的 `SO_REUSEADDR` 语义不同，抢端口不可靠），
而是把 `build_server` 换成"前 N 次抛同样的错"，直接测恢复语义：
  1. 第一次失败 → 有 note、无 URL、服务不受影响；
  2. 下一次重试成功 → URL 有了、留一条"已自动恢复"，且失败 note 不会重复堆积；
  3. 重试有节流，不会每轮狂试。
"""

from __future__ import annotations

import tempfile
import unittest

from asistent.service import runtime as runtime_module
from asistent.service.runtime import CONSOLE_RETRY_SECONDS

from tests.service.support import ServiceWorkbench


class _FlakyBuildServer:
    """把 `build_server` 包一层：前 ``fail_times`` 次按真实故障那样抛 OSError。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self._real = runtime_module.build_server

    def __call__(self, settings, service, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise OSError(99, "Cannot assign requested address")
        # 透传 kwargs（真实签名带 log_sink）：假实现必须跟真签名一致，
        # 否则"接线漏了"这类缺陷会被假实现自己吃掉
        return self._real(settings, service, **kwargs)

    def install(self, testcase: unittest.TestCase) -> "_FlakyBuildServer":
        runtime_module.build_server = self
        testcase.addCleanup(setattr, runtime_module, "build_server", self._real)
        return self


def _console_failure_notes(runtime) -> list[str]:
    return [note for note in runtime.state.notes if note.startswith("控制台启动失败：")]


class ConsoleLateStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="console-late-", ignore_cleanup_errors=True)
        self.root = self._tmp.name
        self.bench = ServiceWorkbench(self.root)

    def tearDown(self) -> None:
        self.bench.close_runtimes()
        self._tmp.cleanup()

    def test_first_bind_failure_is_retried_until_it_works(self) -> None:
        builder = _FlakyBuildServer(fail_times=1).install(self)
        config_path = self.bench.write_config("service-late.json")
        runtime = self.bench.runtime(config_path, start_console=True)

        handle = runtime.start_console()
        self.assertEqual("", handle.url, "第一次绑定失败时不该报出一个假 URL")
        self.assertIn("OSError", handle.error)
        self.assertEqual(1, len(_console_failure_notes(runtime)), "失败要留痕，且只留一条")

        # 下一轮开头重试：这次成功了
        self.assertTrue(runtime.maybe_retry_console(now=CONSOLE_RETRY_SECONDS + 1))
        self.assertTrue(runtime.console.url.startswith("http://127.0.0.1:"), runtime.console.url)
        self.assertTrue(
            any(note.startswith("控制台延迟启动成功：") for note in runtime.state.notes),
            "恢复这件事必须写进 notes，否则运维看不出它曾经失败过",
        )
        self.assertIn("OSError", " ".join(runtime.state.notes[-1:]), "恢复记录里要带上次的失败原因")
        self.assertEqual(2, builder.calls)
        runtime.console.stop()

    def test_retry_is_throttled_and_failure_notes_do_not_pile_up(self) -> None:
        builder = _FlakyBuildServer(fail_times=99).install(self)
        runtime = self.bench.runtime(self.bench.write_config("service-late2.json"), start_console=True)

        runtime.start_console()
        self.assertFalse(runtime.maybe_retry_console(now=0.0), "第一次重试本身也是失败")
        self.assertEqual(2, builder.calls)
        # 节流窗口内不再尝试
        self.assertFalse(runtime.maybe_retry_console(now=CONSOLE_RETRY_SECONDS - 1))
        self.assertEqual(2, builder.calls, "节流窗口内不该再调用 build_server")
        # 过了窗口才再试一次，仍然是失败——但 note 不能变成两条
        self.assertFalse(runtime.maybe_retry_console(now=CONSOLE_RETRY_SECONDS + 1))
        self.assertEqual(3, builder.calls)
        self.assertEqual(1, len(_console_failure_notes(runtime)), "同一条失败只该有一条 note")
        self.assertFalse(any(note.startswith("控制台延迟启动成功") for note in runtime.state.notes))

    def test_successful_console_is_never_rebuilt(self) -> None:
        config_path = self.bench.write_config("service-late3.json", console={"port": 0})
        runtime = self.bench.runtime(config_path, start_console=True)
        self.assertTrue(runtime.start_console().url)
        self.assertFalse(runtime.maybe_retry_console(now=10 * CONSOLE_RETRY_SECONDS), "已经起来的控制台不该被重建")
        runtime.console.stop()

    def test_disabled_console_does_not_retry(self) -> None:
        config_path = self.bench.write_config(
            "service-late4.json", console={"enabled": False, "port": 0, "token_path": self.bench.token_path}
        )
        runtime = self.bench.runtime(config_path, start_console=True)
        self.assertFalse(runtime.maybe_retry_console(now=10 * CONSOLE_RETRY_SECONDS))
        self.assertEqual("", runtime.console.url)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
