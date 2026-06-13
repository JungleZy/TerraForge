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
