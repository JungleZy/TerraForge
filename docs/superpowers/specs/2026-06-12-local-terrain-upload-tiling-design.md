# 本地高程切片（上传 GeoTIFF 后切片）设计

日期：2026-06-12
状态：已与用户确认，待写实现计划

## 结论

新增一条独立的「本地高程切片」任务线：用户在首页上传多个 GeoTIFF（`.tif/.tiff`），后端保存到任务目录后自动调用现有 terrain tiler 生成 Cesium quantized-mesh 瓦片。不复用、不污染现有 NASA DEM 下载任务（`dem_tasks`）。

## 范围与决策（已确认）

| 决策点 | 结论 |
| --- | --- |
| 任务模型 | 独立的本地切片任务，新增表，不混进 `dem_tasks` |
| 多文件 | 一个任务支持上传多个 `.tif/.tiff`，一起切片 |
| 自动切片 | 上传成功后立即后台开始切片，无需手动点开始 |
| 入口 | 首页 `downloadType` 下拉新增「本地高程切片」 |
| 暂停/恢复 | 不做。切片不是可恢复的分块任务，假暂停会误导 |
| 取消 | 上传中/未开始可取消；已进入 `build_terrain()` 无法硬中断，标记 `cancel_requested`，进程返回后落 `cancelled` |

## 非目标（YAGNI）

- 不做切片暂停/恢复。
- 不做上传文件的 bbox 推断展示（tiler 自己从 raster bounds 算）。
- 不重写现有 `tile_dem_task_dir()` / `build_terrain()`。
- 不修复现有 DEM 静态路由写死 `downloads/dem` 的问题（与本功能无关，且本功能的新路由从 DB 读路径，不重复该问题）。

## 架构

### 核心边界

- `dem_tasks` 继续只表示「从 NASA Earthdata 下载 DEM」。
- 新增 `local_terrain_tasks` / `local_terrain_files` 表示「上传 GeoTIFF 后切片」。
- 新增 `services/local_terrain_task_manager.py`（`LocalTerrainTaskManager`），职责：
  1. 保存上传文件；
  2. 维护本地切片任务状态；
  3. 后台调用现有 `tile_dem_task_dir()` 切片。
- 复用 `services/terrain_tiling/dem_task_tiler.py`：它已能从目录读取多个 `*_dem.tif` 并传给 `build_terrain(inputs=[...])`。我们把上传文件规范化保存为 `*_dem.tif` 即可复用，无需改 tiler。
- 新增 `routes/local_terrain_api.py`，不把上传逻辑塞进 `terrain_api.py`（后者是 DEM 下载任务的 terrain 接口）。
- `app.py` 像 `DemTaskManager` 一样初始化并注入 `LocalTerrainTaskManager`，在 blueprint 注册之前完成注入。

### 目录约定

```text
downloads/terrain/local_task_<id>/
  source/
    upload_1_dem.tif
    upload_2_dem.tif
  terrain_tiles/
    layer.json
    meta.json
    {z}/{x}/{y}.terrain
```

`source/` 与 `terrain_tiles/` 分开，便于重切片/删除，且上传文件不与输出瓦片混淆。`stored_filename` 以 `*_dem.tif` 结尾，确保 `list_dem_tifs()` 能识别。

## 数据库

在 `database.py:init_database()` 内创建（与项目现有习惯一致，不单独依赖 `migrations/`）。

### local_terrain_tasks

| 列 | 说明 |
| --- | --- |
| id | 主键 |
| name | 任务名 |
| status | `pending` / `uploading` / `running` / `completed` / `failed` / `cancelled` |
| output_path | `downloads/terrain/local_task_<id>` |
| source_dir | `.../source` |
| output_dir | `.../terrain_tiles` |
| total_files | 上传文件总数 |
| uploaded_files | 成功保存数 |
| failed_files | 保存失败数 |
| maxzoom | 切片最大层级 |
| parent_url | 级联父级 terrain，指向全局 base |
| created_at / started_at / completed_at | 时间戳 |
| error_message | 失败原因 |

索引：`idx_local_terrain_tasks_status`。

### local_terrain_files

| 列 | 说明 |
| --- | --- |
| id | 主键 |
| task_id | 外键 → local_terrain_tasks(id) ON DELETE CASCADE |
| original_filename | 用户上传原名（仅展示，不用于落盘路径） |
| stored_filename | 规范化后的 `*_dem.tif` |
| local_path | 绝对/相对存储路径 |
| size_bytes | 文件大小 |
| status | `uploaded` / `failed` |
| error_message | 失败原因 |

约束：`UNIQUE(task_id, stored_filename)`。索引：`idx_local_terrain_files_status`。

## API

新增 blueprint `local_terrain_api_bp`，前缀 `/api/terrain/local`。

### POST /api/terrain/local/tasks

- `multipart/form-data`
- 字段：
  - `name`：任务名
  - `files[]`：多个 `.tif/.tiff`
  - `maxzoom`（可选）：默认读配置 `terrain_local_maxzoom`
- 行为：
  1. 创建任务行（`pending`）；
  2. 保存所有上传文件到 `source/`，逐个记录 `local_terrain_files`；
  3. 自动启动后台切片线程；
  4. 返回 `{ success: true, task }`。
- 校验失败（无有效文件等）返回 400。

