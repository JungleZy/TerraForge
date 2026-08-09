"""把浏览器读出的 GeoTIFF 头部标签解释成人能看懂的有效信息。

分工：字节层面的解析在前端（static/js/geotiff_meta.js，用 File.slice 只读几 KB，
不上传整包）；地理层面的解释在这里 —— EPSG 码要变成坐标系名称、投影坐标要
落回 WGS84 经纬度，这两件事只有带完整 CRS 库的 GDAL/osr 做得对（国内 DEM 常见
CGCS2000 高斯克吕格分带，手写换算迟早出错）。

osr 一律惰性 import，缺 GDAL 时退化成「只报原生坐标系下的范围」而不是报错：
装不上 GDAL 的环境根本切不了片，在这里卡住用户没有意义。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.services.geo_validation import MAX_ZOOM, MIN_ZOOM

logger = logging.getLogger(__name__)

# 一次探测的文件数上限，与 create_local_terrain_task 的上传上限同口径
MAX_INSPECT_FILES = 100

# 请求体上限。这条接口按设计只收几 KB 的头部标签，从不收文件本身；全局的
# Config.MAX_CONTENT_LENGTH（2 GiB）是给真上传留的，套在这里等于允许对方
# 先让服务端把 2 GiB 缓存进内存、解析完再被 MAX_INSPECT_FILES 拒掉。
MAX_INSPECT_BODY = 1 << 20

# 回显给界面的字段长度/量级上限。全部来自浏览器，不封顶就是让人往信息卡里
# 塞任意长的一行；size 还会经 JSON 进 JS，超过 2**53 就不再是它自己了。
_MAX_NAME_CHARS = 260          # Windows MAX_PATH，文件名不可能比它长
_MAX_CITATION_CHARS = 256
_MAX_DIMENSION = 2 ** 31       # TIFF 自己的行/列量级上限
_MAX_JS_SAFE_INT = 2 ** 53

# 投影后的矩形边界是曲线，只取四个角必然内缩。每条边加密到 21 个采样点
# （与 TransformBounds 的 densify_pts 同一档位），省级 Albers/兰勃特 DEM
# 和极地立体投影的范围才不会报小。100 个文件 × 84 点的代价可以忽略。
_DENSIFY_STEPS = 21


class InspectError(ValueError):
    """输入不合法，带 i18n 键而不是英文原文。

    路由拿 .key/.params 翻译后回给浏览器 —— 直接回 str(e) 的话，中文界面上
    会出现一句嵌在译文里的生英文（信息卡就是原样渲染这段文字的）。
    继承 ValueError：既有的 `except ValueError` 调用方与用例照旧接得住。
    """

    def __init__(self, key: str, **params: Any):
        super().__init__(key)
        self.key = key
        self.params = params


# 赤道处 1 度经度的米数。与 contour_task_manager._finest_pixel_size_3857 同一常数，
# 只用来把投影坐标系的米级像素折算成度，供层级估算用。
_M_PER_DEG = 111320.0

# 地形切片的瓦片顶点网格边长，见 dem_task_tiler.TileParams.tile_size。
# 建议层级必须按同一个值算，否则给出的数字与真正切出来的不是一回事。
_TERRAIN_TILE_SIZE = 65

# TIFF SampleFormat(339) × BitsPerSample(258) -> GDAL 风格的数据类型名
_DTYPE_NAMES = {
    (1, 8): "Byte", (1, 16): "UInt16", (1, 32): "UInt32", (1, 64): "UInt64",
    (2, 8): "Int8", (2, 16): "Int16", (2, 32): "Int32", (2, 64): "Int64",
    (3, 16): "Float16", (3, 32): "Float32", (3, 64): "Float64",
}

# TIFF Compression(259) -> 名称。只列 DEM 里真会遇到的。
_COMPRESSION_NAMES = {
    1: "None", 5: "LZW", 7: "JPEG", 8: "Deflate", 32773: "PackBits",
    32946: "Deflate", 34887: "LERC", 34925: "LZMA", 50000: "ZSTD", 50001: "WebP",
}


def _number(value: Any) -> Optional[float]:
    """把 JSON 里来的东西收成有限 float；None/字符串/NaN/inf 一律当没有。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        v = float(value)
    except (OverflowError, ValueError):
        # JSON 的整数字面量是任意精度的（1 后面 400 个 0 照样解析成 int），
        # float() 对它抛的是 OverflowError 而不是 ValueError —— 不在这个收口处
        # 挡住，十来个调用点里随便哪个都能把路由打成 500。
        return None
    return v if math.isfinite(v) else None


