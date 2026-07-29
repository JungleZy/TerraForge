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


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """把 Config 的所有落盘路径**和数据库**都指向 tmp_path,然后建库。

    为什么 DATABASE_PATH 必须一起 patch(CLAUDE.md 的测试规约就是这一条):
    `DownloadEngine()` 持有一个 `ConfigManager`,拼接链路会读 `gdal_resampling`
    / `gdal_compression`。只 patch CACHE_DIR/OUTPUT_DIR 的话,配置读的是仓库里
    **真实的** data/map_downloader.db ——
      - 开发机上那个库存在,gdal_resampling = 'cubic' → 跑 cubic 重采样
      - 干净 checkout / CI 上那个库不存在,ConfigManager.get 吞掉异常返回
        download_engine 里的兜底值 'nearest' → 跑 nearest
    同一份测试在两处跑的是**不同的**代码路径,这种测试的绿是没有意义的。

    建库(而不是只把路径指向一个不存在的文件)是为了让取到的值是生产真实默认值:
    config 表由 database.DEFAULT_CONFIGS 播种,gdal_resampling = 'cubic'。
    """
    from config import Config
    import database

    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'config.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')

    # init_database() 内部会 Config.init_app() 建目录,所以上面四条必须先生效
    database.init_database()


def test_resampling_config_is_the_production_default():
    """守住上面那个 fixture 的目的:测试跑的必须是生产默认的重采样算法。

    这条测试存在的意义是「兜底值 nearest 和真实默认 cubic 不一致」这个坑:
    如果哪天 fixture 里的建库被删掉,ConfigManager 会静默退回 'nearest',
    上面所有拼接测试立刻改跑另一条代码路径而**不会变红**。这条会红。
    """
    from database import DEFAULT_CONFIGS
    from services.config_manager import ConfigManager

    defaults = dict(DEFAULT_CONFIGS)
    assert defaults['gdal_resampling'] == 'cubic', (
        "生产默认重采样算法变了,download_engine.py 里那条『真实默认是 cubic』"
        "的注释要同步更新"
    )
    assert ConfigManager().get('gdal_resampling', 'nearest') == 'cubic', (
        "测试库里读不到 cubic,说明 isolated_config 没建库 —— 拼接测试正在跑 "
        "nearest 兜底值,和生产不是同一条路径"
    )


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


# --------------------------------------------------------------------------
# 调色板（PNG8）瓦片必须展开成真彩色 RGB
# --------------------------------------------------------------------------

# 取自真实 Google roadmap 瓦片的几种典型配色。刻意让每个条目的 R/G/B
# 三个分量互不相同 —— 这样「通道顺序搞成 BGR」「三个通道都写成同一个
# 波段」这类错误才会被逐像素断言抓到；若用灰阶调色板（R==G==B）则隐形。
_ROADMAP_PALETTE = [
    (0, 0, 0),           # 索引 0：黑（文字描边）
    (145, 147, 148),     # 路面灰（实测 roadmap 里真实存在的一档）
    (241, 243, 244),     # 底图浅灰
    (163, 204, 255),     # 水体蓝
    (252, 214, 164),     # 高速公路橙
    (200, 30, 40),       # 标注红
]

# 第二块瓦片用一套**完全不同**的调色板，模拟实测事实：相邻的 Google
# roadmap 瓦片各自带不同的调色板（实测 167 / 137 / 119 色，前 137 个索引
# 里 132 个颜色不同）。索引矩阵与第一块一致，所以只要哪一块被用了别人的
# 调色板解码，颜色断言立刻红。
_ROADMAP_PALETTE_ALT = [
    (255, 255, 255),
    (60, 61, 62),
    (12, 34, 56),
    (255, 128, 0),
    (0, 176, 80),
    (99, 7, 201),
]


