"""
Tile georeference geotransform tests
"""

import os
import sys
import math
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.download_engine import DownloadEngine
from models.task import Tile


def _tile_lat(y_tile_float: float, zoom: int) -> float:
    """逆墨卡托：给定连续的瓦片 y 坐标，返回其真实纬度（度）"""
    n = 2 ** zoom
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y_tile_float / n))))


def _tile_extent_3857(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    """
    独立推导瓦片在 EPSG:3857 下的理论范围，返回 (min_x, min_y, max_x, max_y)。

    推导路径刻意与实现不同：
      - 这里走「瓦片索引 → 经纬度（slippy map 标准公式）→ 正向墨卡托投影 → 米」
      - 实现走「瓦片索引 → 3857 平面坐标线性映射」

    两条路径不共用任何算式，所以两者吻合才构成对实现的独立验证。
    """
    R = 6378137.0  # WGS84 长半轴，Web Mercator 的球半径
    n = 2 ** zoom

    lon_west = x / n * 360.0 - 180.0
    lon_east = (x + 1) / n * 360.0 - 180.0
    lat_north = _tile_lat(y, zoom)
    lat_south = _tile_lat(y + 1, zoom)

    def merc_x(lon: float) -> float:
        return R * math.radians(lon)

    def merc_y(lat: float) -> float:
        return R * math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))

    return merc_x(lon_west), merc_y(lat_south), merc_x(lon_east), merc_y(lat_north)


def test_tile_geotransform_corners_match_tile_bounds():
    """
    不变量：geotransform 描述的栅格范围 == 瓦片的理论地理范围，且与像素尺寸无关。

    ⚠️ 护栏说明 —— 修改本测试前必读：
    期望值来自 `_tile_extent_3857`，它经由「经纬度 + 正向墨卡托」独立推导，
    **不是**复读 `tile_geotransform` 内部的 origin / tile_span 算式。
    如果本测试将来变红，请先怀疑实现，不要把实现里的表达式抄进期望值 ——
    那会让实现和测试同错同绿，这条护栏就废了。

    容差 1e-6 m（微米级）远小于任何真实配准误差，但足以容纳
    sinh/atan/log 往返的浮点噪声。
    """
    engine = DownloadEngine()
    zoom, x, y = 10, 843, 387
    tile = Tile(task_id=0, zoom=zoom, x=x, y=y)

    gt, epsg = engine.tile_geotransform(tile, 256, 256)

    assert len(gt) == 6, "geotransform 必须是 6 元组"
    assert gt[2] == 0 and gt[4] == 0, "north-up 影像的旋转项必须为 0"
    assert gt[5] < 0, "pixel_height 必须为负（图像 y 向下）"
    assert isinstance(epsg, int)
    # Task 4 的重投影依赖这个契约：瓦片以 3857 平面坐标写出
    assert epsg == 3857, "瓦片必须以 EPSG:3857 平面坐标配准"

    min_x, min_y, max_x, max_y = _tile_extent_3857(zoom, x, y)

    assert gt[0] == pytest.approx(min_x, abs=1e-6)                    # 左边界
    assert gt[3] == pytest.approx(max_y, abs=1e-6)                    # 上边界
    assert gt[0] + gt[1] * 256 == pytest.approx(max_x, abs=1e-6)      # 右边界
    assert gt[3] + gt[5] * 256 == pytest.approx(min_y, abs=1e-6)      # 下边界

    # 非方形尺寸：范围必须不变，只有像素大小随尺寸缩放。
    # width != height 时才能抓到 pixel_width / pixel_height 算式里
    # width 与 height 互换这类错误 —— 256x256 下这种错误是隐形的。
    gt_wide, epsg_wide = engine.tile_geotransform(tile, 512, 256)

    assert epsg_wide == epsg
    assert gt_wide[0] == pytest.approx(min_x, abs=1e-6)               # 左边界
    assert gt_wide[3] == pytest.approx(max_y, abs=1e-6)               # 上边界
    assert gt_wide[0] + gt_wide[1] * 512 == pytest.approx(max_x, abs=1e-6)   # 右边界
    assert gt_wide[3] + gt_wide[5] * 256 == pytest.approx(min_y, abs=1e-6)   # 下边界
    assert gt_wide[1] == pytest.approx(gt[1] / 2, abs=1e-9), "宽度翻倍则像素宽减半"
    assert gt_wide[5] == pytest.approx(gt[5], abs=1e-9), "高度不变则像素高不变"


