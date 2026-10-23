# MVP-8：常驻服务 + 纵向端到端（`asistent/service/**`、`scripts/run-service.sh`、`tests/service/**`）

- 分支：`mvp/8-service`（worktree：`D:\dsh\个人助手\.worktrees\mvp-8-service`）
- 起点：`1cc1113`（`merge(mvp-6)`；该树里 `asistent/contracts` 自报 `CONTRACT_VERSION = "v0.7"`，
  `asistent/archive/**` 的 manifest `schema_version = mvp7-archive-manifest-1` 可用，全套 38 项在双后端全绿）
- 交付物：`asistent/service/{__init__,config,phases,runtime,health,answers,mail_source,status_artifact,cli,__main__}.py`、
  `scripts/run-service.sh`（设备端唯一入口）、`tests/service/**`（38 项测试 + 设备取证脚本 + 端到端演练脚本）、本文档
- 结论：本地（Windows / Python 3.12.14）`python3 -m unittest discover -s tests/service -t .`
  在 **reference 与 sqlite 两个后端**都全绿（38 项；reference 跳过 9 项，全部是"跨进程/落盘才能证明"的用例，
  理由在跳过消息里写明）；纯标准库、Python 3.10 兼容（无 `match`、无 `X | Y` 注解、无 `datetime.UTC`/`TaskGroup`）；
  纵向端到端离线跑通（`tests/service/demo_vertical.py`，§4.2）；**恰好一次外发**有账本断言（submit 行数恒为 1）；
  **重启不重发**有跨进程强杀取证（§4.3）；手机端控制台真的能下载健康快照并提交审批（§4.4）。
  ⚠️ ~~**真机（`board`）运行尚未完成**~~ → **已完成**（2026-10-12，设备提交 `198f442`，
  原始输出见 `docs/evidence/mvp-8-device-20261012.log`，原始结论见 §4.5）；那次取证还抓出并修掉了
  4 个"本机全绿、真机才露"的真缺陷（§4.5.2）。
  另注：合并后服务又补了**真实邮件通道接线**（`mail.source=mcp`）：真实收信要等你录入授权码（U-5），
  在此之前服务如实记账"缺授权码"，不假装收信。
- 边界：`asistent/contracts/**`、`tests/contracts/**` 与其它已交付模块（`ingest/index/mail/understand/actions/growth/console/archive/pan`）**零改动**；
  本轮只写上述四个写作用域。

---

## 1. 目标与验收对照

### 1.1 SPEC §873-877 三条 Given/When/Then

| SPEC 条目 | 状态 | 证据 |
| --- | --- | --- |
| §873 常驻服务：关掉电脑/关闭桌面窗口后，助手仍按间隔自己跑轮次，手机端能看到待处理与审批状态 | ✅ 离线全证：`--loop` 一个进程连跑多轮，期间**客户端连接为 0**；健康快照写成文件对象，手机端用同一令牌 `GET /api/artifacts` → `/content` 下载；`run-service.sh start` 用 `setsid + nohup` 脱离终端，另有 `systemd --user` unit（`unit-file`） | §4.4、§4.5、§3.6 |
| §874 一条通知走完全程：同步 → 索引/检索 → 理解（含时间语义） → 需要时提问 → 用户回答 → 生成草稿 → 批准 → 外发 → 归档 | ✅ `tests/service/test_service_e2e.py::TestVerticalE2E` + `demo_vertical.py` 全程原始输出：1 封通知 → `Case` 1 个 → `Fact` 3 条（含 `2026-10-16T10:00:00.000Z`/`2026-10-17T06:00:00.000Z`）→ 草稿 1 版 → 用户批准 → 外发 1 次 → 归档 `VERIFIED` | §4.2 |
| §875 一次外部动作**恰好一次**：重启/重跑都不许重发 | ✅ 三重取证：①第三轮 `executed=[]` 且账本 submit 行数 = 1；②外发前被强杀（`os._exit(9)`）→ 重启后动作降级 `UNKNOWN`、`resent_action_ids=[]`、`sender_call_count=0`、仍只有 1 行 submit；③已授权未执行的动作用"关通道"配置跨重启后**只发一次** | §4.2、§4.3 |
| §876 "规则纠正后，下一封同类通知命中新规则版本，版本号在审计里可见"（**必须真的接上 `asistent/growth`**） | ✅ 用真 `asistent.growth`：v1 命中写 `RULE_HIT`（`detail.rule_id/rule_version`）并在问题文案里显示 `v1`；纠正后 v2 `ENABLED`、v1 `SUPERSEDED`；下一封同类通知的 `RULE_HIT.rule_version=2` | §4.3 |
| §877 归档清单逐文件哈希校验通过，且能在手机端看到 | ✅ `manifest.entries[].sha256/size_bytes` 与磁盘逐文件比对全通过；`policy_gate`、`secret_scan.clean=true`、`content_origin` 分类齐全；清单可从模拟远端**读回**校验 | §4.2 |

### 1.2 本轮附加硬要求

| 要求 | 落实 | 证据 |
| --- | --- | --- |
| 一轮的失败不许接管服务（一次同步失败 ≠ 服务退出） | ✅ 八个阶段各自 `try/except` 隔离（`BaseException` 之外全兜住），失败记 `phase_failures`/`last_error` 后继续下一阶段、下一轮；非 `Exception`（`SimulatedCrash`）**不吞** | §4.3、§5.3 |
| 间隔/开关全部来自**仓库外**配置 | ✅ `$ASISTENT_VAR/config/service.json`（`load_config` 只读这一个源，环境变量只能覆盖路径/间隔/后端）；仓库内没有任何运行期配置 | §3.1 |
| 缺凭据也能起来（用户动作 U-1/U-2 未做完时的真实状态） | ✅ 7 项用例（另加 `test_service_console_http.py` 的"控制台关掉就一个端口都不开"）：没有配置文件、没有邮箱适配器、邮件目录不存在、网盘未授权/未审批、令牌文件缺失、state 文件损坏——服务**照常启动并继续跑轮次**，每一路都写"暂时不可用 + 原因 + 下一步动作" | §4.6、§5.7 |
| 崩溃自愈且不重发 | ✅ 见 §3.5；`recover_phase` 是**只读**的（源码级断言：不含 `.send(`/`execute(`/`execute_request`） | §4.3 |
| 优雅 SIGTERM/SIGINT 停止 | ✅ 信号只置停止标志，不打断当前轮；`run-service.sh stop` 等它自己退（POSIX 上已验证；Windows 上 `terminate()` 是强杀，用例显式 skip 并说明） | §4.4、§4.5 |
| 离线端到端用**受控假通道**，并断言通道调用次数 == 1 | ✅ `ScriptedMailSender` + `MailCallLog`：`submit` 行数 == 1；`ActionEngine.sender_call_count()` 也被断言 | §4.2、§4.3 |
| 常驻证据来自 `--once` 分轮 + 状态从**存储**恢复（不是进程内存） | ✅ sqlite 后端每轮**一个新进程**：第 2、3 轮不重复理解、不重复外发，全部靠存储里的 `Event`/审计/`Approval`/动作状态重算 | §3.4、§4.3 |
| `--once` 与常驻用**同一个**实现 | ✅ `run_loop(max_rounds=1)` 就是 `--once`；`main()` 里只有"跑几轮"的差别 | §3.2 |

