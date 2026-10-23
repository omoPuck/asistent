"""归档 manifest 的生产化封装（SPEC §12.4、§21.4；契约 `asistent/contracts/archive.py`）。

职责边界：
- 复用契约的 `capture_files` / `scan_for_secrets` / `build_manifest` / `snapshot_digest`
  （**不重写**范围校验、排除清单、体积上限、秘密扫描、`archive_id` 派生规则）；
- 补上契约当前没覆盖的一点：`ArchiveEntry.content_origin`（CONTRACT §12.2 要求归档条目必须带
  来源类型），并把它纳入 `manifest_digest` —— 否则改动"这条到底是原始字节还是元数据投影"
  将无法被摘要发现，正是 §12.2 要防的"看起来通过、实际没校验到字节"。

快照语义（与 Spike 一致）：manifest 的哈希与上传的字节都取自**冻结拷贝**；
上传期间源文件被改写不影响本次归档，变化进入下一份快照。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..contracts.archive import (
    DEFAULT_EXCLUDES,
    ArchivePolicy,
    LocalFile,
    SecretScanResult,
    build_manifest,
    capture_files,
    scan_for_secrets,
    snapshot_digest,
)
from ..contracts.errors import ValidationError
from ..contracts.ids import canonical_json, digest_of, sha256_bytes, sha256_file

#: 契约 v0.7（CP-20261010-18）：`content_origin` 的规范取值由契约定义，这里**引用**而不是重新定义。
#: 以前本模块各定义一份字面量，任何拼写漂移都会让"投影 vs 字节级"的判据静默错配。
from ..contracts.objects import (  # noqa: E402  (import 分组：契约常量紧跟其它契约导入)
    CONTENT_ORIGIN_METADATA_PROJECTION,
    CONTENT_ORIGIN_RAW_BYTES,
    VALID_CONTENT_ORIGINS,
)

from ..contracts.archive import (  # noqa: E402
    INTEGRITY_BYTE_LEVEL,
    INTEGRITY_PROJECTION_ONLY,
    INTEGRITY_UNDECLARED,
    integrity_level,
)

__all__ = [
    "CONTENT_ORIGIN_METADATA_PROJECTION",
    "CONTENT_ORIGIN_RAW_BYTES",
    "MANIFEST_SCHEMA",
    "VALID_CONTENT_ORIGINS",
    "SourceFile",
    "build_archive_manifest",
    "capture_source_tree",
    "content_origin_summary",
    "freeze_snapshot",
    "integrity_claim",
    "manifest_bytes",
    "manifest_digest_of",
    "object_digest",
    "validate_manifest",
    "verify_frozen_snapshot",
]

MANIFEST_SCHEMA = "mvp7-archive-manifest-1"


@dataclass
class SourceFile:
    """待归档文件：契约 `LocalFile` + `content_origin`。"""

    absolute_path: str
    relative_path: str
    size_bytes: int
    sha256: str
    kind: str = "file"
    content_origin: Optional[str] = CONTENT_ORIGIN_RAW_BYTES
    source_object_id: Optional[str] = None
    source_version: Optional[int] = None

    def to_local_file(self) -> LocalFile:
        return LocalFile(
            absolute_path=self.absolute_path,
            relative_path=self.relative_path,
            size_bytes=self.size_bytes,
            sha256=self.sha256,
            source_object_id=self.source_object_id,
            source_version=self.source_version,
            kind=self.kind,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "content_origin": self.content_origin,
            "source_object_id": self.source_object_id,
            "source_version": self.source_version,
        }


def capture_source_tree(
    root: str,
    *,
    excludes: Sequence[str] = DEFAULT_EXCLUDES,
    max_file_bytes: Optional[int] = None,
    content_origin: Optional[str] = CONTENT_ORIGIN_RAW_BYTES,
    origin_by_relative: Optional[Dict[str, Optional[str]]] = None,
    include_relative: Optional[Iterable[str]] = None,
    scan_secrets: bool = True,
) -> Tuple[List[SourceFile], List[str], SecretScanResult]:
    """捕获快照：返回（文件清单, 被排除的相对路径, 秘密扫描结果）。

    `content_origin` 是整批默认值；`origin_by_relative` 可为单个文件覆盖
    （例如某条材料来自 MCP 元数据投影 → `METADATA_PROJECTION`）。
    """
    if origin_by_relative:
        for origin in origin_by_relative.values():
            if origin is not None and origin not in VALID_CONTENT_ORIGINS:
                raise ValidationError(f"未知 content_origin: {origin}")
    captured, excluded = capture_files(
        root,
        excludes=excludes,
        max_file_bytes=max_file_bytes,
        include_relative=include_relative,
    )
    files: List[SourceFile] = []
    for item in captured:
        origin = (origin_by_relative or {}).get(item.relative_path, content_origin)
        files.append(
            SourceFile(
                absolute_path=item.absolute_path,
                relative_path=item.relative_path,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                kind=item.kind,
                content_origin=origin,
                source_object_id=item.source_object_id,
                source_version=item.source_version,
            )
        )
    scan = scan_for_secrets([item.to_local_file() for item in files]) if scan_secrets else SecretScanResult(clean=True)
    return files, excluded, scan


def freeze_snapshot(files: Sequence[SourceFile], stage_dir: str) -> List[SourceFile]:
    """把已捕获的文件复制到冻结目录，并逐文件复核哈希。

    捕获与冻结之间源文件若被改写 → 直接拒绝（`ValidationError`），
    绝不"边传边变"，也不静默用新内容替换旧哈希。
    """
    staged: List[SourceFile] = []
    for item in files:
        target = os.path.join(stage_dir, item.relative_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(target) or stage_dir, exist_ok=True)
        shutil.copyfile(item.absolute_path, target)
        actual = sha256_file(target)
        if actual != item.sha256:
            raise ValidationError(
                "源文件在快照与冻结之间发生变化，本次归档已中止（请重跑以生成新 archive_id）",
                detail={"relative_path": item.relative_path, "expected": item.sha256, "actual": actual},
            )
        staged.append(
            SourceFile(
                absolute_path=target,
                relative_path=item.relative_path,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                kind=item.kind,
                content_origin=item.content_origin,
                source_object_id=item.source_object_id,
                source_version=item.source_version,
            )
        )
    return staged


def verify_frozen_snapshot(files: Sequence[SourceFile]) -> Dict[str, Any]:
    """上传前复核：冻结拷贝是否仍与 manifest 记录一致。"""
    problems: List[Dict[str, Any]] = []
    for item in files:
        if not os.path.isfile(item.absolute_path):
            problems.append({"relative_path": item.relative_path, "problem": "missing_staged_file"})
            continue
        actual = sha256_file(item.absolute_path)
        if actual != item.sha256:
            problems.append(
                {"relative_path": item.relative_path, "problem": "staged_hash_changed", "actual": actual}
            )
    return {"ok": not problems, "problems": problems}


def build_archive_manifest(
    *,
    scope: str,
    scope_ref: str = "",
    files: Sequence[SourceFile],
    policy: Optional[ArchivePolicy] = None,
    source_versions: Optional[Dict[str, str]] = None,
    code_version: Optional[str] = None,
    contract_version: Optional[str] = None,
    rule_version: Optional[str] = None,
    workflow_version: Optional[str] = None,
    remote_root: Optional[str] = None,
    secret_scan: Optional[SecretScanResult] = None,
    excluded: Optional[Sequence[str]] = None,
    require_clean_scan: bool = True,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造不可变 manifest（含逐条 `content_origin`，并纳入 `manifest_digest`）。

    `require_clean_scan=True` 时命中疑似秘密直接 `SecretDetected` 阻止归档（契约 §7）。
    """
    if secret_scan is not None and require_clean_scan:
        secret_scan.require_clean()
    manifest = build_manifest(
        scope=scope,
        scope_ref=scope_ref,
        files=[item.to_local_file() for item in files],
        policy=policy,
        source_versions=source_versions,
        code_version=code_version,
        contract_version=contract_version,
        rule_version=rule_version,
        workflow_version=workflow_version,
        remote_root=remote_root,
        # 契约的 build_manifest 只看 LocalFile —— 秘密扫描结果必须由调用方显式传入，
        # 否则 manifest 里会写"扫描通过"（那是假证据）。
        secret_scan=secret_scan if secret_scan is not None else SecretScanResult(clean=False, hits=[{"kind": "not-scanned"}]),
        excluded=excluded,
    )
    origins = {item.relative_path: item.content_origin for item in files}
    for entry in manifest["entries"]:
        entry["content_origin"] = origins.get(entry["relative_path"], None)
    manifest["schema_version"] = MANIFEST_SCHEMA
    manifest["content_origin_summary"] = content_origin_summary(manifest)
    if extra:
        for key, value in extra.items():
            if key in ("manifest_digest", "archive_id", "entries"):
                raise ValidationError(f"extra 不得覆盖核心字段: {key}")
            manifest[key] = value
    # 摘要覆盖除自身与 remote_root 外的全部字段（与契约 build_manifest 同一规则 + content_origin）
    manifest["manifest_digest"] = digest_of(
        {key: value for key, value in manifest.items() if key not in ("manifest_digest", "remote_root")}
    )
    return manifest


