# 实时供货核验

## 强制顺序

收到当前商品的销量链接后、记录链接和打开平台页面前执行。每次使用配置中的实时文档，不使用上一次结果、截图、本地商品目录或记忆代替。

先运行 `audit`。只有 `collection_ready=true` 才继续；若为 `false`，停止并向用户索取当前品类供货文档。不得自行复用旧品类 URL。用户提供后，把 URL 写入 `availability.document_url`，并把当前 `catalog_roots` 同顺序写入 `availability.bound_catalog_roots`，再次运行 `audit`。

1. 使用 `browser:control-in-app-browser` 打开 `availability.document_url`。
2. 关闭不影响查看的登录提示；需要登录、验证码或权限时交给用户。
3. 按 `Ctrl+F` 打开文档查找，输入历史条目的 `product_code`；仅在供应商明确提供时使用预留时记录的别名。
4. 把范围切换为“所有工作表”，执行“查找全部”，只采信精确结果。
5. 检查全部精确结果的工作表、单元格和值，不只看当前可见区域。
6. 只要任一结果出现在配置的 `sold_out_sheets`，立即判定不可采集；售罄结果优先于旧在售表结果。
7. 没有售罄命中时，至少需要一个正常供货工作表的精确命中。记录真实工作表名、A1 单元格和命中文字。
8. 如果文档显示库存数量，至少一个可供 SKU 必须明确大于零；全部为零、空白或无法读清时停止。

查无结果、只有模糊结果、相邻货号、只命中售罄表、文档打不开、无法确认所有工作表范围或匹配关系不明确时，一律停止。不要记录链接或打开平台页。已预留商品仍永久保留在历史中，避免以后重复选择。

## 记录证据

核验通过后立即运行；以下内容必须替换为真实结果：

```bash
python "<SKILL_DIR>/scripts/listing_workflow.py" --root "<WORKSPACE>" confirm-availability \
  "<OUTPUT_DIR>" --query "SUPPLIER-CODE" --matched-text "SUPPLIER-CODE" \
  --active-sheet "ACTIVE-SHEET" --active-cell '$A$2' \
  --scope all_sheets --sold-out-status clear
```

PowerShell 和 Bash 都使用单引号包住含 `$` 的单元格地址。命令只记录浏览器核验结果；没有完成上述浏览器步骤时不得调用。

证据绑定当前商品、配置中的文档和售罄表、当前链接及核验时间。下列情况要求重新打开文档核验并再次运行命令：

- 超过 `fresh_minutes`。
- 替换已记录的链接。
- 修改供货文档、售罄工作表或有效时间。
- 妙手编辑耗时导致完成标记前证据过期。