def test_tile_geotransform_interior_pixel_latitude_is_accurate():
    """
    瓦片内部像素的纬度必须与 Web Mercator 真值一致。

    这条测试锁的是曾经的缺陷：旧实现把 (lat_max - lat_min) 线性均分给
    height 行像素并当成 EPSG:4326 写出，但瓦片纵向是 Mercator y 等间隔,
    对应纬度是 atan(sinh(...)) 曲线。四角坐标对、中间像素全错，误差在
    瓦片中部最大、上下边界为 0 —— z10/y387 上峰值 14.8 m。

    改用 3857 平面坐标后走下面的 epsg == 3857 分支，残余偏差只剩浮点噪声
    （实测约 4e-9 m）。若哪天这条又红了，说明配准数学被改坏了。
    """
    engine = DownloadEngine()
    zoom, x, y = 10, 843, 387          # 北京附近
    height = 256
    tile = Tile(task_id=0, zoom=zoom, x=x, y=y)

    gt, epsg = engine.tile_geotransform(tile, 256, height)
    top_left_y, pixel_height = gt[3], gt[5]

    max_err_deg = 0.0
    for row in range(height + 1):
        if epsg == 4326:
            got_lat = top_left_y + row * pixel_height
        elif epsg == 3857:
            merc_y = top_left_y + row * pixel_height
            got_lat = math.degrees(
                2 * math.atan(math.exp(merc_y / 6378137.0)) - math.pi / 2
            )
        else:
            raise AssertionError(f"未预期的 EPSG: {epsg}")

        true_lat = _tile_lat(y + row / height, zoom)
        max_err_deg = max(max_err_deg, abs(got_lat - true_lat))

    max_err_m = max_err_deg * 111320.0
    assert max_err_m < 1.0, (
        f"瓦片内像素纬度最大偏差 {max_err_m:.2f} m,超过 1 m 容差。"
        f"(zoom={zoom}, y={y})"
    )


def test_adjacent_tiles_are_seamless_with_uniform_pixel_size():
    """
    跨瓦片邻接不变量：相邻瓦片边界严丝合缝，且所有瓦片像素尺寸完全相同。

    这正是改用 3857 所宣称的收益（见 tile_geotransform docstring:
    "every pixel exactly the same size, so BuildVRT can mosaic them
    losslessly"）。旧的 4326 线性近似实现里 pixel_height 随纬度变化，
    不同纬度带的瓦片拼在一起会错缝——本测试就是那条缺陷的回归护栏。

    ⚠️ 边界期望值取自 `_tile_extent_3857`（独立推导），不是复读实现。
    """
    engine = DownloadEngine()
    zoom, x, y = 10, 843, 387
    size = 256

    gt_a, _ = engine.tile_geotransform(Tile(task_id=0, zoom=zoom, x=x, y=y), size, size)
    gt_right, _ = engine.tile_geotransform(Tile(task_id=0, zoom=zoom, x=x + 1, y=y), size, size)
    gt_below, _ = engine.tile_geotransform(Tile(task_id=0, zoom=zoom, x=x, y=y + 1), size, size)

    # 横向邻接：瓦片 (x,y) 的右边界 == 瓦片 (x+1,y) 的左边界
    right_edge_of_a = gt_a[0] + gt_a[1] * size
    assert right_edge_of_a == pytest.approx(gt_right[0], abs=1e-6), "横向接缝错位"
    assert gt_right[0] == pytest.approx(_tile_extent_3857(zoom, x + 1, y)[0], abs=1e-6)

    # 纵向邻接：瓦片 (x,y) 的下边界 == 瓦片 (x,y+1) 的上边界
    bottom_edge_of_a = gt_a[3] + gt_a[5] * size
    assert bottom_edge_of_a == pytest.approx(gt_below[3], abs=1e-6), "纵向接缝错位"
    assert gt_below[3] == pytest.approx(_tile_extent_3857(zoom, x, y + 1)[3], abs=1e-6)

    # 像素尺寸与纬度无关：从赤道到高纬，pixel_width / pixel_height 必须逐字节一致。
    # 旧实现在这里会随 y 变化（这就是 14.8 m 缺陷的第二层表现）。
    for other_y in (0, 1, 100, 387, 512, 1023):
        gt_o, _ = engine.tile_geotransform(Tile(task_id=0, zoom=zoom, x=x, y=other_y), size, size)
        assert gt_o[1] == pytest.approx(gt_a[1], abs=1e-9), f"y={other_y} 的 pixel_width 与 y={y} 不同"
        assert gt_o[5] == pytest.approx(gt_a[5], abs=1e-9), f"y={other_y} 的 pixel_height 与 y={y} 不同"

    # 3857 下像素是正方形
    assert gt_a[1] == pytest.approx(-gt_a[5], abs=1e-9), "3857 瓦片像素必须是正方形"


def test_stitch_default_target_epsg_is_4326():
    """默认输出必须仍是 EPSG:4326,不能静默改变存量用户的产出"""
    import inspect
    from services.download_engine import DownloadEngine

    sig = inspect.signature(DownloadEngine.stitch_tiles_with_gdal)
    assert 'target_epsg' in sig.parameters, "stitch_tiles_with_gdal 应有 target_epsg 参数"
    assert sig.parameters['target_epsg'].default == 4326, "默认输出坐标系必须保持 4326"


