"""
Unit tests for DownloadEngine service
"""

import pytest
import os
import sys
import tempfile
import shutil
import asyncio
import math
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core import database
from src.services.download_engine import DownloadEngine, WARN_TILES_THRESHOLD, MIN_ZOOM, MAX_ZOOM
from src.models.task import Tile
from src.core.config import Config
from src.contracts.outcome import TileOutcome


@pytest.fixture
def test_db(monkeypatch):
    """Create a temporary test database"""
    # Create temporary directory for test database
    temp_dir = tempfile.mkdtemp()
    test_db_path = Path(temp_dir) / 'test.db'

    # Override config paths (must be Path — config.init_app uses .parent).
    # monkeypatch restores them at teardown.
    monkeypatch.setattr(Config, 'DATABASE_PATH', test_db_path)
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', Path(temp_dir) / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', Path(temp_dir) / 'cache')

    # Initialize test database
    database.init_database()

    yield str(test_db_path)

    # Cleanup
    shutil.rmtree(temp_dir)


@pytest.fixture
def download_engine(test_db):
    """Create DownloadEngine instance with test database"""
    return DownloadEngine()


def test_lat_lon_to_tile_zoom_10(download_engine):
    """Test converting lat/lon to tile coordinates at zoom level 10"""
    # Test Beijing coordinates (39.9, 116.4) at zoom 10
    x, y = download_engine.lat_lon_to_tile(39.9, 116.4, 10)

    # Expected values calculated using Web Mercator projection
    # n = 2^10 = 1024
    # x = int((116.4 + 180) / 360 * 1024) = int(843.093...) = 843
    # y calculation is more complex due to Mercator projection
    assert x == 843
    assert y == 388  # Corrected based on actual Web Mercator calculation

    # Test edge cases
    # Equator, Prime Meridian at zoom 10
    x, y = download_engine.lat_lon_to_tile(0, 0, 10)
    assert x == 512  # Middle of x range
    assert y == 512  # Middle of y range

    # North pole area (85.0511 is max latitude for Web Mercator)
    x, y = download_engine.lat_lon_to_tile(85.0, 0, 10)
    assert x == 512
    assert y == 0 or y == 1  # Near top

    # South pole area
    x, y = download_engine.lat_lon_to_tile(-85.0, 0, 10)
    assert x == 512
    assert y == 1023 or y == 1022  # Near bottom


def test_calculate_tiles_single_zoom(download_engine):
    """Test calculating tiles for a single zoom level"""
    # Small area: Beijing city center
    # North: 40.0, South: 39.8, East: 116.5, West: 116.3
    # Zoom: 10
    tiles = download_engine.calculate_tiles(
        north=40.0,
        south=39.8,
        east=116.5,
        west=116.3,
        zoom_min=10,
        zoom_max=10
    )

    # Should return a list of Tile objects
    assert isinstance(tiles, list)
    assert len(tiles) > 0

    # All tiles should be at zoom level 10
    for tile in tiles:
        assert isinstance(tile, Tile)
        assert tile.zoom == 10
        assert tile.status == "pending"
        assert tile.retry_count == 0

    # Check that tiles cover the expected range
    x_coords = [tile.x for tile in tiles]
    y_coords = [tile.y for tile in tiles]

    # Should have at least 1 tile in each direction
    assert len(set(x_coords)) >= 1
    assert len(set(y_coords)) >= 1

    # Coordinates should be reasonable for zoom 10
    assert all(0 <= x < 1024 for x in x_coords)
    assert all(0 <= y < 1024 for y in y_coords)


