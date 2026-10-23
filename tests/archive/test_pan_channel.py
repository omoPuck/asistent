"""`asistent/pan` 真通道适配器测试（在**假 bdpan CLI 子进程**上跑真实通道代码）。

覆盖点（对应 docs/mvp/mvp-7.md 的验收行）：
- 写锁：未解锁的 put/delete/ensure_dir 一律 `APPROVAL_REQUIRED`，且**一次 CLI 都没发**；
- 未授权：CLI 未登录 / `errno=-6` → `NOT_AUTHORIZED`（带"待用户授权"下一步），不是静默跳过；
- 结果未知：超时 → `PanUnavailable`（`EXTERNAL_RESULT_UNKNOWN`），绝不当作成功；
- 通道不采用百度侧 md5：`PanStat.content_hash is None`，强制走下载回读；
- 只用标准库、绝对路径调用、`--no-check-update` 一定在 argv 里、凭据不进 argv；
- 路径穿越拒绝；`quota()` 不编造数字。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from asistent.contracts.errors import ValidationError  # noqa: E402
from asistent.contracts.pan import PanClient  # noqa: E402
from asistent.pan import (  # noqa: E402
    BdpanCliPanClient,
    PanNotAuthorized,
    PanUnavailable,
    PanWriteLocked,
    probe_bdpan,
)
from asistent.pan.bdpan import NOT_AUTHORIZED_MARKERS, normalize_remote  # noqa: E402
from tests.archive.support import FakeBdpanEnv  # noqa: E402


class PanChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = FakeBdpanEnv(self)
        self.addCleanup(self.env.cleanup)

    def _client(self, **kwargs) -> BdpanCliPanClient:
        client = BdpanCliPanClient(binary=self.env.binary, root_prefix="asistent", timeout=kwargs.pop("timeout", 20), **kwargs)
        return client

    # ---------------------------------------------------------------- 协议与闸门
    def test_adapter_satisfies_frozen_pan_client_protocol(self) -> None:
        client = self._client()
        self.assertIsInstance(client, PanClient)
        self.assertEqual(client.name, "bdpan_cli")

    def test_writes_are_locked_until_approved_and_no_cli_call_is_made(self) -> None:
        client = self._client()
        local = self.env.make_local_file("payload.txt", "hello")
        for call, exc_type in (
            (lambda: client.put(local, "objects/payload.txt"), PanWriteLocked),
            (lambda: client.delete("objects/payload.txt"), PanWriteLocked),
            (lambda: client.ensure_dir("objects"), PanWriteLocked),
        ):
            with self.subTest(call=call):
                with self.assertRaises(exc_type) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, "APPROVAL_REQUIRED")
                self.assertIn("approval_ref", json.dumps(ctx.exception.detail, ensure_ascii=False))
        self.assertEqual(client.call_log, [], "未获批准时不应发出任何 CLI 调用")

    def test_unlock_requires_real_approval_ref(self) -> None:
        client = self._client()
        with self.assertRaises(ValidationError):
            client.unlock_writes("   ")
        client.unlock_writes("appr_20261010_1")
        self.assertFalse(client.writes_locked)
        self.assertEqual(client.approval_ref, "appr_20261010_1")
        client.lock_writes()
        self.assertTrue(client.writes_locked)

    # ---------------------------------------------------------------- 只读探测 / 未授权
    def test_readonly_probe_reports_not_authorized_explicitly(self) -> None:
        self.env.set_spec({"logged_in": False})
        client = self._client()
        probe = client.probe()
        self.assertTrue(probe.installed)
        self.assertFalse(probe.logged_in)
        payload = probe.to_dict()
        self.assertEqual(payload["auth_state"], "NOT_AUTHORIZED")
        self.assertIn("bdpan login", payload["next_step"])

    def test_require_authorized_raises_not_authorized_without_any_write(self) -> None:
        self.env.set_spec({"logged_in": False})
        client = self._client()
        client.unlock_writes("appr_1")
        with self.assertRaises(PanNotAuthorized) as ctx:
            client.require_authorized()
        self.assertEqual(ctx.exception.code, "NOT_AUTHORIZED")
        self.assertIn("待用户授权", ctx.exception.detail["next_step"])
        self.assertEqual([entry for entry in client.call_log if entry[0] in ("upload", "rm", "mkdir")], [])

    def test_put_returns_not_authorized_when_token_expired(self) -> None:
        self.env.set_spec({"logged_in": True, "unauthorized_error": True})
        client = self._client()
        client.unlock_writes("appr_2")
        local = self.env.make_local_file("payload.txt", "hello")
        with self.assertRaises(PanNotAuthorized) as ctx:
            client.put(local, "objects/payload.txt")
        self.assertEqual(ctx.exception.code, "NOT_AUTHORIZED")
        self.assertEqual(ctx.exception.failure_class.name, "PERMISSION_FAILURE")

    def test_probe_of_missing_binary_reports_not_installed(self) -> None:
        probe = probe_bdpan(binary=os.path.join(self.env.root, "definitely-missing-bdpan"))
        self.assertFalse(probe.installed)
        self.assertFalse(probe.logged_in)
        self.assertIn("bdpan", probe.detail)

    # ---------------------------------------------------------------- 正常读写
    def test_put_get_stat_list_delete_roundtrip(self) -> None:
        client = self._client()
        client.unlock_writes("appr_3")
        local = self.env.make_local_file("payload.txt", "内容-中文")
        result = client.put(local, "objects/payload.txt")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.result_class, "SUCCEEDED")
        self.assertTrue(result.remote_path.endswith("asistent/objects/payload.txt"))

        stat = client.stat("objects/payload.txt")
        self.assertIsNotNone(stat)
        assert stat is not None
        self.assertEqual(stat.size_bytes, os.path.getsize(local))
        # 百度侧 md5 不可信 → 通道必须留空，逼上层走下载回读
        self.assertIsNone(stat.content_hash)
        self.assertTrue(stat.raw["md5_present"])

        listing = client.list("objects")
        self.assertEqual([os.path.basename(item.remote_path) for item in listing], ["payload.txt"])

        target = os.path.join(self.env.root, "downloaded.bin")
        download = client.get("objects/payload.txt", target)
        self.assertTrue(download.ok)
        self.assertEqual(download.content_hash, result.content_hash)

        self.assertTrue(client.delete("objects/payload.txt"))
        self.assertIsNone(client.stat("objects/payload.txt"))
        self.assertFalse(client.delete("objects/payload.txt"))

    def test_upload_is_not_treated_as_success_without_readback(self) -> None:
        """上传返回成功但远端字节被改写 → 通道不撒谎，回读的哈希与本地不同。"""
        self.env.set_spec({"faults": {"objects/payload.txt": {"upload": "corrupt"}}})
        client = self._client()
        client.unlock_writes("appr_4")
        local = self.env.make_local_file("payload.txt", "原始内容")
        result = client.put(local, "objects/payload.txt")
        self.assertTrue(result.ok)
        target = os.path.join(self.env.root, "readback.bin")
        download = client.get("objects/payload.txt", target)
        self.assertNotEqual(download.content_hash, result.content_hash)

    def test_stat_returns_none_for_missing_object(self) -> None:
        client = self._client()
        self.assertIsNone(client.stat("objects/not-there.txt"))

    def test_ensure_dir_tolerates_missing_mkdir_subcommand(self) -> None:
        self.env.set_spec({"mkdir_supported": False})
        client = self._client()
        client.unlock_writes("appr_5")
        client.ensure_dir("objects")
        self.assertTrue(any("mkdir" in note for note in client.notes), client.notes)

    def test_quota_is_reported_unsupported_instead_of_fabricated(self) -> None:
        client = self._client()
        quota = client.quota()
        self.assertFalse(quota["supported"])
        self.assertIn("不得把未知配额当 0", quota["note"])

    # ---------------------------------------------------------------- 结果未知
    def test_timeout_is_unknown_not_failure(self) -> None:
        self.env.set_spec(
            {"timeout_seconds": 5, "faults": {"objects/timeout.txt": {"upload": "timeout"}}}
        )
        client = self._client(timeout=1)
        client.unlock_writes("appr_6")
        local = self.env.make_local_file("payload.txt", "超时用")
        with self.assertRaises(PanUnavailable) as ctx:
            client.put(local, "objects/timeout.txt")
        self.assertEqual(ctx.exception.code, "EXTERNAL_RESULT_UNKNOWN")
        self.assertTrue(ctx.exception.detail["must_decide_by_stat"])
        # 副作用可能已经发生：远端文件确实被写出来了（所以上层必须先 stat 判定）
        self.assertTrue(os.path.isfile(self.env.remote_path("asistent/objects/timeout.txt")))

    # ---------------------------------------------------------------- argv / 路径纪律
    def test_absolute_binary_and_no_check_update_are_always_used(self) -> None:
        self.env.set_spec({})
        client = self._client()
        client.list("objects")
        self.assertTrue(client.call_log, "应当至少有 ls 调用")
        for argv in client.call_log:
            self.assertEqual(argv[0], self.env.binary, "必须用解析出的绝对路径调用，不依赖 PATH")
            self.assertIn("--no-check-update", argv)
            self.assertIn("--json", argv)
        # 子进程侧的真实 argv 同样带 flag（证明 flag 真的传到了子进程）
        child_argv = self.env.argv_log()
        self.assertTrue(child_argv)
        self.assertTrue(all("--no-check-update" in entry for entry in child_argv))

    def test_credentials_never_appear_in_argv_or_detail(self) -> None:
        secret = "BDUSS=REAL-SECRET-COOKIE-VALUE-0123456789"
        self.env.set_spec({"logged_in": False})
        client = self._client()
        client.unlock_writes(secret)  # 审批引用被当作不透明字符串
        local = self.env.make_local_file("payload.txt", "x")
        with self.assertRaises(PanNotAuthorized) as ctx:
            client.put(local, "objects/payload.txt")
        rendered = json.dumps(ctx.exception.detail, ensure_ascii=False) + json.dumps(
            self.env.argv_log(), ensure_ascii=False
        )
        self.assertNotIn(secret, rendered)

    def test_path_traversal_is_rejected(self) -> None:
        client = self._client()
        client.unlock_writes("appr_7")
        local = self.env.make_local_file("payload.txt", "x")
        for bad in ("../escape.txt", "a/../../b.txt", "/", "  ", "..\\windows"):
            with self.subTest(bad=bad):
                with self.assertRaises((ValidationError, ValueError)):
                    client.put(local, bad)
        self.assertEqual(client.call_log, [])

    def test_normalize_remote_collapses_and_rejects(self) -> None:
        self.assertEqual(normalize_remote("/a//b/./c.txt"), "a/b/c.txt")
        self.assertEqual(normalize_remote("a\\b.txt"), "a/b.txt")
        with self.assertRaises(ValidationError):
            normalize_remote("a/../b")

    def test_not_authorized_markers_cover_device_wording(self) -> None:
        # 设备实测过的真实措辞必须被识别为未授权
        for wording in ("未登录", "登录已过期", "errno=-6"):
            self.assertIn(wording, NOT_AUTHORIZED_MARKERS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
