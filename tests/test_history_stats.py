import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "database", "services.dem_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _seed(db):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO tasks (name, status, north, south, east, west, "
                    "zoom_min, zoom_max, style, output_format, total_tiles, downloaded_tiles, output_path) "
                    "VALUES ('m1','completed',1,0,1,0,1,2,'m','png',10,10,'/x')")
        cur.execute("INSERT INTO tasks (name, status, north, south, east, west, "
                    "zoom_min, zoom_max, style, output_format, total_tiles, downloaded_tiles, output_path) "
                    "VALUES ('m2','failed',1,0,1,0,1,2,'m','png',10,3,'/x')")
        cur.execute("INSERT INTO dem_tasks (name, status, north, south, east, west, "
                    "dataset, total_files, downloaded_files, output_path) "
                    "VALUES ('d1','completed',1,0,1,0,'ASTGTM.003',2,2,'/x')")
        cur.execute("INSERT INTO local_terrain_tasks (name, status, output_path, "
                    "source_dir, output_dir, maxzoom, total_files, uploaded_files) "
                    "VALUES ('l1','completed','/x','/src','/out',14,5,5)")
        conn.commit()
    finally:
        conn.close()


def test_history_stats_aggregates_three_tables(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("database")
    _seed(db)

    resp = client.get("/api/history_stats")
    assert resp.status_code == 200
    stats = resp.get_json()["stats"]
    assert stats["total_tasks"] == 4
    assert stats["completed"] == 3
    assert stats["failed"] == 1
    assert stats["total_downloaded"] == 10 + 3 + 2 + 5  # 20


def test_history_stats_empty_db(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/api/history_stats")
    assert resp.status_code == 200
    stats = resp.get_json()["stats"]
    assert stats == {"total_tasks": 0, "completed": 0, "failed": 0, "total_downloaded": 0}
