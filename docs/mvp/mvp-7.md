# MVP-7：容错归档 / 隔离恢复 + A-5「构建即发布」（`asistent/pan/**`、`asistent/archive/**`）

- 分支：`mvp/7-archive`（worktree：`D:\dsh\个人助手\.worktrees\mvp-7-archive`）
- 起点：`149a2d9`（contracts v0.5，MVP-2 邮件同步 + SQLite 存储，全套 325 全绿）；
  交付时 main 已前进到 `ef013e6`（契约 v0.6 + MVP-5 合入）——本分支与 main 的 merge-base 仍是 `149a2d9`，
  合并/变基由 Lead 决定（本轮全部改动都在**新增文件**里，预期无冲突，但 v0.6 的契约演进需要复核 §8 的 CP 是否已解决）
- 交付物：`asistent/pan/{__init__,errors,bdpan}.py`、`asistent/archive/{__init__,manifest,engine,restore,release}.py`、
  `tests/archive/**`（80 项测试 + 可脚本化假 `bdpan` CLI + 离线端到端演练 + 真实只读探针）、
  `scripts/release.sh`、本文档
- 结论：本机（Windows / Python 3.12.14）与设备（`ssh board` / `/usr/bin/python3` = **3.10.12**）
  `python3 -m unittest discover -s tests -t .` 在 **reference 与 sqlite 两个后端**都全绿
  （406 项，设备 `OK (skipped=11)`，跳过项全部是"当前目录不是 git 工作树"的发布打包用例，理由显式）；
  离线端到端演练 `python3 tests/archive/e2e_offline.py` **27/27 检查通过**；
  `scripts/release.sh --dry-run` 离线跑通（`exit 0`，幂等复跑 `transfers=0`）；
  **未接入真实网盘**（用户尚未完成 bdpan 授权）：只做只读探测，`NOT_AUTHORIZED` 显式失败且**零写操作**（§4.2）；
  `asistent/contracts/**`、`tests/contracts/**` **零改动**（§6.1）。

---

## 1. 目标与验收对照

### 1.1 SPEC §MVP-7 五行 Given/When/Then

| SPEC（§16 MVP-7） | 状态 | 证据 |
| --- | --- | --- |
| Given 已完成的 Case，When 定期归档任务触发，Then 生成 manifest 并上传事务材料、规则/流程引用和审计摘要 | ✅ manifest 由契约 `build_manifest` + 逐条 `content_origin` 派生，含策略/来源/版本；`ArchiveEngine.archive()` 一步完成上传与回读 | §5.1、§4.2 |
| Given 用户要求归档某个 chat，When 归档完成，Then 网盘中可按 Conversation/Case 找到对话、文件、关联资料和清单 | ⚠️ 离线已验证路径与清单（`<root>/个人助手归档/事务归档/archive-<digest>/{manifest.json,objects/**,…}`），**真实网盘目录未验证**（未授权） | §3.1、§7.1 |
| Given 网盘上传部分失败，When 本地查询 Case，Then Case 为 ARCHIVE_PENDING，能看到已成功/未成功对象并可重试 | ⚠️ 已实现并验证等价语义：`Case.archive_state = PARTIAL`（契约枚举里没有 `ARCHIVE_PENDING`，见 CP-MVP7-1）、`ArchiveJob.pending` 逐条列出未成功对象、重试**只补传失败对象**（计数断言） | §5.3、§8.1 |
| Given 归档目标存在但校验不一致，When 系统验证，Then 不标记归档完成并记录不一致 | ✅ 回读哈希不一致 → `PARTIAL` + 不写完成标记；对"已完成归档被改写"→ `ArchiveIntegrityError` 且**拒绝覆盖**远端 | §5.2、§5.4 |
| Given 本地资料受损，When 用户发起恢复，Then 先恢复到隔离位置，不覆盖主数据，并生成恢复记录 | ✅ `protected_roots` 必填 + 双向重叠即拒绝；逐文件 sha256 + 引用闭合；目标已存在即冲突不覆盖；本地恢复记录必写 | §5.5、§4.2 |

### 1.2 A-5「构建即发布」逐条

| A-5 要求 | 状态 | 证据 |
| --- | --- | --- |
| 快照 → manifest → 上传 → 逐文件哈希回读校验 → 记录归档状态，作为构建一步 | ✅ `python3 -m asistent.archive.release` / `scripts/release.sh` 一次调用完成；状态写入 `release-archive_<digest>.json` | §6.1、§4.3 |
| 可重复执行（幂等）：同一 `archive_id` 重跑不重复上传 | ✅ 重跑 `transfers=0`（本机 sh 入口与服务内 CLI 两处都测） | §4.3、§6.2 |
| 内容变化派生新 `archive_id`，旧归档不被破坏 | ✅ `tests/archive/test_archive_engine.py::test_content_change_derives_new_archive_id_and_leaves_old_archive_intact` | §5.6 |
| 部分失败可见、可重试，且只补传失败对象 | ✅ 计数断言：首轮尝试 4/4，重试只尝试失败的 2 个（`second_attempt_object_uploads=['a.md','c.md']`） | §4.2、§5.3 |
| 结果未知先 `stat`/回读再决定（不盲目重传） | ✅ 超时（副作用已发生）→ 重试零上传；超时（无副作用）→ 重试只补 1 个 | §5.3 |
| 构建日志/输出里不得出现凭据 | ✅ 输出断言不含 `BDUSS`/`cookie`/`.config/bdpan`；`approval_ref` 只是审批记录 ID，不读 bdpan 配置 | §5.7 |
| `--dry-run` 离线可跑（可验证打包 + manifest） | ✅ `RELEASE_OUT=/tmp/x sh scripts/release.sh --dry-run` → `state=VERIFIED exit=0`（本地目录模拟远端，不需要授权、不装 bdpan） | §4.3、§6.2 |

