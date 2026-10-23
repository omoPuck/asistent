"""tests/storage 公共夹具（纯标准库）。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any, List, Tuple

from asistent.storage.sqlite_store import SqliteStore

#: 仓库根目录（worktree 根）：子进程用它作为 cwd，因此 `import asistent` 可用
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_python(source: str, *args: str, timeout: float = 180.0) -> Tuple[int, str]:
    """在独立进程里跑一段脚本（cwd = 仓库根，stdout/stderr 合并返回）。"""
    command = [sys.executable, "-c", source] + list(args)
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return int(completed.returncode), completed.stdout.decode("utf-8", "replace")


class SqliteStoreTestCase(unittest.TestCase):
    """每个测试一个独立的库文件目录；同一个 `db_path` 用于模拟"进程重启"。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="asistent-storage-test-")
        self.db_path = os.path.join(self.tmpdir, "asistent.sqlite3")
        self._stores: List[SqliteStore] = []
        # 先注册 rmtree，后注册 close：addCleanup 是 LIFO，所以会先关连接再删目录
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def open_store(self, path: Any = None, **kwargs: Any) -> SqliteStore:
        store = SqliteStore(path=self.db_path if path is None else path, **kwargs)
        self._stores.append(store)
        self.addCleanup(store.close)
        return store

    def restart(self, store: SqliteStore, **kwargs: Any) -> SqliteStore:
        """关闭当前实例并打开同一路径的新实例（模拟进程重启）。"""
        store.close()
        self._stores.remove(store)
        return self.open_store(**kwargs)

    def assert_object_present(self, name: str, store: SqliteStore) -> None:
        """断言 sqlite_master 里存在某张表/触发器/索引。"""
        row = store.connection.execute(
            "SELECT name FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()
        self.assertIsNotNone(row, "期望 %s 存在" % name)
