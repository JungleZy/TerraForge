"""逐顶点法线与 ghost cells 的测试。

不需要 GDAL：法线是纯几何计算，直接喂经纬度/高程数组；碰到切片流程的几条
用假 sampler（monkeypatch DemSampler）绕开 GDAL。
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.services.terrain_tiling.cesiumlab_terrain as ct
from src.services.terrain_tiling.cesiumlab_terrain import (
    GeographicTilingScheme,
    _tile_normals,
    _vertex_normals_ecef,
    lonlat_to_ecef,
)


def _grid(lon0, lat0, span, n, heights):
    lons, lats = np.meshgrid(np.linspace(lon0, lon0 + span, n),
                             np.linspace(lat0, lat0 + span, n))
    return lons, lats, heights


def test_flat_terrain_normals_point_straight_up():
    """水平面上的法线必须与地心方向一致（ECEF 空间，不是经纬度空间）。"""
    n = 9
    lons, lats, h = _grid(86.0, 41.0, 0.01, n, np.full((n, n), 1000.0))
    nrm = _vertex_normals_ecef(lons, lats, h)
    up = lonlat_to_ecef(lons, lats, np.zeros_like(h))
    up = up / np.linalg.norm(up, axis=-1, keepdims=True)
    cos = (nrm * up).sum(-1)
    assert np.allclose(cos, 1.0, atol=1e-6)


def test_normals_are_unit_length():
    n = 9
    rng = np.random.default_rng(4)
    lons, lats, h = _grid(86.0, 41.0, 0.01, n, rng.random((n, n)) * 500.0)
    nrm = _vertex_normals_ecef(lons, lats, h)
    assert np.allclose(np.linalg.norm(nrm, axis=-1), 1.0, atol=1e-9)


def test_normals_always_point_outward():
    """不论三角形绕序如何，法线必须朝外（与地心方向点积为正）。"""
    n = 9
    rng = np.random.default_rng(6)
    lons, lats, h = _grid(86.0, 41.0, 0.01, n, rng.random((n, n)) * 900.0)
    nrm = _vertex_normals_ecef(lons, lats, h)
    xyz = lonlat_to_ecef(lons, lats, h)
    up = xyz / np.linalg.norm(xyz, axis=-1, keepdims=True)
    assert ((nrm * up).sum(-1) > 0).all()


def test_outward_fix_survives_a_north_to_south_grid():
    """纬度递减的网格喂进来，法线仍须朝外 —— 钉住那行朝外修正本身。

    上一条测试的网格纬度是递增的，此时累加的叉积**本来就**朝外，那行
    `out[(out*up).sum(-1) < 0] *= -1` 是 no-op，删掉它全量测试一条都不红
    （实测：62 passed）。而它并非可有可无 —— 纬度递减正是 GDAL 的天然行序，
    真有人按那个方向建网格时，实测法线**整体翻转**（dot 从 +1.0 变 -1.0），
    Cesium 开光照后整块地形的明暗会反过来。

    docstring 里写的「不依赖三角形绕序」这个承诺，靠的就是这行修正；
    RTIN 的绕向在本轮已经被改过一次（dad22bd77），别让法线再被同一件事绊一遍。
    """
    n = 9
    # 纬度从北到南递减：v2 - v0 指向南而非北，叉积因此整体反向
    lons, lats = np.meshgrid(np.linspace(86.0, 86.01, n),
                             np.linspace(41.01, 41.0, n))
    h = np.full((n, n), 1000.0)
    nrm = _vertex_normals_ecef(lons, lats, h)
    xyz = lonlat_to_ecef(lons, lats, h)
    up = xyz / np.linalg.norm(xyz, axis=-1, keepdims=True)
    dot = (nrm * up).sum(-1)
    assert (dot > 0).all(), f"纬度递减时法线朝内了，dot 最小 {dot.min():.4f}"


def test_slope_tilts_normal_towards_downhill():
    """东高西低的斜坡，法线应朝西倾斜（东向分量为负）。"""
    n = 9
    xx = np.arange(n, dtype=float)
    h = np.tile(xx * 100.0, (n, 1))          # 沿经度递增
    lons, lats, h = _grid(86.0, 41.0, 0.01, n, h)
    nrm = _vertex_normals_ecef(lons, lats, h)
    lo = np.radians(86.005)
    east = np.array([-np.sin(lo), np.cos(lo), 0.0])
    assert (nrm[n // 2, n // 2] * east).sum() < 0


def _decode_high_water_mark(encoded):
    """把 _mesh_constants 的 high-water-mark 索引流解回原始三角形索引。

    解码要在 uint32 模 2^32 下做：编码写的是 highest - index，而网格索引流并非
    按首现顺序递增（第三个索引就是 n，> highest=2），差值为负时按 uint32 回绕。
    """
    out, highest = [], 0
    for code in encoded:
        idx = (highest - int(code)) % (1 << 32)
        out.append(idx)
        if idx == highest:
            highest += 1
    return np.array(out, dtype=np.int64)


def _reference_normals(lons, lats, heights, weighted):
    """独立参考实现：三角形表**从 _mesh_constants 的索引流解出**，逐片显式循环。

    不抄实现的向量化切片，也不自己重新推导切法 —— 三角形来源是生产网格自己写进
    字节流的那一份，因此这条参考同时钉住「法线用的切法 = 落盘网格的切法」。
    """
    n = lons.shape[0]
    tris = _decode_high_water_mark(ct._mesh_constants(n)[2]).reshape(-1, 3)
    xyz = lonlat_to_ecef(lons, lats, heights).reshape(-1, 3)
    acc = np.zeros_like(xyz)
    for a, b, c in tris:
        face = np.cross(xyz[b] - xyz[a], xyz[c] - xyz[a])
        if not weighted:                      # 面积权重丢掉 = 简单平均
            face = face / np.linalg.norm(face)
        acc[a] += face
        acc[b] += face
        acc[c] += face
    return (acc / np.linalg.norm(acc, axis=-1, keepdims=True)).reshape(n, n, 3)


def test_normals_are_area_weighted_not_a_simple_average():
    """权重必须是未归一化叉积（=2×面积），不是简单平均。

    面积加权在这里不是审美问题：陡坎三角形的面积比相邻平坦三角形大两个量级，
    简单平均会让一个几乎不存在的平坦小片和整面陡坎等权。判据不抄实现 ——
    参考实现从 _mesh_constants 的索引流解三角形、逐片显式累加。
    实测这块 3000 m 陡坎上两种权重最大差 52.4°，分辨力充足。
    """
    n = 5
    lons, lats = np.meshgrid(np.linspace(86.0, 86.01, n),
                             np.linspace(41.0, 41.01, n))
    h = np.zeros((n, n))
    h[:, 2] = 3000.0                          # 一列高墙：邻接三角形面积悬殊

    got = _vertex_normals_ecef(lons, lats, h)
    weighted = _reference_normals(lons, lats, h, weighted=True)
    simple = _reference_normals(lons, lats, h, weighted=False)

    def ang(a, b):
        return np.degrees(np.arccos(np.clip((a * b).sum(-1), -1.0, 1.0)))

    assert ang(got, weighted).max() < 1e-4, "与面积加权参考不符"
    assert ang(got, simple).max() > 5.0, "与简单平均无法区分 —— 面积权重丢了"


def test_ghost_ring_eliminates_seam_between_adjacent_tiles():
    """核心断言：用 ghost 环算出的边界法线，在相邻瓦片两侧必须完全相同。

    A 的东边界与 B 的西边界是同一条经线上的同样一组点。各自扩一圈后，
    两侧该处的邻接三角形集合完全一致，法线因此逐位相同。
    """
    n = 9
    span = 0.01
    step = span / (n - 1)

    def terrain(lon, lat):
        return 500.0 + 300.0 * np.sin(lon * 60.0) + 200.0 * np.cos(lat * 40.0)

    def ghost_normals(lon0, lat0):
        lons, lats = np.meshgrid(
            np.linspace(lon0 - step, lon0 + span + step, n + 2),
            np.linspace(lat0 - step, lat0 + span + step, n + 2))
        nrm = _vertex_normals_ecef(lons, lats, terrain(lons, lats))
        return nrm[1:-1, 1:-1]

    a = ghost_normals(86.0, 41.0)
    b = ghost_normals(86.0 + span, 41.0)
    assert np.array_equal(a[:, -1], b[:, 0]), "ghost 修正后接缝未归零"


def test_without_ghost_ring_seam_exists():
    """对照组：不扩环时接缝必然存在 —— 证明上一个测试不是恒真。"""
    n = 9
    span = 0.01

    def terrain(lon, lat):
        return 500.0 + 300.0 * np.sin(lon * 60.0) + 200.0 * np.cos(lat * 40.0)

    def plain_normals(lon0, lat0):
        lons, lats = np.meshgrid(np.linspace(lon0, lon0 + span, n),
                                 np.linspace(lat0, lat0 + span, n))
        return _vertex_normals_ecef(lons, lats, terrain(lons, lats))

    a = plain_normals(86.0, 41.0)
    b = plain_normals(86.0 + span, 41.0)
    assert not np.allclose(a[:, -1], b[:, 0])


# ---------------------------------------------------------------------------
# 接线：_tile_normals / _worker_tile / build_terrain
#
# 上面六条全都直接调 _vertex_normals_ecef，一行都没碰切片流程 —— 也就是说
# 「ghost 环没扩」「裁切下标写错」「with_normals 没透传」这三种失败在上面
# 一条都红不了（实测：内联版本下三个变异全部静默存活、全量测试全绿）。
# 本项目的失败形态历来是静默的（作业 completed + 前端不报错 + 什么都没有），
# 所以接线必须自己有守卫。
# ---------------------------------------------------------------------------

def _terrain(lon, lat):
    """起伏足够大的解析地形：z11 瓦片跨度内 sin 项走过约 5 弧度。"""
    return 500.0 + 300.0 * np.sin(lon * 60.0) + 200.0 * np.cos(lat * 40.0)


class _AnalyticSampler:
    """假 DemSampler：解析地形 + 记录每次采样的网格，不需要 GDAL。"""

    def __init__(self, *a, **k):
        self.calls = []
        self.bounds = (85.0, 41.0, 87.0, 43.0)
        self.pixel_size_deg = 1.0 / 3600.0
        self.ds = object()
        self.band = object()

    def sample(self, lons, lats):
        self.calls.append((np.asarray(lons), np.asarray(lats)))
        return _terrain(lons, lats)


def _plain_normals(west, south, east, north, n):
    """不扩环的对照：直接在瓦片自己的 n×n 网格上算。"""
    lo, la = np.meshgrid(np.linspace(west, east, n, dtype=np.float64),
                         np.linspace(south, north, n, dtype=np.float64))
    return _vertex_normals_ecef(lo, la, _terrain(lo, la))


def test_ghost_ring_samples_exactly_one_step_beyond_the_tile():
    """ghost 网格必须是 (n+2)²，且第 1 圈/倒数第 1 圈精确落在瓦片边界上。

    扩得不对（比如扩半格、或扩到邻瓦片中心）时接缝不归零，但没有任何现成
    断言会红 —— 法线此刻还没有消费者。
    """
    n = 17
    west, south, east, north = GeographicTilingScheme(tile_size=n).tile_extent_deg(5, 20, 9)
    s = _AnalyticSampler()
    _tile_normals(s, west, south, east, north, n)

    assert len(s.calls) == 1
    glon, glat = s.calls[0]
    assert glon.shape == (n + 2, n + 2)
    step_lon = (east - west) / (n - 1)
    step_lat = (north - south) / (n - 1)
    assert glon[0, 1] == pytest.approx(west, abs=1e-12)
    assert glon[0, -2] == pytest.approx(east, abs=1e-12)
    assert glon[0, 0] == pytest.approx(west - step_lon, abs=1e-12)
    assert glon[0, -1] == pytest.approx(east + step_lon, abs=1e-12)
    assert glat[1, 0] == pytest.approx(south, abs=1e-12)
    assert glat[-2, 0] == pytest.approx(north, abs=1e-12)
    assert glat[0, 0] == pytest.approx(south - step_lat, abs=1e-12)
    assert glat[-1, 0] == pytest.approx(north + step_lat, abs=1e-12)


def test_ghost_crop_keeps_the_tile_itself_not_a_shifted_window():
    """裁切必须是 [1:-1, 1:-1]。

    判据不抄实现：ghost 环**只影响边界一圈**，内部顶点的邻接三角形集合与不扩环
    时完全相同，因此内部必须逐位吻合（错位裁切会整体平移一行/一列，当场差几度），
    而边界必须与不扩环时不同（否则 ghost 根本没起作用）。
    """
    n = 17
    west, south, east, north = GeographicTilingScheme(tile_size=n).tile_extent_deg(5, 20, 9)
    got = _tile_normals(_AnalyticSampler(), west, south, east, north, n)
    plain = _plain_normals(west, south, east, north, n)

    assert got.shape == (n, n, 3)
    assert np.allclose(got[1:-1, 1:-1], plain[1:-1, 1:-1], atol=1e-12), \
        "内部顶点与不扩环版不吻合 —— 裁切窗口错位了"
    for edge in (np.s_[0, :], np.s_[-1, :], np.s_[:, 0], np.s_[:, -1]):
        assert not np.allclose(got[edge], plain[edge]), \
            "边界与不扩环版一模一样 —— ghost 环没起作用"


@pytest.mark.parametrize("scheme_args", [(5, 20, 9), (8, 300, 120)])
def test_adjacent_tiles_agree_bitwise_on_the_shared_edge(scheme_args):
    """核心保证的接线版：两张真实相邻瓦片，公共边上的法线逐位相同。

    ⚠️ 别把这条读成生产保证。它用的 _AnalyticSampler 是**位置的纯函数**，而生产的
    DemSampler.sample 不是（读窗口随传入网格变、再降采样），所以真实 DEM 上只有
    「网格比源像素密、不降采样」的最深层才真逐位归零，中间层只是被压小一个量级。
    实测数字与机制见 _tile_normals 的 docstring。这条钉的是**本函数自己那一半**：
    ghost 环扩对了、裁切窗口没错位 —— 采样器那一半它管不着。
    """
    n = 17
    z, x, y = scheme_args
    scheme = GeographicTilingScheme(tile_size=n)
    a = _tile_normals(_AnalyticSampler(), *scheme.tile_extent_deg(z, x, y), n)
    b = _tile_normals(_AnalyticSampler(), *scheme.tile_extent_deg(z, x + 1, y), n)
    assert np.array_equal(a[:, -1], b[:, 0]), "东西相邻瓦片的公共边法线不一致"

    c = _tile_normals(_AnalyticSampler(), *scheme.tile_extent_deg(z, x, y + 1), n)
    assert np.array_equal(a[-1, :], c[0, :]), "南北相邻瓦片的公共边法线不一致"

    # 对照：不扩环时同样两条边必然不同，证明上面不是恒真
    pa = _plain_normals(*scheme.tile_extent_deg(z, x, y), n)
    pb = _plain_normals(*scheme.tile_extent_deg(z, x + 1, y), n)
    assert not np.allclose(pa[:, -1], pb[:, 0])


@pytest.mark.parametrize("with_normals,expected_samples", [(True, 2), (False, 1)])
def test_worker_tile_samples_the_ghost_ring_only_when_normals_enabled(
        tmp_path, monkeypatch, with_normals, expected_samples):
    """with_normals 必须真的到达 worker：开时多一次 (n+2)² 采样，关时一次都不多。

    透传断了的话法线永远算不出来，而作业照样 completed、瓦片照样落盘 —— 正是
    本项目栽过五次的静默失败形态。
    """
    n = 17
    sampler = _AnalyticSampler()
    monkeypatch.setattr(ct, "_WORKER_SAMPLER", sampler)
    west, south, east, north = GeographicTilingScheme(tile_size=n).tile_extent_deg(5, 20, 9)

    result = ct._worker_tile((5, 20, 9, west, south, east, north, n, str(tmp_path),
                              "auto", 0.15, with_normals))

    assert result is not None
    assert (tmp_path / "5" / "20" / "9.terrain").is_file()
    assert len(sampler.calls) == expected_samples
    shapes = {c[0].shape for c in sampler.calls}
    if with_normals:
        assert shapes == {(n, n), (n + 2, n + 2)}
    else:
        assert shapes == {(n, n)}


@pytest.mark.parametrize("flag", [True, False])
def test_build_terrain_threads_the_normals_flag_into_every_task(tmp_path, monkeypatch, flag):
    """build_terrain(normals=...) 必须原样进到每个任务元组的末位。"""
    seen = []

    def fake_worker(task):
        seen.append(task)
        return (0.0, 1.0, "grid")

    monkeypatch.setattr(ct, "DemSampler", _AnalyticSampler)
    monkeypatch.setattr(ct, "_worker_tile", fake_worker)
    ct.build_terrain(["fake.tif"], str(tmp_path), min_level=0, max_level=1,
                     tile_size=17, workers=1, normals=flag)

    assert seen, "一张瓦片都没派发"
    assert {t[-1] for t in seen} == {flag}


def test_build_terrain_turns_normals_on_by_default(tmp_path, monkeypatch):
    """默认必须是开 —— 设计稿定的是「法线数据无条件写入，开关只在 Cesium 渲染端」。"""
    seen = []
    monkeypatch.setattr(ct, "DemSampler", _AnalyticSampler)
    monkeypatch.setattr(ct, "_worker_tile", lambda t: seen.append(t) or (0.0, 1.0, "grid"))
    ct.build_terrain(["fake.tif"], str(tmp_path), min_level=0, max_level=1,
                     tile_size=17, workers=1)
    assert {t[-1] for t in seen} == {True}
