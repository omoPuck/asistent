# 部署通路说明（设备 = 权威运行目标）

- 版本：v0.1（首次打通，2026-10-10）
- 关联：ADR-0002（开发位置与部署拓扑）、SPEC §14.1 / §18 环境门 / §21.6.5 / §21.6.6 / §21.6.7 / §21.6.8
- 环境角色：`env-probe`
- 状态：**通路已打通并实测通过**。
  - 首轮（骨架）：设备 HEAD `46d878d009dc010f2a817b4e0ab0eba2fa9e3d19`，8/8 文件与开发机逐字节一致。
  - 真实负载复验（2026-10-10 13:14）：`46d878d..344cae1  main -> main`（fast-forward），
    设备 HEAD `344cae1424b679e26cd57e10997d76be786d2264`、tree `6385abab244be5d1e6cf7ef877831da3ebd45a03`，
    **49/49 文件逐字节一致**，设备工作区 clean，且设备端 **`python3 -m unittest discover` → `Ran 75 tests ... OK`**。

---

## 1. 拓扑与不变量

```
开发机 (Windows, D:\dsh\个人助手  ← 多 Agent 并行开发/测试)
        │  git push board:code/asistent main    (ssh, scp-like 语法)
        ▼
跳板 jump (198.51.100.20)  ──直达 TCP 22 只对跳板开放
        ▼
board = 192.0.2.10 (Orange Pi 5 Max / RK3588 / aarch64 / Ubuntu 22.04)
        └─ ~/code/asistent      ← 唯一权威 checkout（受 Git 管理，push 直接更新历史+工作区）
        └─ ~/asistent-var/      ← 运行数据（仓库外，不随 push 覆盖）
```

不变量（违反任一条都会让部署门失效）：

| # | 不变量 | 理由 |
|---|---|---|
| I1 | 设备 `~/code/asistent` 是**唯一**权威 checkout，禁止多写者 | ADR-0002 §4；SPEC §21.6.3 |
| I2 | 只允许 `git push` 推进设备，禁止「只复制最终目录」 | SPEC §21.6.5 |
| I3 | 设备工作区必须保持 clean；禁止在设备上直接编辑受控文件 | `receive.denyCurrentBranch=updateInstead` 在工作区脏时**拒绝**更新 |
| I4 | 设备当前分支必须等于被推送分支（`main`） | updateInstead 只更新 checked-out 分支 |
| I5 | 运行数据 `~/asistent-var/` 在仓库之外，**不随 push 覆盖，也不随 Git 回滚** | ADR-0002 §3；SPEC §21.6.7 |
| I6 | 禁止强推/重置覆盖已共享历史；回退用 `revert` 或设备侧切回旧提交 | SPEC §21.6.7 |
| I7 | 凭据只放设备本地（`~/asistent-var/config/`，`chmod 700`），绝不进 Git | SPEC §14.4 |

---

## 2. 首次初始化（已在 2026-10-10 实际执行，此处为可复现步骤）

前置：`~/.ssh/config` 中已有 `Host board`（`HostName 192.0.2.10`、`User asistent`、`ProxyJump jump`、免密公钥）。

```bash
# 设备侧（一次性）
mkdir -p ~/code/asistent
cd ~/code/asistent
git init --initial-branch=main
git config receive.denyCurrentBranch updateInstead
```

实测原始输出：

```
$ ssh board 'mkdir -p ~/code/asistent && cd ~/code/asistent && git init --initial-branch=main && git config receive.denyCurrentBranch updateInstead'
Initialized empty Git repository in /home/asistent/code/asistent/.git/

$ git config --local --list | grep -i receive
receive.denycurrentbranch=updateInstead

$ git rev-parse --is-bare-repository ; git symbolic-ref HEAD
false
refs/heads/main
```

> `~/.gitconfig` 在设备上不存在（`fatal: unable to read config file '/home/asistent/.gitconfig'`）→ 设备端没有全局 git 配置，行为完全由仓库本地配置决定，不会因用户既有配置漂移。

