#!/usr/bin/env bash
# asistent · frpc 接入（板子 → 阿里云 frps → 手机）
#
# 为什么用 frp 而不是 Tailscale：ADR-0006（docs/adr/0006-phone-access-via-frp.md）。
# 一句话：手机在外网、板子在公司内网、公司不给装客户端；用一台自有公网服务器做中转，
# 板子只**出站**连 frps，公网侧只暴露一个端口（默认 18800）。
#
# 这个脚本做的事（全部幂等，可反复跑）：
#   1. 装 frpc 到 ~/asistent-var/bin（用户目录，不需要 sudo）；
#   2. 用 token 文件生成 ~/asistent-var/config/frpc.toml（600，token 不进 argv/日志）；
#   3. 写 systemd **user** unit 并 enable；
#   4. 自检：进程活着、frps 端口可连、本地控制台端口在听。
#
# 凭据约定：token 只从 --token-file 指定的文件读（建议 600）；本脚本**不打印**它。
#
# 用法：
#   bash scripts/frp-onboard.sh --server 203.0.113.50 --token-file ~/asistent-var/config/frp-token
#   bash scripts/frp-onboard.sh --status          # 只看现状
#   bash scripts/frp-onboard.sh --server ... --token-file ... --verify-url http://<公网>:18800/
#
# 退出码：0 成功；1 前置条件不满足；2 下载/校验失败；3 启动后自检失败。

set -u

FRP_VERSION="${FRP_VERSION:-0.71.1}"
SERVER="${FRP_SERVER:-}"
SERVER_PORT="${FRP_SERVER_PORT:-7000}"
REMOTE_PORT="${FRP_REMOTE_PORT:-18800}"
LOCAL_PORT="${FRP_LOCAL_PORT:-18090}"
PROXY_NAME="${FRP_PROXY_NAME:-asistent-console}"
TOKEN_FILE=""
VERIFY_URL=""
ACTION="install"
ALLOW_UNVERIFIED=0

VAR_ROOT="${ASISTENT_VAR:-$HOME/asistent-var}"
BIN_DIR="$VAR_ROOT/bin"
CONFIG_DIR="$VAR_ROOT/config"
LOG_DIR="$VAR_ROOT/logs"
FRPC_BIN="$BIN_DIR/frpc"
FRPC_CONFIG="$CONFIG_DIR/frpc.toml"
TOKEN_COPY="$CONFIG_DIR/frp-token"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/asistent-frpc.service"
LOG_FILE="$LOG_DIR/frpc.log"

info()  { printf '  %s\n' "$*"; }
warn()  { printf '  ! %s\n' "$*" >&2; }
fail()  { printf '错误：%s\n' "$*" >&2; exit "${2:-1}"; }

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2:-}"; shift 2 ;;
    --server-port) SERVER_PORT="${2:-}"; shift 2 ;;
    --remote-port) REMOTE_PORT="${2:-}"; shift 2 ;;
    --local-port) LOCAL_PORT="${2:-}"; shift 2 ;;
    --proxy-name) PROXY_NAME="${2:-}"; shift 2 ;;
    --token-file) TOKEN_FILE="${2:-}"; shift 2 ;;
    --verify-url) VERIFY_URL="${2:-}"; shift 2 ;;
    --allow-unverified) ALLOW_UNVERIFIED=1; shift ;;
    --status) ACTION="status"; shift ;;
    -h|--help) usage ;;
    *) fail "未知参数：$1（--help 看用法）" ;;
  esac
done

# ---------------------------------------------------------------- 只读状态

if [ "$ACTION" = "status" ]; then
  echo "=== frpc 现状 ==="
  if [ -x "$FRPC_BIN" ]; then info "binary: $FRPC_BIN ($("$FRPC_BIN" -v 2>&1))"; else info "binary: 未安装"; fi
  if [ -f "$FRPC_CONFIG" ]; then
    info "config: $FRPC_CONFIG（权限 $(stat -c '%A' "$FRPC_CONFIG" 2>/dev/null || echo '?')）"
    grep -E '^(serverAddr|serverPort)' "$FRPC_CONFIG" | sed 's/^/    /'
    grep -E '^(name|type|localIP|localPort|remotePort)' "$FRPC_CONFIG" | sed 's/^/    /'
  else
    info "config: 未生成"
  fi
  info "unit: $(systemctl --user is-active asistent-frpc 2>/dev/null) / 开机自启 $(systemctl --user is-enabled asistent-frpc 2>/dev/null)"
  if [ -f "$LOG_FILE" ]; then
    echo "  --- $LOG_FILE 最后 8 行 ---"
    tail -8 "$LOG_FILE" | sed 's/^/    /'
  fi
  exit 0
fi

# ---------------------------------------------------------------- 前置检查

echo "=== 1) 前置检查 ==="

if [ -z "$SERVER" ]; then
  fail "缺少 --server（frps 的公网地址），或设置 FRP_SERVER 环境变量"
fi
if [ -z "$TOKEN_FILE" ]; then
  fail "缺少 --token-file（600 的 frps token 文件）。token 不放命令行参数，避免进 ps/历史记录"
