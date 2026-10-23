"""长时间浸泡（soak）取样器：把"连续 24h 不掉轮"这句结论变成**可以看的曲线**。

为什么需要它
============

`docs/mvp/mvp-8.md` §7.5 一直写着"长时稳定性未证：只跑了分钟级多轮"。这不是懒，
而是**没有取样就没有证据**：24 小时后回头说"它一直好好的"，凭的是印象，不是数据。
内存泄漏、句柄泄漏、日志暴涨、轮次失败率上升，这四件事都只在几小时尺度上才显形，
而且都不会让进程直接死掉——所以"服务还活着"完全不足以证明稳定。

取样什么（每项都对应一种真实的慢失败）
======================================

| 指标 | 慢失败时它会怎么动 |
|---|---|
| `rss_kb` | 内存泄漏：单调上升，永不回落 |
| `fds` | 句柄泄漏（socket/子进程管道没关）：缓慢爬升 |
| `threads` | 线程泄漏 |
| `wal_bytes` / `db_bytes` | WAL 不 checkpoint：写放大后突然变慢 |
| `log_bytes` / `mail_mcp_log_bytes` | 日志无轮转：磁盘被慢慢吃满 |
| `rounds_completed` / `rounds_failed` | 轮次失败率上升（最常见的"悄悄退化"） |
| `listening` | 控制台端口掉了（手机端连不上，但进程还活着） |
| `disk_free_bytes` | 磁盘耗尽 |

取样**只读**：不重启服务、不写服务数据目录下的业务文件，只往 `logs/soak.jsonl` 追一行。
JSONL 而不是 CSV：字段以后只会变多，JSONL 加字段不会让旧行失效。

用法（设备上由 `asistent-soak.timer` 每 5 分钟调一次）
=====================================================

    python3 -X utf8 -m asistent.service.soak --sample        # 追一行
    python3 -X utf8 -m asistent.service.soak --summary        # 打印人读的结论
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

#: 取样间隔（分钟）：5 分钟 × 24h = 288 个点，足够看出趋势又不至于把日志写爆。
DEFAULT_INTERVAL_MINUTES = 5

#: 服务单元名（`systemctl --user`）
SERVICE_UNIT = "asistent"

#: CP-20261010-37 **预先冻结**的浸泡判据。
#:
#: 为什么必须在跑之前写死：曲线拿到手之后再定标准，就会不自觉地挑一个"刚好能过"的阈值
#: ——那不是验收，是事后解释。所以这些数字**先于** 24h 窗口结束写进契约（v0.9），
#: 由 `--verdict` 机械判定，不靠眼睛看。
#:
#: 各项含义与理由：
#:   `rss_growth_max_pct`     内存只要持续上涨就是问题；允许 25% 的波动（GC/缓存预热）。
#:   `fd_growth_max`          句柄是"泄漏"最直接的证据；10 个浮动都算多。
#:   `rounds_failed_max`      轮次失败率：24h × 120s ≈ 720 轮，0 失败是常态，容忍 3 次网络抖动。
#:   `wal_max_bytes`          WAL 不 checkpoint 会越跑越慢。512 MB 是"明显失控"的量级。
#:   `log_max_bytes`          日志无轮转会把磁盘吃满：24h 允许 64 MB（当前 66 KB/小时量级）。
#:   `disk_free_min_bytes`    低于 2 GB 就不再是"稳定"，而是"快满了"。
#:   `run_id_must_be_unique`  这一条是**连续性**的判据：`rounds_completed` 是跨进程累计值，
#:                            即使中途重启它也只会继续往上走；只有 `run_id` 变了才看得出来。
#:   `console_must_stay_up`   端口掉了 = 手机端连不上，而进程还"活着"。
SOAK_CRITERIA = {
    "rss_growth_max_pct": 25.0,
    "fd_growth_max": 10,
    "rounds_failed_max": 3,
    "wal_max_bytes": 512 * 1024 * 1024,
    "log_max_bytes": 64 * 1024 * 1024,
    "disk_free_min_bytes": 2 * 1024 * 1024 * 1024,
    "run_id_must_be_unique": True,
    "console_must_stay_up": True,
}


def var_root() -> str:
    return os.environ.get("ASISTENT_VAR") or os.path.expanduser(os.path.join("~", "asistent-var"))


def soak_log_path(root: Optional[str] = None) -> str:
    return os.path.join(root or var_root(), "logs", "soak.jsonl")


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _size(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _service_pids(pattern: str = "asistent.service --loop") -> List[int]:
    """扫 /proc 找服务进程：不依赖 pgrep，且 systemd 是否重开都看得见。

    开发机上没有 `/proc`（Windows），所以**扫不到就返回空**，而不是抛异常：
    取样器的职责是"如实记录"，不是"必须在每个平台上都能拿到进程指标"。
    拿不到时 `rss_kb`/`fds`/`threads` 留 `None`（= 没测到），
    这样"在开发机上跑一下"不会因为环境不同而失败，也不会伪装成"指标是 0"。
    """
    pids: List[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as handle:
                cmdline = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        if pattern in cmdline.replace("\x00", " "):
            pids.append(int(entry))
    return sorted(pids)


def _proc_metrics(pid: int) -> Dict[str, Optional[int]]:
    metrics: Dict[str, Optional[int]] = {"rss_kb": None, "threads": None, "fds": None}
    try:
        with open("/proc/%d/status" % pid, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    metrics["rss_kb"] = int(line.split()[1])
                elif line.startswith("Threads:"):
                    metrics["threads"] = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        metrics["fds"] = len(os.listdir("/proc/%d/fd" % pid))
    except OSError:
        pass
    return metrics


def _systemctl(*args: str) -> str:
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    try:
        proc = subprocess.run(
            [systemctl, "--user", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.decode("utf-8", "replace").strip()


def _repo_head(root: str) -> Optional[str]:
    """设备仓库当前 HEAD（只读）。拿不到就 `None`（**不猜**）。"""
    git = shutil.which("git")
    if not git:
        return None
    repo = os.path.join(os.path.expanduser("~"), "code", "asistent")
    if not os.path.isdir(os.path.join(repo, ".git")):
        return None
    try:
        proc = subprocess.run(
            [git, "-C", repo, "rev-parse", "--short", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


def _status_snapshot(root: str) -> Dict[str, Any]:
    path = os.path.join(root, "state", "service-status.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _listening_on(port: int) -> Optional[int]:
    """统计监听该端口的 socket 数；`ss` 不在就返回 None（**不猜**）。"""
    ss = shutil.which("ss")
    if not ss:
        return None
    try:
        proc = subprocess.run(
            [ss, "-ltn"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = proc.stdout.decode("utf-8", "replace")
    needle = ":%d" % port
    return sum(1 for line in text.splitlines() if needle in line)


def _storage_bytes(root: str) -> Dict[str, Any]:
    """统计落盘数据库与 WAL 的总字节数：**发现**而不是硬编码文件名。

    为什么"发现"：这里原来写死 `data/asistent.sqlite3`，而真机上的库其实叫
    `data/asistent.db`（另外还有 `index/fts.db`）。后果不是崩溃，而是**静默变成 null**——
    整整一段浸泡期里"WAL 有没有涨"根本没人回答，而报告上看起来只是"无数据"。
    这类"猜路径"的 bug 不会自己暴露，只会让结论少一块。

    返回 `{"db_bytes":…, "wal_bytes":…, "db_files":[…]}`：
    `db_bytes` 只算主库文件（不含 `-wal`/`-shm`），`wal_bytes` 只算 `-wal`。
    """
    db_total = 0
    wal_total = 0
    files: List[str] = []
    for sub in ("data", "index", "db"):
        directory = os.path.join(root, sub)
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in sorted(names):
            if not name.endswith((".db", ".sqlite3", ".db-wal", ".sqlite3-wal", ".db-shm", ".sqlite3-shm")):
                continue
            path = os.path.join(directory, name)
            size = _size(path)
            if size is None:
                continue
            if name.endswith(("-wal",)):
                wal_total += size
            elif name.endswith(("-shm",)):
                continue  # 共享内存文件是与内存同量级的临时映射，不参与"是否泄漏"的判断
            else:
                db_total += size
            files.append("%s/%s" % (sub, name))
    return {"db_bytes": db_total, "wal_bytes": wal_total, "db_files": files}


def sample(root: Optional[str] = None, *, console_port: int = 18090) -> Dict[str, Any]:
    """取一次样，返回这一行的字典（**纯函数式地可测**：不打印、不写文件）。"""
    root = root or var_root()
    row: Dict[str, Any] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "epoch": int(time.time()),
        "var_root": root,
    }
    row["service_active"] = _systemctl("is-active", SERVICE_UNIT) or "unknown"
    pids = _service_pids()
    row["service_pids"] = pids
    row["service_pid_count"] = len(pids)
    metrics: Dict[str, Optional[int]] = {"rss_kb": None, "threads": None, "fds": None}
    for pid in pids:
        candidate = _proc_metrics(pid)
        # 取 RSS 最大的那个（子进程/临时进程不该顶掉真正的服务进程）
        if candidate.get("rss_kb") and (metrics.get("rss_kb") or 0) < candidate["rss_kb"]:
            metrics = candidate
    row.update(metrics)

    status = _status_snapshot(root)
    row["rounds_completed"] = status.get("rounds_completed")
    row["rounds_failed"] = status.get("rounds_failed")
    row["waiting_on"] = status.get("waiting_on") or []
    row["last_round_at"] = status.get("last_round_at")
    # ---- CP-20261010-37：这几项是"连续性"的证据，少了它们就分不清"连续跑了 24h"和
    #      "中途重启过、但累计轮次继续上涨"。`rounds_completed` 是跨进程累计值：
    #      重启后它只会继续变大，所以**它无法证明连续**；`run_id` 变了才看得出来。
    row["run_id"] = status.get("run_id")
    row["service_started_at"] = status.get("started_at")
    # `round_index` 在 `last_round` 里面（不是顶层）：它才是**本进程**的轮次，
    # `rounds_completed` 是跨进程累计值。取不到就 None（不猜、也不用 0 冒充）。
    last_round = status.get("last_round")
    if isinstance(last_round, dict):
        row["round_index"] = last_round.get("round_index")
    else:
        row["round_index"] = status.get("round_index")
    # 部署在设备仓库里的提交（只是"仓库当时是什么"，不等于**进程加载的代码**——
    # 进程启动时已 import，之后的部署不会进到运行中的进程里。AT-01 的结论要同时写这两样。
    row["repo_head"] = _repo_head(root)

    storage = _storage_bytes(root)
    row["db_bytes"] = storage["db_bytes"]
    row["wal_bytes"] = storage["wal_bytes"]
    row["db_files"] = storage["db_files"]
    row["log_bytes"] = _size(os.path.join(root, "logs", "service.log"))
    row["mail_mcp_log_bytes"] = _size(os.path.join(root, "logs", "mail-mcp.log"))
    row["listening_console"] = _listening_on(console_port)
    try:
        usage = shutil.disk_usage(root)
        row["disk_free_bytes"] = usage.free
        row["disk_total_bytes"] = usage.total
    except OSError:
        row["disk_free_bytes"] = None
        row["disk_total_bytes"] = None
    return row


def append_sample(root: Optional[str] = None, *, console_port: int = 18090) -> str:
    """取一次样并**追加**到 `logs/soak.jsonl`（0600）。返回写入的路径。"""
    path = soak_log_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = sample(root, console_port=console_port)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    # 先创建成 0600 再写：不留"先 644 后改权限"的窗口
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:
        raise
    return path


def _load_rows(root: Optional[str] = None) -> List[Dict[str, Any]]:
    path = soak_log_path(root)
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue  # 半个写入的行不该让整份统计失效
    except OSError:
        return []
    return rows


def _trend(values: List[Optional[int]]) -> Dict[str, Any]:
    clean = [value for value in values if isinstance(value, int)]
    if len(clean) < 2:
        return {"first": clean[0] if clean else None, "last": clean[-1] if clean else None, "delta": None}
    return {"first": clean[0], "last": clean[-1], "delta": clean[-1] - clean[0]}


def summarize(root: Optional[str] = None) -> str:
    """把取样序列变成人读的结论。**没有数据就说没有数据**，不编。"""
    rows = _load_rows(root)
    path = soak_log_path(root)
    if not rows:
        return "还没有取样数据：%s（先跑 --sample，或看 timer 是否在跑）" % path
    first, last = rows[0], rows[-1]
    span = (last.get("epoch") or 0) - (first.get("epoch") or 0)
    hours = span / 3600.0
    lines = [
        "取样文件 : %s（%d 个点，跨度 %.1f 小时）" % (path, len(rows), hours),
        "取样区间 : %s → %s" % (first.get("at"), last.get("at")),
        "服务状态 : %s" % last.get("service_active"),
        "进程数   : %s → %s（重启过就会 >1 或跳变）"
        % (first.get("service_pid_count"), last.get("service_pid_count")),
    ]
    for key, label in (
        ("rss_kb", "RSS"),
        ("fds", "打开句柄"),
        ("threads", "线程"),
        ("wal_bytes", "WAL"),
        ("db_bytes", "数据库"),
        ("log_bytes", "服务日志"),
        ("mail_mcp_log_bytes", "邮件子进程日志"),
        ("disk_free_bytes", "磁盘可用"),
    ):
        trend = _trend([row.get(key) for row in rows])
        if trend["last"] is None:
            lines.append("%-14s : 无数据" % label)
            continue
        delta = trend["delta"]
        if delta is None:
            # 只有 1 个点：**说"看不出趋势"，不说"持平"**。
            # 把"没数据"显示成"没变化"，正是浸泡取证最不该犯的错。
            arrow = "只有 1 个点，看不出趋势"
        elif delta == 0:
            arrow = "持平"
        elif delta > 0:
            arrow = "+%d" % delta
        else:
            arrow = "%d" % delta
        lines.append(
            "%-14s : %s → %s（%s）" % (label, trend["first"], trend["last"], arrow)
        )
    rounds_first, rounds_last = first.get("rounds_completed"), last.get("rounds_completed")
    if isinstance(rounds_first, int) and isinstance(rounds_last, int):
        lines.append(
            "轮次       : %d → %d（+%d 轮；共失败 %s 次）"
            % (rounds_first, rounds_last, rounds_last - rounds_first, last.get("rounds_failed"))
        )
    missing = [row.get("at") for row in rows if row.get("service_active") not in ("active", "unknown")]
    if missing:
        lines.append("⚠ 有 %d 个取样点服务不是 active：%s" % (len(missing), missing[:5]))
    listens = [row.get("listening_console") for row in rows]
    if any(value == 0 for value in listens if value is not None):
        lines.append("⚠ 有取样点听不到控制台端口（手机端会连不上）")
    return "\n".join(lines)


def verdict(root: Optional[str] = None, *, criteria: Optional[Dict[str, Any]] = None) -> str:
    """按**预先冻结**的判据判定这轮浸泡过没过（CP-20261010-37）。

    为什么要有这个函数：如果明早拿到曲线再由人"看着定"结论，那就不叫验收。
    判据在 `SOAK_CRITERIA` 里写死、在这里机械执行，人只负责读结论。

    数据不足时**不判通过**（`结论：证据不足`），因为"点太少所以看不出增长"
    和"确实没有增长"是两件事。
    """
    rules = dict(SOAK_CRITERIA)
    rules.update(criteria or {})
    rows = _load_rows(root)
    lines: List[str] = []
    failures: List[str] = []
    unknowns: List[str] = []

    if len(rows) < 2:
        return "结论：证据不足（取样点少于 2 个，先让它跑）"

    # ---- 判定窗口 = **最近这一段连续运行**（tail of rows sharing the final run_id）。
    #
    # 为什么必须切窗口：用户已决定"先做 U-2/U-5（bdpan 授权 + 邮件授权码），再跑最终 24h"，
    # 所以服务会因为改配置而重启一次，`run_id` 必然变化。若把整份日志一起判，
    # 前缀那段"预跑"会让 `run_id` 唯一性判据失败——那不是最终那轮的问题，
    # 而是预热段造成的。切窗口 + 把预热段显式写出来，才既不误判也不隐瞒。
    tail_run = rows[-1].get("run_id")
    if tail_run:
        start = len(rows)
        for index in range(len(rows) - 1, -1, -1):
            if rows[index].get("run_id") != tail_run:
                break
            start = index
        prelude = rows[:start]
        if prelude:
            unknown_run = sum(1 for row in prelude if not row.get("run_id"))
            detail = "，其中 %d 个没有 run_id（无法判定属于哪一段）" % unknown_run if unknown_run else ""
            lines.append(
                "判定窗口 : 只判最近这段 run（%d 个点，run_id=%s）；之前另有 %d 个点不属于本段%s"
                % (len(rows) - start, str(tail_run)[:20] + "...", len(prelude), detail)
            )
        rows = rows[start:]

    span_hours = ((rows[-1].get("epoch") or 0) - (rows[0].get("epoch") or 0)) / 3600.0
    lines.append("取样跨度 : %.1f 小时（%d 个点）" % (span_hours, len(rows)))

    # ---- 连续性：run_id 必须唯一（`rounds_completed` 是跨进程累计值，证明不了连续）
    run_ids = [row.get("run_id") for row in rows if row.get("run_id")]
    distinct = sorted(set(run_ids))
    legacy = len(rows) - len(run_ids)
    if legacy:
        # 旧版取样器写的行没有 run_id。这些行仍能看内存/WAL 趋势，
        # 但**不能**用来证明"这段时间没重启过"——必须说出来，不能默默按没问题处理。
        # 分级处理：少数点没带 run_id 只是备注（否则明天一段干净的 24h 会被误判成"证据不足"）；
        # 过半数没带，就说明"连续未重启"确实没被证明，要计入证据不足。
        note = "有 %d/%d 个取样点没有 run_id（旧版取样器写的），这些点的「未重启」无法证明" % (
            legacy, len(rows),
        )
        if legacy * 2 > len(rows):
            unknowns.append(note)
        else:
            lines.append("注意     : %s" % note)
    if rules["run_id_must_be_unique"]:
        if len(distinct) <= 1 and run_ids:
            lines.append("连续性   : OK（整段只有 1 个 run_id: %s）" % distinct[0])
        elif not run_ids:
            unknowns.append("取样里没有 run_id：无法证明「连续未重启」（旧版取样器写的行）")
        else:
            failures.append(
                "整段出现 %d 个 run_id（中途重启过），不构成「连续运行」：%s"
                % (len(distinct), ", ".join(distinct[:5]))
            )

    # ---- 内存
    rss = [row.get("rss_kb") for row in rows if isinstance(row.get("rss_kb"), int)]
    if len(rss) >= 2 and rss[0]:
        growth = (rss[-1] - rss[0]) / float(rss[0]) * 100.0
        peak = max(rss)
        lines.append("RSS      : %d → %d KB（增长 %.1f%%，峰值 %d KB）" % (rss[0], rss[-1], growth, peak))
        if growth > rules["rss_growth_max_pct"]:
            failures.append("RSS 增长 %.1f%% 超过上限 %.1f%%" % (growth, rules["rss_growth_max_pct"]))
    else:
        unknowns.append("RSS 数据不足")

    # ---- 句柄
    fds = [row.get("fds") for row in rows if isinstance(row.get("fds"), int)]
    if len(fds) >= 2:
        delta = fds[-1] - fds[0]
        lines.append("句柄     : %d → %d（%+d）" % (fds[0], fds[-1], delta))
        if delta > rules["fd_growth_max"]:
            failures.append("句柄增长 %+d 超过上限 %d" % (delta, rules["fd_growth_max"]))
    else:
        unknowns.append("句柄数据不足")

    # ---- 轮次失败
    failed = [row.get("rounds_failed") for row in rows if isinstance(row.get("rounds_failed"), int)]
    completed = [row.get("rounds_completed") for row in rows if isinstance(row.get("rounds_completed"), int)]
    if failed and completed:
        lines.append("轮次     : 完成 %d → %d，失败 %d → %d"
                     % (completed[0], completed[-1], failed[0], failed[-1]))
        if failed[-1] - failed[0] > rules["rounds_failed_max"]:
            failures.append("轮次失败增加 %d 次，超过上限 %d"
                            % (failed[-1] - failed[0], rules["rounds_failed_max"]))
    else:
        unknowns.append("轮次数据不足")

    # ---- WAL / 日志 / 磁盘
    for key, limit_key, label in (
        ("wal_bytes", "wal_max_bytes", "WAL"),
        ("log_bytes", "log_max_bytes", "服务日志"),
    ):
        values = [row.get(key) for row in rows if isinstance(row.get(key), int)]
        if values:
            peak = max(values)
            lines.append("%-9s: 峰值 %d 字节（上限 %d）" % (label, peak, rules[limit_key]))
            if peak > rules[limit_key]:
                failures.append("%s 峰值 %d 超过上限 %d" % (label, peak, rules[limit_key]))
        else:
            unknowns.append("%s 数据不足" % label)

    free = [row.get("disk_free_bytes") for row in rows if isinstance(row.get("disk_free_bytes"), int)]
    if free:
        lines.append("磁盘可用 : 最低 %d 字节（下限 %d）" % (min(free), rules["disk_free_min_bytes"]))
        if min(free) < rules["disk_free_min_bytes"]:
            failures.append("磁盘可用最低 %d 低于下限 %d" % (min(free), rules["disk_free_min_bytes"]))
    else:
        unknowns.append("磁盘数据不足")

    # ---- 控制台
    if rules["console_must_stay_up"]:
        listens = [row.get("listening_console") for row in rows if row.get("listening_console") is not None]
        if listens:
            if all(value >= 1 for value in listens):
                lines.append("控制台   : OK（每个取样点都听得到 18090）")
            else:
                bad = sum(1 for value in listens if value < 1)
                failures.append("有 %d 个取样点听不到控制台端口" % bad)
        else:
            unknowns.append("控制台端口数据不足")

    lines.append("")
    if failures:
        lines.append("结论：❌ 未通过（%d 项）" % len(failures))
        lines.extend("  - " + item for item in failures)
    elif unknowns:
        lines.append("结论：⚠ 证据不足（%d 项）" % len(unknowns))
        lines.extend("  - " + item for item in unknowns)
    else:
        lines.append("结论：✅ 通过（按 CP-20261010-37 预先冻结的判据）")
    if span_hours < 23.5:
        lines.append("注意：跨度 %.1f 小时不足 24 小时——**现在还不能当作 AT-01 的 24h 结论**" % span_hours)
    return "\n".join(lines)


def _main(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(__doc__ or "")
        return 0
    command = argv[0]
    root = os.environ.get("ASISTENT_VAR") or var_root()
    if command == "--sample":
        path = append_sample(root)
        # systemd 日志里留一行心跳（不含任何凭据）
        sys.stdout.write("soak sample appended -> %s\n" % path)
        return 0
    if command == "--summary":
        sys.stdout.write(summarize(root) + "\n")
        return 0
    if command == "--verdict":
        sys.stdout.write(verdict(root) + "\n")
        return 0
    sys.stderr.write("未知参数：%s（用 --sample / --summary / --verdict）\n" % command)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(sys.argv[1:]))
