"""逐顶点法线与 ghost cells 的测试。

不需要 GDAL：法线是纯几何计算，直接喂经纬度/高程数组；碰到切片流程的几条
用假 sampler（monkeypatch DemSampler）绕开 GDAL。
"""

import gzip
import json
import math
import os
import struct
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.services.terrain_tiling.cesium_terrain as ct
from src.services.terrain_tiling.cesium_terrain import (
    GeographicTilingScheme,
    _oct_encode,
    _tile_normals,
    _vertex_normals_ecef,
    encode_quantized_mesh,
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
    """build_terrain 这一层的默认必须是开。

    钉的是 **build_terrain 签名**那份默认，不是生产实际用的那份：应用侧
    dem_task_tiler.TileParams.normals 默认已改成【关】（三档预设），显式透传
    覆盖这里。留着这条是因为不显式传参的实验代码与新调用方继承的正是这一份，
    而 CLI / 全球底图也走它。
    """
    seen = []
    monkeypatch.setattr(ct, "DemSampler", _AnalyticSampler)
    monkeypatch.setattr(ct, "_worker_tile", lambda t: seen.append(t) or (0.0, 1.0, "grid"))
    ct.build_terrain(["fake.tif"], str(tmp_path), min_level=0, max_level=1,
                     tile_size=17, workers=1)
    assert {t[-1] for t in seen} == {True}


# ---------------------------------------------------------------------------
# oct 编码与 quantized-mesh 的 Oct-Encoded Per-Vertex Normals 扩展段
# （extensionId=1，每顶点 2 字节）
#
# 这一段是法线**第一次**变成落盘字节。扩展段写错在 Cesium 里几乎全是静默的：
# 解析循环（Cesium.js 的 lFt）拿到 extensionId 后是 `H=new Uint8Array(t,a,v*2)`
# —— 长度取的是 **vertexCount*2**，根本不看 extensionLength，然后 `a+=Ce` 继续
# 找下一个扩展。于是 extensionLength 写错时法线照常解出来、循环只是跳到一个错位
# 的地方，而扩展段又恰好是流的最后一段，循环 `a<byteLength` 一判就退出 ——
# **不报错、不显示异常**。extensionId 写错则整段被忽略，同样一声不吭。
# 所以这里的断言必须自己按 spec 走完整条流（下面的 _split_quantized_mesh），
# 不能指望"Cesium 不抛异常"当验收标准。
# ---------------------------------------------------------------------------

def _js_math_round(x: float) -> float:
    """JavaScript 的 `Math.round`：平局一律进位（half-up），**不是** Python /
    numpy 的 half-to-even。

    这不是吹毛求疵：`toSNorm` 的输入 t=(p*0.5+0.5)*255 真的会精确落在 k+0.5 上
    （用 nextafter 在 p=(2(k+0.5)/255)-1 附近扫，255 个 k 上共找到 2164 个这样的
    double），其中 744 个 k 是偶数 —— 那些点上 numpy 的 half-to-even 往下取、
    Cesium 往上取，差 1 个 LSB。下面 _TIE_VECTORS 就是这么找出来的。

    x 在调用处恒为 [0,255]：floor 精确，t-floor(t) 也精确（结果最多 52 位），
    所以这个写法与 ES 规范里 Math.round 的定义（"最近的整数，平局取更大的那个"）
    对 x>=0 逐位相同。
    """
    f = math.floor(x)
    return f + 1 if x - f >= 0.5 else f


def _cesium_oct_encode_scalar(vec) -> tuple[int, int]:
    """逐字转写 vendored CesiumJS 1.143.0 的 `AttributeCompression.octEncodeInRange(v, 255)`。

    源文件 `static/vendor/cesium/1.143.0/Cesium.js`（压缩产物，原文照抄）：

        octEncodeInRange=function(e,t,n){
          n.x=e.x/(Math.abs(e.x)+Math.abs(e.y)+Math.abs(e.z)),
          n.y=e.y/(Math.abs(e.x)+Math.abs(e.y)+Math.abs(e.z));
          if(e.z<0){let i=n.x,o=n.y;
            n.x=(1-Math.abs(o))*W.signNotZero(i),
            n.y=(1-Math.abs(i))*W.signNotZero(o)}
          return n.x=W.toSNorm(n.x,t),n.y=W.toSNorm(n.y,t),n}
        signNotZero=function(e){return e<0?-1:1}
        toSNorm=function(e,t){return Math.round((yt.clamp(e,-1,1)*.5+.5)*t)}
        clamp=function(e,t,n){return e<t?t:e>n?n:e}

    刻意写成标量 + 显式 if，一条对一条 —— 不抄实现的向量化布尔索引，否则这条
    参考就只是实现的镜像。转写的正确性另有外部佐证：把上面四个函数**按字面量
    从 Cesium.js 里切出来**丢进 Node 跑（不改一个字符），对 20012 个随机单位
    向量 + 2164 个平局向量与本函数逐字节一致。
    """
    x, y, z = (float(c) for c in vec)
    l1 = abs(x) + abs(y) + abs(z)
    px = x / l1
    py = y / l1
    if z < 0:                                    # e.z<0 -> 折叠到八面体外圈
        i, o = px, py
        px = (1.0 - abs(o)) * (-1.0 if i < 0 else 1.0)
        py = (1.0 - abs(i)) * (-1.0 if o < 0 else 1.0)

    def to_snorm(e):
        e = -1.0 if e < -1.0 else (1.0 if e > 1.0 else e)
        return int(_js_math_round((e * 0.5 + 0.5) * 255.0))

    return to_snorm(px), to_snorm(py)


# 精确命中 toSNorm 平局（t == k+0.5 且 k 为偶数）的**单位**向量。
# 来历：对每个偶数 k 解析求出 v=(-sinθ,0,cosθ) 使 x/L1 ≈ (2(k+0.5)/255)-1，
# 再用 np.nextafter 在 θ 附近逐 double 扫，取第一个让 t 精确等于 k+0.5 的。
# 右列是把 Cesium.js 里的 octEncodeInRange 原样丢进 Node 跑出来的结果 ——
# 半局进位（k+1）。numpy 的 np.round 在这里会给 k，差 1 个 LSB。
_TIE_VECTORS = [
    ((-0.9461639849533561, 0.0, 0.3236876790629902), (33, 128)),
    ((-0.9281245790205044, 0.0, 0.3722697487280044), (37, 128)),
    ((-0.9065820610798457, 0.0, 0.4220295801578592), (41, 128)),
    ((-0.8813220663961949, 0.0, 0.4725160476461526), (45, 128)),
    ((-0.8522134181772287, 0.0, 0.5231943136910835), (49, 128)),
]


def _oct_decode(enc):
    """spec 的解码算法（= Cesium 的 octDecodeInRange），用于往返验证。"""
    f = enc.astype(np.float64) / 255.0 * 2.0 - 1.0
    x, y = f[:, 0].copy(), f[:, 1].copy()
    z = 1.0 - np.abs(x) - np.abs(y)
    neg = z < 0
    if neg.any():
        ox, oy = x[neg].copy(), y[neg].copy()
        x[neg] = (1.0 - np.abs(oy)) * np.where(ox >= 0, 1.0, -1.0)
        y[neg] = (1.0 - np.abs(ox)) * np.where(oy >= 0, 1.0, -1.0)
    v = np.stack([x, y, z], axis=1)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def _random_unit_vectors(seed, count):
    rng = np.random.default_rng(seed)
    v = rng.normal(size=(count, 3))
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def test_oct_encode_matches_the_vendored_cesium_encoder_bit_for_bit():
    """与 Cesium 的 AttributeCompression.octEncode **逐字节**相同。

    参考实现是标量转写（见 _cesium_oct_encode_scalar 的 docstring，含 Node
    交叉验证的来历）。样本刻意覆盖三类：随机方向、六个轴向与对角（z=0 与
    z<0 的边界）、以及舍入平局向量。
    """
    axis = np.array([
        [0.0, 0.0, 1.0], [0.0, 0.0, -1.0], [1.0, 0.0, 0.0], [-1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0], [0.0, -1.0, 0.0],
        [1.0, 1.0, 1.0], [-1.0, -1.0, -1.0], [1.0, -1.0, -1.0],
        [1.0, 0.0, -1.0], [0.0, 1.0, -1.0], [-1.0, 0.0, -1.0], [0.0, -1.0, -1.0],
    ])
    axis /= np.linalg.norm(axis, axis=1, keepdims=True)
    ties = np.array([v for v, _ in _TIE_VECTORS])
    sample = np.vstack([_random_unit_vectors(20260805, 5000), axis, ties])

    got = _oct_encode(sample)
    want = np.array([_cesium_oct_encode_scalar(v) for v in sample], dtype=np.uint8)
    bad = np.where((got != want).any(axis=1))[0]
    assert len(bad) == 0, (
        f"{len(bad)}/{len(sample)} 个向量与 Cesium 不符，"
        f"首例 v={sample[bad[0]]} got={got[bad[0]]} want={want[bad[0]]}")


@pytest.mark.parametrize("vec,expected", _TIE_VECTORS)
def test_oct_encode_rounds_ties_up_like_javascript_not_half_to_even(vec, expected):
    """量化到 [0,255] 必须是 half-up，不能用 numpy 默认的 half-to-even。

    期望值不是「录当前输出」：它是把 Cesium.js 里的 octEncodeInRange 按字面量
    切出来丢进 Node 跑出来的。np.round 在这五个向量上全部低一个 LSB。
    单独列一条（而不是靠上面那条大样本）是为了让失败信息直指舍入模式。
    """
    got = _oct_encode(np.array([vec]))
    assert tuple(int(c) for c in got[0]) == expected


def test_oct_encode_roundtrip_within_quantisation_error():
    """oct16 往返误差应小于 2°（含 z<0 的下半球）。"""
    v = _random_unit_vectors(9, 500)
    assert (v[:, 2] < 0).sum() > 100, "样本里没有足够的下半球向量，测不到折叠分支"
    back = _oct_decode(_oct_encode(v))
    cos = np.clip((v * back).sum(1), -1, 1)
    assert np.degrees(np.arccos(cos)).max() < 2.0, "oct16 往返误差应小于 2°"


def test_oct_encode_output_is_two_bytes_per_vertex():
    v = np.tile(np.array([[0.0, 0.0, 1.0]]), (7, 1))
    enc = _oct_encode(v)
    assert enc.shape == (7, 2)
    assert enc.dtype == np.uint8


def test_downward_normals_are_folded_not_mirrored_onto_the_upper_hemisphere():
    """z<0 的折叠分支：删掉它，z 分量为负的法线会被解成正的。

    八面体投影只有 (x,y) 两个数，上下半球靠「|x|+|y| 是否超过 1」区分。不折叠
    时 |x|+|y|<1 恒成立，解码端算出的 z 恒为正 —— 一整个下半球被镜像到上半球，
    往返夹角最坏 180°，而 Cesium 那边只表现为"那一块的明暗反了"，不报任何错。

    ⚠️ 这里的 z 是 **ECEF 的 Z 轴**（指向北极），不是"地形朝下"：法线基本沿着
    地心方向，所以 z 的符号约等于 sin(纬度)，与坡度几乎无关。实测同一块地形
    搬到各纬度，z<0 的法线个数（33×33 网格）：lat-70 1089/1089、lat-42
    1089/1089、lat-5 958/1089、lat0 495/1089、lat+5 10/1089、**lat+42 0/1089**。
    也就是说折叠分支是**南半球专属**路径 —— 本项目手头的 DEM 全在 N28~N52
    （实测天山 z9-12 共 36 张瓦片、304200 个法线分量，z<0 的一个都没有），
    所以删掉这个分支在现有数据上**一点症状都没有**，得靠这条测试守。
    """
    down = np.array([[0.0, 0.0, -1.0],
                     [0.3, 0.2, -0.9327379053088816],
                     [-0.6, 0.5, -0.6244997998398398]])
    down /= np.linalg.norm(down, axis=1, keepdims=True)
    back = _oct_decode(_oct_encode(down))
    assert (back[:, 2] < 0).all(), "下半球法线被折成朝上了 —— 折叠分支没生效"
    cos = np.clip((down * back).sum(1), -1, 1)
    assert np.degrees(np.arccos(cos)).max() < 2.0

    # 对照：把 |x|+|y| 压回 1 以内（= 不折叠）确实会翻到上半球，证明上面不恒真
    l1 = np.abs(down).sum(1, keepdims=True)
    unfolded = np.round((np.clip(down[:, :2] / l1, -1, 1) * 0.5 + 0.5) * 255.0).astype(np.uint8)
    assert (_oct_decode(unfolded)[:, 2] > 0).all()


def _split_quantized_mesh(data: bytes):
    """按 quantized-mesh-1.0 spec 从零走完一张瓦片，返回 (vertexCount, 扩展段列表)。

    刻意**不复用** tests/test_fix_terrain_mesh_indices.py 的 `_parse` —— 那个
    是给规则网格/自适应网格的顶点与索引段写的，它的 `assert off == len(data)`
    在有扩展段之后本来就要改；两边共用一个解析器的话，解析器写错会同时骗过
    两边。这里只走「跳到扩展段」所需的最少字段。

    推进方式与 Cesium 的 lFt 完全一致：`a += extensionLength`。因此
    extensionLength 算错时，最后那句"字节流没被精确消费完"会当场红 ——
    Cesium 自己反而不会（它读法线用的是 vertexCount*2，不看这个字段）。
    """
    (vertex_count,) = struct.unpack_from("<I", data, 88)
    off = 88 + 4 + 6 * vertex_count
    width = 4 if vertex_count > 65536 else 2
    off += (-off) % width                       # spec: IndexData 之前的对齐 padding
    (tcount,) = struct.unpack_from("<I", data, off)
    off += 4 + tcount * 3 * width
    for _ in range(4):
        (ecount,) = struct.unpack_from("<I", data, off)
        off += 4 + ecount * width

    exts = []
    while off < len(data):
        ext_id, ext_len = struct.unpack_from("<BI", data, off)
        off += 5
        exts.append((ext_id, data[off:off + ext_len]))
        off += ext_len
    assert off == len(data), f"字节流没被精确消费完（off={off} len={len(data)}）"
    return vertex_count, exts


def _flat_grid(n):
    yy, xx = np.mgrid[0:n, 0:n]
    return (100.0 + xx * 2.5 + yy * 1.25).astype(np.float64)


def _varying_normals(n):
    """每个格点一个**互不相同**的方向：子集取错就一定露馅。"""
    yy, xx = np.mgrid[0:n, 0:n]
    v = np.stack([(xx - n / 2) / n, (yy - n / 2) / n, np.ones_like(xx, dtype=float)], axis=-1)
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def test_extension_segment_is_appended_with_correct_header():
    """extension 段格式：unsigned char id(=1) + unsigned int length(小端) + payload。

    小端不是随口说的：Cesium 读 extensionLength 用的是
    `S.getUint32(a, littleEndianExtensionSize)`，而 littleEndianExtensionSize
    在 layer.json 声明 "octvertexnormals" 时为 true（声明成老的 "vertexnormals"
    才是大端）。
    """
    n = 17
    h = _flat_grid(n)
    nrm = np.zeros((n, n, 3))
    nrm[..., 2] = 1.0

    without = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h)
    withn = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h, normals=nrm)

    # 扩展段是**追加**在原有字节流之后的，前面一个字节都不许动
    assert withn[:len(without)] == without, "加法线改动了 header/顶点/索引/边索引段"
    assert len(withn) == len(without) + 1 + 4 + 2 * n * n

    ext_id, ext_len = struct.unpack_from("<BI", withn, len(without))
    assert ext_id == 1, "oct-encoded normals 的 extensionId 必须是 1"
    assert ext_len == 2 * n * n

    vcount, exts = _split_quantized_mesh(withn)
    assert vcount == n * n
    assert [e[0] for e in exts] == [1]
    assert exts[0][1] == _oct_encode(nrm.reshape(-1, 3)).tobytes()

    assert _split_quantized_mesh(without)[1] == [], "没传 normals 却写了扩展段"


