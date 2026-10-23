"""真实通道适配：把本机 `bdpan` CLI 包装成 `panclient.PanClient`。

⚠️ 本文件在 Spike 阶段**没有对真实百度网盘执行过任何写操作**。
   所有写入方法默认锁定，必须由上层在拿到用户明确批准后调用
   `unlock_writes(approval_ref=...)` 才可用（SPEC §21.3：外部删除/覆盖需逐次批准）。

通道事实（详见 docs/spikes/baidu-pan-spike.md）
------------------------------------------------
- `bdpan` 是百度官方发布的 Go 二进制（CDN: issuecdn.baidupcs.com/issue/netdisk/ai-bdpan/…）。
- 有官方 linux-arm64 构建（ELF 64-bit LSB, e_machine=183 AArch64），可直接跑在 RK3588 上。
- 认证 = OAuth 授权码模式（OOB）：`bdpan login --get-auth-url` 拿授权链接，
  用户在自己浏览器里授权后把 32 位授权码通过 `--set-code-stdin` 回填。
  Token 加密存放在 `~/.config/bdpan/config.json`（配合 `~/.config/bdpan/.token_key`）。
- 该 CLI 的所有操作被限制在网盘 `/apps/bdpan/` 之内；本适配器对外暴露的相对路径
  正是相对这个目录。
- `ls --json` 会返回 `md5`，但那是**百度侧**的 md5（大文件为分片 md5 拼接），
  不能当作可信内容校验。本适配器因此一律把 `content_sha256` 留空，
  强制上层走“下载回读 + 本地重算 sha256”，与 SPEC §21.4 一致。

不做的事
--------
- 不读取、不输出、不复制 `~/.config/bdpan/config.json` 的任何内容。
- 不把任何令牌/授权码放进命令行 argv 或日志。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

from panclient import (
    PanConflict,
    PanError,
    PanInvalid,
    PanNotFound,
    PanUnavailable,
    RemoteObject,
    normalize_remote,
    sha256_file,
)

__all__ = ["BdpanNotInstalled", "PanWriteNotAuthorized", "BdpanCliPanClient", "probe_bdpan"]


class BdpanNotInstalled(PanError):
    """找不到 bdpan 可执行文件。"""


class PanWriteNotAuthorized(PanError):
    """写入类操作未获批准（SPEC §21.3）。"""


@dataclass(frozen=True)
class BdpanProbe:
    installed: bool
    binary: str | None
    version: str | None
    logged_in: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "installed": self.installed,
            "binary": self.binary,
            "version": self.version,
            "logged_in": self.logged_in,
            "detail": self.detail,
        }


def probe_bdpan(binary: str | None = None, timeout: int = 20) -> BdpanProbe:
    """只读探测：是否安装、版本、是否已登录。**不读取配置文件内容。**"""
    exe = binary or shutil.which("bdpan")
    if not exe:
        return BdpanProbe(False, None, None, False, "PATH 上找不到 bdpan")
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
    version = (ver.stdout or "").strip().splitlines()[0] if ver.stdout else None
    logged_in = "已登录" in (who.stdout or "")
    return BdpanProbe(True, exe, version, logged_in, f"whoami exit={who.returncode}")


class BdpanCliPanClient:
    """`bdpan` CLI → PanClient 适配器。

    参数
    ----
    binary        : bdpan 可执行文件路径（默认走 PATH）
    root_prefix   : bdpan 应用目录（`/apps/bdpan/`）之下的子前缀，例如 `asistent`
    writes_locked : 默认 True。写入（put/delete）在解锁前一律拒绝。
    timeout       : 单次 CLI 调用超时（秒）
    """

    REMOTE_SCOPE = "/apps/bdpan"  # bdpan 强制的应用目录；只用于文档/断言，不作为可写参数

    def __init__(
        self,
        binary: str | None = None,
        root_prefix: str = "",
        *,
        writes_locked: bool = True,
        timeout: int = 600,
    ) -> None:
        self.binary = binary or shutil.which("bdpan")
        if not self.binary:
            raise BdpanNotInstalled("找不到 bdpan；请先在设备上安装官方 bdpan CLI")
        self.root_prefix = normalize_remote(root_prefix) if root_prefix else ""
        self.writes_locked = writes_locked
        self.timeout = timeout
        self._approval_ref: str | None = None

    # ---- 批准闸门 --------------------------------------------------------
    def unlock_writes(self, approval_ref: str) -> None:
        """由上层在拿到用户/认证控制的明确批准后调用。`approval_ref` 是审批记录 ID。"""
        if not approval_ref or not str(approval_ref).strip():
            raise PanInvalid("unlock_writes 需要非空的 approval_ref（不得凭空解锁）")
        self._approval_ref = str(approval_ref)
        self.writes_locked = False

    def lock_writes(self) -> None:
        self._approval_ref = None
        self.writes_locked = True

    def _assert_writes_allowed(self, action: str) -> None:
        if self.writes_locked:
            raise PanWriteNotAuthorized(
                f"{action} 未获批准：默认锁定。需要用户/认证控制明确批准后调用 "
                f"unlock_writes(approval_ref=...)（SPEC §21.3）"
            )

    # ---- 路径 ------------------------------------------------------------
    def _remote(self, remote_path: str) -> str:
        rel = normalize_remote(remote_path)
        return f"{self.root_prefix}/{rel}" if self.root_prefix else rel

    # ---- CLI 调用 --------------------------------------------------------
    def _run(self, args: Sequence[str], *, expect_json: bool) -> tuple[int, str, str]:
        argv = [self.binary, *args, "--no-check-update"]
        if expect_json:
            argv.append("--json")
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
            # 超时 = 结果未知：绝不假定“没发生”
            raise PanUnavailable(f"bdpan 调用超时（结果未知）：{' '.join(args)}") from exc
        except OSError as exc:
            raise PanUnavailable(f"bdpan 无法执行：{exc}") from exc
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    @staticmethod
    def _load_json(stdout: str) -> object | None:
        text = stdout.strip()
        if not text:
            return None
        # CLI 可能在 JSON 前后夹带提示行，取第一个 '{' 到最后一个 '}'
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            start, end = text.find("["), text.rfind("]")
            if start < 0 or end < 0:
                return None
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            return None

    @staticmethod
    def _errno(payload: object) -> int | None:
        if isinstance(payload, dict):
            val = payload.get("errno")
            if isinstance(val, (int, str)):
                try:
                    return int(val)
                except ValueError:
                    return None
        return None

    # ---- PanClient -------------------------------------------------------
    def put(self, local_path, remote_path: str, *, overwrite: bool = False) -> RemoteObject:
        self._assert_writes_allowed("上传 put")
        src = Path(local_path)
        if not src.is_file():
            raise PanInvalid(f"本地文件不存在：{src}")
        rel = self._remote(remote_path)

        existing = self.stat(remote_path)
        if existing is not None:
            same, _ = self._verify(rel, sha256_file(src))
            if same:
                return existing
            if not overwrite:
                # bdpan 的 upload 在重名时行为依赖 CLI；这里显式要求 overwrite，
                # 避免“以为覆盖了其实重命名成了 xxx(1)”这种静默偏差。
                raise PanConflict(
                    f"远端已存在不同内容且未授权覆盖：{rel}（SPEC §21.3；"
                    "真实覆盖需单独批准，且须先确认 CLI 的冲突策略）"
                )

        code, out, err = self._run(["upload", str(src), rel], expect_json=True)
        payload = self._load_json(out)
        if code != 0:
            raise PanUnavailable(
                f"bdpan upload 返回非 0（exit={code}）且无法确认副作用是否发生：{err.strip()[:200]}"
            )
        if self._errno(payload) not in (0, None):
            raise PanError(f"bdpan upload 业务错误 errno={self._errno(payload)}")

        # CLI 报告成功仍不足以判定：真实内容一律回读校验（由对端 PanClient 协议要求）
        st = self.stat(remote_path)
        if st is None:
            raise PanUnavailable(f"上传后 stat 不到对象，状态未知：{rel}")
        return RemoteObject(
            path=st.path, size=st.size, isdir=False, mtime=st.mtime, content_sha256=None
        )

    def get(self, remote_path: str, local_path) -> RemoteObject:
        rel = self._remote(remote_path)
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()
        code, _out, err = self._run(["download", rel, str(dst)], expect_json=False)
        if code != 0 or not dst.is_file():
            msg = err.strip()[:200]
            if "不存在" in msg or "not found" in msg.lower():
                raise PanNotFound(f"远端文件不存在：{rel}")
            raise PanUnavailable(f"bdpan download 失败（exit={code}）：{msg}")
        return RemoteObject(
            path=rel, size=dst.stat().st_size, isdir=False, mtime=dst.stat().st_mtime, content_sha256=None
        )

    def list(self, remote_dir: str) -> list[RemoteObject]:
        if remote_dir in ("", ".", "/"):
            rel = self.root_prefix or ""
        else:
            rel = self._remote(remote_dir)
        args = ["ls", rel] if rel else ["ls"]
        code, out, err = self._run(args, expect_json=True)
        payload = self._load_json(out)
        if code != 0:
            raise PanUnavailable(f"bdpan ls 失败（exit={code}）：{err.strip()[:200]}")
        items = payload if isinstance(payload, list) else (payload or {}).get("list", []) if isinstance(payload, dict) else []
        result: list[RemoteObject] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            # 把 /apps/bdpan/xxx 还原成相对路径
            for prefix in (self.REMOTE_SCOPE + "/", "/apps/bdpan/"):
                if path.startswith(prefix):
                    path = path[len(prefix) :]
                    break
            result.append(
                RemoteObject(
                    path=path or str(item.get("server_filename") or ""),
                    size=int(item.get("size") or 0),
                    isdir=bool(item.get("isdir")),
                    mtime=float(item.get("server_mtime") or 0.0),
                    # 百度侧 md5 不作为可信校验依据 -> 刻意留空，强制回读
                    content_sha256=None,
                )
            )
        return result

    def stat(self, remote_path: str) -> RemoteObject | None:
        rel = self._remote(remote_path)
        parent = str(PurePosixPath(rel).parent)
        name = PurePosixPath(rel).name
        parent_arg = parent if parent not in ("", ".") else (self.root_prefix or ".")
        code, out, err = self._run(["ls", parent_arg], expect_json=True)
        if code != 0:
            # 父目录不存在 ⇒ 对象不存在（确定性，不算未知）
            if "不存在" in err or "not found" in err.lower():
                return None
            raise PanUnavailable(f"bdpan ls 失败（exit={code}）：{err.strip()[:200]}")
        payload = self._load_json(out)
        items = payload if isinstance(payload, list) else (payload or {}).get("list", []) if isinstance(payload, dict) else []
        for item in items or []:
            if isinstance(item, dict) and str(item.get("server_filename") or "") == name:
                return RemoteObject(
                    path=rel,
                    size=int(item.get("size") or 0),
                    isdir=bool(item.get("isdir")),
                    mtime=float(item.get("server_mtime") or 0.0),
                    content_sha256=None,
                )
        return None

    def delete(self, remote_path: str) -> bool:
        self._assert_writes_allowed("删除 delete")
        rel = self._remote(remote_path)
        if self.stat(remote_path) is None:
            return False
        code, out, err = self._run(["rm", rel, "--force"], expect_json=True)
        if code != 0:
            raise PanUnavailable(f"bdpan rm 失败（exit={code}）：{err.strip()[:200]}")
        return True

    # ---- 内部：回读校验 --------------------------------------------------
    def _verify(self, rel_path: str, expected_sha256: str) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory(prefix="bdpan-verify-") as td:
            local = Path(td) / "object.bin"
            try:
                self.get(rel_path, local)
            except PanNotFound:
                return False, "missing"
            actual = sha256_file(local)
            return actual == expected_sha256, actual
