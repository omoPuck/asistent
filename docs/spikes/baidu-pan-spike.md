# Spike：百度网盘归档通道可行性（asistent / MVP-7）

- 状态：**结论已出（条件可行）**，真实写入未执行（等待用户批准）
- 日期：2026-10-10
- 关联：SPEC §12、§18 门 4、§21.3、§21.4、§16 MVP-7、§21.8 AT-12/13/14；ADR-0003 OQ-2
- Spike 角色：`pan-spike`（接入 Spike）
- 产物：本文件 + [`spike/baidu_pan/`](../../spike/baidu_pan/)（可离线跑通的契约实现）

---

## 1. 结论先行

**条件可行。** 百度网盘作为首阶段正式归档能力可以落地，**推荐走本机 `baidu-drive` skill 底层的官方 `bdpan` CLI 通道**，而不是自建开放平台 OAuth 应用。

两条路都真实存在且端点存活，区别在**前置成本**与**配额**：

| | 推荐路线 A：`bdpan` CLI | 备选路线 B：自建开放平台应用 |
|---|---|---|
| 通道性质 | 百度官方发布的 CLI（`issuecdn.baidupcs.com`），其 App 已由百度自己过审 | 用户自建应用，走 xpan REST API |
| 前置条件 | 设备上装二进制 + **用户本人做一次授权** | **实名认证 + 创建应用 + 勾选网盘接口**，且配额受审核状态约束 |
| 是否需企业认证 | 不需要 | 不需要（个人开发者可创建 1 个应用） |
| 关键配额风险 | 未发现硬性调用频率限制 | **未过上线审核：10 次/小时**（见 §5），对逐文件归档是致命的 |
| 设备侧可行性 | **官方有 linux-arm64 原生构建**，已核验（见 §3） | 纯 HTTP + 标准库即可，但凭据与配额要用户先办 |
| 结论 | **首选** | 仅当需要独立应用身份/独立配额时才值得 |

**必须写清楚的一句话**：`baidu-drive` skill 在本机（Windows）与设备（`~/.dsh/skills/baidu-drive`）都已存在，但**设备上缺的正是引擎本身**——`bdpan` 二进制没装、也没有登录态。所以“设备上有 skill”不等于“设备上有可用通道”。

**当前唯一阻塞项**：需要用户批准在 `board` 上安装 `bdpan`（linux-arm64），并由用户本人在设备上完成一次百度账号授权。**未经此批准，本 Spike 未向真实网盘写入任何数据。**

---

## 2. 实测环境

### 2.1 设备 `board`（RK3588）

```
Linux board-host 6.1.43-rockchip-rk3588 #1.0.0 SMP aarch64
PRETTY_NAME="Orange Pi 1.0.0 Jammy"   NAME="Ubuntu"  VERSION_ID="22.04"
python3 -> /usr/bin/python3   Python 3.10.12   OpenSSL 3.0.2
nproc=8   Mem total 15964 MB (available 14998 MB)   / 116G (avail 107G)
```

```console
$ curl -s -o /dev/null -w "pan.baidu.com http=%{http_code} time=%{time_total}s\n" -I https://pan.baidu.com/
pan.baidu.com http=200 time=0.390809s
$ curl -s -o /dev/null -w "openapi.baidu.com http=%{http_code}\n" -I https://openapi.baidu.com/
openapi.baidu.com http=200 time=0.212048s
```

出网通畅（IPv6 解析到 `yiyun.n.shifen.com`），延迟 < 0.5s。

设备上**已有**：`~/.dsh/skills/baidu-drive/`（SKILL.md、`scripts/{install,login,memory-backup,uninstall,update}.sh`、6 份 reference）、`curl`/`wget`/`jq`/`git`/`rsync`。
设备上**没有**：`bdpan`、`bypy`、`BaiduPCS-Go`、`~/.config/bdpan`、`~/.bypy`、`~/.netrc`、`~/code`（不存在）。

```console
$ for t in bdpan bypy baidupcs-go BaiduPCS-Go aria2c curl wget jq git rsync; do printf "%-16s %s\n" "$t" "$(command -v $t || echo -)"; done
bdpan            -
bypy             -
baidupcs-go      -
BaiduPCS-Go      -
aria2c           -
curl             /usr/bin/curl
wget             /usr/bin/wget
jq               /usr/bin/jq
git              /usr/bin/git
rsync            /usr/bin/rsync
$ for p in ~/.config/bdpan ~/.bypy ~/.config/BaiduPCS-Go ~/.netrc; do [ -e "$p" ] && echo "EXISTS: $p" || echo "absent: $p"; done
absent: /home/asistent/.config/bdpan
absent: /home/asistent/.bypy
absent: /home/asistent/.config/BaiduPCS-Go
absent: /home/asistent/.netrc
```

### 2.2 本机（Windows，`baidu-drive` skill 宿主）

skill 已落在 `C:\Users\<you>\.dsh\skills\baidu-drive`，其引擎已安装**且已登录**：

