"""A-5「构建即发布」测试（`docs/REQUIREMENTS-ADDENDUM.md` A-5）。

要求逐条对应：
1. 快照 → manifest → 上传 → 逐文件哈希回读校验 → 记录归档状态：一次调用完成，产物可查；
2. 可重复执行 / 幂等：同一 `archive_id` 重跑**零上传**；内容变化派生新 id；
3. 部分失败可见且可重试：失败的只是那几个对象（**计数断言**），其余复用；
4. 结果未知先 stat/回读，不盲目重传；
5. `--dry-run` 离线可跑：打包 + manifest + 上传回路全在本地目录里验证，不接触真实网盘；
6. 未授权 / 未获审批 → 显式状态 + 明确退出码，且**零写入**；
7. 构建日志里不得出现凭据。

测试里的 git 调用全是**只读**的（`rev-parse` / `status` / `archive` / `bundle create` 输出到临时目录），
不执行任何 git 写命令。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from asistent.archive import release as release_mod  # noqa: E402
from asistent.archive import capture_source_tree, freeze_snapshot, build_archive_manifest  # noqa: E402
from asistent.archive.release import (  # noqa: E402
    EXIT_CODES,
    ReleasePlan,
    build_release_snapshot,
    exit_code_for,
    main,
    publish_release,
    run_release,
)
from asistent.archive.manifest import validate_manifest  # noqa: E402
from asistent.contracts.pan import LocalDirPanClient, PanPutResult  # noqa: E402
from asistent.pan import BdpanCliPanClient  # noqa: E402
from tests.archive.support import FakeBdpanEnv  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _in_git_worktree(repo_root: str) -> bool:
    """只读探测：当前目录是不是 git 工作树（`git rev-parse --git-dir`，不写任何东西）。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


class FaultyLocalPan(LocalDirPanClient):
    """离线故障注入：按目标路径让 put 失败一次（用于验证"只补传失败对象"）。"""

    def __init__(self, root: str, *, fail_targets: Optional[List[str]] = None) -> None:
        super().__init__(root)
        self.fail_targets = list(fail_targets or [])
        self.put_calls: List[str] = []

    def put(self, local_path: str, remote_path: str, **kwargs: Any) -> Any:  # type: ignore[override]
        self.put_calls.append(remote_path)
        for target in list(self.fail_targets):
            if remote_path.endswith(target):
                self.fail_targets.remove(target)
                return PanPutResult(
                    ok=False,
                    remote_path=remote_path,
                    local_path=local_path,
                    result_class="FAILED_SAFE",
                    error="注入的确定性失败（离线）",
                )
        return super().put(local_path, remote_path, **kwargs)


class ReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        if not _in_git_worktree(REPO_ROOT):
            # 非 git 工作树（例如只拷贝了源码树的设备目录）：跳过依赖 git 打包的用例。
            # "打包 + manifest + 回读校验"这条链由 ReleaseWithoutGitTest 覆盖（不需要 git）。
            raise unittest.SkipTest("当前目录不是 git 工作树：跳过依赖 git 打包的发布用例")
        self.root = tempfile.mkdtemp(prefix="archive-release-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.out_dir = os.path.join(self.root, "out")
        self.state_dir = os.path.join(self.root, "state")
        self.mirror = os.path.join(self.root, "mirror")
        self.plan = build_release_snapshot(repo_root=REPO_ROOT, out_dir=self.out_dir, label="mvp7-test")

    # ------------------------------------------------------------------ 1. 产物与 manifest
    def test_snapshot_produces_source_bundle_and_manifest(self) -> None:
        kinds = sorted(item["kind"] for item in self.plan.artifacts)
        self.assertEqual(kinds, ["git_bundle", "source_tarball"])
        self.assertEqual(self.plan.label, "mvp7-test")
        self.assertEqual(len(self.plan.commit or ""), 40)
        self.assertEqual(self.plan.code_version, self.plan.commit)
        for item in self.plan.artifacts:
            self.assertTrue(os.path.isfile(item["path"]))
            self.assertGreater(item["size_bytes"], 0)
            self.assertEqual(len(item["sha256"]), 64)

        manifest = self.plan.manifest
        names = sorted(entry["relative_path"] for entry in manifest["entries"])
        self.assertEqual(names, ["mvp7-test-src.tar.gz", "mvp7-test.git.bundle"])
        self.assertEqual(manifest["content_origin_summary"]["RAW_BYTES"], 2)
        self.assertEqual(manifest["content_origin_summary"]["byte_level_verifiable"], 2)
        self.assertEqual(manifest["code_version"], self.plan.commit)
        self.assertEqual(manifest["release"]["commit"], self.plan.commit)
        self.assertEqual(manifest["release"]["label"], "mvp7-test")
        self.assertTrue(validate_manifest(manifest)["ok"])
        self.assertTrue(manifest["secret_scan"]["clean"])
        # 发布产物是"我们自己生成的字节" → 每个条目都能做字节级校验
        for entry in manifest["entries"]:
            self.assertEqual(entry["content_origin"], "RAW_BYTES")
            self.assertEqual(len(entry["sha256"]), 64)

    def test_git_bundle_failure_blocks_release_with_config_error(self) -> None:
        original = release_mod._run_git

        def fake_git(repo_root: str, args, *, timeout: int = 600):  # type: ignore[no-untyped-def]
            if args and args[0] == "bundle":
                return 1, "", "fatal: bundle 失败"
            return original(repo_root, args, timeout=timeout)

        with mock.patch.object(release_mod, "_run_git", side_effect=fake_git):
            plan, result = run_release(
                repo_root=REPO_ROOT, out_dir=self.out_dir, label="mvp7-bundlefail", state_dir=self.state_dir
            )
        self.assertTrue(plan.git_errors)
        self.assertEqual(result.state, "CONFIG_ERROR")
        self.assertEqual(result.exit_code, EXIT_CODES["CONFIG_ERROR"])

    # ------------------------------------------------------------------ 2/5. dry-run 离线闭环 + 幂等
    def test_dry_run_completes_offline_and_is_idempotent(self) -> None:
        first = publish_release(self.plan, dry_run=True, state_dir=self.state_dir, mirror_dir=self.mirror)
        self.assertEqual(first.state, "VERIFIED")
        self.assertEqual(first.exit_code, 0)
        self.assertTrue(first.dry_run)
        self.assertEqual(sorted(first.verified), ["mvp7-test-src.tar.gz", "mvp7-test.git.bundle"])
        # 两个产物 + manifest + 完成标记
        self.assertEqual(first.transfers, 4)
        self.assertEqual(first.integrity_claim["byte_level_count"], 2)
        self.assertTrue(any("不接触真实网盘" in note for note in first.notes))
        self.assertTrue(os.path.isfile(first.state_record_path or ""))

        second = publish_release(self.plan, dry_run=True, state_dir=self.state_dir, mirror_dir=self.mirror)
        self.assertEqual(second.state, "VERIFIED")
        self.assertEqual(second.transfers, 0, "同一 archive_id 重跑必须零上传")
        self.assertEqual(sorted(second.reused), ["mvp7-test-src.tar.gz", "mvp7-test.git.bundle"])

    def test_dry_run_is_reachable_through_the_cli(self) -> None:
        code = main(
            [
                "--repo",
                REPO_ROOT,
                "--out",
                self.out_dir,
                "--label",
                "mvp7-cli",
                "--dry-run",
                "--mirror",
                self.mirror,
                "--state-dir",
                self.state_dir,
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        # dry-run 的 CLI 全程不接触真实网盘：镜像目录里能看到归档回路留下的字节
        self.assertTrue(os.path.isdir(self.mirror))

    def test_cli_reports_partial_with_exit_code_2(self) -> None:
        plan = build_release_snapshot(repo_root=REPO_ROOT, out_dir=self.out_dir, label="mvp7-cli-partial")
        faulty = FaultyLocalPan(self.mirror, fail_targets=["objects/mvp7-cli-partial.git.bundle"])
        result = publish_release(plan, faulty, state_dir=self.state_dir)
        self.assertEqual(result.state, "PARTIAL")
        self.assertEqual(result.exit_code, EXIT_CODES["PARTIAL"])
        self.assertEqual(
            [item["relative_path"] for item in result.failed], ["mvp7-cli-partial.git.bundle"]
        )

    # ------------------------------------------------------------------ 3. 部分失败只补传失败对象
    def test_dry_run_partial_failure_retries_only_failed_objects(self) -> None:
        faulty = FaultyLocalPan(self.mirror, fail_targets=["objects/mvp7-test.git.bundle"])
        first = publish_release(self.plan, faulty, state_dir=self.state_dir)
        self.assertEqual(first.state, "PARTIAL")
        self.assertEqual(len(first.verified), 1)
        self.assertEqual(len(first.failed), 1)
        self.assertEqual(len(first.unknown_objects), 0, "确定性失败不应记成未知")

        object_puts = [item for item in faulty.put_calls if "/objects/" in item]
        self.assertEqual(len(object_puts), 2, "第一次尝试会尝试上传两个产物")
        faulty.put_calls.clear()
        second = publish_release(self.plan, faulty, state_dir=self.state_dir)
        self.assertEqual(second.state, "VERIFIED")
        object_puts = [item for item in faulty.put_calls if "/objects/" in item]
        self.assertEqual(len(object_puts), 1, f"重试只补传失败的 1 个对象，实际 {object_puts}")
        self.assertTrue(object_puts[0].endswith("mvp7-test.git.bundle"))
        self.assertEqual(second.uploaded, ["mvp7-test.git.bundle"])
        self.assertEqual(second.reused, ["mvp7-test-src.tar.gz"])

    # ------------------------------------------------------------------ 4. 结果未知
    def test_unknown_result_is_resolved_by_readback_not_blind_retry(self) -> None:
        class UnknownOnce(FaultyLocalPan):
            def __init__(self, root: str) -> None:
                super().__init__(root)
                self.unknown_budget = 1

            def put(self, local_path: str, remote_path: str, **kwargs: Any):  # type: ignore[override]
                self.put_calls.append(remote_path)
                result = super().put(local_path, remote_path, **kwargs)
                if self.unknown_budget > 0 and remote_path.endswith(".git.bundle"):
                    self.unknown_budget -= 1
                    result.result_class = "UNKNOWN"
                    result.error = "注入的未知结果（离线）"
                return result

        pan = UnknownOnce(self.mirror)
        first = publish_release(self.plan, pan, state_dir=self.state_dir)
        self.assertEqual(first.state, "PARTIAL")
        self.assertEqual(first.unknown_objects, ["mvp7-test.git.bundle"])
        pan.put_calls.clear()
        second = publish_release(self.plan, pan, state_dir=self.state_dir)
        self.assertEqual(second.state, "VERIFIED")
        self.assertEqual(
            [item for item in pan.put_calls if "/objects/" in item],
            [],
            "远端内容已一致：重跑必须先 stat/回读判定，不得盲目重传",
        )

    # ------------------------------------------------------------------ 6. 未授权 / 未获审批
    def test_write_lock_and_not_authorized_are_explicit_with_zero_writes(self) -> None:
        env = FakeBdpanEnv(self)
        locked = BdpanCliPanClient(binary=env.binary, root_prefix="asistent", timeout=20)
        result = publish_release(self.plan, locked, state_dir=self.state_dir)
        self.assertEqual(result.state, "APPROVAL_REQUIRED")
        self.assertEqual(result.exit_code, 3)
        self.assertIn("--approval-ref", result.next_step or "")
        self.assertEqual(env.calls_of("upload"), [], "未获审批时不得发起写操作")

        env.set_spec({"logged_in": False})
        unlocked_no_auth = BdpanCliPanClient(binary=env.binary, root_prefix="asistent", timeout=20)
        result = publish_release(
            self.plan, unlocked_no_auth, approval_ref="appr_a5_test", state_dir=self.state_dir
        )
        self.assertEqual(result.state, "NOT_AUTHORIZED")
        self.assertEqual(result.exit_code, EXIT_CODES["NOT_AUTHORIZED"])
        self.assertIn("待用户授权", result.next_step or "")
        self.assertEqual(env.calls_of("upload"), [], "未授权时一次写操作都不能发")

    def test_exit_code_mapping_is_total(self) -> None:
        self.assertEqual(exit_code_for("VERIFIED"), 0)
        self.assertEqual(exit_code_for("PARTIAL"), 2)
        self.assertEqual(exit_code_for("PENDING"), 2)
        self.assertEqual(exit_code_for("FAILED", error_code="NOT_AUTHORIZED"), 3)
        self.assertEqual(exit_code_for("INTEGRITY_ERROR"), 4)
        self.assertEqual(exit_code_for("CONFIG_ERROR"), 4)

    # ------------------------------------------------------------------ 7. 凭据纪律
    def test_release_output_never_contains_credentials(self) -> None:
        result = publish_release(self.plan, dry_run=True, state_dir=self.state_dir, mirror_dir=self.mirror)
        rendered = json.dumps(result.to_dict(), ensure_ascii=False) + json.dumps(
            self.plan.to_dict(), ensure_ascii=False
        )
        for forbidden in ("BDUSS", "bduss", "cookie", "config.json", ".config/bdpan"):
            self.assertNotIn(forbidden, rendered)
        self.assertIn("而不是凭据", release_mod.__doc__ or "")

    def test_state_record_is_json_with_plan_and_outcome(self) -> None:
        result = publish_release(self.plan, dry_run=True, state_dir=self.state_dir, mirror_dir=self.mirror)
        payload = json.loads(open(result.state_record_path or "", encoding="utf-8").read())  # type: ignore[arg-type]
        self.assertEqual(payload["schema_version"], release_mod.RELEASE_STATE_SCHEMA)
        self.assertEqual(payload["state"], "VERIFIED")
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["release"]["label"], "mvp7-test")
        self.assertEqual(payload["outcome"]["state"], "VERIFIED")
        self.assertEqual(payload["outcome"]["archive_id"], result.archive_id)
        self.assertEqual(payload["release"]["manifest_digest"], self.plan.manifest["manifest_digest"])
        self.assertTrue(payload["recorded_at"])


class ReleaseWithoutGitTest(unittest.TestCase):
    """不依赖 git 的打包链验证：没有 git 的机器上，"打包 + manifest + 回读校验"也必须成立。

    `scripts/release.sh --dry-run` 里的 git 只负责产出两种产物（源码包 + 完整历史）；
    这里用 `tarfile` 造出等价产物，证明 manifest 与逐文件回读校验**不依赖 git**。
    """

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="archive-release-nogit-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state_dir = os.path.join(self.root, "state")

    def test_packaging_and_manifest_work_without_git(self) -> None:
        import tarfile

        artifacts_dir = os.path.join(self.root, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)
        src = os.path.join(artifacts_dir, "no-git-src.tar.gz")
        with tarfile.open(src, "w:gz") as tar:
            tar.add(os.path.join(REPO_ROOT, "asistent"), arcname="asistent")
        bundle_like = os.path.join(artifacts_dir, "no-git-history.bundle")
        with open(bundle_like, "w", encoding="utf-8") as fh:
            fh.write("bundle-like-bytes\n")

        files, _excluded, scan = capture_source_tree(
            artifacts_dir, include_relative=["no-git-src.tar.gz", "no-git-history.bundle"]
        )
        staged = freeze_snapshot(files, os.path.join(self.root, "stage-nogit"))
        manifest = build_archive_manifest(
            scope="snapshot", scope_ref="no-git", files=staged, secret_scan=scan
        )
        self.assertTrue(validate_manifest(manifest)["ok"])
        plan = ReleasePlan(
            label="no-git",
            repo_root=REPO_ROOT,
            out_dir=artifacts_dir,
            stage_dir=os.path.join(self.root, "stage-nogit"),
            archive_id=manifest["archive_id"],
            manifest=manifest,
            files=staged,
            code_version="no-git-runner",
            artifacts=[{"name": "no-git-src.tar.gz"}, {"name": "no-git-history.bundle"}],
        )
        result = publish_release(
            plan, dry_run=True, state_dir=self.state_dir, mirror_dir=os.path.join(self.root, "mirror-nogit")
        )
        self.assertEqual(result.state, "VERIFIED")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(sorted(result.verified), ["no-git-history.bundle", "no-git-src.tar.gz"])
        self.assertEqual(result.transfers, 4)
        # 幂等：同一 archive_id 重跑零上传（离线可验证）
        again = publish_release(
            plan, dry_run=True, state_dir=self.state_dir, mirror_dir=os.path.join(self.root, "mirror-nogit")
        )
        self.assertEqual(again.state, "VERIFIED")
        self.assertEqual(again.transfers, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
