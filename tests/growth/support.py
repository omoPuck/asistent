"""tests/growth 公共夹具（纯标准库）。

后端开关沿用契约测试的 `ASISTENT_STORE=reference|sqlite`；夹具刻意复用
`tests/contracts/helpers.py::store_factory` 与 `tests/understand/helpers.py`
的 `make_document` / `fixture_bytes`：MVP-6 不另立一套"看起来一样"的存储与邮件夹具。

证据约定（与 MVP-5 一致）：测试把关键观察用 `[evidence] <标题>` + JSON 打到 stdout，
文档只引用**真实跑出来的**那些行，不手写"应该是"的数字。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from asistent.growth import (  # noqa: E402
    EFFECT_ALLOW_ALIAS,
    EFFECT_REQUIRE_QUESTION,
    Correction,
    RuleScope,
    process_with_growth,
)
from asistent.contracts import SourceDocument  # noqa: E402
from tests.contracts.helpers import store_factory  # noqa: E402
from tests.understand.helpers import fixture_bytes, fixture_path, make_document  # noqa: E402

#: worktree 根
REPO_ROOT = _REPO_ROOT

#: 邮件夹具目录
FIXTURE_ROOT = os.path.join(REPO_ROOT, "fixtures", "public", "understand")

#: 03/04 等夹具用的发件人（带显示名，考验"从头部里抽地址"的匹配）
NOTICE_SENDER = "市青少年科技中心 <notice@example.org>"

#: 跳过理由（与契约/MVP-5 保持同一口径）
SKIP_PERSISTENCE = "跨进程共享状态需要持久化后端"
SKIP_RESTART = "重启语义需要持久化后端"


def backend_kind() -> str:
    kind = os.environ.get("ASISTENT_STORE", "reference").strip().lower()
    return "sqlite" if kind == "sqlite" else "reference"


def requires_persistence(testcase):
    """需要"新连接能读到旧数据"的用例：reference 后端只能跳过（并说明原因）。"""
    return unittest.skipUnless(backend_kind() == "sqlite", SKIP_RESTART)(testcase)


def dump(title: str, payload: Any) -> None:
    """打印一条可引用的原始证据。"""
    print("[evidence] %s" % title)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def report_line(title: str, report: Any, growth: Any) -> None:
    """一行摘要（不进证据 JSON，方便人工扫）。"""
    print("[evidence] %s | %s | %s" % (title, report.summary_line(), growth.summary_line()))


class GrowthCase(unittest.TestCase):
    """每个测试一个临时目录 + 一个按 `ASISTENT_STORE` 选择的存储实现。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="asistent-mvp6-")
        self.backend = backend_kind()
        # 先注册 rmtree，后注册 close：addCleanup 是 LIFO → 先关连接再删目录
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.store = self.make_store()

    # ------------------------------------------------------------------ 基础设施
    def path(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)

    def db_path(self, name: str = "asistent.sqlite3") -> str:
        return os.path.join(self.tmpdir, name)

    def make_store(self, *, name: Optional[str] = None) -> Any:
        """新开一个存储实例；sqlite 模式是同一库文件的新连接（= 模拟进程重启）。"""
        if self.backend == "sqlite":
            from asistent.storage.sqlite_store import SqliteStore  # 局部导入：reference 后端不需要它

            store = SqliteStore(path=name or self.db_path())
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

    # ------------------------------------------------------------------ 夹具
    def contact_scope(self, effect: str, *, ref: str = "notice@example.org", subject_re: str = "") -> RuleScope:
        """针对"报名截止 / 活动开始"两个语义槽位的规则范围（contact 精确到发件人）。"""
        return RuleScope(
            effect=effect,
            slots=("registration_deadline", "event_start"),
            kind="contact",
            ref=ref,
            subject_re=subject_re,
        )

    def global_scope(self, effect: str = EFFECT_REQUIRE_QUESTION, *, subject_re: str = "") -> RuleScope:
        return RuleScope(
            effect=effect,
            slots=("registration_deadline", "event_start"),
            kind="global",
            subject_re=subject_re,
        )

    def correction(
        self,
        scope: RuleScope,
        *,
        statement: str = "报名截止时间不是活动开始时间，缺活动开始时间必须追问",
        user_message: str = "报名截止时间不是活动开始时间，别把 10 月 20 日当成开营时间",
        correction_of: str = "把报名截止时间当成了活动开始时间",
        document_id: str = "doc_02a",
        case_id: str = "",
    ) -> Correction:
        return Correction(
            statement=statement,
            user_message=user_message,
            correction_of=correction_of,
            scope=scope,
            source_document_id=document_id,
            source_case_id=case_id,
        )

    def make_doc(self, store: Any, name: str, *, document_id: Optional[str] = None, case_id: Optional[str] = None):
        """登记一封夹具邮件；同 ID 重复登记时返回既有对象（模拟"重复投递同一封"）。"""
        if document_id:
            from asistent.contracts.errors import NotFound

            try:
                existing = store.get("SourceDocument", document_id)
            except NotFound:
                existing = None
            if isinstance(existing, SourceDocument):
                return existing
        return make_document(store, name, document_id=document_id, case_id=case_id)

    def run_pipeline(
        self,
        store: Any,
        name: str,
        *,
        document_id: Optional[str] = None,
        case_id: Optional[str] = None,
        **kwargs: Any
    ):
        """真实串联：MVP-3 落对象 + MVP-6 记命中（`process_with_growth`）。

        `case_id` 用来把这份资料挂到已有事务上（跨资料冲突检测的场景）。
        """
        doc = self.make_doc(store, name, document_id=document_id, case_id=case_id)
        return process_with_growth(store, doc, eml_bytes=fixture_bytes(name), **kwargs)

    def doc_id(self, name: str, suffix: str) -> str:
        return "doc_%s_%s" % (name.replace(".", "-"), suffix)

    # ------------------------------------------------------------------ 断言辅助
    def object_ids(self, store: Any, object_type: str, *, case_id: Optional[str] = None):
        where = {"case_id": case_id} if case_id else None
        return sorted(item.id for item in store.find(object_type, where=where, limit=None))


__all__ = [
    "FIXTURE_ROOT",
    "GrowthCase",
    "NOTICE_SENDER",
    "REPO_ROOT",
    "SKIP_PERSISTENCE",
    "SKIP_RESTART",
    "backend_kind",
    "dump",
    "fixture_bytes",
    "fixture_path",
    "report_line",
    "requires_persistence",
    "store_factory",
]
