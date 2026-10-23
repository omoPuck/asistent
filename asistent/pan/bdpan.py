"""真实归档通道：把设备上的百度官方 `bdpan` CLI 包成契约 `PanClient`（SPEC §12、§21.3、§21.4）。

本模块是 `spike/baidu_pan/bdpan_client.py` 的**生产化**版本（同一套已验证的通道结论，
不重写通道）：绝对路径调用 + `--no-check-update`、写锁 + `approval_ref` 审批引用、
超时 = 结果未知、只读探测、凭据绝不进 argv/日志。

与 Spike 的差别（都是为了对齐冻结契约 `asistent/contracts/pan.py`）：
- 返回值改成契约的 `PanPutResult` / `PanStat`，用 `result_class` 明确区分
  `SUCCEEDED` / `FAILED_SAFE` / `UNKNOWN`（Spike 用异常表达，生产化后上层状态机更好落库）；
- `PanStat.content_hash` 一律 `None`：`bdpan ls --json` 返回的 `md5` 是百度侧摘要
  （大文件为分片 md5 拼接），**不能**作为可信内容校验依据 → 强制上层走"下载回读 + 本地重算 sha256"；
- 未授权（未登录 / 令牌过期 `errno=-6`）抛 `PanNotAuthorized`（`code=NOT_AUTHORIZED`），
  带上"待用户授权"的下一步动作，绝不静默跳过；
- `quota()` 不编造数字：CLI 没有配额查询命令时返回 `supported=False`。

通道事实（实测见 `docs/spikes/baidu-pan-spike.md`）：
- `bdpan` 是 Go 二进制，linux-arm64 可直接跑在 RK3588 上；设备上装在 `~/.local/bin/bdpan`；
- 所有操作被限制在网盘 `/apps/bdpan/` 之内，本适配器暴露的相对路径正是相对该目录；
- 认证 = OAuth 授权码（OOB），token 加密存放在 `~/.config/bdpan/config.json`；
  本模块**不读取、不输出、不复制**该文件，也不把任何令牌/授权码放进 argv 或日志。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..contracts.errors import ValidationError
from ..contracts.ids import sha256_file
from ..contracts.pan import PanPutResult, PanStat
from .errors import (
    LOGIN_NEXT_STEP,
    PanNotAuthorized,
    PanNotInstalled,
    PanUnavailable,
    PanWriteLocked,
)

__all__ = [
    "BDPAN_DEFAULT_BINARY",
    "DEFAULT_BINARY_CANDIDATES",
    "RESULT_FAILED_SAFE",
    "RESULT_SUCCEEDED",
    "RESULT_UNKNOWN",
    "BdpanCliPanClient",
    "BdpanProbe",
    "normalize_remote",
    "probe_bdpan",
]

#: 设备上的默认安装位置（绝对路径调用，不依赖 PATH —— 常驻服务/构建脚本的环境可能很干净）
BDPAN_DEFAULT_BINARY = "~/.local/bin/bdpan"
DEFAULT_BINARY_CANDIDATES: Tuple[str, ...] = (
    BDPAN_DEFAULT_BINARY,
    "/usr/local/bin/bdpan",
    "/usr/bin/bdpan",
)

RESULT_SUCCEEDED = "SUCCEEDED"
RESULT_FAILED_SAFE = "FAILED_SAFE"
RESULT_UNKNOWN = "UNKNOWN"

#: 未授权特征（`bdpan whoami` 未登录、`errno=-6` 令牌过期等）
NOT_AUTHORIZED_MARKERS: Tuple[str, ...] = (
    "未登录",
    "请先登录",
    "登录已过期",
    "授权已过期",
    "not logged in",
    "please login",
    "login required",
    "invalid token",
    "unauthorized",
    "errno=-6",
)
NOT_FOUND_MARKERS: Tuple[str, ...] = ("不存在", "not found", "no such file", "errno=-8", "errno=-9")
CONFLICT_MARKERS: Tuple[str, ...] = ("已存在", "already exists", "conflict", "errno=-10", "file exists")
#: bdpan 不认识子命令时（例如老版本没有 `mkdir`）——不算失败，但必须记录
UNSUPPORTED_MARKERS: Tuple[str, ...] = ("unknown command", "unknown flag", "unrecognized", "未知命令")
#: 能**确定没有产生远端副作用**的失败（输入/权限类），可安全重试
DETERMINISTIC_FAILURE_MARKERS: Tuple[str, ...] = (
    "本地文件不存在",
    "路径无效",
    "invalid argument",
    "no such file or directory",
    "permission denied",
    "拒绝访问",
)


def normalize_remote(remote_path: str) -> str:
    """规范化远端相对路径：禁止绝对路径与 `..` 穿越（契约 §21.4 安全边界）。"""
    if remote_path is None:
        raise ValidationError("远端路径不能为空")
    raw = str(remote_path).replace("\\", "/").strip()
    if not raw:
        raise ValidationError("远端路径不能为空")
    parts: List[str] = []
    for piece in raw.strip("/").split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            raise ValidationError(f"远端路径越界（含 ..）：{remote_path}")
        parts.append(piece)
    if not parts:
        raise ValidationError(f"远端路径不能是根目录：{remote_path}")
    return "/".join(parts)


@dataclass(frozen=True)
class BdpanProbe:
    """只读探测结果（不读配置文件内容、不做任何写操作）。"""

    installed: bool
    binary: Optional[str]
    version: Optional[str]
    logged_in: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "installed": self.installed,
            "binary": self.binary,
            "version": self.version,
            "logged_in": self.logged_in,
            "detail": self.detail,
            "auth_state": "AUTHORIZED" if self.logged_in else "NOT_AUTHORIZED",
            "next_step": None if self.logged_in else LOGIN_NEXT_STEP,
        }


def resolve_binary(binary: Optional[str] = None) -> Optional[str]:
    """解析 bdpan 可执行文件：显式参数 → 设备默认绝对路径 → PATH。"""
    if binary:
        return binary
    for candidate in DEFAULT_BINARY_CANDIDATES:
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    return shutil.which("bdpan")


def probe_bdpan(binary: Optional[str] = None, timeout: int = 20) -> BdpanProbe:
    """只读探测：是否安装、版本、是否已登录（等价于 `bdpan version` + `bdpan whoami`）。

    只调用这两个只读子命令；不读取 `~/.config/bdpan/config.json`。
    """
    exe = resolve_binary(binary)
    if not exe:
        return BdpanProbe(False, None, None, False, "默认路径与 PATH 上都没有 bdpan")
    if not os.path.isfile(exe) and shutil.which(exe) is None:
        return BdpanProbe(False, None, None, False, f"指定的 bdpan 不存在：{exe}")
    try:
        ver = subprocess.run(
            [exe, "version", "--no-check-update"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        who = subprocess.run(
            [exe, "whoami", "--no-check-update"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return BdpanProbe(True, exe, None, False, f"执行失败：{exc}")
    version = (ver.stdout or "").strip().splitlines()[0] if (ver.stdout or "").strip() else None
    combined = f"{who.stdout or ''}{who.stderr or ''}"
    logged_in = "已登录" in combined
    return BdpanProbe(
        installed=True,
        binary=exe,
        version=version,
        logged_in=logged_in,
        detail=f"version exit={ver.returncode} whoami exit={who.returncode}",
    )


class BdpanCliPanClient:
    """`bdpan` CLI → 契约 `PanClient`。

    参数
    ----
    binary        : bdpan 路径；默认按 `resolve_binary()` 解析（设备上为 `~/.local/bin/bdpan`）
    root_prefix   : `/apps/bdpan/` 之下再套的子前缀，例如默认的 `asistent`
    writes_locked : 默认 True。写入（put/delete/ensure_dir）在解锁前一律拒绝
    timeout       : 单次 CLI 调用超时（秒）；超时一律按**结果未知**处理
    runner        : 测试注入点 `(argv, timeout) -> (code, stdout, stderr)`；默认走 subprocess
    """

    name = "bdpan_cli"

    def __init__(
        self,
        binary: Optional[str] = None,
        root_prefix: str = "asistent",
        *,
        writes_locked: bool = True,
        timeout: int = 600,
        runner: Optional[Callable[[Sequence[str], int], Tuple[int, str, str]]] = None,
    ) -> None:
        self.binary = resolve_binary(binary)
        if not self.binary:
            raise PanNotInstalled(
                "找不到 bdpan；请先在设备上安装官方 bdpan CLI（spike 文档 §安装）",
                detail={"candidates": list(DEFAULT_BINARY_CANDIDATES)},
            )
        self.root_prefix = normalize_remote(root_prefix) if root_prefix else ""
        self.writes_locked = writes_locked
        self.timeout = timeout
        self._runner = runner
        self._approval_ref: Optional[str] = None
        #: 通道级备注（例如 `mkdir` 不受支持时的降级说明），供报告如实标注
        self.notes: List[str] = []
        #: 已实际发起的 CLI 调用（用于测试断言"只补传失败对象"这类计数）
        self.call_log: List[List[str]] = []

    # ------------------------------------------------------------ 批准闸门
    def unlock_writes(self, approval_ref: str) -> None:
        """由上层在拿到明确批准后调用；`approval_ref` 是审批记录 ID。

        凭据本身不进 argv —— 这里传的是**审批引用**，不是密码/token。
        """
        if not approval_ref or not str(approval_ref).strip():
            raise ValidationError("unlock_writes 需要非空的 approval_ref（不得凭空解锁）")
        self._approval_ref = str(approval_ref)
        self.writes_locked = False

    def lock_writes(self) -> None:
        self._approval_ref = None
        self.writes_locked = True

    @property
    def approval_ref(self) -> Optional[str]:
        return self._approval_ref

    def _assert_writes_allowed(self, action: str) -> None:
        if self.writes_locked:
            raise PanWriteLocked(action)

    # ------------------------------------------------------------ 只读探测
    def probe(self) -> BdpanProbe:
        return probe_bdpan(self.binary, timeout=min(self.timeout, 30))

    def require_authorized(self) -> BdpanProbe:
        """归档前的前置检查：未授权直接显式报错，绝不开始写。

        这是"未授权不得假装成功"的第一道闸门：连一次写调用都不会发出。
        """
        result = self.probe()
        if not result.logged_in:
            raise PanNotAuthorized(
                "bdpan 未授权（未登录或令牌已过期）：拒绝执行归档写入",
                detail={"probe": result.to_dict()},
            )
        return result

    # ------------------------------------------------------------ 路径
    def _remote(self, remote_path: str) -> str:
        relative = normalize_remote(remote_path)
        return f"{self.root_prefix}/{relative}" if self.root_prefix else relative

    # ------------------------------------------------------------ CLI 调用
    def _argv(self, args: Sequence[str], *, expect_json: bool) -> List[str]:
        # `--no-check-update` 必须带：常驻服务/构建环境里不允许 CLI 自更新
        argv = [str(self.binary), *args, "--no-check-update"]
        if expect_json:
            argv.append("--json")
        return argv

    def _run(self, args: Sequence[str], *, expect_json: bool) -> Tuple[int, str, str]:
        argv = self._argv(args, expect_json=expect_json)
        # 记录完整 argv（含绝对路径与 flag），供测试断言"只看不发凭据"
        self.call_log.append(list(argv))
        if self._runner is not None:
            return self._runner(argv, self.timeout)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # 超时 = 结果未知：绝不假定"没发生"
            raise _unknown_call(args, f"bdpan 调用超时（结果未知）：{exc}") from exc
        except OSError as exc:
            raise _unknown_call(args, f"bdpan 无法执行：{exc}") from exc
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    @staticmethod
    def _load_json(stdout: str) -> Any:
        """从 CLI 输出里取 JSON：可能在前后夹带提示行，且顶层可能是数组或对象。

        必须按"**最先出现的定界符**"决定切哪一段：`[{...}]` 这种数组如果按 `{` 切，
        会退化成单个对象，`ls` 就会被解析成空列表（真实 bug，已由本用例锁住）。
        """
        text = (stdout or "").strip()
        if not text:
            return None
        starts = [(index, mark) for index, mark in ((text.find("{"), "{"), (text.find("["), "[")) if index >= 0]
        if not starts:
            return None
        start, opening = min(starts)
        closing = "}" if opening == "{" else "]"
        end = text.rfind(closing)
        if end < start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            return None

    @staticmethod
    def _errno(payload: Any) -> Optional[int]:
        if isinstance(payload, dict):
            value = payload.get("errno")
            if isinstance(value, (int, str)):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    def _raise_if_unauthorized(self, out: str, err: str) -> None:
        combined = f"{out}\n{err}"
        for marker in NOT_AUTHORIZED_MARKERS:
            if marker in combined:
                raise PanNotAuthorized(
                    "bdpan 未授权（未登录 / 令牌过期）：拒绝继续，等待用户授权后重跑",
                    detail={"marker": marker},
                )

    def _classify(self, code: int, out: str, err: str) -> str:
        """`(exit_code, stdout, stderr)` → `SUCCEEDED` / `FAILED_SAFE` / `UNKNOWN`。

        先用 `errno` 判定（结构化、不受编码影响），再退回文本特征：
        非 UTF-8 环境的 stderr 可能整段变成 U+FFFD，此时**不允许**猜成成功或确定失败。
        """
        self._raise_if_unauthorized(out, err)
        if code == 0:
            return RESULT_SUCCEEDED
        payload = self._load_json(out)
        errno_value = self._errno(payload)
        if errno_value == -6:
            raise PanNotAuthorized("bdpan 未授权（errno=-6 令牌过期）", detail={"errno": -6})
        if errno_value in (-8, -9, -12):
            return RESULT_FAILED_SAFE
        if errno_value == -10:
            return RESULT_FAILED_SAFE
        combined = f"{out}\n{err}".lower()
        if any(marker.lower() in combined for marker in NOT_FOUND_MARKERS):
            return RESULT_FAILED_SAFE
        if any(marker.lower() in combined for marker in CONFLICT_MARKERS):
            return RESULT_FAILED_SAFE
        if any(marker.lower() in combined for marker in UNSUPPORTED_MARKERS):
            return RESULT_FAILED_SAFE
        if any(marker in f"{out}{err}" for marker in DETERMINISTIC_FAILURE_MARKERS):
            return RESULT_FAILED_SAFE
        # 兜底：非零退出 + 没有可识别的失败语义（崩溃、被 kill、未知 errno、编码不可逆）
        # → 副作用是否发生**无法判定**，必须报"结果未知"，让上层先 stat/回读再决定（A-5 约束 4）。
        return RESULT_UNKNOWN

    # ------------------------------------------------------------ 契约实现
    def ensure_dir(self, remote_dir: str) -> None:
        self._assert_writes_allowed("创建远端目录 ensure_dir")
        # 注意：前缀只在此处之外的 _remote() 里加一次；这里传**未加前缀**的路径
        if self.stat(remote_dir) is not None:
            return
        remote = self._remote(remote_dir)
        code, out, err = self._run(["mkdir", remote], expect_json=False)
        if code != 0:
            detail = f"{out}\n{err}".strip()
            if any(marker in detail for marker in CONFLICT_MARKERS):
                return
            if any(marker.lower() in detail.lower() for marker in UNSUPPORTED_MARKERS):
                note = (
                    "bdpan 不支持 mkdir（或该版本无此子命令）：依赖 upload 自动创建父目录"
                    f"（remote={remote}）"
                )
                if note not in self.notes:
                    self.notes.append(note)
                return
            raise _unknown_call(["mkdir", remote], f"bdpan mkdir 失败（exit={code}）：{detail[:200]}")

    def put(self, local_path: str, remote_path: str) -> PanPutResult:
        self._assert_writes_allowed("上传 put")
        if not os.path.isfile(local_path):
            raise ValidationError(f"本地文件不存在，禁止上传：{local_path}")
        remote = self._remote(remote_path)
        size = os.path.getsize(local_path)
        local_hash = sha256_file(local_path)
        code, out, err = self._run(["upload", local_path, remote], expect_json=False)
        result_class = self._classify(code, out, err)
        detail = f"{out}\n{err}".strip()
        return PanPutResult(
            ok=result_class == RESULT_SUCCEEDED,
            remote_path=remote,
            local_path=local_path,
            size_bytes=size,
            content_hash=local_hash,
            result_class=result_class,
            raw={"exit_code": code, "stdout": _safe_clip(out), "stderr": _safe_clip(err)},
            error=None if result_class == RESULT_SUCCEEDED else (detail[:400] or "上传失败（无输出）"),
        )

    def get(self, remote_path: str, local_path: str) -> PanPutResult:
        remote = self._remote(remote_path)
        os.makedirs(os.path.dirname(os.path.abspath(local_path)) or ".", exist_ok=True)
        code, out, err = self._run(["download", remote, local_path], expect_json=False)
        result_class = self._classify(code, out, err)
        detail = f"{out}\n{err}".strip()
        size = os.path.getsize(local_path) if os.path.isfile(local_path) else 0
        return PanPutResult(
            ok=result_class == RESULT_SUCCEEDED and size > 0,
            remote_path=remote,
            local_path=local_path,
            size_bytes=size,
            content_hash=sha256_file(local_path) if os.path.isfile(local_path) else "",
            result_class=result_class if size > 0 else RESULT_FAILED_SAFE,
            raw={"exit_code": code, "stdout": _safe_clip(out), "stderr": _safe_clip(err)},
            error=None if result_class == RESULT_SUCCEEDED else (detail[:400] or "下载失败（无输出）"),
        )

    def stat(self, remote_path: str) -> Optional[PanStat]:
        remote = self._remote(remote_path)
        parent, _, name = remote.rpartition("/")
        listing = self._list_raw(parent)
        for item in listing:
            if str(item.get("server_filename") or "") == name:
                return PanStat(
                    remote_path=remote,
                    size_bytes=int(item.get("size") or 0),
                    is_dir=bool(item.get("isdir")),
                    # 刻意留空：百度侧 md5 不可作为可信内容校验（见模块 docstring）
                    content_hash=None,
                    modified_at=_mtime_iso(item.get("server_mtime")),
                    raw={"fs_id": item.get("fs_id"), "md5_present": bool(item.get("md5"))},
                )
        return None

    def list(self, remote_dir: str) -> List[PanStat]:
        remote = self._remote(remote_dir)
        items: List[PanStat] = []
        for item in self._list_raw(remote):
            name = str(item.get("server_filename") or "")
            if not name:
                continue
            items.append(
                PanStat(
                    remote_path=f"{remote}/{name}",
                    size_bytes=int(item.get("size") or 0),
                    is_dir=bool(item.get("isdir")),
                    content_hash=None,
                    modified_at=_mtime_iso(item.get("server_mtime")),
                    raw={"fs_id": item.get("fs_id"), "md5_present": bool(item.get("md5"))},
                )
            )
        return items

    def delete(self, remote_path: str) -> bool:
        self._assert_writes_allowed("删除 delete")
        # 同样：传给 stat 的是未加前缀的相对路径（前缀只在 _remote() 加一次）
        if self.stat(remote_path) is None:
            return False
        remote = self._remote(remote_path)
        code, out, err = self._run(["rm", remote, "--force"], expect_json=True)
        result_class = self._classify(code, out, err)
        if result_class != RESULT_SUCCEEDED:
            raise _unknown_call(["rm", remote], f"bdpan rm 失败（exit={code}）：{err.strip()[:200]}")
        return True

    def quota(self) -> Dict[str, Any]:
        """配额：CLI 未提供查询命令时**如实返回不支持**，绝不编造数字（软配额判定留给上层）。"""
        return {
            "supported": False,
            "reason": "bdpan CLI 未提供配额查询子命令（spike 未实测出可用命令）",
            "note": "不得把未知配额当 0 或当作充足；软配额检查在真实命令可用前保持 UNKNOWN",
        }

    # ------------------------------------------------------------ 内部
    def _list_raw(self, remote_dir: str) -> List[Dict[str, Any]]:
        code, out, err = self._run(["ls", remote_dir], expect_json=True)
        if code != 0:
            self._raise_if_unauthorized(out, err)
            payload = self._load_json(out)
            errno_value = self._errno(payload)
            detail = f"{out}\n{err}"
            if errno_value in (-8, -9, -12) or any(
                marker.lower() in detail.lower() for marker in NOT_FOUND_MARKERS
            ):
                # 目录不存在 ⇒ 对象不存在（确定性，不算未知）
                return []
            raise _unknown_call(["ls", remote_dir], f"bdpan ls 失败（exit={code}）：{detail.strip()[:200]}")
        payload = self._load_json(out)
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("list") or payload.get("items") or []
        else:
            items = []
        return [item for item in items if isinstance(item, dict)]


def _unknown_call(args: Sequence[str], message: str) -> PanUnavailable:
    """结果未知的统一构造：`code=EXTERNAL_RESULT_UNKNOWN`，绝不当作"没发生"。"""
    return PanUnavailable(message, detail={"call": list(args), "result_class": RESULT_UNKNOWN})


def _safe_clip(text: str, limit: int = 300) -> str:
    return (text or "").strip()[:limit]


def _mtime_iso(value: Any) -> Optional[str]:
    try:
        import datetime

        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