def test_calculate_tiles_range(download_engine):
    """Test calculating tiles for a range of zoom levels"""
    # Very small area to keep tile count manageable
    # North: 40.0, South: 39.9, East: 116.4, West: 116.3
    # Zoom: 8 to 10
    tiles = download_engine.calculate_tiles(
        north=40.0,
        south=39.9,
        east=116.4,
        west=116.3,
        zoom_min=8,
        zoom_max=10
    )

    # Should return tiles for all zoom levels
    assert isinstance(tiles, list)
    assert len(tiles) > 0

    # Check that we have tiles at each zoom level
    zoom_levels = set(tile.zoom for tile in tiles)
    assert 8 in zoom_levels
    assert 9 in zoom_levels
    assert 10 in zoom_levels

    # Count tiles per zoom level
    tiles_per_zoom = {}
    for tile in tiles:
        zoom = tile.zoom
        if zoom not in tiles_per_zoom:
            tiles_per_zoom[zoom] = 0
        tiles_per_zoom[zoom] += 1

    # Higher zoom levels should generally have more tiles (4x per zoom level)
    # But for small areas, this might not always hold
    assert tiles_per_zoom[8] >= 1
    assert tiles_per_zoom[9] >= 1
    assert tiles_per_zoom[10] >= 1

    # All tiles should have proper attributes
    for tile in tiles:
        assert isinstance(tile, Tile)
        assert 8 <= tile.zoom <= 10
        assert tile.x >= 0
        assert tile.y >= 0
        assert tile.status == "pending"


def test_get_tile_url(download_engine):
    """Test generating Google Maps tile URL"""
    # Test with different parameters
    url = download_engine.get_tile_url(x=843, y=368, z=10, style='m', server_index=0)

    # Should match Google Maps tile URL format
    assert url.startswith('http://mts0.googleapis.com/vt')
    assert 'lyrs=m' in url
    assert 'x=843' in url
    assert 'y=368' in url
    assert 'z=10' in url

    # Test with different server index
    url = download_engine.get_tile_url(x=100, y=200, z=5, style='s', server_index=2)
    assert url.startswith('http://mts2.googleapis.com/vt')
    assert 'lyrs=s' in url
    assert 'x=100' in url
    assert 'y=200' in url
    assert 'z=5' in url

    # Test with all server indices
    for i in range(4):
        url = download_engine.get_tile_url(x=0, y=0, z=0, style='m', server_index=i)
        assert url.startswith(f'http://mts{i}.googleapis.com/vt')


def test_tile_count_warning(download_engine, caplog):
    """A request that would generate more than WARN_TILES_THRESHOLD tiles
    should log a warning. The product no longer hard-rejects large jobs."""
    import logging

    with caplog.at_level(logging.WARNING, logger='src.services.download_engine'):
        tiles = download_engine.calculate_tiles(
            north=50.0,
            south=30.0,
            east=130.0,
            west=100.0,
            zoom_min=10,
            zoom_max=15,
        )

    assert len(tiles) > WARN_TILES_THRESHOLD
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Large tile count" in m for m in warnings)


def test_input_validation_north_south(download_engine):
    """Test that north must be greater than south"""
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=39.0,
            south=40.0,  # south > north (invalid)
            east=116.5,
            west=116.3,
            zoom_min=10,
            zoom_max=10
        )

    assert "North latitude" in str(exc_info.value)
    assert "must be greater than south latitude" in str(exc_info.value)


def test_input_validation_zoom_range(download_engine):
    """Test that zoom levels must be in valid range"""
    # Test zoom_min too low
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=116.5,
            west=116.3,
            zoom_min=-1,  # Invalid
            zoom_max=10
        )

    assert "Minimum zoom level must be between" in str(exc_info.value)

    # Test zoom_max too high
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=116.5,
            west=116.3,
            zoom_min=10,
            zoom_max=22  # Invalid
        )

    assert "Maximum zoom level must be between" in str(exc_info.value)


def test_input_validation_zoom_min_max(download_engine):
    """Test that zoom_min must be <= zoom_max"""
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=116.5,
            west=116.3,
            zoom_min=15,
            zoom_max=10  # zoom_min > zoom_max (invalid)
        )

    assert "Minimum zoom" in str(exc_info.value)
    assert "must be less than or equal to maximum zoom" in str(exc_info.value)