```console
$ bdpan version
bdpan: 3.8.7
Git Commit: 32440e1
Build Time: 2026-09-03T10:23:59+08:00
二进制路径: %LOCALAPPDATA%\bdpan\bdpan.exe
配置路径: %USERPROFILE%\.config\bdpan\config.json

$ bdpan whoami
 ✓ 已登录        用户名: <已脱敏>        Token: 有效 (29 天 20 小时)

$ bdpan ls --json          # 只读列举
[]                          # exit=0：/apps/bdpan/ 当前为空
```

凭据落盘位置（**只报告位置，未读取内容**）：

```
%USERPROFILE%\.config\bdpan\config.json    452 B   （access/refresh token，加密存储）
%USERPROFILE%\.config\bdpan\.token_key      65 B   （解密密钥）
%USERPROFILE%\.config\bdpan\disclaimer_accepted.json
%USERPROFILE%\.config\bdpan\update_check.json
```

**底层通道不是 cookie/BDUSS，而是 OAuth 授权码模式**（OOB）。依据 `scripts/login.sh`：

```bash
AUTH_URL=$(bdpan login --get-auth-url --accept-disclaimer)   # 打印授权链接，有效期 10 分钟
# 用户在自己浏览器打开 → 授权 → 页面显示 32 位十六进制授权码
printf '%s\n' "$AUTH_CODE" | bdpan login --set-code-stdin --accept-disclaimer
bdpan whoami | grep -q "已登录"
```

要点：授权码经 **stdin** 回填，不进 argv；脚本对授权码做 `^[a-fA-F0-9]{32}$` 校验并在失败信息里替换成 `[授权码已隐藏]`；`reference/authentication.md` 明确 “OOB 模式” 覆盖 SSH 远程/无图形界面场景。**这对无显示的 RK3588 正好适用**。

---

## 3. 路线 A：`bdpan` CLI 能否跑在 RK3588 上 —— 已核验

### 3.1 官方提供 linux-arm64 构建

`scripts/install.sh` 内置各平台 SHA256：

```bash
VERSION="3.8.4"
CDN_BASE="https://issuecdn.baidupcs.com/issue/netdisk/ai-bdpan/installer/${VERSION}"
linux-arm64)  echo "abb39a1f7dc0bf44883bbb30b057e6e65c5db64ffdce7c2ae92dea60a136362a" ;;
```

实测下载该产物并独立校验：

```console
$ curl -fsSL -o bdpan-installer-linux-arm64 \
    https://issuecdn.baidupcs.com/issue/netdisk/ai-bdpan/installer/3.8.4/bdpan-installer-linux-arm64
size   = 14090424 bytes
sha256 = abb39a1f7dc0bf44883bbb30b057e6e65c5db64ffdce7c2ae92dea60a136362a
pinned_in_skill = abb39a1f7dc0bf44883bbb30b057e6e65c5db64ffdce7c2ae92dea60a136362a
MATCH: YES
```

**哈希与 skill 内置校验值逐字节一致** ⇒ 产物来自百度官方 CDN 且未被篡改。

### 3.2 它是原生 AArch64 可执行文件（不是 x86 或脚本）

```console
magic (前 20 字节) = 7f 45 4c 46 02 01 01 00 00 00 00 00 00 00 00 00 02 00 b7 00
is ELF : True     ELF class = 2 (64-bit)     endianness = 1 (LE)
e_machine = 0x00b7 = 183 = AArch64
```

⇒ 与 RK3588（aarch64）架构匹配。二进制内字符串显示它是 Go 写的 “dynamic installer”，安装逻辑为
`detectPlatform → downloadPackage(bdpan.tar.gz) → extractTarGz → installBinary → configurePATH`，
并从 `https://pan.baidu.com/act/v2/api/conf?conf_key=nd_ai_bdpan` 拉动态配置。
源码路径前缀 `icode.baidu.com/baidu/netdisk/nd-ai-bdpan/…`（百度内源）。

### 3.3 对 Lead 三个问题的直接回答

| 问题 | 回答 |
|---|---|
| 依赖是否纯 Python（arm64 轮子）？ | **不适用**。`bdpan` 是自带的 Go 二进制，**不是** Python 包，因此没有 arm64 轮子/编译问题。设备只需 `curl`（已有）+ 出网。 |
| 凭据如何安全搬到设备？ | **建议不搬**。推荐在设备上做一次**独立授权**（用户浏览器 + 授权码），而不是拷贝 `~/.config/bdpan/`。若用户要求复用现有登录态，那属于跨主机凭据复制（含加密 token + 密钥文件），必须用户明确批准；本 Spike 未执行、未读取其内容。 |
| 是否需要交互式扫码/登录态刷新？ | **首次授权必须人工一次**（一次性，不是每次归档）。之后 `bdpan` 自述“Token 快过期时工具会自动刷新”；开放平台文档口径为 access_token 30 天、refresh_token 10 年。**但无人值守下的自动续期未实测**，见 §11 未解决项 U-1。 |

### 3.4 尚未实测的一步

