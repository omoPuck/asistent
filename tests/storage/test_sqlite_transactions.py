"""SQLite 事务语义：原子性（失败整体回滚）、CAS 冲突、审计只追加。

三个问题必须由**数据库**回答，而不是靠调用方自觉：

1. 一个多步操作中途失败，会不会留下半个状态？（`ROLLBACK`）
2. 两个写入者基于同一旧版本写，会不会静默覆盖？（`RevisionConflict`）
3. 审计记录能不能被就地改掉？（触发器 `ABORT`）
"""

from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from asistent.contracts import ActionSummary, ApprovalInvalid, RevisionConflict, ValidationError
from asistent.contracts.enums import ActorKind, AuditAction
from asistent.contracts.objects import AuditEvent
from tests.contracts.helpers import make_action, make_case, make_document

from .support import SqliteStoreTestCase


class TransactionAtomicityTest(SqliteStoreTestCase):
    def test_failed_transition_rolls_back_object_and_audit(self) -> None:
        """状态转换是"写对象 + 写审计 + 发事件"，事件环节失败必须整体回滚。"""
        store = self.open_store()
        case = make_case(store)
        objects_before = store.raw_counts()["objects"]
        audit_before = store.raw_counts()["audit_events"]

        with mock.patch.object(
            type(store), "_emit_state_event", side_effect=RuntimeError("模拟事务中途崩溃")
        ):
            with self.assertRaises(RuntimeError):
                store.transition("Case", case.id, "TRIAGED", actor_kind="AGENT", actor_id="agent")

        self.assertEqual("NEW", str(store.get("Case", case.id).state), "对象写入必须被回滚")
        self.assertEqual(objects_before, store.raw_counts()["objects"])
        self.assertEqual(audit_before, store.raw_counts()["audit_events"], "审计写入必须一起回滚")
        self.assertEqual([], [e for e in store.list_events() if e.kind == "STATE_CHANGED"])

        # 回滚之后连接必须仍然可用（事务没有卡死）
        store.transition("Case", case.id, "TRIAGED", actor_kind="AGENT", actor_id="agent")
        self.assertEqual("TRIAGED", str(store.get("Case", case.id).state))
        self.assertEqual(1, len([e for e in store.list_events() if e.kind == "STATE_CHANGED"]))

    def test_failed_claim_leaves_no_partial_write(self) -> None:
        """领取动作前的内容一致性校验失败 → 不许留下 attempt，也不许消费审批。"""
        store = self.open_store()
        case = make_case(store)
        action, approval, summary = make_action(store, case=case, body="我确认参加。")

        changed = ActionSummary(
            kind=summary.kind,
            from_account=summary.from_account,
            to=list(summary.to),
            bcc=list(summary.bcc),
            subject=summary.subject,
            body="我确认参加（正文被改）。",
            draft_version_id=summary.draft_version_id,
            target_thread_ref=summary.target_thread_ref,
            evidence_versions=dict(summary.evidence_versions),
            attachments=list(summary.attachments),
        )
        action.action_summary = changed.normalized()
        action.summary_digest = changed.digest()
        store.put(action, expected_revision=action.revision, reason="草稿被编辑")
        before = store.raw_counts()

        with self.assertRaises(ApprovalInvalid):
            store.claim_action(action.id, worker="executor-1")

        self.assertEqual([], store.find("ActionAttempt"), "校验失败不得创建 attempt")
        self.assertEqual("AUTHORIZED", str(store.get("ExternalAction", action.id).state))
        self.assertEqual("ACTIVE", str(store.get("Approval", approval.id).state))
        self.assertEqual(before["objects"], store.raw_counts()["objects"])
        self.assertEqual(before["audit_events"], store.raw_counts()["audit_events"])

    def test_cas_conflict_is_detected_and_stored_value_is_preserved(self) -> None:
        store = self.open_store()
        case = make_case(store)
        winner = store.get("Case", case.id)
        loser = store.get("Case", case.id)
        winner.goal = "胜利者的目标"
        store.put(winner, expected_revision=1, reason="并发写入 1")

        loser.goal = "失败者的目标"
        with self.assertRaises(RevisionConflict) as ctx:
            store.put(loser, expected_revision=1, reason="并发写入 2")
        self.assertEqual(1, ctx.exception.detail["expected"])
        self.assertEqual(2, ctx.exception.detail["actual"])
        self.assertEqual("胜利者的目标", store.get("Case", case.id).goal)
        self.assertEqual(2, store.get("Case", case.id).revision)

    def test_update_without_expected_revision_is_rejected(self) -> None:
        store = self.open_store()
        case = make_case(store)
        case.goal = "静默覆盖尝试"
        with self.assertRaises(ValidationError):
            store.put(case)
        self.assertEqual("处理一封需要回复的通知邮件", store.get("Case", case.id).goal)

    def test_index_state_write_conflict_is_rejected(self) -> None:
        store = self.open_store()
        doc = make_document(store, identity="mail-index-cas")
        store.set_index_state(doc.id, "INDEXED", reason="首次索引")
        with self.assertRaises(RevisionConflict):
            store.set_index_state(doc.id, "STALE", expected_revision=1, reason="基于旧版本")


