"""瓦片专用端口（:5001）—— 把瓦片风暴从 API 的连接池里隔出去。

根因：浏览器对**单源**只开 6 条 HTTP/1.1 连接。底图/地形/历史瓦片全部走
同源转发，首屏几十张瓦片、每张上游 RTT 秒级，6 条连接被占满期间页面上一切
API 操作（配置保存、历史查询、SocketIO 握手）都在浏览器连接池里排队 ——
「加载地图时别的请求都被阻塞」。磁盘缓存（routes/basemap_static.py）只挡
重复访问，首访与新区域风暴照旧。

修法是把瓦片流量挪到**另一个源**（同主机、不同端口）：浏览器按 (host, port)
分连接池，瓦片风暴再也挤不到 API。这些断言守的是：

  1. 瓦片端口只放行精确健康路径和瓦片类路径（/basemap /tiles /terrain
     /contour），其余一律 404 —— 不把第二个端口变成 API 的又一个入口；
  2. 健康响应不可缓存，所有放行响应带 Access-Control-Allow-Origin: * ——
     换端口对浏览器就是跨源，Cesium 取瓦片/layer.json 没有 CORS 头会失败；
  3. 端口被占时降级为 None（前端退回同源路径），而不是把主服务一起搞死；
  4. 描述符能把 tile_port 带给前端（主端口拿不到时如实为 None）。
"""
import json
import os
from pathlib import Path
import re
import socket
import sys
import urllib.error
import urllib.request

import pytest

from conftest import fresh_import, isolated_app  # noqa: F401  (fixture)

from src.services.basemap_source import client_descriptor, resolve_basemap


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'templates' / 'index.html'
UI_JS = ROOT / 'static' / 'js' / 'ui.js'

# 「端口被占」这个前提在 Windows 上**构造不出来**，两条冲突用例只能跳过。
#
# 依据：Windows 的 SO_REUSEADDR 语义与 BSD/Linux 不同 —— 它允许**同一用户**的另
# 一个进程绑到一个已经在 listen 的端口上，两个 socket 都 bind 成功，连接由谁收
# 由内核任意决定（要拒绝后来者得由先到者显式设 SO_EXCLUSIVEADDRUSE）。而生产
# 路径两侧都带 SO_REUSEADDR：werkzeug 的 BaseWSGIServer.allow_reuse_address 为
# True，本仓的 _bind_conflict 探测 socket 与它同口径。于是在 Windows 上：
# 用一个普通的 listening socket 占住端口，探测照样绑得上、make_server 照样起得
# 来，`start_tile_server` 返回的不是 None 而是一个 listener（连接落到哪个 socket
# 由内核决定，MSDN 明说是 indeterminate，实际表现就是这个 listener 收不到连接），
# 断言 `srv is None` 必红。
#
# 换 SO_EXCLUSIVEADDRUSE 去造冲突可以让断言变绿，但那测的是另一个场景：真实的
# 占用者是另一个 TerraForge 实例（同样 allow_reuse_address=True，不设独占），
# 绿灯会假装覆盖了一条 Windows 上根本不会走到的分支。
#
# 跳过不放宽实现，也不留下无保护的失败面：Windows 上真撞端口时降级由**前端**
# 兜底 —— 页面对 /tile-health 探测 1 秒拿不到正常响应就整页退回同源路径
# （static/js/ui.js 的 initTileOrigin，见 tests/test_tile_origin_runtime.py 的
# 超时用例，那几条是跨平台的纯 JS 断言）。功能仍然安全降级，代价只是速度。
_WINDOWS_REUSEADDR_SKIP = pytest.mark.skipif(
    sys.platform == 'win32',
    reason='Windows 的 SO_REUSEADDR 允许同用户进程绑到已占用端口，'
           '造不出端口冲突；该平台的降级由前端 1 秒探测超时兜底')


