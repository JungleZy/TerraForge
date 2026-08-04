# docs/superpowers/plans —— 实施计划归档（11 份，其中 1 份待执行）

> **没有一份代表当前实现。** 这里是 planning 阶段的历史产物，当前架构以仓库根 `CLAUDE.md` 和代码为准。
>
> **唯一例外是 `2026-08-04-terrain-triangulation.md`** —— 它是**尚未执行**的新计划，下面三条硬约束（尤其「复选框无效」「源码行号禁止照抄」）**不适用于它**：那份的复选框就是执行进度，代码块就是照着写的。区分办法看清单里的「结果」栏。

## 三条硬约束

1. **不代表当前实现。** 每份写的是当天的目标形态，不是今天的代码形态。多数计划落地后又被后续改造推翻过一轮甚至两轮。
2. **复选框状态一律无效。** 十份 plan 合计 500+ 个 `- [ ]`，几乎全是未勾状态——**未勾选 ≠ 未执行**，下表判「已实施」的那几份一个都没勾。唯一被勾上的 15 项集中在 `2026-05-08-agent-team-comprehensive-review.md` 文末的「计划自审清单」（检查计划文档本身写全了没有），也不是执行进度。
3. **正文内嵌的源码与行号是当日快照，禁止照抄、禁止照行号定位。** 好几份正文里嵌着完整可直接粘贴的代码块（`2026-05-06` 那份尤其），照抄会把演进数月的实现整体回退；行号今天大多指向完全不相干的代码。最危险的一例：`2026-07-28-gis-workbench-ui.md:51-53` 让把 `static/js/map.js:78,101,104` 的色号改成蓝色——那三行今天是 `updateTileEstimate()` 的反经线 wrap 判断，照改会破坏瓦片数预估。

顺带一条：每份顶部那条「REQUIRED SUB-SKILL：逐任务实施」横幅是写计划当天的指令，**对今天的读者无效**，不要把这些文件当待办工单认领。

同理，正文里指向其它文档的相对路径也是当日快照——文档目录此后重组过（如 `2026-07-27-master-plan.md` 指向的 `ui-review-2026-07.md` 现在是 `../../reviews/2026-07-27-ui-review.md`）。**这类失效链接是有意保留的历史原貌，不是待修的死链**，扫链接工具报出来时请跳过本目录。

## 清单

结果一栏取自每份文件开头的状态头（状态头是 2026-08-03 归档时加的，正文未回改）。

