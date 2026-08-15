"""路由层的四条门槛：估算预检、上传体积闸、导出缺块标记与容器校验、完成筛选。

钉的都是**实测复现过**的缺陷，不是理论风险：

- TF-SEC-002 `POST /api/region/estimate` 对调用方给的 `region` 既不封顶顶点数、
  也不封顶栅格化工作量。栅格化的开销是「顶点数 × 扫描行数」，两个量都由请求体
  决定：梳状多边形 z16 实测 104 s，全球锯齿 z0..21 实测 11.3 s，一个 75 KB 的
  请求体约 56 min —— 单进程 Flask 上这就是一条把服务打死的路径。
- TF-SEC-016 同一条接口不传 `export_mbtiles`。MBTiles 与 `output_format` 正交，
  推不出来；不传的后果是预检结论**整整少算一份松散镜像**，而容器在任务最后一步
  才生成 —— 「盘够」的判决在跑了几小时之后才被现实推翻。
- TF-SEC-015 `/api/region/import` 的 32 MiB 闸开在 `upload.read()` 上，可是
  `request.files` 一取，werkzeug 已经把整个请求体 spool 到临时盘了 —— 真实上限
  是 `Config.MAX_CONTENT_LENGTH`(2 GiB)。
- §13-3 / §10 阶段 2：界面上导出的 MBTiles 被登记成「无缺块」（`has_gaps` 默认
  False），紧挨着同一任务写着 True 的 xyz_dir / geotiff 兄弟行；而容器「可自动
  校验」的那半个门槛没有任何调用方，出厂的库合不合规从来没有留下过判决。
- Gate 10：`completed_with_gaps` 一个状态 chip 都不匹配，`history_stats.completed`
  少算它 —— 用户接受了缺块、成品出来了，却在「完成」里找不到自己的东西。
"""
import io
import time

import pytest

# MBTilesWriter 校验魔数，所以瓦片必须是真的 PNG 头。
PNG = b'\x89PNG\r\n\x1a\n' + b'fake-png-body'


def _comb_ring(n_teeth, lon0, lat0, span):
    """梳状多边形的外环。齿越多顶点越多，而外接矩形不变 —— 这正是「顶点数」与
    「区域大小」两个量必须分别封顶的原因。"""
    pts = []
    step = span / (2.0 * n_teeth)
    x = lon0
    for _ in range(n_teeth):
        pts.append([x, lat0])
        pts.append([x, lat0 + span * 0.9])
        x += step
        pts.append([x, lat0 + span * 0.9])
        pts.append([x, lat0])
        x += step
    pts.append([lon0 + span, lat0])
    pts.append([lon0, lat0])
    return pts


def _region_body(ring, bbox, zoom_min, zoom_max):
    return {
        'region': {'type': 'MultiPolygon', 'coordinates': [[ring]],
                   'bbox': bbox, 'crs': 'EPSG:4326', 'source': 'imported'},
        'zoom_min': zoom_min, 'zoom_max': zoom_max,
    }


def _insert_map_task(output_path, *, status, gap_tiles):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO tasks (name, status, style, output_format, north, south, "
            "east, west, zoom_min, zoom_max, output_path, gap_tiles, total_tiles, "
            "downloaded_tiles) VALUES ('gappy', ?, 'satellite', 'tiles', 39.2, 39.0, "
            "116.2, 116.0, 10, 10, ?, ?, 4, 1)",
            (status, str(output_path), gap_tiles))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _write_one_tile(tile_root):
    """`<root>/10/843/388.png` —— 116.0~116.2/39.0~39.2 在 z10 上真实覆盖的那一块。"""
    d = tile_root / '10' / '843'
    d.mkdir(parents=True, exist_ok=True)
    (d / '388.png').write_bytes(PNG)


# --------------------------------------------------------------- 估算预检