### 1.3 三条抽查点（最容易假装的部分）

| 抽查点 | 落实 | 证据 |
| --- | --- | --- |
| ① "跑起来了"不是"打印了一行日志"：必须是**没人连它也在轮转**，且手机端读到的数字与日志一致 | ✅ 常驻测试先连跑 2 轮（期间 HTTP 请求数 = 0），再下载快照比对 `rounds_completed`；`run_once` 的顺序被钉成"先落盘 + 先发布快照，最后才打印本轮摘要"（否则日志说第 2 轮、快照还是第 1 轮） | §4.4 |
| ② "恰好一次"不是"我说只发了一次"：必须有**盘上账本**与**强杀后重启**两个方向的取证 | ✅ 账本 `outbound-calls.jsonl` 的 submit 行数；`os._exit(9)` 强杀在"已提交、结果未落库"的窗口里，重启后 `UNKNOWN` + 0 次通道调用 | §4.2、§4.3 |
| ③ "规则纠正"不是把版本号写死：必须真的走 `asistent/growth` 的版本链，且下一封**同类**通知命中的是新版本 | ✅ `RuleScope(effect=slot_split.require_question, slots=(event_start,event_end), kind=contact, ref=rules@example.org)`；`enable_new_version` → v2；下一封通知 `RULE_HIT.rule_version=2` | §4.3 |

---

## 2. 范围与交付物

| 文件 | 作用 |
| --- | --- |
| `asistent/service/config.py` | 仓库外配置：`ServiceConfig` + 各段（console/mail/ingest/outbound/archive）；安全默认（邮件关闭、外发改 `none`、归档关闭）；`describe()` 永不含令牌内容；未知通道**直接报错**而不是静默降级 |
| `asistent/service/phases.py` | **一轮的唯一实现**：`recover → sweep → sync → ingest → understand → prepare → outbound → archive`；`RunContext`/`PhaseResult`/`RoundReport`；归档通道不可用时的结构化结论 |
| `asistent/service/runtime.py` | `ServiceRuntime`：接线（存储/邮件引擎/动作引擎/归档引擎/控制台）、`run_once`、`run_loop`、信号处理、健康快照、`status()` |
| `asistent/service/health.py` | 待处理计数 `snapshot_pending`、`infer_waiting_on`（"现在在等谁"）、`ServiceState`（原子写、损坏不致命、跨重启续上累计计数） |
| `asistent/service/answers.py` | 用户回答并入再理解的兜底（v0.7 的 `Question` 没有 `kind`，见 §7.4）；冲突问题**永不**被回答"解决" |
| `asistent/service/mail_source.py` | 离线夹具邮件源（`fixture`）：uid 稳定、摘要按原始字节、头部解码用 `decode_eml_bytes`；目录缺失**显式报错**而不是"今天没信" |
| `asistent/service/status_artifact.py` | 把健康快照发布成手机端可下载的 `FileArtifact`（确定性 ID、内容未变则跳过、出现凭据则拒绝发布） |
| `asistent/service/cli.py` / `__main__.py` | `--once/--loop/--status/--init-token/--config/--interval/--max-rounds/--console/--quiet/--json`；退出码 0/1 有明确含义 |
| `scripts/run-service.sh` | 设备端入口：`config-init / init-token / doctor / once / start / stop / restart / status / logs / crontab-line / unit-file`；`umask 077`；凭据不进 argv/日志 |
| `tests/service/support.py` | 仓库外工作区 + 配置工厂 + **新进程**轮次驱动 + 常驻进程（行缓冲读取、端口解析、优雅停止） |
| `tests/service/test_service_unit.py` | 20 项：配置安全默认、快照计数与"在等什么"、阶段顺序与隔离、回答并入、夹具邮件源 |
| `tests/service/test_service_e2e.py` | 4 项：纵向全程、无客户端两轮连跑、第三轮干净空转、规则版本可见且递增 |
| `tests/service/test_service_bare_start.py` | 7 项：**没有凭据也起得来**（无配置/无适配器/目录缺失/网盘两道闸门/state 损坏/令牌自动生成） |
| `tests/service/test_service_console_http.py` | 5 项：真 HTTP（快照下载、审批提交、401、优雅停止、关掉控制台就一个端口都不开） |
| `tests/service/test_service_crash.py` + `crash_child_service.py` | 2 项：强杀后 `UNKNOWN` 且永不重发；已授权动作跨重启恰好发一次 |
| `tests/service/demo_vertical.py` | 一条命令跑出文档 §4.2 的全部原始输出（跑完不删工作目录，方便自己翻） |
| `tests/service/device_console_probe.py` | 设备端用真 HTTP 读健康快照/待处理/审批（纯标准库，设备没 curl 也能取证） |
| `tests/service/device_run.sh` | 设备端取证脚本：环境事实 → 单轮 → 启动器自检/systemd unit → 常驻两轮 → 手机端接口 → 审批与外发恰好一次 → 优雅停止 |

---

## 3. 接线拓扑与一轮做什么

### 3.1 拓扑（谁在什么时候动）

```text
        $ASISTENT_VAR/config/service.json          ← 所有开关/间隔/路径都在仓库外
                 │
                 ▼
  scripts/run-service.sh  ─(setsid+nohup 或 systemd --user)─►  python3 -m asistent.service --loop --console
                 │                                                        │
                 │                                                        ▼
                 │                                              ServiceRuntime.run_loop
                 │                                                        │  每 interval 秒
                 │                                                        ▼
                 │                                              run_once()  ← 唯一的一轮实现
                 │                                                        │
                 │   ┌────────────────────────────────────────────────────┴──────────────────────────────────┐
                 │   │ recover → sweep → sync → ingest → understand → prepare → outbound → archive           │
                 │   └───┬────────┬───────┬────────┬───────────┬───────────┬──────────┬──────────┬─────────────┘
                 │       │        │       │        │           │           │          │          │
                 │   (只读重算) (过期审批) │   FTS5 索引  理解+growth   动作派生    Action     Archive
                 │                │   MailSyncEngine  SearchPipeline  Understander  Engine     Engine
                 │                │                                                              │
                 │                └──────────────► 存储（SQLite WAL 或参考实现）◄───────────────┘
                 │                                        │
   健康快照 ──► $ASISTENT_VAR/state/service-status.json ─┘   （登记成 FileArtifact，手机端可下载）
   崩溃缓存 ──► $ASISTENT_VAR/state/service-state.json        （累计计数/最近错误；**不**决定下一轮做什么）
   外发账本 ──► $ASISTENT_VAR/state/outbound-calls.jsonl      （每次通道调用的原始记录）
   日志     ──► $ASISTENT_VAR/logs/service.log（600）
                 │
                 ▼
        手机端/电脑端控制台（HTTP + Bearer 令牌，令牌只在 $ASISTENT_VAR/config/console-token，600）
```

### 3.2 一轮（`run_phases`）八个阶段

