"""
Contour rendering engine.

Pure helpers (tile math, classification, style) have no heavy deps and are unit
tested directly. The heavy raster->contour->PNG builder (build_contour_tiles)
imports GDAL/numpy/matplotlib lazily inside the function body so this module is
import-safe without them (mirrors src/services/terrain_tiling/dem_task_tiler.py).
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


# 单张图(瓦片 / 样式预览)允许的等高线级数上限。
# 没有上限时 interval 相对起伏过小就会炸开:0.1m 间距叠 1000m 起伏 ≈ 单瓦片
# 1 万条 trace,而瓦片**内部**没有停止检查(串行在瓦片之间查、并行在 512 张的
# 批之间查),暂停和删除都打不断它。下限 2 是「至少要有一条线可画」。
_MIN_CONTOUR_LEVELS = 2
_MAX_CONTOUR_LEVELS = 200


def render_style_preview(style, interval, width=640, height=200, shade=True) -> bytes:
    """样式预览 PNG：用合成 DEM（沿 x 的海拔斜坡 + 正弦起伏）按与瓦片渲染
    相同的管线出图 —— 分层设色 + 绝对晕渲混合 + 普通/计曲线等高线与标签。
    配色自定义时前端随取色器实时刷新，重依赖（matplotlib/numpy）惰性导入。
    """
    import io

    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.colors as mcolors
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap

    interval = float(interval) if float(interval or 0) > 0 else 50.0
    breaks = [float(b) for b in style.hypsometric_breaks]
    z0 = breaks[0] if breaks else 0.0
    z1 = (breaks[-1] if breaks else 5000.0) * 1.05
    if z1 <= z0:
        z1 = z0 + 1.0

    ny, nx = max(40, height // 4), max(120, width // 4)
    ys, xs = np.mgrid[0:ny, 0:nx].astype("float64")
    ramp = z0 + (z1 - z0) * (xs / max(nx - 1, 1))
    hills = 0.12 * (z1 - z0) * (np.sin(xs / nx * 4 * np.pi) + np.cos(ys / ny * 3 * np.pi))
    elev = ramp + hills

    dpi = 100
    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
    try:
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_axis_off()
        ax.set_xlim(0, nx)
        ax.set_ylim(0, ny)
        extent = (0, nx, 0, ny)

        transparent = (style.background or "transparent").strip().lower() == "transparent"
        if not transparent:
            base = np.empty(elev.shape + (4,), dtype="float64")
            base[...] = mcolors.to_rgba(style.background)
            ax.imshow(base, extent=extent, origin="upper", zorder=-1,
                      interpolation="nearest")

        if shade and style.hypsometric_colors:
            cmap = ListedColormap(list(style.hypsometric_colors))
            norm = BoundaryNorm([-1e9] + breaks + [1e9], ncolors=cmap.N)
            # 合成场的“格网间距”，只影响晕渲的坡度的明暗强度
            dx = dy = (z1 - z0) / 20.0
            intensity = absolute_hillshade_intensity(
                elev, style.hillshade_azimuth, style.hillshade_altitude,
                style.hillshade_vert_exag, dx, dy)[..., np.newaxis]
            rgb = cmap(norm(elev))[..., :3]
            if style.hillshade_blend == "overlay":
                low = 2 * intensity * rgb
                high = 1 - 2 * (1 - intensity) * (1 - rgb)
                blended = np.where(rgb <= 0.5, low, high)
            else:  # 'soft'，与瓦片渲染同一公式
                blended = 2 * intensity * rgb + (1 - 2 * intensity) * rgb ** 2
            rgba = np.empty(elev.shape + (4,), dtype="float64")
            rgba[..., :3] = np.clip(blended, 0.0, 1.0)
            rgba[..., 3] = 1.0
            ax.imshow(rgba, extent=extent, origin="upper", zorder=0,
                      interpolation="bilinear")

        # 等高线：与瓦片同一套 level/索引规则（预览只看样式，与 zoom 无关）
        zmin, zmax = float(np.min(elev)), float(np.max(elev))
        lo = math.floor(zmin / interval) * interval
        hi = math.ceil(zmax / interval) * interval
        n_levels = int(round((hi - lo) / interval)) + 1
        if _MIN_CONTOUR_LEVELS <= n_levels <= _MAX_CONTOUR_LEVELS:
            levels = [lo + i * interval for i in range(n_levels)]
            minor = [lv for lv in levels if not is_index_contour(lv, interval, style.index_step)]
            major = [lv for lv in levels if is_index_contour(lv, interval, style.index_step)]
            X, Y = np.meshgrid(np.arange(nx) + 0.5, np.arange(ny) + 0.5)
            if minor:
                ax.contour(X, Y, elev, levels=minor, colors=style.color_intermediate,
                           linewidths=style.width_intermediate, zorder=3)
            if major:
                cs = ax.contour(X, Y, elev, levels=major, colors=style.color_index,
                                linewidths=style.width_index, zorder=3)
                # %g 保留非整数 interval 的标签(%d 会把 62.5 截成 62)
                ax.clabel(cs, fmt="%g", fontsize=style.label_size,
                          colors=style.color_label)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, transparent=True,
                    facecolor="none", pad_inches=0)
        return buf.getvalue()
    finally:
        plt.close(fig)


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
        # 批量取配置：ConfigManager.get 每键开一次连接，这里 18 个键就是 18 次
        # 连接/查询；get_many 单连接一次取回。无 get_many 的轻量 config 替身
        # （测试 FakeConfig 等）回退逐键 get，行为不变。
        _KEYS = (
            "contour_color_intermediate", "contour_color_index", "contour_color_label",
            "contour_width_intermediate", "contour_width_index", "contour_background",
            "contour_index_step", "contour_label_size", "contour_detail_zoom",
            "contour_zoom_scaling", "contour_hypsometric_breaks",
            "contour_hypsometric_colors", "contour_hillshade_azimuth",
            "contour_hillshade_altitude", "contour_hillshade_vert_exag",
            "contour_hillshade_blend", "contour_water_color_ocean",
            "contour_water_color_inland",
        )
        _get_many = getattr(config, "get_many", None)
        if callable(_get_many):
            _vals = _get_many(_KEYS)

            def _cfg(key, default):
                v = _vals.get(key)
                return default if v is None else v
        else:
            _cfg = config.get

        def _f(key, default):
            try:
                return float(_cfg(key, str(default)))
            except (TypeError, ValueError):
                return float(default)

        def _i(key, default):
            try:
                return int(float(_cfg(key, str(default))))
            except (TypeError, ValueError):
                return int(default)

        def _tuple_floats(key, default_csv):
            try:
                parts = [x.strip() for x in str(_cfg(key, default_csv)).split(",")]
                return tuple(float(x) for x in parts if x != "")
            except (TypeError, ValueError):
                return tuple(float(x) for x in default_csv.split(","))

        def _tuple_strs(key, default_csv):
            parts = [x.strip() for x in str(_cfg(key, default_csv)).split(",") if x.strip() != ""]
            return tuple(parts) if parts else tuple(default_csv.split(","))

        return cls(
            color_intermediate=_cfg("contour_color_intermediate", "#9C6B3F"),
            color_index=_cfg("contour_color_index", "#7A4F2A"),
            color_label=_cfg("contour_color_label", "#7A4F2A"),
            width_intermediate=_f("contour_width_intermediate", 0.5),
            width_index=_f("contour_width_index", 1.2),
            background=_cfg("contour_background", "#FAF6EC"),
            index_step=_i("contour_index_step", 5),
            label_size=_f("contour_label_size", 6.0),
            detail_zoom=_i("contour_detail_zoom", 14),
            zoom_scaling=_cfg("contour_zoom_scaling", "standard"),
            hypsometric_breaks=_tuple_floats(
                "contour_hypsometric_breaks", "0,200,500,1000,2000,3000,4000,5000"),
            hypsometric_colors=_tuple_strs(
                "contour_hypsometric_colors",
                "#5E8C61,#8FBF6F,#B6CF7E,#DCD98E,#D9B97E,#C49A6C,#AC7F58,#8E6246,#F0EAE2"),
            hillshade_azimuth=_f("contour_hillshade_azimuth", 315.0),
            hillshade_altitude=_f("contour_hillshade_altitude", 45.0),
            hillshade_vert_exag=_f("contour_hillshade_vert_exag", 1.0),
            hillshade_blend=_cfg("contour_hillshade_blend", "soft"),
            water_color_ocean=_cfg("contour_water_color_ocean", "#6BAED6"),
            water_color_inland=_cfg("contour_water_color_inland", "#9ECAE1"),
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


# 单瓦片读窗口的像素上限(= 256 输出像素 + 2 余量,与输出瓦片对齐)。
# 低 zoom 时一个瓦片的窗口可能覆盖整幅 DEM —— 按原始分辨率 ReadAsArray 会把
# 整幅 float64 读进内存(大 DEM 直接 OOM),超过上限时改由 GDAL 端重采样到
# 上限尺寸再返回(同 src/services/terrain_tiling/cesiumlab_terrain.py 的做法)。
_MAX_READ_DIM = 258


def _read_band_window(band, xoff, yoff, win_x, win_y, resample_alg=None):
    """读栅格窗口;窗口超过 _MAX_READ_DIM 时让 GDAL 重采样到上限尺寸。

    窗口不超限时不传任何 buf 参数,行为与原 ReadAsArray 完全一致。返回数组的
    shape 为 (min(win_y, _MAX_READ_DIM), min(win_x, _MAX_READ_DIM))。
    """
    buf_x = min(win_x, _MAX_READ_DIM)
    buf_y = min(win_y, _MAX_READ_DIM)
    if buf_x == win_x and buf_y == win_y:
        return band.ReadAsArray(xoff, yoff, win_x, win_y)
    kwargs = {"buf_xsize": buf_x, "buf_ysize": buf_y}
    if resample_alg is not None:
        kwargs["resample_alg"] = resample_alg
    return band.ReadAsArray(xoff, yoff, win_x, win_y, **kwargs)


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

    每瓦片容错:读窗口/ReadAsArray/level 计算与绘图全部在 try 内 —— 单瓦片
    I/O 或渲染异常只计 'failed' 并记 warning,绝不炸掉整个任务。
    """
    import numpy as np
    import matplotlib.pyplot as plt
    from osgeo import gdal

    try:
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
        arr = _read_band_window(ctx.band, col0, row0, win_x, win_y,
                                resample_alg=gdal.GRA_Bilinear).astype("float64")
        # 重采样后 arr 可能小于原始窗口:有效像元尺寸按实际 shape 折算
        # (未重采样时恒等于 ctx.pxW/pxH,行为不变)。
        eff_px_w = ctx.pxW * win_x / arr.shape[1]
        eff_px_h = ctx.pxH * win_y / arr.shape[0]
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
            n_levels = int(round((hi - lo) / eff)) + 1
            # 与 render_style_preview 同一道闸门(_MAX_CONTOUR_LEVELS)。创建时
            # 已按 interval 下限拒收,这里兜住的是存量行:级数无上限时单瓦片
            # 会跑到分钟级,而瓦片内部没有停止检查,暂停/删除都打不断。
            # 不按倍数放粗 eff 来"救"这张瓦片 —— eff 只依赖 zoom 才能保证同层
            # 相邻瓦片的等高线接得上(见 interval_for_zoom),一旦改成依赖本瓦片
            # 自己的 zmin/zmax,边界就会错位。
            if n_levels > _MAX_CONTOUR_LEVELS:
                if not getattr(ctx, "levels_capped", False):
                    ctx.levels_capped = True
                    logger.warning(
                        f"Contour: interval={eff:g} 在 z={z} 需要 {n_levels} 级"
                        f"(上限 {_MAX_CONTOUR_LEVELS}),该瓦片不画等高线;"
                        f"请把 contour_interval 调大")
                draw_lines = False
            else:
                levels = [lo + i * eff for i in range(n_levels)]
                minor = [lv for lv in levels if not is_index_contour(lv, eff, ctx.style.index_step)]
                major = [lv for lv in levels if is_index_contour(lv, eff, ctx.style.index_step)]
                draw_lines = bool(minor or major)

        # Pure-line mode on a featureless tile: nothing to draw, leave a gap.
        if not ctx.shade and not ctx.water and not draw_lines:
            return "skipped"

        # figure 复用:plt.figure()+close() 每瓦片各一次,大头是 figure/后端
        # 初始化的固定开销;同一 worker(串行=主进程 ctx,并行=子进程 ctx)内复用
        # 单个 figure,每瓦片 clear() 清轴重画。clear() 会移除全部 axes/artist,
        # clabel 的标签状态挂在 ContourSet/axes 上随 clear 一并清掉,不串味。
        # figure 挂在 ctx 上,任务结束由 build_contour_tiles 统一 close。
        fig = getattr(ctx, "fig", None)
        if fig is None:
            fig = plt.figure(figsize=(2.56, 2.56), dpi=100)
            ctx.fig = fig
        else:
            fig.clear()
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_axis_off()
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        drew = False
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
                ctx.style.hillshade_vert_exag, abs(eff_px_w), abs(eff_px_h))[..., np.newaxis]
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
                att = _read_band_window(ctx.att_band, ac0, ar0, ac1 - ac0, ar1 - ar0)
                wr = np.zeros((att.shape[0], att.shape[1], 4), dtype="float64")
                wr[att == 1] = ctx.ocean_rgba
                wr[(att == 2) | (att == 3)] = ctx.inland_rgba
                att_extent = (ctx.aOX + ac0 * ctx.aPW, ctx.aOX + ac1 * ctx.aPW,
                              ctx.aOY + ar1 * ctx.aPH, ctx.aOY + ar0 * ctx.aPH)
                ax.imshow(wr, extent=att_extent, origin="upper", zorder=1, interpolation="nearest")
                if bool(np.any(att >= 1)):
                    drew = True

        if draw_lines:
            # 窗口偏移必须用【原始】像元 pxW/pxH,只有窗口内部的步进才用重采样后
            # 的 eff_px_*。写成 (col0 + i + 0.5) * eff_px_w 会让偏移项也被缩放,
            # 误差 col0*pxW*(win_x - arr.shape[1])/arr.shape[1] 随瓦片越靠东/南
            # 线性增大,低 zoom 下整片等高线被 set_xlim 裁掉(H1)。
            # 口径与上面 arr_extent 的 originX + col0*ctx.pxW 保持一致。
            xs = ctx.originX + col0 * ctx.pxW + (np.arange(arr.shape[1]) + 0.5) * eff_px_w
            ys = ctx.originY + row0 * ctx.pxH + (np.arange(arr.shape[0]) + 0.5) * eff_px_h
            X, Y = np.meshgrid(xs, ys)
            if minor:
                ax.contour(X, Y, arr, levels=minor, colors=ctx.style.color_intermediate,
                           linewidths=ctx.style.width_intermediate, zorder=3)
            if major:
                cs = ax.contour(X, Y, arr, levels=major, colors=ctx.style.color_index,
                                linewidths=ctx.style.width_index, zorder=3)
                # %g 保留非整数 interval 的标签(%d 会把 62.5 截成 62)
                ax.clabel(cs, fmt="%g", fontsize=ctx.style.label_size, colors=ctx.style.color_label)
            drew = True

        if not drew:
            return "skipped"

        tile_path = ctx.out_dir / str(z) / str(tx) / f"{ty}.png"
        tile_path.parent.mkdir(parents=True, exist_ok=True)
        # figure 始终透明保存:DEM 内底色由上面的底色图层承载,出界区域透明。
        fig.savefig(str(tile_path), dpi=100, transparent=True,
                    facecolor="none", pad_inches=0)
        return "rendered"
    except Exception as e:
        logger.warning(f"等高线瓦片渲染失败 z={z} x={tx} y={ty}: {e!r}")
        return "failed"
    # 复用的 figure 不在此 close:异常留下的半截内容会在下一瓦片 fig.clear()
    # 时清掉;任务结束时由 build_contour_tiles 统一关闭(见下方 finally)。


