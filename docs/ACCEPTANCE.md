# 验收矩阵（SPEC §21.8 AT-01…AT-17）

> 规则：**只写有证据的状态**。没有原始输出的行不写"通过"，只写"待执行/待你的动作"。
> 每行的"证据"列指向可复现的产出（脚本、日志、测试名、设备提交），而不是口头结论。
>
> 最近更新：2026-10-12 · 设备权威目标 `board:~/code/asistent`（部署提交见下行）
> 设备 HEAD：`3f7ca58`，`scripts/deploy.ps1` 六层校验 `RESULT: deploy path verified`，
> 248 个已跟踪文件与提交 blob 逐字节一致，设备套件 `UNITTEST_RC=0`。

| 用例 | 通过条件（要点） | 状态 | 证据 / 缺口 |
|---|---|---|---|
| **AT-01** | 在目标目录跑 24h + 恢复演练；桌面窗口关闭仍完成事务；记录部署提交 | 🟡 **部分** | 部署提交已记录（`f01679f`）；常驻为本机 `systemd --user`（`Linger=yes`），**不依赖任何桌面窗口**——`systemctl --user status asistent` = `active (running)`、`Restart=always`；恢复演练见 AT-17。<br>**24h 起点以进程自身为准，不是"安装时刻"**：服务进程 `started_at = 2026-10-12T08:57:00+08:00`（`run_id = exec_a46b5f24…`），首个取样点 09:23:58 —— 因此 **24h 到 2026-10-13T08:57 才算走完**（08:37 那次是首次安装，08:54 被重配覆盖）。<br>取样已武装（`asistent-soak.timer`，5 分钟一行），结论由 `run-service.sh soak --verdict` 按 **CP-37 预先冻结**的判据机械判定；`--verdict` 会在跨度 < 23.5h 时显式提醒"不能当作 24h 结论"。<br>⚠ 另有一条必须写进结论的事实：**运行中的进程加载的是它启动那一刻的代码**（Python 启动即 import），而设备仓库在此之后又部署了多次，所以 AT-01 的结论要同时写 `run_id` 与"进程启动时的提交"，不能只写"当前设备 HEAD" |
| **AT-02** | 未选 dsh/pi 时，用契约后端完成验证；切换后端旧数据可读、审批边界不变 | ✅ 通过 | 双后端（`reference` 内存实现 / `sqlite` 落盘）跑同一组测试；`tests/storage/test_sqlite_durability.py`、`test_python310_compat.py`；契约测试对两个后端都跑（`tests/contracts/*`） |
| **AT-03** | 不起 Agent 也能编辑资料；60 秒内检索到新版本；旧引用能开当时快照；移动/删除不留死链 | ✅ 通过 | MVP-1：`tests/ingest/**`（含 `test_pipeline_freshness.py` 新鲜度、`test_query_chinese.py` 中文检索、`test_indexer_and_rebuild.py` 快照重建、`test_citations.py` 引用） |
| **AT-04** | 从 UI 查一次回答的实际资料版本/读取记录/写入理由/资产归属；秘密字段不暴露 | ✅ 通过 | MVP-4/6：`tests/console/test_artifacts.py`、`test_drafts.py`；配置描述与快照的泄漏口径见 `tests/service/support.py::SECRET_MARKERS` 与 `tests/service/test_service_mcp_mail.py` |
| **AT-05** | 新邮件/重放/游标失效/Message-ID 缺失或冲突/跨文件夹移动；每个来源消息只有一个原始对象 | ✅ 通过 | MVP-2：`tests/mail/test_mail_sync.py`、`tests/mail/test_mcp_stdio.py`（夹具 `tests/mail/fixtures/*.eml` 含无 Message-ID、断编码、附件、线回复） |
| **AT-06** | 通知关联联系人/历史/附件；缺材料追问；建议与草稿有来源；未知时间不用截止日期替代 | ✅ 通过 | MVP-3：`tests/understand/test_time_semantics.py`、`test_questions.py`、`test_citation_guard.py`、`test_case_flow.py` |
| **AT-07** | 两 worker 抢同一动作 / 手机双击批准 / 发送后落盘前崩溃：**不重复提交**、UNKNOWN 不重发；附件或 Bcc 变化使旧批准失效 | ✅ 通过 | MVP-5：`tests/actions/test_single_execution.py`（跨进程并发单赢家）、`test_approval_binding.py`、`test_crash_recovery.py`、`test_unknown_decision.py` |
| **AT-08** | 手机独立发起/看状态/答追问/编辑/审批；离线重连看到全部待处理；旧版本编辑被拒 | ✅ 通过（设备实测） | 设备实测：控制台 `LISTEN 127.0.0.1:18090`（不绑 LAN、不绑 0.0.0.0；frpc 同机转发）、无令牌 401、`Authorization: Bearer` 后 `/api/overview` 返回真实待处理项；`tests/console/**`（147 项，含暖白主题/PWA/iOS/鉴权节流断言：窗口内连续失败达阈值后连正确令牌也返回 429 + `Retry-After`）。2026-10-23 起公网入口改为 `https://asistent.example.com/`（frp + Caddy，见 [ADR-0006](adr/0006-phone-access-via-frp.md)），**已生效（2026-10-23 15:37 实测）**：Let's Encrypt 证书签发成功、严格证书校验 `Verify return code: 0`、无令牌 401、带令牌 200 且取到暖白主题 CSS、`/api/overview` 200、iPhone Safari 与 Android Chrome UA 均 200；公网只需 80/443，frps 的 18800 从外部为 TIMEOUT。原始输出见 [phone-https-20261023.md](evidence/phone-https-20261023.md) |
| **AT-09** | 邮件→资料→追问→草稿→批准→动作→网盘逐阶段反查同一 Case ID 与来源版本 | 🟡 **部分** | 离线链路已通（`tests/service/test_service_e2e.py`、`tests/understand/test_case_flow.py`）；设备轮次报告里同一 `case_id` 贯穿 8 个阶段。**第一段（邮件，真实通道）已在设备上预演**：故意用无效凭据跑真实 `mcp-email-server`，子进程被拉起、协议握手成功、`sync` 阶段以 `PARTIAL/failed=sync` + `last_error` 响亮失败，没有粉饰成"没有新邮件"（见 [mcp-invalid-credential-20261012.md](evidence/mcp-invalid-credential-20261012.md)）。**最后一段（→网盘）缺 U-2 授权**，故不能写"通过" |
| **AT-10** | 保存纠正并启用后另一通知命中；废止/回滚后行为随版本变化且历史不变；脚本改后无授权不能执行 | ✅ 通过 | MVP-6：`tests/growth/test_rules_lifecycle.py`、`test_rule_conflict.py`、`test_workflow_versions.py`、`test_rule_hits_audit.py` |
| **AT-11** | 闲聊正常回复并生成文件；未确认推断不进事实/长期记忆；手机与桌面浏览器下载同一文件哈希一致；未认证请求被拒 | ✅ 通过 | `tests/console/test_auth.py`、`test_artifacts.py`、`tests/growth/test_memory_validity.py`（`Memory.valid_until`）、`tests/understand/test_citation_guard.py`（无引用只能进 `Inference`/`Question`） |
| **AT-12** | 时间推进触发定期归档；保存已授权记忆与完整事务；重复 archive_id 不产生破坏性副本；失败/配额不足为待归档；逾期告警 | 🔴 **阻塞** | 离线通道已验证：`tests/archive/test_archive_engine.py`（重复 `archive_id`、失败态、逐文件 sha256 回读）。**真实网盘未授权**，只能在 `local_dir` 通道上证明；设备上归档阶段对 bdpan 如实返回 `NOT_AUTHORIZED / retryable=false` 并把 2 个文件按字节级留在本地 staging |
| **AT-13** | 指定一个 chat 归档；清单只含授权快照；新增内容归下一版；未授权 chat/凭据未被上传 | 🔴 **阻塞** | 同上：清单与排除项已由 `tests/archive/test_manifest.py` 覆盖（含 `secret_scan`），真实上传未验 |
| **AT-14** | 从真实网盘下载验算、隔离恢复、重建索引与 Git 历史；哈希/引用/提交一致；历史外发不重放；远端缺文件则拒绝完成 | 🔴 **阻塞** | 离线可验部分：`tests/archive/test_restore.py`（`approvals_invalidated`/`replay_forbidden`、远端缺文件拒绝完成）。真实下载未验 |
| **AT-15** | 冻结基础对象/状态转换/契约测试/冻结提交**在原型派工之前**；原型只用对应共享契约 | ✅ 通过 | 标签 `contracts-v0.1`…`contracts-v0.8`（设备上已存在，`deploy.ps1` 会推 `--tags`）；快照 `contracts/contracts-v0.8.json`；`contracts/CHANGELOG.md` CP 记录；`docs/contracts/CONTRACT.md` |
| **AT-16** | 核对多个原型各自的独立分支/worktree/写集/测试与 commit；越权改共享对象者不得通过 | ✅ 通过 | `docs/dev/OWNERSHIP.md`（所有权表）、`docs/dev/WORKFLOW.md`；8 个 MVP 各自的 `mvp/<name>` 分支 + worktree + `commit` 记录；只有 Lead 提交/合并/部署 |
| **AT-17** | 破坏性/回滚演练可复现：回滚必须能指名目标，且部署仓库不受影响 | ✅ 通过 | 设备实测（2026-10-12，`scripts/at17-rollback-drill.sh`，在 `/tmp` 克隆里做，不动部署仓库）：回滚目标 = **最新冻结标签** `contracts-v0.9` → 840 项 OK；**再退一版** `contracts-v0.8` → 749 项 OK；回 `main` → 842 项 OK；部署仓库 dirty=0、`~/asistent-var` 700 未动。新增两条本演练该守的断言：**最新标签必须等于 `version.py` 的 `CONTRACT_VERSION`**（防止「改了版本号忘了打标签」这种冻结机制悄悄失效），以及**连续两跳回滚**都必须可跑。未授权探针另证：`archive` 只发只读子命令、非只读子命令数 0，未授权时返回 `PERMISSION_FAILURE` 而不是硬闯 |

