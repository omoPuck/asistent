#!/usr/bin/env bash
#
# 最终验收窗口（24h）的起跑与判定。
#
# 为什么需要它：AT-01 要证明的是"连续跑满 24h"。这件事最容易出错的不是程序，是**口径**——
#   * 什么时候算起点？（上一轮就出错：文档写"08:37 起跑"，而进程自身 started_at 是 08:57）
#   * 怎么知道中途没重启？（`rounds_completed` 是跨进程累计值，重启后照样上涨）
#   * 什么时候才允许下结论？（人看着曲线觉得"差不多了"就宣布，等于自己挑判据）
# 所以把这三件事都交给脚本：起点写成一条记录、重启由 run_id 暴露、未满 24h 拒绝出结论。
#
# 用法（在设备上，仓库根目录下）：
#   scripts/acceptance-round.sh start     # 起跑：前置检查 → 记锚点 → 重启服务 → 立即取一个点
#   scripts/acceptance-round.sh status    # 看已跑多久、够不够 24h、锚点是什么
#   scripts/acceptance-round.sh verdict   # 未满 24h 拒绝判定；满 24h 才按 CP-37 判据出结论
#
# 行为边界：只动 `~/asistent-var`（运行数据）与 `systemctl --user` 的 asistent 单元；
# 不碰仓库、不用 sudo、不改任何系统文件。
set -u

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT" || exit 1
VAR="${ASISTENT_VAR:-$HOME/asistent-var}"
ANCHOR="$VAR/state/acceptance-round.json"
PY=/usr/bin/python3
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

log() { printf '%s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ 前置检查
preflight() {
    log "===== 起跑前置检查 ====="
    local blocked=0

    # 1) 邮件通道：真实收信是 AT-02 的证据来源
    if [ -f "$VAR/config/mail-authcode.txt" ]; then
        log "[OK]   邮件授权码文件存在（600/700 由脚本保证）"
    else
        log "[BLOCK] 邮件授权码文件不存在：真实收信（AT-02）无法验证"
        log "        在设备上运行：bash ~/asistent-var/bin/set-mail-authcode.sh"
        blocked=1
    fi

    # 2) 网盘授权：AT-12/13/14 的入口
    if command -v bdpan >/dev/null 2>&1 || [ -x "$HOME/.local/bin/bdpan" ]; then
        local who
        who=$("$HOME/.local/bin/bdpan" whoami --json 2>/dev/null | tr -d ' \n' || true)
        case "$who" in
            *'"authenticated":true'*) log "[OK]   bdpan 已授权" ;;
            *) log "[BLOCK] bdpan 未授权：归档只能停在 NOT_AUTHORIZED（AT-12/13/14 无法验证）"
               log "        需要人在手机边上扫设备码：bdpan login --device-code"
               blocked=1 ;;
        esac
    else
        log "[BLOCK] 找不到 bdpan"
        blocked=1
    fi

    # 3) 服务与配置文件
    [ -f "$VAR/config/service.json" ] || { log "[BLOCK] 缺少 $VAR/config/service.json"; blocked=1; }
    if [ "$(systemctl --user is-active asistent 2>/dev/null)" = "active" ]; then
        log "[OK]   asistent 服务当前 active"
    else
        log "[WARN] asistent 服务当前不是 active（start 会把它拉起来）"
    fi

    # 4) 取样定时器：没有它就等于 24h 之后没有曲线
    if [ "$(systemctl --user is-active asistent-soak.timer 2>/dev/null)" = "active" ]; then
        log "[OK]   取样定时器 active"
    else
        log "[BLOCK] 取样定时器未运行：24h 之后将没有稳定性曲线可判"
        log "        修复：scripts/run-service.sh soak-unit --stage 然后 systemctl --user enable --now asistent-soak.timer"
        blocked=1
    fi

    if [ "$blocked" = "1" ]; then
        log ""
        log "结论：**现在不能起跑最终验收窗口**（上面 BLOCK 项未清）。"
        log "可以显式跳过：--allow-blocked（那会在锚点里记下'带缺口起跑'，结论也必须照实写）"
        [ "${1:-}" = "--allow-blocked" ] || return 1
        log "已按 --allow-blocked 继续；缺口会写进锚点。"
    fi
    return 0
}

