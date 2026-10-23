# 共享基础契约 CONTRACT（冻结）

> 文件名自 v0.3 起**版本无关**：契约变更只更新版本号与变更记录，不再改名，避免散落各处的引用随版本漂移。

- 状态：**已冻结**（当前标签 `contracts-v0.8`；历史标签 `contracts-v0.7`、`contracts-v0.6`、`contracts-v0.5`、`contracts-v0.4`、`contracts-v0.3`、`contracts-v0.2`、`contracts-v0.1`）
- 契约版本：`v0.8`；对象 `schema_version = 1`；事件 `event_schema_version = 1`
- 版本历史：`v0.1` 首版冻结 → `v0.2` 检索规范化 + 时钟守门 → `v0.3` 存储接口增补（§11） → `v0.4` 顺序/内容来源/可选能力语义（§12） → `v0.5` 定序合法性 + 事件访问路径（§13） → `v0.6` 审批可用性说清变化字段 + 授权唯一 + TTL 常量（§14） → `v0.7` `content_origin` 规范取值 / 完整性等级 / 未授权码 / 恢复安全字段（§15） → `v0.8` 追问类别与记忆有效期落字段 / 阻塞事务不得被批准 / 资料-事务双向关联 / 阻塞原因可清除（§16）
- 变更提案与裁决记录：`contracts/CHANGELOG.md`
- 正文来源：`asistent/contracts/**`（代码是唯一事实源，本文档与代码由脚本校验一致）
- 状态表：`contracts/state-tables.md`（自动生成，勿手改）
- 机器可读快照：`contracts/contracts-v0.8.json`
- 一致性校验：`python3 scripts/gen_contract_snapshot.py --check`
- 变更流程：只能提案 → 主 Agent 裁决 → 发布新版本，登记在 `contracts/CHANGELOG.md`（SPEC §19、§21.1）

> 依据：SPEC.md v0.4 §4（运行模型与共享基础契约）、§5（对象与生命周期）、§6（资料与引用）、
> §7（邮件与幂等）、§8（草稿/追问/审批）、§13（失败与恢复）、§12/§21.4（归档与恢复）、§21.1/§21.2（精确定义）。

---

## 1. 通用对象字段与写入语义

每个共享对象都有：

| 字段 | 含义 | 约束 |
|---|---|---|
| `id` | 稳定 ID，前缀由 `ids.ID_PREFIXES` 登记 | 由核心分配，模型不得自造 |
| `schema_version` | 对象模式版本 | 写入时记录，用于迁移 |
| `revision` | 乐观并发版本 | 新建为 1，每次成功写入 +1 |
| `created_at` / `updated_at` | UTC ISO-8601（毫秒 + `Z`） | 展示时区默认 `Asia/Shanghai` |
| `links` | 关联 ID 索引 | 跨对象反查用 |

写入规则（`StoreProtocol.put`）：

1. **更新必须携带 `expected_revision`**；缺失即报 `VALIDATION_ERROR`，杜绝静默覆盖；
2. `expected_revision` 与现值不符 → `REVISION_CONFLICT`（含 expected/actual），**不得覆盖手机侧编辑**；
3. `unique=(namespace, value)` 提供幂等唯一键；重复创建抛 `DUPLICATE_OBJECT` 并给出既有对象 ID；
4. `put` 修改传入对象（写回分配到的 id/revision/时间），并保存副本；`get`/`find` 返回副本；
5. 每次写入追加一条审计事件（`CREATE`/`UPDATE`/`TRANSITION`/…），审计只追加、不改写。

## 2. 对象清单

详见 `contracts/contracts-v0.3.json` 的 `objects` 段。按 SPEC §4.2 一一对应：

| 对象 | ID 前缀 | 关键语义 | SPEC |
|---|---|---|---|
| `SourceDocument` | `doc` | 原始文档/邮件/附件；原文不可被模型输出覆盖；带 `index_state`、`archive_state`、`identity_key` | §5.1 |
| `Person` / `Organization` / `ContactPoint` | `per` / `org` / `cp` | 联系人与组织；带置信度与"用户是否确认" | §4.2、§7.2 |
| `Conversation` | `conv` | 持久化会话 ID，≠ 进程/页面/模型窗口 | §21.1 |
| `Case` | `case` | 事务容器；业务状态与归档状态**独立** | §5.2 |
| `Task` | `task` | Case 内工作项；含重试/超时/前置条件 | §5.3 |
| `Fact` / `Inference` | `fact` / `inf` | 事实与推断必须分开；推断默认只进候选区 | §5.4、§21.5 |
| `Rule` / `Memory` | `rule` / `mem` | 长期资产；带范围、版本、确认者、废止/替代关系 | §5.8、§10 |
| `Workflow` / `WorkflowVersion` | `wf` / `wfv` | 可复用流程版本（步骤/追问点/审批点/归档要求） | §10.4 |
| `Draft` / `DraftVersion` | `draft` / `dv` | 草稿与不可变版本；审批绑定 `DraftVersion` | §5.5、§8.2 |
| `Question` / `Answer` | `q` / `ans` | 异步追问与回答 | §8.1 |
| `Approval` | `appr` | 绑定规范化动作摘要、版本、有效期、认证来源 | §5.6、§8.4 |
| `ExternalAction` / `ActionAttempt` | `act` / `att` | 外部副作用与每次尝试（含稳定幂等键） | §5.7、§21.2 |
| `ArchiveJob` / `ArchiveManifest` / `RestoreJob` | `arc` / `arcm` / `rstr` | 归档任务、不可变清单、恢复任务 | §12 |
| `AuditEvent` | `audit` | 只追加的审计事件（可作废/更正，不可覆盖） | §11.3 |
| `Execution` / `ToolCall` | `exec` / `tc` | 一次 Agent 执行与工具调用 | §4.2、§11.2 |
| `Event` | `evt` | 事件信封（event_id/source/source_event_id/时间/schema_version） | §21.1 |
| `FileArtifact` | `file` | 生成文件（身份/来源 Case/版本/校验/权限） | §9.3 |

## 3. 状态机

完整合法转换表见 `contracts/state-tables.md`（由 `transitions.MACHINES` 生成）。核心约束：

1. **非法转换必须拒绝并留原因**（`ILLEGAL_TRANSITION`，detail 带 allowed 列表）——MVP-0 验收项；
2. **Case 不是严格单一路径**：`READY_FOR_REVIEW → ANALYZING`（草稿编辑/追问回答）、
   `SUCCEEDED → ARCHIVING/ARCHIVE_PENDING`、`COMPLETED → ARCHIVE_PENDING`（新内容进下一快照）；
