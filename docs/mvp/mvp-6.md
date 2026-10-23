# MVP-6：成长资产（`asistent/growth/**`）

- 分支：`mvp/6-growth`（worktree：`D:\dsh\个人助手\.worktrees\mvp-6-growth`）
- 起点：`16c6315`（contracts **v0.6** 冻结点；本分支**没有提交**，工作区里只有两个新目录）
- 交付物：`asistent/growth/{__init__,scope,rules,bridge,workflow,memory}.py`、`tests/growth/**`、本文档
- 结论：**规则是数据，不是提示词；命中的是哪一版，必须能在审计里查到。**
  - 本地（Windows + `python 3.12`，双后端）：`528/528` 全绿
    （reference `OK (skipped=14)`、sqlite `OK (skipped=5)`；跳过项=「需要持久化后端」的跨进程/重启用例
    + 平台相关用例，理由字符串见 §5.2）；
  - 设备（`ssh board`，`/usr/bin/python3` = **3.10.12**，双后端 + `bash scripts/test.sh`）：`528/528` 全绿（§5.3）；
    这一轮打包的树包含本文档（`docs/mvp/mvp-6.md` 已随包上传到设备）。
  - **未确认不得生效**由契约状态机硬约束（`CANDIDATE → ENABLED` 直接抛 `IllegalTransition`，§5.4）；
  - **命中可证**：每次命中落 `AuditEvent(action="RULE_HIT")`，`detail` 里带 `rule_id`/`rule_version`/
    `effect`/`slots`/`applied`；被规则影响的事实登记 `Fact.links["rule_version"]`（§5.5）；
  - **冲突不猜**：同一槽位被两条生效规则给出不同效果时，两个效果**都不应用**，落 `Question` +
    `Case.blocked_reason`，新的可发送结论为 0，外发通道调用次数增量为 0（§5.6）；
  - **废止/回滚不动历史**：废止后新事务不命中，旧 Case 的引用与审计逐字节不变；回滚产生**新版本**
    且不删对象、不改旧版本内容（§5.7）；
  - **流程版本在途不换**：`Case.links["workflow_version"]` 是唯一真源，在途 Case 继续用旧版本、
    新 Case 用新版本，两侧在**同一条测试**里同时钉住（§5.8）；
  - **记忆过期不再当事实**，但对象与历史事实原样保留、仍可查（§5.9）；
  - **持久化优先**：丢掉实例后只拿库路径新建连接，冷启动处理新邮件的结论与历史完全一致（§5.10）；
  - `asistent/contracts/**`、`tests/contracts/**` **一行未改**（§5.11）；纯标准库、零第三方依赖。

---

## 1. 目标与验收对照

### 1.1 SPEC §857-863（MVP-6 的五条 Given/When/Then）

| 验收项 | 状态 | 证据 |
| --- | --- | --- |
| §859 Given 用户明确纠正，When 用户确认保存，Then 生成**带来源和适用范围**的规则版本 | ✅ 候选阶段 0 命中；确认后 0 命中变 1 命中；来源=用户原话+原始邮件 ID+原事务 ID，范围=机器可判的 `scope_ref`；同一次纠正重复提交幂等；不认识的规则定义一律拒绝（9 条） | §5.4 |
| §860 Given 新通知命中该规则，When Agent 分析，Then **记录命中的规则版本**并遵守规则 | ✅ 5 项审计用例 + 1 项「按规则认定」用例：审计带 `rule_version`、事实带 `Fact.links["rule_version"]`、事务带 `Case.rule_ids`；不适用则不算命中；重复投递不重复建对象但审计如实追加 | §5.5 |
| §861 Given 规则冲突，When Agent 分析，Then 暂停相关自动动作并向用户展示冲突 | ✅ 3 项：两个效果都不应用、`Question` 写明是谁和谁冲突、`Case.blocked_reason` 可读、`require_sendable` 拒绝、旧草稿转 `STALE`、`Approval`/`ExternalAction` 增量为 0、通道调用次数不变；裁决后恢复 | §5.6 |
| §862 Given 用户废止规则，When 后续事务处理，Then **不再命中废止版本，历史事务仍保留原版本** | ✅ 4 项：废止前后命中对照；旧 Case 引用/事实/审计逐字节不变；回滚=`新版本`且不删对象；跨重启仍然成立 | §5.7 |
| §863 Given 用户认可一次成功流程，When 生成 WorkflowVersion，Then 记录**步骤、追问点、审批点、归档要求和适用范围** | ✅ 5 项：八件事缺一即拒；版本对象逐字段可查；在途旧版本/新事务新版本同一条测试两侧钉住；改绑定必须显式留痕；回滚产生新版本 | §5.8 |

### 1.2 本次任务额外点名的八条语义

| # | 语义要求 | 测试 | 证据 |
| --- | --- | --- | --- |
| 1 | 纠正 → 规则候选（来源：哪次纠正/哪封原始邮件/谁确认 + 适用范围）→ 确认版本；确认前不生效 | `test_rules_lifecycle.py`、`test_understand_chain.py` | §5.4 |
| 2 | 命中必须可证：审计里能看到命中的规则版本；Case/Fact 能追「来自哪一版」 | `test_rule_hits_audit.py` | §5.5 |
| 3 | 规则冲突 → 暂停自动动作（外发 0 次）+ `Question` + `Case.blocked_reason`（§14.5） | `test_rule_conflict.py` | §5.6 |
| 4 | 废止不擦历史：后续不命中，历史 Case 的引用/审计保持原样（不重指向） | `test_rules_lifecycle.py::test_revoke_...`、`test_persistence_first.py` | §5.7 |
| 5 | 在途事务用启动时钉住的 `WorkflowVersion`，新 Case 用新版本；两侧同一条测试同时钉住 | `test_workflow_versions.py::test_in_flight_...` | §5.8 |
| 6 | 回滚产生**新版本**，不删历史、不改旧记录 | `test_rules_lifecycle.py::test_rollback_...`、`test_workflow_versions.py::test_workflow_rollback_...` | §5.7、§5.8 |
| 7 | `Memory` 过期后不再作为事实（进不了 `Fact`/结论），但历史保留、可查 | `test_memory_validity.py` | §5.9 |
| 8 | 持久化优先：命中、生效、废止、回滚全部走存储对象 + 审计，不靠进程内字典/缓存 | `test_persistence_first.py`、各模块 `@requires_persistence` 用例 | §5.10 |

### 1.3 三条硬验收

| 验收项 | 状态 | 证据 |
| --- | --- | --- |
| 与 `asistent.understand` **真实串联**（不是单测里造的假链路） | ✅ `GrowthUnderstander.base` 是真实 `asistent.understand.understander.RuleBasedUnderstander`；无规则时两边事实槽位逐条一致；纠正→命中→改版→再命中全程走真实流水线 | §5.9 |
| 双后端 + Python 3.10（设备）全绿 | ✅ 本地 `528/528` ×2、设备 `528/528` ×2 + `scripts/test.sh` | §5.1、§5.3 |
| `asistent/contracts/**`、`tests/contracts/**` 零改动 | ✅ 与基线 `16c6315` 差异为空、无提交、工作区只有两个新目录（对 `main` 的差异来源已说明，见 §5.11） | §5.11 |

