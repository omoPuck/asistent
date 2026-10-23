# MVP-4 手机控制台（设备本地 Web/PWA）

> 交付范围：`asistent/console/**`（含 `static/**`）、`tests/console/**`、本文件。
> 基线：`main`（契约 v0.6）。本层**零契约改动**。
> 结论一句话：在设备本地起一个只监听内网网卡的 Web 服务（18090），手机浏览器输入一个
> 存在仓库外 700/600 文件里的令牌，就能**看 / 答 / 改 / 批 / 取**，并且**断线、关页面、
> 重启服务之后状态还在**。

---

## 0. 一句话回答"凭什么可信"

| 最容易作假的地方 | 本层怎么堵 | 证据 |
| --- | --- | --- |
| "状态"其实存在进程内存里 | 服务对象只有 `RLock` 和配置，任何读路径都打到 `store` | §7.1 真子进程重启 |
| 未鉴权时靠 404 泄露"这个 Case 存在" | token 校验在**任何 store 访问之前**，401 文案是常量 | §7.2 逐字节对比 |
| 手机上"点了一下同意"就算批准 | 审批必须 `confirm=true` + `version_no` + 绑定内容哈希 | §7.3 三类拒收 |
| 并发编辑"最后写的赢" | 每次修改都要 `expected_revision` 或 `version_no`，冲突 409 且**不落盘** | §7.4 |
| 用第三方框架"顺便"带上依赖 | 只有标准库 + 无构建的原生 JS，静态扫描钉住 | §7.7 |

---

## 1. 设计取舍

### 1.1 为什么是"设备本地服务 + 手机浏览器"，而不是原生 App

* 设备（RK3588）就是这个人的**唯一权威数据源**。控制台做成设备上的服务，手机只是
  一个瘦客户端：换手机、重装浏览器、清缓存都不会丢状态。原生 App 反而会引出
  "手机上那份缓存才是真相"的新问题。
* 不做公网入口。服务**只绑设备内网地址**（`192.0.2.10:18090`），手机在同一内网即可用。
  这满足 SPEC §9.1「独立控制」与 §13.4「权限和网络」：默认关闭、只走内网、显式开启。

### 1.2 为什么坚持纯标准库（不用 FastAPI/Flask，前端不用 Node/构建）

* 设备上**没有 pip**（`/usr/bin/python3` 是系统 3.10.12）。任何第三方依赖都意味着
  要先在生产设备上解决包管理问题，这是本项目明确回避的风险。
* `http.server` 的 `ThreadingHTTPServer` 对"一个人、一台设备、几个请求"这个量级足够；
  引入框架换不来什么，却引入了"框架升级 → 行为变化"的长期负担。
* 前端是一个 `index.html` + 一个 `app.js`（无构建、无依赖、无 CDN）。手机浏览器直接跑，
  离线时至少壳还在（`sw.js` 只缓存壳，`/api/**` **永不缓存**）。

### 1.3 令牌不落浏览器存储，但设备会被记住：HttpOnly 会话 Cookie

最初的版本把令牌只放在 `app.js` 的内存变量里，代价是**每次重开页面都要重粘 64 位令牌**。
装成 PWA 从桌面图标启动时，这等于每次打开都先做一次手工鉴权——"能打开"和"天天愿意用"
是两件事（见 ADR-0007）。

现在的分工是：

* **令牌只用来换一次会话**（`POST /api/session`），换完立刻从 `app.js` 内存里清掉；
* 会话放在 `HttpOnly + Secure + SameSite=Strict` 的 Cookie 里：JS 读不到、不进
  localStorage/sessionStorage、不进 Service Worker 缓存；
* 服务端只存会话值的 **SHA-256**，TTL 默认 7 天且**滑动续期**；退出登录会真正吊销
  （`DELETE /api/session`），服务重启即全部失效；
* 会话鉴权的**写操作**必须再带 `X-Asistent-Client: console`：跨站表单发不出自定义头，
  也没开 CORS，这是 `SameSite=Strict` 之外的第二道 CSRF 防线；
* 手机+电脑各自独立会话（登录时不踢别的设备）；设备上"再登录一次"只轮换自己那份。

`?token=` 形式的 URL 令牌仍然被**故意忽略**（避免令牌进浏览器历史、进代理日志）；
`Authorization: Bearer` 也仍然可用（curl 排障、旧客户端）。

---

## 2. 模块结构

```
asistent/console/
  __init__.py      # 对外只暴露这 4 个东西：Settings / Service / build_server / load_token
  errors.py        # ConsoleError -> BadRequest/Unauthorized/Forbidden/NotFound/Conflict，统一 JSON
  settings.py      # 端口 18090、绑定地址、token 路径、请求体上限、可下载目录白名单
  auth.py          # load_token（700/600 校验）+ TokenAuth（常量时间比较 + 脱敏）
  throttle.py      # 全局鉴权失败节流（frp 隧道下客户端 IP 都是 127.0.0.1，只能按全局算）
  sessions.py      # HttpOnly 会话 Cookie（只存哈希、滑动续期、可吊销）
  service.py       # 全部业务语义：**读写在 store，状态不在内存**
  http_api.py      # 路由 + HTTP 细节（鉴权前置、Content-Length、静态文件 realpath 校验）
  static/          # index.html / styles.css / app.js / sw.js / manifest.webmanifest / icon.svg
```

依赖方向是单向的：`http_api → service → store/contracts`，`service` 不知道 HTTP 的存在，
`http_api` 不知道业务规则。这样"同一份语义"可以脱离 HTTP 被测试（本层的并发/重启证据
就不经过 socket 之外的任何抽象）。

### 2.1 四个不变式（写在 `__init__.py` 的 docstring 里，改代码前先读）

