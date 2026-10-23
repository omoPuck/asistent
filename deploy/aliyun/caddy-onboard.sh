#!/usr/bin/env bash
# asistent · 阿里云侧中转配置（frps 的 HTTPS 门面：Caddy 反代到 frpc 隧道出口）
#
# 架构（为什么这么摆）：
#   手机 ──https://<域名>/──▶ Caddy(443/80, 阿里云) ──▶ 127.0.0.1:18800 ──frps 隧道──▶ 板子控制台
#   * 板子只做出站连接，公网不开放任何入站端口到板子；
#   * 阿里云只暴露 80/443 两个端口（80 仅用于 Let's Encrypt HTTP-01 验证与跳转）；
#   * 以后别的服务想挂出来，**不用再开新端口**，在同一个 Caddyfile 里加一段反代即可；
#     真要独占端口（SSH、游戏服之类），按约定从 30000 开始往后排。
#
# 用法（在阿里云 ECS 上，需要 root）：
#   bash caddy-onboard.sh --check      # 只体检：DNS / 端口 / frp 上游 / 现有配置
#   bash caddy-onboard.sh --install    # 装 Caddy + 落配置 + 放行本机 ufw（不启动）
#   bash caddy-onboard.sh --start      # 启动并等待证书签发，然后自检 HTTPS
#   bash caddy-onboard.sh --status     # 现状
#
# 前置：云安全组放行 TCP 80 与 TCP 443 —— 这一步只能在控制台做，脚本会明确提示。
#
# 注意：脚本在本机读同目录的 Caddyfile；**不要**用 ssh 一行命令里的 sed 去处理它
# （实测：经 PowerShell→ssh 传 `s/\r$//`，远端收到的是 `s/r$//`，会把每行末尾的字母 r 吃掉，
#  例如 `-Server` 变成 `-Serve`）。

set -u

DOMAIN="${CADDY_DOMAIN:-asistent.example.com}"
UPSTREAM="${CADDY_UPSTREAM:-127.0.0.1:18800}"
CADDYFILE_SRC="$(cd "$(dirname "$0")" && pwd)/Caddyfile"
CADDYFILE_DST="/etc/caddy/Caddyfile"
ACTION="${1:---check}"

info() { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
fail() { printf '错误：%s\n' "$*" >&2; exit "${2:-1}"; }

need_root() {
  [ "$(id -u)" -eq 0 ] || fail "需要 root（这台机器是 ECS，脚本会装包/改 /etc）"
}

public_ip() {
  curl -s -m 8 https://api.ipify.org 2>/dev/null || curl -s -m 8 http://ifconfig.me 2>/dev/null || true
}

check() {
  need_root
  echo "=== 体检 ==="
  . /etc/os-release 2>/dev/null && info "系统: $PRETTY_NAME $(uname -r)"

  info "域名: $DOMAIN → 期望指向本机公网 IP"
  local resolved ip
  resolved=$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -1)
  ip=$(public_ip)
  info "  解析: ${resolved:-解析失败}"
  info "  公网: ${ip:-未知}"
  if [ -n "$resolved" ] && [ -n "$ip" ] && [ "$resolved" != "$ip" ]; then
    warn "解析结果与公网 IP 不一致：证书会签不下来"
  fi

  info "ACME 出网: $(curl -s -o /dev/null -m 10 -w '%{http_code}' https://acme-v02.api.letsencrypt.org/directory 2>/dev/null)"
  info "本机 ufw: $(ufw status 2>/dev/null | head -1 | sed 's/Status: //')"
  info "80/443 占用: $(ss -lnt 2>/dev/null | grep -cE ':(80|443) ') 个监听"
  info "Caddy: $(command -v caddy >/dev/null 2>&1 && caddy version || echo 未安装)"
  info "caddy 服务: $(systemctl is-active caddy 2>/dev/null) / 开机自启 $(systemctl is-enabled caddy 2>/dev/null)"

  info "上游隧道（frps 侧 18800）:"
  local code
  code=$(curl -s -o /dev/null -m 8 -w '%{http_code}' "http://$UPSTREAM/" 2>/dev/null)
  if [ "$code" = "401" ]; then
    info "  HTTP $code ✓（控制台在应答，鉴权在位）"
  else
    warn "  HTTP $code —— 期望 401；检查板子 frpc 是否在线、控制台是否在跑"
  fi

  info "frps 代理列表:"
  local pass
  pass=$(sed -nE 's/^[[:space:]]*webServer\.password[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/p' /etc/frp/frps.toml 2>/dev/null | head -1)
  curl -s -m 5 -u "admin:$pass" http://127.0.0.1:7500/api/proxy/tcp 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('    ' + '\n    '.join('%s: %s (remotePort=%s)' % (p['name'], p['status'], p['conf'].get('remotePort')) for p in d.get('proxies', [])))" 2>/dev/null \
    || warn "  面板读取失败"
}

