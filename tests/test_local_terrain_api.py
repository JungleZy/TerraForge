import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _reload(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in ("database", "services.local_terrain_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("database")
    db.init_database()
    mgr_mod = importlib.import_module("services.local_terrain_task_manager")
    return db, mgr_mod


def test_create_task_saves_files_and_rows(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    # Don't actually tile in this task's tests.
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    files = [
        ("a.tif", b"fake-tif-bytes-a"),
        ("b.tiff", b"fake-tif-bytes-b"),
    ]
    task_id = mgr.create_task_with_files(name="local-1", files=files, maxzoom=12)

    task = mgr.get_task(task_id)
    assert task["name"] == "local-1"
    assert task["total_files"] == 2
    assert task["uploaded_files"] == 2
    assert task["failed_files"] == 0
    assert task["maxzoom"] == 12

    # Files saved under source/ as *_dem.tif
    from pathlib import Path
    source_dir = Path(task["source_dir"])
    saved = sorted(p.name for p in source_dir.glob("*_dem.tif"))
    assert saved == ["upload_1_dem.tif", "upload_2_dem.tif"]
    assert (source_dir / "upload_1_dem.tif").read_bytes() == b"fake-tif-bytes-a"

    rows = mgr.list_files(task_id)
    assert len(rows) == 2
    assert {r["original_filename"] for r in rows} == {"a.tif", "b.tiff"}
    assert all(r["status"] == "uploaded" for r in rows)


def test_create_task_rejects_non_tif(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    import pytest
    with pytest.raises(ValueError):
        mgr.create_task_with_files(name="bad", files=[("x.png", b"data")], maxzoom=12)


def test_create_task_rejects_empty_file_list(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    import pytest
    with pytest.raises(ValueError):
        mgr.create_task_with_files(name="empty", files=[], maxzoom=12)


def test_create_task_rejects_zero_byte_file(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    import pytest
    with pytest.raises(ValueError):
        mgr.create_task_with_files(name="zero", files=[("a.tif", b"")], maxzoom=12)


def test_start_tiling_invokes_build_terrain(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    calls = {}

    # Capture the tiler call by patching tile_dem_task_dir in the manager module.
    def fake_tile(task_dir, out_dir, params):
        calls["task_dir"] = task_dir
        calls["out_dir"] = out_dir
        calls["maxzoom"] = params.maxzoom
        # Simulate tiler output so completion logic can proceed.
        from pathlib import Path
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    files = [("a.tif", b"fake-tif-bytes-a")]
    task_id = mgr.create_task_with_files(name="local-1", files=files, maxzoom=11)

    # start_tiling spawns a thread; wait for it.
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)

    task = mgr.get_task(task_id)
    assert task["status"] == "completed"
    assert calls["maxzoom"] == 11
    assert str(calls["task_dir"]).endswith(f"local_task_{task_id}/source")
    assert str(calls["out_dir"]).endswith(f"local_task_{task_id}/terrain_tiles")


def test_start_tiling_marks_failed_on_error(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    def boom(task_dir, out_dir, params):
        raise RuntimeError("tiler exploded")

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", boom)

    task_id = mgr.create_task_with_files(name="local-1", files=[("a.tif", b"x")], maxzoom=11)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)

    task = mgr.get_task(task_id)
    assert task["status"] == "failed"
    assert "tiler exploded" in (task["error_message"] or "")
