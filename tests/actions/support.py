"""tests/actions 公共夹具（纯标准库）。

后端切换沿用契约测试的开关：`ASISTENT_STORE=reference|sqlite`。
夹具刻意复用 `tests/contracts/helpers.py` 的 `store_factory` 与 `make_case`：
MVP-5 不应另立一套"看起来一样"的存储与事务夹具。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple

from asistent.actions import (
    ActionEngine,
    MailCallLog,
    ScriptedMailSender,
    SendScriptStep,
    bind_and_authorize,
    create_approval,
)
from asistent.contracts import (
    ActionSummary,
    AttachmentRef,
    Draft,
    DraftVersion,
    ExternalAction,
    action_idempotency_key,
    utcnow_iso,
)
from asistent.contracts.ids import sha256_bytes
from tests.contracts.helpers import make_case, store_factory

#: worktree 根：子进程用它当 cwd，`import asistent` 才可用
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHILD_DIR = os.path.join(REPO_ROOT, "tests", "actions")

#: 默认通道名（真实通道不在本轮范围：全部是假通道）
CONNECTOR = "mail:recording"


def backend_kind() -> str:
    kind = os.environ.get("ASISTENT_STORE", "reference").strip().lower()
    return "sqlite" if kind == "sqlite" else "reference"


def run_child(script: str, *args: str, timeout: float = 240.0) -> Tuple[int, str]:
    """在独立进程里跑 tests/actions 下的脚本（cwd = worktree 根）。"""
    command = [sys.executable, os.path.join(CHILD_DIR, script)] + [str(item) for item in args]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return int(completed.returncode), _decode_child_output(completed.stdout)


def _decode_child_output(raw: bytes) -> str:
    """子进程输出按 UTF-8 严格解码。

    `tests/__init__.py` 已经把 `PYTHONUTF8`/`PYTHONIOENCODING` 设成默认值，子进程继承后**保证**说 UTF-8。
    这里刻意**不用** `errors="replace"`：以前它把"父子编码不一致"悄悄变成一堆 `U+FFFD`，
    证据看起来"有内容"，实际已经失真，最后只在别处以一个莫名其妙的编码错误爆出来。
    编码不一致应当**当场**报出可读的错误，而不是生产一份看不出来的坏证据。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AssertionError(
            "子进程输出不是 UTF-8，父子进程 stdio 编码不一致（这会让证据失真）：%s\n"
            "前 200 字节原始内容：%r" % (exc, raw[:200])
        ) from exc


def start_child(script: str, *args: str) -> "subprocess.Popen":
    """并发拉起子进程（跨进程抢动作时用）。"""
    command = [sys.executable, os.path.join(CHILD_DIR, script)] + [str(item) for item in args]
    return subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