def test_normals_follow_the_simplified_vertex_subset():
    """传 mesh 时法线数量跟随保留顶点，**值仍来自满网格**（粗几何 + 精细法线）。

    只断言数量是不够的：`nrm[:len(vert_idx)]` 这种取前 N 个的写法数量也对。
    所以这里用逐格点互不相同的法线场，逐字节比对 `nrm[vert_idx]`。
    """
    from src.services.terrain_tiling.rtin import rtin_errors, rtin_extract

    n = 17
    h = _flat_grid(n)
    nrm = _varying_normals(n)
    err = rtin_errors(h.reshape(-1), n, pin_border=True)
    verts, tris = rtin_extract(err, n, max_error=3.0)
    assert len(verts) < n * n

    data = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h,
                                 mesh=(verts, tris), normals=nrm)
    base = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h, mesh=(verts, tris))
    assert data[:len(base)] == base

    ext_id, ext_len = struct.unpack_from("<BI", data, len(base))
    assert ext_id == 1
    assert ext_len == 2 * len(verts)

    vcount, exts = _split_quantized_mesh(data)
    assert vcount == len(verts)
    want = _oct_encode(nrm.reshape(-1, 3)[verts]).tobytes()
    assert exts[0][1] == want, "自适应分支的法线不是满网格法线在保留顶点上的子集"
    # 反向对照：取前 N 个（而不是按 vert_idx 取）会得到不同的字节，
    # 证明上面那条断言是有分辨力的
    assert want != _oct_encode(nrm.reshape(-1, 3)[:len(verts)]).tobytes()


