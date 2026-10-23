"""邮件通道的 stdio 适配器：以子进程方式驱动 `mcp-email-server`（JSON-RPC 2.0 over stdio）。

调用序列（MCP stdio，实测见 docs/mvp/mvp-2.md）
==============================================

    spawn: <server> stdio
    -> initialize                     (protocolVersion / capabilities / clientInfo)
    <- result: protocolVersion + serverInfo
    -> notifications/initialized      (无 id)
    -> tools/list                     (只读探测：拿到工具名与入参 schema)
    -> tools/call  list_mailboxes        列邮箱（文件夹映射）
    -> tools/call  list_emails_metadata  取新邮件元数据（since/order/page/page_size）
    -> tools/call  get_emails_content    取正文（mark_as_read 恒为 false）

安全约束（任务硬性要求）
========================

1. **凭据不进代码/日志/审计**：凭据只存在于服务器自己的配置/密钥环里，本适配器
   从不接收、不打印、不返回凭据。父进程传给子进程的环境变量与任何看起来像
   `password=/token:` 的内容都先过 `Redactor` 再进日志；审计只由同步层写，
   且同步层拿不到环境变量。
2. **子进程 stderr 只进日志**：stderr 由独立线程读取、逐行脱敏、追加写日志文件
   （默认权限 0600），绝不写 stdout、绝不写审计。
3. **崩溃可检测**：stdout EOF / 进程退出 / 管道破裂 → `McpCrashed`（带退出码与
   stderr 尾巴），不会假装成"没有新邮件"。
4. **超时有界**：每次请求都有 deadline（默认 30s），reader 线程 + 队列实现，
   超时抛 `McpTimeout`，不会无限挂住。
5. **只读模式**：`read_only=True`（默认）时任何写工具名都会先被本地拒绝
   （`WriteNotPermitted`）；适配器本身只调用 4 个读工具，没有发送路径。

已知限制（见 docs/mvp/mvp-2.md）：MCP 不返回 RFC822 原始字节，`fetch_raw()`
返回的是**确定性重建**的邮件字节（头部 + 正文），因此身份摘要用的是
「元数据投影摘要」，不是 IMAP 直连时的 raw 摘要。
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ..contracts.ids import digest_of, sha256_bytes
from ..contracts.mail import MailMessageRef
from ..contracts.objects import CONTENT_ORIGIN_METADATA_PROJECTION
from ..contracts.timeutil import utcnow_iso
from .errors import (
    CursorInvalid,
    McpContentError,
    McpCrashed,
    McpHandshakeError,
    McpProtocolError,
    McpTimeout,
    McpToolError,
    WriteNotPermitted,
)

__all__ = [
    "CHILD_UTF8_ENV",
    "CONTENT_ORIGIN_PROJECTION",
    "DEFAULT_PROTOCOL_VERSION",
    "READ_ONLY_TOOLS",
    "STDIO_ENCODING",
    "WRITE_TOOLS",
    "Redactor",
    "LogSink",
    "StdioJsonRpcClient",
    "McpToolNames",
    "McpMailSource",
]

#: 契约 v0.4：在**构造引用**时声明摘要来源（CP-20261010-07）。
#: mcp-email-server 不暴露 RFC822 原始字节，所以这里的 `content_digest` / 文档 `content_hash`
#: 只是**元数据投影摘要**——只能证明"投影一致"，不能当字节级完整性证明。
#: 判据见 `asistent.contracts.objects.SourceDocument.content_origin`。
#:
#: 契约 v0.7（CP-20261010-18）：取值绑定到契约常量，不再写字面量——
#: 各模块各写一份字面量时，任何拼写漂移都会让"投影 vs 字节级"判据静默错配。
CONTENT_ORIGIN_PROJECTION = CONTENT_ORIGIN_METADATA_PROJECTION

#: 子进程 stdio 的强制项：**不依赖操作系统 locale / 控制台代码页**。
#: Windows 默认代码页是 cp936(gbk)，不强制的话子进程会用 ANSI 代码页收发 JSON-RPC，
#: 中文主题/附件名会在子进程侧被替换成 U+FFFD（`\xef\xbf\xbd`）——静默数据损坏。
#: 这两项连同父进程侧的严格 UTF-8 解码一起，保证"中文邮件逐字节往返"。
CHILD_UTF8_ENV = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
}
#: UTF-8 严格解码用的编码名（父进程侧）
STDIO_ENCODING = "utf-8"

DEFAULT_PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "asistent-mvp2-mail"
CLIENT_VERSION = "0.1.0"

#: 读工具：只读取，不改变远端状态
READ_ONLY_TOOLS = frozenset(
    {
        "list_available_accounts",
        "list_email_tags",
        "list_emails_metadata",
        "list_allowed_recipients",
        "list_allowed_senders",
        "list_mailboxes",
        "get_attachment_content",
        # 服务端把它的 readOnlyHint 标为 false，因为 mark_as_read=true 会改远端标记；
        # 本适配器恒发 mark_as_read=false，并在只读模式外仍禁止其它写工具。
        "get_emails_content",
    }
)

#: 写工具：只读模式下本地直接拒绝；同步层根本不会调用它们
WRITE_TOOLS = frozenset(
    {
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
)

_SECRET_INLINE = re.compile(
    r"(?i)\b(pass(?:word|wd)?|secret|token|api[_-]?key|apikey|credential|authorization)\b\s*[:=]\s*\S+"
)
#: `scheme://user:password@host` 形态
_SECRET_URL = re.compile(r"(?i)([a-z][a-z0-9+.\-]*://[^:/\s@]+:)([^@\s]+)(@)")
_REDACTED = "***REDACTED***"


class Redactor:
    """把已知秘密值与内联 `password=...` 形态都替换掉。"""

    def __init__(self, secrets: Optional[Iterable[str]] = None) -> None:
        self._secrets: List[str] = []
        for value in secrets or ():
            self.add(value)

    def add(self, value: Any) -> None:
        text = "" if value is None else str(value)
        # 太短的值（如 "1"）匹配会把正常文本吃掉，只登记有辨识度的长度
        if len(text) >= 6 and text not in self._secrets:
            self._secrets.append(text)
        self._secrets.sort(key=len, reverse=True)

    def extend(self, values: Iterable[Any]) -> None:
        for value in values:
            self.add(value)

    @property
    def secrets(self) -> List[str]:
        return list(self._secrets)

    def redact(self, text: Optional[str]) -> str:
        if not text:
            return ""
        out = str(text)
        for secret in self._secrets:
            if secret and secret in out:
                out = out.replace(secret, _REDACTED)
        out = _SECRET_URL.sub(lambda match: match.group(1) + _REDACTED + match.group(3), out)
        return _SECRET_INLINE.sub(
            lambda match: match.group(0).split(":")[0].split("=")[0] + "=" + _REDACTED, out
        )


class LogSink:
    """子进程 stderr 的落点：逐行追加到文件（0600）+ 内存保留最近若干行。

    它**不是**审计通道：日志失败也不能影响同步；审计只由同步层写共享对象存储。
    """

    def __init__(self, path: Optional[str] = None, *, memory_lines: int = 200) -> None:
        self.path = os.path.abspath(path) if path else None
        self.memory_lines = int(memory_lines)
        self._lines: List[str] = []
        self._lock = threading.Lock()
        self.write_failures = 0
        if self.path:
            directory = os.path.dirname(self.path)
            if directory and not os.path.isdir(directory):
                try:
                    os.makedirs(directory, mode=0o700, exist_ok=True)
                except OSError:
                    self.path = None

    def write(self, line: str) -> None:
        clean = line.rstrip("\n")
        with self._lock:
            self._lines.append(clean)
            if len(self._lines) > self.memory_lines:
                del self._lines[: len(self._lines) - self.memory_lines]
            path = self.path
        if not path:
            return
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(clean + "\n")
            except BaseException:
                raise
        except OSError:
            with self._lock:
                self.write_failures += 1

    def tail(self, limit: int = 20) -> List[str]:
        with self._lock:
            return list(self._lines[-limit:])

    def all_lines(self) -> List[str]:
        with self._lock:
            return list(self._lines)


@dataclass
class _Line:
    kind: str  # message / eof / error
    payload: Any = None


class StdioJsonRpcClient:
    """最小 JSON-RPC 2.0 over stdio 客户端（带超时、崩溃检测、脱敏日志）。"""

    def __init__(
        self,
        command: List[str],
        *,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: float = 30.0,
        log: Optional[LogSink] = None,
        redactor: Optional[Redactor] = None,
        popen_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not command:
            raise ValueError("必须给出 mcp-email-server 的命令行")
        self.command = [str(item) for item in command]
        self.cwd = cwd
        self.timeout = float(timeout)
        self.log = log or LogSink()
        self.redactor = redactor or Redactor()
        if env:
            # 只登记「看起来是秘密」的环境变量值用于脱敏，绝不记录键值本身
            self.redactor.extend(
                value
                for key, value in env.items()
                if isinstance(value, str) and _looks_secret_key(key)
            )
        self._extra_env = dict(env or {})
        self._popen_factory = popen_factory or subprocess.Popen
        self._proc: Optional[Any] = None
        self._inbox: "queue.Queue[_Line]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._err_reader: Optional[threading.Thread] = None
        self._next_id = 0
        self._lock = threading.RLock()
        #: 只读诊断信息（不含凭据）：请求方法序列 + 噪声计数
        self.call_log: List[Dict[str, Any]] = []
        self.notifications: List[Dict[str, Any]] = []
        self.protocol_noise = 0
        self.stderr_decode_failures = 0
        self.started_at: Optional[str] = None

    # ------------------------------------------------------------------ 生命周期
    @property
    def child_env(self) -> Dict[str, str]:
        """子进程实际会收到的环境变量（只读快照，便于测试断言 UTF-8 强制项）。

        顺序固定为「父环境 → 显式 env= 覆盖 → UTF-8 强制项」，
        因此调用方给什么都不会让子进程落到 ANSI 代码页上。
        """
        env = dict(os.environ)
        env.update(self._extra_env)
        env.update(CHILD_UTF8_ENV)
        return env

    def start(self) -> "StdioJsonRpcClient":
        with self._lock:
            if self._proc is not None:
                return self
            full_env = self.child_env
            kwargs: Dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "cwd": self.cwd,
                "env": full_env,
                "bufsize": 0,
                "close_fds": True,
            }
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):  # pragma: no cover - 平台差异
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                self._proc = self._popen_factory(self.command, **kwargs)
            except UnicodeEncodeError as exc:
                # Popen 用**父进程**的文件系统编码把 argv 编成字节：在非 UTF-8 locale
                # （例如 LC_ALL=C，fsencoding=ascii / zh_CN.GBK）下，非 ASCII 参数在这里就失败，
                # 子进程根本没起来。必须显式说明原因，不能让调用方看到"未预期失败"。
                raise McpCrashed(
                    "无法启动 mcp 子进程：命令行参数含当前文件系统编码（%s）无法表示的字符：%s"
                    % (sys.getfilesystemencoding(), exc),
                    detail={
                        "command0": _safe_command_part(self.command[0]),
                        "fs_encoding": sys.getfilesystemencoding(),
                        "hint": "改用 UTF-8 locale（例如 LANG=C.UTF-8），或把非 ASCII 内容通过文件/环境之外的通道传入",
                    },
                ) from exc
            except OSError as exc:
                raise McpCrashed(
                    "无法启动 mcp 子进程：%s" % exc, detail={"command0": _safe_command_part(self.command[0])}
                ) from exc
            self.started_at = utcnow_iso()
            # 重新启动时清空上一个进程遗留的 EOF/错误行，否则新请求会立刻读到旧 EOF
            self._inbox = queue.Queue()
            self._reader = threading.Thread(target=self._read_stdout, name="mcp-stdout", daemon=True)
            self._reader.start()
            self._err_reader = threading.Thread(target=self._read_stderr, name="mcp-stderr", daemon=True)
            self._err_reader.start()
            return self

    def close(self, *, timeout: float = 5.0) -> Optional[int]:
        with self._lock:
            proc = self._proc
            if proc is None:
                return None
        try:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:  # pragma: no cover - 顽固子进程
                    proc.kill()
                    try:
                        proc.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            for thread in (self._reader, self._err_reader):
                if thread is not None:
                    thread.join(timeout=timeout)
            # 读线程退出后显式关管道，避免 ResourceWarning（子进程对象仍保留 returncode）
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):  # pragma: no cover - 已关闭/读线程占用
                        pass
            self._proc = None
        return proc.returncode

    def is_alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def returncode(self) -> Optional[int]:
        proc = self._proc
        return None if proc is None else proc.poll()

    def stderr_tail(self, limit: int = 20) -> List[str]:
        return self.log.tail(limit)

    # ------------------------------------------------------------------ 请求
    def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
        tool: Optional[str] = None,
    ) -> Any:
        self._ensure_alive()
        request_id = None
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            self._write(payload)
        deadline = time.monotonic() + float(timeout if timeout is not None else self.timeout)
        while True:
            message = self._next_message(deadline, method=method, tool=tool)
            if message.get("id") != request_id:
                self.protocol_noise += 1
                self._log("忽略 id 不匹配的响应：%r" % (message.get("id"),))
                continue
            if "error" in message:
                error = message.get("error") or {}
                code = error.get("code")
                text = _error_text(error)
                detail = {"method": method, "rpc_code": code}
                if tool:
                    detail["tool"] = tool
                exc_type = McpToolError if tool else McpProtocolError
                self._record_call(method, ok=False, tool=tool, error=text)
                raise exc_type("MCP 调用失败：%s" % text, detail=detail)
            self._record_call(method, ok=True, tool=tool)
            return message.get("result")

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._ensure_alive()
        with self._lock:
            payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            self._write(payload)

    def call_tool(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
        read_only: bool = True,
    ) -> Dict[str, Any]:
        """调用一个工具并返回结构化结果（`structuredContent` 或 text 里的 JSON）。"""
        if read_only and name in WRITE_TOOLS:
            raise WriteNotPermitted(
                "只读模式拒绝调用写工具：%s" % name, detail={"tool": name, "phase": "READ_ONLY_GUARD"}
            )
        result = self.request(
            "tools/call",
            {"name": name, "arguments": dict(arguments or {})},
            timeout=timeout,
            tool=name,
        )
        return self._structured(result, tool=name)

    # ------------------------------------------------------------------ 内部
    def _ensure_alive(self) -> None:
        if self._proc is None:
            self.start()
        proc = self._proc
        assert proc is not None
        code = proc.poll()
        if code is not None:
            raise McpCrashed(
                "mcp 子进程已退出（exit=%r），无法继续请求" % (code,),
                detail={"returncode": code, "stderr_tail": self.stderr_tail()},
            )

    def _write(self, payload: Dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpCrashed("mcp 子进程 stdin 不可用")
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode(STDIO_ENCODING, "strict")
        try:
            proc.stdin.write(raw)
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            code = proc.poll()
            raise McpCrashed(
                "写入 mcp 子进程失败（可能已崩溃）：%s" % exc,
                detail={"returncode": code, "stderr_tail": self.stderr_tail()},
            ) from exc

    def _next_message(self, deadline: float, *, method: str, tool: Optional[str] = None) -> Dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpTimeout(
                    "等待 %s 响应超时（上限 %.1fs）" % (method, self.timeout),
                    detail=self._channel_detail(method, tool, extra={"stderr_tail": self.stderr_tail()}),
                )
            try:
                line = self._inbox.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                self._ensure_not_crashed(method=method, tool=tool)
                continue
            if line.kind == "eof":
                raise McpCrashed(
                    "mcp 子进程在等待 %s 响应时关闭了 stdout（exit=%r）" % (method, line.payload),
                    detail=self._channel_detail(method, tool, extra={
                        "returncode": line.payload, "stderr_tail": self.stderr_tail(),
                    }),
                )
            if line.kind == "error":
                raise McpProtocolError(
                    "读取 mcp 子进程输出失败：%s" % line.payload,
                    detail=self._channel_detail(method, tool, extra={"stderr_tail": self.stderr_tail()}),
                )
            if line.kind == "decode_error":
                info = line.payload if isinstance(line.payload, dict) else {}
                where = "工具 %s" % tool if tool else method
                raise McpProtocolError(
                    "mcp 子进程输出的不是合法 UTF-8（%s 响应，字节偏移 %s，附近字节 %s）"
                    % (where, info.get("offset"), info.get("hex")),
                    detail=self._channel_detail(method, tool, extra={
                        "byte_offset": info.get("offset"),
                        "hex": info.get("hex"),
                        "reason": info.get("reason"),
                        "hint": "子进程 stdio 必须强制 UTF-8（PYTHONUTF8=1 / PYTHONIOENCODING=utf-8）",
                        "stderr_tail": self.stderr_tail(),
                    }),
                )
            text = str(line.payload or "").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                self.protocol_noise += 1
                self._log("忽略非 JSON 输出：%s" % _head(text))
                continue
            if not isinstance(message, dict):
                self.protocol_noise += 1
                continue
            if "id" not in message:
                self.notifications.append(message)
                continue
            return message

    @staticmethod
    def _channel_detail(method: str, tool: Optional[str], *, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """通道级错误（超时/崩溃/协议）的统一 detail：始终带工具名（有的话）。"""
        detail: Dict[str, Any] = {"method": method}
        if tool:
            detail["tool"] = tool
        if extra:
            detail.update(extra)
        return detail

    def _ensure_not_crashed(self, *, method: str, tool: Optional[str] = None) -> None:
        proc = self._proc
        if proc is None:
            return
        code = proc.poll()
        if code is not None:
            raise McpCrashed(
                "mcp 子进程已退出（exit=%r），%s 无响应" % (code, method),
                detail=self._channel_detail(method, tool, extra={
                    "returncode": code, "stderr_tail": self.stderr_tail(),
                }),
            )

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        offset = 0
        try:
            for raw in iter(proc.stdout.readline, b""):
                if not raw:
                    break
                try:
                    # 严格解码：任何非法字节都必须显式报错，绝不静默替换成 U+FFFD
                    # （中文主题/附件名一旦被替换，"成功"的同步其实写进了损坏数据）
                    text = raw.decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    self._inbox.put(_Line("decode_error", {
                        "offset": offset + int(exc.start),
                        "hex": raw[max(0, int(exc.start) - 16): int(exc.end) + 16].hex(),
                        "reason": str(exc.reason),
                    }))
                    offset += len(raw)
                    continue
                offset += len(raw)
                self._inbox.put(_Line("message", text))
        except (OSError, ValueError) as exc:  # pragma: no cover - 管道异常
            self._inbox.put(_Line("error", "%s: %s" % (type(exc).__name__, exc)))
        finally:
            code = proc.poll()
            if code is None:
                try:
                    code = proc.wait(timeout=5.0)
                except Exception:  # pragma: no cover - 退出码拿不到也不能丢 EOF 通知
                    code = None
            self._inbox.put(_Line("eof", code))

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            for raw in iter(proc.stderr.readline, b""):
                if not raw:
                    break
                try:
                    text = raw.decode("utf-8", "strict")
                except UnicodeDecodeError:
                    # 日志通道按"尽量可读"处理，但显式标注，不假装是正常文本
                    self.stderr_decode_failures += 1
                    text = "[非 UTF-8 字节，%d 字节已丢弃] %s" % (
                        len(raw), raw.decode("utf-8", "replace"),
                    )
                self._log(text)
        except (OSError, ValueError):  # pragma: no cover - 管道异常
            return

    def _log(self, line: str) -> None:
        """stderr / 噪声统一入口：先脱敏，再只写日志（绝不写 stdout / 审计）。"""
        self.log.write(self.redactor.redact(line))

    def _record_call(self, method: str, *, ok: bool, tool: Optional[str] = None, error: str = "") -> None:
        entry: Dict[str, Any] = {"method": method, "ok": bool(ok), "at": utcnow_iso()}
        if tool:
            entry["tool"] = tool
        if error:
            entry["error"] = self.redactor.redact(error)
        self.call_log.append(entry)

    @staticmethod
    def _structured(result: Any, *, tool: str) -> Dict[str, Any]:
        if not isinstance(result, dict):
            raise McpContentError(
                "tools/call 返回类型不是对象：%s" % type(result).__name__, detail={"tool": tool}
            )
        if result.get("isError"):
            raise McpToolError(
                "工具返回错误：%s" % _error_text_from_content(result),
                detail={"tool": tool, "is_error": True},
            )
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
        raise McpContentError("tools/call 返回内容无法解析为结构化结果", detail={"tool": tool})


# --------------------------------------------------------------------------------------
# mcp-email-server 的 MailSource 实现
# --------------------------------------------------------------------------------------


@dataclass
class McpToolNames:
    """工具名映射（默认值来自设备实测的 19 个工具；可用参数覆盖以适配版本差异）。"""

    accounts: str = "list_available_accounts"
    folders: str = "list_mailboxes"
    metadata: str = "list_emails_metadata"
    content: str = "get_emails_content"
    attachment: str = "get_attachment_content"


CURSOR_VERSION = 1
#: 服务端在正文被截断时追加的标记（实测 mcp-email-server 的 body 语义）
_TRUNCATION_MARKER = "...[TRUNCATED]"


class McpMailSource:
    """以 `mcp-email-server` 为后端的 `MailSource` 实现。


    - `list_new(cursor)` 用 `since`（INTERNALDATE，含边界）+ `order=asc` + 分页，
      游标是版本化的 JSON（含账号/文件夹/窗口起点），跨账号或跨文件夹的游标会被
      判为失效（`CursorInvalid`），从而触发重建；
    - `fetch_raw(ref)` 用 `get_emails_content`（`mark_as_read` 恒为 false，
      正文超长时按 `...[TRUNCATED]` 标记翻页）拼出**确定性重建**的邮件字节；
    - 身份摘要：MCP 不暴露 raw 字节，因此 `content_digest` 用**元数据投影摘要**
      （email_id + message_id + 主题 + 发件人 + 排序后的收件人/附件 + 日期），
      并在引用与文档上显式声明 `content_origin=METADATA_PROJECTION`（契约 v0.4，
      CP-20261010-07）：下游据此知道"这只是投影摘要，不能当字节级完整性证明"。
      取件时若投影与列取时不一致 → `McpContentError`（同一 UID 上出现内容冲突，
      交给同步层隔离并记录），不会静默把新内容挂到旧身份上。
    - 可选能力（契约 v0.4，CP-20261010-08）：`cursor_position()` 曝露"同步到哪了"
      给控制台/巡检（无参、返回字典，无信息时返回 `None`）；`cursor_rank(cursor)`
      是**本实现额外**提供的数值比较钩子，仅供同步层做游标回退检测
      （它不是契约成员，调用方必须 `getattr` 探测）。
    """

    name = "mcp_stdio"

    def __init__(
        self,
        client: StdioJsonRpcClient,
        *,
        account: str,
        folder: str = "INBOX",
        tool_names: Optional[McpToolNames] = None,
        page_size: int = 50,
        max_pages: int = 20,
        lookback_days: int = 30,
        overlap_seconds: int = 3600,
        max_body_length: int = 100000,
        max_body_pages: int = 8,
        read_only: bool = True,
        clock: Optional[Callable[[], str]] = None,
        now_fn: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.client = client
        self.account = account
        self.folder = folder
        self.tools = tool_names or McpToolNames()
        self.page_size = max(1, min(int(page_size), 100))
        self.max_pages = max(1, int(max_pages))
        self.lookback_days = max(1, int(lookback_days))
        self.overlap_seconds = max(0, int(overlap_seconds))
        self.max_body_length = max(1, min(int(max_body_length), 100000))
        self.max_body_pages = max(1, int(max_body_pages))
        self.read_only = bool(read_only)
        self._clock = clock or utcnow_iso
        self._now_fn = now_fn
        self._ready = False
        self.server_info: Dict[str, Any] = {}
        self.available_tools: List[str] = []
        self.read_only_tool_calls: List[str] = []
        #: 最近一次列取后的"同步位置"快照（供契约可选能力 `cursor_position()` 观测）
        self._position: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ 连接与握手
    def connect(self, *, timeout: Optional[float] = None) -> "McpMailSource":
        if self._ready:
            return self
        self.client.start()
        result = self.client.request(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
            timeout=timeout,
        )
        if not isinstance(result, dict) or not result.get("protocolVersion"):
            raise McpHandshakeError(
                "initialize 响应缺少 protocolVersion",
                detail={"keys": sorted(result.keys()) if isinstance(result, dict) else type(result).__name__},
            )
        self.server_info = dict(result.get("serverInfo") or {})
        self.client.notify("notifications/initialized")
        listed = self.client.request("tools/list", {}, timeout=timeout)
        tools = listed.get("tools") if isinstance(listed, dict) else None
        if not isinstance(tools, list):
            raise McpHandshakeError("tools/list 响应缺少 tools 数组")
        self.available_tools = [
            str(item.get("name")) for item in tools if isinstance(item, dict) and item.get("name")
        ]
        required = {
            self.tools.folders: "列邮箱",
            self.tools.metadata: "取新邮件元数据",
            self.tools.content: "取原始邮件",
        }
        missing = sorted(name for name in required if name not in self.available_tools)
        if missing:
            raise McpHandshakeError(
                "服务器缺少必需工具：%s" % ", ".join(missing),
                detail={"missing": missing, "available": sorted(self.available_tools)[:64]},
            )
        self._ready = True
        return self

    def close(self, *, timeout: float = 5.0) -> Optional[int]:
        return self.client.close(timeout=timeout)

    # ------------------------------------------------------------------ MailSource
    def list_folders(self) -> List[str]:
        self._ensure_ready()
        data = self._call_read_tool(self.tools.folders, {"account_name": self.account, "pattern": "*"})
        rows = data.get("result")
        if not isinstance(rows, list):
            raise McpContentError("list_mailboxes 返回缺少 result 数组", detail={"tool": self.tools.folders})
        names: List[str] = []
        for row in rows:
            if isinstance(row, dict) and row.get("name"):
                names.append(str(row["name"]))
            elif isinstance(row, str):
                names.append(row)
        return names

    def list_accounts(self) -> List[Dict[str, Any]]:
        """只读：列出服务器暴露的账号能力（不含凭据）。"""
        self._ensure_ready()
        data = self._call_read_tool(self.tools.accounts, {})
        rows = data.get("result")
        if not isinstance(rows, list):
            raise McpContentError("list_available_accounts 返回缺少 result 数组")
        return [dict(row) for row in rows if isinstance(row, dict)]

    def list_new(self, cursor: Optional[str]) -> Tuple[List[MailMessageRef], str]:
        self._ensure_ready()
        state = self.decode_cursor(cursor)
        since = state.get("since") or self._default_since()
        refs: List[MailMessageRef] = []
        page = 1
        last_date = since
        while page <= self.max_pages:
            data = self._call_read_tool(
                self.tools.metadata,
                {
                    "account_name": self.account,
                    "mailbox": self.folder,
                    "since": since,
                    "order": "asc",
                    "page": page,
                    "page_size": self.page_size,
                },
            )
            emails = data.get("emails")
            if emails is None:
                emails = []
            if not isinstance(emails, list):
                raise McpContentError("list_emails_metadata 返回 emails 不是数组")
            for item in emails:
                if not isinstance(item, dict):
                    continue
                ref = self._ref_from_metadata(item)
                refs.append(ref)
                stamp = _iso_from_any(item.get("date"))
                if stamp and stamp > last_date:
                    last_date = stamp
            total = data.get("total")
            if len(emails) < self.page_size:
                break
            if isinstance(total, int) and len(refs) >= total:
                break
            page += 1
        new_cursor = self.encode_cursor(since=last_date)
        # 记录位置快照（可选能力 cursor_position() 的数据来源），不参与游标比较
        self._position = {
            "cursor_version": CURSOR_VERSION,
            "account": self.account,
            "folder": self.folder,
            "since": last_date,
            "cursor": new_cursor,
            "window_start": since,
            "listed_count": len(refs),
            "observed_at": utcnow_iso(),
        }
        if cursor is not None and new_cursor == cursor:
            # 窗口没有前移（没有更新邮件）：游标保持原值，重放不产生任何写入/审计
            return refs, cursor
        return refs, new_cursor

    def fetch_raw(self, ref: MailMessageRef) -> bytes:
        self._ensure_ready()
        mailbox = ref.folder or self.folder
        body_parts: List[str] = []
        offset = 0
        item: Optional[Dict[str, Any]] = None
        for _ in range(self.max_body_pages):
            data = self._call_read_tool(
                self.tools.content,
                {
                    "account_name": self.account,
                    "email_ids": [str(ref.uid)],
                    "mailbox": mailbox,
                    "mark_as_read": False,  # 只读硬约束：绝不改远端标记
                    "body_offset": offset,
                    "max_body_length": self.max_body_length,
                },
            )
            emails = data.get("emails")
            if not isinstance(emails, list) or not emails:
                raise McpContentError(
                    "get_emails_content 未返回该邮件（可能已被移动或删除）",
                    detail={"uid": str(ref.uid), "failed_ids": data.get("failed_ids")},
                )
            current = emails[0]
            if not isinstance(current, dict):
                raise McpContentError("get_emails_content 返回条目类型异常")
            item = current
            body = current.get("body")
            body_text = body if isinstance(body, str) else ""
            if body_text.endswith(_TRUNCATION_MARKER):
                body_parts.append(body_text[: -len(_TRUNCATION_MARKER)])
                offset += self.max_body_length
                continue
            body_parts.append(body_text)
            break
        else:
            raise McpContentError(
                "正文分页超过上限，疑似服务端窗口语义异常",
                detail={"uid": str(ref.uid), "max_body_pages": self.max_body_pages},
            )
        assert item is not None
        projection = self.metadata_projection(item)
        if ref.content_digest and projection["digest"] != ref.content_digest:
            raise McpContentError(
                "同一 UID 的元数据在列取与取件之间发生变化（内容冲突），拒绝把新内容挂到旧身份上",
                detail={
                    "uid": str(ref.uid),
                    "listed_digest": ref.content_digest,
                    "fetched_digest": projection["digest"],
                },
            )
        raw = self.build_raw(ref, item, "".join(body_parts))
        return raw

    # ------------------------------------------------------------------ 游标
    def encode_cursor(self, *, since: str) -> str:
        """游标 = 窗口起点（账号 + 文件夹 + since）。

        刻意**只**包含位置信息：不含处理时刻等会自然变化的内容，这样
        「重复列取但没有新邮件」会得到完全相同的游标字符串，同步层因此
        不会推进游标、也不会产生额外的审计行（幂等要求）。
        """
        payload = {
            "v": CURSOR_VERSION,
            "account": self.account,
            "folder": self.folder,
            "since": since,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def decode_cursor(self, cursor: Optional[str]) -> Dict[str, Any]:
        if not cursor:
            return {}
        try:
            data = json.loads(cursor)
        except (ValueError, TypeError) as exc:
            raise CursorInvalid(
                "游标不是合法 JSON（可能被外部改写或来自旧版本）",
                detail={"cursor_head": cursor[:64]},
            ) from exc
        if not isinstance(data, dict) or int(data.get("v") or 0) != CURSOR_VERSION:
            raise CursorInvalid("游标版本不可识别", detail={"cursor_head": cursor[:64]})
        if str(data.get("account") or "") != self.account:
            raise CursorInvalid("游标账号与当前账号不符")
        if str(data.get("folder") or "") != self.folder:
            raise CursorInvalid("游标文件夹与当前文件夹不符")
        since = data.get("since")
        if not since:
            raise CursorInvalid("游标缺少窗口起点 since")
        stamp = _parse_iso(str(since))
        if stamp is None:
            raise CursorInvalid("游标 since 不是可解析时间")
        moment = self._now()
        if stamp > moment + 600:
            raise CursorInvalid("游标时间在未来，判定失效")
        if stamp < moment - self.lookback_days * 86400 * 4:
            raise CursorInvalid("游标过旧（超出可靠重建窗口），判定失效")
        return data

    def cursor_position(self) -> Optional[Dict[str, Any]]:
        """契约 v0.4 可选能力（CP-20261010-08）：**观测**"同步到哪了"。

        无参、返回字典（给控制台/巡检看），**没有信息就返回 `None`**——不假装 0 进度。
        只有真正列取过一次（或收到过合法游标）之后才有信息；这里不发起任何网络调用。
        """
        if self._position is None:
            return None
        return dict(self._position)

    def cursor_rank(self, cursor: Optional[str]) -> Optional[float]:
        """**本实现额外**的能力：给同步层做游标回退检测用的单调位置（epoch 秒）。

        注意它**不是**契约成员（契约的可选能力是上面的无参 `cursor_position()`）；
        调用方必须 `getattr(source, "cursor_rank", None)` 探测，缺失时跳过回退检测。
        """
        if not cursor:
            return None
        try:
            data = json.loads(cursor)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        return _parse_iso(str(data.get("since") or ""))

    def _default_since(self) -> str:
        from datetime import datetime, timedelta, timezone

        moment = datetime.fromtimestamp(self._now(), tz=timezone.utc) - timedelta(days=self.lookback_days)
        return moment.isoformat()

    def _now(self) -> float:
        if self._now_fn is not None:
            return float(self._now_fn())
        from datetime import datetime, timezone

        text = self._clock()
        moment = _parse_iso(text)
        if moment is None:
            return datetime.now(tz=timezone.utc).timestamp()
        return moment

    # ------------------------------------------------------------------ 元数据/原文
    def _ref_from_metadata(self, item: Dict[str, Any]) -> MailMessageRef:
        uid = str(item.get("email_id") or "").strip()
        if not uid:
            raise McpContentError("元数据缺少 email_id（无法建立账号作用域稳定身份）")
        attachments = [str(name) for name in item.get("attachments") or [] if name]
        projection = self.metadata_projection(item)
        return MailMessageRef(
            account=self.account,
            folder=self.folder,
            uid=uid,
            vendor_message_id=None,  # mcp-email-server 不暴露额外不可变 ID
            message_id=(str(item.get("message_id")).strip() if item.get("message_id") else None),
            thread_keys=self._thread_keys(item),
            subject=_text(item.get("subject")),
            from_addr=_text(item.get("sender")),
            to_addrs=[_text(value) for value in item.get("recipients") or []],
            received_at=_text(item.get("date")),
            size_bytes=int(projection.get("size_bytes") or 0),
            has_attachments=bool(attachments),
            content_digest=projection["digest"],  # 元数据投影摘要（该通道没有 raw 字节）
            # 契约 v0.4：显式声明摘要来源，下游才不会被误导（见 CONTENT_ORIGIN_PROJECTION）
            content_origin=CONTENT_ORIGIN_PROJECTION,
        )

    @staticmethod
    def _thread_keys(item: Dict[str, Any]) -> List[str]:
        keys: List[str] = []
        message_id = item.get("message_id")
        if message_id:
            keys.append(str(message_id).strip())
        references = item.get("references") or item.get("in_reply_to")
        if isinstance(references, str):
            keys.extend(part for part in references.split() if part)
        keys.extend(
            str(value).strip()
            for value in (item.get("provider_keywords") or [])
            if isinstance(value, str) and value.strip()
        )
        return keys[:32]

    @staticmethod
    def metadata_projection(item: Dict[str, Any]) -> Dict[str, Any]:
        """稳定的元数据投影：只含身份相关且不随读取动作变化的字段。"""
        projection = {
            "email_id": _text(item.get("email_id")),
            "message_id": _text(item.get("message_id")) if item.get("message_id") else "",
            "subject": _text(item.get("subject")),
            "sender": _text(item.get("sender")),
            "recipients": sorted(_text(value) for value in item.get("recipients") or []),
            "attachments": sorted(_text(name) for name in item.get("attachments") or [] if name),
            "date": _text(item.get("date")),
        }
        digest = digest_of(["mcp-mail-metadata", projection])
        projection["digest"] = digest
        projection["size_bytes"] = int(item.get("size_bytes") or 0)
        return projection

    @staticmethod
    def build_raw(ref: MailMessageRef, item: Dict[str, Any], body: str) -> bytes:
        """把 MCP 返回的字段拼成确定性的邮件字节（顺序固定 → 摘要稳定）。"""
        headers: List[Tuple[str, str]] = []
        headers.append(("X-Asistent-Source", "mcp-email-server"))
        headers.append(("X-Asistent-Account", _text(ref.account)))
        headers.append(("X-Asistent-Mailbox", _text(ref.folder or "")))
        headers.append(("X-Asistent-Uid", _text(ref.uid)))
        sender = _text(item.get("sender"))
        if sender:
            headers.append(("From", sender))
        recipients = [_text(value) for value in item.get("recipients") or []]
        if recipients:
            headers.append(("To", ", ".join(recipients)))
        subject = _text(item.get("subject"))
        if subject:
            headers.append(("Subject", subject))
        date = _text(item.get("date"))
        if date:
            headers.append(("Date", date))
        message_id = item.get("message_id")
        if message_id:
            headers.append(("Message-ID", _text(message_id)))
        in_reply_to = item.get("in_reply_to")
        if in_reply_to:
            headers.append(("In-Reply-To", _text(in_reply_to)))
        references = item.get("references")
        if references:
            headers.append(("References", _text(references)))
        attachments = [name for name in item.get("attachments") or [] if name]
        if attachments:
            headers.append(("X-Asistent-Attachments", "; ".join(_text(name) for name in attachments)))
        lines = ["%s: %s" % (name, value) for name, value in headers]
        text = "\r\n".join(lines) + "\r\n\r\n" + body
        return text.encode("utf-8", "replace")

    # ------------------------------------------------------------------ 内部
    def _ensure_ready(self) -> None:
        if not self._ready:
            self.connect()

    def _call_read_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        data = self.client.call_tool(name, arguments, timeout=self.client.timeout, read_only=True)
        self.read_only_tool_calls.append(name)
        return data


def _looks_secret_key(key: str) -> bool:
    return bool(_SECRET_INLINE.match(key + "=") or re.search(r"(?i)(pass|secret|token|key|credential|auth)", key))


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _safe_command_part(value: Any, limit: int = 120) -> str:
    """只把 argv[0] 的**文件名**放进错误详情。

    完整 argv 可能含凭据（命令行传密码是常见反模式），错误详情会被写进状态文件与日志，
    所以这里只保留可执行文件名 + 截断，绝不外传其余参数。
    """
    text = _text(value).replace("\\", "/")
    return text.rsplit("/", 1)[-1][:limit]


def _head(text: str, limit: int = 200) -> str:
    return text[:limit]


def _error_text(error: Dict[str, Any]) -> str:
    if not isinstance(error, dict):
        return str(error)
    message = error.get("message")
    data = error.get("data")
    if data in (None, ""):
        return str(message)
    return "%s（%s）" % (message, data)


def _error_text_from_content(result: Dict[str, Any]) -> str:
    parts: List[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return " ".join(parts)[:500] or "tools/call isError=True"


def _parse_iso(text: str) -> Optional[float]:
    if not text:
        return None
    try:
        moment = parsedate_to_datetime(text)
        if moment is not None:
            return moment.timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        from datetime import datetime

        cleaned = text.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        return datetime.fromisoformat(cleaned).timestamp()
    except (TypeError, ValueError):
        return None


def _iso_from_any(value: Any) -> str:
    """把服务端返回的日期转成可比较的 ISO 字符串（解析失败返回空串）。"""
    if value is None:
        return ""
    text = str(value)
    moment = _parse_iso(text)
    if moment is None:
        return ""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat()
