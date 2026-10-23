"""一轮里到底做什么（SPEC §4.1、§13.2/§13.3、§873-877）。

一整轮 = 七个阶段，**顺序固定**，且每个阶段互相隔离：

```text
1 recover      IN_FLIGHT → UNKNOWN（只读查证，绝不重发）+ 过期审批清扫
2 sync         邮件同步（游标/去重/失败状态化由 MVP-2 负责）
3 ingest       挂载资料目录采集 + FTS5 索引（MVP-1；含新同步进来的邮件）
4 understand   理解 → 事实/追问/草稿（MVP-3 + MVP-6 规则命中）
5 prepare      用户已批准草稿 → 派生外部动作 + 动作级审批（不发送）
6 outbound     领取 → 外呼 → 分类（MVP-5；一轮里每个动作最多领一次）
7 archive      已成功事务 → manifest + 逐文件回读校验（MVP-7）
```

三条不变量（跟 SPEC §13.2/§13.3 逐条对应）：

* **失败不接管服务**：每个阶段 `except Exception`，记进快照与该轮报告后继续；
  只有 `BaseException`（被强杀 / Ctrl-C 之外的 BaseException）原样上抛 —— 那正是
  "已提交、结果未落库就被杀"的语义，必须留给下一轮 `recover` 转 UNKNOWN；
* **恢复不重发**：`recover_phase` 里没有一条路径调用外发通道；
* **一轮不重复领同一动作**：`outbound_phase` 只遍历 ASSISTANT 授权态
  （`AUTHORIZED`）动作，`claim_action` 会把状态改成 `IN_FLIGHT`，因此**同一轮内**
  自然不会被第二次领取；跨轮的重试由用户裁决路径决定，不由本层自动决定。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..actions import ActionEngine, approve_and_authorize, create_approval, sweep_expired_approvals
from ..archive.engine import ArchiveEngine, ArchiveOutcome
from ..archive.manifest import build_archive_manifest, capture_source_tree
from ..contracts import (
    CONTRACT_VERSION,
    ActionSummary,
    ArchivePolicy,
    ExternalAction,
    action_idempotency_key,
    digest_of,
    new_id,
    sha256_file,
)
from ..contracts.enums import ActorKind, ArchiveState, AuditAction, CaseState, QuestionState
from ..contracts.errors import AsistentError, NotFound, PermissionDenied
from ..contracts.objects import AuditEvent, DraftVersion
from ..contracts.timeutil import utcnow_iso
from ..growth.bridge import case_is_blocked, process_with_growth, require_sendable
from ..understand.drafts import approval_binding, current_version, versions_of
from ..understand.understander import RuleBasedUnderstander
from .answers import AnswerAwareUnderstander, answered_questions, record_answer_facts
from .config import CHANNEL_NONE, ServiceConfig
from .health import ServiceState, infer_waiting_on, snapshot_pending

#: 还需要理解/重新理解的事务状态（其余状态要么已经产出草稿、要么已经终态）
ANALYZABLE_CASE_STATES: Tuple[str, ...] = (
    str(CaseState.NEW),
    str(CaseState.TRIAGED),
    str(CaseState.ANALYZING),
    str(CaseState.WAITING_FOR_CONTEXT),
    str(CaseState.WAITING_FOR_USER),
)

#: 归档候选：业务上已经"外部动作成功"的事务
ARCHIVABLE_CASE_STATES: Tuple[str, ...] = (str(CaseState.SUCCEEDED), str(CaseState.COMPLETED))

#: 本层自己写的审计动作名（不用契约枚举之外的字符串去冒充状态变化）
AUDIT_SERVICE_ANALYZE = "SERVICE_ANALYZE"
AUDIT_SERVICE_PREPARE = "SERVICE_PREPARE"
AUDIT_SERVICE_ROUND = "SERVICE_ROUND"

#: 归档"要等用户动手"的错误码：不是抖动，重试也不会好，必须显示成"在等你"而不是"故障"
ARCHIVE_BLOCKING_CODES: Tuple[str, ...] = ("NOT_AUTHORIZED", "PAN_NOT_INSTALLED", "APPROVAL_REQUIRED")
#: 兜底的下一步动作（通道自己没给时用）：手机端/文档要能直接拿去照做
ARCHIVE_NEXT_STEPS = {
    "NOT_AUTHORIZED": "完成百度网盘授权（bdpan login）后服务会自动重试归档，不需要人工点确认",
    "PAN_NOT_INSTALLED": "在这台设备上安装 bdpan CLI 并完成授权（用户动作 U-2），服务下一轮会自动重试",
    "APPROVAL_REQUIRED": "先拿到写入审批（unlock_writes），再让服务重试归档",
}


# --------------------------------------------------------------------------------------
# 一轮的上下文与报告
# --------------------------------------------------------------------------------------


@dataclass
class RunContext:
    """一轮所需的全部依赖（构造注入；没有全局状态）。"""

    config: ServiceConfig
    store: Any
    state: ServiceState
    clock: Callable[[], str] = utcnow_iso
    mail_engine: Any = None
    pipeline: Any = None
    action_engine: Optional[ActionEngine] = None
    archive_engine: Optional[ArchiveEngine] = None
    channel_ready: bool = False
    actor_id: str = "service"
    index_connection: Any = None

    def now(self) -> str:
        moment = self.clock()
        return str(moment)


@dataclass
class PhaseResult:
    """一个阶段的可观察结果。"""

    name: str
    ok: bool = True
    detail: Dict[str, Any] = field(default_factory=dict)
    error_class: str = ""
    error_text: str = ""
    skipped: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "skipped": self.skipped,
            "detail": dict(self.detail),
            "error_class": self.error_class,
            "error_text": self.error_text,
        }

    def summary_line(self) -> str:
        if self.skipped:
            return "%-11s SKIP %s" % (self.name, self.skipped)
        if not self.ok:
            return "%-11s FAIL %s: %s" % (self.name, self.error_class, self.error_text[:160])
        return "%-11s OK   %s" % (self.name, json.dumps(self.detail, ensure_ascii=False, sort_keys=True)[:400])


@dataclass
class RoundReport:
    """一次 `run_once()` 的完整记录（验收证据结构，`to_dict()` 即可打印）。"""

    round_index: int = 0
    started_at: str = ""
    finished_at: str = ""
    phases: List[PhaseResult] = field(default_factory=list)
    pending: Dict[str, int] = field(default_factory=dict)
    waiting_on: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: 本轮外发通道被 `send()` 调用的次数（"恰好一次"的可观察量）
    sender_call_count: int = 0
    #: 落盘外发账本里的 submit 行数（跨进程的硬证据）
    submit_count_on_disk: Optional[int] = None

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.phases)

    def phase(self, name: str) -> Optional[PhaseResult]:
        for item in self.phases:
            if item.name == name:
                return item
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_index": self.round_index,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "phases": [item.to_dict() for item in self.phases],
            "pending": dict(self.pending),
            "waiting_on": list(self.waiting_on),
            "notes": list(self.notes),
            "sender_call_count": self.sender_call_count,
            "submit_count_on_disk": self.submit_count_on_disk,
        }

    def summary_line(self) -> str:
        failed = [item.name for item in self.phases if not item.ok]
        return "round=%d %s phases=%d failed=%s pending=%s waiting=%s sender_calls=%d" % (
            self.round_index,
            "OK" if self.ok else "PARTIAL",
            len(self.phases),
            ",".join(failed) or "-",
            json.dumps(self.pending, ensure_ascii=False, sort_keys=True),
            ",".join(self.waiting_on) or "-",
            self.sender_call_count,
        )

    def evidence_lines(self) -> List[str]:
        """逐阶段的原始日志行（`--once` 与常驻循环打印的就是这些行）。"""
        return ["[round %d] %s" % (self.round_index, item.summary_line()) for item in self.phases]


# --------------------------------------------------------------------------------------
# 阶段 1：恢复 + 审批清扫
# --------------------------------------------------------------------------------------


def recover_phase(ctx: RunContext) -> PhaseResult:
    """重启恢复：`IN_FLIGHT → UNKNOWN`，绝不重发（SPEC §13.3、§21.2）。"""
    result = PhaseResult(name="recover")
    if ctx.action_engine is None:
        result.skipped = "未配置外发引擎（无动作需要恢复）"
        return result
    outcome = ctx.action_engine.recover(reason="服务轮次恢复：仍为 IN_FLIGHT 的动作先转 UNKNOWN 查证")
    reconciled: List[str] = []
    for action_id in outcome.demoted_action_ids:
        try:
            action = ctx.store.get("ExternalAction", action_id)
        except NotFound:  # pragma: no cover - 刚数出来就没了
            continue
        case = ctx.store.find_one("Case", id=action.case_id)
        # 动作结论回写事务：EXECUTING 的事务在结果未知时必须落 UNKNOWN，不能停在"正在执行"
        if case is not None and str(case.state) == str(CaseState.EXECUTING):
            try:
                ctx.store.transition(
                    "Case",
                    case.id,
                    str(CaseState.UNKNOWN),
                    actor_kind=str(ActorKind.SYSTEM),
                    actor_id=ctx.actor_id,
                    reason="重启恢复：动作结果未知，事务落 UNKNOWN 等用户裁决",
                )
                reconciled.append(case.id)
            except Exception as exc:  # noqa: BLE001 - 单个事务回写失败不影响恢复结论
                result.detail.setdefault("case_reconcile_errors", []).append("%s: %s" % (case.id, exc))
    result.detail = {
        "demoted_action_ids": list(outcome.demoted_action_ids),
        "unknown_action_ids": list(outcome.unknown_action_ids),
        "pending_decision_action_ids": list(outcome.pending_decision_action_ids),
        "resent_action_ids": list(outcome.resent_action_ids),
        "sender_call_count": int(outcome.sender_call_count),
        "case_ids_reconciled": reconciled,
    }
    return result


def sweep_phase(ctx: RunContext) -> PhaseResult:
    """把"已过期但仍 ACTIVE"的审批落成 EXPIRED（SPEC §8.4；只收紧不放宽）。"""
    result = PhaseResult(name="sweep")
    expired = sweep_expired_approvals(ctx.store, now=ctx.now(), limit=ctx.config.batch_limit * 10)
    result.detail = {"expired_approval_ids": list(expired)}
    return result


# --------------------------------------------------------------------------------------
# 阶段 2/3：同步与采集索引
# --------------------------------------------------------------------------------------


def sync_phase(ctx: RunContext) -> PhaseResult:
    result = PhaseResult(name="sync")
    if ctx.mail_engine is None:
        # 这里**只**能有"到底是什么原因没收信"。以前无论什么原因都写
        # `mail.enabled=false`，接上真实通道之后就变成一句假话：
        # 配置里明明写着 `enabled=true`（只是缺授权码），手机端却看到"邮件未启用"。
        # 真正的原因在 `state.notes` 里（谁接的邮件通道、缺什么、下一步做什么）。
        if getattr(ctx.config.mail, "enabled", False) and ctx.config.mail.source not in ("", "none"):
            result.skipped = "邮件通道未接入（mail.source=%s）：原因见状态说明" % ctx.config.mail.source
        else:
            result.skipped = "邮件同步未启用（mail.enabled=false）"
        return result
    report = ctx.mail_engine.sync_once()
    payload = report.to_dict() if hasattr(report, "to_dict") else {}
    result.detail = {
        "account": payload.get("account", ""),
        "folder": payload.get("folder", ""),
        "listed": int(payload.get("listed", 0) or 0),
        "documents_created": int(payload.get("documents_created", 0) or 0),
        "documents_updated": int(payload.get("documents_updated", 0) or 0),
        "duplicates_skipped": int(payload.get("duplicates_skipped", 0) or 0),
        "failed": len(payload.get("failures", []) or []),
        "complete": bool(payload.get("complete", True)),
        "cursor_after": payload.get("cursor_after", ""),
        "aborted_reason": payload.get("aborted_reason", ""),
        "result": "OK" if payload.get("complete", True) else "PARTIAL",
    }
    if not payload.get("complete", True):
        result.ok = False
        result.error_class = "SyncIncomplete"
        result.error_text = str(payload.get("aborted_reason", "") or "同步未完成（游标不推进）")
    return result


def ingest_phase(ctx: RunContext) -> PhaseResult:
    """采集挂载目录 + 索引（含刚同步进来的邮件）。"""
    result = PhaseResult(name="ingest")
    if ctx.pipeline is None:
        result.skipped = "索引流水线未配置"
        return result
    detail: Dict[str, Any] = {}
    if ctx.config.ingest.enabled and ctx.config.ingest.roots:
        run = ctx.pipeline.collect(list(ctx.config.ingest.roots))
        detail["collect"] = {
            "scanned": int(getattr(run.collection, "scanned", 0) or 0),
            "created": int(getattr(run.collection, "created", 0) or 0),
            "updated": int(getattr(run.collection, "updated", 0) or 0),
            "skipped": int(getattr(run.collection, "skipped", 0) or 0),
            "seconds": round(float(run.collect_seconds), 6),
        }
        indexing = run.indexing
    else:
        indexing = ctx.pipeline.indexer.sync()
    detail["index"] = {
        "documents": int(getattr(indexing, "total", 0) or len(getattr(indexing, "results", []) or [])),
        "indexed": len([item for item in (getattr(indexing, "results", []) or []) if getattr(item, "ok", False)]),
        "failed": len([item for item in (getattr(indexing, "results", []) or []) if not getattr(item, "ok", True)]),
    }
    detail["freshness"] = ctx.pipeline.freshness()
    result.detail = detail
    return result


# --------------------------------------------------------------------------------------
# 阶段 4：理解（含规则命中；MVP-3 + MVP-6 真实串联）
# --------------------------------------------------------------------------------------


def _documents_of(store: Any, case_id: str) -> List[Any]:
    rows = [item for item in store.find("SourceDocument", limit=None) if str(item.case_id or "") == case_id]
    rows.sort(key=lambda item: (str(item.created_at or ""), item.id))
    return rows


def _analyzed_documents(store: Any, case_id: str) -> Dict[str, str]:
    """已"理解完成"的文档 → 内容哈希。**两个来源并集**，都是从存储里读回来的：

    * 契约层发布的 `Event`（`payload.document_id` / `payload.content_hash`，MVP-3 口径）；
    * 本层自己的"服务理解"审计（`detail.document_id` / `detail.content_hash`）。

    为什么必须并集：`Event` 表在 MVP-8 的实测里**是空的**（写入层没有发布文档分析事件），
    只靠它会导致"已经在等用户回答"的事务每轮都被重新理解一遍——那既烧 CPU 又会刷审计。
    服务自己写的审计是同一份存储里的追加记录，重启后照样成立，所以拿它兜底是诚实的。
    """
    done: Dict[str, str] = {}
    try:
        events = store.find("Event", where={"case_id": case_id}, limit=None)
    except Exception:  # noqa: BLE001 - 不同存储实现对 where 的支持面不同
        events = []
    for event in events:
        payload = dict(getattr(event, "payload", {}) or {})
        document_id = str(payload.get("document_id") or "")
        if document_id:
            done[document_id] = str(payload.get("content_hash") or "")
    try:
        trail = store.audit_trail(case_id=case_id, limit=None)
    except Exception:  # noqa: BLE001
        trail = []
    for row in trail:
        if not str(getattr(row, "reason", "") or "").startswith("服务理解"):
            continue
        detail = dict(getattr(row, "detail", {}) or {})
        document_id = str(detail.get("document_id") or "")
        if document_id:
            done[document_id] = str(detail.get("content_hash") or "")
    return done


def _consumed_answer_ids(store: Any, case_id: str) -> List[str]:
    """本层已经"吃进理解"的回答 ID（从审计里读，进程重启后仍然成立）。"""
    consumed: List[str] = []
    try:
        trail = store.audit_trail(case_id=case_id, limit=None)
    except Exception:  # noqa: BLE001
        return consumed
    for event in trail:
        if str(getattr(event, "reason", "") or "").startswith("服务理解"):
            for item in list((getattr(event, "detail", {}) or {}).get("answer_ids", []) or []):
                if item not in consumed:
                    consumed.append(str(item))
    return consumed


def case_needs_analysis(store: Any, case: Any) -> Tuple[bool, str]:
    """这个事务这一轮要不要（重新）理解？返回 `(需要?, 原因)`。"""
    state = str(case.state)
    if state not in ANALYZABLE_CASE_STATES:
        return False, "state=%s 不在需要理解的状态集合里" % state
    if case_is_blocked(store, case.id):
        return False, "规则冲突未裁决：暂停自动分析（SPEC §861）"
    documents = _documents_of(store, case.id)
    if not documents:
        return False, "没有任何 SourceDocument"
    analyzed = _analyzed_documents(store, case.id)
    fresh = [
        item
        for item in documents
        if analyzed.get(item.id) != str(item.content_hash or "")
    ]
    consumed = _consumed_answer_ids(store, case.id)
    pending_answers = [
        str(getattr(item, "answer_id", "") or "")
        for item in answered_questions(store, case.id)
        if str(getattr(item, "answer_id", "") or "") and str(getattr(item, "answer_id")) not in consumed
    ]
    if fresh:
        return True, "有 %d 份资料尚未理解" % len(fresh)
    if pending_answers:
        return True, "有 %d 条用户回答尚未并入理解" % len(pending_answers)
    return False, "资料与回答都已并入理解（幂等跳过，避免每轮刷审计）"


def _document_bytes(document: Any) -> Optional[bytes]:
    path = str(getattr(document, "origin_path", "") or "")
    if not path or not os.path.isfile(path):
        return None
    with open(path, "rb") as handle:
        return handle.read()


def understand_phase(ctx: RunContext) -> PhaseResult:
    """理解 → 落对象；规则（MVP-6）真实参与，命中写审计（含版本号）。"""
    result = PhaseResult(name="understand")
    analyzed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    cases = list(ctx.store.find("Case", limit=ctx.config.batch_limit * 4))
    cases.sort(key=lambda item: (str(item.created_at or ""), item.id))
    for case in cases:
        if len(analyzed) >= ctx.config.batch_limit:
            break
        needed, why = case_needs_analysis(ctx.store, case)
        if not needed:
            skipped.append({"case_id": case.id, "why": why})
            continue
        documents = _documents_of(ctx.store, case.id)
        for document in documents:
            raw = _document_bytes(document)
            if raw is None:
                skipped.append({"case_id": case.id, "why": "原文不可读：%s" % document.id})
                continue
            # MVP-3 抽取 + MVP-6 规则：AnswerAware 只做"已回答的追问不再阻塞"这一层兜底
            base = AnswerAwareUnderstander(
                ctx.store,
                case_id=case.id,
                base=RuleBasedUnderstander(),
                clock=ctx.now,
                actor_id=ctx.actor_id,
            )
            report, growth = process_with_growth(
                ctx.store,
                document,
                eml_bytes=raw,
                base=base,
                clock=ctx.now,
                actor_id=ctx.actor_id,
                create_draft=ctx.config.resolve_answers,
            )
            resolved_answers = record_answer_facts(ctx.store, case_id=case.id, actor_id=ctx.actor_id)
            answer_ids = [
                str(getattr(item, "answer_id", "") or "")
                for item in answered_questions(ctx.store, case.id)
                if str(getattr(item, "answer_id", "") or "")
            ]
            ctx.store.append_audit(
                AuditEvent(
                    at=ctx.now(),
                    action=str(AuditAction.UPDATE),
                    actor_kind=str(ActorKind.AGENT),
                    actor_id=ctx.actor_id,
                    case_id=case.id,
                    object_type="SourceDocument",
                    object_id=document.id,
                    result="OK",
                    reason="服务理解：第 %d 次（重复处理只留审计与命中记录）" % (2 if report.duplicate_run else 1),
                    detail={
                        "document_id": document.id,
                        # 内容哈希进审计：下一轮"这份资料是不是已经理解过"就靠它（换进程也成立）
                        "content_hash": str(getattr(document, "content_hash", "") or ""),
                        "case_state": report.case_state,
                        "draftable": bool(report.draftable),
                        "questions": len([item for item in report.outcomes if item.kind == "Question"]),
                        "drafts": len([item for item in report.outcomes if item.kind == "Draft"]),
                        "answer_ids": answer_ids,
                        "answer_fact_ids": resolved_answers,
                        "resolved_questions": list(base.resolved),
                        "rule_hits": [item.to_dict() for item in growth.hits],
                        "rule_conflicts": list(growth.conflicts),
                        "blocked_reason": growth.blocked_reason,
                    },
                )
            )
            analyzed.append(
                {
                    "case_id": case.id,
                    "document_id": document.id,
                    "case_state": report.case_state,
                    "draftable": bool(report.draftable),
                    "counts": dict(report.counts),
                    "rule_hits": [
                        "rule=%s v%s effect=%s applied=%s" % (hit.rule_id, hit.rule_version, hit.effect, hit.applied)
                        for hit in growth.hits
                    ],
                    "resolved_question_count": len(base.resolved),
                    "summary": report.summary_line(),
                    "growth_summary": growth.summary_line(),
                }
            )
    result.detail = {
        "analyzed": analyzed,
        "skipped": skipped[:50],
        "skipped_count": len(skipped),
    }
    return result


# --------------------------------------------------------------------------------------
# 阶段 5：把"用户已批准的草稿"变成动作（不发送）
# --------------------------------------------------------------------------------------


def _active_approval_for_version(store: Any, case_id: str, version_id: str) -> Optional[Any]:
    """找出用户通过认证通道给出的、覆盖这一版草稿的 ACTIVE 审批。"""
    rows = [item for item in store.find("Approval", where={"case_id": case_id}, limit=None)]
    rows.sort(key=lambda item: (str(item.created_at or ""), item.id))
    for approval in rows:
        if str(approval.state) != "ACTIVE":
            continue
        bound = set(approval.target_object_ids or []) | set((approval.content_version_refs or {}).keys())
        if version_id in bound:
            return approval
    return None


def _action_for_version(ctx: RunContext, case: Any, version: Any) -> Optional[ExternalAction]:
    for action in ctx.store.find("ExternalAction", where={"case_id": case.id}, limit=None):
        summary = dict(getattr(action, "action_summary", {}) or {})
        if str(summary.get("draft_version_id") or "") == version.id and str(action.state) != str(
            "CANCELLED"
        ):
            return action
    return None


def build_action_summary(
    *,
    case: Any,
    version: DraftVersion,
    from_account: str,
    target_ref: str,
    subject: str,
) -> ActionSummary:
    """从"用户批准的那一版草稿"确定性地派生动作摘要（不含任何模型自由发挥）。"""
    summary = ActionSummary(
        kind="send_email",
        from_account=from_account,
        to=[target_ref] if target_ref else [],
        subject=subject,
        body=str(version.body or ""),
        draft_version_id=version.id,
        target_thread_ref=str(getattr(case, "conversation_ids", [""])[0] if case.conversation_ids else ""),
        evidence_versions={"draft": version.id, "content_hash": str(version.content_hash or "")},
    )
    summary.validate()
    return summary


def prepare_phase(ctx: RunContext) -> PhaseResult:
    """已批准草稿 → `ExternalAction`（PREPARED → AUTHORIZED），**不发送**。

    这一步刻意拆出来，好让"批准"和"发送"之间的每一环都可单独取证：
    动作摘要、动作级审批、幂等键、Case 状态都在发送之前就落库了。
    """
    result = PhaseResult(name="prepare")
    prepared: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    from_account = ctx.config.outbound.from_account
    for case in ctx.store.find("Case", limit=ctx.config.batch_limit * 4):
        if str(case.state) != str(CaseState.READY_FOR_REVIEW):
            continue
        # growth 的硬门闸：`Case.blocked_reason` 非空（规则冲突/资料矛盾/缺材料）不得产出可发送结论
        try:
            require_sendable(ctx.store, case.id)
        except PermissionDenied as exc:
            skipped.append({"case_id": case.id, "why": exc.message})
            continue
        draft = None
        for draft_id in list(case.draft_ids or []):
            try:
                candidate = ctx.store.get("Draft", draft_id)
            except NotFound:
                continue
            if str(candidate.state) == "OPEN":
                draft = candidate
                break
        if draft is None:
            skipped.append({"case_id": case.id, "why": "没有 OPEN 草稿"})
            continue
        version = current_version(ctx.store, draft.id)
        if version is None:
            skipped.append({"case_id": case.id, "why": "草稿没有版本"})
            continue
        approval = _active_approval_for_version(ctx.store, case.id, version.id)
        if approval is None:
            skipped.append({"case_id": case.id, "why": "用户尚未批准当前版本 %s" % version.id})
            continue
        existing = _action_for_version(ctx, case, version)
        if existing is not None and str(existing.state) in ("AUTHORIZED", "IN_FLIGHT", "SUCCEEDED", "UNKNOWN"):
            skipped.append({"case_id": case.id, "why": "该版本已有动作 %s（state=%s）" % (existing.id, existing.state)})
            continue
        if not from_account:
            skipped.append({"case_id": case.id, "why": "未配置发件账号（outbound.from_account）"})
            continue

        binding = approval_binding(ctx.store, draft.id, int(version.version_no))
        target_ref = str(draft.target_ref or "")
        subject = "Re: %s" % (version.summary or draft.target_ref or "回复")
        summary = build_action_summary(
            case=case,
            version=version,
            from_account=from_account,
            target_ref=target_ref,
            subject=subject,
        )
        if binding.content_hash and str(version.content_hash or "") == str(binding.content_hash):
            pass  # 审批绑定的就是这一版的哈希（下面 create_approval 会再校验一次）
        action_id = new_id("ExternalAction", token=digest_of(["service_action", case.id, version.id])[:24])
        try:
            action = ctx.store.get("ExternalAction", action_id)
            state = str(action.state)
        except NotFound:
            action = ctx.store.put(
                ExternalAction(
                    id=action_id,
                    case_id=case.id,
                    kind="send_email",
                    idempotency_key="pending",
                    action_summary=summary.normalized(),
                    summary_digest=summary.digest(),
                    target_ref=target_ref,
                    connector="mail:service",
                    prepared_at=ctx.now(),
                ),
                actor_kind=str(ActorKind.AGENT),
                actor_id=ctx.actor_id,
                reason="用户已批准草稿版本，派生外部动作（尚未发送）",
                audit_action=str(AuditAction.CREATE),
                audit_detail={"draft_version_id": version.id, "approval_id": approval.id},
            )
            action.idempotency_key = action_idempotency_key(action.id)
            ctx.store.put(
                action,
                expected_revision=action.revision,
                actor_kind=str(ActorKind.AGENT),
                actor_id=ctx.actor_id,
                reason="分配稳定幂等键（同一动作的所有重试沿用）",
            )
            state = str(ctx.store.get("ExternalAction", action.id).state)
        if state not in ("PREPARED",):
            skipped.append({"case_id": case.id, "why": "动作 %s 已在 state=%s" % (action_id, state)})
            continue

        # 用户的批准是**认证控制操作**（控制在 phone/console 上完成）；这里把同一个依据
        # 投影到"这一版动作"上：证据串指向用户那张审批，不是服务自称批准（SPEC §21.3）。
        projected = create_approval(
            ctx.store,
            summary=summary,
            action=ctx.store.get("ExternalAction", action_id),
            case_id=case.id,
            approver=str(getattr(approval, "approver", "") or "user"),
            auth_channel=str(getattr(approval, "auth_channel", "") or "console"),
            auth_evidence="console-approval:%s" % approval.id,
        )
        saved = approve_and_authorize(
            ctx.store,
            summary=summary,
            action=ctx.store.get("ExternalAction", action_id),
            approver=str(getattr(approval, "approver", "") or "user"),
            auth_channel=str(getattr(approval, "auth_channel", "") or "console"),
            auth_evidence="console-approval:%s" % approval.id,
        )
        action_object = ctx.store.get("ExternalAction", action_id)
        if action_id not in list(case.action_ids or []):
            case = ctx.store.get("Case", case.id)
            case.action_ids = list(dict.fromkeys(list(case.action_ids or []) + [action_id]))
            ctx.store.put(case, expected_revision=case.revision, reason="关联外部动作")
        try:
            ctx.store.transition(
                "Case",
                case.id,
                str(CaseState.APPROVED),
                actor_kind=str(ActorKind.AGENT),
                actor_id=ctx.actor_id,
                reason="用户已在认证通道批准草稿版本，事务进入 APPROVED",
            )
        except Exception as exc:  # noqa: BLE001 - 状态机拒绝也不该让整轮失败
            skipped.append({"case_id": case.id, "why": "Case→APPROVED 被状态机拒绝：%s" % exc})
        prepared.append(
            {
                "case_id": case.id,
                "action_id": saved.id,
                "action_state": str(action_object.state),
                "approval_id": projected.id,
                "user_approval_id": approval.id,
                "draft_version_id": version.id,
                "summary_digest": summary.digest(),
                "idempotency_key": action_object.idempotency_key,
            }
        )
    result.detail = {"prepared": prepared, "skipped": skipped[:50], "skipped_count": len(skipped)}
    return result


# --------------------------------------------------------------------------------------
# 阶段 6：外发（恰好一次）
# --------------------------------------------------------------------------------------


def outbound_phase(ctx: RunContext) -> PhaseResult:
    """领取并执行 `AUTHORIZED` 动作。没有配置通道时**不做任何尝试**（fail-closed）。"""
    result = PhaseResult(name="outbound")
    if ctx.action_engine is None:
        result.skipped = "未配置外发引擎"
        return result
    if not ctx.config.outbound.enabled:
        result.skipped = "outbound.enabled=false"
        return result
    if ctx.config.outbound.channel == CHANNEL_NONE:
        result.skipped = "outbound.channel=none（未配置受控通道：拒绝外呼，动作留在 AUTHORIZED）"
        return result

    executed: List[Dict[str, Any]] = []
    actions = list(ctx.store.find("ExternalAction", where={"state": "AUTHORIZED"}, limit=ctx.config.batch_limit))
    actions.sort(key=lambda item: (str(item.prepared_at or ""), item.id))
    for action in actions:
        case = ctx.store.find_one("Case", id=action.case_id)
        if case is not None and str(case.state) == str(CaseState.APPROVED):
            try:
                ctx.store.transition(
                    "Case",
                    case.id,
                    str(CaseState.EXECUTING),
                    actor_kind=str(ActorKind.SYSTEM),
                    actor_id=ctx.actor_id,
                    reason="开始执行已授权外部动作",
                )
            except Exception as exc:  # noqa: BLE001
                result.detail.setdefault("case_transition_errors", []).append("%s: %s" % (case.id, exc))
        # 注意：`execute` 内部的崩溃类异常（BaseException）**故意**不捕获，
        # 让 IN_FLIGHT 留在库里，下一轮的 recover 把它转成 UNKNOWN。
        outcome = ctx.action_engine.execute(action.id, worker=ctx.config.outbound.node)
        executed.append(
            {
                "action_id": outcome.action_id,
                "result_class": outcome.result_class,
                "action_state": outcome.action_state,
                "sender_called": bool(outcome.sender_called),
                "sender_call_count": int(outcome.sender_call_count),
                "attempt_no": int(outcome.attempt_no),
                "idempotency_key": outcome.idempotency_key,
                "external_ref": outcome.external_ref,
                "reason": outcome.reason,
            }
        )
    result.detail = {
        "executed": executed,
        "sender_call_count": int(ctx.action_engine.sender_call_count()),
    }
    return result


# --------------------------------------------------------------------------------------
# 阶段 7：归档
# --------------------------------------------------------------------------------------


def staging_dir_for(config: ServiceConfig, case_id: str) -> str:
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in case_id)
    return os.path.join(config.archive.staging_root, safe or "case-unknown")


def stage_case_material(ctx: RunContext, case: Any) -> Tuple[str, List[Dict[str, str]]]:
    """把该事务要归档的东西**复制**到一个冻结目录（原文件绝不被改动）。

    归档对象 = 该 Case 每份原文的字节 + 一份 `case.json` 摘要（含规则/流程版本引用）。
    """
    directory = staging_dir_for(ctx.config, case.id)
    if os.path.isdir(directory):
        shutil.rmtree(directory)
    os.makedirs(directory, exist_ok=True)
    sources: List[Dict[str, str]] = []
    for document in _documents_of(ctx.store, case.id):
        raw = _document_bytes(document)
        if raw is None:
            continue
        name = "%s.eml" % str(document.id).replace(":", "-")
        target = os.path.join(directory, name)
        with open(target, "wb") as handle:
            handle.write(raw)
        sources.append(
            {
                "relative_path": name,
                "document_id": document.id,
                "content_hash": str(document.content_hash or ""),
                "sha256": sha256_file(target),
            }
        )
    summary = {
        "case_id": case.id,
        "goal": case.goal,
        "state": str(case.state),
        "archive_state": str(getattr(case, "archive_state", "") or ""),
        "rule_ids": list(case.rule_ids or []),
        "workflow_ids": list(getattr(case, "workflow_ids", []) or []),
        "fact_ids": list(case.fact_ids or []),
        "question_ids": list(case.question_ids or []),
        "draft_ids": list(case.draft_ids or []),
        "approval_ids": list(case.approval_ids or []),
        "action_ids": list(case.action_ids or []),
        "document_ids": [item.id for item in _documents_of(ctx.store, case.id)],
        "staged_at": ctx.now(),
        "days_of_work": [],
    }
    case_path = os.path.join(directory, "case.json")
    with open(case_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    sources.append({"relative_path": "case.json", "document_id": case.id, "content_hash": "", "sha256": sha256_file(case_path)})
    return directory, sources


def _archive_once(ctx: RunContext, case: Any, manifest: Dict[str, Any], files: Any) -> Any:
    """调一次归档，把"通道暂时不可用"变成**结构化结论**而不是异常。

    网盘没装/没授权是**已知状态**（用户动作 U-2 还没做完），不是故障：这一路要如实记账
    （`NOT_AUTHORIZED` + 下一步动作）、继续跑别的阶段、下一轮再试，
    而不是让整轮变成"失败"（那会淹没真正的故障）。
    """
    try:
        return ctx.archive_engine.archive(manifest, files)
    except AsistentError as exc:
        code = str(getattr(exc, "code", "") or type(exc).__name__)
        detail = dict(getattr(exc, "detail", {}) or {})
        return ArchiveOutcome(
            archive_id=manifest["archive_id"],
            state=str(ArchiveState.PENDING),
            manifest_digest=str(manifest.get("manifest_digest") or ""),
            object_count=len(files),
            attempt=1,
            failed=[{"case_id": case.id, "code": code, "message": str(exc)[:300]}],
            retryable=bool(detail.get("retryable", code not in ARCHIVE_BLOCKING_CODES)),
            integrity_claim={},
            error_code=code,
            error_text=str(exc)[:500],
            next_step=str(detail.get("next_step") or "") or ARCHIVE_NEXT_STEPS.get(code, ""),
            remote_root=str(manifest.get("remote_root") or "") or None,
            notes=["归档通道不可用（已知状态）：本轮只记账，下一轮再试"],
        )


def archive_phase(ctx: RunContext) -> PhaseResult:
    result = PhaseResult(name="archive")
    if ctx.archive_engine is None:
        result.skipped = "归档未启用（archive.enabled=false 或通道为 none）"
        return result
    archived: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for case in ctx.store.find("Case", limit=ctx.config.batch_limit * 4):
        if str(case.state) not in ARCHIVABLE_CASE_STATES:
            continue
        if str(getattr(case, "archive_state", "") or "") == "VERIFIED":
            continue
        directory, sources = stage_case_material(ctx, case)
        files, excluded, scan = capture_source_tree(directory)
        if not files:
            failed.append({"case_id": case.id, "why": "没有可归档对象"})
            continue
        manifest = build_archive_manifest(
            scope="case",
            scope_ref=case.id,
            files=files,
            source_versions={item["relative_path"]: item["sha256"] for item in sources},
            code_version=CONTRACT_VERSION,
            contract_version=CONTRACT_VERSION,
            rule_version=",".join(sorted(set(case.rule_ids or []))) or None,
            remote_root=ctx.config.archive.root,
            secret_scan=scan,
            excluded=excluded,
            extra={"policy_gate": "U-6 未确认：本次不带 ArchivePolicy（spec §21.3）", "staged_sources": sources},
        )
        outcome = _archive_once(ctx, case, manifest, files)
        payload = outcome.to_dict()
        failed_detail = [dict(item) for item in (outcome.failed or []) if isinstance(item, dict)]
        record = {
            "case_id": case.id,
            "archive_id": outcome.archive_id,
            "state": outcome.state,
            "ok": bool(outcome.ok),
            "uploaded": len(outcome.uploaded),
            "verified": len(outcome.verified),
            "failed": len(outcome.failed),
            "error_code": outcome.error_code,
            "error_text": outcome.error_text,
            "next_step": outcome.next_step,
            "manifest_digest": outcome.manifest_digest,
            "content_origin_summary": payload.get("content_origin_summary", {}),
            "failed_detail": failed_detail[:5],
        }
        if outcome.error_code == "NOT_AUTHORIZED":
            record["why"] = "网盘未授权：显式 NOT_AUTHORIZED，一次写调用都没有发出（用户动作 U-2）"
        elif not outcome.error_code and failed_detail and {
            str(item.get("reason") or "") for item in failed_detail
        } == {"readback-unknown"}:
            record["why"] = (
                "通道无法判定远端是否已有该对象（readback-unknown）：按 fail-safe 不上传，"
                "既不重复上传也不当作失败，下一轮再判"
            )
        archived.append(record)
        if not outcome.ok:
            failed.append({"case_id": case.id, "state": outcome.state, "error_code": outcome.error_code})
    result.detail = {"archived": archived, "failed": failed}
    return result


# --------------------------------------------------------------------------------------
# 轮次编排
# --------------------------------------------------------------------------------------


def run_phases(ctx: RunContext, *, round_index: int, run_id: str = "") -> RoundReport:
    """跑一轮的全部阶段：每个阶段独立隔离，失败只记录、不中断。"""
    report = RoundReport(round_index=round_index, started_at=ctx.now())
    steps = (
        ("recover", recover_phase),
        ("sweep", sweep_phase),
        ("sync", sync_phase),
        ("ingest", ingest_phase),
        ("understand", understand_phase),
        ("prepare", prepare_phase),
        ("outbound", outbound_phase),
        ("archive", archive_phase),
    )
    for name, function in steps:
        try:
            result = function(ctx)
        except BaseException as exc:  # noqa: BLE001 - 崩溃类异常必须原样上抛（见模块首文档）
            if not isinstance(exc, Exception):
                raise
            ctx.state.record_error(name, exc)
            report.phases.append(
                PhaseResult(name=name, ok=False, error_class=type(exc).__name__, error_text=str(exc))
            )
            report.notes.append("%s 阶段失败：%s" % (name, exc))
            continue
        if not result.ok:
            ctx.state.record_error(name, AsistentError(result.error_text or result.error_class))
        report.phases.append(result)

    # ---- 现场从存储算计数与"正在等什么"（不依赖进程内存）
    try:
        report.pending = snapshot_pending(ctx.store)
    except Exception as exc:  # noqa: BLE001
        report.pending = {}
        report.notes.append("统计待处理计数失败：%s" % exc)
    report.waiting_on = infer_waiting_on(
        report.pending,
        channel_ready=ctx.channel_ready,
        archive_blocked=bool(
            [
                item
                for item in (
                    report.phase("archive").detail.get("archived", []) if report.phase("archive") else []
                )
                if item.get("error_code") in ARCHIVE_BLOCKING_CODES
            ]
        ),
    )
    if ctx.action_engine is not None:
        report.sender_call_count = int(ctx.action_engine.sender_call_count())
    log_path = getattr(ctx.state, "outbound_log_path", "")
    if log_path:
        from ..actions import MailCallLog

        try:
            report.submit_count_on_disk = MailCallLog.submitted_count_on_disk(log_path)
        except Exception:  # noqa: BLE001
            report.submit_count_on_disk = None

    # ---- 快照与审计（"关掉客户端后仍能看到状态"靠这两处）
    ctx.state.pending = dict(report.pending)
    ctx.state.waiting_on = list(report.waiting_on)
    # "最近一次同步/索引是什么时候、结果如何"必须进快照：手机端第一眼要看的两个数字
    for name, slot in (("sync", "last_sync"), ("ingest", "last_ingest")):
        result = report.phase(name)
        if result is None:
            continue
        payload = dict(result.detail)
        payload["at"] = report.finished_at or report.started_at
        payload["ok"] = bool(result.ok)
        payload["skipped"] = result.skipped
        if result.error_class:
            payload["error"] = "%s: %s" % (result.error_class, result.error_text)
        setattr(ctx.state, slot, payload)
    ctx.state.record_round(report.to_dict(), at=report.started_at)
    _audit_round(ctx, report, run_id=run_id)
    report.finished_at = ctx.now()
    ctx.state.last_round["finished_at"] = report.finished_at
    ctx.state.last_round["ok"] = report.ok
    return report


def _audit_round(ctx: RunContext, report: RoundReport, *, run_id: str) -> None:
    detail = {
        "round_index": report.round_index,
        "ok": report.ok,
        "run_id": run_id,
        "pending": dict(report.pending),
        "waiting_on": list(report.waiting_on),
        "sender_call_count": report.sender_call_count,
        "submit_count_on_disk": report.submit_count_on_disk,
        "phases": {item.name: (item.skipped or ("OK" if item.ok else "%s: %s" % (item.error_class, item.error_text))) for item in report.phases},
    }
    for key, value in (
        ("sync", report.phase("sync")),
        ("outbound", report.phase("outbound")),
        ("archive", report.phase("archive")),
    ):
        if value is not None and value.detail:
            detail[key] = value.detail
    ctx.store.append_audit(
        AuditEvent(
            at=report.started_at,
            action=str(AuditAction.WRITE),
            actor_kind=str(ActorKind.SYSTEM),
            actor_id=ctx.actor_id,
            result="OK" if report.ok else "FAILED",
            reason="服务轮次：第 %d 轮（%s）" % (report.round_index, "全部阶段成功" if report.ok else "有阶段失败"),
            detail=detail,
        )
    )


__all__ = [
    "ANALYZABLE_CASE_STATES",
    "ARCHIVABLE_CASE_STATES",
    "AUDIT_SERVICE_ANALYZE",
    "AUDIT_SERVICE_PREPARE",
    "AUDIT_SERVICE_ROUND",
    "PhaseResult",
    "RoundReport",
    "RunContext",
    "archive_phase",
    "build_action_summary",
    "case_needs_analysis",
    "ingest_phase",
    "outbound_phase",
    "prepare_phase",
    "recover_phase",
    "run_phases",
    "stage_case_material",
    "staging_dir_for",
    "sweep_phase",
    "sync_phase",
    "understand_phase",
]