def _floats(value: Any, expect: int) -> Optional[List[float]]:
    if not isinstance(value, (list, tuple)) or len(value) < expect:
        return None
    out = []
    for item in value[:expect]:
        n = _number(item)
        if n is None:
            return None
        out.append(n)
    return out


def _geotransform(entry: Mapping[str, Any]) -> Optional[Tuple[float, ...]]:
    """还原 GDAL 六参数 geotransform。

    GeoTIFF 有两种写法，DEM 两种都会遇到：
    * ModelPixelScale(33550) + ModelTiepoint(33922)：无旋转的常规情形；
      像元原点 = 绑定点坐标减去它在栅格里的像素位置。
    * ModelTransformation(34264)：4x4 行主序矩阵，带旋转/翻转时只能用它
      （y 轴朝上的栅格也落在这里）。
    """
    transform = _floats(entry.get("transform"), 16)
    if transform:
        return (transform[3], transform[0], transform[1],
                transform[7], transform[4], transform[5])

    scale = _floats(entry.get("pixel_scale"), 3)
    tie = _floats(entry.get("tie_point"), 6)
    if scale and tie and scale[0] and scale[1]:
        i, j, _k, x, y, _z = tie
        return (x - i * scale[0], scale[0], 0.0,
                y + j * scale[1], 0.0, -scale[1])
    return None


def _corners(gt: Sequence[float], width: int, height: int):
    for px, py in ((0, 0), (width, 0), (width, height), (0, height)):
        yield (gt[0] + px * gt[1] + py * gt[2], gt[3] + px * gt[4] + py * gt[5])


def _perimeter(gt: Sequence[float], width: int, height: int):
    """栅格四条边上的加密采样点（原生坐标），见 _DENSIFY_STEPS。"""
    box = ((0, 0), (width, 0), (width, height), (0, height))
    for i in range(4):
        x0, y0 = box[i]
        x1, y1 = box[(i + 1) % 4]
        for s in range(_DENSIFY_STEPS):
            f = s / _DENSIFY_STEPS
            px, py = x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
            yield (gt[0] + px * gt[1] + py * gt[2],
                   gt[3] + px * gt[4] + py * gt[5])


def _wrap_lons(lons: Sequence[float]) -> Optional[List[float]]:
    """跨 180° 时把西半球的经度 +360，不跨就返回 None。

    osr 把经度规整进 [-180, 180]，横跨 180° 的栅格（UTM 60N/1S、楚科奇、
    斐济、太平洋 DEM）角点是 179.5 与 -179.6，直接 min/max 得到 359 度宽的
    「几乎全球」范围。判据不能只看「原始跨度 > 180」：真正的全球栅格
    （-180..180）也满足它，补完 360 反而塌成一个假范围。所以再要求
    「补完之后跨度收进 180 度以内」—— 只有真跨越才做得到。
    """
    if max(lons) - min(lons) <= 180:
        return None
    shifted = [lon + 360.0 if lon < 0 else lon for lon in lons]
    return shifted if max(shifted) - min(shifted) <= 180 else None


def _bounds_from_points(points) -> Optional[Tuple[float, float, float, float, bool]]:
    """[(lon, lat)] -> (w, s, e, n, 是否跨 180°)。points 为 None 或含非有限值时返回 None。

    非有限值必须在这里被挡掉：osr 的 TransformPoint 在点落到源坐标系有效域外时
    返回 HUGE_VAL 而不是抛异常，而 flask 的 jsonify 用 allow_nan=True —— 响应体里
    会出现裸 Infinity，浏览器的 JSON.parse 直接拒收，卡片一片空白且没有报错。
    """
    if not points:
        return None
    if not all(math.isfinite(v) for p in points for v in p[:2]):
        return None
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    wrapped = _wrap_lons(lons)
    if wrapped is not None:
        return min(wrapped), min(lats), max(wrapped), max(lats), True
    return min(lons), min(lats), max(lons), max(lats), False


