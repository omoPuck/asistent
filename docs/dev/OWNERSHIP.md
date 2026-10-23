# 模块所有权与写集边界（SPEC §15.3、§21.6.1/2/3）

> 目的：让多个 Agent 并行时不互相踩踏。**写集是硬约束**：任何 Agent 不得写自己写集之外的文件；
> 必须改共享契约时，先提交契约变更提案（`docs/contracts/CHANGELOG.md`），由主 Agent 裁决后发布新契约版本。

## 角色

| 角色 | 职责 | 契约变更权 |
|---|---|---|
| 主 Agent（Lead） | 范围裁决、契约裁决、集成门禁、最终验收 | 有（唯一签发者） |
| 基础 Agent | 交付共享对象/状态机/接口/契约测试，冻结 `contracts-v0.8` | 提案 |
| 子 Agent（MVP-x） | 只实现一个 MVP，只写自己的写集，独立测试 + 独立提交 | 只能提案 |
| 集成 Agent | 按 SPEC §15.4 顺序接入主线，逐步测试/审查/提交 | 提案 |
| 审查 Agent | 独立复核（作者不得作为唯一审查者） | 无 |

## 写集表

| 模块 | 路径写集 | 归属 | 依赖 |
|---|---|---|---|
| 共享契约 | `asistent/contracts/**`、`contracts/**`、`docs/contracts/**` | 基础 Agent → 冻结后主/集成 Agent | — |
| 存储层 | `asistent/storage/**` | MVP-0 | 契约 |
| Runtime | `asistent/runtime/**`、`asistent/cli.py` | MVP-0 | 契约、storage |
| 个人资料库 | `asistent/knowledge/**` | MVP-1 | 契约、storage |
| 邮件接入 | `asistent/mail/**` | MVP-2 | 契约、storage |
| 理解/草稿/追问 | `asistent/understand/**` | MVP-3 | 契约、knowledge、mail |
| 手机控制台 | `asistent/console/**`、`asistent/console_ui/**` | MVP-4 | 契约、runtime |
| 外部动作/幂等 | `asistent/actions/**` | MVP-5 | 契约、storage |
| 成长资产 | `asistent/growth/**` | MVP-6 | 契约、storage |
| 百度网盘 | `asistent/pan/**` | MVP-7 | 契约、storage |
| 执行后端适配 | `asistent/backends/**` | MVP-0 | 契约 |
| 测试 | `tests/**`（按 MVP 子目录） | 各 MVP 自己目录 | — |
| 夹具 | `fixtures/**` | 各 MVP 自己子目录 | — |
| 脚本/部署 | `scripts/**`、`docs/env/**` | 环境角色 | — |

## 并发现则

1. 并行原型 **默认最多 3 个** 同时在制（设备资源受限时下调）。
2. 每个原型在自己的 git worktree + 独立分支（`mvp/<name>`）上工作，禁止多个 Agent 共用可写 checkout。
3. 禁止并行修改：`asistent/contracts/**`、`contracts/**`、迁移注册表、`docs/dev/OWNERSHIP.md`、锁文件。
4. 提交信息必须写明：MVP、行为变化、测试命令、未解决问题。
5. 原型提交不等于进主线；必须经主/集成 Agent 审查（`docs/reviews/`）。
