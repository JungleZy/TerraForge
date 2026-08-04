# docs/superpowers/specs —— 设计稿归档（13 份）

> **没有一份代表当前实现。** 这里是动工前的方案记录，写的是「打算怎么做」，不是「今天怎么跑」。当前架构以仓库根 `CLAUDE.md` 和代码为准。

## 读之前必须知道

- **「状态：已实现」只说明写下那天已实现**，不保证今天还成立。多份设计稿落地后又被后续改造推翻了一部分。
- **正文一律保持原样未回改。** 「今天还成不成立」的信息只写在每份文件开头的归档状态头里；正文里的路径、行号、令牌值是当日快照。
- **最危险的不是整篇过时，是局部失效**——文件头写着「已实现」，正文里却藏着一节早被推翻的约束。整篇过时的文档会自曝（照做立刻失败），局部失效的会骗人（读起来处处合理，只有那一节是反的）。下面两份点名了。

## 清单

| 文件 | 日期 | 状态 |
|---|---|---|
| `2026-05-06-google-maps-downloader-design.md` | 2026-05-06 | **部分作废**。仍成立：Flask + SocketIO + aiohttp + GDAL + SQLite 骨架、`tasks`/`task_tiles`/`config` 三表、mts0-3 瓦片源、`cache/{style}/{z}/{x}/{y}.png` 共享缓存布局。已作废：Leaflet（→ CesiumJS）、Python 3.9（→ 3.12）、缓存自动淘汰（0.2.4 删除）、根目录扁平结构（→ `core/`） |
| `2026-05-08-agent-team-comprehensive-review-design.md` | 2026-05-08 | **从未执行**。设计的 10 份产物只落地 1 份，被审的项目形态也已整体不存在 |
| `2026-06-12-local-terrain-upload-tiling-design.md` | 2026-06-12 | **已实施，与当前实现一致**。2026-08-03 复核过：两张表、manager、`/api/terrain/local/...`、`local_task_<id>/{source,terrain_tiles}` 布局、静态服务全部对得上。唯一路径变化：`database.py` → `core/database.py` |
| `2026-06-15-frontend-premium-redesign-design.md` | 2026-06-15 | **已实施，但设计令牌部分已被取代**。⚠️ 不要把第 3 节当设计系统基准——青绿 `#2dd4bf` 一族已整套换成 sky 蓝，单一深色主题已变三态。仍成立：§6 的 `GET /api/history_stats`、§7 的 DOM 契约（`#activeTasks` 除外） |
| `2026-06-16-contour-map-design.md` | 2026-06-16 | **部分作废**。开头那句「框选区域后自动下载 DEM」已不成立（现为上传驱动）；渲染也不走 `gdal_contour`（改 matplotlib `ax.contour`）；「不做晕渲」被推翻。仍成立：两张表、独立 manager、落盘布局、`/contour/<id>/...` 的穿越防护、曲线分级与配色可配置 |
| `2026-06-16-copernicus-glo30-design.md` | 2026-06-16 | **已实施且仍成立**（本文没有归档状态头——归档时判定内容与现状无冲突）。COP-DEM-GLO-30 今天仍是 DEM 与等高线管线的默认数据集，ASTER 保留为可选 |
| `2026-06-16-terrain-color-design.md` | 2026-06-16 | **已实施，渲染部分成立、高程源前提作废**。三层渲染（分层设色 + 晕渲 → 水体 → 等高线）今天仍是 `services/contour_engine.py` 的结构；正文写的高程源 ASTGTM.003 其后改为 COP-DEM-GLO-30，granule 后缀随之由 `_dem.tif` 变 `_DEM.tif` |
| `2026-07-28-gis-workbench-ui-design.md` | 2026-07-28 | **部分作废（局部失效，见下）** |
| `2026-07-30-workbench-ux-redesign-design.md` | 2026-07-30 | **已实现**，0.2.x 沿用至今（splash 动画、底部状态栏、框选角点可调、dock 并入记录中心）。三点出入：按钮文案实际是「任务」不是「记录」；记录面板的三分区结构已被 2026-08-01 单一时间流重构取代；文中要求断言的 `#recordsPanel` 并不存在（实现沿用 `#historyPanel`） |
| `2026-08-01-concurrency-recommend-design.md` | 2026-08-01 | **已实现**，随当轮改动落地 |
| `2026-08-02-absolute-save-path-design.md` | 2026-08-02 | **已实现，但一节被 0.2.4 整体推翻（局部失效，见下）** |
| `2026-08-04-src-layout-migration-design.md` | 2026-08-04 | **待实施**。写下时代码仍是根目录平铺 `core/`/`models/`/`routes/`/`services/`；正文里的 493/302 处计数与 `config.py:58` 等行号是当日快照 |
| `2026-08-04-terrain-triangulation-design.md` | 2026-08-04 | **主体待实施；「插曲」一节的两个 bug 已修复落地**。给地形切片加「自适应三角化（自写 Martini/RTIN）」与「逐顶点法线」，两者默认开、UI 无开关、K 固定 0.15；唯一暴露给用户的是 Cesium `enableLighting`（渲染端开关，默认关）。正文大半是**实测数据与选型排除依据**（3518 个真实瓦片样本，山地 + 平缓两套 DEM）——QEM/PyMeshLab、fast-simplification、自研 TVD + Numba 三条路都实测后排除，附录 B 记了 8 个产生过假数字的坑。**两条必读**：①「插曲」一节记录了 `triangleCount` 字段写错导致**地形从未真正工作过**，以及测试为何镜像了这个 bug；② 实测同屏峰值仅 27.8 万三角形，**几何不是 GPU 瓶颈**，减面的价值在存储而非帧率 |

