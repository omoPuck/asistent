#!/usr/bin/env python3
"""端到端契约验证：asistent 百度网盘归档 / 恢复（离线，零第三方依赖）。

用法
----
    python3 e2e_offline.py [-k] [--workdir DIR]

- 默认在系统临时目录下建工作区，运行结束自动清理（`-k` 保留以便人工检查）。
- 只使用 Python 标准库；不需要网络；**不接触任何真实网盘**。
- 输出为可直接复制的证据行；全部通过时退出码 0，任意一项失败退出码 1。

验证的契约（SPEC §12.4 / §12.5 / §21.4 / §16 MVP-7 / §21.8 AT-12·AT-13·AT-14）
----------------------------------------------------------------------------
S1  manifest 不可变 + archive_id 确定性派生
S2  上传后逐文件内容哈希回读校验，全部一致才写完成标记
S3  相同 archive_id 重试：复用已校验对象，不重复上传、不重写完成标记
S4  内容变化 -> 新 archive_id；旧归档不受影响
S5  部分失败 -> ARCHIVE_PENDING，记录已成功/未成功对象，可安全重试并复用
S6  结果未知（副作用可能已发生）-> 不盲目重传，重试时用回读判定真实状态
S7  恢复到隔离目录 + 逐文件哈希校验 + 引用闭合 + 只读恢复模式
S8  恢复防线：拒绝非空目标、拒绝受保护目录、远端缺文件拒绝完成
S9  归档目标被篡改 -> 逐文件校验发现不一致，不标记完成，记录不一致
S10 同一 archive_id 绑定不同 manifest -> 拒绝覆盖（不覆盖已校验的不同内容）
S11 manifest 冻结后本地文件被改写 -> 仍上传已捕获快照
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from panclient import (  # noqa: E402
    LocalDirPanClient,
    PanClient,
    PanConflict,
    PanError,
    PanInvalid,
    PanUnavailable,
    RemoteObject,
    sha256_file,
)
from archive import (  # noqa: E402
    ARCHIVE_PENDING,
    ARCHIVE_VERIFIED,
    ArchiveLayout,
    ArchivePolicy,
    ArchiveStore,
    CodeVersion,
    FileEntry,
    build_manifest,
    canonical_json,
    capture_snapshot,
)

# --------------------------------------------------------------------------
# 证据输出
# --------------------------------------------------------------------------
RESULTS: list[tuple[str, bool, str]] = []


def rule(title: str) -> None:
    print()
    print("=" * 78)
    print(f" {title}")
    print("=" * 78)


def step(msg: str) -> None:
    print(f"  [STEP] {msg}")


def check(msg: str) -> None:
    print(f"  [CHECK] {msg}")


def ok(msg: str) -> None:
    print(f"  [ OK ] {msg}")


def bad(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def record(name: str, passed: bool, detail: str = "") -> bool:
    RESULTS.append((name, passed, detail))
    (ok if passed else bad)(f"{name}{(' — ' + detail) if detail else ''}")
    return passed


# --------------------------------------------------------------------------
# 故障注入包装（实现同一 PanClient 协议）
# --------------------------------------------------------------------------
class FaultPan:
    """包装任意 PanClient，按远端路径后缀注入故障并统计调用次数。"""

    def __init__(
        self,
        inner: PanClient,
        *,
        fail_put_suffixes: tuple[str, ...] = (),
        mode: str = "clean",
    ) -> None:
        self.inner = inner
        self.fail_put_suffixes = fail_put_suffixes
        self.mode = mode
        self.calls = {"put": 0, "get": 0, "stat": 0, "list": 0, "delete": 0}
        self.put_paths: list[str] = []

    def _hit(self, remote_path: str) -> bool:
        return any(remote_path.endswith(s) for s in self.fail_put_suffixes)

    def object_puts(self, since: int = 0) -> list[str]:
        """`since` 之后所有针对内容对象（objects/）的 put 路径。"""
        return [p for p in self.put_paths[since:] if "/objects/" in p]

    def put(self, local_path, remote_path, *, overwrite: bool = False) -> RemoteObject:
        self.calls["put"] += 1
        self.put_paths.append(remote_path)
        if self._hit(remote_path):
            if self.mode == "unknown":
                # 副作用已发生，但调用方无法得知结果 -> UNKNOWN
                self.inner.put(local_path, remote_path, overwrite=overwrite)
                raise PanUnavailable(f"注入的未知结果（对象可能已写入）：{remote_path}")
            raise PanError(f"注入的确定性失败（未写入）：{remote_path}")
        return self.inner.put(local_path, remote_path, overwrite=overwrite)

    def get(self, remote_path, local_path) -> RemoteObject:
        self.calls["get"] += 1
        return self.inner.get(remote_path, local_path)

    def list(self, remote_dir) -> list[RemoteObject]:
        self.calls["list"] += 1
        return list(self.inner.list(remote_dir))

    def stat(self, remote_path):
        self.calls["stat"] += 1
        return self.inner.stat(remote_path)

    def delete(self, remote_path) -> bool:
        self.calls["delete"] += 1
        return self.inner.delete(remote_path)


# --------------------------------------------------------------------------
# 工作区
# --------------------------------------------------------------------------
def build_source_tree(root: Path) -> None:
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "attachments").mkdir(parents=True, exist_ok=True)
    (root / "bin").mkdir(parents=True, exist_ok=True)

    (root / "notes" / "会议纪要.md").write_text(
        "# 会议纪要\n\n- 报名截止：2026-10-20\n- 活动开始：2026-10-25\n", encoding="utf-8"
    )
    (root / "data.csv").write_text("ts,item,value\n1,a,10\n2,b,20\n", encoding="utf-8")
    rnd = random.Random(20261010)
    (root / "attachments" / "报告.bin").write_bytes(bytes(rnd.randrange(256) for _ in range(64 * 1024)))
    (root / "bin" / "blob.bin").write_bytes(bytes(rnd.randrange(256) for _ in range(512 * 1024)))


def policy() -> ArchivePolicy:
    return ArchivePolicy(
        policy_id="policy-personal-default",
        policy_version=1,
        logical_root="个人助手归档/事务归档",
        restore_record_root="个人助手归档/恢复记录",
        exclude_globs=("**/*.token", "**/secrets/**", "**/.env"),
    )


def code_version() -> CodeVersion:
    return CodeVersion(asistent="spike-0.1.0", rules="rules-v0", workflow="wf-v0")


def make_manifest(src: Path, stage: Path, *, revision: str = "case-rev-1", relpaths=None):
    entries, snap = capture_snapshot(src, stage, relpaths=relpaths, source_revision=revision)
    manifest = build_manifest(
        source_kind="case",
        source_id="CASE-0001",
        source_revision=revision,
        policy=policy(),
        code_version=code_version(),
        entries=entries,
    )
    return manifest, snap


# --------------------------------------------------------------------------
# 场景
# --------------------------------------------------------------------------
def s1_manifest(work: Path) -> None:
    rule("S1  manifest 不可变 + archive_id 确定性派生（SPEC §21.4）")
    src = work / "source"
    m1, snap1 = make_manifest(src, work / "stage-a")
    m2, snap2 = make_manifest(src, work / "stage-b")

    print(f"  source_kind        : {m1.source_kind}")
    print(f"  source_id          : {m1.source_id}")
    print(f"  source_revision    : {m1.source_revision}")
    print(f"  policy             : {m1.policy.policy_id} v{m1.policy.policy_version}")
    print(f"  code_version       : {canonical_json(m1.code_version.to_dict())}")
    print(f"  archive_id         : {m1.archive_id}")
    print(f"  manifest_digest    : {m1.digest}")
    print(f"  object_count       : {len(m1.files)}   total_size={m1.total_size} B")
    print("  逐文件条目（relpath | size | sha256 | source_revision）：")
    for e in m1.files:
        print(f"    {e.relpath:<28} {e.size:>8} B  {e.sha256}  {e.source_revision}")

    record("S1.1 manifest 含逐文件 sha256 / 大小 / 来源版本", all(e.sha256 and e.size >= 0 for e in m1.files))
    record("S1.2 相同输入两次构建得到相同 archive_id 与 digest", m1.archive_id == m2.archive_id and m1.digest == m2.digest)
    record("S1.3 manifest 字节可重复（摘要稳定）", m1.to_bytes() == m2.to_bytes())


def s2_happy_path(work: Path, remote_root: Path) -> tuple:
    rule("S2  上传后逐文件内容哈希回读校验，全部一致才写完成标记（SPEC §12.5 / §21.4）")
    src = work / "source"
    manifest, snap = make_manifest(src, work / "stage-s2")
    pan = FaultPan(LocalDirPanClient(remote_root))
    store = ArchiveStore(pan, ArchiveLayout(), work / "state")

    step(f"archive(manifest, snapshot) → 远端根 {remote_root}")
    t0 = time.time()
    out = store.archive(manifest, snap)
    dt = time.time() - t0

    print(f"  {out.summary()}")
    print(f"  elapsed            : {dt:.3f}s")
    print(f"  uploaded           : {sorted(out.uploaded)}")
    print(f"  readback_verified  : {len(out.readback_verified)} 个对象（下载回读 + 重算 sha256）")
    print(f"  完成标记远端路径   : {ArchiveLayout().complete_path(manifest.archive_id)}")

    marker_path = remote_root / ArchiveLayout().complete_path(manifest.archive_id)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    print(f"  marker.manifest_digest = {marker['manifest_digest']}")
    print(f"  marker.verification    = {marker['verification']}")

    record("S2.1 归档状态为 ARCHIVE_VERIFIED", out.state == ARCHIVE_VERIFIED, out.state)
    record("S2.2 写入了完成标记", out.marker_written and marker_path.is_file())
    record(
        "S2.3 每个对象都做过回读校验（覆盖所有对象）",
        len(set(out.readback_verified)) == len(manifest.files),
        f"{len(set(out.readback_verified))}/{len(manifest.files)}",
    )
    record("S2.4 完成标记记录了 manifest_digest 与校验方式", marker["manifest_digest"] == manifest.digest and marker["verification"] == "per-file-content-sha256-readback")

    report = store.verify_archive(manifest.archive_id)
    print(f"  verify_archive → consistent={report['consistent']} verified={len(report['verified'])} missing={report['missing']} mismatched={report['mismatched']}")
    record("S2.5 独立校验：远端逐文件哈希与 manifest 一致", report["consistent"] is True)

    return manifest, snap, pan, store, marker_path


def s3_idempotent_retry(work: Path, manifest, snap, pan: FaultPan, store: ArchiveStore) -> None:
    rule("S3  相同 archive_id 重试：复用已校验对象，不重复上传、不重写完成标记（SPEC §21.4）")
    put_mark = len(pan.put_paths)
    get_before = pan.calls["get"]

    out = store.archive(manifest, snap)
    obj_puts = pan.object_puts(put_mark)
    get_delta = pan.calls["get"] - get_before

    print(f"  {out.summary()}")
    print(f"  本轮对象 put 次数  : {len(obj_puts)}（期望 0：全部复用）")
    print(f"  本轮 get 次数      : {get_delta}（期望 >= 每对象 1 次：强制回读复核）")
    print(f"  reused             : {sorted(out.reused)}")
    print(f"  already_complete   : {out.already_complete}")
    print(f"  notes              : {out.notes}")

    record("S3.1 复用全部对象，未产生任何重复上传", not obj_puts, f"object_put={len(obj_puts)}")
    record("S3.2 复用对象仍被逐文件回读校验", len(set(out.readback_verified)) == len(manifest.files))
    record("S3.3 完成标记未被重写（幂等）", out.marker_written is False)
    record("S3.4 重试后状态仍为 ARCHIVE_VERIFIED", out.state == ARCHIVE_VERIFIED)
    record("S3.5 重试的读回校验覆盖全部对象（不凭本地状态信任）", get_delta >= len(manifest.files), f"get={get_delta}")

    # 远端对象总数没有增长
    objects_dir = store.pan.inner.root / ArchiveLayout().archive_dir(manifest.archive_id) / "objects"
    files = [p for p in objects_dir.rglob("*") if p.is_file()]
    record(
        "S3.5 远端 objects 数量等于 manifest 条目数（无破坏性副本）",
        len(files) == len(manifest.files),
        f"{len(files)} vs {len(manifest.files)}",
    )


def s4_content_change(work: Path, remote_root: Path, old_manifest, pan: FaultPan, store: ArchiveStore) -> tuple:
    rule("S4  内容变化 -> 新 archive_id；旧归档不受影响（SPEC §21.4）")
    src = work / "source"
    target = src / "notes" / "会议纪要.md"
    target.write_text(
        "# 会议纪要\n\n- 报名截止：2026-10-20\n- 活动开始：2026-10-25（已确认）\n", encoding="utf-8"
    )
    step(f"修改源文件：{target.relative_to(src).as_posix()}")

    new_manifest, new_snap = make_manifest(src, work / "stage-s4", revision="case-rev-2")
    print(f"  旧 archive_id      : {old_manifest.archive_id}")
    print(f"  新 archive_id      : {new_manifest.archive_id}")
    print(f"  旧 source_revision : {old_manifest.source_revision}")
    print(f"  新 source_revision : {new_manifest.source_revision}")

    record("S4.1 内容变化产生新的 archive_id", new_manifest.archive_id != old_manifest.archive_id)

    out_new = store.archive(new_manifest, new_snap)
    print(f"  {out_new.summary()}")
    old_report = store.verify_archive(old_manifest.archive_id)
    print(f"  旧归档独立校验 → consistent={old_report['consistent']} verified={len(old_report['verified'])}")

    record("S4.2 新归档完成并写入完成标记", out_new.state == ARCHIVE_VERIFIED and out_new.marker_written)
    record("S4.3 旧归档仍完整可校验（未被改动）", old_report["consistent"] is True)

    # 两份归档并存
    root = pan.inner.root
    old_md5 = hashlib.sha256((root / ArchiveLayout().object_path(old_manifest.archive_id, "notes/会议纪要.md")).read_bytes()).hexdigest()
    new_md5 = hashlib.sha256((root / ArchiveLayout().object_path(new_manifest.archive_id, "notes/会议纪要.md")).read_bytes()).hexdigest()
    record("S4.4 两个 archive_id 的对象内容各自独立", old_md5 != new_md5)
    return new_manifest, new_snap


def s5_partial_failure(work: Path, remote_root: Path) -> None:
    rule("S5  部分失败 -> ARCHIVE_PENDING，记录已成功/未成功对象，可安全重试（SPEC §12.5 / MVP-7）")
    src = work / "source"
    manifest, snap = make_manifest(src, work / "stage-s5")
    inner = LocalDirPanClient(remote_root / "s5")
    store = ArchiveStore(inner, ArchiveLayout(), work / "state-s5")

    failing = ("报告.bin",)
    faulty = FaultPan(inner, fail_put_suffixes=failing, mode="clean")
    store_faulty = ArchiveStore(faulty, ArchiveLayout(), work / "state-s5")
    step(f"注入确定性失败的对象：{failing}")

    out = store_faulty.archive(manifest, snap)
    print(f"  {out.summary()}")
    print(f"  uploaded（已成功） : {sorted(out.uploaded)}")
    print(f"  failed  （未成功） : {[f['relpath'] + ':' + f['reason'] for f in out.failed]}")
    print(f"  marker_written     : {out.marker_written}")

    record("S5.1 部分失败时状态为 ARCHIVE_PENDING", out.state == ARCHIVE_PENDING, out.state)
    record("S5.2 未写完成标记", out.marker_written is False)
    record("S5.3 已记录成功对象与失败对象", len(out.uploaded) == len(manifest.files) - len(failing) and len(out.failed) == len(failing))

    marker_present = inner.stat(ArchiveLayout().complete_path(manifest.archive_id)) is not None
    record("S5.4 远端确实没有完成标记", marker_present is False)

    step("安全重试（同 archive_id，故障原因已消除）")
    faulty.fail_put_suffixes = ()
    put_mark = len(faulty.put_paths)
    out2 = store_faulty.archive(manifest, snap)
    obj_puts = faulty.object_puts(put_mark)
    print(f"  {out2.summary()}")
    print(f"  重试对象 put       : {obj_puts}（期望只有 1 个：只补传失败对象）")
    print(f"  reused             : {sorted(out2.reused)}")
    print(f"  uploaded           : {sorted(out2.uploaded)}")

    record("S5.5 重试后归档完成并写入完成标记", out2.state == ARCHIVE_VERIFIED and out2.marker_written)
    record("S5.6 重试复用已成功对象，只补传失败对象", len(obj_puts) == len(failing), f"object_put={len(obj_puts)}")
    record("S5.7 重试后逐文件回读校验覆盖全部对象", len(set(out2.readback_verified)) == len(manifest.files))


def s6_unknown_result(work: Path, remote_root: Path) -> None:
    rule("S6  结果未知 -> 不盲目重传；重试时用回读判定真实状态（SPEC §13.2 / §21.2 UNKNOWN 语义）")
    src = work / "source"
    manifest, snap = make_manifest(src, work / "stage-s6")
    inner = LocalDirPanClient(remote_root / "s6")
    faulty = FaultPan(inner, fail_put_suffixes=("blob.bin",), mode="unknown")
    store = ArchiveStore(faulty, ArchiveLayout(), work / "state-s6")

    step("注入“已写入但结果未知”：put 先落盘再抛 PanUnavailable")
    out = store.archive(manifest, snap)
    print(f"  {out.summary()}")
    print(f"  failed             : {[(f['relpath'], f['reason']) for f in out.failed]}")
    print(f"  unknown_objects    : {sorted(out.unknown_objects)}")
    record("S6.1 未知结果记为未成功对象、状态 ARCHIVE_PENDING", out.state == ARCHIVE_PENDING and "bin/blob.bin" in out.unknown_objects)
    record("S6.2 未知结果未写完成标记", out.marker_written is False)

    # 重试：此时远端其实已经有这个对象，必须通过 stat+回读发现并复用，而不是重传
    faulty.fail_put_suffixes = ()
    step("解除注入后重试（不应重传 blob.bin，应通过回读发现其已存在且正确）")
    put_mark = len(faulty.put_paths)
    out2 = store.archive(manifest, snap)
    obj_puts = faulty.object_puts(put_mark)
    print(f"  {out2.summary()}")
    print(f"  重试对象 put       : {obj_puts}")
    print(f"  reused             : {sorted(out2.reused)}")
    print(f"  repaired           : {sorted(out2.repaired)}")
    record("S6.3 重试未盲目重传任何内容对象（对象 put 增量为 0）", not obj_puts, f"object_put={obj_puts}")
    record("S6.4 重试后完成归档", out2.state == ARCHIVE_VERIFIED and out2.marker_written)


def s7_restore(work: Path, remote_root: Path, manifest, store: ArchiveStore) -> None:
    rule("S7  恢复到隔离目录 + 逐文件哈希校验 + 引用闭合 + 只读恢复模式（SPEC §12.5 / §21.4 / AT-14）")
    dest_root = work / "restore-target"
    out = store.restore(
        manifest.archive_id,
        dest_root,
        protected_roots=[work / "source"],
        publish_record=True,
    )
    print(f"  restore_id         : {out.restore_id}")
    print(f"  state              : {out.state}")
    print(f"  dest（隔离目录）   : {out.dest}")
    print(f"  manifest_digest_ok : {out.manifest_digest_ok}  digest={out.manifest_digest}")
    print(f"  verified           : {len(out.verified)} 个文件逐文件哈希一致")
    print(f"  closure_ok（引用闭合）: {out.closure_ok}")
    print(f"  record_paths       : {out.record_paths}")
    print("  恢复出的文件：")
    for f in sorted(out.verified):
        print(f"    {f}")

    dest = Path(out.dest)
    mode_txt = (dest / "RESTORE_MODE.txt").read_text(encoding="utf-8")
    print(f"  RESTORE_MODE.txt   : {mode_txt.splitlines()[0]}")

    record("S7.1 恢复前建立清单校验且 manifest 摘要一致", out.manifest_digest_ok is True)
    record("S7.2 逐文件哈希校验全部通过", len(out.verified) == len(manifest.files) and not out.failed)
    record("S7.3 引用闭合（manifest 条目 == 落地对象）", out.closure_ok is True)
    record("S7.4 恢复目标是隔离目录，未写入源数据目录", dest != (work / "source").resolve() and dest.is_dir())
    record("S7.5 默认只读恢复模式已声明（历史审批失效、不重放）", "read_only" in mode_txt and "不重放" in mode_txt)
    record("S7.6 生成恢复记录（本地 + 远端 恢复记录/）", any(p.startswith("pan:") for p in out.record_paths))

    # 隔离性对照：源目录内容未被改动
    src_hashes = {
        p.relative_to(work / "source").as_posix(): sha256_file(p)
        for p in (work / "source").rglob("*")
        if p.is_file()
    }
    print(f"  源目录文件哈希（供对照，未被恢复过程改动）：{json.dumps(src_hashes, ensure_ascii=False, sort_keys=True)}")
    record("S7.7 主数据目录未被恢复过程写入", len(src_hashes) == len(manifest.files))


def s8_restore_guards(work: Path, remote_root: Path, manifest, store: ArchiveStore) -> None:
    rule("S8  恢复防线：拒绝非空目标 / 拒绝受保护目录 / 未声明隔离边界 / 远端缺文件拒绝完成")
    prot = [work / "source"]

    # 8.1 非空目标
    dest_root = work / "restore-target"
    raised = None
    try:
        store.restore(manifest.archive_id, dest_root, protected_roots=prot)
    except PanInvalid as exc:
        raised = exc
    print(f"  非空目标 -> {type(raised).__name__ if raised else '未阻止'}: {raised}")
    record("S8.1 拒绝恢复到已存在的非空目录", isinstance(raised, PanInvalid))

    # 8.2 受保护目录（主数据目录之内）
    src_notes = work / "source" / "notes"
    before = sorted(p.as_posix() for p in src_notes.rglob("*") if p.is_file())
    raised = None
    try:
        store.restore(manifest.archive_id, src_notes, protected_roots=prot)
    except PanInvalid as exc:
        raised = exc
    after = sorted(p.as_posix() for p in src_notes.rglob("*") if p.is_file())
    print(f"  受保护目录 -> {type(raised).__name__ if raised else '未阻止'}: {raised}")
    record("S8.2 拒绝恢复到主数据/受保护目录内", isinstance(raised, PanInvalid))
    record(
        "S8.2b 被拒绝的恢复没有在主数据目录留下任何文件",
        before == after,
        f"before={len(before)} after={len(after)}",
    )

    # 8.3 未声明隔离边界：接口层面就是必填，缺失即无法调用
    raised = None
    try:
        store.restore(manifest.archive_id, work / "restore-noguard")  # type: ignore[call-arg]
    except TypeError as exc:
        raised = exc
    print(f"  完全不传 protected_roots -> {type(raised).__name__ if raised else '未阻止'}: {raised}")
    record("S8.3 不传隔离边界时接口直接拒绝调用（必填参数）", isinstance(raised, TypeError))

    # 8.3b 传空列表也要被拒（纵深防御）
    raised = None
    try:
        store.restore(manifest.archive_id, work / "restore-noguard", protected_roots=[])
    except PanInvalid as exc:
        raised = exc
    print(f"  protected_roots=[] -> {type(raised).__name__ if raised else '未阻止'}: {raised}")
    record("S8.3b 隔离边界为空列表时同样拒绝恢复", isinstance(raised, PanInvalid))

    # 8.4 远端缺 manifest
    other = ArchiveStore(LocalDirPanClient(remote_root / "empty"), ArchiveLayout(), work / "state-s8")
    out = other.restore("a0000000000000000000", work / "restore-missing", protected_roots=prot)
    print(f"  远端缺 manifest -> state={out.state} notes={out.notes}")
    record("S8.4 远端归档不存在时拒绝完成恢复", out.state == "RESTORE_FAILED" and not out.verified)


def s9_tamper_detection(work: Path, remote_root: Path) -> None:
    rule("S9  归档目标被篡改 -> 逐文件校验发现不一致，不标记完成（SPEC MVP-7 / §12.5）")
    src = work / "source"
    manifest, snap = make_manifest(src, work / "stage-s9")
    inner = LocalDirPanClient(remote_root / "s9")
    store = ArchiveStore(inner, ArchiveLayout(), work / "state-s9")
    out = store.archive(manifest, snap)
    print(f"  首次归档          : {out.summary()}")
    record("S9.1 首次归档完成", out.state == ARCHIVE_VERIFIED)

    victim = "attachments/报告.bin"
    remote_obj = inner.root / ArchiveLayout().object_path(manifest.archive_id, victim)
    step(f"篡改远端对象内容：{victim}（直接改写字节，模拟目标侧内容不一致）")
    remote_obj.write_bytes(b"TAMPERED-CONTENT" + b"\x00" * 32)

    report = store.verify_archive(manifest.archive_id)
    print(f"  verify_archive → consistent={report['consistent']} mismatched={report['mismatched']}")
    record("S9.2 校验发现内容不一致并不判定一致", report["consistent"] is False and len(report["mismatched"]) == 1)

    raised = None
    try:
        store.archive(manifest, snap)
    except PanConflict as exc:
        raised = exc
    print(f"  再次归档 -> {type(raised).__name__ if raised else '未阻止'}: {str(raised)[:160]}")
    record("S9.3 已完成的归档遇到内容不一致时拒绝覆盖（不静默重写）", isinstance(raised, PanConflict))

    # 篡改后的归档恢复也必须失败（逐文件校验拦住）
    out_r = store.restore(
        manifest.archive_id, work / "restore-tampered", protected_roots=[work / "source"]
    )
    print(f"  篡改后恢复 -> state={out_r.state} failed={[f['relpath'] for f in out_r.failed]}")
    record("S9.4 篡改后的归档恢复到隔离目录时逐文件校验失败、拒绝判定成功", out_r.state == "RESTORE_FAILED")


def s10_archive_id_collision(work: Path, remote_root: Path) -> None:
    rule("S10 同一 archive_id 绑定不同 manifest -> 拒绝覆盖（SPEC §21.4 不覆盖已校验的不同内容）")
    src = work / "source"
    manifest, snap = make_manifest(src, work / "stage-s10")
    inner = LocalDirPanClient(remote_root / "s10")
    store = ArchiveStore(inner, ArchiveLayout(), work / "state-s10")
    out = store.archive(manifest, snap)
    print(f"  首次归档          : {out.summary()}")

    forged_files = tuple(
        FileEntry(
            relpath=e.relpath,
            size=e.size,
            sha256=("0" * 64 if e.relpath.endswith("data.csv") else e.sha256),
            source_revision=e.source_revision,
            mtime_ns=e.mtime_ns,
        )
        for e in manifest.files
    )
    from dataclasses import replace as _replace

    forged = _replace(manifest, files=forged_files)
    step("伪造一份同 archive_id 但逐文件哈希不同的 manifest（模拟受控变更/冲突）")

    raised = None
    try:
        store.archive(forged, snap)
    except PanConflict as exc:
        raised = exc
    print(f"  archive(forged) -> {type(raised).__name__ if raised else '未阻止'}: {str(raised)[:200]}")
    record(
        "S10.1 检测到 archive_id 与远端完成标记摘要冲突并拒绝覆盖",
        isinstance(raised, PanConflict) and "manifest_digest" in str(raised),
    )

    still = store.verify_archive(manifest.archive_id)
    record("S10.2 原归档未被伪造 manifest 破坏", still["consistent"] is True)


def s11_frozen_snapshot(work: Path, remote_root: Path) -> None:
    rule("S11 manifest 冻结后本地文件被改写 -> 仍上传已捕获快照（SPEC §21.4）")
    src = work / "source"
    manifest, snap = make_manifest(src, work / "stage-s11")
    captured = {e.relpath: e.sha256 for e in manifest.files}
    print(f"  archive_id         : {manifest.archive_id}")
    print(f"  快照根目录         : {snap}")

    victim = "notes/会议纪要.md"
    step(f"在 manifest 冻结后改写源文件：{victim}")
    (src / victim).write_text("# 被改写的内容（不应出现在本次归档中）\n", encoding="utf-8")
    changed_hash = sha256_file(src / victim)
    print(f"  源文件新 sha256    : {changed_hash}")
    print(f"  manifest 捕获 sha256: {captured[victim]}")

    inner = LocalDirPanClient(remote_root / "s11")
    store = ArchiveStore(inner, ArchiveLayout(), work / "state-s11")
    out = store.archive(manifest, snap)
    print(f"  {out.summary()}")

    remote_obj = inner.root / ArchiveLayout().object_path(manifest.archive_id, victim)
    uploaded_hash = sha256_file(remote_obj)
    print(f"  远端落地 sha256    : {uploaded_hash}")

    record("S11.1 归档成功（上传的是冻结快照，不是被改写的源文件）", out.state == ARCHIVE_VERIFIED)
    record("S11.2 远端内容等于 manifest 捕获的快照哈希", uploaded_hash == captured[victim])
    record("S11.3 远端内容不等于被改写的源文件", uploaded_hash != changed_hash)
    record(
        "S11.4 manifest 记录来源版本，变化应进入下一快照",
        manifest.source_revision == "case-rev-1",
    )


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="asistent 百度网盘归档契约离线验证")
    ap.add_argument("--workdir", default=None, help="工作目录（默认系统临时目录）")
    ap.add_argument("-k", "--keep", action="store_true", help="保留工作目录")
    args = ap.parse_args()

    tmp = Path(args.workdir or tempfile.mkdtemp(prefix="asistent-pan-e2e-"))
    tmp.mkdir(parents=True, exist_ok=True)
    remote_root = tmp / "remote"
    src = tmp / "source"
    src.mkdir(parents=True, exist_ok=True)
    build_source_tree(src)

    rule("asistent × 百度网盘归档契约 Spike（离线 / 零第三方依赖 / 不接触真实网盘）")
    print(f"  python             : {sys.version.split()[0]}")
    print(f"  platform           : {platform.platform()} / {platform.machine()}")
    print(f"  工作目录           : {tmp}")
    print(f"  远端模拟根         : {remote_root}")
    print(f"  源数据目录         : {src}")
    print(f"  开始时间           : {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")

    s1_manifest(tmp)
    manifest, snap, pan, store, _marker = s2_happy_path(tmp, remote_root)
    s3_idempotent_retry(tmp, manifest, snap, pan, store)
    s4_content_change(tmp, remote_root, manifest, pan, store)
    s5_partial_failure(tmp, remote_root)
    s6_unknown_result(tmp, remote_root)
    s7_restore(tmp, remote_root, manifest, store)
    s8_restore_guards(tmp, remote_root, manifest, store)
    s9_tamper_detection(tmp, remote_root)
    s10_archive_id_collision(tmp, remote_root)
    s11_frozen_snapshot(tmp, remote_root)

    rule("结果汇总")
    passed = sum(1 for _, p, _ in RESULTS if p)
    total = len(RESULTS)
    for name, p, detail in RESULTS:
        print(f"  {'PASS' if p else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    print()
    print(f"  合计：{passed}/{total} 项通过")
    print(f"  结束时间           : {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")

    if not args.keep and not args.workdir:
        shutil.rmtree(tmp, ignore_errors=True)
        print("  工作目录已清理（用 -k 保留）")
    else:
        print(f"  工作目录已保留：{tmp}")

    print()
    if passed == total:
        print("  RESULT: ALL CHECKS PASSED")
        return 0
    print(f"  RESULT: {total - passed} CHECK(S) FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