### 1.3 本轮三条抽查点

| 抽查点 | 落实 | 证据 |
| --- | --- | --- |
| ①「上传完 = 成功」是禁止的：必须逐文件哈希回读；`METADATA_PROJECTION` 只能声称"投影一致" | ✅ 每个对象上传后必然 `stat` → 远端 `content_hash`（或下载重算）比对；claim 显式三分：字节级 / 仅投影 / 未声明，并带"不得视为字节级证明"警告 | §5.2、§5.8 |
| ② 部分失败只补传失败对象（有传输次数断言）；未知先 stat/回读；未授权必须显式 `NOT_AUTHORIZED` | ✅ 计数断言见 §5.3；未授权 → `state=FAILED,error_code=NOT_AUTHORIZED,retryable=False` 且**零写调用**（`calls_of("upload") == []`） | §5.3、§5.4、§4.2 |
| ③ A-5 发布入口 `scripts/release.sh` 在 `--dry-run` 下也能跑（离线可验证打包 + manifest） | ✅ 见 §4.3；无 git 的机器上"打包 + manifest + 回读校验"这条链另有不依赖 git 的用例 | §4.3、§6.2 |

---

## 2. 范围与交付物

| 文件 | 作用 |
| --- | --- |
| `asistent/pan/bdpan.py` | 真实通道：`bdpan` CLI 适配器（**复用** `spike/baidu_pan/bdpan_client.py` 的调用约定与经验，没有另写通道） |
| `asistent/pan/errors.py` | 通道错误语义：`PanNotInstalled` / `PanNotAuthorized(NOT_AUTHORIZED)` / `PanUnavailable(EXTERNAL_RESULT_UNKNOWN)` / `PanWriteLocked(APPROVAL_REQUIRED)` |
| `asistent/archive/manifest.py` | 捕获 → 冻结快照 → manifest（逐条 `content_origin`、策略、秘密扫描、`manifest_digest` 复算） |
| `asistent/archive/engine.py` | 归档状态机：上传 → 逐文件回读 → 完成标记；复用 / 修复 / 补传 / 未知判定；状态落盘与落库 |
| `asistent/archive/restore.py` | 隔离恢复：逐文件哈希 + 引用闭合 + 索引重建 + 恢复记录（只读、无重放） |
| `asistent/archive/release.py` | A-5 发布：产物打包（源码包 + 完整历史 bundle）→ manifest → 归档 → 退出码 |
| `tests/archive/fake_bdpan.py` | 可脚本化假 `bdpan` CLI（故障注入 + 调用日志 + 真实 argv 记录），**测试绝不调用真机 bdpan** |
| `tests/archive/{support,test_pan_channel,test_manifest,test_archive_engine,test_restore,test_release}.py` | 80 项测试 |
| `tests/archive/e2e_offline.py` | 离线端到端演练（归档 → 校验 → 篡改 → 恢复 → 重建索引 → 幂等 → 部分失败） |
| `tests/archive/probe_real_bdpan_readonly.py` | 对真实 bdpan 的**只读**探针（授权后也会拒绝写入） |
| `scripts/release.sh` | A-5 发布入口（POSIX sh，`--dry-run` 离线可跑，退出码透传） |

写入范围之外的文件（`asistent/contracts/**`、`tests/contracts/**`、`scripts/deploy.ps1`、`scripts/test.*`、
其他 `docs/mvp/*`）**未改动**（§6.1）。

---

## 3. 设计要点

### 3.1 远端布局与通道边界

```
/apps/bdpan/                          ← bdpan CLI 强制的应用目录（spike 已实测）
└── asistent/                         ← root_prefix（可配置，默认 "asistent"）
    └── 个人助手归档/
        ├── 事务归档/archive-<archive_id 摘要>/
        │   ├── manifest.json         ← 唯一可信依据（逐文件 sha256 + content_origin）
        │   ├── objects/<相对路径>     ← 内容对象，路径与 manifest 逐条对应
        │   └── _COMPLETE.json        ← 完成标记（含 integrity_claim 摘要）
        └── 恢复记录/...              ← 恢复记录（需审批引用才发布）
```

- `archive_id = archive_id_for(scope, scope_ref, snapshot_digest)`（契约函数）；**内容变 → id 变**，
  因此"同一 id 重跑"天然等价于"同一份内容重跑"，幂等判定不依赖本地状态文件。
- 所有远端路径都经过 `normalize_remote()`（拒绝 `..`、空路径、根路径），不存在路径穿越面。
- 通道写入默认**锁死**（`writes_locked=True`），必须由上层用 `approval_ref` 显式解锁（审批引用，非凭据）。

### 3.2 「上传完 ≠ 成功」

每个内容对象走同一条判定链：

```
stat(远端) ── 存在且哈希一致 ─→ 复用（本 attempt 内仍计入"已回读校验")
     │            └─ 存在但哈希不一致 ─→ 归档未完成时修复重传；已完成时 → ArchiveIntegrityError（拒绝覆盖）
     └─ 不存在 / 未知 ─→ put → 回读（stat.content_hash 或 download + 本地重算 sha256）
                             ├─ 一致 → verified
                             └─ 不一致/未知 → 记入 failed（不写完成标记）
```

