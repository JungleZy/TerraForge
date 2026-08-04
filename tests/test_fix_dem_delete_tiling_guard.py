"""HIGH #3: 删除 DEM 任务必须在 manager 层锁内统一复查下载线程 + 任务状态 +
tiling job 状态 —— 下载中或 tiling 中的任务拒删，否则 rmtree 会删掉正在被
GDAL 写入的 terrain_tiles/，dem_terrain_jobs 行被 ON DELETE CASCADE 静默删除。
"""

import importlib
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database", "src.services.dem_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    dtm = importlib.import_module("src.services.dem_task_manager")
    return db, dtm


def _seed_dem_task(db, output_path, status="completed"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('t', ?, 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 0, 0, 0)
            """,
            (status, str(output_path)),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _seed_tiling_job(db, task_id, status):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_terrain_jobs (task_id, status, output_dir, maxzoom, parent_url)
            VALUES (?, ?, '/tmp/x/terrain_tiles', 14, 'http://localhost:5000/terrain/base/layer.json')
            """,
            (task_id, status),
        )
        conn.commit()
    finally:
        conn.close()


def _row_exists(db, table, where, args):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {table} WHERE {where}", args)
        return cur.fetchone() is not None
    finally:
        conn.close()


def test_delete_refuses_while_tiling_job_running(monkeypatch, tmp_path):
    """任务本身已 completed，但 tiling job 正在跑 —— 必须拒删且两表行都保留。"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out", status="completed")
    _seed_tiling_job(db, task_id, "running")

    with pytest.raises(ValueError):
        mgr.delete_task(task_id)

    assert _row_exists(db, "dem_tasks", "id=?", (task_id,))
    assert _row_exists(db, "dem_terrain_jobs", "task_id=?", (task_id,))


def test_delete_refuses_active_download_thread(monkeypatch, tmp_path):
    """DB 状态是 paused，但 active_tasks 里有存活下载线程 —— 必须拒删。"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out", status="paused")

    gate = threading.Event()
    th = threading.Thread(target=lambda: gate.wait(timeout=30), daemon=True)
    th.start()
    mgr.active_tasks[task_id] = th
    try:
        with pytest.raises(ValueError):
            mgr.delete_task(task_id)
        assert _row_exists(db, "dem_tasks", "id=?", (task_id,))
    finally:
        gate.set()
        th.join(timeout=5)


def test_delete_refuses_running_task(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out", status="running")

    with pytest.raises(ValueError):
        mgr.delete_task(task_id)
    assert _row_exists(db, "dem_tasks", "id=?", (task_id,))


def test_delete_allows_after_tiling_finished(monkeypatch, tmp_path):
    """tiling job 已 completed —— 允许删除，job 行随 CASCADE 一起消失。"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out", status="completed")
    _seed_tiling_job(db, task_id, "completed")

    mgr.delete_task(task_id)

    assert not _row_exists(db, "dem_tasks", "id=?", (task_id,))
    assert not _row_exists(db, "dem_terrain_jobs", "task_id=?", (task_id,))


def test_delete_not_found_raises(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)

    with pytest.raises(ValueError):
        mgr.delete_task(9999)


# ---------------------------------------------------------------------------
# 路由层：DELETE /api/dem/tasks/<id> 返回码约定
# ---------------------------------------------------------------------------


def _load_app(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in (
        "app",
        "src.core.database",
        "src.services.dem_task_manager",
        "src.routes.dem_api",
    ):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def test_http_delete_refuses_while_tiling(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _seed_dem_task(db, tmp_path / "out", status="completed")
    _seed_tiling_job(db, task_id, "running")

    resp = client.delete(f"/api/dem/tasks/{task_id}")

    assert resp.status_code == 400
    assert _row_exists(db, "dem_tasks", "id=?", (task_id,))
    assert _row_exists(db, "dem_terrain_jobs", "task_id=?", (task_id,))


def test_http_delete_running_task_returns_400(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _seed_dem_task(db, tmp_path / "out", status="running")

    resp = client.delete(f"/api/dem/tasks/{task_id}")

    assert resp.status_code == 400
    assert _row_exists(db, "dem_tasks", "id=?", (task_id,))


def test_http_delete_not_found_returns_404(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)

    resp = client.delete("/api/dem/tasks/9999")

    assert resp.status_code == 404


def test_http_delete_finished_task_returns_200(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _seed_dem_task(db, tmp_path / "out", status="completed")
    _seed_tiling_job(db, task_id, "failed")

    resp = client.delete(f"/api/dem/tasks/{task_id}")

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert not _row_exists(db, "dem_tasks", "id=?", (task_id,))
    assert not _row_exists(db, "dem_terrain_jobs", "task_id=?", (task_id,))
