"""邮件通道的错误分类（MVP-2 私有；契约错误仍在 `asistent.contracts.errors`）。

设计原则（SPEC §13.1、§21.2）：
- 每个错误都带稳定 `code` 与 `failure_class`，同步循环据此把它写成**持久状态**，
  而不是吞掉异常；
- 「通道不可用」「游标失效」「内容校验失败」必须可区分：它们的恢复动作完全不同
  （重试同一批 / 重建游标 / 隔离该封邮件）。
"""

from __future__ import annotations

from typing import Optional

from ..contracts.enums import FailureClass
from ..contracts.errors import AsistentError, PermissionDenied, ValidationError

__all__ = [
    "MailChannelError",
    "ChannelUnavailable",
    "CursorInvalid",
    "CursorRegression",
    "McpError",
    "McpProtocolError",
    "McpToolError",
    "McpTimeout",
    "McpCrashed",
    "McpHandshakeError",
    "McpContentError",
    "RawContentMismatch",
    "MailContentError",
    "WriteNotPermitted",
]


class MailChannelError(AsistentError):
    """邮件通道错误的基类：可重试性与失败分类由子类决定。"""

    code = "MAIL_CHANNEL_ERROR"
    failure_class: FailureClass = FailureClass.TOOL_FAILURE
    #: 是否属于「临时故障、应重试同一批且不推进游标」
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        detail: Optional[dict] = None,
        failure_class: Optional[str] = None,
    ) -> None:
        super().__init__(message, detail=detail)
        if failure_class is not None:
            self.failure_class = FailureClass.coerce(failure_class)  # type: ignore[assignment]


class ChannelUnavailable(MailChannelError):
    """通道整体不可用（进程起不来、连接断开）：重试同一批，不推进游标。"""

    code = "MAIL_CHANNEL_UNAVAILABLE"
    failure_class = FailureClass.NETWORK_FAILURE
    retryable = True


class CursorInvalid(MailChannelError):
    """游标失效（格式不可识别、账号不符、太旧、被服务端拒绝）：必须重建而不是当作空结果。"""

    code = "MAIL_CURSOR_INVALID"
    failure_class = FailureClass.MISSING_DATA
    retryable = True


class CursorRegression(MailChannelError):
    """游标回退（新游标早于已有游标）：说明来源侧发生过重置，必须重建。"""

    code = "MAIL_CURSOR_REGRESSION"
    failure_class = FailureClass.MISSING_DATA
    retryable = True


class McpError(MailChannelError):
    """MCP stdio 通道错误基类。"""

    code = "MCP_ERROR"


class McpHandshakeError(McpError):
    """initialize 握手失败或协议版本/服务器信息不可用。"""

    code = "MCP_HANDSHAKE_ERROR"
    failure_class = FailureClass.TOOL_FAILURE
    retryable = True


class McpProtocolError(McpError):
    """JSON-RPC 层错误：响应不是 JSON、id 不匹配、缺少 result/error。"""

    code = "MCP_PROTOCOL_ERROR"
    failure_class = FailureClass.TOOL_FAILURE
    retryable = True


class McpToolError(McpError):
    """tools/call 返回错误（isError 或 JSON-RPC error）。消息必须已脱敏。"""

    code = "MCP_TOOL_ERROR"
    failure_class = FailureClass.TOOL_FAILURE
    retryable = True


class McpTimeout(McpError):
    """有界超时：不得无限等待。"""

    code = "MCP_TIMEOUT"
    failure_class = FailureClass.TOOL_FAILURE
    retryable = True


class McpCrashed(McpError):
    """子进程崩溃/退出/管道关闭：可检测，必须记录退出码。"""

    code = "MCP_CRASHED"
    failure_class = FailureClass.SYSTEM_CRASH
    retryable = True


class McpContentError(McpError):
    """返回内容不符合预期形状（缺字段、类型不对）——不重试，需人工看。"""

    code = "MCP_CONTENT_ERROR"
    failure_class = FailureClass.TOOL_FAILURE
    retryable = False


class RawContentMismatch(MailChannelError):
    """同一身份下原始内容与其声明摘要不一致：绝不能覆盖已保存的原文。"""

    code = "MAIL_RAW_CONTENT_MISMATCH"
    failure_class = FailureClass.MISSING_DATA
    retryable = False


class MailContentError(ValidationError):
    """邮件内容/身份不足（缺少稳定身份、非法 UID）：隔离该封，不阻塞整批。"""

    code = "MAIL_CONTENT_ERROR"
    failure_class = FailureClass.INVALID_INPUT
    retryable = False


class WriteNotPermitted(PermissionDenied):
    """只读模式拒绝任何写操作（发信/删除/标记/移动/归档）。"""

    code = "MAIL_WRITE_NOT_PERMITTED"
    failure_class = FailureClass.PERMISSION_FAILURE
    retryable = False
