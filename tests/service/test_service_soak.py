"""浸泡取样器的用例（`asistent/service/soak.py`）。

这些用例锁的**不是**"能读 /proc"，而是三件容易悄悄变坏的事：

1. **取样必须只读**：它跑在正在服务用户的机器上，任何写入（除了自己那一行日志）
   都是不可接受的副作用；
2. **没有数据不许编**：`--summary` 在无数据时必须说"没有数据"，而不是打印一排 0
   ——一排 0 看起来像"内存没涨"，实际上是什么都没测；
3. **接口口径固定**：`sample()` 返回的字段名是 24h 之后给人看结论的依据，
   改字段名等于让旧数据静默失效。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from asistent.service import soak


class SampleShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="soak-")
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "state"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "data"), exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sample_has_the_fields_the_soak_verdict_needs(self) -> None:
        row = soak.sample(self.root)
        for key in (
            "at",
            "epoch",
            "service_active",
            "service_pids",
            "service_pid_count",
            "rss_kb",
            "fds",
            "threads",
            "rounds_completed",
            "rounds_failed",
            "wal_bytes",
            "db_bytes",
            "log_bytes",
            "disk_free_bytes",
            "listening_console",
        ):
            self.assertIn(key, row, key)
        # 时间戳必须能被解析回来（跨时区/本地化写着写着就会坏）
        self.assertRegex(str(row["at"]), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertIsInstance(row["epoch"], int)

    def test_sample_reads_rounds_from_status_snapshot(self) -> None:
        with open(os.path.join(self.root, "state", "service-status.json"), "w", encoding="utf-8") as handle:
            json.dump({"rounds_completed": 7, "rounds_failed": 1, "waiting_on": ["pan"]}, handle)
        row = soak.sample(self.root)
        self.assertEqual(row["rounds_completed"], 7)
        self.assertEqual(row["rounds_failed"], 1)
        self.assertEqual(row["waiting_on"], ["pan"])

    def test_broken_status_snapshot_does_not_break_sampling(self) -> None:
        # 半个写入的 JSON 不该让取样失败：那样浸泡会在最需要数据的时候断掉
        with open(os.path.join(self.root, "state", "service-status.json"), "w", encoding="utf-8") as handle:
            handle.write('{"rounds_completed": 3')
        row = soak.sample(self.root)
        self.assertIsNone(row["rounds_completed"])
        self.assertEqual(row["service_pid_count"], len(row["service_pids"]))

    def test_missing_files_become_none_not_zero(self) -> None:
        row = soak.sample(self.root)
        # 文件不存在 ⇒ None（"测不到"），而不是 0（"测到是 0 字节"）——两者含义不同
        self.assertIsNone(row["log_bytes"])
        self.assertEqual(row["db_files"], [])

    def test_storage_files_are_discovered_not_hardcoded(self) -> None:
        """回归：数据库文件名**不能写死**。

        真机上的库叫 `data/asistent.db`（另有 `index/fts.db`），而取样器原来写死
        `data/asistent.sqlite3` → 两项永远是 null。后果不是崩溃，而是**静默少一块结论**：
        "WAL 有没有涨"整整一段浸泡期没人回答，报告上只显示"无数据"。
        """
        os.makedirs(os.path.join(self.root, "index"), exist_ok=True)
        for name, size in (
            ("data/asistent.db", 1000),
            ("data/asistent.db-wal", 300),
            ("data/asistent.db-shm", 32),
            ("index/fts.db", 500),
            ("index/fts.db-wal", 200),
        ):
            with open(os.path.join(self.root, name), "wb") as handle:
                handle.write(b"x" * size)
        row = soak.sample(self.root)
        self.assertEqual(row["db_bytes"], 1500, "主库合计（不含 -wal/-shm）")
        self.assertEqual(row["wal_bytes"], 500, "WAL 合计")
        self.assertIn("data/asistent.db", row["db_files"])
        self.assertIn("index/fts.db-wal", row["db_files"])
        self.assertNotIn("data/asistent.db-shm", row["db_files"], "-shm 不参与泄漏判断")

    def test_legacy_sqlite3_name_still_counted(self) -> None:
        with open(os.path.join(self.root, "data", "asistent.sqlite3"), "wb") as handle:
            handle.write(b"x" * 42)
        row = soak.sample(self.root)
        self.assertEqual(row["db_bytes"], 42)

    def test_sampling_does_not_write_into_the_service_data_dir(self) -> None:
        before = set(os.listdir(os.path.join(self.root, "data")))
        soak.sample(self.root)
        self.assertEqual(before, set(os.listdir(os.path.join(self.root, "data"))))


class AppendAndSummarizeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="soak-")
        self.root = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_summary_without_data_says_so(self) -> None:
        text = soak.summarize(self.root)
        self.assertIn("还没有取样数据", text)
        # 关键：**不许**用一排 0 假装测过
        self.assertNotIn("RSS", text)

    def test_append_writes_one_json_line_with_0600(self) -> None:
        path = soak.append_sample(self.root)
        with open(path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        self.assertEqual(len(lines), 1)
        json.loads(lines[0])  # 必须是合法单行 JSON
        if os.name != "nt":  # Windows 上不做 POSIX 位断言（与其它用例口径一致）
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_append_is_append_only(self) -> None:
        soak.append_sample(self.root)
        soak.append_sample(self.root)
        with open(soak.soak_log_path(self.root), "r", encoding="utf-8") as handle:
            self.assertEqual(len([line for line in handle if line.strip()]), 2)

    def test_summary_reports_trend_and_rounds(self) -> None:
        rows = [
            {"at": "2026-10-12T08:00:00+0800", "epoch": 1000, "service_active": "active",
             "service_pid_count": 1, "rss_kb": 1000, "fds": 10, "threads": 3,
             "wal_bytes": 100, "db_bytes": 200, "log_bytes": 300, "disk_free_bytes": 10**9,
             "rounds_completed": 5, "rounds_failed": 0, "listening_console": 1},
            {"at": "2026-10-13T08:00:00+0800", "epoch": 87400, "service_active": "active",
             "service_pid_count": 1, "rss_kb": 1200, "fds": 12, "threads": 3,
             "wal_bytes": 150, "db_bytes": 260, "log_bytes": 900, "disk_free_bytes": 10**9 - 4096,
             "rounds_completed": 700, "rounds_failed": 2, "listening_console": 1},
        ]
        os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)
        with open(soak.soak_log_path(self.root), "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        text = soak.summarize(self.root)
        self.assertIn("24.0 小时", text)
        self.assertIn("1000 → 1200（+200）", text)
        self.assertIn("+695 轮", text)
        self.assertIn("共失败 2 次", text)

    def test_summary_flags_service_downtime_and_lost_console(self) -> None:
        rows = [
            {"at": "t1", "epoch": 1, "service_active": "inactive", "service_pid_count": 0},
            {"at": "t2", "epoch": 2, "service_active": "active", "service_pid_count": 1,
             "listening_console": 0},
        ]
        os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)
        with open(soak.soak_log_path(self.root), "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        text = soak.summarize(self.root)
        self.assertIn("服务不是 active", text)
        self.assertIn("听不到控制台端口", text)

    def test_half_written_line_does_not_invalidate_the_series(self) -> None:
        path = soak.append_sample(self.root)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"at": "2026-10-12T09:00:00+0800", "rss')  # 断电时的半行
        text = soak.summarize(self.root)
        self.assertIn("1 个点", text)


    def test_sample_records_run_identity_not_just_counters(self) -> None:
        """CP-20261010-37：取样必须记**运行身份**，否则"连续 24h"无法证明。

        `rounds_completed` 是跨进程累计值：中途重启它只会继续变大，**看起来完全正常**。
        所以每一行都要带 `run_id` / `service_started_at` / `round_index`，
        判据里才能写"整段 run_id 必须唯一"。
        """
        os.makedirs(os.path.join(self.root, "state"), exist_ok=True)
        with open(os.path.join(self.root, "state", "service-status.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "rounds_completed": 38,
                    "rounds_failed": 0,
                    "run_id": "exec_abc",
                    "started_at": "2026-10-12T00:57:00Z",
                    # 真实结构：round_index 嵌在 last_round 里（设备实测的键布局）
                    "last_round": {"round_index": 19, "notes": []},
                },
                handle,
            )
        row = soak.sample(self.root)
        self.assertEqual(row["run_id"], "exec_abc")
        self.assertEqual(row["service_started_at"], "2026-10-12T00:57:00Z")
        self.assertEqual(row["round_index"], 19)
        self.assertIn("repo_head", row)


class VerdictTest(unittest.TestCase):
    """判据必须**预先冻结**、由代码判定（不靠人看着曲线定标准）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="soak-")
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rows: list) -> None:
        with open(soak.soak_log_path(self.root), "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    @staticmethod
    def _row(epoch: int, **overrides) -> dict:
        row = {
            "at": "t%d" % epoch,
            "epoch": epoch,
            "service_active": "active",
            "run_id": "exec_same",
            "rss_kb": 28000,
            "fds": 10,
            "rounds_completed": 100,
            "rounds_failed": 0,
            "wal_bytes": 3_000_000,
            "log_bytes": 60_000,
            "disk_free_bytes": 100 * 1024**3,
            "listening_console": 1,
        }
        row.update(overrides)
        return row

    def test_legacy_rows_without_run_id_are_disclosed_not_ignored(self) -> None:
        """旧版取样器写的行没有 run_id：少数只备注，过半数才算证据不足。

        两个方向都要防：默默忽略 = 假装连续；一律判证据不足 = 明天一段干净的 24h
        会因为最前面几行没带 run_id 而被误判。
        这里把 legacy 行放在**末尾**：末尾没有 run_id 时不做窗口切分，整份都参与判定
        （这正是"取样器升级发生在这一段中间"时会出现的形态）。
        """
        rows = [self._row(1000), self._row(2000)]
        legacy = dict(self._row(2100))
        legacy.pop("run_id")
        rows.append(legacy)
        self._write(rows)
        text = soak.verdict(self.root)
        self.assertIn("✅ 通过", text)
        self.assertIn("没有 run_id", text, "少数旧行必须备注出来，不能不提")

        rows = [self._row(1000)]
        for index in range(3):
            row = dict(self._row(1100 + index))
            row.pop("run_id")
            rows.append(row)
        self._write(rows)
        text = soak.verdict(self.root)
        self.assertIn("证据不足", text)
        self.assertIn("没有 run_id", text)

    def test_flat_24h_passes(self) -> None:
        rows = [self._row(1000), self._row(1000 + 24 * 3600, rounds_completed=800)]
        self._write(rows)
        text = soak.verdict(self.root)
        self.assertIn("✅ 通过", text)

    def test_insufficient_points_is_not_a_pass(self) -> None:
        self._write([self._row(1000)])
        text = soak.verdict(self.root)
        self.assertIn("证据不足", text)
        self.assertNotIn("✅", text, "点太少不许判通过")

    def test_verdict_window_is_the_latest_contiguous_run(self) -> None:
        """判定窗口只覆盖"最近这段连续运行"，并把预跑段**写出来**。

        背景：已决定"先做 U-2/U-5，再跑最终 24h"，服务会因改配置重启一次、run_id 变化。
        若把整份日志一起判，预热段会让"run_id 唯一"判据失败——那不是最终那轮的问题。
        两个方向都要防：切窗口不能切掉对**最终那轮**的严格判定，也不能把预热段藏起来
        （"另有 N 个点属于上一个 run"必须可见）。
        """
        rows = [
            self._row(1000, run_id="exec_prelude", rss_kb=99000),  # 预热段：内存明显更高
            self._row(2000, run_id="exec_prelude", rss_kb=99000),
            self._row(3000, run_id="exec_final", rss_kb=28000),
            self._row(3000 + 24 * 3600, run_id="exec_final", rss_kb=28200),
        ]
        self._write(rows)
        text = soak.verdict(self.root)
        self.assertIn("判定窗口", text)
        self.assertIn("之前另有 2 个点不属于本段", text)
        self.assertIn("✅ 通过", text, "预热段内存高不该连累最终那轮")
        self.assertNotIn("99000", text.split("判定窗口")[1].split("\n")[0])

    def test_run_change_is_disclosed_and_single_point_window_is_not_a_pass(self) -> None:
        """run 变了必须**说出来**；而"刚重启只有一个点"绝不能判通过。

        改用判定窗口之后，"中途重启"不再以失败形式出现（重启是用户决定的正常操作），
        但它仍必须**可见**——否则预热段和最终段会被混成一句"连续 24h"。
        """
        rows = [
            self._row(1000, run_id="exec_before", rounds_completed=300),
            self._row(2000, run_id="exec_after"),
            self._row(2000 + 24 * 3600, run_id="exec_after"),
        ]
        self._write(rows)
        text = soak.verdict(self.root)
        self.assertIn("判定窗口", text)
        self.assertIn("不属于本段", text, "换了 run 必须写明，不能默默合并")
        self.assertIn("✅ 通过", text)

        # 刚重启、窗口里只有 1 个点 → 证据不足（不能拿"上一个 run 的数据"充数）
        self._write([self._row(1000, run_id="exec_new")])
        self.assertIn("证据不足", soak.verdict(self.root))

    def test_memory_growth_fails(self) -> None:
        rows = [self._row(1000, rss_kb=28000), self._row(2000, rss_kb=40000)]
        self._write(rows)
        self.assertIn("RSS 增长", soak.verdict(self.root))

    def test_fd_leak_fails(self) -> None:
        rows = [self._row(1000, fds=10), self._row(2000, fds=25)]
        self._write(rows)
        self.assertIn("句柄增长", soak.verdict(self.root))

    def test_round_failures_and_wal_and_console_are_checked(self) -> None:
        rows = [
            self._row(1000),
            self._row(2000, rounds_failed=9, wal_bytes=900 * 1024 * 1024, listening_console=0),
        ]
        self._write(rows)
        text = soak.verdict(self.root)
        self.assertIn("轮次失败增加", text)
        self.assertIn("WAL 峰值", text)
        self.assertIn("听不到控制台端口", text)

    def test_short_span_is_flagged_even_when_metrics_are_clean(self) -> None:
        # 曲线干净 ≠ 24h 跑够：跨度不足必须显式提醒，否则会被当成 AT-01 通过
        rows = [self._row(1000), self._row(1000 + 3600)]
        self._write(rows)
        text = soak.verdict(self.root)
        self.assertIn("✅ 通过", text)
        self.assertIn("不足 24 小时", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
