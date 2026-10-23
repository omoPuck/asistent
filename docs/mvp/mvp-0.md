# MVP-0：SQLite 存储实现（`asistent/storage/**`）

- 分支：`mvp/0-sqlite-storage`（worktree：`D:\dsh\个人助手\.worktrees\mvp-0-sqlite`）
- 起点：`e110f25`（contracts-v0.2 冻结点）；已依次对齐 **contracts-v0.3**（`efd2e04`，合入 `0fb0df6`）、
  **v0.4/v0.5**（先到 `8b24283`，定序契约 `e8e6123`，由 Lead 合并为 `231bead`）
- 交付物：`asistent/storage/{__init__,sqlite_store,migrations,search_index}.py`、`tests/storage/**`、本文档
- 结论：**全仓测试在 SQLite 后端下 314/314 全绿**（契约 106、存储 63、MVP-1 99、MVP-2 46）；
  SQLite 专属测试 63/63 全绿；设备 `/usr/bin/python3` = **3.10.12** 复跑同样全绿；
  纯标准库（零第三方依赖）。定序契约（v0.5）连跑 10 次稳定全绿。

---

## 1. 目标与验收对照

| 验收项 | 状态 | 证据位置 |
| --- | --- | --- |
| `ASISTENT_STORE=sqlite python -m unittest discover -s tests -t .` 全绿（contracts-v0.5） | ✅ **314/314**（契约 106 / 存储 63 / MVP-1 99 / MVP-2 46） | §4.6 |
| `ASISTENT_STORE=reference …` 同样全绿 | ✅ 314/314 | §4.6 |
| **定序与写入顺序一致**（v0.5）：同时间戳按写入顺序、稳定、重开一致、更新不移动位置、不用 id 平局 | ✅ 持久化 `seq` + `order_key`-等价排序；连跑 10 次全绿 | §2.7、§4.6 |
| `limit` 语义：`None` 不限 / `>=0` 最多 N / `<0` 抛 `ValidationError` | ✅ 三个入口单一裁决点 + 边界测试 | §2.6、§4.5 |
| `tests/contracts/**`、`asistent/contracts/**` 一行未改 | ✅ `git diff --name-only main -- …` 为空 | §4.3、§4.6 |
| `tests/storage/**` 覆盖 SQLite 特有行为 | ✅ 63 项 | §4.2、§4.6 |
| Python 3.10 兼容（设备 3.10.12 复跑） | ✅ 314/314（两套后端）+ 63/63 | §4.4、§4.6 |
| 纯标准库、零第三方依赖 | ✅ AST 静态检查 + 设备无 pip 可跑 | §4.4、§5.6 |
| 结果未知语义：`IN_FLIGHT` 重启不得当成功 | ✅ `demote_in_flight_to_unknown` | §2.4、§4.2 |

---

## 2. 设计

### 2.1 分层

```
asistent/storage/
  sqlite_store.py    # SqliteStore：StoreProtocol 的完整实现（唯一对外类）
  migrations.py      # schema 账本 + 只向前追加的迁移 + connect() 统一 pragma
  search_index.py    # FTS5 检索表原语（建表/入库/查询/重建）
```

`SqliteStore` **只依赖 `asistent.contracts`（对象/状态机/错误/时间契约）**，不 import
`ReferenceStore`；两套实现共享的语义常量（`OPEN_CASE_STATES`、`STATE_FIELD_BY_TYPE`）
在 `sqlite_store.py` 里独立声明，并由 `test_semantics_constants_do_not_drift_from_reference`
逐值比对，防止分叉。

### 2.2 并发与事务（最关键的设计决策）

实测事实：本机 3.12 的 `sqlite3.threadsafety` = 3，但**设备 3.10 恒为 1**，默认连接不允许跨线程使用；
而契约要求"动作领取是一个原子调用"（§21.2）且测试用 6 线程并发抢同一动作。因此：

1. 连接以 `sqlite3.connect(..., isolation_level=None, check_same_thread=False)` 打开；
2. 所有公开方法都在同一把 `threading.RLock` 下执行，事务边界由我们显式写出
   （写入路径 `BEGIN IMMEDIATE`，读取路径 `BEGIN`）；
3. `_tx()` 支持**可重入**：`claim_action` 内部的 `put/get/find` 复用最外层事务，
   任一层抛异常都由最外层整体 `ROLLBACK`；
4. 跨进程靠 SQLite 自己的写锁 + `PRAGMA busy_timeout=30s`：`BEGIN IMMEDIATE` 在
   "读—判断—写"之前就拿到写锁，因此不存在 TOCTOU，也不会出现"先查询、后标记"。

副作用（已知限制）：同一进程内写是串行的。对单机个人助手是合适的取舍；
它不是"高并发服务器存储"，这一点写进 §5 而不是藏着。

### 2.3 schema 与迁移

`schema_migrations` 是**只追加**的版本账本，当前目标版本 **4**：

| 版本 | 内容 |
| --- | --- |
| v1 | `objects`（对象 + 派生列）、`unique_keys`（幂等键）、`audit_events`（审计）、`events`（事件）、`store_meta` |
| v2 | 列索引（type+state / type+case_id / type+index_state / created）+ 审计只追加触发器 |
| v3 | FTS5 检索表 `search_index`（DDL 取自 `asistent.contracts.search.FTS_DDL`） |
| v4 | **写入序号 `seq`**（`objects`/`events` 各加一列 + 索引 + 历史行按 `rowid` 回填），定序与写入顺序一致（契约 v0.5） |

- 每个迁移在**单个事务**内执行：失败整体回滚，不留半截 schema，也不记账（§4.2 有专项测试）；
- 打开库时自动补齐缺失迁移；重开不会重复应用；
- 旧库原地升级：从真正的 v1 库（已有数据）升级到 v4，数据不丢、历史行拿到回填序号
  （测试见 `tests/storage/test_sqlite_schema.py::test_old_database_is_upgraded_in_place_without_losing_data`）；
- **v1 建表语句永不改写**，v4 对"全新建库"和"旧库升级"一视同仁地 `ALTER + 回填`，
  因此两种路径得到完全相同的 schema（避免"新库/老库结构不一致"这类隐性分叉）；
- FTS5 未编译时 v3 只写备注、不让整库打不开；但 `search()` 会**明确抛
  `SearchIndexUnavailable`**，不静默降级为全表扫描（`asistent.contracts.search` 的契约要求）。

表结构（v1 要点 + v4 增量）：

