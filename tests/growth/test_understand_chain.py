"""MVP-6 §858：与 `asistent.understand` **真实串联**（不是单测里造的假链路）。

本模块专门证明两件事：

1. 成长模块没有替掉理解链路：`GrowthUnderstander` 只是把**真正的** `asistent.understand`
   理解器包一层（`base` 是真实对象，模块名可打印），并且在没有规则时，两边的结论一致
   （同一封邮件的语义槽位/事实陈述逐条相同）；
2. 完整链路可跑通：**纠正 → 规则候选（带来源：哪次纠正、哪封原始邮件、谁确认、什么范围）
   → 确认生效 → 下一封邮件命中新版本**，审计里能看到每次命中的 `rule_version`。
"""

from __future__ import annotations

from typing import Any, Dict, List

from asistent.growth import (
    AUDIT_CONFIRM,
    AUDIT_HIT,
    AUDIT_PROPOSE,
    EFFECT_ALLOW_ALIAS,
    EFFECT_REQUIRE_QUESTION,
    FACT_RULE_LINK,
    GrowthUnderstander,
    confirm_rule,
    facts_for_rule,
    process_with_growth,
    propose_rule,
    rule_hits,
)
from asistent.understand import RuleBasedUnderstander
from tests.understand.helpers import fixture_bytes, make_document, process, raw_extract
from tests.growth.support import GrowthCase, dump, report_line


