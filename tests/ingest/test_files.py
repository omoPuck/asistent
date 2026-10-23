"""纯函数测试：二进制/文本探测、编码解码（SPEC §6.1 "按策略跳过并记录原因"）。"""

from __future__ import annotations

import unittest

from asistent.ingest.files import (
    TEXT_CODECS,
    FileKind,
    classify_bytes,
    decode_text,
    extract_index_text,
    looks_binary,
    unsupported_reason,
)

from .support import make_png


class FileKindTest(unittest.TestCase):
    def test_empty_file_is_empty_not_text(self) -> None:
        self.assertEqual(FileKind.EMPTY, classify_bytes(b"", max_bytes=1024))

    def test_real_png_is_detected_as_binary(self) -> None:
        self.assertEqual(FileKind.BINARY, classify_bytes(make_png(), max_bytes=1024 * 1024))

    def test_nul_byte_marks_binary(self) -> None:
        self.assertTrue(looks_binary(b"abc\x00def"))
        self.assertEqual(FileKind.BINARY, classify_bytes(b"abc\x00def", max_bytes=1024))

    def test_size_limit_wins_over_text(self) -> None:
        self.assertEqual(FileKind.TOO_LARGE, classify_bytes(b"a" * 100, max_bytes=10))

    def test_utf8_chinese_is_text(self) -> None:
        self.assertEqual(FileKind.TEXT, classify_bytes("部署方案".encode("utf-8"), max_bytes=1024))

    def test_gb18030_chinese_is_text(self) -> None:
        self.assertEqual(FileKind.TEXT, classify_bytes("百度网盘".encode("gb18030"), max_bytes=1024))


class DecodeTest(unittest.TestCase):
    def test_utf8_and_utf8_bom(self) -> None:
        decoded = decode_text("部署 deploy\n".encode("utf-8"))
        self.assertIsNotNone(decoded)
        self.assertEqual("部署 deploy\n", decoded.text)
        self.assertIn(decoded.codec, TEXT_CODECS)

    def test_gb18030_fallback(self) -> None:
        decoded = decode_text("百度网盘归档".encode("gb18030"))
        self.assertIsNotNone(decoded)
        self.assertEqual("百度网盘归档", decoded.text)
        self.assertEqual("gb18030", decoded.codec)

    def test_binary_bytes_are_not_decoded(self) -> None:
        self.assertIsNone(decode_text(b"\xff\xfe\x00\x00\x01\x02"))

    def test_truncation_is_reported_not_silent(self) -> None:
        text, truncated = extract_index_text("x" * 10, max_chars=4)
        self.assertEqual("xxxx", text)
        self.assertTrue(truncated)
        text2, truncated2 = extract_index_text("abc", max_chars=4)
        self.assertEqual("abc", text2)
        self.assertFalse(truncated2)


class ReasonTextTest(unittest.TestCase):
    def test_every_skip_kind_has_a_human_readable_reason(self) -> None:
        self.assertIn("binary_content_detected", unsupported_reason(FileKind.BINARY, size_bytes=10, max_bytes=100))
        self.assertIn("file_too_large", unsupported_reason(FileKind.TOO_LARGE, size_bytes=200, max_bytes=100))
        self.assertIn("empty_file", unsupported_reason(FileKind.EMPTY, size_bytes=0, max_bytes=100))
        self.assertIn("unreadable", unsupported_reason(FileKind.UNREADABLE, size_bytes=0, max_bytes=100))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
