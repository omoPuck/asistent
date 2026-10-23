# MVP-2：邮件同步与去重身份（`asistent/mail/**`）

- 分支：`mvp/2-mail-sync`（worktree：`D:\dsh\个人助手\.worktrees\mvp-2-mail`）
- 起点：`e110f25`（contracts-v0.2 冻结点）；**已合并 main**（`dd603bd` = contracts v0.4 +
  SQLite 存储 + 资料采集检索）；契约 v0.4 的对齐已落地（§4.10）
- 交付物：`asistent/mail/{__init__,errors,sync,mcp_stdio}.py`、`tests/mail/**`、本文档
- 结论：本地（Windows，**默认 cp936 控制台，不设 `PYTHONUTF8`/`PYTHONIOENCODING`**）与设备
  （`ssh board`，`/usr/bin/python3` = **3.10.12**，默认 locale / ASCII locale / sqlite 三种）
  `python -m unittest discover -s tests -t .` **312/312 全绿**（设备 0 跳过）、
  `tests/mail` **57/57 全绿**（含编码回归 6 项 §4.9 + 契约对齐 11 项 §4.10）；纯标准库、零第三方依赖；
  **未接入真实邮箱**（凭据尚未提供）：MCP 通路用自写假 stdio 服务器
  端到端验证，另附对真实 `mcp-email-server` 1.11.1 的**只读探针**原始输出（§4.8）。

---

## 1. 目标与验收对照

| 验收项 | 状态 | 证据 |
| --- | --- | --- |
| 幂等：同一封邮件投递 2 次，对象/Case/审计条数完全一致 | ✅ `run1 == run2 == {"AuditEvent": 7, "Case": 2, "Conversation": 2, "Event": 2, "SourceDocument": 2}` | §4.2 |
| 两个账号同一 `Message-ID` → 2 个对象、2 个 Case | ✅ 身份不同、`source_system` 不同 | §4.3 |
| 游标续传：第二轮只取新邮件；重复列取不推进游标 | ✅ 续传 `listed=1 new_doc=1`；重复列取 `new_doc=0 dup=1 cursor_changed=False` | §4.4 |
| 游标失效/损坏可检测且可重建（有原始输出） | ✅ 三种触发路径：状态文件损坏、来源回退、来源抛 `CursorInvalid` | §4.4 |
| 失败记成状态而非吞掉 | ✅ `failures()` / `quarantined()` / 审计 `FAILED` 行；可 `release_quarantine()` 重试 | §4.6 |
| 可重入：任一写入点崩溃后重放补齐且不重复 | ✅ 2 个崩溃注入点（对象写入间、游标写前） | §4.5 |
| 假 MCP stdio 服务器验证调用序列与错误处理 | ✅ 19 项测试：握手序列、分页、超时、崩溃、JSON-RPC 错误、`isError`、只读守卫、脱敏 | §4.7 |
| 一条真实 `tools/list` 只读探针输出 | ✅ 19 个工具 + `list_available_accounts` 实测返回 | §4.8 |
| `contracts/**`、`tests/contracts/**` 零改动 | ✅ `git diff --name-only HEAD -- asistent/contracts tests/contracts` 与 `merge-base..HEAD` 两处均为空 | §4.1、§4.10.5 |
| Python 3.10 兼容（设备复跑） | ✅ 312/312（默认 locale，0 跳过）+ 邮件 57/57（默认 locale / ASCII locale / sqlite） | §4.1、§4.9、§4.10.3 |
| 中文邮件不依赖控制台代码页（cp936/gbk/ascii 下逐字节往返） | ✅ 默认 cp936 控制台、敌对父环境、设备 ASCII 控制台三处全绿；新增 6 项回归测试 | §4.9、§2.8 |
| 摘要语义显式声明（contracts v0.4 `content_origin`） | ✅ MCP 引用/文档 = `METADATA_PROJECTION`；夹具通路 = `RAW_BYTES`；未声明 = `None` | §4.10.1、§2.9 |
| 可选能力拆分（v0.4：观测 `cursor_position()` vs 比较 `cursor_rank()`） | ✅ 无参观测返回字典、无信息返回 `None`；回退检测改走 `cursor_rank`；最小来源仍满足协议 | §4.10.1 |
| 凭据不进代码/日志/夹具/提交 | ✅ `env=` 密钥值自动登记 `Redactor` + `LogSink` 0600（不进审计）；测试断言密钥不出现在日志/审计/状态/共享对象；spawn 失败详情只带 argv[0] 文件名 | §4.7、§2.7、§4.10.2 |

---

## 2. 设计

### 2.1 分层与依赖

```
asistent/mail/
  errors.py       # 邮件通道错误分类（retryable / failure_class 单一裁决点）
  sync.py         # MailSyncEngine：列取→身份→入库→事件→游标→失败状态（只依赖 contracts）
  mcp_stdio.py    # StdioJsonRpcClient（JSON-RPC 2.0 over stdio）+ McpMailSource（MailSource 实现）
```

`sync.py` 只 import `asistent.contracts`（`MailSource`/`MailMessageRef`/`ReferenceStore` 协议、
对象、枚举、`ids`、`timeutil`），**不知道 MCP 的存在**；`mcp_stdio.py` 只通过 `MailSource`
协议暴露给引擎。因此换任何来源（IMAP 直连、内存夹具）都不需要动同步逻辑，
`tests/mail/fakes.py` 的 `MemoryMailSource` 就是这条边界的证明。

### 2.2 同步状态与游标（`SyncStateStore`）

单文件 JSON（`version=1`），字段：`runs`、`cursors`、`identities`、`failures`、`last_report`、`recent_runs`。
每账号+文件夹一个游标项：`{"cursor", "epoch", "advanced_at", "invalidated_reason", "invalidated_at"}`；
每封邮件在 `identities` 里一行：`{"state", "attempts", "document_id", "case_id", "conversation_id",
"uid", "folder", "message_id", "content_hash", "last_outcome", "synced_at", "failure_class", "error"}`；
失败同时在 `failures` 里留一条完整记录（`state` 为 `FAILED`/`QUARANTINED`）。

推进规则（`sync_once`）：

1. 读游标 → 调 `source.list_new(cursor)`；
2. 每个 `ref` 走 `_process_ref`（§2.3）；单封失败**不中断**本轮；
3. 本轮结束后，仅当 `新游标 != 旧游标` 或本轮做过重建时才落盘并写一条 `AuditAction.WRITE`
   审计（reason=`游标推进`／`游标重建后推进`）。**没有新邮件且窗口没前移 → 游标字符串逐字节相同
   → 不写状态、不写审计**，这是"重复投递计数不变"的前提。

检测到异常时的重建路径（`_list_with_recovery`）：

| 触发 | 判定处 | 动作 |
| --- | --- | --- |
| 状态文件不可解析 / 版本不认识 | `SyncStateStore.load()` | 隔离坏文件（改名留存）→ `bump_epoch()` → 本轮按"无游标"重建 |
| 来源游标回退（`cursor_position` 变小） | `_detect_regression` | `_invalidate()` → 重新列取（`list_attempts=2`）→ 重建 |
| 来源直接抛 `CursorInvalid`/`CursorRegression` | `_list_with_recovery` | 同上（只重试一次，避免死循环） |
| 其它来源异常 | `_list_with_recovery` | 记 `failures`+审计 `FAILED`，**不推进游标**，本轮 `complete=False` |

重建永远安全：身份与对象 ID 都是内容/身份决定的确定性值（§2.3），重新列取只会命中唯一键去重。

### 2.3 幂等与去重身份

- **身份**直接复用契约：`MailMessageRef.identity_key()`（供应商 ID > 邮箱作用域 ID+内容摘要 > 弱
  `Message-ID`，且**永不跨账号合并**）。引擎不自己拼身份。
- **确定性 ID**：`new_id(type, token=digest_of(["mvp2-mail", type, 种子])[:24])`，种子取身份键
  （`SourceDocument`/`Case`）或线程键（`Conversation`）。崩溃重放时重算出的 ID 与首次一致，
  因此不需要额外的"处理中"索引就能收敛。
