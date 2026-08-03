# 等高线瓦片下载功能 实现计划

> **归档文档 · 非当前实现**
> **记录时间**：2026-06-16 ｜ **状态**：部分作废（渲染层契约基本成立，入口与产品定位已被推翻）
> 已作废的四点：①**入口从「框选 bbox 自动下载 DEM」改成「上传 GeoTIFF」**——下载驱动的 `create_task` 已删除（`services/contour_task_manager.py:11-12` 明写；现存的是 `create_task_with_files`），`routes/contour_api.py:77` 的创建接口只收 multipart 上传、不接受 bbox；②Task 9 的前端 Leaflet 叠加代码已失效，地图引擎现为 CesiumJS；③测试片段里的 `from config import Config` / `from database import get_connection` 路径已失效，两个模块现在都在 `core/` 下；④**产品定位被推翻**——计划把「不做晕渲」列为 YAGNI，现在默认出图就是分层设色 + 晕渲（见 `services/contour_engine.py:100`）。
> ⚠️ 复选框状态无效；正文源码与行号为当日快照，禁止照抄或照行号定位。
> *正文保持原样未回改。*

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增第 4 种下载类型「等高线瓦片」——框选 bbox 后自动从 NASA 下 ASTER DEM、用 GDAL+matplotlib 渲染成透明背景的棕色等高线 PNG 瓦片（首曲线/计曲线 + 计曲线标高程），存盘可下载、网页地图可叠加预览。

**Architecture:** 照抄现有 DEM 下载管线：独立 `ContourTaskManager`（复用 `dem_granules` 算 granule、`DemDownloadEngine` 下 DEM），下载完在同一任务流程内接续「等高线渲染」阶段（`contour_engine` + 可注入的 `contour_task_tiler`）。瓦片走独立 `/contour/<id>/{z}/{x}/{y}.png` 路由（复用 `terrain_static._resolve_safe_file` 防穿越）。前端在统一下载页加选项 + Leaflet 叠加预览。

**Tech Stack:** Flask + Flask-SocketIO、SQLite、aiohttp（复用）、GDAL（warp+ReadAsArray）、numpy、matplotlib（新增）、Leaflet。

**配色默认（解放军军标/国标棕色，等高线棕色，全部可配置）：** 首曲线 `#9C6B3F`、计曲线 `#7A4F2A`、注记 `#7A4F2A`、背景默认 `transparent`、每 5 条一计曲线。拿到军标精确 CMYK 后改配置即生效。

---

## 锁定的接口契约（所有 Task 必须严格一致）

**`services/contour_engine.py`** 顶层只依赖 `math`/`dataclasses`/`pathlib`/`typing`（GDAL/numpy/matplotlib 仅在 `build_contour_tiles` 函数体内 import，保证纯函数可无依赖测试）：
- `EARTH_RADIUS = 6378137.0`，`ORIGIN_SHIFT = math.pi * EARTH_RADIUS`，`WEB_MERCATOR_MAX_LAT = 85.0511`
- `lonlat_to_meters(lon, lat) -> (x, y)`
- `meters_to_lonlat(x, y) -> (lon, lat)`
- `deg2num(lat_deg, lon_deg, zoom) -> (x, y)`（标准 slippy-map，纬度先 clamp）
- `tile_bounds_meters(z, x, y) -> (xmin, ymin, xmax, ymax)`（EPSG:3857 米）
- `tiles_for_bbox_xyz(north, south, east, west, zoom) -> List[(x, y)]`
- `count_tiles(north, south, east, west, zoom_min, zoom_max) -> int`
- `is_index_contour(elevation, interval, index_step) -> bool`（计曲线判定）
- `@dataclass(frozen=True) class ContourStyle`：`color_intermediate, color_index, color_label, width_intermediate, width_index, background, index_step, label_size`；`@classmethod from_config(cls, config) -> ContourStyle`
- `build_contour_tiles(dem_tifs, out_dir, interval, zoom_min, zoom_max, style, progress_cb=None, stop_flag=None) -> dict`（返回 `{"total","rendered","failed"}`；`progress_cb(done, total)`；`stop_flag` 为 `threading.Event`）

**`services/contour_task_tiler.py`**：
- `@dataclass(frozen=True) class ContourParams`：`interval: float, zoom_min: int, zoom_max: int, style: ContourStyle`
- `contour_output_dir_for_task(task_output_path: str, task_id: int) -> Path` → `Path(task_output_path)/f"contour_task_{task_id}"/"contour_tiles"`
- `tile_contour_task_dir(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None, stop_flag=None) -> dict`（懒导入 `contour_engine.build_contour_tiles` 作默认；`build_contour_fn` 供测试注入；DEM 列表用 `services.terrain_tiling.vrt_builder.list_dem_tifs`）

**`services/contour_task_manager.py`**：`class ContourTaskManager`，方法集与 `DemTaskManager` 对齐：`__init__(socketio=None)`、`create_task(params)->int`、`start_task`、`pause_task`、`resume_task`、`cancel_task`、`get_task`、`list_tasks`、`_run_task`、`_execute`。

**`routes/contour_api.py`**：`contour_api_bp`（`url_prefix="/api/contour"`）、`init_contour_task_manager(tm)`；端点 `POST /tasks`（仅创建，返回 task_id）、`GET /tasks`、`GET /tasks/<id>`、`DELETE /tasks/<id>`、`POST /tasks/<id>/{start,pause,resume,cancel}`。

**`routes/contour_static.py`**：`contour_static_bp`（`url_prefix="/contour"`）、`GET /<int:task_id>/<path:subpath>`，`base_dir = Config.DOWNLOADS_DIR/"dem"/f"contour_task_{task_id}"/"contour_tiles"`，复用 `from routes.terrain_static import _resolve_safe_file`。

**落盘约定**：DEM 与瓦片都在 `Config.DOWNLOADS_DIR/dem/contour_task_<id>/`（`*_dem.tif` 在根，瓦片在 `contour_tiles/{z}/{x}/{y}.png`）。`create_task` 的 `output_path` 缺省 = `str(Path(Config.DOWNLOADS_DIR)/"dem")`。

---

## Task 1: 数据库表 + 默认配置

**Files:**
- Modify: `database.py`（`init_database()` 内、`DEFAULT_CONFIGS`）
- Test: `tests/test_contour_db.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_contour_db.py
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _reload_db(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "database"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("database")
    db.init_database()
    return db


def test_contour_tables_exist(monkeypatch, tmp_path):
    db = _reload_db(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        names = {r["name"] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()
    assert "contour_tasks" in names
    assert "contour_files" in names


def test_contour_default_configs_seeded(monkeypatch, tmp_path):
    db = _reload_db(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        rows = {r["key"]: r["value"] for r in cur.execute("SELECT key, value FROM config").fetchall()}
    finally:
        conn.close()
    assert rows["contour_default_interval"] == "50"
    assert rows["contour_color_index"] == "#7A4F2A"
    assert rows["contour_background"] == "transparent"
    assert rows["contour_index_step"] == "5"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_contour_db.py -v`
Expected: FAIL（`contour_tasks` 不在 names；KeyError on config）

- [ ] **Step 3: 实现 — 在 `init_database()` 中 `local_terrain_tasks` 建表块之后、`conn.commit()` 之前，加两张表**

