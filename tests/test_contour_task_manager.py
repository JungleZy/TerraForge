import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("core.database")
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
        files = cur.execute("SELECT granule_id, kind FROM contour_files WHERE task_id=? ORDER BY kind", (task_id,)).fetchall()
    finally:
        conn.close()

    assert task["contour_interval"] == 50
    assert task["zoom_min"] == 12 and task["zoom_max"] == 14
    assert task["status"] == "pending"
    # Default source is Copernicus GLO-30; terrain shading + water default ON
    # -> 1 GLO-30 DEM + 1 ASTWBD att granule.
    assert task["dataset"] == "COP-DEM-GLO-30"
    assert task["terrain_shade"] == 1 and task["water"] == 1
    assert task["total_files"] == 2
    by_kind = {f["kind"]: f["granule_id"] for f in files}
    assert by_kind["dem"] == "Copernicus_DSM_COG_10_N00_00_E000_00_DEM/Copernicus_DSM_COG_10_N00_00_E000_00_DEM.tif"
    assert by_kind["water"] == "ASTWBDV001_N00E000_att.tif"


def test_create_task_with_aster_dataset(monkeypatch, tmp_path):
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = mgr.create_task({
        "name": "aster", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
        "contour_interval": 50, "zoom_min": 12, "zoom_max": 12,
        "dataset": "ASTGTM.003", "water": False,
    })
    conn = db.get_connection()
    try:
        task = conn.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
        dem = conn.execute("SELECT granule_id FROM contour_files WHERE task_id=? AND kind='dem'", (task_id,)).fetchone()
    finally:
        conn.close()
    assert task["dataset"] == "ASTGTM.003"
    assert dem["granule_id"] == "ASTGTMV003_N00E000_dem.tif"


def test_create_task_water_off_skips_att_granule(monkeypatch, tmp_path):
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = mgr.create_task({
        "name": "noh2o", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
        "contour_interval": 50, "zoom_min": 12, "zoom_max": 12,
        "water": False, "terrain_shade": False,
    })
    conn = db.get_connection()
    try:
        task = conn.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM contour_files WHERE task_id=?", (task_id,)).fetchall()]
    finally:
        conn.close()
    assert task["terrain_shade"] == 0 and task["water"] == 0
    assert task["total_files"] == 1
    assert kinds == ["dem"]  # no att granule when water off


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
    assert task["contour_interval"] == 50


def test_create_task_background_default_and_explicit(monkeypatch, tmp_path):
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    t1 = mgr.create_task({"name": "a", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
                          "contour_interval": 50, "zoom_min": 12, "zoom_max": 12})
    t2 = mgr.create_task({"name": "b", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
                          "contour_interval": 50, "zoom_min": 12, "zoom_max": 12, "background": "transparent"})
    conn = db.get_connection()
    try:
        r1 = conn.execute("SELECT background FROM contour_tasks WHERE id=?", (t1,)).fetchone()
        r2 = conn.execute("SELECT background FROM contour_tasks WHERE id=?", (t2,)).fetchone()
    finally:
        conn.close()
    assert r1["background"] == "#FAF6EC"
    assert r2["background"] == "transparent"
