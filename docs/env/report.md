# 设备能力实测报告（board / Orange Pi 5 Max / RK3588）

- 版本：v0.1 · 2026-10-10
- 角色：环境/部署实测（teammate `env-probe`）
- 关联：SPEC §14.1、§14.3、§18 环境门、§21.6.8、§21.7；`docs/adr/ADR-0001-runtime-and-stack.md`、`ADR-0002-dev-and-deploy-topology.md`
- 命令约定：设备侧命令一律 `$s = @'...'@; $s | ssh board "bash -s"`（stdin 送脚本，避免 Windows PowerShell 5.1 引号破坏）。
  **行尾 CR 是实测陷阱**：把 here-string 管道给远端 bash 时，**最后一行会被追加一个裸 `\r`**，
  于是 `exit 0` 变成 `exit 0\r`（`numeric argument required`），`cat /etc/resolv.conf` 变成 `cat '/etc/resolv.conf\r'`（No such file）。
  对策：远端脚本最后一行写成注释（`scripts/deploy.ps1` 的 `ConvertTo-Lf` 会自动追加 `# __END__`）。
- 所有实体改动只落在：设备 `~/code/asistent`（git 仓库）、`~/asistent-var/`、`~/asistent-spike/`（邮件通道 Spike，可整目录删除）。
  未触碰 `~/assistant-dev`、`~/.dsh`、`~/miniforge3`（仅只读探测版本与 pip）。

---

## 0. 结论速览

### 0.1 设备**能做**（已实测通过）

| 能力 | 关键证据 | 出处 |
|---|---|---|
| Python 3.10.12 标准库齐全（助手载体） | 33/34 目标模块导入成功；`sqlite3/imaplib/smtplib/email/http.server/ssl/hashlib/urllib.request/threading/zoneinfo/json/csv/logging/unittest` 全部 OK | §2 |
| SQLite 3.37.2：WAL 可用 | `PRAGMA journal_mode=WAL` → `wal`，`-wal`/`-shm` 落盘，重开后仍为 `wal`，`integrity_check=ok` | §3.1 |
| SQLite FTS5 可用（4 种分词器） | `unicode61/porter/trigram/ascii` 建表成功 | §3.2 |
| WAL 并发读+写 | 2 线程 200 写 + 200 读，0 错误，`integrity_check=ok` | §3.4 |
| 时区 `Asia/Shanghai` | `/etc/localtime → /usr/share/zoneinfo/Asia/Shanghai`；`zoneinfo` 导入 OK | §4 |
| 16GB 内存 / 8 核 / 107G 可用磁盘 | `free -m`、`nproc`、`df -h /` | §5 |
| 外网：pypi 清华镜像、pypi.org、pan.baidu.com | 均 HTTP 200 | §6.1 |
| IMAP 993 / SMTP 465（TLS 1.3） | `imap.qq.com:993` 与 `smtp.qq.com:465` TLS 握手成功 | §6.2 |
| `systemctl --user` 可用 | `systemctl --user is-system-running` → `running`；`systemd-run --user` 起停瞬时 unit 成功 | §7.1 |
| `crontab @reboot` 语法可用 | 安装→`crontab -l` 回显→删除，全程 exit 0 | §7.4 |
| 进程脱离 SSH 会话存活 | `setsid nohup` 启动后断开 SSH，重连仍在（PPID 变 1） | §7.5 |
| 内网 Web 端口 | `192.0.2.10:18080` 绑定并自测 HTTP 200；18080–18100 全部空闲 | §8 |
| 设备侧能跑 `mcp-email-server`（邮件通道） | venv 安装成功（80s，全 aarch64 wheel，无编译），stdio 握手成功，19 个邮件工具可见 | §10 |
| 设备侧能跑冻结契约测试 | 部署 `344cae1` 后：`python3 -m unittest discover` → **`Ran 75 tests in 0.076s ... OK`（RC=0）** | §11 |

### 0.2 设备**不能做**（实测否定）

| 不能做 | 证据 |
|---|---|
| 系统 python 装第三方包 | `python3 -m pip` → `No module named pip`；`ensurepip` 不存在，`python3 -m venv`（默认）失败 |
| 直接跑需要 Python ≥3.11 的包 | `mcp-email-server` 要求 `<4.0,>=3.11`，系统 python 是 3.10.12 → 必须借 `~/miniforge3` 的 3.14 |
| 无口令 sudo | `sudo -n true` → `sudo: a password is required`（用户确在 sudoers，但要口令） |
| 无桌面会话时保活 `systemctl --user` | `Linger=no` → 需 `sudo loginctl enable-linger asistent`（要 root） |
| 开发机↔设备直连 | 开发机→`192.0.2.10:22` 不通、ping 不通；设备→`192.0.2.2` ping 100% 丢包、TCP 全封。**必须走 `ProxyJump jump`** |
| 设备访问开发机上的服务（复用 Windows 那台 mcp-email-server） | 设备 ping `198.51.100.30` 通（4ms），但 22/3000/3333/5000/8000/8080/8765/9000 **全部 TCP 超时**（Windows 防火墙） |
| FTS5 直接做中文检索 | `unicode61` 把连续中文切成**一个整词**，`MATCH '部署'` 命中 0 行 | §3.3 |

### 0.3 需要额外变更/人工介入的项

| 项 | 需要什么 | 是否要 sudo |
|---|---|---|
| 24h 无人值守保活（首选 systemd user + linger） | 用户执行一次 `sudo loginctl enable-linger asistent` | 是（口令） |
| 系统级 systemd unit / 开机自启服务 | 用户在场执行 `sudo` | 是 |
| 设备无桌面会话也能常驻（退化方案） | `crontab @reboot` + `setsid nohup`（已实测语法与脱离会话均可行；重启后行为待一次真实验证） | 否 |
| 真实邮件收发验收 | 真实 IMAP/SMTP 凭据（本文档不写入任何凭据） | 否 |
| 开机时钟可靠性 | 建议启用 `systemd-time-wait-sync.service`（当前 disabled）或在应用内等待 NTP 同步 | 是 |
| 手机控制台连通性 | 需手机接入 `192.0.2.0/24`（手机不在场，未验证） | 否 |

> 更新（2026-10-10 13:14）：**"设备上跑不了契约测试"这一项已解除** —— 部署 `344cae1` 后设备侧 75 条契约测试全绿（§11）。上表为剩余项。

---

## 1. 被测环境与连接路径

```
$ ssh -o BatchMode=yes board "bash -s"   # 免密可用，经 ProxyJump jump (198.51.100.20)
remote_user=asistent
remote_host=board-host
remote_uname=aarch64 6.1.43-rockchip-rk3588
remote_python=Python 3.10.12

$ uname -a
Linux board-host 6.1.43-rockchip-rk3588 #1.0.0 SMP Mon Jul  8 11:54:40 CST 2024 aarch64 GNU/Linux

$ cat /etc/os-release | head -4
PRETTY_NAME="Orange Pi 1.0.0 Jammy"
NAME="Ubuntu"
VERSION_ID="22.04"
VERSION="22.04.4 LTS (Jammy Jellyfish)"
```

工具链：

```
$ for c in git sqlite3 curl wget node npm tmux screen jq rsync crontab systemctl loginctl ssh ss ip gcc make uv uvx pipx conda mamba poetry; do printf "%-10s %s\n" "$c" "$(command -v $c || echo MISSING)"; done
git        /usr/bin/git          sqlite3    MISSING      curl    /usr/bin/curl
wget       /usr/bin/wget         node       MISSING      npm     MISSING
tmux       /usr/bin/tmux         screen     /usr/bin/screen
jq         /usr/bin/jq           rsync      /usr/bin/rsync
crontab    /usr/bin/crontab      systemctl  /usr/bin/systemctl
loginctl   /usr/bin/loginctl     ssh        /usr/bin/ssh
ss         /usr/bin/ss           ip         /usr/sbin/ip
gcc        /usr/bin/gcc          make       /usr/bin/make
uv/uvx/pipx/conda/mamba/poetry   MISSING（都不在 PATH 上）

$ git --version      → git version 2.34.1
$ sqlite3 --version  → sqlite3 CLI: MISSING（但 Python 内置 sqlite3 模块可用，见 §3）
```

