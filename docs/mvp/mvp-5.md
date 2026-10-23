# MVP-5：外部动作与幂等（`asistent/actions/**`）

- 分支：`mvp/5-actions`（worktree：`D:\dsh\个人助手\.worktrees\mvp-5-actions`）
- 起点：`b3b35b6`（contracts **v0.5** 冻结点；main 当时为 `ca65631`，本分支未 rebase，
  `git diff --name-only main -- asistent/contracts tests/contracts` 为空，见 §5.1）
- 交付物：`asistent/actions/{__init__,approval,engine,senders}.py`、`tests/actions/**`、本文档
- 结论：**这是整个系统最不能出错的一块：宁可拒绝执行，也绝不重复发送。**
  - 本地（Windows + `python 3.12`，双后端）：`378/378` 全绿
    （reference `OK (skipped=9)`、sqlite `OK (skipped=5)`；跳过项=4 项"需要持久化后端"的跨进程/重启用例
    + 5 项平台符号链接用例）；
  - 设备（`ssh board`，`/usr/bin/python3` = **3.10.12**，双后端）：`378/378` 全绿
    （reference `OK (skipped=4)`、sqlite `OK`，见 §5.3）；
  - **真并发只有一个赢家**：同进程 8 线程 + 跨进程 4 进程，外部提交次数均为 **1**；
    另有一条**确定性**用例把"在途窗口"钉住，证明赢家外呼途中别人一律进不来（§5.4）；
  - **崩溃后绝不重发**：子进程在"已提交、结果未落库"时被强杀（exit 9），重启后转 `UNKNOWN`，
    落盘调用账本提交次数仍是 **1**（§5.5）；
  - `contracts/**`、`tests/contracts/**` **一行未改**（§5.1）；纯标准库、零第三方依赖。

---

## 1. 目标与验收对照

| 验收项（SPEC §851-855） | 状态 | 证据 |
| --- | --- | --- |
| §851 未批准的发送动作 → 被拒且**无外部副作用** | ✅ 6 项测试（未批准拒绝 / 未批准时严格模式抛错 / 审批已撤销 / 审批在仓库里被绕过撤销 / 未注入通道 / 本地类动作）；`sender_call_count=0`、`ActionAttempt` 为 0 条 | §5.6、§6 对照表 |
| §852 批准 DraftVersion-3 且内容未变 → **只允许一个执行尝试获得执行权** | ✅ 同进程 N=8（`threading.Barrier`）+ 跨进程 N=4，赢家恰好 1、落盘提交恰好 1 | §5.4 |
| §853 发送成功后重复轮询或重启 → **不再发送第二次** | ✅ 轮询 3 次 + 2 次 `recover()` + 再次 `execute()`；提交恒为 1 | §5.6 |
| §854 结果 UNKNOWN → **不自动重试**，Case 显示 UNKNOWN 等待用户裁决 | ✅ 崩溃注入 + 通道未知两类；`recover()` 的 `resent_action_ids` 恒为空；`pending_decisions()` 给出两个合法裁决 | §5.5、§5.7 |
| §855 草稿/收件人/关键依据变化 → **旧 Approval 失效且必须重新确认** | ✅ 版本换版、正文、收件人、Bcc、附件哈希、依据版本 6 条路径；失效原因可读 | §5.8 |
| 唯一执行权用**原子** `claim_action`，不是"先查后标记" | ✅ 存储调用序列证据：恰好 1 次 `claim_action`，引擎零直写 | §5.9 |
| 先落库再外呼（attempt `IN_FLIGHT` 先持久化） | ✅ 通道被调用时，库里动作已是 `IN_FLIGHT` 且 attempt 已存在 | §5.6 |
| 崩溃 → `IN_FLIGHT` 降级 `UNKNOWN`，不自动重发 | ✅ 三种注入点（真子进程强杀 / 同进程 `BaseException` / 库里残留） | §5.5 |
| UNKNOWN 只能由用户裁决推进；重试用**新** attempt + **同一**幂等键 | ✅ `acknowledged_duplicate_risk` 与认证渠道双闸门；attempt_no 2、key 不变、原 UNKNOWN 记录保留 | §5.7 |
| 双后端 + Python 3.10（设备）全绿 | ✅ 本地 378/378 ×2、设备 378/378 ×2 | §5.1、§5.3 |
| `contracts/**`、`tests/contracts/**` 零改动 | ✅ `git diff --name-only main -- asistent/contracts tests/contracts` 空 | §5.1 |

---

## 2. 交付物

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `asistent/actions/senders.py` | 315 | 外发通道适配器：增量结果分类、异常分类单一裁决点、跨进程调用账本、可编程发送器、崩溃注入 |
| `asistent/actions/approval.py` | 604 | 审批与确定性内容绑定：创建/绑定授权/只读检查/撤销/过期清扫/换版收紧/内容更新失效 |
| `asistent/actions/engine.py` | 785 | 执行引擎：一次性领权 → 外呼 → 落结论；恢复路径；UNKNOWN 用户裁决 |
| `asistent/actions/__init__.py` | 89 | 对外导出面（控制台/后续 MVP 只从这里取） |
| `tests/actions/support.py` | 255 | 测试基座：双后端、临时目录、夹具闭环、子进程工具、证据 `dump()` |
| `tests/actions/test_execution_path.py` | 283 | 16 项：拒绝/先落库/成功幂等/结果分类 |
| `tests/actions/test_approval_binding.py` | 388 | 20 项：版本绑定、内容失效六路、过期、认证渠道、契约缺陷复现 |
| `tests/actions/test_single_execution.py` | 245 | 5 项：同进程 N=8、在途窗口、跨进程 N=4、原子调用序列、审批不可复用 |
| `tests/actions/test_crash_recovery.py` | 190 | 6 项：真子进程强杀、同进程崩溃、残留 IN_FLIGHT、恢复幂等 |
| `tests/actions/test_unknown_decision.py` | 240 | 10 项：UNKNOWN 不自动重试、retry 双闸门、abandon、多轮裁决 |
| `tests/actions/test_sender_adapters.py` | 137 | 7 项：与冻结 `RecordingMailSender` 互操作、协议符合性、跨进程账本 |
| `tests/actions/crash_child.py` / `claim_child.py` | 54 / 57 | 崩溃注入子进程 / 并发领取子进程 |

新增 64 项测试（基线 `Ran 314 tests` → `Ran 378 tests`）。

---

## 3. 不变量与设计

### 3.1 单一线性化点：`store.claim_action`

`claim_action` 是"本动作能否对外发出"的**唯一**判定点与**唯一**写入口。它一次原子地完成：