```python
    # 等高线任务表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS contour_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            north REAL NOT NULL,
            south REAL NOT NULL,
            east REAL NOT NULL,
            west REAL NOT NULL,
            dataset TEXT NOT NULL DEFAULT 'ASTGTM.003',
            contour_interval REAL NOT NULL,
            zoom_min INTEGER NOT NULL,
            zoom_max INTEGER NOT NULL,
            output_path TEXT,
            total_files INTEGER DEFAULT 0,
            downloaded_files INTEGER DEFAULT 0,
            failed_files INTEGER DEFAULT 0,
            total_tiles INTEGER DEFAULT 0,
            rendered_tiles INTEGER DEFAULT 0,
            failed_tiles INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            error_message TEXT
        )
    ''')

    # 等高线 DEM 文件表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS contour_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            granule_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            local_path TEXT,
            size_bytes INTEGER,
            retry_count INTEGER DEFAULT 0,
            error_message TEXT,
            FOREIGN KEY (task_id) REFERENCES contour_tasks(id) ON DELETE CASCADE
        )
    ''')
```

- [ ] **Step 4: 实现 — 在 `DEFAULT_CONFIGS` 字典末尾追加 8 个键**

```python
    "contour_default_interval": "50",
    "contour_color_intermediate": "#9C6B3F",
    "contour_color_index": "#7A4F2A",
    "contour_color_label": "#7A4F2A",
    "contour_width_intermediate": "0.5",
    "contour_width_index": "1.2",
    "contour_background": "transparent",
    "contour_index_step": "5",
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_contour_db.py -v`
Expected: PASS（2 passed）

- [ ] **Step 6: 提交**

```bash
git add database.py tests/test_contour_db.py
git commit -m "feat(contour): add contour_tasks/contour_files tables + default configs"
```

---

## Task 2: contour_engine 纯函数（瓦片坐标 + 计曲线判定 + 配色）

**Files:**
- Create: `services/contour_engine.py`
- Test: `tests/test_contour_engine.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_contour_engine.py
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.contour_engine import (
    ORIGIN_SHIFT, deg2num, tile_bounds_meters, tiles_for_bbox_xyz,
    count_tiles, is_index_contour, ContourStyle,
)


def test_tile_bounds_meters_world_at_z0():
    xmin, ymin, xmax, ymax = tile_bounds_meters(0, 0, 0)
    assert abs(xmin + ORIGIN_SHIFT) < 1e-6
    assert abs(ymax - ORIGIN_SHIFT) < 1e-6
    assert abs(xmax - ORIGIN_SHIFT) < 1e-6
    assert abs(ymin + ORIGIN_SHIFT) < 1e-6


def test_deg2num_center_z1():
    # 经纬度 (0,0) 在 z=1 落在 (1,1)
    assert deg2num(0.0, 0.0, 1) == (1, 1)


def test_tiles_for_bbox_xyz_single_small_area():
    # 北京一个很小的框，z=12 至少 1 个瓦片，且都在合法范围
    tiles = tiles_for_bbox_xyz(north=39.95, south=39.90, east=116.45, west=116.40, zoom=12)
    assert len(tiles) >= 1
    n = 2 ** 12
    for (x, y) in tiles:
        assert 0 <= x < n and 0 <= y < n


def test_count_tiles_monotonic():
    a = count_tiles(39.95, 39.90, 116.45, 116.40, 12, 12)
    b = count_tiles(39.95, 39.90, 116.45, 116.40, 12, 14)
    assert b > a


def test_is_index_contour():
    # interval=50, index_step=5 -> 每 250m 一条计曲线
    assert is_index_contour(500, 50, 5) is True
    assert is_index_contour(250, 50, 5) is True
    assert is_index_contour(550, 50, 5) is False
    assert is_index_contour(300, 50, 5) is False


def test_contour_style_from_config():
    cfg = {
        "contour_color_intermediate": "#111111",
        "contour_color_index": "#222222",
        "contour_color_label": "#333333",
        "contour_width_intermediate": "0.7",
        "contour_width_index": "1.5",
        "contour_background": "transparent",
        "contour_index_step": "5",
    }

    class FakeConfig:
        def get(self, k, default=None):
            return cfg.get(k, default)

    style = ContourStyle.from_config(FakeConfig())
    assert style.color_index == "#222222"
    assert style.width_intermediate == 0.7
    assert style.index_step == 5
    assert style.background == "transparent"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_contour_engine.py -v`
Expected: FAIL（`ModuleNotFoundError: services.contour_engine`）

- [ ] **Step 3: 实现 `services/contour_engine.py`（纯函数部分）**

```python
"""
Contour rendering engine.

Pure helpers (tile math, classification, style) have no heavy deps and are unit
tested directly. The heavy raster->contour->PNG builder (build_contour_tiles)
imports GDAL/numpy/matplotlib lazily inside the function body so this module is
import-safe without them (mirrors services/terrain_tiling/dem_task_tiler.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

EARTH_RADIUS = 6378137.0
ORIGIN_SHIFT = math.pi * EARTH_RADIUS  # 20037508.342789244
WEB_MERCATOR_MAX_LAT = 85.0511


def lonlat_to_meters(lon: float, lat: float) -> Tuple[float, float]:
    x = math.radians(lon) * EARTH_RADIUS
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * EARTH_RADIUS
    return x, y


def meters_to_lonlat(x: float, y: float) -> Tuple[float, float]:
    lon = math.degrees(x / EARTH_RADIUS)
    lat = math.degrees(2 * math.atan(math.exp(y / EARTH_RADIUS)) - math.pi / 2)
    return lon, lat


def deg2num(lat_deg: float, lon_deg: float, zoom: int) -> Tuple[int, int]:
    lat = max(min(lat_deg, WEB_MERCATOR_MAX_LAT), -WEB_MERCATOR_MAX_LAT)
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    x = min(max(x, 0), n - 1)
    y = min(max(y, 0), n - 1)
    return x, y


def tile_bounds_meters(z: int, x: int, y: int) -> Tuple[float, float, float, float]:
    n = 2 ** z
    tile_size = (2 * ORIGIN_SHIFT) / n
    xmin = -ORIGIN_SHIFT + x * tile_size
    xmax = xmin + tile_size
    ymax = ORIGIN_SHIFT - y * tile_size
    ymin = ymax - tile_size
    return xmin, ymin, xmax, ymax


def tiles_for_bbox_xyz(north: float, south: float, east: float, west: float, zoom: int) -> List[Tuple[int, int]]:
    x0, y0 = deg2num(north, west, zoom)
    x1, y1 = deg2num(south, east, zoom)
    xmin, xmax = min(x0, x1), max(x0, x1)
    ymin, ymax = min(y0, y1), max(y0, y1)
    return [(x, y) for x in range(xmin, xmax + 1) for y in range(ymin, ymax + 1)]


def count_tiles(north: float, south: float, east: float, west: float, zoom_min: int, zoom_max: int) -> int:
    return sum(len(tiles_for_bbox_xyz(north, south, east, west, z)) for z in range(zoom_min, zoom_max + 1))


def is_index_contour(elevation: float, interval: float, index_step: int) -> bool:
    if index_step <= 0 or interval <= 0:
        return False
    major = interval * index_step
    ratio = elevation / major
    return abs(ratio - round(ratio)) < 1e-6


@dataclass(frozen=True)
class ContourStyle:
    color_intermediate: str = "#9C6B3F"
    color_index: str = "#7A4F2A"
    color_label: str = "#7A4F2A"
    width_intermediate: float = 0.5
    width_index: float = 1.2
    background: str = "transparent"
    index_step: int = 5
    label_size: float = 6.0

    @classmethod
    def from_config(cls, config) -> "ContourStyle":
        def _f(key, default):
            try:
                return float(config.get(key, str(default)))
            except (TypeError, ValueError):
                return float(default)

        def _i(key, default):
            try:
                return int(float(config.get(key, str(default))))
            except (TypeError, ValueError):
                return int(default)

        return cls(
            color_intermediate=config.get("contour_color_intermediate", "#9C6B3F"),
            color_index=config.get("contour_color_index", "#7A4F2A"),
            color_label=config.get("contour_color_label", "#7A4F2A"),
            width_intermediate=_f("contour_width_intermediate", 0.5),
            width_index=_f("contour_width_index", 1.2),
            background=config.get("contour_background", "transparent"),
            index_step=_i("contour_index_step", 5),
            label_size=_f("contour_label_size", 6.0),
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_contour_engine.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add services/contour_engine.py tests/test_contour_engine.py
git commit -m "feat(contour): contour_engine pure helpers (tile math, classify, style)"
```