def _geokey_int(geo_keys: Mapping[str, Any], key: str) -> Optional[int]:
    """GeoKeyDirectory 里的一个整数键（前端可能用字符串也可能用整数下标）。"""
    code = _number(geo_keys.get(key, geo_keys.get(int(key))))
    return None if code is None else int(code)


def _epsg_from_geokeys(geo_keys: Any) -> Optional[int]:
    """GeoKeyDirectory 里的 EPSG 码。32767 是「用户自定义」，等于没有。

    必须先看 GTModelTypeGeoKey(1024)：投影栅格上 GeographicType(2048) **永远**
    在，它说的是投影所基于的大地坐标系，不是像素单位。拿它当栅格的 EPSG，米级
    东北坐标就会被当成经纬度解释，凭空报出一个自信的错范围且不带任何警告 ——
    国产 GIS 导出的自定义 Albers/兰勃特/高斯克吕格 DEM 正是 1024=1 + 3072=32767。
    投影(1)/地心(3)只认 3072，3072 不可用就是 unknown_crs：诚实的「不知道」
    好过一个错的范围。
    """
    if not isinstance(geo_keys, Mapping):
        return None
    model = _geokey_int(geo_keys, "1024")
    for key in ("3072",) if model in (1, 3) else ("3072", "2048"):
        code = _geokey_int(geo_keys, key)
        if code is not None and 0 < code < 32767:
            return code
    return None


def _geokey_citation(geo_keys: Any) -> Optional[str]:
    """坐标系的文字说明（GTCitation/GeogCitation），GDAL 缺席时的兜底名称。"""
    if not isinstance(geo_keys, Mapping):
        return None
    for key in ("1026", "3073", "2049"):
        value = geo_keys.get(key, geo_keys.get(int(key)))
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_CITATION_CHARS]
    return None


class _Crs:
    """一个坐标系的解释结果。GDAL 缺席时除 epsg/name 外全是 None。

    gdal_missing 区分「服务端没装 GDAL」与「这个 EPSG 解不开」：两者退化后的
    形状一样，但说给用户听的话必须不同 —— 把后者说成前者，等于让一个装得
    好好的用户去查一个健康的安装。
    """

    __slots__ = ("epsg", "name", "geographic", "unit", "unit_scale",
                 "gdal_missing", "_transform")

    def __init__(self, epsg, name, geographic, unit, unit_scale, transform,
                 gdal_missing=False):
        self.epsg = epsg
        self.name = name
        self.geographic = geographic
        self.unit = unit
        self.unit_scale = unit_scale
        self.gdal_missing = gdal_missing
        self._transform = transform

    def to_wgs84(self, points):
        """[(x, y)] -> [(lon, lat)]；没有 GDAL 或转换失败返回 None。"""
        if self._transform is None:
            return None
        try:
            return [tuple(self._transform.TransformPoint(x, y)[:2]) for x, y in points]
        except Exception as e:      # pragma: no cover - osr 内部异常
            logger.warning("坐标转换到 WGS84 失败: %s", e)
            return None

    def transform_bounds(self, minx, miny, maxx, maxy):
        """原生外接矩形 -> (w, s, e, n, 是否跨 180°)，边界按 _DENSIFY_STEPS 加密。

        GDAL >= 3.4 才有 TransformBounds；老绑定（3.0-3.3）返回 None，由调用方
        退回「自己加密四条边逐点转」那条路。跨 180° 时 osr 返回 xmin > xmax，
        东边界补 360 才是真实跨度。
        """
        fn = getattr(self._transform, "TransformBounds", None)
        if fn is None:
            return None
        try:
            out = fn(minx, miny, maxx, maxy, _DENSIFY_STEPS)
        except Exception as e:      # pragma: no cover - osr 内部异常
            logger.warning("TransformBounds 失败: %s", e)
            return None
        if not out or len(out) < 4 or not all(math.isfinite(v) for v in out[:4]):
            return None
        west, south, east, north = out[:4]
        if west > east:
            return west, south, east + 360.0, north, True
        return west, south, east, north, False


