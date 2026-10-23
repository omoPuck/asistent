"""CP-20261010-32：邮件头字段必须解码成字符串（契约级）。

**缺陷（2026-10-12 实测）**：契约自带的离线邮件源 `FixtureMailSource` 读不了自己的夹具——
`fixtures/public/understand/*.eml` 6 个文件 6 个全失败：

    AttributeError: 'Header' object has no attribute 'split'     （contracts/mail.py:191）
    LookupError: unknown encoding: unknown-8bit                  （解码头字段时）

原因是 `email.message_from_bytes()` 对含 RFC 2047 编码字的头返回 `Header` 对象而不是 `str`，
而当时的代码写的是 `message.get("To").split(",")`；即使不崩，下游拿到的也是 `Header`，
不能当字符串用（存身份、写标题、做线程关联全要字符串）。

**当时的处理**：在服务层另写一个"头字段统一解码"的邮件源绕过去。
**现在的处理**：把解码放回契约层，并删掉服务层补丁——同一个缺陷不该有两个实现。

这组用例锁住的是**给出去的必须是可比较的字符串**这条契约，而不只是"别抛异常"。
"""

from __future__ import annotations

import base64
import email
import os
import tempfile
import unittest

from asistent.contracts.ids import sha256_bytes
from asistent.contracts.mail import (
    FixtureMailSource,
    MailMessageRef,
    _address_list,
    _header_text,
)


class HeaderTextTest(unittest.TestCase):
    """`_header_text`：任何头字段值都要变成**解好的**字符串。"""

    def test_plain_string_passes_through(self) -> None:
        self.assertEqual(_header_text("owner@example.com"), "owner@example.com")

    def test_none_and_empty(self) -> None:
        self.assertEqual(_header_text(None), "")
        self.assertEqual(_header_text(""), "")
        self.assertEqual(_header_text("   "), "")

    def test_header_object_is_not_returned_as_object(self) -> None:
        # 直接构造 `Header`：这条分支必须成立，因为真机 `message_from_bytes()` 会产出它
        # （3.10/3.12 的 compat32 在不同输入下分别给 str 或 Header，所以别把
        #  "解析器一定给 Header" 写成前提——要锁的是"给出去的一定是解好的字符串"）
        header = email.header.Header("张 三", "utf-8")
        text = _header_text(header)
        self.assertIsInstance(text, str)
        self.assertEqual(text, "张 三")

        # 解析路径：不论解析器给出 str 还是 Header，结果都必须是解好的字符串
        for raw in (
            b"To: =?utf-8?B?5byg5LiJ?= <z@example.com>\n\n",
            "To: =?utf-8?B?5byg5LiJ?= <z@example.com>\n\n",
        ):
            with self.subTest(kind=type(raw).__name__):
                message = (
                    email.message_from_bytes(raw)
                    if isinstance(raw, bytes)
                    else email.message_from_string(raw)
                )
                self.assertEqual(_header_text(message.get("To")), "张三 <z@example.com>")

    def test_unknown_charset_does_not_explode(self) -> None:
        # 夹具里真实出现过：声明的字符集名 Python 不认识（unknown-8bit）
        message = email.message_from_string(
            "From: =?unknown-8bit?B?5byg5LiJ?= <z@example.com>\n\n"
        )
        text = _header_text(message.get("From"))
        self.assertIsInstance(text, str)
        self.assertIn("z@example.com", text)

    def test_gb18030_fallback_for_unlabelled_chinese_bytes(self) -> None:
        # 中文邮箱现实里常见：头里说是 unknown-8bit，字节其实是 GB18030
        encoded = base64.b64encode("张伟".encode("gb18030")).decode()
        message = email.message_from_string(
            "From: =?unknown-8bit?B?%s?= <z@example.com>\n\n" % encoded
        )
        text = _header_text(message.get("From"))
        self.assertIn("张伟", text, "按 GB18030 兜底解码应当还原中文，而不是变成问号")

    def test_utf8_fallback_when_charset_lies(self) -> None:
        encoded = base64.b64encode("李四".encode("utf-8")).decode()
        message = email.message_from_string(
            "Subject: =?iso-8859-1?B?%s?=\n\n" % encoded  # 声明撒谎：其实是 UTF-8
        )
        self.assertIn("李四", _header_text(message.get("Subject")))


