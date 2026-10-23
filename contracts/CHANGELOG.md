# 契约变更记录（SPEC §19、§21.1）

契约冻结后，任何对象字段、状态、接口签名、幂等/审批语义、归档格式的变更都必须在此登记，
并由主 Agent 裁决后发布新版本。未登记的变更视为违规。

## 变更提案模板

```text
提案编号：CP-YYYYMMDD-NN
提议者：
变更原因：
受影响的需求 ID / 对象 / 接口 / 状态 / 测试：
是否影响历史事务：
是否需要数据迁移或回滚：
是否改变用户审批边界：
需要用户确认吗：
替代方案：
主 Agent 裁决：
```

---

## 变更提案（提案 → 主 Agent 裁决 → 发布）

## contracts-v0.9（冻结，2026-10-12）

冻结范围：v0.8 之后 `main` 上的全部契约变更。**本版只有一个契约变更（CP-32）**，
其余提案一并裁决但**不改变冻结对象**（见下）。快照：`contracts/contracts-v0.9.json`；
正文追加见 `docs/contracts/CONTRACT.md` §17。

### CP-20261010-32：邮件头字段必须解码成字符串（**接受并实现**）

- 提议者：`mvp8-service`（实现期发现）
- 变更原因：`FixtureMailSource._ref_for()` 用 `message.get("To").split(",")` 取头。
  `email.message_from_bytes()` 对含 RFC 2047 编码字的头**返回 `Header` 对象而不是 `str`**，
  于是"读一封收件人名字是中文的邮件"直接抛
  `AttributeError: 'Header' object has no attribute 'split'`；修掉之后立刻暴露第二层
  `LookupError: unknown encoding: unknown-8bit`。
- **实测影响面**：仓库自带夹具 `fixtures/public/understand/*.eml` **6 个文件 6 个全失败**，
  即契约自带的离线邮件源**读不了自己的夹具**（2026-10-12，本机与设备各复现一次）。
- 受影响的对象/接口：`MailMessageRef.subject` / `from_addr` / `to_addrs` / `received_at`
  的**取值类型**（此前在编码字场景下可能是 `Header` 对象，不是契约声明的 `str`）。
- 契约新增（`asistent/contracts/mail.py`）：
  * `_header_text(value) -> str`：`None` / `str` / `Header` 统一成解码后的字符串；
  * `_decode_bytes(piece, charset) -> str`：声明的字符集不可用（不认识的名字 / 单字节集）时
    仍须给出可用结果，顺序为"字节本身是合法 UTF-8 且声明不可信 → UTF-8，
    否则 声明 → UTF-8 → GB18030"，并跳过解出 C1 控制字符（U+0080–U+009F）的结果；
  * `_address_list(value) -> List[str]`：用 `email.utils.getaddresses()` 解析，
    **不再裸切逗号**（`"Doe, John" <j@e.com>` 裸切会切出不存在的地址，而收件人进审批判断）；
  * `FixtureMailSource._paths()` 在**夹具目录不存在**时抛 `ValidationError`
    （"目录不存在"与"今天没信"是两件事，静默返回空会把配置错误伪装成正常）；
  * `FixtureMailSource.list_new()` 在**游标损坏**时按 0 处理（宁可重读，不可漏读：
    下游按身份去重，重读只多花时间，漏读是永久丢信）。
- 是否影响历史事务：**否**。摘要仍按原始字节算（`content_digest`），身份键不变，故无需迁移。
- 是否需要数据迁移或回滚：**否**。
- 是否改变用户审批边界：否。
- 需要用户确认吗：否（纯正确性缺陷）。
- 替代方案：在服务层另写一个"头字段统一解码"的邮件源绕过去（**已在实现期用过，现删除**）。
  不采纳：同一个缺陷不该有两个实现——谁用 `FixtureMailSource` 都会撞上同一堵墙，
  且两边行为会逐渐分叉。
- 主 Agent 裁决：**接受**。测试：`tests/contracts/test_mail_headers.py`（+16 项），
  contracts 125 → 141；两个后端逐包复跑 815 项 0 失败（本轮另加 13+8 项取样用例后为 828+）。

### CP-20261010-33：删除"只按问题文本"的配对退路（**接受并实现**）

- 变更原因：`asistent/service/answers.py::_match()` 在结构化键 `(问题文本, affects 槽位)`
  未命中时，会退化成"只看问题文本"。**这会猜，而且猜错的方向最危险**：同一句追问可以针对
  不同对象（两个草稿都可能问"活动开始时间？"），错配上会把"对 A 的回答"当成"B 已回答"，
  而 `Question(state=ANSWERED)` 一旦成立 B 就不再是阻塞项——**一个真实缺口静默消失**。