```
objects(object_type, id, class_name, revision, created_at, updated_at,
        state, case_id, index_state, archive_state, payload)   PK(object_type, id)
        + seq INTEGER            -- v4：写入序号，只在首次插入时分配
unique_keys(namespace, value, object_type, object_id)          PK(namespace, value)
audit_events(seq AUTOINCREMENT, id UNIQUE, at, action, actor_kind, actor_id,
             execution_id, case_id, object_type, object_id, result,
             version_before, version_after, payload)
events(id PK, dedupe_key UNIQUE, kind, source, source_event_id, occurred_at,
       received_at, case_id, conversation_id, processed_at, processing_result,
       claimed_by, claimed_at, payload)
       + seq INTEGER            -- v4：写入序号（FIFO 平局判据）
store_meta(key PK, value)
```

`payload` 是契约对象的 `to_dict()` 规范化 JSON（`sort_keys` + `ensure_ascii=False`），
读回时按 `OBJECT_TYPE → dataclass` 注册表用 `from_dict()` 还原，字段类型/枚举由契约自己的
`_coerce` 处理；派生列（`state`/`case_id`/`index_state`/`archive_state`）**只在对象真的声明了该字段时才写**，
从而与参考实现的 `str(getattr(obj, field, None))` 比较语义严格一致。

### 2.4 契约语义 → 实现机制 → 测试

| 契约要求（SPEC） | 实现机制 | 测试 |
| --- | --- | --- |
| 更新必须带 `expected_revision`，旧版本拒绝（§21.1） | 事务内比对行内 revision；不一致抛 `RevisionConflict`（含 expected/actual） | `tests/contracts/test_storage_contract.py::test_stale_update_is_rejected`、`tests/storage/...::test_cas_conflict_is_detected_and_stored_value_is_preserved` |
| 幂等键只允许一份对象（§7.3） | `unique_keys` 主键 + 事务内先查后插 | `test_unique_key_prevents_duplicate_object`、`test_parallel_handlers_create_single_case` |
| 事件按去重键幂等（§21.1） | `events.dedupe_key` UNIQUE + `INSERT` 冲突返回既有对象 | `test_event_dedupe_on_repeated_delivery`、`test_event_dedupe_and_claim_survive_reopen` |
| 事件领取独占 | 单条 `UPDATE ... WHERE processed_at IS NULL AND claimed_by IS NULL`（原子） | `test_event_claim_is_exclusive` |
| 审计只追加（§11.3） | `audit_events` 上 BEFORE UPDATE/DELETE 触发器 `RAISE(ABORT)` | `test_audit_rows_cannot_be_updated_or_deleted_by_sql`、`test_audit_is_append_only` |
| 动作领取是一个原子调用（§21.2） | 单事务：校验 → 建 attempt → 消费审批 → 写 `IN_FLIGHT` | `test_concurrent_claim_has_single_winner`、`test_failed_claim_leaves_no_partial_write` |
| 结果未知不得自动重发（§21.2） | `demote_in_flight_to_unknown()` 只做 `IN_FLIGHT → UNKNOWN` 并记 `EXTERNAL_UNKNOWN` | `test_in_flight_demoted_to_unknown_not_retried`、`..._after_crash_is_demoted_to_unknown_not_success` |
| 状态机拒绝非法转换（§16） | 复用 `asistent.contracts.transitions`，非法即 `IllegalTransition` | `test_illegal_state_transition_is_rejected`、`test_case_cannot_be_cancelled_after_irreversible_action` |
| 重启恢复要能扫出待办（§13.3） | `recovery_scan()` 按列预筛 + 契约常量过滤 | `test_recovery_scan_finds_everything_that_needs_attention` |
| 索引状态独立持久化（§6.2） | `index_state`/`index_error`/`last_indexed_at` 落库 | `test_index_state_and_error_survive_reopen` |
| 中文检索不得静默失效（§6.2） | 入库走 `normalize_for_index()`，查询走 `to_fts_query()`，无效查询返回空 | `test_cjk_substring_search_works`、`test_invalid_query_returns_empty_and_never_everything` |
| `limit` 语义显式化（v0.3，CP-20261010-03） | `_validate_limit`/`_apply_limit`/`_apply_limit_tail` 单点裁决，三入口共用 | `tests/contracts/...::test_limit_semantics_are_explicit`、`tests/storage/test_limit_semantics.py`（9 项） |
| **定序 = (时间戳, 写入序号)**（v0.5，CP-20261010-11） | 持久化 `seq` 列（只写一次、更新不改）+ `ORDER BY 时间戳, seq` | `tests/contracts/...::StoreV04ContractTest`（9 项）、`tests/storage/test_sqlite_ordering.py`（10 项） |

### 2.5 激活点（未改冻结文件）

`tests/contracts/helpers.py` 的 `store_factory()` 未做任何改动，`ASISTENT_STORE=sqlite`
即 `from asistent.storage.sqlite_store import SqliteStore`；构造签名与 `ReferenceStore` 对齐
（关键字参数，`clock` 可注入），因此同一组测试对两个实现各跑一遍。
`test_store_factory_seam_selects_sqlite` 直接断言"激活点确实生效"。

### 2.6 `limit` 语义（契约 v0.3 对齐，task-4）

契约 v0.3 把 `limit` 的裁决权收到一个点：`None` → 不限制；`>= 0` → 最多 limit 条（`0` → 空列表）；
`< 0` → `ValidationError`（**不允许**靠 Python 负切片隐式"从尾部取"）。`audit_trail` 取**最新** limit 条、
但返回顺序仍是正序；`find`/`list_events` 取**最早** limit 条。

实现上刻意复制而不是 import 参考实现（存储层不依赖内存参考实现），防漂移由
`tests/storage/test_limit_semantics.py` 逐例把结果与契约自己的
`asistent.contracts.reference.apply_limit` / `apply_limit_tail` 对照：

- `_validate_limit()`：负数在**打开事务之前**就报错（空库也报错，不做任何数据库工作）；
- `find`：SQL 层 `LIMIT` 只在 `limit is not None and limit >= 0` 时下推（`0` → `LIMIT 0` 直接空），
  最终仍由 `_apply_limit()` 裁决；
- `list_events`：同上；
- `audit_trail`：`limit == 0` 提前返回空（不把整张审计表捞出来再丢掉）；`limit > 0` 走
  `ORDER BY seq DESC LIMIT ?` 再翻回正序（与 `apply_limit_tail` 等价）；`limit is None` 取全部正序。