---

## 3. 日常部署：一条命令

```powershell
# 在开发机仓库根目录（D:\dsh\个人助手）
#
# 注意：本机实际 shell 是 Windows PowerShell 5.1，系统里没有 pwsh 7（实测 `pwsh` 命令不存在）。
#      因此统一用 powershell -File 调用。
powershell -File scripts\deploy.ps1                     # 推送 + 全套只读校验 + 打印设备 HEAD
powershell -File scripts\deploy.ps1 -VerifyOnly         # 只校验设备现状，不推送
powershell -File scripts\deploy.ps1 -InitVarDir         # 首次额外创建 ~/asistent-var/ 骨架

# 也可在当前 PowerShell 会话里直接调用：
#   & D:\dsh\个人助手\scripts\deploy.ps1 -VerifyOnly
```

> **脚本刻意写成纯 ASCII**（英文注释与英文输出，实测 `non-ascii bytes = 0`）：
> PowerShell 5.1 会把**无 BOM 的 UTF-8 `.ps1` 按 ANSI 解码**，中文会乱码并直接导致解析错误
> （实测报错 `表达式或语句中包含意外的标记"}"` / `字符串缺少终止符`）。
> 用 write/edit 工具改过的文件会**丢掉 BOM**，所以"给脚本加 BOM"不是可靠做法 —— 纯 ASCII 才是。
> 中文说明一律放本文档与 `docs/env/report.md`。

脚本做的事（全部只读，除 push 与可选 `mkdir`）：

1. **预检**：RepoRoot 是 git 仓库；`main` 存在；打印本地 HEAD/tree/标题；工作区有未提交内容时**警告**（未提交内容不会被部署）；`ssh -o BatchMode=yes` 探测设备可达；确认设备 `~/code/asistent/.git` 存在。
2. **推送**：`git -C <repo> push board:code/asistent main`（绝不新建提交、绝不动分支/`.git` 配置）。
3. **设备只读校验**：`rev-parse HEAD` / `rev-parse HEAD^{tree}` / `rev-parse --abbrev-ref HEAD` / `git log -1` / `git ls-files | wc -l` / `git status --porcelain -uall | wc -l` / `git ls-files --others --exclude-standard | wc -l`（未跟踪且未被忽略的文件必须为 0）。
4. **工作区逐文件内容比对**：设备工作区跑 `git hash-object`，与开发机 `git rev-parse HEAD:<path>` 的**已提交 blob 哈希**逐文件比对 → 证明设备工作区字节等于该提交的内容（不只是 ref 一致）。
5. **运行数据目录分离检查**：`~/asistent-var` 存在性/是否符号链接/权限/是否在仓库外，外加 `df -h`。
6. **契约测试可跑性**：设备侧 `python3 -V` + `python3 -m unittest discover`（`tests/` 不存在时降级为「待补项」WARN 而非 FAIL）。
7. **部署记录**：打印部署时间、开发机/设备 HEAD、tree、分支、受控文件数、运行数据目录（不含任何秘密）。

退出码：`0` 全通过 / `1` 预检或推送失败 / `2` 设备侧校验不一致。

---

## 4. 验证方式（六层，缺一层都不算「部署成功」）

| 层 | 命令 | 通过判据 | 本次实测结果 |
|---|---|---|---|
| V1 引用一致 | 设备 `git rev-parse HEAD` | 等于开发机 `main` 的 HEAD | ✅ 骨架轮 `46d878d…`；真实负载轮 `344cae1424b679e26cd57e10997d76be786d2264` |
| V2 树一致 | 设备 `git rev-parse 'HEAD^{tree}'` | 等于开发机 tree | ✅ 骨架轮 `869ae05…`；真实负载轮 `6385abab244be5d1e6cf7ef877831da3ebd45a03` |
| V3 工作区干净 | 设备 `git status --porcelain -uall` | 输出为空 | ✅ 两轮均为空（`updateInstead` 确实完成了 checkout） |
| V4 内容逐字节一致 | 设备 `git hash-object <file>` vs 开发机 `git rev-parse HEAD:<file>` | 全部文件哈希相同 | ✅ 49/49（骨架轮 8/8） |
| V5 测试可跑 | 设备 `python3 -m unittest discover` | 可运行且通过 | ✅ **`Ran 75 tests in 0.076s ... OK`（UNITTEST_RC=0）** |
| V6 无意外残留 | 设备 `git ls-files --others --exclude-standard \| wc -l` | 必须为 0 | ✅ 0（另有 26 个**已被 .gitignore 忽略**的 `__pycache__` 文件，属正常运行产物） |