- 该退路存在的理由（"`affects` 在旧数据里可能为空"）**经实测不成立**：
  设备 `~/asistent-var/data/asistent.db` 中 `Question` 对象 **0 个**（2026-10-12）。
- 本模块自己的文档早已写着"比对 question 文本 + affects，**不是猜**"——代码此前不满足那句话。
- 实现：删除退路；`_match` 只认结构化键；新增 `_near_miss`，
  当"同文本、不同槽位"时**不解除阻塞**，但写入说明与 `result="REJECTED"` 的审计
  （静默不生效和答错对象一样糟）；`analyze` 即使一条都没解除也返回带说明的结果。
- 受影响的对象/接口：无（服务层行为，不改冻结对象）。
- 是否影响历史事务：否（历史 `Question` 的 `affects` 已在库中）。
- 主 Agent 裁决：**接受**。测试：`tests/service/test_service_unit.py` +5 项
  （含"同文本不同槽位必须不解除阻塞"，该用例在修复前是**红的**）。
  失败方向的不对称性写进用例注释：漏配对的代价是"追问继续挡着"（可见、可处理），
  错配对的代价是"不该放行的草稿被放行"（静默、有外部后果）。

### CP-20261010-34：`Event` 账本归属（**前提未复现，退回重写**）

- 原提案称"`Event` 表实际上是空的"。
- **实测（2026-10-12，设备库）**：`events` = 9 行，`audit_events` = 192 行。
  即 `events` 非空，原前提不成立。
- 裁决：**不按原表述裁决**。可观察的真实现象是"`events` 行数远少于 `audit_events`"，
  需要先把"哪些动作该产生 `Event`、哪些只该进审计"写清楚再提；退回提议者重写。

### CP-20261010-35：契约级"服务自检"只读对象（**接受，实现推迟到 v0.10**）

- 现象实测确认：`FileArtifact` 仅 1 行，即 `asistent/service/status_artifact.py`
  借用 `FileArtifact`（`case_id=""`）承载服务健康快照。
- 裁决：**接受**该提案的必要性——服务自检不是"某个 Case 的产出物"，
  借用会把"资产归属"语义污染成""。但新增对象类型属于契约变更，
  与本次冻结（只含 CP-32）分开，排入 v0.10；届时迁移成本极低（现存 1 行）。

### CP-20261010-36：归档携带 `ArchivePolicy`（**推迟，等你确认 U-6**）

- 现状：归档清单里原样记录 `policy_gate="U-6 未确认…"`，`policy: null`，不假装有策略。
- 裁决：**推迟**。策略内容（源白名单/目标根目录/敏感排除/调度/体积上限/保留）是
  用户的数据边界决定，不能由实现方代填；同时"归档写入是否需要单独审批"必须与策略一起定，
  否则会绕过审批边界。

### CP-20261010-37：长时间浸泡的验收判据（**接受并实现，判据预先冻结**）

- 为什么判据必须先冻结：**曲线拿到手之后再定标准，人会不自觉地挑一个刚好能过的阈值**——
  那不是验收，是事后解释。因此下列阈值在 24h 窗口结束**之前**写死并由代码机械判定。
- 取样：`asistent/service/soak.py`，`asistent-soak.timer` 每 5 分钟一行
  （只读：不重启服务、不写业务数据，只追 `logs/soak.jsonl`，0600）。
- 每行记录：`rss_kb` / `fds` / `threads` / `wal_bytes` / `db_bytes` / `log_bytes` /
  `rounds_completed` / `rounds_failed` / `listening_console` / `disk_free_bytes`，
  以及**运行身份** `run_id` / `service_started_at` / `round_index` / `repo_head`。
- 判据（`SOAK_CRITERIA`，`--verdict` 机械执行）：
  | 判据 | 阈值 | 为什么 |
  |---|---|---|
  | RSS 增长 | ≤ 25% | 持续上涨即问题；25% 容 GC/缓存预热 |
  | 句柄增长 | ≤ +10 | 句柄是泄漏最直接的证据 |
  | 轮次失败增加 | ≤ 3 | 24h ≈ 720 轮；容忍偶发网络抖动 |
  | WAL 峰值 | ≤ 512 MB | 不 checkpoint 会越跑越慢 |
  | 服务日志峰值 | ≤ 64 MB | 无轮转会吃满磁盘 |
  | 磁盘可用 | ≥ 2 GB | 低于此不再是"稳定" |
  | **`run_id` 唯一** | 必须 | `rounds_completed` 是跨进程累计值，**重启后照样上涨**，证明不了连续 |
  | 控制台端口 | 全程听得到 | 端口掉了手机连不上，而进程还"活着" |
- 另两条口径：取样点 < 2 时输出"证据不足"，**不判通过**；跨度 < 23.5h 时显式提醒
  "不能当作 AT-01 的 24h 结论"。