def test_encode_rejects_a_normal_count_that_does_not_match_the_vertices():
    """法线条数与顶点数对不上必须当场报错。

    Cesium 读法线是 `new Uint8Array(buffer, offset, vertexCount*2)` —— 长度取
    vertexCount，**不看 extensionLength**。写多了它静默只用前面一截，写少了
    直接 RangeError 且整张瓦片解码失败。两种都是本项目最怕的形态，所以在
    编码端一次性挡掉，比让读端去猜强。
    """
    n = 17
    h = _flat_grid(n)
    with pytest.raises(ValueError, match="normals"):
        encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h,
                              normals=np.tile([[0.0, 0.0, 1.0]], (n * n - 1, 1)))


class _FlatSampler(_AnalyticSampler):
    """全平地形：ledger 实测平坦瓦片上规则网格 gzip 后恒胜（U 形曲线的左端）。"""

    def sample(self, lons, lats):
        self.calls.append((np.asarray(lons), np.asarray(lats)))
        return np.full(np.shape(lons), 1200.0)


class _SmoothSampler(_AnalyticSampler):
    """平滑中等起伏：瓦片跨度内只走过几个弧度，martini 在 gzip 后能赢。

    _AnalyticSampler 的 sin(lon*60) 在一张 z5 瓦片里要走 675 弧度，逐格点近似
    白噪声 —— 那是 U 形曲线的右端，grid 赢。要逼 auto 落在 martini 上必须给
    中间带（ledger 实测 30~300 m 高差）的平滑地形。
    """

    SPAN = None                                  # 由 fixture 填瓦片跨度

    def sample(self, lons, lats):
        lons = np.asarray(lons)
        lats = np.asarray(lats)
        self.calls.append((lons, lats))
        w, s, e, n = self.SPAN
        return 500.0 + 150.0 * np.sin((lons - w) / (e - w) * 4.0) * \
            np.cos((lats - s) / (n - s) * 3.0)


