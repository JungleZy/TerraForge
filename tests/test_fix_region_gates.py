"""Gate 7：同一块跨反经线的地不能有四个答案，同一句报错不能盖住七种原因。

两条缺陷都实测复现过：

- 179..181E 这块地被 `/api/region/import`（200 + `crosses_antimeridian` 警告）、
  `/api/region/estimate`（200）、`/api/dem/tasks`（201，两颗颗粒）收下，却被
  `/api/tasks` 一句「跨 180° 反经线的选区暂不支持」打回。拆分能力
  （`RegionSpec.antimeridian_parts` / `region_tiles.iter_region_tile_spans` /
  DEM 颗粒枚举）早就都在了，地图管线是最后一个还在拒的 —— 正是路线图 §5.2 说
  的「注释说要拆、没人拆」的残留。
- `/api/region/import` 把 `RegionImportError` 与 `RegionValidationError` 一起
  塌进同一个译文键，于是「不是 JSON」「空文件」「传了个 Point」「坐标越界」
  「环退化」「KML 截断」「假 zip」七种完全不同的原因，用户看到的是同一句
  「请换一个文件或检查它的坐标系」。对上传了点要素的人来说，那句话是把他往
  错的方向指。

断言只打在可观测契约上：HTTP 状态码、响应体的字段、库里那一行的四至列、
以及枚举出来的瓦片 x 落在 180° 的哪一侧。
"""
import io
import json

import pytest


# 179..181E 就是那块地：西半段 179..180，东半段 -180..-179（规范写法 east=181）。
_AM_RING = [[179.0, 39.0], [181.0, 39.0], [181.0, 40.0], [179.0, 40.0], [179.0, 39.0]]
_AM_REGION = {
    'type': 'MultiPolygon',
    'coordinates': [[_AM_RING]],
    'bbox': [179.0, 39.0, 181.0, 40.0],
    'crs': 'EPSG:4326',
    'source': 'imported',
}


