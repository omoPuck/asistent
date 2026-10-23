"""存储层（MVP-0）。

- `SqliteStore`：SQLite 实现，完整覆盖冻结契约 `asistent.contracts.store_api.StoreProtocol`；
- `migrations`：schema 版本账本与只向前追加的迁移；
- `search_index`：FTS5 检索表原语（中文按 `asistent.contracts.search` 规范化）。

激活接缝：`ASISTENT_STORE=sqlite` → `asistent.storage.sqlite_store.SqliteStore`
（见 tests/contracts/helpers.py 的 `store_factory`）。
"""

from __future__ import annotations

from . import migrations, search_index
from .sqlite_store import OBJECT_TYPES, OPEN_CASE_STATES, STATE_FIELD_BY_TYPE, SqliteStore, open_store

__all__ = [
    "OBJECT_TYPES",
    "OPEN_CASE_STATES",
    "STATE_FIELD_BY_TYPE",
    "SqliteStore",
    "migrations",
    "open_store",
    "search_index",
]
