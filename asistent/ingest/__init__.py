"""MVP-1 资料采集层（SPEC §6.1、§6.2、§15.2、§16 MVP-1）。

职责边界：
- 只负责**发现 → 判重 → 读取 → 写入 SourceDocument**，不做检索（检索在 `asistent.index`）；
- 只依赖 `asistent.contracts.store_api.StoreProtocol`（构造注入），不实现自己的存储后端；
- **绝不修改源文件**：全程只读打开目录与文件；
- 跳过任何文件都必须留下**可查询的跳过记录**（审计 + 事件），不允许静默丢弃。

身份与判重：
- 身份键 = `namespace="source_document", source_system, 规范化相对路径` → 对象 ID 确定性派生，
  重复扫描落回同一个 `SourceDocument`；
- 变化判定 = `mtime + size + sha256`：三者都没变则**完全不写库**（对象数、审计条数都不变）。

用法（契约注入）：

    from asistent.contracts import ReferenceStore
    from asistent.ingest import CollectionPolicy, collect_directory

    store = ReferenceStore()
    report = collect_directory(store, ["./资料"], policy=CollectionPolicy())
"""

from __future__ import annotations

from .collector import (
    CollectionPolicy,
    CollectionReport,
    FileStatus,
    FileOutcome,
    collect_directory,
    source_document_id,
)
from .files import (
    TEXT_CODECS,
    FileKind,
    classify_bytes,
    decode_text,
    extract_index_text,
)
from .scanner import ScanItem, scan_directory

__all__ = [
    "CollectionPolicy",
    "CollectionReport",
    "FileStatus",
    "FileOutcome",
    "collect_directory",
    "source_document_id",
    "ScanItem",
    "scan_directory",
    "FileKind",
    "classify_bytes",
    "decode_text",
    "extract_index_text",
    "TEXT_CODECS",
]