1. 校验动作处于 `AUTHORIZED`（其它状态一律 `ActionNotClaimable`）；
2. 校验 `approval_id` 指向的 Approval 为 `ACTIVE`、未过期、`action_id` 与本动作一致、
   且尚未被消费（契约返回 `ApprovalInvalid`）；
3. 抢占式把动作置 `IN_FLIGHT`（乐观并发：冲突即失败，不做"读-判-写"）；
4. 消费这次批准（`Approval.state=CONSUMED`、`consumed_by_attempt_id` 指向本次 attempt）；
5. 建 `ActionAttempt`（`attempt_no` 递增，`request_idempotency_key` **取自动作上已持久化的**
   `action.idempotency_key`，不允许模型/执行者自造）。

因此"并发只有一个赢家"不是靠锁、也不是靠 `if` 判断，而是靠**一次 CAS 式写入**。
`tests/actions/test_single_execution.py` 用存储调用序列证明引擎只调了一次 `claim_action`，
且从不自己 `put`/`transition` 打状态（§5.9）。

### 3.2 执行路径的写入顺序（先落库，再外呼）

```
execute(action_id):
  ① 只读前置检查（不写库）
       - 通道是否注入（无通道 → OUTBOUND_CHANNEL_MISSING，拒绝，不领权）
       - enforce_version_binding()：内容/版本被改过就先收紧（失活审批 + 动作回落 PREPARED）
       - build_request()：纯本地构造请求（校验 kind/connector/幂等键/摘要一致性）
  ────────────── 到这里为止，任何失败都 0 副作用 ──────────────
  ② store.claim_action()            ← 唯一线性化点；失败即 REJECTED（附错误码与原因）
  ③ sender.send(request)            ← 此时库里已经是 IN_FLIGHT + attempt 已存在
  ④ store.record_attempt(...)       ← 先落确定结论
  ⑤ Case 状态回写（只走合法转换）
  ⑥ 记录 Execution + 审计
```

第 ③ 步之前持久化已完成，所以"外呼途中进程消失"这件事在库里一定看得见
（`IN_FLIGHT` + attempt），不会被误当成"没发过"。

### 3.3 崩溃语义：`BaseException` 不打折

`execute` 只捕获 `Exception`。`BaseException`（`KeyboardInterrupt`、`SystemExit`、
测试里的 `SimulatedCrash`）**原样上抛**，attempt 与动作一起停在 `IN_FLIGHT`。
这不是疏漏：把崩溃当成"通道错误"分类，就有机会被写成 `FAILED_SAFE`——
而 `FAILED_SAFE` 是"确定没发出去"，一旦判断错就是重复发送。

### 3.4 保守分类（宁可 UNKNOWN）

| 情形 | 结论 | 为什么 |
| --- | --- | --- |
| 通道返回 `SUCCEEDED` 且 `accepted=True` | `SUCCEEDED` | 有接受证据 |
| 通道抛 `OutboundError(provably_unsent=True)` | `FAILED_SAFE` | 通道**证明**未提交 |
| 请求非法（`ValidationError`）/ 权限被拒（`PermissionDenied`） | `FAILED_SAFE` | 请求没离开本进程 |
| 其它一切异常（含 `OutboundTimeout`） | `UNKNOWN` | 可能已发出 |
| 结果自相矛盾（`SUCCEEDED` 但 `accepted=False`，或反之） | `UNKNOWN` + `mismatch` 说明 | 通道说了两件矛盾的事，不猜 |

`FAILED_SAFE` 与 `UNKNOWN` 的区别只体现在一个地方：**能否自动重试**。
`FAILED_SAFE` 允许在**重新审批后**再次执行（旧 attempt 已闭环，动作回到 `AUTHORIZED`）；
`UNKNOWN` 必须先由用户裁决。

### 3.5 审批绑定的是**确定的 DraftVersion**

`create_approval` 默认 `verify_draft_version=True`：`summary.draft_version_id` 必须存在、
必须是该 Draft 的**当前**版本、且 `summary.body` 的规范化摘要必须与那一版的正文一致，
否则 `ApprovalInvalid`。批准确认的是"这一版文字 + 这一组收件人 + 这一组依据"，
不是"这个草稿"。

| 变化 | 触发 | 结果 |
| --- | --- | --- |
| 草稿产生新版本（`Draft.current_version_id` 变了） | `check_action_approval` / `execute` 前置 | Approval `INVALIDATED`、动作 `AUTHORIZED→PREPARED`、执行被拒 |
| 正文 / 收件人 / 抄送 / 密送 / 附件哈希 / 依据版本变化 | `update_action_content()` | 同上；`changed_fields` 列出具体字段 |
| 只有规范化等价差异（大小写、行尾、首尾空白） | 同上 | **不**失效（复用冻结契约的规范化口径，避免误伤） |
| 过期 | `claim_action` 自己 + `sweep_expired_approvals()` | 拒绝执行；`EXPIRED` + 动作回落 `PREPARED` |
| 用户撤销 | `revoke_approval()` | `REVOKED` + 动作回落 `PREPARED` |

"收紧"永远只是**减少**权限。`enforce_version_binding` 幂等、可反复调用，
判决结果稳定（§5.8）。

### 3.6 UNKNOWN 的三条出口

`pending_decisions()` 给控制台两个合法裁决与一段风险说明：

- `abandon` → `UNKNOWN → CANCELLED`：永不外发，永远不会重复；
- `retry` → 需要**显式** `acknowledged_duplicate_risk=True` **且**认证渠道 + 认证依据，
  然后重新建一次 Approval，走 `UNKNOWN → FAILED_SAFE → AUTHORIZED`，
  由 `execute` 建**新的** attempt（`attempt_no+1`），请求**沿用同一幂等键**，
  原 UNKNOWN attempt 记录**保留**。

`UNKNOWN → FAILED_SAFE` 这一步不是"改判"：UNKNOWN 代表"上次结果未知，需人工闭环"，
人工裁决之后这次尝试就以"由用户确认为安全"结束；随后新 attempt 才是重试本身。
这样任何一次重试在库里都能看到"为什么允许它重试"。

如果通道自己按幂等键去重（`refuse_reused_key=True`），第二次提交会被通道拒绝，
引擎如实报 `FAILED_SAFE` 并写明是通道侧兜底——**不会假装成功**（§5.7）。

---

## 4. 状态机（本模块涉及的转换）

