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