> V4 为什么比"设备 vs 开发机工作区"更对：能被 push 到设备的只有**已提交内容**，
> 所以期望值取 `HEAD:<path>` 的 blob 哈希。这样开发机上未提交的改动（例如正在改的 `deploy.ps1`）
> **不会**被误报成"设备内容不一致"；而设备侧若发生 smudge/CRLF/编码污染（工作区 ≠ blob）仍会被抓到。
>
> V6 的教训：早期版本用 `find . -type f | wc -l` 统计文件数来发现残留，结果**在设备上跑完契约测试后**
> `__pycache__` 让文件数从 49 涨到 75，产生**误报**（`git status` 是干净的，因为 `__pycache__/` 在 `.gitignore` 里）。
> 现在只把"未跟踪且**未被忽略**"的文件当作问题信号，忽略产物只作信息展示。

补充：设备与开发机的**文件数量**也必须一致（受控 8 / 工作区文件 8），用于发现「push 成功但 checkout 残留旧文件」这类半成品状态。

---

## 5. 运行数据分离（设备侧 `~/asistent-var/`）

| 目录 | 用途 | 是否随 push 覆盖 |
|---|---|---|
| `~/code/asistent/` | 代码、契约模板、脚本（Git 权威副本） | **是**（这正是目的） |
| `~/asistent-var/db/` | 个人数据库（SQLite，WAL） | 否 |
| `~/asistent-var/index/` | 全文索引 / 派生数据 | 否 |
| `~/asistent-var/logs/` | 运行日志、审计日志 | 否 |
| `~/asistent-var/archive/` | 归档暂存队列（百度网盘上传前） | 否 |
| `~/asistent-var/queue/` | 待处理队列 | 否 |
| `~/asistent-var/config/` | 设备本地配置与凭据（`700`，永不入库） | 否 |
| `~/asistent-var/tmp/` | 临时文件 | 否 |

为什么放在仓库**外面**而不是 `~/code/asistent/var/`：仓库内的 `var/` 虽然已在 `.gitignore`（第 2 行）里，但一旦有人执行设备侧 `git clean -xdf` 或 `git reset --hard`（回滚演练），未跟踪的运行数据会被一并删除。放在 `~/asistent-var/` 可以让「代码回滚」与「数据」物理隔离，符合 SPEC §21.6.7「不能把 Git 回滚当作数据自动回滚」。

创建（幂等，`umask 077` + `chmod 700`）：

```bash
umask 077
mkdir -p ~/asistent-var/{db,index,logs,archive,queue,config,tmp}
chmod 700 ~/asistent-var
```

代码内取路径的方式（约定，待 MVP-0 落实）：环境变量 `ASISTENT_VAR_DIR`，缺省 `~/asistent-var`；**禁止**把 `var/` 相对路径硬编码进代码，否则 push 会把运行数据目录拖进仓库。

现状（2026-10-10）：**已按上述骨架创建**（`deploy.ps1 -InitVarDir` 实测，`VAR_MODE=700`）：