顺带修掉一个同源的偏差：`find` 只有在 `where` 的**每个**键都能下推 SQL 时才用 SQL `LIMIT`；
只要有键要在 Python 里过滤，就先取候选、过滤完再 `apply_limit`——否则"SQL 先截断、Python 再过滤"
会少返回结果，与参考实现不一致（`test_sql_limit_does_not_truncate_before_python_filtering`）。

### 2.7 定序：写入序号（契约 v0.5 对齐，task-7）

**真实缺陷**（Lead 在 main 上复现：单类连跑 6 次红 5 次）：`created_at` 只有毫秒精度，
快速连写经常落在同一毫秒；两套实现原先都按 `(created_at, id)` 排序，而 `id` 是随机的 →
**平局判据随机**。后果不是"偶发抖动"，而是语义没定死：分页漏项/重复、`limit=1` 随机取到
不同对象、同一份数据在不同后端给出不同答案。参考实现因为 Python 慢、常跨毫秒而掩盖了它。

契约 v0.5（CP-20261010-11）把定序键定死为 `(时间戳, 写入序号)`，语义参照是参考实现的
`order_key()` + `_write_seq`/`_seq_counter`（本实现**不 import**，照抄语义 + parity 测试锁漂移）。

实现：

- **持久化序号**：`objects`/`events` 各加一列 `seq`（迁移 v4），插入时取
  `(SELECT COALESCE(MAX(seq), 0) + 1 FROM 本表)` —— 与行插入同一条语句，天然在写事务里，
  且**带索引**（`idx_objects_seq`/`idx_events_seq`），所以是 O(log n) 不是全表扫；
- **只在插入时分配**：更新语句的列清单里没有 `seq`，因此"改内容"不会把对象挪到队尾
  （契约要求"更新不得改变对象已有的定序位置"）；
- **不用 `INSERT OR REPLACE`**：那会删旧行再插新行，把序号重置到队尾（对象表本来就是
  `INSERT` + `UPDATE` 两条语句，事件表用 `INSERT` + 唯一键冲突兜底）；
- **排序键**：`find` → `ORDER BY created_at, seq`；`list_events` → `ORDER BY received_at, seq`；
  `audit_trail` 保持 `seq DESC`（审计表本来就有自增 `seq`）；Python 侧用同一组键再排一次，
  保证"SQL 顺序"与"语义顺序"不可能分叉；
- **id 完全退出定序**：不是"换个平局判据"，是把随机量从排序键里删掉；
- **历史数据**：v4 迁移按 `rowid` 回填（`rowid` 就是历史行的插入顺序），
  新行的 `MAX(seq)+1` 必然大于所有回填值 → 升级不会打乱既有顺序。

---

## 3. 文件清单

| 文件 | 说明 |
| --- | --- |
| `asistent/storage/__init__.py` | 对外导出 `SqliteStore` / `open_store` / `migrations` / `search_index` |
| `asistent/storage/sqlite_store.py` | `SqliteStore`：协议 21 个方法 + `get_by_unique`/`now`/`schema_version`/`wal_enabled`/`checkpoint`/`search`/`index_document`/`raw_counts` |
| `asistent/storage/migrations.py` | `connect()`、`apply_migrations()`、`initialize()`、账本查询、v1–v3 迁移 |
| `asistent/storage/search_index.py` | FTS5 建表/入库/查询/重建原语 |
| `tests/storage/support.py` | 库文件目录夹具、跨进程脚本执行器 |
| `tests/storage/test_sqlite_durability.py` | 8 项：WAL、重启/跨进程持久、索引状态、幂等键、事件、IN_FLIGHT→UNKNOWN |
| `tests/storage/test_sqlite_transactions.py` | 10 项：事务原子性、CAS、审计只追加、更正追加 |
| `tests/storage/test_sqlite_concurrency.py` | 5 项：并发单赢家、唯一键竞争、线程/跨进程不丢更新、写锁等待 |
| `tests/storage/test_sqlite_search.py` | 8 项：FTS5 可用、中文子串、无效查询、幂等入库、重启可检索、重建路径 |
| `tests/storage/test_sqlite_schema.py` | 9 项：版本账本、迁移幂等、旧库原地升级、坏迁移回滚、协议/常量一致性、激活点 |
| `tests/storage/test_python310_compat.py` | 4 项：3.11+ 用法禁用表、3.10 语法解析、仅标准库导入、无第三方存储驱动 |
| `tests/storage/test_limit_semantics.py` | **9 项**（v0.3 新增）：`limit` 边界矩阵、与契约裁决点 parity、三入口一致、负数拒绝后连接仍可用 |
| `tests/storage/test_sqlite_ordering.py` | **10 项**（v0.5 新增）：同时间戳按写入顺序、顺序与 id 无关、多次调用稳定、重开库不变、更新不移动位置、事件 FIFO、`get("Event")` 拒绝、limit 与过滤的先后次序 |

---

## 4. 原始命令与原始输出（逐字粘贴）

### 4.1 契约测试：SQLite 后端（v0.2 基线证据；v0.3 的最新数字见 §4.5）

```
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> set ASISTENT_STORE=sqlite
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python -m unittest discover -s tests -t .
...................................................................................................................................
----------------------------------------------------------------------
Ran 135 tests in 3.004s

OK
```

（135 = 91 项契约测试 + 44 项 SQLite 专属测试。仅契约部分：）

```
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> set ASISTENT_STORE=sqlite
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python -m unittest discover -s tests/contracts -t .
...........................................................................................
----------------------------------------------------------------------
Ran 91 tests in 0.710s

OK
```

对照（同一组测试在参考实现上）：

```
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> set ASISTENT_STORE=reference
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python -m unittest discover -s tests/contracts -t .
...........................................................................................
----------------------------------------------------------------------
Ran 91 tests in 0.115s

OK

D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python -m unittest discover -s tests -t .
.......................................................................................................................................
----------------------------------------------------------------------
Ran 135 tests in 2.458s

OK
```

契约快照未被推动：

```
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python scripts/gen_contract_snapshot.py --check
契约快照一致（v0.2）
```

本机解释器：`C:\Users\<you>\.conda\envs\<env>-a29d9cc4\python.exe` = Python 3.12.14，
sqlite 3.53.4，`threadsafety=3`，FTS5 可用。

### 4.2 SQLite 专属测试（v0.2 时的 44 项，逐条列出；v0.3 后为 53 项，新增 9 项见 §4.5）

