"""MVP-6 §863：流程出新版本后，**在途事务继续用旧版本、新事务用新版本**（同一条测试两侧钉住）。

另外覆盖 SPEC §10.4 要求的"流程抽象必须保留八件事"，以及流程版本的回滚（= 新版本）。
"""

from __future__ import annotations

from typing import Any, Dict, List

from asistent.contracts.errors import ValidationError
from asistent.growth import (
    REQUIRED_PARTS,
    WorkflowSpec,
    case_workflow_steps,
    case_workflow_version,
    confirm_workflow,
    create_workflow,
    diff_versions,
    effective_workflow_version,
    pin_case_workflow,
    publish_workflow_version,
    repin_case_workflow,
    rollback_workflow,
    workflow_versions,
)
from tests.growth.support import GrowthCase, dump, report_line, requires_persistence


def spec_v1() -> WorkflowSpec:
    return WorkflowSpec(
        name="研学营报名确认",
        description="确认参加并提醒材料",
        trigger_conditions=["主题含「报名」", "发件人是机构通知邮箱"],
        steps=[{"action": "确认参加", "with": "学生姓名"}, {"action": "上传材料", "with": "身份证号"}],
        question_points=["学生姓名", "是否参加线下环节"],
        approval_points=["发送确认邮件"],
        outputs={"draft_kind": "email_reply", "archive": "确认记录"},
        archive_requirements=["原邮件归档", "确认记录归档"],
        failure_handling=["失败转人工", "3 次后告警"],
        scope="global",
        inputs={"required": ["学生姓名"]},
    )


def spec_v2() -> WorkflowSpec:
    data = spec_v1().to_dict()
    data["description"] = "确认参加、提醒材料并缴纳费用"
    data["steps"] = [
        {"action": "确认参加", "with": "学生姓名"},
        {"action": "上传材料", "with": "身份证号"},
        {"action": "缴纳费用", "with": "订单号"},
    ]
    data["approval_points"] = ["发送确认邮件", "发送缴费提醒"]
    return WorkflowSpec(**data)


