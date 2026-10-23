# MVP-3：理解、追问与草稿

- 状态：已实现（worktree `mvp-3-understand`，分支 `mvp/3-understand-draft`，基于 `contracts-v0.5` 冻结点）
- 日期：2026-10-10
- 代码：`asistent/understand/`（新增，未改动任何契约/存储/邮件/索引文件）
- 测试：`tests/understand/`（100 项）
- 样例：`fixtures/public/understand/`（6 份合成中文邮件，无真实个人信息）
- 决策记录：[ADR-0004 理解层](0004-understanding-layer.md)

---

## 一、这一版要解决什么

把一封邮件/一份资料变成**可追溯**的理解结果：带语义的时间字段、待办、缺失材料、行动建议、回复草稿，以及**需要用户回答的 Question**。

四条硬性验收（[SPEC.md](../../SPEC.md) §837-840）：

| # | Given/When/Then | 本版做法 |
|---|---|---|
| 1 | 一封含多个时间字段的通知 → 保留字段语义并区分报名截止与活动开始 | 时间字段一律带 `semantic` 槽位 + 原文引用；语义靠**同句标签证据**判定，判定不了就追问 |
| 2 | 缺少回复所需资料 → 创建 Question 且手机端可回答 | 每份缺失材料一条 `Question(state=OPEN, required_format=text, affects=[材料:…])` |
| 3 | 用户编辑草稿 → 新 `DraftVersion` 可看差异可恢复旧版本 | 草稿只增不改；`diff_versions` / `restore_version`（恢复=再产生新版本）；审批绑定确定版本 |
| 4 | 事实与资料冲突 → 不生成可发送结论，展示冲突并追问 | 冲突落成 `Question`（含两个取值与两个来源）；草稿候选不落库，已存在草稿标 `STALE` |

---

## 二、设计

### 2.1 模块结构

| 文件 | 职责 |
|---|---|
| `model.py` | 理解结果数据结构：`TimeField`、`TodoItem`、`MaterialRequirement`、`ActionSuggestion`、`DraftCandidate`、`QuestionCandidate`、`Conflict`、`SourceText`、`UnderstandingResult`、`CitationRef`（引用唯一入口） |
| `extract.py` | 确定性规则/模板抽取器（纯函数）：中文日期时间表达、标签语义、待办、材料清单、待定/歧义/冲突识别 |
| `understander.py` | `Understander` 协议 + `RuleBasedUnderstander`（默认）+ `ModelBackedUnderstander`（可选，含预算守门） |
| `case_flow.py` | 把理解结果落成共享对象（Case/Task/Fact/Inference/Question/Draft/DraftVersion），走 `store.transition()`，幂等 |
| `drafts.py` | 草稿版本、diff、恢复、审批绑定 |

### 2.2 时间字段：语义是字段的一部分，不是格式

抽取结果**不允许只留裸时间戳**。一个时间槽位是：

```
semantic（registration_deadline / event_start / event_end / submission_deadline /
          reply_deadline / meeting_time / payment_deadline / other / unknown）
+ original_text（原文写法，永不改写）
+ original_label（原文里的标签词，如「报名截止时间」）
+ normalized_utc（按来源时区换算的 UTC）
+ confidence
+ citations（至少一条，能回到原文行）
```

语义判定顺序（`extract.py::label_for`）：

1. **同句**内、跨度之前 20 字里的时间标签（句子边界是 `。；;\n！？!?`，**不跨句**）；
2. 跨度前 12 字出现「前 + 行为动词」（`请在10月12日前回复`）→ 按行为定语义；
3. 跨度紧邻的行为动词（`报名截止到10月12日`）。

关键限制与研究过的失败模式：

- 「报名截止时间：待定。场地将于10月24日开放参观」——A 句的标签**不能**解释 B 句的时间；这条由 `test_label_in_a_different_sentence_does_not_leak` 锁住。
- 落款日期与邮件 `Date` 头同一天 → 视为签名，不追问（否则每封邮件都多一条噪声追问）。比较按**来源时区**的日历日，不按 UTC（`Date: 8 Oct 2026 00:30 +0800` 在 UTC 是 10 月 7 日）。
- 裸日期（正文里出现、没有任何标签解释）→ `unaccounted` + `Question(kind=ambiguity)`，**不得**当事实。
- 「待定」不是"没有这个字段"，而是"字段存在、值缺失" → `Question(kind=missing_time)`，并保留 `original_text="待定"`、`confidence=0`、`normalized_utc=None`。
- 时间区间（`14:00 至 16:30`）在抽取层落成两个语义槽位（`event_start` + `event_end`，各自带引用），但**草稿里只渲染一行**——用户读到的是一句原文。

