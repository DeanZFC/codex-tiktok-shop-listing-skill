# TikTok 商品上架与视频制作 Skills

仓库包含商品上架与视频制作两套 Codex Skills：上架流程管理选品、供货与成本核验、妙手采集和 SKU 编辑；视频流程从指定产品资料目录生成参考帧、视频及发布文案。

仓库只包含流程、脚本和默认配置，不包含商品库、选品历史、Cookie、Token、密码或浏览器登录状态。

## 新电脑制作视频

视频 Skill 位于 `skills/swimwear-tiktok-video-workflow`，支持牛仔裤、泳装和其他服装。新电脑安装一次，以后在 Codex 对话中给出单个产品的内容/图片文件夹即可。

把下面这段发给新电脑的 Codex，并替换最后的产品目录：

```text
请从 https://github.com/DeanZFC/codex-tiktok-shop-listing-skill 拉取代码。
阅读 README.md，安装 swimwear-tiktok-video-workflow 技能和本地依赖，运行 doctor。
然后用这个技能制作视频。产品内容和图片目录：<这里填本机产品文件夹的绝对路径>。
默认网页生图和视频生成；需要登录哪个网站时，打开 Codex 内置浏览器让我登录。
登录成功后继续，不要反复创建令牌。视频模型按页面积分从便宜到贵尝试，每次低于300积分，遵守工作流重试上限。
成片后同时保存店铺标题、内容、尺寸、视频正文与最多5个标签，以及不超过30字符的链接标题。
```

macOS/Linux 安装命令：

```bash
git clone https://github.com/DeanZFC/codex-tiktok-shop-listing-skill.git
cd codex-tiktok-shop-listing-skill
python3 install_video.py
```

依赖 Python 3.10+ 和 FFmpeg/FFprobe。安装器创建独立 Python 环境，安装路径默认为 `~/.codex/skills/swimwear-tiktok-video-workflow`（支持 CODEX_HOME）。安装后让 Codex 阅读该技能；若未自动发现，重新打开对话或直接指定 SKILL.md 的绝对路径。

```bash
~/.codex/skills/swimwear-tiktok-video-workflow/.venv/bin/python ~/.codex/skills/swimwear-tiktok-video-workflow/scripts/run.py doctor
~/.codex/skills/swimwear-tiktok-video-workflow/.venv/bin/python ~/.codex/skills/swimwear-tiktok-video-workflow/scripts/run.py start "/本机/产品资料目录"
```

start 创建本地任务，Codex 随后按技能继续网页生成、下载和验收。默认输出 `~/TikTokVideoWorkspace/已做/日期/001-货号`，可传 `--workspace` 改位置。恢复任务用 `run.py task TASK_DIR status`。

账号要求：Codex 桌面端需要用户正常登录；生成阶段只需 toapi.top 账号和积分。API 密钥是可选配置；默认网页流程无需它。公开 GitHub 拉取无需登录，TikTok Shop/妙手仅上架发布时需要，Otuapp/Zexapi 不属于新流程必登项。

生成页面未登录或会话失效时，Codex 显示内置浏览器登录页，等待用户登录后继续。账号密码、Cookie、令牌和产品资料不提交到 Git。网页自动操作依赖目标电脑实际提供的浏览器控制工具。Windows 原生 Python 暂不支持此视频状态机；可在 WSL2 运行 CLI，但桌面浏览器文件互通仍需目标机验证。

更新时在克隆目录执行 `git pull --ff-only` 和 `python3 install_video.py --replace`，安装器先备份旧技能。旧电脑进行中任务和跨机去重不会自动迁移。详见 [视频安装说明](skills/swimwear-tiktok-video-workflow/references/setup.md)。

## 商品上架 Skill 安装

这是公开仓库，不需要登录 GitHub。最简单的安装方式是在另一台电脑的 Codex 中直接发送下面这句话和地址：

```text
请安装这个 GitHub Skill：
https://github.com/DeanZFC/codex-tiktok-shop-listing-skill/tree/main/skills/manage-tiktok-shop-listings
```

也可以使用 Codex 自带的安装器手动安装：

```bash
python ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo DeanZFC/codex-tiktok-shop-listing-skill \
  --path skills/manage-tiktok-shop-listings
```

Windows PowerShell：

```powershell
py "$env:USERPROFILE\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py" `
  --repo DeanZFC/codex-tiktok-shop-listing-skill `
  --path skills/manage-tiktok-shop-listings
```

安装后在下一轮 Codex 任务中即可使用。Skill 需要 Python 3.10+；规格图和完成前的真实图片校验需要 Pillow。如本机缺少 Pillow，运行：

```bash
python -m pip install Pillow
```

## 首次配置

在新电脑准备一个业务工作区，然后运行：

```bash
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" init-config
```

编辑 `<WORKSPACE>/.tiktok-shop-listing/config.json`，设置商品库目录和输出目录。供货文档按品类独立配置：首次使用或换品类后，必须由用户手动重新提供文档，不能沿用以前品类的地址；填写地址时还要把当前 `catalog_roots` 写入 `availability.bound_catalog_roots`。该文档既用于确认商品仍在供货，也用于采集原价低于阈值时取得对应产品或 SKU 的供货成本。完整字段说明见 Skill 内的 `references/configuration.md`。

默认业务规则：

- 每次处理一款，输出到日期目录下的 `001-货号` 文件夹。
- 删除低销量输出目录后，历史仍永久避免重复选品。
- 用户链接顺序代表销量从高到低。
- 每个 SKU 单独定价：采集原价 `P >= 50` 时保持原价；`P < 50` 时从当前品类供货文档取得对应成本 `C`，最低允许价为 `C * 1.05` 向上取整到分，最终价为原价与最低允许价中的较高者。无法确认成本、币种、计价单位或 SKU 对应关系时停止。
- 所有 SKU 库存改为 `19`。
- 默认最终保留三张同款图片，颜色、型号、规格和变体以供应商资料为准。
- 换品类后旧供货文档自动失去采集资格；未提供新文档时仍可选品和准备扫同款资料。
- 采集前必须按当前品类配置检查实时供货文档的所有工作表；采集后遇到低价 SKU 时还要记录精确成本及价格计算证据。
- 编辑清理完成后，默认自动认领到 TikTok 采集箱，并按 `publication` 配置选择店铺、立即发布；最终核对商品、店铺、数量和附加项通过后自动点击“确认发布”。
- 浏览器分工固定为：外部 TikTok/Temu/Amazon 商品页的插件采集使用 Microsoft Edge；供货文档、妙手、认领、编辑和发布页面使用 Codex 内置浏览器。
- 常规编辑、核验和导航连续执行，不逐步询问；一个明确批次确认一次后，同批次的重复“确定/继续”弹窗自动处理。新的永久删除批次仍需一次动作时确认；配置启用自动发布时，最终发布不再重复询问。

需要跨电脑延续去重时，另行安全迁移工作区内的 `.tiktok-shop-listing/history.json`，不要把它提交到代码仓库。
