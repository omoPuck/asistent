# 真实 MCP 邮件通道预演：故意用无效凭据（2026-10-12）

目的：在 U-5（163 授权码）真正落地**之前**，把真实邮件通道能提前验证的部分验掉，把剩下
唯一没验的东西收敛到"就是那个授权码"。同时验证一条重要的**失败方向**：凭据不对时必须
如实说"通道有问题"，**绝不能显示成"没有新邮件"**——后者最坏，用户会以为邮箱是空的。

- 日期：2026-10-12T10:23+08:00
- 设备：board（board-host / RK3588 / Ubuntu 22.04 / python 3.10.12）
- 仓库提交：`820d964`
- 隔离手段：独立 `ASISTENT_VAR=~/asistent-var-mcptest`、独立 sqlite 库、控制台关闭、
  外呼与归档关闭；**不触碰**正在常驻的 `~/asistent-var`（该服务全程 `active`，MainPID 未变）
- 凭据：写入 `config/mail-authcode.txt`（600）的**故意无效值**，只走"文件→子进程环境变量"
- MCP 服务器：`~/asistent-spike/venv/bin/mcp-email-server` 1.11.1
- 命令：`ASISTENT_VAR=~/asistent-var-mcptest bash scripts/run-service.sh once`

## 原始输出（关键行）

```
MCP 服务器: /home/asistent/asistent-spike/venv/bin/mcp-email-server （1.11.1）
已写好隔离配置（结构同生产）

===== 跑恰好一轮（前台，最多 90 秒） =====
退出码=1

===== 这一轮提到邮件通道的行 =====
总行数: 12
  [service] 配置 {"archive": {"channel": "none", "enabled": false, ...
  [round 1] sync        FAIL SyncIncomplete: 列取新邮件失败：工具返回错误：Error executing tool
                                   list_emails_metadata: Account owner@example.com was not found
  [round 1] archive     SKIP 归档未启用（archive.enabled=false 或通道为 none）
  [round 1] round=1 PARTIAL phases=8 failed=sync pending={...}
  [service] 退出：共 1 轮，rounds=1 failed=1 sync=PARTIAL waiting=none open_cases=0 questions=0

===== 状态快照 =====
  mail.enabled : True
  waiting_on   : ['none']
  last_error   : {'at': '2026-10-12T02:23:21.436Z', 'error_class': 'AsistentError', 'phase': 'sync',
                  'text': '列取新邮件失败：工具返回错误：Error executing tool list_emails_metadata:
                           Account owner@example.com was not found'}
  note         : ingest.enabled=false：不扫描挂载目录（只索引邮件原文）
  note         : outbound.channel=none：拒绝外呼；已授权动作留在 AUTHORIZED 等通道配置
  note         : archive.enabled=false：不做归档（默认关闭）
  note         : console.enabled=false：不启动控制台（手机端暂时看不到状态）

===== 脱敏：日志/状态里不得出现凭据值 =====
[OK]   日志与状态里没有凭据值
```

## 这次证明了什么

1. **stdio 子进程通道是通的**：报错来自 **工具调用**（`list_emails_metadata`），
   说明我们的客户端成功拉起了 `mcp-email-server` 1.11.1、完成了协议握手（19 个工具）、
   并把凭据经环境变量正确传给了它。U-5 落地后唯一未验的变量就只剩**授权码本身**。
2. **失败是"响的"，不是"哑的"**：`sync` 阶段标记 FAIL，轮次为 `PARTIAL`、
   `failed=sync`，`last_error` 带 phase 与原文。**没有**出现"没有新邮件"这种说法。
3. **脱敏有效**：日志与状态里都搜不到那个凭据值。
4. **配置错误会明确报错**：第一次我把 `store` 写成嵌套对象，服务直接拒绝
   （`未知存储实现：{...}（只支持 reference / sqlite）`），没有静默套默认值——
   这也是"没数据不能装成没问题"的另一面。

## 这次没有证明什么（不能说成通过）

- **真实收信本身**：授权码无效，所以"能读到真邮件"并未验证，AT-02 仍需 U-5；
- 邮件内容解析质量（只在离线夹具上验过：仓库 6 个夹具已全部可读，CP-32）；
- 163 的 IMAP 侧行为差异（例如它对异常登录的返回文案是"Account … was not found"，
  读起来像"账号不存在"而不是"授权码错"——真实的错误语义要等有效凭据才能定性）。

## 复现方式

```bash
scp var/mcp_invalid_cred_test.sh board:/tmp/
ssh board 'sed -i "s/\r$//" /tmp/mcp_invalid_cred_test.sh && bash /tmp/mcp_invalid_cred_test.sh'
```

脚本用完即删（`rm -rf ~/asistent-var-mcptest`），不留凭据、不留第二套运行数据。
