"""「处理」按钮链路：高程下载任务 → 新的本地地形切片任务（零拷贝源）。

钉住的行为：
- create_task_from_dem_task 建出独立的 local_terrain 任务行（进时间流），
  源 tif 不拷贝，local_terrain_files.local_path 直指 DEM 任务目录；
- 三道闸门：DEM 任务不存在 / 未 completed / 目录里没有 DEM tif；
- start_tiling 对零拷贝行按 source_dem_task_id 重算源目录（不信库存
  source_dir），切片器拿到的 task_dir 是 DEM 任务目录；
- 删除切片任务只清自己的产物目录，源 DEM 文件原样保留。
"""
import importlib
import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _reload(monkeypatch, tmp_path):
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    # 全球底图可用性闸门会去看 BASE_DIR/assets/terrain/base_z8（见
    # test_local_terrain_api._reload 的同款注释）。
    monkeypatch.setattr(config.Config, "BASE_DIR", tmp_path)

    for mod in ("src.core.database", "src.services.local_terrain_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    mgr_mod = importlib.import_module("src.services.local_terrain_task_manager")
    return db, mgr_mod


def _seed_dem_task(db, tmp_path, status="completed", with_tif=True):
    """造一个 completed 的 DEM 下载任务与其目录里的 tif，返回 (dem_task_id, tif_dir)。"""
    out_base = tmp_path / "downloads" / "dem"
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('dem-src', ?, 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 1, 1, 0)
            """,
            (status, str(out_base)),
        )
        dem_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    tif_dir = out_base / f"dem_task_{dem_id}"
    if with_tif:
        tif_dir.mkdir(parents=True, exist_ok=True)
        (tif_dir / "N47E006_dem.tif").write_bytes(b"fake-dem-bytes")
    return dem_id, tif_dir


def test_create_from_dem_task_zero_copy(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling",
                        lambda self, task_id: None)
    dem_id, tif_dir = _seed_dem_task(db, tmp_path)

    task_id = mgr.create_task_from_dem_task(
        name="from-dem", dem_task_id=dem_id, maxzoom=12, quality="speed",
        vertex_normals=True)

    task = mgr.get_task(task_id)
    assert task["name"] == "from-dem"
    assert task["source_dem_task_id"] == dem_id
    assert task["source_dir"] == str(tif_dir)
    assert task["total_files"] == 1
    assert task["uploaded_files"] == 1
    assert task["maxzoom"] == 12
    assert task["quality"] == "speed"
    assert task["vertex_normals"] == 1

    # 零拷贝：文件行直指 DEM 任务目录，本任务目录下没有 source/ 副本。
    rows = mgr.list_files(task_id)
    assert len(rows) == 1
    assert rows[0]["local_path"] == str(tif_dir / "N47E006_dem.tif")
    assert rows[0]["status"] == "uploaded"
    task_root = Path(task["output_path"])
    assert not (task_root / "source").exists()
    assert (task_root / "terrain_tiles").is_dir()


def test_create_from_dem_task_gates(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling",
                        lambda self, task_id: None)

    # 任务不存在
    with pytest.raises(ValueError, match="not found"):
        mgr.create_task_from_dem_task(name="x", dem_task_id=99999)

    # 没下完不许切（与 dem_task_manager.start_tiling 同一道闸门）
    running_id, _ = _seed_dem_task(db, tmp_path, status="running")
    with pytest.raises(ValueError, match="wait for the download to complete"):
        mgr.create_task_from_dem_task(name="x", dem_task_id=running_id)

    # 目录里没有 DEM tif
    empty_id, _ = _seed_dem_task(db, tmp_path, with_tif=False)
    with pytest.raises(ValueError, match="No DEM tifs"):
        mgr.create_task_from_dem_task(name="x", dem_task_id=empty_id)


def test_start_tiling_uses_dem_source_dir(monkeypatch, tmp_path):
    """零拷贝行的切片器输入必须是 DEM 任务目录，不是本任务的 source/。"""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    dem_id, tif_dir = _seed_dem_task(db, tmp_path)

    calls = {}

    def fake_tile(task_dir, out_dir, params):
        calls["task_dir"] = Path(task_dir)
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    task_id = mgr.create_task_from_dem_task(name="from-dem", dem_task_id=dem_id,
                                            maxzoom=11)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)

    assert calls["task_dir"] == tif_dir
    assert mgr.get_task(task_id)["status"] == "completed"


def test_start_tiling_missing_source_dem_task_fails(monkeypatch, tmp_path):
    """源 DEM 任务被删后重切必须当场报错，不拿空目录切出「成功」的空产物。"""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    # 只挡 create 末尾的自动起切；断言要调的是真 start_tiling。
    real_start = mgr_mod.LocalTerrainTaskManager.start_tiling
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling",
                        lambda self, task_id: None)
    dem_id, _ = _seed_dem_task(db, tmp_path)
    task_id = mgr.create_task_from_dem_task(name="from-dem", dem_task_id=dem_id,
                                            maxzoom=11)

    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM dem_tasks WHERE id=?", (dem_id,))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="Source DEM task"):
        real_start(mgr, task_id)


def test_delete_keeps_dem_source_files(monkeypatch, tmp_path):
    """删切片任务只清自己的产物目录；源 DEM 任务的 tif 原样保留。"""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling",
                        lambda self, task_id: None)
    dem_id, tif_dir = _seed_dem_task(db, tmp_path)
    task_id = mgr.create_task_from_dem_task(name="from-dem", dem_task_id=dem_id,
                                            maxzoom=12)
    task_root = Path(mgr.get_task(task_id)["output_path"])
    assert task_root.exists()

    outcome = mgr.delete_task(task_id, delete_files=True)
    assert outcome.row_deleted
    assert not task_root.exists()
    assert (tif_dir / "N47E006_dem.tif").read_bytes() == b"fake-dem-bytes"


# --- 路由层 -----------------------------------------------------------------

def _load_app(monkeypatch, tmp_path):
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config.Config, "BASE_DIR", tmp_path)

    for mod in ("app", "src.core.database", "src.services.local_terrain_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def test_http_dem_task_id_creates_task(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    monkeypatch.setattr(
        app_mod.local_terrain_task_manager.__class__,
        "start_tiling", lambda self, task_id: None)
    dem_id, _ = _seed_dem_task(db, tmp_path)

    resp = client.post(
        "/api/terrain/local/tasks",
        data={"name": "http-from-dem", "maxzoom": "12",
              "dem_task_id": str(dem_id)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    task_id = resp.get_json()["task_id"]
    detail = client.get(f"/api/terrain/local/tasks/{task_id}").get_json()
    assert detail["task"]["source_dem_task_id"] == dem_id


def test_http_dem_task_id_and_files_conflict(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.post(
        "/api/terrain/local/tasks",
        data={"name": "x", "dem_task_id": "1",
              "files": [(io.BytesIO(b"x"), "a.tif")]},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "not both" in resp.get_json()["error"]


def test_http_invalid_dem_task_id_400(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.post(
        "/api/terrain/local/tasks",
        data={"name": "x", "dem_task_id": "abc"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "Invalid dem_task_id" in resp.get_json()["error"]