```
ExternalAction:
  PREPARED ──approve/bind_and_authorize──▶ AUTHORIZED ──claim_action──▶ IN_FLIGHT
      ▲                                        │                          │
      │◀──── 失效/撤销/过期/换版（收紧）────────┘                          │
      │                                                                   ├─▶ SUCCEEDED
      └──── UNKNOWN 裁决 retry 后重新授权 ────◀── recover()/record_attempt ─┤
                                                   (EXTERNAL_UNKNOWN)     ├─▶ FAILED_SAFE
                                                                          └─▶ UNKNOWN
  UNKNOWN ──resolve(abandon)──▶ CANCELLED        UNKNOWN ──resolve(retry)──▶ FAILED_SAFE ─▶ AUTHORIZED

Approval:
  ACTIVE ──claim_action 消费──▶ CONSUMED
  ACTIVE ──换版/内容变化──▶ INVALIDATED      ACTIVE ──撤销──▶ REVOKED      ACTIVE ──过期──▶ EXPIRED

Case（本模块只回写，不越权）:
  APPROVED ──claim_action──▶ EXECUTING ──SUCCEEDED──▶ SUCCEEDED
                                       ──UNKNOWN────▶ UNKNOWN
                                       ──FAILED_SAFE─▶ FAILED（仅在 Case 已是 EXECUTING 时）
  UNKNOWN ──用户裁决 retry 成功──▶ SUCCEEDED（UNKNOWN → SUCCEEDED 是合法转换）
```

> `_reconcile_case` 只走 `Case` 状态机允许的转换：非法就**不**硬来（宁可 Case 停在原地，
> 也不写出一个状态机不允许的事务状态）。`FAILED_SAFE` 只在 Case 已是 `EXECUTING` 时落 `FAILED`。

---

## 5. 原始命令与原始输出

> 引用约定（避免"看不懂的原文"与"看起来像原文的转述"）：
>
> 1. `>` 开头是**原始命令**（在 worktree 根目录执行），下面是它的**原始输出**；
> 2. `[evidence] ...` 行是测试自己打印的，属于原始输出；为排版方便，
>    多行 JSON 有时被合并成单行，被裁掉的部分一律用 `...` 标出，**没有改动任何数值**；
> 3. 对象 ID / 幂等键每次运行都随机，同一小节内的 ID 与键取自**同一次运行**的日志，
>    因此可以互相对照；跨小节的 ID 不相同；
> 4. Windows PowerShell 5.1 会把子进程写往 stderr 的证据行标成 `NativeCommandError`，
>    并把带非 ASCII 的 stderr 变成代码页乱码，因此统一 `> log 2>&1` 重定向后读文件，
>    避免"假 exit 1"；本机 Python 为 3.12，设备为 3.10.12。

### 5.1 完整套件（本地，双后端）

```
> $env:PYTHONIOENCODING="utf-8"; $env:ASISTENT_STORE="reference"
> python -m unittest discover -s tests -t . > _all_ref.log 2>&1; echo "exit=$LASTEXITCODE"
exit=0
> Select-String -Path _all_ref.log -Pattern '^(Ran|OK|FAILED)'
Ran 378 tests in 13.792s
OK (skipped=9)

> $env:ASISTENT_STORE="sqlite"
> python -m unittest discover -s tests -t . > _all_sql.log 2>&1; echo "exit=$LASTEXITCODE"
exit=0
Ran 378 tests in 19.370s
OK (skipped=5)
```

基线对照（同一 worktree，未改动任何源文件时）：

```
Ran 314 tests in 11.324s
OK (skipped=5)
```

跳过项构成（`-v` 原文）：4 项 `跳过` 原因为"跨进程崩溃注入需要持久化后端"/
"跨进程账本需要持久化后端"/"跨进程共享状态需要持久化后端"/"重启语义需要持久化后端"
（reference 内存后端无法跨进程共享），5 项为 Windows 平台符号链接限制
（`当前平台/权限不允许创建符号链接`），非本模块引入。

契约零改动（只读 git 命令）：

```
> git diff --name-only main -- asistent/contracts tests/contracts
(空输出)
> git status --short
?? asistent/actions/
?? tests/actions/
```

### 5.2 逐模块（reference 后端）

```
> foreach ($m in @("tests.actions.test_execution_path","tests.actions.test_approval_binding",
    "tests.actions.test_single_execution","tests.actions.test_crash_recovery",
    "tests.actions.test_unknown_decision","tests.actions.test_sender_adapters")) {
    python -m unittest $m 2>&1 | Select-String '^(Ran|OK|FAILED)' }
tests.actions.test_execution_path : Ran 16 tests in 0.058s
tests.actions.test_execution_path : OK
tests.actions.test_approval_binding : Ran 20 tests in 0.100s
tests.actions.test_approval_binding : OK
tests.actions.test_single_execution : Ran 5 tests in 0.035s
tests.actions.test_single_execution : OK (skipped=1)
tests.actions.test_crash_recovery : Ran 6 tests in 0.021s
tests.actions.test_crash_recovery : OK (skipped=1)
tests.actions.test_unknown_decision : Ran 10 tests in 0.057s
tests.actions.test_unknown_decision : OK (skipped=1)
tests.actions.test_sender_adapters : Ran 7 tests in 0.016s
tests.actions.test_sender_adapters : OK (skipped=1)
```

（reference 后端每模块各跳过 1 项"需要持久化后端"的跨进程用例；
同命令在 sqlite 后端输出为全 `OK`、无跳过，见 §5.3 设备端逐模块输出。）

### 5.3 设备复验（RK3588 / Python 3.10.12）

打包与投递（只带源码树与仓库自带脚本；设备侧目录 `~/asistent-spike/mvp5-check/`，
**未触碰 `~/code/asistent`**）：

```
> tar -cf %TEMP%\mvp5-full.tar --exclude=*.log --exclude=*.pyc --exclude=__pycache__ asistent tests fixtures docs contracts scripts SPEC.md README.md .gitignore
> scp %TEMP%\mvp5-full.tar board:~/asistent-spike/mvp5-check/full.tar
```

设备侧直接使用仓库自带的 `scripts/test.sh`（它就是设备测试入口）：

```
$ bash scripts/test.sh 2>&1 | grep -E '^==|^Ran |^OK|^FAILED'
== python: Python 3.10.12
== 契约快照一致性检查（--check）
== 测试（ASISTENT_STORE=reference）
Ran 378 tests in 10.636s
OK (skipped=4)
OK
$ ASISTENT_STORE=sqlite bash scripts/test.sh 2>&1 | grep -E '^==|^Ran |^OK|^FAILED'
== python: Python 3.10.12
== 契约快照一致性检查（--check）
== 测试（ASISTENT_STORE=sqlite）
Ran 378 tests in 13.593s
OK
OK
```

设备端 MVP-5 六个模块（sqlite）：

```
tests.actions.test_execution_path: Ran 16 tests in 0.477s OK
tests.actions.test_approval_binding: Ran 20 tests in 0.623s OK
tests.actions.test_single_execution: Ran 5 tests in 0.737s OK
tests.actions.test_crash_recovery: Ran 6 tests in 0.434s OK
tests.actions.test_unknown_decision: Ran 10 tests in 0.459s OK
tests.actions.test_sender_adapters: Ran 7 tests in 0.355s OK
```