class WorkflowVersionTest(GrowthCase):
    def content_of(self, version: Any) -> Dict[str, Any]:
        data = version.to_dict()
        for key in ("revision", "created_at", "updated_at", "state", "links", "schema_version", "object_type"):
            data.pop(key, None)
        return data

    # ------------------------------------------------------------------ §863 双侧钉住
    def test_in_flight_keeps_old_version_while_new_case_uses_new_version(self) -> None:
        workflow, v1 = create_workflow(self.store, spec_v1())
        confirm_workflow(self.store, workflow.id)
        self.assertEqual(1, v1.version)
        self.assertEqual("ENABLED", str(self.store.get("WorkflowVersion", v1.id).state))

        # 在途事务：处理时钉住 v1
        in_flight_report, in_flight_growth = self.run_pipeline(
            self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "in-flight")
        )
        pinned = pin_case_workflow(self.store, case_id=in_flight_report.case_id, workflow_id=workflow.id)
        self.assertEqual(v1.id, pinned.id)
        steps_before = case_workflow_steps(self.store, in_flight_report.case_id)
        self.assertEqual(["确认参加", "上传材料"], [item["action"] for item in steps_before])

        # 流程演进 → v2
        v2 = publish_workflow_version(self.store, workflow.id, spec_v2())
        self.assertEqual(2, v2.version)
        self.assertEqual("SUPERSEDED", str(self.store.get("WorkflowVersion", v1.id).state))
        self.assertEqual(v2.id, effective_workflow_version(self.store, workflow.id).id)

        # 新事务：处理另一封 → 拿到 v2
        new_report, new_growth = self.run_pipeline(
            self.store, "05-reply-needed-ok.eml", document_id=self.doc_id("05", "new-case")
        )
        pin_case_workflow(self.store, case_id=new_report.case_id, workflow_id=workflow.id)

        # ——— 两侧同时断言 ———
        self.assertEqual(v1.id, case_workflow_version(self.store, in_flight_report.case_id).id)
        self.assertEqual(v2.id, case_workflow_version(self.store, new_report.case_id).id)
        steps_after = case_workflow_steps(self.store, in_flight_report.case_id)
        self.assertEqual(steps_before, steps_after)
        self.assertEqual(["确认参加", "上传材料"], [item["action"] for item in steps_after])
        self.assertEqual(
            ["确认参加", "上传材料", "缴纳费用"],
            [item["action"] for item in case_workflow_steps(self.store, new_report.case_id)],
        )
        # 重复钉版本不会把在途事务换到新版本
        again = pin_case_workflow(self.store, case_id=in_flight_report.case_id, workflow_id=workflow.id)
        self.assertEqual(v1.id, again.id)
        dump(
            "§863 在途用旧版本 / 新事务用新版本",
            {
                "workflow_id": workflow.id,
                "versions": [
                    {"version": item.version, "state": str(item.state), "steps": len(item.steps)}
                    for item in workflow_versions(self.store, workflow.id)
                ],
                "in_flight_case": {
                    "case_id": in_flight_report.case_id,
                    "pinned_version_id": case_workflow_version(self.store, in_flight_report.case_id).id,
                    "pinned_version_no": case_workflow_version(self.store, in_flight_report.case_id).version,
                    "steps": [item["action"] for item in case_workflow_steps(self.store, in_flight_report.case_id)],
                },
                "new_case": {
                    "case_id": new_report.case_id,
                    "pinned_version_id": case_workflow_version(self.store, new_report.case_id).id,
                    "pinned_version_no": case_workflow_version(self.store, new_report.case_id).version,
                    "steps": [item["action"] for item in case_workflow_steps(self.store, new_report.case_id)],
                },
                "effective_version": effective_workflow_version(self.store, workflow.id).version,
                "case_links": {
                    "in_flight": (self.store.get("Case", in_flight_report.case_id).links or {}),
                    "new": (self.store.get("Case", new_report.case_id).links or {}),
                },
            },
        )

    def test_explicit_repin_is_the_only_way_to_switch_in_flight_version(self) -> None:
        workflow, v1 = create_workflow(self.store, spec_v1())
        confirm_workflow(self.store, workflow.id)
        report, _ = self.run_pipeline(self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "repin"))
        pin_case_workflow(self.store, case_id=report.case_id, workflow_id=workflow.id)
        v2 = publish_workflow_version(self.store, workflow.id, spec_v2())
        with self.assertRaises(ValidationError):
            repin_case_workflow(self.store, case_id=report.case_id, workflow_version_id=v2.id, reason="")
        switched = repin_case_workflow(
            self.store,
            case_id=report.case_id,
            workflow_version_id=v2.id,
            reason="用户要求这份事务改用新流程",
        )
        self.assertEqual(v2.id, switched.id)
        audit_actions: List[str] = [
            str(event.action) for event in self.store.audit_trail(case_id=report.case_id, limit=None)
        ]
        dump(
            "§863 改绑定必须显式且留痕",
            {
                "pinned_before": v1.id,
                "pinned_after": case_workflow_version(self.store, report.case_id).id,
                "audit_actions": sorted(set(audit_actions)),
                "workflow_version_states": {
                    item.version: str(item.state) for item in workflow_versions(self.store, workflow.id)
                },
            },
        )

    # ------------------------------------------------------------------ §10.4 八件事
    def test_workflow_abstraction_requires_all_parts(self) -> None:
        base = spec_v1().to_dict()
        rejected: List[str] = []
        for name in REQUIRED_PARTS:
            broken = dict(base)
            broken[name] = "" if isinstance(base[name], str) else [] if isinstance(base[name], list) else {}
            with self.assertRaises(ValidationError, msg=name):
                WorkflowSpec(**broken).validate()
            rejected.append(name)
        with self.assertRaises(ValidationError):
            WorkflowSpec(**{**base, "steps": [{"with": "缺少 action"}]}).validate()
        self.assertEqual(len(REQUIRED_PARTS), len(rejected))

        workflow, v1 = create_workflow(self.store, spec_v1())
        stored = self.store.get("WorkflowVersion", v1.id)
        dump(
            "§10.4 流程抽象八件事",
            {
                "rejected_when_missing": rejected,
                "stored_version": {
                    "version": stored.version,
                    "trigger_conditions": list(stored.trigger_conditions),
                    "steps": list(stored.steps),
                    "question_points": list(stored.question_points),
                    "approval_points": list(stored.approval_points),
                    "outputs": dict(stored.outputs),
                    "archive_requirements": list(stored.archive_requirements),
                    "failure_handling": list(stored.failure_handling),
                    "scope": stored.scope,
                },
                "workflow_state": str(self.store.get("Workflow", workflow.id).state),
                "current_version_id": self.store.get("Workflow", workflow.id).current_version_id,
            },
        )

    # ------------------------------------------------------------------ 回滚 = 新版本
    def test_workflow_rollback_creates_new_version(self) -> None:
        workflow, v1 = create_workflow(self.store, spec_v1())
        confirm_workflow(self.store, workflow.id)
        v2 = publish_workflow_version(self.store, workflow.id, spec_v2())
        v1_before = self.content_of(self.store.get("WorkflowVersion", v1.id))
        v2_before = self.content_of(self.store.get("WorkflowVersion", v2.id))

        v3 = rollback_workflow(self.store, workflow.id, to_version=1, reason="缴费环节先不上")
        self.assertEqual(3, v3.version)
        self.assertEqual("ENABLED", str(v3.state))
        self.assertEqual([v1.id], (v3.links or {}).get("rollback_of"))
        self.assertEqual(["确认参加", "上传材料"], [item["action"] for item in v3.steps])
        self.assertEqual(v1_before, self.content_of(self.store.get("WorkflowVersion", v1.id)))
        self.assertEqual(v2_before, self.content_of(self.store.get("WorkflowVersion", v2.id)))
        self.assertEqual([1, 2, 3], [item.version for item in workflow_versions(self.store, workflow.id)])
        self.assertEqual(
            ["SUPERSEDED", "SUPERSEDED", "ENABLED"],
            [str(item.state) for item in workflow_versions(self.store, workflow.id)],
        )
        diff = diff_versions(self.store.get("WorkflowVersion", v2.id), self.store.get("WorkflowVersion", v3.id))
        self.assertIn("steps", diff["changed"])
        dump(
            "§10.5 流程回滚 = 新版本",
            {
                "versions": [
                    {"version": item.version, "state": str(item.state), "steps": [s["action"] for s in item.steps]}
                    for item in workflow_versions(self.store, workflow.id)
                ],
                "new_version_rollback_of": (v3.links or {}).get("rollback_of"),
                "v1_content_unchanged": v1_before == self.content_of(self.store.get("WorkflowVersion", v1.id)),
                "v2_content_unchanged": v2_before == self.content_of(self.store.get("WorkflowVersion", v2.id)),
                "diff_v2_v3_changed_keys": sorted(diff["changed"]),
            },
        )

    @requires_persistence
    def test_pin_and_versions_survive_restart(self) -> None:
        workflow, v1 = create_workflow(self.store, spec_v1())
        confirm_workflow(self.store, workflow.id)
        report, _ = self.run_pipeline(self.store, "02a-notice-conflict.eml", document_id=self.doc_id("02a", "warm"))
        pin_case_workflow(self.store, case_id=report.case_id, workflow_id=workflow.id)
        restarted = self.restart_store()
        self.assertEqual(v1.id, case_workflow_version(restarted, report.case_id).id)
        self.assertEqual(["确认参加", "上传材料"], [item["action"] for item in case_workflow_steps(restarted, report.case_id)])
        dump(
            "§863 绑定与版本跨重启",
            {
                "backend": self.backend,
                "pinned_version_id": case_workflow_version(restarted, report.case_id).id,
                "steps": [item["action"] for item in case_workflow_steps(restarted, report.case_id)],
                "versions": [item.version for item in workflow_versions(restarted, workflow.id)],
            },
        )