---

## 2. 交付物

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `asistent/growth/scope.py` | 293 | 适用范围与效果的**机器可判**编码（`v=1;kind=...;effect=...;slots=A>B`），解析/格式化/匹配/校验；不认识的东西一律 `ValidationError`，绝不静默忽略 |
| `asistent/growth/rules.py` | 767 | 规则版本生命周期：候选 → 确认 → 生效 → 替代/废止/回滚；命中记录与读回；事实/事务与规则版本的关联；审计动作常量 |
| `asistent/growth/bridge.py` | 574 | 与理解链路的桥：`GrowthUnderstander`（包住真实理解器）→ 冲突检测 → 应用效果 → 落审计/关联/阻塞；`process_with_growth()` 入口；`require_sendable()` 门闸 |
| `asistent/growth/workflow.py` | 622 | 流程抽象（八件事）与版本：创建/确认/发布/回滚；`Case.links["workflow_version"]` 的在途钉定与显式改绑 |
| `asistent/growth/memory.py` | 569 | 长期记忆：记住/确认/有效期（落在 `Task(kind="memory_validity")`）/过期判定/清扫/撤销/落成事实 |
| `asistent/growth/__init__.py` | 204 | 对外导出面（控制台与后续 MVP 只从这里取） |
| `tests/growth/support.py` | 190 | 测试基座：双后端、临时目录、夹具复用（`tests.contracts.helpers` / `tests.understand.helpers`）、证据 `dump()` |
| `tests/growth/test_rules_lifecycle.py` | 415 | 8 项：候选不生效、跳过确认被拒、幂等、非法定义被拒、范围匹配、废止、回滚、纠正换代 |
| `tests/growth/test_rule_hits_audit.py` | 203 | 5 项：审计带版本、不适用不算命中、两个事务各钉各版本、重复投递、重启后仍可查 |
| `tests/growth/test_rule_conflict.py` | 284 | 3 项：冲突暂停+阻塞+通道 0 增量、冲突只冻结分歧槽位、裁决后恢复 |
| `tests/growth/test_workflow_versions.py` | 247 | 5 项：在途/新事务两侧钉住、改绑留痕、八件事、回滚=新版本、跨重启 |
| `tests/growth/test_memory_validity.py` | 236 | 5 项：过期不再用+历史保留、未确认不算数、无期限长期可用、撤销可查、有效期跨重启 |
| `tests/growth/test_understand_chain.py` | 292 | 3 项：真实理解链路、纠正→版本→命中、按规则认定槽位 |
| `tests/growth/test_persistence_first.py` | 216 | 2 项：冷启动、废止/回滚跨重启（仅在 sqlite 后端运行） |

新增 **31** 项测试（基线 `Ran 497 tests` → `Ran 528 tests`）。

---

## 3. 不变量与设计

### 3.1 规则是数据，而且必须**机器可判**

一条规则由三部分构成：**适用范围**（谁/什么主题/哪个事务范围）、**效果**（命中后做什么）、
**来源**（哪次用户纠正、哪封原始邮件、哪个事务）。

契约的 `Rule` 只有 `statement`（自由文本）、`scope`、`scope_ref`。为了让"遵守规则"这件事**可判定**
而不是靠自然语言解释，MVP-6 把适用范围与效果编码进 `Rule.scope_ref`：

```
v=1;kind=contact;ref=notice@example.org;subject_re=;effect=slot_split.require_question;slots=registration_deadline>event_start
```

- `kind ∈ {global, contact, project, case_type}`；`ref` 是具体值（邮箱/项目/事务类型）；
- `subject_re` 是可选主题正则（大小写不敏感 `search`）；
- `effect ∈ {slot_split.require_question, slot_split.allow_alias}`，`slots=A>B` 恰好两个语义槽位
  （必须都在 `TemporalField.SEMANTIC_SLOTS` 里）；
- **版本号 `v=1` 是必填的**：不认识的 spec 版本、未知 `kind`、未知 `effect`、槽位不是恰好两个、
  槽位不在语义槽位表里……**全部抛 `ValidationError`**，绝不"看不懂就忽略"（§5.4 有 9 条被拒定义）。

`contact` 匹配会先把 `From` 拆成邮箱地址集合，再支持 `@domain` 后缀匹配，因此
`市青少年科技中心 <notice@example.org>` 这种带显示名的发件人也能命中 `contact:notice@example.org`。

### 3.2 命中可证：审计 + 事实 + 事务三处留痕

"记录命中的规则版本"不是日志，而是**可查询的对象**：

| 位置 | 内容 | 读法 |
| --- | --- | --- |
| `AuditEvent(action="RULE_HIT")` | `rule_id`、`rule_version`、`logical_key`、`effect`、`slots`、`applied`、`detail`（人话） | `rule_hits(store, document_id=.../case_id=...)` |
| `Fact.links["rule_version"]` | 被规则直接影响的每条事实所依据的规则版本 ID | `facts_for_rule(store, rule_id, case_id=...)` |
| `Case.rule_ids` | 本事务依据过哪些规则（去重） | `store.get("Case", case_id).rule_ids` |

审计事件挂在 `object_id=document_id`、`case_id=case_id` 上，所以"这一封邮件、这一个事务命中了哪一版规则"
可以直接查；重启后照样查得到（§5.5）。

### 3.3 版本与历史：只有"新增"，没有"改写"

- 每次纠正产生**新版本对象**（`rule_id_for(scope, version)` 由 `logical_key + version` 确定性派生）；
- 同一逻辑规则**只允许一个生效版本**：确认新版本时，同 `logical_key` 的其它生效版本自动转
  `SUPERSEDED` 并记录 `superseded_by`（这是"新事务命中新版本"能成立的前提）；
- 废止（`revoke_rule`）只改状态与 `revoked_at`，**不改内容、不删对象**；新事务不再命中，历史事务的
  引用与审计保持原样；
- 回滚（`rollback_rule`）**产生新版本**（version+1，`links["rollback_of"]` 指向目标版本），
  目标版本的历史状态**不复活**、内容不被改写。

### 3.4 冲突 = 数据分歧，不猜、不选

同一封邮件上，两条**当前都生效**的规则对**同一个目标槽位**给出**不同效果**，即冲突：

- 两个效果**都不应用**（不会"取优先级高的那个"）；
- 两条规则的命中都记进审计，`applied=false`（谁被卷进冲突是可查的）；
- 追加一个 `Conflict`（`semantic` = 分歧槽位，`values` = 效果集合，`sources` = 每条规则的 id/version/效果/范围/用户原话）；
- 追加 `QuestionCandidate(kind="conflict", affects=["时间字段:<槽位>"])`，问题正文写明"以哪一条为准"；
- 写 `Case.blocked_reason`（契约 §14.5）；
- `require_sendable(store, case_id)` 只要 `blocked_reason` 非空就抛 `PermissionDenied`。