（设备 sqlite 后端 0 跳过：4 项"需要持久化后端"的用例在 sqlite 下真实执行，
符号链接用例在 Linux 上也可执行。）

### 5.4 并发只有一个赢家（SPEC §852）

命令（sqlite 后端，同时含同进程与跨进程两个用例）：

```
> $env:ASISTENT_STORE="sqlite"
> python -m unittest -v tests.actions.test_single_execution > _ev_conc2.log 2>&1; echo "exit=$LASTEXITCODE"
exit=0
test_thread_concurrency_has_exactly_one_winner ... ok
test_losers_are_rejected_while_the_winner_is_still_in_flight ... ok
test_cross_process_concurrency_has_exactly_one_winner ... ok
test_claim_is_one_atomic_call_not_query_then_mark ... ok
test_single_approval_cannot_authorize_a_second_action ... ok
Ran 5 tests in 0.604s
OK
```

同进程 N=8 原始证据（`_ev_conc.log`，截取；JSON 多行已并为单行，`...` 处省略）：

```
[evidence] 并发执行结果 = [
  {"attempt_id": null, "attempt_no": 0, "claimed": false, "code": "ACTION_NOT_CLAIMABLE",
   "reason": "动作当前状态不可领取: SUCCEEDED", "result_class": "REJECTED", "worker": "executor-0"},
  {"attempt_id": null, "attempt_no": 0, "claimed": false, "code": "ACTION_NOT_CLAIMABLE",
   "reason": "动作当前状态不可领取: SUCCEEDED", "result_class": "REJECTED", "worker": "executor-1"},
  {"attempt_id": null, "attempt_no": 0, "claimed": false, "code": "ACTION_NOT_CLAIMABLE",
   "reason": "动作当前状态不可领取: SUCCEEDED", "result_class": "REJECTED", "worker": "executor-2"},
  {"attempt_id": "att_dd84a70895904865b689d80fb6694186", "attempt_no": 1, "claimed": true,
   "code": null, "reason": "远端已接受（有接受证据）", "result_class": "SUCCEEDED", "worker": "executor-3"},
  ... executor-4 / executor-5 / executor-6 / executor-7 均为 claimed=false, REJECTED ...
]
```

（该次运行里失败者都在赢家收尾之后才发起领取，看到的是 `SUCCEEDED`；
下面那条**确定性**用例负责把"在途窗口"钉住。）

在途窗口钉住（`_ev_conc2.log` 原文）：赢家的 `send()` 停在 `Event.wait()`，
此时从库里读到的动作状态是 `IN_FLIGHT`，attempt 也是 `IN_FLIGHT`（结果尚未落库），
另外 7 个 worker 依次发起执行：

```
[evidence] 在途窗口内的失败者 = {
  "winner": {"action_state": "SUCCEEDED", "attempt_no": 1, "sender_called": true, "result_class": "SUCCEEDED", ...},
  "losers": [
    {"action_id": "act_f94e124d357e4ee39de16e8fbaf32835",
     "action_state": "IN_FLIGHT",
     "attempt_id": null, "attempt_no": 0,
     "code": "ACTION_NOT_CLAIMABLE",
     "detail": {"action_id": "act_f94e124d357e4ee39de16e8fbaf32835", "state": "IN_FLIGHT"},
     "idempotency_key": "action:bbc9dfac67f7bda51c32b07b31fa4f67af98c58e31fa896495e6eef17d838e35",
     "reason": "动作当前状态不可领取: IN_FLIGHT",
     "result_class": "REJECTED", "sender_called": false},
    ... 另外 6 个同样 REJECTED / ACTION_NOT_CLAIMABLE / IN_FLIGHT ...
  ]
}
```

断言：7 个失败者全部 `REJECTED` 且原因含 `IN_FLIGHT`；赢家 `SUCCEEDED`；
`GatedSender.call_count == 1`；库里 `ActionAttempt` **始终只有 1 条**（`attempt_no == 1`）。

> 读数说明：`ExecutionOutcome.sender_call_count` 是**通道累计**调用次数（绝对量），
> `sender_called` 才是"**本次**执行是否调用了通道"（增量判定）。
> 所以失败者那两行是 `sender_call_count: 1`（赢家已经调用过）配 `sender_called: false`（自己没调）。

跨进程 N=4 原始证据（`BEGIN IMMEDIATE` 真跨进程互斥；`_ev_conc2.log` 原文）：

```
[evidence] 跨进程竞争 = [
  {"attempt_id": null, "attempt_no": 0, "claimed": false, "code": "ACTION_NOT_CLAIMABLE",
   "reason": "动作当前状态不可领取: SUCCEEDED", "result_class": "REJECTED",
   "sender_call_count": 0, "worker": "worker-0"},
  {"attempt_id": null, "attempt_no": 0, "claimed": false, "code": "ACTION_NOT_CLAIMABLE",
   "reason": "动作当前状态不可领取: SUCCEEDED", "result_class": "REJECTED",
   "sender_call_count": 0, "worker": "worker-1"},
  {"attempt_id": "att_f00edc46de0f4de2a64e61c9f671326e", "attempt_no": 1, "claimed": true,
   "external_ref": "跨进程唯一赢家@local", "result_class": "SUCCEEDED",
   "sender_call_count": 1, "worker": "worker-2"},
  ... worker-3 同 worker-0/1 被拒 ...
]
[evidence] 跨进程落盘提交次数 = 1
```

（4 个子进程各自的 `claimed` 恰好一个为 `true`；`sender_call_count` 总和为 1。）

设备端同一用例输出（`sqlite.log`）：

```
[evidence] 跨进程落盘提交次数 = 1
```

### 5.5 崩溃注入：不重发（SPEC §853-854）

注入方式：子进程领取动作 → 通道"已提交" → `os._exit(9)`，**结果未落库**。
父进程重启后只做恢复。

```
> python -m unittest -v tests.actions.test_crash_recovery.CrashRecoveryTest.test_subprocess_killed_after_submit_recovered_as_unknown_without_resend
test_subprocess_killed_after_submit_recovered_as_unknown_without_resend ... ok
Ran 1 test in 0.298s
OK
```

以下全部取自**同一次运行**（`_ev_crash.log`）：

