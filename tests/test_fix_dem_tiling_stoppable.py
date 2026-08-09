"""DEM 地形切片必须能被中途停止。

TileParams.stop_flag 字段、tile_dem_task_dir 里把它透传给 build_terrain 的
`stop_flag=params.stop_flag`、build_terrain 的逐瓦片检查（串行分支每瓦片、
并行分支批间各一处 `stop_flag.is_set()`）全都是
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
    什么都没产出」准备的失败判据。中途停止会让它误命中：error_message 写成
    "produced no tiles"，而真实原因是用户主动停的。
    local_terrain_task_manager._run_tiling_job 早就有逐字对应的豁免。

    钉的是**这条判据没误命中**，不是终态：本用例没人删这一行（真实场景里删除
    已经把它 CASCADE 掉了），活下来的行会被 finally 的搁死补偿判 failed。
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
    assert "produced no tiles" not in (job["error_message"] or ""), (
        f"中途停止被「切片器什么都没产出」判据误命中，error={job['error_message']}")
    # 中途停止也不能报 completed —— 产物是残缺的
    assert job["status"] != "completed", (
        "中途停止的切片不能记 completed（产物残缺）")


def test_stopped_tiling_does_not_leave_the_job_row_running(monkeypatch, tmp_path):
    """中途停止不写 completed、不广播终态；行要是还活着，必须被搁死补偿判 failed。

    DEM 切片没有暂停语义，能置这个 flag 的只有「删除任务」。正常情况下 dem_tasks
    行连同 CASCADE 的 dem_terrain_jobs 行都已经没了，补偿是静默 no-op。但
    task_deletion.delete_task_row 的 commit 失败分支回滚 DELETE 却【不】回滚停止
    标志（有意的，重试删除仍要它停）—— 行就这么活下来，而切片线程走 stop 分支
    **正常** return，没有异常给兜底 except 接。此前的契约是「这时什么都不写」，
    留下的正是一条永久 running 的 job 行：start_tiling 的 `status != 'running'`
    闸门一直判「已在运行」，terrain_api 又没有重置 job 的端点，只有重启进程能解开。
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
    assert job["status"] == "failed", (
        f"停止标志置位后行还活着却没落终态，实际 {job['status']} —— "
        "job 行永久 running，用户点不动「开始切片」")
    assert "运行中" in (job["error_message"] or ""), (
        f"要向用户说清行为什么被判失败，实际 error={job['error_message']}")
    finished = [p for e, p in emitted
                if e == "terrain_job_progress" and p.get("status") in ("completed", "failed")]
    assert finished == [], f"中途停止不该广播终态，实际 {finished}"
