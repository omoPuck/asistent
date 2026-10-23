"""SQLite schema 账本与迁移（MVP-0，纯标准库）。

契约依据：SPEC §15.1（MVP-0 = SQLite 持久化）、§21.1（状态/版本必须独立持久化）。

规则（这三条是留给后续 MVP 的硬约束）：

1. `schema_migrations` 是**只追加**的版本账本：一个版本一行，版本号单调递增；
2. 迁移只允许**继续追加**，禁止改写已发布的历史条目——改写历史会让设备上
   已经建好的库无法升级到同一形态（"代码和库对不上"）；
3. 打开库时自动补齐缺失迁移，每个迁移在**单个事务**里执行：失败整体回滚，
   不会留下半个 schema。

FTS5 的处理：v3 建检索表。如果当前 SQLite 没编译 FTS5，v3 记录"已跳过"的备注
而不是让整库打不开；但**检索能力绝不允许静默降级**——`asistent.storage.search_index.search()`
在表不存在时明确报错（见 asistent.contracts.search 的契约要求）。

Python 3.10 兼容：只使用 3.10 就有的语法与标准库（静态闸门见
tests/storage/test_python310_compat.py）。
"""

from __future__ import annotations

import sqlite3
from typing import Callable, List, Optional, Tuple

from asistent.contracts.timeutil import utcnow_iso

from . import search_index

#: 当前代码期望的 schema 版本（= MIGRATIONS 里最大版本号）
SCHEMA_TARGET_VERSION = 4