---

## Task 3: contour_engine 重型渲染器 `build_contour_tiles`（GDAL warp + matplotlib contour）

**Files:**
- Modify: `services/contour_engine.py`（追加 `build_contour_tiles`）
- Modify: `requirements.txt`（新增 `matplotlib`）
- Test: `tests/test_contour_engine_render.py`（GDAL/matplotlib 缺失则 skip）

- [ ] **Step 1: 写测试（条件 skip，真实渲染冒烟）**

```python
# tests/test_contour_engine_render.py
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

gdal = pytest.importorskip("osgeo.gdal")
np = pytest.importorskip("numpy")
pytest.importorskip("matplotlib")

from services.contour_engine import build_contour_tiles, ContourStyle


def _make_dem(path, lon0=116.0, lat0=39.0):
    # 1 度范围、60x60 的斜坡 DEM（EPSG:4326），高程 0..600m，确保有等高线穿过
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(path), 60, 60, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((lon0, 1.0 / 60, 0, lat0 + 1.0, 0, -1.0 / 60))
    srs = gdal.osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    arr = np.tile(np.linspace(0, 600, 60).astype("float32"), (60, 1))
    ds.GetRasterBand(1).WriteArray(arr)
    ds.FlushCache()
    ds = None


def test_build_contour_tiles_emits_png(tmp_path):
    dem = tmp_path / "ASTGTMV003_N39E116_dem.tif"
    _make_dem(dem)
    out = tmp_path / "contour_tiles"

    counts = build_contour_tiles(
        dem_tifs=[dem], out_dir=out, interval=50,
        zoom_min=10, zoom_max=11, style=ContourStyle(),
    )

    assert counts["total"] >= 1
    assert counts["rendered"] >= 1
    pngs = list(out.rglob("*.png"))
    assert len(pngs) >= 1
    # 256x256 PNG
    assert pngs[0].stat().st_size > 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_contour_engine_render.py -v`
Expected: FAIL（`ImportError: cannot import name 'build_contour_tiles'`）；若本机无 GDAL/matplotlib 则 SKIP（可接受，CI/有 GDAL 环境会真正跑）

- [ ] **Step 3: 实现 — 在 `services/contour_engine.py` 末尾追加 `build_contour_tiles`**

```python
def build_contour_tiles(
    dem_tifs,
    out_dir,
    interval: float,
    zoom_min: int,
    zoom_max: int,
    style: ContourStyle,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    stop_flag=None,
) -> dict:
    """
    Warp DEM(s) to EPSG:3857, then per slippy tile read the window, run
    matplotlib contour (minor + major + labels) and save a transparent 256x256 PNG.
    Heavy deps imported lazily so the module stays import-safe without them.
    """
    from osgeo import gdal
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    gdal.UseExceptions()
    out_dir = Path(out_dir)
    dem_paths = [str(p) for p in dem_tifs]
    counts = {"total": 0, "rendered": 0, "failed": 0}
    if not dem_paths:
        return counts

    # 1) merge -> VRT -> warp to 3857
    vrt = gdal.BuildVRT("", dem_paths)
    warped = gdal.Warp("", vrt, format="MEM", dstSRS="EPSG:3857",
                       resampleAlg="bilinear", dstNodata=-9999)
    vrt = None
    band = warped.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    originX, pxW, _, originY, _, pxH = warped.GetGeoTransform()
    nx, ny = warped.RasterXSize, warped.RasterYSize

    cov_west, cov_south = meters_to_lonlat(originX, originY + ny * pxH)
    cov_east, cov_north = meters_to_lonlat(originX + nx * pxW, originY)

    tile_list = []
    for z in range(zoom_min, zoom_max + 1):
        for (tx, ty) in tiles_for_bbox_xyz(cov_north, cov_south, cov_east, cov_west, z):
            tile_list.append((z, tx, ty))
    counts["total"] = len(tile_list)

    transparent = (style.background or "transparent").strip().lower() == "transparent"
    facecolor = "none" if transparent else style.background

    for (z, tx, ty) in tile_list:
        if stop_flag is not None and stop_flag.is_set():
            break
        xmin, ymin, xmax, ymax = tile_bounds_meters(z, tx, ty)

        col0 = int(math.floor((xmin - originX) / pxW)) - 1
        col1 = int(math.ceil((xmax - originX) / pxW)) + 1
        row0 = int(math.floor((ymax - originY) / pxH)) - 1
        row1 = int(math.ceil((ymin - originY) / pxH)) + 1
        col0 = max(col0, 0); row0 = max(row0, 0)
        col1 = min(col1, nx); row1 = min(row1, ny)
        if col1 <= col0 or row1 <= row0:
            if progress_cb is not None:
                progress_cb(counts["rendered"] + counts["failed"], counts["total"])
            continue

        win_x, win_y = col1 - col0, row1 - row0
        arr = band.ReadAsArray(col0, row0, win_x, win_y).astype("float64")
        if nodata is not None:
            arr = np.where(arr == nodata, np.nan, arr)
        if np.all(np.isnan(arr)):
            if progress_cb is not None:
                progress_cb(counts["rendered"] + counts["failed"], counts["total"])
            continue
        zmin = float(np.nanmin(arr)); zmax = float(np.nanmax(arr))
        if not math.isfinite(zmin) or not math.isfinite(zmax) or (zmax - zmin) < 1e-6:
            if progress_cb is not None:
                progress_cb(counts["rendered"] + counts["failed"], counts["total"])
            continue

        xs = originX + (col0 + np.arange(win_x) + 0.5) * pxW
        ys = originY + (row0 + np.arange(win_y) + 0.5) * pxH
        X, Y = np.meshgrid(xs, ys)

        lo = math.floor(zmin / interval) * interval
        hi = math.ceil(zmax / interval) * interval
        levels = [lo + i * interval for i in range(int(round((hi - lo) / interval)) + 1)]
        minor = [lv for lv in levels if not is_index_contour(lv, interval, style.index_step)]
        major = [lv for lv in levels if is_index_contour(lv, interval, style.index_step)]
        if not minor and not major:
            if progress_cb is not None:
                progress_cb(counts["rendered"] + counts["failed"], counts["total"])
            continue

        fig = plt.figure(figsize=(2.56, 2.56), dpi=100)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_axis_off()
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        try:
            if minor:
                ax.contour(X, Y, arr, levels=minor, colors=style.color_intermediate,
                           linewidths=style.width_intermediate)
            if major:
                cs = ax.contour(X, Y, arr, levels=major, colors=style.color_index,
                                linewidths=style.width_index)
                ax.clabel(cs, fmt="%d", fontsize=style.label_size, colors=style.color_label)
            tile_path = out_dir / str(z) / str(tx) / f"{ty}.png"
            tile_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(str(tile_path), dpi=100, transparent=transparent,
                        facecolor=facecolor, pad_inches=0)
            counts["rendered"] += 1
        except Exception:
            counts["failed"] += 1
        finally:
            plt.close(fig)

        if progress_cb is not None:
            progress_cb(counts["rendered"] + counts["failed"], counts["total"])

    warped = None
    return counts
```

- [ ] **Step 4: 实现 — `requirements.txt` 追加一行**

```
matplotlib
```

