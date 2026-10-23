"""目录扫描：确定性、只读、可解释（SPEC §6.1）。

必须满足的边界：
- **只读**：只调用 `os.scandir` / `os.stat`，不写、不删、不改 mtime；
- **确定性**：同一目录树两次扫描给出完全相同的顺序（目录名/文件名按 `casefold` 排序），
  这样"重复扫描幂等"才有可复现的证据；
- **不穿越白名单**：符号链接默认不跟随；即使开启跟随，也要
  (1) 解析后的真实路径仍在白名单根内，且 (2) 不进入已访问目录（防止链接环）；
- **循环保护**：即使在没有符号链接的平台上，也用 `(st_dev, st_ino)` 身份集兜底，
  避免目录环导致无限扫描。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class ScanItem:
    """扫描到的一个候选文件（此时尚未读取内容）。"""

    #: 绝对规范化路径（用于读取）
    absolute_path: str
    #: 相对扫描根的路径，统一使用 `/` 作为分隔符（跨平台稳定）
    relative_path: str
    #: 归属的扫描根
    root: str
    size_bytes: int
    #: 修改时间（纳秒），用于增量判定
    mtime_ns: int
    #: 设备内文件身份，用于去重与环检测
    device: int
    inode: int

    @property
    def mtime(self) -> float:
        return self.mtime_ns / 1_000_000_000


@dataclass
class ScanReport:
    """一次扫描的完整结果，包含被跳过/重复的目录项（用于可解释性）。"""

    root: str
    items: List[ScanItem] = field(default_factory=list)
    #: 因策略被跳过的文件（后缀不匹配等），记录原因而不是静默忽略
    skipped: List[Dict[str, str]] = field(default_factory=list)
    #: 因符号链接策略/环检测被跳过的路径
    pruned: List[Dict[str, str]] = field(default_factory=list)
    #: 目录遍历错误（权限等），必须记录
    errors: List[Dict[str, str]] = field(default_factory=list)


def normalize_relative(relative_path: str) -> str:
    """跨平台统一的相对路径写法：`/` 分隔、去掉 `./` 前缀、折叠重复分隔符。"""
    cleaned = relative_path.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    return "/".join(parts)


def _suffix_allowed(name: str, suffixes: Sequence[str]) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix.lower()) for suffix in suffixes)


def _dir_identity(info) -> Optional[tuple]:
    """目录身份：优先 `(st_dev, st_ino)`；平台不给 inode 时返回 None（由真实路径兜底）。"""
    if info.st_ino:
        return (info.st_dev, info.st_ino)
    return None


def _file_identity(info, path: str) -> tuple:
    """文件身份：优先 `(st_dev, st_ino)`；平台不给 inode 时退化为规范化真实路径。

    注意：Windows 上某些文件系统/句柄会返回 `st_ino == 0`，若仍以它做身份，
    **同一个目录下所有文件都会被误判为"同一物理文件"而被丢弃**——这是必须避开的坑。
    """
    if info.st_ino:
        return (info.st_dev, info.st_ino)
    return (info.st_dev, os.path.normcase(os.path.realpath(path)))


def _is_link_safe(entry) -> bool:
    """排序辅助：符号链接排在同名真实条目之后。`is_symlink()` 出错时按"链接"处理（更保守）。"""
    try:
        return bool(entry.is_symlink())
    except OSError:  # pragma: no cover - 极端平台错误
        return True


def _is_within(path: str, root: str) -> bool:
    """路径是否位于根目录内（含根本身）。两边都必须先规范化。"""
    try:
        common = os.path.commonpath([os.path.realpath(path), os.path.realpath(root)])
    except ValueError:  # 不同驱动器
        return False
    return os.path.normcase(common) == os.path.normcase(os.path.realpath(root))


def scan_directory(
    root: str,
    *,
    follow_symlinks: bool = False,
    suffixes: Optional[Sequence[str]] = None,
    max_depth: int = 64,
    current_depth: int = 0,
    _seen_dirs: Optional[set] = None,
    _seen_files: Optional[Dict[tuple, str]] = None,
    _visited_dirs: Optional[set] = None,
) -> ScanReport:
    """扫描单个根目录。

    `_seen_*` / `_visited_dirs` 是内部参数（递归时跨层级共享），调用方不要传。
    """
    report = ScanReport(root=os.path.abspath(root))
    seen_dirs = _seen_dirs if _seen_dirs is not None else set()
    seen_files = _seen_files if _seen_files is not None else {}
    visited_dirs = _visited_dirs if _visited_dirs is not None else set()
    absolute_root = os.path.abspath(root)
    if current_depth > max_depth:
        report.pruned.append({"path": absolute_root, "reason": f"max_depth_exceeded:{max_depth}"})
        return report
    if not os.path.isdir(absolute_root):
        report.errors.append({"path": absolute_root, "reason": "not_a_directory"})
        return report

    def walk(directory: str, depth: int) -> None:
        if depth > max_depth:
            report.pruned.append({"path": directory, "reason": f"max_depth_exceeded:{max_depth}"})
            return
        directory_identity = os.path.normcase(os.path.realpath(directory))
        if directory_identity in visited_dirs:
            report.pruned.append({"path": directory, "reason": "directory_already_visited"})
            return
        visited_dirs.add(directory_identity)
        try:
            scanned = list(os.scandir(directory))
        except OSError as exc:
            report.errors.append({"path": directory, "reason": f"scandir_failed:{exc.__class__.__name__}:{exc.strerror}"})
            return
        # 排序规则：**真实条目优先于符号链接**，同类再按名字排序。
        # 这样"同一物理目录只能走一条路径"的结果是确定的——先走真实路径，
        # 后遇到的别名才被剪掉；否则在某些平台上别名先被遍历，目录内文件
        # 就会以 `alias/...` 的形式出现，同一份资料出现两种相对路径。
        entries = sorted(scanned, key=lambda entry: (_is_link_safe(entry), entry.name.casefold()))

        for entry in entries:
            path = entry.path
            try:
                entry_is_link = entry.is_symlink()
            except OSError as exc:
                report.errors.append({"path": path, "reason": f"is_symlink_failed:{exc.__class__.__name__}"})
                continue

            if entry_is_link and not follow_symlinks:
                report.pruned.append({"path": path, "reason": "symlink_not_followed"})
                continue
            if entry_is_link and not _is_within(path, absolute_root):
                report.pruned.append({"path": path, "reason": "symlink_escapes_root"})
                continue

            try:
                info = entry.stat(follow_symlinks=follow_symlinks)
            except OSError as exc:
                report.errors.append({"path": path, "reason": f"stat_failed:{exc.__class__.__name__}"})
                continue

            if stat.S_ISDIR(info.st_mode):
                identity = _dir_identity(info)
                if identity is not None:
                    if identity in seen_dirs:
                        report.pruned.append({"path": path, "reason": "directory_already_visited"})
                        continue
                    seen_dirs.add(identity)
                walk(path, depth + 1)
                continue

            if not stat.S_ISREG(info.st_mode):
                report.skipped.append({"path": path, "reason": f"not_a_regular_file:mode={oct(info.st_mode)}"})
                continue

            if suffixes is not None and not _suffix_allowed(entry.name, suffixes):
                report.skipped.append({"path": path, "reason": f"suffix_not_in_allowlist:{os.path.splitext(entry.name)[1].lower()}"})
                continue

            identity = _file_identity(info, path)
            relative = normalize_relative(os.path.relpath(path, absolute_root))
            previous = seen_files.get(identity)
            if previous is not None:
                # 同一物理文件被两条路径（符号链接）指向：只采集一次，另一条记录原因。
                report.pruned.append({"path": path, "reason": f"duplicate_of:{previous}"})
                continue
            seen_files[identity] = relative
            report.items.append(
                ScanItem(
                    absolute_path=os.path.abspath(path),
                    relative_path=relative,
                    root=absolute_root,
                    size_bytes=int(info.st_size),
                    mtime_ns=int(info.st_mtime_ns),
                    device=int(info.st_dev),
                    inode=int(info.st_ino),
                )
            )

    walk(absolute_root, current_depth)
    return report


def scan_roots(
    roots: Sequence[str],
    *,
    follow_symlinks: bool = False,
    suffixes: Optional[Sequence[str]] = None,
    max_depth: int = 64,
) -> List[ScanReport]:
    """扫描多个根目录，跨根去重（同一物理文件只出现一次）。"""
    seen_dirs: set = set()
    seen_files: Dict[tuple, str] = {}
    visited_dirs: set = set()
    reports: List[ScanReport] = []
    for root in roots:
        reports.append(
            scan_directory(
                root,
                follow_symlinks=follow_symlinks,
                suffixes=suffixes,
                max_depth=max_depth,
                _seen_dirs=seen_dirs,
                _seen_files=seen_files,
                _visited_dirs=visited_dirs,
            )
        )
    return reports


__all__ = ["ScanItem", "ScanReport", "scan_directory", "scan_roots", "normalize_relative"]
