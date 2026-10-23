"""MVP-6 §862：规则冲突 → 暂停自动动作 + 冲突追问 + `Case.blocked_reason`（契约 §14.5）。

冲突的定义（本模块口径）：**两条当前都生效的规则**对同一个语义槽位给出**不同效果**。
契约 §14.5 的约定是：`Question(kind="conflict")` + `Case.blocked_reason`，
且**冲突未解决前不得产出可发送结论**。这里逐条验证：

- 两个效果**都不应用**（不静默选一个）；
- 不产出新的草稿；已经存在的草稿被标 `STALE`（MVP-3 的冲突处理，不删除、不隐藏）；
- `Case.blocked_reason` 写明原因；追问里能看到是**哪两版规则**在打架；
- 外发链路无对象可发：`Approval` / `ExternalAction` 数量为 0，通道调用次数不增加
  （对照组先证明"通道计数器是活的"）；
- 冲突裁决后（本例：废止其中一条规则）恢复可发送。
"""

from __future__ import annotations

from typing import Any, Dict, List

from asistent.contracts.errors import PermissionDenied
from asistent.growth import (
    AUDIT_HIT,
    EFFECT_ALLOW_ALIAS,
    EFFECT_REQUIRE_QUESTION,
    case_is_blocked,
    confirm_rule,
    process_with_growth,
    propose_rule,
    require_sendable,
    revoke_rule,
    rule_conflict_questions,
    rule_conflict_reasons,
    rule_hits,
)
from tests.actions.support import engine_for
from tests.growth.support import GrowthCase, dump, report_line