def content_origin_summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """逐条来源类型统计：`None` 单列，绝不当成 `RAW_BYTES`。

    分类由契约 `integrity_level()` 决定（v0.7，CP-20261010-21），本模块不再自己判等。
    """
    counts = {"RAW_BYTES": 0, "METADATA_PROJECTION": 0, "undeclared": 0}
    for entry in manifest.get("entries", []):
        level = integrity_level(entry.get("content_origin"))
        if level == INTEGRITY_BYTE_LEVEL:
            counts["RAW_BYTES"] += 1
        elif level == INTEGRITY_PROJECTION_ONLY:
            counts["METADATA_PROJECTION"] += 1
        else:
            counts["undeclared"] += 1
    counts["byte_level_verifiable"] = counts["RAW_BYTES"]
    counts["projection_only"] = counts["METADATA_PROJECTION"] + counts["undeclared"]
    return counts


def integrity_claim(manifest: Dict[str, Any], verified_paths: Sequence[str]) -> Dict[str, Any]:
    """按 `content_origin` 如实拆分"校验到了什么"（契约 §12.2）。

    - `RAW_BYTES`：回读 sha256 一致 = **字节级**校验（归档里的字节就是源字节）；
    - `METADATA_PROJECTION`：本地字节本身是上游元数据的**重建产物**，回读一致只能证明
      "投影重建是确定的、上传字节没变"，**不能**证明与远端原始字节一致；
    - 未声明（`None`）：同样只能算投影级，且必须在报告里点名（不得默认当字节级）。

    分类规则来自契约 `integrity_level()`（v0.7，CP-20261010-21）：规则唯一、措辞由本模块负责。
    """
    verified = set(verified_paths)
    byte_level: List[str] = []
    projection_only: List[str] = []
    undeclared: List[str] = []
    for entry in manifest.get("entries", []):
        path = entry["relative_path"]
        if path not in verified:
            continue
        level = integrity_level(entry.get("content_origin"))
        if level == INTEGRITY_BYTE_LEVEL:
            byte_level.append(path)
        elif level == INTEGRITY_PROJECTION_ONLY:
            projection_only.append(path)
        else:
            undeclared.append(path)
    claim = {
        "byte_level_verified": sorted(byte_level),
        "projection_only_verified": sorted(projection_only),
        "undeclared_origin_verified": sorted(undeclared),
        "byte_level_count": len(byte_level),
        "projection_only_count": len(projection_only) + len(undeclared),
        "statement": (
            f"逐文件回读 sha256 校验通过 {len(verified)} 项：其中 {len(byte_level)} 项为字节级"
            f"（content_origin=RAW_BYTES），{len(projection_only)} 项仅为投影一致"
            f"（METADATA_PROJECTION），{len(undeclared)} 项来源未声明（按投影级对待）"
        ),
    }
    if projection_only or undeclared:
        claim["warning"] = (
            "存在非 RAW_BYTES 条目：本次归档**不得**被引用为字节级完整性证明"
            "（CONTRACT §12.2）；下游引用这些对象时必须按投影语义处理"
        )
    return claim