# 并行 worker:每个子进程在 initializer 里构造一次自己的 ctx(打开自己的 GDAL
# dataset),存入进程级 global。task 只传可 pickle 的 (z,tx,ty),其余上下文不跨进程。
_CONTOUR_WORKER_CTX = None


def _contour_worker_init(dem_path, att_path, style, interval, shade, water, out_dir):
    global _CONTOUR_WORKER_CTX
    _CONTOUR_WORKER_CTX = _build_render_ctx(dem_path, att_path, style, interval, shade, water, out_dir)


def _contour_worker_render(zxy) -> str:
    z, tx, ty = zxy
    return _render_contour_tile_core(z, tx, ty, _CONTOUR_WORKER_CTX)


def _build_raster_overviews(path, resample):
    """给 warp 产物建内部金字塔(overviews)。

    warp 产物只开了 TILED+LZW、没有金字塔:低 zoom 时一个瓦片的读窗口覆盖很大
    范围,_read_band_window 要靠 GDAL 把全分辨率窗口解压后再重采样到 258px,
    每瓦片都重复这份解压。有了 overviews,GDAL 重采样直接读就近层级,低 zoom
    解压量随层级指数下降。级别按栅格尺寸翻倍,到 <256 像素(约一瓦片)止。
    best-effort:失败只记 warning —— 没有金字塔渲染结果不变,只是慢。
    """
    from osgeo import gdal
    try:
        ds = gdal.Open(path, gdal.GA_Update)
        if ds is None:
            return
        dim = max(ds.RasterXSize, ds.RasterYSize)
        levels = []
        lv = 2
        while dim // lv >= 256:
            levels.append(lv)
            lv *= 2
        if levels:
            ds.BuildOverviews(resample, levels)
        ds = None
    except Exception as e:
        logger.warning(f"Contour: 建 overviews 失败({e!r}),跳过(仅影响低 zoom 读取性能)")