def test_a_75kb_region_payload_is_rejected_instead_of_grinding(isolated_app):
    """时间断言在这里是**契约本身**：这条请求原来要跑约 56 分钟。

    断言的量级差着三个数量级（实测 5 ms vs 1 s 上限），不是在量机器速度。
    """
    client = isolated_app.app.test_client()
    body = _region_body(_comb_ring(700, 100.0, 20.0, 40.0),
                        [100.0, 20.0, 140.0, 60.0], 0, 21)
    import json
    raw = json.dumps(body)
    assert len(raw) > 60_000, '前提变了：这个载荷不再是 75 KB 量级，本用例已失效'

    started = time.perf_counter()
    resp = client.post('/api/region/estimate', data=raw,
                       content_type='application/json')
    elapsed = time.perf_counter() - started

    assert resp.status_code == 400, resp.get_json()
    assert elapsed < 1.0, f'预检自己跑了 {elapsed:.1f}s —— 闸门没有在栅格化之前生效'


def test_the_scan_work_gate_fires_where_the_tile_ceiling_cannot(isolated_app):
    """瓦片数上界过得了、工作量过不了的那一类：小框里塞一把稠密的齿。

    这条用例是「为什么两道闸都要有」的证据：外接矩形只有 0.1°，瓦片数几十万，
    任何合理的瓦片上限都拦不住它，而每一个扫描行都要遍历八万条活动边。
    """
    client = isolated_app.app.test_client()
    body = _region_body(_comb_ring(20000, 100.0, 20.0, 0.1),
                        [100.0, 20.0, 100.1, 20.1], 21, 21)

    started = time.perf_counter()
    resp = client.post('/api/region/estimate', json=body)
    elapsed = time.perf_counter() - started

    assert resp.status_code == 400, resp.get_json()
    assert 'tile rows' in resp.get_json()['error'], (
        '拒的理由必须是工作量那一条 —— 换成瓦片数就说明这条用例测的不再是它')
    assert elapsed < 3.0, f'预检自己跑了 {elapsed:.1f}s'


def test_the_vertex_cap_is_the_one_from_region_import(isolated_app):
    """两个入口一套上限。文件导入路径早就封了顶，而 `region` 字段是同一份几何的
    另一个入口 —— 两个入口两套上限，等于第一个上限白设。"""
    from src.routes import api
    from src.services import region_import

    assert api._preflight_region_cost.__module__ == 'src.routes.api'
    # 常量必须是**引用**而不是抄的数值：抄一份，改了一边就是两个上限。
    src = (api.__file__)
    with open(src, encoding='utf-8') as fh:
        text = fh.read()
    assert 'region_import.MAX_TOTAL_VERTICES' in text, (
        '顶点上限必须引用 region_import 的常量，不能在路由层另写一个数')
    assert str(region_import.MAX_TOTAL_VERTICES) not in text, (
        '路由层出现了顶点上限的字面量 —— 那就是第二份事实来源')


def test_a_modest_polygon_still_gets_an_answer(isolated_app):
    """反方向：闸门不能把正常的多边形请求也拒了。

    这条接口存在的意义就是回答「这一片有多少张、盘够不够」，一个 160 顶点、
    0.5° 见方、z8..15 的区域是它的**典型**输入。
    """
    client = isolated_app.app.test_client()
    resp = client.post('/api/region/estimate', json=_region_body(
        _comb_ring(40, 116.0, 39.0, 0.5), [116.0, 39.0, 116.5, 39.5], 8, 15))

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['tile_count'] > 0


def test_export_mbtiles_is_carried_into_the_verdict(isolated_app):
    """勾了「同时导出 MBTiles」，预检的字节数必须跟着涨。

    容器体积约等于整份松散镜像，而它在任务最后一步才生成 —— 漏算的表现是
    「盘够」的判决在跑了几小时之后才被现实推翻。
    """
    client = isolated_app.app.test_client()
    body = {'bbox': [116.0, 39.0, 116.2, 39.2], 'zoom_min': 10, 'zoom_max': 13,
            'output_format': 'tiles'}

    without = client.post('/api/region/estimate', json=body).get_json()
    with_container = client.post('/api/region/estimate',
                                 json={**body, 'export_mbtiles': True}).get_json()

    assert without['tile_count'] == with_container['tile_count'] > 0, (
        '瓦片数与容器无关，涨的只该是字节数')
    assert with_container['estimate']['output_bytes'] > without['estimate']['output_bytes']
    assert with_container['estimate']['peak_bytes'] > without['estimate']['peak_bytes']


