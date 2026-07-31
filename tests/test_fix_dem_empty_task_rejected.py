"""M11: 完全落在数据覆盖范围外的选区不得创建 total_files=0 的空任务。

ASTGTM.003 只覆盖 83°S–83°N（dem_granules 对 |lat|>83 返回空列表）；
此前 create_task 不检查 total_files==0，会创建无事可做却"成功完成"的空任务。
修复：total_files==0 时 create_task 抛 ValueError，且不落库。
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


def test_create_task_fully_out_of_coverage_rejected(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)

    with pytest.raises(ValueError) as excinfo:
        mgr.create_task({
            "name": "polar", "north": 90.0, "south": 84.5, "east": 1.0, "west": 0.0,
            "dataset": "ASTGTM.003",
        })
    assert "coverage" in str(excinfo.value) or "no" in str(excinfo.value).lower()

    # 不得留下任何任务/文件行
    conn = db.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM dem_tasks").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM dem_files").fetchone()["c"] == 0
    finally:
        conn.close()


def test_create_task_partially_covered_still_created(monkeypatch, tmp_path):
    """对照：选区与覆盖范围有交集时照常创建（只含覆盖内的颗粒）。"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)

    task_id = mgr.create_task({
        "name": "edge", "north": 85.0, "south": 83.5, "east": 1.0, "west": 0.0,
        "dataset": "ASTGTM.003",
    })

    conn = db.get_connection()
    try:
        task = conn.execute("SELECT * FROM dem_tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert task is not None
    assert task["total_files"] == 1  # 只有 N83 一颗
