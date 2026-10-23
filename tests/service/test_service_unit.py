"""MVP-8 单元测试：配置、健康快照、一轮的隔离性、追问回答兜底、夹具邮件源。

这些测试**不依赖任何子进程**，因此在 `reference` 与 `sqlite` 两个后端上都跑
（`ASISTENT_STORE=reference|sqlite`）；需要"真换进程/真被 kill"的部分放在
`test_service_e2e.py` / `test_service_crash.py`，只在落盘后端上跑。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock as mock
from typing import Any, Dict, List, Optional

from asistent.contracts import (
    ActorKind,
    AuditEvent,
    Case,
    CaseState,
    Citation,
    Question,
    QuestionState,
    SourceDocument,
    action_idempotency_key,
    digest_of,
    new_id,
)
from asistent.contracts.enums import ActionState, ArchiveState, ApprovalState, DraftState, TaskState
from asistent.contracts.objects import Answer, Approval, Draft, DraftVersion, ExternalAction, Task
from asistent.service import config as config_module
from asistent.service import health as health_module
from asistent.service import phases as phases_module
from asistent.service.answers import (
    ANSWER_RESOLVABLE_KINDS,
    AnswerAwareUnderstander,
    answered_questions,
    record_answer_facts,
)
from asistent.service.config import CHANNEL_NONE
from asistent.service.health import ServiceState, infer_waiting_on, snapshot_pending
from asistent.contracts.mail import FixtureMailSource as ContractFixtureMailSource
from asistent.service.phases import RunContext, run_phases
from asistent.understand.model import (
    CitationRef,
    DraftCandidate,
    QuestionCandidate,
    SourceText,
    UnderstandingResult,
)
from tests.contracts.helpers import store_factory
from tests.service.support import CLEAN_NOTICE_BODY, ServiceWorkbench, notice_eml


def _make_store(**kwargs: Any) -> Any:
    if os.environ.get("ASISTENT_STORE", "reference").strip().lower() == "sqlite":
        directory = kwargs.pop("directory", None) or tempfile.mkdtemp(prefix="mvp8-unit-")
        return store_factory(path=os.path.join(directory, "asistent.db"))
    return store_factory()


# --------------------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------------------


class TestConfig(unittest.TestCase):
    """配置在仓库外 + 安全默认值 + 不认识的值必须显式失败。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mvp8-config-")
        self.root = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_defaults_are_safe(self) -> None:
        config = config_module.load_config(payload={})
        self.assertEqual(config.store, os.environ.get("ASISTENT_STORE", "sqlite").strip().lower() or "sqlite")
        self.assertFalse(config.mail.enabled, "默认绝不连接邮箱")
        self.assertEqual(config.outbound.channel, CHANNEL_NONE, "默认拒绝外呼")
        self.assertFalse(config.archive.enabled, "默认不归档")
        self.assertEqual(config.interval_seconds, config_module.DEFAULT_INTERVAL_SECONDS)
        self.assertTrue(config.db_path.endswith(os.path.join("asistent-var", "data", "asistent.db")))
        described = json.dumps(config.describe(), ensure_ascii=False)
        # 口径同 `SECRET_MARKERS`：禁的是**值形态**（`password=`），不是"凭据"这个概念的影子。
        for forbidden in ("token_value", "password=", "authcode=", "bduss", "cookie:"):
            self.assertNotIn(forbidden, described)

    def test_unknown_channel_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            config_module.load_config(payload={"outbound": {"channel": "smtp"}})
        with self.assertRaises(ValueError):
            config_module.load_config(payload={"archive": {"channel": "s3"}})

    def test_bdpan_remote_root_must_stay_a_remote_path(self) -> None:
        """`archive.channel=bdpan` 时 `root` 是**网盘上的**目录前缀，不能变成本机绝对路径。

        设备取证实录里它是 `/home/asistent/asistent-var/config/个人助手归档`——那是一个本机路径，
        而归档清单会把它当成"远端位置"记下来（清单的核心作用就是说明副本在哪）。
        本机测不出来：本地跑的都是 `local_dir` 通道，那种通道本来就需要绝对路径。
        """
        config = config_module.load_config(
            payload={"archive": {"enabled": True, "channel": "bdpan", "root": "个人助手归档"}}
        )
        self.assertEqual(config.archive.root, "个人助手归档")
        self.assertFalse(os.path.isabs(config.archive.root), "远端前缀绝不能被解析成本机绝对路径")
        # 前导/尾随斜杠要归一化，但仍然是个远端相对路径
        trimmed = config_module.load_config(
            payload={"archive": {"enabled": True, "channel": "bdpan", "root": "/个人助手归档/"}}
        )
        self.assertEqual(trimmed.archive.root, "个人助手归档")
        # 真正本地的通道照旧解析成绝对路径（别把这条修过头）
        local = config_module.load_config(
            payload={"archive": {"enabled": True, "channel": "local_dir", "root": "var/archive"}}
        )
        self.assertTrue(os.path.isabs(local.archive.root))

    def test_relative_paths_resolve_against_config_dir(self) -> None:
        path = os.path.join(self.root, "cfg", "service.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"var_root": self.root, "db_path": "rel/asistent.db"}, handle)
        config = config_module.load_config(path)
        self.assertEqual(config.db_path, os.path.join(os.path.dirname(path), "rel", "asistent.db"))
        self.assertEqual(config.config_path, path)

    def test_env_interval_override(self) -> None:
        with mock.patch.dict(os.environ, {"ASISTENT_SERVICE_INTERVAL": "7"}):
            config = config_module.load_config(payload={})
        self.assertEqual(config.interval_seconds, 7)

    def test_interval_never_zero(self) -> None:
        config = config_module.load_config(payload={"interval_seconds": 0})
        self.assertGreaterEqual(config.interval_seconds, 1, "0 秒间隔会变成空转死循环：必须夹住")


# --------------------------------------------------------------------------------------
# 健康快照
# --------------------------------------------------------------------------------------


class TestHealthSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _make_store()

    def test_pending_counts_only_open_cases(self) -> None:
        case = self.store.put(Case(goal="处理通知"))
        open_case = self.store.put(Case(goal="还在跑", state=CaseState.WAITING_FOR_USER))
        self.store.put(Case(goal="已完成", state=CaseState.SUCCEEDED))
        question = self.store.put(Question(case_id=open_case.id, question="参加几个人？"))
        self.store.put(
            Question(case_id=open_case.id, question="已答过", state=QuestionState.ANSWERED)
        )
        self.store.put(Draft(case_id=case.id, state=DraftState.OPEN))
        self.store.put(Draft(case_id=open_case.id, state=DraftState.REVOKED))
        self.store.put(Task(case_id=open_case.id, state=TaskState.PENDING))
        self.store.put(Task(case_id=open_case.id, state=TaskState.DONE))
        counts = snapshot_pending(self.store)
        self.assertEqual(counts["cases_total"], 3)
        self.assertEqual(counts["cases_open"], 2)
        self.assertEqual(counts["questions_open"], 1)
        self.assertEqual(counts["drafts_open"], 1, "已完成事务的草稿不该算进待办")
        self.assertEqual(counts["tasks_open"], 1)
        self.assertIsNotNone(question)

    def test_archive_pending_requires_a_job_row(self) -> None:
        self.store.put(Case(goal="新事务"))  # archive_state 默认就是 PENDING
        counts = snapshot_pending(self.store)
        self.assertEqual(
            counts["archive_pending"],
            0,
            "没有 ArchiveJob 就不能说'在等网盘'（默认 PENDING 不是证据）",
        )

    def test_waiting_on_priority_and_values(self) -> None:
        self.assertEqual(infer_waiting_on({}, channel_ready=True), [health_module.WAITING_NONE])
        waiting = infer_waiting_on(
            {
                "questions_open": 1,
                "cases_awaiting_approval": 1,
                "actions_unknown": 1,
                "actions_authorized": 1,
                "archive_pending": 1,
            },
            channel_ready=False,
        )
        self.assertEqual(
            waiting,
            [
                health_module.WAITING_USER,
                health_module.WAITING_APPROVAL,
                health_module.WAITING_UNKNOWN,
                health_module.WAITING_CHANNEL,
                health_module.WAITING_PAN,
            ],
        )
        self.assertEqual(
            infer_waiting_on({"actions_authorized": 1}, channel_ready=True),
            [health_module.WAITING_NONE],
            "通道就绪时'已授权等执行'不是等用户",
        )

    def test_state_round_trip_and_corrupt_file(self) -> None:
        path = os.path.join(tempfile.mkdtemp(prefix="mvp8-state-"), "state", "service-state.json")
        state = ServiceState().begin(run_id="run-1", interval_seconds=5, state_path=path)
        state.pending = {"cases_open": 2}
        state.waiting_on = [health_module.WAITING_APPROVAL]
        state.record_error("sync", RuntimeError("通道不可用"))
        state.record_round({"ok": True, "phases": []})
        saved = state.save()
        self.assertEqual(saved, path)
        loaded = ServiceState.load(path)
        self.assertEqual(loaded.run_id, "run-1")
        self.assertEqual(loaded.rounds_completed, 1)
        self.assertEqual(loaded.pending["cases_open"], 2)
        self.assertEqual(loaded.last_error["phase"], "sync")
        self.assertFalse(loaded.running, "落盘快照里的 running 不代表进程还活着")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")
        broken = ServiceState.load(path)
        self.assertTrue(broken.notes, "坏文件必须被记录，不能让服务起不来")
        self.assertEqual(broken.rounds_completed, 0)


# --------------------------------------------------------------------------------------
# 一轮的隔离性（SPEC §13.2：一个阶段失败不得接管服务）
# --------------------------------------------------------------------------------------


class _StubStore:
    """`run_phases` 只需要"能把计数查空"的最小存储。"""

    def __init__(self) -> None:
        self.audits: List[AuditEvent] = []
        self.now_value = "2026-10-10T00:00:00.000Z"

    def find(self, object_type: str, where: Optional[Dict[str, Any]] = None, limit: Optional[int] = 100) -> List[Any]:
        return []

    def now(self) -> str:
        return self.now_value

    def append_audit(self, audit: AuditEvent) -> AuditEvent:
        self.audits.append(audit)
        return audit

    def stats(self) -> Dict[str, int]:
        return {}


def _context(store: Any) -> RunContext:
    return RunContext(
        config=config_module.load_config(payload={"var_root": tempfile.mkdtemp(prefix="mvp8-phases-")}),
        store=store,
        state=ServiceState().begin(run_id="run-x", interval_seconds=1),
    )


class TestRoundIsolation(unittest.TestCase):
    def test_phase_order_is_fixed(self) -> None:
        store = _StubStore()
        report = run_phases(_context(store), round_index=1)
        self.assertEqual(
            [item.name for item in report.phases],
            ["recover", "sweep", "sync", "ingest", "understand", "prepare", "outbound", "archive"],
        )
        self.assertTrue(report.ok, "全 skip 也算一轮干净地跑完")
        self.assertTrue(store.audits, "每轮都必须留一条审计（否则'跑过'无法自证）")

    def test_one_failing_phase_does_not_stop_the_round(self) -> None:
        store = _StubStore()
        context = _context(store)
        calls: List[str] = []
        original = phases_module.sweep_phase

        def boom(_ctx: RunContext) -> Any:
            calls.append("sweep")
            raise RuntimeError("模拟阶段故障：磁盘暂时不可写")

        with mock.patch.object(phases_module, "sweep_phase", boom):
            report = run_phases(context, round_index=1)
        self.assertIn("sweep", calls)
        self.assertFalse(report.ok)
        failed = report.phase("sweep")
        self.assertIsNotNone(failed)
        assert failed is not None
        self.assertEqual(failed.error_class, "RuntimeError")
        self.assertIsNotNone(report.phase("archive"), "后面阶段必须照常执行")
        self.assertEqual(context.state.last_error["phase"], "sweep")
        self.assertEqual(context.state.phase_failures["sweep"], 1)
        self.assertIsNotNone(original)

    def test_crash_class_exception_is_not_swallowed(self) -> None:
        from asistent.actions import SimulatedCrash

        store = _StubStore()

        def crash(_ctx: RunContext) -> Any:
            raise SimulatedCrash("被强杀")

        with mock.patch.object(phases_module, "sync_phase", crash):
            with self.assertRaises(SimulatedCrash):
                run_phases(_context(store), round_index=1)

    def test_recover_never_calls_the_sender(self) -> None:
        """恢复路径上**没有**外发：这是"不重发"的第一道闸门（第二道是 claim_action）。"""
        with open(phases_module.__file__, "r", encoding="utf-8") as handle:
            source = handle.read()
        body = source.split("def recover_phase")[1].split("\ndef ")[0]
        for forbidden in (".send(", "execute(", "execute_request"):
            self.assertNotIn(forbidden, body, "恢复阶段不得出现 %s" % forbidden)


# --------------------------------------------------------------------------------------
# 追问回答 → 重新理解（本层兜底）
# --------------------------------------------------------------------------------------


class _StubUnderstander:
    name = "stub"

    def __init__(self, result: UnderstandingResult) -> None:
        self.result = result
        self.calls = 0

    def analyze(self, request: Any) -> UnderstandingResult:
        self.calls += 1
        return self.result


def _candidate_draft() -> DraftCandidate:
    citation = CitationRef.require(
        Citation(
            source_object_id="doc-1",
            source_object_type="SourceDocument",
            path="mail/notice.eml",
            locator="body:3",
            content_hash=digest_of(["body", "报名截止时间：10月16日 18:00"]),
            supports="支持草稿中的截止时间",
        )
    )
    return DraftCandidate(body="您好：确认参加 2 人。", target_ref="kids@example.org", citations=[citation])


def _result(**kwargs: Any) -> UnderstandingResult:
    defaults: Dict[str, Any] = {
        "source_object_id": "doc-1",
        "path": "mail/notice.eml",
        "content_hash": digest_of(["raw"]),
        "questions": [],
        "drafts": [_candidate_draft()],
        "source_text": SourceText(raw_text="...", body_text="...", from_addr="kids@example.org"),
    }
    defaults.update(kwargs)
    return UnderstandingResult(**defaults)


class TestAnswerAwareUnderstander(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _make_store()
        self.case = self.store.put(Case(goal="处理通知"))
        self.document = self.store.put(
            SourceDocument(
                case_id=self.case.id,
                identity_key="k1",
                origin_path="mail/notice.eml",
                content_hash=digest_of(["raw"]),
            )
        )

    def _question(self, text: str, *, affects: List[str], state: str = QuestionState.OPEN) -> Any:
        return self.store.put(Question(case_id=self.case.id, question=text, affects=list(affects), state=state))

    def test_blocking_question_blocks_until_answered(self) -> None:
        text = "报名截止具体到几点？"
        question = self._question(text, affects=["时间字段:registration_deadline"])
        result = _result(
            questions=[QuestionCandidate(question=text, kind="missing_time", affects=["时间字段:registration_deadline"])]
        )
        base = _StubUnderstander(result)
        aware = AnswerAwareUnderstander(self.store, case_id=self.case.id, base=base)
        unresolved = aware.analyze(None)
        self.assertFalse(unresolved.draftable(), "没人回答时必须阻塞")
        self.assertTrue(unresolved.blocking_reasons())

        # 用户回答 → 下一轮重新分析不再把这条算作阻塞
        answer = self.store.put(Answer(question_id=question.id, case_id=self.case.id, answer_text="18:00"))
        question = self.store.get("Question", question.id)
        question.state = QuestionState.ANSWERED
        question.answer_id = answer.id
        self.store.put(question, expected_revision=question.revision, reason="用户已回答")
        self.assertEqual(len(answered_questions(self.store, self.case.id)), 1)

        resolved = aware.analyze(None)
        self.assertTrue(resolved.draftable(), "已回答的追问不得继续阻塞")
        self.assertEqual(resolved.blocking_reasons(), [])
        self.assertEqual(aware.resolved[0]["answer_id"], answer.id)
        self.assertEqual(len(resolved.questions), 1, "候选列表必须原样保留（下标即身份，不能删）")
        self.assertTrue(any("已由用户回答解除" in item for item in resolved.warnings))

    def test_conflict_question_is_never_resolved_by_an_answer(self) -> None:
        text = "两条规则冲突：以哪个时间为准？"
        question = self._question(text, affects=["时间字段:event_start"])
        answer = self.store.put(Answer(question_id=question.id, case_id=self.case.id, answer_text="用开始时间"))
        question = self.store.get("Question", question.id)
        question.state = QuestionState.ANSWERED
        question.answer_id = answer.id
        self.store.put(question, expected_revision=question.revision, reason="用户已回答")
        result = _result(questions=[QuestionCandidate(question=text, kind="conflict", affects=["时间字段:event_start"])])
        aware = AnswerAwareUnderstander(self.store, case_id=self.case.id, base=_StubUnderstander(result))
        resolved = aware.analyze(None)
        self.assertFalse(resolved.draftable(), "规则冲突必须由裁决路径处理，不能靠一条回答解除")
        self.assertEqual(aware.resolved, [])
        self.assertNotIn("conflict", ANSWER_RESOLVABLE_KINDS)

    def test_no_answers_leaves_existing_behaviour_untouched(self) -> None:
        aware = AnswerAwareUnderstander(self.store, case_id=self.case.id, base=_StubUnderstander(_result()))
        result = aware.analyze(None)
        self.assertIsInstance(result, UnderstandingResult)

    def _answer(self, text: str, *, affects: List[str]) -> Any:
        """造一条"已由用户回答"的追问，返回那个 Question（记账指向的是它）。"""
        question = self._question(text, affects=list(affects), state=QuestionState.ANSWERED)
        answer = self.store.put(Answer(question_id=question.id, case_id=self.case.id, answer_text="18:00"))
        question.answer_id = answer.id
        self.store.put(question, expected_revision=question.revision, reason="用户已回答")
        return question

    def test_same_text_different_slot_must_not_resolve(self) -> None:
        """CP-20261010-33：**同一句追问、不同 affects，绝不能靠文本猜配对**。

        这条用例在加退路删除之前是**红的**：旧代码会认为"已被回答"，于是
        `draftable()` 变 True —— 一个真实的缺口静默消失。
        风险方向是不对称的：漏配对的代价是"追问继续挡着"（看得见、能处理），
        错配对的代价是"不该放行的草稿被放行"（静默、且有外部后果）。
        """
        text = "活动开始时间以哪个为准？"
        # 用户回答的是"另一个对象"上的同一句追问
        answered_elsewhere = self._answer(text, affects=["时间字段:draft_A"])
        result = _result(
            questions=[QuestionCandidate(question=text, kind="missing_time", affects=["时间字段:draft_B"])]
        )
        aware = AnswerAwareUnderstander(self.store, case_id=self.case.id, base=_StubUnderstander(result))
        resolved = aware.analyze(None)
        self.assertFalse(resolved.draftable(), "affects 不同就不能解除阻塞")
        self.assertEqual(aware.resolved, [])
        # 不解除，但**要让用户看见原因**（静默不生效和答错对象一样糟）
        self.assertTrue(
            any("affects 上不是同一处" in item for item in resolved.warnings),
            "必须说明为什么回答没有生效：%r" % (resolved.warnings,),
        )
        rejected = [
            event for event in self.store.audit_trail(case_id=self.case.id)
            if getattr(event, "result", "") == "REJECTED"
        ]
        self.assertEqual(len(rejected), 1, "要有记账，否则事后查不出「为什么没解除」")
        self.assertEqual(
            rejected[0].object_id,
            answered_elsewhere.id,
            "记账要指向那句未采用的回答所在的追问",
        )
        self.assertEqual(rejected[0].detail.get("answer_id"), answered_elsewhere.answer_id)
        self.assertIn("affects 槽位不同", rejected[0].reason)

    def test_exact_slot_still_resolves(self) -> None:
        text = "活动开始时间以哪个为准？"
        self._answer(text, affects=["时间字段:draft_A"])
        result = _result(
            questions=[QuestionCandidate(question=text, kind="missing_time", affects=["时间字段:draft_A"])]
        )
        aware = AnswerAwareUnderstander(self.store, case_id=self.case.id, base=_StubUnderstander(result))
        resolved = aware.analyze(None)
        self.assertTrue(resolved.draftable(), "同一槽位的回答必须解除阻塞")
        self.assertEqual(resolved.blocking_reasons(), [])

    def test_empty_affects_on_both_sides_still_matches(self) -> None:
        # 两边都没有 affects：仍然算同一件事（空槽位是"没指定，就按整体算"）
        text = "还需要补充什么材料吗？"
        self._answer(text, affects=[])
        result = _result(questions=[QuestionCandidate(question=text, kind="missing_material", affects=[])])
        aware = AnswerAwareUnderstander(self.store, case_id=self.case.id, base=_StubUnderstander(result))
        self.assertTrue(aware.analyze(None).draftable())

    def test_different_text_produces_no_spurious_note(self) -> None:
        self._answer("报名截止具体到几点？", affects=["时间字段:registration_deadline"])
        result = _result(
            questions=[QuestionCandidate(question="完全不相关的问题？", kind="missing_time", affects=["时间字段:event_end"])]
        )
        aware = AnswerAwareUnderstander(self.store, case_id=self.case.id, base=_StubUnderstander(result))
        resolved = aware.analyze(None)
        self.assertEqual(resolved.warnings, [], "不是同一句话就不该冒出提示")
        rejected = [
            event for event in self.store.audit_trail(case_id=self.case.id)
            if getattr(event, "result", "") == "REJECTED"
        ]
        self.assertEqual(rejected, [], "无关的追问不该产生 REJECTED 记账")
        self.assertEqual(type(result), UnderstandingResult, "没有已答问题时结果类型不变")

    def test_record_answer_facts_is_cited_and_idempotent(self) -> None:
        question = self.store.put(Question(case_id=self.case.id, question="参加几个人？"))
        answer = self.store.put(Answer(question_id=question.id, case_id=self.case.id, answer_text="2 人"))
        question = self.store.get("Question", question.id)
        question.state = QuestionState.ANSWERED
        question.answer_id = answer.id
        self.store.put(question, expected_revision=question.revision, reason="用户已回答")
        created = record_answer_facts(self.store, case_id=self.case.id)
        self.assertEqual(len(created), 1)
        fact = self.store.get("Fact", created[0])
        self.assertTrue(fact.confirmed_by_user)
        self.assertIn("2 人", fact.statement)
        cited = fact.citations[0]
        self.assertEqual(cited["source_object_type"], "Answer")
        self.assertEqual(cited["source_object_id"], answer.id)
        self.assertTrue(cited["content_hash"])
        self.assertEqual(record_answer_facts(self.store, case_id=self.case.id), [], "同一条回答不得重复落事实")


# --------------------------------------------------------------------------------------
# 夹具邮件源（下层缺陷的兜底层）
# --------------------------------------------------------------------------------------


class TestFixtureMailSource(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mvp8-mail-")
        self.root = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_non_ascii_headers_are_decoded(self) -> None:
        path = os.path.join(self.root, "01.eml")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(notice_eml(subject="周末科普讲座报名确认（含活动时间）", body=CLEAN_NOTICE_BODY))
        source = ContractFixtureMailSource(self.root, account="parent@example.com")
        refs, cursor = source.list_new(None)
        self.assertEqual(len(refs), 1)
        self.assertEqual(cursor, "1")
        self.assertIn("（含活动时间）", refs[0].subject)
        self.assertIn("kids@example.org", refs[0].from_addr)
        self.assertTrue(refs[0].content_digest)
        self.assertTrue(source.fetch_raw(refs[0]).startswith(b"From:"))

    def test_cursor_advances_and_repeats_are_stable(self) -> None:
        for index in (1, 2):
            with open(os.path.join(self.root, "%02d.eml" % index), "w", encoding="utf-8", newline="\n") as handle:
                handle.write(notice_eml(subject="通知 %d" % index, body=CLEAN_NOTICE_BODY, message_id="m-%d@x" % index))
        source = ContractFixtureMailSource(self.root)
        first, cursor = source.list_new(None)
        second, cursor2 = source.list_new(cursor)
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [], "游标之后没有新邮件")
        self.assertEqual(cursor, cursor2)
        again, _ = source.list_new(None)
        self.assertEqual(
            [item.identity_key() for item in again],
            [item.identity_key() for item in first],
            "同一封邮件的身份必须稳定（去重的根据）",
        )

    def test_source_works_against_the_repo_public_fixture(self) -> None:
        """回归：仓库自带的公开夹具必须能被**契约自带**的离线源读出来。

        这条用例原本是"服务层绕行实现能读夹具"的证据；CP-20261010-32 把解码修回契约层、
        删掉了绕行实现之后，它变成"契约自己读得了自己的夹具"的证据——同一个断言，
        但**证明的对象更强**：不再依赖服务层的补丁。
        """
        from tests.service.support import fixture_path

        source = ContractFixtureMailSource(fixture_path("understand"))
        refs, _cursor = source.list_new(None)
        self.assertTrue(refs, "公开夹具目录里必须有邮件")
        self.assertTrue(any("科普" in item.subject for item in refs))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
