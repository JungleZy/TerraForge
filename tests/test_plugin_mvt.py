"""MVT 管线插件：TileJSON 解析、下载循环、MBTiles 矢量写出、缺块语义。

全程用假 HTTP——不打真实网络（离线不变量同样约束测试）。URL 用 **IP 字面量**：
`ensure_fetchable_url` 对字面量不做 DNS（`url_guard.py:219-225`），而
`tiles.example.com` 那样的名字会走 getaddrinfo，离线机上整套用例会变红。
"""

import gzip
import json
import os
import sqlite3
import sys
import threading

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.contracts.region import RegionSpec
from src.contracts.reservation import ResourceKind
from src.plugins import registry
from src.plugins.builtin import mvt_pipeline
from src.plugins.task_context import TaskContext
from src.services.mbtiles import read_metadata, read_tile

#: 公网 IP 字面量：过得了 SSRF 闸，且不触发 DNS。
_HOST = 'https://93.184.216.34'

TILEJSON = {
    'tilejson': '3.0.0', 'name': 'demo',
    'tiles': [f'{_HOST}/{{z}}/{{x}}/{{y}}.pbf'],
    'minzoom': 0, 'maxzoom': 14,
    'vector_layers': [{'id': 'roads', 'fields': {'class': 'String'}}],
}

