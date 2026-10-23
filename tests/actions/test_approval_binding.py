"""审批绑定与失效：确定 DraftVersion、内容/收件人/依据变化、过期、认证渠道。

覆盖 SPEC §849-855 的两条：

- Given 草稿、收件人或关键依据变化，When 旧 Approval 被检查，Then Approval 失效且必须重新确认；
- Given 用户批准 DraftVersion-3，When DraftVersion-3 未变化且 Approval 有效，Then 只允许一个执行尝试获得执行权
  （"唯一执行权"在 `tests/actions/test_single_execution.py`，本文件负责前半句）。

另外记录契约 `changed_fields` 缺陷（CP-20261010-13）在 v0.6 修好前后的行为：
修好后传入获批摘要能说清哪几个字段变了，不传则为 `None`（显式未知），见
`ContractChangedFieldsHoleTest`。
"""

from __future__ import annotations

import dataclasses
import unittest

from asistent.actions import (
    ActionEngine,
    ApprovalCheck,
    ScriptedMailSender,
    SendScriptStep,
    assert_summary_matches_draft_version,
    bind_and_authorize,
    check_action_approval,
    create_approval,
    draft_version_binding,
    enforce_version_binding,
    revoke_approval,
    summary_from_normalized,
    sweep_expired_approvals,
    update_action_content,
)
from asistent.contracts import (
    ActionSummary,
    ApprovalInvalid,
    AttachmentRef,
    NotFound,
    PermissionDenied,
    ValidationError,
    check_approval_valid,
)
from tests.contracts.helpers import make_case

from .support import ActionCase, dump, engine_for


