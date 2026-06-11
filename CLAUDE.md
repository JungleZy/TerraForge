# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Flask + Socket.IO web app for downloading map data in two parallel pipelines:

1. **Google Maps tile downloader** — selects a bbox in the UI, downloads tiles from `mts0..mts3.googleapis.com`, optionally stitches them into GeoTIFFs via GDAL.
2. **DEM / terrain pipeline** — downloads ASTER GDEM v3 (ASTGTM.003) granules from NASA LP DAAC (Earthdata Login), then optionally tiles them into Cesium quantized-mesh terrain for serving to CesiumJS.

The two pipelines have **separate** managers, routes, DB tables, and frontend pages but share the SocketIO instance and `ConfigManager`.

## Commands

本项目使用 **uv** 管理虚拟环境（`.venv/` 已存在于项目根目录）。所有 Python 命令都通过 `uv run` 执行，无需手动 `source .venv/bin/activate`。

```bash
# Setup (one-time)
uv venv                                 # 如果 .venv 不存在
uv pip install -r requirements.txt
uv run python database.py               # 创建 data/map_downloader.db + 默认配置行

# Run dev server (Flask + Socket.IO on :5000)
uv run python app.py                    # DEBUG=1 by default → use_reloader=True
DEBUG=0 uv run python app.py            # disable reloader/debug

# Migrations (rarely needed — init_database() in database.py also runs idempotent ALTERs)
uv run python migrations/001_add_time_tracking.py

# Tests
uv run pytest tests/                                                # full suite
uv run pytest tests/test_terrain_api.py                             # single file
uv run pytest tests/test_dem_task_tiler.py::test_terrain_output_dir_for_task   # single test

# Build standalone executable (PyInstaller)
./build.sh           # Linux/macOS（脚本内部使用 uv run python -m PyInstaller）
build.bat            # Windows（脚本内部使用 uv run python -m PyInstaller）
# Output: dist/map-downloader/ — entry build.spec, GDAL hook hook-gdal.py
```

GDAL system libraries are required (`gdal-bin libgdal-dev` on Debian, `brew install gdal` on macOS). `requirements.txt` pins `GDAL==3.8.4` — keep in sync with the system `gdal-config --version`. 安装 GDAL Python 绑定时，`uv pip install gdal==$(gdal-config --version)` 通常比固定版本更稳。

