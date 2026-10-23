"""FTS5 索引存储 + 检索（中文检索的唯一正确路径）。

**为什么不能直接用 `unicode61` 查中文**：FTS5 的 `unicode61` 分词器把连续中文当成
一个 token，`MATCH '部署'` 命中 0 行**且不报错**（设备实测，见 docs/env/report.md §B5）。
本模块强制走契约层：

- 入库：`normalize_for_index()`（NFKC + 小写 + CJK 逐字切分）→ 存进 `search_index`；
- 查询：`to_fts_query()`（每个 token 加引号，CJK 连续串转短语）→ 才允许进 `MATCH`；
- 建表：`FTS_DDL`（契约里那一份，不另造）；
- 无效查询（空串/纯符号）→ `to_fts_query()` 返回 `None`，调用方**必须**当无效处理，
  绝不退化成"返回全部结果"。

**引用里为什么用"渲染时现切"**：`normalize_for_index` 是逐字切分且统一小写，
拿它做引用摘录会丢失原文形态。所以额外存一列 `raw_body`（原始文本）用于展示，
检索仍然只看切分后的 `body` 列——中文子串命中 + 原文可定位，两者都要。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..contracts.search import FTS_DDL, fts5_available, normalize_for_index, to_fts_query

#: 检索表名（`search_index`，与契约 `FTS_DDL` 同名）与文档表名（`document_index`）
SEARCH_TABLE = "search_index"
DOCUMENT_TABLE = "document_index"

#: 检索表的**列集**（与契约 `FTS_DDL` 同表名、同分词器；多出来的列全部 `UNINDEXED`）
#: - `body`：`normalize_for_index()` 之后的切分正文（**唯一**被检索的列）；
#: - `raw_body`：原文快照，`UNINDEXED`，只用于引用展示与行号定位；
#: - `content_version` / `content_hash`：引用里必填的版本与哈希（SPEC §6.2）。
_SEARCH_COLUMNS = (
    "doc_id UNINDEXED, path UNINDEXED, body, raw_body UNINDEXED, "
    "content_version UNINDEXED, content_hash UNINDEXED"
)
_SEARCH_TOKENIZE = "tokenize='unicode61 remove_diacritics 2'"

#: 契约 `FTS_DDL` 的等价扩展版（表名仍是 `search_index`，仍然是 unicode61 + remove_diacritics 2）
SEARCH_INDEX_DDL = f"CREATE VIRTUAL TABLE IF NOT EXISTS {SEARCH_TABLE} USING fts5({_SEARCH_COLUMNS}, {_SEARCH_TOKENIZE});"

#: 文档检索表：与 `search_index` 列集完全一致，两者内容在写入时保持一致
DOCUMENT_INDEX_DDL: Tuple[str, ...] = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {DOCUMENT_TABLE} USING fts5({_SEARCH_COLUMNS}, {_SEARCH_TOKENIZE});",
)

#: 实际建表用的 DDL（契约 `FTS_DDL` 由 `verify_contract_ddl_compatibility()` 校验兼容性）
CREATE_DDL: Tuple[str, ...] = (SEARCH_INDEX_DDL,) + DOCUMENT_INDEX_DDL

#: 供文档与排查展示的完整 DDL 列表
ALL_DDL: Tuple[str, ...] = (FTS_DDL,) + CREATE_DDL

#: 索引状态表：让"索引失败/待重建"在库里可查，而不是只存在于内存报告里
INDEX_STATE_DDL = (
    "CREATE TABLE IF NOT EXISTS index_state ("
    "doc_id TEXT PRIMARY KEY, path TEXT NOT NULL, index_state TEXT NOT NULL, "
    "content_version INTEGER NOT NULL DEFAULT 1, content_hash TEXT NOT NULL DEFAULT '', "
    "indexed_at TEXT, error TEXT);"
)


class FtsUnavailableError(RuntimeError):
    """当前 SQLite 未编译 FTS5：必须显式失败，不允许静默降级为全表扫描。"""

    def __init__(self, message: str = "SQLite 未编译 FTS5，无法建立检索索引（禁止静默降级）") -> None:
        super().__init__(message)
        self.code = "FTS5_UNAVAILABLE"


@dataclass
class IndexedDoc:
    """一条已入库的检索记录（原文与切分后正文都在）。"""

    doc_id: str
    path: str
    body: str
    raw_body: str
    content_version: int = 1
    content_hash: str = ""


@dataclass
class IndexWriteResult:
    doc_id: str
    stored: bool
    revision: Optional[int] = None
    index_state: Optional[str] = None
    error: Optional[str] = None
    normalized_chars: int = 0


def open_index_db(path: Optional[str] = None) -> sqlite3.Connection:
    """打开（或内存创建）索引库并建表。`path=None` → 内存库（测试用）。"""
    if path and path != ":memory:":
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(path or ":memory:")
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("PRAGMA foreign_keys=ON;")
    if not fts5_available(connection):
        connection.close()
        raise FtsUnavailableError()
    _ensure_fts_tables(connection)
    connection.execute(INDEX_STATE_DDL)
    connection.commit()
    return connection


def _ensure_fts_tables(connection: sqlite3.Connection) -> None:
    """建表；若已有的是旧列集（缺 `raw_body`），按当前列集重建（索引是派生数据，可重建）。

    注意：契约 `FTS_DDL` 用 `IF NOT EXISTS` 且列集更窄，**不能**用它来建 `search_index`，
    否则会先建出窄表、扩展 DDL 被静默跳过。这里改成：先用 `FTS_DDL` 建一张**临时表**
    验证列名兼容（表名/分词器/列名前缀一致），再按扩展列集建正式表。
    """
    if not verify_contract_ddl_compatibility(connection):  # pragma: no cover - 契约漂移时才会触发
        raise FtsUnavailableError("契约 FTS_DDL 的列集与索引列集不兼容，拒绝建表（需先对齐契约）")
    for table in (SEARCH_TABLE, DOCUMENT_TABLE):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if columns and "raw_body" not in columns:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
    for statement in CREATE_DDL:
        connection.execute(statement)
    connection.commit()


def verify_contract_ddl_compatibility(connection: sqlite3.Connection) -> bool:
    """用契约 `FTS_DDL` 建临时表，确认其列名是我们列集的**前缀子集**。

    这样既"复用契约 DDL"（而不是绕过它），又不会被它的窄列集拖住引用所需的字段。
    """
    probe = "__fts_contract_probe"
    try:
        connection.execute(FTS_DDL.replace(SEARCH_TABLE, probe))
        contract_columns = [row[1] for row in connection.execute(f"PRAGMA table_info({probe})").fetchall()]
    except sqlite3.Error:  # pragma: no cover - FTS5 不可用时也当作不兼容
        return False
    finally:
        try:
            connection.execute(f"DROP TABLE IF EXISTS {probe}")
        except sqlite3.Error:  # pragma: no cover
            pass
    return contract_columns == ["doc_id", "path", "body"]


def delete_document(connection: sqlite3.Connection, doc_id: str) -> Tuple[int, int]:
    """删除一个文档的两张索引表记录（返回 (search_table_deleted, document_table_deleted)）。"""
    first = connection.execute(f"DELETE FROM {SEARCH_TABLE} WHERE doc_id = ?", (doc_id,)).rowcount
    second = connection.execute(f"DELETE FROM {DOCUMENT_TABLE} WHERE doc_id = ?", (doc_id,)).rowcount
    connection.commit()
    return int(first), int(second)


def upsert_document(
    connection: sqlite3.Connection,
    *,
    doc_id: str,
    path: str,
    raw_body: str,
    content_version: int = 1,
    content_hash: str = "",
) -> IndexedDoc:
    """写入/更新一条检索记录。

    **入库前必须走 `normalize_for_index()`**——这是中文检索能命中的唯一原因。
    检索用正文（切分后）写 `body`，原文写 `raw_body`（`UNINDEXED`），互不污染。
    """
    body = normalize_for_index(raw_body)
    delete_document(connection, doc_id)
    for table in (SEARCH_TABLE, DOCUMENT_TABLE):
        connection.execute(
            f"INSERT INTO {table}(doc_id, path, body, raw_body, content_version, content_hash) "
            "VALUES (?,?,?,?,?,?)",
            (doc_id, path, body, raw_body, int(content_version), content_hash),
        )
    connection.execute(
        "INSERT INTO index_state(doc_id, path, index_state, content_version, content_hash, indexed_at, error) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(doc_id) DO UPDATE SET path=excluded.path, index_state=excluded.index_state, "
        "content_version=excluded.content_version, content_hash=excluded.content_hash, "
        "indexed_at=excluded.indexed_at, error=excluded.error",
        (doc_id, path, "INDEXED", int(content_version), content_hash, _utcnow_iso(), None),
    )
    connection.commit()
    return IndexedDoc(
        doc_id=doc_id,
        path=path,
        body=body,
        raw_body=raw_body,
        content_version=int(content_version),
        content_hash=content_hash,
    )


def mark_index_state(
    connection: sqlite3.Connection,
    *,
    doc_id: str,
    path: str,
    index_state: str,
    content_version: int = 1,
    content_hash: str = "",
    error: Optional[str] = None,
) -> None:
    """记录索引状态（PENDING/INDEXED/STALE/FAILED/REBUILD_REQUIRED），失败原因必须落库。"""
    connection.execute(
        "INSERT INTO index_state(doc_id, path, index_state, content_version, content_hash, indexed_at, error) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(doc_id) DO UPDATE SET path=excluded.path, index_state=excluded.index_state, "
        "content_version=excluded.content_version, content_hash=excluded.content_hash, "
        "indexed_at=excluded.indexed_at, error=excluded.error",
        (
            doc_id,
            path,
            index_state,
            int(content_version),
            content_hash,
            _utcnow_iso() if index_state == "INDEXED" else None,
            error,
        ),
    )
    connection.commit()


def index_state_rows(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    cursor = connection.execute(
        "SELECT doc_id, path, index_state, content_version, content_hash, indexed_at, error "
        "FROM index_state ORDER BY doc_id"
    )
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def indexed_hashes(connection: sqlite3.Connection, table: str = DOCUMENT_TABLE) -> Dict[str, Tuple[str, str]]:
    """返回 `{doc_id: (path, content_hash)}`，用于增量判定与孤儿清理。"""
    cursor = connection.execute(f"SELECT doc_id, path, content_hash FROM {table}")
    return {str(row[0]): (str(row[1]), str(row[2])) for row in cursor.fetchall()}


def count_rows(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def fetch_raw_body(connection: sqlite3.Connection, doc_id: str, table: str = DOCUMENT_TABLE) -> Optional[str]:
    row = connection.execute(f"SELECT raw_body FROM {table} WHERE doc_id = ? LIMIT 1", (doc_id,)).fetchone()
    return None if row is None else str(row[0])


def _utcnow_iso() -> str:
    from ..contracts.timeutil import utcnow_iso

    return utcnow_iso()


__all__ = [
    "FtsUnavailableError",
    "FTS_DDL",
    "ALL_DDL",
    "CREATE_DDL",
    "SEARCH_INDEX_DDL",
    "DOCUMENT_INDEX_DDL",
    "INDEX_STATE_DDL",
    "SEARCH_TABLE",
    "DOCUMENT_TABLE",
    "IndexedDoc",
    "IndexWriteResult",
    "open_index_db",
    "upsert_document",
    "delete_document",
    "mark_index_state",
    "index_state_rows",
    "indexed_hashes",
    "count_rows",
    "fetch_raw_body",
]
