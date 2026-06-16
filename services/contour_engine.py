"""
Contour rendering engine.

Pure helpers (tile math, classification, style) have no heavy deps and are unit
tested directly. The heavy raster->contour->PNG builder (build_contour_tiles)
imports GDAL/numpy/matplotlib lazily inside the function body so this module is
import-safe without them (mirrors services/terrain_tiling/dem_task_tiler.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

EARTH_RADIUS = 6378137.0
ORIGIN_SHIFT = math.pi * EARTH_RADIUS  # 20037508.342789244
WEB_MERCATOR_MAX_LAT = 85.0511


def lonlat_to_meters(lon: float, lat: float) -> Tuple[float, float]:
    x = math.radians(lon) * EARTH_RADIUS
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * EARTH_RADIUS
    return x, y


def meters_to_lonlat(x: float, y: float) -> Tuple[float, float]:
    lon = math.degrees(x / EARTH_RADIUS)
    lat = math.degrees(2 * math.atan(math.exp(y / EARTH_RADIUS)) - math.pi / 2)
    return lon, lat


def deg2num(lat_deg: float, lon_deg: float, zoom: int) -> Tuple[int, int]:
    lat = max(min(lat_deg, WEB_MERCATOR_MAX_LAT), -WEB_MERCATOR_MAX_LAT)
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    x = min(max(x, 0), n - 1)
    y = min(max(y, 0), n - 1)
    return x, y


def tile_bounds_meters(z: int, x: int, y: int) -> Tuple[float, float, float, float]:
    n = 2 ** z
    tile_size = (2 * ORIGIN_SHIFT) / n
    xmin = -ORIGIN_SHIFT + x * tile_size
    xmax = xmin + tile_size
    ymax = ORIGIN_SHIFT - y * tile_size
    ymin = ymax - tile_size
    return xmin, ymin, xmax, ymax


def tiles_for_bbox_xyz(north: float, south: float, east: float, west: float, zoom: int) -> List[Tuple[int, int]]:
    x0, y0 = deg2num(north, west, zoom)
    x1, y1 = deg2num(south, east, zoom)
    xmin, xmax = min(x0, x1), max(x0, x1)
    ymin, ymax = min(y0, y1), max(y0, y1)
    return [(x, y) for x in range(xmin, xmax + 1) for y in range(ymin, ymax + 1)]


def count_tiles(north: float, south: float, east: float, west: float, zoom_min: int, zoom_max: int) -> int:
    return sum(len(tiles_for_bbox_xyz(north, south, east, west, z)) for z in range(zoom_min, zoom_max + 1))


def is_index_contour(elevation: float, interval: float, index_step: int) -> bool:
    if index_step <= 0 or interval <= 0:
        return False
    major = interval * index_step
    ratio = elevation / major
    return abs(ratio - round(ratio)) < 1e-6


@dataclass(frozen=True)
class ContourStyle:
    color_intermediate: str = "#9C6B3F"
    color_index: str = "#7A4F2A"
    color_label: str = "#7A4F2A"
    width_intermediate: float = 0.5
    width_index: float = 1.2
    background: str = "#FFFFFF"
    index_step: int = 5
    label_size: float = 6.0

    @classmethod
    def from_config(cls, config) -> "ContourStyle":
        def _f(key, default):
            try:
                return float(config.get(key, str(default)))
            except (TypeError, ValueError):
                return float(default)

        def _i(key, default):
            try:
                return int(float(config.get(key, str(default))))
            except (TypeError, ValueError):
                return int(default)

        return cls(
            color_intermediate=config.get("contour_color_intermediate", "#9C6B3F"),
            color_index=config.get("contour_color_index", "#7A4F2A"),
            color_label=config.get("contour_color_label", "#7A4F2A"),
            width_intermediate=_f("contour_width_intermediate", 0.5),
            width_index=_f("contour_width_index", 1.2),
            background=config.get("contour_background", "#FFFFFF"),
            index_step=_i("contour_index_step", 5),
            label_size=_f("contour_label_size", 6.0),
        )


def build_contour_tiles(
    dem_tifs,
    out_dir,
    interval: float,
    zoom_min: int,
    zoom_max: int,
    style: ContourStyle,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    stop_flag=None,
) -> dict:
    """
    Warp DEM(s) to EPSG:3857, then per slippy tile read the window, run
    matplotlib contour (minor + major + labels) and save a transparent 256x256 PNG.
    Heavy deps imported lazily so the module stays import-safe without them.
    """
    from osgeo import gdal
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    gdal.UseExceptions()
    out_dir = Path(out_dir)
    dem_paths = [str(p) for p in dem_tifs]
    counts = {"total": 0, "rendered": 0, "failed": 0}
    if not dem_paths:
        return counts

    vrt = gdal.BuildVRT("", dem_paths)
    warped = gdal.Warp("", vrt, format="MEM", dstSRS="EPSG:3857",
                       resampleAlg="bilinear", dstNodata=-9999)
    vrt = None
    band = warped.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    originX, pxW, _, originY, _, pxH = warped.GetGeoTransform()
    nx, ny = warped.RasterXSize, warped.RasterYSize

    cov_west, cov_south = meters_to_lonlat(originX, originY + ny * pxH)
    cov_east, cov_north = meters_to_lonlat(originX + nx * pxW, originY)

    tile_list = []
    for z in range(zoom_min, zoom_max + 1):
        for (tx, ty) in tiles_for_bbox_xyz(cov_north, cov_south, cov_east, cov_west, z):
            tile_list.append((z, tx, ty))
    counts["total"] = len(tile_list)

    transparent = (style.background or "transparent").strip().lower() == "transparent"
    facecolor = "none" if transparent else style.background

    for (z, tx, ty) in tile_list:
        if stop_flag is not None and stop_flag.is_set():
            break
        xmin, ymin, xmax, ymax = tile_bounds_meters(z, tx, ty)

        col0 = int(math.floor((xmin - originX) / pxW)) - 1
        col1 = int(math.ceil((xmax - originX) / pxW)) + 1
        row0 = int(math.floor((ymax - originY) / pxH)) - 1
        row1 = int(math.ceil((ymin - originY) / pxH)) + 1
        col0 = max(col0, 0); row0 = max(row0, 0)
        col1 = min(col1, nx); row1 = min(row1, ny)
        if col1 <= col0 or row1 <= row0:
            if progress_cb is not None:
                progress_cb(counts["rendered"] + counts["failed"], counts["total"])
            continue

        win_x, win_y = col1 - col0, row1 - row0
        arr = band.ReadAsArray(col0, row0, win_x, win_y).astype("float64")
        if nodata is not None:
            arr = np.where(arr == nodata, np.nan, arr)
        if np.all(np.isnan(arr)):
            if progress_cb is not None:
                progress_cb(counts["rendered"] + counts["failed"], counts["total"])
            continue
        zmin = float(np.nanmin(arr)); zmax = float(np.nanmax(arr))
        if not math.isfinite(zmin) or not math.isfinite(zmax) or (zmax - zmin) < 1e-6:
            if progress_cb is not None:
                progress_cb(counts["rendered"] + counts["failed"], counts["total"])
            continue

        xs = originX + (col0 + np.arange(win_x) + 0.5) * pxW
        ys = originY + (row0 + np.arange(win_y) + 0.5) * pxH
        X, Y = np.meshgrid(xs, ys)

        lo = math.floor(zmin / interval) * interval
        hi = math.ceil(zmax / interval) * interval
        levels = [lo + i * interval for i in range(int(round((hi - lo) / interval)) + 1)]
        minor = [lv for lv in levels if not is_index_contour(lv, interval, style.index_step)]
        major = [lv for lv in levels if is_index_contour(lv, interval, style.index_step)]
        if not minor and not major:
            if progress_cb is not None:
                progress_cb(counts["rendered"] + counts["failed"], counts["total"])
            continue

        fig = plt.figure(figsize=(2.56, 2.56), dpi=100)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_axis_off()
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        try:
            if minor:
                ax.contour(X, Y, arr, levels=minor, colors=style.color_intermediate,
                           linewidths=style.width_intermediate)
            if major:
                cs = ax.contour(X, Y, arr, levels=major, colors=style.color_index,
                                linewidths=style.width_index)
                ax.clabel(cs, fmt="%d", fontsize=style.label_size, colors=style.color_label)
            tile_path = out_dir / str(z) / str(tx) / f"{ty}.png"
            tile_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(str(tile_path), dpi=100, transparent=transparent,
                        facecolor=facecolor, pad_inches=0)
            counts["rendered"] += 1
        except Exception:
            counts["failed"] += 1
        finally:
            plt.close(fig)

        if progress_cb is not None:
            progress_cb(counts["rendered"] + counts["failed"], counts["total"])

    warped = None
    return counts