fi
[ -f "$TOKEN_FILE" ] || fail "token 文件不存在：$TOKEN_FILE"

TOKEN_MODE=$(stat -c '%a' "$TOKEN_FILE" 2>/dev/null || echo "")
case "$TOKEN_MODE" in
  600|400) : ;;
  "") warn "读不到 token 文件权限（非 Linux？）" ;;
  *) warn "token 文件权限是 $TOKEN_MODE，建议 chmod 600" ;;
esac

FRPC_TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null | tr -d '\r\n')
[ -n "$FRPC_TOKEN" ] || fail "token 文件是空的：$TOKEN_FILE"

command -v curl >/dev/null 2>&1 || fail "需要 curl"

ARCH=$(uname -m)
case "$ARCH" in
  aarch64|arm64) FRP_ARCH="linux_arm64" ;;
  x86_64|amd64)  FRP_ARCH="linux_amd64" ;;
  armv7l)        FRP_ARCH="linux_arm" ;;
  *) fail "不认识的架构：$ARCH" ;;
esac
info "架构 $ARCH → $FRP_ARCH；frpc 目标版本 $FRP_VERSION"

# 本地控制台在不在听（frpc 转发目标）；不在听不是致命错（服务可能还没起）
if command -v ss >/dev/null 2>&1 && ss -lnt 2>/dev/null | grep -q ":$LOCAL_PORT "; then
  info "本地控制台 127.0.0.1:$LOCAL_PORT 在监听 ✓"
else
  warn "本地 127.0.0.1:$LOCAL_PORT 没在监听：frpc 会连上但转发会失败（先确认 asistent 服务在跑）"
fi

# 到 frps 的连通性（板子必须能出站到 7000）
if timeout 8 bash -c "cat < /dev/null > /dev/tcp/$SERVER/$SERVER_PORT" 2>/dev/null; then
  info "frps $SERVER:$SERVER_PORT 可连 ✓"
else
  fail "连不上 frps $SERVER:$SERVER_PORT（检查：出网策略 / 云安全组是否放行 / frps 是否在跑）"
fi

mkdir -p "$BIN_DIR" "$CONFIG_DIR" "$LOG_DIR"
chmod 700 "$BIN_DIR" 2>/dev/null || true

# ---------------------------------------------------------------- 装二进制

echo
echo "=== 2) frpc 二进制 ==="

NEED_DOWNLOAD=0
if [ -x "$FRPC_BIN" ]; then
  INSTALLED=$("$FRPC_BIN" -v 2>/dev/null | head -1)
  if [ "$INSTALLED" = "$FRP_VERSION" ]; then
    info "已装 $INSTALLED，跳过下载"
  else
    warn "已装版本 ${INSTALLED:-未知}，与目标 $FRP_VERSION 不一致 → 重装"
    NEED_DOWNLOAD=1
  fi
else
  NEED_DOWNLOAD=1
fi

if [ "$NEED_DOWNLOAD" = "1" ]; then
  TARBALL="frp_${FRP_VERSION}_${FRP_ARCH}.tar.gz"
  TMPDIR_FRP=$(mktemp -d)
  trap 'rm -rf "$TMPDIR_FRP"' EXIT

  # 国内网络：GitHub 直连常常 0 字节，镜像优先，直连兜底
  BASE_GH="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}"
  MIRRORS="${FRP_MIRROR:-https://gh-proxy.com/https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}}"
  URLS="$MIRRORS/$TARBALL $BASE_GH/$TARBALL"

  GOT=""
  for url in $URLS; do
    info "尝试下载 $url"
    if curl -fsSL -m 300 -o "$TMPDIR_FRP/$TARBALL" "$url"; then
      GOT="$url"
      break
    fi
    warn "下载失败：$url"
  done
  [ -n "$GOT" ] || fail "所有下载源都失败（可设 FRP_MIRROR 换镜像）" 2
  info "包大小 $(stat -c %s "$TMPDIR_FRP/$TARBALL") 字节"

  # 校验和：官方 release 旁有 frp_sha256_checksums.txt，能拿到就必须对上
  SUM_OK=0
  for url in "$MIRRORS/frp_sha256_checksums.txt" "$BASE_GH/frp_sha256_checksums.txt"; do
    if curl -fsSL -m 60 -o "$TMPDIR_FRP/sums.txt" "$url" 2>/dev/null; then
      if (cd "$TMPDIR_FRP" && grep " $TARBALL\$" sums.txt > want.txt && sha256sum -c want.txt >/dev/null 2>&1); then
        info "sha256 校验通过 ✓"
        SUM_OK=1
      fi
      break
    fi
  done
  if [ "$SUM_OK" = "0" ]; then
    if [ "$ALLOW_UNVERIFIED" = "1" ]; then
      warn "拿不到官方校验和，按 --allow-unverified 继续（来源：$GOT）"
    else
      fail "拿不到官方 sha256 校验和。确认镜像可信后可加 --allow-unverified 重跑" 2
    fi
  fi

  tar -xzf "$TMPDIR_FRP/$TARBALL" -C "$TMPDIR_FRP" || fail "解包失败" 2
  install -m 700 "$TMPDIR_FRP/frp_${FRP_VERSION}_${FRP_ARCH}/frpc" "$FRPC_BIN" || fail "安装 frpc 失败" 2
  rm -rf "$TMPDIR_FRP"
  trap - EXIT
  info "已安装 $("$FRPC_BIN" -v 2>&1)"