def build_contour_tiles(
    dem_tifs,
    out_dir,
    interval: float,
    zoom_min: int,
    zoom_max: int,
    style: ContourStyle,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    stage_cb: Optional[Callable[[str, float], None]] = None,
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

    workers: 0 = auto (min(4, os.cpu_count())); 1 = serial; N>1 = N worker processes.
    并行时每个 worker 各自打开 warp 后的 GTiff;瓦片按 batch 从生成器取(不物化
    整张列表,高 zoom 大区域会 OOM),批间检查 stop_flag。串行与并行共用
    _render_contour_tile_core,产出完全一致。
    """
    from osgeo import gdal
    import os
    import shutil
    import tempfile
    import itertools
    import time

    gdal.UseExceptions()
    out_dir = Path(out_dir)
    dem_paths = [str(p) for p in dem_tifs]
    counts = {"total": 0, "rendered": 0, "failed": 0, "skipped": 0}
    if not dem_paths:
        return counts

    # Warp to on-disk GTiffs (NOT MEM): a whole-coverage in-RAM warp OOMs on large
    # multi-degree areas. On-disk warp streams to disk, and per-tile windowed reads
    # keep RAM bounded regardless of coverage size. tmpdir is removed at the end.
    # 大区域 warp 产物可达数十 GB,默认落系统临时目录;contour_warp_tmpdir 配置键
    # 可指到空间充足的盘(留空 = 系统默认)。
    from src.services.config_manager import ConfigManager
    try:
        warp_tmp_base = (ConfigManager().get("contour_warp_tmpdir", "") or "").strip() or None
    except Exception as e:
        # 配置库不可用(fresh clone 尚无 data/ 目录、cwd 不同等)不应让渲染失败;
        # ConfigManager.get 对 sqlite 错误是有意重抛的,这里自行兜底。
        logger.warning(f"读取 contour_warp_tmpdir 失败({e!r}),回退系统临时目录")
        warp_tmp_base = None
    tmpdir = tempfile.mkdtemp(prefix="contour_warp_", dir=warp_tmp_base)
    dem_path = os.path.join(tmpdir, "dem_3857.tif")
    att_path = None
    _t_warp = time.time()
    logger.info(f"Contour: 开始 warp {len(dem_paths)} 个 DEM 到 EPSG:3857(大区域可能耗时数十秒)...")

    def _stage(phase: str, fraction: float) -> None:
        """阶段进度。这一段跑在 progress_cb 有意义之前 —— total 要等 warp 完、
        建完 ctx 才算得出,所以没有分母,只能报阶段 + 比例。"""
        if stage_cb is None:
            return
        try:
            stage_cb(phase, float(fraction))
        except Exception:
            logger.debug("contour stage_cb failed (ignored)", exc_info=True)

    def _gdal_stage(phase: str):
        """GDAL 原生进度回调 -> _stage。

        ⚠️ 回调里绝不能抛:实测 GDAL 把回调抛异常当成「用户请求中止」——
        gdal.Warp 会失败、产物被删。恒返回 1(继续)。
        """
        if stage_cb is None:
            return None

        def _cb(complete, message, cb_data):
            _stage(phase, float(complete))
            return 1

        return _cb

    _stage("warp", 0.0)
    try:
        vrt = gdal.BuildVRT("", dem_paths)
        gdal.Warp(dem_path, vrt, format="GTiff",
                  dstSRS="EPSG:3857", resampleAlg="bilinear", dstNodata=-9999,
                  creationOptions=["TILED=YES", "COMPRESS=LZW", "BIGTIFF=IF_SAFER"],
                  callback=_gdal_stage("warp"))
        vrt = None
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    logger.info(f"Contour: DEM warp 完成, 耗时 {time.time() - _t_warp:.1f}s")
    _stage("warp", 1.0)
    # 金字塔与 warp 的 bilinear 重采样口径一致(逐层平均约等于连续 bilinear)
    _stage("overview", 0.0)
    _build_raster_overviews(dem_path, "BILINEAR")
    _stage("overview", 1.0)

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
            # att 是类别栅格(0陆/1海/2河/3湖),金字塔必须 NEAREST 保留类别值
            _build_raster_overviews(att_path, "NEAREST")
        except Exception as e:
            # 水体是 best-effort:att warp 失败只跳过水色层,但必须留日志,
            # 否则"开了 water 却没有水体"无从排查
            logger.warning(f"Contour: ASTWBD att warp 失败({e!r}),水体图层跳过,任务继续")
            att_path = None

    ctx = None  # 先置 None:_build_render_ctx 抛错时 finally 里也能安全引用
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
                # 进度按 processed(rendered+skipped+failed)计:skipped(无数据/
                # 无线条可画)也是处理完的瓦片,不计的话进度条会停在如 72% 就直接
                # completed。
                progress_cb(counts["rendered"] + counts["failed"] + counts["skipped"],
                            counts["total"])

        # total 到这里就已知了,而 _emit 此前只被逐瓦片的 _tally 调用 —— 第一发要
        # 等**第一块瓦片渲染完**。上传任务的 total_tiles 在建任务时写死为 0,于是
        # 界面在整个 warp + 建 ctx + worker 启动期间显示「0 / 0 瓦片 · 0%」不动。
        # 先上报一次真实分母。
        _emit()

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
            else:  # "skipped"
                counts["skipped"] += 1
            _emit()

        n_workers = int(workers or 0)
        if n_workers <= 0:
            # 保守默认:每个 worker 都要加载 GDAL+matplotlib+numpy 并 mmap warp 后的
            # DEM,worker 太多会内存吃紧。无脑用 cpu_count 在多核 Windows 打包环境下
            # 曾 spawn ~20 个重型 worker 把内存打爆、worker 被 OS 杀掉(BrokenProcessPool)。
            # 封顶 4;用户可用 contour_workers 配置显式提高。
            n_workers = min(4, os.cpu_count() or 1)

        def _render_serial(skip_existing=False):
            nonlocal ctx
            if ctx is None:
                ctx = _build_render_ctx(dem_path, att_path, style, interval, shade, water, str(out_dir))
            for (z, tx, ty) in _iter_tiles():
                if stop_flag is not None and stop_flag.is_set():
                    break
                if skip_existing:
                    # BrokenProcessPool 回退重跑:已落盘的 PNG(含崩溃批次里已写但
                    # 未及 tally 的)直接计 rendered,不重复渲染——counts 已清零,
                    # 不会重复计数,进度也会快速爬回崩溃前位置而不是长期停在 0。
                    # 零字节文件视为崩溃残留,照常重渲染。
                    tile_path = ctx.out_dir / str(z) / str(tx) / f"{ty}.png"
                    if tile_path.exists() and tile_path.stat().st_size > 0:
                        _tally("rendered")
                        continue
                _tally(_render_contour_tile_core(z, tx, ty, ctx))

        _serial = n_workers == 1 or total <= 4
        logger.info(f"Contour: 开始渲染 {total} 瓦片, 模式={'串行' if _serial else f'并行 {n_workers} workers'}")
        _t_render = time.time()
        if _serial:
            # 串行:主进程 ctx 直接渲染,stop_flag 每瓦片即时检查。
            _render_serial()
        else:
            # 并行:释放主进程 dataset handle(worker 各自打开)。
            from concurrent.futures import ProcessPoolExecutor
            from concurrent.futures.process import BrokenProcessPool
            import multiprocessing
            init_args = (dem_path, att_path, style, interval, shade, water, str(out_dir))
            BATCH = 512  # 批内并行,批间检查 stop_flag;不物化整张瓦片列表。
            # 2048 -> 512:暂停/停止要等当前批跑完才生效,批小 4 倍响应约快 4 倍;
            # 单批 list 内存从 2048 个 (z,x,y) 元组降到 512 个,本身都可忽略。
            #
            # 显式 spawn,不用 Linux 默认的 fork:本进程是多线程的(Flask +
            # SocketIO + 任务线程),fork 一个持锁的多线程进程,子进程可能带着
            # 一把已被别的线程持有的锁启动然后死锁;CPython 已经为此发
            # DeprecationWarning,3.14 起 Linux 默认改 forkserver。Windows 打包
            # 目标本来就只有 spawn,所以 worker 早已必须是 spawn-safe 的
            # (initargs 全可 pickle,ctx 在 initializer 里各自重建)。
            try:
                ctx = None
                with ProcessPoolExecutor(max_workers=n_workers,
                                         mp_context=multiprocessing.get_context("spawn"),
                                         initializer=_contour_worker_init,
                                         initargs=init_args) as ex:
                    logger.info(f"Contour: {n_workers} 个渲染 worker 已就绪, 开始分批渲染(每批 {BATCH})")
                    tiles = _iter_tiles()
                    while True:
                        if stop_flag is not None and stop_flag.is_set():
                            break
                        batch = list(itertools.islice(tiles, BATCH))
                        if not batch:
                            break
                        for status in ex.map(_contour_worker_render, batch, chunksize=8):
                            _tally(status)
                        processed = counts['rendered'] + counts['failed'] + counts['skipped']
                        logger.info(f"Contour: 已处理 {processed}/{total} 瓦片, 耗时 {time.time() - _t_render:.1f}s")
            except BrokenProcessPool as e:
                # worker 进程被异常终止(Windows 打包环境多 worker 内存耗尽常见)。进程级
                # 崩溃 except 兜不住,会让整个任务失败 -> 回退串行重跑:已落盘的 PNG 跳过
                # 直接计数(skip_existing),未生成的补齐。串行单进程内存压力小,通常能跑完。
                logger.warning(
                    f"并行渲染 worker 崩溃({e!r}),回退串行重跑(已生成的瓦片跳过)"
                )
                counts["rendered"] = 0
                counts["failed"] = 0
                counts["skipped"] = 0
                ctx = None
                _render_serial(skip_existing=True)
    finally:
        # 串行路径(含 BrokenProcessPool 回退)在主进程 ctx 上复用的 figure
        # 任务结束统一关闭;并行 worker 的 figure 随子进程退出释放。
        _fig = getattr(ctx, "fig", None) if ctx is not None else None
        if _fig is not None:
            import matplotlib.pyplot as plt
            plt.close(_fig)
        # L4: rmtree 之前必须先放掉 GDAL 句柄。ctx.ds / ctx.att_ds 从 _build_render_ctx
        # 建好后就没被置过 None,凡是「ctx 建成后未清就走到这里」的路径(串行、
        # BrokenProcessPool 回退里 _render_serial 用 nonlocal 重建的 ctx、渲染中途
        # 抛异常)都会让 Windows 上的 warp 产物删不掉,而 ignore_errors=True 把
        # PermissionError 静默吞掉 —— 单次运行期间每跑一次串行/回退任务就泄漏
        # 一份(大区域数十 GB),直到下次启动清扫才回收。同项目 stitch 路径每个
        # dataset 在 rmtree 前都显式置 None,注释还自述了 Windows 文件锁的理由。
        if ctx is not None:
            for _attr in ("band", "att_band", "ds", "att_ds"):
                try:
                    setattr(ctx, _attr, None)
                except Exception:
                    pass
        ctx = None

        # 不再静默吞：删不掉要留下线索（Windows 文件锁是最常见原因）。
        # onerror 在 3.12 起弃用、3.14 移除，onexc 是替代品 —— 两个签名不同
        # （onexc 收 (func, path, exc)，onerror 收 (func, path, exc_info)），
        # 按运行时支持情况分派，别让弃用告警在未来变成硬错误。
        try:
            shutil.rmtree(
                tmpdir,
                onexc=lambda func, path, exc: logger.warning(
                    f"清理等高线 warp 临时目录失败: {path} ({exc!r})"))
        except TypeError:  # Python < 3.12 没有 onexc
            shutil.rmtree(
                tmpdir,
                onerror=lambda func, path, exc_info: logger.warning(
                    f"清理等高线 warp 临时目录失败: {path} ({exc_info[1]!r})"))

    return counts
