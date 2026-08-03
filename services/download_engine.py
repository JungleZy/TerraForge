"""
Download Engine Service

Handles tile coordinate calculation, URL generation, and download orchestration.
Implements Web Mercator projection for converting geographic coordinates to tile coordinates.
"""

import logging
import math
import asyncio
import itertools
import shutil
import tempfile
import threading
import time
import aiohttp
import aiofiles
import os
from typing import List, Tuple, Optional, Dict, Any
from pathlib import Path
from models.task import Tile
from services.config_manager import ConfigManager
from services.tile_url_probe import should_bypass_proxy
from core.config import Config

logger = logging.getLogger(__name__)


def __getattr__(name: str):
    """GDAL 惰性导入(PEP 562,照 contour_engine「模块可 import、重依赖用到才引」的模式)。

    模块加载不再引 osgeo:不走路径拼接的进程(只下载/只复制瓦片、--help 等)
    不用付 GDAL 的 import 成本,缺 GDAL 的环境也能 import 本模块。首个用到
    gdal/osr 的函数经模块属性查找走到这里才真正 import,并写回 globals()
    缓存,后续访问不再进 __getattr__。

    必须保持 de.gdal / de.osr 这两个模块属性形态:既有测试替身直接
    monkeypatch de.osr.SpatialReference(tests/test_tile_georeference.py
    的原子写护栏),若把 import 收进函数体,替身会因模块没有 osr 属性而
    AttributeError。
    """
    if name in ('gdal', 'osr'):
        from osgeo import gdal, osr
        module = {'gdal': gdal, 'osr': osr}[name]
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Constants
WEB_MERCATOR_MAX_LAT = 85.0511  # Maximum valid latitude for Web Mercator projection
WARN_TILES_THRESHOLD = 100000  # 单任务瓦片数软阈值,超过只记警告(0.1.4 起放开硬上限)
MIN_ZOOM = 0  # Minimum zoom level
MAX_ZOOM = 21  # Maximum zoom level

# download_tiles_batch 每批创建的协程数上限。旧实现对全部瓦片一次性
# 预建协程再 gather —— 百万级瓦片就是百万个待调度协程同时挂在事件循环上;
# 分批后任一时刻只有一批协程存活,下载语义(信号量限流、逐瓦片容错)不变。
DOWNLOAD_BATCH_SIZE = 1000

# CRS the per-tile georeferenced intermediates are written in. Single source of
# truth: tile_geotransform returns it and _add_georeference bakes it into the
# intermediate file name, so a stale intermediate written in a *different* CRS
# can never be picked up by the exists() short-circuit.
TILE_GEOREF_EPSG = 3857

# File-name suffix of the per-tile georeferenced intermediate. It encodes every
# property of the content that the exists() short-circuit in _add_georeference
# assumes without re-opening the file:
#   - `3857`: the CRS (see TILE_GEOREF_EPSG)
#   - `rgb`:  pixel values are *colours*, not palette indices
# Whenever the intermediate's contract changes, this suffix must change too.
# The rule exists because intermediates outlive the run that made them: they are
# removed in a finally block, but that unlink only logs a warning on failure
# (a locked file on Windows is enough), and the cache is shared across tasks and
# never cleaned by upgrades. An unchanged name plus a changed contract means the
# short-circuit silently serves content the current code would never produce.
GEOREF_SUFFIX = f'_geo{TILE_GEOREF_EPSG}rgb'

# Suffixes earlier releases wrote. Never reused — only deleted, so the residue
# on users' disks drains away as tiles get re-stitched:
#   `_geo`     — up to 0.0.9: EPSG:4326, wrong projection for a 3857 mosaic
#   `_geo3857` — palette-unaware: paletted tiles left as 1 band of raw indices
LEGACY_GEOREF_SUFFIXES = ('_geo', f'_geo{TILE_GEOREF_EPSG}')

# EPSG:3857 的 WKT,模块级惰性缓存。ImportFromEPSG 每次都要查 PROJ 库,
# 拼接时逐瓦片重建 SpatialReference 是纯浪费;只缓存成功结果,osr 故障
# 时下次调用重算。仍按缓存 WKT 逐瓦片构造 SRS 再导出(见
# _add_georeference),保持「配准阶段过一遍 osr」的既有故障点与异常语义。
_TILE_GEOREF_WKT: Optional[str] = None


def _tile_georef_wkt() -> str:
    """返回 EPSG:3857(TILE_GEOREF_EPSG)的 WKT,首次调用时构建并缓存。"""
    from osgeo import osr  # 惰性 import,见模块级 __getattr__
    global _TILE_GEOREF_WKT
    if _TILE_GEOREF_WKT is None:
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(TILE_GEOREF_EPSG)
        _TILE_GEOREF_WKT = srs.ExportToWkt()
    return _TILE_GEOREF_WKT


class DownloadCancelled(Exception):
    """Raised inside the download path when a task's stop flag is set.

    Lets _download_single_tile distinguish a user cancellation from a genuine
    download failure, so a cancelled tile is reported as 'cancelled' rather than
    'failed' and the retry loop / queued-tile backlog stops immediately.
    """


class NotAnImageResponse(aiohttp.ClientError):
    """HTTP 200 但响应体不是图片（M5）。

    继承 ClientError 使它走 download_tile 既有的重试/服务器轮换逻辑 —— 劫持
    与瞬时故障通常换一台服务器或重试一次即可恢复；重试全部用尽后才向上抛,
    由 _download_single_tile 记为 failed(且【不写缓存】)。
    """


# 瓦片图片的魔数。判定以魔数为准而【不看 Content-Type】:自建瓦片服务返回
# application/octet-stream 是常见且合法的（模块 docstring 把自建服务列为一等
# 用法），按 Content-Type 拒收会误杀它们。
_IMAGE_MAGIC_PREFIXES = (
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"\xff\xd8\xff",        # JPEG
    b"GIF87a",
    b"GIF89a",
    b"BM",                  # BMP
    b"II*\x00",             # TIFF little-endian
    b"MM\x00*",             # TIFF big-endian
)


def looks_like_image(data: bytes) -> bool:
    """响应体前几字节是否是已知图片格式的魔数。

    透明代理 / 酒店 Portal / 运营商劫持在明文 http 链路上返回 200 + HTML 是
    教科书场景（默认 mts0-3 别名就展开成明文 http://），自建服务对越界坐标返
    200 + JSON 同理。这些字节一旦被原子写进共享 cache,0.2.4 起没有自动淘汰
    → 永久命中,且跨任务扩散,除手工清空整个缓存分类外没有恢复途径。
    """
    if len(data) < 12:
        return False
    if data.startswith(_IMAGE_MAGIC_PREFIXES):
        return True
    # WebP: 'RIFF' + 4 字节长度 + 'WEBP'
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


