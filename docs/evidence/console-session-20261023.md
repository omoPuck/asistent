# 证据：控制台会话 Cookie + 反代 keep-alive 501（2026-10-23）

> 场景：手机端公网 HTTPS 入口（`Caddy(ECS:443) -> frps:18800 -> frpc -> 控制台 127.0.0.1:18090`）
> 已上线。本轮做两件事：①让手机"打开即用"（令牌换 HttpOnly 会话）；②修掉验证过程中
> 撞到的一个只在反代后面出现的间歇 501。
>
> 所有输出都来自**外部视角**（开发机经公网 / 板子上直连回环），不是控制台自己报的。

## 1. 会话 Cookie：公网实测（开发机 → 公网 443）

| 步骤 | 请求 | 结果 |
|---|---|---|
| 无凭据 | `GET /` | **401**（`WWW-Authenticate: Bearer realm="asistent-console"`） |
| 令牌换会话 | `POST /api/session` + Bearer | **200**，体 `{"expires_in": 604800, "ok": true}` |
| Set-Cookie 属性 | — | `Path=/; HttpOnly; SameSite=Strict; Max-Age=604800; Secure`（值已遮蔽） |
| 会话值是否出现在响应体 | — | **否**（否则 JS 就拿到了） |
| 只带 Cookie | `GET /api/overview`（无 Authorization） | **200**，返回完整 pending 快照 |
| Cookie 写操作、无标记头 | `POST /api/cases` | **403** `FORBIDDEN`（CSRF 第二道防线生效） |
| Cookie 写操作、带 `X-Asistent-Client: console` | `POST /api/cases` | **400**（空体参数校验；**不是** 403/501） |
| 退出登录 | `DELETE /api/session` | **200**，`Set-Cookie: ...; Max-Age=0` |
| 旧 Cookie 再访问 | `GET /api/overview` | **401**（服务端真吊销，不只是浏览器删 Cookie） |
| Bearer 仍可用 | `GET /api/health` + Bearer | **200**，`sessions: {"active": 0, "ttl_seconds": 604800.0}` |
| health 是否泄露会话值 | — | **否**（只有数量与 TTL） |

## 2. 间歇 501：怎么发现的、根因、修完怎么验

公网验证第 3 步第一次跑出来的是 **501**，而不是 200。响应体是 Python `http.server` 的默认错误页：

```
Error code: 501
Message: Unsupported method ('{}GET').
```

* 501 不是本项目任何代码返回的状态码 → 请求在**路由之前**被拒；
* 错误页是 `text/html;charset=utf-8`（Go 的 `http.Error` 是 `text/plain`）→ 是 Python 的 `send_error`；
* `'{}GET'` 直接点名：请求行前面多了两个字节 `{}`；
* 服务自身日志里**没有**这次请求 → `_handle` 没执行。

**根因**：`POST /api/session` 的体是 `{}`（2 字节），该端点不需要参数所以没读 body。
Caddy 复用到底座的 keep-alive 连接，残留 `{}` 与下一个请求行粘连 → `{}GET` 被当成方法名。

**修法**：`_handle` 在分发前统一消费请求体（`_consume_request_body`，仍受 `max_body_bytes` 限制）；
长度非法/超限时不硬读，标记 `close_connection` 并回 `Connection: close`。

**修后公网压测**（30 轮：每轮先一个带 `{}` 体的 `POST /api/session`，紧接着 `GET /api/overview`）：

```
200/200 出现 30 次
异常轮数: 0 / 30
```

设备日志最近 200 行内 `501` 行数：**0**。

## 3. 板子上直连回环对照（排除网络/反代因素）

```
POST /api/session            -> 200
只带 Cookie GET              -> 200
Cookie + 标记头 POST         -> 400（空体参数校验）
```

## 4. 回归测试

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `tests/console/test_sessions.py` | 18 | 纯逻辑 7（TTL/滑动/吊销/只存哈希/伪造）+ HTTP 11（Set-Cookie 属性、体不含会话值、Cookie 单独可读、写操作缺头 403、退出真吊销、带旧 Cookie 重登轮换、多设备独立、伪造/过期 401 不泄露、Bearer 兼容、失败登录不发会话、health 只报数量） |
| `tests/console/test_keepalive_body.py` | 5 | **同一条连接**上"不读体的 POST + 下一个请求"、连续两个带体 POST、超限体回 400+close、非法 Content-Length 回 400+close、空体仍按语义 400 |

全量：**924 项通过**（skipped=31）；设备门禁同样通过（`UNITTEST_RC=0`）。
另：`ASISTENT_STORE=sqlite` 全量在开发机（901，skipped=13）与设备（901，skipped=1）各跑一遍，
补上"一直只跑默认后端"的验证缺口。

## 5. 仍未做（不夸大）

* 真实手机浏览器上的"装到主屏后 7 天免登录"没有真人验证过：我只能证明 Cookie 属性、
  服务端语义与公网链路都对；**iOS 对第三方 Cookie 的处置、PWA 冷启动是否复用 Cookie
  需要你在手机上看一眼**（打开两次，第二次应直接进待办页）。
* 会话表在进程内存里：服务重启后手机会要求再粘一次令牌（这是刻意的，见 ADR-0007）。
* 24h 真实验收轮仍等你两步：U-5 邮箱授权码、U-2 网盘授权。
