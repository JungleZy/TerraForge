"""
Download Engine Service

Handles tile coordinate calculation, URL generation, and download orchestration.
Implements Web Mercator projection for converting geographic coordinates to tile coordinates.
"""

import logging
import math
from typing import List, Tuple
from models.task import Tile
from services.config_manager import ConfigManager

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
        # Input validation
        if north <= south:
            raise ValueError(f"North latitude ({north}) must be greater than south latitude ({south})")

        if not MIN_ZOOM <= zoom_min <= MAX_ZOOM:
            raise ValueError(f"Minimum zoom level must be between {MIN_ZOOM} and {MAX_ZOOM}, got {zoom_min}")

        if not MIN_ZOOM <= zoom_max <= MAX_ZOOM:
            raise ValueError(f"Maximum zoom level must be between {MIN_ZOOM} and {MAX_ZOOM}, got {zoom_max}")

        if zoom_min > zoom_max:
            raise ValueError(f"Minimum zoom ({zoom_min}) must be less than or equal to maximum zoom ({zoom_max})")

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

        # Check if tile count exceeds maximum allowed
        if len(tiles) > MAX_TILES:
            raise ValueError(
                f"Tile count ({len(tiles)}) exceeds maximum allowed ({MAX_TILES}). "
                f"Please reduce the area size or zoom level range."
            )

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
