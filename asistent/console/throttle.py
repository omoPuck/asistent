"""鉴权失败节流：控制台可能暴露在公网，令牌是唯一门禁，必须给爆破成本封顶。

为什么是"全局"而不是"按客户端 IP"
---------------------------------
手机走 frp 的 **TCP** 隧道进来，控制台看到的对端恒为 `127.0.0.1` —— 真实客户端 IP 在
隧道那一层就丢了（frp 的 tcp 代理不传 `X-Forwarded-For`，除非另开 PROXY protocol）。
所以按 IP 计数会把所有客户端算进同一个桶，只是看起来精确，实际是假精度。
这里诚实地做成**全局连续失败计数**：连续失败到阈值后整体冷却，冷却时长指数增长、有上限。

代价（必须写下来，不要假装没有）
--------------------------------
攻击者可以用错误令牌在冷却期内把主人挡在门外。对单人自用场景这个代价远小于
"公网无限次爆破"，而且：冷却上限很小（默认 5 分钟）、成功一次就清零、
改配置或重启服务即可立刻解除。

并发
----
`ThreadingHTTPServer` 下多个请求线程会同时进出，所以计数与冷却状态都加锁。
纯逻辑 + 可注入时钟，便于测试"时间过去了多久"而不用真的 sleep。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional


class AuthThrottle:
    """连续鉴权失败 -> 全局冷却。线程安全。"""

    def __init__(
        self,
        *,
        max_failures: int = 20,
        window_seconds: float = 600.0,
        base_lockout_seconds: float = 60.0,
        max_lockout_seconds: float = 300.0,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures 必须 >= 1")
        if window_seconds <= 0 or base_lockout_seconds <= 0 or max_lockout_seconds <= 0:
            raise ValueError("时间窗口/冷却时长必须为正数")
        self.max_failures = int(max_failures)
        self.window_seconds = float(window_seconds)
        self.base_lockout_seconds = float(base_lockout_seconds)
        self.max_lockout_seconds = float(max_lockout_seconds)
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._failures: List[float] = []
        self._locked_until = 0.0
        #: 连续被冷却的次数：每次再加一次，冷却时长翻倍（有上限）
        self._lockout_level = 0

    # ------------------------------------------------------------------ 查询
    def check(self) -> Optional[float]:
        """当前是否处于冷却中。返回还需等待的秒数（>0）或 None（放行）。"""
        now = float(self._now())
        with self._lock:
            if self._locked_until > now:
                return round(self._locked_until - now, 3)
            return None

    def record_failure(self) -> Optional[float]:
        """记一次失败。若触发冷却，返回冷却秒数；否则 None。"""
        now = float(self._now())
        with self._lock:
            # 只保留窗口内的失败，避免"很久以前的失败"累积成一次误伤
            self._failures = [ts for ts in self._failures if now - ts <= self.window_seconds]
            self._failures.append(now)
            if len(self._failures) < self.max_failures:
                return None
            # 触发冷却：清空计数，冷却时长随连续触发次数翻倍
            self._failures = []
            self._lockout_level += 1
            duration = min(
                self.base_lockout_seconds * (2 ** (self._lockout_level - 1)),
                self.max_lockout_seconds,
            )
            self._locked_until = now + duration
            return round(duration, 3)

    def record_success(self) -> None:
        """鉴权成功：清零计数与冷却级别（合法使用者进来了，说明不是爆破）。"""
        with self._lock:
            self._failures = []
            self._lockout_level = 0
            self._locked_until = 0.0

    # ------------------------------------------------------------------ 观测
    def snapshot(self) -> Dict[str, Any]:
        """给 `/api/health` 用的状态。**不含令牌、不含客户端信息**（隧道后也分不出来）。"""
        now = float(self._now())
        with self._lock:
            remaining = max(0.0, self._locked_until - now)
            return {
                "failures_in_window": len(self._failures),
                "max_failures": self.max_failures,
                "window_seconds": self.window_seconds,
                "lockout_seconds_remaining": round(remaining, 3),
                "lockout_level": self._lockout_level,
            }