**结论**：`sqlite3` 命令行缺失、无 node/npm/uv/conda（PATH 上），但 `git 2.34.1`、`gcc/make`、`curl/jq/rsync/tmux/screen` 齐备；
Python 内置 `sqlite3` 模块可直接用，不依赖 CLI。开发机 `git 2.55.0.windows.5` 与设备 `2.34.1` 通过标准 `receive-pack` 协议互操作，实测无告警。

---

## 2. Python 3.10.12 标准库可用性

```
$ which -a python3 ; python3 -VV
/usr/bin/python3
/bin/python3
Python 3.10.12 (main, Nov 20 2023, 15:14:05) [GCC 11.4.0]

$ python3 - <<'PY'   # 逐个 importlib.import_module
mods = ["sqlite3","imaplib","smtplib","email","email.header","email.utils","http.server","ssl",
        "hashlib","urllib.request","threading","zoneinfo","json","csv","logging","unittest",
        "asyncio","dataclasses","typing","pathlib","datetime","tempfile","socket","sqlite3.dbapi2",
        "http.client","secrets","hmac","base64","gzip","zipfile","subprocess","signal","atexit","tomllib"]
PY
OK(33): sqlite3, imaplib, smtplib, email, email.header, email.utils, http.server, ssl, hashlib,
        urllib.request, threading, zoneinfo, json, csv, logging, unittest, asyncio, dataclasses,
        typing, pathlib, datetime, tempfile, socket, sqlite3.dbapi2, http.client, secrets, hmac,
        base64, gzip, zipfile, subprocess, signal, atexit
FAIL(1): tomllib: ModuleNotFoundError: No module named 'tomllib'

$ python3 -c "import sqlite3,ssl;print(sqlite3.version, sqlite3.sqlite_version, sqlite3.threadsafety)"
2.6.0 3.37.2 1
$ python3 -c "import ssl;print(ssl.OPENSSL_VERSION)"
OpenSSL 3.0.2 15 Mar 2022
$ python3 -c "import hashlib;print(hashlib.blake2b(b'x').hexdigest()[:16])"
c0d0f7ea2461b6c7
```

**结论**

1. 首阶段所需标准库**全部可用**（含 `zoneinfo`、`http.server`、`ssl`、`imaplib/smtplib`）→ ADR-0001「纯标准库」约束在设备侧成立。
2. **`tomllib` 是 3.11+**（设备 3.10.12 没有）。若要用 TOML 配置，纯标准库路线只能：自写最小 TOML 解析、改用 `json`/`ini`，或引入第三方 `tomli`（与"纯标准库"冲突）。**建议契约层选 JSON**，把 TOML 需求退掉。
3. `sqlite3.threadsafety == 1`（module-level，非 3=serialized）→ 连接**不可跨线程共享**；每线程独立 `Connection`，跨线程传连接需 `check_same_thread=False` 且自行加锁。WAL 模式下的多连接并发见 §3.4。
4. 无 `tomllib` 也意味着：配置文件读取代码**不能**写成 `import tomllib`，否则在设备上 ImportError（开发机 3.12 有，会掩盖这个问题）→ 建议在契约测试里加一条"3.11+ 专有模块黑名单"静态检查。

---

## 3. SQLite：WAL、FTS5、中文检索

### 3.1 WAL 实测

```
$ python3 - <<'PY'
import sqlite3, os
con = sqlite3.connect(db)
print("journal_mode before:", con.execute("PRAGMA journal_mode").fetchone()[0])
print("journal_mode set ->", con.execute("PRAGMA journal_mode=WAL").fetchone()[0])
print("journal_mode after:", con.execute("PRAGMA journal_mode").fetchone()[0])
print("synchronous:", con.execute("PRAGMA synchronous").fetchone()[0])
con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, body TEXT)")
con.execute("INSERT INTO t(body) VALUES ('hello')"); con.commit()
print("wal file exists:", os.path.exists(db+"-wal"), "| shm:", os.path.exists(db+"-shm"))
PY
journal_mode before: delete
journal_mode set -> wal
journal_mode after: wal
synchronous: 2
wal file exists: True | shm: True
reopen WAL journal_mode: wal
integrity_check: ok
row count: 4
```

**结论**：WAL 可用且**持久**（重开连接仍为 `wal`），`-wal`/`-shm` 正常生成，`integrity_check=ok`。
注意默认 `synchronous=2`（FULL）。WAL + 掉电（§21.7 掉电恢复演练）下的取舍应在运行配置里显式写死，不要依赖默认值。

### 3.2 FTS5 与分词器

```
$ python3 - <<'PY'
con = sqlite3.connect(":memory:")
con.execute("CREATE VIRTUAL TABLE x USING fts5(a)")            → fts5: AVAILABLE
tokenize='unicode61' → OK   tokenize='porter' → OK   tokenize='trigram' → OK   tokenize='ascii' → OK
tokenize='unicode61 remove_diacritics 2' → OK
PY
```

**结论**：FTS5 编译进去且 4 种分词器可用（不需要 `sqlite3` CLI，也不需要 `-DSQLITE_ENABLE_FTS5` 重编）。

### 3.3 中文检索（**本报告最重要的发现**）

```
$ python3 - <<'PY'   # 索引 4 条中英混合文档，tokenize='unicode61'
rows = [("项目会议纪要","讨论 RK3588 部署方案，决定采用 systemd 常驻。"),
        ("Railway ticket","Train G123 business trip to Shanghai next Monday."),
        ("邮件归档","百度网盘归档任务失败，需要重试。"),
        ("Invoice 2026","发票金额 1288 元，来自供应商。")]
PY
[MATCH CJK-2char '部署']        rows=0 -> []
[MATCH CJK-2char '归档']        rows=0 -> []
[MATCH CJK-single '会']         rows=0 -> []
[MATCH CJK-3char '百度网盘']    rows=0 -> []
[MATCH EN term]                 rows=1 -> [('Railway ticket',)]
[prefix 'Shang*']               rows=1 -> [('Railway ticket',)]
[snippet()]                     rows=0 -> []
[highlight()]                   rows=0 -> []
[bm25()]                        rows=0 -> []
[LIKE fallback CJK]             rows=1 -> [('项目会议纪要',)]
```

分词结果（`fts5vocab` 直接看索引里的 token）：

```
$ python3 - <<'PY'
con.execute("CREATE VIRTUAL TABLE docs USING fts5(body, tokenize='unicode61')")
con.execute("CREATE VIRTUAL TABLE v USING fts5vocab(docs,'row')")
con.execute("INSERT INTO docs(body) VALUES ('讨论 RK3588 部署方案，决定采用 systemd 常驻。Invoice 2026')")
print("tokens:", [r[0] for r in con.execute("SELECT term FROM v ORDER BY term")])
PY
tokens: ['2026', 'invoice', 'rk3588', 'systemd', '决定采用', '常驻', '讨论', '部署方案']
```

即：`unicode61` 把**连续中文整段当一个 token**（`部署方案`、`决定采用`），所以任何**子串**检索（"部署"）都命中 0；
英文/数字正常（`train`、`Shang*` 均命中）。

两条已验证的可行绕法：

