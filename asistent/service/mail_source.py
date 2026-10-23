"""真实邮件通道适配器（服务层）：把设备上的 `mcp-email-server` 接成 `MailSource`。

本模块**只**负责真实通道。离线夹具直接用契约自带的
`asistent.contracts.mail.FixtureMailSource`——它曾经读不了自己的夹具
（`AttributeError: 'Header' object has no attribute 'split'`，6/6 全失败），
当时本层写了个 `FixtureDirMailSource` 绕过去。该缺陷已在 CP-20261010-32 于契约层修好，
绕行实现随之删除：**同一个缺陷不该有两个实现**，否则契约修好了、补丁还在跑，
两边行为会慢慢分叉。

凭据纪律（SPEC §21.3、A-6）：
- 授权码只从 600 文件读，只放进**子进程环境变量** `MCP_EMAIL_SERVER_PASSWORD`；
- 不进配置、不进 argv、不进本模块的日志（子进程 stderr 会经过 `Redactor` 脱敏）。
"""


from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..mail.errors import ChannelUnavailable
from ..mail.mcp_stdio import LogSink, McpMailSource, Redactor, StdioJsonRpcClient


#: `source="mcp"` 时的默认命令（设备上 MVP-2 只读探针验过的那个二进制）。
DEFAULT_MCP_COMMAND = (
    os.path.expanduser("~/asistent-spike/venv/bin/mcp-email-server"),
    "stdio",
)

#: 授权码文件里允许出现的空白（粘贴常带换行/空格）
_TRIM = " \t\r\n"


def read_authcode(path: str) -> str:
    """从 600 文件读取授权码；读不到就返回空串（**不抛异常**，由调用方决定怎么记账）。

    只做最小限度的清洗（去掉首尾空白与内部换行）：163 的授权码本身可能含
    字母与数字，但不能含空白，所以"去掉所有空白"比"只 strip 首尾"更稳。
    """
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return ""
    return "".join(ch for ch in raw if ch not in _TRIM)


def build_mcp_mail_source(
    config: Any,
) -> Tuple[Optional[Any], str]:
    """按配置构造真实 MCP 邮件源，返回 `(source, 说明)`。

    返回 `(None, 说明)` 的几种情况都是**用户动作没做完**，不是程序故障——
    服务必须照样常驻，并在状态里把"为什么没有真邮件"写清楚：

    - 没有授权码文件 → 说明指向 `set-mail-authcode.sh`（用户动作 U-1）；
    - 命令不存在 → 说明指出配置项与期望路径；
    - 其他构造失败 → 原样记录异常类型与消息（便于定位，不吞掉）。

    凭据纪律：授权码只出现在子进程环境变量 `MCP_EMAIL_SERVER_PASSWORD` 里；
    配置、argv、日志、状态快照都不含它（`StdioJsonRpcClient` 按键名自动登记脱敏）。
    """
    mail = config.mail
    authcode = read_authcode(mail.authcode_path)
    if not authcode:
        return None, (
            "mail.source=mcp 但还没有授权码文件 %s：真实收信未启用（用户动作 U-1："
            "在设备上执行 `bash ~/asistent-var/bin/set-mail-authcode.sh`）" % mail.authcode_path
        )

    command: Sequence[str] = tuple(mail.mcp_command) or DEFAULT_MCP_COMMAND
    binary = command[0]
    if not (os.path.isabs(binary) and os.path.exists(binary)) and not _which(binary):
        return None, (
            "mail.source=mcp 但找不到可执行文件 %r：请在配置里设 mail.mcp_command "
            "（期望的是设备上的 mcp-email-server，例如 %s）" % (binary, DEFAULT_MCP_COMMAND[0])
        )

    from ..mail.mcp_stdio import LogSink, McpMailSource, StdioJsonRpcClient

    account = mail.account or ""
    env: Dict[str, str] = {}
    env.update(dict(mail.mcp_env))
    # 这些键名是 `mcp-email-server` 1.11.1 记录的**环境变量建账号**通路
    # （`EmailSettings.from_environment()`）：EMAIL_ADDRESS + PASSWORD + IMAP_HOST 三件套必填，
    # 少了 IMAP_HOST 服务端会静默返回 None（日志里只有一行 warning），所以这里显式传全。
    env["MCP_EMAIL_SERVER_EMAIL_ADDRESS"] = account
    env["MCP_EMAIL_SERVER_PASSWORD"] = authcode
    env["MCP_EMAIL_SERVER_USER_NAME"] = account
    env["MCP_EMAIL_SERVER_IMAP_HOST"] = mail.imap_host
    env["MCP_EMAIL_SERVER_IMAP_PORT"] = str(mail.imap_port)
    env["MCP_EMAIL_SERVER_IMAP_SSL"] = "true" if mail.imap_ssl else "false"

    # 子进程 stderr 落到 600 日志：真机出问题时这是**唯一**能看的东西
    # （`mcp-email-server` 的告警都走 stderr，例如"缺 IMAP_HOST 就静默不建账号"）。
    # 凭据不会落到这里：客户端按键名登记了脱敏值。
    log_path = os.path.join(config.logs_dir, "mail-mcp.log") if getattr(config, "logs_dir", "") else None

    try:
        client = StdioJsonRpcClient(list(command), env=env, log=LogSink(log_path))
        source = McpMailSource(
            client,
            account=account or "default",
            folder=mail.folder or "INBOX",
        )
    except Exception as exc:  # noqa: BLE001 - 说清失败原因，不让服务因为收信通道挂掉
        return None, "mail.source=mcp 构造失败：%s: %s" % (type(exc).__name__, exc)
    return source, ""


def _which(binary: str) -> str:
    """极简 `which`：只查 PATH（不调用外部命令，避免多一层 shell）。"""
    if os.path.sep in binary:
        return binary if os.path.exists(binary) else ""
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, binary)
        if os.path.exists(candidate):
            return candidate
    return ""
