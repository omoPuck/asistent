"""邮件通道契约（SPEC §7、§21.2）。

冻结的是协议：设备侧服务只依赖 `MailSource` / `MailSender`，
实现可以是 `mcp-email-server` 通道、IMAP/SMTP 直连，或离线夹具。

硬性约束：
- 接收侧必须给出**账号作用域内的稳定消息身份**（供应商不可变 ID 优先）；
  Message-ID 只能作为线程/重复候选，不能单独合并不同账号的邮件；
- 发送侧必须返回可分类的结果（SUCCEEDED / FAILED_SAFE / UNKNOWN），
  `UNKNOWN` 一律不许自动重试（§21.2）；
- 协议签名里带 `idempotency_key`，由核心分配，模型不得自造。
"""

from __future__ import annotations

import email
import email.header
import email.utils
import mailbox
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from .errors import ValidationError
from .ids import mail_identity_key, sha256_bytes


#: 这些"字符集名"等于没有声明（发信方自己也不知道字节是什么编码）。
_UNKNOWN_CHARSETS = frozenset({"", "unknown", "unknown-8bit", "x-unknown", "default", "none"})

#: 单字节字符集：**它们永远不会解码失败**，所以"解码成功"不能当作证据。
#: 用它们去解 UTF-8 的字节会得到一串"看着像字母"的乱码（é → Ã©），而且不会报错。
_SINGLE_BYTE_CHARSETS = frozenset(
    {
        "ascii",
        "us-ascii",
        "ansi_x3.4-1968",
        "latin-1",
        "latin1",
        "iso-8859-1",
        "iso8859-1",
        "iso_8859-1",
        "cp1252",
        "windows-1252",
    }
)


def _has_c1_controls(text: str) -> bool:
    """头字段里出现 C1 控制字符（U+0080–U+009F）几乎只可能来自解错编码。"""
    return any("\u0080" <= char <= "\u009f" for char in text)


def _decode_bytes(piece: bytes, charset: Optional[str]) -> str:
    """按声明的字符集解码，**声明的靠不住时也要给出可用结果**。

    真机上会遇到三种"声明靠不住"：
    - `LookupError: unknown encoding: unknown-8bit`：声明的字符集名 Python 根本不认识
      （`unknown-8bit` 是"我也不知道"的约定写法，仓库夹具里就有）；
    - 声明的字符集认识、但字节流其实不是它（转码出错或误标的老邮件）；
    - 声明的是**单字节字符集**（`iso-8859-1` 这类）——这类解码**永远不会失败**，
      于是"能解码"这个判据在这里是假的：UTF-8 的中文字节会被解成 `Ã¥Â¼Â ` 这样的乱码，
      而代码还以为自己成功了。

    任何一种都不该让"读一封邮件"失败：**收信链路挂在一封格式不规范的老邮件上，
    等于整条链路被一封信掐死**。因此这里不追求"猜对每一个字节"，只保证两件事：
    1. 不抛异常（最差也要给出 `utf-8 + replace` 的结果）；
    2. 在几种常见写法里选**最可能是原意**的那个——顺序是
       "字节本来就是合法 UTF-8 且声明不可信 → 就按 UTF-8 解"，
       否则按声明 → UTF-8 → GB18030（中文邮箱现实里很常见）依次试，
       并跳过解出 C1 控制字符的结果。
    """
    declared = (charset or "").strip().lower()
    if declared in _UNKNOWN_CHARSETS or declared in _SINGLE_BYTE_CHARSETS:
        # 声明不可信：字节本身是合法 UTF-8 就以它为准（比信任单字节声明安全得多）
        try:
            candidate = piece.decode("utf-8", "strict")
        except UnicodeDecodeError:
            pass
        else:
            if not _has_c1_controls(candidate):
                return candidate

    for name in (declared, "utf-8", "gb18030"):
        if not name:
            continue
        try:
            text = piece.decode(name, "strict")
        except (LookupError, UnicodeDecodeError):
            continue
        if _has_c1_controls(text):
            continue  # 这个字符集明显不对，继续试下一个
        return text
    # 全都不行：宁可个别字符是问号，也不抛异常
    return piece.decode("utf-8", "replace")


