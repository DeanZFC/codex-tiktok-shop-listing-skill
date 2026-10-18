# 新电脑安装与账号

公开仓库：<https://github.com/DeanZFC/codex-tiktok-shop-listing-skill>。GitHub 拉取不需要登录。

仓库只安装一个主 Skill：`manage-tiktok-shop-listings`。macOS/Linux 使用根目录 `python3 install_workflow.py`；Windows 使用 `py -3 install_workflow_windows.py`，详细步骤见 [windows.md](windows.md)。旧 `install_video*.py` 仅是兼容转发。

系统依赖：Python 3.10+、Git、FFmpeg/FFprobe。安装器把 Skill 放到 `${CODEX_HOME:-~/.codex}/skills/manage-tiktok-shop-listings`，创建独立 `.venv` 并安装依赖。更新先 `git pull --ff-only`，再运行安装器 `--replace`；旧安装会备份，商品工作区和任务不在 Skill 安装目录中。

安装后运行 `scripts/run.py doctor`。doctor 不发付费请求、不登录网站、不输出密钥。默认网页生图/视频不需要 API Key；`api-first` 才从本机环境读取 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`。

## 按需登录

| 服务 | 何时需要 | 处理方式 |
| --- | --- | --- |
| Codex 桌面端 | 开始使用 | 用户在应用内登录 |
| 当前供货文档平台 | 供货和成本核验 | Codex 打开内置浏览器，用户登录后继续 |
| 妙手 | 编辑、清理、认领 | Codex 打开内置浏览器，用户登录后继续 |
| TikTok Shop | 店铺发布和读取商品 ID | Codex 打开内置浏览器，用户登录后继续 |
| toapi.top | 网页生图/视频 | Codex 打开当前模型页，用户登录后继续 |
| Microsoft Edge 妙手插件 | 外部商品页采集 | 用户在已打开页面手工点插件 |

无需预先一次性登录。到达当前步骤才打开对应页面。不要索取、复制或迁移密码、验证码、Cookie、Token 或浏览器配置；复用新电脑本机会话。Otuapp/Zexapi 和 ai.chengtingkj.com 不属于新流程必登项。

## 产品目录和本机状态

用户提供一个单产品目录，包含真实图片以及可核实的文案、尺寸或标签。主入口自动创建 `~/TikTokShopWorkspace`，上架资料和视频统一保存到 `链接/YYYY-MM-DD/NNN-货号`。供应商目录保持原样。

配置、永久去重历史和发布记录位于工作区 `.tiktok-shop-listing`，不会进入 GitHub。换电脑若只想开始新商品，不迁移任何旧状态；若要延续去重，安全复制完整 `history.json` 并保持商品库相对路径一致。旧进行中视频任务还依赖绝对路径和文件身份，不能只复制进度 JSON。

`run.py` 管理本地状态；网页导航、上传、生成、下载和视觉验收由 Codex 浏览器与媒体工具完成，不是纯后台无人值守服务。目标电脑如果只能打开网页但没有上传/下载控制能力，应保留任务并报告具体缺失，不得声称已自动完成。
