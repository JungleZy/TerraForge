"""L3: 重试退避 sleep 分段可中断 —— 暂停/停止不必等一觉睡完（最多 10s）。

此前 `await asyncio.sleep(min(2 ** attempt, 10))` 持有信号量且不检查
stop_event，暂停最多延迟 10s 才生效。修复后退避被切成 0.5s 小段，
stop 置位后立刻跳出、回写 pending。
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class _StubConfig:
    def __init__(self, values):
        self._v = values

    def get(self, key, default=None):
        return self._v.get(key, default)


class _FakeResp:
    """恒 500 —— 每次尝试都失败，进入退避分支。"""

    status = 500
    headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, proxy=None):
        return _FakeResp()


def test_retry_backoff_aborts_promptly_on_stop(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    import src.services.dem_download_engine as dde

    engine = dde.DemDownloadEngine()
    engine.config = _StubConfig({
        "dem_cache_enabled": "false",
        "max_retries": "3",
        "request_timeout": "5",
        "concurrent_downloads": "1",
    })

    monkeypatch.setattr(dde.aiohttp, "ClientSession", lambda *a, **k: _FakeSession())
    monkeypatch.setattr(dde.aiohttp, "TCPConnector", lambda *a, **k: None)
    monkeypatch.setattr(dde.aiohttp, "CookieJar", lambda *a, **k: None)

    events = []

    async def progress(granule, status, error, size):
        events.append((granule, status))

    async def run():
        # asyncio.Event 必须在运行中的事件循环里创建（3.13+ 在循环外构造直接
        # 抛 RuntimeError）
        stop = asyncio.Event()

        async def set_stop_soon():
            await asyncio.sleep(0.2)
            stop.set()
        asyncio.create_task(set_stop_soon())
        await engine.download_files(
            dataset="COP-DEM-GLO-30",
            granules=["tile/G.tif"],
            output_dir=tmp_path / "out",
            progress_callback=progress,
            stop_flag=stop,
        )

    started = time.monotonic()
    asyncio.run(run())
    elapsed = time.monotonic() - started

    # 首次退避是 1s（2**0）；不可中断的实现会完整睡完 1s（还要叠加后续
    # 退避或 failed），可中断实现应在 ~0.2s stop 置位后立刻退出。
    assert elapsed < 0.9, f"stop 后仍在退避里睡满了 {elapsed:.2f}s"
    assert events[-1] == ("tile/G.tif", "pending"), f"实际事件: {events}"