### GET /api/terrain/local/tasks

- 列出本地切片任务，供首页活动任务区加载和历史页后续扩展。

### GET /api/terrain/local/tasks/<id>

- 返回任务详情、文件列表、`layer.json` URL。

### POST /api/terrain/local/tasks/<id>/cancel

- `pending`/`uploading`：可取消。
- 已进入 `build_terrain()`：标记 `cancel_requested`，进程返回后落 `cancelled`（不假装能硬中断）。

返回约定沿用现有风格：成功 `{"success": true, ...}`；`ValueError` → 400/404；manager 未注入 → 500；未预期异常记日志 + 泛化 JSON。

## 前端

### 首页表单（templates/index.html + static/js/map.js）

- `downloadType` 新增「本地高程切片」。
- 选中后隐藏地图 bbox / zoom / 地图样式 / DEM 下载选项；显示：
  - 任务名称；
  - 多文件上传控件 `accept=".tif,.tiff"`；
  - 可选 `maxzoom`（默认配置值或 14）；
  - 提交按钮文案「上传并开始切片」。
- 提交分流：`downloadType === 'local_terrain'` → 用 `FormData` `POST /api/terrain/local/tasks` → 成功后 `loadActiveTasks()`。

### 活动任务（static/js/tasks.js）

- `normalizeTask()` 扩展：
  - `task_type: 'local_terrain'`
  - `total_items = total_files`
  - `downloaded_items = uploaded_files`（切片完成时显示满）
  - `items_label = '文件'`
- `apiPrefixForType('local_terrain') = '/api/terrain/local/tasks'`。
- 按钮：`running` 显示「取消」；`pending/uploading` 可取消；`completed/failed/cancelled` 不显示下载任务的暂停/恢复。

### 实时状态（Socket.IO）

复用统一事件，`task_type` 固定 `local_terrain`：

- 保存文件、切片开始 → `task_progress`
- 切片成功 → `task_completed`
- 失败 → `task_failed`

## 静态服务

新增 `GET /terrain/local/<task_id>/<subpath>`（可放在 `routes/terrain_static.py` 或新文件）：

- 目录从 `local_terrain_tasks.output_dir` 查询（不写死路径）。
- 用现有 `_resolve_safe_file()` 校验解析后的文件落在 `Config.DOWNLOADS_DIR` 之下，阻止 `..` 和反斜杠穿越。
- 切片完成后 `patch_layer_json_parent()` 写入 `parent_url`，CesiumJS 自动级联到全局 base。

## 错误处理

- 上传校验：扩展名必须 `.tif/.tiff`；零字节拒绝；至少 1 个有效文件，否则任务 `failed` + 400。
- 保存失败：单文件失败记 `local_terrain_files.status='failed'`，不中断其余；全部失败 → 任务 `failed`。
- 切片失败：`build_terrain()` 抛异常 → 任务 `failed`，`error_message` 落库，发 `task_failed`。
- 文件名安全：`stored_filename` 用任务内序号重命名（`upload_<n>_dem.tif`），不信任原始文件名。
- 取消语义：已进入 `build_terrain()` 无法硬中断，UI 明示，标记 `cancel_requested`，进程返回后落 `cancelled`。
- 重启恢复：`LocalTerrainTaskManager` 启动时把残留 `running` 任务降级为 `failed`（切片不可恢复，降 `paused` 会误导），沿用现有 orphan recovery 模式。

## 测试范围

沿用现有 `tests/` 模式：`sys.path.insert` + monkeypatch `Config.DATABASE_PATH/DOWNLOADS_DIR/CACHE_DIR` + `sys.modules.pop` 重导入。

1. `tests/test_local_terrain_api.py`
   - app wiring 不 500；
   - 上传 0 个有效文件 → 400；
   - 上传多个 tif → 创建任务、文件落盘、注入的 `build_terrain_fn` 被调用（不跑 GDAL/numpy）；
   - `GET /tasks/<id>` 返回文件列表与 `layer.json` URL。
2. `tests/test_local_terrain_static.py`
   - `/terrain/local/<id>/layer.json` 从 `output_dir` 服务文件；
   - 路径穿越被拒。
3. `tests/test_local_terrain_schema.py`
   - 两张新表与索引存在；关键列 NOT NULL。
4. 扩展 `tests/test_orphan_recovery.py`
   - 残留 `running` 本地切片任务恢复为 `failed`。

切片本身复用 `tile_dem_task_dir()`，已有 `test_dem_task_tiler.py` 覆盖，不重复测 GDAL 路径。

## 需要新增/修改的文件清单

新增：
- `services/local_terrain_task_manager.py`
- `routes/local_terrain_api.py`
- `tests/test_local_terrain_api.py`
- `tests/test_local_terrain_static.py`
- `tests/test_local_terrain_schema.py`

修改：
- `database.py`（建表 + 索引）
- `app.py`（初始化 + 注入 + 注册 blueprint）
- `routes/__init__.py`（导出新 blueprint）
- `routes/terrain_static.py`（新增本地切片静态路由）
- `templates/index.html`（下载类型 + 上传表单）
- `static/js/map.js`（提交分流 + FormData）
- `static/js/tasks.js`（normalize + apiPrefix + 按钮）
- `tests/test_orphan_recovery.py`（扩展）