@pytest.mark.parametrize("sampler_cls,triangulator,expect_backend", [
    (_SmoothSampler, "martini", "martini"),
    (_SmoothSampler, "grid", "grid"),
    (_SmoothSampler, "auto", "martini"),
    (_FlatSampler, "auto", "grid"),
])
def test_every_written_tile_carries_normals_whichever_backend_wins(
        tmp_path, monkeypatch, sampler_cls, triangulator, expect_backend):
    """落盘瓦片必须带法线段 —— 三个后端都要，`auto` 的**两条**分支都要。

    auto 会把 grid 与 martini 各编一遍再比字节，法线只传给其中一条的话，
    另一条胜出时瓦片就没有法线段：作业照样 completed、瓦片照样 200、
    Cesium 的 hasVertexNormals 照样 true（那是 layer.json 说了算），只是
    那些瓦片不受光照 —— 又一个"什么都不报，就是不对"。所以这里把两种地形
    都跑一遍，逼 auto 分别落在 martini 和 grid 上；`expect_backend` 那条断言
    是这个用例的**前提校验** —— 哪天择优平衡变了、两个用例都落在同一个后端
    上，它会当场告诉你覆盖没了，而不是让用例悄悄退化成重复。

    tile_size 用生产值 65：ts=17 时 martini 减面空间太小、gzip 后恒大，
    auto 张张选 grid（实测 4 种地形 × 2 种 normals 开关全是 grid），
    在那个尺寸上根本构造不出 auto→martini 的用例。
    """
    n = 65
    west, south, east, north = GeographicTilingScheme(tile_size=n).tile_extent_deg(5, 20, 9)
    sampler = sampler_cls()
    sampler.SPAN = (west, south, east, north)
    monkeypatch.setattr(ct, "_WORKER_SAMPLER", sampler)

    result = ct._worker_tile((5, 20, 9, west, south, east, north, n, str(tmp_path),
                              triangulator, 0.15, True))
    assert result is not None
    assert result[2] == expect_backend, f"这张瓦片落在 {result[2]}，用例想测的是 {expect_backend}"

    data = gzip.decompress((tmp_path / "5" / "20" / "9.terrain").read_bytes())
    vcount, exts = _split_quantized_mesh(data)
    assert [e[0] for e in exts] == [1], f"{triangulator}/{expect_backend} 的瓦片没有法线扩展段"
    assert len(exts[0][1]) == 2 * vcount