- 裁决：**接受并实现**。测试：`tests/service/test_service_soak.py` 21 项。

---

### CP-20261010-01：中文检索必须走应用层切分

- 提议者：主 Agent（依据 `env-probe` 设备实测，`docs/env/report.md` §B5）
- 变更原因：FTS5 `unicode61` 把连续中文当成一个 token，`MATCH '部署'` 命中 0 行**且不报错**，
  MVP-1「60 秒内可检索」在中文语料上会静默失效。
- 受影响：新增 `asistent/contracts/search.py`；新增 `FTS_DDL` 约定；新增测试
  `tests/contracts/test_search_and_clock.py`；MVP-1 索引入库/查询必须调用本层。
- 是否影响历史事务：否（纯新增，索引是派生数据，重建即可）。
- 是否需要迁移：是 —— 已建索引需按新切分规则重建（`IndexState=REBUILD_REQUIRED`）。
- 是否改变用户审批边界：否。
- 替代方案：仅用 trigram（≥3 字查询可用，2 字中文查询仍失效）、外部分词库（引入依赖，违反零依赖约束）。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.2`。

### CP-20261010-02：时钟不可信时禁止写业务时间戳

- 提议者：主 Agent（依据设备实测，`docs/env/report.md` §B6）
- 变更原因：设备本次开机时钟停在 `2024-07-08`，落后约 2 年 3 个月，
  `systemd-time-wait-sync` 为 disabled；开机窗口写业务时间戳会污染审计因果、审批有效期、
  邮件 `SINCE` 窗口与归档时间范围。
- 受影响：新增 `asistent/contracts/clock.py`、`errors.ClockAnomaly`；新增测试同上。
- 是否影响历史事务：否（新增守门人，不改变已有对象字段）。
- 是否需要迁移：否。
- 是否改变用户审批边界：间接——审批有效期判定改用可信时间，避免"过期审批被当有效"。
- 替代方案：只记录告警不拒写（会让错误时间进入审计与审批判定，否决）。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.2`。

### CP-20261010-03：`limit` 语义必须显式

- 提议者：MVP-0（SQLite 存储原型，独立复核冻结契约时提出）
- 变更原因：原先只有"按 Python 切片"的隐含行为，`limit=-1` 会取到"除最后一条以外的全部"，
  分页/巡检代码会静默出错。
- 受影响：`StoreProtocol.find/audit_trail/list_events` 的 `limit` 收 `Optional[int]`；
  参考实现新增 `apply_limit()`/`apply_limit_tail()` 作为唯一裁决点；新增 4 项契约测试。
- 是否影响历史事务：否。是否需要迁移：否。是否改变用户审批边界：否。
- 替代方案：保持隐式切片语义（否决：静默错误比显式报错昂贵得多）。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.3`；SQLite 实现对齐见 follow-up task-4。

### CP-20261010-04：`get_by_unique` 登记进协议

- 提议者：MVP-0
- 变更原因：**冻结测试调用了协议里没登记的方法**——照协议实现的第三方必然漏方法，是真缺口。
- 受影响：`StoreProtocol` 新增 `get_by_unique(namespace, value) -> Optional[SharedObject]`（找不到返回 `None`）；
  新增协议完备性测试。
- 是否影响历史事务：否。是否需要迁移：否。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.3`。

### CP-20261010-05：`now()` 与 `put(audit_detail=)` 登记进协议

- 提议者：MVP-0
- 变更原因：两者都是参考实现已有、且被调用方使用的公共能力，却不在协议里；
  时间不可注入会让时钟守门与测试无法正常工作。
- 受影响：`StoreProtocol` 新增 `now() -> str`；`put` 增加 `audit_detail` 关键字参数
  （审计明细不得含凭据）。
- 是否影响历史事务：否。是否需要迁移：否。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.3`。

---

### CP-20261010-06：`limit` 截取哪一端写死

- 提议者：MVP-1（采集与检索原型）
- 变更原因：`find`/`list_events` 先升序排序再取前 N 条 = **最早** N 条；该语义未写下来，
  导致调用方（含 MVP-1 自己的幂等测试）默认它是"最近 N 条"而"假绿"。
- 受影响：`docs/contracts/CONTRACT.md` §12.1 列表；新增 3 项顺序契约测试。行为**不变**。
- 同时**拒绝**了"顺手改参考实现"的做法：该改动经逐行比对是等价的无操作改动且理由与代码不符，已回退。
- 主 Agent 裁决：**接受**（仅固化语义），纳入 `contracts-v0.4`。

### CP-20261010-07：`content_origin` 声明 `content_hash` 的含义

- 提议者：MVP-2（邮件同步原型）
- 变更原因：mcp-email-server 通道不暴露原始字节，`content_digest` 只能是元数据投影摘要；
  若不区分"原始字节"与"投影摘要"，下游会把它当字节级完整性证明（归档回读校验正是这么用的）。
- 受影响：`MailMessageRef.content_origin`（默认 `RAW_BYTES`）、`SourceDocument.content_origin`、
  `ArchiveEntry.content_origin` 及 `to_dict/from_dict`；新增 2 项契约测试。
- 是否影响历史事务：否（都是带默认值的可空字段）；是否需要迁移：否。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.4`。

