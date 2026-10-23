"""存储契约测试：revision CAS、审计追加、事件去重、幂等、恢复扫描。

这组测试是**可执行契约**：MVP-0 的 SQLite 实现必须原样通过
（切换方式：`ASISTENT_STORE=sqlite python3 -m unittest discover -s tests -t .`）。
"""

from __future__ import annotations

import threading
import unittest

from asistent.contracts import (
    DEFAULT_APPROVAL_TTL_SECONDS,
    ActionSummary,
    Approval,
    ApprovalInvalid,
    AuditEvent,
    Case,
    DuplicateObject,
    Event,
    IllegalTransition,
    NotFound,
    RevisionConflict,
    ValidationError,
    check_approval_valid,
    utcnow_iso,
)
from asistent.contracts.enums import ActorKind
from asistent.contracts.timeutil import add_seconds

from .helpers import make_action, make_case, make_document, store_factory


class StoreV03ContractTest(unittest.TestCase):
    """契约 v0.3 增补（CP-20261010-03/04/05）：

    - 冻结测试调用的 `get_by_unique`、以及 `now`/`put(audit_detail=)` 必须**登记进协议**，
      否则照协议实现的第三方必然漏方法（MVP-0 独立复核时提出的真实缺口）；
    - `limit` 语义必须显式：`None` 不限、`>=0` 最多 N 条、`<0` 报 ValidationError
      （不允许靠 Python 负切片隐式"从尾部取"）。
    """

    def setUp(self) -> None:
        self.store = store_factory()

    def test_protocol_declares_the_methods_frozen_tests_use(self) -> None:
        from asistent.contracts.store_api import StoreProtocol

        for name in ("put", "get", "find", "find_one", "get_by_unique", "now", "audit_trail", "list_events"):
            self.assertTrue(hasattr(StoreProtocol, name), f"StoreProtocol 未登记 {name}")

    def test_limit_semantics_are_explicit(self) -> None:
        for _ in range(3):
            make_case(self.store)
        self.assertEqual(3, len(self.store.find("Case", limit=None)))
        self.assertEqual(3, len(self.store.find("Case", limit=10)))
        self.assertEqual(2, len(self.store.find("Case", limit=2)))
        self.assertEqual(0, len(self.store.find("Case", limit=0)))
        with self.assertRaises(ValidationError):
            self.store.find("Case", limit=-1)
        with self.assertRaises(ValidationError):
            self.store.list_events(limit=-1)
        with self.assertRaises(ValidationError):
            self.store.audit_trail(limit=-1)

    def test_get_by_unique_returns_none_instead_of_raising(self) -> None:
        self.assertIsNone(self.store.get_by_unique("case_trigger", "does-not-exist"))
        case = make_case(self.store)
        found = self.store.get_by_unique("case_trigger", f"trigger-{case.id}")
        # 参考实现与 SQLite 实现都必须按同一语义返回：要么给对象，要么给 None
        self.assertTrue(found is None or found.id == case.id)

    def test_now_is_utc_iso_with_milliseconds(self) -> None:
        moment = self.store.now()
        self.assertRegex(moment, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_put_audit_detail_lands_in_the_audit_trail(self) -> None:
        case = make_case(self.store)
        case.next_action = "回信确认参加"
        self.store.put(
            case,
            expected_revision=case.revision,
            audit_action="UPDATE",
            audit_detail={"step": "v03-contract-test"},
        )
        trail = self.store.audit_trail(object_id=case.id, limit=None)
        self.assertTrue(
            any((event.detail or {}).get("step") == "v03-contract-test" for event in trail),
            "put(audit_detail=...) 必须落到审计事件里",
        )


class StoreV04ContractTest(unittest.TestCase):
    """契约 v0.4 增补（CP-20261010-06/07/08/10）：

    - `find`/`list_events`/`audit_trail` 的 limit **截取哪一端**必须写死（MVP-1 的幂等测试
      曾因默认 `find` 返回"最近 N 条"而假绿）；
    - `content_origin` 必须能声明 `content_hash` 到底哈希的是什么（原始字节 vs 元数据投影）；
    - `MailSource.cursor_position()` 是**可选**能力，调用方必须探测而不能假定存在；
    - 幂等口径：重复投递后**对象计数不变**（审计行允许增长）。
    """

    def setUp(self) -> None:
        self.store = store_factory()

    def _cases_with_identical_timestamp(self, count: int = 3):
        """造 count 个 `created_at` **完全相同**的对象，按写入顺序返回。

        必须显式把时间戳设成一样，而不是"快速连写碰巧同毫秒"：
        否则测试会因为实现快慢而忽绿忽红（参考实现慢、常跨毫秒，SQLite 快、必然撞上），
        我们就会漏掉"平局判据不稳定"这个真实缺陷（CP-20261010-11）。
        """
        stamp = utcnow_iso()
        cases = []
        for _ in range(count):
            case = make_case(self.store)
            case.created_at = stamp
            self.store.put(case, expected_revision=case.revision, audit_action="UPDATE")
            cases.append(case)
        return stamp, cases

    def test_find_returns_the_earliest_n_not_the_latest(self) -> None:
        cases = [make_case(self.store) for _ in range(3)]
        found = self.store.find("Case", limit=2)
        self.assertEqual([case.id for case in cases[:2]], [case.id for case in found])

    def test_ties_are_broken_by_write_order(self) -> None:
        _, cases = self._cases_with_identical_timestamp(3)
        found = self.store.find("Case", limit=3)
        self.assertEqual([case.id for case in cases], [case.id for case in found])

    def test_find_order_is_stable_across_calls(self) -> None:
        self._cases_with_identical_timestamp(4)
        first = [case.id for case in self.store.find("Case", limit=4)]
        second = [case.id for case in self.store.find("Case", limit=4)]
        self.assertEqual(first, second)

    def test_list_events_is_fifo_and_takes_the_earliest_n(self) -> None:
        stamp = utcnow_iso()
        events = []
        for index in range(3):
            event, _ = self.store.publish_event(
                Event(
                    kind="MAIL_RECEIVED",
                    source="imap",
                    source_event_id=f"fifo-{index}",
                    occurred_at=stamp,
                    received_at=stamp,
                )
            )
            events.append(event)
        listed = self.store.list_events(limit=2)
        self.assertEqual([event.id for event in events[:2]], [event.id for event in listed])
        # 同一时间戳的事件也必须按写入顺序（FIFO），且多次调用顺序稳定
        self.assertEqual(
            [event.id for event in events],
            [event.id for event in self.store.list_events(limit=3)],
        )

    def test_audit_trail_takes_the_latest_n(self) -> None:
        case = make_case(self.store)
        for index in range(3):
            case.next_action = f"第 {index} 步"
            self.store.put(case, expected_revision=case.revision, audit_action="UPDATE", reason=f"r{index}")
            case = self.store.get("Case", case.id)
        trail = self.store.audit_trail(object_id=case.id, limit=2)
        self.assertEqual(2, len(trail))
        self.assertIn("r2", [event.reason for event in trail])

    def test_duplicate_event_publish_keeps_object_counts(self) -> None:
        first, created_first = self.store.publish_event(
            Event(kind="MAIL_RECEIVED", source="imap", source_event_id="dup-1", occurred_at=utcnow_iso(), received_at=utcnow_iso())
        )
        replay, created_second = self.store.publish_event(
            Event(kind="MAIL_RECEIVED", source="imap", source_event_id="dup-1", occurred_at=utcnow_iso(), received_at=utcnow_iso())
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.id, replay.id)
        self.assertEqual(1, self.store.stats()["Event"])

    def test_mail_ref_declares_content_origin(self) -> None:
        from asistent.contracts import MailMessageRef

        ref = MailMessageRef(account="a@example.com", folder="INBOX", uid="1")
        self.assertEqual("RAW_BYTES", ref.content_origin)
        projection = MailMessageRef(
            account="a@example.com", folder="INBOX", uid="1", content_origin="METADATA_PROJECTION"
        )
        self.assertEqual("METADATA_PROJECTION", projection.content_origin)

    def test_documents_and_archive_entries_carry_content_origin(self) -> None:
        from asistent.contracts import ArchiveEntry

        document = make_document(self.store)
        self.assertIsNone(self.store.get("SourceDocument", document.id).content_origin)
        entry = ArchiveEntry(relative_path="a/b.txt", sha256="0" * 64, size_bytes=3, content_origin="METADATA_PROJECTION")
        self.assertEqual("METADATA_PROJECTION", entry.to_dict()["content_origin"])
        self.assertEqual("METADATA_PROJECTION", ArchiveEntry.from_dict(entry.to_dict()).content_origin)

    def test_cursor_position_is_an_optional_capability(self) -> None:
        from asistent.contracts import FixtureMailSource, MailSource

        class MinimalSource:
            """只有协议必需成员的实现，必须仍然满足 MailSource。"""

            name = "minimal"

            def list_folders(self):
                return ["INBOX"]

            def list_new(self, cursor):
                return [], cursor or "0"

            def fetch_raw(self, ref):
                return b""

        self.assertIsInstance(MinimalSource(), MailSource)
        self.assertIsNone(getattr(MinimalSource(), "cursor_position", None))
        fixture = FixtureMailSource("fixtures/does-not-exist")
        self.assertIsInstance(fixture, MailSource)


class StoreV05ContractTest(unittest.TestCase):
    """契约 v0.5（CP-20261010-12）：`Event` 的访问路径。

    `Event` 不走通用 `get`/`put`，而走事件接口（publish/claim/mark/list）。
    两个实现必须**一致地**拒绝通用 get —— 否则后人"顺手补一下"就会造成后端分叉，
    或者绕过 `claim_event` 的原子领取语义。
    """

    def setUp(self) -> None:
        self.store = store_factory()

    def test_event_is_not_reachable_through_generic_get(self) -> None:
        event, created = self.store.publish_event(
            Event(
                kind="MAIL_RECEIVED",
                source="imap",
                source_event_id="acc-1",
                occurred_at=utcnow_iso(),
                received_at=utcnow_iso(),
            )
        )
        self.assertTrue(created)
        with self.assertRaises(NotFound):
            self.store.get("Event", event.id)
        self.assertIn(event.id, [item.id for item in self.store.list_events(limit=None)])


class StorageContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = store_factory()

    # ---------------------------------------------------------------- CAS / revision
    def test_create_assigns_revision_one(self) -> None:
        case = make_case(self.store)
        self.assertTrue(case.id.startswith("case_"))
        self.assertEqual(1, case.revision)
        self.assertTrue(case.created_at)
        self.assertEqual(case.created_at, case.updated_at)

    def test_stale_update_is_rejected(self) -> None:
        case = make_case(self.store)
        first = self.store.get("Case", case.id)
        second = self.store.get("Case", case.id)
        first.goal = "第一次修改"
        self.store.put(first, expected_revision=1, reason="并发写入 1")
        second.goal = "第二次修改（基于旧版本）"
        with self.assertRaises(RevisionConflict) as ctx:
            self.store.put(second, expected_revision=1, reason="并发写入 2")
        self.assertEqual(1, ctx.exception.detail["expected"])
        self.assertEqual(2, ctx.exception.detail["actual"])
        self.assertEqual("第一次修改", self.store.get("Case", case.id).goal)

    def test_update_without_expected_revision_is_rejected(self) -> None:
        case = make_case(self.store)
        case.goal = "静默覆盖尝试"
        with self.assertRaises(ValidationError):
            self.store.put(case)

    # ---------------------------------------------------------------- 幂等
    def test_unique_key_prevents_duplicate_object(self) -> None:
        first = make_document(self.store, identity="mail-abc")
        with self.assertRaises(DuplicateObject) as ctx:
            make_document(self.store, identity="mail-abc")
        self.assertEqual(first.id, ctx.exception.detail["existing_id"])
        self.assertEqual(1, len(self.store.find("SourceDocument")))

    def test_event_dedupe_on_repeated_delivery(self) -> None:
        event = Event(
            kind="MAIL_RECEIVED",
            source="imap:owner@example.com",
            source_event_id="uid-42",
            occurred_at=utcnow_iso(),
            received_at=utcnow_iso(),
            payload={"subject": "通知"},
        )
        first, created_first = self.store.publish_event(event)
        replay = Event(
            kind="MAIL_RECEIVED",
            source="imap:owner@example.com",
            source_event_id="uid-42",
            occurred_at=utcnow_iso(),
            received_at=utcnow_iso(),
            payload={"subject": "通知（重复投递）"},
        )
        second, created_second = self.store.publish_event(replay)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.id, second.id)
        self.assertEqual(1, len(self.store.list_events()))

    def test_event_claim_is_exclusive(self) -> None:
        event, _ = self.store.publish_event(
            Event(kind="MAIL_RECEIVED", source="s", source_event_id="e-1", occurred_at=utcnow_iso(), received_at=utcnow_iso())
        )
        self.assertTrue(self.store.claim_event(event.id, worker="worker-a"))
        self.assertFalse(self.store.claim_event(event.id, worker="worker-b"))
        self.store.mark_event_processed(event.id, result="ok")
        self.assertFalse(self.store.claim_event(event.id, worker="worker-c"))

    # ---------------------------------------------------------------- 审计
    def test_audit_is_append_only(self) -> None:
        case = make_case(self.store)
        before = len(self.store.audit_trail(object_id=case.id))
        self.store.transition("Case", case.id, "TRIAGED", actor_kind=ActorKind.AGENT, actor_id="agent", reason="分类完成")
        after = self.store.audit_trail(object_id=case.id)
        self.assertEqual(before + 1, len(after))
        self.assertEqual("TRANSITION", str(after[-1].action))
        self.assertEqual("TRIAGED", after[-1].detail["to"])
        # 更正以新事件表达，而不是覆盖旧记录
        correction = self.store.append_audit(
            AuditEvent(
                action="CORRECTION",
                actor_kind="USER",
                actor_id="owner",
                object_type="Case",
                object_id=case.id,
                reason="修正分类原因",
                voids_event_id=after[-1].id,
            )
        )
        self.assertTrue(correction.id)
        self.assertEqual(len(after) + 1, len(self.store.audit_trail(object_id=case.id)))

    def test_transition_records_before_and_after_revision(self) -> None:
        case = make_case(self.store)
        self.store.transition("Case", case.id, "TRIAGED", actor_kind="AGENT", actor_id="agent")
        trail = self.store.audit_trail(object_id=case.id)
        self.assertEqual(1, trail[-1].version_before)
        self.assertEqual(2, trail[-1].version_after)

    def test_transition_emits_state_event_for_recovery(self) -> None:
        case = make_case(self.store)
        self.store.transition("Case", case.id, "TRIAGED", actor_kind="AGENT", actor_id="agent")
        events = [e for e in self.store.list_events() if e.kind == "STATE_CHANGED"]
        self.assertEqual(1, len(events))
        self.assertEqual(case.id, events[0].payload["object_id"])

    # ---------------------------------------------------------------- 状态守卫
    def test_illegal_state_transition_is_rejected(self) -> None:
        case = make_case(self.store)
        with self.assertRaises(IllegalTransition):
            self.store.transition("Case", case.id, "EXECUTING", actor_kind="AGENT", actor_id="agent")
        self.assertEqual("NEW", str(self.store.get("Case", case.id).state))

    def test_case_cannot_be_cancelled_after_irreversible_action(self) -> None:
        """取消/阻塞只允许发生在尚无不可逆外部动作的阶段（SPEC §21.1）。"""
        case = make_case(self.store)
        self.store.transition("Case", case.id, "TRIAGED", actor_kind="AGENT", actor_id="agent")
        action, _approval, _summary = make_action(self.store, case=case)
        self.store.claim_action(action.id, worker="executor-1")
        with self.assertRaises(IllegalTransition) as ctx:
            self.store.transition("Case", case.id, "CANCELLED", actor_kind="USER", actor_id="owner")
        self.assertIn("不可逆", ctx.exception.message)
        self.assertEqual("TRIAGED", str(self.store.get("Case", case.id).state))

    def test_case_cannot_skip_to_executing(self) -> None:
        case = make_case(self.store)
        with self.assertRaises(IllegalTransition) as ctx:
            self.store.transition("Case", case.id, "EXECUTING", actor_kind="AGENT", actor_id="agent")
        self.assertIn("EXECUTING", ctx.exception.message)

    # ---------------------------------------------------------------- 索引状态
    def test_index_state_machine_and_failure_marking(self) -> None:
        doc = make_document(self.store, identity="mail-index-1")
        self.store.set_index_state(doc.id, "INDEXED", reason="首次索引")
        self.assertEqual("INDEXED", str(self.store.get("SourceDocument", doc.id).index_state))
        self.store.set_index_state(doc.id, "STALE", reason="资料被修改")
        self.store.set_index_state(doc.id, "FAILED", reason="解析失败", error="boom")
        failed = self.store.get("SourceDocument", doc.id)
        self.assertEqual("boom", failed.index_error)
        with self.assertRaises(IllegalTransition):
            self.store.set_index_state(doc.id, "STALE", reason="非法回退")  # FAILED→STALE 不在表中

    # ---------------------------------------------------------------- 恢复
    def test_recovery_scan_finds_everything_that_needs_attention(self) -> None:
        case = make_case(self.store)
        doc = make_document(self.store, identity="mail-recovery-1")
        self.store.set_index_state(doc.id, "FAILED", reason="解析失败", error="bad encoding")
        action, _approval, _summary = make_action(self.store, case=case)
        self.store.claim_action(action.id, worker="executor-9")

        scan = self.store.recovery_scan()
        self.assertIn(case.id, scan.open_case_ids)
        self.assertIn(action.id, scan.in_flight_action_ids)
        self.assertIn(doc.id, scan.pending_index_document_ids)

    def test_in_flight_demoted_to_unknown_not_retried(self) -> None:
        case = make_case(self.store)
        action, _approval, _summary = make_action(self.store, case=case)
        self.store.claim_action(action.id, worker="executor-1")
        demoted = self.store.demote_in_flight_to_unknown(reason="进程重启")
        self.assertEqual([action.id], demoted)
        self.assertEqual("UNKNOWN", str(self.store.get("ExternalAction", action.id).state))
        scan = self.store.recovery_scan()
        self.assertIn(action.id, scan.unknown_action_ids)
        self.assertNotIn(action.id, scan.in_flight_action_ids)

    def test_same_writer_twice_keeps_single_object(self) -> None:
        """同一幂等事件重复处理两次，只能有一个 Case。"""
        event, _ = self.store.publish_event(
            Event(kind="MAIL_RECEIVED", source="imap", source_event_id="uid-7", occurred_at=utcnow_iso(), received_at=utcnow_iso())
        )
        def handle() -> str:
            if not self.store.claim_event(event.id, worker="w"):
                return "skipped"
            existing = self.store.get_by_unique("case_trigger", "uid-7")
            if existing is not None:
                self.store.mark_event_processed(event.id, result="duplicate")
                return "duplicate"
            case = Case(id="", goal="处理 uid-7", trigger_event_id=event.id)
            self.store.put(case, unique=("case_trigger", "uid-7"))
            self.store.mark_event_processed(event.id, result="created")
            return "created"

        first = handle()
        second = handle()
        self.assertEqual("created", first)
        self.assertIn(second, ("skipped", "duplicate"))
        self.assertEqual(1, len(self.store.find("Case")))

    def test_parallel_handlers_create_single_case(self) -> None:
        events = [
            self.store.publish_event(
                Event(kind="MAIL_RECEIVED", source="imap", source_event_id="uid-99", occurred_at=utcnow_iso(), received_at=utcnow_iso())
            )
        ]
        event = events[0][0]
        barrier = threading.Barrier(4)
        results = []

        def worker(index: int) -> None:
            barrier.wait()
            try:
                case = Case(id="", goal=f"worker-{index}")
                self.store.put(case, unique=("case_trigger", "uid-99"))
                results.append(("created", case.id))
            except DuplicateObject:
                results.append(("duplicate", None))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        created = [r for r in results if r[0] == "created"]
        self.assertEqual(1, len(created), f"只允许一个 Case 被创建，实际: {results}")
        self.assertEqual(1, len(self.store.find("Case")))
        self.assertTrue(event.id)