```
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python -m unittest discover -s tests/storage -t . -v
test_no_python_311_plus_constructs_in_owned_sources (tests.storage.test_python310_compat.Python310CompatibilityTest.test_no_python_311_plus_constructs_in_owned_sources) ... ok
test_only_standard_library_imports (tests.storage.test_python310_compat.Python310CompatibilityTest.test_only_standard_library_imports) ... ok
test_sources_parse_as_python_310_grammar (tests.storage.test_python310_compat.Python310CompatibilityTest.test_sources_parse_as_python_310_grammar) ... ok
test_third_party_storage_drivers_are_absent (tests.storage.test_python310_compat.Python310CompatibilityTest.test_third_party_storage_drivers_are_absent) ... ok
test_cross_process_writers_lose_no_updates (tests.storage.test_sqlite_concurrency.CrossProcessConcurrencyTest.test_cross_process_writers_lose_no_updates) ... ok
test_write_contention_waits_instead_of_failing (tests.storage.test_sqlite_concurrency.CrossProcessConcurrencyTest.test_write_contention_waits_instead_of_failing) ... ok
test_cas_retry_loop_loses_no_updates (tests.storage.test_sqlite_concurrency.ThreadedConcurrencyTest.test_cas_retry_loop_loses_no_updates) ... ok
test_concurrent_claim_action_has_single_winner (tests.storage.test_sqlite_concurrency.ThreadedConcurrencyTest.test_concurrent_claim_action_has_single_winner) ... ok
test_concurrent_unique_put_creates_exactly_one_object (tests.storage.test_sqlite_concurrency.ThreadedConcurrencyTest.test_concurrent_unique_put_creates_exactly_one_object) ... ok
test_event_dedupe_and_claim_survive_reopen (tests.storage.test_sqlite_durability.WalAndDurabilityTest.test_event_dedupe_and_claim_survive_reopen) ... ok
test_in_flight_after_crash_is_demoted_to_unknown_not_success (tests.storage.test_sqlite_durability.WalAndDurabilityTest.test_in_flight_after_crash_is_demoted_to_unknown_not_success) ... ok
test_index_state_and_error_survive_reopen (tests.storage.test_sqlite_durability.WalAndDurabilityTest.test_index_state_and_error_survive_reopen) ... ok
test_memory_database_skips_wal_without_failing (tests.storage.test_sqlite_durability.WalAndDurabilityTest.test_memory_database_skips_wal_without_failing) ... ok
test_objects_audit_events_survive_reopen (tests.storage.test_sqlite_durability.WalAndDurabilityTest.test_objects_audit_events_survive_reopen) ... ok
test_other_process_sees_committed_rows (tests.storage.test_sqlite_durability.WalAndDurabilityTest.test_other_process_sees_committed_rows) ... ok
test_unique_key_survives_reopen_and_still_blocks_duplicates (tests.storage.test_sqlite_durability.WalAndDurabilityTest.test_unique_key_survives_reopen_and_still_blocks_duplicates) ... ok
test_wal_is_enabled_and_sidecar_files_exist (tests.storage.test_sqlite_durability.WalAndDurabilityTest.test_wal_is_enabled_and_sidecar_files_exist) ... ok
test_failed_migration_rolls_back_and_keeps_ledger_consistent (tests.storage.test_sqlite_schema.SchemaVersionTest.test_failed_migration_rolls_back_and_keeps_ledger_consistent) ... ok
test_fresh_database_gets_full_schema_and_ledger (tests.storage.test_sqlite_schema.SchemaVersionTest.test_fresh_database_gets_full_schema_and_ledger) ... ok
test_old_database_is_upgraded_in_place_without_losing_data (tests.storage.test_sqlite_schema.SchemaVersionTest.test_old_database_is_upgraded_in_place_without_losing_data) ... ok
test_reopen_does_not_reapply_migrations (tests.storage.test_sqlite_schema.SchemaVersionTest.test_reopen_does_not_reapply_migrations) ... ok
test_schema_version_of_empty_and_foreign_database (tests.storage.test_sqlite_schema.SchemaVersionTest.test_schema_version_of_empty_and_foreign_database) ... ok
test_default_constructor_uses_private_temp_directory (tests.storage.test_sqlite_schema.StoreConsistencyTest.test_default_constructor_uses_private_temp_directory) ... ok
test_object_type_registry_covers_all_contract_objects (tests.storage.test_sqlite_schema.StoreConsistencyTest.test_object_type_registry_covers_all_contract_objects) ... ok
test_semantics_constants_do_not_drift_from_reference (tests.storage.test_sqlite_schema.StoreConsistencyTest.test_semantics_constants_do_not_drift_from_reference) ... ok
test_sqlite_store_satisfies_frozen_protocol (tests.storage.test_sqlite_schema.StoreConsistencyTest.test_sqlite_store_satisfies_frozen_protocol) ... ok
test_store_factory_seam_selects_sqlite (tests.storage.test_sqlite_schema.StoreConsistencyTest.test_store_factory_seam_selects_sqlite) ... ok
test_body_is_stored_normalized_not_raw (tests.storage.test_sqlite_search.Fts5SearchTest.test_body_is_stored_normalized_not_raw) ... ok
test_cjk_substring_search_works (tests.storage.test_sqlite_search.Fts5SearchTest.test_cjk_substring_search_works) ... ok
test_fts5_available_and_index_table_is_built_by_migration (tests.storage.test_sqlite_search.Fts5SearchTest.test_fts5_available_and_index_table_is_built_by_migration) ... ok
test_index_write_is_idempotent_per_document (tests.storage.test_sqlite_search.Fts5SearchTest.test_index_write_is_idempotent_per_document) ... ok
test_invalid_query_returns_empty_and_never_everything (tests.storage.test_sqlite_search.Fts5SearchTest.test_invalid_query_returns_empty_and_never_everything) ... ok
test_query_is_built_by_contract_helper (tests.storage.test_sqlite_search.Fts5SearchTest.test_query_is_built_by_contract_helper) ... ok
test_rebuild_path_after_drop (tests.storage.test_sqlite_search.Fts5SearchTest.test_rebuild_path_after_drop) ... ok
test_search_index_survives_reopen (tests.storage.test_sqlite_search.Fts5SearchTest.test_search_index_survives_reopen) ... ok
test_audit_requires_action (tests.storage.test_sqlite_transactions.AuditAppendOnlyTest.test_audit_requires_action) ... ok
test_audit_rows_cannot_be_updated_or_deleted_by_sql (tests.storage.test_sqlite_transactions.AuditAppendOnlyTest.test_audit_rows_cannot_be_updated_or_deleted_by_sql) ... ok
test_audit_trail_filters_and_limit (tests.storage.test_sqlite_transactions.AuditAppendOnlyTest.test_audit_trail_filters_and_limit) ... ok
test_correction_is_a_new_appended_row (tests.storage.test_sqlite_transactions.AuditAppendOnlyTest.test_correction_is_a_new_appended_row) ... ok
test_cas_conflict_is_detected_and_stored_value_is_preserved (tests.storage.test_sqlite_transactions.TransactionAtomicityTest.test_cas_conflict_is_detected_and_stored_value_is_preserved) ... ok
test_failed_claim_leaves_no_partial_write (tests.storage.test_sqlite_transactions.TransactionAtomicityTest.test_failed_claim_leaves_no_partial_write) ... ok
test_failed_transition_rolls_back_object_and_audit (tests.storage.test_sqlite_transactions.TransactionAtomicityTest.test_failed_transition_rolls_back_object_and_audit) ... ok
test_index_state_write_conflict_is_rejected (tests.storage.test_sqlite_transactions.TransactionAtomicityTest.test_index_state_write_conflict_is_rejected) ... ok
test_update_without_expected_revision_is_rejected (tests.storage.test_sqlite_transactions.TransactionAtomicityTest.test_update_without_expected_revision_is_rejected) ... ok

----------------------------------------------------------------------
Ran 44 tests in 2.229s

OK
```

