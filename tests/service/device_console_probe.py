"""设备端取证辅助：用**真 HTTP 请求**读服务健康快照与审批状态（纯标准库，3.10 兼容）。

`python3 -X utf8 tests/service/device_console_probe.py <token_path> <port> [--approve] [--expect-submits N]`

为什么要有这个脚本：设备上没有 curl 时也得能取证，而且"手机端能看到什么"必须是
**真的 HTTP 响应**，不能靠喊。它做四件事：

1. 读 `/api/health`、`/api/overview`、`/api/cases?include_closed=1`、`/api/approvals`；
2. 从 `/api/artifacts` 里找 `service-status.json`，再 `/api/artifacts/<id>/content` **下载**，
   把健康快照原文打出来（这就是手机端看到的那份）；
3. `--approve` 时：找到待审批草稿 → `POST /api/approvals`（带 confirm），打印受理结果；
4. `--expect-submits N` 时：等外发账本里出现 N 行 submit（证明"恰好一次"）。

令牌只从 600 文件读，**不进 argv、不进日志**。
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.parse

STATUS_NAME = "service-status.json"


def _token(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def _request(port: int, method: str, path: str, token: str, body=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=20)
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        return response.status, raw.decode("utf-8", "replace")
    finally:
        connection.close()


def _get_json(port: int, path: str, token: str):
    status, text = _request(port, "GET", path, token)
    if status != 200:
        raise SystemExit("GET %s -> %s\n%s" % (path, status, text[:400]))
    return json.loads(text)


def main(argv) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    token_path, port = argv[1], int(argv[2])
    approve = "--approve" in argv
    expect_submits = 0
    if "--expect-submits" in argv:
        expect_submits = int(argv[argv.index("--expect-submits") + 1])
    token = _token(token_path)

    print("== 未鉴权请求应被拒绝 ==")
    status, _ = _request(port, "GET", "/api/overview", "")
    print("GET /api/overview（不带令牌）-> %s" % status)

    health = _get_json(port, "/api/health", token)
    print("== /api/health ==")
    print(json.dumps(health, ensure_ascii=False, sort_keys=True)[:400])

    listing = _get_json(port, "/api/artifacts", token)
    rows = [row for row in listing.get("artifacts", []) if row.get("name") == STATUS_NAME]
    print("== 文件列表里的服务健康快照 ==")
    print(json.dumps(rows, ensure_ascii=False, sort_keys=True)[:600])
    if rows:
        artifact_id = str(rows[0]["id"])
        status, text = _request(
            port, "GET", "/api/artifacts/%s/content" % urllib.parse.quote(artifact_id), token
        )
        print("GET /api/artifacts/%s/content -> %s（%d 字节）" % (artifact_id, status, len(text)))
        snapshot = json.loads(text)
        print("== 手机端下载到的健康快照（关键字段）==")
        for key in (
            "rounds_completed",
            "rounds_failed",
            "waiting_on",
            "pending",
            "last_archive",
            "notes",
            "status_artifact",
            "state_path",
            "console_url",
        ):
            if key in snapshot:
                print("  %s = %s" % (key, json.dumps(snapshot[key], ensure_ascii=False, sort_keys=True)[:400]))

    cases = _get_json(port, "/api/cases?include_closed=1", token)
    drafts = _get_json(port, "/api/drafts", token)
    approvals = _get_json(port, "/api/approvals", token)
    questions = _get_json(port, "/api/questions", token)
    print("== 待处理（手机端首页数字）==")
    print(
        "  事务=%s 草稿=%s 审批=%s 问题=%s"
        % (cases.get("total"), drafts.get("total"), approvals.get("total"), questions.get("total"))
    )
    print("  审批明细: %s" % json.dumps(approvals.get("approvals"), ensure_ascii=False, sort_keys=True)[:600])

    if approve:
        if not drafts.get("drafts"):
            raise SystemExit("没有可审批的草稿")
        draft_id = str(drafts["drafts"][0]["id"])
        detail = _get_json(port, "/api/drafts/%s" % urllib.parse.quote(draft_id), token)
        current = detail.get("current") or {}
        body = {"draft_id": draft_id, "version_no": int(current["version_no"]), "confirm": True}
        status, text = _request(port, "POST", "/api/approvals", token, body)
        print("== 从手机端提交审批 ==")
        print("POST /api/approvals %s -> %s" % (json.dumps(body, ensure_ascii=False), status))
        print(text[:600])

    if expect_submits:
        deadline = time.time() + 120
        while time.time() < deadline:
            rows = _submit_rows()
            if len(rows) >= expect_submits:
                print("== 外发账本 ==")
                for row in rows:
                    print(
                        "  event=%s action=%s key=%s to=%s"
                        % (row.get("event"), row.get("action_id"), row.get("idempotency_key", "")[:24], row.get("to"))
                    )
                break
            time.sleep(1)
        else:
            raise SystemExit("等待 %d 行 submit 超时" % expect_submits)
    return 0


def _submit_rows():
    """读服务写的外发账本（`$ASISTENT_VAR/state/outbound-calls.jsonl`）。"""
    var_root = os.environ.get("ASISTENT_VAR", os.path.join(os.path.expanduser("~"), "asistent-var"))
    path = os.path.join(var_root, "state", "outbound-calls.jsonl")
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return [row for row in rows if row.get("event") == "submit"]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
