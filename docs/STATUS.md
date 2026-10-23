# 项目状态（asistent）

> 最新更新：**2026-10-12** · 开发机 shell 为 **Windows PowerShell 5.1**（无 pwsh 7，
> `.ps1` 必须纯 ASCII）；设备 `board:~/code/asistent` 是**权威运行目标**。
>
> **一句话现状**：8 个 MVP 原型已全部合入并在设备上跑通；`contracts-v0.9` 已冻结；
> 24h 常驻服务已在板子上以 `systemd --user` 运行（自启 + `Restart=always`），
> **手机端已可从公网 HTTPS 打开并安装到主屏**（`https://asistent.example.com/`，Let's Encrypt 真证书，
> 2026-10-23 15:37 实测：证书校验通过、无令牌 401、带令牌 200、PWA manifest/SW 均可取）；
> **手机端登录已改为 HttpOnly 会话 Cookie**（换一次令牌后 7 天免粘、滑动续期、可单设备吊销，
> 见 ADR-0007），并在同一次公网验证里修掉了"反代 keep-alive 下未读请求体导致的间歇 501"
> （`docs/env/ops-traps.md §7`）；
> **真实邮箱与真实网盘这两道环境门还等你本人两步动作**
> （U-5 授权码、U-2 网盘授权），在它们完成之前不宣称"真实收发/真实归档已通过"。


## 交付复核（2026-10-12，机械核对，可重跑）

`python3 scripts/delivery_audit.py` 的结果（本地与设备同一提交 `42bbb33`）：

| 核对项 | 结果 |
|---|---|
| 文档里引用的路径是否存在 | 49 处引用**全部存在** |
| 验收矩阵分布 | 17 条：**12 条已写通过**、2 条部分、3 条阻塞 |
| 尚未写通过的 | `AT-01`（24h 窗口）、`AT-09`（末跳→网盘）、`AT-12`/`AT-13`/`AT-14`（真实网盘）——**都卡在 U-2/U-5 这两个只有你本人能做的动作上** |
| 契约冻结 | `version.py = v0.9`，9 个冻结标签，快照自检**一致** |
| 测试 | 双后端各 **860 项、0 失败**（10 个包；`contracts 141 / storage 63 / ingest 99 / mail 57 / understand 105 / actions 65 / archive 81 / console 123 / growth 31 / service 95`） |
| 部署仓库 | `dirty=0` |

这个脚本存在的理由：验收矩阵的证据是几轮里陆续写进去的，路径会随重构改名。
**声称的证据不存在比没有证据更糟**——它看起来像验过了，实际没人再去看。
（它自己也被修过一次：第一版对 `tests/*` 这类通配和"相对文档目录的链接"误报 7 条，
误报的审计等于没人看的审计。）

## 一、阶段进度

