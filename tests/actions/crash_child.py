"""崩溃注入子进程：模拟"已标记 IN_FLIGHT、尚未确认结果"时被强杀（SPEC §21.2、§16 AT-07）。

用法（父进程调用）：

    python tests/actions/crash_child.py <db_path> <call_log_path> <action_id>

行为：打开 sqlite 库 → 用剧本型假通道执行一次动作；通道**先**把这次提交写进
落盘调用账本（flush + fsync），**再**调用 `os._exit(9)`。

于是进程消失时留下的状态恰好是"可能已经触达外部、但结果没落库"：
- 库里：`ExternalAction.state = IN_FLIGHT`、`ActionAttempt.state = IN_FLIGHT`；
- 盘上：调用账本里 1 行 `event="submit"`。

父进程据此断言"重启恢复不重发"：恢复后动作变 `UNKNOWN`，而账本行数**仍然**是 1。

只对 sqlite 后端有意义：参考实现的状态在内存里，进程死了就没了。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from asistent.actions import (  # noqa: E402 - 需要先修 sys.path
    ActionEngine,
    MailCallLog,
    ScriptedMailSender,
    SendScriptStep,
)
from asistent.storage.sqlite_store import SqliteStore  # noqa: E402


#: 模拟被强杀（父进程断言这个退出码）
CRASH_EXIT_CODE = 9


def main(argv):
    if len(argv) != 4:
        sys.stderr.write("用法: crash_child.py <db_path> <call_log_path> <action_id>\n")
        return 2
    db_path, call_log_path, action_id = argv[1], argv[2], argv[3]

    def hard_kill():
        # 不跑 atexit / finally / flush：就是"被强杀"的样子
        os._exit(CRASH_EXIT_CODE)

    store = SqliteStore(path=db_path)
    sender = ScriptedMailSender(
        name="crash-child",
        log=MailCallLog(call_log_path),
        script=[SendScriptStep.crash_after_submit("已提交，结果未落库即被强杀")],
        crash_hook=hard_kill,
    )
    engine = ActionEngine(store, sender, node="child-executor")
    outcome = engine.execute(action_id)
    # 正常情况下走不到这里（crash_hook 会 os._exit）；留着是为了让"没崩"可诊断
    sys.stdout.write("子进程未按预期崩溃: %s\n" % (outcome.to_dict(),))
    sys.stdout.flush()
    store.close()
    return 1


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    sys.exit(main(sys.argv))