```
# 方案 A：入库前把中文按"单字+空格"切分（unigram），查询词同样切分 → 相邻单字构成 phrase
$ python3 - <<'PY'
def seg(s):  # CJK run -> "讨 论"，ASCII word 原样
...
con.execute("INSERT INTO docs(body) VALUES (?)", (seg("讨论 RK3588 部署方案，决定采用 systemd 常驻。"),))
PY
  MATCH '部 署'        -> 1
  MATCH '方 案'        -> 1
  MATCH '部 署 方 案'  -> 1
  MATCH 'systemd'      -> 1
  MATCH 'RK3588'       -> 1
  MATCH '常 驻'        -> 1
  phrase-no-space '部署' -> 0   （应用层必须自己切分查询串）

# 方案 B：trigram 分词器（子串检索，无需分字）
tokenize='trigram'
  MATCH '部'          -> 0        # <3 字符不支持
  MATCH '部署'        -> 0        # <3 字符不支持
  MATCH '部署方'      -> 1
  MATCH '部署方案'    -> 1
  MATCH 'RK3588'      -> 1
  MATCH 'rk3588'      -> 1        # 大小写不敏感
  MATCH '常驻'        -> 0        # 2 字仍不行
```

**结论与建议（需基础 Agent 裁决后写入契约）**

- **什么都不做**的方案不可接受：中文检索直接返回空，会让 MVP-1「已挂载资料 60 秒内可检索到新版本」（§21.7）在中文语料上静默失败——**这是最危险的一类缺陷：不报错，只是查不到**。
- 方案 A（unigram 切分 + 应用层同样切分查询串）纯标准库可实现、支持任意长度中文查询、可继续使用 `bm25/snippet/highlight`；代价是索引膨胀（每字一个 token）且查询串需统一走切分函数。
- 方案 B（trigram）实现最简单、天然支持子串与中英混合，但**中文 1–2 字查询不支持**（"部署""常驻"这类 2 字查询在中文里极常见）。
- 推荐：**A + B 双索引**（短查询走 unigram，长/模糊走 trigram），或 A 为主 + `LIKE '%xx%'` 兜底（实测 `LIKE` 对中文可用）。无论选哪个，**必须有一条契约测试直接断言中文 2 字查询**，否则该缺陷会一直潜伏。

### 3.4 WAL 并发（多连接）

```
$ python3 - <<'PY'   # 1 写线程 200 次 commit + 1 读线程 200 次 count，各自独立连接，busy_timeout=5000
writer commits=200 reader queries=200 elapsed=0.01s rows=200
integrity: ok
```

**结论**：WAL 下"单写多读"并发实测 0 错误、0 秒级阻塞（单连接 `timeout=5` / `PRAGMA busy_timeout` 仍需设置，用于真实负载下的写写竞争）。多 writer 场景须在应用层串行化（WAL 只允许一个 writer）。

---

## 4. 时间与时区

```
$ date -Is ; echo "TZ=[${TZ:-unset}]" ; cat /etc/timezone ; readlink -f /etc/localtime
2026-10-10T12:59:39+08:00
TZ=[unset]
Asia/Shanghai
/usr/share/zoneinfo/Asia/Shanghai

$ timedatectl status
               Local time: Sat 2026-10-10 12:59:39 CST
           Universal time: Sat 2026-10-10 04:59:39 UTC
                 RTC time: Sat 2026-10-10 04:59:39
                Time zone: Asia/Shanghai (CST, +0800)
System clock synchronized: yes
              NTP service: active
            RTC in local TZ: no

$ python3 -c "import zoneinfo;print(zoneinfo.ZoneInfo('Asia/Shanghai'))"
ZoneInfo(key='Asia/Shanghai')      # 导入/构造均成功
```

**结论**：`Asia/Shanghai` 可用，NTP 已同步，RTC 走 UTC（正确姿势）。
但 `TZ` 环境变量**未设置**，且系统时间/时区依赖 NetworkManager + NTP：代码里必须显式 `ZoneInfo("Asia/Shanghai")`，禁止依赖隐式本地时间。

### 4.1 ⚠️ 开机初期时钟跳变（实测到的异常）

```
$ journalctl --list-boots | head -2
 0 e528bd1858c14dd7a84340a707b605d8 Mon 2024-07-08 11:59:05 CST—Sat 2026-10-10 13:12:17 CST

$ uptime -s            → 2026-10-10 10:03:38      (本次真实开机时间)
$ who -b               → system boot  2024-07-08 11:59
$ stat -c '%y %n' /run/user/1000 /run/systemd/seats/seat0
2024-07-08 11:59:11  /run/user/1000
2024-07-08 11:59:17  /run/systemd/seats/seat0
$ cat /proc/uptime     → 11320.03 89818.23
$ systemctl status systemd-time-wait-sync
○ systemd-time-wait-sync.service ... Active: inactive (dead)     ← vendor preset disabled
```

**结论**：这次开机时系统时钟**停在 2024-07-08 11:59**（落后约 2 年 3 个月），开机约 2.5 分钟后被 NTP 纠正到 2026-10-10。
后果与对策：

| 后果 | 说明 |
|---|---|
| 开机前约 2 分钟内产生的时间戳全是 2024 年 | 日志/审计/事件时间会错到 2 年前，破坏 §11 的可观察性与 §21.7「恢复后 5 分钟内显示待处理状态」的判定 |
| IMAP `SINCE`/`BEFORE` 检索窗口、归档定期任务 | 时钟跳变前后查询窗口会错乱，可能漏抓或重抓邮件 |
| TLS 证书校验 | 时钟落后 2 年通常仍在证书有效期内，但方向反过来（时钟超前）会直接握手失败 |
| `systemd-time-wait-sync.service` 当前 disabled | 服务可在时钟有效前启动 → 应用**不能假设启动时时间可信** |

**建议（需 Lead/基础 Agent 定成契约）**：
1. 应用启动时检查 `timedatectl show -p NTPSynchronized --value`（或比对 `CLOCK_REALTIME` 与单调钟），未同步则**拒绝写入带业务语义的时间戳**，或标记为待校准。
2. 计时逻辑（超时、轮询间隔、24h 运行）一律用**单调钟**（`time.monotonic()`），不用墙上钟。
3. 请求用户启用 `sudo systemctl enable --now systemd-time-wait-sync`（需 root），或确认 RTC 电池状态（需一次真实断电重启验证——本次未做）。

---

## 5. CPU / 内存 / 磁盘 / 温度基线（SPEC §21.7 要求记录）

```
$ date -Is ; uptime ; nproc
2026-10-10T13:02:24+08:00
 13:02:24 up  2:58,  3 users,  load average: 0.00, 0.05, 0.07
8

$ lscpu | head -25
Architecture: aarch64        CPU(s): 8        On-line CPU(s) list: 0-7
Model name: Cortex-A55        Core(s) per socket: 4   CPU max MHz: 1800.0000   CPU min MHz: 408.0000
Model name: Cortex-A76        Core(s) per socket: 4   CPU max MHz: 2256.0000   CPU min MHz: 408.0000

$ for c in /sys/devices/system/cpu/cpufreq/policy*/; do echo "$(basename $c) governor=$(cat $c/scaling_governor) cur=$(cat $c/scaling_cur_freq) max=$(cat $c/scaling_max_freq)"; done
policy0 governor=ondemand cur=408000 max=1800000
policy4 governor=ondemand cur=600000 max=2256000
policy6 governor=ondemand cur=600000 max=2256000

$ free -m
               total        used        free      shared  buff/cache   available
Mem:           15964         719       11952          81        3292       15005
Swap:           7982           0        7982
$ cat /proc/meminfo | head -5
MemTotal: 16347844 kB   MemFree: 12222340 kB   MemAvailable: 15354936 kB   Buffers: 123984 kB   Cached: 2997560 kB
$ swapon --show
NAME       TYPE       SIZE USED PRIO
/dev/zram0 partition  7.8G   0B    5        ← swap 是 zram（内存压缩盘），不是磁盘分区

$ df -h /
Filesystem      Size  Used Avail Use% Mounted on
/dev/nvme0n1p2  116G  7.1G  107G   7% /         ← Spike 安装前
/dev/nvme0n1p2  116G  7.2G  107G   7% /         ← Spike 安装后（+83MB）

$ vmstat 1 3
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 1  0      0 12232176 123968 3253944    0    0     9    42   46   56  0  0 99  0  0
 0  0      0 12232428 123968 3253980    0    0     0     0  334  247  0  0 100  0  0
 0  0      0 12232428 123968 3253980    0    0     0     0  150  184  0  0 100  0  0

$ for z in /sys/class/thermal/thermal_zone*/; do echo "$(basename $z) type=$(cat $z/type) temp_mC=$(cat $z/temp)"; done
thermal_zone0 soc-thermal      45307     thermal_zone4 center-thermal  45307
thermal_zone1 bigcore0-thermal 45307     thermal_zone5 gpu-thermal     45307
thermal_zone2 bigcore1-thermal 45307     thermal_zone6 npu-thermal     45307
thermal_zone3 littlecore-thermal 45307
（7 个 zone 读数一致，疑似同一传感器镜像；无 thermal_throttle 属性文件）

$ ps -eo pcpu,pmem,comm --sort=-pcpu | head -4
 1.7  0.0 sshd
 0.6  0.0 sshd
 0.3  0.0 bash
```