1. **状态只在 store 里**：进程内存里没有"当前打开的 Case""上一个令牌"这类会话状态；
2. **鉴权先于一切**：`require_auth` 在任何 store 访问之前执行，未鉴权请求拿不到任何对象级信息；
3. **写必须带版本**：每个修改接口要么带 `expected_revision`，要么带 `version_no`；
4. **审批必须显式**：`confirm=true` + 绑定确定版本 + 绑定内容哈希，普通请求文本不算批准。

---

## 3. 接口一览（`http_api.py`）

所有接口都要令牌；响应统一 `application/json; charset=utf-8`，错误统一为
`{"error": {"code", "message", "detail"?}}`。

| 方法 | 路径 | 用途 | 关键点 |
| --- | --- | --- | --- |
| GET | `/api/health` | 存活与总量 | 只回计数与时间，不回 token |
| GET | `/api/overview` | 待办总览 | 回 `pending` 快照 + `next_since` + `ETag`；带 `If-None-Match` 命中 304 |
| GET | `/api/changes?since=` | 增量拉取 | 只回变更对象；**快照仍完整** |
| GET | `/api/cases` | 事务列表 | 过滤 `state` / `since` / `include_closed` / `limit` |
| POST | `/api/cases` | 新建事务 | 只有 `goal` 一个必填 |
| GET | `/api/cases/{id}` | 事务详情 | `include=documents,audit` 时附带原文与审计 |
| GET | `/api/questions` | 待回答追问 | 默认只回 `OPEN`；`?state=`（空值=全部） |
| POST | `/api/questions/{id}/answer` | 提交回答 | 需 `answer_text` + `expected_revision` |
| GET | `/api/drafts/{id}` | 草稿与版本 | 回 `current` + `versions` |
| POST | `/api/drafts/{id}/edit` | 编辑保存 | 需 `version_no` 或 `expected_revision`；产生新 DraftVersion |
| POST | `/api/drafts/{id}/restore` | 恢复旧版本 | 需 `version_no` + `expected_revision`；也是**新版本**（不覆盖历史） |
| GET | `/api/drafts/{id}/diff?from=&to=` | 版本差异 | 统一 diff 文本 + 增删行 |
| GET/POST | `/api/approvals` | 审批列表 / 提交 | 提交需 `confirm=true` + `version_no` |
| GET | `/api/approvals/{id}` | 审批详情 | 含 `summary_shown`（当时给用户看的是什么） |
| POST | `/api/approvals/{id}/revoke` | 撤销 | 需 `expected_revision`；仅 `ACTIVE` 可撤 |
| GET | `/api/artifacts` | 文件元数据 | 不回本机路径 |
| GET | `/api/artifacts/{id}/content` | 下载 | 白名单校验 + 下载计数/失败计数 |

### 3.1 并发控制的统一做法

* 修改接口全部走 `_require_revision`：`expected_revision != obj.revision` → `409 REVISION_CONFLICT`，
  `detail` 里给 `expected_revision / current_revision / action="请刷新后基于最新版本重新提交"`；
* 草稿编辑额外检查 `version_no`（"我在改的是不是当前版本"），因为手机端最自然的
  心智是"版本"，而不是"行号式的 revision"；
* **冲突路径不写任何东西**。这不是靠"小心"，而是靠先把校验做完再调用 store —— 证据里
  用"版本历史仍是 `[1, 2]`""该追问的 Answer 条数仍是 1"来钉住。

---

## 4. 六项必备能力（SPEC §9.2）逐条落地

| 能力 | 落地位置 | 测试（共 123 项） |
| --- | --- | --- |
| **看**：Case 状态/Agent 状态/最近动作/等待原因/下一步/相关对象（可定位引用） | `list_cases`、`get_case`、`_agent_status`、`_citation` | `test_cases.py` 21 项 |
| **答**：列出待回答追问，提交后落进**正确 Case** 且依赖任务重新评估 | `list_questions`、`submit_answer`、`_reevaluate_dependents`、`_refresh_case_waiting_state` | `test_questions.py` 17 项 |
| **改**：编辑产生**新 DraftVersion**（作者/时间/内容哈希/引用/原因），旧审批失效，差异可见 | `edit_draft`、`restore_draft_version`、`diff_draft`、`_invalidate_approvals_for` | `test_drafts.py` 14 项 |
| **批**：审批绑定确定版本 + 可撤销；**只有这条通道**算批准 | `submit_approval`、`revoke_approval` | `test_approvals.py` 17 项 |
| **取**：文件元数据 + 下载，失败可重试且互不影响 | `list_artifacts`、`get_artifact`、`read_artifact` | `test_artifacts.py` 11 项 |
| **断线可恢复**：`since`/ETag 增量，不每次全量 | `overview`、`changes`、`_etag`、`_changed_since` | `test_resilience.py` 13 项 |
| **鉴权与信息泄露**（以上全部能力的前提） | `require_auth`、`load_token`、`TokenAuth` | `test_auth.py` 24 项 |
| **纯标准库 / 3.10 兼容 / 前端无依赖** | 静态扫描 | `test_stack_and_compat.py` 6 项 |

### 4.1 "答"之后 Case 状态怎么走（一个真实约束）

提交最后一条回答后，`Case` 应该从 `WAITING_FOR_USER` 走到"等 Agent 重新分析"。
但契约里 `WAITING_FOR_USER → WAITING_FOR_CONTEXT` **不是合法转移**（可去的是
`ANALYZING / BLOCKED / CANCELLED / READY_FOR_REVIEW`）。控制台的做法是：

* 只要还有 `OPEN` 追问：保持 `WAITING_FOR_USER`，把 `waiting_reason` 写成"等待用户回答 N 条追问"；
* 追问清空且有 `OPEN` 草稿：`READY_FOR_REVIEW` + "等待用户审批草稿"；
* 追问清空、没有草稿：把 `waiting_reason` 写成"追问已回答，等待 Agent 基于回答重新分析"、
  `next_action` 写成"重新分析事务"，**但状态只在 `machine_for("Case").can(...)` 允许时才改**，
  否则把"该谁动"如实留给 Agent 的 `ANALYZING` 那一步。