- [ ] **Step 5: 跑测试确认通过（有 GDAL+matplotlib 的环境）**

Run: `uv run pytest tests/test_contour_engine_render.py -v`
Expected: PASS（1 passed）或 SKIP（无 GDAL/matplotlib 时）。若 PASS，`out` 下应出现 `{z}/{x}/{y}.png`。

- [ ] **Step 6: 提交**

```bash
git add services/contour_engine.py requirements.txt tests/test_contour_engine_render.py
git commit -m "feat(contour): build_contour_tiles renderer (GDAL warp + matplotlib contour/clabel)"
```

---

## Task 4: contour_task_tiler（可注入的渲染封装，对齐 dem_task_tiler）

**Files:**
- Create: `services/contour_task_tiler.py`
- Test: `tests/test_contour_task_tiler.py`

- [ ] **Step 1: 写失败测试（注入 fake，不需 GDAL）**

```python
# tests/test_contour_task_tiler.py
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.contour_engine import ContourStyle
from services.contour_task_tiler import (
    ContourParams, contour_output_dir_for_task, tile_contour_task_dir,
)


def test_contour_output_dir_for_task(tmp_path: Path):
    out = contour_output_dir_for_task(str(tmp_path), 7)
    assert out == tmp_path / "contour_task_7" / "contour_tiles"


def test_tile_contour_task_dir_injects_and_filters_dem(tmp_path: Path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "ASTGTMV003_N39E116_dem.tif").write_text("", encoding="utf-8")
    (task_dir / "ASTGTMV003_N39E116_num.tif").write_text("", encoding="utf-8")
    out_dir = tmp_path / "out"

    seen = {}

    def fake_build(dem_tifs, out_dir, interval, zoom_min, zoom_max, style,
                   progress_cb=None, stop_flag=None):
        seen["dem_tifs"] = list(dem_tifs)
        seen["interval"] = interval
        seen["zooms"] = (zoom_min, zoom_max)
        return {"total": 3, "rendered": 3, "failed": 0}

    params = ContourParams(interval=50, zoom_min=12, zoom_max=13, style=ContourStyle())
    counts = tile_contour_task_dir(task_dir, out_dir, params, build_contour_fn=fake_build)

    assert counts == {"total": 3, "rendered": 3, "failed": 0}
    # *_num.tif 必须被过滤掉
    assert seen["dem_tifs"] == [task_dir / "ASTGTMV003_N39E116_dem.tif"]
    assert seen["interval"] == 50
    assert seen["zooms"] == (12, 13)


def test_tile_contour_task_dir_no_dem_returns_zero(tmp_path: Path):
    task_dir = tmp_path / "empty"
    task_dir.mkdir()
    out_dir = tmp_path / "out"
    params = ContourParams(interval=50, zoom_min=12, zoom_max=12, style=ContourStyle())

    def fake_build(*a, **k):
        raise AssertionError("should not be called without DEM")

    counts = tile_contour_task_dir(task_dir, out_dir, params, build_contour_fn=fake_build)
    assert counts == {"total": 0, "rendered": 0, "failed": 0}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_contour_task_tiler.py -v`
Expected: FAIL（`ModuleNotFoundError: services.contour_task_tiler`）

- [ ] **Step 3: 实现 `services/contour_task_tiler.py`**

```python
"""
Contour task tiler.

Thin wrapper around contour_engine.build_contour_tiles with a lazy default so
tests can inject build_contour_fn=<fake> without GDAL/matplotlib (mirrors
services/terrain_tiling/dem_task_tiler.py). DEM listing reuses the existing
vrt_builder.list_dem_tifs (which filters *_num.tif).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from services.contour_engine import ContourStyle
from services.terrain_tiling.vrt_builder import list_dem_tifs


@dataclass(frozen=True)
class ContourParams:
    interval: float
    zoom_min: int
    zoom_max: int
    style: ContourStyle


def contour_output_dir_for_task(task_output_path: str, task_id: int) -> Path:
    return Path(task_output_path) / f"contour_task_{task_id}" / "contour_tiles"


def tile_contour_task_dir(
    task_dir,
    out_dir,
    params: ContourParams,
    build_contour_fn: Optional[Callable] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    stop_flag=None,
) -> dict:
    task_dir = Path(task_dir)
    out_dir = Path(out_dir)

    dem_tifs = list_dem_tifs(task_dir)
    if not dem_tifs:
        return {"total": 0, "rendered": 0, "failed": 0}

    if build_contour_fn is None:
        from services.contour_engine import build_contour_tiles as build_contour_fn

    return build_contour_fn(
        dem_tifs=dem_tifs,
        out_dir=out_dir,
        interval=params.interval,
        zoom_min=params.zoom_min,
        zoom_max=params.zoom_max,
        style=params.style,
        progress_cb=progress_cb,
        stop_flag=stop_flag,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_contour_task_tiler.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add services/contour_task_tiler.py tests/test_contour_task_tiler.py
git commit -m "feat(contour): injectable contour_task_tiler wrapper"
```

---

## Task 5: ContourTaskManager（任务生命周期 + 一站式 下载→渲染）

**Files:**
- Create: `services/contour_task_manager.py`
- Test: `tests/test_contour_task_manager.py`

- [ ] **Step 1: 写失败测试（只测 create_task 的 granule 计算与建行，不触网）**

```python
# tests/test_contour_task_manager.py
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("database")
    db.init_database()
    ctm_mod = importlib.import_module("services.contour_task_manager")
    return db, ctm_mod


def test_create_task_computes_granules_and_rows(monkeypatch, tmp_path):
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)

    task_id = mgr.create_task({
        "name": "bj",
        "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
        "contour_interval": 50, "zoom_min": 12, "zoom_max": 14,
    })

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        task = cur.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
        files = cur.execute("SELECT granule_id FROM contour_files WHERE task_id=?", (task_id,)).fetchall()
    finally:
        conn.close()

    assert task["contour_interval"] == 50
    assert task["zoom_min"] == 12 and task["zoom_max"] == 14
    assert task["status"] == "pending"
    # bbox (1,0,1,0) -> 单个 1x1 度瓦片 N00E000 -> 单个 *_dem.tif
    assert task["total_files"] == 1
    assert [f["granule_id"] for f in files] == ["ASTGTMV003_N00E000_dem.tif"]


def test_create_task_defaults_interval_from_config(monkeypatch, tmp_path):
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = mgr.create_task({
        "name": "x", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
        "zoom_min": 12, "zoom_max": 13,
    })
    conn = db.get_connection()
    try:
        task = conn.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert task["contour_interval"] == 50  # 来自 contour_default_interval
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_contour_task_manager.py -v`
Expected: FAIL（`ModuleNotFoundError: services.contour_task_manager`）

- [ ] **Step 3: 实现 `services/contour_task_manager.py`（完整文件）**

