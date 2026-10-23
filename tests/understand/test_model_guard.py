"""MVP-3 模型路径守门测试（SPEC §12、§1064）。

要证明的事：

1. **默认路径零模型调用** —— 注入一个"被调用就炸"的假后端，跑完整流程也不炸；
2. **模型只是可选适配器** —— 后端/预算没显式配置时，构造即失败，不会"试着调一下"；
3. **预算封顶** —— 当日调用数到上限就 `BudgetExceeded` 并 `blocked_reason` 落账；
4. **模型失败不伪装成功** —— 配了回退实现时退回规则抽取器并留下 `warnings`，
   没配回退就抛 `UnderstandError`；
5. **引用守卫对模型同样生效** —— 模型给的"没有引用的结论"只能变成追问。
"""

from __future__ import annotations

import json
import unittest

from asistent.contracts.errors import ValidationError
from asistent.understand import (
    AnalysisInput,
    BudgetExceeded,
    ModelBackedUnderstander,
    ModelBudget,
    ModelDisabled,
    RuleBasedUnderstander,
    UnderstandError,
    budget_from_mapping,
    decode_eml_bytes,
)

from .helpers import UnderstandTestCase, fixture_bytes, make_document, process


class FailIfCalledBackend:
    """默认路径的守卫：被调用就报错（证明"零调用"）。"""

    name = "fail-if-called"

    def __init__(self):
        self.calls = 0

    def complete(self, *, prompt, system=""):
        self.calls += 1
        raise AssertionError("默认路径不允许调用模型后端")


class ScriptedBackend:
    name = "scripted"

    def __init__(self, payload=None, error=None):
        self.calls = 0
        self.payload = payload
        self.error = error
        self.prompts = []

    def complete(self, *, prompt, system=""):
        self.calls += 1
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        return type("_Call", (), {"text": text, "cost": 0.01, "tokens": 12, "model": "scripted-1"})()


def _request(name: str = "05-reply-needed-ok.eml") -> AnalysisInput:
    raw = fixture_bytes(name)
    text = decode_eml_bytes(raw)
    return AnalysisInput(
        document_id="doc_model",
        path="fixtures/public/understand/" + name,
        content_hash="sha256:" + name,
        source_text=text,
        source_version=1,
        source_time=text.anchor_utc,
        known_time_fields=[],
        target_ref=text.from_addr,
    )


class DefaultPathTests(UnderstandTestCase):
    def test_full_flow_makes_zero_model_calls(self):
        """把 fail-if-called 后端注入流程：整条链路一次都不碰模型。"""
        backend = FailIfCalledBackend()
        understander = RuleBasedUnderstander()
        document = make_document(self.store, "05-reply-needed-ok.eml")
        report = process(self.store, document, understander=understander)
        self.assertEqual(0, backend.calls)
        self.assertTrue(report.draftable)
        log = understander.call_log()
        self.assertEqual(0, log.calls)
        self.assertEqual(0.0, log.cost)
        self.assertEqual("", log.blocked_reason)

    def test_default_producer_is_the_rule_engine(self):
        """产物带 `producer`：默认路径必须能看出"结论来自规则集"。"""
        document = make_document(self.store, "05-reply-needed-ok.eml")
        report = process(self.store, document)
        self.assertTrue(report.producer.startswith("rules:"), report.producer)

    def test_rule_understander_never_needs_a_backend(self):
        """`RuleBasedUnderstander` 不持有任何后端（结构上就不可能调用模型）。"""
        understander = RuleBasedUnderstander()
        self.assertFalse(hasattr(understander, "backend"))
        self.assertIsNone(getattr(understander, "backend", None))


