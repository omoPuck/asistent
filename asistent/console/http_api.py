"""HTTP 层：`ThreadingHTTPServer` + 无第三方依赖的 JSON API 与静态文件。

这一层只做四件事：**鉴权 → 路由 → 调 `ConsoleService` → 序列化**。
它不持有任何业务状态，所以"服务重启"与"页面关闭"都不会丢东西（SPEC §14.2）。

安全相关顺序（不要调换）：

1. `_dispatch` 第一件事是 `service.require_auth(...)`；失败立刻 401 返回，
   **在此之前不调用任何 store 读取**，所以 401 不可能泄露"某个 Case 是否存在"；
2. token 只从 `Authorization: Bearer` 读；URL 里的 token 一律忽略（`TokenAuth.extract`）；
3. 日志只记方法/路径/状态/耗时/鉴权结果，**不记 query、不记 header、不记 body**；
4. 静态文件只从 `static_dir` 下读，并且做 realpath 前缀校验（防目录穿越）。
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from .auth import TokenAuth
from .errors import BadRequest, ConsoleError, Forbidden, NotFound, TooManyRequests, Unauthorized
from .service import ConsoleService
from .sessions import ConsoleSessions
from .settings import is_loopback_bind
from .throttle import AuthThrottle

#: 允许的请求方法
_METHODS = ("GET", "HEAD", "POST", "OPTIONS")
#: 静态文件目录里允许直接服务的后缀（其余一律 404，避免误暴露备份/隐藏文件）
_STATIC_SUFFIXES = (
    ".html",
    ".js",
    ".css",
    ".webmanifest",
    ".json",
    ".png",
    ".svg",
    ".ico",
    ".txt",
)


#: 会话鉴权的写操作必须带这个头；跨站表单发不出来（且没开 CORS）
_CLIENT_TAG = "console"


def _cookie_value(header: Optional[str], name: str) -> str:
    """从 `Cookie` 头里取一个值（够用即可：控制台只发一个 Cookie）。"""
    if not header or not name:
        return ""
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.strip()
    return ""


def _session_cookie_header(name: str, value: str, max_age: int, *, secure: bool) -> str:
    """拼 `Set-Cookie`：HttpOnly + SameSite=Strict（+ HTTPS 下的 Secure）。"""
    parts = [
        "%s=%s" % (name, value),
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        "Max-Age=%d" % int(max_age),
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    """单个请求的处理。实例由 `ThreadingHTTPServer` 每连接创建，**不保存跨请求状态**。"""

    server_version = "asistent-console"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ---- 基础设施 ----
    @property
    def service(self) -> ConsoleService:
        return self.server.service  # type: ignore[attr-defined]

    @property
    def static_dir(self) -> str:
        return self.server.static_dir  # type: ignore[attr-defined]

    #: 请求体缓存：`_handle` 在分发前读取一次，所有端点共用（见 _consume_request_body）
    _body_bytes: bytes = b""

    @property
    def max_body(self) -> int:
        return int(self.server.max_body_bytes)  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # pragma: no cover - 由 _log 取代
        return

    def _log(self, status: int, *, auth: str, extra: str = "") -> None:
        sink = getattr(self.server, "log_sink", None)  # type: ignore[attr-defined]
        if sink is None:
            return
        path = urllib.parse.urlsplit(self.path).path
        line = "%s %s -> %s auth=%s%s" % (self.command, path, status, auth, (" " + extra) if extra else "")
        try:
            sink(line)
        except Exception:  # 日志失败不能影响请求
            pass

    def _throttle_notice(self, lockout_seconds: float) -> None:
        """冷却触发时写一条日志。**不含令牌、不含请求体**；隧道后也分不出客户端，不假装能。"""
        sink = getattr(self.server, "log_sink", None)  # type: ignore[attr-defined]
        if sink is None:
            return
        try:
            sink("auth-throttle: 连续鉴权失败达阈值，全局冷却 %.0fs" % lockout_seconds)
        except Exception:  # 日志失败不能影响请求
            pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        # 不实现跨域：控制台只从本机页面用，开放 CORS 等于把 token 交给任意网页
        self._send_json(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": "不允许跨域预检"}})

    def _handle(self, method: str) -> None:
        started = time.time()
        status = 500
        auth_state = "denied"
        self._body_bytes = b""
        try:
            # 先消费请求体，再做别的事：这是 keep-alive 的正确性要求，不是洁癖。
            # 反代（Caddy -> frp -> 本服务）会复用到底座的连接；如果某个端点收了 POST
            # 却没把 body 读干净，残留字节会和下一个请求的请求行粘在一起，Python 的
            # http.server 就会把 "{}GET" 当成方法名回 501 —— 现场表现为"手机端偶尔 501，
            # 重试又好了"，极难从客户端看出来。
            self._body_bytes = self._consume_request_body()
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            # 冷却检查放在最前面：冷却期间连令牌都不比对，攻击者拿不到额外信息
            retry_after = self.service.auth_gate()
            if retry_after is not None:
                auth_state = "throttled"
                raise TooManyRequests(retry_after)
            cookie = _cookie_value(self.headers.get("Cookie"), self._session_cookie_name)
            auth_state = self._authenticate(cookie)
            # Cookie 会自动随请求发送，所以"会话鉴权 + 写操作"必须再要一个自定义头：
            # 跨站表单发不出自定义头（也没开 CORS），This 是 SameSite=Strict 之外的第二道 CSRF 防线。
            if auth_state == "session" and method not in ("GET", "HEAD"):
                if (self.headers.get("X-Asistent-Client") or "") != _CLIENT_TAG:
                    raise Forbidden("会话登录的写操作缺少客户端标记头")
            if path.startswith("/api/"):
                status = self._dispatch_api(method, path, query, parsed.query)
            elif method in ("GET", "HEAD"):
                status = self._serve_static(path, head_only=(method == "HEAD"))
            else:
                status = self._send_json(404, _error("NOT_FOUND", "接口不存在"))
        except ConsoleError as exc:
            extra = {"Retry-After": str(exc.retry_after)} if isinstance(exc, TooManyRequests) else None
            if self.close_connection:
                extra = dict(extra or {})
                extra["Connection"] = "close"
            status = self._send_json(exc.status, exc.to_payload(), extra_headers=extra)
        except BrokenPipeError:  # pragma: no cover - 客户端提前断开
            status = 499
        except Exception as exc:  # 兜底：任何未预期异常都要变成结构化 500，且不泄露堆栈
            status = self._send_json(
                500, _error("INTERNAL_ERROR", "服务内部错误（%s）" % type(exc).__name__)
            )
        finally:
            self._log(status, auth=auth_state, extra="%.1fms" % ((time.time() - started) * 1000.0))

    # ---- 鉴权（Bearer 令牌 或 HttpOnly 会话 Cookie） ----
    @property
    def _session_cookie_name(self) -> str:
        return str(getattr(self.server, "session_cookie_name", "asistent_session"))

    def _authenticate(self, cookie: Optional[str]) -> str:
        """返回鉴权状态：`ok`（Bearer 令牌）/ `session`（会话 Cookie）/ 抛 Unauthorized。

        只有"提交了凭据但被拒"才算一次失败：没带凭据的普通 401（例如首次打开页面）
        不该计入爆破节流，否则正常刷新也会把主人锁在门外。
        """
        token = TokenAuth.extract(self.headers.get("Authorization"), query_value=_first(self._query(), "token"))
        if token is not None:
            try:
                self.service.require_auth(token)
            except Unauthorized:
                self._note_failure()
                raise
            self.service.note_auth_success()
            return "ok"
        if cookie and self.service.session_valid(cookie):
            self.service.note_auth_success()
            return "session"
        if cookie:
            # 失效/伪造的会话值：按一次失败计，但**不**提示"会话无效 vs 令牌错"的区别
            self._note_failure()
        raise Unauthorized()

    def _note_failure(self) -> None:
        locked = self.service.note_auth_failure()
        if locked is not None:
            self._throttle_notice(locked)

    def _query(self) -> Dict[str, List[str]]:
        parsed = urllib.parse.urlsplit(self.path)
        return urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    def _open_session(self) -> int:
        """用已鉴权的请求换一个会话 Cookie；响应体里不放会话值。"""
        previous = _cookie_value(self.headers.get("Cookie"), self._session_cookie_name)
        if previous:
            self.service.revoke_session(previous)  # 轮换：登录时旧会话立即作废
        value = self.service.open_session()
        ttl = int(getattr(self.server, "session_ttl_seconds", 604800))
        return self._send_json(
            200,
            {"ok": True, "expires_in": ttl},
            extra_headers={
                "Set-Cookie": _session_cookie_header(
                    self._session_cookie_name, value, ttl,
                    secure=bool(getattr(self.server, "session_cookie_secure", True)),
                )
            },
        )

    def _close_session(self) -> int:
        """退出登录：吊销会话并把 Cookie 立刻清掉。"""
        previous = _cookie_value(self.headers.get("Cookie"), self._session_cookie_name)
        self.service.revoke_session(previous)
        return self._send_json(
            200,
            {"ok": True},
            extra_headers={
                "Set-Cookie": _session_cookie_header(
                    self._session_cookie_name, "", 0,
                    secure=bool(getattr(self.server, "session_cookie_secure", True)),
                )
            },
        )

    # ---- 路由 ----
    def _dispatch_api(self, method: str, path: str, query: Dict[str, List[str]], raw_query: str) -> int:
        service = self.service
        parts = [item for item in path.split("/") if item]
        # parts[0] == "api"
        route = parts[1:]
        if not route:
            raise NotFound("接口不存在")

        if route == ["session"]:
            if method == "POST":
                return self._open_session()
            if method == "DELETE":
                return self._close_session()
            raise BadRequest("会话接口只支持 POST（登录）与 DELETE（退出）")

        if route == ["health"] and method in ("GET", "HEAD"):
            return self._send_json(200, service.health())
        if route == ["overview"] and method in ("GET", "HEAD"):
            etag = self._if_none_match()
            payload = service.overview(since=_first(query, "since") or "")
            if etag and etag == payload.get("etag"):
                return self._send_json(304, None, etag=str(payload.get("etag")), empty=True)
            return self._send_json(200, payload, etag=str(payload.get("etag")))
        if route == ["changes"] and method in ("GET", "HEAD"):
            payload = service.changes(since=_first(query, "since") or "")
            return self._send_json(200, payload, etag=str(payload.get("etag")))

        if route[:1] == ["cases"]:
            if len(route) == 1:
                if method == "POST":
                    return self._send_json(201, service.create_case(self._read_json()))
                return self._send_json(
                    200,
                    service.list_cases(
                        state=_first(query, "state") or "",
                        include_closed=_truthy(_first(query, "include_closed")),
                        limit=_int_query(query, "limit", 50),
                        since=_first(query, "since") or "",
                    ),
                )
            if len(route) == 2:
                return self._send_json(
                    200, service.get_case(route[1], include=_first(query, "include") or "")
                )
            raise NotFound("接口不存在")

        if route[:1] == ["questions"]:
            if len(route) == 1:
                return self._send_json(
                    200,
                    service.list_questions(
                        state=_query_or_default(query, "state", "OPEN"), limit=_int_query(query, "limit", 100)
                    ),
                )
            if len(route) == 2:
                return self._send_json(200, service.get_question(route[1]))
            if len(route) == 3 and route[2] == "answer" and method == "POST":
                return self._send_json(200, service.submit_answer(route[1], self._read_json()))
            raise NotFound("接口不存在")

        if route[:1] == ["drafts"]:
            if len(route) == 1:
                return self._send_json(
                    200,
                    service.list_drafts(case_id=_first(query, "case_id") or "", limit=_int_query(query, "limit", 100)),
                )
            if len(route) == 2:
                return self._send_json(200, service.get_draft(route[1]))
            if len(route) == 3:
                if route[2] == "edit" and method == "POST":
                    return self._send_json(200, service.edit_draft(route[1], self._read_json()))
                if route[2] == "restore" and method == "POST":
                    return self._send_json(200, service.restore_draft_version(route[1], self._read_json()))
                if route[2] == "diff":
                    return self._send_json(
                        200,
                        service.diff_draft(
                            route[1],
                            from_no=_int_query(query, "from", 1),
                            to_no=_int_query(query, "to", 1),
                        ),
                    )
            raise NotFound("接口不存在")

        if route[:1] == ["approvals"]:
            if len(route) == 1 and method == "POST":
                return self._send_json(201, service.submit_approval(self._read_json()))
            if len(route) == 1:
                return self._send_json(
                    200,
                    service.list_approvals(
                        case_id=_first(query, "case_id") or "",
                        state=_first(query, "state") or "",
                        limit=_int_query(query, "limit", 100),
                    ),
                )
            if len(route) == 2:
                return self._send_json(200, service.get_approval(route[1]))
            if len(route) == 3 and route[2] == "revoke" and method == "POST":
                return self._send_json(200, service.revoke_approval(route[1], self._read_json()))
            raise NotFound("接口不存在")

        if route[:1] == ["artifacts"]:
            if len(route) == 1:
                return self._send_json(
                    200,
                    service.list_artifacts(
                        case_id=_first(query, "case_id") or "", limit=_int_query(query, "limit", 100)
                    ),
                )
            if len(route) == 2:
                return self._send_json(200, service.get_artifact(route[1]))
            if len(route) == 3 and route[2] == "content":
                data, row = service.read_artifact(route[1])
                return self._send_bytes(
                    200,
                    data,
                    content_type=str(row.get("content_type") or "application/octet-stream"),
                    extra_headers={
                        "Content-Disposition": _disposition(str(row.get("name") or "artifact")),
                        "X-Artifact-Id": str(row.get("id") or ""),
                        "X-Artifact-Sha256": str(row.get("sha256") or ""),
                    },
                )
            raise NotFound("接口不存在")

        raise NotFound("接口不存在")

    # ---- 静态文件 / PWA ----
    def _serve_static(self, path: str, *, head_only: bool) -> int:
        relative = path.lstrip("/") or "index.html"
        if relative.endswith("/"):
            relative += "index.html"
        candidate = os.path.realpath(os.path.join(self.static_dir, relative))
        root = os.path.realpath(self.static_dir)
        if not (candidate == root or candidate.startswith(root + os.sep)):
            return self._send_json(404, _error("NOT_FOUND", "资源不存在"))
        if not os.path.isfile(candidate) or os.path.splitext(candidate)[1].lower() not in _STATIC_SUFFIXES:
            return self._send_json(404, _error("NOT_FOUND", "资源不存在"))
        with open(candidate, "rb") as handle:
            data = handle.read()
        content_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
        if candidate.endswith(".webmanifest"):
            content_type = "application/manifest+json"
        # 前端是可缓存的静态资源，但 API 永不缓存
        return self._send_bytes(
            200,
            b"" if head_only else data,
            content_type=content_type,
            extra_headers={"Cache-Control": "no-cache"},
            content_length=len(data),
        )

    # ---- 请求/响应小工具 ----
    def _consume_request_body(self) -> bytes:
        """把请求体一次读干净（keep-alive 下必须做，否则残字节会污染下一个请求）。

        长度非法/超限时不硬读（避免被超大 body 拖死），改为标记关闭连接后返回 400：
        宁可让反代重开一条连接，也不能让残留字节和一个新请求粘在一起。
        """
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError:
            self.close_connection = True
            raise BadRequest("Content-Length 不是整数")
        if length < 0:
            self.close_connection = True
            raise BadRequest("Content-Length 不能为负")
        if length > self.max_body:
            self.close_connection = True
            raise BadRequest("请求体过大（上限 %d 字节）" % self.max_body)
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _read_json(self) -> Dict[str, Any]:
        raw = self._body_bytes or b""
        if not raw:
            raise BadRequest("请求体为空（需要 JSON 对象）")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BadRequest("请求体不是合法 JSON（UTF-8）")
        if not isinstance(payload, dict):
            raise BadRequest("请求体必须是 JSON 对象")
        return payload

    def _if_none_match(self) -> str:
        return (self.headers.get("If-None-Match") or "").strip()

    def _send_json(
        self,
        status: int,
        payload: Optional[Dict[str, Any]],
        *,
        etag: str = "",
        empty: bool = False,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> int:
        body = b"" if empty else json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
        if etag:
            headers["ETag"] = etag if etag.startswith("W/") else 'W/"%s"' % etag
        headers.update(extra_headers or {})
        return self._send_bytes(status, body, content_type=None, extra_headers=headers, content_length=len(body))

    def _send_bytes(
        self,
        status: int,
        data: bytes,
        *,
        content_type: Optional[str],
        extra_headers: Optional[Dict[str, str]] = None,
        content_length: Optional[int] = None,
    ) -> int:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        if status == 401:
            # 明确告诉客户端"要用 Bearer"，但仍然不透露任何对象信息
            self.send_header("WWW-Authenticate", 'Bearer realm="asistent-console"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(data) if content_length is None else content_length))
        self.end_headers()
        if data:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                return status
        return status


class ConsoleHTTPServer(ThreadingHTTPServer):
    """带 daemon 线程与可注入日志的多线程 HTTP 服务。

    `daemon_threads = True`：进程退出时不被残留连接卡住（部署脚本 restart 立刻生效）。
    `allow_reuse_address = True`：重启后端口立刻可用，避免"要等 TIME_WAIT"。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        settings: Any,
        service: ConsoleService,
        *,
        log_sink: Optional[Callable[[str], None]] = None,
        static_dir: Optional[str] = None,
    ) -> None:
        self.settings = settings
        self.service = service
        self.static_dir = static_dir or settings.static_dir or _default_static_dir()
        self.max_body_bytes = int(getattr(settings, "max_body_bytes", 262144))
        self.log_sink = log_sink
        self._bound_started_at = time.time()
        # handler 通过 self.server 读这些设置（BaseHTTPRequestHandler 的惯例）
        self.session_cookie_name = str(getattr(settings, "session_cookie_name", "asistent_session"))
        self.session_ttl_seconds = float(getattr(settings, "session_ttl_seconds", 7 * 24 * 3600.0))
        self.session_cookie_secure = bool(getattr(settings, "session_cookie_secure", True))
        super().__init__((settings.bind, int(settings.port)), ConsoleRequestHandler)  # type: ignore[arg-type]

    @property
    def bound_port(self) -> int:
        return int(self.server_address[1])

    @property
    def bound_host(self) -> str:
        return str(self.server_address[0])

    def describe(self) -> Dict[str, Any]:
        return {
            "bind": self.bound_host,
            "port": self.bound_port,
            "static_dir": self.static_dir,
            "started_at": self._bound_started_at,
        }


