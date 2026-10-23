#!/bin/sh
# MVP-8 常驻服务启动器（POSIX sh；设备端唯一入口）。
#
# 一台 RK3588 上的助手要"真的在跑"，就得有个能起、能停、能看状态的入口。
# 本脚本只做进程管理，**不做业务**：业务在 `python3 -m asistent.service` 里。
#
# 用法：
#   scripts/run-service.sh config-init          # 首次：写一份安全默认配置（不外发、不归档）
#   scripts/run-service.sh init-token           # 首次：生成控制台令牌（600，内容不打印）
#   scripts/run-service.sh install-helpers      # 安装运维脚本到 $ASISTENT_VAR/bin（如授权码录入）
#   scripts/run-service.sh doctor               # 环境自检（版本/目录/FTS5/setsid/bdpan 授权）
#   scripts/run-service.sh once                 # 跑恰好一轮（前台，日志打到终端）
#   scripts/run-service.sh start                # 常驻：setsid + nohup 后台跑，写 pidfile
#   scripts/run-service.sh stop                 # 优雅停止（SIGTERM，等当前轮跑完）
#   scripts/run-service.sh restart
#   scripts/run-service.sh status               # 健康快照（JSON 可读；含"正在等什么"）
#   scripts/run-service.sh logs [N]             # 看最近 N 行日志（默认 40）
#   scripts/run-service.sh crontab-line         # 打印 @reboot 降级启动用的 crontab 行
#   scripts/run-service.sh unit-file            # 打印 systemd --user 的 unit（首选用它常驻）
#   scripts/run-service.sh unit-file --stage    # 写到 $ASISTENT_VAR/systemd/asistent.service
#   scripts/run-service.sh soak --sample        # 浸泡取样：往 logs/soak.jsonl 追一行（只读）
#   scripts/run-service.sh soak --summary       # 把取样序列变成 24h 结论（没数据就说没数据）
#   scripts/run-service.sh soak --verdict       # 按 CP-37 预先冻结的判据机械判定过没过
#   scripts/run-service.sh soak-unit --stage    # 5 分钟一次的取样定时器（systemd --user timer）
#
# 环境变量（都有安全默认值）：
#   ASISTENT_VAR            运行数据根目录，默认 $HOME/asistent-var（700）
#   ASISTENT_PYTHON         python3 可执行文件，默认 `python3`
#   ASISTENT_STORE          存储后端：sqlite（默认，落盘）/ reference（内存，仅调试）
#   ASISTENT_SERVICE_CONFIG 配置文件，默认 $ASISTENT_VAR/config/service.json
#
# 凭据纪律（SPEC §21.3、A-6）：
#   * 本脚本**从不**读取、打印或转发任何口令/授权码/令牌内容；
#   * 控制台令牌只由服务自己从 `$ASISTENT_VAR/config/console-token`（600）读，
#     文件内容不进终端、不进日志、不进 pidfile、不进 argv；
#   * 邮箱授权码用 `$ASISTENT_VAR/bin/set-mail-authcode.sh`（700）另行写入，
#     本脚本不碰它；日志与快照里出现凭据即为缺陷。
#
# 两套启动方式（都验证过；systemd 不可用时用降级方式）：
#
#   1) 推荐：systemd --user（设备已 `loginctl enable-linger`，开机即起、崩溃自动重启）
#         install -m 600 ...（模板见 docs/mvp/mvp-8.md §启动）
#         systemctl --user start asistent.service
#   2) 降级：本脚本的 `start`（setsid + nohup，脱离终端，关掉 SSH/桌面窗口都不受影响）
#         scripts/run-service.sh start
#      开机自启再加一行 crontab（`scripts/run-service.sh crontab-line` 打印的就是它）：
#         @reboot ASISTENT_VAR=$HOME/asistent-var $HOME/code/asistent/scripts/run-service.sh start
#
# 退出码：0 = 成功；1 = 用法/环境错误；其余为该命令自身（`once`/`status` 透传服务退出码）。
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

