# 证据：控制台鉴权失败节流（设备实测）

- 日期：2026-10-23
- 设备：board（RK3588，Python 3.10.12），服务 `asistent` 以 systemd 用户单元运行
- 代码：`asistent/console/throttle.py`（本次新增）、`asistent/console/http_api.py` 接线
- 目的：控制台经 frp + Caddy 暴露到公网后，令牌是唯一门禁。**没有失败上限 = 允许无限爆破**，
  所以加"连续失败 → 全局冷却"。这份文档记录的是设备上的原始现象，不是开发机推断。

## 1. 为什么是全局冷却而不是按 IP

手机走 frp 的 TCP 隧道进来，控制台看到的对端恒为 `127.0.0.1`——真实客户端 IP 在隧道层就丢了
（frp 的 tcp 代理不传 `X-Forwarded-For`，除非另开 PROXY protocol）。按 IP 计数会把所有客户端
算进同一个桶，只是**看起来**精确。因此实现为全局计数，并把这个代价写进 [ADR-0006](../adr/0006-phone-access-via-frp.md)：
攻击者可在冷却期内把主人挡在门外最多 5 分钟，改配置或重启即可解除。

## 2. 设备实测：爆破 21 次

```
第 19 次: 401
第 20 次: 401
第 21 次: 429          <- 达到阈值(20)后进入冷却
正确令牌（冷却中）: 429   <- 冷却期间连正确令牌也拦住，且不做令牌比对
```

冷却响应（`curl -D -` 头）：

```
HTTP/1.1 429 Too Many Requests
Retry-After: 60
{"error": {"code": "AUTH_THROTTLED", "detail": {"retry_after_seconds": 60},
           "message": "鉴权失败次数过多，请稍后再试"}}
```

61 秒后再测：正确令牌 `200`、错误令牌回到 `401`，`/api/health` 的 `auth` 块回到全零：

```
{'failures_in_window': 0, 'lockout_level': 0, 'lockout_seconds_remaining': 0.0,
 'max_failures': 20, 'window_seconds': 600.0}
```

## 3. 顺带发现的真缺陷：日志没接上（已修）

第一次在设备上验证时，`service.log` 里**一条请求日志都没有**。根因不在节流，而在
`ServiceRuntime.start_console()` 调 `build_server()` 时**漏传 `log_sink`**——部署环境里所有
HTTP 请求日志都是空操作，包括 401、429 和"开始全局冷却"这条安全事件。

危险之处：`tests/console/**` 当时全绿，因为测试 helpers 自己传了 `log_sink`。
**单测覆盖了控制台内部，却没覆盖"谁把 sink 接上去"**。

修复后同一轮实测（`service.log` 新增行）：

```
GET /api/health -> 401 auth=denied 0.6ms        x20
auth-throttle: 连续鉴权失败达阈值，全局冷却 60s
GET /api/health -> 429 auth=throttled 0.7ms     x2
日志里是否出现令牌: 0
```

并新增 `tests/service/test_service_console_logging.py`，从 `ServiceRuntime` 这一层断言
"401 行 / auth-throttle 行 / 429 行真的落进日志"。这 3 项在修复前会失败，属于回归保护。

## 4. 第二个发现：设备上才暴露的测试竞态

新增的日志测试在开发机 5/5 通过，设备上红 2 项：抓到空日志、抓到 `-> 401` 却抓不到 `-> 429`。
原因不是日志没接上，而是**测试在和 HTTP 线程赛跑**——请求日志写在 handler 的 `finally` 里，
客户端拿到状态码时 append 可能还没发生；RK3588 比开发机慢，这个窗口就撞上了。

改法是有界轮询 `_await_line()`（最长 5s，命中即返回）而不是裸 `sleep`，失败时把已抓到的
日志原样打进断言消息。同一模块连跑 5 次全绿。

## 5. 测试与门禁

| 项目 | 结果 |
|---|---|
| `tests/console/test_auth_throttle.py` | 12 项通过（纯逻辑用假时钟，不 sleep） |
| `tests/service/test_service_console_logging.py` | 3 项通过（设备上 5 次重复全绿） |
| 控制台测试 | 147 项 |
| 全量（开发机） | 893 项通过（skipped=31） |
| 全量（设备，部署门禁） | 893 项通过（skipped=20） |

## 6. 仍未做（别把这份证据读成"安全了"）

- 双因素、按手机出口 IP 的白名单（后者需要 PROXY protocol 才有真实 IP）；
- Caddy 的 HTTPS 入口还没生效——**阿里云安全组 80/443 尚未放行**，见 [STATUS](../STATUS.md) 与 ADR-0006。
  在放行并实测 `https://<域名>/` 之前，不得宣称"手机随时可用"。
