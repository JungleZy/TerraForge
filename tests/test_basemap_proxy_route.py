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
import re
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import fresh_import  # noqa: E402
from src.core.config import Config  # noqa: E402
from src.services import proxy_autodetect  # noqa: E402
from src.services.basemap_source import (  # noqa: E402
    BASEMAP_PRESETS, DOWNLOAD_SOURCE, fallback_candidates, resolve_basemap,
    source_version,
)


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
    # create_app() 会起一条 proxy-autodetect 后台线程，而它探代理用的正是
    # urllib.request.build_opener —— 与下面 _stub 打的是同一个 stdlib 符号
    # （route_mod.urllib 就是 urllib 包本身）。任何有代理候选的机器上
    # （设了 HTTP_PROXY，或本地开着 7890）那条线程都会抢到替身 opener，
    # 把自己的探测 URL 追加进 up.requests —— `up.requests == []`、
    # `len(up.requests) == 1` 这些断言全变成竞态，而且只在部分机器上翻红
    # （发布流程三个平台都跑一遍）。探测对本文件毫无价值：代理值一律由
    # _stub 直接给定，所以起手就把它关掉。
    monkeypatch.setattr(proxy_autodetect, 'start_background_autodetect',
                        lambda *a, **k: False)
    # 出网路径回退（_egress_paths）只在「默认路径本来就会用上代理」时才追加
    # 一条强制直连，判据是 getproxies()。跑测试的机器上 export 了 HTTP_PROXY
    # （或 Windows 上开着系统代理）就会凭空多出一次上游请求，断言 up.calls /
    # up.requests 的用例全变成看环境脸色 —— 与上面关掉 autodetect 同一类处理。
    # 起手清空环境变量，并把 getproxies 钉成「只读环境变量」的那个实现：
    # 三个平台行为一致，而需要环境变量代理的用例自己 setenv 就能拿回来
    # （test_no_configured_proxy_leaves_the_environment_to_urllib）。
    for var in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(urllib.request, 'getproxies',
                        urllib.request.getproxies_environment)
    # fresh_import 而不是裸 sys.modules.pop：裸 pop 不还原，会把绑在已删除
    # tmp_path 上的 app 实例留给后面的测试文件（conftest 开篇的 M23）。
    app_mod = fresh_import(monkeypatch, 'app', 'src.core.database')[0]
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


def test_no_configured_proxy_leaves_the_environment_to_urllib(app_ctx, monkeypatch):
    """代理为空时**不装** ProxyHandler，让 urllib 照默认行为读环境变量。

    这条口径必须与下载路径一致：download_engine 的 aiohttp 开着
    trust_env=True，proxy_url 为空时下载照样吃 HTTP(S)_PROXY。这里曾经传
    ProxyHandler({})（**关掉**环境变量），于是「export 了 HTTP_PROXY 又把代理
    自动发现关掉」的 WSL 用户（文档里写着的工作流）得到的是「下载正常、底图
    一颗蓝球」—— 正是这条路由存在的理由所要消灭的那种分叉。

    断言落在**真的会被拿去取瓦片的那个 opener** 上，而不是「没传某个参数」：
    后者是实现形状，换个写法就变成空断言。
    """
    client, route_mod = app_ctx
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:7899')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7899')
    up = _FakeUpstream()
    real_build_opener = route_mod.urllib.request.build_opener
    built = []

    def spy(*handlers):
        built.append(real_build_opener(*handlers))
        return up

    monkeypatch.setattr(route_mod.urllib.request, 'build_opener', spy)
    monkeypatch.setattr(route_mod, 'resolve_from_config', lambda cm, wait_s=None: '')

    assert client.get('/basemap/2/1/1').status_code == 200

    proxies = [h.proxies for h in built[0].handlers
               if isinstance(h, urllib.request.ProxyHandler)]
    assert proxies, 'opener 里连默认 ProxyHandler 都没有 —— 环境变量代理被掐掉了'
    assert proxies[0].get('http') == 'http://127.0.0.1:7899', (
        f'取瓦片的 opener 没读到环境变量代理：{proxies[0]}')


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
    # TTL 必须大到这 8 次请求不可能跨过去：真 5 秒下这条用例是在赌墙钟
    # ——机器一慢（CI、并行跑）缓存就在中途过期重填，calls 变成 6 而断言翻红，
    # 与被测的那条缺陷毫无关系。下面 test_changing_the_source... 已经单独
    # 钉住「TTL 到期要重读」，这里只关心命中期内的读库次数。
    monkeypatch.setattr(route_mod, '_SOURCE_TTL_S', 1e9)

    calls = []
    real_get = route_mod.config_manager.get
    monkeypatch.setattr(route_mod.config_manager, 'get',
                        lambda key, default=None: (calls.append(key),
                                                   real_get(key, default))[1])

    for x in range(8):
        assert client.get(f'/basemap/3/{x}/6').status_code == 200

    assert len(calls) <= 4, (
        f'8 张瓦片读了 {len(calls)} 次配置（{calls}）—— TTL 缓存没生效，'
        '每张瓦片都在开 sqlite 连接')
    # 上限从 3 调到 4：磁盘缓存开关（cache_enabled）也走同一个 TTL 缓存
    # （_disk_cache_enabled），首轮多一次读库，之后同样零读。量纲不变：
    # 读库次数必须恒定，绝不随瓦片数增长。


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