### CP-20261010-08：`MailSource` 可选能力探测 `cursor_position()`

- 提议者：MVP-2
- 变更原因：不同通道掌握的同步位置信息不同（IMAP 直连有 UIDNEXT，MCP 通道只能给服务端 `date`），
  需要一个"有就用、没有就降级"的观测口，而不是逼所有实现都提供。
- 受影响：`MailSource`/`MailSender` 加 `@runtime_checkable`；文档化可选方法契约；新增 1 项契约测试。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.4`。

### CP-20261010-09：失败是否升为一等对象

- 提议者：MVP-2
- 主 Agent 裁决：**拒绝**。理由：失败已有 `failure_class` + 状态机 + 只追加审计/事件三处承载，
  再加速成一个对象会形成第四份真相；"结果未知"已有 `ActionAttempt` 载体；
  需要失败列表时用投影查询即可。留档以备将来出现"失败要审批/要分配给人"的真实需求时重开。

### CP-20261010-10：幂等验收口径

- 提议者：MVP-2
- 变更原因：验收时"审计行数是否变化"容易被误当成幂等判据。
- 受影响：文档化口径（对象计数不变 + 事件不重复处理 + **允许新增审计行**）；新增 1 项契约测试。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.4`。

---

### CP-20261010-11：定序必须与写入顺序一致（消除随机 id 平局）

- 提议者：MVP-2（发现顺序类测试在 sqlite 下不稳定）；Lead 复现并定位为契约缺陷
- 变更原因：`created_at` 只有毫秒精度，连写对象常同毫秒；`(created_at, id)` 的 id 是随机的，
  平局判据因此随机 → 分页漏项、`limit=1` 随机、**跨后端答案不同**。
- 受影响：`reference.order_key()` 与 `_write_seq`/`_seq_counter`（参考实现已改）；
  SQLite 实现须对齐（follow-up task-7）；新增 2 项定序契约测试 + 1 项稳定性测试；
  `docs/contracts/CONTRACT.md` §13.1。
- 是否影响历史事务：否（只影响同毫秒并列时的返回顺序）；是否需要迁移：SQLite 侧需补序号列或改用 rowid。
- 替代方案：把契约改成"顺序不保证"（否决：分页/巡检会静默漏项，正是要避免的静默失效）。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.5`。

### CP-20261010-12：`Event` 的访问路径（文档化既有行为）

- 提议者：Lead（诊断 `get("Event", ...)` 抛 NotFound 时确认两实现行为一致）
- 变更原因：`get(object_type, object_id)` 与事件接口的关系没有写下来，
  后人容易"顺手补上"而造成两个后端分叉，或者绕过 `claim_event` 的原子领取语义。
- 受影响：`docs/contracts/CONTRACT.md` §13.2；新增 1 项契约测试固化"两边一致抛 NotFound"。
- 主 Agent 裁决：**接受**（文档化 + 测试固化），纳入 `contracts-v0.5`。

### CP-20261010-13：`check_approval_valid` 的 `changed_fields` 恒为空

- 提议者：MVP-5（实现时发现契约函数"说不清哪里变了"）
- 变更原因：`changed = current_summary.diff_keys(current_summary)` —— 拿自己和自己比，必然为 `[]`。
  这不是"缺个字段"，而是**把结论说反**：调用方读到空列表会理解成"没有字段变化"，
  而真实情况恰是内容变了；上层被迫各自绕路（MVP-5 用 `Approval.summary_shown` 自行还原）。
- 受影响：`actions.check_approval_valid()` 新增关键字参数 `approved_summary`（可空，向后兼容）；
  传入时给出真实变化字段，不传时为 `None`（显式未知，不再用 `[]` 冒充"没有变化"）；
  新增 3 项契约测试；`docs/contracts/CONTRACT.md` §14.1。
- 是否影响历史事务：否（无持久化字段变化）；是否需要迁移：否。
- 替代方案：把 `changed_fields` 从 detail 里删掉（否决：控制台与审计需要"为什么失效"的可读原因）。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.6`。

### CP-20261010-14：`ActionState` 里并不存在 `FAILED_UNSAFE`