def _map_payload(app_mod, **overrides):
    from src.core import config

    payload = {
        'name': 'antimeridian', 'north': 40.0, 'south': 39.0,
        # 四至字段是必填的（路由的 required_fields），但带了 region 就以 region
        # 为准 —— 这里故意填一组回绕写法，用来确认落库的是 RegionSpec 归一后的
        # 值而不是请求里的原样值。
        'east': -179.0, 'west': 179.0,
        'zoom_min': 3, 'zoom_max': 3, 'style': 'roadmap',
        'output_format': 'tiles_only',
        'output_path': str(config.Config.DOWNLOADS_DIR / 'am'),
        'region': _AM_REGION,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 四个入口对同一块地的答案
# ---------------------------------------------------------------------------

def test_region_import_accepts_the_antimeridian_region(isolated_app):
    client = isolated_app.app.test_client()
    body = json.dumps({'type': 'Polygon', 'coordinates': [_AM_RING]}).encode()
    resp = client.post('/api/region/import',
                       data={'file': (io.BytesIO(body), 'am.geojson')},
                       content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_json()
    assert 'crosses_antimeridian' in resp.get_json()['warnings']


def test_region_estimate_accepts_the_antimeridian_region(isolated_app):
    client = isolated_app.app.test_client()
    resp = client.post('/api/region/estimate',
                       json={'region': _AM_REGION, 'zoom_min': 3, 'zoom_max': 3})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['tile_count'] > 0


def test_dem_task_accepts_the_antimeridian_region(isolated_app):
    from src.core import config

    client = isolated_app.app.test_client()
    resp = client.post('/api/dem/tasks', json={
        'name': 'am-dem', 'north': 40.0, 'south': 39.0, 'east': -179.0, 'west': 179.0,
        'output_path': str(config.Config.DOWNLOADS_DIR / 'amdem'),
        'dataset': 'ASTGTM.003', 'region': _AM_REGION,
    })
    assert resp.status_code == 201, resp.get_json()


def test_map_task_accepts_the_antimeridian_region(isolated_app):
    """§5.2 的收口：第四个入口不能再是那个异类。"""
    client = isolated_app.app.test_client()
    resp = client.post('/api/tasks', json=_map_payload(isolated_app))
    assert resp.status_code == 201, resp.get_json()


def test_map_task_stores_the_normalised_unwrapped_east(isolated_app):
    """落库的 east 是 RegionSpec 归一后的 181.0，不是请求里的 -179.0。

    这一列的语义就是本次改造放开的那条不变量：跨界任务的 east 落在
    (180, 360]，与 dem_tasks 早就在存的形状一致。读到 181.0 的人顺着
    region_spec 列能一跳看到为什么它合法。
    """
    from src.core.database import get_connection

    client = isolated_app.app.test_client()
    task_id = client.post('/api/tasks', json=_map_payload(isolated_app)).get_json()['task_id']

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT east, west, region_spec FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()

    assert row['east'] == pytest.approx(181.0)
    assert row['west'] == pytest.approx(179.0)
    # 四至列与 region_spec 列必须讲同一个东界 —— 一半读列、一半读 region 的
    # 消费方（枚举、足迹渲染、磁盘估算）才不会各算各的。
    assert json.loads(row['region_spec'])['bbox'][2] == pytest.approx(181.0)


def test_map_task_enumerates_tiles_on_both_sides_of_the_line(isolated_app):
    """收下不等于能跑：枚举必须真的落在 180° 两侧，且不重复。"""
    from src.contracts.region import RegionSpec
    from src.contracts.region_tiles import iter_region_tile_spans
    from src.core.database import get_connection

    client = isolated_app.app.test_client()
    task_id = client.post('/api/tasks', json=_map_payload(isolated_app)).get_json()['task_id']

    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()

    region = RegionSpec.from_row(row, source='drawn')
    xs = set()
    rows_seen = []
    for y, x_start, x_end in iter_region_tile_spans(region, 8):
        rows_seen.append(y)
        xs.update(range(x_start, x_end + 1))

    n = 1 << 8
    assert any(x < n // 2 for x in xs), '东半球（-180 那侧）一块瓦片都没枚举到'
    assert any(x >= n // 2 for x in xs), '西半球（+180 那侧）一块瓦片都没枚举到'
    assert all(0 <= x < n for x in xs), '瓦片号没有回绕进合法域'
    assert rows_seen == sorted(rows_seen), 'y 升序契约被破坏'


def test_bare_four_corner_antimeridian_is_still_rejected(isolated_app):
    """没有 region 的裸四角输入照旧 400 —— 那里的 east < west 只可能是填反了。"""
    client = isolated_app.app.test_client()
    payload = _map_payload(isolated_app)
    payload.pop('region')
    resp = client.post('/api/tasks', json=payload)
    assert resp.status_code == 400
    assert 'must be greater than west' in resp.get_json()['error']


def test_a_region_that_does_not_cross_cannot_smuggle_an_out_of_range_east():
    """判据是「region 真的跨界」，不是「有 region」。

    否则 east=250 配一个普通多边形就能混进库里，而 250 在下游会被当成未回绕
    坐标展开成一块横跨大半个地球的下载区。
    """
    from src.contracts.region import RegionSpec
    from src.models.task import Task

    plain = RegionSpec.from_bbox(40, 39, 117, 116, source='drawn')
    with pytest.raises(ValueError, match='must be between -180 and 180'):
        Task(name='t', north=40, south=39, east=250, west=116,
             zoom_min=1, zoom_max=2, output_path='/tmp/a/b',
             region_spec=plain.to_json())


def test_a_crossing_region_cannot_smuggle_a_different_east():
    """跨界放行之后仍要对账：四至列必须逐字派生自 RegionSpec.bbox。"""
    from src.contracts.region import RegionSpec
    from src.models.task import Task

    crossing = RegionSpec.from_bbox(40, 39, -179, 179, source='drawn')
    with pytest.raises(ValueError, match='does not match the task'):
        Task(name='t', north=40, south=39, east=250, west=179,
             zoom_min=1, zoom_max=2, output_path='/tmp/a/b',
             region_spec=crossing.to_json())


def test_created_task_object_carries_the_region_it_persists(isolated_app):
    """create_task 造出来的 Task 与它写出去的行不能从出生起就不一致。

    以前 `Task(...)` 不带 region_spec，行里却写着 `spec.to_json()` —— 内存里的
    `task.region_spec` 是空串，而 `to_dict()` 把空串当成「这个任务没有区域」。
    """
    from src.core import config

    task_manager = isolated_app.task_manager
    task_id = task_manager.create_task({
        'name': 'drawn', 'north': 40.0, 'south': 39.0, 'east': 117.0, 'west': 116.0,
        'zoom_min': 3, 'zoom_max': 3, 'style': 'roadmap', 'output_format': 'tiles_only',
        'output_path': str(config.Config.DOWNLOADS_DIR / 'drawn'),
    })
    from src.core.database import get_connection

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT region_spec FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row['region_spec'], '画框任务也必须写 region_spec 列'


# ---------------------------------------------------------------------------
# 七种上传失败，七种说法
# ---------------------------------------------------------------------------

_TRUNCATED_KML = (b'<?xml version="1.0"?><kml><Document><Placemark><Polygon>'
                  b'<outerBoundaryIs><LinearRing><coordinates>179,39 181,39')

_BAD_UPLOADS = [
    # (用例名, 文件名, 内容, 报错里必须出现的、只属于这一种原因的片段)
    ('not_json', 'x.geojson', b'this is not json at all', 'invalid GeoJSON'),
    ('empty_file', 'x.geojson', b'', 'empty'),
    ('point_not_polygon', 'x.geojson',
     json.dumps({'type': 'Feature', 'properties': {},
                 'geometry': {'type': 'Point', 'coordinates': [116.0, 39.0]}}).encode(),
     'no Polygon or MultiPolygon'),
    ('coords_out_of_range', 'x.geojson',
     json.dumps({'type': 'Polygon', 'coordinates': [[
         [400.0, 39.0], [401.0, 39.0], [401.0, 40.0], [400.0, 39.0]]]}).encode(),
     'within'),
    # 退化环要挑一个**外接矩形不退化**的：三点共线但沿对角展开，否则先撞上
    # 「north 必须大于 south」，测到的就不是环退化那条路了。
    ('degenerate_ring', 'x.geojson',
     json.dumps({'type': 'Polygon', 'coordinates': [[
         [116.0, 39.0], [117.0, 40.0], [116.5, 39.5], [116.0, 39.0]]]}).encode(),
     'zero area'),
    ('truncated_kml', 'x.kml', _TRUNCATED_KML, 'invalid KML'),
    ('fake_zip', 'x.zip', b'PK\x03\x04not really a zip at all', 'archive'),
]


@pytest.mark.parametrize('label, filename, payload, needle',
                         _BAD_UPLOADS, ids=[c[0] for c in _BAD_UPLOADS])
def test_each_bad_upload_names_its_own_cause(isolated_app, label, filename,
                                             payload, needle):
    client = isolated_app.app.test_client()
    resp = client.post('/api/region/import',
                       data={'file': (io.BytesIO(payload), filename)},
                       content_type='multipart/form-data')
    assert resp.status_code == 400, resp.get_json()
    assert needle in resp.get_json()['error'], resp.get_json()['error']


def test_the_seven_causes_produce_seven_distinct_messages(isolated_app):
    """逐条断言还不够：七条消息必须互不相同。

    只查「每条都含自己的关键词」挡不住回归成同一句话再各自拼一个码上去 ——
    这条断言盯的是「用户读到的那句话真的不一样」。
    """
    client = isolated_app.app.test_client()
    messages = set()
    for _label, filename, payload, _needle in _BAD_UPLOADS:
        resp = client.post('/api/region/import',
                           data={'file': (io.BytesIO(payload), filename)},
                           content_type='multipart/form-data')
        messages.add(resp.get_json()['error'])
    assert len(messages) == len(_BAD_UPLOADS), messages
