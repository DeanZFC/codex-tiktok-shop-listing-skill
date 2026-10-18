---
name: swimwear-tiktok-video-workflow
description: Compatibility alias for older prompts that requested the former standalone TikTok apparel video workflow. Redirect all new and resumed work to manage-tiktok-shop-listings; this folder contains no independent workflow implementation.
---

# 已合并到主 Skill

此旧名称仅用于兼容历史提示。立即改用 `$manage-tiktok-shop-listings`，并读取已安装主 Skill 的 `SKILL.md`。

- 不在本目录创建、恢复或维护独立任务。
- 不要求安装第二套依赖。
- 已发布商品的视频必须由主 Skill 的 `scripts/run.py start-video OUTPUT_DIR` 启动。
- 旧电脑已有进行中任务应先保留原安装与任务目录；不要只复制进度 JSON 到新流程。