def test_lat_lon_to_tile_invalid_zoom(download_engine):
    """Test that invalid zoom levels are rejected"""
    # Test zoom too low
    with pytest.raises(ValueError) as exc_info:
        download_engine.lat_lon_to_tile(40.0, 116.0, -1)

    assert "Zoom level must be between" in str(exc_info.value)

    # Test zoom too high
    with pytest.raises(ValueError) as exc_info:
        download_engine.lat_lon_to_tile(40.0, 116.0, 25)

    assert "Zoom level must be between" in str(exc_info.value)


def test_coordinate_range_validation_latitude(download_engine):
    """Test that latitude must be between -90 and 90"""
    # Test north latitude too high
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=95.0,  # Invalid: > 90
            south=39.0,
            east=116.5,
            west=116.3,
            zoom_min=10,
            zoom_max=10
        )

    assert "North latitude must be between -90 and 90" in str(exc_info.value)

    # Test north latitude too low
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=-95.0,  # Invalid: < -90
            south=-96.0,
            east=116.5,
            west=116.3,
            zoom_min=10,
            zoom_max=10
        )

    assert "North latitude must be between -90 and 90" in str(exc_info.value)

    # Test south latitude too high
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=95.0,  # Invalid: > 90
            east=116.5,
            west=116.3,
            zoom_min=10,
            zoom_max=10
        )

    assert "South latitude must be between -90 and 90" in str(exc_info.value)

    # Test south latitude too low
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=-95.0,  # Invalid: < -90
            east=116.5,
            west=116.3,
            zoom_min=10,
            zoom_max=10
        )

    assert "South latitude must be between -90 and 90" in str(exc_info.value)


def test_coordinate_range_validation_longitude(download_engine):
    """Test that longitude must be between -180 and 180"""
    # Test east longitude too high
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=185.0,  # Invalid: > 180
            west=116.3,
            zoom_min=10,
            zoom_max=10
        )

    assert "East longitude must be between -180 and 180" in str(exc_info.value)

    # Test east longitude too low
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=-185.0,  # Invalid: < -180
            west=-186.0,
            zoom_min=10,
            zoom_max=10
        )

    assert "East longitude must be between -180 and 180" in str(exc_info.value)

    # Test west longitude too high
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=116.5,
            west=185.0,  # Invalid: > 180
            zoom_min=10,
            zoom_max=10
        )

    assert "West longitude must be between -180 and 180" in str(exc_info.value)

    # Test west longitude too low
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=116.5,
            west=-185.0,  # Invalid: < -180
            zoom_min=10,
            zoom_max=10
        )

    assert "West longitude must be between -180 and 180" in str(exc_info.value)


def test_coordinate_validation_east_west_equal(download_engine):
    """Test that east and west longitude must be different"""
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=116.5,
            west=116.5,  # Invalid: east == west
            zoom_min=10,
            zoom_max=10
        )

    assert "must be different from west longitude" in str(exc_info.value)


def test_download_tiles_batch_passes_cache_enabled_false(download_engine):
    from src.services.config_manager import ConfigManager

    ConfigManager().set('cache_enabled', 'false')
    seen = []

    async def fake_single(tile, style, session, cache_enabled, progress_callback=None, proxy_url='', stop_flag=None):
        seen.append(cache_enabled)
        return {'tile': tile, 'status': 'completed'}

    download_engine._download_single_tile = fake_single

    asyncio.run(download_engine.download_tiles_batch([Tile(task_id=1, zoom=0, x=0, y=0)], 's'))

    assert seen == [False]


