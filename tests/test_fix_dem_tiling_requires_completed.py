"""M16: start_tiling 只接受下载已完成（completed）的任务；路由区分 400/500。

- pending/running 的下载任务触发 tiling 会在残缺数据上"成功"产出错误地形，
  manager 层必须拒绝（ValueError）；
- 路由把客户端错误（ValueError→400）与服务器内部错误（→500 并记日志）分开，
  不再一律 400；get_dem_tiling_job 补一致的错误处理。
"""

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup_manager(monkeypatch, tmp_path):
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


def _load_app(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database", "services.dem_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _seed_dem_task(db, output_path, status):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path, total_files)
            VALUES ('t', ?, 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 1)
            """,
            (status, str(output_path)),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# manager 层：非 completed 拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "running", "paused", "failed", "cancelled"])
def test_start_tiling_rejects_non_completed_task(monkeypatch, tmp_path, status):
    db, dtm = _setup_manager(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out", status)

    with pytest.raises(ValueError) as excinfo:
        mgr.start_tiling(task_id)
    assert status in str(excinfo.value)

    # 不得创建 job 行
    assert mgr.get_tiling_job(task_id) is None


def test_start_tiling_accepts_completed_task(monkeypatch, tmp_path):
    db, dtm = _setup_manager(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out", "completed")

    monkeypatch.setattr(
        dtm.DemTaskManager, "_run_tiling_job",
        lambda self, task_id, task_dir, output_dir, maxzoom, parent_url: None,
    )
    mgr.start_tiling(task_id)

    job = mgr.get_tiling_job(task_id)
    assert job is not None
    assert job["status"] == "running"


# ---------------------------------------------------------------------------
# 路由层：ValueError→400，内部错误→500
# ---------------------------------------------------------------------------


def test_route_start_tiling_non_completed_returns_400(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("core.database")
    task_id = _seed_dem_task(db, tmp_path / "out", "running")

    resp = client.post(f"/api/terrain/dem/{task_id}/start")
    assert resp.status_code == 400
    assert "running" in resp.get_json()["error"]


def test_route_start_tiling_internal_error_returns_500(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)

    def boom(task_id):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(app_mod.dem_task_manager, "start_tiling", boom)

    resp = client.post("/api/terrain/dem/1/start")
    assert resp.status_code == 500
    # 内部错误细节不外泄为客户端错误描述
    assert resp.get_json()["error"] == "Failed to start DEM tiling"


def test_route_get_tiling_job_internal_error_returns_500(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)

    def boom(task_id):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(app_mod.dem_task_manager, "get_tiling_job", boom)

    resp = client.get("/api/terrain/dem/1")
    assert resp.status_code == 500
    assert resp.get_json()["error"] == "Failed to get DEM tiling job"
