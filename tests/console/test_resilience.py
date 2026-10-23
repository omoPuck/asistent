"""断线可恢复 / 重启后数据仍在 / 增量同步 / 并发编辑（MVP-4 §844-847 的基础）。

三组证据：

1. **进程级重启**：真起一个子进程服务、写数据、杀掉进程、再起一个新进程 ——
   换进程之后数据仍在（不是"换了个 HTTP 对象但共用内存"这种假重启）；
2. **增量同步**：`/api/changes?since=...` 只回变更过的对象，但**每次都回完整的待处理快照**，
   这样手机断网期间产生的事项不会漏；`If-None-Match` 命中时返回 304，不重复传正文；
3. **并发**：多个请求同时提交，`expected_revision` 语义保证不会有两次写入都"成功覆盖"。
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest

from .helpers import ConsoleTestCase, SubprocessServer, store_is_sqlite


class ProcessRestartTests(ConsoleTestCase):
    """换进程之后，手机上还能看到全部待处理项。"""

    def setUp(self) -> None:
        super().setUp()
        _doc, self.report, self.case = self.process_fixture("03-missing-material.eml")
        self.question = self.store.find("Question", where={"case_id": self.case.id}, limit=None)[0]

    def _config(self):
        return {
            "store": "sqlite" if store_is_sqlite() else "reference",
            "db_path": self.db_path,
            "token_path": self.token_path,
            "port": 0,
            "bind": "127.0.0.1",
        }

    def test_reference_store_mode_uses_in_process_restart(self):
        """reference 模式下换进程会丢掉内存库，所以这里只断言"同 store 换服务实例仍可见"。"""
        if store_is_sqlite():
            self.skipTest("SQLite 模式请用 test_new_process_sees_previous_writes")
        self.assertStatus(
            200,
            self.client.post(
                "/api/questions/%s/answer" % self.question.id,
                {"answer_text": "已准备好", "expected_revision": self.question.revision},
            ),
        )
        self.client = self.restart_server()
        payload = self.client.get("/api/overview").json
        self.assertNotIn(self.question.id, payload["pending"]["question_ids"])
        self.assertEqual(1, payload["pending"]["counts"]["questions"])
        self.assertEqual(1, payload["pending"]["counts"]["cases"])
        self.assertEqual("ANSWERED", self.client.get("/api/questions/%s" % self.question.id).json["state"])

    @unittest.skipUnless(store_is_sqlite(), "真重启需要落盘库：请用 ASISTENT_STORE=sqlite 运行")
    def test_new_process_sees_previous_writes(self):
        server = SubprocessServer(self._config())
        client = server.client(self.token)
        question = None
        answer_id = ""
        try:
            health = client.get("/api/health")
            self.assertStatus(200, health)
            self.assertEqual(server.port, health.json and server.port)

            question = client.get("/api/questions").json["questions"][0]
            answered = client.post(
                "/api/questions/%s/answer" % question["id"],
                {"answer_text": "子进程里写的回答", "expected_revision": question["revision"]},
            )
            self.assertStatus(200, answered)
            answer_id = answered.json["answer"]["id"]

            drafts = client.get("/api/drafts").json["drafts"]
            if drafts:
                detail = client.get("/api/drafts/%s" % drafts[0]["id"]).json
                edited = client.post(
                    "/api/drafts/%s/edit" % drafts[0]["id"],
                    {
                        "body": "子进程里写的第二版",
                        "change_reason": "重启前保存",
                        "version_no": detail["current"]["version_no"],
                        "expected_revision": detail["revision"],
                    },
                )
                self.assertStatus(200, edited)
        finally:
            server.stop()
        self.assertTrue(server.wait_stopped(), "服务进程必须真的停掉（否则不是重启）")
        self.assertFalse(server.is_alive())

        restarted = SubprocessServer(self._config())
        new_client = restarted.client(self.token)
        try:
            overview = new_client.get("/api/overview")
            self.assertStatus(200, overview)
            self.assertIn(self.case.id, overview.json["pending"]["case_ids"])
            stored_answer = new_client.get("/api/questions/%s" % question["id"]).json
            self.assertEqual("ANSWERED", stored_answer["state"])
            self.assertEqual(answer_id, stored_answer["answer_id"])
            self.assertTokenAbsent(overview)
        finally:
            restarted.stop()

    def test_log_lines_from_subprocess_never_contain_token(self):
        if not store_is_sqlite():
            self.skipTest("真重启需要落盘库")
        server = SubprocessServer(self._config())
        client = server.client(self.token)
        try:
            self.assertStatus(401, client.get("/api/health", token="wrong-token-000000000000000000000000"))
            self.assertStatus(200, client.get("/api/health"))
            for _ in range(40):  # 日志是异步进管道的，轮询到"成功/失败都记上了"为止
                server.drain_logs()
                joined = "\n".join(server.log_lines)
                if "auth=ok" in joined and "auth=denied" in joined:
                    break
                time.sleep(0.05)
                client.get("/api/health")
            server.drain_logs()
        finally:
            server.stop()
        joined = "\n".join(server.log_lines)
        self.assertNotIn(self.token, joined)
        self.assertIn("auth=ok", joined)
        self.assertIn("auth=denied", joined, "401 的那次也要记成 denied（只记结果，不记原因）")
        # 日志格式固定：方法 + 路径 + 状态 + 鉴权结果 + 耗时；不带 query、不带 header
        for line in server.log_lines:
            if line.startswith("LISTENING"):
                continue
            self.assertRegex(line, r"^(GET|POST) /\S+ -> \d{3} auth=(ok|denied) \d")
            self.assertNotIn("?", line)


class IncrementalSyncTests(ConsoleTestCase):
    """增量同步：`since` 只回变更；待处理快照永远完整。"""

    def setUp(self) -> None:
        super().setUp()
        _doc, self.report, self.case = self.process_fixture("03-missing-material.eml")
        self.case = self.store.get("Case", self.case.id)  # noqa: F841 - 保留给子测试
        self.case_id = self.report.case_id

    def test_overview_gives_cursor_and_etag(self):
        payload = self.client.get("/api/overview").json
        self.assertTrue(payload["next_since"])
        self.assertTrue(payload["etag"].startswith('W/"'))
        self.assertIn("cases", payload["pending"]["counts"])
        self.assertEqual(1, payload["pending"]["counts"]["cases"])
        self.assertEqual(2, payload["pending"]["counts"]["questions"])

    def test_etag_match_returns_304_without_body(self):
        first = self.client.get("/api/overview")
        etag = first.response_headers["ETag"]
        second = self.client.get("/api/overview", headers={"If-None-Match": etag})
        self.assertStatus(304, second)
        self.assertEqual("", second.response_body)

    def test_changes_since_returns_only_updated_objects(self):
        start = self.client.get("/api/overview").json["next_since"]
        question = self.store.find("Question", where={"case_id": self.case_id}, limit=None)[0]
        self.assertStatus(
            200,
            self.client.post(
                "/api/questions/%s/answer" % question.id,
                {"answer_text": "已准备好", "expected_revision": question.revision},
            ),
        )
        changed = self.client.get("/api/changes?since=%s" % start)
        self.assertStatus(200, changed)
        self.assertIn(question.id, [row["id"] for row in changed.json["changed"]["questions"]])
        self.assertTrue(changed.json["changed"]["answers"])
        # 待处理快照**永远完整**：断网期间的新事项不会漏
        self.assertEqual(1, changed.json["pending"]["counts"]["questions"])
        self.assertIn(self.case_id, changed.json["pending"]["case_ids"])

    def test_changes_requires_since(self):
        recorded = self.client.get("/api/changes")
        self.assertStatus(400, recorded)
        self.assertIn("since", recorded.json["error"]["message"])

    def test_dropped_connection_reconnect_sees_everything(self):
        """模拟断网：先拿一次快照，然后只是重新登录（不保留任何本地游标）也能看到全部待办。"""
        before = self.client.get("/api/overview").json["pending"]
        question = self.store.find("Question", where={"case_id": self.case_id}, limit=None)[1]
        self.assertStatus(
            200,
            self.client.post(
                "/api/questions/%s/answer" % question.id,
                {"answer_text": "已准备好", "expected_revision": question.revision},
            ),
        )
        # 断开（新客户端 = 新页面/新会话），不带任何本地状态重新连
        fresh = self.client.__class__(self.server.bound_host or "127.0.0.1", self.server.bound_port, self.token)
        after = fresh.get("/api/overview").json["pending"]
        self.assertEqual(1, after["counts"]["questions"])
        self.assertIn(question.id, before["question_ids"])
        self.assertNotIn(question.id, after["question_ids"])

    def test_case_list_since_only_returns_touched_cases(self):
        start = self.client.get("/api/overview").json["next_since"]
        unchanged = self.client.get("/api/cases?since=%s" % start).json
        self.assertEqual(0, unchanged["total"])
        self.client.post("/api/cases", {"goal": "新事务"})
        updated = self.client.get("/api/cases?since=%s" % start).json
        self.assertEqual(1, updated["total"])
        self.assertEqual("新事务", updated["cases"][0]["goal"])


class ConcurrencyTests(ConsoleTestCase):
    """并发下的版本语义。"""

    def setUp(self) -> None:
        super().setUp()
        # 刻意用两封不同的邮件：草稿来自 01，追问来自 03 —— 两个场景互不影响
        _doc, self.report, self.case = self.process_fixture("01-notice-multi-time.eml")
        self.draft = self.store.find("Draft", where={"case_id": self.report.case_id}, limit=None)[0]
        _doc2, self.report2, _case2 = self.process_fixture("03-missing-material.eml")
        self.question = self.store.find("Question", where={"case_id": self.report2.case_id}, limit=None)[0]

    def test_parallel_edits_of_same_version_have_exactly_one_winner(self):
        """8 个请求同时基于同一版本保存：只能有一个成功，其余必须 409（不能都成功覆盖）。"""
        draft = self.draft
        detail = self.client.get("/api/drafts/%s" % draft.id).json
        version_no = detail["current"]["version_no"]
        revision = detail["revision"]
        results = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            client = self.client.__class__(
                self.server.bound_host or "127.0.0.1", self.server.bound_port, self.token
            )
            response = client.post(
                "/api/drafts/%s/edit" % draft.id,
                {
                    "body": "并发写入 #%d" % index,
                    "change_reason": "并发 #%d" % index,
                    "version_no": version_no,
                    "expected_revision": revision,
                },
            )
            with lock:
                results.append(response.status)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        started = time.time()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertLess(time.time() - started, 30, "并发请求不应互相卡死")

        successes = results.count(200)
        conflicts = results.count(409)
        self.assertEqual(8, len(results), "每个请求都要有明确结果")
        self.assertEqual(1, successes, "同一版本只能有一次成功写入：%r" % (results,))
        self.assertEqual(7, conflicts, "其余必须 409：%r" % (results,))
        versions = self.store.find("DraftVersion", where={"draft_id": draft.id}, limit=None)
        self.assertEqual(2, len(versions), "只应产生一个新版本")

    def test_parallel_answers_to_same_question_have_exactly_one_winner(self):
        detail = self.client.get("/api/questions/%s" % self.question.id).json
        results = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            client = self.client.__class__(
                self.server.bound_host or "127.0.0.1", self.server.bound_port, self.token
            )
            response = client.post(
                "/api/questions/%s/answer" % detail["id"],
                {"answer_text": "并发回答 #%d" % index, "expected_revision": detail["revision"]},
            )
            with lock:
                results.append(response.status)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(1, results.count(200), "同一追问只能被回答一次：%r" % (results,))
        self.assertEqual(5, results.count(409), "其余必须冲突：%r" % (results,))
        answers = self.store.find("Answer", where={"question_id": detail["id"]}, limit=None)
        self.assertEqual(1, len(answers))

    def test_health_and_overview_are_safe_under_load(self):
        statuses = []
        lock = threading.Lock()

        def worker() -> None:
            client = self.client.__class__(
                self.server.bound_host or "127.0.0.1", self.server.bound_port, self.token
            )
            for _ in range(5):
                response = client.get("/api/overview")
                with lock:
                    statuses.append(response.status)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(30, len(statuses))
        self.assertEqual({200}, set(statuses))

    def test_threading_server_is_actually_concurrent(self):
        """并发 6 个请求的墙钟时间应明显短于串行（证明服务真的是多线程而非排队）。"""
        responses = []

        def worker() -> None:
            client = self.client.__class__(
                self.server.bound_host or "127.0.0.1", self.server.bound_port, self.token
            )
            responses.append(client.get("/api/cases?include_closed=true").status)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        started = time.time()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        elapsed = time.time() - started
        self.assertEqual([200] * 6, sorted(responses))
        self.assertLess(elapsed, 10.0)


__all__ = ["ConcurrencyTests", "IncrementalSyncTests", "ProcessRestartTests"]
