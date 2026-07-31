import importlib
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    # Isolate DB and directory side effects before importing app.py (which runs init_database()).
    from core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in ("app", "core.database", "services.dem_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")

    app = app_mod.app
    app.config["TESTING"] = True
    return app_mod, app.test_client()


def _insert_dem_task(db, output_path):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path, total_files)
            VALUES ('dem', 'completed', 1, 0, 1, 0, 'ASTGTM.003', ?, 1)
            """,
            (str(output_path),),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_terrain_api_wiring_does_not_return_500(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)

    get_response = client.get("/api/terrain/dem/1")
    assert get_response.status_code == 200
    assert get_response.get_json()["job"] is None

    start_response = client.post("/api/terrain/dem/1/start")
    assert start_response.status_code == 400
    assert "DEM task 1 not found" in start_response.get_json()["error"]


def test_terrain_api_start_creates_running_job(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    def fake_run_tiling_job(self, task_id, task_dir, output_dir, maxzoom, parent_url):
        return None

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job", fake_run_tiling_job)

    response = client.post(f"/api/terrain/dem/{task_id}/start")

    assert response.status_code == 200
    assert response.get_json()["success"] is True

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM dem_terrain_jobs WHERE task_id=?", (task_id,))
        job = cur.fetchone()
    finally:
        conn.close()

    assert job is not None
    assert job["status"] == "running"
    assert job["maxzoom"] == 14
    assert job["output_dir"].endswith(os.path.join(f"dem_task_{task_id}", "terrain_tiles"))


def test_terrain_api_get_existing_job(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_terrain_jobs
              (task_id, status, output_dir, maxzoom, parent_url, started_at)
            VALUES (?, 'completed', ?, 12, 'http://localhost:5000/terrain/base/layer.json', ?)
            """,
            (task_id, str(tmp_path / "tiles"), datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get(f"/api/terrain/dem/{task_id}")

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["job"]["task_id"] == task_id
    assert body["job"]["status"] == "completed"


def test_terrain_api_rejects_duplicate_running_start(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    def fake_run_tiling_job(self, task_id, task_dir, output_dir, maxzoom, parent_url):
        return None

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job", fake_run_tiling_job)

    first = client.post(f"/api/terrain/dem/{task_id}/start")
    second = client.post(f"/api/terrain/dem/{task_id}/start")

    assert first.status_code == 200
    assert second.status_code == 400
    assert "already running" in second.get_json()["error"]