**`ImportError: cannot import name '_gdal_array' from 'osgeo'`** — GDAL Python bindings were built without numpy support. The `gdal_array` C extension is only compiled when numpy is import-able at *sdist build time*. Triggered by `band.ReadAsArray()`/`WriteArray()` (used in `cesiumlab_terrain.py` and `download_engine.py`'s stitching path) and by `gdal.UseExceptions()`. Fix by rebuilding from sdist with numpy + setuptools in the venv:

```bash
uv pip install numpy setuptools wheel
UV_NO_CACHE=1 uv pip install --force-reinstall --no-build-isolation --no-binary :all: "GDAL==$(gdal-config --version)"
```

`UV_NO_CACHE=1` is required because uv caches sdist builds; without it, a previously broken build (made before numpy was in the env) is silently reused. Verify with `ls .venv/lib/python3.12/site-packages/osgeo/ | grep _gdal_array` — the `.so` must be present.

## Architecture

### Wiring (app.py)

`app.py` is the single composition root. It constructs `socketio`, `TaskManager`, `DemTaskManager` and **injects** them into blueprints via `init_task_manager(...)`, `init_dem_task_manager(...)`, `init_terrain_dem_task_manager(...)`. The blueprints rely on those module-level globals being set before any request arrives, so the registration order in `app.py` matters. Never instantiate a second `TaskManager` inside a route.

`Config.init_app()` runs both in `app.py` and inside `init_database()` — both call sites are idempotent (`mkdir(..., exist_ok=True)`). Tests monkey-patch `Config.DATABASE_PATH`/`DOWNLOADS_DIR`/`CACHE_DIR` **before** importing `app` so init writes into `tmp_path` (see `tests/test_terrain_api.py`).

### Two parallel task pipelines

| Concern             | Map tile pipeline                                   | DEM pipeline                                            |
| ------------------- | --------------------------------------------------- | ------------------------------------------------------- |
| Manager             | `services/task_manager.py` (`TaskManager`)          | `services/dem_task_manager.py` (`DemTaskManager`)       |
| Engine              | `services/download_engine.py` (aiohttp + GDAL)      | `services/dem_download_engine.py` (+ `earthdata_client`) |
| DB tables           | `tasks`, `task_tiles`, `task_time_records`          | `dem_tasks`, `dem_files`, `dem_terrain_jobs`            |
| REST blueprint      | `routes/api.py` → `/api/tasks/...`                  | `routes/dem_api.py` → `/api/dem/...`                    |
| Terrain tiling      | n/a                                                 | `routes/terrain_api.py` → `/api/terrain/dem/<id>/start` |
| Static tile serving | files only (no HTTP serving of cached tiles)        | `routes/terrain_static.py` → `/terrain/base/...` & `/terrain/dem/<id>/...` |

Both managers follow the same lifecycle pattern: `active_tasks: Dict[int, Thread]` + `stop_flags: Dict[int, threading.Event]`. Background threads run an asyncio loop internally; cancel/pause works by setting the event. Progress is pushed via `socketio.emit('task_progress', ...)`.

### Database conventions

- SQLite at `Config.DATABASE_PATH` (`data/map_downloader.db`). Connections use `sqlite3.Row` factory and `PRAGMA foreign_keys = ON`.
- Use `get_connection_context()` (context manager) for short reads; `get_connection()` + manual close inside managers.
- **Schema evolves inside `init_database()`** with `ALTER TABLE ... ADD COLUMN` wrapped in a try/except that swallows `duplicate column name`. New backward-compatible columns go there; the `migrations/` folder exists but is not the primary mechanism.
- The `config` table is seeded from `DEFAULT_CONFIGS` in `database.py` with `INSERT OR IGNORE`. Adding a new setting means appending there.

### Map tile specifics

- Style codes used in Google URLs (`lyrs=`): `m` roadmap, `s` satellite, `y` hybrid, `h` roads, `t` terrain. `MapStyle.from_shorthand` accepts both the legacy 1-char codes and the full names (`roadmap`, `satellite`, etc.); `STYLE_MAP` in `task_manager.py` maps full → short.
- Tiles are cached at `cache/<style>/<zoom>/<x>/<y>.png`. The cache is **shared across tasks** — `Tile.cache_path()` keys only on style + coords. Don't add task-id segments.
- `WEB_MERCATOR_MAX_LAT = 85.0511`, zoom is clamped to `0..21`. `WARN_TILES_THRESHOLD = 100000` triggers a UI warning.

### DEM / terrain specifics

- Only dataset supported: `ASTGTM.003` (1°×1° granules named `ASTGTMV003_{N|S}LL{E|W}LLL_dem.tif`). See `services/dem_granules.py`.
- Earthdata Login credentials live in the `config` table (`earthdata_username`, `earthdata_password`). `EarthdataClient` does a manual URS OAuth redirect dance — do not "harden" it (per inline note).
- Terrain tiling layout:
  - DEM granules: `downloads/dem/dem_task_<id>/*_dem.tif` (`*_num.tif` is intentionally filtered out by `list_dem_tifs`)
  - Output tiles: `downloads/dem/dem_task_<id>/terrain_tiles/{z}/{x}/{y}.terrain` + `layer.json`
  - Global base (offline-built, low-zoom planet coverage): `downloads/terrain/base_z8/` served at `/terrain/base/...`
  - Local DEM tiles `layer.json` is patched (`patch_layer_json_parent`) to carry `parentUrl` pointing at the base, so CesiumJS cascades automatically (see `docs/terrain/cesiumjs-loading.md`).
- The tiler is `services/terrain_tiling/cesiumlab_terrain.py` — a vendored copy of CesiumLab 4.0.17's quantized-mesh builder. It's used as a library (`build_terrain(...)`) by `dem_task_tiler.tile_dem_task_dir`. The import is **lazy** so tests can inject a `build_terrain_fn=` stub without needing numpy/GDAL at import time.
- `routes/terrain_static.py` enforces path-traversal safety: every served file must resolve under `Config.DOWNLOADS_DIR`. Don't bypass `_resolve_safe_file`.

### Frozen / PyInstaller mode

Both `app.py` and `config.py` branch on `getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')`:

- Templates/static come from `sys._MEIPASS` (PyInstaller's read-only bundle).
- `Config.BASE_DIR` becomes `Path(sys.executable).parent` so `data/`, `downloads/`, `cache/` live next to the executable, not inside the bundle. Anything writing to disk must go through `Config.*_DIR` to stay portable across frozen vs source runs.
- `build.spec` collects `flask_socketio`, `socketio`, `engineio`, `aiohttp`, `osgeo.*`, etc., and copies the GDAL data dir per platform. `hook-gdal.py` is the runtime hook.

### Testing patterns to follow

- `sys.path.insert(0, ...)` at top of test files (no `conftest.py`/no installed package).
- For anything that imports `app` or touches the DB: monkey-patch `Config.DATABASE_PATH`/`DOWNLOADS_DIR`/`CACHE_DIR` **first**, then `sys.modules.pop("app", None)` and reimport — `init_database()` runs at import time.
- For terrain tiler tests, pass `build_terrain_fn=<fake>` to `tile_dem_task_dir` instead of installing numpy/GDAL — the production code has a lazy-import hook for exactly this.
