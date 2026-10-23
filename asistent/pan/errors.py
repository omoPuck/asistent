"""归档通道错误分类（SPEC §13.1 失败分类、§21.3）。

设计原则：**通道必须能把「确定失败」「结果未知」「未授权」三者分开**。
- 确定失败（远端明确拒绝/不存在/冲突）→ 可安全重试的 `FAILED_SAFE`；
- 结果未知（超时/崩溃/响应无法解析）→ `UNKNOWN`，**绝不**当作"没发生"；
- 未授权（CLI 未登录、令牌过期）→ 显式 `NOT_AUTHORIZED` + 明确的下一步动作，
  不允许静默跳过、也不允许假装成功（A-5 约束 4/8）。

这些错误都在 `asistent.contracts.errors` 的分类体系内（`code` 稳定、带 `failure_class`），
只有 `NOT_AUTHORIZED` 是本包新增的稳定码：契约没有覆盖"通道未授权"这一类，
需要 Lead 裁决是否升级为契约级语义（见 docs/mvp/mvp-7.md CP-MVP7-3）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..contracts.enums import FailureClass
from ..contracts.errors import (
    ApprovalRequired,
    AsistentError,
    ExternalResultUnknown,
    NotAuthorized,
    PermissionDenied,
)

#: 未授权时给出的下一步动作（必须能被直接复制执行，凭据由用户本人输入）
LOGIN_NEXT_STEP = (
    "待用户授权：在设备上执行 `bdpan login --get-auth-url --accept-disclaimer` 取授权链接，"
    "浏览器授权后 `printf '%s\\n' \"<授权码>\" | bdpan login --set-code-stdin --accept-disclaimer`；"
    "完成后重跑同一命令（同一 archive_id 会复用已校验对象，不会重复上传）。"
)


class PanError(AsistentError):
    """归档通道错误基类。"""

    code = "PAN_ERROR"
    failure_class = FailureClass.TOOL_FAILURE


class PanNotInstalled(PanError):
    """找不到 bdpan 可执行文件（环境问题，不是远端问题）。"""

    code = "PAN_NOT_INSTALLED"


class PanNotAuthorized(NotAuthorized):
    """通道未授权：CLI 未登录 / 令牌过期 / 授权被撤销（SPEC §21.3，A-5 约束 8）。

    契约 v0.7（CP-20261010-19）起，`NOT_AUTHORIZED` 是**契约级稳定码**：
    继承 `asistent.contracts.errors.NotAuthorized`（它本身继承 `PermissionDenied`，
    failure_class = PERMISSION_FAILURE），这样调用方在失败分类上天然把它当权限问题而不是网络抖动，
    并且与 `ApprovalRequired`（需审批引用）形成明确分工——本类只需补上"下一步怎么授权"。
    """

    code = "NOT_AUTHORIZED"
    failure_class = FailureClass.PERMISSION_FAILURE

    def __init__(self, message: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        payload = {"next_step": LOGIN_NEXT_STEP, "retryable": False}
        if detail:
            payload.update(detail)
        super().__init__(message, detail=payload)


class PanUnavailable(ExternalResultUnknown):
    """**结果未知**：超时 / 崩溃 / 响应无法解析（SPEC §13.1 `EXTERNAL_UNKNOWN`）。

    语义要点：结果未知 ≠ 失败。调用方必须先用 `stat`/回读判定远端真实状态，
    判定不了就保持未知并上报，**绝不**盲目重传、也不得直接标成功（A-5 约束 3/4）。
    """

    code = "EXTERNAL_RESULT_UNKNOWN"
    failure_class = FailureClass.EXTERNAL_UNKNOWN

    def __init__(self, message: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        payload = {"result_class": "UNKNOWN", "retryable": True, "must_decide_by_stat": True}
        if detail:
            payload.update(detail)
        super().__init__(message, detail=payload)


class PanWriteLocked(ApprovalRequired):
    """写入未获批准：通道默认锁死，必须带 approval_ref 解锁（SPEC §21.3）。"""

    code = "APPROVAL_REQUIRED"
    failure_class = FailureClass.PERMISSION_FAILURE

    def __init__(self, action: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        payload = {
            "action": action,
            "reason": "归档通道写入默认锁定；需要用户/认证控制明确批准",
            "next_step": "拿到审批记录后调用 unlock_writes(approval_ref=<审批记录 ID>)",
        }
        if detail:
            payload.update(detail)
        super().__init__(f"{action} 未获批准：归档通道写入默认锁定", detail=payload)


__all__ = [
    "LOGIN_NEXT_STEP",
    "PanError",
    "PanNotAuthorized",
    "PanNotInstalled",
    "PanUnavailable",
    "PanWriteLocked",
]