| 阶段 | 做什么 | 关键约束 |
| --- | --- | --- |
| `recover` | 枚举未完成的工作：`IN_FLIGHT` 的外部动作 → `UNKNOWN`；Case `EXECUTING` → `UNKNOWN` | **绝不调用通道**（源码级断言）；`resent_action_ids` 恒为空 |
| `sweep` | 过期审批作废（`expire_due`） | 只作废、不代批 |
| `sync` | 邮件同步（`MailSyncEngine` + 通道）：新邮件 → `SourceDocument` + 原始字节落盘 | 通道缺失/目录不存在 → 如实报错，不做假通道 |
| `ingest` | 采集/索引：FTS5 每字符 CJK 切分 + 短语查询 | 只索引真实字节；索引状态可查 |
| `understand` | 理解（`Understander` + `asistent.growth` 规则命中）→ 事实/推断/任务/草稿；需要时产生 `Question`；**用户回答并入再理解** | 已理解的资料与已并入的回答**幂等跳过**（不刷审计）；规则冲突时暂停自动分析（SPEC §861） |
| `prepare` | `READY_FOR_REVIEW` → 硬闸门 → 生成确定 ID 的外发动作 → 把**用户审批**投影成动作级审批并授权 | 没有 `ACTIVE` 审批就不派生动作；动作 ID 由 `(case_id, version_id)` 派生，重跑不产生第二个动作 |
| `outbound` | 领取动作（`claim_action`，原子）→ `ActionEngine.execute` | 通道为 `none`/未配置 → 跳过并说明；执行结果三态（成功/确定失败/未知）分别落到 Case 与动作 |
| `archive` | `SUCCEEDED/COMPLETED` 的事务：暂存材料 → 冻结快照 → manifest → 上传 → 逐文件回读校验 → 完成标记 | 未授权/未审批 → **结构化结论**（不是整轮失败），一次写调用都不发 |

### 3.3 失败与重试策略

| 情形 | 处理 | 用户能看到什么 |
| --- | --- | --- |
| 单个阶段抛异常 | 该阶段记 `error_class/error_text`，其余阶段照跑；轮次 `ok=False`，`--once` 退出码 1 | `last_error`、`phase_failures`、日志里有错误类名 |
| 邮箱通道不可用（缺授权/目录缺失） | `sync` 阶段失败并记账，服务继续轮转；恢复后自动补读（游标 + 内容身份去重） | `notes` 里"这一路为什么没干活 + 下一步动作" |
| 外发结果**未知**（提交后进程死/超时） | 动作置 `UNKNOWN`，**不重试**；等人/等通道明确 | `pending.actions_unknown`、`waiting_on` 含 `channel`，需人工裁决 |
| 外发**确定失败** | 允许重试（同一幂等键，通道自己拒绝重复键） | 动作 `FAILED`，账本里有 `fail` 行 |
| 网盘未授权/未审批/未安装 | 归档只记账：`error_code` ∈ `{NOT_AUTHORIZED, PAN_NOT_INSTALLED, APPROVAL_REQUIRED}`、`next_step` 写明怎么修 | `waiting_on` 含 `pan`，手机端一眼看到"在等你授权" |
| 控制台起不来 | 记 `state.notes`，服务本体照跑 | `notes` 里有原因（端口占用等） |
| state 文件损坏 | `ServiceState.load` 返回空状态 + 一条说明，服务照跑 | `notes` 里有"state 文件不可读" |

**重试的安全前提**：所有重试都建立在"外部结果未知 ≠ 可以再发一次"之上——顺序是
`claim_action`（落盘提交）→ 调通道 → 落结果；任何一步断在中间，恢复时只会得到 `UNKNOWN`。

### 3.4 "状态只来自存储"（常驻能跑对的前提）

每轮开始时**不读进程内存里的任何"上一轮结论"**，而是从存储现算：

| 决策 | 依据 |
| --- | --- |
| 这份资料理解过了吗 | `服务理解` 审计行里的 `document_id/content_hash`（与 `Event.payload` 取并集——见 §7.3） |
| 用户的回答并入了吗 | 该 `Answer` 是否已有引用它的 `user_answer` 事实 |
| 这个版本能发了吗 | `Approval` 是否存在 `ACTIVE` 且 `target_object_ids == [version_id]` |
| 这个动作发过了吗 | `ExternalAction` 状态 + 幂等键 + `Attempt` 记录 |
| 有没有悬挂的动作/事务 | `recovery_scan()` + Case `EXECUTING` |

`service-state.json` 是**展示缓存**（累计轮数、最近错误、最近同步/索引），跨重启会续上，但**从不**用来决定"下一轮做什么"。

### 3.5 崩溃自愈（重启不重发）

```text
动作已提交给通道（账本已有 submit 行）
        │  ← 进程在这里被强杀（os._exit(9)）
        ▼
存储里：ExternalAction=IN_FLIGHT，Case=EXECUTING，没有结果
        ▼  重启（新进程）
recover_phase：IN_FLIGHT → UNKNOWN；Case EXECUTING → UNKNOWN（不碰通道）
        ▼
准备阶段：这个动作不是 AUTHORIZED，**不会**被再次领取
        ▼
结论：UNKNOWN（要在人/通道明确后才动） + 账本 submit 行数仍然是 1
```

`Restart=always`（systemd）或 `@reboot`（crontab）只是"再来一次"，**不会**变成"再发一次"。

### 3.6 手机端怎么看到健康快照

控制台的 `/api/overview` 看不到"服务本身"的状态，所以本层每轮把一份**给人看的快照**写到
`$ASISTENT_VAR/state/service-status.json`（600），并登记成 `FileArtifact`：

* `case_id=""`、`name="service-status.json"`、ID 确定性（重启不堆垃圾）、内容未变则跳过、版本号递增；
* 内容含：`rounds_completed/rounds_failed`、`pending`（九项计数）、`waiting_on`、`last_sync/last_ingest`、
  `last_archive`（含 `next_step`）、`notes`（每一路为什么没干活）、`status_artifact` 自身、`console_url`；
* **发布前扫明文**：只要控制台令牌出现在快照里就拒绝发布（宁可手机端看不到，也不能把凭据送出去）；
* 读取路径：`GET /api/artifacts`（找 `service-status.json`）→ `GET /api/artifacts/<id>/content`；
  ⚠️ 真机配置必须把 state 目录放进 `console.artifact_roots`（`run-service.sh config-init` 生成的配置已包含），
  否则会 404「文件不在允许下载的目录内」（这是本轮实测踩到的一条，见 §7.2）。

---

## 4. 原始证据

> 全部命令都在仓库根目录执行。`ASISTENT_STORE` 切后端（默认 `reference`）。

### 4.1 双后端全套测试

```sh
$ python3 -m unittest discover -s tests/service -t . -p 'test_*.py'      # ASISTENT_STORE=reference
Ran 38 tests in 9.691s
OK (skipped=9)

$ ASISTENT_STORE=sqlite python3 -m unittest discover -s tests/service -t . -p 'test_*.py'
Ran 38 tests in 31.271s
OK (skipped=1)
```

