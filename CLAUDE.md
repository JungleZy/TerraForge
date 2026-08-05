# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Flask + Socket.IO web app for acquiring map data in four parallel pipelines:

1. **Google Maps tile downloader** — selects a bbox in the UI, downloads tiles from `mts0..mts3.googleapis.com`, optionally stitches them into GeoTIFFs via GDAL.
2. **DEM / terrain pipeline** — downloads DEM granules (default: Copernicus GLO-30 from the public `copernicus-dem-30m` AWS bucket, no auth; or ASTER GDEM v3 `ASTGTM.003` from NASA LP DAAC behind Earthdata Login), then optionally tiles them into Cesium quantized-mesh terrain for serving to CesiumJS.
3. **Local terrain pipeline** — tiles user-uploaded GeoTIFFs into Cesium quantized-mesh terrain (reuses the DEM pipeline's tiler; nothing is downloaded).
4. **Contour pipeline** — renders contour-map XYZ PNG tiles from user-uploaded DEM GeoTIFFs (configurable interval, background, shading/hypsometric tint). Legacy download-driven tasks (dataset `ASTGTM.003` / `COP-DEM-GLO-30`) are still runnable, but new tasks are upload-only. **Water masking is legacy-only**: `create_task_with_files` hardcodes `water=0`, there is no UI control for it, and the `ASTWBD.001` fetch is only reachable from the old download-driven path.

The four pipelines have **separate** managers, routes, DB tables, and frontend pages but share the SocketIO instance and `ConfigManager`.

## Commands

本项目使用 **uv** 管理虚拟环境（`.venv/` 已存在于项目根目录）。所有 Python 命令都通过 `uv run` 执行，无需手动 `source .venv/bin/activate`。

```bash
# Setup (one-time)
uv venv                                 # 如果 .venv 不存在
uv pip install -r requirements.txt
uv run python -c "from src.core.database import init_database; init_database()"               # 创建 data/map_downloader.db + 默认配置行

# Run dev server (Flask + Socket.IO on :5000)
uv run python app.py                    # 源码运行 DEBUG=1 by default → use_reloader=True（打包 exe 默认 DEBUG=0）
DEBUG=0 uv run python app.py            # disable reloader/debug

# Migrations — 无独立迁移脚本：迁移已内联在 src/core/database.py 的 init_database()
# （幂等 ALTER + PRAGMA user_version 一次性标记），启动时自动执行，无需手动运行。

# Tests
uv run pytest tests/                                                # full suite
uv run pytest tests/test_terrain_api.py                             # single file
uv run pytest tests/test_dem_task_tiler.py::test_terrain_output_dir_for_task   # single test

# Build standalone executable (Nuitka)
./build.sh           # Linux/macOS（脚本内部使用 uv run python nuitka_build.py）
build.bat            # Windows（脚本内部使用 uv run python nuitka_build.py）
# Output: dist/terraforge/ — entry nuitka_build.py, GDAL/PROJ 环境设置在 src/core/bundle.py
```

GDAL system libraries are required (`gdal-bin libgdal-dev` on Debian, `brew install gdal` on macOS). `requirements.txt` pins `GDAL==3.8.4` — keep in sync with the system `gdal-config --version`. 安装 GDAL Python 绑定时，`uv pip install gdal==$(gdal-config --version)` 通常比固定版本更稳。

**`ImportError: cannot import name '_gdal_array' from 'osgeo'`** — GDAL Python bindings were built without numpy support. The `gdal_array` C extension is only compiled when numpy is import-able at *sdist build time*. Triggered by `band.ReadAsArray()`/`WriteArray()` (used in `cesiumlab_terrain.py` and `download_engine.py`'s stitching path) and by `gdal.UseExceptions()`. Fix by rebuilding from sdist with numpy + setuptools in the venv:

```bash
uv pip install numpy setuptools wheel
UV_NO_CACHE=1 uv pip install --force-reinstall --no-build-isolation --no-binary :all: "GDAL==$(gdal-config --version)"
```

`UV_NO_CACHE=1` is required because uv caches sdist builds; without it, a previously broken build (made before numpy was in the env) is silently reused. Verify with `ls .venv/lib/python3.12/site-packages/osgeo/ | grep _gdal_array` — the `.so` must be present.

## Architecture

### Wiring (app.py → src/app_factory.py)

`app.py` is a ~85-line entry point that only sequences startup; the pieces live beside it and the order between them is load-bearing:

| Step | Module | Why it must sit where it sits |
| --- | --- | --- |
| Entry guards (`freeze_support`, `-c` forwarding) | `src/core/process_entry.py` | Frozen `ProcessPoolExecutor` workers and `multiprocessing`'s resource_tracker re-execute the exe; anything below this line would re-run app init |
| `setup_bundle_env()` | `src/core/bundle.py` | Sets `GDAL_DATA`/`PROJ_DATA` before any transitive `import osgeo` |
| `detect_startup_role(__name__)` | `src/core/runtime_mode.py` | Decides who prints the banner, who prints the ready line, and who runs `create_app()` (truth table in that module's docstring) |
| Banner + spinner | `src/core/startup_banner.py` | Must print *before* the multi-second heavy import, else the console looks hung |
| `create_app()` | `src/app_factory.py` | The actual composition |
| `run_server(...)` | `src/core/server_runner.py` | `socketio.run` plus startup-noise suppression and the reloader watchdog |

`create_app()` is the single composition root. It constructs `socketio`, `TaskManager`, `DemTaskManager`, `LocalTerrainTaskManager`, `ContourTaskManager` and **injects** them into blueprints via `init_task_manager(...)`, `init_dem_task_manager(...)`, `init_terrain_dem_task_manager(...)`, `init_local_terrain_task_manager(...)`, `init_contour_task_manager(...)` (`_build_task_managers`), then registers the blueprints (`_register_blueprints`). The blueprints rely on those module-level globals being set before any request arrives, so injection must precede registration. Never instantiate a second manager inside a route. `create_app()` only runs when `StartupRole.should_create_app` — spawn-platform multiprocessing workers (`__mp_main__` / Nuitka's `__parents_main__`) and the dev reloader's watcher parent must skip init (they would re-run `init_database()` and the contour/local-terrain orphan recovery).

**Every business import inside `src/app_factory.py` is function-local, on purpose.** Tests pop `app` / `src.routes.*` / `src.services.*` out of `sys.modules` and re-import `app`; `app_factory` itself is not popped, so a module-level `from src.routes import api_bp` would pin the previous module instance and produce the silently-green "test patches the new module, requests hit the old one" failure (see `tests/conftest.py`). Module level only holds Flask/SocketIO plus warm-up `import src.routes` / manager imports, whose sole job is to pay the multi-second import cost while the spinner is still spinning.

`Config.init_app()` runs both in `src/app_factory.py` and inside `init_database()` — both call sites are idempotent (`mkdir(..., exist_ok=True)`). Tests monkey-patch `Config.DATABASE_PATH`/`DOWNLOADS_DIR`/`CACHE_DIR` **before** importing `app` so init writes into `tmp_path` (see `tests/test_terrain_api.py`).

### Four parallel task pipelines

| Concern             | Map tile pipeline                                   | DEM pipeline                                            | Local terrain pipeline                                   | Contour pipeline                                        |
| ------------------- | --------------------------------------------------- | ------------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------- |
| Manager             | `src/services/task_manager.py` (`TaskManager`)          | `src/services/dem_task_manager.py` (`DemTaskManager`)       | `src/services/local_terrain_task_manager.py` (`LocalTerrainTaskManager`) | `src/services/contour_task_manager.py` (`ContourTaskManager`) |
| Engine              | `src/services/download_engine.py` (aiohttp + GDAL)      | `src/services/dem_download_engine.py` (+ `earthdata_client`) | none — reuses `tile_dem_task_dir` on the uploaded GeoTIFFs | `src/services/contour_engine.py` / `contour_task_tiler.py` (render). `dem_download_engine.py` is only reached by legacy download-driven tasks |
| DB tables           | `tasks`, `task_tiles`, `task_time_records`          | `dem_tasks`, `dem_files`, `dem_terrain_jobs`            | `local_terrain_tasks`, `local_terrain_files`             | `contour_tasks`, `contour_files`                        |
| REST blueprint      | `src/routes/api.py` → `/api/tasks/...`                  | `src/routes/dem_api.py` → `/api/dem/...`                    | `src/routes/local_terrain_api.py` → `/api/terrain/local/...` | `src/routes/contour_api.py` → `/api/contour/...`            |
| Tiling / rendering  | n/a                                                 | `src/routes/terrain_api.py` → `/api/terrain/dem/<id>/start` | the task itself is the tiling job (starts after upload)  | renders in-task from the uploaded GeoTIFFs (legacy download-driven tasks render after their granules land) |
| Static tile serving | `src/routes/tiles_static.py` → `/tiles/<id>/...` (completed-task preview; the shared tile cache itself is not served) | `src/routes/terrain_static.py` → `/terrain/base/...` & `/terrain/dem/<id>/...` | `src/routes/terrain_static.py` → `/terrain/local/<id>/...`   | `src/routes/contour_static.py` → `/contour/<id>/...`        |

All four managers keep `active_tasks: Dict[int, Thread]`. The map, DEM, and contour managers also keep `stop_flags: Dict[int, threading.Event]` and run an asyncio loop inside background threads; cancel/pause works by setting the event. Local-terrain tiling is a one-shot `build_terrain` call with no stop flags and no pause/resume — cancelling only flips a still-`pending` task to `cancelled`. Progress is pushed via `socketio.emit('task_progress', ...)`; the map pipeline additionally emits `task_stitch_progress` / `task_copy_progress` for the post-download phases (stitching per zoom, tile mirror-copy), emitted from the start of each phase so a big task never looks stuck at 100%.

**Pre-tiling phases report through a separate `stage_cb(phase, fraction)` channel, not `progress_cb(done, total)`** — merging multiple DEMs into one raster and building its pyramid both run *before* `total` exists (`total` needs the sampler, the sampler needs the merged raster), so there is no denominator. Terrain managers emit it as `terrain_job_progress` with `stage`/`stage_label`/`stage_fraction`; contour emits `task_progress` with `phase='prepare'` (**not** `'render'` — several tests filter events by that value). ⚠️ `stage_cb` is ultimately wired to **GDAL's native progress callback**, and GDAL treats a raising callback as *user cancellation*: measured on 3.8.4, an exception makes `gdal.Translate` return `None` and deletes the output. Every layer from the GDAL callback down to `socketio.emit` swallows exceptions — one disconnected client must not fail a tiling job. Throttle in the manager (`_PROGRESS_EMIT_MIN_INTERVAL`), never emit raw: real files produce ~59 callbacks for a 13 s merge. Measured end-to-end on 6 ASTER granules: first UI signal moved from **13.83 s → 0.98 s**.

### Task lifecycle & deletion conventions

- **Cancel never rewrites terminal states.** `cancel_task` in `task_manager.py`, `dem_task_manager.py`, and `contour_task_manager.py` only transitions `pending`/`running`/`paused` → `cancelled` via a guarded `UPDATE ... WHERE status IN ('pending','running','paused')` — a `completed`/`failed` record must never be flipped to `cancelled`. Keep this guard when touching state transitions.
- **DELETE endpoints take a `delete_files` query param.** `DELETE /api/tasks/<id>`, `/api/dem/tasks/<id>`, and `/api/contour/tasks/<id>` treat `delete_files=1/true/yes` (default: **false**) as a request to also remove the task's on-disk artifact directory after the DB row is gone, via `src/services/task_cleanup.py`'s `remove_task_dir_if_safe` — save paths are full-disk since 0.2.4, so the guardrail is: refuse any path with a symlink component, paths shallower than two directory levels, the user's home directory, `Config.DOWNLOADS_DIR` itself or its ancestors, and anything that is/contains/is contained by `Config.CACHE_DIR`. `DELETE /api/terrain/local/tasks/<id>` supports the same param but **defaults to true** (historical behavior) — pass `delete_files=false` to keep the files.

### Database conventions

- SQLite at `Config.DATABASE_PATH` (`data/map_downloader.db`). Connections use `sqlite3.Row` factory and `PRAGMA foreign_keys = ON`.
- Use `get_connection_context()` (context manager) for short reads; `get_connection()` + manual close inside managers.
- **Schema evolves inside `init_database()`** with `ALTER TABLE ... ADD COLUMN` wrapped in a try/except that swallows `duplicate column name`. New backward-compatible columns go there. One-shot data migrations guard on `PRAGMA user_version` (currently 2: sparse `task_tiles` = 1, legacy relative `output_path` normalization = 2). There is **no** side-channel migration runner — the `migrations/` folder was emptied in a previous review cycle and git does not track empty directories, so it does not exist on a fresh clone.
- The `config` table is seeded from `DEFAULT_CONFIGS` in `src/core/database.py` with `INSERT OR IGNORE`. Adding a new setting means appending there.

### Theming (dark / light / system)

- The single switch is the `data-bs-theme` attribute on `<html>` (`dark` / `light`). Bootstrap 5.3 reacts natively; all custom components read the `--color-*` tokens in `static/css/style.css` `:root` (dark values are the `:root` defaults = SSR/no-JS fallback; light overrides live in a `[data-bs-theme="light"]` block overriding the same token names).
- Preference is client-side only: localStorage key `tf-theme` ∈ `dark` | `light` | `system` (default `dark`), **not** the server `config` table. `system` resolves via `matchMedia('(prefers-color-scheme: light)')` and follows it live via a `change` listener.
- `templates/base.html` `<head>` has an inline bootstrap script (before any CSS link) that sets the attribute synchronously to avoid first-frame flash; `<html data-bs-theme="dark">` literal must stay (pinned by `tests/test_css_contract.py`). Runtime switching lives in `static/js/theme.js` (`window.TerraTheme` = `{get, set, resolved, init}`), loaded globally in `base.html` after `ui.js`; the switcher UI is the 「外观」 section at the top of `templates/_config_content.html` (three text-labelled `.status-chip` buttons), wired by `initThemeSwitcher()` in `static/js/config.js`.

### Interface language (zh / en)

- Deliberately **not** Flask-Babel: this is a Nuitka-packaged offline tool, and gettext would mean compiling `.mo` files at build time and shipping them as data. The catalog is plain Python modules — `src/i18n/catalog/<domain>.py`, each exporting `MESSAGES = {key: {'zh': ..., 'en': ...}}` — so Nuitka's static analysis pulls them in and `nuitka_build.py` needs no change. New domains **must** be listed explicitly in `src/i18n/catalog/__init__.py`; there is no `pkgutil` auto-discovery, because Nuitka cannot follow it.
- Merge-time invariants (they raise, they don't warn): keys are globally unique, and every key has **both** locales. `tests/test_i18n.py` additionally rejects an `en` value that is still Chinese and placeholder sets that differ between locales.
- Locale lives in the cookie `tf-lang` (default `zh`), **not** localStorage — templates are server-rendered, so the locale must reach the server. `src/i18n.register(app)` installs the Jinja global `t()` plus the context values `locale` / `html_lang` / `i18n_client_json`. `<html lang="{{ html_lang }}">` is the hook CSS uses for locale-specific layout (e.g. the map toolbar capsule widens for English labels).
- Only `js.*` keys are inlined into the page (`window.__I18N__`); template text is already rendered server-side. `static/js/i18n.js` exposes the global `t(key, params)` and must load **before** every business script — they call `t()` at parse time. Missing keys render as the key itself on both sides, deliberately:漏翻 shows up on screen instead of silently falling back to Chinese.
- Key prefixes map to the source: `tpl.<page>.*` templates, `js.<module>.*` browser, `api.<area>.*` route responses, `val.<module>.*` synchronous validation feedback. `t()` outside a request context (background task threads) falls back to `zh`.
- **Out of scope on purpose**: log messages, the startup console banner, and error text persisted into the DB by async task managers. They are diagnostics, and the locale at write time says nothing about who reads them later.
- Source-level contract tests that used to grep Chinese literals now go through the catalog (see the 「i18n 改造」 registrations in `tests/test_tasks_js_contract.py`, `test_css_contract.py`, `test_map_js_contract.py`, `test_theme_switch.py`): assert both that the source references the key **and** that the catalog's `zh` value is the expected word. Checking only one half is bypassable.

### Map tile specifics

- Style codes used in Google URLs (`lyrs=`): `m` roadmap, `s` satellite, `y` hybrid, `h` roads, `t` terrain. `MapStyle.from_shorthand` accepts both the legacy 1-char codes and the full names (`roadmap`, `satellite`, etc.); `STYLE_MAP` in `task_manager.py` maps full → short.
- Tiles are cached at `cache/<style>/<zoom>/<x>/<y>.png`. The cache is **shared across tasks** — `Tile.cache_path()` keys only on style + coords. Don't add task-id segments.
- Since 0.2.4 there is **no automatic cache eviction** (the LRU `cache_max_size_mb` cleanup was removed along with the config key). Inspection and clearing are user-driven: `GET /api/cache/stats` (one category per top-level dir under `cache/`) and `POST /api/cache/clear` (`{"category": key|"__all__"}`), both backed by `src/services/task_cleanup.py`.
- The task output dir is a **live mirror** of the cache: each tile is copied to `<output_path>/task_<id>/<z>/<x>/<y>.png` the moment its download lands in cache (download callback), and a separate backfill thread copies cache-hit tiles (resume / repeated bbox) in parallel with the download. A cancelled task keeps its partially-mirrored output dir, matching cache state.
- `WEB_MERCATOR_MAX_LAT = 85.0511`, zoom is clamped to `0..21`. `WARN_TILES_THRESHOLD = 100000` (in `src/services/download_engine.py`) only writes a server-side `logger.warning` when the estimated tile count exceeds it — there is no UI warning and no hard cap.

### DEM / terrain specifics

- Datasets (see `src/services/dem_granules.py`): `COP-DEM-GLO-30` (default — Copernicus GLO-30 COGs on the public `copernicus-dem-30m` S3 bucket, no auth; granules are nested `<name>/<name>.tif`) and `ASTGTM.003` (Earthdata; 1°×1° granules named `ASTGTMV003_{N|S}LL{E|W}LLL_dem.tif`, optional `_num.tif`; coverage 83S–83N). Water-body masks come from `ASTWBD.001` (`ASTWBDV001_*_att.tif`, Earthdata, best-effort — 404s don't fail the task).
- Earthdata Login credentials live in the `config` table (`earthdata_username`, `earthdata_password`). `EarthdataClient` does a manual URS OAuth redirect dance — do not "harden" it (per inline note).
- Terrain tiling layout:
  - DEM granules: `downloads/dem/dem_task_<id>/*_dem.tif` / `*_DEM.tif` (Copernicus) (`*_num.tif` is intentionally filtered out by `list_dem_tifs`)
  - Output tiles: `downloads/dem/dem_task_<id>/terrain_tiles/{z}/{x}/{y}.terrain` + `layer.json`
  - Global base (low-zoom planet coverage): `downloads/terrain/base_z8/` served at `/terrain/base/...`. **Ships with the repo** as split archives in `assets/terrain/base_z8.tar.gz.part{aa,ab}` (167 MB total — split because GitHub's single-file hard limit is 100 MB); restore once with `uv run python scripts/unpack_base_terrain.py`. Built from GEBCO 2024 at **z0–7** (the `_z8` in the directory name is historical — z8 alone would be 76% of the volume for a 1.2 km vertex spacing only useful when zoomed right up to the DEM's edge, which is what tiling a real DEM is for). Carries vertex normals; built with `triangulator="auto"` — the intuition that "grid always wins on flat tiles" only held *before* normals, since normals cost 2 B/vertex and grid is fixed at 4225 vertices vs martini's 589 on flat tiles (measured: grid 2.1 GB vs auto 942 MB for the same coverage). Not restoring it is safe — see the `parentUrl` note below. Full details in `docs/reference/terrain/global-base-build.md`.
  - Local DEM tiles `layer.json` is patched (`patch_layer_json_parent`) to carry `parentUrl` pointing at the base, so CesiumJS cascades automatically (see `docs/reference/terrain/cesiumjs-loading.md`).
- The tiler is `src/services/terrain_tiling/cesiumlab_terrain.py` — a vendored copy of CesiumLab 4.0.17's quantized-mesh builder. It's used as a library (`build_terrain(...)`) by `dem_task_tiler.tile_dem_task_dir`. The import is **lazy** so tests can inject a `build_terrain_fn=` stub without needing numpy/GDAL at import time.
- ⚠️ **Multi-granule input is materialised into one GeoTIFF, never handed to the sampler as a multi-source VRT** (`build_input_raster`, landed 2026-08-05). Reading a multi-source VRT with `buf_xsize/buf_ysize` smaller than the window makes GDAL pick a source overview *per split segment*, and ASTER's built-in overviews are at 2/3/4/**7.98**/**8.98**/15.9/63/80× — not powers of two. Two adjacent tiles have different read windows (south `win=664/buf=83`, north `win=656/buf=82`), so they land on different overview levels and the same lat/lon samples to different heights. Measured on 6 ASTER granules: z10 had 16 tile pairs whose shared edge disagreed by up to **50.9 m**; z11/z12 were clean only because their scale (4/2) can't reach the 7.98/8.98 pair. Single-granule input is immune (verified at scale 8/16/32, with and without overviews). `OVERVIEW_LEVEL=NONE` does **not** help — it only disables the VRT's *own* virtual overviews; sources still self-select after `VRTSimpleSource` forwards the request. The materialised copy **must** carry power-of-two overviews too: without them low-zoom tiles fall back to full-resolution reads (z6 goes 0.2 → 13.0 ms/tile). Costs one extra copy during tiling, deleted in `build_terrain`'s `finally` (and on its own failure paths — the `finally` in `build_terrain` can't reach it, since `build_input_raster` is called *outside* that `try`). `SIGKILL` still can't be covered, so the filename carries the writing pid (`cesiumlab_terrain_<pid>_*.tif`) and `task_cleanup.sweep_startup_residue` sweeps it as its 5th residue class — ownership is decided by pid, not mtime (mtime freezes when the merge finishes while tiling runs for hours). Sweep roots come from the DB (`dem_terrain_jobs` / `local_terrain_tasks` output dirs), because DEM `output_path` is any absolute path the user picks and is often outside `DOWNLOADS_DIR` — that's where GB-scale residue actually lands. Sizing: ~78% of source for **Int16** ASTER, but ~**1.9×** for the default **Float32** Copernicus GLO-30 — budget for the latter. Two traps that make GDAL report success while handing back a corrupt raster, both guarded: `BIGTIFF=IF_SAFER` is mandatory (with `COMPRESS` set, GTiff's default `IF_NEEDED` silently caps at 4 GiB ≈ 92 Copernicus granules), and `Translate`/`BuildOverviews` return values do **not** reflect I/O write failures — the code checks `gdal.GetLastErrorType()` and verifies the product against the source. Full evidence: `tests/test_fix_terrain_vrt_overview_seam.py`.
- **Triangulation & per-vertex normals** (landed 2026-08-05; design + measurements in `docs/superpowers/specs/2026-08-04-terrain-triangulation-design.md`, read its archive header first — four of its conclusions were overturned during execution):
  - `TileParams` defaults to `triangulator="auto"` / `max_error_k=0.15`; `build_terrain` defaults to `normals=True`.
  - `"auto"` is **per-tile best-of**: both backends (regular grid and the self-written Martini/RTIN in `terrain_tiling/rtin.py`) encode every tile and the one that is smaller **after gzip** wins, so no tile is ever larger than `min(grid, martini)`. This is not gold-plating — on 112,584 paired tiles plain Martini is a net *loss* on rough terrain (mountain +17.6%, hill +9.8% in gzipped bytes) because gzip costs ≈0.91 B/triangle on the grid stream vs ≈4.04 B/triangle on Martini, i.e. simplification has to exceed 77.4% just to break even. `"grid"` / `"martini"` force a single backend for troubleshooting. `build_terrain` returns `chose_martini` / `chose_grid` counts alongside `total`/`rendered`/`failed`.
  - Tiles carry oct-encoded per-vertex normals (`extensionId=1`, 2 bytes/vertex) and `layer.json` declares `extensions: ["octvertexnormals"]`. Normals are computed on the **full grid in ECEF space** with a ghost ring (samples `(n+2)²`, crops back to `n²`), independent of the simplified geometry. Cost: tiling CPU +35.8% vs `normals=False`.
  - `triangulator` / `max_error_k` / `normals` are **not exposed to UI / DB / API** — nothing reads them from the `config` table, env, request body, or query string. They exist for troubleshooting and test injection; the CLI's `--triangulator` / `--max-error-k` are the designated escape hatch.
  - Frontend: `static/js/map.js` must pass `requestVertexNormals: true` to `CesiumTerrainProvider.fromUrl` — without it Cesium never requests the extension and the normals in the tiles are dead weight. The lighting toggle is `static/js/terrain_lighting.js` (`window.TerrainLighting`, localStorage key `tf-terrain-lighting`, **default off**, deliberately not in the `config` table); it must be loaded **before** `map.js` or the `if (window.TerrainLighting)` guard is always false and the toggle silently disappears.
- ⚠️ **`parentUrl` must be a directory URL** (`http://localhost:5000/terrain/base`), never `.../layer.json`. Cesium runs `appendForwardSlash()` and then appends `layer.json`, so a `.../layer.json` value 404s — and Cesium **does not reject**: it installs a fake heightmap-1.0 layer whose `heightmapStructure` lands on the *shared* builder, so the task's own quantized-mesh tiles get parsed as heightmap too. Measured: a 4154 m peak decoded as −744 m while `hasVertexNormals` still reported true, every tile 200, zero console errors. `layer_json.normalize_parent_url()` strips the suffix at the single write point (`patch_layer_json_parent`) — that matters because existing installs still have the bad value in their `config` row; changing `DEFAULT_CONFIGS` alone only helps fresh databases.
- ⚠️ **`build_terrain`'s own default `tile_size` is 17, but production always goes through `TileParams.tile_size=65`.** Any experiment calling `build_terrain(...)` directly must pass `tile_size=65` explicitly — at 17 there is too little to simplify, `auto` picks grid for every single tile, and the result looks like a bug when it is just the wrong parameter.
- `src/routes/terrain_static.py` enforces path-traversal safety: every served file must resolve under `Config.DOWNLOADS_DIR`. Don't bypass `_resolve_safe_file`.

### Local terrain & contour specifics

- Local terrain tasks live under `downloads/terrain/local_task_<id>/`: uploads in `source/` (saved as `*_dem.tif` so the existing `tile_dem_task_dir` tiler can consume them), quantized-mesh output in `terrain_tiles/`, served at `/terrain/local/<id>/...`. Static serving recomputes the path from the current `Config.DOWNLOADS_DIR` instead of trusting the absolute path stored at creation time (frozen-mode relocation).
- Contour tasks default `output_path` to `downloads/dem/`; uploaded GeoTIFFs (and, for legacy download-driven tasks, fetched granules) plus output live in `contour_task_<id>/` with XYZ PNGs at `contour_tiles/{z}/{x}/{y}.png`, served at `/contour/<id>/...`. New tasks carry `dataset='upload'`; water mask is hardcoded to 0 on that path (`ASTWBD.001` is only fetched by the legacy download path).
- Like the terrain tiler, the contour renderer is lazy-imported for testability: `contour_task_tiler.tile_contour_task_dir` accepts a `build_contour_fn=` stub so tests don't need GDAL/matplotlib.

### Frozen / Nuitka mode

`src/core/bundle.py` branches on `'__compiled__' in globals()` (injected by Nuitka into every compiled module); `src/app_factory.py`, `src/core/runtime_mode.py` and `src/core/config.py` consume `bundle_dir()`:

- Templates/static come from `bundle_dir()` via `src/app_factory.py:_asset_dirs()` (the Nuitka standalone dist dir — data dirs sit next to the executable, and `sys.executable` points at the real exe). It hands Flask **absolute** folders plus an explicit `root_path`, because the factory no longer lives next to `templates/`.
- `Config.BASE_DIR` becomes `Path(sys.executable).parent` so `data/`, `downloads/`, `cache/` live next to the executable, not inside the bundle. Anything writing to disk must go through `Config.*_DIR` to stay portable across frozen vs source runs.
- `src/core/bundle.py:setup_bundle_env()` (called at the top of `app.py`, before any `osgeo` import) sets `GDAL_DATA`/`PROJ_DATA` and fails loudly if the bundle lacks them — it replaces the old PyInstaller runtime hook. `nuitka_build.py` collects `flask_socketio`, `socketio`, `engineio`, `aiohttp`, `osgeo.*`, etc., and copies the GDAL/PROJ data dirs per platform.
- Nuitka only bundles dependency libraries inside the Python/conda prefix. `nuitka_build.py` therefore post-copies the GDAL system-library closure into the dist root on Linux (apt GDAL, `ldd` walk) and on non-conda Windows layouts (OSGeo4W etc., via Nuitka's own Win32 dependency scanner), then self-checks for unresolved libraries. Windows CI uses conda, whose `Library/bin` is inside the prefix, so Nuitka covers it natively.

### Testing patterns to follow

- `sys.path.insert(0, ...)` at top of test files (no `conftest.py`/no installed package).
- For anything that imports `app` or touches the DB: monkey-patch `Config.DATABASE_PATH`/`DOWNLOADS_DIR`/`CACHE_DIR` **first**, then `sys.modules.pop("app", None)` and reimport — `init_database()` runs at import time.
- For terrain/contour tiler tests, pass `build_terrain_fn=<fake>` to `tile_dem_task_dir` (or `build_contour_fn=<fake>` to `tile_contour_task_dir`) instead of installing numpy/GDAL/matplotlib — the production code has lazy-import hooks for exactly this.
