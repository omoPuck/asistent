# ADR-0007：控制台用 HttpOnly 会话 Cookie 记住这台设备

* 状态：已采纳（2026-10-23）
* 关联：ADR-0006（手机经 frp + Caddy 访问）、`docs/mvp/mvp-4.md §1.3`、`asistent/console/sessions.py`

## 背景

控制台的鉴权从 MVP-4 起是"`Authorization: Bearer <token>` + 令牌只放内存"。
这个取舍在浏览器里是安全的（令牌不进 URL、不进 localStorage、不进 SW 缓存），
但它有个在手机上很致命的使用代价：

**PWA 从桌面图标启动 = 一次新的页面加载**，于是每次打开都要重新粘贴 64 位令牌。
2026-10-23 手机端公网 HTTPS 入口打通后，这成了实际使用中的第一个摩擦点——
用户不是"打不开"，而是"每次都要翻设备上的令牌"，等于把 24h 助手降级成偶发工具。

需要找一个方案，同时满足：

1. 令牌本身不能落任何浏览器存储（localStorage/sessionStorage 都会被 XSS 读走）；
2. 打开即可用，不需要每次手工粘贴；
3. 手机丢了，能单方面让"这台设备"失效，而不必换掉全局令牌；
4. 仍然是纯标准库、无第三方依赖（ADR-0001）。

## 决策

**令牌只用来换取一次会话；会话以 `HttpOnly` Cookie 承载。**

* `POST /api/session`（需已鉴权）→ `Set-Cookie: asistent_session=<随机值>; Path=/; HttpOnly; SameSite=Strict; Max-Age=7d; Secure`
* 之后浏览器自动带 Cookie；服务端接受 `Bearer` 或会话 Cookie 两种凭据。
* 服务端内存里只保存会话值的 **SHA-256**（内存泄露/日志误写都拿不到可用凭据）。
* TTL 默认 7 天，**滑动续期**：日常使用不过期，长期不用自然失效。
* `DELETE /api/session` 真正吊销（不是只删浏览器 Cookie）；服务重启 = 全部会话失效。
* 多设备各自独立会话：登录不踢别的设备；带旧 Cookie 再登录只轮换自己那份。
* **CSRF**：Cookie 鉴权下，非 GET 请求必须带 `X-Asistent-Client: console`
  （跨站表单发不出自定义头；服务端不开 CORS），叠加 `SameSite=Strict`。
* 写操作仍然要求显式结构化请求（审批三要素 `confirm/version_no/hash` 不变），
  会话只是"你是谁"，不是"你同意了什么"。

## 被否决的方案

| 方案 | 否决理由 |
|---|---|
| 令牌存 localStorage | 任何注入脚本都能读走长期令牌；等于把门禁交给 XSS，且没有单设备吊销能力 |
| 令牌存 sessionStorage | PWA 重启即新会话，解决不了"每次打开要粘"的问题；同样可被 XSS 读 |
| 把令牌放进 URL（`?token=`） | 进浏览器历史 / 代理日志 / Referer，服务端原本就故意忽略 |
| 换成 HTTP Basic | 浏览器会记住但把明文口令编码后反复发送；无法吊销单个设备，也躲不开 XSS 读取 |
| 每条请求都用令牌、由客户端自己持久化 | 同 localStorage：把长期凭据留在 JS 可读的地方 |
| 上第三方会话库 | 违反 ADR-0001（纯标准库、设备无 pip） |

## 代价与边界

* 会话表在**进程内存**里：服务重启后手机上要重新粘一次令牌（7 天一次都不到，
  因为只有重启才丢）。这是刻意的：把会话落到磁盘就要处理"磁盘上的会话文件被拷走"。
* Cookie 的 `Secure` 属性要求 HTTPS；本机 `127.0.0.1` 调试时浏览器仍把它当安全上下文，
  `console.session_cookie_secure=false` 可关闭（仅限调试）。
* 控制台仍然是单进程单令牌模型，没有"多个用户"的概念——这是个人助手的边界，不是缺陷。
