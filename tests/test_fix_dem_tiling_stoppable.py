"""DEM 地形切片必须能被中途停止。

TileParams.stop_flag 字段（dem_task_tiler.py:55）、tile_dem_task_dir 的透传
（:136）、build_terrain 的逐瓦片检查（cesiumlab_terrain.py:1427/1446）全都是
现成的 —— 缺的一直是 dem_task_manager 这个调用方。后果不止「停不下来」：切片
线程不进 active_tasks，delete_task 的 is_alive() 守卫对它完全无效。
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


def _seed_completed_dem_task(db, output_path):
    """切片只接受 completed 的下载任务（M16），所以种子行必须是 completed。"""
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('t', 'completed', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 1, 1, 0)
            """,
            (str(output_path),),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_tiling_thread_receives_stop_flag_and_is_registered(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_completed_dem_task(db, tmp_path / "out")

    entered = threading.Event()
    release = threading.Event()
    seen = {}

    def fake_tiler(task_dir, out_dir, params, build_terrain_fn=None):
        seen["stop_flag"] = params.stop_flag
        entered.set()
        release.wait(timeout=10)
        return {"total": 4, "rendered": 4, "failed": 0}

    monkeypatch.setattr(dtm, "tile_dem_task_dir", fake_tiler)

    mgr.start_tiling(task_id)
    assert entered.wait(timeout=10), "切片线程没跑起来"

    # 线程还卡在 fake_tiler 里 —— 此刻两个登记表都必须看得见它
    assert isinstance(seen["stop_flag"], threading.Event), (
        f"TileParams.stop_flag 必须是 Event，实际 {seen['stop_flag']!r}")
    assert task_id in mgr.stop_flags, "切片的 stop_flag 必须登记，否则没人能置位"
    th = mgr.active_tasks.get(task_id)
    assert th is not None and th.is_alive(), (
        "切片线程必须登记进 active_tasks，否则 delete_task 的 is_alive() 守卫形同虚设")

    release.set()
    th.join(timeout=10)
    assert not th.is_alive()

    # 收尾后两个登记表都要清干净，别把 key 泄漏给下一轮
    assert task_id not in mgr.active_tasks
    assert task_id not in mgr.stop_flags


def test_stopped_tiling_with_zero_rendered_is_not_a_failure(monkeypatch, tmp_path):
    """刚开始切就被停掉时 rendered 本来就是 0 —— 不能误判成「一张瓦片都没切出来」。

    dem_task_manager 的 `if total > 0 and rendered == 0: raise` 是给「切片器真的
    什么都没产出」准备的失败判据。中途停止会让它误命中：作业被记 failed、
    error_message 写成 "produced no tiles"，而真实原因是用户主动停的。
    local_terrain_task_manager.py:482 早就有逐字对应的豁免。
    """
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_completed_dem_task(db, tmp_path / "out")

    def fake_tiler(task_dir, out_dir, params, build_terrain_fn=None):
        # 模拟 build_terrain 看到 stop_flag 立刻收工：一张都没渲染
        params.stop_flag.set()
        return {"total": 100, "rendered": 0, "failed": 0}

    monkeypatch.setattr(dtm, "tile_dem_task_dir", fake_tiler)

    mgr.start_tiling(task_id)
    th = mgr.active_tasks.get(task_id)
    if th is not None:
        th.join(timeout=10)

    job = mgr.get_tiling_job(task_id)
    assert job["status"] != "failed", (
        f"中途停止不是失败，实际 status={job['status']} error={job['error_message']}")
    # 中途停止也不能报 completed —— 产物是残缺的
    assert job["status"] != "completed", (
        "中途停止的切片不能记 completed（产物残缺）")


def test_stopped_tiling_writes_no_terminal_state(monkeypatch, tmp_path):
    """中途停止时不写状态、不广播 —— 置位的唯一入口是删除，行已经不在了。

    DEM 切片没有暂停语义。改造后能置这个 flag 的只有「删除任务」，那时
    dem_tasks 行连同 CASCADE 的 dem_terrain_jobs 行都已经没了：UPDATE 是静默
    no-op，emit 也没有行可更新。写死这条契约，免得后来者「补上遗漏的状态迁移」。
    """
    db, dtm = _setup(monkeypatch, tmp_path)

    emitted = []

    class _Sock:
        def emit(self, event, payload=None):
            emitted.append((event, payload))

    mgr = dtm.DemTaskManager(socketio=_Sock())
    task_id = _seed_completed_dem_task(db, tmp_path / "out")

    def fake_tiler(task_dir, out_dir, params, build_terrain_fn=None):
        params.stop_flag.set()
        return {"total": 10, "rendered": 3, "failed": 0}

    monkeypatch.setattr(dtm, "tile_dem_task_dir", fake_tiler)

    mgr.start_tiling(task_id)
    th = mgr.active_tasks.get(task_id)
    if th is not None:
        th.join(timeout=10)

    job = mgr.get_tiling_job(task_id)
    assert job["status"] == "running", (
        f"中途停止不该改写 job 状态，实际 {job['status']}")
    assert job["completed_at"] is None
    finished = [p for e, p in emitted
                if e == "terrain_job_progress" and p.get("status") in ("completed", "failed")]
    assert finished == [], f"中途停止不该广播终态，实际 {finished}"
