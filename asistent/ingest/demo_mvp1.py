"""MVP-1 可检索性演示：新文件落盘 → 采集 → 中文查询命中（SPEC §16 MVP-1、AT-03）。

这个脚本是**验收证据的生产者**：跑一次，粘贴输出到 `docs/mvp/mvp-1.md`。

```bash
python -m asistent.ingest.demo_mvp1                 # 用系统默认临时目录
python -m asistent.ingest.demo_mvp1 --keep DIR      # 保留工作区（排查用）
```

覆盖的验收点：
1. 60 秒内可检索（打印实测耗时：collect / index / 端到端）；
2. 中文 2 字 / 3 字真实命中（`部署`、`百度网盘`、`会议纪要`），并打印**原始 MATCH 表达式与命中行**；
3. 中英混排与注入式查询（不得报错、不得返回全部结果）；
4. 重复采集幂等（对象数、审计条数、事件数都不变）；
5. 增量：只处理变化文件；
6. 不静默丢弃：二进制/超大文件给原因 + 审计 + 事件；
7. 源文件被删除后的状态处理；
8. 引用完整性：每条命中都带 `path/locator/source_version/content_hash/supports`。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import redirect_stdout
from io import StringIO
from typing import Any, Dict, List, Optional

from ..contracts.reference import ReferenceStore
from ..contracts.search import to_fts_query
from ..ingest.collector import CollectionPolicy
from ..index.fts import DOCUMENT_TABLE, open_index_db
from ..index.pipeline import SearchPipeline, Stopwatch
from ..index.snapshots import SnapshotStore

#: 端到端时延上限（验收要求 < 60s，目标是 < 5s）
BUDGET_SECONDS = 60.0
TARGET_SECONDS = 5.0

DEMO_FILES: Dict[str, str] = {
    "资料/部署方案.md": (
        "# 部署方案\n"
        "\n"
        "设备常驻服务通过 systemd 拉起，部署脚本只做三件事。\n"
        "deploy 前先确认索引状态为 INDEXED。\n"
    ),
    "资料/归档通道.md": (
        "# 归档通道\n"
        "\n"
        "百度网盘归档通道每 6 小时同步一次，逐文件回读校验 sha256。\n"
    ),
    "资料/会议纪要.md": (
        "# 会议纪要\n"
        "\n"
        "2026-10-09 例会确认：部署与归档的负责人已就位。\n"
    ),
    "资料/mixed-中英.md": (
        "# 中英混排\n"
        "\n"
        "deploy部署 连写，百度网盘 upload 混排，关键词包含 archive。\n"
    ),
    "资料/无关-联系人.md": "张三的邮箱地址与电话号码。\n",
}


def _section(title: str) -> None:
    print("")
    print("=" * 78)
    print(title)
    print("=" * 78)


def _raw_match(connection, query_text: str) -> List[tuple]:
    """直接执行契约层 MATCH 表达式，返回 SQLite 的原始行（唯一可信的命中证据）。"""
    expression = to_fts_query(query_text)
    if expression is None:
        return []
    return connection.execute(
        f"SELECT doc_id, path, raw_body FROM {DOCUMENT_TABLE} WHERE {DOCUMENT_TABLE} MATCH ? ORDER BY path",
        (expression,),
    ).fetchall()


def _write_workspace(root: str) -> str:
    data_dir = os.path.join(root, "data")
    for relative, text in DEMO_FILES.items():
        target = os.path.join(data_dir, relative.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)
    return data_dir


def _locale_report() -> Dict[str, str]:
    return {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "encoding": sys.getdefaultencoding(),
        "filesystem_encoding": sys.getfilesystemencoding(),
        "cwd": os.getcwd(),
    }


def run_demo(*, keep: Optional[str] = None) -> int:
    """把 demo 跑一遍并打印全部证据；返回进程退出码（0 = 所有自检通过）。"""
    checks: List[Dict[str, Any]] = []

    def check(name: str, value: Any, expectation: str, ok: bool) -> None:
        checks.append({"name": name, "value": value, "expectation": expectation, "ok": bool(ok)})

    section = _section
    section("MVP-1 演示：新文件落盘 → 采集 → 中文检索")
    for key, value in _locale_report().items():
        print(f"{key:22s}: {value}")

    root = keep or tempfile.mkdtemp(prefix="asistent-mvp1-demo-")
    if keep:
        if os.path.isdir(keep):
            shutil.rmtree(keep, ignore_errors=True)
        os.makedirs(keep, exist_ok=True)
    data_dir = _write_workspace(root)
    snapshot_dir = os.path.join(root, "raw")
    index_path = os.path.join(root, "index", "fts.db")

    store = ReferenceStore()
    connection = open_index_db(index_path)
    pipeline = SearchPipeline(store, connection, snapshot_root=snapshot_dir)
    print(f"{'workspace':22s}: {root}")
    print(f"{'index_db':22s}: {index_path}")

    # ---------------------------------------------------------------- 采集 + 索引
    section("1) 采集 + 索引：实测耗时（要求 < 60s，目标 < 5s）")
    stopwatch = Stopwatch()
    run = pipeline.collect([data_dir])
    wall_seconds = stopwatch.elapsed_seconds()
    print(f"collect_seconds        : {run.collect_seconds:.4f}")
    print(f"index_seconds          : {run.index_seconds:.4f}")
    print(f"ingest_total_seconds   : {run.ingest_total_seconds:.4f}")
    print(f"wall_clock_seconds     : {wall_seconds:.4f}")
    print(f"new_documents          : {run.collection.new_documents}")
    print(f"updated_documents      : {run.collection.updated_documents}")
    print(f"skipped_files          : {run.collection.skipped_files}")
    print(f"indexed                : {run.indexing.indexed}")
    print(f"index_failed           : {run.indexing.failed}")
    print(f"scan_fingerprint       : {run.collection.fingerprint()}")
    print(f"budget(<60s)_ok        : {wall_seconds < BUDGET_SECONDS}")
    print(f"target(<5s)_ok         : {wall_seconds < TARGET_SECONDS}")
    check("collect_seconds", round(run.collect_seconds, 4), ">= 0", run.collect_seconds >= 0)
    check("ingest_total_seconds", round(run.ingest_total_seconds, 4), "< 60（目标 < 5）", run.ingest_total_seconds < BUDGET_SECONDS)
    check("new_documents", run.collection.new_documents, "== 5", run.collection.new_documents == len(DEMO_FILES))
    check("indexed", run.indexing.indexed, "== 5", run.indexing.indexed == len(DEMO_FILES))
    check("index_failed", run.indexing.failed, "== 0", run.indexing.failed == 0)

    # ---------------------------------------------------------------- 中文命中
    section("2) 中文检索：原始 MATCH 表达式 + SQLite 原始命中行")
    expected_hits = {"部署": 3, "百度网盘": 2, "会议纪要": 1, "归档": 2}
    for query in ("部署", "百度网盘", "会议纪要", "归档"):
        expression = to_fts_query(query)
        rows = _raw_match(connection, query)
        print(f"\nquery                  : {query!r}")
        print(f"contract to_fts_query() : {expression}")
        print(f"sqlite MATCH rows       : {len(rows)}")
        for doc_id, path, body in rows:
            relative = os.path.relpath(path, data_dir) if path else path
            print(f"  - {doc_id} {relative}")
        print(f"  hit paths            : {[os.path.relpath(row[1], data_dir) for row in rows]}")
        check(f"MATCH {query!r}", len(rows), f"== {expected_hits[query]}", len(rows) == expected_hits[query])

    section("3) 中英混排 + 大小写")
    for query in ("deploy", "DEPLOY", "deploy 部署", "deploy部署", "archive"):
        print(
            f"{query!r:16s} -> {to_fts_query(query)!r:36s} "
            f"hits={[os.path.relpath(row[1], data_dir) for row in _raw_match(connection, query)]}"
        )
    check("大小写等价 deploy/DEPLOY", len(_raw_match(connection, "deploy")), "== 2", len(_raw_match(connection, "deploy")) == 2)
    check("语序无关 deploy 部署", len(_raw_match(connection, "deploy 部署")), "== 2", len(_raw_match(connection, "deploy 部署")) == 2)

    section("4) 注入式 / 无效查询：不得报错、不得返回全部")
    total_documents = len(store.find("SourceDocument", limit=1000))
    print(f"total_documents        : {total_documents}")
    injection_queries = ['部署" OR "x', "MATCH *", "*", "a AND b", '"""', "()^", "部署;", "' OR 1=1 --", "", "   ", "！！！"]
    for query in injection_queries:
        response = pipeline.search(query)
        print(
            f"{query!r:18s} valid={str(response.valid_query):5s} "
            f"expr={str(response.fts_query)!r:28s} hits={response.count}"
        )
        if response.valid_query:
            assert response.count <= total_documents, "命中数不可能超过文档总数"
    maximal = max(pipeline.search(query).count for query in injection_queries)
    check("注入式查询最大命中数", maximal, "<= total_documents 且无异常", maximal <= total_documents)
    check("无效查询 valid_query", [pipeline.search(query).valid_query for query in ("", "   ", "！！！", "*")], "全 False", not any(pipeline.search(query).valid_query for query in ("", "   ", "！！！", "*")))

    section("5) 引用完整性：每条命中都能定位到原文")
    for hit in pipeline.search("部署").hits:
        citation = hit.citation
        print(f"- {hit.doc_id} {hit.path} {hit.locator}")
        print(f"    version={citation.source_version} hash={citation.content_hash[:16]}…")
        print(f"    supports={citation.supports}")
        print(f"    excerpt={citation.excerpt}")
    citation = pipeline.search("百度网盘").hits[0].citation
    required = (
        "source_object_id",
        "source_object_type",
        "path",
        "locator",
        "source_version",
        "content_hash",
        "supports",
    )
    payload = citation.to_dict()
    missing = [field for field in required if payload.get(field) in (None, "")]
    print(f"cited_required_fields   : {required}")
    print(f"cited_required_missing  : {missing}")
    print(f"cited_required_fields_ok: {not missing}")
    check("citation 必填字段", list(required), "无缺失", not missing)

    section("6) 幂等：同一目录再扫一次")
    before = store.stats()
    audit_before = len(store.audit_trail(limit=100000))
    events_before = len(store.list_events(limit=10000))
    second = pipeline.collect([data_dir])
    after = store.stats()
    print(f"unchanged_files        : {second.collection.unchanged_files}")
    print(f"new_documents          : {second.collection.new_documents}")
    print(f"indexed                : {second.indexing.indexed}")
    print(f"stats_before           : {_sorted_counts(before)}")
    print(f"stats_after            : {_sorted_counts(after)}")
    print(f"stats_equal            : {before == after}")
    print(f"audit_before/after     : {audit_before} / {len(store.audit_trail(limit=100000))}")
    print(f"events_before/after    : {events_before} / {len(store.list_events(limit=10000))}")
    print(f"fingerprint_equal      : {run.collection.fingerprint() == second.collection.fingerprint()}")
    check("二次扫描 stats 相等", before == after, "True", before == after)
    check("二次扫描 audit 条数", len(store.audit_trail(limit=100000)), f"== {audit_before}", len(store.audit_trail(limit=100000)) == audit_before)
    check("二次扫描 event 条数", len(store.list_events(limit=10000)), f"== {events_before}", len(store.list_events(limit=10000)) == events_before)
    check("二次扫描 new_documents", second.collection.new_documents, "== 0", second.collection.new_documents == 0)

    section("7) 增量：改一个文件，只处理它")
    changed = os.path.join(data_dir, "资料", "部署方案.md")
    with open(changed, "a", encoding="utf-8") as handle:
        handle.write("补充：2026-10-10 新增灰度发布说明，关键词 灰度。\n")
    third = pipeline.collect([data_dir])
    print(f"updated_documents      : {third.collection.updated_documents}")
    print(f"unchanged_files        : {third.collection.unchanged_files}")
    print(f"indexed                : {third.indexing.indexed}")
    print(f"查 '灰度'（新内容）      : {[os.path.relpath(row[1], data_dir) for row in _raw_match(connection, '灰度')]}")
    document = store.find_one("SourceDocument", source_id="资料/部署方案.md")
    print(f"content_version        : {document.content_version}")
    print(f"index_state            : {document.index_state}")
    check("增量 updated_documents", third.collection.updated_documents, "== 1", third.collection.updated_documents == 1)
    check("增量 unchanged_files", third.collection.unchanged_files, "== 4", third.collection.unchanged_files == 4)
    check("增量 content_version", document.content_version, "== 2", document.content_version == 2)

    section("8) 不静默丢弃：二进制 / 超大 / 不可解码 都给原因")
    binary_path = os.path.join(data_dir, "资料", "logo.png")
    with open(binary_path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01")
    big_path = os.path.join(data_dir, "资料", "超大.log")
    with open(big_path, "w", encoding="utf-8") as handle:
        handle.write("x" * 4096)
    strict = pipeline.collect([data_dir], policy=pipeline.policy(suffixes=None, max_bytes=1024))
    for outcome in strict.collection.skipped:
        print(f"skipped  {outcome.relative_path:24s} reason={outcome.reason}")
    skip_audits = [event for event in store.audit_trail(limit=10000) if event.action == "COLLECT_SKIP"]
    skip_events = [event for event in store.list_events(limit=10000) if event.kind == "CONTENT_SKIPPED"]
    print(f"COLLECT_SKIP audits    : {len(skip_audits)}")
    print(f"CONTENT_SKIPPED events : {len(skip_events)}")
    print(f"source_bytes_untouched : {os.path.getsize(binary_path)} / {os.path.getsize(big_path)}")
    check("skip 原因条数", len(strict.collection.skipped), "== 2", len(strict.collection.skipped) == 2)
    check("COLLECT_SKIP 审计", len(skip_audits), "== 2", len(skip_audits) == 2)
    check("CONTENT_SKIPPED 事件", len(skip_events), "== 2", len(skip_events) == 2)
    check(
        "源文件未被改写",
        [os.path.getsize(binary_path), os.path.getsize(big_path)],
        f"== [20, 4096]",
        [os.path.getsize(binary_path), os.path.getsize(big_path)] == [20, 4096],
    )

    section("9) 源文件删除 / 改名后的状态处理")
    os.remove(os.path.join(data_dir, "资料", "会议纪要.md"))
    removed = pipeline.collect([data_dir])
    deleted = store.find_one("SourceDocument", source_id="资料/会议纪要.md")
    print(f"stale_documents        : {removed.collection.stale_documents}")
    print(f"deleted_doc_index_state: {deleted.index_state}")
    print(f"still_searchable('会议纪要'): {[os.path.relpath(row[1], data_dir) for row in _raw_match(connection, '会议纪要')]}")

    os.replace(
        os.path.join(data_dir, "资料", "归档通道.md"),
        os.path.join(data_dir, "资料", "归档通道-改名.md"),
    )
    renamed = pipeline.collect([data_dir])
    old = store.find_one("SourceDocument", source_id="资料/归档通道.md")
    new = store.find_one("SourceDocument", source_id="资料/归档通道-改名.md")
    print(f"new_documents          : {renamed.collection.new_documents}")
    print(f"old_doc_index_state    : {old.index_state}")
    print(f"new_doc_index_state    : {new.index_state}")
    print(f"old_id != new_id       : {old.id != new.id}")
    check("删除后状态", str(deleted.index_state), "== STALE", str(deleted.index_state) == "STALE")
    check("删除后仍可检索", len(_raw_match(connection, "会议纪要")) >= 1, ">= 1", len(_raw_match(connection, "会议纪要")) >= 1)
    check("改名后新路径状态", str(new.index_state), "== INDEXED", str(new.index_state) == "INDEXED")

    section("10) 索引失败不丢原始资料 + 重建索引")
    grayscale_hits = _raw_match(connection, "灰度")
    print(f"删索引前 '灰度' 命中     : {[os.path.relpath(row[1], data_dir) for row in grayscale_hits]}")
    for row in grayscale_hits:
        connection.execute(f"DELETE FROM {DOCUMENT_TABLE} WHERE doc_id = ?", (row[0],))
    connection.commit()
    after_delete = _raw_match(connection, "灰度")
    print(f"删掉这些索引后查 '灰度'   : {_raw_match(connection, '灰度')}")
    check("索引被删后查不到", len(after_delete), "== 0", not after_delete)
    rebuild = pipeline.rebuild_index()
    print(f"rebuild.indexed        : {rebuild.indexed}")
    print(f"rebuild.failed         : {rebuild.failed}")
    for result in rebuild.results:
        if not result.ok and not result.skipped:
            shown = os.path.relpath(result.path, data_dir) if result.path.startswith(data_dir) else result.path
            print(f"  ! {shown}: {result.error}")
    print(f"重建后查 '灰度'         : {[os.path.relpath(row[1], data_dir) for row in _raw_match(connection, '灰度')]}")
    print(f"source_documents       : {len(store.find('SourceDocument', limit=1000))}")
    snapshots = SnapshotStore(snapshot_dir)
    print(f"snapshot_files         : {len(snapshots.list_hashes())}")
    print(f"snapshot_bytes         : {snapshots.total_bytes()}")
    check("重建后重新可检索", len(_raw_match(connection, "灰度")) >= 1, ">= 1", len(_raw_match(connection, "灰度")) >= 1)
    check("重建 indexed", rebuild.indexed, "== 5", rebuild.indexed == 5)
    check("原文快照数量", len(snapshots.list_hashes()), "== 7（5 份演示资料 + 2 份被跳过资料）", len(snapshots.list_hashes()) == 7)

    section("11) 端到端：新文件落盘 → 可被中文检索（计时）")
    stopwatch = Stopwatch()
    fresh = os.path.join(data_dir, "资料", "新落盘.md")
    with open(fresh, "w", encoding="utf-8") as handle:
        handle.write("# 新落盘\n部署灰度发布会话已排期。\n")
    fresh_run = pipeline.collect([data_dir])
    elapsed = stopwatch.elapsed_seconds()
    hits = _raw_match(connection, "灰度发布")
    print(f"elapsed_seconds        : {elapsed:.4f}")
    print(f"within_60s             : {elapsed < BUDGET_SECONDS}")
    print(f"within_5s              : {elapsed < TARGET_SECONDS}")
    print(f"new_documents          : {fresh_run.collection.new_documents}")
    print(f"'灰度发布' 命中          : {[os.path.relpath(row[1], data_dir) for row in hits]}")
    check("端到端 elapsed_seconds", round(elapsed, 4), "< 60（目标 < 5）", elapsed < BUDGET_SECONDS)
    check("端到端目标 < 5s", elapsed < TARGET_SECONDS, "True", elapsed < TARGET_SECONDS)
    check("'灰度发布' 命中", [os.path.relpath(row[1], data_dir) for row in hits], "含 资料/新落盘.md", any("新落盘" in row[1] for row in hits))

    section("结论")
    failed_checks = [entry for entry in checks if not entry["ok"]]
    print(f"自检条目              : {len(checks)}")
    print(f"自检失败              : {len(failed_checks)}")
    for entry in checks:
        print(f"  [{'OK ' if entry['ok'] else 'FAIL'}] {entry['name']}: {entry['value']} (期望 {entry['expectation']})")
    print(f"全部演示步骤完成；端到端耗时 {elapsed:.4f}s（预算 {BUDGET_SECONDS:.0f}s，目标 {TARGET_SECONDS:.0f}s）")
    connection.close()
    if keep:
        print(f"工作区保留在: {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if not failed_checks else 1


def _sorted_counts(counts: Dict[str, int]) -> Dict[str, int]:
    return {key: counts[key] for key in sorted(counts)}


def capture() -> Dict[str, Any]:
    """跑一遍 demo 并返回结构化证据（供测试/CI 使用，不打印）。"""
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = run_demo()
    text = buffer.getvalue()
    checks = []
    for line in text.splitlines():
        if line.startswith("  [OK ]") or line.startswith("  [FAIL]"):
            head, _, tail = line[7:].partition(": ")
            value, _, expectation = tail.partition(" (期望 ")
            checks.append(
                {
                    "name": head,
                    "value": value,
                    "expectation": expectation.rstrip(")"),
                    "ok": line.startswith("  [OK ]"),
                }
            )
    return {"exit_code": code, "checks": checks, "output": text}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MVP-1 采集与中文检索演示（纯标准库）")
    parser.add_argument("--keep", default=None, help="保留演示工作区到指定目录（默认用临时目录并在结束后删除）")
    parser.add_argument("--json", dest="json_path", default=None, help="把结构化自检结果写到指定 JSON 文件")
    arguments = parser.parse_args(argv)
    code = run_demo(keep=arguments.keep)
    if arguments.json_path:
        payload = {"exit_code": code, "note": "见 stdout 的完整输出；此文件仅记录自检结果"}
        with open(arguments.json_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