def _resolve_crs(epsg: Optional[int], citation: Optional[str]) -> _Crs:
    if epsg is None:
        return _Crs(None, citation, None, None, None, None)
    try:
        from osgeo import osr
    except Exception:
        # 唯一一条「真的没有 GDAL」的路径，下面几条都是「GDAL 在但解不开」
        return _Crs(epsg, citation, None, None, None, None, gdal_missing=True)

    try:
        src = osr.SpatialReference()
        if src.ImportFromEPSG(epsg) != 0:
            return _Crs(epsg, citation, None, None, None, None)
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        geographic = bool(src.IsGeographic())
        if geographic:
            unit, unit_scale = "degree", 1.0
        else:
            unit = src.GetLinearUnitsName() or "metre"
            unit_scale = src.GetLinearUnits() or 1.0
        tgt = osr.SpatialReference()
        tgt.ImportFromEPSG(4326)
        tgt.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        return _Crs(epsg, src.GetName() or citation, geographic, unit, unit_scale,
                    osr.CoordinateTransformation(src, tgt))
    except Exception as e:
        logger.warning("解析 EPSG:%s 失败: %s", epsg, e)
        return _Crs(epsg, citation, None, None, None, None)


def _estimate_maxzoom(mode: str, pixel_deg: Optional[float],
                      pixel_3857_m: Optional[float]) -> Optional[int]:
    """按源像素尺寸估算「不填层级时会切到第几级」。

    两条管线的算法不同，必须分开算，否则给出的建议与实际跑出来的对不上：
    * 高程切片走 Cesium 的经纬度分块（GeographicTilingScheme.estimate_max_level，
      比的是 180/(tile_size-1) 度的顶点间距）；
    * 等高线走 Web Mercator 瓦片（contour_task_manager.estimate_max_zoom，
      比的是 3857 下的米/像素，还多给一级过采样保证线条平滑）。
      zoom_min 传 0 是刻意的：这里报的是「数据本身撑得住第几级」，
      而不是把用户当前填的最小层级夹进来。

    cesiumlab_terrain 模块级 import osgeo，缺 GDAL 时导入即失败 —— 那就不给
    建议（此时本来也切不了片）。
    """
    if mode == "contour":
        if not pixel_3857_m or pixel_3857_m <= 0:
            return None
        from src.services.contour_task_manager import estimate_max_zoom
        return estimate_max_zoom(pixel_3857_m, MIN_ZOOM)
    if not pixel_deg or pixel_deg <= 0:
        return None
    try:
        from src.services.terrain_tiling.cesiumlab_terrain import GeographicTilingScheme
    except Exception:
        return None
    level = GeographicTilingScheme(tile_size=_TERRAIN_TILE_SIZE).estimate_max_level(pixel_deg)
    return max(MIN_ZOOM, min(MAX_ZOOM, level))