裁决路径：废止其中一条规则（持久化）→ 新事务不再冲突、按留下的那一条执行（§5.6）。

### 3.5 流程版本钉在 `Case` 上，不读"当前版本"

`pin_case_workflow()` 把版本 ID 写进 `Case.links["workflow_version"]`，并在审计里留 `WORKFLOW_PIN`。
**它是唯一真源**：读在途流程永远读 Case 上的这个字段，而不是 `Workflow.current_version_id`。
因此流程发布新版本后：在途 Case 继续用旧版本，新 Case 拿到新版本；`pin_case_workflow` 对已钉定的
Case 是幂等的（不会悄悄换版本），要换必须走 `repin_case_workflow(..., reason=...)`（理由必填、留痕）。

### 3.6 记忆有效期落在一条 `Task` 上

契约的 `Memory` 既没有 `valid_until`，也没有 `EXPIRED` 状态。MVP-6 不新增字段（不许改契约），
而是把有效期表达为一条 `Task(kind="memory_validity", timeout_at=<到期时刻>, outputs={memory_id, valid_until, ttl_seconds})`：

- `is_expired(store, memory, now=...)` 是 `now` 的**纯函数**（读 `timeout_at` 比较，不依赖当前时间）；
- `usable_memories()` = 已生效 + 未过期 + 范围匹配；`facts_from_memories()` 只从可用记忆落事实；
- 过期后清扫（`sweep_expired_memories`）把记忆转 `REVOKED` 并结束任务，**不删对象、不改陈述**；
- 已经落过的事实**不动**（那是历史记录，带引用可查）；新事务拿不到过期记忆（跳过原因里写明"已过期（…）"）。

### 3.7 持久化优先

所有语义都落在对象上（`Rule`/`Workflow`/`WorkflowVersion`/`Memory`/`Task`/`Case.links`）与审计里，
**没有任何进程内字典/缓存**参与判断。验证方式不是"我说没有缓存"，而是**冷启动**：
丢掉实例 A、只拿库路径新建实例 B，处理一封**新**邮件，断言命中版本、在途钉定、记忆有效期、
历史审计与 A 完全一致（§5.10）。

---

## 4. 状态机（本模块涉及的转换）

`Rule` / `Memory` / `Workflow` / `WorkflowVersion` 都复用契约的资产状态机：

```
CANDIDATE ──→ PENDING_CONFIRMATION ──→ ENABLED ──→ SUPERSEDED ──→ ENABLED
    │                    │                  │            │
    └────────────────────┴──────────────────┴────────────┴──→ REVOKED
```

- `CANDIDATE → ENABLED` **不在**合法表里，直接抛 `IllegalTransition`：这就是"未确认不得生效"的
  实现方式（不是靠调用方自觉，而是契约硬约束）；
- `rule_id_for(scope, version)` 让"同一逻辑规则的 v1/v2/v3"成为三个对象，各自有自己的状态与历史。

---

## 5. 原始命令与原始输出

以下命令全部在 worktree 根目录执行。PowerShell 下先设
`$env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"`，再设 `$env:ASISTENT_STORE="reference"|"sqlite"`；
输出重定向到文件后再读（PowerShell 直接管道会弄坏非 ASCII 输出）。

本节的输出都是照抄的原始文本：`[evidence]` 单行摘要与 `Ran/OK` 行**逐字复制**；
多行块是测试里 `json.dumps(..., sort_keys=True, ensure_ascii=False, indent=2)` 的输出，
为了可读性可能**省略无关键（用 `...` 标出）并重排键的先后**，但**任何数字与字符串都没有改动**。
对象 ID（`rule_…`/`case_…`/`fact_…`/`wfv_…`）每次运行都会变，只在同一次引用的输出内部保持一致。

### 5.1 完整套件（本地，双后端）

```
PS> $env:ASISTENT_STORE="reference"
PS> & $PY -m unittest discover -s tests -t . > var\_final_reference.log 2>&1
PS> Get-Content var\_final_reference.log | Select-String '^Ran |^OK|^FAILED'
reference: Ran 528 tests in 16.977s
reference: OK (skipped=14)

PS> $env:ASISTENT_STORE="sqlite"
PS> & $PY -m unittest discover -s tests -t . > var\_final_sqlite.log 2>&1
PS> Get-Content var\_final_sqlite.log | Select-String '^Ran |^OK|^FAILED'
sqlite: Ran 528 tests in 31.043s
sqlite: OK (skipped=5)
```

### 5.2 逐模块（reference 后端；持久化用例在 sqlite 后端）

```
PS> & $PY -m unittest tests.growth.test_rules_lifecycle   → Ran 8 tests in 0.076s   OK
PS> & $PY -m unittest tests.growth.test_rule_hits_audit   → Ran 5 tests in 0.055s   OK (skipped=1)
PS> & $PY -m unittest tests.growth.test_rule_conflict     → Ran 3 tests in 0.061s   OK
PS> & $PY -m unittest tests.growth.test_workflow_versions → Ran 5 tests in 0.053s   OK (skipped=1)
PS> & $PY -m unittest tests.growth.test_memory_validity   → Ran 5 tests in 0.023s   OK (skipped=1)
PS> & $PY -m unittest tests.growth.test_understand_chain  → Ran 3 tests in 0.055s   OK
PS> $env:ASISTENT_STORE="sqlite"
PS> & $PY -m unittest tests.growth.test_persistence_first → Ran 2 tests in 0.309s   OK
PS> & $PY -m unittest tests.growth.test_memory_validity   → Ran 5 tests in 0.181s   OK
```

跳过的用例不是"跳过就算过"：`reference` 后端没有跨进程共享状态，相关用例用
`unittest.skipUnless` 显式跳过并写明原因——`跨进程共享状态需要持久化后端`、
`跨进程崩溃注入需要持久化后端`、`重启语义需要持久化后端`（与契约/MVP-5 同口径）；
sqlite 后端下这些用例**真的在跑**（§5.10）。

### 5.3 设备复验（RK3588 / Python 3.10.12）

```
PS> tar -cf "$env:TEMP\mvp6.tar" --exclude="*.log" --exclude="*.pyc" --exclude="__pycache__" --exclude="var" `
      asistent tests fixtures docs contracts scripts SPEC.md README.md .gitignore