- **唯一键三元组**送入 `ReferenceStore.put(unique=...)`：`("mail_identity", 身份键)`、
  `("mail_case", 身份键)`、`("mail_thread", 线程键)`。重复投递 → `DuplicateObject` → 记为
  `duplicates_skipped`，不产生任何新对象。
- **事件去重**走契约的 `event_dedupe_key(source, source_event_id)`，`source_event_id` 取身份键
  （同一封邮件在重放/重复投递里恒等）。
- **线程键** `"<account>|<message-id>"`（无 `Message-ID` 时退化为 `"<account>|uid:<folder>:<uid>"`），
  同一账号内同 `Message-ID` 归一会话，跨账号不归一。
- **文件夹漂移**：同一身份、同一内容摘要、`mailbox_folder` 变了 → 原地 `UPDATE`（`documents_updated`），
  仍是一个对象。
- 审计只追加、不改写（`docs/contracts/CONTRACT-v0.2.md` 第 35 行），因此重复投递只可能多出"尝试"审计行；
  幂等断言比较的是**非审计对象计数**（`tests/mail/test_mail_sync.py::object_counts`）。

### 2.4 失败即状态

`_fail_ref` / `_record_list_failure` 把失败写成四份可查证据：内存 `SyncReport.failures`、
状态文件 `failures`（含 `attempts`、`terminal`）、审计 `FAILED` 行（`object_type="MailSyncFailure"`，
`detail.failure_class`）、以及 `quarantined()` 列表。终态判定：`attempts >= max_attempts(3)` **或**
错误自报 `retryable is False`（例如内容冲突）→ 立即隔离，**不再阻塞游标**，其余邮件继续同步。

### 2.5 崩溃重放

写序固定为 `SourceDocument → Conversation → Case → Event → 游标`，每一步都用确定性 ID + 唯一键，
所以任意点崩溃后重放都是"补齐缺的、跳过已有的"。原始邮件落盘走 `RawStore`
（`<root>/<account>/<摘要前32>/g<代>-<摘要16>.eml`，先写临时文件再 `os.replace`，权限 0600，
**永不覆盖**），因此重放时 raw 已在盘上也不会重复写。

### 2.6 MCP stdio 适配器

`StdioJsonRpcClient`：`subprocess.Popen`（管道）+ 读线程 + `queue` 超时；每行一条 JSON-RPC 2.0 消息；
非 JSON 行记为 `protocol_noise`，`notifications/*` 收到 `notifications` 列表。能力：

- `initialize` → `notifications/initialized` → `tools/list`（`connect()` 校验必需工具齐全，
  缺工具抛 `McpHandshakeError`）；
- 崩溃检测：stdout 在等待响应时关闭 → `McpCrashed(returncode, stderr_tail)`；
- 超时：`McpTimeout`（默认 30s，测试用 0.6s 验证上界）；
- JSON-RPC `error` 与工具 `isError=true` → `McpToolError`；`structuredContent` 与 `content[].text`
  两种返回形态都解析（`_structured`）。

`McpMailSource`（`MailSource` 实现，工具名集中在本文件的 `McpToolNames`，实测 19 个工具名见 §4.8）：

| 方法 | 真实工具 | 关键参数 |
| --- | --- | --- |
| `list_new(cursor)` | `list_emails_metadata` | `since` + `order=asc` + `page/page_size` 翻页 |
| `fetch_raw(ref)` | `get_emails_content` | `mark_as_read=False`、`body_offset`/`max_body_length` 分页，遇 `...[TRUNCATED]` 继续取 |
| `list_folders()` | `list_mailboxes` | — |
| `list_accounts()` | `list_available_accounts` | — |
| `cursor_position(cursor)` | — | 本地解析游标里的 `since`，供回退检测 |

游标是版本化 JSON `{"v":1,"account","folder","since"}`，**只含位置**（不含处理时刻），
所以"重复列取无新邮件"会得到完全相同的字符串（§2.2）。`decode_cursor` 对坏 JSON、版本不符、
账号/文件夹不符、`since` 在未来或超出回溯窗口逐一抛 `CursorInvalid`（带 `cursor_head` 便于排查）。

`build_raw`：MCP 不提供 RFC822 原文，适配器把工具返回的字段确定性地重建成 `.eml` 文本
（`X-Asistent-*` 头 + 头字段 + 正文），因此 `fetch_raw` 是**确定性**的；并用
`metadata_projection()` 的摘要在取件时复核"列取到的元数据"与"取到的内容"是否一致，
不一致立即 `McpContentError`（非重试类 → 隔离），避免把新内容挂到旧身份上（§4.7）。

### 2.7 只读与凭据卫生

- `WRITE_TOOLS`（`send_email`/`delete_emails`/`move_emails`/`mark_emails_as_read`/`archive_emails`/
  `save_draft`/`save_to_mailbox`/`forward_email`/`set_email_flags`/`set_email_tags`/`download_attachment`）
  在 `call_tool(read_only=True)` 里**本地先拒绝**（`WriteNotPermitted`），根本不会发到网络上；
  测试另外检查假服务器转录里没有 `WRITE_TOOL_CALLED` 行。
- 子进程环境 = 宿主 `os.environ` **加上**显式 `env=` 覆盖（真实 MCP 服务器要靠 `HOME`/`PATH`/`XDG_*`
  才能找到配置，所以不能改成白名单）；但 `env=` 里"看起来是密钥"的键（`*PASSWORD*`/`*TOKEN*`/`*SECRET*` 等）
  的值会**自动登记进 `Redactor`**，一旦被服务端回显到 stderr 也会被掩掉。
- stderr 全部经 `Redactor`（已知密钥值 + `password=`/`token:`/`scheme://user:pw@` 形态）脱敏后
  写入 0600 追加日志与内存尾部，**不进审计**；测试断言密钥不出现在日志、审计、状态文件、
  `RawStore` 里的原始邮件里（§4.7）。

### 2.8 stdio 编码：不依赖控制台代码页

邮件系统的核心场景就是中文邮件，所以编码必须自己兜住，不能指望运行环境：

- **子进程强制 UTF-8**：环境按「父环境 → 显式 `env=` 覆盖 → `CHILD_UTF8_ENV`」合成，
  `CHILD_UTF8_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}`。父环境照旧保留
  （真实服务端要靠 `HOME`/`PATH`/`XDG_*` 找配置），但编码项**总是被覆盖**，
  所以 Windows 的 cp936、Linux 的 `LC_ALL=C` 都不会让子进程落到 ANSI 代码页上。
- **父进程严格解码**：JSON-RPC 通道 `raw.decode("utf-8", "strict")`；任何非法字节 →
  `McpProtocolError`，detail 带 `tool` + `byte_offset` + `hex` + `hint`。
  选二进制管道而不是 `Popen(encoding=...)`，是为了让两条通道有各自的错误策略：
  协议通道**必须**报错（静默替换 U+FFFD 会写坏数据），stderr 日志通道**尽量可读**
  （非法字节标注 `[非 UTF-8 字节，N 字节已丢弃]` 并计入 `stderr_decode_failures`）。
- **文件 I/O 全显式**：状态文件、日志、原始邮件都显式 UTF-8/二进制，不受默认编码影响。
- **内容级容错与通道级严格是两件事**：`safe_text()` 仍对**邮件内容**里的代理区字符做替换
  （损坏的 RFC 2047 头部不该让整轮同步失败，原文另存 `RawStore`、身份摘要覆盖原始字节）；
  但通道级解码一旦不合法就报错，绝不伪装成功。
- **argv 是例外，且必须显式报错**：命令行参数由 **Popen 用父进程的文件系统编码**编码成字节，
  这不是产品代码能改的（`LC_ALL=C` → `fs=ascii`，非 ASCII 参数在父进程就编不出来，子进程根本没起来）。
  所以 `start()` 把这种失败转成 `McpCrashed` 并写入 `fs_encoding` + 换 UTF-8 locale 的提示（§4.10.4），
  调用方看到的是可诊断的错误而不是"未预期失败"。

### 2.9 摘要语义声明（contracts v0.4 `content_origin`）

