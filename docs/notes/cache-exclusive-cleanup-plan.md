# 共享缓存安全清理方案（独占集清理 + 孤儿清扫）

> **设计稿 · 尚未实施**
> **撰写时间**：2026-08-03（基线 0.2.4）｜ **状态**：未实施，属有效 backlog
> 本文提出的全部新增能力在代码里**均不存在** —— `_rect_subtract`、`exclusive_tile_rects`、
> `exclusive_dem_granules`、`clear_task_exclusive_cache`、`sweep_orphan_cache`、
> `?clear_cache=1`、`POST /api/cache/sweep_orphans`、`GET /api/tasks/<id>/cache_footprint`
> 全仓零命中。文中对**现状**的代码引用（`file:line`）是撰写当日核实过的，可信；
> 但凡描述新增接口的段落一律是**拟新增**，不要当成已有 API 调用。
> 现行的缓存能力只有：`GET /api/cache/stats`、`POST /api/cache/clear`
> （`src/services/task_cleanup.py:clear_cache_category`），且 0.2.4 起缓存无自动淘汰。

---

来源：`docs/geolibre-takeaways.md` 第 1 条（借鉴 GeoLibre `offline-regions.ts`）的落地设计。2026-08-03 对照本仓库代码核实。

## 要解决的问题

- 瓦片 cache（`cache/<style>/<z>/<x>/<y>.png`）和 DEM cache（`cache/dem/<granule>`）跨任务共享、只增不减。
- 删除任务时 `delete_files` 只删任务产物目录，`remove_task_dir_if_safe` 明确拒绝碰 cache（`src/services/task_cleanup.py:118-121`）。
- 现有清理手段只有配置页按分类整清（`POST /api/cache/clear`），粒度太粗：清一个分类会把其他仍在用的任务缓存一起清掉。

目标：删除任务时可选清理**仅该任务独占**的缓存；另提供**孤儿缓存**（不被任何现存任务覆盖）手动清扫。保持既定约定不变：cache 不做任何自动清理，所有清理都是用户显式触发。

## 核心思路：任务表就是 manifest

GeoLibre 需要另建 region manifest，是因为它的 SW cache 不记元数据。本项目不需要：

- 瓦片集合是 `bbox + zoom区间 + style` 的纯函数，唯一枚举入口是 `DownloadEngine._tile_ranges`（`src/services/download_engine.py:199`，`count_tiles`/`iter_tiles`/`calculate_tiles` 共用，口径一致性已有约定）。
- DEM 颗粒集合是 `dataset + bbox` 的纯函数（`src/services/dem_granules.py`：`tiles_for_bbox` + 各 dataset 的 granule 命名）。
- `tasks` / `dem_tasks` / `contour_tasks` 行持久保存了全部枚举入参，任务创建后参数不可变（无更新四至的接口）。

所以「哪些缓存被谁引用」任何时候都能从现存任务行重建，**不新建任何表**（当年 `task_tiles` 稀疏化掉全量行的理由——DB 膨胀、写放大——依然成立）。

**独占集 = 本任务枚举集 − 所有其他现存任务枚举集的并集**，按 zoom 逐层做瓦片矩形减法，全程不物化他人集合。

## 安全性论证（这种功能的核心，实现前先过一遍）

| 事实 | 出处 | 结论 |
|---|---|---|
| 任务参数创建后不变 | 无 update 接口 | 清理期间保留任务的覆盖集不变；并发下载中的保留任务新落的瓦片必在其参数矩形内，绝不在独占集内 → 并发安全 |
| cache 写入是 .part 原子替换 | `download_engine.py:671-676` | 文件要么完整要么不存在，unlink 不会碰到半成品 |
| 地图任务产物是从 cache **复制**的 | `task_manager.py:131` `_stream_copy_tile` | 删 cache 不动任务产物与预览 |
| DEM 颗粒 cache↔任务目录是**硬链接**（退化复制） | `dem_download_engine.py:77-89` | unlink cache 只减链接数，任务目录数据不丢，只损失去重收益 |
| 枚举失败（历史脏行，见 `Task.from_row` 注释） | `src/models/task.py:193-199` | 保守降级：该行同 style/dataset 本轮整体放弃清理，宁可保留 |

误删的最坏结果统一是「cache  miss 后重下」，不产生数据丢失。删除顺序：行删除**前**取枚举快照，行删除**后**执行文件清理（与现有 `remove_task_dir_if_safe` 同序）；文件清理中途失败只意味着部分缓存保留。

## 数据口径（各 cache 类别的引用来源）

| cache 位置 | 引用来源（现存行即视为引用） |
|---|---|
| `cache/<style>/<z>/<x>/<y>.png` | `tasks` 行：`north/south/east/west, zoom_min/zoom_max, style` |
| `cache/dem/ASTGTMV003_*` | `dem_tasks`（`dataset='ASTGTM.003'`）+ 遗留下载型 `contour_tasks`（`dataset='ASTGTM.003'`） |
| `cache/dem/Copernicus_*` | `dem_tasks` / 遗留 `contour_tasks`（`dataset='COP-DEM-GLO-30'`） |
| `cache/dem/ASTWBDV001_*` | 遗留下载型 `contour_tasks` 且 `water=1` |

注意两点：

