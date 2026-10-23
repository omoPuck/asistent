"""`asistent/archive/manifest.py` 测试：不可变清单、逐条来源声明、引用闭合、秘密拦截。

重点（契约 §12.2 的漏洞面）：`content_origin` 必须**逐条**声明、并且**纳入 manifest_digest**，
否则"把投影摘要当字节级证明"这种篡改无法被发现。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from asistent.archive import manifest as M  # noqa: E402
from asistent.contracts.archive import ArchivePolicy, build_manifest as contract_build_manifest  # noqa: E402
from asistent.contracts.errors import SecretDetected, ValidationError  # noqa: E402
from asistent.contracts.ids import archive_id_for  # noqa: E402


def _policy(**overrides) -> ArchivePolicy:
    payload = {
        "policy_id": "pol_test",
        "source_paths": ["/tmp/source"],
        "target_account": "owner@example.com",
        "target_root": "asistent",
        "approved_by": "user",
    }
    payload.update(overrides)
    return ArchivePolicy(**payload)


class ManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="archive-manifest-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.source = os.path.join(self.root, "source")
        os.makedirs(self.source)

    def _write(self, relative: str, content: str) -> str:
        path = os.path.join(self.source, relative.replace("/", os.sep))
        os.makedirs(os.path.dirname(path) or self.source, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def _capture(self, **kwargs):
        return M.capture_source_tree(self.source, **kwargs)

    # ------------------------------------------------------------------ 捕获与来源
    def test_capture_defaults_to_raw_bytes_and_records_projection_overrides(self) -> None:
        self._write("a.txt", "原始字节")
        self._write("b.txt", "元数据投影重建物")
        files, excluded, scan = self._capture(
            origin_by_relative={"b.txt": M.CONTENT_ORIGIN_METADATA_PROJECTION}
        )
        origins = {item.relative_path: item.content_origin for item in files}
        self.assertEqual(origins, {"a.txt": "RAW_BYTES", "b.txt": "METADATA_PROJECTION"})
        self.assertTrue(scan.clean)

    def test_unknown_origin_value_is_rejected(self) -> None:
        self._write("a.txt", "x")
        with self.assertRaises(ValidationError):
            self._capture(origin_by_relative={"a.txt": "MAYBE_BYTES"})

    def test_manifest_entries_carry_origin_and_digest_covers_it(self) -> None:
        self._write("a.txt", "内容")
        files, _excluded, scan = self._capture()
        staged = M.freeze_snapshot(files, os.path.join(self.root, "stage"))
        manifest = M.build_archive_manifest(scope="snapshot", scope_ref="snap-1", files=staged, secret_scan=scan)
        self.assertEqual(manifest["entries"][0]["content_origin"], "RAW_BYTES")
        self.assertEqual(manifest["content_origin_summary"]["RAW_BYTES"], 1)
        self.assertEqual(manifest["content_origin_summary"]["byte_level_verifiable"], 1)

        # 只有"来源声明"变化，也必须改变 manifest_digest（否则投影冒充字节级不会被发现）
        tampered = json.loads(json.dumps(manifest))
        tampered["entries"][0]["content_origin"] = "METADATA_PROJECTION"
        self.assertNotEqual(M.manifest_digest_of(tampered), manifest["manifest_digest"])
        self.assertFalse(M.validate_manifest(tampered)["ok"])

        # 而 archive_id 由内容快照派生，不受来源声明影响（内容没变 → 同一个归档身份）
        self.assertEqual(
            manifest["archive_id"],
            archive_id_for(
                scope="snapshot", scope_ref="snap-1", snapshot_digest=manifest["snapshot_digest"]
            ),
        )
        self.assertEqual(
            contract_build_manifest(scope="snapshot", scope_ref="snap-1", files=[item.to_local_file() for item in staged])["archive_id"],
            manifest["archive_id"],
        )

    def test_freeze_snapshot_rejects_file_changed_between_capture_and_freeze(self) -> None:
        path = self._write("a.txt", "版本 1")
        files, _excluded, _scan = self._capture()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("版本 2")
        with self.assertRaises(ValidationError) as ctx:
            M.freeze_snapshot(files, os.path.join(self.root, "stage"))
        self.assertEqual(ctx.exception.detail["relative_path"], "a.txt")

    def test_uploaded_bytes_come_from_the_frozen_copy(self) -> None:
        path = self._write("a.txt", "快照时的内容")
        files, _excluded, scan = self._capture()
        staged = M.freeze_snapshot(files, os.path.join(self.root, "stage"))
        manifest = M.build_archive_manifest(scope="snapshot", scope_ref="snap-2", files=staged, secret_scan=scan)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("上传途中被改写")
        self.assertTrue(M.verify_frozen_snapshot(staged)["ok"])
        with open(staged[0].absolute_path, "rb") as fh:
            frozen_bytes = fh.read()
        self.assertEqual(manifest["entries"][0]["sha256"], M.object_digest(frozen_bytes))

    # ------------------------------------------------------------------ 秘密拦截
    def test_secret_hit_blocks_manifest_build(self) -> None:
        self._write("config.txt", "api_key = AKIAIOSFODNN7EXAMPLE\n")
        files, _excluded, scan = self._capture()
        staged = M.freeze_snapshot(files, os.path.join(self.root, "stage"))
        with self.assertRaises(SecretDetected):
            M.build_archive_manifest(scope="snapshot", scope_ref="x", files=staged, secret_scan=scan)
        self.assertFalse(scan.clean)
        self.assertEqual(scan.hits[0]["path"], "config.txt")

    def test_manifest_without_scan_is_not_reported_clean(self) -> None:
        self._write("a.txt", "x")
        files, _excluded, _scan = self._capture()
        staged = M.freeze_snapshot(files, os.path.join(self.root, "stage"))
        manifest = M.build_archive_manifest(
            scope="snapshot", scope_ref="x", files=staged, secret_scan=None, require_clean_scan=False
        )
        # 没扫描过就不能写"扫描通过"：validate 会把它当问题报出来
        self.assertFalse(manifest["secret_scan"]["clean"])
        self.assertFalse(M.validate_manifest(manifest)["ok"])

    def test_credential_like_files_are_excluded_from_capture(self) -> None:
        self._write("a.txt", "正常资料")
        self._write("credentials", "password = topsecretpw\n")
        self._write("server.pem", "-----BEGIN PRIVATE KEY-----\n")
        files, excluded, _scan = self._capture()
        self.assertEqual([item.relative_path for item in files], ["a.txt"])
        self.assertIn("credentials", excluded)
        self.assertIn("server.pem", excluded)

    def test_policy_requires_user_approval(self) -> None:
        self._write("a.txt", "x")
        files, _excluded, scan = self._capture()
        staged = M.freeze_snapshot(files, os.path.join(self.root, "stage"))
        with self.assertRaises(ValidationError):
            M.build_archive_manifest(
                scope="snapshot", scope_ref="x", files=staged, policy=ArchivePolicy(policy_id="p", source_paths=["/x"], target_account="a", target_root="r"), secret_scan=scan
            )
        manifest = M.build_archive_manifest(
            scope="snapshot", scope_ref="x", files=staged, policy=_policy(), secret_scan=scan
        )
        self.assertEqual(manifest["policy"]["policy_id"], "pol_test")

    # ------------------------------------------------------------------ 校验与声明
    def test_validate_manifest_detects_missing_origin_and_tampering(self) -> None:
        self._write("a.txt", "x")
        files, _excluded, scan = self._capture()
        staged = M.freeze_snapshot(files, os.path.join(self.root, "stage"))
        manifest = M.build_archive_manifest(scope="snapshot", scope_ref="x", files=staged, secret_scan=scan)
        self.assertTrue(M.validate_manifest(manifest)["ok"])

        del manifest["entries"][0]["content_origin"]
        result = M.validate_manifest(manifest)
        self.assertFalse(result["ok"])
        self.assertIn("entry_without_content_origin", [item["problem"] for item in result["problems"]])

    def test_validate_manifest_rejects_path_traversal_entries(self) -> None:
        self._write("a.txt", "x")
        files, _excluded, scan = self._capture()
        staged = M.freeze_snapshot(files, os.path.join(self.root, "stage"))
        manifest = M.build_archive_manifest(scope="snapshot", scope_ref="x", files=staged, secret_scan=scan)
        manifest["entries"][0]["relative_path"] = "../escape.txt"
        problems = [item["problem"] for item in M.validate_manifest(manifest)["problems"]]
        self.assertIn("untrusted_relative_path", problems)

    def test_integrity_claim_splits_byte_level_and_projection(self) -> None:
        self._write("raw.txt", "原始")
        self._write("proj.txt", "投影")
        self._write("unknown.txt", "未声明")
        files, _excluded, scan = self._capture(
            origin_by_relative={
                "proj.txt": "METADATA_PROJECTION",
                "unknown.txt": None,
            }
        )
        staged = M.freeze_snapshot(files, os.path.join(self.root, "stage"))
        manifest = M.build_archive_manifest(scope="snapshot", scope_ref="x", files=staged, secret_scan=scan)
        claim = M.integrity_claim(manifest, [item.relative_path for item in staged])
        self.assertEqual(claim["byte_level_verified"], ["raw.txt"])
        self.assertEqual(claim["projection_only_verified"], ["proj.txt"])
        self.assertEqual(claim["undeclared_origin_verified"], ["unknown.txt"])
        self.assertEqual(claim["byte_level_count"], 1)
        self.assertEqual(claim["projection_only_count"], 2)
        self.assertIn("不得", claim["warning"])
        self.assertIn("仅为投影一致", claim["statement"])

    def test_manifest_object_digest_is_stable_for_same_content(self) -> None:
        self._write("a.txt", "稳定")
        files, _excluded, scan = self._capture()
        staged = M.freeze_snapshot(files, os.path.join(self.root, "stage"))
        first = M.build_archive_manifest(scope="snapshot", scope_ref="x", files=staged, secret_scan=scan)
        second = M.build_archive_manifest(scope="snapshot", scope_ref="x", files=staged, secret_scan=scan)
        self.assertEqual(first["manifest_digest"], second["manifest_digest"])
        self.assertEqual(M.manifest_bytes(first), M.manifest_bytes(second))
        self.assertEqual(M.object_digest(M.manifest_bytes(first)), M.object_digest(M.manifest_bytes(second)))

    def test_content_change_derives_new_archive_id(self) -> None:
        self._write("a.txt", "版本 1")
        files, _excluded, scan = self._capture()
        staged = M.freeze_snapshot(files, os.path.join(self.root, "stage"))
        first = M.build_archive_manifest(scope="snapshot", scope_ref="x", files=staged, secret_scan=scan)
        self._write("a.txt", "版本 2")
        files2, _excluded2, scan2 = self._capture()
        staged2 = M.freeze_snapshot(files2, os.path.join(self.root, "stage2"))
        second = M.build_archive_manifest(scope="snapshot", scope_ref="x", files=staged2, secret_scan=scan2)
        self.assertNotEqual(first["archive_id"], second["archive_id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