* reference 跳过 9 项：都是"跨进程/落盘才能证明"的用例（强杀重启、常驻进程、手机端 HTTP），跳过消息写明理由；
* sqlite 只跳过 1 项：`test_graceful_stop_on_sigterm_finishes_the_round_then_exits`
  （`Windows 上 terminate() 是强杀，优雅停止只能在 POSIX/设备上验证`）；
* **一次环境导致的假失败**：某轮 sqlite 全套在机器休眠中被挂起（墙钟 99998s）后报了两项 ERROR；
  唤醒后单独重跑这两项与整套均为 `OK`，属环境中断而非代码缺陷（记录在此，避免以后误判）。

### 4.2 纵向端到端（`tests/service/demo_vertical.py`，原始输出摘录）

```sh
$ python3 -X utf8 tests/service/demo_vertical.py "$TEMP/mvp8-demo-doc"
工作目录（仓库外）：C:\Users\...\Temp\mvp8-demo-doc
存储后端：sqlite
```

第一轮（只理解、不外发）：

```text
$ python3 -X utf8 -m asistent.service --once --json --config ...\var\config\service.json
  sync        : {"aborted_reason": null, "account": "parent@example.com", "complete": true, "cursor_after": "1",
                 "documents_created": 1, "documents_updated": 0, "duplicates_skipped": 0, "failed": 0,
                 "folder": "INBOX", "listed": 1, "result": "OK"}
  understand  : [{"case_id": "case_559b34482497f7844db1d9f7", "case_state": "READY_FOR_REVIEW",
                  "counts": {"Draft": 1, "DraftVersion": 1, "Fact": 3, "Inference": 1, "Task": 1},
                  "document_id": "doc_82d689d8199431ddf7cd1d30", "draftable": true,
                  "growth_summary": "growth doc=doc_82d689d8199431ddf7cd1d30 case=case_559b34482497f7844db1d9f7 命中=0 应用=0 冲突=0"}]
  prepare     : {"prepared": [], "skipped": [{"case_id": "case_559b34482497f7844db1d9f7",
                  "why": "用户尚未批准当前版本 dv_73f49e1cffb442b5eacff22e"}], "skipped_count": 1}
  待处理      : {"actions_authorized": 0, "actions_in_flight": 0, "actions_unknown": 0, "approvals_active": 0,
                 "archive_pending": 0, "cases_awaiting_approval": 1, "cases_open": 1, "cases_total": 1,
                 "drafts_open": 1, "questions_open": 0, "tasks_open": 1}
  正在等      : ['approval']
```

时间语义（同一封通知的事实，摘自演练第 8 步从存储读出的 `Fact.temporal_fields`）：

```text
时间语义（真的落到了 `Fact.temporal_fields` 里，不是"提了一嘴"）：
  semantic=registration_deadline  normalized_utc=2026-10-16T10:00:00.000Z  raw=None  tz=+08:00
  semantic=event_start            normalized_utc=2026-10-17T06:00:00.000Z  raw=None  tz=+08:00
  semantic=registration_deadline  normalized_utc=2026-10-16T10:00:00.000Z  raw=None  tz=+08:00
```

（正文里的"10月16日 18:00"与"10月17日 14:00"都被换算成 UTC 落地；`test_service_e2e.py:215-216` 对这两个值有断言）

用户批准（等价于手机端点"批准"）：

```text
草稿 draft_4899fb2e36d022b65dd4e738 当前版本 dv_73f49e1cffb442b5eacff22e（v1）
审批结果：state=ACTIVE id=appr_40bf255c7011c5dd382c0aef 绑定对象=['dv_73f49e1cffb442b5eacff22e']
```

第二轮（准备 + 外发，受控假通道）：

```text
  prepare  : {"prepared": [{"action_id": "act_7317d4e64b86cf0af019de44", "action_state": "AUTHORIZED",
               "approval_id": "appr_f5f2fa221d434d6284d841913fa4fd23",
               "case_id": "case_559b34482497f7844db1d9f7",
               "draft_version_id": "dv_73f49e1cffb442b5eacff22e",
               "idempotency_key": "action:a2f7846f166e9f82f175313c674e9245c808593da62fa702eca7bc2526aa391d"}],
               "skipped": [], "skipped_count": 0}
  outbound : {"executed": [{"action_id": "act_7317d4e64b86cf0af019de44", "action_state": "SUCCEEDED",
               "attempt_no": 1, "external_ref": "fake-message-id@local",
               "idempotency_key": "action:a2f7846f166e9f82f175313c674e9245c808593da62fa702eca7bc2526aa391d",
               "reason": "远端已接受（有接受凭据）"}]}
```

第三轮（同配置再跑一次 → **不重发**）：

```text
  outbound : {"executed": [], "sender_call_count": 0}
$ cat ...\var\state\outbound-calls.jsonl
  {"action_id": "act_7317d4e64b86cf0af019de44", "event": "submit", "from_account": "parent@example.com",
   "idempotency_key": "action:a2f7846f166e9f82f175313c674e9245c808593da62fa702eca7bc2526aa391d", "note": "远端接受",
   "subject": "Re: 回复周末科普讲座报名确认（含活动时间）：确认知悉时间安排",
   "to": ["市图书馆少儿部 <kids@example.org>"]}
submit 行数 = 1
```

第四轮（归档 + 逐文件哈希）：

```text
  archive  : {"archive_id": "archive:aeac0c49a22c0590937c4d5cc76d70044f2960fc630830d1c89f620d3a1daa4d",
              "case_id": "case_559b34482497f7844db1d9f7", "state": "VERIFIED", "ok": true,
              "uploaded": 2, "verified": 2, "failed": 0, "failed_detail": [],
              "manifest_digest": "d4483c42a81f6410b897946ea54f51f5d390112524294cff29abb50efeb3a822",
              "content_origin_summary": {"METADATA_PROJECTION": 0, "RAW_BYTES": 2,
                                         "byte_level_verifiable": 2, "projection_only": 0, "undeclared": 0}}
  远端布局：个人助手归档/事务归档/archive-aeac0c49a22c0590937c4d5cc76d70044f2960fc630830d1c89f620d3a1daa4d
  scope=case scope_ref=case_559b34482497f7844db1d9f7 文件数=2 policy=None
  顶层键：archive_id, code_version, content_origin_summary, contract_version, entries, excluded,
          manifest_digest, policy, policy_gate, remote_root, rule_version, schema_version, scope, scope_ref,
          secret_scan, snapshot_digest, source_versions, staged_sources, total_bytes, workflow_version
  policy_gate="U-6 未确认：本次不带 ArchivePolicy（spec §21.3）"
  secret_scan.clean=True
  case.json                                      sha256 匹配  919e431b4ca8328a...
  doc_82d689d8199431ddf7cd1d30.eml               sha256 匹配  cd00f49e4e3b8fb3...
逐文件哈希校验：全部通过
```

### 4.3 常驻 / 无客户端 / 重启不重发 / 规则版本

```sh
$ ASISTENT_STORE=sqlite python3 -m unittest -v \
    tests.service.test_service_e2e.TestResidencyWithoutClient \
    tests.service.test_service_crash \
    tests.service.test_service_e2e.TestGrowthRuleCorrection
```

关键断言（判据就是原始输出里的数字，不是叙述）：