| 文件 | 日期 | 结果 |
|---|---|---|
| `2026-05-06-google-maps-downloader.md` | 2026-05-06 | **已实施**（day-0 首版），此后代码持续演进三个月。正文的单管线形态、根目录 `config.py`/`database.py` 均已不存在 |
| `2026-05-08-agent-team-comprehensive-review.md` | 2026-05-08 | **从未执行**。计划产出 10 份审查报告，只落地 1 份（`../../reviews/2026-05-08-comprehensive-review/04-backend-architecture.md`），66 个执行步骤一个都没跑完 |
| `2026-05-16-dem-terrain-tiling-ctb-cesium.md` | 2026-05-16 | **部分作废**。核心技术路线（外部 `ctb-tile` 子进程）当日即被 vendored 的 `services/terrain_tiling/cesiumlab_terrain.py` 取代，残留代码 2026-07-31 删净；其余产物（`dem_terrain_jobs` 表、terrain 路由与静态服务、vrt/layer_json 构建器）全部上线且仍在用 |
| `2026-06-13-local-terrain-upload-tiling.md` | 2026-06-13 | **已实施**，与现状吻合度最高的一份。两处失准：建表位置现为 `core/database.py`；前端已被 2026-07 工作台重构改成 workbench 面板结构 |
| `2026-06-15-frontend-premium-redesign.md` | 2026-06-15 | **已实施后被取代**。青绿 `#2dd4bf` 是它的产物，现已整套换成 sky `#38bdf8` + dark/light/system 三态主题。**照本文改 CSS 会把界面改坏。** 仍有价值：Task 2 的「廉价特效清单」与 `#detail*` DOM 契约只在这里有完整记录 |
| `2026-06-16-contour-map.md` | 2026-06-16 | **部分作废**。入口从「框选 bbox 自动下 DEM」改成上传 GeoTIFF；「不做晕渲」的产品定位被推翻（现默认分层设色 + 晕渲）；Leaflet 叠加代码失效；渲染层契约基本仍成立 |
| `2026-07-27-master-plan.md` | 2026-07-27 | **已实施**（两阶段 2026-07-28 全量落地，merge `44788878f`）。本文只是总览，不含可执行步骤。技术栈行的 PyInstaller 与 Leaflet 已过时 |
| `2026-07-27-phase1-data.md` | 2026-07-27 | **已实施**，内容与今天的代码基本一致。**勿当待办重跑**——重跑只会把已修好的代码改回中间态。Task 6 的 `image_only` 语义表已被 0.2.4「边下边复制」推翻 |
| `2026-07-27-phase2-visual.md` | 2026-07-27 | **已实施**（C1 与 A1-A7 全部落地）。文中「已核实的基线数字」是改造**前**的快照，`!important` 计数与行号今天全部错位。⚠️ Task 1「建立视觉基线截图」若被重跑会覆盖 `../../assets/images/phase2-baseline/` 下唯一的改造前留档 |
| `2026-07-28-gis-workbench-ui.md` | 2026-07-28 | **已执行后部分被推翻**（`38e3e30fc` 落地，核心产物 dock 两天后被 `c854e12fe` 删除）。仍有效：accent 五令牌、`.workbench` 外壳、状态栏、`.page-content`。📌 该文点名保留的 `--color-on-accent: #041e2b` 对比度实测数据（4.04:1 → 4.74:1）是全仓唯一记录，且没有测试钉住它 |
| `2026-08-04-terrain-triangulation.md` | 2026-08-04 | **⏳ 待执行**（本目录唯一一份）。8 个任务 + 收尾：RTIN 自适应三角化（新建 `src/services/terrain_tiling/rtin.py`）+ 逐顶点法线。正文的 `rtin_tables`/`rtin_errors`/`rtin_extract` 三段代码**已与设计阶段验证过的原型对拍**（索引表、误差、提取结果逐元素一致），可以照着写。**上面三条硬约束对它不适用** —— 复选框是真实进度，代码块是照着写的。两个批次（Task 1-5 三角化 / Task 6-7 法线）互不依赖，可任选其一或调换顺序；但 Task 3 的索引编码向量化**与 Task 2 绑死不可单做**（理由见该文 Global Constraints） |

## 与 `../specs/` 的配对

计划与设计稿是一一配对的两阶段产物：`specs/` 先定方案，`plans/` 再拆步骤。配对靠文件名主干，不靠目录内的显式链接（只有 `2026-06-13` 那对写了「设计依据：」指针）。

| plans/ | 配对的 specs/ |
|---|---|
| `2026-05-06-google-maps-downloader.md` | `2026-05-06-google-maps-downloader-design.md` |
| `2026-05-08-agent-team-comprehensive-review.md` | `2026-05-08-agent-team-comprehensive-review-design.md` |
| `2026-05-16-dem-terrain-tiling-ctb-cesium.md` | **无** |
| `2026-06-13-local-terrain-upload-tiling.md` | `2026-06-12-local-terrain-upload-tiling-design.md`（日期差一天） |
| `2026-06-15-frontend-premium-redesign.md` | `2026-06-15-frontend-premium-redesign-design.md` |
| `2026-06-16-contour-map.md` | `2026-06-16-contour-map-design.md` |
| `2026-07-27-master-plan.md` / `-phase1-data.md` / `-phase2-visual.md` | 无 design 稿，三份共同的依据是审查报告 `../../reviews/2026-07-27-ui-review.md` |
| `2026-07-28-gis-workbench-ui.md` | `2026-07-28-gis-workbench-ui-design.md` |
| `2026-08-04-terrain-triangulation.md` | `2026-08-04-terrain-triangulation-design.md` |

**唯一有计划没设计稿的是 `2026-05-16-dem-terrain-tiling-ctb-cesium.md`** —— 偏偏就是唯一发生技术路线改道的那条线。CTB → CesiumLab 为什么换、当天换的依据是什么，仓库里查不到任何记录。

反方向（有设计稿没计划）的清单见 `../specs/README.md`。