# ------------------------------------------------------------------ 起跑
cmd_start() {
    preflight "${1:-}" || exit 1

    if [ -f "$ANCHOR" ]; then
        log ""
        log "已存在锚点：$(cat "$ANCHOR")"
        log "（重新 start 会覆盖锚点并再重启一次服务；旧窗口的取样点仍留在 soak.jsonl 里）"
        [ "${1:-}" = "--force" ] || [ "${2:-}" = "--force" ] || die "需要 --force 才会覆盖已有锚点"
    fi

    log ""
    log "===== 重启服务，让最终窗口从干净的进程开始 ====="
    systemctl --user restart asistent || die "重启失败"
    # 等 service-status.json 里出现新的 run_id（最多 60 秒）
    for _ in $(seq 1 60); do
        sleep 1
        [ "$(systemctl --user is-active asistent)" = "active" ] && break
    done
    sleep 2

    log "===== 记录锚点（起点、run_id、代码提交） ====="
    "$PY" - "$VAR" "$REPO_ROOT" "$ANCHOR" <<'PY'
import json, os, subprocess, sys
from datetime import datetime, timezone

var, repo, anchor = sys.argv[1], sys.argv[2], sys.argv[3]
status_path = os.path.join(var, "state", "service-status.json")
status = {}
try:
    with open(status_path, encoding="utf-8") as handle:
        status = json.load(handle)
except (OSError, ValueError) as exc:
    print("警告：读不到服务状态（%s）：%s" % (status_path, exc))

def git(*args):
    try:
        out = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10)
        return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None

record = {
    "anchor_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    "service_started_at": status.get("started_at"),
    "run_id": status.get("run_id"),
    "rounds_completed": status.get("rounds_completed"),
    # 进程加载的是启动那一刻的代码；仓库 HEAD 之后可能还会变，所以两者都记
    "repo_head": git("rev-parse", "--short", "HEAD"),
    "repo_head_subject": git("log", "-1", "--format=%s"),
    "contract_version": None,
    "criteria_version": "CP-20261010-37",
}
try:
    version_py = open(os.path.join(repo, "asistent", "contracts", "version.py"), encoding="utf-8").read()
    for line in version_py.splitlines():
        if line.startswith("CONTRACT_VERSION"):
            record["contract_version"] = line.split('"')[1]
            break
except OSError:
    pass

os.makedirs(os.path.dirname(anchor), exist_ok=True)
with open(anchor, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
os.chmod(anchor, 0o600)
print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
PY

    log ""
    log "===== 立即取一个取样点（确认取样器认识这次 run） ====="
    bash "$REPO_ROOT/scripts/run-service.sh" soak --sample >/dev/null 2>&1 && log "已取样" || log "取样失败（检查 soak 定时器）"
    log ""
    log "最终窗口已起跑。24 小时之后运行：scripts/acceptance-round.sh verdict"
}

# ------------------------------------------------------------------ 状态
cmd_status() {
    [ -f "$ANCHOR" ] || die "还没有锚点：先运行 scripts/acceptance-round.sh start"
    "$PY" - "$ANCHOR" <<'PY'
import json, sys
from datetime import datetime, timezone

record = json.load(open(sys.argv[1], encoding="utf-8"))
anchor = datetime.fromisoformat(record["anchor_at"])
now = datetime.now(anchor.tzinfo)
elapsed = now - anchor
hours = elapsed.total_seconds() / 3600.0
print("锚点      : %s（%.1f 小时前）" % (record["anchor_at"], hours))
print("run_id    : %s" % record.get("run_id"))
print("进程起点  : %s" % record.get("service_started_at"))
print("代码提交  : %s  %s" % (record.get("repo_head"), (record.get("repo_head_subject") or "")[:48]))
print("契约版本  : %s / 判据 %s" % (record.get("contract_version"), record.get("criteria_version")))
print("")
if hours >= 24:
    print("状态：✅ 已满 24 小时，可以出结论")
else:
    print("状态：⏳ 还差 %.1f 小时（现在下结论会早 %.1f 小时）" % (24 - hours, 24 - hours))
PY
}

# ------------------------------------------------------------------ 判定
cmd_verdict() {
    [ -f "$ANCHOR" ] || die "还没有锚点：先运行 scripts/acceptance-round.sh start"
    HOURS=$("$PY" - "$ANCHOR" <<'PY'
import json, sys
from datetime import datetime
record = json.load(open(sys.argv[1], encoding="utf-8"))
anchor = datetime.fromisoformat(record["anchor_at"])
print("%.2f" % ((datetime.now(anchor.tzinfo) - anchor).total_seconds() / 3600.0))
PY
)
    log "===== 锚点 ====="
    cmd_status
    log ""
    if "$PY" -c "import sys; sys.exit(0 if float('$HOURS') >= 24.0 else 1)"; then
        log "===== 已满 24 小时：按 CP-37 预先冻结的判据判定 ====="
        bash "$REPO_ROOT/scripts/run-service.sh" soak --verdict
    else
        log "===== 拒绝出结论：只跑了 $HOURS 小时 ====="
        log "判据要求满 24 小时。现在给结论就是把「还没跑够」说成「跑够了」——"
        log "这正是本项目反复防的那类错误。先看趋势可以（soak --summary），但不要当成 AT-01 的结论："
        bash "$REPO_ROOT/scripts/run-service.sh" soak --summary
    fi
}

case "${1:-}" in
    start) shift; cmd_start "$@" ;;
    status) cmd_status ;;
    verdict) cmd_verdict ;;
    *) printf '用法：%s {start|status|verdict}\n' "$0" >&2; exit 2 ;;
esac