### 2.3 引用守卫：没有引用就不是事实

- `Citation` 必须四条齐全：`path` / `locator` / `content_hash` / `supports`；理解层统一产出 `line:N` 或 `line:N-line:M` 的 `locator`，测试用 `parse_locator()` 解回行号并**打开原文**验证。
- `CitationRef.require()` 是唯一构造入口；缺字段直接 `ValueError`（契约 `citations.py` 的定义）。
- 给不出引用的内容只能落成 `Inference`（候选区）或 `Question`。
- **模型输出同样受此约束**：模型给的结论没有引用 → 降级成 `Question` + warning（见场景 5 与 `test_model_payload_without_citation_becomes_question`）。
- 原始资料只读：处理前后 `SourceDocument` 的 `content_hash` / `origin_path` / `identity_key` 不变，`content_version` 保持 1。

### 2.4 追问：不明确就停

四类阻塞各自对应一种 `Question.kind`：

| kind | 触发 | 例子 |
|---|---|---|
| `missing_material` | 原文提到材料要求但没有"已提供"证据 | 「监护人知情同意书」是否已经准备好？ |
| `missing_time` | 标签存在但值是「待定」 | 「报名截止时间」目前是「待定」——具体时间是什么时候？ |
| `ambiguity` | 时间表达没有语义标签 | 「10月24日」属于哪一类（报名截止？活动开始？）？ |
| `conflict` | 同一语义槽位在跨资料出现不同取值 | 2026-10-25 vs 2026-10-20，以哪一个为准？ |

有阻塞项 → `Case.state = WAITING_FOR_USER` + `waiting_reason` 写明 `missing_material` / `missing_time` / `ambiguity` / `conflict`，`next_action = 等待用户回答追问`。

### 2.5 冲突：不产出可发送结论

跨资料同一语义槽位出现两个不同 UTC → `Conflict`（含 `values` / `sources` / `citations`）：

- `UnderstandingResult.draftable()` 为 False → 草稿候选**不落库**；
- 该 Case 上**已存在**的 `OPEN` 草稿被标 `DraftState.STALE`，并从 `case.draft_ids` 移除（版本历史保留，供审计）；
- 冲突详情写进 `Case.blocked_reason`，用户打开事务直接看到冲突本身。

### 2.6 草稿版本化

- `Draft` 是稳定容器（`derive_draft_id(case_id, target_kind, target_ref)`，同 Case 同目标 = 同一草稿）；
- 内容全在 `DraftVersion` 上：`version_no` / `body` / `content_hash` / `author_kind` / `author_id` / `change_reason` / `citations` / `parent_version_id` / `target_object_id`（草稿阶段为 `None`，未发送）；
- 编辑同一正文 → 幂等，不新增版本（避免"点两次保存"造两条一样的历史）；
- `restore_version(vN)` **强制**产生新版本（`force=True`），正文等于 vN；
- `approval_binding()` 返回 `(draft_id, version_id, version_no, content_hash)`；指定历史版本 → `ValidationError`；
- 版本历史的权威来源是**存储里的 `DraftVersion` 对象**（按 `draft_id` 查），不是 `Draft.version_ids` 反向指针 —— 见 §五 已知限制第 1 条。

### 2.7 离线优先 + 模型可替换

- 默认 `RuleBasedUnderstander`：纯标准库、离线、零成本、同一输入同一输出，产物带 `producer = "rules:mvp3-1"`；
- `ModelBackedUnderstander` 必须**显式配置后端与预算**，否则构造即失败：
  - 无预算 → `ValidationError`（§1064：未设预算禁止无人值守调用）；
  - `max_calls_per_day = 0` → `ValidationError`（"禁用"应该是关掉模型路径，不是把上限设成 0）；
  - 无后端 → `ModelDisabled`；
