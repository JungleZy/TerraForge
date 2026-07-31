# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Flask + Socket.IO web app for acquiring map data in four parallel pipelines:

1. **Google Maps tile downloader** — selects a bbox in the UI, downloads tiles from `mts0..mts3.googleapis.com`, optionally stitches them into GeoTIFFs via GDAL.
2. **DEM / terrain pipeline** — downloads ASTER GDEM v3 (ASTGTM.003) granules from NASA LP DAAC (Earthdata Login), then optionally tiles them into Cesium quantized-mesh terrain for serving to CesiumJS.
3. **Local terrain pipeline** — tiles user-uploaded GeoTIFFs into Cesium quantized-mesh terrain (reuses the DEM pipeline's tiler; nothing is downloaded).
4. **Contour pipeline** — downloads the same ASTER GDEM granules for a bbox, then renders contour-map XYZ PNG tiles (configurable interval, shading, water mask).

The four pipelines have **separate** managers, routes, DB tables, and frontend pages but share the SocketIO instance and `ConfigManager`.

## Commands

本项目使用 **uv** 管理虚拟环境（`.venv/` 已存在于项目根目录）。所有 Python 命令都通过 `uv run` 执行，无需手动 `source .venv/bin/activate`。

```bash
# Setup (one-time)
uv venv                                 # 如果 .venv 不存在
uv pip install -r requirements.txt
uv run python -c "from core.database import init_database; init_database()"               # 创建 data/map_downloader.db + 默认配置行

# Run dev server (Flask + Socket.IO on :5000)
uv run python app.py                    # 源码运行 DEBUG=1 by default → use_reloader=True（打包 exe 默认 DEBUG=0）
DEBUG=0 uv run python app.py            # disable reloader/debug

# Migrations — 无独立迁移脚本：迁移已内联在 core/database.py 的 init_database()
# （幂等 ALTER + PRAGMA user_version 一次性标记），启动时自动执行，无需手动运行。

# Tests
uv run pytest tests/                                                # full suite
uv run pytest tests/test_terrain_api.py                             # single file
uv run pytest tests/test_dem_task_tiler.py::test_terrain_output_dir_for_task   # single test

# Build standalone executable (Nuitka)
./build.sh           # Linux/macOS（脚本内部使用 uv run python nuitka_build.py）
build.bat            # Windows（脚本内部使用 uv run python nuitka_build.py）
# Output: dist/terraforge/ — entry nuitka_build.py, GDAL/PROJ 环境设置在 core/bundle.py
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

`app.py` is the single composition root. `create_app()` constructs `socketio`, `TaskManager`, `DemTaskManager`, `LocalTerrainTaskManager`, `ContourTaskManager` and **injects** them into blueprints via `init_task_manager(...)`, `init_dem_task_manager(...)`, `init_terrain_dem_task_manager(...)`, `init_local_terrain_task_manager(...)`, `init_contour_task_manager(...)`. The blueprints rely on those module-level globals being set before any request arrives, so the registration order in `app.py` matters. Never instantiate a second manager inside a route. At module level `create_app()` only runs when `multiprocessing.parent_process() is None` — spawn-platform multiprocessing workers (Windows/macOS frozen exe) re-import the module and must skip init (they would re-run `init_database()` and the contour/local-terrain orphan recovery).

`Config.init_app()` runs both in `app.py` and inside `init_database()` — both call sites are idempotent (`mkdir(..., exist_ok=True)`). Tests monkey-patch `Config.DATABASE_PATH`/`DOWNLOADS_DIR`/`CACHE_DIR` **before** importing `app` so init writes into `tmp_path` (see `tests/test_terrain_api.py`).

### Four parallel task pipelines

| Concern             | Map tile pipeline                                   | DEM pipeline                                            | Local terrain pipeline                                   | Contour pipeline                                        |
| ------------------- | --------------------------------------------------- | ------------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------- |
| Manager             | `services/task_manager.py` (`TaskManager`)          | `services/dem_task_manager.py` (`DemTaskManager`)       | `services/local_terrain_task_manager.py` (`LocalTerrainTaskManager`) | `services/contour_task_manager.py` (`ContourTaskManager`) |
| Engine              | `services/download_engine.py` (aiohttp + GDAL)      | `services/dem_download_engine.py` (+ `earthdata_client`) | none — reuses `tile_dem_task_dir` on the uploaded GeoTIFFs | `dem_download_engine.py` (granule fetch) + `services/contour_engine.py` / `contour_task_tiler.py` (render) |
| DB tables           | `tasks`, `task_tiles`, `task_time_records`          | `dem_tasks`, `dem_files`, `dem_terrain_jobs`            | `local_terrain_tasks`, `local_terrain_files`             | `contour_tasks`, `contour_files`                        |
| REST blueprint      | `routes/api.py` → `/api/tasks/...`                  | `routes/dem_api.py` → `/api/dem/...`                    | `routes/local_terrain_api.py` → `/api/terrain/local/...` | `routes/contour_api.py` → `/api/contour/...`            |
| Tiling / rendering  | n/a                                                 | `routes/terrain_api.py` → `/api/terrain/dem/<id>/start` | the task itself is the tiling job (starts after upload)  | renders in-task once the DEM granules finish downloading |
| Static tile serving | `routes/tiles_static.py` → `/tiles/<id>/...` (completed-task preview; the shared tile cache itself is not served) | `routes/terrain_static.py` → `/terrain/base/...` & `/terrain/dem/<id>/...` | `routes/terrain_static.py` → `/terrain/local/<id>/...`   | `routes/contour_static.py` → `/contour/<id>/...`        |

All four managers keep `active_tasks: Dict[int, Thread]`. The map, DEM, and contour managers also keep `stop_flags: Dict[int, threading.Event]` and run an asyncio loop inside background threads; cancel/pause works by setting the event. Local-terrain tiling is a one-shot `build_terrain` call with no stop flags and no pause/resume — cancelling only flips a still-`pending` task to `cancelled`. Progress is pushed via `socketio.emit('task_progress', ...)`.

### Task lifecycle & deletion conventions

- **Cancel never rewrites terminal states.** `cancel_task` in `task_manager.py`, `dem_task_manager.py`, and `contour_task_manager.py` only transitions `pending`/`running`/`paused` → `cancelled` via a guarded `UPDATE ... WHERE status IN ('pending','running','paused')` — a `completed`/`failed` record must never be flipped to `cancelled`. Keep this guard when touching state transitions.
- **DELETE endpoints take a `delete_files` query param.** `DELETE /api/tasks/<id>`, `/api/dem/tasks/<id>`, and `/api/contour/tasks/<id>` treat `delete_files=1/true/yes` (default: **false**) as a request to also remove the task's on-disk artifact directory after the DB row is gone, via `services/task_cleanup.py`'s `remove_task_dir_if_safe`: the resolved path must lie strictly inside `Config.DOWNLOADS_DIR` and must never be or contain `Config.CACHE_DIR`. `DELETE /api/terrain/local/tasks/<id>` supports the same param but **defaults to true** (historical behavior) — pass `delete_files=false` to keep the files.

### Database conventions

- SQLite at `Config.DATABASE_PATH` (`data/map_downloader.db`). Connections use `sqlite3.Row` factory and `PRAGMA foreign_keys = ON`.
- Use `get_connection_context()` (context manager) for short reads; `get_connection()` + manual close inside managers.
- **Schema evolves inside `init_database()`** with `ALTER TABLE ... ADD COLUMN` wrapped in a try/except that swallows `duplicate column name`. New backward-compatible columns go there; the `migrations/` folder exists but is not the primary mechanism.
- The `config` table is seeded from `DEFAULT_CONFIGS` in `core/database.py` with `INSERT OR IGNORE`. Adding a new setting means appending there.

### Map tile specifics

- Style codes used in Google URLs (`lyrs=`): `m` roadmap, `s` satellite, `y` hybrid, `h` roads, `t` terrain. `MapStyle.from_shorthand` accepts both the legacy 1-char codes and the full names (`roadmap`, `satellite`, etc.); `STYLE_MAP` in `task_manager.py` maps full → short.
- Tiles are cached at `cache/<style>/<zoom>/<x>/<y>.png`. The cache is **shared across tasks** — `Tile.cache_path()` keys only on style + coords. Don't add task-id segments.
- `WEB_MERCATOR_MAX_LAT = 85.0511`, zoom is clamped to `0..21`. `WARN_TILES_THRESHOLD = 100000` (in `services/download_engine.py`) only writes a server-side `logger.warning` when the estimated tile count exceeds it — there is no UI warning and no hard cap.

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

### Local terrain & contour specifics

- Local terrain tasks live under `downloads/terrain/local_task_<id>/`: uploads in `source/` (saved as `*_dem.tif` so the existing `tile_dem_task_dir` tiler can consume them), quantized-mesh output in `terrain_tiles/`, served at `/terrain/local/<id>/...`. Static serving recomputes the path from the current `Config.DOWNLOADS_DIR` instead of trusting the absolute path stored at creation time (frozen-mode relocation).
- Contour tasks default `output_path` to `downloads/dem/`; granules and output live in `contour_task_<id>/` with XYZ PNGs at `contour_tiles/{z}/{x}/{y}.png`, served at `/contour/<id>/...`.
- Like the terrain tiler, the contour renderer is lazy-imported for testability: `contour_task_tiler.tile_contour_task_dir` accepts a `build_contour_fn=` stub so tests don't need GDAL/matplotlib.

### Frozen / Nuitka mode

`core/bundle.py` branches on `'__compiled__' in globals()` (injected by Nuitka into every compiled module); `app.py` and `core/config.py` consume `bundle_dir()`:

- Templates/static come from `bundle_dir()` (the Nuitka standalone dist dir — data dirs sit next to the executable, and `sys.executable` points at the real exe).
- `Config.BASE_DIR` becomes `Path(sys.executable).parent` so `data/`, `downloads/`, `cache/` live next to the executable, not inside the bundle. Anything writing to disk must go through `Config.*_DIR` to stay portable across frozen vs source runs.
- `core/bundle.py:setup_bundle_env()` (called at the top of `app.py`, before any `osgeo` import) sets `GDAL_DATA`/`PROJ_DATA` and fails loudly if the bundle lacks them — it replaces the old PyInstaller runtime hook. `nuitka_build.py` collects `flask_socketio`, `socketio`, `engineio`, `aiohttp`, `osgeo.*`, etc., and copies the GDAL/PROJ data dirs per platform.
- Nuitka only bundles dependency libraries inside the Python/conda prefix. `nuitka_build.py` therefore post-copies the GDAL system-library closure into the dist root on Linux (apt GDAL, `ldd` walk) and on non-conda Windows layouts (OSGeo4W etc., via Nuitka's own Win32 dependency scanner), then self-checks for unresolved libraries. Windows CI uses conda, whose `Library/bin` is inside the prefix, so Nuitka covers it natively.

### Testing patterns to follow

- `sys.path.insert(0, ...)` at top of test files (no `conftest.py`/no installed package).
- For anything that imports `app` or touches the DB: monkey-patch `Config.DATABASE_PATH`/`DOWNLOADS_DIR`/`CACHE_DIR` **first**, then `sys.modules.pop("app", None)` and reimport — `init_database()` runs at import time.
- For terrain/contour tiler tests, pass `build_terrain_fn=<fake>` to `tile_dem_task_dir` (or `build_contour_fn=<fake>` to `tile_contour_task_dir`) instead of installing numpy/GDAL/matplotlib — the production code has lazy-import hooks for exactly this.
