"""取：生成文件的元数据与传输，失败可重试（MVP-4 §847、SPEC §9.3）。

关键点：

* 下载**必须**限制在白名单目录内（空白名单 = 一律拒绝），不跟随符号链接；
* 一个文件失败只影响它自己：别的文件照样能取（不能一个坏文件拖垮整个列表）；
* 失败要**记账**（`transfer_failures`）并返回可读原因，让手机端给"重试"按钮；
* 成功也记账（`download_count`），审计里可观察。
"""

from __future__ import annotations

import hashlib
import os
import unittest

from asistent.contracts.objects import FileArtifact

from .helpers import ConsoleTestCase


class ArtifactTests(ConsoleTestCase):
    """文件元数据与下载。"""

    #: 只有本测试类允许下载（白名单在 setUp 里指向临时目录）
    artifact_roots = ()

    def setUp(self) -> None:
        super().setUp()
        _doc, self.report, self.case = self.process_fixture("01-notice-multi-time.eml")
        self.export_dir = os.path.join(self.tmpdir, "exports")
        os.makedirs(self.export_dir, exist_ok=True)
        self.content = "报名确认：11 月 2 日 09:00 开始。\n（合成文件，非真实信息）\n".encode("utf-8")
        self.file_path = os.path.join(self.export_dir, "confirmation.txt")
        with open(self.file_path, "wb") as handle:
            handle.write(self.content)
        self.sha256 = hashlib.sha256(self.content).hexdigest()
        self.artifact = self._make_artifact(self.file_path, name="confirmation.txt")
        # 重新起一个带白名单的服务（模拟部署时显式配置可下载目录）
        self.client, self.server = self.start_server(artifact_roots=[self.export_dir])

    def _make_artifact(self, path: str, *, name: str = "", case_id: str = "", **overrides):
        payload = {
            "id": overrides.pop("id", "file_" + os.path.basename(path).replace(".", "-")),
            "case_id": case_id or self.case.id,
            "path": path,
            "name": name or os.path.basename(path),
            "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
            "sha256": self.sha256,
            "content_type": "text/plain; charset=utf-8",
            "version": 1,
            "generated_at": self.store.now(),
            "permission": "owner_read",
            "links": {"case": [case_id or self.case.id]},
        }
        payload.update(overrides)
        return self.store.put(FileArtifact(**payload))

    def test_list_artifacts_shows_metadata(self):
        recorded = self.client.get("/api/artifacts?case_id=%s" % self.case.id)
        self.assertStatus(200, recorded)
        self.assertEqual(1, recorded.json["total"])
        row = recorded.json["artifacts"][0]
        self.assertEqual(self.artifact.id, row["id"])
        self.assertEqual("confirmation.txt", row["name"])
        self.assertEqual(len(self.content), row["size_bytes"])
        self.assertEqual(self.sha256, row["sha256"])
        self.assertEqual(1, row["version"])
        self.assertTrue(row["generated_at"])
        self.assertEqual("owner_read", row["permission"])
        self.assertEqual(0, row["download_count"])
        self.assertTokenAbsent(recorded)

    def test_artifact_detail_and_missing(self):
        recorded = self.client.get("/api/artifacts/%s" % self.artifact.id)
        self.assertStatus(200, recorded)
        self.assertEqual(self.artifact.id, recorded.json["id"])
        self.assertTrue(recorded.json["available"])
        self.assertStatus(404, self.client.get("/api/artifacts/file_nope"))

    def test_download_returns_bytes_with_identity_headers(self):
        recorded = self.client.get("/api/artifacts/%s/content" % self.artifact.id)
        self.assertStatus(200, recorded)
        self.assertEqual(self.content.decode("utf-8"), recorded.response_body)
        self.assertEqual("text/plain; charset=utf-8", recorded.response_headers.get("Content-Type"))
        self.assertEqual('attachment; filename="confirmation.txt"', recorded.response_headers.get("Content-Disposition"))
        self.assertEqual(self.artifact.id, recorded.response_headers.get("X-Artifact-Id"))
        self.assertEqual(self.sha256, recorded.response_headers.get("X-Artifact-Sha256"))
        # 响应头里不能带服务端路径
        self.assertNotIn(self.export_dir, str(recorded.response_headers))

    def test_download_is_counted_and_audited(self):
        self.client.get("/api/artifacts/%s/content" % self.artifact.id)
        self.client.get("/api/artifacts/%s/content" % self.artifact.id)
        stored = self.store.get("FileArtifact", self.artifact.id)
        self.assertEqual(2, stored.download_count)
        trail = [entry for entry in self.store.audit_trail(limit=50) if entry.object_id == self.artifact.id]
        self.assertTrue(trail, "取用要留审计")
        self.assertTrue(any(entry.action == "READ" for entry in trail))

    def test_download_of_missing_file_is_404_and_counted_as_failure(self):
        self._make_artifact(os.path.join(self.export_dir, "ghost.txt"), id="file_ghost")
        recorded = self.client.get("/api/artifacts/file_ghost/content")
        self.assertStatus(404, recorded)
        self.assertIn("可稍后重试", recorded.json["error"]["message"])
        self.assertEqual(1, int(self.store.get("FileArtifact", "file_ghost").transfer_failures))
        # 失败的文件会出现在待办快照的"传输失败"里
        pending = self.client.get("/api/overview").json["pending"]
        self.assertIn("file_ghost", pending["artifact_ids"])
        self.assertEqual(1, pending["counts"]["failed_transfers"])

    def test_one_failure_does_not_break_the_others(self):
        good = self._make_artifact(self.file_path, name="good.txt", id="file_good")
        self._make_artifact(os.path.join(self.export_dir, "ghost.txt"), name="ghost.txt", id="file_ghost")
        broken = self.client.get("/api/artifacts/file_ghost/content")
        self.assertStatus(404, broken)
        ok = self.client.get("/api/artifacts/%s/content" % good.id)
        self.assertStatus(200, ok)
        listed = self.client.get("/api/artifacts").json
        self.assertEqual(3, listed["total"], "一个文件失败不应影响列表")

    def test_retry_succeeds_after_file_appears(self):
        """失败可重试：文件补上之后，同一个 artifact 再取就成功。"""
        path = os.path.join(self.export_dir, "late.txt")
        self._make_artifact(path, name="late.txt", id="file_late")
        self.assertStatus(404, self.client.get("/api/artifacts/file_late/content"))
        with open(path, "wb") as handle:
            handle.write(b"late but fine\n")
        retried = self.client.get("/api/artifacts/file_late/content")
        self.assertStatus(200, retried)
        self.assertEqual("late but fine\n", retried.response_body)
        self.assertEqual(1, int(self.store.get("FileArtifact", "file_late").transfer_failures))
        self.assertEqual(1, int(self.store.get("FileArtifact", "file_late").download_count))

    def test_path_outside_allowlist_is_refused(self):
        outside = os.path.join(self.tmpdir, "secret.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("不该被取走")
        self._make_artifact(outside, name="secret.txt", id="file_outside")
        recorded = self.client.get("/api/artifacts/file_outside/content")
        self.assertStatus(404, recorded)
        self.assertNotIn("secret.txt", recorded.response_body.replace("file_outside", ""))

    def test_symlink_escape_is_refused(self):
        link = os.path.join(self.export_dir, "escape.txt")
        outside = os.path.join(self.tmpdir, "outside.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("外面")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows 无权限时跳过
            self.skipTest("当前环境不支持创建符号链接")
        self._make_artifact(link, name="escape.txt", id="file_escape")
        recorded = self.client.get("/api/artifacts/file_escape/content")
        self.assertStatus(404, recorded)
        self.assertNotIn("外面", recorded.response_body)

    def test_empty_allowlist_refuses_every_download(self):
        """没配置白名单时一律拒绝：宁可"取不到"也不猜路径。"""
        client, _server = self.start_server(artifact_roots=[])
        recorded = client.get("/api/artifacts/%s/content" % self.artifact.id)
        self.assertStatus(404, recorded)
        self.assertIn("未配置可下载目录白名单", recorded.json["error"]["message"])

    def test_artifact_without_path_is_refused(self):
        artifact = self._make_artifact(self.file_path, name="no-path.bin", id="file_no_path")
        artifact.path = ""
        self.store.put(artifact, expected_revision=artifact.revision, actor_kind="SYSTEM", actor_id="test", reason="测试用")
        recorded = self.client.get("/api/artifacts/file_no_path/content")
        self.assertStatus(404, recorded)
        self.assertIn("没有 path", recorded.json["error"]["message"])


__all__ = ["ArtifactTests"]