| 用例 | 判据 |
| --- | --- |
| `test_two_consecutive_rounds_keep_working_without_any_client` | 两轮都 `ok=True`、每轮 8 个阶段齐全；两轮之间**没有任何客户端连接**（新进程驱动，无 socket）；第 2 轮 `sync.listed=0`、`understand.analyzed=[]`（不重复理解） |
| `test_third_round_is_a_clean_noop` | 第三轮 `prepare.prepared=[]`、`outbound.executed=[]`、审计行数不增（幂等跳过） |
| `test_killed_after_submit_becomes_unknown_and_never_resends` | 子进程被 `os._exit(9)` 强杀（无 traceback、退出码 9）→ 存储里 `ExternalAction=IN_FLIGHT`、`Case=EXECUTING`、账本 1 行；**新进程**恢复后：`recover.demoted_action_ids` 长度 1、`resent_action_ids=[]`、`sender_call_count=0`、`Case=UNKNOWN`、`outbound.executed=[]`、账本仍然 1 行、`pending.actions_unknown=1`、`waiting_on` 含 `action_unknown`；第三轮也不重发 |
| `test_authorized_action_survives_restart_and_is_sent_exactly_once` | 先"关通道"配置跑一轮 → 1 个 `AUTHORIZED` 动作、0 次通道调用；换成受控假通道**新进程**再跑 → 1 次发送、账本 1 行；第三轮仍然 1 行 |
| `test_rule_version_is_visible_in_audit_and_grows_after_correction` | v1 命中：审计 `detail.rule_id/rule_version=1`、`Case.rule_ids` 含 v1、`Fact.links["rule_version"]`、问题文案含规则 id 与 `v1`；`enable_new_version` 后版本链 `[1,2]`、状态 `["SUPERSEDED","ENABLED"]`；下一封同类通知 `RULE_HIT` = `[(rule_v2.id, 2)]` |

### 4.4 手机端控制台（真 HTTP，`tests/service/test_service_console_http.py`）

```sh
$ ASISTENT_STORE=sqlite python3 -m unittest -v tests.service.test_service_console_http
test_approval_can_be_submitted_from_the_console_and_the_service_continues ... ok
test_console_disabled_means_no_server_and_no_token ... ok
test_graceful_stop_on_sigterm_finishes_the_round_then_exits ... skipped 'Windows 上 terminate() 是强杀，优雅停止只能在 POSIX/设备上验证'
test_resident_service_keeps_rounding_with_zero_clients_and_publishes_health ... ok
test_without_token_everything_is_401 ... ok

Ran 5 tests in 7.467s
OK (skipped=1)
```

用例到底证明了什么（每条都有断言）：

1. **常驻**：子进程 `--loop --console --max-rounds 0`；先等到 2 行轮次摘要（`[round N] round=N ...`），
   期间客户端请求数 == 0；再 `GET /api/artifacts` 找到 `service-status.json`，
   `GET /api/artifacts/<id>/content` 下载并解析 → `rounds_completed >= 2`、`pending/waiting_on/notes/last_sync` 齐全、
   `status_artifact.published == true` 且 sha256 非空；同时用 `--status` 的独立进程读数交叉核对（两边一致）；
2. **审批走手机端**：`GET /api/drafts` → `GET /api/drafts/<id>` 取当前版本号 → `POST /api/approvals {draft_id,version_no,confirm:true}`
   → `201` 且 `state=ACTIVE`；此后连跑两轮出现 `outbound` 执行（受控假通道），
   账本 submit 行数 == 1；`/api/approvals` 里恰好 1 条动作级审批 `CONSUMED` 且带 `consumed_by_attempt_id`；
   用户那条审批仍绑定 `target_object_ids == [当前版本]`；
3. **不开后门**：无令牌/错令牌访问 `/api/overview /api/cases /api/artifacts /api/approvals` 全部 `401`，响应里不含令牌；
4. **关掉就真关**：`console.enabled=false` 时 `--console` 也**不监听任何端口、不生成令牌文件**；
5. **优雅停止**（POSIX）：`SIGTERM` → 退出码 0、日志有"收到停止请求"、之后不再开新轮。

### 4.5 真机（`board`）

**状态：✅ 已执行（2026-10-12T08:29+08:00，设备提交 `198f442`，退出码 0）。**

原始输出**未删改**地存进版本化验收目录：`docs/evidence/mvp-8-device-20261012.log`（483 行）。
一条命令即可复现（脚本只写 `$HOME/asistent-var` 与它自己的临时目录，**不碰** `~/code/asistent`）：

```sh
ssh board 'cd ~/code/asistent && sh tests/service/device_run.sh ~/code/asistent'
```

#### 4.5.1 这一轮真机跑出来的事实

| 环节 | 真机原始结论 |
|---|---|
| 环境 | `aarch64` / `Python 3.10.12` / `sqlite 3.37.2` + **FTS5 True** / `systemd 249` / `Linger=yes` / `setsid` 可用 / `crontab` 为空 |
| 单轮 | `--once --json` 八阶段全 OK：`sync` 建出 1 个文档、`ingest` 建 FTS 行、`understand` 派生 `Fact 4 / Inference 1 / Draft 1 / Task 1`，`waiting=approval` |
| 常驻 | `setsid` 降级方式启动后连跑，`rounds_completed` 一路涨到 **7**，`failed=0` |
| 手机端 | 无令牌 `401`；带 `Authorization: Bearer <token>` → `200`，`/api/overview` 返回真实待处理项（1 审批 / 1 草稿 / 1 任务） |
| 恰好一次 | 批准后外发账本**恰好 1 行**，又等两轮后仍是 1 行（`外发账本行数（应恰好 1）：1`） |
| 优雅停止 | `SIGTERM` → “收到停止请求，等当前轮跑完后退出” → 无残留进程 |
| 归档（未授权） | `archive` 阶段返回 `NOT_AUTHORIZED`（`retryable=false`），2 个文件按**字节级**（`RAW_BYTES: 2`）留在本地 staging —— 不假装成功 |

#### 4.5.2 这次取证顺手抓出的 4 个真缺陷（本机全绿、真机才露）

取证的价值不在于"证明它对"，而在于**它真的抓到了错**。4 个都已修 + 已部署（设备套件 `UNITTEST_RC=0`）：

1. 令牌父目录权限不合规时 `TokenAuth("")` 抛异常 → **整个服务起不来**（POSIX 才做这项校验）；
   改为只降级控制台。
2. `archive.root` 被 `_expand()` 成本机绝对路径，且 bdpan 客户端**没接收**配置的前缀
   → 归档清单会记错"远端位置"、实际归到别处。
3. `unable to open database file`：sqlite 不建目录，而"手写配置直接跑服务"这条路不会先建 `data/`。
4. 取证脚本自己：`crontab -l` 返回 1 + `set -e` → **整份取证在第 0 节就退出**（41 行）。
   取证脚本的原则应是"凡是探测都要记账，但不能因为探测结果不如意就中断"。

> 连带结论：`device_run.sh` 属于**取证工具**，它的失败模式也要按产品缺陷对待——
> 工具崩了会让人误以为"没证据 = 没问题"。

