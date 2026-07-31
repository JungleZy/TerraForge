"""M6(dem): thread.start() 抛异常时任务不得卡死在 running。

commit（status=running）与 th.start() 之间若 start 失败，此前会留下
"DB 是 running、线程从未启动"的死任务。修复：start 失败时状态回退为
paused（可重新 start/resume），并清理 active_tasks/stop_flags 登记。
"""

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(monkeypatch, tmp_path):
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


def test_thread_start_failure_reverts_running_status(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = mgr.create_task({
        "name": "t", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
    })

    class _BoomThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot spawn thread")

        def is_alive(self):
            return False

    monkeypatch.setattr(dtm.threading, "Thread", _BoomThread)

    with pytest.raises(RuntimeError):
        mgr.start_task(task_id)

    conn = db.get_connection()
    try:
        row = conn.execute("SELECT status FROM dem_tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()

    assert row["status"] != "running", f"start 失败不得卡在 running，实际: {row['status']}"
    assert row["status"] == "paused"  # paused 可经 start_task/resume_task 重启
    assert task_id not in mgr.active_tasks
    assert task_id not in mgr.stop_flags
