"""MVP-1 采集与检索测试包。

测试只依赖契约层：存储用 `asistent.contracts.reference.ReferenceStore`（语义基准），
索引用真实 SQLite FTS5（`asistent.contracts.search.FTS_DDL`）。
"""

from __future__ import annotations
