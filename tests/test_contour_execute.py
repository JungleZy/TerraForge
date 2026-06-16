import asyncio
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("database")
    db.init_database()
    ctm_mod = importlib.import_module("services.contour_task_manager")
    return db, ctm_mod


def _make_running_task(db, mgr, background=None):
    params = {
        "name": "t", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
        "contour_interval": 50, "zoom_min": 12, "zoom_max": 12,
    }
    if background is not None:
        params["background"] = background
    task_id = mgr.create_task(params)
    conn = db.get_connection()
    try:
        conn.execute("UPDATE contour_tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    return task_id


def test_execute_completes_after_download_and_render(monkeypatch, tmp_path):
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    task_id = _make_running_task(db, mgr, background="transparent")

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
    task_id = mgr.create_task({"name": "tiny", **box,
                               "contour_interval": 50, "zoom_min": 12, "zoom_max": 12})
    conn = db.get_connection()
    try:
        conn.execute("UPDATE contour_tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()

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
    task_id = _make_running_task(db, mgr)

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
    task_id = _make_running_task(db, mgr)

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
    task_id = _make_running_task(db, mgr)

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
