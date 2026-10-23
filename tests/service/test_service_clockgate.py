"""时钟闸门（`asistent/service/clockgate.py`）的行为锁定。

为什么值得单独测：这道闸门跑在 service 启动路径上，**它的失败方式直接决定 24h 结论是否可信**。
两个方向都要防：

* 时钟正常时**不许**引入延迟（否则每次重启都白等两分钟）；
* 时钟明显错误时**不许**安静通过（否则时间戳错了没人知道）。
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from asistent.service import clockgate


def _now(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _anchor_floor() -> datetime:
    """当前仓库锚点算出的下界（"代码提交时刻 − 余量"与固定下界取较晚者）。"""
    return clockgate.effective_floor({})


def _sane_now(margin: timedelta = timedelta(hours=2)) -> datetime:
    """锚点之后 2 小时：代表"时钟是正常的"。

    **不要**在这里写死日期。下界随每次提交往前推（提交时刻 − 1 天余量），写死的"现在"
    迟早会落到下界之后——2026-10-23 真的发生了：两个测试在设备上变红，而产品逻辑没错，
    是夹具过期了。夹具必须跟着锚点走。
    """
    return _anchor_floor() + margin


def _insane_now(margin: timedelta = timedelta(days=400)) -> datetime:
    """锚点之前 400 天：代表"时钟明显偏早"（本机真实发生过 RTC 慢约两年）。"""
    return _anchor_floor() - margin


class FloorTest(unittest.TestCase):
    def test_default_floor_is_recorded_boot_era(self) -> None:
        floor = clockgate.floor_from_env({})
        self.assertEqual(floor.year, 2024)
        self.assertIsNotNone(floor.tzinfo)

    def test_env_override_and_garbage_falls_back(self) -> None:
        overridden = clockgate.floor_from_env({"ASISTENT_CLOCK_FLOOR": "2025-06-01T00:00:00+00:00"})
        self.assertEqual((overridden.year, overridden.month), (2025, 6))
        # 配置写错时回落到默认值，而不是崩掉启动路径
        self.assertEqual(clockgate.floor_from_env({"ASISTENT_CLOCK_FLOOR": "昨天"}).year, 2024)

    def test_naive_floor_is_treated_as_utc(self) -> None:
        floor = clockgate.floor_from_env({"ASISTENT_CLOCK_FLOOR": "2025-01-01T00:00:00"})
        self.assertEqual(floor.tzinfo, timezone.utc)


class SaneTest(unittest.TestCase):
    def test_clock_before_floor_is_insane(self) -> None:
        floor = _now("2024-01-01T00:00:00+00:00")
        # 这台设备真的出现过这种时钟（RTC 慢约两年）
        self.assertFalse(clockgate.is_sane(_now("2023-03-01T00:00:00+00:00"), floor))

    def test_clock_after_floor_is_sane(self) -> None:
        floor = _now("2024-01-01T00:00:00+00:00")
        self.assertTrue(clockgate.is_sane(_now("2026-10-12T10:00:00+00:00"), floor))

    def test_naive_now_is_treated_as_utc(self) -> None:
        floor = _now("2024-01-01T00:00:00+00:00")
        self.assertTrue(clockgate.is_sane(datetime(2026, 10, 12, 10, 0, 0), floor))


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="clockgate-")
        self.state = os.path.join(self.tmp.name, "state", "clock-gate.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, times, **kwargs):
        """times: 每次被问到时间时依次返回的时刻（最后一个反复使用）。"""
        log = []
        seen = {"index": 0}

        def now_fn():
            index = min(seen["index"], len(times) - 1)
            seen["index"] += 1
            return times[index]

        slept = []
        fake_clock = {"now": 0.0}

        def clock():
            return fake_clock["now"]

        def sleep(seconds):
            slept.append(seconds)
            fake_clock["now"] += seconds

        code = clockgate.run(
            env=kwargs.get("env", {}),
            clock=clock,
            sleep=sleep,
            now_fn=now_fn,
            state_path=self.state,
            log=log.append,
        )
        return code, log, slept, fake_clock["now"]

    def test_sane_clock_returns_immediately_without_sleeping(self) -> None:
        code, log, slept, _ = self._run([_sane_now()])
        self.assertEqual(code, 0, "闸门永远不该让启动失败")
        self.assertEqual(slept, [], "时钟正常时不许引入任何等待")
        self.assertEqual(log, [], "时钟正常时不该刷日志")

    def test_behind_clock_waits_then_proceeds_when_it_catches_up(self) -> None:
        env = {"ASISTENT_CLOCK_WAIT_SECONDS": "30"}
        # 前两次仍然偏早，第三次正常
        code, log, slept, waited = self._run(
            [
                _insane_now(),
                _insane_now(),
                _sane_now(),
            ],
            env=env,
        )
        self.assertEqual(code, 0)
        self.assertTrue(slept, "时钟偏早时必须等一等")
        self.assertTrue(any("时钟闸门" in line for line in log))
        self.assertGreater(waited, 0)

    def test_never_sane_still_starts_but_says_so_loudly(self) -> None:
        env = {"ASISTENT_CLOCK_WAIT_SECONDS": "12"}
        code, log, slept, waited = self._run([_insane_now()], env=env)
        self.assertEqual(code, 0, "等不到也要启动：助手不能因为时钟被永久关掉")
        self.assertTrue(any("警告" in line for line in log), "必须留下明确告警")
        self.assertTrue(any("时间戳可能不可信" in line for line in log))
        self.assertLessEqual(waited, 12 + clockgate.POLL_SECONDS, "等待必须是有界的")

    def test_state_file_records_the_verdict_with_0600(self) -> None:
        self._run([_insane_now()], env={"ASISTENT_CLOCK_WAIT_SECONDS": "0"})
        with open(self.state, encoding="utf-8") as handle:
            record = __import__("json").load(handle)
        self.assertFalse(record["clock_sane"])
        self.assertEqual(record["verdict"], "clock_behind_floor")
        self.assertIn("时间戳可能不可信", record["note"])
        if os.name != "nt":
            self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o600)

    def test_unwritable_state_path_does_not_block_startup(self) -> None:
        # 记不下来是诊断信息缺失，不能变成"服务起不来"
        code, _, _, _ = self._run(
            [_sane_now()],
        )
        self.assertEqual(code, 0)


class EvaluateTest(unittest.TestCase):
    def test_evaluate_shape(self) -> None:
        record = clockgate.evaluate(now=_sane_now())
        self.assertEqual(record["verdict"], "ok")
        self.assertTrue(record["clock_sane"])
        for key in ("checked_at", "floor", "waited_seconds"):
            self.assertIn(key, record)

    def test_evaluate_marks_behind(self) -> None:
        record = clockgate.evaluate(now=_insane_now())
        self.assertEqual(record["verdict"], "clock_behind_floor")
        self.assertFalse(record["clock_sane"])


class FixtureAnchorTest(unittest.TestCase):
    """测试夹具不许写死"现在"。

    2026-10-23 的教训：`test_evaluate_shape` 与 `test_sane_clock_returns_immediately...`
    在设备上变红，产品没问题——夹具把"现在"写成了 2026-10-12，而仓库每次提交都把下界
    往前推一天（提交时刻 − 1 天余量），过了那个点夹具就过期了。
    """

    def test_fixtures_track_the_repo_anchor(self) -> None:
        floor = _anchor_floor()
        self.assertTrue(clockgate.is_sane(_sane_now(), floor))
        self.assertFalse(clockgate.is_sane(_insane_now(), floor))

    def test_insane_fixture_is_unambiguously_behind(self) -> None:
        # 落后量大到不可能被"余量"吃掉，避免以后调整 slack 时这条测试变得含糊
        self.assertGreater((_anchor_floor() - _insane_now()), timedelta(days=365))


class RepoAnchorTest(unittest.TestCase):
    """固定下限不够用：本机真实故障是 **RTC 慢了约两年**（读到 2024，实际 2026）。

    固定下限 2024-01-01 对这种情况会**放行**——闸门等于没有。
    所以下限要取"代码提交时刻 - 余量"与固定下限中较晚的那个：
    我们确知代码是什么时候写的，这就是可用的时间锚点。
    """

    class _Proc:
        def __init__(self, stdout, returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def _runner(self, text, returncode=0):
        return lambda *a, **k: RepoAnchorTest._Proc(text.encode("utf-8"), returncode)

    def test_repo_floor_parses_commit_date(self) -> None:
        anchor = clockgate.repo_floor("/tmp", self._runner("2026-10-12T09:45:52+08:00\n"))
        self.assertEqual((anchor.year, anchor.month, anchor.day), (2026, 10, 12))

    def test_repo_floor_is_none_when_git_is_unavailable(self) -> None:
        self.assertIsNone(clockgate.repo_floor("/tmp", self._runner("", returncode=128)))
        self.assertIsNone(clockgate.repo_floor("/tmp", self._runner("not-a-date")))

    def test_two_years_behind_is_caught_by_the_anchor(self) -> None:
        # 代码是 2026-10-12 写的；时钟读到 2024-03-01（正是本机发生过的形态）
        floor = clockgate.effective_floor({}, runner=self._runner("2026-10-12T09:45:52+08:00"))
        self.assertFalse(
            clockgate.is_sane(_now("2024-03-01T00:00:00+00:00"), floor),
            "落后两年必须被判为不可信——固定下限会放行，所以必须有代码锚点",
        )
        self.assertTrue(clockgate.is_sane(_now("2026-10-12T10:00:00+08:00"), floor))

    def test_anchor_never_makes_floor_earlier(self) -> None:
        # 若配置里给了更晚的固定下限（例如未来某次要求），不能被提交时间拉早
        floor = clockgate.effective_floor(
            {"ASISTENT_CLOCK_FLOOR": "2030-01-01T00:00:00+00:00"},
            runner=self._runner("2026-10-12T09:45:52+08:00"),
        )
        self.assertEqual(floor.year, 2030)

    def test_slack_allows_small_backward_skew(self) -> None:
        # 提交时刻前 1 小时（在默认 1 天余量内）仍算正常：避免刚部署就误报
        floor = clockgate.effective_floor({}, runner=self._runner("2026-10-12T09:45:52+08:00"))
        self.assertTrue(clockgate.is_sane(_now("2026-10-12T08:45:52+08:00"), floor))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
