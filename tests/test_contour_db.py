import importlib
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _reload_db(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
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


def test_contour_files_unique_constraint_fresh_db(monkeypatch, tmp_path):
    """新库：contour_files 与 dem_files 一致有 UNIQUE(task_id, granule_id)。"""
    db = _reload_db(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO contour_tasks (name, status, north, south, east, west,"
            " contour_interval, zoom_min, zoom_max)"
            " VALUES ('t', 'pending', 1, 0, 1, 0, 50, 12, 14)")
        task_id = cur.lastrowid
        cur.execute("INSERT INTO contour_files (task_id, granule_id) VALUES (?, 'g1')", (task_id,))
        with pytest.raises(sqlite3.IntegrityError):
            cur.execute("INSERT INTO contour_files (task_id, granule_id) VALUES (?, 'g1')", (task_id,))
    finally:
        conn.close()


def test_contour_files_unique_migration_dedupes_existing_db(monkeypatch, tmp_path):
    """存量库：旧表没有 UNIQUE 且已有重复行 —— init_database 先删重复
    （保留最小 rowid），再建唯一索引兜底；建完后重复写入被拒。"""
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    # 模拟旧 schema（无 UNIQUE）+ 已有重复行
    conn = sqlite3.connect(config.Config.DATABASE_PATH)
    conn.execute('''
        CREATE TABLE contour_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            granule_id TEXT NOT NULL,
            kind TEXT DEFAULT 'dem',
            status TEXT NOT NULL DEFAULT 'pending',
            local_path TEXT,
            size_bytes INTEGER,
            retry_count INTEGER DEFAULT 0,
            error_message TEXT
        )
    ''')
    conn.executemany(
        "INSERT INTO contour_files (task_id, granule_id, status) VALUES (1, 'g1', ?)",
        [("completed",), ("pending",)],
    )
    conn.commit()
    conn.close()

    for mod in ("app", "src.core.database"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()

    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT status FROM contour_files WHERE task_id=1 AND granule_id='g1'").fetchall()
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name='idx_contour_files_task_granule'").fetchone()
        assert len(rows) == 1
        assert rows[0]["status"] == "completed"  # 保留最小 rowid 那行
        assert idx is not None
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO contour_files (task_id, granule_id) VALUES (1, 'g1')")
            conn.commit()
    finally:
        conn.close()

    # 幂等：再次 init 不报错、行数不变
    db.init_database()
    conn = db.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM contour_files").fetchone()["c"] == 1
    finally:
        conn.close()
