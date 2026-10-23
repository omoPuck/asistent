"""SQLite 持久化保证：WAL、重启后数据仍在、索引状态与幂等键持久化。

这些测试回答的问题是：**进程被杀之后，用户看到的状态会不会变**。
"""

from __future__ import annotations

import json
import os
import unittest

from asistent.contracts import DuplicateObject
from asistent.contracts.timeutil import utcnow_iso
from tests.contracts.helpers import make_action, make_case, make_document

from .support import SqliteStoreTestCase, run_python

RESTART_READER_SCRIPT = """
import json, os, sys
sys.path.insert(0, os.getcwd())
from asistent.storage.sqlite_store import SqliteStore

store = SqliteStore(path=sys.argv[1])
case = store.get("Case", sys.argv[2])
payload = {
    "goal": case.goal,
    "state": str(case.state),
    "revision": case.revision,
    "stats": store.stats(),
    "audit_rows": len(store.audit_trail(object_id=case.id)),
    "journal_mode": str(store.connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
}
store.close()
print(json.dumps(payload))
"""


class WalAndDurabilityTest(SqliteStoreTestCase):
    def test_wal_is_enabled_and_sidecar_files_exist(self) -> None:
        store = self.open_store()
        mode = str(store.connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        self.assertEqual("wal", mode)
        self.assertTrue(store.wal_enabled())
        # 崩溃安全：同步级别必须是 FULL（不是 OFF/NORMAL 的"可能丢最近提交"）
        self.assertEqual(2, int(store.connection.execute("PRAGMA synchronous").fetchone()[0]))
        # 写一笔之后，WAL 旁路文件必须真实存在（证明不是"只是声明了 WAL"）
        make_case(store)
        self.assertTrue(os.path.exists(self.db_path + "-wal"), "WAL 旁路文件不存在")
        store.checkpoint()
        self.assertEqual(
            1, int(store.connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0])
        )

    def test_memory_database_skips_wal_without_failing(self) -> None:
        store = self.open_store(path=":memory:")
        self.assertEqual("memory", str(store.connection.execute("PRAGMA journal_mode").fetchone()[0]).lower())
        self.assertFalse(store.wal_enabled())
        case = make_case(store)
        self.assertEqual(case.id, store.get("Case", case.id).id)

    def test_objects_audit_events_survive_reopen(self) -> None:
        store = self.open_store()
        case = make_case(store)
        store.transition("Case", case.id, "TRIAGED", actor_kind="AGENT", actor_id="agent", reason="分类完成")
        doc = make_document(store, identity="mail-restart-1")
        action, approval, _summary = make_action(store, case=case)
        store.claim_action(action.id, worker="executor-1")
        before = {
            "trail": len(store.audit_trail(object_id=case.id)),
            "stats": store.stats(),
            "events": len(store.list_events()),
        }

        reopened = self.restart(store)
        self.assertEqual("TRIAGED", str(reopened.get("Case", case.id).state))
        self.assertEqual("IN_FLIGHT", str(reopened.get("ExternalAction", action.id).state))
        self.assertEqual("CONSUMED", str(reopened.get("Approval", approval.id).state))
        self.assertEqual(doc.id, reopened.get("SourceDocument", doc.id).id)
        self.assertEqual(before["trail"], len(reopened.audit_trail(object_id=case.id)))
        self.assertEqual(before["stats"], reopened.stats())
        self.assertEqual(before["events"], len(reopened.list_events()))
        self.assertEqual([], reopened.migrations_applied, "重开不应该再跑迁移")

    def test_other_process_sees_committed_rows(self) -> None:
        """真正的"进程重启"证据：另一个进程打开同一个库文件能读到已提交数据。"""
        store = self.open_store()
        case = make_case(store, goal="处理一封需要回复的通知邮件")
        store.close()

        code, output = run_python(RESTART_READER_SCRIPT, self.db_path, case.id)
        self.assertEqual(0, code, output)
        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual("处理一封需要回复的通知邮件", payload["goal"])
        self.assertEqual("NEW", payload["state"])
        self.assertEqual(1, payload["revision"])
        self.assertEqual(1, payload["stats"]["Case"])
        self.assertEqual(1, payload["audit_rows"])
        self.assertEqual("wal", payload["journal_mode"])

    def test_index_state_and_error_survive_reopen(self) -> None:
        store = self.open_store()
        doc = make_document(store, identity="mail-index-restart")
        store.set_index_state(doc.id, "INDEXED", reason="首次索引")
        indexed_at = store.get("SourceDocument", doc.id).last_indexed_at
        self.assertTrue(indexed_at)

        reopened = self.restart(store)
        self.assertEqual("INDEXED", str(reopened.get("SourceDocument", doc.id).index_state))
        self.assertEqual(indexed_at, reopened.get("SourceDocument", doc.id).last_indexed_at)

        reopened.set_index_state(doc.id, "STALE", reason="资料被修改")
        reopened.set_index_state(doc.id, "FAILED", reason="解析失败", error="boom")
        again = self.restart(reopened)
        failed = again.get("SourceDocument", doc.id)
        self.assertEqual("FAILED", str(failed.index_state))
        self.assertEqual("boom", failed.index_error)
        self.assertIn(doc.id, again.recovery_scan().pending_index_document_ids)

    def test_unique_key_survives_reopen_and_still_blocks_duplicates(self) -> None:
        store = self.open_store()
        doc = make_document(store, identity="mail-unique-restart")

        reopened = self.restart(store)
        found = reopened.get_by_unique("mail_identity", "mail-unique-restart")
        self.assertIsNotNone(found)
        self.assertEqual(doc.id, found.id)
        with self.assertRaises(DuplicateObject) as ctx:
            make_document(reopened, identity="mail-unique-restart")
        self.assertEqual(doc.id, ctx.exception.detail["existing_id"])

    def test_event_dedupe_and_claim_survive_reopen(self) -> None:
        from asistent.contracts import Event

        store = self.open_store()
        event, created = store.publish_event(
            Event(
                kind="MAIL_RECEIVED",
                source="imap:owner@example.com",
                source_event_id="uid-restart-1",
                occurred_at=utcnow_iso(),
                received_at=utcnow_iso(),
                payload={"subject": "通知"},
            )
        )
        self.assertTrue(created)
        self.assertTrue(store.claim_event(event.id, worker="worker-a"))

        reopened = self.restart(store)
        replay, created_again = reopened.publish_event(
            Event(
                kind="MAIL_RECEIVED",
                source="imap:owner@example.com",
                source_event_id="uid-restart-1",
                occurred_at=utcnow_iso(),
                received_at=utcnow_iso(),
                payload={"subject": "重复投递"},
            )
        )
        self.assertFalse(created_again, "重启后重复投递必须仍然被去重")
        self.assertEqual(event.id, replay.id)
        self.assertFalse(reopened.claim_event(event.id, worker="worker-b"), "已领取的事件不得被第二个 worker 抢走")
        reopened.mark_event_processed(event.id, result="ok")
        self.assertFalse(reopened.claim_event(event.id, worker="worker-c"))
        self.assertEqual(1, len(reopened.list_events()))
        self.assertEqual(1, len(reopened.list_events(processed=True)))

    def test_in_flight_after_crash_is_demoted_to_unknown_not_success(self) -> None:
        """结果未知语义：进程在 IN_FLIGHT 时被杀，重启不得当成功，必须转 UNKNOWN 查证。"""
        store = self.open_store()
        case = make_case(store)
        action, approval, _summary = make_action(store, case=case)
        attempt = store.claim_action(action.id, worker="executor-crashed")
        store.close()

        reopened = self.restart(store)
        self.assertEqual("IN_FLIGHT", str(reopened.get("ExternalAction", action.id).state))
        self.assertEqual([action.id], reopened.demote_in_flight_to_unknown(reason="进程重启"))

        final = reopened.get("ExternalAction", action.id)
        self.assertEqual("UNKNOWN", str(final.state), "绝不允许把 IN_FLIGHT 当成成功")
        self.assertIsNotNone(final.finalized_at)
        self.assertEqual("UNKNOWN", reopened.get("ActionAttempt", attempt.id).state)
        self.assertEqual("EXTERNAL_UNKNOWN", reopened.get("ActionAttempt", attempt.id).failure_class)
        self.assertEqual("CONSUMED", str(reopened.get("Approval", approval.id).state))
        scan = reopened.recovery_scan()
        self.assertIn(action.id, scan.unknown_action_ids)
        self.assertNotIn(action.id, scan.in_flight_action_ids)
        self.assertEqual([], reopened.demote_in_flight_to_unknown())
        # 只增不减：审计里能看到这次"领取 → 未知"的完整链路
        actions = [str(item.action) for item in reopened.audit_trail(object_id=action.id)]
        self.assertIn("EXECUTE", actions)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
