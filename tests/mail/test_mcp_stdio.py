"""假 MCP stdio 服务器测试：调用序列、错误处理、超时、崩溃、只读、凭据不外泄。

用 `tests/mail/fake_mcp_server.py`（一个小型 scripted Python 子进程）驱动真实的
`StdioJsonRpcClient` / `McpMailSource`，因此这里覆盖的是**适配器**的行为，
不需要真实邮箱，也不产生任何外部副作用。
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from typing import Any, Dict, List, Optional, Sequence

from asistent.contracts.ids import sha256_bytes
from asistent.contracts.mail import MailSource
from asistent.contracts.reference import ReferenceStore
from asistent.mail import (
    CHILD_UTF8_ENV,
    CONTENT_ORIGIN_PROJECTION,
    LogSink,
    MailSyncEngine,
    McpCrashed,
    McpHandshakeError,
    McpMailSource,
    McpProtocolError,
    McpToolError,
    McpTimeout,
    RawStore,
    Redactor,
    StdioJsonRpcClient,
    SyncStateStore,
    WriteNotPermitted,
)
from tests.mail.evidence import evidence, make_evidence_safe
from tests.mail.fakes import TempWorkspace, counts

#: 证据输出与代码页无关：cp936(gbk) 控制台下打印不可编码字符不再抛 UnicodeEncodeError
EVIDENCE_STDOUT_SAFE = make_evidence_safe()

FAKE_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")
SECRET = "s3cr3t-imap-password-xyz"


def email_record(
    email_id: int,
    *,
    subject: str = "活动通知",
    date: str = "2026-10-05T01:00:00+00:00",
    message_id: Optional[str] = None,
    sender: str = "Organizer <organizer@example.org>",
    recipients: Optional[Sequence[str]] = None,
    attachments: Optional[Sequence[str]] = None,
    body: str = "正文内容",
    mailbox: str = "INBOX",
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "email_id": str(email_id),
        "message_id": message_id if message_id is not None else "<m%d@example.org>" % email_id,
        "subject": subject,
        "sender": sender,
        "recipients": list(recipients or ["owner@example.com"]),
        "date": date,
        "attachments": list(attachments or []),
        "body": body,
        "mailbox": mailbox,
        "in_reply_to": in_reply_to,
        "references": references,
    }


class McpStdioTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = TempWorkspace()
        self.addCleanup(self.ws.cleanup)
        self.transcript = os.path.join(self.ws.root, "transcript.jsonl")
        self.stderr_log = os.path.join(self.ws.log_dir, "mcp-stderr.log")
        self._clients: List[StdioJsonRpcClient] = []

    def tearDown(self) -> None:
        for client in self._clients:
            try:
                client.close(timeout=2.0)
            except Exception:  # pragma: no cover - 清理路径
                pass

    # ------------------------------------------------------------------ 夹具
    def write_emails(self, emails: Sequence[Dict[str, Any]]) -> str:
        path = os.path.join(self.ws.root, "emails-%d.json" % len(os.listdir(self.ws.root)))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(list(emails), handle, ensure_ascii=False)
        return path

    def write_text_file(self, name: str, text: str) -> str:
        """把非 ASCII 文本通过 UTF-8 文件传给子进程（argv 会被按文件系统编码编码）。"""
        path = os.path.join(self.ws.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def make_client(
        self,
        extra_args: Sequence[str] = (),
        *,
        env: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
        log_path: Optional[str] = None,
    ) -> StdioJsonRpcClient:
        command = [sys.executable, FAKE_SERVER, "--transcript", self.transcript, *extra_args]
        client = StdioJsonRpcClient(
            command,
            env=env,
            timeout=timeout,
            log=LogSink(log_path or self.stderr_log),
            redactor=Redactor([SECRET]),
        )
        self._clients.append(client)
        return client

    def make_source(self, client: StdioJsonRpcClient, **kwargs: Any) -> McpMailSource:
        kwargs.setdefault("account", "fake")
        kwargs.setdefault("folder", "INBOX")
        return McpMailSource(client, **kwargs)

    def transcript_entries(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.transcript):
            return []
        rows: List[Dict[str, Any]] = []
        with open(self.transcript, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def transcript_methods(self) -> List[str]:
        return [str(row.get("method")) for row in self.transcript_entries()]

    def transcript_tools(self) -> List[str]:
        return [
            str(row.get("tool"))
            for row in self.transcript_entries()
            if row.get("method") == "tools/call"
        ]

    def tool_arguments(self, tool: str) -> List[Dict[str, Any]]:
        return [
            dict(row.get("arguments") or {})
            for row in self.transcript_entries()
            if row.get("method") == "tools/call" and row.get("tool") == tool
        ]


class HandshakeTest(McpStdioTestBase):
    def test_handshake_and_tools_list_sequence(self) -> None:
        emails = self.write_emails([email_record(1)])
        client = self.make_client(["--emails", emails])
        source = self.make_source(client)
        source.connect()

        methods = self.transcript_methods()
        print("[evidence] stdio method sequence =", methods)
        self.assertEqual(["initialize", "notifications/initialized", "tools/list"], methods)
        self.assertEqual(["initialize", "tools/list"], [row["method"] for row in client.call_log])
        self.assertEqual("email", source.server_info.get("name"))
        self.assertIn("list_emails_metadata", source.available_tools)
        self.assertTrue(client.is_alive())

    def test_missing_required_tool_fails_handshake(self) -> None:
        client = self.make_client(["--folders", "INBOX"])
        source = self.make_source(client)
        source.tools.metadata = "no_such_tool"
        with self.assertRaises(McpHandshakeError) as ctx:
            source.connect()
        print("[evidence] handshake missing tool =", ctx.exception.to_dict())
        self.assertEqual("MCP_HANDSHAKE_ERROR", ctx.exception.code)
        self.assertIn("no_such_tool", str(ctx.exception))


class ListAndFetchTest(McpStdioTestBase):
    def test_list_new_pages_through_metadata_and_returns_versioned_cursor(self) -> None:
        emails = self.write_emails([
            email_record(index, subject="M%d" % index, date="2026-10-05T0%d:00:00+00:00" % (index + 1))
            for index in range(1, 6)
        ])
        client = self.make_client(["--emails", emails])
        source = self.make_source(client, page_size=2)
        refs, cursor = source.list_new(None)

        pages = [int(arg["page"]) for arg in self.tool_arguments("list_emails_metadata")]
        print("[evidence] metadata pages =", pages)
        print("[evidence] refs =", [(ref.uid, ref.subject, ref.content_digest[:12]) for ref in refs])
        self.assertEqual([1, 2, 3], pages)
        self.assertEqual(["1", "2", "3", "4", "5"], [ref.uid for ref in refs])
        self.assertEqual(
            ["INBOX:1", "INBOX:2", "INBOX:3", "INBOX:4", "INBOX:5"],
            ["%s:%s" % (ref.folder, ref.uid) for ref in refs],
        )
        decoded = json.loads(cursor)
        print("[evidence] cursor =", json.dumps(decoded, ensure_ascii=False, sort_keys=True))
        self.assertEqual(1, decoded["v"])
        self.assertEqual("fake", decoded["account"])
        self.assertEqual("INBOX", decoded["folder"])
        self.assertEqual("2026-10-05T06:00:00+00:00", decoded["since"])
        identities = {ref.identity_key() for ref in refs}
        self.assertEqual(5, len(identities))

    def test_cursor_for_other_account_or_folder_is_rejected(self) -> None:
        client = self.make_client(["--emails", self.write_emails([email_record(1)])])
        source = self.make_source(client)
        _, cursor = source.list_new(None)
        other = self.make_source(client, account="another", folder="INBOX")
        with self.assertRaises(Exception) as ctx:
            other.decode_cursor(cursor)
        print("[evidence] cross-account cursor rejected =", type(ctx.exception).__name__, str(ctx.exception))
        self.assertIn("账号", str(ctx.exception))

        other_folder = self.make_source(client, folder="Archive")
        with self.assertRaises(Exception) as ctx2:
            other_folder.decode_cursor(cursor)
        self.assertIn("文件夹", str(ctx2.exception))

    def test_fetch_raw_is_deterministic_and_marks_no_read_flags(self) -> None:
        emails = self.write_emails([
            email_record(1, subject="带附件", attachments=["报名表.txt"], body="第一行\n第二行",
                         message_id="<att-1@example.org>")
        ])
        client = self.make_client(["--emails", emails])
        source = self.make_source(client)
        refs, _ = source.list_new(None)
        first = source.fetch_raw(refs[0])
        second = source.fetch_raw(refs[0])
        print("[evidence] reconstructed raw head =", first.decode("utf-8").splitlines()[:4])
        self.assertEqual(first, second)  # 确定性重建
        self.assertIn(b"X-Asistent-Uid: 1", first)
        self.assertIn("报名表.txt".encode("utf-8"), first)
        self.assertIn("第二行".encode("utf-8"), first)
        arguments = self.tool_arguments("get_emails_content")
        print("[evidence] get_emails_content args =", arguments)
        self.assertTrue(all(arg.get("mark_as_read") is False for arg in arguments))
        self.assertTrue(refs[0].has_attachments)

    def test_long_body_is_paged_until_truncation_marker_disappears(self) -> None:
        long_body = "X" * 250
        emails = self.write_emails([email_record(1, body=long_body)])
        client = self.make_client(["--emails", emails])
        source = self.make_source(client, max_body_length=100)
        refs, _ = source.list_new(None)
        raw = source.fetch_raw(refs[0])
        offsets = [int(arg["body_offset"]) for arg in self.tool_arguments("get_emails_content")]
        print("[evidence] body page offsets =", offsets)
        self.assertEqual([0, 100, 200], offsets)
        self.assertTrue(raw.decode("utf-8").endswith(long_body))


class ErrorHandlingTest(McpStdioTestBase):
    def test_rpc_error_is_detected(self) -> None:
        emails = self.write_emails([email_record(1)])
        client = self.make_client(["--emails", emails, "--rpc-error-tool", "list_emails_metadata"])
        source = self.make_source(client)
        with self.assertRaises(McpToolError) as ctx:
            source.list_new(None)
        print("[evidence] rpc error =", ctx.exception.to_dict())
        self.assertEqual("MCP_TOOL_ERROR", ctx.exception.code)
        self.assertEqual("list_emails_metadata", ctx.exception.detail.get("tool"))

    def test_iserror_result_is_detected(self) -> None:
        emails = self.write_emails([email_record(1)])
        client = self.make_client(["--emails", emails, "--is-error-tool", "get_emails_content"])
        source = self.make_source(client)
        refs, _ = source.list_new(None)
        with self.assertRaises(McpToolError) as ctx:
            source.fetch_raw(refs[0])
        print("[evidence] isError result =", ctx.exception.to_dict())
        self.assertIn("fake isError", str(ctx.exception))

    def test_timeout_is_bounded(self) -> None:
        emails = self.write_emails([email_record(1)])
        client = self.make_client(
            ["--emails", emails, "--hang-tool", "list_emails_metadata", "--hang-seconds", "30"],
            timeout=0.6,
        )
        source = self.make_source(client)
        started = time.monotonic()
        with self.assertRaises(McpTimeout) as ctx:
            source.list_new(None)
        elapsed = time.monotonic() - started
        print("[evidence] timeout elapsed = %.2fs" % elapsed)
        self.assertLess(elapsed, 5.0)
        self.assertEqual("MCP_TIMEOUT", ctx.exception.code)

    def test_crash_is_detected_with_exit_code(self) -> None:
        emails = self.write_emails([email_record(1)])
        client = self.make_client(
            ["--emails", emails, "--crash-tool", "list_emails_metadata", "--crash-code", "9"]
        )
        source = self.make_source(client)
        with self.assertRaises(McpCrashed) as ctx:
            source.list_new(None)
        print("[evidence] crash detected =", ctx.exception.to_dict())
        self.assertEqual(9, ctx.exception.detail.get("returncode"))
        self.assertFalse(client.is_alive())

    def test_garbage_lines_and_notifications_are_skipped(self) -> None:
        emails = self.write_emails([email_record(1)])
        client = self.make_client(["--emails", emails, "--garbage", "2", "--notify-before-result"])
        source = self.make_source(client)
        refs, _ = source.list_new(None)
        print("[evidence] protocol noise =", client.protocol_noise,
              "notifications =", len(client.notifications))
        self.assertEqual(1, len(refs))
        self.assertGreaterEqual(client.protocol_noise, 2)
        self.assertGreaterEqual(len(client.notifications), 2)

    def test_structured_content_mode_is_supported(self) -> None:
        emails = self.write_emails([email_record(1)])
        client = self.make_client(["--emails", emails, "--structured"])
        source = self.make_source(client)
        folders = source.list_folders()
        refs, _ = source.list_new(None)
        print("[evidence] structuredContent folders =", folders, "refs =", len(refs))
        self.assertEqual(["INBOX", "Sent", "Drafts"], folders)
        self.assertEqual(1, len(refs))


class ReadOnlyTest(McpStdioTestBase):
    def test_write_tools_are_refused_locally_and_never_sent(self) -> None:
        emails = self.write_emails([email_record(1)])
        client = self.make_client(["--emails", emails])
        source = self.make_source(client, read_only=True)
        source.connect()
        with self.assertRaises(WriteNotPermitted) as ctx:
            client.call_tool(
                "send_email",
                {"account_name": "fake", "recipients": ["a@b.c"], "subject": "s", "body": "b"},
                read_only=True,
            )
        print("[evidence] write guard =", ctx.exception.to_dict())
        for tool in ("delete_emails", "mark_emails_as_read", "move_emails", "archive_emails",
                     "save_draft", "save_to_mailbox", "forward_email", "set_email_flags"):
            with self.assertRaises(WriteNotPermitted):
                client.call_tool(tool, {}, read_only=True)
        source.list_new(None)
        self.assertNotIn("WRITE_TOOL_CALLED", json.dumps(self.transcript_entries()))
        self.assertEqual(
            [],
            [tool for tool in self.transcript_tools() if tool in ("send_email", "delete_emails")],
        )

    def test_engine_over_mcp_source_is_idempotent(self) -> None:
        emails = self.write_emails([
            email_record(1, subject="第一封", date="2026-10-05T01:00:00+00:00"),
            email_record(2, subject="第二封", date="2026-10-05T02:00:00+00:00"),
        ])
        client = self.make_client(["--emails", emails])
        source = self.make_source(client, page_size=10, overlap_seconds=0)
        store = ReferenceStore()
        engine = MailSyncEngine(
            store, source, account="fake", folder="INBOX",
            state_store=SyncStateStore(self.ws.state_path), raw_store=RawStore(self.ws.raw_root),
        )
        first = engine.sync_once()
        print("[evidence] mcp run1 =", first.summary_line())
        self.assertEqual(2, first.documents_created)
        self.assertEqual(2, first.cases_created)
        self.assertEqual(2, first.events_published)

        second = engine.sync_once()
        print("[evidence] mcp run2 =", second.summary_line())
        self.assertEqual(0, second.documents_created)
        self.assertEqual(0, second.cases_created)
        self.assertEqual(0, second.events_published)
        before = counts(store)

        third = engine.sync_once()
        self.assertEqual(before, counts(store))
        print("[evidence] mcp run3 counts unchanged =", json.dumps(before, sort_keys=True))

        with open(emails, "w", encoding="utf-8") as handle:
            json.dump([
                email_record(1, subject="第一封", date="2026-10-05T01:00:00+00:00"),
                email_record(2, subject="第二封", date="2026-10-05T02:00:00+00:00"),
                email_record(3, subject="第三封", date="2026-10-05T03:00:00+00:00"),
            ], handle, ensure_ascii=False)
        client.close(timeout=2.0)
        client2 = self.make_client(["--emails", emails])
        source2 = self.make_source(client2, page_size=10, overlap_seconds=0)
        engine2 = MailSyncEngine(
            store, source2, account="fake", folder="INBOX",
            state_store=SyncStateStore(self.ws.state_path), raw_store=RawStore(self.ws.raw_root),
        )
        fourth = engine2.sync_once()
        print("[evidence] mcp run4 (new mail) =", fourth.summary_line())
        self.assertEqual(1, fourth.documents_created)
        self.assertEqual(1, fourth.cases_created)
        self.assertEqual(3, len(store.find("SourceDocument")))

    def test_two_accounts_with_same_message_id_are_not_merged(self) -> None:
        emails = self.write_emails([
            email_record(1, subject="跨账号同 Message-ID",
                         message_id="<same-id@example.org>", date="2026-10-05T01:00:00+00:00")
        ])
        client_a = self.make_client(["--emails", emails])
        client_b = self.make_client(["--emails", emails])
        source_a = self.make_source(client_a, account="acct-a")
        source_b = self.make_source(client_b, account="acct-b")
        refs_a, _ = source_a.list_new(None)
        refs_b, _ = source_b.list_new(None)
        print("[evidence] identity A =", refs_a[0].identity_key())
        print("[evidence] identity B =", refs_b[0].identity_key())
        self.assertEqual("<same-id@example.org>", refs_a[0].message_id)
        self.assertEqual("<same-id@example.org>", refs_b[0].message_id)
        self.assertNotEqual(refs_a[0].identity_key(), refs_b[0].identity_key())

        store = ReferenceStore()
        for source in (source_a, source_b):
            engine = MailSyncEngine(
                store, source, account=source.account, folder="INBOX",
                state_store=SyncStateStore(None), raw_store=RawStore(self.ws.raw_root),
            )
            report = engine.sync_once()
            self.assertEqual(1, report.documents_created)
        documents = store.find("SourceDocument")
        print("[evidence] cross-account documents =",
              [(doc.source_system, doc.identity_key[:16]) for doc in documents])
        self.assertEqual(2, len(documents))
        self.assertEqual(2, len(store.find("Case")))
        self.assertEqual(2, len(store.find("Conversation")))
        self.assertEqual(2, len({doc.source_system for doc in documents}))

    def test_metadata_conflict_is_quarantined_immediately(self) -> None:
        emails = self.write_emails([email_record(1, subject="原主题")])
        # 改写主题走 UTF-8 文件，不走 argv：Popen 用父进程的文件系统编码编码命令行参数，
        # 在非 UTF-8 locale（LC_ALL=C → ascii / zh_CN.GBK）下非 ASCII 参数根本传不进去。
        mutation = self.write_text_file("mutation.txt", "被改写的主题")
        client = self.make_client(["--emails", emails, "--mutate-subject-file", mutation])
        source = self.make_source(client)
        store = ReferenceStore()
        engine = MailSyncEngine(
            store, source, account="fake", folder="INBOX",
            state_store=SyncStateStore(None), raw_store=RawStore(self.ws.raw_root),
        )
        report = engine.sync_once()
        print("[evidence] metadata conflict =", report.summary_line())
        print("[evidence] conflict failure =", report.failures[0].to_dict() if report.failures else None)
        self.assertEqual(0, report.documents_created)
        self.assertEqual(1, len(report.failures))
        self.assertEqual("fetch", report.failures[0].stage)
        self.assertTrue(report.failures[0].terminal)  # 非重试类失败 → 立即隔离，不阻塞游标
        self.assertEqual(1, len(engine.status()["quarantined"]))
        self.assertIsNotNone(report.cursor_after)

    def test_mcp_failures_become_queryable_state_and_recovery_works(self) -> None:
        emails = self.write_emails([email_record(1)])
        client = self.make_client(["--emails", emails, "--crash-tool", "list_emails_metadata"])
        source = self.make_source(client, max_pages=1)
        store = ReferenceStore()
        state_path = self.ws.state_path
        engine = MailSyncEngine(
            store, source, account="fake", folder="INBOX",
            state_store=SyncStateStore(state_path), raw_store=RawStore(self.ws.raw_root),
        )
        report = engine.sync_once()
        print("[evidence] mcp crash run =", report.summary_line())
        print("[evidence] mcp crash failure =", report.failures[0].to_dict())
        self.assertFalse(report.complete)
        self.assertIsNone(report.cursor_after)
        self.assertEqual("SYSTEM_CRASH", report.failures[0].failure_class)

        healthy = self.make_client(["--emails", emails])
        engine2 = MailSyncEngine(
            store, self.make_source(healthy), account="fake", folder="INBOX",
            state_store=SyncStateStore(state_path), raw_store=RawStore(self.ws.raw_root),
        )
        recovered = engine2.sync_once()
        print("[evidence] mcp recovery run =", recovered.summary_line())
        self.assertEqual(1, recovered.documents_created)
        self.assertIsNotNone(recovered.cursor_after)
        failed_rows = [row for row in store.audit_trail(limit=100) if row.result == "FAILED"]
        self.assertEqual(1, len(failed_rows))
        self.assertEqual("MailSync", failed_rows[0].object_type)
        self.assertEqual("SYSTEM_CRASH", failed_rows[0].detail.get("failure_class"))


class CredentialHygieneTest(McpStdioTestBase):
    def test_stderr_goes_only_to_log_and_secrets_are_redacted(self) -> None:
        emails = self.write_emails([email_record(1)])
        env = {"ASISTENT_FAKE_PASSWORD": SECRET, "ASISTENT_FAKE_TOKEN": SECRET}
        client = self.make_client(
            ["--emails", emails, "--secret-env", "ASISTENT_FAKE_PASSWORD"],
            env=env,
        )
        source = self.make_source(client)
        source.connect()
        refs, _ = source.list_new(None)
        source.fetch_raw(refs[0])
        client.close(timeout=2.0)

        with open(self.stderr_log, "r", encoding="utf-8") as handle:
            log_text = handle.read()
        print("[evidence] stderr log first line =", log_text.splitlines()[0] if log_text else "")
        print("[evidence] secret found in log =", SECRET in log_text)
        self.assertIn("DEBUG imap login failed", log_text)
        self.assertNotIn(SECRET, log_text)
        self.assertIn("***REDACTED***", log_text)
        self.assertNotIn(SECRET, json.dumps(client.call_log, ensure_ascii=False))
        self.assertNotIn(SECRET, json.dumps(client.stderr_tail(50), ensure_ascii=False))

        store = ReferenceStore()
        engine = MailSyncEngine(
            store, source, account="fake", folder="INBOX",
            state_store=SyncStateStore(self.ws.state_path), raw_store=RawStore(self.ws.raw_root),
            redact=client.redactor.redact,
        )
        engine.sync_once()
        audit_text = json.dumps([row.to_dict() for row in store.audit_trail(limit=100)], ensure_ascii=False)
        print("[evidence] secret found in audit =", SECRET in audit_text)
        self.assertNotIn(SECRET, audit_text)
        with open(self.ws.state_path, "r", encoding="utf-8") as handle:
            state_text = handle.read()
        self.assertNotIn(SECRET, state_text)
        with open(store.find("SourceDocument")[0].origin_path, "rb") as handle:
            self.assertNotIn(SECRET.encode("utf-8"), handle.read())

    def test_redactor_masks_unknown_inline_credentials(self) -> None:
        redactor = Redactor()
        text = "connecting imap://user:topsecretpw@imap.example.org password=hunter2 token=abc123"
        masked = redactor.redact(text)
        print("[evidence] redacted =", masked)
        self.assertNotIn("hunter2", masked)
        self.assertNotIn("abc123", masked)
        self.assertNotIn("topsecretpw", masked)
        self.assertIn("***REDACTED***", masked)


class ConsoleEncodingRegressionTest(McpStdioTestBase):
    """非 UTF-8 控制台（Windows cp936/gbk）下的编码回归。

    缺陷现场（merge f970975 后在开发机默认控制台复现）：
      AssertionError: b'\\xe6\\x8a\\xa5\\xe5\\x90\\x8d\\xe8\\xa1\\xa8.txt' not found in
        b'...X-Asistent-Attachments: \\xef\\xbf\\xbd\\xef\\xbf\\xbd...'
    根因：适配器没有为子进程强制 UTF-8 stdio，子进程按 ANSI 代码页收发 JSON-RPC，
    中文主题/附件名在**子进程侧**就被替换成 U+FFFD，父进程再"正常"解码，于是静默写坏数据。

    本类做两件事：
    1. 把「敌对环境」（PYTHONUTF8=0 / PYTHONIOENCODING=cp936）显式传给子进程，
       断言适配器仍然强制 UTF-8（既断言 env，也读子进程自报的真实 stdio 编码）；
    2. 断言中文主题、附件名在 raw 里**逐字节**等于 UTF-8 编码，且不出现替换字符；
       非法 UTF-8 输入必须显式报错（`McpProtocolError` + 字节偏移），不能静默替换。
    """

    #: 模拟开发机默认控制台 / GBK locale：调用方把子进程推向 ANSI 代码页
    HOSTILE_ENV = {
        "PYTHONUTF8": "0",
        "PYTHONIOENCODING": "cp936",
        "LANG": "zh_CN.GBK",
        "LC_ALL": "zh_CN.GBK",
    }
    SUBJECT = "活动通知：报名表（中文）"
    ATTACHMENT = "报名表.txt"
    BODY = "请提交报名表，截止日期 10 月 20 日。"

    def chinese_email(self) -> str:
        return self.write_emails([
            email_record(1, subject=self.SUBJECT, attachments=[self.ATTACHMENT], body=self.BODY,
                         message_id="<cn-1@example.org>")
        ])

    def test_child_env_is_forced_to_utf8_even_when_caller_says_cp936(self) -> None:
        client = self.make_client(
            ["--emails", self.chinese_email(), "--report-encoding"], env=dict(self.HOSTILE_ENV)
        )
        self.make_source(client).connect()

        snapshot = client.child_env
        evidence("child env PYTHONUTF8 =", snapshot["PYTHONUTF8"],
                 "PYTHONIOENCODING =", snapshot["PYTHONIOENCODING"],
                 "CHILD_UTF8_ENV =", json.dumps(CHILD_UTF8_ENV, sort_keys=True))
        self.assertEqual("1", snapshot["PYTHONUTF8"])
        self.assertEqual("utf-8", snapshot["PYTHONIOENCODING"])
        # 父环境要保留（子进程需要 HOME/PATH/XDG_*），只是被覆盖掉编码项
        self.assertIn("PATH", snapshot)

        reported = " | ".join(client.stderr_tail(30))
        evidence("child self-reported stdio:", reported)
        self.assertIn("ENVCHECK", reported)
        self.assertIn("stdout=utf-8", reported)
        self.assertIn("stdin=utf-8", reported)
        self.assertIn("utf8_mode=1", reported)

    def test_chinese_mail_round_trips_byte_exact_under_hostile_env(self) -> None:
        client = self.make_client(["--emails", self.chinese_email()], env=dict(self.HOSTILE_ENV))
        source = self.make_source(client)
        refs, _ = source.list_new(None)
        ref = refs[0]
        evidence("subject over the wire =", repr(ref.subject))
        self.assertEqual(self.SUBJECT, ref.subject)  # 列取阶段就已经不能被替换

        raw = source.fetch_raw(ref)
        evidence("raw head =", raw.split(b"\r\n")[:8])
        self.assertIn(self.SUBJECT.encode("utf-8"), raw)
        self.assertIn(self.ATTACHMENT.encode("utf-8"), raw)
        self.assertIn(self.BODY.encode("utf-8"), raw)
        self.assertNotIn("\ufffd".encode("utf-8"), raw)  # 不允许出现替换字符
        evidence("attachment name bytes =", self.ATTACHMENT.encode("utf-8"),
                 "U+FFFD present =", b"\xef\xbf\xbd" in raw)

        second = source.fetch_raw(ref)  # 往返确定性（重建不受控制台影响）
        self.assertEqual(raw, second)

    def test_identity_is_console_independent(self) -> None:
        emails = self.chinese_email()
        hostile = self.make_client(["--emails", emails], env=dict(self.HOSTILE_ENV))
        neutral = self.make_client(["--emails", emails], env={"PYTHONUTF8": "1"})
        first = self.make_source(hostile).list_new(None)[0][0]
        second = self.make_source(neutral).list_new(None)[0][0]
        evidence("identity under hostile env =", first.identity_key())
        evidence("identity under utf8 env    =", second.identity_key())
        self.assertEqual(first.identity_key(), second.identity_key())
        self.assertEqual(first.content_digest, second.content_digest)

    def test_invalid_utf8_from_server_is_reported_not_replaced(self) -> None:
        client = self.make_client(
            ["--emails", self.chinese_email(), "--raw-invalid-utf8"], env=dict(self.HOSTILE_ENV)
        )
        source = self.make_source(client)
        with self.assertRaises(McpProtocolError) as ctx:
            source.list_new(None)
        detail = ctx.exception.detail
        evidence("invalid utf-8 error =", json.dumps(ctx.exception.to_dict(), ensure_ascii=False))
        evidence("invalid utf-8 detail =", json.dumps(detail, ensure_ascii=False, sort_keys=True))
        self.assertIn("UTF-8", str(ctx.exception))
        self.assertIn("list_emails_metadata", str(ctx.exception))
        self.assertEqual("tools/call", detail.get("method"))
        self.assertEqual("list_emails_metadata", detail.get("tool"))
        self.assertIsNotNone(detail.get("byte_offset"))
        self.assertTrue(str(detail.get("hex")))
        self.assertIn("PYTHONUTF8", str(detail.get("hint")))

    def test_invalid_utf8_on_stderr_is_marked_not_swallowed(self) -> None:
        client = self.make_client(
            ["--emails", self.chinese_email(), "--raw-invalid-utf8-stderr"], env=dict(self.HOSTILE_ENV)
        )
        source = self.make_source(client)
        source.connect()  # 日志通道的坏字节不影响协议通道
        refs, _ = source.list_new(None)
        self.assertEqual(self.SUBJECT, refs[0].subject)

        with open(self.stderr_log, "r", encoding="utf-8") as handle:
            log_text = handle.read()
        evidence("stderr decode failures =", client.stderr_decode_failures)
        self.assertGreaterEqual(client.stderr_decode_failures, 1)
        self.assertIn("[非 UTF-8 字节", log_text)
        self.assertIn("字节已丢弃", log_text)
        self.assertIn("fake mcp-email-server ready", log_text)

    def test_sync_over_chinese_mail_under_hostile_env_stores_intact_bytes(self) -> None:
        store = ReferenceStore()
        client = self.make_client(["--emails", self.chinese_email()], env=dict(self.HOSTILE_ENV))
        source = self.make_source(client)
        engine = MailSyncEngine(
            store, source, account="fake", folder="INBOX",
            state_store=SyncStateStore(self.ws.state_path), raw_store=RawStore(self.ws.raw_root),
        )
        report = engine.sync_once()
        evidence("chinese sync =", report.summary_line())
        self.assertEqual(1, report.documents_created)
        document = store.find("SourceDocument")[0]
        with open(document.origin_path, "rb") as handle:
            stored = handle.read()
        self.assertIn(self.ATTACHMENT.encode("utf-8"), stored)
        self.assertNotIn(b"\xef\xbf\xbd", stored)
        evidence("stored raw bytes =", len(stored), "attachment bytes found = True",
                 "U+FFFD found = False")


class ContractV04AlignmentTest(McpStdioTestBase):
    """契约 v0.4 对齐：`content_origin` 显式声明 + `cursor_position` 观测能力拆分。"""

    def test_mcp_channel_declares_metadata_projection(self) -> None:
        """MCP 通路只给元数据：引用与文档都必须声明 METADATA_PROJECTION。"""
        emails = self.write_emails([email_record(1), email_record(2)])
        client = self.make_client(["--emails", emails])
        source = self.make_source(client)
        refs, _ = source.list_new(None)
        evidence("mcp ref origins =", sorted({ref.content_origin for ref in refs}))
        self.assertEqual(len(refs), len([ref for ref in refs if ref.content_origin == CONTENT_ORIGIN_PROJECTION]))
        self.assertEqual({CONTENT_ORIGIN_PROJECTION}, {ref.content_origin for ref in refs})

        store = ReferenceStore()
        engine = MailSyncEngine(
            store, source, account="fake", folder="INBOX",
            state_store=SyncStateStore(self.ws.state_path), raw_store=RawStore(self.ws.raw_root),
        )
        engine.sync_once()
        documents = store.find("SourceDocument")
        origins = sorted({str(doc.content_origin) for doc in documents})
        evidence("mcp document origins =", origins)
        self.assertEqual([CONTENT_ORIGIN_PROJECTION], origins)
        for document in documents:
            with open(document.origin_path, "rb") as handle:
                stored = handle.read()
            # content_hash 是"重建字节"的摘要 —— 重建是元数据投影的确定性函数，
            # 所以它能证明"投影一致"，但**不能**证明与远端原始字节一致（这是声明的意义）。
            self.assertEqual(sha256_bytes(stored), document.content_hash)
        engine.sync_once()  # 重放不能把声明改回默认值
        self.assertEqual(
            {CONTENT_ORIGIN_PROJECTION}, {document.content_origin for document in store.find("SourceDocument")}
        )

    def test_cursor_position_is_observability_and_cursor_rank_is_comparison(self) -> None:
        """契约形状 `cursor_position()` 无参、返回字典；数值比较另名 `cursor_rank`。"""
        emails = self.write_emails([email_record(1), email_record(2)])
        client = self.make_client(["--emails", emails])
        source = self.make_source(client)
        evidence("cursor_position before listing =", source.cursor_position())
        self.assertIsNone(source.cursor_position())  # 没有信息 → None，不假装 0 进度

        refs, cursor = source.list_new(None)
        position = source.cursor_position()
        evidence("cursor_position after listing =", json.dumps(position, ensure_ascii=False, sort_keys=True))
        self.assertIsInstance(position, dict)
        self.assertEqual("fake", position["account"])
        self.assertEqual("INBOX", position["folder"])
        self.assertEqual(2, position["listed_count"])
        self.assertEqual(cursor, position["cursor"])
        self.assertIn("since", position)

        store = ReferenceStore()
        engine = MailSyncEngine(
            store, source, account="fake", folder="INBOX",
            state_store=SyncStateStore(self.ws.state_path), raw_store=RawStore(self.ws.raw_root),
        )
        engine.sync_once()
        self.assertEqual(position["since"], engine.status()["source_position"]["since"])

        # 数值比较钩子：游标更晚 → rank 更大；旧游标 → 变小（回退检测用）
        earlier = source.encode_cursor(since="2026-10-01T00:00:00+00:00")
        later = source.encode_cursor(since="2026-10-09T00:00:00+00:00")
        evidence("cursor_rank =", source.cursor_rank(earlier), source.cursor_rank(later))
        self.assertLess(source.cursor_rank(earlier), source.cursor_rank(later))
        self.assertIsNone(source.cursor_rank("not-a-cursor"))

    def test_mcp_source_satisfies_mail_source_protocol(self) -> None:
        from asistent.contracts.mail import MailSource

        emails = self.write_emails([email_record(1)])
        client = self.make_client(["--emails", emails])
        source = self.make_source(client)
        source.connect()
        evidence("isinstance(mcp_source, MailSource) =", isinstance(source, MailSource))
        # 运行时协议只检查必需成员：可选能力（cursor_position）不影响判定
        self.assertIsInstance(source, MailSource)
        self.assertTrue(callable(getattr(source, "cursor_position", None)))

    def test_unencodable_command_line_reports_fs_encoding_not_mystery_error(self) -> None:
        """非 UTF-8 文件系统编码下 argv 编不出来时，必须是可诊断的显式失败。

        真实场景：`LC_ALL=C`（fsencoding=ascii）下命令行参数含中文，Popen 在**父进程**就失败
        （子进程根本没起来）。这时错误详情必须说明"文件系统编码 + 提示换 UTF-8 locale"，
        而不是让调用方看到"列取新邮件未预期失败"。
        """

        def exploding_popen(command, **kwargs):
            raise UnicodeEncodeError("ascii", "被改写的主题", 0, 6, "ordinal not in range(128)")

        client = StdioJsonRpcClient(
            ["fake-server", "--subject", "中文"],
            popen_factory=exploding_popen,
            log=LogSink(os.path.join(self.ws.log_dir, "spawn.log")),
        )
        with self.assertRaises(McpCrashed) as caught:
            client.start()
        error = caught.exception
        evidence("spawn failure =", error.code, "|", error.message)
        evidence("spawn detail =", json.dumps(error.detail, ensure_ascii=False, sort_keys=True))
        self.assertEqual("MCP_CRASHED", error.code)
        self.assertIn("文件系统编码", error.message)
        self.assertEqual(sys.getfilesystemencoding(), error.detail["fs_encoding"])
        self.assertIn("UTF-8", error.detail["hint"])
        # 凭据卫生：错误详情不得携带完整 argv
        self.assertEqual("fake-server", error.detail["command0"])
        self.assertNotIn("中文", json.dumps(error.detail, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
