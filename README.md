# asistent · 事务台

**A 24/7 on-call personal assistant that runs on a small arm64 box — pure Python standard library, no pip, no containers, no cloud service.**

它替一个"要上班、要顾家"的人盯着邮件和资料：把进来的事拆成待办，缺信息就来问你，
出草稿给你改，**只有你批准了才真的发出去**，做完的归档留痕。手机上一个网页就是它的全部界面。

---

## 它解决什么问题

不是"再做一个聊天机器人"，而是把一件真事做完整：

```
邮件/资料进来 → 理解（时间、人物、要办什么） → 缺信息？追问你 → 出草稿/建议
        → 你审批（手机点一下） → 执行（发信/归档） → 全链路留痕，可回溯到来源
```

关键在最后两步的**约束**，而不是前几步的"智能"：

- **不确定就不动**：没有批准的外部动作一律不执行；时间说不清就不编截止日期。
- **重复不出错**：审批绑定内容摘要，附件变了旧审批自动失效；发送后崩溃也**不重发**。
- **失败不装死**：通道没配好、网盘没授权、邮件令牌失效——每一路都**如实报告"这一轮为什么没干活"**，
  而不是安静地跳过。这条是这个项目的第一原则：**降级必须看得见**。

## 架构

```
┌──────────────┐   ┌──────────────────────────────────────────┐
│ 邮件 / 目录   │──▶│  service（8 阶段轮次，systemd 常驻）        │
└──────────────┘   │  recover→sweep→sync→ingest→understand     │
                   │  →prepare→outbound→archive                │
┌──────────────┐   │                                          │
│ SQLite WAL   │◀─▶│  契约层 contracts/（对象/状态机/幂等）      │
│ + FTS5 全文  │   └───────────────┬──────────────────────────┘
└──────────────┘                   │
                                   │  stdlib http.server（只绑 127.0.0.1）
                   ┌───────────────▼──────────────────────────┐
                   │  console：手机端 Web / PWA（暖白主题）      │
                   │  看状态 · 答追问 · 改草稿 · 审批 · 取文件   │
                   └───────────────▲──────────────────────────┘
                                   │  Bearer token（只存浏览器内存）
                   手机(移动数据) ──┼── https://asistent.example.com
                                   │        │ Caddy（自动 HTTPS）
                                   │        ▼
                                   └── frps 隧道（板子只做出站连接）
```

板子在公司内网、没有公网入站；手机在外面。做法是板子跑 `frpc` **出站**连一台自有公网
服务器上的 `frps`，公网侧只暴露 80/443 给 Caddy 做 HTTPS 门面，控制台本身只绑回环。
详细取舍见 [`docs/adr/0006-phone-access-via-frp.md`](docs/adr/0006-phone-access-via-frp.md)。

## 关键工程特性

| 特性 | 一句话 | 在哪 |
|---|---|---|
| 契约冻结 | 共享对象/状态机/接口先冻结成快照，改契约要有 CHANGELOG 条目 + 版本号 | `contracts/`、`scripts/gen_contract_snapshot.py` |
| 双存储后端 | 参考实现与 SQLite 实现跑**同一组**契约测试，跑得通同一个语义 | `asistent/storage/`、`tests/storage/` |
| 幂等与一次性执行权 | 同一动作只执行一次；跨进程并发只有一个赢家 | `asistent/actions/`、`tests/actions/test_single_execution.py` |
| 审批绑定 | 审批绑内容摘要，附件/Bcc 变了旧审批即失效 | `tests/actions/test_approval_binding.py` |
| UNKNOWN 保护 | 发送结果未知时绝不重发，转人工决策 | `tests/actions/test_unknown_decision.py` |
| 时钟闸门 | 启动前校验系统时间；下界取 `max(固定下限, 代码提交时刻−1天)`，"RTC 慢两年"能被抓住 | `asistent/service/clockgate.py` |
| 浸泡取样 | 每 5 分钟记内存/句柄/轮次/WAL 曲线，24h 后"一直很稳"要有曲线而不是感觉 | `asistent/service/soak.py` |
| 手机端 PWA | 无框架、无构建、无外部 CDN；暖白主题、PNG/maskable 图标、iOS 可"添加到主屏幕" | `asistent/console/static/` |
| 中文检索 | SQLite FTS5 + 中文分词策略，60 秒内可检索 | `asistent/index/`、`tests/ingest/` |

## 技术栈与硬约束

- **Python 3.10+，只用标准库**（设备上没有 pip，也不打算装）。HTTP 服务用 `http.server`，
  测试用 `unittest`——不是"图省事"，是为了让部署只有 `git pull` 一步。
