# -*- coding: utf-8 -*-
"""底图瓦片的服务端磁盘缓存（cache/basemap/）。

为什么要有它：底图每张瓦片都经代理回源，上游 RTT 秒级且与连接复用无关
（实测 keep-alive 无改善）。首屏几十张瓦片的风暴会占满浏览器对单源的
6 条 HTTP/1.1 连接 15-30 秒，期间页面上一切 API 操作（配置保存等）都
要在浏览器连接池里排队 —— 「配置页第一次点保存要等很久」的根因。
浏览器缓存（max-age=86400）只挡一天、且回退期出的图只配 60s，天天
重演。瓦片内容按 URL 基本不变，服务端落盘一次，之后任何浏览器/任何
一天都是毫秒级命中，风暴窗口缩到一两秒，连接池排队随之消失。

口径与下载瓦片缓存一致：不做自动清理（配置页「缓存管理」手动清，
cache 顶层子目录自动成为一个分类）；受 cache_enabled 同一个开关管。
"""
import os
import sys
import urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from test_basemap_proxy_route import _FakeUpstream, _PNG, app_ctx  # noqa: E402,F401


def _tile_count(up):
    """只数打到上游瓦片 URL 的请求（排除任何非瓦片噪音）。"""
    return len(up.requests)


def test_second_request_for_same_tile_hits_disk_cache(app_ctx, monkeypatch):
    client, route_mod = app_ctx
    up = _FakeUpstream()
    monkeypatch.setattr(route_mod.urllib.request, 'build_opener',
                        lambda *a, **k: up)
    monkeypatch.setattr(route_mod, 'resolve_from_config', lambda cm, wait_s=None: '')

    r1 = client.get('/basemap/3/6/2')
    r2 = client.get('/basemap/3/6/2')

    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.data == _PNG
    assert _tile_count(up) == 1, '第二次请求又回源了 —— 磁盘缓存没生效'


def test_cache_hit_skips_proxy_resolution(app_ctx, monkeypatch):
    """缓存命中必须整条跳过网络路径 —— 包括代理解析的 _PROXY_WAIT_S 阻塞。"""
    client, route_mod = app_ctx
    up = _FakeUpstream()
    monkeypatch.setattr(route_mod.urllib.request, 'build_opener',
                        lambda *a, **k: up)
    monkeypatch.setattr(route_mod, 'resolve_from_config', lambda cm, wait_s=None: '')
    assert client.get('/basemap/3/6/2').status_code == 200

    def _boom(cm, wait_s=None):
        raise AssertionError('缓存命中还调了 resolve_from_config')

    monkeypatch.setattr(route_mod, 'resolve_from_config', _boom)
    r = client.get('/basemap/3/6/2')
    assert r.status_code == 200 and r.data == _PNG


def test_cache_disabled_by_config_fetches_every_time(app_ctx, monkeypatch):
    client, route_mod = app_ctx
    # 必须在第一次请求之前关：开关本身带短 TTL 缓存（热路径不每瓦片开
    # sqlite），先请求会把 true 缓存住。
    route_mod.config_manager.set('cache_enabled', 'false')
    up = _FakeUpstream()
    monkeypatch.setattr(route_mod.urllib.request, 'build_opener',
                        lambda *a, **k: up)
    monkeypatch.setattr(route_mod, 'resolve_from_config', lambda cm, wait_s=None: '')

    client.get('/basemap/3/6/2')
    client.get('/basemap/3/6/2')

    assert _tile_count(up) == 2, 'cache_enabled=false 时还在用磁盘缓存'


def test_upstream_404_is_not_cached(app_ctx, monkeypatch):
    """404 是上游说「这里没有图」的常态答复，缓存它会把覆盖空洞钉死。"""
    client, route_mod = app_ctx
    err = urllib.error.HTTPError('http://x', 404, 'Not Found', {}, None)
    up = _FakeUpstream(raise_error=err)
    monkeypatch.setattr(route_mod.urllib.request, 'build_opener',
                        lambda *a, **k: up)
    monkeypatch.setattr(route_mod, 'resolve_from_config', lambda cm, wait_s=None: '')

    assert client.get('/basemap/1/0/1').status_code == 404
    assert client.get('/basemap/1/0/1').status_code == 404
    assert _tile_count(up) == 2, '404 被写进了磁盘缓存'


