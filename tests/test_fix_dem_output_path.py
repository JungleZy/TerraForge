"""C5/I16/I14: output_path 校验与解析、任务名消毒、local_path 与实际落盘一致。

- C5: 创建任务时 output_path 必须解析并强制落在 Config.DOWNLOADS_DIR 内，
  越界抛 ValueError（路由层转 400）；任务名消毒后再入库。
- I16: 相对 output_path 相对 Config.DOWNLOADS_DIR 解析，不依赖进程 CWD。
- I14: COP-DEM 嵌套 granule_id 的 local_path 必须与实际落盘（basename）一致。
"""

import asyncio
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BBOX = {"north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0}


def _setup(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "database", "services.dem_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("database")
    db.init_database()
    dtm = importlib.import_module("services.dem_task_manager")
    return db, dtm


def test_create_task_rejects_output_path_escape(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)

    with pytest.raises(ValueError):
        mgr.create_task({"name": "x", **BBOX, "output_path": "../../escape"})


def test_create_task_rejects_absolute_path_outside_downloads(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)

    with pytest.raises(ValueError):
        mgr.create_task({"name": "x", **BBOX, "output_path": str(tmp_path / "outside")})


def test_create_task_resolves_relative_output_path_against_downloads(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = mgr.create_task({"name": "x", **BBOX, "output_path": "sub/dir"})

    conn = db.get_connection()
    try:
        row = conn.execute("SELECT output_path FROM dem_tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()

    expected = str((tmp_path / "downloads" / "sub" / "dir").resolve())
    assert row["output_path"] == expected, (
        f"相对 output_path 必须相对 DOWNLOADS_DIR 解析成绝对路径，实际: {row['output_path']}"
    )


def test_create_task_sanitizes_name(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = mgr.create_task({"name": "../evil/name\\x", **BBOX})

    conn = db.get_connection()
    try:
        row = conn.execute("SELECT name FROM dem_tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()

    name = row["name"]
    assert "/" not in name and "\\" not in name and ".." not in name, (
        f"任务名必须消毒后才能入库/拼目录，实际: {name!r}"
    )


def test_execute_resolves_relative_output_path_against_downloads(monkeypatch, tmp_path):
    """I16: 历史数据里相对 output_path 不能按进程 CWD 解析。"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('t', 'running', 1, 0, 1, 0, 'COP-DEM-GLO-30', 'rel_out', 1, 0, 0)
            """
        )
        task_id = cur.lastrowid
        cur.execute(
            "INSERT INTO dem_files (task_id, granule_id, status, retry_count) VALUES (?, 'G.tif', 'pending', 0)",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    seen = {}

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag):
        seen["output_dir"] = output_dir
        await progress_callback("G.tif", "completed", None, 10)

    mgr.engine.download_files = fake_download_files
    asyncio.run(mgr._execute(task_id))

    expected = (tmp_path / "downloads" / "rel_out" / f"dem_task_{task_id}").resolve()
    assert os.path.realpath(str(seen["output_dir"])) == os.path.realpath(str(expected)), (
        f"相对 output_path 必须相对 DOWNLOADS_DIR 解析，实际: {seen['output_dir']}"
    )


def test_local_path_uses_actual_on_disk_basename(monkeypatch, tmp_path):
    """I14: COP-DEM 嵌套 granule_id 落盘是 basename，local_path 必须一致。"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)

    granule = ("Copernicus_DSM_COG_10_N00_00_E000_00_DEM/"
               "Copernicus_DSM_COG_10_N00_00_E000_00_DEM.tif")
    basename = "Copernicus_DSM_COG_10_N00_00_E000_00_DEM.tif"

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('t', 'running', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 1, 0, 0)
            """,
            (str(tmp_path / "out"),),
        )
        task_id = cur.lastrowid
        cur.execute(
            "INSERT INTO dem_files (task_id, granule_id, status, retry_count) VALUES (?, ?, 'pending', 0)",
            (task_id, granule),
        )
        conn.commit()
    finally:
        conn.close()

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag):
        await progress_callback(granule, "completed", None, 10)

    mgr.engine.download_files = fake_download_files
    asyncio.run(mgr._execute(task_id))

    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT local_path FROM dem_files WHERE task_id=? AND granule_id=?", (task_id, granule)
        ).fetchone()
    finally:
        conn.close()

    expected = str((tmp_path / "out") / f"dem_task_{task_id}" / basename)
    assert row["local_path"] == expected, (
        f"local_path 必须是实际落盘路径（basename），实际: {row['local_path']}"
    )
