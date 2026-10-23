"""MVP-6 §859：命中必须可证 —— 审计里能看到命中的**规则版本号**，事实能反查它来自哪一版。

这一条针对 SPEC「不接受 Agent 自称遵守了规则」：每一次命中都要落
`AuditEvent(action="RULE_HIT")`（带 `rule_id` 与 `rule_version`），并且被规则影响的事实
要在 `Fact.links["rule_version"]` 上登记版本 ID；事务用 `Case.rule_ids` 记录依据过哪些版本。
"""

from __future__ import annotations

from typing import Any, Dict, List

from asistent.growth import (
    AUDIT_HIT,
    EFFECT_ALLOW_ALIAS,
    EFFECT_REQUIRE_QUESTION,
    FACT_RULE_LINK,
    confirm_rule,
    facts_for_rule,
    propose_rule,
    replace_rule,
    rule_hits,
)
from tests.growth.support import GrowthCase, dump, report_line, requires_persistence


class RuleHitAuditTest(GrowthCase):
    def hit_events(self, store: Any, case_id: str) -> List[Dict[str, Any]]:
        return [
            {
                "id": event.id,
                "action": str(event.action),
                "object_id": event.object_id,
                "case_id": event.case_id,
                "detail": dict(event.detail or {}),
            }
            for event in store.audit_trail(case_id=case_id, limit=None)
            if str(event.action) == AUDIT_HIT
        ]

    # ------------------------------------------------------------------ 审计带版本号
    def test_audit_records_rule_version_for_every_hit(self) -> None:
        scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        rule = confirm_rule(self.store, propose_rule(self.store, self.correction(scope)).id)
        report, growth = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "hit")
        )
        report_line("命中一次", report, growth)
        self.assertEqual(1, len(growth.hits))

        events = self.hit_events(self.store, report.case_id)
        self.assertEqual(1, len(events))
        detail = events[0]["detail"]
        self.assertEqual(rule.id, detail["rule_id"])
        self.assertEqual(1, detail["rule_version"])
        self.assertEqual(EFFECT_REQUIRE_QUESTION, detail["effect"])
        self.assertEqual(["registration_deadline", "event_start"], detail["slots"])
        self.assertTrue(detail["applied"])
        self.assertEqual(report.document_id, events[0]["object_id"])

        # 事务侧与事实侧都能反查规则版本
        case = self.store.get("Case", report.case_id)
        self.assertIn(rule.id, case.rule_ids)
        linked = facts_for_rule(self.store, rule.id, case_id=report.case_id)
        self.assertTrue(linked, "被规则影响的事实没有登记规则版本")
        for fact in linked:
            self.assertIn(rule.id, (fact.links or {}).get(FACT_RULE_LINK, []))
            self.assertTrue(fact.citations, "事实必须带引用")
        dump(
            "§860 审计里的命中记录",
            {
                "audit_events": events,
                "case_rule_ids": list(case.rule_ids),
                "linked_facts": [
                    {
                        "id": fact.id,
                        "statement": fact.statement,
                        "rule_versions": (fact.links or {}).get(FACT_RULE_LINK, []),
                        "citation_locator": (fact.citations[0] or {}).get("locator"),
                    }
                    for fact in linked
                ],
                "rule_hits_readback": [item.to_dict() for item in rule_hits(self.store, case_id=report.case_id)],
            },
        )

    def test_not_applicable_rule_is_not_recorded_as_hit(self) -> None:
        scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        confirm_rule(self.store, propose_rule(self.store, self.correction(scope)).id)
        # 01 这封邮件同时有"报名截止"和"活动开始" → 规则不适用 → 既不改结果，也不该记命中
        report, growth = self.run_pipeline(
            self.store, "01-notice-multi-time.eml", document_id=self.doc_id("01", "not-applicable")
        )
        report_line("不适用", report, growth)
        self.assertEqual([], growth.hits)
        self.assertEqual([], self.hit_events(self.store, report.case_id))
        self.assertEqual("READY_FOR_REVIEW", report.case_state)
        case = self.store.get("Case", report.case_id)
        self.assertEqual([], list(case.rule_ids))
        dump(
            "§860 不适用就不算命中",
            {"hits": [], "case_state": report.case_state, "case_rule_ids": [], "draftable": report.draftable},
        )

    def test_each_case_keeps_the_version_it_hit(self) -> None:
        """旧事务指向旧版本、新事务指向新版本：两侧引用同时常驻，互不覆盖。"""
        old_scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        old_rule = confirm_rule(self.store, propose_rule(self.store, self.correction(old_scope)).id)
        old_report, old_growth = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "v1-case")
        )
        new_rule = replace_rule(
            self.store,
            old_rule_id=old_rule.id,
            correction=self.correction(
                self.contact_scope(EFFECT_ALLOW_ALIAS),
                statement="这家机构的通知里「报名截止时间」就是「活动开始时间」",
                user_message="这家机构报名截止那行写的就是开始时间",
                correction_of="一律追问太啰嗦",
            ),
        )
        new_report, new_growth = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "v2-case")
        )
        old_case = self.store.get("Case", old_report.case_id)
        new_case = self.store.get("Case", new_report.case_id)
        self.assertEqual([old_rule.id], list(old_case.rule_ids))
        self.assertEqual([new_rule.id], list(new_case.rule_ids))
        self.assertEqual([old_rule.id], [item.rule_id for item in old_growth.hits])
        self.assertEqual([new_rule.id], [item.rule_id for item in new_growth.hits])
        # 旧事务的审计里仍然是 v1，新事务里是新规则
        self.assertEqual(1, self.hit_events(self.store, old_case.id)[0]["detail"]["rule_version"])
        self.assertEqual(old_rule.id, self.hit_events(self.store, old_case.id)[0]["detail"]["rule_id"])
        self.assertEqual(new_rule.id, self.hit_events(self.store, new_case.id)[0]["detail"]["rule_id"])
        dump(
            "§860 两个事务各自钉住自己命中的版本",
            {
                "old_case": {"id": old_case.id, "rule_ids": list(old_case.rule_ids)},
                "new_case": {"id": new_case.id, "rule_ids": list(new_case.rule_ids)},
                "old_rule": {"id": old_rule.id, "version": old_rule.version, "state": str(self.store.get("Rule", old_rule.id).state)},
                "new_rule": {"id": new_rule.id, "version": new_rule.version, "state": str(new_rule.state)},
                "old_audit_hits": self.hit_events(self.store, old_case.id),
                "new_audit_hits": self.hit_events(self.store, new_case.id),
            },
        )

    def test_repeat_processing_keeps_objects_but_appends_audit(self) -> None:
        """重复投递同一封：对象不重复建（幂等），审计可以追加（如实记录发生过两次）。"""
        scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        confirm_rule(self.store, propose_rule(self.store, self.correction(scope)).id)
        document_id = self.doc_id("02a", "repeat")
        first, _ = self.run_pipeline(self.store, "02a-notice-conflict.eml", document_id=document_id)
        counts_first = dict(first.counts)
        facts_first = self.object_ids(self.store, "Fact", case_id=first.case_id)
        second, _ = self.run_pipeline(self.store, "02a-notice-conflict.eml", document_id=document_id)
        counts_second = dict(second.counts)
        facts_second = self.object_ids(self.store, "Fact", case_id=first.case_id)
        self.assertEqual(first.case_id, second.case_id)
        self.assertTrue(second.duplicate_run)
        self.assertTrue(all(value == 0 for value in counts_second.values()), counts_second)
        self.assertEqual(facts_first, facts_second)
        self.assertEqual(2, len(self.hit_events(self.store, first.case_id)))
        dump(
            "§860 重复处理同一封邮件",
            {
                "case_id": first.case_id,
                "first_counts": counts_first,
                "second_counts": counts_second,
                "duplicate_run": second.duplicate_run,
                "fact_ids_stable": facts_first == facts_second,
                "audit_hit_events": len(self.hit_events(self.store, first.case_id)),
            },
        )

    # ------------------------------------------------------------------ 重启后仍在
    @requires_persistence
    def test_hit_records_survive_restart(self) -> None:
        """命中记录不是进程内字典：换一个连接（= 重启）照样读得到。"""
        scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        rule = confirm_rule(self.store, propose_rule(self.store, self.correction(scope)).id)
        report, _ = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "before-restart")
        )
        before = [item.to_dict() for item in rule_hits(self.store, case_id=report.case_id)]
        facts_before = self.object_ids(self.store, "Fact", case_id=report.case_id)

        restarted = self.restart_store()
        after = [item.to_dict() for item in rule_hits(restarted, case_id=report.case_id)]
        self.assertEqual(before, after)
        self.assertEqual(facts_before, self.object_ids(restarted, "Fact", case_id=report.case_id))
        case = restarted.get("Case", report.case_id)
        self.assertIn(rule.id, case.rule_ids)
        linked = facts_for_rule(restarted, rule.id, case_id=report.case_id)
        self.assertTrue(linked)
        dump(
            "§860 重启后命中记录仍可查",
            {
                "backend": self.backend,
                "hits_before": before,
                "hits_after": after,
                "case_rule_ids": list(case.rule_ids),
                "linked_fact_ids": sorted(item.id for item in linked),
            },
        )