`MailMessageRef.content_digest` / `SourceDocument.content_hash` 到底"哈希的是什么"，必须由来源自己声明：

- `McpMailSource` 构造引用时显式 `content_origin="METADATA_PROJECTION"`（常量
  `CONTENT_ORIGIN_PROJECTION`）——MCP 只给结构化字段，没有 RFC822 原始字节，
  文档里的 `content_hash` 是**重建字节**的 sha256，而重建是元数据投影的确定性函数：
  它能证明"投影一致"，**不能**证明与远端原始字节一致。
- 同步层 `_content_origin(ref)` 把声明**原样**带到 `SourceDocument.content_origin`（含内容代次更新路径）；
  契约自带的 `FixtureMailSource`（离线 `.eml`）保持默认 `RAW_BYTES`，即"字节级摘要"通路不受影响。
- 来源没声明（空串）时记 `None`＝未声明，下游**不得**把它当字节级完整性证明；
  代码里不用 `hasattr` 兜底，直接按 v0.4 字段读写。

不标注的后果很具体：归档 manifest 的逐文件哈希回读校验会把投影摘要当字节证明——
"看起来通过、实际没校验到字节"。相关原始输出见 §4.10.1。

---

## 3. 关键决策与取舍

1. **身份/对象 ID 全用确定性推导**：多花一次摘要计算，换来"无额外索引也能幂等重放"。
2. **游标只含位置**：牺牲一点诊断信息（不放处理时刻），换来"无新邮件即零写入"。
3. **`Conversation.last_message_at` 用邮件自身时间戳**（不给日期时才退回本地时刻）：
   否则每次重放都会因"本地时间更新"改写会话、追加审计行，幂等断言会假失败。
4. **失败分"可重试/终态"两类**：终态立即隔离并放行游标，避免一封坏邮件堵住整个信箱。
5. **原文重建而不是硬拼字节**：MCP 没有原文接口，选择"确定性重建 + 元数据投影校验"，
   把不确定性显式暴露成 `McpContentError` 而不是悄悄写错数据（限制见 §5.1）。

---

## 4. 原始证据

（时间戳随运行变化；以下为原样粘贴的输出片段。）

### 4.1 全量测试与改动范围

```
# 本地（C:\Users\<you>\.conda\envs\<env>-a29d9cc4\python.exe = 3.12.14）
# ⚠️ 这一行是 task-3 当时的原始输出，但它是**带** PYTHONIOENCODING=utf-8 跑出来的：
#    环境变量让"子进程用 ANSI 代码页收发 JSON-RPC"这个缺陷藏了起来。
#    真实复现与修复见 §4.9「编码回归」，现在的默认控制台结论以 §4.9 为准。
> python -m unittest discover -s tests -t .
Ran 131 tests in 5.371s
OK

# 设备（ssh board, /usr/bin/python3）
Python 3.10.12
> PYTHONIOENCODING=utf-8 /usr/bin/python3 -m unittest discover -s tests -t .
EXIT=0
Ran 131 tests in 5.543s
OK

# 分模块计数（task-3 时点）
> python -m unittest discover -s tests/contracts -t .
Ran 91 tests in 0.121s
OK
> python -m unittest discover -s tests/mail -t .
Ran 40 tests in 5.284s
OK
# 编码回归后：tests/mail = 46 项（新增 ConsoleEncodingRegressionTest 6 项）

# 改动范围（contracts 未动）
> git status --porcelain
?? asistent/mail/
?? docs/mvp/
?? tests/mail/
> git diff --stat
（空）
> git diff --stat -- contracts tests/contracts
（空）
```

### 4.2 幂等：同一批邮件投递两次

```
[evidence] run1 = sync account=owner@example.com folder=INBOX listed=2 new_doc=2 dup=0 cases=2 events=2 failures=0 quarantined=0 cursor_changed=True rebuilt=False complete=True
[evidence] run1 counts = {"AuditEvent": 7, "Case": 2, "Conversation": 2, "Event": 2, "SourceDocument": 2, "_total": 6}
[evidence] run2 = sync account=owner@example.com folder=INBOX listed=2 new_doc=0 dup=2 cases=0 events=0 failures=0 quarantined=0 cursor_changed=False rebuilt=False complete=True
[evidence] run2 counts = {"AuditEvent": 7, "Case": 2, "Conversation": 2, "Event": 2, "SourceDocument": 2, "_total": 6}
```

（`AuditEvent` 7 = 2×doc + 2×conv + 2×case + 1×游标；第二轮**逐项相同**，游标未推进故没有新审计行。）

MCP 通路（假服务器）同样的三轮重放：

```
[evidence] mcp run1 = sync account=fake folder=INBOX listed=2 new_doc=2 dup=0 cases=2 events=2 failures=0 quarantined=0 cursor_changed=True rebuilt=False complete=True
[evidence] mcp run2 = sync account=fake folder=INBOX listed=1 new_doc=0 dup=1 cases=0 events=0 failures=0 quarantined=0 cursor_changed=False rebuilt=False complete=True
[evidence] mcp run3 counts unchanged = {"AuditEvent": 7, "Case": 2, "Conversation": 2, "Event": 2, "SourceDocument": 2, "_total": 6}
[evidence] mcp run4 (new mail) = sync account=fake folder=INBOX listed=2 new_doc=1 dup=1 cases=1 events=1 failures=0 quarantined=0 cursor_changed=True rebuilt=False complete=True
```

### 4.3 两个账号同一 `Message-ID`

```
[evidence] identity A = mail:a276658bedc1d948df24fd930d350d2476f6ce7a27c2f57d8d82c38af477bbc1
[evidence] identity B = mail:59251f83379bb61677ac5a1aa46b299ea43e1b19bac17524bec7e0859b267d82
[evidence] cross-account documents = [('mcp_stdio:acct-a', 'mail:a276658bedc'), ('mcp_stdio:acct-b', 'mail:59251f83379')]
```

断言：`Message-ID` 相同（`<same-id@example.org>`）但身份不同、两个 `SourceDocument`、两个 `Case`、
两个 `Conversation`。

### 4.4 游标：续传、回退、失效重建

```
# 续传
[evidence] cursor run1 = sync account=owner@example.com folder=INBOX listed=2 new_doc=2 dup=0 cases=2 events=2 failures=0 quarantined=0 cursor_changed=True rebuilt=False complete=True
[evidence] cursor run2 = sync account=owner@example.com folder=INBOX listed=1 new_doc=1 dup=0 cases=1 events=1 failures=0 quarantined=0 cursor_changed=True rebuilt=False complete=True
[evidence] list_calls = [None, "2"]           # 首轮无游标，次轮带游标续传

# 来源游标回退 → 判定 + 重建
[evidence] cursor regression = {"account": "owner@example.com", "folder": "INBOX", "cursor_before": "3", "cursor_after": "1",
  "cursor_changed": true, "cursor_invalidated": true, "invalid_reason": "来源游标回退：3.0 -> 1.0", "epoch": 1,
  "list_attempts": 2, "listed": 3, "documents_created": 0, "duplicates_skipped": 3, "events_duplicate": 3, "complete": true}

# 状态文件损坏 → 隔离 + 重建（本轮把坏文件改名留存）
[evidence] corrupt-state rebuild = {"cursor_before": null, "cursor_after": "1", "cursor_invalidated": true,
  "invalid_reason": "同步状态文件不可解析：JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 3 (char 2)",
  "epoch": 1, "list_attempts": 1, "listed": 1, "documents_created": 0, "duplicates_skipped": 1, "events_duplicate": 1, "complete": true}

# 来源直接抛 CursorInvalid
[evidence] source-side cursor invalid = {"cursor_before": "3:v0|stale-cursor", "cursor_after": "1", "cursor_invalidated": true,
  "invalid_reason": "模拟服务端判定游标失效：3:v0|stale-cursor", "list_attempts": 2, "documents_created": 0, "duplicates_skipped": 1, "complete": true}

# 通道中断不推进游标，恢复后重放幂等
[evidence] interrupted run = sync ... listed=0 new_doc=0 dup=0 failures=1 cursor_changed=False complete=False
[evidence] resumed run = sync ... listed=2 new_doc=2 dup=0 cases=2 events=2 failures=0 cursor_changed=True complete=True
[evidence] fetch-failure run = sync ... listed=2 new_doc=1 dup=0 failures=1 cursor_changed=False complete=False
[evidence] fetch-failure retry = sync ... listed=2 new_doc=1 dup=1 failures=0 cursor_changed=True complete=True
```