- 超预算 → `BudgetExceeded`，`call_log().blocked_reason` 记录原因，**不再真的调用后端**；
- 调用失败：没有 fallback → `UnderstandError`；有 fallback → 退回规则抽取器，`producer = "model->fallback:…"` + `warnings` 写明降级；
- 提示词要求输出**结构化 JSON + 引用**，不解析自由文本。

---

## 三、原始命令与原始输出（验收证据）

### 3.1 场景证据：`tests/understand/evidence_dump.py`

```
$ python -X utf8 tests/understand/evidence_dump.py
```

（原始输出，未做任何编辑）

```text
==============================================================================
场景 1（SPEC §835-836）同一封通知的多个时间各有语义
==============================================================================
  time 报名截止时间           semantic=registration_deadline  original=10月20日 17:00     normalized_utc=2026-10-20T09:00:00.000Z confidence=1.00
  time 活动开始时间           semantic=event_start            original=11月2日 09:00      normalized_utc=2026-11-02T01:00:00.000Z confidence=1.00
  time 活动结束时间           semantic=event_end              original=11月4日 17:00      normalized_utc=2026-11-04T09:00:00.000Z confidence=1.00
  time 提交截止时间           semantic=submission_deadline    original=10月28日 23:59     normalized_utc=2026-10-28T15:59:00.000Z confidence=1.00
  case_state=READY_FOR_REVIEW draftable=True
  outcome Fact          created registration_deadline
  outcome Fact          created event_start
  outcome Fact          created event_end
  outcome Fact          created submission_deadline
  outcome Fact          created todo
  outcome Fact          created todo
  outcome Fact          created todo
  outcome Inference     created reply
  outcome Task          created task_d963335c30eba6ce096ed311
  outcome Task          created registration_deadline
  outcome Task          created task_e20f56633f65283ab56f3e3f
  outcome Draft         created draft_49ebd13b410c7bb235d01bdb
  outcome DraftVersion  created v1
  store.stats() = {'SourceDocument': 1, 'Case': 1, 'Fact': 7, 'Inference': 1, 'Task': 3, 'Draft': 1, 'DraftVersion': 1, 'AuditEvent': 33, 'Event': 4, '_total': 15}
==============================================================================
场景 2 草稿版本化：编辑 / diff / 恢复（历史只增不改）
==============================================================================
  draft_id = draft_e9d78fd8d005aabba4f169a6
  v1 body = '您好：\n\n已收到贵方关于「周末科普讲座报名确认（含活动时间）」的通知，现回复如下：\n- 报名截止时间：10月16日 18:00（原文：报名截止时间）\n- 活动开始时间 / 活动结束时间：10月17日 14:00 至 16:30（原文：活动开始时间）\n- 我方已知悉上述时间安排，将按要求处理。\n\n如有变动请回复告知，谢谢。'
  v1 citations = 3
  v2 version_no=2 body='您好：\n\n已收到贵方关于「周末科普讲座报名确认（含活动时间）」的通知，现回复如下（已按您的意见精简）：\n- 报名截止时间：10月16日 18:00（原文：报名截止时间）\n- 活动开始时间 / 活动结束时间：10月17日 14:00 至 16:30（原文：活动开始时间）\n- 我方已知悉上述时间安排，将按要求处理。\n\n如有变动请回复告知，谢谢。'
  diff v1->v2 identical=False added=1 removed=1
    + 已收到贵方关于「周末科普讲座报名确认（含活动时间）」的通知，现回复如下（已按您的意见精简）：
    - 已收到贵方关于「周末科普讲座报名确认（含活动时间）」的通知，现回复如下：
  restore -> 新版本号=3 正文==v1? True 历史版本号=[1, 2, 3]
  v1 正文仍然可用（未被覆盖）= '您好：\n\n已收到贵方关于「周末科普讲座报名确认（含活动时间）」的通知，现回复如下：\n- 报名截止时间：10月16日 18:00（原文：报名截止时间）\n- 活动开始时间 / 活动结束时间：10月17日 14:00 至 16:30（原文：活动开始时间）\n- 我方已知悉上述时间安排，将按要求处理。\n\n如有变动请回复告知，谢谢。'
  current_version_id=dv_fca4efb8a3d07bd8c27725f3
  审批绑定 draft=draft_e9d78fd8d005aabba4f169a6 version_no=3 version_id=dv_fca4efb8a3d07bd8c27725f3 hash=c07e1f21507f80e6
  审批历史版本 v1 被拒绝: ValidationError: 只能审批当前版本（历史版本已不是草稿内容）: draft=draft_e9d78fd8d005aabba4f169a6 指定 v1，当前 v3
==============================================================================
场景 3（SPEC §839-840）资料缺失与时间不明一律转成追问
==============================================================================
  --- 03-missing-material.eml
      case_state=WAITING_FOR_USER draftable=False
      blocking: 待回答（missing_material）：材料/资料「监护人知情同意书」我在原文里只看到要求，没有看到已提交或已具备的证据。是否已经准备好？
      blocking: 待回答（missing_material）：材料/资料「学生一寸免冠照片」我在原文里只看到要求，没有看到已提交或已具备的证据。是否已经准备好？
      Question id=q_1520a33b6a4ad2c18a841ff3 state=OPEN required_format=text affects=['材料:监护人知情同意书']
               question   = 材料/资料「监护人知情同意书」我在原文里只看到要求，没有看到已提交或已具备的证据。是否已经准备好？
               background = 原文提到该材料，但缺少「已提供」的依据（SPEC §7.4）。
      Question id=q_b5c7cae2875de92ade79886c state=OPEN required_format=text affects=['材料:学生一寸免冠照片']
               question   = 材料/资料「学生一寸免冠照片」我在原文里只看到要求，没有看到已提交或已具备的证据。是否已经准备好？
               background = 原文提到该材料，但缺少「已提供」的依据（SPEC §7.4）。
      draft_ids on Case: []
  --- 04-ambiguous-time.eml
      case_state=WAITING_FOR_USER draftable=False
      blocking: 待回答（missing_time）：「报名截止时间」目前是「待定」——具体时间是什么时候？
      blocking: 待回答（ambiguity）：原文出现时间「10月24日」，但没说它属于哪一类（报名截止？活动开始？还是别的）。请指明它的语义。
      Question id=q_50e55efd45f54623e3342d0f state=OPEN required_format=text affects=['时间字段:registration_deadline']
               question   = 「报名截止时间」目前是「待定」——具体时间是什么时候？
               background = 原文标签：报名截止时间；原文措辞：待定
      Question id=q_448d0cba565cd4caf5d1547b state=OPEN required_format=text affects=['时间字段:unknown']
               question   = 原文出现时间「10月24日」，但没说它属于哪一类（报名截止？活动开始？还是别的）。请指明它的语义。
               background = 没有语义标签的时间不得当事实使用（SPEC §7.4、§21.1）
      draft_ids on Case: []
==============================================================================
场景 4（SPEC §835-840 第 4 条）跨资料时间冲突：不生成可发送结论
==============================================================================
  [资料 A] 报名截止时间 = 2026-10-20T09:00:00.000Z (10月20日 17:00)
  [资料 B] 报名截止时间 = 2026-10-25T09:00:00.000Z (10月25日 17:00)
  conflicts = 1
    conflict semantic   = registration_deadline
    conflict values     = ['2026-10-25T09:00:00.000Z', '2026-10-20T09:00:00.000Z']
    conflict sources    = [{'source_object_id': 'doc_scene4_b', 'path': 'fixtures/public/understand\\02b-notice-conflict.eml', 'label': '报名截止时间', 'text': '10月25日 17:00', 'normalized_utc': '2026-10-25T09:00:00.000Z'}, {'source_object_id': 'doc_scene4_a', 'path': 'fixtures/public/understand\\02a-notice-conflict.eml', 'label': '报名截止时间', 'text': '10月20日 17:00', 'normalized_utc': '2026-10-20T09:00:00.000Z'}]
    conflict detail     = 同一语义槽位 registration_deadline 出现 2 个不同时间值：2026-10-25T09:00:00.000Z / 2026-10-20T09:00:00.000Z
    conflict citations  = 2 条
  Question 总数 = 1
    Question id=q_eb6bb88bffe99f7229eff210 state=OPEN affects=['时间字段:registration_deadline']
             question   = 关于「registration_deadline」：不同资料给出的时间不一致（2026-10-25T09:00:00.000Z / 2026-10-20T09:00:00.000Z）。以哪一个为准？
             background = 同一语义槽位 registration_deadline 出现 2 个不同时间值：2026-10-25T09:00:00.000Z / 2026-10-20T09:00:00.000Z
  case.state          = WAITING_FOR_USER
  case.draft_ids      = []
  case.blocked_reason = 同一语义槽位 registration_deadline 出现 2 个不同时间值：2026-10-25T09:00:00.000Z / 2026-10-20T09:00:00.000Z
  draftable           = False
  Draft 对象（保留审计，但状态必须是 STALE）:
    Draft id=draft_6492b01ca3dc892a96d23fa2 state=STALE version_ids=['dv_54a4c6e483e46fc0be5f5935']
  可发送草稿（state=OPEN）数量 = 0（期望 0）
  case.draft_ids 中剩余的可发送草稿 = []（期望 []）
  store.stats()       = {'SourceDocument': 2, 'Case': 1, 'Fact': 5, 'Inference': 2, 'Task': 2, 'Draft': 1, 'DraftVersion': 1, 'Question': 1, 'AuditEvent': 38, 'Event': 7, '_total': 15}
==============================================================================
场景 5 默认路径零模型调用；模型路径必须显式配置且预算封顶
==============================================================================
  producer            = rules:mvp3-1
  注入的 fail-if-called backend 调用次数 = 0（期望 0）
  规则理解器调用日志 calls=0 cost=0.0 blocked_reason=''
  draftable=True Draft 数=1
  ModelBackedUnderstander(未配置预算) 拒绝: ValidationError: 预算未配置：模型后端必须显式配置预算后才能启用（SPEC §1064）
  ModelBackedUnderstander(模型后端未配置) 拒绝: ModelDisabled: 模型后端未配置：未显式启用时任何路径都不得尝试联网调用（SPEC §1064）
  第 1 次调用：backend.calls=1 producer=model:fake-model question 数=0
  第 2 次调用被拦: BudgetExceeded: 模型调用预算已耗尽（调用数 1/1），任务停下等待用户调整预算（SPEC §1064）
  最终调用日志 calls=1 blocked_reason='当日调用数已达上限 1'
  counting backend 总调用次数 = 1（期望 1）
==============================================================================
证据导出结束（全部为确定性输出，未写入任何仓库数据）
```