def test_worker_tile_without_normals_writes_no_extension_segment(tmp_path, monkeypatch):
    """对照组：with_normals=False 时一个扩展字节都不该有。"""
    n = 17
    monkeypatch.setattr(ct, "_WORKER_SAMPLER", _AnalyticSampler())
    west, south, east, north = GeographicTilingScheme(tile_size=n).tile_extent_deg(5, 20, 9)
    ct._worker_tile((5, 20, 9, west, south, east, north, n, str(tmp_path),
                     "auto", 0.15, False))
    data = gzip.decompress((tmp_path / "5" / "20" / "9.terrain").read_bytes())
    assert _split_quantized_mesh(data)[1] == []


@pytest.mark.parametrize("flag,expected", [(True, ["octvertexnormals"]), (False, [])])
def test_layer_json_declares_octvertexnormals_iff_normals_are_written(
        tmp_path, monkeypatch, flag, expected):
    """layer.json 不声明 extensions，法线写了也是白写。

    Cesium 的 CesiumTerrainProvider 是拿 layer.json 的 extensions 数组决定
    `hasVertexNormals` 的（Cesium.js: `t.extensions.indexOf("octvertexnormals")
    !==-1 ? hasVertexNormals=true : ...`），而 requestTileGeometry 只有在
    `_requestVertexNormals && layer.hasVertexNormals` 时才把 extensions 放进
    Accept 头，解析时也只有这时才去取 OCT_VERTEX_NORMALS 段。也就是说不声明
    的话，扩展段原封不动躺在字节流里、Cesium 一眼都不看 —— 静默失效。
    """
    monkeypatch.setattr(ct, "DemSampler", _AnalyticSampler)
    monkeypatch.setattr(ct, "_worker_tile", lambda t: (0.0, 1.0, "grid"))
    ct.build_terrain(["fake.tif"], str(tmp_path), min_level=0, max_level=1,
                     tile_size=17, workers=1, normals=flag)
    layer = json.loads((tmp_path / "layer.json").read_text(encoding="utf-8"))
    assert layer["extensions"] == expected
