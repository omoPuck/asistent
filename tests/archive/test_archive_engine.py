"""归档状态机测试：在**假 bdpan CLI 子进程**上跑真实通道 + 真实引擎。

这里锁住的是交付说明里最容易被做错的几条：
1. 上传完 ≠ 成功：逐文件回读哈希，数量/大小一致不算通过；
2. 部分失败只补传失败对象（**计数断言**：第二次运行的对象传输次数 = 失败数）；
3. 结果未知先 stat/回读判定，**不盲目重传**（远端已有且一致 → 零重传）；
4. 幂等：同 `archive_id` 重跑零上传、复用对象也要在本 attempt 内回读；
5. `METADATA_PROJECTION` 只能声称"投影一致"；
6. 完成后远端被改 → 拒绝覆盖并报不一致；
7. 未授权 = 显式 `NOT_AUTHORIZED`，且一次写都没发。
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
    ArchiveOutcome,
    build_archive_manifest,
    capture_source_tree,
    freeze_snapshot,
)
from asistent.contracts.enums import ArchiveState  # noqa: E402
from asistent.contracts.errors import ArchiveIntegrityError  # noqa: E402
from asistent.pan import BdpanCliPanClient  # noqa: E402
from tests.archive.support import FakeBdpanEnv  # noqa: E402
from tests.contracts.helpers import make_case, store_factory  # noqa: E402


class ArchiveEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = FakeBdpanEnv(self)
        self.root = tempfile.mkdtemp(prefix="archive-engine-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.source = os.path.join(self.root, "source")
        self.stage = os.path.join(self.root, "stage")
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(self.source, exist_ok=True)
        self.pan = BdpanCliPanClient(binary=self.env.binary, root_prefix="asistent", timeout=20)
        self.pan.unlock_writes("appr_engine_test")
        self.layout = ArchiveLayout()
        self.engine = ArchiveEngine(self.pan, layout=self.layout, state_dir=self.state_dir, require_authorized=True)

    # ------------------------------------------------------------------ 构造
    def _write(self, relative: str, content: str) -> str:
        path = os.path.join(self.source, relative.replace("/", os.sep))
        os.makedirs(os.path.dirname(path) or self.source, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def _prepare(
        self,
        files: Dict[str, str],
        *,
        scope: str = "snapshot",
        scope_ref: str = "snap-test",
        origins: Optional[Dict[str, Optional[str]]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Any], Dict[str, Any]]:
        for name, content in files.items():
            self._write(name, content)
        captured, _excluded, scan = capture_source_tree(self.source, origin_by_relative=origins)
        staged = freeze_snapshot(captured, os.path.join(self.stage, scope_ref))
        manifest = build_archive_manifest(
            scope=scope, scope_ref=scope_ref, files=staged, secret_scan=scan, extra=extra
        )
        return staged, manifest

    def _object_uploads(self, *, successful_only: bool = True) -> List[str]:
        return [
            item["target"]
            for item in self.env.calls_of("upload")
            if "/objects/" in item.get("target", "") and (item.get("success") or not successful_only)
        ]

    def _remote(self, relative: str) -> str:
        return self.env.remote_path(f"asistent/{relative}")

    # ------------------------------------------------------------------ 1. 正常路径
    def test_happy_path_verifies_every_file_by_hash(self) -> None:
        staged, manifest = self._prepare({"a.txt": "甲", "b.txt": "乙", "c.txt": "丙"})
        outcome = self.engine.archive(manifest, staged)
        self.assertEqual(outcome.state, ArchiveState.VERIFIED.value)
        self.assertTrue(outcome.ok)
        self.assertEqual(len(outcome.verified), 3)
        self.assertEqual(len(outcome.uploaded), 3)
        self.assertTrue(outcome.marker_written)
        # 每个对象 + manifest + 完成标记
        self.assertEqual(outcome.transfer_count, 5)
        # 逐文件回读：每个对象至少下载回读一次
        self.assertGreaterEqual(self.env.download_count(), 3)
        self.assertEqual(outcome.integrity_claim["byte_level_count"], 3)
        self.assertNotIn("warning", outcome.integrity_claim)

        report = self.engine.verify_archive(manifest["archive_id"])
        self.assertTrue(report["consistent"])
        self.assertEqual(report["contract"]["state"], "VERIFIED")
        self.assertEqual(sorted(report["verified"]), ["a.txt", "b.txt", "c.txt"])
        marker = json.loads(
            open(self._remote(f"{self.layout.archive_dir(manifest['archive_id'])}/_COMPLETE.json"), encoding="utf-8").read()
        )
        self.assertEqual(marker["verification"], "per-file-content-sha256-readback")
        self.assertEqual(marker["integrity_claim"]["byte_level_count"], 3)

    def test_manifest_is_the_only_basis_and_missing_file_is_visible(self) -> None:
        staged, manifest = self._prepare({"a.txt": "甲", "b.txt": "乙"})
        outcome = self.engine.archive(manifest, staged)
        self.assertTrue(outcome.ok)
        # 人为删掉远端一个对象：只读校验必须报缺失，而不是"数量对得上就算过"
        os.remove(self._remote(f"{self.layout.archive_dir(manifest['archive_id'])}/objects/b.txt"))
        report = self.engine.verify_archive(manifest["archive_id"])
        self.assertEqual(report["missing"], ["b.txt"])
        self.assertFalse(report["consistent"])
        self.assertEqual(report["contract"]["state"], "PARTIAL")
        with self.assertRaises(ArchiveIntegrityError):
            self.engine.assert_verified(manifest["archive_id"])

    def test_size_only_match_is_not_acceptance(self) -> None:
        """同尺寸不同内容：必须靠哈希发现（尺寸/数量一致不算通过）。"""
        staged, manifest = self._prepare({"a.txt": "AA"})
        outcome = self.engine.archive(manifest, staged)
        self.assertTrue(outcome.ok)
        remote_object = self._remote(f"{self.layout.archive_dir(manifest['archive_id'])}/objects/a.txt")
        self.assertEqual(os.path.getsize(remote_object), 2)
        with open(remote_object, "w", encoding="utf-8") as fh:
            fh.write("BB")  # 大小相同、内容不同
        self.assertEqual(os.path.getsize(remote_object), 2)
        report = self.engine.verify_archive(manifest["archive_id"])
        self.assertFalse(report["consistent"])
        self.assertEqual(len(report["mismatched"]), 1)

    # ------------------------------------------------------------------ 2. 部分失败只补传失败对象
    def test_partial_failure_retries_only_the_failed_objects(self) -> None:
        staged, manifest = self._prepare({f"f{i}.txt": f"内容{i}" for i in range(5)})
        self.env.set_spec(
            {
                "faults": {
                    "objects/f1.txt": {"upload": "not_found"},
                    "objects/f3.txt": {"upload": "not_found"},
                }
            }
        )
        first = self.engine.archive(manifest, staged)
        self.assertEqual(first.state, ArchiveState.PARTIAL.value)
        self.assertEqual(sorted(item["relative_path"] for item in first.failed), ["f1.txt", "f3.txt"])
        self.assertEqual([item["reason"] for item in first.failed], ["put-failed", "put-failed"])
        self.assertEqual(sorted(first.verified), ["f0.txt", "f2.txt", "f4.txt"])
        self.assertEqual(
            len(self._object_uploads(successful_only=False)), 5, "第一次尝试会尝试上传全部对象"
        )
        self.assertFalse(first.marker_written, "部分失败绝不能写完成标记")
        marker = self._remote(f"{self.layout.archive_dir(manifest['archive_id'])}/_COMPLETE.json")
        self.assertFalse(os.path.exists(marker))
        self.assertTrue(first.retryable)
        state = self.engine.load_state(manifest["archive_id"])
        self.assertEqual(state["state"], "PARTIAL")
        self.assertEqual(sorted(item["relative_path"] for item in state["failed"]), ["f1.txt", "f3.txt"])

        # 重试：只补传失败的两个对象
        self.env.set_spec({})
        self.env.reset_logs()
        second = self.engine.archive(manifest, staged)
        self.assertEqual(second.state, ArchiveState.VERIFIED.value)
        self.assertEqual(second.attempt, 2)
        attempted = self._object_uploads(successful_only=False)
        self.assertEqual(len(attempted), 2, f"第二次运行只应尝试上传失败的 2 个对象，实际 {attempted}")
        self.assertEqual(sorted(os.path.basename(item) for item in attempted), ["f1.txt", "f3.txt"])
        self.assertEqual(sorted(second.uploaded), ["f1.txt", "f3.txt"])
        self.assertEqual(sorted(second.reused), ["f0.txt", "f2.txt", "f4.txt"])
        # 复用对象也必须在本 attempt 内回读校验（不凭本地状态"信任"）
        self.assertGreaterEqual(self.env.download_count(), 2 + 3)
        self.assertTrue(self.engine.verify_archive(manifest["archive_id"])["consistent"])

    def test_generic_upload_error_without_semantics_is_unknown(self) -> None:
        """无 errno 语义的泛化错误（真实环境的崩溃/未知 errno）→ 必须按结果未知上报。"""
        staged, manifest = self._prepare({"a.txt": "甲"})
        self.env.set_spec({"faults": {"objects/a.txt": {"upload": "fail"}}})
        outcome = self.engine.archive(manifest, staged)
        self.assertEqual(outcome.unknown_objects, ["a.txt"])
        self.assertEqual([item["reason"] for item in outcome.failed], ["put-unknown"])
        self.assertFalse(outcome.marker_written)

    # ------------------------------------------------------------------ 3. 结果未知
    def test_unknown_upload_is_resolved_by_stat_and_not_reuploaded(self) -> None:
        staged, manifest = self._prepare({"a.txt": "甲", "b.txt": "乙"})
        self.env.set_spec(
            {
                "timeout_seconds": 5,
                "faults": {"objects/a.txt": {"upload": "timeout", "upload_times": 1}},
            }
        )
        fast = BdpanCliPanClient(binary=self.env.binary, root_prefix="asistent", timeout=1)
        fast.unlock_writes("appr_fast")
        engine = ArchiveEngine(fast, layout=self.layout, state_dir=self.state_dir, require_authorized=True)
        first = engine.archive(manifest, staged)
        self.assertEqual(first.state, ArchiveState.PARTIAL.value)
        self.assertEqual(first.unknown_objects, ["a.txt"])
        self.assertEqual([item["reason"] for item in first.failed], ["put-unknown"])
        self.assertIn("a.txt", [os.path.basename(item) for item in self._object_uploads(successful_only=False)])
        # 副作用可能已发生：远端确实已经存在这个对象（所以必须 stat 判定）
        remote = self._remote(f"{self.layout.archive_dir(manifest['archive_id'])}/objects/a.txt")
        self.assertTrue(os.path.exists(remote))

        self.env.reset_logs()
        second = engine.archive(manifest, staged)
        self.assertEqual(second.state, ArchiveState.VERIFIED.value)
        self.assertEqual(
            self._object_uploads(successful_only=False), [], "远端内容已一致：不得盲目重传（应为零次上传）"
        )
        self.assertEqual(sorted(second.reused), ["a.txt", "b.txt"])

    def test_unknown_without_side_effect_is_uploaded_on_retry(self) -> None:
        staged, manifest = self._prepare({"a.txt": "甲"})
        self.env.set_spec(
            {
                "timeout_seconds": 5,
                "timeout_effect": "none",
                "faults": {"objects/a.txt": {"upload": "timeout", "upload_times": 1}},
            }
        )
        fast = BdpanCliPanClient(binary=self.env.binary, root_prefix="asistent", timeout=1)
        fast.unlock_writes("appr_fast")
        engine = ArchiveEngine(fast, layout=self.layout, state_dir=self.state_dir, require_authorized=True)
        first = engine.archive(manifest, staged)
        self.assertEqual(first.unknown_objects, ["a.txt"])
        remote = self._remote(f"{self.layout.archive_dir(manifest['archive_id'])}/objects/a.txt")
        self.assertFalse(os.path.exists(remote))

        self.env.reset_logs()
        second = engine.archive(manifest, staged)
        self.assertEqual(second.state, ArchiveState.VERIFIED.value)
        self.assertEqual(
            len(self._object_uploads(successful_only=False)), 1, "判定为缺失后才补传，且只补这一个"
        )

    def test_crash_mid_upload_is_unknown(self) -> None:
        staged, manifest = self._prepare({"a.txt": "甲", "b.txt": "乙"})
        self.env.set_spec({"faults": {"objects/b.txt": {"upload": "crash"}}})
        outcome = self.engine.archive(manifest, staged)
        self.assertEqual(outcome.state, ArchiveState.PARTIAL.value)
        self.assertIn("b.txt", outcome.unknown_objects)
        self.assertIn("b.txt", [item["relative_path"] for item in outcome.failed])
        self.assertFalse(outcome.marker_written)

    def test_manifest_upload_failure_keeps_retryable_state(self) -> None:
        staged, manifest = self._prepare({"a.txt": "甲"})
        self.env.set_spec({"faults": {"manifest.json": {"upload": "fail"}}})
        first = self.engine.archive(manifest, staged)
        self.assertEqual(first.state, ArchiveState.PARTIAL.value)
        self.assertEqual(first.error_code, "EXTERNAL_RESULT_UNKNOWN")
        self.assertEqual(first.verified, ["a.txt"], "内容对象都已校验通过，只是清单没落地")
        self.assertFalse(os.path.exists(self._remote(f"{self.layout.archive_dir(manifest['archive_id'])}/_COMPLETE.json")))

        self.env.set_spec({})
        self.env.reset_logs()
        second = self.engine.archive(manifest, staged)
        self.assertEqual(second.state, ArchiveState.VERIFIED.value)
        # 重试不重传内容对象：本 attempt 只有 manifest + 完成标记两次上传
        self.assertEqual(self._object_uploads(), [])
        self.assertEqual(second.transfer_count, 2)

    def test_remote_same_name_different_content_before_archive_is_repaired(self) -> None:
        """远端已有同名但内容不同的对象（归档尚未完成）→ 重传并回读确认（不"看到同名就跳过"）。"""
        staged, manifest = self._prepare({"a.txt": "快照里的内容", "b.txt": "乙"})
        archive_dir = self.layout.archive_dir(manifest["archive_id"])
        self.env.write_remote(f"asistent/{archive_dir}/objects/a.txt", "远端已存在的不同内容")
        outcome = self.engine.archive(manifest, staged)
        self.assertEqual(outcome.state, ArchiveState.VERIFIED.value)
        self.assertEqual(outcome.repaired, ["a.txt"])
        self.assertEqual(sorted(outcome.verified), ["a.txt", "b.txt"])
        with open(self._remote(f"{archive_dir}/objects/a.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "快照里的内容")

    def test_upload_conflict_errno_is_recorded_as_deterministic_failure(self) -> None:
        """远端因"同名已存在"拒绝上传（errno=-10）→ 记确定失败，可重试；不写完成标记。"""
        staged, manifest = self._prepare({"a.txt": "甲", "b.txt": "乙"})
        self.env.set_spec({"faults": {"objects/a.txt": {"upload": "conflict"}}})
        outcome = self.engine.archive(manifest, staged)
        self.assertEqual(outcome.state, ArchiveState.PARTIAL.value)
        self.assertEqual([item["reason"] for item in outcome.failed], ["put-failed"])
        self.assertEqual(outcome.verified, ["b.txt"])
        self.assertEqual(outcome.unknown_objects, [], "errno=-10 是确定的失败语义，不得记成未知")
        self.assertFalse(outcome.marker_written)

    # ------------------------------------------------------------------ 4. 幂等
    def test_rerun_is_idempotent_and_reverifies_reused_objects(self) -> None:
        staged, manifest = self._prepare({"a.txt": "甲", "b.txt": "乙"})
        first = self.engine.archive(manifest, staged)
        self.assertTrue(first.ok)
        uploads_after_first = self.env.upload_count()
        downloads_before = self.env.download_count()

        self.env.reset_logs()
        second = self.engine.archive(manifest, staged)
        self.assertEqual(second.state, ArchiveState.VERIFIED.value)
        self.assertTrue(second.already_complete)
        self.assertEqual(second.transfer_count, 0, "已完成的归档重跑不得有任何上传")
        self.assertEqual(self.env.upload_count(), 0)
        self.assertEqual(sorted(second.reused), ["a.txt", "b.txt"])
        self.assertEqual(self.env.upload_count(), uploads_after_first - uploads_after_first)  # 0
        self.assertGreaterEqual(self.env.download_count(), 2, "复用对象必须重新回读校验")
        del downloads_before

    def test_content_change_derives_new_archive_id_and_leaves_old_archive_intact(self) -> None:
        staged, manifest = self._prepare({"a.txt": "版本 1"})
        first = self.engine.archive(manifest, staged)
        self.assertTrue(first.ok)
        staged2, manifest2 = self._prepare({"a.txt": "版本 2"}, scope_ref="snap-test")
        self.assertNotEqual(manifest["archive_id"], manifest2["archive_id"])
        second = self.engine.archive(manifest2, staged2)
        self.assertTrue(second.ok)
        self.assertTrue(self.engine.verify_archive(manifest["archive_id"])["consistent"])
        self.assertTrue(self.engine.verify_archive(manifest2["archive_id"])["consistent"])

    # ------------------------------------------------------------------ 5. 来源声明
    def test_projection_entries_only_claim_projection(self) -> None:
        staged, manifest = self._prepare(
            {"raw.txt": "原始字节", "proj.txt": "投影重建物"},
            origins={"proj.txt": "METADATA_PROJECTION"},
        )
        outcome = self.engine.archive(manifest, staged)
        self.assertEqual(outcome.state, ArchiveState.VERIFIED.value)
        claim = outcome.integrity_claim
        self.assertEqual(claim["byte_level_verified"], ["raw.txt"])
        self.assertEqual(claim["projection_only_verified"], ["proj.txt"])
        self.assertEqual(claim["projection_only_count"], 1)
        self.assertIn("不得", claim["warning"])
        marker = json.loads(
            open(self._remote(f"{self.layout.archive_dir(manifest['archive_id'])}/_COMPLETE.json"), encoding="utf-8").read()
        )
        self.assertEqual(marker["integrity_claim"]["byte_level_verified"], ["raw.txt"])
        self.assertEqual(marker["content_origin_summary"]["METADATA_PROJECTION"], 1)

    # ------------------------------------------------------------------ 6. 篡改
    def test_completed_archive_tampering_is_refused_not_overwritten(self) -> None:
        staged, manifest = self._prepare({"a.txt": "原始内容"})
        outcome = self.engine.archive(manifest, staged)
        self.assertTrue(outcome.ok)
        remote = self._remote(f"{self.layout.archive_dir(manifest['archive_id'])}/objects/a.txt")
        self.env.write_remote(f"asistent/{self.layout.archive_dir(manifest['archive_id'])}/objects/a.txt", "被篡改的内容")
        with open(remote, encoding="utf-8") as fh:
            tampered = fh.read()
        self.assertEqual(tampered, "被篡改的内容")

        with self.assertRaises(ArchiveIntegrityError) as ctx:
            self.engine.archive(manifest, staged)
        self.assertIn("拒绝覆盖", str(ctx.exception))
        with open(remote, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "被篡改的内容", "拒绝覆盖：远端内容不得被改写")
        report = self.engine.verify_archive(manifest["archive_id"])
        self.assertFalse(report["consistent"])
        self.assertEqual(len(report["mismatched"]), 1)

    def test_readback_mismatch_is_detected_and_no_marker_is_written(self) -> None:
        staged, manifest = self._prepare({"a.txt": "原始内容", "b.txt": "另一个"})
        self.env.set_spec({"faults": {"objects/a.txt": {"upload": "corrupt"}}})
        outcome = self.engine.archive(manifest, staged)
        self.assertEqual(outcome.state, ArchiveState.PARTIAL.value)
        self.assertEqual(sorted(item["relative_path"] for item in outcome.failed), ["a.txt"])
        self.assertEqual([item["reason"] for item in outcome.failed], ["verify-mismatch"])
        self.assertEqual(outcome.verified, ["b.txt"], "上传成功但回读不一致的对象不得计入已校验")
        self.assertFalse(os.path.exists(self._remote(f"{self.layout.archive_dir(manifest['archive_id'])}/_COMPLETE.json")))

    # ------------------------------------------------------------------ 7. 未授权 / 锁定
    def test_not_authorized_fails_explicitly_with_zero_writes(self) -> None:
        staged, manifest = self._prepare({"a.txt": "甲"})
        self.env.set_spec({"logged_in": False})
        outcome = self.engine.archive(manifest, staged)
        self.assertEqual(outcome.state, ArchiveState.FAILED.value)
        self.assertEqual(outcome.error_code, "NOT_AUTHORIZED")
        self.assertIn("待用户授权", outcome.next_step or "")
        self.assertFalse(outcome.retryable)
        self.assertEqual(self.env.calls_of("upload"), [], "未授权时一次写操作都不能发")
        self.assertEqual(self.env.calls_of("rm"), [])
        self.assertEqual(outcome.unknown_objects, [])

    def test_write_lock_failure_is_explicit_and_retryable_after_approval(self) -> None:
        staged, manifest = self._prepare({"a.txt": "甲"})
        locked = BdpanCliPanClient(binary=self.env.binary, root_prefix="asistent", timeout=20)
        engine = ArchiveEngine(locked, layout=self.layout, state_dir=self.state_dir, require_authorized=False)
        outcome = engine.archive(manifest, staged)
        self.assertEqual(outcome.state, ArchiveState.FAILED.value)
        self.assertEqual(outcome.error_code, "APPROVAL_REQUIRED")
        self.assertIn("approval_ref", json.dumps(outcome.to_dict(), ensure_ascii=False))
        self.assertEqual(self.env.calls_of("upload"), [])

        locked.unlock_writes("appr_after_approval")
        retry = engine.archive(manifest, staged)
        self.assertEqual(retry.state, ArchiveState.VERIFIED.value)

    # ------------------------------------------------------------------ 8. 状态落库
    def test_state_file_and_store_objects_record_the_outcome(self) -> None:
        store = store_factory()
        case = make_case(store)
        staged, manifest = self._prepare(
            {"a.txt": "甲", "b.txt": "乙"}, scope="case", scope_ref=case.id, extra={"trigger": "case_closed"}
        )
        engine = ArchiveEngine(
            self.pan, layout=self.layout, state_dir=self.state_dir, store=store, require_authorized=True
        )
        self.env.set_spec({"faults": {"objects/a.txt": {"upload": "not_found"}}})
        outcome = engine.archive(manifest, staged)
        self.assertEqual(outcome.state, ArchiveState.PARTIAL.value)

        state = engine.load_state(manifest["archive_id"])
        self.assertEqual(state["state"], ArchiveState.PARTIAL.value)
        self.assertEqual(state["attempt"], 1)
        job = store.get_by_unique("archive_job", manifest["archive_id"])
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.state, ArchiveState.PARTIAL)
        self.assertEqual(job.attempt_count, 1)
        self.assertEqual(job.pending, ["a.txt"])
        self.assertEqual(job.unverified_reason, "put-failed")
        stored_manifest = store.get_by_unique("archive_id", manifest["archive_id"])
        self.assertIsNotNone(stored_manifest)
        assert stored_manifest is not None
        self.assertEqual(stored_manifest.manifest_digest, manifest["manifest_digest"])
        self.assertEqual(stored_manifest.entries[0].relative_path, "a.txt")
        self.assertEqual(stored_manifest.entries[0].content_origin, "RAW_BYTES")
        # SPEC：部分失败时 Case 的归档状态必须可见（契约枚举里对应 PARTIAL）
        refreshed = store.get("Case", case.id)
        self.assertEqual(refreshed.archive_state, ArchiveState.PARTIAL)

        self.env.set_spec({})
        fixed = engine.archive(manifest, staged)
        self.assertEqual(fixed.state, ArchiveState.VERIFIED.value)
        self.assertEqual(store.get("Case", case.id).archive_state, ArchiveState.VERIFIED)
        job_after = store.get_by_unique("archive_job", manifest["archive_id"])
        self.assertEqual(job_after.attempt_count, 2)
        self.assertEqual(job_after.pending, [])

    def test_outcome_is_json_serialisable_and_carries_no_credentials(self) -> None:
        staged, manifest = self._prepare({"a.txt": "甲"})
        outcome: ArchiveOutcome = self.engine.archive(manifest, staged)
        rendered = json.dumps(outcome.to_dict(), ensure_ascii=False)
        self.assertIn("VERIFIED", rendered)
        for forbidden in ("BDUSS", "token=", "bduss"):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
