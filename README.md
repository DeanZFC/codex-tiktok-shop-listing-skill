# TikTok Shop 上架与视频一体化 Skill

本仓库只有一个主流程：`manage-tiktok-shop-listings`。它从一个单产品目录完成上架资料、供货与成本核验、妙手采集编辑、TikTok Shop 发布确认，再为同一已发布商品制作视频和发布文案。旧 `swimwear-tiktok-video-workflow` 仅保留历史提示兼容，不含第二套实现。

仓库不包含商品、历史、Cookie、Token、密码或浏览器登录状态。

## 新 Windows 电脑

把下面整段发给新电脑 Codex，只替换产品目录：

```text
请从 https://github.com/DeanZFC/codex-tiktok-shop-listing-skill 拉取代码，
安装 manage-tiktok-shop-listings 完整工作流。
这是 Windows 电脑。产品目录：D:\产品资料\当前商品。
从该目录完成商品资料、供货核验、妙手采集编辑和 TikTok Shop 发布；
只有确认每个目标店铺发布成功并记录 TikTok 商品 ID 后，才继续为同一商品目录制作视频。
视频完成后生成 tk发布内容.txt（正文加最多5个标签）和 视频链接标题.txt（不超过30字符）。
需要哪个网站或账号时，打开对应内置浏览器让我登录；登录后继续并复用本机会话。
Edge 妙手插件点击、验证码、系统重启、首次 WSL 用户创建或 sudo 密码输入时再让我操作。
其他命令、安装、保存、发布、重试、下载、验收和剪辑由你自动执行，不要让我手工运行脚本。
不要读取或上传旧电脑的密码、Cookie、Token；不要把产品资料和 history 提交 GitHub。
```

新电脑实际需要按需登录：Codex 桌面端、当前供货文档平台、妙手、TikTok Shop、toapi.top。公开 GitHub 无需登录。

## 安装入口

Windows：

```powershell
git clone https://github.com/DeanZFC/codex-tiktok-shop-listing-skill.git
cd codex-tiktok-shop-listing-skill
py -3 install_workflow_windows.py
```

macOS/Linux：

```bash
git clone https://github.com/DeanZFC/codex-tiktok-shop-listing-skill.git
cd codex-tiktok-shop-listing-skill
python3 install_workflow.py
```

系统依赖 Python 3.10+、Git、FFmpeg/FFprobe。Windows 视频和状态后端运行于 WSL2。更新用 `git pull --ff-only` 后运行对应安装器 `--replace`。详细说明见主 Skill 的 [setup.md](skills/manage-tiktok-shop-listings/references/setup.md) 和 [windows.md](skills/manage-tiktok-shop-listings/references/windows.md)。

## 关键门禁

- 上架输出使用 `链接/日期/001-货号`；视频继续使用同一目录。
- `mark-collected` 只表示妙手编辑完成，不表示发布成功。
- `mark-published` 必须记录每个配置店铺的唯一 TikTok 商品 ID，之后才允许 `start-video`。
- 视频两段各约 15 秒，每段有效画面内英语口播至少覆盖三分之二；模型单次严格低于 300 积分并按便宜到贵尝试。
- 最终保存 `final.mp4`、正文加最多 5 个标签，以及不超过 30 字符的链接标题。
