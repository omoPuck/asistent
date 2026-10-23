"""定序必须与写入顺序一致（契约 v0.5，CP-20261010-11）。

真实缺陷的现场：`created_at` 只有毫秒精度，快速连写经常落在同一毫秒；此前用
`(created_at, id)` 排序，而 id 是随机的 → **平局判据随机**：`find(limit=1)` 会随机取到
不同对象、分页会漏项/重复、两个后端给出不同答案。参考实现因为 Python 慢、常跨毫秒而
"看起来没事"——典型"测试绿≠语义对"。

这里的每组用例都**显式**把时间戳设成同一个值（不靠"碰巧同毫秒"），并覆盖：

- 同一时间戳批量写入 → 顺序 = 写入顺序；
- 顺序与 id 无关（故意让 id 的字典序与写入顺序相反）；
- 多次调用稳定；
- 重开库（新进程/新实例）后顺序不变，序号本身也持久化；
- 更新不改变对象已有的定序位置；
- `list_events` 同一时间戳按 FIFO 取最早 n 条；
- 定了序之后，SQL 的 LIMIT 不能先截断再被 Python 过滤（否则会少返回结果）。
"""

from __future__ import annotations

import unittest

from asistent.contracts import Case, Event, NotFound, utcnow_iso
from tests.contracts.helpers import make_case

from .support import SqliteStoreTestCase