fi

# ---------------------------------------------------------------- 写配置

echo
echo "=== 3) 配置 ==="

# token 单独留一份 600 副本：重装/重跑时不必再问一次，也不会散落在别处
printf '%s\n' "$FRPC_TOKEN" > "$TOKEN_COPY"
chmod 600 "$TOKEN_COPY"

WANT_CONFIG=$(cat <<EOF
# asistent 控制台穿透（由 scripts/frp-onboard.sh 生成，勿手改）
# 板子 127.0.0.1:${LOCAL_PORT}  →  公网 ${SERVER}:${REMOTE_PORT}
# 板子只做出站连接；公网侧不开放任何入站端口到板子。
serverAddr = "${SERVER}"
serverPort = ${SERVER_PORT}

auth.method = "token"
auth.token = "${FRPC_TOKEN}"

transport.tls.enable = true

[[proxies]]
name = "${PROXY_NAME}"
type = "tcp"
localIP = "127.0.0.1"
localPort = ${LOCAL_PORT}
remotePort = ${REMOTE_PORT}
EOF
)

RESTART_NEEDED=0
if [ -f "$FRPC_CONFIG" ] && [ "$(cat "$FRPC_CONFIG")" = "$WANT_CONFIG" ]; then
  info "配置无变化：$FRPC_CONFIG"
else
  printf '%s\n' "$WANT_CONFIG" > "$FRPC_CONFIG"
  chmod 600 "$FRPC_CONFIG"
  info "已写入 $FRPC_CONFIG（600）"
  RESTART_NEEDED=1
fi
unset FRPC_TOKEN WANT_CONFIG

# ---------------------------------------------------------------- 写单元

echo
echo "=== 4) systemd user unit ==="
mkdir -p "$UNIT_DIR"
WANT_UNIT=$(cat <<'EOF'
[Unit]
Description=asistent frpc - map console to frps (outbound only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/asistent-var/bin/frpc -c %h/asistent-var/config/frpc.toml
Restart=always
RestartSec=5
# 这台板子的 user journal 是空的，所以同时落文件，排障才看得见
StandardOutput=append:%h/asistent-var/logs/frpc.log
StandardError=append:%h/asistent-var/logs/frpc.log

[Install]
WantedBy=default.target
EOF
)

if [ -f "$UNIT_FILE" ] && [ "$(cat "$UNIT_FILE")" = "$WANT_UNIT" ]; then
  info "unit 无变化：$UNIT_FILE"
else
  printf '%s\n' "$WANT_UNIT" > "$UNIT_FILE"
  info "已写入 $UNIT_FILE"
  RESTART_NEEDED=1
fi
unset WANT_UNIT

systemctl --user daemon-reload

# ---------------------------------------------------------------- 启动 + 自检

echo
echo "=== 5) 启动 ==="
systemctl --user enable asistent-frpc >/dev/null 2>&1 || warn "enable 失败（linger 没开？）"
if [ "$RESTART_NEEDED" = "1" ]; then
  systemctl --user restart asistent-frpc || fail "启动失败：journalctl --user -u asistent-frpc -n 50" 3
else
  systemctl --user start asistent-frpc || fail "启动失败" 3
fi

for _ in $(seq 1 15); do
  sleep 1
  if systemctl --user is-active asistent-frpc >/dev/null 2>&1 && [ -f "$LOG_FILE" ] && grep -q "start proxy success" "$LOG_FILE"; then
    break
  fi
done

echo
echo "=== 6) 自检 ==="
STATUS=$(systemctl --user is-active asistent-frpc 2>/dev/null)
info "服务状态: $STATUS"
if [ -f "$LOG_FILE" ]; then
  tail -4 "$LOG_FILE" | sed 's/^/    /'
fi

if [ "$STATUS" != "active" ]; then
  fail "frpc 没起来：journalctl --user -u asistent-frpc -n 50 或看 $LOG_FILE" 3
fi
if [ -f "$LOG_FILE" ] && ! grep -q "start proxy success" "$LOG_FILE"; then
  warn "日志里没有 'start proxy success'：注册可能没成功（看 $LOG_FILE）"
fi

if [ -n "$VERIFY_URL" ]; then
  CODE=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$VERIFY_URL" 2>/dev/null)
  info "公网自检 $VERIFY_URL → HTTP $CODE（401/200 都算通；000 表示公网端口没放行）"
fi

cat <<EOF

完成。手机侧入口：
  http://${SERVER}:${REMOTE_PORT}/          （需要云安全组放行 ${REMOTE_PORT}）
想看现状：bash scripts/frp-onboard.sh --status
EOF