PS> scp "$env:TEMP\mvp6.tar" board:~/asistent-spike/mvp6-check/full.tar
PS> ssh board "cd ~/asistent-spike/mvp6-check/run && tar -xf ../full.tar && ..."
=== reference ===
Ran 528 tests in 12.519s
OK (skipped=9)
=== sqlite ===
Ran 528 tests in 19.556s
OK
=== test.sh ===
契约快照一致（v0.6）
Ran 528 tests in 12.597s
OK (skipped=9)
=== docs present ===
docs/mvp/mvp-6.md
=== python ===
Python 3.10.12
```

只写自己创建的目录 `~/asistent-spike/mvp6-check/`，没有碰 `~/code/asistent`、`~/.dsh`、`~/miniforge3`。
`fixtures/` 与 `scripts/` 必须一起打包（公开夹具在仓库根目录，设备测试入口是仓库自带的 `scripts/test.sh`）。

### 5.4 §859：纠正 → 带来源与范围的规则版本（确认前不生效）

```
PS> & $PY -m unittest tests.growth.test_understand_chain -v
[evidence] 确认前 | case=case_ceab3c80ec91cce3f1a8a570 state=WAITING_FOR_USER doc=doc_03_before-confirm Fact+5 Inference+1 Question+2 Task+1 draftable=False duplicate=False | growth doc=doc_03_before-confirm case=case_ceab3c80ec91cce3f1a8a570 命中=0 应用=0 冲突=0
[evidence] 命中 v1 | case=case_93c1ae6bad05ca9ef2dfa154 state=WAITING_FOR_USER doc=doc_03_hit-v1 Fact+5 Inference+1 Question+3 Task+1 draftable=False duplicate=False | growth doc=doc_03_hit-v1 case=case_93c1ae6bad05ca9ef2dfa154 命中=1 应用=1 冲突=0
[evidence] 命中 v2 | case=case_9f19d4a53e645acf2fe87482 state=WAITING_FOR_USER doc=doc_03_hit-v2 Fact+5 Inference+1 Question+3 Task+1 draftable=False duplicate=False | growth doc=doc_03_hit-v2 case=case_9f19d4a53e645acf2fe87482 命中=1 应用=1 冲突=0

[evidence] §859 纠正 → 规则版本 → 下一次真实命中
{
  "before_confirm": { "case_id": "case_ceab3c80ec91cce3f1a8a570", "case_state": "WAITING_FOR_USER", "hits": [] },
  "candidate": {
    "id": "rule_653f8daa7a34748c82a2b7295105d40c",
    "state_at_propose": "CANDIDATE",
    "state_at_dump_time": "SUPERSEDED",
    "candidate_had_no_effect": true,
    "propose_audit_detail": {
      "correction_of": "把报名截止时间当成了活动开始时间",
      "effect": "slot_split.require_question",
      "rule_spec": "v=1;kind=contact;ref=notice@example.org;subject_re=;effect=slot_split.require_question;slots=registration_deadline>event_start",
      "rule_version": 1,
      "slots": ["registration_deadline", "event_start"],
      "source_case_id": "",
      "source_document_id": "doc_02a",
      "user_message": "报名截止时间不是活动开始时间，别把 10 月 20 日当成开营时间"
    }
  },
  "confirmed_v1": {
    "id": "rule_653f8daa7a34748c82a2b7295105d40c",
    "state": "SUPERSEDED",
    "confirmed_by": "user",
    "confirmed_at": "2026-10-10T08:37:06.875Z",
    "effective_from": "2026-10-10T08:37:06.875Z",
    "audit_actions": ["PROPOSE_RULE", "TRANSITION", "CONFIRM_RULE", "TRANSITION"]
  },
  ...
}
```

说明（避免误读）：

- 第一段 `命中=0` 是**候选阶段**：规则还没确认，理解链路照跑，只是没有命中；
- `state_at_propose=CANDIDATE` 是提出时的状态（`candidate` 是当时的对象快照）；
  `state_at_dump_time=SUPERSEDED` 是**打完 v2 之后**再看同一个对象的当前状态——v1 已被 v2 版取代，
  但 v1 对象的**内容与它当时的命中记录**都没被改写（§5.5、§5.7）；
- 规则来源四要素来自用户原话（`user_message`/`correction_of`）、原始邮件（`source_document_id`）、
  适用范围（`rule_spec`）与确认者（`confirmed_by=user` + `effective_from`）；
- 确认前的 `0 命中` 由契约状态机保证：直接 `CANDIDATE → ENABLED` 会抛 `IllegalTransition`（§5.4 尾段）。

### 5.5 §860：命中必须可证

```
PS> & $PY -m unittest tests.growth.test_rule_hits_audit -v
[evidence] 命中一次 | case=case_76f50e575b8272de22ace4d1 state=WAITING_FOR_USER doc=doc_02a_hit Fact+2 Inference+1 Question+1 Task+1 draftable=False duplicate=False | growth doc=doc_02a_hit case=case_76f50e575b8272de22ace4d1 命中=1 应用=1 冲突=0

[evidence] §860 审计里的命中记录
{
  "audit_events": [
    {
      "action": "RULE_HIT",
      "case_id": "case_76f50e575b8272de22ace4d1",
      "object_id": "doc_02a_hit",
      "id": "audit_00002720261010T08370",
      "detail": {
        "applied": true,
        "detail": "缺「活动开始时间」→ 按规则追问（不得用「报名截止时间」顶替）",
        "document_id": "doc_02a_hit",
        "effect": "slot_split.require_question",
        "logical_key": "contact|notice@example.org||slot_split.require_question|registration_deadline>event_start",
        "rule_id": "rule_653f8daa7a34748c82a2b7295105d40c",
        "rule_version": 1,
        "slots": ["registration_deadline", "event_start"]
      }
    }
  ],
  "case_rule_ids": ["rule_653f8daa7a34748c82a2b7295105d40c"],
  "linked_facts": [
    {
      "id": "fact_6e5878269df3ef3c579da2ed",
      "statement": "报名截止时间：10月20日 17:00（原文标签：报名截止时间）",
      "citation_locator": "line:14-14",
      "rule_versions": ["rule_653f8daa7a34748c82a2b7295105d40c"]
    }
  ],
  "rule_hits_readback": [ ... 同 audit_events[0].detail ... ]
}
```

审计是**读得回来**的（`rule_hits()` 直接读审计，不是另存一份）：同一个版本 ID 同时出现在
审计事件、`Case.rule_ids`、`Fact.links["rule_version"]` 三处。另外两条用例：
`不适用就不算命中`（01 那封同时有报名截止与活动开始 → 0 命中、`Case.rule_ids` 为空、仍 `READY_FOR_REVIEW`）、
`重复处理同一封邮件`（对象数不增而审计如实追加第 2 条 `RULE_HIT`）。

### 5.6 §861：冲突 → 暂停 + 展示冲突

```
PS> & $PY -m unittest tests.growth.test_rule_conflict -v
[evidence] 冲突前（无规则） | case=case_6fdbd17dbcb58ecf263d665c ... Draft+1 DraftVersion+1 Fact+2 ... 命中=0 应用=0 冲突=0
[evidence] 对照（无规则冲突） | case=case_14992b286a3392c807ff395e ... Draft+1 DraftVersion+1 Fact+2 ... 命中=0 应用=0 冲突=0
[evidence] 冲突（两条规则同时命中） | case=case_6fdbd17dbcb58ecf263d665c state=WAITING_FOR_USER doc=doc_03_conflict Draft+0 Fact+5 Inference+1 Question+3 Task+1 draftable=False duplicate=False | growth doc=doc_03_conflict case=case_6fdbd17dbcb58ecf263d665c 命中=2 应用=0 冲突=1

