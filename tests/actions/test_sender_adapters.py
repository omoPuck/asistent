"""通道适配器互操作：本模块的适配器要能顶替冻结契约里的适配器。

契约只要求 `MailSender` 协议（`name` + `send`），所以只要有 `name`/`send`
就能插进引擎。这里验证：

- 冻结契约自带的 `RecordingMailSender`（只写本地发件箱）可以原样接进来；
- 本模块的 `ScriptedMailSender` / `MailCallLog` 满足同一协议；
- 调用账本是"跨进程可读"的：父进程能读到子进程落盘的提交次数（崩溃测试依赖它）。
"""

from __future__ import annotations

import os
import unittest

from asistent.actions import (
    ActionEngine,
    MailCallLog,
    OutboundRejected,
    ScriptedMailSender,
    classify_sender_exception,
)
from asistent.contracts import (
    MailSendRequest,
    MailSender,
    PermissionDenied,
    RecordingMailSender,
    ValidationError,
)

from .support import ActionCase, backend_kind, dump, engine_for, run_child


class ChannelInteropTest(ActionCase):
    def test_adapters_satisfy_the_sender_protocol(self) -> None:
        outbox = self.path("outbox")
        frozen = RecordingMailSender(outbox)
        scripted = ScriptedMailSender()
        self.assertIsInstance(frozen, MailSender)
        self.assertIsInstance(scripted, MailSender)
        self.assertEqual("recording", frozen.name)
        self.assertEqual("scripted", scripted.name)

    def test_frozen_recording_sender_drives_the_engine_once(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        outbox = self.path("outbox")
        frozen = RecordingMailSender(outbox)
        engine = ActionEngine(self.store, frozen, node="executor-interop")

        first = engine.execute(action.id)
        second = engine.execute(action.id)

        self.assertEqual("SUCCEEDED", first.result_class)
        self.assertTrue(first.sender_called, "通道没有 call_count 时也必须如实标记已调用")
        self.assertEqual("REJECTED", second.result_class)
        files = sorted(os.listdir(outbox))
        self.assertEqual(1, len(files), "发件箱必须只有一封")
        with open(os.path.join(outbox, files[0]), encoding="utf-8") as handle:
            raw = handle.read()
        self.assertIn("X-Asistent-Idempotency-Key: %s" % first.idempotency_key, raw)
        self.assertIn("Subject: %s" % action.action_summary["subject"], raw)
        self.assertEqual(1, len(frozen.sent))
        print("[evidence] 冻结适配器发件箱 =", dump({"files": files, "sender_called": first.sender_called}))

    def test_scripted_sender_rejects_a_request_without_idempotency_key(self) -> None:
        sender = ScriptedMailSender()
        request = MailSendRequest(
            from_account="owner@example.com",
            to=["organizer@example.org"],
            subject="Re: 活动通知",
            body="正文",
            idempotency_key="",
        )
        with self.assertRaises(ValidationError):
            sender.send(request)
        self.assertEqual(1, sender.call_count, "call_count 数的是「调用了几次 send()」")
        self.assertEqual([], sender.log_entries(), "没有提交就没有账本条目")
        self.assertEqual([], sender.submitted_keys(), "非法请求不得占用幂等键")

    def test_engine_refuses_to_send_a_stored_action_without_a_key(self) -> None:
        """动作上没有幂等键时，引擎必须在**领权之前**拒绝（不许拿动作 ID 现造一个）。"""
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        raw = self.store.get("ExternalAction", action.id)
        raw.idempotency_key = ""
        self.store.put(raw, expected_revision=raw.revision, reason="测试：清掉幂等键")

        engine, sender = engine_for(self.store)
        outcome = engine.execute(action.id)

        self.assertEqual("REJECTED", outcome.result_class)
        self.assertEqual("VALIDATION_ERROR", outcome.code)
        self.assertEqual(0, sender.call_count)
        self.assertEqual("AUTHORIZED", str(self.store.get("ExternalAction", action.id).state), "未领权")
        print("[evidence] 缺幂等键被拒 =", dump(outcome.to_dict()))

    def test_exception_classification_is_conservative(self) -> None:
        cases = [
            (OutboundRejected("通道拒绝"), "FAILED_SAFE"),
            (ValidationError("请求不合法"), "FAILED_SAFE"),
            (PermissionDenied("没有发送权限"), "FAILED_SAFE"),
            (KeyError("意外错误"), "UNKNOWN"),
            (RuntimeError("谁也不认识的错"), "UNKNOWN"),
            (ValueError("维度不对"), "UNKNOWN"),
        ]
        rows = []
        for exc, expected in cases:
            actual = classify_sender_exception(exc)
            rows.append({"exception": type(exc).__name__, "classified": list(actual)})
            self.assertEqual(expected, actual[0])
        for _exc, expected in cases:
            if expected == "UNKNOWN":
                self.assertEqual("EXTERNAL_UNKNOWN", classify_sender_exception(_exc)[1], "未知错误必须保守归 UNKNOWN")
        print("[evidence] 异常分类表 =", dump(rows))

    @unittest.skipUnless(backend_kind() == "sqlite", "跨进程账本需要持久化后端")
    def test_call_log_is_readable_across_processes(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        call_log = self.path("cross-process-log.log")
        self.store.close()

        code, output = run_child("claim_child.py", self.db_path(), call_log, action.id, "log-worker")
        self.assertEqual(0, code, output)
        self.assertEqual(1, MailCallLog.submitted_count_on_disk(call_log))
        entries = MailCallLog.read_entries(call_log)
        self.assertEqual(1, len(entries))
        self.assertEqual("submit", entries[0]["event"])
        self.assertEqual(action.id, entries[0]["action_id"])
        self.assertTrue(entries[0]["idempotency_key"])
        print("[evidence] 跨进程账本 =", dump(entries))

    def test_unreadable_call_log_counts_as_zero(self) -> None:
        """账本不存在/为空时按 0 算：崩溃测试的基线必须是真实的 0，而不是猜出来的。"""
        self.assertEqual(0, MailCallLog.submitted_count_on_disk(self.path("nope.log")))
        empty = self.path("empty.log")
        with open(empty, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("")
        self.assertEqual(0, MailCallLog.submitted_count_on_disk(empty))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
