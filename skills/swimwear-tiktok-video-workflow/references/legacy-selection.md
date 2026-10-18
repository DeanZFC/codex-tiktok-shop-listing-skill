# 旧链接历史模式

仅用户明确提供完整旧目录与历史时使用。单个产品目录始终使用 run.py start，不依赖此模式。

通过 workflow.py 的全局参数显式传 `--catalog CATALOG --link-history HISTORY --done-root DONE_ROOT select`，参数必须放在子命令之前。历史需为 version=1、products 列表；只有 status=miaoshou_collected、output_dir 指向当前电脑绝对 `CATALOG/YYYY-MM-DD/NNN-货号` 且含 `TK店铺标题和内容.txt`、`扫同款产品图.jpg`、`尺寸配置图.png`、`供应商短链接.txt` 的记录可选。

旧 select 默认随机选择；用户要求按顺序时由 Codex 按日期/目录名排序，明确一个单品路径后使用 run.py start，避免随机选品。归档去重按标准化货号跨日期检查；不要因上一个任务未完成阻塞新的独立产品。

旧入口的后续命令继续显式带原 catalog、done-root、task-dir。run.py task 是针对新指定目录入口的便捷包装；不要把历史源目录布局当作新入口假定。

全局 state 的一次性迁移：显式带 --state-file LEGACY_STATE_FILE 运行 migrate-state；只有目标写入并校验成功后才能使用 --remove-source 删除旧 state。跨电脑状态重定位不在这个命令范围内。