也就是说：控制台**不伪造**它没有权限做的状态转移。这是 CP 提案 2 的起因（见 §10）。

### 4.2 审批的"唯一激活"（契约 v0.6 对齐）

契约 v0.6（CP-20261010-15）规定：**同一 `action_id` 上至多一个 `ACTIVE` 审批**，
批准新审批时存储层在同一事务内把同动作的其它 `ACTIVE` 置为 `INVALIDATED`。

控制台的做法与边界：

* 请求可带可选的 `action_id`，带了就交给 `store.approve` 就地取代；
* 响应回 `superseded_approval_ids`（对比批准前后的 `ACTIVE` 集合），客户端据此把被取代的
  审批从"有效"列表里拿掉 —— **不由客户端猜**；
* `action_id` 留空时（"审批一张草稿"这一步通常还不知道将来由哪次执行消费），
  v0.6 的唯一性约束**不生效**，同一版本可以并存两张 `ACTIVE` 审批。这一点在 §9 写成已知限制，
  并作为 CP 提案 3 请 Lead 裁决。

---

## 5. 手机端界面（PWA）

* 三个页签：**待办**（待回答追问 / 待审批草稿 / 可下载文件 / 传输失败）、**事务**（列表 + 详情）、
  **文件**（元数据 + 下载）。
* 令牌闸门：未连接时只显示一个输入框；令牌校验通过（`GET /api/overview` 200）才渲染任何数据。
* 连接状态药丸：`已连接 / 离线 / 令牌失效`。401 一律回到闸门（不保留任何已加载数据）。
* 每次"写入"都带版本；服务端回 409 时弹"版本已变，已刷新到最新状态"并**重新拉取**，
  而不是让用户的手输内容覆盖别人的修改。
* `sw.js` 只缓存壳（`/`、`/index.html`、`/styles.css`、`/app.js`、清单、图标），
  `/api/**` 明确 **network-only**。注意 Service Worker 需要 HTTPS 或 localhost，
  设备这里走的是明文 HTTP，所以**离线壳能力在设备上不生效**（这是刻意的：不为了离线
  而把令牌和业务数据放进缓存）。

---

## 6. 安全模型（以及"被拒绝"的样子）

| 面 | 做法 |
| --- | --- |
| 令牌来源 | 仓库外的文件，目录 700 / 文件 600；`openssl rand -hex 32` 生成 |
| 令牌校验 | `hmac.compare_digest`；长度 < 16 直接拒；只接受 `Bearer` |
| 令牌去向 | 不进 URL（`?token=` 被忽略）、不进日志、不进响应、不进审计 detail |
| 未鉴权 | 401 + 常量文案 + `WWW-Authenticate`；**在任何 store 读取之前**判定 |
| 静态壳 | 也要令牌（未鉴权访问 `/` 也是 401） |
| 路径穿越 | 静态文件与下载都做 `realpath` 前缀校验；下载还需**显式白名单**（默认空 = 全部拒绝） |
| 请求体 | 上限 256 KiB；`Content-Length` 缺失/非数字/负数/超限/为 0 一律 400 |
| 输出卫生 | `nosniff` + `no-referrer`；错误永不回 traceback；`SourceDocument.origin_path`、`FileArtifact.path` 不外泄 |
| 越权 | 未实现的动作/资源 → 403/404；CORS 预检 → 405（不做跨站可用） |

"被拒绝"的样子（可直接对照 §7 原始输出）：

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer realm="asistent-console"

{"error": {"code": "UNAUTHORIZED", "message": "需要有效的控制台令牌"}}
```

---

## 7. 验收证据

三份原始输出，均可复现：

1. **离线自测**：`python -X utf8 tests/console/evidence_dump.py`（本机 Python 3.12.14，双后端）；
2. **离线测试**：`python -X utf8 -m unittest discover -s tests -t .`（含/不含 `ASISTENT_STORE=sqlite`）；
3. **真机验证**：设备 `board`（aarch64 / Ubuntu 22.04 / `/usr/bin/python3` 3.10.12）
   上 `bash tests/console/device_check.sh`。

> 下面贴的都是**原样输出**。令牌在响应/日志里从未出现，故文档里也不需要给它占位符；
> 文档不记录令牌值，只记录"长度 65 字节（含换行）"。

### 7.0 环境与命令

```
$ python -X utf8 tests/console/evidence_dump.py
MVP-4 手机控制台 · 可复现证据
Python: 3.12.14
后端：reference
命令：python -X utf8 tests/console/evidence_dump.py
```

### 7.1 状态在 store 里（真子进程重启）

`tests/console/serve_once.py` 以独立进程启动服务；测试写数据 → 杀掉进程 → **起一个全新进程** →
数据仍在。这是"状态不在内存里"的唯一硬证据（同进程换 HTTP 对象的"假重启"不算）。

```
$ ASISTENT_STORE=sqlite python -X utf8 -m unittest discover -s tests/console -p test_resilience.py -t .
Ran 13 tests in 2.772s

OK (skipped=1)
```

其中"真重启"那条的子进程日志（真机版见 §7.7，格式相同）：

```
LISTENING 127.0.0.1 61145
GET /api/health -> 401 auth=denied 0.0ms
GET /api/health -> 200 auth=ok 0.1ms
```

重启前后断言的是同一批事实：换进程之后 `Case` 仍在待处理快照里、`Question` 仍是
`ANSWERED` 且 `answer_id` 指回同一条 `Answer`；被 `terminate()` 杀掉的进程以非零码结束
（这是信号结束的正常表现），测试断言的是"进程确实停了 + 数据仍在"，不是某个特定退出码。

服务进程自己的日志（真机版，见 §7.6，同样格式）：

```
LISTENING 192.0.2.10 18090
GET /api/health -> 401 auth=denied 0.1ms
GET /api/health -> 200 auth=ok 0.2ms
```

要点：**日志只有方法 / 路径 / 状态 / 鉴权结果 / 耗时**，没有 query、没有 header、没有 body。

### 7.2 未鉴权：401 且不泄露"对象是否存在"

同一路径、同一个**不存在**的 Case，分别用"无 token"和"错 token"请求：

```
--- 无 token 请求任意不存在的 Case ---
GET /api/cases/case_does_not_exist HTTP/1.1
Host: 127.0.0.1:64022
Accept: application/json

