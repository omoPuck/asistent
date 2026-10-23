"""跨进程抢执行权子进程（SPEC §21.2「一个原子调用」、§16 AT-07「两个 worker 抢同一动作」）。

用法（父进程并发拉起多个）：

    python tests/actions/claim_child.py <db_path> <call_log_path> <action_id> <worker>

打印一行 JSON：`{"worker": ..., "claimed": bool, "result_class": ..., "code": ..., "reason": ...}`。

`claimed=true` 表示这个 worker 拿到了执行权并真的调用了外发通道；
`claimed=false` 表示被原子领取挡下（`ACTION_NOT_CLAIMABLE` 或 `APPROVAL_INVALID`）。
所有 worker 共用一个调用账本文件，因此父进程可以直接数出"跨进程总共提交了几次"。
"""

from __future__ import annotations

import json
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


def main(argv):
    if len(argv) != 5:
        sys.stderr.write("用法: claim_child.py <db_path> <call_log_path> <action_id> <worker>\n")
        return 2
    db_path, call_log_path, action_id, worker = argv[1], argv[2], argv[3], argv[4]

    store = SqliteStore(path=db_path)
    sender = ScriptedMailSender(
        name="claim-child",
        log=MailCallLog(call_log_path),
        script=[SendScriptStep.succeed("跨进程唯一赢家@local")],
    )
    engine = ActionEngine(store, sender, node=worker)
    outcome = engine.execute(action_id)
    payload = {
        "worker": worker,
        "claimed": outcome.claimed,
        "result_class": outcome.result_class,
        "code": outcome.code,
        "reason": outcome.reason,
        "attempt_id": outcome.attempt_id,
        "attempt_no": outcome.attempt_no,
        "sender_call_count": outcome.sender_call_count,
        "external_ref": outcome.external_ref,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()
    store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    sys.exit(main(sys.argv))
