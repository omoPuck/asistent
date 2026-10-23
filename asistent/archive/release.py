"""A-5「构建即发布」：把发布产物的归档做成构建流程的一步（`docs/REQUIREMENTS-ADDENDUM.md` A-5）。

一步完成：**快照 → manifest → 上传 → 逐文件哈希回读校验 → 记录归档状态**，
且可重复执行（同一 `archive_id` 重跑不重复上传；内容变化派生新 id）。

产物（release artifacts）：
1. 源码包：`git archive --format=tar.gz -o <out>/<label>-src.tar.gz HEAD`；
2. 完整历史：`git bundle create <out>/<label>.git.bundle --all`（离线可克隆的完整历史）；
3. `manifest.json`：逐文件 sha256 + 来源类型 + 版本信息 + 策略；
   三件产物都是**我们自己生成的字节** → 条目 `content_origin=RAW_BYTES`（可做字节级校验）。

`--dry-run`（`dry_run=True`）：完全不碰真实网盘，也不要求已授权 —— 打包与 manifest 照常生成，
归档回路改走契约自带的 `LocalDirPanClient` 到临时目录，于是"上传 → 逐文件回读 → 完整性判定"
这些逻辑在离线状态也能被验证（这是 Lead 会亲自跑的入口）。

凭据纪律：本模块从不读取 bdpan 配置文件；`approval_ref` 是**审批引用**而不是凭据；
打印的 argv/输出里不含 token/BDUSS/cookie。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..contracts.enums import ArchiveState
from ..contracts.errors import ValidationError
from ..contracts.ids import sha256_file
from ..contracts.pan import LocalDirPanClient
from ..contracts.timeutil import utcnow_iso
from ..pan.bdpan import BdpanCliPanClient
from ..pan.errors import PanNotAuthorized, PanWriteLocked
from .engine import ArchiveEngine, ArchiveLayout, ArchiveOutcome
from .manifest import (
    SourceFile,
    build_archive_manifest,
    capture_source_tree,
    content_origin_summary,
    freeze_snapshot,
)

__all__ = [
    "EXIT_CODES",
    "RELEASE_STATE_SCHEMA",
    "ReleasePlan",
    "ReleaseResult",
    "build_release_snapshot",
    "exit_code_for",
    "main",
    "publish_release",
    "run_release",
]

RELEASE_STATE_SCHEMA = "mvp7-release-state-1"

#: 退出码（`scripts/release.sh` 直接透传，部署流程据此判定）
EXIT_CODES: Dict[str, int] = {
    "VERIFIED": 0,
    "PARTIAL": 2,
    "PENDING": 2,
    "UNKNOWN": 2,
    "NOT_AUTHORIZED": 3,
    "APPROVAL_REQUIRED": 3,
    "CONFIG_ERROR": 4,
    "INTEGRITY_ERROR": 4,
}


def _contract_version() -> Optional[str]:
    try:
        from ..contracts.version import CONTRACT_VERSION  # type: ignore

        return str(CONTRACT_VERSION)
    except Exception:  # noqa: BLE001 - 版本信息缺失不应阻断发布，但会记空值
        return None


def _run_git(repo_root: str, args: Sequence[str], *, timeout: int = 600) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"git 执行失败：{exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


@dataclass
class ReleasePlan:
    """一次发布的计划与产物（dry-run 也产出完整计划）。"""

    label: str
    repo_root: str
    out_dir: str
    stage_dir: str
    archive_id: str
    manifest: Dict[str, Any] = field(default_factory=dict)
    files: List[SourceFile] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    commit: Optional[str] = None
    dirty: bool = False
    remote_root: str = ""
    code_version: Optional[str] = None
    contract_version: Optional[str] = None
    git_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "repo_root": self.repo_root,
            "out_dir": self.out_dir,
            "archive_id": self.archive_id,
            "commit": self.commit,
            "dirty_worktree": self.dirty,
            "remote_root": self.remote_root,
            "code_version": self.code_version,
            "contract_version": self.contract_version,
            "manifest_digest": self.manifest.get("manifest_digest"),
            "content_origin_summary": content_origin_summary(self.manifest),
            "artifacts": list(self.artifacts),
            "git_errors": list(self.git_errors),
        }


@dataclass
class ReleaseResult:
    state: str
    archive_id: str
    manifest_digest: str = ""
    exit_code: int = 0
    dry_run: bool = False
    transfers: int = 0
    verified: List[str] = field(default_factory=list)
    uploaded: List[str] = field(default_factory=list)
    reused: List[str] = field(default_factory=list)
    repaired: List[str] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    unknown_objects: List[str] = field(default_factory=list)
    integrity_claim: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_text: Optional[str] = None
    next_step: Optional[str] = None
    state_record_path: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"release state={self.state} archive_id={self.archive_id} transfers={self.transfers} "
            f"verified={len(self.verified)} uploaded={len(self.uploaded)} reused={len(self.reused)} "
            f"failed={len(self.failed)} unknown={len(self.unknown_objects)} dry_run={self.dry_run}"
        )


def build_release_snapshot(
    *,
    repo_root: str,
    out_dir: str,
    label: str,
    stage_dir: Optional[str] = None,
    git_bundle: bool = True,
    remote_root: str = "",
    contract_version: Optional[str] = None,
) -> ReleasePlan:
    """生成发布产物（源码包 + 完整历史 bundle）并构造 manifest（**不上传**）。"""
    repo_root = os.path.abspath(repo_root)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    stage_dir = os.path.abspath(stage_dir or os.path.join(out_dir, "_stage"))
    os.makedirs(stage_dir, exist_ok=True)

    plan = ReleasePlan(
        label=label,
        repo_root=repo_root,
        out_dir=out_dir,
        stage_dir=stage_dir,
        archive_id="",
        remote_root=remote_root,
        contract_version=contract_version or _contract_version(),
    )

    code, out, err = _run_git(repo_root, ["rev-parse", "HEAD"])
    if code != 0:
        raise ValidationError(f"release 必须在 git 仓库内运行：{err.strip()[:200]}")
    plan.commit = out.strip()
    plan.code_version = plan.commit
    status_code, status_out, _ = _run_git(repo_root, ["status", "--porcelain"])
    plan.dirty = bool(status_out.strip())

    src_name = f"{label}-src.tar.gz"
    src_path = os.path.join(out_dir, src_name)
    code, _out, err = _run_git(repo_root, ["archive", "--format=tar.gz", "-o", src_path, "HEAD"])
    if code != 0:
        raise ValidationError(f"git archive 失败：{err.strip()[:200]}")
    plan.artifacts.append(_artifact(src_path, "source_tarball"))

    if git_bundle:
        bundle_path = os.path.join(out_dir, f"{label}.git.bundle")
        code, _out, err = _run_git(repo_root, ["bundle", "create", bundle_path, "--all"])
        if code != 0:
            # bundle 失败不静默跳过：记进计划，由调用方决定是否阻断（默认阻断见 run_release）
            plan.git_errors.append(f"git bundle 失败：{err.strip()[:200]}")
        else:
            plan.artifacts.append(_artifact(bundle_path, "git_bundle"))

    names = [item["name"] for item in plan.artifacts]
    files, _excluded, scan = capture_source_tree(
        out_dir,
        include_relative=names,
        content_origin="RAW_BYTES",
        scan_secrets=True,
    )
    if len(files) != len(names):
        raise ValidationError(
            "发布产物不完整：捕获到的文件数与生成的产物数不一致",
            detail={"expected": names, "captured": [item.relative_path for item in files]},
        )
    staged = freeze_snapshot(files, stage_dir)
    plan.files = staged
    plan.manifest = build_archive_manifest(
        scope="snapshot",
        scope_ref=label,
        files=staged,
        code_version=plan.code_version,
        contract_version=plan.contract_version,
        remote_root=remote_root or ArchiveLayout().archive_dir("<archive_id>"),
        secret_scan=scan,
        extra={
            "release": {
                "label": label,
                "commit": plan.commit,
                "dirty_worktree": plan.dirty,
                "artifacts": [item["name"] for item in plan.artifacts],
                "pipeline": "A-5 构建即发布",
            }
        },
    )
    # remote_root 里带真实 archive_id（契约的 manifest_digest 不覆盖 remote_root，所以这里可以回填）
    plan.archive_id = plan.manifest["archive_id"]
    plan.manifest["remote_root"] = ArchiveLayout().archive_dir(plan.archive_id)
    return plan


def _artifact(path: str, kind: str) -> Dict[str, Any]:
    return {
        "name": os.path.basename(path),
        "path": path,
        "kind": kind,
        "size_bytes": os.path.getsize(path),
        "sha256": sha256_file(path),
    }


def publish_release(
    plan: ReleasePlan,
    pan: Any = None,
    *,
    dry_run: bool = False,
    approval_ref: Optional[str] = None,
    state_dir: Optional[str] = None,
    store: Any = None,
    layout: Optional[ArchiveLayout] = None,
    mirror_dir: Optional[str] = None,
) -> ReleaseResult:
    """执行归档一步：上传 → 逐文件回读校验 → 记录发布状态。

    `dry_run=True` 时用契约的 `LocalDirPanClient` 在本地目录里跑完整回路（可用
    `mirror_dir` 指定固定目录，从而离线验证幂等性：第二次运行零上传），
    不接触真实网盘、不要求已授权。
    """
    layout = layout or ArchiveLayout()
    result = ReleaseResult(state=ArchiveState.PENDING.value, archive_id=plan.archive_id, dry_run=dry_run)
    workdir: Optional[str] = None
    if dry_run:
        workdir = mirror_dir or tempfile.mkdtemp(prefix="asistent-release-dryrun-")
        pan = LocalDirPanClient(workdir)
        result.notes.append(f"dry-run：归档回路走本地目录模拟远端（{workdir}），不接触真实网盘")
    elif pan is None:
        pan = BdpanCliPanClient(root_prefix="asistent")

    if not dry_run:
        unlock = getattr(pan, "unlock_writes", None)
        if getattr(pan, "writes_locked", False):
            if not approval_ref:
                result.state = "APPROVAL_REQUIRED"
                result.error_code = "APPROVAL_REQUIRED"
                result.error_text = "归档通道写入未获批准：需要 approval_ref"
                result.next_step = "取得审批记录后重跑：scripts/release.sh --approval-ref <审批记录 ID>"
                result.exit_code = EXIT_CODES["APPROVAL_REQUIRED"]
                _record_state(result, plan, state_dir)
                return result
            if callable(unlock):
                unlock(approval_ref)
                result.notes.append("写入已按 approval_ref 解锁（审批引用，非凭据）")

    engine = ArchiveEngine(pan, layout=layout, state_dir=state_dir, store=store)
    try:
        outcome: ArchiveOutcome = engine.archive(plan.manifest, plan.files)
    except PanWriteLocked as exc:
        result.state = "APPROVAL_REQUIRED"
        result.error_code = exc.code
        result.error_text = exc.message
        result.next_step = exc.detail.get("next_step")
        result.exit_code = EXIT_CODES["APPROVAL_REQUIRED"]
        _record_state(result, plan, state_dir)
        return result
    except PanNotAuthorized as exc:
        result.state = "NOT_AUTHORIZED"
        result.error_code = exc.code
        result.error_text = exc.message
        result.next_step = exc.detail.get("next_step")
        result.exit_code = EXIT_CODES["NOT_AUTHORIZED"]
        _record_state(result, plan, state_dir)
        return result
    except Exception as exc:  # noqa: BLE001 - 归档异常必须变成显式状态，不许静默
        result.state = "INTEGRITY_ERROR"
        result.error_code = getattr(exc, "code", type(exc).__name__)
        result.error_text = str(exc)
        result.exit_code = EXIT_CODES["INTEGRITY_ERROR"]
        _record_state(result, plan, state_dir)
        return result

    result.state = outcome.state
    if outcome.error_code and outcome.state == ArchiveState.FAILED.value:
        # 让状态名与"可操作的原因"一致：部署流程据此直接判定（未授权 vs 一般失败）
        result.state = outcome.error_code
    result.manifest_digest = outcome.manifest_digest
    result.transfers = outcome.transfer_count
    result.verified = list(outcome.verified)
    result.uploaded = list(outcome.uploaded)
    result.reused = list(outcome.reused)
    result.repaired = list(outcome.repaired)
    result.failed = list(outcome.failed)
    result.unknown_objects = list(outcome.unknown_objects)
    result.integrity_claim = dict(outcome.integrity_claim)
    result.error_code = outcome.error_code
    result.error_text = outcome.error_text
    result.next_step = outcome.next_step
    result.notes.extend(outcome.notes)
    result.exit_code = exit_code_for(result.state, error_code=result.error_code)
    if outcome.unknown_objects and result.state != ArchiveState.FAILED.value:
        result.notes.append(
            "存在结果未知的对象：保持未知状态，重跑时先 stat/回读判定（本模块不会盲目重传）"
        )
    _record_state(result, plan, state_dir)
    if workdir is not None:
        result.notes.append(f"dry-run 远端模拟目录内容保留在 {workdir}（可人工复核）")
    return result


def exit_code_for(state: str, *, error_code: Optional[str] = None) -> int:
    if error_code == "NOT_AUTHORIZED":
        return EXIT_CODES["NOT_AUTHORIZED"]
    return EXIT_CODES.get(state, EXIT_CODES["INTEGRITY_ERROR"])


def _record_state(result: ReleaseResult, plan: ReleasePlan, state_dir: Optional[str]) -> None:
    """记录发布/归档状态（A-5 的"记录归档状态"一步；本地状态不参与可信判定）。"""
    directory = state_dir or os.path.join(tempfile.gettempdir(), "asistent-release-state")
    os.makedirs(directory, exist_ok=True)
    safe = result.archive_id.replace(":", "_").replace("/", "_")
    path = os.path.join(directory, f"release-{safe}.json")
    payload = {
        "schema_version": RELEASE_STATE_SCHEMA,
        "recorded_at": utcnow_iso(),
        "state": result.state,
        "exit_code": result.exit_code,
        "error_code": result.error_code,
        "error_text": result.error_text,
        "next_step": result.next_step,
        "dry_run": result.dry_run,
        "release": plan.to_dict(),
        "outcome": result.to_dict(),
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fh.write("\n")
    os.replace(tmp, path)
    result.state_record_path = path


def run_release(
    *,
    repo_root: str,
    out_dir: str,
    label: str,
    dry_run: bool = False,
    approval_ref: Optional[str] = None,
    state_dir: Optional[str] = None,
    pan: Any = None,
    store: Any = None,
    mirror_dir: Optional[str] = None,
    require_git_bundle: bool = True,
) -> Tuple[ReleasePlan, ReleaseResult]:
    plan = build_release_snapshot(repo_root=repo_root, out_dir=out_dir, label=label)
    if require_git_bundle and plan.git_errors:
        result = ReleaseResult(
            state="CONFIG_ERROR",
            archive_id=plan.archive_id,
            error_code="GIT_BUNDLE_FAILED",
            error_text="; ".join(plan.git_errors),
            next_step="修复 git bundle 失败（完整历史是发布产物的必备部分）后重跑",
            exit_code=EXIT_CODES["CONFIG_ERROR"],
        )
        _record_state(result, plan, state_dir)
        return plan, result
    return plan, publish_release(
        plan,
        pan,
        dry_run=dry_run,
        approval_ref=approval_ref,
        state_dir=state_dir,
        store=store,
        mirror_dir=mirror_dir,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="asistent.archive.release",
        description="A-5 构建即发布：产物快照 → manifest → 上传 → 逐文件回读校验 → 记录状态",
    )
    parser.add_argument("--repo", default=os.getcwd(), help="git 仓库根目录（默认当前目录）")
    parser.add_argument("--out", default=None, help="产物输出目录（默认 <repo>/../asistent-release-out）")
    parser.add_argument("--label", default=None, help="发布标签（默认取 commit 短哈希）")
    parser.add_argument("--dry-run", action="store_true", help="离线跑通打包+manifest+归档回路，不接触真实网盘")
    parser.add_argument("--approval-ref", default=None, help="审批记录 ID（写入授权引用，非凭据）")
    parser.add_argument("--state-dir", default=None, help="归档状态记录目录")
    parser.add_argument("--mirror", default=None, help="dry-run 用的本地镜像目录（可复用同一目录验证幂等）")
    parser.add_argument("--binary", default=None, help="bdpan 路径（默认按设备默认路径/PATH 解析）")
    parser.add_argument("--root-prefix", default="asistent", help="网盘 `/apps/bdpan/` 下的子前缀")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果（供部署流程消费）")
    args = parser.parse_args(list(argv) if argv is not None else None)

    repo = os.path.abspath(args.repo)
    out_dir = os.path.abspath(args.out or os.path.join(os.path.dirname(repo), "asistent-release-out"))
    label = args.label
    if not label:
        code, out, _err = _run_git(repo, ["rev-parse", "--short", "HEAD"])
        label = f"asistent-{out.strip() or 'unknown'}" if code == 0 else "asistent-unknown"

    pan = None
    if not args.dry_run:
        try:
            pan = BdpanCliPanClient(binary=args.binary, root_prefix=args.root_prefix)
        except Exception as exc:  # noqa: BLE001 - 环境问题必须显式报出
            print(f"[FAIL] 无法初始化 bdpan 通道：{type(exc).__name__}: {exc}")
            return EXIT_CODES["CONFIG_ERROR"]

    plan, result = run_release(
        repo_root=repo,
        out_dir=out_dir,
        label=label,
        dry_run=args.dry_run,
        approval_ref=args.approval_ref,
        state_dir=args.state_dir,
        pan=pan,
        mirror_dir=args.mirror,
    )

    if args.json:
        print(json.dumps({"plan": plan.to_dict(), "result": result.to_dict()}, ensure_ascii=False, indent=2))
    else:
        print(f"[plan] label={plan.label} commit={(plan.commit or '')[:12]} archive_id={plan.archive_id}")
        print(f"[plan] artifacts={[item['name'] for item in plan.artifacts]}")
        for item in plan.artifacts:
            print(f"       - {item['name']} {item['size_bytes']}B sha256={item['sha256'][:16]}…")
        print(f"[plan] manifest_digest={plan.manifest.get('manifest_digest')}")
        print(f"[plan] content_origin={content_origin_summary(plan.manifest)}")
        print(f"[run ] {result.summary()}")
        if result.integrity_claim:
            print(f"[run ] integrity_claim: {result.integrity_claim.get('statement')}")
            if result.integrity_claim.get("warning"):
                print(f"[warn] {result.integrity_claim['warning']}")
        if result.failed:
            for item in result.failed:
                print(f"[fail] {item.get('relative_path')}: {item.get('reason')} {item.get('detail')}")
        if result.state_record_path:
            print(f"[state] {result.state_record_path}")
        if result.error_text:
            print(f"[error] {result.error_code}: {result.error_text}")
        if result.next_step:
            print(f"[next] {result.next_step}")
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())
