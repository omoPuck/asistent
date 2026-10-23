# ADR-0006：手机可达性改走 frp 内网穿透（自有公网服务器）

- 状态：已接受（2026-10-23）
- 取代：ADR-0005（Tailscale tailnet）——该方案不再实施，保留备查
- 相关：ADR-0002（开发/部署拓扑）、ADR-0003（外部集成与待确认）、`scripts/frp-onboard.sh`

## 背景（实测事实）

1. 板子在**公司内网** `192.0.2.0/24`（`192.0.2.10`），手机在**移动数据**上，两者没有直连路径。
   手机与板子不在同一 WiFi，且公司网络不提供任何入站通道——这是 ADR-0005 的出发点，结论仍然成立。
2. 用户已有一台**阿里云 ECS**（`203.0.113.50`，Ubuntu 24.04）跑着 `frps v0.71.1`，
   配置里 `bindPort = 7000`、`transport.tls.force = true`、
   `allowPorts` 只放行 `20000-20009` 与 `18800`，其中注释写明 **18800 预留给"个人助手 Web 控制台"**。
3. 板子出网到 `203.0.113.50:7000` **可连**（实测），因此不需要动公司网络策略。
4. 用户明确要求用 frp（而不是在手机/板子装 Tailscale 客户端）。这属于用户的部署决定，
   且自有服务器比引入第三方 mesh 账号更符合"内网资产不外借"的偏好。

## 决策

板子跑 **frpc**（`systemd --user` 单元），只**出站**连 frps，把公网 `18800` 映射到板子
`127.0.0.1:18090`：

```
手机(移动数据) ──HTTPS?──▶ frps 203.0.113.50:18800 ──隧道(TLS+token)──▶ 板子 frpc
                                                                     └─▶ 127.0.0.1:18090 控制台
```

配套改动（与穿透直接相关，不是顺手改）：

* **控制台绑定从 `192.0.2.10` 改为 `127.0.0.1`**。两个理由：
  1. frpc 就在同机，转发到回环即可；
  2. 板子地址是 **DHCP** 租来的，服务可能先于网卡地址就绪而 `bind()` 失败
     （2026-10-12 真实发生，控制台静默缺席 11 天）。绑回环后不再依赖租约时序，
     并把"办公室同一 WiFi 上任何人都能敲控制台"这条多余暴露面一起去掉。
* **控制台启动失败改为可重试**（`CONSOLE_RETRY_SECONDS`，每轮开头补一次机会）：
  绑定失败不该等于"手机端永久消失"，失败要留痕、恢复也要留痕。

## 后果

**获得**：手机（以及任何外网设备）可通过公网入口访问控制台；板子不开放任何入站端口；
暴露面收敛到 frps 的一个端口（`allowPorts` 在服务端也做了白名单）；不需要第三方 mesh 账号。

**代价 / 未决**：

1. ~~**当前是 HTTP**~~ **已解决（2026-10-23 15:37 实测）**：ECS 上 Caddy 2.6.2 已启用并开机自启，
   Let's Encrypt 通过 **TLS-ALPN-01** 签发成功（日志 `certificate obtained successfully`，
   issuer `Let's Encrypt YE1`，SAN 仅 `asistent.example.com`，有效期至 2027-01-21），
   反代链路：手机 → `https://asistent.example.com/`（443）→ Caddy → `127.0.0.1:18800`
   （frps 出口）→ frpc → 板子 `127.0.0.1:18090`。80 端口 308 跳 HTTPS；HSTS 1 年；
   `Server` 头已隐藏。**Service Worker 现在满足安全上下文**，PWA 离线壳与"添加到主屏幕"真实可用。
   续期由 Caddy 自动完成（需要 80/443 长期可入；`systemctl is-enabled caddy` = enabled）。
   注意：公网**只需** 80/443；frps 的 18800 从外网实测是 TIMEOUT（被挡），这是想要的——
   HTTPS 入口不需要它。
2. **鉴权只有控制台令牌**（`Authorization: Bearer`，令牌只在内存里、每次重粘）。公网可达后，
   令牌就是唯一那道门。**已实施第一层防护：鉴权失败节流**（`asistent/console/throttle.py`）——
   窗口内连续失败达阈值即全局冷却，冷却时长指数增长、上限 5 分钟，成功一次清零。
   为什么是**全局**而不是按 IP：frp 走 TCP 隧道，控制台看到的对端恒为 `127.0.0.1`，
   真实客户端 IP 在隧道层就丢了，按 IP 计数只是假精度。代价是攻击者可在冷却期内把主人
   挡在门外最多 5 分钟，改配置或重启即可解除——对单人自用场景这个交换是值得的。
   仍未做：可选的双因素、手机出口 IP 白名单（后者需要 PROXY protocol 才有真实 IP）。
   **可观测性**：控制台请求日志（含 `auth=denied`、`-> 429`、`auth-throttle: …`）已接入
   `~/asistent-var/logs/service.log`——这里曾经断线（`runtime` 调 `build_server` 时漏传
   `log_sink`），导致设备上"有人在爆破我"完全看不见，而单测因 helpers 自己传了 sink 仍全绿；
   现已由 `tests/service/test_service_console_logging.py` 从服务层堵住这个缝。
3. **依赖自有服务器可用性**：ECS 到期/欠费/被攻击都会让手机端不可达。这不是"更可靠"的方案，
   是"可控"的方案——可控但单点。
4. **frps 的 token 与 frpc 配置都在仓库外**（`~/asistent-var/config/frp-token`、`frpc.toml`，均 600）。
   仓库里只有生成它们的脚本（`scripts/frp-onboard.sh`），任何一步都不打印凭据。

## 被否决/暂缓的备选

| 备选 | 为什么没选 |
|---|---|
| Tailscale（ADR-0005） | 手机与板子都要装客户端并登录同一账号；用户已经有 frp 基础设施，明确要求用 frp。保留为"不想依赖自有服务器时"的备选 |
| cloudflared 快速隧道（`dsh-pocket` 的公网模式） | 把入口交给 Cloudflare 边缘、依赖第三方域名；且 `dsh-pocket` 暴露的是**能执行代码的 DSH 界面**，风险面完全不同。仅用于"手机上操控开发机 DSH"，不用于本控制台 |
| frps 的 vhost HTTPS（`vhostHTTPSPort` + `https2http`） | 需要把证书放到板子并在 frpc 侧做 TLS 终止，比"ECS 上 Caddy 反代"更难维护；且证书续期仍要回到 ECS |
| 让手机进公司内网（VPN/堡垒机） | 手机侧无法安装公司客户端；且把个人设备塞进内网方向相反 |

## 验证方式（可重跑）

```bash
# 板子上：看 frpc 现状
bash ~/code/asistent/scripts/frp-onboard.sh --status

# 从任意外网机器：控制台应答（401 = 通了，要求令牌）
curl -s -o /dev/null -w '%{http_code}\n' http://203.0.113.50:18800/
```

2026-10-23 实测：`frps` 面板显示 `asistent-console` 在线、`tcp proxy listen port [18800]`；
阿里云本机 `curl 127.0.0.1:18800` 返回 **401**（控制台在应答）。
