"""底图瓦片转发路由 /basemap/<z>/<x>/<y>。

真实故障：底图让浏览器直连上游，用户那边拿到
    GET https://server.arcgisonline.com/.../1/0/1  403 (Forbidden)
    blocked by CORS policy: No 'Access-Control-Allow-Origin' header
两个问题叠在一起 ——

  1. 403 才是根因，CORS 报错只是它的副产品（错误页不带 CORS 头）。直连时
     真实状态码被浏览器的 CORS 消息盖住，看报错的人会去查一个不存在的问题。
  2. 更要命的是浏览器**不吃**项目的 proxy_url / 代理自动发现。底图和下载
     走的是两条不同的出网路径：后端能连、浏览器不能，就是这次的现场。

转发之后同源，CORS 从根上消失，且底图与下载共用 proxy_autodetect 一个入口。
这些断言守的就是这两条，外加不让 z/x/y 直接拼进上游地址。

上游请求全程打桩 —— 断言网络行为不能真的去打 Esri。
"""
import importlib
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core.config import Config  # noqa: E402

_PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


class _FakeUpstream:
    """替身 opener：记下 urllib 实际拿到的 Request，并返回可控响应。"""

    def __init__(self, body=_PNG, content_type='image/jpeg', raise_error=None):
        self.body = body
        self.content_type = content_type
        self.raise_error = raise_error
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.raise_error is not None:
            raise self.raise_error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body

    @property
    def headers(self):
        return {'Content-Type': self.content_type}


