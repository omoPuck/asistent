"""一条命令跑出"纵向端到端"的原始证据（文档 §原始输出 用的就是它的输出）。

用法：

```sh
python3 -X utf8 tests/service/demo_vertical.py [工作目录]
```

它会（全部在仓库外的工作目录里做，跑完**不删**，方便自己翻）：

1. 放一封真实结构的活动通知（离线邮件夹具）；
2. 写两份配置：一份"只理解不外发"，一份"受控假通道外发 + 本地目录归档"；
3. 跑三轮并打印**原始轮次报告 JSON**（`--once --json`）；
4. 中间用控制台接口批准当前草稿版本（和手机端走的是同一个入口）；
5. 最后打印归档清单里每个文件的 sha256 与磁盘对照、外发账本、以及对象汇总。

注意：这里用的是**受控假通道**（`channel=scripted` 只是把"要发什么"记账），
它证明的是"服务在正确的时间、对正确的内容、只做了一次动作"，
**不证明**真的能连 163 邮箱（那要靠用户完成授权后的真机运行）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from tests.service.support import (  # noqa: E402
    FAKE_TOKEN,
    ServiceWorkbench,
    audit_rows,
    parse_round_report,
    python_executable,
    submit_lines,
)

WIDTH = 100


def banner(title: str) -> None:
    print("\n" + "=" * WIDTH)
    print(title)
    print("=" * WIDTH)


def main(argv) -> int:
    root = os.path.abspath(argv[1]) if len(argv) > 1 else os.path.join(os.environ.get("TEMP", "/tmp"), "mvp8-demo")
    os.makedirs(root, exist_ok=True)
    bench = ServiceWorkbench(root)
    print("工作目录（仓库外）：%s" % root)
    print("存储后端：%s" % bench.backend)

    banner("1) 放一封活动通知（离线邮件夹具）")
    path = bench.add_notice("notice-01.eml", subject="周末科普讲座报名确认（含活动时间）")
    print("$ ls -l %s" % path)
    print("写入完成：%d 字节" % os.path.getsize(path))

    banner("2) 写配置（间隔/通道/库路径全部来自仓库外）")
    understand_only = bench.default_config()
    sending = bench.sending_config()
    archiving = bench.archiving_config()
    for name in ("service.json", "service-send.json", "service-archive.json"):
        target = os.path.join(bench.config_dir, name)
        print("\n$ cat %s" % target)
        with open(target, "r", encoding="utf-8") as handle:
            print(handle.read().rstrip())

    banner("3) 第一轮：同步 → 采集/索引 → 理解 → 生成草稿（不外发）")
    print("$ python3 -X utf8 -m asistent.service --once --json --config %s" % understand_only)
    first = bench.run_round(understand_only)
    report = parse_round_report(first.stdout)
    print("-" * WIDTH)
    print(first.stdout.rstrip())
    print("-" * WIDTH)
    phase = {item["name"]: item for item in report["phases"]}
    print("看这一轮的关键数字：")
    print("  sync        : %s" % json.dumps(phase["sync"]["detail"], ensure_ascii=False, sort_keys=True)[:220])
    print("  understand  : %s" % json.dumps(phase["understand"]["detail"].get("analyzed"), ensure_ascii=False)[:320])
    print("  prepare     : %s" % json.dumps(phase["prepare"]["detail"], ensure_ascii=False)[:220])
    print("  待处理      : %s" % json.dumps(report["pending"], ensure_ascii=False, sort_keys=True))
    print("  正在等      : %s" % report["waiting_on"])

    banner("4) 用户批准当前草稿版本（等价于手机端点'批准'）")
    from asistent.console.auth import TokenAuth
    from asistent.console.service import ConsoleService

    store = bench.open_store()
    try:
        draft = list(store.find("Draft", limit=None))[0]
        version = store.get("DraftVersion", draft.current_version_id)
        print("草稿 %s 当前版本 %s（v%s）" % (draft.id, version.id, version.version_no))
        approval = ConsoleService(store, TokenAuth(FAKE_TOKEN), server_name="mvp8-demo").submit_approval(
            {"draft_id": draft.id, "version_no": int(version.version_no), "confirm": True}
        )
        print("$ POST /api/approvals {'draft_id': %r, 'version_no': %s, 'confirm': true}" % (draft.id, version.version_no))
        print("审批结果：state=%s id=%s 绑定对象=%s"
              % (approval["approval"]["state"], approval["approval"]["id"], approval["approval"]["target_object_ids"]))
    finally:
        store.close()

    banner("5) 第二轮：准备动作 → 外发（受控假通道，恰好一次）")
    print("$ python3 -X utf8 -m asistent.service --once --json --config %s" % sending)
    second = bench.run_round(sending)
    print(second.stdout.rstrip())
    print("-" * WIDTH)
    phase = {item["name"]: item for item in parse_round_report(second.stdout)["phases"]}
    print("  prepare  : %s" % json.dumps(phase["prepare"]["detail"], ensure_ascii=False)[:260])
    print("  outbound : %s" % json.dumps(phase["outbound"]["detail"], ensure_ascii=False)[:260])

    banner("6) 第三轮：同一份配置再跑一次（**不得重发**）")
    third = bench.run_round(sending)
    phase = {item["name"]: item for item in parse_round_report(third.stdout)["phases"]}
    print("  outbound : %s" % json.dumps(phase["outbound"]["detail"], ensure_ascii=False)[:260])
    ledger = os.path.join(bench.state_dir, "outbound-calls.jsonl")
    print("$ grep -c '\"event\": \"submit\"' %s" % ledger)
    print("submit 行数 = %d" % len(submit_lines(ledger)))
    print("$ cat %s" % ledger)
    with open(ledger, "r", encoding="utf-8") as handle:
        for line in handle:
            print("  " + line.rstrip()[:200])

    banner("7) 第四轮：归档（本地目录模拟远端；逐文件 sha256 对照）")
    fourth = bench.run_round(archiving)
    phase = {item["name"]: item for item in parse_round_report(fourth.stdout)["phases"]}
    record = phase["archive"]["detail"]["archived"][0]
    print("$ python3 -X utf8 -m asistent.service --once --json --config %s" % archiving)
    print("  archive  : %s" % json.dumps(record, ensure_ascii=False, sort_keys=True)[:700])
    from asistent.archive.engine import ArchiveLayout, remote_component

    layout = ArchiveLayout()
    # 远端布局：`<本地模拟盘根>/个人助手归档/事务归档/<安全化 archive_id>/{manifest.json,objects/**}`
    manifest_path = os.path.join(bench.archive_root, *layout.manifest_path(record["archive_id"]).split("/"))
    print("$ cat %s" % manifest_path)
    print("  远端布局：%s" % layout.archive_dir(record["archive_id"]))
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    print("  scope=%s scope_ref=%s 文件数=%d policy=%s"
          % (manifest.get("scope"), manifest.get("scope_ref"), len(manifest.get("entries") or []), manifest.get("policy")))
    print("  顶层键：%s" % ", ".join(sorted(manifest.keys())))
    print("  policy_gate=%s（spec §21.3 的 U-6 还没确认：先不带 ArchivePolicy，但把这件事写进清单）"
          % json.dumps(manifest.get("policy_gate"), ensure_ascii=False))
    print("  secret_scan.clean=%s" % (manifest.get("secret_scan") or {}).get("clean"))
    import hashlib

    ok = True
    for entry in manifest.get("entries") or []:
        remote = os.path.join(
            bench.archive_root, *(layout.object_path(record["archive_id"], entry["relative_path"]).split("/"))
        )
        digest = ""
        size = 0
        if os.path.isfile(remote):
            with open(remote, "rb") as handle:
                data = handle.read()
            digest = hashlib.sha256(data).hexdigest()
            size = len(data)
        match = digest == entry["sha256"] and size == int(entry["size_bytes"])
        ok = ok and match
        print("  %-46s sha256 %s  %s" % (entry["relative_path"], "匹配" if match else "不匹配", entry["sha256"][:16] + "..."))
    print("逐文件哈希校验：%s" % ("全部通过" if ok else "有不一致（缺陷）"))

    banner("8) 对象汇总（这一整套动作到底在存储里留下了什么）")
    cmd = [python_executable(), "-X", "utf8", os.path.join("tests", "service", "evidence_dump.py"), archiving]
    result = subprocess.run(cmd, cwd=REPO_ROOT, env=bench.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print("$ %s" % " ".join(cmd))
    print(result.stdout.decode("utf-8", "replace").rstrip())
    store = bench.open_store()
    try:
        print("\n时间语义（真的落到了 `Fact.temporal_fields` 里，不是'提了一嘴'）：")
        for fact in store.find("Fact", limit=None):
            for field in fact.temporal_fields or ():
                if field.get("semantic") in ("registration_deadline", "event_start", "event_end"):
                    print(
                        "  semantic=%-22s normalized_utc=%s  raw=%r  tz=%s"
                        % (
                            field.get("semantic"),
                            field.get("normalized_utc"),
                            field.get("raw_text"),
                            field.get("source_time_zone"),
                        )
                    )
        print("\n审计（最后 12 行）：")
        for row in audit_rows(store)[-12:]:
            print("  %s %s %s" % (row.get("action"), row.get("object_type"), str(row.get("detail"))[:80]))
    finally:
        store.close()
    bench.close_runtimes()
    print("\n工作目录保留在：%s（可以自己再看）" % root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