- 数量一致、大小一致都**不算**通过：`test_size_only_match_is_not_acceptance` 用"同尺寸不同内容"证明靠哈希发现。
- 只有全部对象 `verified` 且 manifest 落地后，才写 `_COMPLETE.json`；标记里带 `verification`、
  `integrity_claim`、`content_origin_summary`，便于日后只读复核。

### 3.3 失败 / 未知 / 重试的语义（三条硬规则）

1. **确定失败**（`result_class=FAILED_SAFE`，例如"文件不存在"、errno=-8/-9）→ 记 `put-failed`，可安全补传。
2. **结果未知**（`UNKNOWN`：超时、崩溃、被 kill、无法识别的 errno、编码不可逆的错误文本）→ 记 `put-unknown`，
   **下一次运行先 `stat`/回读判定远端真实状态**：内容已一致 → 零重传；确实缺失 → 只补这一个。
   - 兜底规则：非零退出且**没有可识别的失败语义**时一律报"未知"，绝不猜成"确定失败"（否则会掩盖已发生的写入）。
3. **未授权 / 未获审批**（`NOT_AUTHORIZED` / `APPROVAL_REQUIRED`）→ 显式失败状态 + `next_step`，
   `retryable=False`（未授权）且**一次写调用都不发**（测试断言 `calls_of("upload") == []`）。

### 3.4 部分失败：只补传失败对象

`ArchiveOutcome` 记录 `uploaded / reused / repaired / failed / unknown_objects / transfer_count`，
`ArchiveJob.pending` 逐条列出未成功对象；重试时对象级判定保证只有"缺失或不一致"的对象会被上传。
离线演练的原始计数：首轮 `attempted=4`，重试 `attempted=2` 且 `['a.md','c.md']`（§4.2）。

### 3.5 来源声明：`RAW_BYTES` vs `METADATA_PROJECTION`

- 逐条声明 `content_origin`，且**纳入 `manifest_digest`**（只改来源声明也会改变摘要，防"投影冒充字节级"）。
- `integrity_claim(manifest, verified)` 把已校验对象分成三堆：
  `byte_level_verified`（RAW_BYTES）/ `projection_only_verified`（METADATA_PROJECTION 或未声明）/ `undeclared_origin_verified`，
  并给出 `statement` 与 `warning`（"仅在投影层一致，**不得**当作字节级完整性证明"）。
- 恢复记录同样带这份 claim，所以"恢复成功"这句话永远带着"哪一部分是字节级证明"的限定。

### 3.6 隔离恢复（绝不覆盖主数据）

- `protected_roots` **必填**；恢复目标与任一受保护目录**双向**重叠即 `ValidationError`，且不读取任何远端字节。
- 恢复期间锁死通道写入（调用日志里没有 `upload`/`rm`/`mkdir`），结束后把调用方原本的写锁状态原样交还。
- 逐文件 sha256 + 引用闭合（隔离目录不得出现 manifest 之外的文件）；已有不同内容 → `CONFLICT` 且不覆盖。
- 回读哈希不一致的字节**不留在隔离目录**（删掉并记录），避免"坏数据冒充恢复结果"。
- 索引重建是注入钩子：传了却失败 → `INDEX_FAILURE`，恢复**不算成功**（资料落盘但检索不到 = 没恢复）。

### 3.7 A-5 发布入口

```
scripts/release.sh [--repo R] [--out O] [--label L] [--python P] [--dry-run|--approval-ref ID|--json|--state-dir D|--mirror M]
```

- 打包：`git archive HEAD` → `<label>-src.tar.gz`；`git bundle create` → 完整历史 `<label>.git.bundle`（失败即阻断，退出码 4）。
- manifest：两件产物都是**我们自己生成的字节** → `content_origin=RAW_BYTES`，可做字节级校验；
  附 `code_version`（= HEAD）、`contract_version`、`dirty_worktree`、`release` 元数据。
- `--dry-run`：归档回路走契约自带的 `LocalDirPanClient`（本地目录模拟远端），**不接触真实网盘、不要求授权**，
  因此"打包 + manifest + 回读校验 + 状态记录"在离线环境可完整验证；`--mirror` 指定固定目录即可验证幂等。
- 退出码：`0` 校验通过 / `2` 部分失败·待重试 / `3` 未授权·未获审批 / `4` 配置或完整性错误。

---

## 4. 原始证据

### 4.1 双后端全套测试

本机（Windows / Python 3.12.14）：

```text
> C:\Users\<you>\.conda\envs\<env>-a29d9cc4\python.exe -m unittest discover -s tests -t .
Ran 406 tests in 126.069s
OK (skipped=5)

> $env:ASISTENT_STORE="sqlite"; python.exe -m unittest discover -s tests -t .
Ran 406 tests in 121.181s
OK (skipped=5)
```

> 说明：本机 5 项跳过是既有模块里与平台相关的条件跳过（与本轮新增无关）；设备上这些项不跳过。

设备（`ssh board` / `/usr/bin/python3` 3.10.12，源码树拷到 `/tmp/mvp7-final/repo`）：

```text
$ python3 -m unittest discover -s tests -t .
Ran 406 tests in 64.787s
OK (skipped=11)

$ ASISTENT_STORE=sqlite python3 -m unittest discover -s tests -t .
Ran 406 tests in 65.764s
OK (skipped=11)
```

