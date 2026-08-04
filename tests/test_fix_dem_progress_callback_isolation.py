"""HIGH #4: progress 回调的异常必须与下载异常隔离 —— 回调（DB/socketio）瞬时
故障只记日志，不得落入引擎的 except Exception 被当成下载失败重试，也不得
击穿无 return_exceptions 的 asyncio.gather 取消其余 granule 协程。
"""

import asyncio
import importlib
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

GRANULE = "ASTGTMV003_N29E106_dem.tif"
PAYLOAD = b"fake-dem-bytes"


def _setup(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("core.database"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("core.database")
    db.init_database()
    dem_mod = importlib.import_module("services.dem_download_engine")
    return db, dem_mod


class _FakeContent:
    def __init__(self, payload):
        self._payload = payload

    async def iter_chunked(self, _n):
        yield self._payload


class _FakeResponse:
    def __init__(self, status, payload=b""):
        self.status = status
        self.headers = {"Content-Length": str(len(payload))} if status == 200 else {}
        self.content = _FakeContent(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_session(monkeypatch, dem_mod, status, payload, calls):
    """把 aiohttp.ClientSession 换成假实现；calls 记录每次 GET 的 URL。"""

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, proxy=None):
            calls.append(url)
            return _FakeResponse(status, payload)

    monkeypatch.setattr(dem_mod.aiohttp, "ClientSession", FakeSession)


def test_downloading_callback_exception_does_not_trigger_retry(monkeypatch, tmp_path):
    """'downloading' 回调抛 sqlite 异常：下载仍一次成功、不重试、文件落盘。"""
    db, dem_mod = _setup(monkeypatch, tmp_path)
    engine = dem_mod.DemDownloadEngine()
    calls = []
    _patch_session(monkeypatch, dem_mod, 200, PAYLOAD, calls)

    seen = []

    async def callback(granule, status, error, size_bytes):
        seen.append(status)
        if status == "downloading":
            raise sqlite3.OperationalError("database is locked")

    out_dir = tmp_path / "out"
    asyncio.run(engine.download_files(
        dataset="COP-DEM-GLO-30",
        granules=[GRANULE],
        output_dir=out_dir,
        progress_callback=callback,
    ))

    assert (out_dir / GRANULE).read_bytes() == PAYLOAD
    assert len(calls) == 1, "回调异常不得被当成下载失败重试"
    assert "completed" in seen


def test_completed_callback_exception_does_not_propagate(monkeypatch, tmp_path):
    """成功后的 'completed' 回调再抛异常：不得传播、不得影响下载结果。"""
    db, dem_mod = _setup(monkeypatch, tmp_path)
    engine = dem_mod.DemDownloadEngine()
    calls = []
    _patch_session(monkeypatch, dem_mod, 200, PAYLOAD, calls)

    async def callback(granule, status, error, size_bytes):
        raise sqlite3.OperationalError("database is locked")

    out_dir = tmp_path / "out"
    asyncio.run(engine.download_files(
        dataset="COP-DEM-GLO-30",
        granules=[GRANULE],
        output_dir=out_dir,
        progress_callback=callback,
    ))

    assert (out_dir / GRANULE).read_bytes() == PAYLOAD
    assert len(calls) == 1


def test_failed_callback_exception_does_not_break_gather(monkeypatch, tmp_path):
    """下载确实失败、'failed' 回调也抛异常：不得击穿 gather 传播出来
    （此前会取消其余 granule 协程、状态半更新）。"""
    db, dem_mod = _setup(monkeypatch, tmp_path)
    # 0 次重试，避免失败路径的退避 sleep 拖慢测试
    cfg_mod = importlib.import_module("services.config_manager")
    cfg_mod.ConfigManager().set("max_retries", "0")

    engine = dem_mod.DemDownloadEngine()
    calls = []
    _patch_session(monkeypatch, dem_mod, 500, b"", calls)

    seen = []

    async def callback(granule, status, error, size_bytes):
        seen.append(status)
        if status == "failed":
            raise sqlite3.OperationalError("database is locked")

    asyncio.run(engine.download_files(
        dataset="COP-DEM-GLO-30",
        granules=[GRANULE],
        output_dir=tmp_path / "out",
        progress_callback=callback,
    ))

    assert "failed" in seen
    assert len(calls) == 1


def test_callback_exception_on_existing_file_does_not_propagate(monkeypatch, tmp_path):
    """dest 已存在走快速路径时回调抛异常：download_files 正常返回。"""
    db, dem_mod = _setup(monkeypatch, tmp_path)
    engine = dem_mod.DemDownloadEngine()

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / GRANULE).write_bytes(PAYLOAD)

    invoked = []

    async def callback(granule, status, error, size_bytes):
        invoked.append(status)
        raise sqlite3.OperationalError("database is locked")

    asyncio.run(engine.download_files(
        dataset="COP-DEM-GLO-30",
        granules=[GRANULE],
        output_dir=out_dir,
        progress_callback=callback,
    ))

    assert invoked == ["completed"]
