"""geo_validation 测试 —— bbox / zoom 共用边界校验。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.services.geo_validation import (
    MAX_ZOOM, MIN_ZOOM, coerce_number, validate_bbox, validate_zoom,
)


# ---------- coerce_number ----------

def test_coerce_accepts_numbers_and_numeric_strings():
    assert coerce_number(40.5, 'north') == 40.5
    assert coerce_number('40.5', 'north') == 40.5
    assert coerce_number(-0.0, 'west') == 0.0


@pytest.mark.parametrize('bad', [None, 'abc', [1], {'x': 1}, object(), True, False])
def test_coerce_rejects_non_numbers_as_valueerror(bad):
    """None/列表等会让 float() 抛 TypeError —— 必须统一成 ValueError(400 语义)。
    JSON 布尔是 int 子类(float(True)=1.0),坐标不含义布尔,同样拒绝。"""
    with pytest.raises(ValueError, match='must be a number'):
        coerce_number(bad, 'north')


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf'), 'nan', 'inf'])
def test_coerce_rejects_non_finite(bad):
    with pytest.raises(ValueError, match='must be a finite number'):
        coerce_number(bad, 'north')


# ---------- validate_bbox ----------

def test_validate_bbox_ok_returns_floats():
    assert validate_bbox('40', 39, 117.0, 116) == (40.0, 39.0, 117.0, 116.0)


def test_validate_bbox_accepts_full_world():
    assert validate_bbox(90, -90, 180, -180) == (90.0, -90.0, 180.0, -180.0)


@pytest.mark.parametrize('kwargs, match', [
    (dict(north=39, south=40), 'must be greater than south'),
    (dict(north=91), 'must be between -90 and 90'),
    (dict(south=-91), 'must be between -90 and 90'),
    (dict(east=181), 'must be between -180 and 180'),
    (dict(west=-181), 'must be between -180 and 180'),
    (dict(east=115, west=116), 'must be greater than west'),
    (dict(east=116, west=116), 'must be greater than west'),
    (dict(north=float('nan')), 'must be a finite number'),
])
def test_validate_bbox_rejects_bad_input(kwargs, match):
    base = dict(north=40, south=39, east=117, west=116)
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        validate_bbox(**base)


def test_validate_bbox_rejects_antimeridian_style_input():
    """west=170, east=-170(跨反经线的直觉写法)必须拒绝,不能静默交换后
    下载 -170..170 这个完全错误的区域。"""
    with pytest.raises(ValueError, match='must be greater than west'):
        validate_bbox(north=40, south=39, east=-170, west=170)


def test_validate_bbox_accepts_the_normalised_antimeridian_form_when_asked():
    """跨界的规范写法是 east > 180（不是 east < west），显式开关才放行。

    这条口子是给「四至列派生自一个真的跨界的 RegionSpec」那条路开的：
    RegionSpec 把 west=170/east=-170 归一成 west=170/east=190，任务表存的就是
    190 —— dem_tasks 早就在这么存了，地图管线本次跟上。
    """
    assert validate_bbox(40, 39, 190, 170, allow_unwrapped_east=True) == (
        40.0, 39.0, 190.0, 170.0)


def test_validate_bbox_default_still_rejects_the_unwrapped_form():
    """开关是 keyword-only 且默认关：裸四角路径的合同一个字没动。"""
    with pytest.raises(ValueError, match='must be between -180 and 180'):
        validate_bbox(40, 39, 190, 170)


def test_validate_bbox_unwrapped_still_rejects_east_below_west():
    """放开上界不等于放开顺序 —— 170..-170 这种回绕写法照旧拒。"""
    with pytest.raises(ValueError, match='must be greater than west'):
        validate_bbox(40, 39, -170, 170, allow_unwrapped_east=True)


def test_validate_bbox_unwrapped_rejects_more_than_a_full_turn():
    """west=-180 + east=360 是 540°，同一条经线被绕两遍，枚举会重复出瓦片。"""
    with pytest.raises(ValueError, match='must not exceed 360'):
        validate_bbox(40, 39, 360, -180, allow_unwrapped_east=True)


# ---------- validate_zoom ----------

def test_validate_zoom_ok():
    assert validate_zoom(12, 'zoom_min') == 12
    assert validate_zoom('14', 'zoom_max') == 14
    assert validate_zoom(MIN_ZOOM, 'z') == MIN_ZOOM
    assert validate_zoom(MAX_ZOOM, 'z') == MAX_ZOOM


@pytest.mark.parametrize('bad', [-1, 22, 12.5, 'abc', None, float('nan'), [12]])
def test_validate_zoom_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        validate_zoom(bad, 'zoom_min')