PBF_BYTES = b'\x1a\x0f\x0a\x05roads' * 10   # 假 pbf 字节，内容不参与判定


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张真库：DATABASE_PATH 指到 tmp_path 后 init_database() 建全。

    conftest.py 没有 `db` fixture（只有 autouse 的隔离夹具），按
    tests/test_plugin_task_context.py:22-42 的既有写法在本文件里建一个。
    """
    from src.core import config as config_mod

    path = tmp_path / "data" / "map_downloader.db"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(config_mod.Config, "DATABASE_PATH", path)
    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_mod.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config_mod.Config, "CACHE_DIR", tmp_path / "cache")

    from src.core.database import init_database

    init_database()
    return path


class _FakeResponse:
    def __init__(self, status, body=b''):
        self.status = status
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """`handler(url)` → 上下文管理器。记录全部请求过的 URL。"""

    def __init__(self, handler):
        self._handler = handler
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return self._handler(url)


class _CM:
    """配置替身：关掉代理自动探测。

    不关的话 `ctx.proxy_url()` 会起后台探测线程并等到 25 秒超时
    （`proxy_autodetect.resolve_proxy_url`）——既慢又真的出网。
    """

    def get(self, key, default=None):
        return 'false' if key == 'proxy_auto_detect' else (default or '')


def _ctx(db, tmp_path, params, granted=4):
    return TaskContext(
        task_id=1, plugin_id='mvt',
        region=RegionSpec.from_bbox(31.0, 30.0, 121.1, 121.0),
        params=params, output_dir=tmp_path / 'out', snapshot=None,
        stop_flag=threading.Event(), tlog=None, emit_progress=None,
        granted={ResourceKind.NETWORK: granted}, config_manager=_CM())


def _seed_task(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO plugin_tasks (id, plugin_id, name, status)"
                 " VALUES (1, 'mvt', 't', 'running')")
    conn.commit()
    conn.close()


def _install(monkeypatch, handler):
    session = _FakeSession(handler)
    monkeypatch.setattr(mvt_pipeline, '_open_session', lambda ctx: session)
    return session


def _tilejson_then(body_for_tile, tilejson=None):
    payload = json.dumps(tilejson or TILEJSON).encode()

    def handler(url):
        if url.endswith('t.json'):
            return _FakeResponse(200, payload)
        return body_for_tile(url)

    return handler


def _params(**over):
    params = {'tilejson_url': f'{_HOST}/t.json', 'zoom_min': 3, 'zoom_max': 3,
              'name': 'demo'}
    params.update(over)
    return params


def _tile_urls(session):
    return [u for u in session.urls if u.endswith('.pbf')]


def _statuses(db):
    conn = sqlite3.connect(db)
    rows = conn.execute('SELECT DISTINCT status FROM plugin_task_tiles'
                        ' WHERE task_id = 1').fetchall()
    conn.close()
    return sorted(r[0] for r in rows)


def test_plugin_loads(db):
    registry.reset_for_tests()
    registry.load_all()
    rec = registry.get_record('mvt')
    assert rec is not None and rec.load_error == ''
    assert 'pipeline' in rec.manifest.capabilities


def test_run_writes_pbf_mbtiles(db, tmp_path, monkeypatch):
    _seed_task(db)
    _install(monkeypatch, _tilejson_then(lambda url: _FakeResponse(200, PBF_BYTES)))

    outcome = mvt_pipeline.MvtPipeline().run(_ctx(db, tmp_path, _params()))

    assert outcome.value == 'completed'
    mbtiles = list((tmp_path / 'out').glob('*.mbtiles'))
    assert mbtiles, 'MBTiles 未产出'
    meta = read_metadata(mbtiles[0])
    assert meta['format'] == 'pbf'
    # 矢量库必须带 json 键描述图层，否则 MapLibre/tileserver 打得开、什么也不显示
    assert json.loads(meta['json'])['vector_layers'][0]['id'] == 'roads'
    # bbox(31,30,121.1,121.0) 在 z3 命中瓦片 (x=6, y=3)。按 XYZ 取回 ==
    # 写进去的字节，说明 TMS 行号翻转没写反。
    assert read_tile(mbtiles[0], 3, 6, 3) == PBF_BYTES
    # 打包成功后暂存区即删：瓦片字节已经在库里，留着就是双份占用
    assert not (tmp_path / 'out' / '.staging').exists()

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT kind, format, has_gaps, meta FROM artifacts"
                       " WHERE pipeline = 'plugin' AND task_id = 1").fetchone()
    conn.close()
    assert row[:3] == ('mbtiles', 'pbf', 0)
    # 宿主的 register_artifact 对文件形态只量得到字节数，瓦片数/层级只能走 meta
    art_meta = json.loads(row[3])
    assert art_meta['tile_count'] == 1
    assert (art_meta['minzoom'], art_meta['maxzoom']) == (3, 3)
    assert art_meta['vector_layers'][0]['id'] == 'roads'


def test_404_is_no_data_not_failure(db, tmp_path, monkeypatch):
    _seed_task(db)
    _install(monkeypatch, _tilejson_then(lambda url: _FakeResponse(404)))

    outcome = mvt_pipeline.MvtPipeline().run(
        _ctx(db, tmp_path, _params(name='')))

    # 全是 no_data：completed_with_gaps（§13-3：no_data 是已解释的缺块）
    assert outcome.value == 'completed_with_gaps'
    assert _statuses(db) == ['no_data']
    # 一块都没下到 → 不产出空库（bounds/minzoom/maxzoom 推不出来，不合规）
    assert not list((tmp_path / 'out').glob('*.mbtiles'))


def test_500_is_pending_decision(db, tmp_path, monkeypatch):
    _seed_task(db)
    _install(monkeypatch, _tilejson_then(lambda url: _FakeResponse(500)))

    outcome = mvt_pipeline.MvtPipeline().run(_ctx(db, tmp_path, _params()))

    assert outcome.value == 'pending_decision'
    assert _statuses(db) == ['retryable_failure']
    # 默认严格：有未解释的洞就不产出产物
    assert not list((tmp_path / 'out').glob('*.mbtiles'))


def test_other_4xx_is_permanent_failure(db, tmp_path, monkeypatch):
    _seed_task(db)
    _install(monkeypatch, _tilejson_then(lambda url: _FakeResponse(403)))

    outcome = mvt_pipeline.MvtPipeline().run(_ctx(db, tmp_path, _params()))

    assert outcome.value == 'pending_decision'
    assert _statuses(db) == ['permanent_failure']


def test_gap_accepted_skips_download_and_packages(db, tmp_path, monkeypatch):
    """用户点「接受缺块」后的收尾那一趟：不许重下，直接用暂存区打包。"""
    _seed_task(db)
    # z3 一块成功、z4 一块 500 → pending_decision，暂存区里留着 z3 那块
    first = _install(monkeypatch, _tilejson_then(
        lambda url: _FakeResponse(200, PBF_BYTES) if '/3/' in url
        else _FakeResponse(500)))
    params = _params(zoom_max=4)
    assert mvt_pipeline.MvtPipeline().run(
        _ctx(db, tmp_path, params)).value == 'pending_decision'
    assert len(_tile_urls(first)) == 2
    assert (tmp_path / 'out' / '.staging' / '3' / '6' / '3.pbf').exists()

    def _boom(url):
        raise AssertionError(f'接受缺块后仍然重下了瓦片：{url}')

    second = _install(monkeypatch, _tilejson_then(_boom))
    outcome = mvt_pipeline.MvtPipeline().run(
        _ctx(db, tmp_path, dict(params, _gap_accepted=True)))

    assert outcome.value == 'completed_with_gaps'
    assert _tile_urls(second) == []
    mbtiles = list((tmp_path / 'out').glob('*.mbtiles'))
    assert mbtiles and read_tile(mbtiles[0], 3, 6, 3) == PBF_BYTES
    conn = sqlite3.connect(db)
    has_gaps = conn.execute("SELECT has_gaps FROM artifacts WHERE task_id = 1"
                            " AND pipeline = 'plugin'").fetchone()[0]
    conn.close()
    assert has_gaps == 1, '带洞的产物必须登记成 has_gaps'


def test_gzip_body_is_stored_verbatim(db, tmp_path, monkeypatch):
    """已知取舍：落库的就是 `resp.read()` 给的字节。

    生产里 aiohttp 会把 `Content-Encoding: gzip` 透明解压，所以库里通常是
    **未压缩**的 pbf；服务器不声明编码直接吐压缩字节时则原样入库。两种都合法，
    写入端对 pbf 不做魔数校验（`mbtiles.py:94-96`）。
    """
    _seed_task(db)
    packed = gzip.compress(PBF_BYTES)
    _install(monkeypatch, _tilejson_then(lambda url: _FakeResponse(200, packed)))

    outcome = mvt_pipeline.MvtPipeline().run(_ctx(db, tmp_path, _params()))

    assert outcome.value == 'completed'
    mbtiles = list((tmp_path / 'out').glob('*.mbtiles'))
    assert read_tile(mbtiles[0], 3, 6, 3) == packed


def test_empty_body_counts_as_no_data(db, tmp_path, monkeypatch):
    """200 + 空体 = 这块没有要素。记 no_data，不能报成功——`add_tile` 拒收
    0 字节瓦片，报成功会让任务看着干净而库里少一块。"""
    _seed_task(db)
    _install(monkeypatch, _tilejson_then(lambda url: _FakeResponse(200, b'')))

    outcome = mvt_pipeline.MvtPipeline().run(_ctx(db, tmp_path, _params()))

    assert outcome.value == 'completed_with_gaps'
    assert _statuses(db) == ['no_data']


def test_tms_scheme_flips_only_the_requested_row(db, tmp_path, monkeypatch):
    """TileJSON 的 `scheme: tms` 只影响 URL 里的 {y}；库里仍按 XYZ 存。"""
    _seed_task(db)
    session = _install(monkeypatch, _tilejson_then(
        lambda url: _FakeResponse(200, PBF_BYTES),
        tilejson=dict(TILEJSON, scheme='tms')))

    outcome = mvt_pipeline.MvtPipeline().run(_ctx(db, tmp_path, _params()))

    assert outcome.value == 'completed'
    # z3 的 XYZ y=3 → TMS 行号 2^3-1-3 = 4
    assert _tile_urls(session) == [f'{_HOST}/3/6/4.pbf']
    mbtiles = list((tmp_path / 'out').glob('*.mbtiles'))
    assert read_tile(mbtiles[0], 3, 6, 3) == PBF_BYTES


def test_zoom_range_outside_tilejson_fails_loudly(db, tmp_path, monkeypatch):
    """请求层级完全在上游声明之外：那是参数错，不是几十万次 404。"""
    _seed_task(db)
    session = _install(monkeypatch, _tilejson_then(
        lambda url: _FakeResponse(200, PBF_BYTES),
        tilejson=dict(TILEJSON, minzoom=0, maxzoom=2)))

    with pytest.raises(RuntimeError, match='TileJSON'):
        mvt_pipeline.MvtPipeline().run(
            _ctx(db, tmp_path, _params(zoom_min=5, zoom_max=6)))
    assert _tile_urls(session) == []


def test_product_name_cannot_escape_output_dir(db, tmp_path, monkeypatch):
    """`name` 是用户参数：直接拼进路径会让产物落到 output_dir 之外。"""
    _seed_task(db)
    _install(monkeypatch, _tilejson_then(lambda url: _FakeResponse(200, PBF_BYTES)))

    outcome = mvt_pipeline.MvtPipeline().run(
        _ctx(db, tmp_path, _params(name='../../evil')))

    assert outcome.value == 'completed'
    assert list((tmp_path / 'out').glob('*.mbtiles')), '产物没落在任务目录里'
    assert not list(tmp_path.glob('*.mbtiles'))
    assert not list(tmp_path.parent.glob('evil*.mbtiles'))


def test_concurrency_never_exceeds_granted_network(db, tmp_path, monkeypatch):
    """并发上限只来自调度器配额，而且真的并发（不是逐块 await）。"""
    _seed_task(db)
    import asyncio

    state = {'now': 0, 'peak': 0}

    class _Tracked(_FakeResponse):
        async def __aenter__(self):
            state['now'] += 1
            state['peak'] = max(state['peak'], state['now'])
            await asyncio.sleep(0.01)
            return self

        async def __aexit__(self, *a):
            state['now'] -= 1
            return False

    session = _install(monkeypatch, _tilejson_then(
        lambda url: _Tracked(200, PBF_BYTES)))
    ctx = TaskContext(
        task_id=1, plugin_id='mvt',
        # z8 上覆盖 4 列 x 2 行 = 8 块，足够看出池子的上界
        region=RegionSpec.from_bbox(32.0, 30.0, 122.0, 118.0),
        params=_params(zoom_min=8, zoom_max=8), output_dir=tmp_path / 'out',
        snapshot=None, stop_flag=threading.Event(), tlog=None,
        emit_progress=None, granted={ResourceKind.NETWORK: 3},
        config_manager=_CM())

    assert mvt_pipeline.MvtPipeline().run(ctx).value == 'completed'
    assert len(_tile_urls(session)) >= 8
    assert state['peak'] == 3, f"在飞请求数应恰好顶到配额 3，实际 {state['peak']}"


def test_estimate_counts_tiles_and_publishes_assumptions(tmp_path):
    region = RegionSpec.from_bbox(31.0, 30.0, 121.1, 121.0)
    est = mvt_pipeline.MvtPipeline().estimate(
        {'zoom_min': 3, 'zoom_max': 4}, region)

    assert est.tile_count == 2                      # z3 一块 + z4 一块
    assert est.cache_bytes == 0                     # 暂存区不进共享缓存
    assert est.peak_bytes == est.temp_bytes + est.output_bytes
    assert est.detail['tiles_by_zoom'] == {'3': 1, '4': 1}
    assert est.detail['assumptions']
