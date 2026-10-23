#!/bin/bash
# 交互式写入 163 客户端授权码：不回显、不落 shell 历史、权限 600。
#
# 用法（在设备上的交互式 SSH 会话里执行）：
#   bash ~/asistent-var/bin/set-mail-authcode.sh
#
# 这个文件由 `scripts/run-service.sh install-helpers` 安装到 `$ASISTENT_VAR/bin/`（700）。
# 之所以放进仓库而不是"设备上手工放一个"：它是**用户动作 U-1** 的唯一入口，
# 只存在于设备上就意味着"换台机器就得重新手写一遍"，部署链路里也就没有它。
#
# 写完之后谁用它：`mail.source=mcp` 时服务会读这个文件，把内容作为
# `MCP_EMAIL_SERVER_PASSWORD` 放进 `mcp-email-server` 子进程的环境变量
# （见 `asistent/service/mail_source.py`）。凭据不进配置、不进 argv、不进日志。
set -eu

TARGET_DIR="${ASISTENT_VAR:-$HOME/asistent-var}/config"
TARGET="$TARGET_DIR/mail-authcode.txt"

mkdir -p "$TARGET_DIR"
chmod 700 "$TARGET_DIR"

if [ -f "$TARGET" ]; then
    printf '已存在 %s，覆盖吗? [y/N] ' "$TARGET"
    read -r answer
    case "$answer" in
        y | Y) ;;
        *)
            echo "已取消，未改动任何内容。"
            exit 0
            ;;
    esac
fi

cat <<'TIP'
说明：
  - 163 邮箱需要在网页端「设置 → POP3/SMTP/IMAP」里开启 IMAP/SMTP，并生成「客户端授权码」；
  - 这里要填的是那个授权码，不是邮箱登录密码；
  - 输入时不会回显，粘贴后直接回车即可。
TIP

printf '请粘贴 163 客户端授权码（不回显）: '
IFS= read -rs CODE
echo

# 去掉可能被一起粘进来的空白与换行
CODE=$(printf '%s' "$CODE" | tr -d '[:space:]')

if [ -z "$CODE" ]; then
    echo "输入为空，已取消。"
    exit 1
fi

umask 077
printf '%s' "$CODE" >"$TARGET"
chmod 600 "$TARGET"
unset CODE

echo "已写入: $TARGET"
echo "权限  : $(stat -c '%a %U:%G' "$TARGET")"
echo "长度  : $(wc -c <"$TARGET") 字节（内容不会显示）"
echo
echo "下一步：重启服务让它读到（不需要说出内容、也不需要把它贴到任何地方）："
echo "  systemctl --user restart asistent    # systemd --user 常驻时"
echo "  $HOME/code/asistent/scripts/run-service.sh restart   # setsid 降级方式"