跳过的 11 项全部是发布打包用例，理由是显式的（设备目录不是 git 工作树，且**遵守"设备上不执行 git 写命令"**，
没有为了跑测试而 `git init` 一个临时仓库）：

```text
test_snapshot_produces_source_bundle_and_manifest (tests.archive.test_release.ReleaseTest) ... skipped '当前目录不是 git 工作树：跳过依赖 git 打包的发布用例'
...（共 11 条同类）
```

同一台设备上，**不依赖 git 的打包链**由 `ReleaseWithoutGitTest` 覆盖（见 §6.2）。

### 4.2 离线端到端演练（`tests/archive/e2e_offline.py`）

Windows 与设备（3.10.12）两个环境都是 `checks=27/27`。设备原始输出（一次真实运行，`archive_id` 随内容/时间变化）：

```text
=== 0. 准备本地源目录（含子目录、中文内容、一个凭据类文件） ===
source=/tmp/asistent-e2e-7ztlinjh/source
fake_bdpan=/tmp/fake-bdpan-im69hj7t/fake_bdpan.py
remote_root=/tmp/fake-bdpan-im69hj7t/remote
[PASS] 凭据类文件被排除、快照秘密扫描干净 :: excluded=['credentials'] scan_clean=True
archive_id=archive:9fadbc9ec4ee037834567a155e2a019fba0f5d2fab6d49d32a54aa2276631898
manifest_digest=0c3ae94fce9a8abe6e122e8e088cee98bac275a31b283dd22a5e33064e8fa737
content_origin_summary={"RAW_BYTES": 3, "METADATA_PROJECTION": 0, "undeclared": 0, "byte_level_verifiable": 3, "projection_only": 0}

=== 1. 归档：上传 → 逐文件哈希回读校验 → 写完成标记 ===
archive_id=archive:9fadbc9e… state=VERIFIED attempt=1 objects=3 uploaded=3 reused=0 repaired=0 failed=0 unknown=0 transfers=5 marker=yes
claim={"byte_level_verified": ["notes/a.md", "notes/b.md", "top.txt"], "projection_only_verified": [], "undeclared_origin_verified": [], "byte_level_count": 3, "projection_only_count": 0, "statement": "逐文件回读 sha256 校验通过 3 项：其中 3 项为字节级（content_origin=RAW_BYTES），0 项仅为投影一致（METADATA_PROJECTION），0 项来源未声明（按投影级对待）"}
[PASS] 归档到达 VERIFIED :: VERIFIED
[PASS] 每个对象都做了回读（下载次数 >= 对象数） :: downloads=4
[PASS] 完成标记已写且声明逐文件 sha256 校验
[PASS] 三件内容对象 + manifest + 完成标记 = 5 次上传 :: transfers=5 uploaded=3

=== 2. 只读校验（不做任何写入） ===
consistent=True verified=3 missing=[]
[PASS] 只读校验一致
[PASS] 只读校验过程零上传/零删除 :: uploads=0 rm=0

=== 3. 篡改检测：远端对象被改写 ===
mismatched=['top.txt']
[PASS] 同尺寸不同内容仍被哈希发现
[PASS] 篡改后状态不再一致
restore_id=rstr_73eba89b13b6db … state=CONFLICT files=3 verified=2 mismatched=1 missing=0 conflicts=0 reindex=no read_only=True
[PASS] 恢复遇篡改 → CONFLICT :: CONFLICT
[PASS] 不一致的字节不留在隔离目录

=== 4. 修复远端后恢复：下载 → 逐文件哈希 → 索引重建（无写入/无重放） ===
restore_id=rstr_d1e840986d8abc … state=VERIFIED files=3 verified=3 mismatched=0 missing=0 conflicts=0 reindex=yes read_only=True
verified=['notes/a.md', 'notes/b.md', 'top.txt']
reindex={"indexed": 3, "index": "/tmp/asistent-e2e-7ztlinjh/index/search.jsonl"}
record=['/tmp/asistent-e2e-7ztlinjh/isolate/restore-1/RESTORE_RECORD.json']
[PASS] 恢复到达 VERIFIED :: VERIFIED
[PASS] 引用闭合 + manifest 摘要复算通过
[PASS] 索引重建完成
[PASS] 恢复期间零写入/零删除
[PASS] 恢复是只读模式且不重放外部动作
[PASS] 主数据目录未被触碰
[PASS] 恢复出的文件内容与源一致 :: ['notes/a.md', 'notes/b.md', 'top.txt']

=== 5. 幂等：同一 archive_id 重跑 ===
archive_id=archive:9fadbc9e… state=VERIFIED attempt=2 objects=3 uploaded=0 reused=3 repaired=0 failed=0 unknown=0 transfers=0 marker=no
[PASS] 重跑零上传、全部复用 :: transfers=0 reused=3
[PASS] 复用对象仍然重新回读校验 :: downloads=6

=== 6. 部分失败：只补传失败对象（计数断言） ===
archive_id=archive:30a8624a0496423955039b4be921a262cd3c5a3201c2b4eda6c433bbc0b50896 state=PARTIAL attempt=1 objects=4 uploaded=2 reused=0 repaired=0 failed=2 unknown=0 transfers=4 marker=no
[PASS] 部分失败 → PARTIAL，且失败对象逐条可见 :: ['notes/a.md', 'notes/c.md']
[PASS] 部分失败不写完成标记
[PASS] 第一次尝试确实尝试了全部 4 个对象 :: attempted=4
archive_id=archive:30a8624a… state=VERIFIED attempt=2 objects=4 uploaded=2 reused=2 repaired=0 failed=0 unknown=0 transfers=4 marker=yes
second_attempt_object_uploads=['a.md', 'c.md']
[PASS] 重试后 VERIFIED :: VERIFIED
[PASS] 第二次只补传失败的 2 个对象（不是全部 4 个） :: 2
[PASS] 未失败对象全部复用 :: ['top.txt', 'notes/b.md']

=== 7. 秘密拦截：manifest 构建阶段就拦住凭据 ===
[PASS] 含凭据的快照被拒绝归档 :: ['notes/leak.txt']

=== 汇总 ===
checks=27/27
```