HTTP/1.1 401
Content-Type: application/json; charset=utf-8
Cache-Control: no-store
WWW-Authenticate: Bearer realm="asistent-console"
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Content-Length: 80

{"error": {"code": "UNAUTHORIZED", "message": "需要有效的控制台令牌"}}

--- 错 token 请求同一个不存在的 Case（响应必须与上面完全一致） ---
GET /api/cases/case_does_not_exist HTTP/1.1
Host: 127.0.0.1:64022
Accept: application/json
Authorization: <token>

HTTP/1.1 401
（响应头与响应体与上面逐字节相同）

>>> 两次响应体逐字节相同：True

--- 未鉴权访问静态壳（同样要 401） ---
GET /index.html HTTP/1.1
HTTP/1.1 401
Content-Length: 80

{"error": {"code": "UNAUTHORIZED", "message": "需要有效的控制台令牌"}}

--- 正确 token：/api/health ---
GET /api/health HTTP/1.1
Authorization: <token>

HTTP/1.1 200
Content-Length: 139

{"counts": {"AuditEvent": 0, "Event": 0, "_total": 0}, "ok": true, "server": "asistent-console", "server_time": "2026-10-10T08:28:52.556Z"}
```

测试层面的断言更强（`tests/console/test_auth.py`）：

* 401 响应体**恰好**等于上面的 80 字节，没有 `detail` 字段；
* 未鉴权 `/api/overview` 的响应里不含 `case` / `question` / `approval` / `pending` / `count` 任何子串；
* 未鉴权 `POST /api/approvals` 之后 `store.stats()` **没有变化**（证明没写任何东西）；
* 静态资源 `/`、`/index.html`、`/app.js`、`/styles.css`、`/manifest.webmanifest`、`/icon.svg`
  未鉴权全部 401；
* `?token=` 形式的 URL 令牌被忽略，仍是 401。

### 7.3 批：三条"看起来像批准"的请求全被拒

```
--- 普通请求文本不能当批准（无 confirm=true → 403） ---
POST /api/approvals HTTP/1.1
Content-Length: 100

{"draft_id": "draft_6794cf928cba4f471715ae5d", "text": "同意，直接发送", "message": "批准"}

HTTP/1.1 403
Content-Length: 142

{"error": {"code": "FORBIDDEN", "message": "审批必须显式确认（confirm=true）：普通请求文本不构成批准（SPEC §21.3）"}}

--- 审批必须绑定确定版本（缺 version_no → 400） ---
POST /api/approvals HTTP/1.1
Content-Length: 63

{"draft_id": "draft_6794cf928cba4f471715ae5d", "confirm": true}

HTTP/1.1 400
Content-Length: 99

{"error": {"code": "BAD_REQUEST", "message": "审批必须绑定确定版本：缺少 version_no"}}

--- 显式审批 v1（confirm=true + version_no + 绑定内容哈希） ---
POST /api/approvals HTTP/1.1
Content-Length: 80

{"draft_id": "draft_6794cf928cba4f471715ae5d", "version_no": 1, "confirm": true}

HTTP/1.1 201
Content-Length: 978

{"approval": {"action_id": null, "approved_action_digest": "072aec5648ad3b7857470cc2b64abc978075f71d3d50b6e1508922c65e2a790d", "approver": "user", "auth_channel": "console", "case_id": "case_8fdacd6204ee77588301348f", "consumed_by_attempt_id": null, "created_at_approval": "2026-10-10T08:31:06.737Z", "expires_at": "2026-10-11T08:31:06.737Z", "id": "appr_47356f570b6fe425b4d185cd", "invalidated_reason": null, "revision": 1, "revoked_reason": null, "state": "ACTIVE", "target_object_ids": ["dv_de176cecd77431f3617b6474"], "updated_at": "2026-10-10T08:31:06.738Z"}, "draft": {...}, "superseded_approval_ids": []}
```

注意 `approved_action_digest` 就是该 DraftVersion 的 `content_hash`，
`target_object_ids` 精确指向 `dv_...`，`expires_at` = 批准时间 + 24h（契约 v0.6 的
`DEFAULT_APPROVAL_TTL_SECONDS`），`auth_channel = "console"`、`auth_evidence` 非空
（`approve()` 的硬要求：没有认证证据不给批）。

### 7.4 改：新版本 + 旧审批失效 + 过期编辑 409

```
--- 编辑保存 → 新版本 v2（旧审批必须失效） ---
POST /api/drafts/draft_6794cf928cba4f471715ae5d/edit HTTP/1.1
Content-Length: 174

{"body": "老师您好：\n我会在 10 月 20 日前确认参加（手机端修改）。", "change_reason": "补一句确认时间", "version_no": 1, "expected_revision": 2}

HTTP/1.1 200
Content-Length: 6793

{"case_id": "case_8fdacd6204ee77588301348f", "created_version": {"author_id": "console", "author_kind": "USER", "body": "老师您好：\n我会在 10 月 20 日前确认参加（手机端修改）。", "change_reason": "补一句确认时间", "citations": [], "content_hash": "9e88247006f33216bf5bd23203bcb03f3b3c0d65dc576acb78f9d03b1ee9fa69", "created_at": "2026-10-10T08:28:52.490Z", "draft_id": "draft_6794cf928cba4f471715ae5d", "id": "dv_81baf984c44500ea7aa4c5d7", "parent_version_id": "dv_de176cecd77431f3617b6474", "revision": 1, "summary": "手机控制台编辑", "target_object_id": null, "version_no": 2}, ...}