### 4.5 崩溃重放（两个注入点）

```
[evidence] crash-save-cursor replay = sync ... listed=1 new_doc=0 dup=1 cases=0 events=0 failures=0 cursor_changed=True complete=True
[evidence] crash-replay          = sync ... listed=1 new_doc=0 dup=1 cases=1 events=1 failures=0 cursor_changed=True complete=True
```

（第一个注入点：原始对象已写、游标未写；第二个：`SourceDocument` 已写、`Case` 未写。
两例都断言重放后**没有孤儿对象**：文档、Case、Conversation、Event 各恰好 1 个。）

### 4.6 失败与隔离

```
[evidence] poison run1 = sync ... listed=2 new_doc=1 dup=0 cases=1 events=1 failures=1 quarantined=0 cursor_changed=False complete=False
[evidence] poison run2 = sync ... listed=2 new_doc=0 dup=1 cases=0 events=0 failures=1 quarantined=0 cursor_changed=False complete=False
[evidence] poison run3 = sync ... listed=2 new_doc=0 dup=1 cases=0 events=0 failures=1 quarantined=1 cursor_changed=True complete=True
[evidence] poison run4 = sync ... listed=2 new_doc=0 dup=2 cases=0 events=0 failures=0 quarantined=1 cursor_changed=False complete=True
[evidence] identity-less mail = sync ... new_doc=0 failures=1 quarantined=1 complete=True
```

（第 3 次失败达到 `max_attempts=3` → 隔离并放行游标；第 4 轮该邮件被跳过、正常邮件继续。
`quarantined()` / `failures()` / `release_quarantine()` 均可查。）

### 4.7 假 MCP stdio 服务器（`tests/mail/fake_mcp_server.py`）

```
[evidence] stdio method sequence = ['initialize', 'notifications/initialized', 'tools/list']
[evidence] metadata pages = [1, 2, 3]                     # page_size=2 翻到第 3 页
[evidence] cursor = {"account": "fake", "folder": "INBOX", "since": "2026-10-05T06:00:00+00:00", "v": 1}
[evidence] cross-account cursor rejected = CursorInvalid 游标账号与当前账号不符
[evidence] body page offsets = [0, 100, 200]               # 正文按 ...[TRUNCATED] 续取
[evidence] get_emails_content args = [{'account_name': 'fake', 'body_offset': 0, 'email_ids': ['1'], 'mailbox': 'INBOX', 'mark_as_read': False, 'max_body_length': 100000}]
[evidence] timeout elapsed = 0.67s                         # 服务端睡 30s，客户端 0.6s 抛出
[evidence] crash detected = {'code': 'MCP_CRASHED', 'message': 'mcp 子进程在等待 tools/call 响应时关闭了 stdout（exit=9）',
  'failure_class': 'SYSTEM_CRASH', 'detail': {'returncode': 9, 'method': 'tools/call', 'stderr_tail': [...]}}
[evidence] rpc error = {'code': 'MCP_TOOL_ERROR', 'message': "MCP 调用失败：fake rpc error for list_emails_metadata", 'detail': {'method': 'tools/call', 'rpc_code': -32000, 'tool': 'list_emails_metadata'}}
[evidence] isError result = {'code': 'MCP_TOOL_ERROR', 'message': '工具返回错误：fake isError payload for get_emails_content', 'detail': {'tool': 'get_emails_content', 'is_error': True}}
[evidence] write guard = {'code': 'MAIL_WRITE_NOT_PERMITTED', 'message': '只读模式拒绝调用写工具：send_email', 'failure_class': 'PERMISSION_FAILURE', 'detail': {'tool': 'send_email', 'phase': 'READ_ONLY_GUARD'}}
[evidence] redacted = connecting imap://user:***REDACTED***@imap.example.org password=***REDACTED*** token=***REDACTED***
[evidence] secret found in log = False
[evidence] secret found in audit = False
[evidence] metadata conflict = sync account=fake folder=INBOX listed=1 new_doc=0 dup=0 cases=0 events=0 failures=1 quarantined=1 cursor_changed=True complete=True
[evidence] conflict failure = {'stage': 'fetch', 'error_code': 'MCP_CONTENT_ERROR', 'failure_class': 'TOOL_FAILURE',
  'message': '同一 UID 的元数据在列取与取件之间发生变化（内容冲突），拒绝把新内容挂到旧身份上', 'terminal': True}
[evidence] mcp crash run = sync account=fake folder=INBOX listed=0 new_doc=0 failures=1 quarantined=0 cursor_changed=False complete=False
[evidence] mcp recovery run = sync account=fake folder=INBOX listed=1 new_doc=1 failures=0 cursor_changed=True complete=True
```

### 4.8 真实 `mcp-email-server` 只读探针（设备，`tests/mail/real_mcp_probe.py`）

```
$ PYTHONIOENCODING=utf-8 /usr/bin/python3 tests/mail/real_mcp_probe.py \
      --command $HOME/asistent-spike/venv/bin/mcp-email-server --args stdio --call-tools
EXIT=0

{"result": {"capabilities": {"experimental": {}, "prompts": {"listChanged": false}, "resources": {"listChanged": false, "subscribe": false}, "tools": {"listChanged": false}}, "protocolVersion": "2024-11-05", "serverInfo": {"name": "email", "version": "1.11.1"}}, "step": "initialize"}
{"count": 19, "names": ["archive_emails", "delete_emails", "download_attachment", "forward_email", "get_attachment_content", "get_emails_content", "list_allowed_recipients", "list_allowed_senders", "list_available_accounts", "list_email_tags", "list_emails_metadata", "list_mailboxes", "mark_emails_as_read", "move_emails", "save_draft", "save_to_mailbox", "send_email", "set_email_flags", "set_email_tags"], "step": "tools/list"}
{"adapter_read_only": ["get_attachment_content", "get_emails_content", "list_allowed_recipients", "list_allowed_senders", "list_available_accounts", "list_email_tags", "list_emails_metadata", "list_mailboxes"], "adapter_refuses_in_read_only": ["archive_emails", "delete_emails", "download_attachment", "forward_email", "mark_emails_as_read", "move_emails", "save_draft", "save_to_mailbox", "send_email", "set_email_flags", "set_email_tags"], "note": "get_emails_content 的服务端 readOnlyHint=false（mark_as_read=true 会改远端），适配器恒发 mark_as_read=false 因此按只读处理", "server_read_only_hint": ["get_attachment_content", "list_allowed_recipients", "list_allowed_senders", "list_available_accounts", "list_email_tags", "list_emails_metadata", "list_mailboxes"], "step": "classification"}
{"ok": true, "result": {"result": []}, "step": "tools/call", "tool": "list_available_accounts"}
{"error": "McpToolError", "message": "工具返回错误：Error executing tool list_mailboxes: 1 validation error for list_mailboxesArguments\naccount_name\n  Field required [type=missing, input_value={}, input_type=dict]", "ok": false, "step": "tools/call", "tool": "list_mailboxes"}
{"protocol_noise": 0, "stderr_tail": ["[10/10/26 13:52:17] INFO     Processing request of type            server.py:733", "                             ListToolsRequest                                   ", "                    INFO     Processing request of type            server.py:733", "                             CallToolRequest                                    ", "2026-10-10 13:52:17.313 | INFO     | mcp_email_server.config:get_settings:985 - Loading legacy settings from /home/asistent/.config/mcp-email-server/config.toml", "                    INFO     Processing request of type            server.py:733", "                             CallToolRequest                                    "], "step": "stdio", "tools_called": ["list_available_accounts", "list_mailboxes"]}
```

