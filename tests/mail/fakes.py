"""tests/mail 的公共夹具（内存邮件源、崩溃注入存储、.eml 构造器）。

这些夹具**只用于测试**：被测代码只依赖 `asistent.contracts` 的两个协议，
所以这里既不修改契约，也不给生产代码留后门。
"""

from __future__ import annotations

import email.message
import os
import tempfile
from email.header import Header
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from asistent.contracts.ids import sha256_bytes
from asistent.contracts.mail import MailMessageRef, imap_raw_to_ref
from asistent.contracts.reference import ReferenceStore
from asistent.mail import errors as mail_errors
from asistent.mail.sync import SyncStateStore


class SimulatedCrash(BaseException):
    """模拟进程中途被杀：`BaseException` 不会被同步层的 `except Exception` 吃掉。"""


class CrashInjectingStore(ReferenceStore):
    """在第 N 次 `put` 之后抛 `SimulatedCrash`，用来复现「写入一半崩溃」。"""

    def __init__(self, *, crash_after_puts: Optional[int] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.put_count = 0
        self.crash_after_puts = crash_after_puts

    def put(self, obj: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        self.put_count += 1
        if self.crash_after_puts is not None and self.put_count > self.crash_after_puts:
            raise SimulatedCrash(
                "模拟进程在写入过程中崩溃（第 %d 次 put 之后）" % self.crash_after_puts
            )
        return super().put(obj, **kwargs)  # type: ignore[arg-type]


class CrashOnSaveStateStore(SyncStateStore):
    """在第 N 次状态保存（= 推进游标）时崩溃，用来复现「对象已写、游标未落盘」。"""

    def __init__(self, *args: Any, crash_after_saves: Optional[int] = None, **kwargs: Any) -> None:
        self.crash_after_saves = crash_after_saves
        self.save_count = 0
        super().__init__(*args, **kwargs)

    def save(self) -> None:  # type: ignore[override]
        self.save_count += 1
        if self.crash_after_saves is not None and self.save_count > self.crash_after_saves:
            raise SimulatedCrash("模拟在保存同步游标前崩溃（第 %d 次保存）" % self.save_count)
        super().save()


def counts(store: ReferenceStore) -> Dict[str, int]:
    """对象计数（含审计与事件）。"""
    stats = dict(store.stats())
    stats["AuditEvent"] = len(store.audit_trail(limit=100000))
    return stats


def audit_rows(store: ReferenceStore) -> List[Any]:
    return store.audit_trail(limit=100000)


def make_eml(
    *,
    subject: str = "关于 10 月活动的通知",
    sender: str = "Organizer <organizer@example.org>",
    to: str = "owner@example.com",
    message_id: Optional[str] = "<notice-1@example.org>",
    body: str = "您好，请于 10 月 20 日前确认是否参加。",
    date: str = "Mon, 05 Oct 2026 09:00:00 +0800",
    attachments: Optional[Sequence[Tuple[str, bytes]]] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    charset: str = "utf-8",
) -> bytes:
    """构造一封（可带附件的）合法邮件字节。"""
    message = email.message.EmailMessage()
    message["From"] = sender
    message["To"] = to
    if message_id:
        message["Message-ID"] = message_id
    message["Date"] = date
    message["Subject"] = Header(subject, charset).encode() if charset != "utf-8" else subject
    for key, value in (extra_headers or {}).items():
        message[key] = value
    if attachments:
        message.set_content(body, charset=charset)
        for name, payload in attachments:
            message.add_attachment(
                payload, maintype="application", subtype="octet-stream", filename=name
            )
    else:
        message.set_content(body, charset=charset)
    return message.as_bytes()


def broken_encoding_eml() -> bytes:
    """编码异常的邮件：未知 charset + 非法字节 + 无法解码的头部。

    这类邮件必须在解析、入库、写状态文件、写审计的全链路都不炸
    （`email` 会给出代理区字符，直接 json 序列化会失败）。
    """
    headers = (
        b"From: \xd6\xd0\xce\xc4\xb7\xa2\xbc\xfe\xc8\xcb <broken@example.org>\r\n"
        b"To: owner@example.com\r\n"
        b"Subject: \xd6\xd0\xce\xc4\xd6\xf7\xcc\xe2 (GBK raw bytes)\r\n"
        b"Message-ID: <broken-1@example.org>\r\n"
        b"Date: not a real date at all\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=unknown-8bit\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n"
    )
    return headers + b"\xff\xfe \xd6\xd0\xce\xc4 body with invalid utf-8 \xff\n"


class MemoryMailSource:
    """内存邮件源：`list_new` 的游标就是「已处理的邮件条数」。

    - `redeliver_all=True` → 每轮都返回全部邮件（模拟重复投递/游标失效重投）；
    - `fail_fetch_uids` → 指定 UID 取原文时抛错（模拟通道中断）；
    - `interrupt_on_list_call` → 第 N 次列取抛错（模拟连接断开）；
    - `return_cursor` → 强行返回指定游标（模拟游标回退）。
    """

    name = "memory"

    def __init__(
        self,
        raws: Iterable[bytes],
        *,
        account: str = "owner@example.com",
        folder: str = "INBOX",
        vendor_ids: Optional[Sequence[Optional[str]]] = None,
        redeliver_all: bool = False,
    ) -> None:
        self.raws = list(raws)
        self.account = account
        self.folder = folder
        self.vendor_ids = list(vendor_ids) if vendor_ids is not None else [None] * len(self.raws)
        self.redeliver_all = redeliver_all
        self.list_calls: List[Optional[str]] = []
        self.fetch_calls: List[str] = []
        self.fail_fetch_uids: Dict[str, BaseException] = {}
        self.interrupt_on_list_call: Optional[Tuple[int, BaseException]] = None
        self.invalid_cursors: set = set()
        self.return_cursor: Optional[str] = None
        self.rebuild_calls = 0

    # ------------------------------------------------------------------ MailSource
    def list_folders(self) -> List[str]:
        return [self.folder]

    def list_new(self, cursor: Optional[str]) -> Tuple[List[MailMessageRef], str]:
        self.list_calls.append(cursor)
        if self.interrupt_on_list_call is not None:
            call_no, exc = self.interrupt_on_list_call
            if len(self.list_calls) == call_no:
                raise exc
        if cursor is not None and cursor in self.invalid_cursors:
            raise mail_errors.CursorInvalid("模拟服务端判定游标失效：%s" % cursor[:32])
        if cursor is None:
            self.rebuild_calls += 1
            start = 0
        elif self.redeliver_all:
            start = 0
        else:
            try:
                start = int(cursor)
            except (TypeError, ValueError):
                raise mail_errors.CursorInvalid("无法解析的游标：%r" % cursor[:32])
        refs = [self._ref(index) for index in range(start, len(self.raws))]
        returned = self.return_cursor if self.return_cursor is not None else str(len(self.raws))
        return refs, returned

    def fetch_raw(self, ref: MailMessageRef) -> bytes:
        self.fetch_calls.append(ref.uid)
        problem = self.fail_fetch_uids.get(ref.uid)
        if problem is not None:
            raise problem
        return self.raws[self._index_of(ref.uid)]

    def cursor_rank(self, cursor: Optional[str]) -> Optional[float]:
        """实现级额外能力（非契约成员）：把游标换算成可比较的数值，供回退检测使用。"""
        try:
            return float(cursor)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ 夹具辅助
    def _index_of(self, uid: str) -> int:
        return int(uid.split(":")[-1]) - 1

    def _ref(self, index: int) -> MailMessageRef:
        vendor_id = self.vendor_ids[index] if index < len(self.vendor_ids) else None
        return imap_raw_to_ref(
            account=self.account,
            folder=self.folder,
            uid="uid:%d" % (index + 1),
            vendor_message_id=vendor_id,
            raw=self.raws[index],
        )


class MinimalMailSource:
    """**只**实现 `MailSource` 必需成员的最小来源。

    刻意不提供可选能力（`cursor_position()` 观测、`cursor_rank()` 数值比较）：
    契约 v0.4 把"当前同步位置"定为**可选**能力，调用方必须降级为"不可观测"，
    而不是报错或假装 0 进度。用它来证明协议本身不要求这些能力。
    """

    name = "minimal"

    def __init__(
        self,
        raws: Iterable[bytes],
        *,
        account: str = "minimal@example.com",
        folder: str = "INBOX",
    ) -> None:
        self.raws = list(raws)
        self.account = account
        self.folder = folder

    def list_folders(self) -> List[str]:
        return [self.folder]

    def list_new(self, cursor: Optional[str]) -> Tuple[List[MailMessageRef], str]:
        seen = int(cursor or "0")
        refs = [
            imap_raw_to_ref(
                account=self.account,
                folder=self.folder,
                uid="uid:%d" % (index + 1),
                vendor_message_id=None,
                raw=self.raws[index],
            )
            for index in range(seen, len(self.raws))
        ]
        return refs, str(len(self.raws))

    def fetch_raw(self, ref: MailMessageRef) -> bytes:
        return self.raws[int(str(ref.uid).split(":")[-1]) - 1]


class IdentityLyingSource(MemoryMailSource):
    """模拟「MCP 元数据投影不变、但正文变化」：ref 的身份摘要固定，取回的字节却变了。

    用于验证同步层不会把新内容静默挂到旧身份上，而是登记新内容代次。
    """

    def __init__(self, raw_v1: bytes, raw_v2: bytes, **kwargs: Any) -> None:
        super().__init__([raw_v1], **kwargs)
        self.raw_v2 = raw_v2
        self.switch = False

    def _ref(self, index: int) -> MailMessageRef:
        ref = super()._ref(index)
        # 固定身份摘要（等价于服务端元数据未变），与取回的字节解耦
        ref.content_digest = sha256_bytes(b"metadata-projection-stable")
        # 摘要来源也要照实声明：这个夹具模拟的是"投影型"通道
        ref.content_origin = "METADATA_PROJECTION"
        return ref

    def fetch_raw(self, ref: MailMessageRef) -> bytes:
        self.fetch_calls.append(ref.uid)
        return self.raw_v2 if self.switch else self.raws[0]


class TempWorkspace:
    """临时工作目录：状态文件、原始内容、stderr 日志、出件箱。"""

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="mvp2-mail-")
        self.root = self._dir.name
        self.state_path = os.path.join(self.root, "state", "mail-sync.json")
        self.raw_root = os.path.join(self.root, "raw")
        self.log_dir = os.path.join(self.root, "logs")
        self.outbox = os.path.join(self.root, "outbox")
        for path in (os.path.dirname(self.state_path), self.raw_root, self.log_dir, self.outbox):
            os.makedirs(path, exist_ok=True)

    def cleanup(self) -> None:
        self._dir.cleanup()


__all__ = [
    "SimulatedCrash",
    "CrashInjectingStore",
    "CrashOnSaveStateStore",
    "counts",
    "audit_rows",
    "make_eml",
    "broken_encoding_eml",
    "MemoryMailSource",
    "MinimalMailSource",
    "IdentityLyingSource",
    "TempWorkspace",
    "mail_errors",
]