# --------------------------------------------------------------- 上传体积闸


def test_oversized_upload_is_rejected_before_werkzeug_spools_it(isolated_app):
    """按 Content-Length 拒，且**在碰 request.files 之前**。

    取一次 `request.files` 就等于让 werkzeug 把整个请求体落到临时盘：一次 POST
    能在临时盘上写满 2 GiB，而返回码还是 400 —— 用户看不出发生了什么，盘却真的
    少了。声明值可以撒谎，但两种谎都不亏：撒小了 werkzeug 自己按
    MAX_CONTENT_LENGTH 截断，撒大了正好被这道闸拦住。
    """
    client = isolated_app.app.test_client()
    resp = client.post(
        '/api/region/import',
        data={'file': (io.BytesIO(b'{}'), 'x.geojson')},
        content_type='multipart/form-data',
        environ_overrides={'CONTENT_LENGTH': str(64 * 1024 * 1024)})

    assert resp.status_code == 413, resp.get_json()
    # 用户此刻手里只有文件属性里的那个大小，「太大了」不带数字等于没说。
    assert '32' in resp.get_json()['error']


def test_a_small_upload_is_not_caught_by_the_size_gate(isolated_app):
    """反方向：正常大小的上传必须走到解析器（这里因内容不是面要素而 400），
    不能被体积闸误拒成 413。"""
    client = isolated_app.app.test_client()
    resp = client.post('/api/region/import',
                       data={'file': (io.BytesIO(b'{}'), 'x.geojson')},
                       content_type='multipart/form-data')

    assert resp.status_code == 400


# ------------------------------------------------- 导出：缺块标记与容器校验


def test_ui_exported_mbtiles_carries_the_gap_flag_and_a_verdict(isolated_app, tmp_path):
    """一个 `completed_with_gaps` 任务从界面按钮导出的容器，三件事都要对：

    1. `has_gaps=true` —— 调用方没说时按任务行推断，而不是默认 False。原来的行为
       是每一件按钮导出的容器都被登记成无缺块，紧挨着写着 True 的兄弟行；
    2. `meta.validation` 里有判决 —— §10 阶段 2 要的是「可自动校验」，校验器只在
       有人手动调用时才跑，等于那半个门槛没实现；
    3. 两者都要**穿过 HTTP** 到得了界面 —— §10 的原话是判决要能被产物索引与界面
       看到，只落库不下发等于只做了一半。`GET /api/tasks/<id>/artifacts` 长期
       没有调用方，这正是「兄弟行标记互相矛盾」当初能一直没人发现的原因。
    """
    from src.services import artifact_store

    out = tmp_path / 'downloads' / 'map'
    task_id = _insert_map_task(out, status='completed_with_gaps', gap_tiles=3)
    _write_one_tile(out / f'task_{task_id}')

    client = isolated_app.app.test_client()
    resp = client.post(f'/api/export/map/{task_id}', json={'format': 'mbtiles'})
    payload = resp.get_json()

    assert resp.status_code == 200, payload
    assert payload['has_gaps'] is True
    assert payload['validation']['ok'] is True

    rows = [a for a in artifact_store.list_artifacts('map', task_id)
            if a.kind.value == 'mbtiles']
    assert len(rows) == 1
    assert rows[0].has_gaps is True, (
        '产物行说自己没有缺块 —— 标记跟着文件走是 §13-3 的要求，'
        '任务行可以被删，这个标记必须活得比它久')
    assert rows[0].meta['validation']['ok'] is True
    assert rows[0].meta['validation']['problems'] == []

    listed = client.get(f'/api/tasks/{task_id}/artifacts?pipeline=map').get_json()
    served = [a for a in listed['artifacts'] if a['kind'] == 'mbtiles']
    assert len(served) == 1
    assert served[0]['has_gaps'] is True
    assert served[0]['meta']['validation']['ok'] is True


