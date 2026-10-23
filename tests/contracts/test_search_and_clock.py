"""检索与时钟契约测试（SPEC §6.2、§11.2、§21.1；契约变更 CP-20261010-01/02）。

这两项都是设备实测暴露的**静默失效**风险，必须由契约测试锁住：
- 中文检索：FTS5 unicode61 把连续中文当一个 token，2 字查询命中 0 行且不报错；
- 时钟：开机初期墙钟落后 2 年 3 个月，会污染审计/审批有效期/邮件窗口。
"""

from __future__ import annotations

import sqlite3
import unittest

from asistent.contracts import (
    ClockAnomaly,
    ClockGuard,
    FTS_DDL,
    check_clock_sanity,
    fts5_available,
    is_valid_query,
    normalize_for_index,
    split_cjk,
    to_fts_query,
)


class ChineseRetrievalContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.fts5 = fts5_available(self.conn)
        if self.fts5:
            self.conn.execute(FTS_DDL)

    def tearDown(self) -> None:
        self.conn.close()

    def _index(self, doc_id: str, path: str, body: str) -> None:
        self.conn.execute(
            "INSERT INTO search_index(doc_id, path, body) VALUES (?,?,?)",
            (doc_id, path, normalize_for_index(body)),
        )

    def _query(self, text: str):
        expression = to_fts_query(text)
        self.assertIsNotNone(expression, f"查询规范化后不应为空: {text!r}")
        return self.conn.execute(
            "SELECT doc_id FROM search_index WHERE search_index MATCH ? ORDER BY doc_id", (expression,)
        ).fetchall()

    def test_cjk_is_split_per_character(self) -> None:
        self.assertEqual("部 署 方 案", split_cjk("部署方案"))
        self.assertEqual("deploy 部 署", split_cjk("deploy部署"))
        self.assertEqual("", normalize_for_index(""))
        self.assertEqual("部 署 deploy plan", normalize_for_index("部署 Deploy Plan"))

    def test_query_grouping_survives_whitespace(self) -> None:
        """`部署方案` 是一个短语（四字相邻），`部署 方案` 是两个短语（各自相邻即可）。"""
        self.assertEqual('"部 署 方 案"', to_fts_query("部署方案"))
        self.assertEqual('"部 署" AND "方 案"', to_fts_query("部署 方案"))
        self.assertEqual('"deploy" AND "部 署"', to_fts_query("deploy部署"))

    def test_two_character_chinese_query_hits(self) -> None:
        """最关键的一条：`部署` 这类 2 字中文查询必须命中（设备实测原方案命中 0 行）。"""
        if not self.fts5:
            self.skipTest("本机 SQLite 未编译 FTS5，交由 MVP-1 在设备上验证")
        self._index("doc_1", "资料/部署方案.md", "设备常驻与部署方案说明")
        self._index("doc_2", "资料/联系人.md", "张三的邮箱地址")
        self.assertEqual([("doc_1",)], self._query("部署"))
        self.assertEqual([("doc_1",)], self._query("部署方案"))
        self.assertEqual([], self._query("网盘"))

    def test_latin_search_is_case_insensitive_and_mixed_works(self) -> None:
        if not self.fts5:
            self.skipTest("本机 SQLite 未编译 FTS5")
        self._index("doc_1", "资料/notes.md", "Deploy 部署 checklist")
        self.assertEqual([("doc_1",)], self._query("deploy"))
        self.assertEqual([("doc_1",)], self._query("DEPLOY"))
        self.assertEqual([("doc_1",)], self._query("部署 deploy"))

    def test_phrase_adjacency_is_respected(self) -> None:
        """拆成单字后必须仍是短语查询，否则会退化成"包含这些字的任意文档"。"""
        if not self.fts5:
            self.skipTest("本机 SQLite 未编译 FTS5")
        self._index("doc_1", "a.md", "部署方案")
        self._index("doc_2", "b.md", "部件署名的方案")
        self.assertEqual([("doc_1",)], self._query("部署方案"))

    def test_untrusted_text_cannot_break_the_query(self) -> None:
        """邮件正文是不可信输入：FTS5 语法字符不得破坏查询或变成注入（§13.5）。"""
        if not self.fts5:
            self.skipTest("本机 SQLite 未编译 FTS5")
        self._index("doc_1", "a.md", "正常内容")
        for hostile in ['部署" OR "x', "MATCH *", "a AND b", '"""', "()^", "部署;" ]:
            expression = to_fts_query(hostile)
            if expression is None:
                continue
            # 不抛语法错误即为通过；且不得因为注入而返回全部文档
            rows = self.conn.execute(
                "SELECT doc_id FROM search_index WHERE search_index MATCH ?", (expression,)
            ).fetchall()
            self.assertNotIn(("doc_1",), rows, f"可疑查询 {hostile!r} 不应命中无关文档")

    def test_empty_or_symbol_only_query_is_invalid(self) -> None:
        for value in ["", "   ", "!!!", "（）", "\n\t"]:
            self.assertIsNone(to_fts_query(value), f"{value!r} 必须视为无效查询")
            self.assertFalse(is_valid_query(value))

    def test_multi_token_query_requires_all_terms(self) -> None:
        expression = to_fts_query("部署 方案")
        self.assertIn("AND", expression)


