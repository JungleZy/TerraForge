import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _reload_db(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("core.database")
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
    assert rows["contour_background"] == "#FAF6EC"
    assert rows["contour_index_step"] == "5"
    # Terrain coloring (hypsometric + hillshade + water) defaults.
    assert rows["contour_hypsometric_breaks"] == "0,200,500,1000,2000,3000,4000,5000"
    assert len(rows["contour_hypsometric_colors"].split(",")) == 9  # N breaks -> N+1 bands
    assert rows["contour_hillshade_azimuth"] == "315"
    assert rows["contour_hillshade_altitude"] == "45"
    assert rows["contour_water_color_ocean"].startswith("#")
    assert rows["contour_water_color_inland"].startswith("#")


def test_contour_terrain_columns_exist(monkeypatch, tmp_path):
    db = _reload_db(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        task_cols = {r["name"] for r in cur.execute("PRAGMA table_info(contour_tasks)").fetchall()}
        file_cols = {r["name"] for r in cur.execute("PRAGMA table_info(contour_files)").fetchall()}
    finally:
        conn.close()
    assert "terrain_shade" in task_cols
    assert "water" in task_cols
    assert "kind" in file_cols