## ⚠️ 两份「局部失效」的，点名

**`2026-08-02-absolute-save-path-design.md`** —— 头部写「状态：已实现」，路径绝对化确实上线了，但 **`:18` 的「边界不变」整节已被 0.2.4 推翻**：

- 正文（`:20-21`）：「任务产物必须落在 `Config.DOWNLOADS_DIR` 之内」——现在保存目录**可选全盘任意位置**；
- 正文（`:36`）：「`GET /api/fs/browse` 只列根目录内非隐藏子目录」——现在**能列全盘**。

这条最容易骗人：读者据此去写校验、写测试，甚至把「能保存到任意目录」当 bug 报。删除侧的实际护栏见 `CLAUDE.md` 的 "Task lifecycle & deletion conventions"（`remove_task_dir_if_safe`：拒符号链接、拒过浅路径、拒家目录、拒 `DOWNLOADS_DIR`/`CACHE_DIR` 自身及其祖先）。

**`2026-07-28-gis-workbench-ui-design.md`** —— 失效的**只有布局部分**，被 `2026-07-30-workbench-ux-redesign-design.md` 取代：顶部工具栏 `.workbench-toolbar` 与 380px 右侧 dock 都已删除，全仓 grep 不到，`templates/base.html:53-55` 有注释专门反驳它；Leaflet 相关的一切也随引擎更换作废；「不做浅色主题」被三态主题反转。

**但两块内容今天仍是现行依据，别因为上面几条就整份丢弃**：

- `:68` 起的**配色与视觉令牌表** —— 五个 accent 令牌与 `static/css/style.css:31-35` 逐值一致；其中 `--color-on-accent: #041e2b` 的取值理由（初稿 `#082f49` 实测 active 态对比度仅 4.04:1，加深后最差 4.74:1）是全仓**唯一**记录该实测数字的地方，且没有任何测试钉住它——改这个令牌前必须先读这条；
- `:114` 起的**覆盖面板架构** —— `.workbench-panel` 480px / `--wide` 920px、z-index 阶梯、`static/js/panels.js` 的懒初始化，今天全部在跑。

另外这份文件被 `../plans/2026-07-28-gis-workbench-ui.md` 按路径引用，**请勿移动或改名**。

## 没有配对 plan 的设计稿（6 份）

`plans/` 与 `specs/` 本该成对（方案 → 步骤），以下 6 份只有设计稿：

| 文件 | 为什么没有 plan |
|---|---|
| `2026-06-16-copernicus-glo30-design.md` | 数据源切换，设计定完直接改代码，没走计划流程 |
| `2026-06-16-terrain-color-design.md` | 同上，当日即落地合入 master |
| `2026-07-30-workbench-ux-redesign-design.md` | 写下时项目已停止产出 `plans/`（2026-07-28 后停摆，2026-08-04 才随地形三角化计划恢复） |
| `2026-08-01-concurrency-recommend-design.md` | 同上 |
| `2026-08-02-absolute-save-path-design.md` | 同上 |
| `2026-08-04-src-layout-migration-design.md` | 同上；且改动清单本身即步骤，另写 plan 是重复 |

反方向（有 plan 没 design）只有一份，见 `../plans/README.md`。
