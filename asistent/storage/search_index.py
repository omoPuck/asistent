"""FTS5 检索索引原语（MVP-0 只负责建表与读写原语；接线到资料流水线是 MVP-1）。

契约依据：`asistent.contracts.search`（SPEC §6.2、§21.1）：

1. 入库前必须 `normalize_for_index()`：NFKC + 小写 + **CJK 逐字切分** + 空白折叠。
   直接用 `unicode61` 把 `"部署方案"` 当一个 token 存进去，查 `部署` 会**静默返回 0 行**；
2. 查询必须 `to_fts_query()`：每个 token 加引号，连续 CJK 转成短语查询，
   邮件正文/资料是不可信输入，绝不能拼进 `MATCH`；
3. `to_fts_query()` 返回 `None`（空串/纯符号）时**不得**退化成"返回全部结果"；
4. 索引是派生数据：切分规则变了必须重建，且保留"待重建"状态。

FTS5 不可用时 `search()` 明确抛错，不静默降级为全表扫描。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from asistent.contracts.search import FTS_DDL, fts5_available, normalize_for_index, to_fts_query

#: 检索表名（与 contracts.search.FTS_DDL 一致；MVP-1 必须使用同一份 DDL）
TABLE = "search_index"


class SearchIndexUnavailable(RuntimeError):
    """FTS5 检索表不可用（SQLite 未编译 FTS5，或迁移被跳过）。"""


def ensure(conn: sqlite3.Connection) -> None:
    """建表（幂等）。FTS5 缺失时抛 SearchIndexUnavailable。"""
    try:
        conn.execute(FTS_DDL)
    except sqlite3.Error as exc:
        raise SearchIndexUnavailable("当前 SQLite 无法创建 FTS5 表：%s" % exc) from exc


def is_ready(conn: sqlite3.Connection) -> bool:
    """检索表是否已存在（迁移 v3 是否真正建了表）。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    return row is not None


def check_available(conn: sqlite3.Connection) -> bool:
    """FTS5 编译能力探测（不改动真实 schema）。"""
    return fts5_available(conn)


def index_text(conn: sqlite3.Connection, *, document_id: str, path: str, body: str) -> None:
    """写入/覆盖一份文档正文（同一 document_id 重放幂等）。"""
    if not is_ready(conn):
        raise SearchIndexUnavailable("检索表 %s 不存在：FTS5 不可用或迁移未应用" % TABLE)
    conn.execute("DELETE FROM %s WHERE doc_id = ?" % TABLE, (document_id,))
    conn.execute(
        "INSERT INTO %s(doc_id, path, body) VALUES (?, ?, ?)" % TABLE,
        (document_id, path, normalize_for_index(body)),
    )


def search(conn: sqlite3.Connection, query: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    """检索。无效查询（空串/纯符号）返回 `[]`，而不是返回全部结果。"""
    if not is_ready(conn):
        raise SearchIndexUnavailable("检索表 %s 不存在：FTS5 不可用或迁移未应用" % TABLE)
    expression = to_fts_query(query)
    if expression is None:
        return []
    rows = conn.execute(
        "SELECT doc_id, path FROM %s WHERE %s MATCH ? ORDER BY rank LIMIT ?" % (TABLE, TABLE),
        (expression, int(limit)),
    ).fetchall()
    return [{"doc_id": str(row["doc_id"]), "path": str(row["path"])} for row in rows]


def drop(conn: sqlite3.Connection) -> None:
    """删除检索表（重建索引用：切分规则变化后必须重建）。"""
    conn.execute("DROP TABLE IF EXISTS %s" % TABLE)


__all__ = ["TABLE", "SearchIndexUnavailable", "check_available", "drop", "ensure", "index_text", "is_ready", "search"]