要点：`initialize` 握手成功（协议 2024-11-05，serverInfo `email/1.11.1`）；19 个工具与 §2.6 映射一致；
`list_available_accounts` 在**未配置账号**时返回空列表；故意漏参数的 `list_mailboxes` 让真实服务端的
pydantic 校验错误**原样变成 `McpToolError`**（错误信息可读、可入状态）；`protocol_noise=0`，
stderr 只有 INFO 级日志。探针只调用只读工具，且末尾有"若调用记录里出现写工具则退出码 3"的双保险。
（这次运行带了 `PYTHONIOENCODING=utf-8`；探针本身已做成与代码页无关，见 §4.9 第 8 条冒烟。）

### 4.9 编码回归（非 UTF-8 控制台下的中文邮件）

> 本节是 task-5 那一轮的原始记录，其中的计数（131/137）是当时的规模；后续 task-6
> 又加了 11 项契约对齐测试，当前计数见 §4.10（全量 312、邮件 57）。结论未变。

**缺陷（task-5）**：合并进 main（`f970975`）后，在开发机**默认控制台（cp936/gbk）**跑全量测试红 2 项，
设备端绿。根因不是测试写法，而是 `asistent/mail/mcp_stdio.py` **没有为子进程强制 UTF-8 stdio**：
子进程按 ANSI 代码页收发 JSON-RPC，中文主题/附件名在**子进程侧**就被替换成 U+FFFD，父进程再"正常"
解码 → 静默写坏数据（这比抛错更危险）。

```
# 0) 默认控制台环境确认（不设任何环境变量）
Console.OutputEncoding=utf-8 ; chcp=936
> python -c "import sys, locale, os; print(sys.stdout.encoding, locale.getpreferredencoding(False), os.environ.get('PYTHONUTF8'), sys.flags.utf8_mode)"
gbk cp936 None 0

# 1) 修复前复现（同一命令，无环境变量）
> python -m unittest discover -s tests -t .
ERROR: test_crash_is_detected_with_exit_code (…ErrorHandlingTest…)
  print("[evidence] crash detected =", ctx.exception.to_dict())
UnicodeEncodeError: 'gbk' codec can't encode character '\u0123' in position 240: illegal multibyte sequence
FAIL: test_fetch_raw_is_deterministic_and_marks_no_read_flags (…ListAndFetchTest…)
  self.assertIn("报名表.txt".encode("utf-8"), first)
AssertionError: b'\xe6\x8a\xa5\xe5\x90\x8d\xe8\xa1\xa8.txt' not found in
  b'…Subject: \xef\xbf\xbd\xef\xbf\xbd…\r\nX-Asistent-Attachments: \xef\xbf\xbd…\r\n…'
Ran 131 tests in 5.783s
FAILED (failures=1, errors=1)   → EXIT=1
```

**修复**（`asistent/mail/mcp_stdio.py`，见 §2.8）：

- 子进程环境固定为「父环境 → 显式 `env=` 覆盖 → `CHILD_UTF8_ENV`」，
  `CHILD_UTF8_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}`：调用方给什么都不会落到 ANSI 代码页；
- 父进程侧对 JSON-RPC 通道**严格**解码（`raw.decode("utf-8", "strict")`），非法字节 →
  `McpProtocolError`，detail 带 `tool`/`byte_offset`/`hex`/`hint`，**不再** `errors="replace"` 静默替换；
- stderr 是日志通道：仍尽量可读，但非法字节显式标注 `[非 UTF-8 字节，N 字节已丢弃]` 并计入
  `stderr_decode_failures`；
- 证据输出（`tests/mail/evidence.py`）保留控制台编码、只把错误策略改成 `replace`，因此 cp936/ascii
  下证据行退化成 `?` 而不再抛 `UnicodeEncodeError`。

```
# 2) 默认控制台（cp936，无环境变量）修复后
> python -m unittest discover -s tests -t .
Ran 137 tests in 6.321s
OK            → EXIT=0

# 原两项失败测试单跑
> python -m unittest tests.mail.test_mcp_stdio.ListAndFetchTest.test_fetch_raw_is_deterministic_and_marks_no_read_flags \
                     tests.mail.test_mcp_stdio.ErrorHandlingTest.test_crash_is_detected_with_exit_code
Ran 2 tests in 0.198s
OK

# 3) 连父进程也放到敌对代码页（PYTHONUTF8=0 / PYTHONIOENCODING=cp936）
> $env:PYTHONUTF8="0"; $env:PYTHONIOENCODING="cp936"; python -m unittest discover -s tests -t .
Ran 137 tests in 6.174s
OK            → EXIT_HOSTILE=0

# 4) 新增回归类（6 项）——子进程自报编码，比断言 env 更硬
> python -m unittest -v tests.mail.test_mcp_stdio.ConsoleEncodingRegressionTest
[evidence] child env PYTHONUTF8 = 1 PYTHONIOENCODING = utf-8 CHILD_UTF8_ENV = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
[evidence] child self-reported stdio: INFO fake mcp-email-server ready (pid=53828) | ENVCHECK stdout=utf-8 stdin=utf-8 utf8_mode=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8
[evidence] subject over the wire = '活动通知：报名表（中文）'
[evidence] raw head = [b'X-Asistent-Source: mcp-email-server', …, b'Subject: \xe6\xb4\xbb\xe5\x8a\xa8\xe9\x80\x9a\xe7\x9f\xa5\xef\xbc\x9a\xe6\x8a\xa5\xe5\x90\x8d\xe8\xa1\xa8\xef\xbc\x88\xe4\xb8\xad\xe6\x96\x87\xef\xbc\x89', …]
[evidence] attachment name bytes = b'\xe6\x8a\xa5\xe5\x90\x8d\xe8\xa1\xa8.txt' U+FFFD present = False
[evidence] identity under hostile env = mail:7dee59699c306a9bb1cac703eeab7200119144c2a483c828c4add904d205706c
[evidence] identity under utf8 env    = mail:7dee59699c306a9bb1cac703eeab7200119144c2a483c828c4add904d205706c
[evidence] invalid utf-8 error = {"code": "MCP_PROTOCOL_ERROR", "message": "mcp 子进程输出的不是合法 UTF-8（工具 list_emails_metadata 响应，字节偏移 2369，附近字节 74657874222c202274657874223a2022fffe20626164206279746573207b227061）", "failure_class": "TOOL_FAILURE", "detail": {"method": "tools/call", "tool": "list_emails_metadata", "byte_offset": 2369, "hex": "74657874222c202274657874223a2022fffe20626164206279746573207b227061", "reason": "invalid start byte", "hint": "子进程 stdio 必须强制 UTF-8（PYTHONUTF8=1 / PYTHONIOENCODING=utf-8）"}}
[evidence] stderr decode failures = 1
[evidence] chinese sync = sync account=fake folder=INBOX listed=1 new_doc=1 dup=0 cases=1 events=1 failures=0 quarantined=0 cursor_changed=True rebuilt=False complete=True
[evidence] stored raw bytes = 376 attachment bytes found = True U+FFFD found = False
Ran 6 tests in 0.716s
OK

# 5) sqlite 后端：本 worktree 基于 e110f25（**没有** asistent/storage），
#    因此在合成树上验证：git archive main（c09b8ee，contracts v0.4 + storage）
#    + 本分支的 asistent/mail/** 与 tests/mail/**
> python -m unittest discover -s tests -t .                       # 合成树，内存后端
Ran 301 tests in 12.420s
OK (skipped=5)
> $env:ASISTENT_STORE="sqlite"; python -m unittest discover -s tests -t .
Ran 301 tests in 13.047s
OK (skipped=5)                # 本次绿；但这两项存储测试本身不稳，见下面"既有缺陷"
> $env:ASISTENT_STORE="sqlite"; python -m unittest discover -s tests/mail -t .
Ran 46 tests in 5.973s
OK
```

