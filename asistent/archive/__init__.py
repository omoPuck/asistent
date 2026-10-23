"""事务归档与隔离恢复（SPEC §12.4/§12.5、§21.4；A-5 构建即发布）。

模块分工：
- `manifest.py`：快照捕获 / 冻结 / 不可变 manifest（含逐条 `content_origin`）/ 自校验 / 完整性声明；
- `engine.py`：归档状态机 —— 逐文件回读校验、部分失败只补传失败对象、结果未知先判定、
  幂等复用、完成标记、状态落库（`ArchiveManifest` / `ArchiveJob` / `Case.archive_state`）；
- `restore.py`：隔离恢复 —— 保护目录必填、只读、逐文件哈希 + 引用闭合 + 重建索引 + 恢复记录；
- `release.py`：A-5 发布一步 —— 产物快照 + `git bundle` + manifest → 上传 → 回读校验 → 记录状态。

底层通道在 `asistent.pan`（真实 bdpan CLI）或契约自带的 `LocalDirPanClient`（离线回路）。
"""

from __future__ import annotations

from .engine import ARCHIVE_BASE_DIR, RESTORE_RECORD_DIR, ArchiveEngine, ArchiveLayout, ArchiveOutcome
from .manifest import (
    CONTENT_ORIGIN_METADATA_PROJECTION,
    CONTENT_ORIGIN_RAW_BYTES,
    MANIFEST_SCHEMA,
    SourceFile,
    build_archive_manifest,
    capture_source_tree,
    content_origin_summary,
    freeze_snapshot,
    integrity_claim,
    manifest_bytes,
    object_digest,
    validate_manifest,
)
from .release import (
    EXIT_CODES,
    ReleasePlan,
    ReleaseResult,
    build_release_snapshot,
    exit_code_for,
    publish_release,
    run_release,
)
from .restore import RESTORE_RECORD_NAME, RestoreOutcome, restore_archive

__all__ = [
    "ARCHIVE_BASE_DIR",
    "CONTENT_ORIGIN_METADATA_PROJECTION",
    "CONTENT_ORIGIN_RAW_BYTES",
    "EXIT_CODES",
    "MANIFEST_SCHEMA",
    "RESTORE_RECORD_DIR",
    "RESTORE_RECORD_NAME",
    "ArchiveEngine",
    "ArchiveLayout",
    "ArchiveOutcome",
    "ReleasePlan",
    "ReleaseResult",
    "RestoreOutcome",
    "SourceFile",
    "build_archive_manifest",
    "build_release_snapshot",
    "capture_source_tree",
    "content_origin_summary",
    "exit_code_for",
    "freeze_snapshot",
    "integrity_claim",
    "manifest_bytes",
    "object_digest",
    "publish_release",
    "restore_archive",
    "run_release",
    "validate_manifest",
]
