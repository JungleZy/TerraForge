# notes/ — 调研笔记与未实施的计划

**这里的东西都还没做。** 本目录放的是想法、外部项目调研、以及写完了但没动工的设计稿。读这里的文档时默认：**文中描述的能力在代码里不存在**，不要照着调 API、不要当成已有功能写进别处的文档。

## 目录内容

| 文档 | 是什么 | 状态 |
|---|---|---|
| [`cache-exclusive-cleanup-plan.md`](cache-exclusive-cleanup-plan.md) | 共享缓存的「独占集清理 + 孤儿清扫」方案：删除任务时只清理**仅该任务独占**的瓦片/DEM 缓存，另提供孤儿缓存手动清扫。核心结论是本项目的任务表本身就是 manifest（缓存集合是 bbox+zoom+style 的纯函数），不需要另建清单表 | **尚未实施**，有效 backlog |
| [`geolibre-takeaways.md`](geolibre-takeaways.md) | 2026-08-03 对外部项目 [GeoLibre](https://github.com/opengeos/GeoLibre)（上游 commit `483c663`）的调研笔记，逐条给出「可借鉴 / 不采纳」评估：共享缓存清理、MBTiles 导出、覆盖率 ratchet、跨反经线 bbox 拆分等 | 评估结论，**不含任何必须兑现的承诺** |
| [`geo-downloader-takeaways.md`](geo-downloader-takeaways.md) | 2026-08-11 对同类项目 [GeoDownloader](https://github.com/gaopengbin/geo-downloader)（上游 commit `58cfa570`）的源码、Release 与 Issue 调研；对比产品定位、任务/缓存可靠性、区域输入、输出格式和桌面分发，并给出分层融合路线 | 评估结论，**尚未实施，不代表排期** |

### cache-exclusive-cleanup-plan.md：注意不要当成 API 文档

该文件开头自带「设计稿 · 尚未实施」状态头。它提到的全部新增能力在仓库里**零命中**——`_rect_subtract`、`exclusive_tile_rects`、`exclusive_dem_granules`、`clear_task_exclusive_cache`、`sweep_orphan_cache`、`?clear_cache=1`、`POST /api/cache/sweep_orphans`、`GET /api/tasks/<id>/cache_footprint` 一个都不存在。

现行的缓存能力只有两个：`GET /api/cache/stats` 和 `POST /api/cache/clear`（`src/services/task_cleanup.py`），且 0.2.4 起缓存**没有任何自动淘汰**。

文中对**现状**的代码引用（`file:line`）是撰写当日核实过的，可信；凡描述新增接口的段落一律是拟新增。

## 与 superpowers/ 的区别

- **notes/（本目录）**：**尚未动工**的想法与调研。没有人承诺要做，也没有排期。
- **[`../superpowers/`](../superpowers/)**：**已执行过**（或曾计划执行）的计划与设计稿归档，是历史记录。同 `archive/` 的规矩：正文不回改，包括其中已失效的路径引用。

区别落到实际操作上：notes/ 里的东西真要做了，就从这里取需求；superpowers/ 里的东西是「当时怎么想的、怎么做的」，翻它是为了考古，不是为了排期。
