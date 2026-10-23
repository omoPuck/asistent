#!/bin/bash
# 把板子接入 Tailscale，并用 HTTPS 暴露控制台，让手机从任意网络都能打开（并装成应用）。
#
# 为什么需要你自己跑一次：这一步要 root（装包、启服务、动 tailscaled），而我会话里的
# sudo 需要密码——凭据纪律上，你的密码不该出现在对话里，所以做成"我给你脚本、你跑一次"。
#
# 用法（在板子上）：
#     sudo bash ~/code/asistent/scripts/tailscale-onboard.sh
#
# 幂等：重复运行只会补齐缺的步骤，不会把已配好的东西弄坏。
# 不打印任何机密：不进登录密钥、不接管板子的 DNS。
set -uo pipefail

VAR="${ASISTENT_VAR:-$HOME/asistent-var}"
CONFIG="$VAR/config/service.json"
SERVE_TARGET=""
STEP_FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  [OK]   %s\n' "$*"; }
warn() { printf '  [注意] %s\n' "$*"; }
bad()  { printf '  [需要处理] %s\n' "$*"; STEP_FAIL=1; }

if [ "$(id -u)" != "0" ]; then
    say "请用 root 运行：sudo bash $0"
    exit 2
fi

# 控制台的监听地址从服务配置里读，避免把地址写死在两个地方
if [ -f "$CONFIG" ]; then
    SERVE_TARGET=$(python3 - "$CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
console = cfg.get("console", {})
bind = console.get("bind") or "127.0.0.1"
port = console.get("port") or 18090
print("http://%s:%s" % (bind, port))
PY
)
fi
[ -n "$SERVE_TARGET" ] || SERVE_TARGET="http://127.0.0.1:18090"
say "控制台目标：$SERVE_TARGET"
say ""

say "== 1/6 装 tailscale（幂等） =="
if command -v tailscale >/dev/null 2>&1; then
    ok "已安装：$(tailscale version | head -1)"
else
    if [ ! -f /usr/share/keyrings/tailscale-archive-keyring.gpg ]; then
        curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.noarmor.gpg \
            -o /usr/share/keyrings/tailscale-archive-keyring.gpg || bad "下载签名密钥失败"
    fi
    [ -f /etc/apt/sources.list.d/tailscale.list ] || \
        echo "deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/ubuntu jammy main" \
            > /etc/apt/sources.list.d/tailscale.list
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq || bad "apt-get update 失败"
    apt-get install -y -qq tailscale || bad "安装 tailscale 失败"
    command -v tailscale >/dev/null 2>&1 && ok "安装完成：$(tailscale version | head -1)" || bad "装完仍找不到 tailscale"
fi
say ""

say "== 2/6 启动 tailscaled 并设为开机自启 =="
systemctl enable --now tailscaled >/dev/null 2>&1
if systemctl is-active --quiet tailscaled; then ok "tailscaled active"; else bad "tailscaled 未运行"; fi
say ""

say "== 3/6 加入 tailnet（不接管 DNS） =="
# --accept-dns=false：**不碰板子的 /etc/resolv.conf**，内网名字解析照旧。
#   手机侧要用的 MagicDNS 名字由手机自己的客户端解析，与板子的 DNS 无关。
STATE=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('BackendState',''))" 2>/dev/null)
if [ "$STATE" = "Running" ]; then
    ok "已经在线（BackendState=Running）"
else
    say "  现在会打印一个登录链接。**请用手机或浏览器打开它并登录**——"
    say "  登录的是你自己的账号，所以只有你能完成这一步。"
    say ""
    tailscale up --accept-dns=false --timeout=15m || bad "tailscale up 未完成（登录超时？可重跑本脚本）"
    STATE=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('BackendState',''))" 2>/dev/null)
    [ "$STATE" = "Running" ] && ok "已上线" || bad "仍未上线"
fi
say ""

say "== 4/6 节点信息 =="
FQDN=$(tailscale status --json 2>/dev/null | python3 -c "
import json,sys
d = json.load(sys.stdin)
self = d.get('Self') or {}
print((self.get('DNSName') or '').rstrip('.'))
" 2>/dev/null)
IP4=$(tailscale ip -4 2>/dev/null | head -1)
say "  tailnet 域名：${FQDN:-（未取到，通常是 MagicDNS 没开）}"
say "  tailnet IPv4：${IP4:-（未取到）}"
[ -n "$FQDN" ] || warn "没有域名不影响用 IP 访问，但 HTTPS 需要域名（见下一步）"
say ""

say "== 5/6 用 HTTPS 暴露控制台（tailscale serve） =="
say "  TLS 在 Tailscale 侧终止，控制台本身不用改协议；这样手机侧才是安全上下文，"
say "  PWA 才能注册 Service Worker（明文 HTTP 下只能'添加到主屏幕'，没有离线壳）。"
SERVE_OUT=$(tailscale serve --bg --https=443 "$SERVE_TARGET" 2>&1)
SERVE_RC=$?
printf '%s\n' "$SERVE_OUT" | sed 's/^/    /'
if [ $SERVE_RC -ne 0 ]; then
    bad "serve 配置失败——最常见原因是 tailnet 还没开 HTTPS 证书"
    say "        请在 Tailscale 管理控制台 → DNS → 打开 'HTTPS Certificates'，然后重跑本脚本。"
else
    ok "serve 已配置（后台常驻）"
fi
say ""

say "== 6/6 自检 =="
tailscale serve status 2>&1 | sed 's/^/    /' || true
if [ -n "${FQDN:-}" ]; then
    CODE=$(curl -s -o /dev/null -m 12 -w '%{http_code}' "https://$FQDN/" 2>/dev/null)
    say "    经 tailnet 域名访问控制台：HTTP ${CODE:-无响应}"
    case "${CODE:-}" in
        401) ok "链路通（401 = 正是'需要令牌'，说明请求真的到达了控制台）" ;;
        000) bad "没取到响应：检查第 5 步是否成功、手机是否在同一 tailnet" ;;
        *)   warn "返回 $CODE，请人工看一眼" ;;
    esac
fi
say ""
say "================ 手机侧怎么用 ================"
say "1. 手机装 Tailscale 客户端，用**同一个账号**登录；"
say "2. 手机浏览器打开："
say "     https://${FQDN:-<你的节点名>.<tailnet>.ts.net}/"
say "   页面上会要令牌：在板子上执行 `cat ${VAR}/config/console-token` 取出来粘进去。"
say "   ⚠ **不要把令牌写进 URL**（形如 ?token=…）：控制台按设计只把它放在内存变量里，"
say "     不进 URL、不落浏览器持久存储——贴进地址栏会让它进浏览器历史，白丢一层保护。"
say "   ⚠ 也正因为不落存储：关掉页面后再打开需要**重新粘一次令牌**。"
say "     （这是当前的有意设计，不是 bug；若嫌烦，可以让控制台在手机上记住会话，"
say "       但那属于安全模型变更，要先定成 ADR/原型再做。）"
say "3. 浏览器菜单 →'添加到主屏幕'/'安装应用'：因为现在是 HTTPS，"
say "   这一步会得到一个带离线壳的独立应用图标。"
say ""
if [ "$STEP_FAIL" = "1" ]; then
    say "结果：有步骤需要处理（见上面 [需要处理]）。修好后重跑本脚本即可。"
    exit 1
fi
say "结果：全部完成。"