| 阶段 | 内容 | 状态 | 证据 |
|---|---|---|---|
| 工程骨架 | 仓库/忽略规则/所有权表/工作流/ADR-0001..0003 | ✅ 完成 | commit `46d878d` |
| 契约冻结 | 共享对象/状态机/接口/幂等/审批/归档 manifest + 检索/时钟/存储协议 | ✅ **contracts-v0.9 已冻结** | 标签 `contracts-v0.1`…`contracts-v0.8`（设备上也在）；快照 `contracts/contracts-v0.8.json`；`docs/contracts/CONTRACT.md` §16 |
| 契约测试 | **141 项**契约测试（并发单赢家、UNKNOWN 不重发、逐文件哈希回读、limit 语义、定序平局、`content_origin`、邮件头解码） | ✅ 双后端全绿 | `python3 -m unittest discover -s tests/contracts -t .`；**2026-10-12 全量复跑（含 AT-17 演练复跑）：10 个包 × 2 个后端 = 各 842 项，0 个包失败**（reference `skipped=35` / sqlite `skipped=13`）；见 `docs/ACCEPTANCE.md` |
| 部署通路 | `scripts/deploy.ps1` 推送 + 六层只读校验 + 冻结标签一并推送 | ✅ 打通 | 最近部署 `3f7ca58`：248 个已跟踪文件逐字节一致、`UNITTEST_RC=0`、`RESULT: deploy path verified` |
| 设备环境门 | 能力与限制实测（含邮件通道可行性） | ✅ 完成（剩余 2 项用户动作） | `docs/env/report.md`、`docs/env/deploy.md` |
| 网盘归档门 | bdpan 通道 Spike + 离线闭环 | 🟡 条件可行（bdpan 3.8.7 已装，**待 U-2 授权**） | `docs/spikes/baidu-pan-spike.md`；`spike/baidu_pan/e2e_offline.py` 52/52 |
| MVP-0 SQLite 存储 | 同一组契约测试下替换参考实现 | ✅ 合入 | `docs/mvp/mvp-0.md`；commit `c34bd95` |
| MVP-1 采集与检索 | 中文可检索 | ✅ 合入 | `docs/mvp/mvp-1.md`；commit `498e30c` |
| MVP-2 邮件同步 | 设备侧 `mcp-email-server` 通道 | ✅ 合入 | `docs/mvp/mvp-2.md`；commit `5072f9e`/`061e7fa`；真实只读探测 19 工具通过 |
| MVP-3 理解与草稿 | 时间语义/追问/草稿版本化 | ✅ 合入 | `docs/mvp/mvp-3.md`、ADR-0004；commit `53278c6` |
| MVP-4 手机控制台 | 设备本地 Web/PWA + token 鉴权（18090，只绑回环）+ 鉴权失败节流 | ✅ 合入 + **设备实测** | `docs/mvp/mvp-4.md`；设备 `LISTEN 127.0.0.1:18090`、无令牌 401、连续失败 429（全局冷却，防公网爆破）；2026-10-23 换暖白主题 + PNG/maskable 图标（iOS 可"添加到主屏幕"）+ 配色按 WCAG 调过对比度 |
| MVP-5 动作与幂等 | 审批绑定/一次性执行权/UNKNOWN 保护 | ✅ 合入 | `docs/mvp/mvp-5.md`；commit `7ad94ee` |
| MVP-6 成长资产 | 纠正/规则/记忆/流程版本与回滚 | ✅ 合入 | `docs/mvp/mvp-6.md` |
| MVP-7 + A-5 归档/发布 | 容错归档恢复 + 构建即发布 | ✅ 合入（真实网盘待 U-2） | `docs/mvp/mvp-7.md`；`scripts/release.sh` |
| MVP-8 集成（常驻服务） | 8 阶段轮次 + `--once/--loop/--status` + systemd unit | ✅ **已合入并在设备常驻** | `docs/mvp/mvp-8.md`（含设备取证实录）；commit `c526240`/`7ab880a` |

### 2026-10-12 复盘（Lead）：这一轮抓到的真缺陷

把"设备实测"当权威目标（SPEC §14.1）之后，**本机全绿、上板就露**的缺陷一下暴露了 4 个。
每一个都不是笔误，而是"只在真机条件成立时才出现"的假设错误：

1. **令牌不可用 ⇒ 整个服务起不来**。POSIX 上令牌父目录权限不是 700 就被拒，
   而 `TokenAuth("")` 会抛异常把服务带崩。Win 不做这项校验，所以本机 9 项红在设备上才出现。
   改成"只降级控制台、不拖死服务"，并补了跨平台回归测试。
2. **网盘远端目录被当成本机路径**。`archive.root` 一律 `_expand()` 成绝对路径，
   于是配置里的 `个人助手归档` 变成 `/home/asistent/asistent-var/config/个人助手归档`，
   归档清单会把本机路径记成"远端位置"；同时 bdpan 客户端**根本没接收**配置的前缀
   （写 A 归到 B）。已按通道区分语义并真传前缀。
3. **`unable to open database file`**。sqlite 会建库文件但不会建目录；
   只有"手写配置直接跑服务"这条路会踩到，而本机夹具恰好都会先建目录。
   现在服务自己保证父目录存在。
4. **取证脚本被一个探测项弄崩**。`crontab -l` 在没有 crontab 时返回 1，脚本是 `set -e`，
   于是整份取证在第 0 节就退出（41 行）。教训值得记：**取证脚本的原则是"凡是探测都要记账，
   但不能因为探测结果不如意就中断"**。

### 2026-10-10 复盘（Lead）