def _default_static_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def build_server(
    settings: Any,
    service: Optional[ConsoleService] = None,
    *,
    log_sink: Optional[Callable[[str], None]] = None,
) -> ConsoleHTTPServer:
    """按设置建服务：`port=0` 时由系统分配端口（测试用），实际端口在 `bound_port`。"""
    # `allow_remote` 必须真的管用：它以前只是被解析、被写进配置快照，没有任何地方执行。
    # 结果就是——有人把 bind 设成 0.0.0.0、allow_remote 保持 false，会以为自己被保护了，
    # 实际把"令牌 + 全部待办"以明文暴露在整个网络上。控制台没有 TLS，这个缺口不能留。
    bind = str(getattr(settings, "bind", "") or "")
    if not is_loopback_bind(bind) and not bool(getattr(settings, "allow_remote", False)):
        raise ValueError(
            "console.bind=%r 不是回环地址，但 console.allow_remote=false：拒绝启动控制台。"
            "控制台自身没有 TLS，对外监听等于把令牌与待办明文发到网络上；"
            "确需如此请显式设置 console.allow_remote=true，"
            "更好的做法是让 frp/Caddy 反代到 127.0.0.1（见 ADR-0006）。" % bind
        )
    if service is None:
        token = getattr(settings, "token", "")
        service = ConsoleService(
            settings.store,
            TokenAuth(token),
            server_name=getattr(settings, "server_name", "asistent-console"),
            artifact_roots=getattr(settings, "artifact_roots", ()) or (),
            throttle=AuthThrottle(
                max_failures=int(getattr(settings, "max_auth_failures", 20)),
                window_seconds=float(getattr(settings, "auth_window_seconds", 600.0)),
                base_lockout_seconds=float(getattr(settings, "auth_lockout_seconds", 60.0)),
                max_lockout_seconds=float(getattr(settings, "auth_max_lockout_seconds", 300.0)),
            ),
            sessions=ConsoleSessions(
                ttl_seconds=float(getattr(settings, "session_ttl_seconds", 7 * 24 * 3600.0)),
            ),
        )
    return ConsoleHTTPServer(settings, service, log_sink=log_sink)


def serve_forever(server: ConsoleHTTPServer) -> None:  # pragma: no cover - 部署入口
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass


def _error(code: str, message: str) -> Dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _first(query: Dict[str, List[str]], key: str) -> Optional[str]:
    values = query.get(key)
    return values[0] if values else None


def _query_or_default(query: Dict[str, List[str]], key: str, default: str) -> str:
    """区分"参数没给"与"参数给了但为空"：`?state=` 是**显式要求不过滤**。"""
    if key not in query:
        return default
    return _first(query, key) or ""


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _int_query(query: Dict[str, List[str]], key: str, default: int) -> int:
    raw = _first(query, key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise BadRequest("%s 必须是整数" % key)


def _disposition(name: str) -> str:
    """只保留文件名，避免把服务端路径带进响应头。"""
    safe = os.path.basename(name).replace('"', "")
    return 'attachment; filename="%s"' % safe


__all__ = ["ConsoleHTTPServer", "ConsoleRequestHandler", "build_server", "serve_forever"]
