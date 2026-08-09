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



def _fake_sampler_cls(pixel_deg, bounds=(116.0, 39.0, 116.1, 39.1)):
    """最小可用的 DemSampler 替身：build_terrain 只用它的 pixel_size_deg 与 bounds。"""
    class _S:
        def __init__(self, path, nodata=None):
            self.pixel_size_deg = pixel_deg
            self.bounds = bounds
            self.ds = None
            self.band = None
    return _S


def _run_build(monkeypatch, tmp_path, **kw):
    """跑 build_terrain 但不真切瓦片：替掉 sampler 与 worker，只看层级决策。"""
    from src.services.terrain_tiling import cesiumlab_terrain as ct

    monkeypatch.setattr(ct, "DemSampler", _fake_sampler_cls(180.0 / (64 * 2 ** 10)))
    monkeypatch.setattr(ct, "_worker_tile", lambda task: (0.0, 1.0, "grid"))
    return ct.build_terrain(["fake.tif"], str(tmp_path), tile_size=65, workers=1, **kw)


def test_level_offset_shifts_the_estimated_base(monkeypatch, tmp_path):
    """未传 max_level 时，基准 = estimate_max_level，偏移叠加在它上面。"""
    assert _run_build(monkeypatch, tmp_path / "a")["max_level"] == 10
    assert _run_build(monkeypatch, tmp_path / "b", level_offset=1)["max_level"] == 11
    assert _run_build(monkeypatch, tmp_path / "c", level_offset=-1)["max_level"] == 9


def test_level_offset_shifts_an_explicit_base(monkeypatch, tmp_path):
    """显式传了 max_level 时，它就是基准，偏移同样叠加。"""
    assert _run_build(monkeypatch, tmp_path / "d", max_level=14)["max_level"] == 14
    assert _run_build(monkeypatch, tmp_path / "e", max_level=14,
                      level_offset=-1)["max_level"] == 13


def test_level_offset_is_clamped_into_the_valid_range(monkeypatch, tmp_path):
    """负偏移不得把层级压到 0 以下，正偏移不得越过 MAX_ZOOM。"""
    assert _run_build(monkeypatch, tmp_path / "f", max_level=0,
                      level_offset=-1)["max_level"] == 0
    assert _run_build(monkeypatch, tmp_path / "g", max_level=21,
                      level_offset=1)["max_level"] == 21


def test_min_level_is_clamped_below_the_effective_max(monkeypatch, tmp_path):
    """min_level > max_level 会让 _tile_ranges 产出空区间 —— 切 0 张却报 completed。

    调用方按基准层级算 min_level（dem_task_tiler 恒传 8），下调偏移后基准可能
    低于 8，钳位必须在 build_terrain 里做，调用方不知道最终层级。
    """
    r = _run_build(monkeypatch, tmp_path / "h", min_level=8, max_level=5)
    assert r["max_level"] == 5
    assert r["total"] > 0, "min_level 未被钳到 max_level 以下，产出了空区间"