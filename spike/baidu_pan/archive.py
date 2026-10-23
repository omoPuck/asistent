"""归档 manifest、事务归档流程与隔离恢复（SPEC §12.4 / §21.4 契约）。

零第三方依赖（仅标准库）。传输层通过 `panclient.PanClient` 注入，因此同一份
契约代码既可以在本地目录模拟远端上跑，也可以接真实网盘实现。

本模块保证的契约（SPEC §12.5 / §21.4 / MVP-7）
------------------------------------------------
1. **上传前生成不可变 manifest**：`archive_id`、逐文件 sha256、大小、来源版本、
   策略版本、代码/规则/流程版本。manifest 字节一旦生成即冻结，其 sha256 即
   `manifest_digest`（manifest 内部不自引用摘要，避免自指）。
2. **上传后逐文件内容哈希回读校验**：每个对象上传后**立即下载回读**并重算
   sha256；只有本 attempt 内全部对象都通过回读校验，才写完成标记 `_COMPLETE.json`。
   复用的对象（上次 attempt 已上传）也必须在本 attempt 内回读校验，绝不凭
   本地状态文件“信任”。
3. **archive_id 幂等**：同一 `archive_id` 重试复用未完成对象；远端已存在**内容
   不同**的对象时，只有在“该 archive 尚未完成”时才允许修复（因为它属于同一
   archive_id 的路径，属于未完成对象）；一旦出现完成标记，任何内容不一致一律
   判定为篡改/冲突并拒绝覆盖。内容变化 -> 新 `archive_id`（旧归档不动）。
4. **部分失败 -> `ARCHIVE_PENDING`**：记录已成功/未成功对象，不写完成标记，
   可安全重试。
5. **恢复先到隔离目录**：逐文件校验哈希、校验 manifest 引用闭合，绝不覆盖主数据，
   默认进入只读恢复模式并声明历史审批失效。
6. **结果未知不盲目重试**：`PanUnavailable` 被记录为未成功对象，重试时先 `stat`
   + 回读判定真实状态，再决定是否重传。

有意不做的事（保持在 Spike 范围外）
------------------------------------
- 不做秘密扫描（SPEC §21.4 要求打包后执行秘密扫描；本 Spike 只预留
  `ArchivePolicy.exclude_globs` 与 `secret_scan` 钩子，不实现扫描器）。
- 不做配额/保留策略清理（§21.4 的 30 天/12 个月策略）。
- 不做 Approval/ArchivePolicy 的审批绑定（那是基础契约的职责）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from panclient import (
    PanClient,
    PanConflict,
    PanError,
    PanInvalid,
    PanNotFound,
    PanUnavailable,
    RemoteObject,
    normalize_remote,
    sha256_file,
)

__all__ = [
    "MANIFEST_SCHEMA",
    "ARCHIVE_PENDING",
    "ARCHIVE_VERIFIED",
    "CodeVersion",
    "ArchivePolicy",
    "FileEntry",
    "Manifest",
    "ArchiveLayout",
    "ArchiveOutcome",
    "RestoreOutcome",
    "build_manifest",
    "capture_snapshot",
    "iter_source_files",
    "ArchiveStore",
    "canonical_json",
]

MANIFEST_SCHEMA = "asistent.archive-manifest/0.1"
ARCHIVE_PENDING = "ARCHIVE_PENDING"   # SPEC §12.5「待归档」
ARCHIVE_VERIFIED = "ARCHIVE_VERIFIED"  # 校验通过、完成标记已写


# --------------------------------------------------------------------------
# 规范化 JSON（稳定字节序列 = 稳定摘要）
# --------------------------------------------------------------------------
def canonical_json(obj: Any) -> str:
    """确定性 JSON：键排序、紧凑分隔、保留非 ASCII（可读中文路径）。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# manifest 组成
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CodeVersion:
    """归档时生效的代码/规则/流程版本（SPEC §21.4）。"""

    asistent: str = "unknown"
    rules: str = "unknown"
    workflow: str = "unknown"

    def to_dict(self) -> dict[str, str]:
        return {"asistent": self.asistent, "rules": self.rules, "workflow": self.workflow}


