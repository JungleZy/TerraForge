"""I2: start_tiling 的检查/upsert/起线程三步必须原子（TOCTOU 竞态）。

照抄 start_task 的范本：锁内条件 UPDATE + rowcount 判定。
并发调用同一任务的 start_tiling 只能有一个成功，其余必须 ValueError。
"""

import importlib
import os
import sys
import threading
import time

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


def _seed_dem_task(db, output_path):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('t', 'completed', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 0, 0, 0)
            """,
            (str(output_path),),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_start_tiling_concurrent_calls_only_one_wins(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out")

    # tiling 任务挂住，保证整个竞态窗口内 job 一直是 'running'
    gate = threading.Event()

    def fake_tile(task_dir, out_dir, params):
        gate.wait(timeout=30)

    monkeypatch.setattr(dtm, "tile_dem_task_dir", fake_tile)

    # 把 datetime.now 变慢，确定性撑大「检查 → upsert」之间的竞态窗口：
    # 无锁时所有线程都会在第一个提交前通过检查；有锁时被串行化。
    real_datetime = dtm.datetime

    class _SlowDatetime:
        @staticmethod
        def now():
            time.sleep(0.05)
            return real_datetime.now()

    monkeypatch.setattr(dtm, "datetime", _SlowDatetime)

    barrier = threading.Barrier(8)
    results = {"ok": 0, "err": 0}
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait(timeout=10)
            mgr.start_tiling(task_id)
            with lock:
                results["ok"] += 1
        except ValueError:
            with lock:
                results["err"] += 1

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    gate.set()

    assert results["ok"] == 1, (
        f"并发 start_tiling 必须只有 1 个成功，实际 {results}"
    )
    assert results["err"] == 7

    # 清理：等胜出的 tiling job 跑完，避免后台线程泄漏到后续测试
    deadline = time.time() + 10
    while time.time() < deadline:
        job = mgr.get_tiling_job(task_id)
        if job and job["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