- 提议者：MVP-5（按文档实现时发现枚举里没有这个状态）
- 变更原因：契约散文把状态机写成 `FAILED_SAFE`/`FAILED_UNSAFE`/`UNKNOWN`，
  实际枚举是 `PREPARED / AUTHORIZED / IN_FLIGHT / SUCCEEDED / FAILED_SAFE / UNKNOWN / CANCELLED`。
  文档与枚举不一致会让实现者去"补一个状态"，而状态机的任何新增都要付迁移与转换代价。
- 受影响：`docs/contracts/CONTRACT.md` §12.4 措辞 + §14.2 说明；**不改枚举**。
- 主 Agent 裁决：**接受**（文档修正；枚举保持不动）。

### CP-20261010-15：同一动作允许并存多条 ACTIVE 审批

- 提议者：MVP-5（实测：同一动作批两次 → `ACTIVE 审批数 = 2`）
- 变更原因：授权来源有歧义——审计与执行侧"这条动作由哪张审批授权"不再唯一。
  执行侧 `claim_action` 只保证"执行一次"，不能替代"授权唯一"。
- 受影响：`store.approve()` 语义（同一事务内把同 `action_id` 的其它 ACTIVE 审批置 `INVALIDATED`）；
  参考实现与 SQLite 实现均已对齐；新增 3 项契约测试；`docs/contracts/CONTRACT.md` §14.3。
- 是否影响历史事务：**是**——已存在的"同一动作多条 ACTIVE"记录不会被自动清理
  （不写迁移：历史数据只需在执行侧继续走 `claim_action` 兜底，行为不变）；新写入受新规则约束。
- 替代方案："靠 `claim_action` 兜住就够了"（否决：见上，两道保护解决不同问题）。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.6`。

### CP-20261010-16：审批默认 TTL 只藏在实现里

- 提议者：MVP-5
- 变更原因：两个实现都在 `approve()` 里静默写 `created_at_approval + 24h`，签名看不出 TTL，
  调用方无法从契约得知"审批多久后失效"。
- 受影响：新增契约常量 `DEFAULT_APPROVAL_TTL_SECONDS`（导出到包根），两个实现改为引用它；
  显式 `expires_at` 优先；新增 2 项契约测试；`docs/contracts/CONTRACT.md` §14.4。
- 是否影响历史事务：否；是否需要迁移：否。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.6`。

### CP-20261010-17：冲突是否升为独立共享对象

- 提议者：MVP-3
- 变更原因：没有 `Conflict` 共享对象，冲突只能进 `Case.blocked_reason` + `Question(kind="conflict")`，
  无法 `find()` 查询，手机端"节目化展示冲突"不便。
- 受影响：无代码改动；在契约里写死约定（冲突 = `Question(kind="conflict")` + `Case.blocked_reason`，
  冲突未解决前不得产出可发送结论）；`docs/contracts/CONTRACT.md` §14.5。
- 替代方案（被采纳为拒绝理由）：新增一等 `Conflict` 对象会带来模式迁移、
  全链路（find/审计/控制台/归档 manifest）支持成本，以及"冲突对象与 Question 谁是真源"的新分歧点；
  与 CP-20261010-09 同源：**冲突是结论，不是主体**。
- 主 Agent 裁决：**拒绝**，理由与约定留档纳入 `contracts-v0.6`。

---

### CP-20261010-18：`content_origin` 的取值必须由契约给出（真缺陷）

- 提议者：MVP-7（落地 §12.2 时发现取值只有字面量）
- 变更原因：契约只写了 `content_origin` 的**语义**，没写**取值**；两个模块各定义一份字面量
  （`mail/mcp_stdio.py`、`archive/manifest.py`）。任何一处漂移会让"投影 vs 字节级"判据静默错配，
  最坏情况是把投影当字节级证明——正是 §12.2 要禁止的。
- 受影响：`objects.CONTENT_ORIGIN_RAW_BYTES` / `CONTENT_ORIGIN_METADATA_PROJECTION` /
  `VALID_CONTENT_ORIGINS`（导出到包根）；两个模块改为引用契约常量；新增跨模块一致性测试；
  `docs/contracts/CONTRACT.md` §15.1。
- 是否影响历史事务：否（取值字符串未变，只是定义位置上收）；是否需要迁移：否。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.7`。

### CP-20261010-19：`NOT_AUTHORIZED` 登记为契约错误码

- 提议者：MVP-7（bdpan 未登录需要一个稳定码；契约只有 PERMISSION_DENIED/APPROVAL_REQUIRED）
- 变更原因：两者都是"现在不能做"但下一步动作不同（重新授权 vs 取审批引用），混用会误导用户与重试逻辑。
- 受影响：`errors.NotAuthorized`（`code="NOT_AUTHORIZED"`，`retryable=False`、`needs_reauth=True`）；
  `asistent/pan/errors.py::PanNotAuthorized` 改为继承它；新增契约测试；§15.3。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.7`。

### CP-20261010-20：`PanClient.put()` 没有"不覆盖"前置条件（文档化，不改签名）

