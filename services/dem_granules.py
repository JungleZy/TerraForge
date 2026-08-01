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


def coverage_bbox(north: float, south: float, east: float, west: float) -> Tuple[float, float, float, float]:
    """
    Bounding box of the union of 1x1 degree tiles intersecting the given bbox.

    DEM download and contour rendering operate on whole granule tiles, so the
    area actually covered is this whole-degree-aligned box, which is always
    equal to or larger than the framed selection. Returns (north, south, east,
    west) in degrees. Agrees exactly with the tiles from tiles_for_bbox.

    边界就是 tiles_for_bbox 里 floor(south)/floor(north-eps) 的算术结果，
    直接算 —— 全球 bbox 会物化 6 万+ LatLonTile 对象再取 min/max，没有必要。
    校验规则与 tiles_for_bbox 保持一致（非法 bbox 同样抛 ValueError）。
    """
    if north <= south:
        raise ValueError(f"north ({north}) must be greater than south ({south})")
    if east <= west:
        raise ValueError(f"east ({east}) must be greater than west ({west})")

    eps = 1e-12
    lat_min = int(math.floor(south))
    lat_max = int(math.floor(north - eps))
    lon_min = int(math.floor(west))
    lon_max = int(math.floor(east - eps))
    return (lat_max + 1, lat_min, lon_max + 1, lon_min)


def astgtm_v3_granules_for_tile(tile: LatLonTile, include_num: bool, include_swb: bool) -> List[str]:
    """
    Build ASTGTM.003 granule filenames for a given tile.

    Note: ASTGTM.003 only ships _dem and _num. There is no _swb granule in this
    product — requesting one is rejected explicitly; real water bodies come from
    the separate ASTWBD.001 product (see astwbd_v1_att_granules_for_tile).

    Coverage is 83S-83N: tiles with |lat| > 83 have no data (every request would
    404), so they produce no granules at all.
    """
    if include_swb:
        raise ValueError(
            "ASTGTM.003 has no _swb granules; water body data comes from the "
            "separate ASTWBD.001 product (see astwbd_v1_att_granules_for_tile)"
        )
    if abs(tile.lat) > 83:
        return []
    base = f"ASTGTMV003_{tile.tile_id}"
    out = [f"{base}_dem.tif"]
    if include_num:
        out.append(f"{base}_num.tif")
    return out


def copernicus_glo30_granules_for_tile(tile: LatLonTile) -> List[str]:
    """
    Copernicus DEM GLO-30 (30m) granule path on the AWS public bucket (no auth).

    Tiles are nested as <name>/<name>.tif, e.g.
    Copernicus_DSM_COG_10_N40_00_E116_00_DEM/Copernicus_DSM_COG_10_N40_00_E116_00_DEM.tif
    (the "10" denotes GLO-30; file suffix is uppercase _DEM.tif).
    """
    lat_p = "N" if tile.lat >= 0 else "S"
    lon_p = "E" if tile.lon >= 0 else "W"
    name = f"Copernicus_DSM_COG_10_{lat_p}{abs(tile.lat):02d}_00_{lon_p}{abs(tile.lon):03d}_00_DEM"
    return [f"{name}/{name}.tif"]


def astwbd_v1_att_granules_for_tile(tile: LatLonTile) -> List[str]:
    """
    ASTWBD V1 water-body attribute GeoTIFF filename for a tile.

    The *_att.tif attribute layer classifies each pixel: 0=land, 1=ocean,
    2=river, 3=lake. Distributed on LP DAAC alongside ASTGTM, same tiling.
    """
    return [f"ASTWBDV001_{tile.tile_id}_att.tif"]