## 一、怎么复现这些行

```bash
# 1) 设备：部署提交与六层校验（只读，不改设备）
powershell -File scripts\deploy.ps1 -VerifyOnly

# 2) 设备：整套测试（纯标准库，不需要 pip）
ssh board 'cd ~/code/asistent && python3 -m unittest discover -s tests -t .'

# 3) 设备：真实硬件端到端取证（一轮 + 常驻两轮 + 手机审批 + 恰好一次外发 + SIGTERM）
ssh board 'cd ~/code/asistent && sh tests/service/device_run.sh ~/code/asistent'

# 4) 设备：回滚演练（可复现，不动已部署副本）
ssh board 'bash ~/code/asistent/scripts/at17-rollback-drill.sh'
```

## 二、诚实边界（不夸大的地方）

1. **真实百度网盘能力未验证**：`bdpan` 3.8.7 已装好但未授权。上传/下载/删除/配额/限流/覆盖
   的**真实行为全部未验**；`local_dir` 通道只是契约自带的离线回路，**不代表**真实网盘能力。
   未授权时归档如实返回 `NOT_AUTHORIZED`（`retryable=false`），且只发只读子命令。
2. **真实邮箱收发未验证**：服务侧接线已完成（`mail.source=mcp`，含子进程与凭据通路），
   但授权码没到位时它不假装收信，只在状态里说明"未启用 + 下一步动作"。
   真实 IMAP 的 `SINCE` 窗口/限流/字段差异要等 U-5 后才能取证。
3. **24h 墙钟未走完**：常驻能力（自启、linger、崩溃重启、优雅停止）已在设备上验过；
   "连续 24 小时不掉轮"这句话在 2026-10-13T08:37 之前不许说。
4. **时钟**：设备本次开机时钟曾落后约 2 年 3 个月（U-4）。契约的做法是"时间戳不可信就拒写"，
   所以这不会伪造出可信业务时间，但会降低可用性。
5. **`ArchivePolicy`（U-6）未确认**：当前归档不带 `ArchivePolicy`，并在清单里原样记录
   `policy_gate="U-6 未确认…"`，不假装有策略。
