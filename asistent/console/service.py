"""控制台业务服务：所有对存储的读写都经过这里。

三条不能妥协的规则：

1. **无会话状态**。本类不保存"当前用户""当前待办""已读游标"之类的字段；每次调用都现场
   查询 store。唯一持有的可变东西是一把可重入锁（保护多线程下的**事务序列**，不是业务状态）。
2. **不泄露路径**。对外序列化只给对象身份、状态、文本与引用；本机绝对路径
   （`SourceDocument.origin_path`、`FileArtifact.path`）不进响应。
3. **写操作必须带版本**。`expected_revision`（或草稿的 `version_no`）不匹配 → `Conflict`（409），
   并把"当前 revision"一起返回，让手机端能刷新后重试。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..contracts.enums import (
    ActorKind,
    ApprovalState,
    AuditAction,
    CaseState,
    DraftState,
    QuestionState,
    TaskState,
)
from ..contracts.errors import AsistentError, DuplicateObject
from ..contracts.errors import NotFound as StoreNotFound
from ..contracts.errors import PermissionDenied, RevisionConflict, ValidationError
from ..contracts.ids import new_id
from ..contracts.objects import (
    Answer,
    Approval,
    AuditEvent,
    Case,
    Draft,
    DraftVersion,
    Execution,
    Fact,
    FileArtifact,
    Inference,
    Question,
    SourceDocument,
    Task,
)
from ..contracts.transitions import machine_for
from .errors import BadRequest, Conflict, ConsoleError, Forbidden, NotFound, Unauthorized
from .auth import TokenAuth
from .sessions import ConsoleSessions
from .throttle import AuthThrottle

#: 待处理 Case 状态：非终态的都算（终态见 `_TERMINAL_CASE_STATES`）
_TERMINAL_CASE_STATES = (
    CaseState.COMPLETED,
    CaseState.CANCELLED,
    CaseState.SUCCEEDED,
)
#: `Task` 的终态
_TERMINAL_TASK_STATES = (TaskState.DONE, TaskState.CANCELLED)
#: 单次响应里最多返回多少条子对象（防一个 Case 把响应撑爆）
_SECTION_LIMIT = 200
#: 正文最大长度
_MAX_TEXT = 20000
#: 审计条目里**不得**出现的字段名（防御性检查，见 `_sanitize_detail`）
_FORBIDDEN_DETAIL_KEYS = ("token", "password", "secret", "authorization", "cookie")


class ConsoleService:
    """控制台的全部业务操作。线程安全（内部一把可重入锁 + 只依赖 store 的原子写）。"""

    def __init__(
        self,
        store: Any,
        auth: TokenAuth,
        *,
        server_name: str = "asistent-console",
        clock=None,
        artifact_roots: Optional[Sequence[str]] = None,
        throttle: Optional[AuthThrottle] = None,
        sessions: Optional[ConsoleSessions] = None,
    ) -> None:
        self._store = store
        self._auth = auth
        self._lock = threading.RLock()
        self._server_name = server_name
        self._clock = clock or (lambda: store.now() if hasattr(store, "now") else "")
        #: 允许下载的目录白名单；空列表 = 不允许任何路径下载（默认最严）
        self._artifact_roots = [os.path.abspath(item) for item in (artifact_roots or [])]
        #: 鉴权失败节流（公网暴露下的爆破成本上限）；测试可注入假时钟
        self._throttle = throttle or AuthThrottle()
        #: 会话（HttpOnly Cookie）：令牌换一次会话，手机 PWA 打开即用
        self._sessions = sessions or ConsoleSessions()

    # ------------------------------------------------------------------ 基础设施
    @property
    def store(self) -> Any:
        return self._store

    def now(self) -> str:
        return str(self._clock() or "")

    def require_auth(self, presented: Optional[str]) -> None:
        """鉴权守门。**必须是每个请求的第一件事**，且失败时不查询任何对象。"""
        if not self._auth.matches(presented):
            raise Unauthorized()

    def auth_gate(self) -> Optional[float]:
        """冷却检查：在解析/校验令牌**之前**调用。返回剩余冷却秒数，None = 放行。

        放在最前面是有意的：冷却期间连"令牌对不对"都不再判断，攻击者拿不到任何
        额外的比对信息，服务也不为此消耗 `hmac.compare_digest`。
        """
        return self._throttle.check()

    def note_auth_failure(self) -> Optional[float]:
        """记录一次鉴权失败；触发冷却时返回冷却秒数（供调用方写日志）。"""
        return self._throttle.record_failure()

    def note_auth_success(self) -> None:
        """鉴权成功：清零失败计数。"""
        self._throttle.record_success()

    # ---- 会话（HttpOnly Cookie） ----
    def open_session(self) -> str:
        """为已鉴权的调用方新建会话，返回会话 Cookie 的值（响应体里不放它）。"""
        return self._sessions.create()

    def session_valid(self, value: Optional[str]) -> bool:
        """Cookie 里的会话值是否仍有效（有效则滑动续期）。"""
        return self._sessions.valid(value or "")

    def revoke_session(self, value: Optional[str]) -> bool:
        """退出登录：吊销这个会话值。"""
        return self._sessions.revoke(value or "")

    def session_stats(self) -> Dict[str, Any]:
        self._sessions.sweep()
        return {"active": self._sessions.count(), "ttl_seconds": self._sessions.ttl_seconds}

    # ------------------------------------------------------------------ 看：总览
    def health(self) -> Dict[str, Any]:
        """健康信息：只有版本、时间与统计数量，没有 token、没有对象内容。"""
        stats = dict(self._store.stats())
        return {
            "ok": True,
            "server": self._server_name,
            "server_time": self.now(),
            "counts": stats,
            # 鉴权节流状态：让主人能看到"是否有人在爆破我"，且不含任何令牌信息
            "auth": self._throttle.snapshot(),
            # 会话数（不含任何会话值）：手机端已登录几个设备一眼可见
            "sessions": self.session_stats(),
        }

    def overview(self, *, since: str = "") -> Dict[str, Any]:
        """手机首页所需的最小集合：待处理项 + 增量游标 + ETag。

        `since` 是上一次响应里的 `next_since`；给了它以后 `changed` 只返回该时刻之后
        变更过的对象，避免每次全量重刷（SPEC §9.4）。
        """
        with self._lock:
            pending = self._pending_snapshot()
            cursor = max(self.now(), pending["max_updated_at"] or "")
            changed = self._changed_since(since) if since else None
            etag = self._etag(pending, changed, since)
            payload: Dict[str, Any] = {
                "server_time": self.now(),
                "next_since": cursor,
                "etag": etag,
                "pending": pending,
            }
            if changed is not None:
                payload["changed"] = changed
            return payload

    def changes(self, *, since: str) -> Dict[str, Any]:
        """纯增量接口：只回 `since` 之后变更的对象 + **始终完整的待处理快照**。

        待处理快照必须每次都给全：手机断网期间正是靠它"补齐看不到的东西"（SPEC §9.4），
        而不是靠增量拼一个可能不完整的列表。
        """
        if not since:
            raise BadRequest("缺少 since 参数（首次加载请用 GET /api/overview）")
        with self._lock:
            pending = self._pending_snapshot()
            changed = self._changed_since(since)
            return {
                "server_time": self.now(),
                "since": since,
                "next_since": max(self.now(), pending["max_updated_at"] or ""),
                "etag": self._etag(pending, changed, since),
                "pending": pending,
                "changed": changed,
            }

    def _pending_snapshot(self) -> Dict[str, Any]:
        """当前所有"需要用户或 Agent 继续处理"的对象（每次都从 store 现查）。"""
        cases = [
            item
            for item in self._find("Case", limit=None)
            if str(item.state) not in tuple(str(s) for s in _TERMINAL_CASE_STATES)
        ]
        questions = [
            item for item in self._find("Question", limit=None) if str(item.state) == str(QuestionState.OPEN)
        ]
        approvals = [
            item for item in self._find("Approval", limit=None) if str(item.state) == str(ApprovalState.ACTIVE)
        ]
        drafts = [
            item for item in self._find("Draft", limit=None) if str(item.state) == str(DraftState.OPEN)
        ]
        tasks = [
            item
            for item in self._find("Task", limit=None)
            if str(item.state) not in tuple(str(s) for s in _TERMINAL_TASK_STATES)
        ]
        artifacts = [
            item
            for item in self._find("FileArtifact", limit=None)
            if int(getattr(item, "transfer_failures", 0) or 0) > 0
        ]
        items = (
            [(case.id, case.updated_at) for case in cases]
            + [(item.id, item.updated_at) for item in questions]
            + [(item.id, item.updated_at) for item in approvals]
            + [(item.id, item.updated_at) for item in drafts]
            + [(item.id, item.updated_at) for item in tasks]
            + [(item.id, item.updated_at) for item in artifacts]
        )
        return {
            "case_ids": [item.id for item in cases],
            "question_ids": [item.id for item in questions],
            "approval_ids": [item.id for item in approvals],
            "draft_ids": [item.id for item in drafts],
            "task_ids": [item.id for item in tasks],
            "artifact_ids": [item.id for item in artifacts],
            "counts": {
                "cases": len(cases),
                "questions": len(questions),
                "approvals": len(approvals),
                "drafts": len(drafts),
                "tasks": len(tasks),
                "failed_transfers": len(artifacts),
            },
            "max_updated_at": max((item[1] or "" for item in items), default=""),
            "items": sorted(
                ({"id": object_id, "updated_at": updated_at} for object_id, updated_at in items),
                key=lambda row: (row["updated_at"] or "", row["id"]),
            ),
        }

    def _changed_since(self, since: str) -> Dict[str, List[Dict[str, Any]]]:
        """`since` 之后变更过的对象，按类型分组（每类只给身份与状态，正文走详情接口）。"""
        out: Dict[str, List[Dict[str, Any]]] = {
            "cases": [],
            "questions": [],
            "answers": [],
            "drafts": [],
            "draft_versions": [],
            "approvals": [],
            "tasks": [],
            "artifacts": [],
        }
        mapping = (
            ("Case", "cases"),
            ("Question", "questions"),
            ("Answer", "answers"),
            ("Draft", "drafts"),
            ("DraftVersion", "draft_versions"),
            ("Approval", "approvals"),
            ("Task", "tasks"),
            ("FileArtifact", "artifacts"),
        )
        for object_type, key in mapping:
            for item in self._find(object_type, limit=None):
                if str(item.updated_at or "") > since:
                    out[key].append(self._brief(item))
        for key in out:
            out[key].sort(key=lambda row: (row.get("updated_at") or "", row.get("id") or ""))
        return out

    def _etag(self, pending: Dict[str, Any], changed: Optional[Dict[str, Any]], since: str) -> str:
        """弱 ETag：由"待处理集合的 (id, updated_at)"决定，与变更窗口无关。"""
        seed = json.dumps(
            {
                "pending": pending["items"],
                "since": since,
                "changed": None
                if changed is None
                else {key: [row["id"] for row in rows] for key, rows in sorted(changed.items())},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return 'W/"%s"' % hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

    # ------------------------------------------------------------------ 看：Case
    def list_cases(
        self,
        *,
        state: str = "",
        include_closed: bool = False,
        limit: int = 50,
        since: str = "",
    ) -> Dict[str, Any]:
        """Case 列表。默认只给未结束的；`include_closed=true` 才带终态 Case。"""
        cases = self._find("Case", limit=None)
        if state:
            wanted = self._case_state(state)
            cases = [item for item in cases if str(item.state) == wanted]
        elif not include_closed:
            cases = [
                item
                for item in cases
                if str(item.state) not in tuple(str(s) for s in _TERMINAL_CASE_STATES)
            ]
        if since:
            cases = [item for item in cases if str(item.updated_at or "") > since]
        cases.sort(key=lambda item: (str(item.updated_at or ""), item.id), reverse=True)
        rows = [self._case_row(item) for item in cases[: max(0, int(limit))]]
        return {"server_time": self.now(), "total": len(cases), "cases": rows}

    def get_case(self, case_id: str, *, include: str = "") -> Dict[str, Any]:
        """Case 详情：状态、Agent 状态、最近动作、等待原因、下一步、关联对象与引用。"""
        with self._translated(object_type="Case", object_id=case_id):
            case = self._get("Case", case_id)
            sections = {item.strip() for item in str(include or "").split(",") if item.strip()}
            payload = self._case_row(case, full=True)
            payload["tasks"] = [self._task_row(item) for item in self._tasks_of(case_id)]
            payload["questions"] = [self._question_row(item) for item in self._questions_of(case_id)]
            payload["facts"] = [
                self._fact_row(item) for item in self._for_case("Fact", case_id, limit=_SECTION_LIMIT)
            ]
            payload["inferences"] = [
                self._inference_row(item)
                for item in self._for_case("Inference", case_id, limit=_SECTION_LIMIT)
            ]
            payload["drafts"] = [self._draft_row(item) for item in self._drafts_of(case_id)]
            payload["approvals"] = [
                self._approval_row(item) for item in self._for_case("Approval", case_id, limit=_SECTION_LIMIT)
            ]
            payload["artifacts"] = [
                self._artifact_row(item) for item in self._for_case("FileArtifact", case_id, limit=_SECTION_LIMIT)
            ]
            payload["agent"] = self._agent_status(case_id)
            if "documents" in sections or "all" in sections:
                payload["documents"] = [
                    self._document_row(item) for item in self._documents_of(case_id)
                ]
            if "audit" in sections or "all" in sections:
                payload["audit"] = self._audit_rows(case_id=case_id, limit=50)
            return payload

    def create_case(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """手机独立发起任务（SPEC §9.2 第一条）。"""
        goal = self._text(body.get("goal"), field="goal", required=True, limit=500)
        with self._lock, self._translated(object_type="Case"):
            case = Case(
                id=self._new_id("Case"),
                goal=goal,
                state=CaseState.NEW,
                next_action="等待 Agent 分析",
                last_action_summary="由手机控制台发起",
                links={"origin": ["console"]},
            )
            saved = self._store.put(
                case,
                actor_kind=str(ActorKind.USER),
                actor_id="console",
                reason="手机控制台发起任务",
                audit_action=str(AuditAction.CREATE),
                audit_detail={"goal": goal},
            )
            return self._case_row(saved, full=True)

    # ------------------------------------------------------------------ 答：Question → Answer
    def list_questions(self, *, state: str = "OPEN", limit: int = 100) -> Dict[str, Any]:
        questions = self._find("Question", limit=None)
        if state:
            questions = [item for item in questions if str(item.state) == str(state)]
        questions.sort(key=lambda item: (str(item.created_at or ""), item.id))
        return {
            "server_time": self.now(),
            "total": len(questions),
            "questions": [self._question_row(item) for item in questions[: max(0, int(limit))]],
        }

    def get_question(self, question_id: str) -> Dict[str, Any]:
        with self._translated(object_type="Question", object_id=question_id):
            question = self._get("Question", question_id)
            payload = self._question_row(question)
            payload["answers"] = [
                self._answer_row(item)
                for item in self._find("Answer", where={"question_id": question.id}, limit=None)
            ]
            payload["case"] = self._case_row(self._get("Case", question.case_id), full=False)
            return payload

    def submit_answer(self, question_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """提交 Answer：落进**正确的 Case**，并触发依赖任务重新评估。

        校验：`expected_revision` 必填（并发回答同一问题时旧版本必须被拒）；
        回答文本只作为 Answer 内容，**不会**被当成审批（审批见 `submit_approval`）。
        """
        text = self._text(body.get("answer_text"), field="answer_text", required=True, limit=_MAX_TEXT)
        expected = self._expected_revision(body)
        with self._lock, self._translated(object_type="Question", object_id=question_id):
            question = self._get("Question", question_id)
            self._require_revision(question, expected, object_type="Question")
            if str(question.state) != str(QuestionState.OPEN):
                raise Conflict(
                    "该追问已不是待回答状态（state=%s），不需要再次回答" % question.state,
                    object_type="Question",
                    object_id=question.id,
                    expected_revision=expected,
                    current_revision=int(question.revision),
                )
            answer = Answer(
                id=self._new_id("Answer"),
                question_id=question.id,
                case_id=question.case_id,
                answer_text=text,
                answered_by="user",
                links={"case": [question.case_id], "question": [question.id]},
            )
            saved_answer = self._store.put(
                answer,
                actor_kind=str(ActorKind.USER),
                actor_id="console",
                reason="手机控制台回答问题",
                audit_action=str(AuditAction.CREATE),
                audit_detail={"question_id": question.id, "case_id": question.case_id},
            )
            question = self._get("Question", question_id)
            question.answer_id = saved_answer.id
            question.state = QuestionState.ANSWERED  # 状态机由 store.put 校验（OPEN → ANSWERED 合法）
            self._store.put(
                question,
                expected_revision=int(question.revision),
                actor_kind=str(ActorKind.USER),
                actor_id="console",
                reason="用户已回答，追问关闭",
                audit_action=str(AuditAction.TRANSITION),
                audit_detail={
                    "question_id": question.id,
                    "answer_id": saved_answer.id,
                    "from": str(QuestionState.OPEN),
                    "to": str(QuestionState.ANSWERED),
                },
            )
            reevaluated = self._reevaluate_dependents(question.case_id, question.id)
            self._link(question.case_id, "question_ids", question.id)
            self._refresh_case_waiting_state(question.case_id)
            return {
                "question": self._question_row(self._get("Question", question_id)),
                "answer": self._answer_row(saved_answer),
                "case": self._case_row(self._get("Case", question.case_id), full=True),
                "reevaluated_tasks": reevaluated,
            }

    def _reevaluate_dependents(self, case_id: str, question_id: str) -> List[Dict[str, Any]]:
        """回答完后重新评估依赖该追问的任务：`WAITING_USER`/`BLOCKED` → `READY`。

        只做契约允许的转换（`machine_for("Task").can`），不合法就跳过并记录原因 ——
        状态机是唯一裁决点，控制台不能自己发明转换。
        """
        machine = machine_for("Task")
        out: List[Dict[str, Any]] = []
        for task in self._tasks_of(case_id):
            if task.id not in self._dependents_of(case_id, question_id):
                continue
            current = str(task.state)
            if current not in (str(TaskState.WAITING_USER), str(TaskState.BLOCKED)):
                out.append({"task_id": task.id, "from": current, "to": current, "changed": False,
                            "reason": "状态无需重新评估"})
                continue
            target = str(TaskState.READY)
            if not machine.can(current, target):
                out.append({"task_id": task.id, "from": current, "to": target, "changed": False,
                            "reason": "状态机不允许该转换"})
                continue
            self._store.transition(
                "Task",
                task.id,
                target,
                expected_revision=int(task.revision),
                actor_kind=str(ActorKind.AGENT),
                actor_id="console",
                reason="依赖的追问已由用户回答，重新评估",
                trigger="AGENT",
            )
            out.append({"task_id": task.id, "from": current, "to": target, "changed": True, "reason": ""})
        return out

    def _dependents_of(self, case_id: str, question_id: str) -> List[str]:
        """找出依赖某个追问的任务：`preconditions`/`inputs.questions` 里提到该 ID 的都算。"""
        found: List[str] = []
        for task in self._tasks_of(case_id):
            blob = " ".join(
                [
                    " ".join(str(item) for item in (task.preconditions or [])),
                    json.dumps(task.inputs or {}, ensure_ascii=False),
                    " ".join((task.links or {}).get("question", []) or []),
                ]
            )
            if question_id in blob:
                found.append(task.id)
        return found

    def _refresh_case_waiting_state(self, case_id: str) -> None:
        """回答后刷新 Case 的等待原因/下一步/状态。

        规则（只在状态机允许时才动 `state`，绝不自己发明转换）：

        * 还有 OPEN 追问 → `WAITING_FOR_USER`，等待原因写"还有几条待回答"；
        * 没有 OPEN 追问但有 OPEN 草稿 → `READY_FOR_REVIEW`（等用户审批）；
        * 都没有 → 保持当前状态（契约里 `WAITING_FOR_USER → WAITING_FOR_CONTEXT` 不存在，
          必须经过 `ANALYZING`，而重新分析是 Agent 的动作，不是控制台能自己做的），
          只把等待原因/下一步改成"等 Agent 重新分析"。如果状态机**允许**直接到
          `WAITING_FOR_CONTEXT` 就走那一步（设备上的实现可能是别的状态机版本）。
        """
        case = self._get("Case", case_id)
        open_questions = [
            item for item in self._questions_of(case_id) if str(item.state) == str(QuestionState.OPEN)
        ]
        open_drafts = [
            item for item in self._drafts_of(case_id) if str(item.state) == str(DraftState.OPEN)
        ]
        if open_questions:
            target, waiting, next_action = (
                CaseState.WAITING_FOR_USER,
                "等待用户回答 %d 条追问" % len(open_questions),
                "在手机上回答追问",
            )
        elif open_drafts:
            target, waiting, next_action = (
                CaseState.READY_FOR_REVIEW,
                None,
                "等待用户审批草稿",
            )
        else:
            target, waiting, next_action = (
                CaseState.WAITING_FOR_CONTEXT,
                "追问已回答，等待 Agent 基于回答重新分析",
                "重新分析事务",
            )
        case = self._get("Case", case_id)
        changed_state = False
        if str(case.state) != str(target) and machine_for("Case").can(str(case.state), str(target)):
            case.state = target
            changed_state = True
        if str(case.waiting_reason or "") == str(waiting or "") and case.next_action == next_action and not changed_state:
            return  # 没有变化就不写库：避免每次回答都产生一条无意义审计
        case.waiting_reason = waiting
        case.next_action = next_action
        case.last_action_summary = "用户已回答追问，依赖任务重新评估"
        self._store.put(
            case,
            expected_revision=int(case.revision),
            actor_kind=str(ActorKind.AGENT),
            actor_id="console",
            reason="回答后重新评估事务等待状态",
            audit_action=str(AuditAction.UPDATE),
            audit_detail={
                "open_questions": [item.id for item in open_questions],
                "open_drafts": [item.id for item in open_drafts],
                "state_changed": changed_state,
                "state_target": str(target),
            },
        )

    # ------------------------------------------------------------------ 改：草稿版本
    def list_drafts(self, *, case_id: str = "", limit: int = 100) -> Dict[str, Any]:
        drafts = self._find("Draft", limit=None)
        if case_id:
            drafts = [item for item in drafts if item.case_id == case_id]
        drafts.sort(key=lambda item: (str(item.updated_at or ""), item.id), reverse=True)
        return {
            "server_time": self.now(),
            "total": len(drafts),
            "drafts": [self._draft_row(item) for item in drafts[: max(0, int(limit))]],
        }

    def get_draft(self, draft_id: str, *, version_no: Optional[int] = None) -> Dict[str, Any]:
        from ..understand import drafts as draft_api

        with self._translated(object_type="Draft", object_id=draft_id):
            draft = self._get("Draft", draft_id)
            payload = self._draft_row(draft, full=True)
            versions = draft_api.versions_of(self._store, draft_id)
            payload["versions"] = [self._version_row(item) for item in versions]
            current = draft_api.current_version(self._store, draft_id)
            payload["current"] = self._version_row(current) if current is not None else None
            if version_no is not None:
                target = draft_api.version_by_number(self._store, draft_id, int(version_no))
                if target is None:
                    raise NotFound("草稿版本不存在: v%s" % version_no)
                payload["selected"] = self._version_row(target, with_body=True)
            if len(versions) >= 2:
                newest = versions[-1]
                previous = versions[-2]
                payload["latest_diff"] = draft_api.diff_versions(
                    self._store, draft_id, int(previous.version_no), int(newest.version_no)
                ).to_dict()
            return payload

    def edit_draft(self, draft_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """保存编辑 → 产生**新版本**；旧 Approval（若有）失效。

        必须带 `version_no`（正在编辑的版本）或 `expected_revision`（Draft 的 revision）：
        任何一个对不上 → 409，且**不写入任何内容**（SPEC §846：旧版本编辑被拒绝）。

        校验顺序是**先 version_no 后 expected_revision**：手机端最常见的过期场景是
        "我在编辑 v1，别人已经存了 v2"，先报版本号能给出一条能直接读懂的原因。
        """
        from ..understand import drafts as draft_api

        text = self._text(body.get("body"), field="body", required=True, limit=_MAX_TEXT)
        reason = self._text(body.get("change_reason"), field="change_reason", required=False, limit=500)
        expected = body.get("expected_revision")
        version_no = body.get("version_no")
        if expected is None and version_no is None:
            raise BadRequest(
                "编辑必须带 version_no 或 expected_revision 之一（没有版本就没有并发保护）"
            )
        with self._lock, self._translated(object_type="Draft", object_id=draft_id):
            draft = self._get("Draft", draft_id)
            current = draft_api.current_version(self._store, draft_id)
            if version_no is not None:
                wanted = self._coerce_int(version_no, "version_no")
                if current is None or int(current.version_no) != wanted:
                    raise Conflict(
                        "正在编辑的版本已不是当前版本：提交 v%s，当前 v%s"
                        % (wanted, "-" if current is None else int(current.version_no)),
                        object_type="Draft",
                        object_id=draft_id,
                        expected_revision=wanted,
                        current_revision=None if current is None else int(current.version_no),
                    )
            if expected is not None:
                self._require_revision(
                    draft, self._coerce_int(expected, "expected_revision"), object_type="Draft"
                )
            version = draft_api.new_version(
                self._store,
                draft_id,
                body=text,
                author_kind=str(ActorKind.USER),
                author_id="console",
                change_reason=reason or "手机控制台编辑",
                summary="手机控制台编辑",
            )
            invalidated = self._invalidate_approvals_for(draft_id, reason="草稿内容已改变，旧审批失效")
            if str(draft.state) not in (str(DraftState.OPEN), str(DraftState.LOCKED)):
                draft = self._get("Draft", draft_id)
                if str(draft.state) == str(DraftState.STALE):
                    self._store.transition(
                        "Draft",
                        draft_id,
                        str(DraftState.OPEN),
                        expected_revision=int(draft.revision),
                        actor_kind=str(ActorKind.USER),
                        actor_id="console",
                        reason="用户重新编辑，草稿回到 OPEN",
                        trigger="USER",
                    )
            payload = self.get_draft(draft_id)
            payload["created_version"] = self._version_row(version)
            payload["invalidated_approval_ids"] = invalidated
            return payload

    def restore_draft_version(self, draft_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """恢复旧版本：产生**新版本**（不是回滚指针），并把旧审批失效。"""
        from ..understand import drafts as draft_api

        version_no = self._coerce_int(body.get("version_no"), "version_no")
        expected = self._expected_revision(body)
        reason = self._text(body.get("change_reason"), field="change_reason", required=False, limit=500)
        with self._lock, self._translated(object_type="Draft", object_id=draft_id):
            draft = self._get("Draft", draft_id)
            self._require_revision(draft, expected, object_type="Draft")
            if draft_api.version_by_number(self._store, draft_id, version_no) is None:
                raise NotFound("草稿版本不存在: v%s" % version_no)
            version = draft_api.restore_version(
                self._store, draft_id, version_no, author_kind=str(ActorKind.USER), author_id="console",
                reason=reason or ("手机控制台恢复到 v%s" % version_no),
            )
            invalidated = self._invalidate_approvals_for(draft_id, reason="草稿恢复了旧版本，旧审批失效")
            payload = self.get_draft(draft_id)
            payload["created_version"] = self._version_row(version)
            payload["invalidated_approval_ids"] = invalidated
            return payload

    def diff_draft(self, draft_id: str, *, from_no: int, to_no: int) -> Dict[str, Any]:
        from ..understand import drafts as draft_api

        self._get("Draft", draft_id)
        delta = draft_api.diff_versions(self._store, draft_id, int(from_no), int(to_no))
        return delta.to_dict()

    # ------------------------------------------------------------------ 批：审批
    def list_approvals(self, *, case_id: str = "", state: str = "", limit: int = 100) -> Dict[str, Any]:
        approvals = self._find("Approval", limit=None)
        if case_id:
            approvals = [item for item in approvals if item.case_id == case_id]
        if state:
            approvals = [item for item in approvals if str(item.state) == str(state)]
        approvals.sort(key=lambda item: (str(item.created_at or ""), item.id), reverse=True)
        return {
            "server_time": self.now(),
            "total": len(approvals),
            "approvals": [self._approval_row(item) for item in approvals[: max(0, int(limit))]],
        }

    def submit_approval(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """审批通道：**只能**通过这里批准，且必须显式绑定版本号 + 显式确认。

        拒收三类"看起来像批准"的请求（SPEC §21.3）：

        * 没有 `version_no` 的（审批必须绑定确定版本）；
        * 没有 `confirm=true` 的（聊天文本里的"同意/发送"不是批准）；
        * 请求体里只有一段自由文本、没有结构字段的。
        """
        from ..understand import drafts as draft_api

        if not isinstance(body, dict):
            raise BadRequest("审批请求体必须是 JSON 对象")
        confirmation = body.get("confirm")
        if confirmation is not True:
            raise Forbidden(
                "审批必须显式确认（confirm=true）：普通请求文本不构成批准（SPEC §21.3）"
            )
        draft_id = self._text(body.get("draft_id"), field="draft_id", required=True, limit=200)
        if body.get("version_no") is None:
            raise BadRequest("审批必须绑定确定版本：缺少 version_no")
        version_no = self._coerce_int(body.get("version_no"), "version_no")
        digest = self._text(body.get("action_digest"), field="action_digest", required=False, limit=200)
        # 契约 v0.6（CP-20261010-15）：同一 action_id 上至多一个 ACTIVE 审批。
        # 控制台在"审批"这一步通常还不知道将来由哪次执行来消费这张审批，所以
        # `action_id` 可选；调用方知道就传（由 store 就地取代旧审批）。
        action_id = self._text(body.get("action_id"), field="action_id", required=False, limit=200)
        with self._lock, self._translated(object_type="Draft", object_id=draft_id):
            draft = self._get("Draft", draft_id)
            binding = draft_api.approval_binding(self._store, draft_id, version_no)
            if digest and digest != binding.content_hash:
                raise Conflict(
                    "提交的动作摘要与绑定版本的内容哈希不一致（内容可能已变），请刷新后重新确认",
                    object_type="Draft",
                    object_id=draft_id,
                )
            before_active = {
                item.id
                for item in self._find("Approval", limit=None)
                if str(item.state) == str(ApprovalState.ACTIVE)
            }
            approval = Approval(
                id=self._new_id("Approval"),
                case_id=draft.case_id,
                action_id=action_id or None,
                approver="user",
                approved_action_digest=binding.content_hash,
                target_object_ids=[binding.version_id],
                content_version_refs={binding.version_id: binding.content_hash},
                created_at_approval=self.now(),
                state=ApprovalState.ACTIVE,
                auth_channel="console",
                auth_evidence="console:bearer-token",
                summary_shown={
                    "draft_id": binding.draft_id,
                    "version_no": binding.version_no,
                    "version_id": binding.version_id,
                    "content_hash": binding.content_hash,
                },
                links={"case": [draft.case_id], "draft": [draft_id]},
            )
            saved = self._store.approve(approval)
            self._link(draft.case_id, "approval_ids", saved.id)
            superseded = sorted(before_active - {item.id for item in self._find(
                "Approval", where={"state": str(ApprovalState.ACTIVE)}, limit=None
            )})
            return {
                "approval": self._approval_row(saved),
                "draft": self._draft_row(self._get("Draft", draft_id)),
                # 契约 v0.6 的"授权唯一"是**存储层**的决定，控制台只做如实转达：
                # 客户端据此把被取代的审批从"有效"列表里拿掉，而不是自己猜。
                "superseded_approval_ids": superseded,
            }

    def revoke_approval(self, approval_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """撤销审批（`ACTIVE → REVOKED`）。"""
        reason = self._text(body.get("reason"), field="reason", required=False, limit=500) or "用户在手机端撤销"
        expected = self._expected_revision(body)
        with self._lock, self._translated(object_type="Approval", object_id=approval_id):
            approval = self._get("Approval", approval_id)
            self._require_revision(approval, expected, object_type="Approval")
            if str(approval.state) != str(ApprovalState.ACTIVE):
                raise Conflict(
                    "只有 ACTIVE 审批可以撤销（当前 state=%s）" % approval.state,
                    object_type="Approval",
                    object_id=approval_id,
                    expected_revision=expected,
                    current_revision=int(approval.revision),
                )
            saved = self._store.invalidate_approval(approval_id, reason=reason, state="REVOKED")
            return {"approval": self._approval_row(saved)}

    def _invalidate_approvals_for(self, draft_id: str, *, reason: str) -> List[str]:
        """把绑定了该草稿任一版本的 ACTIVE 审批置为 INVALIDATED（SPEC §846）。"""
        from ..understand import drafts as draft_api

        version_ids = {item.id for item in draft_api.versions_of(self._store, draft_id)}
        invalidated: List[str] = []
        for approval in self._find("Approval", limit=None):
            if str(approval.state) != str(ApprovalState.ACTIVE):
                continue
            bound = set(approval.target_object_ids or []) | set((approval.content_version_refs or {}).keys())
            if not bound & version_ids:
                continue
            self._store.invalidate_approval(approval.id, reason=reason)
            invalidated.append(approval.id)
        return invalidated

    def get_approval(self, approval_id: str) -> Dict[str, Any]:
        approval = self._get("Approval", approval_id)
        return self._approval_row(approval, full=True)

    # ------------------------------------------------------------------ 取：文件
    def list_artifacts(self, *, case_id: str = "", limit: int = 100) -> Dict[str, Any]:
        artifacts = self._find("FileArtifact", limit=None)
        if case_id:
            artifacts = [item for item in artifacts if item.case_id == case_id]
        artifacts.sort(key=lambda item: (str(item.generated_at or ""), item.id), reverse=True)
        return {
            "server_time": self.now(),
            "total": len(artifacts),
            "artifacts": [self._artifact_row(item) for item in artifacts[: max(0, int(limit))]],
        }

    def get_artifact(self, artifact_id: str) -> Dict[str, Any]:
        return self._artifact_row(self._get("FileArtifact", artifact_id), full=True)

    def read_artifact(self, artifact_id: str) -> Tuple[bytes, Dict[str, Any]]:
        """读取文件内容。失败只影响这一个请求（并记账 `transfer_failures`），不牵连别的。

        安全：路径必须在白名单目录里；符号链接不跟随；不做任何"猜路径"。
        失败原因会**原样**返回给已鉴权用户（他有权限看自己设备的错误），但这不构成
        信息泄露：未鉴权请求在更早的 401 处就被拦掉了。
        """
        with self._translated(object_type="FileArtifact", object_id=artifact_id):
            artifact = self._get("FileArtifact", artifact_id)
            row = self._artifact_row(artifact, full=True)
            path = str(artifact.path or "")
            try:
                resolved = self._safe_artifact_path(path)
                with open(resolved, "rb") as handle:
                    data = handle.read()
            except Exception as exc:  # 失败要可重试：记账后返回 404 + 可读原因
                reason = str(exc) or type(exc).__name__
                self._record_transfer_failure(artifact, reason)
                raise NotFound("文件当前不可取用（%s）：可稍后重试" % reason)
            self._record_transfer_success(artifact)
            return data, row

    def _safe_artifact_path(self, path: str) -> str:
        if not path:
            raise ValidationError("文件对象没有 path")
        if not self._artifact_roots:
            raise ValidationError("未配置可下载目录白名单，拒绝取用任何文件")
        resolved = os.path.realpath(path)
        if not any(resolved == root or resolved.startswith(root + os.sep) for root in self._artifact_roots):
            raise ValidationError("文件不在允许下载的目录内")
        if not os.path.isfile(resolved):
            raise ValidationError("文件不存在")
        return resolved

    def _record_transfer_failure(self, artifact: FileArtifact, error: str) -> None:
        try:
            fresh = self._get("FileArtifact", artifact.id)
            fresh.transfer_failures = int(fresh.transfer_failures or 0) + 1
            self._store.put(
                fresh,
                expected_revision=int(fresh.revision),
                actor_kind=str(ActorKind.SYSTEM),
                actor_id="console",
                reason="文件取用失败（可重试）",
                audit_action=str(AuditAction.UPDATE),
                audit_detail={"artifact_id": artifact.id, "error_class": error[:200]},
            )
        except Exception:  # 记账失败不能掩盖原始错误
            pass

    def _record_transfer_success(self, artifact: FileArtifact) -> None:
        try:
            fresh = self._get("FileArtifact", artifact.id)
            fresh.download_count = int(fresh.download_count or 0) + 1
            self._store.put(
                fresh,
                expected_revision=int(fresh.revision),
                actor_kind=str(ActorKind.USER),
                actor_id="console",
                reason="文件已取用",
                audit_action=str(AuditAction.READ),
                audit_detail={"artifact_id": artifact.id},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ 序列化
    def _brief(self, obj: Any) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "object_type": getattr(obj, "OBJECT_TYPE", type(obj).__name__),
            "id": obj.id,
            "revision": int(obj.revision),
            "updated_at": obj.updated_at,
        }
        for field in ("state", "case_id", "question_id", "draft_id", "version_no", "kind"):
            if hasattr(obj, field):
                row[field] = str(getattr(obj, field) or "")
        return row

    def _case_row(self, case: Case, *, full: bool = False) -> Dict[str, Any]:
        row = {
            "id": case.id,
            "revision": int(case.revision),
            "state": str(case.state),
            "goal": case.goal,
            "created_at": case.created_at,
            "updated_at": case.updated_at,
            "waiting_reason": case.waiting_reason,
            "blocked_reason": case.blocked_reason,
            "next_action": case.next_action,
            "last_action_summary": case.last_action_summary,
            "result_summary": case.result_summary,
            "failure_class": case.failure_class,
            "archive_state": str(case.archive_state),
            "completed_at": case.completed_at,
            "links": {key: list(value) for key, value in (case.links or {}).items()},
        }
        if full:
            row["counts"] = {
                "tasks": len(case.task_ids),
                "questions": len(case.question_ids),
                "drafts": len(case.draft_ids),
                "approvals": len(case.approval_ids),
                "facts": len(case.fact_ids),
                "inferences": len(case.inference_ids),
                "documents": len(case.source_document_ids),
            }
        return row

    def _task_row(self, task: Task) -> Dict[str, Any]:
        return {
            "id": task.id,
            "revision": int(task.revision),
            "case_id": task.case_id,
            "kind": task.kind,
            "title": task.title,
            "state": str(task.state),
            "assignee": task.assignee,
            "preconditions": list(task.preconditions or []),
            "depends_on": list(task.depends_on or []),
            "attempt_count": int(task.attempt_count or 0),
            "last_error": task.last_error,
            "failure_class": task.failure_class,
            "next_run_at": task.next_run_at,
            "timeout_at": task.timeout_at,
            "updated_at": task.updated_at,
            "inputs": _plain(task.inputs),
            "outputs": _plain(task.outputs),
        }

    def _question_row(self, question: Question) -> Dict[str, Any]:
        return {
            "id": question.id,
            "revision": int(question.revision),
            "case_id": question.case_id,
            "question": question.question,
            "background": question.background,
            "required_format": question.required_format,
            "affects": list(question.affects or []),
            "deadline": question.deadline,
            "state": str(question.state),
            "answer_id": question.answer_id,
            "created_at": question.created_at,
            "updated_at": question.updated_at,
        }

    def _answer_row(self, answer: Answer) -> Dict[str, Any]:
        return {
            "id": answer.id,
            "revision": int(answer.revision),
            "question_id": answer.question_id,
            "case_id": answer.case_id,
            "answer_text": answer.answer_text,
            "answered_by": answer.answered_by,
            "created_at": answer.created_at,
        }

    def _draft_row(self, draft: Draft, *, full: bool = False) -> Dict[str, Any]:
        row = {
            "id": draft.id,
            "revision": int(draft.revision),
            "case_id": draft.case_id,
            "target_kind": draft.target_kind,
            "target_ref": draft.target_ref,
            "state": str(draft.state),
            "current_version_id": draft.current_version_id,
            "version_ids": list(draft.version_ids or []),
            "version_count": len(draft.version_ids or []),
            "updated_at": draft.updated_at,
        }
        if full:
            row["links"] = {key: list(value) for key, value in (draft.links or {}).items()}
        return row

    def _version_row(self, version: Any, *, with_body: bool = True) -> Optional[Dict[str, Any]]:
        if version is None:
            return None
        row = {
            "id": version.id,
            "revision": int(version.revision),
            "draft_id": version.draft_id,
            "version_no": int(version.version_no),
            "content_hash": version.content_hash,
            "author_kind": str(version.author_kind),
            "author_id": version.author_id,
            "change_reason": version.change_reason,
            "summary": version.summary,
            "parent_version_id": version.parent_version_id,
            "target_object_id": version.target_object_id,
            "created_at": version.created_at,
            "citations": _plain(version.citations),
        }
        if with_body:
            row["body"] = version.body
        return row

    def _fact_row(self, fact: Fact) -> Dict[str, Any]:
        return {
            "id": fact.id,
            "revision": int(fact.revision),
            "case_id": fact.case_id,
            "subject": fact.subject,
            "statement": fact.statement,
            "content_kind": str(fact.content_kind),
            "confirmed_by_user": bool(fact.confirmed_by_user),
            "citations": [_citation(row) for row in (fact.citations or [])],
            "temporal_fields": _plain(fact.temporal_fields),
            "updated_at": fact.updated_at,
        }

    def _inference_row(self, item: Inference) -> Dict[str, Any]:
        return {
            "id": item.id,
            "revision": int(item.revision),
            "case_id": item.case_id,
            "statement": item.statement,
            "confidence": item.confidence,
            "candidate_only": bool(item.candidate_only),
            "based_on_fact_ids": list(item.based_on_fact_ids or []),
            "updated_at": item.updated_at,
        }

    def _approval_row(self, approval: Approval, *, full: bool = False) -> Dict[str, Any]:
        row = {
            "id": approval.id,
            "revision": int(approval.revision),
            "case_id": approval.case_id,
            "action_id": approval.action_id,
            "state": str(approval.state),
            "approver": approval.approver,
            "approved_action_digest": approval.approved_action_digest,
            "target_object_ids": list(approval.target_object_ids or []),
            "created_at_approval": approval.created_at_approval,
            "expires_at": approval.expires_at,
            "consumed_by_attempt_id": approval.consumed_by_attempt_id,
            "revoked_reason": approval.revoked_reason,
            "invalidated_reason": approval.invalidated_reason,
            "auth_channel": approval.auth_channel,
            "updated_at": approval.updated_at,
        }
        if full:
            row["summary_shown"] = _plain(approval.summary_shown)
            row["content_version_refs"] = dict(approval.content_version_refs or {})
        return row

    def _artifact_row(self, artifact: FileArtifact, *, full: bool = False) -> Dict[str, Any]:
        row = {
            "id": artifact.id,
            "revision": int(artifact.revision),
            "case_id": artifact.case_id,
            "name": artifact.name or os.path.basename(str(artifact.path or "")),
            "size_bytes": int(artifact.size_bytes or 0),
            "sha256": artifact.sha256,
            "content_type": artifact.content_type,
            "version": int(artifact.version or 1),
            "generated_at": artifact.generated_at,
            "permission": artifact.permission,
            "download_count": int(artifact.download_count or 0),
            "transfer_failures": int(artifact.transfer_failures or 0),
            "updated_at": artifact.updated_at,
        }
        if full:
            # 只给"可不可取用"，不给本机路径
            row["available"] = bool(self._artifact_roots)
        return row

    def _document_row(self, document: SourceDocument) -> Dict[str, Any]:
        """原始资料：**不给 `origin_path`**（本机绝对路径属于运行环境信息，不外泄）。"""
        return {
            "id": document.id,
            "revision": int(document.revision),
            "source_type": document.source_type,
            "source_system": document.source_system,
            "content_hash": document.content_hash,
            "content_origin": document.content_origin,
            "content_version": int(document.content_version or 1),
            "received_at": document.received_at,
            "readable": bool(document.readable),
            "index_state": str(document.index_state),
            "archive_state": str(document.archive_state),
            "case_id": document.case_id,
            "size_bytes": int(document.size_bytes or 0),
            "mime_type": document.mime_type,
        }

    def _audit_rows(self, *, case_id: str, limit: int) -> List[Dict[str, Any]]:
        rows = self._store.audit_trail(case_id=case_id, limit=limit)
        out = []
        for entry in rows:
            out.append(
                {
                    "id": entry.id,
                    "at": entry.at,
                    "action": str(entry.action),
                    "actor_kind": str(entry.actor_kind),
                    "actor_id": entry.actor_id,
                    "object_type": entry.object_type,
                    "object_id": entry.object_id,
                    "result": entry.result,
                    "version_before": entry.version_before,
                    "version_after": entry.version_after,
                    "reason": entry.reason,
                    "detail": _sanitize_detail(entry.detail),
                }
            )
        return out

    def _agent_status(self, case_id: str) -> Dict[str, Any]:
        """Agent 当前状态：最后动作 + 当前是否有执行在跑 + 最近执行记录。"""
        executions = [
            item for item in self._find("Execution", limit=None) if str(item.case_id or "") == case_id
        ]
        executions.sort(key=lambda item: (str(item.started_at or ""), item.id))
        running = [item for item in executions if str(item.state) == "RUNNING"]
        rows = [
            {
                "id": item.id,
                "state": str(item.state),
                "backend": item.backend,
                "model": item.model,
                "task_id": item.task_id,
                "input_summary": item.input_summary,
                "output_summary": item.output_summary,
                "started_at": item.started_at,
                "finished_at": item.finished_at,
                "failure_class": item.failure_class,
                "error_text": item.error_text,
            }
            for item in executions[-5:]
        ]
        tasks = self._tasks_of(case_id)
        return {
            "running": bool(running),
            "current_execution_id": running[-1].id if running else None,
            "recent_executions": rows,
            "running_tasks": [
                {"id": item.id, "title": item.title, "state": str(item.state)}
                for item in tasks
                if str(item.state) in ("RUNNING", "READY")
            ],
        }

    # ------------------------------------------------------------------ store 小工具
    def _find(self, object_type: str, *, where: Optional[Dict[str, Any]] = None, limit: Any = 100) -> List[Any]:
        try:
            return list(self._store.find(object_type, where=where, limit=limit))
        except ValidationError as exc:
            raise BadRequest(str(exc))

    def _get(self, object_type: str, object_id: str) -> Any:
        try:
            return self._store.get(object_type, object_id)
        except StoreNotFound:
            raise NotFound("%s 不存在" % object_type)
        except ValidationError as exc:
            raise BadRequest(str(exc))

    def _for_case(self, object_type: str, case_id: str, *, limit: int) -> List[Any]:
        return [
            item
            for item in self._find(object_type, where={"case_id": case_id}, limit=None)
            if str(getattr(item, "case_id", "") or "") == case_id
        ][:limit]

    def _documents_of(self, case_id: str, *, limit: int = _SECTION_LIMIT) -> List[Any]:
        """Case 的原始对象：走 Case 反向索引（`SourceDocument.case_id` 可能为空，
        文档是在处理时被挂到 Case 上的）。"""
        case = self._get("Case", case_id)
        rows: List[Any] = []
        for document_id in list(case.source_document_ids or []):
            try:
                rows.append(self._store.get("SourceDocument", document_id))
            except StoreNotFound:
                continue
        if rows:
            return rows[:limit]
        # 兼容：有些路径把 case_id 直接写在文档上
        return self._for_case("SourceDocument", case_id, limit=limit)

    def _tasks_of(self, case_id: str) -> List[Task]:
        tasks = [
            item
            for item in self._find("Task", where={"case_id": case_id}, limit=None)
            if str(item.case_id) == case_id
        ]
        tasks.sort(key=lambda item: (str(item.created_at or ""), item.id))
        return tasks  # type: ignore[return-value]

    def _questions_of(self, case_id: str) -> List[Question]:
        questions = [
            item
            for item in self._find("Question", where={"case_id": case_id}, limit=None)
            if str(item.case_id) == case_id
        ]
        questions.sort(key=lambda item: (str(item.created_at or ""), item.id))
        return questions  # type: ignore[return-value]

    def _drafts_of(self, case_id: str) -> List[Draft]:
        drafts = [
            item
            for item in self._find("Draft", where={"case_id": case_id}, limit=None)
            if str(item.case_id) == case_id
        ]
        drafts.sort(key=lambda item: (str(item.created_at or ""), item.id))
        return drafts  # type: ignore[return-value]

    def _link(self, case_id: str, field_name: str, object_id: str) -> None:
        """把对象挂到 Case 的反向索引上（幂等：已在列表里就只写审计）。"""
        if not case_id:
            return
        case = self._get("Case", case_id)
        current = list(getattr(case, field_name, []) or [])
        if object_id in current:
            return
        current.append(object_id)
        setattr(case, field_name, current)
        self._store.put(
            case,
            expected_revision=int(case.revision),
            actor_kind=str(ActorKind.USER),
            actor_id="console",
            reason="登记关联对象",
            audit_action=str(AuditAction.UPDATE),
            audit_detail={"field": field_name, "object_id": object_id},
        )

    # ------------------------------------------------------------------ 校验小工具
    def _require_revision(self, obj: Any, expected: Optional[int], *, object_type: str) -> None:
        """版本必须匹配；不匹配 → 409 + 当前版本（绝不覆盖）。"""
        if expected is None:
            raise BadRequest("写操作必须带 expected_revision（防止覆盖他人修改）")
        actual = int(obj.revision)
        if int(expected) != actual:
            raise Conflict(
                "%s 已被其他操作修改：提交基于 revision=%s，当前是 revision=%d"
                % (object_type, expected, actual),
                object_type=object_type,
                object_id=obj.id,
                expected_revision=int(expected),
                current_revision=actual,
            )

    def _expected_revision(self, body: Dict[str, Any]) -> Optional[int]:
        if not isinstance(body, dict):
            raise BadRequest("请求体必须是 JSON 对象")
        if "expected_revision" not in body:
            return None
        return self._coerce_int(body.get("expected_revision"), "expected_revision")

    def _coerce_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or value is None:
            raise BadRequest("%s 必须是整数" % field)
        try:
            return int(value)
        except (TypeError, ValueError):
            raise BadRequest("%s 必须是整数（收到 %r）" % (field, value))

    def _text(self, value: Any, *, field: str, required: bool, limit: int = _MAX_TEXT) -> str:
        if value is None:
            if required:
                raise BadRequest("缺少字段 %s" % field)
            return ""
        if not isinstance(value, str):
            raise BadRequest("%s 必须是字符串" % field)
        if required and not value.strip():
            raise BadRequest("%s 不能为空" % field)
        if len(value) > limit:
            raise BadRequest("%s 过长（上限 %d 字符）" % (field, limit))
        return value

    def _case_state(self, value: str) -> str:
        from ..contracts.enums import CaseState as _CaseState

        try:
            return str(_CaseState.coerce(value))
        except Exception:
            raise BadRequest("未知的 Case 状态: %r" % value)

    def _new_id(self, object_type: str) -> str:
        return new_id(object_type, token=hashlib.sha256(
            ("%s|%s|%s" % (object_type, self.now(), id(self))).encode("utf-8")
        ).hexdigest()[:24])

    @contextmanager
    def _translated(self, *, object_type: str, object_id: str = "") -> Iterator[None]:
        """把存储层/理解层的异常翻译成控制台 HTTP 语义。

        * `RevisionConflict` → 409（旧版本写入）；
        * `DuplicateObject` → 409（唯一键冲突，同样属于"你基于的状态已变"）；
        * 存储层 `NotFound` → 404；
        * `PermissionDenied` → 403；
        * `ValidationError` / `IllegalTransition` → 400（带原始文本，前端能直接显示原因）。
        """
        try:
            yield
        except ConsoleError:
            raise
        except RevisionConflict as exc:
            raise Conflict(
                "写入被拒：对象版本已变化（%s）" % str(exc),
                object_type=object_type,
                object_id=object_id,
            )
        except DuplicateObject as exc:
            raise Conflict(
                "写入被拒：唯一键冲突（%s）" % str(exc),
                object_type=object_type,
                object_id=object_id,
            )
        except StoreNotFound as exc:
            raise NotFound(str(exc) or ("%s 不存在" % object_type))
        except PermissionDenied as exc:
            raise Forbidden(str(exc))
        except AsistentError as exc:
            # ValidationError / IllegalTransition / ApprovalInvalid … 都归到"请求或状态不允许"
            raise BadRequest(str(exc))


def _plain(value: Any) -> Any:
    """把契约对象里的嵌套结构转成纯 JSON 类型（枚举 → 字符串）。"""
    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "to_dict"):
        return _plain(value.to_dict())
    return str(value)


def _citation(row: Any) -> Dict[str, Any]:
    """引用：必须能被手机端打开——所以 path/locator/content_hash/supports 一个都不省。"""
    data = _plain(row)
    if not isinstance(data, dict):
        return {"path": "", "locator": "", "content_hash": "", "supports": "", "source_object_id": ""}
    return {
        "source_object_id": data.get("source_object_id", ""),
        "source_object_type": data.get("source_object_type", ""),
        "path": data.get("path", ""),
        "locator": data.get("locator", ""),
        "content_hash": data.get("content_hash", ""),
        "supports": data.get("supports", ""),
    }


def _sanitize_detail(detail: Any) -> Dict[str, Any]:
    """审计详情落盘前再兜一层：任何看起来像凭据的 key 都不进响应。"""
    data = _plain(detail or {})
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in data.items():
        lowered = str(key).lower()
        if any(bad in lowered for bad in _FORBIDDEN_DETAIL_KEYS):
            out[key] = "<redacted>"
        else:
            out[key] = value
    return out


__all__ = ["ConsoleService"]