class WriteOrderTest(SqliteStoreTestCase):
    # ------------------------------------------------------------------ 夹具
    def _case_batch(self, store, ids, stamp=None):
        """写入若干个 `created_at` **完全相同** 的对象，返回 (写入顺序, 时间戳)。"""
        stamp = stamp or utcnow_iso()
        written = []
        for object_id in ids:
            case = Case(goal="顺序测试 %s" % object_id)
            case.id = object_id
            case.created_at = stamp
            case.updated_at = stamp
            store.put(case)
            written.append(case)
        return written, stamp

    def _ids(self, items):
        return [item.id for item in items]

    def _seq_of(self, store, object_id, table="objects"):
        row = store.connection.execute(
            "SELECT seq FROM %s WHERE id = ?" % table, (object_id,)
        ).fetchone()
        return None if row is None else int(row[0])

    # ------------------------------------------------------------------ 基本顺序
    def test_same_timestamp_objects_follow_write_order(self) -> None:
        store = self.open_store()
        written, _stamp = self._case_batch(store, ["case_a1", "case_a2", "case_a3"])
        self.assertEqual(self._ids(written), self._ids(store.find("Case", limit=None)))
        self.assertEqual(self._ids(written[:1]), self._ids(store.find("Case", limit=1)))

    def test_order_does_not_depend_on_id(self) -> None:
        """id 的字典序故意与写入顺序相反：按 id 平局就会得到反序。"""
        store = self.open_store()
        written, _stamp = self._case_batch(store, ["case_zzz", "case_mmm", "case_aaa"])
        self.assertEqual(["case_zzz", "case_mmm", "case_aaa"], self._ids(store.find("Case", limit=None)))
        self.assertEqual(self._ids(written), self._ids(store.find("Case", limit=3)))
        self.assertEqual(["case_zzz"], self._ids(store.find("Case", limit=1)))

    def test_order_is_stable_across_calls(self) -> None:
        store = self.open_store()
        written, _stamp = self._case_batch(store, ["case_s1", "case_s2", "case_s3", "case_s4"])
        for _ in range(5):
            self.assertEqual(self._ids(written), self._ids(store.find("Case", limit=None)))
        self.assertEqual(
            self._ids(written[:2]),
            self._ids(store.find("Case", limit=2)),
            "limit 必须取**最早**的 n 条（不是最近 n 条）",
        )

    def test_mixed_timestamps_still_sort_by_time_first(self) -> None:
        """时间不同的时候，仍然先按时间：写入序号只做平局判据。

        故意让"时间早的"比"时间晚的"**后写**：如果实现把写入序号放在时间之前，这里就会翻车。
        """
        store = self.open_store()
        self._case_batch(store, ["case_later_stamp"], stamp="2026-01-02T00:00:00.000Z")
        self._case_batch(store, ["case_earlier_stamp"], stamp="2026-01-01T00:00:00.000Z")
        self.assertEqual(
            ["case_earlier_stamp", "case_later_stamp"],
            self._ids(store.find("Case", limit=None)),
            "先按时间戳；写入序号只在同一时间戳内决定顺序",
        )

    # ------------------------------------------------------------------ 持久化
    def test_write_order_survives_reopen(self) -> None:
        store = self.open_store()
        written, stamp = self._case_batch(store, ["case_r1", "case_r2", "case_r3"])
        seqs_before = [self._seq_of(store, case.id) for case in written]
        reopened = self.restart(store)

        self.assertEqual(self._ids(written), self._ids(reopened.find("Case", limit=None)))
        self.assertEqual(self._ids(written[:2]), self._ids(reopened.find("Case", limit=2)))
        self.assertEqual(seqs_before, [self._seq_of(reopened, case.id) for case in written])

        # 重开之后继续写：新对象的序号必须排在所有历史对象之后
        later, _ = self._case_batch(reopened, ["case_r4"], stamp=stamp)
        self.assertEqual(
            ["case_r1", "case_r2", "case_r3", "case_r4"],
            self._ids(reopened.find("Case", limit=None)),
        )
        self.assertGreater(self._seq_of(reopened, later[0].id), max(seqs_before))

    def test_update_does_not_move_the_object(self) -> None:
        store = self.open_store()
        written, stamp = self._case_batch(store, ["case_u1", "case_u2", "case_u3"])
        before = [self._seq_of(store, case.id) for case in written]

        # 更新第一个写入的对象：改内容、改 created_at（合成同一个时间戳），位置都不许动
        target = store.get("Case", "case_u1")
        target.next_action = "改一下内容"
        target.created_at = stamp
        store.put(target, expected_revision=target.revision, audit_action="UPDATE")

        self.assertEqual(["case_u1", "case_u2", "case_u3"], self._ids(store.find("Case", limit=None)))
        self.assertEqual(before, [self._seq_of(store, case.id) for case in written], "更新不得改写写入序号")

    # ------------------------------------------------------------------ 事件 FIFO
    def test_list_events_is_fifo_for_identical_timestamps(self) -> None:
        store = self.open_store()
        stamp = utcnow_iso()
        published = []
        for index in range(3):
            event, created = store.publish_event(
                Event(
                    kind="MAIL_RECEIVED",
                    source="imap",
                    source_event_id="fifo-order-%d" % index,
                    occurred_at=stamp,
                    received_at=stamp,
                )
            )
            self.assertTrue(created)
            published.append(event)

        self.assertEqual(self._ids(published), self._ids(store.list_events(limit=None)))
        self.assertEqual(self._ids(published[:2]), self._ids(store.list_events(limit=2)))
        # 多次调用稳定
        self.assertEqual(self._ids(published), self._ids(store.list_events(limit=None)))

        reopened = self.restart(store)
        self.assertEqual(self._ids(published), self._ids(reopened.list_events(limit=None)), "重开库后 FIFO 不变")

    def test_event_is_not_reachable_through_generic_get(self) -> None:
        """契约 v0.5（CP-20261010-12）：Event 走事件接口，通用 get 必须一致地拒绝。"""
        store = self.open_store()
        event, _created = store.publish_event(
            Event(
                kind="MAIL_RECEIVED",
                source="imap",
                source_event_id="acc-order-1",
                occurred_at=utcnow_iso(),
                received_at=utcnow_iso(),
            )
        )
        with self.assertRaises(NotFound):
            store.get("Event", event.id)
        self.assertIn(event.id, self._ids(store.list_events(limit=None)))

    # ------------------------------------------------------------------ limit 与过滤的次序
    def test_sql_limit_does_not_truncate_before_python_filtering(self) -> None:
        """`where` 里若有**不能下推**的键，必须先过滤再 limit，否则会少返回结果。

        （参考实现是"先过滤、后 apply_limit"；如果 SQL 先 `LIMIT 1` 再被 Python 过滤掉，
        就会返回空列表——同一份数据两个后端给出不同答案。）
        """
        store = self.open_store()
        self._case_batch(store, ["case_f1", "case_f2", "case_f3"])
        target = store.get("Case", "case_f2")
        target.goal = "匹配目标"
        store.put(target, expected_revision=target.revision, audit_action="UPDATE")

        matched = store.find("Case", where={"goal": "匹配目标"}, limit=1)
        self.assertEqual(["case_f2"], self._ids(matched))
        self.assertEqual([], store.find("Case", where={"goal": "没有这个目标"}, limit=1))

        # 能下推的键仍然可以用 SQL 截断，且答案与"过滤后取最早 n 条"一致
        self.assertEqual(
            ["case_f2"],
            self._ids(store.find("Case", where={"id": "case_f2"}, limit=1)),
        )

    def test_make_case_objects_are_ordered_without_touching_timestamps(self) -> None:
        """真实路径（不手工改时间戳）：快速连写也必须按写入顺序返回。"""
        store = self.open_store()
        cases = [make_case(store) for _ in range(5)]
        self.assertEqual(self._ids(cases), self._ids(store.find("Case", limit=None)))
        self.assertEqual(self._ids(cases[:3]), self._ids(store.find("Case", limit=3)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
