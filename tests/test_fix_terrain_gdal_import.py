"""
I9 fix: cesiumlab_terrain.py's lazy GDAL import hook called sys.exit(), which
raises SystemExit — a BaseException that `except Exception` handlers do NOT
catch. With GDAL missing, the DEM tiling job thread died with SystemExit and
the dem_terrain_jobs row stayed 'running' forever.

The hook must raise ImportError instead, so that
  - importing the module without GDAL fails with ImportError (not SystemExit),
  - dem_task_tiler.tile_dem_task_dir wraps it in its friendly RuntimeError,
  - DemTaskManager._run_tiling_job (which catches Exception) marks the job
    'failed' with an error_message.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

CESIUMLAB_MOD = "src.services.terrain_tiling.cesiumlab_terrain"


def _block_gdal(monkeypatch):
    """Make `from osgeo import ...` raise ImportError on the next fresh import."""
    for name in [m for m in sys.modules if m == "osgeo" or m.startswith("osgeo.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "osgeo", None)  # import blocker
    monkeypatch.delitem(sys.modules, CESIUMLAB_MOD, raising=False)


def test_import_without_gdal_raises_importerror_not_systemexit(monkeypatch):
    _block_gdal(monkeypatch)
    try:
        importlib.import_module(CESIUMLAB_MOD)
    except SystemExit as e:  # noqa: BLE001 - explicitly asserting this never happens
        pytest.fail(f"SystemExit leaked from lazy GDAL import hook: {e!r}")
    except ImportError:
        pass
    else:
        pytest.fail("expected ImportError when GDAL bindings are missing")


def test_tile_dem_task_dir_wraps_missing_gdal_in_friendly_runtimeerror(monkeypatch, tmp_path):
    _block_gdal(monkeypatch)
    from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "A_dem.tif").write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Terrain tiling runtime deps missing"):
        tile_dem_task_dir(
            task_dir,
            tmp_path / "out",
            TileParams(maxzoom=0, parent_url="http://x/layer.json"),
        )


def test_tiling_job_marked_failed_with_error_message_when_tiler_raises(monkeypatch, tmp_path):
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    sys.modules.pop("src.core.database", None)
    db = importlib.import_module("src.core.database")
    db.init_database()

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks (name, status, north, south, east, west, dataset, output_path)
            VALUES ('t', 'completed', 1, 0, 1, 0, 'aster', '')
            """
        )
        task_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO dem_terrain_jobs (task_id, status, output_dir, maxzoom, parent_url)
            VALUES (?, 'running', '', 14, '')
            """,
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    import src.services.dem_task_manager as mgr_mod

    def boom(**kwargs):
        raise RuntimeError("Terrain tiling runtime deps missing (need numpy + GDAL bindings).")

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", boom)

    mgr = mgr_mod.DemTaskManager.__new__(mgr_mod.DemTaskManager)
    # Must NOT raise: _run_tiling_job catches Exception and records the failure.
    mgr._run_tiling_job(task_id, Path("x"), Path("y"), 14, "http://x/layer.json")

    conn = db.get_connection()
    try:
        row = conn.cursor().execute(
            "SELECT status, error_message FROM dem_terrain_jobs WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row["status"] == "failed"
    assert row["error_message"] and "runtime deps missing" in row["error_message"]