### 4.3 A-5 发布入口 `scripts/release.sh --dry-run`（离线）

本机用 Git 自带 POSIX `sh` 直接跑真实仓库（`git archive` / `git bundle` 只读仓库、产物写到临时目录）：

```text
$ RELEASE_OUT=/tmp/mvp7-release sh scripts/release.sh --dry-run --label local-dryrun2 \
      --mirror /tmp/mvp7-mirror --state-dir /tmp/mvp7-state
release: repo=/d/dsh/.../.worktrees/mvp-7-archive out=/tmp/mvp7-release label=local-dryrun2 python=3.12.14
[plan] label=local-dryrun2 commit=149a2d9b8b93 archive_id=archive:7cfd68eebbf98473408b1a155b4b406f42da73383654b5d4230eab6b6cfd9d2c
[plan] artifacts=['local-dryrun2-src.tar.gz', 'local-dryrun2.git.bundle']
       - local-dryrun2-src.tar.gz 420495B sha256=403cfd5f286a2f36…
       - local-dryrun2.git.bundle 719172B sha256=da53c84e351b1256…
[plan] manifest_digest=ca404df0b852e16f9c5b09bfe52a0cee0d1e1b9270665eaae78b43963fb71845
[plan] content_origin={'RAW_BYTES': 2, 'METADATA_PROJECTION': 0, 'undeclared': 0, 'byte_level_verifiable': 2, 'projection_only': 0}
[run ] release state=VERIFIED … transfers=4 verified=2 uploaded=2 reused=0 failed=0 unknown=0 dry_run=True
[run ] integrity_claim: 逐文件回读 sha256 校验通过 2 项：其中 2 项为字节级（content_origin=RAW_BYTES），0 项仅为投影一致（METADATA_PROJECTION），0 项来源未声明（按投影级对待）
[state] /tmp/mvp7-state/release-archive_7cfd68eebbf98473408b1a155b4b406f42da73383654b5d4230eab6b6cfd9d2c.json
exit=0

# 幂等复跑（同一个 --mirror，同一份产物）
[run ] release state=VERIFIED archive_id=archive:7cfd… transfers=0 verified=2 uploaded=0 reused=2 failed=0 unknown=0 dry_run=True
exit=0
```

设备侧（3.10.12）对同一脚本做 POSIX 语法检查（**未**在设备上创建 git 仓库）：

```text
$ sh -n scripts/release.sh && echo SH_SYNTAX_OK
SH_SYNTAX_OK
```

### 4.4 真实 bdpan 只读探针（`tests/archive/probe_real_bdpan_readonly.py`）

用户尚未完成 bdpan 授权，因此**只做只读探测**，一次真实写操作都没有尝试：

```text
== 1. 只读探测（bdpan version + whoami） ==
{
  "auth_state": "NOT_AUTHORIZED",
  "binary": "/home/asistent/.local/bin/bdpan",
  "detail": "version exit=0 whoami exit=0",
  "installed": true,
  "logged_in": false,
  "version": "bdpan: 3.8.7"
}

== 2. 授权的可执行判定（require_authorized，只读） ==
writes_locked=True
NOT_AUTHORIZED（显式失败，不是静默跳过）
{
  "code": "NOT_AUTHORIZED",
  "failure_class": "PERMISSION_FAILURE",
  "message": "bdpan 未授权（未登录或令牌已过期）：拒绝执行归档写入",
  "detail": {"retryable": false, "next_step": "待用户授权：在设备上执行 `bdpan login --get-auth-url --accept-disclaimer` …"}
}

== 3. 只读列目录（不写任何东西） ==
list('个人助手归档') -> 0 项（目录不存在时按 0 项处理）

== 4. 实际发出的子命令（必须只有只读子命令） ==
  /home/asistent/.local/bin/bdpan ls asistent/个人助手归档 --no-check-update --json
非只读子命令数量 = 0（必须为 0）
```

同一语义在单元测试里也被锁住（不依赖真机）：
`test_not_authorized_fails_explicitly_with_zero_writes`、
`test_write_lock_failure_is_explicit_and_retryable_after_approval`、
`test_write_lock_and_not_authorized_are_explicit_with_zero_writes`。

### 4.5 新增测试清单（80 项）