[evidence] §861 规则冲突 → 暂停 + 追问 + 阻塞
{
  "conflict": {
    "semantic": "event_start",
    "effects": ["slot_split.allow_alias", "slot_split.require_question"],
    "rule_ids": ["rule_653f8daa7a34748c82a2b7295105d40c", "rule_eb877d1aba3aa43076bbbc6c6ed53ee8"],
    "rule_versions": [1, 1],
    "detail": "规则冲突：槽位「活动开始时间」被两条已生效规则给出不同结论（slot_split.allow_alias（允许把来源槽位按规则当作目标槽位） / slot_split.require_question（缺失时必须追问，不得用另一个槽位顶替））。未裁决前两个效果都不应用，也不产出可发送结论。"
  },
  "case_state": "WAITING_FOR_USER",
  "blocked_reason": "规则冲突：槽位「活动开始时间」被两条已生效规则给出不同结论（...）。未裁决前两个效果都不应用，也不产出可发送结论。",
  "draft": {
    "draftable": false,
    "report_counts": { "Draft": 0, "Fact": 5, "Inference": 1, "Question": 3, "Task": 1 },
    "states_before": { "draft_43e62140a610d12dd8b619c0": "OPEN" },
    "states_after":  { "draft_43e62140a610d12dd8b619c0": "STALE" }
  },
  "outbound": {
    "external_actions": [],
    "approvals": [],
    "new_actions_from_conflict": 0,
    "new_approvals_from_conflict": 0,
    "channel_calls_after_control": 1,
    "channel_calls_after_conflict": 1,
    "control_outcome": "ExecutionOutcome(action_id='act_00004520261010T08370', action_state='SUCCEEDED', result_class='SUCCEEDED', ..., sender_called=True, sender_call_count=1, ...)"
  },
  "question": {
    "id": "q_27480f889690fbf851041e53",
    "affects": ["时间字段:event_start"],
    "question": "关于「活动开始时间」：两条已生效规则给出不同结论（rule_653f8daa... v1 → 缺失时必须追问，不得用另一个槽位顶替；rule_eb877d1a... v1 → 允许把来源槽位按规则当作目标槽位）。以哪一条为准？",
    "background": "规则冲突：槽位「活动开始时间」被两条已生效规则给出不同结论（...）\n规则 rule_653f8daa... v1：来自 notice@example.org 的通知：...\n规则 rule_eb877d1a... v1：..."
  },
  "audit_rule_hits": [ { "detail": { "applied": false, "effect": "slot_split.require_question", "rule_version": 1, ... } }, { "detail": { "applied": false, "effect": "slot_split.allow_alias", "rule_version": 1, ... } } ]
}
```

要点：

- **外发调用次数**：`对照组`先用真实 MVP-5 执行链路把一封**没有冲突**的草稿发出去
  （`result_class=SUCCEEDED`、`sender_call_count=1`、通道账本 1 条），证明计数器是活的；
  冲突发生后再看，通道账本**仍然是 1 条**（增量 0），并且冲突 Case 里
  `ExternalAction`/`Approval` 都是空列表；
- **不产出可发送结论**：`report_counts["Draft"]=0`，同时**已经存在**的那份草稿被标 `STALE`
  （不删除、不隐藏，是可见的"这份不能发了"）；
- **§14.5 的两个载体**都落齐：`Question`（`affects=["时间字段:event_start"]`、正文写明以哪条为准）
  与 `Case.blocked_reason`；`require_sendable()` 此时抛 `PermissionDenied`；
- `命中=2 应用=0 冲突=1`：两条规则都被记录为"命中但未应用"，所以"谁被卷进冲突"也是可查的。

### 5.7 §862：废止不擦历史

```
PS> & $PY -m unittest tests.growth.test_rules_lifecycle -v
[evidence] 废止前：处理 03（对照） | case=case_9d2b84d2d32483c529f186df state=WAITING_FOR_USER doc=doc_03_before-revoke Fact+5 Inference+1 Question+3 Task+1 draftable=False duplicate=False | growth doc=doc_03_before-revoke case=case_9d2b84d2d32483c529f186df 命中=1 应用=1 冲突=0
[evidence] 废止后：处理 03 | case=case_4a94dd90517ba23c79073b58 state=WAITING_FOR_USER doc=doc_03_after-revoke Fact+5 Inference+1 Question+2 Task+1 draftable=False duplicate=False | growth doc=doc_03_after-revoke case=case_4a94dd90517ba23c79073b58 命中=0 应用=0 冲突=0