**对应到四条验收 Given/When/Then：**

| 验收 | 证据位置（上面的原始输出） |
|---|---|
| §837 多时间字段保留语义 | 场景 1 的 4 行 `time … semantic=…`（4 个不同语义、4 个不同 UTC） |
| §838 缺资料 → Question | 场景 3 的 `03-missing-material.eml` 段（2 条 `Question state=OPEN required_format=text affects=['材料:…']`） |
| §839 编辑 → 新版本可 diff 可恢复 | 场景 2 的 `v2 version_no=2` / `diff v1->v2 identical=False added=1 removed=1` / `restore -> 新版本号=3` |
| §840 冲突 → 不生成可发送结论 + 追问 | 场景 4 的 `conflict values = [...]` + `Question … 以哪一个为准？` + `可发送草稿（state=OPEN）数量 = 0` + `case.draft_ids = []` |

### 3.2 全量测试（开发机，reference 后端）

```
$ python -X utf8 -m unittest discover -s tests -t .
```

```text
Ran 404 tests in 14.439s

OK (skipped=5)
```

> `tests/` 里其他 MVP 的 evidence 脚本会把 `[evidence] …` 行打进 stdout，与本版无关；上面是测试运行摘要。

### 3.3 理解层测试（单独跑）

```
$ python -X utf8 -m unittest discover -s tests\understand -t .
```

