"""MVP-3 理解、追问与草稿（SPEC §5.5、§6、§7.4、§8.1、§8.2、§16 MVP-3）。

这一层把「一封邮件 / 一份资料」变成**可追溯的理解结果**：

```text
原文（SourceDocument，绝不覆盖）
  → extract.py       确定性规则抽取（中文优先：时间语义、待办、材料、要求）
  → understander.py  Understander 协议 + RuleBasedUnderstander（默认，零模型调用）
                     + ModelBackedUnderstander（可选，必须显式配置预算）
  → model.py         UnderstandingResult：带引用的时间字段/事实/推断/追问/草稿候选
  → case_flow.py     落到 Case/Task/Fact/Inference/Question/Draft/DraftVersion（幂等）
  → drafts.py        草稿版本、任意两版差异、恢复旧版本（恢复=再产生新版本）
```

三条不可让渡的约束
==================

1. **引用守卫**：`Fact` 必须带 `Citation`（path/locator/source_version/content_hash/supports）；
   给不出引用的内容只能落成 `Inference` 或 `Question`；
2. **时间语义不丢**：`报名截止时间` 与 `活动开始时间` 是两个不同的语义槽位，
   歧义或冲突一律转成 `Question`，**不猜**；
3. **默认离线**：默认路径不触碰任何模型；模型只是可选适配器，且必须显式配置预算
   （SPEC §12 不锁定技术栈、§1064 未设预算禁止无人值守无限调用）。

典型用法：

```python
from asistent.contracts import ReferenceStore, SourceDocument
from asistent.understand import process_document, load_source_text

store = ReferenceStore()
doc = store.put(SourceDocument(source_type="email", origin_path="fixtures/.../01-notice.eml"))
report = process_document(store, doc, eml_bytes=open("01-notice.eml", "rb").read())
print(report.summary_line())
```
"""

from __future__ import annotations

from .case_flow import (
    CASE_GOAL_PREFIX,
    CaseFlowReport,
    EntryOutcome,
    load_source_text,
    process_document,
)
from .drafts import (
    ApprovalBinding,
    VersionDiff,
    approval_binding,
    create_draft,
    current_version,
    derive_draft_id,
    derive_version_id,
    diff_versions,
    new_version,
    restore_version,
    version_by_number,
    versions_of,
)
from .extract import (
    DECLARATIVE_TIME_RULES,
    LABEL_RULES,
    MATERIAL_MARKERS,
    NO_REPLY_MARKERS,
    REQUIREMENT_MARKERS,
    RULESET_VERSION,
    TIME_EXPRESSION_RE,
    ExtractionResult,
    LabelRule,
    TimeExpression,
    decode_eml_bytes,
    decode_header_value,
    email_message_to_source_text,
    extract,
    extract_time_expressions,
    label_for,
    parse_expression,
    parse_source_text,
    reply_requested,
    resolve_weekday,
    safe_text,
    timezone_from_date_header,
)
from .model import (
    ActionSuggestion,
    CitationRef,
    Conflict,
    DraftCandidate,
    MaterialRequirement,
    QuestionCandidate,
    SourceText,
    TimeField,
    TodoItem,
    UnderstandingResult,
    hash_text,
    merge_time_fields,
)
from .understander import (
    AnalysisInput,
    BudgetExceeded,
    ModelBackedUnderstander,
    ModelBackend,
    ModelBudget,
    ModelCallLog,
    ModelCallResult,
    ModelDisabled,
    RuleBasedUnderstander,
    Understander,
    UnderstandError,
    budget_from_mapping,
    cross_check,
)

__all__ = [
    # 协议与实现
    "Understander",
    "RuleBasedUnderstander",
    "ModelBackedUnderstander",
    "ModelBackend",
    "ModelBudget",
    "ModelCallLog",
    "ModelCallResult",
    "AnalysisInput",
    "cross_check",
    "budget_from_mapping",
    # 错误
    "UnderstandError",
    "BudgetExceeded",
    "ModelDisabled",
    # 数据结构
    "UnderstandingResult",
    "TimeField",
    "TodoItem",
    "MaterialRequirement",
    "ActionSuggestion",
    "DraftCandidate",
    "QuestionCandidate",
    "Conflict",
    "SourceText",
    "CitationRef",
    "merge_time_fields",
    "hash_text",
    # 抽取
    "extract",
    "ExtractionResult",
    "TimeExpression",
    "LabelRule",
    "LABEL_RULES",
    "DECLARATIVE_TIME_RULES",
    "TIME_EXPRESSION_RE",
    "REQUIREMENT_MARKERS",
    "MATERIAL_MARKERS",
    "NO_REPLY_MARKERS",
    "RULESET_VERSION",
    "parse_source_text",
    "parse_expression",
    "extract_time_expressions",
    "label_for",
    "resolve_weekday",
    "timezone_from_date_header",
    "decode_eml_bytes",
    "email_message_to_source_text",
    "decode_header_value",
    "reply_requested",
    "safe_text",
    # 落地
    "process_document",
    "load_source_text",
    "CaseFlowReport",
    "EntryOutcome",
    "CASE_GOAL_PREFIX",
    # 草稿
    "create_draft",
    "new_version",
    "diff_versions",
    "restore_version",
    "approval_binding",
    "ApprovalBinding",
    "VersionDiff",
    "current_version",
    "version_by_number",
    "versions_of",
    "derive_draft_id",
    "derive_version_id",
]
