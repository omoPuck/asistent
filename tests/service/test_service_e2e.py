"""MVP-8 纵向端到端测试：一封通知邮件从进来到归档，全程走**真模块**（SPEC §873-877）。

这一组测试要钉死四件最容易"看起来跑通其实没跑"的事：

1. **理解真的是理解层做的**：`Question`/`Draft` 由 `asistent.understand` 产出，
   时间语义（10月16日 18:00 → `2026-10-16T10:00:00.000Z`）落在存储里；
2. **外发恰好一次**：`submit` 账本行数 == 1、`attempt == 1`、审批 `CONSUMED`，
   并且**再跑一轮不会重发**（跨进程：`sqlite` 后端每轮一个新进程）；
3. **状态只来自存储**：每轮换进程、控制台全程没连过，服务照样知道该干什么；
4. **归档逐文件校验通过**：manifest 里每个文件的 sha256 == 冻结文件 == 远端回读内容。

分轮驱动方式由 `ServiceDriver` 统一：`sqlite` = 每轮新进程；`reference` = 同进程多轮
（内存库无法跨进程，真重启类证据见 `test_service_crash.py`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List, Optional

from asistent.archive.engine import ARCHIVE_BASE_DIR, ArchiveLayout, remote_component
from asistent.console.auth import TokenAuth
from asistent.console.service import ConsoleService
from asistent.contracts.enums import ActionState, ApprovalState, ArchiveState, CaseState, DraftState
from asistent.contracts.pan import LocalDirPanClient
from asistent.service import config as config_module
from asistent.service.health import WAITING_APPROVAL, WAITING_NONE, WAITING_PAN, WAITING_UNKNOWN, snapshot_pending
from tests.service.support import (
    CLEAN_NOTICE_BODY,
    FAKE_TOKEN,
    SECRET_MARKERS,
    ServiceDriver,
    ServiceWorkbench,
    audit_rows,
    find_phase,
    store_is_sqlite,
    submit_lines,
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    with open(path, "rb") as handle:
        return _sha256_bytes(handle.read())


class _E2EBase(unittest.TestCase):
    """公共：一个仓库外工作区 + 分轮驱动 + 读断言用的存储快照。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mvp8-e2e-", ignore_cleanup_errors=True)
        self.root = self._tmp.name
        self.bench = ServiceWorkbench(self.root)
        self.driver = ServiceDriver(self.bench)
        self._stores: List[Any] = []
        self.plain = self.bench.default_config()
        self.sending = self.bench.sending_config()
        self.archiving = self.bench.archiving_config()
        self.call_log = os.path.join(self.bench.state_dir, "outbound-calls.jsonl")

    def tearDown(self) -> None:
        self.driver.close()
        for store in reversed(self._stores):
            closer = getattr(store, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        self._stores = []
        self._tmp.cleanup()

    # ------------------------------------------------------------------ 工具
    def store_for_read(self) -> Any:
        """测试侧的读句柄；统一登记，`tearDown` 里关干净（Windows 上不关会锁住临时目录）。"""
        store = self.driver.store()
        self._stores.append(store)
        return store
    def snapshot(self, **facts: Any) -> Dict[str, Any]:
        """把存储里关心的东西一次性读成纯数据（换进程也不影响断言）。"""
        store = self.store_for_read()
        if True:
            cases = list(store.find("Case", limit=None))
            documents = list(store.find("SourceDocument", limit=None))
            questions = list(store.find("Question", limit=None))
            answers = list(store.find("Answer", limit=None))
            drafts = list(store.find("Draft", limit=None))
            versions = list(store.find("DraftVersion", limit=None))
            approvals = list(store.find("Approval", limit=None))
            actions = list(store.find("ExternalAction", limit=None))
            attempts = list(store.find("ActionAttempt", limit=None))
            jobs = list(store.find("ArchiveJob", limit=None))
            facts_rows = list(store.find("Fact", limit=None))
            payload = {
                "cases": [
                    {
                        "id": item.id,
                        "state": str(item.state),
                        "archive_state": str(item.archive_state),
                        "rule_ids": list(item.rule_ids or []),
                        "blocked_reason": item.blocked_reason,
                        "waiting_reason": getattr(item, "waiting_reason", None),
                        "draft_ids": list(item.draft_ids or []),
                        "action_ids": list(item.action_ids or []),
                    }
                    for item in cases
                ],
                "documents": [{"id": item.id, "index_state": str(item.index_state)} for item in documents],
                "questions": [{"id": item.id, "state": str(item.state), "affects": list(item.affects or [])} for item in questions],
                "answers": [{"id": item.id, "question_id": item.question_id} for item in answers],
                "drafts": [{"id": item.id, "state": str(item.state), "current_version_id": item.current_version_id} for item in drafts],
                "versions": [
                    {"id": item.id, "draft_id": item.draft_id, "version_no": int(item.version_no), "content_hash": item.content_hash}
                    for item in versions
                ],
                "approvals": [
                    {"id": item.id, "state": str(item.state), "action_id": item.action_id, "targets": list(item.target_object_ids or [])}
                    for item in approvals
                ],
                "actions": [
                    {
                        "id": item.id,
                        "state": str(item.state),
                        "kind": item.kind,
                        "connector": item.connector,
                        "idempotency_key": item.idempotency_key,
                        "result_class": getattr(item, "result_class", ""),
                        "attempt_ids": list(item.attempt_ids or []),
                        "approval_id": item.approval_id,
                        "summary_digest": getattr(item, "summary_digest", ""),
                    }
                    for item in actions
                ],
                "attempts": [{"id": item.id, "state": str(item.state), "action_id": item.action_id} for item in attempts],
                "archive_jobs": [
                    {"id": item.id, "state": str(item.state), "scope_ref": item.scope_ref, "archive_id": item.archive_id}
                    for item in jobs
                ],
                "facts": [
                    {"id": item.id, "links": dict(item.links or {}), "temporal_fields": list(item.temporal_fields or [])}
                    for item in facts_rows
                ],
                "pending": snapshot_pending(store),
            }
        payload.update(facts)
        return payload

    def console(self, store: Any) -> ConsoleService:
        return ConsoleService(store, TokenAuth(FAKE_TOKEN), server_name="mvp8-test")

    def open_store_for_console(self) -> Any:
        """控制台写操作要一份可写存储（测试扮演"手机端"）。"""
        if store_is_sqlite():
            from asistent.storage.sqlite_store import open_store

            store = open_store(self.bench.db_path)
            self._stores.append(store)
            return store
        return self.store_for_read()


# --------------------------------------------------------------------------------------
# 主链路
# --------------------------------------------------------------------------------------


class TestVerticalE2E(_E2EBase):
    """§873-877：邮件进来 → 理解 → 追问/草稿 → 审批 → 恰好一次外发 → 归档。"""

    def test_full_vertical_flow(self) -> None:
        # ---- 1) 第一轮：同步 + 索引 + 理解（没有任何客户端）
        self.bench.add_notice("notice-01.eml", subject="周末科普讲座报名确认（含活动时间）")
        first = self.driver.round("service.json")

        sync = find_phase(first, "sync")
        self.assertTrue(sync["ok"], sync)
        self.assertEqual(sync["detail"]["documents_created"], 1)
        self.assertEqual(sync["detail"]["listed"], 1)
        ingest = find_phase(first, "ingest")
        self.assertEqual(ingest["detail"]["index"]["indexed"], 1, "新邮件必须进 FTS5 索引")

        understand = find_phase(first, "understand")
        self.assertEqual(len(understand["detail"]["analyzed"]), 1)
        analyzed = understand["detail"]["analyzed"][0]
        self.assertTrue(analyzed["draftable"])
        self.assertEqual(analyzed["case_state"], str(CaseState.READY_FOR_REVIEW))
        self.assertEqual(analyzed["counts"]["Draft"], 1)
        self.assertNotIn("Question", analyzed["counts"], "干净通知不该产生追问")

        after_first = self.snapshot()
        case = after_first["cases"][0]
        self.assertEqual(case["state"], str(CaseState.READY_FOR_REVIEW))
        self.assertEqual(len(after_first["documents"]), 1)
        self.assertEqual(after_first["documents"][0]["index_state"], "INDEXED")
        self.assertEqual(len(after_first["drafts"]), 1)
        self.assertEqual(len(after_first["versions"]), 1)
        self.assertEqual(after_first["approvals"], [], "服务自己不批准任何东西")
        self.assertEqual(after_first["actions"], [])
        # 时间语义真的落到了事实里（不是"提了一嘴"）
        temporal = [
            field
            for fact in after_first["facts"]
            for field in fact["temporal_fields"]
            if field.get("semantic") in ("registration_deadline", "event_start")
        ]
        by_semantic = {field["semantic"]: field for field in temporal}
        self.assertEqual(by_semantic["registration_deadline"]["normalized_utc"], "2026-10-16T10:00:00.000Z")
        self.assertEqual(by_semantic["event_start"]["normalized_utc"], "2026-10-17T06:00:00.000Z")

        # 健康快照：明确说"在等用户审批"，且计数现场从存储算出来
        status = self.driver.status("service.json")
        self.assertEqual(status["waiting_on"], [WAITING_APPROVAL])
        self.assertEqual(status["pending"]["cases_awaiting_approval"], 1)
        self.assertEqual(status["rounds_completed"], 1)
        self.assertEqual(status["submit_count_on_disk"], 0)
        self.assertEqual(status["last_sync"]["documents_created"], 1)
        state_path = os.path.join(self.bench.state_dir, "service-state.json")
        self.assertTrue(os.path.isfile(state_path))
        with open(state_path, encoding="utf-8") as handle:
            raw_state = handle.read()
        self.assertIn("waiting_on", raw_state)

        # ---- 2) 手机端审批：先证明"看起来像批准的东西"会被拒收
        store = self.open_store_for_console()
        console = self.console(store)
        draft_id = after_first["drafts"][0]["id"]
        with self.assertRaises(Exception) as ctx:
            console.submit_approval({"draft_id": draft_id, "version_no": 1})
        self.assertIn("confirm", str(ctx.exception))
        with self.assertRaises(Exception) as ctx2:
            console.submit_approval({"draft_id": draft_id, "confirm": True})
        self.assertIn("version_no", str(ctx2.exception))
        approved = console.submit_approval({"draft_id": draft_id, "version_no": 1, "confirm": True})
        self.assertEqual(approved["approval"]["state"], str(ApprovalState.ACTIVE))
        self.assertEqual(approved["approval"]["state"], "ACTIVE")
        user_approval_id = approved["approval"]["id"]
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()

        # ---- 3) 第二轮（受控假通道）：恰好外发一次
        second = self.driver.round("service-send.json")
        prepared = find_phase(second, "prepare")["detail"]
        self.assertEqual(len(prepared["prepared"]), 1, prepared)
        outbound = find_phase(second, "outbound")["detail"]
        self.assertEqual(len(outbound["executed"]), 1)
        outcome = outbound["executed"][0]
        self.assertEqual(outcome["result_class"], "SUCCEEDED")
        self.assertEqual(outcome["sender_call_count"], 1)
        self.assertEqual(outcome["attempt_no"], 1)
        self.assertEqual(second["sender_call_count"], 1)

        sends = submit_lines(self.call_log)
        self.assertEqual(len(sends), 1, "通道只应被提交一次")
        self.assertEqual(sends[0]["idempotency_key"], outcome["idempotency_key"])

        after_send = self.snapshot()
        self.assertEqual(after_send["cases"][0]["state"], str(CaseState.SUCCEEDED))
        action = after_send["actions"][0]
        self.assertEqual(action["state"], str(ActionState.SUCCEEDED))
        self.assertEqual(len(action["attempt_ids"]), 1)
        self.assertTrue(action["idempotency_key"].startswith("action:"))
        # 用户的审批是权威依据；动作级审批被执行消费掉（一次性执行权）
        states = {item["id"]: item["state"] for item in after_send["approvals"]}
        self.assertEqual(states[user_approval_id], str(ApprovalState.ACTIVE), "用户那张审批是证据，保留")
        action_bound = [item for item in after_send["approvals"] if item["action_id"] == action["id"]]
        self.assertTrue(action_bound)
        self.assertTrue(
            all(item["state"] in (str(ApprovalState.CONSUMED), str(ApprovalState.INVALIDATED)) for item in action_bound),
            "动作级审批必须已被消费（否则可被第二次领取）",
        )
        self.assertEqual(
            after_send["pending"]["actions_authorized"], 0, "没有残留的可执行动作"
        )
        exec_audits = [
            row
            for row in audit_rows(self.store_for_read(), case_id=after_send["cases"][0]["id"])
            if row["action"] in ("EXECUTE", "ARCHIVE")
        ]
        self.assertTrue(exec_audits, "执行过程必须有审计")

        # ---- 4) 第三轮：同一份配置再跑一次，**不得重发**
        third = self.driver.round("service-send.json")
        self.assertEqual(find_phase(third, "outbound")["detail"]["executed"], [])
        # "不重发"的铁证是**盘上账本**与 attempt 数（与后端无关）。
        # `sender_call_count` 是"本进程调用过几次通道"：sqlite 后端每轮一个新进程，第三轮必须是 0；
        # reference 后端同一进程复用运行时，计数天然累计，所以只在 sqlite 上断言它。
        if store_is_sqlite():
            self.assertEqual(third["sender_call_count"], 0, "第三轮不得再调通道")
        self.assertEqual(int(third["submit_count_on_disk"]), 1)
        self.assertEqual(len(submit_lines(self.call_log)), 1, "第二次执行尝试必须被拒绝，通道不得再被调用")
        after_third = self.snapshot()
        self.assertEqual(len(after_third["attempts"]), 1, "只允许一个 attempt")

        # ---- 5) 第四轮：归档 + 逐文件哈希校验
        fourth = self.driver.round("service-archive.json")
        archive_detail = find_phase(fourth, "archive")["detail"]
        self.assertEqual(len(archive_detail["archived"]), 1, archive_detail)
        record = archive_detail["archived"][0]
        self.assertEqual(record["state"], str(ArchiveState.VERIFIED))
        self.assertTrue(record["ok"])
        self.assertEqual(record["failed"], 0)
        self.assertEqual(record["verified"], 2, "邮件原文 + case.json 两个对象都回读校验过")
        self.assertEqual(record["content_origin_summary"]["RAW_BYTES"], 2)
        archive_id = record["archive_id"]

        self._verify_archive_files(archive_id, after_send["cases"][0]["id"])

        final = self.snapshot()
        self.assertEqual(final["cases"][0]["archive_state"], str(ArchiveState.VERIFIED))
        self.assertEqual(len(final["archive_jobs"]), 1)
        self.assertEqual(final["archive_jobs"][0]["state"], str(ArchiveState.VERIFIED))
        self.assertEqual(self.driver.status("service-archive.json")["waiting_on"], [WAITING_NONE])

    # ------------------------------------------------------------------ 归档校验
    def _verify_archive_files(self, archive_id: str, case_id: str) -> None:
        layout = ArchiveLayout()
        shape = remote_component(archive_id)
        manifest_path = os.path.join(self.bench.archive_root, *layout.manifest_path(archive_id).split("/"))
        self.assertTrue(os.path.isfile(manifest_path), "manifest 必须落在远端目录里：%s" % manifest_path)
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["scope"], "case")
        self.assertEqual(manifest["scope_ref"], case_id)
        self.assertEqual(manifest["archive_id"], archive_id)
        self.assertEqual(manifest["policy"], None, "ArchivePolicy（U-6）尚未经用户确认：如实记录为 null")
        self.assertIn("policy_gate", manifest, "未确认策略这件事要写进 manifest")
        self.assertTrue(manifest["secret_scan"]["clean"], "归档前必须做过秘密扫描且干净")

        remote_root = os.path.join(self.bench.archive_root, *os.path.join(layout.archive_dir(archive_id)).split("/"))
        objects_dir = os.path.join(remote_root, "objects")
        entries = manifest["entries"]
        self.assertGreaterEqual(len(entries), 2)
        for entry in entries:
            relative = entry["relative_path"]
            target = os.path.join(objects_dir, *relative.split("/"))
            self.assertTrue(os.path.isfile(target), "远端缺少对象：%s" % relative)
            self.assertEqual(_sha256_file(target), entry["sha256"], "远端对象哈希与 manifest 不一致：%s" % relative)
            self.assertEqual(os.path.getsize(target), entry["size_bytes"])
            self.assertIn(entry.get("content_origin"), ("RAW_BYTES", "METADATA_PROJECTION"))

        # 冻结目录（上传源）与远端字节级一致：manifest 的哈希说的是"磁盘上这两个东西"
        from asistent.service.phases import staging_dir_for

        staging = staging_dir_for(config_module.load_config(self.archiving), case_id)
        self.assertTrue(os.path.isdir(staging), "归档必须先冻结快照再上传：%s" % staging)
        staged_names = sorted(name for name in os.listdir(staging) if os.path.isfile(os.path.join(staging, name)))
        for name in staged_names:
            remote = os.path.join(objects_dir, name)
            self.assertTrue(os.path.isfile(remote), "冻结文件没有出现在远端：%s" % name)
            self.assertEqual(_sha256_file(os.path.join(staging, name)), _sha256_file(remote))

        marker = os.path.join(remote_root, "_COMPLETE.json")
        self.assertTrue(os.path.isfile(marker), "只有全部对象校验通过才会写 _COMPLETE 标记")
        with open(marker, "r", encoding="utf-8") as handle:
            complete = json.load(handle)
        self.assertEqual(complete.get("archive_id"), archive_id)

        # 通过真实通道 API 回读 manifest（不是直接读本地文件）：逐文件哈希再对一次
        client = LocalDirPanClient(self.bench.archive_root)
        readback_path = os.path.join(self._tmp.name, "readback-manifest.json")
        readback = client.get(layout.manifest_path(archive_id), readback_path)
        self.assertTrue(readback.ok, readback.error)
        with open(readback_path, "r", encoding="utf-8") as handle:
            remote_manifest = json.load(handle)
        self.assertEqual(remote_manifest["manifest_digest"], manifest["manifest_digest"])
        self.assertEqual(
            {item["relative_path"]: item["sha256"] for item in remote_manifest["entries"]},
            {item["relative_path"]: item["sha256"] for item in entries},
        )