#: v1：核心表（对象 / 幂等键 / 审计 / 事件 / 元数据）
V1_DDL: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version    INTEGER PRIMARY KEY,
        note       TEXT NOT NULL DEFAULT '',
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS objects (
        object_type   TEXT NOT NULL,
        id            TEXT NOT NULL,
        class_name    TEXT NOT NULL DEFAULT '',
        revision      INTEGER NOT NULL,
        created_at    TEXT NOT NULL DEFAULT '',
        updated_at    TEXT NOT NULL DEFAULT '',
        state         TEXT,
        case_id       TEXT,
        index_state   TEXT,
        archive_state TEXT,
        payload       TEXT NOT NULL,
        PRIMARY KEY (object_type, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS unique_keys (
        namespace   TEXT NOT NULL,
        value       TEXT NOT NULL,
        object_type TEXT NOT NULL,
        object_id   TEXT NOT NULL,
        PRIMARY KEY (namespace, value)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        seq            INTEGER PRIMARY KEY AUTOINCREMENT,
        id             TEXT NOT NULL UNIQUE,
        at             TEXT NOT NULL DEFAULT '',
        action         TEXT NOT NULL DEFAULT '',
        actor_kind     TEXT NOT NULL DEFAULT 'SYSTEM',
        actor_id       TEXT NOT NULL DEFAULT 'system',
        execution_id   TEXT,
        case_id        TEXT,
        object_type    TEXT,
        object_id      TEXT,
        result         TEXT NOT NULL DEFAULT 'OK',
        version_before INTEGER,
        version_after  INTEGER,
        payload        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id                TEXT NOT NULL PRIMARY KEY,
        dedupe_key        TEXT NOT NULL,
        kind              TEXT NOT NULL DEFAULT '',
        source            TEXT NOT NULL DEFAULT '',
        source_event_id   TEXT NOT NULL DEFAULT '',
        occurred_at       TEXT NOT NULL DEFAULT '',
        received_at       TEXT NOT NULL DEFAULT '',
        case_id           TEXT,
        conversation_id   TEXT,
        processed_at      TEXT,
        processing_result TEXT,
        claimed_by        TEXT,
        claimed_at        TEXT,
        payload           TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedupe ON events(dedupe_key)",
    """
    CREATE TABLE IF NOT EXISTS store_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)

#: v2：索引 + 审计只追加触发器（审计语义由数据库强制，而不是靠调用方自觉）
V2_DDL: Tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_objects_type_state ON objects(object_type, state)",
    "CREATE INDEX IF NOT EXISTS idx_objects_type_case ON objects(object_type, case_id)",
    "CREATE INDEX IF NOT EXISTS idx_objects_type_index_state ON objects(object_type, index_state)",
    "CREATE INDEX IF NOT EXISTS idx_objects_type_created ON objects(object_type, created_at, id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_object ON audit_events(object_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_events(case_id, seq)",
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_append_only_update
    BEFORE UPDATE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events 只允许追加：禁止 UPDATE');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_append_only_delete
    BEFORE DELETE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events 只允许追加：禁止 DELETE');
    END
    """,
)


class Migration:
    """一条迁移：版本号 + 备注 + 执行体。

    `apply(conn)` 返回 `None` 表示正常应用；返回字符串表示"应用了但带旁注"
    （例如 FTS5 不可用时跳过建表），旁注会写进账本，便于运维排查。
    """

    __slots__ = ("version", "note", "apply")

    def __init__(self, version: int, note: str, apply: Callable[[sqlite3.Connection], Optional[str]]) -> None:
        self.version = version
        self.note = note
        self.apply = apply

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "Migration(version=%r, note=%r)" % (self.version, self.note)


def _apply_v1(conn: sqlite3.Connection) -> Optional[str]:
    for statement in V1_DDL:
        conn.execute(statement)
    return None


def _apply_v2(conn: sqlite3.Connection) -> Optional[str]:
    for statement in V2_DDL:
        conn.execute(statement)
    return None


def _apply_v3(conn: sqlite3.Connection) -> Optional[str]:
    """建立 FTS5 检索表；FTS5 不可用时跳过（旁注入账本，检索 API 会明确报错）。"""
    if not search_index.fts5_available(conn):
        return "FTS5 不可用：跳过 search_index（检索 API 将明确报错，不静默降级）"
    search_index.ensure(conn)
    return None


def _table_columns(conn: sqlite3.Connection, table: str) -> Tuple[str, ...]:
    return tuple(str(row[1]) for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall())


def _add_write_sequence(conn: sqlite3.Connection, table: str) -> None:
    """给一张表补 `seq`（写入序号）并回填历史行。

    回填按 `rowid` 升序赋序号：`rowid` 就是这些行的实际插入顺序，等价于"当年就是按写入顺序排的"。
    之后新行的 `seq` 由应用侧取 `MAX(seq) + 1`，因此严格大于所有历史行。
    """
    if "seq" in _table_columns(conn, table):
        return
    conn.execute("ALTER TABLE %s ADD COLUMN seq INTEGER" % table)
    conn.execute("UPDATE %s SET seq = rowid WHERE seq IS NULL" % table)


def _apply_v4(conn: sqlite3.Connection) -> Optional[str]:
    """写入序号：定序必须与写入顺序一致（契约 v0.5，CP-20261010-11）。

    为什么需要它：`created_at` 只有毫秒精度，快速连续写入经常落在同一毫秒里；
    此前用 `(created_at, id)` 排序，而 id 是随机的 → 平局判据随机 → 分页漏项/重复、
    `limit=1` 随机取到不同对象、两个后端给出不同答案。

    这里刻意不动 v1 的建表语句（历史迁移不改写），v4 对**新旧库一视同仁**地 `ALTER + 回填`，
    所以"全新建库"和"从旧库升级"得到完全相同的 schema。
    """
    _add_write_sequence(conn, "objects")
    _add_write_sequence(conn, "events")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_objects_seq ON objects(seq)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_seq ON events(seq)")
    # 定序键从 (created_at, id) 变成 (created_at, seq)：换掉 v2 建的旧索引，避免重复维护
    conn.execute("CREATE INDEX IF NOT EXISTS idx_objects_type_created_seq ON objects(object_type, created_at, seq)")
    conn.execute("DROP INDEX IF EXISTS idx_objects_type_created")
    return None


MIGRATIONS: Tuple[Migration, ...] = (
    Migration(1, "核心表：objects / unique_keys / audit_events / events / store_meta", _apply_v1),
    Migration(2, "列索引 + 审计只追加触发器", _apply_v2),
    Migration(3, "FTS5 检索表 search_index（中文按 contracts.search 规范化后入库）", _apply_v3),
    Migration(4, "写入序号 seq：objects/events 的定序与写入顺序一致（契约 v0.5）", _apply_v4),
)


def connect(path: str, *, timeout: float = 30.0) -> sqlite3.Connection:
    """打开连接：显式事务 + 原生字符串（不做隐式类型转换）+ WAL。

    - `isolation_level=None`：由我们显式 `BEGIN IMMEDIATE` / `COMMIT`，
      不做 sqlite3 模块的隐式事务魔法，事务边界可读；
    - `check_same_thread=False`：Python 3.10 的 `sqlite3.threadsafety` 恒为 1，
      默认不允许跨线程用连接，而契约要求多线程并发领取（SPEC §21.2）。
      安全性由 `SqliteStore` 的可重入锁保证（见 sqlite_store.py）；
    - `busy_timeout`：跨进程争用时等待写锁，先把"等"变成延迟而不是立刻报错。
    """
    connection = sqlite3.connect(
        path,
        timeout=timeout,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = %d" % int(timeout * 1000))
    connection.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    return connection


def table_names(conn: sqlite3.Connection) -> List[str]:
    """当前库里的表名（含 FTS5 虚拟表）。

    用位置下标取列：外部传入的连接可能没设 `row_factory`。
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def current_schema_version(conn: sqlite3.Connection) -> int:
    """账本里的最大版本号；没有账本表（全新库或非本项目的库）时返回 0。"""
    if "schema_migrations" not in table_names(conn):
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def applied_migrations(conn: sqlite3.Connection) -> List[Tuple[int, str, str]]:
    """已应用迁移清单（version, note, applied_at），按版本号升序。"""
    if "schema_migrations" not in table_names(conn):
        return []
    rows = conn.execute(
        "SELECT version, note, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [(int(row[0]), str(row[1]), str(row[2])) for row in rows]


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:  # pragma: no cover - 事务已被数据库自己结束
        pass


def apply_migrations(conn: sqlite3.Connection, *, upto: Optional[int] = None) -> List[int]:
    """补齐缺失迁移，返回本次实际应用的版本号列表。

    调用约定：连接必须处于**无活动事务**状态（`connect()` 的默认状态）。
    `upto` 只用于测试"旧库升级"路径（建一个低版本库再升级）。
    """
    target = SCHEMA_TARGET_VERSION if upto is None else int(upto)
    current = current_schema_version(conn)
    applied: List[int] = []
    for migration in MIGRATIONS:
        if migration.version <= current or migration.version > target:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            extra_note = migration.apply(conn)
            note = migration.note if not extra_note else "%s | %s" % (migration.note, extra_note)
            conn.execute(
                "INSERT INTO schema_migrations(version, note, applied_at) VALUES (?, ?, ?)",
                (migration.version, note, utcnow_iso()),
            )
            conn.execute("COMMIT")
        except BaseException:
            _rollback_quietly(conn)
            raise
        applied.append(migration.version)
    return applied


def initialize(conn: sqlite3.Connection) -> List[int]:
    """建库/升级入口：写出 store 元数据，再补齐迁移。"""
    applied = apply_migrations(conn)
    conn.execute(
        "INSERT INTO store_meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(current_schema_version(conn)),),
    )
    conn.execute(
        "INSERT INTO store_meta(key, value) VALUES ('created_at', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (utcnow_iso(),),
    )
    return applied


__all__ = [
    "MIGRATIONS",
    "Migration",
    "SCHEMA_TARGET_VERSION",
    "applied_migrations",
    "apply_migrations",
    "connect",
    "current_schema_version",
    "initialize",
    "table_names",
]