**结论（§21.7 基线记录）**

| 指标 | 空闲基线 | 备注 |
|---|---|---|
| CPU | 8 核（4×A55 ≤1.8GHz + 4×A76 ≤2.256GHz），`ondemand`，load 0.00/0.05/0.07 | 空闲 CPU 99–100% idle；A76 可用于重活，A55 跑常驻轮询 |
| 内存 | 总 15964 MiB（15.6 GiB），可用 15005 MiB，已用 719 MiB | 16GB 足够；**swap 是 zram 7.8G**（无磁盘 swap，掉电不丢、但有 CPU 代价） |
| 磁盘 | 根分区 116G，已用 7.1–7.2G，**可用 107G** | NVMe；`~/asistent-var` 与日志空间充足 |
| 温度 | 全部 zone 45.3 °C（空闲） | 24h 常驻需在真实负载（模型调用/索引重建）下复测；本次只取空闲点 |
| 观察 | 1 个 `dnsmasq.service` failed（systemd 状态 `degraded`，既有问题，非本任务引入） | 见 §12 风险 B9 |

**未做**：24h 连续运行的 CPU/内存曲线、真实负载下的温度与降频、模型调用成本——这些属于 §21.7 的 24h 验收，需在应用可运行后单独跑。

---

## 6. 网络

### 6.1 出网能力

```
$ curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" --max-time 25 https://pypi.tuna.tsinghua.edu.cn/simple/
200 9.139926s
$ curl ... https://pypi.org/simple/
200 7.849287s
$ curl ... https://pan.baidu.com/
200 0.270235s

$ getent hosts pypi.tuna.tsinghua.edu.cn pan.baidu.com imap.qq.com
2402:f000:1:400::2   pypi.tuna.tsinghua.edu.cn     ← 只返回 AAAA（IPv6）
240e:940:603:750:0:ff:b017:c94c  pan.baidu.com
240e:97c:2f:201d::c9  imap.qq.com

$ ip -6 addr show scope global | grep inet6
（空 —— 设备没有全局 IPv6 地址）
```

**结论**：三个目标站点均可达（HTTP 200）。清华镜像比 pypi.org 快一点（9.1s vs 7.8s，首次 TLS+索引页，实际包下载实测 370 kB/s 左右）。
**坑**：`getent hosts` 只返回 **AAAA**，而设备**没有全局 IPv6 地址**（`ip -6 addr` 无 global）→ 用 `getent`/glibc 默认行为做"可达性判断"会误判；HTTP 客户端因为会回落 IPv4 才成功。
应用内做网络探测时，要么显式用 IPv4，要么按"实际连接成功"判定，不要用 DNS 结果判定。

### 6.2 邮件端口（TLS）

```
$ python3 - <<'PY'   # socket.create_connection + ssl.wrap_socket / STARTTLS
imap.qq.com:993  TCP+STARTTLS OK ver=TLSv1.3 peer=CN=*.mail.qq.com (0.15s)
imap.qq.com:143  TCP OK (0.04s)
smtp.qq.com:465  TCP+STARTTLS OK ver=TLSv1.3 peer=CN=*.mail.qq.com (0.15s)
PY
```

**结论**：IMAP 993（TLS1.3）、SMTP 465（TLS1.3）**从设备直连可用**，延迟 0.15s。证书链校验通过（系统 CA 正常）。

### 6.3 设备 ↔ 开发机（**只有 push 方向可用**）

```
# 开发机 → 设备（直连，绕过跳板）
PS> Test-NetConnection -ComputerName 192.0.2.10 -Port 22 -InformationLevel Quiet  → False
PS> Test-Connection -ComputerName 192.0.2.10 -Count 2 -Quiet                        → False

# 设备 → 开发机
$ ping -c 2 -W 2 192.0.2.2        → 2 packets transmitted, 0 received, 100% packet loss
$ python3 (TCP scan)
  192.0.2.2:22    CLOSED (TimeoutError)     192.0.2.2:8080  CLOSED (OSError)
  192.0.2.2:135   CLOSED (TimeoutError)     192.0.2.2:3389  CLOSED (TimeoutError)
  192.0.2.2:445   CLOSED (OSError)          192.0.2.2:8000  CLOSED (OSError)

# 设备 → 开发机 WLAN 地址
$ ping -c 2 198.51.100.30        → 2 received, 0% loss, rtt avg 4.107 ms   ← ICMP 通
  198.51.100.30:22/3000/3333/5000/8000/8080/8765/9000  → 全部 CLOSED (TimeoutError)

$ ip -4 route
default via 192.0.2.1 dev enP3p49s0 proto dhcp metric 100
192.0.2.0/24 dev enP3p49s0 proto kernel scope link src 192.0.2.10 metric 100
$ cat /etc/resolv.conf
# Generated by NetworkManager
nameserver 192.0.2.1
```

**结论**

| 方向 | 结果 | 影响 |
|---|---|---|
| 开发机 → 设备（直连） | 不通（22 端口/ping 均失败） | 部署必须保留 `ProxyJump jump`；`deploy.ps1` 不提供"直连"快捷路径是对的 |
| 设备 → 开发机（TCP） | 全部不通（含 ICMP 可达但 TCP 全封的 WLAN 地址） | 设备**不能**主动拉取开发机资源，也**不能**访问开发机上的 mcp-email-server HTTP 端点（除非在 Windows 放行入站规则） |
| 设备 → 开发机（ICMP） | WLAN 地址 4ms 可达 | 只能当"存活探测"，不能当数据通道 |

因此协作模型只能是 **push 型（开发机 → 设备）+ 需要时 ssh 进入设备调试**。

---

## 7. 24h 常驻能力

### 7.1 `systemctl --user`

```
$ systemctl --user is-system-running      → running
$ echo "$XDG_RUNTIME_DIR / $DBUS_SESSION_BUS_ADDRESS"
/run/user/1000 / unix:path=/run/user/1000/bus
$ systemctl --user list-units --type=service | head -4
at-spi-dbus-bus.service   loaded active running
dbus.service              loaded active running
gconf/dconf ...           loaded active running

$ systemd-run --user --unit=asistent-probe --collect /bin/sleep 45
Running as unit: asistent-probe.service
$ systemctl --user status asistent-probe --no-pager | head -5
     Loaded: loaded (/run/user/1000/systemd/transient/asistent-probe.service; transient)
     Active: active (running) since Sat 2026-10-10 13:02:43 CST; 1s ago
     Memory: 280.0K
$ systemctl --user stop asistent-probe   → stop_exit=0
```

**结论**：user manager 可用（`systemd-run --user` 起停瞬时 unit 实测成功），**当前能跑** —— 但保活依赖用户会话存在（见 7.2）。

### 7.2 Linger（**关键阻塞**）

