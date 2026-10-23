"""控制台配置：端口、绑定地址、token 文件位置。

token **必须**放在仓库之外，并且文件权限为 600（目录 700）。本模块只负责
"读到哪里去"，不负责生成；生成方式见 `docs/mvp/mvp-4.md`（一条 `openssl rand -hex 32`）。
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from typing import Sequence

#: 默认端口（SPEC/Lead 裁决：18080 已被占用，控制台用 18090）
DEFAULT_PORT = 18090

#: 默认绑定地址：只监听内网。`0.0.0.0` 会让转发规则外的接口也暴露，
#: 所以默认绑到设备内网网卡地址（由部署时显式给出），不给默认值就绑回环。
LOOPBACK = "127.0.0.1"


def is_loopback_bind(bind: str) -> bool:
    """判断监听地址是否**只对本机可见**。

    只有回环地址算"只对本机可见"。空串、`0.0.0.0`、`::`、内网网卡地址都不算——
    它们会让同一网络里的其他机器（或被转发到公网的入口）够到控制台。
    """
    text = (bind or "").strip()
    if not text:
        return False
    if text.lower() == "localhost":
        return True
    host = text[1:-1] if text.startswith("[") and text.endswith("]") else text
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def default_token_path() -> str:
    """默认 token 文件位置：`$ASISTENT_VAR/config/console-token`。

    `$ASISTENT_VAR` 默认 `~/asistent-var`（仓库外）。绝不放在仓库内 —— 否则
    会被 `git add -A` 带进提交。
    """
    var_dir = os.environ.get("ASISTENT_VAR") or os.path.join(os.path.expanduser("~"), "asistent-var")
    return os.path.join(var_dir, "config", "console-token")


def default_db_path() -> str:
    """默认库文件位置：`$ASISTENT_VAR/data/asistent.db`。"""
    var_dir = os.environ.get("ASISTENT_VAR") or os.path.join(os.path.expanduser("~"), "asistent-var")
    return os.path.join(var_dir, "data", "asistent.db")


@dataclass
class ConsoleSettings:
    """一次服务运行所需的全部配置（没有全局状态、没有隐式默认）。"""

    store: object
    token: str
    bind: str = LOOPBACK
    port: int = DEFAULT_PORT
    token_path: str = ""
    static_dir: str = ""
    #: 请求体上限（字节）：控制台只接收短文本，超过就直接 413，避免内存被拖垮
    max_body_bytes: int = 262144
    #: 是否允许回环地址之外访问（部署时显式打开；测试里保持默认 False）
    allow_remote: bool = False
    #: 日志钩子里能看到的客户端标识；token 永远不进日志
    server_name: str = "asistent-console"
    #: 允许下载的目录白名单（空 = 不允许任何文件下载，最严默认）
    artifact_roots: Sequence[str] = field(default_factory=tuple)
    #: 鉴权失败节流：连续失败 max_auth_failures 次（窗口内）后全局冷却。
    #: 控制台经 frp 暴露在公网时令牌是唯一门禁，没有上限等于允许无限爆破。
    max_auth_failures: int = 20
    auth_window_seconds: float = 600.0
    auth_lockout_seconds: float = 60.0
    auth_max_lockout_seconds: float = 300.0
    #: 会话 Cookie：令牌只用来换一次会话，之后靠 HttpOnly Cookie（详见 sessions.py）
    session_ttl_seconds: float = 7 * 24 * 3600.0
    session_cookie_name: str = "asistent_session"
    session_cookie_secure: bool = True

    def describe(self) -> dict:
        """用于启动日志与 `/api/health`：**不含 token**。"""
        return {
            "bind": self.bind,
            "port": self.port,
            "token_path": self.token_path,
            "token_loaded": bool(self.token),
            "allow_remote": self.allow_remote,
            "max_body_bytes": self.max_body_bytes,
            "server_name": self.server_name,
            "artifact_roots": list(self.artifact_roots),
            "max_auth_failures": self.max_auth_failures,
            "auth_lockout_seconds": self.auth_lockout_seconds,
            "session_ttl_seconds": self.session_ttl_seconds,
            "session_cookie_name": self.session_cookie_name,
        }


__all__ = [
    "ConsoleSettings",
    "DEFAULT_PORT",
    "LOOPBACK",
    "default_db_path",
    "default_token_path",
    "is_loopback_bind",
]