def test_a_clean_task_is_not_marked_with_gaps(isolated_app, tmp_path):
    """反方向：推断不能把每一件产物都标成带洞 —— 那样标记就没有信息量了。"""
    from src.services import artifact_store

    out = tmp_path / 'downloads' / 'map'
    task_id = _insert_map_task(out, status='completed', gap_tiles=0)
    _write_one_tile(out / f'task_{task_id}')

    client = isolated_app.app.test_client()
    resp = client.post(f'/api/export/map/{task_id}', json={'format': 'mbtiles'})

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['has_gaps'] is False
    rows = [a for a in artifact_store.list_artifacts('map', task_id)
            if a.kind.value == 'mbtiles']
    assert rows[0].has_gaps is False


def test_a_failed_verdict_is_recorded_and_logged_but_never_fails_the_export(
        isolated_app, tmp_path, monkeypatch, caplog):
    """校验有问题时：产物照登记、接口照回 200、判决落进 meta、日志点名问题。

    **为什么替身而不是真造一个坏库**：写入端与校验端是互相自洽的（tests/test_mbtiles.py
    钉了这一点），所以自产的库永远是 ok —— 想让真校验器说 not ok，只能在导出**之后**
    改库，而那时判决已经写完了，重新导出又会把改动覆盖掉。校验器自己认不认得出问题
    是 tests/test_mbtiles.py 的事（scheme='xyz' 那一组）；这里要钉的是**导出端拿到一份
    坏判决之后的行为**，所以在那个接缝上放替身才是对准的。

    这三条缺一条都是真缺陷：不落 meta 就等于「可自动校验」白做（判决仍然是未知）；
    抛异常会把一个**已经落盘、通常还能用**的成品变成一次失败的导出；不写日志的话
    用户在界面上只看到一个红点，排查不了。
    """
    import logging

    from src.services import artifact_store, mbtiles as mbtiles_mod

    bad = {'ok': False, 'tile_count': 1, 'minzoom': 10, 'maxzoom': 10,
           'format': 'png', 'bounds': None, 'vector_layers': [],
           'problems': ["metadata declares scheme='xyz'"]}
    monkeypatch.setattr(mbtiles_mod, 'validate_mbtiles', lambda path: dict(bad))

    out = tmp_path / 'downloads' / 'map'
    task_id = _insert_map_task(out, status='completed', gap_tiles=0)
    _write_one_tile(out / f'task_{task_id}')

    client = isolated_app.app.test_client()
    with caplog.at_level(logging.WARNING, logger='src.services.artifact_export'):
        resp = client.post(f'/api/export/map/{task_id}', json={'format': 'mbtiles'})

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['validation']['ok'] is False

    rows = [a for a in artifact_store.list_artifacts('map', task_id)
            if a.kind.value == 'mbtiles']
    assert len(rows) == 1, '校验不过不代表产物不该登记 —— 文件就在那里'
    assert rows[0].meta['validation']['ok'] is False
    assert rows[0].meta['validation']['problems'] == bad['problems']

    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("scheme='xyz'" in m for m in warnings), (
        '警告必须点名具体问题 —— 一句「校验失败」排查不了任何事')


