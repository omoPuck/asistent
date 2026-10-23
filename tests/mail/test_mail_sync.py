"""MVP-2 同步循环测试：游标恢复、幂等去重、游标失效重建、失败状态化（AT-05）。"""

from __future__ import annotations

import json
import os
import unittest

from asistent.contracts.errors import NotFound
from asistent.contracts.mail import FixtureMailSource, RecordingMailSender
from asistent.mail import (
    ChannelUnavailable,
    CursorInvalid,
    MailSyncEngine,
    RawStore,
    SyncStateStore,
)
from asistent.mail.sync import IDENTITY_QUARANTINED
from tests.mail.fakes import (
    CrashInjectingStore,
    CrashOnSaveStateStore,
    IdentityLyingSource,
    MemoryMailSource,
    MinimalMailSource,
    SimulatedCrash,
    TempWorkspace,
    audit_rows,
    broken_encoding_eml,
    counts,
    make_eml,
)


class MailSyncTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = TempWorkspace()
        self.addCleanup(self.ws.cleanup)

    def make_store(self, **kwargs):
        return CrashInjectingStore(**kwargs)

    def make_engine(self, store, source, *, state=None, raw_root=None, **kwargs) -> MailSyncEngine:
        state_store = state or SyncStateStore(self.ws.state_path)
        raw_store = RawStore(raw_root or self.ws.raw_root)
        return MailSyncEngine(
            store,
            source,
            account=getattr(source, "account", "owner@example.com"),
            folder=getattr(source, "folder", "INBOX"),
            state_store=state_store,
            raw_store=raw_store,
            **kwargs,
        )


def object_counts(store) -> dict:
    """对象计数（不含审计：审计按设计允许追加新的尝试记录）。"""
    snapshot = counts(store)
    snapshot.pop("AuditEvent", None)
    return snapshot


