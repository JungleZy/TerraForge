"""I12: ASTGTM 覆盖范围过滤 + 404 颗粒按 skipped 处理（部分成功语义）。

- ASTGTM V3 只覆盖 83°S–83°N：|lat|>83 的颗粒必然 404，生成时直接钳掉；
- 海洋等无数据区域下载遇 404 → 文件标记 skipped（无数据），不计 failed、
  不阻断任务完成。
"""

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
    for mod in ("app", "database", "services.dem_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("database")
    db.init_database()
    dtm = importlib.import_module("services.dem_task_manager")
    return db, dtm


# ---------------------------------------------------------------------------
# 颗粒生成钳制 |lat| <= 83
# ---------------------------------------------------------------------------


def test_astgtm_granules_clamped_to_coverage():
    from services.dem_granules import LatLonTile, astgtm_v3_granules_for_tile

    assert astgtm_v3_granules_for_tile(LatLonTile(lat=83, lon=0), include_num=False, include_swb=False) == [
        "ASTGTMV003_N83E000_dem.tif"
    ]
    assert astgtm_v3_granules_for_tile(LatLonTile(lat=-83, lon=0), include_num=False, include_swb=False) == [
        "ASTGTMV003_S83E000_dem.tif"
    ]
    # 覆盖范围之外：必然 404，不应生成颗粒
    assert astgtm_v3_granules_for_tile(LatLonTile(lat=84, lon=0), include_num=False, include_swb=False) == []
    assert astgtm_v3_granules_for_tile(LatLonTile(lat=-84, lon=0), include_num=False, include_swb=False) == []


def test_create_task_astgtm_skips_out_of_coverage_tiles(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = mgr.create_task({
        "name": "polar", "north": 85.0, "south": 83.5, "east": 1.0, "west": 0.0,
        "dataset": "ASTGTM.003",
    })

    conn = db.get_connection()
    try:
        gids = [r["granule_id"] for r in conn.execute(
            "SELECT granule_id FROM dem_files WHERE task_id=? ORDER BY granule_id", (task_id,)).fetchall()]
    finally:
        conn.close()

    assert gids == ["ASTGTMV003_N83E000_dem.tif"], f"N84 颗粒必然 404，不应入队: {gids}"


# ---------------------------------------------------------------------------
# 下载遇 404 → skipped
# ---------------------------------------------------------------------------


class _StubConfig:
    def __init__(self, values):
        self._v = values

    def get(self, key, default=None):
        return self._v.get(key, default)


class _FakeResp:
    def __init__(self, status=200, chunks=(), headers=None):
        self.status = status
        self.headers = headers or {}
        self.content = self
        self._chunks = chunks

    async def iter_chunked(self, _n):
        for c in self._chunks:
            yield c

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


def _patch_session(monkeypatch, dde, resp):
    monkeypatch.setattr(dde.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(resp))
    monkeypatch.setattr(dde.aiohttp, "TCPConnector", lambda *a, **k: None)
    monkeypatch.setattr(dde.aiohttp, "CookieJar", lambda *a, **k: None)


def test_engine_404_marks_file_skipped(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    import services.dem_download_engine as dde

    engine = dde.DemDownloadEngine()
    engine.config = _StubConfig({
        "dem_cache_enabled": "true",
        "max_retries": "3",
        "request_timeout": "5",
        "concurrent_downloads": "2",
    })
    _patch_session(monkeypatch, dde, _FakeResp(status=404))

    events = []

    async def progress(granule, status, error, size):
        events.append((granule, status, error))

    asyncio.run(engine.download_files(
        dataset="COP-DEM-GLO-30",
        granules=["ocean/G.tif"],
        output_dir=tmp_path / "out",
        progress_callback=progress,
        stop_flag=None,
    ))

    statuses = [(g, s) for g, s, _ in events]
    assert statuses[-1] == ("ocean/G.tif", "skipped"), (
        f"404 必须标记 skipped，实际: {events}"
    )
    assert statuses.count(("ocean/G.tif", "downloading")) == 1, (
        f"404 不应重试，实际: {events}"
    )
    assert not any(s == "failed" for _, s in statuses), "404 不能计入 failed"
    # 不留残文件、不进缓存
    assert not (tmp_path / "out" / "G.tif").exists()
    assert not (tmp_path / "cache" / "dem" / "G.tif").exists()


def test_skipped_file_does_not_block_task_completion(monkeypatch, tmp_path):
    """部分成功语义：skipped（无数据）不算 failed，任务照常 completed。"""
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
            VALUES ('t', 'running', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 2, 0, 0)
            """,
            (str(tmp_path / "out"),),
        )
        task_id = cur.lastrowid
        for g in ("land.tif", "ocean.tif"):
            cur.execute(
                "INSERT INTO dem_files (task_id, granule_id, status, retry_count) VALUES (?, ?, 'pending', 0)",
                (task_id, g),
            )
        conn.commit()
    finally:
        conn.close()

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag):
        await progress_callback("land.tif", "completed", None, 10)
        await progress_callback("ocean.tif", "skipped", "no data (HTTP 404)", None)

    mgr.engine.download_files = fake_download_files

    asyncio.run(mgr._execute(task_id))

    conn = db.get_connection()
    try:
        row = conn.execute("SELECT * FROM dem_tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()

    assert row["status"] == "completed", (
        f"skipped 文件不应阻断任务完成，实际: {row['status']} ({row['error_message']})"
    )
