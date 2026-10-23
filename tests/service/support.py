"""MVP-8 服务测试夹具（`tests/service/**` 专用，不进生产代码）。

提供三样东西：

1. `ServiceWorkbench` —— 一个**仓库外**的工作区（`var/` 是 `ASISTENT_VAR`，
   `mail/` 是离线夹具邮件目录，`docs/` 是挂载资料目录），以及若干份
   `service*.json`（间隔、通道、库路径全部来自配置，符合"配置在仓库外"）；
2. `run_service()` —— 用**新进程**跑 `python3 -m asistent.service`，
   这是"状态只来自存储"的证据来源：每轮一个干净进程，进程内存带不走任何东西；
3. 断言小工具（`submit_lines()`、`assert_no_secret_in()`、`audit_trail()` 等）。

存储后端沿用契约测试的开关：`ASISTENT_STORE=reference|sqlite`
（默认 `reference` = 内存参考实现）。**真重启**（换进程、被 kill）只有落盘库才能证明，
所以那部分测试在 reference 后端上会跳过——这与 `tests/contracts/test_resilience.py`
的既有口径一致，不另立规矩。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE_ROOT = os.path.join(REPO_ROOT, "fixtures", "public")

#: 控制台/服务用到的令牌内容（**测试专用**的假令牌，不是任何真实凭据）
FAKE_TOKEN = "test-token-not-a-secret-0123456789"

#: 会被判定为"凭据泄漏"的关键词（任何日志/快照/argv 里出现即失败）。
#:
#: 口径是**值形态**而不是裸词：`password=` / `authcode=` / `access_token` / `authorization:`
#: 这些出现就说明"值真的被打出来了"。原来这里还有裸词 `authcode`，接真实邮件通道后
#: 它与两样**必须能说出口**的东西撞名了：
#:   - 凭据文件的名字（`~/asistent-var/config/mail-authcode.txt`）——运维要知道去哪填；
#:   - 用户动作脚本的名字（`set-mail-authcode.sh`）——"下一步怎么办"要指得出来。
#: 这两者都是路径/动作说明，不是凭据材料；把它们一起禁掉会让"缺授权码"这件事说不清楚，
#: 于是诊断信息被迫含糊化——那比"说了文件在哪"更糟。真正的值泄漏另有**更强的**断言兜底：
#: `tests/service/test_service_mcp_mail.py` 会拿真的假授权码去搜日志/快照/配置描述。
SECRET_MARKERS: Tuple[str, ...] = (
    "authcode=",
    "authorization:",
    "passwd",
    "password=",
    "access_token",
)


def store_is_sqlite() -> bool:
    return os.environ.get("ASISTENT_STORE", "reference").strip().lower() == "sqlite"


def python_executable() -> str:
    return sys.executable or "python3"


def fixture_path(*parts: str) -> str:
    return os.path.join(FIXTURE_ROOT, *parts)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write_text(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def notice_eml(
    *,
    subject: str,
    body: str,
    from_addr: str = "kids@example.org",
    to_addr: str = "parent@example.com",
    from_name: str = "市图书馆少儿部",
    message_id: str = "mvp8-001@example.org",
    date: str = "Thu, 8 Oct 2026 16:45:00 +0800",
) -> str:
    """构造一封真实结构的 `.eml`（LF 换行；`decode_eml_bytes` 两种都吃）。"""
    return (
        'From: "%s" <%s>\n' % (from_name, from_addr)
        + 'To: "家长联系人" <%s>\n' % to_addr
        + "Subject: %s\n" % subject
        + "Date: %s\n" % date
        + "Message-ID: <%s>\n" % message_id
        + "MIME-Version: 1.0\n"
        + 'Content-Type: text/plain; charset="utf-8"\n'
        + "Content-Transfer-Encoding: 8bit\n"
        + "\n"
        + body.lstrip("\n")
    )


#: 一封"干净"的活动通知：有两个明确时间 + 明确回复要求，**没有**缺材料标记
CLEAN_NOTICE_BODY = """各位家长：

本周末科普讲座安排如下。

报名截止时间：10月16日 18:00。
活动开始时间：10月17日 14:00。
活动地点：市图书馆三层报告厅。

请于报名截止前回复本邮件确认参加人数。

