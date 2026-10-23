#!/bin/sh
# A-5「构建即发布」发布入口（POSIX sh + python3 标准库，无第三方依赖）。
#
# 一步完成：快照 → manifest → 上传 → 逐文件哈希回读校验 → 记录归档状态。
# `--dry-run` 时打包与 manifest 照常生成，归档回路改走本地目录（不接触真实网盘，
# 不要求已授权），因此**离线也能验证**"打包 + manifest + 回读校验"是否成立。
#
# 用法：
#   scripts/release.sh --dry-run                       # 离线演练（推荐先跑这个）
#   scripts/release.sh                                 # 真实发布（需要 --approval-ref）
#   scripts/release.sh --approval-ref APP-123 --json   # 真实发布并输出 JSON 结果
#
# 退出码（由 python 侧给出，脚本原样透传）：
#   0 = 校验通过（VERIFIED）
#   2 = 部分失败/待重试（PARTIAL/PENDING/UNKNOWN，可安全重跑）
#   3 = 未授权 / 未获审批（NOT_AUTHORIZED / APPROVAL_REQUIRED）
#   4 = 配置或完整性错误（CONFIG_ERROR / INTEGRITY_ERROR）
#
# 凭据纪律：本脚本从不读取 bdpan 配置文件，也不接受任何 token/cookie 参数；
# `--approval-ref` 只是审批记录 ID。
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON=${PYTHON:-python3}

REPO=$REPO_ROOT
OUT=${RELEASE_OUT:-"$REPO_ROOT/dist/release"}
LABEL=$(date -u +%Y%m%dT%H%M%SZ)
PASSTHROUGH=""

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

脚本自有参数：
  --repo <path>       仓库根目录（默认：本脚本所在仓库）
  --out <path>        发布产物输出目录（默认：$RELEASE_OUT 或 <repo>/dist/release）
  --label <name>      发布标签（默认：UTC 时间戳）
  --python <path>     指定 python3（默认：$PYTHON 环境变量或 python3）
  -h | --help         显示本帮助

其余参数（--dry-run / --approval-ref / --state-dir / --mirror / --json）原样透传给
`python3 -m asistent.archive.release`。
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --repo) REPO=$2; shift 2 ;;
        --out) OUT=$2; shift 2 ;;
        --label) LABEL=$2; shift 2 ;;
        --python) PYTHON=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) PASSTHROUGH="$PASSTHROUGH $1"; shift ;;
    esac
done

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "release: 找不到 python3（可用 --python 或 PYTHON=... 指定）" >&2
    exit 4
fi

echo "release: repo=$REPO out=$OUT label=$LABEL python=$("$PYTHON" -c 'import sys;print(sys.version.split()[0])')"
echo "release: target=$PYTHON -c 'from asistent.archive.release import main; ...'$PASSTHROUGH"

cd "$REPO"
# 让发布模块能在任意工作目录下被导入（源码树布局：<repo>/asistent）
PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
# 用 `-c` 而不是 `-m`：`-m` 会因包 __init__ 已导入该模块而打 RuntimeWarning（runpy 已知行为），
# 用显式导入既避免噪音，也保证退出码原样透传。
# shellcheck disable=SC2086  # PASSTHROUGH 需要按空格切分（参数由调用方控制）
exec "$PYTHON" -c 'import sys; from asistent.archive.release import main; sys.exit(main())' \
    --repo "$REPO" --out "$OUT" --label "$LABEL" $PASSTHROUGH