关键测试做了什么（避免"看名字就算过"）：

- **WAL**：断言 `PRAGMA journal_mode='wal'`、`PRAGMA synchronous=2`，并断言写完一笔后
  `<db>-wal` 旁路文件真实存在；`:memory:` 库走无 WAL 降级路径。
- **进程重启**：另起一个 **子进程**（`python -c`）打开同一个库文件，读回对象/审计/统计
  （不是同进程 new 一个实例）；另有"关闭 → 重开同路径"的持久化断言。
- **事务原子性**：用 `mock.patch` 让"发状态事件"环节抛异常，断言对象状态、审计行数、
  事件三者**一起回滚**，且事务回滚后连接仍可用；另一条是"审批摘要不一致"时不许留下
  attempt、不许消费审批。
- **并发不丢更新**：8 线程 × 10 次 CAS 重试 → 断言条目数 = 80、无重复、revision = 81；
  跨进程 3 进程 × 8 次 → 断言条目集合与 revision 精确对账。
- **写锁等待**：另一个连接持有写锁 0.4s，断言第二个连接的写入**等待后成功**（耗时 > 0.2s），
  而不是 `database is locked`。
- **审计只追加**：绕过程序直接 `UPDATE`/`DELETE` `audit_events` → 触发器 ABORT 抛
  `sqlite3.IntegrityError`，行数不变。
- **迁移**：坏迁移注入后断言账本仍是 3、`should_not_exist` 表未残留；
  "v1 旧库"原地升到 v3 且数据、WAL、列索引都在。
- **FTS5**：中文 2 字查询 `部署` 命中目标文档；入库内容确实是 `normalize_for_index()`
  的结果（逐字切分）；无效查询返回空而不是全量。

### 4.3 冻结文件未被改动（`git diff --stat`；v0.3 复验见 §4.5）

```
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> git status --short
?? asistent/storage/
?? tests/storage/

D:\dsh\个人助手\.worktrees\mvp-0-sqlite> git diff --stat
（无输出：没有任何已跟踪文件被修改）

D:\dsh\个人助手\.worktrees\mvp-0-sqlite> git diff --stat -- asistent/contracts tests/contracts scripts
（无输出）

D:\dsh\个人助手\.worktrees\mvp-0-sqlite> git log --oneline -1
e110f25 docs(status): 阶段状态与阻塞清单（契约 v0.2 冻结、部署通路打通）
```

说明：`git status` 只出现两个**新增目录**（我的写作用域），没有任何 `M`/`D` 条目；
`git diff --stat` 与针对 `asistent/contracts`、`tests/contracts`、`scripts` 的 diff 均为空。
本次会话**未运行任何 git 写命令**（无 add/commit/push/checkout/worktree）。

### 4.4 设备 Python 3.10.12 复跑（v0.2 基线；v0.3 复跑见 §4.5）

上传方式：把 worktree 的 `asistent/`、`tests/` 复制到 ASCII 临时目录后
`scp -r` 到 `board:~/asistent-spike/mvp0-check/`（未触碰 `~/code/asistent`、`~/.dsh`、
`~/miniforge3`、`~/assistant-dev`、`~/asistent-spike/venv`）。

```
ssh board "cd ~/asistent-spike/mvp0-check && export PYTHONIOENCODING=utf-8 && /usr/bin/python3 --version && \
  echo '=== A contracts sqlite' && ASISTENT_STORE=sqlite /usr/bin/python3 -m unittest discover -s tests/contracts -t . 2>&1 | tail -4 && \
  echo '=== B all sqlite'      && ASISTENT_STORE=sqlite /usr/bin/python3 -m unittest discover -s tests -t . 2>&1 | tail -4 && \
  echo '=== C all reference'   && /usr/bin/python3 -m unittest discover -s tests -t . 2>&1 | tail -4 && \
  echo '=== D storage'         && /usr/bin/python3 -m unittest discover -s tests/storage -t . 2>&1 | tail -4 && \
  echo '=== E compileall'      && /usr/bin/python3 -m compileall -q asistent tests > /dev/null && echo compileall-ok"

Python 3.10.12
=== A contracts sqlite
----------------------------------------------------------------------
Ran 91 tests in 0.413s

OK
=== B all sqlite
----------------------------------------------------------------------
Ran 135 tests in 2.621s

OK
=== C all reference
----------------------------------------------------------------------
Ran 135 tests in 2.307s

OK
=== D storage
----------------------------------------------------------------------
Ran 44 tests in 2.221s

OK
=== E compileall
compileall-ok
```

设备环境实测（`device_probe.py` 逐字输出）：

```
ssh board "cd ~/asistent-spike/mvp0-check && python3 device_probe.py"
python 3.10.12 (main, Nov 20 2023, 15:14:05) [GCC 11.4.0]
sqlite lib 3.37.2
sqlite module threadsafety 1
journal_mode ('wal',)
fts5 ddl ok
has sqlite3.Connection.autocommit (3.12+): False
has sqlite3.Connection.getlimit (3.11+): False
has sys.stdlib_module_names (3.10+): True
```

