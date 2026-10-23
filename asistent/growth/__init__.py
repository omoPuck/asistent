"""MVP-6 成长资产（SPEC §855-863）：规则 / 流程版本 / 记忆有效期，以及它们与理解的**真实串联**。

```text
scope.py     适用范围 + 效果的机器可读说明（`Rule.scope_ref` 里的 v1 说明串）
rules.py     候选 → 确认 → 生效 → 替代 → 废止 → 回滚；命中审计（带规则版本号）
bridge.py    包住 MVP-3 的理解器：已生效规则真的参与理解；冲突 → 追问 + 阻塞（契约 §14.5）
workflow.py  流程抽象（八件事齐全）→ 版本；启动钉版本、在途用旧版本、演进出新版本
memory.py    长期资产的有效期：过期不再作为事实使用，但历史保留可查
```

四条不可让渡的约束（都被测试与原始输出钉住）
============================================

1. **未确认不得生效**：规则/记忆/流程都只能经 `CANDIDATE → PENDING_CONFIRMATION → ENABLED`
   走到生效（直接写 `ENABLED` 会被契约状态机拒绝）。
2. **命中可证**：每次命中都落 `AuditEvent(action="RULE_HIT")`，其中带 `rule_id` **和 `rule_version`**；
   受影响的事实通过 `Fact.links["rule_version"]`、事务通过 `Case.rule_ids` 反查——
   "Agent 自称遵守了规则"不算证据。
3. **废止不清史、回滚出新版**：`revoke_rule` / `rollback_rule` / `publish_workflow_version`
   只新增对象与审计；旧 Case 的引用、旧 Fact、旧审计一律原样。
4. **持久化优先**：规则、版本、有效期窗口、命中记录全部存在 `Store`（内存或 SQLite）里，
   没有任何"进程内字典/缓存"承载语义；换后端或重启后结论一致。

典型用法：

```python
from asistent.contracts import SqliteStore
from asistent.growth import Correction, RuleScope, EFFECT_REQUIRE_QUESTION, process_with_growth

store = SqliteStore("var/asistent.db")
rule = propose_rule(store, Correction(statement="...", user_message="...", correction_of="...",
                                     scope=RuleScope(effect=EFFECT_REQUIRE_QUESTION,
                                                     slots=("registration_deadline", "event_start"))))
confirm_rule(store, rule.id)
report, growth = process_with_growth(store, document, eml_bytes=raw)
print(report.summary_line(), growth.summary_line(), growth.hit_lines())
```

详细设计与原始证据见 `docs/mvp/mvp-6.md`。
"""

from __future__ import annotations

from .bridge import (
    RULE_CONFLICT_PREFIX,
    GrowthDecision,
    GrowthReport,
    GrowthUnderstander,
    apply_growth,
    case_is_blocked,
    ensure_blocked_reason,
    process_with_growth,
    require_sendable,
    rule_conflict_reasons,
    rule_conflict_questions,
)
from .memory import (
    MEMORY_VALIDITY_KIND,
    MemoryFactResult,
    all_memories,
    confirm_memory,
    fact_from_memory,
    facts_from_memories,
    grant_validity,
    is_expired,
    is_usable,
    memory_citation,
    memory_id_for,
    memory_valid_until,
    remember,
    revoke_memory,
    sweep_expired_memories,
    usable_memories,
    usable_statements,
    validity_task,
)
from .rules import (
    AUDIT_HIT,
    AUDIT_PROPOSE,
    AUDIT_CONFIRM,
    AUDIT_ROLLBACK,
    AUDIT_REVOKE,
    AUDIT_SUPERSEDE,
    FACT_RULE_LINK,
    Correction,
    RuleHit,
    confirm_rule,
    current_version,
    effective_rules,
    enable_new_version,
    enabled_rules,
    facts_for_rule,
    get_rule,
    link_case_rule,
    link_facts_to_rule,
    propose_rule,
    record_rule_hits,
    replace_rule,
    revoke_rule,
    rollback_rule,
    rule_hits,
    rule_versions,
    supersede_rule,
    versions_by_logical_key,
)
from .scope import (
    EFFECT_ALLOW_ALIAS,
    EFFECT_REQUIRE_QUESTION,
    EFFECTS,
    SCOPE_KINDS,
    MatchInput,
    RuleScope,
    describe_effects,
    parse_scope,
    slot_label,
)
from .workflow import (
    CASE_WORKFLOW_LINK,
    REQUIRED_PARTS,
    WorkflowSpec,
    case_workflow_steps,
    case_workflow_version,
    confirm_workflow,
    create_workflow,
    diff_versions,
    effective_workflow_version,
    pin_case_workflow,
    publish_workflow_version,
    repin_case_workflow,
    rollback_workflow,
    spec_from_version,
    workflow_versions,
)

__all__ = [
    # 适用范围与效果
    "RuleScope",
    "MatchInput",
    "EFFECTS",
    "EFFECT_ALLOW_ALIAS",
    "EFFECT_REQUIRE_QUESTION",
    "SCOPE_KINDS",
    "parse_scope",
    "describe_effects",
    "slot_label",
    # 规则
    "Correction",
    "RuleHit",
    "propose_rule",
    "confirm_rule",
    "enable_new_version",
    "supersede_rule",
    "replace_rule",
    "revoke_rule",
    "rollback_rule",
    "rule_versions",
    "versions_by_logical_key",
    "get_rule",
    "current_version",
    "effective_rules",
    "enabled_rules",
    "rule_hits",
    "facts_for_rule",
    "record_rule_hits",
    "link_case_rule",
    "link_facts_to_rule",
    "AUDIT_HIT",
    "AUDIT_PROPOSE",
    "AUDIT_CONFIRM",
    "AUDIT_REVOKE",
    "AUDIT_SUPERSEDE",
    "AUDIT_ROLLBACK",
    "FACT_RULE_LINK",
    # 与理解串联
    "GrowthUnderstander",
    "GrowthReport",
    "GrowthDecision",
    "process_with_growth",
    "apply_growth",
    "ensure_blocked_reason",
    "case_is_blocked",
    "require_sendable",
    "rule_conflict_reasons",
    "rule_conflict_questions",
    "RULE_CONFLICT_PREFIX",
    # 流程版本
    "WorkflowSpec",
    "REQUIRED_PARTS",
    "CASE_WORKFLOW_LINK",
    "create_workflow",
    "confirm_workflow",
    "publish_workflow_version",
    "rollback_workflow",
    "pin_case_workflow",
    "repin_case_workflow",
    "case_workflow_version",
    "case_workflow_steps",
    "effective_workflow_version",
    "workflow_versions",
    "spec_from_version",
    "diff_versions",
    # 记忆有效期
    "MEMORY_VALIDITY_KIND",
    "MemoryFactResult",
    "remember",
    "confirm_memory",
    "grant_validity",
    "validity_task",
    "memory_valid_until",
    "is_expired",
    "is_usable",
    "usable_memories",
    "usable_statements",
    "sweep_expired_memories",
    "revoke_memory",
    "facts_from_memories",
    "fact_from_memory",
    "memory_citation",
    "all_memories",
    "memory_id_for",
]
