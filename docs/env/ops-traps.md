# 运维陷阱（都是实测踩出来的，不是想象）

这份文档记"不长记性就会再踩一次"的坑。每条都给出**表现 → 根因 → 规矩**。

---

## 1. 经 PowerShell→ssh 传 `sed "s/\r$//"` 会吃掉行尾字母 `r`

**表现**：远端脚本里 `-Server` 变成 `-Serve`、`referer` 变 `refere`……其他都正常，
所以脚本还能跑，只是内容被悄悄改了。2026-10-23 在阿里云装 Caddy 时，
Caddyfile 里去掉 `Server` 头的 `-Server` 指令被写成 `-Serve`（等于删了一个不存在的头，没报错）。

**根因**：Windows 上 `ssh host 'sed -i "s/\r$//" /tmp/x.sh'` 这条链路里，`\r` 在到达远端
`sed` 之前被解释成了**字母 r**，于是正则变成 `s/r$//` —— 把每行末尾的 `r` 删掉。
用文件里真正的 CR（`A\r\n`）测是正常的，所以这个坑只对"经 ssh 一行命令"的情况成立。

**验证方式（可复现）**：

```powershell
# 本地文件含 "-Server"，scp 到远端后哈希一致
Get-FileHash .\var\Caddyfile -Algorithm SHA256
ssh ubuntu 'sha256sum /tmp/Caddyfile.prod'          # 一致
ssh ubuntu 'sed -i "s/\r$//" /tmp/Caddyfile.prod'   # 跑完再算
ssh ubuntu 'sha256sum /tmp/Caddyfile.prod'          # 变了；grep -c Server 变成 0
```

**规矩**：
1. 仓库里的脚本/配置一律 **LF**（`write` 工具默认就是 LF），所以**根本不需要**去 CR 这步；
   `scp` 是字节精确的（哈希已验证）。直接 `bash /tmp/x.sh` 即可。
2. 真要处理 CRLF，写成脚本文件在远端跑（脚本内部的 `\r` 不会被二次解释），或用
   `tr -d '\r' < in > out`，**不要**用 ssh 一行命令里的 sed 正则。
3. 传完关键文件后核对一次哈希或 `grep` 关键 token（本次就是这样发现问题的）。

---

## 2. 测试里写死"现在"= 时间炸弹（随仓库提交日期过期）

**表现**：本机全绿，**设备部署门禁突然红**，报
`'clock_behind_floor' != 'ok'` 或"时钟正常时不许等待"却等了 24×5 秒。
代码没改、环境没变，红的时机与质量无关。

**根因**：时钟闸门的下界是 `max(固定下限 2024-01-01, 仓库 HEAD 提交时刻 - 1 天)`。
测试夹具把"现在是 `2026-10-12`"写进断言；每提交一次下界就往后挪一天，
2026-10-23 的提交把下界推到 `2026-10-22`，写死的"现在"就落到下界之前了。

**规矩**：涉及"当前时间 vs 某个随时间移动的锚点"的测试，**一律相对计算**：

```python
def _sane_now() -> datetime:
    return clockgate.effective_floor({}) + timedelta(hours=2)
```

并留一条"夹具必须跟着锚点走"的测试（`FixtureAnchorTest`）把这条规矩钉住。
类似的还有：不要断言"今天是某年某月"、不要写死自增 ID、不要假设目录里只有 N 个文件。

---

## 3. `systemctl --user` 的日志可能根本不在 journal 里

**表现**：`journalctl --user -u asistent` 返回 `-- No entries --`，看起来"服务没日志"。

**根因**：这台板子的 user journal 没有落盘/未启用，user unit 的输出没进 journal。

**规矩**：常驻 user unit 一律在 `[Service]` 里显式落文件：

```ini
StandardOutput=append:%h/asistent-var/logs/frpc.log
StandardError=append:%h/asistent-var/logs/frpc.log
```

排障先看这些文件（`~/asistent-var/logs/service.log`、`soak.jsonl`、`frpc.log`）。

---

## 4. `apt install caddy` 会顺手把服务拉起来

**表现**：装完就 `active`，还占住 admin API `127.0.0.1:2019`，导致自己起的
临时验证实例报 `bind: address already in use`。

**规矩**：装完先 `systemctl stop caddy`，等前置条件（云安全组 80/443）齐了再
`caddy-onboard.sh --start`；验证配置用 `caddy validate`，验证反代链路用**独立 admin** 的
临时实例（`{ admin off }` + `:8443`）。

**附带教训**：Caddy 里 `:8443 { tls internal }` 这种**无主机名**的站点地址**不会**自动启用 TLS
（只有带域名的站点才会），所以拿 HTTPS 去连它会得到
`SSL routines::wrong version number`，而用 HTTP 连它反而正常返回 401。
验证 TLS 要用带域名的站点地址，或者干脆验明文链路 + `caddy validate` 两件事分开做。