| 模块 | 项数 | 覆盖 |
| --- | --- | --- |
| `tests/archive/test_pan_channel.py` | 18 | 通道协议一致性、写锁与审批引用、只读探测、errno 分类、超时=未知、`--no-check-update`/`--json`、路径穿越、凭据不进 argv |
| `tests/archive/test_manifest.py` | 14 | 冻结快照与漂移检测、来源声明注入与摘要覆盖、秘密拦截、策略审批、引用闭合、篡改检测 |
| `tests/archive/test_archive_engine.py` | 20 | 正常路径逐文件回读、同尺寸不同内容、部分失败只补传（计数）、未知先 stat、崩溃=未知、manifest 失败可重试、幂等、新内容派生新 id、投影声明、篡改拒绝覆盖、同名不同内容修复、上传冲突(errno=-10)记确定失败、未授权零写入、状态落盘落库 |
| `tests/archive/test_restore.py` | 17 | 隔离边界（必填/双向重叠）、恢复期零写入、索引重建失败、冲突不覆盖、引用闭合、缺失对象、投影声明、记录与落库、写锁状态交还 |
| `tests/archive/test_release.py` | 12（1 项不需要 git） | 产物 + manifest、bundle 失败阻断、dry-run 离线闭环与幂等、部分失败只补传、未知先回读、未授权零写入、退出码表、凭据纪律、状态记录、无 git 打包链 |

---

## 5. 关键实现细节

### 5.1 归档一步（`ArchiveEngine.archive`）

```
校验 manifest（规范化路径/逐条 content_origin/digest 复算）→ 写 PENDING 状态
→ require_authorized()（只读探测；未授权 → FAILED/NOT_AUTHORIZED，零写入）
→ 逐对象：stat → 复用/修复/上传 → 回读校验
→ 全部 verified 后：manifest 对象 → 完成标记
→ 状态落盘（archive-<id>.json）+ 落库（ArchiveManifest/ArchiveJob/Case.archive_state）
```

### 5.2 回读校验的三种判定

| 判定 | 依据 | 结果 |
| --- | --- | --- |
| 一致 | 远端 `stat.content_hash`（若有）或下载后本地重算 sha256 == manifest 条目 | 计入 `verified` |
| 不一致 | 同上但不等 | `verify-mismatch`（归档未完成 → 可修复重传；已完成 → `ArchiveIntegrityError` 拒绝覆盖） |
| 无法判定 | 通道抛 `PanUnavailable` | 记入 `unknown_objects`，不写完成标记 |

### 5.3 结果未知的处理（两种副作用都测）

| 场景 | 首轮 | 重试行为 | 测试 |
| --- | --- | --- | --- |
| 上传超时但**已写入**（`timeout_effect=write_then_hang`） | `PARTIAL`，`unknown_objects=['a.txt']` | 回读一致 → **零上传**（`_object_uploads == []`） | `test_unknown_upload_is_resolved_by_stat_and_not_reuploaded` |
| 上传超时且**无副作用**（`timeout_effect=none`） | `PARTIAL`，`unknown_objects=['a.txt']` | 判定缺失 → 只补传 1 个 | `test_unknown_without_side_effect_is_uploaded_on_retry` |
| 上传进程崩溃（exit 9，无可识别错误语义） | `PARTIAL`，`unknown_objects=['b.txt']` | 同上 | `test_crash_mid_upload_is_unknown` |
| 泛化错误（未知 errno，无法确定副作用） | `PARTIAL`，`put-unknown` | 同上 | `test_generic_upload_error_without_semantics_is_unknown` |

### 5.4 未授权 / 未获审批

| 场景 | 状态 | 错误码 | 退出码 | 写调用数 |
| --- | --- | --- | --- | --- |
| 真实网盘未授权 | `FAILED` | `NOT_AUTHORIZED` | 3 | 0 |
| 通道写锁未解锁（无 `approval_ref`） | `FAILED` | `APPROVAL_REQUIRED` | 3 | 0 |
| 归档内容被篡改后重跑 | 抛 `ArchiveIntegrityError`（发布侧映射 `INTEGRITY_ERROR`） | `ARCHIVE_INTEGRITY_ERROR` | 4 | 0（不覆盖） |

### 5.5 恢复的只读语义

恢复记录中显式包含：`read_only_mode=true`、`approvals_invalidated=true`、`replay_external_actions=false`、
`read_only_notice`（"只读恢复：不重放归档里的上传/发送/审批动作"）、`integrity_claim`。

### 5.6 幂等与内容变化

| 情形 | `archive_id` | 行为 |
| --- | --- | --- |
| 同一份内容重跑 | 相同 | 全部复用、`transfers=0`、仍逐文件回读校验 |
| 任一文件内容变化 | 新 id | 新归档独立完成，旧归档不被触碰（只读校验仍一致） |
| 已完成归档的远端对象被改写 | 相同 | 拒绝覆盖并报不一致（不"静默修复"历史归档） |

### 5.7 凭据纪律

- 从不读取 `~/.config/bdpan/config.json`、`~/.config/mcp-email-server/config.toml`；
- 命令行只出现绝对路径 + `--no-check-update` + 可选 `--json`；`approval_ref` 是审批 ID；
- 输出/状态记录/共享对象里断言不含 `BDUSS`/`cookie`/`token=`（测试 `test_release_output_never_contains_credentials`、
  `test_credentials_never_appear_in_argv_or_detail`）；
- 测试用的唯一"像凭据"的字符串是构造出来的假值（`BDUSS=REAL-SECRET-COOKIE-VALUE-…`），只用于断言"不外泄"。

### 5.8 措辞纪律

- 完成标记/恢复记录里的结论永远写成"逐文件回读 sha256 校验通过 N 项，其中 M 项为字节级…"，
  `METADATA_PROJECTION` 与未声明来源**只声称投影一致**，并附警告；
- `tests/archive/test_archive_engine.py::test_projection_entries_only_claim_projection` 与
  `test_restore.py::test_projection_entries_are_marked_as_projection_only` 锁住这条。

