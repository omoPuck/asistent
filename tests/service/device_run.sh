#!/bin/sh
# MVP-8 设备端取证脚本（在设备上跑；**只写 $HOME/asistent-var 与自建临时目录**）。
#
# 为什么要有它：验收要求"至少有一次在真设备上真的跑起来"，而"真的跑起来"必须
# 一次跑完、可复现、可对照着看输出。这个脚本就是那一次：
#
#   0) 环境事实（内核/Python/SQLite-FTS5/systemd版本/linger/setsid/bdpan授权）
#   1) 把源码放到临时目录（**不动** ~/code/asistent 里的已部署副本）
#   2) 写一份仓库外配置（邮件用离线夹具；外发用受控假通道；归档指向真 bdpan）
#   3) 单轮：--once --json（原始轮次报告）
#   4) run-service.sh doctor / unit-file
#   5) start（setsid 降级方式）→ 连跑两轮 → status → 手机端视角读健康快照
#   6) 从手机端接口提交审批 → 下一轮外发 → **恰好一次**
#   7) 优雅停止（SIGTERM）→ 日志尾部
#
# 用法（设备上）：
#   sh /tmp/mvp8-run/run-device-evidence.sh /tmp/mvp8-run/asistent-src
#
# 凭据纪律：本脚本不读不写任何口令/授权码/令牌内容；控制台令牌由服务自己生成
# 到 $HOME/asistent-var/config/console-token（600），只被当成"文件路径"传给探针。

set -eu

SRC=${1:?用法: run-device-evidence.sh <源码目录>}
VAR=${ASISTENT_VAR:-$HOME/asistent-var}
PY=${ASISTENT_PYTHON:-python3}
CONFIG="$VAR/config/service.json"
MAIL_DIR="$VAR/mail-cases"

say() { printf '\n=== %s ===\n' "$1"; }
run() { printf '\n$ %s\n' "$*"; "$@"; }

say "0) 环境事实"
run uname -a
run $PY -V
run $PY -c 'import sqlite3,sys; print("sqlite", sqlite3.sqlite_version); print("fts5", bool(sqlite3.connect(":memory:").execute("select 1 from pragma_compile_options where compile_options like \"%FTS5%\"").fetchone()))'
run systemctl --version
run loginctl show-user "$(id -un)"
command -v setsid && run setsid --version
# `crontab -l` 在没有 crontab 时返回 1，而本脚本是 `set -e`：
# 一个纯信息性的探测项把整份取证脚本弄崩过一次（2026-10-12 设备实测，日志停在第 0 节）。
# 取证脚本的原则是：凡是探测都要记账，但不能因为探测结果不如意就中断。
if command -v crontab >/dev/null 2>&1; then run crontab -l || printf '（没有 crontab，属正常）\n'; else printf 'crontab 不可用\n'; fi
printf '\n(bdpan 授权探测，只读)\n'
command -v bdpan || printf 'bdpan 不在 PATH（未授权/未安装都属已知状态）\n'
run df -h "$VAR"

say "1) 准备运行数据目录"
# 目录清单要与 scripts/run-service.sh 的 ensure_var_layout 一致：
# 漏了 data/ 会让服务以 `sqlite3.OperationalError: unable to open database file` 退出
# （2026-10-12 设备实测：第 3 节单轮直接抛这个错，而本机测试因为夹具自己建了 data/ 所以看不到）。
mkdir -p "$VAR/config" "$VAR/state" "$VAR/logs" "$VAR/raw" "$VAR/data" \
         "$VAR/index" "$VAR/archive/staging" "$MAIL_DIR"
chmod 700 "$VAR" 2>/dev/null || true
if [ ! -f "$MAIL_DIR/05-reply-needed-ok.eml" ]; then
    cp "$SRC/fixtures/public/understand/05-reply-needed-ok.eml" "$MAIL_DIR/" 2>/dev/null || true
fi
ls -l "$MAIL_DIR" || true

say "2) 写仓库外配置（${CONFIG}）"
if [ -f "$CONFIG" ]; then
    cp "$CONFIG" "$CONFIG.bak.$$"
    printf '已存在，先备份到 %s\n' "$CONFIG.bak.$$"