# --------------------------------------------------------------------------
# 拼接输出坐标系 + 配准中间文件（_geo*.tif）治理
# --------------------------------------------------------------------------

def _write_png_tile(path, size: int = 64, value: int = 128) -> None:
    """在 path 写一张真实可被 GDAL 打开的 PNG，模拟下载好的瓦片缓存"""
    from osgeo import gdal

    path.parent.mkdir(parents=True, exist_ok=True)
    mem = gdal.GetDriverByName('MEM').Create('', size, size, 3, gdal.GDT_Byte)
    for band_idx in range(1, 4):
        mem.GetRasterBand(band_idx).Fill(value)
    png = gdal.GetDriverByName('PNG').CreateCopy(str(path), mem)
    assert png is not None, f"无法写入测试瓦片 {path}"
    png = None
    mem = None


def _epsg_of(dataset_path) -> str:
    """读出 GDAL 数据集的 EPSG 代码（字符串）"""
    from osgeo import gdal, osr

    ds = gdal.Open(str(dataset_path))
    assert ds is not None, f"无法打开 {dataset_path}"
    srs = osr.SpatialReference(wkt=ds.GetProjection())
    code = srs.GetAuthorityCode(None)
    ds = None
    return code


def test_stale_4326_geo_tif_is_never_reused(tmp_path, monkeypatch):
    """
    存量用户盘上已有的 4326 `_geo.tif` 残骸，绝不能被当成 3857 瓦片复用。

    背景：`_add_georeference` 有一条 exists() 短路；而拼接时任意一个瓦片
    缺失就会 raise，导致此前生成的配准中间文件全留在**跨任务共享**的
    cache 目录里。坐标系从 4326 改成 3857 之后，那些残骸的投影是错的，
    一旦被短路复用就会和真正的 3857 瓦片混进同一个 VRT。

    对策：中间文件名带上坐标系标记，新代码只认带标记的文件。
    """
    from config import Config

    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')

    engine = DownloadEngine()
    zoom, x, y = 10, 843, 387
    tile = Tile(task_id=0, zoom=zoom, x=x, y=y)
    tile_png = tile.cache_path('m')
    _write_png_tile(tile_png)

    # 造一份存量用户盘上真实存在的 4326 残骸
    from osgeo import gdal, osr
    legacy = tile_png.with_name(f"{tile_png.stem}_geo.tif")
    stale = gdal.GetDriverByName('GTiff').Create(str(legacy), 64, 64, 3, gdal.GDT_Byte)
    stale.SetGeoTransform([116.0, 0.001, 0.0, 40.0, 0.0, -0.001])
    stale_srs = osr.SpatialReference()
    stale_srs.ImportFromEPSG(4326)
    stale.SetProjection(stale_srs.ExportToWkt())
    stale = None
    assert legacy.exists()

    georef_path = engine._add_georeference(str(tile_png), tile)

    assert Path(georef_path) != legacy, "4326 残骸被当成配准中间文件复用了"
    assert _epsg_of(georef_path) == '3857', "配准中间文件必须是 EPSG:3857"
    assert not legacy.exists(), "旧的 4326 残骸应被顺手清掉，避免长期占盘"


