"""MVP-6 §858/§860/§861：候选 → 确认 → 生效 → 替代 → 废止 → 回滚，且历史不可改写。

对应 SPEC Given/When/Then：

- §858「用户纠正 → 规则候选（含来源、适用范围、示例）→ **确认后**才有版本生效」；
- §860「规则废止后，**新**事务不再命中，**旧**事务的引用与审计原样保留」；
- §861「回滚产生**新版本**，不删除、不改写历史记录」。
"""

from __future__ import annotations

from typing import Any, Dict, List

from asistent.contracts.errors import IllegalTransition, ValidationError
from asistent.growth import (
    EFFECT_ALLOW_ALIAS,
    EFFECT_REQUIRE_QUESTION,
    confirm_rule,
    current_version,
    enable_new_version,
    enabled_rules,
    facts_for_rule,
    get_rule,
    parse_scope,
    propose_rule,
    replace_rule,
    revoke_rule,
    rollback_rule,
    rule_hits,
    rule_versions,
    versions_by_logical_key,
)
from tests.growth.support import GrowthCase, NOTICE_SENDER, dump, report_line

#: 规则对象的"历史内容"字段（排除 state/时间戳/revision 这类会随生命周期变化的字段）
CONTENT_FIELDS = (
    "id",
    "version",
    "statement",
    "scope",
    "scope_ref",
    "priority",
    "source_case_id",
    "source_user_message",
    "examples",
    "confirmed_by",
    "effective_from",
)


def content_of(rule: Any) -> Dict[str, Any]:
    data = rule.to_dict()
    return {key: data.get(key) for key in CONTENT_FIELDS}