ASISTENT_VAR=${ASISTENT_VAR:-"$HOME/asistent-var"}
# 解释器：优先 ASISTENT_PYTHON，否则在候选里挑**真的能跑**的那个。
# 只写 `PYTHON=${ASISTENT_PYTHON:-python3}` 有两个坑：①"有 python 没有 python3"的机器上 doctor 显示成空；
# ②Windows 的 `WindowsApps/python3` 是应用商店占位符，`command -v` 找得到但一跑就无声无息。
# 所以这里对每个候选做一次 `-c` 试跑，只认真正能执行的那个。
pick_python() {
    # 显式指定的优先，且整条路径当字符串用（路径里可能有空格）
    if [ -n "${ASISTENT_PYTHON:-}" ]; then
        if [ "$($ASISTENT_PYTHON -c 'print(1)' 2>/dev/null)" = "1" ]; then
            printf '%s\n' "$ASISTENT_PYTHON"
            return 0
        fi
        # 明确指定了却跑不起来 → 如实报错，**不要**改用别的解释器（那会让"我明明指定了"变成谜案）
        printf '%s\n' "$ASISTENT_PYTHON"
        return 1
    fi
    for candidate in python3 python python3.10; do
        path=$(command -v "$candidate" 2>/dev/null) || continue
        if [ "$("$path" -c 'print(1)' 2>/dev/null)" = "1" ]; then
            printf '%s\n' "$path"
            return 0
        fi
    done
    printf '%s\n' python3
    return 1
}
PYTHON=$(pick_python || true)
PYTHON_OK=0
[ "$("$PYTHON" -c 'print(1)' 2>/dev/null)" = "1" ] && PYTHON_OK=1
CONFIG=${ASISTENT_SERVICE_CONFIG:-"$ASISTENT_VAR/config/service.json"}
STATE_DIR="$ASISTENT_VAR/state"
LOG_DIR="$ASISTENT_VAR/logs"
PID_FILE="$STATE_DIR/service.pid"
LOG_FILE="$LOG_DIR/service.log"

# 只给"当前用户"：运行数据目录 700、日志/pid 600（含邮件主题与事务摘要，不该给别的用户看）
umask 077

usage() {
    sed -n '2,/^set -eu/p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
    echo "错误：$*" >&2
    exit 1
}

ensure_dirs() {
    mkdir -p "$ASISTENT_VAR" "$ASISTENT_VAR/config" "$ASISTENT_VAR/data" "$STATE_DIR" \
        "$LOG_DIR" "$ASISTENT_VAR/index" "$ASISTENT_VAR/raw" "$ASISTENT_VAR/archive" 2>/dev/null || true
    chmod 700 "$ASISTENT_VAR" "$STATE_DIR" "$LOG_DIR" 2>/dev/null || true
}

service_argv() {
    # 所有参数都来自本脚本的常量或用户显式传入的配置路径；没有任何秘密进 argv
    echo "$PYTHON -X utf8 -m asistent.service $*"
}

run_service() {
    if [ "$PYTHON_OK" != "1" ]; then
        printf '解释器不可用：%s\n' "$PYTHON" >&2
        printf '请用 ASISTENT_PYTHON=/path/to/python3 指定一个**能跑**的解释器（先跑 `%s doctor` 看自检）。\n' "$0" >&2
        return 1
    fi
    # shellcheck disable=SC2086
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH="$REPO_ROOT" ASISTENT_VAR="$ASISTENT_VAR" \
        "$PYTHON" -X utf8 -m asistent.service "$@"
}

find_service_pid() {
    # 不依赖 pgrep/pidof：直接扫 /proc，找命令行里带 `asistent.service --loop` 的那个进程。
    # 这样 `setsid` 是否 fork 都不影响"我们到底该 signal 谁"。
    for proc in /proc/[0-9]*; do
        [ -r "$proc/cmdline" ] || continue
        if tr '\0' ' ' < "$proc/cmdline" 2>/dev/null | grep -q 'asistent[.]service --loop'; then
            printf '%s' "${proc#/proc/}"
            return 0
        fi
    done
    return 1
}

pid_of_service() {
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            if [ -r "/proc/$pid/cmdline" ]; then
                if tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q 'asistent[.]service'; then
                    printf '%s' "$pid"
                    return 0
                fi
            else
                printf '%s' "$pid"
                return 0
            fi
        fi
    fi
    # pidfile 丢了/过期了也别慌：直接从 /proc 找
    find_service_pid
}

