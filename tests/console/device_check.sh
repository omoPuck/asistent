#!/usr/bin/env bash
# MVP-4 控制台 · 设备端演练与验证脚本（在 board 上运行，**不碰** ~/code/asistent）
#
# 用法（在设备上）：
#   bash tests/console/device_check.sh /home/asistent/asistent-mvp4 192.0.2.10
#
# 它做四件事：
#   1. 生成仓库外的 token 文件（目录 700 / 文件 600），并验证权限不合格时服务会拒绝启动；
#   2. 真的在 18090 上起服务，把**绑定地址**打出来（证明只监听内网网卡）；
#   3. 用 curl 打原始请求：无 token → 401、错 token → 401、对 token → 200；
#   4. 跑一遍测试（understand + console + 全量），并打印 Python 版本。
#
# 所有输出都是可以原样贴进 docs/mvp/mvp-4.md 的原始文本。
set -u

ROOT="${1:-$HOME/asistent-mvp4}"
BIND="${2:-192.0.2.10}"
PORT="${3:-18090}"
VAR="$ROOT/var"
LOG="$ROOT/console.log"
TOKEN_FILE="$VAR/config/console-token"
DB="$VAR/data/asistent.db"

say() { printf '\n=== %s ===\n' "$1"; }

cd "$ROOT" || exit 1

say "运行环境"
date -Is
hostname
python3 -V
python3 -c "import sqlite3, hmac, http.server, socketserver; print('stdlib ok:', sqlite3.sqlite_version)"

say "1. token 文件（仓库外，700/600）"
mkdir -p -m 700 "$VAR/config" "$VAR/data"
if [ ! -f "$TOKEN_FILE" ]; then
  openssl rand -hex 32 > "$TOKEN_FILE"
fi
chmod 700 "$VAR/config" "$VAR/data"
chmod 600 "$TOKEN_FILE"
ls -ld "$VAR/config"
ls -l "$TOKEN_FILE"
echo "token 长度: $(wc -c < "$TOKEN_FILE") 字节（含换行）"

say "1b. 权限不合格时必须拒绝启动（把文件改成 644 试一次）"
chmod 644 "$TOKEN_FILE"
python3 -c "
import sys; sys.path.insert(0, '$ROOT')
from asistent.console.auth import load_token, AuthError
try:
    load_token('$TOKEN_FILE')
    print('意外：权限过宽竟然被接受了')
except AuthError as exc:
    print('按预期拒绝启动：', exc)
"
chmod 600 "$TOKEN_FILE"

say "2. 在 ${BIND}:${PORT} 起服务（后台）"
pkill -f "tests/console/serve_once.py" 2>/dev/null || true
sleep 0.5
nohup python3 -X utf8 "$ROOT/tests/console/serve_once.py" \
  "{\"store\": \"sqlite\", \"db_path\": \"$DB\", \"token_path\": \"$TOKEN_FILE\", \"port\": $PORT, \"bind\": \"$BIND\"}" \
  > "$LOG" 2>&1 &
sleep 1.5
cat "$LOG"

say "2b. 监听地址（只应绑在内网网卡上，不是 0.0.0.0）"
ss -ltnp | grep -E "$PORT" || echo "没有监听 $PORT"

say "3. curl 原始请求（raw）"
TOKEN="$(cat "$TOKEN_FILE")"
echo "--- 3.1 无 token ---"
curl -sS -i "http://${BIND}:${PORT}/api/health" | sed -n '1,12p'
echo "--- 3.2 错 token ---"
curl -sS -i -H "Authorization: Bearer wrong-token-000000000000000000000000" \
  "http://${BIND}:${PORT}/api/health" | sed -n '1,12p'
echo "--- 3.3 未鉴权访问一个不存在的 Case（不得与「存在」可区分）---"
curl -sS -i "http://${BIND}:${PORT}/api/cases/case_does_not_exist" | tail -5
echo "--- 3.4 对 token：/api/health ---"
curl -sS -i -H "Authorization: Bearer $TOKEN" "http://${BIND}:${PORT}/api/health" | sed -n '1,12p'
echo "--- 3.5 对 token：/api/overview（待处理快照 + 游标 + ETag）---"
curl -sS -i -H "Authorization: Bearer $TOKEN" "http://${BIND}:${PORT}/api/overview" | sed -n '1,14p'
echo "--- 3.6 对 token：静态壳（PWA 首页与清单）---"
curl -sS -o /dev/null -w 'HTTP %{http_code}  content-type=%{content_type}  bytes=%{size_download}\n' \
  -H "Authorization: Bearer $TOKEN" "http://${BIND}:${PORT}/"
curl -sS -o /dev/null -w 'HTTP %{http_code}  content-type=%{content_type}  bytes=%{size_download}\n' \
  -H "Authorization: Bearer $TOKEN" "http://${BIND}:${PORT}/manifest.webmanifest"
curl -sS -o /dev/null -w 'HTTP %{http_code}  content-type=%{content_type}  bytes=%{size_download}\n' \
  -H "Authorization: Bearer $TOKEN" "http://${BIND}:${PORT}/app.js"
echo "--- 3.7 未鉴权拿静态壳（也必须 401）---"
curl -sS -o /dev/null -w 'HTTP %{http_code}  bytes=%{size_download}\n' "http://${BIND}:${PORT}/"
echo "--- 3.8 token 不得出现在日志里 ---"
grep -c "$TOKEN" "$LOG" || true
echo "（上面必须是 0）"

say "3c. 手机端拿到的 JSON（未鉴权的固定文案）"
curl -sS "http://${BIND}:${PORT}/api/cases"
echo

say "4. 测试"

# unittest 把结果写在 **stderr** 上，有些层还会往 stderr 打 [evidence] 诊断行。
# 这里先把 stderr 收进临时文件再过滤，保证贴进文档的是"干净的结论"而不是噪音。
run_suite() {
  local label="$1"; shift
  local out
  out="$(mktemp)"
  if "$@" > "$out" 2>&1; then :; fi
  echo "--- $label ---"
  grep -E '^(Ran |OK|FAILED|ERROR: |FAIL: )' "$out" | tail -5
  rm -f "$out"
}

run_suite "4.1 理解层（回归）" python3 -X utf8 -m unittest discover -s tests/understand -t .
run_suite "4.2 控制台（本层）" python3 -X utf8 -m unittest discover -s tests/console -t .
run_suite "4.3 全量（reference 后端）" python3 -X utf8 -m unittest discover -s tests -t .
run_suite "4.4 全量（sqlite 后端）" env ASISTENT_STORE=sqlite python3 -X utf8 -m unittest discover -s tests -t .

say "5. 收尾"
pkill -f "tests/console/serve_once.py" 2>/dev/null || true
sleep 0.5
ss -ltnp | grep -E "$PORT" || echo "服务已停止，端口已释放"
echo "工作目录：$ROOT（完全在 ~/code/asistent 之外）"
