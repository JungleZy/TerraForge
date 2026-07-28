# GIS 界面专业度改造 · 主计划

> **For agentic workers:** 本文是**总览**，不含可执行步骤。实际执行请打开对应阶段的子计划，并使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`。

**Goal:** 把 map-download 从「能跑的网页表单」改造成「GIS 从业者愿意用的空间数据工具」——先保证产出数据正确，再补齐界面专业度。

**Architecture:** 改造拆成两条**互不阻塞**的并行线。Phase 1 只碰 Python 后端与 `map.js` 的表单逻辑，修的是「产出的数据对不对」；Phase 2 只碰 CSS 与前端渲染层，修的是「界面像不像专业工具」。两条线的文件交集只有 `static/js/map.js`（Phase 1 改提交/绘制逻辑，Phase 2 改四至显示区域），冲突点已在各自计划中标注。

**Tech Stack:** Flask 3 + Jinja2 + Socket.IO · Bootstrap 5.3 + Leaflet 1.9.4 + 原生 JS（无构建工具）· GDAL 3.8.4 · SQLite · pytest · uv · PyInstaller

**依据：** [`docs/ui-review-2026-07.md`](../../ui-review-2026-07.md)（90 条发现，24 条经对抗性验证）

---

## Global Constraints

以下约束对**所有阶段的所有任务**生效。

### 环境与命令

| 项 | 值 |
|---|---|
| Python 环境 | 项目根 `.venv/`，所有命令走 `uv run`，**不要 `source .venv/bin/activate`** |
| 跑全量测试 | `uv run pytest tests/ -q` |
| 跑单个测试 | `uv run pytest tests/test_x.py::test_name -v` |
| 起开发服务 | `DEBUG=0 uv run python app.py`（`DEBUG=1` 会开 reloader，实测干扰截图） |
| GDAL | **3.8.4 可用，`osgeo.gdal_array` 已编译**（实测确认）。测试可直接用真 GDAL，无需 mock 或 skipif |

### 测试基线

**改造开始前：148 passed, 27 warnings in 54.00s**（2026-07-27 实测）。

- 27 个 warning 全部是 `sqlite3` 的 `DeprecationWarning: The default datetime adapter is deprecated`，与本次改造无关，**不要顺手修**（超出范围）
- 任何阶段的任何任务完成后，`uv run pytest tests/ -q` 必须仍是 **148+ passed, 0 failed**
- 新增测试只增不减这个数字

### 测试写法约束

- **无 `conftest.py`**。每个测试文件顶部自己 `sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))`
- 涉及 `app` 或数据库的测试：**先** monkey-patch `Config.DATABASE_PATH`/`DOWNLOADS_DIR`/`CACHE_DIR` 到 `tmp_path`，**再** `sys.modules.pop("app", None)` 并重新 import（`init_database()` 在 import 时就跑）。范式见 `tests/test_terrain_api.py`
- **无任何 JS 测试框架**（无 `package.json`/vitest/jest），**不要引入**——违背项目离线打包形态
- 前端改动的回归保护走 `tests/test_index_has_contour_option.py` 的范式：用 pytest 读模板/静态文件文本并断言
- 纯视觉效果（颜色、尺寸、有没有漏白）用 Playwright 实测 + 检查清单，不写自动化断言

### 代码约束

- **不引入任何新的运行时依赖**（npm 包、Python 包、CDN 资源都不行）。项目要能 PyInstaller 打包离线运行
- 数据库 schema 变更走 `init_database()` 里的 `ALTER TABLE ... ADD COLUMN` + try/except 吞 `duplicate column name`，**不要**新建 `migrations/` 文件（项目约定见 `CLAUDE.md`）
- 任何写磁盘的路径必须走 `Config.*_DIR`，保证 frozen/源码两种运行方式都正确
- commit message 用中文 conventional commits，格式参照 `git log`（第一行结论，body 说清为什么）

### 存量兼容

- **不得静默改变已有用户的产出行为**。Phase 1 涉及输出坐标系变更，默认值必须保持现状（EPSG:4326），新行为通过显式选项开启
- 不得让已存在的任务记录（`tasks`/`dem_tasks`/`contour_tasks` 表）无法读取

---

## 阶段划分

```
                    ┌─────────────────────────────────┐
   ┌────────────────│  Phase 1：数据正确性             │───┐
   │                │  B1 拼接配准 + A8 三处硬 bug     │   │
   │                │  ~8h · 后端为主 · 可 TDD         │   │
   │                └─────────────────────────────────┘   │
   │                                                       │
   │                ┌─────────────────────────────────┐   │
   ├────────────────│  调色板修复（Phase 1 后追加）    │───┤
   │                │  拼接产物丢 color table          │   │
   │                │  rgbExpand 逐瓦片展开成 RGB      │   │
   │                └─────────────────────────────────┘   │
   │                                                       ├──► 统一发一个版本
   │                ┌─────────────────────────────────┐   │
   └────────────────│  Phase 2：视觉与交互             │───┘
                    │  C1 CSS 清理 → A1-A7 视觉修复    │
                    │  ~22h · 前端 · 部分可 TDD        │
                    └─────────────────────────────────┘
                              │
                              │ C1 必须先于 A1-A7
                              ▼ （阶段内强顺序）
