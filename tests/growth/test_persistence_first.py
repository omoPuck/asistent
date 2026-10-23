"""MVP-6 §862/§863：**持久化优先** —— 规则版本、命中、废止、回滚、流程绑定、记忆有效期
全部落在存储对象与审计上；换一个连接（= 进程重启）行为必须完全一致。

这一条针对"不能靠进程内的字典/缓存维持语义"：本模块用**冷启动**验证——先在实例 A 里把成长
状态建起来，丢掉实例 A，只拿同一个库路径新建实例 B，再从 B 里处理一封**新**邮件，
断言 B 命中/绑定/有效期与 A 完全一致，且历史审计一个字都没变。
"""

from __future__ import annotations

from typing import Any, Dict, List

from asistent.contracts.enums import RuleState
from asistent.growth import (
    AUDIT_HIT,
    AUDIT_REVOKE,
    EFFECT_ALLOW_ALIAS,
    EFFECT_REQUIRE_QUESTION,
    all_memories,
    case_workflow_version,
    confirm_rule,
    current_version,
    effective_rules,
    effective_workflow_version,
    enable_new_version,
    MatchInput,
    memory_valid_until,
    propose_rule,
    remember,
    revoke_rule,
    rollback_rule,
    rule_hits,
    usable_statements,
    workflow_versions,
    pin_case_workflow,
    publish_workflow_version,
    confirm_workflow,
    create_workflow,
)
from tests.growth.support import GrowthCase, dump, report_line, requires_persistence
from tests.growth.test_workflow_versions import spec_v1, spec_v2

T0 = "2026-10-05T00:00:00.000Z"