>>> 旧审批 appr_47356f570b6fe425b4d185cd 现在的状态：INVALIDATED（原因：草稿内容已改变，旧审批失效）
>>> 草稿版本历史：[1, 2]（冲突请求没有产生 v3）
```

```
--- 拿旧版本号再提交一次（必须 409，且不产生新版本） ---
POST /api/drafts/draft_6794cf928cba4f471715ae5d/edit HTTP/1.1
Content-Length: 122

{"body": "拿着旧版本号的第二次提交", "change_reason": "过期编辑", "version_no": 1, "expected_revision": 2}

HTTP/1.1 409
Content-Length: 307

{"error": {"code": "REVISION_CONFLICT", "detail": {"action": "请刷新后基于最新版本重新提交", "current_revision": 2, "expected_revision": 1, "object_id": "draft_6794cf928cba4f471715ae5d", "object_type": "Draft"}, "message": "正在编辑的版本已不是当前版本：提交 v1，当前 v2"}}
```

版本历史仍是 `[1, 2]`：**冲突请求没有写任何东西**。同一语义在"答"上也一样：

```
--- 旧 revision 再提交一次（必须 409 且不写入） ---
{"answer_text": "拿着旧版本再来一次", "expected_revision": 1}

HTTP/1.1 409

{"error": {"code": "REVISION_CONFLICT", "detail": {"action": "请刷新后基于最新版本重新提交", "current_revision": 2, "expected_revision": 1, "object_id": "q_480f0b0a9e426b0b01a3ef5b", "object_type": "Question"}, "message": "Question 已被其他操作修改：提交基于 revision=1，当前是 revision=2"}}

>>> 该追问的 Answer 条数：1（冲突请求没有产生第二条）
```

### 7.5 取：下载失败可重试，且不影响别的文件

```
--- 下载不存在的文件（404 + 可读原因，可重试） ---
GET /api/artifacts/file_evidence_missing/content HTTP/1.1

HTTP/1.1 404
Content-Length: 110

{"error": {"code": "NOT_FOUND", "message": "文件当前不可取用（文件不存在）：可稍后重试"}}

--- 失败之后，别的文件照旧可下载 ---
GET /api/artifacts/file_evidence_good/content HTTP/1.1

HTTP/1.1 200
Content-Type: text/plain; charset=utf-8
Content-Disposition: attachment; filename="confirmation.txt"
X-Artifact-Id: file_evidence_good
X-Artifact-Sha256: 058af193c1f462cd680bd68f4504e6ca058ce4ca3d5eb955864beb4f06ae150d
Content-Length: 44

报名确认：11 月 2 日 09:00 开始。

>>> 待办快照里的传输失败项（transfer_failures>0 需要用户注意）：['file_evidence_missing']

--- 补上文件后重试成功（同一个 artifact） ---
GET /api/artifacts/file_evidence_missing/content HTTP/1.1

HTTP/1.1 200
Content-Type: text/plain; charset=utf-8
Content-Disposition: attachment; filename="not-yet-written.txt"
Content-Length: 15

now it exists

>>> 重试成功后的计数：download_count=1，transfer_failures=1（失败记录保留，需要用户确认才清，避免看起来已经好了）
>>> 重试后仍在待办里的失败项：['file_evidence_missing']
```

设计说明：`transfer_failures` **不因为一次成功就清零**。失败是"发生过的事实"，
清掉会把"还没确认过的失败"从用户眼前抹掉；重试成功只增加 `download_count`。
这一条与 SPEC §9.3「失败可重试」不冲突（可重试已证明），但把"何时算彻底解决"
留给用户确认 —— 见 §9 已知限制。

### 7.6 断线可恢复：增量游标 + 完整快照

```
--- 首次加载：拿到 next_since 与 ETag ---
GET /api/overview HTTP/1.1

HTTP/1.1 200
ETag: W/"2d22a269c1059108155e522c1e34c19e"
Content-Length: 2450

{"etag": "W/\"2d22a269c1059108155e522c1e34c19e\"", "next_since": "2026-10-10T08:28:52.532Z", "pending": {"artifact_ids": ["file_evidence_missing"], "case_ids": [...], "counts": {"approvals": 0, "cases": 4, "drafts": 2, "failed_transfers": 1, "questions": 3, "tasks": 8}, "draft_ids": [...], "items": [{"id": "q_e1ff84e39bc4cda3433cb0ba", "updated_at": "..."}, ...], "question_ids": [...], "task_ids": [...]}, "server_time": "2026-10-10T08:28:52.533Z"}

--- 带 If-None-Match 再来一次（304，不重复传正文） ---
GET /api/overview HTTP/1.1
If-None-Match: W/"2d22a269c1059108155e522c1e34c19e"

HTTP/1.1 304
ETag: W/"2d22a269c1059108155e522c1e34c19e"
Content-Length: 0

--- 增量拉取（只回变更对象 + 完整待处理快照） ---
GET /api/changes?since=2026-10-10T08:28:52.532Z HTTP/1.1

HTTP/1.1 200
ETag: W/"0f8414fa3fdc7e93669177909ed936ba"
Content-Length: 3061