---

## 5. 端口分工（阿里云中转站的约定）

- **80 / 443**：留给 Caddy 门面（80 只做 ACME 验证与跳转，443 是手机入口）。
- **30000 起往后**：用户约定给"以后别的小玩意儿"。
- frps 的 `remotePort`（现为 18800、20000）**不直接对公网放行**：它们只被阿里云本机的
  Caddy / SSH 客户端从回环访问。云安全组不需要为它们开端口。

---

## 6. "连不上"必须区分**被挡**与**没人监听**（2026-10-23 我判断错过一次）

现象：用 `Test-NetConnection` 探 `203.0.113.50:443` 得到 `TcpTestSucceeded=False`，
于是我在汇报里写"80/443 还被安全组挡着"，让用户去开端口。用户回："80/443 是已经开的啊"。
一测才发现：**安全组早就放行了，只是 Caddy 当时是停的**——`connect()` 立刻被 RST（拒绝），
而不是被丢包超时。我的探针把两种完全不同的状态都标成 `closed/blocked`，给出了假结论。

**规则**：探端口时把异常分成三类，别合并：

| 结果 | 含义 | 下一步 |
|---|---|---|
| `CONNECTED` | 有进程在监听 | 直接验业务 |
| `ConnectionRefusedError`（通常 <100ms 返回） | **安全组放行**、该端口没有监听进程 | 去启动服务，别让用户改安全组 |
| `socket.timeout`（等到超时） | 很可能被安全组/防火墙丢包 | 才需要请用户放行 |

一条命令自查（在外部机器上跑）：

```bash
python3 - <<'EOF'
import socket, time
for port in (80, 443):
    s = socket.socket(); s.settimeout(6); t0 = time.time()
    try:
        s.connect(("203.0.113.50", port)); print(port, "CONNECTED")
    except ConnectionRefusedError:
        print(port, "REFUSED -> 已放行但没人监听")
    except socket.timeout:
        print(port, "TIMEOUT -> 可能被安全组挡")
    finally:
        s.close()
EOF
```

教训：**"测不到"和"没放行"是两件事**。把不确定写成确定，会让用户白干一遍。

---

## 7. 反代后面的间歇 501：请求体没读完，残字节粘到下一个请求行

**症状**（2026-10-23，手机端公网入口上线当天）：手机走
`Caddy -> frps -> frpc -> 控制台:18090`，偶尔一次请求 501，刷新一下又好了。响应体是
Python `http.server` 的默认错误页：

```
Error code: 501
Message: Unsupported method ('{}GET').
```

**根因**：`POST /api/session` 的请求体是 `{}`（2 字节），但那个端点不需要参数，
于是**没有读 body**。Caddy 默认会**复用**到上游的 keep-alive 连接；残留的 `{}` 和下一个
请求的请求行粘在一起，`http.server` 把 `{}GET` 当成了 HTTP 方法名。

**为什么本机测不出来**：本机测试 / curl 每次开新连接，`parse_request` 拿到的永远是干净的
请求行。只有"客户端 → 反代（复用连接）→ 本服务"这条路径会踩到，表现还带随机性。

**排查线索**（记住这个顺序）：
1. 501 不是自己代码里的状态码 → 说明请求**在路由之前**就被拒了；
2. 错误页是 `text/html;charset=utf-8` + "Error response" → 是 Python `http.server` 的
   `send_error`，不是 Caddy/Go 的（Go 的 `http.Error` 是 `text/plain`）；
3. 页面里的 `Unsupported method ('...')` **冒号后面那一串就是被解析出的方法名**——
   这里是 `{}GET`，一看就知道有字节提前溜进请求行了；
4. 对照服务自身的请求日志：501 那次**没有**日志行，因为 `_handle` 根本没执行。

**修法**（通用原则，不只是本项目）：HTTP/1.1 keep-alive 下，**服务端必须消费掉请求体**，
哪怕这个端点完全不需要参数。`http_api._handle` 现在在分发前统一
`_consume_request_body()`（受 `max_body_bytes` 限制）；长度非法/超限时不硬读，而是
`close_connection = True` + `Connection: close`，宁可让反代重开连接也不留残字节。

**回归测试怎么写**：用**同一条** `http.client.HTTPConnection` 连发两个请求：
先 `POST` 一个带 body、端点不读体的路径，再 `GET` 一个正常路径，断言第二个不是 501
（`tests/console/test_keepalive_body.py`）。用"每次请求新建连接"的测试客户端写，这个 bug 永远测不出来。