class ActionCase(unittest.TestCase):
    """每个测试一个临时目录 + 一个按 `ASISTENT_STORE` 选择的存储实现。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="asistent-mvp5-")
        self.backend = backend_kind()
        self._db_seq = 0
        # 先注册 rmtree，后注册 store.close：addCleanup 是 LIFO → 先关连接再删目录
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.store = self.make_store()

    # ------------------------------------------------------------------ 基础设施
    def path(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)

    def make_store(self, *, name: Optional[str] = None) -> Any:
        """新开一个存储实例；sqlite 模式返回同一库文件的新连接（= 模拟进程重启）。"""
        if self.backend == "sqlite":
            from asistent.storage.sqlite_store import SqliteStore  # 局部导入：reference 后端不需要它

            self._db_seq += 1
            db_path = name or os.path.join(self.tmpdir, "asistent.sqlite3")
            store = SqliteStore(path=db_path)
        else:
            store = store_factory()
        closer = getattr(store, "close", None)
        if callable(closer):
            self.addCleanup(closer)
        return store

    def restart_store(self) -> Any:
        """关掉当前实例并重开（sqlite = 真正的重启；reference 只能拿到空库）。"""
        closer = getattr(self.store, "close", None)
        if callable(closer):
            closer()
        self.store = self.make_store()
        return self.store

    def db_path(self, name: str = "asistent.sqlite3") -> str:
        return os.path.join(self.tmpdir, name)

    # ------------------------------------------------------------------ 夹具
    def make_draft(self, store: Any, *, body: str, case_id: str = "") -> Tuple[Draft, DraftVersion]:
        draft = store.put(Draft(case_id=case_id, target_kind="email_reply", target_ref="thread-1"))
        return self.add_draft_version(store, draft=draft, body=body)

    def add_draft_version(self, store: Any, *, draft: Draft, body: str) -> Tuple[Draft, DraftVersion]:
        """追加一个新版本并让草稿指向它（模拟"用户又改了一版"）。"""
        current = store.get("Draft", draft.id)
        version = store.put(
            DraftVersion(
                draft_id=current.id,
                version_no=len(current.version_ids) + 1,
                body=body,
                content_hash=sha256_bytes(body.encode("utf-8")),
                change_reason="测试夹具",
            )
        )
        current.version_ids.append(version.id)
        current.current_version_id = version.id
        store.put(current, expected_revision=current.revision, reason="草稿指向新版本")
        return store.get("Draft", current.id), version

    def advance_case_to_review(self, store: Any, case_id: str) -> Any:
        """把事务推到 `READY_FOR_REVIEW`（真实流程里"分析完成、等待批准"的位置）。"""
        store.transition(
            "Case", case_id, "TRIAGED", actor_kind="AGENT", actor_id="agent", reason="夹具：分诊"
        )
        return store.transition(
            "Case", case_id, "READY_FOR_REVIEW", actor_kind="AGENT", actor_id="agent", reason="夹具：进入复核"
        )

    def make_outbound_action(
        self,
        store: Any,
        *,
        case_id: str,
        version: DraftVersion,
        body: Optional[str] = None,
        to: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        subject: str = "Re: 活动通知",
        attachment: Optional[AttachmentRef] = None,
        evidence_versions: Optional[Dict[str, str]] = None,
        kind: str = "send_email",
        connector: str = CONNECTOR,
        approve: bool = True,
        expires_in: int = 24 * 3600,
        auth_channel: str = "console",
        auth_evidence: Optional[str] = "console-session:mvp5-test",
        verify_draft_version: bool = True,
    ) -> Tuple[ExternalAction, ActionSummary, Optional[Any]]:
        """构造"已准备（+ 可选已批准已授权）"的外部动作。"""
        summary = ActionSummary(
            kind=kind,
            from_account="owner@example.com",
            to=to if to is not None else ["organizer@example.org"],
            bcc=bcc or [],
            subject=subject,
            body=body if body is not None else version.body,
            draft_version_id=version.id,
            target_thread_ref="thread-1",
            evidence_versions=evidence_versions if evidence_versions is not None else {"policy": "rule-1@v1"},
            attachments=[attachment] if attachment else [],
        )
        summary.validate()
        action = store.put(
            ExternalAction(
                case_id=case_id,
                kind=kind,
                idempotency_key="pending",
                action_summary=summary.normalized(),
                summary_digest=summary.digest(),
                target_ref=summary.to[0] if summary.to else "",
                connector=connector,
                prepared_at=utcnow_iso(),
            )
        )
        action.idempotency_key = action_idempotency_key(action.id)
        store.put(action, expected_revision=action.revision, reason="分配稳定幂等键")

        case = store.get("Case", case_id)
        if action.id not in case.action_ids:
            case.action_ids.append(action.id)
            store.put(case, expected_revision=case.revision, reason="关联动作")

        approval = None
        if approve:
            approval = create_approval(
                store,
                summary=summary,
                action=action,
                case_id=case_id,
                auth_channel=auth_channel,
                auth_evidence=auth_evidence,
                ttl_seconds=expires_in,
                verify_draft_version=verify_draft_version,
            )
            bind_and_authorize(store, action_id=action.id, approval_id=approval.id)
            approval = store.get("Approval", approval.id)
            # 真实流程：批准后事务进入 APPROVED，领取时才进 EXECUTING（SPEC §5.2）
            case_now = store.get("Case", case_id)
            if str(case_now.state) == "READY_FOR_REVIEW":
                store.transition(
                    "Case", case_id, "APPROVED", actor_kind="USER", actor_id="owner", reason="夹具：批准"
                )
        return store.get("ExternalAction", action.id), summary, approval

    def fixture(
        self,
        store: Any,
        *,
        body: str = "您好，我确认参加。",
        goal: str = "处理一封需要回复的通知邮件",
        **kwargs: Any,
    ) -> Tuple[Any, ExternalAction, ActionSummary, Optional[Any], Draft, DraftVersion]:
        """一个"事务 + 草稿版本 + 已授权动作"的完整夹具。"""
        case = make_case(store, goal=goal)
        self.advance_case_to_review(store, case.id)
        draft, version = self.make_draft(store, body=body, case_id=case.id)
        action, summary, approval = self.make_outbound_action(
            store, case_id=case.id, version=version, **kwargs
        )
        return case, action, summary, approval, draft, version


def engine_for(store: Any, *, steps: Optional[List[SendScriptStep]] = None, log_path: Optional[str] = None, **kwargs: Any):
    """装配"引擎 + 假通道"，返回 `(engine, sender)`。"""
    log = MailCallLog(log_path) if log_path else MailCallLog()
    sender = ScriptedMailSender(log=log, script=list(steps or []), **kwargs)
    return ActionEngine(store, sender, node="executor-test"), sender


def dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)


__all__ = [
    "ActionCase",
    "REPO_ROOT",
    "CONNECTOR",
    "backend_kind",
    "run_child",
    "start_child",
    "engine_for",
    "dump",
]