class ClockContractTest(unittest.TestCase):
    def test_reference_missing_is_allowed_but_recorded(self) -> None:
        check = check_clock_sanity(now_iso="2026-10-10T13:00:00.000Z")
        self.assertTrue(check.ok)
        self.assertEqual("NO_REFERENCE", check.reason)

    def test_backward_jump_is_refused(self) -> None:
        """设备实测场景：开机墙钟落后 2 年 3 个月，必须拒绝写业务时间戳。"""
        check = check_clock_sanity(
            now_iso="2024-07-08T11:59:00.000Z", reference_iso="2026-10-10T13:00:00.000Z"
        )
        self.assertFalse(check.ok)
        self.assertEqual("CLOCK_BEHIND", check.reason)
        with self.assertRaises(ClockAnomaly) as ctx:
            check.require_ok()
        self.assertEqual("CLOCK_ANOMALY", ctx.exception.code)

    def test_small_jitter_is_tolerated(self) -> None:
        check = check_clock_sanity(
            now_iso="2026-10-10T12:59:58.000Z", reference_iso="2026-10-10T13:00:00.000Z"
        )
        self.assertTrue(check.ok)
        self.assertFalse(check.requires_audit)

    def test_forward_jump_is_allowed_but_audited(self) -> None:
        """NTP 纠正大偏差后必须还能写时间，但要留审计。"""
        check = check_clock_sanity(
            now_iso="2026-10-10T13:00:00.000Z", reference_iso="2024-07-08T11:59:00.000Z"
        )
        self.assertTrue(check.ok)
        self.assertEqual("CLOCK_FORWARD_JUMP", check.reason)
        self.assertTrue(check.requires_audit)

    def test_invalid_timestamp_is_not_trusted(self) -> None:
        self.assertFalse(check_clock_sanity(now_iso="not-a-time").ok)
        self.assertFalse(
            check_clock_sanity(now_iso="2026-10-10T13:00:00.000Z", reference_iso="坏参照").ok
        )

    def test_guard_blocks_writes_until_clock_is_plausible(self) -> None:
        guard = ClockGuard()
        guard.observe("2026-10-10T13:00:00.000Z")
        guard.require_sane("2026-10-10T13:05:00.000Z")
        self.assertFalse(guard.degraded)

        # 回退：必须抛错且进入降级，参照时间不被污染
        with self.assertRaises(ClockAnomaly):
            guard.require_sane("2024-07-08T11:59:00.000Z")
        self.assertTrue(guard.degraded)
        self.assertEqual("2026-10-10T13:00:00.000Z", guard.reference_iso)

        # 时钟被校回来后恢复正常
        guard.require_sane("2026-10-10T13:06:00.000Z")
        self.assertFalse(guard.degraded)

    def test_monotonic_duration_is_independent_of_wall_clock(self) -> None:
        ticks = iter([100.0, 130.0])
        guard = ClockGuard(monotonic=lambda: next(ticks))
        self.assertEqual(0.0, guard.monotonic_now())
        self.assertEqual(30.0, guard.elapsed_seconds())

    def test_status_exposes_degradation_for_the_phone_console(self) -> None:
        guard = ClockGuard()
        guard.observe("2026-10-10T13:00:00.000Z")
        guard.observe("2026-10-10T13:00:10.000Z")
        status = guard.status()
        self.assertFalse(status["degraded"])
        self.assertEqual("2026-10-10T13:00:10.000Z", status["reference_iso"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
