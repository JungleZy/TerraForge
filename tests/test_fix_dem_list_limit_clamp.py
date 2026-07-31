"""M13: dem list_tasks 的 limit 必须钳到 [1, 100]。

此前 `min(int(limit or 100), 100)` 不挡负数，SQLite LIMIT -1 表示无上限，
可绕过上限返回全表。修复后与 routes/api.py get_tasks 同一约定：
<1 或 >100 都回退到默认窗口 100。
"""

import importlib
import os
import sys

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


def _seed_tasks(db, count):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.executemany(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path, total_files)
            VALUES ('t', 'completed', 1, 0, 1, 0, 'COP-DEM-GLO-30', '/tmp/x', 1)
            """,
            [() for _ in range(count)],
        )
        conn.commit()
    finally:
        conn.close()


def test_list_tasks_negative_limit_clamped(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    _seed_tasks(db, 105)

    # LIMIT -1 在 SQLite 里是无上限 —— 必须被钳回上限 100，而不是返回全表
    assert len(mgr.list_tasks(limit=-1)) == 100
    assert len(mgr.list_tasks(limit=0)) == 100


def test_list_tasks_normal_limit_respected(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    _seed_tasks(db, 10)

    assert len(mgr.list_tasks(limit=5)) == 5
    assert len(mgr.list_tasks(limit=500)) == 10  # 钳到 100，但表里只有 10 行
    assert len(mgr.list_tasks(limit=None)) == 10