```
$ ssh board 'ls -ld ~/asistent-var ~/asistent-var/*'
drwx------ 9 asistent asistent 4096 Oct 10 13:10 /home/asistent/asistent-var
drwx------ 2 asistent asistent 4096 Oct 10 13:10 /home/asistent/asistent-var/archive
drwx------ 2 asistent asistent 4096 Oct 10 13:10 /home/asistent/asistent-var/config
drwx------ 2 asistent asistent 4096 Oct 10 13:10 /home/asistent/asistent-var/db
drwx------ 2 asistent asistent 4096 Oct 10 13:10 /home/asistent/asistent-var/index
drwx------ 2 asistent asistent 4096 Oct 10 13:10 /home/asistent/asistent-var/logs
drwx------ 2 asistent asistent 4096 Oct 10 13:10 /home/asistent/asistent-var/queue
drwx------ 2 asistent asistent 4096 Oct 10 13:10 /home/asistent/asistent-var/tmp
```

---

## 6. 回滚方式

### 6.1 首选：可追溯 `revert` 提交（由 Lead 提交，符合 SPEC §21.6.7）

```bash
# 开发机：找到上一个通过验收的提交
git log --oneline -5
git show --stat <bad-commit>

# Lead 提交 revert（本环境角色不执行提交动作）
git revert <bad-commit>        # 生成新的可追溯提交

# 部署回滚后的结果
pwsh -File scripts/deploy.ps1
ssh board 'git -C ~/code/asistent log --oneline -3'
```

`updateInstead` 会把设备工作区直接 checkout 回旧内容；设备 HEAD 前进到 revert 提交，历史不被重写。

### 6.2 应急：设备侧快速切回旧提交（开发机不可用时）

```bash
ssh board
cd ~/code/asistent
git log --oneline -10                 # 选定旧提交
git reset --hard <旧提交>             # 设备本地回退（不推送、不影响开发机历史）
git rev-parse HEAD
```

回退后设备 HEAD 落后于开发机 `main`，下一次 `git push` 仍是 fast-forward，`updateInstead` 会重新把工作区更新到 `main`；如需长期停留在旧版本，应改为在开发机 `main` 上做 revert（§6.1），不要把设备停在「与分支背离」的状态。

### 6.3 回滚不可跨越的边界

- **数据不随之回滚**：`~/asistent-var/` 独立于 Git；涉及数据兼容性的回滚必须先备份（`sqlite3 .backup` 不可用 → 用 Python `sqlite3` 的 `Connection.backup()` 或 `VACUUM INTO`），并验证旧代码能读新 schema。
- **真实世界副作用不可回滚**：已发送的邮件、已上传的归档不会因为代码回滚而撤销（SPEC §21.5/§21.6.7）。
- **禁止强推**：`git push --force` 一律禁止；设备侧也不要为了「让它接受推送」而清掉工作区改动，先查明改动来源。

### 6.4 push 被拒绝时的处置

`receive.denyCurrentBranch=updateInstead` 在设备工作区脏（或 HEAD 不在被推送分支）时会拒绝：

```bash
ssh board 'git -C ~/code/asistent status --porcelain -uall; git -C ~/code/asistent rev-parse --abbrev-ref HEAD'
```

- 若 `.git` 之外出现设备本地改动：确认这些改动不是别人需要的产物 → `git checkout -- <file>` 或移动出仓库（**不要** `git clean -xdf`，会删掉未跟踪文件）；
- 若分支不是 `main`：`git checkout main` 后再推送。

---

## 7. 首次打通的原始输出（实测留存）

### 7.1 开发机侧

```
$ git -C D:\dsh\个人助手 rev-parse HEAD
46d878d009dc010f2a817b4e0ab0eba2fa9e3d19
$ git -C D:\dsh\个人助手 rev-parse "HEAD^{tree}"
869ae05da5a2f0a4e92f4fbd05b26bfef51f808b
$ git -C D:\dsh\个人助手 ls-tree -r --name-only HEAD | wc -l
8
$ git -C D:\dsh\个人助手 status --porcelain -uall
(空)
```

### 7.2 推送

```
$ git push board:code/asistent main
To board:code/asistent
 * [new branch]      main -> main

# 第二次（幂等性确认，无新提交）
$ git push board:code/asistent main
Everything up-to-date

# 第三次（真实负载：3 个提交、49 个文件，fast-forward）
$ git push board:code/asistent main
To board:code/asistent
   46d878d..344cae1  main -> main
```

