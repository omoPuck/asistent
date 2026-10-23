"""时钟可信度契约（SPEC §11.2、§21.1、§21.7）。

## 为什么需要

设备实测：本次开机时钟停在 `2024-07-08`，落后约 **2 年 3 个月**，且
`systemd-time-wait-sync` 处于 disabled（docs/env/report.md §B6）。
在时钟不可信的开机窗口内写入业务时间戳，会污染：

- 审计事件时间与"关键动作前后都有审计事件"的因果顺序；
- 审批有效期判定（可能把有效审批判成过期，或把过期审批判成有效）；
- 邮件同步窗口（`SINCE` 会漏信）；
- 归档时间范围与保留策略。

## 契约规定

1. 服务启动自检时钟：拿"持久化的最后可信时间"和"本次观察到的墙钟"比较；
2. **墙钟回退超过容差 → 拒绝写入业务时间戳**，进入降级模式并把原因暴露到手机端
   （宁可不写，也不要写错时间）；
3. 墙钟前跳（NTP 纠正大偏差）**允许**，但必须记审计并携带 note，
   因为拒绝前跳会让刚校时之后的正常时间也无法写入；
4. 时长测量一律用**单调钟**（`time.monotonic`），不用墙钟相减；
5. 未同步状态下必须有外部信号可查（`NTPSynchronized` 或等价），
   降级/恢复都要留审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .errors import ClockAnomaly
from .timeutil import parse_iso, utcnow_iso

#: 墙钟回退容差（秒）：小于它视为抖动/时区数据刷新，不算异常
DEFAULT_BACKWARD_TOLERANCE = 300
#: 墙钟前跳阈值（秒）：超过它必须记审计（约 1 天）
DEFAULT_FORWARD_AUDIT_THRESHOLD = 86400


@dataclass
class ClockCheck:
    ok: bool
    reason: str = "OK"
    requires_audit: bool = False
    detail: Dict[str, Any] = field(default_factory=dict)

    def require_ok(self) -> None:
        if not self.ok:
            raise ClockAnomaly(
                f"时钟不可信（{self.reason}）：拒绝写入业务时间戳，进入降级模式",
                detail={"reason": self.reason, **self.detail},
            )

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "requires_audit": self.requires_audit, "detail": self.detail}


def check_clock_sanity(
    *,
    now_iso: str,
    reference_iso: Optional[str] = None,
    backward_tolerance: int = DEFAULT_BACKWARD_TOLERANCE,
    forward_audit_threshold: int = DEFAULT_FORWARD_AUDIT_THRESHOLD,
) -> ClockCheck:
    """比较墙钟与"最后可信时间"。

    - 墙钟回退超过 `backward_tolerance` → `ok=False`（拒绝写业务时间戳）；
    - 墙钟前跳超过 `forward_audit_threshold` → `ok=True` + `requires_audit=True`；
    - 无参照时间 → `ok=True`，但 reason 标为 `NO_REFERENCE`（首次启动，允许写入）。
    """
    try:
        now = parse_iso(now_iso)
    except Exception as exc:  # 时间格式不合法同样是不可信
        return ClockCheck(ok=False, reason="INVALID_TIMESTAMP", detail={"error": str(exc), "now": now_iso})

    if not reference_iso:
        return ClockCheck(ok=True, reason="NO_REFERENCE", detail={"now": now_iso})

    try:
        reference = parse_iso(reference_iso)
    except Exception as exc:
        return ClockCheck(
            ok=False, reason="INVALID_REFERENCE", detail={"error": str(exc), "reference": reference_iso}
        )

    delta = (now - reference).total_seconds()
    if delta < -abs(backward_tolerance):
        return ClockCheck(
            ok=False,
            reason="CLOCK_BEHIND",
            requires_audit=True,
            detail={"now": now_iso, "reference": reference_iso, "delta_seconds": delta},
        )
    if delta > abs(forward_audit_threshold):
        return ClockCheck(
            ok=True,
            reason="CLOCK_FORWARD_JUMP",
            requires_audit=True,
            detail={"now": now_iso, "reference": reference_iso, "delta_seconds": delta},
        )
    return ClockCheck(ok=True, reason="OK", detail={"delta_seconds": delta})


class ClockGuard:
    """带状态的时间戳守门人：记住最后可信时间，并强制"不可信就拒写"。

    用法（runtime 启动时）::

        guard = ClockGuard()
        guard.observe(last_persisted_iso)     # 载入参照
        guard.require_sane(utcnow_iso())      # 不可信则抛 ClockAnomaly
    """

    def __init__(
        self,
        *,
        backward_tolerance: int = DEFAULT_BACKWARD_TOLERANCE,
        forward_audit_threshold: int = DEFAULT_FORWARD_AUDIT_THRESHOLD,
        monotonic=None,
    ) -> None:
        self.reference_iso: Optional[str] = None
        self.backward_tolerance = backward_tolerance
        self.forward_audit_threshold = forward_audit_threshold
        self.degraded = False
        self.last_check: Optional[ClockCheck] = None
        self.audit_notes: list = []
        self._monotonic = monotonic
        self._monotonic_start: Optional[float] = None

    # ------------------------------------------------------------------ 墙钟
    def observe(self, iso_value: str, *, now_iso: Optional[str] = None) -> ClockCheck:
        """记录一个（已经信任的）时间作为参照。"""
        check = check_clock_sanity(
            now_iso=now_iso or iso_value,
            reference_iso=self.reference_iso,
            backward_tolerance=self.backward_tolerance,
            forward_audit_threshold=self.forward_audit_threshold,
        )
        self.last_check = check
        self.degraded = not check.ok
        if check.ok:
            self.reference_iso = iso_value
            if check.requires_audit:
                self.audit_notes.append(check.to_dict())
        return check

    def require_sane(self, now_iso: Optional[str] = None) -> ClockCheck:
        check = check_clock_sanity(
            now_iso=now_iso or utcnow_iso(),
            reference_iso=self.reference_iso,
            backward_tolerance=self.backward_tolerance,
            forward_audit_threshold=self.forward_audit_threshold,
        )
        self.last_check = check
        self.degraded = not check.ok
        if check.requires_audit and check.ok:
            self.audit_notes.append(check.to_dict())
        check.require_ok()
        return check

    # ------------------------------------------------------------------ 单调钟
    def monotonic_now(self) -> float:
        """时长测量用单调钟：墙钟被校时不影响已耗时长的计算。"""
        import time

        clock = self._monotonic or time.monotonic
        value = clock()
        if self._monotonic_start is None:
            self._monotonic_start = value
        return value - self._monotonic_start

    def elapsed_seconds(self) -> float:
        return self.monotonic_now()

    # ------------------------------------------------------------------ 展示
    def status(self) -> Dict[str, Any]:
        return {
            "reference_iso": self.reference_iso,
            "degraded": self.degraded,
            "last_check": self.last_check.to_dict() if self.last_check else None,
            "audit_notes": list(self.audit_notes),
        }


__all__ = [
    "ClockAnomaly",
    "ClockCheck",
    "ClockGuard",
    "check_clock_sanity",
    "DEFAULT_BACKWARD_TOLERANCE",
    "DEFAULT_FORWARD_AUDIT_THRESHOLD",
]
