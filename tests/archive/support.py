"""测试支撑：在临时目录里造一个**可执行假 bdpan CLI** 与它的故障注入环境。

- Windows：生成 `bdpan.cmd`（Python 的 subprocess 能直接执行 .cmd，已实测）；
- POSIX（设备）：生成 `bdpan`（`#!/bin/sh`，chmod 755）；
- 假 CLI 与它的调用日志都放在**纯 ASCII 临时路径**下，避免中文路径在 cmd.exe /
  scp / 子进程编码上踩坑（详见 docs/mvp/mvp-7.md 的编码一节）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional

FAKE_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_bdpan.py")

_ENV_KEYS = ("FAKE_BDPAN_SPEC", "FAKE_BDPAN_REMOTE", "FAKE_BDPAN_LOG", "FAKE_BDPAN_ARGV_LOG")


class FakeBdpanEnv:
    """一个测试用的"设备"：假 CLI + 远端目录 + 故障规格 + 调用日志。"""

    def __init__(self, case: Optional[unittest.TestCase] = None) -> None:
        self.root = tempfile.mkdtemp(prefix="fake-bdpan-")
        self.remote = os.path.join(self.root, "remote")
        self.payloads = os.path.join(self.root, "payloads")
        os.makedirs(self.remote, exist_ok=True)
        os.makedirs(self.payloads, exist_ok=True)
        self.spec_path = os.path.join(self.root, "spec.json")
        self.log_path = os.path.join(self.root, "calls.jsonl")
        self.argv_path = os.path.join(self.root, "argv.jsonl")
        self.fake_py = os.path.join(self.root, "fake_bdpan.py")
        shutil.copyfile(FAKE_SOURCE, self.fake_py)
        self._saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        self.binary = self._write_wrapper()
        self.set_spec({})
        os.environ["FAKE_BDPAN_SPEC"] = self.spec_path
        os.environ["FAKE_BDPAN_REMOTE"] = self.remote
        os.environ["FAKE_BDPAN_LOG"] = self.log_path
        os.environ["FAKE_BDPAN_ARGV_LOG"] = self.argv_path
        if case is not None:
            case.addCleanup(self.cleanup)

    # ------------------------------------------------------------------ 环境构造
    def _write_wrapper(self) -> str:
        if os.name == "nt":
            path = os.path.join(self.root, "bdpan.cmd")
            body = (
                "@echo off\r\n"
                f'"{sys.executable}" "{self.fake_py}" %*\r\n'
            )
            with open(path, "w", encoding="ascii", newline="") as fh:
                fh.write(body)
            return path
        path = os.path.join(self.root, "bdpan")
        body = f'#!/bin/sh\nexec "{sys.executable}" "{self.fake_py}" "$@"\n'
        with open(path, "w", encoding="ascii", newline="\n") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
        return path

    def set_spec(self, spec: Dict[str, Any]) -> None:
        with open(self.spec_path, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False, sort_keys=True)

    def set_env(self, **values: str) -> None:
        for key, value in values.items():
            os.environ[key] = value

    # ------------------------------------------------------------------ 文件
    def make_local_file(self, name: str, content: str) -> str:
        path = os.path.join(self.payloads, name)
        os.makedirs(os.path.dirname(path) or self.payloads, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def make_local_bytes(self, name: str, content: bytes) -> str:
        path = os.path.join(self.payloads, name)
        os.makedirs(os.path.dirname(path) or self.payloads, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def remote_path(self, relative: str) -> str:
        return os.path.join(self.remote, relative.replace("/", os.sep))

    def write_remote(self, relative: str, content: str) -> str:
        path = self.remote_path(relative)
        os.makedirs(os.path.dirname(path) or self.remote, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    # ------------------------------------------------------------------ 日志
    def calls(self) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.log_path):
            return []
        with open(self.log_path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def argv_log(self) -> List[List[str]]:
        if not os.path.isfile(self.argv_path):
            return []
        with open(self.argv_path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def reset_logs(self) -> None:
        for path in (self.log_path, self.argv_path):
            with open(path, "w", encoding="utf-8"):
                pass

    def calls_of(self, action: str, *, successful_only: bool = False) -> List[Dict[str, Any]]:
        """某类调用的日志。`successful_only=True` 只看成功的那些（默认看全部尝试）。"""
        return [
            item
            for item in self.calls()
            if item.get("action") == action and (item.get("success") or not successful_only)
        ]

    def upload_count(self) -> int:
        return len(self.calls_of("upload"))

    def download_count(self) -> int:
        return len(self.calls_of("download"))

    def uploaded_targets(self, since: int = 0) -> List[str]:
        return [item.get("target", "") for item in self.calls_of("upload")][since:]

    def successful_uploads(self) -> List[str]:
        return [item.get("target", "") for item in self.calls_of("upload") if item.get("success")]

    # ------------------------------------------------------------------ 清理
    def cleanup(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.root, ignore_errors=True)


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