- **`scripts/deploy.ps1` 的"测试通过"判定曾不可靠**：probe 用 `unittest ... | tail -8`，
  而 MVP-2 的测试往 stderr 打大量 `[evidence]` 行，把 `Ran N tests / OK` 挤出窗口 →
  测试其实全过（`UNITTEST_RC=0`）却报 `[WARN] device tests did not all pass`。
  已改为"判定看退出码、摘要单独 grep 全量日志"（commit `69337a5`）。
  教训：**判定逻辑本身也是被测对象**，假告警会训练人忽略告警。
- **契约 v0.5 定序缺陷**：`created_at` 只有毫秒精度 + 用随机 `id` 当平局判据 → 顺序随机、
  分页漏项、跨后端答案不同；参考实现因 Python 慢而掩盖。已定契约（定序键 = `(时间戳, 写入序号)`）
  并让 SQLite 侧补 `seq` 列（迁移 v4）修复。
- **测试环境前提**：夹具含中文文件名，在 ASCII 文件系统编码（`LC_ALL=C`）下会红；
  设备与开发机默认 locale 均为 UTF-8，故按"环境前提"记录而非产品缺陷。

## 二、现在就能用的东西（设备上已经在跑）

```bash
# 常驻服务状态（systemd --user，Linger=yes，掉线/重启自动拉起）
ssh board 'systemctl --user status asistent --no-pager | head -6'
# 健康快照（JSON：跑了多少轮、在等什么、为什么没在收信/归档）
ssh board 'bash ~/code/asistent/scripts/run-service.sh status'
```

- 手机端：**`https://asistent.example.com/`**（阿里云 Caddy 签发 Let's Encrypt 证书 →
  反代到 frps 侧 `127.0.0.1:18800` → frpc 隧道 → 板子 `127.0.0.1:18090`）。
  **待你放行云安全组 TCP 80/443 后自动生效**；在那之前只能从板子本机访问。
  板子控制台现在只绑 **`127.0.0.1`**（不再绑 LAN 地址，也不绑 `0.0.0.0`）：
  frpc 在同机转发，不需要对办公室局域网暴露；也顺带修掉了"DHCP 地址晚到导致 bind 失败"的隐患。
  令牌在设备上 `cat ~/asistent-var/config/console-token`，页面按 `Authorization: Bearer <token>` 鉴权
  （令牌只存浏览器内存：刷新/重进要重粘；它不进 URL、不进 localStorage）。
- 轮次节奏 120s；每轮 8 个阶段（`recover / sweep / sync / ingest / understand / prepare / outbound / archive`）。
- **浸泡取样已在跑**（`asistent-soak.timer`，每 5 分钟一行，写 `~/asistent-var/logs/soak.jsonl`，0600）：
  ```bash
  ssh board 'bash ~/code/asistent/scripts/run-service.sh soak --summary'
  ```
  为什么要有它：24h 之后说"一直很稳"必须有曲线。内存/句柄泄漏、日志与 WAL 无界增长、
  轮次失败率上升**都不会让进程死掉**，所以"服务还活着"证明不了稳定。
- 运行数据全在仓库外：`~/asistent-var`（700），`config`/凭据/日志都在里面。
- **真实邮件通道已预演过**（故意用无效凭据跑真实 `mcp-email-server`）：通道通、握手成功、失败**响亮**（`sync` 阶段 FAIL 并写 `last_error`）、凭据不泄漏到日志与状态；见 [mcp-invalid-credential-20261012.md](evidence/mcp-invalid-credential-20261012.md)。也就是说 **U-5 落地后唯一没验的就只剩那个授权码本身**。

## 三、必须先由你决定/执行的事（阻塞项）