class RuleConflictTest(GrowthCase):
    # ------------------------------------------------------------------ 夹具
    def enable_conflicting_rules(self) -> Dict[str, Any]:
        """两条都生效、对同一槽位给出不同效果的规则（contact 精确到同一发件人）。"""
        ask = confirm_rule(
            self.store,
            propose_rule(
                self.store,
                self.correction(
                    self.contact_scope(EFFECT_REQUIRE_QUESTION),
                    statement="缺「活动开始时间」必须追问，不得用「报名截止时间」顶替",
                    user_message="报名截止不是活动开始，缺活动开始要问",
                    correction_of="把报名截止当成了活动开始",
                ),
            ).id,
        )
        alias = confirm_rule(
            self.store,
            propose_rule(
                self.store,
                self.correction(
                    self.contact_scope(EFFECT_ALLOW_ALIAS),
                    statement="这家机构的通知里「报名截止时间」就是「活动开始时间」",
                    user_message="这家机构报名截止那行写的就是开始时间",
                    correction_of="上一版规则要求追问",
                ),
            ).id,
        )
        return {"ask": ask, "alias": alias}

    def rule_hits_of(self, case_id: str) -> List[Dict[str, Any]]:
        return [item.to_dict() for item in rule_hits(self.store, case_id=case_id)]

    def audit_hits(self, case_id: str) -> List[Dict[str, Any]]:
        return [
            {
                "id": event.id,
                "detail": dict(event.detail or {}),
            }
            for event in self.store.audit_trail(case_id=case_id, limit=None)
            if str(event.action) == AUDIT_HIT
        ]

    # ------------------------------------------------------------------ 主用例
    def test_conflicting_rules_pause_actions_and_block_case(self) -> None:
        # 1) 冲突当事 Case：先用"没有规则"的状态处理一封，产出可发送草稿
        conflict_report, conflict_base_growth = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "conflict-base")
        )
        report_line("冲突前（无规则）", conflict_report, conflict_base_growth)
        self.assertTrue(conflict_report.draftable)
        self.assertEqual(1, conflict_report.counts.get("Draft", 0))

        # 2) 对照 Case：证明外发通道的计数器是活的（与冲突 Case 无关）
        control_report, control_growth = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "control")
        )
        report_line("对照（无规则冲突）", control_report, control_growth)
        control_version_id = [
            item.object_id for item in control_report.outcomes if item.kind == "DraftVersion"
        ][0]
        from tests.actions.support import ActionCase

        version = self.store.get("DraftVersion", control_version_id)
        action, _summary, _approval = ActionCase.make_outbound_action(
            self, self.store, case_id=control_report.case_id, version=version
        )
        engine, sender = engine_for(self.store)
        log = getattr(sender, "log", None)
        outcome = engine.execute(action.id, worker="mvp6-test")
        channel_calls_after_control = log.in_memory_count() if log is not None else -1
        self.assertGreaterEqual(channel_calls_after_control, 1, "对照组的通道计数器应当是活的")

        # 3) 冲突：两条规则同时生效，同一个 Case 的下一份资料让它们对同一槽位打架
        rules = self.enable_conflicting_rules()
        before_actions = self.object_ids(self.store, "ExternalAction", case_id=conflict_report.case_id)
        before_approvals = self.object_ids(self.store, "Approval", case_id=conflict_report.case_id)
        before_draft_states = {
            item.id: str(item.state)
            for item in self.store.find("Draft", where={"case_id": conflict_report.case_id}, limit=None)
        }
        conflict_case_report, conflict_growth = self.run_pipeline(
            self.store,
            "03-missing-material.eml",
            document_id=self.doc_id("03", "conflict"),
            case_id=conflict_report.case_id,
        )
        report_line("冲突（两条规则同时命中）", conflict_case_report, conflict_growth)

        # 2.1 两个效果都不应用，冲突被如实记录
        self.assertEqual(1, len(conflict_growth.conflicts))
        self.assertEqual("event_start", conflict_growth.conflicts[0]["semantic"])
        self.assertEqual([], conflict_growth.applied_rule_ids)
        self.assertEqual(2, len(conflict_growth.hits))
        self.assertTrue(all(not item.applied for item in conflict_growth.hits))
        self.assertEqual(
            sorted([rules["ask"].id, rules["alias"].id]),
            sorted(item.rule_id for item in conflict_growth.hits),
        )

        # 2.2 不产出新的可发送结论；已存在的草稿转 STALE（不删除、不隐藏）
        self.assertFalse(conflict_case_report.draftable)
        self.assertEqual(0, conflict_case_report.counts.get("Draft", 0))
        draft_rows = self.store.find("Draft", where={"case_id": conflict_report.case_id}, limit=None)
        after_draft_states = {item.id: str(item.state) for item in draft_rows}
        self.assertEqual(set(before_draft_states), set(after_draft_states))
        self.assertEqual(
            {draft_id: "STALE" for draft_id in before_draft_states}, after_draft_states
        )

        # 2.3 阻塞原因 + 冲突追问（契约 §14.5）
        case = self.store.get("Case", conflict_report.case_id)
        self.assertTrue(case_is_blocked(self.store, conflict_report.case_id))
        self.assertTrue(rule_conflict_reasons(self.store, conflict_report.case_id))
        questions = rule_conflict_questions(self.store, conflict_report.case_id)
        self.assertEqual(1, len(questions))
        self.assertEqual(["时间字段:event_start"], questions[0].affects)
        all_questions = self.store.find("Question", where={"case_id": conflict_report.case_id}, limit=None)
        self.assertTrue(
            any(
                item.background.startswith(conflict_growth.conflicts[0]["detail"])
                for item in all_questions
            ),
            "冲突追问的背景里应当写明冲突原因",
        )
        with self.assertRaises(PermissionDenied):
            require_sendable(self.store, conflict_report.case_id)

        # 2.4 外发：冲突 Case 里没有任何动作对象/审批对象，通道调用次数没有增加
        after_actions = self.object_ids(self.store, "ExternalAction", case_id=conflict_report.case_id)
        after_approvals = self.object_ids(self.store, "Approval", case_id=conflict_report.case_id)
        self.assertEqual(before_actions, after_actions)
        self.assertEqual(before_approvals, after_approvals)
        self.assertEqual([], after_actions)
        self.assertEqual([], after_approvals)
        channel_calls_after_conflict = log.in_memory_count() if log is not None else -1
        self.assertEqual(channel_calls_after_control, channel_calls_after_conflict)

        # 2.5 命中仍然可证：审计里能看到是哪两版规则被卷进冲突
        audit_hits = self.audit_hits(conflict_report.case_id)
        self.assertEqual(2, len(audit_hits))
        self.assertEqual(
            {(rules["ask"].id, 1), (rules["alias"].id, 1)},
            {(item["detail"]["rule_id"], item["detail"]["rule_version"]) for item in audit_hits},
        )
        self.assertTrue(all(item["detail"]["applied"] is False for item in audit_hits))

        dump(
            "§861 规则冲突 → 暂停 + 追问 + 阻塞",
            {
                "conflict": conflict_growth.conflicts[0],
                "case_state": conflict_case_report.case_state,
                "blocked_reason": case.blocked_reason,
                "rule_conflict_reasons": rule_conflict_reasons(self.store, conflict_report.case_id),
                "question": {
                    "id": questions[0].id,
                    "affects": questions[0].affects,
                    "question": questions[0].question,
                    "background": questions[0].background,
                },
                "draft": {
                    "report_counts": dict(conflict_case_report.counts),
                    "draftable": conflict_case_report.draftable,
                    "states_before": before_draft_states,
                    "states_after": after_draft_states,
                },
                "outbound": {
                    "external_actions": after_actions,
                    "approvals": after_approvals,
                    "new_actions_from_conflict": len(after_actions) - len(before_actions),
                    "new_approvals_from_conflict": len(after_approvals) - len(before_approvals),
                    "channel_calls_after_control": channel_calls_after_control,
                    "channel_calls_after_conflict": channel_calls_after_conflict,
                    "control_outcome": str(getattr(outcome, "result", outcome)),
                },
                "audit_rule_hits": audit_hits,
                "rule_hits_readback": self.rule_hits_of(conflict_report.case_id),
            },
        )

    def test_conflict_has_no_effect_on_unrelated_slots(self) -> None:
        """冲突只冻结有分歧的槽位：本封的其它结论照旧（不是"整封不算"）。"""
        self.enable_conflicting_rules()
        report, growth = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "conflict-only")
        )
        report_line("冲突只冻结分歧槽位", report, growth)
        self.assertEqual(1, len(growth.conflicts))
        self.assertEqual(0, report.counts.get("Draft", 0))
        # "报名截止时间"（无分歧的槽位）仍然被抽成事实
        self.assertTrue(
            [item for item in report.outcomes if item.kind == "Fact" and item.detail == "registration_deadline"]
        )
        # 而"活动开始时间"没有被凭空认定出来
        self.assertEqual(
            [], [item for item in report.outcomes if item.kind == "Fact" and item.detail == "event_start"]
        )
        dump(
            "§861 冲突的作用范围",
            {
                "conflict_semantic": growth.conflicts[0]["semantic"],
                "fact_slots": sorted(
                    item.detail for item in report.outcomes if item.kind == "Fact"
                ),
                "draftable": report.draftable,
            },
        )

    def test_resolving_conflict_restores_sendable(self) -> None:
        """冲突裁决（废止其中一条）之后：新事务不再被挡，命中的是留下来的那一版。"""
        rules = self.enable_conflicting_rules()
        blocked_report, blocked_growth = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "blocked")
        )
        report_line("裁决前", blocked_report, blocked_growth)
        self.assertEqual(1, len(blocked_growth.conflicts))
        with self.assertRaises(PermissionDenied):
            require_sendable(self.store, blocked_report.case_id)

        revoke_rule(self.store, rules["alias"].id, reason="两条规则打架，用户选择保留「追问」这一条")
        after_report, after_growth = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "resolved")
        )
        report_line("裁决后", after_report, after_growth)
        self.assertEqual([], after_growth.conflicts)
        self.assertEqual([rules["ask"].id], [item.rule_id for item in after_growth.hits])
        self.assertTrue(after_growth.applied_rule_ids)
        self.assertTrue(after_report.draftable or after_report.case_state == "WAITING_FOR_USER")
        require_sendable(self.store, after_report.case_id)  # 不再被规则冲突挡住
        dump(
            "§861 裁决后恢复",
            {
                "before": {
                    "case_id": blocked_report.case_id,
                    "conflicts": blocked_growth.conflicts,
                    "blocked_reason": blocked_growth.blocked_reason,
                },
                "after": {
                    "case_id": after_report.case_id,
                    "conflicts": after_growth.conflicts,
                    "hits": [item.to_dict() for item in after_growth.hits],
                    "blocked_reason": after_growth.blocked_reason,
                    "case_state": after_report.case_state,
                    "rule_state_of_alias": str(self.store.get("Rule", rules["alias"].id).state),
                },
            },
        )