def test_contour_gaps_come_from_failed_tiles(isolated_app, tmp_path):
    """判据看**列**不看管线：`contour_tasks` 没有 gap_tiles，它的等价事实是
    failed_tiles（渲染失败 = 成品上的洞）。同时钉住「显式传 False 是覆盖」。"""
    from src.core.database import get_connection
    from src.services import artifact_export

    out = tmp_path / 'downloads' / 'contour'
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO contour_tasks (name, status, north, south, east, west, "
            "contour_interval, zoom_min, zoom_max, output_path, total_tiles, "
            "rendered_tiles, failed_tiles) VALUES ('c', 'completed', 39.2, 39.0, "
            "116.2, 116.0, 10.0, 10, 10, ?, 4, 3, 1)", (str(out),))
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    _write_one_tile(out / f'contour_task_{task_id}' / 'contour_tiles')

    with isolated_app.app.app_context():
        inferred = artifact_export.export_task_mbtiles('contour', task_id)
        override = artifact_export.export_task_mbtiles('contour', task_id,
                                                      has_gaps=False)

    assert inferred['has_gaps'] is True, 'failed_tiles > 0 就是等高线的缺块事实'
    assert override['has_gaps'] is False, (
        '显式 False 必须是覆盖 —— 「没说」和「断言没洞」是两件事，'
        '这正是这个参数默认 None 而不是 False 的理由')
    assert inferred['validation']['ok'] is True


# --------------------------------------------------------- 读取路由的 ext


def test_mbtiles_route_refuses_an_extension_the_library_cannot_serve(
        isolated_app, tmp_path):
    """`.<ext>` 与库的 format 不符时 404，而不是照着别的扩展名发 PNG。

    以前 ext 被完全忽略：`.../1.pbf` 会拿到一张 PNG、Content-Type 还写着
    image/png —— 一个自相矛盾的响应，而且带着 `immutable` 被缓存一年。这条路由
    不转码（库里只有一种 format），所以说不出口的就别说。
    """
    out = tmp_path / 'downloads' / 'map'
    task_id = _insert_map_task(out, status='completed', gap_tiles=0)
    _write_one_tile(out / f'task_{task_id}')

    client = isolated_app.app.test_client()
    assert client.post(f'/api/export/map/{task_id}',
                       json={'format': 'mbtiles'}).status_code == 200

    assert client.get(f'/mbtiles/map/{task_id}/10/843/388.png').status_code == 200
    assert client.get(f'/mbtiles/map/{task_id}/10/843/388.pbf').status_code == 404
    assert client.get(f'/mbtiles/map/{task_id}/10/843/388.jpg').status_code == 404


# --------------------------------------------------- Gate 10：完成筛选与统计


@pytest.mark.parametrize('status_filter,expected', [
    ('completed', 2),            # 两个终态成品都算「完成」
    ('completed_with_gaps', 1),  # 精确状态值仍然是单值等值
    ('failed', 0),
    ('active', 0),
])
def test_completed_filter_covers_both_successful_terminal_states(
        isolated_app, tmp_path, status_filter, expected):
    """`completed_with_gaps` 必须出现在「完成」筛选里。

    §13-3 允许用户「接受缺块、导出部分成果」，那种成品的状态就叫
    completed_with_gaps。写 `status = 'completed'` 的实测后果是它一个 chip 都不
    匹配 —— 用户找不到自己的成品，等于那条产品决定白做。
    """
    out = tmp_path / 'downloads' / 'map'
    _insert_map_task(out, status='completed', gap_tiles=0)
    _insert_map_task(out, status='completed_with_gaps', gap_tiles=3)

    client = isolated_app.app.test_client()
    body = client.get(f'/api/history_all?status={status_filter}').get_json()

    assert body['pagination']['total_count'] == expected
    assert len(body['tasks']) == expected


def test_history_stats_counts_both_successful_terminal_states(isolated_app, tmp_path):
    """统计与筛选必须给同一个答案 —— 否则「完成 1」下面列着两条。"""
    out = tmp_path / 'downloads' / 'map'
    _insert_map_task(out, status='completed', gap_tiles=0)
    _insert_map_task(out, status='completed_with_gaps', gap_tiles=3)
    _insert_map_task(out, status='failed', gap_tiles=0)

    stats = isolated_app.app.test_client().get(
        '/api/history_stats').get_json()['stats']

    assert stats['completed'] == 2
    assert stats['failed'] == 1
    assert stats['total_tasks'] == 3


