"""`limit` 语义边界（契约 v0.3，CP-20261010-03）。

契约把 `limit` 的裁决权收到**一个点**：

- `None` → 不限制；
- `>= 0` → 最多 limit 条（`0` → 空列表）；
- `< 0`  → `ValidationError`（不允许靠 Python 负切片隐式"从尾部取"）。

`find` / `list_events` / `audit_trail` 三个入口必须给出同一个答案，且
`audit_trail` 取的是**最新** limit 条（返回顺序仍是正序）。

这里的做法是逐例把"我的实现"与"契约自己的 `apply_limit`/`apply_limit_tail`"
放在一起比对：语义只有一处权威，漂移立刻变红。
"""

from __future__ import annotations

import unittest

from asistent.contracts import Event, ValidationError, utcnow_iso
from asistent.contracts.reference import apply_limit, apply_limit_tail
from tests.contracts.helpers import make_case

from .support import SqliteStoreTestCase

#: 边界矩阵：None=不限、0=空、超出总数、正好等于总数、1
LIMITS = (None, 0, 1, 2, 3, 10)


class LimitSemanticsTest(SqliteStoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()

    # ------------------------------------------------------------------ 夹具
    def _seed(self, cases: int = 3, events: int = 4, audits: int = 3) -> None:
        for _ in range(cases):
            case = make_case(self.store)
            for index in range(audits):
                case.next_action = "第 %d 步" % index
                case = self.store.put(
                    case,
                    expected_revision=case.revision,
                    audit_action="UPDATE",
                    audit_detail={"step": index},
                )
        for index in range(events):
            self.store.publish_event(
                Event(
                    kind="MAIL_RECEIVED",
                    source="imap:owner@example.com",
                    source_event_id="uid-limit-%d" % index,
                    occurred_at=utcnow_iso(),
                    received_at=utcnow_iso(),
                    payload={"subject": "第 %d 封" % index},
                )
            )

    def _ids(self, items) -> list:
        return [item.id for item in items]

    # ------------------------------------------------------------------ find
    def test_find_limit_matrix(self) -> None:
        self._seed()
        self.assertEqual(3, len(self.store.find("Case")))
        self.assertEqual(3, len(self.store.find("Case", limit=None)))
        self.assertEqual(3, len(self.store.find("Case", limit=10)))
        self.assertEqual(2, len(self.store.find("Case", limit=2)))
        self.assertEqual(0, len(self.store.find("Case", limit=0)))
        with self.assertRaises(ValidationError):
            self.store.find("Case", limit=-1)

    def test_find_matches_contract_decision_point(self) -> None:
        self._seed()
        everything = self.store.find("Case", limit=None)
        for limit in LIMITS:
            self.assertEqual(
                self._ids(apply_limit(everything, limit)),
                self._ids(self.store.find("Case", limit=limit)),
                "find(limit=%r) 与契约 apply_limit 不一致" % (limit,),
            )

    # ------------------------------------------------------------------ list_events
    def test_list_events_limit_matrix(self) -> None:
        self._seed()
        self.assertEqual(4, len(self.store.list_events()))
        self.assertEqual(4, len(self.store.list_events(limit=None)))
        self.assertEqual(2, len(self.store.list_events(limit=2)))
        self.assertEqual(0, len(self.store.list_events(limit=0)))
        with self.assertRaises(ValidationError):
            self.store.list_events(limit=-1)

    def test_list_events_matches_contract_decision_point(self) -> None:
        self._seed()
        everything = self.store.list_events(limit=None)
        for limit in LIMITS:
            self.assertEqual(
                self._ids(apply_limit(everything, limit)),
                self._ids(self.store.list_events(limit=limit)),
                "list_events(limit=%r) 与契约 apply_limit 不一致" % (limit,),
            )

    # ------------------------------------------------------------------ audit_trail
    def test_audit_trail_returns_the_latest_rows_in_ascending_order(self) -> None:
        self._seed(cases=1, events=0, audits=3)
        everything = self.store.audit_trail(limit=None)
        self.assertEqual(4, len(everything), "1 条 CREATE + 3 条 UPDATE")

        self.assertEqual(0, len(self.store.audit_trail(limit=0)))
        latest = self.store.audit_trail(limit=2)
        self.assertEqual(2, len(latest))
        self.assertEqual(
            self._ids(everything[-2:]), self._ids(latest), "audit_trail 必须返回**最新**的 limit 条"
        )
        steps = [(event.detail or {}).get("step") for event in latest]
        self.assertEqual([1, 2], steps, "返回顺序必须仍是正序（最新两条按 1、2 排列）")
        with self.assertRaises(ValidationError):
            self.store.audit_trail(limit=-1)

    def test_audit_trail_matches_contract_decision_point(self) -> None:
        self._seed(cases=2, events=0, audits=3)
        everything = self.store.audit_trail(limit=None)
        self.assertEqual(8, len(everything))
        for limit in LIMITS:
            self.assertEqual(
                self._ids(apply_limit_tail(everything, limit)),
                self._ids(self.store.audit_trail(limit=limit)),
                "audit_trail(limit=%r) 与契约 apply_limit_tail 不一致" % (limit,),
            )

    # ------------------------------------------------------------------ 一致性 / 健壮性
    def test_three_entrypoints_agree_on_every_limit(self) -> None:
        """三个入口对同一 limit 要么都给结果、要么都报 ValidationError。"""
        self._seed()
        for limit in LIMITS:
            expected_cases = 3 if limit is None else min(3, limit)
            expected_events = 4 if limit is None else min(4, limit)
            expected_audits = 12 if limit is None else min(12, limit)
            counts = (
                len(self.store.find("Case", limit=limit)),
                len(self.store.list_events(limit=limit)),
                len(self.store.audit_trail(limit=limit)),
            )
            self.assertEqual(
                (expected_cases, expected_events, expected_audits),
                counts,
                "limit=%r 时三个入口的条数不符合契约" % (limit,),
            )
        for method in ("find", "list_events", "audit_trail"):
            with self.assertRaises(ValidationError, msg="%s 必须拒绝负数 limit" % method):
                if method == "find":
                    self.store.find("Case", limit=-1)
                else:
                    getattr(self.store, method)(limit=-1)

    def test_negative_limit_is_rejected_before_any_work_and_leaves_usable_store(self) -> None:
        """空库也要报错（说明校验发生在"取数据"之前），且报错后连接不卡死。"""
        with self.assertRaises(ValidationError):
            self.store.find("Case", limit=-5)
        with self.assertRaises(ValidationError):
            self.store.audit_trail(limit=-5)

        self._seed(cases=1, events=1, audits=0)
        self.assertEqual(1, len(self.store.find("Case")))
        self.assertEqual(1, len(self.store.list_events()))
        self.assertEqual(1, len(self.store.audit_trail(limit=None)))

    def test_out_of_range_limit_types_are_not_silently_swallowed(self) -> None:
        """`limit` 是 None / 非负整数；布尔 True 当 1 用（Python 语义），字符串直接报错。"""
        self._seed()
        self.assertEqual(1, len(self.store.find("Case", limit=True)))
        with self.assertRaises(TypeError):
            self.store.find("Case", limit="2")  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