`threadsafety=1` 正是"必须 `check_same_thread=False` + 自己的锁"的原因；
`autocommit`/`getlimit` 都不存在，验证实现没有用 3.11+/3.12+ 专有的 sqlite3 接口
（静态禁用表也覆盖了这两项）。

---

### 4.5 契约 v0.3 对齐（task-4：`limit` 语义）

背景：Lead 把契约升到 **v0.3**（tag `contracts-v0.3`，commit `efd2e04`）并合并进本分支
（`0fb0df6`），新增 5 项契约测试（`tests/contracts/test_storage_contract.py::StoreV03ContractTest`）。
对齐前 Lead 实测的 2 处差距（原始输出，逐字）：

```
ERROR: test_put_audit_detail_lands_in_the_audit_trail
  File "asistent/storage/sqlite_store.py", line 734, in audit_trail
    rows = rows[-limit:]
TypeError: bad operand type for unary -: 'NoneType'

FAIL: test_limit_semantics_are_explicit
AssertionError: ValidationError not raised
```

修复后（本机 3.12.14）：

```
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> set ASISTENT_STORE=sqlite
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python -m unittest discover -s tests/contracts -t .
----------------------------------------------------------------------
Ran 96 tests in 0.802s

OK

D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python -m unittest discover -s tests -t .
----------------------------------------------------------------------
Ran 149 tests in 3.484s

OK

D:\dsh\个人助手\.worktrees\mvp-0-sqlite> set ASISTENT_STORE=reference
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python -m unittest discover -s tests/contracts -t .
----------------------------------------------------------------------
Ran 96 tests in 0.129s

OK

D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python -m unittest discover -s tests -t .
----------------------------------------------------------------------
Ran 149 tests in 2.683s

OK

D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python scripts/gen_contract_snapshot.py --check
契约快照一致（v0.3）
```

新增的 9 项边界测试（`tests/storage/test_limit_semantics.py`，全绿）：

```
test_audit_trail_matches_contract_decision_point ... ok
test_audit_trail_returns_the_latest_rows_in_ascending_order ... ok
test_find_limit_matrix ... ok
test_find_matches_contract_decision_point ... ok
test_list_events_limit_matrix ... ok
test_list_events_matches_contract_decision_point ... ok
test_negative_limit_is_rejected_before_any_work_and_leaves_usable_store ... ok
test_out_of_range_limit_types_are_not_silently_swallowed ... ok
test_three_entrypoints_agree_on_every_limit ... ok

----------------------------------------------------------------------
Ran 53 tests in 2.769s

OK
```

冻结契约未被触碰（要求的那条命令）：

```
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> git diff --name-only efd2e04 -- asistent/contracts tests/contracts
（无输出）

D:\dsh\个人助手\.worktrees\mvp-0-sqlite> git status --short
?? asistent/storage/
?? docs/mvp/
?? tests/storage/
```

设备 `/usr/bin/python3` 3.10.12 复跑（scp 到 `~/asistent-spike/mvp0-check/`）：

```
Python 3.10.12
=== A contracts sqlite
Ran 96 tests in 0.430s
OK
=== B all sqlite
Ran 149 tests in 2.922s
OK
=== C all reference
Ran 149 tests in 2.486s
OK
=== D storage
Ran 53 tests in 2.396s
OK
=== E compileall
compileall-ok
```

数字口径：v0.2 时 91 项契约 + 44 项存储 = 135；v0.3 新增 5 项契约测试
（`StoreV03ContractTest`）→ 契约 96 项，我在 `tests/storage/**` 新增 9 项边界测试 → 存储 53 项，
合计 **149 项**，两套后端、两套 Python（3.12.14 / 3.10.12）四组运行全部 `OK`。

---

### 4.6 契约 v0.5 对齐：定序与写入顺序一致（task-7）

Lead 报的现场（`StoreV04ContractTest` 单类连跑 6 次红 5 次）。我先做了**对照实验**：
把 `asistent/storage/{sqlite_store,migrations}.py` 换回修复前的版本（`git show 7800bfd:…`，
脚本放在 `%TEMP%`，不碰仓库），同一份契约测试连跑 10 次：

```
===== BEFORE (storage @ 7800bfd, 修复前) : StoreV04ContractTest x10
run  1 : exit=1  Ran 9 tests in 0.201s FAILED (failures=1)
run  2 : exit=1  Ran 9 tests in 0.177s FAILED (failures=1)
run  3 : exit=1  Ran 9 tests in 0.191s FAILED (failures=1)
run  4 : exit=1  Ran 9 tests in 0.183s FAILED (failures=1)
run  5 : exit=1  Ran 9 tests in 0.177s FAILED (failures=1)
run  6 : exit=1  Ran 9 tests in 0.176s FAILED (failures=2)
run  7 : exit=0  Ran 9 tests in 0.195s OK
run  8 : exit=1  Ran 9 tests in 0.170s FAILED (failures=2)
run  9 : exit=1  Ran 9 tests in 0.257s FAILED (failures=2)
run 10 : exit=1  Ran 9 tests in 0.178s FAILED (failures=1)
```

失败的名字（说明这条测试不是"空转"）：

```
run  1..5, 10 : test_list_events_is_fifo_and_takes_the_earliest_n
run  6, 8, 9  : test_find_returns_the_earliest_n_not_the_latest, test_list_events_is_fifo_and_takes_the_earliest_n
run  7         : (OK)
```

一次典型失败（同时间戳事件的 FIFO 被随机 id 打乱）：

```
AssertionError: Lists differ: ['evt_d0e5a69c…', 'evt_33ca76b7…'] != ['evt_33ca76b7…', 'evt_460ac40c…']
  File "…\tests\contracts\test_storage_contract.py", line 147, in test_list_events_is_fifo_and_takes_the_earliest_n
```

修复后（本机 3.12.14），同一类连跑 **10 次全绿**：

```
===== AFTER (当前实现) : StoreV04ContractTest x10
run  1 : exit=0  Ran 9 tests in 0.199s OK
run  2 : exit=0  Ran 9 tests in 0.246s OK
run  3 : exit=0  Ran 9 tests in 0.184s OK
run  4 : exit=0  Ran 9 tests in 0.196s OK
run  5 : exit=0  Ran 9 tests in 0.197s OK
run  6 : exit=0  Ran 9 tests in 0.196s OK
run  7 : exit=0  Ran 9 tests in 0.193s OK
run  8 : exit=0  Ran 9 tests in 0.197s OK
run  9 : exit=0  Ran 9 tests in 0.194s OK
run 10 : exit=0  Ran 9 tests in 0.197s OK
```

