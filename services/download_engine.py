"""
Download Engine Service

Handles tile coordinate calculation, URL generation, and download orchestration.
Implements Web Mercator projection for converting geographic coordinates to tile coordinates.
"""

import logging
import math
import asyncio
import threading
import aiohttp
import aiofiles
import os
from typing import List, Tuple, Optional, Dict, Any
from pathlib import Path
from osgeo import gdal, osr
from models.task import Tile
from services.config_manager import ConfigManager
from config import Config

logger = logging.getLogger(__name__)

# Constants
WEB_MERCATOR_MAX_LAT = 85.0511  # Maximum valid latitude for Web Mercator projection
TILE_SERVER_COUNT = 4  # Number of Google Maps tile servers (mts0-mts3)
WARN_TILES_THRESHOLD = 100000  # Warn user if tile count exceeds this threshold
MIN_ZOOM = 0  # Minimum zoom level
MAX_ZOOM = 21  # Maximum zoom level

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


class DownloadCancelled(Exception):
    """Raised inside the download path when a task's stop flag is set.

    Lets _download_single_tile distinguish a user cancellation from a genuine
    download failure, so a cancelled tile is reported as 'cancelled' rather than
    'failed' and the retry loop / queued-tile backlog stops immediately.
    """


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
            ValueError: If input parameters are invalid or tile count exceeds MAX_TILES

        Note:
            Tiles are generated for each zoom level from zoom_min to zoom_max (inclusive).
            The number of tiles increases exponentially with zoom level.
            Maximum allowed tiles per task: 100,000
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

        # Calculate expected tile count BEFORE generating any tiles
        expected_tile_count = 0
        for zoom in range(zoom_min, zoom_max + 1):
            # Get tile coordinates for corners
            x_min, y_max = self.lat_lon_to_tile(south, west, zoom)
            x_max, y_min = self.lat_lon_to_tile(north, east, zoom)

            # Ensure proper ordering
            if x_min > x_max:
                x_min, x_max = x_max, x_min
            if y_min > y_max:
                y_min, y_max = y_max, y_min

            # Calculate tile count for this zoom level
            tiles_at_zoom = (x_max - x_min + 1) * (y_max - y_min + 1)
            expected_tile_count += tiles_at_zoom

        # Warn if tile count is very large
        if expected_tile_count > WARN_TILES_THRESHOLD:
            logger.warning(
                f"Large tile count detected: {expected_tile_count} tiles. "
                f"This may take a long time to download and process. "
                f"Estimated time: {expected_tile_count / 10 / 3600:.1f} hours at 10 tiles/sec."
            )

        # Now generate tiles
        tiles = []

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

            # Generate all tiles in the range
            for x in range(x_min, x_max + 1):
                for y in range(y_min, y_max + 1):
                    tile = Tile(
                        task_id=task_id,
                        zoom=zoom,
                        x=x,
                        y=y,
                        status="pending",
                        retry_count=0
                    )
                    tiles.append(tile)

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
        Generate Google Maps tile URL

        Args:
            x: Tile X coordinate
            y: Tile Y coordinate
            z: Zoom level
            style: Map style code (m=roadmap, s=satellite, y=hybrid, t=terrain)
            server_index: Server index (0-3 for mts0-mts3)

        Returns:
            Complete tile URL string

        Google Maps Style Codes:
            m: roadmap (default)
            s: satellite
            y: hybrid (satellite with labels)
            t: terrain
            p: terrain with labels
        """
        # Ensure server index is in valid range
        server_index = server_index % TILE_SERVER_COUNT

        # Build URL using Google Maps tile server format
        url = f"http://mts{server_index}.googleapis.com/vt?lyrs={style}&x={x}&y={y}&z={z}"

        return url

    def _get_cache_path(self, tile: Tile, style: str) -> Path:
        """
        Get cache file path for a tile

        Args:
            tile: Tile object containing coordinates
            style: Map style code

        Returns:
            Path object for cache file location

        Cache Path Format:
            cache/task_{task_id}/{style}/{zoom}/{x}/{y}.png
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
        proxy_url: Optional[str] = None,
        stop_flag: Optional[threading.Event] = None
    ) -> bytes:
        """
        Download a single tile with retry logic and server rotation

        Args:
            tile: Tile object to download
            style: Map style code
            session: aiohttp ClientSession for making requests

        Returns:
            Tile image data as bytes

        Raises:
            aiohttp.ClientError: If download fails after all retries
            asyncio.TimeoutError: If request times out after all retries

        Retry Strategy:
            - Max retries from config (max_retries)
            - Server rotation on each retry (mts0 -> mts1 -> mts2 -> mts3)
            - Exponential backoff: 2^attempt seconds between retries
            - Timeout from config (request_timeout)
        """
        # Get configuration values
        max_retries = int(self.config_manager.get('max_retries', '3'))
        request_timeout = int(self.config_manager.get('request_timeout', '30'))
        proxy_url = proxy_url if proxy_url is not None else (self.config_manager.get('proxy_url', '') or '')

        last_error = None

        for attempt in range(max_retries + 1):
            # Cooperative cancellation: bail out before (re)issuing a request.
            if stop_flag is not None and stop_flag.is_set():
                raise DownloadCancelled()
            try:
                # Rotate server index on each attempt
                server_index = attempt % TILE_SERVER_COUNT
                url = self.get_tile_url(tile.x, tile.y, tile.zoom, style, server_index)

                logger.debug(
                    f"Downloading tile {tile.zoom}/{tile.x}/{tile.y} "
                    f"from server mts{server_index} (attempt {attempt + 1}/{max_retries + 1})"
                )

                # Download with timeout
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=request_timeout),
                    proxy=proxy_url or None,
                ) as response:
                    response.raise_for_status()
                    data = await response.read()

                    logger.debug(
                        f"Successfully downloaded tile {tile.zoom}/{tile.x}/{tile.y} "
                        f"({len(data)} bytes)"
                    )

                    return data

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
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
                    logger.warning(
                        f"Failed to write tile {tile.zoom}/{tile.x}/{tile.y} to cache: {cache_write_error}. "
                        f"Download was successful but tile will not be cached."
                    )

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
        tiles: List[Tile],
        style: str,
        progress_callback=None,
        stop_flag: Optional[threading.Event] = None
    ) -> List[Dict[str, Any]]:
        """
        Download multiple tiles concurrently with semaphore control

        Args:
            tiles: List of Tile objects to download
            style: Map style code
            progress_callback: Optional async callback function(tile, status, error)

        Returns:
            List of download result dictionaries

        Concurrency Control:
            Uses semaphore to limit concurrent downloads based on
            concurrent_downloads config value
        """
        # Get configuration values
        concurrent_downloads = int(self.config_manager.get('concurrent_downloads', '10'))
        request_timeout = int(self.config_manager.get('request_timeout', '30'))
        cache_enabled = (self.config_manager.get('cache_enabled', 'true') or 'true').lower() == 'true'
        proxy_url = self.config_manager.get('proxy_url', '') or ''

        logger.info(
            f"Starting batch download: {len(tiles)} tiles, "
            f"concurrency={concurrent_downloads}, timeout={request_timeout}s"
        )

        # Create semaphore for concurrency control
        semaphore = asyncio.Semaphore(concurrent_downloads)

        # Create aiohttp session with connection pooling
        connector = aiohttp.TCPConnector(limit=concurrent_downloads, limit_per_host=TILE_SERVER_COUNT)

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

            # Create download tasks for all tiles
            tasks = [download_with_semaphore(tile) for tile in tiles]

            # Execute all downloads concurrently
            results = await asyncio.gather(*tasks, return_exceptions=False)

        logger.info(f"Batch download completed: {len(results)} results")

        return results

    def stitch_tiles_with_gdal(
        self,
        tiles: List[Tile],
        style: str,
        output_path: str,
        zoom_level: int,
        target_epsg: int = 4326
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
            2. Add georeference to each tile (creates _geo3857rgb.tif versions)
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

        # Validate and normalize output path
        output_path_obj = Path(output_path).resolve()

        # Validate output path is within allowed directories (cache or output directory)
        allowed_dirs = [Config.CACHE_DIR.resolve(), Config.OUTPUT_DIR.resolve()]
        if not any(output_path_obj.is_relative_to(allowed_dir) for allowed_dir in allowed_dirs):
            raise ValueError(
                f"Output path {output_path_obj} is not within allowed directories: "
                f"{', '.join(str(d) for d in allowed_dirs)}"
            )

        # Create output directory if it doesn't exist
        output_path_obj.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Ensured output directory exists: {output_path_obj.parent}")

        # Get configuration values
        gdal_compression = self.config_manager.get('gdal_compression', 'LZW')
        gdal_resampling = self.config_manager.get('gdal_resampling', 'nearest')

        # Filter tiles for the specified zoom level
        tiles_at_zoom = [t for t in tiles if t.zoom == zoom_level]
        if not tiles_at_zoom:
            raise ValueError(f"No tiles found at zoom level {zoom_level}")

        logger.info(f"Processing {len(tiles_at_zoom)} tiles at zoom level {zoom_level}")

        # Intermediates are cleaned in the finally block below. They must NOT be
        # cleaned only on the happy path: a single missing cache tile raises
        # inside the loop below, and every intermediate produced so far would
        # otherwise be stranded in the cross-task cache directory.
        georef_paths: List[str] = []
        vrt_path_obj = output_path_obj.with_suffix('.vrt')
        vrt_path = str(vrt_path_obj)
        warped_path_obj: Optional[Path] = None

        try:
            # Get cache paths and create georeferenced versions
            for tile in tiles_at_zoom:
                cache_path = self._get_cache_path(tile, style)
                if not cache_path.exists():
                    raise FileNotFoundError(f"Tile not found in cache: {cache_path}")

                # Add georeference to tile
                georef_path = self._add_georeference(str(cache_path), tile)
                georef_paths.append(georef_path)

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

            # Perform translation
            output_ds = gdal.Translate(str(output_path_obj), translate_source, options=translate_options)
            if output_ds is None:
                raise RuntimeError(f"Failed to translate VRT to {output_path_obj}")
            output_ds = None  # Close output dataset

            logger.info(f"Translation completed: {output_path_obj}")
        finally:
            # Clean up temporary files — on success *and* on failure.
            # Only files actually removed are logged/counted: on an early
            # failure most of these were never created, and reporting them as
            # "cleaned up" would send whoever debugs that failure down the
            # wrong path.
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

            # 2. Clean up temporary georeferenced tiles (_geo3857rgb.tif files)
            cleaned_count = 0
            for georef_path in georef_paths:
                try:
                    Path(georef_path).unlink()
                    cleaned_count += 1
                except FileNotFoundError:
                    pass  # already gone
                except Exception as e:
                    logger.warning(f"Failed to clean up georeferenced tile {georef_path}: {e}")

            logger.debug(f"Cleaned up {cleaned_count}/{len(georef_paths)} temporary georeferenced tiles")

        logger.info(f"GDAL tile stitching completed: {output_path_obj}")
        return str(output_path_obj)

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

    def _add_georeference(self, tile_path: str, tile: Tile) -> str:
        """
        Add georeference information to a tile image

        Args:
            tile_path: Path to the tile image file
            tile: Tile object with coordinates

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
        georef_path_obj = tile_path_obj.with_stem(
            f"{tile_path_obj.stem}{GEOREF_SUFFIX}"
        ).with_suffix('.tif')
        georef_path = str(georef_path_obj)

        # Opportunistically drop leftovers written by earlier releases so the
        # existing residue on users' disks drains away as tiles get re-stitched
        # instead of sitting there forever.
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

        # Create georeferenced output file
        driver = gdal.GetDriverByName('GTiff')
        dst_ds = driver.Create(
            georef_path,
            width,
            height,
            bands,
            src_ds.GetRasterBand(1).DataType
        )

        if dst_ds is None:
            src_ds = None
            raise RuntimeError(f"Failed to create georeferenced tile: {georef_path}")

        # Copy raster data
        for band_idx in range(1, bands + 1):
            src_band = src_ds.GetRasterBand(band_idx)
            dst_band = dst_ds.GetRasterBand(band_idx)
            data = src_band.ReadAsArray()
            dst_band.WriteArray(data)

        # Set geotransform
        dst_ds.SetGeoTransform(geotransform)

        # Set projection to Web Mercator (EPSG:3857) — see tile_geotransform
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg_code)
        dst_ds.SetProjection(srs.ExportToWkt())

        # Close datasets
        src_ds = None
        dst_ds = None

        logger.debug(f"Created georeferenced tile: {georef_path}")
        return georef_path