#### 4.5.3 另一种取证方式（源码放临时目录，不依赖设备上的已部署副本）

下面这条走 `/tmp` 副本：**取证对象是"我本地的源码"**，适合验证尚未部署的改动；
而 §4.5.1 那次走的是**设备上的已部署副本**，取证对象是"板上真正在跑的那份提交"——
两种都要会，但要说清自己在证哪一份。脚本只写 `$HOME/asistent-var` 与 `/tmp/mvp8-*`，不碰 `~/code/asistent`：

```sh
# 1) 本机打包并送到设备（源码放临时目录，不动设备上已部署的副本）
tar czf /tmp/mvp8-src.tgz --exclude=__pycache__ asistent fixtures scripts/run-service.sh tests/service
scp /tmp/mvp8-src.tgz board:/tmp/mvp8-src.tgz
ssh board 'rm -rf /tmp/mvp8-run && mkdir -p /tmp/mvp8-run && tar xzf /tmp/mvp8-src.tgz -C /tmp/mvp8-run'

# 2) 在设备上跑取证（§0 环境事实 → §7 优雅停止；输出整段留着当原始证据）
ssh board 'chmod 700 /tmp/mvp8-run/scripts/run-service.sh; sh /tmp/mvp8-run/tests/service/device_run.sh /tmp/mvp8-run'
```

> 附注（权限）：在 Windows 上创建的 `scripts/run-service.sh` **没有可执行位**，所以本机/设备一律用
> `sh scripts/run-service.sh …` 调用（文档与 unit 都按这个约定写）；设备上若要直接 `./run-service.sh`，
> 需要 Lead 在部署或提交时 `chmod 700`。

脚本会依次打印（这些就是验收要的原始输出）：

```text
0) 环境事实：uname -a / python3 -V / sqlite+FTS5 / systemctl --version / loginctl show-user（Linger）
1) 运行目录：mkdir -p ~/asistent-var/{config,state,logs,raw,mail-cases}
2) 写配置：~/asistent-var/config/service.json（邮件=离线夹具、外发=受控假通道、归档=真 bdpan）
3) 单轮：python3 -X utf8 -m asistent.service --once --json --config ...   ← 原始轮次报告
4) run-service.sh doctor / unit-file（+ systemd-analyze verify，若可用）
5) run-service.sh start（setsid 降级方式）→ 连跑两轮 → status → tail 日志
6) 手机端视角：device_console_probe.py（读健康快照 + POST 审批）→ 下一轮外发 → 账本 submit 行数恒为 1
7) run-service.sh stop（SIGTERM 优雅停止）→ 日志尾部 → 确认无残留进程
```

真机上还必须核实的两点（脚本已内置断言）：

* `systemctl --version` ≥ 240：`StandardOutput=append:` 需要 ≥240；更低版本改用 `journal`（`unit-file` 输出里已给出替代写法注释）；
* `bdpan` 处于**未授权**状态时：归档阶段必须返回显式 `NOT_AUTHORIZED` 且 `uploaded=0`（一次写调用都不发）。

### 4.6 测试清单（38 项）

| 模块 | 项数 | 覆盖 |
| --- | --- | --- |
| `test_service_unit.py` | 20 | 配置安全默认与 `describe()` 无凭据、未知通道报错、相对路径、间隔覆盖与下限；快照计数（只算未关闭事务名下的对象、`archive_pending` 需要真有 `ArchiveJob`）、`infer_waiting_on` 优先级；`ServiceState` 往返与损坏容错；阶段固定顺序与隔离（一个阶段失败不接管本轮、`SimulatedCrash` 不被吞）、`recover_phase` 源码级"不调通道"；回答并入（阻断-解除、冲突不解除、无回答透传、事实幂等且带引用）；夹具邮件源（非 ASCII 头解码、游标稳定、仓库公共夹具可读） |
| `test_service_e2e.py` | 4 | 纵向全程（含"第三轮不重发"、"归档逐文件哈希"）；无客户端两轮连跑；第三轮干净空转；规则版本递增 |
| `test_service_bare_start.py` | 7 | 无配置文件也起得来、每一路都说明白为什么没干活；令牌自动生成（600）且不进 stdout；邮箱无适配器/目录缺失都如实记账且**继续跑第二轮**；网盘两道闸门（未授权 / 未审批）**零写调用**；真通道写锁先于子进程；state 文件损坏仍能启动 |
| `test_service_console_http.py` | 5 | 见 §4.4 |
| `test_service_crash.py` | 2 | 强杀 → `UNKNOWN` 且永不重发；已授权动作跨重启恰好一次 |

---

## 5. 关键实现细节

### 5.1 "恰好一次"是怎么成立的（不是靠 if 判断）

```text
用户批准（控制台/手机端）         →  Approval(ACTIVE, target_object_ids=[version_id])
        │ prepare_phase
        ▼
动作派生（ID 由 (case_id, version_id) 确定性派生，重跑得到同一个）
        │ 投影：create_approval(..., auth_evidence="console-approval:<用户审批 id>") + approve_and_authorize
        ▼
ExternalAction(AUTHORIZED, idempotency_key=action:<digest>)
        │ outbound_phase：Case APPROVED→EXECUTING
        ▼
ActionEngine.execute → claim_action（一次原子调用：把动作从 AUTHORIZED 改成 IN_FLIGHT 并落盘）
        │  ← 只有到这一步之后才允许外呼
        ▼
调通道（幂等键随行；通道自己拒绝重复键）
        │
        ▼
结果落库（SUCCEEDED / FAILED_SAFE / UNKNOWN）+ Attempt 记录
```

第二个人再点一次、服务再跑一轮、进程重启，都只会看到"这个动作已经不是 `AUTHORIZED`"。

### 5.2 审批是"内容绑定"的

用户批准的对象是**具体草稿版本**（`target_object_ids=[dv_...]`），不是"这一类事"。
草稿被编辑产生新版本后，准备阶段找不到新版本的 `ACTIVE` 审批 → 继续等（不会拿旧批准发新内容）。
动作级审批是**投影**，其 `approved_action_digest` 与动作的内容摘要绑定；动作内容一旦变化，投影也就不匹配。

### 5.3 阶段隔离与"失败不接管"

```python
for name, function in steps:
    try:
        result = function(ctx)
    except Exception as exc:          # 记录并继续
        result = PhaseResult(name=name, ok=False, error_class=type(exc).__name__, error_text=str(exc))
    # BaseException（KeyboardInterrupt/SystemExit/SimulatedCrash）**故意不吞**：那不是业务失败
```

### 5.4 用户回答并入理解（v0.7 契约下的兜底）

MVP-3 的 `Question` 没有 `kind` 字段，而它的抽取是确定性的——即"提问后答案无法让草稿解锁"。
本层的兜底：`AnswerAwareUnderstander` 只把 `ANSWERED` 且问题类别属于
`{ambiguity, missing_material, missing_time}` 的回答并成事实（带 `Citation(source_object_type="Answer")`），
再交给理解器重算；**冲突类问题永不被回答"解决"**（SPEC §861 的人工裁决优先）。
契约 v0.8 提供 `Question.kind` 后，这段匹配逻辑可以整体删掉（见 §8 CP-33）。