3. **取消/阻塞**只允许发生在"尚无不可逆外部动作"的阶段：存在 `IN_FLIGHT`/`SUCCEEDED`/`UNKNOWN`
   动作时，`CASE → CANCELLED|BLOCKED` 一律拒绝；
4. **动作状态表**（SPEC §21.2 逐字对应）：`PREPARED → AUTHORIZED → IN_FLIGHT → SUCCEEDED | FAILED_SAFE | UNKNOWN`；
   - `PREPARED → AUTHORIZED` 只允许 `USER`/`SYSTEM` 触发（**Agent 不得自我授权外呼**）；
   - `UNKNOWN` 只能由 `USER`/`SYSTEM` 核实后推进到 `SUCCEEDED`/`FAILED_SAFE`，**不得**回到 `AUTHORIZED`/`IN_FLIGHT`；
   - `SUCCEEDED`/`CANCELLED` 为终态；
5. **业务状态、审批状态、动作状态、归档状态各自独立**：归档失败不得让"已发送"显示为"未发送"，
   归档重试不得触发重发。
6. 规则/记忆/流程：`CANDIDATE → PENDING_CONFIRMATION → ENABLED → SUPERSEDED|REVOKED → ENABLED`（可恢复）。

## 4. 接口契约

### 4.1 存储 `asistent.contracts.store_api.StoreProtocol`

| 方法 | 语义 | 关键约束 |
|---|---|---|
| `put(obj, expected_revision=, unique=)` | 创建/更新 | CAS；幂等唯一键 |
| `get` / `find` / `find_one` | 读取 | 返回副本 |
| `transition(type, id, target, expected_revision=, trigger=)` | 状态转换 | 校验合法表 + 不可逆守卫 + 审计 |
| `set_index_state(document_id, target, ...)` | 索引状态 | 独立状态机，失败带原因 |
| `append_audit` / `audit_trail` | 审计 | 只追加 |
| `publish_event(event)` → `(event, created)` | 事件发布 | 按 `dedupe_key` 幂等；重复投递返回既有事件 |
| `claim_event` / `mark_event_processed` | 事件消费 | 独占领取，避免重复处理 |
| `approve` / `invalidate_approval` | 审批 | 审批必须带 `approved_action_digest` 与 `auth_evidence` |
| `claim_action(action_id, worker=)` → `ActionAttempt` | **原子**领取动作 | 单次调用完成"校验 + 预留一次性审批 + 写 IN_FLIGHT + 建 attempt"；禁止"先查询后标记" |
| `record_attempt(attempt_id, state=, ...)` | 记录尝试结果 | 结果分类必须落到动作状态表 |
| `demote_in_flight_to_unknown()` | 重启恢复 | `IN_FLIGHT` 先转 `UNKNOWN` 查证，**不按租约重发** |
| `recovery_scan()` | 恢复清单 | 未完成 Case / 待回答 / 待审批 / IN_FLIGHT / UNKNOWN / 待索引 / 待归档 / 失败恢复 |
| `stats()` | 观测 | 对象计数 |

### 4.2 执行后端 `asistent.contracts.backend.ExecutionBackend`

`name` + `execute(ExecutionRequest) -> ExecutionResult`。后端只能通过 `inputs` 拿到纯数据，
**不持有邮件/网盘凭据**；副作用必须走受控连接器（§21.3）。

- `EchoBackend`：确定性测试后端，保证离线可跑、可替换（AT-02）；
- `ScriptedBackend`：剧本/失败注入；
- `BudgetedBackend`：预算护栏，未配置预算时限制无人值守调用（§21.7）；
- `HttpChatBackend`：OpenAI 兼容 HTTP（标准库 urllib），凭据只从受保护配置读取；
- `DshBackendStub`：dsh 后端占位，未完成适配前明确返回"未交付"，不冒充能力。

### 4.3 邮件 `asistent.contracts.mail`

- `MailSource`：`list_folders()` / `list_new(cursor)` → `(refs, new_cursor)` / `fetch_raw(ref)`；
  游标是恢复点，游标失效必须能被检测与重建；
- `MailMessageRef.identity_key()`：账号作用域稳定身份 —— 供应商不可变 ID 优先，
  否则"邮箱范围 ID + 内容摘要"，`Message-ID` 只作弱身份且**绝不跨账号合并**；
- `MailSender.send(MailSendRequest)` → `MailSendResult(result_class ∈ SUCCEEDED|FAILED_SAFE|UNKNOWN)`；
  `MailSendRequest.idempotency_key` 由核心分配，模型不得自造；
- 随包提供 `FixtureMailSource`（读 `.eml` 夹具）与 `RecordingMailSender`（只写本地发件箱，绝不外发）。

### 4.4 归档通道 `asistent.contracts.pan`

- `PanClient`：`ensure_dir` / `put` / `get` / `stat` / `list` / `delete` / `quota`；
- `LocalDirPanClient`：本地目录模拟远端（离线闭环与契约测试用；**不构成真实网盘能力**）；
- `verify_readback(...)`：逐文件回读校验，返回 `VERIFIED | PENDING_VERIFICATION | PARTIAL`；
  拿不到可信哈希时必须停在 `PENDING_VERIFICATION`，**不得**宣称完整归档成功。

## 5. 幂等、审批与一次性发送

幂等键派生（`asistent.contracts.ids`）：

| 键 | 派生方式 | 用途 |
|---|---|---|
| 事件去重键 | `sha256("event", source, source_event_id)` | 重复投递不重复建对象 |
| 邮件身份 | 供应商 IM 优先；否则 `(范围 ID, 内容摘要)` | 同一邮件只有一份原始对象、一个默认 Case |
| 动作幂等键 | `sha256("action", action_id)` | 同一已批准动作的重试沿用同一键 |
| 归档 ID | `sha256("archive", scope, scope_ref, 快照摘要)` | 同 id 重试不覆盖已校验内容；内容变化→新 id |

审批绑定（`actions.ActionSummary.digest()`）：规范化 `kind/发件账号/To/Cc/Bcc/主题/正文/附件内容哈希/
目标线程/DraftVersion/关键依据版本/关键参数`。任何一项变化 → 摘要变化 → 旧审批必须失效
（`APPROVAL_INVALID`，detail 给出 `changed_fields`）。审批还必须带 `auth_evidence`（认证控制操作来源）：
**聊天文本里的"同意/发送"不构成审批**（§21.3）。

发送一次性保护（`claim_action` 一个调用内完成）：

