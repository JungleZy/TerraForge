# docs/superpowers —— 历史实施计划与设计稿

> **本目录不代表当前实现。** 这里是 brainstorming / planning 阶段的历史产物：`plans/` 是实施计划，`specs/` 是配对的设计稿。当前架构以 `CLAUDE.md` 和代码为准。

## 读之前必须知道的三条

1. **复选框状态一律无效。** 十份 plan 合计 521 个 `- [ ]`，只有 15 个被勾选——而那 15 个全部集中在 `plans/2026-05-08-agent-team-comprehensive-review.md` 的「自审检查清单」一节（检查计划文档本身写全了没有），**不是执行进度**。反过来说，未勾选 ≠ 未执行：下表里判「已实施」的几份，复选框一个都没勾。
2. **正文内嵌的源码与行号是当日快照，禁止照抄或照行号定位。** 多份 plan 含可直接粘贴的完整代码块（`2026-05-06` 那份尤其），照抄会把已演进数月的代码回退；文中的行号今天大多指向完全不相干的代码。
3. **不要按这些计划重新执行。** 顶部的「REQUIRED SUB-SKILL: 逐任务实施」横幅是写计划当天的指令，今天照做等于重跑一遍已完成（或已被推翻）的改造。

## 计划 ↔ 设计稿 ↔ 实施结果

| 计划（plans/） | 配对设计稿（specs/） | 结果 |
|---|---|---|
| `2026-05-06-google-maps-downloader.md` | `2026-05-06-google-maps-downloader-design.md` | 已实施，其后被大幅重构（文件结构、依赖版本、配置键均已变） |
| `2026-05-08-agent-team-comprehensive-review.md` | `2026-05-08-agent-team-comprehensive-review-design.md` | **从未执行**——计划产出 10 份报告，只落地 `docs/reviews/2026-05-08-comprehensive-review/04-backend-architecture.md` 一份 |
| `2026-05-16-dem-terrain-tiling-ctb-cesium.md` | **无**（见下） | **部分作废**：切片引擎从外部 CTB（`ctb-tile`）改道 vendored CesiumLab（`services/terrain_tiling/cesiumlab_terrain.py`），残留代码已删；计划其余产物（`dem_terrain_jobs` 表、terrain 路由与静态服务）全部上线 |
| `2026-06-13-local-terrain-upload-tiling.md` | `2026-06-12-local-terrain-upload-tiling-design.md` | 已实施，与现状吻合度最高的一份（唯一显式写了「设计依据：」指针的一对） |
| `2026-06-15-frontend-premium-redesign.md` | `2026-06-15-frontend-premium-redesign-design.md` | 已实施后**被取代**：青绿 `#2dd4bf` 已整套换成 sky `#38bdf8` + dark/light/system 三态主题。照它改 CSS 会把界面改坏 |
| `2026-06-16-contour-map.md` | `2026-06-16-contour-map-design.md` | **部分作废**：入口从「框选 bbox 自动下 DEM」改为上传 GeoTIFF；「不做晕渲」的产品定位被推翻（现默认分层设色 + 晕渲）；渲染层契约基本仍成立 |
| `2026-07-27-master-plan.md` | 无（依据 `docs/ui-review-2026-07.md`） | 已实施，2026-07-28 全量落地（merge `44788878f`） |
| `2026-07-27-phase1-data.md` | 无（master-plan 的子计划） | 已实施，同上 |
| `2026-07-27-phase2-visual.md` | 无（master-plan 的子计划） | 已实施，同上。⚠️ 文中「已核实的基线数字」是改造**前**的快照，`!important` 计数等数字今天全部错位 |
| `2026-07-28-gis-workbench-ui.md` | `2026-07-28-gis-workbench-ui-design.md` | **部分作废**：顶部工具栏 + 380px 右侧 dock 只活了两天就被 `specs/2026-07-30-workbench-ux-redesign-design.md` 推翻；accent 令牌、`.workbench` 外壳、状态栏、`.page-content` 今天仍在跑 |

## 不成对的

**有计划没设计稿（1 份）**

- `plans/2026-05-16-dem-terrain-tiling-ctb-cesium.md` —— 恰恰是唯一改道的那条线缺少设计记录。CTB → CesiumLab 的改道原因在仓库里查不到。

**有设计稿没计划（5 份）**

- `specs/2026-06-16-copernicus-glo30-design.md` —— 把 GLO-30 设为默认高程源，已实施。
- `specs/2026-06-16-terrain-color-design.md` —— 与上面同一天，且它的高程源前提当天就被上面那份推翻（ASTGTM.003 → COP-DEM-GLO-30）。同日两份说法相反，只看其一会判断错。
- `specs/2026-07-30-workbench-ux-redesign-design.md` —— 当前工作台形态（splash / 状态栏 / 选区可调 / 记录中心）的设计稿，已实现（`c854e12fe`）。
- `specs/2026-08-01-concurrency-recommend-design.md` —— 并发数「测速推荐」，已实现，与代码逐条吻合。
- `specs/2026-08-02-absolute-save-path-design.md` —— 保存路径绝对化，已实现（0.2.3）；其中「边界不变」一节已被 0.2.4 的全盘放开推翻。

## 每份文件开头还有自己的状态头

上表只给结论。哪一节仍有效、哪一节已死、当前事实源在哪个文件，写在每份文件开头的状态头里——动手前先读那一段。