def _describe_one(entry: Mapping[str, Any], mode: str) -> Dict[str, Any]:
    name = entry.get("name")
    result: Dict[str, Any] = {
        # 下面几个都直接来自浏览器。文件名不封顶等于让人往信息卡里塞任意长的
        # 一行；size 会经 JSON 进 JS，超过 2**53 之后就不再是它自己了；宽高越过
        # TIFF 自己的量级只可能是把字节读串了 —— 这三种都当头部读坏处理。
        "name": name[:_MAX_NAME_CHARS] if isinstance(name, str) else "",
        "size": 0,
        "warnings": [],
    }

    size = _number(entry.get("size")) or 0.0
    if not 0 <= size <= _MAX_JS_SAFE_INT:
        result["warnings"].append("header_unreadable")
        return result
    result["size"] = int(size)

    width = _number(entry.get("width")) or 0.0
    height = _number(entry.get("height")) or 0.0
    if not (0 < width <= _MAX_DIMENSION and 0 < height <= _MAX_DIMENSION):
        result["warnings"].append("header_unreadable")
        return result
    width, height = int(width), int(height)
    result["width"] = width
    result["height"] = height

    bits = int(_number(entry.get("bits")) or 0)
    fmt = int(_number(entry.get("sample_format")) or 1)
    result["dtype"] = _DTYPE_NAMES.get((fmt, bits)) or (f"{bits}-bit" if bits else None)
    result["bands"] = int(_number(entry.get("samples")) or 1)
    result["nodata"] = _number(entry.get("nodata"))
    result["compression"] = _COMPRESSION_NAMES.get(
        int(_number(entry.get("compression")) or 1))
    result["big_tiff"] = bool(entry.get("big_tiff"))

    stats = entry.get("statistics")
    if isinstance(stats, Mapping):
        lo, hi = _number(stats.get("min")), _number(stats.get("max"))
        if lo is not None and hi is not None:
            result["elevation"] = {"min": lo, "max": hi}

    if result["bands"] > 1:
        # 切片只吃第 1 波段（DemSampler.GetRasterBand(1)），多波段不是错但要说清楚
        result["warnings"].append("multi_band")

    gt = _geotransform(entry)
    epsg = _epsg_from_geokeys(entry.get("geo_keys"))
    if gt is None or epsg is None:
        # 缺任意一半都切不了片：没有 geotransform 不知道在哪，没有坐标系不知道
        # 按什么解释这些数字。GDAL 打开时会当成「像素坐标」，切出来落在几内亚湾。
        result["warnings"].append("no_georeference" if gt is None else "unknown_crs")
        return result

    corners = list(_corners(gt, width, height))
    if not all(math.isfinite(v) for xy in corners for v in xy):
        # 有限的像元大小 × 有限的宽高照样能溢出成 inf。jsonify 是 allow_nan=True，
        # 裸 Infinity 会让浏览器的 JSON.parse 整体拒收，卡片一片空白且不报错。
        result["warnings"].append("header_unreadable")
        return result

    if gt[2] or gt[4]:
        result["warnings"].append("rotated")

    crs = _resolve_crs(epsg, _geokey_citation(entry.get("geo_keys")))
    result["epsg"] = crs.epsg
    result["crs_name"] = crs.name
    result["crs_unit"] = crs.unit

    xs = [x for x, _ in corners]
    ys = [y for _, y in corners]
    result["bounds_native"] = [min(xs), min(ys), max(xs), max(ys)]
    result["pixel_size"] = [abs(gt[1]), abs(gt[5])]

    box = crs.transform_bounds(min(xs), min(ys), max(xs), max(ys))
    if box is None:
        # 老绑定（GDAL 3.0-3.3）没有 TransformBounds：自己把四条边加密后逐点转
        box = _bounds_from_points(crs.to_wgs84(list(_perimeter(gt, width, height))))
    if box is None:
        # 换算不出经纬度：原生范围照报，但估不出层级。「没装 GDAL」和「这个
        # EPSG 解不开」必须分开说 —— 混成前者会让装得好好的用户去查安装。
        # 两个码写在 .append( 同一行：test_tif_info_frontend 的两侧对账按行扫。
        result["warnings"].append("gdal_unavailable" if crs.gdal_missing else "crs_unresolved")
        return result

    west, south, east, north, crossed = box
    result["bounds_wgs84"] = [west, south, east, north]
    if crossed:
        # 东边界已经补过 360（见 _wrap_lons），界面上要说清这不是笔误
        result["warnings"].append("antimeridian")

    center_lat = (south + north) / 2.0
    cos_lat = max(1e-6, math.cos(math.radians(center_lat)))
    if crs.geographic:
        # 层级估算用 x 向度像素：切片管线的 DemSampler.pixel_size_deg 就是 abs(gt[1])。
        pixel_deg = abs(gt[1])
        # 显示用南北向米数（一度纬度处处 ≈111 km）。这才是 DEM 惯常的标称分辨率
        # ——1 弧秒 DEM 就该显示 30 m；换成东西向会随纬度缩水成 23 m，用户认不出。
        result["pixel_meters"] = abs(gt[5]) * _M_PER_DEG
        # 等高线按 EPSG:3857 计层级，口径抄 contour_task_manager._finest_pixel_size_3857：
        # x 向像素在 3857 下 ≈ deg*111320（与纬度无关），y 向还要除以 cos(纬度)。
        pixel_3857 = min(abs(gt[1]) * _M_PER_DEG,
                         abs(gt[5]) * _M_PER_DEG / cos_lat)
    else:
        pixel_m = abs(gt[1]) * (crs.unit_scale or 1.0)
        result["pixel_meters"] = pixel_m
        # 重投影到 4326 之后的度像素：gdalwarp 按变换后的经度跨度均分列数，
        # 所以东西向米数要先除以 cos(纬度) 才折算成度。
        pixel_deg = pixel_m / (_M_PER_DEG * cos_lat)
        # 投影坐标系的像素在 3857 下放大 1/cos(中心纬度)（同上，抄同一份口径）
        pixel_3857 = min(abs(gt[1]), abs(gt[5])) * (crs.unit_scale or 1.0) / cos_lat
        result["warnings"].append("reprojected")

    result["pixel_deg"] = pixel_deg
    result["pixel_3857"] = pixel_3857
    result["recommended_maxzoom"] = _estimate_maxzoom(mode, pixel_deg, pixel_3857)
    return result