# ---------------------------------------------------------------- 自动回退链
#
# 真实故障（2026-08）：Esri 的 CDN(Akamai) 封了用户代理的出口 IP，每块底图瓦片
# 403；而这台机器上 Google 只有走代理才通 —— 两张卫星图分属两条网络路径。
# 结果是配置里选着 Esri 的用户对着一颗蓝色地球，日志里有 403 但界面上没有。

class _PerHostUpstream:
    """按上游主机分别决定成功/失败的替身 opener。

    routes 里 build_opener 只调一次拿到 opener、之后每个候选各 open 一次，
    所以一个实例就能覆盖「第一张源挂了、第二张通」这条链。
    """

    def __init__(self, failures, blocked=()):
        self.failures = failures        # {url 子串: 要抛的异常}
        # url 子串：回 200 + image/png + x-blocked。这是 OSM 拒绝一个违反瓦片
        # 使用政策的调用方时的答复形态 —— 不是错误码，是一张写着
        # "Access blocked" 的、能正常渲染的图。
        self.blocked = tuple(blocked)
        self.requests = []
        self.sent = []                  # 完整的 Request 对象（要看请求头时用）
        self._blocked_now = False

    def open(self, request, timeout=None):
        self.requests.append(request.full_url)
        self.sent.append(request)
        for needle, error in self.failures.items():
            if needle in request.full_url:
                raise error
        self._blocked_now = any(n in request.full_url for n in self.blocked)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return _PNG

    @property
    def headers(self):
        h = {'Content-Type': 'image/jpeg'}
        if self._blocked_now:
            h['x-blocked'] = ('Access denied. See '
                              'https://operations.osmfoundation.org/policies/tiles/')
        return h


def _esri_403():
    return urllib.error.HTTPError('http://x', 403, 'Forbidden', {}, None)


def _http_error(code, reason='Upstream said so'):
    return urllib.error.HTTPError('http://x', code, reason, {}, None)


def test_auto_fallback_only_offers_wgs84_sources():
    """回退链是自动的、无人确认的 —— 放一张 GCJ-02 的图进去，用户会在偏移
    100-700 米的底图上框选而毫不知情。google_roadmap（lyrs=m）因此必须缺席。"""
    chain = fallback_candidates(resolve_basemap('esri'))
    assert [c['source'] for c in chain][0] == 'esri', '配置的源永远排第一'
    for candidate in chain[1:]:
        assert BASEMAP_PRESETS[candidate['source']]['wgs84'], (
            f"{candidate['source']} 不是 WGS-84，不能进自动回退链")
    assert 'google_roadmap' not in [c['source'] for c in chain]


def test_blocked_source_falls_back_to_the_next_one(app_ctx, monkeypatch):
    client, route_mod = app_ctx
    up = _PerHostUpstream({'arcgisonline.com': _esri_403()})
    _stub(monkeypatch, route_mod, up)

    r = client.get('/basemap/2/1/1')

    assert r.status_code == 200 and r.data == _PNG
    assert 'arcgisonline.com' in up.requests[0], '配置的源必须先试'
    assert 'googleapis.com' in up.requests[1], '再退到链上的下一张卫星图'