[evidence] §862 废止「命中」与「历史」对照
{
  "before_revoke_03_hits": [ { "applied": true, "rule_id": "rule_653f8daa7a34748c82a2b7295105d40c", "rule_version": 1, "document_id": "doc_03_before-revoke", ... } ],
  "after_revoke_03_hits": [],
  "revoked_rule": { "state": "REVOKED", "revoked_at": "2026-10-10T08:37:05.281Z" },
  "old_case": "case_a87ceee803efad87e69bbc51",
  "old_case_audit_hit_records": [ { "applied": true, "rule_id": "rule_653f8daa7a34748c82a2b7295105d40c", "rule_version": 1, "document_id": "doc_02a_old-case", ... } ],
  "old_case_fact_ids": ["fact_26bd2534d7ea5d2e90b94252", "fact_cfda65a83f5fac635c56f38c"],
  "old_case_rule_links": ["rule_653f8daa7a34748c82a2b7295105d40c"],
  "old_audit_event_count": 54,
  "new_audit_event_count": 81
}
```

- 废止前：03 这封命中 v1（`命中=1 应用=1`）；废止后同款邮件 `命中=0`；
- 旧事务 `case_a87cee...` 的**审计命中记录、事实 ID、规则引用**都在断言里逐条比对过
  （`test_revoke_stops_new_cases_but_keeps_old_case_intact` 里做的是快照对比，不是"看起来还在"）；
- 审计只增不改：旧事件逐字节相等，事件总数从 54 涨到 81（新增的是废止与后续命中）。

回滚证据（同模块）：

```
[evidence] 版本语义：回滚 = 新版本 + 历史不动
{
  "versions": [
    { "version": 1, "state": "SUPERSEDED", "statement": "缺「活动开始时间」必须追问" },
    { "version": 2, "state": "SUPERSEDED", "statement": "缺「活动开始时间」必须追问，并要求问清线上/线下" },
    { "version": 3, "state": "ENABLED",    "statement": "缺「活动开始时间」必须追问" }
  ],
  "new_version_rollback_of": ["rule_653f8daa7a34748c82a2b7295105d40c"],
  "rule_objects_before": 2,
  "rule_objects_after": 3,
  "no_object_deleted": true,
  "v1_unchanged": true,
  "audit_events_before": 10,
  "audit_events_after": 17,
  "old_audit_events_unchanged": true
}
```

回滚到 v1 的内容 → 产生 **v3**（内容等同 v1，但 `rollback_of` 指向 v1）；v1/v2 的历史状态与内容
都没被动过（v1 不复活、v2 不被删除）。

### 5.8 §863：流程版本记录 + 在途/新事务两侧钉住

```
PS> & $PY -m unittest tests.growth.test_workflow_versions -v
[evidence] §863 在途用旧版本 / 新事务用新版本
{
  "workflow_id": "wf_6411691781675b77cca8d1b8bdc93a63",
  "versions": [
    { "version": 1, "state": "SUPERSEDED", "steps": 2 },
    { "version": 2, "state": "ENABLED",    "steps": 3 }
  ],
  "effective_version": 2,
  "in_flight_case": {
    "case_id": "case_8ec27af7658a3572097c3f3b",
    "pinned_version_id": "wfv_98cddf03e9b448ba6e87b254c29ce83f",
    "pinned_version_no": 1,
    "steps": ["确认参加", "上传材料"]
  },
  "new_case": {
    "case_id": "case_301817868354d5379d3391e6",
    "pinned_version_id": "wfv_987a2a7d702ebe83921fbdd2bfe01520",
    "pinned_version_no": 2,
    "steps": ["确认参加", "上传材料", "缴纳费用"]
  },
  "case_links": {
    "in_flight": { "source_document": ["doc_02a_in-flight"], "workflow_version": ["wfv_98cddf03e9b448ba6e87b254c29ce83f"] },
    "new":       { "source_document": ["doc_05_new-case"],  "workflow_version": ["wfv_987a2a7d702ebe83921fbdd2bfe01520"] }
  }
}
```

同一条测试里同时断言：在途 Case 钉 v1（步骤 2 个、`steps` 前后完全相等）、新 Case 拿 v2（步骤 3 个）、
`effective_workflow_version()` 是 v2；并且**再次**调用 `pin_case_workflow(在途 Case)` 仍然返回 v1
（不会把在途事务悄悄换到新版本）。要换版本只能 `repin_case_workflow(..., reason="...")`，理由为空直接
`ValidationError`（有专门用例）。

八件事的证据（缺一即拒 + 落库逐字段可查）：

```
[evidence] §10.4 流程抽象八件事
{
  "rejected_when_missing": ["trigger_conditions", "steps", "question_points", "approval_points", "outputs", "archive_requirements", "failure_handling", "scope"],
  "stored_version": {
    "version": 1,
    "trigger_conditions": ["主题含「报名」", "发件人是机构通知邮箱"],
    "steps": [ { "action": "确认参加", "with": "学生姓名" }, { "action": "上传材料", "with": "身份证号" } ],
    "question_points": ["学生姓名", "是否参加线下环节"],
    "approval_points": ["发送确认邮件"],
    "outputs": { "draft_kind": "email_reply", "archive": "确认记录" },
    "archive_requirements": ["原邮件归档", "确认记录归档"],
    "failure_handling": ["失败转人工", "3 次后告警"],
    "scope": "global"
  },
  "workflow_state": "ENABLED",
  "current_version_id": "wfv_98cddf03e9b448ba6e87b254c29ce83f"
}
```

流程回滚同样是"新版本"：

```
[evidence] §10.5 流程回滚 = 新版本
{
  "versions": [
    { "version": 1, "state": "SUPERSEDED", "steps": ["确认参加", "上传材料"] },
    { "version": 2, "state": "SUPERSEDED", "steps": ["确认参加", "上传材料", "缴纳费用"] },
    { "version": 3, "state": "ENABLED",    "steps": ["确认参加", "上传材料"] }
  ],
  "new_version_rollback_of": ["wfv_98cddf03e9b448ba6e87b254c29ce83f"],
  "v1_content_unchanged": true,
  "v2_content_unchanged": true
}
```

### 5.9 记忆过期：不再使用 + 历史保留

```
PS> & $PY -m unittest tests.growth.test_memory_validity -v
[evidence] 记忆过期：不再使用 + 历史保留
{
  "memory": {
    "id": "mem_37954fc62984fa93695f1a81f1765872",
    "statement": "家长偏好周五晚上联系，不要周末上午",
    "valid_until": "2026-10-05T01:00:00.000Z",
    "state_before_sweep": "ENABLED",
    "state_after_sweep": "REVOKED",
    "still_queryable": ["mem_37954fc62984fa93695f1a81f1765872"]
  },
  "usability": {
    "at_T0+2h":  { "expired": true,  "usable": false, "statements": [], "usable_memory_ids": [] },
    "at_T0+30m": { "expired": false, "usable": true, "statements": ["家长偏好周五晚上联系，不要周末上午"], "usable_memory_ids": ["mem_37954fc62984fa93695f1a81f1765872"] },
    "note": "两个快照都在清扫之前采集"
  },
  "facts": {
    "before_expiry": { "created": ["fact_56353a969fd3d4a0a78a050191d2679d"], "reused": [], "skipped": [] },
    "after_expiry":  { "created": [], "reused": [], "skipped": [ { "memory_id": "mem_37954fc6...", "reason": "已过期（2026-10-05T01:00:00.000Z）" } ] },
    "historical_fact_unchanged": true,
    "historical_fact": {
      "id": "fact_56353a969fd3d4a0a78a050191d2679d",
      "statement": "家长偏好周五晚上联系，不要周末上午",
      "links": { "memory": ["mem_37954fc62984fa93695f1a81f1765872"] },
      "citations": [ { "locator": "kv:memory", "source_object_id": "doc_01_memory-src", "supports": "用户确认的长期记忆（可回到来源资料）", ... } ]
    }
  },
  "sweep": {
    "first_run": ["mem_37954fc62984fa93695f1a81f1765872"],
    "second_run": [],
    "validity_task": { "id": "task_cbd461c105ab2a70fee67f6a54b825db", "kind": "memory_validity", "state": "DONE", "timeout_at": "2026-10-05T01:00:00.000Z", "outputs": { "memory_id": "mem_37954fc6...", "ttl_seconds": 3600, "valid_until": "2026-10-05T01:00:00.000Z" } }
  }
}
```

- 过期前落成的事实**原样保留**（`historical_fact_unchanged": true`，引用仍在）；
- 过期后新事务里 `created=[]`，跳过原因写明"已过期（…）"——不是静默消失；
- 清扫幂等（第二次返回空），清扫后记忆仍是 `REVOKED` 的**同一个对象**（`statement` 未变、仍可查）。

### 5.10 持久化优先：冷启动