```
[evidence] 子进程退出码 = 9 输出 =
[evidence] 崩溃后残留状态 = {
  "action_state": "IN_FLIGHT",
  "attempt_states": ["IN_FLIGHT"],
  "claimed_by": "child-executor"
}
[evidence] 恢复结论 = {
  "demoted_action_ids": ["act_c028464757974379b4f8bc3a50557fd2"],
  "pending_decision_action_ids": ["act_c028464757974379b4f8bc3a50557fd2"],
  "reason": "进程重启时动作仍为 IN_FLIGHT",
  "resent_action_ids": [],
  "sender_call_count": 0,
  "unknown_action_ids": ["act_c028464757974379b4f8bc3a50557fd2"]
}
[evidence] 待用户裁决 = [
  {"action_id": "act_c028464757974379b4f8bc3a50557fd2", "action_state": "UNKNOWN",
   "allowed_decisions": ["retry", "abandon"],
   "attempt_id": "att_1de68ffedf014e1d973aded0148ddd58", "attempt_no": 1,
   "case_id": "case_4f4fdc05278d48f9ae317cb6ada2c57b",
   "error_text": "进程重启时动作仍为 IN_FLIGHT", "failure_class": "EXTERNAL_UNKNOWN",
   "idempotency_key": "action:f26b6bcf6373e2aaf22a84ca9cbafc05bf1fa39a28807198184a15d85aafe676",
   "kind": "send_email",
   "warning": "结果未知：未发送证明 ≠ 未发送。重试可能造成重复发送，必须由用户显式确认重复风险并重新审批（SPEC §21.2）。"}
]
```

用户裁决"重试"之后（同一日志续）：

```
[evidence] 用户裁决 = {
  "action_id": "act_c028464757974379b4f8bc3a50557fd2",
  "action_state": "AUTHORIZED",
  "approval_id": "appr_57c779e44c0e42e69fe733a3ac673433",
  "decision": "retry",
  "duplicate_risk_acknowledged": true,
  "next_step": "调用 execute() 建新 attempt；请求沿用同一幂等键 action:f26b6bcf6373e2aaf22a84ca9cbafc05bf1fa39a28807198184a15d85aafe676",
  "reason": "用户核对发件箱确认未发送"
}
[evidence] 裁决后重试 = {
  "action_id": "act_c028464757974379b4f8bc3a50557fd2",
  "action_state": "SUCCEEDED",
  "attempt_id": "att_b78f9165dcc846cba03c0098bed29b44",
  "attempt_no": 2,
  "external_ref": "retry-ref",
  "idempotency_key": "action:f26b6bcf6373e2aaf22a84ca9cbafc05bf1fa39a28807198184a15d85aafe676",
  "result_class": "SUCCEEDED", "sender_called": true
}
[evidence] 提交过的幂等键 = [
  "action:f26b6bcf6373e2aaf22a84ca9cbafc05bf1fa39a28807198184a15d85aafe676"
]
```

即：崩溃前那次提交与裁决后这次重试**用的是同一个幂等键**（集合里只有一个键），
`attempt_no` 从 1 → 2，原 UNKNOWN attempt 记录保留（`ActionAttempt` 状态序列 `["UNKNOWN","SUCCEEDED"]`）。

同用例断言的关键数字：

```
self.assertEqual(1, MailCallLog.submitted_count_on_disk(call_log))   # 崩溃前已提交 1 次
...recover()...
self.assertEqual([], recovery.resent_action_ids)                     # 恢复绝不重发
self.assertEqual(1, MailCallLog.submitted_count_on_disk(call_log))   # 恢复后仍是 1
self.assertEqual(1, MailCallLog.submitted_count_on_disk(call_log))   # 轮询/再 execute 之后仍是 1
```

同进程崩溃注入（`BaseException` 必须原样上抛；`_ev_crash2.log` 原文）：

```
[evidence] 同进程崩溃恢复 = {
  "demoted_action_ids": ["act_a4b42901f7764a0c8fcc7ee808b56f6b"],
  "pending_decision_action_ids": ["act_a4b42901f7764a0c8fcc7ee808b56f6b"],
  "reason": "进程重启时动作仍为 IN_FLIGHT",
  "resent_action_ids": [],
  "sender_call_count": 1,
  "unknown_action_ids": ["act_a4b42901f7764a0c8fcc7ee808b56f6b"]
}
```

库里残留 `IN_FLIGHT`（不经通道，直接走恢复路径；同一日志）：

```
[evidence] 残留 IN_FLIGHT 恢复 = {
  "demoted_action_ids": ["act_a2ce75c53389437090dee97c44687dde"],
  "pending_decision_action_ids": ["act_a2ce75c53389437090dee97c44687dde"],
  "reason": "进程重启时动作仍为 IN_FLIGHT",
  "resent_action_ids": [],
  "sender_call_count": 0,
  "unknown_action_ids": ["act_a2ce75c53389437090dee97c44687dde"]
}
```

### 5.6 未批准 / 成功后重复轮询（SPEC §851、§853）

```
[evidence] 未批准拒绝 = {
  "action_state": "PREPARED",
  "attempt_id": null, "attempt_no": 0,
  "code": "ACTION_NOT_CLAIMABLE",
  "reason": "动作当前状态不可领取: PREPARED",
  "result_class": "REJECTED",
  "sender_call_count": 0, "sender_called": false
}
```

成功后（同一动作）连续轮询与重启（`tests/actions/test_execution_path.py` 原文断言）：

```
polls = [engine.execute(action.id).result_class for _ in range(3)]
recovery = engine.recover()
again = engine.recover()

self.assertEqual(["REJECTED"] * 3, polls)                       # 3 次轮询全部被拒
self.assertEqual([], recovery.demoted_action_ids)
self.assertEqual([], recovery.resent_action_ids)                # 重启恢复不重发
self.assertEqual([], again.resent_action_ids)
self.assertEqual(1, sender.call_count, "重复轮询与重启都不得再调用通道")
self.assertEqual(1, len(sender.log_entries()))                  # 账本只有 1 条
self.assertEqual(1, len(self.store.find("ActionAttempt", where={"action_id": action.id}, limit=10)))
self.assertEqual("SUCCEEDED", str(self.store.get("Case", case.id).state))
```

### 5.7 UNKNOWN 的裁决（SPEC §854）

```
> $env:ASISTENT_STORE="sqlite"
> python -m unittest tests.actions.test_unknown_decision > _ev_unknown.log 2>&1; echo "exit=$LASTEXITCODE"
exit=0
Ran 10 tests in 0.782s
OK
```

覆盖的行为（测试方法名，按源码顺序）：
`test_unknown_is_never_retried_automatically`、`test_retry_requires_duplicate_risk_acknowledgement`、
`test_retry_requires_authenticated_approval`、`test_unknown_decision_rejects_unknown_verb`、
`test_resolve_unknown_only_accepts_unknown_state`、`test_abandon_cancels_and_never_sends`、
`test_retry_creates_new_attempt_reusing_the_same_idempotency_key`、
`test_retry_against_a_key_deduping_channel_is_reported_truthfully`、
`test_second_cycle_of_unknown_needs_another_decision`、`test_unknown_survives_restart_and_stays_blocked`。