```text
校验动作状态 == AUTHORIZED
校验未被其他 worker 占位（禁止 TOCTOU）
校验审批 ACTIVE + 未过期 + 摘要一致 + 未被预留
→ 原子预留并消费审批（consumed_by_attempt_id）
→ 动作转 IN_FLIGHT（claimed_by/claimed_at）
→ 建 ActionAttempt，request_idempotency_key = 动作幂等键
```

结果处理：

| 结果 | 动作状态 | 后续行为 |
|---|---|---|
| 有外部接受证据 | `SUCCEEDED` | 终态，禁止重发 |
| 有确切证据未发生副作用 | `FAILED_SAFE` | 可重新审批后重试（沿用同一幂等键） |
| 超时/连接中断/崩溃 | `UNKNOWN` | 只查询与人工裁决，**禁止自动重发**；重启发现 `IN_FLIGHT` 先转 `UNKNOWN` |

> 承诺的边界：**不因自身重复轮询、并发、重启或重试而再次主动提交同一不安全动作**，
> 不是在不可控邮件服务上宣称端到端 exactly-once（§21.2）。

## 6. 资料引用模型

`Citation` 必填：`source_object_id`、`source_object_type`、`path`、`locator`、`content_hash`、`supports`；
建议填 `source_time`、`source_version`、`used_by_execution_id`、`excerpt`。
`EvidenceSet` 明确标注 `contains_inference`，事实与推断不得混写（§5.4、§6.2）。

时间字段用 `TemporalField`：保留**原文标签**与语义槽位（`registration_deadline`、`event_start` 等），
`semantic="unknown"` 时不得给出归一化时间，必须追问。「明天」等相对日期按**原文发出时间及其时区**解释。

## 7. 归档 manifest 与恢复

`ArchivePolicy` 必填 `policy_id / source_paths 白名单 / target_account / target_root / 确认者`；
默认调度「Asia/Shanghai 03:00 每日」，单文件默认上限 20 MiB，软配额 80%，保留策略只列候选、
**不自动删除远端备份**。

`build_manifest(...)` 产出不可变清单：`archive_id`、逐文件 `sha256`/大小、来源版本、策略版本、
代码/契约/规则/流程版本、排除清单、秘密扫描结果、`manifest_digest`。

校验与恢复：
- `verify_remote(...)`：数量/大小一致**不算**通过，必须逐文件哈希；哈希不可得 → `PENDING_VERIFICATION`；
- 恢复必须先建 `RestoreJob`、恢复到隔离目录、默认只读、逐文件校验哈希、重建索引，
  **绝不重放归档中的发送/上传/审批**，恢复后审批默认失效；
- 命中疑似秘密 → `SecretDetected`，阻止归档并要求处理（不能只依赖扫描器证明"无敏感内容"）。

## 8. 错误与失败分类

`AsistentError` 体系，每个错误带稳定 `code` 与 `failure_class`（SPEC §13.1）：
`INVALID_INPUT`、`MISSING_DATA`、`RULE_CONFLICT`、`MODEL_FAILURE`、`TOOL_FAILURE`、`NETWORK_FAILURE`、
`PERMISSION_FAILURE`、`EXTERNAL_UNKNOWN`、`APPROVAL_EXPIRED`、`INDEX_FAILURE`、`ARCHIVE_FAILURE`、`SYSTEM_CRASH`。

关键错误：`VALIDATION_ERROR`、`REVISION_CONFLICT`、`ILLEGAL_TRANSITION`、`DUPLICATE_OBJECT`、
`NOT_FOUND`、`APPROVAL_INVALID`、`ACTION_NOT_CLAIMABLE`、`EXTERNAL_RESULT_UNKNOWN`、
`ARCHIVE_INTEGRITY_ERROR`、`SECRET_DETECTED`、`UNTRUSTED_CONTENT_VIOLATION`。

## 9. 契约测试 ↔ 验收映射

| 测试文件 | 覆盖 | 对应验收 |
|---|---|---|
| `tests/contracts/test_contract_surface.py` | 对象/前缀/状态机完备性、非法转换、快照可序列化 | MVP-0、AT-15 |
| `tests/contracts/test_storage_contract.py` | CAS 冲突、审计追加、事件去重、唯一键、并发只建一个 Case、不可逆守卫、恢复扫描；v0.3 增补：协议完备性、`limit` 显式语义、`get_by_unique` 返回 `None`、`now()` 格式、`put(audit_detail=)` 落审计 | MVP-0、AT-08、AT-15 |
| `tests/contracts/test_approval_action_contract.py` | 未批准不可执行、并发单赢家、审批随内容/收件人/附件/时间失效、UNKNOWN 不重发、FAILED_SAFE 重批重试同一幂等键 | MVP-5、AT-07 |
| `tests/contracts/test_archive_contract.py` | manifest 不可变、逐文件哈希、`PENDING_VERIFICATION`、秘密拦截、越限拒绝、隔离恢复 | MVP-7、AT-12/13/14 |
| `tests/contracts/test_connector_contracts.py` | 邮件游标幂等、身份账号作用域、发送幂等键拒绝重放、网盘回读与篡改检测 | MVP-2/5/7、AT-05 |
| `tests/contracts/test_semantics.py` | 时间字段语义分离、相对日期锚定、引用必填、权限裁决表、后端可替换 | MVP-3、AT-02/AT-06 |
| `tests/contracts/test_search_and_clock.py` | 中文逐字切分与短语查询、混合中英文、注入防护、纯符号查询无效、时钟回退拒写/前跳审计/单调钟 | MVP-1、MVP-0、AT-03 |

运行方式：

```bash
python3 -m unittest discover -s tests -t .          # 参考实现
ASISTENT_STORE=sqlite python3 -m unittest discover -s tests -t .   # MVP-0 的 SQLite 实现（同一组契约测试）
```

## 10. 检索与时钟可信度（v0.2 增补）

这两项来自设备实测暴露的**静默失效**风险（证据见 `docs/env/report.md` §B5、§B6），
契约变更提案：`CP-20261010-01`、`CP-20261010-02`。

### 10.1 中文检索必须走应用层切分（CP-20261010-01）

**问题**：SQLite FTS5 的 `unicode61` 把连续中文当成一个 token。设备实测
`MATCH '部署'` 返回 **0 行且不报错**，`'百度网盘'` 同样 0 行 —— 检索会静默失效，
MVP-1「60 秒内可检索」在中文语料上等于没实现。

**契约规定**（`asistent/contracts/search.py`）：

