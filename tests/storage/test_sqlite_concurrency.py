"""并发语义：单赢家领取、并发写不丢更新（线程内 + 跨进程）。

契约要求"动作领取是一个原子调用"（SPEC §21.2）。这里用两层证据证明它不是靠运气：

1. 线程层：多个执行者用 `threading.Barrier` 同时冲，只能有一个拿到执行权，
   且数据库里只留一条 attempt；
2. 进程层：CAS 重试循环（读 → 改 → 带 expected_revision 写）在多个进程下
   不丢更新，最终 revision 与条目数完全对得上。
"""

from __future__ import annotations

import json
import threading
import time
import unittest

from asistent.contracts import ActionNotClaimable, DuplicateObject, RevisionConflict
from asistent.contracts.objects import Case
from tests.contracts.helpers import make_action, make_case

from .support import SqliteStoreTestCase, run_python

CAS_WORKER_SCRIPT = """
import json, os, sqlite3, sys, time
sys.path.insert(0, os.getcwd())
from asistent.contracts.errors import RevisionConflict
from asistent.storage.sqlite_store import SqliteStore

db_path, case_id, tag, rounds = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
store = SqliteStore(path=db_path)
done = 0
attempts = 0
conflicts = 0
while done < rounds:
    attempts += 1
    if attempts > 2000:
        print(json.dumps({"ok": False, "tag": tag, "attempts": attempts}))
        sys.exit(3)
    try:
        case = store.get("Case", case_id)
        case.participants.append("%s-%d" % (tag, done))
        store.put(case, expected_revision=case.revision, reason="cross-process CAS")
        done += 1
    except RevisionConflict:
        conflicts += 1
        time.sleep(0.002)
    except sqlite3.OperationalError:
        time.sleep(0.01)
store.close()
print(json.dumps({"ok": True, "tag": tag, "rounds": done, "attempts": attempts, "conflicts": conflicts}))
"""


class ThreadedConcurrencyTest(SqliteStoreTestCase):
    def test_concurrent_claim_action_has_single_winner(self) -> None:
        store = self.open_store()
        case = make_case(store)
        action, approval, _summary = make_action(store, case=case)

        barrier = threading.Barrier(6)
        outcomes = []

        def worker(index: int) -> None:
            barrier.wait()
            try:
                attempt = store.claim_action(action.id, worker="executor-%d" % index)
                outcomes.append(("claimed", attempt.id))
            except ActionNotClaimable as exc:
                outcomes.append(("rejected", exc.code))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        claimed = [item for item in outcomes if item[0] == "claimed"]
        self.assertEqual(1, len(claimed), "只能有一个执行者拿到执行权，实际: %r" % (outcomes,))
        self.assertEqual("IN_FLIGHT", str(store.get("ExternalAction", action.id).state))
        self.assertEqual("CONSUMED", str(store.get("Approval", approval.id).state))
        attempts = store.find("ActionAttempt")
        self.assertEqual(1, len(attempts), "数据库里只能留下一条 attempt")
        self.assertEqual("IN_FLIGHT", attempts[0].state)

    def test_concurrent_unique_put_creates_exactly_one_object(self) -> None:
        store = self.open_store()
        barrier = threading.Barrier(4)
        results = []

        def worker(index: int) -> None:
            barrier.wait()
            try:
                created = store.put(Case(goal="worker-%d" % index), unique=("case_trigger", "uid-race"))
                results.append(("created", created.id))
            except DuplicateObject:
                results.append(("duplicate", None))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        created = [item for item in results if item[0] == "created"]
        self.assertEqual(1, len(created), "唯一键必须只有一个赢家，实际: %r" % (results,))
        self.assertEqual(1, len(store.find("Case")))
        self.assertIsNotNone(store.get_by_unique("case_trigger", "uid-race"))

    def test_cas_retry_loop_loses_no_updates(self) -> None:
        """8 个线程各追加 10 次，最终 revision 与条目数必须精确对账（无丢更新）。"""
        store = self.open_store()
        case = make_case(store)
        threads_count = 8
        per_thread = 10
        barrier = threading.Barrier(threads_count)
        errors = []

        def worker(index: int) -> None:
            barrier.wait()
            try:
                for run in range(per_thread):
                    while True:
                        current = store.get("Case", case.id)
                        current.participants.append("t%d-%d" % (index, run))
                        try:
                            store.put(
                                current,
                                expected_revision=current.revision,
                                reason="并发追加参与者",
                            )
                            break
                        except RevisionConflict:
                            continue  # 重读后重试：这才是正确的乐观并发处理
            except Exception as exc:  # pragma: no cover - 线程内异常带回主线程
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], errors)
        final = store.get("Case", case.id)
        expected = threads_count * per_thread
        self.assertEqual(expected, len(final.participants), "并发写入丢了更新")
        self.assertEqual(len(set(final.participants)), len(final.participants), "出现重复条目")
        self.assertEqual(expected + 1, final.revision, "revision 必须精确等于写入次数+1")


class CrossProcessConcurrencyTest(SqliteStoreTestCase):
    def test_cross_process_writers_lose_no_updates(self) -> None:
        """3 个进程各用 CAS 追加 8 次：跨进程也不许丢更新（busy_timeout 生效）。"""
        store = self.open_store()
        case = make_case(store)
        store.close()

        processes = 3
        rounds = 8
        results = []
        for index in range(processes):
            results.append(
                run_python(
                    CAS_WORKER_SCRIPT,
                    self.db_path,
                    case.id,
                    "p%d" % index,
                    str(rounds),
                    timeout=300.0,
                )
            )

        for code, output in results:
            self.assertEqual(0, code, output)
            payload = json.loads(output.strip().splitlines()[-1])
            self.assertTrue(payload["ok"], output)
            self.assertEqual(rounds, payload["rounds"])

        reopened = self.open_store()
        final = reopened.get("Case", case.id)
        expected = processes * rounds
        self.assertEqual(expected, len(final.participants), "跨进程写入丢了更新：%r" % (final.participants,))
        self.assertEqual(expected + 1, final.revision)
        self.assertEqual(
            sorted("p%d-%d" % (p, r) for p in range(processes) for r in range(rounds)),
            sorted(final.participants),
        )

    def test_write_contention_waits_instead_of_failing(self) -> None:
        """第二个连接撞上正在写的第一个连接时必须**等待**写锁（busy_timeout），而不是立刻报 locked。

        白盒说明：用 `_tx(write=True)` 直接占住写锁，是为了制造"另一个连接正在写"的场景。
        """
        first = self.open_store()
        second = self.open_store()
        make_case(first)
        holding = threading.Event()
        hold_seconds = 0.4

        def hold_write_lock() -> None:
            with first._tx(write=True):
                holding.set()
                time.sleep(hold_seconds)

        holder = threading.Thread(target=hold_write_lock)
        holder.start()
        self.assertTrue(holding.wait(10.0), "写锁没有被占住")

        started = time.time()
        created = second.put(Case(goal="第二个连接"), unique=("case_trigger", "uid-等待锁"))
        elapsed = time.time() - started
        holder.join(10.0)

        self.assertTrue(created.id)
        self.assertGreater(elapsed, hold_seconds * 0.5, "没有真的等待写锁（可能没走 SQLite 写锁）")
        self.assertEqual("第二个连接", second.get("Case", created.id).goal)
        self.assertEqual(2, len(second.find("Case")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