{"changed": {"answers": [{"case_id": "case_5cbd4a5073248a603cebac0b", "id": "ans_d0fb306dfb4b8d9aed83c281", "object_type": "Answer", "question_id": "q_e1ff84e39bc4cda3433cb0ba", "revision": 1, "updated_at": "2026-10-10T08:28:52.536Z"}], "approvals": [], "artifacts": [], "cases": [{"id": "case_5cbd4a5073248a603cebac0b", "object_type": "Case", "revision": 16, "state": "WAITING_FOR_USER", "updated_at": "2026-10-10T08:28:52.537Z"}], "draft_versions": [], "drafts": [], "questions": [{"case_id": "...", "id": "q_e1ff84e39bc4cda3433cb0ba", "object_type": "Question", "revision": 2, "state": "ANSWERED", "updated_at": "..."}], "tasks": []}, "etag": "W/\"0f8414fa3fdc7e93669177909ed936ba\"", "next_since": "2026-10-10T08:28:52.539Z", "pending": {...完整快照...}}
```

关键点：`changed` 只含变更对象（手机断网期间产生的**新事项**不会因为"只拉增量"而漏掉，
因为 `pending` 每次都是完整快照）；`next_since` 单调前进；ETag 命中直接 304。

### 7.7 真机验证（`board`，18090，原始 curl）

设备：`board-host` / aarch64 / Ubuntu 22.04 / `/usr/bin/python3` 3.10.12 / 无 pip。
代码放在 `~/asistent-mvp4`（**完全在 `~/code/asistent` 之外**），库放在
`~/asistent-mvp4/var/data/asistent.db`。

```
=== 运行环境 ===
2026-10-10T16:25:20+08:00
board-host
Python 3.10.12
stdlib ok: 3.37.2

=== 1. token 文件（仓库外，700/600） ===
drwx------ 2 asistent asistent 4096 Oct 10 16:10 /home/asistent/asistent-mvp4/var/config
-rw------- 1 asistent asistent 65 Oct 10 16:10 /home/asistent/asistent-mvp4/var/config/console-token
token 长度: 65 字节（含换行）

=== 1b. 权限不合格时必须拒绝启动（把文件改成 644 试一次） ===
按预期拒绝启动： token 文件权限过宽（需要 600，实际 0o644）: /home/asistent/asistent-mvp4/var/config/console-token —— 请 chmod 600 该文件

=== 2. 在 192.0.2.10:18090 起服务（后台） ===
LISTENING 192.0.2.10 18090

=== 2b. 监听地址（只应绑在内网网卡上，不是 0.0.0.0） ===
LISTEN 0      0        192.0.2.10:18090      0.0.0.0:*    users:(("python3",pid=97657,fd=6))

=== 3. curl 原始请求（raw） ===
--- 3.1 无 token ---
HTTP/1.1 401 Unauthorized
Server: asistent-console 
Date: Sat, 10 Oct 2026 08:25:22 GMT
Content-Type: application/json; charset=utf-8
Cache-Control: no-store
WWW-Authenticate: Bearer realm="asistent-console"
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Content-Length: 80

{"error": {"code": "UNAUTHORIZED", "message": "需要有效的控制台令牌"}}
--- 3.2 错 token ---
HTTP/1.1 401 Unauthorized
（同上，逐字节一致）
{"error": {"code": "UNAUTHORIZED", "message": "需要有效的控制台令牌"}}
--- 3.3 未鉴权访问一个不存在的 Case（不得与「存在」可区分）---
Content-Length: 80

{"error": {"code": "UNAUTHORIZED", "message": "需要有效的控制台令牌"}}
--- 3.4 对 token：/api/health ---
HTTP/1.1 200 OK
Content-Length: 139

{"counts": {"AuditEvent": 0, "Event": 0, "_total": 0}, "ok": true, "server": "asistent-console", "server_time": "2026-10-10T08:25:23.043Z"}
--- 3.5 对 token：/api/overview（待处理快照 + 游标 + ETag）---
HTTP/1.1 200 OK
ETag: W/"b55c30059d50ca07694d28ff02d672a5"
Content-Length: 396

{"etag": "W/\"b55c30059d50ca07694d28ff02d672a5\"", "next_since": "2026-10-10T08:25:23.071Z", "pending": {"approval_ids": [], "artifact_ids": [], "case_ids": [], "counts": {"approvals": 0, "cases": 0, "drafts": 0, "failed_transfers": 0, "questions": 0, "tasks": 0}, "draft_ids": [], "items": [], "max_updated_at": "", "question_ids": [], "task_ids": []}, "server_time": "2026-10-10T08:25:23.071Z"}
--- 3.6 对 token：静态壳（PWA 首页与清单）---
HTTP 200  content-type=text/html  bytes=3278
HTTP 200  content-type=application/manifest+json  bytes=651
HTTP 200  content-type=text/javascript  bytes=27361
--- 3.7 未鉴权拿静态壳（也必须 401）---
HTTP 401  bytes=80
--- 3.8 token 不得出现在日志里 ---
0
（上面必须是 0）

=== 3c. 手机端拿到的 JSON（未鉴权的固定文案） ===
{"error": {"code": "UNAUTHORIZED", "message": "需要有效的控制台令牌"}}

=== 4. 测试 ===
--- 4.1 理解层（回归） ---
Ran 100 tests in 0.812s
OK
--- 4.2 控制台（本层） ---
Ran 123 tests in 9.898s
OK (skipped=2)
--- 4.3 全量（reference 后端） ---
Ran 701 tests in 77.819s
OK (skipped=20)
--- 4.4 全量（sqlite 后端） ---
Ran 701 tests in 87.214s
OK (skipped=15)