def test_stitch_cleans_up_georef_tiles_when_stitching_fails(tmp_path, monkeypatch):
    """
    拼接失败时也必须清掉已生成的配准中间文件。

    缓存缺瓦片（用户清了缓存 / 上次没跑完）会在循环里 raise
    FileNotFoundError，此前处理过的瓦片如果没有 try/finally 就全成了残骸。
    """
    from config import Config

    cache_dir = tmp_path / 'cache'
    out_dir = tmp_path / 'downloads'
    monkeypatch.setattr(Config, 'CACHE_DIR', cache_dir)
    monkeypatch.setattr(Config, 'OUTPUT_DIR', out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = DownloadEngine()
    zoom, x, y = 10, 843, 387
    present = Tile(task_id=0, zoom=zoom, x=x, y=y)
    missing = Tile(task_id=0, zoom=zoom, x=x + 1, y=y)
    _write_png_tile(present.cache_path('m'))

    with pytest.raises(FileNotFoundError):
        engine.stitch_tiles_with_gdal(
            tiles=[present, missing],
            style='m',
            output_path=str(out_dir / 'boom.tif'),
            zoom_level=zoom,
        )

    leftovers = sorted(str(p) for p in cache_dir.rglob('*_geo*.tif'))
    assert leftovers == [], f"拼接失败后残留了配准中间文件: {leftovers}"


def test_stitch_reprojects_to_4326_by_default_and_keeps_3857_on_request(tmp_path, monkeypatch):
    """
    端到端跑一遍真实的 GDAL 拼接链路（_add_georeference → BuildVRT → Warp → Translate）：

      - 默认产出必须是 EPSG:4326（存量行为不变），且像素近似方形
      - target_epsg=3857 时跳过 warp，直接输出原生 3857、范围与瓦片理论范围逐米吻合
      - 两种路径都不能留下任何中间文件
    """
    from config import Config
    from osgeo import gdal

    cache_dir = tmp_path / 'cache'
    out_dir = tmp_path / 'downloads'
    monkeypatch.setattr(Config, 'CACHE_DIR', cache_dir)
    monkeypatch.setattr(Config, 'OUTPUT_DIR', out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = DownloadEngine()
    zoom, x, y = 10, 843, 387
    tiles = [
        Tile(task_id=0, zoom=zoom, x=x, y=y),
        Tile(task_id=0, zoom=zoom, x=x + 1, y=y),
    ]
    for idx, tile in enumerate(tiles):
        _write_png_tile(tile.cache_path('m'), value=40 + idx * 60)

    # --- 默认：4326 ---
    out_4326 = out_dir / 'default.tif'
    engine.stitch_tiles_with_gdal(tiles, 'm', str(out_4326), zoom)

    assert out_4326.exists()
    assert _epsg_of(out_4326) == '4326', "默认产出坐标系必须是 EPSG:4326"

    ds = gdal.Open(str(out_4326))
    gt = ds.GetGeoTransform()
    raster_w, raster_h = ds.RasterXSize, ds.RasterYSize
    ds = None

    # ⚠️ 不要在这里断言 pixel_width == |pixel_height|。
    # gdal.Warp 不给 xRes/yRes 时走 GDALSuggestedWarpOutput,该算法用
    # 「变换后影像对角线长度 ÷ 源影像对角线像素数」算出**一个各向同性的**
    # 像素尺寸给 x/y 共用,所以 4326 输出的像素在度单位下**必然**严格方形——
    # 那是 GDAL 内部的算术恒等式,warp 算错了它也照样成立,不构成任何证据。
    # 真正能证伪的是下面这几条边界断言:它们把产出的地理范围钉死在
    # 瓦片的理论经纬度边界上,期望值独立推导(slippy map 经度公式 +
    # _tile_lat 墨卡托反算),不复读实现。
    west = x / 2 ** zoom * 360.0 - 180.0
    east = (x + 2) / 2 ** zoom * 360.0 - 180.0   # 两块瓦片,所以到 x+2
    north = _tile_lat(y, zoom)
    south = _tile_lat(y + 1, zoom)

    # 左/上边界:warp 的输出范围锚在源范围的角点上,是逐位精确的
    assert gt[0] == pytest.approx(west, abs=1e-9), "4326 产出左边界应等于瓦片西经"
    assert gt[3] == pytest.approx(north, abs=1e-6), "4326 产出上边界应等于瓦片北纬(墨卡托反算)"

    # 右/下边界:GDAL 把栅格尺寸向上取整成整数像素,所以最多差一个像素。
    # 容差取一个像素宽——仍足以抓住 outputBounds / srcSRS 写错这类整体错位
    # (那会差几十上百个像素),但不会因为取整噪声误报。
    one_pixel = abs(gt[1])
    assert gt[0] + gt[1] * raster_w == pytest.approx(east, abs=one_pixel), "4326 产出右边界越界超过一个像素"
    assert gt[3] + gt[5] * raster_h == pytest.approx(south, abs=one_pixel), "4326 产出下边界越界超过一个像素"

    # --- 显式 3857：跳过 warp，无重采样 ---
    out_3857 = out_dir / 'native.tif'
    engine.stitch_tiles_with_gdal(tiles, 'm', str(out_3857), zoom, target_epsg=3857)

    assert _epsg_of(out_3857) == '3857'
    ds = gdal.Open(str(out_3857))
    gt_native = ds.GetGeoTransform()
    ds = None

    min_x, _, _, max_y = _tile_extent_3857(zoom, x, y)
    _, _, right_x, _ = _tile_extent_3857(zoom, x + 1, y)
    assert gt_native[0] == pytest.approx(min_x, abs=1e-6), "3857 直出左边界应等于瓦片理论边界"
    assert gt_native[3] == pytest.approx(max_y, abs=1e-6), "3857 直出上边界应等于瓦片理论边界"
    assert gt_native[0] + gt_native[1] * 128 == pytest.approx(right_x, abs=1e-6), "3857 直出右边界应等于瓦片理论边界"

    # --- 中间文件必须清干净 ---
    assert sorted(str(p) for p in cache_dir.rglob('*_geo*.tif')) == []
    assert sorted(str(p) for p in out_dir.rglob('*.vrt')) == []
