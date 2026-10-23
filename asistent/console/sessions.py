"""控制台会话：把"一次性令牌"换成"HttpOnly 会话 Cookie"，让手机 PWA 打开即用。

为什么要会话 Cookie（而不是把令牌存在浏览器里）
------------------------------------------------
原来的设计是令牌只放内存（`state.token`），安全但难用：PWA 每次从桌面启动都是一次
新的页面加载，用户就得重新粘一遍 64 位令牌——"能打开"和"好用"差很远。
把令牌存 localStorage / sessionStorage 都会让任何注入脚本能读走它，等于把门禁交给 XSS。

会话 Cookie 的做法是：令牌只用来**换一次会话**，之后浏览器自动带 Cookie：

* `HttpOnly` —— JS 读不到，XSS 拿不到凭据本身；
* `Secure` —— 只在 HTTPS 下发送（手机走 Caddy，天然满足）；
* `SameSite=Strict` —— 跨站请求不带，配合"非 GET 必须带自定义头"防 CSRF；
* 服务端只存 **会话值的 SHA-256**，内存泄露/日志误写都拿不到可用会话值；
* 有 TTL 且**滑动续期**：常用就一直有效，长期不用自然失效。

这不是"把令牌换个地方存"：会话可以被单独吊销（退出登录、服务重启），令牌不会。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from typing import Callable, Dict, Optional


def _fingerprint(value: str) -> str:
    """会话值只以哈希形式留在服务端内存里。"""
    return hashlib.sha256(("asistent-session:" + (value or "")).encode("utf-8")).hexdigest()


class ConsoleSessions:
    """内存会话表：单进程控制台足够；重启即全部失效（这是想要的性质，不是缺点）。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = 7 * 24 * 3600.0,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("会话有效期必须为正数")
        self.ttl_seconds = float(ttl_seconds)
        self._now = now or time.time
        self._lock = threading.Lock()
        #: fingerprint -> 过期时间（epoch 秒）
        self._sessions: Dict[str, float] = {}

    # ------------------------------------------------------------------ 读写
    def create(self) -> str:
        """新建会话，返回**明文会话值**（只在这一刻存在于服务端内存之外）。"""
        value = secrets.token_urlsafe(32)
        now = float(self._now())
        with self._lock:
            self._sessions[_fingerprint(value)] = now + self.ttl_seconds
        return value

    def valid(self, value: str) -> bool:
        """校验会话值；有效则**滑动续期**（常用就不过期）。"""
        if not value:
            return False
        key = _fingerprint(value)
        now = float(self._now())
        with self._lock:
            expires_at = self._sessions.get(key)
            if expires_at is None:
                return False
            if expires_at <= now:
                self._sessions.pop(key, None)
                return False
            self._sessions[key] = now + self.ttl_seconds
            return True

    def revoke(self, value: str) -> bool:
        """吊销一个会话（退出登录）。返回它原本是否存在。"""
        if not value:
            return False
        with self._lock:
            return self._sessions.pop(_fingerprint(value), None) is not None

    def sweep(self) -> int:
        """清掉过期的会话，返回清掉的条数（控制台会顺带调用，避免内存里越积越多）。"""
        now = float(self._now())
        with self._lock:
            dead = [key for key, expires_at in self._sessions.items() if expires_at <= now]
            for key in dead:
                self._sessions.pop(key, None)
            return len(dead)

    # ------------------------------------------------------------------ 观测
    def count(self) -> int:
        """当前会话数（给健康信息看，不含任何会话值）。"""
        with self._lock:
            return len(self._sessions)
