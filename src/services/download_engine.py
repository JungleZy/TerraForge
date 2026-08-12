"""
Download Engine Service

Handles tile coordinate calculation, URL generation, and download orchestration.
Implements Web Mercator projection for converting geographic coordinates to tile coordinates.
"""

import logging
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
from src.contracts.outcome import TileOutcome
from src.contracts.region import RegionSpec
# 经纬度 → 瓦片的数学**全部**来自 region_tiles，本模块一行都不再自己算。
# 以前这里有一份 Web Mercator 公式、dem_task_tiler 有一份、前端 map.js 又有
# 一份,三处各自演化就是「预估 17 万块、实下 3 万块」那类偏差的温床。
# MIN_ZOOM / MAX_ZOOM / WEB_MERCATOR_MAX_LAT 在这里**重新导出**(不是重新定义):
# tests 与 dem 侧都 `from src.services.download_engine import MIN_ZOOM, MAX_ZOOM`,
# 而值只此一份 —— 抄一份就等于允许两个上界。
from src.contracts.region_tiles import (MAX_ZOOM, MIN_ZOOM,
                                        WEB_MERCATOR_MAX_LAT,
                                        bbox_tile_range, count_region_tiles,
                                        iter_region_tile_spans,
                                        lat_lon_to_tile as _lat_lon_to_tile,
                                        validate_zoom_range)
from src.contracts.source import SourceSnapshot
from src.models.task import Tile
from src.services.config_manager import ConfigManager
from src.services.proxy_autodetect import resolve_from_config
from src.services.tile_url_probe import should_bypass_proxy
from src.core.config import Config
from src.core.gdal_mode import pin_gdal_exception_mode

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
        pin_gdal_exception_mode()  # 见 src/core/gdal_mode.py
        module = {'gdal': gdal, 'osr': osr}[name]
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Constants
WARN_TILES_THRESHOLD = 100000  # 单任务瓦片数软阈值,超过只记警告(0.1.4 起放开硬上限)

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
    pin_gdal_exception_mode()  # 见 src/core/gdal_mode.py
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


