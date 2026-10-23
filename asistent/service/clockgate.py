"""启动前的时钟闸门（systemd `ExecStartPre`）。

为什么需要它
============

本项目的持久化层按 **`(timestamp, 写入序号)`** 定序（v0.5 CP-11），审计时间戳也是证据的一部分。
如果服务在一个明显错误的时钟下启动，后果不是"日志难看"，而是：

* 新写入的事件会被排到历史之前，`(timestamp, sequence)` 顺序**不再等于真实因果顺序**；
* 审计时间戳、归档清单时间、浸泡取样点全部失去可解释性；
* "连续 24h" 这种结论直接失效（起点算不出来）。

这台设备上真的发生过：RTC 曾经慢了约两年（2026-10-12 实测发现）。

为什么不直接依赖 systemd 的 `time-sync.target`
=============================================

`systemd-time-wait-sync.service` 等的是 **`systemd-timesyncd` 发出的 `time-sync.target` 通知**。
本机的时间同步由 **chrony** 提供（`systemd-timesyncd` 是 inactive），而 chrony 不发这个通知，
于是那个 unit 从开机起一直卡在 `activating`、`time-sync.target` 始终 inactive
（2026-10-12 实测：已挂了约两天，`Result=success`、`SubState=activating`）。

也就是说"等时钟"这件事在本机**从来没真正生效**。与其去改系统级 NTP 配置，
不如在本服务自己的启动路径上放一道闸门：**不依赖是哪个 NTP 客户端**。

设计取舍
========

* **时钟正常时零延迟**：先看一次，正常就直接退出，不缺省等待。
* **不正常时有界等待**：默认最多等 120 秒（给 NTP 校正留时间），每 5 秒复查一次。
* **等不到也照样启动，但要说出来**：拒绝启动会把 24h 助手直接干掉，比时间戳不可信更糟；
  静默启动则会让用户以为时间是对的。所以**两害相权取其响**：启动，但打印一条明确的告警，
  并把结论写进 `$ASISTENT_VAR/state/clock-gate.json`，供状态快照与验收记录引用。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

#: 早于这个时刻的时钟一定不对（本部署 2026 年上线；留足余量）。
DEFAULT_FLOOR = "2024-01-01T00:00:00+00:00"

#: 相对"代码提交时间"允许的回退余量（秒）。默认 1 天。
#:
#: 为什么还需要这一条：固定下限只能防"时钟回到 2024 年以前"，
#: 而本机真正发生的是 **RTC 慢了约两年**——时钟读到 2024 年，实际是 2026 年。
#: 那种情况下固定下限**放行**，闸门等于没有。所以下限取"两者中较晚的那个"：
#: 代码提交时刻 - 余量（我们确知代码是什么时候写的）。
DEFAULT_SLACK_SECONDS = 86400.0

#: 有界等待的默认上限（秒）。时钟正常时根本用不到。
DEFAULT_WAIT_SECONDS = 120.0

#: 复查间隔（秒）
POLL_SECONDS = 5.0


def floor_from_env(env: Optional[Dict[str, str]] = None) -> datetime:
    source = env if env is not None else os.environ
    raw = (source.get("ASISTENT_CLOCK_FLOOR") or DEFAULT_FLOOR).strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = datetime.fromisoformat(DEFAULT_FLOOR)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def wait_seconds_from_env(env: Optional[Dict[str, str]] = None) -> float:
    source = env if env is not None else os.environ
    raw = (source.get("ASISTENT_CLOCK_WAIT_SECONDS") or "").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_WAIT_SECONDS


def slack_seconds_from_env(env: Optional[Dict[str, str]] = None) -> float:
    source = env if env is not None else os.environ
    raw = (source.get("ASISTENT_CLOCK_SLACK_SECONDS") or "").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_SLACK_SECONDS


def repo_floor(repo_root: Optional[str] = None, runner: Any = None) -> Optional[datetime]:
    """代码提交时刻（用作"我们确知的时间"锚点）。拿不到就 `None`，**不猜**。"""
    import subprocess

    root = repo_root or os.environ.get("ASISTENT_REPO_ROOT") or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    run = runner or subprocess.run
    try:
        proc = run(
            ["git", "-C", root, "log", "-1", "--format=%cI"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    raw = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def effective_floor(
    env: Optional[Dict[str, str]] = None,
    *,
    repo_root: Optional[str] = None,
    runner: Any = None,
) -> datetime:
    """下限 = max(固定下限, 代码提交时刻 - 余量)。

    固定下限防"时钟跑到很久以前"，代码锚点防"时钟落后以年计"（本机实测的那种）。
    """
    source = env if env is not None else os.environ
    floor = floor_from_env(source)
    anchor = repo_floor(repo_root, runner)
    if anchor is not None:
        candidate = anchor - timedelta(seconds=slack_seconds_from_env(source))
        if candidate > floor:
            floor = candidate
    return floor


def is_sane(now: datetime, floor: datetime) -> bool:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= floor


def _record(state_path: str, record: Dict[str, Any]) -> None:
    try:
        directory = os.path.dirname(state_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(state_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(state_path, 0o600)
    except OSError:
        # 记不下来不能阻断启动：这是诊断信息，不是业务数据
        pass


def evaluate(
    *,
    now: Optional[datetime] = None,
    floor: Optional[datetime] = None,
    waited_seconds: float = 0.0,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """给出一次判断的结论（纯函数，便于测试）。"""
    floor = floor or effective_floor(env)
    now = now or datetime.now(timezone.utc)
    sane = is_sane(now, floor)
    return {
        "checked_at": now.isoformat(timespec="seconds"),
        "floor": floor.isoformat(timespec="seconds"),
        "waited_seconds": round(waited_seconds, 1),
        "clock_sane": sane,
        "verdict": "ok" if sane else "clock_behind_floor",
    }


def run(
    *,
    env: Optional[Dict[str, str]] = None,
    clock: Any = None,
    sleep: Any = None,
    now_fn: Any = None,
    state_path: Optional[str] = None,
    log: Any = None,
) -> int:
    """执行闸门。**总是返回 0**（见模块文档：拒绝启动比时间戳不可信更糟）。"""
    source = env if env is not None else os.environ
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    write = log or (lambda text: sys.stderr.write(text + "\n"))

    floor = effective_floor(source)
    limit = wait_seconds_from_env(source)
    started = clock()
    waited = 0.0
    attempts = 0

    while True:
        attempts += 1
        current = now_fn()
        if is_sane(current, floor):
            record = evaluate(now=current, floor=floor, waited_seconds=waited)
            record["attempts"] = attempts
            if waited > 0:
                write("时钟闸门：等待 %.0f 秒后时钟已正常（%s）" % (waited, record["checked_at"]))
            if state_path:
                _record(state_path, record)
            return 0
        if waited >= limit:
            break
        remaining = limit - waited
        step = min(POLL_SECONDS, remaining)
        if attempts == 1:
            write(
                "时钟闸门：当前时间 %s 早于下限 %s——等 NTP 校正，最多等 %.0f 秒"
                % (current.isoformat(timespec="seconds"), floor.isoformat(timespec="seconds"), limit)
            )
        sleep(step)
        waited = clock() - started

    record = evaluate(now=now_fn(), floor=floor, waited_seconds=waited)
    record["attempts"] = attempts
    record["note"] = "等待超时后仍然启动：时间戳可能不可信，请勿据此下因果结论"
    if state_path:
        _record(state_path, record)
    write(
        "警告：时钟仍早于下限（%s < %s），等了 %.0f 秒仍没等到 NTP 校正。"
        "服务照常启动，但**时间戳可能不可信**，验收结论不得基于这段时间的先后顺序。"
        % (
            record["checked_at"],
            record["floor"],
            waited,
        )
    )
    return 0


def _state_path() -> str:
    var = os.environ.get("ASISTENT_VAR") or os.path.join(os.path.expanduser("~"), "asistent-var")
    return os.path.join(var, "state", "clock-gate.json")


def _main(argv) -> int:  # pragma: no cover - 薄壳
    return run(state_path=_state_path())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
