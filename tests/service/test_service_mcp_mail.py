"""服务侧真实邮件通道接线（`mail.source=mcp`）的测试。

要证明三件事，而且都是"设备上会真的发生"的事：

1. **没授权码也能常驻**：`mail.source=mcp` 但授权码文件不存在时，服务照常起来、只记一条
   指向用户动作的说明——不是崩、也不是偷偷接个假通道；
2. **接上了就真收信**：用 `tests/mail/fake_mcp_server.py` 当子进程，跑完整一轮，
   `SourceDocument` 真的进存储（这一段才是"会替你处理真邮件"的接线证据）；
3. **授权码不泄漏**：授权码必须进到子进程环境变量里（否则连不上），
   但**绝不能**出现在日志、状态快照或配置描述里。

真实 `mcp-email-server` 自身的行为由 MVP-2 的设备只读探针取证（`docs/mvp/mvp-2.md §4.8`）；
本文件证明的是**服务这一侧**：命令、环境、凭据来源、失败记账。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

from tests.mail.test_mcp_stdio import FAKE_SERVER, email_record
from tests.service.support import ServiceWorkbench, write_text

#: 假授权码：故意做成"一眼能认出来"的字符串，便于断言它没出现在任何输出里。
FAKE_AUTHCODE = "AUTHCODE-must-not-leak-9f3a1c"
ACCOUNT = "owner@example.com"


class _MailMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mvp8-mcp-")
        self.root = self._tmp.name
        self.wb = ServiceWorkbench(self.root)
        self.fixtures = os.path.join(self.root, "fixtures")
        os.makedirs(self.fixtures, exist_ok=True)

    def tearDown(self) -> None:
        self.wb.close_runtimes()
        self._tmp.cleanup()

    # ------------------------------------------------------------------ 夹具
    def authcode(self, text: str) -> str:
        """按部署侧的样子写授权码：目录 700 + 文件 600。"""
        path = os.path.join(self.wb.config_dir, "mail-authcode.txt")
        write_text(path, text)
        os.chmod(path, 0o600)
        return path

    def mail_overrides(self, **extra: object) -> dict:
        overrides = {
            "enabled": True,
            "source": "mcp",
            "account": ACCOUNT,
            "folder": "INBOX",
            "create_conversation": True,
        }
        overrides.update(extra)
        return overrides

    def config(self, **extra: object) -> str:
        return self.wb.write_config("service-mcp.json", mail=self.mail_overrides(**extra))


class McpMailWiringTest(_MailMixin):
    """接线规则：什么时候接、什么时候只记账（都不该让服务倒下）。"""

    def test_missing_authcode_degrades_to_a_note_instead_of_crashing(self) -> None:
        config_path = self.config(authcode_path=os.path.join(self.wb.config_dir, "mail-authcode.txt"))
        runtime = self.wb.runtime(config_path)

        self.assertIsNone(runtime.mail_engine, "没有授权码时不得接一个假通道")
        notes = " | ".join(runtime.state.notes)
        self.assertIn("授权码", notes, "必须说清是缺授权码")
        self.assertIn("set-mail-authcode.sh", notes, "说明要指向用户动作（怎么办）")
        report = runtime.run_once()
        self.assertTrue(report.ok, "缺授权码不该让整个轮次失败（降级只影响收信）")

    def test_missing_binary_is_reported_with_the_configured_command(self) -> None:
        config_path = self.config(
            authcode_path=self.authcode(FAKE_AUTHCODE),
            mcp_command=["/nope/mcp-email-server", "stdio"],
        )
        runtime = self.wb.runtime(config_path)

        self.assertIsNone(runtime.mail_engine)
        notes = " | ".join(runtime.state.notes)
        self.assertIn("/nope/mcp-email-server", notes, "要说清是哪个可执行文件找不到")

    def test_unknown_source_is_named_in_the_notes(self) -> None:
        config_path = self.wb.write_config(
            "service-unknown.json", mail={"enabled": True, "source": "carrier-pigeon"}
        )
        runtime = self.wb.runtime(config_path)
        self.assertIsNone(runtime.mail_engine)
        self.assertIn("carrier-pigeon", " | ".join(runtime.state.notes))

    def test_authcode_is_read_from_file_with_whitespace_stripped(self) -> None:
        from asistent.service.mail_source import read_authcode

        path = self.authcode("  %s \r\n" % FAKE_AUTHCODE)
        self.assertEqual(read_authcode(path), FAKE_AUTHCODE)
        self.assertEqual(read_authcode(os.path.join(self.wb.config_dir, "nope.txt")), "")
        self.assertEqual(read_authcode(""), "")

    def test_config_description_never_carries_the_secret_value(self) -> None:
        config_path = self.config(authcode_path=self.authcode(FAKE_AUTHCODE))
        from asistent.service.config import load_config

        described = json.dumps(load_config(config_path).describe(), ensure_ascii=False)
        self.assertNotIn(FAKE_AUTHCODE, described)
        payload = json.loads(described)
        self.assertTrue(payload["mail"]["credential_present"], "要能看出'凭据已就位'")
        self.assertEqual(payload["mail"]["mcp_env_keys"], 0)


class McpMailRoundTest(_MailMixin):
    """接上假 MCP 服务器跑真轮次：文档进库、游标落地、授权码不泄漏。"""

    def setUp(self) -> None:
        super().setUp()
        self.emails = os.path.join(self.fixtures, "emails.json")
        with open(self.emails, "w", encoding="utf-8") as handle:
            json.dump(
                [
                    email_record(
                        7,
                        subject="家长会通知",
                        sender="Teacher <teacher@example.org>",
                        body="本周五下午三点家长会，请回复是否参加。",
                    )
                ],
                handle,
                ensure_ascii=False,
            )
        self.transcript = os.path.join(self.fixtures, "transcript.jsonl")

    def _command(self) -> list:
        # `--secret-env` 会让假服务器把该环境变量的值打到 stderr：
        # 既证明"授权码确实传进了子进程"，也用来验证脱敏。
        return [
            sys.executable,
            FAKE_SERVER,
            "--transcript",
            self.transcript,
            "--emails",
            self.emails,
            "--secret-env",
            "MCP_EMAIL_SERVER_PASSWORD",
        ]

    def _runtime(self):
        config_path = self.config(
            authcode_path=self.authcode(FAKE_AUTHCODE), mcp_command=self._command()
        )
        return self.wb.runtime(config_path)

    def test_round_syncs_documents_through_the_mcp_source(self) -> None:
        runtime = self._runtime()
        self.assertIsNotNone(runtime.mail_engine, "配好了就该接上真通道")

        report = runtime.run_once()
        self.assertTrue(report.ok)

        documents = runtime.store.find("SourceDocument", limit=None)
        print("[evidence] MCP 通道一轮后 SourceDocument 数 =", len(documents))
        self.assertGreaterEqual(len(documents), 1, "走真通道的一轮必须真的建出文档")

        # 到这一步"这封信真的进来了"要能看出来：主题会落在由它派生的事务上
        cases = runtime.store.find("Case", limit=None)
        blob = json.dumps([item.to_dict() for item in cases], ensure_ascii=False)
        print("[evidence] Case 数 =", len(cases))
        self.assertIn("家长会通知", blob, "邮件主题必须能在派生出的对象里看到")

        # 假服务器收到了 initialize + tools/list（说明握手真的发生了）
        with open(self.transcript, "r", encoding="utf-8") as handle:
            methods = [json.loads(line)["method"] for line in handle if line.strip()]
        print("[evidence] stdio 方法序列 =", methods)
        self.assertIn("initialize", methods)
        self.assertIn("tools/list", methods)

        self.assertTrue(
            os.path.exists(runtime.config.mail.state_path), "同步游标必须持久化（否则每轮重收）"
        )

    def test_second_round_does_not_duplicate_the_same_mail(self) -> None:
        runtime = self._runtime()
        runtime.run_once()
        first = len(runtime.store.find("SourceDocument", limit=None))
        runtime.run_once()
        second = len(runtime.store.find("SourceDocument", limit=None))
        print("[evidence] 两轮后的文档数 =", first, "->", second)
        self.assertEqual(first, second, "同一封信不能被收两次（幂等）")

    def test_authcode_is_passed_to_the_child_env_and_registered_for_redaction(self) -> None:
        """阳性证明：授权码**确实**进了子进程环境变量（否则真机连不上），
        同时被登记为脱敏值（否则它会顺着 stderr 泄进日志）。"""
        from asistent.service.config import load_config
        from asistent.service.mail_source import build_mcp_mail_source

        config_path = self.config(
            authcode_path=self.authcode(FAKE_AUTHCODE),
            mcp_command=[sys.executable, FAKE_SERVER, "--transcript", self.transcript],
        )
        config = load_config(config_path)
        source, why = build_mcp_mail_source(config)

        self.assertEqual(why, "")
        self.assertIsNotNone(source, "配好了就该构造出真通道")
        child_env = source.client.child_env
        self.assertEqual(child_env["MCP_EMAIL_SERVER_PASSWORD"], FAKE_AUTHCODE)
        self.assertEqual(child_env["MCP_EMAIL_SERVER_EMAIL_ADDRESS"], ACCOUNT)
        self.assertEqual(child_env["MCP_EMAIL_SERVER_IMAP_HOST"], "imap.163.com")
        self.assertEqual(child_env["MCP_EMAIL_SERVER_IMAP_PORT"], "993")
        self.assertEqual(child_env["MCP_EMAIL_SERVER_IMAP_SSL"], "true")

        # 脱敏登记：客户端按键名把"像秘密"的值收进来，日志里只会出现 <redacted>
        self.assertIn(FAKE_AUTHCODE, source.client.redactor.secrets)
        redacted = source.client.redactor.redact("password=%s token=%s" % (FAKE_AUTHCODE, FAKE_AUTHCODE))
        self.assertNotIn(FAKE_AUTHCODE, redacted)
        self.assertIn("redacted", redacted.lower())

    def test_authcode_never_appears_in_logs_snapshot_or_config(self) -> None:
        runtime = self._runtime()
        runtime.run_once()

        log_path = os.path.join(runtime.config.logs_dir, "service.log")
        log_text = ""
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                log_text = handle.read()
        mcp_log_path = os.path.join(runtime.config.logs_dir, "mail-mcp.log")
        mcp_log = ""
        if os.path.exists(mcp_log_path):
            with open(mcp_log_path, "r", encoding="utf-8", errors="replace") as handle:
                mcp_log = handle.read()
        snapshot_text = ""
        state_path = runtime.state.state_path or os.path.join(
            runtime.config.state_dir, "service-state.json"
        )
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8", errors="replace") as handle:
                snapshot_text = handle.read()

        # 阴性对照：假服务器**确实**把密码打到了 stderr，所以"没出现"不是因为什么都没发生
        self.assertIn(
            "imap login failed",
            mcp_log,
            "子进程 stderr 必须落盘（否则真机出问题没法查），且它确实回显了凭据",
        )
        self.assertNotIn(FAKE_AUTHCODE, mcp_log, "授权码绝不能出现在子进程日志里")
        self.assertNotIn(FAKE_AUTHCODE, log_text, "授权码绝不能出现在服务日志里")
        self.assertNotIn(FAKE_AUTHCODE, snapshot_text, "授权码绝不能出现在状态快照里")
        self.assertNotIn(
            FAKE_AUTHCODE,
            json.dumps(runtime.config.describe(), ensure_ascii=False),
            "授权码绝不能出现在配置描述里",
        )


if __name__ == "__main__":
    unittest.main()