```
PS> $env:ASISTENT_STORE="sqlite"
PS> & $PY -m unittest tests.growth.test_persistence_first -v
[evidence] 冷启动处理新邮件 | case=case_56c9072e1938061b6151d290 state=WAITING_FOR_USER doc=doc_02a_cold Fact+2 Inference+1 Question+1 Task+1 draftable=False duplicate=False | growth doc=doc_02a_cold case=case_56c9072e1938061b6151d290 命中=1 应用=1 冲突=0

[evidence] 持久化优先：冷启动状态全在库里
{
  "backend": "sqlite",
  "rule_versions": [
    { "id": "rule_653f8daa...", "version": 1, "state": "SUPERSEDED", "statement": "报名截止时间不是活动开始时间，缺活动开始时间必须追问" },
    { "id": "rule_b2e0fc72...", "version": 2, "state": "ENABLED",    "statement": "缺「活动开始时间」必须追问，且要一次问清" }
  ],
  "warm_case_hits": [1],
  "warm_case_hits_after_cold_start": [1],
  "cold_case": { "case_id": "case_56c9072e...", "audit_hits": [ { "rule_id": "rule_b2e0fc72...", "rule_version": 2, "applied": true, ... } ], "case_state": "WAITING_FOR_USER" },
  "workflow": { "in_flight_pinned": "wfv_...", "effective": "wfv_...", "cold_case_pinned": "wfv_...", "versions": [1, 2] },
  "memory": { "valid_until": "2026-10-05T01:00:00.000Z", "usable_at_T0+30m": ["家长偏好周五晚上联系"] },
  "audit": { "events_before": 51, "events_after": 72, "historical_events_unchanged": true },
  "stored_object_counts": { "AuditEvent": 51, "Case": 1, "Event": 18, "Fact": 2, "Inference": 1, "Memory": 1, "Question": 1, "Rule": 2, "SourceDocument": 2, "Task": 2, "Workflow": 1, "WorkflowVersion": 2 }
}
```

做法是：在实例 A 里把规则 v1/v2、流程 v1/v2、记忆有效期、命中记录都建起来，然后
**丢掉实例 A**（`close()`），只拿同一个库路径新建实例 B，再在 B 里处理一封**新**邮件——
B 命中库里的 v2（审计 `rule_version: 2`）、在途 Case 仍钉 v1、记忆有效期照样算得出来、
51 条历史审计逐字节不变。另一条用例验证"废止 + 回滚"的决定跨重启依然有效
（`[(1, SUPERSEDED), (2, REVOKED), (3, ENABLED)]`）。

### 5.11 契约零改动

```
PS> git diff --name-only 16c6315 -- asistent/contracts tests/contracts
（空）
PS> git diff --name-only
（空）
PS> git status --porcelain
?? asistent/growth/
?? tests/growth/
PS> git log --oneline 16c6315..HEAD
（空）
```

**注意一个容易误读的地方**：`git diff --name-only main -- asistent/contracts tests/contracts`
在本分支上**不是空的**，但差异**不是我的改动**，而是 `main` 已经走到 契约 v0.7：

```
PS> git log --oneline -1 main
a1457f0 fix(contracts): 契约 v0.7 —— content_origin 取值上收、完整性等级、未授权码、恢复安全字段（CP-18..23）
PS> git log --oneline 16c6315..main -- asistent/contracts
a1457f0 fix(contracts): 契约 v0.7 ...
```

也就是说：`main` 在我这条分支的基线（`16c6315`，契约 v0.6）之后又前进了（v0.7 + MVP-4 合并），
所以"分支 vs main"的差异里既有 main 新增的文件（`asistent/contracts/archive.py` 等 = 我这边"缺失"），
也有 main 改过的文件。**我这边对 contracts 的改动是 0**：与自己的基线差异为空、没有任何提交、
工作区未跟踪文件只有 `asistent/growth/` 与 `tests/growth/`。
合并时请以 v0.7 为基座 rebase，MVP-6 不依赖 v0.7 的任何新字段（只用 v0.6 已有的 `Rule`/`Memory`/
`Workflow`/`WorkflowVersion`/`Task`/`Case`/`Fact`/`AuditEvent`）。

---

## 6. 与 `asistent.understand` 的真实串联

```
PS> & $PY -m unittest tests.growth.test_understand_chain -v
[evidence] 真实理解链路（无规则） | case=case_2d2ee69159da3063e9994271 state=READY_FOR_REVIEW doc=doc_05_wrapped Draft+1 DraftVersion+1 Fact+4 Inference+1 Task+1 draftable=True duplicate=False | growth doc=doc_05_wrapped case=case_2d2ee69159da3063e9994271 命中=0 应用=0 冲突=0

[evidence] §859 真实理解链路（base 是 asistent.understand）
{
  "base_class": "RuleBasedUnderstander",
  "base_module": "asistent.understand.understander",
  "growth_extends": ["GrowthUnderstander", "RuleBasedUnderstander", "object"],
  "plain_facts":    [ { "slot": "registration_deadline", "outcome": "created" }, ... ],
  "wrapped_facts":  [ { "slot": "registration_deadline", "outcome": "created" }, ... ],
  "extractor_slots": ["registration_deadline", "event_start", "event_end", "submission_deadline"],
  "plain_case_state": "READY_FOR_REVIEW",
  "wrapped_case_state": "READY_FOR_REVIEW"
}
```

- `GrowthUnderstander` **继承**真实的 `asistent.understand.RuleBasedUnderstander`，并在 `analyze()`
  前后做成长判断；`base` 是可注入的真实对象（测试里显式传入并断言模块名）；
- 没有规则时，两边的 `Fact` 槽位与结果集合**逐条相等**——成长层不替理解层做抽取判断；
- 素材走真实夹具（`fixtures/public/understand/*.eml`）与真实 `fixtures` 抽取器（`extractor_slots` 可查）；
- 全链路：纠正 → 候选 → 确认 → 命中 v1 → 改版 → 命中 v2 → 按规则认定槽位（`allow_alias`，
  事实带 citation 且登记规则版本），每一步都是 `process_with_growth()` 跑真实流水线（§5.4）。

---

## 7. 已知限制（明确不做与不能做的）

1. **`Question` 没有 `kind` 字段**：契约 §14.5 写的 `Question(kind="conflict")` 在对象模型里**无法表达**
   （`Question` 的字段是 `case_id/question/background/required_format/affects/deadline/state/answer_id/
   asked_by_execution_id`）。MVP-3 的 `QuestionCandidate.kind` 在写库时被丢掉（`case_flow._upsert_question`）。
   现状：规则冲突的追问只能靠 `affects=["时间字段:<槽位>"]` + `background` 以 `规则冲突` 开头来识别，
   MVP-6 提供 `rule_conflict_questions(store, case_id)` 做这件事。**这是 CP 提案，不是我的实现选择。**
2. **跨模块的"不得产出可发送结论"只在成长侧成立**：MVP-6 保证"不新增草稿 + `require_sendable()` 抛
   `PermissionDenied` + 冲突 Case 无 `Approval`/`ExternalAction`"。但 MVP-5 的审批路径
   （`require_draft_version` / `create_approval`）**不检查** `Case.blocked_reason`，也就是"先冲突、
   再手工把旧草稿拿去批准"这条路目前没有被硬门闸挡住（旧草稿会被标 `STALE`，但如果有人绕过
   成长层直接调审批，仍可能通过）。要闭环需要 MVP-5 侧加一条"冲突未裁决不得批准/授权"的检查
   ——已列入 CP/集成建议。