全仓测试（本机 3.12.14）：

```
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> set ASISTENT_STORE=sqlite
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python -m unittest discover -s tests -t .
----------------------------------------------------------------------
Ran 314 tests in 19.788s

OK (skipped=5)

D:\dsh\个人助手\.worktrees\mvp-0-sqlite> set ASISTENT_STORE=reference
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> python -m unittest discover -s tests -t .
----------------------------------------------------------------------
Ran 314 tests in 19.118s

OK (skipped=5)
```

（5 项 skip 全部来自 MVP-1 的 `tests/ingest/test_scanner.py`：mkfifo / 符号链接需要平台权限，
Windows 下跳过；设备 Linux 上只剩 3 项 skip，那 2 项在设备上真跑。）

分目录计数（sqlite 后端）：

```
tests/contracts  Ran 106 tests in 1.072s OK
tests/storage    Ran 63 tests in 3.512s OK
tests/ingest     Ran 99 tests in 3.720s OK (skipped=5)
tests/mail       Ran 46 tests in 6.276s OK
```

仓库自带脚本端到端：

```
> powershell -NoProfile -File scripts/test.ps1 -Store sqlite -Python <python.exe>
Ran 314 tests in 13.021s

OK (skipped=5)
OK
```

冻结契约零改动 + 改动面：

```
D:\dsh\个人助手\.worktrees\mvp-0-sqlite> git diff --name-only main -- asistent/contracts tests/contracts
（无输出）

D:\dsh\个人助手\.worktrees\mvp-0-sqlite> git diff --stat main -- asistent/contracts tests/contracts
（无输出）

D:\dsh\个人助手\.worktrees\mvp-0-sqlite> git status --short
 M asistent/storage/migrations.py
 M asistent/storage/sqlite_store.py
 M tests/storage/test_sqlite_schema.py
?? tests/storage/test_sqlite_ordering.py
```

设备 `/usr/bin/python3` 3.10.12 复跑：

```
python: Python 3.10.12
A sqlite done          Ran 314 tests in 10.480s   OK (skipped=3)
B reference done       Ran 314 tests in 10.205s   OK (skipped=3)
C compileall ok
--- StoreV04ContractTest x10（设备）
run 1 : exit=0  Ran 9 tests in 0.077s OK
run 2 : exit=0  Ran 9 tests in 0.077s OK
run 3 : exit=0  Ran 9 tests in 0.078s OK
run 4 : exit=0  Ran 9 tests in 0.077s OK
run 5 : exit=0  Ran 9 tests in 0.078s OK
run 6 : exit=0  Ran 9 tests in 0.077s OK
run 7 : exit=0  Ran 9 tests in 0.077s OK
run 8 : exit=0  Ran 9 tests in 0.077s OK
run 9 : exit=0  Ran 9 tests in 0.077s OK
run 10 : exit=0  Ran 9 tests in 0.077s OK
--- storage suite    Ran 63 tests in 2.939s   OK
```

注意设备上 `StoreV04ContractTest` 单次只要 0.077s（本机 0.2s）：**写得越快越容易撞同一毫秒**，
所以这个缺陷在设备上比在本机更容易暴露——这也正是它当初被 Lead 判为"真实缺陷"而不是抖动的原因。

---

## 5. 已知限制
1. **单进程内写串行**：一把可重入锁包住每个公开方法；跨进程靠 SQLite 写锁 + `busy_timeout`。
   适合单机个人助手；不是高并发服务器存储。读并发（多进程同时只读）未做压力验证。
2. **payload 是 JSON**：元组会变成列表；不可 JSON 序列化的值经 `default=str` 变字符串。
   契约对象字段都是 list/dict/标量，当前无影响；但"字段里塞自定义对象"会静默变字符串。
3. **`find` 只有列字段下推 SQL**（`id`/`state`/`case_id`/`index_state`/`archive_state`），
   其他 `where` 键在进程内过滤——这是为了让 `str()` 比较语义与参考实现逐字一致。
   单类型行数很大时需要为该字段加列或加索引。
4. **审计只追加由触发器强制**：若将来需要合法删除审计行（迁移/归档），必须先显式
   `DROP TRIGGER`；当前没有提供 API（有意为之）。
5. **WAL 的物理要求**：`-wal`/`-shm` 必须与主库同一文件系统；不支持网络文件系统。
6. **默认构造会建临时目录库**（供契约测试零参数使用），实例回收时清理；
   生产必须显式传 `path`（`open_store()`）。
7. **检索索引写入是显式调用**（`index_document`）：资料流水线的自动接线是 MVP-1 的活。
   FTS5 不可用时 `migration v3` 只记账备注，`search()` 明确抛错（不静默降级）。
8. **`stats()` 沿用参考实现语义**：`_total` 不含 `Event`/`AuditEvent`；若同时把一个
   `AuditEvent` 对象 `put()` 进对象表，`AuditEvent` 计数会被审计表计数覆盖（参考实现同一怪癖）。
9. **无列级更新**：每次 `put` 全量重写 `payload`（对象大时写入放大）。
10. **`connection` 是逃生通道**：测试/运维可读；绕过锁直接改数据不保证并发安全。
11. **测试脚本回放的中文显示**：`scripts/test.ps1 -Store sqlite` 实测通过（314 OK），
    但 PS 5.1 控制台按 GBK 回放 UTF-8 日志，中文测试名会显示成乱码——**属显示层问题**
    （日志文件 `%TEMP%\asistent-tests.log` 本身是 UTF-8）。
12. **`limit` 不做类型强转**：契约只定义 `None` 与非负整数。`limit=True` 按 Python 语义当 1 用；
    传字符串会以 `TypeError` 立刻暴露（不静默转 `int`）——宁可炸也不要悄悄改语义。
    上限没有额外保护（`limit` 极大等于"不限"，与契约一致）。
13. **`seq` 由应用侧分配**（`MAX(seq)+1`），不是数据库自增列：好处是"新旧库 schema 完全一致"、
    更新语句天然不碰它；代价是如果将来出现**删除**对象/事件的路径，`MAX+1` 可能复用被删行的序号
    （当前没有任何删除路径，审计表也不允许删除）。真要支持删除，改成 `AUTOINCREMENT` 或
    独立计数表即可——这是有意留的一个明确决策点，不是隐藏行为。
