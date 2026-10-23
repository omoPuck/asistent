"""崩溃注入子进程：**已提交外发、结果未落库就被强杀**（SPEC §13.3、§21.7 第 1079 行）。

```bash
python3 -X utf8 tests/service/crash_child_service.py <config_path> <call_log_path>
```

它复用生产代码的**同一条外发路径**（`ServiceRuntime` → 阶段 6 `outbound` → `ActionEngine.execute`
→ `claim_action` → `sender.send`），只把发送通道换成"剧本 = 提交后立刻 `os._exit(9)`"的假通道。
因此进程死掉时：

* `ExternalAction` 已经被 `claim_action` 原子地置为 `IN_FLIGHT`（在库里的，不是内存）；
* 通道账本里**已经有**那一次 `submit` 行（flush + fsync 过）；
* 而"结果"永远没机会落库 —— 这正是真实世界里最难处理的那一刻。

父进程要断言的是：下一轮 `recover` 把它转成 `UNKNOWN`，并且**一次都不重发**。
退出码 9 是本进程的固定约定（`CRASH_EXIT_CODE`），父进程据此确认"是真的被强杀"。
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from asistent.actions import MailCallLog, ScriptedMailSender  # noqa: E402
from asistent.actions.senders import SendScriptStep  # noqa: E402
from asistent.service import ServiceRuntime  # noqa: E402
from asistent.service.config import load_config  # noqa: E402

CRASH_EXIT_CODE = 9


def hard_kill() -> None:
    """模拟被强杀：不走解释器清理、不写任何收尾状态。"""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(CRASH_EXIT_CODE)


def main(argv: list) -> int:
    if len(argv) < 3:
        sys.stderr.write("用法: crash_child_service.py <config_path> <call_log_path>\n")
        return 2
    config_path, call_log_path = argv[1], argv[2]
    config = load_config(config_path)
    sender = ScriptedMailSender(
        log=MailCallLog(call_log_path),
        script=[SendScriptStep.crash_after_submit("已提交到通道，结果未落库即被强杀")],
        crash_hook=hard_kill,
    )
    runtime = ServiceRuntime(config, sender=sender)
    try:
        print("[crash-child] pid=%d 配置=%s" % (os.getpid(), config_path), flush=True)
        report = runtime.run_once()
        # 正常情况下走不到这里：`SimulatedCrash`/`os._exit` 会先结束进程
        print("[crash-child] 未按预期崩溃：%s" % report.summary_line(), flush=True)
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    sys.exit(main(sys.argv))
