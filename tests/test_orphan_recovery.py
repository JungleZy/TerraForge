"""Startup orphan-recovery for TaskManager and DemTaskManager.

After a crash/restart, DB rows still marked status='running' have no live thread
behind them. Instantiating a manager must demote those rows so the UI stops
showing them as alive.
"""

import importlib
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _reload_with_isolated_db(monkeypatch, tmp_path):
    """Point Config at tmp_path and reimport database + service modules fresh."""
    from core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in (
        "core.database",
        "services.task_manager",
        "services.dem_task_manager",
        "services.local_terrain_task_manager",
        "app",
    ):
        sys.modules.pop(mod, None)

    db = importlib.import_module("core.database")
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

    tm_mod = importlib.import_module("services.task_manager")
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
        for status in ("pending", "paused", "completed", "failed", "cancelled"):
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

    tm_mod = importlib.import_module("services.task_manager")
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
        ("cancelled", "cancelled"),
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

    dtm_mod = importlib.import_module("services.dem_task_manager")
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

    ltm_mod = importlib.import_module("services.local_terrain_task_manager")
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
