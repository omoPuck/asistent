"""隔离恢复（SPEC §12.5、§21.4 第 5 条；契约 `restore_plan`）。

硬约束，全部由代码而不是"调用方记得"来保证：
1. **`protected_roots` 必填**：恢复目标与主数据/源资料目录双向重叠 → 直接拒绝；
2. **默认只读**：恢复过程把通道写入锁死（`lock_writes()`），恢复期间**不存在**上传/删除动作，
   更不重放归档里的发送/上传/审批（`replay_external_actions=False`）；
3. **逐文件哈希校验 + 引用闭合**：每个文件都要与 manifest 的 sha256 一致，
   落盘后目录里不能出现 manifest 之外的文件；
4. **投影条目如实标注**：`METADATA_PROJECTION` 条目只能证明"投影重建一致"（契约 §12.2）；
5. **不覆盖已有主数据**：目标位置已有**不同内容**的文件 → 记冲突并拒绝覆盖（`CONFLICT`）；
6. 生成恢复记录（本地必写；远端发布需显式授权），并在有 store 时落 `RestoreJob`。
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..contracts.archive import restore_plan
from ..contracts.enums import RestoreState
from ..contracts.errors import ValidationError
from ..contracts.ids import digest_of, new_id, sha256_file
from ..contracts.objects import RestoreJob
from ..contracts.timeutil import utcnow_iso
from ..pan.errors import PanNotAuthorized, PanUnavailable, PanWriteLocked
from .engine import ArchiveEngine, ArchiveLayout
from .manifest import (
    integrity_claim,
    manifest_digest_of,
    validate_manifest,
)

__all__ = ["RESTORE_RECORD_NAME", "RestoreOutcome", "restore_archive"]

RESTORE_RECORD_NAME = "RESTORE_RECORD.json"


@dataclass
class RestoreOutcome:
    archive_id: str
    restore_id: str
    isolate_dir: str
    state: str
    file_count: int = 0
    verified: List[str] = field(default_factory=list)
    projection_only: List[str] = field(default_factory=list)
    hash_mismatches: List[Dict[str, Any]] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    unknown: List[str] = field(default_factory=list)
    manifest_digest: str = ""
    manifest_digest_ok: bool = False
    closure_ok: bool = False
    extra_files: List[str] = field(default_factory=list)
    read_only_mode: bool = True
    writes_locked_during_restore: bool = False
    reindex_done: bool = False
    reindex_detail: Dict[str, Any] = field(default_factory=dict)
    approvals_invalidated: bool = True
    replay_external_actions: bool = False
    record_paths: List[str] = field(default_factory=list)
    integrity_claim: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_text: Optional[str] = None
    next_step: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == RestoreState.VERIFIED.value and not (
            self.hash_mismatches or self.missing or self.conflicts or self.unknown
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload

    def summary(self) -> str:
        return (
            f"restore_id={self.restore_id} archive_id={self.archive_id} state={self.state} "
            f"files={self.file_count} verified={len(self.verified)} mismatched={len(self.hash_mismatches)} "
            f"missing={len(self.missing)} conflicts={len(self.conflicts)} reindex={'yes' if self.reindex_done else 'no'} "
            f"read_only={self.read_only_mode}"
        )


def _same_or_nested(left: str, right: str) -> bool:
    left_abs = os.path.abspath(left).rstrip(os.sep)
    right_abs = os.path.abspath(right).rstrip(os.sep)
    return left_abs == right_abs or left_abs.startswith(right_abs + os.sep)


def _assert_isolated(isolate_dir: str, protected_roots: Sequence[str]) -> None:
    if not protected_roots:
        raise ValidationError(
            "恢复必须显式传入 protected_roots（主数据/源资料目录）；拒绝在没有隔离边界的情况下恢复"
        )
    resolved = os.path.abspath(isolate_dir)
    if resolved in (os.sep, os.path.abspath(os.path.expanduser("~"))):
        raise ValidationError(f"恢复目标过于宽泛（根目录/家目录），拒绝执行：{resolved}")
    for root in protected_roots:
        if _same_or_nested(resolved, root) or _same_or_nested(root, resolved):
            raise ValidationError(
                "恢复目标与受保护的主数据目录重叠，拒绝执行（绝不覆盖主数据）",
                detail={"isolate_dir": resolved, "protected_root": os.path.abspath(root)},
            )


def restore_archive(
    pan: Any,
    *,
    archive_id: str,
    isolate_dir: str,
    protected_roots: Sequence[str],
    layout: Optional[ArchiveLayout] = None,
    engine: Optional[ArchiveEngine] = None,
    state_dir: Optional[str] = None,
    reindex: Optional[Callable[[Sequence[str]], Dict[str, Any]]] = None,
    publish_record: bool = False,
    publish_approval_ref: Optional[str] = None,
    store: Any = None,
    requested_by: str = "user",
    clock: Optional[Callable[[], str]] = None,
) -> RestoreOutcome:
    """公开入口：恢复**是只读操作**，因此调用方原本的通道写锁状态必须原样交还。

    （内部实现会为了"恢复期间绝对不写"而锁死通道；结束后若它是解锁状态，
    用同一个 `approval_ref` 恢复到解锁，避免把调用方的通道永久锁死。）
    """
    prior_locked = bool(getattr(pan, "writes_locked", False))
    prior_ref = None if prior_locked else getattr(pan, "approval_ref", None)
    try:
        return _restore_archive_inner(
            pan,
            archive_id=archive_id,
            isolate_dir=isolate_dir,
            protected_roots=protected_roots,
            layout=layout,
            engine=engine,
            state_dir=state_dir,
            reindex=reindex,
            publish_record=publish_record,
            publish_approval_ref=publish_approval_ref,
            store=store,
            requested_by=requested_by,
            clock=clock,
        )
    finally:
        if prior_locked:
            lock = getattr(pan, "lock_writes", None)
            if callable(lock):
                lock()
        elif prior_ref:
            unlock = getattr(pan, "unlock_writes", None)
            if callable(unlock):
                unlock(prior_ref)


def _restore_archive_inner(
    pan: Any,
    *,
    archive_id: str,
    isolate_dir: str,
    protected_roots: Sequence[str],
    layout: Optional[ArchiveLayout] = None,
    engine: Optional[ArchiveEngine] = None,
    state_dir: Optional[str] = None,
    reindex: Optional[Callable[[Sequence[str]], Dict[str, Any]]] = None,
    publish_record: bool = False,
    publish_approval_ref: Optional[str] = None,
    store: Any = None,
    requested_by: str = "user",
    clock: Optional[Callable[[], str]] = None,
) -> RestoreOutcome:
    """把归档恢复到**隔离目录**：逐文件校验 + 引用闭合 + 重建索引 + 恢复记录。

    `reindex` 是注入的索引重建器（`(restored_paths) -> dict`）；传了却失败时
    恢复**不算成功**（`INDEX_FAILURE`）—— 资料落盘但检索不到等于没恢复。
    """
    layout = layout or ArchiveLayout()
    engine = engine or ArchiveEngine(pan, layout=layout, state_dir=state_dir)
    now = clock or engine.now

    _assert_isolated(isolate_dir, protected_roots)
    dest = os.path.abspath(isolate_dir)
    os.makedirs(dest, exist_ok=True)

    restore_id = "rstr_" + digest_of(
        {"archive_id": archive_id, "at": now(), "host": socket.gethostname()}
    )[:14]
    outcome = RestoreOutcome(
        archive_id=archive_id,
        restore_id=restore_id,
        isolate_dir=dest,
        state=RestoreState.PENDING.value,
    )

    # 只读保证：恢复期间锁死通道写入（真实通道支持时）；任何意外写入都会抛 PanWriteLocked
    lock = getattr(pan, "lock_writes", None)
    if callable(lock):
        lock()
        outcome.writes_locked_during_restore = True
        outcome.notes.append("恢复期间通道写入已锁死：不存在上传/删除动作（更不重放历史动作）")

    manifest_remote = layout.manifest_path(archive_id)
    try:
        stat = pan.stat(manifest_remote)
    except PanUnavailable as exc:
        outcome.state = RestoreState.FAILED.value
        outcome.error_code = "EXTERNAL_RESULT_UNKNOWN"
        outcome.error_text = f"manifest 状态无法判定：{exc}"
        outcome.notes.append("结果未知：保持未知状态，未写入任何文件")
        return outcome
    if stat is None:
        outcome.state = RestoreState.FAILED.value
        outcome.error_code = "ARCHIVE_MANIFEST_MISSING"
        outcome.error_text = f"远端找不到 manifest：{manifest_remote}"
        return outcome

    manifest: Dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="asistent-restore-manifest-") as tmp:
        local = os.path.join(tmp, "manifest.json")
        download = pan.get(manifest_remote, local)
        if not download.ok:
            outcome.state = RestoreState.FAILED.value
            outcome.error_code = "ARCHIVE_MANIFEST_UNREADABLE"
            outcome.error_text = f"manifest 回读失败：{download.error or '无输出'}"
            return outcome
        with open(local, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)

    outcome.manifest_digest = str(manifest.get("manifest_digest") or "")
    outcome.manifest_digest_ok = manifest_digest_of(manifest) == outcome.manifest_digest
    closure = validate_manifest(manifest)
    outcome.file_count = len(manifest.get("entries") or [])

    # 引用闭合必须在**写入任何文件之前**判定
    if not closure["ok"] or not outcome.manifest_digest_ok:
        outcome.state = RestoreState.FAILED.value
        outcome.error_code = "MANIFEST_NOT_CLOSED"
        outcome.error_text = "manifest 自校验/摘要复算未通过：拒绝据此恢复"
        outcome.notes.append(json.dumps(closure["problems"], ensure_ascii=False)[:500])
        return outcome
    outcome.closure_ok = True

    plan = restore_plan(manifest, isolate_dir=dest)
    outcome.notes.append(
        f"恢复计划：read_only_mode={plan['read_only_mode']} "
        f"replay_external_actions={plan['replay_external_actions']} "
        f"approvals_invalidated={plan['approvals_invalidated']}"
    )

    outcome.state = RestoreState.RUNNING.value
    for entry in manifest.get("entries", []):
        relative = entry["relative_path"]
        target = os.path.join(dest, relative.replace("/", os.sep))
        remote = layout.object_path(archive_id, relative)
        os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
        if os.path.isfile(target):
            # 已存在：内容一致则复用，不一致则记冲突并拒绝覆盖（绝不覆盖主数据）
            if sha256_file(target) == entry["sha256"]:
                outcome.verified.append(relative)
                continue
            outcome.conflicts.append({"relative_path": relative, "reason": "existing_file_differs"})
            continue
        try:
            download = pan.get(remote, target)
        except PanUnavailable as exc:
            outcome.unknown.append(relative)
            outcome.notes.append(f"{relative}：下载结果未知（{exc}）")
            continue
        if not download.ok:
            outcome.missing.append(relative)
            continue
        actual = download.content_hash or (sha256_file(target) if os.path.isfile(target) else "")
        if actual != entry["sha256"]:
            outcome.hash_mismatches.append(
                {"relative_path": relative, "expected": entry["sha256"], "actual": actual}
            )
            # 已知不一致的字节**不得留在隔离目录**里冒充"恢复成功"的数据
            try:
                if os.path.isfile(target):
                    os.remove(target)
                    outcome.notes.append(f"{relative}：回读哈希不一致，已删除隔离目录中的不一致副本")
            except OSError as exc:  # pragma: no cover - 权限异常环境
                outcome.notes.append(f"{relative}：回读哈希不一致且清理失败（{exc}）")
            continue
        if entry.get("content_origin") != "RAW_BYTES":
            outcome.projection_only.append(relative)
        outcome.verified.append(relative)

    # 引用闭合复查：隔离目录里不得出现 manifest 之外的文件（恢复记录除外）
    for current, _dirs, filenames in os.walk(dest):
        for filename in sorted(filenames):
            absolute = os.path.join(current, filename)
            relative = os.path.relpath(absolute, dest).replace("\\", "/")
            if relative == RESTORE_RECORD_NAME:
                continue
            if relative not in {entry["relative_path"] for entry in manifest.get("entries", [])}:
                outcome.extra_files.append(relative)
    outcome.closure_ok = outcome.closure_ok and not outcome.extra_files
    outcome.integrity_claim = integrity_claim(manifest, outcome.verified)

    if outcome.hash_mismatches or outcome.conflicts or outcome.extra_files:
        outcome.state = RestoreState.CONFLICT.value
        outcome.error_code = "ARCHIVE_INTEGRITY_ERROR"
        outcome.error_text = "恢复校验未通过：存在哈希不一致/冲突/多余文件（未覆盖任何已有数据）"
    elif outcome.missing or outcome.unknown:
        outcome.state = RestoreState.FAILED.value
        outcome.error_code = "EXTERNAL_RESULT_UNKNOWN" if outcome.unknown else "ARCHIVE_OBJECT_MISSING"
        outcome.error_text = "恢复未完成：存在缺失或结果未知的对象（可重试，已校验对象会复用）"
    else:
        outcome.state = RestoreState.VERIFIED.value

    # 重建索引：注入器失败 → 恢复不算成功（资料落盘但检索不到等于没恢复）
    if reindex is not None and outcome.state == RestoreState.VERIFIED.value:
        try:
            detail = reindex([os.path.join(dest, p.replace("/", os.sep)) for p in outcome.verified])
            outcome.reindex_detail = dict(detail or {})
            outcome.reindex_done = True
        except Exception as exc:  # noqa: BLE001 - 必须显式可见，不能吞
            outcome.state = RestoreState.FAILED.value
            outcome.error_code = "INDEX_FAILURE"
            outcome.error_text = f"索引重建失败：{type(exc).__name__}: {exc}"
            outcome.reindex_detail = {"error": str(exc)}
    elif reindex is None:
        outcome.notes.append("未注入索引重建器：reindex_done=False（恢复可读但未重建检索索引）")

    # 恢复记录：本地必写
    record = outcome.to_dict()
    record["recorded_at"] = now()
    record["plan"] = plan
    record["manifest_digest"] = outcome.manifest_digest
    record["read_only_notice"] = (
        "本次恢复为只读恢复：未重放归档中的发送/上传/审批；历史审批默认失效，"
        "如需重放必须重新走审批"
    )
    record_path = os.path.join(dest, RESTORE_RECORD_NAME)
    with open(record_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fh.write("\n")
    outcome.record_paths.append(record_path)

    if publish_record:
        outcome.record_paths.extend(
            _publish_restore_record(pan, layout, outcome, record_path, publish_approval_ref)
        )

    if store is not None:
        _persist_restore_job(store, outcome, manifest, requested_by=requested_by, at=now())
    return outcome


def _publish_restore_record(
    pan: Any,
    layout: ArchiveLayout,
    outcome: RestoreOutcome,
    record_path: str,
    approval_ref: Optional[str],
) -> List[str]:
    """把恢复记录也写到远端（**需要显式授权**：真实网盘默认关闭）。"""
    unlock = getattr(pan, "unlock_writes", None)
    lock = getattr(pan, "lock_writes", None)
    if not approval_ref:
        outcome.notes.append("publish_record=True 但未提供 publish_approval_ref：远端恢复记录未发布")
        return []
    if callable(unlock):
        unlock(approval_ref)
    try:
        remote = layout.restore_record_path(outcome.restore_id)
        result = pan.put(record_path, remote)
        if not result.ok:
            outcome.notes.append(f"恢复记录远端发布失败：{result.error or result.result_class}")
            return []
        stat = pan.stat(remote)
        if stat is None:
            outcome.notes.append("恢复记录远端发布后回读不到：不计入发布成功")
            return []
        with tempfile.TemporaryDirectory(prefix="asistent-restore-record-") as tmp:
            local = os.path.join(tmp, "record.json")
            download = pan.get(remote, local)
            if not download.ok or sha256_file(local) != sha256_file(record_path):
                outcome.notes.append("恢复记录远端回读校验不一致：不计入发布成功")
                return []
        return [f"pan:{remote}"]
    except PanWriteLocked:
        outcome.notes.append("恢复记录远端发布被写锁拒绝（未获批准）")
        return []
    except PanNotAuthorized as exc:
        outcome.notes.append(f"恢复记录远端发布未授权：{exc.message}")
        return []
    finally:
        if callable(lock):
            lock()


def _persist_restore_job(
    store: Any,
    outcome: RestoreOutcome,
    manifest: Dict[str, Any],
    *,
    requested_by: str,
    at: str,
) -> None:
    job = RestoreJob(
        id=new_id("RestoreJob", token=outcome.restore_id.replace("rstr_", "")[:24]),
        archive_id=outcome.archive_id,
        state=RestoreState(outcome.state),
        isolate_dir=outcome.isolate_dir,
        read_only_mode=outcome.read_only_mode,
        # 契约 v0.7（CP-20261010-22）：三条安全属性落成可机检字段，不再只写在 note 里。
        # 默认值本身就是安全值；这里显式写出，让恢复记录能**独立**证明"确实这么做了"。
        approvals_invalidated=True,
        replay_forbidden=True,
        verified_files=list(outcome.verified),
        hash_mismatches=list(outcome.hash_mismatches),
        conflicts=list(outcome.conflicts),
        requested_by=requested_by,
        started_at=at,
        finished_at=at,
        reindex_done=outcome.reindex_done,
        git_history_restored=bool(manifest.get("git_bundle")),
        note="恢复后审批默认失效；未重放归档中的任何外部动作",
    )
    job.touch(at=at)
    store.put(
        job,
        actor_id="archive-restore",
        reason="隔离恢复",
        audit_action="RESTORE_JOB_CREATE",
        audit_detail={
            "restore_id": outcome.restore_id,
            "archive_id": outcome.archive_id,
            "state": outcome.state,
            "verified": len(outcome.verified),
        },
    )
