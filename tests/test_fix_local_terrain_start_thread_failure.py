"""local terrain: thread.start() 抛异常时任务行不得永久卡在 running。

start_tiling 在锁里把行置 running 并 commit，随后才 `th.start()`。start 失败
（线程数耗尽 / 内存压力）而不回补的话，行停在 running 且**永不恢复**：再次
start 被状态检查拒、delete 也被拒，只能重启进程靠孤儿恢复解开。界面上是一条
永远转圈、点什么都没反应的幽灵任务。

三条管线里 DEM 有 tests/test_fix_dem_start_thread_failure.py、map 有
tests/test_fix_ghost_row_on_delete.py，只有 local 这条一直没人守：实测把回补
SQL 的 `AND status='running'` 改成永不匹配的值，7 个最相关的测试文件全绿。

断言只钉可观察契约（行落 failed + 错误信息点名起因 + 登记表无残留），不钉具体
的回补辅助方法名 —— 那层实现随时可能重排。
"""

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(monkeypatch, tmp_path):
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    # 全球底图闸门会去看 BASE_DIR/assets/terrain/base_z8：不改就把「本机有没有
    # 解压过底图」变成测试结果的一部分（范本 test_local_terrain_api._reload）。
    monkeypatch.setattr(config.Config, "BASE_DIR", tmp_path)

    for mod in ("src.core.database", "src.services.local_terrain_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    mgr_mod = importlib.import_module("src.services.local_terrain_task_manager")
    return db, mgr_mod


class _BoomThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        raise RuntimeError("cannot spawn thread")

    def is_alive(self):
        return False


def test_thread_start_failure_leaves_the_row_failed_not_running(monkeypatch, tmp_path):
    db, mgr_mod = _setup(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    # 建任务这一步不要真切片：本用例要单独驱动 start_tiling。
    real_start_tiling = mgr_mod.LocalTerrainTaskManager.start_tiling
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling",
                        lambda self, task_id: None)
    task_id = mgr.create_task_with_files(
        name="local-boom", files=[("a.tif", b"fake-tif-bytes")], maxzoom=11)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling",
                        real_start_tiling)

    monkeypatch.setattr(mgr_mod.threading, "Thread", _BoomThread)

    with pytest.raises(RuntimeError):
        mgr.start_tiling(task_id)

    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT status, error_message FROM local_terrain_tasks WHERE id=?",
            (task_id,)).fetchone()
    finally:
        conn.close()

    assert row["status"] == "failed", \
        f"线程没起来却停在 {row['status']} —— 这一行再也 start/delete 不动了"
    message = row["error_message"] or ""
    assert "thread" in message.lower() and "cannot spawn thread" in message, \
        f"错误信息没点名起因，用户只看到一条无来由的失败：{message!r}"
    assert task_id not in mgr.active_tasks, "线程从未启动，active_tasks 里不该留登记"
    assert task_id not in mgr.stop_flags, "线程从未启动，stop_flags 里不该留登记"
