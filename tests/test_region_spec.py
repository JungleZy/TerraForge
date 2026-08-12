"""RegionSpec 合同 —— 四条管线共用的区域对象，构造即校验。

这个文件守的是「文档里写了、代码里做了、但没有任何测试盯着」的那一批行为。
其中最贵的一条是反经线归一：原判据「直接跨度 > 180 且平移后 ≤ 180 就平移」
会把一个**合法的宽区域**换成它的补集 —— 经度 -170..30 的区域被读成 30..190，
用户导入一个文件、程序去下地球另一半，瓦片数看着还挺合理，界面上没有任何异常，
而且这个错误会随 to_json 一起持久化进 tasks.region_spec。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.contracts.region import RegionSpec, RegionValidationError  # noqa: E402


def _box(west, south, east, north):
    return [(west, south), (east, south), (east, north), (west, north)]


# ---------------------------------------------------------------------------
# 洞
# ---------------------------------------------------------------------------

def test_holes_survive_construction_and_are_really_subtracted():
    """洞环必须进 geometry 并且真的从 contains_point 里挖掉。

    GeoDownloader 的洞环 bug 根因是前端只取外环 —— 后端掩膜本来是奇偶扫描线，
    传进内环就正确。这里不给「只取外环」留任何余地。
    """
    spec = RegionSpec.from_polygons([[_box(0, 0, 10, 10), _box(3, 3, 7, 7)]])
    assert spec.has_holes and spec.hole_count == 1
    assert len(spec.geometry[0]) == 2
    assert spec.contains_point(1.0, 1.0) is True      # 外环内、洞外
    assert spec.contains_point(5.0, 5.0) is False     # 洞里


def test_hole_winding_is_normalised_opposite_to_the_outer_ring():
    """外环 CCW、洞环 CW（RFC 7946）—— 否则 QGIS / OGR 读出来的洞不是洞。"""
    from src.contracts.region import ring_area

    spec = RegionSpec.from_polygons([[_box(0, 0, 10, 10), _box(3, 3, 7, 7)]])
    outer, hole = spec.geometry[0]
    assert ring_area(outer) > 0
    assert ring_area(hole) < 0


# ---------------------------------------------------------------------------
# 拒绝
# ---------------------------------------------------------------------------

def test_self_intersecting_ring_is_rejected():
    """领结形环直接拒。

    它的 shoelace 面积恰好是 0，所以先查面积会给出「所有点共线」这个彻头彻尾
    的假原因 —— 用户拿着这句话去简化几何，怎么也修不好。
    """
    bowtie = [(0.0, 0.0), (10.0, 10.0), (10.0, 0.0), (0.0, 10.0)]
    with pytest.raises(RegionValidationError, match='self-intersecting'):
        RegionSpec.from_polygons([[bowtie]])


def test_zero_area_ring_is_rejected():
    """共线的「环」没有面积，放行等于让下载阶段才暴露成 0 张瓦片。"""
    collinear = [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]
    with pytest.raises(RegionValidationError, match='zero area'):
        RegionSpec.from_polygons([[collinear]])


@pytest.mark.parametrize('ring, match', [
    (_box(0, 0, 10, 95), 'north must be within'),
    (_box(0, -95, 10, 10), 'south must be within'),
    (_box(-181, 0, 10, 10), 'west must be within'),
])
def test_out_of_range_coordinates_are_rejected(ring, match):
    """值域越界必须在构造处拒绝：`_frac_y` 对 |lat|>90 会给出无意义的行号。"""
    with pytest.raises(RegionValidationError, match=match):
        RegionSpec.from_polygons([[ring]])


def test_non_wgs84_crs_cannot_be_stored():
    """合同里不留「可能不是 4326」的口子 —— 那等于把重投影责任推给每个消费方。"""
    ring = tuple(_box(0, 0, 10, 10)) + ((0.0, 0.0),)
    with pytest.raises(RegionValidationError, match='EPSG:4326'):
        RegionSpec(geometry=((ring,),), bbox_north=10, bbox_south=0,
                   bbox_east=10, bbox_west=0, crs='EPSG:3857')


def test_empty_geometry_is_rejected():
    with pytest.raises(RegionValidationError):
        RegionSpec.from_polygons([])


# ---------------------------------------------------------------------------
# 反经线
# ---------------------------------------------------------------------------

def test_antimeridian_polygon_is_read_as_a_narrow_crossing_region():
    """{179, -179} 这两簇点必须读成 2° 宽的跨界区域，不是「几乎整个地球」。"""
    ring = [(179.0, 0.0), (-179.0, 0.0), (-179.0, 10.0), (179.0, 10.0)]
    spec = RegionSpec.from_polygons([[ring]])
    assert spec.crosses_antimeridian
    assert spec.bbox_west == pytest.approx(179.0)
    assert spec.bbox_east == pytest.approx(181.0)
    assert spec.contains_point(180.5, 5.0) is True
    assert spec.contains_point(0.0, 5.0) is False


def test_wide_region_is_not_rewritten_into_its_complement():
    """**回归**：经度 -170..30 的 200° 宽区域绝不能被读成 30..190。

    它完全没碰反经线，但旧判据（跨度 > 180 且平移后 ≤ 180 就平移）会把它换成
    补集：`contains_point(0°E, 40°N)` 从 True 变 False、`contains_point(120°E)`
    从 False 变 True —— 程序去下地球另一半，瓦片数看着还合理，界面无异常。
    所以这里断言的是 contains_point（用户画的那个框里的点），不是 bbox 数字：
    数字对不上还能靠肉眼发现，点判错才是静默去错地方。
    """
    ring = [(-170.0, 20.0), (30.0, 20.0), (30.0, 60.0), (-170.0, 60.0)]
    spec = RegionSpec.from_polygons([[ring]])
    assert not spec.crosses_antimeridian
    assert spec.bbox_west == pytest.approx(-170.0)
    assert spec.bbox_east == pytest.approx(30.0)
    assert spec.contains_point(0.0, 40.0) is True        # 用户框内
    assert spec.contains_point(-160.0, 40.0) is True     # 用户框内（西端）
    assert spec.contains_point(120.0, 40.0) is False     # 补集那一半


def test_from_bbox_accepts_both_antimeridian_spellings():
    """`east < west`（绕回写法）与 `east > 180`（规范写法）必须归一到同一个对象。"""
    wrapped = RegionSpec.from_bbox(north=10, south=0, east=-170, west=170)
    canonical = RegionSpec.from_bbox(north=10, south=0, east=190, west=170)
    assert wrapped.bbox == canonical.bbox
    assert wrapped.geometry == canonical.geometry


def test_antimeridian_parts_are_always_inside_plus_minus_180():
    """消费方拿到的分段必须都是合法经度；不跨界时只有一段（下游不需要 if）。"""
    crossing = RegionSpec.from_bbox(north=10, south=0, east=-170, west=170)
    parts = crossing.antimeridian_parts
    assert len(parts) == 2
    assert all(-180.0 <= w <= 180.0 and -180.0 <= e <= 180.0
               for _n, _s, e, w in parts)
    plain = RegionSpec.from_bbox(north=10, south=0, east=20, west=10)
    assert len(plain.antimeridian_parts) == 1


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('spec_factory', [
    lambda: RegionSpec.from_bbox(north=39.9, south=39.1, east=116.8, west=116.1,
                                 source='drawn', display_name='北京'),
    lambda: RegionSpec.from_polygons([[_box(0, 0, 10, 10), _box(3, 3, 7, 7)]],
                                     source='imported', display_name='带洞'),
    lambda: RegionSpec.from_bbox(north=10, south=0, east=-170, west=170),
])
def test_json_round_trip_preserves_everything_downstream_reads(spec_factory):
    """落库再读回必须逐字相同：这条链路上任何一处丢信息都会静默改变下载范围。"""
    spec = spec_factory()
    back = RegionSpec.from_json(spec.to_json())
    assert back.geometry == spec.geometry
    assert back.bbox == spec.bbox
    assert back.source == spec.source
    assert back.display_name == spec.display_name
    assert back.crosses_antimeridian == spec.crosses_antimeridian


def test_from_json_rejects_empty_and_garbage():
    for bad in ('', None, '{'):
        with pytest.raises(RegionValidationError):
            RegionSpec.from_json(bad)


# ---------------------------------------------------------------------------
# from_row
# ---------------------------------------------------------------------------

def test_from_row_prefers_the_stored_region_spec():
    stored = RegionSpec.from_polygons([[_box(0, 0, 10, 10), _box(3, 3, 7, 7)]])
    row = {'region_spec': stored.to_json(),
           'north': 90, 'south': -90, 'east': 180, 'west': -180}
    got = RegionSpec.from_row(row)
    assert got.geometry == stored.geometry
    assert got.hole_count == 1


def test_from_row_falls_back_to_the_bbox_columns_for_a_legacy_row():
    """存量任务行没有 region_spec（新列，默认空串），必须能从四至列还原。

    还原不了就意味着历史任务在新 UI 上整片消失 —— 那正是 `Task.from_row`
    刻意绕过 __post_init__ 想避免的那类回归。
    """
    row = {'region_spec': '', 'north': 39.9, 'south': 39.1,
           'east': 116.8, 'west': 116.1}
    spec = RegionSpec.from_row(row)
    assert spec is not None
    assert spec.is_rectangle
    assert spec.bbox == (39.9, 39.1, 116.8, 116.1)
    assert spec.source == 'derived'


def test_from_row_falls_back_when_the_stored_spec_is_geometrically_invalid():
    """坏掉的 region_spec 不该让一行任务消失 —— 四至列还在就还能用。"""
    row = {'region_spec': '{"coordinates":[[[[0,0],[1,1],[2,2],[0,0]]]]}',
           'north': 1.0, 'south': 0.0, 'east': 1.0, 'west': 0.0}
    spec = RegionSpec.from_row(row)
    assert spec is not None and spec.bbox == (1.0, 0.0, 1.0, 0.0)


def test_from_row_survives_a_structurally_corrupt_region_spec():
    """`from_row` 的合同是「任何一步失败都返回 None」，包括**结构**畸形。

    落库的 region_spec 只是一列 TEXT，畸形内容（手改过的库、写了一半的行、
    上一版格式）曾经会让 `from_dict` 在 `float(p[0])` 上抛**裸 ValueError**，
    而 `from_row` 只 catch `RegionValidationError`。异常一路穿过
    `cache_exclusive._row_rects_by_zoom`（它只挡 iter 的 ValueError）与历史
    列表渲染 —— 一行脏数据打爆整个页面，正是 `Task.from_row` 刻意绕过
    `__post_init__` 想避免的形态。

    修法是在 `from_dict` 里把结构性损坏收敛成 `RegionValidationError`，
    而不是在 `from_row` 加宽 except：这样每一个调用方（路由收到的客户端
    region、from_json、from_row）拿到的都是同一种异常，不必各自去猜要
    catch 哪几种。
    """
    for corrupt in ('{"coordinates": "not-a-polygon"}',
                    '{"coordinates": [[[["a", "b"]]]]}',
                    '{"coordinates": [[[[0]]]]}',
                    '{"coordinates": [[[[0,0],[1,1],[2,2],[0,0]]]], "bbox": "nope"}'):
        row = {'region_spec': corrupt,
               'north': 1.0, 'south': 0.0, 'east': 1.0, 'west': 0.0}
        spec = RegionSpec.from_row(row)
        assert spec is not None and spec.bbox == (1.0, 0.0, 1.0, 0.0), corrupt


def test_from_dict_reports_structural_damage_as_region_validation_error():
    """路由层只 catch ValueError 家族；结构损坏必须落在 RegionValidationError
    上，否则一个畸形的客户端 region 是 500 而不是 400。"""
    for corrupt in ({'coordinates': 'not-a-polygon'},
                    {'coordinates': [[[['a', 'b']]]]},
                    {'coordinates': [[[[0, 0], [1, 1], [2, 2], [0, 0]]]],
                     'bbox': 'nope'},
                    'not-a-dict'):
        with pytest.raises(RegionValidationError):
            RegionSpec.from_dict(corrupt)


def test_from_row_returns_none_when_nothing_is_recoverable():
    """兜底也拿不出东西时返回 None，由调用方决定降级形态 —— 绝不抛。"""
    assert RegionSpec.from_row({'region_spec': ''}) is None
    assert RegionSpec.from_row({'region_spec': '', 'north': 1, 'south': 2,
                                'east': 1, 'west': 0}) is None
    assert RegionSpec.from_row(None) is None


# ---------------------------------------------------------------------------
# 派生属性
# ---------------------------------------------------------------------------

def test_is_rectangle_is_false_once_an_extra_vertex_appears():
    """多一个中点顶点就不是「几何等于外接矩形」了 —— 下游必须走栅格化。

    这条判定错的方向很危险：把带缺口的多边形当矩形，就会下载整个外接矩形。
    """
    plain = RegionSpec.from_polygons([[_box(0, 0, 10, 10)]])
    assert plain.is_rectangle
    with_midpoint = RegionSpec.from_polygons([[
        [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]]])
    assert not with_midpoint.is_rectangle


def test_contains_point_ignores_other_polygons_holes():
    """两个多边形各自的洞互不干扰（奇偶规则按环累计，不是按多边形累计）。"""
    spec = RegionSpec.from_polygons([
        [_box(0, 0, 10, 10), _box(3, 3, 7, 7)],
        [_box(20, 0, 30, 10)],
    ])
    assert spec.contains_point(25.0, 5.0) is True
    assert spec.contains_point(5.0, 5.0) is False
    assert spec.polygon_count == 2 and spec.hole_count == 1