@pytest.fixture
def app_ctx(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    for mod in ('app', 'src.core.database'):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module('app')
    app_mod.app.config['TESTING'] = True
    route_mod = importlib.import_module('src.routes.basemap_static')
    return app_mod.app.test_client(), route_mod


def _stub(monkeypatch, route_mod, upstream, proxy=''):
    monkeypatch.setattr(route_mod.urllib.request, 'build_opener',
                        lambda *a, **k: upstream)
    monkeypatch.setattr(route_mod, 'resolve_from_config',
                        lambda cm, wait_s=None: proxy)


def test_tile_is_fetched_from_upstream_and_returned(app_ctx, monkeypatch):
    client, route_mod = app_ctx
    up = _FakeUpstream()
    _stub(monkeypatch, route_mod, up)

    r = client.get('/basemap/3/6/2')

    assert r.status_code == 200
    assert r.data == _PNG
    assert r.mimetype == 'image/jpeg'
    # 默认预设 Esri 的顺序是 /tile/{z}/{y}/{x} —— 行在列前。
    # 这个坑留在服务端，前端始终按常规 {z}/{x}/{y} 请求。
    assert up.requests[0].full_url.endswith('/tile/3/2/6')


def test_response_is_same_origin_so_cors_cannot_apply(app_ctx, monkeypatch):
    """转发的意义就在这里：响应从本站出，同源请求根本不走 CORS 检查。"""
    client, route_mod = app_ctx
    _stub(monkeypatch, route_mod, _FakeUpstream())
    r = client.get('/basemap/2/1/1')
    assert r.status_code == 200
    assert 'Access-Control-Allow-Origin' not in r.headers, (
        '同源响应不需要 CORS 头；加了反而说明有人还想让浏览器直连上游'
    )


def test_upstream_403_is_passed_through_not_swallowed(app_ctx, monkeypatch):
    """上游 403 必须如实传给浏览器。

    这正是用户看到的那个错误：直连时 403 被 CORS 消息盖住。同源之后
    浏览器能看到真实状态码，Cesium 也能报一条有意义的失败。
    """
    client, route_mod = app_ctx
    err = urllib.error.HTTPError('http://x', 403, 'Forbidden', {}, None)
    _stub(monkeypatch, route_mod, _FakeUpstream(raise_error=err))

    assert client.get('/basemap/1/0/1').status_code == 403


def test_upstream_network_failure_becomes_504(app_ctx, monkeypatch):
    client, route_mod = app_ctx
    _stub(monkeypatch, route_mod, _FakeUpstream(raise_error=OSError('no route')))
    assert client.get('/basemap/1/0/1').status_code == 504


def test_configured_proxy_is_applied(app_ctx, monkeypatch):
    """底图必须与下载共用同一个代理入口。

    这是转发方案的**主要**收益：改造前浏览器直连，proxy_url 对底图完全
    无效 —— 配好代理下载哗哗跑，底图还是蓝球。
    """
    client, route_mod = app_ctx
    up = _FakeUpstream()
    seen = {}
    monkeypatch.setattr(route_mod.urllib.request, 'ProxyHandler',
                        lambda mapping: seen.setdefault('mapping', mapping))
    _stub(monkeypatch, route_mod, up, proxy='http://127.0.0.1:7890')

    client.get('/basemap/2/1/1')

    assert seen['mapping'] == {'http': 'http://127.0.0.1:7890',
                               'https': 'http://127.0.0.1:7890'}


def test_no_proxy_disables_environment_proxies(app_ctx, monkeypatch):
    """代理为空时传空 dict，而不是不装 handler。

    ProxyHandler({}) 会关掉 urllib 对 HTTP_PROXY 等环境变量的隐式读取。
    代理的唯一事实源是 proxy_autodetect（它本来就把环境变量算作候选），
    这里再隐式吃一次会造成「配置页显示直连、实际走了环境变量代理」的分叉。
    """
    client, route_mod = app_ctx
    seen = {}
    monkeypatch.setattr(route_mod.urllib.request, 'ProxyHandler',
                        lambda mapping: seen.setdefault('mapping', mapping))
    _stub(monkeypatch, route_mod, _FakeUpstream(), proxy='')

    client.get('/basemap/2/1/1')

    assert seen['mapping'] == {}


def test_browser_user_agent_is_sent(app_ctx, monkeypatch):
    """上游把 UA 当风控信号；urllib 默认的 Python-urllib/3.x 最容易吃 403。"""
    client, route_mod = app_ctx
    up = _FakeUpstream()
    _stub(monkeypatch, route_mod, up)

    client.get('/basemap/2/1/1')

    ua = up.requests[0].get_header('User-agent') or ''
    assert 'Mozilla' in ua and 'urllib' not in ua.lower()


@pytest.mark.parametrize('path', [
    '/basemap/1/2/0',      # x 越界（z=1 只有 0..1）
    '/basemap/1/0/2',      # y 越界
    '/basemap/99/0/0',     # z 越界
])
def test_out_of_range_coordinates_are_rejected(app_ctx, monkeypatch, path):
    """z/x/y 会被拼进上游地址，越界值不该有机会跑到上游去。"""
    client, route_mod = app_ctx
    up = _FakeUpstream()
    _stub(monkeypatch, route_mod, up)

    assert client.get(path).status_code == 404
    assert up.requests == [], '越界坐标不该发起上游请求'


def test_configured_source_is_honoured(app_ctx, monkeypatch):
    """换了配置，转发就应该去新的上游 —— 不是把默认预设写死在路由里。"""
    client, route_mod = app_ctx
    up = _FakeUpstream()
    _stub(monkeypatch, route_mod, up)
    route_mod.config_manager.set('basemap_source',
                                 'https://example.com/t/{z}/{x}/{y}.png')

    client.get('/basemap/4/5/6')

    assert up.requests[0].full_url == 'https://example.com/t/4/5/6.png'


def test_index_page_points_the_basemap_at_the_local_route(app_ctx):
    client, _ = app_ctx
    html = client.get('/').get_data(as_text=True)
    assert '/basemap/{z}/{x}/{y}' in html
    assert 'arcgisonline' not in html, (
        '上游地址不该出现在页面里 —— 前端只认同源路径'
    )


def test_config_is_not_read_from_sqlite_on_every_tile(app_ctx, monkeypatch):
    """瓦片热路径不能每张都开 sqlite 连接。

    改造前 basemap_tile 每次请求都读三个配置项（basemap_source / tile_servers /
    default_style），而 ConfigManager.get 每次都新开一条连接 —— 首屏几十上百张
    瓦片就是几百次连接开销，并且与 _PROXY_WAIT_S 的线程占用叠加。同仓
    terrain_static 早就为同一个问题写了 5 秒 TTL 缓存并留了注释说明理由；
    这里现在缓存的是解析【结果】，顺带省掉每瓦片一次 resolve_basemap。

    断言 get 的调用次数而不是「有缓存对象」：后者在实现换成别的形态时会变成
    空断言，而调用次数直接就是这条缺陷的量纲。
    """
    client, route_mod = app_ctx
    _stub(monkeypatch, route_mod, _FakeUpstream())

    calls = []
    real_get = route_mod.config_manager.get
    monkeypatch.setattr(route_mod.config_manager, 'get',
                        lambda key, default=None: (calls.append(key),
                                                   real_get(key, default))[1])

    for x in range(8):
        assert client.get(f'/basemap/3/{x}/6').status_code == 200

    assert len(calls) <= 3, (
        f'8 张瓦片读了 {len(calls)} 次配置（{calls}）—— TTL 缓存没生效，'
        '每张瓦片都在开 sqlite 连接')


def test_changing_the_source_still_takes_effect_after_the_ttl(app_ctx, monkeypatch):
    """缓存不能把配置改动锁死 —— TTL 到期必须重读。

    没有失效钩子是有意的（同 terrain_static），代价是最多 TTL 秒延迟；
    但「永远不生效」是缺陷，所以这一条必须钉住。
    """
    client, route_mod = app_ctx
    up = _FakeUpstream()
    _stub(monkeypatch, route_mod, up)

    assert client.get('/basemap/1/0/1').status_code == 200
    first = up.requests[0].full_url

    from src.services.config_manager import ConfigManager
    ConfigManager().set('basemap_source', 'https://example.invalid/t/{z}/{x}/{y}.png')

    # 模拟 TTL 到期：把缓存条目的时间戳推到过去，而不是 sleep 5 秒
    with client.application.app_context():
        cached = client.application.extensions.get(route_mod._CACHE_KEY_SOURCE)
        assert cached is not None, '第一次请求应当已经填过缓存'
        client.application.extensions[route_mod._CACHE_KEY_SOURCE] = (
            cached[0] - route_mod._SOURCE_TTL_S - 1.0, cached[1])

    assert client.get('/basemap/1/0/1').status_code == 200
    assert up.requests[-1].full_url != first, 'TTL 到期后仍在用旧的上游地址'
    assert 'example.invalid' in up.requests[-1].full_url


def test_link_local_basemap_source_is_rejected_at_write_time():
    """`basemap_source` 不许指向链路本地段。

    这个值与其他配置项不同：`/basemap/{z}/{x}/{y}` 会**由服务端**去取它，并把
    上游响应体**原样回吐**给浏览器。所以一个指向 169.254.169.254 的模板等于把
    服务端当跳板去读云实例元数据 —— 实测过：靶机返回的正文完整地出现在
    /basemap/1/0/0 的响应里。

    只拦链路本地，**不**拦回环与私网：自建瓦片镜像住在 127.0.0.1 或
    192.168.x.x 是项目文档里就有的正当用法，而 169.254.x.x 从来不是一个瓦片
    服务地址。这条区分本身也钉在下面。
    """
    from src.services.basemap_source import validate_basemap_source

    ok, err = validate_basemap_source(
        'http://169.254.169.254/latest/meta-data/?a={z}{x}{y}')
    assert ok is False and err, '链路本地地址必须被拒'
    assert '169.254' in err, '报错要带上被拒的值'

    ok6, _ = validate_basemap_source('http://[fe80::1]/t/{z}/{x}/{y}.png')
    assert ok6 is False, 'IPv6 链路本地同样要拒'

    # 正当用法不能被顺手拦掉
    for good in ('http://127.0.0.1:8080/t/{z}/{x}/{y}.png',
                 'http://192.168.1.10/tiles/{z}/{x}/{y}.png',
                 'https://tiles.example.com/{z}/{x}/{y}.png'):
        ok_good, err_good = validate_basemap_source(good)
        assert ok_good is True, f'{good} 被误拒：{err_good}'


def test_link_local_upstream_is_refused_at_fetch_time(app_ctx, monkeypatch):
    """存量库里可能已经存着链路本地的值 —— 校验只管新写入，取瓦片时要再拦一次。

    直接把配置写进库（绕过校验，模拟升级前存下来的值），然后请求瓦片：
    必须 502 且**一次上游请求都不发**。
    """
    client, route_mod = app_ctx
    up = _FakeUpstream()
    _stub(monkeypatch, route_mod, up)

    from src.core.database import get_connection
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO config (key, value) VALUES ('basemap_source', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ('http://169.254.169.254/latest/meta-data/?a={z}{x}{y}',))
        conn.commit()
    finally:
        conn.close()

    assert client.get('/basemap/1/0/0').status_code == 502
    assert up.requests == [], '拒绝之后不该有任何上游请求发出去'
