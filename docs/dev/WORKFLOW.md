# 协作与 Git 规程（SPEC §15.5、§21.6）

## 分支与 worktree

```
main                     始终是「已通过门禁」的状态
mvp/<name>               每个 MVP 原型：git worktree add .worktrees/<name> -b mvp/<name> main
integration/mvp8         集成分支：一次一个 MVP，按依赖拓扑合入
```

- 一个 Agent 一个 worktree，一个 worktree 只有一个写者。
- `.worktrees/` 不入库；worktree 内的运行数据仍写各自的 `var/`（在 `.gitignore` 内）。
- 合入方式：全项目统一使用 `git merge --no-ff`（不使用 cherry-pick，避免重复变更）。
- 禁止强推、禁止 reset 覆盖已共享历史；回退用 `git revert` 或修复提交。

## 提交规则

- 小步、单一目的；不把无关格式化与功能混在一次提交。
- 共享契约、数据迁移、审批规则、归档格式变更必须**单独提交**。
- 提交前必须本地通过：`python3 -m unittest discover -s tests`。
- 每次提交记录：需求 ID、契约版本、测试命令与结果、已知限制。
- 关键节点打标签：`contracts-v0.8`（当前契约冻结；历史 `contracts-v0.7`、`contracts-v0.6`、`contracts-v0.5`、`contracts-v0.4`、`contracts-v0.3`、`contracts-v0.2`、`contracts-v0.1`）、`mvp-x-v0.1`（原型）、`integration-v0.1`（集成）。
- 同一组契约测试必须在**两种后端**下都稳定通过：偶发红一律按真缺陷处理（先定位是契约没写清还是实现没对齐，禁止重跑掩盖）。
- 测试必须在**默认控制台代码页**下也全绿（Windows cp936 会暴露编码缺陷）：证据输出不得依赖终端编码，
  子进程 stdio 一律显式 UTF-8。合入前 Lead 会在两种后端各跑一次全量。

## 契约冻结门（进入并行原型前必须关闭）

1. `docs/contracts/CONTRACT.md` 正文完成，对象/状态/接口/错误语义无歧义；
2. `asistent/contracts/**` 可导入且 `tests/contracts/**` 全绿；
3. `contracts/contracts-v0.8.json` 快照与代码一致（`scripts/gen_contract_snapshot.py --check` 通过）；
4. 本地 Git 提交 + tag `contracts-v0.8`；
5. ADR 记录未锁定的技术选择与待验证假设。

## 每次集成的固定动作（SPEC §15.4）

1. 检查依赖与接口兼容性；
2. 跑单元/契约/集成/回归测试；
3. 审查权限、幂等、日志、数据迁移；
4. 在 `board:~/code/asistent` 上验证；
5. 记录差异、风险、回滚方式；
6. 形成独立提交；
7. 提交成功后才开始下一次集成。