# ----------------------------------- 导出格式清单：这个任务**真能**导出哪些
#
# 改造前全仓没有任何 GET 能回答这个问题：格式表只在 400 的响应体里出现
# （`supported_formats`），而 `accepts(kind)` 的匹配又只在 POST 到达之后才做。
# 界面于是把 body 写死成 `{"format": "mbtiles"}` —— 后端把插件导出器并进了
# 同一条路由，用户却点不到任何别的格式。
#
# 这一组钉的是「清单是**这个任务**的事实，不是全局格式表的复制」：
#   · 只有瓦片目录 -> 只有 mbtiles（没有 GEOTIFF，GpkgExporter 收不下）；
#   · 登记了 GEOTIFF -> 多出 gpkg；
#   · 插件被禁 -> gpkg 消失（清单必须是运行期的，不能冻一份常量）；
#   · dem 任务 -> 空清单（既没有瓦片金字塔，也没有任何产物登记行）。


# in-tree 插件**默认关**（`registry._upsert_row` 插新行时写 enabled=0：启停是
# 用户的决定，不是发现的副产物）。所以格式选择器出厂状态下只有 mbtiles 一种，
# 界面手感与改造前一字不差 —— 想测到 gpkg 就得像用户那样先把它打开。
@pytest.fixture
def gpkg_on(isolated_app):
    from src.plugins import registry

    assert registry.get_record('gpkg') is not None, (
        'gpkg 导出器插件没被载入 —— 本组用例已失效')
    registry.set_enabled('gpkg', True)
    yield
    registry.set_enabled('gpkg', False)


def _export_formats(client, pipeline, task_id):
    resp = client.get(f'/api/export/{pipeline}/{task_id}/formats')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()['formats']


def _register(pipeline, task_id, kind, path, fmt):
    from src.contracts.artifact import Artifact
    from src.services import artifact_store

    artifact_store.record_artifact(Artifact(
        pipeline=pipeline, task_id=task_id, kind=kind, path=str(path), fmt=fmt))


def test_gpkg_is_listed_only_once_a_geotiff_is_registered(gpkg_on, isolated_app,
                                                          tmp_path):
    """`accepts()` 必须真的被问过，不能把全局格式表原样回给前端。

    in-tree 的 `GpkgExporter.accepts()` 只收 `GEOTIFF`。一个刚跑完、只有 XYZ
    目录的地图任务上 gpkg 是**没有货**的：列出来的后果不是「多一个选项」，是
    用户选了它之后撞一个 400（`_export_via_plugin` 找不到候选产物）。
    """
    from src.contracts.artifact import ArtifactKind

    out = tmp_path / 'downloads' / 'map'
    task_id = _insert_map_task(out, status='completed', gap_tiles=0)
    task_dir = out / f'task_{task_id}'
    _write_one_tile(task_dir)
    _register('map', task_id, ArtifactKind.XYZ_DIR, task_dir, 'png')

    client = isolated_app.app.test_client()
    assert _export_formats(client, 'map', task_id) == ['mbtiles'], (
        '只有瓦片目录的任务列出了 gpkg —— GpkgExporter.accepts() 没被问过')

    tif = task_dir / 'city_zoom_10.tif'
    tif.write_bytes(b'stand-in for a stitched GeoTIFF')
    _register('map', task_id, ArtifactKind.GEOTIFF, tif, 'tif')

    assert _export_formats(client, 'map', task_id) == ['mbtiles', 'gpkg'], (
        '登记了 GEOTIFF 之后 gpkg 仍然不在清单里 —— 那条读端点在照抄管线闸，'
        '没有看产物')