---

## 6. 边界核对

### 6.1 契约零改动

```text
$ git status --porcelain          # 只有本轮新增（?? = 未跟踪的新文件），没有修改任何既有文件
?? asistent/archive/
?? asistent/pan/
?? docs/mvp/mvp-7.md
?? scripts/release.sh
?? tests/archive/

$ git diff --name-only 149a2d9 -- asistent/contracts tests/contracts   # 149a2d9 = 本分支起点（merge-base）
（空）
```

> 注意：复核时 main 已经前进到 `ef013e6`（契约 v0.6 + MVP-5 合入 + 设备部署证据），
> 因此**按当前 main 直接 diff 会列出 main 自己的契约改动**，那不是本分支的改动；
> 正确的判据是"本分支起点（merge-base）→ 工作树"为空，加上 `git status` 里没有修改既有文件。
> 本分支的全部改动都在上面 5 个未跟踪路径内（`asistent/pan/**`、`asistent/archive/**`、`tests/archive/**`、
> `scripts/release.sh`、`docs/mvp/mvp-7.md`）。

> 另注：`scripts/release.sh` 默认输出目录是 `<repo>/dist/release`（可用 `RELEASE_OUT` 或 `--out` 覆盖）；
> 本轮演练产生的 `dist/` 已清理。若部署流程希望忽略该目录，需要在 `.gitignore` 里加一行（不在本分支写入范围内）。

### 6.2 复跑指引

```bash
# 1) 全套（两个后端）
python3 -m unittest discover -s tests -t .
ASISTENT_STORE=sqlite python3 -m unittest discover -s tests -t .

# 2) 离线端到端演练（不接触真实网盘）
python3 tests/archive/e2e_offline.py          # 期望 checks=27/27

# 3) A-5 发布入口（离线演练；真实发布需 --approval-ref）
RELEASE_OUT=/tmp/mvp7-release sh scripts/release.sh --dry-run --mirror /tmp/mvp7-mirror
sh scripts/release.sh --dry-run --mirror /tmp/mvp7-mirror    # 复跑：transfers=0

# 4) 真实网盘只读探测（用户未授权时应看到 NOT_AUTHORIZED，且零写操作）
python3 tests/archive/probe_real_bdpan_readonly.py
```

注：`scripts/release.sh` 需要在提交时带上可执行位（`git update-index --chmod=+x scripts/release.sh`）；
用 `sh scripts/release.sh …` 调用则不需要。

---

## 7. 局限与诚实边界

### 7.1 「离线已验证」vs「待用户授权后才能验证」

| 事情 | 状态 |
| --- | --- |
| 通道调用约定（绝对路径、`--no-check-update`、`--json`、`ls` 字段解析、`errno` 分类） | ✅ 离线用假 CLI 逐项验证 + 真机只读探针（version/whoami/ls）一致 |
| 上传/下载/删除/建目录的真实行为（配额、限速、重名覆盖、超大文件、限流错误码） | ❌ **未验证**：需要授权后由用户在受控范围里跑一次 |
| 逐文件回读哈希校验、部分失败补传、未知先 `stat`、幂等、篡改检测 | ✅ 离线端到端 + 单元测试（含计数断言） |
| 真实网盘目录结构（`/apps/bdpan/asistent/个人助手归档/...`） | ❌ 未验证（未授权） |
| 未授权时的显式失败（`NOT_AUTHORIZED`、零写操作） | ✅ 真机只读探针原始输出（§4.4） |
| `--dry-run` 离线打包 + manifest + 回读闭环 | ✅ 本机真实仓库 + 无 git 机器两处 |
| 恢复流程（隔离、逐文件哈希、引用闭合、索引重建、记录） | ✅ 离线端到端；真实网盘上的恢复同上待授权 |

### 7.2 已知未实测项（明确登记，不假装已验）

1. **重名覆盖策略**：真实 bdpan 对"远端已存在同名文件"是覆盖、报错还是新建副本，spike 未实测（U-3）。
   因此引擎不依赖覆盖语义：先 `stat` 判定，必要时才 `put`，并在 `outcome.notes` 里登记该不确定性。
2. **并发/限流**：未验证真实网盘对并发上传与限流的反应；当前实现串行上传、单个对象级重试。
3. **超大文件**：`put` 走 CLI，未做分片；manifest 里记录 `size_bytes`，将来可据此决定是否拒绝超大对象。
4. **`mkdir` 子命令**：老版本 bdpan 可能没有 `mkdir`；实现对其缺失做容错（视为"不支持"而非失败），
   真实 3.8.7 是否支持未实测（离线假 CLI 两种分支都测了）。
5. **设备上的 `release.sh` 端到端**：设备未创建 git 仓库（遵守只读 git 纪律），因此设备只做了 `sh -n` 语法检查；
   脚本的真实打包链在本机真实仓库上跑通（§4.3）。

### 7.3 编码教训（写进实现，避免重演）

- 假 CLI 必须 `reconfigure(encoding="utf-8")`：否则 Windows 上子进程按 cp936 输出，父进程按 utf-8 解码会把中文错误信息
  变成 `U+FFFD`，测试就变成在测"编码陷阱"而不是测通道语义（本轮真实踩到并修复）。
