"""错误语义（SPEC §4.3、§13.1）：接口必须明确错误，且能映射到失败分类。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .enums import FailureClass


class AsistentError(Exception):
    """所有契约错误的基类：带稳定 code 与失败分类，便于审计与恢复分流。"""

    code = "ASISTENT_ERROR"
    failure_class: FailureClass = FailureClass.TOOL_FAILURE

    def __init__(self, message: str, *, detail: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "failure_class": str(self.failure_class),
            "detail": self.detail,
        }


class ContractError(AsistentError):
    """调用方违反契约（签名、必填字段、状态前置条件）。"""

    code = "CONTRACT_ERROR"


class ClockAnomaly(AsistentError):
    """时钟不可信：拒绝写入业务时间戳（SPEC §11.2、§21.7，设备实测见 docs/env/report.md §B6）。"""

    code = "CLOCK_ANOMALY"
    failure_class = FailureClass.SYSTEM_CRASH


class ValidationError(ContractError):
    code = "VALIDATION_ERROR"
    failure_class = FailureClass.INVALID_INPUT


class IllegalTransition(ContractError):
    """非法状态转换：必须拒绝并记录原因（SPEC §16 MVP-0）。"""

    code = "ILLEGAL_TRANSITION"


class RevisionConflict(ContractError):
    """旧版本写入：拒绝并返回冲突，禁止静默覆盖（SPEC §21.1）。"""

    code = "REVISION_CONFLICT"


class NotFound(ContractError):
    code = "NOT_FOUND"
    failure_class = FailureClass.MISSING_DATA


class DuplicateObject(ContractError):
    """幂等键已存在：返回既有对象而不是创建第二份（SPEC §7.3、§21.2）。"""

    code = "DUPLICATE_OBJECT"


class PermissionDenied(AsistentError):
    code = "PERMISSION_DENIED"
    failure_class = FailureClass.PERMISSION_FAILURE


class ApprovalRequired(PermissionDenied):
    code = "APPROVAL_REQUIRED"


class NotAuthorized(PermissionDenied):
    """通道**未授权**：需要用户重新授权（契约 v0.7，CP-20261010-19）。

    与 `ApprovalRequired` 的分工必须分清——两者都是"现在不能做"，但下一步动作完全不同：

    - `ApprovalRequired`：权限在、审批没给 → 下一步是"拿审批引用"（用户在本系统内批准）；
    - `NotAuthorized`：凭据不在/过期/被撤销 → 下一步是"重新授权"（用户去通道那边登录），
      **重试多少次都没用**，所以 `retryable = False`。

    把它单独登记是因为它极易被错误归因：当成网络抖动 → 无限重试；当成审批缺失 →
    提示用户"请批准"，而用户批了也没用。归档通道（bdpan 未登录）与邮件通道（授权码失效）
    都会命中这一类。
    """

    code = "NOT_AUTHORIZED"
    failure_class = FailureClass.PERMISSION_FAILURE

    def __init__(self, message: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"retryable": False, "needs_reauth": True}
        if detail:
            payload.update(detail)
        super().__init__(message, detail=payload)


class ApprovalInvalid(AsistentError):
    """审批缺失、过期、被消费、被撤销或绑定版本已变化（SPEC §8.4、§21.2）。"""

    code = "APPROVAL_INVALID"
    failure_class = FailureClass.APPROVAL_EXPIRED


class ActionNotClaimable(AsistentError):
    """动作已被占位或状态不允许再次外呼（SPEC §21.2）。"""

    code = "ACTION_NOT_CLAIMABLE"


class ExternalResultUnknown(AsistentError):
    """结果未知：禁止自动重试，必须转人工裁决（SPEC §7.5、§21.2）。"""

    code = "EXTERNAL_RESULT_UNKNOWN"
    failure_class = FailureClass.EXTERNAL_UNKNOWN


class ArchiveIntegrityError(AsistentError):
    """归档完整性校验失败：只有逐文件哈希一致才能标记完成（SPEC §12.5、§21.4）。"""

    code = "ARCHIVE_INTEGRITY_ERROR"
    failure_class = FailureClass.ARCHIVE_FAILURE


class SecretDetected(AsistentError):
    """疑似秘密：阻止归档并要求处理（SPEC §21.4）。"""

    code = "SECRET_DETECTED"
    failure_class = FailureClass.ARCHIVE_FAILURE


class UntrustedContentViolation(AsistentError):
    """不可信内容试图影响权限/审批/规则：必须拒绝（SPEC §13.5）。"""

    code = "UNTRUSTED_CONTENT_VIOLATION"
    failure_class = FailureClass.PERMISSION_FAILURE