**顺带发现的既有缺陷（与编码无关，属 `asistent/storage` 范围）**：
`tests/contracts/test_storage_contract.py::StoreV04ContractTest` 在 sqlite 后端下
`test_find_returns_the_earliest_n_not_the_latest` / `test_list_events_is_fifo_and_takes_the_earliest_n`
**不稳定**：单类单跑 8 次里 6 次红（`Ran 7 tests … FAILED`），干净 main（不含本分支改动）同样复现，
且这两个测试在测试发现顺序里排在 `tests/mail` **之前**，不可能由邮件代码引起；
同一份代码的全量 sqlite 复跑也时红时绿（最后一次 301/301 绿）。已作为发现上报 Lead，
需要 sqlite 存储负责人看 v0.4 的"最早 N 条"顺序语义实现（可能与时间戳分辨率/同刻并列有关）。

**设备（`ssh board`，`/usr/bin/python3` 3.10.12）——不再让环境变量帮忙**：

```
# 6) 默认 locale，无任何环境变量
3.10.12 utf-8 UTF-8
SUITE1_EXIT=0                  # 全量绿（137 项）
ENCODING_CLASS_EXIT=0          # 编码回归类 6/6 绿

# 7) 把父进程 stdout 压成 ASCII：LC_ALL=C + PYTHONUTF8=0 + PYTHONCOERCECLOCALE=0
parent stdout encoding = ascii ; preferred = ANSI_X3.4-1968 ; utf8_mode = 0
SUITE_ASCII_EXIT=0             # 全量仍绿
ENCODING_CLASS_ASCII_EXIT=0    # 编码回归类 6/6 绿
[evidence] stderr decode failures = 1                      # 坏 stderr 被标注计数
[evidence] stored raw bytes = 376 attachment bytes found = True U+FFFD found = False
# ASCII 控制台下证据行自动退化成 '?'，不再抛异常：
[evidence] write guard = {… 'message': '????????????send_email', …}
```

**"之前本机 131/131"是在什么环境下得到的**：每次命令都预先设置了
`$env:PYTHONIOENCODING="utf-8"`（设备那次是显式 `PYTHONIOENCODING=utf-8 /usr/bin/python3 …`）。
这个变量被**子进程继承**，于是子进程也用 UTF-8 收发 JSON-RPC，缺陷被完全掩盖——
那 131/131 只证明"UTF-8 控制台下绿"，不证明"控制台无关"。现在的结论以本节为准：
默认 cp936 控制台、敌对父环境、ASCII 控制台三种条件下都绿，且子进程编码由产品代码强制。

```
# 8) 只读探针自身也与代码页无关（本地 cp936 控制台，打自写假服务器冒烟）
> python tests/mail/real_mcp_probe.py --command <python> --args tests/mail/fake_mcp_server.py --call-tools
{"count": 11, …, "step": "tools/list"}
{"note": "get_emails_content ????????? readOnlyHint=false…", "step": "classification"}   # 中文退化成 '?'，不抛异常
EXIT=0        # 6 行 JSON
```

### 4.10 契约 v0.4 对齐（task-6：`content_origin` + `cursor_position` 拆分）

#### 4.10.1 两处对齐（改动与判据）

1. **摘要语义显式声明**：`McpMailSource._ref_from_metadata()` 构造引用时带
   `content_origin=CONTENT_ORIGIN_PROJECTION`（`"METADATA_PROJECTION"`）；
   同步层 `_content_origin()` 原样传到 `SourceDocument.content_origin`（新建与"内容代次+1"两条路径都传）；
   空串 → `None`＝未声明。新增回归测试断言**引用与文档**都带声明、且重放不会退回默认值。
2. **`cursor_position` 签名拆分**：
   - 契约形状 `cursor_position() -> Optional[Dict[str, Any]]`：无参、观测用，没有信息返回 `None`；
     数据来自最近一次 `list_new` 的位置快照（账号/文件夹/窗口起点 `since`/游标串/本次条数/观测时刻），
     **不发任何网络调用**；
   - 数值比较改名 `cursor_rank(cursor) -> Optional[float]`：同步层回退检测专用，**不是契约成员**，
     调用方 `getattr` 探测，缺失就跳过回退检测（降级为"不可比较"，不假装没有回退）；
   - 引擎新增 `source_position()`，`status()["source_position"]` 供控制台/巡检直接看"同步到哪了"。

```
# 引用/文档的来源声明（真实运行输出）
[evidence] mcp ref origins = ['METADATA_PROJECTION']
[evidence] mcp document origins = ['METADATA_PROJECTION']
[evidence] fixture origins = ['RAW_BYTES']                 # 契约自带 FixtureMailSource（原始字节通路）
[evidence] projection origin = METADATA_PROJECTION          # 投影型来源 → 文档原样继承
[evidence] undeclared origin = None                         # 未声明 → 不得当字节证明

# 观测 vs 比较
[evidence] cursor_position before listing = None            # 没有信息就是 None，不假装 0 进度
[evidence] cursor_position after listing = {"account": "fake", "cursor": "{\"account\": \"fake\", \"folder\": \"INBOX\", \"since\": \"2026-10-05T01:00:00+00:00\", \"v\": 1}", "cursor_version": 1, "folder": "INBOX", "listed_count": 2, "observed_at": "2026-10-10T07:08:36.475Z", "since": "2026-10-05T01:00:00+00:00", "window_start": "2026-09-10T07:08:36.470000+00:00"}
[evidence] cursor_rank = 1790812800.0 1791504000.0          # 单调（回退检测用）
[evidence] regression = 来源游标回退：3.0 -> 1.0 | attempts = 2
[evidence] source_position = {"cursor": "1", "since": "2026-10-05T01:00:00+00:00"}
[evidence] isinstance(mcp_source, MailSource) = True        # 可选能力不影响协议判定
# 只实现必需成员的最小来源（MinimalMailSource，无 cursor_position/cursor_rank）：
[evidence] minimal source sync = sync account=minimal@example.com folder=INBOX listed=1 new_doc=1 dup=0 cases=1 events=1 failures=0 quarantined=0 cursor_changed=True rebuilt=False complete=True
```

新增 11 项测试：`ContractV04AlignmentTest`（4）+ `ContentOriginTest`（3）+ `CursorCapabilityTest`（4），
`tests/mail` 从 46 → **57** 项。

#### 4.10.2 本地复跑（Windows，**默认 cp936 控制台，无任何环境变量**，真实仓库 `dd603bd`）

```
> python -m unittest discover -s tests -t .
Ran 312 tests in 13.079s
OK (skipped=5)          # 5 项 skip 全在 tests/ingest（Windows 上 mkfifo / 超大文件样例），与本包无关

> python -m unittest discover -s tests/mail -t .
Ran 57 tests in 6.790s
OK

> $env:ASISTENT_STORE="sqlite"; python -m unittest discover -s tests -t .
Ran 312 tests in 16.767s
OK (skipped=5)          # 本次全绿；但定序类**单跑仍会抖**，见 §4.10.4 第 3 条

> ASISTENT_STORE=sqlite python -m unittest discover -s tests/mail -t .
Ran 57 tests in 6.394s
OK
```

**范围核验**（我没有碰契约）：

```
> git diff --name-only HEAD -- asistent/contracts tests/contracts        # 工作区 vs HEAD
(空)
> git diff --name-only $(git merge-base HEAD main) HEAD -- asistent/contracts tests/contracts
(空)
> git status --porcelain
 M asistent/mail/__init__.py
 M asistent/mail/mcp_stdio.py
 M asistent/mail/sync.py
 M tests/mail/fake_mcp_server.py
 M tests/mail/fakes.py
 M tests/mail/test_mail_sync.py
 M tests/mail/test_mcp_stdio.py
```

#### 4.10.3 设备复跑（`ssh board`，`/usr/bin/python3` **3.10.12**）