```text
Ran 100 tests in 0.623s

OK
```

### 3.4 换存储实现复跑（证明没有依赖 ReferenceStore 的私有行为）

```
$ $env:ASISTENT_STORE="sqlite"; python -X utf8 -m unittest discover -s tests\understand -t .
```

```text
Ran 100 tests in 5.151s

OK
```

### 3.5 设备复跑（RK3588，`/usr/bin/python3` 3.10.12，自建目录）

```text
$ ssh board "uname -a; /usr/bin/python3 -V"
Linux board-host 6.1.43-rockchip-rk3588 #1.0.0 SMP Mon Jul  8 11:54:40 CST 2024 aarch64 aarch64 aarch64 GNU/Linux
Python 3.10.12
```

```text
$ ssh board "cd ~/asistent-spike/mvp3-check-1010-1533 && /usr/bin/python3 -m unittest discover -s tests/understand -t ."
....................................................................................................
----------------------------------------------------------------------
Ran 100 tests in 0.812s

OK
```

```text
$ ssh board "cd ~/asistent-spike/mvp3-check-1010-1533 && /usr/bin/python3 -m unittest discover -s tests -t ."
Ran 404 tests in 10.955s

OK (skipped=3)
```

> 设备侧跳过数比开发机少 2：有几项测试按"平台能力探测"条件跳过，与理解层无关。
> 传输方式：`tar -c -f src.tar -T <filelist>` → `scp` → 远端 `tar -x`，落在自建目录 `~/asistent-spike/mvp3-check-1010-1533/`；
> **没有触碰** `~/code/asistent`、`~/.dsh`、`~/miniforge3`。

