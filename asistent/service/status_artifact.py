"""把服务健康快照发布成"手机端能下载的文件对象"（SPEC §877 第 2 条取证要求）。

手机端（控制台，MVP-4）能读到的只有存储里的对象：`GET /api/artifacts` 列表 +
`GET /api/artifacts/<id>/content` 下载。所以这里做两件小事：

* 把一份**给人看的健康快照**（每轮都写：最近同步/索引结果、轮次、"正在等什么"、
  待处理计数、最近归档结论、最近错误）写成仓库外的 `service-status.json`（600）；
* 把它登记成一个 `FileArtifact`（`case_id=""`，名字固定 `service-status.json`），
  **只在内容真的变了**的时候重写同一个对象（ID 确定性，revision 递增）。

和 `service-state.json` 的分工：state 文件是服务自己的崩溃自愈缓存（只服务自己），
`service-status.json` 是**对外**的只读快照（手机端唯一入口）。两者都在仓库外、都在
`$ASISTENT_VAR` 下，且都可以随时删掉——删了下一轮会重建。

三道自我约束：

1. 快照里不得出现任何凭据 —— 发布前扫一遍明文，命中就**拒绝发布**并在返回值里说明
   （宁可手机端看不到，也不能把令牌送出去）；
2. 发布失败绝不影响服务运行（只记账、只写日志）；
3. 路径交给控制台的下载白名单去管（`console.artifact_roots`），本模块不改任何权限。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any, Dict, List, Optional, Sequence

from ..contracts import FileArtifact, digest_of, new_id

#: 手机端在文件列表里认这个名字
STATUS_ARTIFACT_NAME = "service-status.json"
STATUS_ARTIFACT_KIND = "service_status"
CONTENT_TYPE = "application/json"


def status_artifact_id() -> str:
    """确定性 ID：同一个服务在任何进程/任何重启里都指向同一个对象（不会堆垃圾）。"""
    return new_id("FileArtifact", token=digest_of(["service_status_artifact"])[:24])


def snapshot_path_for(state_path: str) -> str:
    """对外快照的落盘位置：与 `service-state.json` 同目录（同属 `$ASISTENT_VAR/state`）。"""
    return os.path.join(os.path.dirname(os.path.abspath(state_path)) or ".", STATUS_ARTIFACT_NAME)


def write_status_snapshot(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """原子写对外快照（`tmp` + `fsync` + `os.replace`，600）：掉电不会留半个 JSON。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=directory, prefix=".status-", suffix=".tmp", delete=False
    )
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, path)
    except BaseException:
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            os.remove(handle.name)
        except OSError:
            pass
        raise
    return {
        "path": os.path.abspath(path),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "size_bytes": len(text.encode("utf-8")),
    }


def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def publish_status_artifact(
    store: Any,
    *,
    state_path: str,
    clock: Any = None,
    actor_id: str = "service-console",
    protect: Sequence[str] = (),
) -> Dict[str, Any]:
    """发布（或更新）健康快照文件对象。返回可安全落日志/审计的结论。"""
    outcome: Dict[str, Any] = {
        "published": False,
        "artifact_id": status_artifact_id(),
        "reason": "",
        "sha256": "",
        "size_bytes": 0,
        "version": 0,
    }
    if not state_path or not os.path.isfile(state_path):
        outcome["reason"] = "state 文件还不存在"
        return outcome
    data = _read_bytes(state_path)
    if data is None:
        outcome["reason"] = "state 文件不可读"
        return outcome
    text = data.decode("utf-8", "replace")
    for secret in protect:
        if secret and len(str(secret)) >= 8 and str(secret) in text:
            outcome["reason"] = "快照里出现凭据：拒绝发布（宁可手机端看不到，也不能泄露）"
            return outcome
    digest = hashlib.sha256(data).hexdigest()
    outcome["sha256"] = digest
    outcome["size_bytes"] = len(data)
    artifact_id = outcome["artifact_id"]
    moment = clock() if callable(clock) else store.now()
    try:
        current = store.get("FileArtifact", artifact_id)
    except Exception:  # noqa: BLE001 - 还没有发布过
        current = None
    if current is not None:
        outcome["version"] = int(current.version or 1)
        if str(current.sha256 or "") == digest:
            outcome["reason"] = "快照未变化，跳过"
            return outcome
        current.path = os.path.abspath(state_path)
        current.name = STATUS_ARTIFACT_NAME
        current.size_bytes = len(data)
        current.sha256 = digest
        current.content_type = CONTENT_TYPE
        current.version = int(current.version or 1) + 1
        current.generated_at = moment
        try:
            saved = store.put(
                current,
                expected_revision=current.revision,
                actor_kind="SYSTEM",
                actor_id=actor_id,
                reason="更新服务健康快照（手机端可下载）",
            )
        except Exception as exc:  # noqa: BLE001
            outcome["reason"] = "更新失败：%s: %s" % (type(exc).__name__, exc)
            return outcome
        outcome["published"] = True
        outcome["version"] = int(saved.version or 1)
        return outcome

    artifact = FileArtifact(
        id=artifact_id,
        case_id="",
        path=os.path.abspath(state_path),
        name=STATUS_ARTIFACT_NAME,
        size_bytes=len(data),
        sha256=digest,
        content_type=CONTENT_TYPE,
        version=1,
        generated_at=moment,
        permission="owner_read",
    )
    try:
        saved = store.put(
            artifact,
            actor_kind="SYSTEM",
            actor_id=actor_id,
            reason="发布服务健康快照（手机端可下载）",
        )
    except Exception as exc:  # noqa: BLE001
        outcome["reason"] = "发布失败：%s: %s" % (type(exc).__name__, exc)
        return outcome
    outcome["published"] = True
    outcome["version"] = int(saved.version or 1)
    return outcome


def describe_snapshot_access(artifact_id: str) -> List[str]:
    """给文档/日志用的三行说明：手机端怎么读到这份快照（写清楚，免得靠猜）。"""
    return [
        "手机端读健康快照：GET /api/artifacts（找 name=%s，id=%s）" % (STATUS_ARTIFACT_NAME, artifact_id),
        "再取内容：GET /api/artifacts/%s/content（同一个 Bearer 令牌）" % artifact_id,
        "控制台只认存储里的文件对象，且路径必须在 console.artifact_roots 白名单内",
    ]


__all__ = [
    "STATUS_ARTIFACT_KIND",
    "STATUS_ARTIFACT_NAME",
    "describe_snapshot_access",
    "publish_status_artifact",
    "snapshot_path_for",
    "status_artifact_id",
    "write_status_snapshot",
]
