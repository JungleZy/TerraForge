"""C4: 'downloading' 状态的 DEM 文件不能成孤儿。

暂停/崩溃时正在下载的文件停留在 downloading：
- 恢复（_execute）时必须重新入队下载，而不是被查询条件跳过；
- 终态统计必须把任何非 completed/skipped 的文件算作未完成，
  否则任务会被误报 completed（用户拿到缺块的"成功"DEM）；
- 下载引擎在暂停（stop_flag 置位）时必须把下载中的文件回写为 pending，
  不能静默 return 留下 downloading。
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
    for mod in ("app", "src.core.database", "src.services.dem_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    dtm = importlib.import_module("src.services.dem_task_manager")
    return db, dtm


def _seed_dem_task(db, output_path, file_statuses, task_status="running"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('t', ?, 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, ?, 0, 0)
            """,
            (task_status, str(output_path), len(file_statuses)),
        )
        task_id = cur.lastrowid
        for i, st in enumerate(file_statuses):
            cur.execute(
                "INSERT INTO dem_files (task_id, granule_id, status, retry_count) VALUES (?, ?, ?, 0)",
                (task_id, f"G{i:02d}.tif", st),
            )
        conn.commit()
        return task_id
    finally:
        conn.close()


def _task_row(db, task_id):
    conn = db.get_connection()
    try:
        return conn.execute("SELECT * FROM dem_tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()


def test_downloading_file_is_requeued_on_resume(monkeypatch, tmp_path):
    """崩溃恢复：downloading 文件必须重新入队，任务才能真正 completed。"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out", ["completed", "downloading"])

    received = []

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag):
        received.extend(granules)
        for g in granules:
            await progress_callback(g, "completed", None, 10)

    mgr.engine.download_files = fake_download_files

    asyncio.run(mgr._execute(task_id))

    assert "G01.tif" in received, "downloading 状态的文件必须重新入队下载"
    assert _task_row(db, task_id)["status"] == "completed"


def test_unfinished_downloading_file_prevents_completed(monkeypatch, tmp_path):
    """ downloading 文件没下完时任务绝不能报 completed（缺块的假成功）。"""
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_dem_task(db, tmp_path / "out", ["completed", "downloading"])

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag):
        # 引擎什么都没完成（模拟暂停/崩溃后文件仍处于未完成状态）
        return

    mgr.engine.download_files = fake_download_files

    asyncio.run(mgr._execute(task_id))

    row = _task_row(db, task_id)
    assert row["status"] == "failed", (
        f"还有未完成文件时任务必须报 failed，实际报 {row['status']}"
    )


# ---------------------------------------------------------------------------
# 引擎暂停路径：stop_flag 置位时下载中的文件必须回写 pending
# ---------------------------------------------------------------------------


class _StubConfig:
    def __init__(self, values):
        self._v = values

    def get(self, key, default=None):
        return self._v.get(key, default)


class _StopContent:
    """第一块之后置位 stop_flag，模拟用户中途点暂停。"""

    def __init__(self, stop_event):
        self._stop = stop_event

    async def iter_chunked(self, _n):
        yield b"x" * 16
        self._stop.set()
        yield b"y" * 16


class _FakeResp:
    def __init__(self, status=200, content=None, headers=None):
        self.status = status
        self.content = content
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, proxy=None):
        return self._resp


def test_engine_pause_writes_back_pending(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    import src.services.dem_download_engine as dde

    engine = dde.DemDownloadEngine()
    engine.config = _StubConfig({
        "dem_cache_enabled": "false",
        "max_retries": "3",
        "request_timeout": "5",
        "concurrent_downloads": "2",
    })

    async def run():
        stop = asyncio.Event()
        resp = _FakeResp(status=200, content=_StopContent(stop))
        monkeypatch.setattr(dde.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(resp))
        monkeypatch.setattr(dde.aiohttp, "TCPConnector", lambda *a, **k: None)
        monkeypatch.setattr(dde.aiohttp, "CookieJar", lambda *a, **k: None)

        events = []

        async def progress(granule, status, error, size):
            events.append((granule, status))

        await engine.download_files(
            dataset="COP-DEM-GLO-30",
            granules=["G.tif"],
            output_dir=tmp_path / "out",
            progress_callback=progress,
            stop_flag=stop,
        )
        return events

    events = asyncio.run(run())

    assert ("G.tif", "pending") in events, (
        f"暂停时下载中的文件必须回写 pending，实际事件: {events}"
    )
    assert not any(status == "failed" for _, status in events), (
        "用户暂停不能把文件标记成 failed"
    )