### 5.5 启动器（`scripts/run-service.sh`）

* 只做进程管理，不做业务；`umask 077`；pidfile/日志 600；
* **解释器挑"真的能跑"的那个**：`ASISTENT_PYTHON` > `python3` > `python` > `python3.10`，每个候选都先 `-c 'print(1)'` 试跑一次才认。
  两个坑值得记着：①"有 python 没有 python3"的机器上，只写 `python3` 会让 `doctor` 显示成空；
  ②Windows 的 `WindowsApps/python3` 是应用商店占位符，`command -v` 找得到但一跑无声无息。
  显式指定了却跑不起来时**如实报错、绝不换解释器**，并且 `once/start/…` 会先拒绝执行并说明怎么修；
* `find_service_pid` 扫 `/proc/*/cmdline` 找 `asistent[.]service --loop`，pidfile 只是缓存（被写入坏值也能自愈）；
* `start` 用 `setsid + nohup` 脱离终端，并**显式** `--max-rounds 0`（配置里若残留 `max_rounds=N`，
  服务会在第 N 轮后正常退出——那看起来像"跑一会儿自己死了"，是最难查的一类故障）；
* `unit-file` 打印 `systemd --user` unit（`Restart=always`、`StartLimitIntervalSec=0`、`KillSignal=SIGTERM`、
  `TimeoutStopSec=90`），`unit-file --stage` 写到 `$ASISTENT_VAR/systemd/asistent.service`（600）；
  本脚本**不**替你写 `~/.config/systemd/user/`（那是使用者的系统目录）；
* 降级方式 `@reboot` 一行（`crontab-line` 打印），与 systemd 方式二选一；
* 凭据纪律：脚本从不读/打印/转发任何口令、授权码、令牌内容；令牌只由服务自己从
  `$ASISTENT_VAR/config/console-token`（600）读，既不进 argv 也不进日志。

### 5.6 归档的三道闸门（未授权/未审批绝不写）

```text
① pan.require_authorized()（只读探测）   → 未登录/令牌过期 → NOT_AUTHORIZED（一次写调用都不发）
② 写锁（bdpan 默认锁住，需 unlock_writes(approval_ref)） → 没有审批就 APPROVAL_REQUIRED
③ 冻结快照 + 逐对象回读哈希 → 只有全部匹配才写 _COMPLETE 标记
```

本层**不**调用 `unlock_writes`：也就是说，**没有用户的明确批准，服务不可能向网盘写入任何东西**
（用例 `test_real_bdpan_client_refuses_before_starting_any_subprocess` 用一个不存在的 binary 证明：
未解锁时连一次 `bdpan` 子进程都不会被拉起）。

### 5.7 凭据纪律

| 位置 | 是否可能出现凭据 |
| --- | --- |
| 代码/测试/文档/配置模板 | ❌（测试只用假令牌 `test-token-…`；`config.describe()` 只输出 `token_present: true/false`） |
| 日志/审计/健康快照/argv | ❌（`SECRET_MARKERS` 扫描断言；快照发布前还会再扫一遍令牌明文，命中即拒绝发布） |
| 仓库外 600 文件 | ✅ 只在这里（`config/console-token`、`bin/set-mail-authcode.sh` 写进去的授权码） |

### 5.8 措辞纪律

离线受控假通道**只**证明"服务在正确的时间、对正确的内容、只做了一次动作"；
它**不证明**能连上 163 邮箱、也**不证明**网盘的读写能力。见 §6。

---

## 6. 「离线假通道证明了什么」vs「真机能证明什么」

| 结论 | 离线（本分支已证） | 真机（`board`） |
| --- | --- | --- |
| 一轮的编排与状态机（8 阶段、状态转移、审计） | ✅ 全证 | 同一份代码，重跑即可 |
| 恰好一次外发（账本 + 强杀重启） | ✅ 全证（受控假通道 + `os._exit(9)`） | ⏳ 同断言可复跑（脚本已内置） |
| "没人连它也在轮转" | ✅ 全证（多轮 + 0 客户端） | ⏳ 需真机连跑（`run-service.sh start`） |
| 关掉电脑/关闭桌面窗口后仍常驻 | ⚠️ 机制层面已证（`setsid`+`nohup`；systemd unit 已生成但未安装） | ⏳ `systemd --user` + `Linger=yes` 或 `@reboot` |
| 手机端能看到待处理/审批/健康快照 | ✅ 全证（真 HTTP + Bearer + 401 反向） | ⏳ 需真机端口与令牌（脚本已内置 `device_console_probe.py`） |
| 真 IMAP/163 收信 | ❌ **未证**（本层没有真实邮箱适配器；`mail.source=imap` 会如实记为"无适配器 + 下一步动作"） | ⏳ 用户动作 U-1 完成后才有意义 |
| 真百度网盘归档 | ❌ **未证**（只用本地目录模拟远端） | ⏳ 用户动作 U-2；未授权时必须走显式 `NOT_AUTHORIZED`（脚本已内置断言） |
| 设备端 Python 3.10 兼容 | ✅ 静态保证（无 3.11+ 语法；纯标准库） | ⏳ 3.10.12 上跑一遍即可确认 |
| 28 小时无人值守的稳定性 | ❌ 未证（只跑了分钟级） | ⏳ 需长时间观察（见 §7.5） |

---

## 7. 已知限制与发现的缺陷

### 7.1 发现的既有缺陷（**不在本层修**，按约定上报）

**契约层邮件身份函数对未编码的非 ASCII 头部会崩**

* 位置：`asistent/contracts/mail.py:191`（`FixtureMailSource._ref_for`）与 `:252`（`imap_raw_to_ref`），
  两处都是 `(message.get("To") or "").split(",")`；
* 触发：`email.message_from_bytes()` 对**未用 RFC2047 编码**的非 ASCII 头部返回 `email.header.Header`，
  它没有 `.split` → `AttributeError: 'Header' object has no attribute 'split'`；
* 影响面：`fixtures/public/understand/*.eml` **每一封**都带这种头部（真实中文邮件的常见形态），
  也就是说任何走契约层邮件身份函数的路径都会崩；
* 本层的规避：`asistent/service/mail_source.py::FixtureDirMailSource` 自己走
  `asistent.understand.extract.decode_eml_bytes` 解码头部，不碰契约层的这个函数；
* 建议修法：`_ref_for` / `imap_raw_to_ref` 复用 `decode_eml_bytes`（或至少 `str(msg.get("To"))` 后 `email.utils.getaddresses`），
  并在契约测试里加一封"非 ASCII 未编码头部"的夹具。修复后 `asistent/service/mail_source.py` 可以整体删掉（见 CP-32）。

### 7.2 实测踩到的接线坑（本轮已修，记录价值在于"别踩第二次"）

1. `console.artifact_roots` 默认只覆盖 `mail.raw_root`，而健康快照在 `state_dir` 下 →
   手机端点得到文件名、打不开内容（404「文件不在允许下载的目录内」）。
   已改：`run-service.sh config-init` 生成的配置显式写入 `artifact_roots=[state, raw]`；
   测试里也有专门一条（`test_resident_service_...`）用真 HTTP 覆盖。
