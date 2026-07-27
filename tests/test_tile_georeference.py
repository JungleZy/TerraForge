"""
Tile georeference geotransform tests
"""

import os
import sys
import math

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
