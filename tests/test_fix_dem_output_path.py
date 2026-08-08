"""C5/I16/I14: output_path 校验与解析、任务名消毒、local_path 与实际落盘一致。

- C5: 创建任务时 output_path 必须是绝对路径且落在 Config.DOWNLOADS_DIR 内，
  相对路径/越界一律抛 ValueError（路由层转 400）；任务名消毒后再入库。
- I16: 存量数据的相对 output_path 在执行时相对 Config.DOWNLOADS_DIR 解析，
  不依赖进程 CWD（仅兼容历史行，新输入不再接受相对值）。
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


def test_create_task_rejects_output_path_escape(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)

    with pytest.raises(ValueError):
        mgr.create_task({"name": "x", **BBOX, "output_path": "../../escape"})


def test_create_task_accepts_absolute_path_outside_downloads(monkeypatch, tmp_path):
    """0.2.4 全盘化:DOWNLOADS_DIR 之外的绝对路径(深度足够)同样接受"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)

    task_id = mgr.create_task({"name": "x", **BBOX, "output_path": str(tmp_path / "outside")})
    assert isinstance(task_id, int)


def test_create_task_rejects_relative_output_path(monkeypatch, tmp_path):
    """新口径:保存路径一律要求绝对路径,相对值不再代为解析,直接拒绝。

    (本测试翻面前钉的是旧行为「相对路径相对 DOWNLOADS_DIR 解析入库」;
    UI 的「浏览」按钮选出的就是绝对路径,相对输入多半是手滑或旧脚本,
    放行会让 exe 换目录启动后落盘位置漂移。)"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)

    with pytest.raises(ValueError, match='绝对路径'):
        mgr.create_task({"name": "x", **BBOX, "output_path": "sub/dir"})


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


def test_execute_resolves_relative_output_path_without_using_cwd(monkeypatch, tmp_path):
    """I16: 历史数据里相对 output_path 不能按进程 CWD 解析。

    钉的是「不依赖 CWD」+「与存量归一化同一套口径」，**不是**某个具体目录：
    2026-08-08 起 DEM 侧统一走 `task_cleanup.resolve_stored_output_dir`（M10 归一
    化用的就是它），裸相对值落到 BASE_DIR；此前 DEM 侧另有一份走
    `geo_validation.resolve_output_dir` 的私有 helper，落到 DOWNLOADS_DIR ——
    同一字段两套解析规则，正是 M10 要消灭的东西（见 P1#5）。

    注意这一行是**直接 INSERT** 造出来的:`create_task` 走
    `require_absolute_output_dir`，实测拒收一切相对值，所以线上的
    `dem_tasks.output_path` 恒为绝对路径（两套口径对绝对值一致）。本用例守的是
    手工改库/远古存量这类边角输入不会被按 CWD 解析。
    """
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

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag, bytes_callback=None):
        seen["output_dir"] = output_dir
        await progress_callback("G.tif", "completed", None, 10)

    mgr.engine.download_files = fake_download_files
    asyncio.run(mgr._execute(task_id))

    from src.services.task_cleanup import resolve_stored_output_dir

    expected = (resolve_stored_output_dir("rel_out") / f"dem_task_{task_id}").resolve()
    assert os.path.realpath(str(seen["output_dir"])) == os.path.realpath(str(expected)), (
        f"DEM 侧必须走 resolve_stored_output_dir（与 M10 存量归一同一套口径），"
        f"实际: {seen['output_dir']}"
    )
    # 「不依赖进程 CWD」那一半单独钉住:CWD 换到别处结果必须不变。
    monkeypatch.chdir(tmp_path / "downloads")
    assert (resolve_stored_output_dir("rel_out") / f"dem_task_{task_id}").resolve() == expected


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

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag, bytes_callback=None):
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