def manifest_bytes(manifest: Dict[str, Any]) -> bytes:
    """manifest 的远端对象字节（规范化 JSON，UTF-8 保真）。"""
    return (canonical_json(manifest) + "\n").encode("utf-8")


def object_digest(raw: bytes) -> str:
    return sha256_bytes(raw)


def manifest_digest_of(manifest: Dict[str, Any]) -> str:
    return digest_of(
        {key: value for key, value in manifest.items() if key not in ("manifest_digest", "remote_root")}
    )


def validate_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """manifest 自校验（引用闭合 + 摘要可复算 + 逐条来源声明）。"""
    problems: List[Dict[str, Any]] = []
    required = ("archive_id", "scope", "entries", "manifest_digest", "snapshot_digest")
    for key in required:
        if not manifest.get(key):
            problems.append({"problem": "missing_field", "field": key})
    entries = manifest.get("entries") or []
    if not entries:
        problems.append({"problem": "empty_manifest"})
    seen: Dict[str, int] = {}
    for entry in entries:
        path = entry.get("relative_path")
        if not path:
            problems.append({"problem": "entry_without_path"})
            continue
        if path.startswith("/") or ".." in path.split("/"):
            problems.append({"problem": "untrusted_relative_path", "relative_path": path})
        seen[path] = seen.get(path, 0) + 1
        if not entry.get("sha256") or len(str(entry["sha256"])) != 64:
            problems.append({"problem": "entry_without_sha256", "relative_path": path})
        if entry.get("size_bytes") is None:
            problems.append({"problem": "entry_without_size", "relative_path": path})
        origin = entry.get("content_origin", "MISSING")
        if origin == "MISSING":
            problems.append({"problem": "entry_without_content_origin", "relative_path": path})
        elif origin is not None and origin not in VALID_CONTENT_ORIGINS:
            problems.append({"problem": "invalid_content_origin", "relative_path": path, "origin": origin})
    duplicates = sorted(path for path, count in seen.items() if count > 1)
    if duplicates:
        problems.append({"problem": "duplicate_paths", "paths": duplicates})
    scan = manifest.get("secret_scan") or {}
    if not scan.get("clean"):
        problems.append({"problem": "secret_scan_not_clean", "hits": scan.get("hits", [])})
    recomputed = manifest_digest_of(manifest)
    if manifest.get("manifest_digest") and recomputed != manifest["manifest_digest"]:
        problems.append(
            {
                "problem": "manifest_digest_mismatch",
                "recorded": manifest["manifest_digest"],
                "recomputed": recomputed,
            }
        )
    return {
        "ok": not problems,
        "problems": problems,
        "entry_count": len(entries),
        "snapshot_digest": manifest.get("snapshot_digest"),
        "content_origin_summary": content_origin_summary(manifest),
    }


def redacted_policy(policy: ArchivePolicy) -> Dict[str, Any]:
    """策略的可打印形态（不含凭据；`ArchivePolicy` 本身也不存凭据）。"""
    payload = policy.to_dict()
    payload.pop("approved_by", None)
    return payload
