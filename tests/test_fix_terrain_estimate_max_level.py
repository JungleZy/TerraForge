"""M22: estimate_max_level 此前硬编码 180.0/64.0（=65 顶点网格假设），无视
self.tile_size。瓦片顶点网格实际是 tile_size x tile_size（_worker_tile 用
np.linspace(west, east, tile_size)，端点含边界），即 tile_size-1 个采样间隔
均分瓦片的 180°/2^z 纬度跨度；tile_size=17 时旧公式自动层级少算约 2 级。
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.terrain_tiling.cesiumlab_terrain import GeographicTilingScheme


def test_estimate_max_level_uses_actual_tile_size():
    scheme = GeographicTilingScheme(tile_size=17)
    # tile_size=17 -> 16 个间隔：level z 的采样间隔 = 180/(16*2^z) 度。
    # 源像素恰好等于 z=10 的间隔 -> 应返回 10。
    p = 180.0 / (16 * 2**10)
    assert scheme.estimate_max_level(p) == 10
    # 像素尺寸大一倍 -> 少一级
    assert scheme.estimate_max_level(p * 2) == 9
    # 旧硬编码 180/64 会给出 12 —— 确认不再钉在 65 顶点假设上
    assert scheme.estimate_max_level(p) != 12


def test_estimate_max_level_65_vertices_matches_legacy():
    # 65x65 顶点网格（生产 TileParams 默认）与旧行为一致
    scheme = GeographicTilingScheme(tile_size=65)
    p = 180.0 / (64 * 2**10)
    assert scheme.estimate_max_level(p) == 10


def test_estimate_max_level_degenerate_inputs():
    assert GeographicTilingScheme(tile_size=17).estimate_max_level(0) == 14
    assert GeographicTilingScheme(tile_size=17).estimate_max_level(-1.0) == 14
    # tile_size=1 不崩溃（间隔数钳到 1）
    assert GeographicTilingScheme(tile_size=1).estimate_max_level(0.5) >= 0
