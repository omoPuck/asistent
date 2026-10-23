"""事务归档引擎：不可变 manifest + 逐文件回读校验 + 部分失败补传 + 未知判定。

不变量（SPEC §12.4/§12.5、§21.4；A-5 约束 1-5）：
1. **上传完 ≠ 归档成功**：每个对象都必须在本 attempt 内**回读**并重算 sha256；
   只有数量/大小一致不算通过（`verify_readback`/本模块的 `_readback_matches` 只认内容哈希）；
2. **部分失败可见可重试**：状态 `PARTIAL`/`PENDING`，逐文件记录已成功/未成功，
   重试只补传未通过对象（`transfer_count` 只统计真正发起的上传）；
3. **结果未知不得盲目重传**：`UNKNOWN` 对象先 `stat` + 回读判定远端真实状态：
   远端已有且哈希一致 → 复用；缺失 → 补传；不一致 → 修复；判不出来 → 保持未知并上报；
4. **幂等**：同一 `archive_id` 重跑不重复上传，复用对象也必须在本 attempt 内重新回读校验；
   内容变化 → 契约的 `archive_id_for()` 派生新 id（旧归档不动）；
5. **`content_origin` 如实标注**：`METADATA_PROJECTION` 条目只能声称"投影一致"，
   报告与完成标记都按 `integrity_claim()` 拆分，**不得**宣称字节级校验（契约 §12.2）；
6. 已完成的归档若远端内容不一致 → 拒绝覆盖并报不一致（`ArchiveIntegrityError`）。

本地状态文件只用于可观察性/审计，**不参与**可信判定（决定永远来自远端回读）。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..contracts.archive import ArchivePolicy, require_verified, verify_remote
from ..contracts.enums import ArchiveState
from ..contracts.errors import ArchiveIntegrityError, NotFound, ValidationError
from ..contracts.ids import new_id, sha256_file
from ..contracts.objects import ArchiveEntry, ArchiveJob, ArchiveManifest
from ..contracts.pan import PanStat
from ..contracts.timeutil import utcnow_iso
from ..pan.bdpan import RESULT_FAILED_SAFE, RESULT_SUCCEEDED, RESULT_UNKNOWN
from ..pan.errors import PanNotAuthorized, PanUnavailable, PanWriteLocked
from .manifest import (
    MANIFEST_SCHEMA,
    SourceFile,
    content_origin_summary,
    integrity_claim,
    manifest_bytes,
    object_digest,
    validate_manifest,
    verify_frozen_snapshot,
)

__all__ = [
    "ARCHIVE_BASE_DIR",
    "RESTORE_RECORD_DIR",
    "ArchiveEngine",
    "ArchiveLayout",
    "ArchiveOutcome",
]

ARCHIVE_BASE_DIR = "个人助手归档/事务归档"
RESTORE_RECORD_DIR = "个人助手归档/恢复记录"


def remote_component(archive_id: str) -> str:
    """把 `archive_id` 映射成安全的远端目录名：只有 `[A-Za-z0-9._-]` 保留。

    `archive_id` 形如 `archive:<sha256>`，冒号在 Windows 上是非法路径字符
    （并且对网盘路径也不友好），所以远端目录统一做确定性替换；
    **身份本身仍然用完整 `archive_id`**（manifest 内、状态文件里、回读判定都按原值）。
    """
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in archive_id)
    return safe or "archive-unknown"


@dataclass
class ArchiveLayout:
    """远端布局（SPEC §12.4 建议逻辑目录；`prefix` 之上的 `/apps/bdpan/` 由通道负责）。"""

    base: str = ARCHIVE_BASE_DIR
    restore_records: str = RESTORE_RECORD_DIR

    def archive_dir(self, archive_id: str) -> str:
        return f"{self.base}/{remote_component(archive_id)}"

    def object_path(self, archive_id: str, relative_path: str) -> str:
        return f"{self.archive_dir(archive_id)}/objects/{relative_path}"

    def manifest_path(self, archive_id: str) -> str:
        return f"{self.archive_dir(archive_id)}/manifest.json"

    def complete_path(self, archive_id: str) -> str:
        return f"{self.archive_dir(archive_id)}/_COMPLETE.json"

    def restore_record_path(self, restore_id: str) -> str:
        return f"{self.restore_records}/{restore_id}.json"


@dataclass
class ArchiveOutcome:
    """一次归档尝试的可观察结果（可安全落库/打日志：不含凭据）。"""

    archive_id: str
    state: str
    manifest_digest: str = ""
    object_count: int = 0
    total_bytes: int = 0
    attempt: int = 1
    uploaded: List[str] = field(default_factory=list)
    reused: List[str] = field(default_factory=list)
    repaired: List[str] = field(default_factory=list)
    verified: List[str] = field(default_factory=list)
    unknown_objects: List[str] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    transfer_count: int = 0
    marker_written: bool = False
    already_complete: bool = False
    retryable: bool = True
    integrity_claim: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_text: Optional[str] = None
    next_step: Optional[str] = None
    remote_root: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    content_origin_summary: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == ArchiveState.VERIFIED.value and not self.failed

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload

    def summary(self) -> str:
        return (
            f"archive_id={self.archive_id} state={self.state} attempt={self.attempt} "
            f"objects={self.object_count} uploaded={len(self.uploaded)} reused={len(self.reused)} "
            f"repaired={len(self.repaired)} failed={len(self.failed)} unknown={len(self.unknown_objects)} "
            f"transfers={self.transfer_count} marker={'yes' if self.marker_written else 'no'}"
        )


class ArchiveEngine:
    """归档状态机（幂等、可重试、未知不盲传）。

    参数
    ----
    pan              : 契约 `PanClient`（真实 bdpan 通道 / `LocalDirPanClient` 离线回路）
    layout           : 远端布局
    state_dir        : 本地状态目录（可观察性/审计；不参与可信判定）
    store            : 可选存储（提供时写入 `ArchiveManifest` / `ArchiveJob` / `Case.archive_state`）
    clock            : 时间源；默认 `contracts.timeutil.utcnow_iso`，有 store 时用 `store.now()`
    require_authorized : 归档前做只读授权探测（通道支持时）；未授权直接显式失败，一次写都不发
    """

    def __init__(
        self,
        pan: Any,
        *,
        layout: Optional[ArchiveLayout] = None,
        state_dir: Optional[str] = None,
        store: Any = None,
        clock: Optional[Callable[[], str]] = None,
        require_authorized: bool = True,
        actor_id: str = "archive-engine",
    ) -> None:
        self.pan = pan
        self.layout = layout or ArchiveLayout()
        self.state_dir = state_dir or os.path.join(tempfile.gettempdir(), "asistent-archive-state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.store = store
        self._clock = clock
        self.require_authorized = require_authorized
        self.actor_id = actor_id

    # ------------------------------------------------------------------ 时间/状态
    def now(self) -> str:
        if self._clock is not None:
            return self._clock()
        if self.store is not None and hasattr(self.store, "now"):
            return self.store.now()
        return utcnow_iso()

    def _state_path(self, archive_id: str) -> str:
        safe = archive_id.replace(":", "_").replace("/", "_")
        return os.path.join(self.state_dir, f"{safe}.json")

    def _write_state(self, outcome: ArchiveOutcome) -> None:
        payload = outcome.to_dict()
        payload["updated_at"] = self.now()
        path = self._state_path(outcome.archive_id)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
        os.replace(tmp, path)

    def load_state(self, archive_id: str) -> Optional[Dict[str, Any]]:
        path = self._state_path(archive_id)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # ------------------------------------------------------------------ 授权前置
    def _preflight_authorization(self, outcome: ArchiveOutcome) -> bool:
        """未授权 → 显式 `NOT_AUTHORIZED`，返回 False，且不发出任何写调用。"""
        if not self.require_authorized:
            return True
        probe = getattr(self.pan, "require_authorized", None)
        if probe is None:  # 通道没有该能力（例如本地目录模拟）→ 不假装探测过
            outcome.notes.append("通道未提供授权探测能力（require_authorized 缺失），跳过前置探测")
            return True
        try:
            probe()
        except PanNotAuthorized as exc:
            outcome.state = ArchiveState.FAILED.value
            outcome.error_code = exc.code
            outcome.error_text = exc.message
            outcome.next_step = exc.detail.get("next_step")
            outcome.retryable = False
            outcome.notes.append("前置授权探测失败：未发出任何写调用")
            return False
        return True

    # ------------------------------------------------------------------ 回读判定
    def _readback_matches(self, remote_path: str, expected_sha256: str) -> Tuple[str, str]:
        """返回 `(verdict, detail)`；`verdict ∈ {match, mismatch, missing, unknown}`。

        `verdict=unknown` 表示**无法判定**（超时/通道异常），调用方必须保持未知，不得当失败重传。
        """
        try:
            stat = self.pan.stat(remote_path)
        except PanUnavailable as exc:
            return "unknown", f"stat 结果未知：{exc}"
        if stat is None:
            return "missing", "远端不存在"
        if getattr(stat, "is_dir", False):
            return "mismatch", "远端同名对象是目录"
        if getattr(stat, "content_hash", None):
            actual = str(stat.content_hash)
            return ("match" if actual == expected_sha256 else "mismatch"), actual
        # 通道给不出可信哈希（bdpan 的 md5 不可信）→ 下载回读 + 本地重算
        try:
            with tempfile.TemporaryDirectory(prefix="asistent-archive-readback-") as tmp:
                local = os.path.join(tmp, "object.bin")
                download = self.pan.get(remote_path, local)
                if not download.ok or not os.path.isfile(local):
                    return "unknown", f"回读下载不可判定：{download.error or '无输出'}"
                actual = download.content_hash or sha256_file(local)
        except PanUnavailable as exc:
            return "unknown", f"回读结果未知：{exc}"
        return ("match" if actual == expected_sha256 else "mismatch"), actual

    # ------------------------------------------------------------------ 单对象
    def _put_object(self, local_path: str, remote_path: str, outcome: ArchiveOutcome) -> str:
        """发起一次上传，返回 `SUCCEEDED`/`FAILED_SAFE`/`UNKNOWN`（未授权直接抛）。"""
        try:
            result = self.pan.put(local_path, remote_path)
        except PanWriteLocked:
            raise
        except PanNotAuthorized:
            raise
        except PanUnavailable:
            return RESULT_UNKNOWN
        outcome.transfer_count += 1
        return str(getattr(result, "result_class", RESULT_FAILED_SAFE if not result.ok else RESULT_SUCCEEDED))

    def _ensure_object(
        self,
        archive_id: str,
        entry: Dict[str, Any],
        staged_path: str,
        *,
        archive_completed: bool,
        outcome: ArchiveOutcome,
    ) -> None:
        relative = entry["relative_path"]
        expected = entry["sha256"]
        remote = self.layout.object_path(archive_id, relative)
        verdict, detail = self._readback_matches(remote, expected)
        if verdict == "unknown":
            outcome.unknown_objects.append(relative)
            outcome.failed.append({"relative_path": relative, "reason": "readback-unknown", "detail": detail})
            return
        if verdict == "match":
            # 复用也要在本 attempt 内回读校验过（不凭本地状态"信任"）
            outcome.reused.append(relative)
            outcome.verified.append(relative)
            return
        if verdict == "mismatch" and archive_completed:
            raise ArchiveIntegrityError(
                "已完成的归档在远端内容不一致：拒绝覆盖，需人工裁决",
                detail={"archive_id": archive_id, "relative_path": relative, "expected": expected, "actual": detail},
            )
        if verdict == "mismatch":
            outcome.repaired.append(relative)
            note = (
                "修复尝试：bdpan 的远端重名覆盖策略未实测（spike U-3），"
                "以本次回读结果为准；回读仍不一致则记未通过并保持可重试"
            )
            if note not in outcome.notes:
                outcome.notes.append(note)
        result_class = self._put_object(staged_path, remote, outcome)
        if result_class == RESULT_UNKNOWN:
            outcome.unknown_objects.append(relative)
            outcome.failed.append(
                {"relative_path": relative, "reason": "put-unknown", "detail": "上传结果未知，需 stat/回读判定"}
            )
            return
        if result_class != RESULT_SUCCEEDED:
            outcome.failed.append(
                {"relative_path": relative, "reason": "put-failed", "detail": detail or "上传返回确定失败"}
            )
            return
        outcome.uploaded.append(relative)
        verdict2, actual = self._readback_matches(remote, expected)
        if verdict2 == "match":
            outcome.verified.append(relative)
            return
        if verdict2 == "unknown":
            outcome.unknown_objects.append(relative)
            outcome.failed.append(
                {"relative_path": relative, "reason": "verify-unknown", "detail": actual}
            )
            return
        outcome.failed.append(
            {
                "relative_path": relative,
                "reason": "verify-mismatch" if verdict2 == "mismatch" else "verify-missing",
                "detail": f"expected={expected} actual={actual}",
            }
        )

    # ------------------------------------------------------------------ manifest 对象
    def _ensure_manifest_object(self, manifest: Dict[str, Any], archive_completed: bool, outcome: ArchiveOutcome) -> Dict[str, Any]:
        remote = self.layout.manifest_path(manifest["archive_id"])
        raw = manifest_bytes(manifest)
        expected = object_digest(raw)
        with tempfile.TemporaryDirectory(prefix="asistent-archive-manifest-") as tmp:
            local = os.path.join(tmp, "manifest.json")
            with open(local, "wb") as fh:
                fh.write(raw)
            verdict, detail = self._readback_matches(remote, expected)
            if verdict == "match":
                outcome.notes.append("manifest 已存在且摘要一致：未重写")
                return {"action": "reused", "remote_path": remote, "digest": expected}
            if verdict == "mismatch" and archive_completed:
                raise ArchiveIntegrityError(
                    "归档已完成，但远端 manifest 摘要不一致：拒绝覆盖",
                    detail={"remote_path": remote, "expected": expected, "actual": detail},
                )
            if verdict == "unknown":
                raise PanUnavailable(f"manifest 状态无法判定（结果未知）：{detail}")
            result_class = self._put_object(local, remote, outcome)
            if result_class != RESULT_SUCCEEDED:
                raise PanUnavailable(f"manifest 上传未成功（{result_class}）：{remote}")
            verdict2, actual = self._readback_matches(remote, expected)
            if verdict2 != "match":
                raise ArchiveIntegrityError(
                    "manifest 回读校验失败",
                    detail={"remote_path": remote, "expected": expected, "actual": actual, "verdict": verdict2},
                )
            return {"action": "uploaded", "remote_path": remote, "digest": expected}

    # ------------------------------------------------------------------ 完成标记
    def _read_complete_marker(self, archive_id: str) -> Optional[Dict[str, Any]]:
        remote = self.layout.complete_path(archive_id)
        # 标记内容每次都会变（verified_at），不能用哈希判定；这里只判定存在性 + 回读身份
        try:
            stat = self.pan.stat(remote)
        except PanUnavailable as exc:
            raise PanUnavailable(f"无法确认完成标记状态：{remote}（{exc}）") from exc
        if stat is None:
            return None
        with tempfile.TemporaryDirectory(prefix="asistent-archive-marker-") as tmp:
            local = os.path.join(tmp, "complete.json")
            download = self.pan.get(remote, local)
            if not download.ok:
                raise PanUnavailable(f"完成标记存在但无法回读：{remote}")
            with open(local, "r", encoding="utf-8") as fh:
                return json.load(fh)

    def _write_complete_marker(self, manifest: Dict[str, Any], outcome: ArchiveOutcome) -> None:
        claim = integrity_claim(manifest, outcome.verified)
        payload = {
            "schema_version": MANIFEST_SCHEMA,
            "archive_id": manifest["archive_id"],
            "manifest_digest": manifest["manifest_digest"],
            "object_count": len(manifest["entries"]),
            "total_bytes": manifest.get("total_bytes"),
            "policy": manifest.get("policy"),
            "code_version": manifest.get("code_version"),
            "contract_version": manifest.get("contract_version"),
            "scope": manifest.get("scope"),
            "scope_ref": manifest.get("scope_ref"),
            "verified_at": self.now(),
            "attempt": outcome.attempt,
            "transfers": outcome.transfer_count,
            "verification": "per-file-content-sha256-readback",
            "integrity_claim": claim,
            "content_origin_summary": content_origin_summary(manifest),
        }
        remote = self.layout.complete_path(manifest["archive_id"])
        with tempfile.TemporaryDirectory(prefix="asistent-archive-marker-") as tmp:
            local = os.path.join(tmp, "complete.json")
            with open(local, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                fh.write("\n")
            # 完成标记只在完成时写一次；已存在时按身份（archive_id + manifest_digest）判定等价，
            # 不比较重新生成的字节（verified_at 每次都变，字节比较会把幂等重试误判成冲突）
            existing = self.pan.stat(remote)
            if existing is not None:
                marker = self._read_complete_marker(manifest["archive_id"])
                if marker and marker.get("manifest_digest") == manifest["manifest_digest"]:
                    outcome.notes.append("完成标记已存在且身份一致：本次未重写标记")
                    return
                raise ArchiveIntegrityError(
                    "完成标记与本次 manifest 身份不一致：拒绝覆盖",
                    detail={"remote_path": remote, "existing": (marker or {}).get("manifest_digest")},
                )
            result_class = self._put_object(local, remote, outcome)
            if result_class == RESULT_UNKNOWN:
                raise PanUnavailable(f"完成标记上传结果未知：{remote}")
            if result_class != RESULT_SUCCEEDED:
                raise PanUnavailable(f"完成标记上传失败（{result_class}）：{remote}")
            outcome.marker_written = True

    # ------------------------------------------------------------------ 主流程
    def archive(
        self,
        manifest: Dict[str, Any],
        files: Sequence[SourceFile],
        *,
        attempt: Optional[int] = None,
    ) -> ArchiveOutcome:
        """执行（或重试）一次归档。同一 manifest 反复调用 = 幂等重试。"""
        check = validate_manifest(manifest)
        if not check["ok"]:
            raise ValidationError("manifest 自校验未通过，拒绝归档", detail=check)

        archive_id = manifest["archive_id"]
        previous = self.load_state(archive_id) or {}
        attempt_number = attempt or int(previous.get("attempt") or 0) + 1
        outcome = ArchiveOutcome(
            archive_id=archive_id,
            state=ArchiveState.UPLOADING.value,
            manifest_digest=manifest["manifest_digest"],
            object_count=len(manifest["entries"]),
            total_bytes=int(manifest.get("total_bytes") or 0),
            attempt=attempt_number,
            remote_root=self.layout.archive_dir(archive_id),
            content_origin_summary=content_origin_summary(manifest),
        )

        if not self._preflight_authorization(outcome):
            self._persist(manifest, outcome)
            return outcome

        frozen = verify_frozen_snapshot(files)
        if not frozen["ok"]:
            outcome.state = ArchiveState.FAILED.value
            outcome.error_code = "SNAPSHOT_CHANGED"
            outcome.error_text = "冻结快照与 manifest 不一致，已中止（未上传任何新内容）"
            outcome.notes.append(json.dumps(frozen["problems"], ensure_ascii=False))
            self._persist(manifest, outcome)
            return outcome

        staged = {item.relative_path: item for item in files}
        try:
            marker = self._read_complete_marker(archive_id)
        except PanUnavailable:
            marker = None
            outcome.notes.append("完成标记状态未知：本 attempt 继续，但不会把结果标成完成")
        archive_completed = bool(marker and marker.get("manifest_digest") == manifest["manifest_digest"])
        if marker and not archive_completed:
            raise ArchiveIntegrityError(
                "同一 archive_id 已有完成标记但 manifest 摘要不同：内容变化必须派生新 id，拒绝覆盖",
                detail={
                    "archive_id": archive_id,
                    "existing_marker_digest": marker.get("manifest_digest"),
                    "this_manifest_digest": manifest["manifest_digest"],
                },
            )
        if archive_completed:
            outcome.already_complete = True
            outcome.notes.append("远端已有完成标记：本次只做回读复验，不重复上传")

        try:
            for entry in manifest["entries"]:
                item = staged[entry["relative_path"]]
                self._ensure_object(
                    archive_id,
                    entry,
                    item.absolute_path,
                    archive_completed=archive_completed,
                    outcome=outcome,
                )
        except PanNotAuthorized as exc:
            outcome.state = ArchiveState.FAILED.value
            outcome.error_code = exc.code
            outcome.error_text = exc.message
            outcome.next_step = exc.detail.get("next_step")
            outcome.retryable = False
            self._persist(manifest, outcome)
            return outcome
        except PanWriteLocked as exc:
            outcome.state = ArchiveState.FAILED.value
            outcome.error_code = exc.code
            outcome.error_text = exc.message
            outcome.next_step = exc.detail.get("next_step")
            outcome.retryable = False
            self._persist(manifest, outcome)
            return outcome

        outcome.integrity_claim = integrity_claim(manifest, outcome.verified)

        # 未成功对象 → 保持待归档，不写完成标记
        if outcome.failed:
            outcome.state = (
                ArchiveState.PARTIAL.value if outcome.verified else ArchiveState.PENDING.value
            )
            outcome.notes.append("存在未通过对象：不写完成标记，可安全重试（只补传未通过对象）")
            self._persist(manifest, outcome)
            return outcome

        # 完整性断言：本 attempt 内每个对象都通过回读校验，才允许写 manifest 与完成标记
        if len(set(outcome.verified)) != len(manifest["entries"]):
            outcome.state = ArchiveState.PARTIAL.value
            outcome.notes.append("assertion-failed: readback coverage incomplete")
            self._persist(manifest, outcome)
            return outcome

        # manifest 与完成标记放在全部内容对象校验通过之后。
        # 这两步失败/未知时**不抛异常**而是停在可重试状态：对象都已校验通过，
        # 重试只需重新落地 manifest/标记，不必重传内容对象（A-5 约束 2/3）。
        try:
            self._ensure_manifest_object(manifest, archive_completed, outcome)
            self._write_complete_marker(manifest, outcome)
        except PanUnavailable as exc:
            outcome.state = ArchiveState.PARTIAL.value
            outcome.error_code = "EXTERNAL_RESULT_UNKNOWN"
            outcome.error_text = f"内容对象已全部通过回读校验，但 manifest/完成标记未落地：{exc}"
            outcome.notes.append("可重试：重试只补 manifest/完成标记，不重传已校验内容对象")
            self._persist(manifest, outcome)
            return outcome

        outcome.state = ArchiveState.VERIFIED.value
        outcome.integrity_claim = integrity_claim(manifest, outcome.verified)
        self._persist(manifest, outcome)
        return outcome

    # ------------------------------------------------------------------ 只读校验
    def verify_archive(self, archive_id: str) -> Dict[str, Any]:
        """只读校验已完成归档：manifest 存在 + 逐文件回读 + 完成标记身份一致。

        判定口径直接交给契约：把逐文件回读结果喂给 `verify_remote()`，
        拿不到可信哈希的条目用空串表示"不可验证"，于是契约会把它判成
        `PENDING_VERIFICATION` 而**不是**通过 —— 这正是 §21.4 要的口径。
        """
        report: Dict[str, Any] = {
            "archive_id": archive_id,
            "manifest_present": False,
            "manifest_digest": None,
            "marker_present": False,
            "missing": [],
            "mismatched": [],
            "unknown": [],
            "verified": [],
            "consistent": False,
        }
        remote = self.layout.manifest_path(archive_id)
        try:
            if self.pan.stat(remote) is None:
                return report
        except PanUnavailable as exc:
            report["unknown"].append(str(exc))
            return report
        report["manifest_present"] = True
        manifest: Dict[str, Any] = {}
        with tempfile.TemporaryDirectory(prefix="asistent-archive-verify-") as tmp:
            local = os.path.join(tmp, "manifest.json")
            download = self.pan.get(remote, local)
            if not download.ok:
                report["unknown"].append("manifest 无法回读")
                return report
            with open(local, "rb") as fh:
                raw = fh.read()
            report["manifest_object_digest"] = object_digest(raw)
            manifest = json.loads(raw.decode("utf-8"))
            report["manifest_digest"] = manifest.get("manifest_digest")
            report["content_origin_summary"] = content_origin_summary(manifest)
            remote_files: Dict[str, str] = {}
            for entry in manifest.get("entries", []):
                relative = entry["relative_path"]
                verdict, detail = self._readback_matches(
                    self.layout.object_path(archive_id, relative), entry["sha256"]
                )
                if verdict == "match":
                    report["verified"].append(relative)
                    remote_files[relative] = entry["sha256"]
                elif verdict == "missing":
                    report["missing"].append(relative)
                elif verdict == "unknown":
                    report["unknown"].append(relative)
                    # 空串 = 契约语义里的"拿不到可信哈希" → unverifiable → PENDING_VERIFICATION
                    remote_files[relative] = ""
                else:
                    report["mismatched"].append({"relative_path": relative, "actual": detail})
                    remote_files[relative] = detail
            report["integrity_claim"] = integrity_claim(manifest, report["verified"])
            report["contract"] = verify_remote(manifest=manifest, remote_files=remote_files, require_hash=True)
        marker = self._read_complete_marker(archive_id)
        report["marker_present"] = marker is not None
        if marker is not None:
            report["marker_manifest_digest"] = marker.get("manifest_digest")
            report["marker_digest_matches"] = marker.get("manifest_digest") == report["manifest_digest"]
        report["consistent"] = bool(
            report.get("contract", {}).get("state") == "VERIFIED"
            and report["marker_present"]
            and report.get("marker_digest_matches")
        )
        return report

    def assert_verified(self, archive_id: str) -> Dict[str, Any]:
        """契约口径的强校验：不通过就抛 `ArchiveIntegrityError`（不许写完成标记/宣称成功）。"""
        report = self.verify_archive(archive_id)
        contract = report.get("contract")
        if contract is None:
            raise ArchiveIntegrityError(
                "归档校验无法进行（manifest 缺失或结果未知）",
                detail={"archive_id": archive_id, "missing": report.get("missing"), "unknown": report.get("unknown")},
            )
        require_verified(contract)
        if not report["marker_present"] or not report.get("marker_digest_matches"):
            raise ArchiveIntegrityError(
                "manifest 与完成标记不一致，不得宣称归档完成",
                detail={"archive_id": archive_id, "marker_present": report["marker_present"]},
            )
        return report

    # ------------------------------------------------------------------ 落库
    def _persist(self, manifest: Dict[str, Any], outcome: ArchiveOutcome) -> None:
        """本地状态永远写；有 store 时同时落 `ArchiveManifest` / `ArchiveJob` / `Case.archive_state`。"""
        self._write_state(outcome)
        if self.store is None:
            return
        archive_id = manifest["archive_id"]
        manifest_object = self._upsert_manifest_object(manifest)
        job = self._upsert_job(manifest, outcome, manifest_object.id)
        if manifest.get("scope") == "case" and manifest.get("scope_ref"):
            self._update_case_state(manifest["scope_ref"], outcome)

    def _upsert_manifest_object(self, manifest: Dict[str, Any]) -> ArchiveManifest:
        archive_id = manifest["archive_id"]
        existing = self.store.get_by_unique("archive_id", archive_id)
        obj = ArchiveManifest(
            id=new_id("ArchiveManifest", token=archive_id.split(":", 1)[-1][:24]),
            archive_id=archive_id,
            scope=manifest.get("scope", "snapshot"),
            scope_ref=manifest.get("scope_ref", ""),
            created_at_manifest=self.now(),
            entries=[ArchiveEntry.from_dict(entry) for entry in manifest.get("entries", [])],
            source_versions=dict(manifest.get("source_versions") or {}),
            policy_id=(manifest.get("policy") or {}).get("policy_id"),
            policy_version=(manifest.get("policy") or {}).get("version"),
            code_version=manifest.get("code_version"),
            contract_version=manifest.get("contract_version"),
            rule_version=manifest.get("rule_version"),
            workflow_version=manifest.get("workflow_version"),
            excluded=list(manifest.get("excluded") or []),
            secret_scan=dict(manifest.get("secret_scan") or {}),
            manifest_digest=manifest.get("manifest_digest", ""),
            remote_root=self.layout.archive_dir(archive_id),
        )
        obj.touch(at=self.now())
        if existing is not None:
            obj.id = existing.id
            self.store.put(
                obj,
                expected_revision=existing.revision,
                unique=("archive_id", archive_id),
                actor_id=self.actor_id,
                reason="归档 manifest 幂等重写",
                audit_action="ARCHIVE_MANIFEST_UPSERT",
                audit_detail={"archive_id": archive_id, "manifest_digest": obj.manifest_digest},
            )
        else:
            self.store.put(
                obj,
                unique=("archive_id", archive_id),
                actor_id=self.actor_id,
                reason="归档 manifest 登记",
                audit_action="ARCHIVE_MANIFEST_UPSERT",
                audit_detail={"archive_id": archive_id, "manifest_digest": obj.manifest_digest},
            )
        return obj

    def _upsert_job(
        self, manifest: Dict[str, Any], outcome: ArchiveOutcome, manifest_object_id: str
    ) -> ArchiveJob:
        archive_id = manifest["archive_id"]
        existing = self.store.get_by_unique("archive_job", archive_id)
        verified_files = len(set(outcome.verified))
        unverified_reason = None
        if outcome.failed:
            unverified_reason = ";".join(sorted({str(item.get("reason")) for item in outcome.failed}))
        job = ArchiveJob(
            id=new_id("ArchiveJob", token=archive_id.split(":", 1)[-1][:24]),
            archive_id=archive_id,
            scope=manifest.get("scope", "snapshot"),
            scope_ref=manifest.get("scope_ref", ""),
            manifest_id=manifest_object_id,
            state=ArchiveState(outcome.state),
            policy_id=(manifest.get("policy") or {}).get("policy_id"),
            policy_version=(manifest.get("policy") or {}).get("version"),
            trigger=str((manifest.get("trigger") or "user_request")),
            started_at=(existing.started_at if existing is not None else self.now()),
            last_attempt_at=self.now(),
            finished_at=self.now() if outcome.state != ArchiveState.UPLOADING.value else None,
            attempt_count=outcome.attempt,
            uploaded=list(outcome.uploaded),
            pending=list(outcome.failed and [item["relative_path"] for item in outcome.failed] or []),
            failed=list(outcome.failed),
            verified_files=verified_files,
            unverified_reason=unverified_reason,
            remote_root=outcome.remote_root,
            error_text=outcome.error_text,
        )
        job.touch(at=self.now())
        if existing is not None:
            job.id = existing.id
            self.store.put(
                job,
                expected_revision=existing.revision,
                unique=("archive_job", archive_id),
                actor_id=self.actor_id,
                reason="归档任务状态更新",
                audit_action="ARCHIVE_JOB_UPDATE",
                audit_detail={"archive_id": archive_id, "state": outcome.state},
            )
        else:
            self.store.put(
                job,
                unique=("archive_job", archive_id),
                actor_id=self.actor_id,
                reason="归档任务创建",
                audit_action="ARCHIVE_JOB_UPDATE",
                audit_detail={"archive_id": archive_id, "state": outcome.state},
            )
        return job

    def _update_case_state(self, case_id: str, outcome: ArchiveOutcome) -> None:
        """把归档状态写回 Case.archive_state（与业务状态独立持久化，SPEC §21.1）。

        SPEC 用词是 `ARCHIVE_PENDING`，契约 v0.5 的枚举里对应 `PARTIAL`/`PENDING`；
        这里只写契约枚举值，措辞差异见 docs/mvp/mvp-7.md 的 CP 提案。
        """
        try:
            case = self.store.get("Case", case_id)
        except NotFound:
            return
        target = ArchiveState(outcome.state)
        if getattr(case, "archive_state", None) == target:
            return
        case.archive_state = target
        case.touch(at=self.now())
        self.store.put(
            case,
            expected_revision=case.revision,
            actor_id=self.actor_id,
            reason=f"归档状态回写：{outcome.state}",
            audit_action="CASE_ARCHIVE_STATE",
            audit_detail={"case_id": case_id, "archive_state": outcome.state, "archive_id": outcome.archive_id},
        )