def _sentinel_app(calls):
    """一个什么都不服务的 WSGI app：只记录自己被没被调用。"""
    def app(environ, start_response):
        calls.append(environ.get('PATH_INFO'))
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [b'ok']
    return app


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class TestTileGate:
    """WSGI 包装层：只放行健康检查和瓦片前缀，并给响应补 CORS 头。"""

    def _call(self, path):
        from src.core.tile_server import wrap_tile_app
        calls = []
        app = wrap_tile_app(_sentinel_app(calls))
        captured = {}

        def start_response(status, headers, exc_info=None):
            captured['status'] = status
            captured['headers'] = dict(headers)

        body = b''.join(app({'PATH_INFO': path}, start_response))
        return captured, body, calls

    def test_health_is_served_without_calling_wrapped_app(self):
        captured, body, calls = self._call('/tile-health')
        assert calls == []
        assert captured['status'].startswith('204')
        assert body == b''
        assert captured['headers']['Access-Control-Allow-Origin'] == '*'
        assert captured['headers']['Cache-Control'] == 'no-store'

    def test_health_204_sends_no_content_length(self):
        """RFC 7230 §3.3.2：204 响应**不得**带 Content-Length。

        代理和 HTTP/2 网关对「204 + Content-Length」的处理各不相同（有的直接
        当成帧错误断连），而这个头在这里一点用都没有 —— 204 的语义本身就是
        没有正文。
        """
        captured, _, _ = self._call('/tile-health')
        assert 'Content-Length' not in captured['headers']

    def test_tile_prefixes_pass_through(self):
        for path in ('/basemap/3/1/2', '/tiles/7/3/1/2.png',
                     '/terrain/base/layer.json', '/contour/9/3/1/2.png'):
            captured, body, calls = self._call(path)
            assert calls == [path], path
            assert captured['status'].startswith('200'), path
            assert captured['headers'].get('Access-Control-Allow-Origin') == '*'

    def test_non_tile_path_is_404_and_app_untouched(self):
        for path in ('/api/config', '/', '/basemapx/1/2/3', '/static/js/map.js'):
            captured, body, calls = self._call(path)
            assert calls == [], path
            assert captured['status'].startswith('404'), path
            # 404 也带 CORS 头：跨源瓦片 404 时不带头的响应在浏览器里
            # 只会报成一句 CORS 错误，真实状态码又被埋掉（正是 basemap_static
            # 模块 docstring 记过的那个坑）。
            assert captured['headers'].get('Access-Control-Allow-Origin') == '*'

    @pytest.mark.parametrize('path', ['/tile-health/', '/tile-healthx',
                                      '/socket.io/', '/api/basemap'])
    def test_non_tile_and_health_prefix_collisions_are_rejected(self, path):
        captured, _, calls = self._call(path)
        assert calls == []
        assert captured['status'].startswith('404')