def _write_palette_png_tile(path, palette, size: int = 16):
    """
    在 path 写一张真实的 PNG8 调色板瓦片（1 波段索引 + color table）。

    返回索引矩阵（list[list[int]]），供调用方逐像素核对颜色。
    """
    import numpy as np
    from osgeo import gdal

    path.parent.mkdir(parents=True, exist_ok=True)

    indices = [[(row + col) % len(palette) for col in range(size)] for row in range(size)]

    mem = gdal.GetDriverByName('MEM').Create('', size, size, 1, gdal.GDT_Byte)
    color_table = gdal.ColorTable()
    for idx, (r, g, b) in enumerate(palette):
        color_table.SetColorEntry(idx, (r, g, b, 255))
    band = mem.GetRasterBand(1)
    band.SetRasterColorTable(color_table)
    band.SetRasterColorInterpretation(gdal.GCI_PaletteIndex)
    band.WriteArray(np.array(indices, dtype=np.uint8))

    png = gdal.GetDriverByName('PNG').CreateCopy(str(path), mem)
    assert png is not None, f"无法写入调色板测试瓦片 {path}"
    png = None
    mem = None

    # 自检：写出来的 PNG 必须真的是「1 波段 + 调色板」，否则这条测试就没在
    # 测它想测的东西（GDAL 的 PNG driver 若哪天默默展开成 RGB，测试会变成空壳）。
    check = gdal.Open(str(path))
    assert check.RasterCount == 1, "测试素材不是单波段，PNG8 前提已失效"
    assert check.GetRasterBand(1).GetRasterColorTable() is not None, \
        "测试素材没有调色板，PNG8 前提已失效"
    check = None

    return indices


def test_palette_tile_is_expanded_to_true_rgb(tmp_path):
    """
    调色板瓦片必须在配准阶段就地展开成 3 波段真彩色。

    缺陷：`_add_georeference` 逐波段 ReadAsArray/WriteArray 复制像素，从不碰
    color table。PNG8 的 roadmap 瓦片只有 1 个波段、装的是**调色板索引**，
    复制出来调色板就丢了，任何软件打开都是「把索引当灰度显示」，颜色全错。

    为什么不是「顺手把 color table 也复制过去」：相邻 Google 瓦片各自带
    不同的调色板，BuildVRT 只能保留一份，其余瓦片会按错的调色板解码 ——
    产出「颜色鲜艳但地物语义错乱」，比灰度图更危险，因为它看起来像对的。
    所以必须在瓦片彼此相遇之前，各自用自己的调色板解算成 RGB。

    断言逐像素比对颜色，不只数波段个数 —— 只数波段的话，「展开成 3 波段但
    通道顺序错」或「三个波段都塞索引值」都能骗过测试。
    """
    from osgeo import gdal

    engine = DownloadEngine()
    zoom, x, y = 10, 843, 387
    tile = Tile(task_id=0, zoom=zoom, x=x, y=y)

    tile_png = tmp_path / 'palette_tile.png'
    indices = _write_palette_png_tile(tile_png, _ROADMAP_PALETTE, size=16)

    georef_path = engine._add_georeference(str(tile_png), tile)

    ds = gdal.Open(georef_path)
    assert ds is not None, f"配准中间文件打不开: {georef_path}"
    assert ds.RasterCount == 3, (
        f"调色板瓦片应展开成 3 波段 RGB，实际 {ds.RasterCount} 波段"
        "（1 波段说明调色板被丢弃，产物是索引值当灰度）"
    )
    assert ds.GetRasterBand(1).GetRasterColorTable() is None, \
        "展开后不应再残留 color table"

    arr = ds.ReadAsArray()
    ds = None

    size = len(indices)
    for row in range(size):
        for col in range(size):
            expected = _ROADMAP_PALETTE[indices[row][col]]
            actual = tuple(int(arr[band][row][col]) for band in range(3))
            assert actual == expected, (
                f"像素 ({row},{col}) 索引 {indices[row][col]} 的颜色应为 "
                f"{expected}，实际 {actual}"
            )

    # 配准逻辑不能被展开顺手改坏
    assert _epsg_of(georef_path) == '3857', "配准中间文件必须仍是 EPSG:3857"


