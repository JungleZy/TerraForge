"""
Contour rendering engine.

Pure helpers (tile math, classification, style) have no heavy deps and are unit
tested directly. The heavy raster->contour->PNG builder (build_contour_tiles)
imports GDAL/numpy/matplotlib lazily inside the function body so this module is
import-safe without them (mirrors services/terrain_tiling/dem_task_tiler.py).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

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


def absolute_hillshade_intensity(elev, azimuth, altitude, vert_exag=1.0, dx=1.0, dy=1.0):
    """逐瓦片一致的光照强度图,值域 [0,1]。

    强度只由局部坡度坡向 + 固定光源(azimuth/altitude)决定,**不做**逐瓦片
    min/max 拉伸 —— matplotlib `LightSource.shade_normals` 会把每个瓦片的强度按
    自身 min/max 拉伸到 0-1,导致相邻瓦片明暗基准不一致、同级瓦片色差。这里改用
    绝对映射:平地(法线 [0,0,1])统一归到 0.5,坡面在其上下偏移,全局基准一致。

    numpy 在函数体内惰性导入,保持本模块 import-safe(与 build_contour_tiles 一致)。
    """
    import numpy as np

    elev = np.asarray(elev, dtype="float64")
    az = math.radians(90.0 - float(azimuth))
    alt = math.radians(float(altitude))
    light_dir = np.array([
        math.cos(az) * math.cos(alt),
        math.sin(az) * math.cos(alt),
        math.sin(alt),
    ])
    # dy 隐式为负(栅格首行在顶部),与 matplotlib LightSource.hillshade 一致。
    e_dy, e_dx = np.gradient(float(vert_exag) * elev, -abs(dy), abs(dx))
    normal = np.empty(elev.shape + (3,), dtype="float64")
    normal[..., 0] = -e_dx
    normal[..., 1] = -e_dy
    normal[..., 2] = 1.0
    normal /= np.sqrt((normal ** 2).sum(axis=-1))[..., np.newaxis]
    intensity = normal.dot(light_dir)
    # 平地·光源 = light_dir[2];减去它再 +0.5 使平地=0.5(中性),全局统一基准。
    return np.clip(intensity - light_dir[2] + 0.5, 0.0, 1.0)


def _build_render_ctx(dem_path, att_path, style, interval, shade, water, out_dir):
    """打开 warp 后的 EPSG:3857 GTiff 并预建渲染所需的全部上下文(geotransform、
    可选水体栅格、全局 hypsometric cmap/norm、各类预转换颜色)。

    串行(主进程)与并行 worker(子进程)都通过它构造 ctx,保证两条路径渲染逻辑
    与参数完全一致。GDAL dataset 不可跨进程传递,所以每个 worker 各自按路径打开。
    """
    from osgeo import gdal
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.colors as mcolors
    from matplotlib.colors import ListedColormap, BoundaryNorm
    from types import SimpleNamespace

    gdal.UseExceptions()
    ctx = SimpleNamespace()
    ds = gdal.Open(dem_path)
    ctx.ds = ds  # 持引用,避免被 GC 关闭
    ctx.band = ds.GetRasterBand(1)
    ctx.nodata = ctx.band.GetNoDataValue()
    ctx.originX, ctx.pxW, _, ctx.originY, _, ctx.pxH = ds.GetGeoTransform()
    ctx.nx, ctx.ny = ds.RasterXSize, ds.RasterYSize

    # 可选水体栅格(ASTWBD att),NEAREST 保留类别值(0 陆 / 1 海 / 2 河 / 3 湖)。
    ctx.att_ds = ctx.att_band = None
    ctx.aOX = ctx.aPW = ctx.aOY = ctx.aPH = ctx.anx = ctx.anumy = None
    if water and att_path:
        ads = gdal.Open(att_path)
        if ads is not None:
            ctx.att_ds = ads
            ctx.att_band = ads.GetRasterBand(1)
            ctx.aOX, ctx.aPW, _, ctx.aOY, _, ctx.aPH = ads.GetGeoTransform()
            ctx.anx, ctx.anumy = ads.RasterXSize, ads.RasterYSize

    # 全局固定 hypsometric 色表 + 边界 norm(所有瓦片共享 -> 跨瓦片颜色一致)。
    ctx.cmap = ctx.norm = None
    if shade:
        colors = list(style.hypsometric_colors)
        breaks = [float(b) for b in style.hypsometric_breaks]
        ctx.cmap = ListedColormap(colors)
        ctx.norm = BoundaryNorm([-1e9] + breaks + [1e9], ncolors=ctx.cmap.N)
    ctx.ocean_rgba = mcolors.to_rgba(style.water_color_ocean)
    ctx.inland_rgba = mcolors.to_rgba(style.water_color_inland)
    ctx.transparent = (style.background or "transparent").strip().lower() == "transparent"
    ctx.bg_rgba = None if ctx.transparent else mcolors.to_rgba(style.background)

    ctx.style = style
    ctx.interval = interval
    ctx.shade = shade
    ctx.water = water
    ctx.out_dir = Path(out_dir)
    return ctx


def _render_contour_tile_core(z, tx, ty, ctx) -> str:
    """渲染单个 slippy 瓦片为 256x256 PNG。返回 'rendered'/'failed'/'skipped'。

    图层 bottom->top:DEM 覆盖区底色(非透明背景时)+ hypsometric tint/hillshade
    (shade)+ ASTWBD 水体(water)+ 等高线与标注。串行与并行 worker 共用此函数。
    """
    import numpy as np
    import matplotlib.pyplot as plt

    xmin, ymin, xmax, ymax = tile_bounds_meters(z, tx, ty)
    col0 = int(math.floor((xmin - ctx.originX) / ctx.pxW)) - 1
    col1 = int(math.ceil((xmax - ctx.originX) / ctx.pxW)) + 1
    row0 = int(math.floor((ymax - ctx.originY) / ctx.pxH)) - 1
    row1 = int(math.ceil((ymin - ctx.originY) / ctx.pxH)) + 1
    col0 = max(col0, 0); row0 = max(row0, 0)
    col1 = min(col1, ctx.nx); row1 = min(row1, ctx.ny)
    if col1 <= col0 or row1 <= row0:
        return "skipped"

    win_x, win_y = col1 - col0, row1 - row0
    arr = ctx.band.ReadAsArray(col0, row0, win_x, win_y).astype("float64")
    if ctx.nodata is not None:
        arr = np.where(arr == ctx.nodata, np.nan, arr)
    if np.all(np.isnan(arr)):
        return "skipped"
    zmin = float(np.nanmin(arr)); zmax = float(np.nanmax(arr))
    arr_extent = (ctx.originX + col0 * ctx.pxW, ctx.originX + col1 * ctx.pxW,
                  ctx.originY + row1 * ctx.pxH, ctx.originY + row0 * ctx.pxH)

    # Contour levels — only where there is elevation variation.
    eff = interval_for_zoom(ctx.interval, z, ctx.style.detail_zoom, ctx.style.zoom_scaling)
    minor: List[float] = []
    major: List[float] = []
    draw_lines = math.isfinite(zmin) and math.isfinite(zmax) and (zmax - zmin) >= 1e-6
    if draw_lines:
        lo = math.floor(zmin / eff) * eff
        hi = math.ceil(zmax / eff) * eff
        levels = [lo + i * eff for i in range(int(round((hi - lo) / eff)) + 1)]
        minor = [lv for lv in levels if not is_index_contour(lv, eff, ctx.style.index_step)]
        major = [lv for lv in levels if is_index_contour(lv, eff, ctx.style.index_step)]
        draw_lines = bool(minor or major)

    # Pure-line mode on a featureless tile: nothing to draw, leave a gap.
    if not ctx.shade and not ctx.water and not draw_lines:
        return "skipped"

    fig = plt.figure(figsize=(2.56, 2.56), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    drew = False
    try:
        # 非透明背景:底色只铺在 DEM 覆盖区(arr 非 NaN),出界/空洞保持透明。
        # 出界区域改由 figure 透明承载,而不是统一 facecolor —— 否则 slippy 瓦片
        # 网格与 DEM 矩形不对齐时,最外圈瓦片的出界部分会被填成背景色(高 zoom
        # '四周白边')。
        if not ctx.transparent:
            base = np.zeros((arr.shape[0], arr.shape[1], 4), dtype="float64")
            base[~np.isnan(arr)] = ctx.bg_rgba
            ax.imshow(base, extent=arr_extent, origin="upper", zorder=-1,
                      interpolation="nearest")

        if ctx.shade and arr.shape[0] >= 2 and arr.shape[1] >= 2:
            fill = zmin if math.isfinite(zmin) else 0.0
            filled = np.where(np.isnan(arr), fill, arr)
            # 绝对 hillshade(全局统一基准,不逐瓦片拉伸)+ 全局 hypsometric 色,
            # 再用 matplotlib 同款 soft/overlay 公式合成 -> 相邻瓦片无明暗接缝。
            intensity = absolute_hillshade_intensity(
                filled, ctx.style.hillshade_azimuth, ctx.style.hillshade_altitude,
                ctx.style.hillshade_vert_exag, abs(ctx.pxW), abs(ctx.pxH))[..., np.newaxis]
            rgb = ctx.cmap(ctx.norm(filled))[..., :3]
            if ctx.style.hillshade_blend == "overlay":
                low = 2 * intensity * rgb
                high = 1 - 2 * (1 - intensity) * (1 - rgb)
                blended = np.where(rgb <= 0.5, low, high)
            else:  # 'soft' (pegtop) 默认;其他值(如 'hsv')回退到 soft
                blended = 2 * intensity * rgb + (1 - 2 * intensity) * rgb ** 2
            rgba = np.empty(arr.shape + (4,), dtype="float64")
            rgba[..., :3] = np.clip(blended, 0.0, 1.0)
            rgba[..., 3] = 1.0
            rgba[np.isnan(arr), 3] = 0.0
            # bilinear: smooth the ~30m DEM when tiles are finer (z14+), else
            # nearest-neighbour upsampling looks like coarse mosaic blocks.
            ax.imshow(rgba, extent=arr_extent, origin="upper", zorder=0, interpolation="bilinear")
            drew = True

        if ctx.water and ctx.att_band is not None:
            ac0 = int(math.floor((xmin - ctx.aOX) / ctx.aPW)) - 1
            ac1 = int(math.ceil((xmax - ctx.aOX) / ctx.aPW)) + 1
            ar0 = int(math.floor((ymax - ctx.aOY) / ctx.aPH)) - 1
            ar1 = int(math.ceil((ymin - ctx.aOY) / ctx.aPH)) + 1
            ac0 = max(ac0, 0); ar0 = max(ar0, 0)
            ac1 = min(ac1, ctx.anx); ar1 = min(ar1, ctx.anumy)
            if ac1 > ac0 and ar1 > ar0:
                att = ctx.att_band.ReadAsArray(ac0, ar0, ac1 - ac0, ar1 - ar0)
                wr = np.zeros((att.shape[0], att.shape[1], 4), dtype="float64")
                wr[att == 1] = ctx.ocean_rgba
                wr[(att == 2) | (att == 3)] = ctx.inland_rgba
                att_extent = (ctx.aOX + ac0 * ctx.aPW, ctx.aOX + ac1 * ctx.aPW,
                              ctx.aOY + ar1 * ctx.aPH, ctx.aOY + ar0 * ctx.aPH)
                ax.imshow(wr, extent=att_extent, origin="upper", zorder=1, interpolation="nearest")
                if bool(np.any(att >= 1)):
                    drew = True

        if draw_lines:
            xs = ctx.originX + (col0 + np.arange(win_x) + 0.5) * ctx.pxW
            ys = ctx.originY + (row0 + np.arange(win_y) + 0.5) * ctx.pxH
            X, Y = np.meshgrid(xs, ys)
            if minor:
                ax.contour(X, Y, arr, levels=minor, colors=ctx.style.color_intermediate,
                           linewidths=ctx.style.width_intermediate, zorder=3)
            if major:
                cs = ax.contour(X, Y, arr, levels=major, colors=ctx.style.color_index,
                                linewidths=ctx.style.width_index, zorder=3)
                ax.clabel(cs, fmt="%d", fontsize=ctx.style.label_size, colors=ctx.style.color_label)
            drew = True

        if not drew:
            return "skipped"

        tile_path = ctx.out_dir / str(z) / str(tx) / f"{ty}.png"
        tile_path.parent.mkdir(parents=True, exist_ok=True)
        # figure 始终透明保存:DEM 内底色由上面的底色图层承载,出界区域透明。
        fig.savefig(str(tile_path), dpi=100, transparent=True,
                    facecolor="none", pad_inches=0)
        return "rendered"
    except Exception:
        return "failed"
    finally:
        plt.close(fig)


# 并行 worker:每个子进程在 initializer 里构造一次自己的 ctx(打开自己的 GDAL
# dataset),存入进程级 global。task 只传可 pickle 的 (z,tx,ty),其余上下文不跨进程。
_CONTOUR_WORKER_CTX = None


def _contour_worker_init(dem_path, att_path, style, interval, shade, water, out_dir):
    global _CONTOUR_WORKER_CTX
    _CONTOUR_WORKER_CTX = _build_render_ctx(dem_path, att_path, style, interval, shade, water, out_dir)


def _contour_worker_render(zxy) -> str:
    z, tx, ty = zxy
    return _render_contour_tile_core(z, tx, ty, _CONTOUR_WORKER_CTX)


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
    workers: int = 0,
) -> dict:
    """
    Warp DEM(s) to EPSG:3857, then per slippy tile read the window and render a
    256x256 PNG. Layers bottom->top: hypsometric tint + hillshade (shade=True),
    ASTWBD water mask (water=True, needs att_tifs), then contour lines + labels.

    Hypsometric coloring uses a global fixed elevation->color map so colors line
    up across tiles. Heavy deps imported lazily so the module stays import-safe.

    workers: 0 = auto (os.cpu_count()); 1 = serial; N>1 = N worker processes.
    并行时每个 worker 各自打开 warp 后的 GTiff;瓦片按 batch 从生成器取(不物化
    整张列表,高 zoom 大区域会 OOM),批间检查 stop_flag。串行与并行共用
    _render_contour_tile_core,产出完全一致。
    """
    from osgeo import gdal
    import os
    import shutil
    import tempfile
    import itertools

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
    dem_path = os.path.join(tmpdir, "dem_3857.tif")
    att_path = None
    try:
        vrt = gdal.BuildVRT("", dem_paths)
        gdal.Warp(dem_path, vrt, format="GTiff",
                  dstSRS="EPSG:3857", resampleAlg="bilinear", dstNodata=-9999,
                  creationOptions=["TILED=YES", "COMPRESS=LZW", "BIGTIFF=IF_SAFER"])
        vrt = None
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    att_paths = [str(p) for p in (att_tifs or [])]
    if water and att_paths:
        try:
            avrt = gdal.BuildVRT("", att_paths)
            _ap = os.path.join(tmpdir, "att_3857.tif")
            gdal.Warp(_ap, avrt, format="GTiff",
                      dstSRS="EPSG:3857", resampleAlg="near",
                      creationOptions=["TILED=YES", "COMPRESS=LZW", "BIGTIFF=IF_SAFER"])
            avrt = None
            att_path = _ap
        except Exception:
            att_path = None

    try:
        # 主进程 ctx:既用于算 coverage/total,串行路径也直接拿它渲染。
        ctx = _build_render_ctx(dem_path, att_path, style, interval, shade, water, str(out_dir))

        cov_west, cov_south = meters_to_lonlat(ctx.originX, ctx.originY + ctx.ny * ctx.pxH)
        cov_east, cov_north = meters_to_lonlat(ctx.originX + ctx.nx * ctx.pxW, ctx.originY)

        # Count tiles arithmetically (no list) — materializing every (z,x,y) tile
        # OOMs at high zoom over large areas (tens of millions of tuples).
        total = 0
        for z in range(zoom_min, zoom_max + 1):
            x0, y0 = deg2num(cov_north, cov_west, z)
            x1, y1 = deg2num(cov_south, cov_east, z)
            total += (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
        counts["total"] = total

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

        def _tally(status):
            if status == "rendered":
                counts["rendered"] += 1
            elif status == "failed":
                counts["failed"] += 1
            _emit()

        n_workers = int(workers or 0)
        if n_workers <= 0:
            # 保守默认:每个 worker 都要加载 GDAL+matplotlib+numpy 并 mmap warp 后的
            # DEM,worker 太多会内存吃紧。无脑用 cpu_count 在多核 Windows 打包环境下
            # 曾 spawn ~20 个重型 worker 把内存打爆、worker 被 OS 杀掉(BrokenProcessPool)。
            # 封顶 4;用户可用 contour_workers 配置显式提高。
            n_workers = min(4, os.cpu_count() or 1)

        def _render_serial():
            nonlocal ctx
            if ctx is None:
                ctx = _build_render_ctx(dem_path, att_path, style, interval, shade, water, str(out_dir))
            for (z, tx, ty) in _iter_tiles():
                if stop_flag is not None and stop_flag.is_set():
                    break
                _tally(_render_contour_tile_core(z, tx, ty, ctx))

        if n_workers == 1 or total <= 4:
            # 串行:主进程 ctx 直接渲染,stop_flag 每瓦片即时检查。
            _render_serial()
        else:
            # 并行:释放主进程 dataset handle(避免 fork 继承),worker 各自打开。
            from concurrent.futures import ProcessPoolExecutor
            from concurrent.futures.process import BrokenProcessPool
            init_args = (dem_path, att_path, style, interval, shade, water, str(out_dir))
            BATCH = 2048  # 批内并行,批间检查 stop_flag;不物化整张瓦片列表
            try:
                ctx = None
                with ProcessPoolExecutor(max_workers=n_workers,
                                         initializer=_contour_worker_init,
                                         initargs=init_args) as ex:
                    tiles = _iter_tiles()
                    while True:
                        if stop_flag is not None and stop_flag.is_set():
                            break
                        batch = list(itertools.islice(tiles, BATCH))
                        if not batch:
                            break
                        for status in ex.map(_contour_worker_render, batch, chunksize=8):
                            _tally(status)
            except BrokenProcessPool as e:
                # worker 进程被异常终止(Windows 打包环境多 worker 内存耗尽常见)。进程级
                # 崩溃 except 兜不住,会让整个任务失败 -> 回退串行重跑保证切片完整:已生成
                # 的 PNG 被覆盖,未生成的补齐。串行单进程内存压力小,通常能跑完。
                logger.warning(
                    f"并行渲染 worker 崩溃({e!r}),回退串行重新渲染整个任务"
                )
                counts["rendered"] = 0
                counts["failed"] = 0
                ctx = None
                _render_serial()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return counts