def test_download_single_tile_does_not_use_cache_when_disabled(download_engine):
    tile = Tile(task_id=1, zoom=0, x=0, y=0)
    cache_path = tile.cache_path('s')
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b'cached')
    downloaded = {'called': False}

    async def fake_download_tile(tile, style, session, proxy_url='', stop_flag=None):
        downloaded['called'] = True
        return b'fresh'

    download_engine.download_tile = fake_download_tile

    result = asyncio.run(download_engine._download_single_tile(tile, 's', session=None, cache_enabled=False))

    assert downloaded['called'] is True
    # 引擎结果字典的 status 现在是 `TileOutcome` 的值('success',旧 'completed')
    assert result['status'] == TileOutcome.SUCCESS.value
    assert cache_path.read_bytes() == b'cached'


def test_download_single_tile_writes_cache_atomically(download_engine):
    tile = Tile(task_id=1, zoom=0, x=1, y=0)
    cache_path = tile.cache_path('s')

    async def fake_download_tile(tile, style, session, proxy_url='', stop_flag=None):
        return b'fresh'

    download_engine.download_tile = fake_download_tile

    result = asyncio.run(download_engine._download_single_tile(tile, 's', session=None, cache_enabled=True))

    assert result['status'] == TileOutcome.SUCCESS.value
    assert cache_path.read_bytes() == b'fresh'
    assert list(cache_path.parent.glob('*.part.*')) == []


# M5 起下载路径按魔数校验响应体,替身必须返回真实 PNG 头(否则会被当成
# 劫持/错误页拒收) —— 这正是该校验要拦的东西。
_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_download_tile_passes_proxy_to_session(download_engine):
    """调用方传入的 proxy_url 原样透传给 session.get(空串归一为 None)。"""
    seen = []

    class FakeResponse:
        headers = {'Content-Type': 'image/png'}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            pass

        async def read(self):
            return _FAKE_PNG

    class FakeSession:
        def get(self, url, timeout=None, proxy=None):
            seen.append(proxy)
            return FakeResponse()

    data = asyncio.run(download_engine.download_tile(
        Tile(task_id=1, zoom=0, x=0, y=0), 's', FakeSession(),
        proxy_url='http://127.0.0.1:7890',
    ))

    assert data == _FAKE_PNG
    assert seen == ['http://127.0.0.1:7890']


# ---------------------------------------------------------------------------
# 重试策略:4xx(429 除外)永久错误不重试;max_retries 负值按 0 钳制
# ---------------------------------------------------------------------------

def _client_response_error(status):
    """构造一个带状态码的 aiohttp.ClientResponseError(raise_for_status 的抛出物)。"""
    import aiohttp
    from aiohttp import RequestInfo
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    request_info = RequestInfo(
        URL('http://example.invalid/tile'), 'GET',
        CIMultiDictProxy(CIMultiDict()), URL('http://example.invalid/tile'),
    )
    return aiohttp.ClientResponseError(
        request_info, (), status=status, message='error'
    )


def _failing_session(attempts, error):
    class FailingResponse:
        async def __aenter__(self):
            raise error

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeSession:
        def get(self, url, timeout=None, proxy=None):
            attempts.append(url)
            return FailingResponse()

    return FakeSession()


@pytest.mark.parametrize('status', [400, 403, 404])
def test_download_tile_permanent_4xx_is_not_retried(download_engine, status):
    """404 的瓦片重试多少次都不存在:永久 4xx 第一次失败就直接抛出。"""
    import aiohttp
    from src.services.config_manager import ConfigManager

    ConfigManager().set('max_retries', '3')
    attempts = []

    with pytest.raises(aiohttp.ClientResponseError):
        asyncio.run(download_engine.download_tile(
            Tile(task_id=1, zoom=0, x=0, y=0), 's',
            _failing_session(attempts, _client_response_error(status)),
        ))

    assert len(attempts) == 1, f"HTTP {status} 不应退避重试,实际请求了 {len(attempts)} 次"


