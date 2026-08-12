"""Startup orphan-recovery for TaskManager and DemTaskManager.

After a crash/restart, DB rows still marked status='running' have no live thread
behind them. Instantiating a manager must demote those rows so the UI stops
showing them as alive.

降级还必须在**任务自己的**日志里留下解释（§4.5）。进程崩溃 / 断电 / 关窗口是
四条管线上最常发生的真实终态转移，而任务日志在崩溃那一瞬间戛然而止：最后一行
是某个瓦片的进度，既没有终态也没有任何解释，任务看起来是凭空消失的。
`dem_terrain_jobs` 与 `local_terrain_tasks` 在这里写的还是**硬终态 failed** ——
用户点开任务详情看到的原本是「失败」两个字加一片空白。
"""

import importlib
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import fresh_import  # noqa: E402


def _reload_with_isolated_db(monkeypatch, tmp_path):
    """Point Config at tmp_path and reimport database + service modules fresh."""
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in (
        "src.core.database",
        "src.services.task_manager",
        "src.services.dem_task_manager",
        "src.services.local_terrain_task_manager",
        "app",
    ):
        sys.modules.pop(mod, None)

    db = importlib.import_module("src.core.database")
    db.init_database()
    return db


def test_task_manager_recovers_orphan_running_tasks(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)

    # Seed a 'running' task as if a previous process crashed mid-download.
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles, downloaded_tiles,
               failed_tiles, started_at, total_running_seconds)
            VALUES ('orphan', 'running', 1, 0, 1, 0, 0, 0, 'satellite', 'tiles_only',
                    '/tmp', 100, 90, 10, ?, 12345)
            """,
            ((datetime.now() - timedelta(hours=110)).isoformat(),),
        )
        task_id = cur.lastrowid
        # A matching 'start' time record exists (as the real code path would write).
        cur.execute(
            "INSERT INTO task_time_records (task_id, action, timestamp) VALUES (?, 'start', ?)",
            (task_id, (datetime.now() - timedelta(hours=110)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    tm_mod = importlib.import_module("src.services.task_manager")
    tm_mod.TaskManager(socketio=None)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, total_running_seconds FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        assert row["status"] == "paused"
        # Wall-clock since 'start' must NOT have been folded in — we deliberately
        # don't call _update_total_running_time on recovery.
        assert row["total_running_seconds"] == 12345

        cur.execute(
            "SELECT action FROM task_time_records WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        actions = [r["action"] for r in cur.fetchall()]
        assert actions == ["start", "pause"]
    finally:
        conn.close()


def test_task_manager_recovery_ignores_other_statuses(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        for status in ("pending", "paused", "completed", "failed"):
            cur.execute(
                """
                INSERT INTO tasks
                  (name, status, north, south, east, west, zoom_min, zoom_max,
                   style, output_format, output_path)
                VALUES (?, ?, 1, 0, 1, 0, 0, 0, 'satellite', 'tiles_only', '/tmp')
                """,
                (status, status),
            )
        conn.commit()
    finally:
        conn.close()

    tm_mod = importlib.import_module("src.services.task_manager")
    tm_mod.TaskManager(socketio=None)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, status FROM tasks ORDER BY id")
        rows = [(r["name"], r["status"]) for r in cur.fetchall()]
    finally:
        conn.close()

    # Nothing was 'running', nothing should have changed.
    assert rows == [
        ("pending", "pending"),
        ("paused", "paused"),
        ("completed", "completed"),
        ("failed", "failed"),
    ]


def test_dem_task_manager_recovers_orphans(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path)
            VALUES ('dem-orphan', 'running', 1, 0, 1, 0, 'ASTGTM.003', '/tmp')
            """
        )
        dem_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO dem_terrain_jobs (task_id, status, output_dir, maxzoom)
            VALUES (?, 'running', '/tmp', 12)
            """,
            (dem_id,),
        )
        job_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    dtm_mod = importlib.import_module("src.services.dem_task_manager")
    dtm_mod.DemTaskManager(socketio=None)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM dem_tasks WHERE id = ?", (dem_id,))
        assert cur.fetchone()["status"] == "paused"

        cur.execute(
            "SELECT status, completed_at, error_message FROM dem_terrain_jobs WHERE id = ?",
            (job_id,),
        )
        row = cur.fetchone()
        assert row["status"] == "failed"
        assert row["completed_at"] is not None
        assert "interrupted" in (row["error_message"] or "").lower()
    finally:
        conn.close()

    # 崩溃恢复的解释必须落在**任务自己的**日志里。切片作业那一条尤其不能省：
    # 它写的是 failed —— 一个硬终态，而作业行没有 pause/resume 模型。
    from src.services.task_logging import read_task_log

    messages = "\n".join(e["message"] for e in read_task_log("dem", dem_id))
    assert "EVENT terminal status=paused reason=process_restart" in messages
    assert "EVENT terminal status=failed reason=process_restart" in messages, (
        "dem_terrain_jobs 判了 failed，任务日志里却没有任何解释")
    assert f"#{job_id}" in messages, "解释要指名是哪个切片作业"
    assert "恢复" in messages and "重新起切片" in messages, "要告诉用户下一步做什么"


def test_local_terrain_manager_recovers_orphans(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO local_terrain_tasks
              (name, status, output_path, source_dir, output_dir, maxzoom)
            VALUES ('lt-orphan', 'running', '/tmp/x', '/tmp/x/source', '/tmp/x/terrain_tiles', 14)
            """
        )
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    ltm_mod = importlib.import_module("src.services.local_terrain_task_manager")
    ltm_mod.LocalTerrainTaskManager(socketio=None)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, error_message FROM local_terrain_tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        assert row["status"] == "failed"
        assert "interrupted" in (row["error_message"] or "").lower()
    finally:
        conn.close()

    from src.services.task_logging import read_task_log

    messages = "\n".join(e["message"] for e in read_task_log("local_terrain", task_id))
    assert "EVENT terminal status=failed reason=process_restart" in messages, (
        "local_terrain_tasks 判了 failed，任务日志里却没有任何解释")
    assert "没有断点续跑" in messages, "要告诉用户为什么不能续跑"


def test_contour_task_manager_recovers_orphans(monkeypatch, tmp_path):
    """等高线：孤儿 running → paused，且任务日志里要有解释。"""
    db = _reload_with_isolated_db(monkeypatch, tmp_path)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO contour_tasks
              (name, status, north, south, east, west, contour_interval,
               zoom_min, zoom_max, output_path)
            VALUES ('contour-orphan', 'running', 1, 0, 1, 0, 50, 10, 12, '/tmp/x')
            """
        )
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    # 用 conftest.fresh_import 而不是裸 sys.modules.pop：contour_task_manager 被
    # 别的测试文件在模块级 import，裸 pop 会留下两个模块实例，而
    # tests/test_conftest_isolation_contract.py 把这条钉成了硬约束。
    ctm_mod = fresh_import(monkeypatch, "src.services.contour_task_manager")
    ctm_mod.ContourTaskManager(socketio=None)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM contour_tasks WHERE id = ?", (task_id,))
        assert cur.fetchone()["status"] == "paused"
    finally:
        conn.close()

    from src.services.task_logging import read_task_log

    messages = "\n".join(e["message"] for e in read_task_log("contour", task_id))
    assert "EVENT terminal status=paused reason=process_restart" in messages
    assert "恢复" in messages
