"""MVP-8 常驻服务：把前面各层串成一台**真的在守着**的助手（SPEC §4.1、§13、§14.2、§873-877）。

MVP-0..7 都是库：各自有测试，但没有任何东西在跑。本包补的就是这一块：

```text
config.py    配置（仓库外 JSON，安全默认值，绝不读凭据内容）
health.py    健康/状态快照（轮次、最近同步、待处理计数、正在等什么、最近错误）
answers.py   把"用户已回答的追问"接进下一轮理解（MVP-3 没有这条路径，这里兜底，见下）
phases.py    一轮里的六件事：恢复 → 同步 → 采集/索引 → 理解/草稿 → 审批清扫与执行 → 归档
runtime.py   ServiceRuntime：一轮逻辑 + 常驻循环 + 优雅退出 + 健康快照落盘
cli.py       `python3 -m asistent.service --once|--loop|--status`（唯一入口）
```

四条不可让渡的约束（都被 `tests/service/**` 与 `docs/mvp/mvp-8.md` 的原始输出钉住）
============================================================================

1. **一轮只有一份实现**：`--once` 与常驻循环调用**同一个** `ServiceRuntime.run_once()`，
   区别只有"调用几次"。因此"分轮驱动的证据"与"常驻的证据"说的是同一段代码。
2. **状态只来自存储**：快照、待处理计数、待审批、UNKNOWN 全部现场从 `StoreProtocol` 查；
   进程内存里没有"上次处理到哪"的业务语义。分轮驱动用**新进程**读同一个库来证明这一点。
3. **一轮失败不接管服务**：每个阶段各自 `except Exception`，失败写进快照与审计后继续下一轮；
   唯一**不**吞的是 `BaseException`（`SimulatedCrash` / `KeyboardInterrupt`）——
   那正是"被强杀"的语义，必须让 IN_FLIGHT 留在库里，交给下一轮的 `recover()` 转 UNKNOWN。
4. **恢复不重发**：启动/每轮的恢复阶段只做 `IN_FLIGHT → UNKNOWN`，本包内**没有任何**
   在恢复路径上调用外发通道的代码；`asistent.actions` 的一次性执行权是第二道兜底。

关于 `answers.py` 的兜底（**下层缺口，不是本层发明**）
=====================================================

契约 v0.7 的 `Question` 没有 `kind` 字段，MVP-3 的抽取器又是**纯函数**：同一封邮件重跑
永远得到同一批追问，因此"用户答完追问 → 该阻塞项解除 → 产出草稿"这条路径在下层**不存在**
（`asistent/console/service.py` 的 `_refresh_case_waiting_state` 把事务置为
`WAITING_FOR_CONTEXT` 并写明"等待 Agent 基于回答重新分析"——那就是本层要干的活）。

本层的兜底做法：`AnswerAwareUnderstander` 包住理解器，在**输出**上把"已经被用户回答过"
的阻塞型追问标成已解除（`AnswerAwareUnderstandingResult` 覆写 `draftable()` /
`blocking_reasons()`），从而让 `process_document` 照常落对象、照常产出草稿。
匹配依据是**存储里的 `Question` 对象**（`state=ANSWERED` + 问题文本 + `affects`），
不是猜。冲突类追问（`kind="conflict"`）**永不**由本层解除——那是 MVP-6 的裁决路径。

> Lead 已宣布契约 v0.8 会补 `Question.kind`；届时本兜底可以简化成"按 `kind` + `state`
> 过滤候选"，不必再按问题文本比对（见 `docs/mvp/mvp-8.md` 的 CP 提案）。
"""

from __future__ import annotations

from .answers import (
    ANSWER_RESOLVABLE_KINDS,
    AnswerAwareUnderstander,
    AnswerAwareUnderstandingResult,
    answered_questions,
    record_answer_facts,
)
from .config import (
    DEFAULT_INTERVAL_SECONDS,
    SERVICE_CONFIG_NAME,
    ServiceConfig,
    load_config,
)
from .health import ServiceState, WaitingOn
from .phases import RoundReport
from .runtime import ServiceRuntime, run_once, run_loop

__all__ = [
    # 配置
    "ServiceConfig",
    "load_config",
    "SERVICE_CONFIG_NAME",
    "DEFAULT_INTERVAL_SECONDS",
    # 健康快照
    "ServiceState",
    "WaitingOn",
    # 一轮
    "ServiceRuntime",
    "RoundReport",
    "run_once",
    "run_loop",
    # 追问回答 → 重新理解（兜底）
    "AnswerAwareUnderstander",
    "AnswerAwareUnderstandingResult",
    "ANSWER_RESOLVABLE_KINDS",
    "answered_questions",
    "record_answer_facts",
]

__version__ = "0.8.0"