只验证到「官方 arm64 产物存在 + 哈希一致 + 确为 AArch64 ELF」。**尚未在设备上真正执行它**（执行安装器会写 `~/.local/bin` 并改 PATH，属系统变更，需用户批准）。glibc 兼容风险低（Ubuntu 22.04 为 glibc 2.35，Go 产物通常静态链接），但仍以实跑为准。

---

## 4. 路线 B：开放平台 OAuth（自建应用）—— 可行，但有配额门槛

端点全部存活，可实测：

```console
# 设备码模式（device code）——弱输入设备/无浏览器设备专用
$ curl -s -H 'User-Agent: pan.baidu.com' \
  "https://openapi.baidu.com/oauth/2.0/device/code?response_type=device_code&client_id=SPIKE_DUMMY_APPKEY&scope=basic,netdisk"
{"error":"invalid_client","error_description":"unknown client id"}      # 端点存活，按参数校验

$ curl -s ".../device/code?response_type=device_code&scope=basic,netdisk"   # 缺 client_id
{"error":"invalid_request","error_description":"The request is missing a required parameter"}

# token 端点同样存活
$ curl -s ".../token?grant_type=device_token&code=SPIKE_DUMMY&client_id=SPIKE_DUMMY_APPKEY&client_secret=SPIKE_DUMMY"
{"error":"invalid_client","error_description":"unknown client id"}

# 授权页存活；旧域名已下线
openapi.baidu.com/oauth/2.0/authorize?response_type=code&client_id=test&... → HTTP 200（<title>与百度连接</title>）
pan.baidu.com/oauth/2.0/authorize?...                                        → HTTP 404（页面不存在）

# 身份识别：access_token 是它认的凭据，BDUSS 是另一套
$ curl -s -H 'User-Agent: pan.baidu.com' "https://pan.baidu.com/rest/2.0/xpan/nas?method=uinfo"
{"errmsg":"Invalid Bduss","errno":-6,"request_id":"..."}
$ curl -s -H 'User-Agent: pan.baidu.com' "https://pan.baidu.com/rest/2.0/xpan/nas?method=uinfo&access_token=SPIKE_DUMMY"
{"errno":20017,"show_msg":"nas access token invalid","request_id":8.8588198997692e+18}
```

### 4.1 前置条件（官方文档原文）