install_caddy() {
  need_root
  echo "=== 安装 Caddy 并落配置 ==="
  if command -v caddy >/dev/null 2>&1; then
    info "已安装: $(caddy version)"
  else
    info "apt 安装（阿里云镜像）…"
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -qq caddy >/tmp/caddy-install.log 2>&1 || { tail -20 /tmp/caddy-install.log; fail "apt 安装失败" 2; }
    info "已安装: $(caddy version)"
  fi

  [ -f "$CADDYFILE_SRC" ] || fail "找不到同目录的 Caddyfile：$CADDYFILE_SRC"
  info "配置来源: $CADDYFILE_SRC"
  install -m 644 "$CADDYFILE_SRC" "$CADDYFILE_DST"
  caddy fmt --overwrite "$CADDYFILE_DST"
  caddy validate --config "$CADDYFILE_DST" --adapter caddyfile >/tmp/caddy-validate.log 2>&1 \
    && info "配置校验: Valid" || { tail -5 /tmp/caddy-validate.log; fail "配置校验失败"; }

  info "放行本机 ufw（与云安全组是两道不同的门，都要开）"
  ufw allow 80/tcp >/dev/null 2>&1 && info "  80/tcp ✓"
  ufw allow 443/tcp >/dev/null 2>&1 && info "  443/tcp ✓"

  # apt 安装会顺手把服务拉起来，但那时还没有配置/端口，让它停在"已配置但未启动"
  systemctl stop caddy 2>/dev/null || true
  info "caddy 已就绪但**未启动**（等云安全组放行 80/443 后再 --start）"
  warn "去阿里云控制台放行：TCP 80（证书验证）+ TCP 443（手机入口）"
}

start_caddy() {
  need_root
  echo "=== 启动 Caddy 并等待证书 ==="
  systemctl start caddy
  local i code
  for i in $(seq 1 30); do
    sleep 2
    code=$(curl -s -o /dev/null -m 8 -w '%{http_code}' --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/" 2>/dev/null)
    if [ "$code" = "401" ] || [ "$code" = "200" ]; then
      info "HTTPS 就绪（本机自测 HTTP $code）"
      break
    fi
  done
  [ "${code:-000}" = "401" ] || [ "${code:-000}" = "200" ] || {
    warn "本机 HTTPS 自测仍未通过（HTTP ${code:-000}）"
    warn "常见原因：云安全组没放行 80 → Let's Encrypt 无法验证；看 journalctl -u caddy"
  }
  info "证书状态:"
  journalctl -u caddy -n 400 --no-pager 2>/dev/null | grep -iE "certificate obtained|obtained certificate|serving|error" | tail -4 | sed 's/^/    /'
  info "外部验证（任一联网机器执行）:"
  info "  curl -s -o /dev/null -w '%{http_code}\\n' https://$DOMAIN/    # 期望 401"
}

status() {
  need_root
  echo "=== 现状 ==="
  info "caddy: $(systemctl is-active caddy 2>/dev/null) / 开机自启 $(systemctl is-enabled caddy 2>/dev/null)"
  info "配置: $CADDYFILE_DST"
  [ -f "$CADDYFILE_DST" ] && sed 's/^/    /' "$CADDYFILE_DST"
  info "监听: $(ss -lnt 2>/dev/null | grep -E ':(80|443) ' | tr '\n' ' ')"
  info "证书目录:"
  ls -1 /var/lib/caddy/.local/share/caddy/certificates/ 2>/dev/null | sed 's/^/    /' || info "    （还没有证书）"
}

case "$ACTION" in
  --check)   check ;;
  --install) install_caddy ;;
  --start)   start_caddy ;;
  --status)  status ;;
  -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) fail "未知参数：$ACTION（--check / --install / --start / --status）" ;;
esac