# --------------------------------------------------------------------------------------
# 常驻：没有客户端也在干活 + 状态只来自存储
# --------------------------------------------------------------------------------------


class TestResidencyWithoutClient(_E2EBase):
    """§875：关掉桌面窗口/没有任何客户端连着，服务照常轮转并推进事务。"""

    def test_two_consecutive_rounds_keep_working_without_any_client(self) -> None:
        self.bench.add_notice("notice-01.eml", subject="第一批：报名确认")
        first = self.driver.round("service.json")
        self.assertEqual(find_phase(first, "sync")["detail"]["documents_created"], 1)
        status_first = self.driver.status("service.json")
        self.assertEqual(status_first["rounds_completed"], 1)
        self.assertEqual(status_first["console_url"], "", "这一组刻意不开控制台：没有任何客户端")

        # 第二轮：没有任何客户端，服务自己发现"又有新邮件"
        self.bench.add_notice(
            "notice-02.eml",
            subject="第二批：新的报名确认",
            message_id="mvp8-residency-02@example.org",
            from_addr="library@example.org",
        )
        second = self.driver.round("service.json")
        self.assertEqual(find_phase(second, "sync")["detail"]["documents_created"], 1)
        self.assertEqual(len(find_phase(second, "understand")["detail"]["analyzed"]), 1)

        snapshot_data = self.snapshot()
        self.assertEqual(len(snapshot_data["cases"]), 2, "两封不同邮件必须各自成事务")
        self.assertEqual(len(snapshot_data["drafts"]), 2)
        status_second = self.driver.status("service.json")
        self.assertEqual(status_second["rounds_completed"], 2)
        self.assertEqual(status_second["pending"]["cases_awaiting_approval"], 2)
        self.assertEqual(status_second["waiting_on"], [WAITING_APPROVAL])

        # 状态快照落在仓库外的 state 目录，且不含任何凭据
        with open(os.path.join(self.bench.state_dir, "service-state.json"), encoding="utf-8") as handle:
            state_text = handle.read()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, state_text.lower())
        self.assertNotIn(FAKE_TOKEN, state_text)
        self.assertIn("rounds_completed", state_text)

    def test_third_round_is_a_clean_noop(self) -> None:
        """已经处理过的邮件不该被反复理解：幂等跳过（否则每轮都刷审计、烧 CPU）。"""
        self.bench.add_notice("notice-01.eml", subject="报名确认")
        self.driver.round("service.json")
        again = self.driver.round("service.json")
        self.assertEqual(find_phase(again, "sync")["detail"]["documents_created"], 0)
        self.assertEqual(find_phase(again, "sync")["detail"]["duplicates_skipped"], 0)
        detail = find_phase(again, "understand")["detail"]
        self.assertEqual(detail["analyzed"], [])
        self.assertEqual(detail["skipped_count"], 1)
        # 已经出过草稿、在等审批的事务不该被反复理解
        self.assertIn("不在需要理解的状态集合", detail["skipped"][0]["why"])


