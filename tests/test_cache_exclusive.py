"""cache_exclusive —— 「只被这一个任务引用的瓦片」的矩形差集。

删除一个任务时要清掉它独占的缓存。算错一格的后果是不对称的：多删就是**误伤
别的任务**（它下次跑要重下那片，而且没有任何提示），少删只是留点垃圾。所以
模块的保守规则是「算不出来就不删」，而差集本身必须是精确的。

`rect_subtract` 的四块切法（上下带占满整个 x 跨度、左右块只占被挖的那几行）
保证输出互不相交 —— 「面积之和 = 原面积 − 交集面积」这条不变式可以直接测，
本文件就在小整数网格上逐格复核它。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _cells(rect):
    x0, x1, y0, y1 = rect
    return {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}


def _area(rect):
    x0, x1, y0, y1 = rect
    return max(0, x1 - x0 + 1) * max(0, y1 - y0 + 1)


# ---------------------------------------------------------------------------
# rect_subtract
# ---------------------------------------------------------------------------

RECT = (0, 9, 0, 9)

HOLE_SETS = {
    'no_hole': [],
    'disjoint': [(20, 25, 20, 25)],
    'corner': [(0, 3, 0, 3)],
    'centre': [(4, 5, 4, 5)],
    'full_cover': [(0, 9, 0, 9)],
    'larger_than_rect': [(-5, 20, -5, 20)],
    'cross': [(4, 5, 0, 9), (0, 9, 4, 5)],
    'two_overlapping': [(0, 5, 0, 5), (3, 8, 3, 8)],
    'stripes': [(0, 9, 0, 0), (0, 9, 2, 2), (0, 9, 4, 4),
                (0, 9, 6, 6), (0, 9, 8, 8)],
    'edge_touching': [(10, 15, 0, 9), (0, 9, 10, 15)],
}


@pytest.mark.parametrize('name', sorted(HOLE_SETS))
def test_rect_subtract_is_exact_and_disjoint(name):
    """差集逐格等于「原矩形 − 所有 hole 的并集」，且各块互不相交。

    重叠会让同一块瓦片被删两次（第二次 unlink 报 ENOENT，统计数虚高）；
    少切会误删别人的缓存。两个方向在这里同时被钉住。
    """
    from src.services.cache_exclusive import rect_subtract

    holes = HOLE_SETS[name]
    pieces = rect_subtract(RECT, holes)

    covered = set()
    area_sum = 0
    for piece in pieces:
        cells = _cells(piece)
        assert not (covered & cells), f'{name}: 输出的矩形互相重叠'
        covered |= cells
        area_sum += _area(piece)

    # 面积之和 == 去重后的格子数 —— 不重叠的等价说法，也顺带证明没有空矩形。
    assert area_sum == len(covered)

    expected = _cells(RECT)
    for hole in holes:
        expected -= _cells(hole)
    assert covered == expected


def test_rect_subtract_returns_the_rect_itself_when_nothing_overlaps():
    """完全不相交时不许把矩形切碎 —— 碎片会让后续的命中索引白白变慢。"""
    from src.services.cache_exclusive import rect_subtract

    assert rect_subtract((0, 9, 0, 9), [(100, 200, 100, 200)]) == [(0, 9, 0, 9)]


def test_rect_subtract_of_a_full_cover_is_empty():
    from src.services.cache_exclusive import rect_subtract

    assert rect_subtract((0, 9, 0, 9), [(0, 9, 0, 9)]) == []


# ---------------------------------------------------------------------------
# 任务行 → 独占矩形
# ---------------------------------------------------------------------------

def _snapshot_json(style='s', url='https://tiles.example/s/{z}/{x}/{y}.png'):
    from src.contracts.source import SourceSnapshot

    return SourceSnapshot(source_id='satellite', url_template=url,
                          style=style).to_json()


def _row(task_id, *, north, south, east, west, zoom, snapshot=None):
    """一行最小可用的地图任务行（dict 就够：模块只用 row[key]）。"""
    return {
        'id': task_id,
        'style': 'satellite',
        'source_snapshot': _snapshot_json() if snapshot is None else snapshot,
        'region_spec': '',
        'north': north, 'south': south, 'east': east, 'west': west,
        'zoom_min': zoom, 'zoom_max': zoom,
    }


def _exclusive_cells(task_row, other_rows):
    from src.services.cache_exclusive import exclusive_tile_rects

    out = set()
    for zoom, x0, x1, y0, y1 in exclusive_tile_rects(task_row, other_rows):
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                assert (zoom, x, y) not in out, '独占矩形之间不许重叠'
                out.add((zoom, x, y))
    return out


def _referenced_cells(row):
    from src.contracts.region import RegionSpec
    from src.contracts.region_tiles import iter_region_tile_spans

    region = RegionSpec.from_row(row)
    zoom = row['zoom_min']
    return {(zoom, x, y)
            for y, x0, x1 in iter_region_tile_spans(region, zoom)
            for x in range(x0, x1 + 1)}


WIDE = dict(north=40.0, south=39.0, east=117.0, west=116.0, zoom=10)
NARROW = dict(north=39.6, south=39.4, east=116.6, west=116.4, zoom=10)


def test_two_identical_tasks_leave_nothing_exclusive():
    """另一个存活任务引用着同一片瓦片 → 一块都不能删。

    这是删除路径最常见的形态（同一区域下了 satellite 又下了一遍），漏掉它
    就会在用户删掉其中一个之后把另一个的缓存清空。
    """
    mine = _row(1, **WIDE)
    twin = _row(2, **WIDE)
    assert _exclusive_cells(mine, [twin]) == set()


def test_a_superset_task_leaves_only_the_difference():
    """本任务覆盖得更广时，独占的恰好是「广出来的那一圈」。"""
    mine = _row(1, **WIDE)
    inner = _row(2, **NARROW)
    expected = _referenced_cells(mine) - _referenced_cells(inner)
    assert expected, '用例本身要有差集，否则断言是空的'
    assert _exclusive_cells(mine, [inner]) == expected


def test_a_subset_task_owns_nothing_exclusively():
    """反过来：被别人完全包住的任务一块也不独占。"""
    inner = _row(1, **NARROW)
    outer = _row(2, **WIDE)
    assert _exclusive_cells(inner, [outer]) == set()


def test_a_different_namespace_never_subtracts():
    """不同源 = 不同缓存目录，磁盘上根本不重叠，不许拿来做差集。

    减错了会让「换了图源之后删掉旧任务」什么都清不掉，缓存永远涨。
    """
    other_source = _snapshot_json(url='https://other.example/{z}/{x}/{y}.png')
    mine = _row(1, **WIDE)
    same_area_other_source = _row(2, snapshot=other_source, **WIDE)
    assert _exclusive_cells(mine, [same_area_other_source]) == _referenced_cells(mine)


def test_the_task_itself_is_never_treated_as_another_reference():
    """按 id 排除自己 —— 否则任何任务的独占集都是空的，删除功能整体失效。"""
    mine = _row(1, **WIDE)
    assert _exclusive_cells(mine, [dict(mine)]) == _referenced_cells(mine)


def test_a_task_on_other_zoom_levels_only_protects_those_levels():
    """差集是**逐层**做的：别人只下了 z10，本任务的 z11 照样独占。"""
    from src.services.cache_exclusive import exclusive_tile_rects

    mine = dict(_row(1, **WIDE), zoom_min=10, zoom_max=11)
    other = _row(2, **WIDE)                      # 只有 z10
    zooms = {zoom for zoom, *_ in exclusive_tile_rects(mine, [other])}
    assert zooms == {11}


def test_an_unparseable_task_row_protects_everything():
    """保守规则：本任务自己解析不了就什么都不产出（一块都不删）。"""
    from src.services.cache_exclusive import exclusive_tile_rects

    broken = {'id': 7, 'style': 'satellite', 'source_snapshot': _snapshot_json(),
              'region_spec': '', 'zoom_min': 10, 'zoom_max': 10}
    assert list(exclusive_tile_rects(broken, [])) == []


def test_a_row_without_a_usable_namespace_protects_everything(monkeypatch):
    """算不出命名空间就不知道该动哪个目录 —— 同样是「什么都不删」。"""
    from src.services import cache_exclusive

    mine = _row(1, **WIDE)
    # 用替身让命名空间解析失败，而不是去构造一行畸形数据：这里要验的是
    # 「解析不出来时怎么办」，不是「什么样的行解析不出来」。
    monkeypatch.setattr(cache_exclusive, '_row_namespace',
                        lambda row, config_manager=None: None)
    assert list(cache_exclusive.exclusive_tile_rects(mine, [])) == []