def test_download_tile_429_is_retried(download_engine, monkeypatch):
    """429 是限流不是永久错误,仍走指数退避重试。"""
    import aiohttp
    from src.services.config_manager import ConfigManager

    ConfigManager().set('max_retries', '2')

    async def no_sleep(seconds, stop_flag=None, step=0.25):
        return

    monkeypatch.setattr(download_engine, '_interruptible_sleep', no_sleep)
    attempts = []

    with pytest.raises(aiohttp.ClientResponseError):
        asyncio.run(download_engine.download_tile(
            Tile(task_id=1, zoom=0, x=0, y=0), 's',
            _failing_session(attempts, _client_response_error(429)),
        ))

    assert len(attempts) == 3  # 1 + max_retries(2)


def test_download_tile_negative_max_retries_clamped(download_engine):
    """max_retries 为负时 range(0) 一次都不进,修复前 raise None 变 TypeError
    吞掉真实错误;钳制到 0 后恰好试一次并抛出真实的网络错误。
    (负值进不了 ConfigManager.set 的校验,这里直接写库模拟历史脏数据。)"""
    import aiohttp
    from src.core.database import get_connection

    conn = get_connection()
    try:
        conn.execute("UPDATE config SET value = '-5' WHERE key = 'max_retries'")
        conn.commit()
    finally:
        conn.close()

    attempts = []

    with pytest.raises(aiohttp.ClientResponseError):
        asyncio.run(download_engine.download_tile(
            Tile(task_id=1, zoom=0, x=0, y=0), 's',
            _failing_session(attempts, _client_response_error(500)),
        ))

    assert len(attempts) == 1


# ---------------------------------------------------------------------------
# Cooperative cancellation (stop_flag) — mirrors the DEM engine's behaviour so
# that cancelling a task stops in-flight retries and skips queued tiles instead
# of letting every tile run its full timeout x retry budget to completion.
# ---------------------------------------------------------------------------

def test_download_tile_does_not_request_when_stop_flag_already_set(download_engine):
    """If the task is already cancelled, download_tile must not hit the network."""
    import threading
    from src.services.download_engine import DownloadCancelled

    stop = threading.Event()
    stop.set()

    calls = []

    class FakeSession:
        def get(self, url, timeout=None, proxy=None):
            calls.append(url)
            raise AssertionError("must not download once cancelled")

    with pytest.raises(DownloadCancelled):
        asyncio.run(download_engine.download_tile(
            Tile(task_id=1, zoom=0, x=0, y=0), 's', FakeSession(), stop_flag=stop
        ))

    assert calls == []


def test_download_tile_stops_retrying_after_stop_flag_set(download_engine):
    """Reproduces the bug: cancelling mid-retry must abort the retry loop at once.

    With max_retries=3 an unresponsive tile would normally be attempted 4 times.
    Once the stop flag is set after the first failure, no further attempt may run.
    """
    import threading
    from src.services.download_engine import DownloadCancelled

    stop = threading.Event()
    attempts = []

    class FailingResponse:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeSession:
        def get(self, url, timeout=None, proxy=None):
            attempts.append(url)
            stop.set()  # user cancels during the very first attempt
            return FailingResponse()

    with pytest.raises(DownloadCancelled):
        asyncio.run(download_engine.download_tile(
            Tile(task_id=1, zoom=0, x=0, y=0), 's', FakeSession(), stop_flag=stop
        ))

    assert len(attempts) == 1


def test_download_tiles_batch_skips_queued_tiles_when_stopped(download_engine):
    """A cancelled batch must skip tiles that have not started downloading yet."""
    import threading

    stop = threading.Event()
    stop.set()

    downloaded = []

    async def fake_single(tile, style, session, cache_enabled,
                          progress_callback=None, proxy_url='', stop_flag=None):
        downloaded.append(tile)
        return {'tile': tile, 'status': 'completed'}

    download_engine._download_single_tile = fake_single

    tiles = [Tile(task_id=1, zoom=0, x=i, y=0) for i in range(5)]
    results = asyncio.run(download_engine.download_tiles_batch(tiles, 's', stop_flag=stop))

    assert downloaded == []
    assert all(r['status'] == 'cancelled' for r in results)
