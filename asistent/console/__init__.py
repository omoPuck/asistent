"""手机控制台：设备本地 Web/PWA + token 鉴权（MVP-4）。

设计要点（`docs/mvp/mvp-4.md` 有完整说明）：

* **状态只来自持久化存储**：每个请求现场 `store.find/get/audit_trail`，进程里不缓存任何
  会话/待办列表，所以服务重启、页面关闭、手机断网重连都不影响可见性（SPEC §9.4、§14.2）；
* **鉴权先行**：无 token / 错 token 在**任何对象查询之前**就被拒绝，响应体只有一条固定
  文案，不含"某个 Case 是否存在"这类信息（SPEC §13.4）；
* **编辑带版本**：写操作一律要求 `expected_revision`（或草稿的 `version_no`），
  旧版本写入 → 409 + 可读原因，绝不静默覆盖（SPEC §21.1）；
* **审批走独立通道**：`POST /api/approvals` + 显式 `version_no` + 显式确认字段；
  普通请求体里的"同意/发送"文本不会被当成批准（SPEC §21.3）。
"""

from __future__ import annotations

from .auth import AuthError, TokenAuth, load_token
from .errors import ConsoleError, BadRequest, Conflict, Forbidden, NotFound, Unauthorized
from .http_api import ConsoleHTTPServer, build_server, serve_forever
from .service import ConsoleService
from .settings import ConsoleSettings, default_token_path

__all__ = [
    "AuthError",
    "BadRequest",
    "Conflict",
    "ConsoleError",
    "ConsoleHTTPServer",
    "ConsoleService",
    "ConsoleSettings",
    "Forbidden",
    "NotFound",
    "TokenAuth",
    "Unauthorized",
    "build_server",
    "default_token_path",
    "load_token",
    "serve_forever",
]