```python
"""
Contour Task Manager

One-stop pipeline: download ASTER DEM granules (reuses DemDownloadEngine), then
render brown contour PNG tiles (contour_task_tiler). Lifecycle/threading mirror
DemTaskManager (active_tasks + stop_flags + orphan recovery).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Config
from database import get_connection
from services.config_manager import ConfigManager
from services.dem_download_engine import DemDownloadEngine
from services.dem_granules import tiles_for_bbox, astgtm_v3_granules_for_tile

logger = logging.getLogger(__name__)


def _status_count_deltas(old_status: Optional[str], new_status: str) -> tuple[int, int]:
    downloaded_delta = int(new_status == "completed") - int(old_status == "completed")
    failed_delta = int(new_status == "failed") - int(old_status == "failed")
    return downloaded_delta, failed_delta


class ContourTaskManager:
    def __init__(self, socketio=None):
        self.socketio = socketio
        self.config = ConfigManager()
        self.engine = DemDownloadEngine()
        self.active_tasks: Dict[int, threading.Thread] = {}
        self.stop_flags: Dict[int, threading.Event] = {}
        self._state_lock = threading.Lock()
        self._recover_orphan_running_tasks()

    def _recover_orphan_running_tasks(self) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM contour_tasks WHERE status = 'running'")
            task_ids = [row["id"] for row in cur.fetchall()]
            if task_ids:
                cur.executemany(
                    "UPDATE contour_tasks SET status='paused' WHERE id=? AND status='running'",
                    [(tid,) for tid in task_ids],
                )
                conn.commit()
                logger.warning(f"Recovered orphan contour tasks (paused): {task_ids}")
        except Exception as e:
            logger.error(f"Failed to recover contour orphan tasks: {e}")
            conn.rollback()
        finally:
            conn.close()

    def create_task(self, params: dict) -> int:
        name = params.get("name") or "Contour Task"
        north = float(params["north"]); south = float(params["south"])
        east = float(params["east"]); west = float(params["west"])
        dataset = params.get("dataset") or "ASTGTM.003"
        if dataset != "ASTGTM.003":
            raise ValueError(f"Unsupported dataset: {dataset}")

        interval_raw = params.get("contour_interval")
        if interval_raw in (None, ""):
            interval_raw = self.config.get("contour_default_interval", "50")
        interval = float(interval_raw)
        if interval <= 0:
            raise ValueError(f"contour_interval must be > 0, got {interval}")

        zoom_min = int(params.get("zoom_min", 12))
        zoom_max = int(params.get("zoom_max", 14))
        if zoom_min > zoom_max:
            raise ValueError(f"zoom_min ({zoom_min}) must be <= zoom_max ({zoom_max})")

        output_path = params.get("output_path") or str(Path(Config.DOWNLOADS_DIR) / "dem")

        tiles = tiles_for_bbox(north=north, south=south, east=east, west=west)
        granules: List[str] = []
        for t in tiles:
            granules.extend(astgtm_v3_granules_for_tile(t, include_num=False, include_swb=False))
        total_files = len(granules)

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO contour_tasks (
                    name, status, north, south, east, west, dataset,
                    contour_interval, zoom_min, zoom_max, output_path,
                    total_files, downloaded_files, failed_files,
                    total_tiles, rendered_tiles, failed_tiles
                )
                VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0)
                """,
                (name, north, south, east, west, dataset,
                 interval, zoom_min, zoom_max, output_path, total_files),
            )
            task_id = cur.lastrowid
            cur.executemany(
                "INSERT INTO contour_files (task_id, granule_id, status, retry_count) VALUES (?, ?, 'pending', 0)",
                [(task_id, g) for g in granules],
            )
            conn.commit()
            return task_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start_task(self, task_id: int) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            with self._state_lock:
                active = self.active_tasks.get(task_id)
                if active and active.is_alive():
                    raise ValueError(f"Contour task {task_id} is already running")
                cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Contour task {task_id} not found")
                if row["status"] not in ("pending", "paused"):
                    raise ValueError(f"Cannot start contour task {task_id} with status '{row['status']}'")
                cur.execute(
                    "UPDATE contour_tasks SET status='running', started_at=? WHERE id=? AND status IN ('pending','paused')",
                    (datetime.now(), task_id),
                )
                if cur.rowcount != 1:
                    raise ValueError(f"Contour task {task_id} could not be started (status changed)")
                conn.commit()
                stop_flag = threading.Event()
                self.stop_flags[task_id] = stop_flag
                th = threading.Thread(target=self._run_task, args=(task_id, stop_flag),
                                      daemon=True, name=f"ContourTask-{task_id}")
                self.active_tasks[task_id] = th
            th.start()
        finally:
            conn.close()

    def pause_task(self, task_id: int) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE contour_tasks SET status='paused' WHERE id=? AND status='running'", (task_id,))
            if cur.rowcount == 0:
                row = cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
                if not row:
                    raise ValueError(f"Contour task {task_id} not found")
                raise ValueError(f"Cannot pause contour task {task_id} with status '{row['status']}'")
            conn.commit()
            with self._state_lock:
                if task_id in self.stop_flags:
                    self.stop_flags[task_id].set()
        finally:
            conn.close()

    def resume_task(self, task_id: int) -> None:
        self.start_task(task_id)

    def cancel_task(self, task_id: int) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE contour_tasks SET status='cancelled' WHERE id=? AND status!='cancelled'", (task_id,))
            if cur.rowcount == 0:
                row = cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
                if not row:
                    raise ValueError(f"Contour task {task_id} not found")
            conn.commit()
            with self._state_lock:
                if task_id in self.stop_flags:
                    self.stop_flags[task_id].set()
        finally:
            conn.close()

    def get_task(self, task_id: int) -> Dict[str, Any]:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError(f"Contour task {task_id} not found")
            return dict(row)
        finally:
            conn.close()

    def list_tasks(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = min(int(limit or 100), 100)
        conn = get_connection()
        try:
            rows = conn.execute("SELECT * FROM contour_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _update_render_counts(self, task_id: int, rendered: int, total: int) -> None:
        conn = get_connection()
        try:
            conn.execute("UPDATE contour_tasks SET rendered_tiles=?, total_tiles=? WHERE id=?",
                         (rendered, total, task_id))
            conn.commit()
        finally:
            conn.close()

    def _run_task(self, task_id: int, stop_flag: Optional[threading.Event] = None) -> None:
        try:
            asyncio.run(self._execute(task_id, stop_flag))
        except Exception as e:
            logger.error(f"Contour task {task_id} thread failed: {e}")
        finally:
            with self._state_lock:
                if self.active_tasks.get(task_id) is threading.current_thread():
                    self.active_tasks.pop(task_id, None)
                if stop_flag is None or self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)

    async def _execute(self, task_id: int, stop_flag: Optional[threading.Event] = None) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            task = cur.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ValueError(f"Contour task {task_id} not found")

            dataset = task["dataset"]
            output_dir = Path(task["output_path"]) / f"contour_task_{task_id}"

            rows = cur.execute(
                "SELECT granule_id FROM contour_files WHERE task_id=? AND status IN ('pending','failed') ORDER BY granule_id",
                (task_id,),
            ).fetchall()
            granules = [r["granule_id"] for r in rows]

            stop_ev = asyncio.Event()
            if stop_flag and stop_flag.is_set():
                stop_ev.set()

            async def progress(granule_id: str, status: str, error: Optional[str], size_bytes: Optional[int]):
                tile_conn = get_connection()
                try:
                    c = tile_conn.cursor()
                    existing = c.execute("SELECT status FROM contour_files WHERE task_id=? AND granule_id=?",
                                         (task_id, granule_id)).fetchone()
                    old_status = existing["status"] if existing else None
                    c.execute(
                        "UPDATE contour_files SET status=?, error_message=?, size_bytes=?, local_path=? WHERE task_id=? AND granule_id=?",
                        (status, error, size_bytes, str(output_dir / granule_id), task_id, granule_id),
                    )
                    d_delta, f_delta = _status_count_deltas(old_status, status)
                    if d_delta or f_delta:
                        c.execute(
                            "UPDATE contour_tasks SET downloaded_files=MAX(downloaded_files+?,0), failed_files=MAX(failed_files+?,0) WHERE id=?",
                            (d_delta, f_delta, task_id),
                        )
                    tile_conn.commit()
                    trow = c.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
                    if trow and self.socketio:
                        payload = dict(trow)
                        payload["task_type"] = "contour"
                        payload["phase"] = "download"
                        self.socketio.emit("task_progress", payload)
                finally:
                    tile_conn.close()

            async def stop_watcher():
                while True:
                    if stop_flag and stop_flag.is_set():
                        stop_ev.set()
                        return
                    await asyncio.sleep(0.2)

            watcher = asyncio.create_task(stop_watcher())
            try:
                await self.engine.download_files(
                    dataset=dataset, granules=granules, output_dir=output_dir,
                    progress_callback=progress, stop_flag=stop_ev,
                )
            finally:
                watcher.cancel()

            if stop_ev.is_set():
                return

            current = cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
            if not current or current["status"] in ("cancelled", "paused"):
                return

            counts = cur.execute(
                """
                SELECT SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count,
                       SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count
                FROM contour_files WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
            failed_count = counts["failed_count"] or 0
            pending_count = counts["pending_count"] or 0
            if failed_count > 0 or pending_count > 0:
                msg = f"{failed_count} DEM file(s) failed, {pending_count} pending"
                cur.execute("UPDATE contour_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status='running'",
                            (msg, datetime.now(), task_id))
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "contour", "status": "failed", "error_message": msg})
                return

            # ---- One-stop render phase: DEM downloaded -> contour tiles ----
            from services.contour_task_tiler import ContourParams, tile_contour_task_dir
            from services.contour_engine import ContourStyle, count_tiles

            style = ContourStyle.from_config(self.config)
            interval = float(task["contour_interval"])
            zoom_min = int(task["zoom_min"]); zoom_max = int(task["zoom_max"])
            total_tiles = count_tiles(task["north"], task["south"], task["east"], task["west"], zoom_min, zoom_max)
            self._update_render_counts(task_id, rendered=0, total=total_tiles)

            def render_progress(done: int, total: int):
                self._update_render_counts(task_id, rendered=done, total=total)
                if self.socketio:
                    trow = self.get_task(task_id)
                    payload = dict(trow)
                    payload["task_type"] = "contour"
                    payload["phase"] = "render"
                    self.socketio.emit("task_progress", payload)

            params = ContourParams(interval=interval, zoom_min=zoom_min, zoom_max=zoom_max, style=style)
            render_counts = tile_contour_task_dir(
                task_dir=output_dir, out_dir=output_dir / "contour_tiles",
                params=params, progress_cb=render_progress, stop_flag=stop_flag,
            )

            if stop_flag and stop_flag.is_set():
                return
            if render_counts.get("rendered", 0) == 0:
                msg = "No contour tiles rendered (check DEM coverage / interval / zoom range)"
                cur.execute("UPDATE contour_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status='running'",
                            (msg, datetime.now(), task_id))
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "contour", "status": "failed", "error_message": msg})
                return

            cur.execute("UPDATE contour_tasks SET status='completed', completed_at=? WHERE id=? AND status='running'",
                        (datetime.now(), task_id))
            conn.commit()
            if cur.rowcount and self.socketio:
                self.socketio.emit("task_completed", {"task_id": task_id, "task_type": "contour", "status": "completed"})

        except Exception as e:
            try:
                cur = conn.cursor()
                cur.execute("UPDATE contour_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status NOT IN ('cancelled','paused')",
                            (str(e), datetime.now(), task_id))
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "contour", "status": "failed", "error_message": str(e)})
            except Exception as e2:
                logger.error(f"Failed to mark contour task {task_id} failed: {e2}")
            raise
        finally:
            conn.close()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_contour_task_manager.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add services/contour_task_manager.py tests/test_contour_task_manager.py
git commit -m "feat(contour): ContourTaskManager one-stop download->render pipeline"
```

