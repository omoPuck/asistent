"""常驻运行时：一轮 + 循环 + 优雅退出 + 控制台线程（SPEC §4.1、§14.2、§21.7）。

`ServiceRuntime` 只做三件事：

1. **接线**：按 `ServiceConfig` 把 MVP-1/2/3/5/6/7 的引擎装起来（缺哪个能力就少接哪个，
   并且**明确**记录"为什么没接"，绝不用假对象顶替）；
2. **跑一轮**：调用 `phases.run_phases()` —— 与 `--once` 是同一份实现；
3. **守住**：跨轮次保持同一份存储连接与内存状态，按间隔重复；`SIGTERM`/`SIGINT`
   只置停止标志，**当前这一轮跑完**才退出（不把半截状态留在库里）。

`--once` 与常驻循环的唯一区别是循环次数（`max_rounds=1` vs 0）。因此
"分轮驱动得到的证据"和"24 小时常驻得到的证据"说的是同一段代码，不是两套实现。

进程内存里**没有**任何业务游标：每轮要处理什么全部现场从存储查（`case_needs_analysis`、
`snapshot_pending`、`_consumed_answer_ids`），所以被 kill 之后重新起来，
接着干的还是同一件没干完的事。
"""

from __future__ import annotations

import hashlib
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..actions import MailCallLog, ScriptedMailSender
from ..actions.engine import ActionEngine
from ..archive.engine import ArchiveEngine
from ..console.auth import TokenAuth
from ..console.http_api import build_server
from ..console.service import ConsoleService
from ..console.settings import ConsoleSettings
from ..contracts.enums import ActorKind
from ..contracts.ids import new_id
from ..contracts.mail import FixtureMailSource, RecordingMailSender
from ..contracts.timeutil import utcnow_iso
from ..index.fts import open_index_db
from ..index.pipeline import SearchPipeline
from ..mail.sync import MailSyncEngine, RawStore, SyncStateStore
from .config import (
    ARCHIVE_CHANNEL_LOCAL,
    CHANNEL_NONE,
    ServiceConfig,
    load_config,
)
from .health import ServiceState, snapshot_pending, infer_waiting_on
from .mail_source import build_mcp_mail_source
from .phases import ARCHIVE_BLOCKING_CODES, RoundReport, RunContext, run_phases
from .status_artifact import (
    describe_snapshot_access,
    publish_status_artifact,
    snapshot_path_for,
    status_artifact_id,
    write_status_snapshot,
)


