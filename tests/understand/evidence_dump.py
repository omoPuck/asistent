"""MVP-3 证据导出：把四个验收场景的**可观察证据**原样打到 stdout。

这是 `docs/mvp/mvp-3.md` 里粘贴的命令与输出来源，因此：

* 只读本地 fixture，只用 `ReferenceStore`（内存，不写仓库数据）；
* 输出**确定**（同一输入 → 同一输出），没有时间戳/随机数；
* 不依赖第三方库。

用法（仓库根目录）：

    python -X utf8 tests/understand/evidence_dump.py
"""

from __future__ import annotations

import io
import json
import os
import sys

if "pytest" not in sys.modules:  # 允许被 unittest 发现时复用
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", newline="\n")
    except Exception:  # pragma: no cover - 无 buffer 的环境（如某些 IDE）
        pass

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
os.chdir(_REPO_ROOT)

from asistent.contracts import ReferenceStore, SourceDocument  # noqa: E402
from asistent.understand import (  # noqa: E402
    ModelBackedUnderstander,
    RuleBasedUnderstander,
    decode_eml_bytes,
    process_document,
)
from asistent.understand import drafts as draft_api  # noqa: E402

FIXTURE_ROOT = "fixtures/public/understand"
FIXED_ANCHOR = "2026-10-05T01:12:00.000Z"


def _put(store, path, document_id, *, case_id=None, anchor=None):
    raw = open(path, "rb").read()
    text = decode_eml_bytes(raw)
    document = store.put(
        SourceDocument(
            id=document_id,
            source_type="email",
            source_system="fixture:understand",
            source_id=os.path.basename(path),
            identity_key=document_id,
            origin_path=path,
            content_hash="sha256-fixture-" + os.path.basename(path),
            received_at=anchor or text.anchor_utc or FIXED_ANCHOR,
            case_id=case_id,
        )
    )
    return document, raw, text


def _line(title):
    print("=" * 78)
    print(title)
    print("=" * 78)


def scenario_1():
    """SPEC §835-836：一封通知里的多个时间必须各自带语义，且不得混用。"""
    _line("场景 1（SPEC §835-836）同一封通知的多个时间各有语义")
    path = os.path.join(FIXTURE_ROOT, "01-notice-multi-time.eml")
    store = ReferenceStore()
    document, raw, text = _put(store, path, "doc_scene1")
    report = process_document(store, document, eml_bytes=raw)
    for field in report.time_fields:
        print(
            "  time %-16s semantic=%-22s original=%-16s normalized_utc=%s confidence=%.2f"
            % (
                field["label_text"],
                field["semantic"],
                field["original_text"],
                field["normalized_utc"],
                field["confidence"],
            )
        )
    print("  case_state=%s draftable=%s" % (report.case_state, report.draftable))
    for outcome in report.outcomes:
        print("  outcome %-13s %-7s %s" % (outcome.kind, outcome.outcome, outcome.detail or outcome.object_id))
    print("  store.stats() =", store.stats())
    return report, store


def scenario_3():
    """SPEC §839-840：缺材料/时间不明 → 追问，不编造。"""
    _line("场景 3（SPEC §839-840）资料缺失与时间不明一律转成追问")
    store = ReferenceStore()
    for index, name in enumerate(("03-missing-material.eml", "04-ambiguous-time.eml"), start=1):
        path = os.path.join(FIXTURE_ROOT, name)
        document, raw, _ = _put(store, path, "doc_scene3_%d" % index)
        report = process_document(store, document, eml_bytes=raw)
        print("  --- %s" % name)
        print("      case_state=%s draftable=%s" % (report.case_state, report.draftable))
        for reason in report.blocking_reasons:
            print("      blocking: %s" % reason)
        for question in store.find("Question", where={"case_id": report.case_id}, limit=None):
            print(
                "      Question id=%s state=%s required_format=%s affects=%s"
                % (question.id, question.state, question.required_format, list(question.affects))
            )
            print("               question   = %s" % question.question)
            print("               background = %s" % question.background)
        print(
            "      draft_ids on Case: %s"
            % [item for item in store.get("Case", report.case_id).draft_ids]
        )
    return store