class TestTilePrefixSingleSource:
    """瓦片端口的两个跨语言常量（前缀名单、健康路径）只能有**一份**口径。

    前缀名单同时决定三件事：瓦片端口放行谁（tile_server）、控制台丢掉谁的成功
    访问日志（logging_setup）、浏览器把哪些路径改写到瓦片 origin（ui.js）。
    手抄一份就等于埋一个「加了新瓦片路由只改了一处」的坑：漏改 tile_server 是
    跨源硬 404，漏改 ui.js 是白开了个端口，漏改 logging_setup 是控制台被瓦片
    日志刷屏。健康路径漂移则是每页白等 1 秒后整页退回同源，同样无声。
    """

    def test_python_sources_share_one_tuple_object(self):
        from src.core import logging_setup, tile_server
        from src.core.tile_paths import TILE_PATH_PREFIXES

        assert tile_server.TILE_PATH_PREFIXES is TILE_PATH_PREFIXES
        assert logging_setup._TILE_PATH_PREFIXES is TILE_PATH_PREFIXES
        assert TILE_PATH_PREFIXES == ('/basemap/', '/tiles/', '/terrain/',
                                      '/contour/', '/mbtiles/')

    def test_prefix_literal_is_typed_only_in_tile_paths(self):
        """`is` 断言挡得住漂移，挡不住有人再抄一份**新的**常量出来。"""
        hits = []
        for path in sorted(ROOT.joinpath('src').rglob('*.py')):
            text = path.read_text(encoding='utf-8')
            for lineno, line in enumerate(text.splitlines(), 1):
                if "'/basemap/'" in line and "'/tiles/'" in line:
                    # as_posix()：Windows 上 relative_to 产出反斜杠，与下面用正
                    # 斜杠字面量写的断言对不上，整条用例会在 CI 上假红。
                    hits.append(f'{path.relative_to(ROOT).as_posix()}:{lineno}')
        assert len(hits) == 1 and hits[0].startswith('src/core/tile_paths.py:'), (
            '瓦片前缀名单只应在 src/core/tile_paths.py 里写一次，实际出现在：'
            + ', '.join(hits))

    def test_frontend_list_matches_backend(self):
        """ui.js 的名单必须与后端逐字相同 —— 前端多一条得跨源 404，少一条则
        那类瓦片仍挤在主端口的 6 条连接里，两种漂移都无声。"""
        from src.core.tile_paths import TILE_PATH_PREFIXES

        js = UI_JS.read_text(encoding='utf-8')
        match = re.search(r'const TILE_PATH_PREFIXES = \[([^\]]*)\];', js)
        assert match, 'ui.js 里找不到 TILE_PATH_PREFIXES 名单'
        assert tuple(re.findall(r"'([^']+)'", match.group(1))) == \
            TILE_PATH_PREFIXES

    def test_frontend_health_path_matches_backend(self):
        """健康检查路径同样是跨语言常量，同样只能有一份口径。

        它是**精确匹配**（`/tile-health/` 和 `/tile-healthx` 都 404，见
        test_non_tile_and_health_prefix_collisions_are_rejected），所以一旦两边
        漂开，探测必然 404 → 每个页面都白等 1 秒然后整页退回同源：瓦片端口起着、
        没人用，日志里也不会有任何异常。名单那条断言防的是同一类无声漂移。
        """
        from src.core.tile_server import TILE_HEALTH_PATH

        js = UI_JS.read_text(encoding='utf-8')
        match = re.search(r"health\.pathname = '([^']*)';", js)
        assert match, 'ui.js 里找不到探测用的 health.pathname 赋值'
        assert match.group(1) == TILE_HEALTH_PATH, (
            f'前端探的是 {match.group(1)!r}，后端只认 {TILE_HEALTH_PATH!r} '
            '（精确匹配，差一个字符就是 404 + 每页白等 1 秒）')


def test_tile_gate_preserves_wrapped_cache_control():
    def cached_app(environ, start_response):
        start_response('200 OK', [('Cache-Control', 'public, max-age=86400')])
        return [b'ok']
    from src.core.tile_server import wrap_tile_app
    captured = {}
    body = b''.join(wrap_tile_app(cached_app)(
        {'PATH_INFO': '/basemap/0/0/0'},
        lambda status, headers, exc_info=None: captured.update(
            status=status, headers=dict(headers))))
    assert body == b'ok'
    assert captured['headers']['Cache-Control'] == 'public, max-age=86400'
    assert captured['headers']['Access-Control-Allow-Origin'] == '*'