> 三条记录分别证明：① 空仓库可接收首个推送并 checkout；② 无变更时幂等；③ **带多文件的 fast-forward 推送会同时更新设备历史与工作区**
> （`updateInstead` 的实际作用点，也说明"回滚 = 推一个 revert 提交"这条路径可预期）。

### 7.3 设备侧校验

```
$ ssh board 'cd ~/code/asistent && git rev-parse HEAD'
46d878d009dc010f2a817b4e0ab0eba2fa9e3d19
$ ssh board "cd ~/code/asistent && git rev-parse 'HEAD^{tree}'"
869ae05da5a2f0a4e92f4fbd05b26bfef51f808b
$ ssh board 'cd ~/code/asistent && git rev-parse --abbrev-ref HEAD && git log --oneline -1 && git status --porcelain -uall | wc -l'
main
46d878d chore(repo): 初始化工程骨架与开发规程
0
$ ssh board 'cd ~/code/asistent && git ls-files | wc -l && find . -path ./.git -prune -o -type f -print | wc -l'
8
8
$ ssh board 'cd ~/code/asistent && git reflog --date=iso | head -2'
46d878d HEAD@{2026-10-10 13:02:02 +0800}: push
```

### 7.4 工作区逐字节一致性（两侧 `sha256sum` / `git hash-object` 摘要）

两侧内容哈希完全一致（左：设备工作区 `sha256sum` 前 16 位；右：开发机同名文件 `sha256` 前 16 位）：

| 文件 | 设备 sha256[:16] | 开发机 sha256[:16] | 字节数 |
|---|---|---|---|
| `.gitignore` | `9409e0d3b9aaa3ca` | `9409e0d3b9aaa3ca` | 628 |
| `README.md` | `c3a801c48e8098af` | `c3a801c48e8098af` | 3204 |
| `SPEC.md` | `1df417a9316d8703` | `1df417a9316d8703` | 66762 |
| `docs/adr/ADR-0001-runtime-and-stack.md` | `7f860216d690d888` | `7f860216d690d888` | 4110 |
| `docs/adr/ADR-0002-dev-and-deploy-topology.md` | `8d22566c09118d10` | `8d22566c09118d10` | 2286 |
| `docs/adr/ADR-0003-external-integrations-and-open-questions.md` | `793427b2c3915512` | `793427b2c3915512` | 3023 |
| `docs/dev/OWNERSHIP.md` | `1a376f79e12d18b8` | `1a376f79e12d18b8` | 2587 |
| `docs/dev/WORKFLOW.md` | `8b3adc198d7b25aa` | `8b3adc198d7b25aa` | 1954 |

### 7.5 设备端 Git 版本与 Python

```
$ ssh board 'git --version'
git version 2.34.1
$ ssh board 'python3 -V'
Python 3.10.12
```

开发机 `git version 2.55.0.windows.5`。`git` 2.34 ↔ 2.55 通过标准 `git-receive-pack` 协议通信，本次实测无告警。

---

## 8. 已知边界与未验证项

| 项 | 状态 | 说明 |
|---|---|---|
| `updateInstead` 拒脏工作区 | **未实测**（需一个新提交才能触发，本环境角色被禁止创建提交） | 行为来自 git 官方文档；`denyCurrentBranch=updateInstead` 在目标工作区有本地修改时拒绝更新。首次真实推送时请确认拒绝信息与 §6.4 处置一致 |
| 契约测试在设备上跑通 | **未满足** | `tests/` 尚未存在；机制已验证（`unittest discover` 在设备上可运行） |
| `~/asistent-var/` 落地 | 待创建 | 目录约定已定，`deploy.ps1 -InitVarDir` 可幂等创建 |
| 设备↔开发机双向直连 | **不可达**（实测） | 开发机→设备 22 端口、设备→`192.0.2.2` 均不通；必须经 `ProxyJump jump`。详见 `docs/env/report.md` §网络 |
| 24h 常驻方式 | 见 `docs/env/report.md` 风险节 | `systemd --user` 可用但 `Linger=no` 且 sudo 需口令；退化方案 = `crontab @reboot` + `setsid nohup` |
| 设备端 Python 依赖 | 系统 python 无 pip | 运行载体按 ADR-0001 约束为**纯标准库**；第三方通道（`mcp-email-server`）走独立 venv，见 `docs/env/report.md` |