def test_openstreetmap_is_asked_with_an_identifying_user_agent(app_ctx, monkeypatch):
    """OSM 只能用能识别应用的 UA 去要，其余上游仍要浏览器 UA。

    真实故障（2026-08）：Esri 403 之后回退到链尾的 OSM，而 OSM 收到的是伪造的
    Chrome UA —— 它的瓦片使用政策明写「伪造别的应用的 UA 会被封」，于是回了
    HTTP 200 + image/png + x-blocked，图上印着 "Access blocked"。用户看到的是
    一张能正常渲染的假地图。反过来把浏览器 UA 一起换掉也不行：Esri 与 Google
    把 UA 当风控信号，非浏览器 UA 直接 403（正是 _UA 存在的理由）。
    两条约束方向相反，所以这里按主机各钉一遍。
    """
    client, route_mod = app_ctx
    up = _PerHostUpstream({'arcgisonline.com': _esri_403(),
                           'googleapis.com': _esri_403()})
    _stub(monkeypatch, route_mod, up)

    assert client.get('/basemap/2/1/1').status_code == 200

    osm = [r.get_header('User-agent') for r in up.sent
           if 'openstreetmap.org' in r.full_url]
    esri = [r.get_header('User-agent') for r in up.sent
            if 'arcgisonline.com' in r.full_url]
    assert osm and esri, '这条用例要求回退链真的走到了 OSM'
    assert 'Mozilla' not in osm[0], 'OSM 上伪造浏览器 UA 会被封'
    assert 'TerraForge' in osm[0], 'UA 必须能认出是哪个应用'
    assert esri[0] == route_mod._UA, 'Esri/Google 那边非浏览器 UA 会吃 403'


def test_a_200_that_carries_x_blocked_is_not_a_tile(app_ctx, monkeypatch):
    """上游用 200 回一张「拒绝访问」的图，必须当失败，且绝不能落盘。

    落盘是这个 bug 最恶的一半：blocked 图是合法的 image/png，写进
    cache/basemap 之后每次都缓存命中、再也不回源 —— 就算 UA 已经改对，
    那张 "Access blocked" 也会永远挂在地图上。
    """
    client, route_mod = app_ctx
    up = _PerHostUpstream({'arcgisonline.com': _esri_403(),
                           'googleapis.com': _esri_403()},
                          blocked=('openstreetmap.org',))
    _stub(monkeypatch, route_mod, up)

    r = client.get('/basemap/2/1/1')

    assert r.status_code == 403, '整条链都不可用时如实报配置源的状态码'
    assert r.data != _PNG, '被拒的图不能当瓦片发给浏览器'
    tile_dir = Config.CACHE_DIR / 'basemap'
    assert not (tile_dir.exists() and list(tile_dir.iterdir())), (
        '被拒的图落了盘就再也回不了源')


# ------------------------------------------------------------ 出网路径回退
#
# 实测 2026-08-11，同一台机器同一分钟：
#   Esri   走代理 403 AkamaiGHost（封出口 IP） / 直连 200
#   Google 走代理 200                          / 直连 超时
#   OSM    走代理 200                          / 直连 超时
# 代理是全局一个值，所以无论配不配都有一家取不到 —— 挂掉的不是源，是那条路，
# 换供应商永远解决不了它。回退是最后手段（它会静默改变用户看到的图），
# 换路径不会，所以换路径必须排在换源前面。

class _EgressUpstream:
    """按「这次走没走代理」决定成败的替身 opener。

    build_opener(*handlers) 收到的 ProxyHandler 就是本次的出网路径：
    proxies 非空 = 走代理，空 dict = 强制直连，没有 handler = 默认路径。
    """

    def __init__(self, failures):
        self.failures = failures        # {'proxy'|'direct'|'default': 要抛的异常}
        self.calls = []                 # [(url, 路径名)]
        self._path = 'default'

    def build_opener(self, *handlers):
        self._path = 'default'
        for h in handlers:
            if isinstance(h, urllib.request.ProxyHandler):
                self._path = 'proxy' if h.proxies else 'direct'
        return self

    def open(self, request, timeout=None):
        self.calls.append((request.full_url, self._path))
        error = self.failures.get(self._path)
        if error is not None:
            raise error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return _PNG

    @property
    def headers(self):
        return {'Content-Type': 'image/jpeg'}


def _stub_egress(monkeypatch, route_mod, upstream, proxy):
    monkeypatch.setattr(route_mod.urllib.request, 'build_opener',
                        upstream.build_opener)
    # ''.join 是有意的：每次返回一个**新的**等值 str 对象，与真实环境一致
    # （代理地址每次从 sqlite 读出来都是新对象）。直接返回字面量会走常量池，
    # 让「路径去重写成 is 比较」这个 bug 在测试里隐身
    # （见 test_the_remembered_path_is_not_tried_twice）。
    monkeypatch.setattr(route_mod, 'resolve_from_config',
                        lambda cm, wait_s=None: ''.join(proxy))


