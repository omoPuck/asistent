"""原文快照存储：让"索引失败不丢原始资料"成为可验证的事实。

为什么需要它：`SourceDocument` 只登记元数据（路径/大小/哈希/版本），
而**可检索正文必须来自真实原始内容**。如果索引只从源文件路径现读：
- 源文件被删除/改名 → 索引彻底丢内容；
- 源文件是二进制/超大 → 索引拿不到东西。

所以采集阶段把原始字节按 `content_hash` 落成**只读快照**（内容寻址、天然去重），
索引阶段优先用快照、其次读源文件；两者都不可得才是真正的 `MISSING_DATA`。
快照只写不删不覆盖（同哈希同内容），任何模型输出都写不到这里。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from ..contracts.ids import sha256_bytes

#: 快照目录名（位于工作根之下）
SNAPSHOT_DIRNAME = "raw"
METADATA_SUFFIX = ".meta"


@dataclass
class StoredSnapshot:
    sha256: str
    path: str
    size_bytes: int
    created: bool


class SnapshotStore:
    """内容寻址的原文快照仓库。"""

    def __init__(self, root: str, *, verify_on_put: bool = True) -> None:
        self.root = os.path.abspath(root)
        self.verify_on_put = verify_on_put

    # ------------------------------------------------------------------ 路径
    def path_for(self, content_hash: str) -> str:
        return os.path.join(self.root, content_hash[:2], f"{content_hash}.bin")

    def metadata_path_for(self, content_hash: str) -> str:
        return os.path.join(self.root, content_hash[:2], f"{content_hash}{METADATA_SUFFIX}")

    def has(self, content_hash: str) -> bool:
        return bool(content_hash) and os.path.isfile(self.path_for(content_hash))

    # ------------------------------------------------------------------ 写
    def put(
        self,
        data: bytes,
        *,
        content_hash: Optional[str] = None,
        relative_path: str = "",
    ) -> StoredSnapshot:
        """写入快照（已存在则复用，不重写）。返回落盘位置与是否新写入。"""
        digest = content_hash or sha256_bytes(data)
        if self.verify_on_put and content_hash and digest != content_hash:
            raise ValueError(f"内容哈希不一致：声明 {content_hash} 实际 {digest}")
        target = self.path_for(digest)
        if os.path.isfile(target):
            return StoredSnapshot(sha256=digest, path=target, size_bytes=os.path.getsize(target), created=False)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        temporary = f"{target}.tmp-{os.getpid()}"
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, target)
        if relative_path:
            metadata = self.metadata_path_for(digest)
            stored_relative = relative_path
            if os.path.isfile(metadata):
                try:
                    with open(metadata, "r", encoding="utf-8") as handle:
                        existing = handle.read().strip()
                except OSError:  # pragma: no cover - 读不到就重写
                    existing = ""
                if existing and existing != relative_path:
                    # 同一内容出现在多个路径：保留首个（说明"同内容多路径"已是事实，不覆盖）
                    stored_relative = existing
                else:
                    stored_relative = relative_path
            with open(metadata, "w", encoding="utf-8") as handle:
                handle.write(stored_relative)
        return StoredSnapshot(sha256=digest, path=target, size_bytes=len(data), created=True)

    # ------------------------------------------------------------------ 读
    def get(self, content_hash: str) -> Optional[bytes]:
        path = self.path_for(content_hash)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            data = handle.read()
        if self.verify_on_put and sha256_bytes(data) != content_hash:
            # 快照被改过：宁可当作没有，也不拿被污染的内容去做索引
            return None
        return data

    def describe(self, content_hash: str) -> str:
        metadata = self.metadata_path_for(content_hash)
        if not os.path.isfile(metadata):  # pragma: no cover - 元数据可选
            return ""
        with open(metadata, "r", encoding="utf-8") as handle:
            return handle.read().strip()

    def list_hashes(self) -> List[str]:
        found: List[str] = []
        if not os.path.isdir(self.root):
            return found
        for directory in sorted(os.listdir(self.root)):
            sub = os.path.join(self.root, directory)
            if not os.path.isdir(sub):
                continue
            for name in sorted(os.listdir(sub)):
                if name.endswith(".bin"):
                    found.append(name[: -len(".bin")])
        return found

    def total_bytes(self) -> int:
        total = 0
        for digest in self.list_hashes():
            total += os.path.getsize(self.path_for(digest))
        return total


def snapshot_sink(store_holder: dict) -> object:
    """给 `CollectionPolicy.raw_sink` 用的适配器（延迟绑定 SnapshotStore）。"""

    def sink(absolute_path: str, relative_path: str, content_hash: str, raw: bytes) -> None:
        snapshots: SnapshotStore = store_holder["snapshots"]
        snapshots.put(raw, content_hash=content_hash, relative_path=relative_path)

    return sink


__all__ = ["SnapshotStore", "StoredSnapshot", "SNAPSHOT_DIRNAME", "snapshot_sink"]
