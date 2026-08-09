"""ContourTaskManager._execute 的行为测试。

2026-08-08 评审之后，_execute 只服务本地源（dataset='upload' / 'dem_task'）：
下载驱动的那半程连同它的 create_task 一起删掉了（约 110 行从 dem_task_manager
拷来的代码，没有任何用户可达路径能执行到它，因此一直在无声漂移）。存量下载行
由一行守卫直接判失败。

因此本文件里的种子行从 `dataset='COP-DEM-GLO-30'` 换成了 `dataset='upload'`：
被断言的不变量（进度节流、载荷形态、删除后不许再推送、暂停不被误伤、收尾列语义）
与数据源无关，只有「怎么进到渲染阶段」变了。

随之删掉的用例（它们的被测对象整段不存在了，留着就是在测死代码）：
* test_execute_total_tiles_covers_whole_dem_not_bbox —— 按 1° granule 并集预算
  total_tiles 是下载路径独有的；本地源的 total 由引擎 warp 完按实际覆盖上报，
  由本文件的节流用例断言（total_tiles == 100 来自 progress_cb）。
* test_execute_water_download_failure_is_not_fatal / ASTWBD 下载支线。
* test_download_progress_local_path_uses_flat_basename —— 扁平 basename 是
  dem_download_engine 的落盘约定，只有下载回调会写 local_path。
* test_in_flight_bytes_drive_download_speed —— 渲染阶段没有网络字节；
  SpeedMeter 的契约由 tests/test_download_speed.py 与 DEM 侧用例守。
* test_deleted_task_emits_no_further_download_progress —— 下载阶段不复存在。
"""

import asyncio
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    ctm_mod = importlib.import_module("src.services.contour_task_manager")
    return db, ctm_mod


