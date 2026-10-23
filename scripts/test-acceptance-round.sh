#!/usr/bin/env bash
# 输入：无。输出：逐项 OK/FAIL + 汇总，失败时退码 1。
# 用法：bash scripts/test-acceptance-round.sh
#
# 本地验证 acceptance-round.sh 的时间判定逻辑（不需要设备）：
# 这是这个脚本存在的核心理由——"没跑够 24 小时就不许出结论"必须是代码保证的，不能靠人记。
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
mkdir -p "$TMP/state"
export ASISTENT_VAR="$TMP"

mk_anchor() {  # $1 = 几小时前
  python3 - "$TMP/state/acceptance-round.json" "$1" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone
path, hours = sys.argv[1], float(sys.argv[2])
now = datetime.now(timezone.utc).astimezone()
record = {
    "anchor_at": (now - timedelta(hours=hours)).isoformat(timespec="seconds"),
    "run_id": "exec_test",
    "service_started_at": (now - timedelta(hours=hours)).isoformat(timespec="seconds"),
    "repo_head": "deadbee",
    "repo_head_subject": "test",
    "contract_version": "v0.9",
    "criteria_version": "CP-20261010-37",
}
json.dump(record, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
}

pass=0; fail=0
check() {  # $1 描述, $2 期望包含, $3 实际输出
  if printf '%s' "$3" | grep -qF "$2"; then
    echo "[OK]   $1"
    pass=$((pass + 1))
  else
    echo "[FAIL] $1（期望包含：$2）"
    printf '%s\n' "$3" | head -8
    fail=$((fail + 1))
  fi
}

echo "===== 1) 没跑够：必须拒绝出结论 ====="
mk_anchor 2
OUT=$(bash "$ROOT/scripts/acceptance-round.sh" verdict 2>&1)
check "2 小时 → 拒绝" "拒绝出结论" "$OUT"
check "2 小时 → 说清还差多久" "还差" "$OUT"

echo
echo "===== 2) 刚好差一点（23.9h）：仍然拒绝 ====="
mk_anchor 23.9
OUT=$(bash "$ROOT/scripts/acceptance-round.sh" verdict 2>&1)
check "23.9 小时 → 拒绝" "拒绝出结论" "$OUT"

echo
echo "===== 3) 满 24 小时：才允许判定 ====="
mk_anchor 24.1
OUT=$(bash "$ROOT/scripts/acceptance-round.sh" verdict 2>&1)
check "24.1 小时 → 进入判定" "已满 24 小时" "$OUT"
if printf '%s' "$OUT" | grep -qF "拒绝出结论"; then
  echo "[FAIL] 满 24 小时却仍拒绝"
  fail=$((fail + 1))
else
  echo "[OK]   满 24 小时不再拒绝"
  pass=$((pass + 1))
fi

echo
echo "===== 4) status：锚点信息要看得见 ====="
mk_anchor 5
OUT=$(bash "$ROOT/scripts/acceptance-round.sh" status 2>&1)
check "status 显示锚点" "锚点" "$OUT"
check "status 显示 run_id" "exec_test" "$OUT"
check "status 提示还差多久" "还差" "$OUT"

echo
echo "===== 5) 没有锚点时不许装成有结论 ====="
rm -f "$TMP/state/acceptance-round.json"
OUT=$(bash "$ROOT/scripts/acceptance-round.sh" status 2>&1 || true)
check "无锚点 → 明确报错" "还没有锚点" "$OUT"

rm -rf "$TMP"
echo
echo "结果：通过 $pass 项，失败 $fail 项"
[ "$fail" = "0" ] || exit 1
