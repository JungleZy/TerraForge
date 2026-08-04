import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload):
        self.events.append((event, payload))


def _reload_with_isolated_db(monkeypatch, tmp_path):
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in (
        "src.core.database",
        "src.services.task_manager",
        "src.services.dem_task_manager",
        "app",
    ):
        sys.modules.pop(mod, None)

    db = importlib.import_module("src.core.database")
    db.init_database()
    return db


def _seed_map_task(db, status, output_path="/tmp"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles,
               downloaded_tiles, failed_tiles)
            VALUES ('map-task', ?, 1, 0, 1, 0, 0, 0, 'satellite',
                    'tiles_only', ?, 0, 0, 0)
            """,
            (status, output_path),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _seed_dem_task(db, status, output_path="/tmp"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('dem-task', ?, 1, 0, 1, 0, 'ASTGTM.003', ?, 0, 0, 0)
            """,
            (status, output_path),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _seed_contour_task(db, status, output_path="/tmp"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO contour_tasks
              (name, status, north, south, east, west, dataset,
               contour_interval, zoom_min, zoom_max, output_path, total_files)
            VALUES ('contour-task', ?, 1, 0, 1, 0, 'ASTGTM.003',
                    50, 12, 14, ?, 0)
            """,
            (status, output_path),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _task_status(db, table, task_id):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT status FROM {table} WHERE id=?", (task_id,))
        return cur.fetchone()["status"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# cancel 状态机：终态（completed/failed）不得被 cancel 改写
#
# 缺陷：cancel 的 UPDATE 条件是 status != 'cancelled'，对已 completed/failed
# 的任务调 cancel 会把终态记录改写成 cancelled —— 完成记录被抹掉、历史统计
# 失真。cancel 只允许 pending/running/paused → cancelled。
# ---------------------------------------------------------------------------


def test_map_cancel_completed_task_keeps_status(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = _seed_map_task(db, status="completed")

    # 终态任务无可取消:如实抛 ValueError(API 层映射 400),状态保持不动 ——
    # 而不是什么都不改却记一条 'cancelled' 日志、返回 success。
    with pytest.raises(ValueError, match="Cannot cancel"):
        tm.cancel_task(task_id)

    assert _task_status(db, "tasks", task_id) == "completed"


def test_map_cancel_failed_task_keeps_status(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = _seed_map_task(db, status="failed")

    with pytest.raises(ValueError, match="Cannot cancel"):
        tm.cancel_task(task_id)

    assert _task_status(db, "tasks", task_id) == "failed"


def test_map_cancel_pending_task_marks_cancelled(monkeypatch, tmp_path):
    """对照：pending 任务仍然可以被取消。"""
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = _seed_map_task(db, status="pending")

    tm.cancel_task(task_id)

    assert _task_status(db, "tasks", task_id) == "cancelled"


def test_dem_cancel_completed_task_keeps_status(monkeypatch, tmp_path):
    """终态任务 cancel 抛 ValueError（与 pause_task 行为对齐），状态不变。"""
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    dtm_mod = importlib.import_module("src.services.dem_task_manager")
    dtm = dtm_mod.DemTaskManager(socketio=FakeSocketIO())
    task_id = _seed_dem_task(db, status="completed")

    with pytest.raises(ValueError, match="Cannot cancel DEM task"):
        dtm.cancel_task(task_id)

    assert _task_status(db, "dem_tasks", task_id) == "completed"


def test_dem_cancel_failed_task_keeps_status(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    dtm_mod = importlib.import_module("src.services.dem_task_manager")
    dtm = dtm_mod.DemTaskManager(socketio=FakeSocketIO())
    task_id = _seed_dem_task(db, status="failed")

    with pytest.raises(ValueError, match="Cannot cancel DEM task"):
        dtm.cancel_task(task_id)

    assert _task_status(db, "dem_tasks", task_id) == "failed"


def test_dem_cancel_pending_task_marks_cancelled(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    dtm_mod = importlib.import_module("src.services.dem_task_manager")
    dtm = dtm_mod.DemTaskManager(socketio=FakeSocketIO())
    task_id = _seed_dem_task(db, status="pending")

    dtm.cancel_task(task_id)

    assert _task_status(db, "dem_tasks", task_id) == "cancelled"


def test_contour_cancel_completed_task_keeps_status(monkeypatch, tmp_path):
    """U2:终态任务 cancel 抛 ValueError(与 map/dem 对齐),状态不变。

    此前 contour 是唯一一条静默 fall through 的管线 —— rowcount==0 且行存在时
    什么都不改就返回,路由照回 {"success": true},用户以为取消生效了。
    """
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    ctm_mod = importlib.import_module("src.services.contour_task_manager")
    ctm = ctm_mod.ContourTaskManager(socketio=FakeSocketIO())
    task_id = _seed_contour_task(db, status="completed")

    with pytest.raises(ValueError, match="Cannot cancel contour task"):
        ctm.cancel_task(task_id)

    assert _task_status(db, "contour_tasks", task_id) == "completed"


def test_contour_cancel_failed_task_keeps_status(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    ctm_mod = importlib.import_module("src.services.contour_task_manager")
    ctm = ctm_mod.ContourTaskManager(socketio=FakeSocketIO())
    task_id = _seed_contour_task(db, status="failed")

    with pytest.raises(ValueError, match="Cannot cancel contour task"):
        ctm.cancel_task(task_id)

    assert _task_status(db, "contour_tasks", task_id) == "failed"


def test_contour_cancel_pending_task_marks_cancelled(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    ctm_mod = importlib.import_module("src.services.contour_task_manager")
    ctm = ctm_mod.ContourTaskManager(socketio=FakeSocketIO())
    task_id = _seed_contour_task(db, status="pending")

    ctm.cancel_task(task_id)

    assert _task_status(db, "contour_tasks", task_id) == "cancelled"