def _header_text(value: Any) -> str:
    """把任意头字段值统一成**解码后的字符串**（`None` / `str` / `Header` 都吃）。

    为什么契约层必须自己解决：`email.message_from_bytes()` 对含 RFC 2047 编码字的头
    （`To: =?utf-8?B?...?=`）**可能返回 `Header` 对象而不是 `str`**，而 `Header` 没有
    `split()`。于是"读一封收件人名字是中文的邮件"会直接抛
    `AttributeError: 'Header' object has no attribute 'split'`。

    这不是边角情况：仓库自带夹具 `fixtures/public/understand/*.eml` **全部**带这种头，
    也就是说契约自带的离线邮件源**读不了自己的夹具**（6/6 全失败，2026-10-12 实测）。
    当时的权宜之计是在服务层另写一个"头字段统一解码"的源绕过去——那等于把契约的缺陷
    变成每个调用方各自的补丁。正确的位置是这里：**头字段解码属于通道契约的一部分**，
    因为"给出去的必须是可比较的字符串"是下游能成立的前提（存身份、写标题、做线程关联都要它）。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        # `email.header.Header` 及类似对象：先拿回（可能仍是编码字的）字符串
        try:
            encoded = value.encode()
        except Exception:  # noqa: BLE001 - 解不出来也不能让"读一封邮件"失败
            encoded = str(value)
        text = encoded.decode("utf-8", "replace") if isinstance(encoded, bytes) else str(encoded)
    if "=?" in text:  # 只在确实像编码字时才走解码，避免无谓改变普通文本
        chunks: List[str] = []
        for piece, charset in email.header.decode_header(text):
            if isinstance(piece, bytes):
                chunks.append(_decode_bytes(piece, charset))
            else:
                chunks.append(str(piece))
        text = "".join(chunks)
    return text.strip()


def _address_list(value: Any) -> List[str]:
    """解析收件人类头字段，返回**字符串**列表。

    用 `getaddresses()` 而不是 `split(",")`：显示名里带逗号
    （`"Doe, John" <j@example.com>`）用裸切分会切出一个不存在的地址，
    而"收件人是谁"会进身份与审批判断，错一个字符就是错一封邮件。
    """
    text = _header_text(value)
    if not text:
        return []
    out: List[str] = []
    for name, addr in email.utils.getaddresses([text]):
        name = (name or "").strip()
        addr = (addr or "").strip()
        if not name and not addr:
            continue
        out.append(email.utils.formataddr((name, addr)) if name else addr)
    return out



@dataclass
class MailMessageRef:
    """一封邮件的定位信息（不含正文；正文按需 fetch）。"""

    account: str
    folder: str
    uid: str
    vendor_message_id: Optional[str] = None
    message_id: Optional[str] = None
    thread_keys: List[str] = field(default_factory=list)
    subject: str = ""
    from_addr: str = ""
    to_addrs: List[str] = field(default_factory=list)
    received_at: str = ""
    size_bytes: int = 0
    has_attachments: bool = False
    content_digest: str = ""
    content_origin: str = "RAW_BYTES"
    #: 契约 v0.4（CP-20261010-07）：`content_digest` 的来源必须自己声明。
    #: - `RAW_BYTES`：通道能给出 RFC822 原始字节，摘要即"原文摘要"；
    #: - `METADATA_PROJECTION`：通道只给元数据（例如 mcp-email-server 的 `get_emails` /
    #:   `get_emails_content` 都不暴露 raw 字节），摘要只能反映**元数据投影**。
    #:   下游据此判断"能否把它当字节级完整性证明"，也据此决定归档条目怎么标注。
    #: 默认 `RAW_BYTES` 是为了不破坏既有通道；**投影型通道必须显式改成 `METADATA_PROJECTION`**。

    def identity_key(self) -> str:
        """账号作用域内的稳定身份（SPEC §21.2 接收去重）。"""
        return mail_identity_key(
            account_scope=self.account,
            vendor_message_id=self.vendor_message_id,
            mailbox_scope_id=f"{self.folder}:{self.uid}" if self.uid else None,
            content_digest=self.content_digest or None,
            message_id=self.message_id,
        )


@dataclass
class MailSendRequest:
    from_account: str
    to: List[str]
    subject: str
    body: str
    cc: List[str] = field(default_factory=list)
    bcc: List[str] = field(default_factory=list)
    attachment_paths: List[str] = field(default_factory=list)
    in_reply_to: Optional[str] = None
    references: List[str] = field(default_factory=list)
    #: 由核心分配的稳定幂等键：同一已批准动作的所有重试必须沿用
    idempotency_key: str = ""
    action_id: str = ""

    def validate(self) -> None:
        if not self.from_account:
            raise ValidationError("发送必须指定发件账号")
        if not self.to:
            raise ValidationError("发送必须至少有一个收件人")
        if not self.idempotency_key:
            raise ValidationError("发送必须携带核心分配的幂等键（禁止模型自造）")


@dataclass
class MailSendResult:
    #: SUCCEEDED / FAILED_SAFE / UNKNOWN
    result_class: str
    accepted: bool
    external_ref: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @staticmethod
    def unknown(reason: str, raw: Optional[Dict[str, Any]] = None) -> "MailSendResult":
        return MailSendResult(result_class="UNKNOWN", accepted=False, error=reason, raw=raw or {})

    @staticmethod
    def failed_safe(reason: str, raw: Optional[Dict[str, Any]] = None) -> "MailSendResult":
        return MailSendResult(result_class="FAILED_SAFE", accepted=False, error=reason, raw=raw or {})

    @staticmethod
    def succeeded(external_ref: str, raw: Optional[Dict[str, Any]] = None) -> "MailSendResult":
        return MailSendResult(result_class="SUCCEEDED", accepted=True, external_ref=external_ref, raw=raw or {})


@runtime_checkable
class MailSource(Protocol):
    """接收侧：持续获取新邮件并给出稳定身份与恢复点。

    可选能力（契约 v0.4，CP-20261010-08）：实现**可以**额外提供

        def cursor_position(self) -> Optional[Dict[str, Any]]: ...

    用于把"当前同步位置"作为可观测状态暴露给控制台/巡检（例如 `{"uid_next": 42}`）。
    调用方必须用 `getattr(source, "cursor_position", None)` 探测存在性，
    **不得假定存在**：不同通道（IMAP 直连 / MCP 元数据通道 / 离线夹具）掌握的信息不同。
    探测不到时应降级为"不可观测"，而不是报错或假装 0 进度。
    """

    name: str

    def list_folders(self) -> List[str]: ...

    def list_new(self, cursor: Optional[str]) -> Tuple[List[MailMessageRef], str]:
        """返回（新邮件引用列表, 新的同步游标）。游标失效/回退时的行为由实现负责，但必须可恢复。"""
        ...

    def fetch_raw(self, ref: MailMessageRef) -> bytes: ...


@runtime_checkable
class MailSender(Protocol):
    """发送侧：受控外发适配器，必须能区分"确定未发送"与"结果未知"。"""

    name: str

    def send(self, request: MailSendRequest) -> MailSendResult: ...


class FixtureMailSource:
    """离线夹具邮件源：读取目录下的 `.eml` 文件（可提交到仓库的脱敏夹具）。

    用它可以在没有真实邮箱的情况下把「同步 → 去重 → 关联 → 草稿 → 审批 → 发送」
    的可测部分全部跑通（ADR-0003 默认方案）。
    """

    name = "fixture_eml"

    def __init__(self, root: str, *, account: str = "fixture@example.com") -> None:
        self.root = os.path.abspath(root)
        self.account = account
        self.folder = "INBOX"

    def list_folders(self) -> List[str]:
        return [self.folder]

    def _paths(self) -> List[str]:
        if not os.path.isdir(self.root):
            # 目录不存在 ≠ 今天没信：静默返回空会让"邮箱根本没接上"看起来像"今天没有邮件"，
            # 而这两件事的处理方式完全不同（一个要修配置，一个什么都不用做）。
            raise ValidationError(f"夹具邮件目录不存在：{self.root}")
        return sorted(
            os.path.join(self.root, name) for name in os.listdir(self.root) if name.lower().endswith(".eml")
        )

    def list_new(self, cursor: Optional[str]) -> Tuple[List[MailMessageRef], str]:
        try:
            seen = max(0, int(cursor or "0"))
        except (TypeError, ValueError):
            # 游标坏了就当没读过：**宁可重读，不可漏读**。下游按身份去重，
            # 重读只是多花点时间；漏读却是永久丢信。
            seen = 0
        paths = self._paths()
        refs: List[MailMessageRef] = []
        for index, path in enumerate(paths[seen:], start=seen):
            refs.append(self._ref_for(path, index))
        return refs, str(len(paths))

    def _ref_for(self, path: str, index: int) -> MailMessageRef:
        with open(path, "rb") as handle:
            raw = handle.read()
        message = email.message_from_bytes(raw)
        attachments = any(part.get_filename() for part in message.walk())
        message_id = _header_text(message.get("Message-ID")) or None
        references = [item for item in _header_text(message.get("References")).split() if item]
        thread_keys = [key for key in ([message_id] + references) if key]
        return MailMessageRef(
            account=self.account,
            folder=self.folder,
            uid=f"fixture-{index}:{os.path.basename(path)}",
            vendor_message_id=None,
            message_id=message_id,
            thread_keys=thread_keys,
            subject=_header_text(message.get("Subject")),
            from_addr=_header_text(message.get("From")),
            to_addrs=_address_list(message.get("To")),
            received_at=_header_text(message.get("Date")),
            size_bytes=len(raw),
            has_attachments=attachments,
            content_digest=sha256_bytes(raw),
        )

    def fetch_raw(self, ref: MailMessageRef) -> bytes:
        for index, path in enumerate(self._paths()):
            if ref.uid.startswith(f"fixture-{index}:"):
                with open(path, "rb") as handle:
                    return handle.read()
        raise ValidationError(f"夹具邮件不存在: {ref.uid}")


class RecordingMailSender:
    """记录型发送器：只写本地发件箱文件，绝不真正外发。

    用于回归测试「同一动作只被提交一次」；真实发信必须换成受控的真实适配器。
    """

    name = "recording"

    def __init__(self, outbox_dir: str) -> None:
        self.outbox_dir = os.path.abspath(outbox_dir)
        os.makedirs(self.outbox_dir, exist_ok=True)
        self.sent: List[MailSendRequest] = []

    def send(self, request: MailSendRequest) -> MailSendResult:
        request.validate()
        for existing in self.sent:
            if existing.idempotency_key == request.idempotency_key:
                return MailSendResult.failed_safe(
                    "幂等键已被使用：拒绝重复提交（受控适配器不去重，靠核心幂等）",
                    raw={"idempotency_key": request.idempotency_key},
                )
        self.sent.append(request)
        path = os.path.join(self.outbox_dir, f"{len(self.sent):04d}-{request.idempotency_key.split(':')[-1][:12]}.eml")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"From: {request.from_account}\n")
            handle.write(f"To: {', '.join(request.to)}\n")
            if request.bcc:
                handle.write(f"Bcc: {', '.join(request.bcc)}\n")
            handle.write(f"Subject: {request.subject}\n")
            handle.write(f"X-Asistent-Idempotency-Key: {request.idempotency_key}\n\n")
            handle.write(request.body)
        return MailSendResult.succeeded(external_ref=os.path.basename(path), raw={"outbox": path})


def imap_raw_to_ref(*, account: str, folder: str, uid: str, vendor_message_id: Optional[str], raw: bytes) -> MailMessageRef:
    """把 IMAP 原始邮件转成引用（供真实 IMAP 实现复用，保证身份规则一致）。"""
    message = email.message_from_bytes(raw)
    return MailMessageRef(
        account=account,
        folder=folder,
        uid=uid,
        vendor_message_id=vendor_message_id,
        message_id=(message.get("Message-ID") or "").strip() or None,
        thread_keys=[item for item in ([message.get("Message-ID")] + (message.get("References") or "").split()) if item],
        subject=message.get("Subject", ""),
        from_addr=message.get("From", ""),
        to_addrs=[item.strip() for item in (message.get("To") or "").split(",") if item.strip()],
        received_at=message.get("Date", ""),
        size_bytes=len(raw),
        has_attachments=any(part.get_filename() for part in message.walk()),
        content_digest=sha256_bytes(raw),
    )


def mbox_all_paths(path: str) -> List[str]:
    """读取 mbox 的全部消息（辅助工具；不作为运行时依赖）。"""  # pragma: no cover - 工具函数
    if not os.path.exists(path):
        return []
    box = mailbox.mbox(path)
    return [item.as_string() for item in box]


__all__ = [
    "MailSource",
    "MailSender",
    "MailMessageRef",
    "MailSendRequest",
    "MailSendResult",
    "FixtureMailSource",
    "RecordingMailSender",
    "imap_raw_to_ref",
]
