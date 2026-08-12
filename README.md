# TikTok Shop 通用上架 Skill

这是一个跨品类的 Codex Skill，用于管理 TikTok Shop 商品选品、永久去重、实时供货核验、销量链接排序、妙手手动采集、商品编辑和采集箱清理。

仓库只包含流程、脚本和默认配置，不包含商品库、选品历史、Cookie、Token、密码或浏览器登录状态。

## 安装

在另一台电脑的 Codex 中直接说：

```text
请从 GitHub 私有仓库 DeanZFC/codex-tiktok-shop-listing-skill 安装 skills/manage-tiktok-shop-listings 这个 Skill。如果需要认证，先让我登录 GitHub。
```

这是私有仓库。新电脑必须先登录有权访问该仓库的 GitHub 账号。推荐安装 GitHub CLI 后运行：

```bash
gh auth login
```

也可以在当前终端临时设置具有该私有仓库读取权限的 `GH_TOKEN`。不要把 Token 写入本仓库、配置文件或命令历史。

认证完成后，可以使用 Codex 自带的安装器：

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

编辑 `<WORKSPACE>/.tiktok-shop-listing/config.json`，设置商品库目录、输出目录和实时供货文档。完整字段说明见 Skill 内的 `references/configuration.md`。

默认业务规则：

- 每次处理一款，输出到日期目录下的 `001-货号` 文件夹。
- 删除低销量输出目录后，历史仍永久避免重复选品。
- 用户链接顺序代表销量从高到低。
- 价格低于 `50` 时改为 `50.01`，等于或高于 `50` 时保持原价。
- 所有 SKU 库存改为 `19`。
- 默认最终保留三张同款图片，颜色、型号、规格和变体以供应商资料为准。
- 采集前必须按本机配置检查实时供货文档的所有工作表。

需要跨电脑延续去重时，另行安全迁移工作区内的 `.tiktok-shop-listing/history.json`，不要把它提交到代码仓库。