class PersistenceFirstTest(GrowthCase):
    def audit_snapshot(self, store: Any) -> Dict[str, Dict[str, Any]]:
        return {event.id: event.to_dict() for event in store.audit_trail(limit=None)}

    def hit_versions(self, store: Any, case_id: str) -> List[int]:
        return [item.rule_version for item in rule_hits(store, case_id=case_id)]

    @requires_persistence
    def test_cold_start_new_case_uses_stored_versions(self) -> None:
        # ---- 实例 A：把成长状态建起来 ----
        scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        correction = self.correction(scope)
        v1 = confirm_rule(self.store, propose_rule(self.store, correction).id)
        workflow, wf_v1 = create_workflow(self.store, spec_v1())
        confirm_workflow(self.store, workflow.id)
        warm_report, warm_growth = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "warm")
        )
        pin_case_workflow(self.store, case_id=warm_report.case_id, workflow_id=workflow.id)
        document = self.make_doc(self.store, "03-missing-material.eml", document_id=self.doc_id("03", "memory-src"))
        memory = remember(
            self.store,
            statement="家长偏好周五晚上联系",
            source_document_id=document.id,
            source_case_id=warm_report.case_id,
            ttl_seconds=3600,
            now=T0,
        )
        # 演进：规则 v2（同一条规则的新措辞）+ 流程 v2（多了缴费环节）
        v2 = enable_new_version(
            self.store,
            self.correction(
                scope,
                statement="缺「活动开始时间」必须追问，且要一次问清",
                user_message="再确认一次：缺活动开始时间还是必须问，别猜",
                correction_of="上一版措辞不够明确",
            ),
        )
        self.assertEqual(2, v2.version)
        wf_v2 = publish_workflow_version(self.store, workflow.id, spec_v2())
        audit_before = self.audit_snapshot(self.store)
        stats_before = self.store.stats()
        warm_hits = self.hit_versions(self.store, warm_report.case_id)
        self.assertEqual([1], warm_hits)

        # ---- 丢掉实例 A，只拿库路径新建实例 B（冷启动）----
        warm_store = self.store
        cold = self.restart_store()
        self.assertIsNot(warm_store, cold, "必须是真的换了连接，而不是复用同一个实例")

        # 1) 规则版本与命中：B 里读得到，且历史命中仍是 v1
        self.assertEqual(2, current_version(cold, scope).version)
        self.assertEqual("ENABLED", str(cold.get("Rule", v2.id).state))
        self.assertEqual("SUPERSEDED", str(cold.get("Rule", v1.id).state))
        self.assertEqual([1], self.hit_versions(cold, warm_report.case_id))

        # 2) 冷启动后处理**新**邮件：命中库里的 v2（没有任何进程内状态可用）
        cold_report, cold_growth = self.run_pipeline(
            cold, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "cold")
        )
        report_line("冷启动处理新邮件", cold_report, cold_growth)
        self.assertEqual([v2.id], [item.rule_id for item in cold_growth.hits])
        self.assertEqual([2], [item.rule_version for item in cold_growth.hits])
        audit_hits = [
            dict(event.detail or {})
            for event in cold.audit_trail(case_id=cold_report.case_id, limit=None)
            if str(event.action) == AUDIT_HIT
        ]
        self.assertEqual([(v2.id, 2)], [(item["rule_id"], item["rule_version"]) for item in audit_hits])

        # 3) 流程：在途事务仍钉在 v1，新事务拿 v2
        self.assertEqual(wf_v1.id, case_workflow_version(cold, warm_report.case_id).id)
        self.assertEqual(wf_v2.id, effective_workflow_version(cold, workflow.id).id)
        pin_case_workflow(cold, case_id=cold_report.case_id, workflow_id=workflow.id)
        self.assertEqual(wf_v2.id, case_workflow_version(cold, cold_report.case_id).id)

        # 4) 记忆有效期
        self.assertEqual("2026-10-05T01:00:00.000Z", memory_valid_until(cold, memory.id))
        self.assertEqual(["家长偏好周五晚上联系"], usable_statements(cold, now="2026-10-05T00:30:00.000Z"))
        self.assertEqual([memory.id], [item.id for item in all_memories(cold)])

        # 5) 历史审计没有被改写（只是在末尾追加）
        audit_after = self.audit_snapshot(cold)
        self.assertTrue(set(audit_before) <= set(audit_after))
        self.assertTrue(
            all(audit_after[event_id] == payload for event_id, payload in audit_before.items()),
            "重启后历史审计被改写了",
        )
        dump(
            "持久化优先：冷启动状态全在库里",
            {
                "backend": self.backend,
                "db_path": self.db_path(),
                "rule_versions": [
                    {"id": item.id, "version": item.version, "state": str(item.state), "statement": item.statement}
                    for item in sorted(
                        self.store.find("Rule", limit=None), key=lambda item: int(item.version)
                    )
                ],
                "warm_case_hits": warm_hits,
                "warm_case_hits_after_cold_start": self.hit_versions(cold, warm_report.case_id),
                "cold_case": {
                    "case_id": cold_report.case_id,
                    "hits": [item.to_dict() for item in cold_growth.hits],
                    "audit_hits": audit_hits,
                    "case_state": cold_report.case_state,
                },
                "workflow": {
                    "in_flight_pinned": case_workflow_version(cold, warm_report.case_id).id,
                    "effective": effective_workflow_version(cold, workflow.id).id,
                    "cold_case_pinned": case_workflow_version(cold, cold_report.case_id).id,
                    "versions": [item.version for item in workflow_versions(cold, workflow.id)],
                },
                "memory": {
                    "valid_until": memory_valid_until(cold, memory.id),
                    "usable_at_T0+30m": usable_statements(cold, now="2026-10-05T00:30:00.000Z"),
                },
                "audit": {
                    "events_before": len(audit_before),
                    "events_after": len(audit_after),
                    "historical_events_unchanged": all(
                        audit_after[event_id] == payload for event_id, payload in audit_before.items()
                    ),
                },
                "stored_object_counts": {key: value for key, value in sorted(stats_before.items()) if key != "_total"},
            },
        )

    @requires_persistence
    def test_revoke_and_rollback_survive_restart(self) -> None:
        scope = self.contact_scope(EFFECT_REQUIRE_QUESTION)
        v1 = confirm_rule(self.store, propose_rule(self.store, self.correction(scope)).id)
        v2 = enable_new_version(
            self.store,
            self.correction(
                scope, statement="缺「活动开始时间」必须追问", user_message="再确认一次：还是必须追问", correction_of="上一版"
            ),
        )
        revoke_rule(self.store, v2.id, reason="用户说这家机构的规则先不要了")
        rolled = rollback_rule(self.store, scope, to_version=1, reason="恢复第一版")
        observed = [
            (item.version, str(item.state))
            for item in sorted(self.store.find("Rule", limit=None), key=lambda i: int(i.version))
        ]

        cold = self.restart_store()
        after = [
            (item.version, str(item.state))
            for item in sorted(cold.find("Rule", limit=None), key=lambda i: int(i.version))
        ]
        self.assertEqual(observed, after)
        self.assertEqual([(1, "SUPERSEDED"), (2, "REVOKED"), (3, "ENABLED")], after)
        self.assertEqual(3, rolled.version)
        matches = effective_rules(
            cold, match=MatchInput(sender="市青少年科技中心 <notice@example.org>", subject="补充通知：研学营报名时间调整")
        )
        self.assertEqual([rolled.id], [item[0].id for item in matches])
        audit_actions = sorted({str(event.action) for event in cold.audit_trail(limit=None)})
        dump(
            "持久化优先：废止与回滚跨重启依然有效",
            {
                "backend": self.backend,
                "versions_after_restart": after,
                "effective_rule_ids": [item[0].id for item in matches],
                "rolled_back_to": rolled.links.get("rollback_of"),
                "audit_actions": audit_actions,
                "revoke_recorded": AUDIT_REVOKE in audit_actions,
                "hit_action_constant": AUDIT_HIT,
                "revoked_state_constant": str(RuleState.REVOKED),
            },
        )