class ApprovalVersionBindingTest(ActionCase):
    """语义 2：审批绑定的是**确定的 DraftVersion**，不是"这个草稿"。"""

    def test_approval_records_exact_version_binding(self) -> None:
        _case, action, summary, approval, draft, version = self.fixture(self.store)
        refs = dict(approval.content_version_refs)
        print("[evidence] content_version_refs =", dump(refs))
        self.assertEqual(version.id, refs["draft_version_id"])
        self.assertEqual(draft.id, refs["draft_id"])
        self.assertEqual("1", refs["draft_version_no"])
        self.assertEqual(draft_version_binding(self.store, version.id)["draft_body_sha256"], refs["draft_body_sha256"])
        self.assertEqual(summary.digest(), approval.approved_action_digest)
        self.assertEqual([action.id], approval.target_object_ids)
        self.assertEqual("console", approval.auth_channel)
        self.assertEqual(summary.normalized(), approval.summary_shown)
        self.assertTrue(check_action_approval(self.store, action.id).ok)

    def test_new_draft_version_invalidates_old_approval(self) -> None:
        """用户又改了一版草稿 → 旧审批必须失效，动作回落 PREPARED，执行被拒。"""
        case, action, _summary, approval, draft, _version1 = self.fixture(self.store)
        _draft, version2 = self.add_draft_version(self.store, draft=draft, body="您好，我确认参加（改过）。")

        check = check_action_approval(self.store, action.id)
        self.assertFalse(check.ok)
        self.assertIn("草稿已产生新版本", check.reason_text())

        engine, sender = engine_for(self.store, steps=[SendScriptStep.succeed()])
        outcome = engine.execute(action.id)

        self.assertEqual("REJECTED", outcome.result_class)
        self.assertEqual("APPROVAL_INVALID", outcome.code)
        self.assertEqual(0, sender.call_count, "草稿变了就绝不允许按旧审批外呼")
        self.assertEqual("PREPARED", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual("INVALIDATED", str(self.store.get("Approval", approval.id).state))
        self.assertIn("草稿", str(self.store.get("Approval", approval.id).invalidated_reason))
        self.assertEqual(version2.id, str(self.store.get("Draft", draft.id).current_version_id))
        print("[evidence] 草稿换版后 =", dump({"check": check.to_dict(), "outcome": outcome.to_dict()}))

    def test_approval_requires_existing_current_draft_version(self) -> None:
        _case, _action, summary, _approval, draft, version = self.fixture(self.store)

        missing = ActionSummary(
            kind="send_email",
            from_account=summary.from_account,
            to=list(summary.to),
            subject=summary.subject,
            body=summary.body,
            draft_version_id="dv_not_exist",
        )
        with self.assertRaises(NotFound):
            create_approval(self.store, summary=missing, auth_evidence="console-session:x")

        stale_body = ActionSummary(
            kind="send_email",
            from_account=summary.from_account,
            to=list(summary.to),
            subject=summary.subject,
            body="这条正文跟那一版草稿对不上。",
            draft_version_id=version.id,
        )
        with self.assertRaises(ApprovalInvalid) as ctx:
            create_approval(self.store, summary=stale_body, auth_evidence="console-session:x")
        self.assertIn("正文", str(ctx.exception))

        _draft, version2 = self.add_draft_version(self.store, draft=draft, body="第二版")
        moved = ActionSummary(
            kind="send_email",
            from_account=summary.from_account,
            to=list(summary.to),
            subject=summary.subject,
            body=version2.body,
            draft_version_id=version.id,
        )
        with self.assertRaises(ApprovalInvalid) as ctx2:
            assert_summary_matches_draft_version(self.store, moved)
        self.assertIn("当前版本", str(ctx2.exception))

    def test_approval_cannot_be_reused_for_another_action(self) -> None:
        case, action, summary, approval, _draft, version = self.fixture(self.store)
        other = self.make_outbound_action(
            self.store, case_id=case.id, version=version, approve=False
        )[0]
        with self.assertRaises(ApprovalInvalid):
            bind_and_authorize(self.store, action_id=other.id, approval_id=approval.id)

    def test_enforce_version_binding_is_idempotent(self) -> None:
        _case, action, _summary, _approval, draft, _version = self.fixture(self.store)
        self.add_draft_version(self.store, draft=draft, body="第二版正文")
        first = enforce_version_binding(self.store, action.id)
        second = enforce_version_binding(self.store, action.id)
        self.assertTrue(first)
        self.assertEqual(first, second, "收紧检查可重复执行，结果稳定")
        self.assertEqual("PREPARED", str(self.store.get("ExternalAction", action.id).state))


class ContentChangeInvalidatesApprovalTest(ActionCase):
    """语义 2（续）：正文 / 收件人 / Bcc / 附件内容哈希变化 → 旧审批失效。"""

    def _cycle(self, *, mutate, expect_fields):
        case, action, summary, approval, _draft, version = self.fixture(self.store)
        changed = mutate(summary, version)
        stale = update_action_content(self.store, action_id=action.id, summary=changed, reason="草稿被编辑")
        ids = [item[0] for item in stale]
        self.assertEqual([approval.id], ids)
        self.assertEqual("INVALIDATED", str(self.store.get("Approval", approval.id).state))
        self.assertEqual("PREPARED", str(self.store.get("ExternalAction", action.id).state))

        check = check_action_approval(self.store, action.id)
        engine, sender = engine_for(self.store)
        outcome = engine.execute(action.id)

        self.assertFalse(check.ok)
        self.assertEqual("REJECTED", outcome.result_class)
        self.assertEqual(0, sender.call_count)
        self.assertEqual(expect_fields, check.changed_fields, "可读原因必须指出变化字段")
        return stale, check, outcome

    def _reconfirm_and_send(self, *, case, action, summary):
        """旧审批失效之后的完整闭环：重新确认 → 重新授权 → 只发一次。"""
        approval2 = create_approval(
            self.store,
            summary=summary,
            action=self.store.get("ExternalAction", action.id),
            case_id=case.id,
            auth_evidence="console-session:mvp5-reconfirm",
        )
        bind_and_authorize(self.store, action_id=action.id, approval_id=approval2.id)
        engine, sender = engine_for(self.store, steps=[SendScriptStep.succeed("after-reconfirm")])
        outcome = engine.execute(action.id)
        self.assertEqual("SUCCEEDED", outcome.result_class)
        self.assertEqual(1, sender.call_count)
        self.assertEqual("SUCCEEDED", str(self.store.get("Case", case.id).state))
        return outcome

    def test_body_change(self) -> None:
        """草稿改到第二版 → 旧审批失效、动作回落、执行被拒 → 重新确认后才能发。"""
        case, action, summary, approval, draft, version = self.fixture(self.store)
        refreshed_draft, version2 = self.add_draft_version(self.store, draft=draft, body="您好，我确认参加（改过）。")
        changed = dataclasses.replace(
            summary_from_normalized(self.store.get("ExternalAction", action.id).action_summary),
            body=version2.body,
            draft_version_id=version2.id,
        )

        stale = update_action_content(self.store, action_id=action.id, summary=changed, reason="草稿被编辑")
        check = check_action_approval(self.store, action.id)
        engine, sender = engine_for(self.store)
        outcome = engine.execute(action.id)

        self.assertEqual([approval.id], [item[0] for item in stale])
        self.assertEqual("INVALIDATED", str(self.store.get("Approval", approval.id).state))
        self.assertEqual("PREPARED", str(self.store.get("ExternalAction", action.id).state))
        self.assertEqual(version2.id, str(refreshed_draft.current_version_id))
        self.assertFalse(check.ok)
        self.assertEqual("REJECTED", outcome.result_class)
        self.assertEqual(0, sender.call_count)
        self.assertEqual(["body", "draft_version_id"], check.changed_fields)
        print("[evidence] 正文换版 =", dump({"stale": stale, "changed_fields": check.changed_fields}))
        follow_up = self._reconfirm_and_send(case=case, action=action, summary=changed)
        print("[evidence] 重新确认后可发送 =", dump(follow_up.to_dict()))

    def test_recipient_change(self) -> None:
        def mutate(summary, version):
            return dataclasses.replace(summary, to=["someone-else@example.org"])

        stale, check, _outcome = self._cycle(mutate=mutate, expect_fields=["to"])
        print("[evidence] 收件人变化 =", dump({"stale": stale, "changed_fields": check.changed_fields}))

    def test_bcc_change(self) -> None:
        def mutate(summary, version):
            return dataclasses.replace(summary, bcc=["hidden@example.org"])

        _stale, check, _outcome = self._cycle(mutate=mutate, expect_fields=["bcc"])
        print("[evidence] Bcc 变化 =", dump({"changed_fields": check.changed_fields}))

    def test_attachment_content_hash_change(self) -> None:
        def mutate(summary, version):
            return dataclasses.replace(
                summary, attachments=[AttachmentRef(name="行程单.pdf", content_hash="sha-attachment-v2")]
            )

        _stale, check, _outcome = self._cycle(mutate=mutate, expect_fields=["attachments"])
        print("[evidence] 附件哈希变化 =", dump({"changed_fields": check.changed_fields}))

    def test_evidence_version_change(self) -> None:
        """关键依据版本变化（规则升版）也要让旧审批失效。"""

        def mutate(summary, version):
            return dataclasses.replace(summary, evidence_versions={"policy": "rule-1@v2"})

        _stale, check, _outcome = self._cycle(mutate=mutate, expect_fields=["evidence_versions"])
        print("[evidence] 依据版本变化 =", dump({"changed_fields": check.changed_fields}))

    def test_whitespace_only_change_does_not_invalidate(self) -> None:
        """规范化口径：只改换行/行尾空白不算内容变化（复用冻结契约的规范化语义）。"""
        case, action, summary, approval, _draft, version = self.fixture(self.store, body="第一行\r\n第二行  ")
        same = dataclasses.replace(
            summary,
            to=[item.upper() for item in summary.to],
            subject=" %s " % summary.subject,
            body="第一行\n第二行",
        )
        self.assertEqual(summary.digest(), same.digest())
        stale = update_action_content(self.store, action_id=action.id, summary=same, reason="仅空白差异")
        self.assertEqual([], stale, "只是空白差异不得让审批失效")
        self.assertEqual("AUTHORIZED", str(self.store.get("ExternalAction", action.id).state))
        self.assertTrue(check_action_approval(self.store, action.id).ok)

    def test_content_change_after_success_is_refused(self) -> None:
        _case, action, summary, _approval, _draft, version = self.fixture(self.store)
        engine, _sender = engine_for(self.store)
        engine.execute(action.id)
        with self.assertRaises(ValidationError):
            update_action_content(self.store, action_id=action.id, summary=summary, reason="发送后再改")


class ExpiredApprovalTest(ActionCase):
    """语义 3：审批过期 → 拒绝执行（不"顺手放宽"）。"""

    def test_expired_approval_is_rejected_and_expiring_is_persisted(self) -> None:
        case, action, _summary, approval, _draft, _version = self.fixture(self.store, expires_in=-60)
        check = check_action_approval(self.store, action.id)
        self.assertFalse(check.ok)
        self.assertIn("过期", check.reason_text())

        engine, sender = engine_for(self.store, steps=[SendScriptStep.succeed()])
        outcome = engine.execute(action.id)
        self.assertEqual("REJECTED", outcome.result_class)
        self.assertEqual("APPROVAL_INVALID", outcome.code, "原子领取自己会拒绝过期审批")
        self.assertEqual(0, sender.call_count)

        expired = sweep_expired_approvals(self.store)
        self.assertEqual([approval.id], expired)
        self.assertEqual("EXPIRED", str(self.store.get("Approval", approval.id).state))
        self.assertIn("过期", str(self.store.get("Approval", approval.id).invalidated_reason))
        self.assertEqual("PREPARED", str(self.store.get("ExternalAction", action.id).state))
        print("[evidence] 过期审批 =", dump({"check": check.to_dict(), "swept": expired}))

    def test_expired_approval_still_active_is_caught_by_claim(self) -> None:
        """不跑 sweep 也不能放行：原子领取自己会拒绝过期审批。"""
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store, expires_in=-60)
        engine = ActionEngine(self.store, ScriptedMailSender())
        outcome = engine.execute(action.id)
        self.assertEqual("REJECTED", outcome.result_class)
        self.assertIn(outcome.code, ("ACTION_NOT_CLAIMABLE", "APPROVAL_INVALID"))

    def test_not_yet_expired_is_allowed(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store, expires_in=3600)
        self.assertTrue(check_action_approval(self.store, action.id).ok)
        engine, sender = engine_for(self.store)
        outcome = engine.execute(action.id)
        self.assertEqual("SUCCEEDED", outcome.result_class)
        self.assertEqual(1, sender.call_count)