14. **`where` 里不能下推的键会退化为全类型扫描**：为保证"先过滤再 limit"与参考实现一致，
    这类查询不再用 SQL `LIMIT` 截断（`find("Case", where={"goal": …})` 会取回该类型的全部行再过滤）。
    数量级上有索引的键（`id`/`state`/`case_id`/`index_state`/`archive_state`）不受影响。
15. **`seq` 只在同一张表内单调**：对象表与事件表各自独立编号；两表之间的先后不构成全序
    （契约也只要求表内定序）。跨表的时间线要看时间戳。

### 技术债

- 跨进程测试规模是 3 进程 × 8 轮（正确性验证），**未测吞吐/延迟**；写放大与 WAL 增长未测量。
- 没有 `PRAGMA integrity_check` 的巡检入口，也没有定期 checkpoint 策略
  （长跑进程下 WAL 可能持续增长；当前只提供手动 `checkpoint()`）。
- "旧库升级"现在用 `apply_migrations(conn, upto=1)` 建一个**真正的 v1 库**（含手写的历史对象行）
  再升级验证；但库里没有"从设备上拷回来的真实旧库文件"作为回归样本，仍算半覆盖。
- `_tx()` 会串行化同进程的读；多进程只读并发、`SQLITE_BUSY` 边界下的等待行为未覆盖。
- `tests/storage` 依赖 `tests/contracts/helpers.py` 的 `make_case/make_action/make_document`
  构造数据（只读复用，未修改该文件）；若将来冻结夹具改名，这里要跟着改。

---

## 6. 提案与后续

### 6.1 CP 提案（**已全部被 Lead 采纳**：CP-1/2/3 → contracts-v0.3）

状态：CP-1/CP-2/CP-3 均被接受，契约已升到 **v0.3**（tag `contracts-v0.3`，commit `efd2e04`，
变更记录 CP-20261010-03/04/05），并合并进本分支（`0fb0df6`）。后续 v0.4（`c09b8ee`）
与 v0.5（`e8e6123`，定序 + Event 访问路径）也已合并（`231bead`）。下面是提出时的原文，留档备查。

- **CP-1（已采纳）**：`StoreProtocol` 未登记 `get_by_unique(namespace, value)`，
  但冻结的契约测试 `tests/contracts/test_storage_contract.py::test_same_writer_twice_keeps_single_object`
  **直接调用它**——它是事实契约却不在协议里。我的实现当时被迫额外实现这个未登记方法
  （否则契约测试无法全绿）。v0.3 已把它写进 `StoreProtocol`，并写明"未绑定返回 `None`"。
- **CP-2（已采纳）**：`put(..., audit_detail=...)` 与 `now()` 同样是"参考实现有、协议没有"
  的公共方法。v0.3 已把两者登记进协议（`test_protocol_declares_the_methods_frozen_tests_use` 可验证）。
- **CP-3（已采纳）**：`find(limit=...)` 的边界语义原本没有定义。v0.3 把它显式化为
  "`None` 不限 / `>= 0` 最多 N 条（`0` → 空）/ `< 0` 抛 `ValidationError`"，
  并把裁决权收到 `apply_limit` / `apply_limit_tail` 一个点——**旧的"按 Python 负切片一致"的说法作废**，
  本实现已按新语义重写（§2.6、§4.5），并用 parity 测试锁住三个入口。

### 6.1b 定序缺陷（**Lead 发现，本实现已对齐**：CP-20261010-11 → contracts-v0.5）

这条不是我的提案，而是 Lead 在 main 上实测到的**真实缺陷**（不是抖动）：定序键用了随机 id 平局。
契约 v0.5 把它定死为 `(时间戳, 写入序号)`，本实现按 §2.7 落地，并补了"修复前 9/10 红 → 修复后 10/10 绿"
的对照证据（§4.6）。**没有对契约或 `tests/contracts/**` 提任何异议**：v0.5 的两条规则
（定序与写入顺序一致、`Event` 不走通用 `get`）都与本实现的实际行为一致，且都能在本机与设备上稳定复现。

### 6.2 冻结点上的编码缺陷（**已由 Lead 修复**）

提出时的事实：`asistent/contracts/__init__.py` 是 **UTF-8 带 BOM**，且首行 docstring 是 **mojibake**
（原本该是"共享契约包（SPEC §4、§5、§21.1）"，实际存成了 UTF-8 字节被按 GBK 解读后的错码文本）。
当时的字节级扫描（对 `asistent/` 下 23 个 `.py` 逐个扫描，**只有这 1 个文件**受影响）：

```
affected: 1
asistent\contracts\__init__.py           bom=True  mojibake   b'\xef\xbb\xbf"""\xe9\x8d\x8f\xe5\x8f\x98...'
---- control: total py files scanned
23
```

结果：Lead 已在 commit `85acc7e` 修复（根因是 PowerShell 5.1 的 `Get-Content`/`Set-Content`
写该文件时按 GBK 转码），并对全仓做了同类扫描确认无第二处损坏。`git diff e110f25 efd2e04` 中
该文件的新首行已是正常中文（"共享契约包（SPEC §4、§5、§21.1）"）。
我的兼容测试仍保留"读字节解析"的写法：它对 BOM 免疫，属于防御性做法，不是绕过。

### 6.3 其他

- 我未运行任何 git 写命令（含 merge/rebase）。本次改动面（`git status --short`）：
  `M asistent/storage/migrations.py`、`M asistent/storage/sqlite_store.py`、
  `M tests/storage/test_sqlite_schema.py`、`?? tests/storage/test_sqlite_ordering.py` + 本文档，**提交由 Lead 做**。
- Lead 端到端验收命令（都已在本地与设备实测通过）：
  `set ASISTENT_STORE=sqlite` + `python -m unittest discover -s tests -t .`（期望 **314 OK**）、
  `set ASISTENT_STORE=reference` + 同一命令（期望 **314 OK**）、
  `python -m unittest tests.contracts.test_storage_contract.StoreV04ContractTest`（连跑 10 次全绿）、
  `git diff --name-only main -- asistent/contracts tests/contracts`（期望无输出）、
  `powershell -NoProfile -File scripts/test.ps1 -Store sqlite -Python <python.exe>`（期望 `OK`，
  日志见 `%TEMP%\asistent-tests.log`）。