def _seed_running_task(db, background=None, dataset="upload",
                       file_statuses=("completed",)):
    """一条 running 的等高线任务行 + 它的 contour_files 行。

    默认形态对齐 create_task_with_files 建出来的行：dataset='upload'、water=0、
    文件行建的时候就是 completed（没有下载阶段）。dataset 可以传别的值来构造
    存量下载行，专给守卫用例。
    """
    from pathlib import Path

    from src.core import config
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO contour_tasks (
                name, status, north, south, east, west, dataset,
                contour_interval, background, terrain_shade, water,
                zoom_min, zoom_max, output_path,
                total_files, downloaded_files, failed_files,
                total_tiles, rendered_tiles, failed_tiles
            )
            VALUES ('t', 'running', 1.0, 0.0, 1.0, 0.0, ?, 50, ?, 1, 0,
                    12, 12, ?, ?, ?, 0, 0, 0, 0)
            """,
            (dataset, background or "#FAF6EC",
             str(Path(config.Config.DOWNLOADS_DIR) / "dem"),
             len(file_statuses),
             sum(1 for s in file_statuses if s == "completed")),
        )
        task_id = cur.lastrowid
        cur.executemany(
            "INSERT INTO contour_files (task_id, granule_id, kind, status, retry_count)"
            " VALUES (?, ?, 'dem', ?, 0)",
            [(task_id, f"upload_{i}_dem.tif", s)
             for i, s in enumerate(file_statuses, start=1)],
        )
        conn.commit()
        return task_id
    finally:
        conn.close()


def _patch_tiler(monkeypatch, fn):
    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fn)


def test_execute_completes_after_render(monkeypatch, tmp_path):
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = _seed_running_task(db, background="transparent")

    called = {"render": False, "background": None}

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        called["render"] = True
        called["background"] = params.style.background
        if progress_cb:
            progress_cb(3, 3)
        return {"total": 3, "rendered": 3, "failed": 0}

    _patch_tiler(monkeypatch, fake_tiler)

    asyncio.run(mgr._execute(task_id, None))

    task = mgr.get_task(task_id)
    assert called["render"] is True
    assert called["background"] == "transparent"
    assert task["status"] == "completed"
    assert task["rendered_tiles"] == 3


def test_execute_fails_legacy_download_driven_row_with_clear_message(monkeypatch, tmp_path):
    """存量下载驱动的行（dataset 为 ASTGTM.003 / COP-DEM-GLO-30）必须当场判失败，
    错误信息说清「这是已移除的下载驱动类型 + 该怎么办」，且**不得**进渲染。

    以前这类行会走那段没人能创建、因此从没被执行过的下载拷贝。留一行守卫比留
    一份跑不起来的拷贝诚实：拷贝不会报错，只会继续漂移。
    """
    import pytest

    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = _seed_running_task(db, dataset="ASTGTM.003",
                                 file_statuses=("pending",))

    called = {"render": False}

    def fake_tiler(*a, **k):
        called["render"] = True
        return {"total": 0, "rendered": 0, "failed": 0}

    _patch_tiler(monkeypatch, fake_tiler)

    with pytest.raises(ValueError, match="ASTGTM.003"):
        asyncio.run(mgr._execute(task_id, None))

    task = mgr.get_task(task_id)
    assert task["status"] == "failed"
    assert "ASTGTM.003" in (task["error_message"] or "")
    # 错误信息必须给出出路，而不只是「不支持」
    assert "数据下载" in (task["error_message"] or "")
    assert called["render"] is False


def test_execute_passes_shade_water_flags_to_tiler(monkeypatch, tmp_path):
    # terrain_shade/water 两列必须透传给渲染参数（种子行 shade=1 / water=0）。
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = _seed_running_task(db)

    seen = {}

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        seen["shade"] = params.shade
        seen["water"] = params.water
        return {"total": 1, "rendered": 1, "failed": 0}

    _patch_tiler(monkeypatch, fake_tiler)

    asyncio.run(mgr._execute(task_id, None))

    assert seen["shade"] is True and seen["water"] is False
    assert mgr.get_task(task_id)["status"] == "completed"


def test_execute_fails_and_skips_render_on_incomplete_dem_files(monkeypatch, tmp_path):
    """有 failed / 未终结的 DEM 文件行时必须判失败并跳过渲染 —— 在缺文件的输入上
    渲染会「成功」产出带缺口的瓦片，而任务报完成。"""
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = _seed_running_task(db, file_statuses=("completed", "failed"))

    called = {"render": False}

    def fake_tiler(*a, **k):
        called["render"] = True
        return {"total": 0, "rendered": 0, "failed": 0}

    _patch_tiler(monkeypatch, fake_tiler)

    asyncio.run(mgr._execute(task_id, None))

    task = mgr.get_task(task_id)
    assert task["status"] == "failed"
    assert called["render"] is False


def test_render_progress_is_throttled_and_payload_stays_compatible(monkeypatch, tmp_path):
    # 逐瓦片回调不再每瓦片落库+广播:节流窗口内只记内存,100 次回调只产生
    # 初始/100%/收尾三次 render 事件;载荷保持前端消费的全行字段形态。
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(ctm_mod, "_RENDER_PROGRESS_MIN_INTERVAL", 3600)

    events = []

    class FakeSocket:
        def emit(self, event, payload):
            events.append((event, dict(payload)))

    mgr = ctm_mod.ContourTaskManager(socketio=FakeSocket())
    task_id = _seed_running_task(db)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        for i in range(1, 101):
            progress_cb(i, 100)
        return {"total": 100, "rendered": 95, "failed": 5}

    _patch_tiler(monkeypatch, fake_tiler)

    asyncio.run(mgr._execute(task_id, None))

    render_events = [p for e, p in events if e == "task_progress" and p.get("phase") == "render"]
    # 初始进入渲染 + 100% + 渲染结束强制 flush,而不是每瓦片一次
    assert len(render_events) == 3
    assert render_events[0]["rendered_tiles"] == 0
    assert render_events[-1]["rendered_tiles"] == 100
    assert render_events[-1]["total_tiles"] == 100
    # 载荷字段与 static/js/tasks.js 消费的全行形态兼容
    for field in ("name", "status", "task_type", "phase", "total_files",
                  "downloaded_files", "failed_files", "total_tiles",
                  "rendered_tiles", "failed_tiles", "zoom_min", "zoom_max",
                  "contour_interval"):
        assert field in render_events[-1]

    task = mgr.get_task(task_id)
    assert task["status"] == "completed"
    # 收尾用真实 rendered/failed 重写列语义
    assert task["rendered_tiles"] == 95
    assert task["failed_tiles"] == 5


def test_render_progress_flushes_pending_counts_on_pause(monkeypatch, tmp_path):
    # 暂停(stop_flag)中断渲染时,节流窗口内最后一次回调的计数也必须落库,
    # 否则恢复前 DB 里看到的是旧进度。
    import threading

    db, ctm_mod = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(ctm_mod, "_RENDER_PROGRESS_MIN_INTERVAL", 3600)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = _seed_running_task(db)

    stop = threading.Event()

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        progress_cb(5, 100)  # 节流窗口内,不强制 flush 的话这次计数会丢
        stop.set()
        return {"total": 100, "rendered": 5, "failed": 0}

    _patch_tiler(monkeypatch, fake_tiler)

    asyncio.run(mgr._execute(task_id, stop))

    task = mgr.get_task(task_id)
    assert task["rendered_tiles"] == 5  # 渲染结束强制 flush 保住了最后计数
    assert task["total_tiles"] == 100


def test_render_progress_reuses_one_connection_and_no_per_tile_select(monkeypatch, tmp_path):
    # 每次 flush 复用同一连接(回调期间不允许新开任何 DB 连接),且不再为
    # 构造 emit 载荷逐瓦片 SELECT 全行(get_task 全程不被调用)。
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(ctm_mod, "_RENDER_PROGRESS_MIN_INTERVAL", 0)  # 每次回调都 flush

    events = []

    class FakeSocket:
        def emit(self, event, payload):
            events.append((event, dict(payload)))

    mgr = ctm_mod.ContourTaskManager(socketio=FakeSocket())
    task_id = _seed_running_task(db)

    real_get_connection = ctm_mod.get_connection
    state = {"in_cb": False, "conns_in_cb": 0}

    def counting_get_connection():
        if state["in_cb"]:
            state["conns_in_cb"] += 1
        return real_get_connection()

    monkeypatch.setattr(ctm_mod, "get_connection", counting_get_connection)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        for i in range(1, 51):
            state["in_cb"] = True
            try:
                progress_cb(i, 50)
            finally:
                state["in_cb"] = False
        return {"total": 50, "rendered": 50, "failed": 0}

    _patch_tiler(monkeypatch, fake_tiler)

    def _boom(*a, **k):
        raise AssertionError("渲染期间不应逐瓦片 SELECT 全行")
    monkeypatch.setattr(mgr, "get_task", _boom)

    asyncio.run(mgr._execute(task_id, None))

    assert state["conns_in_cb"] == 0  # 50 次 flush 没有新开一个连接
    render_events = [p for e, p in events if e == "task_progress" and p.get("phase") == "render"]
    assert render_events[-1]["rendered_tiles"] == 50


def test_completed_task_not_flipped_to_failed_on_emit_error(monkeypatch, tmp_path):
    # 收尾已把任务置 completed,随后 emit("task_completed") 抛异常走到外层
    # 兜底时,不能把已完成的任务改判 failed。
    db, ctm_mod = _setup(monkeypatch, tmp_path)

    class BoomSocket:
        def emit(self, event, payload):
            if event == "task_completed":
                raise RuntimeError("client gone")

    mgr = ctm_mod.ContourTaskManager(socketio=BoomSocket())
    task_id = _seed_running_task(db)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        return {"total": 1, "rendered": 1, "failed": 0}

    _patch_tiler(monkeypatch, fake_tiler)

    import pytest
    with pytest.raises(RuntimeError, match="client gone"):
        asyncio.run(mgr._execute(task_id, None))

    assert mgr.get_task(task_id)["status"] == "completed"


def _delete_contour_row(db, task_id):
    """把「删除运行中的等高线任务」在库里留下的状态复现出来：行没了
    （contour_files 由 FK ON DELETE CASCADE 跟着走）。

    不走 task_deletion.delete_task_row：它按 manager.active_tasks 判「线程还
    活着吗」再决定同步删还是起后台收尾线程，而这里 _execute 是 asyncio.run
    直跑的、没有登记线程，走进去只会命中快路径。删除留给渲染线程的可观察
    状态就两样 —— 行没了 + 停止标志置位 —— 后者由各用例自己 set()。
    """
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM contour_tasks WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()


def test_deleted_task_emits_no_further_render_progress(monkeypatch, tmp_path):
    """删掉运行中的等高线任务之后，渲染线程不许再发一发 task_progress。

    base_payload 是【渲染开始前】的整行快照，里面 status='running'。行已经
    DELETE 了还继续发，前端那边 key 既不在时间流也不在活动集（deleteTask 刚
    摘干净），于是走 static/js/tasks.js 的 prependStreamRow 把行插回来；
    而停止后 _execute 直接 return、再不发任何终态事件，那行就永久卡在
    「运行中」，只能刷新页面才消失。
    """
    import threading

    db, ctm_mod = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(ctm_mod, "_RENDER_PROGRESS_MIN_INTERVAL", 0)  # 每次回调都 flush

    events = []

    class FakeSocket:
        def emit(self, event, payload):
            events.append((event, dict(payload)))

    mgr = ctm_mod.ContourTaskManager(socketio=FakeSocket())
    task_id = _seed_running_task(db)

    stop = threading.Event()
    mark = {}

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        progress_cb(5, 100)              # 删除之前的正常一发
        _delete_contour_row(db, task_id)
        stop.set()
        mark["at"] = len(events)
        # 渲染循环要跑到批边界才停，这中间回调照来 —— 幽灵行就是这儿发出来的
        progress_cb(9, 100)
        return {"total": 100, "rendered": 9, "failed": 0}

    _patch_tiler(monkeypatch, fake_tiler)

    asyncio.run(mgr._execute(task_id, stop))

    # 删除之后一个事件都不许有（含 tiler 返回后那一发无条件收尾 flush）
    # 比整个 events 而不是只比 (event, phase)：失败时要能看见 payload 里那句
    # status='running' —— 那才是「幽灵行」的直接证据
    assert mark.get("at", 0) > 0, "删除之前必须先有正常推送，否则这条用例没测到闸门"
    assert events[mark["at"]:] == [], (
        f"行已删除，之后不得再有任何推送: {events[mark['at']:]}")


def test_deleted_task_emits_no_further_prepare_stage(monkeypatch, tmp_path):
    """warp / 建金字塔阶段同样不许在行删掉之后继续发。

    render_stage 用的是同一份 base_payload，但它【没有 DB 写】、拿不到
    rowcount，所以它的闸门必须另有来源。删除完全可以发生在 warp 期间：那时
    只有「进入渲染阶段」那一发 flush 跑过（rowcount 当时还是 1），凭那次结论
    判活是过期的。
    """
    import threading

    db, ctm_mod = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(ctm_mod, "_RENDER_PROGRESS_MIN_INTERVAL", 0)

    events = []

    class FakeSocket:
        def emit(self, event, payload):
            events.append((event, dict(payload)))

    mgr = ctm_mod.ContourTaskManager(socketio=FakeSocket())
    task_id = _seed_running_task(db)

    stop = threading.Event()
    mark = {}

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        stage_cb("warp", 0.0)            # 删除之前：正常进入 warp
        _delete_contour_row(db, task_id)
        stop.set()
        mark["at"] = len(events)
        stage_cb("warp", 0.5)
        stage_cb("warp", 1.0)            # fraction=1.0 是 edge，节流豁免，必发
        return {"total": 0, "rendered": 0, "failed": 0}

    _patch_tiler(monkeypatch, fake_tiler)

    asyncio.run(mgr._execute(task_id, stop))

    assert mark.get("at", 0) > 0, "删除之前必须先有正常推送，否则这条用例没测到闸门"
    assert events[mark["at"]:] == [], (
        f"行已删除，之后不得再有任何推送: {events[mark['at']:]}")


def test_paused_render_still_emits_the_final_flush(monkeypatch, tmp_path):
    """暂停不能被误伤：行还在，收尾那一发 flush 必须照旧落库【并广播】。

    这是「删除时闸掉 emit」最容易撞坏的东西 —— 判据要是取 stop_flag.is_set()，
    暂停也置停止标志，这一发就被一起掐掉，节流窗口内最后一段计数在界面上就
    丢了（库里靠 flush 保住了，界面上没有）。rowcount 恰好只在行没了时为 0。
    """
    import threading

    db, ctm_mod = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(ctm_mod, "_RENDER_PROGRESS_MIN_INTERVAL", 3600)  # 全程在节流窗口内

    events = []

    class FakeSocket:
        def emit(self, event, payload):
            events.append((event, dict(payload)))

    mgr = ctm_mod.ContourTaskManager(socketio=FakeSocket())
    task_id = _seed_running_task(db)

    stop = threading.Event()

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        progress_cb(5, 100)                 # 节流窗口内,只记内存
        mgr.stop_flags[task_id] = stop      # start_task 会做的登记
        mgr.pause_task(task_id)             # 真暂停:库置 paused + 置停止标志
        return {"total": 100, "rendered": 5, "failed": 0}

    _patch_tiler(monkeypatch, fake_tiler)

    asyncio.run(mgr._execute(task_id, stop))

    assert stop.is_set(), "pause_task 必须置停止标志,否则这条用例没在测暂停"
    assert mgr.get_task(task_id)["status"] == "paused"
    render_events = [p for e, p in events if e == "task_progress" and p.get("phase") == "render"]
    # 进入渲染阶段那一发 + 收尾强制 flush 那一发
    assert len(render_events) == 2, [p["rendered_tiles"] for p in render_events]
    assert render_events[-1]["rendered_tiles"] == 5
    assert render_events[-1]["total_tiles"] == 100