---

## Task 6: routes/contour_api.py（CRUD + start/pause/resume/cancel）

**Files:**
- Create: `routes/contour_api.py`
- Test: `tests/test_contour_api.py`

- [ ] **Step 1: 写失败测试（POST 仅创建，不自动启动 → 无触网）**

```python
# tests/test_contour_api.py
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def test_create_contour_task_returns_201(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/contour/tasks", json={
        "name": "bj", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
        "contour_interval": 50, "zoom_min": 12, "zoom_max": 14,
    })
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["success"] is True
    assert isinstance(body["task_id"], int)


def test_create_contour_task_missing_field_400(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/contour/tasks", json={"name": "x"})
    assert resp.status_code == 400


def test_list_and_get_contour_task(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    tid = client.post("/api/contour/tasks", json={
        "name": "bj", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
        "contour_interval": 50, "zoom_min": 12, "zoom_max": 14,
    }).get_json()["task_id"]

    lst = client.get("/api/contour/tasks")
    assert lst.status_code == 200
    assert lst.get_json()["count"] >= 1

    got = client.get(f"/api/contour/tasks/{tid}")
    assert got.status_code == 200
    assert got.get_json()["task"]["id"] == tid
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_contour_api.py -v`
Expected: FAIL（404，路由不存在 — app 尚未注册 contour 蓝图，且 dem→contour 端点缺失）

- [ ] **Step 3: 实现 `routes/contour_api.py`**

```python
"""
Contour API routes — create/run contour tile download tasks.
"""

import logging
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

contour_api_bp = Blueprint("contour_api", __name__, url_prefix="/api/contour")

contour_task_manager = None


def init_contour_task_manager(tm):
    global contour_task_manager
    contour_task_manager = tm
    logger.info("Contour task manager initialized in contour API routes")


@contour_api_bp.route("/tasks", methods=["POST"])
def create_contour_task():
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        data = request.get_json() or {}
        required = ["name", "north", "south", "east", "west", "contour_interval", "zoom_min", "zoom_max"]
        missing = [k for k in required if k not in data]
        if missing:
            return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400
        task_id = contour_task_manager.create_task(data)
        return jsonify({"success": True, "task_id": task_id}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error creating contour task: {e}")
        return jsonify({"error": "Failed to create contour task"}), 500


@contour_api_bp.route("/tasks", methods=["GET"])
def list_contour_tasks():
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        limit = request.args.get("limit", 100, type=int)
        tasks = contour_task_manager.list_tasks(limit=limit)
        return jsonify({"success": True, "tasks": tasks, "count": len(tasks)})
    except Exception as e:
        logger.error(f"Error listing contour tasks: {e}")
        return jsonify({"error": "Failed to list contour tasks"}), 500


@contour_api_bp.route("/tasks/<int:task_id>", methods=["GET"])
def get_contour_task(task_id: int):
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        task = contour_task_manager.get_task(task_id)
        return jsonify({"success": True, "task": task})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error getting contour task {task_id}: {e}")
        return jsonify({"error": "Failed to get contour task"}), 500


@contour_api_bp.route("/tasks/<int:task_id>", methods=["DELETE"])
def delete_contour_task(task_id: int):
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        task = contour_task_manager.get_task(task_id)
        if task.get("status") == "running":
            return jsonify({"error": "Cannot delete running contour task. Pause or cancel it first."}), 400
        from database import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM contour_tasks WHERE id = ?", (task_id,))
            if cur.rowcount == 0:
                return jsonify({"error": f"Contour task {task_id} not found"}), 404
            conn.commit()
        finally:
            conn.close()
        return jsonify({"success": True, "message": f"Contour task {task_id} deleted"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error deleting contour task {task_id}: {e}")
        return jsonify({"error": "Failed to delete contour task"}), 500


@contour_api_bp.route("/tasks/<int:task_id>/start", methods=["POST"])
def start_contour_task(task_id: int):
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        contour_task_manager.start_task(task_id)
        return jsonify({"success": True, "message": f"Contour task {task_id} started"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error starting contour task {task_id}: {e}")
        return jsonify({"error": "Failed to start contour task"}), 500


@contour_api_bp.route("/tasks/<int:task_id>/pause", methods=["POST"])
def pause_contour_task(task_id: int):
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        contour_task_manager.pause_task(task_id)
        return jsonify({"success": True, "message": f"Contour task {task_id} paused"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error pausing contour task {task_id}: {e}")
        return jsonify({"error": "Failed to pause contour task"}), 500


@contour_api_bp.route("/tasks/<int:task_id>/resume", methods=["POST"])
def resume_contour_task(task_id: int):
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        contour_task_manager.resume_task(task_id)
        return jsonify({"success": True, "message": f"Contour task {task_id} resumed"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error resuming contour task {task_id}: {e}")
        return jsonify({"error": "Failed to resume contour task"}), 500


@contour_api_bp.route("/tasks/<int:task_id>/cancel", methods=["POST"])
def cancel_contour_task(task_id: int):
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        contour_task_manager.cancel_task(task_id)
        return jsonify({"success": True, "message": f"Contour task {task_id} cancelled"})
    except Exception as e:
        logger.error(f"Error cancelling contour task {task_id}: {e}")
        return jsonify({"error": "Failed to cancel contour task"}), 500
```

