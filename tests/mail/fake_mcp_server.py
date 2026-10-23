#!/usr/bin/env python3
"""测试用的**假 MCP stdio 服务器**（scripted Python 子进程）。

它说的协议与真实 `mcp-email-server stdio` 相同：JSON-RPC 2.0、一行一条消息、
`initialize` → `notifications/initialized` → `tools/list` → `tools/call`。
它把收到的每一条请求写进 transcript（JSONL），供测试断言调用序列与入参
（例如 `mark_as_read` 必须恒为 false、绝不能出现 `send_email`）。

纯标准库 / Python 3.10 兼容；由 `tests/mail/test_mcp_stdio.py` 通过
`subprocess` 启动，不依赖 pytest 或任何第三方包。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

TOOLS = [
    "list_available_accounts",
    "list_email_tags",
    "list_emails_metadata",
    "list_allowed_recipients",
    "list_allowed_senders",
    "send_email",
    "list_mailboxes",
    "get_emails_content",
    "get_attachment_content",
    "delete_emails",
    "mark_emails_as_read",
]

WRITE_TOOLS = {
    "send_email",
    "forward_email",
    "save_to_mailbox",
    "save_draft",
    "delete_emails",
    "set_email_flags",
    "set_email_tags",
    "mark_emails_as_read",
    "move_emails",
    "archive_emails",
    "download_attachment",
}


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="scripted fake mcp-email-server (stdio)")
    parser.add_argument("--transcript", default="")
    parser.add_argument("--emails", default="")
    parser.add_argument("--folders", default="INBOX,Sent,Drafts")
    parser.add_argument("--hang-tool", default="")
    parser.add_argument("--hang-seconds", type=float, default=30.0)
    parser.add_argument("--crash-tool", default="")
    parser.add_argument("--crash-code", type=int, default=7)
    parser.add_argument("--rpc-error-tool", default="")
    parser.add_argument("--is-error-tool", default="")
    parser.add_argument("--tools-list-error", action="store_true")
    parser.add_argument("--structured", action="store_true", help="用 structuredContent 而不是 text JSON 返回")
    parser.add_argument("--garbage", type=int, default=0, help="在每次响应前先发 N 行垃圾输出")
    parser.add_argument("--notify-before-result", action="store_true")
    parser.add_argument("--secret-env", default="", help="启动时把这个环境变量的值（脱敏对象）打到 stderr")
    parser.add_argument("--truncate-body-at", type=int, default=0)
    parser.add_argument("--refuse-mark-as-read", action="store_true")
    parser.add_argument("--mutate-subject-after-list", default="",
                        help="第一次 list_emails_metadata 之后改写主题，模拟列取与取件之间的内容冲突"
                             "（注意：命令行参数会被父进程用文件系统编码编码，"
                             "非 ASCII 主题请改用 --mutate-subject-file）")
    parser.add_argument("--mutate-subject-file", default="",
                        help="同上，但主题从 UTF-8 文件读取："
                             "避免把非 ASCII 文本放进 argv（非 UTF-8 文件系统编码下 Popen 会直接失败）")
    parser.add_argument("--server-name", default="email")
    parser.add_argument("--server-version", default="1.11.1-fake")
    parser.add_argument("--report-encoding", action="store_true",
                        help="启动时把自身 stdio 编码打到 stderr（用于断言 UTF-8 强制项真的生效）")
    parser.add_argument("--raw-invalid-utf8", action="store_true",
                        help="在 tools/call 响应里写出非法 UTF-8 字节（验证父进程严格解码）")
    parser.add_argument("--raw-invalid-utf8-stderr", action="store_true",
                        help="在 stderr 写出非法 UTF-8 字节（验证日志通道的显式标注）")
    return parser.parse_args(argv)


class Server:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.emails: List[Dict[str, Any]] = []
        if args.emails:
            with open(args.emails, "r", encoding="utf-8") as handle:
                self.emails = json.load(handle)
        self.folders = [item for item in args.folders.split(",") if item]
        self.mutated = False
        # 非 ASCII 的改写主题一律走文件：argv 会被父进程按文件系统编码编码，
        # 在非 UTF-8 locale 下（例如 LC_ALL=C）根本传不进来。
        self.mutation = args.mutate_subject_after_list
        if args.mutate_subject_file:
            with open(args.mutate_subject_file, "r", encoding="utf-8") as handle:
                self.mutation = handle.read()
        self.transcript = open(args.transcript, "a", encoding="utf-8") if args.transcript else None

    # ------------------------------------------------------------------ io
    def record(self, entry: Dict[str, Any]) -> None:
        if self.transcript is None:
            return
        entry["at"] = time.time()
        self.transcript.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        self.transcript.flush()

    def log(self, text: str) -> None:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()

    def send(self, payload: Dict[str, Any]) -> None:
        for _ in range(self.args.garbage):
            sys.stdout.write("this is not json {\n")
        if self.args.notify_before_result and payload.get("id") is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/message",
                                         "params": {"level": "info", "data": "working"}}) + "\n")
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def result(self, request_id: Any, payload: Dict[str, Any]) -> None:
        """普通方法（initialize / tools/list）的结果：直接返回对象，不做 content 包裹。"""
        self.send({"jsonrpc": "2.0", "id": request_id, "result": payload})

    def ok(self, request_id: Any, structured: Dict[str, Any]) -> None:
        if self.args.raw_invalid_utf8:
            # 故意写出非法 UTF-8：父进程必须显式报错，绝不能替换成 U+FFFD
            body = json.dumps(structured, ensure_ascii=False).encode("utf-8")
            payload = (b'{"jsonrpc": "2.0", "id": ' + json.dumps(request_id).encode("ascii")
                       + b', "result": {"content": [{"type": "text", "text": "'
                       + b"\xff\xfe bad bytes " + body + b'"}], "isError": false}}\n')
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
            return
        if self.args.structured:
            result = {"content": [], "structuredContent": structured, "isError": False}
        else:
            result = {"content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}],
                      "isError": False}
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        error: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.send({"jsonrpc": "2.0", "id": request_id, "error": error})

    def is_error(self, request_id: Any, message: str) -> None:
        self.send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": message}], "isError": True},
        })

    # ------------------------------------------------------------------ 工具
    def tool_result(self, name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if name == "list_available_accounts":
            return {"result": [{"account_name": "fake", "account_type": "email",
                                "description": "fake account", "email_address": "fake@example.com",
                                "can_receive": True, "can_send": True}]}
        if name == "list_mailboxes":
            return {"result": [{"name": folder, "delimiter": "/", "flags": []} for folder in self.folders]}
        if name == "list_emails_metadata":
            return self.list_metadata(arguments)
        if name == "get_emails_content":
            return self.get_content(arguments)
        return None

    def list_metadata(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        mailbox = str(arguments.get("mailbox") or "INBOX")
        since = arguments.get("since")
        order = str(arguments.get("order") or "desc")
        page = int(arguments.get("page") or 1)
        page_size = int(arguments.get("page_size") or 10)
        rows = [item for item in self.emails if str(item.get("mailbox") or "INBOX") == mailbox]
        if since:
            since_stamp = _stamp(since)
            rows = [
                item
                for item in rows
                if since_stamp is not None and _stamp(item.get("date")) is not None
                and _stamp(item.get("date")) >= since_stamp
            ]
        rows = sorted(rows, key=lambda item: str(item.get("date") or ""), reverse=(order == "desc"))
        total = len(rows)
        start = (page - 1) * page_size
        window = rows[start:start + page_size]
        emails = [
            {
                "email_id": str(item.get("email_id")),
                "message_id": item.get("message_id"),
                "subject": item.get("subject", ""),
                "sender": item.get("sender", ""),
                "recipients": item.get("recipients", []),
                "date": item.get("date", ""),
                "attachments": item.get("attachments", []),
                "provider_keywords": item.get("provider_keywords", []),
                "semantic_tags": [],
            }
            for item in window
        ]
        response = {
            "page": page,
            "page_size": page_size,
            "before": None,
            "since": since,
            "subject": None,
            "emails": emails,
            "total": total,
        }
        if self.mutation and not self.mutated:
            # 在**返回元数据之后**改写：模拟「列取与取件之间内容发生变化」
            self.mutated = True
            for item in self.emails:
                item["subject"] = self.mutation
            self.log("DEBUG 已模拟列取后主题变化（内容冲突）")
        return response

    def get_content(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if self.args.refuse_mark_as_read and arguments.get("mark_as_read") is True:
            raise Refused("refused: 只读测试夹具拒绝 mark_as_read=true")
        wanted = [str(item) for item in arguments.get("email_ids") or []]
        offset = int(arguments.get("body_offset") or 0)
        window = int(arguments.get("max_body_length") or 20000)
        emails: List[Dict[str, Any]] = []
        failed: List[str] = []
        for email_id in wanted:
            found = None
            for item in self.emails:
                if str(item.get("email_id")) == email_id:
                    found = item
                    break
            if found is None:
                failed.append(email_id)
                continue
            body = str(found.get("body") or "")
            if self.args.truncate_body_at:
                body = body[: self.args.truncate_body_at]
            chunk = body[offset:offset + window]
            if offset + window < len(body):
                chunk = chunk + "...[TRUNCATED]"
            emails.append({
                "email_id": email_id,
                "message_id": found.get("message_id"),
                "subject": found.get("subject", ""),
                "sender": found.get("sender", ""),
                "recipients": found.get("recipients", []),
                "date": found.get("date", ""),
                "attachments": found.get("attachments", []),
                "provider_keywords": found.get("provider_keywords", []),
                "semantic_tags": [],
                "in_reply_to": found.get("in_reply_to"),
                "references": found.get("references"),
                "body": chunk,
            })
        return {
            "emails": emails,
            "requested_count": len(wanted),
            "retrieved_count": len(emails),
            "failed_ids": failed,
        }

    # ------------------------------------------------------------------ 主循环
    def serve(self) -> int:
        self.log("INFO fake mcp-email-server ready (pid=%d)" % os.getpid())
        if self.args.raw_invalid_utf8_stderr:
            # 绕过文本层直接写非法字节：父进程的 stderr 通道必须标注而不是静默吞掉
            sys.stderr.buffer.write(b"WARN bad stderr bytes \xff\xfe\xfd\n")
            sys.stderr.buffer.flush()
        if self.args.report_encoding:
            self.log(
                "ENVCHECK stdout=%s stdin=%s utf8_mode=%d PYTHONUTF8=%s PYTHONIOENCODING=%s"
                % (
                    sys.stdout.encoding,
                    sys.stdin.encoding,
                    sys.flags.utf8_mode,
                    os.environ.get("PYTHONUTF8", "<unset>"),
                    os.environ.get("PYTHONIOENCODING", "<unset>"),
                )
            )
        if self.args.secret_env:
            value = os.environ.get(self.args.secret_env, "<unset>")
            # 故意把凭据打到 stderr：测试要求适配器只把它写进脱敏后的日志
            self.log("DEBUG imap login failed user=owner password=%s token=%s" % (value, value))
        for raw in sys.stdin:
            text = raw.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                self.log("WARN 忽略非 JSON 输入")
                continue
            method = message.get("method")
            request_id = message.get("id")
            params = message.get("params") or {}
            entry: Dict[str, Any] = {"method": method, "id": request_id, "params": params}
            if method == "tools/call":
                entry["tool"] = params.get("name")
                entry["arguments"] = params.get("arguments") or {}
            self.record(entry)
            if method == "initialize":
                self.result(request_id, {
                    "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.args.server_name, "version": self.args.server_version},
                })
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                if self.args.tools_list_error:
                    self.error(request_id, -32603, "tools/list 内部错误 (fake)")
                    continue
                tools = [
                    {"name": name, "description": "fake tool %s" % name,
                     "inputSchema": {"type": "object", "properties": {}, "required": []},
                     "annotations": {"readOnlyHint": name not in WRITE_TOOLS}}
                    for name in TOOLS
                ]
                self.result(request_id, {"tools": tools})
            elif method == "tools/call":
                self.handle_tool_call(request_id, params)
            else:
                self.error(request_id, -32601, "method not found: %s" % method)
        return 0

    def handle_tool_call(self, request_id: Any, params: Dict[str, Any]) -> None:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if name in WRITE_TOOLS:
            self.record({"event": "WRITE_TOOL_CALLED", "tool": name})
            self.log("ERROR 写工具被调用：%s（测试会因此失败）" % name)
        if self.args.crash_tool and name == self.args.crash_tool:
            self.log("DEBUG 模拟崩溃：工具 %s 触发 os._exit(%d)" % (name, self.args.crash_code))
            sys.stderr.flush()
            os._exit(self.args.crash_code)
        if self.args.hang_tool and name == self.args.hang_tool:
            self.log("DEBUG 模拟挂起：%s 睡眠 %.1fs" % (name, self.args.hang_seconds))
            time.sleep(self.args.hang_seconds)
        if self.args.rpc_error_tool and name == self.args.rpc_error_tool:
            self.error(request_id, -32000, "fake rpc error for %s" % name, data={"tool": name})
            return
        if self.args.is_error_tool and name == self.args.is_error_tool:
            self.is_error(request_id, "fake isError payload for %s" % name)
            return
        try:
            structured = self.tool_result(name, arguments)
        except Refused as exc:
            self.is_error(request_id, str(exc))
            return
        if structured is None:
            self.error(request_id, -32602, "unknown tool: %s" % name)
            return
        self.ok(request_id, structured)


class Refused(Exception):
    pass


def _stamp(value: Any) -> Optional[float]:
    """把 ISO 时间串（带/不带时区）转成 epoch 秒；解析失败返回 None。"""
    if not value:
        return None
    text = str(value).strip()
    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")
    for candidate in candidates:
        try:
            from datetime import datetime

            return datetime.fromisoformat(candidate).timestamp()
        except (TypeError, ValueError):
            continue
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError):
        return None


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    server = Server(args)
    try:
        return server.serve()
    finally:
        if server.transcript is not None:
            server.transcript.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
