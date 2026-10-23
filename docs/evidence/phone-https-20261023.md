# 证据：手机端公网 HTTPS 入口（frp + Caddy + Let's Encrypt）实测

- 日期：2026-10-23 15:37（Asia/Shanghai）
- 目标：让手机（Android/iOS）从公网真正**可达可用**，且是安全上下文（HTTPS），使 Service Worker /
  "添加到主屏幕" / 离线壳成立——而不只是"配置写好了"。
- 链路：手机 → `https://asistent.example.com/`（ECS:443）→ Caddy → `127.0.0.1:18800`
  （frps 出口）→ frpc 隧道 → 板子 `127.0.0.1:18090`

## 1. 证书（Let's Encrypt，TLS-ALPN-01）

ECS 上 Caddy 2.6.2，`systemctl enable --now caddy` 后自动申请。日志（原样）：

```
tls.obtain  msg="obtaining certificate" identifier="asistent.example.com"
tls         msg="served key authentication certificate" challenge="tls-alpn-01"   x N
tls.obtain  msg="certificate obtained successfully" identifier="asistent.example.com"
```

严格校验（**开发机**，外部视角 1）：

```
校验结果: 通过（系统信任链 + 主机名匹配）
协议/套件: TLSv1.3 TLS_AES_128_GCM_SHA256
主体: {'commonName': 'asistent.example.com'}
签发者: {'countryName': 'US', 'organizationName': "Let's Encrypt", 'commonName': 'YE1'}
SAN: ['asistent.example.com']
有效期至: Jan 21 06:38:56 2027 GMT
```

严格校验（**板子**，外部视角 2，`openssl s_client -verify_return_error`）：

```
subject=CN = asistent.example.com
issuer=C = US, O = Let's Encrypt, CN = YE1
Verify return code: 0 (ok)
Protocol  : TLSv1.3
```

## 2. 反向代理是否真的到了控制台（不是随便一个 200 页面）

| 检查 | 结果 |
|---|---|
| `GET /`（无令牌） | **401** + `WWW-Authenticate: Bearer realm="asistent-console"` |
| 响应体 | `{"error": {"code": "UNAUTHORIZED", "message": "需要有效的控制台令牌"}}` |
| `GET /`（错令牌，走公网） | **401**（鉴权在公网入口下仍生效） |
| `GET /`（带令牌） | **200** |
| `GET /styles.css`（带令牌） | 命中暖白新色值 `#736657`（证明拿到的是当前代码，不是旧缓存） |
| `GET /`（带令牌）HTML | 命中 `role="tablist"`（无障碍标记在位） |
| `GET /api/overview`（带令牌） | **200**，返回 `etag`/`next_since`/`pending` 真实结构 |
| `GET /manifest.webmanifest` | 200，`content-type: application/manifest+json` |
| `GET /sw.js` | 200，`content-type: text/javascript` |
| `HTTP :80` | **308** → `https://asistent.example.com/` |
| HSTS | `max-age=31536000` |
| `Server` 头 | 已隐藏（Caddyfile 里 `-Server`） |

## 3. 手机 UA 实测

| UA | 结果 |
|---|---|
| iPhone Safari（iOS 17.5，Mobile/15E148） | HTTP **200** |
| Android Chrome（Pixel 8，Android 14，Chrome 126 Mobile） | HTTP **200** |

PWA 安装条件（manifest 实际内容）：`display=standalone`、`start_url=/`、`scope=/`、
`theme_color/background_color=#faf6ef`、图标 192/512 + maskable 512、`lang=zh-CN`。
在 HTTPS 下 Service Worker 满足安全上下文，可注册。

## 4. 端口事实（为什么公网只需要 80/443）

外部探测（`connect()` 结果分类，**refused ≠ blocked**，见 [ops-traps §6](../env/ops-traps.md)）：

| 端口 | 结果 | 含义 |
|---|---|---|
| 80 / 443 | 启动 Caddy 前 `REFUSED`、之后 `CONNECTED` | 安全组**早已放行**，之前只是没进程监听 |
| 18800 | `TIMEOUT` | 被挡（正确：入口不需要它，Caddy 在 ECS 内部反代） |
| 20000 / 20001-20010 / 30000-30010 | 已放行 | 与手机入口无关（20000 给 train ssh） |

> ⚠️ 我在本轮之前的汇报里把 `REFUSED` 误判成"安全组未放行"，让用户去开了已开着的端口。
> 教训与自查命令已写进 [ops-traps §6](../env/ops-traps.md)：把"测不到"说成"没放行"是错误结论。

## 5. 运维事实

- Caddy：`active` 且 `enabled`（开机自启）；证书续期由 Caddy 自动完成，前提是 80/443 长期可入。
- ACME 账户与证书存储：`/var/lib/caddy/.local/share/caddy/acme/acme-v02.api.letsencrypt.org-directory`。
- 板子侧：`asistent` 与 `asistent-frpc` 均 active；frpc 只做出站连接。
- 公网击穿成本：控制台令牌 + 鉴权失败节流（全局冷却，见
  [console-auth-throttle-20261023.md](console-auth-throttle-20261023.md)）。

## 6. 手机怎么用（用户侧，两步）

1. 手机浏览器打开 `https://asistent.example.com/`，粘贴控制台令牌（设备上
   `~/asistent-var/config/console-token`）。
2. 安装到主屏：
   - **iOS Safari**：分享 → "添加到主屏幕"；
   - **Android Chrome**：菜单 → "安装应用 / 添加到主屏幕"。

装好后从桌面图标启动即为 `standalone`（无浏览器地址栏），断网时由 Service Worker 提供壳。

## 7. 仍未做（不要读成"全好了"）

- 真实邮箱（U-5 授权码）与真实网盘（U-2 授权）仍未配置 → 不得宣称"真实收信/归档已通过"；
- 双因素、按真实客户端 IP 的白名单（frp TCP 隧道丢失客户端 IP）；
- 域名不是自有域名：用的是把 IP 写进域名的公共通配解析服务；若以后有真域名，只改 Caddyfile 站点名与 A 记录。