cmd_install_helpers() {
    # 把"用户动作"需要的脚本装到运行数据目录里（仓库外），这样换台机器也有同一套入口。
    # 只装脚本本身，绝不装任何凭据；装完提醒怎么用，但**不**替用户输入。
    ensure_dirs
    bin_dir="$ASISTENT_VAR/bin"
    mkdir -p "$bin_dir"
    chmod 700 "$bin_dir"
    for name in set-mail-authcode.sh; do
        source_path="$REPO_ROOT/scripts/$name"
        if [ ! -f "$source_path" ]; then
            echo "（跳过 $name：仓库里没有 $source_path）" >&2
            continue
        fi
        cp "$source_path" "$bin_dir/$name"
        chmod 700 "$bin_dir/$name"
        echo "已安装：$bin_dir/$name（700）"
    done
    echo
    echo "163 授权码录入（用户动作 U-1，交互式、不回显）："
    echo "  bash $bin_dir/set-mail-authcode.sh"
}

cmd_config_init() {
    ensure_dirs
    if [ -f "$CONFIG" ]; then
        echo "配置已存在，未改动：$CONFIG"
        return 0
    fi
    mkdir -p "$(dirname "$CONFIG")"
    chmod 700 "$(dirname "$CONFIG")" 2>/dev/null || true
    cat > "$CONFIG" <<EOF
{
  "var_root": "$ASISTENT_VAR",
  "store": "sqlite",
  "db_path": "$ASISTENT_VAR/data/asistent.db",
  "state_dir": "$STATE_DIR",
  "interval_seconds": 60,
  "max_rounds": 0,
  "batch_limit": 50,
  "console": {
    "enabled": true,
    "bind": "127.0.0.1",
    "port": 18090,
    "token_path": "$ASISTENT_VAR/config/console-token",
    "artifact_roots": ["$STATE_DIR", "$ASISTENT_VAR/raw"]
  },
  "mail": {
    "enabled": false,
    "source": "none",
    "account": "",
    "state_path": "$STATE_DIR/mail-sync.json",
    "raw_root": "$ASISTENT_VAR/raw"
  },
  "ingest": { "enabled": false, "roots": [], "index_path": "$ASISTENT_VAR/index/fts.db" },
  "outbound": { "enabled": true, "channel": "none", "from_account": "", "node": "board" },
  "archive": { "enabled": false, "channel": "none", "root": "$ASISTENT_VAR/archive/local-pan" }
}
EOF
    chmod 600 "$CONFIG"
    echo "已写入安全默认配置（mail/archive 关闭、外发通道 none）：$CONFIG"
    echo "启用真实能力前请逐项确认，并把凭据写进 $ASISTENT_VAR/bin/ 下的 700 脚本。"
}

cmd_init_token() {
    ensure_dirs
    run_service --init-token --config "$CONFIG"
    if [ -f "$ASISTENT_VAR/config/console-token" ]; then
        chmod 600 "$ASISTENT_VAR/config/console-token" 2>/dev/null || true
        echo "令牌文件权限：$(ls -l "$ASISTENT_VAR/config/console-token" | awk '{print $1}')（内容不打印）"
    fi
}