class IdempotencyTest(MailSyncTestBase):
    def test_first_sync_creates_one_document_case_conversation_and_event(self) -> None:
        store = self.make_store()
        raw = make_eml(subject="通知 A", message_id="<a@example.org>")
        source = MemoryMailSource([raw])
        engine = self.make_engine(store, source)
        report = engine.sync_once()

        self.assertTrue(report.complete, report.summary_line())
        self.assertEqual(1, report.documents_created)
        self.assertEqual(1, report.cases_created)
        self.assertEqual(1, report.conversations_created)
        self.assertEqual(1, report.events_published)
        self.assertEqual(0, report.duplicates_skipped)
        self.assertTrue(report.cursor_changed)

        documents = store.find("SourceDocument")
        self.assertEqual(1, len(documents))
        document = documents[0]
        self.assertEqual("email", document.source_type)
        self.assertEqual("INBOX:uid:1", document.source_id)
        self.assertEqual("INBOX", document.mailbox_folder)
        self.assertTrue(document.origin_path.endswith(".eml"))
        self.assertTrue(os.path.exists(document.origin_path))
        self.assertEqual(len(raw), os.path.getsize(document.origin_path))
        self.assertEqual(1, document.content_version)

        cases = store.find("Case")
        self.assertEqual(1, len(cases))
        self.assertEqual([document.id], cases[0].source_document_ids)
        self.assertEqual(document.case_id, cases[0].id)
        self.assertTrue(cases[0].goal.startswith("处理邮件："))

        conversations = store.find("Conversation")
        self.assertEqual(1, len(conversations))
        self.assertEqual("email_thread", conversations[0].channel)
        self.assertEqual([document.id], conversations[0].source_document_ids)

        events = store.list_events()
        self.assertEqual(1, len(events))
        self.assertEqual("MAIL_RECEIVED", events[0].kind)
        self.assertEqual(str(document.identity_key), events[0].source_event_id)
        self.assertEqual(cases[0].id, events[0].case_id)
        self.assertIsNotNone(events[0].processed_at)
        self.assertEqual("PROCESSED", events[0].processing_result)

    def test_duplicate_delivery_keeps_object_case_and_audit_counts_identical(self) -> None:
        """验收：同一封邮件投递两次后对象数/Case 数/审计条数不变。"""
        store = self.make_store()
        source = MemoryMailSource(
            [
                make_eml(subject="通知 A", message_id="<a@example.org>"),
                make_eml(subject="通知 B", message_id="<b@example.org>", body="第二封"),
            ],
            redeliver_all=True,
        )
        engine = self.make_engine(store, source)

        first = engine.sync_once()
        after_first = counts(store)
        audit_after_first = len(audit_rows(store))
        documents_after_first = {item.id for item in store.find("SourceDocument")}
        cases_after_first = {item.id for item in store.find("Case")}
        print("[evidence] run1 =", first.summary_line())
        print("[evidence] run1 counts =", json.dumps(after_first, sort_keys=True))

        second = engine.sync_once()
        after_second = counts(store)
        print("[evidence] run2 =", second.summary_line())
        print("[evidence] run2 counts =", json.dumps(after_second, sort_keys=True))

        self.assertEqual(2, second.listed)
        self.assertEqual(0, second.documents_created)
        self.assertEqual(0, second.cases_created)
        self.assertEqual(0, second.conversations_created)
        self.assertEqual(2, second.duplicates_skipped)
        self.assertEqual(0, second.events_published)
        self.assertEqual(2, second.events_duplicate)
        self.assertEqual(after_first, after_second)
        self.assertEqual(audit_after_first, len(audit_rows(store)))
        self.assertEqual(documents_after_first, {item.id for item in store.find("SourceDocument")})
        self.assertEqual(cases_after_first, {item.id for item in store.find("Case")})

    def test_crash_between_object_writes_replays_without_orphan(self) -> None:
        """模拟「原始对象已写、Case 未写」时进程崩溃，重放必须补齐且不重复。"""
        store = CrashInjectingStore(crash_after_puts=1)
        source = MemoryMailSource([make_eml(subject="崩溃重放", message_id="<crash-1@example.org>")])
        engine = self.make_engine(store, source)
        with self.assertRaises(SimulatedCrash):
            engine.sync_once()
        self.assertEqual(1, len(store.find("SourceDocument")))
        self.assertEqual(0, len(store.find("Case")))

        store.crash_after_puts = None  # 真实重启不会带着注入的故障
        engine2 = self.make_engine(store, MemoryMailSource([source.raws[0]]))
        report = engine2.sync_once()
        print("[evidence] crash-replay =", report.summary_line())
        self.assertEqual(0, report.documents_created)
        self.assertEqual(1, report.duplicates_skipped)
        self.assertEqual(1, report.cases_created)
        self.assertEqual(1, len(store.find("SourceDocument")))
        self.assertEqual(1, len(store.find("Case")))
        self.assertEqual(1, len(store.find("Conversation")))
        document = store.find("SourceDocument")[0]
        case = store.find("Case")[0]
        self.assertEqual(case.id, document.case_id)
        self.assertEqual([document.id], case.source_document_ids)

    def test_crash_before_cursor_save_replays_without_duplicates(self) -> None:
        """模拟「对象都写完了、游标还没落盘」时崩溃，重放不得产生第二份对象。"""
        store = self.make_store()
        source = MemoryMailSource([make_eml(subject="游标前崩溃", message_id="<crash-2@example.org>")])
        crashing_state = CrashOnSaveStateStore(self.ws.state_path, crash_after_saves=0)
        engine = self.make_engine(store, source, state=crashing_state)
        with self.assertRaises(SimulatedCrash):
            engine.sync_once()
        object_counts_after_crash = object_counts(store)
        self.assertEqual(1, len(store.find("SourceDocument")))
        self.assertEqual(1, len(store.find("Case")))

        engine2 = self.make_engine(store, source)
        report = engine2.sync_once()
        print("[evidence] cursor-save-crash replay =", report.summary_line())
        self.assertEqual(0, report.documents_created)
        self.assertEqual(0, report.cases_created)
        self.assertEqual(object_counts_after_crash, object_counts(store))  # 对象不重复
        self.assertEqual(1, len(store.find("SourceDocument")))
        self.assertEqual(1, len(store.find("Case")))
        self.assertEqual("1", report.cursor_after)

    def test_content_change_under_same_identity_registers_new_content_version(self) -> None:
        """同一身份下内容摘要变化：登记新内容代次，不覆盖旧原文，也不新建对象。"""
        store = self.make_store()
        raw_v1 = make_eml(subject="内容冲突", message_id="<conflict@example.org>", body="原内容")
        raw_v2 = make_eml(subject="内容冲突", message_id="<conflict@example.org>", body="被改写的内容")
        source = IdentityLyingSource(raw_v1, raw_v2, redeliver_all=True)
        engine = self.make_engine(store, source)
        engine.sync_once()
        document = store.find("SourceDocument")[0]
        original_path = document.origin_path
        self.assertEqual(1, document.content_version)

        source.switch = True
        report = engine.sync_once()
        print("[evidence] content-version bump =", report.summary_line())
        self.assertEqual(1, report.documents_updated)
        self.assertEqual(0, report.documents_created)
        self.assertEqual(1, len(store.find("SourceDocument")))
        self.assertEqual(1, len(store.find("Case")))
        fresh = store.find("SourceDocument")[0]
        self.assertEqual(2, fresh.content_version)
        self.assertNotEqual(original_path, fresh.origin_path)
        self.assertTrue(os.path.exists(original_path))  # 旧原文不覆盖


