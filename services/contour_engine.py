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


def _zoom_interval_multiplier(delta: int, scaling: str = "standard") -> float:
    """Multiplier on the base interval for a tile `delta` zoom levels below the detail zoom.
    standard: step the 1-2-5 ladder once per zoom level.
    gentle:   step once per two zoom levels (low zooms keep more lines).
    """
    if delta <= 0:
        return 1.0
    idx = (delta // 2) if scaling == "gentle" else delta
    ladder = [1, 2, 5]
    return (10 ** (idx // 3)) * ladder[idx % 3]


def interval_for_zoom(base_interval: float, z: int, detail_zoom: int = 14, scaling: str = "standard") -> float:
    """Effective contour interval at slippy zoom `z`. base applies for z >= detail_zoom;
    coarsens up the 1-2-5 ladder for z < detail_zoom. Depends only on zoom (tiles at the
    same zoom share one interval -> contours align across tile boundaries)."""
    delta = max(0, int(detail_zoom) - int(z))
    return base_interval * _zoom_interval_multiplier(delta, scaling)


@dataclass(frozen=True)
class ContourStyle:
    color_intermediate: str = "#9C6B3F"
    color_index: str = "#7A4F2A"
    color_label: str = "#7A4F2A"
    width_intermediate: float = 0.5
    width_index: float = 1.2
    background: str = "#FAF6EC"
    index_step: int = 5
    label_size: float = 6.0
    detail_zoom: int = 14
    zoom_scaling: str = "standard"
    # Hypsometric tints: N elevation breakpoints (m) -> N+1 color bands.
    hypsometric_breaks: tuple = (0.0, 200.0, 500.0, 1000.0, 2000.0, 3000.0, 4000.0, 5000.0)
    hypsometric_colors: tuple = (
        "#5E8C61", "#8FBF6F", "#B6CF7E", "#DCD98E", "#D9B97E",
        "#C49A6C", "#AC7F58", "#8E6246", "#F0EAE2",
    )
    # Hillshade (shaded relief) light source.
    hillshade_azimuth: float = 315.0
    hillshade_altitude: float = 45.0
    hillshade_vert_exag: float = 1.0
    hillshade_blend: str = "soft"
    # Water (ASTWBD att): ocean=1, river=2, lake=3.
    water_color_ocean: str = "#6BAED6"
    water_color_inland: str = "#9ECAE1"

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

        def _tuple_floats(key, default_csv):
            try:
                parts = [x.strip() for x in str(config.get(key, default_csv)).split(",")]
                return tuple(float(x) for x in parts if x != "")
            except (TypeError, ValueError):
                return tuple(float(x) for x in default_csv.split(","))

        def _tuple_strs(key, default_csv):
            parts = [x.strip() for x in str(config.get(key, default_csv)).split(",") if x.strip() != ""]
            return tuple(parts) if parts else tuple(default_csv.split(","))

        return cls(
            color_intermediate=config.get("contour_color_intermediate", "#9C6B3F"),
            color_index=config.get("contour_color_index", "#7A4F2A"),
            color_label=config.get("contour_color_label", "#7A4F2A"),
            width_intermediate=_f("contour_width_intermediate", 0.5),
            width_index=_f("contour_width_index", 1.2),
            background=config.get("contour_background", "#FAF6EC"),
            index_step=_i("contour_index_step", 5),
            label_size=_f("contour_label_size", 6.0),
            detail_zoom=_i("contour_detail_zoom", 14),
            zoom_scaling=config.get("contour_zoom_scaling", "standard"),
            hypsometric_breaks=_tuple_floats(
                "contour_hypsometric_breaks", "0,200,500,1000,2000,3000,4000,5000"),
            hypsometric_colors=_tuple_strs(
                "contour_hypsometric_colors",
                "#5E8C61,#8FBF6F,#B6CF7E,#DCD98E,#D9B97E,#C49A6C,#AC7F58,#8E6246,#F0EAE2"),
            hillshade_azimuth=_f("contour_hillshade_azimuth", 315.0),
            hillshade_altitude=_f("contour_hillshade_altitude", 45.0),
            hillshade_vert_exag=_f("contour_hillshade_vert_exag", 1.0),
            hillshade_blend=config.get("contour_hillshade_blend", "soft"),
            water_color_ocean=config.get("contour_water_color_ocean", "#6BAED6"),
            water_color_inland=config.get("contour_water_color_inland", "#9ECAE1"),
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
    shade: bool = False,
    water: bool = False,
    att_tifs=None,
) -> dict:
    """
    Warp DEM(s) to EPSG:3857, then per slippy tile read the window and render a
    256x256 PNG. Layers bottom->top: hypsometric tint + hillshade (shade=True),
    ASTWBD water mask (water=True, needs att_tifs), then contour lines + labels.

    Hypsometric coloring uses a global fixed elevation->color map so colors line
    up across tiles. Heavy deps imported lazily so the module stays import-safe.
    """
    from osgeo import gdal
    import numpy as np
    import os
    import shutil
    import tempfile
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    from matplotlib.colors import ListedColormap, BoundaryNorm, LightSource

    gdal.UseExceptions()
    out_dir = Path(out_dir)
    dem_paths = [str(p) for p in dem_tifs]
    counts = {"total": 0, "rendered": 0, "failed": 0}
    if not dem_paths:
        return counts

    # Warp to on-disk GTiffs (NOT MEM): a whole-coverage in-RAM warp OOMs on large
    # multi-degree areas. On-disk warp streams to disk, and per-tile windowed reads
    # keep RAM bounded regardless of coverage size. tmpdir is removed at the end.
    tmpdir = tempfile.mkdtemp(prefix="contour_warp_")
    warped = None
    att_warped = None
    try:
        vrt = gdal.BuildVRT("", dem_paths)
        warped = gdal.Warp(os.path.join(tmpdir, "dem_3857.tif"), vrt, format="GTiff",
                           dstSRS="EPSG:3857", resampleAlg="bilinear", dstNodata=-9999,
                           creationOptions=["TILED=YES", "COMPRESS=LZW", "BIGTIFF=IF_SAFER"])
        vrt = None
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    band = warped.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    originX, pxW, _, originY, _, pxH = warped.GetGeoTransform()
    nx, ny = warped.RasterXSize, warped.RasterYSize

    cov_west, cov_south = meters_to_lonlat(originX, originY + ny * pxH)
    cov_east, cov_north = meters_to_lonlat(originX + nx * pxW, originY)

    # Optional water raster (ASTWBD att), warped to disk too. NEAREST keeps the
    # categorical class values (0 land / 1 ocean / 2 river / 3 lake) intact.
    att_band = None
    aOX = aPW = aOY = aPH = anx = anumy = None
    att_paths = [str(p) for p in (att_tifs or [])]
    if water and att_paths:
        try:
            avrt = gdal.BuildVRT("", att_paths)
            att_warped = gdal.Warp(os.path.join(tmpdir, "att_3857.tif"), avrt, format="GTiff",
                                   dstSRS="EPSG:3857", resampleAlg="near",
                                   creationOptions=["TILED=YES", "COMPRESS=LZW", "BIGTIFF=IF_SAFER"])
            avrt = None
            att_band = att_warped.GetRasterBand(1)
            aOX, aPW, _, aOY, _, aPH = att_warped.GetGeoTransform()
            anx, anumy = att_warped.RasterXSize, att_warped.RasterYSize
        except Exception:
            att_warped = None
            att_band = None

    # Global hypsometric colormap + boundary norm + light source (shared by all
    # tiles -> consistent colors/shading across tile boundaries).
    cmap = norm = light = None
    if shade:
        colors = list(style.hypsometric_colors)
        breaks = [float(b) for b in style.hypsometric_breaks]
        cmap = ListedColormap(colors)
        bounds = [-1e9] + breaks + [1e9]
        norm = BoundaryNorm(bounds, ncolors=cmap.N)
        light = LightSource(azdeg=float(style.hillshade_azimuth), altdeg=float(style.hillshade_altitude))
    ocean_rgba = mcolors.to_rgba(style.water_color_ocean)
    inland_rgba = mcolors.to_rgba(style.water_color_inland)

    # Count tiles arithmetically (no list) — materializing every (z,x,y) tile
    # OOMs at high zoom over large areas (tens of millions of tuples).
    total = 0
    for z in range(zoom_min, zoom_max + 1):
        x0, y0 = deg2num(cov_north, cov_west, z)
        x1, y1 = deg2num(cov_south, cov_east, z)
        total += (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
    counts["total"] = total

    transparent = (style.background or "transparent").strip().lower() == "transparent"
    facecolor = "none" if transparent else style.background

    def _emit():
        if progress_cb is not None:
            progress_cb(counts["rendered"] + counts["failed"], counts["total"])

    def _iter_tiles():
        for z in range(zoom_min, zoom_max + 1):
            x0, y0 = deg2num(cov_north, cov_west, z)
            x1, y1 = deg2num(cov_south, cov_east, z)
            txmin, txmax = min(x0, x1), max(x0, x1)
            tymin, tymax = min(y0, y1), max(y0, y1)
            for tx in range(txmin, txmax + 1):
                for ty in range(tymin, tymax + 1):
                    yield z, tx, ty

    for (z, tx, ty) in _iter_tiles():
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
            _emit()
            continue

        win_x, win_y = col1 - col0, row1 - row0
        arr = band.ReadAsArray(col0, row0, win_x, win_y).astype("float64")
        if nodata is not None:
            arr = np.where(arr == nodata, np.nan, arr)
        if np.all(np.isnan(arr)):
            _emit()
            continue
        zmin = float(np.nanmin(arr)); zmax = float(np.nanmax(arr))
        arr_extent = (originX + col0 * pxW, originX + col1 * pxW,
                      originY + row1 * pxH, originY + row0 * pxH)

        # Contour levels — only where there is elevation variation.
        eff = interval_for_zoom(interval, z, style.detail_zoom, style.zoom_scaling)
        minor: List[float] = []
        major: List[float] = []
        draw_lines = math.isfinite(zmin) and math.isfinite(zmax) and (zmax - zmin) >= 1e-6
        if draw_lines:
            lo = math.floor(zmin / eff) * eff
            hi = math.ceil(zmax / eff) * eff
            levels = [lo + i * eff for i in range(int(round((hi - lo) / eff)) + 1)]
            minor = [lv for lv in levels if not is_index_contour(lv, eff, style.index_step)]
            major = [lv for lv in levels if is_index_contour(lv, eff, style.index_step)]
            draw_lines = bool(minor or major)

        # Pure-line mode on a featureless tile: nothing to draw, leave a gap.
        if not shade and not water and not draw_lines:
            _emit()
            continue

        fig = plt.figure(figsize=(2.56, 2.56), dpi=100)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_axis_off()
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        drew = False
        try:
            if shade and arr.shape[0] >= 2 and arr.shape[1] >= 2:
                fill = zmin if math.isfinite(zmin) else 0.0
                filled = np.where(np.isnan(arr), fill, arr)
                rgba = light.shade(filled, cmap=cmap, norm=norm,
                                   vert_exag=float(style.hillshade_vert_exag),
                                   dx=abs(pxW), dy=abs(pxH), blend_mode=style.hillshade_blend)
                rgba[np.isnan(arr), 3] = 0.0
                # bilinear: smooth the ~30m DEM when tiles are finer (z14+), else
                # nearest-neighbour upsampling looks like coarse mosaic blocks.
                ax.imshow(rgba, extent=arr_extent, origin="upper", zorder=0, interpolation="bilinear")
                drew = True

            if water and att_band is not None:
                ac0 = int(math.floor((xmin - aOX) / aPW)) - 1
                ac1 = int(math.ceil((xmax - aOX) / aPW)) + 1
                ar0 = int(math.floor((ymax - aOY) / aPH)) - 1
                ar1 = int(math.ceil((ymin - aOY) / aPH)) + 1
                ac0 = max(ac0, 0); ar0 = max(ar0, 0)
                ac1 = min(ac1, anx); ar1 = min(ar1, anumy)
                if ac1 > ac0 and ar1 > ar0:
                    att = att_band.ReadAsArray(ac0, ar0, ac1 - ac0, ar1 - ar0)
                    wr = np.zeros((att.shape[0], att.shape[1], 4), dtype="float64")
                    wr[att == 1] = ocean_rgba
                    wr[(att == 2) | (att == 3)] = inland_rgba
                    att_extent = (aOX + ac0 * aPW, aOX + ac1 * aPW,
                                  aOY + ar1 * aPH, aOY + ar0 * aPH)
                    ax.imshow(wr, extent=att_extent, origin="upper", zorder=1, interpolation="nearest")
                    if bool(np.any(att >= 1)):
                        drew = True

            if draw_lines:
                xs = originX + (col0 + np.arange(win_x) + 0.5) * pxW
                ys = originY + (row0 + np.arange(win_y) + 0.5) * pxH
                X, Y = np.meshgrid(xs, ys)
                if minor:
                    ax.contour(X, Y, arr, levels=minor, colors=style.color_intermediate,
                               linewidths=style.width_intermediate, zorder=3)
                if major:
                    cs = ax.contour(X, Y, arr, levels=major, colors=style.color_index,
                                    linewidths=style.width_index, zorder=3)
                    ax.clabel(cs, fmt="%d", fontsize=style.label_size, colors=style.color_label)
                drew = True

            if not drew:
                _emit()
                plt.close(fig)
                continue

            tile_path = out_dir / str(z) / str(tx) / f"{ty}.png"
            tile_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(str(tile_path), dpi=100, transparent=transparent,
                        facecolor=facecolor, pad_inches=0)
            counts["rendered"] += 1
        except Exception:
            counts["failed"] += 1
        finally:
            plt.close(fig)

        _emit()

    warped = None
    att_warped = None
    shutil.rmtree(tmpdir, ignore_errors=True)
    return counts
