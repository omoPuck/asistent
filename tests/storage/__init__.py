"""MVP-0 SQLite 存储专属测试（与 `tests/contracts/**` 的契约测试互补）。

本目录测的是**SQLite 特有行为**，契约测试测的是**语义一致性**：

- `test_sqlite_durability.py`：WAL 生效、重启/跨进程后数据仍在、索引状态与幂等键持久化；
- `test_sqlite_transactions.py`：事务原子性（失败整体回滚）、CAS 冲突、审计只追加；
- `test_sqlite_concurrency.py`：并发单赢家、并发写不丢更新（线程 + 跨进程）；
- `test_sqlite_search.py`：FTS5 可用性、中文子串检索、无效查询不退化成全量；
- `test_sqlite_schema.py`：schema 版本表、迁移幂等、旧库原地升级、迁移失败回滚、与参考实现的一致性。

运行（worktree 根目录）：

    python -m unittest discover -s tests -t .
"""