def test_rgb_tile_passes_through_georeference_unchanged(tmp_path):
    """
    卫星图（3 波段 RGB、无调色板）路径必须零变化。

    这是当前生产中唯一被真正使用的路径（用户缓存里 43 万张全是卫星图），
    调色板展开的改动绝不能顺带把它改坏 —— 既不能改波段数，也不能动像素值。
    """
    import numpy as np
    from osgeo import gdal

    engine = DownloadEngine()
    zoom, x, y = 10, 843, 387
    tile = Tile(task_id=0, zoom=zoom, x=x, y=y)

    # 三个波段值刻意不同，这样「通道顺序被打乱」也会被抓到；
    # 再叠一个随行变化的梯度，避免纯色图掩盖空间错位（翻转 / 平移）。
    size = 16
    base = np.array(
        [[row * 3 + col for col in range(size)] for row in range(size)],
        dtype=np.uint8,
    )
    channels = [base, (base + 60).astype(np.uint8), (base + 120).astype(np.uint8)]

    tile_png = tmp_path / 'satellite_tile.png'
    mem = gdal.GetDriverByName('MEM').Create('', size, size, 3, gdal.GDT_Byte)
    for band_idx, data in enumerate(channels, start=1):
        mem.GetRasterBand(band_idx).WriteArray(data)
    png = gdal.GetDriverByName('PNG').CreateCopy(str(tile_png), mem)
    assert png is not None
    png = None
    mem = None

    georef_path = engine._add_georeference(str(tile_png), tile)

    ds = gdal.Open(georef_path)
    assert ds is not None
    assert ds.RasterCount == 3, f"RGB 瓦片波段数应保持 3，实际 {ds.RasterCount}"
    for band_idx, expected in enumerate(channels, start=1):
        actual = ds.GetRasterBand(band_idx).ReadAsArray()
        assert np.array_equal(actual, expected), (
            f"波段 {band_idx} 的像素值被改动了（RGB 路径必须逐像素不变）"
        )
    ds = None

    assert _epsg_of(georef_path) == '3857', "配准中间文件必须仍是 EPSG:3857"