fi
cat > "$CONFIG" <<EOF
{
  "var_root": "$VAR",
  "store": "sqlite",
  "db_path": "$VAR/data/asistent.db",
  "state_dir": "$VAR/state",
  "interval_seconds": 5,
  "max_rounds": 0,
  "batch_limit": 50,
  "console": {
    "enabled": true,
    "bind": "127.0.0.1",
    "port": 18090,
    "token_path": "$VAR/config/console-token",
    "artifact_roots": ["$VAR/state", "$VAR/raw"]
  },
  "mail": {
    "enabled": true,
    "source": "fixture",
    "account": "parent@example.com",
    "folder": "INBOX",
    "fixture_dir": "$MAIL_DIR",
    "state_path": "$VAR/state/mail-sync.json",
    "raw_root": "$VAR/raw"
  },
  "ingest": { "enabled": false, "roots": [], "index_path": "$VAR/index/fts.db" },
  "outbound": {
    "enabled": true,
    "channel": "scripted",
    "from_account": "parent@example.com",
    "node": "board"
  },
  "archive": {
    "enabled": true,
    "channel": "bdpan",
    "root": "个人助手归档",
    "staging_root": "$VAR/archive/staging"
  }
}
EOF
chmod 600 "$CONFIG"
run ls -l "$CONFIG"

say "3) 单轮（同一份实现；原始轮次报告）"
cd "$SRC"
PYTHONPATH="$SRC" ASISTENT_VAR="$VAR" PYTHONUTF8=1 \
    run $PY -X utf8 -m asistent.service --once --json --config "$CONFIG"

say "4) 启动器自检 + systemd unit"
run sh "$SRC/scripts/run-service.sh" doctor || printf "（doctor 返回非 0，见上；继续取证）\n"
run sh "$SRC/scripts/run-service.sh" unit-file
printf '\n(systemd 语法校验，可缺)\n'
sh "$SRC/scripts/run-service.sh" unit-file --stage >/dev/null 2>&1 || true
systemd-analyze verify "$VAR/systemd/asistent.service" 2>&1 || printf 'systemd-analyze 不可用或报了 warning（不影响运行）\n'

say "5) 常驻启动（setsid 降级方式）+ 连跑两轮 + 状态"
export ASISTENT_VAR="$VAR" ASISTENT_PYTHON="$PY" ASISTENT_SERVICE_CONFIG="$CONFIG"
run sh "$SRC/scripts/run-service.sh" start
sleep 12
export ASISTENT_VAR="$VAR" ASISTENT_PYTHON="$PY" ASISTENT_SERVICE_CONFIG="$CONFIG"
run sh "$SRC/scripts/run-service.sh" status
run sh -c "tail -n 25 '$VAR/logs/service.log'"

say "6) 手机端视角：读健康快照 + 提交审批 + 恰好一次外发"
PORT=$(sed -n 's/.*"port": *\([0-9]*\).*/\1/p' "$CONFIG" | head -1)
run $PY -X utf8 "$SRC/tests/service/device_console_probe.py" "$VAR/config/console-token" "$PORT" || printf "（探针失败，见上；继续取证）\n"
run $PY -X utf8 "$SRC/tests/service/device_console_probe.py" "$VAR/config/console-token" "$PORT" --approve || printf "（审批提交失败，见上）\n"
sleep 12
run $PY -X utf8 "$SRC/tests/service/device_console_probe.py" "$VAR/config/console-token" "$PORT" --expect-submits 1 || printf "（核对失败，见上）\n"
printf '\n外发账本行数（应恰好 1）：'
grep -c '"event": "submit"' "$VAR/state/outbound-calls.jsonl" 2>/dev/null || printf '0\n'
printf '再等两轮，确认不重发……\n'
sleep 12
printf '外发账本行数（仍应恰好 1）：'
grep -c '"event": "submit"' "$VAR/state/outbound-calls.jsonl" 2>/dev/null || printf '0\n'

say "7) 优雅停止（SIGTERM）+ 日志尾部"
export ASISTENT_VAR="$VAR" ASISTENT_PYTHON="$PY" ASISTENT_SERVICE_CONFIG="$CONFIG"
run sh "$SRC/scripts/run-service.sh" stop
run sh -c "tail -n 15 '$VAR/logs/service.log'"
printf '\n进程还在吗（应为空）：\n'
pgrep -af "asistent[.]service" || printf '（没有残留进程）\n'

printf '\n=== 取证完成 ===\n'
