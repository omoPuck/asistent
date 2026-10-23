#!/usr/bin/env python3
"""生成/校验契约快照与状态表（保证文档与代码不漂移）。

用法：
    python3 scripts/gen_contract_snapshot.py           # 写入 contracts/
    python3 scripts/gen_contract_snapshot.py --check   # 校验是否与代码一致（CI/集成门禁用）

产物：
    contracts/contracts-<version>.json   机器可读契约快照
    contracts/state-tables.md            由状态机代码生成的状态表
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from asistent.contracts import CONTRACT_VERSION, contract_snapshot  # noqa: E402
from asistent.contracts import transitions as tr  # noqa: E402

OUT_DIR = os.path.join(ROOT, "contracts")


def render_state_tables() -> str:
    lines = [
        "# 状态转换表（由 `asistent/contracts/transitions.py` 生成，请勿手改）",
        "",
        f"- 契约版本：`{CONTRACT_VERSION}`",
        "- 生成命令：`python3 scripts/gen_contract_snapshot.py`",
        "- 校验命令：`python3 scripts/gen_contract_snapshot.py --check`",
        "",
    ]
    for name, machine in sorted(tr.MACHINES.items()):
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"- 初始状态：`{machine.initial}`")
        lines.append(f"- 全部状态：{', '.join(f'`{s}`' for s in machine.states)}")
        lines.append("")
        lines.append("| 从 | 到 | 允许触发者 | 需要无不可逆动作 | 说明 |")
        lines.append("|---|---|---|---|---|")
        for row in machine.table():
            note = row["note"] or ""
            lines.append(
                f"| `{row['from']}` | `{row['to']}` | {', '.join(row['triggers'])} | "
                f"{'是' if row['requires_no_irreversible'] else '—'} | {note} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验，不写入")
    args = parser.parse_args()

    snapshot = contract_snapshot()
    snapshot_path = os.path.join(OUT_DIR, f"contracts-{CONTRACT_VERSION}.json")
    tables_path = os.path.join(OUT_DIR, "state-tables.md")
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tables = render_state_tables()

    if args.check:
        problems = []
        for path, expected in ((snapshot_path, payload), (tables_path, tables)):
            if not os.path.exists(path):
                problems.append(f"缺少产物: {os.path.relpath(path, ROOT)}")
                continue
            with open(path, "r", encoding="utf-8") as handle:
                actual = handle.read()
            if actual != expected:
                problems.append(f"与代码不一致: {os.path.relpath(path, ROOT)}")
        if problems:
            print("契约快照校验失败：")
            for item in problems:
                print(f"  - {item}")
            print("请运行: python3 scripts/gen_contract_snapshot.py")
            return 1
        print(f"契约快照一致（{CONTRACT_VERSION}）")
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(snapshot_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    with open(tables_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(tables)
    print(f"已写入 {os.path.relpath(snapshot_path, ROOT)} 与 {os.path.relpath(tables_path, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