def describe_headers(entries: Sequence[Mapping[str, Any]],
                     mode: str = "terrain") -> Dict[str, Any]:
    """逐个解释前端读出的 GeoTIFF 头部，并给出多文件合并后的总览。

    entries 的每一项是 static/js/geotiff_meta.js 的 read() 返回值。
    mode 决定「建议最大层级」按哪条管线算："terrain"（高程切片，Cesium
    经纬度分块）或 "contour"（等高线，Web Mercator 瓦片）—— 见 _estimate_maxzoom。
    单个文件读坏了就带着 header_unreadable 警告出现在结果里，不影响其他文件；
    输入本身不合法时抛 InspectError（带 i18n 键，由路由翻译后回给浏览器）。
    """
    if mode not in ("terrain", "contour"):
        raise InspectError("api.raster.unknown_mode")
    if not isinstance(entries, (list, tuple)):
        raise InspectError("api.raster.files_not_a_list")
    if not entries:
        raise InspectError("api.raster.no_files")
    if len(entries) > MAX_INSPECT_FILES:
        raise InspectError("api.raster.too_many_files", max=MAX_INSPECT_FILES)

    files = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise InspectError("api.raster.entry_not_object")
        files.append(_describe_one(entry, mode))

    total_size = sum(f.get("size") or 0 for f in files)
    bounds = [f["bounds_wgs84"] for f in files if f.get("bounds_wgs84")]
    # 最细的那个像素决定合并后的建议层级：切片是按并集切的，粗的那份被拉伸，
    # 细的那份不能被牺牲
    pixels = [f["pixel_deg"] for f in files if f.get("pixel_deg")]
    pixels_3857 = [f["pixel_3857"] for f in files if f.get("pixel_3857")]
    epsgs = {f["epsg"] for f in files if f.get("epsg")}

    summary: Dict[str, Any] = {
        "count": len(files),
        "total_size": total_size,
        "warnings": [],
    }
    if bounds:
        # 并集同样要过 180° 这一关：两个各自不跨界的文件（179..180 与
        # -180..-179）合起来是跨界的，naive min/max 会把 1 度报成 359 度。
        wests = [b[0] for b in bounds]
        easts = [b[2] for b in bounds]
        wrapped = _wrap_lons(wests + easts)
        if wrapped is not None:
            wests, easts = wrapped[:len(wests)], wrapped[len(wests):]
            summary["warnings"].append("antimeridian")
        summary["bounds_wgs84"] = [
            min(wests), min(b[1] for b in bounds),
            max(easts), max(b[3] for b in bounds),
        ]
    if pixels:
        summary["pixel_deg"] = min(pixels)
        summary["recommended_maxzoom"] = _estimate_maxzoom(
            mode, min(pixels), min(pixels_3857) if pixels_3857 else None)
        meters = [f["pixel_meters"] for f in files if f.get("pixel_meters")]
        if meters:
            summary["pixel_meters"] = min(meters)
    if len(epsgs) > 1:
        # 不是致命错误（切片会把每个文件各自 warp），但用户多半是选错了文件
        summary["warnings"].append("mixed_crs")
    if any("no_georeference" in f["warnings"] or "unknown_crs" in f["warnings"]
           or "header_unreadable" in f["warnings"] for f in files):
        summary["warnings"].append("some_unusable")

    return {"files": files, "summary": summary}