不自动重试（`_ev_unknown.log` 原文，JSON 多行已并为单行）：

```
[evidence] UNKNOWN 待裁决 = [
  {"action_id": "act_b84baa78190d41498d9e5b1de8c2b1b7", "action_state": "UNKNOWN",
   "allowed_decisions": ["retry", "abandon"],
   "attempt_id": "att_a333c58558644eea8b7e0e323f3e41de", "attempt_no": 1,
   "case_id": "case_30c8ed949a6a4f1298a4b1e95e28e979",
   "error_text": "响应不可解析", "failure_class": "EXTERNAL_UNKNOWN",
   "idempotency_key": "action:9a9e6441e165709a3a6d090102c13c127470e98e8943a8894439ebf5d8921dd2",
   "kind": "send_email",
   "warning": "结果未知：未发送证明 ≠ 未发送。重试可能造成重复发送，必须由用户显式确认重复风险并重新审批（SPEC §21.2）。"}
]
```

（引擎侧：3 次 `execute` 全部 `ACTION_NOT_CLAIMABLE`；2 次 `recover()` 的 `resent_action_ids` 全为空；
通道调用次数恒为 1，`ActionAttempt` 恒为 1 条，Case 恒为 `UNKNOWN`。）

retry 的两个闸门（无 `acknowledged_duplicate_risk` → `ApprovalRequired`；
`auth_channel="chat"` → `PermissionDenied`；缺 `auth_evidence` → `ValidationError`），
通过后（同一日志）：

```
[evidence] 重试 = {
  "attempts": ["UNKNOWN", "SUCCEEDED"],
  "resolution": {
    "action_id": "act_99e54824ea674da898821fdf8e2e6a95",
    "action_state": "AUTHORIZED",
    "approval_id": "appr_3440ef38a7a84f26b8af5e113810b198",
    "decision": "retry",
    "duplicate_risk_acknowledged": true,
    "next_step": "调用 execute() 建新 attempt；请求沿用同一幂等键 action:6cd81bdd42330dbc4c4b19a630f769c2dbf388429d24a8113d83523253c0139c",
    "reason": "用户核对发件箱确认未发送"
  },
  "second": {"action_id": "act_99e54824ea674da898821fdf8e2e6a95", "action_state": "SUCCEEDED",
             "attempt_id": "att_12efd5286a0e49e18fbbd4c33a6ddae6", "attempt_no": 2,
             "result_class": "SUCCEEDED", "external_ref": "fake-message-id@local",
             "idempotency_key": "action:6cd81bdd42330dbc4c4b19a630f769c2dbf388429d24a8113d83523253c0139c"},
  "submitted_keys": ["action:6cd81bdd42330dbc4c4b19a630f769c2dbf388429d24a8113d83523253c0139c"]
}
```

通道侧去重兜底（`refuse_reused_key=True`）时如实报 `FAILED_SAFE`：

```
[evidence] 通道侧去重兜底 = {"second": {"result_class": "FAILED_SAFE", "attempt_no": 2, ...},
                             "events": ["submit", "refused"]}
```

`abandon`：动作 → `CANCELLED`，此后 `execute` 恒被拒，通道调用次数不变。

### 5.8 内容变化使审批失效（SPEC §855）

```
> $env:ASISTENT_STORE="sqlite"
> python -m unittest tests.actions.test_approval_binding > _ev_approval.log 2>&1; echo "exit=$LASTEXITCODE"
exit=0
Ran 20 tests in 1.116s
OK
```

换版（草稿产生 DraftVersion-2，动作内容同步到新版本；`_ev_approval.log` 原文）：

```
[evidence] 正文换版 = {
  "changed_fields": ["body", "draft_version_id"],
  "stale": [["appr_bfbba113f8cc46ef891c32ec0f18e0cf",
             "审批绑定的内容已变化，必须重新确认；变化字段: body,draft_version_id"]]
}
[evidence] 重新确认后可发送 = {
  "action_id": "act_2c88bdaf8b894939a9390f8424b0f293",
  "action_state": "SUCCEEDED",
  "attempt_id": "att_5515371d0f004b4aafa4175a1f5d9a84",
  "attempt_no": 1,
  "external_ref": "after-reconfirm",
  "result_class": "SUCCEEDED"
}
```

各字段路径（`changed_fields` 原文，逐用例打印）：

```
收件人变化   → ["to"]
Bcc 变化     → ["bcc"]
附件哈希变化 → ["attachments"]
依据版本变化 → ["evidence_versions"]
仅空白差异   → []（不失效，动作仍为 AUTHORIZED）
```

同时断言：失效后动作回落 `PREPARED`、`execute` 返回 `REJECTED`、`sender.call_count == 0`、
通道调用账本无 `submit` 条目；重新确认（`create_approval` + `bind_and_authorize`）后才发送，
且只发送一次。

### 5.9 原子领取而非"先查后标记"

```
> $env:ASISTENT_STORE="sqlite"
> python -m unittest -v tests.actions.test_single_execution.SingleExecutionTest.test_claim_is_one_atomic_call_not_query_then_mark > _ev_atomic.log 2>&1; echo "exit=$LASTEXITCODE"
exit=0
test_claim_is_one_atomic_call_not_query_then_mark ... ok
Ran 1 test in 0.060s
OK

[evidence] 存储+通道调用序列 = ["get", "get", "find_one", "find_one", "find_one", "claim_action", "now",
                               "sender.send", "record_attempt", "get", "find_one", "now", "record_execution"]
```

断言：`claim_action` 恰好 1 次、顺序为 `claim_action < sender.send < record_attempt`、
引擎调用序列中**没有** `put`、**没有** `transition`（`IN_FLIGHT` 只可能由 `claim_action` 打），
整个执行路径只允许出现白名单内的存储接口。

---

## 6. SPEC §851-855 逐条对照

### §851

> Given 未批准的发送动作，When Agent 尝试执行，Then 动作被拒绝且无外部副作用。