def test_a_source_unreachable_through_the_proxy_is_retried_direct(app_ctx, monkeypatch):
    """代理 403、直连 200：换路径就够了，不该换供应商。"""
    client, route_mod = app_ctx
    up = _EgressUpstream({'proxy': _esri_403()})
    _stub_egress(monkeypatch, route_mod, up, 'http://127.0.0.1:7890')

    r = client.get('/basemap/2/1/1')

    assert r.status_code == 200 and r.data == _PNG
    assert [p for _, p in up.calls] == ['proxy', 'direct']
    assert all('arcgisonline.com' in u for u, _ in up.calls), (
        '路径还没试完就换源，等于让用户看一张他没选的图')
    bm = client.get('/api/basemap').get_json()['basemap']
    assert bm['fallback'] is False and bm['source'] == 'esri'


def test_the_working_egress_path_is_remembered(app_ctx, monkeypatch):
    """否则每张瓦片都要先吃一次代理 403 —— 一次完整 TCP+TLS 往返，首屏几十张。"""
    client, route_mod = app_ctx
    up = _EgressUpstream({'proxy': _esri_403()})
    _stub_egress(monkeypatch, route_mod, up, 'http://127.0.0.1:7890')

    client.get('/basemap/2/1/1')
    up.calls.clear()
    client.get('/basemap/2/1/2')

    assert [p for _, p in up.calls] == ['direct'], '成功过的那条路径要排第一'


def test_the_remembered_path_is_not_tried_twice(app_ctx, monkeypatch):
    """记住的那条路径要顶到队首，而不是在队列里多出一份。

    代理这一支最容易踩：配置里的代理地址每次读出来都是新的 str 对象，去重
    若按 identity 比就形同没做，那条路径被试两次 —— 每张瓦片白吃一次完整
    往返，而且只在真实环境发作。
    """
    client, route_mod = app_ctx
    up = _EgressUpstream({})
    _stub_egress(monkeypatch, route_mod, up, 'http://127.0.0.1:7890')

    client.get('/basemap/2/1/1')            # 代理这条路通，记下它
    up.failures = {'proxy': _esri_403()}    # 随后代理挂了
    up.calls.clear()
    client.get('/basemap/2/1/2')

    assert [p for u, p in up.calls if 'arcgisonline.com' in u] == ['proxy', 'direct']


def test_no_second_path_is_tried_when_there_is_no_proxy(app_ctx, monkeypatch):
    """没有代理时两条路径完全等价，重试只是白等一个 _TIMEOUT_S。"""
    client, route_mod = app_ctx
    up = _EgressUpstream({'default': _http_error(500)})
    _stub_egress(monkeypatch, route_mod, up, '')

    client.get('/basemap/2/1/1')

    esri = [c for c in up.calls if 'arcgisonline.com' in c[0]]
    assert len(esri) == 1 and esri[0][1] == 'default'


def test_the_source_is_dropped_only_after_every_path_failed(app_ctx, monkeypatch):
    client, route_mod = app_ctx
    up = _EgressUpstream({'proxy': _esri_403(), 'direct': _http_error(500)})
    _stub_egress(monkeypatch, route_mod, up, 'http://127.0.0.1:7890')

    r = client.get('/basemap/2/1/1')

    assert [p for _, p in up.calls[:2]] == ['proxy', 'direct'], '两条路都试过才算源挂'
    assert 'googleapis.com' in up.calls[2][0], '之后才轮到换供应商'
    assert r.status_code == 403, (
        '整条链都不可用时，用户要知道的是他选的那张图在他当前的出网路径上'
        '怎么了 —— 报第一条路径的状态码，不是最后一次失败的')


def test_fallback_is_reported_to_the_client(app_ctx, monkeypatch):
    """底图默默换了一张而界面不说，正是本项目最不能接受的那种静默。"""
    client, route_mod = app_ctx
    _stub(monkeypatch, route_mod, _PerHostUpstream({'arcgisonline.com': _esri_403()}))

    client.get('/basemap/2/1/1')
    bm = client.get('/api/basemap').get_json()['basemap']

    assert bm['fallback'] is True
    assert bm['source'] == 'google_satellite'
    assert bm['configured_source'] == 'esri'
    # max_level / 署名必须跟着实际那张图走，否则放大层数和版权字样都是错的
    assert bm['max_level'] == 21
    assert bm['credit'] == '© Google'


