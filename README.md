# TikTok Shop 通用上架 Skill

这是一个跨品类的 Codex Skill，用于管理 TikTok Shop 商品选品、永久去重、实时供货与成本核验、销量链接排序、妙手手动采集、逐 SKU 定价、商品编辑和采集箱清理。

仓库只包含流程、脚本和默认配置，不包含商品库、选品历史、Cookie、Token、密码或浏览器登录状态。

## 安装

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

需要跨电脑延续去重时，另行安全迁移工作区内的 `.tiktok-shop-listing/history.json`，不要把它提交到代码仓库。