class DownloadEngine:
    """
    Download engine for Google Maps tiles

    Provides tile coordinate calculation using Web Mercator projection,
    tile URL generation, and download orchestration capabilities.

    Web Mercator Projection Formula:
        n = 2^zoom
        x = int((lon + 180) / 360 * n)
        y = int((1 - log(tan(lat_rad) + 1/cos(lat_rad)) / pi) / 2 * n)

    Google Maps Tile URL Format:
        http://mts{server_index}.googleapis.com/vt?lyrs={style}&x={x}&y={y}&z={z}
    """

    def __init__(self):
        """Initialize download engine with config manager"""
        self.config_manager = ConfigManager()
        # tile_servers 列表缓存：get_tile_url 每块瓦片都走，不能每次都查库；
        # 60s TTL 足够让「改配置后新任务生效」（任务通常跑几十分钟以上）。
        self._servers_cache = None
        self._servers_loaded_at = 0.0
        # download_tiles_batch 入口预读的 (max_retries, request_timeout),
        # 供 download_tile 免查询复用;None = 不在批量下载中,自行读配置。
        self._batch_retry_config: Optional[Tuple[int, int]] = None
        # download_tiles_batch 是否物化全量 results 列表。默认 True 保持
        # 「结果数==输入数且按序」的返回契约(tests 钉死);唯一调用方
        # task_manager 刻意不接返回值(completed 清单由 progress_callback
        # 逐块维护),会在调用前置 False 逐批丢弃 —— 百万级瓦片就是百万条
        # 结果 dict 白占内存。为什么是实例属性而不是新方法参数:tests/ 里
        # 多处把 download_tiles_batch 整个换成 (tiles, style,
        # progress_callback, stop_flag=None) 四参替身,多传一个 kwarg
        # 会让全部替身 TypeError。
        self._collect_batch_results = True

    def _tile_servers(self) -> List[str]:
        """读取配置的瓦片服务器列表（60s 缓存；为空回退默认 mts0-3）。"""
        from services.tile_url_probe import parse_server_list
        now = time.monotonic()
        if self._servers_cache is None or now - self._servers_loaded_at > 60:
            raw = self.config_manager.get('tile_servers', '') or ''
            self._servers_cache = parse_server_list(raw)
            self._servers_loaded_at = now
        return self._servers_cache

    def lat_lon_to_tile(self, lat: float, lon: float, zoom: int) -> Tuple[int, int]:
        """
        Convert latitude/longitude to tile coordinates using Web Mercator projection

        Args:
            lat: Latitude in degrees (-85.0511 to 85.0511)
            lon: Longitude in degrees (-180 to 180)
            zoom: Zoom level (0-21)

        Returns:
            Tuple of (x, y) tile coordinates

        Raises:
            ValueError: If zoom level is outside valid range (0-21)

        Note:
            Web Mercator projection has valid latitude range of approximately
            -85.0511 to 85.0511 degrees. Values outside this range will be clamped.
        """
        # Validate zoom level
        if not MIN_ZOOM <= zoom <= MAX_ZOOM:
            raise ValueError(f"Zoom level must be between {MIN_ZOOM} and {MAX_ZOOM}, got {zoom}")

        # Clamp latitude to Web Mercator valid range
        lat = max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, lat))

        # Calculate number of tiles at this zoom level
        n = 2 ** zoom

        # Calculate x coordinate
        x = int((lon + 180.0) / 360.0 * n)

        # Calculate y coordinate using Mercator projection
        lat_rad = math.radians(lat)
        y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)

        # Clamp to valid tile range
        x = max(0, min(n - 1, x))
        y = max(0, min(n - 1, y))

        return x, y

    def _tile_ranges(
        self,
        north: float,
        south: float,
        east: float,
        west: float,
        zoom_min: int,
        zoom_max: int
    ):
        """
        Validate inputs and yield per-zoom tile index ranges.

        Yields:
            Tuples of (zoom, x_min, x_max, y_min, y_max), zoom ascending.

        Raises:
            ValueError: If input parameters are invalid

        Note:
            count_tiles / iter_tiles / calculate_tiles 共用这一段,
            保证三者的计数、顺序、覆盖范围口径完全一致。
        """
        # Coordinate range validation
        if not -90 <= north <= 90:
            raise ValueError(f"North latitude must be between -90 and 90, got {north}")

        if not -90 <= south <= 90:
            raise ValueError(f"South latitude must be between -90 and 90, got {south}")

        if not -180 <= east <= 180:
            raise ValueError(f"East longitude must be between -180 and 180, got {east}")

        if not -180 <= west <= 180:
            raise ValueError(f"West longitude must be between -180 and 180, got {west}")

        if east == west:
            raise ValueError(f"East longitude ({east}) must be different from west longitude ({west})")

        # Input validation
        if north <= south:
            raise ValueError(f"North latitude ({north}) must be greater than south latitude ({south})")

        if not MIN_ZOOM <= zoom_min <= MAX_ZOOM:
            raise ValueError(f"Minimum zoom level must be between {MIN_ZOOM} and {MAX_ZOOM}, got {zoom_min}")

        if not MIN_ZOOM <= zoom_max <= MAX_ZOOM:
            raise ValueError(f"Maximum zoom level must be between {MIN_ZOOM} and {MAX_ZOOM}, got {zoom_max}")

        if zoom_min > zoom_max:
            raise ValueError(f"Minimum zoom ({zoom_min}) must be less than or equal to maximum zoom ({zoom_max})")

        # Iterate through each zoom level
        for zoom in range(zoom_min, zoom_max + 1):
            # Get tile coordinates for corners
            x_min, y_max = self.lat_lon_to_tile(south, west, zoom)
            x_max, y_min = self.lat_lon_to_tile(north, east, zoom)

            # Ensure proper ordering
            if x_min > x_max:
                x_min, x_max = x_max, x_min
            if y_min > y_max:
                y_min, y_max = y_max, y_min

            yield zoom, x_min, x_max, y_min, y_max

    def count_tiles(
        self,
        north: float,
        south: float,
        east: float,
        west: float,
        zoom_min: int,
        zoom_max: int
    ) -> int:
        """
        Count tiles needed for a geographic region without materialising them

        Args:
            north: Northern latitude boundary
            south: Southern latitude boundary
            east: Eastern longitude boundary
            west: Western longitude boundary
            zoom_min: Minimum zoom level
            zoom_max: Maximum zoom level

        Returns:
            Number of tiles covering the region at all zoom levels

        Raises:
            ValueError: If input parameters are invalid

        Note:
            与 calculate_tiles 同口径(共用 _tile_ranges),但只做纯计数。
            大任务(数十万块瓦片)的 create_task 只需要总数,物化 Tile 列表
            既费内存又没必要。
        """
        expected_tile_count = 0
        for zoom, x_min, x_max, y_min, y_max in self._tile_ranges(
            north, south, east, west, zoom_min, zoom_max
        ):
            expected_tile_count += (x_max - x_min + 1) * (y_max - y_min + 1)

        # Warn if tile count is very large. The *hard* limit used to be enforced
        # at task creation; since 0.1.4 it is a soft threshold (the UI asks the
        # user to confirm), so here we only warn.
        if expected_tile_count > WARN_TILES_THRESHOLD:
            logger.warning(
                f"Large tile count detected: {expected_tile_count} tiles. "
                f"This may take a long time to download and process. "
                f"Estimated time: {expected_tile_count / 10 / 3600:.1f} hours at 10 tiles/sec."
            )

        return expected_tile_count

    def iter_tiles(
        self,
        north: float,
        south: float,
        east: float,
        west: float,
        zoom_min: int,
        zoom_max: int,
        task_id: int = 0
    ):
        """
        Lazily yield all tiles for a region in deterministic order

        Args:
            north: Northern latitude boundary
            south: Southern latitude boundary
            east: Eastern longitude boundary
            west: Western longitude boundary
            zoom_min: Minimum zoom level
            zoom_max: Maximum zoom level
            task_id: Task ID for the tiles (default: 0)

        Yields:
            Tile objects, ordered by zoom ascending, then x, then y — exactly
            the order calculate_tiles() materialises.

        Note:
            瓦片集合是 bbox+zoom 的纯函数,可以随时按同一确定性顺序重建。
            恢复任务靠它枚举待下载集合(配合磁盘 cache 判断完成态),
            这是 task_tiles 不再存全量行的前提。
        """
        for zoom, x_min, x_max, y_min, y_max in self._tile_ranges(
            north, south, east, west, zoom_min, zoom_max
        ):
            # Generate all tiles in the range
            for x in range(x_min, x_max + 1):
                for y in range(y_min, y_max + 1):
                    yield Tile(
                        task_id=task_id,
                        zoom=zoom,
                        x=x,
                        y=y,
                        status="pending",
                        retry_count=0
                    )

    def calculate_tiles(
        self,
        north: float,
        south: float,
        east: float,
        west: float,
        zoom_min: int,
        zoom_max: int,
        task_id: int = 0
    ) -> List[Tile]:
        """
        Calculate all tiles needed for a geographic region across zoom levels

        Args:
            north: Northern latitude boundary
            south: Southern latitude boundary
            east: Eastern longitude boundary
            west: Western longitude boundary
            zoom_min: Minimum zoom level
            zoom_max: Maximum zoom level
            task_id: Task ID for the tiles (default: 0)

        Returns:
            List of Tile objects covering the region at all zoom levels

        Raises:
            ValueError: If input parameters are invalid

        Note:
            Tiles are generated for each zoom level from zoom_min to zoom_max (inclusive).
            The number of tiles increases exponentially with zoom level.
            When the count exceeds WARN_TILES_THRESHOLD (100,000) this function only
            logs a warning — the hard rejection lives at the task-creation entry
            point (TaskManager.create_task → API 400), because this calculator is
            also used by callers that merely want the count.
            实现上就是 list(iter_tiles(...)),先过一遍 count_tiles 触发参数
            校验和大任务警告(iter_tiles 是惰性生成器,不消费就不会校验)。
        """
        # count_tiles 先消费一遍 _tile_ranges:参数非法时在这里就抛
        # ValueError(保持历史上的急切校验语义),并输出大任务警告。
        self.count_tiles(north, south, east, west, zoom_min, zoom_max)

        tiles = list(self.iter_tiles(north, south, east, west, zoom_min, zoom_max, task_id=task_id))

        logger.info(
            f"Calculated {len(tiles)} tiles for region "
            f"({south},{west}) to ({north},{east}) "
            f"at zoom levels {zoom_min}-{zoom_max}"
        )

        return tiles

    def get_tile_url(
        self,
        x: int,
        y: int,
        z: int,
        style: str,
        server_index: int = 0
    ) -> str:
        """
        Generate tile URL from the configured tile server list

        Args:
            x: Tile X coordinate
            y: Tile Y coordinate
            z: Zoom level
            style: Map style code (m=roadmap, s=satellite, y=hybrid, t=terrain)
            server_index: Index into the configured tile_servers list
                (rotates on retry; wraps around the list)

        Returns:
            Complete tile URL string

        条目形态见 services.tile_url_probe.expand_server_entry：
        别名/主机按 Google vt 格式拼 lyrs={style}；完整 XYZ 模板按占位符
        展开（模板含 {style} 时替换，不含则样式由地址自身决定）。
        """
        from services.tile_url_probe import expand_server_entry
        servers = self._tile_servers()
        entry = servers[server_index % len(servers)]
        template = expand_server_entry(entry, style)
        return (template
                .replace('{z}', str(z))
                .replace('{x}', str(x))
                .replace('{y}', str(y)))

    def _get_cache_path(self, tile: Tile, style: str) -> Path:
        """
        Get cache file path for a tile

        Args:
            tile: Tile object containing coordinates
            style: Map style code

        Returns:
            Path object for cache file location

        Cache Path Format:
            cache/{style}/{zoom}/{x}/{y}.png —— cache 跨任务共享,
            不带 task_id(见 models/task.py Tile.cache_path)。
        """
        return tile.cache_path(style)

    async def _interruptible_sleep(
        self,
        seconds: float,
        stop_flag: Optional[threading.Event] = None,
        step: float = 0.25,
    ) -> None:
        """Sleep up to ``seconds``, but wake early as soon as stop_flag is set.

        Keeps retry backoff from blocking cancellation: a cancelled task no
        longer has to wait out a multi-second exponential backoff before it
        notices the stop flag and gives up.
        """
        if stop_flag is None:
            await asyncio.sleep(seconds)
            return
        remaining = float(seconds)
        while remaining > 0:
            if stop_flag.is_set():
                return
            chunk = min(step, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk

    async def download_tile(
        self,
        tile: Tile,
        style: str,
        session: aiohttp.ClientSession,
        proxy_url: str = '',
        stop_flag: Optional[threading.Event] = None
    ) -> bytes:
        """
        Download a single tile with retry logic and server rotation

        Args:
            tile: Tile object to download
            style: Map style code
            session: aiohttp ClientSession for making requests
            proxy_url: Proxy URL ('' means no proxy); 由调用方从配置读出传入

        Returns:
            Tile image data as bytes

        Raises:
            aiohttp.ClientError: If download fails after all retries
            asyncio.TimeoutError: If request times out after all retries

        Retry Strategy:
            - Max retries from config (max_retries, 负值按 0 钳制 —— 至少试一次)
            - Server rotation on each retry (按 tile_servers 配置列表轮换)
            - Exponential backoff: 2^attempt seconds between retries
            - Timeout from config (request_timeout)
            - 4xx(429 除外)是永久性错误(404 瓦片不存在、403 禁止访问等),
              重试不会改变结果,直接失败不退避
        """
        # Get configuration values
        # max_retries 钳制到 >=0:负值会让 range(max_retries + 1) 一次都不进,
        # 最后 raise last_error(None) 变成 TypeError,真实错误被吞。
        # 优先用 download_tiles_batch 入口预读的配置(每块瓦片两次
        # config_manager.get 就是两次新开 SQLite 连接,逐瓦片查询是批量下载的
        # 热点);直接调用 download_tile(不经批量入口)时回退为自行读配置。
        # 为什么不加参数显式传入:download_tiles_batch → _download_single_tile
        # → download_tile 这条调用链的签名被既有测试的替身钉死,不能带新参数。
        retry_config = self._batch_retry_config
        if retry_config is not None:
            max_retries, request_timeout = retry_config
        else:
            max_retries = max(0, int(self.config_manager.get('max_retries', '3')))
            request_timeout = int(self.config_manager.get('request_timeout', '30'))

        last_error = None

        for attempt in range(max_retries + 1):
            # Cooperative cancellation: bail out before (re)issuing a request.
            if stop_flag is not None and stop_flag.is_set():
                raise DownloadCancelled()
            try:
                # Rotate server index on each attempt（列表长度来自配置）。
                # 起点加 (x + y):旧实现首 attempt 全部落在 servers[0],叠加
                # 较小的 limit_per_host,所有瓦片的前几个并发把第一台服务器
                # 打满、其余三台闲置 —— 首尝试按瓦片坐标天然分散到各台服务器,
                # 重试仍按 attempt 轮换。
                servers = self._tile_servers()
                server_index = (tile.x + tile.y + attempt) % len(servers)
                url = self.get_tile_url(tile.x, tile.y, tile.zoom, style, server_index)

                logger.debug(
                    f"Downloading tile {tile.zoom}/{tile.x}/{tile.y} "
                    f"from server {servers[server_index]} (attempt {attempt + 1}/{max_retries + 1})"
                )

                # Download with timeout
                # M4: 逐 URL 判断是否绕过代理。proxy_url 是给「访问 Google 等
                # 公网源」配的；套在 127.0.0.1 / 192.168.x.x 这类自建瓦片服务上,
                # 请求会被代理转发到它自己根本到不了的地址(WSL 下尤其明显)。
                # 「验证」与「测速」两条路径早就调 should_bypass_proxy 了,下载
                # 路径一次都没调 —— 于是「验证明明通过了,下载全失败」,而日志
                # 指不到代理这一层。必须在这里逐 URL 判断而不是按批清空:
                # tile_servers 可以混配公网 + 内网。
                # aiohttp 的显式 proxy= 会完全覆盖 trust_env 那一套(包括系统
                # bypass 列表),连 NO_PROXY 都救不了,所以只能自己判。
                effective_proxy = proxy_url or None
                if effective_proxy and should_bypass_proxy(url):
                    logger.debug(f"Bypassing proxy for intranet/loopback tile URL: {url}")
                    effective_proxy = None

                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=request_timeout),
                    proxy=effective_proxy,
                ) as response:
                    response.raise_for_status()
                    data = await response.read()

                    # M5: 200 不代表拿到的是瓦片。不做这道校验的话,劫持返回的
                    # HTML / 自建服务返回的 JSON 会被当成瓦片永久写进共享 cache。
                    if not looks_like_image(data):
                        ctype = getattr(response, "headers", {}).get("Content-Type", "?")
                        raise NotAnImageResponse(
                            f"HTTP 200 but body is not an image "
                            f"(content-type={ctype}, {len(data)} bytes, "
                            f"head={data[:16]!r})"
                        )

                    logger.debug(
                        f"Successfully downloaded tile {tile.zoom}/{tile.x}/{tile.y} "
                        f"({len(data)} bytes)"
                    )

                    return data

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # 4xx(429 限流除外)是永久性错误 —— 404 的瓦片重试多少次都
                # 不存在,指数退避只会把必然失败拖延成分钟级,直接抛出。
                if isinstance(e, aiohttp.ClientResponseError) and (
                    400 <= e.status < 500 and e.status != 429
                ):
                    logger.warning(
                        f"Tile {tile.zoom}/{tile.x}/{tile.y} got HTTP {e.status}, "
                        f"not retrying a permanent client error"
                    )
                    raise
                last_error = e
                logger.warning(
                    f"Failed to download tile {tile.zoom}/{tile.x}/{tile.y} "
                    f"(attempt {attempt + 1}/{max_retries + 1}): "
                    f"{type(e).__name__}: {e!r}"
                )

                # If not the last attempt, wait with exponential backoff
                if attempt < max_retries:
                    backoff_time = 2 ** attempt
                    logger.debug(f"Waiting {backoff_time}s before retry...")
                    await self._interruptible_sleep(backoff_time, stop_flag)

        # If the loop ended because the task was cancelled, surface that rather
        # than the last network error so the tile isn't recorded as 'failed'.
        if stop_flag is not None and stop_flag.is_set():
            raise DownloadCancelled()

        # All retries failed
        error_msg = f"Failed to download tile after {max_retries + 1} attempts: {last_error}"
        logger.error(error_msg)
        raise last_error

    async def _download_single_tile(
        self,
        tile: Tile,
        style: str,
        session: aiohttp.ClientSession,
        cache_enabled: bool,
        progress_callback=None,
        proxy_url: str = '',
        stop_flag: Optional[threading.Event] = None
    ) -> Dict[str, Any]:
        """
        Download a single tile with cache check and progress reporting

        Args:
            tile: Tile object to download
            style: Map style code
            session: aiohttp ClientSession
            cache_enabled: Whether to check/use cache
            progress_callback: Optional async callback function(tile, status, error)

        Returns:
            Dictionary with download result:
                {
                    'tile': Tile object,
                    'status': 'completed' or 'failed',
                    'size': bytes downloaded (if successful),
                    'error': error message (if failed)
                }
        """
        try:
            # Check cache first if enabled
            if cache_enabled:
                try:
                    cache_path = self._get_cache_path(tile, style)
                    if cache_path.exists():
                        # Validate cached file (check size > 0)
                        file_size = await asyncio.to_thread(lambda: cache_path.stat().st_size)

                        if file_size > 0:
                            logger.debug(f"Tile {tile.zoom}/{tile.x}/{tile.y} found in cache ({file_size} bytes)")

                            # Report success from cache
                            if progress_callback:
                                await progress_callback(tile, 'completed', None)

                            return {
                                'tile': tile,
                                'status': 'completed',
                                'size': file_size
                            }
                        else:
                            logger.warning(f"Cached tile {tile.zoom}/{tile.x}/{tile.y} is empty, re-downloading")
                except Exception as cache_error:
                    # Cache access failed (permissions, race condition, etc.)
                    # Log warning and fall through to download
                    logger.warning(
                        f"Cache validation failed for tile {tile.zoom}/{tile.x}/{tile.y}: {cache_error}. "
                        f"Falling back to download."
                    )

            # Download tile
            data = await self.download_tile(tile, style, session, proxy_url=proxy_url, stop_flag=stop_flag)

            if cache_enabled:
                try:
                    cache_path = self._get_cache_path(tile, style)
                    await asyncio.to_thread(lambda: cache_path.parent.mkdir(parents=True, exist_ok=True))
                    part_path = cache_path.with_name(f"{cache_path.name}.part.{os.getpid()}.{id(tile)}")

                    try:
                        async with aiofiles.open(part_path, 'wb') as f:
                            await f.write(data)
                        await asyncio.to_thread(lambda: part_path.replace(cache_path))
                    finally:
                        if part_path.exists():
                            await asyncio.to_thread(part_path.unlink)

                    logger.debug(f"Saved tile {tile.zoom}/{tile.x}/{tile.y} to cache")
                except Exception as cache_write_error:
                    # H2: cache_enabled 下,cache 文件是「这块瓦片已完成」的唯一
                    # 真相 —— 枚举段按 cache 存在且非空重建待下集合,收尾复制也
                    # 从 cache 取。写盘失败却仍上报 completed 的话,这块瓦片会:
                    # 磁盘上不存在任何文件、task_tiles 里没有 failed 行、
                    # tasks.downloaded_tiles 却 +1;而完成判定只数 failed 行,
                    # 任务照标 completed,completed 任务又不允许重启 —— 用户既
                    # 看不到异常、也无法原地续传自愈(tiles_only 全程无声)。
                    # 改为登记失败:稀疏失败表记下它,任务判 failed,点重试即续传。
                    error_msg = (
                        f"cache write failed: "
                        f"{type(cache_write_error).__name__}: {cache_write_error}"
                    )
                    logger.error(
                        f"Failed to write tile {tile.zoom}/{tile.x}/{tile.y} to cache: "
                        f"{cache_write_error}. Recording the tile as failed so the task "
                        f"does not silently report success with a missing tile."
                    )
                    if progress_callback:
                        await progress_callback(tile, 'failed', error_msg)
                    return {
                        'tile': tile,
                        'status': 'failed',
                        'error': error_msg,
                    }

            # Report success
            if progress_callback:
                await progress_callback(tile, 'completed', None)

            return {
                'tile': tile,
                'status': 'completed',
                'size': len(data)
            }

        except DownloadCancelled:
            # Task was cancelled mid-download: don't record this as a failure and
            # don't fire the progress callback — the task is being torn down.
            logger.info(f"Tile {tile.zoom}/{tile.x}/{tile.y} download cancelled")
            return {
                'tile': tile,
                'status': 'cancelled'
            }

        except Exception as e:
            error_msg = f"{type(e).__name__}: {e!r}"
            logger.error(f"Failed to download tile {tile.zoom}/{tile.x}/{tile.y}: {error_msg}")

            # Report failure
            if progress_callback:
                await progress_callback(tile, 'failed', error_msg)

            return {
                'tile': tile,
                'status': 'failed',
                'error': error_msg
            }

    async def download_tiles_batch(
        self,
        tiles,
        style: str,
        progress_callback=None,
        stop_flag: Optional[threading.Event] = None
    ) -> List[Dict[str, Any]]:
        """
        Download multiple tiles concurrently with semaphore control

        Args:
            tiles: Tiles to download — any iterable (list or generator);
                it is consumed lazily in batches of DOWNLOAD_BATCH_SIZE
            style: Map style code
            progress_callback: Optional async callback function(tile, status, error)

        Returns:
            List of download result dictionaries, in input order.
            Empty when self._collect_batch_results is False (task_manager's
            call path — results are consumed per-tile via progress_callback
            instead of being materialised; see __init__).

        Concurrency Control:
            Uses semaphore to limit concurrent downloads based on
            concurrent_downloads config value; coroutines are created in
            batches (DOWNLOAD_BATCH_SIZE) instead of all upfront, so a
            million-tile task never has a million pending coroutines.
        """
        # Get configuration values
        concurrent_downloads = int(self.config_manager.get('concurrent_downloads', '10'))
        request_timeout = int(self.config_manager.get('request_timeout', '30'))
        cache_enabled = (self.config_manager.get('cache_enabled', 'true') or 'true').lower() == 'true'
        proxy_url = self.config_manager.get('proxy_url', '') or ''
        # max_retries/request_timeout 在入口读一次,整批复用 —— 逐瓦片读就是
        # 每块瓦片两次新开 SQLite 连接(见 download_tile 里的回退逻辑)。
        self._batch_retry_config = (
            max(0, int(self.config_manager.get('max_retries', '3'))),
            request_timeout,
        )

        tile_iterator = iter(tiles)

        logger.info(
            f"Starting batch download: concurrency={concurrent_downloads}, "
            f"timeout={request_timeout}s, batch_size={DOWNLOAD_BATCH_SIZE}"
        )

        # Create semaphore for concurrency control
        semaphore = asyncio.Semaphore(concurrent_downloads)

        # Create aiohttp session with connection pooling。
        # limit_per_host 必须跟着并发走:旧版恒为 4(服务器数),4 台服务器
        # 最多 16 条连接,concurrent_downloads 调到 20+ 也被悄悄压死(实测
        # 吞吐差一倍以上);瓦片按 (x+y) 坐标天然轮换服务器(见 download_tile),
        # per-host 不需要再承担均衡职责,与 dem_download_engine 口径一致。
        connector = aiohttp.TCPConnector(limit=concurrent_downloads, limit_per_host=concurrent_downloads)

        results: List[Dict[str, Any]] = []

        # trust_env=True lets aiohttp read HTTP_PROXY/HTTPS_PROXY from env.
        # app.py's apply_system_proxy() populates those from the Windows
        # registry / macOS scutil on startup, so this is what makes the
        # packaged exe usable from behind a Clash/V2Ray system proxy.
        async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:

            async def download_with_semaphore(tile: Tile):
                """Download a single tile with semaphore control"""
                async with semaphore:
                    # Cooperative cancellation: a tile still queued behind the
                    # semaphore when the task is cancelled is skipped here instead
                    # of running its full timeout x retry budget to completion.
                    if stop_flag is not None and stop_flag.is_set():
                        return {'tile': tile, 'status': 'cancelled'}
                    return await self._download_single_tile(
                        tile=tile,
                        style=style,
                        session=session,
                        cache_enabled=cache_enabled,
                        progress_callback=progress_callback,
                        proxy_url=proxy_url,
                        stop_flag=stop_flag
                    )

            # 分批创建协程(见 DOWNLOAD_BATCH_SIZE):不在批次间因 stop_flag
            # 提前退出 —— 每块瓦片都要产出一条结果(queued 的报 'cancelled'),
            # 保持「结果数 == 输入瓦片数」的既有语义。
            # _collect_batch_results=False 时(task_manager 的调用路径,结果
            # 由 progress_callback 逐块消费)逐批丢弃 gather 返回值,不物化
            # 全量 results;result_count 仅为日志计数。
            result_count = 0
            while True:
                batch = list(itertools.islice(tile_iterator, DOWNLOAD_BATCH_SIZE))
                if not batch:
                    break
                batch_results = await asyncio.gather(
                    *(download_with_semaphore(tile) for tile in batch),
                    return_exceptions=False
                )
                result_count += len(batch_results)
                if self._collect_batch_results:
                    results.extend(batch_results)

        logger.info(f"Batch download completed: {result_count} results")

        return results

    def stitch_tiles_with_gdal(
        self,
        tiles: List[Tile],
        style: str,
        output_path: str,
        zoom_level: int,
        target_epsg: int = 4326,
        extra_allowed_dir: str = None
    ) -> str:
        """
        Stitch tiles into a single georeferenced image using GDAL

        Args:
            tiles: List of Tile objects to stitch
            style: Map style code
            output_path: Path for output file (GeoTIFF or PNG)
            zoom_level: Zoom level of the tiles
            target_epsg: CRS of the output file. Defaults to 4326, which is what
                this tool has always produced. Tiles are mosaicked in their
                native EPSG:3857 (see tile_geotransform) and then reprojected
                once at the end. Pass 3857 to skip the reprojection and get the
                resample-free native mosaic.

        Returns:
            Path to the stitched output file

        Process:
            1. Get cache paths for all tiles
            2. Add georeference to each tile (creates _geo3857rgb.tif versions
               in a per-stitch private temp directory)
            3. Build VRT (Virtual Dataset) from georeferenced tiles
            4. Reproject the VRT to target_epsg (skipped when it is already 3857)
            5. Translate VRT to final format with compression
            6. Clean up temporary VRT files and georeferenced tiles

        GDAL Configuration:
            - Compression: from config (gdal_compression)
            - Resampling: from config (gdal_resampling)
            - Output format: GeoTIFF (.tif) or PNG (.png) based on extension
        """
        logger.info(f"Starting GDAL tile stitching: {len(tiles)} tiles at zoom {zoom_level}")

        from osgeo import gdal  # 惰性 import,见模块级 __getattr__

        # Validate and normalize output path
        output_path_obj = Path(output_path).resolve()

        # Validate output path is within allowed directories (cache, output dir,
        # or the calling task's registered output dir — 0.2.4 起保存路径可全盘,
        # 任务产物目录随任务 output_path 走,由调用方经 extra_allowed_dir 传入)
        allowed_dirs = [Config.CACHE_DIR.resolve(), Config.OUTPUT_DIR.resolve()]
        if extra_allowed_dir is not None:
            allowed_dirs.append(Path(extra_allowed_dir).resolve())
        if not any(output_path_obj.is_relative_to(allowed_dir) for allowed_dir in allowed_dirs):
            raise ValueError(
                f"Output path {output_path_obj} is not within allowed directories: "
                f"{', '.join(str(d) for d in allowed_dirs)}"
            )

        # Create output directory if it doesn't exist
        output_path_obj.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Ensured output directory exists: {output_path_obj.parent}")

        # Get configuration values.
        # NOTE: the second argument is only a last-resort fallback for a config
        # table that has no such row (a DB created before the setting existed).
        # The *real* default ships in database.py's DEFAULT_CONFIGS and is
        # 'cubic' for gdal_resampling / 'LZW' for gdal_compression — don't read
        # these literals as "what users get by default".
        gdal_compression = self.config_manager.get('gdal_compression', 'LZW')
        gdal_resampling = self.config_manager.get('gdal_resampling', 'nearest')

        # Filter tiles for the specified zoom level
        tiles_at_zoom = [t for t in tiles if t.zoom == zoom_level]
        if not tiles_at_zoom:
            raise ValueError(f"No tiles found at zoom level {zoom_level}")

        logger.info(f"Processing {len(tiles_at_zoom)} tiles at zoom level {zoom_level}")

        # Intermediates live in a per-stitch private temp directory, NOT next to
        # the cached tiles. They used to be named style/z/x/y inside the shared
        # cache, so two concurrent stitches of overlapping bboxes wrote the same
        # paths and each one's finally-block unlink deleted files the other was
        # still reading. A fresh directory per stitch makes the intermediates
        # private by construction; the rmtree in the finally below replaces the
        # old per-file unlink loop and also covers the failure path (a missing
        # cache tile raises inside the loop below).
        georef_paths: List[str] = []
        vrt_path_obj = output_path_obj.with_suffix('.vrt')
        vrt_path = str(vrt_path_obj)
        warped_path_obj: Optional[Path] = None
        # 中间产物默认落系统临时盘(可能是小容量系统盘,大 zoom 一层可达 GB 级);
        # stitch_tmpdir 配置键(形制同 contour_warp_tmpdir,见 contour_engine)
        # 可指到空间充足的盘,留空 = 系统默认;读取失败回退系统默认,不让配置库
        # 故障拖垮拼接。
        try:
            stitch_tmp_base = (
                self.config_manager.get('stitch_tmpdir', '') or ''
            ).strip() or None
        except Exception as e:
            logger.warning(f"读取 stitch_tmpdir 失败({e!r}),回退系统临时目录")
            stitch_tmp_base = None
        if stitch_tmp_base:
            os.makedirs(stitch_tmp_base, exist_ok=True)
            work_dir = tempfile.mkdtemp(prefix='map_dl_stitch_', dir=stitch_tmp_base)
        else:
            # 保持「只传 prefix」的调用形态:既有测试把 tempfile.mkdtemp 换成
            # lambda prefix=None 的替身来投放毒中间文件(见
            # tests/test_tile_georeference.py _plant_poison_in_work_dir)。
            work_dir = tempfile.mkdtemp(prefix='map_dl_stitch_')

        try:
            # legacy 配准中间产物(_geo.tif / _geo3857.tif,见
            # LEGACY_GEOREF_SUFFIXES)批量清理:每个 cache 目录扫一次,替代
            # 旧版 _add_georeference 里逐瓦片 2 次 exists —— 10 万瓦片同目录
            # 反复扫同一批文件名,是 20 万次纯浪费的 syscall。直调
            # _add_georeference(无 output_dir)的路径仍保留逐瓦片清理
            # (存量契约,见 _add_georeference 里的注释)。
            cleaned_legacy_dirs = set()
            for tile in tiles_at_zoom:
                cache_parent = self._get_cache_path(tile, style).parent
                if cache_parent in cleaned_legacy_dirs:
                    continue
                cleaned_legacy_dirs.add(cache_parent)
                for legacy_suffix in LEGACY_GEOREF_SUFFIXES:
                    for legacy_path in cache_parent.glob(f"*{legacy_suffix}.tif"):
                        try:
                            legacy_path.unlink()
                            logger.debug(f"Removed stale georeferenced tile: {legacy_path}")
                        except Exception as e:
                            logger.warning(
                                f"Failed to remove stale georeferenced tile "
                                f"{legacy_path}: {e}"
                            )

            # Get cache paths and create georeferenced versions.
            # 逐瓦片串行配准在大 mosaic 上是纯 CPU/IO 空转:每块瓦片的输出
            # 都写进本次 stitch 私有的 work_dir(原子 .part + rename),瓦片间
            # 无任何共享状态,可以安全并行。worker 数沿用项目封顶惯例
            # (min(4, cpu_count),同 contour_engine / cesiumlab_terrain)。
            # map 保序,georef_paths 顺序与串行一致;任一瓦片失败时 with 退出
            # 会等所有 worker 收尾,再由 finally 清掉整个 work_dir。
            def _georef_one(tile: Tile) -> str:
                cache_path = self._get_cache_path(tile, style)
                if not cache_path.exists():
                    raise FileNotFoundError(f"Tile not found in cache: {cache_path}")

                # Add georeference to tile (written into the private work_dir)
                return self._add_georeference(str(cache_path), tile, output_dir=work_dir)

            max_workers = min(4, os.cpu_count() or 1)
            if max_workers > 1 and len(tiles_at_zoom) > 1:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    georef_paths = list(pool.map(_georef_one, tiles_at_zoom))
            else:
                georef_paths = [_georef_one(tile) for tile in tiles_at_zoom]

            logger.info(f"Created {len(georef_paths)} georeferenced tiles")

            # Build VRT (Virtual Dataset) from georeferenced tiles
            # Use Path operations instead of string replace for robustness
            logger.info(f"Building VRT: {vrt_path}")

            vrt_options = gdal.BuildVRTOptions(
                resampleAlg=gdal_resampling,
                addAlpha=False
            )
            vrt_ds = gdal.BuildVRT(vrt_path, georef_paths, options=vrt_options)
            if vrt_ds is None:
                raise RuntimeError(f"Failed to build VRT from {len(georef_paths)} tiles")
            try:
                self._assert_vrt_covers_tile_grid(vrt_ds, tiles_at_zoom, zoom_level)
            finally:
                vrt_ds = None  # Close VRT dataset

            logger.info("VRT built successfully")

            # Reproject to the requested CRS if it differs from the tile CRS.
            # Tiles are georeferenced in EPSG:3857 (see tile_geotransform), so a
            # real gdal.Warp is what turns them into correct EPSG:4326 output —
            # not the linear latitude interpolation this code used to do.
            # format='VRT' keeps the reprojection lazy; Translate below is still
            # the only step that writes a full-size raster to disk.
            translate_source = vrt_path
            if target_epsg != TILE_GEOREF_EPSG:
                warped_path_obj = output_path_obj.with_suffix('.warp.vrt')
                logger.info(f"Warping VRT to EPSG:{target_epsg}: {warped_path_obj}")
                warp_ds = gdal.Warp(
                    str(warped_path_obj),
                    vrt_path,
                    format='VRT',
                    dstSRS=f'EPSG:{target_epsg}',
                    resampleAlg=gdal_resampling,
                )
                if warp_ds is None:
                    raise RuntimeError(f"Failed to warp VRT to EPSG:{target_epsg}")
                warp_ds = None
                translate_source = str(warped_path_obj)

            # Translate VRT to final format
            logger.info(f"Translating VRT to final format: {output_path}")

            # Determine output format based on file extension
            output_ext = Path(output_path).suffix.lower()
            if output_ext == '.tif' or output_ext == '.tiff':
                output_format = 'GTiff'
                translate_options = gdal.TranslateOptions(
                    format=output_format,
                    creationOptions=[f'COMPRESS={gdal_compression}', 'TILED=YES'],
                    resampleAlg=gdal_resampling
                )
            elif output_ext == '.png':
                output_format = 'PNG'
                translate_options = gdal.TranslateOptions(
                    format=output_format,
                    resampleAlg=gdal_resampling
                )
            else:
                # Default to GeoTIFF
                output_format = 'GTiff'
                translate_options = gdal.TranslateOptions(
                    format=output_format,
                    creationOptions=[f'COMPRESS={gdal_compression}', 'TILED=YES'],
                    resampleAlg=gdal_resampling
                )

            # M2: 原子写 —— 先写 .part.<pid>，关闭数据集后再 os.replace 到最终
            # 路径。断点判定是「output_path 存在且非空就跳过重拼」，而 GDAL 写
            # GTiff 是边写边落盘，进程被杀 / Translate 抛异常（磁盘写满、目标盘
            # 掉线）留下的必然是【非空】半成品，恰好满足那个判据：孤儿恢复翻
            # paused、用户点继续 → 命中短路 → 该 zoom 记成功 → 任务 completed
            # 无 warning；failed 任务点一次重试同理，warning 还会消失。产物是
            # 损坏状态不确定的 tif（从打不开到下半张空白都可能）。
            # 同文件的瓦片缓存落盘与 _add_georeference 早就是这么写的。
            part_path_obj = output_path_obj.with_name(
                f"{output_path_obj.name}.part.{os.getpid()}")
            try:
                output_ds = gdal.Translate(str(part_path_obj), translate_source, options=translate_options)
                if output_ds is None:
                    raise RuntimeError(f"Failed to translate VRT to {output_path_obj}")
                output_ds = None  # Close output dataset before replacing
                os.replace(str(part_path_obj), str(output_path_obj))
            finally:
                # 异常路径清残件；顺带清 PNG/JPEG 驱动可能写出的 .aux.xml 边车。
                for residue in (part_path_obj,
                                part_path_obj.with_name(part_path_obj.name + '.aux.xml')):
                    try:
                        residue.unlink()
                    except FileNotFoundError:
                        pass
                    except Exception as e:
                        logger.warning(f"Failed to clean up partial output {residue}: {e}")

            logger.info(f"Translation completed: {output_path_obj}")
        finally:
            # Clean up temporary files — on success *and* on failure.
            # 1. Clean up VRT files (mosaic + optional reprojected one)
            for temp_vrt in (vrt_path_obj, warped_path_obj):
                if temp_vrt is None:
                    continue
                try:
                    temp_vrt.unlink()
                    logger.debug(f"Cleaned up VRT file: {temp_vrt}")
                except FileNotFoundError:
                    pass  # never created (failed before this stage)
                except Exception as e:
                    logger.warning(f"Failed to clean up VRT file {temp_vrt}: {e}")

            # 2. Remove the per-stitch private work directory with all
            # georeferenced intermediates in it. ignore_errors keeps the old
            # best-effort semantics: a locked file on Windows must not mask the
            # stitch's real result or exception.
            shutil.rmtree(work_dir, ignore_errors=True)

        logger.info(f"GDAL tile stitching completed: {output_path_obj}")
        return str(output_path_obj)

    def _assert_vrt_covers_tile_grid(
        self,
        vrt_ds,
        tiles_at_zoom: List[Tile],
        zoom_level: int
    ) -> None:
        """Fail loudly when BuildVRT silently dropped tiles from the mosaic.

        gdal.BuildVRT does not fail on a source it cannot use. It prints one
        `Warning 1: ... Skipping <file>` line, returns a perfectly valid dataset,
        and everything downstream (Translate, the task status update) succeeds —
        so the user gets a mosaic covering less ground than they asked for and
        the task still reports "completed". Measured triggers, all of which
        arrive here as a size mismatch:

          - a half-written intermediate: `driver.Create()` alone already puts a
            correctly-*named* file on disk, so a kill/exception between Create
            and SetGeoTransform leaves one behind that the exists() short-circuit
            in _add_georeference then reuses. gdalbuildvrt refuses it with
            "does not support ungeoreferenced image". (The atomic write in
            _add_georeference is the actual fix; this is the backstop for
            residue written by earlier releases.)
          - an intermediate with a different band count, e.g. a leftover RGBA
            tile: "gdalbuildvrt: gdalbuildvrt was called with a band count of 3
            but the file ... has 4 bands. Skipping".
          - an intermediate deleted underneath us. Historical trigger, now
            prevented: intermediates used to be named by style + z/x/y inside
            the shared cache, so the finally block of a concurrent stitch of an
            overlapping bbox deleted files this one was still using. They now
            live in a per-stitch private temp directory (see
            stitch_tiles_with_gdal).

        The expectation is derived from the requested tile grid (x/y extremes ->
        geographic span via tile_geotransform) divided by the pixel size the VRT
        actually ended up with. It is not a restatement of BuildVRT's own
        arithmetic, and it does not assume 256x256 tiles.

        Known blind spot: a tile dropped from the *interior* of the grid leaves
        the bounding box — and therefore the raster size — unchanged. That case
        shows up as a nodata hole in the output, not as a shrunken mosaic.
        """
        geotransform = vrt_ds.GetGeoTransform()
        pixel_width, pixel_height = abs(geotransform[1]), abs(geotransform[5])
        if not pixel_width or not pixel_height:
            raise RuntimeError(
                f"VRT for zoom {zoom_level} has a degenerate pixel size "
                f"{geotransform[1]}x{geotransform[5]}; refusing to stitch"
            )

        x_min = min(t.x for t in tiles_at_zoom)
        x_max = max(t.x for t in tiles_at_zoom)
        y_min = min(t.y for t in tiles_at_zoom)
        y_max = max(t.y for t in tiles_at_zoom)

        # Corner coordinates of the grid: the top-left of tile (x_min, y_min) and
        # the top-left of the tile one step past (x_max, y_max), which is exactly
        # the grid's bottom-right. width/height are irrelevant for a corner, so
        # pass 1x1.
        grid_top_left, _ = self.tile_geotransform(
            Tile(task_id=0, zoom=zoom_level, x=x_min, y=y_min), 1, 1
        )
        grid_past_end, _ = self.tile_geotransform(
            Tile(task_id=0, zoom=zoom_level, x=x_max + 1, y=y_max + 1), 1, 1
        )

        expected_x_size = round((grid_past_end[0] - grid_top_left[0]) / pixel_width)
        expected_y_size = round((grid_top_left[3] - grid_past_end[3]) / pixel_height)

        actual = (vrt_ds.RasterXSize, vrt_ds.RasterYSize)
        if actual != (expected_x_size, expected_y_size):
            raise RuntimeError(
                f"VRT for zoom {zoom_level} covers {actual[0]}x{actual[1]} px but the "
                f"{x_max - x_min + 1}x{y_max - y_min + 1} tile grid requires "
                f"{expected_x_size}x{expected_y_size} px — gdalbuildvrt skipped at least "
                f"one of the {len(tiles_at_zoom)} intermediates (check the GDAL "
                f"'Warning 1: ... Skipping' lines above: unwritable/ungeoreferenced "
                f"leftover, band-count mismatch, or a concurrent stitch deleting them)"
            )

    def tile_geotransform(self, tile: Tile, width: int, height: int) -> tuple[list[float], int]:
        """
        Calculate GDAL geotransform + EPSG code for a single tile.

        Tiles are Web Mercator (EPSG:3857) squares of constant size at a given
        zoom level. Writing them in 3857 plane coordinates makes every pixel
        exactly the same size, so BuildVRT can mosaic them losslessly.

        Writing them as EPSG:4326 with a linearly-interpolated latitude step
        (the previous implementation) is wrong: pixel rows are evenly spaced in
        Mercator y, not in latitude. Peak error inside a single z10 tile at
        40 degrees N is about 14.8 m.

        Args:
            tile: Tile object with zoom/x/y
            width: Tile image width in pixels
            height: Tile image height in pixels

        Returns:
            (geotransform, epsg_code) where geotransform is
            [top_left_x, pixel_width, 0, top_left_y, 0, pixel_height]
        """
        # Half-circumference of the earth at the equator, in metres.
        origin = 20037508.342789244
        tile_span = 2 * origin / (2 ** tile.zoom)

        x0 = -origin + tile.x * tile_span
        y0 = origin - tile.y * tile_span

        geotransform = [x0, tile_span / width, 0, y0, 0, -tile_span / height]
        return geotransform, TILE_GEOREF_EPSG

    def _add_georeference(self, tile_path: str, tile: Tile, output_dir: Optional[str] = None) -> str:
        """
        Add georeference information to a tile image

        Args:
            tile_path: Path to the tile image file
            tile: Tile object with coordinates
            output_dir: Directory for the georeferenced intermediate. When given
                (stitch_tiles_with_gdal passes its per-stitch private temp dir),
                the intermediate is written there instead of next to the cached
                tile — nothing concurrent can then delete or reuse it. When None
                (direct callers), the historical cache-sibling location is used.

        Returns:
            Path to the georeferenced tile file

        Process:
            1. Check if georeferenced version already exists
            2. Open source tile with GDAL
            3. Expand paletted (PNG8) tiles to 3-band RGB — see the comment at
               that branch for why the colour table cannot just be copied
            4. Create georeferenced copy with GTiff driver
            5. Calculate geotransform via tile_geotransform()
            6. Set geotransform and projection (EPSG:3857, Web Mercator)

        Geotransform Calculation:
            Delegated to tile_geotransform(). Tiles are written in EPSG:3857
            plane coordinates (metres), which is the coordinate system the
            tile grid is natively defined in, so every pixel of every tile at
            a given zoom has exactly the same size.

            For tile at (x, y, zoom):
                origin    = 6378137 * π = 20037508.342789244 m
                tile_span = 2 * origin / 2^zoom
                x0        = -origin + x * tile_span
                y0        =  origin - y * tile_span

        Georef Path Format:
            Original: /path/to/tile.png
            Georef:   /path/to/tile_geo3857rgb.tif   (see GEOREF_SUFFIX)

            The name encodes the content's contract — CRS *and* pixel form — on
            purpose, because the exists() short-circuit below trusts the name
            instead of re-opening the file. Leftovers survive on disk whenever a
            stitch aborts part-way (a missing cache tile raises inside the loop
            and the finally-block unlink only warns on failure), the cache is
            shared across tasks, and upgrades never clean it. Two releases have
            already changed the contract:
              - up to 0.0.9 `tile_geo.tif` was EPSG:4326 — reusing one would
                feed a 4326 tile into a 3857 mosaic
              - `tile_geo3857.tif` predates palette expansion — reusing one for
                a roadmap tile would put raw palette indices back into the
                mosaic and the colours would be wrong again
            Both are listed in LEGACY_GEOREF_SUFFIXES and get deleted on sight.
        """
        # Generate georeferenced file path using Path operations
        tile_path_obj = Path(tile_path)
        if output_dir is not None:
            # Per-stitch private directory: starts empty, so the exists()
            # short-circuit below never applies here and no other stitch can
            # see (or delete) this file.
            # 文件名必须带 x:cache 文件名只是 {y}.png(cache/{style}/{z}/{x}/{y}.png),
            # 同 y 不同 x 的瓦片 stem 相同,只用 stem 会在私有目录里互相覆盖,
            # 第二张瓦片命中 exists() 短路拿到第一张的中间文件,VRT 缺列。
            georef_path_obj = Path(output_dir) / (
                f"{tile.x}_{tile_path_obj.stem}{GEOREF_SUFFIX}.tif"
            )
        else:
            georef_path_obj = tile_path_obj.with_stem(
                f"{tile_path_obj.stem}{GEOREF_SUFFIX}"
            ).with_suffix('.tif')
        georef_path = str(georef_path_obj)

        # Opportunistically drop leftovers written by earlier releases so the
        # existing residue on users' disks drains away as tiles get re-stitched
        # instead of sitting there forever.
        # 只在直调路径(无 output_dir)逐瓦片清:stitch 热路径由
        # stitch_tiles_with_gdal 按 cache 目录批量扫(每目录一次),这里再
        # 逐瓦片 2 次 exists 是纯重复。直调路径保留是存量契约 ——
        # tests/test_tile_georeference.py 的 stale 残骸测试直接调用本函数
        # 并断言残骸被顺手清掉。
        if output_dir is None:
            for legacy_suffix in LEGACY_GEOREF_SUFFIXES:
                legacy_path_obj = tile_path_obj.with_stem(
                    f"{tile_path_obj.stem}{legacy_suffix}"
                ).with_suffix('.tif')
                try:
                    if legacy_path_obj.exists():
                        legacy_path_obj.unlink()
                        logger.debug(f"Removed stale georeferenced tile: {legacy_path_obj}")
                except Exception as e:
                    logger.warning(f"Failed to remove stale georeferenced tile {legacy_path_obj}: {e}")

        # Return if already exists
        if georef_path_obj.exists():
            logger.debug(f"Georeferenced tile already exists: {georef_path}")
            return georef_path

        logger.debug(f"Adding georeference to tile {tile.zoom}/{tile.x}/{tile.y}")

        # 惰性 import,见模块级 __getattr__;放在 exists() 短路之后,命中
        # 短路(以及 stitch 私有目录的纯命名路径)不触发 GDAL 加载。
        # 注意:测试替身 patch 的是 de.osr 解析到的同一个 osgeo.osr 模块
        # 对象,本地 import 拿到的就是它,替身语义不变。
        from osgeo import gdal, osr

        # Open source tile
        src_ds = gdal.Open(tile_path)
        if src_ds is None:
            raise RuntimeError(f"Failed to open tile: {tile_path}")

        # Resolve paletted (PNG8) tiles against their own colour table *here*,
        # before any tile meets another one.
        #
        # roadmap/hybrid/roads/terrain tiles come back from Google as PNG8: one
        # band of colour-table *indices* plus the table. The band copy below
        # only moves pixel values, so without this the intermediate keeps the
        # raw indices and every viewer renders them as greyscale — the colours
        # are simply gone. Satellite tiles are already 3-band RGB and skip this
        # branch entirely (no extra read, no extra copy).
        #
        # Carrying the colour table across instead of expanding it does not
        # work: adjacent Google tiles ship *different* tables (measured on three
        # neighbouring roadmap tiles: 167 / 137 / 119 entries, and 132 of the
        # first 137 indices hold different colours). A VRT can only hold one
        # table, so every other tile would be decoded with the wrong one —
        # vivid output with scrambled semantics, which is worse than obviously
        # grey output because it looks correct.
        #
        # Expanding also means the mosaic can be resampled with the user's
        # configured gdal_resampling: interpolating RGB averages colours, which
        # is meaningful. Interpolating palette indices is not, and would have
        # forced 'nearest' regardless of configuration.
        if src_ds.RasterCount == 1 and src_ds.GetRasterBand(1).GetRasterColorTable() is not None:
            logger.debug(f"Expanding paletted tile to RGB: {tile_path}")
            expanded_ds = gdal.Translate('', src_ds, format='MEM', rgbExpand='rgb')
            src_ds = None  # the index band is not needed any more
            if expanded_ds is None:
                raise RuntimeError(f"Failed to expand paletted tile to RGB: {tile_path}")
            # Hand the *only* reference to src_ds, so the `src_ds = None` at the
            # end of this function really is the last one and the MEM dataset is
            # released there rather than lingering until the frame is destroyed.
            src_ds, expanded_ds = expanded_ds, None

        # Get tile dimensions
        width = src_ds.RasterXSize
        height = src_ds.RasterYSize
        bands = src_ds.RasterCount

        # Calculate geotransform (see tile_geotransform for the math)
        geotransform, epsg_code = self.tile_geotransform(tile, width, height)

        # The CRS is baked into georef_path. If they ever diverge the exists()
        # short-circuit starts handing back files whose projection does not
        # match their name — exactly the stale-residue bug the tag prevents.
        if epsg_code != TILE_GEOREF_EPSG:
            raise RuntimeError(
                f"tile_geotransform returned EPSG:{epsg_code} but the intermediate "
                f"file name is tagged EPSG:{TILE_GEOREF_EPSG}; update "
                f"TILE_GEOREF_EPSG so cached intermediates stay self-describing"
            )

        # Write to a sibling .part file and rename into place only once the
        # pixels, geotransform and projection are all in — the same pattern the
        # tile cache uses (see _download_single_tile).
        #
        # Without it the file name stops being a reliable contract: driver.Create()
        # on its own already puts a file called `<tile>_geo3857rgb.tif` on disk, so
        # the user closing the exe (or any exception) between Create and
        # SetProjection leaves behind a name-compliant, content-broken leftover.
        # Two things then conspire: the exists() short-circuit above trusts the
        # name and hands the leftover straight to BuildVRT, and BuildVRT skips an
        # ungeoreferenced source with a warning instead of failing. The leftover is
        # also invisible to stitch_tiles_with_gdal's finally block, which only
        # cleans paths this function successfully *returned*.
        part_path_obj = georef_path_obj.with_name(
            f"{georef_path_obj.name}.part.{os.getpid()}.{id(tile)}"
        )
        dst_ds = None  # bound up front so the finally block can always close it
        try:
            # Create georeferenced output file.
            # DEFLATE 无损压缩:旧版默认无压缩 GTiff 每瓦片固定 ~196KB
            # (256x256x3),而源瓦片才 10-60KB —— 10 万瓦片的 zoom 就是
            # ~15GB 的中间产物写+读。压缩只改磁盘体积,文件名/波段/像素
            # 契约(见 GEOREF_SUFFIX)不变,BuildVRT 读取透明。
            driver = gdal.GetDriverByName('GTiff')
            dst_ds = driver.Create(
                str(part_path_obj),
                width,
                height,
                bands,
                src_ds.GetRasterBand(1).DataType,
                options=['COMPRESS=DEFLATE']
            )

            if dst_ds is None:
                raise RuntimeError(f"Failed to create georeferenced tile: {part_path_obj}")

            # Copy raster data
            for band_idx in range(1, bands + 1):
                src_band = src_ds.GetRasterBand(band_idx)
                dst_band = dst_ds.GetRasterBand(band_idx)
                data = src_band.ReadAsArray()
                dst_band.WriteArray(data)

            # Set geotransform
            dst_ds.SetGeoTransform(geotransform)

            # Set projection to Web Mercator (EPSG:3857) — see tile_geotransform
            # WKT 走模块级缓存(见 _tile_georef_wkt),不再逐瓦片 ImportFromEPSG
            # 查 PROJ 库;仍逐瓦片构造 SRS 并导出,故障点与旧实现一致。
            srs = osr.SpatialReference(_tile_georef_wkt())
            dst_ds.SetProjection(srs.ExportToWkt())

            # Close the dataset *before* the rename: GDAL flushes on close, so
            # renaming an open dataset would publish a half-flushed file.
            dst_ds = None

            os.replace(part_path_obj, georef_path_obj)
        finally:
            # Close datasets
            src_ds = None
            dst_ds = None
            # Still there means the rename never happened — drop the debris so a
            # retry starts clean and the shared cache dir doesn't accumulate it.
            try:
                if part_path_obj.exists():
                    part_path_obj.unlink()
            except Exception as cleanup_error:
                logger.warning(
                    f"Failed to remove partial georeferenced tile {part_path_obj}: "
                    f"{cleanup_error}"
                )

        logger.debug(f"Created georeferenced tile: {georef_path}")
        return georef_path