class StoreV06ContractTest(unittest.TestCase):
    """契约 v0.6 增补（CP-20261010-13/15/16）。

    这三条的共同点是"实现看起来是对的，但契约本身在骗人"：
    - `changed_fields` 恒为空：调用方读到"没有字段变化"，而真实情况恰恰是内容变了；
    - 同一动作允许留多条 ACTIVE 审批：授权来源有歧义（执行侧兜住 ≠ 契约允许）；
    - 审批默认有效期只藏在实现里：签名看不出"审批 24 小时后失效"。
    """

    def setUp(self) -> None:
        self.store = store_factory()

    # ---- CP-20261010-13：变化字段必须真的给得出来 ----
    @staticmethod
    def _summary(*, body: str = "您好，我确认参加。", bcc=None) -> ActionSummary:
        return ActionSummary(
            kind="send_email",
            from_account="owner@example.com",
            to=["organizer@example.org"],
            bcc=bcc or [],
            subject="Re: 活动通知",
            body=body,
            draft_version_id="dv_0001",
            target_thread_ref="thread-1",
            evidence_versions={"policy": "rule-1@v1"},
        )

    def test_changed_fields_are_reported_when_approved_summary_is_given(self) -> None:
        approved = self._summary()
        current = self._summary(body="您好，我确认参加，并且带一位同事。", bcc=["shadow@example.com"])
        with self.assertRaises(ApprovalInvalid) as ctx:
            check_approval_valid(
                approval_state="ACTIVE",
                approved_digest=approved.digest(),
                current_summary=current,
                expires_at=None,
                approved_summary=approved,
            )
        detail = ctx.exception.detail
        self.assertEqual(
            ["bcc", "body"],
            detail["changed_fields"],
            "必须给出到底是哪几个字段变了（旧实现恒为 []，会把'内容变了'说成'没有变化'）",
        )
        self.assertNotEqual(detail["approved_digest"], detail["current_digest"])

    def test_changed_fields_is_none_not_empty_list_when_summary_missing(self) -> None:
        approved = self._summary()
        current = self._summary(body="完全不同的正文")
        with self.assertRaises(ApprovalInvalid) as ctx:
            check_approval_valid(
                approval_state="ACTIVE",
                approved_digest=approved.digest(),
                current_summary=current,
                expires_at=None,
            )
        self.assertIsNone(
            ctx.exception.detail["changed_fields"],
            "拿不到获批摘要时只能是'未知'（None），绝不能用空列表冒充'没有字段变化'",
        )

    def test_unchanged_summary_passes_check(self) -> None:
        summary = self._summary()
        check_approval_valid(
            approval_state="ACTIVE",
            approved_digest=summary.digest(),
            current_summary=self._summary(),
            expires_at=None,
            approved_summary=summary,
        )

    # ---- CP-20261010-15：同一动作至多一个 ACTIVE 审批 ----
    def test_second_approval_supersedes_the_first_active_one(self) -> None:
        case = make_case(self.store)
        action, first, summary = make_action(self.store, case=case)
        self.assertEqual("ACTIVE", str(self.store.get("Approval", first.id).state))

        second = self.store.approve(
            Approval(
                case_id=case.id,
                action_id=action.id,
                approver="owner",
                approved_action_digest=summary.digest(),
                target_object_ids=[action.id],
                created_at_approval=utcnow_iso(),
                expires_at=add_seconds(utcnow_iso(), 3600),
                auth_channel="console",
                auth_evidence="console-session:test-2",
                summary_shown=summary.normalized(),
            )
        )

        superseded = self.store.get("Approval", first.id)
        self.assertEqual("INVALIDATED", str(superseded.state), "旧审批必须被取代，而不是并列存在")
        self.assertIn("新的有效审批", superseded.invalidated_reason or "")
        self.assertNotEqual(
            superseded.revision,
            1,
            "取代动作必须真正落盘（revision 增长），不能只是改了内存对象",
        )
        active = [
            item.id
            for item in self.store.find("Approval", where={"action_id": action.id}, limit=None)
            if str(item.state) == "ACTIVE"
        ]
        self.assertEqual([second.id], active, f"同一动作只能有一个 ACTIVE 审批，实际: {active}")
        # 被取代的审批不能拿来执行
        self.assertNotEqual(str(self.store.get("ExternalAction", action.id).state), "SUCCEEDED")

    def test_consumed_approval_does_not_block_reapproval(self) -> None:
        """重试路径不能被这条规则误伤：已 CONSUMED 的旧审批不算"竞争"。"""
        case = make_case(self.store)
        action, first, summary = make_action(self.store, case=case)
        self.store.claim_action(action.id, worker="executor-1")
        self.assertEqual("CONSUMED", str(self.store.get("Approval", first.id).state))

        second = self.store.approve(
            Approval(
                case_id=case.id,
                action_id=action.id,
                approver="owner",
                approved_action_digest=summary.digest(),
                target_object_ids=[action.id],
                created_at_approval=utcnow_iso(),
                expires_at=add_seconds(utcnow_iso(), 3600),
                auth_channel="console",
                auth_evidence="console-session:test-3",
                summary_shown=summary.normalized(),
            )
        )
        self.assertEqual("CONSUMED", str(self.store.get("Approval", first.id).state), "已消费的审批不该被改写")
        self.assertEqual("ACTIVE", str(second.state))

    def test_approval_of_another_action_is_untouched(self) -> None:
        case = make_case(self.store)
        action_a, approval_a, _summary_a = make_action(self.store, case=case)
        action_b, no_approval, summary_b = make_action(
            self.store, case=case, subject="Re: 另一个通知", approve=False
        )
        self.assertIsNone(no_approval)
        third = self.store.approve(
            Approval(
                case_id=case.id,
                action_id=action_b.id,
                approver="owner",
                approved_action_digest=summary_b.digest(),
                target_object_ids=[action_b.id],
                created_at_approval=utcnow_iso(),
                expires_at=add_seconds(utcnow_iso(), 3600),
                auth_channel="console",
                auth_evidence="console-session:test-4",
                summary_shown=summary_b.normalized(),
            )
        )
        self.assertEqual(
            "ACTIVE",
            str(self.store.get("Approval", approval_a.id).state),
            "别的动作的审批不许被牵连（取代只针对同一 action_id）",
        )
        self.assertEqual(action_a.id, self.store.get("Approval", approval_a.id).action_id)
        self.assertEqual("ACTIVE", str(third.state))

    # ---- CP-20261010-16：默认 TTL 是契约的一部分 ----
    def test_default_ttl_comes_from_contract_constant(self) -> None:
        case = make_case(self.store)
        action, _approval, summary = make_action(self.store, case=case)
        saved = self.store.approve(
            Approval(
                case_id=case.id,
                action_id=action.id,
                approver="owner",
                approved_action_digest=summary.digest(),
                auth_channel="console",
                auth_evidence="console-session:test-5",
                summary_shown=summary.normalized(),
                # expires_at 故意不给：默认 TTL 由契约常量决定，而不是各实现各自硬编码
            )
        )
        self.assertEqual(
            add_seconds(saved.created_at_approval, DEFAULT_APPROVAL_TTL_SECONDS),
            saved.expires_at,
        )

    def test_explicit_expiry_is_respected(self) -> None:
        case = make_case(self.store)
        action, _approval, summary = make_action(self.store, case=case)
        explicit = add_seconds(utcnow_iso(), 90)
        saved = self.store.approve(
            Approval(
                case_id=case.id,
                action_id=action.id,
                approver="owner",
                approved_action_digest=summary.digest(),
                created_at_approval=utcnow_iso(),
                expires_at=explicit,
                auth_channel="console",
                auth_evidence="console-session:test-6",
                summary_shown=summary.normalized(),
            )
        )
        self.assertEqual(explicit, saved.expires_at, "显式有效期必须被尊重，不能被默认 TTL 覆盖")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
