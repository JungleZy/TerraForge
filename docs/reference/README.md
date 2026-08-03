# reference/ — 当前实现说明

这里的文档描述**代码现在是怎么跑的**。判定标准很硬：**内容与代码不一致就是缺陷，要么改文档要么改代码**，不能放着。想找历史上曾经怎么跑，去 [`../archive/`](../archive/)。

## 目录内容

| 文档 | 讲什么 |
|---|---|
| [`time-tracking.md`](time-tracking.md) | 任务计时口径：`total_running_seconds` 累计时长怎么持久化、暂停/恢复怎么累加、刷新页面后前端怎么复原 |
| [`partial-dom-update.md`](partial-dom-update.md) | 前端任务卡片的局部 DOM 更新策略：状态变→整卡重建，进度变→只改进度条和数字，无变化→不动 |
| [`terrain/`](terrain/) | 地形相关运维说明：CesiumJS 端怎么加载本项目产出的地形、全球低层级基础地形怎么离线构建。**含一个已知问题，动地形前先看该目录的 README** |

（`time-tracking.md` 原名 `TIME_TRACKING_SYSTEM.md`，`partial-dom-update.md` 原名 `PARTIAL_DOM_UPDATE.md`，2026-08-03 docs 重构时改名归位，内容未变。）

## 易错点：计时字段只有地图管线在写

`time-tracking.md` 描述的 `total_running_seconds` 持久化计时**只适用于地图瓦片管线**（`services/task_manager.py`）。DEM、等高线、本地地形三条管线的 manager **不写这个字段**，它们的任务行里该字段是缺失的。

前端 `calculateTimeInfo`（`static/js/tasks.js`）因此必须区分「字段缺失」和「累计为 0 秒」两种情况：缺失时回退按 `started_at` 算墙钟时长，否则这三条管线的已运行时间会恒显示 0 秒。这条回退被测试钉死了：`tests/test_tasks_js_contract.py:1159`（`test_time_info_falls_back_when_total_running_seconds_missing`）。

读 `time-tracking.md` 时把它理解成「地图管线的计时设计」，不要当成四条管线的通用机制。

## 与 archive/ 的区别

- **reference/（本目录）**：描述当前代码。要求与实现同步，发现漂移就当缺陷处理。改了相关代码就要回头看这里要不要跟着改。
- **[`../archive/`](../archive/)**：历史文档归档。**不要求与当前代码一致**，也不回改正文（包括其中已失效的路径引用），保留撰写当时的原貌。看到里面的描述和代码对不上，那是正常的，不是 bug。