```
$ loginctl show-user asistent -p Linger
Linger=no
$ loginctl show-user asistent | head -12
State=active   Sessions=131 3 4 c1   Display=c1
$ loginctl list-sessions
SESSION UID USER     SEAT  TTY
    137 1000 asistent
    189 1000 asistent
      3 1000 asistent seat0 tty1
      4 1000 asistent       ttyFIQ0
     c1 1000 asistent seat0
$ systemctl get-default          → graphical.target
$ systemctl status display-manager | head -3
● lightdm.service ... Active: active (running)
```

**结论**：`Linger=no` + 默认 `graphical.target` + lightdm 常驻会话 ⇒ **当前 user 服务能跑，是因为有 5 个用户会话存在**。
一旦桌面会话全部结束（登出/切到无 GUI），`user@1000.service` 会被停止，`systemctl --user` 服务随之被杀。
让 user 服务在无会话时也常驻需要：`sudo loginctl enable-linger asistent`（**要 root 口令**）。

### 7.3 sudo（**关键阻塞**）

```
$ sudo -n true
sudo: a password is required
sudo_n_true_exit=1
$ timeout 10 sudo -n -l | head -5
Matching Defaults entries for asistent on board-host: env_reset, mail_badpass, secure_path=..., use_pty
User asistent may run the following commands on board-host:
    (ALL : ALL) ALL
```

**结论**：用户**在 sudoers 里**（`(ALL:ALL) ALL`），但 `sudo` **需要口令**，没有 NOPASSWD。
⇒ 本次实测**没有也不能执行任何提权操作**；一切"需要 root 的项"都必须由用户在场执行（见 §0.3、§12）。

### 7.4 `crontab @reboot`（退化方案的语法验证）

```
$ which crontab ; systemctl is-enabled cron      → /usr/bin/crontab ; enabled
$ crontab -l                                     → no crontab for asistent
$ printf '# probe: syntax check only\n@reboot /bin/true\n' | crontab -    → install_exit=0
$ crontab -l
# probe: syntax check only
@reboot /bin/true
$ crontab -r                                     → remove_exit=0
$ crontab -l                                     → no crontab for asistent   （已还原，设备未留残留）
```

**结论**：`cron` 服务 enabled，用户级 `@reboot` 语法被接受（0 错误），
且探测条目**已完全移除**（`no crontab for asistent`）。`@reboot` 由 root 的 cron 守护进程在开机后以用户身份执行，**不需要 sudo、不需要 linger**。

### 7.5 nohup/setsid 脱离会话存活（真实验证）

```
$ setsid nohup sh -c 'echo $$ > /tmp/probe_detach.pid; sleep 180' >/tmp/probe_detach.log 2>&1 < /dev/null &
launched pid file: 18276
    PID    PPID     SID CMD
  18276   18268   18276 sh -c echo $$ > /tmp/probe_detach.pid; sleep 180

# —— 断开 SSH，另起一次连接 ——
$ PID=$(cat /tmp/probe_detach.pid); ps -o pid,ppid,sid,tty,cmd -p "$PID"
    PID    PPID     SID TT       CMD
  18276       1   18276 ?        sh -c echo $$ > /tmp/probe_detach.pid; sleep 180
$ kill "$PID" → killed probe process      （探测进程已清理，/tmp 无残留）
```

**结论**：`setsid` + `nohup` + 重定向 stdin/stdout/stderr 的进程**在 SSH 会话结束后仍存活**（PPID 被 init 收养、SID 独立、无控制终端）。
这正是退化方案 `crontab @reboot` + `setsid nohup` 所需的机制。

**尚未验证**：**真实重启后**（无桌面会话介入）`@reboot` 是否成功拉起并存活 —— 需要一次受控重启，本次未做（会影响设备上用户既有内容，且需用户同意）。

### 7.6 systemd 系统状态

```
$ ps -p 1 -o pid,comm=      → 1 systemd
$ systemctl is-system-running → degraded
$ systemctl --failed
  UNIT            LOAD   ACTIVE SUB    DESCRIPTION
● dnsmasq.service loaded failed failed dnsmasq - A lightweight DHCP and caching DNS server
```

**结论**：系统整体 `degraded`，唯一失败单元是 `dnsmasq.service`（既有问题，DNS 实际由 NetworkManager/`192.0.2.1` 提供，域名解析实测正常）。
记录在案：不是本任务引入，但"systemd 状态 degraded"会干扰未来用 `systemctl is-system-running` 做健康判断，健康检查应改为"检查自己关心的单元"。

---

## 8. 内网地址与端口（手机控制台选址依据）

```
$ ip -4 addr show
1: lo         inet 127.0.0.1/8 scope host lo
2: enP3p49s0  inet 192.0.2.10/24 brd 192.0.2.255 scope global dynamic noprefixroute
$ ip -6 addr show scope global     → （空，无全局 IPv6）

$ ss -ltnp
State  Local Address:Port
LISTEN 127.0.0.53:53            (systemd-resolved stub)
LISTEN 127.0.0.1:5037           (adb server, 仅本机)
LISTEN 0.0.0.0:22               (sshd, IPv4)
LISTEN 127.0.0.1:631            (cups)
LISTEN 0.0.0.0:5555             (adb tcp, 对外)
LISTEN *:3389                   (对外的远程桌面)
LISTEN *:22                     (sshd, IPv6)
LISTEN [::1]:3350 / [::1]:631

$ python3 - <<'PY'   # 逐个 bind 探测 18080-18100
PY
free ports: [18080..18100]  21 of 21 free

$ cd /tmp && python3 -m http.server 18080 --bind 0.0.0.0 & sleep 1.5
$ curl -s -o /dev/null -w "%{http_code}\n" http://192.0.2.10:18080/   → 200
$ ss -ltn | grep 18080
LISTEN 0      0            0.0.0.0:18080      0.0.0.0:*
```

**结论（手机控制台选址）**

| 项 | 值 |
|---|---|
| 设备唯一内网地址 | **`192.0.2.10`**（`enP3p49s0`，DHCP，网关 `192.0.2.1`） |
| 无全局 IPv6 | 只能按 IPv4 暴露 |
| 已占用端口 | 22（sshd）、3389（远程桌面，对外）、5555（adb，对外）、53/631/5037/3350（仅本机） |
| **建议端口** | **`18080`**（18080–18100 全部空闲，已实测 `0.0.0.0:18080` 绑定成功且本机 HTTP 200） |
| 手机访问前提 | 手机需与设备同网段可达（**手机不在场，未验证**）；设备侧无需 root 即可绑定 ≥1024 端口 |
| 暴露面提醒 | `3389`、`5555` 已对外监听（既有配置）。新增控制台建议只在内网暴露，并确保令牌认证（§9.1）；不要为省事把服务映射到公网 |

---

## 9. 打包/依赖通道

### 9.1 系统 Python

```
$ python3 -m pip --version
/usr/bin/python3: No module named pip
$ python3 -c "import ensurepip"
ModuleNotFoundError: No module named 'ensurepip'
$ python3 -m venv /tmp/probe_venv            → 失败（默认要 ensurepip）
$ python3 -m venv --without-pip /tmp/probe_venv  → OK
   生成 bin: activate activate.csh activate.fish Activate.ps1 python python3 python3.10
$ python3 -c "import sys;print([p for p in sys.path if 'packages' in p])"
['/usr/local/lib/python3.10/dist-packages', '/usr/lib/python3/dist-packages']
$ ls /usr/lib/python3/dist-packages | head    # apt/apt_pkg/cairo/chardet/click... 系统包
```

**结论**：系统 python **没有 pip、没有 ensurepip**，`venv` 只能 `--without-pip`（无法装包）。
系统自带 `dist-packages` 里有 apt/GTK 等发行版包，但**不是**通用依赖来源。
→ 生产载体必须按 ADR-0001 保持**纯标准库**；要装第三方包只能：`sudo apt install python3-pip`（需 root 口令），或用 `~/miniforge3` 的 python（见 9.2）。

### 9.2 `~/miniforge3`（只读探测）