class TestStartTileServer:
    """起真服务器：真端口、真请求、健康检查和端口冲突降级。"""

    def test_serves_tile_path_with_cors(self):
        from src.core.tile_server import start_tile_server
        calls = []
        srv = start_tile_server(_sentinel_app(calls), host='127.0.0.1', port=0)
        assert srv is not None
        try:
            status, headers, body = _get(
                f'http://127.0.0.1:{srv.server_port}/basemap/0/0/0')
            assert status == 200
            assert headers.get('Access-Control-Allow-Origin') == '*'
            assert calls == ['/basemap/0/0/0']
        finally:
            srv.shutdown()
            srv.server_close()

    def test_health_endpoint_is_reachable_with_cors_and_no_store(self):
        from src.core.tile_server import start_tile_server
        srv = start_tile_server(_sentinel_app([]), host='127.0.0.1', port=0)
        assert srv is not None
        try:
            status, headers, body = _get(
                f'http://127.0.0.1:{srv.server_port}/tile-health')
            assert status == 204
            assert body == b''
            assert headers['Access-Control-Allow-Origin'] == '*'
            assert headers['Cache-Control'] == 'no-store'
        finally:
            srv.shutdown()
            srv.server_close()

    def test_health_response_carries_no_content_length_over_the_wire(self):
        from src.core.tile_server import start_tile_server
        srv = start_tile_server(_sentinel_app([]), host='127.0.0.1', port=0)
        assert srv is not None
        try:
            status, headers, _ = _get(
                f'http://127.0.0.1:{srv.server_port}/tile-health')
            assert status == 204
            assert 'Content-Length' not in headers
        finally:
            srv.shutdown()
            srv.server_close()

    @_WINDOWS_REUSEADDR_SKIP
    def test_port_conflict_degrades_to_none(self):
        from src.core.tile_server import start_tile_server
        blocker = socket.socket()
        blocker.bind(('127.0.0.1', 0))
        blocker.listen(1)
        try:
            srv = start_tile_server(_sentinel_app([]), host='127.0.0.1',
                                    port=blocker.getsockname()[1])
            assert srv is None
        finally:
            blocker.close()

    @_WINDOWS_REUSEADDR_SKIP
    def test_port_conflict_prints_nothing_to_stderr(self, capfd, caplog):
        """降级只留一句 warning 日志，stderr 上不许出现 werkzeug 那句建议。

        werkzeug 绑定失败会 print 到 stderr：「Port 5001 is in use by another
        program. Either identify and stop that program, or start the server with
        a different port.」桌面用户看到的就是这句 —— 而瓦片端口**不可配置**
        （docs/guides/DISTRIBUTION.md：固定 5000+1，配置页和配置文件里都没有这
        个开关），照它去找「换一个端口」的地方是找不到的；更要命的是它把一次
        「只影响性能的自动降级」说成了必须动手处理的故障。

        这里钉的是**用户看得见的那条通道**（fd 级 stderr）上不许出现 werkzeug 的
        那两句，不是「stderr 上什么都不许有」：生产上降级的 warning 日志本来就
        写 stderr（logging 的控制台 handler），那一句是排查时唯一的线索，必须留。
        测试里它落进 caplog、不经过 fd，所以 fd 上剩下的任何东西都只可能来自
        werkzeug —— 空断言在这个上下文里等价于「werkzeug 一个字都没漏出来」，
        比逐句黑名单更严（换个 werkzeug 版本改了措辞也照样抓得住）。
        """
        import logging as _logging

        from src.core.tile_server import start_tile_server
        blocker = socket.socket()
        blocker.bind(('127.0.0.1', 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            capfd.readouterr()
            with caplog.at_level(_logging.WARNING, logger='src.core.tile_server'):
                srv = start_tile_server(_sentinel_app([]), host='127.0.0.1',
                                        port=port)
            captured = capfd.readouterr()
            assert srv is None
        finally:
            blocker.close()

        assert 'is in use by another program' not in captured.err
        assert 'different port' not in captured.err
        assert captured.err.strip() == '', (
            '端口降级不得让 werkzeug 的提示漏到 stderr（本仓自己的 warning 走 '
            f'logging，测试里由 caplog 接走，不经过 fd）；实际写了：{captured.err!r}')
        assert any(str(port) in r.getMessage() and '同源' in r.getMessage()
                   for r in caplog.records), (
            '降级必须留下一条能定位的 warning 日志')


class TestTilePortDescriptor:
    """tile_port 要能被下发给前端：默认值是 SERVER_PORT + 1，描述符如实携带。"""

    def test_default_port_is_server_port_plus_one(self):
        from src.core.runtime_mode import SERVER_PORT
        from src.core.tile_server import TILE_PORT
        assert TILE_PORT == SERVER_PORT + 1

    def test_client_descriptor_carries_tile_port(self):
        resolved = resolve_basemap(None)
        with_port = client_descriptor(resolved, tile_port=5001)
        assert with_port['tile_port'] == 5001
        # 瓦片服务没起来时如实 None —— 前端拿 None 退回同源路径，
        # 不能收到一个指向死端口的 URL。
        without = client_descriptor(resolved)
        assert without['tile_port'] is None

    def test_current_tile_port_reads_app_extensions(self, isolated_app):
        from src.core.tile_server import current_tile_port
        app = isolated_app.app
        with app.test_request_context('/'):
            assert current_tile_port() is None
            app.extensions['tile_server_port'] = 5001
            assert current_tile_port() == 5001

    def test_wsgi_import_descriptor_has_no_tile_port(self, isolated_app):
        response = isolated_app.app.test_client().get('/api/basemap')
        payload = response.get_json()
        assert response.status_code == 200
        assert payload['basemap']['tile_port'] is None

    def test_index_fallback_descriptor_uses_current_tile_port(
            self, isolated_app, monkeypatch):
        from src.routes import main
        app = isolated_app.app
        app.extensions['tile_server_port'] = 5001
        captured = {}

        def fail_config():
            raise RuntimeError('force index fallback')

        monkeypatch.setattr(main, '_flat_config', fail_config)
        monkeypatch.setattr(
            main, 'render_template',
            lambda template, **kwargs: captured.update(kwargs) or '')

        response = app.test_client().get('/')

        assert response.status_code == 200
        assert captured['basemap']['tile_port'] == 5001

    def test_api_basemap_includes_tile_port(self, isolated_app):
        app = isolated_app.app
        app.extensions['tile_server_port'] = 5001
        client = app.test_client()
        j = json.loads(client.get('/api/basemap').data)
        assert j['basemap']['tile_port'] == 5001


_OPEN = '([{'
_CLOSE = ')]}'


def _tile_url_calls(js):
    """扫出每处 `tileUrl(` 的实参串与其中**顶层**逗号的个数。

    不能用 `tileUrl\\([^()\\n]*,` 这类正则：它靠「实参里没有括号」来找逗号，
    第一个实参自己带括号时就整条不匹配 —— `tileUrl(String(bm.url), port)`、
    `tileUrl(join(a, b), port)` 全是漏网的，而这恰恰是回归最可能长出来的样子
    （加一层包装函数，顺手把端口塞回第二个位置）。这里按括号配对走到匹配的
    右括号，只数深度为 1 的逗号，嵌套调用与 `${...}` 里的逗号都不算。
    """
    calls = []
    for m in re.finditer(r'\btileUrl\(', js):
        depth, commas, i = 1, 0, m.end()
        while i < len(js) and depth:
            c = js[i]
            if c in _OPEN:
                depth += 1
            elif c in _CLOSE:
                depth -= 1
            elif c == ',' and depth == 1:
                commas += 1
            i += 1
        assert depth == 0, f'tileUrl( 括号不配对：{js[m.start():m.start() + 60]!r}'
        calls.append((js[m.end():i - 1], commas))
    return calls


def test_tile_url_call_scanner_sees_through_nested_parentheses():
    """守卫的守卫：扫描器本身必须能识别「第一实参带括号」的两参调用。

    旧正则在这三条上全是绿的（漏报），扫描器必须报红；同时对象字面量的
    行尾逗号（`url: tileUrl(bm.url),`）不能误报，否则守卫会被当成噪音删掉。
    """
    assert [c for _, c in _tile_url_calls('tileUrl(bm.url)')] == [0]
    assert [c for _, c in _tile_url_calls('url: tileUrl(bm.url),')] == [0]
    assert [c for _, c in _tile_url_calls('tileUrl(`/t/${a.b}`)')] == [0]
    # 以下三条是旧正则 `tileUrl\([^()\n]*,` 检不出的两参调用
    assert [c for _, c in _tile_url_calls('tileUrl(String(bm.url), port)')] == [1]
    assert [c for _, c in _tile_url_calls('tileUrl(f(a, b), bm.tile_port)')] == [1]
    assert [c for _, c in _tile_url_calls('tileUrl(`/t/${f(x)}`, port)')] == [1]
    # 旧正则确实漏：把盲区钉成断言，免得有人「简化」回去
    assert not re.search(r'\btileUrl\([^()\n]*,', 'tileUrl(String(bm.url), port)')


class TestFrontendWiring:
    """前端必须真的把瓦片 URL 指到瓦片端口 —— 后端端口起了、前端没用等于白做。

    文本锚点与项目既有 JS 守卫同一口径（tests/test_splash_ready_signal.py）。
    """

    _JS_DIR = os.path.join(os.path.dirname(__file__), '..', 'static', 'js')

    def _read(self, name):
        with open(os.path.join(self._JS_DIR, name), encoding='utf-8') as f:
            return f.read()

    def test_ui_js_provides_tile_url_helper(self):
        js = self._read('ui.js')
        assert 'window.initTileOrigin = initTileOrigin;' in js
        assert 'window.tileUrl = tileUrl;' in js

    def test_map_js_routes_tiles_through_tile_url(self):
        """按路径逐条钉，不数次数。

        计数断言（`count('_tileUrl(`/') >= 3`）漏得掉最关键的那一处：晕渲回退
        的 PNG 不是模板串拼出来的路径、而是 /hillshade 返回体里的 hs.url，
        计数照样够 3、URL 却仍指主端口。改成逐条锚点后每类消费点各自可查。
        """
        js = self._read('map.js')
        assert 'url: tileUrl(bm.url)' in js
        # 二次入口 _tileUrl 已删：一个页面级解析器 tileUrl(path) 就够了
        assert '_tileUrl(' not in js
        for anchor in (
            'tileUrl(`/tiles/${task.id}`)',
            'tileUrl(`/contour/${task.id}`)',
            'tileUrl(`/terrain/local/${task.id}`)',
            'tileUrl(`/terrain/dem/${task.id}`)',
            'url: tileUrl(hs.url)',
        ):
            assert anchor in js, f'map.js 里找不到瓦片消费锚点：{anchor}'

    def test_tile_url_helper_takes_exactly_one_argument(self):
        """端口是会话状态，不是每次调用的入参 —— 调用点不许再传第二个实参。"""
        ui_js = self._read('ui.js')
        assert re.search(r'function tileUrl\(\s*path\s*\)', ui_js), (
            'ui.js 的 tileUrl 必须是单参数签名 tileUrl(path)')
        for name in ('map.js', 'history.js'):
            js = self._read(name)
            multi = [args for args, commas in _tile_url_calls(js) if commas]
            assert not multi, (
                f'{name} 里还有带第二个实参的 tileUrl(...) 调用：{multi}')

    def test_history_js_routes_basemap_through_tile_url(self):
        js = self._read('history.js')
        assert 'tileUrl(bm.url)' in js


def test_index_starts_tasks_while_tile_probe_is_pending():
    html = INDEX_HTML.read_text(encoding='utf-8')
    probe_boot_at = html.index("const tileOriginReady = _boot('tileOrigin', function ()")
    probe_at = html.index('initTileOrigin(basemapDescriptor.tile_port)', probe_boot_at)
    tasks_at = html.index("_boot('tasks', initTasks)")
    continuation_boot_at = html.index("_boot('mapReady', function ()")
    continuation_at = html.index('return tileOriginReady.then(function ()', continuation_boot_at)
    map_at = html.index("_boot('map'", continuation_at)
    assert probe_boot_at < probe_at < tasks_at < continuation_boot_at < continuation_at < map_at


def test_index_boot_isolates_promise_rejections():
    html = INDEX_HTML.read_text(encoding='utf-8')
    assert 'const result = fn();' in html
    assert 'return result.catch(function (e) {' in html
    assert "console.error('[boot] ' + label + ' failed:', e);" in html


def test_index_sync_probe_failure_keeps_a_resolved_gate():
    html = INDEX_HTML.read_text(encoding='utf-8')
    fallback = re.search(
        r"const\s+tileOriginReady\s*=\s*_boot\('tileOrigin',\s*function\s*\(\)\s*\{"
        r"\s*return\s+initTileOrigin\(basemapDescriptor\.tile_port\);\s*\}\)"
        r"\s*\|\|\s*Promise\.resolve\(false\);",
        html,
        re.S,
    )
    assert fallback, (
        '探测同步抛时必须用已决 Promise 兜住 mapReady 门控，不能让 viewer 初始化静默失效'
    )


def test_index_keeps_double_animation_frame_before_viewer():
    html = INDEX_HTML.read_text(encoding='utf-8')
    outer = html.index('requestAnimationFrame(function ()')
    inner = html.index('requestAnimationFrame(function ()', outer + 1)
    continuation_at = html.index("_boot('mapReady', function ()", inner)
    map_at = html.index("_boot('map'", continuation_at)
    assert outer < inner < continuation_at < map_at