```

> **发版策略（用户 2026-07-27 决定）：全部改造完成后统一发一个版本。**
> 各阶段完成后只合并到集成分支，**不 bump 版本号、不打 tag**。这样避免中间态版本流出——尤其 Phase 1 内部有一个「产出坐标系已改成 3857 但还没 warp 回 4326」的中间态，以及 Phase 2 的 C1 与 A1-A7 之间界面处于「地基已清理但视觉未修」的状态。

### 为什么这样切

**按「改动会不会互相踩」切，不按「功能模块」切。** Phase 1 动的是 Python 和表单提交逻辑，Phase 2 动的是 CSS 和渲染层。两边同时开工不会撞车（唯一交集见下）。

**Phase 2 内部有强顺序：C1 必须先做。** `style.css` 有 92 处 `!important` 和一整块自我覆盖的字号声明（`:1318-1434` 重新声明了前面已定义的选择器）。不先清理，A5 的密度调整会改了不生效——改 `:877-887` 的 `font-size` 没用，真正生效的是 `:1342` 那条 `!important`。这不是理论风险，是评审时实测到的。

### 两阶段的唯一文件交集

`static/js/map.js`：

| 阶段 | 改哪里 | 做什么 |
|---|---|---|
| Phase 1 | `:34-77` 绘制事件监听、`:247/376/525` 三处表单重置 | 补 `L.Draw.Event.EDITED` 监听、抽 `resetForm()` |
| Phase 2 | `:141-152` `updateBoundsInfo` 的四至渲染 | 把 5 行坐标压成 1-2 行（A5，单项值 90px 高度） |

**不重叠**，但如果两条线并行推进，合并时留意 `updateBoundsInfo` 的调用点——Phase 1 的 `EDITED` 监听会新增一个对它的调用。

---

## 各阶段验收标准

### Phase 1 —— [`2026-07-27-phase1-data.md`](2026-07-27-phase1-data.md)

**目标：产出的 GeoTIFF 能和正确数据叠合；界面上不存在必崩的选项；框选范围所见即所得。**

验收：

- [ ] `uv run pytest tests/ -q` ≥ 148 passed, 0 failed
- [ ] 新增测试能证明修复前的配准是错的（先看它失败，再看它通过）
- [ ] 拼接产出的 GeoTIFF 用 `gdalinfo` 查看，角点坐标与瓦片理论边界一致
- [ ] 「输出格式」下拉里不再有会抛异常的选项
- [ ] 实测：画框 → 拖顶点改框 → 右侧四至数字**跟着变** → 提交的是新 bbox
- [ ] 实测：任一类型任务创建成功后，表单状态正确重置

**可独立完成并合并**，不依赖 Phase 2 的任何改动。但**不单独发版** —— 见上方发版策略。

### Phase 2 —— [`2026-07-27-phase2-visual.md`](2026-07-27-phase2-visual.md)

**目标：界面在 1366×768 上一屏放得下；进度条不再骗人；没有漏白的控件。**

验收：

- [ ] `uv run pytest tests/ -q` ≥ 148 passed, 0 failed
- [ ] C1 完成后视觉基线截图对比：**除已知的 select 箭头修复外，界面无可见变化**
- [ ] 1366×768 视口下，首页「创建下载任务」按钮在折叠线**以上**
- [ ] 刚启动（进度 <25%）的任务，进度条**不是红色**
- [ ] 任务失败后卡片**不消失**，显示错误原因，toast 常驻不自动消失
- [ ] 文件选择按钮、number 微调箭头、select 弹层在深色背景下**无白底**
- [ ] Leaflet 绘制工具条与深色主题一致，且**图标仍然显示**（不是空白按钮）
- [ ] 所有 select 有下拉三角指示符（修复 `background` 简写导致的图标丢失）

**可独立完成并合并**。作为最后一个阶段，它完成后才进入统一发版流程。

---

## 建议的执行顺序

**推荐：Phase 1 先行。**

理由：第一档全是观感，改完界面好看了，但拼出来的 GeoTIFF 依然和别人的数据叠不上。对一个 GIS 工具来说，数据正确性是唯一一条「不修就不能叫这个名字」的问题。而且 Phase 1 可 TDD、可验证、~8h 就能发一个版。

**如果更想先看到界面变化**，直接从 Phase 2 开始也完全可以——两阶段无依赖。但**不要跳过 C1 直接做 A1-A7**。

---

## 本次改造范围外

以下在评审中被识别但**明确不在这两个阶段**，避免执行时范围蔓延：

| 项 | 属于 | 为什么现在不做 |
|---|---|---|
| B2 地图状态栏 HUD（坐标/比例尺/CRS） | 第二档 | 独立特性，值得单独一个计划 |
| B3 框选实时预估条 | 第二档 | 同上。后端 `expected_tile_count` 已就绪，只差接线 |
| B4 范围手输 / 导入 GeoJSON | 第二档 | 同上 |
| B5 底图换真实下载源 + GCJ-02 说明 | 第二档 | 需要新增瓦片代理路由，独立特性 |
| B6 任务日志面板 + 三态结束 | 第二档 | 独立特性 |
| B7-B12 | 第二档 | 见评审报告第 4 节 |
| C2-C5 JS 模块化 / 图标 sprite / 可拖拽分栏 / 增量渲染 | 第三档 | 纯内部重构，用户无感知，不急 |
| 27 个 sqlite3 DeprecationWarning | 无 | 与本次改造无关 |
| GCJ-02 栅格纠偏、projects 工程表、五页导航重构 | **不推荐做** | 理由见评审报告第 5 节 |

**执行时如果发现某个「顺手就能修」的东西不在计划里 —— 记下来，不要顺手改。** 范围蔓延会让验收标准失效。