### 3.6 契约未被改动

```
$ git diff --name-only main -- asistent/contracts tests/contracts
(空输出)
```

```
$ git status --short
?? asistent/understand/
?? docs/adr/0004-understanding-layer.md
?? docs/mvp/mvp-3.md
?? fixtures/public/understand/
?? tests/understand/
```

> `asistent/understand/**` 等全部是**新增**文件，所以 `contracts-v0.5` 的契约测试在改动前后都是 404 OK。

### 3.7 覆盖清单（`tests/understand/**`，100 项）

| 文件 | 项数 | 覆盖 |
|---|---|---|
| `test_time_semantics.py` | 14 | 4 个语义槽位互不混淆、区间尾 → `event_end`、跨句标签不成立、同句标签成立、歧义 → 追问、待定 → `missing_time`、签名日期不追问、缺年份用锚年、跨年（12 月底收到「1月3日」→ 次年）、来源时区取自 `Date` 头、解析不了就不猜 |
| `test_citation_guard.py` | 10 | 引用缺字段构造即失败、无引用不得成为 `Fact`、落库 `Fact` 的引用能打开原文行、无引用内容落 `Inference`、原始对象未被覆盖、抽取是纯函数、规则集版本化 |
| `test_extract.py` | 16 | 待办抽取与去碎片、`due` 绑定语义槽位、回复要求识别（含「无需回复」识别为"不要求"）、材料只在清单语境取、`missing` / `to_submit` 状态区分、裸日期 → 歧义、签名日期抑制（含跨时区）、非 ASCII / RFC2047 头部解码、正文行号能落回原始文件 |
| `test_questions.py` | 10 | 缺材料每项一条 `Question`、待定 → `missing_time`、歧义不落时间字段、冲突一条追问含两个取值、追问幂等、冲突无可发送结论、冲突后旧草稿标 `STALE`、追问 OPEN 且可答、资料齐全零追问、有阻塞 → `WAITING_FOR_USER` |
| `test_case_flow.py` | 20 | 落库对象全是契约类型、Case 双向链接、Case 收集 fact/task/draft ids、待办 `assignee=user` 且有 `inputs`、重复处理零新增对象、再跑不重复追问/草稿、状态机合法路径、目标/下一步/等待原因、`READY_FOR_REVIEW`、草稿未发送未审批、无回复要求不产草稿、有阻塞草稿不落库、`outcomes` 覆盖每个对象、事件去重键、审计轨迹、两资料两 Case、显式同 Case 汇总、缺 ID 拒绝、fixture 矩阵、每个 fixture 可幂等重跑 |
| `test_draft_versions.py` | 14 | v1 关联与状态、编辑产生新版本且 v1 不变、同正文幂等、空正文拒绝、diff、自比 identical、恢复产生新版本、多版恢复全历史、审批绑定确定版本、审批历史版本被拒、恢复后绑定新版本、草稿本身不可发送、区间只渲染一行 |
| `test_model_guard.py` | 16 | 默认路径零模型调用、`producer` 是规则集、规则理解器不持后端、未配置预算/后端/零预算均拒绝、`budget_from_mapping`、调用数与花费双上限、无 fallback 抛 `UnderstandError`、有 fallback 降级 + warning、模型结论缺引用 → 追问、带完整引用 → `Fact`、自由文本不抽取、提示词要求 JSON + citation、调用日志是副本 |