| 编号 | 事项 | 为什么需要你 | 不做的后果 |
|---|---|---|---|
| U-1 | ~~批准在设备安装官方 `bdpan`~~ | ✅ **已完成** | bdpan 3.8.7 → `~/.local/bin/bdpan`（ELF aarch64，静态链接） |
| U-2 | 你本人在设备上完成一次 `bdpan` 授权（推荐 `--device-code`） | OAuth 凭据不可代持、不可经对话传递 | 归档不能用真网盘：`archive` 阶段会如实返回 `NOT_AUTHORIZED`，文件只留在本地 staging。**也不要把 token/BDUSS/授权码贴进对话** |
| U-3 | ~~`sudo loginctl enable-linger asistent`~~ | ✅ **已完成**（`Linger=yes`） | — |
| U-4 | ~~时钟同步~~ **已处理（且发现原机制本来就没生效）** | 实测：本机时间同步由 **chrony** 提供（active/enabled），`systemd-timesyncd` 是 inactive；而 `systemd-time-wait-sync` 等的是 timesyncd 发出的 `time-sync.target` 通知 → 该 unit 从开机起卡在 `activating`（约两天）、target 始终 inactive。也就是说“启动前先等时钟”本来**从来没真正生效** | 现在的保障不依赖 NTP 客户端：unit 里加了 `ExecStartPre=...service.clockgate`（时钟正常零延迟；明显偏早有限等待；等不到也启动但大声告警并落盘 `state/clock-gate.json`）0600））。下限取 `max(固定下限, 代码提交时刻 - 1天)`，所以“RTC 慢两年”这种真实故障现在会被抓住 |
| U-5 | 在设备执行 `bash ~/asistent-var/bin/set-mail-authcode.sh`（不回显、600、不进 shell 历史） | 凭据不得进入对话/仓库/日志；设备 keyring 不可用，只能 700/600 明文文件 | 真实收信不启用（现状：服务每轮如实说"缺授权码 + 怎么办"，不假装收信） |
| U-6 | 确认 `ArchivePolicy`（源白名单/目标根目录/敏感排除/调度/体积上限/保留） | 涉及你的数据边界与自动化授权（SPEC §21.3） | 归档清单里会原样记录 `policy_gate="U-6 未确认"`，不能开"预先确认后自动定期归档" |
| U-7 | **手机侧可达**：✅ **已完成（2026-10-23）** —— 云安全组 80/443 已放行，ECS 上 Caddy 已启用并开机自启，Let's Encrypt 证书签发成功（`issuer=Let's Encrypt YE1`，SAN=`asistent.example.com`，有效期至 2027-01-21），80 强制 308 跳 HTTPS | 入口=`https://asistent.example.com/` → Caddy(443) → `127.0.0.1:18800` → frpc → 板子 `127.0.0.1:18090`；公网只需 80/443，frps 的 18800 从外部实测 TIMEOUT（好事） | 两个外部视角（开发机 + 板子）实测：严格证书校验 `Verify return code: 0`、无令牌 401 带 `WWW-Authenticate`、带令牌 200 且取到新主题 CSS、`/api/overview` 200、iPhone/Android UA 均 200；证据见 [phone-https-20261023.md](evidence/phone-https-20261023.md) |

> 📱 **手机侧可达方案**见 [ADR-0006](adr/0006-phone-access-via-frp.md)（已取代 ADR-0005 的 Tailscale 方案）：
> 板子跑 `frpc`（systemd user unit）**出站**连阿里云 `frps:7000`，把公网 `18800` 映射到板子
> `127.0.0.1:18090`；阿里云用 Caddy 在 **80/443** 上做 HTTPS 门面并自动续证书。
> 安全边界：板子不向公网开任何入站端口，公网只暴露阿里云的 80/443；**控制台令牌仍是第二道门**，
> 且控制台只绑回环、不经 LAN 暴露。以后别的服务也挂这台中转站，端口从 **30000** 起往后排（你的约定）。
> 过渡手段（已在跑，但依赖这台开发机不休眠）：本机 `ssh -L 0.0.0.0:18090:192.0.2.10:18090 board`。

> 你不需要提供：token、cookie、BDUSS、AppSecret、授权码的**任何内容**。
> 也不要把 Windows 上已登录的 `~/.config/bdpan/` 复制到设备（建议设备独立授权）。

## 四、已确认的关键决策

- **运行形态**：纯标准库 Python 3.10+（设备 3.10.12 实测可跑）；SQLite WAL + FTS5；`unittest`；`http.server` 控制台。
- **开发/部署拓扑**：开发机开发 → `git push board:code/asistent`；运行数据在 `~/asistent-var/`（700，仓库外）。
- **邮件通道**：`mcp-email-server`（stdio 子进程）+ 服务侧 `mail.source=mcp` 接线；
  凭据只从 600 文件读、只进子进程环境变量 `MCP_EMAIL_SERVER_PASSWORD`，不进配置/argv/日志。
