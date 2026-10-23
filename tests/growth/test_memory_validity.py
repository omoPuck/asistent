"""MVP-6 §862：记忆过期后**不再作为事实使用**，但历史**照原样保留、仍然可查**。

一边是"不能再用旧结论"，一边是"不许删历史"——这两件事必须同时成立，所以本模块每个用例都
同时断言两侧：可用集合里没有它了、而对象与它落过的历史事实一个字都没变。
"""

from __future__ import annotations

from typing import Any, Dict

from asistent.contracts.enums import RuleState as MemoryState
from asistent.contracts.errors import ValidationError
from asistent.growth import (
    MEMORY_VALIDITY_KIND,
    all_memories,
    confirm_memory,
    facts_from_memories,
    grant_validity,
    is_expired,
    is_usable,
    memory_valid_until,
    remember,
    revoke_memory,
    sweep_expired_memories,
    usable_memories,
    usable_statements,
    validity_task,
)
from tests.contracts.helpers import make_case
from tests.growth.support import GrowthCase, dump, requires_persistence

T0 = "2026-10-05T00:00:00.000Z"
T_PLUS_30M = "2026-10-05T00:30:00.000Z"
T_PLUS_2H = "2026-10-05T02:00:00.000Z"
T_PLUS_1Y = "2027-10-05T00:00:00.000Z"
STATEMENT = "家长偏好周五晚上联系，不要周末上午"


