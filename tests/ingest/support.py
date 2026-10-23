"""测试夹具：临时工作区 + 契约存储 + 真实 FTS5 索引。"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import struct
import tempfile
import unittest
import zlib
from typing import Any, Dict, List, Optional, Sequence

from asistent.contracts.reference import ReferenceStore
from asistent.ingest.collector import CollectionPolicy, collect_directory
from asistent.index.fts import open_index_db
from asistent.index.pipeline import SearchPipeline

FIXTURE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "fixtures", "public")


def fixture_path(*parts: str) -> str:
    return os.path.join(FIXTURE_ROOT, *parts)


def make_png(width: int = 1, height: int = 1) -> bytes:
    """生成一个最小的真实 PNG（用于"二进制文件必须被跳过且记录原因"的测试）。"""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = b"\x00" + b"\xff\xff\xff\x00" * width
    body = zlib.compress(raw * height)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", body)
        + chunk(b"IEND", b"")
    )


class Workbench:
    """一个临时工作区：`data/` 放资料，`raw/` 放原文快照，`index.db` 放索引。"""

    def __init__(self, root: str) -> None:
        self.root = root
        self.data_dir = os.path.join(root, "data")
        self.snapshot_dir = os.path.join(root, "raw")
        self.index_path = os.path.join(root, "index", "fts.db")
        os.makedirs(self.data_dir, exist_ok=True)
        self.store = ReferenceStore()
        self.connection = open_index_db(self.index_path)
        self.pipeline = SearchPipeline(self.store, self.connection, snapshot_root=self.snapshot_dir)

    # ------------------------------------------------------------------ 文件
    def write(self, relative_path: str, text: str, *, encoding: str = "utf-8") -> str:
        target = os.path.join(self.data_dir, relative_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(text.encode(encoding))
        return target

    def write_bytes(self, relative_path: str, data: bytes) -> str:
        target = os.path.join(self.data_dir, relative_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)
        return target

    def path(self, relative_path: str) -> str:
        return os.path.join(self.data_dir, relative_path.replace("/", os.sep))

    def remove(self, relative_path: str) -> None:
        os.remove(self.path(relative_path))

    def rename(self, old: str, new: str) -> None:
        target = self.path(new)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(self.path(old), target)

    def collect(self, *, roots: Optional[Sequence[str]] = None, policy: Optional[CollectionPolicy] = None, **overrides: Any):
        """采集并返回 `CollectionReport`（大多数测试关心的是采集结果本身）。"""
        return self.run(roots=roots, policy=policy, **overrides).collection

    def collect_again(self, *, roots: Optional[Sequence[str]] = None):
        return self.collect(roots=roots)

    def run(self, *, roots: Optional[Sequence[str]] = None, policy: Optional[CollectionPolicy] = None, **overrides: Any):
        """采集并返回带实测耗时的 `CollectionRun`。"""
        return self.pipeline.collect(
            list(roots) if roots is not None else [self.data_dir],
            policy=policy,
            **overrides,
        )

    # ------------------------------------------------------------------ 观测
    def counts(self) -> Dict[str, int]:
        return self.store.stats()

    def audit_count(self) -> int:
        return len(self.store.audit_trail(limit=100000))

    def documents(self) -> List[Any]:
        return self.store.find("SourceDocument", limit=1000)

    def document_by_relative(self, relative_path: str) -> Optional[Any]:
        for document in self.documents():
            if document.source_id == relative_path:
                return document
        return None

    def search(self, query: str, *, limit: int = 20):
        return self.pipeline.search(query, limit=limit)

    def refresh(self) -> None:
        """重新打开连接（验证数据真的落盘，而不是只在内存里）。"""
        self.connection.close()
        self.connection = open_index_db(self.index_path)
        self.pipeline = SearchPipeline(self.store, self.connection, snapshot_root=self.snapshot_dir)

    def close(self) -> None:
        self.connection.close()


class MVP1TestCase(unittest.TestCase):
    """每个测试一个临时工作区，结束后清理（不留垃圾在仓库里）。"""

    keep_workspace = False

    def setUp(self) -> None:
        self._tempdir = tempfile.mkdtemp(prefix="asistent-mvp1-")
        self.bench = Workbench(self._tempdir)
        self.addCleanup(self._tempdir_cleanup)

    def _tempdir_cleanup(self) -> None:
        try:
            self.bench.close()
        except Exception:  # pragma: no cover - 清理阶段忽略
            pass
        if self.keep_workspace:
            return
        shutil.rmtree(self._tempdir, ignore_errors=True)

    # ------------------------------------------------------------------ 断言
    def assert_hits(self, response, paths: Sequence[str], *, locator_prefix: str = "") -> None:
        """断言命中集合恰好等于 `paths`（两边都排序后比较，避免依赖返回顺序）。"""
        self.assertTrue(response.valid_query, f"查询应有效: {response.invalid_reason}")
        hits = sorted(hit.path.replace("\\", "/") for hit in response.hits)
        expected = sorted(path.replace("\\", "/") for path in paths)
        self.assertEqual(expected, hits)
        if locator_prefix:
            for hit in response.hits:
                self.assertTrue(
                    hit.locator.startswith(locator_prefix), f"定位应为 {locator_prefix}*，实际 {hit.locator}"
                )

    def hit_paths(self, response) -> List[str]:
        return sorted(hit.path.replace("\\", "/") for hit in response.hits)


def dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def raw_match(connection: sqlite3.Connection, expression: str, *, table: str = "document_index") -> List[Any]:
    """直接执行契约层的 MATCH 表达式，返回 `(doc_id, path)` 原始行。

    这是"中文检索真实命中"的**原始输出**来源：不走本仓任何封装，直接看 SQLite 给的答案。
    """
    return connection.execute(
        f"SELECT doc_id, path FROM {table} WHERE {table} MATCH ? ORDER BY doc_id",
        (expression,),
    ).fetchall()


__all__ = [
    "Workbench",
    "MVP1TestCase",
    "FIXTURE_ROOT",
    "fixture_path",
    "make_png",
    "dump",
    "raw_match",
]
