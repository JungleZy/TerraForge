"""M18: 下载途中暂停（stop）也必须清理 .part 临时文件。

此前 except 分支里 stop 路径在 .part 清理代码之前 return，中断路径留下
半成品 .part（与自身注释"失败/中断不留垃圾"矛盾）。修复后清理先于
stop 判断执行。
"""

import asyncio
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class _StubConfig:
    def __init__(self, values):
        self._v = values

    def get(self, key, default=None):
        return self._v.get(key, default)


class _FakeResp:
    """iter_chunked 在产出首块后置 stop，再产第二块 —— 模拟下载途中暂停。"""

    def __init__(self, stop_event):
        self.status = 200
        self.headers = {"Content-Length": "8"}
        self.content = self
        self._stop = stop_event

    async def iter_chunked(self, _n):
        yield b"aaaa"
        self._stop.set()
        yield b"bbbb"

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


def test_stop_mid_download_leaves_no_part_file(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    import services.dem_download_engine as dde

    engine = dde.DemDownloadEngine()
    engine.config = _StubConfig({
        "dem_cache_enabled": "true",
        "max_retries": "3",
        "request_timeout": "5",
        "concurrent_downloads": "2",
    })

    monkeypatch.setattr(dde.aiohttp, "TCPConnector", lambda *a, **k: None)
    monkeypatch.setattr(dde.aiohttp, "CookieJar", lambda *a, **k: None)

    events = []

    async def progress(granule, status, error, size):
        events.append((granule, status))

    out = tmp_path / "out"

    async def main():
        # asyncio.Event 必须在运行中的事件循环里创建（3.13+ 在循环外构造直接
        # 抛 RuntimeError），依赖它的 _FakeResp 一并挪进来
        stop = asyncio.Event()
        resp = _FakeResp(stop)
        monkeypatch.setattr(dde.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(resp))
        await engine.download_files(
            dataset="COP-DEM-GLO-30",
            granules=["tile/G.tif"],
            output_dir=out,
            progress_callback=progress,
            stop_flag=stop,
        )

    asyncio.run(main())

    # 暂停语义：回写 pending（不是 failed）
    assert events[-1] == ("tile/G.tif", "pending"), f"实际事件: {events}"
    # 关键断言：不留 .part 残留，也不落半成品 dest
    assert not (out / "G.tif.part").exists(), "stop 路径不得留下 .part 残留"
    assert not (out / "G.tif").exists()
