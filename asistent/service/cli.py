"""命令行入口（`python3 -m asistent.service ...`）——与 `scripts/run-service.sh` 一一对应。

```text
--once                跑**恰好一轮**就退出（同一份实现；离线回路/分轮驱动用）
--loop                常驻循环（默认；按 interval 反复跑）
--interval N          覆盖轮次间隔（秒）
--max-rounds N        跑满 N 轮退出（0 = 不限）
--status              只打印当前健康快照（不改任何业务状态、不发任何请求）
--config PATH         仓库外配置文件（默认 $ASISTENT_VAR/config/service.json）
--init-token          生成控制台令牌文件后退出（内容不打印；600）
--quiet               只打印轮次摘要（默认也打印逐阶段证据行）
--json                把快照/轮次报告打成 JSON（取证/机器读取）
```

退出码：`0` 全部阶段成功；`1` 有阶段失败（服务本身没崩，但这一轮不干净）。
被强杀（`SIGKILL`）不会有退出码 —— 那正是"结果未落库"的证据来源。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional, Sequence

from .config import ServiceConfig, load_config
from .phases import RoundReport
from .runtime import ServiceRuntime


def _force_utf8_stdio() -> None:
    """中文邮件主题/正文必须原样落地：stdout/stderr 一律 UTF-8（Python 3.10 兼容写法）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover - 被重定向到不可重配的对象
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m asistent.service",
        description="个人助手常驻服务：一轮 = 恢复→同步→采集索引→理解→准备→外发→归档",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="跑恰好一轮就退出")
    mode.add_argument("--loop", action="store_true", help="常驻循环（默认）")
    mode.add_argument("--status", action="store_true", help="只打印健康快照，不做任何动作")
    mode.add_argument("--init-token", action="store_true", help="生成控制台令牌文件后退出")
    parser.add_argument("--config", default="", help="仓库外配置文件路径")
    parser.add_argument("--interval", type=int, default=0, help="覆盖轮次间隔（秒）")
    parser.add_argument("--max-rounds", type=int, default=-1, help="跑满 N 轮退出（0 = 不限）")
    parser.add_argument("--console", action="store_true", help="同时启动控制台 HTTP 服务")
    parser.add_argument("--quiet", action="store_true", help="只打印轮次摘要")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    return parser


def _apply_overrides(config: ServiceConfig, args: argparse.Namespace) -> ServiceConfig:
    if args.interval:
        config.interval_seconds = max(1, int(args.interval))
    if args.max_rounds >= 0:
        config.max_rounds = int(args.max_rounds)
    return config


def _print_status(runtime: ServiceRuntime, *, as_json: bool) -> None:
    snapshot = runtime.status()
    if as_json:
        print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2))
        return
    print("服务状态（run_id=%s pid=%s）" % (snapshot.get("run_id", "-"), snapshot.get("pid", "-")))
    print("  存储后端      : %s" % snapshot.get("store_class", "-"))
    print("  轮次          : 完成 %s 次 / 失败 %s 次（最近 %s）"
          % (snapshot.get("rounds_completed"), snapshot.get("rounds_failed"), snapshot.get("last_round_at") or "-"))
    print("  轮次间隔      : %ss" % snapshot.get("interval_seconds"))
    print("  最近同步      : %s" % json.dumps(snapshot.get("last_sync") or {}, ensure_ascii=False, sort_keys=True))
    print("  待处理        : %s" % json.dumps(snapshot.get("pending") or {}, ensure_ascii=False, sort_keys=True))
    print("  正在等        : %s" % ", ".join(snapshot.get("waiting_on") or []))
    print("  最近错误      : %s" % (snapshot.get("last_error") or {}).get("text", "-"))
    print("  阶段失败计数  : %s" % json.dumps(snapshot.get("phase_failures") or {}, ensure_ascii=False, sort_keys=True))
    print("  外发调用次数  : %s（账本 submit=%s）"
          % (snapshot.get("sender_call_count", "-"), snapshot.get("submit_count_on_disk", "-")))
    if snapshot.get("console_url"):
        print("  控制台        : %s" % snapshot["console_url"])
    for note in snapshot.get("notes") or []:
        print("  说明          : %s" % note)


def main(argv: Optional[Sequence[str]] = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    config = _apply_overrides(load_config(args.config or None), args)

    if args.init_token:
        runtime = ServiceRuntime(config)
        created = runtime.token_created
        runtime.close()
        print("控制台令牌文件：%s（%s）" % (config.console.token_path, "已生成" if created else "已存在，未改动"))
        return 0

    if args.status:
        runtime = ServiceRuntime(config)
        try:
            _print_status(runtime, as_json=args.json)
        finally:
            runtime.close()
        return 0

    log = None if args.quiet else (lambda message: print(message, flush=True))
    runtime = ServiceRuntime(config, log=log, start_console=args.console)
    max_rounds = 1 if args.once else max(0, config.max_rounds)
    previous = {}
    # 先绑定：`install_signal_handlers` 在受限环境（非主线程等）可能直接抛错，
    # 那时 `finally` 里的取证输出不能因为"reports 还没赋值"而把真正的错误顶掉。
    reports: List[RoundReport] = []
    try:
        previous = runtime.install_signal_handlers()
        if not args.quiet:
            print("[service] 启动 run_id=%s 模式=%s" % (runtime.run_id, "once" if args.once else "loop"))
            print("[service] 配置 %s" % json.dumps(config.describe(), ensure_ascii=False, sort_keys=True))
        reports = runtime.run_loop(max_rounds=max_rounds)
    finally:
        import signal as _signal

        for name, handler in previous.items():
            signum = getattr(_signal, name, None)
            if signum is not None:
                try:
                    _signal.signal(signum, handler)
                except (ValueError, OSError):  # pragma: no cover
                    pass
        if args.json:
            print(json.dumps([item.to_dict() for item in reports], ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print("[service] 退出：共 %d 轮，%s" % (len(reports), runtime.state.summary_line()))
        runtime.close()
    return 0 if all(item.ok for item in reports) else 1


__all__: List[str] = ["build_parser", "main"]