| 触发场景 | 期望 | 实测 | 测试 |
| --- | --- | --- | --- |
| 动作停在 `PREPARED`（未批准） | 拒绝、0 副作用 | `REJECTED` / `ACTION_NOT_CLAIMABLE`，`sender_call_count=0`，`ActionAttempt=0` | `test_execution_path.NoApprovalNoSideEffectTest.test_unapproved_action_is_rejected_with_zero_calls` |
| 未批准 + 严格模式（`raise_on_reject=True`） | 抛错给调用方，仍 0 副作用 | `ActionNotClaimable` | `...test_unapproved_action_raises_when_asked` |
| 审批已 `REVOKED` 且动作仍是 `AUTHORIZED`（被绕过） | 由原子领取拦下 | `REJECTED` / `APPROVAL_INVALID`，0 次外呼 | `...test_approval_revoked_out_of_band_is_caught_by_atomic_claim` |
| 未注入外发通道 | 拒绝且**不领权**（不留假 `IN_FLIGHT`） | `OUTBOUND_CHANNEL_MISSING`，动作仍 `AUTHORIZED` | `...test_missing_channel_refuses_without_claiming` |
| 非"逐次审批"类动作 | 拒绝 | `REJECTED` / `ACTION_NOT_CLAIMABLE`（`PREPARED`） | `...test_local_kind_is_not_sent_out` |
| 动作缺幂等键 | 拒绝，不现造键 | `REJECTED` / `VALIDATION_ERROR`，0 次外呼 | `test_sender_adapters...test_engine_refuses_to_send_a_stored_action_without_a_key` |
| `assert_authenticated_channel` 直接校验 | `chat` 不是认证渠道 | `PermissionDenied`，不落 Approval | `test_approval_binding.AuthenticatedChannelTest.test_chat_text_is_not_an_approval` |

### §852

> Given 用户批准 DraftVersion-3，When DraftVersion-3 未变化且 Approval 有效，
> Then 只允许一个执行尝试获得执行权。

| 维度 | 实测 |
| --- | --- |
| 同进程 8 线程（`Barrier` 对齐） | 赢家 1、被拒 7（原因可读）、`sender.call_count=1`、`ActionAttempt=1`、Approval `CONSUMED` |
| 跨进程 4 进程（sqlite `BEGIN IMMEDIATE`） | 赢家 1、被拒 3、落盘提交次数 `1` |
| 审批一次性 | 同一 Approval 被违规绑到第二个动作 → `APPROVAL_INVALID`，0 次外呼 |
| 版本未变化时批准才有效 | Draft 变版即失效（§5.8），`claim_action` 自己也会拒绝 |

对应测试：`test_single_execution.SingleExecutionTest` **5 项**（含"在途窗口钉住"那条）。

### §853

> Given 发送成功后重复轮询或重启，When 恢复任务，Then 不再发送第二次。

| 场景 | 实测 |
| --- | --- |
| 轮询 3 次 | 全部 `REJECTED`（`ACTION_NOT_CLAIMABLE`），`sender.call_count` 恒为 1，账本恒为 1 条 |
| 重启（新 store 连接）后 `recover()` ×2 | `demoted_action_ids=[]`、`resent_action_ids=[]`、0 次通道调用 |
| 重启后再次 `execute()` | `REJECTED`，库中 `Execution` 与 `ActionAttempt` 仍各只有 1 条 |
| 崩溃后再恢复（更严苛） | 转 `UNKNOWN` 且落盘提交次数保持 1（§5.5） |

### §854

> Given 发送结果 UNKNOWN，When 恢复任务，Then 不自动重试，Case 显示 UNKNOWN 并等待用户裁决。

| 维度 | 实测 |
| --- | --- |
| 通道返回未知 / 超时 | `UNKNOWN` + `EXTERNAL_UNKNOWN`，Case → `UNKNOWN` |
| 自相矛盾结果 | 降级为 `UNKNOWN` 并附 `mismatch` |
| `IN_FLIGHT` 残留 → `recover()` | 降级 `UNKNOWN`，`resent_action_ids` 为空 |
| 裁决前 | `pending_decisions()` 列出动作、原 attempt、错误文本与风险提示；`execute` 恒被拒 |
| `retry` | 需认证渠道 + `acknowledged_duplicate_risk=True`；新 attempt、同幂等键、原记录保留 |
| `abandon` | `CANCELLED`，永久不再外发 |
| 多轮 | 第二次又 UNKNOWN → 必须再裁决一次（`attempt_no` 3 才能成功） |

### §855

> Given 草稿、收件人或关键依据变化，When 旧 Approval 被检查，Then Approval 失效且必须重新确认。

| 变化类型 | 结果 |
| --- | --- |
| 草稿新版本 | `INVALIDATED`（原因含"草稿已产生新版本"）+ 动作回落 `PREPARED` + 执行被拒 |
| 正文 | `changed_fields=["body", "draft_version_id"]` |
| 收件人 / Bcc | `["to"]` / `["bcc"]` |
| 附件内容哈希 | `["attachments"]` |
| 关键依据版本 | `["evidence_versions"]` |
| 仅规范化等价差异 | 不失效（防误伤） |
| 过期 | `EXPIRED`（`sweep_expired_approvals`）+ 动作回落；未 sweep 时由原子领取拒绝 |
| 撤销 | `REVOKED` + 动作回落 |
| 重新确认 | 新 Approval → `bind_and_authorize` → 只发送一次 |

---

## 7. 已知限制（明确不做与不能做的）

1. **reference 后端只支持同进程并发**。`ReferenceStore` 是内存对象 + 进程内 `RLock`，
   跨进程共享状态无从谈起。因此"跨进程唯一赢家"与"崩溃/重启"用例只在 sqlite 后端跑，
   在 reference 后端显式 `skip` 并写明理由（不是隐藏失败）。
   跨进程互斥的真正保障来自 SQLite 的 `BEGIN IMMEDIATE` + WAL，这一点在设备上也复验过。
2. **只接入受控邮件外发通道**：`build_request` 要求 `connector` 以 `mail:` 开头，
   其它外部动作类型（网盘、日历等）留待后续 MVP，面对未知类型一律拒绝而不是猜。
3. **附件本轮不发送**：摘要里只有 `AttachmentRef(name, content_hash)`，没有真实路径；
   带附件的动作会被拒绝（`ValidationError`），而不是静默把附件丢掉。
4. **单节点假设**：节点标识只用于审计与 `claimed_by`，不做分布式租约；
   多节点同时操作同一个库要靠 SQLite 的文件锁，未做跨机验证。
5. **过期清扫需要外部触发**：`sweep_expired_approvals()` 是幂等的纯函数式清扫，
   需要调度器（MVP-6/守护进程）定期调用；不调用也不会出事——
   原子领取自己会拒绝过期审批（§5.8 已覆盖该路径）。
6. **`UNKNOWN` 的最终闭环依赖用户**：设计如此。系统不会为了"把状态清干净"而自动重试。
7. **通道去重能力未知**：核心不依赖通道去重（幂等键是核心自己分配的、
   跨重试稳定的），通道若自己去重，只会让结果更保守（`FAILED_SAFE`）。

---

## 8. 契约变更与 CP 提案（未改任何契约文件）

`contracts/**`、`tests/contracts/**` 一行未改。以下 4 条是使用中发现的契约层面的缺口，
提给契约维护者在下一轮裁决；本模块均在**不修改契约**的前提下做出了保守处理。

