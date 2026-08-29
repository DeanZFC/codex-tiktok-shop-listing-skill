---
name: manage-tiktok-shop-listings
description: Manage category-agnostic TikTok Shop product selection and listing workflows from supplier folders, including configurable catalogs, permanent cross-folder de-duplication, English listing copy, a supplier-original scan image, an optional PNG specification chart, mandatory configurable all-worksheet availability and supplier-cost checks, sales-ranked marketplace links, Microsoft Edge and manual Miaoshou-plugin collection, evidence-backed SKU pricing and inventory rules, image and variant cleanup, final record deletion, and configurable TikTok claim and store publishing. Use when the user asks for the next product, prepares or edits a TikTok Shop listing, supplies sales-ranked product links, confirms Miaoshou collection, changes product categories, moves the workflow to another computer, or needs deleted low-sales products to remain permanently excluded.
---

# TikTok Shop 通用上架

## 工作方式

每次只处理一款商品，按以下状态推进：

```text
候选 -> 已预留/待扫同款 -> 已核验供货 -> 待妙手采集 -> 已编辑并清理完成 -> TikTok 已认领/待发布 -> 已发布
```

使用 `scripts/listing_workflow.py` 维护本机配置和永久历史。Skill 目录只保存程序；商品库、配置、历史、浏览器登录状态和妙手登录状态始终留在用户工作区，不得上传到 Skill 仓库。

## 浏览器分工

- 只有打开 TikTok、Temu、Amazon 等外部商品页并让用户手动点击妙手插件采集时，使用 Microsoft Edge；短链接跳转后的商品页也保持在 Edge 中。
- 供货文档核验、妙手公用采集箱、商品编辑与清理、TikTok 认领、店铺选择和发布，统一使用 Codex 内置浏览器，并沿用其中的登录会话。
- 不要把 Edge 的插件弹窗操作替代为内置浏览器操作；也不要在 Edge 中执行供货核验、商品编辑或发布。若内置浏览器没有可用的已登录页面，先提示用户在内置浏览器登录或打开页面。

## 确认与连续操作

- 供货核验、页面导航、商品编辑、保存、刷新复核、认领准备和发布配置等常规步骤连续执行，不为每一步向用户重复询问“是否继续”。
- 用户对一个明确批次作出动作时确认后，该确认覆盖该批次内为完成同一操作而出现的重复“确定”“继续”等确认弹窗；逐个处理这些弹窗，不再逐条询问用户。批次范围必须由目标商品及明确的记录 ID、店铺或操作定义，不得扩展到新商品或新记录。
- 用户在当前任务或工作区明确写出“确认删除”“以后自动确认”“自动执行”等持续授权时，将其视为本 Skill 的业务确认偏好，直到用户明确撤回；对当前唯一商品的常规步骤（包括保存、清理、认领、店铺选择、发布配置，以及经核对的本次未采用记录删除）直接执行，不重复询问业务确认。
- 持续授权不扩大目标范围。永久删除前仍按采集箱 ID、平台和货源 ID 三重核对，并且只处理当前商品明确列出的未采用记录；商品、记录 ID、数量、店铺或发布配置发生变化时停止并重新核对。未有持续授权时，新的永久删除批次在动作前只取得一次业务确认，随后自动处理该批次的重复弹窗。
- 不得绕过浏览器、扩展、操作系统或宿主的强制确认、权限请求、验证码、安全拦截或二次登录。此类系统门槛无法由 Skill 代替用户完成；出现时暂停并说明需要在界面完成的具体动作。
- `publication.auto_publish_tiktok=true` 表示用户已要求把当前工作流中经过完整核验的单个商品直接发布：最终再次核对商品、店铺名称与数量、发布方式及附加服务后，自动点击“确认发布”并处理后续弹窗，不再询问用户。
- 自动发布只适用于当前工作流唯一确定的单个商品和配置中的目标店铺，不得借此批量发布其他商品。商品或店铺不唯一、配置变化、付费、验证码、权限变更、覆盖已有商品或无法唯一识别目标时停止并说明具体阻碍。

