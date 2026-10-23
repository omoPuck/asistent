"""控制台错误类型：**错误响应本身也是接口**，所以统一在这里定义。

约定（安全相关，不要随手改）：

* 所有错误响应体都是 `{"error": {"code": ..., "message": ...}}`，字段固定；
* `Unauthorized` 的文案是**常量**，绝不拼接对象 ID / 数量 / 是否存在等信息
  （SPEC §13.4：未鉴权不得泄露"某个 Case 是否存在"）；
* 409 用于版本冲突，必须带可读原因与当前 revision，让手机端知道"该刷新了"。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class ConsoleError(Exception):
    """控制台层错误基类。`status` 即 HTTP 状态码。"""

    status = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_payload(self) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            error["detail"] = self.detail
        return {"error": error}


class BadRequest(ConsoleError):
    """请求本身有问题（缺字段、类型不对、非法状态值）。"""

    status = 400
    code = "BAD_REQUEST"


class Unauthorized(ConsoleError):
    """未通过鉴权。文案固定，**不含**任何对象维度信息。"""

    status = 401
    code = "UNAUTHORIZED"

    #: 唯一允许的文案：只有这一条，不回显 token、不说明"是哪个 Case 的请求"
    MESSAGE = "需要有效的控制台令牌"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.MESSAGE)


class Forbidden(ConsoleError):
    """已鉴权但该操作不允许（例如把聊天文本当审批）。"""

    status = 403
    code = "FORBIDDEN"


class NotFound(ConsoleError):
    """已鉴权且对象不存在。"""

    status = 404
    code = "NOT_FOUND"


class TooManyRequests(ConsoleError):
    """鉴权失败次数过多，进入全局冷却。

    文案与 401 同样**不透露**任何对象维度信息；唯一额外信息是"还要等多久"。
    """

    status = 429
    code = "AUTH_THROTTLED"

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after = max(1, int(round(float(retry_after_seconds))))
        super().__init__(
            "鉴权失败次数过多，请稍后再试",
            detail={"retry_after_seconds": self.retry_after},
        )


class Conflict(ConsoleError):
    """版本冲突：调用方拿的是旧版本，写入被拒。"""

    status = 409
    code = "REVISION_CONFLICT"

    def __init__(
        self,
        message: str,
        *,
        object_type: str = "",
        object_id: str = "",
        expected_revision: Optional[int] = None,
        current_revision: Optional[int] = None,
    ) -> None:
        detail: Dict[str, Any] = {"object_type": object_type, "object_id": object_id}
        if expected_revision is not None:
            detail["expected_revision"] = expected_revision
        if current_revision is not None:
            detail["current_revision"] = current_revision
            detail["action"] = "请刷新后基于最新版本重新提交"
        super().__init__(message, detail=detail)


__all__ = [
    "BadRequest",
    "Conflict",
    "ConsoleError",
    "Forbidden",
    "NotFound",
    "TooManyRequests",
    "Unauthorized",
]
