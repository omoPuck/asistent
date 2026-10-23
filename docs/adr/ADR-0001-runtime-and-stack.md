# ADR-0001：运行时语言、存储与依赖策略

- 状态：已接受（主 Agent 裁决）
- 日期：2026-10-10
- 影响范围：全部 MVP、部署、测试
- 关联需求：R-001、R-002、R-004、R-019、SPEC §14、§20

## 背景

SPEC §14.1 要求最终运行/维护/恢复以 `board:~/code/asistent`（RK3588，Ubuntu 22.04）为准；
§20 允许主 Agent 决定语言、持久化和常驻方式，但必须满足对象、状态、幂等、审批、可观察、恢复、网盘与 Git 约束。

## 决策

1. **语言与运行时**：Python 3.10+（设备自带 3.10.12，开发机 3.12.x）。核心（契约、存储、runtime、连接器、控制台）**只使用标准库**，不引入第三方运行时依赖。
2. **持久化**：SQLite（设备实测 3.37.2，`ENABLE_FTS5=True`），WAL 模式，单库 `var/asistent.db`；
   - 业务对象表带 `id / schema_version / revision / created_at / updated_at`；
   - 更新使用 `revision` 预期值做 CAS（`UPDATE ... WHERE id=? AND revision=?`），旧版本写入返回冲突，不静默覆盖（SPEC §21.1）；
   - 审计与事件表只追加（`audit_event`、`event`），修正通过新增更正事件表达（§11.3）；
   - 索引用 FTS5 虚表 + `doc_version`/`content_hash`，索引是派生物，失败标记待重建（§6.2）。
3. **测试框架**：标准库 `unittest`（`python3 -m unittest discover -s tests`），设备零安装即可跑；同时兼容 pytest。
4. **Web 控制台**：标准库 `http.server.ThreadingHTTPServer` + JSON API + 无构建步骤的单页 HTML（PWA 能力用静态 manifest + 原生 fetch），绑定内网地址并要求 Bearer/会话令牌（§9.1、§21.3）。不引入 FastAPI/Node 构建链。
5. **模型后端**：`asistent/backends/` 定义统一 `ExecutionBackend` 协议；默认提供 `EchoBackend`（确定性、供契约测试与离线运行）与 `HttpChatBackend`（OpenAI 兼容 HTTP，`urllib` 实现，凭据来自受保护的本地配置/环境变量）。dsh/pi 作为可选后端后面按同一协议接入（§2.4、§21.1）。
6. **邮件**：`imaplib`/`smtplib` + `email` 标准库实现受控连接器；凭据只从受保护配置文件读取，不进入 Git/日志（§14.4）。
7. **不锁定项**：以标准库为主的实现不排除后续为索引或解析引入可选依赖，但必须保持「核心零依赖可运行」，可选依赖用 `try/except` 降级并显式记录。

## 理由

- 设备无 pip（`python3 -m pip` 不存在），装依赖需要额外系统变更；纯标准库方案免安装、易恢复、易审计，符合「断电/重装后可恢复」。
- SQLite 单文件 + WAL 在手写 CAS、审计追加、崩溃恢复上语义清晰，可直接对应 SPEC 的 revision/幂等/恢复要求。
- 用统一的 `ExecutionBackend` 协议把 dsh/pi/模型服务降级为可替换后端，避免执行后端成为状态唯一载体（§2.4）。

## 被否方案

- **Node.js/TypeScript**：设备无 node（未安装），而 DSH 侧已有 Node 生态；为减少设备侧安装面和长期维护风险，不采用。
- **PostgreSQL/外部数据库**：违背「本地文件系统为事实源、可随时恢复」的取向，运维成本高。
- **FastAPI/uvicorn 控制台**：需要 pip 依赖与 ASGI 运行模型，收益不足以抵消设备侧安装与升级风险。
- **向量数据库作为记忆**：SPEC 明确禁止把记忆只写入不可解释的向量/隐藏缓存（§6.4）。

## 影响与代价

- 需要自己实现路由、鉴权、模板等轻量设施（约数百行），换取零依赖与可审计。
- FTS5 中文分词无内置分词器，先用 `unicode61` + 中文按字符切分（CJK 逐字 bigram）策略，作为 MVP-1 的已知限制记录。

## 验证证据

- 设备实测：`python3 3.10.12` / `sqlite 3.37.2` / `fts5 True` / `git 2.34.1` / `nproc 8` / `Mem 15964MB` / `df 107G 可用`（见 `docs/env/report.md`）。
- 契约测试：`python3 -m unittest discover -s tests`（本 ADR 落地后由契约测试覆盖 CAS 冲突、审计追加、事件去重、状态转换拒绝）。