cmd_doctor() {
    ensure_dirs
    echo "== 服务自检 =="
    echo "仓库           : $REPO_ROOT"
    echo "运行根目录     : $ASISTENT_VAR ($(ls -ld "$ASISTENT_VAR" 2>/dev/null | awk '{print $1}'))"
    echo "配置文件       : $CONFIG $([ -f "$CONFIG" ] && echo '(存在)' || echo '(缺失，可先 config-init)')"
    if [ "$PYTHON_OK" = "1" ]; then
        echo "python         : $($PYTHON -V 2>&1) ($PYTHON)"
    elif [ -n "${ASISTENT_PYTHON:-}" ]; then
        echo "python         : 不可用！ASISTENT_PYTHON 指定的解释器跑不起来：$ASISTENT_PYTHON"
    else
        echo "python         : 不可用！候选 python3/python/python3.10 都跑不起来（装 Python 3.10+，或用 ASISTENT_PYTHON=/path/to/python3 指定）"
    fi
    echo "store 后端     : ${ASISTENT_STORE:-sqlite}"
    echo "setsid         : $(command -v setsid || echo '缺失（降级启动不可用）')"
    echo "crontab        : $(command -v crontab || echo '缺失')"
    echo "bdpan          : $(command -v bdpan 2>/dev/null || echo '不在 PATH') $([ -f "$HOME/.local/bin/bdpan" ] && echo "(~/.local/bin/bdpan 存在)")"
    echo "bdpan 授权目录 : $([ -d "$HOME/.config/bdpan" ] && echo '存在' || echo '不存在（未授权：归档前会显式 NOT_AUTHORIZED）')"
    echo "控制台令牌     : $([ -f "$ASISTENT_VAR/config/console-token" ] && ls -l "$ASISTENT_VAR/config/console-token" | awk '{print $1}' || echo '缺失（可先 init-token）')"
    echo "已部署软件版本 : $(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo '非 git 检出')"
    if [ "$PYTHON_OK" = "1" ]; then
        echo "FTS5           : $($PYTHON -c 'import sqlite3;c=sqlite3.connect(":memory:");c.execute("CREATE VIRTUAL TABLE t USING fts5(x)");print("OK", sqlite3.sqlite_version)' 2>&1)"
    else
        echo "FTS5           : 未检查（解释器不可用）"
    fi
    echo "运行中         : $(pid_of_service >/dev/null 2>&1 && echo "是 (pid $(pid_of_service))" || echo '否')"
}

cmd_once() {
    ensure_dirs
    run_service --once --config "$CONFIG"
}

cmd_start() {
    ensure_dirs
    if pid=$(pid_of_service); then
        echo "服务已在运行：pid=$pid"
        return 0
    fi
    # setsid + nohup：脱离终端会话，关掉 SSH / 桌面窗口都不影响（SPEC §14.2）
    # `--max-rounds 0` 显式写出来：常驻就是"永远跑下去"。配置里若残留 max_rounds=N，
    # 服务会在第 N 轮后正常退出——那看起来像"跑了一会儿自己死了"，是最难查的一类故障。
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH="$REPO_ROOT" ASISTENT_VAR="$ASISTENT_VAR" \
        setsid nohup $PYTHON -X utf8 -m asistent.service --loop --console --max-rounds 0 --config "$CONFIG" \
        >> "$LOG_FILE" 2>&1 < /dev/null &
    pid=$!
    sleep 2
    # `setsid` 在需要时会 fork，`$!` 未必是服务本体；以 /proc 扫到的真实进程为准。
    real=$(find_service_pid || true)
    [ -n "$real" ] && pid=$real
    echo "$pid" > "$PID_FILE"
    chmod 600 "$PID_FILE" 2>/dev/null || true
    chmod 600 "$LOG_FILE" 2>/dev/null || true
    if kill -0 "$pid" 2>/dev/null; then
        echo "服务已启动：pid=$pid（日志 $LOG_FILE）"
        echo "控制台：127.0.0.1:$(sed -n 's/.*"port": *\([0-9]*\).*/\1/p' "$CONFIG" | head -1)（手机端同地址 + 令牌）"
    else
        echo "服务启动后立即退出，请查看日志：$LOG_FILE" >&2
        tail -20 "$LOG_FILE" 2>/dev/null || true
        exit 1
    fi
}

cmd_stop() {
    if ! pid=$(pid_of_service); then
        echo "服务未在运行"
        rm -f "$PID_FILE"
        return 0
    fi
    echo "发送 SIGTERM 给 pid=$pid（等当前轮跑完，最长 30s）"
    kill -TERM "$pid" 2>/dev/null || true
    waited=0
    while [ "$waited" -lt 30 ]; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "优雅退出超时，发送 SIGKILL" >&2
        kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "服务已停止（等待 ${waited}s）"
}

cmd_status() {
    ensure_dirs
    if pid=$(pid_of_service); then
        echo "进程：运行中 pid=$pid"
    else
        echo "进程：未运行（下面显示的是最近一次落盘的状态快照）"
    fi
    run_service --status --config "$CONFIG"
}

cmd_logs() {
    lines=${1:-40}
    [ -f "$LOG_FILE" ] || die "日志不存在：$LOG_FILE"
    echo "== 最近 $lines 行：$LOG_FILE =="
    tail -n "$lines" "$LOG_FILE"
}

cmd_crontab_line() {
    echo "@reboot ASISTENT_VAR=$ASISTENT_VAR $REPO_ROOT/scripts/run-service.sh start"
}