- ASTER 的 `_num.tif`：是否下载取决于创建参数且未持久化。枚举时**一律按包含 `_num` 取超集**——只会少删不会误删。
- 暂停/失败/取消的任务仍视为引用（其参数矩形仍覆盖 cache）。取消是终态、理论上可不pin，但保守起见统一算入；多保留的只是磁盘，用户仍可用现有分类整清兜底。写进文档避免误解「删了任务 cache 没变小」。

## 功能分解

### 1. 独占集计算（纯函数，先行可测）

落在 `src/services/task_cleanup.py`（清理逻辑已集中于此）：

- `_rect_subtract(rect, others) -> list[rect]`：标准矩形减法，每个相交他矩形把剩余矩形最多劈成 4 块。
- `exclusive_tile_rects(task_row, other_rows) -> Iterator[(zoom, x_min, x_max, y_min, y_max)]`：逐 zoom 调用 `_tile_ranges` 取本任务矩形，扣除同 style、zoom 区间覆盖该层、矩形相交的他任务矩形。
- `exclusive_dem_granules(dataset, bbox, other_rows) -> set[str]`：granule 集合差（颗粒数 ≤ 数万，直接物化集合）。

### 2. 独占清理执行

- `clear_task_exclusive_cache(task_row, other_rows) -> {removed_bytes, removed_files}`：枚举独占矩形 → `Tile.cache_path(style)` 定位 → 存在即 stat + unlink → 顺手 prune 空掉的 `x/`、`z/` 目录（`os.removedirs` 式向上尝试，撞到非空即停）。
- DEM 侧同理：`cache/dem/<flat basename>` unlink。
- 大任务是数十万文件的 stat+unlink，本地盘秒级到几十秒；同步执行、返回统计，与 `clear_cache_category` 同形态。显式动作，可接受。

### 3. API（与现有参数正交，默认全部关闭）

- `DELETE /api/tasks/<id>?clear_cache=1`（`src/routes/api.py:339`，在 `_state_lock` 内取快照，行删后执行清理）
- `DELETE /api/dem/tasks/<id>?clear_cache=1`（`src/routes/dem_api.py` 同模式）
- `POST /api/cache/sweep_orphans`：孤儿清扫（见下），配置页按钮 + 二次确认，与现有分类整清并列
- （二期可选）`GET /api/tasks/<id>/cache_footprint`：按需统计该任务缓存占用（总量/独占量），history 页按行触发；不在列表页默认算，避免每行一次枚举

### 4. 孤儿清扫 `sweep_orphan_cache()`

对应「历史上没清缓存就删掉的任务」留下的存量：

- `cache/<style>/...`：全部现存 `tasks` 行的覆盖集之外的文件 → 删除；
- `cache/dem/...`：全部现存 `dem_tasks` + 遗留 `contour_tasks` 枚举之外的颗粒 → 删除；
- 任一类别出现无法枚举的行 → 该类别本轮整体跳过（保守）；
- 只删已知布局内的文件：`*.png` 瓦片与 granule 白名单命名，`cache` 顶层散落文件不碰（那是 `_root` 分类的职责）。

### 5. 前端

- 任务删除确认对话框加复选框「同时清理仅该任务独占的下载缓存」（默认不勾；文案注明：与其他任务重叠的部分会保留）；
- 配置页缓存管理区加「清理孤儿缓存」按钮（二次确认，展示返回的释放量）。

## 不做的事

- 不重建全量 `task_tiles` 行、不加引用计数表（稀疏化的理由仍成立；纯函数重建已够用）；
- 不做任何自动/定期清理（`task_cleanup.py` 模块 docstring 的既定约定不变）；
- 不做 LRU / 容量上限（与本方案正交，真需要时另立题）。

## 实施步骤与测试

按序，每步独立可验：

1. `_rect_subtract` + 单测：相离 / 包含 / 被包含 / 部分重叠 / 边相邻（不重叠不扣）。
2. `exclusive_tile_rects` + 单测：两任务部分重叠→交集保留；不同 style 互不影响；zoom 区间不相交互不影响；脏行降级（同 style 整组保留）。
3. `clear_task_exclusive_cache` 文件层 + 测试：临时 cache 造文件，验证只删独占、交集幸存、空目录被 prune、缺失文件跳过。
4. DEM granule 差集 + 测试：含 `_num` 超集规则；cache 颗粒被硬链接进任务目录时 unlink 后任务侧仍可读。
5. API 接入 + 测试：`clear_cache` 默认 false 行为不变；与 `delete_files` 组合；运行中任务仍 400。
6. `sweep_orphan_cache` + 测试：只清不被覆盖的；布局外文件不碰；枚举失败类别整体跳过。
7. 前端复选框 + 配置页按钮。
8. 文档收尾：`CLAUDE.md`「Task lifecycle & deletion conventions」节补 `clear_cache` 约定；`task_cleanup.py` 模块 docstring 更新（「不做自动清理」的表述扩展为三类入口）；`docs/geolibre-takeaways.md` 第 1 条标记已落地。

## 风险与缓解

- **枚举口径漂移**：清理枚举必须与下载枚举永远同口径 → 硬要求复用 `_tile_ranges` / `dem_granules`，禁止另写经纬度转瓦片实现；测试里用同一 bbox 对 `iter_tiles` 与独占集做交叉验证。
- **大任务清理耗时**：同步数十秒的可能 → API 文档注明；真成为问题再改后台线程 + socketio 进度（不预先做）。
- **遗留 contour 行口径**：遗留下载型行只是「others」来源之一，枚举失败只触发保守降级，不阻塞主流程。