```
=== 环境 ===
py 3.10.12 | stdout utf-8 | preferred UTF-8 | fs utf-8 | utf8_mode 0
LANG=en_US.UTF-8

=== A) 默认 locale 全量 ===
DEVICE_DEFAULT_EXIT=0
Ran 312 tests in 10.414s
OK
跳过项计数 = 0

=== D/E/F) 邮件包专项 ===
DEVICE_MAIL_EXIT=0         Ran 57 tests in 6.563s OK      # 默认 locale
DEVICE_ASCII_MAIL_EXIT=0   Ran 57 tests in 6.713s OK      # ASCII 控制台（LC_ALL=C，stdout/fs=ascii）
DEVICE_SQLITE_MAIL_EXIT=0  Ran 57 tests in 6.403s OK      # sqlite 后端

=== C) sqlite 后端全量 ===
DEVICE_SQLITE_EXIT=1
Ran 312 tests in 10.647s
FAILED (failures=1)
FAIL: test_list_events_is_fifo_and_takes_the_earliest_n (tests.contracts.test_storage_contract.StoreV04ContractTest)
      # 本分支当时还没有 main 的 v0.5 定序修复（task-7）；叠加后全绿，见 §4.10.5

=== H) 子进程自报编码（编码强制项） ===
ENVCHECK stdout=utf-8 stdin=utf-8 utf8_mode=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8
```

设备暂存说明（避免误判）：第一次只拷了 `asistent/` + `tests/`，`tests/ingest` 依赖仓库根的
`fixtures/public/**` 样例资料，于是出现 2 项 `new_documents >= 4` 失败（`0 not greater than or equal to 4`）。
把**整个仓库**（含 `fixtures/`）暂存后，同样的命令得到 `Ran 312 tests … OK`、0 跳过——
那 2 项是我的暂存疏漏，不是产品缺陷（设备上不做任何 git 写操作，只在 `/tmp` 下工作）。

#### 4.10.4 本轮顺带查出的三件事（都不在 `asistent/mail/**` 写作用域内，交 Lead 裁决）

1. **非 UTF-8 文件系统编码下 argv 传不了非 ASCII**（本包已做产品侧加固）：
   设备 `LC_ALL=C`（`fs=ascii`）时，`os.fsencode("被改写的主题")` 直接
   `UnicodeEncodeError: 'ascii' codec can't encode characters in position 0-5`——
   假服务器的 `--mutate-subject-after-list 被改写的主题` 就是在 `Popen` 阶段炸的，
   表现为"列取新邮件未预期失败"。这是**父进程**在编码 argv，强制子进程 UTF-8 救不了。
   - 产品侧：`start()` 捕获 `UnicodeEncodeError` → `McpCrashed`，detail 带
     `fs_encoding` + `hint`（换 UTF-8 locale / 用文件或其它通道传非 ASCII），且只带 argv[0] 文件名（不回显完整 argv，防凭据外泄）；
   - 夹具侧：假服务器新增 `--mutate-subject-file`（从 UTF-8 文件读改写主题），冲突测试改走文件；
   - 结果：`tests/mail` 在设备 ASCII 控制台下也从"1 项红"变成 **57/57 绿**（§4.10.3 E 段）。
     ```
     [evidence] spawn failure = MCP_CRASHED | 无法启动 mcp 子进程：命令行参数含当前文件系统编码（utf-8）无法表示的字符：'ascii' codec can't encode characters in position 0-5: ordinal not in range(128)
     [evidence] spawn detail = {"command0": "fake-server", "fs_encoding": "utf-8", "hint": "改用 UTF-8 locale（例如 LANG=C.UTF-8），或把非 ASCII 内容通过文件/环境之外的通道传入"}
     ```
2. **仓库里其它 MVP 的测试在 ASCII locale 下会红（47 项 ERROR，与邮件包无关）**：
   同一个设备 ASCII 控制台下 `Ran 312 tests … FAILED (errors=47)`，模块分布：
   `tests.ingest.test_query_chinese` 19、`tests.contracts.test_archive_contract` 8、
   `tests.ingest.test_citations` 7、`tests.ingest.test_pipeline_freshness` 6、
   `tests.contracts.test_connector_contracts` 4、`tests.ingest.test_demo_evidence` 2、
   `tests.storage.test_sqlite_schema` 1。典型报错是在**ASCII 文件系统编码**下建/读中文名文件：
   `File ".../test_archive_contract.py", line 30, in _write` → `os.makedirs` →
   `UnicodeEncodeError: 'ascii' codec can't encode characters in position 31-33`。
   这类失败**不是产品缺陷**（OS 在该 locale 下根本表示不了中文文件名），
   可选的收口方式是：测试在 `sys.getfilesystemencoding()` 无法表示夹具名时 `skipTest(...)`，
   或在 CI/设备上统一用 UTF-8 locale（`LANG=C.UTF-8`）。当前主线口径是"设备默认 locale 全绿"（§4.10.3 A）。
3. **sqlite 定序 flake 已由 main 的 v0.5 修复（task-7）**：本分支（没有该修复）下
   `ASISTENT_STORE=sqlite` 全量偶发红、`StoreV04ContractTest` 单跑 6 次红 4 次
   （`test_find_returns_the_earliest_n_not_the_latest` / `test_list_events_is_fifo_and_takes_the_earliest_n`）；
   与邮件代码无关（干净 main 同样复现），根因是毫秒精度时间戳 + 随机 id 做平局判据。

#### 4.10.5 合并 main（v0.5）后的预览结果

本分支当前 `dd603bd` 落后 main 16 个文件（v0.5 定序修复 + 文档）。我没有 git 写权限，
所以用 `git archive main -o … -- <16 个路径>` 把 main 的最新文件**叠加**到一份拷贝上跑，作为合并预览：

```
# 预览树 = 本分支工作区 + main 最新 16 个文件（contracts v0.5）
> python -m unittest discover -s tests -t .            # 默认 cp936
Ran 325 tests in 14.124s
OK (skipped=5)
> $env:ASISTENT_STORE="sqlite"; python -m unittest discover -s tests -t .
Ran 325 tests in 14.422s
OK (skipped=5)
> python -m unittest discover -s tests/mail -t .
Ran 57 tests in 6.203s
OK
# v0.5 定序修复消除抖动（同一命令单跑 6 次）
StoreV04ContractTest => OK OK OK OK OK OK            # 合并前同一命令 6 次里红 4 次
```

结论：**再合并一次 main（v0.5）即双后端全绿**，且 v0.5 只动了 `reference/version/storage/
tests/contracts/tests/storage/contracts/**`，未改 `contracts/mail.py` / `objects.py`，
所以本轮 `content_origin` 对齐不需要二次改动（`git diff HEAD main -- asistent/contracts/mail.py
asistent/contracts/objects.py` 为空）。

---

## 5. 已知限制

1. **没有 RFC822 原文**：MCP 只暴露结构化字段，`fetch_raw()` 返回**确定性重建**的 `.eml`。
   `vendor_message_id` 不可得，所以身份走契约第二条路径（邮箱作用域 ID + 内容摘要），
   而"内容摘要"是 `metadata_projection()`（结构字段投影）的摘要，不是原始字节摘要。
   原文级校验（DKIM/签名/精确字节比对）在 MCP 通路上做不到。
2. **`get_emails_content` 的服务端标注是 `readOnlyHint=false`**（因为 `mark_as_read=true` 会改远端标记）。
   适配器恒发 `mark_as_read=false` 并把它当只读用；若某个版本忽略该参数、仍然标记已读，
   就必须改用只读账号或 IMAP 直连（未验证，见 §6）。
3. **游标语义依赖服务端 `date` 字段**：`since` + 1 小时重叠窗口（可配 `overlap_seconds`）。
   服务端若给出稳定 UID 游标更好；当前实现靠"重复列取 → 幂等吸收"兜住窗口重叠。
4. **正文分页靠 `...[TRUNCATED]` 标记**：标记语义变化会导致多取/少取；已设 `max_body_pages` 上限并
   在超限时报 `McpContentError`（不静默截断）。
5. **附件只记录"有附件"**：`get_attachment_content` 未接入；`download_attachment` 是写工具，明确拒绝。
6. **会话不做跨邮件聚合**：线程键取单封邮件的 `Message-ID`/`In-Reply-To`/`References` 归一，
   没有"同一事务的多次沟通合并成一个 Case"的语义（留给后续 MVP）。
7. **状态文件无跨进程锁**：同一账号的同步必须串行调用；单进程内由引擎顺序执行保证。
8. **退避策略简单**：只有 `max_attempts=3` + 隔离，没有指数退避/配额感知（见 §6）。
9. **未验证真实信箱**：所有通路证据来自假 stdio 服务器 + 真实服务器的**只读**握手/列表；
   真实收发、真实字段差异、限流行为都还没有实测数据。
