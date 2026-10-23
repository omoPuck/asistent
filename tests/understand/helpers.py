"""MVP-3 理解层测试的公共夹具。

约定：

* 默认只用 `ReferenceStore`（内存、确定、不写仓库数据）。设 `ASISTENT_STORE=sqlite`
  时会切到 SQLite 实现，用来证明这一层没有依赖某个特定存储的私有行为；
* fixture 一律是 `fixtures/public/understand/*.eml`（合成内容，无真实个人信息）；
* 断言的是**可观察产物**（时间字段的语义、Question/Draft 对象、store 统计），
  不是内部函数返回值。
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from asistent.contracts import (  # noqa: E402
    Case,
    Citation,
    Draft,
    DraftVersion,
    Fact,
    Inference,
    Question,
    SourceDocument,
    Task,
    utcnow_iso,
)

FIXTURE_ROOT = os.path.join(_REPO_ROOT, "fixtures", "public", "understand")


def store_factory(**kwargs: Any):
    """按 `ASISTENT_STORE` 选择被测存储实现（默认 `reference`）。"""
    kind = os.environ.get("ASISTENT_STORE", "reference").strip().lower()
    if kind in ("", "reference", "memory", "mem"):
        from asistent.contracts import ReferenceStore

        return ReferenceStore(**kwargs)
    if kind == "sqlite":
        from asistent.storage.sqlite_store import SqliteStore  # type: ignore

        return SqliteStore(**kwargs)
    raise RuntimeError("未知存储实现: %s" % kind)


def fixture_path(name: str) -> str:
    return os.path.join(FIXTURE_ROOT, name)


def fixture_bytes(name: str) -> bytes:
    with io.open(fixture_path(name), "rb") as handle:
        return handle.read()


def make_document(
    store,
    name: str,
    *,
    document_id: Optional[str] = None,
    case_id: Optional[str] = None,
    received_at: str = "",
) -> SourceDocument:
    """把一封 fixture 邮件登记成 `SourceDocument`（模拟"已同步入库"）。"""
    from asistent.understand import decode_eml_bytes

    raw = fixture_bytes(name)
    text = decode_eml_bytes(raw)
    return store.put(
        SourceDocument(
            id=document_id or ("doc_" + name.replace(".", "-")),
            source_type="email",
            source_system="fixture:understand",
            source_id=name,
            identity_key=document_id or ("doc_" + name),
            origin_path=fixture_path(name),
            content_hash="sha256:" + name,
            received_at=received_at or text.anchor_utc or "2026-10-05T01:12:00.000Z",
            case_id=case_id,
        )
    )


def process(store, document, *, name: str = "", **kwargs):
    """对 fixture 跑一次 `process_document`，返回报告。"""
    from asistent.understand import process_document

    if not name:
        name = os.path.basename(document.origin_path)
    return process_document(store, document, eml_bytes=fixture_bytes(name), **kwargs)


def raw_extract(name: str, *, source_object_id: str = "doc_probe", path: str = ""):
    """只跑确定性抽取（不落库），便于断言抽取层。"""
    from asistent.understand import decode_eml_bytes, extract

    return extract(
        decode_eml_bytes(fixture_bytes(name)),
        source_object_id=source_object_id,
        path=path or fixture_path(name),
        content_hash="sha256:" + name,
    )


def parse_locator(locator: str) -> Tuple[int, int]:
    """把契约的 `locator`（形如 `line:15` / `line:15-line:18`）解成 `(start, end)` 行号。

    契约的 `Citation.validate()` 只要求 `locator` 非空、不规定格式；理解层统一产出
    `line:N[-line:M]`，测试用它证明"引用能打开到原文的同一段"。
    """
    pieces = [item.strip() for item in str(locator).replace("–", "-").split("-") if item.strip()]
    numbers = []
    for piece in pieces:
        _, _, raw = piece.partition(":")
        numbers.append(int(raw or piece))
    if not numbers:
        raise AssertionError("无法解析 locator: %r" % (locator,))
    return numbers[0], numbers[-1]


def time_field(report, semantic: str) -> Optional[Dict[str, Any]]:
    for item in report.time_fields:
        if item["semantic"] == semantic:
            return item
    return None


def questions_of(store, case_id: str) -> List[Question]:
    return list(store.find("Question", where={"case_id": case_id}, limit=None))


def facts_of(store, case_id: str) -> List[Fact]:
    return list(store.find("Fact", where={"case_id": case_id}, limit=None))


def tasks_of(store, case_id: str) -> List[Task]:
    return list(store.find("Task", where={"case_id": case_id}, limit=None))


def drafts_of(store, case_id: str) -> List[Draft]:
    return [item for item in store.find("Draft", limit=None) if item.case_id == case_id]


def open_drafts(store) -> List[Draft]:
    return [item for item in store.find("Draft", limit=None) if str(item.state) == "OPEN"]


def loaded_module():
    import asistent.understand as module

    return module


class UnderstandTestCase(unittest.TestCase):
    """公共基类：统一建 store、统一收尾断言。"""

    def setUp(self) -> None:
        self.store = store_factory()

    def assertHasCitation(self, payload: Dict[str, Any], *, owner: str = "") -> None:
        """每条引用都必须能被打开：path + locator + content_hash + supports 都在。"""
        for key in ("path", "locator", "content_hash", "supports", "source_object_id"):
            self.assertTrue(payload.get(key), "%s 的引用缺 %s：%r" % (owner or "对象", key, payload))