> 注：本任务的测试要 PASS 依赖 Task 8 完成 app 装配。先实现本文件，测试在 Task 8 后转绿；执行顺序上 Task 6→7→8 连续完成后统一验证。若用 subagent 严格按任务转绿，可把本测试的运行移到 Task 8 Step 4 一并跑。

- [ ] **Step 4: 提交**

```bash
git add routes/contour_api.py tests/test_contour_api.py
git commit -m "feat(contour): contour_api blueprint (CRUD + lifecycle)"
```

---

## Task 7: routes/contour_static.py（瓦片服务 + 路径穿越防护）

**Files:**
- Create: `routes/contour_static.py`
- Test: `tests/test_contour_static.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_contour_static.py
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def test_serve_contour_tile(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    tile = tmp_path / "downloads" / "dem" / "contour_task_1" / "contour_tiles" / "12" / "5" / "6.png"
    tile.parent.mkdir(parents=True, exist_ok=True)
    tile.write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG magic, enough to serve

    resp = client.get("/contour/1/12/5/6.png")
    assert resp.status_code == 200
    assert resp.data.startswith(b"\x89PNG")


def test_missing_tile_404(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/contour/1/0/0/0.png")
    assert resp.status_code == 404


def test_path_traversal_blocked(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/contour/1/..%2f..%2f..%2fetc%2fpasswd")
    assert resp.status_code in (400, 404)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_contour_static.py -v`
Expected: FAIL（404 路由不存在 — 蓝图未实现/未注册）

- [ ] **Step 3: 实现 `routes/contour_static.py`**

```python
"""
Static contour tile serving — XYZ raster PNG tiles for contour tasks.
"""

import logging
from pathlib import Path

from flask import Blueprint, abort, send_file

from config import Config
from routes.terrain_static import _resolve_safe_file

logger = logging.getLogger(__name__)

contour_static_bp = Blueprint("contour_static", __name__, url_prefix="/contour")


@contour_static_bp.route("/<int:task_id>/<path:subpath>", methods=["GET"])
def contour_tile_static(task_id: int, subpath: str):
    # Recompute from current DOWNLOADS_DIR (survives PyInstaller relocation; do not
    # trust an absolute path stored at task-creation time).
    base_dir = Path(Config.DOWNLOADS_DIR) / "dem" / f"contour_task_{task_id}" / "contour_tiles"
    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    return send_file(str(target))
```

- [ ] **Step 4: 提交**

```bash
git add routes/contour_static.py tests/test_contour_static.py
git commit -m "feat(contour): contour_static tile serving with traversal guard"
```

> 同 Task 6：测试转绿依赖 Task 8 注册蓝图。

---

## Task 8: 装配 — routes/__init__.py + app.py

**Files:**
- Modify: `routes/__init__.py`
- Modify: `app.py`
- Test: `tests/test_contour_wiring.py`（并在此统一跑 Task 6/7 的测试）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_contour_wiring.py
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    return importlib.import_module("app")


def test_contour_routes_registered(monkeypatch, tmp_path):
    app_mod = _load_app(monkeypatch, tmp_path)
    rules = {r.rule for r in app_mod.app.url_map.iter_rules()}
    assert "/api/contour/tasks" in rules
    assert any(r.startswith("/contour/") for r in rules)
    assert app_mod.contour_task_manager is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_contour_wiring.py -v`
Expected: FAIL（`/api/contour/tasks` 不在 rules；`app_mod.contour_task_manager` AttributeError）

- [ ] **Step 3: 实现 — `routes/__init__.py` 增加导出**

```python
from routes.main import main_bp
from routes.api import api_bp
from routes.dem_api import dem_api_bp
from routes.terrain_api import terrain_api_bp
from routes.terrain_static import terrain_static_bp
from routes.local_terrain_api import local_terrain_api_bp
from routes.contour_api import contour_api_bp
from routes.contour_static import contour_static_bp

__all__ = ['main_bp', 'api_bp', 'dem_api_bp', 'terrain_api_bp', 'terrain_static_bp',
           'local_terrain_api_bp', 'contour_api_bp', 'contour_static_bp']
```

- [ ] **Step 4: 实现 — `app.py` 装配（三处改动）**

`app.py` 顶部 import 区，把 `from routes import ...` 一行替换为包含新蓝图：

```python
from routes import (
    main_bp, api_bp, dem_api_bp, terrain_api_bp, terrain_static_bp,
    local_terrain_api_bp, contour_api_bp, contour_static_bp,
)
```

并在 `from routes.local_terrain_api import init_local_terrain_task_manager` 之后追加：

```python
from services.contour_task_manager import ContourTaskManager
from routes.contour_api import init_contour_task_manager
```

在 `local_terrain_task_manager` 注入之后（`logger.info("LocalTerrainTaskManager created and injected")` 之后）追加构造与注入：

```python
# Create ContourTaskManager and inject into contour API routes
contour_task_manager = ContourTaskManager(socketio=socketio)
init_contour_task_manager(contour_task_manager)
logger.info("ContourTaskManager created and injected")
```

在 `app.register_blueprint(local_terrain_api_bp)` 之后追加注册：

```python
app.register_blueprint(contour_api_bp)
logger.info("Contour API blueprint registered")