2. `--loop` 的轮数上限来自配置里的 `max_rounds`：配置文件里残留 `max_rounds: 1` 会让"常驻服务"
   跑完一轮就退出（日志看起来完全正常）。已改：`start` 与 systemd unit 都显式 `--max-rounds 0`。
3. `console.enabled=false` 时仍然构造 `TokenAuth("")` → `AuthError` → **整个服务起不来**。
   这正是"缺配置就不能活"的典型。已改：关掉控制台时不读令牌、不建鉴权对象，`--console` 只记一条说明。
4. 轮次摘要先于"落盘 + 发布快照"打印 → 日志说第 2 轮完成、手机端下载到的还是第 1 轮快照。
   已改：`run_once` 顺序固定为 `run_phases → state.save → 发布快照 → 打印本轮`。

### 7.3 审计口径：`Event` 表在实践里是空的

`run_phases` 原先只从 `Event.payload` 判断"这份资料理解过了"，但**实测该表为空**
（走契约层的写入路径没有落事件），导致"每轮重复理解同一份资料"。
本层的兜底：`服务理解` 审计行里写 `document_id` + `content_hash`，判断时把审计行与 `Event.payload` **取并集**。
契约层把事件补齐后可以只留 `Event`（见 CP-34）。

### 7.4 依赖用户动作

| 动作 | 影响 | 现在服务怎么表现 |
| --- | --- | --- |
| U-1 邮箱授权（163 授权码 → `~/asistent-var/bin/set-mail-authcode.sh`） | 真收信 | 已接真实通道（`mail.source=mcp`）：缺授权码时如实记为"未启用 + 下一步动作"，服务正常轮转；**不假装收信** |
| U-2 百度网盘授权（`bdpan login`） | 真归档 | 归档阶段显式 `NOT_AUTHORIZED` + `next_step`；`waiting_on` 含 `pan`；**零写调用** |
| U-6 ArchivePolicy 确认（spec §21.3） | 归档策略 | 本次归档 `policy: null` + `policy_gate` 说明（清单里写明，不假装有策略） |

### 7.5 其它诚实边界

* **长时稳定性未证**：只跑了分钟级多轮；"24h 无人值守"需要真机跑够时间（内存/句柄/日志轮转都要看）。
  2026-10-12T08:37 起已在板上以 `systemd --user` 常驻（自启 + `Restart=always`），
  但**墙钟没走完之前不写"通过"**。
* ~~**真机证据缺失**（§4.5）：网络不可达导致本轮没有真机输出~~ → **已补齐**（§4.5、`docs/evidence/mvp-8-device-20261012.log`）。
* **控制台没有"服务健康"这一节**：`/api/overview` 只覆盖业务对象，所以健康快照走 `FileArtifact`。
  这是**过渡方案**：MVP-9 的手机可访问存储（`GET /api/store/<token>`）到位后，可以让手机端直接读快照，
  但这**不等于**满足"绕过存储直读"的要求——本层不声称已满足。
* **Windows 上无法验证优雅停止**（`terminate()` 是强杀），该用例在 Windows 显式 skip 并写明理由。
* **单实例假设**：同一份 `$ASISTENT_VAR` 只应有一个服务进程。两个进程同时跑不会重发（`claim_action` 原子），
  但会产生重复的日志与快照写入；`run-service.sh start` 已做单实例检查（pidfile + `/proc` 反查）。

---

## 8. CP 提案（从 `CP-20261010-32` 起）

| 编号 | 提案 | 类型 | 影响面 | 建议 |
| --- | --- | --- | --- | --- |
| CP-20261010-32 | 修 `asistent/contracts/mail.py` 的非 ASCII 头部解码（`_ref_for` / `imap_raw_to_ref` 复用 `decode_eml_bytes`），并补一封未编码中文头部的契约夹具；修完后**删除** `asistent/service/mail_source.py`（本层规避代码） | 缺陷修复（契约） | `contracts/mail.py`、`tests/contracts/**`、`asistent/service/**` | 下一个契约版本内修；这是我这边唯一"因为下层缺陷而多写的代码" |
| CP-20261010-33 | 契约 v0.8 的 `Question.kind` 落地后，删除 `asistent/service/answers.py` 的文本匹配兜底，改成按 `kind` 判定"回答能否解除阻断" | 简化 | `asistent/service/**` | v0.8 合并后紧接着做，避免两套语义并存 |
| CP-20261010-34 | 把"资料已理解 / 回答已并入"的账落到 `Event`（或在契约里明确"分析账由审计行承担"），让 `asistent/service/phases.py::_analyzed_documents` 去掉审计行并集 | 契约/实现对齐 | `contracts/*`、`asistent/service/**` | 需要设计决策：事件表与审计表的职责边界 |
| CP-20261010-35 | 给"服务自身健康"定义契约级只读对象（而不是把 `service-state.json` 当 `FileArtifact` 发布）。当前方案能用，但 `case_id=""` 是一种借用 | 接口设计 | `contracts/objects.py`、`asistent/console/**`、`asistent/service/**` | MVP-9 设计存储直读时一起定；定了就把 `status_artifact.py` 换成契约对象 |
| CP-20261010-36 | `ArchivePolicy`（spec §21.3 的 U-6）确认后，本层归档要带 `policy` 并去掉 `policy_gate` 说明；同时明确"归档写入是否需要独立审批"（现在靠通道写锁 = `APPROVAL_REQUIRED`，没有走审批链） | 需求确认 | `contracts/archive.py`、`asistent/service/phases.py`、`asistent/archive/**` | 与用户确认 U-6 时一起定 |
| CP-20261010-37 | 长时稳定性验收口径（跑多久、看哪些指标：RSS、打开句柄、日志大小、SQLite WAL 增长、轮次失败率） | 验收口径 | `docs/**`、`tests/service/**` | 真机跑通后与 Lead 一起定，避免"跑了一晚上"这种不可复现的证据 |

---

## 9. 复现步骤（本机）

```sh
# 0) 依赖：纯标准库（Python ≥3.10）；测试用 unittest
# 1) 双后端全套
python3 -m unittest discover -s tests/service -t . -p 'test_*.py'
ASISTENT_STORE=sqlite python3 -m unittest discover -s tests/service -t . -p 'test_*.py'

# 2) 纵向端到端（跑完不删工作目录，可自己翻存储/清单/账本）
python3 -X utf8 tests/service/demo_vertical.py /tmp/mvp8-demo

# 3) 一键看状态（新进程；只读，不发任何请求）
ASISTENT_VAR=/tmp/mvp8-demo/var python3 -X utf8 -m asistent.service --status --json \
    --config /tmp/mvp8-demo/var/config/service-archive.json

# 4) 常驻（前台观察；Ctrl-C 优雅退出）
ASISTENT_VAR=/tmp/mvp8-demo/var python3 -X utf8 -m asistent.service --loop --console \
    --interval 5 --config /tmp/mvp8-demo/var/config/service-archive.json
```

设备端（可达时）：

```sh
sh tests/service/device_run.sh /tmp/mvp8-run        # 见 §4.5
```