def scenario_4():
    """SPEC §835-840 第 4 条：两份资料时间冲突 → 不生成可发送结论 + 追问。"""
    _line("场景 4（SPEC §835-840 第 4 条）跨资料时间冲突：不生成可发送结论")
    store = ReferenceStore()
    shared_case_id = "case_conflict_demo"
    first, raw_first, _ = _put(
        store,
        os.path.join(FIXTURE_ROOT, "02a-notice-conflict.eml"),
        "doc_scene4_a",
        case_id=shared_case_id,
        anchor="2026-10-05T06:30:00.000Z",
    )
    first_report = process_document(store, first, eml_bytes=raw_first)
    print("  [资料 A] 报名截止时间 = %s" % _deadline_of(first_report))
    second, raw_second, _ = _put(
        store,
        os.path.join(FIXTURE_ROOT, "02b-notice-conflict.eml"),
        "doc_scene4_b",
        case_id=shared_case_id,
        anchor="2026-10-05T10:05:00.000Z",
    )
    second_report = process_document(store, second, eml_bytes=raw_second)
    print("  [资料 B] 报名截止时间 = %s" % _deadline_of(second_report))

    print("  conflicts = %d" % len(second_report.conflicts))
    for conflict in second_report.conflicts:
        print("    conflict semantic   = %s" % conflict["semantic"])
        print("    conflict values     = %s" % conflict["values"])
        print("    conflict sources    = %s" % conflict["sources"])
        print("    conflict detail     = %s" % conflict["detail"])
        print("    conflict citations  = %d 条" % len(conflict["citations"]))
    questions = store.find("Question", where={"case_id": shared_case_id}, limit=None)
    print("  Question 总数 = %d" % len(questions))
    for question in questions:
        print("    Question id=%s state=%s affects=%s" % (question.id, question.state, list(question.affects)))
        print("             question   = %s" % question.question)
        print("             background = %s" % question.background)

    case = store.get("Case", shared_case_id)
    print("  case.state          = %s" % case.state)
    print("  case.draft_ids      = %s" % list(case.draft_ids))
    print("  case.blocked_reason = %s" % case.blocked_reason)
    print("  draftable           = %s" % second_report.draftable)

    drafts = store.find("Draft", limit=None)
    print("  Draft 对象（保留审计，但状态必须是 STALE）:")
    for draft in drafts:
        print("    Draft id=%s state=%s version_ids=%s" % (draft.id, draft.state, list(draft.version_ids)))
    sendable = [item for item in drafts if str(item.state) == "OPEN"]
    print("  可发送草稿（state=OPEN）数量 = %d（期望 0）" % len(sendable))
    print("  case.draft_ids 中剩余的可发送草稿 = %s（期望 []）" % list(case.draft_ids))
    print("  store.stats()       = %s" % store.stats())
    return second_report, store


def scenario_2():
    """草稿版本化：编辑 → diff → 恢复（恢复产生新版本，历史不动）。"""
    _line("场景 2 草稿版本化：编辑 / diff / 恢复（历史只增不改）")
    path = os.path.join(FIXTURE_ROOT, "05-reply-needed-ok.eml")
    store = ReferenceStore()
    document, raw, _ = _put(store, path, "doc_scene2")
    report = process_document(store, document, eml_bytes=raw)
    draft_id = ""
    for outcome in report.outcomes:
        if outcome.kind == "Draft":
            draft_id = outcome.object_id
    print("  draft_id = %s" % draft_id)
    versions = draft_api.versions_of(store, draft_id)
    print("  v1 body = %r" % versions[0].body)
    print("  v1 citations = %d" % len(versions[0].citations))

    edited = versions[0].body.replace("现回复如下", "现回复如下（已按您的意见精简）")
    v2 = draft_api.new_version(store, draft_id, body=edited, author_id="user", change_reason="用户编辑")
    print("  v2 version_no=%d body=%r" % (v2.version_no, v2.body))

    diff = draft_api.diff_versions(store, draft_id, 1, 2)
    print(
        "  diff v1->v2 identical=%s added=%d removed=%d"
        % (diff.identical, len(diff.added_lines), len(diff.removed_lines))
    )
    for row in diff.added_lines:
        print("    + %s" % row)
    for row in diff.removed_lines:
        print("    - %s" % row)

    restored = draft_api.restore_version(store, draft_id, 1, author_id="user", reason="恢复到 v1")
    print("  restore -> 新版本号=%d 正文==v1? %s 历史版本号=%s"
          % (restored.version_no, restored.body == versions[0].body,
             [item.version_no for item in draft_api.versions_of(store, draft_id)]))
    print("  v1 正文仍然可用（未被覆盖）= %r" % draft_api.version_by_number(store, draft_id, 1).body)
    print("  current_version_id=%s" % draft_api.current_version(store, draft_id).id)
    binding = draft_api.approval_binding(store, draft_id)
    print("  审批绑定 draft=%s version_no=%d version_id=%s hash=%s"
          % (binding.draft_id, binding.version_no, binding.version_id, binding.content_hash[:16]))
    try:
        draft_api.approval_binding(store, draft_id, 1)
        print("  审批历史版本 v1 = 允许（不符合预期）")
    except Exception as exc:
        print("  审批历史版本 v1 被拒绝: %s: %s" % (type(exc).__name__, exc))
    return report, store


