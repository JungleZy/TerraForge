"""region_tiles 枚举契约 —— 扫描线栅格化一块瓦片都不许漏。

为什么这个文件必须存在：`iter_region_tile_spans` 的水平边分支曾经**不存在**，
`x_at()` 在 dy≈0 时只能返回 x0，于是一条横跨五格的水平边只标了一格；而规则 2
的奇偶扫描只在行中线取交点，多边形在某一行整个落在中线同一侧时一个交点都没有。
两条规则同时失灵 = 那一行中间的格子静默消失。漏掉的瓦片不进计数、不进下载、
不进拼接，也不进独占缓存的保护集 —— 成品上一个洞，没有缺块行、没有日志，
而且那块缓存还会被别的任务当成孤儿删掉。

所以这里的主力不是逐例硬编码期望值，而是一份**独立实现的暴力交叉校验**：
把瓦片方格与多边形的环做 Sutherland–Hodgman 裁剪求交集面积，面积非零就是
「这块瓦片真的被碰到了」，然后断言扫描线枚举一块都没少（也没有凭空多出
完全不相干的格子）。暴力法只用 math，不 import 被测模块的任何私有函数 ——
共用一份坐标换算就等于两边一起错。
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.contracts.region import RegionSpec  # noqa: E402
from src.contracts.region_tiles import (  # noqa: E402
    bbox_tile_range,
    count_region_tiles,
    iter_region_tile_spans,
)

_MAX_LAT = 85.0511


# ---------------------------------------------------------------------------
# 独立的坐标换算（暴力法专用，刻意不从被测模块 import）
# ---------------------------------------------------------------------------

def _fx(lon, n):
    return (lon + 180.0) / 360.0 * n


def _fy(lat, n):
    lat = max(-_MAX_LAT, min(_MAX_LAT, lat))
    r = math.radians(lat)
    return (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n


def _lon_of(fx, n):
    """瓦片空间 x → 经度（_fx 的逆）。用例直接在瓦片空间描图形，可读性最好。"""
    return fx / n * 360.0 - 180.0


def _lat_of(fy, n):
    """瓦片空间 y → 纬度（_fy 的逆）。"""
    t = math.pi * (1.0 - 2.0 * fy / n)
    return math.degrees(math.atan(math.sinh(t)))


def _ring(points, n):
    """瓦片空间的点列 → (lon, lat) 环。"""
    return [(_lon_of(x, n), _lat_of(y, n)) for x, y in points]


# ---------------------------------------------------------------------------
# 暴力交叉校验
# ---------------------------------------------------------------------------

def _clip_to_box(pts, x0, y0, x1, y1):
    """Sutherland–Hodgman：把一个环裁进凸的方格窗口，返回裁剪后的点列。

    裁剪窗口是凸的，所以对任意简单多边形（含凹形）求出的**面积**都是正确的
    —— 退化的连接边贡献 0 面积。
    """
    def _clip(poly, inside, cross):
        out = []
        for i, cur in enumerate(poly):
            prev = poly[i - 1]
            cur_in, prev_in = inside(cur), inside(prev)
            if cur_in:
                if not prev_in:
                    out.append(cross(prev, cur))
                out.append(cur)
            elif prev_in:
                out.append(cross(prev, cur))
        return out

    def _cross_x(a, b, bx):
        t = (bx - a[0]) / (b[0] - a[0])
        return (bx, a[1] + t * (b[1] - a[1]))

    def _cross_y(a, b, by):
        t = (by - a[1]) / (b[1] - a[1])
        return (a[0] + t * (b[0] - a[0]), by)

    poly = list(pts)
    for inside, cross in (
            (lambda p: p[0] >= x0, lambda a, b: _cross_x(a, b, x0)),
            (lambda p: p[0] <= x1, lambda a, b: _cross_x(a, b, x1)),
            (lambda p: p[1] >= y0, lambda a, b: _cross_y(a, b, y0)),
            (lambda p: p[1] <= y1, lambda a, b: _cross_y(a, b, y1))):
        if not poly:
            return []
        poly = _clip(poly, inside, cross)
    return poly


def _signed_area(pts):
    if len(pts) < 3:
        return 0.0
    total = 0.0
    for i, (ax, ay) in enumerate(pts):
        bx, by = pts[(i + 1) % len(pts)]
        total += ax * by - bx * ay
    return total / 2.0


def _rings_in_tile_space(region, n):
    """RegionSpec 的所有环 → 瓦片空间点列（去掉闭合重复点）。

    洞环与外环绕向相反（RegionSpec 构造时已归一），所以有符号面积直接求和
    就是「挖了洞之后的净面积」。本文件的多边形之间互不相交，全局求和与
    逐 polygon 求和等价。
    """
    return [[(_fx(lon, n), _fy(lat, n)) for lon, lat in ring[:-1]]
            for poly in region.geometry for ring in poly]


def _brute_force_tiles(region, zoom, *, pad=0.0):
    """独立算出「与多边形有交集」的瓦片集合。

    `pad` 把方格向外扩一点，用来算「闭合意义上相碰」的宽松上界 —— 枚举结果
    不许超出这个上界，否则就是在拿「多返回一些」换「不漏」。
    """
    n = 2 ** zoom
    rings = _rings_in_tile_space(region, n)
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    hit = set()
    y_lo = max(0, math.floor(min(ys)))
    y_hi = min(n - 1, math.floor(max(ys)))
    for y in range(y_lo, y_hi + 1):
        for x in range(math.floor(min(xs)), math.floor(max(xs)) + 1):
            area = 0.0
            for ring in rings:
                area += _signed_area(_clip_to_box(
                    ring, x - pad, y - pad, x + 1.0 + pad, y + 1.0 + pad))
            if abs(area) > 1e-12:
                hit.add((x % n, y))
    return hit


def _enumerated(region, zoom):
    return {(x, y)
            for y, x_start, x_end in iter_region_tile_spans(region, zoom)
            for x in range(x_start, x_end + 1)}


# ---------------------------------------------------------------------------
# 交叉校验用的图形。全部带**水平边**，且刻意让某些行的图形整个落在行中线的
# 同一侧 —— 那正是两条覆盖规则同时失灵的条件。
# ---------------------------------------------------------------------------

_DESIGN_ZOOM = 6
_DESIGN_N = 2 ** _DESIGN_ZOOM


def _rect_with_midpoint_vertices():
    """轴对齐矩形 + 上下边各一个中点顶点。

    多一个顶点就绕开了 `is_rectangle` 快路径（环长不再是 5），于是走扫描线；
    底边 y=5.3 落在第 5 行的中线 5.5 **上方**，那一行一个交点都没有。
    """
    return [_ring([(2.2, 3.2), (4.5, 3.2), (6.8, 3.2),
                   (6.8, 5.3), (4.5, 5.3), (2.2, 5.3)], _DESIGN_N)]


def _thin_horizontal_bar():
    """整条都在第 3 行内、且完全位于行中线 3.5 上方的薄横条。"""
    return [_ring([(2.2, 3.15), (4.0, 3.15), (6.8, 3.15),
                   (6.8, 3.35), (4.0, 3.35), (2.2, 3.35)], _DESIGN_N)]


def _offset_l_shape():
    """L 形：短臂的水平段落在第 4 行中线上方，长臂让那一行仍有交点。

    这是最阴险的形态 —— 规则 2 在这一行**有**交点（来自长臂），看上去覆盖
    正常，缺的只有短臂中间那一格。
    """
    return [_ring([(2.2, 3.2), (6.8, 3.2), (6.8, 4.3),
                   (4.3, 4.3), (4.3, 7.4), (2.2, 7.4)], _DESIGN_N)]


def _comb():
    """梳状多边形：三根齿 + 一条底座，底座顶边 y=5.6 与底边 y=6.4 都是水平边。

    第 6 行（y∈[6,7]）里只有底座的 6.0..6.4 那一条，中线 6.5 在它下方 ——
    整行只剩两侧竖边能标到，中间四格靠水平边分支才不会消失。
    """
    return [_ring([
        (2.1, 6.4), (7.9, 6.4), (7.9, 5.6),
        (7.2, 5.6), (7.2, 3.2), (6.5, 3.2), (6.5, 5.6),
        (5.2, 5.6), (5.2, 3.2), (4.5, 3.2), (4.5, 5.6),
        (3.2, 5.6), (3.2, 3.2), (2.5, 3.2), (2.5, 5.6),
        (2.1, 5.6),
    ], _DESIGN_N)]


def _square_with_hole():
    """带洞的方块：洞必须真的把内部瓦片挖掉，暴力法逐格复核。"""
    outer = _ring([(2.2, 3.2), (8.8, 3.2), (8.8, 9.8), (2.2, 9.8)], _DESIGN_N)
    hole = _ring([(4.2, 5.2), (6.8, 5.2), (6.8, 7.8), (4.2, 7.8)], _DESIGN_N)
    return [outer, hole]


_SHAPES = {
    'rect_with_midpoint_vertices': _rect_with_midpoint_vertices,
    'thin_horizontal_bar': _thin_horizontal_bar,
    'offset_l_shape': _offset_l_shape,
    'comb': _comb,
    'square_with_hole': _square_with_hole,
}


@pytest.mark.parametrize('shape_name', sorted(_SHAPES))
@pytest.mark.parametrize('zoom', [5, 6, 7])
def test_scanline_enumeration_misses_no_tile(shape_name, zoom):
    """暴力交叉校验：多边形真正碰到的每一块瓦片都必须被枚举出来。

    守的就是水平边那个漏块回归 —— 把 `x_extent_in_row` 的 dy≈0 分支去掉，
    薄横条 / 梳状 / L 形这几例立刻红。
    """
    region = RegionSpec.from_polygons([_SHAPES[shape_name]()])
    enumerated = _enumerated(region, zoom)
    required = _brute_force_tiles(region, zoom)
    assert required, '用例本身要碰到瓦片，否则这条断言是空的'
    assert not (required - enumerated), (
        f'{shape_name} @ z{zoom} 漏了 {sorted(required - enumerated)}')


@pytest.mark.parametrize('shape_name', sorted(_SHAPES))
@pytest.mark.parametrize('zoom', [5, 6, 7])
def test_scanline_enumeration_adds_no_unrelated_tile(shape_name, zoom):
    """反方向：枚举不许把完全碰不到的瓦片算进来。

    没有这一条，「所有情况都返回整个外接矩形」也能让上一条通过 —— 而多返回
    的瓦片会被真的下载、真的计费、真的进独占缓存保护集。
    方格向外放宽 1e-6 是为了容忍边界相切（相切本来就允许入选）。
    """
    region = RegionSpec.from_polygons([_SHAPES[shape_name]()])
    enumerated = _enumerated(region, zoom)
    allowed = _brute_force_tiles(region, zoom, pad=1e-6)
    assert not (enumerated - allowed), (
        f'{shape_name} @ z{zoom} 多算了 {sorted(enumerated - allowed)}')


def test_thin_horizontal_bar_row_is_one_contiguous_span():
    """薄横条那一行必须是**一段连续区间**，不是两端两个孤点。

    这是漏块 bug 的最小复现：水平边只标一格时，这里会得到 [(3,2,2),(3,6,6)]。
    """
    region = RegionSpec.from_polygons([_thin_horizontal_bar()])
    assert list(iter_region_tile_spans(region, _DESIGN_ZOOM)) == [(3, 2, 6)]


# ---------------------------------------------------------------------------
# 洞 / MultiPolygon
# ---------------------------------------------------------------------------

def test_hole_reduces_the_tile_count():
    """洞是真的挖掉的：同一个外环，带洞的瓦片数必须更少。

    守的是「后端有能力、前端丢信息」那类退化 —— 一旦洞环被忽略，两个数会相等。
    """
    outer = _ring([(2.2, 3.2), (8.8, 3.2), (8.8, 9.8), (2.2, 9.8)], _DESIGN_N)
    hole = _ring([(4.2, 5.2), (6.8, 5.2), (6.8, 7.8), (4.2, 7.8)], _DESIGN_N)
    solid = RegionSpec.from_polygons([[outer]])
    holed = RegionSpec.from_polygons([[outer, hole]])
    assert holed.hole_count == 1
    assert count_region_tiles(holed, _DESIGN_ZOOM, _DESIGN_ZOOM) < \
        count_region_tiles(solid, _DESIGN_ZOOM, _DESIGN_ZOOM)


def test_hole_excludes_the_tile_that_sits_entirely_inside_it():
    """洞内被完全包住的那一格必须缺席，而洞边上的格子仍然要在。"""
    region = RegionSpec.from_polygons([_square_with_hole()])
    tiles = _enumerated(region, _DESIGN_ZOOM)
    assert (5, 6) not in tiles          # [5,6]x[6,7] 完全落在洞里
    assert (4, 6) in tiles              # 洞的左边界 4.2 穿过这一格


def test_multipolygon_is_exactly_the_union_of_its_parts():
    """MultiPolygon 的枚举结果 = 各部分单独枚举的并集，一块不多一块不少。"""
    part_a = _ring([(2.2, 3.2), (3.5, 3.2), (4.8, 3.2),
                    (4.8, 5.8), (2.2, 5.8)], _DESIGN_N)
    part_b = _ring([(20.2, 9.2), (21.5, 9.2), (22.8, 9.2),
                    (22.8, 11.8), (20.2, 11.8)], _DESIGN_N)
    combined = RegionSpec.from_polygons([[part_a], [part_b]])
    only_a = RegionSpec.from_polygons([[part_a]])
    only_b = RegionSpec.from_polygons([[part_b]])
    assert combined.polygon_count == 2
    assert _enumerated(combined, _DESIGN_ZOOM) == (
        _enumerated(only_a, _DESIGN_ZOOM) | _enumerated(only_b, _DESIGN_ZOOM))


def test_count_matches_the_enumeration_it_claims_to_summarise():
    """count_region_tiles 与 iter_region_tile_spans 必须同源。

    「预估的数」和「实际要下的数」分叉是最难查的一类偏差（用户会以为估算器坏了）。
    """
    region = RegionSpec.from_polygons([_comb()])
    total = sum(len(_enumerated(region, z)) for z in range(4, 9))
    assert count_region_tiles(region, 4, 8) == total


# ---------------------------------------------------------------------------
# 反经线
# ---------------------------------------------------------------------------

def test_antimeridian_region_yields_one_tile_at_zoom_zero():
    """z0 只有一块瓦片：跨界的两段都钳到 x=0，产出两次就是下两遍、多算一块。"""
    region = RegionSpec.from_bbox(north=10, south=-10, east=-170, west=170)
    assert region.crosses_antimeridian
    assert list(iter_region_tile_spans(region, 0)) == [(0, 0, 0)]


def test_antimeridian_rows_ascend_and_never_repeat_a_tile():
    """跨界区域仍然满足「y 升序、瓦片不重复」的产出契约。

    两段各自 yield 会让行号先升一遍再从头来一遍，下游按 (zoom, y, x) 升序
    做断点续传的逻辑会直接错乱。
    """
    region = RegionSpec.from_bbox(north=40, south=-40, east=-170, west=170)
    spans = list(iter_region_tile_spans(region, 5))
    rows = [y for y, _, _ in spans]
    assert rows == sorted(rows)
    tiles = [(y, x) for y, x0, x1 in spans for x in range(x0, x1 + 1)]
    assert len(tiles) == len(set(tiles))
    assert all(0 <= x < 32 for _, x in tiles)


# ---------------------------------------------------------------------------
# 矩形快路径与 bbox_tile_range 的一致性
# ---------------------------------------------------------------------------

def test_rectangle_fast_path_is_bit_for_bit_bbox_tile_range():
    """矩形走快路径，逐层与 bbox_tile_range 完全一致。

    这两者一旦漂移，存量任务恢复时枚举出的瓦片集合就变了 —— 表现为整片缓存
    静默失效 + 「已完成」的任务重新开始下载。
    """
    north, south, east, west = 39.9, 39.1, 116.8, 116.1
    region = RegionSpec.from_bbox(north=north, south=south, east=east, west=west)
    assert region.is_rectangle
    for zoom in range(0, 14):
        x_min, x_max, y_min, y_max = bbox_tile_range(north, south, east, west, zoom)
        assert list(iter_region_tile_spans(region, zoom)) == [
            (y, x_min, x_max) for y in range(y_min, y_max + 1)]


@pytest.mark.parametrize('zoom', [-1, 22, 'x', None])
def test_invalid_zoom_is_rejected(zoom):
    """层级越界必须抛，不能静默按边界档枚举出一份错误的瓦片集合。"""
    region = RegionSpec.from_bbox(north=1, south=0, east=1, west=0)
    with pytest.raises((ValueError, TypeError)):
        list(iter_region_tile_spans(region, zoom))