```
$ for c in uv uvx pipx conda mamba micromamba poetry; do ...; done
uv MISSING  uvx MISSING  pipx MISSING  conda MISSING  mamba MISSING  micromamba MISSING  poetry MISSING
$ ls ~/.local/bin                      → （无 uv/pipx）
$ ~/miniforge3/bin/python -V           → Python 3.14.7
$ ~/miniforge3/bin/python -m pip --version
pip 26.2.1 from /home/asistent/miniforge3/lib/python3.14/site-packages/pip (python 3.14)
$ ~/miniforge3/bin/python -c "import ensurepip, venv; print('miniforge ensurepip+venv OK')"
miniforge ensurepip+venv OK
$ cat ~/.condarc                        → channels: defaults / tuna 镜像 / channel_priority: flexible / auto_activate_base: false
$ ls ~/miniforge3/envs                  → （无 envs 输出）
```

**结论**：设备上**唯一可用的 Python 包管理能力来自 `~/miniforge3`**（Python 3.14.7 + pip 26.2.1 + ensurepip），
但它**不在 PATH**（`auto_activate_base: false`），也没有 `conda` 命令可调用（`~/miniforge3/bin/conda` 未在 PATH，本次未执行）。
`uv/uvx/pipx` 全部不存在。
→ 第三方依赖路线 = 用 `~/miniforge3/bin/python -m venv <spike>/venv` 建 venv（**不修改 miniforge 本体**，实测可行，见 §10）。

---

## 10. 邮件通道 Spike：`mcp-email-server` 在设备侧能否落地（Lead 追加范围）

> 约束遵守：全部写入 `~/asistent-spike/`；`~/miniforge3` 只读（仅执行 `-V` 与 `-m pip --version`）；
> venv 由 miniforge 的 python **创建**，miniforge 自身目录未被写入。Spike **成功**，故按约定保留（`du -sh` = 83M，可随时整目录删除）。

### 10.1 结论先行

| 问题 | 结论 |
|---|---|
| 设备能否本地跑该通道 | **能**。无需 sudo、无需系统变更、纯用户空间、83MB 磁盘 |
| 装成没有 | **装成了**。`pip install mcp-email-server`（清华镜像）**exit 0，耗时 80s**，54 个包全部命中 **aarch64 wheel**，**零编译**（无 gcc/Rust 构建） |
| 依赖是否齐（arm64 有无编译失败） | **齐**。`pydantic-core`、`cryptography`、`cffi`、`rpds-py`、`markupsafe` 等含 C/Rust 扩展的包均有 `aarch64` 轮子 |
| 路径 | `/home/asistent/asistent-spike/venv/`（解释器 Python 3.14.7，来自 miniforge） |
| stdio 握手 | **成功**。`initialize` → `serverInfo={"name":"email","version":"1.11.1"}`、`protocolVersion=2025-06-18`、capabilities `tools/prompts/resources/experimental`；`tools/list` → **19 个工具** |
| 只能留在 Windows 吗 | **不必**。但有一个例外项见下：**设备无法 TCP 访问开发机上的实例**（防火墙），所以"复用 Windows 那台"只能从 Windows 侧使用；设备侧要自建。 |
| 需要额外系统变更吗 | **安装与运行不需要**。但：① 包要求 **Python ≥3.11**，系统 3.10 不满足 → 必须用 `~/miniforge3` 的 3.14；② **keyring 不可用** → 凭据必须走明文配置文件（见 10.4）；③ 若要做成开机常驻，仍需 §7 的 linger/sudo 决策 |

### 10.2 安装实测

```
$ ~/miniforge3/bin/python -m venv $HOME/asistent-spike/venv      → venv_exit=0
$ $SPIKE/venv/bin/python -V
Python 3.14.7
$ $SPIKE/venv/bin/python -m pip --version
pip 26.2.1 from /home/asistent/asistent-spike/venv/lib/python3.14/site-packages/pip (python 3.14)

$ time $SPIKE/venv/bin/python -m pip install --no-input -i https://pypi.tuna.tsinghua.edu.cn/simple mcp-email-server
pip_install_exit=0 elapsed=80s
Downloading cryptography-50.0.2-cp311-abi3-manylinux_2_34_aarch64.whl (4.7 MB)
Downloading pydantic_core-2.50.0-cp314-cp314-manylinux_2_17_aarch64.manylinux2014_aarch64.whl (2.0 MB)
Downloading cffi-2.1.1-cp314-cp314-manylinux2014_aarch64.manylinux_2_17_aarch64.whl (222 kB)
Downloading rpds_py-2026.9.1-cp314-cp314-manylinux_2_17_aarch64...whl (373 kB)
Downloading tomli-2.5.0-cp314-cp314-manylinux...aarch64.whl (273 kB)
Downloading markupsafe-3.0.4-cp314-cp314-manylinux2014...aarch64.whl (24 kB)
Successfully installed MarkupSafe-3.0.4 SecretStorage-3.5.0 aioimaplib-2.0.1 aiosmtplib-5.1.3 ...
                     keyring-25.7.0 loguru-0.7.3 mcp-1.30.0 mcp-email-server-1.11.1 pydantic-2.14.0
                     pydantic-core-2.50.0 pydantic-settings-2.15.0 cryptography-50.0.2 ... (共 54 个包)

$ grep -inE "building wheel|gcc|error:|failed" install.log | head
（无输出 —— 没有任何源码编译或错误行）

$ 入口点
mcp-email-server 1.11.1 ['console_scripts:mcp-email-server=mcp_email_server.cli:app',
                         'console_scripts:mcp-email-server-plugin=mcp_email_server.cli:plugin_stdio']
$ ls $SPIKE/venv/bin | head
cffi-gen-src dotenv httpx idna imap-testing-server jsonschema keyring markdown-it mcp
mcp-email-server mcp-email-server-plugin pip pip3 pip3.14 pygmentize typer uvicorn
$ du -sh $SPIKE   → 83M       df -h /  → 116G 7.2G 107G 7%
```

**包元数据（开发机侧查询 PyPI 确认约束）**：

```
name=mcp-email-server  version=1.11.1  requires_python=<4.0,>=3.11
requires_dist: aioimaplib>=2.0.1 aiosmtplib>=4.0.0 anyio>=4.5 beautifulsoup4>=4.14.3 click>=8.0.0
  filelock>=3.32.2 jinja2>=3.1.5 keyring>=25.0 loguru>=0.7.3 mcp[cli]<2,>=1.23.0
  pydantic-settings[toml]>=2.11.0 pydantic>=2.11.0 pywin32>=312; sys_platform=="win32"
  starlette>=0.40 tomli-w>=1.2.0 typer>=0.15.1 tzdata>=2025.2; sys_platform=="win32" uvicorn>=0.30
```

### 10.3 stdio 握手实测（19 个工具）

测试驱动 `~/asistent-spike/handshake.py`（把 `HOME` 重定向到 `$SPIKE/home`，把配置全部关在 Spike 内）：

```
########## A: mcp-email-server stdio (legacy, no config yet) ##########
initialize: OK  serverInfo={"name": "email", "version": "1.11.1"}  protocolVersion=2025-06-18
          capabilities=['experimental', 'prompts', 'resources', 'tools']
tools/list: OK  19 tools
   - list_available_accounts / list_email_tags / list_emails_metadata / get_emails_content
   - list_allowed_recipients / list_allowed_senders
   - send_email / forward_email / save_to_mailbox / save_draft
   - delete_emails / set_email_flags / set_email_tags / mark_emails_as_read / move_emails / archive_emails
   - list_mailboxes / get_attachment_content / download_attachment
process exit: -15        （客户端主动 terminate，正常）

########## B: mcp-email-server-plugin (plugin stdio entry) ##########
initialize: OK  serverInfo={"name": "email", "version": "1.11.1"}  protocolVersion=2025-06-18
tools/list: OK  19 tools   （与 A 完全一致）
```