class RuleLifecycleTest(GrowthCase):
    # ------------------------------------------------------------------ §858 候选不生效
    def test_candidate_has_no_effect_until_confirmed(self) -> None:
        scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        candidate = propose_rule(self.store, self.correction(scope))
        self.assertEqual("CANDIDATE", str(candidate.state))
        self.assertEqual(1, candidate.version)
        self.assertIsNone(current_version(self.store, scope))
        self.assertEqual([], enabled_rules(self.store))

        report_a, growth_a = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "before-confirm")
        )
        report_line("未确认：处理 02a", report_a, growth_a)
        self.assertEqual([], growth_a.hits)
        self.assertEqual("READY_FOR_REVIEW", report_a.case_state)
        self.assertEqual([], rule_hits(self.store, case_id=report_a.case_id))

        enabled = confirm_rule(self.store, candidate.id)
        self.assertEqual("ENABLED", str(enabled.state))
        self.assertEqual(candidate.id, current_version(self.store, scope).id)

        report_b, growth_b = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "after-confirm")
        )
        report_line("确认后：处理 02a", report_b, growth_b)
        self.assertEqual(1, len(growth_b.hits))
        self.assertEqual(1, growth_b.hits[0].rule_version)
        self.assertEqual(candidate.id, growth_b.hits[0].rule_id)
        self.assertEqual("WAITING_FOR_USER", report_b.case_state)

        dump(
            "§859 候选阶段 vs 确认后",
            {
                "candidate": {"id": candidate.id, "state": str(candidate.state), "version": candidate.version},
                "before_confirm": {
                    "current_version": None,
                    "enabled_rule_count": 0,
                    "hits": len(growth_a.hits),
                    "case_state": report_a.case_state,
                    "audit_hit_count": 0,
                },
                "after_confirm": {
                    "rule_id": enabled.id,
                    "state": str(enabled.state),
                    "current_version": current_version(self.store, scope).id,
                    "hits": [item.to_dict() for item in growth_b.hits],
                    "case_state": report_b.case_state,
                    "question_affects": [
                        item.affects
                        for item in self.store.find("Question", where={"case_id": report_b.case_id}, limit=None)
                    ],
                },
            },
        )

    def test_skipping_confirmation_is_rejected_by_state_machine(self) -> None:
        """"未确认不得生效"不是我们的承诺，而是契约状态机的硬约束。"""
        scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        candidate = propose_rule(self.store, self.correction(scope))
        with self.assertRaises(IllegalTransition):
            self.store.transition(
                "Rule",
                candidate.id,
                "ENABLED",
                actor_kind="USER",
                actor_id="user",
                reason="夹具：试图跳过确认直接生效",
            )
        still = get_rule(self.store, candidate.id)
        self.assertEqual("CANDIDATE", str(still.state))
        self.assertIsNone(current_version(self.store, scope))
        dump(
            "§859 跳过确认被拒绝（契约状态机硬约束）",
            {"state_after_attempt": str(still.state), "current_version": None, "error": "ILLEGAL_TRANSITION"},
        )

    def test_propose_is_idempotent(self) -> None:
        scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        first = propose_rule(self.store, self.correction(scope))
        second = propose_rule(self.store, self.correction(scope))
        self.assertEqual(first.id, second.id)
        self.assertEqual(1, len(rule_versions(self.store, scope)))
        self.assertEqual(1, len(self.store.find("Rule", limit=None)))
        dump(
            "§859 同一次纠正重复提交",
            {"first": first.id, "second": second.id, "version_count": len(rule_versions(self.store, scope))},
        )

    def test_unknown_effect_slot_and_spec_version_are_rejected(self) -> None:
        """不认识的规则：拒绝，而不是"看不懂就忽略"。"""
        from asistent.growth import RuleScope

        rejected: List[str] = []
        cases = [
            ("未知效果", lambda: RuleScope(effect="magic.alias", slots=("registration_deadline", "event_start"))),
            (
                "未知槽位",
                lambda: RuleScope(effect=EFFECT_REQUIRE_QUESTION, slots=("registration_deadline", "not_a_slot")),
            ),
            (
                "来源=目标",
                lambda: RuleScope(effect=EFFECT_REQUIRE_QUESTION, slots=("event_start", "event_start")),
            ),
            (
                "global 带 ref",
                lambda: RuleScope(
                    effect=EFFECT_REQUIRE_QUESTION,
                    slots=("registration_deadline", "event_start"),
                    kind="global",
                    ref="notice@example.org",
                ),
            ),
            (
                "contact 缺 ref",
                lambda: RuleScope(effect=EFFECT_REQUIRE_QUESTION, slots=("registration_deadline", "event_start"), kind="contact"),
            ),
            ("说明串版本不受支持", lambda: parse_scope("v=9;kind=global;effect=slot_split.require_question;slots=registration_deadline>event_start")),
            ("说明串缺字段", lambda: parse_scope("v=1;kind=global;effect=slot_split.require_question")),
            ("说明串为空", lambda: parse_scope("")),
            ("说明串语法错误", lambda: parse_scope("v=1;kind=global;effect")),
        ]
        for name, build in cases:
            with self.assertRaises(ValidationError, msg=name):
                build()
            rejected.append(name)
        self.assertEqual(9, len(rejected))
        dump("§859 被拒绝的规则定义", {"rejected": rejected, "count": len(rejected)})

    def test_scope_matching_is_deterministic(self) -> None:
        contact = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        domain = self.contact_scope(EFFECT_REQUIRE_QUESTION, ref="@example.org")
        subject_only = self.contact_scope(EFFECT_REQUIRE_QUESTION, subject_re="研学营")
        everything = self.global_scope()
        observations = {
            "显示名发件人应命中": contact.matches(sender=NOTICE_SENDER, subject="关于举办2026年青少年科技研修营事通知"),
            "别的发件人不命中": contact.matches(sender="研学营会务组 <affairs@example.org>", subject="研学营会务安排"),
            "域名后缀命中": domain.matches(sender=NOTICE_SENDER, subject="任意"),
            "域名后缀不命中": self.contact_scope(EFFECT_REQUIRE_QUESTION, ref="@other.org").matches(sender=NOTICE_SENDER, subject="任意"),
            "主题正则命中": subject_only.matches(sender=NOTICE_SENDER, subject="研学营会务安排（报名截止以本邮件为准）"),
            "主题正则不命中": subject_only.matches(sender=NOTICE_SENDER, subject="关于科技节报名的提醒"),
            "global 无输入也命中": everything.matches(sender="", subject=""),
        }
        self.assertTrue(observations["显示名发件人应命中"])
        self.assertFalse(observations["别的发件人不命中"])
        self.assertTrue(observations["域名后缀命中"])
        self.assertFalse(observations["域名后缀不命中"])
        self.assertTrue(observations["主题正则命中"])
        self.assertFalse(observations["主题正则不命中"])
        self.assertTrue(observations["global 无输入也命中"])
        dump("§859 适用范围匹配", observations)

    # ------------------------------------------------------------------ §860 废止不清史
    def test_revoke_stops_new_cases_but_keeps_old_case_intact(self) -> None:
        scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        rule = confirm_rule(self.store, propose_rule(self.store, self.correction(scope)).id)

        # 对照：规则生效时，03 这封邮件（同一发件人、缺"活动开始时间"）会被命中
        report_control, growth_control = self.run_pipeline(
            self.store, "03-missing-material.eml", document_id=self.doc_id("03", "before-revoke")
        )
        report_line("废止前：处理 03（对照）", report_control, growth_control)
        self.assertEqual(1, len(growth_control.hits))

        # 旧事务：留下事实与审计
        report_old, growth_old = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "old-case")
        )
        old_case = report_old.case_id
        facts_before = {
            item.id: item.to_dict()
            for item in self.store.find("Fact", where={"case_id": old_case}, limit=None)
        }
        case_before = self.store.get("Case", old_case).to_dict()
        hits_before = [item.to_dict() for item in rule_hits(self.store, case_id=old_case)]
        audit_before = {event.id: event for event in self.store.audit_trail(limit=None)}
        self.assertTrue(hits_before)
        self.assertTrue(facts_before)

        revoked = revoke_rule(self.store, rule.id, reason="用户要求停用：这家机构不再这么报时间")
        self.assertEqual("REVOKED", str(revoked.state))
        self.assertIsNotNone(revoked.revoked_at)

        # 新事务：同一封邮件、同一发件人 → 不再命中
        report_new, growth_new = self.run_pipeline(
            self.store, "03-missing-material.eml", document_id=self.doc_id("03", "after-revoke")
        )
        report_line("废止后：处理 03", report_new, growth_new)
        self.assertEqual([], growth_new.hits)
        self.assertNotEqual(report_control.case_id, report_new.case_id)

        # 旧事务：引用与审计一字未改
        facts_after = {
            item.id: item.to_dict()
            for item in self.store.find("Fact", where={"case_id": old_case}, limit=None)
        }
        self.assertEqual(facts_before, facts_after)
        self.assertEqual(case_before, self.store.get("Case", old_case).to_dict())
        self.assertEqual(hits_before, [item.to_dict() for item in rule_hits(self.store, case_id=old_case)])
        audit_after = {event.id: event for event in self.store.audit_trail(limit=None)}
        self.assertTrue(set(audit_before) <= set(audit_after), "旧审计事件被删了")
        for event_id, event in audit_before.items():
            self.assertEqual(event.to_dict(), audit_after[event_id].to_dict(), "旧审计事件被改写了: %s" % event_id)
        # 被废止的规则对象仍在、内容未改：历史可查
        after = get_rule(self.store, rule.id)
        self.assertEqual(content_of(rule), content_of(after))
        dump(
            "§862 废止「命中」与「历史」对照",
            {
                "rule": rule.id,
                "before_revoke_03_hits": [item.to_dict() for item in growth_control.hits],
                "after_revoke_03_hits": [],
                "old_case": old_case,
                "old_case_fact_ids": sorted(facts_before),
                "old_case_rule_links": sorted(
                    {rid for item in facts_before.values() for rid in (item.get("links") or {}).get("rule_version", [])}
                ),
                "old_case_audit_hit_records": hits_before,
                "old_audit_event_count": len(audit_before),
                "new_audit_event_count": len(audit_after),
                "revoked_rule": {"state": str(after.state), "revoked_at": after.revoked_at},
            },
        )

    # ------------------------------------------------------------------ §861 回滚出新版
    def test_rollback_creates_new_version_and_deletes_nothing(self) -> None:
        scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        v1 = confirm_rule(
            self.store,
            propose_rule(self.store, self.correction(scope, statement="缺「活动开始时间」必须追问")).id,
        )
        v2 = enable_new_version(
            self.store,
            self.correction(
                scope,
                statement="缺「活动开始时间」必须追问，并要求问清线上/线下",
                user_message="追问时还要问清是线上还是线下",
                correction_of="v1 没要求问清线上/线下",
            ),
        )
        self.assertEqual([1, 2], [item.version for item in rule_versions(self.store, scope)])

        v1_before = get_rule(self.store, v1.id).to_dict()
        v2_content_before = content_of(get_rule(self.store, v2.id))
        rule_ids_before = sorted(item.id for item in self.store.find("Rule", limit=None))
        audit_before = {event.id: event.to_dict() for event in self.store.audit_trail(limit=None)}

        v3 = rollback_rule(self.store, scope, to_version=1, reason="v2 追问太多，回到 v1 的措辞")

        self.assertEqual(3, v3.version)
        self.assertEqual("ENABLED", str(v3.state))
        self.assertEqual("缺「活动开始时间」必须追问", v3.statement)
        self.assertEqual([v1.id], (v3.links or {}).get("rollback_of"))
        # 回滚**不复活**历史版本：v1 保持它原来的状态（早前已被 v2 取代），只是内容被 v3 复用
        self.assertEqual("SUPERSEDED", str(get_rule(self.store, v1.id).state))
        self.assertEqual(v1_before, get_rule(self.store, v1.id).to_dict(), "历史版本 v1 被改写了")
        v2_after = get_rule(self.store, v2.id)
        self.assertEqual("SUPERSEDED", str(v2_after.state))
        self.assertEqual(v2_content_before, content_of(v2_after), "v2 的内容被改写了")
        rule_ids_after = sorted(item.id for item in self.store.find("Rule", limit=None))
        self.assertTrue(set(rule_ids_before) <= set(rule_ids_after), "回滚删掉了历史版本对象")
        self.assertIn(v3.id, rule_ids_after)
        self.assertEqual(len(rule_ids_before) + 1, len(rule_ids_after))
        self.assertEqual([1, 2, 3], [item.version for item in rule_versions(self.store, scope)])
        self.assertEqual([1, 2, 3], [item.version for item in versions_by_logical_key(self.store, scope.logical_key)])
        audit_after = {event.id: event.to_dict() for event in self.store.audit_trail(limit=None)}
        self.assertTrue(set(audit_before) <= set(audit_after))
        for event_id, payload in audit_before.items():
            self.assertEqual(payload, audit_after[event_id], "旧审计事件被改写了: %s" % event_id)
        dump(
            "版本语义：回滚 = 新版本 + 历史不动",
            {
                "versions": [
                    {"version": item.version, "state": str(item.state), "statement": item.statement}
                    for item in rule_versions(self.store, scope)
                ],
                "new_version_rollback_of": (v3.links or {}).get("rollback_of"),
                "v1_unchanged": v1_before == get_rule(self.store, v1.id).to_dict(),
                "rule_objects_before": len(rule_ids_before),
                "rule_objects_after": len(rule_ids_after),
                "no_object_deleted": set(rule_ids_before) <= set(rule_ids_after),
                "audit_events_before": len(audit_before),
                "audit_events_after": len(audit_after),
                "old_audit_events_unchanged": all(
                    audit_after[event_id] == payload for event_id, payload in audit_before.items()
                ),
            },
        )

    # ------------------------------------------------------------------ 纠正 → 新规则生效
    def test_correction_replaces_rule_and_next_email_hits_new_one(self) -> None:
        old_scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        old_rule = confirm_rule(self.store, propose_rule(self.store, self.correction(old_scope)).id)
        report_before, growth_before = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "correction-before")
        )
        report_line("纠正前：处理 02a", report_before, growth_before)
        self.assertEqual([old_rule.id], [item.rule_id for item in growth_before.hits])

        new_rule = replace_rule(
            self.store,
            old_rule_id=old_rule.id,
            correction=self.correction(
                self.contact_scope(EFFECT_ALLOW_ALIAS),
                statement="这家机构的通知里「报名截止时间」就是「活动开始时间」，可直接认定",
                user_message="这家机构把开始时间写在报名截止那一行，可以直接当开始时间用",
                correction_of="上一版一律要求追问，但这家机构其实是同一个时间",
            ),
        )
        self.assertEqual("ENABLED", str(new_rule.state))
        self.assertEqual("REVOKED", str(get_rule(self.store, old_rule.id).state))
        # "旧规则废止 + 新规则（不同逻辑键）生效"：两条规则的历史各自完整
        self.assertEqual([1], [item.version for item in versions_by_logical_key(self.store, old_scope.logical_key)])
        self.assertEqual(
            [1], [item.version for item in versions_by_logical_key(self.store, parse_scope(new_rule.scope_ref).logical_key)]
        )

        report_after, growth_after = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "correction-after")
        )
        report_line("纠正后：处理 02a", report_after, growth_after)
        self.assertEqual([new_rule.id], [item.rule_id for item in growth_after.hits])
        self.assertEqual([new_rule.id], growth_after.applied_rule_ids)
        event_start_facts = [
            item.object_id
            for item in report_after.outcomes
            if item.kind == "Fact" and item.detail == "event_start"
        ]
        self.assertTrue(event_start_facts, "按规则认定后应产出「活动开始时间」事实")
        linked = facts_for_rule(self.store, new_rule.id, case_id=report_after.case_id)
        linked_ids = {item.id for item in linked}
        self.assertTrue(set(event_start_facts) <= linked_ids, "按规则产出的事实没有登记规则版本")
        linked_slots = sorted(
            {item.detail for item in report_after.outcomes if item.kind == "Fact" and item.object_id in linked_ids}
        )
        self.assertEqual(["event_start", "registration_deadline"], linked_slots)
        dump(
            "§859 纠正后下一封邮件命中新规则",
            {
                "old_rule": {"id": old_rule.id, "state": str(get_rule(self.store, old_rule.id).state)},
                "new_rule": {
                    "id": new_rule.id,
                    "version": new_rule.version,
                    "state": str(new_rule.state),
                    "statement": new_rule.statement,
                },
                "before": {
                    "hits": [item.to_dict() for item in growth_before.hits],
                    "case_state": report_before.case_state,
                },
                "after": {
                    "hits": [item.to_dict() for item in growth_after.hits],
                    "case_state": report_after.case_state,
                    "event_start_fact_ids": sorted(event_start_facts),
                    "facts_linked_to_new_rule": sorted(linked_ids),
                    "linked_slots": linked_slots,
                    "fact_statements": sorted(item.statement for item in linked),
                },
            },
        )