def open_service_store(config: ServiceConfig) -> Any:
    """按 `config.store` 打开存储。`reference` = 内存参考实现（离线调试/单轮验证）。"""
    kind = str(config.store or "sqlite").strip().lower()
    if kind in ("reference", "ref", "memory"):
        from ..contracts.reference import ReferenceStore

        return ReferenceStore()
    if kind in ("sqlite", "sqlite3", "file"):
        from ..storage.sqlite_store import open_store

        # 库文件的**父目录**由服务自己保证存在。
        # sqlite 会自己创建库文件，但不会创建目录；少了目录就抛
        # `sqlite3.OperationalError: unable to open database file`——一句既不说是哪个目录、
        # 也不说该怎么办的报错。对一个 24h 常驻服务来说，"数据目录没建"不该等于"起不来"，
        # 何况本机的测试夹具恰好都会先把目录建好，这个坑只会在真机部署时露出来（2026-10-12 实测）。
        parent = os.path.dirname(os.path.abspath(config.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        return open_store(config.db_path)
    raise ValueError("未知存储实现：%s（只支持 reference / sqlite）" % kind)


def _make_sender(config: ServiceConfig, log_path: str) -> Any:
    """构造受控外发通道。

    只有两种值，且都**不是**真实网络写操作：

    * `scripted` —— 剧本型假通道，每次 `send()` 落一行账本（`MailCallLog`），
      用于离线回路与"恰好一次"取证；
    * `local_outbox` —— 只写本地发件箱文件，绝不外发（`RecordingMailSender`）。

    真实通道（真实 IMAP/SMTP/MCP）属于 U-1/U-2 的用户动作，本层**没有**默认值可用，
    也绝不"顺手"接一个。
    """
    channel = str(config.outbound.channel or CHANNEL_NONE).strip().lower()
    if channel == "scripted":
        return ScriptedMailSender(log=MailCallLog(log_path))
    if channel == "local_outbox":
        return RecordingMailSender(os.path.join(config.var_root, "outbox"))
    if channel == CHANNEL_NONE:
        return None
    raise ValueError("不支持的外发通道：%r" % channel)


#: 控制台绑定失败后的重试间隔（秒）。
#:
#: 为什么需要重试：板子的地址是 DHCP 租来的，服务可能**先于网卡地址就绪**而启动，
#: 此时 `bind()` 报 `OSError: [Errno 99] Cannot assign requested address`。
#: 2026-10-12 就发生过：只在循环开始前试一次，失败后再没有第二次，控制台静默缺席 11 天。
CONSOLE_RETRY_SECONDS = 30.0


@dataclass
class ConsoleHandle:
    """控制台线程（手机端读状态、答题、审批的入口）。"""

    server: Any = None
    thread: Any = None
    settings: Any = None
    error: str = ""
    bound_host: str = ""
    bound_port: int = 0

    @property
    def url(self) -> str:
        if not self.bound_host:
            return ""
        return "http://%s:%d" % (self.bound_host, self.bound_port)

    def stop(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.server.server_close()
            except Exception:  # noqa: BLE001
                pass
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None


class ServiceRuntime:
    """一台"真的在守着"的助手：`run_once()` 是唯一的一轮实现。"""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        store: Any = None,
        clock: Callable[[], str] = utcnow_iso,
        log: Optional[Callable[[str], None]] = None,
        sender: Any = None,
        start_console: bool = False,
    ) -> None:
        self.config = config
        self.clock = clock
        self._log = log or (lambda message: None)
        self.store = store if store is not None else open_service_store(config)
        self.run_id = new_id("Execution", token=None)
        # 健康快照**先从仓库外的 state 文件恢复**：重启（哪怕换进程）之后手机端看到的
        # "累计跑了多少轮 / 最近一轮 / 最近错误 / 待处理"必须接着以前，而不是从零开始。
        # 注意：这里恢复的只是**展示用计数**；"下一轮该干什么"永远从存储里现算（`run_phases`）。
        previous = ServiceState.load(config.state_file())
        self.state = ServiceState().begin(
            run_id=self.run_id,
            interval_seconds=config.interval_seconds,
            state_path=config.state_file(),
        )
        if previous.rounds_completed or previous.rounds_failed or previous.last_round_at:
            self.state.rounds_completed = previous.rounds_completed
            self.state.rounds_failed = previous.rounds_failed
            self.state.consecutive_failures = previous.consecutive_failures
            self.state.phase_failures = dict(previous.phase_failures)
            self.state.last_round = dict(previous.last_round)
            self.state.last_round_at = previous.last_round_at
            self.state.last_sync = dict(previous.last_sync)
            self.state.last_ingest = dict(previous.last_ingest)
            self.state.last_recovery = dict(previous.last_recovery)
            self.state.last_error = dict(previous.last_error)
            self.state.notes.append(
                "重启恢复：从 %s 接续历史计数（累计完成 %d 轮 / 失败 %d 轮，上一轮 %s）"
                % (config.state_file(), previous.rounds_completed, previous.rounds_failed, previous.last_round_at or "-")
            )
        self.state.notes.extend(self._wiring_notes)
        self._stop = threading.Event()
        self._round_index = 0
        self.console = ConsoleHandle()
        #: 下一次允许重试控制台绑定的时刻（monotonic 秒）；见 CONSOLE_RETRY_SECONDS
        self._console_retry_at = 0.0
        #: 最近一次健康快照发布结果（手机端可下载的 `FileArtifact`）
        self.status_artifact: Dict[str, Any] = {
            "published": False,
            "artifact_id": status_artifact_id(),
            "reason": "尚未发布",
            "sha256": "",
            "size_bytes": 0,
            "version": 0,
        }

        # ---- 索引（FTS5 独立库；与进程同生共死）
        self.index_connection = open_index_db(config.ingest.index_path)
        self.pipeline = SearchPipeline(
            self.store,
            self.index_connection,
            snapshot_root=config.raw_root,
            max_bytes=config.ingest.max_bytes,
        )

        # ---- 邮件同步（通道缺失就**不接**，不是接个假的）
        self.mail_engine = None
        #: 真实通道下这是 `McpMailSource`（持有子进程），关闭时要显式收掉
        self.mail_source: Any = None
        if config.mail.enabled and config.mail.source and config.mail.source != "none":
            source: Any = None
            if config.mail.source in ("fixture", "fixture_eml"):
                # 契约自带的离线源（CP-20261010-32 已在契约层修好头字段解码，
                # 服务层不再需要自己的绕行实现）
                source = FixtureMailSource(
                    config.mail.fixture_dir, account=config.mail.account or "owner@example.com"
                )
            elif config.mail.source in ("mcp", "mcp_stdio", "imap"):
                source, why = build_mcp_mail_source(config)
                if source is None:
                    # 用户动作没做完是**正常状态**，不是故障：如实记账、继续常驻。
                    self.state.notes.append(why)
            else:
                self.state.notes.append(
                    "mail.source=%s 没有可用适配器（只支持 fixture/mcp）：本层不接假通道"
                    % config.mail.source
                )
            if source is not None:
                self.mail_source = source
                self.mail_engine = MailSyncEngine(
                    self.store,
                    source,
                    account=config.mail.account or getattr(source, "account", "") or "owner@example.com",
                    state_store=SyncStateStore(config.mail.state_path),
                    raw_store=RawStore(config.mail.raw_root),
                    folder=config.mail.folder,
                    worker="service-mail-sync",
                    max_attempts=config.mail.max_attempts,
                    create_conversation=config.mail.create_conversation,
                    source_system="mail",
                )

        # ---- 外发
        outbound_log = os.path.join(config.state_dir, "outbound-calls.jsonl")
        self.state.outbound_log_path = outbound_log
        self.sender = sender if sender is not None else _make_sender(config, outbound_log)
        self.action_engine = (
            ActionEngine(self.store, sender=self.sender, node=config.outbound.node)
            if self.sender is not None
            else None
        )

        # ---- 归档
        self.archive_engine = self._build_archive_engine()

        self._ctx = RunContext(
            config=config,
            store=self.store,
            state=self.state,
            clock=self.clock,
            mail_engine=self.mail_engine,
            pipeline=self.pipeline,
            action_engine=self.action_engine,
            archive_engine=self.archive_engine,
            channel_ready=bool(self.sender is not None and config.outbound.channel != CHANNEL_NONE),
            actor_id="service",
            index_connection=self.index_connection,
        )

        # ---- 控制台（手机/电脑都是同一个 HTTP 接口；令牌只从仓库外的 600 文件读）
        # 令牌不可用时**只降级控制台，绝不让服务本体起不来**：`TokenAuth("")` 会抛错，
        # 而"没配令牌 / 令牌文件权限不对"是最常见的人为状态，把它升级成"整个 24h 助手不启动"
        # 是最坏的失败方式——systemd 的 Restart=always 会把它变成无限重启循环。
        #
        # 实测（设备 board，Python 3.10.12，2026-10-12）：令牌文件 644、或它所在目录不是 700 时，
        # POSIX 权限校验会拒绝加载令牌；Windows 不做这项校验，所以**这个缺陷只在设备上暴露**——
        # 开发机上 reference 与 sqlite 两个后端逐包全绿，设备上一跑就是 9 项红。
        # 这正是 SPEC 把设备定为权威目标的意义：跨平台的失败模式必须在设备上验，不能靠本机推断。
        self.token_created = False
        self.token_error = ""
        self.token = self.load_console_token(config) if config.console.enabled else ""
        self.console_service = None
        if not config.console.enabled:
            self.state.notes.append("console.enabled=false：不启动控制台（手机端暂时看不到状态）")
        elif self.token:
            self.console_service = ConsoleService(
                self.store,
                TokenAuth(self.token),
                server_name="asistent-service",
                clock=self.clock,
                artifact_roots=tuple(config.console.artifact_roots) or (config.raw_root,),
            )
        else:
            # 降级必须**看得见**：进日志、进健康快照、进 notes，而不是安静地少一个功能。
            self.token_error = (
                "控制台已启用但令牌不可用（%s）：控制台降级——不监听任何端口；"
                "邮件/理解/审批/归档照常运行。按提示修好令牌文件后重启即可恢复手机端。" % self.token_error
            )
            self.console.error = self.token_error
            self.state.notes.append(self.token_error)
        self._console_requested = start_console

    # ------------------------------------------------------------------ 接线说明
    @property
    def _wiring_notes(self) -> List[str]:
        notes: List[str] = []
        if not self.config.mail.enabled:
            notes.append("mail.enabled=false：不连接邮箱（默认关闭，避免误连真实账号）")
        if not self.config.ingest.enabled:
            notes.append("ingest.enabled=false：不扫描挂载目录（只索引邮件原文）")
        if self.config.outbound.channel == CHANNEL_NONE:
            notes.append("outbound.channel=none：拒绝外呼；已授权动作留在 AUTHORIZED 等通道配置")
        if not self.config.archive.enabled:
            notes.append("archive.enabled=false：不做归档（默认关闭）")
        elif self.config.archive.channel == ARCHIVE_CHANNEL_LOCAL:
            notes.append("archive.channel=local_dir：本地目录模拟远端，**不代表**真实网盘能力")
        elif self.config.archive.channel == "bdpan":
            notes.append("archive.channel=bdpan：真实通道；未授权时必须返回 NOT_AUTHORIZED（用户动作 U-2）")
        return notes

    def load_console_token(self, config: ServiceConfig) -> str:
        """读（必要时生成）控制台令牌文件。

        令牌**只**存在于仓库外的 600 文件里：不打印、不进 argv、不进日志、不进快照。
        首次启动且文件缺失时自动生成一个 32 字节随机令牌（`os.urandom`），
        否则手机端连看都看不了（§877 证据要求能在控制台看到待处理/审批状态）。
        """
        from ..console.auth import load_token

        path = config.console.token_path
        if not path:
            self.token_error = "未配置控制台令牌路径（console.token_path 为空）"
            self.state.notes.append("未配置控制台令牌路径：控制台读接口不可用")
            return ""
        if os.path.isfile(path):
            try:
                token = load_token(path)
                self.token_created = False
                return token
            except Exception as exc:  # noqa: BLE001
                self.token_error = "令牌文件被拒绝：%s" % exc
                self.state.notes.append("控制台令牌不可用（%s）：控制台读接口将全部 401" % exc)
                return ""
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:  # pragma: no cover - Windows
                pass
        token = hashlib.sha256(os.urandom(32)).hexdigest()
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(token + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - Windows
            pass
        self.token_created = True
        self.state.notes.append("已生成控制台令牌文件（内容不打印）：%s" % path)
        return token

    def _build_archive_engine(self) -> Optional[ArchiveEngine]:
        config = self.config
        if not config.archive.enabled or config.archive.channel == "none":
            return None
        pan: Any = None
        if config.archive.channel == ARCHIVE_CHANNEL_LOCAL:
            from ..contracts.pan import LocalDirPanClient

            os.makedirs(config.archive.root, exist_ok=True)
            pan = LocalDirPanClient(config.archive.root)
        elif config.archive.channel == "bdpan":
            from ..pan.bdpan import BdpanCliPanClient

            # 远端目录前缀用配置里的值；没配才沿用客户端默认（`asistent`）。
            # 以前这里**不传参**，于是 `archive.root` 被静默忽略：配置里写的是"个人助手归档"，
            # 实际却归档到 `/apps/bdpan/asistent/` 下面——配置与事实不一致，而且没有任何提示。
            if config.archive.root:
                pan = BdpanCliPanClient(root_prefix=config.archive.root)
            else:
                pan = BdpanCliPanClient()
        if pan is None:
            return None
        return ArchiveEngine(
            pan,
            state_dir=config.archive.staging_root,
            store=self.store,
            clock=self.clock,
            require_authorized=True,
            actor_id="service",
        )

    # ------------------------------------------------------------------ 一轮
    def run_once(self) -> RoundReport:
        self._round_index += 1
        report = run_phases(self._ctx, round_index=self._round_index, run_id=self.run_id)
        # 顺序要紧：**先把这一轮的结果落盘、再把健康快照发布出去，最后才打印"这轮跑完了"**。
        # 反过来的话，日志里已经写着"第 2 轮完成"，手机端下载到的还是第 1 轮的快照
        # （取证时两边数字对不上，是最容易被误判成"服务在撒谎"的一种）。
        self.state.save()
        self._publish_status_snapshot(report)
        for line in report.evidence_lines():
            self._log(line)
        self._log(
            "[round %d] %s | %s"
            % (self._round_index, report.summary_line(), self.state.last_error.get("text", "") or "-")
        )
        return report

    def _publish_status_snapshot(self, report: RoundReport) -> None:
        """写一份给手机端看的健康快照，并登记成可下载的文件对象（失败不影响本轮结论）。"""
        snapshot_path = snapshot_path_for(self.state.state_path)
        try:
            payload = self.status()
            payload["snapshot_of"] = self.state.state_path
            written = write_status_snapshot(snapshot_path, payload)
            outcome = publish_status_artifact(
                self.store,
                state_path=written["path"],
                clock=self.clock,
                actor_id="service-console",
                protect=[self.token] if self.token else (),
            )
        except Exception as exc:  # noqa: BLE001 - 快照发布失败绝不能接管服务
            self.state.notes.append("健康快照发布失败：%s: %s" % (type(exc).__name__, exc))
            return
        outcome["snapshot_path"] = written["path"]
        self.status_artifact = outcome
        report.notes.append(
            "健康快照：%s（artifact=%s version=%s）"
            % ("已更新" if outcome["published"] else "未更新:" + str(outcome["reason"]), outcome["artifact_id"], outcome["version"])
        )

    # ------------------------------------------------------------------ 循环
    def request_stop(self, reason: str = "") -> None:
        if not self._stop.is_set():
            self._stop.set()
            self._log("[service] 收到停止请求（%s）：本轮结束后退出" % (reason or "stop"))

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def install_signal_handlers(self) -> Dict[str, Any]:
        """SIGTERM/SIGINT → 停止标志（**不**在当前轮中间中断）。"""
        previous: Dict[str, Any] = {}

        def handler(signum: int, _frame: Any) -> None:
            name = signal.Signals(signum).name
            self.request_stop(name)

        for name in ("SIGTERM", "SIGINT"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous[name] = signal.getsignal(signum)
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover - 非主线程
                previous.pop(name, None)
        return previous

    def start_console(self) -> ConsoleHandle:
        if self.console.server is not None:
            return self.console
        if not self.config.console.enabled:
            self.console.error = "控制台未启用（console.enabled=false）：不监听任何端口"
            self._log("[service] 控制台未启用（console.enabled=false）：跳过")
            return self.console
        if self.console_service is None:
            self.console.error = self.token_error or "控制台服务对象未构建"
            self._log("[service] %s" % self.console.error)
            if self.token_error:
                self.state.notes.append("手机端不可用：%s" % self.console.error)
            return self.console
        settings = ConsoleSettings(
            store=self.store,
            token=self.token,
            bind=self.config.console.bind,
            port=self.config.console.port,
            token_path=self.config.console.token_path,
            max_body_bytes=self.config.console.max_body_bytes,
            allow_remote=self.config.console.allow_remote,
            server_name="asistent-service",
            artifact_roots=tuple(self.config.console.artifact_roots) or (self.config.raw_root,),
        )
        try:
            # 必须把服务的日志函数接给控制台：否则请求日志（含 401/429、节流触发）全是空操作。
            # 单测里 helpers 自己传了 sink 所以是绿的，但部署环境里"有人在爆破我"在
            # service.log 里完全看不见 —— 2026-10-23 在设备上实测发现并修掉。
            server = build_server(settings, self.console_service, log_sink=self._log)
        except Exception as exc:  # noqa: BLE001 - 控制台起不来不该拖垮服务本体
            self.console.error = "%s: %s" % (type(exc).__name__, exc)
            self._note_console_failure("控制台启动失败：%s" % self.console.error)
            return self.console
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        thread.start()
        self.console.server = server
        self.console.thread = thread
        self.console.settings = settings
        self.console.bound_host = getattr(server, "bound_host", self.config.console.bind)
        self.console.bound_port = int(getattr(server, "bound_port", self.config.console.port))
        return self.console

    def _note_console_failure(self, text: str) -> None:
        """同一条启动失败只保留一条 note。

        重试是常态，若每次都追加，`notes` 会在几小时内被同一句话刷爆，反而把别的
        接线说明挤没了。这里原地更新最近一条同类 note。
        """
        for index in range(len(self.state.notes) - 1, -1, -1):
            if self.state.notes[index].startswith("控制台启动失败："):
                self.state.notes[index] = text
                return
        self.state.notes.append(text)

    def maybe_retry_console(self, *, now: Optional[float] = None) -> bool:
        """控制台没起来就隔 `CONSOLE_RETRY_SECONDS` 再试；本次真的拉起返回 True。

        只有"请求过控制台、还没成功、且配置里启用"时才重试；成功的控制台不会被重建。
        失败本身不抛异常——控制台只是窗口，不能让它把服务本体（收信/理解/审批/归档）拖死。
        """
        if not self._console_requested or self.console.server is not None:
            return False
        if not self.config.console.enabled:
            return False
        stamp = time.monotonic() if now is None else float(now)
        if stamp < self._console_retry_at:
            return False
        self._console_retry_at = stamp + CONSOLE_RETRY_SECONDS
        previous = self.console.error
        handle = self.start_console()
        if not handle.url:
            return False
        self._log("[service] 控制台延迟启动成功 %s（此前：%s）" % (handle.url, previous or "未启动"))
        self.state.notes.append(
            "控制台延迟启动成功：%s（此前失败：%s；已自动恢复，无需重启服务）" % (handle.url, previous or "未知")
        )
        return True

    def run_loop(
        self,
        *,
        max_rounds: int = 0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> List[RoundReport]:
        """按间隔重复跑轮；`max_rounds>0` 时跑满即退出（`--once` 就是 `max_rounds=1`）。"""
        reports: List[RoundReport] = []
        if self._console_requested:
            handle = self.start_console()
            if handle.url:
                self._log("[service] 控制台监听 %s（手机端用同一地址 + 令牌）" % handle.url)
        while not self._stop.is_set():
            # 控制台可能是"晚到"的（网卡地址、端口占用）：每轮开头补一次重试机会，
            # 而不是只认启动那一瞬间的结果。
            self.maybe_retry_console()
            reports.append(self.run_once())
            if max_rounds and len(reports) >= max_rounds:
                break
            if self._stop.is_set():
                break
            # 分段睡眠：收到停止信号最多 1 秒内响应，且不打断正在跑的轮次
            remaining = float(self.config.interval_seconds)
            while remaining > 0 and not self._stop.is_set():
                step = min(1.0, remaining)
                sleep(step)
                remaining -= step
        self.console.stop()
        self.state.running = False
        self.state.save()
        return reports

    # ------------------------------------------------------------------ 观察 / 关闭
    def status(self) -> Dict[str, Any]:
        pending = snapshot_pending(self.store)
        payload = self.state.to_dict()
        payload["pending"] = pending
        last_archive = self._last_archive_summary()
        payload["last_archive"] = last_archive
        payload["waiting_on"] = infer_waiting_on(
            pending,
            channel_ready=bool(self.sender is not None and self.config.outbound.channel != CHANNEL_NONE),
            archive_blocked=bool(last_archive.get("waiting_for_user")),
        )
        payload["pid"] = os.getpid()
        payload["config"] = self.config.describe()
        payload["console_url"] = self.console.url
        payload["store_class"] = type(self.store).__name__
        payload["status_artifact"] = self._status_artifact_view()
        payload["status_artifact_access"] = describe_snapshot_access(payload["status_artifact"]["artifact_id"])
        if self.action_engine is not None:
            payload["sender_call_count"] = int(self.action_engine.sender_call_count())
        log_path = getattr(self.state, "outbound_log_path", "")
        if log_path:
            payload["submit_count_on_disk"] = MailCallLog.submitted_count_on_disk(log_path)
        return payload

    def _status_artifact_view(self) -> Dict[str, Any]:
        """健康快照文件对象的**从存储读回来**的现状（`--status` 是新进程，内存里没有值）。"""
        artifact_id = status_artifact_id()
        snapshot_path = snapshot_path_for(self.state.state_path)
        view: Dict[str, Any] = {
            "artifact_id": artifact_id,
            "published": False,
            "version": 0,
            "sha256": "",
            "size_bytes": 0,
            "state_path": self.state.state_path,
            "snapshot_path": snapshot_path,
            "snapshot_exists": os.path.isfile(snapshot_path),
            "reason": "尚未发布（本轮还没跑过）",
        }
        try:
            row = self.store.get("FileArtifact", artifact_id)
        except Exception:  # noqa: BLE001 - 没发布过就是没有这一行
            return view
        view["published"] = True
        view["version"] = int(row.version or 1)
        view["sha256"] = str(row.sha256 or "")
        view["size_bytes"] = int(row.size_bytes or 0)
        view["name"] = str(row.name or "")
        view["path"] = str(row.path or "")
        view["reason"] = "已发布：手机端可直接下载这份快照"
        return view

    def _last_archive_summary(self) -> Dict[str, Any]:
        """上一轮归档的结论（从快照里的 `last_round` 读，不靠进程内存）。

        用来回答"归档这一路现在到底卡在哪"：没授权/没审批/没安装都属于**要等用户动手**，
        手机端必须一眼看到，而不是把它当成一次普通失败。
        """
        summary: Dict[str, Any] = {"state": "", "error_code": "", "next_step": "", "waiting_for_user": False}
        last_round = dict(self.state.last_round or {})
        for phase in list(last_round.get("phases") or []):
            if str(phase.get("name") or "") != "archive":
                continue
            detail = dict(phase.get("detail") or {})
            entries = [item for item in (detail.get("archived") or []) if isinstance(item, dict)]
            if not entries:
                return summary
            entry = entries[0]
            summary["state"] = str(entry.get("state") or "")
            summary["error_code"] = str(entry.get("error_code") or "")
            summary["next_step"] = str(entry.get("next_step") or "")
            summary["case_id"] = str(entry.get("case_id") or "")
            summary["waiting_for_user"] = summary["error_code"] in ARCHIVE_BLOCKING_CODES
            return summary
        return summary

    def close(self) -> None:
        self.console.stop()
        # 邮件子进程必须显式收掉：`source="mcp"` 时它是一个常驻的子进程，
        # 只关父进程的管道并不能保证它退出（会留下孤儿/僵尸）。
        # 收不上也不该让关闭流程失败，所以逐个 try。
        for target in (self.mail_engine, self.mail_source):
            closer = getattr(target, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        try:
            self.index_connection.close()
        except Exception:  # noqa: BLE001
            pass
        closer = getattr(self.store, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------------------
# 便捷入口（也是 `--once` 的实现）
# --------------------------------------------------------------------------------------


def run_once(
    config: Optional[ServiceConfig] = None,
    *,
    store: Any = None,
    log: Optional[Callable[[str], None]] = None,
) -> RoundReport:
    """跑**恰好一轮**并返回报告（`--once` 与测试都用它，和常驻循环同一份实现）。"""
    runtime = ServiceRuntime(config or load_config(), store=store, log=log)
    try:
        return runtime.run_once()
    finally:
        runtime.close()


def run_loop(
    config: Optional[ServiceConfig] = None,
    *,
    max_rounds: int = 0,
    store: Any = None,
    log: Optional[Callable[[str], None]] = None,
    start_console: bool = False,
    install_signals: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> List[RoundReport]:
    runtime = ServiceRuntime(config or load_config(), store=store, log=log, start_console=start_console)
    previous: Dict[str, Any] = {}
    try:
        if install_signals:
            previous = runtime.install_signal_handlers()
        return runtime.run_loop(max_rounds=max_rounds, sleep=sleep)
    finally:
        for name, handler in previous.items():
            signum = getattr(signal, name, None)
            if signum is not None:
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):  # pragma: no cover
                    pass
        runtime.close()


__all__ = ["ConsoleHandle", "ServiceRuntime", "open_service_store", "run_loop", "run_once"]