class CursorTest(MailSyncTestBase):
    def test_cursor_resume_only_takes_new_messages(self) -> None:
        store = self.make_store()
        raws = [make_eml(subject="M%d" % index, message_id="<m%d@example.org>" % index) for index in range(1, 3)]
        source = MemoryMailSource(raws)
        engine = self.make_engine(store, source)
        first = engine.sync_once()
        self.assertEqual(2, first.documents_created)
        print("[evidence] cursor run1 =", first.summary_line())

        source.raws.append(make_eml(subject="M3", message_id="<m3@example.org>"))
        second = engine.sync_once()
        print("[evidence] cursor run2 =", second.summary_line())
        self.assertEqual(1, second.listed)
        self.assertEqual(1, second.documents_created)
        self.assertEqual(3, len(store.find("SourceDocument")))
        self.assertEqual([None, "2"], [call for call in source.list_calls])  # 首轮无游标，次轮从"2"续传
        self.assertEqual("3", second.cursor_after)

    def test_channel_interruption_does_not_advance_cursor_and_replay_is_idempotent(self) -> None:
        store = self.make_store()
        raws = [
            make_eml(subject="M1", message_id="<n1@example.org>"),
            make_eml(subject="M2", message_id="<n2@example.org>"),
        ]
        source = MemoryMailSource(raws)
        source.interrupt_on_list_call = (1, ChannelUnavailable("模拟 IMAP 连接中断"))
        engine = self.make_engine(store, source)
        report = engine.sync_once()
        print("[evidence] interrupted run =", report.summary_line())
        self.assertFalse(report.complete)
        self.assertIsNone(report.cursor_after)
        self.assertEqual(0, len(store.find("SourceDocument")))
        self.assertEqual(1, len(report.failures))
        self.assertEqual("list", report.failures[0].stage)

        source.interrupt_on_list_call = None
        resumed = engine.sync_once()
        print("[evidence] resumed run =", resumed.summary_line())
        self.assertEqual(2, resumed.documents_created)
        self.assertEqual("2", resumed.cursor_after)
        self.assertEqual(2, len(store.find("SourceDocument")))
        self.assertEqual(2, len(store.find("Case")))

    def test_failed_fetch_keeps_cursor_and_recovers_on_retry(self) -> None:
        store = self.make_store()
        raws = [
            make_eml(subject="Good", message_id="<g@example.org>"),
            make_eml(subject="Flaky", message_id="<f@example.org>"),
        ]
        source = MemoryMailSource(raws)
        source.fail_fetch_uids["uid:2"] = ChannelUnavailable("模拟取正文超时")
        engine = self.make_engine(store, source)
        report = engine.sync_once()
        print("[evidence] fetch-failure run =", report.summary_line())
        self.assertFalse(report.complete)
        self.assertIsNone(report.cursor_after)
        self.assertEqual(1, len(store.find("SourceDocument")))
        self.assertEqual(1, len(report.failures))
        self.assertEqual("fetch", report.failures[0].stage)
        self.assertFalse(report.failures[0].terminal)

        source.fail_fetch_uids.clear()
        resumed = engine.sync_once()
        print("[evidence] fetch-failure retry =", resumed.summary_line())
        self.assertTrue(resumed.complete)
        self.assertEqual(1, resumed.documents_created)
        self.assertEqual(1, resumed.duplicates_skipped)
        self.assertEqual(2, len(store.find("SourceDocument")))
        self.assertEqual("2", resumed.cursor_after)

    def test_corrupt_state_file_triggers_detection_and_rebuild(self) -> None:
        store = self.make_store()
        source = MemoryMailSource([make_eml(subject="R", message_id="<rebuild@example.org>")])
        engine = self.make_engine(store, source)
        engine.sync_once()
        objects_before = object_counts(store)
        audit_before = len(audit_rows(store))
        documents_before = {item.id for item in store.find("SourceDocument")}
        cases_before = {item.id for item in store.find("Case")}

        with open(self.ws.state_path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json ")
        fresh_state = SyncStateStore(self.ws.state_path)
        engine2 = self.make_engine(store, source, state=fresh_state)
        report = engine2.sync_once()
        print("[evidence] corrupt-state rebuild =", json.dumps(report.to_dict(), ensure_ascii=False))
        self.assertTrue(report.cursor_invalidated)
        self.assertIn("不可解析", report.invalid_reason)
        self.assertEqual(1, report.epoch)
        self.assertIsNone(source.list_calls[-1])  # 从头列取
        self.assertEqual(objects_before, object_counts(store))  # 重建不产生新对象
        self.assertEqual(documents_before, {item.id for item in store.find("SourceDocument")})
        self.assertEqual(cases_before, {item.id for item in store.find("Case")})
        self.assertEqual(0, report.documents_created)
        self.assertEqual(1, report.duplicates_skipped)
        rebuild_rows = [row for row in audit_rows(store) if row.object_type == "MailSyncCursor"]
        # run1 的正常推进 + 本轮 1 条 FAILED 失效 + 1 条重建后推进
        self.assertEqual(3, len(rebuild_rows))
        failed_rows = [row for row in rebuild_rows if row.result == "FAILED"]
        self.assertEqual(1, len(failed_rows))
        self.assertIn("游标失效", failed_rows[0].reason)
        self.assertEqual("游标重建后推进", rebuild_rows[-1].reason)
        self.assertEqual("1", rebuild_rows[-1].detail.get("cursor_after"))

    def test_source_cursor_invalid_exception_triggers_rebuild(self) -> None:
        store = self.make_store()
        source = MemoryMailSource([make_eml(subject="R", message_id="<rebuild-2@example.org>")])
        engine = self.make_engine(store, source)
        first = engine.sync_once()
        self.assertEqual("1", first.cursor_after)

        state = SyncStateStore(self.ws.state_path)
        state.load()
        state.set_cursor(source.account, source.folder, "3:v0|stale-cursor")
        source.invalid_cursors.add("3:v0|stale-cursor")
        engine2 = self.make_engine(store, source, state=SyncStateStore(self.ws.state_path))
        report = engine2.sync_once()
        print("[evidence] source-side cursor invalid =", json.dumps(report.to_dict(), ensure_ascii=False))
        self.assertTrue(report.cursor_invalidated)
        self.assertIn("游标失效", report.invalid_reason)
        self.assertEqual(2, report.list_attempts)  # 第一次失效 + 第二次重建
        self.assertEqual(0, report.documents_created)
        self.assertEqual(1, report.duplicates_skipped)
        self.assertEqual(1, report.epoch)

    def test_cursor_regression_is_detected_and_rebuilt(self) -> None:
        store = self.make_store()
        raws = [make_eml(subject="M%d" % index, message_id="<reg%d@example.org>" % index) for index in range(1, 4)]
        source = MemoryMailSource(raws)
        engine = self.make_engine(store, source)
        first = engine.sync_once()
        self.assertEqual("3", first.cursor_after)

        source.return_cursor = "1"  # 来源侧回退
        report = engine.sync_once()
        print("[evidence] cursor regression =", json.dumps(report.to_dict(), ensure_ascii=False))
        self.assertTrue(report.cursor_invalidated)
        self.assertIn("回退", report.invalid_reason)
        self.assertIsNone(source.list_calls[-1])
        self.assertEqual(0, report.documents_created)
        self.assertEqual(3, len(store.find("SourceDocument")))

    def test_explicit_rebuild_keeps_state_queryable(self) -> None:
        store = self.make_store()
        source = MemoryMailSource([make_eml(subject="M", message_id="<explicit@example.org>")])
        engine = self.make_engine(store, source)
        engine.sync_once()
        report = engine.rebuild_cursor("人工排查后要求重建")
        self.assertTrue(report.cursor_invalidated)
        self.assertIn("人工排查", report.invalid_reason)
        status = engine.status()
        self.assertEqual(1, status["epoch"])
        self.assertEqual("1", status["cursor"])
        self.assertEqual(1, status["identities"])


class FailureStateTest(MailSyncTestBase):
    def test_repeated_failure_is_quarantined_and_then_skipped(self) -> None:
        store = self.make_store()
        raws = [
            make_eml(subject="Good", message_id="<good@example.org>"),
            make_eml(subject="Poison", message_id="<poison@example.org>"),
        ]
        source = MemoryMailSource(raws, redeliver_all=True)
        source.fail_fetch_uids["uid:2"] = ChannelUnavailable("模拟永久性通道错误")
        engine = self.make_engine(store, source, max_attempts=3)

        reports = [engine.sync_once() for _ in range(3)]
        for index, report in enumerate(reports, start=1):
            print("[evidence] poison run%d = %s" % (index, report.summary_line()))
        self.assertIsNone(reports[0].cursor_after)
        self.assertIsNone(reports[1].cursor_after)
        self.assertEqual("2", reports[2].cursor_after)  # 第 3 次达到阈值 → 隔离并推进
        self.assertTrue(reports[2].failures[0].terminal)
        self.assertEqual(3, reports[2].failures[0].attempt)
        self.assertEqual(1, len(store.find("SourceDocument")))  # 毒邮件始终没入库

        fetches_before = list(source.fetch_calls)
        fourth = engine.sync_once()
        print("[evidence] poison run4 =", fourth.summary_line())
        self.assertEqual(0, fourth.documents_created)
        new_fetches = source.fetch_calls[len(fetches_before):]
        self.assertNotIn("uid:2", new_fetches)  # 已隔离 → 不再取该邮件正文
        self.assertIn("uid:1", new_fetches)  # 正常邮件重新列取时会重新取（并去重）
        self.assertEqual(1, len(fourth.quarantined))

        failures = engine.failures()
        self.assertEqual(1, len(failures))
        self.assertEqual("fetch", failures[0]["stage"])
        self.assertEqual(3, failures[0]["attempt"])
        self.assertTrue(failures[0]["terminal"])
        self.assertIn("NETWORK_FAILURE", failures[0]["failure_class"])
        status = engine.status()
        self.assertEqual(1, len(status["quarantined"]))
        identity = status["quarantined"][0]
        self.assertEqual(IDENTITY_QUARANTINED, engine.state.identity(identity)["state"])

        failed_audit = [row for row in audit_rows(store) if row.result == "FAILED"]
        print("[evidence] failed audit rows =", len(failed_audit))
        self.assertEqual(3, len(failed_audit))
        self.assertEqual("MailSyncFailure", failed_audit[0].object_type)
        self.assertEqual(3, failed_audit[-1].detail.get("attempts"))
        self.assertTrue(failed_audit[-1].detail.get("terminal"))

    def test_quarantine_can_be_released_for_operator_retry(self) -> None:
        store = self.make_store()
        source = MemoryMailSource(
            [make_eml(subject="X", message_id="<rel@example.org>")], redeliver_all=True
        )
        source.fail_fetch_uids["uid:1"] = ChannelUnavailable("模拟通道故障")
        engine = self.make_engine(store, source, max_attempts=1)
        engine.sync_once()
        identity = engine.status()["quarantined"][0]
        self.assertTrue(engine.release_quarantine(identity))
        self.assertEqual([], engine.status()["quarantined"])
        source.fail_fetch_uids.clear()
        report = engine.sync_once()
        self.assertEqual(1, report.documents_created)

    def test_failure_without_identity_is_quarantined_immediately(self) -> None:
        store = self.make_store()
        source = MemoryMailSource([make_eml(subject="NoIdentity", message_id="<noid@example.org>")])

        def strip_digest(ref):
            ref.content_digest = ""
            ref.vendor_message_id = None
            return ref

        original_ref = source._ref

        def broken_ref(index):
            return strip_digest(original_ref(index))

        source._ref = broken_ref  # type: ignore[assignment]
        engine = self.make_engine(store, source)
        report = engine.sync_once()
        print("[evidence] identity-less mail =", report.summary_line())
        self.assertEqual(0, report.documents_created)
        self.assertEqual(1, len(report.failures))
        self.assertEqual("identity", report.failures[0].stage)
        self.assertTrue(report.failures[0].terminal)
        self.assertEqual("1", report.cursor_after)  # 不阻塞整批


class ReadOnlyTest(MailSyncTestBase):
    def test_sync_path_never_sends_and_creates_no_external_action(self) -> None:
        store = self.make_store()
        sender = RecordingMailSender(self.ws.outbox)
        source = MemoryMailSource([make_eml(subject="只读", message_id="<ro@example.org>")])
        engine = self.make_engine(store, source)
        engine.sync_once()
        engine.sync_once()

        self.assertEqual([], sender.sent)
        self.assertEqual([], os.listdir(self.ws.outbox))
        self.assertEqual([], store.find("ExternalAction"))
        self.assertEqual([], store.find("Approval"))
        self.assertFalse(hasattr(engine, "send"))
        self.assertFalse(hasattr(source, "send"))


class EncodingAndSizeTest(MailSyncTestBase):
    def test_oversized_mail_is_stored_intact(self) -> None:
        store = self.make_store()
        payload = "A" * (6 * 1024 * 1024)
        raw = make_eml(subject="超大邮件", message_id="<huge@example.org>", body=payload)
        source = MemoryMailSource([raw])
        engine = self.make_engine(store, source)
        report = engine.sync_once()
        document = store.find("SourceDocument")[0]
        print("[evidence] huge mail = size=%d stored=%d" % (len(raw), os.path.getsize(document.origin_path)))
        self.assertEqual(1, report.documents_created)
        self.assertEqual(len(raw), os.path.getsize(document.origin_path))
        self.assertEqual(len(raw), document.size_bytes)

    def test_broken_encoding_mail_can_be_synced_and_queried(self) -> None:
        store = self.make_store()
        raw = broken_encoding_eml()
        source = MemoryMailSource([raw])
        engine = self.make_engine(store, source)
        report = engine.sync_once()
        print("[evidence] broken encoding =", report.summary_line())
        self.assertEqual(1, report.documents_created)
        documents = store.find("SourceDocument")
        self.assertEqual(1, len(documents))
        # 状态文件必须仍然是合法 JSON（这是异常编码最容易炸的地方）
        with open(self.ws.state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual(1, state["runs"])
        self.assertEqual(1, len(state["identities"]))
        engine.sync_once()
        with open(self.ws.state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual(2, state["runs"])
        self.assertEqual(1, len(state["identities"]))

    def test_attachment_mail_records_attachment_flag(self) -> None:
        store = self.make_store()
        raw = make_eml(
            subject="带附件",
            message_id="<att@example.org>",
            attachments=[("报名表.txt", "内容".encode("utf-8"))],
        )
        source = MemoryMailSource([raw])
        engine = self.make_engine(store, source)
        engine.sync_once()
        event = store.list_events()[0]
        self.assertTrue(event.payload["has_attachments"])
        source_refs, _ = source.list_new(None)
        self.assertTrue(source_refs[0].has_attachments)


class MessageIdTest(MailSyncTestBase):
    def test_missing_message_id_still_produces_identity_and_thread(self) -> None:
        store = self.make_store()
        source = MemoryMailSource(
            [
                make_eml(subject="无 ID 1", message_id=None),
                make_eml(subject="无 ID 2", message_id=None, body="第二封没有 Message-ID"),
            ]
        )
        engine = self.make_engine(store, source)
        report = engine.sync_once()
        print("[evidence] missing message-id =", report.summary_line())
        self.assertEqual(2, report.documents_created)
        self.assertEqual(2, len(store.find("SourceDocument")))
        self.assertEqual(2, len(store.find("Conversation")))  # 不能瞎猜成同一线程
        events = store.list_events()
        self.assertEqual(2, len(events))
        for event in events:
            self.assertIsNone(event.payload["message_id"])
            self.assertEqual("INBOX", event.payload["folder"])
            self.assertEqual(1, len(store.find("SourceDocument", where={"identity_key": event.source_event_id})))


class VendorIdTest(MailSyncTestBase):
    def test_same_vendor_id_across_folders_keeps_single_document(self) -> None:
        """跨文件夹移动：有供应商不可变 ID 时身份不变，仍只有一个原始对象。"""
        store = self.make_store()
        raw = make_eml(subject="移动的邮件", message_id="<move@example.org>")
        source = MemoryMailSource([raw], vendor_ids=["vendor-77"])
        engine = self.make_engine(store, source)
        engine.sync_once()
        first_document = store.find("SourceDocument")[0]

        moved = MemoryMailSource([raw], vendor_ids=["vendor-77"])
        moved.folder = "Archive"
        engine2 = MailSyncEngine(
            store,
            moved,
            account=moved.account,
            folder="Archive",
            state_store=SyncStateStore(self.ws.state_path),
            raw_store=RawStore(self.ws.raw_root),
        )
        report = engine2.sync_once()
        print("[evidence] folder move =", report.summary_line())
        documents = store.find("SourceDocument")
        self.assertEqual(1, len(documents))
        self.assertEqual(0, report.documents_created)
        self.assertEqual(0, report.cases_created)
        self.assertEqual(first_document.identity_key, documents[0].identity_key)
        self.assertEqual("Archive", documents[0].mailbox_folder)
        self.assertEqual("Archive:uid:1", documents[0].source_id)
        self.assertEqual(1, report.documents_updated)


class ContentOriginTest(MailSyncTestBase):
    """契约 v0.4（CP-20261010-07）：摘要来源必须显式声明，不得"看起来通过"。

    - `RAW_BYTES`：IMAP 直连 / 离线夹具给的是原始字节 → 摘要可用于字节级校验；
    - `METADATA_PROJECTION`：MCP 只给元数据 → 摘要只是投影，不能当字节级证明；
    - `None`：来源没声明 → 下游不得当字节级证明（宁可拒绝）。
    """

    def test_fixture_channel_keeps_raw_bytes_origin(self) -> None:
        """离线夹具（契约自带的 `FixtureMailSource`）走原始字节通路：必须保持 RAW_BYTES。"""
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
        source = FixtureMailSource(root, account="fixture@example.com")
        refs, _ = source.list_new(None)
        self.assertTrue(refs)
        self.assertEqual({"RAW_BYTES"}, {ref.content_origin for ref in refs})

        store = self.make_store()
        engine = self.make_engine(store, source)
        report = engine.sync_once()
        print("[evidence] fixture sync =", report.summary_line())
        documents = store.find("SourceDocument")
        self.assertEqual(len(refs), len(documents))
        self.assertEqual({"RAW_BYTES"}, {doc.content_origin for doc in documents})
        print("[evidence] fixture origins =", sorted({doc.content_origin for doc in documents}))

    def test_undeclared_origin_is_recorded_as_none(self) -> None:
        """来源没声明来源语义（空串）→ 文档记 None，绝不能被当成字节级证明。"""
        store = self.make_store()
        raw = make_eml(subject="未声明来源", message_id="<nodecl@example.org>")
        source = MinimallessDeclaredSource([raw])
        engine = self.make_engine(store, source)
        report = engine.sync_once()
        self.assertEqual(1, report.documents_created)
        document = store.find("SourceDocument")[0]
        print("[evidence] undeclared origin =", repr(document.content_origin))
        self.assertIsNone(document.content_origin)
        self.assertTrue(document.content_hash)  # 摘要仍在，但语义未知

    def test_memory_source_carries_its_declared_origin_to_documents(self) -> None:
        """投影型来源：引用上的 METADATA_PROJECTION 必须原样落到文档上（并保持幂等）。"""
        store = self.make_store()
        raw = make_eml(subject="投影来源", message_id="<proj@example.org>")
        source = IdentityLyingSource(raw, raw)
        engine = self.make_engine(store, source)
        engine.sync_once()
        document = store.find("SourceDocument")[0]
        print("[evidence] projection origin =", document.content_origin)
        self.assertEqual("METADATA_PROJECTION", document.content_origin)

        engine.sync_once()  # 重放：不能把声明改回默认值
        self.assertEqual("METADATA_PROJECTION", store.find("SourceDocument")[0].content_origin)


class MinimallessDeclaredSource(MemoryMailSource):
    """模拟"来源没有声明摘要语义"的通道（字段为空串）。"""

    def _ref(self, index: int):
        ref = super()._ref(index)
        ref.content_origin = ""
        return ref


class CursorCapabilityTest(MailSyncTestBase):
    """契约 v0.4（CP-20261010-08）：`cursor_position()` 是可选观测能力，不是数值比较。"""

    def test_engine_exposes_source_position_only_when_available(self) -> None:
        store = self.make_store()
        raw = make_eml(subject="可观测位置", message_id="<pos@example.org>")
        source = MemoryMailSource([raw])
        # 内存夹具刻意**不**提供 cursor_position()（只提供实现级的 cursor_rank）
        self.assertFalse(hasattr(source, "cursor_position"))
        engine = self.make_engine(store, source)
        engine.sync_once()
        self.assertIsNone(engine.source_position())
        self.assertIsNone(engine.status()["source_position"])

    def test_minimal_source_satisfies_protocol_and_syncs(self) -> None:
        """只实现必需成员的来源仍满足 MailSource；缺可选能力时降级而不是报错。"""
        from asistent.contracts.mail import MailSource

        raws = [make_eml(subject="最小来源", message_id="<min@example.org>")]
        minimal = MinimalMailSource(raws)
        self.assertIsInstance(minimal, MailSource)
        store = self.make_store()
        engine = self.make_engine(store, minimal)
        report = engine.sync_once()
        print("[evidence] minimal source sync =", report.summary_line())
        self.assertTrue(report.complete)
        self.assertEqual(1, report.documents_created)
        self.assertEqual([], engine.failures())
        self.assertIsNone(engine.status()["source_position"])

        # 没有 cursor_rank：游标"回退"不可比较 → 不做失效重建，仍然幂等
        engine2 = self.make_engine(store, MinimalMailSource([]), state=SyncStateStore(self.ws.state_path))
        report2 = engine2.sync_once()
        print("[evidence] minimal empty sync =", report2.summary_line())
        self.assertFalse(report2.cursor_invalidated)

    def test_status_reports_source_position_when_source_provides_it(self) -> None:
        class ObservableSource(MemoryMailSource):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.position = None

            def cursor_position(self):
                return dict(self.position) if self.position else None

        store = self.make_store()
        raw = make_eml(subject="观测位置", message_id="<obs@example.org>")
        source = ObservableSource([raw])
        engine = self.make_engine(store, source)
        report = engine.sync_once()
        self.assertTrue(report.complete)
        source.position = {"cursor": report.cursor_after, "since": "2026-10-05T01:00:00+00:00"}
        position = engine.status()["source_position"]
        print("[evidence] source_position =", json.dumps(position, ensure_ascii=False, sort_keys=True))
        self.assertEqual("2026-10-05T01:00:00+00:00", position["since"])

    def test_cursor_rank_drives_regression_detection(self) -> None:
        """数值比较钩子改名后，回退检测仍必须工作（这是它存在的唯一理由）。"""
        store = self.make_store()
        raws = [
            make_eml(subject="回退检测 %d" % index, message_id="<reg%d@example.org>" % index)
            for index in range(1, 4)
        ]
        source = MemoryMailSource(raws)
        self.assertEqual(3.0, source.cursor_rank("3"))
        engine = self.make_engine(store, source)
        first = engine.sync_once()
        self.assertEqual("3", first.cursor_after)
        source.return_cursor = "1"  # 来源回退 3 -> 1
        report = engine.sync_once()
        print("[evidence] regression =", report.invalid_reason, "| attempts =", report.list_attempts)
        self.assertTrue(report.cursor_invalidated)
        self.assertIn("游标回退", report.invalid_reason)
        self.assertEqual(2, report.list_attempts)


if __name__ == "__main__":
    unittest.main()