def test_working_source_reports_no_fallback(app_ctx, monkeypatch):
    client, route_mod = app_ctx
    _stub(monkeypatch, route_mod, _PerHostUpstream({}))

    client.get('/basemap/2/1/1')
    bm = client.get('/api/basemap').get_json()['basemap']

    assert bm['fallback'] is False
    assert bm['source'] == bm['configured_source'] == 'esri'


def test_a_dead_source_is_not_retried_on_every_tile(app_ctx, monkeypatch):
    """没有冷却的话首屏几十张瓦片每张都要先把挂掉的源整整超时一遍。"""
    client, route_mod = app_ctx
    up = _PerHostUpstream({'arcgisonline.com': _esri_403()})
    _stub(monkeypatch, route_mod, up)

    client.get('/basemap/2/1/1')
    up.requests.clear()
    client.get('/basemap/2/1/2')

    assert all('arcgisonline.com' not in u for u in up.requests), (
        '冷却期内不该再碰挂掉的源')
    assert len(up.requests) == 1


def test_the_configured_source_is_retried_after_the_cooldown(app_ctx, monkeypatch):
    """上游恢复了要能自己回来，不能等用户重启程序。"""
    client, route_mod = app_ctx
    monkeypatch.setattr(route_mod, '_COOLDOWN_S', 0.0)
    up = _PerHostUpstream({'arcgisonline.com': _esri_403()})
    _stub(monkeypatch, route_mod, up)
    client.get('/basemap/2/1/1')

    up.failures.clear()          # Esri 恢复
    up.requests.clear()
    r = client.get('/basemap/2/1/2')

    assert r.status_code == 200
    assert 'arcgisonline.com' in up.requests[0]
    assert client.get('/api/basemap').get_json()['basemap']['fallback'] is False


def test_whole_chain_down_reports_the_configured_sources_status(app_ctx, monkeypatch):
    """整条链都挂时，用户想知道的是**他选的那张**怎么了，不是链尾那张。"""
    client, route_mod = app_ctx
    up = _PerHostUpstream({
        'arcgisonline.com': _esri_403(),
        'googleapis.com': OSError('no route'),
        'openstreetmap.org': OSError('no route'),
    })
    _stub(monkeypatch, route_mod, up)

    assert client.get('/basemap/2/1/1').status_code == 403
    assert len(up.requests) == 3, '放弃之前每个候选都要试过一次'


def test_upstream_404_is_a_missing_tile_not_an_outage(app_ctx, monkeypatch):
    """404 是每个 XYZ 服务说「这里没有图」的方式，不是故障信号。

    Esri 的 World Imagery 在覆盖空洞和超出层级上限时就这么答。把它当上游挂掉
    的话，一张缺图会让整个配置源冷却 60 秒、后续每张瓦片都换供应商、界面弹一次
    根本没发生过的「底图已切换」——回退特性引入之前，缺图就只是缺图。
    """
    client, route_mod = app_ctx
    up = _PerHostUpstream({'arcgisonline.com': _http_error(404, 'Not Found')})
    _stub(monkeypatch, route_mod, up)

    r = client.get('/basemap/2/1/1')

    assert r.status_code == 404, '缺图要原样透传 404'
    assert len(up.requests) == 1, '缺一张图不该把整条回退链走一遍'
    assert client.application.extensions.get(route_mod._CACHE_KEY_COOLDOWN) == {}, (
        '404 不该把源标记成挂掉')
    assert client.get('/api/basemap').get_json()['basemap']['fallback'] is False

    # 冷却没写、回退状态没动 —— 下一张瓦片必须还是先打配置的源
    up.failures.clear()
    up.requests.clear()
    assert client.get('/basemap/2/1/0').status_code == 200
    assert 'arcgisonline.com' in up.requests[0]