class StitchCancelled(Exception):
    """拼接过程中检测到任务停止标志时抛出。

    与拼接**失败**必须分开:失败要记进 stitch_failures、最终把任务标 failed 或
    带警告完成,而用户主动暂停/删除不是故障,调用方见到它应当和其它停止检查
    一样直接收尾返回。
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


def classify_download_error(exc: BaseException) -> TileOutcome:
    """下载异常 → TileOutcome。**分类必须与重试策略同口径。**

    重试策略在 `download_tile` 的 except 里（本文件 :661 附近）：4xx 里除了
    429 之外一律**不重试**直接抛出 —— 404 的瓦片重试多少次都不存在，指数退避
    只会把必然的失败拖成分钟级。那道短路早就在那儿了；这个函数做的是给它
    一个**名字**，而不是在旁边再立一套判据。两处一旦分叉就会出现最难解释的
    组合：引擎重试了 5 次的错误被记成 `permanent_failure`（用户看到「永久失败」
    却发现日志里试了 5 次），或者反过来 404 被记成 `retryable_failure`，让补漏
    功能一遍遍去问一个上游明确说过没有的坐标。

    分档理由：

    - 404 / 410 → `no_data`。上游**明确回答**了「这里没有」。这是唯一
      `is_explained` 为真的缺块：任务可以带着它判 `completed_with_gaps`，
      产物可用且永久带标记（§13-3）。海面、境外未覆盖区的瓦片就长这样。
    - 其余 4xx（429 除外）→ `permanent_failure`。403 鉴权、400 参数错、
      451 法律封锁：重试不会变，但它也**不是**「那里没有数据」，产物有洞
      的原因在我们这边，不能算解释清楚。
    - `NotAnImageResponse` → `permanent_failure`。HTTP 200 但响应体不是图片
      （劫持页 / 自建服务返回 JSON）。重试同一个 URL 只会拿到同一坨字节。
    - 其余（含 429、5xx、超时、连接重置、`DownloadCancelled` 之外的一切）
      → `retryable_failure`。这是**兜底方向**：宁可让用户多点一次补漏，也不要
      把一个其实能救回来的瓦片钉成永久失败（同 `outcome_from_db` 对未知值的
      取向）。

    缓存写失败**不走这里**：那不是下载错误，调用点直接给 `CACHE_FAILURE`。
    """
    if isinstance(exc, NotAnImageResponse):
        return TileOutcome.PERMANENT_FAILURE
    if isinstance(exc, aiohttp.ClientResponseError):
        status = exc.status
        if status in (404, 410):
            return TileOutcome.NO_DATA
        if 400 <= status < 500 and status != 429:
            return TileOutcome.PERMANENT_FAILURE
    return TileOutcome.RETRYABLE_FAILURE


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
        from src.services.tile_url_probe import parse_server_list
        now = time.monotonic()
        if self._servers_cache is None or now - self._servers_loaded_at > 60:
            raw = self.config_manager.get('tile_servers', '') or ''
            self._servers_cache = parse_server_list(raw)
            self._servers_loaded_at = now
        return self._servers_cache

    def lat_lon_to_tile(self, lat: float, lon: float, zoom: int) -> Tuple[int, int]:
        """经纬度 → 瓦片 (x, y)。**实现在 `src.contracts.region_tiles`。**

        方法保留是因为它是既有调用形态(`self.lat_lon_to_tile`,以及
        tests/test_download_engine.py 直接调它);但公式、纬度钳位、层级校验
        与 ValueError 文案全部只有合同里那一份。以前这里、dem 侧和前端
        map.js 各有一份 Web Mercator,三处各自演化的结果就是「预估的块数」
        与「实下的块数」对不上 —— 而那种偏差没有任何一处日志能解释。

        Raises:
            ValueError: 层级越界。文案与合同一致(路由层把它当 400 body)。
        """
        return _lat_lon_to_tile(lat, lon, zoom)

    @staticmethod
    def _validate_bbox(north: float, south: float, east: float, west: float) -> None:
        """四至校验。**每一条 ValueError 的文案都是 API 契约**。

        路由层把 `str(e)` 原样当 400 的 body 返回,tests 按文本断言 ——
        改一个字就是改 API。这些检查留在本模块(而不是搬进 RegionSpec)是因为
        RegionSpec 的校验文案是另一套(它服务导入路径),两边不可能同时满足;
        这里先按历史文案挡掉非法值,通过之后再交给合同构造。
        """
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

    def _bbox_region(self, north: float, south: float, east: float, west: float,
                     zoom_min: int, zoom_max: int) -> RegionSpec:
        """四至 + 层级 → 校验通过的 `RegionSpec`(矩形)。

        `count_tiles` / `iter_tiles` 都经过它,所以「按 bbox 下」与「按多边形下」
        走的是**同一条枚举路径**,只是几何不同。矩形在 `iter_region_tile_spans`
        里有快路径(直接用 `bbox_tile_range`),集合与改造前逐位一致。
        """
        self._validate_bbox(north, south, east, west)
        validate_zoom_range(zoom_min, zoom_max)
        return RegionSpec.from_bbox(north, south, east, west, source='manual')

    def _tile_ranges(
        self,
        north: float,
        south: float,
        east: float,
        west: float,
        zoom_min: int,
        zoom_max: int
    ):
        """校验四至/层级,逐层产出瓦片下标区间 `(zoom, x_min, x_max, y_min, y_max)`。

        单层区间来自 `region_tiles.bbox_tile_range`(含「角点算完再纠正次序」
        那一步 —— 南半球的 y 会反过来)。本方法只剩「参数校验 + 逐层循环」,
        瓦片数学一行都不在这里。

        ⚠️ 它**不认跨反经线**:west>east 时 bbox_tile_range 会把区间纠正成
        「除目标条带外的整个世界」。真正的枚举路径(iter_tiles → _bbox_region
        → RegionSpec)把这种写法归一成 east>180 再按段枚举,所以那条路是对的;
        本方法保留只为「按层级看下标范围」这类诊断/估算调用,不参与下载。

        Raises:
            ValueError: 参数非法(文案见 _validate_bbox / validate_zoom_range)。
        """
        self._validate_bbox(north, south, east, west)
        zoom_min, zoom_max = validate_zoom_range(zoom_min, zoom_max)

        for zoom in range(zoom_min, zoom_max + 1):
            x_min, x_max, y_min, y_max = bbox_tile_range(north, south, east, west, zoom)
            yield zoom, x_min, x_max, y_min, y_max

    @staticmethod
    def _warn_if_large(tile_count: int) -> None:
        """大任务软告警。0.1.4 起硬上限改成软阈值,是否继续由用户在前端确认。"""
        if tile_count > WARN_TILES_THRESHOLD:
            logger.warning(
                f"Large tile count detected: {tile_count} tiles. "
                f"This may take a long time to download and process. "
                f"Estimated time: {tile_count / 10 / 3600:.1f} hours at 10 tiles/sec."
            )

    def count_region_tiles_for(self, region, zoom_min: int, zoom_max: int) -> int:
        """区域(任意 `RegionSpec`)在 [zoom_min, zoom_max] 上的瓦片总数。

        与 `iter_region_tiles` 同源(都走 `iter_region_tile_spans`),所以
        「建任务时算出来的数」与「跑起来真正下的数」永远相等。多边形任务
        因此不再按外接矩形计费 —— 那正是 GeoD「按 bbox 计费、按多边形出图」
        那道裂缝:用户看到的预估是实际的好几倍,而没有任何地方解释差在哪。
        """
        total = count_region_tiles(region, zoom_min, zoom_max)
        self._warn_if_large(total)
        return total

    def count_tiles(
        self,
        north: float,
        south: float,
        east: float,
        west: float,
        zoom_min: int,
        zoom_max: int
    ) -> int:
        """矩形区域的瓦片总数(不物化)。

        签名保持不变(既有调用方与 tests 钉死),内部构造一个矩形 RegionSpec
        再走 `count_region_tiles_for` —— 全项目只有一条枚举路径。

        Raises:
            ValueError: 参数非法。
        """
        region = self._bbox_region(north, south, east, west, zoom_min, zoom_max)
        return self.count_region_tiles_for(region, zoom_min, zoom_max)

    def iter_region_tiles(self, region, zoom_min: int, zoom_max: int,
                          task_id: int = 0):
        """区域(任意 `RegionSpec`)→ 惰性产出 `Tile`,层级升序、行升序、列升序。

        **全项目唯一的瓦片枚举出口。** 洞会被真正挖掉(奇偶扫描线在
        `iter_region_tile_spans` 里),跨反经线按段产出且不重复 —— 这两件事
        是 GeoD 只取外环 / 外接矩形那两个 bug 的正面解。

        顺序契约:`(zoom, y, x)` 升序。改造前是 `(zoom, x, y)` —— 换成行优先
        是因为扫描线天然按行产出,为了维持列优先就得把一层全物化再转置,而
        「一层」在高 zoom 上就是几十万个 Tile。顺序**仍然是确定性的**,这才是
        恢复逻辑真正依赖的性质:`_iter_pending_tiles` 的归并只要求两趟枚举
        产出同一序列,不要求是哪一种序。
        """
        zoom_min, zoom_max = validate_zoom_range(zoom_min, zoom_max)
        for zoom in range(zoom_min, zoom_max + 1):
            for y, x_start, x_end in iter_region_tile_spans(region, zoom):
                for x in range(x_start, x_end + 1):
                    yield Tile(
                        task_id=task_id,
                        zoom=zoom,
                        x=x,
                        y=y,
                        status="pending",
                        retry_count=0
                    )

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
        """矩形区域 → 惰性产出 `Tile`。签名不变,内部走 `iter_region_tiles`。

        瓦片集合是 (区域, zoom) 的纯函数,可以随时按同一确定性顺序重建。
        恢复任务靠它枚举待下载集合(配合磁盘 cache 判断完成态),这是
        task_tiles 不再存全量行的前提。

        Raises:
            ValueError: 参数非法。**必须在第一次 next() 时抛** —— 这是生成器,
                不消费就不校验,`calculate_tiles` 因此先过一遍 `count_tiles`
                来保持历史上的急切校验语义。
        """
        region = self._bbox_region(north, south, east, west, zoom_min, zoom_max)
        yield from self.iter_region_tiles(region, zoom_min, zoom_max, task_id=task_id)

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
        # count_tiles 先完整消费一遍枚举:参数非法时在这里就抛 ValueError
        # (保持历史上的急切校验语义),并输出大任务警告。
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
        server_index: int = 0,
        source=None
    ) -> str:
        """瓦片 URL。**带快照与不带快照是两套截然不同的取数口径,这是重点。**

        source 为 `SourceSnapshot` 时:服务器列表与 URL 模板全部取自快照,
        一次配置都不读。这正是快照存在的理由 —— 任务跑到一半用户在设置页
        换了 tile_servers,已经在跑的这个任务必须继续用建任务那一刻的源,
        否则同一个成品里会混进两个来源的瓦片而**没有任何提示**(改造前就是
        这样:URL 是请求时现展开的)。

        source 为 None 时:退回历史行为 —— 读 `_tile_servers()`(60s TTL 缓存,
        见 `__init__`),于是「改了配置,新任务生效」。这条路给还没接快照的
        调用方(测速、探测、直接调 download_tile 的测试)留着,那些场景本来
        就该看**当前**配置。TTL 缓存只服务这条路;带快照的路径连缓存都不碰。

        Args:
            server_index: 轮换下标,对列表长度取模。
            source: `SourceSnapshot` 或 None。

        条目形态见 src.services.tile_url_probe.expand_server_entry：
        别名/主机按 Google vt 格式拼 lyrs={style}；完整 XYZ 模板按占位符
        展开（模板含 {style} 时替换，不含则样式由地址自身决定）。
        """
        from src.services.tile_url_probe import expand_server_entry
        if isinstance(source, SourceSnapshot):
            servers = list(source.server_list)
            style_code = source.style
            # server_list 为空的快照(自定义单地址源)直接用模板本身,
            # 不回落到配置 —— 回落等于让快照失去意义。
            template = (expand_server_entry(servers[server_index % len(servers)], style_code)
                        if servers else source.url_template)
            if source.subdomains:
                subs = source.subdomains
                template = template.replace('{s}', subs[server_index % len(subs)])
        else:
            servers = self._tile_servers()
            entry = servers[server_index % len(servers)]
            template = expand_server_entry(entry, style)
        return (template
                .replace('{z}', str(z))
                .replace('{x}', str(x))
                .replace('{y}', str(y)))

    def _get_cache_path(self, tile: Tile, style_or_source) -> Path:
        """瓦片的共享缓存路径。**规则只此一份**,在 `Tile.cache_path` 里。

        Args:
            style_or_source: `SourceSnapshot`(新路径)**或**单字符 style 码
                (存量路径)。这里原样透传,不做分支 —— `Tile.cache_path` 已经
                两种都收,在这里再判一次就是第二处路径规则。

        路径形态：
            带快照     cache/{style}-{fingerprint}/{zoom}/{x}/{y}.png
            带 style 码 cache/{style}/{zoom}/{x}/{y}.png（存量形态）

        cache 跨任务共享,不带 task_id。
        """
        return tile.cache_path(style_or_source)

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
        stop_flag: Optional[threading.Event] = None,
        *,
        source=None
    ) -> bytes:
        """
        Download a single tile with retry logic and server rotation

        Args:
            tile: Tile object to download
            style: Map style code
            session: aiohttp ClientSession for making requests
            proxy_url: Proxy URL ('' means no proxy); 由调用方从配置读出传入
            source: `SourceSnapshot` 或 None。给了就用它定 URL 与轮换列表,
                任务全程钉死在建任务那一刻的源(见 get_tile_url 的对比说明)。
                **关键字参数且默认 None**:tests/ 里多处把本方法换成不带
                source 的替身,调用方只在真的有快照时才传(见
                _download_single_tile 里的 source_kw)。

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
                # 有快照时轮换的是**快照里的**列表(它是身份的一部分,已经
                # 参与指纹);没有快照才读配置的 60s 缓存列表。
                if isinstance(source, SourceSnapshot) and source.server_list:
                    servers = list(source.server_list)
                else:
                    servers = self._tile_servers()
                server_index = (tile.x + tile.y + attempt) % len(servers)
                url = self.get_tile_url(tile.x, tile.y, tile.zoom, style,
                                        server_index, source=source)

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
        stop_flag: Optional[threading.Event] = None,
        *,
        source=None
    ) -> Dict[str, Any]:
        """下载一块瓦片:先查缓存,再走网络,最后上报结局。

        Args:
            tile: Tile object to download
            style: Map style code
            session: aiohttp ClientSession
            cache_enabled: Whether to check/use cache
            progress_callback: Optional async callback
                function(tile, status, error, size_bytes)。**四个位置参数,
                签名不变** —— tests/ 里的替身按位置接。size_bytes 只在
                「这块瓦片真的走了网络」时是字节数，缓存命中与失败一律 None
                —— 调用方拿它算下载速度，把读盘字节算进去会让网速虚高一个
                数量级（见 src/services/download_speed.py）。
            source: `SourceSnapshot` 或 None。给了就用它定缓存命名空间与
                URL;None 时按 style 码走存量路径。

        Returns:
            `{'tile', 'status', 'size'|'error'}`。

            **`status` 现在是 `TileOutcome` 的值字符串**,不再是 completed/
            failed 两档。理由:`failed` 一个词把「上游说这里没有数据」(海面、
            境外未覆盖)和「我们的盘写不进去」压成了同一件事,于是任务只能
            二选一 —— 要么把一片必然缺块的海域永远判失败、用户点一百次重试
            也不会变,要么把真实故障洗成成功。分成五档之后,completion 判定
            才有可能区分「已解释的缺块」与「没交代的缺块」(§13-3)。

            取值映射:
              缓存命中     → success
              网络成功     → success
              缓存写失败   → cache_failure
              下载异常     → classify_download_error(exc)
              用户取消     → 'cancelled'(**不是** TileOutcome,见下面的出口)
        """
        # 缓存键:有快照就用快照(指纹命名空间),否则退回 style 码(存量单级目录)。
        cache_key = source if source is not None else style
        # tests/ 里多处把 download_tile 换成不带 source 参数的替身,无条件多传
        # 一个 kwarg 会让它们全部 TypeError(同 __init__ 里 _collect_batch_results
        # 的说明)。只在真的有快照时才传 —— 那些替身本来就不在带快照的路径上。
        source_kw = {'source': source} if source is not None else {}
        try:
            # Check cache first if enabled
            if cache_enabled:
                try:
                    cache_path = self._get_cache_path(tile, cache_key)
                    if cache_path.exists():
                        # Validate cached file (check size > 0)
                        file_size = await asyncio.to_thread(lambda: cache_path.stat().st_size)

                        if file_size > 0:
                            logger.debug(f"Tile {tile.zoom}/{tile.x}/{tile.y} found in cache ({file_size} bytes)")

                            # 缓存命中:字节数传 None。这块瓦片来自磁盘,不是
                            # 网络。task_manager 虽然在下载前就把缓存命中剔出了
                            # 待下清单,这条分支仍然可达 —— 两个 bbox 重叠的任务
                            # 并发时,枚举之后、下载之前会有瓦片被另一个任务写进
                            # 缓存。
                            if progress_callback:
                                await progress_callback(
                                    tile, TileOutcome.SUCCESS.value, None, None)

                            return {
                                'tile': tile,
                                'status': TileOutcome.SUCCESS.value,
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
            data = await self.download_tile(tile, style, session, proxy_url=proxy_url,
                                            stop_flag=stop_flag, **source_kw)

            if cache_enabled:
                try:
                    cache_path = self._get_cache_path(tile, cache_key)
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
                    # 从 cache 取。写盘失败却仍上报成功的话,这块瓦片会:
                    # 磁盘上不存在任何文件、task_tiles 里没有缺块行、
                    # tasks.downloaded_tiles 却 +1;而完成判定只数缺块行,
                    # 任务照标 completed,completed 任务又不允许重启 —— 用户既
                    # 看不到异常、也无法原地续传自愈(tiles_only 全程无声)。
                    #
                    # 结局是 `cache_failure` 而**不是** `retryable_failure`:
                    # 网络那一趟已经成功了,字节拿到了,失手的是本机磁盘。
                    # 分开记的价值在补漏时兑现 —— 两者都在 RETRYABLE_OUTCOMES
                    # 里(值得重试),但用户看到「缓存写失败 × 3200」会去查磁盘
                    # 和权限,看到「网络失败 × 3200」会去查代理,那是两条完全
                    # 不同的排查路径。压成一个词就等于把这条线索抹掉。
                    error_msg = (
                        f"cache write failed: "
                        f"{type(cache_write_error).__name__}: {cache_write_error}"
                    )
                    logger.error(
                        f"Failed to write tile {tile.zoom}/{tile.x}/{tile.y} to cache: "
                        f"{cache_write_error}. Recording the tile as cache_failure so the "
                        f"task does not silently report success with a missing tile."
                    )
                    if progress_callback:
                        await progress_callback(
                            tile, TileOutcome.CACHE_FAILURE.value, error_msg, None)
                    return {
                        'tile': tile,
                        'status': TileOutcome.CACHE_FAILURE.value,
                        'error': error_msg,
                    }

            # Report success —— 唯一真正产生网络字节的出口。
            if progress_callback:
                await progress_callback(tile, TileOutcome.SUCCESS.value, None, len(data))

            return {
                'tile': tile,
                'status': TileOutcome.SUCCESS.value,
                'size': len(data)
            }

        except DownloadCancelled:
            # Task was cancelled mid-download: don't record this as a failure and
            # don't fire the progress callback — the task is being torn down.
            #
            # 'cancelled' **刻意不是一个 TileOutcome**:被取消的瓦片没有结局,
            # 它根本没被尝试完 —— 上游没说过话,盘没写过,什么都没发生。把它塞
            # 进 TileOutcome 就等于承认「取消」是一种缺块原因,于是暂停一次任务
            # 就会在 task_tiles 里留下一堆需要用户决策的假缺块。它只是「本次
            # 运行没轮到」,恢复时按普通待下瓦片重新枚举即可。
            logger.info(f"Tile {tile.zoom}/{tile.x}/{tile.y} download cancelled")
            return {
                'tile': tile,
                'status': 'cancelled'
            }

        except Exception as e:
            outcome = classify_download_error(e)
            error_msg = f"{type(e).__name__}: {e!r}"
            # no_data 不是故障:上游明确回答了「这里没有」。按 error 记会让
            # 一片正常的海域在日志里刷出几千条红色 —— 而真正的故障就淹在里面。
            if outcome is TileOutcome.NO_DATA:
                logger.debug(
                    f"Tile {tile.zoom}/{tile.x}/{tile.y} has no data upstream: {error_msg}")
            else:
                logger.error(
                    f"Failed to download tile {tile.zoom}/{tile.x}/{tile.y} "
                    f"({outcome.value}): {error_msg}")

            # Report failure
            if progress_callback:
                await progress_callback(tile, outcome.value, error_msg, None)

            return {
                'tile': tile,
                'status': outcome.value,
                'error': error_msg
            }

    async def download_tiles_batch(
        self,
        tiles,
        style: str,
        progress_callback=None,
        stop_flag: Optional[threading.Event] = None,
        *,
        source=None,
        max_concurrency: Optional[int] = None,
        disk_recheck=None,
    ) -> List[Dict[str, Any]]:
        """
        Download multiple tiles concurrently with semaphore control

        Args:
            tiles: Tiles to download — any iterable (list or generator);
                it is consumed lazily in batches of DOWNLOAD_BATCH_SIZE
            style: Map style code。source 为 None 时它同时决定缓存目录与 URL;
                有 source 时它只作为 URL 展开的样式参数,缓存目录由快照定。
            progress_callback: Optional async callback
                function(tile, status, error, size_bytes)。status 是
                `TileOutcome` 的值字符串,或 `'cancelled'`。
            source: `SourceSnapshot` 或 None(**关键字参数**)。给了就整批钉死
                在这一个源上:缓存命名空间走指纹目录,URL 从快照的模板与服务器
                列表展开,全程不读配置 —— 跑到一半有人改设置也不会换源。
            max_concurrency: 全局调度器授予的连接数上界(**关键字参数**)。
                给了就与配置的 concurrent_downloads 取小值。这是
                `ResourceScheduler` 的配额真正落到信号量与 TCPConnector 的
                唯一通道:改造前每个任务各自开满 concurrent_downloads 条连接,
                四个任务并行就是四倍,没有任何全局上界。
            disk_recheck: `disk_budget.RunningRecheck` 或 None(**关键字参数**)。
                运行中的磁盘复查,**每批** poll 一次(见下面 while 循环顶部)。
                纯观测:判决只经 on_verdict 进任务日志,不通过也不叫停 ——
                拦截语义 2026-08 起移除(见 disk_budget 模块 docstring)。

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
        if max_concurrency is not None:
            # 取小值而不是直接覆盖:配额是**上界**,用户把 concurrent_downloads
            # 调到 5 时不该因为调度器给了 12 就替他开 12 条。
            granted = max(1, int(max_concurrency))
            if granted < concurrent_downloads:
                logger.info(
                    f"Concurrency capped by the global scheduler: "
                    f"{concurrent_downloads} -> {granted}")
            concurrent_downloads = min(concurrent_downloads, granted)
        request_timeout = int(self.config_manager.get('request_timeout', '30'))
        cache_enabled = (self.config_manager.get('cache_enabled', 'true') or 'true').lower() == 'true'
        # 生效代理：手动 proxy_url > 自动探测到的可用代理 > 直连（见
        # services/proxy_autodetect）。to_thread 是必须的 —— 解析在后台探测
        # 尚未完成时会阻塞等待，就地调用会把整个事件循环连同并发下载一起冻住。
        proxy_url = await asyncio.to_thread(resolve_from_config, self.config_manager)
        # max_retries/request_timeout 在入口读一次,整批复用 —— 逐瓦片读就是
        # 每块瓦片两次新开 SQLite 连接(见 download_tile 里的回退逻辑)。
        self._batch_retry_config = (
            max(0, int(self.config_manager.get('max_retries', '3'))),
            request_timeout,
        )

        # 只在真的有快照时才把 source 传下去 —— tests/ 里多处把
        # _download_single_tile 换成不带 source 的四/七参替身。
        source_kw = {'source': source} if source is not None else {}
        if source is not None:
            logger.info(f"Batch download pinned to source: {source.summary()}")
        tile_iterator = iter(tiles)

        logger.info(
            f"Starting batch download: concurrency={concurrent_downloads}, "
            f"timeout={request_timeout}s, batch_size={DOWNLOAD_BATCH_SIZE}"
        )

        # Create semaphore for concurrency control
        semaphore = asyncio.Semaphore(concurrent_downloads)

        def _stop_requested() -> bool:
            """要不要收手:只看用户的停止标记。"""
            return stop_flag is not None and stop_flag.is_set()

        # Create aiohttp session with connection pooling。
        # limit_per_host 必须跟着并发走:旧版恒为 4(服务器数),4 台服务器
        # 最多 16 条连接,concurrent_downloads 调到 20+ 也被悄悄压死(实测
        # 吞吐差一倍以上);瓦片按 (x+y) 坐标天然轮换服务器(见 download_tile),
        # per-host 不需要再承担均衡职责,与 dem_download_engine 口径一致。
        connector = aiohttp.TCPConnector(limit=concurrent_downloads, limit_per_host=concurrent_downloads)

        results: List[Dict[str, Any]] = []

        # 显式 proxy= 才是主路径(见上面 proxy_url 的解析,以及 download_tile 里的
        # 逐 URL bypass 判断)。trust_env=True 留作兜底:自动探测的候选全部验证
        # 失败时 proxy_url 为空,此时仍让 aiohttp 读 HTTP(S)_PROXY —— 那是
        # apply_system_proxy() 从 Windows 注册表/macOS scutil 灌进来的系统代理,
        # 我们验不通不代表它对别的目标主机也不通,不该主动把它掐掉。
        async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:

            async def download_with_semaphore(tile: Tile):
                """Download a single tile with semaphore control"""
                async with semaphore:
                    # Cooperative cancellation: a tile still queued behind the
                    # semaphore when the task is cancelled is skipped here instead
                    # of running its full timeout x retry budget to completion.
                    if _stop_requested():
                        # 'cancelled' 不是 TileOutcome —— 这块瓦片一次都没被
                        # 尝试过,它没有结局(理由见 _download_single_tile 的
                        # 同名出口)。
                        return {'tile': tile, 'status': 'cancelled'}
                    return await self._download_single_tile(
                        tile=tile,
                        style=style,
                        session=session,
                        cache_enabled=cache_enabled,
                        progress_callback=progress_callback,
                        proxy_url=proxy_url,
                        stop_flag=stop_flag,
                        **source_kw
                    )

            # 分批创建协程(见 DOWNLOAD_BATCH_SIZE):不在批次间因 stop_flag
            # 提前退出 —— 每块瓦片都要产出一条结果(queued 的报 'cancelled'),
            # 保持「结果数 == 输入瓦片数」的既有语义。
            # _collect_batch_results=False 时(task_manager 的调用路径,结果
            # 由 progress_callback 逐块消费)逐批丢弃 gather 返回值,不物化
            # 全量 results;result_count 仅为日志计数。
            result_count = 0
            while True:
                # 每批一次磁盘复查(复查自己还有时间节流,见 RunningRecheck)。
                # **批**是这条管线唯一天然的节奏:逐瓦片查是纯浪费(一批 1000 张),
                # 逐 zoom 查太粗(一个 zoom 可以是几十万张、几十 GB)。
                # 纯观测:判决经 on_verdict 进任务日志,不通过也不叫停。
                if disk_recheck is not None:
                    disk_recheck.poll()
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
        extra_allowed_dir: str = None,
        stop_flag: Optional[threading.Event] = None,
        *,
        work_dir_base=None
    ) -> str:
        """
        Stitch tiles into a single georeferenced image using GDAL

        Args:
            tiles: List of Tile objects to stitch
            style: Map style code **或** `SourceSnapshot`。只用于定位每块瓦片的
                缓存文件(`_get_cache_path` 两种都收),不参与 URL。
            output_path: Path for output file (GeoTIFF or PNG)
            zoom_level: Zoom level of the tiles
            target_epsg: CRS of the output file. Defaults to 4326, which is what
                this tool has always produced. Tiles are mosaicked in their
                native EPSG:3857 (see tile_geotransform) and then reprojected
                once at the end. Pass 3857 to skip the reprojection and get the
                resample-free native mosaic.
            extra_allowed_dir: Extra directory the output path may live under
                (the calling task's registered output root).
            stop_flag: 协作停止标志。整段拼接是单次同步调用,唯一天然的逐块
                循环是「每块瓦片配准一次」(见下面的 _georef_one)——检查点就
                挂在它以及三个 GDAL 阶段(BuildVRT / Warp / Translate)之前。
                置位时抛 StitchCancelled。
                ⚠️ 仍然拦不住的:最后那次 gdal.Translate 是**单个不可中断的
                调用**,大 zoom 一层可以跑十分钟级。检查点把「暂停到真正停下」
                的窗口从「整段拼接」缩到「当前这一次 Translate」,不能缩到零。
            work_dir_base: 中间产物工作目录的父目录（**关键字参数**）。给了就
                用它,而不是 `stitch_tmpdir` 配置。这是 `disk_budget.work_dir_for`
                的落点:它挑的是**与输出同卷**的目录。GeoD #32 就是这件事 ——
                中间件缓冲进系统 TEMP、拼完再整体搬到输出盘,等于把每一个字节
                在两块盘之间搬一遍;同卷时 os.replace 是元数据操作,零拷贝。
                单层 mosaic 的中间件是 GB 级,这不是微优化。

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

        # 进程级钉死非异常模式:本函数的判错(`is None` + 下面那道
        # GetLastErrorType 闸门)以此为前提。见 src/core/gdal_mode.py。
        pin_gdal_exception_mode()

        def _abort_if_stopped(stage: str) -> None:
            """协作停止检查点。见签名里 stop_flag 的说明。"""
            if stop_flag is not None and stop_flag.is_set():
                raise StitchCancelled(
                    f"Stitching zoom {zoom_level} cancelled before {stage}"
                )

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
        # 中间产物默认落系统临时盘(可能是小容量系统盘,大 zoom 一层可达 GB 级)。
        # 三级优先:
        #   ① work_dir_base 参数 —— 调用方(task_manager)经 disk_budget.work_dir_for
        #      挑好的「与输出同卷」目录。它比配置优先,因为它是**按本次任务的
        #      实际输出路径**算出来的,而 stitch_tmpdir 是一个全局设置,不可能
        #      对每个任务都同卷(GeoD #32 讲的正是跨卷搬运)。
        #   ② stitch_tmpdir 配置键(形制同 contour_warp_tmpdir,见 contour_engine);
        #   ③ 系统默认。
        # 读取失败回退系统默认,不让配置库故障拖垮拼接。
        stitch_tmp_base = None
        if work_dir_base:
            stitch_tmp_base = str(work_dir_base)
        else:
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
                _abort_if_stopped('legacy georef cleanup')
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
            # (min(4, cpu_count),同 contour_engine / cesium_terrain)。
            # map 保序,georef_paths 顺序与串行一致;任一瓦片失败时 with 退出
            # 会等所有 worker 收尾,再由 finally 清掉整个 work_dir。
            def _georef_one(tile: Tile) -> str:
                # 逐瓦片检查点 —— 整段拼接里唯一天然的 N 次循环就在这里。
                # ThreadPoolExecutor.map 一次性把全部瓦片排进队列,标志置位后
                # 剩余任务各自在这一行立刻抛出,队列毫秒级排空。
                _abort_if_stopped(f"georeferencing tile {tile.zoom}/{tile.x}/{tile.y}")

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
            _abort_if_stopped('BuildVRT')

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
            _abort_if_stopped('Translate')

            # Determine output format based on file extension
            output_ext = Path(output_path).suffix.lower()
            if output_ext == '.tif' or output_ext == '.tiff':
                output_format = 'GTiff'
                translate_options = gdal.TranslateOptions(
                    format=output_format,
                    # BIGTIFF=IF_SAFER 不能省:GTiff 默认的 IF_NEEDED 只在**不压缩**时
                    # 按未压缩体积决定是否升级 BigTIFF,一旦带了 COMPRESS 就一律按经典
                    # TIFF(4 GiB 上限)建文件,超出部分静默丢弃。实测 68000x68000 Byte
                    # +DEFLATE:产物停在 4294967275 字节、version=42,而 gdal.Translate
                    # 返回**非 None** 的 dataset、RasterXSize/YSize 报 68000x68000 全对、
                    # 左上角与源逐字节相等 —— 只有右下角全 0。也就是任务 completed、
                    # 无 warning、文件打得开、尺寸看着对,下半张却是空白。
                    # 地形侧 cesium_terrain.build_input_raster 早就写了它,这里对齐。
                    creationOptions=[f'COMPRESS={gdal_compression}', 'TILED=YES',
                                     'BIGTIFF=IF_SAFER'],
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
                    # BIGTIFF=IF_SAFER 不能省:GTiff 默认的 IF_NEEDED 只在**不压缩**时
                    # 按未压缩体积决定是否升级 BigTIFF,一旦带了 COMPRESS 就一律按经典
                    # TIFF(4 GiB 上限)建文件,超出部分静默丢弃。实测 68000x68000 Byte
                    # +DEFLATE:产物停在 4294967275 字节、version=42,而 gdal.Translate
                    # 返回**非 None** 的 dataset、RasterXSize/YSize 报 68000x68000 全对、
                    # 左上角与源逐字节相等 —— 只有右下角全 0。也就是任务 completed、
                    # 无 warning、文件打得开、尺寸看着对,下半张却是空白。
                    # 地形侧 cesium_terrain.build_input_raster 早就写了它,这里对齐。
                    creationOptions=[f'COMPRESS={gdal_compression}', 'TILED=YES',
                                     'BIGTIFF=IF_SAFER'],
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
                # 显式钉死「非异常」模式,和 cesium_terrain.build_input_raster 对齐。
                # gdal.UseExceptions() 是**进程全局**的,contour_engine 无条件调它,
                # 四条流水线又共用一个 Flask 进程 —— 用户先跑一个等高线任务再跑地图
                # 任务时,下面那道 GetLastErrorType 检查就会永远读到 0(异常模式下
                # CE_Failure 直接抛成 Python 异常、不回填 CPL 错误栈),白装一道闸门。
                with gdal.ExceptionMgr(useExceptions=False):
                    gdal.ErrorReset()
                    output_ds = gdal.Translate(str(part_path_obj), translate_source, options=translate_options)
                    if output_ds is None:
                        raise RuntimeError(f"Failed to translate VRT to {output_path_obj}")
                    output_ds = None  # Close output dataset before replacing
                    # `output_ds is None` 挡不住 I/O 写失败。磁盘满 / 配额 / 超 4 GiB 时,
                    # Translate 照样返回**非 None** 的 dataset,错误只登记在 CPL 错误栈里,
                    # 读取侧再查 GetLastErrorType() 已经是 0 —— 实测那次 4 GiB 截断,写入
                    # 期间栈里堆了 10073 条 `TIFFAppendToStrip:Maximum TIFF file size
                    # exceeded`,而这里一条都没捞,产物就这么 os.replace 成了正式文件。
                    # 与 cesium_terrain._raise_on_gdal_error 同形态。
                    # ⚠️ 已知未覆盖:写失败只发生在最后一次 flush 时,GDAL 连错误记录
                    # 都不留(GetLastErrorType()==0),这道闸门也拦不住。要堵死得校验
                    # 产物完整性(如回读右下角),尚未做。
                    if gdal.GetLastErrorType() >= gdal.CE_Failure:
                        raise RuntimeError(
                            f"gdal.Translate reported success but GDAL logged a failure "
                            f"for {output_path_obj}: {gdal.GetLastErrorMsg()!r} "
                            f"(GDAL error {gdal.GetLastErrorNo()})")
                    os.replace(str(part_path_obj), str(output_path_obj))
                    # PNG/JPEG 格式内部没有地理配准字段,GDAL 把 geotransform + 投影
                    # 写在**同名 .aux.xml 边车**里。主文件改名时边车必须跟着走 ——
                    # 否则下面的 finally 会把它当残件删掉,打开产物看到的是
                    # geotransform=(0,1,0,0,0,1)、投影为空的一张普通图片,而任务
                    # 报成功、文件也确实在。实测:边车跟着搬之后 gt 与 EPSG:3857 完整。
                    #
                    # ⚠️ **当前生产路径走不到这里**:唯一的生产调用点
                    # task_manager.py 把 output_path 硬编码成 `*_zoom_<z>.tif`,
                    # 而 GTiff 的地理信息在文件内部、不产生边车。UI 的「输出格式」
                    # 是「瓦片 / GeoTIFF」两个复选框(both / tiles_only / image_only),
                    # 产生不了 png/jpg。保留这段是因为 output_format 的枚举里仍收
                    # 'png'/'jpg' 两个历史值,且将来放开图片格式时这里必须是对的。
                    # 由 tests/test_fix_gdal_silent_failure_gaps.py 直调 *.png 钉住。
                    part_aux = part_path_obj.with_name(part_path_obj.name + '.aux.xml')
                    if part_aux.exists():
                        os.replace(
                            str(part_aux),
                            str(output_path_obj.with_name(output_path_obj.name + '.aux.xml')))
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
        pin_gdal_exception_mode()  # 下面靠 `is None` 判错,见 src/core/gdal_mode.py

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