1. 入库前必须 `normalize_for_index()`：NFKC → 小写 → **CJK 逐字切分（含 CJK↔非 CJK 边界）**
   → 空白折叠；`部署方案` 入库为 `部 署 方 案`，`deploy部署` 入库为 `deploy 部 署`；
2. 查询必须 `to_fts_query()`：CJK 连续片段转成 FTS5 短语（`"部 署"`），非 CJK 片段按词加引号，
   片段之间 `AND`；
3. 分组语义保留：`部署方案` → `"部 署 方 案"`（四字相邻）；
   `部署 方案` → `"部 署" AND "方 案"`（各自相邻即可）；
4. 查询串**绝不能**原样拼进 `MATCH`：邮件正文与资料是不可信输入，
   必须逐个加引号（`"` 翻倍），避免语法破坏与注入；
5. 空串/纯符号 → `to_fts_query()` 返回 `None`，调用方必须当作**无效查询**，
   不得退化成"返回全部结果"；
6. FTS5 表 DDL 统一用 `search.FTS_DDL`；`fts5_available()` 探测不到 FTS5 时必须显式报错，
   不得静默降级成全表扫描；
7. 索引是**派生数据**：切分规则变化后必须重建索引（`IndexState=REBUILD_REQUIRED`）。

### 10.2 时钟不可信时禁止写业务时间戳（CP-20261010-02）

**问题**：设备实测本次开机时钟停在 `2024-07-08`，落后约 **2 年 3 个月**
（`systemd-time-wait-sync` 为 disabled）。开机窗口内写业务时间戳会污染审计因果顺序、
审批有效期判定、邮件 `SINCE` 同步窗口与归档时间范围。

**契约规定**（`asistent/contracts/clock.py`）：

1. 启动自检：`check_clock_sanity(now, reference)` 比较墙钟与**最后可信时间**；
2. **墙钟回退超过 300 秒 → `ok=False`**，`ClockGuard.require_sane()` 抛 `CLOCK_ANOMALY`
   （失败分类 `SYSTEM_CRASH`），服务进入**降级模式**并把原因暴露到手机端；
   宁可不写，也不要写错时间；
3. **墙钟前跳**（NTP 纠正大偏差）允许写入，但必须 `requires_audit=True` 并记审计——
   否则刚校时之后的正常时间也写不进去；
4. 首次启动无参照时间时允许写入，reason 记为 `NO_REFERENCE`；
5. **时长测量一律用单调钟**（`ClockGuard.monotonic_now()/elapsed_seconds()`），
   不用墙钟相减；
6. 降级与恢复都必须留审计，且恢复后审批仍按新时间重新判定有效期。

> 运维建议（需用户执行，见 `docs/env/report.md` §B6）：
> `sudo timedatectl set-ntp true` 并启用 `systemd-time-wait-sync`。

## 11. 存储接口 v0.3 增补（CP-20261010-03/04/05）

来源：MVP-0（SQLite 存储）独立复核时发现**冻结测试调用了协议里没登记的方法**——
这是真缺口：照协议实现的第三方必然漏方法。三项均为**纯增补**，不改变既有行为。

### 11.1 `get_by_unique` 登记进协议（CP-20261010-04）

冻结测试 `test_same_writer_twice_keeps_single_object` 直接调用 `store.get_by_unique(namespace, value)`，
但它没写进 `StoreProtocol`。

契约规定：

```python
def get_by_unique(self, namespace: str, value: str) -> Optional[SharedObject]: ...
```

- 语义 = `put(..., unique=(namespace, value))` 登记过的那把唯一键；
- **找不到返回 `None`**，不抛 `NotFound`：重复投递的处理要保持廉价，
  正常路径不该被写成异常路径（§7.3、§21.2）。

### 11.2 `now()` 与 `put(audit_detail=...)` 登记进协议（CP-20261010-05）

- `now() -> str`：UTC ISO-8601 毫秒 + `Z`。单独暴露是为了让时间可注入——
  时钟守门（`clock.ClockGuard`）与测试都要把"当前时间"当依赖，而不是到处直接调 `utcnow()`；
- `put(..., audit_detail: dict | None = None)`：调用方可以给本次写入的审计事件带结构化明细；
  **明细里不得放凭据**（§11.3 的审计是长期留存数据）。

### 11.3 `limit` 语义必须显式（CP-20261010-03）

原先只有"按 Python 切片"的隐含行为：`limit=-1` 会取到"除最后一条以外的全部"，
这种"看起来能用"的语义会在分页/巡检代码里静默出错。契约规定：

| limit | 语义 |
|---|---|
| `None` | 不限制 |
| `>= 0` | 最多返回 limit 条（`0` → 空列表） |
| `< 0` | 抛 `ValidationError`，**不允许**隐式从尾部取 |

适用范围：`find`、`audit_trail`、`list_events`（`audit_trail` 返回**最新** limit 条，
因为审计按时间正序存放）。

> 对应实现变更：参考实现 `reference.apply_limit()/apply_limit_tail()` 为唯一裁决点；
> MVP-0 的 SQLite 实现必须对齐（follow-up 任务 task-4）。

## 12. 契约 v0.4 增补（CP-20261010-06/07/08/09/10）

来源：MVP-1 与 MVP-2 两个原型在实现与验证过程中提出的 5 项提案。四项接受、一项拒绝，
拒绝项也留档（`contracts/CHANGELOG.md`）——"为什么不这么做"和"怎么做"同样重要。

### 12.1 `limit` 截取哪一端必须写死（CP-20261010-06）

`find`/`list_events` 会先按时间**升序**排序再取前 N 条，因此 `limit=N` 拿到的是**最早** N 条，
不是最近 N 条。这个语义之前没有被写下来，MVP-1 的幂等测试因此"假绿"（只数了前 100 条就以为全部一致）。
契约现在写死：

| 方法 | 排序 | 截取 | 含义 |
|---|---|---|---|
| `find` | `(created_at, id)` 升序 | 前 N 条 | 最早的 N 条 |
| `list_events` | `(received_at, id)` 升序 | 前 N 条 | **FIFO 队列**（最早的先处理） |
| `audit_trail` | 时间正序（追加序） | 后 N 条 | 最近的 N 条 |

调用方**不得**假设 `find` 返回"最近 N 条"；需要最近 N 条请显式排序或改用事件/审计视图。
对应契约测试：`StoreV04ContractTest` 的 `test_find_returns_the_earliest_n_not_the_latest`、
`test_list_events_is_fifo_and_takes_the_earliest_n`、`test_audit_trail_takes_the_latest_n`。