> 附带证据：该 `-plugin` 入口是"直接被 MCP 宿主以 stdio 拉起"的形态——我们误传 `--help` 时它把参数当 **JSON-RPC 帧**解析并回
> `Invalid or oversized JSON-RPC frame`，说明 stdio 传输层**确实在监听并解析**标准输入。

资源占用（stdio 模式，stdin 持有打开时测量）：

```
VmRSS: 82704 kB   VmSize: 152180 kB   Threads: 1
```

### 10.4 配置与凭据（**关键运维约束**）

```
# 包内硬编码的配置路径（不是 XDG，而是 expanduser 的固定路径）
config.py:50   DEFAULT_CONFIG_PATH = "~/.config/mcp-email-server/config.toml"
config.py:51   LEGACY_CONFIG_PATH  = "~/.config/zerolib/mcp_email_server/config.toml"

$ mcp-email-server config status
mode=legacy
bootstrap_revision=0
catalog_status=not_configured
restart_required=false
$ mcp-email-server config doctor
Error: No managed database is configured. Run `mcp-email-server config init --database PATH`.

$ $SPIKE/venv/bin/python -c "import keyring;print(keyring.get_keyring())"
keyring.backends.fail.Keyring (priority: 0)      ← 没有 SecretService/gnome-keyring 后端
```

- 两种模式：**legacy**（读 `~/.config/mcp-email-server/config.toml`，本次采用）与 **managed**（`config init --database <path>` 建 SQLite 目录）。
- 凭据存取支持 **plaintext config** 或 **keyring**；本机 `keyring` 是 `fail` 后端（无 SecretService 服务）⇒ **只能用明文配置**。
- `account add` / `set-secret` 设计上"从 stdin 提示读取，绝不出现在 argv"，方向是对的。
- **运维要求**：明文凭据文件必须落在 `~/asistent-var/config/`（`chmod 700`，已在 §11 建立骨架），
  **绝不能**放进 `~/code/asistent`（会被 push 覆盖/进 Git）；`.gitignore` 已含 `.env`/`*credentials*`/`config/local.yaml` 等规则，但不要把配置放在仓库内寄希望于忽略规则。

### 10.5 端到端 IMAP 可达性（**用假凭据证明链路通，不引入真凭据**）

```
# 1) 通过 MCP 工具调用（账户名 probe-qq、假密码、host=imap.qq.com:993）
$ mcp-email-server ... stdio + tools/call
list_available_accounts -> {"account_name": "probe-qq", "account_type": "email",
                            "email_address": "probe-no-such-account@qq.com",
                            "can_receive": true, "can_send": false}
list_mailboxes -> {"content":[{"text":"Error executing tool list_mailboxes: provider_failure: mailbox discovery failed"}],
                   "isError": true}
stderr | INFO | mcp_email_server.config:get_settings - Loading legacy settings from .../config.toml
stderr | INFO | mcp_email_server.emails.classic:list_mailboxes - IMAP logout failed

# 2) 用同一库（aioimaplib）直接做对照实验
$ $SPIKE/venv/bin/python - <<'PY'
c = aioimaplib.IMAP4_SSL(host="imap.qq.com", port=993, timeout=25)
await c.wait_hello_from_server()
typ, data = await c.login("probe-no-such-account@qq.com", "invalid-probe-credential-not-a-secret")
PY
TLS+greeting: OK (IMAP server answered)
LOGIN -> NO ["b'Login fail. Account is abnormal, service is not open, password is incorrect,
              login frequency limited, or system is busy. ...'"]

# 3) 对照组：同一代码连一个不可达地址（错误类型必须不同）
$ aioimaplib.IMAP4_SSL(host="192.0.2.2", port=993, timeout=8) → 不可达
unreachable host -> TimeoutError / OSError: [Errno 113] Connect call failed ('192.0.2.2', 993)
```

**结论**：设备 → `imap.qq.com:993` 的 TLS 连接与 IMAP 协议交互**完全打通**，
失败点**只在凭据**（服务器明确回 `NO` + QQ 邮箱的错误说明），并且错误类型与"网络不可达"（TimeoutError/Errno 113）**可区分**。
⇒ 该通道在设备侧**只差真实凭据**即可跑；**未验证**真实收件/发信（本任务禁止写入任何真实凭据）。

### 10.6 复用"开发机 Windows 上已装的那台"是否可行

```
$ （设备侧 TCP 扫描开发机）
198.51.100.30:22 CLOSED (TimeoutError)    198.51.100.30:8000 CLOSED (TimeoutError)
198.51.100.30:3000 CLOSED (TimeoutError)  198.51.100.30:8080 CLOSED (TimeoutError)
198.51.100.30:3333 CLOSED (TimeoutError)  198.51.100.30:8765 CLOSED (TimeoutError)
198.51.100.30:5000 CLOSED (TimeoutError)  198.51.100.30:9000 CLOSED (TimeoutError)
```

**结论**：设备**无法**通过 TCP 访问开发机上的 `mcp-email-server`（`uvicorn`/`streamable-http`/`sse` 传输都需要入站端口），
因为 Windows 防火墙未放行且设备与开发机之间没有可用的直达链路（§6.3）。
⇒ 若坚持"复用 Windows 那台"，必须：① 在 Windows 加入站放行规则 + ② 让设备能路由到该端口（当前 `192.0.2.2` 完全不通），
③ 还要处理"设备 24h 常驻但开发机可能关机"的可用性问题——**不建议**。
⇒ 建议：**设备侧自建实例**（本次已证明可行），Windows 那台继续供本机使用，两边共享的只是邮箱账号，不是进程。

---

## 11. 载体兼容性结论（Python 3.10.12 ↔ 开发机 3.12.x）

**结论性证据（设备侧 `python3 -c` 逐一导入，全部成功）**：

```
$ python3 - <<'PY'
for m in ["sqlite3","imaplib","http.server","ssl","hashlib","urllib.request","zoneinfo","threading",
          "smtplib","email","json","csv","logging","unittest"]: importlib.import_module(m)
PY
全部 OK（§2 的 34 项里 33 项成功，唯一失败的 `tomllib` 属于 3.11+）
```

**arm64 无编译依赖**：核心用到的模块都来自 CPython 标准库（含 `_sqlite3`、`_ssl`、`_hashlib`、`zlib` 等 C 扩展），
它们在 Ubuntu 22.04 arm64 的 `python3.10-minimal`/`libsqlite3` 里**已预编译到位**（`sqlite_version 3.37.2`、`OpenSSL 3.0.2` 实测可用），
因此"纯标准库"路线在设备上**没有任何构建步骤**（对比 §10 的第三方包也恰好全有 aarch64 轮子）。

**已实测的 3.10 vs 3.12 兼容约束（设备为下限）**：

| 项 | 设备 3.10.12 | 影响 / 约束 |
|---|---|---|
| `tomllib` | **不存在** | 3.11+ 才有。禁止 `import tomllib`；配置用 `json`，或自写/第三方 `tomli`（与纯标准库冲突） |
| `typing.Self` | 3.11+ | 用字符串注解或 `TypeVar` 替代 |
| `asyncio.TaskGroup` / `except*` | 3.11+ | 禁止；异常组用普通 `try/except` |
| `datetime.UTC` | 3.11+ | 用 `datetime.timezone.utc` |
| `datetime.utcnow()` | 3.10 可跑但**已弃用** | ADR-0002 已定：改用显式 UTC 感知对象 |
| `zoneinfo` | ✅ 3.9+ | 可用（已实测） |
| `dataclass(slots=True)`、`match`、`X \| Y` 注解 | ✅ 3.10 | 可用 |
| `sqlite3.threadsafety` | `1`（module-level） | 连接不跨线程共享；每线程独立连接 |
| `sqlite3` FTS5/trigram | ✅ | 但中文语义见 §3.3 |

⇒ 开发机（3.12）与设备（3.10）的差异**集中在 3.11+ 语法糖与 `tomllib`**，
只要代码以 3.10.12 为下限并且在设备上跑契约测试（§13 待补），这个差异就是可控的。

