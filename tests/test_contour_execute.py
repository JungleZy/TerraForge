import asyncio
import importlib
import os
import sys
from pathlib import Path

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


def _seed_running_download_task(db, box=None, background=None):
    """直接 SQL 造一个旧版下载驱动的 running 任务（下载驱动 create_task 已删除,
    旧任务的下载→渲染恢复路径仍要测）。行形态对齐旧 create_task:默认
    COP-DEM-GLO-30 + terrain_shade/water ON,granule 推导沿用 src.services.dem_granules。"""
    from src.core import config
    from src.services.dem_granules import (
        tiles_for_bbox, copernicus_glo30_granules_for_tile,
        astwbd_v1_att_granules_for_tile,
    )
    box = box or dict(north=1.0, south=0.0, east=1.0, west=0.0)
    tiles = tiles_for_bbox(**box)
    dem_granules = [g for t in tiles for g in copernicus_glo30_granules_for_tile(t)]
    att_granules = [g for t in tiles for g in astwbd_v1_att_granules_for_tile(t)]
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
            VALUES ('t', 'running', ?, ?, ?, ?, 'COP-DEM-GLO-30', 50, ?, 1, 1,
                    12, 12, ?, ?, 0, 0, 0, 0, 0)
            """,
            (box["north"], box["south"], box["east"], box["west"],
             background or "#FAF6EC",
             str(Path(config.Config.DOWNLOADS_DIR) / "dem"),
             len(dem_granules) + len(att_granules)),
        )
        task_id = cur.lastrowid
        cur.executemany(
            "INSERT INTO contour_files (task_id, granule_id, kind, status, retry_count)"
            " VALUES (?, ?, ?, 'pending', 0)",
            [(task_id, g, "dem") for g in dem_granules]
            + [(task_id, g, "water") for g in att_granules],
        )
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_execute_completes_after_download_and_render(monkeypatch, tmp_path):
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = _seed_running_download_task(db, background="transparent")

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 123)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    called = {"render": False, "background": None}

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        called["render"] = True
        called["background"] = params.style.background
        if progress_cb:
            progress_cb(3, 3)
        return {"total": 3, "rendered": 3, "failed": 0}

    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, None))

    task = mgr.get_task(task_id)
    assert called["render"] is True
    assert called["background"] == "transparent"
    assert task["status"] == "completed"
    assert task["rendered_tiles"] == 3


def test_execute_total_tiles_covers_whole_dem_not_bbox(monkeypatch, tmp_path):
    # A tiny framed box inside one 1° granule; contours render over the whole
    # downloaded granule, so total_tiles must reflect that coverage, not the box.
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    from src.services.contour_engine import count_tiles
    from src.services.dem_granules import coverage_bbox

    mgr = ctm_mod.ContourTaskManager(socketio=None)
    box = dict(north=0.10, south=0.00, east=0.10, west=0.00)
    task_id = _seed_running_download_task(db, box=box)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 123)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    # Render succeeds but does NOT report progress, so total_tiles keeps the
    # initial coverage-based value set before rendering.
    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        return {"total": 0, "rendered": 1, "failed": 0}
    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, None))

    task = mgr.get_task(task_id)
    cov = coverage_bbox(**box)
    expected = count_tiles(*cov, 12, 12)
    bbox_only = count_tiles(box["north"], box["south"], box["east"], box["west"], 12, 12)
    assert task["total_tiles"] == expected
    assert expected > bbox_only


def test_execute_downloads_water_and_passes_shade_water_flags(monkeypatch, tmp_path):
    # Defaults: terrain_shade + water ON. _execute must download DEM (ASTGTM.003)
    # AND water att (ASTWBD.001), and forward both flags to the tiler.
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = _seed_running_download_task(db)

    calls = []

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        calls.append((dataset, list(granules)))
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    seen = {}

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        seen["shade"] = params.shade
        seen["water"] = params.water
        return {"total": 1, "rendered": 1, "failed": 0}
    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, None))

    datasets = [c[0] for c in calls]
    assert "COP-DEM-GLO-30" in datasets and "ASTWBD.001" in datasets  # default DEM = GLO-30
    astwbd = next(c for c in calls if c[0] == "ASTWBD.001")
    assert astwbd[1] == ["ASTWBDV001_N00E000_att.tif"]
    assert seen["shade"] is True and seen["water"] is True
    assert mgr.get_task(task_id)["status"] == "completed"


def test_execute_water_download_failure_is_not_fatal(monkeypatch, tmp_path):
    # ASTWBD att 404 (e.g. tile with no water bodies) must NOT fail the task;
    # DEM succeeded, so render proceeds.
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = _seed_running_download_task(db)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        # DEM (any source) succeeds; only the ASTWBD water download fails (404).
        status = "failed" if dataset == "ASTWBD.001" else "completed"
        for g in granules:
            if progress_callback:
                await progress_callback(g, status, "404" if status == "failed" else None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        return {"total": 1, "rendered": 1, "failed": 0}
    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, None))
    assert mgr.get_task(task_id)["status"] == "completed"  # water failure tolerated


def test_execute_fails_and_skips_render_on_download_failure(monkeypatch, tmp_path):
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = _seed_running_download_task(db)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "failed", "boom", None)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    called = {"render": False}

    def fake_tiler(*a, **k):
        called["render"] = True
        return {"total": 0, "rendered": 0, "failed": 0}

    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

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
    task_id = _seed_running_download_task(db)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        for i in range(1, 101):
            progress_cb(i, 100)
        return {"total": 100, "rendered": 95, "failed": 5}

    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

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
    task_id = _seed_running_download_task(db)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    stop = threading.Event()

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        progress_cb(5, 100)  # 节流窗口内,不强制 flush 的话这次计数会丢
        stop.set()
        return {"total": 100, "rendered": 5, "failed": 0}

    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

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
    task_id = _seed_running_download_task(db)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

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

    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    def _boom(*a, **k):
        raise AssertionError("渲染期间不应逐瓦片 SELECT 全行")
    monkeypatch.setattr(mgr, "get_task", _boom)

    asyncio.run(mgr._execute(task_id, None))

    assert state["conns_in_cb"] == 0  # 50 次 flush 没有新开一个连接
    render_events = [p for e, p in events if e == "task_progress" and p.get("phase") == "render"]
    assert render_events[-1]["rendered_tiles"] == 50


def test_download_progress_local_path_uses_flat_basename(monkeypatch, tmp_path):
    # Copernicus granule 是 name/name.tif 嵌套路径,引擎按扁平 basename 落盘
    # (dem_download_engine.download_files),写库的 local_path 必须一致。
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = _seed_running_download_task(db)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        return {"total": 1, "rendered": 1, "failed": 0}
    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, None))

    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT granule_id, local_path FROM contour_files WHERE task_id=?", (task_id,)).fetchall()
    finally:
        conn.close()
    assert rows, "expected progress callback to have written local_path"
    for r in rows:
        assert Path(r["local_path"]).name == Path(r["granule_id"]).name
        # 嵌套 granule 的中间目录不能出现在落盘路径里
        assert Path(r["granule_id"]).parent.name not in Path(r["local_path"]).parts


def test_completed_task_not_flipped_to_failed_on_emit_error(monkeypatch, tmp_path):
    # 收尾已把任务置 completed,随后 emit("task_completed") 抛异常走到外层
    # 兜底时,不能把已完成的任务改判 failed。
    db, ctm_mod = _setup(monkeypatch, tmp_path)

    class BoomSocket:
        def emit(self, event, payload):
            if event == "task_completed":
                raise RuntimeError("client gone")

    mgr = ctm_mod.ContourTaskManager(socketio=BoomSocket())
    task_id = _seed_running_download_task(db)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        return {"total": 1, "rendered": 1, "failed": 0}
    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    import pytest
    with pytest.raises(RuntimeError, match="client gone"):
        asyncio.run(mgr._execute(task_id, None))

    assert mgr.get_task(task_id)["status"] == "completed"


def test_in_flight_bytes_drive_download_speed(monkeypatch, tmp_path):
    """等高线的下载阶段与 DEM 共用引擎，速度同样来自在途字节回调。

    单颗 DEM 几十 MB、几分钟才下完，颗粒级状态回调期间一发都不出 —— 只有
    bytes_callback 能让任务行在下载途中显示真实速度。同时钉死另一半契约：
    completed 事件的 size_bytes（缓存命中时也是真实大小）绝不计入网络字节。
    """
    db, ctm_mod = _setup(monkeypatch, tmp_path)

    class _RecordingMeter:
        def __init__(self, *a, **k):
            self.records = []

        def record(self, n_bytes):
            self.records.append(n_bytes)

        def bps(self):
            return 2048.0

    meter = _RecordingMeter()
    monkeypatch.setattr(ctm_mod, "SpeedMeter", lambda *a, **k: meter)

    emitted = []

    class _Sock:
        def emit(self, event, payload=None):
            emitted.append((event, payload))

    mgr = ctm_mod.ContourTaskManager(socketio=_Sock())
    # 单颗粒（不要 water），下载事件序列才好断言
    task_id = _seed_running_download_task(
        db, box=dict(north=0.5, south=0.1, east=0.5, west=0.1))

    async def fake_download(dataset, granules, output_dir, progress_callback=None,
                            stop_flag=None, bytes_callback=None):
        for g in granules:
            await progress_callback(g, "downloading", None, None)
            await bytes_callback(g, 1024)
            await progress_callback(g, "completed", None, 987654)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        return {"total": 1, "rendered": 1, "failed": 0}
    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, None))

    # 每颗粒三笔：downloading -> 0（只推时间窗）、在途 1024、completed -> 0
    assert meter.records and set(meter.records) == {0, 1024}, meter.records
    assert meter.records.count(1024) * 1024 == sum(meter.records), (
        f"只有在途字节能进吞吐计: {meter.records}")

    download_pushes = [p for e, p in emitted
                       if e == "task_progress" and p.get("phase") == "download"]
    assert download_pushes, "下载阶段必须有带 phase=download 的推送"
    assert all(p["download_speed_bps"] == 2048 for p in download_pushes)


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
    摘干净），于是走 prependStreamRow 把行插回来（static/js/tasks.js:428）；
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
    task_id = _seed_running_download_task(db)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

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

    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, stop))

    # 删除之后一个事件都不许有（含 tiler 返回后那一发无条件收尾 flush）
    # 比整个 events 而不是只比 (event, phase)：失败时要能看见 payload 里那句
    # status='running' —— 那才是「幽灵行」的直接证据
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
    task_id = _seed_running_download_task(db)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

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

    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, stop))

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
    task_id = _seed_running_download_task(db)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None, bytes_callback=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    stop = threading.Event()

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        progress_cb(5, 100)                 # 节流窗口内,只记内存
        mgr.stop_flags[task_id] = stop      # start_task 会做的登记
        mgr.pause_task(task_id)             # 真暂停:库置 paused + 置停止标志
        return {"total": 100, "rendered": 5, "failed": 0}

    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, stop))

    assert stop.is_set(), "pause_task 必须置停止标志,否则这条用例没在测暂停"
    assert mgr.get_task(task_id)["status"] == "paused"
    render_events = [p for e, p in events if e == "task_progress" and p.get("phase") == "render"]
    # 进入渲染阶段那一发 + 收尾强制 flush 那一发
    assert len(render_events) == 2, [p["rendered_tiles"] for p in render_events]
    assert render_events[-1]["rendered_tiles"] == 5
    assert render_events[-1]["total_tiles"] == 100


def test_deleted_task_emits_no_further_download_progress(monkeypatch, tmp_path):
    """下载阶段同样不许在行删掉之后继续发。

    这条与上面两条不是重复：渲染阶段的闸门是本轮新加的 row_alive/rowcount，
    下载阶段靠的却是**另一套、而且是偶然的**机制 —— 在途路径每次重查行
    （:839-841 `if not row: return`）、状态路径靠 _record_progress 提交后重查
    整行返回 None（:806-807 → :864 `if row:`）。那两次重查的本意都是拿最新
    计数，挡住幽灵行只是副作用。谁把 payload 换成缓存的行快照（渲染阶段的
    base_payload 就是这么翻车的），幽灵行就在下载阶段重新长出来，而在此之前
    没有任何用例会红。
    """
    import threading

    db, ctm_mod = _setup(monkeypatch, tmp_path)
    # 节流窗口不是本用例的被测对象：留着的话删除之后那几发会被时间窗吞掉，
    # 用例就变成在测节流、反事实探针也照样绿。
    monkeypatch.setattr(ctm_mod, "_DOWNLOAD_PROGRESS_EMIT_MIN_INTERVAL", 0)

    events = []

    class FakeSocket:
        def emit(self, event, payload):
            events.append((event, dict(payload)))

    mgr = ctm_mod.ContourTaskManager(socketio=FakeSocket())
    task_id = _seed_running_download_task(db)

    mark = {}

    async def fake_download(dataset, granules, output_dir, progress_callback=None,
                            stop_flag=None, bytes_callback=None):
        for g in granules:
            if "at" not in mark:
                await progress_callback(g, "completed", None, 1024)  # 删除前的正常一发
                _delete_contour_row(db, task_id)
                mark["at"] = len(events)
                continue
            # 下载协程不会因为行没了就当场停 —— 剩下的颗粒照跑，两条推送路径
            # （在途字节 / 颗粒状态）都还会被踩到
            await bytes_callback(g, 4 * 1024 * 1024)
            await progress_callback(g, "completed", None, 1024)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        return {"total": 0, "rendered": 0, "failed": 0}

    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, threading.Event()))

    assert mark.get("at", 0) > 0, "删除之前必须先有正常推送，否则这条用例没测到闸门"
    # 删除之后一个事件都不许有 —— 含后面渲染阶段和收尾
    assert events[mark["at"]:] == [], (
        f"行已删除，之后不得再有任何推送: {events[mark['at']:]}")