class UnderstandChainTest(GrowthCase):
    def fact_statements(self, store: Any, case_id: str) -> List[Dict[str, str]]:
        return sorted(
            (
                {"slot": str(fact.subject), "statement": fact.statement}
                for fact in store.find("Fact", where={"case_id": case_id}, limit=None)
            ),
            key=lambda item: (item["slot"], item["statement"]),
        )

    def hit_details(self, store: Any, case_id: str) -> List[Dict[str, Any]]:
        return [
            dict(event.detail or {})
            for event in store.audit_trail(case_id=case_id, limit=None)
            if str(event.action) == AUDIT_HIT
        ]

    # ------------------------------------------------------------------ 链路是真的
    def test_growth_wraps_the_real_understand_pipeline(self) -> None:
        base = RuleBasedUnderstander()
        self.assertTrue(
            type(base).__module__.startswith("asistent.understand"),
            "base 必须是 asistent.understand 里的真实理解器，而不是测试桩",
        )
        understander = GrowthUnderstander(self.store, base=base)
        self.assertIs(base, understander.base)

        # 一条规则都没有时：两边的抽取结论必须一致（成长层不"替"理解层做判断）
        plain_store = self.make_store(name=self.db_path("plain.sqlite3"))
        plain_doc = make_document(plain_store, "05-reply-needed-ok.eml", document_id=self.doc_id("05", "plain"))
        plain_report = process(plain_store, plain_doc)
        growth_doc = make_document(plain_store, "05-reply-needed-ok.eml", document_id=self.doc_id("05", "wrapped"))
        growth_report, growth_growth = process_with_growth(
            plain_store, growth_doc, eml_bytes=fixture_bytes("05-reply-needed-ok.eml"), base=base
        )
        report_line("真实理解链路（无规则）", growth_report, growth_growth)
        self.assertEqual([], growth_growth.hits)
        self.assertEqual(
            sorted(
                (item.detail, item.outcome)
                for item in plain_report.outcomes
                if item.kind == "Fact"
            ),
            sorted(
                (item.detail, item.outcome)
                for item in growth_report.outcomes
                if item.kind == "Fact"
            ),
        )
        extraction = raw_extract(
            "05-reply-needed-ok.eml",
            source_object_id=growth_doc.id,
            path=self.path("05-reply-needed-ok.eml"),
        )
        dump(
            "§859 真实理解链路（base 是 asistent.understand）",
            {
                "base_module": type(base).__module__,
                "base_class": type(base).__name__,
                "growth_extends": [item.__name__ for item in type(understander).__mro__[:3]],
                "plain_facts": [
                    {"slot": item.detail, "outcome": item.outcome}
                    for item in plain_report.outcomes
                    if item.kind == "Fact"
                ],
                "wrapped_facts": [
                    {"slot": item.detail, "outcome": item.outcome}
                    for item in growth_report.outcomes
                    if item.kind == "Fact"
                ],
                "extractor_slots": [field.semantic for field in extraction.time_fields],
                "plain_case_state": plain_report.case_state,
                "wrapped_case_state": growth_report.case_state,
            },
        )

    # ------------------------------------------------------------------ 完整链路
    def test_correction_becomes_rule_then_next_email_hits_new_version(self) -> None:
        base = RuleBasedUnderstander()
        scope_v1 = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        correction = self.correction(scope_v1)
        candidate = propose_rule(self.store, correction)
        self.assertEqual("CANDIDATE", str(candidate.state))

        # 1) 未确认：什么都不改（链路照跑，只是没有命中）
        before_report, before_growth = process_with_growth(
            self.store,
            make_document(
                self.store, "03-missing-material.eml", document_id=self.doc_id("03", "before-confirm")
            ),
            eml_bytes=fixture_bytes("03-missing-material.eml"),
            base=base,
        )
        report_line("确认前", before_report, before_growth)
        self.assertEqual([], before_growth.hits)

        # 2) 确认 → 生效
        enabled = confirm_rule(self.store, candidate.id)
        self.assertEqual("ENABLED", str(enabled.state))
        self.assertEqual("user", enabled.confirmed_by)
        audit_events = [
            {
                "action": str(event.action),
                "object_id": event.object_id,
                "detail": dict(event.detail or {}),
            }
            for event in self.store.audit_trail(object_id=enabled.id, limit=None)
        ]
        actions = [item["action"] for item in audit_events]
        self.assertIn(AUDIT_PROPOSE, actions)
        self.assertIn(AUDIT_CONFIRM, actions)
        propose_detail = [item for item in audit_events if item["action"] == AUDIT_PROPOSE][0]["detail"]

        # 3) 下一封同来源邮件：命中 v1（审计里能看到版本号；事实登记了规则版本）
        report_v1, growth_v1 = process_with_growth(
            self.store,
            make_document(self.store, "03-missing-material.eml", document_id=self.doc_id("03", "hit-v1")),
            eml_bytes=fixture_bytes("03-missing-material.eml"),
            base=base,
        )
        report_line("命中 v1", report_v1, growth_v1)
        self.assertEqual([1], [item.rule_version for item in growth_v1.hits])
        self.assertEqual([1], [item["rule_version"] for item in self.hit_details(self.store, report_v1.case_id)])
        questions = self.store.find("Question", where={"case_id": report_v1.case_id}, limit=None)
        rule_question = [item for item in questions if enabled.id in str(item.question)]
        self.assertTrue(rule_question, "规则要求的追问必须落在事务上")

        # 4) 新的纠正（同一条规则的新措辞）→ v2，下一封邮件命中 v2
        correction_v2 = self.correction(
            scope_v1,
            statement="缺「活动开始时间」必须追问，并把「报名截止」和「活动开始」分开写清",
            user_message="还是必须问，但问题里要把两个时间分开说",
            correction_of="上一版追问没说清两个时间的区别",
        )
        v2 = confirm_rule(self.store, propose_rule(self.store, correction_v2).id)
        self.assertEqual(2, v2.version)
        report_v2, growth_v2 = process_with_growth(
            self.store,
            make_document(self.store, "03-missing-material.eml", document_id=self.doc_id("03", "hit-v2")),
            eml_bytes=fixture_bytes("03-missing-material.eml"),
            base=base,
        )
        report_line("命中 v2", report_v2, growth_v2)
        self.assertEqual([v2.id], [item.rule_id for item in growth_v2.hits])
        self.assertEqual([2], [item["rule_version"] for item in self.hit_details(self.store, report_v2.case_id)])
        self.assertEqual([1], [item["rule_version"] for item in self.hit_details(self.store, report_v1.case_id)])

        # 5) v2 生效后 v1 转历史：旧事务的审计仍是 v1，库里的"当前版本"是 v2
        self.assertEqual("SUPERSEDED", str(self.store.get("Rule", enabled.id).state))
        self.assertEqual([2], [item["rule_version"] for item in self.hit_details(self.store, report_v2.case_id)])
        dump(
            "§859 纠正 → 规则版本 → 下一次真实命中",
            {
                "correction": {
                    "user_message": correction.user_message,
                    "correction_of": correction.correction_of,
                    "source_document_id": correction.source_document_id,
                    "source_case_id": correction.source_case_id,
                    "scope": scope_v1.spec,
                },
                "candidate": {
                    "id": candidate.id,
                    "state_at_propose": str(candidate.state),
                    "state_at_dump_time": str(self.store.get("Rule", candidate.id).state),
                    "candidate_had_no_effect": True,
                    "propose_audit_detail": propose_detail,
                },
                "confirmed_v1": {
                    "id": enabled.id,
                    "state": str(self.store.get("Rule", enabled.id).state),
                    "confirmed_by": enabled.confirmed_by,
                    "confirmed_at": enabled.confirmed_at,
                    "effective_from": enabled.effective_from,
                    "audit_actions": actions,
                },
                "before_confirm": {
                    "case_id": before_report.case_id,
                    "hits": [item.to_dict() for item in before_growth.hits],
                    "case_state": before_report.case_state,
                },
                "hit_v1": {
                    "case_id": report_v1.case_id,
                    "hits": [item.to_dict() for item in growth_v1.hits],
                    "audit_hits": self.hit_details(self.store, report_v1.case_id),
                    "questions": sorted(item.question for item in rule_question),
                    "case_state": report_v1.case_state,
                },
                "hit_v2": {
                    "rule_id": v2.id,
                    "version": v2.version,
                    "statement": v2.statement,
                    "case_id": report_v2.case_id,
                    "hits": [item.to_dict() for item in growth_v2.hits],
                    "audit_hits": self.hit_details(self.store, report_v2.case_id),
                    "case_state": report_v2.case_state,
                },
                "old_case_keeps_v1": self.hit_details(self.store, report_v1.case_id),
            },
        )

    # ------------------------------------------------------------------ 按规则认定（另一类效果）
    def test_alias_rule_recognises_slot_with_citation_and_version(self) -> None:
        """`allow_alias`：按规则把来源槽位认定为目标槽位——必须带引用、且登记规则版本。"""
        alias_scope = self.contact_scope(EFFECT_ALLOW_ALIAS)
        alias = confirm_rule(
            self.store,
            propose_rule(
                self.store,
                self.correction(
                    alias_scope,
                    statement="这家机构通知里「报名截止时间」就是「活动开始时间」",
                    user_message="这家机构报名截止那行写的就是活动开始时间",
                    correction_of="这条规则要求把报名截止当活动开始",
                ),
            ).id,
        )
        report, growth = self.run_pipeline(
            self.store,
            "02a-notice-conflict.eml",
            document_id=self.doc_id("02a", "hit-alias"),
            base=RuleBasedUnderstander(),
        )
        report_line("按规则认定", report, growth)
        self.assertEqual([alias.id], [item.rule_id for item in growth.hits])
        self.assertEqual([alias.id], growth.applied_rule_ids)
        self.assertEqual([1], [item["rule_version"] for item in self.hit_details(self.store, report.case_id)])
        linked = facts_for_rule(self.store, alias.id, case_id=report.case_id)
        self.assertTrue(linked)
        self.assertTrue(all(alias.id in (fact.links or {}).get(FACT_RULE_LINK, []) for fact in linked))
        self.assertTrue(all(fact.citations for fact in linked))
        event_start_facts = [fact for fact in linked if fact.subject.endswith("event_start")]
        self.assertTrue(event_start_facts, "按规则应当认定出「活动开始时间」")
        dump(
            "§860 按规则认定槽位（allow_alias）",
            {
                "rule": {
                    "rule_id": alias.id,
                    "state": str(self.store.get("Rule", alias.id).state),
                    "scope": alias_scope.spec,
                    "statement": alias.statement,
                },
                "hits": [item.to_dict() for item in growth.hits],
                "audit_hits": self.hit_details(self.store, report.case_id),
                "case_state": report.case_state,
                "draftable": report.draftable,
                "linked_facts": [
                    {
                        "id": fact.id,
                        "subject": fact.subject,
                        "statement": fact.statement,
                        "citations": fact.citations,
                        "rule_versions": (fact.links or {}).get(FACT_RULE_LINK, []),
                    }
                    for fact in linked
                ],
                "rule_hits_readback": [item.to_dict() for item in rule_hits(self.store, case_id=report.case_id)],
            },
        )
