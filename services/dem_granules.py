"""
DEM granule naming utilities.

Currently supports ASTER GDEM V3 (ASTGTM.003) 1x1 degree tiles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Tuple


@dataclass(frozen=True)
class LatLonTile:
    """
    1x1 degree tile identified by its south-west corner integer degrees.

    Example:
      lat=3, lon=42 -> N03E042
      lat=-4, lon=120 -> S04E120
    """

    lat: int
    lon: int

    @property
    def tile_id(self) -> str:
        lat_prefix = "N" if self.lat >= 0 else "S"
        lon_prefix = "E" if self.lon >= 0 else "W"
        return f"{lat_prefix}{abs(self.lat):02d}{lon_prefix}{abs(self.lon):03d}"


def tiles_for_bbox(north: float, south: float, east: float, west: float) -> List[LatLonTile]:
    """
    Compute all 1x1 degree tiles intersecting a bbox.

    Assumptions:
      - north > south
      - east > west
      - bbox does not cross antimeridian (east/west wrap). Caller should split if needed.
    """
    if north <= south:
        raise ValueError(f"north ({north}) must be greater than south ({south})")
    if east <= west:
        raise ValueError(f"east ({east}) must be greater than west ({west})")

    # Include tiles covering [south, north) and [west, east). If north/east is integer,
    # we don't want to include the next tile above/right.
    eps = 1e-12
    lat_min = int(math.floor(south))
    lat_max = int(math.floor(north - eps))
    lon_min = int(math.floor(west))
    lon_max = int(math.floor(east - eps))

    tiles: List[LatLonTile] = []
    for lat in range(lat_min, lat_max + 1):
        for lon in range(lon_min, lon_max + 1):
            tiles.append(LatLonTile(lat=lat, lon=lon))
    return tiles


def astgtm_v3_granules_for_tile(tile: LatLonTile, include_num: bool, include_swb: bool) -> List[str]:
    """
    Build ASTGTM.003 granule filenames for a given tile.
    """
    base = f"ASTGTMV003_{tile.tile_id}"
    out = [f"{base}_dem.tif"]
    if include_num:
        out.append(f"{base}_num.tif")
    if include_swb:
        out.append(f"{base}_swb.tif")
    return out