def test_a_one_off_4xx_does_not_mark_the_whole_source_down(app_ctx, monkeypatch):
    """只有 403（封 IP）/429（限流）/5xx（上游自己崩了）是源级故障信号。

    410 之类是针对**这一次请求**的答复：拿它冷却整个源 60 秒，等于让一块坏瓦片
    决定其余几十块去哪儿取，而且会连带弹出一次「底图已切换」。回退照走，冷却
    不写。
    """
    client, route_mod = app_ctx
    up = _PerHostUpstream({'arcgisonline.com': _http_error(410, 'Gone')})
    _stub(monkeypatch, route_mod, up)

    assert client.get('/basemap/2/1/1').status_code == 200, '这一张仍要靠回退出图'
    assert client.application.extensions[route_mod._CACHE_KEY_COOLDOWN] == {}, (
        '单张瓦片的 410 不是「这个源挂了」')

    up.failures.clear()
    up.requests.clear()
    assert client.get('/basemap/2/1/0').status_code == 200
    assert 'arcgisonline.com' in up.requests[0], '没写冷却就该继续先试配置的源'


def _version_of(configured, **kw):
    """浏览器实际会用的 v。走的是下发描述符的同一条路径（client_descriptor
    把它缀在同源 URL 后面），所以这里算出来的就是真浏览器会请求的那一个。"""
    return source_version(resolve_basemap(configured, **kw)['upstream'])


def test_fallback_tiles_are_not_baked_into_the_browser_cache_for_a_day(app_ctx,
                                                                       monkeypatch):
    """回退瓦片长缓存会把地图永久变成两家拼图。

    上游抖动 30 秒，浏览器就按 max-age=86400 存下另一家的影像；配置的源恢复后
    缓存命中不再回源，用户对着一张 Esri/Google 混拼的地图，界面上没有任何补救
    手段，而且它永远不会自己好。回退特性引入之前失败是 502，什么都不缓存。

    两次请求都带配置源（Esri）的 v —— 那就是浏览器手里那份描述符给它的 URL。
    不带 v 的请求一律走短缓存，用它来测回退的话这条用例永远绿，测不到东西。
    """
    client, route_mod = app_ctx
    monkeypatch.setattr(route_mod, '_COOLDOWN_S', 0.0)
    up = _PerHostUpstream({'arcgisonline.com': _esri_403()})
    _stub(monkeypatch, route_mod, up)
    v = _version_of('esri')

    fallback = client.get(f'/basemap/2/1/1?v={v}')
    assert fallback.status_code == 200
    max_age = re.search(r'max-age=(\d+)', fallback.headers['Cache-Control'])
    assert max_age is not None, f"回退瓦片没有 Cache-Control：{fallback.headers}"
    assert 0 < int(max_age.group(1)) <= 300, (
        f'回退瓦片缓存了 {max_age.group(1)}s —— 上游恢复后地图会一直是拼图')

    # 配置的源自己出的图仍然要长缓存：平移/缩放的重复请求得挡在本机
    up.failures.clear()
    direct = client.get(f'/basemap/2/1/2?v={v}')
    assert direct.status_code == 200
    assert 'arcgisonline.com' in up.requests[-1], '前置条件：这张是配置源出的'
    assert direct.headers['Cache-Control'] == 'public, max-age=86400'


def test_a_tile_from_a_stale_source_resolution_is_not_baked_in_for_a_day(app_ctx,
                                                                        monkeypatch):
    """换完源的头几秒里出的还是旧那家的图，它不许占住新 URL 空间一整天。

    用户在配置页把底图从 Esri 换成 Google 并刷新：页面立刻拿到新描述符
    （/api/basemap 每次实时解析配置），而路由这边最多还有 _SOURCE_TTL_S 秒
    在按缓存里那份旧解析出图。长缓存的判据一旦比的是【缓存里那份】配置源，
    这几秒取到的 Esri 瓦片就会以 Google 的 URL 被烤进浏览器缓存一整天 ——
    缓存命中不回源，用户只能硬刷新/清缓存，表现成「这个设置项坏了」。
    """
    client, route_mod = app_ctx
    up = _FakeUpstream()
    _stub(monkeypatch, route_mod, up)

    assert client.get('/basemap/2/1/1').status_code == 200, '先把 TTL 缓存焐热'
    assert 'arcgisonline.com' in up.requests[0].full_url, '前置条件：起手是 Esri'

    route_mod.config_manager.set('basemap_source', 'google_satellite')
    url = client.get('/api/basemap').get_json()['basemap']['url']
    assert '?v=' in url, '描述符里没有源标识 —— 换源根本不换 URL 空间'
    v = url.split('?v=')[1]

    r = client.get(f'/basemap/2/1/2?v={v}')

    assert r.status_code == 200
    assert 'arcgisonline.com' in up.requests[-1].full_url, (
        '前置条件：TTL 没到期，这张仍该是旧源出的')
    assert r.headers['Cache-Control'] == f'public, max-age={route_mod._SHORT_MAX_AGE_S}', (
        f"旧源的图拿到了 {r.headers['Cache-Control']} —— 新 URL 空间被旧影像占住一天")


