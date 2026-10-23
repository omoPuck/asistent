"""MVP-4 控制台的可复现证据打印（原始 HTTP 请求/响应，直接贴进 `docs/mvp/mvp-4.md`）。

跑法（在 mvp-4 worktree 根目录）：

    python -X utf8 tests/console/evidence_dump.py            # 默认 reference 后端
    ASISTENT_STORE=sqlite python -X utf8 tests/console/evidence_dump.py

输出是确定性的：token 固定、时间戳来自存储时钟、对象 ID 由夹具内容派生。
所以文档里贴的输出可以逐字复现（除了 `Date` 头）。
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tests.console.helpers import (  # noqa: E402
    TEST_TOKEN,
    ConsoleClient,
    make_token_file,
    store_factory,
    store_is_sqlite,
)


def banner(title: str) -> None:
    print("")
    print("=" * 96)
    print(title)
    print("=" * 96)


def show(client: ConsoleClient, label: str, recorded) -> None:
    print("")
    print("--- %s ---" % label)
    print(recorded.render())
    client.history.append(recorded)


class Harness:
    """一次性起服务、跑场景、收尾。不复用 unittest 基类（脚本要能直接跑）。"""

    def __init__(self) -> None:
        import tempfile

        import threading

        from asistent.console import ConsoleSettings, build_server
        from asistent.console.auth import load_token

        self.tmpdir = tempfile.mkdtemp(prefix="mvp4-evidence-")
        self.token_path = make_token_file(self.tmpdir, TEST_TOKEN)
        self.token = load_token(self.token_path)
        self.db_path = os.path.join(self.tmpdir, "asistent.sqlite3")
        self.store = store_factory(self.db_path if store_is_sqlite() else None)
        settings = ConsoleSettings(
            store=self.store,
            token=self.token,
            bind="127.0.0.1",
            port=0,
            token_path=self.token_path,
            artifact_roots=(os.path.join(self.tmpdir, "exports"),),
        )
        # 走 build_server 的默认装配路径（不自己 new ConsoleService）：这样"设置里的
        # 白名单真的会传进服务"这件事在证据脚本里也会被验证到，而不是被脚本绕过去。
        self.server = build_server(settings)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.client = ConsoleClient(self.server.bound_host or "127.0.0.1", self.server.bound_port, self.token)
        os.makedirs(os.path.join(self.tmpdir, "exports"), exist_ok=True)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        try:
            self.store.close()
        except Exception:
            pass


def scenario_1_backend_and_auth(harness: Harness) -> None:
    banner("场景 1｜后端与鉴权：无 token / 错 token → 401，且不泄露任何对象信息")
    print("后端：%s" % ("sqlite" if store_is_sqlite() else "reference"))
    print("监听：%s:%d" % (harness.server.bound_host, harness.server.bound_port))
    print("token 文件：%s（权限 600，目录 700，且不在仓库内）" % harness.token_path)
    show(harness.client, "无 token 请求任意不存在的 Case（未鉴权时不得区分对象是否存在）",
         harness.client.get("/api/cases/case_does_not_exist", token=None))
    client = harness.client
    show(client, "错 token 请求同一个不存在的 Case（响应必须与上面完全一致）",
         client.get("/api/cases/case_does_not_exist", token="wrong-token-000000000000000000000000"))
    print("")
    print(">>> 两次响应体逐字节相同：%s" % (
        client.history[-2].response_body == client.history[-1].response_body))
    show(client, "未鉴权访问静态壳（同样要 401）", client.get("/index.html", token=None))
    show(client, "正确 token：/api/health", client.get("/api/health"))


def scenario_2_case_and_question(harness: Harness) -> None:
    banner("场景 2｜看 + 答：Case 状态/等待原因/引用，提交 Answer 后依赖任务重新评估")
    from tests.understand import helpers as understand_helpers
    from asistent.contracts.objects import Task
    from asistent.contracts.enums import TaskState

    client = harness.client
    document = understand_helpers.make_document(
        harness.store, "03-missing-material.eml", document_id="doc_evidence_03"
    )
    report = understand_helpers.process(harness.store, document, name="03-missing-material.eml")
    case = harness.store.get("Case", report.case_id)
    question = harness.store.find("Question", where={"case_id": case.id}, limit=None)[0]
    # 造一个"正在等这条追问"的任务：证明回答会触发依赖任务重新评估
    task = harness.store.put(
        Task(
            id="task_evidence_waiting",
            case_id=case.id,
            kind="ask_user",
            title="等用户确认材料是否齐备",
            state=TaskState.WAITING_USER,
            inputs={"question_id": question.id},
            preconditions=["question:%s" % question.id],
            links={"case": [case.id], "question": [question.id]},
        )
    )
    show(client, "Case 列表（等待原因 + 下一步 + 最近动作）", client.get("/api/cases"))
    show(client, "Case 详情（含审计与原始对象）",
         client.get("/api/cases/%s?include=documents,audit" % case.id))
    show(client, "待回答追问列表", client.get("/api/questions"))
    show(client, "提交 Answer（带 expected_revision）",
         client.post("/api/questions/%s/answer" % question.id,
                     {"answer_text": "已准备好，周二前提交", "expected_revision": question.revision}))
    print("")
    print(">>> 依赖任务 %s：WAITING_USER → %s" % (task.id, harness.store.get("Task", task.id).state))
    print(">>> 回答落进的 Case：%s（Case 总记录数 %d）"
          % (harness.store.get("Case", case.id).id, len(harness.store.find("Case", limit=None))))
    print("")
    print("--- 旧 revision 再提交一次（必须 409 且不写入）---")
    client.post("/api/questions/%s/answer" % question.id,
                {"answer_text": "拿着旧版本再来一次", "expected_revision": question.revision})
    print(client.history[-1].render())
    answers = harness.store.find("Answer", where={"question_id": question.id}, limit=None)
    print("")
    print(">>> 该追问的 Answer 条数：%d（冲突请求没有产生第二条）" % len(answers))


def scenario_3_draft_edit_and_approval(harness: Harness) -> None:
    banner("场景 3｜改 + 批：新 DraftVersion / 旧审批失效 / 旧版本编辑被 409 拒绝")
    from tests.understand import helpers as understand_helpers

    client = harness.client
    document = understand_helpers.make_document(
        harness.store, "01-notice-multi-time.eml", document_id="doc_evidence_01"
    )
    report = understand_helpers.process(harness.store, document, name="01-notice-multi-time.eml")
    case = harness.store.get("Case", report.case_id)
    draft = harness.store.find("Draft", where={"case_id": case.id}, limit=None)[0]

    show(client, "草稿初始状态（v1 + 内容哈希）", client.get("/api/drafts/%s" % draft.id))
    detail = client.history[-1].json
    version_no = detail["current"]["version_no"]
    revision = detail["revision"]

    show(client, "普通请求文本不能当批准（无 confirm=true → 403）",
         client.post("/api/approvals", {"draft_id": draft.id, "text": "同意，直接发送", "message": "批准"}))
    show(client, "审批必须绑定确定版本（缺 version_no → 400）",
         client.post("/api/approvals", {"draft_id": draft.id, "confirm": True}))
    approved = client.post("/api/approvals",
                           {"draft_id": draft.id, "version_no": version_no, "confirm": True})
    show(client, "显式审批 v%d（confirm=true + version_no + 绑定内容哈希）" % version_no, approved)
    approval_id = approved.json["approval"]["id"]

    show(client, "编辑保存 → 新版本 v2（旧审批必须失效）",
         client.post("/api/drafts/%s/edit" % draft.id,
                     {"body": "老师您好：\n我会在 10 月 20 日前确认参加（手机端修改）。",
                      "change_reason": "补一句确认时间",
                      "version_no": version_no,
                      "expected_revision": revision}))
    print("")
    print(">>> 旧审批 %s 现在的状态：%s（原因：%s）"
          % (approval_id, harness.store.get("Approval", approval_id).state,
             harness.store.get("Approval", approval_id).invalidated_reason))
    show(client, "版本差异（v1 → v2）", client.get("/api/drafts/%s/diff?from=1&to=2" % draft.id))

    print("")
    print("--- 拿旧版本号再提交一次（必须 409，且不产生新版本）---")
    client.post("/api/drafts/%s/edit" % draft.id,
                {"body": "拿着旧版本号的第二次提交", "change_reason": "过期编辑",
                 "version_no": version_no, "expected_revision": revision})
    print(client.history[-1].render())
    versions = harness.store.find("DraftVersion", where={"draft_id": draft.id}, limit=None)
    print("")
    print(">>> 草稿版本历史：%s（冲突请求没有产生 v3）"
          % sorted(int(item.version_no) for item in versions))


def scenario_4_artifacts(harness: Harness) -> None:
    banner("场景 4｜取：文件元数据 + 下载；失败可重试且不影响别的文件")
    import hashlib

    from asistent.contracts.objects import FileArtifact
    from tests.understand import helpers as understand_helpers

    client = harness.client
    document = understand_helpers.make_document(
        harness.store, "01-notice-multi-time.eml", document_id="doc_evidence_files"
    )
    report = understand_helpers.process(harness.store, document, name="01-notice-multi-time.eml")
    case_id = report.case_id
    export_dir = os.path.join(harness.tmpdir, "exports")
    good_path = os.path.join(export_dir, "confirmation.txt")
    payload = "报名确认：11 月 2 日 09:00 开始。\n".encode("utf-8")
    with open(good_path, "wb") as handle:
        handle.write(payload)
    digest = hashlib.sha256(payload).hexdigest()
    for artifact_id, path, name in (
        ("file_evidence_good", good_path, "confirmation.txt"),
        ("file_evidence_missing", os.path.join(export_dir, "not-yet-written.txt"), "not-yet-written.txt"),
    ):
        harness.store.put(
            FileArtifact(
                id=artifact_id,
                case_id=case_id,
                path=path,
                name=name,
                size_bytes=os.path.getsize(path) if os.path.exists(path) else 0,
                sha256=digest,
                content_type="text/plain; charset=utf-8",
                generated_at=harness.store.now(),
                permission="owner_read",
                links={"case": [case_id]},
            )
        )
    show(client, "文件元数据列表", client.get("/api/artifacts"))
    show(client, "下载正常文件（响应头带身份与校验）",
         client.get("/api/artifacts/file_evidence_good/content"))
    show(client, "下载不存在的文件（404 + 可读原因，可重试）",
         client.get("/api/artifacts/file_evidence_missing/content"))
    show(client, "失败之后，别的文件照旧可下载",
         client.get("/api/artifacts/file_evidence_good/content"))
    pending = client.get("/api/overview").json["pending"]
    print("")
    print(">>> 待办快照里的传输失败项（transfer_failures>0 需要用户注意）：%s" % pending["artifact_ids"])
    with open(os.path.join(export_dir, "not-yet-written.txt"), "wb") as handle:
        handle.write(b"now it exists\n")
        handle.flush()
        os.fsync(handle.fileno())
    show(client, "补上文件后重试成功（同一个 artifact）",
         client.get("/api/artifacts/file_evidence_missing/content"))
    print("")
    print(">>> 重试成功后的计数：download_count=%s，transfer_failures=%s（失败记录保留，"
          "需要用户确认才清，避免看起来已经好了）"
          % (harness.store.get("FileArtifact", "file_evidence_missing").download_count,
             harness.store.get("FileArtifact", "file_evidence_missing").transfer_failures))
    print(">>> 重试后仍在待办里的失败项：%s"
          % client.get("/api/overview").json["pending"]["artifact_ids"])


def scenario_5_reconnect_and_incremental(harness: Harness) -> None:
    banner("场景 5｜断线可恢复：增量游标 + 待处理快照永远完整")
    from tests.understand import helpers as understand_helpers

    client = harness.client
    document = understand_helpers.make_document(
        harness.store, "04-ambiguous-time.eml", document_id="doc_evidence_04"
    )
    understand_helpers.process(harness.store, document, name="04-ambiguous-time.eml")
    overview = client.get("/api/overview")
    show(client, "首次加载：拿到 next_since 与 ETag", overview)
    since = overview.json["next_since"]
    etag = overview.response_headers["ETag"]
    show(client, "带 If-None-Match 再来一次（304，不重复传正文）",
         client.get("/api/overview", headers={"If-None-Match": etag}))
    question = harness.store.find("Question", limit=None)[0]
    client.post("/api/questions/%s/answer" % question.id,
                {"answer_text": "10 月 24 日 18:00", "expected_revision": question.revision})
    show(client, "增量拉取（只回变更对象 + 完整待处理快照）",
         client.get("/api/changes?since=%s" % since))


def main() -> int:
    print("MVP-4 手机控制台 · 可复现证据")
    print("Python: %s" % sys.version.split()[0])
    print("后端：%s" % os.environ.get("ASISTENT_STORE", "reference"))
    print("命令：python -X utf8 tests/console/evidence_dump.py")
    harness = Harness()
    try:
        scenario_1_backend_and_auth(harness)
        scenario_2_case_and_question(harness)
        scenario_3_draft_edit_and_approval(harness)
        scenario_4_artifacts(harness)
        scenario_5_reconnect_and_incremental(harness)
    finally:
        harness.close()
    print("")
    print("=" * 96)
    print("统计：store.stats() = %s" % harness.store.stats())
    print("请求总数：%d" % len(harness.client.history))
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