@dataclass(frozen=True)
class ArchivePolicy:
    """ArchivePolicy 的可归档快照（SPEC §21.3）。

    只放入与归档内容/位置相关的字段，凭据、账号信息一律不入 manifest。
    """

    policy_id: str
    policy_version: int
    logical_root: str = "个人助手归档/事务归档"
    restore_record_root: str = "个人助手归档/恢复记录"
    exclude_globs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "logical_root": self.logical_root,
            "restore_record_root": self.restore_record_root,
            "exclude_globs": list(self.exclude_globs),
        }


@dataclass(frozen=True)
class FileEntry:
    relpath: str
    size: int
    sha256: str
    source_revision: str
    mtime_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "relpath": self.relpath,
            "size": self.size,
            "sha256": self.sha256,
            "source_revision": self.source_revision,
            "mtime_ns": self.mtime_ns,
        }


@dataclass(frozen=True)
class Manifest:
    """不可变归档清单。

    `archive_id` 由 manifest 的**身份字段**（除 created_at / host 之外的全部字段）
    确定性派生：相同输入 -> 相同 archive_id（重试复用）；内容、来源版本、策略版本、
    代码/规则/流程版本任一变化 -> 新 archive_id（旧归档不动）。
    """

    archive_id: str
    schema_version: str
    created_at: str
    source_kind: str
    source_id: str
    source_revision: str
    policy: ArchivePolicy
    code_version: CodeVersion
    files: tuple[FileEntry, ...]
    host: str = "unknown"

    # -- 序列化 ------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "archive_id": self.archive_id,
            "created_at": self.created_at,
            "host": self.host,
            "source": {
                "kind": self.source_kind,
                "source_id": self.source_id,
                "source_revision": self.source_revision,
            },
            "policy": self.policy.to_dict(),
            "code_version": self.code_version.to_dict(),
            "object_count": len(self.files),
            "total_size": sum(f.size for f in self.files),
            "files": [f.to_dict() for f in self.files],
        }

    def to_bytes(self) -> bytes:
        """冻结的 manifest 字节。其 sha256 即 manifest_digest。"""
        return (canonical_json(self.to_dict()) + "\n").encode("utf-8")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    def file_map(self) -> dict[str, FileEntry]:
        return {f.relpath: f for f in self.files}


def iter_source_files(
    source_dir: str | os.PathLike[str],
    relpaths: Sequence[str] | None = None,
) -> list[str]:
    """列出要归档的相对路径（`relpaths=None` 表示全部普通文件）。"""
    root = Path(source_dir).resolve()
    if not root.is_dir():
        raise PanInvalid(f"源目录不存在或不是目录：{root}")
    if relpaths is not None:
        return sorted({normalize_remote(r) for r in relpaths})

    candidates: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".part"):
                continue
            candidates.append(_relpath(root, Path(dirpath) / name))
    return sorted(set(candidates))