- 提议者：MVP-7
- 变更原因：无法表达"仅当远端不存在时写入"，真实通道存在 TOCTOU 窗口。
- 受影响：契约明确补偿控制（写后必须回读；发现被并发改写 → `ArchiveIntegrityError` 拒绝覆盖）；
  §15.4。**不改签名**：通道能力未验证，加后端支持不了的前置条件等于制造假能力。
- 主 Agent 裁决：**接受**（文档化补偿控制），纳入 `contracts-v0.7`。

### CP-20261010-21：完整性等级规则上收契约

- 提议者：MVP-7
- 变更原因：§12.2 只说"不得把投影当字节级证明"，没说怎么分类；归档模块内有两处各自分类。
- 受影响：`archive.integrity_level()` + `INTEGRITY_BYTE_LEVEL/PROJECTION_ONLY/UNDECLARED`；
  归档模块的分类改为调用它（措辞仍由调用方负责）；新增"未知绝不升格成字节级"的契约测试；§15.2。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.7`。

### CP-20261010-22：恢复的三条安全属性落成字段

- 提议者：MVP-7
- 变更原因：`RestoreJob` 只有 `read_only_mode`，"审批失效""不重放"只写在 `note` 散文里，
  无法机检——恢复记录不能独立证明这两条。
- 受影响：`RestoreJob.approvals_invalidated` / `replay_forbidden`（默认 True = 安全值）；
  `asistent/archive/restore.py` 显式写出；新增持久化往返测试；§15.5。
- 是否影响历史事务：否（新增字段有安全默认值，旧记录读出来就是安全语义）；是否需要迁移：否。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.7`。

### CP-20261010-23：SPEC 的 `ARCHIVE_PENDING` 与契约状态枚举的映射（文档化，不改枚举）

- 提议者：MVP-7（按 SPEC 实现时发现枚举里没有这个状态）
- 变更原因：SPEC §16 MVP-7 写"Case 为 `ARCHIVE_PENDING`"，枚举里不存在；实现只能自造映射。
- 受影响：文案映射写进契约（§15.6）：`ARCHIVE_PENDING` 的可观察语义 = `PARTIAL` + `ArchiveJob.pending` 非空。
- 替代方案（被否决）：给枚举加 `ARCHIVE_PENDING`——会立刻产生"何时是 PENDING、何时是 ARCHIVE_PENDING"
  的分歧，而 SPEC 那句话要表达的是**用户可见状态**，用"状态 + 待办集合"更准确。
- 主 Agent 裁决：**拒绝改枚举，接受文档化映射**，纳入 `contracts-v0.7`。

---

### CP-20261010-24：Case 增加 `WAITING_FOR_USER → WAITING_FOR_CONTEXT`

- 提议者：MVP-4（手机控制台）
- 变更原因：追问答完可能**暴露新的上下文缺口**；原状态机不允许回到"等上下文"，控制台只能让状态
  原地不动只改 `next_action` → 手机端显示"旧状态 + 新下一步动作"，自相矛盾。
- 受影响：`transitions` 的 Case 表新增一条转换（不改其它转换、不加状态）；新增"荒谬跳跃仍被拒"的反向测试。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.8`。

### CP-20261010-25：`Question.kind` 必须落库（真缺陷）

- 提议者：MVP-6（并同时被 MVP-4 的控制台分组需求印证）
- 变更原因：§14.5 明文写 `Question(kind="conflict")`，但对象里没有 `kind` 字段，
  而 `QuestionCandidate.validate()` 强制 kind 非空 → 类别只用于派生 ID 与报告 detail，**落库即丢**，
  下游只能读 `background` 文字猜。
- 受影响：`objects.Question.kind`；理解层 `_upsert_question` 落库并纳入"是否需要更新"的判据；
  新增"落库的 kind 与报告 detail 逐条一致"的测试。
- 是否影响历史事务：否（空串 = 未分类）；是否需要迁移：否。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.8`。

### CP-20261010-26：`Memory.valid_until`

- 提议者：MVP-6
- 变更原因：SPEC 要求记忆有有效期，而 `Memory` 无对应字段，只能旁挂自造对象；
  别的模块无法判断"这条记忆现在还能不能用"。
- 受影响：`objects.Memory.valid_until`（`None` = 长期有效）。过期**效果**复用 `state=REVOKED`
  （不为同一效果再造状态："是过期还是撤销"由 links/原因区分）。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.8`。

### CP-20261010-27：阻塞的事务不得被批准（跨层硬门闸）

- 提议者：MVP-6（实测绕过路径）
- 变更原因：成长层保证"冲突不产出可发送结论"，但审批路径不读 `Case.blocked_reason`；
  实测"先生成可发送草稿 → 再制造冲突 → 直接拿旧草稿调审批"**能批准成功**。
- 受影响：`actions.approval.require_case_not_blocked()`；`create_approval` 入口调用它
  （`case_id` 缺失时从 `DraftVersion → Draft.case_id` 兜底）；新增真实串联测试
  （真实规则冲突 → 阻塞 → 审批被拒；裁决后放行）。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.8`。理由：审批是"人同意发出去"的唯一入口，
  必须在**审批**这一层拦，任何"更早一层已拦住"的说法都不能替代这一层。

