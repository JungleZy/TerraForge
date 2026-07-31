import asyncio
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("core.database")
    db.init_database()
    ctm_mod = importlib.import_module("services.contour_task_manager")
    return db, ctm_mod


def _seed_running_download_task(db, box=None, background=None):
    """直接 SQL 造一个旧版下载驱动的 running 任务（下载驱动 create_task 已删除,
    旧任务的下载→渲染恢复路径仍要测）。行形态对齐旧 create_task:默认
    COP-DEM-GLO-30 + terrain_shade/water ON,granule 推导沿用 services.dem_granules。"""
    from core import config
    from services.dem_granules import (
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

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 123)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    called = {"render": False, "background": None}

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None, stop_flag=None):
        called["render"] = True
        called["background"] = params.style.background
        if progress_cb:
            progress_cb(3, 3)
        return {"total": 3, "rendered": 3, "failed": 0}

    import services.contour_task_tiler as tiler_mod
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
    from services.contour_engine import count_tiles
    from services.dem_granules import coverage_bbox

    mgr = ctm_mod.ContourTaskManager(socketio=None)
    box = dict(north=0.10, south=0.00, east=0.10, west=0.00)
    task_id = _seed_running_download_task(db, box=box)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 123)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    # Render succeeds but does NOT report progress, so total_tiles keeps the
    # initial coverage-based value set before rendering.
    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None, stop_flag=None):
        return {"total": 0, "rendered": 1, "failed": 0}
    import services.contour_task_tiler as tiler_mod
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

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None):
        calls.append((dataset, list(granules)))
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    seen = {}

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None, stop_flag=None):
        seen["shade"] = params.shade
        seen["water"] = params.water
        return {"total": 1, "rendered": 1, "failed": 0}
    import services.contour_task_tiler as tiler_mod
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

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None):
        # DEM (any source) succeeds; only the ASTWBD water download fails (404).
        status = "failed" if dataset == "ASTWBD.001" else "completed"
        for g in granules:
            if progress_callback:
                await progress_callback(g, status, "404" if status == "failed" else None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None, stop_flag=None):
        return {"total": 1, "rendered": 1, "failed": 0}
    import services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, None))
    assert mgr.get_task(task_id)["status"] == "completed"  # water failure tolerated


def test_execute_fails_and_skips_render_on_download_failure(monkeypatch, tmp_path):
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = _seed_running_download_task(db)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "failed", "boom", None)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    called = {"render": False}

    def fake_tiler(*a, **k):
        called["render"] = True
        return {"total": 0, "rendered": 0, "failed": 0}

    import services.contour_task_tiler as tiler_mod
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

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None, stop_flag=None):
        for i in range(1, 101):
            progress_cb(i, 100)
        return {"total": 100, "rendered": 95, "failed": 5}

    import services.contour_task_tiler as tiler_mod
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

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    stop = threading.Event()

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None, stop_flag=None):
        progress_cb(5, 100)  # 节流窗口内,不强制 flush 的话这次计数会丢
        stop.set()
        return {"total": 100, "rendered": 5, "failed": 0}

    import services.contour_task_tiler as tiler_mod
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

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None):
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

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None, stop_flag=None):
        for i in range(1, 51):
            state["in_cb"] = True
            try:
                progress_cb(i, 50)
            finally:
                state["in_cb"] = False
        return {"total": 50, "rendered": 50, "failed": 0}

    import services.contour_task_tiler as tiler_mod
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

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None, stop_flag=None):
        return {"total": 1, "rendered": 1, "failed": 0}
    import services.contour_task_tiler as tiler_mod
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

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None, stop_flag=None):
        return {"total": 1, "rendered": 1, "failed": 0}
    import services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    import pytest
    with pytest.raises(RuntimeError, match="client gone"):
        asyncio.run(mgr._execute(task_id, None))

    assert mgr.get_task(task_id)["status"] == "completed"
