import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _reload_db(monkeypatch, tmp_path):
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    sys.modules.pop("src.core.database", None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    return db


def test_local_terrain_tables_exist(monkeypatch, tmp_path):
    db = _reload_db(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r["name"] for r in cur.fetchall()}
        assert "local_terrain_tasks" in tables
        assert "local_terrain_files" in tables

        cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {r["name"] for r in cur.fetchall()}
        assert "idx_local_terrain_tasks_status" in indexes
        assert "idx_local_terrain_files_status" in indexes
    finally:
        conn.close()


def test_local_terrain_tasks_notnull_columns(monkeypatch, tmp_path):
    db = _reload_db(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(local_terrain_tasks)")
        cols = {r["name"]: r for r in cur.fetchall()}
        assert cols["status"]["notnull"] == 1
        assert cols["output_dir"]["notnull"] == 1
        assert cols["maxzoom"]["notnull"] == 1

        cur.execute("PRAGMA table_info(local_terrain_files)")
        fcols = {r["name"]: r for r in cur.fetchall()}
        assert fcols["task_id"]["notnull"] == 1
        assert fcols["stored_filename"]["notnull"] == 1
    finally:
        conn.close()