**已实测的 3.10 下限验证（2026-10-10 13:14，设备侧真实运行）**：

```
# 部署 344cae1（49 个文件，含 contracts-v0.1 与 tests/）后在设备上执行
$ cd ~/code/asistent && python3 -V && python3 -m unittest discover -s tests -t . -p 'test_*.py'
Python 3.10.12
...........................................................................
----------------------------------------------------------------------
Ran 75 tests in 0.076s

OK
UNITTEST_RC=0
```

⇒ **冻结契约 `contracts-v0.1` 在设备 Python 3.10.12 上 75 条测试全绿**，
说明"纯标准库 + 3.10 兼容"这条载体约束在当前契约面上是**已被证据支持的**，不是假设。
（`tests/` 此前不存在，本轮部署后已可跑——原 B4 阻塞项由此解除。）

---

## 12. 风险与阻塞清单（24h 常驻当前缺什么）

### 12.1 阻塞项（不解决就无法完成 §21.7 的 24h 验收）

| ID | 缺什么 | 实测证据 | 后果 | 需要谁做什么 | 要 sudo？ |
|---|---|---|---|---|---|
| **B1** | `Linger=no`，user 服务无法脱离会话常驻 | `loginctl show-user asistent -p Linger` → `Linger=no`；当前能跑只因有 5 个会话 | 桌面会话一结束，`systemctl --user` 服务被杀 → §21.7「桌面聊天关闭仍连续运行 24h」不成立 | 用户执行一次 `sudo loginctl enable-linger asistent` | **是**（口令） |
| **B2** | 无免密 sudo | `sudo -n true` → `a password is required`（sudoers 里有 `(ALL:ALL) ALL`） | 系统级 unit、`systemd-time-wait-sync`、防火墙、`apt install`、绑定 <1024 端口全部做不了 | 用户在场执行，或明确接受退化方案（B1 的替代：§12.2） | **是** |
| **B3** | 真实邮箱凭据未提供 | §10.5 只到"认证失败"这一步 | 邮件门（§18.3）未关闭；MVP-2/AT-05 无法验收 | 用户提供账号（密码只落 `~/asistent-var/config/`，600/700，绝不入库） | 否 |
| **B4** | ~~设备上无 `tests/`，契约测试跑不了~~ **已解除（2026-10-10 13:14）** | 部署 `344cae1` 后设备侧 `Ran 75 tests ... OK`（RC=0）；此前 `TESTS_DIR=absent` | — | 已由本次部署验证；后续每次部署 `deploy.ps1` 第 5 节会自动复验 | 否 |
| **B5** | 中文检索方案未决 | §3.3：`unicode61` 下中文 2 字 `MATCH` 全部 0 行 | MVP-1「60 秒内检索到新版本」在中文语料上**静默失效**（不报错，只是查不到） | 基础 Agent 裁决 unigram / trigram / 双索引，并写进契约+测试 | 否 |
| **B6** | 开机时钟不可信（落后约 2 年 3 个月） | `journalctl --list-boots` 显示本次启动始于 `2024-07-08 11:59`；`uptime -s` 为 `2026-10-10 10:03:38`；`systemd-time-wait-sync` **disabled** | 开机头 ~2 分钟的时间戳错到 2024 年 → 审计/邮件 SINCE 窗口/"5 分钟内显示待处理"判定全部失真 | 用户 `sudo systemctl enable --now systemd-time-wait-sync`（可选），或应用启动时自检 `NTPSynchronized` 并拒绝写入业务时间戳；计时改单调钟 | 部分（enable 要 root） |
| **B7** | 手机控制台连通性未验证 | 手机不在场；`192.0.2.10:18080` 已实测本机可达 | 控制台（MVP-4）在网络层未验证 | 用户用手机接入 `192.0.2.0/24` 后访问 `http://192.0.2.10:18080/` 验证 | 否 |
| **B8** | 设备无反向通道 | §6.3：设备→开发机 TCP 全封 | 设备不能主动上报/拉取；调试只能 ssh 进入 | 接受现状（push 型协作），或用户放行特定端口 | 是（若放行） |

### 12.2 退化方案：`crontab @reboot` + `setsid nohup`（**不需要 sudo**）

实测已确认的部分：

| 环节 | 证据 | 状态 |
|---|---|---|
| cron 服务 enabled | `systemctl is-enabled cron` → `enabled` | ✅ |
| 用户级 `@reboot` 语法可安装 | `printf '@reboot /bin/true' \| crontab -` → exit 0，`crontab -l` 回显；随后 `crontab -r` 清理 | ✅ |
| 进程能脱离 SSH 会话存活 | `setsid nohup` 启动、断开 SSH、重连后 PPID=1 仍在 | ✅ |
| 重定向三件套 | 启动时 `< /dev/null`、stdout/stderr 指向日志文件 | ✅（本次探测同一手法） |
| **重启后**真的能拉起 | — | ❌ **待验证**：需一次受控重启（会影响设备既有使用，须用户同意） |

参考落地形态（待应用可运行后由 Lead/MVP-4 采纳）：

```bash
# 设备侧（用户级，无需 root）—— 示例，尚未安装
@reboot cd "$HOME/code/asistent" && ASISTENT_VAR_DIR="$HOME/asistent-var" \
        setsid nohup ./scripts/run.sh >> "$HOME/asistent-var/logs/boot.log" 2>&1 < /dev/null &
```

注意：`@reboot` 的执行时机早于网络就绪，应用必须**自带就绪重试**（不能假设开机即有网络/NTP）；
这与 B6（开机时钟）叠加，进一步说明启动逻辑要"等时钟+等网络"。

### 12.3 其它已知隐患（不阻塞，但会咬人）

| 项 | 说明 | 建议 |
|---|---|---|
| `systemd` 系统状态 `degraded` | `dnsmasq.service` failed（既有） | 健康检查只看自己的 unit，不要用 `is-system-running` |
| `keyring` 后端为 `fail` | 无 SecretService → `mcp-email-server` 只能用明文配置 | 明文凭据文件放 `~/asistent-var/config/`（700）；不要进仓库 |
| swap 是 zram | `swapon --show` → `/dev/zram0 7.8G` | 内存压力会变成 CPU 压力；长期驻留要限制堆与队列 |
| `3389`、`5555` 对外监听 | 设备已有远程桌面/adb 暴露 | 新增控制台不要扩大暴露面；令牌认证 + 仅内网 |
| 设备 python 无 pip | 见 §9.1 | 生产纯标准库；第三方通道单独 venv，不要把依赖混进载体 |
| 温度只取到空闲点 | 45.3 °C，无 throttling 计数文件 | 24h 跑起来后补测真实负载温度 |
| git 版本落差 2.34 ↔ 2.55 | 实测互操作正常 | 部署脚本不要用 2.34 不支持的新选项（如 `--recurse-submodules` 新语义） |

---

## 13. 本次实测的未覆盖项（诚实清单）

1. **24h 连续运行**、杀进程恢复、断网恢复、掉电恢复（§21.7）——需应用可运行后进行，本轮只建立基线。
2. **设备真实重启**后的 `@reboot` 行为与 RTC 状态——需用户同意的一次重启。
3. **`mcp-email-server` 真实收发邮件**——需真实凭据（禁止写入）。
4. **手机（或任何第三方设备）到 `192.0.2.10:18080`** 的连通性——手机不在场。
5. **`sudo` 提权路径**（enable-linger、系统 unit、端口 <1024）——本轮只做只读探测，未执行任何提权。
6. **`updateInstead` 在设备工作区脏时的拒绝行为**——需一次新提交才能触发，本任务禁止创建提交，故只按 git 文档描述并写入处置流程（见 `docs/env/deploy.md` §6.4）。
7. **`git push` 超过 100MB/大文件**、并发 push——未测。
8. **`~/miniforge3` 的 `conda`**（`~/miniforge3/bin/conda`）未执行，只用了它的 python + pip。