10. **UTF-8 强制项只对 Python 子进程有"标准"保证**：`PYTHONUTF8`/`PYTHONIOENCODING` 是
   Python 解释器读的（也顺手覆盖 `uvx`/`npx` 启动的 Python 型 MCP 服务器）。若将来换成
   非 Python 的 MCP 服务器且它按本地代码页收发，父进程会**显式报错**（`McpProtocolError`
   + 字节偏移），不会静默替换；届时需要给它加"用 UTF-8"的启动参数或做一层包装。
11. **`content_origin` 只声明"摘要语义"，不等于完整性证明**：MCP 通路的 `content_hash` 是
   重建字节的摘要（§2.9），下游拿到 `METADATA_PROJECTION` 就必须降级处理；真正的字节级校验
   只能等 IMAP 直连（`RAW_BYTES`）或服务端提供原始字节。
12. **非 UTF-8 文件系统编码下 argv 传不了非 ASCII**（§4.10.4）：产品侧已显式报错，
   但真实部署若把非 ASCII（例如中文路径）写进 MCP 服务器命令，仍需要 UTF-8 locale；
   当前实现不会静默降级，也不会把凭据回显到错误详情里。
13. **仓库内其它 MVP 的测试在 ASCII locale 下会红（47 项）**：设备 `LC_ALL=C` 全量实测
   `errors=47`，集中在 `tests/ingest/**` 与 `tests/contracts/**`（ASCII 文件系统编码下建中文名文件），
   与邮件包无关（本包在同样条件下 57/57 绿）。要么这些测试自己 skip，要么统一用 UTF-8 locale。
14. **sqlite 定序 flake**：本分支（未含 main 的 v0.5 修复）下 `ASISTENT_STORE=sqlite` 偶发红；
   合并 main 后消除（§4.10.4、§4.10.5）。

---

## 6. 凭据接入清单（TODO，接入真实邮箱时必须逐项确认）

1. **配置文件位置**：实测 stderr 显示服务端读 `~/.config/mcp-email-server/config.toml`
   （"Loading legacy settings from …"，即使我传了 `XDG_CONFIG_HOME`），
   即 `mode=legacy` 时以该路径为准；若改用新版配置目录，需要确认服务端版本的路径规则。
2. **账号名即身份域**：`account` 参与身份键与线程键；改名 = 全量重新入库（会产生新对象）。
   接入前定好账号命名并写进 `mail-sync` 的调用参数。
3. **首次回溯窗口**：`lookback_days` 默认 30 天（`McpMailSource`）。真实信箱要先小范围试跑
   （`page_size=10`、单文件夹），确认总量与耗时再放大，避免一次拉爆。
4. **只读性再验证**：先用 `tests/mail/real_mcp_probe.py --call-tools` 确认 `list_available_accounts` /
   `list_mailboxes` 正常；再跑一次单文件夹小窗口同步，检查信箱里**没有**已读状态变化（限制 §5.2）。
5. **字段差异实测**：`date` 格式（本适配器同时支持 RFC 2822 与 ISO-8601）、`message_id` 缺失比例、
   非 UTF-8/损坏编码主题、附件命名——都要用真实数据回填测试夹具。
6. **限流与配额**：每个账号的列取频率/每日上限未知；需要真实观测后再决定退避与批量大小
   （当前只有 `max_attempts=3` + 隔离）。
7. **文件夹范围**：只同步 `INBOX` 还是包含归档/垃圾箱；多文件夹 = 多游标（已支持），但需要运维约定。
8. **凭据落位**：凭据只放 `~/asistent-var/config/` 下所指的服务端配置文件（700），
   **不进仓库、不进日志、不写进夹具**。注意子进程默认继承宿主环境，因此不要把密钥塞进 `env=`；
   若确实需要传递，`env=` 里键名像密钥的值会被自动登记进 `Redactor` 做兜底脱敏。
9. **首轮结果复核**：同步后检查 `engine.status()`（`failures`/`quarantined` 应为空）、
   `report.complete is True`、`RawStore` 中原文可读、`audit_trail` 无 `FAILED` 行。

---

## 7. 契约问题（CP 提案）

> CP-1 / CP-2 已被 Lead 裁决并落到契约 v0.4，本分支已在 task-6 按裁决改完（§7.1、§4.10）；
> CP-3 / CP-4 仍未纳入，下列保留**原始提案文本**以便追溯。

- **CP-1（`MailMessageRef`）**：契约假设来源能提供原始字节（`imap_raw_to_ref(raw=...)`），但 MCP 通路
  只能给结构化字段。建议增加可选标志（如 `content_origin: "raw" | "projection"`），
  让下游能区分"原文摘要"与"元数据投影摘要"，并据此决定身份强度与归档保真度。
- **CP-2（`MailSource` 协议）**：回退检测需要"比较游标先后"，当前协议没有这个方法；
  我用 `getattr(source, "cursor_position", None)` 做兼容。建议把
  `cursor_position(cursor) -> Optional[float]` 作为可选方法写进协议文档，所有来源统一实现。
- **CP-3（失败对象）**：失败目前只存在于状态文件 + 审计 `FAILED` 行（`detail.failure_class`），
  没有一个可被检索/归档的一等对象。若要"待办式"重试，建议契约层确认失败是否应成为
  独立对象类型（例如 `MailSyncFailure`），而不是只留在审计里。
- **CP-4（审计语义）**：幂等验收只能比较**非审计**计数（契约明确"审计只追加、不改写"，
  重复投递必然可能追加尝试行）。建议在 SPEC/契约里显式写明"重复投递允许追加审计行，
  但对象计数必须不变"，否则后续实现容易把"审计条数必须相同"当成硬指标。

### 7.1 对齐状态（main 已到 contracts v0.5；本分支 base 已合并 v0.4）

Lead 已裁决 CP-1/CP-2 并在契约里落地，本轮（task-6）已按裁决改完代码，原始输出见 §4.10：

| CP | 契约状态 | 本分支动作（已完成/待办） |
| --- | --- | --- |
| CP-1 内容来源声明 | v0.4 已加 `MailMessageRef.content_origin`（默认 `"RAW_BYTES"`，CP-20261010-07） | ✅ **已完成**：`McpMailSource` 构造 ref 时显式 `content_origin=CONTENT_ORIGIN_PROJECTION`；同步层 `_content_origin()` 原样带到 `SourceDocument.content_origin`（含内容代次更新）；空串 → `None`；无 `hasattr` 兜底（§2.9、§4.10.1） |
| CP-2 顺序语义 | v0.4 把"当前同步位置"定为 `MailSource` **可选能力**（CP-20261010-08），形状 `cursor_position(self) -> Optional[Dict[str, Any]]` | ✅ **已完成**（按 Lead 裁决拆分）：无参观测 `cursor_position()` 返回字典、无信息返回 `None`；数值比较改名 `cursor_rank(cursor)`，标注为**非契约**的实现级能力，调用方 `getattr` 探测；`status()["source_position"]` 供控制台观测；最小来源仍满足 `MailSource`（§4.10.1） |
| CP-3 失败一等对象 | v0.4/v0.5 未纳入 | 保持现状：状态文件 `failures` + 审计 `FAILED` 行 + `quarantined()`（等后续版本裁决） |
| CP-4 幂等表述 | 未纳入 | 保持现状：文档与测试只比较非审计计数（等后续版本裁决） |

**待 Lead 执行的一步**：把 main（v0.5）再合并进本分支。v0.5 只动
`asistent/contracts/{reference,version}.py`、`asistent/storage/**`、`tests/contracts/test_storage_contract.py`、
`tests/storage/**`、`contracts/**`、文档，**未改 `contracts/mail.py`/`objects.py`**，
所以本轮对齐无需二次改动；合并后 `git diff --name-only main -- asistent/contracts tests/contracts`
才可能为空（当前非空纯粹因为 main 领先 16 个文件，我一行契约都没碰，证据见 §4.10.2）。
合并预览已在叠加树上验证：双后端 325/325 全绿、`tests/mail` 57/57（§4.10.5）。