def capture_snapshot(
    source_dir: str | os.PathLike[str],
    stage_dir: str | os.PathLike[str],
    *,
    relpaths: Sequence[str] | None = None,
    source_revision: str = "unknown",
) -> tuple[list[FileEntry], Path]:
    """把源文件**冻结拷贝**到 `stage_dir`，并基于拷贝计算 manifest 条目。

    SPEC §21.4：“上传中本地文件发生修改时仍上传已捕获快照；变化进入下一快照。”
    因此 manifest 的哈希与上传字节都取自这份冻结快照，而不是随时可能被改写的
    源文件。`stage_dir` 必须在仓库外/被忽略的目录（跨盘拷贝会产生额外磁盘占用）。

    返回 `(entries, stage_snapshot_root)`；`stage_snapshot_root` 就是上传源根目录。
    """
    root = Path(source_dir).resolve()
    stage = Path(stage_dir).resolve() / "snapshot"
    entries: list[FileEntry] = []
    for rel in iter_source_files(root, relpaths):
        src = root / PurePosixPath(rel)
        if not src.is_file():
            raise PanInvalid(f"归档源文件不存在：{rel}")
        st = src.stat()
        dst = stage / PurePosixPath(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 先拷贝再对**拷贝**取哈希：即使源在拷贝过程中被改写，manifest 描述的是
        # 这份快照实际落地的字节；随后 upload 阶段会用同一份快照再校验一次。
        shutil.copyfile(src, dst)
        entries.append(
            FileEntry(
                relpath=rel,
                size=dst.stat().st_size,
                sha256=sha256_file(dst),
                source_revision=source_revision,
                mtime_ns=st.st_mtime_ns,
            )
        )
    return entries, stage


def build_manifest(
    *,
    source_kind: str,
    source_id: str,
    source_revision: str,
    policy: ArchivePolicy,
    code_version: CodeVersion,
    entries: Sequence[FileEntry] | None = None,
    source_dir: str | os.PathLike[str] | None = None,
    relpaths: Sequence[str] | None = None,
    created_at: str | None = None,
    host: str | None = None,
) -> Manifest:
    """生成不可变 manifest。

    两种用法：
    - 传入 `entries`（来自 `capture_snapshot`）——推荐，哈希基于冻结快照；
    - 传入 `source_dir` ——直接按当前磁盘内容取材（用于只读校验场景）。

    `relpaths=None` 表示归档 `source_dir` 下全部普通文件（跳过 `.part`/`__pycache__`）。
    """
    if entries is None:
        if source_dir is None:
            raise PanInvalid("build_manifest 需要 entries 或 source_dir 之一")
        root = Path(source_dir).resolve()
        computed: list[FileEntry] = []
        for rel in iter_source_files(root, relpaths):
            abs_path = root / PurePosixPath(rel)
            if not abs_path.is_file():
                raise PanInvalid(f"归档源文件不存在：{rel}")
            st = abs_path.stat()
            computed.append(
                FileEntry(
                    relpath=rel,
                    size=st.st_size,
                    sha256=sha256_file(abs_path),
                    source_revision=source_revision,
                    mtime_ns=st.st_mtime_ns,
                )
            )
        entries = computed

    entries = tuple(sorted(entries, key=lambda e: e.relpath))

    identity = {
        "schema_version": MANIFEST_SCHEMA,
        "source": {"kind": source_kind, "source_id": source_id, "source_revision": source_revision},
        "policy": policy.to_dict(),
        "code_version": code_version.to_dict(),
        "files": [e.to_dict() for e in entries],
    }
    archive_id = "a" + _sha256_text(canonical_json(identity))[:22]

    return Manifest(
        archive_id=archive_id,
        schema_version=MANIFEST_SCHEMA,
        created_at=created_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        source_kind=source_kind,
        source_id=source_id,
        source_revision=source_revision,
        policy=policy,
        code_version=code_version,
        files=tuple(entries),
        host=host or _hostname(),
    )


def _hostname() -> str:
    try:
        import socket

        return socket.gethostname()
    except Exception:  # pragma: no cover
        return "unknown"


def _relpath(root: Path, path: Path) -> str:
    return PurePosixPath(path.resolve().relative_to(root).as_posix()).as_posix()


# --------------------------------------------------------------------------
# 远端布局（对应 SPEC §12.4 建议的逻辑目录）
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ArchiveLayout:
    """远端目录布局。`root` 是媒体库根（真实环境里在 `/apps/bdpan/` 之下）。"""

    root: str = "个人助手归档/事务归档"
    restore_record_root: str = "个人助手归档/恢复记录"

    def archive_dir(self, archive_id: str) -> str:
        return f"{self.root}/{archive_id}"

    def object_path(self, archive_id: str, relpath: str) -> str:
        return f"{self.archive_dir(archive_id)}/objects/{relpath}"

    def manifest_path(self, archive_id: str) -> str:
        return f"{self.archive_dir(archive_id)}/manifest.json"

    def complete_path(self, archive_id: str) -> str:
        return f"{self.archive_dir(archive_id)}/_COMPLETE.json"

    def restore_record_path(self, restore_id: str) -> str:
        return f"{self.restore_record_root}/{restore_id}.json"


# --------------------------------------------------------------------------
# 结果对象
# --------------------------------------------------------------------------
@dataclass
class ArchiveOutcome:
    archive_id: str
    state: str
    manifest_digest: str
    object_count: int
    total_size: int
    uploaded: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    readback_verified: list[str] = field(default_factory=list)
    unknown_objects: list[str] = field(default_factory=list)
    marker_written: bool = False
    already_complete: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == ARCHIVE_VERIFIED and not self.failed

    def summary(self) -> str:
        return (
            f"archive_id={self.archive_id} state={self.state} "
            f"objects={self.object_count} uploaded={len(self.uploaded)} "
            f"reused={len(self.reused)} repaired={len(self.repaired)} "
            f"failed={len(self.failed)} readback_verified={len(self.readback_verified)} "
            f"marker={'yes' if self.marker_written else 'no'}"
        )


@dataclass
class RestoreOutcome:
    archive_id: str
    restore_id: str
    dest: str
    state: str
    file_count: int
    verified: list[str] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    manifest_digest: str = ""
    manifest_digest_ok: bool = False
    closure_ok: bool = False
    record_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == "RESTORE_VERIFIED" and not self.failed


# --------------------------------------------------------------------------
# 归档/恢复主体
# --------------------------------------------------------------------------
class ArchiveStore:
    """事务归档与隔离恢复。

    `pan`       : PanClient 实现（本地目录模拟 / 真实网盘适配）
    `layout`    : 远端布局
    `state_dir` : 本地状态目录（**必须**在仓库外或 var/ 之类被忽略的目录；默认临时目录）
    """

    def __init__(
        self,
        pan: PanClient,
        layout: ArchiveLayout | None = None,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        keep_local_state: bool = True,
    ) -> None:
        self.pan = pan
        self.layout = layout or ArchiveLayout()
        self.state_dir = Path(
            state_dir or tempfile.mkdtemp(prefix="asistent-pan-state-")
        ).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.keep_local_state = keep_local_state

    # ---- 本地状态（不参与可信判定，仅用于可观察性/审计）--------------------
    def _state_path(self, archive_id: str) -> Path:
        return self.state_dir / f"{archive_id}.json"

    def _write_state(self, outcome: ArchiveOutcome) -> None:
        if not self.keep_local_state:
            return
        payload = asdict(outcome)
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        tmp = self._state_path(outcome.archive_id).with_suffix(".json.tmp")
        tmp.write_text(canonical_json(payload) + "\n", encoding="utf-8")
        os.replace(tmp, self._state_path(outcome.archive_id))

    # ---- 回读校验 --------------------------------------------------------
    def _readback_verify(self, remote_path: str, expected_sha256: str) -> tuple[bool, str]:
        """下载回读并重算 sha256。返回 (是否一致, 实际哈希或错误说明)。"""
        with tempfile.TemporaryDirectory(prefix="asistent-pan-readback-") as td:
            local = Path(td) / "object.bin"
            try:
                self.pan.get(remote_path, local)
            except PanNotFound as exc:
                return False, f"missing:{exc}"
            except PanUnavailable as exc:
                return False, f"unknown:{exc}"
            actual = sha256_file(local)
            return actual == expected_sha256, actual

    # ---- 上传单个对象（含复用/修复判定）----------------------------------
    def _ensure_object(
        self,
        *,
        archive_id: str,
        relpath: str,
        entry: FileEntry,
        local_path: Path,
        archive_completed: bool,
        outcome: ArchiveOutcome,
    ) -> bool:
        remote = self.layout.object_path(archive_id, relpath)
        existing: RemoteObject | None
        try:
            existing = self.pan.stat(remote)
        except PanUnavailable as exc:
            # 结果未知：不猜测，记录为未成功对象
            outcome.unknown_objects.append(relpath)
            outcome.failed.append({"relpath": relpath, "reason": "stat-unknown", "detail": str(exc)})
            return False

        if existing is not None:
            if archive_completed:
                # 归档已完成：任何内容不一致都视为篡改/冲突，绝不覆盖
                same, actual = self._readback_verify(remote, entry.sha256)
                if not same:
                    raise PanConflict(
                        f"归档 {archive_id} 已完成，但远端对象内容与 manifest 不一致："
                        f"{remote}（expected={entry.sha256[:12]} actual={actual[:12]}）；拒绝覆盖"
                    )
                outcome.reused.append(relpath)
                outcome.readback_verified.append(relpath)
                return True

            # 归档未完成：先回读判定，内容一致则直接复用（幂等重试）
            same, actual = self._readback_verify(remote, entry.sha256)
            if same:
                outcome.reused.append(relpath)
                outcome.readback_verified.append(relpath)
                return True
            # 内容不一致但归档未完成 -> 属于本 archive_id 路径下的未完成/损坏对象，允许修复
            outcome.notes.append(f"repair-required {relpath}: expected={entry.sha256[:12]} actual={actual[:12]}")
            try:
                self.pan.put(local_path, remote, overwrite=True)
            except PanUnavailable as exc:
                outcome.unknown_objects.append(relpath)
                outcome.failed.append({"relpath": relpath, "reason": "put-unknown", "detail": str(exc)})
                return False
            except PanError as exc:
                outcome.failed.append({"relpath": relpath, "reason": "put-error", "detail": str(exc)})
                return False
            outcome.repaired.append(relpath)

        else:
            try:
                self.pan.put(local_path, remote, overwrite=False)
            except PanUnavailable as exc:
                # 结果未知：不能判定成功或失败；留待下次 attempt 用 stat+回读判定
                outcome.unknown_objects.append(relpath)
                outcome.failed.append({"relpath": relpath, "reason": "put-unknown", "detail": str(exc)})
                return False
            except PanError as exc:
                outcome.failed.append({"relpath": relpath, "reason": "put-error", "detail": str(exc)})
                return False
            outcome.uploaded.append(relpath)

        # 上传/修复后立即逐文件回读校验
        same, actual = self._readback_verify(remote, entry.sha256)
        if not same:
            outcome.failed.append(
                {
                    "relpath": relpath,
                    "reason": "verify-mismatch",
                    "detail": f"expected={entry.sha256} actual={actual}",
                }
            )
            return False
        outcome.readback_verified.append(relpath)
        return True

    def _ensure_manifest_object(self, manifest: Manifest, archive_completed: bool) -> None:
        remote = self.layout.manifest_path(manifest.archive_id)
        with tempfile.TemporaryDirectory(prefix="asistent-pan-manifest-") as td:
            local = Path(td) / "manifest.json"
            local.write_bytes(manifest.to_bytes())
            existing = self.pan.stat(remote)
            if existing is not None:
                same, actual = self._readback_verify(remote, manifest.digest)
                if same:
                    return
                if archive_completed:
                    raise PanConflict(
                        f"归档 {manifest.archive_id} 已完成，但远端 manifest 摘要不一致"
                        f"（expected={manifest.digest[:12]} actual={actual[:12]}）；拒绝覆盖"
                    )
                self.pan.put(local, remote, overwrite=True)
            else:
                self.pan.put(local, remote, overwrite=False)

            same, actual = self._readback_verify(remote, manifest.digest)
            if not same:
                raise PanConflict(
                    f"manifest 回读校验失败：{remote}（expected={manifest.digest[:12]} actual={actual[:12]}）"
                )

    # ---- 完成标记 --------------------------------------------------------
    def _read_complete_marker(self, archive_id: str) -> dict[str, Any] | None:
        remote = self.layout.complete_path(archive_id)
        try:
            if self.pan.stat(remote) is None:
                return None
        except PanUnavailable as exc:
            raise PanUnavailable(f"无法确认完成标记状态：{remote}：{exc}") from exc
        with tempfile.TemporaryDirectory(prefix="asistent-pan-marker-") as td:
            local = Path(td) / "complete.json"
            self.pan.get(remote, local)
            return json.loads(local.read_text(encoding="utf-8"))

    def _write_complete_marker(self, manifest: Manifest, outcome: ArchiveOutcome) -> None:
        payload = {
            "schema_version": MANIFEST_SCHEMA,
            "archive_id": manifest.archive_id,
            "manifest_digest": manifest.digest,
            "object_count": len(manifest.files),
            "total_size": manifest.total_size,
            "policy_id": manifest.policy.policy_id,
            "policy_version": manifest.policy.policy_version,
            "code_version": manifest.code_version.to_dict(),
            "source": {
                "kind": manifest.source_kind,
                "source_id": manifest.source_id,
                "source_revision": manifest.source_revision,
            },
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "verification": "per-file-content-sha256-readback",
        }
        remote = self.layout.complete_path(manifest.archive_id)
        with tempfile.TemporaryDirectory(prefix="asistent-pan-marker-") as td:
            local = Path(td) / "complete.json"
            local.write_text(canonical_json(payload) + "\n", encoding="utf-8")
            # 完成标记只在完成时写一次。已存在时按**身份**（archive_id + manifest_digest）
            # 判定是否等价，而不是比较重新生成的字节——标记里的 verified_at 每次都会变，
            # 按字节比较会把幂等重试误判成冲突。
            existing = self.pan.stat(remote)
            if existing is not None:
                readback = Path(td) / "existing.json"
                try:
                    self.pan.get(remote, readback)
                except PanUnavailable as exc:
                    raise PanUnavailable(f"完成标记已存在但无法回读校验：{remote}：{exc}") from exc
                try:
                    data = json.loads(readback.read_text(encoding="utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise PanConflict(f"完成标记内容不可解析：{remote}：{exc}") from exc
                if data.get("archive_id") == manifest.archive_id and data.get("manifest_digest") == manifest.digest:
                    outcome.notes.append("complete-marker-already-present")
                    return
                raise PanConflict(
                    f"完成标记已存在但指向另一份 manifest：{remote}"
                    f"（existing={str(data.get('manifest_digest'))[:12]} "
                    f"local={manifest.digest[:12]}）；拒绝覆盖"
                )
            self.pan.put(local, remote, overwrite=False)
            same, _ = self._readback_verify(remote, hashlib.sha256(local.read_bytes()).hexdigest())
            if not same:
                raise PanUnavailable("完成标记回读校验失败，状态未知")
        outcome.marker_written = True

    # ---- 公开 API --------------------------------------------------------
    def archive(self, manifest: Manifest, source_dir: str | os.PathLike[str]) -> ArchiveOutcome:
        """执行一次归档 attempt。可安全重复调用（幂等）。"""
        src_root = Path(source_dir).resolve()
        outcome = ArchiveOutcome(
            archive_id=manifest.archive_id,
            state=ARCHIVE_PENDING,
            manifest_digest=manifest.digest,
            object_count=len(manifest.files),
            total_size=manifest.total_size,
        )

        marker = self._read_complete_marker(manifest.archive_id)
        archive_completed = False
        if marker is not None:
            if marker.get("manifest_digest") != manifest.digest:
                raise PanConflict(
                    f"archive_id 冲突：远端完成标记的 manifest_digest "
                    f"{str(marker.get('manifest_digest'))[:12]} != 本地 {manifest.digest[:12]}。"
                    "同一 archive_id 绑定不同 manifest 属受控变更，必须人工裁决，拒绝覆盖。"
                )
            archive_completed = True
            outcome.already_complete = True
            outcome.notes.append("previously-completed; re-verifying objects")

        # 1) objects：上传 + 逐文件回读校验
        for entry in manifest.files:
            local_path = src_root / PurePosixPath(entry.relpath)
            if not local_path.is_file():
                outcome.failed.append(
                    {"relpath": entry.relpath, "reason": "local-missing", "detail": str(local_path)}
                )
                continue
            if local_path.stat().st_size != entry.size or sha256_file(local_path) != entry.sha256:
                # 本地在 manifest 生成后被改动：上传已捕获快照的要求无法满足 -> 记录并跳过
                outcome.failed.append(
                    {
                        "relpath": entry.relpath,
                        "reason": "local-changed-after-manifest",
                        "detail": "manifest 冻结后本地文件发生变化；该变化应进入下一份快照",
                    }
                )
                continue
            self._ensure_object(
                archive_id=manifest.archive_id,
                relpath=entry.relpath,
                entry=entry,
                local_path=local_path,
                archive_completed=archive_completed,
                outcome=outcome,
            )

        # 2) 未成功对象 -> 保持待归档，不写完成标记
        if outcome.failed:
            outcome.state = ARCHIVE_PENDING
            self._write_state(outcome)
            return outcome

        # 3) manifest 对象（在全部内容对象校验通过之后）
        self._ensure_manifest_object(manifest, archive_completed)

        # 4) 完成标记（最后一步）
        self._write_complete_marker(manifest, outcome)

        # 5) 完整性断言：本 attempt 内每个对象都通过回读校验
        if len(set(outcome.readback_verified)) != len(manifest.files):
            outcome.state = ARCHIVE_PENDING
            outcome.notes.append("assertion-failed: readback coverage incomplete")
            self._write_state(outcome)
            return outcome

        outcome.state = ARCHIVE_VERIFIED
        self._write_state(outcome)
        return outcome

    def verify_archive(self, archive_id: str) -> dict[str, Any]:
        """只读校验已归档的 archive：远端是否存在、逐文件哈希是否一致。"""
        report: dict[str, Any] = {
            "archive_id": archive_id,
            "manifest_present": False,
            "manifest_digest": None,
            "marker_present": False,
            "missing": [],
            "mismatched": [],
            "verified": [],
            "consistent": False,
        }
        manifest_remote = self.layout.manifest_path(archive_id)
        if self.pan.stat(manifest_remote) is None:
            return report
        report["manifest_present"] = True

        with tempfile.TemporaryDirectory(prefix="asistent-pan-verify-") as td:
            local = Path(td) / "manifest.json"
            self.pan.get(manifest_remote, local)
            raw = local.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            report["manifest_digest"] = digest
            data = json.loads(raw.decode("utf-8"))
            for entry in data.get("files", []):
                rel = entry["relpath"]
                remote = self.layout.object_path(archive_id, rel)
                if self.pan.stat(remote) is None:
                    report["missing"].append(rel)
                    continue
                same, actual = self._readback_verify(remote, entry["sha256"])
                if same:
                    report["verified"].append(rel)
                else:
                    report["mismatched"].append(
                        {"relpath": rel, "expected": entry["sha256"], "actual": actual}
                    )

        marker = self._read_complete_marker(archive_id)
        report["marker_present"] = marker is not None
        if marker is not None:
            report["marker_manifest_digest"] = marker.get("manifest_digest")
            report["marker_digest_matches"] = marker.get("manifest_digest") == report["manifest_digest"]
        report["consistent"] = (
            report["manifest_present"]
            and not report["missing"]
            and not report["mismatched"]
            and report["marker_present"]
            and report.get("marker_digest_matches", False)
        )
        return report

    def restore(
        self,
        archive_id: str,
        dest_root: str | os.PathLike[str],
        *,
        protected_roots: Sequence[str | os.PathLike[str]],
        publish_record: bool = False,
    ) -> RestoreOutcome:
        """恢复到**隔离目录**，逐文件校验哈希，绝不覆盖主数据。

        - `protected_roots`：**必填**。主数据/源资料目录；恢复目标与之重叠时直接拒绝。
          刻意做成必填参数而不是可选开关：SPEC §12.5 / §21.4 的“不得直接覆盖现有主数据”
          是硬约束，隔离保证不应该由调用方“记得打开”。
        - `publish_record`：是否把恢复记录也写到远端 `恢复记录/`。真实网盘默认关闭，
          需由 ArchivePolicy 显式授权后再打开（SPEC §21.3）。
        """
        dest_root_p = Path(dest_root).resolve()
        restore_id = "r" + hashlib.sha256(
            f"{archive_id}|{time.time_ns()}|{_hostname()}".encode("utf-8")
        ).hexdigest()[:14]
        dest = dest_root_p / archive_id
        out = RestoreOutcome(
            archive_id=archive_id,
            restore_id=restore_id,
            dest=str(dest),
            state="RESTORE_FAILED",
            file_count=0,
        )

        # 保护：绝不写进主数据/源目录（双向重叠都拒绝）
        if not protected_roots:
            raise PanInvalid(
                "restore 必须显式传入 protected_roots（主数据目录）；"
                "拒绝在没有隔离边界的情况下恢复"
            )
        for prot in protected_roots:
            prot_p = Path(prot).resolve()
            if dest == prot_p or prot_p in dest.parents or dest in prot_p.parents:
                raise PanInvalid(
                    f"恢复目标 {dest} 与受保护目录 {prot_p} 重叠；拒绝在受保护区域恢复"
                )
        if dest.exists() and any(dest.iterdir()):
            raise PanInvalid(f"恢复目标已存在且非空：{dest}；拒绝覆盖")
        dest.mkdir(parents=True, exist_ok=True)

        # 1) manifest
        manifest_remote = self.layout.manifest_path(archive_id)
        if self.pan.stat(manifest_remote) is None:
            out.notes.append("manifest 缺失：远端归档不存在，拒绝完成恢复（SPEC AT-14）")
            out.record_paths.append(str(self._write_local_record(dest, out)))
            return out

        local_manifest = dest / "manifest.json"
        self.pan.get(manifest_remote, local_manifest)
        raw = local_manifest.read_bytes()
        out.manifest_digest = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw.decode("utf-8"))

        marker = self._read_complete_marker(archive_id)
        if marker is not None:
            out.manifest_digest_ok = marker.get("manifest_digest") == out.manifest_digest
        else:
            out.notes.append("远端无完成标记：该归档未通过完整性校验，恢复结果不可信")

        # 2) 逐文件下载 + 哈希校验
        objects_dir = dest / "objects"
        expected: set[str] = set()
        for entry in data.get("files", []):
            rel = entry["relpath"]
            expected.add(rel)
            target = objects_dir / PurePosixPath(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            remote = self.layout.object_path(archive_id, rel)
            try:
                self.pan.get(remote, target)
            except PanNotFound:
                out.failed.append({"relpath": rel, "reason": "remote-missing"})
                continue
            except PanUnavailable as exc:
                out.failed.append({"relpath": rel, "reason": "unknown", "detail": str(exc)})
                continue
            actual = sha256_file(target)
            if actual != entry["sha256"]:
                out.failed.append(
                    {"relpath": rel, "reason": "hash-mismatch", "expected": entry["sha256"], "actual": actual}
                )
            else:
                out.verified.append(rel)

        # 3) 引用闭合：manifest 里的每个对象都要落地
        found = {
            PurePosixPath(p.relative_to(objects_dir).as_posix()).as_posix()
            for p in objects_dir.rglob("*")
            if p.is_file()
        }
        out.closure_ok = expected == found
        if not out.closure_ok:
            out.notes.append(
                f"引用不闭合：missing={sorted(expected - found)} extra={sorted(found - expected)}"
            )

        out.file_count = len(out.verified)
        if not out.failed and out.closure_ok and out.manifest_digest_ok:
            out.state = "RESTORE_VERIFIED"
        else:
            out.state = "RESTORE_FAILED"

        # 4) 只读恢复模式标记：历史审批默认失效，绝不重放归档中的发送/上传/审批
        (dest / "RESTORE_MODE.txt").write_text(
            "read_only\n"
            "恢复自百度网盘归档：默认进入只读恢复模式。\n"
            "归档中的发送/上传/审批不重放；恢复后的历史审批默认失效（SPEC §21.4）。\n"
            f"archive_id={archive_id} restore_id={restore_id} state={out.state}\n",
            encoding="utf-8",
        )
        out.record_paths.append(str(self._write_local_record(dest, out)))
        if publish_record:
            out.record_paths.extend(self._publish_restore_record(out))
        return out

    # ---- 恢复记录 --------------------------------------------------------
    def _write_local_record(self, dest: Path, out: RestoreOutcome) -> Path:
        payload = asdict(out)
        payload["state"] = out.state
        payload["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        path = dest / "RESTORE_RECORD.json"
        path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
        return path

    def _publish_restore_record(self, out: RestoreOutcome) -> list[str]:
        remote = self.layout.restore_record_path(out.restore_id)
        with tempfile.TemporaryDirectory(prefix="asistent-pan-record-") as td:
            local = Path(td) / "record.json"
            payload = asdict(out)
            payload["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            local.write_text(canonical_json(payload) + "\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            self.pan.put(local, remote, overwrite=False)
            same, _ = self._readback_verify(remote, digest)
            if not same:
                raise PanUnavailable(f"恢复记录回读校验失败：{remote}")
        return [f"pan:{remote}"]
