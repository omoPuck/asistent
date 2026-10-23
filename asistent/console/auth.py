"""令牌鉴权：从仓库外 700/600 文件读 token，比较用固定时间算法。

安全约定（对应 SPEC §13.4、§21.3，`docs/mvp/mvp-4.md` §2 有验收证据）：

1. token 只从**文件**读，不从命令行/环境变量/URL 读 —— 命令行会进 shell history，
   环境变量会进 `/proc/<pid>/environ`，URL 会进浏览器 history 与访问日志；
2. 文件权限必须是 600（目录 700）；不满足就**拒绝启动**，而不是"打个警告继续跑"；
3. 比较用 `hmac.compare_digest`（固定时间），避免按字符长度/前缀泄露 token；
4. token 不进日志、不进审计、不进响应体。本模块只提供 `matches()`，没有 `to_string()`。
"""

from __future__ import annotations

import hmac
import os
import stat
from typing import List, Optional


class AuthError(Exception):
    """token 文件本身有问题（不存在、权限过宽、内容为空）。这类问题必须拒绝启动。"""


#: 允许的 token 文件权限位（八进制）
_ALLOWED_FILE_MODES = (0o600, 0o400)
#: 允许的 token 目录权限位（八进制）
_ALLOWED_DIR_MODES = (0o700, 0o500)
#: token 最短长度：太短的 token 在局域网内可被在线爆破
MIN_TOKEN_LENGTH = 16
#: 支持 POSIX 权限位的平台才做"权限是否过宽"的硬校验。
#: Windows 的 `st_mode` 只有只读位（0o777/0o555），没有 group/other 概念 ——
#: 在 Windows 上假装校验"必须 600"是假验证，所以在那里显式跳过并记录原因。
POSIX_PERMISSIONS = os.name != "nt"


def load_token(path: str, *, check_permissions: Optional[bool] = None) -> str:
    """读取 token 文件并校验权限；任何不合格都抛 `AuthError`（调用方据此拒绝启动）。"""
    if not path:
        raise AuthError("未指定 token 文件路径")
    if not os.path.exists(path):
        raise AuthError("token 文件不存在: %s" % path)
    if os.path.islink(path):
        raise AuthError("token 文件不能是符号链接: %s" % path)

    with open(path, "r", encoding="utf-8") as handle:
        token = handle.read().strip()
    if not token:
        raise AuthError("token 文件为空: %s" % path)
    if len(token) < MIN_TOKEN_LENGTH:
        raise AuthError("token 太短（至少 %d 字符）: %s" % (MIN_TOKEN_LENGTH, path))

    verify = POSIX_PERMISSIONS if check_permissions is None else bool(check_permissions)
    if verify:
        directory = os.path.dirname(os.path.abspath(path))
        dir_mode = stat.S_IMODE(os.stat(directory).st_mode)
        file_mode = stat.S_IMODE(os.stat(path).st_mode)
        if not _within_mode(dir_mode, _ALLOWED_DIR_MODES):
            raise AuthError(
                "token 所在目录权限过宽（需要 700，实际 %s）: %s —— 请 chmod 700 该目录"
                % (oct(dir_mode), directory)
            )
        if not _within_mode(file_mode, _ALLOWED_FILE_MODES):
            raise AuthError(
                "token 文件权限过宽（需要 600，实际 %s）: %s —— 请 chmod 600 该文件"
                % (oct(file_mode), path)
            )
    return token


def _within_mode(mode: int, allowed: tuple) -> bool:
    """必须**恰好**是允许的模式之一。

    不用"至少比 600 更严"这种猜测：`0o777` 看起来"包含 600 的位"却完全不可接受，
    允许集合是白名单，比位运算推导更难写错。
    """
    return mode in allowed


class TokenAuth:
    """无状态令牌校验器：只持有期望值，不持有请求状态（所以多线程安全）。"""

    def __init__(self, token: str) -> None:
        if not token:
            raise AuthError("token 为空：未配置令牌时不得启动控制台")
        self._expected = str(token)
        self._fingerprint = _fingerprint(token)

    @property
    def fingerprint(self) -> str:
        """token 的短指纹（前 12 位 sha256）：只用于日志里确认"加载的是哪一个 token"。"""
        return self._fingerprint

    def matches(self, presented: Optional[str]) -> bool:
        """固定时间比较。`None` / 空串一律不通过。"""
        if not presented:
            return False
        return hmac.compare_digest(str(presented), self._expected)

    @staticmethod
    def extract(header_value: Optional[str], *, query_value: Optional[str] = None) -> Optional[str]:
        """从 `Authorization: Bearer <token>` 里取 token。

        `query_value` 参数**故意忽略**：token 不允许出现在 URL 里（会进浏览器历史、
        反向代理日志与 Referer），所以这里显式声明"看到了也不接受"。
        """
        del query_value  # 明确：URL 里的 token 无效，不读取
        if not header_value:
            return None
        raw = str(header_value).strip()
        scheme, _, value = raw.partition(" ")
        if scheme.lower() != "bearer":
            return None
        value = value.strip()
        return value or None

    def redact(self, text: str) -> str:
        """把文本里出现的 token 替换成 `<redacted>`：日志兜底，防止意外外泄。"""
        return str(text).replace(self._expected, "<redacted>")


def _fingerprint(token: str) -> str:
    import hashlib

    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:12]


def token_file_hints(path: str) -> List[str]:
    """给运维看的一行提示（不含 token 内容）。"""
    return [
        "生成: mkdir -p -m 700 \"$(dirname %s)\" && openssl rand -hex 32 > %s && chmod 600 %s" % (path, path, path),
        "环境: ASISTENT_VAR（默认 ~/asistent-var）决定默认路径",
    ]


__all__ = [
    "AuthError",
    "MIN_TOKEN_LENGTH",
    "POSIX_PERMISSIONS",
    "TokenAuth",
    "load_token",
    "token_file_hints",
]
