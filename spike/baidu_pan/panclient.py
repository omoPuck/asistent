"""PanClient 协议（PanClient protocol）与本地目录实现。

零第三方依赖：仅使用 Python 标准库（Python >= 3.10）。

本模块定义 asistent 百度网盘归档所需的**最小远端契约**，以及一个
`LocalDirPanClient`——用本地目录模拟远端网盘，用于离线跑通 SPEC §21.4 的
归档 / 回读校验 / 重试 / 恢复闭环。

远端路径约定
------------
- 远端路径是**相对路径**（POSIX 分隔符，`/`），不含前导 `/`，不允许 `..`。
- 真实百度网盘开放平台把第三方应用限制在 `/apps/{appname}/` 目录内
  （见 docs/spikes/baidu-pan-spike.md）。因此本项目所有远端路径都是
  `/apps/asistent/` 之下的相对路径，例如 `个人助手归档/事务归档/<archive_id>/manifest.json`。
  协议层不出现 `/apps/asistent` 前缀，前缀由具体传输实现负责拼接。

方法命名（跨实现一致）
----------------------
- `put(local_path, remote_path, *, overwrite=False)`  上传一个本地文件
- `get(remote_path, local_path)`                       下载一个远端文件（回读）
- `list(remote_dir)`                                   列举一个远端目录（单层）
- `stat(remote_path)`                                  查询单个远端对象（不存在返回 None）
- `delete(remote_path)`                                删除一个远端文件

错误语义（对应 SPEC §13.1 失败分类）
------------------------------------
- `PanNotFound`      远端对象不存在（外部确定性失败）
- `PanConflict`      远端已存在**不同内容**且未获授权覆盖（禁止静默覆盖）
- `PanUnavailable`   **结果未知**：超时/连接中断/崩溃，无法证明副作用是否发生。
                     SPEC §13.2/§21.2：未知结果不得盲目重试，必须先查证。
- `PanInvalid`       输入无效（路径穿越、参数错误）
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, Sequence, runtime_checkable

__all__ = [
    "CHUNK_SIZE",
    "RemoteObject",
    "PanError",
    "PanInvalid",
    "PanNotFound",
    "PanConflict",
    "PanUnavailable",
    "PanClient",
    "LocalDirPanClient",
    "sha256_file",
    "normalize_remote",
]

CHUNK_SIZE = 1024 * 1024


# --------------------------------------------------------------------------
# 错误
# --------------------------------------------------------------------------
class PanError(Exception):
    """PanClient 错误基类。"""


class PanInvalid(PanError):
    """输入无效（非法远端路径、本地文件缺失等）。"""


class PanNotFound(PanError):
    """远端对象不存在。"""


class PanConflict(PanError):
    """远端存在不同内容，且未获授权覆盖。"""


class PanUnavailable(PanError):
    """结果未知：无法证明副作用是否发生（超时 / 连接中断 / 崩溃）。"""


# --------------------------------------------------------------------------
# 数据对象
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RemoteObject:
    """一个远端对象的元数据。

    `content_sha256` 只在传输实现**能被信任**地提供内容哈希时才填充；
    默认是 `None`，表示“该实现无法提供可信内容校验”，调用方必须下载回读
    自己算哈希（SPEC §21.4：“接入方式没有可信内容校验时，需下载回读验算”）。
    """

    path: str
    size: int
    isdir: bool = False
    mtime: float = 0.0
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.isdir and self.content_sha256 is not None:
            raise PanInvalid("目录对象不应带内容哈希")


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def sha256_file(path: str | os.PathLike[str]) -> str:
    """流式计算文件 sha256（16 进制小写）。"""
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK_SIZE), b""):
            h.update(block)
    return h.hexdigest()


def normalize_remote(path: str) -> str:
    """规范化并校验远端相对路径；拒绝绝对路径、`..` 和空路径。"""
    if not isinstance(path, str):
        raise PanInvalid(f"远端路径必须是 str，收到 {type(path).__name__}")
    raw = path.replace("\\", "/").strip()
    if not raw:
        raise PanInvalid("远端路径不能为空")
    pure = PurePosixPath(raw)
    if pure.is_absolute():
        raise PanInvalid(f"远端路径必须是相对路径：{path!r}")
    parts: list[str] = []
    for part in pure.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise PanInvalid(f"远端路径不允许出现 '..'：{path!r}")
        if "\x00" in part:
            raise PanInvalid("远端路径不允许包含 NUL")
        parts.append(part)
    if not parts:
        raise PanInvalid(f"远端路径规范化后为空：{path!r}")
    return "/".join(parts)


# --------------------------------------------------------------------------
# 协议
# --------------------------------------------------------------------------
@runtime_checkable
class PanClient(Protocol):
    """远端网盘的最小契约。

    实现必须满足：
    1. 幂等：同一 `(local_path, remote_path)` 且内容一致时重复 `put` 不产生副本；
    2. 不静默覆盖：`overwrite=False` 且内容不同时抛 `PanConflict`；
    3. 结果未知必须显式抛 `PanUnavailable`，不得伪装成功或失败。
    """

    def put(self, local_path: str | os.PathLike[str], remote_path: str, *, overwrite: bool = False) -> RemoteObject:
        """上传本地文件到 `remote_path`，返回远端元数据。"""

    def get(self, remote_path: str, local_path: str | os.PathLike[str]) -> RemoteObject:
        """下载 `remote_path` 到本地 `local_path`（覆盖本地），返回远端元数据。"""

    def list(self, remote_dir: str) -> Sequence[RemoteObject]:
        """列举 `remote_dir` 的**直接**子项（目录在前或在后不保证顺序）。"""

    def stat(self, remote_path: str) -> RemoteObject | None:
        """查询 `remote_path`；不存在返回 `None`。"""

    def delete(self, remote_path: str) -> bool:
        """删除 `remote_path`；返回是否真的删除了对象。"""


# --------------------------------------------------------------------------
# 本地目录实现（模拟远端）
# --------------------------------------------------------------------------
class LocalDirPanClient:
    """用本地目录模拟远端网盘。

    用途：在没有真实凭据、也不向真实网盘写入任何数据的前提下，跑通
    SPEC §21.4 的归档 / 逐文件回读校验 / 幂等重试 / 隔离恢复闭环。

    - `root` 是“远端根”；远端路径 `a/b.md` 映射到 `root/a/b.md`。
    - 提供 `content_sha256`（本地文件可自由读取，因此该实现**能**给出可信哈希）。
      归档流程仍然默认走下载回读，以保证与真实网盘路径行为一致；
      真实实现无法提供可信哈希时行为不变。
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- 内部 --------------------------------------------------------------
    def _abs(self, remote_path: str) -> Path:
        rel = normalize_remote(remote_path)
        target = (self.root / rel).resolve()
        # 防目录穿越：解析后必须仍在 root 之内
        if target != self.root and self.root not in target.parents:
            raise PanInvalid(f"远端路径越界：{remote_path!r}")
        return target

    @staticmethod
    def _obj(remote_path: str, target: Path) -> RemoteObject:
        st = target.stat()
        return RemoteObject(
            path=normalize_remote(remote_path),
            size=st.st_size,
            isdir=target.is_dir(),
            mtime=st.st_mtime,
            content_sha256=None if target.is_dir() else sha256_file(target),
        )

    # -- PanClient ---------------------------------------------------------
    def put(self, local_path, remote_path: str, *, overwrite: bool = False) -> RemoteObject:
        src = Path(local_path)
        if not src.is_file():
            raise PanInvalid(f"本地文件不存在：{src}")
        dst = self._abs(remote_path)
        local_hash = sha256_file(src)

        if dst.exists():
            if dst.is_dir():
                raise PanConflict(f"远端同名为目录：{remote_path!r}")
            existing_hash = sha256_file(dst)
            if existing_hash == local_hash:
                # 幂等：内容一致，不重复写，也不改变 mtime
                return self._obj(remote_path, dst)
            if not overwrite:
                raise PanConflict(
                    f"远端已存在不同内容，拒绝覆盖：{remote_path!r} "
                    f"(remote={existing_hash[:12]} local={local_hash[:12]})"
                )

        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)  # 原子替换
        return self._obj(remote_path, dst)

    def get(self, remote_path: str, local_path) -> RemoteObject:
        src = self._abs(remote_path)
        if not src.is_file():
            raise PanNotFound(f"远端文件不存在：{remote_path!r}")
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return self._obj(remote_path, src)

    def list(self, remote_dir: str) -> list[RemoteObject]:
        d = self._abs(remote_dir) if remote_dir not in ("", ".", "/") else self.root
        if not d.exists():
            raise PanNotFound(f"远端目录不存在：{remote_dir!r}")
        if not d.is_dir():
            raise PanInvalid(f"不是目录：{remote_dir!r}")
        base = "" if d == self.root else normalize_remote(remote_dir) + "/"
        out: list[RemoteObject] = []
        for child in sorted(d.iterdir(), key=lambda p: p.name):
            if child.name.endswith(".part"):
                continue  # 未完成的临时文件，不暴露为对象
            out.append(
                RemoteObject(
                    path=base + child.name,
                    size=child.stat().st_size,
                    isdir=child.is_dir(),
                    mtime=child.stat().st_mtime,
                    content_sha256=None if child.is_dir() else sha256_file(child),
                )
            )
        return out

    def stat(self, remote_path: str) -> RemoteObject | None:
        target = self._abs(remote_path)
        if not target.exists():
            return None
        return self._obj(remote_path, target)

    def delete(self, remote_path: str) -> bool:
        target = self._abs(remote_path)
        if not target.exists():
            return False
        if target.is_dir():
            raise PanInvalid(f"本实现只删除文件：{remote_path!r}")
        target.unlink()
        return True