市图书馆少儿部
2026年10月8日
"""


class ProcResult:
    """一次子进程调用的原始结果（文档里的"原始输出"就是这些字）。"""

    def __init__(self, argv: Sequence[str], returncode: int, stdout: str, stderr: str) -> None:
        self.argv = list(argv)
        self.returncode = int(returncode)
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def raw(self) -> str:
        return "$ %s\n[exit %d]\n%s%s" % (
            " ".join(self.argv),
            self.returncode,
            self.stdout,
            ("\n[stderr]\n" + self.stderr) if self.stderr.strip() else "",
        )

    def json(self) -> Any:
        text = self.stdout.strip()
        start = text.find("[") if text.startswith("$") else 0
        return json.loads(text[start:])


class ServiceWorkbench:
    """一个仓库外的工作区 + 若干配置 + 新进程启动能力。"""

    def __init__(self, root: str, *, backend: Optional[str] = None) -> None:
        self.root = os.path.abspath(root)
        self.backend = (backend or os.environ.get("ASISTENT_STORE", "reference")).strip().lower()
        self.var_root = os.path.join(self.root, "var")
        self.mail_dir = os.path.join(self.root, "mail")
        self.docs_dir = os.path.join(self.root, "docs")
        self.config_dir = os.path.join(self.var_root, "config")
        self.db_path = os.path.join(self.var_root, "data", "asistent.db")
        self.state_dir = os.path.join(self.var_root, "state")
        self.index_path = os.path.join(self.var_root, "index", "fts.db")
        self.archive_root = os.path.join(self.var_root, "archive", "local-pan")
        self.staging_root = os.path.join(self.var_root, "archive", "staging")
        self.token_path = os.path.join(self.config_dir, "console-token")
        for directory in (
            self.config_dir,
            os.path.dirname(self.db_path),
            self.state_dir,
            os.path.dirname(self.index_path),
            self.mail_dir,
            self.docs_dir,
            self.staging_root,
        ):
            os.makedirs(directory, exist_ok=True)
        os.chmod(self.var_root, 0o700)
        # 令牌**所在目录**也必须 700：控制台的权限校验同时看目录与文件
        # （`asistent/console/auth.py::load_token` 的 `_ALLOWED_DIR_MODES`）。
        # 部署侧的真实目录是 700（`set-mail-authcode.sh` 里显式 chmod 700，`~/asistent-var` 本身也是 700），
        # 所以测试必须照 700 建——否则在 Linux 上令牌会被合法地拒绝，
        # 而 Windows 不做这项校验，于是“测试在本机全绿、到设备上 9 项红”（2026-10-12 实测）。
        os.chmod(self.config_dir, 0o700)
        write_text(self.token_path, FAKE_TOKEN + "\n")
        os.chmod(self.token_path, 0o600)
        self._runtimes: List[Any] = []

    # ------------------------------------------------------------------ 文件
    def add_notice(self, name: str, *, subject: str, body: str = CLEAN_NOTICE_BODY, **kwargs: Any) -> str:
        """往离线邮件夹具目录里放一封新邮件（下一轮同步就会发现它）。"""
        path = os.path.join(self.mail_dir, name)
        write_text(path, notice_eml(subject=subject, body=body, **kwargs))
        return path

    def write_doc(self, relative: str, text: str) -> str:
        return write_text(os.path.join(self.docs_dir, relative), text)

    # ------------------------------------------------------------------ 配置
    def config_payload(self, **overrides: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "var_root": self.var_root,
            "store": self.backend,
            "db_path": self.db_path,
            "state_dir": self.state_dir,
            "interval_seconds": 1,
            "max_rounds": 1,
            "batch_limit": 20,
            "console": {"enabled": True, "bind": "127.0.0.1", "port": 0, "token_path": self.token_path},
            "mail": {
                "enabled": True,
                "source": "fixture",
                "account": "parent@example.com",
                "fixture_dir": self.mail_dir,
                "state_path": os.path.join(self.state_dir, "mail-sync.json"),
                "raw_root": os.path.join(self.var_root, "raw"),
            },
            "ingest": {"enabled": False, "roots": [], "index_path": self.index_path},
            "outbound": {"enabled": True, "channel": "none", "from_account": "parent@example.com", "node": "service-1"},
            "archive": {"enabled": False, "channel": "none", "root": self.archive_root, "staging_root": self.staging_root},
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(payload.get(key), dict):
                merged = dict(payload[key])
                merged.update(value)
                payload[key] = merged
            else:
                payload[key] = value
        return payload

    def write_config(self, name: str = "service.json", **overrides: Any) -> str:
        path = os.path.join(self.config_dir, name)
        payload = self.config_payload(**overrides)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        return path

    def default_config(self) -> str:
        """默认：链接通但不外发、不归档（`channel=none`）。"""
        return self.write_config("service.json")

    def sending_config(self) -> str:
        """受控假通道（离线剧本通道），每次 `send()` 落一行账本。"""
        return self.write_config("service-send.json", outbound={"channel": "scripted"})

    def archiving_config(self) -> str:
        return self.write_config(
            "service-archive.json",
            archive={"enabled": True, "channel": "local_dir"},
        )

    def holding_config(self) -> str:
        """通道配好但**关着**：动作会被派生并授权，就是不发（测"批准了但还没执行"的中断点）。"""
        return self.write_config("service-hold.json", outbound={"enabled": False, "channel": "scripted"})

    # ------------------------------------------------------------------ 进程
    def env(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = dict(os.environ)
        env["ASISTENT_VAR"] = self.var_root
        env["ASISTENT_STORE"] = self.backend
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = REPO_ROOT
        if extra:
            env.update(extra)
        return env

    def run_cli(self, *args: str, env_extra: Optional[Dict[str, str]] = None, timeout: int = 120) -> ProcResult:
        argv = [python_executable(), "-X", "utf8", "-m", "asistent.service"] + [str(item) for item in args]
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=self.env(env_extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return ProcResult(
            argv,
            completed.returncode,
            completed.stdout.decode("utf-8", "replace"),
            completed.stderr.decode("utf-8", "replace"),
        )

    def run_round(self, config_path: Optional[str] = None, *, expect_ok: bool = True) -> ProcResult:
        """跑**恰好一轮**（新进程）。默认要求该轮全部阶段成功。

        `--json` 是必须的：跨进程取证时"轮次报告"必须能从 stdout 里机器可读地取回来，
        而且它同时也是"这一轮到底做了什么"的原始证据。
        """
        result = self.run_cli(
            "--once",
            "--json",
            "--config",
            config_path or os.path.join(self.config_dir, "service.json"),
        )
        if expect_ok and result.returncode != 0:
            raise AssertionError("服务轮次未成功：\n%s" % result.raw())
        return result

    def run_script(self, script: str, *args: str, env_extra: Optional[Dict[str, str]] = None, timeout: int = 120) -> ProcResult:
        """跑仓库里的一个 python 脚本（崩溃注入子进程等）。"""
        argv = [python_executable(), "-X", "utf8", os.path.join(REPO_ROOT, script)] + [str(item) for item in args]
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=self.env(env_extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return ProcResult(
            argv,
            completed.returncode,
            completed.stdout.decode("utf-8", "replace"),
            completed.stderr.decode("utf-8", "replace"),
        )

    def status(self, config_path: Optional[str] = None) -> Dict[str, Any]:
        result = self.run_cli("--status", "--json", "--config", config_path or os.path.join(self.config_dir, "service.json"))
        if not result.ok:
            raise AssertionError("--status 失败：\n%s" % result.raw())
        return json.loads(result.stdout)

    # ------------------------------------------------------------------ 同进程运行时（reference 后端用）
    def runtime(self, config_path: Optional[str] = None, **kwargs: Any) -> Any:
        from asistent.service import ServiceRuntime
        from asistent.service.config import load_config

        config = load_config(config_path or os.path.join(self.config_dir, "service.json"))
        runtime = ServiceRuntime(config, **kwargs)
        self._runtimes.append(runtime)
        return runtime

    def close_runtimes(self) -> None:
        for runtime in self._runtimes:
            try:
                runtime.close()
            except Exception:  # noqa: BLE001
                pass
        self._runtimes = []

    # ------------------------------------------------------------------ 存储（测试侧只读断言）
    def open_store(self) -> Any:
        if self.backend == "sqlite":
            from asistent.storage.sqlite_store import open_store

            return open_store(self.db_path)
        raise unittest.SkipTest("reference 后端没有跨进程存储可查：请用 ASISTENT_STORE=sqlite 运行")

    def clear(self) -> None:
        self.close_runtimes()
        for name in os.listdir(self.var_root):
            if name == "config":
                continue
            target = os.path.join(self.var_root, name)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                os.remove(target)

    # ------------------------------------------------------------------ 常驻进程（"真的在跑"的取证）
    def spawn(self, *args: str, env_extra: Optional[Dict[str, str]] = None) -> "ServiceProcess":
        """起一个**真的在跑**的服务进程，逐行收集它的输出（带超时读取）。

        和 `run_cli` 的区别：这里不等待结束，用来证明"没人连它也在轮转"，
        以及"崩溃后被重启不会重发"这类**跨进程**事实。
        """
        argv = [python_executable(), "-X", "utf8", "-m", "asistent.service"] + [str(item) for item in args]
        process = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            env=self.env(env_extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        return ServiceProcess(argv, process)


class ServiceProcess:
    """常驻服务子进程 + 行读取线程（stdout/stderr 合并）。"""

    def __init__(self, argv: Sequence[str], process: Any) -> None:
        self.argv = list(argv)
        self.process = process
        self.lines: List[str] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        stream = self.process.stdout
        if stream is None:  # pragma: no cover
            return
        for line in stream:
            with self._lock:
                self.lines.append(line.rstrip("\n"))

    # ---- 观察
    def output(self) -> str:
        with self._lock:
            return "\n".join(self.lines)

    def tail(self, count: int = 20) -> str:
        with self._lock:
            return "\n".join(self.lines[-count:])

    def wait_for(self, pattern: str, *, timeout: float = 30.0, count: int = 1) -> str:
        """等某条正则出现（默认 1 次）；超时把已收集的输出一起抛出来便于定位。"""
        deadline = time.time() + float(timeout)
        regex = re.compile(pattern)
        while time.time() < deadline:
            hits = [line for line in self.output().splitlines() if regex.search(line)]
            if len(hits) >= count:
                return hits[count - 1]
            if self.process.poll() is not None and len(hits) < count:
                time.sleep(0.2)
                hits = [line for line in self.output().splitlines() if regex.search(line)]
                if len(hits) >= count:
                    return hits[count - 1]
                raise AssertionError(
                    "进程已退出（code=%s）但没有等到 /%s/：\n%s" % (self.process.returncode, pattern, self.output())
                )
            time.sleep(0.1)
        raise AssertionError("等 /%s/ 超时（%.1fs）：\n%s" % (pattern, timeout, self.output()))

    def wait_for_rounds(self, count: int, *, timeout: float = 60.0) -> str:
        """等**轮次**出现 N 次。

        注意：逐阶段的证据行也以 `[round N]` 开头（8 行/轮），所以只认轮次摘要行
        `[round N] round=N ...`——否则"两轮"会因为 8 行证据行而被误判成已经跑过。
        """
        return self.wait_for(r"^\[round \d+\] round=\d+ ", timeout=timeout, count=count)

    def rounds_seen(self) -> int:
        """已经打印出来的轮次摘要行数（= 真正跑完的轮数）。"""
        return len([line for line in self.output().splitlines() if re.match(r"^\[round \d+\] round=\d+ ", line)])

    def console_port(self, *, timeout: float = 30.0) -> int:
        line = self.wait_for(r"控制台监听 http://", timeout=timeout)
        match = re.search(r"http://[^:]+:(\d+)", line)
        if not match:
            raise AssertionError("控制台行里没有端口：%s" % line)
        return int(match.group(1))

    def alive(self) -> bool:
        return self.process.poll() is None

    # ---- 收尾
    def stop(self, *, timeout: float = 30.0) -> int:
        """优雅停止：POSIX 上发 SIGTERM 等它自己退出；Windows 只能强杀（无法测优雅路径）。

        返回退出码；超时仍不退出则强杀，并在异常里带上输出。
        """
        if self.process.poll() is None:
            if os.name == "nt":
                self.process.terminate()
            else:
                self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
                raise AssertionError("SIGTERM 后 %ss 仍未退出：\n%s" % (timeout, self.tail(40)))
        return int(self.process.returncode or 0)

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass

    def close(self) -> None:
        self.kill()
        if self.process.stdout is not None:
            try:
                self.process.stdout.close()
            except Exception:  # noqa: BLE001
                pass


def find_phase(report_payload: Dict[str, Any], name: str) -> Dict[str, Any]:
    for item in report_payload.get("phases", []):
        if item.get("name") == name:
            return item
    return {}


class ServiceDriver:
    """分轮驱动一台服务。**同一份轮次实现，两种"分轮"方式**：

    * `sqlite` 后端：每轮起一个**新进程**（`python3 -m asistent.service --once`），
      进程内存带不走任何东西 —— 因此"下一轮还知道该干什么"只能来自存储；
    * `reference` 后端：同一个进程里多次调用 `ServiceRuntime.run_once()`，
      共用同一个内存存储（跨进程证明留给 sqlite，因为内存库无法跨进程）。

    两条路都会把"一轮的报告"与"健康快照"吐出来，断言口径完全一样。
    """

    def __init__(self, bench: ServiceWorkbench) -> None:
        self.bench = bench
        self.backend = bench.backend
        self._store: Any = None
        self._runtimes: Dict[str, Any] = {}
        self.raw_outputs: List[str] = []

    # ------------------------------------------------------------------ 驱动
    def round(self, config_name: str = "service.json", *, expect_ok: bool = True) -> Dict[str, Any]:
        path = os.path.join(self.bench.config_dir, config_name)
        if self.backend == "sqlite":
            result = self.bench.run_round(path, expect_ok=expect_ok)
            self.raw_outputs.append(result.raw())
            return _report_from_stdout(result.stdout)
        runtime = self._runtime_for(path)
        report = runtime.run_once()
        payload = report.to_dict()
        self.raw_outputs.append("[in-process] %s" % report.summary_line())
        if expect_ok and not report.ok:
            raise AssertionError("轮次有阶段失败：%s" % report.summary_line())
        return payload

    def status(self, config_name: str = "service.json") -> Dict[str, Any]:
        path = os.path.join(self.bench.config_dir, config_name)
        if self.backend == "sqlite":
            return self.bench.status(path)
        return self._runtime_for(path).status()

    def store(self) -> Any:
        """测试侧读断言用的存储（sqlite 后端每次新开一个只读用途的连接）。"""
        if self.backend == "sqlite":
            return self.bench.open_store()
        if self._store is None:
            from asistent.contracts.reference import ReferenceStore

            self._store = ReferenceStore()
        return self._store

    def close(self) -> None:
        for runtime in self._runtimes.values():
            try:
                runtime.close()
            except Exception:  # noqa: BLE001
                pass
        self._runtimes = {}
        self.bench.close_runtimes()

    # ------------------------------------------------------------------ 内部
    def _runtime_for(self, config_path: str) -> Any:
        if config_path in self._runtimes:
            return self._runtimes[config_path]
        runtime = self.bench.runtime(config_path, store=self.store())
        self._runtimes[config_path] = runtime
        return runtime


def parse_round_report(stdout: str) -> Dict[str, Any]:
    """公开别名：从 `--once --json` 的 stdout 里取回该轮报告。"""
    return _report_from_stdout(stdout)


def _report_from_stdout(stdout: str) -> Dict[str, Any]:
    """从 `--once --json` 的输出里取回该轮报告（stdout 上还有人类可读的日志行）。

    `--json` 用 `indent=2` 打整个数组，所以"顶层数组"的标志是**独占一行的 `[`**；
    在它之前出现的都是日志。取第一个这样的行，剩下的整体 `json.loads`。
    """
    lines = stdout.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "[":
            start = index
            break
    if start is None:
        raise AssertionError("--json 输出里没有 JSON 数组（独占一行的 `[`）：\n%s" % stdout)
    payload = json.loads("\n".join(lines[start:]))
    if not payload:
        raise AssertionError("--json 输出是空数组：\n%s" % stdout)
    return payload[-1]


def audit_rows(store: Any, *, case_id: str = "") -> List[Dict[str, Any]]:
    rows = store.audit_trail(case_id=case_id or None, limit=None)
    return [
        {
            "action": str(item.action),
            "reason": str(item.reason),
            "detail": dict(item.detail or {}),
            "object_id": str(item.object_id or ""),
            "at": str(item.at),
        }
        for item in rows
    ]


def rule_hit_versions(store: Any, *, case_id: str = "") -> List[Tuple[str, int]]:
    """从审计里读出该 Case 每一次规则命中的 `(rule_id, rule_version)`。"""
    found: List[Tuple[str, int]] = []
    for row in audit_rows(store, case_id=case_id):
        if row["action"] != "RULE_HIT":
            continue
        detail = row["detail"]
        found.append((str(detail.get("rule_id", "")), int(detail.get("rule_version", 0) or 0)))
    return found


def submit_lines(path: str) -> List[Dict[str, Any]]:
    if not os.path.isfile(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return [item for item in rows if item.get("event") == "submit"]


__all__ = [
    "CLEAN_NOTICE_BODY",
    "FAKE_TOKEN",
    "FIXTURE_ROOT",
    "ProcResult",
    "REPO_ROOT",
    "SECRET_MARKERS",
    "ServiceDriver",
    "ServiceProcess",
    "ServiceWorkbench",
    "audit_rows",
    "find_phase",
    "fixture_path",
    "notice_eml",
    "parse_round_report",
    "python_executable",
    "read_text",
    "rule_hit_versions",
    "store_is_sqlite",
    "submit_lines",
    "write_text",
]