def test_disabling_the_exporter_plugin_drops_its_format(gpkg_on, isolated_app,
                                                       tmp_path):
    """清单必须是**运行期**的。插件可以在两次点击之间被禁用，冻一份常量就等于
    让下拉菜单显示上一轮的世界，而 POST 上去是 400。"""
    from src.contracts.artifact import ArtifactKind
    from src.plugins import registry

    out = tmp_path / 'downloads' / 'map'
    task_id = _insert_map_task(out, status='completed', gap_tiles=0)
    task_dir = out / f'task_{task_id}'
    _write_one_tile(task_dir)
    tif = task_dir / 'city_zoom_10.tif'
    tif.write_bytes(b'stand-in for a stitched GeoTIFF')
    _register('map', task_id, ArtifactKind.GEOTIFF, tif, 'tif')

    client = isolated_app.app.test_client()
    assert 'gpkg' in _export_formats(client, 'map', task_id)

    registry.set_enabled('gpkg', False)
    assert _export_formats(client, 'map', task_id) == ['mbtiles']


def test_a_dem_task_has_nothing_to_export(gpkg_on, isolated_app, tmp_path):
    """空清单是一个正确答案，不是错误。

    dem 既没有松散瓦片金字塔（不在 `_PIPELINE_TILE_LAYOUT` 里），也没有任何
    `artifacts` 登记行 —— `task_manager._register_artifacts` 里 pipeline 写死
    `'map'`，dem 落在盘上的颗粒 GeoTIFF 一件都没被登记过。所以「dem 可以导
    gpkg」这句话在当前代码里没有落地，清单必须照实说没有。
    """
    from src.core.database import get_connection

    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO dem_tasks (name, status, north, south, east, west, "
            "dataset, output_path, total_files, downloaded_files, failed_files) "
            "VALUES ('dem', 'completed', 1, 0, 1, 0, 'ASTGTM.003', ?, 1, 1, 0)",
            (str(tmp_path / 'downloads' / 'dem'),))
        conn.commit()
        dem_id = cur.lastrowid
    finally:
        conn.close()

    client = isolated_app.app.test_client()
    assert _export_formats(client, 'dem', dem_id) == []


def test_the_format_list_shares_the_pipeline_and_existence_gates_with_the_post(
        isolated_app, tmp_path):
    """两道闸与 POST 逐条一致。回一份看着能用的格式表给一个 404 的任务，
    等于让用户对着一条不存在的记录挑格式。"""
    client = isolated_app.app.test_client()

    bad = client.get('/api/export/nope/1/formats')
    assert bad.status_code == 400
    assert 'map' in bad.get_json()['supported_pipelines']

    assert client.get('/api/export/map/999999/formats').status_code == 404


def test_every_listed_format_really_exports(gpkg_on, isolated_app, tmp_path):
    """闭环：清单里的每一种格式 POST 上去都必须成功。

    这条是前面几条的反面 —— 只钉「不该出现的别出现」的话，一份**永远空**的
    清单也能全绿，而那时导出按钮在界面上等于消失了。
    """
    from src.contracts.artifact import ArtifactKind

    out = tmp_path / 'downloads' / 'map'
    task_id = _insert_map_task(out, status='completed', gap_tiles=0)
    task_dir = out / f'task_{task_id}'
    _write_one_tile(task_dir)
    # 真 GeoTIFF：gpkg 那条路要经过 GDAL 的 Translate，占位字节走不通。
    from osgeo import gdal
    tif = task_dir / 'city_zoom_10.tif'
    ds = gdal.GetDriverByName('GTiff').Create(str(tif), 4, 4, 1)
    ds.SetGeoTransform([116.0, 0.05, 0, 39.2, 0, -0.05])
    ds.GetRasterBand(1).Fill(7)
    ds = None
    _register('map', task_id, ArtifactKind.GEOTIFF, tif, 'tif')

    client = isolated_app.app.test_client()
    formats = _export_formats(client, 'map', task_id)
    assert formats == ['mbtiles', 'gpkg'], formats
    for fmt in formats:
        resp = client.post(f'/api/export/map/{task_id}', json={'format': fmt})
        assert resp.status_code == 200, (fmt, resp.get_data(as_text=True))