def test_switching_the_basemap_source_switches_the_url_the_browser_requests(app_ctx,
                                                                            monkeypatch):
    """换源必须换 URL 空间，否则浏览器压根不会回源。

    这是上一条的另一半：就算服务端已经改按新源出图，只要下发给浏览器的 URL
    一个字没变，已经浏览过的区域仍然命中 24 小时的旧缓存 —— 用户看到的画面
    完全不变，而界面上没有任何补救手段。
    """
    client, route_mod = app_ctx
    _stub(monkeypatch, route_mod, _FakeUpstream())

    before = client.get('/api/basemap').get_json()['basemap']['url']
    route_mod.config_manager.set('basemap_source', 'google_satellite')
    after = client.get('/api/basemap').get_json()['basemap']['url']

    assert after != before, f'换源之后浏览器拿到的还是同一条 URL：{after}'
    for url in (before, after):
        # 同源 + Cesium 自己代入 {z}/{x}/{y}：版本串不许破坏这两条契约。
        assert url.startswith('/basemap/{z}/{x}/{y}?v='), url
        for host in ('arcgisonline', 'googleapis', 'http'):
            assert host not in url, f'URL 里泄露了上游地址：{url}'


def test_a_tile_requested_without_a_version_is_not_baked_in_for_a_day(app_ctx,
                                                                      monkeypatch):
    """不带 v 的请求（旧页面、手输地址、history.js 的兜底常量）只给短缓存。

    它的 URL 空间不随源变化，一旦发出 24 小时缓存就再也撤不回：换源之后那些
    地址依然命中旧影像，而服务端没有任何办法让浏览器回源。
    """
    client, route_mod = app_ctx
    _stub(monkeypatch, route_mod, _FakeUpstream())

    r = client.get('/basemap/2/1/1')

    assert r.status_code == 200
    assert r.headers['Cache-Control'] == f'public, max-age={route_mod._SHORT_MAX_AGE_S}'


def test_configured_status_wins_even_after_the_source_was_cooled_down(app_ctx,
                                                                     monkeypatch):
    """「报配置源的状态码」这条契约恰好在配置源进了冷却时才会破。

    冷却中的源被排到链尾，于是「第一个报错的候选」不再是配置的那个：Esri 403
    之后的第二张瓦片里，链是 [google, osm, esri]，Google 超时先写下 504，用户
    拿到的诊断信息与他配的那张图毫无关系。配置的源在链里只出现一次，让它
    无条件覆盖即可。
    """
    client, route_mod = app_ctx
    up = _PerHostUpstream({'arcgisonline.com': _esri_403()})
    _stub(monkeypatch, route_mod, up)

    assert client.get('/basemap/2/1/1').status_code == 200, '前置条件：先回退一次'

    up.failures['googleapis.com'] = OSError('timed out')
    up.failures['openstreetmap.org'] = OSError('no route')
    up.requests.clear()

    r = client.get('/basemap/2/1/2')

    assert r.status_code == 403, (
        f'整条链都挂时要报配置源(Esri)的 403，实际拿到 {r.status_code}')
    assert any('arcgisonline.com' in u for u in up.requests), (
        '冷却中的源排到链尾但不能被剔除，否则永远拿不到它的状态码')


def test_nonstandard_upstream_status_does_not_become_a_500(app_ctx, monkeypatch):
    """werkzeug 的 Aborter 对 520 这类没有异常类的状态码抛 LookupError。

    Flask 把它转成 500，于是一个 Cloudflare 前置的自建镜像返回 520 时，用户看到
    的是「服务端崩了」——正是本模块存在的理由（真实状态码被埋掉）的复现。
    499/520/521/522/525/530 都在 default_exceptions 之外。
    """
    client, route_mod = app_ctx
    up = _PerHostUpstream({
        'arcgisonline.com': _http_error(520, 'Web Server Returned an Unknown Error'),
        'googleapis.com': _http_error(520, 'Web Server Returned an Unknown Error'),
        'openstreetmap.org': _http_error(520, 'Web Server Returned an Unknown Error'),
    })
    _stub(monkeypatch, route_mod, up)

    r = client.get('/basemap/2/1/1')

    assert r.status_code == 502, (
        f'抬不动的状态码要降级成 502，实际 {r.status_code}')