def test_non_image_content_type_is_not_cached(app_ctx, monkeypatch):
    """只缓存认识的图片类型 —— 错误页/验证码页（text/html）不能落盘。"""
    client, route_mod = app_ctx
    up = _FakeUpstream(body=b'<html>challenge</html>', content_type='text/html')
    monkeypatch.setattr(route_mod.urllib.request, 'build_opener',
                        lambda *a, **k: up)
    monkeypatch.setattr(route_mod, 'resolve_from_config', lambda cm, wait_s=None: '')

    assert client.get('/basemap/3/6/2').status_code == 200
    assert client.get('/basemap/3/6/2').status_code == 200
    assert _tile_count(up) == 2, 'text/html 响应被写进了磁盘缓存'


def test_cached_tile_keeps_cache_control_semantics(app_ctx, monkeypatch):
    """命中缓存也要走同一套 Cache-Control 判定（v 匹配才给一天）。"""
    client, route_mod = app_ctx
    up = _FakeUpstream()
    monkeypatch.setattr(route_mod.urllib.request, 'build_opener',
                        lambda *a, **k: up)
    monkeypatch.setattr(route_mod, 'resolve_from_config', lambda cm, wait_s=None: '')
    from src.services.basemap_source import BASEMAP_PRESETS, source_version
    v = source_version(BASEMAP_PRESETS['esri']['url'])

    client.get(f'/basemap/3/6/2?v={v}')
    r = client.get(f'/basemap/3/6/2?v={v}')

    assert r.status_code == 200
    assert r.headers.get('Cache-Control') == 'public, max-age=86400'
    assert _tile_count(up) == 1


def test_main_and_tile_origins_share_disk_cache(app_ctx, monkeypatch):
    """瓦片专用端口与主端口是同一个 app、同一个 cache/basemap 目录。

    分流的前提是「换端口只换连接池，不换缓存」：瓦片端口若跑的是第二个 app
    实例（或另一个缓存根），主端口预热过的瓦片在 5001 上会全部再回源一次 ——
    首屏风暴不但没消失，还翻了一倍。这里从主端口落一次盘，再用真 socket 从
    临时瓦片端口取同一张图，断言上游只被打了一次。
    """
    import urllib.request
    from src.core.tile_server import start_tile_server

    client, route_mod = app_ctx
    # 必须在打桩**之前**留一个真 opener：route_mod.urllib 就是 urllib 包本身，
    # 下面那句 setattr 打的是 stdlib 的 urllib.request.build_opener，裸用
    # urllib.request.urlopen 取瓦片端口会命中替身 opener（连 TCP 都不会建），
    # 断言就变成在验证替身自己。
    # 而这个 opener 必须显式给一个**空的** ProxyHandler：默认的 build_opener()
    # 会从环境里读 http_proxy/all_proxy，开发机上（设了代理、且 no_proxy 没写
    # 回环）这条 127.0.0.1 的请求会被丢给代理，代理连不上本机临时端口 ——
    # 用例翻红的原因与缓存共享毫无关系，而且只在部分机器上复现。
    real_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    up = _FakeUpstream()
    monkeypatch.setattr(route_mod.urllib.request, 'build_opener',
                        lambda *a, **k: up)
    monkeypatch.setattr(route_mod, 'resolve_from_config', lambda cm, wait_s=None: '')

    from src.services.basemap_source import BASEMAP_PRESETS, source_version
    version = source_version(BASEMAP_PRESETS['esri']['url'])
    path = f'/basemap/3/6/2?v={version}'
    first = client.get(path)
    assert first.status_code == 200

    server = start_tile_server(client.application, host='127.0.0.1', port=0)
    assert server is not None
    try:
        with real_opener.open(
                f'http://127.0.0.1:{server.server_port}{path}',
                timeout=5) as response:
            assert response.status == 200
            assert response.headers['Access-Control-Allow-Origin'] == '*'
            assert response.headers['Cache-Control'] == 'public, max-age=86400'
            assert response.read() == _PNG
    finally:
        server.shutdown()
        server.server_close()

    assert _tile_count(up) == 1, '瓦片端口没吃到主端口落的盘 —— 缓存没共享'
