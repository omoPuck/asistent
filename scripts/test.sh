#!/usr/bin/env bash
# 运行契约与 MVP 测试（设备 board：纯标准库，不需要 pip 安装任何东西）
#
#   bash scripts/test.sh                 # 参考实现（默认）
#   ASISTENT_STORE=sqlite bash scripts/test.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
echo "== python: $($PY --version 2>&1)"
echo "== 契约快照一致性检查（--check）"
"$PY" scripts/gen_contract_snapshot.py --check
echo "== 测试（ASISTENT_STORE=${ASISTENT_STORE:-reference}）"
"$PY" -m unittest discover -s tests -t . -v
echo OK
