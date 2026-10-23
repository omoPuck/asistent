"""MVP-4 控制台测试的公共夹具。

约定：

* **真实 HTTP**：测试起一个真的 `ThreadingHTTPServer`（端口由系统分配），用
  `http.client` 发真请求，断言真响应 —— 不直接调 `ConsoleService` 的私有方法；
* **默认只用 `ReferenceStore`**（内存、确定、不写仓库数据）；设 `ASISTENT_STORE=sqlite`
  时切到 SQLite（用临时文件名库，不是 `:memory:` —— 因为"重启后数据还在"必须真的换连接验证）；
* **token 走真实文件**：临时目录 700 + 文件 600，用 `chmod` 真设权限，验证"权限不合格就拒绝启动"；
* 断言的是**可观察产物**（HTTP 状态、响应 JSON、store 里的对象），不是内部函数返回值。
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from asistent.console import ConsoleService, ConsoleSettings, TokenAuth, build_server  # noqa: E402
from asistent.console.auth import load_token  # noqa: E402

#: 测试用 token（够长、无特殊字符；不是真实凭据，仓库里出现也无害）
TEST_TOKEN = "test-token-0123456789abcdef0123456789abcdef"

_WINDOWS = os.name == "nt"


def store_factory(path: Optional[str] = None, **kwargs: Any):
    """按 `ASISTENT_STORE` 选择被测存储实现（默认 `reference`）。"""
    kind = os.environ.get("ASISTENT_STORE", "reference").strip().lower()
    if kind in ("", "reference", "memory", "mem"):
        from asistent.contracts import ReferenceStore

        return ReferenceStore(**kwargs)
    if kind == "sqlite":
        from asistent.storage.sqlite_store import SqliteStore  # type: ignore

        return SqliteStore(path=path, **kwargs)
    raise RuntimeError("未知存储实现: %s" % kind)


def store_is_sqlite() -> bool:
    return os.environ.get("ASISTENT_STORE", "reference").strip().lower() == "sqlite"


def make_token_file(directory: str, token: str = TEST_TOKEN, *, mode: int = 0o600) -> str:
    """在临时目录里造一个 token 文件，并按 POSIX 权限设成 600/700。"""
    config_dir = os.path.join(directory, "config")
    os.makedirs(config_dir, exist_ok=True)
    path = os.path.join(config_dir, "console-token")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    if not _WINDOWS:
        os.chmod(config_dir, 0o700)
        os.chmod(path, mode)
    return path


class RecordedRequest:
    """一次请求的原始记录：方法、路径、请求头、请求体、状态、响应头、响应体。"""

    def __init__(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        body: str,
        status: int,
        response_headers: Dict[str, str],
        response_body: str,
    ) -> None:
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body
        self.status = status
        self.response_headers = response_headers
        self.response_body = response_body
        try:
            self.json: Any = json.loads(response_body) if response_body else None
        except json.JSONDecodeError:
            self.json = None

    def render(self) -> str:
        """给文档用的"请求 + 响应"原始输出（不含 token 明文）。"""
        lines = ["%s %s HTTP/1.1" % (self.method, self.path)]
        for key, value in self.headers.items():
            shown = "<token>" if key.lower() == "authorization" else value
            lines.append("%s: %s" % (key, shown))
        if self.body:
            lines.append("")
            lines.append(self.body)
        lines.append("")
        lines.append("HTTP/1.1 %d" % self.status)
        for key, value in self.response_headers.items():
            lines.append("%s: %s" % (key, value))
        lines.append("")
        body = self.response_body
        if len(body) > 1200:
            body = body[:1200] + "…（截断）"
        lines.append(body)
        return "\n".join(lines)


class ConsoleClient:
    """极简 HTTP 客户端：记录每一次请求/响应，便于把原始输出贴进文档。"""

    def __init__(self, host: str, port: int, token: str = TEST_TOKEN) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.history: List[RecordedRequest] = []

    def call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        token: Any = ...,
        headers: Optional[Dict[str, str]] = None,
        raw_body: Optional[str] = None,
    ) -> RecordedRequest:
        sent: Dict[str, str] = {"Host": "%s:%d" % (self.host, self.port), "Accept": "application/json"}
        use_token = self.token if token is ... else token
        if use_token:
            sent["Authorization"] = "Bearer %s" % use_token
        if headers:
            sent.update(headers)
        payload = ""
        if raw_body is not None:
            payload = raw_body
        elif body is not None:
            payload = json.dumps(body, ensure_ascii=False)
        if payload:
            sent["Content-Type"] = "application/json; charset=utf-8"
            sent["Content-Length"] = str(len(payload.encode("utf-8")))
        connection = http.client.HTTPConnection(self.host, self.port, timeout=15)
        try:
            connection.request(method, path, body=payload.encode("utf-8") if payload else None, headers=sent)
            response = connection.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
            recorded = RecordedRequest(
                method,
                path,
                dict(sent),
                payload,
                response.status,
                {key: value for key, value in response.getheaders()},
                raw,
            )
        finally:
            connection.close()
        self.history.append(recorded)
        return recorded

    def get(self, path: str, **kwargs: Any) -> RecordedRequest:
        return self.call("GET", path, **kwargs)

    def post(self, path: str, body: Any = None, **kwargs: Any) -> RecordedRequest:
        return self.call("POST", path, body=body, **kwargs)

    def last(self) -> RecordedRequest:
        return self.history[-1]


class ConsoleTestCase(unittest.TestCase):
    """公共基类：临时目录 + store + 真 HTTP 服务 + 客户端。"""

    #: 需要 SQLite 文件库的子类可覆盖（默认按 `ASISTENT_STORE` 自动判断）
    use_sqlite_file: Optional[bool] = None
    #: 允许下载的目录白名单（默认空 = 不允许任何文件下载）
    artifact_roots: Sequence[str] = ()

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="asistent-console-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.token = TEST_TOKEN
        self.token_path = make_token_file(self.tmpdir, self.token)
        self.db_path = os.path.join(self.tmpdir, "asistent.sqlite3")
        #: HTTP 服务的日志（用于断言"日志里没有 token / 没有 query"）
        self.log_lines: List[str] = []
        self.store = self._build_store()
        self.addCleanup(self._close_store)
        self.servers: List[Any] = []
        self.client, self.server = self.start_server()

    # ---- 构造 ----
    def _build_store(self):
        if self.use_sqlite_file is None:
            path = self.db_path if store_is_sqlite() else None
        else:
            path = self.db_path if self.use_sqlite_file else None
        if path:
            # 名称里带 ASISTENT_STORE 的语义：SQLite 必须落文件，否则"重启"没有意义
            return store_factory(path)
        return store_factory()

    def _close_store(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    def start_server(
        self,
        *,
        login: bool = True,
        static_dir: Optional[str] = None,
        artifact_roots: Optional[Sequence[str]] = None,
        settings_overrides: Optional[Dict[str, Any]] = None,
    ) -> Tuple[ConsoleClient, Any]:
        """起一个真实 HTTP 服务，端口由系统分配（避免与设备上的 18090 冲突）。"""
        import asistent.console.http_api as http_api

        settings = ConsoleSettings(
            store=self.store,
            token=self.token,
            bind="127.0.0.1",
            port=0,
            token_path=self.token_path,
            static_dir=static_dir or os.path.join(
                os.path.dirname(os.path.abspath(http_api.__file__)), "static"
            ),
            artifact_roots=tuple(artifact_roots or self.artifact_roots),
            **(settings_overrides or {}),
        )
        server = build_server(settings, log_sink=self.log_lines.append)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        self.addCleanup(self._stop_server, server)
        client = ConsoleClient(server.bound_host or "127.0.0.1", server.bound_port, self.token if login else "")
        return client, server

    def _stop_server(self, server) -> None:
        try:
            server.shutdown()
        finally:
            server.server_close()

    def restart_server(self) -> ConsoleClient:
        """模拟服务重启：关掉旧服务实例，用**同一个持久化 store** 起新实例。

        SQLite 模式下会先把连接关掉再重新打开同一个库文件 —— 这才算真的"重启"，
        只换 HTTP 服务对象是骗自己的（数据本来就在同一个进程内存里）。
        reference（内存）模式**不能**关 store：关掉就没数据了，这里只换服务实例，
        真正的"换进程重启"由 `test_resilience.ProcessRestartTests` 用子进程覆盖。
        """
        for server, _thread in list(self.servers):
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        self.servers = []
        if store_is_sqlite():
            self._close_store()
            self.store = store_factory(self.db_path)
        client, server = self.start_server()
        return client

    # ---- 断言 ----
    def assertStatus(self, expected: int, recorded: RecordedRequest, message: str = "") -> None:
        self.assertEqual(
            expected,
            recorded.status,
            "%s\n%s" % (message or "状态码不符", recorded.render()),
        )

    def assertTokenAbsent(self, recorded: RecordedRequest) -> None:
        """响应里不得出现 token 明文（含响应头与响应体）。"""
        blob = recorded.response_body + json.dumps(recorded.response_headers, ensure_ascii=False)
        self.assertNotIn(self.token, blob)

    # ---- 造数据 ----
    def process_fixture(self, name: str, *, case_id: str = ""):
        """用 MVP-3 的公开夹具跑一遍完整流程，得到真实的 Case/Question/Draft。"""
        from tests.understand import helpers as understand_helpers

        document = understand_helpers.make_document(
            self.store, name, document_id="doc_" + name.replace(".", "-"), case_id=case_id or None
        )
        report = understand_helpers.process(self.store, document, name=name)
        case = None
        if getattr(report, "case_id", ""):
            try:
                case = self.store.get("Case", report.case_id)
            except Exception:
                case = None
        return document, report, case

    def first_case_id(self) -> str:
        cases = self.store.find("Case", limit=None)
        self.assertTrue(cases, "夹具没有产生任何 Case")
        return cases[0].id


def _free_port() -> int:  # pragma: no cover - 备用的端口探测（当前用 port=0）
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class SubprocessServer:
    """以**独立进程**运行控制台服务（真重启测试用）。

    `stop()` 用 terminate 结束进程；`wait` 时检查退出码，避免"进程早就崩了但测试还在连"。
    stdout 逐行读：第一行必须是 `LISTENING <host> <port>`，之后的日志行留在 `self.log_lines`。
    """

    def __init__(self, config: Dict[str, Any], *, cwd: str = "", wait_seconds: float = 30.0) -> None:
        import subprocess

        self.config = dict(config)
        self.log_lines: List[str] = []
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve_once.py")
        self.process = subprocess.Popen(
            [sys.executable, "-X", "utf8", script, json.dumps(self.config, ensure_ascii=False)],
            cwd=cwd or _REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.host, self.port = self._await_listening(wait_seconds)

    def _await_listening(self, wait_seconds: float) -> Tuple[str, int]:
        import time

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            line = self.process.stdout.readline()
            if not line:
                if self.process.poll() is not None:
                    raise AssertionError(
                        "子进程服务启动失败（退出码 %s）：\n%s" % (self.process.returncode, "\n".join(self.log_lines))
                    )
                continue
            line = line.rstrip("\n")
            self.log_lines.append(line)
            if line.startswith("LISTENING "):
                _, host, port = line.split()
                return host, int(port)
        raise AssertionError("等待子进程服务监听超时：\n%s" % "\n".join(self.log_lines))

    def client(self, token: str) -> ConsoleClient:
        return ConsoleClient(self.host, self.port, token)

    def drain_logs(self) -> List[str]:
        """把已产生的 stdout 收干净（非阻塞读，读不到就返回）。

        用 `os.set_blocking` 而不是 `select`：Windows 上 `select` 只支持 socket，
        对管道会直接报错（"真重启"测试要在 Windows 与设备上都跑得起来）。
        """
        stream = self.process.stdout
        if stream is None:
            return list(self.log_lines)
        try:
            os.set_blocking(stream.fileno(), False)
        except (OSError, ValueError):  # pragma: no cover - 已关闭
            return list(self.log_lines)
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                self.log_lines.append(line.rstrip("\n"))
        except (TypeError, ValueError, OSError):  # 非阻塞下没有数据时可能抛错
            pass
        return list(self.log_lines)

    def stop(self) -> int:
        """结束子进程。

        返回退出码，但**不保证是 0**：`terminate()` 会让服务进程以信号结束
        （Windows 上表现为非零），这本身就是"被停掉"的正常结果。测试应当断言
        "进程确实结束了 + 数据仍在"，而不是断言某个特定的退出码。
        """
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except Exception:
                self.process.kill()
                self.process.wait(timeout=10)
        return int(self.process.returncode if self.process.returncode is not None else -1)

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def wait_stopped(self, timeout: float = 20.0) -> bool:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                return True
            time.sleep(0.05)
        return False


def posix_permissions_supported() -> bool:
    return not _WINDOWS


__all__ = [
    "ConsoleClient",
    "ConsoleTestCase",
    "RecordedRequest",
    "SubprocessServer",
    "TEST_TOKEN",
    "load_token",
    "make_token_file",
    "posix_permissions_supported",
    "store_factory",
    "store_is_sqlite",
]