class AuthenticatedChannelTest(ActionCase):
    """审批必须来自认证控制操作：聊天里的"同意/发送"不算（§21.3 第 1017 行）。"""

    def test_chat_text_is_not_an_approval(self) -> None:
        case = make_case(self.store)
        _draft, version = self.make_draft(self.store, body="正文", case_id=case.id)
        summary = ActionSummary(
            kind="send_email",
            from_account="owner@example.com",
            to=["organizer@example.org"],
            subject="Re: 活动通知",
            body=version.body,
            draft_version_id=version.id,
        )
        with self.assertRaises(PermissionDenied) as ctx:
            create_approval(
                self.store,
                summary=summary,
                case_id=case.id,
                auth_channel="chat",
                auth_evidence="用户在聊天里说了：同意，发送吧",
            )
        self.assertEqual("PERMISSION_DENIED", ctx.exception.code)
        self.assertEqual([], self.store.find("Approval", limit=10), "聊天文本不得留下审批对象")

    def test_auth_evidence_is_mandatory(self) -> None:
        _case, _action, summary, _approval, _draft, _version = self.fixture(self.store)
        with self.assertRaises(ValidationError):
            create_approval(self.store, summary=summary, auth_channel="console", auth_evidence=None)
        with self.assertRaises(ValidationError):
            create_approval(self.store, summary=summary, auth_channel="", auth_evidence="console-session:x")


