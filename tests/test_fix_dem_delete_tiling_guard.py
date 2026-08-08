"""删除 DEM 任务：任何状态都能删 —— 下载中、切片中都不再拒绝。

切片线程自 v0.2.11 起也登记进 active_tasks / stop_flags（见 start_tiling），
「切片中删除」因此和「下载中删除」走同一条后台路径：当场删行 + 置停止标志，
产物等线程收工后由 task_deletion 的后台收尾删。本文件钉住这条语义，外加
dem_terrain_jobs 行随 ON DELETE CASCADE 一起消失。
"""

import importlib
import os
import sys
import threading

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


def test_delete_while_tiling_job_running(monkeypatch, tmp_path):
    """dem_terrain_jobs 还是 running —— 照样删，两表行都消失。

    active_tasks 里没有登记（模拟进程重启后残留的孤儿 job 行），所以走同步
    快路径；job 行由 ON DELETE CASCADE 带走。
    """
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out", status="completed")
    _seed_tiling_job(db, task_id, "running")

    outcome = mgr.delete_task(task_id)

    assert outcome.row_deleted is True
    assert not _row_exists(db, "dem_tasks", "id=?", (task_id,))
    assert not _row_exists(db, "dem_terrain_jobs", "task_id=?", (task_id,))


def test_delete_with_active_thread_stops_it_and_drops_row(monkeypatch, tmp_path):
    """active_tasks 里有存活线程 —— 行当场消失，且工作线程的停止标志被置上。

    停止标志是「运行中删除」区别于快路径的唯一同步可观察点：产物清理挪到了
    后台线程，行删除两条路径都做。
    """
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out", status="paused")

    gate = threading.Event()
    th = threading.Thread(target=lambda: gate.wait(timeout=30), daemon=True)
    th.start()
    stop_flag = threading.Event()
    mgr.active_tasks[task_id] = th
    mgr.stop_flags[task_id] = stop_flag
    try:
        outcome = mgr.delete_task(task_id)

        assert outcome.row_deleted is True
        assert not _row_exists(db, "dem_tasks", "id=?", (task_id,))
        assert stop_flag.is_set(), "运行中删除必须让工作线程停下来"
    finally:
        gate.set()
        th.join(timeout=5)


def test_delete_running_task(monkeypatch, tmp_path):
    """DB 状态是 running 但没有活线程（进程重启后的孤儿行）—— 同步删掉。"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out", status="running")

    outcome = mgr.delete_task(task_id)

    assert outcome.row_deleted is True
    assert not _row_exists(db, "dem_tasks", "id=?", (task_id,))


def test_delete_allows_after_tiling_finished(monkeypatch, tmp_path):
    """tiling job 已 completed —— 允许删除，job 行随 CASCADE 一起消失。"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out", status="completed")
    _seed_tiling_job(db, task_id, "completed")

    mgr.delete_task(task_id)

    assert not _row_exists(db, "dem_tasks", "id=?", (task_id,))
    assert not _row_exists(db, "dem_terrain_jobs", "task_id=?", (task_id,))


def test_delete_not_found_reports_row_not_deleted(monkeypatch, tmp_path):
    """行不存在不再抛 ValueError —— 共享助手返回 row_deleted=False，
    由路由层翻成 404（见 test_http_delete_not_found_returns_404）。"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)

    outcome = mgr.delete_task(9999)

    assert outcome.row_deleted is False


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


def test_http_delete_while_tiling_returns_200(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _seed_dem_task(db, tmp_path / "out", status="completed")
    _seed_tiling_job(db, task_id, "running")

    resp = client.delete(f"/api/dem/tasks/{task_id}")

    assert resp.status_code == 200, resp.get_json()
    assert not _row_exists(db, "dem_tasks", "id=?", (task_id,))
    assert not _row_exists(db, "dem_terrain_jobs", "task_id=?", (task_id,))


def test_http_delete_running_task_returns_200(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _seed_dem_task(db, tmp_path / "out", status="running")

    resp = client.delete(f"/api/dem/tasks/{task_id}")

    assert resp.status_code == 200, resp.get_json()
    assert not _row_exists(db, "dem_tasks", "id=?", (task_id,))


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