class AuditAppendOnlyTest(SqliteStoreTestCase):
    def test_audit_rows_cannot_be_updated_or_deleted_by_sql(self) -> None:
        """审计只追加由数据库触发器强制：绕过程序直接写 SQL 也改不动。"""
        store = self.open_store()
        case = make_case(store)
        store.transition("Case", case.id, "TRIAGED", actor_kind="AGENT", actor_id="agent", reason="分类完成")
        rows_before = store.raw_counts()["audit_events"]
        self.assertGreaterEqual(rows_before, 2)

        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("UPDATE audit_events SET action = 'VOID'")
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("DELETE FROM audit_events")

        self.assertEqual(rows_before, store.raw_counts()["audit_events"])
        trail = store.audit_trail(object_id=case.id)
        self.assertEqual("分类完成", trail[-1].reason)
        self.assertEqual("TRANSITION", str(trail[-1].action))

    def test_correction_is_a_new_appended_row(self) -> None:
        store = self.open_store()
        case = make_case(store)
        store.transition("Case", case.id, "TRIAGED", actor_kind=ActorKind.AGENT, actor_id="agent")
        target = store.audit_trail(object_id=case.id)[-1]
        before = store.raw_counts()["audit_events"]

        correction = store.append_audit(
            AuditEvent(
                action=str(AuditAction.CORRECTION),
                actor_kind=ActorKind.USER,
                actor_id="owner",
                object_type="Case",
                object_id=case.id,
                reason="修正分类原因",
                voids_event_id=target.id,
            )
        )
        self.assertTrue(correction.id)
        self.assertEqual(before + 1, store.raw_counts()["audit_events"])
        trail = store.audit_trail(object_id=case.id)
        self.assertEqual(target.id, trail[-1].voids_event_id)
        self.assertEqual("CORRECTION", str(trail[-1].action))
        # 旧记录仍然原样可读（没有被覆盖）
        self.assertEqual(target.id, trail[-2].id)

    def test_audit_requires_action(self) -> None:
        store = self.open_store()
        with self.assertRaises(ValidationError):
            store.append_audit(AuditEvent(action=""))

    def test_audit_trail_filters_and_limit(self) -> None:
        store = self.open_store()
        case = make_case(store)
        other = make_case(store, goal="另一件事务")
        store.transition("Case", case.id, "TRIAGED", actor_kind="AGENT", actor_id="agent")
        self.assertEqual(1, len(store.audit_trail(object_id=case.id, limit=1)))
        self.assertEqual(1, len(store.audit_trail(object_id=other.id)))
        self.assertNotEqual(case.id, other.id)
        self.assertEqual(0, len(store.audit_trail(case_id="case_不存在")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