# --------------------------------------------------------------------------------------
# 规则修正 → 下一封同类通知命中新规则版本（§877 第 3 条）
# --------------------------------------------------------------------------------------


class TestGrowthRuleCorrection(_E2EBase):
    """规则真的参与理解，且**版本号**能在审计里查出来（不是"Agent 自称遵守了规则"）。"""

    RULE_SENDER = "rules@example.org"

    def _add_rules_notice(self, name: str, *, message_id: str, second_batch: bool = False) -> None:
        self.bench.add_notice(
            name,
            subject="规则样本文档：活动报名确认（%s）" % name,
            body=CLEAN_NOTICE_BODY,
            from_addr=self.RULE_SENDER,
            from_name="规则样例发件人",
            message_id=message_id,
        )

    def _correction(self, *, statement: str, user_message: str) -> Any:
        """一次"用户纠正"（规则与版本由 `asistent/growth` 决定，本层不自己编号）。"""
        from asistent.growth import Correction, RuleScope, EFFECT_REQUIRE_QUESTION

        return Correction(
            statement=statement,
            user_message=user_message,
            correction_of="把'活动开始时间'当成'活动结束时间'用",
            scope=RuleScope(
                effect=EFFECT_REQUIRE_QUESTION,
                slots=("event_start", "event_end"),
                kind="contact",
                ref=self.RULE_SENDER,
            ),
        )

    def test_rule_version_is_visible_in_audit_and_grows_after_correction(self) -> None:
        from asistent.growth import (
            confirm_rule,
            current_version,
            enable_new_version,
            parse_scope,
            propose_rule,
            rule_versions,
        )

        # 1) 还没有规则：第一封同类通知正常理解，无规则命中
        self._add_rules_notice("rules-01.eml", message_id="mvp8-rules-01@example.org")
        self.driver.round("service.json")
        store = self.open_store_for_console()
        cases = list(store.find("Case", limit=None))
        self.assertEqual(len(cases), 1)
        self.assertEqual(self._rule_hits(store, cases[0].id), [])

        # 2) 用户纠正 → 规则 v1 生效
        rule_v1 = propose_rule(
            store,
            self._correction(
                statement="通知只有活动开始时间而没有结束时间时，必须追问结束时间",
                user_message="这种通知总会漏掉结束时间，别自己猜，先问我",
            ),
        )
        scope = parse_scope(str(rule_v1.scope_ref))
        confirmed_v1 = confirm_rule(store, rule_v1.id, confirmed_by="user")
        self.assertEqual(int(confirmed_v1.version), 1)

        # 3) 下一封同类通知：规则 v1 命中，且**版本号 1** 出现在审计里
        self._add_rules_notice("rules-02.eml", message_id="mvp8-rules-02@example.org")
        second = self.driver.round("service.json")
        analyzed = find_phase(second, "understand")["detail"]["analyzed"]
        self.assertEqual(len(analyzed), 1)
        self.assertTrue(analyzed[0]["rule_hits"], "规则必须真的参与理解")
        self.assertIn("v1", analyzed[0]["rule_hits"][0])

        store = self.open_store_for_console()
        case_two = self._case_from_document(store, analyzed[0]["document_id"])
        self.assertEqual(self._rule_hits(store, case_two.id), [(rule_v1.id, 1)])
        hits_v1 = [row for row in audit_rows(store, case_id=case_two.id) if row["action"] == "RULE_HIT"]
        self.assertEqual(hits_v1[0]["detail"]["rule_version"], 1)
        self.assertEqual(hits_v1[0]["detail"]["rule_id"], rule_v1.id)
        # 命中带来了追问（缺结束时间）→ 事务等用户回答（不是"自己编一个结束时间"）
        questions = [item for item in store.find("Question", limit=None) if item.case_id == case_two.id]
        self.assertTrue(questions)
        self.assertEqual(str(questions[0].state), "OPEN")
        case_two_now = store.get("Case", case_two.id)
        self.assertEqual(str(case_two_now.state), str(CaseState.WAITING_FOR_USER))
        self.assertIn(rule_v1.id, list(case_two_now.rule_ids or []), "事务要能反查命中的规则")
        # 规则驱动的追问必须自证来源：问题正文里写着规则 id 与版本号
        self.assertIn(rule_v1.id, questions[0].question)
        self.assertIn("v1", questions[0].question)
        # 事实与规则的关联（Fact.links["rule_version"]）
        linked = [
            fact
            for fact in store.find("Fact", limit=None)
            if fact.case_id == case_two.id and "rule_version" in dict(fact.links or {})
        ]
        self.assertTrue(linked, "命中规则的事实必须带 rule_version 链接")

        # 4) 用户再纠正一次 → 同一逻辑规则出 v2；下一封同类通知命中 v2
        rule_v2 = enable_new_version(
            store,
            self._correction(
                statement="通知只有活动开始时间而没有结束时间时，必须追问结束时间（并给出原文片段）",
                user_message="追问时把原文那句一起贴出来，我看得快一点",
            ),
        )
        self.assertEqual(int(rule_v2.version), 2, "同一逻辑规则的修正是**版本递增**，不是新规则")
        self.assertNotEqual(rule_v2.id, rule_v1.id, "每个版本有独立的 ID")
        versions = sorted(rule_versions(store, scope), key=lambda item: int(item.version))
        self.assertEqual([int(item.version) for item in versions], [1, 2])
        self.assertEqual(
            [str(item.state) for item in versions],
            ["SUPERSEDED", "ENABLED"],
            "旧版本必须被标成已替代（不能两个版本同时生效）",
        )
        self.assertEqual(int(current_version(store, scope).version), 2)

        self._add_rules_notice("rules-03.eml", message_id="mvp8-rules-03@example.org")
        third = self.driver.round("service.json")
        analyzed_third = find_phase(third, "understand")["detail"]["analyzed"]
        self.assertEqual(len(analyzed_third), 1)
        self.assertTrue(analyzed_third[0]["rule_hits"])
        self.assertIn("v2", analyzed_third[0]["rule_hits"][0], "这一封必须命中 v2，而不是还在用 v1")

        store = self.open_store_for_console()
        case_three = self._case_from_document(store, analyzed_third[0]["document_id"])
        self.assertEqual(
            self._rule_hits(store, case_three.id),
            [(rule_v2.id, 2)],
            "修正后的下一封同类通知必须命中**新版本**，且版本号可查",
        )

    # ------------------------------------------------------------------ 内部
    def _case_from_document(self, store: Any, document_id: str) -> Any:
        document = store.get("SourceDocument", document_id)
        for case in store.find("Case", limit=None):
            if document.id in list(case.source_document_ids or []):
                return case
        raise AssertionError("找不到文档所属事务：%s" % document_id)

    def _rule_hits(self, store: Any, case_id: str) -> List[Any]:
        return [
            (row["detail"]["rule_id"], int(row["detail"]["rule_version"]))
            for row in audit_rows(store, case_id=case_id)
            if row["action"] == "RULE_HIT"
        ]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