---

## 9. Windows 侧操作注意事项（实测踩坑）

1. **远端脚本的最后一行会被追加一个裸 `\r`**（比 CRLF 更隐蔽）：
   把 PowerShell here-string 管道给 `ssh host "bash -s"` 时，最后一行内容变成 `xxx\r`。
   - 若最后一行是命令 → `exit 0` 变成 `exit 0\r`，bash 报 `numeric argument required`；
   - 若在末尾补一个换行 → 末行变成孤立的 `\r`，bash 报 `$'\r': command not found`（exit 127）；
   - 若最后一行是 heredoc 终止符 → `PY\r` 匹配不上 `PY`，报 `warning: here-document at line N delimited by end-of-file`，并把后面的脚本当代码执行。
   - 若最后一行是普通命令（如 `echo "=== END ==="`）→ **不报错**，只是输出末尾多一个不可见 CR（所以早期探测没发现这个问题）。

   对策（`scripts/deploy.ps1` 的 `ConvertTo-Lf` 已内置）：把脚本体归一化为 LF，并**让最后一行成为注释**
   （追加 `\n# __END__`），这样无论 CR 怎么被追加都会被注释吞掉。
2. **`$HOME` 在双引号 here-string 里会被 PowerShell 展开**：`@"... $HOME/xxx ..."@` 会把本机的
   Windows 路径塞进远端脚本（实测导致"设备仓库不存在"的误报）。远端 shell 变量必须写在**单引号** here-string 里，
   或用 `__PLACEHOLDER__` + `.Replace()` 注入。`deploy.ps1` 里所有 bash 片段都用单引号 here-string。
3. **`set -u` 别乱开**：远端脚本开 `set -u` 后 `echo "TZ=$TZ"` 之类未定义变量会直接退出，应写 `${TZ:-unset}`。
4. **stderr 会被 PowerShell 当作错误**：`git push` 把进度写到 stderr，`2>&1` 后可能触发 `NativeCommandError` 显示，
   但 `$LASTEXITCODE`（如 0）才是判据；`deploy.ps1` 用 `$ErrorActionPreference='Continue'` + 显式退出码判断。
5. **不要用 `pwsh`**：本机没有 PowerShell 7。也不要依赖 5.1 没有的参数
   （`ConvertFrom-Json -Depth/-AsHashtable` 等）。
6. **优先 stdin 而非拼引号**：`$s | ssh board "bash -s"` 比 `ssh board "echo '...'"` 稳健得多。

---

## 10. 本轮实测状态与未验证项

| 项 | 状态 |
|---|---|
| push → 设备历史 + 工作区 | ✅ 通过（骨架 8 文件 + 真实负载 49 文件，HEAD/tree/逐文件哈希全一致） |
| `receive.denyCurrentBranch=updateInstead` | ✅ 设备本地配置已确认（`deploy.ps1` 每次校验） |
| `deploy.ps1` 端到端可跑 | ✅ 退出码 0（Windows PowerShell 5.1 实测） |
| `~/asistent-var/` 骨架 | ✅ 已创建，`700` |
| 设备端契约测试 | ✅ **`Ran 75 tests in 0.076s ... OK`**（`contracts-v0.1` 冻结契约在 Python 3.10.12 上全绿） |
| 脏工作区时 push 被拒 | ❌ 未实测（需一次脏工作区 + 新提交触发；流程见 §6.4） |
| 24h 常驻 / 重启后自启 | ❌ 未做，见 `docs/env/report.md` §12（linger 需 sudo；退化方案 `crontab @reboot` + `setsid nohup`） |
