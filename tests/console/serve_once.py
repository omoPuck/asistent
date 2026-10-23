"""以子进程方式启动控制台服务的运行脚本（供"真重启"测试与设备演练使用）。

用法：

    python -X utf8 tests/console/serve_once.py '{"db_path": "...", "token_path": "...", "port": 0}'

这是**被测对象**：父进程（测试）杀掉它、再起一个全新的它，用来证明"状态不在进程内存里"。
它故意不复用 `tests/console/helpers.py` 的测试基类 —— 重启后的服务必须像部署时一样，
只依赖"库文件 + token 文件"这两样东西。
"""

from __future__ import annotations

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from asistent.console import ConsoleService, ConsoleSettings, TokenAuth, build_server  # noqa: E402
from asistent.console.auth import load_token  # noqa: E402


def _log(line: str) -> None:
    """逐行输出日志并立刻 flush。

    stdin/stdout 是管道时 Python 用**块缓冲**：不 flush 的话日志会卡在缓冲区里，
    父进程（测试/运维脚本）看不到"这台设备刚才发生了什么"。每行 flush 的代价对
    控制台这种低频请求完全可以接受。
    """
    print(line, flush=True)


def main(argv) -> int:
    config = json.loads(argv[1]) if len(argv) > 1 else {}
    backend = str(config.get("store", "sqlite")).lower()
    if backend == "sqlite":
        from asistent.storage.sqlite_store import SqliteStore  # type: ignore

        store = SqliteStore(path=config["db_path"])
    else:
        from asistent.contracts import ReferenceStore

        store = ReferenceStore()
    token = load_token(config["token_path"])
    settings = ConsoleSettings(
        store=store,
        token=token,
        bind=config.get("bind", "127.0.0.1"),
        port=int(config.get("port", 0)),
        token_path=config["token_path"],
        artifact_roots=tuple(config.get("artifact_roots", ())),
    )
    service = ConsoleService(store, TokenAuth(token), clock=store.now)
    server = build_server(settings, service, log_sink=_log)
    # 关键：把**真实端口**打到 stdout 第一行，父进程据此连上来（`port=0` 时尤其必要）
    print("LISTENING %s %d" % (server.bound_host, server.bound_port), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