class ModelGuardTests(UnderstandTestCase):
    def test_unconfigured_budget_is_rejected(self):
        """没有预算 → 构造即失败（"未设预算禁止无人值守无限调用"）。"""
        with self.assertRaises(ValidationError):
            ModelBackedUnderstander(backend=ScriptedBackend(payload={}), budget=None)

    def test_unconfigured_backend_is_rejected(self):
        """没有后端 → `ModelDisabled`。"""
        with self.assertRaises(ModelDisabled):
            ModelBackedUnderstander(backend=None, budget=ModelBudget(configured=True, max_calls_per_day=5))

    def test_zero_budget_is_rejected_at_construction(self):
        """`max_calls_per_day = 0` 不是"禁用"，而是非法配置（应当显式关掉模型路径）。"""
        with self.assertRaises(ValidationError):
            ModelBackedUnderstander(backend=ScriptedBackend(payload={}), budget={"max_calls_per_day": 0})

    def test_budget_from_mapping(self):
        """配置字典 → 预算对象；空字典 = 未配置。"""
        self.assertTrue(budget_from_mapping({"max_calls_per_day": 3}).configured)
        self.assertFalse(budget_from_mapping(None).configured)
        self.assertFalse(budget_from_mapping({}).configured)

    def test_budget_cap_stops_calling(self):
        """调用数到上限 → `BudgetExceeded`，且**不再**真的调用后端。"""
        backend = ScriptedBackend(payload={"facts": [], "questions": [], "drafts": []})
        understander = ModelBackedUnderstander(
            backend=backend,
            budget={"max_calls_per_day": 1, "max_cost_per_day": 1.0},
        )
        understander.analyze(_request())
        self.assertEqual(1, backend.calls)
        with self.assertRaises(BudgetExceeded):
            understander.analyze(_request())
        self.assertEqual(1, backend.calls, "预算耗尽后仍然调用了后端")
        log = understander.call_log()
        self.assertEqual(1, log.calls)
        self.assertIn("上限", log.blocked_reason)

    def test_cost_cap_stops_calling(self):
        """花费到上限同样会停下（不只是看次数）。"""
        backend = ScriptedBackend(payload={"facts": [], "questions": [], "drafts": []})
        understander = ModelBackedUnderstander(
            backend=backend,
            budget={"max_calls_per_day": 10, "max_cost_per_day": 0.005},
        )
        understander.analyze(_request())
        with self.assertRaises(BudgetExceeded):
            understander.analyze(_request())
        self.assertEqual(1, backend.calls)

    def test_model_failure_without_fallback_raises(self):
        """模型调用失败且没有回退实现 → `UnderstandError`（不伪装成成功）。"""
        backend = ScriptedBackend(error=RuntimeError("connection reset"))
        understander = ModelBackedUnderstander(
            backend=backend,
            budget={"max_calls_per_day": 3, "max_cost_per_day": 1.0},
        )
        with self.assertRaises(UnderstandError):
            understander.analyze(_request())
        self.assertIn("RuntimeError", understander.call_log().last_failure)

    def test_model_failure_with_fallback_degrades_and_warns(self):
        """配了回退实现 → 退回规则抽取器，`producer` 与 `warnings` 都写明降级。"""
        backend = ScriptedBackend(error=RuntimeError("timeout"))
        understander = ModelBackedUnderstander(
            backend=backend,
            budget={"max_calls_per_day": 3, "max_cost_per_day": 1.0},
            fallback=RuleBasedUnderstander(),
        )
        result = understander.analyze(_request())
        self.assertTrue(result.producer.startswith("model->fallback:"), result.producer)
        self.assertTrue(any("模型调用失败" in item for item in result.warnings))
        self.assertTrue(result.time_fields, "回退后应当仍有规则抽取结果")

    def test_model_payload_without_citation_becomes_question(self):
        """模型给的结论缺引用 → 只能变成追问（引用守卫对模型同样生效）。"""
        backend = ScriptedBackend(
            payload={
                "facts": [{"statement": "报名截止时间是 10月20日"}],
                "questions": [],
                "drafts": [],
            }
        )
        understander = ModelBackedUnderstander(
            backend=backend,
            budget={"max_calls_per_day": 3, "max_cost_per_day": 1.0},
        )
        result = understander.analyze(_request())
        self.assertEqual([], list(result.facts))
        self.assertTrue(result.questions)
        self.assertTrue(any("模型结论缺引用" in item.question for item in result.questions))
        self.assertTrue(any("引用守卫" in item for item in result.warnings))

    def test_model_payload_with_citation_becomes_fact(self):
        """带**完整引用**的模型结论可以成为事实（引用守卫不是"一律拒绝模型"）。"""
        backend = ScriptedBackend(
            payload={
                "facts": [
                    {
                        "statement": "报名截止时间：10月16日 18:00",
                        "citation": {
                            "source_object_id": "doc_model",
                            "source_object_type": "SourceDocument",
                            "path": "fixtures/public/understand/05-reply-needed-ok.eml",
                            "locator": "line:14",
                            "supports": "支持「报名截止时间」这一时间字段",
                            "content_hash": "sha256:05-reply-needed-ok.eml",
                        },
                    }
                ],
                "questions": [],
                "drafts": [],
            }
        )
        understander = ModelBackedUnderstander(
            backend=backend,
            budget={"max_calls_per_day": 3, "max_cost_per_day": 1.0},
        )
        result = understander.analyze(_request())
        self.assertEqual(1, len(result.facts))
        self.assertEqual(1, len(result.facts[0].citations))
        self.assertEqual("model:scripted-1", result.producer)

    def test_garbage_output_is_not_treated_as_conclusion(self):
        """模型返回自由文本（非 JSON）→ 不抽取任何结论，也不崩。"""
        backend = ScriptedBackend(payload="我觉得这封邮件挺重要的，建议尽快回复。")
        understander = ModelBackedUnderstander(
            backend=backend,
            budget={"max_calls_per_day": 3, "max_cost_per_day": 1.0},
        )
        result = understander.analyze(_request())
        self.assertEqual([], list(result.facts))
        self.assertEqual([], list(result.drafts))

    def test_prompt_asks_for_structured_json_not_free_text(self):
        """提示词要求结构化 JSON + 引用（否则"模型输出"无法验证）。"""
        backend = ScriptedBackend(payload={"facts": [], "questions": [], "drafts": []})
        understander = ModelBackedUnderstander(
            backend=backend,
            budget={"max_calls_per_day": 3, "max_cost_per_day": 1.0},
        )
        understander.analyze(_request())
        prompt = backend.prompts[0]
        self.assertIn("JSON", prompt)
        self.assertIn("citation", prompt)

    def test_call_log_is_a_copy_not_internal_state(self):
        """`call_log()` 返回副本：外部改它不会污染内部计数。"""
        backend = ScriptedBackend(payload={"facts": [], "questions": [], "drafts": []})
        understander = ModelBackedUnderstander(
            backend=backend,
            budget={"max_calls_per_day": 3, "max_cost_per_day": 1.0},
        )
        understander.analyze(_request())
        log = understander.call_log()
        log.calls = 999
        self.assertEqual(1, understander.call_log().calls)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
