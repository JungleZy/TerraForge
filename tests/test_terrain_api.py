import importlib
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    # Isolate DB and directory side effects before importing app.py (which runs init_database()).
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in ("app", "src.core.database", "src.services.dem_task_manager"):
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
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    def fake_run_tiling_job(self, *args, **kwargs):
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
    db = importlib.import_module("src.core.database")
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
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    def fake_run_tiling_job(self, *args, **kwargs):
        return None

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job", fake_run_tiling_job)

    first = client.post(f"/api/terrain/dem/{task_id}/start")
    second = client.post(f"/api/terrain/dem/{task_id}/start")

    assert first.status_code == 200
    assert second.status_code == 400
    assert "already running" in second.get_json()["error"]



def _job_row(db, task_id):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM dem_terrain_jobs WHERE task_id=?", (task_id,))
        return cur.fetchone()
    finally:
        conn.close()


def _finish_job(db, task_id):
    """把 job 行从 running 落到 completed —— 重切前的前置（running 会被拒）。"""
    conn = db.get_connection()
    try:
        conn.execute("UPDATE dem_terrain_jobs SET status='completed' WHERE task_id=?",
                     (task_id,))
        conn.commit()
    finally:
        conn.close()


def test_terrain_start_persists_preset_defaults(monkeypatch, tmp_path):
    """不传档位时落库出厂默认：balanced + 法线关。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)

    assert client.post(f"/api/terrain/dem/{task_id}/start").status_code == 200

    job = _job_row(db, task_id)
    assert job["quality"] == "balanced"
    assert job["vertex_normals"] == 0


def test_terrain_start_persists_explicit_preset(monkeypatch, tmp_path):
    """显式档位与法线开关落库。

    直调 manager 而不是打 HTTP：路由层收 quality/vertex_normals 是 Task 8 的活，
    这里钉的是管理器这一层的契约（路由接上后再由那边补端到端那条）。
    """
    app_mod, _client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)

    app_mod.dem_task_manager.start_tiling(task_id, quality="speed", vertex_normals=True)

    job = _job_row(db, task_id)
    assert job["quality"] == "speed"
    assert job["vertex_normals"] == 1


def test_terrain_start_rejects_unknown_quality(monkeypatch, tmp_path):
    """拼错的档位当场报错，不静默退回 balanced，也不留下 job 行。"""
    app_mod, _client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    with pytest.raises(ValueError, match="quality"):
        app_mod.dem_task_manager.start_tiling(task_id, quality="fast")

    assert _job_row(db, task_id) is None


def test_terrain_restart_overwrites_previous_preset(monkeypatch, tmp_path):
    """重切换档位必须真的换掉。

    重切走的是 upsert 的 ON CONFLICT DO UPDATE 分支：那段漏写 quality /
    vertex_normals 的话，用户改了档位重切会沉默沿用上一轮的旧值 —— 产物没变、
    全程零报错。
    """
    app_mod, _client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)

    app_mod.dem_task_manager.start_tiling(task_id, quality="speed", vertex_normals=True)
    assert _job_row(db, task_id)["quality"] == "speed"
    _finish_job(db, task_id)

    app_mod.dem_task_manager.start_tiling(task_id, quality="precision", vertex_normals=False)

    job = _job_row(db, task_id)
    assert job["quality"] == "precision"
    assert job["vertex_normals"] == 0


def test_terrain_start_falls_back_when_configured_maxzoom_is_out_of_range(monkeypatch, tmp_path):
    """配置里的越界 maxzoom 退回出厂默认 14，不靠 build_terrain 那边封顶兜。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)
    app_mod.dem_task_manager.config.set("terrain_local_maxzoom", "99")

    assert client.post(f"/api/terrain/dem/{task_id}/start").status_code == 200
    assert _job_row(db, task_id)["maxzoom"] == 14