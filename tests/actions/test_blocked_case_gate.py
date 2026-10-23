"""跨层门闸：**阻塞中的事务不允许被批准**（契约 v0.8，CP-20261010-27）。

这条测试的价值在于它是**真实串联**，不是构造出来的状态：用成长层的完整链路制造一次真实的
规则冲突（两条都生效的规则对同一时间槽位给出相反效果），确认 Case 被阻塞、草稿转 STALE 之后，
再走 MVP-5 的审批入口——**必须被拒**。

为什么必须在这一层拦：成长层已经保证"冲突不产出新的可发送结论"，但审批是"人已经同意发出去"
的唯一入口；只要还有一种调用顺序能走到审批，"不产出可发送结论"就只是约定而不是不变量。
实测过的绕过路径就是"先制造冲突、再拿旧草稿直调审批"。

对照组同样重要：**不是**把审批一律禁掉——冲突消解（规则被废止）之后，同一套调用必须恢复正常。
"""

from __future__ import annotations

import unittest
from typing import Any, Dict

from asistent.actions import approval as approval_mod
from asistent.actions.engine import approve_and_authorize
from asistent.contracts.errors import PermissionDenied
from asistent.growth import (
    EFFECT_ALLOW_ALIAS,
    EFFECT_REQUIRE_QUESTION,
    case_is_blocked,
    confirm_rule,
    propose_rule,
    revoke_rule,
)

from tests.actions.support import ActionCase
from tests.growth.support import GrowthCase, report_line


class BlockedCaseCannotBeApprovedTest(GrowthCase):
    """真实链路：冲突 → 阻塞 → 审批被拒；冲突消解 → 审批恢复。"""

    def enable_conflicting_rules(self) -> Dict[str, Any]:
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

    def test_approval_is_refused_while_the_case_is_blocked(self) -> None:
        # 1) 无规则时先跑出一份可发送的草稿（这就是"旧草稿"）
        base_report, base_growth = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "gate-base")
        )
        report_line("建立基线（无规则，可发送）", base_report, base_growth)
        self.assertTrue(base_report.draftable)
        version_id = [
            item.object_id for item in base_report.outcomes if item.kind == "DraftVersion"
        ][0]
        version = self.store.get("DraftVersion", version_id)
        # 建动作但**先不批准**（冲突后要走的正是"拿这份动作去批准"这条路）
        action, summary, _ = ActionCase.make_outbound_action(
            self, self.store, case_id=base_report.case_id, version=version, approve=False
        )

        # 2) 让两条规则打架 → Case 阻塞
        rules = self.enable_conflicting_rules()
        _conflict_report, conflict_growth = self.run_pipeline(
            self.store,
            "03-missing-material.eml",
            document_id=self.doc_id("03", "gate-conflict"),
            case_id=base_report.case_id,
        )
        self.assertEqual(1, len(conflict_growth.conflicts))
        self.assertTrue(case_is_blocked(self.store, base_report.case_id))
        case = self.store.get("Case", base_report.case_id)
        self.assertTrue(str(case.blocked_reason or "").strip())

        # 3) 拿旧草稿直调审批 → 必须被拒（这就是以前能绕过去的那条路）
        before = len(self.store.find("Approval", limit=None))
        with self.assertRaises(PermissionDenied) as caught:
            approval_mod.create_approval(
                self.store,
                summary=summary,
                action=action,
                case_id=base_report.case_id,
                auth_channel="console",
                auth_evidence="gate-test",
            )
        detail = dict(getattr(caught.exception, "detail", {}) or {})
        self.assertEqual(base_report.case_id, detail.get("case_id"))
        self.assertIn("blocked_reason", detail)
        self.assertEqual(before, len(self.store.find("Approval", limit=None)), "被拒的路径不得留下审批对象")

        # 3.1 便捷组合入口（控制台"批准并发"走的就是它）同样被拒
        with self.assertRaises(PermissionDenied):
            approve_and_authorize(
                self.store,
                summary=summary,
                action=action,
                auth_channel="console",
                auth_evidence="gate-test",
            )

        # 4) 冲突消解（废止其中一条规则）→ 重新理解同一事务，阻塞**必须被清除**
        #    这条同时钉住 CP-29：以前 blocked_reason 只写在"等待原因变了"这个不相关条件里，
        #    裁决后若等待原因恰好没变，事务会**永久卡在阻塞态**——叠加审批门闸就是死锁。
        revoke_rule(self.store, rules["alias"].id, reason="两条规则打架，先回到保守规则（缺开始时间就追问）")
        after_report, after_growth = self.run_pipeline(
            self.store,
            "02a-notice-conflict.eml",
            document_id=self.doc_id("02a", "gate-resolved"),
            case_id=base_report.case_id,
        )
        report_line("裁决后重新理解同一事务", after_report, after_growth)
        self.assertEqual([], after_growth.conflicts)
        self.assertFalse(
            case_is_blocked(self.store, base_report.case_id),
            "冲突裁决后 Case 仍被规则冲突挡住",
        )
        refreshed = self.store.get("Case", base_report.case_id)
        self.assertFalse(
            str(refreshed.blocked_reason or "").strip(),
            f"冲突裁决后 blocked_reason 没有被清除（会死锁）：{refreshed.blocked_reason!r}",
        )

        # 4.1 门闸随之放行：同一套"检查 + 审批"调用不再因阻塞被拒
        approval_mod.require_case_not_blocked(self.store, case_id=base_report.case_id)
        self.assertEqual(before, len(self.store.find("Approval", limit=None)), "此前被拒的路径留下的审批数应仍为 0")


if __name__ == "__main__":
    unittest.main()
