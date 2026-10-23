# ADR-0002：开发位置与部署拓扑

- 状态：已接受（主 Agent 裁决）
- 日期：2026-10-10
- 影响范围：所有 MVP、集成、验收
- 关联需求：R-032、R-033、SPEC §14.1、§21.6.8

## 背景

SPEC 要求「最终运行、部署、维护、故障恢复和真实链路验证必须以 `board:~/code/asistent` 为目标环境」，
但允许多个 Agent 在其他环境开发后部署（§14.1 末段、§20）。

## 决策

1. **开发在工作区**：`D:\dsh\个人助手` 是开发仓库（多 Agent 并行、worktree、快速测试）。
2. **设备是权威目标**：设备上 `~/code/asistent` 初始化为 git 仓库，配置 `receive.denyCurrentBranch=updateInstead`，
   开发机 `git push board` 即**同时更新设备工作区与历史**，禁止只复制最终目录（§21.6.5）。
3. **运行数据分离**：设备上运行数据放设备本地 `~/asistent-var/`（或 `~/code/asistent/var`，由部署脚本决定），
   不随 push 覆盖；`var/` 在 `.gitignore` 内。
4. **常驻方式**：优先 `systemd` 服务（系统级 unit 或 user unit + linger），退化方案 `crontab @reboot + nohup`。
   启停/升级/回滚脚本纳入 `scripts/deploy/`，每次部署记录设备 Git HEAD 与运行配置摘要（不含秘密）。
5. **验证位置**：契约测试在工作区跑；端到端与恢复演练在设备上跑（§15.4.4、§21.7）。

## 影响与代价

- 需要设备可被工作机免密 SSH（已实测：`ssh board` 免密可用，走 `ProxyJump jump`）。
- 设备与开发机 Python 版本不同（3.10 vs 3.12），要求代码保持 3.10 兼容：不用 3.11+ 专有语法
  （如 `typing.Self`、`asyncio.TaskGroup`、`except*`），`datetime.utcnow` 等弃用项改为显式 UTC 感知对象。
- 设备只有一个 checkout，并行原型在**开发机 worktree**上完成后再推送，避免设备上多写者。

## 验证证据

- `ssh -o BatchMode=yes board "uname -a"` 返回 `Linux board-host 6.1.43-rockchip-rk3588 aarch64`。
- 部署通路验证：`scripts/deploy.ps1` 首次推送后，设备 `~/code/asistent` 工作区文件与开发机 HEAD 一致，
  且 `git -C ~/code/asistent log -1` 与开发机一致（由环境角色在 `docs/env/deploy.md` 记录实际输出）。