class MemoryValidityTest(GrowthCase):
    def new_case(self) -> Any:
        """记忆落成事实时需要一个真实存在的 Case（事实必须挂在事务上）。"""
        return make_case(self.store)

    def remember_preference(self, store: Any = None, **kwargs: Any) -> Any:
        store = store or self.store
        document = self.make_doc(store, "01-notice-multi-time.eml", document_id=self.doc_id("01", "memory-src"))
        params: Dict[str, Any] = {
            "statement": STATEMENT,
            "scope": "global",
            "source_case_id": make_case(store).id,
            "source_document_id": document.id,
            "ttl_seconds": 3600,
            "now": T0,
        }
        params.update(kwargs)
        return remember(store, **params)

    def test_expired_memory_is_not_used_but_history_is_kept(self) -> None:
        memory = confirm_memory(self.store, self.remember_preference().id)
        self.assertEqual(str(MemoryState.ENABLED), str(memory.state))
        self.assertEqual("2026-10-05T01:00:00.000Z", memory_valid_until(self.store, memory.id))
        self.assertFalse(is_expired(self.store, memory, now=T_PLUS_30M))
        self.assertTrue(is_usable(self.store, memory, now=T_PLUS_30M))
        self.assertTrue(is_expired(self.store, memory, now=T_PLUS_2H))
        self.assertFalse(is_usable(self.store, memory, now=T_PLUS_2H))
        # 可用性快照：在清扫**之前**取，避免被后面的清扫影响（清扫后同一时刻也不可用）
        snapshot_30m = {
            "expired": is_expired(self.store, memory, now=T_PLUS_30M),
            "usable": is_usable(self.store, memory, now=T_PLUS_30M),
            "statements": usable_statements(self.store, now=T_PLUS_30M),
            "usable_memory_ids": [item.id for item in usable_memories(self.store, now=T_PLUS_30M)],
        }
        snapshot_2h = {
            "expired": is_expired(self.store, memory, now=T_PLUS_2H),
            "usable": is_usable(self.store, memory, now=T_PLUS_2H),
            "statements": usable_statements(self.store, now=T_PLUS_2H),
            "usable_memory_ids": [item.id for item in usable_memories(self.store, now=T_PLUS_2H)],
        }
        self.assertEqual([STATEMENT], snapshot_30m["statements"])
        self.assertEqual([], snapshot_2h["statements"])

        # 未过期：落成事实（带引用，可回溯到来源资料）
        before_case = self.new_case().id
        result_before = facts_from_memories(self.store, case_id=before_case, now=T_PLUS_30M)
        self.assertEqual(1, len(result_before.created))
        old_fact_id = result_before.created[0]
        old_fact = self.store.get("Fact", old_fact_id)
        self.assertEqual(STATEMENT, old_fact.statement)
        self.assertTrue(old_fact.citations)
        fact_snapshot = old_fact.to_dict()

        # 过期后：不再作为事实使用（新事务里拿不到）
        after_case = self.new_case().id
        result_after = facts_from_memories(self.store, case_id=after_case, now=T_PLUS_2H)
        self.assertEqual([], result_after.created)
        self.assertEqual(1, len(result_after.skipped))
        self.assertEqual(memory.id, result_after.skipped[0][0])
        self.assertIn("已过期", result_after.skipped[0][1])
        self.assertEqual([], usable_statements(self.store, now=T_PLUS_2H))
        self.assertEqual([], [item.id for item in self.store.find("Fact", where={"case_id": after_case}, limit=None)])

        # 历史照原样：对象在、陈述没被改写、当时落的事实一个字没变
        self.assertEqual(fact_snapshot, self.store.get("Fact", old_fact_id).to_dict())
        kept = self.store.get("Memory", memory.id)
        self.assertEqual(STATEMENT, kept.statement)
        self.assertEqual(str(MemoryState.ENABLED), str(kept.state))  # 过期 ≠ 被删；清扫前还是 ENABLED

        swept = sweep_expired_memories(self.store, now=T_PLUS_2H)
        self.assertEqual([memory.id], swept)
        self.assertEqual([], sweep_expired_memories(self.store, now=T_PLUS_2H))  # 幂等
        after_sweep = self.store.get("Memory", memory.id)
        self.assertEqual(str(MemoryState.REVOKED), str(after_sweep.state))
        self.assertEqual(STATEMENT, after_sweep.statement)
        self.assertEqual([memory.id], [item.id for item in all_memories(self.store)])
        self.assertEqual(fact_snapshot, self.store.get("Fact", old_fact_id).to_dict())
        validity = validity_task(self.store, memory.id)
        dump(
            "记忆过期：不再使用 + 历史保留",
            {
                "memory": {
                    "id": memory.id,
                    "statement": after_sweep.statement,
                    "state_before_sweep": str(kept.state),
                    "state_after_sweep": str(after_sweep.state),
                    "valid_until": memory_valid_until(self.store, memory.id),
                    "still_queryable": [item.id for item in all_memories(self.store)],
                },
                "usability": {
                    "note": "两个快照都在清扫之前采集",
                    "at_T0+30m": snapshot_30m,
                    "at_T0+2h": snapshot_2h,
                },
                "facts": {
                    "before_expiry": result_before.to_dict(),
                    "after_expiry": result_after.to_dict(),
                    "historical_fact_unchanged": fact_snapshot == self.store.get("Fact", old_fact_id).to_dict(),
                    "historical_fact": {
                        "id": old_fact_id,
                        "statement": fact_snapshot.get("statement"),
                        "citations": fact_snapshot.get("citations"),
                        "links": fact_snapshot.get("links"),
                    },
                },
                "sweep": {
                    "first_run": swept,
                    "second_run": [],
                    "validity_task": None
                    if validity is None
                    else {
                        "id": validity.id,
                        "kind": str(validity.kind),
                        "state": str(validity.state),
                        "timeout_at": validity.timeout_at,
                        "outputs": dict(validity.outputs or {}),
                    },
                },
            },
        )

    def test_candidate_memory_is_not_used_until_confirmed(self) -> None:
        memory = self.remember_preference(confirmed_by="")
        self.assertEqual(str(MemoryState.CANDIDATE), str(memory.state))
        self.assertEqual([], usable_statements(self.store, now=T_PLUS_30M))
        result = facts_from_memories(self.store, case_id=self.new_case().id, now=T_PLUS_30M)
        self.assertEqual([], result.created)
        self.assertEqual(1, len(result.skipped))
        dump(
            "记忆：未确认不算数",
            {
                "memory_state": str(memory.state),
                "usable_statements": [],
                "facts_result": result.to_dict(),
                "skip_reason": result.skipped[0][1],
            },
        )

    def test_memory_without_expiry_stays_usable(self) -> None:
        memory = confirm_memory(self.store, self.remember_preference(ttl_seconds=None).id)
        self.assertIsNone(memory_valid_until(self.store, memory.id))
        self.assertIsNone(validity_task(self.store, memory.id))
        self.assertFalse(is_expired(self.store, memory, now=T_PLUS_1Y))
        self.assertEqual([STATEMENT], usable_statements(self.store, now=T_PLUS_1Y))
        dump(
            "记忆：没有有效期的长期可用",
            {
                "valid_until": None,
                "expired_after_one_year": is_expired(self.store, memory, now=T_PLUS_1Y),
                "usable_statements": usable_statements(self.store, now=T_PLUS_1Y),
            },
        )

    def test_revoked_memory_is_not_used_but_queryable(self) -> None:
        memory = confirm_memory(self.store, self.remember_preference().id)
        with self.assertRaises(ValidationError):
            revoke_memory(self.store, memory.id, reason="  ")
        revoked = revoke_memory(self.store, memory.id, reason="用户撤销这条偏好")
        self.assertEqual(str(MemoryState.REVOKED), str(revoked.state))
        self.assertEqual(STATEMENT, revoked.statement)
        self.assertEqual([], usable_statements(self.store, now=T_PLUS_30M))
        self.assertEqual([memory.id], [item.id for item in all_memories(self.store)])
        dump(
            "记忆：撤销后不可用但可查",
            {
                "state": str(revoked.state),
                "statement_unchanged": revoked.statement == STATEMENT,
                "usable_statements": [],
                "still_queryable": [item.id for item in all_memories(self.store)],
            },
        )

    @requires_persistence
    def test_validity_window_survives_restart(self) -> None:
        memory = confirm_memory(self.store, self.remember_preference(ttl_seconds=None).id)
        grant_validity(self.store, memory_id=memory.id, ttl_seconds=7200, now=T0)
        # 不允许悄悄改期（避免无声延长记忆寿命）
        with self.assertRaises(ValidationError):
            grant_validity(self.store, memory_id=memory.id, ttl_seconds=86400, now=T0)
        restarted = self.restart_store()
        reloaded = restarted.get("Memory", memory.id)
        self.assertEqual("2026-10-05T02:00:00.000Z", memory_valid_until(restarted, memory.id))
        self.assertTrue(is_usable(restarted, reloaded, now=T_PLUS_30M))
        self.assertTrue(is_expired(restarted, reloaded, now=T_PLUS_2H))
        task = validity_task(restarted, memory.id)
        dump(
            "§862 有效期是存下来的，不是进程内的",
            {
                "backend": self.backend,
                "valid_until": memory_valid_until(restarted, memory.id),
                "usable_at_T0+30m": is_usable(restarted, reloaded, now=T_PLUS_30M),
                "expired_at_T0+2h": is_expired(restarted, reloaded, now=T_PLUS_2H),
                "validity_task": None
                if task is None
                else {"id": task.id, "kind": str(task.kind), "timeout_at": task.timeout_at, "state": str(task.state)},
                "task_kind_constant": MEMORY_VALIDITY_KIND,
            },
        )