class AddressListTest(unittest.TestCase):
    """`_address_list`：收件人解析要能扛住真实邮件的写法。"""

    def test_plain_addresses(self) -> None:
        self.assertEqual(
            _address_list("a@example.com, b@example.com"),
            ["a@example.com", "b@example.com"],
        )

    def test_display_name_with_comma_is_one_address(self) -> None:
        # 裸 split(",") 会切出两个"地址"，而其中一个是显示名的后半截——
        # 收件人进身份与审批判断，错一个字符就是错一封邮件。
        # 归一化后仍带引号是**正确**的（RFC 5322 要求显示名里的逗号必须加引号），
        # 本条锁的是"只有 1 个地址"，不是归一化的具体写法。
        values = _address_list('"Doe, John" <john@example.com>')
        self.assertEqual(len(values), 1)
        self.assertIn("john@example.com", values[0])
        self.assertEqual(values[0], '"Doe, John" <john@example.com>')

    def test_encoded_display_name(self) -> None:
        values = _address_list("=?utf-8?B?5byg5LiJ?= <z@example.com>")
        self.assertEqual(len(values), 1)
        self.assertIn("z@example.com", values[0])

    def test_empty_and_none(self) -> None:
        self.assertEqual(_address_list(None), [])
        self.assertEqual(_address_list(""), [])
        self.assertEqual(_address_list("   "), [])


class FixtureMailSourceReadsOwnFixturesTest(unittest.TestCase):
    """回归：契约自带的离线源必须读得了**仓库自带的夹具**。

    这条用例的写法刻意是"对整个夹具目录跑一遍"，因为原来的缺陷恰恰是
    "逐字段看代码都合理、整目录跑就全崩"——单看 `_ref_for` 不会有人觉得有问题。
    """

    ROOT = os.path.join("fixtures", "public", "understand")

    def test_all_committed_fixtures_load(self) -> None:
        if not os.path.isdir(self.ROOT):
            self.skipTest("夹具目录不存在")
        expected = sorted(name for name in os.listdir(self.ROOT) if name.lower().endswith(".eml"))
        self.assertTrue(expected, "夹具目录里应当有 .eml 文件")
        source = FixtureMailSource(self.ROOT, account="fixture@example.com")
        refs, cursor = source.list_new(None)
        self.assertEqual(len(refs), len(expected))
        self.assertEqual(cursor, str(len(expected)))
        for ref in refs:
            with self.subTest(uid=ref.uid):
                self.assertIsInstance(ref, MailMessageRef)
                # 契约要求：给出去的都是字符串，且 `received_at` 不能是对象
                for field_name in ("subject", "from_addr", "received_at"):
                    self.assertIsInstance(getattr(ref, field_name), str, field_name)
                for item in ref.to_addrs:
                    self.assertIsInstance(item, str)
                self.assertTrue(ref.content_digest, "摘要不能为空")

    def test_refs_are_stable_across_reads(self) -> None:
        # 身份/摘要必须可重复：同一批夹具读两次，结果要逐字段一致
        source = FixtureMailSource(self.ROOT, account="fixture@example.com")
        first, _ = source.list_new(None)
        second, _ = source.list_new(None)
        self.assertEqual([r.identity_key() for r in first], [r.identity_key() for r in second])
        self.assertEqual([r.content_digest for r in first], [r.content_digest for r in second])
        self.assertEqual([r.subject for r in first], [r.subject for r in second])

    def test_fetch_raw_matches_digest(self) -> None:
        source = FixtureMailSource(self.ROOT, account="fixture@example.com")
        refs, _ = source.list_new(None)
        for ref in refs:
            raw = source.fetch_raw(ref)
            self.assertEqual(sha256_bytes(raw), ref.content_digest, ref.uid)

    def test_cursor_prevents_duplicate_listing(self) -> None:
        source = FixtureMailSource(self.ROOT, account="fixture@example.com")
        refs, cursor = source.list_new(None)
        again, cursor2 = source.list_new(cursor)
        self.assertEqual(again, [])
        self.assertEqual(cursor2, cursor)

    def test_non_ascii_subject_is_readable_text(self) -> None:
        # 这些夹具是中英混排的；只要有一个主题解不出来，这条就红
        source = FixtureMailSource(self.ROOT, account="fixture@example.com")
        refs, _ = source.list_new(None)
        self.assertTrue(any(subject and "?" not in subject for subject in (r.subject for r in refs)))

    def test_directory_without_eml_is_empty_not_broken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = FixtureMailSource(tmp, account="fixture@example.com")
            refs, cursor = source.list_new(None)
            self.assertEqual(refs, [])
            self.assertEqual(cursor, "0")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
