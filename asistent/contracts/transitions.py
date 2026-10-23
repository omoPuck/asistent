"""状态机与合法转换表（SPEC §4.4、§5.2、§21.1、§21.2）。

- 每个状态机给出**合法转换表**（触发者、前置条件、事件、可否重试）；
- 非法转换必须拒绝并记录原因（MVP-0 验收）；
- 业务状态、审批状态、动作状态、归档状态**各自独立**持久化；
- Case 不是严格单一路径：新邮件追加、追问回答、草稿编辑可以回到分析/复核。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import IllegalTransition

# 触发者标识
TRIGGER_AGENT = "AGENT"
TRIGGER_USER = "USER"
TRIGGER_SYSTEM = "SYSTEM"
TRIGGER_EXTERNAL = "EXTERNAL"


@dataclass(frozen=True)
class TransitionRule:
    source: str
    target: str
    #: 默认三类主体都可触发业务状态变化；需要限制的行显式给出 triggers
    triggers: Tuple[str, ...] = (TRIGGER_AGENT, TRIGGER_USER, TRIGGER_SYSTEM)
    #: 是否要求「尚无不可逆外部动作」——取消/阻塞必须满足
    requires_no_irreversible: bool = False
    #: 该转换产生的审计动作名（默认 TRANSITION）
    audit_action: str = "TRANSITION"
    note: str = ""

    def allows(self, trigger: Optional[str]) -> bool:
        if trigger is None:
            return True
        return trigger.upper() in self.triggers


def _rows(source: str, targets: Sequence[str], **kw) -> List[TransitionRule]:
    return [TransitionRule(source, target, **kw) for target in targets]


ANY = "*"

_CASE_TABLE: List[TransitionRule] = [
    *_rows("NEW", ["TRIAGED", "WAITING_FOR_CONTEXT", "BLOCKED", "CANCELLED"], requires_no_irreversible=False),
    *_rows("TRIAGED", ["ANALYZING", "WAITING_FOR_CONTEXT", "WAITING_FOR_USER", "READY_FOR_REVIEW", "BLOCKED", "CANCELLED"]),
    *_rows("WAITING_FOR_CONTEXT", ["TRIAGED", "ANALYZING", "WAITING_FOR_USER", "BLOCKED", "CANCELLED"]),
    *_rows("ANALYZING", ["TRIAGED", "WAITING_FOR_CONTEXT", "WAITING_FOR_USER", "READY_FOR_REVIEW", "FAILED", "BLOCKED", "CANCELLED"]),
    # 契约 v0.8（CP-20261010-24）：用户的回答可能**暴露新的上下文缺口**（"这个活动在哪个城市"
    # → 回答里又出现一个没见过的截止时间），这时必须能回到"等上下文"而不是硬撑在"等用户"，
    # 也不能为了表达"追问答完了"就让状态原地不动——手机端会看到"旧状态 + 新下一步动作"的自相矛盾。
    *_rows("WAITING_FOR_USER", ["ANALYZING", "WAITING_FOR_CONTEXT", "READY_FOR_REVIEW", "BLOCKED", "CANCELLED"]),
    # 追问回答 / 草稿编辑可以把事务拉回复核或分析（§21.1）
    *_rows("READY_FOR_REVIEW", ["ANALYZING", "WAITING_FOR_USER", "APPROVED", "FAILED", "BLOCKED", "CANCELLED"]),
    *_rows("APPROVED", ["EXECUTING", "READY_FOR_REVIEW", "BLOCKED", "CANCELLED"]),
    *_rows("EXECUTING", ["SUCCEEDED", "FAILED", "UNKNOWN", "BLOCKED"]),
    *_rows("SUCCEEDED", ["ARCHIVING", "ARCHIVE_PENDING", "COMPLETED", "BLOCKED"]),
    *_rows("FAILED", ["ANALYZING", "READY_FOR_REVIEW", "ARCHIVING", "ARCHIVE_PENDING", "BLOCKED", "CANCELLED"]),
    # UNKNOWN：结果是人工裁决，不得自动重发（§21.2）
    *_rows("UNKNOWN", ["WAITING_FOR_USER", "READY_FOR_REVIEW", "FAILED", "SUCCEEDED", "ARCHIVING", "ARCHIVE_PENDING", "BLOCKED", "CANCELLED"]),
    # 归档失败或结果不确定时，业务状态不能被归档状态污染：仍可回到 SUCCEEDED/UNKNOWN 等
    *_rows("ARCHIVING", ["COMPLETED", "ARCHIVE_PENDING", "SUCCEEDED", "FAILED", "UNKNOWN"]),
    *_rows("ARCHIVE_PENDING", ["ARCHIVING", "COMPLETED", "BLOCKED", "CANCELLED"]),
    # 已完成事务在新内容需要归档时回到待归档（变化进入下一快照，§21.4）
    *_rows("COMPLETED", ["ARCHIVE_PENDING"]),
    *_rows("BLOCKED", ["TRIAGED", "ANALYZING", "WAITING_FOR_USER", "READY_FOR_REVIEW", "ARCHIVING", "ARCHIVE_PENDING", "CANCELLED"]),
    *_rows("CANCELLED", ["ARCHIVING", "ARCHIVE_PENDING"]),
]

_TASK_TABLE: List[TransitionRule] = [
    *_rows("PENDING", ["READY", "WAITING_USER", "BLOCKED", "CANCELLED"]),
    *_rows("READY", ["RUNNING", "WAITING_USER", "RETRY_WAIT", "BLOCKED", "CANCELLED"]),
    *_rows("RUNNING", ["DONE", "FAILED", "WAITING_USER", "RETRY_WAIT", "BLOCKED"]),
    *_rows("WAITING_USER", ["READY", "RUNNING", "BLOCKED", "CANCELLED"]),
    *_rows("RETRY_WAIT", ["READY", "RUNNING", "FAILED", "BLOCKED", "CANCELLED"]),
    *_rows("DONE", []),
    *_rows("FAILED", ["READY", "BLOCKED", "CANCELLED"]),
    *_rows("BLOCKED", ["PENDING", "READY", "CANCELLED"]),
    *_rows("CANCELLED", []),
]

_APPROVAL_TABLE: List[TransitionRule] = [
    *_rows("ACTIVE", ["CONSUMED", "REVOKED", "EXPIRED", "INVALIDATED"]),
    *_rows("CONSUMED", []),
    *_rows("REVOKED", []),
    *_rows("EXPIRED", []),
    *_rows("INVALIDATED", []),
]

#: 外部动作状态转换（SPEC §21.2 状态表）
_ACTION_TABLE: List[TransitionRule] = [    # 授权只能由用户或系统给出：Agent 不得自我授权外呼
    *_rows("PREPARED", ["AUTHORIZED"], triggers=(TRIGGER_USER, TRIGGER_SYSTEM)),
    *_rows("PREPARED", ["CANCELLED"]),
    # 审批被撤销/失效时，AUTHORIZED 回到 PREPARED 等待重新批准
    *_rows("AUTHORIZED", ["IN_FLIGHT", "PREPARED", "CANCELLED"]),
    # IN_FLIGHT 是"可能已触达外部"的状态：只能走向确定结论
    *_rows("IN_FLIGHT", ["SUCCEEDED", "FAILED_SAFE", "UNKNOWN"]),
    # UNKNOWN 只能由人工核实结论推进，且不得伪装成安全重试
    *_rows("UNKNOWN", ["SUCCEEDED", "FAILED_SAFE", "CANCELLED"], triggers=(TRIGGER_USER, TRIGGER_SYSTEM), note="需人工核实或用户裁决"),
    *_rows("FAILED_SAFE", ["AUTHORIZED", "PREPARED"], triggers=(TRIGGER_USER, TRIGGER_SYSTEM), note="重新批准后可再次准备"),
    *_rows("FAILED_SAFE", ["CANCELLED"]),
    *_rows("SUCCEEDED", []),
    *_rows("CANCELLED", []),
]

_ARCHIVE_TABLE: List[TransitionRule] = [    *_rows("PENDING", ["UPLOADING", "FAILED"]),
    *_rows("UPLOADING", ["VERIFYING", "PARTIAL", "PENDING", "FAILED"]),
    *_rows("PARTIAL", ["UPLOADING", "PENDING", "FAILED"]),
    *_rows("VERIFYING", ["VERIFIED", "PENDING_VERIFICATION", "PARTIAL", "FAILED"]),
    *_rows("PENDING_VERIFICATION", ["VERIFYING", "VERIFIED", "FAILED"]),
    *_rows("VERIFIED", []),
    *_rows("FAILED", ["PENDING", "UPLOADING"]),
]

_RULE_TABLE: List[TransitionRule] = [
    *_rows("CANDIDATE", ["PENDING_CONFIRMATION", "REVOKED"]),
    *_rows("PENDING_CONFIRMATION", ["ENABLED", "CANDIDATE", "REVOKED"]),
    *_rows("ENABLED", ["SUPERSEDED", "REVOKED"]),
    # 可恢复：废止/被替代都能回滚（§10.3、§21.5）
    *_rows("SUPERSEDED", ["ENABLED", "REVOKED"]),
    *_rows("REVOKED", ["ENABLED"]),
]

_QUESTION_TABLE: List[TransitionRule] = [
    *_rows("OPEN", ["ANSWERED", "CLOSED", "EXPIRED"]),
    *_rows("ANSWERED", ["CLOSED", "OPEN"]),
    *_rows("CLOSED", []),
    *_rows("EXPIRED", ["OPEN", "CLOSED"]),
]

_DRAFT_TABLE: List[TransitionRule] = [
    *_rows("OPEN", ["LOCKED", "STALE", "REVOKED"]),
    *_rows("LOCKED", ["OPEN", "STALE", "REVOKED"]),
    *_rows("STALE", ["OPEN", "REVOKED"]),
    *_rows("REVOKED", []),
]

_INDEX_TABLE: List[TransitionRule] = [
    *_rows("PENDING", ["INDEXED", "STALE", "FAILED"]),
    *_rows("INDEXED", ["STALE", "PENDING"]),
    *_rows("STALE", ["INDEXED", "FAILED", "REBUILD_REQUIRED"]),
    *_rows("FAILED", ["PENDING", "INDEXED", "REBUILD_REQUIRED"]),
    *_rows("REBUILD_REQUIRED", ["PENDING", "INDEXED", "FAILED"]),
]

_RESTORE_TABLE: List[TransitionRule] = [
    *_rows("PENDING", ["RUNNING", "FAILED"]),
    *_rows("RUNNING", ["VERIFIED", "CONFLICT", "FAILED"]),
    *_rows("CONFLICT", ["RUNNING", "VERIFIED", "FAILED"]),
    *_rows("FAILED", ["PENDING"]),
    *_rows("VERIFIED", []),
]

_EXECUTION_TABLE: List[TransitionRule] = [
    *_rows("RUNNING", ["SUCCEEDED", "FAILED", "TIMEOUT"]),
    *_rows("SUCCEEDED", []),
    *_rows("FAILED", []),
    *_rows("TIMEOUT", []),
]


#: 单次尝试的生命周期：从 IN_FLIGHT 走向一个确定结论
_ATTEMPT_TABLE: List[TransitionRule] = [
    *_rows("IN_FLIGHT", ["SUCCEEDED", "FAILED_SAFE", "UNKNOWN"]),
    *_rows("SUCCEEDED", []),
    *_rows("FAILED_SAFE", []),
    *_rows("UNKNOWN", []),
]


class StateMachine:
    """一台状态机：合法转换表 + 查询 + 强制校验。"""

    def __init__(self, name: str, rules: Sequence[TransitionRule], *, initial: Optional[str] = None) -> None:
        self.name = name
        self._rules: Dict[Tuple[str, str], TransitionRule] = {(r.source, r.target): r for r in rules}
        self._by_source: Dict[str, List[TransitionRule]] = {}
        for rule in rules:
            self._by_source.setdefault(rule.source, []).append(rule)
        self._wildcards: List[TransitionRule] = [r for r in rules if r.source == ANY]
        self.initial = initial
        self.states: List[str] = sorted({r.source for r in rules} | {r.target for r in rules if r.target != ANY})

    def rule(self, current: str, target: str) -> Optional[TransitionRule]:
        return self._rules.get((current, target)) or next(
            (r for r in self._wildcards if r.target == target), None
        )

    def can(self, current: str, target: str) -> bool:
        return self.rule(current, target) is not None

    def next_states(self, current: str) -> List[str]:
        return sorted(r.target for r in self._by_source.get(current, []) if r.target != ANY)

    def is_terminal(self, state: str) -> bool:
        return not self.next_states(state)

    def require(
        self,
        current: str,
        target: str,
        *,
        trigger: Optional[str] = None,
        object_type: str = "",
        object_id: str = "",
    ) -> TransitionRule:
        """校验转换合法性；不合法则抛 IllegalTransition（含原因）。"""
        rule = self.rule(current, target)
        if rule is None:
            raise IllegalTransition(
                f"{object_type or self.name} 不允许从 {current} 转换到 {target}",
                detail={
                    "machine": self.name,
                    "object_type": object_type,
                    "object_id": object_id,
                    "from": current,
                    "to": target,
                    "allowed": self.next_states(current),
                },
            )
        if not rule.allows(trigger):
            raise IllegalTransition(
                f"触发者 {trigger} 无权执行 {current} → {target}",
                detail={"machine": self.name, "from": current, "to": target, "allowed_triggers": list(rule.triggers)},
            )
        return rule

    def table(self) -> List[Dict[str, object]]:
        """导出转换表（文档与契约快照用，保证文档由代码生成、不会漂移）。"""
        rows = []
        for rule in sorted(self._rules.values(), key=lambda r: (r.source, r.target)):
            rows.append(
                {
                    "from": rule.source,
                    "to": rule.target,
                    "triggers": list(rule.triggers),
                    "requires_no_irreversible": rule.requires_no_irreversible,
                    "note": rule.note,
                }
            )
        return rows


MACHINES: Dict[str, StateMachine] = {
    "Case": StateMachine("Case", _CASE_TABLE, initial="NEW"),
    "Task": StateMachine("Task", _TASK_TABLE, initial="PENDING"),
    "Approval": StateMachine("Approval", _APPROVAL_TABLE, initial="ACTIVE"),
    "ExternalAction": StateMachine("ExternalAction", _ACTION_TABLE, initial="PREPARED"),
    "ActionAttempt": StateMachine("ActionAttempt", _ATTEMPT_TABLE, initial="IN_FLIGHT"),
    "ArchiveJob": StateMachine("ArchiveJob", _ARCHIVE_TABLE, initial="PENDING"),
    "Rule": StateMachine("Rule", _RULE_TABLE, initial="CANDIDATE"),
    "Memory": StateMachine("Memory", _RULE_TABLE, initial="CANDIDATE"),
    "Workflow": StateMachine("Workflow", _RULE_TABLE, initial="CANDIDATE"),
    "WorkflowVersion": StateMachine("WorkflowVersion", _RULE_TABLE, initial="CANDIDATE"),
    "Question": StateMachine("Question", _QUESTION_TABLE, initial="OPEN"),
    "Draft": StateMachine("Draft", _DRAFT_TABLE, initial="OPEN"),
    "IndexState": StateMachine("IndexState", _INDEX_TABLE, initial="PENDING"),
    "RestoreJob": StateMachine("RestoreJob", _RESTORE_TABLE, initial="PENDING"),
    "Execution": StateMachine("Execution", _EXECUTION_TABLE, initial="RUNNING"),
}

#: 对外副作用不可逆的状态：处于这些状态的动作会使 Case 不可再"取消/阻塞"
IRREVERSIBLE_ACTION_STATES = ("IN_FLIGHT", "SUCCEEDED", "UNKNOWN")


def machine_for(object_type: str) -> StateMachine:
    try:
        return MACHINES[object_type]
    except KeyError as exc:
        raise IllegalTransition(f"{object_type} 没有登记状态机（需要先提案登记）") from exc


def require_transition(
    object_type: str,
    current: str,
    target: str,
    *,
    trigger: Optional[str] = None,
    object_id: str = "",
) -> TransitionRule:
    return machine_for(object_type).require(
        current, target, trigger=trigger, object_type=object_type, object_id=object_id
    )


def snapshot() -> Dict[str, object]:
    """状态机快照：契约一致性校验与文档生成依据。"""
    return {
        name: {"initial": machine.initial, "states": machine.states, "transitions": machine.table()}
        for name, machine in sorted(MACHINES.items())
    }
