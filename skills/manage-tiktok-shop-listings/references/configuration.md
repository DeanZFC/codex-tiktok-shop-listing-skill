# 本机配置

## 首次设置

运行 `init-config` 后编辑：

```text
<WORKSPACE>/.tiktok-shop-listing/config.json
```

路径相对工作区解析；`catalog_roots` 也允许本机绝对路径。`output_root` 与 `state_root` 必须位于工作区内。可设置环境变量 `TIKTOK_LISTING_WORKSPACE` 作为默认工作区，也可每次传入 `--root`。

至少完成以下设置：

1. `catalog_roots`：一个或多个商品资料目录。
2. `output_root`：按日期和 `001-货号` 保存结果的目录。
3. `availability.document_url`：由用户手动提供的当前品类实时供货与成本文档 URL。
4. `availability.bound_catalog_roots`：确认该文档对应的商品库；填写与当前 `catalog_roots` 相同的列表和顺序。
5. `availability.sold_out_sheets`：所有售罄、暂停供货或下架工作表名称。
6. `deliverables`：交付文件名及规格图是否必需。

首次使用或切换品类时，不得沿用以前品类的文档。先把 `document_url` 清空、`bound_catalog_roots` 设为 `[]`，再向用户索取当前品类文档。用户提供后才填写 URL 并绑定当前商品库。

## 配置字段

| 字段 | 含义 |
|---|---|
| `catalog_roots` | 商品资料根目录列表 |
| `output_root` | 上架资料输出目录 |
| `state_root` | 本机配置、历史和锁文件目录 |
| `product_discovery.image_extensions` | 识别商品候选的图片格式 |
| `product_discovery.ignore_directories` | 扫描商品库时跳过的目录名 |
| `product_discovery.min_product_code_chars` | 供应商货号和别名最短长度 |
| `availability.required` | 是否强制实时供货核验；默认 `true` |
| `availability.document_url` | 当前品类供货、库存及低价 SKU 成本取证文档地址 |
| `availability.bound_catalog_roots` | 文档适用的商品库列表，必须与当前 `catalog_roots` 按顺序对应 |
| `availability.scope` | 固定为 `all_sheets` |
| `availability.sold_out_sheets` | 具有否决优先级的工作表列表 |
| `availability.fresh_minutes` | 供货证据有效时间 |
| `copy.language` | 店铺标题与内容语言 |
| `copy.title_max_chars` | 标题最大字符数 |
| `copy.preferred_fields` | 优先整理的事实字段 |
| `collection.min_links/max_links` | 接受的销量链接数量范围 |
| `collection.final_image_count` | 最终商品图片准确数量 |
| `collection.price_threshold` | 采集原价严格低于此值时启用成本定价，固定为 `50.0` |
| `collection.minimum_profit_rate` | 低价 SKU 相对供应商成本的最低加价率，默认且不得低于 `0.05` |
| `collection.sku_inventory` | 所有 SKU 的目标库存，默认 `19` |
| `collection.delete_nonselected` | 完成后是否删除本次未采用记录 |
| `publication.auto_claim_tiktok` | 保存并清理完成后是否自动认领到 TikTok 采集箱 |
| `publication.auto_publish_tiktok` | 认领后是否自动打开 TikTok 发布流程；最终确认发布仍需动作时确认 |
| `publication.store_selection` | 店铺选择策略；`all` 表示选择当前账号全部可见店铺，`named` 表示只选择 `store_names` 中列出的店铺 |
| `publication.store_names` | `store_selection=named` 时的精确店铺名称列表；名称必须与页面显示完全一致 |
| `publication.expected_store_count` | 自动流程允许的店铺数量；必须与实际选择数量一致 |
| `publication.publish_mode` | 发布方式；本工作区使用 `immediate`（立即发布） |
| `deliverables.spec_chart_required` | 缺少规格图时 `verify` 是否失败 |

`collection.minimum_profit_rate=0.05` 表示在供应商成本上至少加价 `5%`，不是销售毛利率或扣除平台费、税费、运费后的净利润率。低价 SKU 的最低允许价为 `C * (1 + minimum_profit_rate)` 向上取整到分，最终价取采集原价与最低允许价中的较高者；采集原价等于或高于阈值时必须保持原价。脚本不允许把阈值改离 `50.0`，也不允许把最低加价率调低到 `5%` 以下。

`availability.required=false` 只能在用户明确表示不需要供货文档时设置。它会关闭采集前供货门禁；不要为了绕过文档打不开、未提供新文档或查无结果而临时关闭。该设置也不能豁免低价 SKU 的成本证据：没有当前品类文档或无法确认成本时仍须停止。

修改文档地址、绑定商品库、售罄工作表或有效时间后，旧供货证据会自动失效，必须重新核验。修改已记录链接、`price_threshold`、`minimum_profit_rate` 或其他定价相关配置后，旧价格证据也会失效，必须重新取得成本并运行 `record-pricing`。`audit.valid` 只表示商品库、历史和配置结构可用于前期选品；`audit.collection_ready` 表示当前文档已配置并绑定，可进入供货核验和采集，不表示当前商品的成本已经取得或可读。

### TikTok 发布偏好

自动发布必须严格遵守 `publication.store_selection`。`all` 模式要求读取当前账号可见店铺总数并全部勾选；`named` 模式只勾选 `publication.store_names` 中的精确名称，并核对数量等于 `publication.expected_store_count`。不要把商品列表的“全选”误当成店铺全选；商品列表只保留当前目标商品。店铺数量、名称或可见性发生变化时先暂停，重新确认后再修改配置。

## Python 与依赖

选择、预留、供货核验、链接记录和打开浏览器只使用 Python 标准库。`verify`、最终完成门禁和 `render_spec_chart.py` 需要 Pillow 对 JPEG/PNG 做真实解码校验：

```bash
python -m pip install Pillow
```

安装依赖前遵守当前环境的审批规则。没有 Pillow 时仍可使用前期流程，但不得跳过图片验证后标记完成。

## 换电脑

安装 Skill 后在新电脑重新运行 `init-config` 并填写该电脑路径。GitHub 仓库不会携带业务状态。

如需延续去重记录，单独复制：

```text
<OLD_WORKSPACE>/.tiktok-shop-listing/history.json
```

复制到新工作区相同相对位置。历史中的商品来源使用“商品库序号 + 相对路径”，因此新电脑的 `catalog_roots` 顺序必须与旧电脑一致。运行 `audit` 检查缺失来源，不要用 `reset` 或重新扫描输出目录替代历史迁移。
