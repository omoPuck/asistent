"""可脚本化的**假 bdpan CLI**（子进程），用于离线演练真实通道的全部失败矩阵。

为什么是"假 CLI 子进程"而不是 mock 对象：真实通道的失败模式（超时、崩溃、退出码、
stderr 文本、`--json` 输出格式、未授权）都发生在**子进程边界**上；只有真的起一个子进程，
才能验证"超时 = 结果未知""未授权 = 显式 NOT_AUTHORIZED""凭据不进 argv"这些语义。

用法（由 `BdpanCliPanClient(binary=...)` 调用）：

    python fake_bdpan.py <subcommand> ... --no-check-update [--json]

子命令覆盖真实 CLI 的只读/写接口：`version` / `whoami` / `ls` / `upload` / `download` /
`rm` / `mkdir`。远端就是 `FAKE_BDPAN_REMOTE` 指向的本地目录。

故障注入：环境变量 `FAKE_BDPAN_SPEC` 指向一个 JSON 文件：

    {
      "logged_in": true,                  # false → whoami 输出"未登录"（并让写操作报未授权）
      "unauthorized_error": false,        # true → 所有操作返回 errno=-6（令牌过期）
      "faults": {
        "objects/a.txt": {
          "upload": "fail",               # ok | fail | timeout | crash | unauthorized | corrupt
          "upload_times": 1,              # 只在前 N 次上传时注入故障
          "download": "corrupt"           # ok | fail | timeout | corrupt（回读哈希不一致）
        }
      },
      "mkdir_supported": true,            # false → mkdir 报 unknown command
      "log": "/path/calls.jsonl"          # 每次调用追加一行（含 subcommand/success/argc）
    }

每次调用都会把一行 JSON 追加到 `FAKE_BDPAN_LOG`（若设置）：
`{"argv_tail": [...], "subcommand": "...", "action": "upload", "target": "...", "success": true}`
—— 测试据此做"第二次运行只补传失败对象：传输次数 = 失败数"这类**计数断言**。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

SPEC_PATH_ENV = "FAKE_BDPAN_SPEC"
REMOTE_ENV = "FAKE_BDPAN_REMOTE"
LOG_ENV = "FAKE_BDPAN_LOG"
ARGV_ENV = "FAKE_BDPAN_ARGV_LOG"

DEFAULT_REMOTE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fake_bdpan_remote")


def _load_spec() -> Dict[str, Any]:
    path = os.environ.get(SPEC_PATH_ENV)
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _log(entry: Dict[str, Any]) -> None:
    path = os.environ.get(LOG_ENV)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _log_argv(argv: List[str]) -> None:
    """把**子进程实际收到的完整 argv** 记下来：证明凭据从未进入命令行。"""
    path = os.environ.get(ARGV_ENV)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv, ensure_ascii=False) + "\n")


def _remote_root() -> str:
    root = os.environ.get(REMOTE_ENV, DEFAULT_REMOTE)
    os.makedirs(root, exist_ok=True)
    return os.path.abspath(root)


def _resolve(relative: str) -> str:
    root = _remote_root()
    cleaned = relative.lstrip("/").replace("\\", "/")
    absolute = os.path.abspath(os.path.join(root, cleaned))
    if not absolute.startswith(root):
        raise SystemExit(2)
    return absolute


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _fault(spec: Dict[str, Any], relative: str, action: str, state: Dict[str, Any]) -> Optional[str]:
    """查故障配置：先精确匹配 key，再按**后缀**匹配（方便测试只写 `objects/a.txt`）。"""
    faults = spec.get("faults") or {}
    entry = faults.get(relative)
    if entry is None:
        for key, candidate in faults.items():
            if relative.endswith(str(key).lstrip("/")) or str(key).lstrip("/") == relative:
                entry = candidate
                break
    entry = entry or {}
    fault = entry.get(action)
    if not fault:
        return None
    limit = entry.get(f"{action}_times")
    if limit is not None:
        counter_key = f"{relative}|{action}"
        used = int(state.get(counter_key, 0))
        if used >= int(limit):
            return None
        state[counter_key] = used + 1
        _write_state(state)
    return str(fault)


def _state_path() -> str:
    return os.path.join(_remote_root(), ".fake_bdpan_state.json")


def _read_state() -> Dict[str, Any]:
    path = _state_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_state(state: Dict[str, Any]) -> None:
    with open(_state_path(), "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, sort_keys=True)


def _positional(args: List[str]) -> List[str]:
    out: List[str] = []
    for item in args:
        if item.startswith("--"):
            continue
        out.append(item)
    return out


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    elif isinstance(payload, str):
        print(payload)


def _unauthorized(spec: Dict[str, Any]) -> bool:
    return bool(spec.get("unauthorized_error"))


def _fail(code: int, message: str, as_json: bool, errno_value: int = -1) -> int:
    if as_json:
        print(json.dumps({"errno": errno_value, "error": message}, ensure_ascii=False))
    else:
        print(message, file=sys.stderr)
    return code


def main(argv: Optional[List[str]] = None) -> int:
    # 假 CLI 必须像真实 Go 版 bdpan 一样**说 UTF-8**：否则父进程按 utf-8 解码会把中文
    # 错误信息变成 U+FFFD，测试就变成在"测编码陷阱"而不是在测通道语义。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - 某些环境不支持
                pass
    argv = list(sys.argv[1:] if argv is None else argv)
    _log_argv(sys.argv[1:] if argv is None else argv)
    spec = _load_spec()
    state = _read_state()
    as_json = "--json" in argv
    positional = _positional(argv)
    if not positional:
        print("usage: fake_bdpan <subcommand> …", file=sys.stderr)
        return 2
    subcommand = positional[0]
    rest = positional[1:]

    if _unauthorized(spec):
        _log({"subcommand": subcommand, "action": subcommand, "success": False, "reason": "unauthorized"})
        return _fail(1, "错误：登录已过期，请重新登录（errno=-6）", as_json, errno_value=-6)

    if subcommand == "version":
        _log({"subcommand": subcommand, "success": True})
        _emit("fake-bdpan: 3.8.7-fake", as_json)
        return 0

    if subcommand == "whoami":
        _log({"subcommand": subcommand, "success": True})
        if spec.get("logged_in", True):
            _emit("已登录：fake-user", as_json)
            return 0
        _emit("未登录，请先执行 bdpan login", as_json)
        return 0

    if not spec.get("logged_in", True):
        _log({"subcommand": subcommand, "success": False, "reason": "not_logged_in"})
        return _fail(1, "错误：未登录，请先执行 bdpan login", as_json)

    if subcommand == "mkdir":
        if not rest:
            return _fail(2, "usage: mkdir <dir>", as_json)
        if not spec.get("mkdir_supported", True):
            _log({"subcommand": subcommand, "target": rest[0], "success": False, "reason": "unsupported"})
            return _fail(2, "unknown command \"mkdir\" for \"bdpan\"", as_json)
        target = _resolve(rest[0])
        existed = os.path.isdir(target)
        os.makedirs(target, exist_ok=True)
        _log({"subcommand": subcommand, "target": rest[0], "success": True, "existed": existed})
        _emit({"errno": 0, "path": rest[0]}, as_json)
        return 0

    if subcommand == "ls":
        directory = rest[0] if rest else "/"
        target = _resolve(directory)
        if not os.path.isdir(target):
            _log({"subcommand": subcommand, "target": directory, "success": False, "reason": "missing_dir"})
            return _fail(1, f"错误：目录不存在 {directory}", as_json, errno_value=-8)
        items: List[Dict[str, Any]] = []
        for name in sorted(os.listdir(target)):
            absolute = os.path.join(target, name)
            is_dir = os.path.isdir(absolute)
            items.append(
                {
                    "path": f"{directory.rstrip('/')}/{name}",
                    "server_filename": name,
                    "size": 0 if is_dir else os.path.getsize(absolute),
                    "isdir": 1 if is_dir else 0,
                    "server_mtime": int(os.path.getmtime(absolute)),
                    # 真实 CLI 返回百度侧 md5（不可作可信校验）；本假 CLI 同样给一个 md5，
                    # 用来证明通道确实**不**采用它（README 的 U-2 结论）
                    "md5": "0" * 32,
                }
            )
        _log({"subcommand": subcommand, "target": directory, "success": True, "count": len(items)})
        _emit(items, as_json)
        return 0

    if subcommand in ("upload", "put"):
        if len(rest) < 2:
            return _fail(2, "usage: upload <local> <remote>", as_json)
        local, remote = rest[0], rest[1]
        relative = remote.lstrip("/")
        fault = _fault(spec, relative, "upload", state)
        if fault == "timeout":
            # 超时的真实含义：副作用**可能已经发生**。默认先把对象写到远端再挂起，
            # 这样上层必须靠 stat/回读判定，而不是盲目重传（A-5 约束 4）。
            effect = (spec.get("faults") or {}).get(relative, {}).get("timeout_effect") or spec.get(
                "timeout_effect", "write_then_hang"
            )
            if effect == "write_then_hang" and os.path.isfile(local):
                target = _resolve(remote)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(local, "rb") as src, open(target, "wb") as dst:
                    dst.write(src.read())
            _log(
                {
                    "subcommand": subcommand,
                    "action": "upload",
                    "target": relative,
                    "success": False,
                    "reason": "timeout",
                    "side_effect": effect,
                }
            )
            time.sleep(float(spec.get("timeout_seconds", 5)))
            return 0
        if fault == "crash":
            _log({"subcommand": subcommand, "action": "upload", "target": relative, "success": False, "reason": "crash"})
            os._exit(9)
        if fault == "unauthorized":
            _log({"subcommand": subcommand, "action": "upload", "target": relative, "success": False, "reason": "unauthorized"})
            return _fail(1, "错误：登录已过期（errno=-6）", as_json, errno_value=-6)
        if fault == "fail":
            # 泛化失败：真实 bdpan 的未知 errno/崩溃等价物 → 上层必须按"结果未知"处理
            _log({"subcommand": subcommand, "action": "upload", "target": relative, "success": False, "reason": "fail"})
            return _fail(1, "错误：上传失败", as_json, errno_value=-7)
        if fault == "not_found":
            # 确定性失败：远端明确说对象不存在（没有副作用）
            _log(
                {"subcommand": subcommand, "action": "upload", "target": relative, "success": False, "reason": "not_found"}
            )
            return _fail(1, f"错误：文件不存在 {relative}", as_json, errno_value=-9)
        if fault == "conflict":
            # 确定性失败：远端已有同名对象且策略拒绝覆盖（errno=-10）
            _log(
                {"subcommand": subcommand, "action": "upload", "target": relative, "success": False, "reason": "conflict"}
            )
            return _fail(1, f"错误：同名文件已存在，拒绝覆盖 {relative}", as_json, errno_value=-10)
        if fault == "corrupt":
            # 上传成功但落到远端的字节被改写：回读哈希必然不一致
            target = _resolve(remote)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("corrupted-by-fake-bdpan")
            _log({"subcommand": subcommand, "action": "upload", "target": relative, "success": True, "corrupted": True})
            _emit({"errno": 0, "path": remote}, as_json)
            return 0
        if not os.path.isfile(local):
            _log({"subcommand": subcommand, "action": "upload", "target": relative, "success": False, "reason": "local_missing"})
            return _fail(1, "错误：本地文件不存在", as_json)
        target = _resolve(remote)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(local, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())
        _log(
            {
                "subcommand": subcommand,
                "action": "upload",
                "target": relative,
                "success": True,
                "sha256": _sha256_file(target),
            }
        )
        _emit({"errno": 0, "path": remote}, as_json)
        return 0

    if subcommand in ("download", "get"):
        if len(rest) < 2:
            return _fail(2, "usage: download <remote> <local>", as_json)
        remote, local = rest[0], rest[1]
        relative = remote.lstrip("/")
        fault = _fault(spec, relative, "download", state)
        if fault == "timeout":
            _log({"subcommand": subcommand, "action": "download", "target": relative, "success": False, "reason": "timeout"})
            time.sleep(float(spec.get("timeout_seconds", 5)))
            return 0
        if fault == "fail":
            _log({"subcommand": subcommand, "action": "download", "target": relative, "success": False, "reason": "fail"})
            return _fail(1, "错误：下载失败", as_json)
        source = _resolve(remote)
        if not os.path.isfile(source):
            _log({"subcommand": subcommand, "action": "download", "target": relative, "success": False, "reason": "missing"})
            return _fail(1, f"错误：文件不存在 {remote}", as_json, errno_value=-9)
        os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
        with open(source, "rb") as src:
            data = src.read()
        if fault == "corrupt":
            data = b"corrupted-by-fake-bdpan-download"
        with open(local, "wb") as dst:
            dst.write(data)
        _log(
            {
                "subcommand": subcommand,
                "action": "download",
                "target": relative,
                "success": True,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        _emit({"errno": 0, "path": local}, as_json)
        return 0

    if subcommand == "rm":
        if not rest:
            return _fail(2, "usage: rm <remote> [--force]", as_json)
        relative = rest[0].lstrip("/")
        target = _resolve(rest[0])
        state_for_rm = _fault(spec, relative, "rm", state)
        if state_for_rm == "fail":
            _log({"subcommand": subcommand, "action": "rm", "target": relative, "success": False, "reason": "fail"})
            return _fail(1, "错误：删除失败", as_json)
        if os.path.isfile(target):
            os.remove(target)
            _log({"subcommand": subcommand, "action": "rm", "target": relative, "success": True})
            _emit({"errno": 0}, as_json)
            return 0
        _log({"subcommand": subcommand, "action": "rm", "target": relative, "success": False, "reason": "missing"})
        return _fail(1, f"错误：文件不存在 {relative}", as_json, errno_value=-9)

    _log({"subcommand": subcommand, "success": False, "reason": "unknown"})
    return _fail(2, f'unknown command "{subcommand}" for "bdpan"', as_json)


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    sys.exit(main())
