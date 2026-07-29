"""I13: 下载完整性校验 —— Content-Length 与实际字节数不符视为失败。

截断文件过去 size>0 即视为完成并被镜像进全局缓存，永久污染后续所有任务。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


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


def _make_engine(monkeypatch, tmp_path, resp, max_retries="0"):
    import config
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    import services.dem_download_engine as dde

    engine = dde.DemDownloadEngine()
    engine.config = _StubConfig({
        "dem_cache_enabled": "true",
        "max_retries": max_retries,
        "request_timeout": "5",
        "concurrent_downloads": "2",
    })
    monkeypatch.setattr(dde.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(resp))
    monkeypatch.setattr(dde.aiohttp, "TCPConnector", lambda *a, **k: None)
    monkeypatch.setattr(dde.aiohttp, "CookieJar", lambda *a, **k: None)
    return engine


def test_truncated_download_fails_and_does_not_pollute_cache(monkeypatch, tmp_path):
    payload = b"x" * 40
    engine = _make_engine(
        monkeypatch, tmp_path,
        _FakeResp(status=200, chunks=[payload], headers={"Content-Length": "100"}),
    )

    events = []

    async def progress(granule, status, error, size):
        events.append((granule, status))

    asyncio.run(engine.download_files(
        dataset="COP-DEM-GLO-30",
        granules=["G.tif"],
        output_dir=tmp_path / "out",
        progress_callback=progress,
        stop_flag=None,
    ))

    assert ("G.tif", "failed") in events, (
        f"字节数与 Content-Length 不符必须判失败，实际: {events}"
    )
    assert not (tmp_path / "out" / "G.tif").exists(), "截断文件不能落盘为完成品"
    assert not (tmp_path / "cache" / "dem" / "G.tif").exists(), "截断文件不能进全局缓存"


def test_full_download_still_completes_and_caches(monkeypatch, tmp_path):
    """回归护栏：字节数吻合的正常下载照常完成并写缓存。"""
    payload = b"x" * 40
    engine = _make_engine(
        monkeypatch, tmp_path,
        _FakeResp(status=200, chunks=[payload], headers={"Content-Length": "40"}),
    )

    events = []

    async def progress(granule, status, error, size):
        events.append((granule, status))

    asyncio.run(engine.download_files(
        dataset="COP-DEM-GLO-30",
        granules=["G.tif"],
        output_dir=tmp_path / "out",
        progress_callback=progress,
        stop_flag=None,
    ))

    assert ("G.tif", "completed") in events
    assert (tmp_path / "out" / "G.tif").read_bytes() == payload
    assert (tmp_path / "cache" / "dem" / "G.tif").read_bytes() == payload