首次使用、换品类或换电脑时，完整读取 [configuration.md](references/configuration.md)。进入供货或成本核验时读取 [availability.md](references/availability.md)。用户确认插件采集完成后读取 [miaoshou.md](references/miaoshou.md)。编辑保存完成并启用自动认领/发布时，再读取 [tiktok_publish.md](references/tiktok_publish.md)。

## 初始化

确定工作区根目录和可用 Python 3。所有命令都把全局 `--root` 放在子命令前：

```bash
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" init-config
```

编辑 `<WORKSPACE>/.tiktok-shop-listing/config.json`，至少设置正确的 `catalog_roots` 和 `output_root`。供货文档必须由用户针对当前品类重新提供；不得从旧品类、旧配置、历史或记忆复用。该文档同时是低价 SKU 的供应商成本来源。收到地址后，同时把当前 `catalog_roots` 原样写入 `availability.bound_catalog_roots`。运行：

```bash
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" show-config
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" audit
```

`audit.valid=true` 表示可以选品和准备扫同款资料；只有 `audit.collection_ready=true` 才能供货核验、记录销量链接或采集。`collection_ready` 只确认文档配置和品类绑定有效，不代表已取得当前商品成本。文档未提供或未绑定时不要阻止前期选品，但进入采集阶段必须停止并向用户索取当前品类文档。

换品类或修改 `catalog_roots` 时，先把 `availability.document_url` 清空并把 `availability.bound_catalog_roots` 设为 `[]`。即使旧 URL 看起来仍可访问也不得沿用；必须等待用户手动提供新文档，再绑定到当前商品目录。

## 选择与预留

开始或继续时运行：

```bash
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" history
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" eligible
```

只从 `eligible` 结果中选择。用 `view_image` 检查候选商品原图和供应商资料；优先轮换品类、价格带、外形和用途，避免连续高度相似款。不得用目录名称猜货号，必须从供应商文件、标签或明确资料取得 `product_code`。

创建任何交付文件前运行：

```bash
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" reserve \
  "<ABSOLUTE_SOURCE_DIR>" --product-code "SUPPLIER-CODE" \
  --category "CATEGORY" --date YYYY-MM-DD
```

供应商另有明确别名时重复传入 `--alias "ALIAS"`。只使用命令返回的 `output_dir`。预留会同时锁定供应商货号和来源目录；即使用户随后删除低销量商品输出目录，也不得删除或回滚历史，不得再次选择。

仅在用户明确要求清空永久记录时运行：

```bash
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" reset --confirm CLEAR
```

## 准备扫同款资料

按配置文件中的名称生成交付物，默认包括：

```text
TK店铺标题和内容.txt
扫同款产品图.jpg
规格配置图.png（默认可选）
```

### 标题与内容

使用 UTF-8，格式固定：

```text
Title: One title on one line

Content:
Field: Value
```

遵守配置中的语言、标题长度和优先字段。只写供应商资料或图片可明确确认的品牌、型号、材质、颜色、图案、尺寸、数量、套装内容、货号和产地；没有来源的字段省略。不得猜测成分、功能、认证、兼容性、容量、承重、洗护或产地。

### 扫同款图片

逐张查看供应商原图，选择最适合视觉搜索的一张：正面优先、主体完整、颜色与款式准确、遮挡少、无聊天界面或规格表覆盖。转换成真正的 JPEG 后写入配置指定文件名，不能只改扩展名。

### 规格图

只有供应商提供可核对的尺寸或规格时才整理。把数据写入临时 JSON：

```json
{
  "product_code": "SUPPLIER-CODE",
  "unit": "cm",
  "columns": ["Specification", "Value"],
  "rows": [["Length", "20"], ["Width", "10"]]
}
```

运行：

```bash
python "<SKILL_DIR>/scripts/render_spec_chart.py" --input "<SPEC.json>" --output "<OUTPUT.png>"
```

渲染器和 `verify` 的真实图片校验需要 Pillow。原资料没有单位时写 `Not specified by supplier`；不得补值。渲染后用 `view_image` 检查图片尺寸、文字、表格边界和溢出。

完成交付物后运行：

```bash
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" verify "<OUTPUT_DIR>"
```

