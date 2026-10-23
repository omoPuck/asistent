#!/usr/bin/env python3
"""**只读**探测真实 bdpan：证明"设备就绪但用户尚未授权"时通道的处理是正确的。

安全纪律（务必遵守）：
- 只调用只读子命令：`bdpan version`、`bdpan whoami`、`bdpan ls`；
- **已授权时本脚本也绝不写入**：一旦 `require_authorized()` 通过，脚本立即停止并打印提示，
  不做任何 `archive`/`put`/`rm`/`mkdir`；
- 不读取 `~/.config/bdpan/config.json`，不打印任何 token/cookie。

用法（设备 board）：

    python3 tests/archive/probe_real_bdpan_readonly.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from asistent.pan import BdpanCliPanClient, PanNotAuthorized, probe_bdpan  # noqa: E402


def main() -> int:
    print("== 1. 只读探测（bdpan version + whoami） ==")
    probe = probe_bdpan()
    print(json.dumps(probe.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    if not probe.installed:
        print("未安装 bdpan：无法继续（这本身也是一种可接受的终端状态）")
        return 0

    print("\n== 2. 授权的可执行判定（require_authorized，只读） ==")
    pan = BdpanCliPanClient(root_prefix="asistent")
    print(f"writes_locked={pan.writes_locked}")
    try:
        pan.require_authorized()
    except PanNotAuthorized as exc:
        print("NOT_AUTHORIZED（显式失败，不是静默跳过）")
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        print("\n== 3. 只读列目录（不写任何东西） ==")
        try:
            items = pan.list("个人助手归档")
            print(f"list('个人助手归档') -> {len(items)} 项（目录不存在时按 0 项处理）")
        except Exception as exc:  # noqa: BLE001 - 只读探测失败也要如实打印
            print(f"list 失败（如实报告）：{type(exc).__name__}: {exc}")
        print("\n== 4. 实际发出的子命令（必须只有只读子命令） ==")
        for argv in pan.call_log:
            print("  " + " ".join(argv))
        non_readonly = [
            argv
            for argv in pan.call_log
            if len(argv) > 1 and argv[1] in ("upload", "put", "rm", "delete", "mkdir", "download", "get")
        ]
        print(f"非只读子命令数量 = {len(non_readonly)}（必须为 0）")
        return 0 if not non_readonly else 1

    print("已授权：本脚本**不会**执行任何写入；请改用 `scripts/release.sh --dry-run` 演练，"
          "或走审批流程后用 `scripts/release.sh --approval-ref <审批 ID>` 真实发布。")
    for argv in pan.call_log:
        print("  " + " ".join(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
