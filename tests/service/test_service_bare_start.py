"""没有凭据也要能起来、能继续跑轮次（用户动作 U-1/U-2 还没做完时的真实状态）。

这一组回答一句很具体的话：**"现在什么都没有配，服务会怎么样？"**

* 缺配置 → 用安全默认值起，缺的每一路都要在健康快照里写明"这一路为什么没干活 + 下一步动作"；
* 缺邮箱路径（163 授权码还没填 / 没有适配器）→ 服务照常轮转，只是邮件那一路空转；
* 网盘没授权 → 归档如实记 `NOT_AUTHORIZED` 并继续，不假装成功、不崩、不退出；
* 控制台令牌文件不存在 → 自动生成 600（否则手机端连状态都看不到）。

判定标准不是"没抛异常"，而是**每一路都有明确的可观察结论**。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from typing import Any, Dict, List

from asistent.console.auth import POSIX_PERMISSIONS
from asistent.contracts.enums import CaseState
from asistent.service import config as config_module
from asistent.service.health import ServiceState
from asistent.service.phases import ARCHIVE_BLOCKING_CODES
from asistent.service.status_artifact import STATUS_ARTIFACT_NAME, status_artifact_id
from tests.service.support import (
    CLEAN_NOTICE_BODY,
    SECRET_MARKERS,
    ServiceWorkbench,
    find_phase,
    parse_round_report,
    read_text,
    store_is_sqlite,
    write_text,
)


class TestBareStart(unittest.TestCase):
    """没有配置文件、没有任何凭据时的启动与轮转。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mvp8-bare-", ignore_cleanup_errors=True)
        self.root = self._tmp.name
        self.bench = ServiceWorkbench(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------ 1
    def test_starts_without_any_config_file(self) -> None:
        """连配置文件都没有：用安全默认值跑完一轮，退出码 0，每一路都说明白为什么没干。"""
        config_path = os.path.join(self.bench.config_dir, "missing-service.json")
        self.assertFalse(os.path.exists(config_path))
        result = self.bench.run_cli("--once", "--json", "--config", config_path)
        self.assertEqual(result.returncode, 0, result.raw())
        report = parse_round_report(result.stdout)

        skipped = {item["name"]: item["skipped"] for item in report["phases"]}
        self.assertEqual(report["ok"], True, "缺配置不是故障：一轮要干净地跑完")
        self.assertTrue(skipped["sync"], "邮件这一路要说清楚为什么没干活")
        self.assertIn("mail", skipped["sync"].lower() + skipped["sync"])
        self.assertIn("未配置外发", skipped["outbound"])
        self.assertIn("归档未启用", skipped["archive"])

        # 快照：能看出"服务活着、跑过多少轮、正在等什么"，而不是一片空白
        status = self.bench.status(config_path)
        self.assertEqual(status["rounds_completed"], 1)
        self.assertEqual(status["rounds_failed"], 0)
        self.assertEqual(status["pending"]["cases_total"], 0)
        self.assertTrue(status["pending"], "待处理计数必须始终存在（手机端第一眼要看的）")
        self.assertEqual(status["waiting_on"], ["none"], "没有事务时如实说'没在等什么'")
        self.assertTrue(status["notes"], "缺什么要说清楚；notes 不能是空的")
        notes = " ".join(status["notes"])
        self.assertIn("mail", notes.lower() + notes)
        described = json.dumps(status, ensure_ascii=False)
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, described.lower())

        # 快照文件落在仓库外，且能被手机端下载到
        state_path = os.path.join(self.bench.state_dir, "service-state.json")
        snapshot_path = os.path.join(self.bench.state_dir, STATUS_ARTIFACT_NAME)
        self.assertTrue(os.path.isfile(state_path))
        self.assertTrue(os.path.isfile(snapshot_path), "对外快照文件必须真的写出来")
        artifact = status["status_artifact"]
        self.assertEqual(artifact["artifact_id"], status_artifact_id())
        self.assertEqual(artifact["snapshot_path"], snapshot_path)
        if store_is_sqlite():
            self.assertTrue(artifact["published"], "第一轮就要发布健康快照文件对象")
        else:
            # reference 是**内存**存储，而 `--status` 是新进程：跨进程读不回这份登记。
            # 这是后端差异（不是服务缺陷）：文件本体照样写出来了，sqlite 上才会永久登记。
            self.assertTrue(artifact["snapshot_exists"])
            self.assertFalse(artifact["published"])
        if store_is_sqlite():
            from asistent.storage.sqlite_store import open_store

            store = open_store(self.bench.db_path)
            try:
                row = store.get("FileArtifact", artifact["artifact_id"])
                self.assertEqual(row.name, STATUS_ARTIFACT_NAME)
                self.assertEqual(row.path, snapshot_path)
                self.assertTrue(row.sha256)
                self.assertEqual(row.case_id, "", "健康快照不属于任何事务（case_id 为空）")
            finally:
                store.close()
        self.assertTrue(any("api/artifacts" in line for line in status["status_artifact_access"]))
        # 对外快照里必须能看出"在等什么 / 每一路为什么没干活"
        content = json.loads(read_text(snapshot_path))
        self.assertIn("waiting_on", content)
        self.assertIn("pending", content)
        self.assertTrue(content["notes"])

    # ------------------------------------------------------------------ 2
    def test_missing_bin_file_and_token_are_created_not_fatal(self) -> None:
        """`~/.config` 之类目录不存在也要能起：令牌自动生成，权限 600，且不进日志。"""
        self.bench.default_config()
        token_path = os.path.join(self.root, "var", "nested", "deeper", "console-token")
        self.bench.write_config(
            "service-token.json",
            console={"enabled": True, "bind": "127.0.0.1", "port": 0, "token_path": token_path},
        )
        self.assertFalse(os.path.exists(token_path))
        result = self.bench.run_cli("--once", "--json", "--config", os.path.join(self.bench.config_dir, "service-token.json"))
        self.assertEqual(result.returncode, 0, result.raw())
        self.assertTrue(os.path.isfile(token_path), "首次启动必须自动生成令牌，否则手机端永远看不到状态")
        if os.name != "nt":
            mode = stat.S_IMODE(os.stat(token_path).st_mode)
            self.assertEqual(mode, 0o600, "令牌文件必须是 600，实际 %o" % mode)
        with open(token_path, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
        self.assertGreaterEqual(len(token), 16)
        self.assertNotIn(token, result.stdout, "令牌绝不能出现在 stdout/日志里")
        self.assertNotIn(token, result.stderr)

    # ------------------------------------------------------------------ 3
    def test_mailbox_without_a_channel_keeps_the_service_running(self) -> None:
        """邮箱通道还没配好（IMAP 无适配器 / 目录不存在）：如实记账，轮次继续。"""
        # (a) 明确写了要连真实邮箱，但本层没有适配器 → 说明白，不假装
        payload = self.bench.config_payload(mail={"enabled": True, "source": "imap", "account": "owner@example.com"})
        path = os.path.join(self.bench.config_dir, "service-imap.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        first = self.bench.run_cli("--once", "--json", "--config", path)
        self.assertEqual(first.returncode, 0, first.raw())
        report = parse_round_report(first.stdout)
        self.assertTrue(find_phase(report, "sync")["skipped"], "没有适配器就必须明说这一路没干活")
        status = self.bench.status(path)
        self.assertIn("mail", " ".join(status["notes"]).lower() + " ".join(status["notes"]))

        # (b) 夹具目录不存在（比如挂载还没好）：同步阶段报错、但**服务继续跑第二轮**
        broken = self.bench.write_config(
            "service-broken-mail.json",
            mail={"enabled": True, "source": "fixture", "fixture_dir": os.path.join(self.root, "no-such-mailbox")},
        )
        second = self.bench.run_cli("--once", "--json", "--config", broken)
        report2 = parse_round_report(second.stdout)
        sync2 = find_phase(report2, "sync")
        self.assertFalse(sync2["ok"], "读不到邮箱要如实报错")
        self.assertTrue(sync2["error_text"] or sync2["error_class"])
        self.assertNotEqual(second.returncode, 0, "这一轮有阶段失败：退出码要非 0（运维能发现）")
        # 关键：失败之后**没有**退出，第二轮照样跑（这正是"一个阶段失败不接管服务"）
        again = self.bench.run_cli("--once", "--json", "--config", broken)
        self.assertIsNotNone(again.returncode)
        status2 = self.bench.status(broken)
        self.assertGreaterEqual(status2["rounds_completed"], 2, "连续两轮都必须真的跑过")
        self.assertGreaterEqual(status2["phase_failures"].get("sync", 0), 2)
        self.assertIn("sync", str(status2["last_error"].get("phase", "")))
        self.assertTrue(status2["last_error"].get("text"))

    # ------------------------------------------------------------------ 4
    def _reach_archivable_state(self) -> str:
        """把一封通知真走到"业务上成功"（合法状态机路径），返回 case_id。"""
        from asistent.console.auth import TokenAuth
        from asistent.console.service import ConsoleService

        self.bench.add_notice("notice-01.eml", subject="周末科普讲座报名确认（含活动时间）")
        self.bench.default_config()
        self.bench.run_round()
        store = self.bench.open_store()
        try:
            draft = list(store.find("Draft", limit=None))[0]
            version = store.get("DraftVersion", draft.current_version_id)
            ConsoleService(store, TokenAuth("mvp8-test-token"), server_name="mvp8-test").submit_approval(
                {"draft_id": draft.id, "version_no": int(version.version_no), "confirm": True}
            )
            case_id = list(store.find("Case", limit=None))[0].id
        finally:
            store.close()
        self.bench.run_round(self.bench.sending_config())
        store = self.bench.open_store()
        try:
            self.assertEqual(str(store.get("Case", case_id).state), str(CaseState.SUCCEEDED), "前置没走到 SUCCEEDED")
        finally:
            store.close()
        return case_id

    def test_pan_refuses_to_write_without_authorization_or_approval(self) -> None:
        """网盘这一路的两道闸门：**没授权**不写、**没审批**也不写；两种都如实记账、服务照常活着。

        用桩通道跑（不依赖本机 bdpan 状态，也绝不碰真实网盘）：
        `require_authorized()` 抛 `PanNotAuthorized` / 写锁未解 → 断言 `put` 调用次数为 0。
        """
        from asistent.archive.engine import ArchiveEngine
        from asistent.pan.errors import PanNotAuthorized, PanUnavailable, PanWriteLocked
        from asistent.service.phases import RunContext, archive_phase

        class _StubPan:
            """只记录"有没有被要求写"，并按剧本拒绝。"""

            def __init__(self, *, authorized: bool, locked: bool) -> None:
                self.authorized = authorized
                self.writes_locked = locked
                self.put_attempts: List[str] = []

            def require_authorized(self):
                if not self.authorized:
                    raise PanNotAuthorized(
                        "bdpan 未授权（未登录）：拒绝执行归档写入",
                        detail={"next_step": "完成网盘授权后服务会自动重试"},
                    )
                return {"logged_in": True}

            def put(self, local_path: str, remote_path: str) -> Any:
                self.put_attempts.append(remote_path)
                if self.writes_locked:
                    raise PanWriteLocked(
                        "写入未解锁：需要审批记录", detail={"next_step": "拿到审批记录后 unlock_writes"}
                    )
                raise AssertionError("本测试不允许任何真实写入")

            def ensure_dir(self, *_a: Any, **_kw: Any) -> None:
                return None

            def stat(self, remote_path: str):
                return None  # 远端还没有这个对象（真实通道的常见答案）

            def lock_writes(self) -> None:
                self.writes_locked = True

        self._reach_archivable_state()
        store = self.bench.open_store()
        try:
            for authorized, locked, expected in (
                (False, True, "NOT_AUTHORIZED"),
                (True, True, "APPROVAL_REQUIRED"),
            ):
                pan = _StubPan(authorized=authorized, locked=locked)
                engine = ArchiveEngine(
                    pan,
                    state_dir=self.bench.staging_root,
                    store=store,
                    clock=store.now,
                    require_authorized=True,
                    actor_id="test",
                )
                config = config_module.load_config(os.path.join(self.bench.config_dir, "service.json"))
                context = RunContext(
                    config=config,
                    store=store,
                    state=ServiceState().begin(run_id="test", interval_seconds=1),
                    archive_engine=engine,
                )
                result = archive_phase(context)
                self.assertTrue(result.ok, "通道不可用是已知状态，不该把阶段判成故障：%s" % result.summary_line())
                entry = result.detail["archived"][0]
                self.assertEqual(entry["error_code"], expected)
                self.assertEqual(entry["verified"], 0)
                self.assertEqual(entry["uploaded"], 0, "闸门在写入之前：一个对象都不该上传")
                self.assertTrue(entry["next_step"], "必须给出'下一步动作'")
                self.assertNotEqual(entry["state"], "VERIFIED", "没写成功就不可能校验通过")
                if expected == "NOT_AUTHORIZED":
                    self.assertEqual(pan.put_attempts, [], "授权探测没过：连'想写'都不该发生")
                else:
                    self.assertTrue(pan.put_attempts, "第二道闸门是在 put 时拦下的")
        finally:
            store.close()

    def test_real_bdpan_client_refuses_before_starting_any_subprocess(self) -> None:
        """真通道的写锁是**先**于子进程的：未解锁时连一次 bdpan 都不会被拉起。

        用一个不存在的 binary：如果 `put` 真去跑 CLI，报的会是"没装"，而不是"未解锁"。
        """
        from asistent.pan.bdpan import BdpanCliPanClient
        from asistent.pan.errors import PanWriteLocked

        client = BdpanCliPanClient(binary=os.path.join(self.root, "no-such-bdpan-binary"))
        local = os.path.join(self.root, "payload.bin")
        with open(local, "wb") as handle:
            handle.write(b"x" * 32)
        self.assertTrue(client.writes_locked, "默认必须是锁住的：没审批就不许写")
        with self.assertRaises(PanWriteLocked):
            client.put(local, "个人助手归档/archive:test/object.bin")

    # ------------------------------------------------------------------ 4b
    def test_bdpan_channel_keeps_the_round_alive_on_this_machine(self) -> None:
        """真通道（bdpan）在本机无论是什么状态，服务都必须照常跑完一轮并如实记账。"""
        self._reach_archivable_state()
        pan_config = self.bench.write_config(
            "service-bdpan.json",
            archive={"enabled": True, "channel": "bdpan", "root": "个人助手归档"},
        )
        result = self.bench.run_cli("--once", "--json", "--config", pan_config)
        report = parse_round_report(result.stdout)
        archive = find_phase(report, "archive")
        self.assertTrue(archive["ok"], "归档通道的任何已知拒绝都不该让整轮变成故障：%s" % archive)
        entry = archive["detail"]["archived"][0]
        self.assertIn(entry["error_code"], ARCHIVE_BLOCKING_CODES)
        self.assertEqual(entry["verified"], 0, "没有真正回读校验过就不能报 verified")
        self.assertEqual(entry["uploaded"], 0, "闸门在写入之前：一个对象都不该上传")
        self.assertTrue(entry["next_step"])
        status = self.bench.status(pan_config)
        self.assertIn("pan", status["waiting_on"], "快照要体现出'在等网盘'（用户动作 U-2）")
        store = self.bench.open_store()
        try:
            case = list(store.find("Case", limit=None))[0]
            self.assertEqual(str(case.state), str(CaseState.SUCCEEDED))
            self.assertNotEqual(str(case.archive_state), "VERIFIED")
        finally:
            store.close()

    # ------------------------------------------------------------------ 5
    def test_state_file_survives_garbage(self) -> None:
        """state 文件被写坏（掉电）时，服务必须照样起来，而不是拒绝启动。"""
        self.bench.default_config()
        state_path = os.path.join(self.bench.state_dir, "service-state.json")
        os.makedirs(self.bench.state_dir, exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as handle:
            handle.write('{"rounds_completed": 12, "pending": ')  # 被截断的 JSON
        loaded = ServiceState.load(state_path)
        self.assertEqual(loaded.rounds_completed, 0)
        self.assertTrue(loaded.notes, "读不出来要留一条说明")
        result = self.bench.run_cli("--once", "--json", "--config", self.bench.default_config())
        self.assertEqual(result.returncode, 0, result.raw())
        status = self.bench.status(self.bench.default_config())
        self.assertEqual(status["rounds_completed"], 1)

    # ------------------------------------------------------------------ 6 令牌坏掉只降级控制台
    def test_a_rejected_token_degrades_the_console_instead_of_killing_the_service(self) -> None:
        """令牌被拒（这里用"太短"这个跨平台都成立的拒绝理由）→ **服务必须照常启动**。

        这条钉的是设备上抓到的真缺陷（2026-10-12，board，Python 3.10.12）：
        `console.enabled=true` 且令牌不可用时，`TokenAuth("")` 抛错把**整个服务**挡在启动之外，
        设备上一跑就是 9 项红（开发机上两个后端全绿——因为 Windows 不做 POSIX 权限校验）。

        为什么这是**产品缺陷而不是测试缺陷**：令牌文件权限不对/被截断，是最常见的人为状态
        （用编辑器或 `scp` 放一个 644 文件、拷贝时少一行）。把这个状态升级成
        "24h 助手完全不启动"，是最坏的失败方式——`systemd Restart=always` 会把它变成无限重启循环。
        服务本体的职责是继续干活（收邮件、理解、审批、归档），控制台只是窗口：
        **窗口坏了要说明白，而不是把房子拆了。**
        """
        short_token = os.path.join(self.bench.config_dir, "short-token")
        write_text(short_token, "abc\n")  # 低于 MIN_TOKEN_LENGTH → 任何平台都必须被拒绝
        os.chmod(short_token, 0o600)
        config_path = self.bench.write_config(
            "service-short-token.json", console={"enabled": True, "token_path": short_token}
        )

        result = self.bench.run_cli("--once", "--json", "--config", config_path)
        self.assertEqual(
            result.returncode,
            0,
            "令牌不可用不得阻止服务启动（它只该让手机端打不开）：\n" + result.raw(),
        )
        report = parse_round_report(result.stdout)
        self.assertEqual(report["ok"], True, "控制台降级不该把整轮弄成故障")

        status = self.bench.status(config_path)
        notes = " ".join(status["notes"])
        self.assertTrue(
            "控制台" in notes and ("令牌" in notes or "token" in notes.lower()),
            "降级必须**看得见**（健康快照里要写清为什么手机端打不开），不能安静地少一个功能：" + notes,
        )

    # ------------------------------------------------------------------ 7
    @unittest.skipUnless(POSIX_PERMISSIONS, "文件模式校验只在 POSIX 上生效，Windows 看不到这条路径")
    def test_posix_directory_mode_rejection_also_only_degrades_the_console(self) -> None:
        """POSIX：令牌文件是 600，但它**所在目录**是 755 → 令牌被合法拒绝 → 服务仍然起来。

        这条是设备上那 9 项红的原始触发条件（目录不是 700，Windows 不校验模式所以本机复现不了）。
        """
        permissive_dir = os.path.join(self.root, "permissive-config")
        os.makedirs(permissive_dir, exist_ok=True)
        os.chmod(permissive_dir, 0o755)
        token_path = os.path.join(permissive_dir, "console-token")
        write_text(token_path, "t" * 48 + "\n")
        os.chmod(token_path, 0o600)
        config_path = self.bench.write_config(
            "service-permissive-dir.json", console={"enabled": True, "token_path": token_path}
        )

        result = self.bench.run_cli("--once", "--json", "--config", config_path)
        self.assertEqual(result.returncode, 0, result.raw())
        status = self.bench.status(config_path)
        notes = " ".join(status["notes"])
        self.assertIn("权限", notes, "拒绝的理由（目录权限过宽）必须原样说清楚：" + notes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
