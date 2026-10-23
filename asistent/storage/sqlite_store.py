"""SQLite 存储实现（MVP-0）。

契约：`asistent.contracts.store_api.StoreProtocol`（冻结签名）；
语义基准：`asistent.contracts.reference.ReferenceStore`——同一组 `tests/contracts/**`
必须在两个实现上表现一致（切换点：环境变量 `ASISTENT_STORE`）。

## 实现要点与 SPEC 的对应关系

- **单连接 + 进程内可重入锁 + 显式事务**：Python 3.10 的 `sqlite3.threadsafety`
  恒为 1，默认连接不允许跨线程使用，而契约要求多线程并发领取动作
  （§21.2 "一个原子调用，禁止先查询后标记"）。因此连接以
  `check_same_thread=False` 打开，所有公开方法在同一把可重入锁下执行：
  进程内串行，跨线程安全；
- **跨进程靠 SQLite 自己的写锁**：写入路径一律 `BEGIN IMMEDIATE`，
  在"读—判断—写"之前就拿到写锁，跨进程也不会出现 TOCTOU；
  `busy_timeout` 把争用变成等待而不是立刻报错；
- **CAS**：更新必须携带 `expected_revision`，旧版本写入 → `RevisionConflict`，
  禁止静默覆盖（§21.1）；
- **WAL + synchronous=FULL**：提交即持久，进程被杀/重启后已提交事务不丢；
- **审计只追加**：`audit_events` 表上挂 BEFORE UPDATE/DELETE 触发器直接 ABORT，
  由数据库强制，而不是靠调用方自觉（§11.3）；
- **结果未知**：重启恢复时 `IN_FLIGHT` 只能转 `UNKNOWN` 并查证，
  绝不按租约到期自动重发（§21.2）；
- **schema 版本**：`schema_migrations` 账本 + 只向前追加的迁移（见 migrations.py）。

## 已知限制（详见 docs/mvp/mvp-0.md）

- payload 以 JSON 存储：元组→列表（契约对象字段都声明为 list/dict/标量）；
- `find(where=...)` 只对列字段（id/state/case_id/index_state/archive_state）
  下推 SQL，其余键在进程内过滤，以保证与参考实现逐字一致的比较语义；
- 单进程内的写并发被锁串行化；跨进程写并发靠 SQLite 写锁（个人助手单机场景）。

Python 3.10 兼容：只使用 3.10 就有的语法与标准库（设备 `/usr/bin/python3` = 3.10.12）。
静态闸门见 tests/storage/test_python310_compat.py：3.11/3.12 才有的模块、类型与
sqlite3 接口都被列进禁用表，引入即测试变红。
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import weakref
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from asistent.contracts import objects as contract_objects
from asistent.contracts import transitions as tr
from asistent.contracts.actions import DEFAULT_APPROVAL_TTL_SECONDS
from asistent.contracts.enums import ActionState, ActorKind, ApprovalState, AuditAction, IndexState
from asistent.contracts.errors import (
    ActionNotClaimable,
    ApprovalInvalid,
    DuplicateObject,
    NotFound,
    RevisionConflict,
    ValidationError,
)
from asistent.contracts.ids import event_dedupe_key, new_id
from asistent.contracts.objects import (
    ActionAttempt,
    Approval,
    AuditEvent,
    Event,
    Execution,
    SharedObject,
)
from asistent.contracts.store_api import RecoveryScan
from asistent.contracts.timeutil import add_seconds, is_expired, utcnow_iso

from . import migrations, search_index


# --------------------------------------------------------------------------------------
# `limit` 语义的唯一裁决点（契约 v0.3，CP-20261010-03）
#
# 与 asistent.contracts.reference.apply_limit / apply_limit_tail 逐字对齐：
#   None → 不限制；>= 0 → 最多 limit 条（0 → 空列表）；< 0 → ValidationError。
# 刻意**复制**而不是 import 参考实现（存储层不依赖内存参考实现）；
# 防漂移由 tests/storage/test_limit_semantics.py 的 parity 用例逐例比对。
# --------------------------------------------------------------------------------------


def _validate_limit(limit: Optional[int]) -> None:
    """负数立刻报错：不允许靠 Python 负切片隐式"从尾部取"。"""

    if limit is not None and limit < 0:
        raise ValidationError("limit 不允许为负数: %s" % (limit,))


def _apply_limit(rows: List[Any], limit: Optional[int]) -> List[Any]:
    """取**最早**的 limit 条（契约 v0.3 语义）。"""

    _validate_limit(limit)
    if limit is None:
        return rows
    return rows[:limit]


def _apply_limit_tail(rows: List[Any], limit: Optional[int]) -> List[Any]:
    """取**最新**的 limit 条；行序仍保持调用方给的正序（审计轨迹）。"""

    _validate_limit(limit)
    if limit is None:
        return rows
    if limit == 0:
        return []
    return rows[-limit:]

# --------------------------------------------------------------------------------------
# 与参考实现保持一致的语义常量
#
# 这里刻意**复制**而不是 import ReferenceStore 的内部常量：存储实现不应该依赖
# 内存参考实现。防漂移由 tests/storage/test_sqlite_parity.py 校验（逐值相等）。
# --------------------------------------------------------------------------------------

#: 仍被视为"未完成、需要恢复关注"的 Case 状态
OPEN_CASE_STATES: Tuple[str, ...] = (
    "NEW",
    "TRIAGED",
    "WAITING_FOR_CONTEXT",
    "ANALYZING",
    "WAITING_FOR_USER",
    "READY_FOR_REVIEW",
    "APPROVED",
    "EXECUTING",
    "UNKNOWN",
    "ARCHIVING",
    "ARCHIVE_PENDING",
    "BLOCKED",
)

#: 对象类型 → 状态字段名（`transition` 用）
STATE_FIELD_BY_TYPE: Dict[str, str] = {
    "Case": "state",
    "Task": "state",
    "Approval": "state",
    "ExternalAction": "state",
    "ArchiveJob": "state",
    "RestoreJob": "state",
    "Rule": "state",
    "Memory": "state",
    "Question": "state",
    "Draft": "state",
    "Execution": "state",
    "Workflow": "state",
    "WorkflowVersion": "state",
}

#: 需要单独建列（可下推 SQL）的字段
DIRECT_COLUMNS: Tuple[str, ...] = ("state", "case_id", "index_state", "archive_state")

_SELECT_OBJECT = (
    "SELECT object_type, id, class_name, revision, created_at, updated_at, seq, payload "
    "FROM objects"
)


def _build_type_registry() -> Dict[str, type]:
    """OBJECT_TYPE → dataclass 的注册表（从契约对象模块自动收集）。"""
    registry: Dict[str, type] = {}
    for name in getattr(contract_objects, "__all__", []):
        candidate = getattr(contract_objects, name, None)
        if isinstance(candidate, type) and dataclasses.is_dataclass(candidate) and hasattr(candidate, "OBJECT_TYPE"):
            registry[str(candidate.OBJECT_TYPE)] = candidate
    return registry


OBJECT_TYPES: Dict[str, type] = _build_type_registry()


def _class_for(object_type: str, class_name: str = "") -> type:
    """按 OBJECT_TYPE（或存储时的类名）找回 dataclass。

    未知类型退化为 `SharedObject`（通用字段仍然完整），不丢数据。
    """
    found = OBJECT_TYPES.get(object_type)
    if found is not None:
        return found
    for key, value in OBJECT_TYPES.items():
        if value.__name__ == class_name:
            return value
    return SharedObject


def _has_field(cls: type, name: str) -> bool:
    return name in getattr(cls, "__dataclass_fields__", {})


def _dispose(connection: sqlite3.Connection, tmpdir: Optional[str]) -> None:
    """实例被回收时关闭连接并清理自建临时目录。"""
    try:
        connection.close()
    except sqlite3.Error:  # pragma: no cover - 已关闭
        pass
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _decode_payload(payload: str, cls: type) -> SharedObject:
    data = json.loads(payload)
    if not isinstance(data, dict):  # pragma: no cover - 存储被外部破坏
        raise ValidationError("对象 payload 不是 JSON 对象：存储可能被外部破坏")
    return cls.from_dict(data)


class SqliteStore:
    """SQLite 版共享对象存储：契约冻结签名的完整实现。

    构造：
        SqliteStore()                          # 临时目录中的文件库（契约测试用）
        SqliteStore(path="data/asistent.db")   # 显式路径（生产用）
        SqliteStore(path=":memory:", ...)      # 内存库（无 WAL，仅用于快速试验）
    """

    def __init__(
        self,
        *,
        path: Optional[str] = None,
        clock: Optional[Callable[[], str]] = None,
        timeout: float = 30.0,
    ) -> None:
        self._clock = clock or utcnow_iso
        self._lock = threading.RLock()
        self._depth = 0
        self._closed = False
        if path is None:
            self._tmpdir = tempfile.mkdtemp(prefix="asistent-store-")
            path = os.path.join(self._tmpdir, "asistent.sqlite3")
        else:
            self._tmpdir = None
        self.path = path
        self._conn = migrations.connect(path, timeout=timeout)
        self.migrations_applied: List[int] = migrations.initialize(self._conn)
        self._finalizer = weakref.finalize(self, _dispose, self._conn, self._tmpdir)

    # ------------------------------------------------------------------ 基础设施
    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（只读运维/测试用；绕过锁不保证并发安全，不要用它改业务数据）。"""
        return self._conn

    def close(self) -> None:
        """关闭连接（幂等）；自建临时目录会被清理。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._finalizer()

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def now(self) -> str:
        """当前时间（与参考实现一致，可注入时钟）。"""
        return self._clock()

    def schema_version(self) -> int:
        """当前库的 schema 版本（账本最大版本号）。"""
        with self._tx():
            return migrations.current_schema_version(self._conn)

    def wal_enabled(self) -> bool:
        """WAL 是否生效（`PRAGMA journal_mode`）。"""
        with self._tx():
            return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"

    def checkpoint(self) -> None:
        """把 WAL 内容并入主库（运维用：备份前调用；必须在事务外执行）。"""
        self._require_open()
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def fts5_available(self) -> bool:
        """FTS5 编译能力（会建/删一张探测表，因此按写事务处理）。"""
        with self._tx(write=True):
            return search_index.check_available(self._conn)

    def search_index_ready(self) -> bool:
        """检索表是否已经建立（迁移 v3 是否真正建表）。"""
        with self._tx():
            return search_index.is_ready(self._conn)

    def index_document(self, *, document_id: str, path: str, body: str) -> None:
        """把一份文档写入 FTS5 检索表（入库前按契约做中文规范化）。"""
        with self._tx(write=True):
            search_index.index_text(self._conn, document_id=document_id, path=path, body=body)

    def search(self, query: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        """检索；无效查询返回空列表，检索表缺失时明确报错。"""
        with self._tx():
            return search_index.search(self._conn, query, limit=limit)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("SqliteStore 已关闭：不能再访问")

    @contextmanager
    def _tx(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """事务 + 进程内锁。

        可重入：最外层决定事务模式（写用 BEGIN IMMEDIATE 先拿写锁），
        内层复用同一事务，任何一层抛异常都由最外层整体 ROLLBACK。
        """
        self._require_open()
        self._lock.acquire()
        self._depth += 1
        connection = self._conn
        try:
            if self._depth == 1:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            if self._depth == 1:
                connection.execute("COMMIT")
        except BaseException:
            if self._depth == 1:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - 数据库已自行结束事务
                    pass
            raise
        finally:
            self._depth -= 1
            self._lock.release()

    # ------------------------------------------------------------------ 行 <-> 对象
    @staticmethod
    def _state_of(obj: SharedObject) -> str:
        field_name = STATE_FIELD_BY_TYPE.get(obj.OBJECT_TYPE)
        if field_name is None or not hasattr(obj, field_name):
            raise ValidationError(f"{obj.OBJECT_TYPE} 没有可转换的 state 字段")
        return str(getattr(obj, field_name))

    @staticmethod
    def _set_state(obj: SharedObject, target: str) -> None:
        field_name = STATE_FIELD_BY_TYPE.get(obj.OBJECT_TYPE, "state")
        setattr(obj, field_name, target)

    @staticmethod
    def _resolve_case_id(obj: SharedObject) -> Optional[str]:
        if obj.OBJECT_TYPE == "Case":
            return obj.id
        candidate = getattr(obj, "case_id", None)
        if isinstance(candidate, str) and candidate:
            return candidate
        linked = obj.links.get("case") if isinstance(obj.links, dict) else None
        return linked[0] if linked else None

    @staticmethod
    def _columns_for(obj: SharedObject) -> Dict[str, Optional[str]]:
        """派生列：只在对象真的声明了该字段时写值（与 find 的 Python 过滤语义一致）。"""
        cls = type(obj)
        values: Dict[str, Optional[str]] = {}
        for column in DIRECT_COLUMNS:
            if _has_field(cls, column):
                raw = getattr(obj, column, None)
                values[column] = None if raw is None else str(raw)
            else:
                values[column] = None
        return values

    @staticmethod
    def _decode(row: sqlite3.Row) -> SharedObject:
        cls = _class_for(str(row["object_type"]), str(row["class_name"] or ""))
        obj = _decode_payload(str(row["payload"]), cls)
        if not obj.id:
            obj.id = str(row["id"])
        return obj

    @staticmethod
    def _sequence_of(row: sqlite3.Row) -> int:
        """写入序号（契约 v0.5）；`NULL` 只可能来自绕过应用的手写 SQL，按 0 处理。"""
        try:
            value = row["seq"]
        except (IndexError, KeyError):  # pragma: no cover - 查询没选 seq 时不该发生
            return 0
        return 0 if value is None else int(value)

    def _row_by_key(self, object_type: str, object_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            _SELECT_OBJECT + " WHERE object_type = ? AND id = ?", (object_type, object_id)
        ).fetchone()

    def _get_locked(self, object_type: str, object_id: str) -> SharedObject:
        row = self._row_by_key(object_type, object_id)
        if row is None:
            raise NotFound(f"对象不存在: {object_type}/{object_id}")
        return self._decode(row)

    # ------------------------------------------------------------------ 通用对象
    def put(
        self,
        obj: SharedObject,
        *,
        expected_revision: Optional[int] = None,
        unique: Optional[Tuple[str, str]] = None,
        actor_kind: str = ActorKind.SYSTEM,
        actor_id: str = "system",
        reason: str = "",
        audit_action: str = "WRITE",
        audit_detail: Optional[Dict[str, Any]] = None,
    ) -> SharedObject:
        with self._tx(write=True):
            return self._put_locked(
                obj,
                expected_revision=expected_revision,
                unique=unique,
                actor_kind=actor_kind,
                actor_id=actor_id,
                reason=reason,
                audit_action=audit_action,
                audit_detail=audit_detail,
            )

    def _put_locked(
        self,
        obj: SharedObject,
        *,
        expected_revision: Optional[int],
        unique: Optional[Tuple[str, str]],
        actor_kind: str,
        actor_id: str,
        reason: str,
        audit_action: str,
        audit_detail: Optional[Dict[str, Any]],
    ) -> SharedObject:
        """写入对象（调用方必须已持有事务与锁）。"""
        if not obj.id:
            obj.id = new_id(obj.OBJECT_TYPE)
        obj.validate()
        existing = self._row_by_key(obj.OBJECT_TYPE, obj.id)

        if unique is not None:
            namespace, value = unique
            if not namespace or value in (None, ""):
                raise ValidationError("unique 需要 (namespace, 非空值)")
            holder = self._conn.execute(
                "SELECT object_type, object_id FROM unique_keys WHERE namespace = ? AND value = ?",
                (namespace, str(value)),
            ).fetchone()
            if holder is not None and not (
                str(holder["object_type"]) == obj.OBJECT_TYPE and str(holder["object_id"]) == obj.id
            ):
                raise DuplicateObject(
                    f"幂等键已存在，拒绝创建第二份对象：{namespace}={value}",
                    detail={
                        "namespace": namespace,
                        "value": str(value),
                        "existing_type": str(holder["object_type"]),
                        "existing_id": str(holder["object_id"]),
                    },
                )

        if existing is None:
            obj.revision = 1
            obj.created_at = obj.created_at or self.now()
            obj.updated_at = obj.created_at
            action = str(AuditAction.CREATE) if audit_action == "WRITE" else audit_action
            before_revision: Optional[int] = None
        else:
            if expected_revision is None:
                raise ValidationError(
                    "更新必须携带 expected_revision（禁止静默覆盖，SPEC §21.1）",
                    detail={"object_id": obj.id, "current_revision": int(existing["revision"])},
                )
            actual = int(existing["revision"])
            if actual != expected_revision:
                raise RevisionConflict(
                    "revision 冲突：对象已被其他写入者更新",
                    detail={"object_id": obj.id, "expected": expected_revision, "actual": actual},
                )
            before_revision = actual
            obj.revision = before_revision + 1
            obj.created_at = str(existing["created_at"])
            obj.updated_at = self.now()
            action = audit_action

        columns = self._columns_for(obj)
        payload = json.dumps(
            obj.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )
        row = (
            obj.OBJECT_TYPE,
            obj.id,
            type(obj).__name__,
            obj.revision,
            obj.created_at,
            obj.updated_at,
            columns["state"],
            columns["case_id"],
            columns["index_state"],
            columns["archive_state"],
            payload,
        )
        if existing is None:
            # seq：写入序号（契约 v0.5）。只在**首次插入**时分配，更新绝不改写它——
            # 否则"改内容"会把对象挪到队尾，定序就不再等于写入顺序。
            self._conn.execute(
                "INSERT INTO objects(object_type, id, class_name, revision, created_at, updated_at,"
                " state, case_id, index_state, archive_state, seq, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " (SELECT COALESCE(MAX(seq), 0) + 1 FROM objects), ?)",
                row,
            )
        else:
            self._conn.execute(
                "UPDATE objects SET class_name = ?, revision = ?, created_at = ?, updated_at = ?,"
                " state = ?, case_id = ?, index_state = ?, archive_state = ?, payload = ?"
                " WHERE object_type = ? AND id = ?",
                (
                    type(obj).__name__,
                    obj.revision,
                    obj.created_at,
                    obj.updated_at,
                    columns["state"],
                    columns["case_id"],
                    columns["index_state"],
                    columns["archive_state"],
                    payload,
                    obj.OBJECT_TYPE,
                    obj.id,
                ),
            )

        if unique is not None:
            self._conn.execute(
                "INSERT INTO unique_keys(namespace, value, object_type, object_id) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(namespace, value) DO UPDATE SET object_type = excluded.object_type,"
                " object_id = excluded.object_id",
                (unique[0], str(unique[1]), obj.OBJECT_TYPE, obj.id),
            )

        self._append_audit_locked(
            self._make_audit(
                obj,
                action=action,
                actor_kind=actor_kind,
                actor_id=actor_id,
                reason=reason,
                before=before_revision,
                after=obj.revision,
                detail=audit_detail,
            )
        )
        return obj

    def get(self, object_type: str, object_id: str) -> SharedObject:
        with self._tx():
            return self._get_locked(object_type, object_id)

    def find(
        self,
        object_type: str,
        *,
        where: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = 100,
    ) -> List[SharedObject]:
        _validate_limit(limit)
        with self._tx():
            return self._find_locked(object_type, where=where, limit=limit)

    def _find_locked(
        self,
        object_type: str,
        *,
        where: Optional[Dict[str, Any]],
        limit: Optional[int],
    ) -> List[SharedObject]:
        cls = OBJECT_TYPES.get(object_type)
        sql = _SELECT_OBJECT + " WHERE object_type = ?"
        params: List[Any] = [object_type]
        # 只有 `where` 的每个键都能下推 SQL 时，才能让 SQL 的 LIMIT 决定"最早的 n 条"。
        # 否则必须先把候选全取回来、按同一语义在 Python 里过滤完，再 apply_limit——
        # 不然会出现"SQL 先截断、Python 再过滤"从而少返回结果的偏差（与参考实现不一致）。
        fully_pushed_down = True
        for key, value in (where or {}).items():
            if str(value) == "None":
                # 与参考实现的 str() 比较语义保持一致的保守做法：NULL 列不下推
                fully_pushed_down = False
                continue
            if key == "id":
                sql += " AND id = ?"
                params.append(str(value))
            elif key in DIRECT_COLUMNS and cls is not None and _has_field(cls, key):
                sql += " AND CAST(%s AS TEXT) = ?" % key
                params.append(str(value))
            else:
                fully_pushed_down = False
        # 定序键 = (时间戳, 写入序号)：同一毫秒内按**首次写入顺序**，且与 id 无关（契约 v0.5）
        sql += " ORDER BY created_at, seq"
        if limit is not None and limit >= 0 and fully_pushed_down:
            sql += " LIMIT ?"
            params.append(int(limit))

        ordered: List[Tuple[str, int, SharedObject]] = []
        for row in self._conn.execute(sql, params).fetchall():
            obj = self._decode(row)
            if where and any(str(getattr(obj, field, None)) != str(value) for field, value in where.items()):
                continue
            ordered.append((obj.created_at, self._sequence_of(row), obj))
        ordered.sort(key=lambda item: (item[0], item[1]))
        return _apply_limit([item[2] for item in ordered], limit)

    def find_one(self, object_type: str, **where: Any) -> Optional[SharedObject]:
        found = self.find(object_type, where=where or None, limit=1)
        return found[0] if found else None

    def get_by_unique(self, namespace: str, value: str) -> Optional[SharedObject]:
        """按幂等键反查对象（契约测试依赖；见 docs/mvp/mvp-0.md 的 CP-1）。"""
        with self._tx():
            row = self._conn.execute(
                "SELECT object_type, object_id FROM unique_keys WHERE namespace = ? AND value = ?",
                (namespace, str(value)),
            ).fetchone()
            if row is None:
                return None
            return self._get_locked(str(row["object_type"]), str(row["object_id"]))

    # ------------------------------------------------------------------ 状态转换
    def transition(
        self,
        object_type: str,
        object_id: str,
        target_state: str,
        *,
        expected_revision: Optional[int] = None,
        actor_kind: str = ActorKind.AGENT,
        actor_id: str = "agent",
        reason: str = "",
        trigger: Optional[str] = None,
    ) -> SharedObject:
        with self._tx(write=True):
            obj = self._get_locked(object_type, object_id)
            current = self._state_of(obj)
            rule = tr.require_transition(
                object_type, current, target_state, trigger=trigger or actor_kind, object_id=object_id
            )
            if object_type == "Case" and target_state in ("CANCELLED", "BLOCKED"):
                self._require_no_irreversible_action(obj, target_state)
            revision = obj.revision
            if expected_revision is not None and revision != expected_revision:
                raise RevisionConflict(
                    "revision 冲突：状态转换被拒绝",
                    detail={"object_id": object_id, "expected": expected_revision, "actual": revision},
                )
            self._set_state(obj, target_state)
            saved = self._put_locked(
                obj,
                expected_revision=revision,
                unique=None,
                actor_kind=actor_kind,
                actor_id=actor_id,
                reason=reason or rule.note,
                audit_action=str(AuditAction.TRANSITION),
                audit_detail={"from": current, "to": target_state},
            )
            self._emit_state_event(saved, current, target_state)
            return saved

    def _require_no_irreversible_action(self, case_obj: SharedObject, target: str) -> None:
        actions = self._find_locked("ExternalAction", where={"case_id": case_obj.id}, limit=1000)
        blocking = [a for a in actions if str(a.state) in tr.IRREVERSIBLE_ACTION_STATES]
        if blocking:
            raise tr.IllegalTransition(
                f"事务存在不可逆外部动作，不能转换为 {target}",
                detail={
                    "case_id": case_obj.id,
                    "actions": [{"id": a.id, "state": str(a.state)} for a in blocking],
                },
            )

    def set_index_state(
        self,
        document_id: str,
        target_state: str,
        *,
        expected_revision: Optional[int] = None,
        reason: str = "",
        error: Optional[str] = None,
    ) -> SharedObject:
        with self._tx(write=True):
            document = self._get_locked("SourceDocument", document_id)
            current = str(document.index_state)
            tr.machine_for("IndexState").require(
                current, target_state, object_type="IndexState", object_id=document_id
            )
            revision = document.revision
            if expected_revision is not None and revision != expected_revision:
                raise RevisionConflict(
                    "revision 冲突：索引状态更新被拒绝", detail={"document_id": document_id}
                )
            document.index_state = IndexState.coerce(target_state)  # type: ignore[assignment]
            document.index_error = error
            if target_state == "INDEXED":
                document.last_indexed_at = self.now()
            return self._put_locked(
                document,
                expected_revision=revision,
                unique=None,
                actor_kind=ActorKind.SYSTEM,
                actor_id="indexer",
                reason=reason,
                audit_action=str(AuditAction.UPDATE),
                audit_detail={"index_state": target_state, "error": error},
            )

    # ------------------------------------------------------------------ 审计
    def append_audit(self, audit: AuditEvent) -> AuditEvent:
        with self._tx(write=True):
            return self._append_audit_locked(audit)

    def _append_audit_locked(self, audit: AuditEvent) -> AuditEvent:
        if not audit.id:
            audit.id = new_id("AuditEvent")
        audit.at = audit.at or self.now()
        if not audit.action:
            raise ValidationError("审计事件必须带 action")
        audit.revision = max(1, audit.revision)
        audit.created_at = audit.created_at or audit.at
        audit.updated_at = audit.at
        self._insert_audit_row(audit)
        return audit

    def _insert_audit_row(self, audit: AuditEvent) -> None:
        payload = json.dumps(
            audit.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )
        params = [
            audit.id,
            audit.at,
            audit.action,
            str(audit.actor_kind),
            audit.actor_id,
            audit.execution_id,
            audit.case_id,
            audit.object_type,
            audit.object_id,
            audit.result,
            audit.version_before,
            audit.version_after,
            payload,
        ]
        for attempt in range(2):
            try:
                self._conn.execute(
                    "INSERT INTO audit_events(id, at, action, actor_kind, actor_id, execution_id,"
                    " case_id, object_type, object_id, result, version_before, version_after, payload)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    params,
                )
                return
            except sqlite3.IntegrityError:
                if attempt == 1:  # pragma: no cover - 两次都撞 ID（几乎不可能）
                    raise
                # 审计只追加：ID 撞车时换新 ID 继续追加，绝不覆盖已有记录
                audit.id = new_id("AuditEvent")
                params[0] = audit.id

    def audit_trail(
        self,
        *,
        object_id: Optional[str] = None,
        case_id: Optional[str] = None,
        limit: Optional[int] = 200,
    ) -> List[AuditEvent]:
        _validate_limit(limit)
        with self._tx():
            clauses: List[str] = []
            params: List[Any] = []
            if object_id is not None:
                clauses.append("object_id = ?")
                params.append(object_id)
            if case_id is not None:
                clauses.append("case_id = ?")
                params.append(case_id)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            if limit == 0:
                # 契约 v0.3：0 → 空列表。提前返回，别把整张审计表捞出来再丢掉。
                return []
            if limit is not None:
                # 取**最新** limit 条：倒序截断后再翻回正序（与 apply_limit_tail 等价）
                rows = self._conn.execute(
                    "SELECT payload FROM audit_events" + where + " ORDER BY seq DESC LIMIT ?",
                    params + [int(limit)],
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = self._conn.execute(
                    "SELECT payload FROM audit_events" + where + " ORDER BY seq", params
                ).fetchall()
            return [
                AuditEvent.from_dict(json.loads(str(row["payload"])))  # type: ignore[return-value]
                for row in _apply_limit_tail(rows, limit)
            ]

    def _make_audit(
        self,
        obj: SharedObject,
        *,
        action: str,
        actor_kind: str,
        actor_id: str,
        reason: str,
        before: Optional[int],
        after: Optional[int],
        detail: Optional[Dict[str, Any]] = None,
    ) -> AuditEvent:
        return AuditEvent(
            id="",
            at=self.now(),
            action=action,
            actor_kind=ActorKind.coerce(actor_kind),
            actor_id=actor_id,
            case_id=self._resolve_case_id(obj),
            object_type=obj.OBJECT_TYPE,
            object_id=obj.id,
            result="OK",
            version_before=before,
            version_after=after,
            content_hash=getattr(obj, "content_hash", None) or getattr(obj, "sha256", None),
            reason=reason,
            caller=actor_id,
            correlation_id=getattr(obj, "id", None),
            detail=dict(detail or {}),
        )

    # ------------------------------------------------------------------ 事件
    def publish_event(self, event: Event) -> Tuple[Event, bool]:
        with self._tx(write=True):
            if not event.dedupe_key:
                event.dedupe_key = event_dedupe_key(event.source, event.source_event_id)
            existing = self._conn.execute(
                "SELECT payload FROM events WHERE dedupe_key = ?", (event.dedupe_key,)
            ).fetchone()
            if existing is not None:
                return Event.from_dict(json.loads(str(existing["payload"]))), False  # type: ignore[return-value]
            if not event.id:
                event.id = new_id("Event")
            event.received_at = event.received_at or self.now()
            event.occurred_at = event.occurred_at or event.received_at
            event.created_at = event.created_at or event.received_at
            event.updated_at = event.received_at
            self._insert_event_row(event)
            return event, True

    def _insert_event_row(self, event: Event) -> None:
        payload = json.dumps(
            event.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )
        try:
            self._conn.execute(
                "INSERT INTO events(id, dedupe_key, kind, source, source_event_id, occurred_at,"
                " received_at, case_id, conversation_id, processed_at, processing_result,"
                " claimed_by, claimed_at, seq, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " (SELECT COALESCE(MAX(seq), 0) + 1 FROM events), ?)",
                (
                    event.id,
                    event.dedupe_key,
                    event.kind,
                    event.source,
                    event.source_event_id,
                    event.occurred_at,
                    event.received_at,
                    event.case_id,
                    event.conversation_id,
                    event.processed_at,
                    event.processing_result,
                    None,
                    None,
                    payload,
                ),
            )
        except sqlite3.IntegrityError:
            # 去重键撞车：说明并发投递了同一事件，保留先到的那一份（幂等，不报错）
            pass

    def claim_event(self, event_id: str, *, worker: str) -> bool:
        """原子领取事件：`processed_at IS NULL` 且未被占位才成功。"""
        with self._tx(write=True):
            row = self._conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                raise NotFound(f"事件不存在: {event_id}")
            cursor = self._conn.execute(
                "UPDATE events SET claimed_by = ?, claimed_at = ?"
                " WHERE id = ? AND processed_at IS NULL AND claimed_by IS NULL",
                (worker, self.now(), event_id),
            )
            return cursor.rowcount == 1

    def mark_event_processed(self, event_id: str, *, result: str) -> Event:
        with self._tx(write=True):
            row = self._conn.execute("SELECT payload FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                raise NotFound(f"事件不存在: {event_id}")
            event = Event.from_dict(json.loads(str(row["payload"])))
            event.processed_at = self.now()
            event.processing_result = result
            self._conn.execute(
                "UPDATE events SET processed_at = ?, processing_result = ?, payload = ? WHERE id = ?",
                (
                    event.processed_at,
                    result,
                    json.dumps(
                        event.to_dict(),
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    event_id,
                ),
            )
            return event  # type: ignore[return-value]

    def list_events(
        self, *, processed: Optional[bool] = None, limit: Optional[int] = 100
    ) -> List[Event]:
        _validate_limit(limit)
        with self._tx():
            where = ""
            params: List[Any] = []
            if processed is True:
                where = " WHERE processed_at IS NOT NULL"
            elif processed is False:
                where = " WHERE processed_at IS NULL"
            # 定序键 = (received_at, 写入序号)：同一时间戳按 FIFO，且与 id 无关（契约 v0.5）
            if limit is not None:
                rows = self._conn.execute(
                    "SELECT seq, payload FROM events" + where + " ORDER BY received_at, seq LIMIT ?",
                    params + [int(limit)],
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT seq, payload FROM events" + where + " ORDER BY received_at, seq", params
                ).fetchall()
            decoded: List[Tuple[str, int, Event]] = []
            for row in rows:
                event = Event.from_dict(json.loads(str(row["payload"])))
                decoded.append((str(event.received_at or ""), self._sequence_of(row), event))
            decoded.sort(key=lambda item: (item[0], item[1]))
            return [item[2] for item in _apply_limit(decoded, limit)]

    def _emit_state_event(self, obj: SharedObject, before: str, after: str) -> None:
        moment = self.now()
        self.publish_event(
            Event(
                id="",
                kind="STATE_CHANGED",
                source="store",
                source_event_id=f"{obj.OBJECT_TYPE}:{obj.id}:{obj.revision}",
                occurred_at=moment,
                received_at=moment,
                case_id=self._resolve_case_id(obj),
                payload={"object_type": obj.OBJECT_TYPE, "object_id": obj.id, "from": before, "to": after},
            )
        )

    # ------------------------------------------------------------------ 审批与动作
    def approve(self, approval: Approval) -> Approval:
        with self._tx(write=True):
            if not approval.approved_action_digest:
                raise ValidationError("审批必须绑定规范化动作摘要（approved_action_digest）")
            if not approval.auth_evidence:
                raise ValidationError("审批必须由认证控制操作产生（缺少 auth_evidence）")
            approval.created_at_approval = approval.created_at_approval or self.now()
            if approval.expires_at is None:
                approval.expires_at = add_seconds(approval.created_at_approval, DEFAULT_APPROVAL_TTL_SECONDS)
            approval.state = ApprovalState.ACTIVE
            self._supersede_competing_approvals(approval)
            saved = self._put_locked(
                approval,
                expected_revision=None,
                unique=None,
                actor_kind=ActorKind.USER,
                actor_id=approval.approver,
                reason="用户批准",
                audit_action=str(AuditAction.APPROVE),
                audit_detail=None,
            )
            case = self._find_locked("Case", where={"id": approval.case_id}, limit=1)
            if case and approval.id not in case[0].approval_ids:
                case_obj = case[0]
                case_obj.approval_ids.append(approval.id)
                self._put_locked(
                    case_obj,
                    expected_revision=case_obj.revision,
                    unique=None,
                    actor_kind=ActorKind.USER,
                    actor_id=approval.approver,
                    reason="关联审批",
                    audit_action=str(AuditAction.APPROVE),
                    audit_detail=None,
                )
            return saved  # type: ignore[return-value]

    def _supersede_competing_approvals(self, approval: Approval) -> List[str]:
        """同一动作至多一个 ACTIVE 审批（契约 v0.6，CP-20261010-15）。

        旧行为允许同一动作留下多条 ACTIVE 审批：执行侧虽然由 `claim_action` 兜住
        （只有一个赢家），但"这条动作到底由哪张审批授权"变得有歧义，审计上也多出无意义记录。
        现在批准新审批时，同动作上其它 ACTIVE 审批在同一事务内被 INVALIDATED
        （记录与原因都保留，不删除）。
        """
        action_id = getattr(approval, "action_id", None)
        if not action_id:
            return []
        superseded: List[str] = []
        for other in self._find_locked("Approval", where={"action_id": action_id}, limit=None):
            if other.id == approval.id:
                continue
            if ApprovalState.coerce(str(other.state)) != ApprovalState.ACTIVE:
                continue
            self.invalidate_approval(other.id, reason="同一动作已产生新的有效审批，旧审批被取代")
            superseded.append(other.id)
        return superseded

    def invalidate_approval(self, approval_id: str, *, reason: str, state: str = "INVALIDATED") -> Approval:
        with self._tx(write=True):
            approval = self._get_locked("Approval", approval_id)
            target = "REVOKED" if state == "REVOKED" else str(state)
            tr.machine_for("Approval").require(
                str(approval.state), target, object_type="Approval", object_id=approval_id
            )
            approval.state = ApprovalState.coerce(target)  # type: ignore[assignment]
            if target == "REVOKED":
                approval.revoked_reason = reason
            else:
                approval.invalidated_reason = reason
            return self._put_locked(
                approval,
                expected_revision=approval.revision,
                unique=None,
                actor_kind=ActorKind.SYSTEM,
                actor_id="system",
                reason=reason,
                audit_action=str(AuditAction.REVOKE) if target == "REVOKED" else str(AuditAction.CORRECTION),
                audit_detail={"approval_state": target},
            )  # type: ignore[return-value]

    def claim_action(
        self,
        action_id: str,
        *,
        worker: str,
        attempt_id: Optional[str] = None,
        now: Optional[str] = None,
    ) -> ActionAttempt:
        """原子领取：校验 + 预留一次性 Approval + 写 IN_FLIGHT + 建 attempt（SPEC §21.2）。

        整个"校验—占位—写状态"在**一个事务 + 一把锁**里完成，
        调用方不得先查询状态再标记（禁止 TOCTOU）。
        """
        with self._tx(write=True):
            moment = now or self.now()
            action = self._get_locked("ExternalAction", action_id)
            if str(action.state) != str(ActionState.AUTHORIZED):
                raise ActionNotClaimable(
                    f"动作当前状态不可领取: {action.state}",
                    detail={"action_id": action_id, "state": str(action.state)},
                )
            if action.claimed_by:
                raise ActionNotClaimable(
                    "动作已被其他执行器占位，禁止重复外呼",
                    detail={
                        "action_id": action_id,
                        "claimed_by": action.claimed_by,
                        "claimed_at": action.claimed_at,
                    },
                )
            if not action.approval_id:
                raise ActionNotClaimable("动作没有绑定有效审批，禁止外呼", detail={"action_id": action_id})
            approval = self._get_locked("Approval", action.approval_id)
            if str(approval.state) != str(ApprovalState.ACTIVE):
                raise ApprovalInvalid(
                    "审批已被消费、撤销或失效",
                    detail={
                        "approval_id": approval.id,
                        "state": str(approval.state),
                        "reason": approval.revoked_reason or approval.invalidated_reason,
                    },
                )
            if approval.expires_at and is_expired(approval.expires_at, now=moment):
                raise ApprovalInvalid(
                    "审批已过期", detail={"approval_id": approval.id, "expires_at": approval.expires_at}
                )
            if approval.approved_action_digest != action.summary_digest:
                raise ApprovalInvalid(
                    "审批绑定的内容与当前动作不一致",
                    detail={
                        "approval_id": approval.id,
                        "approved": approval.approved_action_digest,
                        "current": action.summary_digest,
                    },
                )

            new_attempt_id = attempt_id or new_id("ActionAttempt")
            attempt = ActionAttempt(
                id=new_attempt_id,
                action_id=action_id,
                attempt_no=len(action.attempt_ids) + 1,
                request_idempotency_key=action.idempotency_key,
                state="IN_FLIGHT",
                started_at=moment,
                executor_node=worker,
            )
            self._put_locked(
                attempt,
                expected_revision=None,
                unique=None,
                actor_kind=ActorKind.SYSTEM,
                actor_id=worker,
                reason="原子领取动作",
                audit_action=str(AuditAction.EXECUTE),
                audit_detail=None,
            )

            # 预留并立即消费一次性审批：预留后不得再授权第二个动作
            approval.state = ApprovalState.CONSUMED
            approval.consumed_by_attempt_id = new_attempt_id
            approval.reserved_at = moment
            self._put_locked(
                approval,
                expected_revision=approval.revision,
                unique=None,
                actor_kind=ActorKind.SYSTEM,
                actor_id=worker,
                reason="审批被原子预留",
                audit_action=str(AuditAction.EXECUTE),
                audit_detail={"attempt_id": new_attempt_id},
            )

            action.state = ActionState.IN_FLIGHT
            action.claimed_by = worker
            action.claimed_at = moment
            action.attempt_ids.append(new_attempt_id)
            saved = self._put_locked(
                action,
                expected_revision=action.revision,
                unique=None,
                actor_kind=ActorKind.SYSTEM,
                actor_id=worker,
                reason="动作进入 IN_FLIGHT",
                audit_action=str(AuditAction.EXECUTE),
                audit_detail={"attempt_id": new_attempt_id, "idempotency_key": action.idempotency_key},
            )
            self._touch_case(saved, worker, note="执行外部动作")
            return attempt

    def record_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        external_response: Optional[Dict[str, Any]] = None,
        external_ref: Optional[str] = None,
        failure_class: Optional[str] = None,
        error_text: Optional[str] = None,
    ) -> ActionAttempt:
        with self._tx(write=True):
            attempt = self._get_locked("ActionAttempt", attempt_id)
            action = self._get_locked("ExternalAction", attempt.action_id)
            target = str(state)
            tr.machine_for("ExternalAction").require(
                str(action.state), target, object_type="ExternalAction", object_id=action.id
            )
            moment = self.now()
            attempt.state = target
            attempt.finished_at = moment
            attempt.external_response = dict(external_response or {})
            attempt.external_ref = external_ref
            attempt.failure_class = failure_class
            attempt.error_text = error_text
            saved_attempt = self._put_locked(
                attempt,
                expected_revision=attempt.revision,
                unique=None,
                actor_kind=ActorKind.SYSTEM,
                actor_id=attempt.executor_node or "executor",
                reason=f"尝试结果: {target}",
                audit_action=str(AuditAction.EXECUTE),
                audit_detail={"external_ref": external_ref, "failure_class": failure_class},
            )

            action.state = ActionState.coerce(target)  # type: ignore[assignment]
            action.result_class = target
            if target != "IN_FLIGHT":
                action.finalized_at = moment
                if target == "FAILED_SAFE":
                    action.claimed_by = None
            saved_action = self._put_locked(
                action,
                expected_revision=action.revision,
                unique=None,
                actor_kind=ActorKind.SYSTEM,
                actor_id=attempt.executor_node or "executor",
                reason=f"动作结果: {target}",
                audit_action=str(AuditAction.EXECUTE),
                audit_detail={"attempt_id": attempt_id, "failure_class": failure_class},
            )
            self._sync_case_after_action(saved_action)
            return saved_attempt  # type: ignore[return-value]

    def demote_in_flight_to_unknown(self, *, reason: str = "进程重启时动作仍为 IN_FLIGHT") -> List[str]:
        """重启恢复：IN_FLIGHT 先转 UNKNOWN 并查证，绝不按租约到期直接重发（SPEC §21.2）。"""
        demoted: List[str] = []
        with self._tx(write=True):
            actions = self._find_locked("ExternalAction", where={"state": "IN_FLIGHT"}, limit=1000)
            for action in actions:
                attempts = self._find_locked(
                    "ActionAttempt", where={"action_id": action.id}, limit=100
                )
                for attempt in attempts:
                    if str(attempt.state) == "IN_FLIGHT":
                        self.record_attempt(
                            attempt.id,
                            state="UNKNOWN",
                            external_response={"note": reason},
                            failure_class="EXTERNAL_UNKNOWN",
                            error_text=reason,
                        )
                demoted.append(action.id)
        return demoted

    def _sync_case_after_action(self, action: SharedObject) -> None:
        case = self._find_locked("Case", where={"id": action.case_id}, limit=1)
        if not case or str(case[0].state) != "EXECUTING":
            return
        target = {"SUCCEEDED": "SUCCEEDED", "FAILED_SAFE": "FAILED", "UNKNOWN": "UNKNOWN"}.get(
            str(action.state)
        )
        if target:
            self.transition(
                "Case",
                case[0].id,
                target,
                actor_kind=ActorKind.SYSTEM,
                actor_id="executor",
                reason="动作结果回写事务状态",
            )

    def _touch_case(self, action: SharedObject, actor_id: str, *, note: str) -> None:
        case = self._find_locked("Case", where={"id": action.case_id}, limit=1)
        if not case:
            return
        if str(case[0].state) in ("APPROVED", "READY_FOR_REVIEW"):
            self.transition(
                "Case", case[0].id, "EXECUTING", actor_kind=ActorKind.SYSTEM, actor_id=actor_id, reason=note
            )

    # ------------------------------------------------------------------ 执行记录
    def record_execution(self, execution: Execution) -> Execution:
        with self._tx(write=True):
            if not execution.id:
                execution.id = new_id("Execution")
            existing = self._row_by_key("Execution", execution.id)
            if existing is None:
                return self._put_locked(
                    execution,
                    expected_revision=None,
                    unique=None,
                    actor_kind=ActorKind.AGENT,
                    actor_id="agent",
                    reason="记录执行",
                    audit_action=str(AuditAction.WRITE),
                    audit_detail=None,
                )  # type: ignore[return-value]
            return self._put_locked(
                execution,
                expected_revision=int(existing["revision"]),
                unique=None,
                actor_kind=ActorKind.AGENT,
                actor_id="agent",
                reason="执行结束",
                audit_action=str(AuditAction.WRITE),
                audit_detail=None,
            )  # type: ignore[return-value]

    # ------------------------------------------------------------------ 恢复与观测
    def recovery_scan(self) -> RecoveryScan:
        with self._tx():
            scan = RecoveryScan()
            for case in self._find_locked("Case", where=None, limit=10000):
                state = str(case.state)
                if state in OPEN_CASE_STATES:
                    scan.open_case_ids.append(case.id)
                if state in ("WAITING_FOR_USER", "UNKNOWN"):
                    scan.waiting_user_case_ids.append(case.id)
            scan.pending_question_ids = [
                q.id for q in self._find_locked("Question", where={"state": "OPEN"}, limit=10000)
            ]
            scan.pending_approval_ids = [
                a.id for a in self._find_locked("Approval", where={"state": "ACTIVE"}, limit=10000)
            ]
            scan.in_flight_action_ids = [
                a.id for a in self._find_locked("ExternalAction", where={"state": "IN_FLIGHT"}, limit=10000)
            ]
            scan.unknown_action_ids = [
                a.id for a in self._find_locked("ExternalAction", where={"state": "UNKNOWN"}, limit=10000)
            ]
            scan.pending_index_document_ids = [
                d.id
                for d in self._find_locked("SourceDocument", where=None, limit=10000)
                if str(d.index_state) in ("PENDING", "STALE", "FAILED", "REBUILD_REQUIRED")
            ]
            scan.pending_archive_job_ids = [
                j.id
                for j in self._find_locked("ArchiveJob", where=None, limit=10000)
                if str(j.state)
                in ("PENDING", "UPLOADING", "PARTIAL", "VERIFYING", "PENDING_VERIFICATION", "FAILED")
            ]
            scan.failed_restore_job_ids = [
                r.id for r in self._find_locked("RestoreJob", where={"state": "FAILED"}, limit=10000)
            ]
            return scan

    def stats(self) -> Dict[str, int]:
        with self._tx():
            counts: Dict[str, int] = {}
            total = 0
            for row in self._conn.execute(
                "SELECT object_type, COUNT(*) AS n FROM objects GROUP BY object_type"
            ).fetchall():
                counts[str(row["object_type"])] = int(row["n"])
                total += int(row["n"])
            counts["AuditEvent"] = int(
                self._conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            )
            counts["Event"] = int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            counts["_total"] = total
            return counts

    # 供测试/运维使用：不经过解码的原始计数
    def raw_counts(self) -> Dict[str, int]:
        with self._tx():
            counts: Dict[str, int] = {}
            for table in ("objects", "unique_keys", "audit_events", "events"):
                counts[table] = int(
                    self._conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                )
            return counts


def open_store(path: str, **kwargs: Any) -> SqliteStore:
    """显式路径打开存储（生产入口）。"""
    return SqliteStore(path=path, **kwargs)


__all__ = [
    "DIRECT_COLUMNS",
    "OBJECT_TYPES",
    "OPEN_CASE_STATES",
    "STATE_FIELD_BY_TYPE",
    "SqliteStore",
    "open_store",
]
