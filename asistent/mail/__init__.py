"""MVP-2 邮件接入：同步循环 + `mcp-email-server` stdio 通道。

对外只暴露两块：

- `MailSyncEngine`（`asistent.mail.sync`）：把 `MailSource` 的新邮件落成共享对象，
  负责游标持久化/断点续传/游标失效重建/幂等去重/失败状态化；
- `McpMailSource` + `StdioJsonRpcClient`（`asistent.mail.mcp_stdio`）：以 stdio
  子进程方式驱动 `mcp-email-server` 的 JSON-RPC 2.0 通道。

本包**没有**发送路径：外发在 MVP-5，且必须走 `asistent.contracts.mail.MailSender`
与审批/幂等键；同步层只读（`mark_as_read` 恒为 false，写工具被本地拒绝）。
"""

from __future__ import annotations

from .errors import (
    ChannelUnavailable,
    CursorInvalid,
    CursorRegression,
    MailChannelError,
    MailContentError,
    McpContentError,
    McpCrashed,
    McpError,
    McpHandshakeError,
    McpProtocolError,
    McpTimeout,
    McpToolError,
    RawContentMismatch,
    WriteNotPermitted,
)
from .mcp_stdio import (
    CHILD_UTF8_ENV,
    CONTENT_ORIGIN_PROJECTION,
    DEFAULT_PROTOCOL_VERSION,
    READ_ONLY_TOOLS,
    STDIO_ENCODING,
    WRITE_TOOLS,
    LogSink,
    McpMailSource,
    McpToolNames,
    Redactor,
    StdioJsonRpcClient,
)
from .sync import (
    IDENTITY_DONE,
    IDENTITY_FAILED,
    IDENTITY_QUARANTINED,
    STATE_VERSION,
    MailSyncEngine,
    RawStore,
    SyncFailure,
    SyncReport,
    SyncStateStore,
    normalize_received_at,
    safe_text,
)

__all__ = [
    # 同步
    "MailSyncEngine",
    "SyncReport",
    "SyncFailure",
    "SyncStateStore",
    "RawStore",
    "safe_text",
    "normalize_received_at",
    "STATE_VERSION",
    "IDENTITY_DONE",
    "IDENTITY_FAILED",
    "IDENTITY_QUARANTINED",
    # MCP stdio 通道
    "StdioJsonRpcClient",
    "McpMailSource",
    "McpToolNames",
    "Redactor",
    "LogSink",
    "DEFAULT_PROTOCOL_VERSION",
    "READ_ONLY_TOOLS",
    "WRITE_TOOLS",
    "STDIO_ENCODING",
    "CHILD_UTF8_ENV",
    "CONTENT_ORIGIN_PROJECTION",
    # 错误
    "MailChannelError",
    "ChannelUnavailable",
    "CursorInvalid",
    "CursorRegression",
    "MailContentError",
    "RawContentMismatch",
    "WriteNotPermitted",
    "McpError",
    "McpHandshakeError",
    "McpProtocolError",
    "McpToolError",
    "McpTimeout",
    "McpCrashed",
    "McpContentError",
]