> 同时拒绝了一个"顺手改实现"的做法：MVP-1 直接把参考实现的 `find()` 深拷贝时机改了并声称修了 bug，
> 经 Lead 逐行比对是**等价的无操作改动**，理由与代码不符，已回退；语义问题用文档 + 契约测试固化。

### 12.2 `content_origin`：`content_hash` 到底哈希的是什么（CP-20261010-07）

有的通道（例如 mcp-email-server）**不暴露原始字节**，只能给出元数据投影。
此时 `content_hash` 不是"原始字节 sha256"，而是"投影摘要"。若不区分，
下游会把它当字节级完整性证明（归档回读校验正是这么用的），从而**看起来通过、实际没校验到字节**。

契约规定：

- `MailMessageRef.content_origin: str = "RAW_BYTES"`（取值 `RAW_BYTES` / `METADATA_PROJECTION`）；
- `SourceDocument.content_origin: Optional[str] = None`（`None` = 未声明）；
- `ArchiveEntry.content_origin: Optional[str] = None`：归档条目必须带来源类型；
- 判据为 `METADATA_PROJECTION` 或 `None` 时，**不得**把 `content_hash` 当字节级完整性证明，
  归档报告必须如实标注"只校验了投影摘要"。

### 12.3 `MailSource.cursor_position()` 是可选能力（CP-20261010-08）

实现可以提供 `cursor_position() -> Optional[dict]` 暴露同步位置供控制台观测；
调用方必须 `getattr(source, "cursor_position", None)` 探测，探测不到就降级为"不可观测"，
不得报错、也不得假装进度为 0。`MailSource` / `MailSender` 现在都是 `@runtime_checkable`，
便于能力探测。

### 12.4 失败**不**升为一等对象（CP-20261010-09，拒绝）

MVP-2 提议新增"Failure"对象。**拒绝**，理由：

1. 失败已经有三处承载：`failure_class`（对象字段）+ 状态机
   （`FAILED_SAFE` / `UNKNOWN`，见 §14.2：**没有** `FAILED_UNSAFE` 这个状态）
   + 只追加审计与事件。再加一个对象就是**第四份真相**，跨表一致性成本高、且容易互相矛盾；
2. "结果未知"这类最需要查证的失败，已经有 `ActionAttempt` 记录（含外部响应摘要），不缺载体；
3. 真正需要"失败列表"时，用**投影查询**（按 `failure_class` + 时间窗口查对象与审计）即可，
   不必把投影落成实体。

若将来出现"失败本身要被审批/被分配给人"的真实需求，再按提案流程重开。

### 12.5 幂等验收口径（CP-20261010-10）

"幂等"在本项目里的**唯一**口径是：

- **对象计数不变**（重复投递不产生第二个 `SourceDocument`、第二个 `Case`、第二个 `Conversation`）；
- **事件不重复处理**（`(source, source_event_id)` 去重键）；
- **允许新增审计行**：重复投递本身是客观发生的事实，审计只追加，必须留痕。
  因此"审计行数增加"**不是**幂等性失败，验收脚本不得把它当成回归。

对应契约测试：`test_duplicate_event_publish_keeps_object_counts`。

## 13. 契约 v0.5 增补（CP-20261010-11/12）

### 13.1 定序必须与写入顺序一致（CP-20261010-11）

**缺陷（Lead 在 main 上复现，sqlite 后端单类连跑 6 次红 5 次）**：
`created_at` 只有**毫秒**精度，快速连续写入的对象经常落在同一毫秒；
原先两个实现都用 `(created_at, id)` 排序，而 `id` 是随机/散列的 —— **平局判据是随机的**。
后果不止"测试偶发红"：分页会漏项或重复、`limit=1` 会随机取到不同对象、
同一份数据在参考实现与 SQLite 实现下给出**不同答案**。

> 教训：参考实现因为 Python 慢、写入常跨毫秒，**掩盖**了这个缺陷；
> SQLite 写入快，立刻暴露。所以契约不能只写"看起来对"的排序，必须写清平局怎么办。

契约规定：

| 项目 | 规则 |
|---|---|
| 定序键 | `(时间戳, 写入序号)`，**不得**用对象 id 当平局判据 |
| `find` | 按 `(created_at, 写入序号)` 升序，`limit=N` 取前 N 条（最早的 N 条） |
| `list_events` | 按 `(received_at, 写入序号)` 升序 = **FIFO** |
| 同一时间戳 | 顺序 = **首次写入顺序** |
| 稳定性 | 多次调用顺序一致；**跨进程重启后仍一致**（序号必须持久化） |
| 更新 | 不得改变对象已有的定序位置（改内容不等于改"排第几"） |
| `audit_trail` | 仍然是"最近 N 条"，且天然按追加序（`seq` 单调） |

对应契约测试（`StoreV04ContractTest`）：`test_ties_are_broken_by_write_order`、
`test_find_order_is_stable_across_calls`、`test_list_events_is_fifo_and_takes_the_earliest_n`。
测试**显式**把时间戳设成同一值来制造平局，而不是靠"快速连写碰巧同毫秒"——
后者会随实现快慢忽红忽绿，正是漏掉这个缺陷的原因。

### 13.2 `Event` 的访问路径（CP-20261010-12）

已确认并固化：`Event` **不**通过通用 `get("Event", id)` 访问，两个实现一致地抛 `NotFound`；
事件走事件接口：`publish_event` / `claim_event` / `mark_event_processed` / `list_events`。

理由：事件有独立的**原子领取**语义（`claim_event` 保证一个事件只被一个 worker 拿走），
如果把它混进通用 `get`/`put`，调用方就会绕过领取语义直接改事件，去重与"不重复处理"的保证会被悄然打破。

对应契约测试：`StoreV05ContractTest.test_event_is_not_reachable_through_generic_get`
（把"两边一致抛 NotFound"钉住，避免后人"顺手补一下"造成后端分叉）。

## 14. 契约 v0.6 增补（CP-20261010-13/14/15/16 + CP-17 拒绝）

### 14.1 `changed_fields` 必须真的说得出"哪里变了"（CP-20261010-13）

`check_approval_valid()` 在摘要不一致时，旧实现写的是：

```python
changed = current_summary.diff_keys(current_summary)   # 拿自己和自己比 → 恒为 []
```

后果不是"少一个字段"，而是**把结论说反**：调用方（控制台、审计、MVP-5 的失效解释）
读到 `changed_fields == []`，会理解为"摘要虽然不一致，但没有字段变化"，而真实情况恰恰是
内容变了。这类"字段永远为空"的缺陷不会自己暴露，只会让上层开始各自绕路
（MVP-5 确实被迫自己用 `Approval.summary_shown` 还原获批摘要）。

