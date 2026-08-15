"""地名服务的三种 bbox 表达 —— 三家的轴序两两不同，读错就是静默下错地方。

`geocoding` 只认两种响应形态（GeoJSON FeatureCollection / Nominatim 风格数组），
但**范围**在这两种形态里有三种写法，而且轴序互不相同：

| 来源 | 字段 | 轴序 |
| --- | --- | --- |
| RFC 7946 | 要素的 `bbox` 成员 | `[west, south, east, north]` |
| Photon | `properties.extent` | `[west, north, east, south]` ← 中间两位对调 |
| Nominatim | `boundingbox`（字符串） | `[south, north, west, east]` |

读错的后果分两档：南北对调会被 `RegionSpec.from_bbox` 判非法（现象是「搜到了
但一条都不显示」，日志只有一句 bbox 非法，很难指回轴序）；经纬对调在低纬度
地区**能通过取值域校验**，于是静默地下载地球另一边的一块地。所以每一家的轴序
都要有一条对着真实响应写的断言。

Photon 那一支是 2026-08 新增的：它是少数免注册免 key 的公共地名服务，但**从不给**
标准 `bbox` 成员、几何又恒为 Point，不认 `extent` 的话它每次都「搜不到」——
响应 200、features 非空，结果全被丢弃。

本文件全程离线：载荷是从官方文档与一次实测里抄下来的定值，不发任何网络请求。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.services import geocoding  # noqa: E402

# komoot/photon 官方 docs/api-v1.md 的示例响应（柏林奥林匹克体育场）逐字抄录。
# 第 2、4 位都是纬度且第 2 位更大 —— 这就是 [W, N, E, S] 的取证之一。
PHOTON_BERLIN_STADIUM = {
    'type': 'Feature',
    'geometry': {'type': 'Point', 'coordinates': [13.239514674078611, 52.51467945]},
    'properties': {
        'name': 'Berlin Olympic Stadium',
        'osm_key': 'leisure', 'osm_value': 'stadium',
        'osm_type': 'W', 'osm_id': 38862723,
        'extent': [13.23727, 52.5157151, 13.241757, 52.5135972],
    },
}

# 实测 https://photon.komoot.io/api/?q=重庆 的第一条（2026-08-13）。
# 重庆市实际跨度约 105.3–110.2°E / 28.2–32.2°N，与之相符。
PHOTON_CHONGQING = {
    'type': 'Feature',
    'geometry': {'type': 'Point', 'coordinates': [107.7539117, 30.0572914]},
    'properties': {
        'name': '重庆市', 'country': '中国', 'countrycode': 'CN',
        'osm_key': 'place', 'osm_value': 'state', 'type': 'state',
        'extent': [105.2868306, 32.2036631, 110.1944429, 28.161744],
    },
}


def _one(feature):
    out = geocoding._parse_feature_collection([feature], 5)
    assert len(out) == 1, f'这条要素被丢弃了：{feature["properties"].get("name")!r}'
    return out[0]


# ---------------------------------------------------------------- Photon

def test_photon_extent_axis_order():
    """`[W, N, E, S]` → `(W, S, E, N)`。中间两位必须对调。

    直接钉这个纯函数，是为了让轴序写反时的失败信息指到**轴序**本身 ——
    经由解析器只能看到「结果被丢弃了」，那句话对下一个维护者没有指向性。
    """
    assert geocoding._photon_extent([13.23727, 52.5157151, 13.241757, 52.5135972]) == (
        13.23727, 52.5135972, 13.241757, 52.5157151)


@pytest.mark.parametrize('raw', [
    None, [], [1, 2, 3], [1, 2, 3, 4, 5], 'not a list',
    ['a', 'b', 'c', 'd'], [1, 2, None, 4],
], ids=['None', '空', '三元', '五元', '字符串', '非数值', '含 None'])
def test_photon_extent_rejects_malformed_input(raw):
    """形态不对一律返回 None，不抛。

    这个字段是**非标准**的，没有任何规范担保它的形态；一个升级后改了结构的
    上游不该让搜索接口 500。
    """
    assert geocoding._photon_extent(raw) is None


def test_photon_feature_yields_a_usable_bbox():
    """Photon 的要素必须解析出结果 —— 它既没有标准 bbox 也没有面几何。

    改造前这里返回 0 条：响应 200、features 非空，用户看到的却是「搜不到」。
    """
    r = _one(PHOTON_BERLIN_STADIUM)
    w, s, e, n = r['bbox']
    assert (w, s, e, n) == (13.23727, 52.5135972, 13.241757, 52.5157151)
    assert n > s, '南北被读反了'
    assert r['name'] == 'Berlin Olympic Stadium'
    assert r['region'] is None, 'Point 几何不该产出面几何'


def test_photon_chinese_result_lands_in_the_right_place():
    """实测样本：重庆市必须落在四川盆地东部，不是地球另一边。

    这条钉的是「经纬没有互换」—— 只看南北顺序的断言对经纬互换是盲的
    （105/32 与 32/105 都能过取值域校验里的纬度那一半）。
    """
    w, s, e, n = _one(PHOTON_CHONGQING)['bbox']
    assert (105 < w < 106) and (110 < e < 111), f'经度落在 {w}~{e}，不是重庆'
    assert (28 < s < 29) and (32 < n < 33), f'纬度落在 {s}~{n}，不是重庆'


def test_standard_bbox_wins_over_photon_extent():
    """要素同时给了两者时，以标准 `bbox` 为准。

    优先级不是随手定的：`bbox` 是规范字段，`extent` 是某一家的扩展；两者打架
    时信规范的那个。反过来写也能让上面几条全绿，所以要单独钉。
    """
    feature = {
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [0, 0]},
        'bbox': [1.0, 2.0, 3.0, 4.0],                 # [w, s, e, n]
        'properties': {'name': 'both', 'extent': [90.0, 40.0, 91.0, 39.0]},
    }
    assert _one(feature)['bbox'] == [1.0, 2.0, 3.0, 4.0]


# ---------------------------------------------------------------- 其余两家

def test_rfc7946_bbox_order_is_unchanged():
    """标准 bbox 仍按 `[w, s, e, n]` 读 —— Photon 支持不能顺手改坏它。"""
    feature = {
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [116.4, 39.9]},
        'bbox': [116.0, 39.6, 116.8, 40.2],
        'properties': {'name': '北京市'},
    }
    assert _one(feature)['bbox'] == [116.0, 39.6, 116.8, 40.2]


def test_nominatim_boundingbox_order_is_unchanged():
    """Nominatim 的 `[south, north, west, east]`（字符串）仍然读得对。

    与上面同理：本次改动只碰 FeatureCollection 那一支，这条是另一支的看门狗。
    """
    out = geocoding._parse_nominatim([{
        'display_name': 'Chongqing, China',
        'class': 'boundary', 'type': 'administrative',
        'boundingbox': ['28.161744', '32.2036631', '105.2868306', '110.1944429'],
    }], 5)
    assert len(out) == 1
    w, s, e, n = out[0]['bbox']
    assert (105 < w < 106) and (110 < e < 111)
    assert (28 < s < 29) and (32 < n < 33)


def test_a_feature_with_no_range_at_all_is_dropped_not_crashed():
    """既无 bbox / extent 也无面几何的要素跳过即可，不能让整次搜索失败。"""
    assert geocoding._parse_feature_collection([{
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [1, 2]},
        'properties': {'name': '只有一个点'},
    }], 5) == []