def test_concurrent_tiles_fall_back_without_corrupting_the_cooldown(app_ctx,
                                                                   monkeypatch):
    """首屏是几十张瓦片同时打进来的 —— 回退与冷却全在这个并发下发生。

    单线程用例看不见的问题在这里才会露头：候选顺序、冷却表写入、回退状态记录
    都被多个请求线程同时碰。要求是全部出图、一个异常都不许有，且冷却表里只留
    真正挂掉的那个源。
    """
    client, route_mod = app_ctx
    up = _PerHostUpstream({'arcgisonline.com': _esri_403()})
    _stub(monkeypatch, route_mod, up)

    flask_app = client.application
    results, errors = [], []

    def fetch(n):
        try:
            results.append(flask_app.test_client().get(f'/basemap/4/{n}/3').status_code)
        except Exception as e:                      # noqa: BLE001 —— 就是要抓住任何异常
            errors.append(repr(e))

    threads = [threading.Thread(target=fetch, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f'并发回退里抛了异常：{errors}'
    assert results == [200] * 12, f'并发下有瓦片没出图：{sorted(results)}'
    assert set(flask_app.extensions[route_mod._CACHE_KEY_COOLDOWN]) == {'esri'}, (
        '只有真正挂掉的源该进冷却表')


def test_fallback_never_leaks_the_upstream_address_to_the_browser(app_ctx, monkeypatch):
    """回退是「上游地址不下发」这条硬约束上唯一的新开口。

    前端一旦拿到上游地址就会有人图省事直连回去，CORS 与「底图不吃代理」两个坑
    立刻复活。回退状态下描述符走的是另一条分支（active_basemap 叠加），所以这条
    要在回退真的生效之后再查一遍首页与 /api/basemap。
    """
    client, route_mod = app_ctx
    _stub(monkeypatch, route_mod, _PerHostUpstream({'arcgisonline.com': _esri_403()}))

    assert client.get('/basemap/2/1/1').status_code == 200
    bm = client.get('/api/basemap').get_json()['basemap']
    assert bm['fallback'] is True, '前置条件：这条用例只在回退生效时有意义'
    assert 'upstream' not in bm

    for name, body in (('/', client.get('/').get_data(as_text=True)),
                       ('/api/basemap', client.get('/api/basemap').get_data(as_text=True))):
        for needle in ('arcgisonline', 'googleapis', 'upstream'):
            assert needle not in body, f'回退状态下 {needle} 漏进了 {name} 的响应体'

    # 描述符这一层剥不剥 upstream 不是全部：active_basemap 本身是被
    # src/routes/api.py 与 src/routes/main.py 两处 import 的**公开函数**，
    # 今天两个调用点都恰好套了 client_descriptor，但只要有一处直接 jsonify 它，
    # 上游地址就下发了。所以在函数出口上单独钉一次。
    with client.application.app_context():
        active = route_mod.active_basemap(resolve_basemap('esri'))
    assert active['fallback'] is True and active['source'] == 'google_satellite', active
    assert 'upstream' not in active, 'active_basemap 的返回值里不许有上游地址'


@pytest.mark.parametrize('configured', [
    *sorted(BASEMAP_PRESETS),
    DOWNLOAD_SOURCE,
    'https://mirror.example.com/tiles/{z}/{x}/{y}.png',
])
def test_fallback_chain_is_wgs84_for_every_configured_source(configured):
    """回退是自动且无人确认的：链上任何一张非 WGS-84 的图都会让用户在偏移
    100-700 米的底图上框选而毫不知情。

    按**每一个**可配置的源各钉一遍，而不是只钉默认的 esri —— 新增预设时漏标
    wgs84、或者给自定义模板/download_source 走了另一条建链分支，都会在这里翻红。
    """
    resolved = resolve_basemap(configured, tile_servers='', default_style='m')
    chain = fallback_candidates(resolved)
    sources = [c['source'] for c in chain]

    assert sources[0] == resolved['source'], (
        f'{configured}：配置的源永远排第一，实际 {sources}')
    for candidate in chain[1:]:
        assert BASEMAP_PRESETS[candidate['source']]['wgs84'], (
            f"{configured} 的回退链里有非 WGS-84 的 {candidate['source']}")
    assert len(sources) == len(set(sources)), f'{configured} 的回退链里有重复源：{sources}'