v0.6 起签名改为：

```python
check_approval_valid(
    *, approval_state, approved_digest, current_summary, expires_at,
    now=None, revoked_reason=None,
    approved_summary: Optional[ActionSummary] = None,   # 新增
) -> None
```

- 传入 `approved_summary` → `detail["changed_fields"]` 是**真实**变化字段列表；
- 不传 → `detail["changed_fields"] is None`，**显式表示"未知"**；
- 永不使用空列表表示"未知"（空列表只能表示"确实没有字段变化"）。

对应契约测试：`StoreV06ContractTest.test_changed_fields_are_reported_when_approved_summary_is_given`、
`..._is_none_not_empty_list_when_summary_missing`。

### 14.2 `ActionState` 里没有 `FAILED_UNSAFE`（CP-20261010-14，文档缺陷）

本文档旧版散文（原 §8 附近）把状态机写成 `FAILED_SAFE`/`FAILED_UNSAFE`/`UNKNOWN`，
但 `ActionState` 枚举实际是
`PREPARED / AUTHORIZED / IN_FLIGHT / SUCCEEDED / FAILED_SAFE / UNKNOWN / CANCELLED`。
"结果不明"由 `UNKNOWN` 承载，"为什么失败"由 `FailureClass` 承载，不存在 `FAILED_UNSAFE` 状态。
本次按枚举修正措辞；**不改枚举**（改枚举会影响状态机与迁移，收益不足以承担风险）。

### 14.3 同一动作至多一个 ACTIVE 审批（CP-20261010-15）

`store.approve()` 旧行为允许同一 `action_id` 上并存多条 ACTIVE 审批：

```
同一动作的 ACTIVE 审批数 = 2 ['appr_409749…', 'appr_616ef7…']
```

执行侧的一次性执行权（`claim_action`）确实兜住了"只会执行一次"，但**授权来源变得有歧义**：
审计上"这条动作由哪张审批授权"不再唯一，而且留下无意义记录。

v0.6 起：`approve()` 必须在**同一事务内**把同 `action_id` 上其它 ACTIVE 审批置为 `INVALIDATED`
（原因写明"被新审批取代"，记录与原因保留不删）。已消费（`CONSUMED`）的旧审批**不受影响**——
重试路径（FAILED_SAFE → 重新批准 → 新 attempt 沿用同一幂等键）必须继续可用。

"`claim_action` 已经兜住了"**不构成**允许重复批准的理由：两道保护解决的是不同问题
（一次执行 vs 授权唯一），不能用一道去替代另一道。

对应契约测试：`StoreV06ContractTest.test_second_approval_supersedes_the_first_active_one`、
`test_consumed_approval_does_not_block_reapproval`、`test_approval_of_another_action_is_untouched`。

### 14.4 审批默认有效期是契约的一部分（CP-20261010-16）

两个实现都在 `approve()` 里静默写 `expires_at = created_at_approval + 24h`，签名看不出 TTL。
v0.6 起常量上移到契约：`asistent.contracts.actions.DEFAULT_APPROVAL_TTL_SECONDS = 24 * 3600`，
并导出到包根；显式传入 `expires_at` 时以传入值为准。

对应契约测试：`test_default_ttl_comes_from_contract_constant`、`test_explicit_expiry_is_respected`。

### 14.5 冲突**不**升为独立共享对象（CP-20261010-17，拒绝）

MVP-3 提出：没有独立的 `Conflict` 共享对象，冲突只能写进 `Case.blocked_reason` +
`Question(kind="conflict")`，无法 `find()` 查询。

裁决：**拒绝新增对象**。理由与 CP-09（失败不升一等对象）同源：

- 冲突是"两个来源给出不同值"的**结论**，不是需要独立生命周期、独立状态机、独立权限的动作主体；
- 现有表示已经**可查询**：`find("Question", where={"case_id": ...})` 能列出冲突型追问，
  `Case.blocked_reason` 说明为什么停下来；
- 新增对象会带来三份成本：模式迁移、`find`/审计/控制台/归档 manifest 全链路支持、
  以及"冲突对象与 Question 谁是真源"的新分歧点。

约定（写进契约，供 MVP-4 控制台与后续实现遵循）：
**冲突 = `Question(kind="conflict")` + `Case.blocked_reason`，且冲突未解决前不得产出可发送结论。**

## 15. 契约 v0.7 增补（CP-20261010-18..23，源自 MVP-7 落地）

### 15.1 `content_origin` 的取值必须由契约给出（CP-20261010-18，真缺陷）

§12.2 规定了"`content_origin` 声明 `content_hash` 到底哈希的是什么"，但**没有给出规范取值**。
于是两个模块各自定义了一份字面量：

- `asistent/mail/mcp_stdio.py`：`CONTENT_ORIGIN_PROJECTION = "METADATA_PROJECTION"`
- `asistent/archive/manifest.py`：`CONTENT_ORIGIN_RAW_BYTES` / `CONTENT_ORIGIN_METADATA_PROJECTION`

后果不是"重复代码"，而是**判据静默错配**：任何一处拼写/大小写漂移，都会让归档侧把
"投影"认成"未声明"（保守，尚可）或把某个值认成字节级（**危险**，正是 §12.2 要禁止的事）。
现在取值由契约定义：`objects.CONTENT_ORIGIN_RAW_BYTES` / `CONTENT_ORIGIN_METADATA_PROJECTION`
/ `VALID_CONTENT_ORIGINS`，模块一律引用；并有跨模块一致性测试钉住
（`ContractV07Test.test_modules_do_not_redefine_content_origin_literals`）。

### 15.2 "回读一致"证明了什么，必须用取值表达（CP-20261010-21）

§12.2 只说了"不得把投影当字节级证明"，没说**该怎么分类**。现在契约给出规则：

```python
integrity_level(content_origin) -> "BYTE_LEVEL" | "PROJECTION_ONLY" | "UNDECLARED"
```

`RAW_BYTES → BYTE_LEVEL`；`METADATA_PROJECTION → PROJECTION_ONLY`；
**未声明/未知 → UNDECLARED，按投影级对待**（宁可保守，也不要"看起来通过"）。
措辞与报告形状仍由调用方负责（归档报告、恢复记录、控制台各自表述），但**分类规则唯一**。

### 15.3 `NOT_AUTHORIZED` 登记为契约错误码（CP-20261010-19）

MVP-7 实测：bdpan 未登录时需要一个稳定码，而契约只有 `PERMISSION_DENIED` / `APPROVAL_REQUIRED`。
两者都是"现在不能做"，但**下一步动作完全不同**，混用会误导用户与重试逻辑：

