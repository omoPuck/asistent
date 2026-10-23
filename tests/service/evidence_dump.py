"""把一台服务的状态**原样倒出来**（取证明用；`tests/service/**` 里的工具，不是生产代码）。

```bash
python3 -X utf8 tests/service/evidence_dump.py /path/to/service.json --rounds 2
```

它做两件事：

1. 在**当前进程**里连续跑 N 轮（`ServiceRuntime.run_once()`，与 `--once` 同一份实现），
   每轮打印逐阶段证据行；
2. 把存储里的对象清单原样倒出来：事务状态、时间字段、追问、草稿版本与哈希、
   审批、动作（含幂等键与结论）、归档任务、审计动作统计、健康快照。

`docs/mvp/mvp-8.md` 里的"原始输出"就是这些字，没有二次加工。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from asistent.service import ServiceRuntime  # noqa: E402
from asistent.service.config import load_config  # noqa: E402


def _line(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def dump_objects(store: Any) -> None:
    _line("对象清单（全部现场从存储读）")

    cases = sorted(store.find("Case", limit=None), key=lambda item: str(item.created_at or ""))
    print("Cases: %d" % len(cases))
    for case in cases:
        print(
            "  - %s state=%s archive_state=%s waiting_reason=%r next_action=%r blocked_reason=%r rules=%s"
            % (
                case.id,
                case.state,
                getattr(case, "archive_state", ""),
                getattr(case, "waiting_reason", ""),
                getattr(case, "next_action", ""),
                getattr(case, "blocked_reason", ""),
                list(case.rule_ids or []),
            )
        )

    documents = sorted(store.find("SourceDocument", limit=None), key=lambda item: str(item.created_at or ""))
    print("SourceDocuments: %d" % len(documents))
    for document in documents:
        print(
            "  - %s origin=%s index_state=%s hash=%s inbox=%s"
            % (
                document.id,
                os.path.basename(str(document.origin_path or "")),
                getattr(document, "index_state", ""),
                str(document.content_hash or "")[:16],
                str(getattr(document, "inbox_state", "") or ""),
            )
        )

    facts = store.find("Fact", limit=None)
    print("Facts: %d" % len(facts))
    for fact in facts:
        fields = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in list(fact.temporal_fields or [])]
        print("  - %s subject=%r 时间字段=%s links=%s" % (fact.id, fact.subject, fields, dict(fact.links or {})))

    questions = store.find("Question", limit=None)
    print("Questions: %d" % len(questions))
    for question in questions:
        print(
            "  - %s state=%s affects=%s question=%r"
            % (question.id, question.state, list(question.affects or []), question.question[:80])
        )

    answers = store.find("Answer", limit=None)
    print("Answers: %d" % len(answers))
    for answer in answers:
        print("  - %s question=%s text=%r" % (answer.id, answer.question_id, answer.answer_text[:80]))

    drafts = store.find("Draft", limit=None)
    print("Drafts: %d" % len(drafts))
    for draft in drafts:
        print("  - %s state=%s target_ref=%s versions=%s" % (draft.id, draft.state, draft.target_ref, list(draft.version_ids or [])))

    versions = store.find("DraftVersion", limit=None)
    print("DraftVersions: %d" % len(versions))
    for version in versions:
        print(
            "  - %s draft=%s v%d hash=%s body=%r"
            % (version.id, version.draft_id, int(version.version_no), str(version.content_hash or "")[:16], version.body[:60])
        )

    approvals = store.find("Approval", limit=None)
    print("Approvals: %d" % len(approvals))
    for approval in approvals:
        print(
            "  - %s state=%s approver=%s channel=%s action=%s targets=%s expires=%s"
            % (
                approval.id,
                approval.state,
                approval.approver,
                approval.auth_channel,
                approval.action_id,
                list(approval.target_object_ids or []),
                approval.expires_at,
            )
        )

    actions = store.find("ExternalAction", limit=None)
    print("ExternalActions: %d" % len(actions))
    for action in actions:
        print(
            "  - %s state=%s kind=%s connector=%s key=%s result=%s external_ref=%s attempts=%s"
            % (
                action.id,
                action.state,
                action.kind,
                action.connector,
                action.idempotency_key,
                getattr(action, "result_class", ""),
                getattr(action, "external_ref", ""),
                len(action.attempt_ids or []),
            )
        )

    jobs = store.find("ArchiveJob", limit=None)
    print("ArchiveJobs: %d" % len(jobs))
    for job in jobs:
        print(
            "  - %s state=%s scope=%s ref=%s uploaded=%d verified=%d error=%s"
            % (job.id, job.state, job.scope, job.scope_ref, len(job.uploaded or []), int(job.verified_files or 0), (job.error_text or "")[:60])
        )

    audits = store.audit_trail(limit=None)
    counter: Dict[str, int] = {}
    for event in audits:
        counter[str(event.action)] = counter.get(str(event.action), 0) + 1
    print("AuditEvents: %d %s" % (len(audits), json.dumps(counter, ensure_ascii=False, sort_keys=True)))
    hits = [event for event in audits if str(event.action) == "RULE_HIT"]
    if hits:
        print("RuleHits（含规则版本号）:")
        for event in hits:
            detail = dict(event.detail or {})
            print(
                "  - at=%s case=%s rule=%s v%s effect=%s applied=%s"
                % (event.at, event.case_id, detail.get("rule_id"), detail.get("rule_version"), detail.get("effect"), detail.get("applied"))
            )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="导出服务状态与轮次证据")
    parser.add_argument("config", help="仓库外配置文件路径")
    parser.add_argument("--rounds", type=int, default=1, help="连续跑多少轮")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出快照")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    runtime = ServiceRuntime(config, log=lambda message: print(message, flush=True))
    try:
        for index in range(max(1, int(args.rounds))):
            report = runtime.run_once()
            print("[dump] 第 %d 轮：%s" % (index + 1, report.summary_line()))
        snapshot = runtime.status()
        if args.json:
            print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            _line("健康快照")
            print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2))
        dump_objects(runtime.store)
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - 工具入口
    sys.exit(main())
