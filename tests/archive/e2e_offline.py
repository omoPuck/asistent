#!/usr/bin/env python3
"""MVP-7 离线端到端演练（假 bdpan CLI + 真实通道/引擎/恢复，**不接触真实网盘**）。

跑一遍完整回路并逐项打印原始证据，便于写进 `docs/mvp/mvp-7.md`：

    归档 → 只读校验 → 篡改检测 → 隔离恢复 → 索引重建 → 幂等重跑 → 部分失败重试

用法（在仓库根目录）：

    python3 tests/archive/e2e_offline.py            # 用 tests/archive/fake_bdpan.py

退出码：0 = 全部检查通过；1 = 有检查失败（失败项会明确打印）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from asistent.archive import (  # noqa: E402
    ArchiveEngine,
    ArchiveLayout,
    build_archive_manifest,
    capture_source_tree,
    freeze_snapshot,
    restore_archive,
)
from asistent.contracts.errors import SecretDetected  # noqa: E402
from asistent.pan import BdpanCliPanClient  # noqa: E402
from tests.archive.support import FakeBdpanEnv  # noqa: E402

CHECKS: List[Dict[str, Any]] = []


def check(name: str, condition: bool, detail: Any = "") -> bool:
    CHECKS.append({"name": name, "ok": bool(condition), "detail": detail})
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" :: {detail}" if detail != "" else ""), flush=True)
    return bool(condition)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def write(path: str, content: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass

    env = FakeBdpanEnv()
    root = tempfile.mkdtemp(prefix="asistent-e2e-")
    source = os.path.join(root, "source")
    isolate = os.path.join(root, "isolate")
    protected = os.path.join(root, "main-data")
    stage = os.path.join(root, "stage")
    state_dir = os.path.join(root, "state")
    os.makedirs(protected, exist_ok=True)
    try:
        section("0. 准备本地源目录（含子目录、中文内容、一个凭据类文件）")
        write(os.path.join(source, "notes", "a.md"), "归档测试资料 A：中文内容\n")
        write(os.path.join(source, "notes", "b.md"), "归档测试资料 B\n")
        write(os.path.join(source, "top.txt"), "顶层文件\n")
        write(os.path.join(source, "credentials"), "password = should-never-be-archived\n")
        print(f"source={source}")
        print(f"fake_bdpan={env.fake_py}")
        print(f"remote_root={env.remote}")

        captured, excluded, scan = capture_source_tree(source)
        check(
            "凭据类文件被排除、快照秘密扫描干净",
            sorted(item.relative_path for item in captured) == ["notes/a.md", "notes/b.md", "top.txt"]
            and "credentials" in excluded
            and scan.clean,
            f"excluded={sorted(excluded)} scan_clean={scan.clean}",
        )

        staged = freeze_snapshot(captured, stage)
        manifest = build_archive_manifest(
            scope="snapshot", scope_ref="e2e-offline", files=staged, secret_scan=scan
        )
        archive_id = manifest["archive_id"]
        print(f"archive_id={archive_id}")
        print(f"manifest_digest={manifest['manifest_digest']}")
        print(f"content_origin_summary={json.dumps(manifest['content_origin_summary'], ensure_ascii=False)}")

        pan = BdpanCliPanClient(binary=env.binary, root_prefix="asistent", timeout=30)
        pan.unlock_writes("appr_e2e")
        layout = ArchiveLayout()
        engine = ArchiveEngine(pan, layout=layout, state_dir=state_dir, require_authorized=True)

        section("1. 归档：上传 → 逐文件哈希回读校验 → 写完成标记")
        first = engine.archive(manifest, staged)
        print(first.summary())
        print(f"claim={json.dumps(first.integrity_claim, ensure_ascii=False)}")
        check("归档到达 VERIFIED", first.state == "VERIFIED", first.state)
        check(
            "每个对象都做了回读（下载次数 >= 对象数）",
            env.download_count() >= 3,
            f"downloads={env.download_count()}",
        )
        check("完成标记已写且声明逐文件 sha256 校验", first.marker_written and not first.failed)
        check(
            "三件内容对象 + manifest + 完成标记 = 5 次上传",
            first.transfer_count == 5 and len(first.uploaded) == 3,
            f"transfers={first.transfer_count} uploaded={len(first.uploaded)}",
        )

        section("2. 只读校验（不做任何写入）")
        env.reset_logs()
        report = engine.verify_archive(archive_id)
        print(f"consistent={report['consistent']} verified={len(report['verified'])} missing={report['missing']}")
        check("只读校验一致", report["consistent"] and len(report["verified"]) == 3)
        check(
            "只读校验过程零上传/零删除",
            env.calls_of("upload") == [] and env.calls_of("rm") == [],
            f"uploads={len(env.calls_of('upload'))} rm={len(env.calls_of('rm'))}",
        )

        section("3. 篡改检测：远端对象被改写")
        archive_dir = layout.archive_dir(archive_id)
        target = f"asistent/{archive_dir}/objects/top.txt"
        env.write_remote(target, "被篡改的内容\n")
        tampered = engine.verify_archive(archive_id)
        print(f"mismatched={[item['relative_path'] for item in tampered['mismatched']]}")
        check(
            "同尺寸不同内容仍被哈希发现",
            [item["relative_path"] for item in tampered["mismatched"]] == ["top.txt"],
        )
        check("篡改后状态不再一致", not tampered["consistent"])

        restore_bad = restore_archive(
            pan,
            archive_id=archive_id,
            isolate_dir=os.path.join(isolate, "blocked"),
            protected_roots=[protected],
            layout=layout,
            state_dir=state_dir,
        )
        print(restore_bad.summary())
        check("恢复遇篡改 → CONFLICT", restore_bad.state == "CONFLICT", restore_bad.state)
        check(
            "不一致的字节不留在隔离目录",
            not os.path.isfile(os.path.join(isolate, "blocked", "top.txt")),
        )

        section("4. 修复远端后恢复：下载 → 逐文件哈希 → 索引重建（无写入/无重放）")
        env.write_remote(target, open(os.path.join(stage, "top.txt"), encoding="utf-8").read())
        index_path = os.path.join(root, "index", "search.jsonl")
        rebuilt: Dict[str, Any] = {}

        def reindex(paths: List[str]) -> Dict[str, Any]:
            os.makedirs(os.path.dirname(index_path), exist_ok=True)
            with open(index_path, "w", encoding="utf-8") as fh:
                for path in sorted(paths):
                    fh.write(json.dumps({"path": os.path.relpath(path, isolate)}, ensure_ascii=False) + "\n")
            rebuilt["paths"] = sorted(paths)
            return {"indexed": len(paths), "index": index_path}

        env.reset_logs()
        restored = restore_archive(
            pan,
            archive_id=archive_id,
            isolate_dir=os.path.join(isolate, "restore-1"),
            protected_roots=[protected],
            layout=layout,
            state_dir=state_dir,
            reindex=reindex,
        )
        print(restored.summary())
        print(f"verified={sorted(restored.verified)}")
        print(f"reindex={json.dumps(restored.reindex_detail, ensure_ascii=False)}")
        print(f"record={restored.record_paths}")
        check("恢复到达 VERIFIED", restored.state == "VERIFIED", restored.state)
        check("引用闭合 + manifest 摘要复算通过", restored.closure_ok and restored.manifest_digest_ok)
        check("索引重建完成", bool(restored.reindex_done and os.path.isfile(index_path)))
        check("恢复期间零写入/零删除", env.calls_of("upload") == [] and env.calls_of("rm") == [])
        check("恢复是只读模式且不重放外部动作", restored.read_only_mode and not restored.replay_external_actions)
        check("主数据目录未被触碰", os.listdir(protected) == [])
        restored_files = [
            os.path.join(restored.isolate_dir, name) for name in ("notes/a.md", "notes/b.md", "top.txt")
        ]
        check(
            "恢复出的文件内容与源一致",
            all(os.path.isfile(path) for path in restored_files)
            and open(os.path.join(restored.isolate_dir, "top.txt"), encoding="utf-8").read() == "顶层文件\n",
            sorted(os.path.relpath(path, restored.isolate_dir) for path in restored_files),
        )

        section("5. 幂等：同一 archive_id 重跑")
        env.reset_logs()
        again = engine.archive(manifest, staged)
        print(again.summary())
        check(
            "重跑零上传、全部复用",
            again.state == "VERIFIED" and again.transfer_count == 0 and len(again.reused) == 3,
            f"transfers={again.transfer_count} reused={len(again.reused)}",
        )
        check("复用对象仍然重新回读校验", env.download_count() >= 3, f"downloads={env.download_count()}")

        section("6. 部分失败：只补传失败对象（计数断言）")
        write(os.path.join(source, "notes", "c.md"), "第三个文件\n")
        captured2, _excluded2, scan2 = capture_source_tree(source)
        staged2 = freeze_snapshot(captured2, os.path.join(root, "stage-2"))
        manifest2 = build_archive_manifest(
            scope="snapshot", scope_ref="e2e-offline-2", files=staged2, secret_scan=scan2
        )
        env.set_spec(
            {
                "faults": {
                    "objects/notes/a.md": {"upload": "not_found"},
                    "objects/notes/c.md": {"upload": "not_found"},
                }
            }
        )
        partial = engine.archive(manifest2, staged2)
        print(partial.summary())
        attempted_first = [
            item for item in env.calls_of("upload", successful_only=False) if "/objects/" in item["target"]
        ]
        check(
            "部分失败 → PARTIAL，且失败对象逐条可见",
            partial.state == "PARTIAL"
            and sorted(item["relative_path"] for item in partial.failed) == ["notes/a.md", "notes/c.md"],
            [item["relative_path"] for item in partial.failed],
        )
        check("部分失败不写完成标记", not partial.marker_written)
        check("第一次尝试确实尝试了全部 4 个对象", len(attempted_first) == 4, f"attempted={len(attempted_first)}")

        env.set_spec({})
        env.reset_logs()
        retried = engine.archive(manifest2, staged2)
        attempted_second = [
            item for item in env.calls_of("upload", successful_only=False) if "/objects/" in item["target"]
        ]
        print(retried.summary())
        print(f"second_attempt_object_uploads={[os.path.basename(item['target']) for item in attempted_second]}")
        check("重试后 VERIFIED", retried.state == "VERIFIED", retried.state)
        check(
            "第二次只补传失败的 2 个对象（不是全部 4 个）",
            sorted(os.path.basename(item["target"]) for item in attempted_second) == ["a.md", "c.md"],
            len(attempted_second),
        )
        check("未失败对象全部复用", len(retried.reused) == 2, retried.reused)

        section("7. 秘密拦截：manifest 构建阶段就拦住凭据")
        write(os.path.join(source, "notes", "leak.txt"), "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n")
        captured3, _excluded3, scan3 = capture_source_tree(source)
        staged3 = freeze_snapshot(captured3, os.path.join(root, "stage-3"))
        blocked = False
        try:
            build_archive_manifest(scope="snapshot", scope_ref="e2e-leak", files=staged3, secret_scan=scan3)
        except SecretDetected:
            blocked = True
        check("含凭据的快照被拒绝归档", blocked and not scan3.clean, [hit["path"] for hit in scan3.hits])

        section("汇总")
        passed = sum(1 for item in CHECKS if item["ok"])
        total = len(CHECKS)
        print(f"checks={passed}/{total}")
        if passed != total:
            for item in CHECKS:
                if not item["ok"]:
                    print(f"FAILED: {item['name']} :: {item['detail']}")
        return 0 if passed == total else 1
    finally:
        env.cleanup()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
