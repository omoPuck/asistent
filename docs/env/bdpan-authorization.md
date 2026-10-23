# bdpan 授权操作指引（需要你本人执行一次）

> 已在 `board` 上装好 **bdpan 3.8.7**（`~/.local/bin/bdpan`，ELF aarch64 静态链接，
> 由 `baidu-drive` skill 自带安装器安装，安装器内置 SHA256 校验）。
> 现在**只差一次授权**。授权码属于凭据：**不要**发到对话里、不要写进任何文件后提交。

## 你需要做的（约 2 分钟）

1. **开一个交互式 SSH 会话**连到设备（用你自己的终端连 `board`；必须能交互输入，不能用非交互命令）。

2. 运行 skill 自带的登录脚本：

   ```bash
   bash ~/.dsh/skills/baidu-drive/scripts/login.sh
   ```

3. 屏幕会打印一条**授权链接**。用手机或电脑浏览器打开它，登录你的百度账号并点"同意/授权"。

4. 百度会显示一个**32 位授权码**。把它复制，回到终端里粘贴到提示符处（**输入不回显**），回车。

   > 终端里粘贴时看不到字符是正常的；授权码只在这台设备上被本地处理。

5. 验证：

   ```bash
   ~/.local/bin/bdpan whoami          # 应显示你的百度账号，而不是"未登录"
   ~/.local/bin/bdpan ls --json       # 应能列出 /apps/bdpan/ 下的内容
   ```

## 重要提示

- **约 30 天有效期**：token 到期后需要你重新做一次上面第 2–4 步。
  无人值守自动续期**尚未实测通过**（未解决项 U-1），所以系统会在需要重新授权时
  在手机控制台显式提示"需重新授权"，**不会**假装通道可用。
- **不要把 Windows 上已登录的 `~/.config/bdpan/` 复制到设备**：跨主机复制凭据风险高，
  也绕过了独立的授权审计。设备独立授权即可。
- 现在**不要**手动跑 `bdpan upload` / `bdpan delete`：归档动作会由系统按 `ArchivePolicy`
  执行（策略参数等你确认，见 `docs/STATUS.md` U-6）。
- 服务调用方式：绝对路径 `~/.local/bin/bdpan` + `--no-check-update`
  （避免 CLI 自更新打断 24h 运行）；不需要你把 `~/.local/bin` 加进 PATH，也不需要 sudo。

## 故障排查

| 现象 | 处理 |
|---|---|
| `bdpan: command not found` | 用绝对路径 `~/.local/bin/bdpan`（非交互 SSH 的 PATH 不含 `~/.local/bin`） |
| 提示 `未登录` | 重新执行上面第 2–4 步 |
| 授权链接打不开 | 换手机流量/其它网络重试；设备侧出网实测正常（`pan.baidu.com` 200） |
| 脚本提示缺少参数 | 脚本会检查 `--get-auth-url/--set-code-stdin/--accept-disclaimer`，若报错请把**原始报错**告诉 Lead（不要贴授权码） |
