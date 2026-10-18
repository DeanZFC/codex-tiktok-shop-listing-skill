# Windows 安装与文件传递

架构：Windows Codex 负责对话、内置浏览器和登录；WSL2 Ubuntu 负责 Python、FFmpeg、文件锁和任务状态。用户仍然提供 `D:\TikTok素材\001-蓝色牛仔裤` 这样的产品目录。任务默认位于 WSL 的 `~/TikTokVideoWorkspace/已做/日期/序号-产品`，不要移动到 NTFS 或修改进度中的 Linux 路径。最终视频和文案可复制回 Windows。

## 首次安装

1. 检查 Windows Python 3.10+（`py -3 --version`）、Git（`git --version`）和 `wsl --list --verbose`。缺少 Python/Git 时使用官方安装包或系统包管理器；已有环境直接复用。
2. 若没有 WSL2，在管理员终端执行 `wsl --install -d Ubuntu`。系统要求重启时，说明安装进度并等用户重启；不宣称已安装完成。随后首次打开 Ubuntu，用户自行创建 Linux 用户及密码。这个密码只用于本机 sudo，不是生成网站账号，也不写入对话。已有发行版可通过安装器 `--distro` 指定，不重装或注销发行版。
3. 在 Ubuntu 终端执行 `sudo apt-get update`，再执行 `sudo apt-get install -y python3 python3-venv ffmpeg`。需要 sudo 密码时让用户在终端输入；不要让用户发到聊天。
4. 在 Windows PowerShell 的仓库目录运行：

```powershell
git clone https://github.com/DeanZFC/codex-tiktok-shop-listing-skill.git
cd codex-tiktok-shop-listing-skill
py -3 install_video_windows.py
```

安装器先检测 WSL2、Python 和 FFmpeg，再在 Linux 文件系统中安装后端及虚拟环境，运行 doctor；通过后将技能复制到 Windows 的 `%CODEX_HOME%\skills`，未设置 CODEX_HOME 时使用 `%USERPROFILE%\.codex\skills`。本机连接配置为技能目录中的 `wsl-runtime.json`，只有发行版名及安装路径，不含登录信息。更新运行 `git pull --ff-only`，再 `py -3 install_video_windows.py --replace`；两边旧技能分别备份，任务目录不受影响。安装中断后可加 `--replace` 重试，不删除现有任务。

## Codex 调用入口

PowerShell 示例，`$videoSkill` 是本机已安装技能目录：

```powershell
$videoCodexDir = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$videoSkill = Join-Path $videoCodexDir 'skills\swimwear-tiktok-video-workflow'
py -3 "$videoSkill\scripts\windows.py" doctor
py -3 "$videoSkill\scripts\windows.py" start "D:\TikTok素材\001-蓝色牛仔裤"
py -3 "$videoSkill\scripts\windows.py" task "/home/用户名/TikTokVideoWorkspace/已做/日期/001-蓝色牛仔裤" status
```

JSON 中 `result` 是原始后端结果（包含 Linux 的 task_dir/state_file），`windows_paths` 是另附的 Windows 可见路径。状态仍由后端统一管理。继续执行 `task TASK_DIR COMMAND ARGUMENTS`；绝对盘符路径会经 wslpath 转换，当前发行版的 `\\wsl.localhost\发行版\...` 路径也可作参数。中文、空格和引号由参数数组传递，不能把它们拼成 `bash -c` 命令。

其他工具通过 `windows.py script openai_image.py ...`、`windows.py script toapi_image.py ...` 或 `windows.py script salvage_merge.py ...` 调用。默认网页模式不要求 API 环境变量；可选 API 模式需要在 **WSL 的进程环境** 中另行配置，Windows 环境变量不会自动视为已经配置。

## 浏览器上传与下载

每次开始前确认新电脑的 Codex 提供浏览器操作、文件上传和下载工具。`open_in_codex` 只能展示页面，不能单独完成这些操作；缺少工具时保留任务并说明具体缺项，不重复付费生成。

1. 网页工具使用 Windows 路径，不能给它 `/home/...` 或 `/mnt/d/...`。先用 `windows.py path LINUX_FILE` 得到 UNC 路径；若工具不支持 UNC，把需要上传的一张图复制到 Windows 普通文件夹。不要复制账户目录或整个产品库。
2. 在 Windows 创建专用暂存目录，例如 `D:\TikTok交换`；用独立候选名，不覆盖已验收文件：

```powershell
py -3 "$videoSkill\scripts\windows.py" copy "/home/用户名/TikTokVideoWorkspace/已做/日期/001-蓝色牛仔裤/图片制作/00-product-reference.png" "D:\TikTok交换\product-reference-001.png"
```

3. 浏览器从 `D:\TikTok交换\product-reference-001.png` 上传。进入实际生成网站页面，未登录时打开内置浏览器让用户登录 toapi.top，复核成功后继续。复用该浏览器会话，不导出 Cookie，不反复创建令牌。
4. 下载成功后先确认文件完整且来源是本次生成。用 `copy` 把 Windows 下载文件送入当前任务的图片或视频候选目录；源文件保留，目标存在时拒绝覆盖。例：

```powershell
py -3 "$videoSkill\scripts\windows.py" copy "C:\Users\用户名\Downloads\本次生成.png" "/home/用户名/TikTokVideoWorkspace/已做/日期/001-蓝色牛仔裤/图片制作/网页生成-1-1.png"
```

复制到任务前必须已按 production.md 登记提示词和本次尝试。两段视频先存入暂存子目录并验收，再依次发布顶层候选并 ingest。不要将 `copy` 视为验收成功。文件传递失败无需重新生成；检查完整性后改用新的候选名重传，不能把部分文件送去 ingest。

5. 生成提示词和文案时可通过 `windows_paths` 的 UNC 路径读写 UTF-8 文本；工具不支持 UNC 时先在 Windows 临时文件写入，再 `copy` 到尚未存在的目标。不要复制或手改工作流进度文件。完成视频和文案验收后，以 `copy` 导出到用户指定 Windows 交付目录，向用户提供这些 Windows 文件链接。

仓库自动测试覆盖 Windows 安装复制、桥接参数及 Linux 状态机；真实 WSL2 文件互通、网站登录和 Codex 浏览器上传下载需在目标电脑做首次验证。首次验证先 doctor、用隔离样例创建并恢复任务、往返复制同一图片并比对哈希；这些操作不提交生成、不扣积分。通过后再处理真实产品。不能把单元测试通过说成整台新电脑已经验证可用。