def test_stitched_palette_tiles_keep_each_tiles_own_colors(tmp_path, monkeypatch):
    """
    端到端：两块**调色板互不相同**的瓦片拼在一起，各自颜色都必须正确。

    这条锁的是「复制 color table」那条死路：VRT 只能带一份调色板，若实现
    走了复制路线，两块瓦片里必然有一块被按错的调色板解码。走 target_epsg
    =3857 跳过 warp，像素原封不动穿过 BuildVRT → Translate，所以可以逐像素
    比对颜色。
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
    size = 16
    left = Tile(task_id=0, zoom=zoom, x=x, y=y)
    right = Tile(task_id=0, zoom=zoom, x=x + 1, y=y)

    indices = _write_palette_png_tile(left.cache_path('m'), _ROADMAP_PALETTE, size=size)
    indices_right = _write_palette_png_tile(
        right.cache_path('m'), _ROADMAP_PALETTE_ALT, size=size
    )
    assert indices == indices_right, "两块瓦片索引应相同，才能证伪『用了别人的调色板』"

    out_path = out_dir / 'palette_mosaic.tif'
    engine.stitch_tiles_with_gdal([left, right], 'm', str(out_path), zoom, target_epsg=3857)

    ds = gdal.Open(str(out_path))
    assert ds is not None
    assert ds.RasterCount == 3, f"拼接产物应为 3 波段 RGB，实际 {ds.RasterCount}"
    assert (ds.RasterXSize, ds.RasterYSize) == (size * 2, size), \
        f"拼接尺寸应为 {size * 2}x{size}，实际 {ds.RasterXSize}x{ds.RasterYSize}"
    arr = ds.ReadAsArray()
    ds = None

    for row in range(size):
        for col in range(size):
            left_expected = _ROADMAP_PALETTE[indices[row][col]]
            right_expected = _ROADMAP_PALETTE_ALT[indices[row][col]]
            left_actual = tuple(int(arr[band][row][col]) for band in range(3))
            right_actual = tuple(int(arr[band][row][col + size]) for band in range(3))
            assert left_actual == left_expected, (
                f"左瓦片像素 ({row},{col}) 应为 {left_expected}，实际 {left_actual}"
            )
            assert right_actual == right_expected, (
                f"右瓦片像素 ({row},{col}) 应为 {right_expected}，实际 {right_actual}"
                "（若等于左瓦片的颜色，说明两块瓦片共用了同一份调色板）"
            )

    assert sorted(str(p) for p in cache_dir.rglob('*_geo*.tif')) == []
    assert sorted(str(p) for p in out_dir.rglob('*.vrt')) == []


def test_stale_single_band_intermediate_is_never_reused(tmp_path, monkeypatch):
    """
    改动前形态的 1 波段中间文件，绝不能被 exists() 短路复用。

    `_add_georeference` 在中间文件已存在时直接返回，不重新打开校验内容 ——
    也就是说它完全信任文件名。展开调色板改变了中间文件的**内容契约**
    （像素从调色板索引变成颜色），文件名却没跟着变的话：残留一份旧的
    1 波段中间文件，下次 roadmap 拼接就会静默复用它，颜色又错回去。

    残留是真实场景：中间文件虽然在 finally 里删，但 unlink 失败只打 warning
    不抛错（Windows 文件占用足够触发），而缓存目录跨任务共享、升级也从不清理。

    这与 Phase 1 的 `_geo.tif` 残骸问题同构：那次是坐标系变更把一个一直无害
    的复用逻辑升级成了有害，这次是波段形态。对策也同构 —— 把契约编进文件名。
    """
    import numpy as np
    from config import Config
    from osgeo import gdal, osr

    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')

    engine = DownloadEngine()
    zoom, x, y = 10, 843, 387
    tile = Tile(task_id=0, zoom=zoom, x=x, y=y)
    tile_png = tile.cache_path('m')
    indices = _write_palette_png_tile(tile_png, _ROADMAP_PALETTE, size=16)

    # 造一份「改动前的代码」会产出的中间文件：EPSG:3857 配准正确，
    # 但只有 1 波段、装的是调色板索引。
    stale_path = tile_png.with_name(f"{tile_png.stem}_geo3857.tif")
    stale = gdal.GetDriverByName('GTiff').Create(str(stale_path), 16, 16, 1, gdal.GDT_Byte)
    stale.SetGeoTransform(engine.tile_geotransform(tile, 16, 16)[0])
    stale_srs = osr.SpatialReference()
    stale_srs.ImportFromEPSG(3857)
    stale.SetProjection(stale_srs.ExportToWkt())
    stale.GetRasterBand(1).WriteArray(np.array(indices, dtype=np.uint8))
    stale = None
    assert stale_path.exists()

    georef_path = engine._add_georeference(str(tile_png), tile)

    assert Path(georef_path) != stale_path, "1 波段残骸被当成中间文件复用了"

    ds = gdal.Open(georef_path)
    assert ds.RasterCount == 3, (
        f"复用把 1 波段索引图带回来了，实际 {ds.RasterCount} 波段"
    )
    # 不只数波段：颜色必须真的是展开后的 RGB。
    # 注意别只挑 (0,0) 验 —— 那里索引是 0、颜色也是 (0,0,0)，
    # 「把索引值复制进三个波段」的错误实现同样能过。逐像素扫。
    arr = ds.ReadAsArray()
    ds = None
    for row in range(len(indices)):
        for col in range(len(indices)):
            expected = _ROADMAP_PALETTE[indices[row][col]]
            actual = tuple(int(arr[band][row][col]) for band in range(3))
            assert actual == expected, (
                f"像素 ({row},{col}) 应为 {expected}，实际 {actual}"
            )

    assert not stale_path.exists(), "旧形态的中间文件应被顺手清掉，避免长期占盘"


# --------------------------------------------------------------------------
# BuildVRT 静默踢瓦片：必须显式失败，不能产出范围缩水的拼接图
#
# gdal.BuildVRT 对用不了的源文件不报错：打一条 `Warning 1: ... Skipping`
# 就继续，返回一个完全合法的数据集。于是 Translate 成功、任务报完成，
# 用户拿到一张少了几块瓦片的拼接图，只有自己去量地理范围才可能发现。
#
# 下面两条测试各自复现一种真实触发路径，都要求 raise 而不是静默缩水。
# 它们同时是 _assert_vrt_covers_tile_grid 这条尺寸校验的护栏。
# --------------------------------------------------------------------------

def _georef_path_of(tile_png: Path) -> Path:
    """中间文件路径。共用实现里的 GEOREF_SUFFIX，避免在测试里硬编码文件名"""
    from services.download_engine import GEOREF_SUFFIX

    return tile_png.with_name(f"{tile_png.stem}{GEOREF_SUFFIX}.tif")


def _private_georef_path_of(work_dir: Path, tile) -> Path:
    """stitch 私有临时目录里的中间文件路径(I8 后的位置)。

    I8 把 stitch 的中间文件从共享 cache 搬进了每次 stitch 私有的临时目录,
    且目录内命名带 x 前缀(cache 文件名只是 {y}.png,同 y 不同 x 会撞名)。
    这里共用实现里的 GEOREF_SUFFIX,镜像这套命名。
    """
    from services.download_engine import GEOREF_SUFFIX

    return work_dir / f"{tile.x}_{tile.y}{GEOREF_SUFFIX}.tif"


def _plant_poison_in_work_dir(monkeypatch, work_dir: Path):
    """让 stitch 用我们准备的目录做私有临时目录(里面已放好毒中间文件)。

    I8 之后 stitch 的中间文件写在 tempfile.mkdtemp 出来的私有目录里,测试
    无法事先知道路径;把 mkdtemp 钉到固定目录,就能在 stitch 开始前把
    「名字合规、内容有毒」的残骸放进去,复现 exists() 短路复用残骸、
    BuildVRT 静默踢瓦片的路径。行为断言(必须显式失败,不能缩水)不变。
    """
    import services.download_engine as de

    work_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(de.tempfile, 'mkdtemp', lambda prefix=None: str(work_dir))


def _two_tiles_with_cache(engine, size=16):
    """在缓存里放好左右相邻两块瓦片，返回 (left, right)"""
    zoom, x, y = 10, 843, 387
    left = Tile(task_id=0, zoom=zoom, x=x, y=y)
    right = Tile(task_id=0, zoom=zoom, x=x + 1, y=y)
    for tile in (left, right):
        _write_png_tile(tile.cache_path('m'), size=size)
    return left, right


def test_half_written_intermediate_fails_instead_of_shrinking_the_mosaic(monkeypatch, tmp_path):
    """
    「名字对、内容只写了一半」的中间文件残骸，必须让拼接显式失败。

    残骸怎么来的：`_add_georeference` 里 driver.Create() 一执行，磁盘上就已经
    有了一个叫 `<tile>_geo3857rgb.tif` 的文件。用户在此刻关掉 exe（或中途抛
    异常），留下的就是这种「文件名完全合规、内容没有 geotransform/投影」的东西。
    下次拼接时 exists() 短路信任文件名，把它直接喂给 BuildVRT。
    （I8 后中间文件在 stitch 私有临时目录里，本测试把 mkdtemp 钉到固定目录
    来投放残骸；要守的行为不变：被复用的毒中间文件必须显式失败。）

    修复分两层，这条测试守的是第二层：
      1. 原子写（.part + os.replace）—— 让这种残骸不再产生
      2. BuildVRT 之后的尺寸校验 —— 已经在盘上的历史残骸也要变成显式失败

    实测未修复时的后果（2 块瓦片 16px）：
        Warning 1: gdalbuildvrt does not support ungeoreferenced image. Skipping ...
        vrt size 16x16          # 本该 32x16，整块瓦片被踢出
    只有一条 warning，Translate 照常成功，任务报完成。
    """
    from osgeo import gdal
    from config import Config

    engine = DownloadEngine()
    left, right = _two_tiles_with_cache(engine)
    out_path = Config.OUTPUT_DIR / 'half_written.tif'

    work_dir = tmp_path / 'stitch_work'
    _plant_poison_in_work_dir(monkeypatch, work_dir)
    poison = _private_georef_path_of(work_dir, right)
    half = gdal.GetDriverByName('GTiff').Create(str(poison), 16, 16, 3, gdal.GDT_Byte)
    assert half is not None
    half = None  # 关掉：没有 SetGeoTransform / SetProjection

    # 自检：残骸必须真的是「打得开、但没有配准」，否则这条测试没在测它想测的东西
    check = gdal.Open(str(poison))
    assert check is not None, "残骸应当是能打开的（BuildVRT 才会走到 Skipping 分支）"
    assert not check.GetProjection(), "残骸不该有投影，否则复现的不是半成品场景"
    check = None

    with pytest.raises(RuntimeError, match='skipped'):
        engine.stitch_tiles_with_gdal([left, right], 'm', str(out_path), left.zoom)

    assert not out_path.exists(), (
        "尺寸校验必须在 Translate 之前拦下来，不能把缩水的拼接图落盘"
    )
    assert not work_dir.exists(), "失败路径也必须清掉中间文件(私有临时目录)"


def test_band_count_mismatch_intermediate_fails_instead_of_shrinking_the_mosaic(monkeypatch, tmp_path):
    """
    波段数不一致的中间文件残骸（如遗留的 4 波段 RGBA），同样必须显式失败。

    BuildVRT 的波段数取自**第一个**源文件，与之不符的后续源会被踢出：
        gdalbuildvrt was called with a band count of 3 but the file ... has 4 bands.
        Skipping ...
    调色板修复把 PNG8 归一成 3 波段是改善，但盘上残留的 RGBA 中间文件没有守卫 ——
    它文件名合规、配准完整，exists() 短路照样会复用它。
    （I8 后中间文件在 stitch 私有临时目录里，本测试把 mkdtemp 钉到固定目录
    来投放残骸；要守的行为不变：被复用的毒中间文件必须显式失败。）

    实测未修复时：vrt size 16x16（本该 32x16），Translate 成功，任务报完成。
    """
    from osgeo import gdal, osr
    from config import Config

    engine = DownloadEngine()
    left, right = _two_tiles_with_cache(engine)
    out_path = Config.OUTPUT_DIR / 'band_mismatch.tif'

    # 4 波段、配准完整 —— 唯一的问题就是波段数，把变量隔离干净
    work_dir = tmp_path / 'stitch_work'
    _plant_poison_in_work_dir(monkeypatch, work_dir)
    poison = _private_georef_path_of(work_dir, right)
    rgba = gdal.GetDriverByName('GTiff').Create(str(poison), 16, 16, 4, gdal.GDT_Byte)
    assert rgba is not None
    rgba.SetGeoTransform(engine.tile_geotransform(right, 16, 16)[0])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    rgba.SetProjection(srs.ExportToWkt())
    for band_idx in range(1, 5):
        rgba.GetRasterBand(band_idx).Fill(90)
    rgba = None

    check = gdal.Open(str(poison))
    assert check.RasterCount == 4, "残骸必须是 4 波段，否则复现的不是波段错配场景"
    assert check.GetProjection(), "残骸必须配准完整，否则退化成上一条测试"
    check = None

    with pytest.raises(RuntimeError, match='skipped'):
        engine.stitch_tiles_with_gdal([left, right], 'm', str(out_path), left.zoom)

    assert not out_path.exists(), "不能把缩水的拼接图落盘"
    assert not work_dir.exists(), "失败路径也必须清掉中间文件(私有临时目录)"


def test_interrupted_georeference_leaves_no_name_compliant_leftover(monkeypatch):
    """
    配准中途失败，绝不能在盘上留下一个「名字合规、内容不对」的中间文件。

    这是原子写那一层（.part + os.replace）的护栏，对应上面 half_written
    那条测试的根因：本分支的整套防线是「把内容契约编进文件名，所以 exists()
    短路可信」，而 driver.Create() 一执行文件名就已经占住了。

    这里在 SetProjection 之前引爆（osr.SpatialReference 抛异常），正好落在
    「文件已创建、配准还没写完」这个最危险的窗口里。
    """
    import services.download_engine as de

    engine = DownloadEngine()
    zoom, x, y = 10, 843, 387
    tile = Tile(task_id=0, zoom=zoom, x=x, y=y)
    tile_png = tile.cache_path('m')
    _write_png_tile(tile_png, size=16)

    class _ExplodingSRS:
        def __init__(self, *args, **kwargs):
            raise RuntimeError('killed mid-write')

    monkeypatch.setattr(de.osr, 'SpatialReference', _ExplodingSRS)

    with pytest.raises(RuntimeError, match='killed mid-write'):
        engine._add_georeference(str(tile_png), tile)

    georef = _georef_path_of(tile_png)
    assert not georef.exists(), (
        f"中途失败留下了名字合规的残骸 {georef.name} —— 下次拼接会被 exists() "
        "短路复用，BuildVRT 只打一条 warning 就把它踢出，拼接图静默缩水"
    )
    leftover_parts = sorted(p.name for p in tile_png.parent.glob('*.part.*'))
    assert leftover_parts == [], f"临时 .part 文件没清掉: {leftover_parts}"

    # 失败之后重试必须能干净地成功 —— 证明上一步没留下挡路的东西
    monkeypatch.undo()
    retried = engine._add_georeference(str(tile_png), tile)
    assert Path(retried) == georef
    assert _epsg_of(retried) == '3857'