返回 `valid: true` 后停止，等待用户手动扫同款并判断销量。

## 收到销量链接

用户发送的一至三条链接按发送顺序代表销量从高到低，永久保持该顺序。先运行 `audit`；若 `collection_ready=false`，向用户索取当前品类供货文档并完成目录绑定。之后按 [availability.md](references/availability.md) 实时检查配置中的供货文档；核验未通过时不记录链接、不打开平台页面、不进入采集。

核验通过并执行 `confirm-availability` 后，运行：

```bash
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" record-links \
  "<OUTPUT_DIR>" "URL_1" "URL_2" "URL_3"
```

打开已记录链接：

```bash
python "<SKILL_DIR>/scripts/open_product_urls.py" "URL_1" "URL_2" "URL_3"
```

保持平台页打开，让用户逐页手动点击妙手扩展并存入公用采集箱。扩展弹窗不属于网页 DOM；不得声称已替用户点击插件。等待用户回复“采集完成”。

## 妙手编辑与完成

用户确认采集完成后，按 [miaoshou.md](references/miaoshou.md) 操作。只选择销量顺序中第一条采集成功且与供应商款式一致的记录。先记录每个保留 SKU 的未修改采集原价；任一原价严格低于 `collection.price_threshold` 时，重新打开当前品类供货文档并取得精确对应的供应商成本。币种、计价单位或 SKU 映射不清时停止。

按 `miaoshou.md` 计算最终价，把全部保留 SKU 写入价格 JSON，并在妙手改价前运行：

```bash
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" record-pricing \
  "<OUTPUT_DIR>" --currency CNY --sales-unit piece \
  --expected-sku-count 1 --sku-file "<PRICING.json>"
```

`record-pricing` 必须通过后才能填写价格、保存商品和删除本次其余记录。编辑保存并复核后删除本次其余记录，绝不删除最终采用记录。

最终完成前，如果供货证据超过配置的有效分钟数，重新检查所有工作表并再次运行 `confirm-availability`；已有价格证据会随其绑定的供货证据、链接或相关配置失效，失效后必须重新取证并运行 `record-pricing`。确认图片数量、变体、价格、库存、标题、内容、规格和清理结果全部正确后运行：

```bash
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" mark-collected \
  "<OUTPUT_DIR>" --resolved-url "FINAL_URL_1"
```

按实际链接数量重复 `--resolved-url`。`mark-collected` 强制要求有效且行数符合人工 SKU 数量声明的价格证据；妙手实际保留 SKU 总数仍需在页面人工清点。任一记录仍在处理中、最终编辑未保存、图片数量不符、变体不合供应商资料、其余记录未删完、供货或价格证据过期时，不得完成标记。

`mark-collected` 成功后，如果配置中的 `publication.auto_claim_tiktok` 和 `publication.auto_publish_tiktok` 已启用，继续读取并执行 [tiktok_publish.md](references/tiktok_publish.md)：自动认领到 TikTok，进入 TikTok 采集箱，只选择当前商品，按 `publication.store_selection` 选择店铺，然后打开发布配置。最终“确认发布”前必须再次核对商品、配置中的店铺、店铺数量、立即发布设置和付费附加项；核对全部通过后直接确认发布，不再询问。未看到成功提示和每个目标店铺的成功记录前不得报告完成。

## 隔离与安全

测试状态变更命令时始终使用临时工作区。真实工作区只执行用户当前商品所需的 `reserve`、`confirm-availability`、`record-links`、`record-pricing` 和 `mark-collected`。

不要提交或上传以下内容：

- `.tiktok-shop-listing/config.json` 与 `history.json`
- 商品图片、供应商数据包、输出目录
- Cookie、Token、密码、浏览器配置或妙手账号数据
- 含用户名的绝对路径

认领和发布只使用已登录的妙手/TikTok 页面，不把 Cookie、Token、店铺凭据或页面截图写入 Skill 仓库。

GitHub Skill 只迁移流程。另一台电脑要继续永久去重，需另行安全迁移工作区的 `.tiktok-shop-listing/history.json`；不要把历史放进公开仓库。
