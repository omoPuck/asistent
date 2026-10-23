"""隔离恢复测试（SPEC §12.5、§21.4 第 5 条）。

锁住的语义：
- `protected_roots` 必填，且与恢复目标**双向**重叠即拒绝（绝不覆盖主数据）；
- 恢复期间通道写入是锁死的：调用日志里**没有** upload/rm（不重放归档里的发送/上传/审批）；
- 逐文件哈希 + 引用闭合；哈希不一致 → `CONFLICT` 且不覆盖已有文件；
- `METADATA_PROJECTION` 条目只能声称"投影一致"；
- 索引重建失败 → 恢复不算成功（`INDEX_FAILURE`）；
- 恢复记录落盘 + `RestoreJob` 落库（只读模式、审批失效、无重放）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from asistent.archive import (  # noqa: E402
    ArchiveEngine,
    ArchiveLayout,
    build_archive_manifest,
    capture_source_tree,
    freeze_snapshot,
    restore_archive,
)
from asistent.contracts.enums import RestoreState  # noqa: E402
from asistent.contracts.errors import ValidationError  # noqa: E402
from asistent.pan import BdpanCliPanClient  # noqa: E402
from tests.archive.support import FakeBdpanEnv  # noqa: E402
from tests.contracts.helpers import store_factory  # noqa: E402


class RestoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = FakeBdpanEnv(self)
        self.root = tempfile.mkdtemp(prefix="archive-restore-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.source = os.path.join(self.root, "source")
        self.state_dir = os.path.join(self.root, "state")
        self.isolate = os.path.join(self.root, "isolate")
        self.protected = os.path.join(self.root, "main-data")
        os.makedirs(self.source, exist_ok=True)
        os.makedirs(self.protected, exist_ok=True)
        self.pan = BdpanCliPanClient(binary=self.env.binary, root_prefix="asistent", timeout=20)
        self.pan.unlock_writes("appr_restore_test")
        self.layout = ArchiveLayout()
        self.engine = ArchiveEngine(self.pan, layout=self.layout, state_dir=self.state_dir, require_authorized=True)

    # ------------------------------------------------------------------ 构造
    def _write(self, relative: str, content: str) -> str:
        path = os.path.join(self.source, relative.replace("/", os.sep))
        os.makedirs(os.path.dirname(path) or self.source, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def _archived(
        self,
        files: Dict[str, str],
        *,
        origins: Optional[Dict[str, Optional[str]]] = None,
        scope_ref: str = "restore-case",
    ) -> Tuple[List[Any], Dict[str, Any], Any]:
        for name, content in files.items():
            self._write(name, content)
        captured, _excluded, scan = capture_source_tree(self.source, origin_by_relative=origins)
        staged = freeze_snapshot(captured, os.path.join(self.root, "stage", scope_ref))
        manifest = build_archive_manifest(
            scope="case", scope_ref=scope_ref, files=staged, secret_scan=scan
        )
        outcome = self.engine.archive(manifest, staged)
        self.assertTrue(outcome.ok, outcome.summary())
        return staged, manifest, outcome

    def _isolate_dir(self, name: str = "restore-1") -> str:
        path = os.path.join(self.isolate, name)
        os.makedirs(path, exist_ok=True)
        return path

    def _restore(self, archive_id: str, **kwargs):
        return restore_archive(
            self.pan,
            archive_id=archive_id,
            isolate_dir=kwargs.pop("isolate_dir", self._isolate_dir()),
            protected_roots=kwargs.pop("protected_roots", [self.protected]),
            layout=self.layout,
            state_dir=self.state_dir,
            **kwargs,
        )

    # ------------------------------------------------------------------ 隔离边界
    def test_protected_roots_is_required(self) -> None:
        _staged, manifest, _outcome = self._archived({"a.txt": "甲"})
        with self.assertRaises(ValidationError) as ctx:
            restore_archive(
                self.pan,
                archive_id=manifest["archive_id"],
                isolate_dir=self._isolate_dir(),
                protected_roots=[],
            )
        self.assertIn("protected_roots", str(ctx.exception))

    def test_target_overlapping_main_data_is_rejected_in_both_directions(self) -> None:
        _staged, manifest, _outcome = self._archived({"a.txt": "甲"})
        self.env.reset_logs()
        inside_protected = os.path.join(self.protected, "restore")
        with self.assertRaises(ValidationError):
            self._restore(manifest["archive_id"], isolate_dir=inside_protected)
        with self.assertRaises(ValidationError):
            self._restore(manifest["archive_id"], isolate_dir=os.path.dirname(self.protected), protected_roots=[self.protected])
        # 拒绝时不得创建任何文件，也不得读取远端任何字节
        self.assertFalse(os.path.exists(inside_protected))
        self.assertEqual(self.env.calls_of("download"), [])

    def test_restore_performs_no_write_calls(self) -> None:
        _staged, manifest, _outcome = self._archived({"a.txt": "甲"})
        self.env.reset_logs()
        outcome = self._restore(manifest["archive_id"])
        self.assertEqual(outcome.state, RestoreState.VERIFIED.value)
        self.assertEqual(self.env.calls_of("upload"), [], "恢复期间不得上传")
        self.assertEqual(self.env.calls_of("rm"), [], "恢复期间不得删除")
        self.assertEqual(self.env.calls_of("mkdir"), [], "恢复期间不得建远端目录")
        self.assertTrue(outcome.writes_locked_during_restore)
        self.assertFalse(outcome.replay_external_actions)
        self.assertTrue(outcome.read_only_mode)

    # ------------------------------------------------------------------ 正常恢复
    def test_restore_verifies_every_file_and_writes_record(self) -> None:
        _staged, manifest, _outcome = self._archived({"docs/a.md": "资料 A", "docs/b.md": "资料 B"})
        reindex_calls: List[List[str]] = []

        def reindex(paths):
            reindex_calls.append(list(paths))
            return {"indexed": len(paths)}

        outcome = self._restore(manifest["archive_id"], reindex=reindex)
        self.assertEqual(outcome.state, RestoreState.VERIFIED.value)
        self.assertTrue(outcome.ok)
        self.assertEqual(sorted(outcome.verified), ["docs/a.md", "docs/b.md"])
        self.assertTrue(outcome.closure_ok)
        self.assertTrue(outcome.manifest_digest_ok)
        self.assertTrue(outcome.reindex_done)
        self.assertEqual(outcome.reindex_detail, {"indexed": 2})
        self.assertEqual(len(reindex_calls), 1)
        base = outcome.isolate_dir
        self.assertTrue(os.path.isfile(os.path.join(base, "docs", "a.md")))
        with open(os.path.join(base, "docs", "b.md"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "资料 B")
        # 恢复记录：本地必写，且声明只读恢复 / 审批失效 / 无重放
        record = json.loads(open(outcome.record_paths[0], encoding="utf-8").read())
        self.assertTrue(record["read_only_mode"])
        self.assertTrue(record["approvals_invalidated"])
        self.assertFalse(record["replay_external_actions"])
        self.assertEqual(record["state"], RestoreState.VERIFIED.value)
        self.assertIn("只读恢复", record["read_only_notice"])
        # 主数据目录必须原封不动
        self.assertEqual(os.listdir(self.protected), [])

    def test_reindex_failure_means_restore_is_not_successful(self) -> None:
        _staged, manifest, _outcome = self._archived({"a.txt": "甲"})

        def broken_reindex(_paths):
            raise RuntimeError("索引库不可写")

        outcome = self._restore(manifest["archive_id"], reindex=broken_reindex)
        self.assertEqual(outcome.state, RestoreState.FAILED.value)
        self.assertEqual(outcome.error_code, "INDEX_FAILURE")
        self.assertFalse(outcome.reindex_done)
        self.assertIn("索引库不可写", outcome.error_text)

    def test_without_reindex_hook_restore_reports_not_indexed(self) -> None:
        _staged, manifest, _outcome = self._archived({"a.txt": "甲"})
        outcome = self._restore(manifest["archive_id"])
        self.assertEqual(outcome.state, RestoreState.VERIFIED.value)
        self.assertFalse(outcome.reindex_done)
        self.assertTrue(any("未注入索引重建器" in note for note in outcome.notes))

    # ------------------------------------------------------------------ 不一致与冲突
    def test_hash_mismatch_is_conflict_and_files_are_not_overwritten(self) -> None:
        _staged, manifest, _outcome = self._archived({"a.txt": "原始内容"})
        archive_dir = self.layout.archive_dir(manifest["archive_id"])
        self.env.write_remote(f"asistent/{archive_dir}/objects/a.txt", "被篡改")
        isolate = self._isolate_dir()
        outcome = self._restore(manifest["archive_id"], isolate_dir=isolate)
        self.assertEqual(outcome.state, RestoreState.CONFLICT.value)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.hash_mismatches[0]["relative_path"], "a.txt")
        # 不一致的文件不得留在隔离目录里冒充"恢复成功"
        target = os.path.join(isolate, "a.txt")
        self.assertFalse(os.path.isfile(target), "回读哈希不一致的字节不得留在隔离目录")
        self.assertTrue(any("已删除隔离目录" in note for note in outcome.notes))

    def test_existing_different_file_is_a_conflict_and_never_overwritten(self) -> None:
        _staged, manifest, _outcome = self._archived({"a.txt": "归档中的内容"})
        isolate = self._isolate_dir()
        with open(os.path.join(isolate, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("隔离目录里已有的不同内容")
        outcome = self._restore(manifest["archive_id"], isolate_dir=isolate)
        self.assertEqual(outcome.state, RestoreState.CONFLICT.value)
        self.assertEqual(outcome.conflicts, [{"relative_path": "a.txt", "reason": "existing_file_differs"}])
        with open(os.path.join(isolate, "a.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "隔离目录里已有的不同内容", "绝不覆盖已有数据")

    def test_existing_identical_file_is_reused(self) -> None:
        _staged, manifest, _outcome = self._archived({"a.txt": "归档中的内容"})
        isolate = self._isolate_dir()
        with open(os.path.join(isolate, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("归档中的内容")
        outcome = self._restore(manifest["archive_id"], isolate_dir=isolate)
        self.assertEqual(outcome.state, RestoreState.VERIFIED.value)
        self.assertEqual(outcome.verified, ["a.txt"])

    def test_extra_file_in_isolate_dir_breaks_reference_closure(self) -> None:
        _staged, manifest, _outcome = self._archived({"a.txt": "甲"})
        isolate = self._isolate_dir()
        with open(os.path.join(isolate, "unrelated.txt"), "w", encoding="utf-8") as fh:
            fh.write("不在 manifest 里")
        outcome = self._restore(manifest["archive_id"], isolate_dir=isolate)
        self.assertEqual(outcome.state, RestoreState.CONFLICT.value)
        self.assertEqual(outcome.extra_files, ["unrelated.txt"])
        self.assertFalse(outcome.closure_ok)

    def test_missing_remote_manifest_fails_before_writing_anything(self) -> None:
        isolate = self._isolate_dir()
        outcome = self._restore("archive:does-not-exist", isolate_dir=isolate)
        self.assertEqual(outcome.state, RestoreState.FAILED.value)
        self.assertEqual(outcome.error_code, "ARCHIVE_MANIFEST_MISSING")
        self.assertEqual(os.listdir(isolate), [])

    def test_missing_remote_object_is_reported_and_retryable(self) -> None:
        _staged, manifest, _outcome = self._archived({"a.txt": "甲", "b.txt": "乙"})
        archive_dir = self.layout.archive_dir(manifest["archive_id"])
        os.remove(self.env.remote_path(f"asistent/{archive_dir}/objects/b.txt"))
        outcome = self._restore(manifest["archive_id"])
        self.assertEqual(outcome.state, RestoreState.FAILED.value)
        self.assertEqual(outcome.missing, ["b.txt"])
        self.assertEqual(outcome.verified, ["a.txt"])

    def test_projection_entries_are_marked_as_projection_only(self) -> None:
        _staged, manifest, _outcome = self._archived(
            {"raw.txt": "原始", "proj.txt": "投影"},
            origins={"proj.txt": "METADATA_PROJECTION"},
        )
        outcome = self._restore(manifest["archive_id"])
        self.assertEqual(outcome.state, RestoreState.VERIFIED.value)
        self.assertEqual(outcome.projection_only, ["proj.txt"])
        self.assertEqual(outcome.integrity_claim["projection_only_verified"], ["proj.txt"])
        self.assertEqual(outcome.integrity_claim["byte_level_verified"], ["raw.txt"])
        record = json.loads(open(outcome.record_paths[0], encoding="utf-8").read())
        self.assertIn("不得", record["integrity_claim"]["warning"])

    # ------------------------------------------------------------------ 记录与落库
    def test_restore_job_is_persisted_with_read_only_semantics(self) -> None:
        store = store_factory()
        _staged, manifest, _outcome = self._archived({"a.txt": "甲"})
        outcome = restore_archive(
            self.pan,
            archive_id=manifest["archive_id"],
            isolate_dir=self._isolate_dir(),
            protected_roots=[self.protected],
            layout=self.layout,
            state_dir=self.state_dir,
            store=store,
            reindex=lambda paths: {"indexed": len(paths)},
        )
        self.assertEqual(outcome.state, RestoreState.VERIFIED.value)
        jobs = store.find("RestoreJob", where={"archive_id": manifest["archive_id"]})
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertTrue(job.read_only_mode)
        self.assertEqual(job.verified_files, ["a.txt"])
        self.assertEqual(job.state, RestoreState.VERIFIED)
        self.assertTrue(job.reindex_done)
        self.assertIn("审批默认失效", job.note or "")

    def test_restore_hands_back_the_callers_write_lock_state(self) -> None:
        """恢复是只读操作：结束后必须原样交还通道的写锁状态（不得永久锁死调用方）。"""
        staged, manifest, _outcome = self._archived({"a.txt": "甲"})
        self.assertFalse(self.pan.writes_locked)
        outcome = self._restore(manifest["archive_id"])
        self.assertEqual(outcome.state, RestoreState.VERIFIED.value)
        self.assertTrue(outcome.writes_locked_during_restore, "恢复期间必须是锁死状态")
        self.assertFalse(self.pan.writes_locked, "恢复结束后交还解锁状态")
        # 交还后归档通道仍可用（证明没有被永久锁死）
        self._write("b.txt", "乙")
        captured, _excluded, scan = capture_source_tree(self.source)
        staged2 = freeze_snapshot(captured, os.path.join(self.root, "stage", "after-restore"))
        manifest2 = build_archive_manifest(scope="case", scope_ref="after-restore", files=staged2, secret_scan=scan)
        again = self.engine.archive(manifest2, staged2)
        self.assertEqual(again.state, "VERIFIED", again.summary())

        self.pan.lock_writes()
        locked_outcome = self._restore(manifest["archive_id"], isolate_dir=self._isolate_dir("restore-2"))
        self.assertEqual(locked_outcome.state, RestoreState.VERIFIED.value)
        self.assertTrue(self.pan.writes_locked, "原本是锁定状态则保持锁定")

    def test_publish_record_requires_approval_and_is_honest_when_refused(self) -> None:
        _staged, manifest, _outcome = self._archived({"a.txt": "甲"})
        self.pan.lock_writes()
        outcome = self._restore(manifest["archive_id"], publish_record=True)
        self.assertEqual(outcome.state, RestoreState.VERIFIED.value)
        self.assertEqual(len(outcome.record_paths), 1, "本地恢复记录必须写")
        self.assertTrue(any("未提供 publish_approval_ref" in note for note in outcome.notes))
        self.assertFalse(
            any(path.startswith("pan:") for path in outcome.record_paths),
            "未获批准时不得写远端记录",
        )

    def test_publish_record_with_approval_verifies_readback(self) -> None:
        _staged, manifest, _outcome = self._archived({"a.txt": "甲"})
        self.pan.lock_writes()
        outcome = self._restore(
            manifest["archive_id"], publish_record=True, publish_approval_ref="appr_publish_record"
        )
        self.assertEqual(outcome.state, RestoreState.VERIFIED.value)
        published = [path for path in outcome.record_paths if path.startswith("pan:")]
        self.assertEqual(len(published), 1, outcome.notes)
        self.assertTrue(self.pan.writes_locked, "发布后必须重新锁上")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