- 通道侧因此加固：`errno` 结构化优先于文本匹配；非零退出且**无任何可识别语义**（含乱码）时一律判"结果未知"。
- `ls --json` 顶层可能是数组：JSON 截取必须按"最先出现的定界符"决定切 `{}` 还是 `[]`（否则 `ls` 会被解析成空列表，本轮真实踩到）。
- 路径前缀只加一次：`delete`/`ensure_dir` 内部把已加前缀的路径再传给 `stat()` 会造成双重前缀（本轮真实踩到并加测试锁住）。

---

## 8. 契约洞见（CP 提案，未改动 `asistent/contracts/**`）

> 以下提案基于本分支起点 `149a2d9`（契约 v0.5）。交付时 main 上的契约已演进到 v0.6（CP-13..17），
> 因此**每一条都请先对 v0.6 复核**是否已被解决，再决定是否新增 CP。

### CP-MVP7-1 `ArchiveState` 缺 `ARCHIVE_PENDING`（SPEC 措辞与契约枚举不一致）

- SPEC §MVP-7 写"Case 为 **ARCHIVE_PENDING**，能看到已成功/未成功对象并可重试"；
  冻结契约 `asistent/contracts/enums.py::ArchiveState` 只有
  `PENDING/UPLOADING/PARTIAL/VERIFYING/PENDING_VERIFICATION/VERIFIED/FAILED`。
- 本实现的选择：**不改契约**，用 `PARTIAL` 表达"部分成功、可重试"，并把逐条未成功对象放进 `ArchiveJob.pending`；
  同时 `Case.archive_state = PARTIAL`（契约内取值）。
- 建议：要么把 SPEC §MVP-7 的措辞改为 `PARTIAL`，要么在下一个契约版本里显式增加 `ARCHIVE_PENDING`
  （若加，需同时定义它与 `PARTIAL`/`PENDING_VERIFICATION` 的区别，否则三个状态语义重叠）。

### CP-MVP7-2 `NOT_AUTHORIZED` 不是契约声明的错误码

- 契约 `errors.py` 有 `ApprovalRequired(code="APPROVAL_REQUIRED")`、`ExternalResultUnknown(code="EXTERNAL_RESULT_UNKNOWN")`、
  `PermissionDenied`（无 `code`，但有 `failure_class=PERMISSION_FAILURE`）。
- 本实现新增 `PanNotAuthorized(PermissionDenied)`，`code="NOT_AUTHORIZED"`、`failure_class` 继承 `PERMISSION_FAILURE`。
- 建议：在契约里显式登记 `NOT_AUTHORIZED`（"外部凭据未就绪，需用户授权"），并说明它与 `APPROVAL_REQUIRED`
  （"已授权但该动作需要审批"）的分工——两者都要求用户动作，但**重试前提不同**（前者需重新授权，后者需审批引用）。

### CP-MVP7-3 `PanClient` 缺少"不覆盖"语义

- `put()` 无法表达"远端已存在同名对象时**不要**覆盖"。归档的正确做法依赖"先 `stat`、必要时才 `put`"，
  这在真实通道上存在 TOCTOU 窗口（`stat` 与 `put` 之间被第三方改写）。
- 建议：下一个契约版本给 `PanPutResult` 增加 `precondition`（如 `if_none_match`/`expected_hash`）或
  独立方法 `put_exclusive()`；无此能力时，本实现以"完成后回读校验 + 完成后改写即报不一致"作为补偿控制。

### CP-MVP7-4 `content_origin` 需要"投影一致性"的正式措辞

- 契约 §12.2 要求"`METADATA_PROJECTION`/`None` 不得被当作字节级完整性证明"，但没有给出**应当怎么表述**。
- 本实现在 `integrity_claim()` 里固化了三分措辞（`byte_level_verified` / `projection_only_verified` /
  `undeclared_origin_verified` + `statement` + `warning`），并把它写进完成标记与恢复记录。
- 建议：把这段措辞提升为契约级 helper（例如 `archive.integrity_claim()`），让所有消费者（报告、手机端、审计）
  用同一句话，避免各处自由发挥后又被误读成"字节级已验证"。

### CP-MVP7-5 `RestoreJob` 的"只读恢复"语义建议契约化

- SPEC §12.5/§21.4 要求恢复"不重放外部动作、审批默认失效、只读"，但契约对象里没有承载这三个事实的字段。
- 本实现把它们写进 `RestoreJob.note`/`RestoreOutcome`（`read_only_mode`、`approvals_invalidated`、
  `replay_external_actions`）与恢复记录 JSON。
- 建议：给 `RestoreJob` 增加三个显式布尔字段，避免将来有人只读 `note` 而漏掉约束。

---

## 9. 交付判定

- [x] `asistent/pan/**`、`asistent/archive/**`、`tests/archive/**`、`scripts/release.sh`、本文档
- [x] SPEC §MVP-7 五行 Given/When/Then 逐条对应，且有可观察证据（§1.1、§4）
- [x] 假 bdpan 失败矩阵全绿（含传输次数断言）：失败/超时/崩溃/未知 errno/回读不一致/未授权/写锁/已存在同名/manifest 缺失
- [x] 设备 `/usr/bin/python3` 3.10.12 复跑：双后端 406 全绿 + 离线 e2e 27/27 + 真实只读探针（§4.1、§4.2、§4.4）
- [x] 纯标准库、Python 3.10 兼容；`asistent/contracts/**`、`tests/contracts/**` 零改动
- [x] 从未对真实网盘执行写操作；凭据不进文件/日志/argv；未运行任何 git 写命令
- [ ] 真实网盘上传/下载/恢复（**待用户完成 bdpan 授权**）