| 码 | 含义 | 下一步 | 可重试 |
|---|---|---|---|
| `APPROVAL_REQUIRED` | 权限在，审批没给 | 用户在本系统内批准，取审批引用 | 批准后可以 |
| `NOT_AUTHORIZED`（新） | 凭据不在 / 过期 / 被撤销 | 用户去通道那边**重新授权** | **否**（重试无意义） |

未授权最容易被错误归因：当成网络抖动 → 无限重试；当成审批缺失 → 提示"请批准"而用户批了也没用。
`NotAuthorized` 自带 `detail = {"retryable": False, "needs_reauth": True}`，
归档通道（`PanNotAuthorized`）与邮件通道（授权码失效）共用这一类。

### 15.4 `PanClient.put()` 没有"不覆盖"前置条件（CP-20261010-20，文档化）

`PanClient.put()` 无法表达"仅当远端不存在时写入"（没有 precondition / if-none-match），
真实通道上存在 TOCTOU 窗口。**本次不改签名**——通道能力尚未验证（bdpan 未授权），
加一个后端支持不了的前置条件等于制造**假能力**，比没有更危险。

因此契约明确补偿控制（调用方必须做）：
**写完必须回读校验；发现远端内容与本次写入不一致（被并发改写）→ 抛 `ArchiveIntegrityError` 拒绝覆盖语义，
绝不把"我写的那份"当成最终状态。**

### 15.5 恢复的三条安全属性落成字段（CP-20261010-22）

`RestoreJob` 原有 `read_only_mode`，另两条只写在散文/`note` 里。现在补齐为可机检字段，
默认值即安全值：`approvals_invalidated = True`、`replay_forbidden = True`。
恢复记录因此能**独立证明**"确实只读、确实使审批失效、确实没有重放归档里的外发动作"，
而不依赖某段文字是否被照抄。

### 15.6 SPEC 的 `ARCHIVE_PENDING` 与契约状态枚举的映射（CP-20261010-23，文档化）

SPEC §16 MVP-7 写"Case 为 `ARCHIVE_PENDING`"，而 `ArchiveState` 枚举里没有这个值
（实际为 `PENDING / UPLOADING / PARTIAL / VERIFYING / PENDING_VERIFICATION / VERIFIED / FAILED`）。
**裁决：不改枚举，改映射口径。** SPEC 那句话的**可观察语义**是"归档没做完、能看到哪些成功哪些没成、可以重试"，
对应契约的 `PARTIAL`（部分对象未完成）+ `ArchiveJob.pending` 非空。

理由：枚举是机器判据，多一个语义重叠的状态会立刻产生"什么时候是 PENDING、
什么时候是 ARCHIVE_PENDING"的分歧；而 SPEC 那句话要表达的是**用户可见状态**，用
"状态 + 待办集合"表达更准确。文档化映射即可。

## 16. 契约 v0.8 增补（CP-20261010-24..31，源自 MVP-4/MVP-6 落地）

这一轮的共性是：**契约缺一个字段/一条转换/一道门闸，于是各层只能各自绕，绕法还不一致**。
其中三条在落地时被证明是"会卡住用户"的真缺陷（CP-25/28/29），两条是跨层不变量缺口（CP-27）。

### 16.1 追问答完又发现缺口：`WAITING_FOR_USER → WAITING_FOR_CONTEXT`（CP-24）

用户的回答**可能暴露新的上下文缺口**（问"活动在哪个城市"，回答里又出现一个没见过的截止时间）。
原状态机不允许这条转换，控制台只能让状态原地不动、只改 `next_action`
——手机端于是显示"状态是旧的、下一步动作是新的"，自相矛盾。

不接受"再加一个状态"的方案：`WAITING_FOR_USER`/`WAITING_FOR_CONTEXT` 的区别是**在等谁**，
不是流程阶段；补一条转换即可，且不改变其它任何转换。

### 16.2 追问类别必须是数据：`Question.kind`（CP-25，真缺陷）

§14.5 明文写 `Question(kind="conflict")`，但 `Question` 对象里**根本没有 `kind` 字段**，
而理解层的 `QuestionCandidate.validate()` 又强制 `kind` 非空。结果：

- 类别只用来派生对象 ID 与写报告 detail，**落库时被丢掉**；
- 下游（控制台分组、成长层的冲突门闸）只能**读 `background` 的文字来猜**类别。

现在 `Question.kind` 是正式字段（`ambiguity` / `conflict` / `missing_material` / `missing_time` /
`missing_recipient`；空串 = 未分类的历史对象），理解层落库时必须写。**契约自己的文字必须能实现。**

### 16.3 记忆有效期是数据：`Memory.valid_until`（CP-26）

SPEC 要求"记忆有有效期"，而 `Memory` 没有对应字段，落地时只能旁挂一个自造对象来表达失效时间
——任何别的模块都无法在不了解该约定的情况下判断"这条记忆现在还能不能用"。

现在 `valid_until`（ISO 字符串，`None` = 长期有效）是字段。过期的**效果**复用
`state=REVOKED`（"是过期还是用户撤销"由 `links`/原因区分）：为同一效果再造一个状态，
会让"还能不能用"出现两种判据。

### 16.4 阻塞的事务不得被批准：跨层门闸（CP-27）

成长层在规则冲突时**不产出可发送结论**、把原因写进 `Case.blocked_reason`、并让旧草稿
`OPEN → STALE`。但 MVP-5 的审批路径**完全不读这个字段**，实测存在这条绕过路径：

> 先用"没有规则"的状态生成一份可发送草稿 → 再制造冲突（Case 阻塞、草稿 STALE）→
> 直接拿旧草稿调审批 → **批准通过**。

因此契约规定：**任何生成审批的入口必须先检查目标 Case 的 `blocked_reason`，非空即拒绝**
（`PermissionDenied`，detail 带 `case_id` / `case_state` / `blocked_reason`）。
`case_id` 缺失时从 `DraftVersion → Draft.case_id` 兜底解析；两边都没有才不拦。

为什么必须在**审批**这一层拦：审批是"人已经同意发出去"的唯一入口。只要还有一种调用顺序能走到
审批，"不产出可发送结论"就只是约定，不是不变量。**任何"更早一层已经拦住了"的说法都不能替代这一层。**

### 16.5 资料与事务的关联必须双向完整（CP-28，真缺陷）

落地时发现两件事：