# ----------------------------------------------------------------------------- systemd --user
# 设备已 `loginctl enable-linger asistent`（Linger=yes），所以**用户级 unit** 就能
# 无人登录常驻 + 开机自启，不需要 sudo，也不需要写 /etc/systemd/system。
# `unit-file` 打印 unit 内容（可 `> 文件` 保存）；`unit-file --stage` 写到
# `$ASISTENT_VAR/systemd/asistent.service` 并打印安装命令——本脚本**不**替你写
# `~/.config/systemd/user/`（那里由使用者自己决定）。
cmd_unit_file() {
    unit_dir="$ASISTENT_VAR/systemd"
    unit_path="$unit_dir/asistent.service"
    case "${1:-}" in
        "" | --print) ;;
        --stage)
            ensure_dirs
            mkdir -p "$unit_dir"
            ;;
        *)
            die "用法：run-service.sh unit-file [--stage]"
            ;;
    esac

    emit_unit() {
        cat <<EOF
# MVP-8 常驻服务（用户级 unit；设备已 enable-linger，不需要 sudo）
#
# 安装（设备上执行）：
#   mkdir -p ~/.config/systemd/user
#   $REPO_ROOT/scripts/run-service.sh unit-file > ~/.config/systemd/user/asistent.service
#   systemctl --user daemon-reload
#   systemctl --user enable --now asistent.service
#   systemctl --user status asistent.service
#
# 崩溃自动重启（Restart=always）。**重启不会重发**：新一轮先把
# IN_FLIGHT 的外部动作降级为 UNKNOWN 并从存储重算该做什么，
# 已提交给通道的动作绝不会因为进程重启而再发一次（见 docs/mvp/mvp-8.md §崩溃自愈）。
#
# 凭据纪律：本 unit 里**没有**任何口令/授权码/令牌；服务自己从
# $ASISTENT_VAR/config/console-token（600）与
# $ASISTENT_VAR/bin/set-mail-authcode.sh（700）之外的文件读取，内容不进日志。
[Unit]
Description=asistent 24h 常驻服务（邮件→理解→草稿→审批→外发→归档）
Documentation=file://$REPO_ROOT/docs/mvp/mvp-8.md
After=network-online.target
Wants=network-online.target
# 时钟就绪再启动：本项目的定序键是 (时间戳, 写入序号)，时钟错了两者就都不可信。
# 注意 `time-sync.target` 在**本机并不生效**——它由 systemd-timesyncd 通知，
# 而本机的时间同步是 chrony（实测那个 unit 从开机起一直卡在 activating）。
# 所以真正的保障是下面的 ExecStartPre 闸门，它不依赖是哪个 NTP 客户端。
After=time-sync.target

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
Environment=PYTHONUTF8=1
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONPATH=$REPO_ROOT
Environment=ASISTENT_VAR=$ASISTENT_VAR
Environment=ASISTENT_STORE=${ASISTENT_STORE:-sqlite}
# 时钟闸门：正常时零延迟放行；明显偏早则有界等待；等不到也启动，但要留下明确告警
# （拒绝启动会把 24h 助手整个干掉，比"时间戳不可信"更糟；静默启动则让人误以为时间是准的）
ExecStartPre=$PYTHON -X utf8 -m asistent.service.clockgate
ExecStart=$PYTHON -X utf8 -m asistent.service --loop --console --max-rounds 0 --config $CONFIG
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE
Restart=always
RestartSec=5
# 不因为"短时间重启太多次"而放弃：宁可慢一点重启，也不要静默死掉
StartLimitIntervalSec=0
KillSignal=SIGTERM
TimeoutStopSec=90
# 运行数据只在 $ASISTENT_VAR 里：unit 不给额外写权限
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=default.target
EOF
    }

    if [ "${1:-}" = "--stage" ]; then
        emit_unit >"$unit_path"
        chmod 600 "$unit_path"
        echo "已写出：$unit_path（600）"
        echo "在设备上执行："
        echo "  mkdir -p ~/.config/systemd/user"
        echo "  cp $unit_path ~/.config/systemd/user/asistent.service"
        echo "  systemctl --user daemon-reload && systemctl --user enable --now asistent.service"
        echo "日志：journalctl --user -u asistent -f   （或 tail -f $LOG_FILE）"
    else
        emit_unit
    fi
}

