# `spike/baidu_pan` — 百度网盘归档契约最小实现（Spike）

零第三方依赖（仅 Python 标准库，`>= 3.10`）。用于在**不接触真实网盘**的前提下，
把 SPEC §12.4 / §12.5 / §21.4 要求的归档与恢复契约跑通并留证。

> 本目录是验证性 Spike 产物，不是 `asistent` 正式模块。
> 真实接入结论、实测证据与需要用户提供的凭据见
> [`docs/spikes/baidu-pan-spike.md`](../../docs/spikes/baidu-pan-spike.md)。

## 文件

| 文件 | 作用 |
|---|---|
| `panclient.py` | `PanClient` 协议（`put/get/list/stat/delete`）+ 错误语义 + `LocalDirPanClient`（本地目录模拟远端） |
| `archive.py` | 不可变 manifest、冻结快照、事务归档（逐文件回读校验 / 幂等重试 / 完成标记）、隔离恢复 |
| `e2e_offline.py` | 端到端契约验证脚本（11 组场景 / 52 项断言），输出可复制的证据行 |
| `bdpan_client.py` | 真实通道适配器：把百度官方 `bdpan` CLI 包成 `PanClient`；**写入默认锁死** |
| `smoke_bdpan_readonly.py` | `bdpan` 适配器**只读**冒烟测试 + 写入闸门验证 |

## `PanClient` 方法命名

```
put(local_path, remote_path, *, overwrite=False) -> RemoteObject
get(remote_path, local_path)                     -> RemoteObject
list(remote_dir)                                 -> list[RemoteObject]
stat(remote_path)                                -> RemoteObject | None
delete(remote_path)                              -> bool
```

错误语义（对应 SPEC §13.1 失败分类）：

| 异常 | 含义 |
|---|---|
| `PanInvalid` | 输入无效（路径穿越、本地文件缺失） |
| `PanNotFound` | 远端对象不存在（确定性失败） |
| `PanConflict` | 远端存在**不同内容**且未获授权覆盖（禁止静默覆盖） |
| `PanUnavailable` | **结果未知**：超时/中断/崩溃，无法证明副作用是否发生 |

`RemoteObject.content_sha256` 只在传输实现**能提供可信内容哈希**时才填充。
`LocalDirPanClient` 会填充；`BdpanCliPanClient` 刻意留空——因为 `bdpan ls --json`
返回的是百度侧 `md5`（大文件为分片 md5 拼接），**不能**作为可信校验依据。
两种情况归档流程都走“下载回读 + 本地重算 sha256”。

## 远端布局

```
个人助手归档/事务归档/<archive_id>/manifest.json
个人助手归档/事务归档/<archive_id>/objects/<relpath>
个人助手归档/事务归档/<archive_id>/_COMPLETE.json   # 完成标记，最后一步写
个人助手归档/恢复记录/<restore_id>.json
```

对应 SPEC §12.4 的建议逻辑目录；真实环境里整个前缀位于百度网盘
`/apps/bdpan/asistent/`（网盘内显示为 `我的应用数据/bdpan/asistent/`）。

## 运行

```bash
# 端到端契约验证（离线，不联网，不接触真实网盘）
python3 e2e_offline.py          # 全部通过 exit 0；-k 保留工作目录便于人工检查

# 只读冒烟：真实 CLI 解析 + 写入闸门（需要本机已安装并登录 bdpan）
python3 smoke_bdpan_readonly.py
```

## 契约保证

1. **不可变 manifest**：上传前生成，含 `archive_id`、逐文件 sha256、大小、来源版本、
   策略版本、代码/规则/流程版本。`archive_id` 由 manifest 身份字段确定性派生——
   相同输入得到相同 id（可安全重试），任何变化得到新 id（旧归档不动）。
2. **冻结快照**：manifest 的哈希与上传字节都取自源文件的冻结拷贝，
   上传期间源文件被改写不影响本次归档（变化进入下一份快照）。
3. **逐文件回读校验**：每个对象上传后立即下载回读并重算 sha256；
   本 attempt 内全部对象通过校验后才写完成标记。复用的对象也必须在本 attempt
   内回读校验，绝不凭本地状态文件“信任”。
4. **幂等重试**：同 `archive_id` 重试复用已完成对象，只补传失败对象。
5. **部分失败 → `ARCHIVE_PENDING`**：记录已成功/未成功对象，不写完成标记，可安全重试。
6. **未知结果不盲目重传**：`PanUnavailable` 记为未成功，重试时先用 `stat` + 回读
   判定真实状态。
7. **篡改可发现**：已完成的归档若远端内容不一致，校验报不一致并拒绝覆盖。
8. **隔离恢复**：`restore()` 的 `protected_roots` 是**必填**参数；逐文件校验哈希、
   校验引用闭合、默认只读恢复模式（历史审批失效、不重放归档中的动作）。

## 已验证环境

| 环境 | 结果 |
|---|---|
| Windows 11 / Python 3.12.14 | `e2e_offline.py` 52/52 通过 |
| **RK3588 (Orange Pi 5 Max, Ubuntu 22.04, aarch64) / Python 3.10.12** | `e2e_offline.py` 52/52 通过；`py_compile` 全部模块通过 |

## 有意未实现（保持 Spike 范围）

- 秘密扫描（SPEC §21.4 要求；本 Spike 只预留 `ArchivePolicy.exclude_globs` 钩子）
- 配额软上限 / 保留策略清理（30 日快照、12 个月快照）
- `Approval` / `ArchivePolicy` 的审批绑定与消费（属基础契约职责）