1. `SourceDocument.case_id` 在 `process_document` 之后**一直是空的**——关联只写在
   `Case.source_document_ids` 里。任何按 `case_id` 过滤文档的下游（控制台/归档范围/检索过滤）
   会**静默**拿到空集（不报错，比报错更危险）；
2. `_ensure_case` 只在**新建**事务时写 `source_document_ids`，同一个事务里处理第二份资料
   （正文 + 附件、或调用方预先指定了 `document.case_id`）时直接返回，新资料**挂不上去**
   ——多资料事务少报成员。

现在两者都必须补齐，且**幂等**：权威仍是 `Case.source_document_ids`（一份资料可以属于多个事务），
`SourceDocument.case_id` 是"主事务"的冗余字段，用于单跳查询。

### 16.6 阻塞原因必须随事实更新，包括**清除**（CP-29，真缺陷）

`Case.blocked_reason` 原先写在"等待原因/下一步是否变化"这个**不相关**的条件分支里。后果：
冲突被裁决（规则废止、或新资料不再冲突）之后重新理解同一个事务时，只要"等待原因"恰好没变，
`blocked_reason` 就**留着不清** → 事务永久卡在阻塞态；与 CP-27 的审批门闸叠加就是**死锁**
（既发不出去，也批准不了）。

修正后的判据：`blocked_reason` 由本次理解结果**独立决定**（有冲突 → 写原因；无冲突 → 清空），
与"等待原因是否变化"解耦，两者任一变化都落库。

### 16.7 审批终态不可复用：重新批准必须新建对象（CP-30，文档化）

`ApprovalState` 的 `CONSUMED`/`INVALIDATED`/`REVOKED`/`EXPIRED` 都是终态，**不能复活**。
用户改了草稿再批准，就是**新的 `Approval` 对象**，旧的留在历史里（`Case.approval_ids` 只增不减）。
理由：审批是"对某个确定内容版本的同意"，把旧对象改成 ACTIVE 会让"批的是哪一版"不可追溯。

### 16.8 授权唯一性只作用于绑定动作的审批（CP-31，文档化）

§14.2（v0.6，CP-15）的"同一动作至多一个 ACTIVE 审批"只对**声明了 `action_id`** 的审批生效。
`action_id` 为空的审批（没有绑定具体动作，例如对"是否继续处理"这类流程性问题的一次性授权）
不参与唯一性判定——它们授权的是判断，不是某个具体外发。

## 17. 未纳入 v0.8 的部分

- 真实 `mcp-email-server` 通道的启动参数与工具映射细节（MVP-2 交付，作为 `MailSource/MailSender` 实现）；
- 真实 `baidu-drive` 通道的认证与调用细节（MVP-7 交付，作为 `PanClient` 实现）；
- 模型路由、预算与限流参数（ADR 待补）；
- 索引实现细节（FTS5 中文分词策略，MVP-1 记录）。

## 18. 契约 v0.9 增补（CP-20261010-32 接受；CP-33/37 接受并实现；CP-34 退回；CP-35/36 推迟）

v0.9 只有一个**契约对象层面**的变更（§18.1），其余是服务层行为与验收判据。

### 18.1 邮件头字段的取值类型是 `str`，且必须能解码（CP-32）

`MailMessageRef` 的 `subject` / `from_addr` / `to_addrs` / `received_at` 在契约里声明为字符串，
因此**通道实现不得把解析器的中间对象直接交出去**：`email.message_from_bytes()` 对含 RFC 2047
编码字（`=?utf-8?B?...?=`）的头会返回 `email.header.Header` 而不是 `str`，
把这种对象放进 `MailMessageRef` 会让下游（存身份、写标题、做线程关联）在**任何一次字符串操作**上崩。

冻结的规矩：

1. 头字段一律解码成字符串；解码失败的**兜底顺序**是
   "字节本身是合法 UTF-8 且声明不可信 → UTF-8；否则 声明 → UTF-8 → GB18030"，
   并跳过解出 C1 控制字符（U+0080–U+009F）的结果；最后兜底 `utf-8 + replace`。
   **不允许抛异常**：收信链路不能因为一封格式不规范的老邮件而整条停摆。
2. 地址类字段用 `email.utils.getaddresses()` 解析，**不得裸切逗号**——
   `"Doe, John" <j@example.com>` 裸切会切出一个不存在的地址，而收件人参与审批判断。
3. 离线夹具源在**夹具目录不存在**时抛 `ValidationError`；"目录不存在"与"今天没信"
   是两件必须能区分的事。
4. 游标损坏（非数字）时按"从未读过"处理：**宁可重读，不可漏读**
   （下游按身份去重，重读只多花时间；漏读是永久丢信）。
5. `content_digest` 仍按原始字节计算，身份键不变——因此**不需要数据迁移**。

对应测试：`tests/contracts/test_mail_headers.py`（16 项，含"整个夹具目录必须读得出来"）。

### 18.2 "已回答的追问不再阻塞"只认结构化键（CP-33，服务层）

阻塞解除必须依据 `(问题文本, affects 槽位)` 这一**结构化键**，不得退化成"只看文本"。
理由是不对称性：漏配对的代价是"追问继续挡着"（可见、可处理），
错配对的代价是"不该放行的草稿被放行"（静默、有外部后果）。
匹配不上时**不解除阻塞**，但必须写入说明与 `REJECTED` 审计——
静默不生效和答错对象一样糟。

### 18.3 长时间浸泡的验收判据（CP-37，预先冻结）

判据与阈值见 `contracts/CHANGELOG.md` 的 CP-20261010-37 一节；实现为
`asistent/service/soak.py` 的 `SOAK_CRITERIA` 与 `--verdict`。
两条口径同样属于判据本身：

- 取样点少于 2 个 → 输出"证据不足"，**不得判通过**；
- 跨度不足 23.5 小时 → 显式提醒"不能当作 AT-01 的 24h 结论"。

**为什么写进契约**：判据若在曲线拿到之后才定，等于事后挑一个刚好能过的阈值。
先冻结、后执行，才叫验收。

### 18.4 本版未采纳／推迟

- **CP-34 退回**：原提案称"`Event` 表实际上是空的"，实测 `events` = 9 行
  （`audit_events` = 192 行），前提不成立。退回重写为"哪些动作该产生 `Event`"。
- **CP-35 推迟到 v0.10**：服务自检快照目前借用 `FileArtifact`（`case_id=""`，现存 1 行）；
  新增专属只读对象属于契约变更，需与本次冻结分开。
- **CP-36 推迟**：档案归档是否携带 `ArchivePolicy`、以及归档写入是否需单独审批，
  取决于用户对数据边界的确认（U-6），实现方不得代填。