app.register_blueprint(contour_static_bp)
logger.info("Contour static blueprint registered")
```

- [ ] **Step 5: 跑测试确认通过（含 Task 6/7 现在转绿）**

Run: `uv run pytest tests/test_contour_wiring.py tests/test_contour_api.py tests/test_contour_static.py -v`
Expected: PASS（全绿）

- [ ] **Step 6: 提交**

```bash
git add routes/__init__.py app.py tests/test_contour_wiring.py
git commit -m "feat(contour): wire ContourTaskManager + register blueprints in app.py"
```

---

## Task 9: 前端 — 下载类型选项 + 等高距输入 + 提交 + 叠加预览

**Files:**
- Modify: `templates/index.html`
- Modify: `static/js/map.js`
- Test: `tests/test_index_has_contour_option.py`

> 执行前先 `Read` 这两个文件，照现有 `dem` / `local_terrain` 分支的写法插入。下列片段是「要新增的内容」与「插入位置」。

- [ ] **Step 1: 写失败测试（后端冒烟：首页含 contour 选项）**

```python
# tests/test_index_has_contour_option.py
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def test_index_has_contour_controls(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    html = client.get("/").get_data(as_text=True)
    assert 'value="contour"' in html
    assert 'id="contourOptions"' in html
    assert 'id="contourInterval"' in html
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_index_has_contour_option.py -v`
Expected: FAIL（断言找不到 `value="contour"`）

- [ ] **Step 3: 实现 — `templates/index.html`**

(a) 在 `#downloadType` 的 `<select>` 内，`local_terrain` 选项之后加一项：

```html
<option value="contour">等高线瓦片</option>
```

(b) 在 `#localTerrainOptions` 区块之后，加等高线专属区块（默认隐藏，结构对齐现有 `#demOptions`）：

```html
<div id="contourOptions" class="form-group" style="display:none;">
  <label for="contourInterval">等高距（米）</label>
  <input type="number" id="contourInterval" min="1" step="1" value="50">
  <small class="form-hint">小范围建议 50m，大范围建议 100m；可手动修改。</small>
</div>
```

- [ ] **Step 4: 实现 — `static/js/map.js`**

(a) 在 `initDownloadTypeToggle()` 内，仿照 `dem` / `local_terrain` 分支，加 `contour` 的显隐控制：当 `downloadType === 'contour'` 时显示 `#contourOptions`、隐藏 `#demOptions`/`#localTerrainOptions`、显示 zoom 范围控件、保存路径默认 `./downloads/dem`；其它类型时隐藏 `#contourOptions`。例如在该函数的类型判断里加入：

```javascript
const contourOptions = document.getElementById('contourOptions');
const isContour = downloadType === 'contour';
if (contourOptions) contourOptions.style.display = isContour ? 'block' : 'none';
// 等高线需要 zoom 范围 + 棕色等高线，路径走 dem 根目录（与瓦片服务路由一致）
if (isContour) {
  const out = document.getElementById('outputPath');
  if (out && !out.dataset.userEdited) out.value = './downloads/dem';
}
```

(b) 在表单提交处理里，仿照现有 `dem` 分支，加 `contour` 分支（先创建再启动 = 一站式一键）：

```javascript
} else if (downloadType === 'contour') {
  const interval = parseFloat(document.getElementById('contourInterval').value) || 50;
  const zMin = parseInt(document.getElementById('zoomMin').value, 10);
  const zMax = parseInt(document.getElementById('zoomMax').value, 10);
  // 渲染比下载现成瓦片慢得多，给个体量提醒
  const approx = estimateTileCount(currentBounds, zMin, zMax); // 复用现有瓦片估算；若无则按 zoom 跨度粗估
  if (approx > 20000 && !confirm(`预计渲染约 ${approx} 个等高线瓦片，可能较慢，确认继续？`)) return;

  const createResp = await fetch('/api/contour/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: taskName,
      north: currentBounds.north, south: currentBounds.south,
      east: currentBounds.east, west: currentBounds.west,
      contour_interval: interval, zoom_min: zMin, zoom_max: zMax,
      output_path: document.getElementById('outputPath').value || './downloads/dem',
    }),
  });
  const created = await createResp.json();
  if (!createResp.ok) { showNotification(created.error || '创建失败', 'error'); return; }
  await fetch(`/api/contour/tasks/${created.task_id}/start`, { method: 'POST' });
  showNotification('等高线任务已开始（自动下 DEM → 渲染瓦片）', 'success');
  initTasks();
  return;
}
```

> 若 `estimateTileCount` 在现有代码中名字不同，用现有的瓦片估算函数；没有就用 `(zMax-zMin+1)` 粗判，体量提醒非硬性。

(c) 叠加预览（首版必做）：在任务列表「已完成」的等高线任务上加「在地图上预览」按钮，点击执行叠加 + 显示/隐藏开关：

```javascript
let contourPreviewLayer = null;
function toggleContourPreview(taskId, zoomMax) {
  if (contourPreviewLayer) {
    map.removeLayer(contourPreviewLayer);
    contourPreviewLayer = null;
    return;
  }
  contourPreviewLayer = L.tileLayer(`/contour/${taskId}/{z}/{x}/{y}.png`, {
    opacity: 0.9,
    maxNativeZoom: zoomMax,
    tms: false,
  }).addTo(map);
}
```

把该函数绑定到等高线任务行的预览按钮（参考现有任务行渲染逻辑插入按钮，仅对 `task_type==='contour'` 且状态 `completed` 显示）。透明背景天然叠在现有底图上。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_index_has_contour_option.py -v`
Expected: PASS（1 passed）

- [ ] **Step 6: 手动验证（记录结果，不通过则回到上面步骤）**

```bash
DEBUG=0 uv run python app.py
```
浏览器开 `http://localhost:5000`：下载类型选「等高线瓦片」→ 出现等高距输入；框选小区域、zoom 12–13、提交 → 任务开始（先下 DEM 再渲染）；完成后点「在地图上预览」→ 棕色等高线叠加在底图上，再点一次隐藏。

- [ ] **Step 7: 提交**

```bash
git add templates/index.html static/js/map.js tests/test_index_has_contour_option.py
git commit -m "feat(contour): frontend download type + interval input + map preview overlay"
```

---

## Task 10: 集成冒烟 + 全量测试

**Files:**
- 无新增（验证整体）

- [ ] **Step 1: 跑全量 pytest**

Run: `uv run pytest tests/ -v`
Expected: 全绿（无 GDAL/matplotlib 的机器上 `test_contour_engine_render.py` 为 SKIP，其余 PASS）

- [ ] **Step 2: 真实一站式冒烟（需 Earthdata 凭据 + 网络；WSL2 先 `export HTTPS_PROXY=http://<Windows网关IP>:7892`）**

在配置页填好 `earthdata_username`/`earthdata_password`，启动服务，框选一个 1°×1° 内的小区域、zoom 12–13、等高距 50m、提交。期望：
- `downloads/dem/contour_task_<id>/ASTGTMV003_*_dem.tif` 出现（DEM 下载）。
- `downloads/dem/contour_task_<id>/contour_tiles/{z}/{x}/{y}.png` 出现（渲染产物，透明背景棕色线）。
- 任务状态 `completed`；前端「在地图上预览」可见棕色等高线叠加、计曲线带高程数字。

- [ ] **Step 3: 记录结论**

把冒烟结果（成功/失败 + 关键现象）写入提交信息或 PR 描述。若 GDAL 报 `_gdal_array` 缺失，按 `CLAUDE.md` 的 sdist 重建步骤修复后重跑。

- [ ] **Step 4: 收尾提交（如有零散修复）**

```bash
git add -A
git commit -m "test(contour): full-suite green + one-stop smoke verified"
```

---

## 自审清单（写计划后已核对）

- **Spec 覆盖**：DB(§5)→T1；渲染管线(§7)→T2/T3；可注入测试(§11)→T3/T4；manager(§6)→T5；API(§6/§10)→T6/T7；wiring(§4)→T8；前端+预览(§9/§2)→T9；matplotlib 依赖(§13)→T3；体量警告(§13)→T9。等高距 50/100 可填→T9 输入框 + T5 缺省回落 config。配色全可配 + 解放军军标棕默认→T1 配置项 + T2 `ContourStyle`。计曲线标高程→T3 `ax.clabel`。✓
- **占位符**：无 TODO/TBD；每个改码步骤含完整代码或精确插入片段。前端改存量文件给的是「新增片段 + 插入锚点」，已注明先 Read。
- **类型/命名一致**：`build_contour_tiles` 签名在 T3 定义、T4 调用一致；`ContourStyle`/`ContourParams`/`tile_contour_task_dir`/`count_tiles`/`is_index_contour` 跨任务同名；manager 方法名与 `contour_api` 调用一致（create_task/list_tasks/get_task/start/pause/resume/cancel）；瓦片服务路径 `DOWNLOADS_DIR/dem/contour_task_<id>/contour_tiles` 与 manager 落盘 `output_path(=DOWNLOADS_DIR/dem)/contour_task_<id>/contour_tiles` 对齐。✓
- **风险**：T6/T7 的测试依赖 T8 装配后转绿（已在任务内注明，T8 Step 5 统一验证）。
