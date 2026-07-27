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


def test_tile_geotransform_corners_match_tile_bounds():
    """geotransform 的左上角必须落在瓦片理论边界上（这一点当前实现就是对的）"""
    engine = DownloadEngine()
    tile = Tile(task_id=0, zoom=10, x=843, y=387)

    gt, epsg = engine.tile_geotransform(tile, 256, 256)

    assert len(gt) == 6, "geotransform 必须是 6 元组"
    assert gt[2] == 0 and gt[4] == 0, "north-up 影像的旋转项必须为 0"
    assert gt[5] < 0, "pixel_height 必须为负（图像 y 向下）"
    assert isinstance(epsg, int)

    n = 2 ** 10
    assert gt[0] == pytest.approx(843 / n * 360.0 - 180.0)               # 左上经度
    assert gt[3] == pytest.approx(_tile_lat(387, 10))                     # 左上纬度
    assert gt[0] + gt[1] * 256 == pytest.approx(844 / n * 360.0 - 180.0)  # 右边界经度
    assert gt[3] + gt[5] * 256 == pytest.approx(_tile_lat(388, 10))       # 下边界纬度


def test_tile_geotransform_interior_pixel_latitude_is_accurate():
    """
    瓦片内部像素的纬度必须与 Web Mercator 真值一致。

    当前实现把 (lat_max - lat_min) 线性均分给 height 行像素，但瓦片纵向
    是 Mercator y 等间隔,对应纬度是 atan(sinh(...)) 曲线。误差在瓦片
    中部最大、上下边界为 0。
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
