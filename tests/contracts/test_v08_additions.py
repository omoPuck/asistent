"""契约 v0.8 增补测试（CP-20261010-24..31）。

这一轮补的都是"规模型洞"——不是实现写错，而是**契约里缺一个字段/一条转换/一道门闸**，
于是各层只能各自绕，绕法还不一致：

- CP-24：`WAITING_FOR_USER → WAITING_FOR_CONTEXT` 不在允许集合里（追问答完又暴露新缺口时无路可走）；
- CP-25：契约明文写 `Question(kind="conflict")`，但对象里没有 `kind` 字段（§14.5 实现不出来）；
- CP-26：`Memory` 没有有效期字段（只能用旁挂对象表达"这条记忆什么时候失效"）；
- CP-27：审批路径不读 `Case.blocked_reason`（跨层门闸缺口，实测能绕过成长层的阻塞）；
- CP-28：`SourceDocument.case_id` 从不回填 + 多资料事务少挂成员。
"""

from __future__ import annotations

import unittest

from asistent.contracts import (
    IllegalTransition,
    Question,
    SharedObject,
    transitions,
)
from asistent.contracts.objects import Memory
from asistent.contracts.timeutil import utcnow_iso

from .helpers import store_factory


class CaseTransitionV08Test(unittest.TestCase):
    """CP-24：追问得到回答后**又发现新的上下文缺口**，必须能回到等上下文。"""

    def test_waiting_for_user_can_go_back_to_waiting_for_context(self) -> None:
        transitions.require_transition("Case", "WAITING_FOR_USER", "WAITING_FOR_CONTEXT")
        rules = transitions.snapshot()["Case"]
        allowed = {(item["from"], item["to"]) for item in rules["transitions"]}
        self.assertIn(("WAITING_FOR_USER", "WAITING_FOR_CONTEXT"), allowed)

    def test_still_rejects_a_transition_that_makes_no_sense(self) -> None:
        """补一条转换不能顺手把状态机变宽：明显荒谬的跳跃仍然要拒。"""
        with self.assertRaises(IllegalTransition):
            transitions.require_transition("Case", "NEW", "COMPLETED")


class QuestionKindV08Test(unittest.TestCase):
    """CP-25：追问类别必须是数据（`conflict` / `missing_time` / …），不能靠读文字猜。"""

    def test_question_kind_survives_a_store_round_trip(self) -> None:
        store = store_factory()
        saved = store.put(
            Question(
                id="question:conflict:1",
                case_id="case:1",
                question="报名截止和活动开始不是一回事，以哪个为准？",
                background="规则冲突",
                kind="conflict",
                affects=["时间字段:event_start"],
            )
        )
        loaded = store.get("Question", saved.id)
        self.assertEqual("conflict", loaded.kind)
        self.assertEqual("", Question().kind, "未分类的历史对象读出来是空串，不是 None")

    def test_kind_is_part_of_the_serialized_object(self) -> None:
        payload = SharedObject.to_dict(Question(id="question:1", kind="missing_time"))
        self.assertEqual("missing_time", payload["kind"])


class MemoryValidityV08Test(unittest.TestCase):
    """CP-26：有效期是数据。"""

    def test_valid_until_survives_a_store_round_trip(self) -> None:
        store = store_factory()
        until = utcnow_iso()
        saved = store.put(
            Memory(id="memory:1", statement="这家机构报名截止=活动开始", valid_until=until)
        )
        loaded = store.get("Memory", saved.id)
        self.assertEqual(until, loaded.valid_until)

    def test_long_lived_memory_has_no_deadline(self) -> None:
        self.assertIsNone(Memory().valid_until, "None = 长期有效，不需要额外特判")