- **SQLite**（WAL 模式）+ **FTS5**；文件系统是事实源（Markdown / 原始邮件 / 附件）。
- **测试即验收**：`unittest` 878 项（10 个包）；`ASISTENT_STORE=sqlite` 会把同一组测试换到
  SQLite 后端再跑一遍，参考实现与 SQLite 实现必须过同一套语义。
- 前端**零依赖**：没有 React、没有 CDN、没有打包步骤；`app.js` 原生 DOM + `sw.js` 离线壳。
- 明确禁止的框架有测试守着（`tests/console/test_stack_and_compat.py`）：引入 FastAPI/Flask 等会直接失败。

## 快速开始

```bash
# 1) 跑测试（纯标准库，不需要安装任何东西）
python3 -m unittest discover -s tests -t .

# 2) 跑一轮，看每个阶段做了什么、为什么跳过
python3 -m asistent.service --once --json

# 3) 健康快照：跑了多少轮、在等什么、哪一路没干活
python3 -m asistent.service --status --json
```

没有配置文件时它会用**安全默认值**直接跑起来，并在每一路说明"为什么没干活"——
这是设计目标，不是异常。

### 手机控制台

控制台随服务启动（`console.enabled=true`），默认 `127.0.0.1:18090`，用令牌鉴权：

```bash
# 首次：生成控制台令牌文件（600，只打印路径，内容不打印）
python3 -m asistent.service --init-token

# 常驻服务 + 控制台（启动日志会打出"控制台监听 http://127.0.0.1:18090"）
python3 -m asistent.service --console
```

打开页面后把令牌粘贴进输入框；令牌只存在页面内存里（刷新要重粘），不进 URL、不进 localStorage。
令牌文件路径由配置里的 `console.token_path` 决定，缺省在 `$ASISTENT_VAR/config/console-token`。

手机侧要么走同一 WiFi（把 bind 改成局域网地址），要么按
[`deploy/aliyun/`](deploy/aliyun/) 的说明搭一台中转服务器走 HTTPS——
那里有 Caddy 配置和幂等安装脚本，以及板子侧的 [`scripts/frp-onboard.sh`](scripts/frp-onboard.sh)。

## 目录结构

```
asistent/
  contracts/  共享对象、状态机、错误码、归档 manifest（冻结的语义层）
  storage/    参考实现 + SQLite 实现（同一组契约测试）
  index/      中文全文检索（FTS5）
  ingest/     本地资料采集与索引
  mail/       IMAP / mcp-email-server 通道适配
  understand/ 抽取、追问、草稿版本化（时间语义是重点）
  actions/    审批、幂等执行、发送器、UNKNOWN 决策
  growth/     纠正/规则/记忆/流程的版本与回滚
  archive/    归档引擎、manifest、恢复、发布
  pan/        百度网盘通道（需用户自行授权）
  console/    手机端控制台（stdlib HTTP + 静态 PWA）
  service/    8 阶段轮次、健康快照、时钟闸门、浸泡取样
tests/        与上面各包对应的契约/行为测试
docs/         SPEC、验收、架构决策（ADR）、MVP 纪实、环境与运维笔记
deploy/       设备与中转服务器的可复现部署脚本
scripts/      开发/部署/发布工具
```

## 当前边界（诚实说明）

这套东西**不是**开箱即用的 SaaS，下面几项必须你自己接：

| 能力 | 现状 | 要做什么 |
|---|---|---|
| 真实邮箱收发 | 适配器已就绪，需要凭据 | 配 IMAP/SMTP 授权码（放 600 文件，不进仓库） |
| 网盘归档 | 通道已实现 | 装 `bdpan` 并**你自己**完成一次登录授权（OAuth 凭据不可代持） |
| 手机外网访问 | 已实现并实测 | 一台有公网 IP 的中转服务器 + 放行 80/443 |
| 智能理解 | 抽取/追问/草稿可用 | 明确说：这是规则+检索驱动，不含大模型调用；接不接模型由你决定 |

没有配置的通道**不会**假装成功：轮次报告里会写清"sync 跳过：邮件通道未接入"这类原因。

## 关于本仓库

这是**脱敏后的公开快照**：内网地址、主机名、账号路径已替换为文档占位符
（`192.0.2.x` / `203.0.113.50` / `asistent.example.com` / `board`），
不含任何真实凭据。仓库历史是从主仓库重新生成的快照提交，**不带原始开发历史**，
所以看不到逐日演进的 commit 记录——这是有意的。

## 许可

[MIT](LICENSE)
