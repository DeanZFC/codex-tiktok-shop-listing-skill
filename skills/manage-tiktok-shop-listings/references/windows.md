# Windows 安装与执行

架构：Windows Codex 负责对话、内置浏览器和登录；WSL2 Ubuntu 负责统一上架历史、Python、FFmpeg、锁和视频状态。用户提供 `D:产品资料当前商品`，产品文件可留在 Windows；业务工作区默认在 WSL 的 `~/TikTokShopWorkspace`。

## 首次安装

Codex 自动检查 Windows Python 3.10+、Git 和 `wsl --list --verbose`。没有 WSL2 时打开管理员终端执行 `wsl --install -d Ubuntu`；需要重启就等待用户重启。首次启动 Ubuntu 时用户自行创建 Linux 用户和密码。sudo 提示只让用户在终端输入，不要让用户把密码发到聊天。

在 Ubuntu 安装 `python3 python3-venv ffmpeg` 后，Codex 在 Windows 仓库目录运行：

```powershell
git clone https://github.com/DeanZFC/codex-tiktok-shop-listing-skill.git
cd codex-tiktok-shop-listing-skill
py -3 install_workflow_windows.py
```

安装器先验证 WSL2、Python、FFmpeg/FFprobe，在 Linux 文件系统安装后端并运行 doctor，再把同一个主 Skill 复制到 `%CODEX_HOME%\skills\manage-tiktok-shop-listings`（未设置时为 `%USERPROFILE%\.codex\skills`）。`wsl-runtime.json` 只保存发行版名和后端路径。更新用 `git pull --ff-only` 和 `py -3 install_workflow_windows.py --replace`。

## 统一入口

```powershell
$codexDir = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$skill = Join-Path $codexDir 'skills\manage-tiktok-shop-listings'
py -3 "$skill\scripts\windows.py" doctor
py -3 "$skill\scripts\windows.py" start "D:\产品资料\当前商品" --product-code "SUPPLIER-CODE"
py -3 "$skill\scripts\windows.py" listing history
py -3 "$skill\scripts\windows.py" start-video "/home/用户名/TikTokShopWorkspace/链接/日期/001-货号"
py -3 "$skill\scripts\windows.py" task "/home/用户名/TikTokShopWorkspace/链接/日期/001-货号" status
```

Windows 盘符和当前 WSL 发行版 UNC 路径会转换为 Linux 路径；参数以 argv 数组传递，不拼接 `bash -c`。任务 JSON 保留 Linux 身份，同时返回 `windows_paths` 便于浏览器和用户查看。显式 `--workspace` 必须在 WSL Linux 文件系统，不能放 `/mnt/c` 或其他 Windows 盘。

## 浏览器文件传递

网页上传使用 Windows 路径。若浏览器不支持 WSL UNC，使用专用 Windows 暂存目录并通过 `windows.py copy SOURCE DESTINATION` 往返传递单个素材或下载文件。copy 不覆盖目标；失败时改用新文件名，不重新付费生成。不要复制账户目录、整个商品库或 `工作流进度.json`。

新电脑第一次执行只做 doctor、隔离样例创建/恢复、单文件往返和哈希比对；不提交生成、不扣积分。真实浏览器登录、上传下载和 Edge 插件仍需在该电脑实际验证，单元测试不能替代。
