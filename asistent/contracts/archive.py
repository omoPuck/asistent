"""归档契约：ArchivePolicy、不可变 manifest、完整性校验、恢复前置（SPEC §12、§21.3、§21.4）。

关键不变量：
1. `archive_id` 由「范围 + 内容快照摘要」派生：内容变化 → 新 archive_id；同 id 重试复用未完成对象；
2. 上传完成 ≠ 归档成功：必须**逐文件内容哈希**与 manifest 一致才写完成标记；
3. 只有文件数量/大小一致**不能**作为完整性通过依据；
4. 命中疑似秘密必须阻止该归档（不能只依赖扫描器证明"无敏感内容"）；
5. 恢复先进隔离目录，默认只读，绝不重放归档中的发送/上传/审批。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import ArchiveIntegrityError, SecretDetected, ValidationError
from .ids import archive_id_for, digest_of, sha256_file
from .objects import CONTENT_ORIGIN_METADATA_PROJECTION, CONTENT_ORIGIN_RAW_BYTES

#: 回读校验等级（契约 v0.7，CP-20261010-21）：把 §12.2 的"不得把投影当字节级证明"变成机检取值。
INTEGRITY_BYTE_LEVEL = "BYTE_LEVEL"
INTEGRITY_PROJECTION_ONLY = "PROJECTION_ONLY"
INTEGRITY_UNDECLARED = "UNDECLARED"
INTEGRITY_LEVELS: Tuple[str, ...] = (INTEGRITY_BYTE_LEVEL, INTEGRITY_PROJECTION_ONLY, INTEGRITY_UNDECLARED)


def integrity_level(content_origin: Optional[str]) -> str:
    """把 `content_origin` 映射成"回读校验到底证明了什么"的等级。

    这条规则以前只以散文形式存在于 §12.2，各模块各自实现一遍分类（`asistent/archive/manifest.py`
    的 `integrity_claim` 与 `content_origin_summary` 就是两份），存在漂移风险。现在规则由契约给出：

    - `RAW_BYTES` → `BYTE_LEVEL`（回读字节一致 = 源字节一致）；
    - `METADATA_PROJECTION` → `PROJECTION_ONLY`（只能证明投影重建确定、上传字节没变）；
    - 未声明/未知 → `UNDECLARED`。**未声明按投影级对待**，绝不放行成字节级
      （宁可保守，也不要"看起来通过"）。
    """
    if content_origin == CONTENT_ORIGIN_RAW_BYTES:
        return INTEGRITY_BYTE_LEVEL
    if content_origin == CONTENT_ORIGIN_METADATA_PROJECTION:
        return INTEGRITY_PROJECTION_ONLY
    return INTEGRITY_UNDECLARED

#: 归档范围类型
ARCHIVE_SCOPES = ("snapshot", "case", "chat", "rules", "git_bundle", "generated_file")

#: 默认排除清单（凭据类，SPEC §21.4）
DEFAULT_EXCLUDES: Tuple[str, ...] = (
    ".env",
    ".env.local",
    "credentials",
    ".credentials.yaml",
    ".credentials.yml",
    "id_rsa",
    "id_ed25519",
    "*.pem",
    "*.key",
    "secrets",
    "token",
    "*.token",
    "cookies.txt",
)

_SECRET_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
    ("bduss_cookie", re.compile(r"\bBDUSS=[A-Za-z0-9~_\-]{20,}")),
    ("generic_api_key", re.compile(r"(?i)\b(api[_-]?key|secret|passwd|password|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{16,}")),
)


@dataclass
class ArchivePolicy:
    """用户预先确认的归档策略（SPEC §21.3）。范围变化必须重新批准。"""

    policy_id: str
    version: int = 1
    #: 允许归档的源目录/资料类型白名单
    source_paths: List[str] = field(default_factory=list)
    #: 目标账号与根目录
    target_account: str = ""
    target_root: str = ""
    #: 额外排除项（与 DEFAULT_EXCLUDES 合并）
    excludes: List[str] = field(default_factory=list)
    #: 调度（cron 或固定每日时间，默认 Asia/Shanghai 03:00）
    schedule: str = "daily@03:00 Asia/Shanghai"
    #: 单文件体积上限（字节），默认 20 MiB（SPEC §21.7）
    max_file_bytes: int = 20 * 1024 * 1024
    #: 单次归档总体积上限
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    #: 保留策略
    retention: Dict[str, Any] = field(default_factory=lambda: {"daily_snapshots": 30, "monthly_snapshots": 12, "auto_delete_case_archives": False})
    #: 软配额：账号可用空间的百分比
    quota_soft_ratio: float = 0.8
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    valid_until: Optional[str] = None
    revoked: bool = False

    def effective_excludes(self) -> Tuple[str, ...]:
        merged: List[str] = list(DEFAULT_EXCLUDES)
        for item in self.excludes:
            if item not in merged:
                merged.append(item)
        return tuple(merged)

    def validate(self) -> None:
        if not self.policy_id or not self.target_account or not self.target_root:
            raise ValidationError("ArchivePolicy 必须指定 policy_id、target_account 与 target_root")
        if not self.source_paths:
            raise ValidationError("ArchivePolicy 必须给定 source_paths 白名单（禁止隐式全盘归档）")
        if self.max_file_bytes <= 0:
            raise ValidationError("max_file_bytes 必须为正数")
        if self.approved_by is None:
            raise ValidationError("ArchivePolicy 必须由用户确认后才能用于真实归档")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "source_paths": list(self.source_paths),
            "target_account": self.target_account,
            "target_root": self.target_root,
            "excludes": list(self.effective_excludes()),
            "schedule": self.schedule,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "retention": dict(self.retention),
            "quota_soft_ratio": self.quota_soft_ratio,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "valid_until": self.valid_until,
            "revoked": self.revoked,
        }


@dataclass
class SecretScanResult:
    clean: bool
    hits: List[Dict[str, Any]] = field(default_factory=list)
    excluded: List[str] = field(default_factory=list)
    scanned_files: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "clean": self.clean,
            "hits": list(self.hits),
            "excluded": list(self.excluded),
            "scanned_files": self.scanned_files,
        }

    def require_clean(self) -> None:
        if not self.clean:
            raise SecretDetected(
                "归档内容命中疑似秘密，已阻止该归档并要求处理",
                detail={"hits": self.hits},
            )


@dataclass
class LocalFile:
    """待归档文件（本地快照捕获结果）。"""

    absolute_path: str
    relative_path: str
    size_bytes: int
    sha256: str
    source_object_id: Optional[str] = None
    source_version: Optional[int] = None
    kind: str = "file"


def _is_excluded(relative_path: str, excludes: Sequence[str]) -> bool:
    import fnmatch

    name = os.path.basename(relative_path).lower()
    lowered = relative_path.lower()
    for pattern in excludes:
        pattern_lower = pattern.lower()
        if fnmatch.fnmatch(name, pattern_lower) or fnmatch.fnmatch(lowered, pattern_lower):
            return True
        if pattern_lower.strip("*.") and pattern_lower.strip("*.") in lowered and pattern_lower.startswith("*."):
            return True
    return False


def capture_files(
    root: str,
    *,
    excludes: Sequence[str] = DEFAULT_EXCLUDES,
    max_file_bytes: Optional[int] = None,
    include_relative: Optional[Iterable[str]] = None,
) -> Tuple[List[LocalFile], List[str]]:
    """捕获本地快照：返回（文件清单, 被排除的相对路径）。

    上传中本地文件被修改时，已捕获快照仍按捕获时的哈希上传（SPEC §21.4）。
    """
    wanted = set(include_relative) if include_relative is not None else None
    files: List[LocalFile] = []
    excluded: List[str] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in sorted(filenames):
            absolute = os.path.join(current, filename)
            relative = os.path.relpath(absolute, root).replace("\\", "/")
            if wanted is not None and relative not in wanted:
                continue
            if _is_excluded(relative, excludes):
                excluded.append(relative)
                continue
            size = os.path.getsize(absolute)
            if max_file_bytes is not None and size > max_file_bytes:
                raise ValidationError(
                    f"文件超过体积上限，需显式拒绝或排队（不得静默丢失）: {relative} ({size} bytes)"
                )
            files.append(LocalFile(absolute, relative, size, sha256_file(absolute)))
    return files, excluded


def scan_for_secrets(files: Sequence[LocalFile], *, max_scan_bytes: int = 2 * 1024 * 1024) -> SecretScanResult:
    """对已捕获快照做秘密扫描（文本类文件；二进制跳过但记录）。"""
    hits: List[Dict[str, Any]] = []
    scanned = 0
    for item in files:
        if item.size_bytes > max_scan_bytes:
            continue
        try:
            with open(item.absolute_path, "rb") as fh:
                raw = fh.read(max_scan_bytes)
            text = raw.decode("utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for label, pattern in _SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                hits.append({"path": item.relative_path, "kind": label, "match": match.group(0)[:12] + "…"})
    return SecretScanResult(clean=not hits, hits=hits, scanned_files=scanned)


def snapshot_digest(files: Sequence[LocalFile]) -> str:
    """内容快照摘要：只由（相对路径, 大小, 内容哈希, 来源版本）决定。"""
    rows = [
        {
            "path": item.relative_path,
            "size": item.size_bytes,
            "sha256": item.sha256,
            "source_object_id": item.source_object_id,
            "source_version": item.source_version,
            "kind": item.kind,
        }
        for item in files
    ]
    rows.sort(key=lambda row: (str(row["path"]), str(row["sha256"])))
    return digest_of(rows)


def build_manifest(
    *,
    scope: str,
    scope_ref: str,
    files: Sequence[LocalFile],
    policy: Optional[ArchivePolicy] = None,
    source_versions: Optional[Dict[str, str]] = None,
    code_version: Optional[str] = None,
    contract_version: Optional[str] = None,
    rule_version: Optional[str] = None,
    workflow_version: Optional[str] = None,
    remote_root: Optional[str] = None,
    secret_scan: Optional[SecretScanResult] = None,
    excluded: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """构造不可变 manifest（dict 形式，持久化时由存储层包装为 ArchiveManifest）。

    这个函数**不做上传**，只固化"这次要归档什么、校验值是多少"。
    """
    if scope not in ARCHIVE_SCOPES:
        raise ValidationError(f"未知归档范围: {scope}")
    if not scope_ref and scope != "snapshot":
        raise ValidationError("除 snapshot 外的归档必须给出 scope_ref（Case/Conversation ID）")

    total = sum(item.size_bytes for item in files)
    if policy is not None:
        policy.validate()
        if total > policy.max_total_bytes:
            raise ValidationError(f"本次归档总体积超限: {total} > {policy.max_total_bytes}")
    snapshot = snapshot_digest(files)
    manifest = {
        "archive_id": archive_id_for(scope=scope, scope_ref=scope_ref, snapshot_digest=snapshot),
        "scope": scope,
        "scope_ref": scope_ref,
        "snapshot_digest": snapshot,
        "entries": [
            {
                "relative_path": item.relative_path,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "source_object_id": item.source_object_id,
                "source_version": item.source_version,
                "kind": item.kind,
            }
            for item in files
        ],
        "total_bytes": total,
        "source_versions": dict(source_versions or {}),
        "policy": {"policy_id": policy.policy_id, "version": policy.version} if policy else None,
        "code_version": code_version,
        "contract_version": contract_version,
        "rule_version": rule_version,
        "workflow_version": workflow_version,
        "remote_root": remote_root,
        "excluded": sorted(excluded or []),
        "secret_scan": (secret_scan or SecretScanResult(clean=True)).to_dict(),
    }
    manifest["manifest_digest"] = digest_of(
        {k: v for k, v in manifest.items() if k not in ("manifest_digest", "remote_root")}
    )
    return manifest


def verify_remote(
    *,
    manifest: Dict[str, Any],
    remote_files: Dict[str, str],
    require_hash: bool = True,
) -> Dict[str, Any]:
    """校验远端内容与 manifest 一致（SPEC §12.5、§21.4）。

    `remote_files`: 远端相对路径 → **内容哈希**（sha256）。仅数量/大小一致不算通过：
    调用方必须提供哈希；拿不到可信哈希时只能返回 `PENDING_VERIFICATION`。
    """
    entries = {entry["relative_path"]: entry for entry in manifest.get("entries", [])}
    missing = sorted(path for path in entries if path not in remote_files)
    extra = sorted(path for path in remote_files if path not in entries)
    mismatched: List[Dict[str, Any]] = []
    unverifiable: List[str] = []
    if require_hash:
        for path, entry in entries.items():
            remote_hash = remote_files.get(path)
            if remote_hash is None:
                continue
            if not remote_hash:
                unverifiable.append(path)
                continue
            if remote_hash != entry["sha256"]:
                mismatched.append({"path": path, "expected": entry["sha256"], "actual": remote_hash})

    ok = not missing and not mismatched and not extra and not unverifiable
    state = "VERIFIED" if ok else ("PENDING_VERIFICATION" if unverifiable and not (missing or mismatched or extra) else "PARTIAL")
    return {
        "ok": ok,
        "state": state,
        "missing": missing,
        "extra": extra,
        "mismatched": mismatched,
        "unverifiable": unverifiable,
        "verified_files": len(entries) - len(missing) - len(mismatched) - len(unverifiable),
    }


def require_verified(result: Dict[str, Any]) -> None:
    """只有校验通过才允许写完成标记。"""
    if not result.get("ok"):
        raise ArchiveIntegrityError(
            "归档完整性校验未通过，不得标记完成",
            detail={
                "state": result.get("state"),
                "missing": result.get("missing"),
                "mismatched": result.get("mismatched"),
                "unverifiable": result.get("unverifiable"),
            },
        )


def restore_plan(manifest: Dict[str, Any], *, isolate_dir: str) -> Dict[str, Any]:
    """恢复计划：先建任务与清单校验，默认进隔离目录且只读（SPEC §12.5、§21.4）。"""
    if not isolate_dir or os.path.abspath(isolate_dir) == os.path.abspath(os.sep):
        raise ValidationError("恢复必须指定隔离目录，禁止直接恢复到主数据目录")
    return {
        "archive_id": manifest.get("archive_id"),
        "isolate_dir": isolate_dir,
        "read_only_mode": True,
        "files": [entry["relative_path"] for entry in manifest.get("entries", [])],
        "manifest_digest": manifest.get("manifest_digest"),
        "replay_external_actions": False,
        "approvals_invalidated": True,
        "notes": "恢复后审批默认失效；人工批准合入资料不等于批准重发历史动作",
    }


def verify_restore(*, manifest: Dict[str, Any], restored_root: str) -> Dict[str, Any]:
    """恢复后逐文件校验哈希 + manifest 引用闭合（SPEC §21.4）。"""
    results: List[Dict[str, Any]] = []
    ok = True
    for entry in manifest.get("entries", []):
        absolute = os.path.join(restored_root, entry["relative_path"])
        if not os.path.exists(absolute):
            ok = False
            results.append({"path": entry["relative_path"], "status": "missing"})
            continue
        actual = sha256_file(absolute)
        match = actual == entry["sha256"]
        ok = ok and match
        results.append({"path": entry["relative_path"], "status": "ok" if match else "hash_mismatch", "actual": actual, "expected": entry["sha256"]})
    return {"ok": ok, "files": results, "manifest_digest": manifest.get("manifest_digest")}
