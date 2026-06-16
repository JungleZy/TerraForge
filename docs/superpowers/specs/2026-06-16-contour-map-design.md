# 等高线地图下载功能 — 设计文档

**一句话结论**：新增第 4 种下载类型「等高线瓦片」——框选区域后，自动从 NASA 下载该区域的 ASTER DEM，用 `gdal_contour` 生成等高线，再用 matplotlib 渲染成**透明背景的 PNG 瓦片**（解放军军标/国标棕色等高线 + 计曲线高程标注），存盘可下载、本地可在地图上叠加预览。

日期：2026-06-16

---

## 1. 目标与非目标

### 目标
- 用户在现有下载页框选 bbox、选「等高线瓦片」、填等高距 → 一键得到该区域的等高线瓦片集（`{z}/{x}/{y}.png`），可下载存盘。
- 瓦片为**透明背景 + 棕色等高线**，能叠在现有卫星图/标准图上看。
- **任务完成后在网页地图上一键叠加预览**（透明等高线层盖在现有底图上，带显示/隐藏开关），直观确认效果 —— **首版必做**。
- 等高线分**首曲线**（细）/**计曲线**（每 5 条加粗 + 标注高程数字），配色遵循**解放军军用地形图棕色惯例**（等高线棕色，与国标 GB/T 20257 一致），**全部色值/线宽可配置**。
- 等高距默认按范围给值（小范围 50m / 大范围 100m），用户每次可改。

### 非目标（YAGNI）
- 不做带地貌晕渲底色的「完整彩色地形图」（只做纯线叠加层）。
- 不做矢量瓦片 / 可点击交互（只出 PNG 栅格）。
- 不接入任何第三方等高线瓦片源（OpenTopoMap 等条款禁止批量下载）。
- 不追求国标官方精确 CMYK 认证色值（用经验棕默认，留配置位）。
- 不复用 Cesium quantized-mesh 那条 3D 地形管线（那是 3D 高程网格，与 2D 线划无关）。

---

## 2. 背景与决策依据

**为什么必须 DEM 自建，而不是用现成瓦片源？**

调研了用户提到的国内外现成源，结论是「要下载」就排除了所有免费源：

| 源 | 是否真等高线 | 能否批量下载 |
|---|---|---|
| 高德 | 无（只有影像/路网） | — |
| 天地图 `ter_w` | 地形晕渲，非等高线；需 tk 配额 | 大批量超配额 |
| OpenTopoMap | 真等高线 | 使用条款明令禁止批量下载/预取，违规封 IP |

本项目本质是「瓦片下载器」，而唯一免费的真等高线源 OpenTopoMap 恰恰禁止批量下载。因此 **DEM 自建是唯一合法、可持续、可商用、可离线的路径**（ASTER GDEM 是 NASA 开放数据）。

---

## 3. 已确定的决策清单

| # | 决策点 | 选定 |
|---|---|---|
| 1 | 数据来源 | ASTER GDEM v3（ASTGTM.003）自建 |
| 2 | 核心诉求 | 框选区域下载等高线 PNG 瓦片存盘 + 网页地图叠加预览（首版必做） |
| 3 | 工作流 | 一站式：框选 → 自动下 DEM → 生成等高线 → 渲染瓦片 |
| 4 | 视觉形态 | 纯等高线透明叠加层 |
| 5 | 架构 | 独立 `ContourTaskManager`，作为第 4 种下载类型 |
| 6 | 渲染技术 | matplotlib（`contour` + `clabel` 标注高程） |
| 7 | 等高距 | 默认 50m（小范围）/100m（大范围），用户可填 |
| 8 | 配色 | 解放军军标/国标棕色规则 + 经验棕默认 + 全可配置；背景默认透明，可选填底色 |

---

## 4. 架构总览

### 数据流（一站式）

```
用户框选bbox + 选"等高线瓦片" + 填等高距 + zoom范围
        │  POST /api/contour/tasks
        ▼
[1] ContourTaskManager.create_task
      → dem_granules.tiles_for_bbox(bbox) 算出覆盖的 DEM granule 列表
      → 写 contour_tasks + contour_files 行，返回 task_id
        │  后台线程（照抄 DemTaskManager 的 active_tasks/stop_flags 模式）
        ▼
[2] 下载 DEM：复用 DemDownloadEngine.download_files(...)
      → downloads/dem/contour_task_<id>/*_dem.tif
        │  下载完在同一任务流程内自动接续（= 一站式）
        ▼
[3] 生成等高线：contour_engine 对每个 *_dem.tif 跑 gdal_contour -i <等高距>
      → contour_lines/*.geojson（带 elevation 属性）
        │
        ▼
[4] 渲染瓦片：按 Web Mercator z/x/y 网格，matplotlib 渲染透明 PNG
      → contour_tiles/{z}/{x}/{y}.png
        │  GET /contour/<task_id>/{z}/{x}/{y}.png
        ▼
[5] 前端 Leaflet 叠加预览 / 用户打包下载 contour_tiles 目录
```

### 落盘布局（对齐现有 `dem_task_<id>` 约定）

```
downloads/dem/contour_task_<id>/
├── *_dem.tif                      # 下载的原始 DEM（复用现有 granule 命名）
├── contour_lines/*.geojson        # gdal_contour 产出的矢量（中间产物）
└── contour_tiles/
    ├── {z}/{x}/{y}.png            # 最终透明等高线瓦片
    └── （无 layer.json — 这是 XYZ 栅格瓦片，不是 Cesium 地形）
```

> 注：复用 `downloads/dem/` 根目录但用 `contour_task_` 前缀，与 `dem_task_` 区分，避免污染现有 DEM 任务。

---

## 5. 数据库设计

新增两张表（结构照抄 `dem_tasks` / `dem_files`，放进 `init_database()`）：

### `contour_tasks`
```
id INTEGER PK
name TEXT NOT NULL
status TEXT DEFAULT 'pending'        -- pending/running/paused/completed/failed/cancelled
north/south/east/west REAL NOT NULL
dataset TEXT DEFAULT 'ASTGTM.003'
contour_interval REAL NOT NULL       -- 等高距（米）
zoom_min INTEGER NOT NULL
zoom_max INTEGER NOT NULL
output_path TEXT
total_files/downloaded_files/failed_files INTEGER DEFAULT 0   -- DEM 下载进度
total_tiles/rendered_tiles/failed_tiles INTEGER DEFAULT 0     -- 瓦片渲染进度
created_at/started_at/completed_at TIMESTAMP
error_message TEXT
```

### `contour_files`（跟踪每个 granule 下载状态，= `dem_files`）
```
id INTEGER PK
task_id INTEGER NOT NULL → contour_tasks(id) ON DELETE CASCADE
granule_id TEXT NOT NULL
status TEXT DEFAULT 'pending'
local_path TEXT
size_bytes INTEGER
retry_count INTEGER DEFAULT 0
error_message TEXT
```

> 瓦片级状态先只用 `contour_tasks` 的计数字段，不单独建 `contour_tiles` 表（YAGNI；失败重渲染以整任务为粒度）。

---

## 6. 后端组件

| 文件 | 职责 | 复用/新建 |
|---|---|---|
| `services/contour_task_manager.py` | 任务生命周期（create/start/pause/cancel）+ 后台线程；`_execute` 先下 DEM 再调 contour_engine 渲染 | 照抄 `DemTaskManager` 骨架 |
| `services/contour_engine.py` | **核心新代码**：`gdal_contour` 包装 + matplotlib 瓦片渲染 + 配色 | 全新 |
| `routes/contour_api.py` | `/api/contour/tasks` CRUD + 进度查询 | 照抄 `dem_api.py` |
| `routes/contour_static.py` | `/contour/<id>/{z}/{x}/{y}.png` 服务，复用 `_resolve_safe_file` 路径安全 | 照抄 `terrain_static.py` |
| `app.py` | 注入 manager + 注册 blueprint | 照抄现有 `init_*_task_manager` 4 处 |

### 复用的无状态工具（不重复造）
- `services/dem_granules.py`：`tiles_for_bbox`、`astgtm_v3_granules_for_tile`
- `services/dem_download_engine.py`：`DemDownloadEngine.download_files(dataset, granules, output_dir, progress_callback, stop_flag)`
- `routes/terrain_static.py`：`_resolve_safe_file` 的路径穿越防护逻辑

---

## 7. 渲染管线（核心新技术，详细）

这是全项目唯一「画 2D 图片瓦片」的新能力（现有代码只切 3D 地形网格）。

### 投影对齐
- DEM tif 是 **EPSG:4326**（经纬度）。
- 目标瓦片是 **EPSG:3857 Web Mercator slippy map**（和现有 Google/标准底图瓦片严格对齐）。
- 瓦片坐标计算复用 `download_engine.py` 已有的 Web Mercator deg↔tile 逻辑（同一套 `WEB_MERCATOR_MAX_LAT=85.0511`、zoom 0–21）。

### 单瓦片渲染步骤（对每个 z/x/y）
1. 由 (z,x,y) 算出该瓦片的 Web Mercator bbox → 转回经纬度 bbox。
2. 从该 task 的 DEM 数据裁出覆盖此 bbox 的高程（gdal，VRT 拼接多 granule）。
3. `gdal_contour -i <interval>` 生成该范围等高线（带 `elevation` 属性）。
4. matplotlib 渲染：
   - `figsize=(2.56, 2.56), dpi=100` → 256×256；`subplots_adjust(0,0,1,1)`、`ax.axis('off')`、`savefig(transparent=True, pad_inches=0)`。
   - `set_xlim/ylim` = 瓦片的投影范围，保证像素对齐。
   - **首曲线**（elevation % (interval*index_step) != 0）：细线，默认 `#9C6B3F` 0.5px。
   - **计曲线**（elevation % (interval*index_step) == 0）：粗线，默认 `#7A4F2A` 1.2px，`clabel` 沿线标注高程数字（默认棕 `#7A4F2A`，字号 6）。
5. 存 `contour_tiles/{z}/{x}/{y}.png`。空瓦片（无线穿过）可跳过或存全透明。

### 优化（实现时按需，不阻塞首版）
- 先对整个 task 的 DEM 跑一次 `gdal_contour` 生成全域 GeoJSON，再按瓦片裁剪渲染，避免每瓦片重算。
- 低 zoom（如 < 10）等高线过密无意义，可对 zoom 范围做合理下限提示。

---

## 8. 配置项（`DEFAULT_CONFIGS` 新增）

```python
"contour_default_interval": "50",        # 默认等高距（米）
"contour_color_intermediate": "#9C6B3F", # 首曲线色
"contour_color_index": "#7A4F2A",        # 计曲线色
"contour_color_label": "#7A4F2A",        # 高程注记色
"contour_width_intermediate": "0.5",     # 首曲线线宽（px）
"contour_width_index": "1.2",            # 计曲线线宽（px）
"contour_background": "transparent",     # 背景（transparent 或 #RRGGBB）
"contour_index_step": "5",               # 每几条等高线一条计曲线
```

> 全部经 `ConfigManager` 读取。将来拿到解放军军标/国标精确 CMYK，改配置即生效，不动代码。

---

## 9. 前端改动

### `templates/index.html`
- `downloadType` 下拉加 `<option value="contour">等高线瓦片</option>`。
- 新增 `contourOptions` 区块（默认隐藏）：等高距输入框（默认值随范围给 50/100）。
- zoom 范围、保存路径复用现有控件。

### `static/js/map.js`
- `initDownloadTypeToggle()`：加 `contour` 分支，控制 `contourOptions` 显隐、保存路径默认 `./downloads/contour`。
- 表单提交：`contour` 类型 → POST `/api/contour/tasks`，body 含 bbox + `contour_interval` + zoom 范围。
- **预览（首版必做）**：任务完成后在活动任务/任务列表加「在地图上预览」按钮，点击执行 `L.tileLayer('/contour/<id>/{z}/{x}/{y}.png', {opacity:0.9, maxNativeZoom:<zoom_max>}).addTo(map)` 把等高线层叠到当前地图；配一个图层显示/隐藏开关（Leaflet `L.control.layers` 或自定义按钮）。透明背景天然盖在卫星/标准底图上，不挡现有要素。

---

## 10. 瓦片服务与安全

- `routes/contour_static.py`：`GET /contour/<int:task_id>/<path:subpath>`，base_dir = `Config.DOWNLOADS_DIR/dem/contour_task_<id>/contour_tiles`。
- 必须走 `_resolve_safe_file`（照抄 terrain_static）：每个文件 resolve 后必须落在 `DOWNLOADS_DIR` 下，挡路径穿越。
- 路径在请求时从当前 `Config.DOWNLOADS_DIR` 重算（兼容 PyInstaller frozen 模式迁移），不信任建任务时存的绝对路径。

---

## 11. 测试策略（遵循现有 pattern）

- `contour_engine` 的 GDAL/matplotlib 调用做成**可注入**（参照 `dem_task_tiler` 的 `build_terrain_fn=` 钩子）：测试塞 fake 渲染函数，不需真 GDAL/numpy/matplotlib。
- 测点：
  - `create_task` 的 granule 计算与 DB 行写入。
  - 瓦片路径/服务路由 + 路径穿越防护（照抄 `test_terrain_api` 的 monkey-patch Config 模式）。
  - bbox → z/x/y 瓦片列表的边界（跨经线、极区裁剪）。
  - 首曲线/计曲线分类逻辑（elevation % step）。
- 测试前 monkey-patch `Config.DATABASE_PATH`/`DOWNLOADS_DIR`/`CACHE_DIR` 到 tmp_path，再 `sys.modules.pop("app")` 重导入。

---

## 12. 实现顺序（给 writing-plans 铺路）

1. **DB + 配置**：`init_database()` 加两表；`DEFAULT_CONFIGS` 加配置项。
2. **contour_engine（核心）**：先跑通「单 granule → gdal_contour → 渲染单个 256×256 透明 PNG（首/计曲线+标注）」，可注入。
3. **contour_task_manager**：照抄 DemTaskManager，`_execute` 末尾接 contour_engine 全量渲染 + 进度 emit。
4. **routes**：`contour_api` + `contour_static`；`app.py` wiring。
5. **前端**：`downloadType` 选项 + 等高距输入 + 提交分支 + **预览叠加（图层显示/隐藏开关 + `L.tileLayer` 叠加）**。
6. **测试**：按第 11 节补齐。

---

## 13. 风险与开放问题

- **GDAL `_gdal_array` 依赖**：渲染裁切要用 `ReadAsArray`，需 GDAL Python 绑定带 numpy 支持（见 CLAUDE.md 的 sdist 重建说明）。新增 matplotlib 依赖，写进 `requirements.txt`。
- **NASA 下载慢 + Earthdata 登录**：一站式依赖 Earthdata 凭据已配置；WSL2 开发环境需 Windows 网关代理（见项目 memory）。
- **大范围 × 高 zoom 瓦片量爆炸**：等高线渲染比下载现成瓦片慢得多，需参照现有 `WARN_TILES_THRESHOLD` 给 UI 警告。
- **国标精确色值**：当前为经验棕，已全配置化，后续可替换。