cmd_soak_unit() {
    unit_dir="$ASISTENT_VAR/systemd"
    service_path="$unit_dir/asistent-soak.service"
    timer_path="$unit_dir/asistent-soak.timer"
    case "${1:-}" in
        "" | --print) ;;
        --stage)
            ensure_dirs
            mkdir -p "$unit_dir"
            ;;
        *)
            die "用法：run-service.sh soak-unit [--stage]"
            ;;
    esac

    emit_service() {
        cat <<EOF
# 浸泡（soak）取样：每 5 分钟往 $ASISTENT_VAR/logs/soak.jsonl 追一行。
#
# 为什么要有它：24h 之后说"一直很稳"必须有曲线。内存/句柄泄漏、日志暴涨、
# 轮次失败率上升都**不会让进程死掉**，所以"服务还活着"证明不了稳定。
#
# 只读：不重启服务、不碰业务数据，只写自己那一行日志。
[Unit]
Description=asistent 浸泡取样（只读，一行 JSON/次）
Documentation=file://$REPO_ROOT/docs/mvp/mvp-8.md

[Service]
Type=oneshot
WorkingDirectory=$REPO_ROOT
Environment=PYTHONUTF8=1
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONPATH=$REPO_ROOT
Environment=ASISTENT_VAR=$ASISTENT_VAR
ExecStart=$PYTHON -X utf8 -m asistent.service.soak --sample
NoNewPrivileges=yes
EOF
    }

    emit_timer() {
        cat <<EOF
# 每 5 分钟一次；开机后 2 分钟开始（给服务自己先跑起来的时间）。
[Unit]
Description=asistent 浸泡取样定时器

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true
Unit=asistent-soak.service

[Install]
WantedBy=timers.target
EOF
    }

    if [ "${1:-}" = "--stage" ]; then
        emit_service >"$service_path"
        emit_timer >"$timer_path"
        chmod 600 "$service_path" "$timer_path"
        echo "已写出：$service_path（600）"
        echo "已写出：$timer_path（600）"
        echo "在设备上执行："
        echo "  mkdir -p ~/.config/systemd/user"
        echo "  cp $service_path $timer_path ~/.config/systemd/user/"
        echo "  systemctl --user daemon-reload && systemctl --user enable --now asistent-soak.timer"
        echo "看结论：$PYTHON -X utf8 -m asistent.service.soak --summary"
    else
        echo "--- asistent-soak.service ---"
        emit_service
        echo "--- asistent-soak.timer ---"
        emit_timer
    fi
}

cmd_soak() {
    # 手工取样 / 看结论（timer 没装或想立刻要一份时用）。
    # 注意走的是 `asistent.service.soak` 模块，不是 `asistent.service`。
    case "${1:-}" in
        "" | --sample) action=--sample ;;
        --summary) action=--summary ;;
        --verdict) action=--verdict ;;
        *) die "用法：run-service.sh soak [--sample|--summary|--verdict]" ;;
    esac
    if [ "$PYTHON_OK" != "1" ]; then
        printf '解释器不可用：%s\n' "$PYTHON" >&2
        return 1
    fi
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH="$REPO_ROOT" ASISTENT_VAR="$ASISTENT_VAR" \
        "$PYTHON" -X utf8 -m asistent.service.soak "$action"
}

[ $# -ge 1 ] || { usage; exit 1; }
command=$1
shift

case "$command" in
    config-init) cmd_config_init "$@" ;;
    init-token) cmd_init_token "$@" ;;
    install-helpers) cmd_install_helpers "$@" ;;
    doctor) cmd_doctor "$@" ;;
    once) cmd_once "$@" ;;
    start) cmd_start "$@" ;;
    stop) cmd_stop "$@" ;;
    restart)
        cmd_stop
        cmd_start
        ;;
    status) cmd_status "$@" ;;
    logs) cmd_logs "$@" ;;
    crontab-line) cmd_crontab_line "$@" ;;
    unit-file) cmd_unit_file "$@" ;;
    soak) cmd_soak "$@" ;;
    soak-unit) cmd_soak_unit "$@" ;;
    -h | --help | help) usage ;;
    *)
        echo "未知命令：$command" >&2
        usage
        exit 1
        ;;
esac