- **个人开发者可以**，不必企业认证：
  > 「个人开发者 …… 可在控制台创建 1 个应用 …… 基础能力包括：获取网盘状态信息、获取网盘文件信息、上传、下载、管理文件 …… 审核准备材料：个人身份证号码、个人联系方式：邮箱、手机号。**可立即审核完成**」
  —— [实名认证介绍](https://pan.baidu.com/union/doc/%E4%BD%BF%E7%94%A8%E5%85%A5%E9%97%A8/%E5%AE%9E%E5%90%8D%E8%AE%A4%E8%AF%81/%E5%AE%9E%E5%90%8D%E8%AE%A4%E8%AF%81%E4%BB%8B%E7%BB%8D/)

- **应用用途可选“个人使用”，免上线审核**：
  > 「【个人使用】**免审核，应用创建后直接上线，限 10 个用户使用**」「【公开发布】面向公众分发，开发完成后提交上线审核，通过后不限使用人数」
  —— [创建应用](https://pan.baidu.com/union/doc/%E4%BD%BF%E7%94%A8%E5%85%A5%E9%97%A8/%E5%88%9B%E5%BB%BA%E5%BA%94%E7%94%A8/)

- **目录被限制在应用自己的文件夹**（2026-06-03 后创建的应用）：
  > 「以下接口仅支持访问 `/apps/{appname}` 目录下的文件：`file?method=download`、`multimedia?method=listall`、`file?method=filemanager`、`file?method=precreate`、`file?method=meta`、`file?method=move`」
  —— 同上

### 4.2 设备码模式（无浏览器设备的正解）

> 「对于**弱输入设备**，不支持浏览器或输入受限的设备（如儿童手表），推荐使用设备码模式接入授权。」
> `GET https://openapi.baidu.com/oauth/2.0/device/code?response_type=device_code&client_id=<AppKey>&scope=basic,netdisk`
> 返回 `device_code / user_code / verification_url / qrcode_url / expires_in=300 / interval=5`
> 换 token：`GET https://openapi.baidu.com/oauth/2.0/token?grant_type=device_token&code=<device_code>&client_id=<AppKey>&client_secret=<SecretKey>`
> 「Access Token 有效期 **30 天**，过期后支持刷新 …… refresh_token **有效期 10 年**」「refresh_token 只支持使用一次」
> 「使用 device_code 轮询换取 Access Token 时，注意轮询间隔，一般不能低于 **5 秒**」
—— [设备码模式授权](https://pan.baidu.com/union/doc/%E4%BD%BF%E7%94%A8%E5%85%A5%E9%97%A8/%E6%8E%A5%E5%85%A5%E6%8E%88%E6%9D%83/%E8%AE%BE%E5%A4%87%E7%A0%81%E6%A8%A1%E5%BC%8F%E6%8E%88%E6%9D%83/)

设备码模式**不需要 `redirect_uri`**，用户在自己的手机/PC 上扫码或输入用户码即可。另：无 server 的应用也可用 `redirect_uri=oob`（[接入流程](https://pan.baidu.com/union/doc/%E5%B9%B3%E5%8F%B0%E7%AE%80%E4%BB%8B/%E6%8E%A5%E5%85%A5%E6%B5%81%E7%A8%8B/)）。

---

## 5. 配额与限速（路线 B 的关键门槛）

> 权限与配额分配：

| 权限分类 | 未通过/未提交应用上线审核 | 已通过应用上线审核 |
|---|---|---|
| 接口权限 | 接口访问权：按接口单独说明；**频率控制：10 次/每小时**；次数控制：按接口单独说明 | 按需分配（白名单制） |
| 最大可使用用户数量 | **10 个用户** | 不限制 |

超限错误码：`20011`（用户数超限/路径越界）、`20012`（频率或次数超限）、`20013`（无接口权限）。
—— [权限与配额](https://pan.baidu.com/union/doc/%E4%BD%BF%E7%94%A8%E5%85%A5%E9%97%A8/%E6%9D%83%E9%99%90%E4%B8%8E%E9%85%8D%E9%A2%9D/)

**这对逐文件归档是致命的**：一个 4 文件的归档 = 4×(precreate+superfile2+create) + 若干 list ≈ 15+ 次调用，超过 10 次/小时就 `20012`。
文档把「个人使用（免审核）」与配额表里的「未通过/未提交上线审核」并列描述，**“个人使用”应用是否仍受 10 次/小时 约束，文档没有明确** ⇒ 记为未解决项 U-2（只能在拿到 AppKey 后实测）。

**路线 A（`bdpan`）不受此限制**：它是百度自家已过审的应用，配额归百度自己管理；文档侧只对开放平台第三方应用做频控，`上传能力说明` 页写明「开放平台仅对异常行为进行相应限制，不会影响正常使用」。

其它容量/大小限制（两条路一致，属账号侧）：

> 「普通用户单个上传文件大小上限为 **4 GB**；会员 **10 GB**；超级会员 **20 GB**；分片数量不得超过 **1024** 个」
—— [上传能力说明](https://pan.baidu.com/union/doc/%E5%9F%BA%E7%A1%80%E7%BD%91%E7%9B%98%E6%9C%8D%E5%8A%A1/%E4%B8%8A%E4%BC%A0/%E8%83%BD%E5%8A%9B%E8%AF%B4%E6%98%8E/)

SPEC §21.7 的单文件验收负载是 **20 MiB**，远低于 4 GB，无风险；也不会触发「>50 MB 不能用浏览器直接下载」的规则（见 §6.3）。

---

## 6. 能力矩阵

### 6.1 上传

**路线 A**：`bdpan upload <本地路径> <远端相对路径>`。
**路线 B**（xpan API）：三步串行，`precreate → superfile2(分片) → create`：

> 「`precreate` …… `block_list` 各分片 MD5 组成的 JSON 字符串数组，MD5 使用 32 位小写 …… 文件小于 4 MB 时，`block_list` 只包含文件整体 MD5；文件大于 4 MB 时，需要在本地切分并按顺序提交每个分片的 MD5」
> 响应 `block_list` = 需要上传的分片序号；**`block_list` 为空时等价于 `[0]`** ⇒ 即“秒传”（内容已在云端，无需传字节）
—— [预上传](https://pan.baidu.com/union/doc/%E5%9F%BA%E7%A1%80%E7%BD%91%E7%9B%98%E6%9C%8D%E5%8A%A1/%E4%B8%8A%E4%BC%A0/%E9%A2%84%E4%B8%8A%E4%BC%A0/)

**这对契约很重要**：`precreate` 返回空 `block_list` 就是可判定的“内容已存在”，可直接进入 `create`——正是 SPEC §21.4「相同 archive_id 重试复用未完成对象」想要的原生能力。

### 6.2 列举 / stat

`bdpan ls [目录] --json` 返回 `path / server_filename / size / isdir / md5 / server_mtime / fs_id / fsid`。
**注意**：`md5` 是百度侧的值（大文件为分片 md5 拼接），**不能当作可信内容校验**。
`stat` 没有独立子命令，需列举父目录并按 `server_filename` 匹配（本 Spike 的适配器已实现）。

### 6.3 下载与**逐文件内容哈希回读校验**

**路线 A**：`bdpan download <远端> <本地>` → 本地重算 sha256 比对。

**路线 B**：三步，且有两个硬性要求：

> 「使用 `dlink` 时，必须拼接 `&access_token=<ACCESS_TOKEN>` 参数」
> 「必须 在请求 Header 中设置 `User-Agent: pan.baidu.com`」
> 「`dlink` 有效期为 **8 小时**」「`dlink` 可能发生 HTTP 302 跳转，客户端应允许跟随跳转并保留鉴权参数」
> 「**不允许使用浏览器直接下载超过 50 MB 的文件**；超过 50 MB 的文件需使用开发者原生软件或 App 下载」
—— [下载](https://pan.baidu.com/union/doc/%E5%9F%BA%E7%A1%80%E7%BD%91%E7%9B%98%E6%9C%8D%E5%8A%A1/%E4%B8%8B%E8%BD%BD/)

`Range: bytes=...` 支持断点续传 ⇒ 可用于“下载后哈希不符则定位重取”。

**结论：两条路都具备“下载回读 + 本地重算 sha256”的完整能力**，满足 SPEC §21.4「接入方式没有可信内容校验时，需下载回读验算」。本 Spike 的实现一律以此为准，**不采信远端 md5**。

### 6.4 删除 / 覆盖

- 路线 A：`bdpan rm <路径> --force --json`（不可逆）。
- SPEC §21.3：**外部删除/覆盖必须逐次批准**。
- 本 Spike 的适配器把 `put(overwrite=True)` 与 `delete()` 全部置于**默认锁定**的写入闸门后，必须 `unlock_writes(approval_ref=...)` 才能调用；`approval_ref` 不允许为空（已实测：`[ OK ] put 被拒绝 / [ OK ] delete 被拒绝`）。

---

## 7. 失败与未知结果的判定

| 情形 | 判定 | 处理 |
|---|---|---|
| 本地路径非法 / 穿越 / 文件缺失 | `PanInvalid` | 不调用远端 |
| 远端对象不存在（父目录列举确认） | `PanNotFound` | 确定性失败，安全 |
| 远端已有**不同内容**且未授权覆盖 | `PanConflict` | **拒绝静默覆盖** |
| CLI 超时 / 非 0 退出 / 网络中断 / 崩溃 | `PanUnavailable`（**结果未知**） | 记为未成功对象 → `ARCHIVE_PENDING`；**重试前先 `stat` + 回读判定真实状态**，不盲目重传 |
| 已完成的归档出现内容不一致 | `PanConflict` | 记录不一致，**不标记归档完成**，拒绝覆盖 |

对应 SPEC §13.2「外部结果未知时不得盲目重试」与 §21.2 的 `UNKNOWN` 语义。已在离线场景 S6 中验证：注入“已落盘但结果未知”后，重试的**对象级 put 次数为 0**（靠 `stat` + 回读发现对象已存在且正确）。

**路线 B 的公共错误码**（部分，取自 [错误码](https://pan.baidu.com/union/doc/%E5%B9%B3%E5%8F%B0%E7%AE%80%E4%BB%8B/%E9%94%99%E8%AF%AF%E7%A0%81/) 与接口页）：

| errno | 含义 | 可重试 |
|---|---|---|
| `-6` | 身份验证失败（token 无效/过期；亦可表示 Bduss 无效） | 否，需重新授权 |
| `-10` | 容量不足 | 否，触发软配额告警 |
| `20011` | 路径不在允许范围 / 用户数超限 | 否 |
| `20012` | 频率或次数超限 | 等待后重试 |
| `20013` | 无接口访问权限 | 否 |
| `31045` | `access_token` 校验未通过 | 刷新 Token 后重试 |
| `31326` | 命中防盗链 | 检查 `User-Agent: pan.baidu.com` |
| `31360` | 下载链接过期 | 是（重新取 dlink） |
| `31362` | 签名错误，下载链接不完整 | 否（勿截断 dlink） |

---

## 8. 恢复能力

- 路线 A/B 都支持按 `manifest.json` 下载回读全部对象。
- 本 Spike 的 `ArchiveStore.restore()` 强制：
  1. **`protected_roots` 为必填参数**——隔离保证不允许被“忘记打开”；空列表也被拒（实测 S8.2/S8.2b/S8.3/S8.3b）；
  2. 逐文件下载 + sha256 校验；
  3. **引用闭合**校验（manifest 条目集合 == 落地对象集合）；
  4. manifest 摘要与远端完成标记比对；
  5. 默认写 `RESTORE_MODE.txt`：**只读恢复模式**，声明归档中的发送/上传/审批**不重放**、历史审批**默认失效**（SPEC §21.4）；
  6. 远端缺 manifest → 直接 `RESTORE_FAILED`，**拒绝完成**（SPEC AT-14）；
  7. 恢复记录落本地，可选发布到远端 `恢复记录/`（真实网盘默认关闭，需 ArchivePolicy 显式授权）。

---

## 9. 本地契约验证结果（与最终走哪条通道无关，首阶段硬要求）

`spike/baidu_pan/`：**零第三方依赖**（仅标准库），`PanClient` 协议 + `LocalDirPanClient`（本地目录模拟远端）+ 端到端脚本。

```console
# Windows 开发机 / Python 3.12.14
$ python3 e2e_offline.py
  合计：52/52 项通过
  RESULT: ALL CHECKS PASSED

# RK3588 设备 / Python 3.10.12 / aarch64
$ python3 e2e_offline.py
  合计：52/52 项通过
  RESULT: ALL CHECKS PASSED
$ python3 -m py_compile panclient.py archive.py e2e_offline.py bdpan_client.py smoke_bdpan_readonly.py
py_compile OK
```

11 组场景 / 52 项断言，逐条对应 SPEC 条款：

| 场景 | 覆盖条款 | 关键证据（真实输出） |
|---|---|---|
| S1 manifest 不可变 + archive_id 确定性 | §21.4 | `archive_id=a90751d5906d1d3de004543`，逐文件 `sha256` 全部打印；相同输入两次构建 id 与 digest 相同 |
| S2 上传后逐文件回读校验后才写完成标记 | §12.5 / §21.4 | `state=ARCHIVE_VERIFIED uploaded=4 readback_verified=4 marker=yes`；`marker.verification = per-file-content-sha256-readback` |
| S3 同 archive_id 重试 | §21.4 | `uploaded=0 reused=4 … marker=no`；**对象级 put = 0**，且 `get >= 每对象 1 次`（复用对象也强制回读复核） |
| S4 内容变化产生新 archive_id | §21.4 | 旧 `a90751d5…` / 新 `aff1afa8…` 并存；旧归档独立校验 `consistent=True` |
| S5 部分失败 → 待归档 | §12.5 / MVP-7 | `state=ARCHIVE_PENDING uploaded=3 failed=1 marker=no`；重试 `uploaded=1 reused=3`，**只补传失败对象** |
| S6 结果未知不盲目重传 | §13.2 | 注入“已落盘但未知”→ `ARCHIVE_PENDING`；重试**对象级 put = 0**，靠回读发现已存在 |
| S7 隔离恢复 | §12.5 / AT-14 | `manifest_digest_ok=True`、`closure_ok=True`、`RESTORE_MODE.txt=read_only`、恢复记录 `pan:个人助手归档/恢复记录/…` |
| S8 恢复防线 | §12.5 | 非空目标拒绝；受保护目录拒绝且**主数据目录零残留**（`before=1 after=1`）；不传隔离边界 → `TypeError`；`protected_roots=[]` → `PanInvalid` |
| S9 篡改检测 | MVP-7 / §12.5 | 改写远端对象后 `consistent=False mismatched=1`，再次归档抛 `PanConflict`（拒绝覆盖），篡改后恢复 `RESTORE_FAILED` |
| S10 archive_id 与不同 manifest 冲突 | §21.4 | 抛 `PanConflict`（含 `manifest_digest` 冲突信息），原归档仍 `consistent=True` |
| S11 冻结快照 | §21.4 | manifest 冻结后改写源文件：远端内容 == 快照哈希，**且 != 被改写的源文件哈希** |

另有**只读**冒烟（`smoke_bdpan_readonly.py`）：在真实 CLI 上验证适配器与写入闸门。

```console
$ python3 smoke_bdpan_readonly.py
  bdpan 已安装: True   版本: bdpan: 3.8.7   已登录: True
  —— 只读列举 /apps/bdpan 根目录 ——   条目数: 0
  [ OK ] 适配器成功解析真实 CLI 的 ls --json 输出（0 条）
  —— 写入闸门（应全部被拒绝）——
    [ OK ] put 被拒绝：上传 put 未获批准：默认锁定…（SPEC §21.3）
    [ OK ] delete 被拒绝：删除 delete 未获批准：默认锁定…（SPEC §21.3）
  RESULT: 只读冒烟通过

# 同脚本在 RK3588 上（预期 SKIP，因为引擎未安装）
  bdpan 已安装: False   探测详情: PATH 上找不到 bdpan
  [SKIP] 未安装 bdpan，跳过适配器调用测试
```

> **本 Spike 对真实百度网盘只做过只读操作**（`version` / `whoami` / `ls`）。
> 没有上传、下载、覆盖、移动或删除任何真实网盘文件。

---

## 10. 推荐架构（路线 A 首选；路线 B 兜底）

### A) 设备直连（推荐）

```
asistent 归档调度（RK3588，systemd/@reboot 常驻）
   └─ ArchiveStore（spike/baidu_pan/archive.py 的契约）
        ├─ capture_snapshot()  → 冻结快照 + 不可变 manifest
        ├─ BdpanCliPanClient   → 子进程调用本机 bdpan（远端根 /apps/bdpan/asistent/）
        └─ 逐文件回读校验（bdpan download → 本地重算 sha256）
                                      ↓
                       百度网盘 /apps/bdpan/asistent/个人助手归档/…
```

- 凭据只在设备本地：`~/.config/bdpan/`（权限 `0600`，**仓库外**，且 `.gitignore` 已覆盖 `*credentials*`/`*.token`/`secrets/` 等模式，但该目录本就不在仓库内）。
- 审批边界（SPEC §21.3）：**归档上传 = 用户预先确认 `ArchivePolicy` 后自动执行**；**删除/覆盖 = 逐次批准**（已由 `unlock_writes(approval_ref)` 在代码层强制）。
- 24h 常驻：建议给 CLI 加 `--no-check-update`，避免服务进程被动触发自更新。

### B) Windows 桥接（仅在用户不同意设备持凭据时）

```
RK3588: 产出「归档包 + 不可变 manifest + 待上传队列」（本地队列，不依赖网盘）
   └─ 传输（scp/rsync/共享目录）
        └─ Windows: baidu-drive skill / bdpan 执行搬运
             └─ 回读校验结果回写 RK3588 的归档状态
```

- 优点：设备不持网盘凭据。
- 代价：**24h 可用性被 Windows 主机的开机状态绑住**（违背 SPEC §4.1 常驻/§14.2 独立运行的精神）；跨主机传输产生一份中间副本；状态回传需要额外协议。
- 若选 B，**逐文件回读校验与 `archive_id` 幂等仍由 §9 的本地契约保证**——搬运者只负责字节搬运，不负责判定归档完成。

### 不推荐：BDUSS / cookie 通道

设备上没有 `bypy` / `BaiduPCS-Go`；但 xpan 端点确实接受 cookie 身份：

```console
$ curl -s "https://pan.baidu.com/rest/2.0/xpan/multimedia?method=listall&path=%2Fapps%2Fbdpan&access_token=..."
{"errmsg":"Invalid Bduss","errno":-6,"request_id":"8858786194315382236"}
```

第三条路存在，但：非官方通道、`BDUSS` 是长期会话密钥（等价于账号登录态，泄露即账号失守）、社区长期报告登录态失效与续期问题（见 [BDUSS 会话续期讨论](https://wenku.csdn.net/answer/ndm4w4vqqq)、[BaiduPCS-Go 使用 BDUSS+STOKEN 登录](https://github.com/tfgzs/BaiduPCS-Go)）。**账号风控/封号风险本 Spike 未做实测**（不拿用户真实账号去撞风控）。结论：不值得为一个已有官方通道的能力承担这个风险。

---

## 11. 风险与许可

| 编号 | 风险 | 影响 | 处置 |
|---|---|---|---|
| R-1 | 设备上尚未实跑 arm64 安装器 | 仅“架构+哈希”已证明，未证明可执行 | 需用户批准后实跑一次（见 §12） |
| R-2 | CLI 自更新可能打断 24h 服务 | 服务异常 | 一律加 `--no-check-update`；如需升级走显式变更流程 |
| R-3 | 凭据是“账号级”能力（可读写整个 `/apps/bdpan/`） | 误操作面大 | 归档只写 `asistent/` 子前缀；删除/覆盖走写入闸门 + 逐次审批 |
| R-4 | 百度侧 `md5` 不可信 | 假“完整归档成功” | **一律下载回读 + 本地 sha256**，不采信远端哈希 |
| R-5 | 归档内容可能含敏感信息 | 泄密 | `ArchivePolicy.exclude_globs` 排除 `*.token`/`secrets/`/`.env`；§21.4 的**打包后秘密扫描**尚未实现（本 Spike 范围外，须在正式实现补齐） |
| R-6 | 第三方 CLI 条款/许可 | 合规 | `bdpan` 来自百度官方 CDN，安装时展示百度免责声明；**首次使用须向用户完整展示安全须知并取得确认** |
| R-7 | 归档范围/保留策略未定 | 可能超配额或误删 | §21.4 的软配额（80%）、30 日/12 个月保留策略需在实施阶段实现 |
| R-8 | 授权码被误打进对话/日志 | 凭据泄露 | 授权码只经 stdin 与交互式 `read -s` 回填；**禁止**出现在对话、argv、日志、仓库 |

---

## 12. 需要用户提供什么（精确到字段与授权范围）

### 12.1 路线 A（推荐）

| # | 需要用户做的事 | 精确到什么程度 |
|---|---|---|
| 1 | **批准在 `board` 上安装官方 `bdpan` CLI** | 源：`https://issuecdn.baidupcs.com/issue/netdisk/ai-bdpan/installer/3.8.4/bdpan-installer-linux-arm64`；SHA256 `abb39a1f…362a`（已独立核验）；安装位置建议 `~/.local/bin`；会改 PATH。**装完后本 Spike 才能实跑 §3.4** |
| 2 | **由用户本人在设备上完成一次授权** | 运行 `bash ~/.dsh/skills/baidu-drive/scripts/login.sh`；浏览器打开输出的授权链接（10 分钟有效）→ 授权 → 复制 **32 位十六进制授权码** → 粘贴到 SSH 交互提示。**授权码不得经对话/日志/argv**；该操作不可代做 |
| 3 | **确认 `ArchivePolicy`（SPEC §21.3）** | 必填字段：源目录/资料类型白名单；目标账号与根目录（建议 `/apps/bdpan/asistent/`）；**敏感数据排除项**；调度（默认每日 03:00 Asia/Shanghai）；单次体积上限；保留策略；策略有效期；撤销方式 |
| 4 | **确认归档根目录名** | 建议 `我的应用数据/bdpan/asistent/个人助手归档/{事务归档,恢复记录,…}`（SPEC §12.4 的逻辑目录一一对应） |
| 5 | 确认是否允许**定期自动归档**（免逐次审批） | 对应 SPEC §21.3「百度网盘定期/事务自动归档：用户预先确认 ArchivePolicy，严格在授权范围内执行」 |

### 12.2 路线 B（仅当用户不希望设备持有凭据）

需要用户确认：由 Windows 侧（`baidu-drive` skill）承担搬运；设备与 Windows 之间的传输通道（scp/rsync/共享目录）；以及“24h 可用性依赖 Windows 开机”这一代价是否接受。

### 12.3 明确**不需要**用户提供

- ❌ 任何 `token` / `cookie` / `BDUSS` / `AppSecret` / 密码的**内容**。
- ❌ 让本 Spike 复制 `~/.config/bdpan/`（含加密 token 与密钥文件）——不搬，改为设备独立授权。
- ❌ 任何 32 位授权码的文本转发（必须由用户直接输入到设备终端）。

### 12.4 仅当用户改选路线 B（自建开放平台应用）时才需要

| 字段 | 形式 | 最小权限范围 |
|---|---|---|
| 实名认证 | 用户在 `pan.baidu.com/union/console` 自行完成（个人：身份证 + 邮箱 + 手机，可即时通过） | — |
| `AppKey` / `SecretKey` | 由用户写入设备上的受保护文件（**推荐环境变量或 `0600` 的仓库外文件**），**不要贴进对话** | 应用用途选**「个人使用」**（免审核）；能力只需 `basic` + `netdisk`；目录自然被限在 `/apps/{appname}` |
| 授权方式 | 选**设备码模式**（无需 `redirect_uri`，最适合无浏览器设备） | `scope=basic,netdisk` |

> 注：`SecretKey` 一旦落到设备，就等同于该应用的网盘读写能力；`.gitignore` 已覆盖 `.env` / `*credentials*` / `secrets/`，但仍建议放在仓库外的 `0600` 文件。

---

## 13. 未解决项 / 后续验证清单

| 编号 | 未解决项 | 影响 | 验证方式 |
|---|---|---|---|
| U-1 | **无人值守下的自动续期**未实测。access_token 约 30 天（实测剩余 29 天 20 小时），refresh_token 理论 10 年；CLI 自述会自动刷新 | 24h 服务可能某天突然 `-6` 失败 | 装好后记录 `bdpan whoami` 有效期随时间的变化，跨一个刷新周期观察；若不能自动刷新，则**必须**在手机端显式提示“需要重新授权”，不得假装可用 |
| U-2 | 「个人使用（免审核）」应用是否仍受 **10 次/小时** 限流，文档未明确 | 决定路线 B 是否可用 | 拿到 AppKey 后实测 `20012` 触发阈值 |
| U-3 | `bdpan upload` 的**重名冲突策略**（覆盖？还是生成 `xxx(1)`？） | 影响 `put` 语义与“不覆盖已校验内容”的保证 | 装好后在 `asistent/` 前缀内用一次性测试文件验证 |
| U-4 | arm64 装载器的 glibc/依赖实际兼容性 | 决定 A 能否成立 | 批准后实跑 `--version/--help` 与安装 |
| U-5 | 百度侧 `md5` 与本地 sha256 的对应关系（大文件分片规则） | 仅影响“能否省掉回读”的优化 | 用小+大文件各一，比对 `ls --json` 的 md5 与本地计算值；**即便吻合也不作为唯一依据** |
| U-6 | 单账号并发/多设备同时持有 token 的行为 | 影响 Windows 与设备并存 | 设备授权后观察 Windows 侧是否被踢 |
| U-7 | 归档**秘密扫描**未实现 | §21.4 明文要求 | 正式实现阶段补齐（打包后扫描 + 排除清单，命中即阻止归档） |

---

## 14. 复现本次结论的命令

```bash
# 设备环境与出网
ssh board 'uname -a; python3 -VV; nproc; free -m | head -2; df -h / | tail -1'
ssh board 'curl -s -o /dev/null -w "pan=%{http_code}\n" -I https://pan.baidu.com/'

# arm64 产物核验（下载到临时目录，只算哈希，不执行）
curl -fsSL -o /tmp/bdpan-arm64 https://issuecdn.baidupcs.com/issue/netdisk/ai-bdpan/installer/3.8.4/bdpan-installer-linux-arm64
sha256sum /tmp/bdpan-arm64     # 期望 abb39a1f7dc0bf44883bbb30b057e6e65c5db64ffdce7c2ae92dea60a136362a
head -c 20 /tmp/bdpan-arm64 | xxd   # 期望 7f454c46 0201 0100 ... 0200 b700 (e_machine=183=AArch64)

# 开放平台端点存活
curl -s -H 'User-Agent: pan.baidu.com' \
  'https://openapi.baidu.com/oauth/2.0/device/code?response_type=device_code&client_id=SPIKE_DUMMY&scope=basic,netdisk'

# 本机通道只读检查（不写网盘）
bdpan version && bdpan whoami && bdpan ls --json

# 离线契约验证（不联网、不接触真实网盘）
python3 spike/baidu_pan/e2e_offline.py
python3 spike/baidu_pan/smoke_bdpan_readonly.py
```