- **网盘通道**：`bdpan` CLI（Go 自包含二进制）；不自建开放平台应用（未过审配额 10 次/小时会撞墙）。
- **中文检索**：应用层逐字切分 + 短语查询（`asistent/contracts/search.py`）。
- **时钟**：不可信就拒写业务时间戳（`asistent/contracts/clock.py`），时长用单调钟。
- **泄漏口径**：禁的是**值形态**（`password=` / `authcode=` / `access_token` / `authorization:`），
  不是"凭据"这个词本身——否则"缺凭据，去跑这个脚本"这句诊断就没法说出口。

## 五、命令速查

```powershell
# 开发机：契约快照校验 + 全部测试
powershell -File scripts/test.ps1 -Python C:\Users\<you>\.conda\envs\<env>-a29d9cc4\python.exe
# 只校验不改：设备状态六层校验
powershell -File scripts\deploy.ps1 -VerifyOnly
# 推送并校验
powershell -File scripts\deploy.ps1
```

```bash
# 设备：全部测试（纯标准库，无需 pip）
cd ~/code/asistent && python3 -m unittest discover -s tests -t .
# 设备：真实硬件端到端取证（一轮 + 常驻两轮 + 手机审批 + 恰好一次外发 + 优雅停止）
sh tests/service/device_run.sh ~/code/asistent
# 设备：常驻服务运维
bash scripts/run-service.sh doctor|status|start|stop|restart|logs|install-helpers
# 设备：回滚演练（可复现，不动已部署副本）
bash scripts/at17-rollback-drill.sh
```

## 六、下一步（按优先级）

1. **U-5 → 真实收信**：你录入授权码后重启服务，用真实邮件走一遍
   邮件→资料→追问→草稿→审批→动作，并把原始输出补进 `docs/mvp/mvp-8.md`。
2. **U-2 → 真实归档**：网盘授权后跑一次真上传与回读校验（AT-12/13/14 的堵点）。
3. **24h 墙钟**：**起点以进程自身为准 = 2026-10-12T08:57:00**（`run_id exec_a46b5f24…`），
   所以要到 **2026-10-13T08:57** 才够 24 小时（08:37 那次是首次安装，08:54 被重配覆盖；
   首个取样点 09:23:58）。取样器已武装（`asistent-soak.timer`，5 分钟一行）：
   ```bash
   # 起跑：前置检查 → 记锚点（run_id/提交/契约版本）→ 重启服务
   ssh board 'bash ~/code/asistent/scripts/acceptance-round.sh start'
   ssh board 'bash ~/code/asistent/scripts/acceptance-round.sh status'   # 还差多久
   ssh board 'bash ~/code/asistent/scripts/acceptance-round.sh verdict'  # 未满 24h 会拒绝出结论
   ```
   结论由**预先冻结**的判据（CP-37）机械判定，不由人看着曲线定标准；跨度不足 23.5h 时它会
   自己提醒"不能当作 24h 结论"。首个取样点（09:27:37）：
   `rss_kb 28752 / fds 10 / threads 2 / rounds_completed 35 / rounds_failed 0 / listening_console 1`，
   存储 `db_bytes 495616 / wal_bytes 3432024`——**WAL 是主库的 ~7 倍，贴着 SQLite 默认
   autocheckpoint 阈值（1000 页 ≈ 4 MB）**，要么在阈值附近振荡（自动 checkpoint 生效）、
   要么单调上升（长读事务挡住 checkpoint）；5 个点判不出来，正是"先武装取样再跑 24h"的意义。
   ⚠ 一条必须与结论同时记录的事实：**运行中的进程跑的是它启动那一刻的代码**，
   之后设备仓库又部署了多次（`de824dc` 起），所以 AT-01 要同时写 `run_id` 与进程启动时的提交。
4. **CP-32…CP-37 裁决**：设备取证暴露的契约级提案（邮件头解码、`Event` 账本归属、
   服务自检对象、归档 `policy_gate`、长时间浸泡判据）。
5. ~~**v0.9 冻结**~~：**已做**（2026-10-12）：快照 `contracts/contracts-v0.9.json` + CHANGELOG（CP-32 接受并实现；CP-33/37 接受并实现；CP-34 前提被实测推翻、退回；CP-35/36 推迟）+ CONTRACT.md §18 + 标签 `contracts-v0.9`。
