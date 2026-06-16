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


def _make_running_task(db, mgr):
    task_id = mgr.create_task({
        "name": "t", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
        "contour_interval": 50, "zoom_min": 12, "zoom_max": 12,
    })
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
    task_id = _make_running_task(db, mgr)

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 123)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    called = {"render": False}

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None, stop_flag=None):
        called["render"] = True
        if progress_cb:
            progress_cb(3, 3)
        return {"total": 3, "rendered": 3, "failed": 0}

    import services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, None))

    task = mgr.get_task(task_id)
    assert called["render"] is True
    assert task["status"] == "completed"
    assert task["rendered_tiles"] == 3


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