| 编号 | 现象 | 复现 | 本模块的临时处理 | 建议 |
| --- | --- | --- | --- | --- |
| **CP-20261010-13** | `actions.check_approval_valid` 计算 `detail["changed_fields"]` 时把当前摘要**与自己**求差（`diff_keys(current_summary)`），导致该字段恒为空列表，控制台无法显示"哪里变了" | `tests.actions.test_approval_binding.ContractChangedFieldsHoleTest.test_changed_fields_are_readable`，原始输出：契约 `detail.changed_fields == []`，而本模块 `check_action_approval().changed_fields == ["bcc","body"]` | `approval.py` 用 `Approval.summary_shown` 还原获批摘要，自行算出真实变化字段并放进 `ApprovalCheck.changed_fields` | 让契约函数接收"获批摘要 + 当前摘要"一对参数（或从 store 侧取 `summary_shown`），使 `changed_fields` 真正可用 |
| CP-20261010-14 | 契约**散文**里提到了 `FAILED_UNSAFE`，但 `ActionState` 枚举里没有它 | `docs/contracts/CONTRACT.md:362` 写"状态机（`FAILED_SAFE`/`FAILED_UNSAFE`/`UNKNOWN`）"；而 `asistent/contracts/objects.py` 的 `ActionState = ['PREPARED','AUTHORIZED','IN_FLIGHT','SUCCEEDED','FAILED_SAFE','UNKNOWN','CANCELLED']` | 代码只使用枚举里存在的 7 个状态；"安全/不安全"由 `result_class` + `failure_class` 表达 | 统一措辞（改散文或补枚举），避免"文档里有一个代码里不存在的状态" |
| CP-20261010-15 | `store.approve(approval)` 没有"同一动作只允许一个有效审批"的原语：重复批准会留下多条 `ACTIVE`，执行侧只靠 `claim_action` 兜底 | 实测：对同一动作批两次 → `同一动作的 ACTIVE 审批数 = 2 ['appr_409749c6ff4d4dffa7add18614d44781', 'appr_616ef739680143429f83c8d9f2d64748']`，第二次批准返回 `True` | `create_approval` 把 `action_id` 写进审批，`bind_and_authorize` 校验 `approval.action_id`；真正的复用由 `claim_action` 拒绝（有测试：`test_single_approval_cannot_authorize_a_second_action`） | 考虑给 `approve()` 加"同一 `action_id` 至多一个 `ACTIVE`"的约束（`RevisionConflict` 或复用旧对象） |
| CP-20261010-16 | `approve()` 在 `expires_at is None` 时静默补 24 小时，签名看不出 TTL（`approve(self, approval: Approval)`） | `asistent/contracts/reference.py:486-487`、`asistent/storage/sqlite_store.py:973-974`：`approval.expires_at = add_seconds(approval.created_at_approval, 24 * 3600)` | `approval.py` 导出 `APPROVAL_TTL_SECONDS = 24*3600` 并显式传值，与契约行为一致（已在此说明） | 把 TTL 提到签名/常量，避免"隐式 24 小时" |

---

## 9. 复现步骤

```powershell
cd D:\dsh\个人助手\.worktrees\mvp-5-actions
$env:PYTHONIOENCODING = "utf-8"

# 1) 双后端全量
$env:ASISTENT_STORE = "reference"; python -m unittest discover -s tests -t . > _all_ref.log 2>&1; echo "exit=$LASTEXITCODE"; Get-Content _all_ref.log -Tail 3
$env:ASISTENT_STORE = "sqlite";    python -m unittest discover -s tests -t . > _all_sql.log 2>&1; echo "exit=$LASTEXITCODE"; Get-Content _all_sql.log -Tail 3

# 2) MVP-5 六个模块
foreach ($m in @("tests.actions.test_execution_path","tests.actions.test_approval_binding","tests.actions.test_single_execution","tests.actions.test_crash_recovery","tests.actions.test_unknown_decision","tests.actions.test_sender_adapters")) {
  python -m unittest $m 2>&1 | Select-String '^(Ran|OK|FAILED)'
}

# 3) 契约零改动
git diff --name-only main -- asistent/contracts tests/contracts
git status --short
```

```powershell
# 4) 设备复验（RK3588 / Python 3.10.12）；只写自己的目录，绝不碰 ~/code/asistent
#    必须带上 fixtures/（tests.ingest 的公开夹具在仓库根目录）与 scripts/（设备测试入口）
tar -cf "$env:TEMP\mvp5-full.tar" --exclude="*.log" --exclude="*.pyc" --exclude="__pycache__" `
    asistent tests fixtures docs contracts scripts SPEC.md README.md .gitignore
scp "$env:TEMP\mvp5-full.tar" board:~/asistent-spike/mvp5-check/full.tar
```

```bash
# 设备侧：仓库自带的 scripts/test.sh 就是设备测试入口（只写 ~/asistent-spike/mvp5-check/）
ssh board 'cd ~/asistent-spike/mvp5-check/run && tar -xf ../full.tar \
  && bash scripts/test.sh 2>&1 | grep -E "^==|^Ran |^OK|^FAILED" \
  && ASISTENT_STORE=sqlite bash scripts/test.sh 2>&1 | grep -E "^==|^Ran |^OK|^FAILED"'
```

实测输出（原文，`======` 分隔行已省略）：

```
== python: Python 3.10.12
== 契约快照一致性检查（--check）
== 测试（ASISTENT_STORE=reference）
Ran 378 tests in 10.636s
OK (skipped=4)
OK
----- sqlite -----
== python: Python 3.10.12
== 契约快照一致性检查（--check）
== 测试（ASISTENT_STORE=sqlite）
Ran 378 tests in 13.593s
OK
OK
```

（`== 契约快照一致性检查（--check）` 是 `scripts/test.sh` 自带的一步：契约快照与
`docs/contracts/CONTRACT.md` 一致，说明本分支没有动契约。）

若只想看 MVP-5 六个模块（在设备上逐模块跑）：

```bash
ssh board 'cd ~/asistent-spike/mvp5-check/run \
  && for m in tests.actions.test_execution_path tests.actions.test_approval_binding \
              tests.actions.test_single_execution tests.actions.test_crash_recovery \
              tests.actions.test_unknown_decision tests.actions.test_sender_adapters; do \
       echo "$m: $(ASISTENT_STORE=sqlite PYTHONIOENCODING=utf-8 /usr/bin/python3 -m unittest $m 2>&1 | grep -E "^Ran |^OK|^FAILED" | tr "\n" " ")"; \
     done'
```

---

## 10. 一句话结论

外部动作只有一条通往世界的路径（`claim_action` 之后才可能 `sender.send`），
这条路径上任何不确定都向"拒绝"或"UNKNOWN"收敛；因此**要么没发，要么只发一次**，
并且每一次"没发/发了/不知道"都能在库里找到对应的状态、attempt、幂等键与可读原因。