---

## 四、已知限制（必须知道，不要当成 bug 猜）

1. **`Draft.version_ids` 不是权威来源**。版本历史一律读存储里的 `DraftVersion` 对象（按 `draft_id` 查），`version_ids` 只在写入时维护、用于审计与人工阅读。
   *原因*：实测踩过——ReferenceStore 往返后 `version_ids` 为空，重跑会算出 `version_no=2` 并**再建一版同样内容**，幂等被破坏。修法见 `drafts.py::versions_of` 的 docstring。
2. **时间语法是有限集**：数字日期（`10月20日` / `2026-10-20` / `10/20`）、时间（`17:00`）、今天/明天/星期几、区间尾、「待定」。
   `下周三下午三点`、`本周内`、`月底`、`国庆假期` 这类表达**不会**被解析成时间字段，只会留在正文里（不猜）。
3. **跨年判定用 30 天阈值**：解析出的月日早于锚 30 天以上 → 判为次年。12 月 31 日收到「1月1日」正确；收到「11月1日」（早 60 天）也会被判为次年，属于已知的取舍。
4. **材料名识别依赖清单语境**。只有「…如下：」/「包括」/「为」这类引子后面的清单项才会成为材料；`说明补交计划` 这类动宾短语不会被误判成材料，但**清单写得非常口语化时可能漏掉**（漏掉 = 不产材料项，不会凭空造）。
5. **`REQUIREMENT_MARKERS` 是穷举表**。没收录的措辞（例如「烦请惠复」）不会被识别为"要求回复"，于是不产草稿。这是**保守失败**：宁可不生成草稿，也不生成不该有的草稿。
6. **多 part 邮件取第一个可读正文**（`text/plain` 优先，HTML 退化为去标签文本）；引用行号以**原始文件**为基准（`SourceText.body_start_line` 做偏移）。
7. **审计事件会随重跑增长**：核心业务对象（Fact/Task/Question/Draft/DraftVersion/Case/SourceDocument）重跑零新增，但审计/事件是追加式设计（§11.3），条目数会变。
8. **模型路径在设备上不可用**（设备无 pip、也没有配后端）。默认路径不需要它；开启模型路径需要先给预算与后端配置。
9. **`Case.blocked_reason` 承载冲突详情**：契约里没有独立的"冲突对象"，本版把冲突详情写进 `Case.blocked_reason` + `Question.background`，用户可见但不利于程序化查询（见 §五 的 CP 提案候选）。

---

## 五、契约侧缺口（未改契约，全部在 `asistent/understand/**` 内绕过）

按纪律没有修改 `asistent/contracts/**`。以下三处是绕过方式，**不作为 CP 提案强推**（都可用现有字段表达），但如果后续 MVP 需要程序化查询冲突/降级状态，值得提 CP：

1. **事件类型**：契约 `Event.kind` 没有"知识变化"枚举值，本版用字符串 `"KNOWLEDGE_CHANGED"`。
2. **草稿失效状态**：契约 `DraftState` 只有 `OPEN/LOCKED/STALE/REVOKED`，冲突导致的失效复用 `STALE`（语义略窄：`STALE` 通常表示"内容过期"，这里是"依据冲突"）。失效原因写在审计条目里。
3. **冲突对象缺失**：没有独立的 `Conflict` 共享对象，因此冲突无法被 `find()` 查询，只能读 `Case.blocked_reason` 与 `Question`。
4. **`range_end_utc` 不在 `TemporalField` 上**：契约的 `TemporalField` 没有区间尾字段，本版把区间尾表达成**两个语义槽位**（`event_start` + `event_end`，各自带引用），草稿渲染时按"两个槽位 `original_text` 相同"合并成一行。

---

## 六、怎么自己验证

```bash
# 理解层测试
python -m unittest discover -s tests/understand -t .

# 全量（reference 后端）
python -m unittest discover -s tests -t .

# 换存储实现
ASISTENT_STORE=sqlite python -m unittest discover -s tests/understand -t .

# 打印人类可读证据（场景 1-5）
python -X utf8 tests/understand/evidence_dump.py

# 契约没被改
git diff --name-only main -- asistent/contracts tests/contracts
```
