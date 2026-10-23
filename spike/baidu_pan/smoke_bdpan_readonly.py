#!/usr/bin/env python3
"""只读冒烟测试：验证 `bdpan` 适配器能解析真实 CLI 输出，且写入默认被锁死。

**本脚本只做只读操作**：version / whoami / ls。
它不调用 upload / download / rm / mv / cp / mkdir / transfer / share，
不会向真实百度网盘写入、覆盖或删除任何数据（SPEC §21.3 要求，Spike 约束）。

用法（Windows 开发机或 RK3588 设备）：
    python3 smoke_bdpan_readonly.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bdpan_client import (  # noqa: E402
    BdpanCliPanClient,
    BdpanNotInstalled,
    PanWriteNotAuthorized,
    probe_bdpan,
)

RC = 0


def line(msg: str) -> None:
    print(msg)


def main() -> int:
    global RC
    line("=" * 74)
    line(" bdpan 适配器只读冒烟测试（不写真实网盘）")
    line("=" * 74)

    probe = probe_bdpan()
    line(f"  bdpan 已安装      : {probe.installed}")
    line(f"  二进制            : {probe.binary}")
    line(f"  版本              : {probe.version}")
    line(f"  已登录            : {probe.logged_in}")
    line(f"  探测详情          : {probe.detail}")
    if not probe.installed:
        line("  [SKIP] 未安装 bdpan，跳过适配器调用测试")
        return 0
    if not probe.logged_in:
        line("  [SKIP] 未登录，跳过适配器调用测试（登录态需用户本人在本机完成）")
        return 0

    try:
        client = BdpanCliPanClient()
    except BdpanNotInstalled as exc:
        line(f"  [SKIP] {exc}")
        return 0

    line("")
    line(f"  远端作用域（CLI 强制）: {BdpanCliPanClient.REMOTE_SCOPE}")
    line("  —— 只读列举 /apps/bdpan 根目录 ——")
    try:
        items = client.list("")
    except Exception as exc:  # noqa: BLE001
        line(f"  [FAIL] list 失败：{type(exc).__name__}: {exc}")
        return 1
    line(f"  条目数            : {len(items)}")
    for it in items[:20]:
        kind = "DIR " if it.isdir else "FILE"
        line(f"    {kind} {it.path}  size={it.size}  content_sha256={it.content_sha256}")
    line(f"  [ OK ] 适配器成功解析真实 CLI 的 ls --json 输出（{len(items)} 条）")

    line("")
    line("  —— 写入闸门（应全部被拒绝）——")
    locked_ok = True
    for label, call in (
        ("put", lambda: client.put(__file__, "asistent-spike/should-not-be-uploaded.txt")),
        ("delete", lambda: client.delete("asistent-spike/should-not-be-uploaded.txt")),
    ):
        try:
            call()
            line(f"    [FAIL] {label} 竟然没有触发写入闸门")
            locked_ok = False
        except PanWriteNotAuthorized as exc:
            line(f"    [ OK ] {label} 被拒绝：{str(exc)[:80]}…")
        except Exception as exc:  # noqa: BLE001
            line(f"    [FAIL] {label} 抛出非预期异常：{type(exc).__name__}: {exc}")
            locked_ok = False
    if not locked_ok:
        RC = 1

    line("")
    line("  说明：真实上传/回读校验需要用户明确批准后再单独执行；")
    line("        本 Spike 未对真实网盘执行任何写操作。")
    line(f"  RESULT: {'只读冒烟通过' if RC == 0 else '存在失败项'}")
    return RC


if __name__ == "__main__":
    raise SystemExit(main())
