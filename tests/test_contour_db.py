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
