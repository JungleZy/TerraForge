import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database", "services.dem_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("core.database")
    db.init_database()
    dtm = importlib.import_module("services.dem_task_manager")
    return db, dtm


def _rows(db, task_id):
    conn = db.get_connection()
    try:
        task = conn.execute("SELECT * FROM dem_tasks WHERE id=?", (task_id,)).fetchone()
        gids = [r["granule_id"] for r in conn.execute(
            "SELECT granule_id FROM dem_files WHERE task_id=? ORDER BY granule_id", (task_id,)).fetchall()]
    finally:
        conn.close()
    return task, gids


def test_dem_create_task_defaults_to_glo30(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = mgr.create_task({"name": "x", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0})
    task, gids = _rows(db, task_id)
    assert task["dataset"] == "COP-DEM-GLO-30"
    assert gids == ["Copernicus_DSM_COG_10_N00_00_E000_00_DEM/Copernicus_DSM_COG_10_N00_00_E000_00_DEM.tif"]


def test_dem_create_task_glo30_ignores_num_swb(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = mgr.create_task({"name": "x", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
                               "download_num": "true", "download_swb": "true"})
    task, gids = _rows(db, task_id)
    assert task["download_num"] == 0 and task["download_swb"] == 0  # GLO-30 has no companions
    assert len(gids) == 1


def test_dem_create_task_aster_with_num(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = mgr.create_task({"name": "a", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
                               "dataset": "ASTGTM.003", "download_num": "true"})
    task, gids = _rows(db, task_id)
    assert task["dataset"] == "ASTGTM.003"
    assert "ASTGTMV003_N00E000_dem.tif" in gids
    assert "ASTGTMV003_N00E000_num.tif" in gids