### CP-20261010-28：资料与事务的关联必须双向完整（真缺陷）

- 提议者：MVP-4（发现 `SourceDocument.case_id` 恒空）；实施时又发现第二个洞
- 变更原因：① `SourceDocument.case_id` 从不回填 → 按 `case_id` 过滤文档的下游**静默**拿到空集；
  ② `_ensure_case` 只在新建事务时挂资料 → 同一事务的第二份资料**挂不上去**（多资料事务少报成员）。
- 受影响：理解层新增 `_backfill_document_case()`（补 `Document.case_id` + 补
  `Case.source_document_ids`，幂等）；新增 3 条测试（回填、第二份资料、重复处理幂等）。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.8`。权威仍是 `Case.source_document_ids`。

### CP-20261010-29：阻塞原因必须随事实更新（含清除）（真缺陷）

- 提议者：Lead（写 CP-27 的对照实验时发现）
- 变更原因：`Case.blocked_reason` 原先写在"等待原因/下一步是否变化"这个不相关分支里 →
  冲突裁决后重新理解同一事务时，若等待原因恰好没变，`blocked_reason` **留着不清**，
  事务永久阻塞；与 CP-27 门闸叠加即**死锁**（发不出去也批不了）。
- 受影响：理解层把 `blocked_reason` 的判定与"等待原因是否变化"解耦，两者任一变化都落库并记审计。
- 主 Agent 裁决：**接受**，纳入 `contracts-v0.8`。

### CP-20261010-30：重新批准必须新建对象（文档化）

- 提议者：MVP-4
- 变更原因：`ApprovalState` 终态不可复活，落地时不确定"重批"该改旧对象还是新建。
- 受影响：契约明确"重批 = 新 `Approval` 对象，旧的留在历史，`Case.approval_ids` 只增不减"；
  理由写进 §16.7（审批是"对确定内容版本的同意"，复活会让"批的是哪一版"不可追溯）。
- 主 Agent 裁决：**接受**（文档化，不改枚举），纳入 `contracts-v0.8`。

### CP-20261010-31：授权唯一性只作用于绑定动作的审批（文档化）

- 提议者：MVP-4
- 变更原因：v0.6 的"同一动作至多一个 ACTIVE 审批"对 `action_id` 为空的审批是否适用未明确。
- 受影响：契约明确"唯一性只对声明了 `action_id` 的审批生效"（§16.8）；
  空 `action_id` 的审批授权的是判断，不是某个具体外发。
- 主 Agent 裁决：**接受**（文档化），纳入 `contracts-v0.8`。

---

## contracts-v0.8（冻结，2026-10-10）

在 `contracts-v0.7` 基础上的**字段补全（2 个）+ 状态转换补全（1 条）+ 跨层门闸（1 道）+
三个真缺陷修复 + 三次文档化**（CP-20261010-24..31）。
对象字段只增不减且默认值安全；状态机只增一条转换、不加状态；**无模式迁移**。

三个真缺陷（都是"落地时才暴露、且会卡住用户"的那类）：
`Question.kind` 落库即丢、`SourceDocument.case_id` 恒空 + 多资料事务少报成员、
`blocked_reason` 裁决后不清除（与审批门闸叠加成死锁）。

冻结证据：

- 契约测试 119 → 125 项，两种后端均通过；
- 全量 737 → **749 项**，两种后端 RC=0（reference `skipped=21`、sqlite `skipped=11`）；
- 跨层门闸有**真实串联**证据：用成长层完整链路制造真实规则冲突 → Case 阻塞 →
  MVP-5 审批入口被拒（含便捷组合入口）→ 废止规则重新理解后阻塞被清除且门闸放行；
- `scripts/gen_contract_snapshot.py --check` 通过，快照 `contracts/contracts-v0.8.json`。

---

## contracts-v0.7（冻结，2026-10-10）

在 `contracts-v0.6` 基础上的**取值上收 + 错误码登记 + 安全属性字段化 + 三次文档化**（CP-20261010-18..23）。
对象字段只增不减且默认值即安全值；状态机与错误分类未改变；**无模式迁移**。

冻结证据：

- 契约测试 119 项在参考实现与 SQLite 实现下均通过；
- 全量 583 项在两种后端下 RC=0（reference `skipped=9`、sqlite `skipped=5`）；
- `scripts/gen_contract_snapshot.py --check` 通过，快照 `contracts/contracts-v0.7.json`；
- A-5 发布入口 `sh scripts/release.sh --dry-run` 由 Lead 独立复跑：`state=VERIFIED exit=0`。

---

## contracts-v0.6（冻结，2026-10-10）

在 `contracts-v0.5` 基础上的**语义收紧 + 两处文档修正 + 一次拒绝留档**（CP-20261010-13..17）。
对象字段、状态机枚举、错误码、幂等规则均未改变；**无模式迁移**。唯一影响历史数据的点是
CP-15（历史上可能已存在"同一动作多条 ACTIVE 审批"，不自动清理，执行侧仍由 `claim_action` 兜底）。

冻结证据：

- 契约测试 114 项（`python3 -m unittest discover -s tests/contracts -t .`）在参考实现与 SQLite 实现下均通过；
- 全量 497 项在两种后端下全绿（reference `skipped=9`、sqlite `skipped=5`，跳过项为跨进程/平台能力，均写明理由）；
- `scripts/gen_contract_snapshot.py --check` 通过，快照 `contracts/contracts-v0.6.json`；
- 设备 `board`（Python 3.10.12）复跑通过。

---

## contracts-v0.5（冻结，2026-10-10）

在 `contracts-v0.4` 基础上的**定序语义修正 + 一次文档化**（CP-20261010-11/12）。

冻结证据：

- 契约测试 106 项在参考实现与 SQLite 实现下均通过（`ASISTENT_STORE` 切换）；
- 全量测试双后端全绿（325 项）；
- `scripts/gen_contract_snapshot.py --check` 通过，快照 `contracts/contracts-v0.5.json`。

---

## contracts-v0.4（冻结，2026-10-10）

在 `contracts-v0.3` 基础上的**纯增补 + 一次拒绝留档**（CP-20261010-06..10）。
除文档与新增可空字段外，对象字段语义、状态机、错误码、幂等规则均未改变，历史数据无需迁移。

冻结证据：

- 契约测试 103 项（`python3 -m unittest discover -s tests/contracts -t .`）在参考实现与 SQLite 实现下均通过；
- 全量 295 项（契约 + 存储 + 采集检索 + 邮件）在两种后端下仅剩 2 项邮件通道编码用例待修（task-5）；
- `scripts/gen_contract_snapshot.py --check` 通过，快照 `contracts/contracts-v0.4.json`。

---

## contracts-v0.3（冻结，2026-10-10）

在 `contracts-v0.2` 基础上的**纯增补**（CP-20261010-03/04/05）：存储协议登记三个已被调用/被测试使用的方法与参数、
`limit` 语义显式化；对象字段、状态机、错误码、幂等规则**均未改动**，历史数据无需迁移。

冻结证据：

- 契约测试 96 项（`python3 -m unittest discover -s tests -t .`）在参考实现与 SQLite 实现下均通过
  （SQLite 侧对齐见 task-4）；
- `scripts/gen_contract_snapshot.py --check` 通过，快照 `contracts/contracts-v0.3.json`；
- 文档改名 `docs/contracts/CONTRACT-v0.2.md` → `docs/contracts/CONTRACT.md`（版本无关，避免引用漂移）。

---

## contracts-v0.1（冻结，2026-10-10）

首次冻结内容：

- 共享对象 29 个：SourceDocument / Person / Organization / ContactPoint / Conversation / Case / Task / Fact /
  Inference / Rule / Memory / Workflow / WorkflowVersion / Draft / DraftVersion / Question / Answer / Approval /
  ExternalAction / ActionAttempt / ArchiveJob / ArchiveManifest / ArchiveEntry / RestoreJob / AuditEvent /
  Execution / ToolCall / Event / FileArtifact；
- 通用字段与 revision CAS 语义（§21.1）；
- 13 台状态机（含 Case、Task、Approval、ExternalAction、ActionAttempt、ArchiveJob、Rule、Memory、Workflow、
  WorkflowVersion、Question、Draft、IndexState、RestoreJob、Execution）；
- 存储接口 `StoreProtocol`、执行后端接口 `ExecutionBackend`、邮件接口 `MailSource/MailSender`、
  归档通道接口 `PanClient`；
- 幂等键派生规则（事件、邮件身份、动作、归档 ID）与动作状态表（§21.2）；
- 资料引用模型与 EvidenceSet（§6.2）；
- 归档 ArchivePolicy / manifest / 回读校验 / 恢复隔离契约（§12、§21.4）；
- 错误与失败分类（§13.1）。

未纳入 v0.1（后续契约版本处理）：真实 `mcp-email-server` 通道的参数细节、真实 `baidu-drive` 通道的
认证与调用细节、模型路由与预算参数。
