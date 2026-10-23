"""schema 版本表与迁移：可发现、幂等、可原地升级、失败整体回滚。

契约依据：SPEC §21.1（状态/版本独立持久化）与 MVP-0 交付要求
"schema 版本表与迁移"。运维必须能一眼看出"设备上的库是什么版本"。
"""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from unittest import mock

from asistent.contracts import Case, reference
from asistent.contracts.store_api import StoreProtocol
from asistent.storage import migrations
from asistent.storage.search_index import TABLE as SEARCH_TABLE
from asistent.storage.sqlite_store import OBJECT_TYPES, OPEN_CASE_STATES, STATE_FIELD_BY_TYPE, SqliteStore
from tests.contracts.helpers import make_case

from .support import SqliteStoreTestCase

#: 当前期望的目标版本（写成表达式而不是数字 4：再加迁移时测试不用改）
TARGET = migrations.SCHEMA_TARGET_VERSION
ALL_VERSIONS = list(range(1, TARGET + 1))


class SchemaVersionTest(SqliteStoreTestCase):
    def test_fresh_database_gets_full_schema_and_ledger(self) -> None:
        store = self.open_store()
        self.assertEqual(ALL_VERSIONS, store.migrations_applied)
        self.assertEqual(migrations.SCHEMA_TARGET_VERSION, store.schema_version())
        self.assertEqual(TARGET, store.schema_version())

        ledger = migrations.applied_migrations(store.connection)
        self.assertEqual(ALL_VERSIONS, [row[0] for row in ledger])
        for _version, note, applied_at in ledger:
            self.assertTrue(note, "每个版本必须记账备注")
            self.assertTrue(applied_at)
        self.assertIn("FTS5", ledger[2][1])

        tables = migrations.table_names(store.connection)
        for expected in ("schema_migrations", "objects", "unique_keys", "audit_events", "events", "store_meta", SEARCH_TABLE):
            self.assertIn(expected, tables)
        self.assertEqual(
            str(TARGET),
            str(store.connection.execute("SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()[0]),
        )
        # v4：两张表都补上了写入序号列（定序与写入顺序一致，契约 v0.5）
        for table in ("objects", "events"):
            columns = [str(row[1]) for row in store.connection.execute("PRAGMA table_info(%s)" % table).fetchall()]
            self.assertIn("seq", columns, "%s 必须有写入序号列" % table)

    def test_reopen_does_not_reapply_migrations(self) -> None:
        store = self.open_store()
        reopened = self.restart(store)
        self.assertEqual([], reopened.migrations_applied)
        self.assertEqual(TARGET, reopened.schema_version())
        self.assertEqual(TARGET, len(migrations.applied_migrations(reopened.connection)))

    def test_old_database_is_upgraded_in_place_without_losing_data(self) -> None:
        """模拟"设备上是一个 v1 旧库（已有数据）"：重开时必须原地补齐后续版本，且数据不丢。"""
        # 1) 建一个真正的 v1 库（只跑到迁移 1），并用手写 SQL 塞进去一条"当年写的"对象
        connection = migrations.connect(self.db_path)
        migrations.apply_migrations(connection, upto=1)
        self.assertEqual(1, migrations.current_schema_version(connection))

        legacy = Case(goal="升级前创建的事务")
        legacy.id = "case_legacy_1"
        legacy.created_at = "2026-01-01T00:00:00.000Z"
        legacy.updated_at = legacy.created_at
        legacy.state = "NEW"
        connection.execute(
            "INSERT INTO objects(object_type, id, class_name, revision, created_at, updated_at,"
            " state, case_id, index_state, archive_state, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "Case",
                legacy.id,
                "Case",
                1,
                legacy.created_at,
                legacy.updated_at,
                "NEW",
                None,
                None,
                None,
                json.dumps(legacy.to_dict(), sort_keys=True, ensure_ascii=False),
            ),
        )
        connection.close()

        # 2) 用存储打开 → 原地升级到当前版本
        upgraded = self.open_store()
        self.assertEqual(list(range(2, TARGET + 1)), upgraded.migrations_applied)
        self.assertEqual(TARGET, upgraded.schema_version())
        self.assertTrue(upgraded.search_index_ready())
        self.assert_object_present("trg_audit_append_only_delete", upgraded)

        # 3) 老数据必须还在，可读、可查、且拿到了写入序号（按 rowid 回填）
        restored = upgraded.get("Case", "case_legacy_1")
        self.assertEqual("升级前创建的事务", restored.goal)
        self.assertEqual(legacy.created_at, restored.created_at)
        backfilled = upgraded.connection.execute(
            "SELECT seq FROM objects WHERE object_type = 'Case' AND id = 'case_legacy_1'"
        ).fetchone()
        self.assertIsNotNone(backfilled[0], "v4 迁移必须给历史行回填 seq")
        self.assertEqual([restored.id], [item.id for item in upgraded.find("Case")])
        self.assertEqual([restored.id], [item.id for item in upgraded.find("Case", where={"state": "NEW"})])
        self.assertEqual([], upgraded.find("Case", where={"state": "TRIAGED"}))

        # 4) 升级后的库仍然是 WAL，列索引建好了，按状态预筛能直接查到数据
        self.assertTrue(upgraded.wal_enabled())
        self.assert_object_present("idx_objects_type_state", upgraded)
        self.assert_object_present("idx_objects_type_created_seq", upgraded)

        # 5) 升级后新写入的序号必须排在历史行之后（序号单调）
        fresh = make_case(upgraded, goal="升级后创建的事务")
        fresh_seq = upgraded.connection.execute(
            "SELECT seq FROM objects WHERE object_type = 'Case' AND id = ?", (fresh.id,)
        ).fetchone()[0]
        self.assertGreater(int(fresh_seq), int(backfilled[0]))

    def test_failed_migration_rolls_back_and_keeps_ledger_consistent(self) -> None:
        """迁移必须整体成败：坏迁移不许留下半截 schema，也不许记账。"""
        store = self.open_store()
        store.close()

        def broken(_conn: sqlite3.Connection):
            _conn.execute("CREATE TABLE should_not_exist(x INTEGER)")
            raise RuntimeError("模拟迁移中途失败")

        next_version = TARGET + 1
        broken_migration = migrations.Migration(next_version, "坏迁移", broken)
        with mock.patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS + (broken_migration,)):
            with mock.patch.object(migrations, "SCHEMA_TARGET_VERSION", next_version):
                connection = migrations.connect(self.db_path)
                try:
                    with self.assertRaises(RuntimeError):
                        migrations.apply_migrations(connection)
                    self.assertEqual(TARGET, migrations.current_schema_version(connection))
                    self.assertNotIn("should_not_exist", migrations.table_names(connection))
                finally:
                    connection.close()

        reopened = self.open_store()
        self.assertEqual(TARGET, reopened.schema_version())
        self.assertEqual(TARGET, len(migrations.applied_migrations(reopened.connection)))
        self.assertEqual([], reopened.migrations_applied)

    def test_schema_version_of_empty_and_foreign_database(self) -> None:
        foreign = os.path.join(self.tmpdir, "foreign.sqlite3")
        connection = sqlite3.connect(foreign)
        connection.execute("CREATE TABLE unrelated(x INTEGER)")
        self.assertEqual(0, migrations.current_schema_version(connection))
        self.assertEqual([], migrations.applied_migrations(connection))
        connection.close()

        store = self.open_store(path=foreign)
        self.assertEqual(ALL_VERSIONS, store.migrations_applied, "非本项目的库也要能安全升级")
        self.assertEqual(TARGET, store.schema_version())


