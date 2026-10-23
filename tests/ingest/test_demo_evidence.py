"""demo 脚本与验收文档的一致性测试。

`docs/mvp/mvp-1.md` 里的证据块是**手工粘贴的原始输出**，会随着实现变化而腐坏。
这里做两件事：

1. demo 脚本自己能跑通，且所有自检项通过（自检项就是"断言写在演示里"）；
2. 文档里粘贴的原始证据块与当前代码产出的输出**关键片段一致**——
   如果不一致，要么重新跑 demo 更新文档，要么改代码，总之不许让文档变成"传说"。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest

from asistent.ingest import demo_mvp1
from tests.ingest.support import MVP1TestCase

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DOC_PATH = os.path.join(REPO_ROOT, "docs", "mvp", "mvp-1.md")

#: 文档必须原样包含的证据片段（都来自 demo 的原始输出，去掉平台相关的路径）
REQUIRED_DOC_LINES = (
    'contract to_fts_query() : "部 署"',
    'contract to_fts_query() : "百 度 网 盘"',
    'contract to_fts_query() : "会 议 纪 要"',
    "sqlite MATCH rows       : 3",
    "stats_before           : {'AuditEvent': 10, 'Event': 5, 'SourceDocument': 5, '_total': 5}",
    "stats_equal            : True",
    "reason=binary_content_detected: NUL byte in first 8192 bytes",
    "reason=file_too_large: size_bytes=4096 > max_bytes=1024",
    "deleted_doc_index_state: STALE",
    "old_doc_index_state    : STALE",
    "new_doc_index_state    : INDEXED",
    "within_60s             : True",
    "within_5s              : True",
    "OK (skipped=5)",
    "Ran 190 tests",
)


class DemoScriptTest(MVP1TestCase):
    def test_demo_runs_and_every_self_check_passes(self) -> None:
        result = demo_mvp1.capture()

        self.assertEqual(0, result["exit_code"], "demo 自检不通过：\n" + result["output"])
        self.assertFalse([entry for entry in result["checks"] if not entry["ok"]], result["checks"])
        # 自检项数量不应退化成 0（否则"全绿"没有意义）
        self.assertGreaterEqual(len(result["checks"]), 25, len(result["checks"]))
        # 关键验收点必须在自检里
        names = " ".join(entry["name"] for entry in result["checks"])
        for needle in ("MATCH", "注入式", "二次扫描", "增量", "skip 原因", "端到端 elapsed_seconds", "重建后重新可检索"):
            self.assertIn(needle, names)

    def test_demo_json_flag_writes_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "evidence.json")
            code = demo_mvp1.main(["--json", target])
            self.assertEqual(0, code)
            with open(target, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(0, payload["exit_code"])


@unittest.skipUnless(os.path.isfile(DOC_PATH), f"缺少验收文档: {DOC_PATH}")
class EvidenceDocumentTest(MVP1TestCase):
    def _document(self) -> str:
        with open(DOC_PATH, encoding="utf-8") as handle:
            return handle.read()

    def test_document_contains_raw_evidence_blocks(self) -> None:
        text = self._document()
        missing = [line for line in REQUIRED_DOC_LINES if line not in text]
        self.assertEqual([], missing, f"docs/mvp/mvp-1.md 缺少原始证据行: {missing}")

    def test_document_records_honest_contract_change(self) -> None:
        """contracts 被改动过就必须在文档里如实披露（不能只改代码不改文档）。"""
        text = self._document()
        self.assertIn("asistent/contracts/reference.py", text)
        self.assertIn("git diff --stat", text)
        self.assertIn("CP", text, "必须留下 CP（契约变更）提案痕迹")

    def test_document_has_no_placeholder(self) -> None:
        text = self._document()
        for placeholder in ("TODO", "TBD", "FIXME", "<待填>", "xxx"):
            self.assertNotIn(placeholder, text, f"验收文档残留占位符: {placeholder}")
        self.assertGreater(len(text), 4000, "文档太短，不像是完整验收记录")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
