"""百度网盘归档通道契约（SPEC §12、§21.3、§21.4）。

冻结的是**协议**，不是某一种接入实现：
- 设备侧常驻服务只依赖 `PanClient`；
- 具体通道（`baidu-drive` skill 通道 / 开放平台 OAuth / 本地目录模拟）实现同一协议；
- 完整性判定必须逐文件内容哈希回读校验（§21.4），只比数量或大小不算通过。

安全边界：
- `delete` 只允许在授权根目录内，且必须由保留策略显式驱动（默认不自动删除远端备份）；
- 凭据不得出现在协议参数、日志或返回值里。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .errors import ArchiveIntegrityError, ValidationError
from .ids import sha256_file


@dataclass
class PanPutResult:
    """一次上传结果。结果分类必须能区分"确定失败"与"结果未知"。"""

    ok: bool
    remote_path: str
    local_path: str
    size_bytes: int = 0
    content_hash: str = ""
    #: SUCCEEDED / FAILED_SAFE / UNKNOWN —— 与外部动作状态表一致
    result_class: str = "SUCCEEDED"
    raw: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class PanStat:
    remote_path: str
    size_bytes: int
    is_dir: bool = False
    content_hash: Optional[str] = None
    modified_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PanReadbackResult:
    """回读校验结果：只有逐文件哈希一致才算归档完成（SPEC §21.4）。"""

    ok: bool
    verified: List[str] = field(default_factory=list)
    mismatched: List[Dict[str, Any]] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    unverifiable: List[str] = field(default_factory=list)
    state: str = "PARTIAL"

    def require_ok(self) -> None:
        if not self.ok:
            raise ArchiveIntegrityError(
                "归档回读校验未通过，不得标记归档完成",
                detail={"state": self.state, "missing": self.missing, "mismatched": self.mismatched, "unverifiable": self.unverifiable},
            )


@runtime_checkable
class PanClient(Protocol):
    """归档通道协议：任何真实/模拟实现都必须满足它。"""

    name: str

    def ensure_dir(self, remote_dir: str) -> None: ...

    def put(self, local_path: str, remote_path: str) -> PanPutResult: ...

    def get(self, remote_path: str, local_path: str) -> PanPutResult: ...

    def stat(self, remote_path: str) -> Optional[PanStat]: ...

    def list(self, remote_dir: str) -> List[PanStat]: ...

    def delete(self, remote_path: str) -> bool: ...

    def quota(self) -> Dict[str, Any]: ...


class LocalDirPanClient:
    """本地目录模拟远端：用于离线跑通归档闭环与契约测试。

    它**不是**真实网盘能力；宣称真实归档能力前必须换成已验证的真实通道（ADR-0003、§21.3）。
    """

    name = "local_dir"

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    # ------------------------------------------------------------------ helpers
    def _resolve(self, remote_path: str) -> str:
        relative = remote_path.lstrip("/\\").replace("\\", "/")
        absolute = os.path.abspath(os.path.join(self.root, relative))
        if not absolute.startswith(self.root + os.sep) and absolute != self.root:
            raise ValidationError(f"远端路径越界，拒绝访问: {remote_path}")
        return absolute

    # ------------------------------------------------------------------ 协议实现
    def ensure_dir(self, remote_dir: str) -> None:
        os.makedirs(self._resolve(remote_dir), exist_ok=True)

    def put(self, local_path: str, remote_path: str) -> PanPutResult:
        target = self._resolve(remote_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(local_path, target)
        return PanPutResult(
            ok=True,
            remote_path=remote_path,
            local_path=local_path,
            size_bytes=os.path.getsize(target),
            content_hash=sha256_file(target),
            result_class="SUCCEEDED",
        )

    def get(self, remote_path: str, local_path: str) -> PanPutResult:
        source = self._resolve(remote_path)
        if not os.path.exists(source):
            return PanPutResult(
                ok=False, remote_path=remote_path, local_path=local_path, result_class="FAILED_SAFE", error="远端不存在"
            )
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        shutil.copyfile(source, local_path)
        return PanPutResult(
            ok=True,
            remote_path=remote_path,
            local_path=local_path,
            size_bytes=os.path.getsize(local_path),
            content_hash=sha256_file(local_path),
            result_class="SUCCEEDED",
        )

    def stat(self, remote_path: str) -> Optional[PanStat]:
        target = self._resolve(remote_path)
        if not os.path.exists(target):
            return None
        if os.path.isdir(target):
            return PanStat(remote_path=remote_path, size_bytes=0, is_dir=True)
        return PanStat(
            remote_path=remote_path,
            size_bytes=os.path.getsize(target),
            content_hash=sha256_file(target),
            modified_at=None,
        )

    def list(self, remote_dir: str) -> List[PanStat]:
        folder = self._resolve(remote_dir)
        if not os.path.isdir(folder):
            return []
        rows: List[PanStat] = []
        for name in sorted(os.listdir(folder)):
            absolute = os.path.join(folder, name)
            relative = f"{remote_dir.rstrip('/')}/{name}" if remote_dir else name
            if os.path.isdir(absolute):
                rows.append(PanStat(remote_path=relative, size_bytes=0, is_dir=True))
            else:
                rows.append(PanStat(remote_path=relative, size_bytes=os.path.getsize(absolute), content_hash=sha256_file(absolute)))
        return rows

    def delete(self, remote_path: str) -> bool:
        target = self._resolve(remote_path)
        if os.path.isdir(target):
            shutil.rmtree(target)
            return True
        if os.path.exists(target):
            os.remove(target)
            return True
        return False

    def quota(self) -> Dict[str, Any]:
        usage = shutil.disk_usage(self.root)
        return {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free, "account": "local_dir"}


def verify_readback(
    *,
    pan: PanClient,
    remote_root: str,
    entries: List[Dict[str, Any]],
    scratch_dir: str,
    local_root: str,
) -> PanReadbackResult:
    """逐文件回读校验（SPEC §21.4）。

    - 优先用远端 stat 的内容哈希（可信校验）；
    - 通道不给哈希时，下载回读到 `scratch_dir` 自己算哈希；
    - 两者都拿不到 → 标记 `unverifiable`，整体状态为 `PENDING_VERIFICATION`，**不得**宣称完整归档成功。
    """
    result = PanReadbackResult(ok=True)
    for entry in entries:
        relative = entry["relative_path"]
        remote_path = f"{remote_root.rstrip('/')}/{relative}"
        expected = entry["sha256"]
        stat = pan.stat(remote_path)
        if stat is None:
            result.missing.append(relative)
            continue
        actual = stat.content_hash
        if actual is None:
            local_copy = os.path.join(scratch_dir, relative)
            download = pan.get(remote_path, local_copy)
            if not download.ok:
                result.unverifiable.append(relative)
                continue
            actual = download.content_hash or sha256_file(local_copy)
        if actual != expected:
            result.mismatched.append({"path": relative, "expected": expected, "actual": actual})
        else:
            result.verified.append(relative)

    result.ok = not (result.missing or result.mismatched or result.unverifiable)
    if result.ok:
        result.state = "VERIFIED"
    elif result.unverifiable and not (result.missing or result.mismatched):
        result.state = "PENDING_VERIFICATION"
    else:
        result.state = "PARTIAL"
    return result


__all__ = [
    "PanClient",
    "LocalDirPanClient",
    "PanPutResult",
    "PanStat",
    "PanReadbackResult",
    "verify_readback",
]