=== 5. 收尾 ===
服务已停止，端口已释放
工作目录：/home/asistent/asistent-mvp4（完全在 ~/code/asistent 之外）
```

关于"只对内网暴露"的证据链：

1. `LISTEN 0 0 192.0.2.10:18090` —— 绑的是内网网卡地址，**不是 `0.0.0.0`**；
2. 设备只有一个全局地址 `192.0.2.10/24`（默认路由 `via 192.0.2.1`），没有公网地址；
3. 令牌文件在 700 目录 / 600 文件里，且该目录在仓库之外；
4. 未鉴权请求（含静态壳）全部 401。

### 7.8 纯标准库 / Python 3.10 / 契约零改动

```
$ python -X utf8 -m unittest discover -s tests/console -p test_stack_and_compat.py -t .
Ran 6 tests in 0.156s
OK
```

这个模块做静态扫描：`asistent/console/**` 的 `import` 只允许标准库白名单 + 包内相对导入
（`service.py` 里一个多余的 `import re` 就是这样被扫出来并删掉的）；禁止
`fastapi/flask/django/aiohttp/starlette/uvicorn/...`；前端禁止
`cdn. / unpkg / jsdelivr / node_modules / require(`；禁止
`tomllib / typing.Self / TaskGroup / datetime.UTC / itertools.batched / except*`。
设备上的 3.10.12 真实跑通（§7.7 4.2）。

```
$ git diff --name-only main -- asistent/contracts tests/contracts
（空）

$ git status --short
?? asistent/console/
?? tests/console/
```

说明：在把本分支 rebase 到当时的 `main`（含契约 v0.6）**之前**，上面的 diff 会列出
`asistent/contracts/*` 与 `tests/contracts/*` —— 那是 `main` 侧的 MVP-5/v0.6 改动，
不是本层改的。判断"本层有没有动契约"的正确命令是跟**基线**比：
`git diff --name-only b7aeb75 -- asistent/contracts tests/contracts` → 空。
rebase 之后两个命令都为空。

---

## 8. 本机与设备的差异（为什么跳过项不一样）

| 环境 | 全量结果 | 跳过原因 |
| --- | --- | --- |
| 本机 Windows / 3.12 | 701 OK（reference skipped=16 / sqlite skipped=11） | POSIX 权限位、`os.symlink`、以及"真重启需要落盘库"在 reference 模式下跳过 |
| 设备 Linux / 3.10.12 | 701 OK（reference skipped=20 / sqlite skipped=15） | 反向：跨进程/平台能力用例在设备上会跑，另一些（如无权限位约束的模拟）在设备上跳过 |

本层的 4 个 POSIX 专属用例（token 文件 644 被拒、目录 777 被拒、符号链接 token 被拒、
符号链接越权下载被拒）**在设备上真实执行并通过**（见 §7.7 的 1b 与 4.2 的 `skipped=2`）。

---

## 9. 已知限制（先说清楚，不装作没有）

1. **`transfer_failures` 不会自动清零**。重试成功只加 `download_count`，"失败待确认"
   一直挂在待办里，需要用户显式确认（本层未实现"我看到了"这个动作）。
2. **审批的 `action_id` 默认留空**。因此契约 v0.6 的"同一动作至多一个 ACTIVE 审批"
   在"只审批草稿"这条路径上不生效；同一版本可以并存两张 `ACTIVE` 审批（都能被撤销）。
   带 `action_id` 提交时唯一性由存储层保证，并回 `superseded_approval_ids`。
3. **`WAITING_FOR_USER → WAITING_FOR_CONTEXT` 不合法**，所以"追问答完"之后 Case 状态
   停在 `WAITING_FOR_USER`，只改 `waiting_reason` / `next_action`（§4.1）。手机上看到的是
   "等待 Agent 基于回答重新分析"，而状态机层面还没离开 `WAITING_FOR_USER`。
4. **只支持 `text` 形态的回答**（`required_format` 里其它形态没有专门的输入控件）。
5. **没有推送**。SPEC §9.4 的"通知"只做到"打开页面看到待办"；设备端要主动通知需要
   额外的通道（本层不做，不做假接口）。
6. **没有多用户/多设备会话**。一个令牌 = 这个人的设备；没有会话列表、没有会话撤销
   （换令牌 = 改文件 + 重启服务）。
7. **明文 HTTP**。内网明文 + 令牌，没有 TLS；因此 PWA 的离线壳能力（需要 HTTPS/localhost）
   在设备上不生效。
8. **`allow_remote` 目前只是个开关**，打开后仍需自己保证网络边界；本层不实现 IP 白名单。
9. **审计/日志没有轮转**。日志走 stdout，靠外部（systemd/journald）管；本层不管。

---

## 10. 契约提案（未越界修改，请 Lead 裁决）

### CP 提案 1：`ApprovalState.INVALIDATED/REVOKED` 是终态，"重新批准"只能新建对象

* **背景**：审批因草稿变化被 `INVALIDATED` 后，用户改好内容想"再批一次"。契约里
  `ACTIVE → {CONSUMED, EXPIRED, INVALIDATED, REVOKED}` 全部是终态，没有回边。
* **影响**：控制台每次重新批准都创建**新的** `Approval`；`Case.approval_ids` 会随每次
  重批增长，历史里出现多张"同一版本"的审批记录（其中只有一张 `ACTIVE`）。
* **替代方案**：(a) 允许 `INVALIDATED → ACTIVE` 重开（需要 `revision` 语义与审计约定）；
  (b) 明确"每次批准都必须新对象"并把这条写进契约（现状即如此，只是没写明）；
  (c) 给 `Approval` 加 `supersedes_id`，让"重新批准"有可追溯的前驱。

### CP 提案 2：`Case` 缺少"追问已答完、还没有可发送草稿"的状态

* **背景**：`WAITING_FOR_USER → WAITING_FOR_CONTEXT` 不是合法转移；控制台只能改
  `waiting_reason` / `next_action`，状态留在原地。
* **影响**：手机端"看"到的状态与"下一步"文字不一致（状态是老状态、下一步是新动作）；
  依赖 `Case.state` 做判断的其它模块（如提醒）会把这类 Case 当成"还在等用户"。
* **替代方案**：(a) 允许 `WAITING_FOR_USER → WAITING_FOR_CONTEXT`；
  (b) 新增一个 `WAITING_FOR_AGENT` 之类的中转状态；(c) 明确"这一步只能由 Agent 的
  `ANALYZING` 触发"，让控制台只改文案（现状），并在契约里写清楚。

### CP 提案 3：审批的"唯一激活"是否需要覆盖"未声明动作"的审批

* **背景**：契约 v0.6 规定同一 `action_id` 至多一个 `ACTIVE` 审批。但"用户在手机上批准
  一张草稿"这一刻通常还不知道将来由哪次执行消费它，`action_id` 只能是空。
* **影响**：`action_id` 为空时唯一性约束失效，同一版本可并存多张 `ACTIVE` 审批。
* **替代方案**：(a) 要求控制台必须先有动作身份才能审批（把审批推迟到"发送前一步"）；
  (b) 把唯一性从 `action_id` 提到 `(target_object_ids, content_hash)` 上，即"同一版本同一
  内容只允许一张 `ACTIVE` 审批"；(c) 保持现状并把"空 action_id 不做唯一性约束"写进契约。

### CP 提案 4：`SourceDocument.case_id` 在 `process_document` 之后为空

* **背景**：文档与事务的关联只存在 `Case.source_document_ids` 里，`SourceDocument.case_id`
  是空的，于是控制台不能用统一的 `where={"case_id": …}` 查询，只能特判
  （`_documents_of` 先走 `Case` 的反向索引）。
* **影响**：任何按 `case_id` 过滤 `SourceDocument` 的下游都会漏；控制台这里已绕开，
  但这是"契约字段名骗人"的典型。
* **替代方案**：(a) `process_document` 同时回填 `SourceDocument.case_id`；
  (b) 明确 `case_id` 对文档无意义并加注释，关联只认 `Case.source_document_ids`。

---

## 11. 怎么复现

```powershell
# 1) 本机（Windows / 3.12）
cd <worktree>
python -X utf8 -m unittest discover -s tests/console -t .          # 123 项
python -X utf8 -m unittest discover -s tests -t .                  # 701 项
$env:ASISTENT_STORE="sqlite"; python -X utf8 -m unittest discover -s tests -t .
python -X utf8 tests/console/evidence_dump.py                      # §7.1-7.6 的原始输出

# 2) 设备（Linux / 3.10.12，无 pip）
#    先把工作树打成 tar（文件清单用 LF），scp 到设备 /tmp，解到 ~/asistent-mvp4
ssh board 'mkdir -p ~/asistent-mvp4 && cd ~/asistent-mvp4 && tar -x -f /tmp/asistent-mvp4.tar'
ssh board 'cd ~/asistent-mvp4 && bash tests/console/device_check.sh /home/asistent/asistent-mvp4 192.0.2.10'
```

日常启动（设备上的最小用法）：

```bash
mkdir -p -m 700 ~/asistent-var/config ~/asistent-var/data
openssl rand -hex 32 > ~/asistent-var/config/console-token
chmod 600 ~/asistent-var/config/console-token
python3 -X utf8 tests/console/serve_once.py \
  '{"store":"sqlite","db_path":"/home/asistent/asistent-var/data/asistent.db",
    "token_path":"/home/asistent/asistent-var/config/console-token",
    "port":18090,"bind":"192.0.2.10"}'
# 手机浏览器打开 http://192.0.2.10:18090/ ，把令牌粘进闸门
```

---

## 12. 与 SPEC 的逐条对照

### §842-847（MVP-4 验收）

| 行 | Given / When / Then | 本层怎么满足 | 证据 |
| --- | --- | --- | --- |
| 844 | 后台 Case 运行中，手机上能看到状态 / Agent 状态 / 最近动作 / 等待原因 / 下一步 | `GET /api/cases/{id}` 回 `state`、`waiting_reason`、`next_action`、`last_action_summary`、`agent{running,current_execution_id,recent_executions,running_tasks}`，相关对象带可定位 `citations`（`line:N`） | §7.2、`test_cases.py` 21 项 |
| 845 | Question 待回答，提交 Answer 后进入**正确 Case** 且依赖任务重新评估 | `POST /api/questions/{id}/answer`：写 `Answer` → 追问置 `ANSWERED` → 依赖任务按 `preconditions/inputs/links` 重新评估（`WAITING_USER → READY` 有审计） → 刷新 Case 等待原因 | §7.4 同款语义 + `test_questions.py` 17 项（含"回答落进正确 Case""另一条追问不受影响""依赖任务被重评"） |
| 846 | DraftVersion 可编辑，保存后当前版本变化、旧 Approval 失效 | `POST /api/drafts/{id}/edit` → 新 `DraftVersion`（作者/时间/哈希/原因/引用/父子关系）→ 绑定该草稿的 `ACTIVE` 审批置 `INVALIDATED`；`GET .../diff` 看差异；`restore` 也是新版本 | §7.4 + `test_drafts.py` 14 项 |
| 847 | 生成文件存在，能查看元数据并传输，失败可重试 | `GET /api/artifacts`（元数据，不含路径）、`GET /api/artifacts/{id}/content`（带 `Content-Disposition`/`X-Artifact-Sha256`）；失败 404 + 可读原因 + `transfer_failures+1`，重试成功 | §7.5 + `test_artifacts.py` 11 项 |

### §9.1-9.4（独立控制 / 必备操作 / 文件传输 / 断线）

* §9.1 独立控制：服务跑在**设备本地**，不依赖外网；手机只是浏览器。
* §9.2 必备操作：看/答/改/批/取 五项全部落地（§4 表）。
* §9.3 文件传输：元数据 + 下载 + 失败可重试；**下载需显式白名单**，路径按 `realpath` 校验。
* §9.4 断线与通知：`since` + ETag 增量，重连后看完整快照；通知做"打开就看到"，
  主动推送不做（§9 限制 5）。

### §13.4 / §13.5 / §14.2 / §21.3

* §13.4 权限和网络：默认只绑内网、只收 `Bearer`、未鉴权 401（无信息泄露）、越权 404/403。
* §13.5 提示注入：控制台**只展示**内容，不把任何外部文本当指令执行；所有写操作都是
  用户在界面上显式触发的结构化请求。
* §14.2 独立运行：纯标准库、无 pip 依赖、无 Node、单文件 SQLite。
* §21.3 审批边界：`confirm=true` + `version_no` + 内容哈希三者缺一不可；
  普通请求文本（含"同意/发送"）不构成批准（§7.3）。
