"""服务配置（SPEC §14.1/§14.4、§21.7）。

三条纪律：

1. **配置在仓库外**：默认 `$ASISTENT_VAR/config/service.json`（`$ASISTENT_VAR` 默认
   `~/asistent-var`）。仓库里只有代码，任何"这台设备上跑什么"的东西都在仓库外。
2. **安全默认值**：不给配置也能起来，但起来的样子是"**什么都不外发**"：外发通道 `none`、
   归档通道 `none`、邮件同步关闭。要用真实能力必须显式打开——默认值不会替用户做危险决定。
3. **配置里不放凭据**：本模块只读路径（token 文件路径、库路径），
   凭据内容由各自模块从 700/600 文件里读；`describe()` 的输出**不含**任何秘密。

`load_config()` 的解析顺序（后者覆盖前者）：

```text
内置安全默认值
  → 配置文件（--config 指定 / $ASISTENT_SERVICE_CONFIG / $ASISTENT_VAR/config/service.json）
  → 少量环境变量（ASISTENT_STORE / ASISTENT_VAR / ASISTENT_SERVICE_INTERVAL）
```

路径里的 `~` 与 `$VAR` 都会展开；相对路径按**配置文件所在目录**解析（不按 cwd，避免
"换个目录启动就换了一套数据"）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

#: 配置文件默认名字（放在 `$ASISTENT_VAR/config/` 下）
SERVICE_CONFIG_NAME = "service.json"

#: 默认轮次间隔（秒）。SPEC §21.7 要求"新邮件默认 5 分钟内发现"，
#: 60 秒一轮留足余量，同时不至于把 RK3588 的 CPU 打满（每轮基本只做几次 SQLite 查询）。
DEFAULT_INTERVAL_SECONDS = 60

#: 外发通道的可选值（"none" = 拒绝外发，安全默认）
CHANNEL_NONE = "none"
#: 归档通道的可选值
ARCHIVE_CHANNEL_NONE = "none"
ARCHIVE_CHANNEL_LOCAL = "local_dir"

#: 真值表：能声明为"已授权外发"的通道值
OUTBOUND_CHANNELS = (CHANNEL_NONE, "scripted")

_ENV_PREFIX = "ASISTENT_SERVICE_"


def _expand(value: str, base_dir: str) -> str:
    """展开 `~` / `$VAR`；相对路径按 `base_dir` 解析（空串保持空串）。"""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    text = os.path.expanduser(os.path.expandvars(text))
    if not os.path.isabs(text) and base_dir:
        text = os.path.join(base_dir, text)
    return os.path.abspath(text)


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: Any, *, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("配置项必须是整数：%r（%s）" % (value, exc)) from exc


def _mapping(value: Any, *, name: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("配置项 %s 必须是对象（JSON object）" % name)
    return dict(value)


# --------------------------------------------------------------------------------------
# 分段配置
# --------------------------------------------------------------------------------------


@dataclass
class ConsoleSection:
    """控制台（MVP-4）接线。token **只记路径**，从不读进来。"""

    enabled: bool = True
    bind: str = "127.0.0.1"
    port: int = 18090
    token_path: str = ""
    artifact_roots: Sequence[str] = ()
    max_body_bytes: int = 262144
    allow_remote: bool = False

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "bind": self.bind,
            "port": self.port,
            "token_path": self.token_path,
            "token_present": bool(self.token_path) and os.path.isfile(self.token_path),
            "allow_remote": self.allow_remote,
        }


@dataclass
class MailSection:
    """邮件同步（MVP-2）接线。`source="none"` = 不同步（安全默认）。

    `source="mcp"` 走真实通道：本层拉起 MVP-2 的 `McpMailSource`
    （`mcp-email-server` 以 stdio 子进程方式运行），这是"能替你处理真邮件"的那条线。

    凭据纪律：授权码**只**从 `authcode_path` 指向的 600 文件读（仓库外、不进配置、
    不进 argv、不进日志），读出来只写进子进程的环境变量 `MCP_EMAIL_SERVER_PASSWORD`；
    `StdioJsonRpcClient` 会按"键名像秘密"自动登记脱敏值。
    """

    enabled: bool = False
    #: none | fixture | mcp
    source: str = "none"
    account: str = ""
    folder: str = "INBOX"
    fixture_dir: str = ""
    state_path: str = ""
    raw_root: str = ""
    create_conversation: bool = True
    max_attempts: int = 3
    #: `source="mcp"` 时拉起子进程的命令（空 = 用内置默认探测）
    mcp_command: Sequence[str] = ()
    #: 传给子进程的**非机密**附加环境变量（机密一律走 authcode_path）
    mcp_env: Mapping[str, str] = field(default_factory=dict)
    #: 授权码文件（600，仓库外）；空 = 默认 `$VAR/config/mail-authcode.txt`
    authcode_path: str = ""
    #: IMAP 接入点（163 用 imap.163.com:993 + SSL；这些不是机密）
    imap_host: str = "imap.163.com"
    imap_port: int = 993
    imap_ssl: bool = True

    def describe(self) -> Dict[str, Any]:
        # 注意：这里**不列**凭据文件的路径，只列"有没有配好"。
        # 原因不是路径本身是秘密，而是这条描述会出现在**手机端可见**的状态快照里，
        # 而团队约定的一条线是"快照里不出现凭据字样"（`tests/service/support.py::SECRET_MARKERS`）。
        # 需要知道具体路径与怎么办的时候，看 `notes`（那是给运维看的，且带用户动作指引）。
        # `mcp_env` 连键名都不列（键名本身可能带信息），只列个数。
        return {
            "enabled": self.enabled,
            "source": self.source,
            "account": self.account,
            "folder": self.folder,
            "fixture_dir": self.fixture_dir,
            "state_path": self.state_path,
            "raw_root": self.raw_root,
            "mcp_command": list(self.mcp_command),
            "mcp_env_keys": len(self.mcp_env),
            "credential_present": bool(self.authcode_path and os.path.exists(self.authcode_path)),
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "imap_ssl": self.imap_ssl,
        }


@dataclass
class IngestSection:
    """资料采集与索引（MVP-1）接线。"""

    enabled: bool = False
    roots: Sequence[str] = ()
    max_bytes: int = 2 * 1024 * 1024
    index_path: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "roots": list(self.roots),
            "max_bytes": self.max_bytes,
            "index_path": self.index_path,
        }


@dataclass
class OutboundSection:
    """外发（MVP-5）接线。

    `channel="none"` 时服务**不尝试**执行任何动作（连 `execute()` 都不调）：
    宁可停在 AUTHORIZED 等配置，也不做一次没有通道的尝试（避免每轮刷一条 REJECT 审计）。
    """

    enabled: bool = True
    channel: str = CHANNEL_NONE
    from_account: str = ""
    node: str = "service-1"

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "channel": self.channel,
            "from_account": self.from_account,
            "node": self.node,
            "will_attempt_send": bool(self.enabled and self.channel != CHANNEL_NONE),
        }


@dataclass
class ArchiveSection:
    """归档（MVP-7）接线。

    `channel="none"` = 不做归档（安全默认，**绝不**因为"反正本地的"就假装归档成功）。
    `channel="local_dir"` = 契约自带的本地目录模拟远端（离线回路，**不是**真实网盘能力）。
    真实 bdpan 通道必须显式配置并由用户完成 OAuth 授权，未授权时归档结果为 `NOT_AUTHORIZED`。
    """

    enabled: bool = False
    channel: str = ARCHIVE_CHANNEL_NONE
    root: str = ""
    allowed_suffix = (".eml", ".md", ".txt", ".json")
    staging_root: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "channel": self.channel,
            "root": self.root,
            "staging_root": self.staging_root,
        }


@dataclass
class ServiceConfig:
    """一次服务运行所需的全部配置（没有全局状态、没有隐式默认）。"""

    #: 配置文件来源（取证用；空串 = 全部默认值）
    config_path: str = ""
    var_root: str = ""
    store: str = "sqlite"
    db_path: str = ""
    raw_root: str = ""
    logs_dir: str = ""
    state_dir: str = ""
    #: 轮次间隔（秒）；`<= 0` 视为默认值（防止配置写 0 变成死循环空转）
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    #: 最多跑多少轮（0 = 不限；`--once` 与测试用 1）
    max_rounds: int = 0
    #: 单轮里每类对象的处理上限（防止一个坏库把一轮拖成全量重建）
    batch_limit: int = 50
    #: 采集/归档等长耗时阶段是否在同一轮里做（关掉可把轮次压到毫秒级）
    console: ConsoleSection = field(default_factory=ConsoleSection)
    mail: MailSection = field(default_factory=MailSection)
    ingest: IngestSection = field(default_factory=IngestSection)
    outbound: OutboundSection = field(default_factory=OutboundSection)
    archive: ArchiveSection = field(default_factory=ArchiveSection)
    #: 是否在理解阶段顺带把"用户已回答的追问"接进重新分析（本层兜底）
    resolve_answers: bool = True
    #: 提示词/界面用的时区名（不参与任何判定，只影响展示）
    display_timezone: str = "Asia/Shanghai"

    # ------------------------------------------------------------------ 观察
    def describe(self) -> Dict[str, Any]:
        """启动日志与 `--status` 用；**不含令牌、密码、授权码**。"""
        return {
            "config_path": self.config_path,
            "var_root": self.var_root,
            "store": self.store,
            "db_path": self.db_path,
            "state_dir": self.state_dir,
            "interval_seconds": self.interval_seconds,
            "max_rounds": self.max_rounds,
            "batch_limit": self.batch_limit,
            "resolve_answers": self.resolve_answers,
            "console": self.console.describe(),
            "mail": self.mail.describe(),
            "ingest": self.ingest.describe(),
            "outbound": self.outbound.describe(),
            "archive": self.archive.describe(),
        }

    # ------------------------------------------------------------------ 派生路径
    def state_file(self) -> str:
        return os.path.join(self.state_dir, "service-state.json")

    def dict_repr(self) -> Dict[str, Any]:
        return self.describe()


# --------------------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------------------


def default_var_root() -> str:
    return os.path.expanduser(os.environ.get("ASISTENT_VAR") or os.path.join("~", "asistent-var"))


def default_config_path(var_root: Optional[str] = None) -> str:
    root = var_root or default_var_root()
    return os.path.join(root, "config", SERVICE_CONFIG_NAME)


def _build(payload: Dict[str, Any], *, config_path: str, base_dir: str) -> ServiceConfig:
    root = _mapping(payload, name="root")
    var_root = _expand(root.get("var_root") or os.environ.get("ASISTENT_VAR") or default_var_root(), base_dir)
    data_dir = os.path.join(var_root, "data")
    state_dir = os.path.join(var_root, "state")

    console_raw = _mapping(root.get("console"), name="console")
    mail_raw = _mapping(root.get("mail"), name="mail")
    ingest_raw = _mapping(root.get("ingest"), name="ingest")
    outbound_raw = _mapping(root.get("outbound"), name="outbound")
    archive_raw = _mapping(root.get("archive"), name="archive")

    console = ConsoleSection(
        enabled=_as_bool(console_raw.get("enabled"), default=True),
        bind=str(console_raw.get("bind") or "127.0.0.1"),
        port=_as_int(console_raw.get("port"), default=18090),
        token_path=_expand(
            console_raw.get("token_path") or os.path.join(var_root, "config", "console-token"), base_dir
        ),
        artifact_roots=tuple(
            _expand(item, base_dir) for item in (console_raw.get("artifact_roots") or ())
        ),
        max_body_bytes=_as_int(console_raw.get("max_body_bytes"), default=262144),
        allow_remote=_as_bool(console_raw.get("allow_remote"), default=False),
    )

    mail = MailSection(
        enabled=_as_bool(mail_raw.get("enabled"), default=False),
        source=str(mail_raw.get("source") or "none"),
        account=str(mail_raw.get("account") or ""),
        folder=str(mail_raw.get("folder") or "INBOX"),
        fixture_dir=_expand(mail_raw.get("fixture_dir") or "", base_dir),
        state_path=_expand(
            mail_raw.get("state_path") or os.path.join(state_dir, "mail-sync.json"), base_dir
        ),
        raw_root=_expand(mail_raw.get("raw_root") or os.path.join(var_root, "raw"), base_dir),
        create_conversation=_as_bool(mail_raw.get("create_conversation"), default=True),
        max_attempts=_as_int(mail_raw.get("max_attempts"), default=3),
        # `mcp_command` 是**命令列表**，绝不经过 shell（不拼字符串、不做展开），
        # 避免把"路径里有空格/引号"变成命令注入面。
        mcp_command=tuple(str(item) for item in (mail_raw.get("mcp_command") or ())),
        mcp_env={
            str(key): str(value)
            for key, value in (mail_raw.get("mcp_env") or {}).items()
            if value is not None
        },
        authcode_path=_expand(
            mail_raw.get("authcode_path") or os.path.join(var_root, "config", "mail-authcode.txt"),
            base_dir,
        ),
        imap_host=str(mail_raw.get("imap_host") or "imap.163.com"),
        imap_port=_as_int(mail_raw.get("imap_port"), default=993),
        imap_ssl=_as_bool(mail_raw.get("imap_ssl"), default=True),
    )

    ingest = IngestSection(
        enabled=_as_bool(ingest_raw.get("enabled"), default=False),
        roots=tuple(_expand(item, base_dir) for item in (ingest_raw.get("roots") or ())),
        max_bytes=_as_int(ingest_raw.get("max_bytes"), default=2 * 1024 * 1024),
        index_path=_expand(
            ingest_raw.get("index_path") or os.path.join(var_root, "index", "fts.db"), base_dir
        ),
    )

    channel = str(outbound_raw.get("channel") or CHANNEL_NONE).strip().lower()
    if channel not in OUTBOUND_CHANNELS:
        raise ValueError(
            "outbound.channel 只能是 %s（收到 %r）——不支持的值必须显式失败，不能默默降级"
            % (list(OUTBOUND_CHANNELS), channel)
        )
    outbound = OutboundSection(
        enabled=_as_bool(outbound_raw.get("enabled"), default=True),
        channel=channel,
        from_account=str(outbound_raw.get("from_account") or ""),
        node=str(outbound_raw.get("node") or "service-1"),
    )

    archive_channel = str(archive_raw.get("channel") or ARCHIVE_CHANNEL_NONE).strip().lower()
    if archive_channel not in (ARCHIVE_CHANNEL_NONE, ARCHIVE_CHANNEL_LOCAL, "bdpan"):
        raise ValueError(
            "archive.channel 只能是 none/local_dir/bdpan（收到 %r）" % (archive_channel,)
        )
    # `archive.root` 的语义**随通道而变**，不能一律展开成本地绝对路径：
    #   - `local_dir`：它真的就是本机目录 → 照旧展开 `~`/`$VAR`、相对路径按配置目录解析；
    #   - `bdpan`：它是**网盘上的远端目录前缀**（例如 `个人助手归档`），必须原样保留。
    #
    # 一律 `_expand()` 的后果是设备取证实录里那条：
    #   "root": "/home/asistent/asistent-var/config/个人助手归档"
    # 于是归档清单会把一个**本机路径**当成"远端位置"记下来（清单的核心作用就是说明副本在哪），
    # 而真实归档要么在网盘上建一个以本机路径命名的怪目录、要么直接失败。
    # 这类缺陷本机测不出来：本地跑的都是 `local_dir` 通道（那本来就需要绝对路径）。
    archive_root_raw = str(archive_raw.get("root") or "").strip()
    if archive_channel == "bdpan":
        archive_root = archive_root_raw.strip("/")
    else:
        archive_root = _expand(
            archive_root_raw or os.path.join(var_root, "archive", "local-pan"), base_dir
        )
    archive = ArchiveSection(
        enabled=_as_bool(archive_raw.get("enabled"), default=False),
        channel=archive_channel,
        root=archive_root,
        staging_root=_expand(
            archive_raw.get("staging_root") or os.path.join(var_root, "archive", "staging"), base_dir
        ),
    )

    return ServiceConfig(
        config_path=config_path,
        var_root=var_root,
        store=str(root.get("store") or os.environ.get("ASISTENT_STORE") or "sqlite").strip().lower(),
        db_path=_expand(root.get("db_path") or os.path.join(data_dir, "asistent.db"), base_dir),
        raw_root=mail.raw_root,
        logs_dir=_expand(root.get("logs_dir") or os.path.join(var_root, "logs"), base_dir),
        state_dir=_expand(root.get("state_dir") or state_dir, base_dir),
        interval_seconds=max(1, _as_int(root.get("interval_seconds"), default=DEFAULT_INTERVAL_SECONDS)),
        max_rounds=max(0, _as_int(root.get("max_rounds"), default=0)),
        batch_limit=max(1, _as_int(root.get("batch_limit"), default=50)),
        console=console,
        mail=mail,
        ingest=ingest,
        outbound=outbound,
        archive=archive,
        resolve_answers=_as_bool(root.get("resolve_answers"), default=True),
        display_timezone=str(root.get("display_timezone") or "Asia/Shanghai"),
    )


def load_config(
    path: Optional[str] = None,
    *,
    payload: Optional[Dict[str, Any]] = None,
) -> ServiceConfig:
    """读配置。文件不存在 → 全部安全默认值（不报错：装完就能起来看状态）。"""
    env_path = os.environ.get(_ENV_PREFIX + "CONFIG") or ""
    resolved = path or env_path or os.environ.get("ASISTENT_SERVICE_CONFIG") or ""
    if not resolved:
        resolved = default_config_path()
    resolved = _expand(resolved, "")

    body: Dict[str, Any] = {}
    if payload is not None:
        body = dict(payload)
        config_path = resolved if path or env_path else ""
        base_dir = os.path.dirname(config_path) if config_path else os.getcwd()
    elif os.path.isfile(resolved):
        with open(resolved, "r", encoding="utf-8") as handle:
            body = json.load(handle)
        if not isinstance(body, dict):
            raise ValueError("配置文件顶层必须是 JSON 对象：%s" % resolved)
        config_path = resolved
        base_dir = os.path.dirname(resolved)
    else:
        config_path = ""
        base_dir = os.getcwd()

    config = _build(body, config_path=config_path, base_dir=base_dir)

    # ---- 少量环境变量兜底（部署脚本/一次性排查用；仍然不碰凭据）
    interval = os.environ.get(_ENV_PREFIX + "INTERVAL")
    if interval:
        config.interval_seconds = max(1, int(interval))
    store_env = os.environ.get("ASISTENT_STORE", "").strip().lower()
    if payload is None and store_env:
        config.store = store_env
    return config


__all__ = [
    "ARCHIVE_CHANNEL_LOCAL",
    "ARCHIVE_CHANNEL_NONE",
    "ArchiveSection",
    "CHANNEL_NONE",
    "ConsoleSection",
    "DEFAULT_INTERVAL_SECONDS",
    "IngestSection",
    "MailSection",
    "OUTBOUND_CHANNELS",
    "OutboundSection",
    "SERVICE_CONFIG_NAME",
    "ServiceConfig",
    "default_config_path",
    "default_var_root",
    "load_config",
]