class ContractChangedFieldsHoleTest(ActionCase):
    """CP-20261010-13：契约 v0.6 修好之后，"到底哪里变了"必须说得清。

    旧契约实现把摘要与自己求差 → `changed_fields` 恒为空，等于把"内容变了"报成"没有变化"。
    v0.6 起：传入获批摘要就给出真实变化字段；不传则为 `None`（显式未知），
    绝不再用空列表冒充"没有变化"。
    """

    def test_changed_fields_are_readable(self) -> None:
        _case, action, summary, approval, _draft, version = self.fixture(self.store)
        changed = dataclasses.replace(summary, bcc=["hidden@example.org"], body=summary.body + "（改了）")
        update_action_content(self.store, action_id=action.id, summary=changed, reason="草稿被编辑")

        # 1) 契约函数（v0.6）：给出获批摘要 → 说清哪几个字段变了
        try:
            check_approval_valid(
                approval_state="ACTIVE",
                approved_digest=approval.approved_action_digest,
                current_summary=changed,
                expires_at=approval.expires_at,
                approved_summary=summary,
            )
            self.fail("应当抛出 ApprovalInvalid")
        except ApprovalInvalid as exc:
            contract_detail = dict(exc.detail)
        print("[evidence] 契约(v0.6) check_approval_valid.detail =", dump(contract_detail))
        self.assertEqual(["bcc", "body"], contract_detail["changed_fields"], "必须给出真实变化字段")

        # 2) 契约函数：拿不到获批摘要 → None（未知），不再是 []（会被读成"没有变化"）
        try:
            check_approval_valid(
                approval_state="ACTIVE",
                approved_digest=approval.approved_action_digest,
                current_summary=changed,
                expires_at=approval.expires_at,
            )
            self.fail("应当抛出 ApprovalInvalid")
        except ApprovalInvalid as exc:
            without_summary = dict(exc.detail)
        print("[evidence] 契约(v0.6) 未传获批摘要时 changed_fields =", dump(without_summary["changed_fields"]))
        self.assertIsNone(without_summary["changed_fields"], "未知必须是 None，不能用 [] 冒充")

        # 3) 本模块的稳定输出：从 summary_shown 还原获批摘要，列出真实变化字段
        check = check_action_approval(self.store, action.id)
        print("[evidence] MVP-5 check_action_approval.changed_fields =", dump(check.changed_fields))
        self.assertEqual(["bcc", "body"], sorted(check.changed_fields))
        self.assertIn("bcc", check.reason_text() or "bcc,body")

    def test_readable_reasons_for_every_approval_state(self) -> None:
        _case, action, _summary, approval, _draft, _version = self.fixture(self.store)
        revoke_approval(self.store, approval_id=approval.id, reason="用户撤销")
        check = check_action_approval(self.store, action.id)
        self.assertIn("撤销", check.reason_text())
        self.assertEqual("REVOKED", check.state)

        _case2, action2, _summary2, approval2, _draft2, _version2 = self.fixture(self.store)
        self.store.invalidate_approval(approval2.id, reason="依据变化", state="INVALIDATED")
        check2 = check_action_approval(self.store, action2.id)
        self.assertIn("依据变化", check2.reason_text())

        engine, sender = engine_for(self.store)
        outcome = engine.execute(action2.id)
        self.assertEqual("REJECTED", outcome.result_class)
        self.assertEqual(0, sender.call_count)
        print("[evidence] 失效原因 =", dump({"revoked": check.to_dict(), "invalidated": check2.to_dict()}))

    def test_check_is_read_only(self) -> None:
        _case, action, _summary, _approval, _draft, _version = self.fixture(self.store)
        before = self.store.stats()
        for _ in range(3):
            check_action_approval(self.store, action.id)
        after = self.store.stats()
        self.assertEqual(before, after, "审批检查不得写入任何对象（控制台可随时调用）")
        self.assertIsInstance(check_action_approval(self.store, action.id), ApprovalCheck)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
