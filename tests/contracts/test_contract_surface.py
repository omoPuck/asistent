"""契约面测试：对象/状态/版本的完备性与自洽（SPEC §4、§5、§15.1、§21.1）。"""

from __future__ import annotations

import json
import unittest
from dataclasses import fields

from asistent.contracts import (
    CONTRACT_VERSION,
    Case,
    IllegalTransition,
    MACHINES,
    SourceDocument,
    Task,
    contract_snapshot,
    machine_for,
    objects,
    require_transition,
    transitions,
)


class ContractSurfaceTest(unittest.TestCase):
    def test_every_object_type_has_id_prefix(self) -> None:
        """每个共享对象类型都必须登记 ID 前缀：不允许 MVP 私下造对象。"""
        from asistent.contracts import ID_PREFIXES

        missing = [
            name for name in objects.snapshot() if name not in ID_PREFIXES and name != "SharedObject"
        ]
        self.assertEqual([], missing, f"未登记 ID 前缀的对象类型: {missing}")

    def test_stateful_objects_have_state_machine(self) -> None:
        """带 state 字段的对象必须有登记的状态机（否则状态迁移无法校验）。"""
        for name, meta in objects.snapshot().items():
            if "state" in meta["fields"]:
                self.assertIn(name, MACHINES, f"{name} 有 state 字段但没有登记状态机")

    def test_revision_and_timestamps_are_universal_fields(self) -> None:
        common = {"id", "schema_version", "revision", "created_at", "updated_at", "links"}
        for name, meta in objects.snapshot().items():
            missing = common.difference(meta["fields"])
            self.assertEqual(set(), missing, f"{name} 缺少通用字段: {sorted(missing)}")

    def test_object_round_trip(self) -> None:
        doc = SourceDocument(
            id="doc_1",
            source_type="email",
            identity_key="mail:abc",
            content_hash="deadbeef",
            origin_path="收件箱/通知.eml",
        )
        payload = doc.to_dict()
        self.assertEqual("SourceDocument", payload["object_type"])
        json.dumps(payload, ensure_ascii=False)  # 必须可 JSON 序列化（事件/归档依赖）
        restored = SourceDocument.from_dict(payload)
        self.assertEqual(doc.id, restored.id)
        self.assertEqual("PENDING", str(restored.index_state))

    def test_snapshot_is_stable_and_serializable(self) -> None:
        snapshot = contract_snapshot()
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        self.assertEqual(CONTRACT_VERSION, snapshot["contract_version"])
        self.assertIn("Case", snapshot["objects"])
        self.assertIn("CaseState", snapshot["states"])
        self.assertIn("Case", snapshot["machines"])
        self.assertIn("ExternalAction", snapshot["machines"])

    def test_illegal_transition_rejected_with_reason(self) -> None:
        rule = require_transition("Case", "NEW", "TRIAGED", trigger="AGENT")
        self.assertEqual("TRIAGED", rule.target)
        with self.assertRaises(IllegalTransition) as ctx:
            require_transition("Case", "NEW", "EXECUTING", trigger="AGENT")
        detail = ctx.exception.to_dict()
        self.assertEqual("ILLEGAL_TRANSITION", detail["code"])
        self.assertEqual("NEW", detail["detail"]["from"])
        self.assertIn("TRIAGED", detail["detail"]["allowed"])

    def test_unknown_machine_is_rejected(self) -> None:
        with self.assertRaises(IllegalTransition):
            machine_for("NotRegisteredObject")

    def test_unknown_and_in_flight_are_protected(self) -> None:
        """UNKNOWN 只能由人工/系统核实推进，模型不能自己"洗白"。"""
        machine = machine_for("ExternalAction")
        self.assertFalse(machine.rule("UNKNOWN", "AUTHORIZED"))
        self.assertFalse(machine.rule("UNKNOWN", "PREPARED"))
        self.assertFalse(machine.rule("UNKNOWN", "IN_FLIGHT"))
        rule = machine.rule("UNKNOWN", "SUCCEEDED")
        self.assertIsNotNone(rule)
        self.assertEqual(("USER", "SYSTEM"), tuple(rule.triggers))

    def test_case_state_has_no_implicit_path(self) -> None:
        """Case 不是严格单一路径：追问回答/草稿编辑可以回到分析或复核。"""
        machine = machine_for("Case")
        self.assertIn("ANALYZING", machine.next_states("READY_FOR_REVIEW"))
        self.assertIn("ARCHIVING", machine.next_states("SUCCEEDED"))
        self.assertIn("ARCHIVE_PENDING", machine.next_states("ARCHIVING"))
        # COMPLETED 不是死胡同：新内容需要新一轮归档时回到待归档
        self.assertEqual(["ARCHIVE_PENDING"], machine.next_states("COMPLETED"))
        self.assertTrue(machine.is_terminal("CANCELLED") is False)

    def test_state_tables_expose_triggers_and_notes(self) -> None:
        rows = transitions.snapshot()
        for name, table in rows.items():
            self.assertIn("transitions", table)
            for row in table["transitions"]:
                self.assertTrue(row["triggers"], f"{name} 的转换缺少触发者: {row}")
                self.assertIn("requires_no_irreversible", row)

    def test_case_and_task_defaults(self) -> None:
        case = Case(goal="处理通知")
        self.assertEqual("NEW", str(case.state))
        self.assertEqual("PENDING", str(case.archive_state))
        task = Task(case_id="case_1", kind="analyze")
        self.assertEqual("PENDING", str(task.state))
        self.assertGreater(task.retry_policy["max_attempts"], 0)
        self.assertIn("state", [f.name for f in fields(task)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