def _deadline_of(report):
    for field in report.time_fields:
        if field["semantic"] in ("registration_deadline", "submission_deadline"):
            return "%s (%s)" % (field["normalized_utc"], field["original_text"])
    return "(none)"


def scenario_5():
    """默认路径零模型调用；模型路径必须显式配置 + 预算封顶。"""
    _line("场景 5 默认路径零模型调用；模型路径必须显式配置且预算封顶")

    class FailIfCalled:
        name = "fail-if-called"

        def __init__(self):
            self.calls = 0

        def complete(self, *, prompt, system=""):
            self.calls += 1
            raise AssertionError("默认路径不允许调用模型后端")

    backend = FailIfCalled()
    understander = RuleBasedUnderstander()
    store = ReferenceStore()
    path = os.path.join(FIXTURE_ROOT, "05-reply-needed-ok.eml")
    document, raw, _ = _put(store, path, "doc_scene5")
    report = process_document(store, document, eml_bytes=raw, understander=understander)
    print("  producer            = %s" % report.producer)
    print("  注入的 fail-if-called backend 调用次数 = %d（期望 0）" % backend.calls)
    log = understander.call_log()
    print("  规则理解器调用日志 calls=%d cost=%s blocked_reason=%r" % (log.calls, log.cost, log.blocked_reason))
    print("  draftable=%s Draft 数=%d" % (report.draftable, len(store.find("Draft", limit=None))))

    for label, kwargs in (
        ("未配置预算", {"backend": backend, "budget": None}),
        ("模型后端未配置", {"backend": None, "budget": {"max_calls_per_day": 5}}),
    ):
        try:
            ModelBackedUnderstander(**kwargs)
            print("  ModelBackedUnderstander(%s) = 未拒绝（不符合预期）" % label)
        except Exception as exc:
            print("  ModelBackedUnderstander(%s) 拒绝: %s: %s" % (label, type(exc).__name__, exc))

    # 预算封顶：上限 = 1 次
    counting = _CountingBackend()
    limited = ModelBackedUnderstander(
        backend=counting,
        budget={"max_calls_per_day": 1, "max_cost_per_day": 1.0},
        fallback=None,
    )
    request = _request_for(raw, path, document)
    first = limited.analyze(request)
    print("  第 1 次调用：backend.calls=%d producer=%s question 数=%d"
          % (counting.calls, first.producer, len(first.questions)))
    try:
        limited.analyze(request)
        print("  第 2 次调用未被拦（不符合预期）")
    except Exception as exc:
        print("  第 2 次调用被拦: %s: %s" % (type(exc).__name__, exc))
    final_log = limited.call_log()
    print("  最终调用日志 calls=%d blocked_reason=%r" % (final_log.calls, final_log.blocked_reason))
    print("  counting backend 总调用次数 = %d（期望 1）" % counting.calls)


class _CountingBackend:
    """按调用次数计数的假后端：第 1 次给一个**带引用**的事实，避免触发引用守卫。"""

    name = "counting"

    def __init__(self):
        self.calls = 0
        self.payload = {
            "facts": [
                {
                    "statement": "报名截止时间：10月16日 18:00",
                    "citation": {
                        "source_object_id": "doc_scene5",
                        "source_object_type": "SourceDocument",
                        "path": "fixtures/public/understand/05-reply-needed-ok.eml",
                        "locator": "10月16日 18:00",
                        "supports": "支持「报名截止时间」这一时间字段",
                        "content_hash": "sha256-fixture-05-reply-needed-ok.eml",
                    },
                }
            ],
            "questions": [],
            "drafts": [],
        }

    def complete(self, *, prompt, system=""):
        self.calls += 1
        text = json.dumps(self.payload, ensure_ascii=False)
        return type("_Call", (), {"text": text, "cost": 0.01, "tokens": 10, "model": "fake-model"})()


def _request_for(raw, path, document):
    from asistent.understand import AnalysisInput, decode_eml_bytes as _decode

    text = _decode(raw)
    return AnalysisInput(
        document_id=document.id,
        path=path,
        content_hash=document.content_hash,
        source_text=text,
        source_version=1,
        source_time=FIXED_ANCHOR,
        known_time_fields=[],
        target_ref=text.from_addr,
    )


def main():
    scenario_1()
    scenario_2()
    scenario_3()
    scenario_4()
    scenario_5()
    print("=" * 78)
    print("证据导出结束（全部为确定性输出，未写入任何仓库数据）")


if __name__ == "__main__":
    main()