3. **规则范围是"声明式但不做语义推理"**：`slot_split.allow_alias` 按用户给的规则把来源槽位的时间值
   复制成目标槽位（带 citation、登记规则版本），**不校验**这个别名在语义上是否真的成立
   （那是用户确认过的规则，系统只负责可追溯地执行）。也正因如此，同一个目标槽位被两个不同效果
   命中时，系统**拒绝猜**，走冲突流程。
4. **八件事只做"必填非空"校验**，没有为每件事定义更深的 schema（例如 `steps[].action` 的类型、
   `outputs` 的取值域）。这是刻意的：先保证"不会凭空少一样"，深 schema 留给后续 MVP。
5. **单进程假设**：本模块的"落库 → 审计"不是跨进程事务（与 MVP-3/5 一致）。多进程同时处理同一封
   邮件时，幂等由确定性 ID 与 `unique=` 约束兜底（重复投递用例覆盖），但没有分布式锁。
6. **没有 UI 回调**：冲突裁决在测试里的路径是"废止其中一条规则"（§5.6 有恢复用例）。手机端的
   裁决交互属于 MVP-4/MVP-7 的范围。
7. **`rule_hits()` / 审计查询是全表扫描**（`limit=None`）：MVP 规模（单机、单用户）够用，
   但没有为"审计很大"做索引优化；审计是只增不改的流水。
8. **过程记录（自查）**：写作过程中一次 `edit` 把 `if scope.matches(...)` 误吞进注释，
   导致"命中恒为 0"。`test_rule_hits_audit` 第一轮就暴露了它，修复后重跑了全部用例；
   本文档引用的所有证据都来自修复**之后**的运行（修复前的日志没有被引用，也没有被保留成证据）。

---

## 8. 契约变更与 CP 提案（未改任何契约文件）

MVP-6 **没有修改** `contracts/**`、`asistent/contracts/**`、`tests/contracts/**`（§5.11）。以下是建议：

| 编号 | 建议 | 现状如何绕过 |
| --- | --- | --- |
| CP-A | `Rule` 缺少结构化的"适用范围 + 效果"字段（现在只有自由文本 `statement`/`scope_ref`） | 把 `v=1;kind=...;effect=...;slots=A>B` 编码进 `scope_ref`，解析失败一律拒绝；坏处是它是字符串约定，别的模块看不懂 |
| CP-B | `Memory` 缺少 `valid_until`/`expired_at` 字段与 `EXPIRED` 状态（`RuleState` 只有 CANDIDATE/PENDING_CONFIRMATION/ENABLED/SUPERSEDED/REVOKED） | 用一条 `Task(kind="memory_validity", timeout_at=...)` 表达有效期，过期后清扫成 `REVOKED`；坏处是"过期"与"用户撤销"共用一个状态 |
| CP-C | `Question` 缺少 `kind` 字段，§14.5 的 `Question(kind="conflict")` 无法表达（`QuestionCandidate.kind` 写库时被丢） | 用 `affects=["时间字段:<槽位>"]` + `background` 前缀 `规则冲突` 识别；坏处是"区分冲突追问"要靠文本约定 |
| CP-D | 跨模块门闸：MVP-5 的审批/授权不读 `Case.blocked_reason` | MVP-6 侧用 `require_sendable()` 挡住"产出可发送结论"，并把已存在的草稿标 `STALE`；坏处是绕过成长层直调审批仍可行（见 §7.2） |

---

## 9. 复现步骤

```powershell
# 0) 进入 worktree
cd D:\dsh\个人助手\.worktrees\mvp-6-growth
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
$PY = "C:\Users\<you>\.conda\envs\<env>-a29d9cc4\python.exe"

# 1) 双后端全量
$env:ASISTENT_STORE="reference"; & $PY -m unittest discover -s tests -t . > var\_final_reference.log 2>&1
$env:ASISTENT_STORE="sqlite";    & $PY -m unittest discover -s tests -t . > var\_final_sqlite.log 2>&1
Get-Content var\_final_reference.log | Select-String '^Ran |^OK|^FAILED'
Get-Content var\_final_sqlite.log    | Select-String '^Ran |^OK|^FAILED'

# 2) MVP-6 逐模块（证据行都在 stdout 的 [evidence] 里）
& $PY -m unittest tests.growth.test_rules_lifecycle -v
& $PY -m unittest tests.growth.test_rule_hits_audit -v
& $PY -m unittest tests.growth.test_rule_conflict -v
& $PY -m unittest tests.growth.test_workflow_versions -v
& $PY -m unittest tests.growth.test_memory_validity -v
& $PY -m unittest tests.growth.test_understand_chain -v
$env:ASISTENT_STORE="sqlite"; & $PY -m unittest tests.growth.test_persistence_first -v

# 3) 契约零改动（对照自己的基线；main 已前进到 v0.7，见 §5.11）
git diff --name-only 16c6315 -- asistent/contracts tests/contracts   # 期望为空
git status --porcelain                                              # 期望只有 ?? asistent/growth/ 与 ?? tests/growth/

# 4) 设备复验（RK3588 / Python 3.10.12）；只写自己的目录，绝不碰 ~/code/asistent
tar -cf "$env:TEMP\mvp6.tar" --exclude="*.log" --exclude="*.pyc" --exclude="__pycache__" --exclude="var" `
    asistent tests fixtures docs contracts scripts SPEC.md README.md .gitignore
scp "$env:TEMP\mvp6.tar" board:~/asistent-spike/mvp6-check/full.tar
ssh board "rm -rf ~/asistent-spike/mvp6-check/run && mkdir -p ~/asistent-spike/mvp6-check/run && " +
            "cd ~/asistent-spike/mvp6-check/run && tar -xf ../full.tar && " +
            "/usr/bin/python3 -m unittest discover -s tests -t . 2>&1 | grep -E '^Ran |^OK|^FAILED' && " +
            "ASISTENT_STORE=sqlite /usr/bin/python3 -m unittest discover -s tests -t . 2>&1 | grep -E '^Ran |^OK|^FAILED' && " +
            "bash scripts/test.sh 2>&1 | grep -E '^Ran |^OK|^FAILED|^契约快照'"
```

`var/` 下的文件是本次开发的一次性脚手架（探针、日志、证据抽取脚本），**不是交付物**，
不参与构建与测试，可随时删除。

---

## 10. 一句话结论

规则以**数据 + 版本 + 审计**的形式存在：没确认不生效，命中的是哪一版查得到，两条规则打架就停下
来问人，废止和回滚只增不改，流程版本钉在事务上不追最新，记忆过期就不再当事实但历史一字不动——
并且这些结论在**换一个连接之后**依然成立。