class StoreConsistencyTest(SqliteStoreTestCase):
    def test_sqlite_store_satisfies_frozen_protocol(self) -> None:
        store = self.open_store()
        self.assertIsInstance(store, StoreProtocol, "必须满足冻结契约（runtime_checkable Protocol）")
        missing = [
            name
            for name in dir(StoreProtocol)
            if not name.startswith("_") and not callable(getattr(SqliteStore, name, None))
        ]
        self.assertEqual([], missing, "契约方法缺失: %r" % (missing,))

    def test_semantics_constants_do_not_drift_from_reference(self) -> None:
        """数据库语义常量与参考实现必须逐值相等，防止两套实现悄悄分叉。"""
        self.assertEqual(reference.OPEN_CASE_STATES, OPEN_CASE_STATES)
        self.assertEqual(reference.STATE_FIELD_BY_TYPE, STATE_FIELD_BY_TYPE)

    def test_object_type_registry_covers_all_contract_objects(self) -> None:
        from asistent.contracts import objects as contract_objects

        expected = {
            getattr(contract_objects, name).OBJECT_TYPE
            for name in contract_objects.__all__
            if isinstance(getattr(contract_objects, name), type)
            and hasattr(getattr(contract_objects, name), "OBJECT_TYPE")
        }
        self.assertEqual(expected, set(OBJECT_TYPES), "对象注册表必须覆盖契约里的全部对象类型")

    def test_store_factory_seam_selects_sqlite(self) -> None:
        """激活点：ASISTENT_STORE=sqlite → SqliteStore（tests/contracts/helpers.py 未改动）。"""
        from tests.contracts.helpers import store_factory

        with mock.patch.dict(os.environ, {"ASISTENT_STORE": "sqlite"}):
            store = store_factory()
        self.assertIsInstance(store, SqliteStore)
        self.addCleanup(store.close)
        self.assertTrue(store.wal_enabled())

        with mock.patch.dict(os.environ, {"ASISTENT_STORE": "不存在"}):
            with self.assertRaises(RuntimeError):
                store_factory()

        with mock.patch.dict(os.environ, {"ASISTENT_STORE": "reference"}):
            from asistent.contracts import ReferenceStore

            self.assertIsInstance(store_factory(), ReferenceStore)

    def test_default_constructor_uses_private_temp_directory(self) -> None:
        store = SqliteStore()
        self.addCleanup(store.close)
        self.assertTrue(os.path.isdir(os.path.dirname(store.path)))
        self.assertEqual("asistent.sqlite3", os.path.basename(store.path))
        self.assertTrue(store.wal_enabled())
        self.assertEqual(TARGET, store.schema_version())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
