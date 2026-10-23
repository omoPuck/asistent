"""归档与恢复契约：manifest 不可变、逐文件哈希校验、秘密拦截、隔离恢复。

对应 SPEC §12.5、§21.4 与 AT-12/AT-13/AT-14 中不依赖真实网盘的部分。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from asistent.contracts import (
    CONTENT_ORIGIN_METADATA_PROJECTION,
    CONTENT_ORIGIN_RAW_BYTES,
    INTEGRITY_BYTE_LEVEL,
    INTEGRITY_PROJECTION_ONLY,
    INTEGRITY_UNDECLARED,
    VALID_CONTENT_ORIGINS,
    ArchiveIntegrityError,
    ArchivePolicy,
    RestoreJob,
    SecretDetected,
    ValidationError,
    build_manifest,
    capture_files,
    integrity_level,
    require_verified,
    restore_plan,
    scan_for_secrets,
    verify_restore,
    verify_remote,
)
from asistent.contracts.errors import ApprovalRequired, NotAuthorized
from .helpers import store_factory


def _write(root: str, relative: str, content: str) -> str:
    absolute = os.path.join(root, relative)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    with open(absolute, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return absolute


class ContractV07Test(unittest.TestCase):
    """契约 v0.7 增补（CP-20261010-18/19/21/22）：把"散文里的规矩"变成可机检的东西。

    这几条的共性是：规矩都对，但只存在于文字或各自实现里，于是**会漂移**——
    而漂移的后果是"投影被当成字节级证明""未授权被当成网络抖动"这类静默错误。
    """

    def test_content_origin_values_are_canonical(self) -> None:
        self.assertEqual("RAW_BYTES", CONTENT_ORIGIN_RAW_BYTES)
        self.assertEqual("METADATA_PROJECTION", CONTENT_ORIGIN_METADATA_PROJECTION)
        self.assertEqual((CONTENT_ORIGIN_RAW_BYTES, CONTENT_ORIGIN_METADATA_PROJECTION), VALID_CONTENT_ORIGINS)

    def test_modules_do_not_redefine_content_origin_literals(self) -> None:
        """两个模块必须**引用**契约常量，而不是各写一份字面量。

        以前 `asistent/mail/mcp_stdio.py` 与 `asistent/archive/manifest.py` 各定义一份
        `"METADATA_PROJECTION"`；任何一处拼写/大小写漂移都会让"投影 vs 字节级"判据静默错配。
        """
        from asistent.archive import manifest as archive_manifest
        from asistent.mail import mcp_stdio

        self.assertEqual(CONTENT_ORIGIN_METADATA_PROJECTION, mcp_stdio.CONTENT_ORIGIN_PROJECTION)
        self.assertEqual(CONTENT_ORIGIN_METADATA_PROJECTION, archive_manifest.CONTENT_ORIGIN_METADATA_PROJECTION)
        self.assertEqual(CONTENT_ORIGIN_RAW_BYTES, archive_manifest.CONTENT_ORIGIN_RAW_BYTES)
        self.assertEqual(VALID_CONTENT_ORIGINS, tuple(archive_manifest.VALID_CONTENT_ORIGINS))

    def test_integrity_level_never_upgrades_unknown_to_byte_level(self) -> None:
        self.assertEqual(INTEGRITY_BYTE_LEVEL, integrity_level(CONTENT_ORIGIN_RAW_BYTES))
        self.assertEqual(INTEGRITY_PROJECTION_ONLY, integrity_level(CONTENT_ORIGIN_METADATA_PROJECTION))
        # 未声明、拼错、大小写不同 —— 一律降到 UNDECLARED，绝不放行成字节级
        for value in (None, "", "raw_bytes", "METADATA-PROJECTION", "BYTES"):
            self.assertEqual(INTEGRITY_UNDECLARED, integrity_level(value), f"{value!r} 不得被当成字节级")

    def test_not_authorized_is_a_registered_code_distinct_from_approval_required(self) -> None:
        error = NotAuthorized("bdpan 未登录")
        self.assertEqual("NOT_AUTHORIZED", error.code)
        self.assertEqual("PERMISSION_FAILURE", str(error.failure_class))
        self.assertFalse(error.detail["retryable"], "未授权重试多少次都没用")
        self.assertTrue(error.detail["needs_reauth"])
        self.assertTrue(issubclass(NotAuthorized, ApprovalRequired.__mro__[1]))
        self.assertFalse(issubclass(NotAuthorized, ApprovalRequired), "未授权≠需要审批：下一步动作不同")

    def test_restore_job_safety_flags_are_explicit_and_persist(self) -> None:
        defaults = RestoreJob(archive_id="archive:x")
        self.assertTrue(defaults.read_only_mode)
        self.assertTrue(defaults.approvals_invalidated)
        self.assertTrue(defaults.replay_forbidden)

        store = store_factory()
        saved = store.put(
            RestoreJob(
                archive_id="archive:x",
                isolate_dir="/tmp/isolate",
                read_only_mode=True,
                approvals_invalidated=True,
                replay_forbidden=True,
            )
        )
        loaded = store.get("RestoreJob", saved.id)
        self.assertTrue(loaded.approvals_invalidated)
        self.assertTrue(loaded.replay_forbidden)
        self.assertTrue(loaded.read_only_mode)


class ArchiveContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="asistent-archive-")
        self.policy = ArchivePolicy(
            policy_id="default-daily",
            source_paths=["资料库", "事务归档"],
            target_account="owner-pan",
            target_root="/个人助手归档",
            approved_by="owner",
            approved_at="2026-10-10T00:00:00.000Z",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_capture_and_manifest_are_content_addressed(self) -> None:
        _write(self.root, "资料库/笔记.md", "第一版内容\n")
        files, excluded = capture_files(self.root)
        self.assertEqual(1, len(files))
        manifest = build_manifest(scope="snapshot", scope_ref="", files=files, policy=self.policy, contract_version="v0.1")
        self.assertTrue(manifest["archive_id"].startswith("archive:"))
        self.assertEqual(64, len(manifest["manifest_digest"]))
        self.assertEqual([], excluded)

        # 相同内容 → 相同 archive_id（重试复用同一次归档）
        again = build_manifest(scope="snapshot", scope_ref="", files=capture_files(self.root)[0], policy=self.policy, contract_version="v0.1")
        self.assertEqual(manifest["archive_id"], again["archive_id"])

        # 内容变化 → 新 archive_id（变化进入下一快照，不覆盖已校验内容）
        _write(self.root, "资料库/笔记.md", "第二版内容\n")
        changed = build_manifest(scope="snapshot", scope_ref="", files=capture_files(self.root)[0], policy=self.policy, contract_version="v0.1")
        self.assertNotEqual(manifest["archive_id"], changed["archive_id"])

    def test_verify_remote_requires_per_file_hash(self) -> None:
        _write(self.root, "资料库/笔记.md", "内容\n")
        _write(self.root, "资料库/联系人.md", "张三\n")
        files, _ = capture_files(self.root)
        manifest = build_manifest(scope="snapshot", scope_ref="", files=files, policy=self.policy)
        entries = {entry["relative_path"]: entry for entry in manifest["entries"]}

        # 数量与大小一致但内容不同 → 必须判定不一致
        lookalike = {path: ("0" * 64) for path in entries}
        result = verify_remote(manifest=manifest, remote_files=lookalike)
        self.assertFalse(result["ok"])
        self.assertEqual(2, len(result["mismatched"]))
        with self.assertRaises(ArchiveIntegrityError):
            require_verified(result)

        # 全部哈希一致 → 通过
        good = {path: entry["sha256"] for path, entry in entries.items()}
        verified = verify_remote(manifest=manifest, remote_files=good)
        self.assertTrue(verified["ok"])
        self.assertEqual("VERIFIED", verified["state"])
        require_verified(verified)

    def test_missing_remote_files_are_partial_not_verified(self) -> None:
        _write(self.root, "资料库/笔记.md", "内容\n")
        files, _ = capture_files(self.root)
        manifest = build_manifest(scope="snapshot", scope_ref="", files=files, policy=self.policy)
        result = verify_remote(manifest=manifest, remote_files={})
        self.assertFalse(result["ok"])
        self.assertEqual("PARTIAL", result["state"])
        self.assertEqual(["资料库/笔记.md"], result["missing"])

    def test_unverifiable_hash_yields_pending_verification(self) -> None:
        _write(self.root, "资料库/笔记.md", "内容\n")
        files, _ = capture_files(self.root)
        manifest = build_manifest(scope="snapshot", scope_ref="", files=files, policy=self.policy)
        result = verify_remote(manifest=manifest, remote_files={"资料库/笔记.md": ""})
        self.assertFalse(result["ok"])
        self.assertEqual("PENDING_VERIFICATION", result["state"])
        with self.assertRaises(ArchiveIntegrityError):
            require_verified(result)

    def test_sensitive_and_credential_files_are_excluded(self) -> None:
        _write(self.root, "资料库/笔记.md", "内容\n")
        _write(self.root, "资料库/.env", "BDUSS=xxxx\n")
        _write(self.root, "资料库/id_ed25519", "key\n")
        files, excluded = capture_files(self.root)
        self.assertEqual(1, len(files))
        self.assertIn("资料库/.env", excluded)
        self.assertIn("资料库/id_ed25519", excluded)

    def test_secret_scan_blocks_archive(self) -> None:
        _write(self.root, "资料库/笔记.md", "正常内容\n")
        _write(self.root, "资料库/临时.txt", "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n")
        files, _ = capture_files(self.root)
        scan = scan_for_secrets(files)
        self.assertFalse(scan.clean)
        with self.assertRaises(SecretDetected):
            scan.require_clean()
        manifest = build_manifest(scope="snapshot", scope_ref="", files=files, policy=self.policy, secret_scan=scan)
        self.assertFalse(manifest["secret_scan"]["clean"])

    def test_oversized_file_is_refused_not_silently_dropped(self) -> None:
        _write(self.root, "资料库/大文件.bin", "x" * 2048)
        with self.assertRaises(ValidationError):
            capture_files(self.root, max_file_bytes=1024)

    def test_policy_requires_explicit_authorization(self) -> None:
        with self.assertRaises(ValidationError):
            ArchivePolicy(policy_id="p", source_paths=["x"], target_account="a", target_root="/r").validate()

    def test_restore_goes_to_isolated_read_only_dir(self) -> None:
        _write(self.root, "资料库/笔记.md", "内容\n")
        files, _ = capture_files(self.root)
        manifest = build_manifest(scope="snapshot", scope_ref="", files=files, policy=self.policy)

        isolate = tempfile.mkdtemp(prefix="asistent-restore-")
        try:
            plan = restore_plan(manifest, isolate_dir=isolate)
            self.assertTrue(plan["read_only_mode"])
            self.assertFalse(plan["replay_external_actions"])
            self.assertTrue(plan["approvals_invalidated"])

            # 未恢复文件时：校验必须失败
            self.assertFalse(verify_restore(manifest=manifest, restored_root=isolate)["ok"])

            for entry in manifest["entries"]:
                target = os.path.join(isolate, entry["relative_path"])
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copyfile(os.path.join(self.root, entry["relative_path"]), target)
            self.assertTrue(verify_restore(manifest=manifest, restored_root=isolate)["ok"])

            # 内容被篡改 → 必须报哈希不一致
            _write(isolate, "资料库/笔记.md", "被篡改\n")
            self.assertFalse(verify_restore(manifest=manifest, restored_root=isolate)["ok"])
        finally:
            shutil.rmtree(isolate, ignore_errors=True)

    def test_restore_plan_refuses_root_directory(self) -> None:
        manifest = build_manifest(scope="snapshot", scope_ref="", files=[], policy=self.policy)
        with self.assertRaises(ValidationError):
            restore_plan(manifest, isolate_dir=os.path.abspath(os.sep))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
