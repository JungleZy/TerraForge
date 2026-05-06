"""
Download Engine Service

Handles tile coordinate calculation, URL generation, and download orchestration.
Implements Web Mercator projection for converting geographic coordinates to tile coordinates.
"""

import logging
import math
import asyncio
import aiohttp
import aiofiles
import os
from typing import List, Tuple, Optional, Dict, Any
from pathlib import Path
from models.task import Tile
from services.config_manager import ConfigManager
from config import Config

logger = logging.getLogger(__name__)

# Constants
WEB_MERCATOR_MAX_LAT = 85.0511  # Maximum valid latitude for Web Mercator projection
TILE_SERVER_COUNT = 4  # Number of Google Maps tile servers (mts0-mts3)
MAX_TILES = 100000  # Maximum number of tiles allowed per download task
MIN_ZOOM = 0  # Minimum zoom level
MAX_ZOOM = 21  # Maximum zoom level


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

        # Check if tile count exceeds maximum allowed BEFORE creating any Tile objects
        if expected_tile_count > MAX_TILES:
            raise ValueError(
                f"Tile count ({expected_tile_count}) exceeds maximum allowed ({MAX_TILES}). "
                f"Please reduce the area size or zoom level range."
            )

        # Now generate tiles (only if count check passed)
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
            cache/{style}/{zoom}/{x}/{y}.png
        """
        cache_path = Config.CACHE_DIR / style / str(tile.zoom) / str(tile.x) / f"{tile.y}.png"
        return cache_path

    async def download_tile(
        self,
        tile: Tile,
        style: str,
        session: aiohttp.ClientSession
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

        last_error = None

        for attempt in range(max_retries + 1):
            try:
                # Rotate server index on each attempt
                server_index = attempt % TILE_SERVER_COUNT
                url = self.get_tile_url(tile.x, tile.y, tile.zoom, style, server_index)

                logger.debug(
                    f"Downloading tile {tile.zoom}/{tile.x}/{tile.y} "
                    f"from server mts{server_index} (attempt {attempt + 1}/{max_retries + 1})"
                )

                # Download with timeout
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=request_timeout)) as response:
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
                    f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
                )

                # If not the last attempt, wait with exponential backoff
                if attempt < max_retries:
                    backoff_time = 2 ** attempt
                    logger.debug(f"Waiting {backoff_time}s before retry...")
                    await asyncio.sleep(backoff_time)

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
        progress_callback=None
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

            # Download tile
            data = await self.download_tile(tile, style, session)

            # Save to cache using async file I/O
            cache_path = self._get_cache_path(tile, style)
            await asyncio.to_thread(lambda: cache_path.parent.mkdir(parents=True, exist_ok=True))

            async with aiofiles.open(cache_path, 'wb') as f:
                await f.write(data)

            logger.debug(f"Saved tile {tile.zoom}/{tile.x}/{tile.y} to cache")

            # Report success
            if progress_callback:
                await progress_callback(tile, 'completed', None)

            return {
                'tile': tile,
                'status': 'completed',
                'size': len(data)
            }

        except Exception as e:
            error_msg = str(e)
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
        progress_callback=None
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

        logger.info(
            f"Starting batch download: {len(tiles)} tiles, "
            f"concurrency={concurrent_downloads}, timeout={request_timeout}s"
        )

        # Create semaphore for concurrency control
        semaphore = asyncio.Semaphore(concurrent_downloads)

        # Create aiohttp session with connection pooling
        connector = aiohttp.TCPConnector(limit=concurrent_downloads, limit_per_host=TILE_SERVER_COUNT)

        async with aiohttp.ClientSession(connector=connector) as session:

            async def download_with_semaphore(tile: Tile):
                """Download a single tile with semaphore control"""
                async with semaphore:
                    return await self._download_single_tile(
                        tile=tile,
                        style=style,
                        session=session,
                        cache_enabled=True,
                        progress_callback=progress_callback
                    )

            # Create download tasks for all tiles
            tasks = [download_with_semaphore(tile) for tile in tiles]

            # Execute all downloads concurrently
            results = await asyncio.gather(*tasks, return_exceptions=False)

        logger.info(f"Batch download completed: {len(results)} results")

        return results
