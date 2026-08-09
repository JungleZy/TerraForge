"""瓦片服务器列表（tile_servers）的扩展功能测试。

覆盖：
  - 条目展开语义（expand_server_entry：别名 / 主机 / 完整 XYZ 模板 / {style}）
  - 条目与列表校验（validate_server_entry / validate_server_list / ConfigManager）
  - 下载引擎真正消费 tile_servers（轮换、别名、模板、空配置回退）
  - POST /api/config/verify_tile_url：{server} 条目校验 400；通联成功/失败
  - 代理绕过（回环/内网地址不带 proxy_url）
  - 页面渲染：行编辑器；map.js 底图读 tile_servers
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core import database
from src.core.config import Config
from src.services.config_manager import ConfigManager
from src.services.download_engine import DownloadEngine
from src.services.tile_url_probe import (
    DEFAULT_TILE_SERVERS,
    build_probe_url,
    expand_server_entry,
    parse_server_list,
    probe_server_entry,
    should_bypass_proxy,
    validate_server_entry,
    validate_server_list,
)


def _load_app(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(Config, "CACHE_DIR", tmp_path / "cache")
    database.init_database()
    return DownloadEngine()


# --- 条目展开语义 -------------------------------------------------------------

def test_expand_alias_appends_googleapis_host():
    assert expand_server_entry('mts0', 'm') == \
        'http://mts0.googleapis.com/vt?lyrs=m&x={x}&y={y}&z={z}'


def test_expand_full_host_kept_as_is():
    assert expand_server_entry('mts0.google.cn', 's') == \
        'http://mts0.google.cn/vt?lyrs=s&x={x}&y={y}&z={z}'


def test_expand_template_substitutes_style_placeholder():
    assert expand_server_entry('https://t.example.com/vt?lyrs={style}&x={x}&y={y}&z={z}', 'y') == \
        'https://t.example.com/vt?lyrs=y&x={x}&y={y}&z={z}'


def test_expand_template_without_style_placeholder_used_as_is():
    tpl = 'https://t.example.com/{z}/{x}/{y}.png'
    assert expand_server_entry(tpl, 's') == tpl


def test_parse_server_list_falls_back_to_default():
    assert parse_server_list('') == DEFAULT_TILE_SERVERS.split(',')
    assert parse_server_list(' , , ') == DEFAULT_TILE_SERVERS.split(',')
    assert parse_server_list(' a, b ,, c ') == ['a', 'b', 'c']


# --- 条目与列表校验 -------------------------------------------------------------

@pytest.mark.parametrize('entry,ok', [
    ('mts0', True),
    ('mts3', True),
    ('mts0.google.cn', True),
    ('https://tile.openstreetmap.org/{z}/{x}/{y}.png', True),
    ('http://192.168.1.10:8080/tiles/{z}/{x}/{y}.png', True),
    ('https://t.example.com/vt?lyrs={style}&x={x}&y={y}&z={z}', True),
    ('', False),
    ('ftp://t.example.com/{z}/{x}/{y}.png', False),
    ('https://t.example.com/{z}/{x}.png', False),      # 缺 {y}
    ('https://{s}.example.com/{z}/{x}/{y}.png', False),  # {s} 下载引擎不替换
    ('https://t.example.com/{z}/{x}/{y}.png?key={token}', False),  # 未知占位符
    ('mts 0', False),                                 # 空格
    ('mts0/evil', False),                             # 主机形态不许带路径
])
def test_validate_server_entry(entry, ok):
    assert validate_server_entry(entry)[0] is ok


def test_validate_server_entry_unknown_placeholder_message():
    ok, err = validate_server_entry('https://{s}.example.com/{z}/{x}/{y}.png')
    assert ok is False
    assert '{s}' in err


def test_validate_server_list_requires_at_least_one_valid_entry():
    assert validate_server_list('mts0,mts1')[0] is True
    assert validate_server_list('')[0] is False
    assert validate_server_list(' , ,')[0] is False
    ok, err = validate_server_list('mts0,https://bad/{z}/{x}.png')
    assert ok is False and 'bad' in err


def test_config_manager_validates_tile_servers():
    cm = ConfigManager()
    assert cm.validate_config('tile_servers', 'mts0,mts1,mts2,mts3') is True
    assert cm.validate_config(
        'tile_servers', 'mts0.google.cn,https://t.example.com/{z}/{x}/{y}.png') is True
    assert cm.validate_config('tile_servers', '') is False
    assert cm.validate_config('tile_servers', 'https://t.example.com/{z}/{x}.png') is False


# --- 下载引擎消费 tile_servers ---------------------------------------------------

def test_engine_default_list_matches_legacy_google_urls(engine):
    """默认配置下 URL 必须与硬编码时代逐字一致（行为不回归）。"""
    for i in range(4):
        url = engine.get_tile_url(x=843, y=368, z=10, style='m', server_index=i)
        assert url == f'http://mts{i}.googleapis.com/vt?lyrs=m&x=843&y=368&z=10'
    # 索引超出列表长度回绕
    assert engine.get_tile_url(0, 0, 0, 'm', 4).startswith('http://mts0.')


def test_engine_uses_configured_template_entry(engine):
    cm = ConfigManager()
    cm.set('tile_servers', 'https://tiles.lan/{z}/{x}/{y}.png,mts1')
    url = engine.get_tile_url(x=1, y=2, z=3, style='s', server_index=0)
    assert url == 'https://tiles.lan/3/1/2.png'
    # 轮换到第二个条目（样式对无 {style} 的模板无效，对别名生效）
    assert engine.get_tile_url(1, 2, 3, 's', 1) == \
        'http://mts1.googleapis.com/vt?lyrs=s&x=1&y=2&z=3'


def test_engine_template_with_style_placeholder(engine):
    cm = ConfigManager()
    cm.set('tile_servers', 'https://g.mirror.lan/vt?lyrs={style}&x={x}&y={y}&z={z}')
    assert engine.get_tile_url(5, 6, 7, 'y', 0) == \
        'https://g.mirror.lan/vt?lyrs=y&x=5&y=6&z=7'


def test_engine_falls_back_when_config_empty(engine):
    cm = ConfigManager()
    cm.set('proxy_url', '')  # 不相关的键，确认 engine 不因其它配置受影响
    database.get_connection_context
    with database.get_connection_context() as conn:
        conn.execute("UPDATE config SET value='' WHERE key='tile_servers'")
        conn.commit()
    fresh = DownloadEngine()
    assert fresh.get_tile_url(0, 0, 0, 'm', 0).startswith('http://mts0.googleapis.com')


# --- 样例瓦片 URL 展开 ----------------------------------------------------------

def test_build_probe_url_expands_center_tile_at_z3():
    url, (z, x, y) = build_probe_url(
        'https://t.example.com/{z}/{x}/{y}.png', 106.55, 29.56)
    assert (z, x, y) == (3, 6, 3)
    assert url == 'https://t.example.com/3/6/3.png'


# --- 探测（fake fetcher，无网） --------------------------------------------------

async def _fake_ok(url, proxy_url, timeout_s):
    assert url.endswith('/3/6/3.png')
    return {'success': True, 'status_code': 200, 'content_type': 'image/png',
            'elapsed_ms': 12, 'bytes_read': 1024, 'error': None}


async def _fake_refused(url, proxy_url, timeout_s):
    return {'success': False, 'status_code': None, 'content_type': '',
            'elapsed_ms': 3, 'bytes_read': 0, 'error': '连接失败：Connection refused'}


def test_probe_alias_builds_google_url():
    seen = []

    async def _spy(url, proxy_url, timeout_s):
        seen.append(url)
        return {'success': True, 'status_code': 200, 'content_type': 'image/png',
                'elapsed_ms': 1, 'bytes_read': 1, 'error': None}

    result = probe_server_entry('mts2', fetcher=_spy)
    assert seen[0] == 'http://mts2.googleapis.com/vt?lyrs=m&x=6&y=3&z=3'
    assert result['success'] is True
    assert result['tile'] == '3/6/3'


def test_probe_failure_path_is_a_result_not_an_exception():
    result = probe_server_entry(
        'https://t.example.com/{z}/{x}/{y}.png', fetcher=_fake_refused)
    assert result['success'] is False
    assert '连接失败' in result['error']


def test_probe_rejects_invalid_entry_before_fetching():
    called = []

    async def _spy(url, proxy_url, timeout_s):
        called.append(url)
        return {}

    result = probe_server_entry('https://t.example.com/{z}/{x}.png', fetcher=_spy)
    assert result['success'] is False
    assert result['tile'] is None
    assert called == [], '条目非法时不许发起任何网络请求'


def test_probe_log_masks_url_userinfo(caplog):
    """模板内嵌 user:pass@ 时，凭据不得进探测日志（host 保留便于排查）。"""
    import logging

    async def _fake(url, proxy_url, timeout_s):
        assert 'user:pass@' in url  # 实际请求仍用完整 URL
        return {'success': True, 'status_code': 200, 'content_type': 'image/png',
                'elapsed_ms': 1, 'bytes_read': 1, 'error': None}

    with caplog.at_level(logging.INFO, logger='src.services.tile_url_probe'):
        result = probe_server_entry(
            'https://user:pass@t.example.com/{z}/{x}/{y}.png', fetcher=_fake)
    assert result['success'] is True
    assert 'user:pass@' not in caplog.text
    assert '***:***@t.example.com' in caplog.text


# --- 代理绕过（本机/内网地址不走 proxy_url） --------------------------------------

@pytest.mark.parametrize('url,bypass', [
    ('http://127.0.0.1:8765/3/6/3.png', True),
    ('http://localhost:8765/3/6/3.png', True),
    ('http://192.168.1.10:8080/3/6/3.png', True),
    ('http://10.0.0.5/3/6/3.png', True),
    ('https://tile.openstreetmap.org/3/6/3.png', False),
])
def test_should_bypass_proxy(url, bypass):
    assert should_bypass_proxy(url) is bypass


def test_probe_drops_proxy_for_loopback_but_keeps_it_for_public():
    """proxy_url 是给公网源配的；探测本机/内网地址时必须摘掉，
    否则请求被代理转发到它自己到不了的地方（WSL：代理在宿主机上，
    回环地址各是各的），表现为挂起到超时。"""
    seen = []

    async def _spy(url, proxy_url, timeout_s):
        seen.append((url, proxy_url))
        return {'success': True, 'status_code': 200, 'content_type': 'image/png',
                'elapsed_ms': 1, 'bytes_read': 1, 'error': None}

    probe_server_entry('http://127.0.0.1:8765/{z}/{x}/{y}.png',
                       proxy_url='http://proxy:7890', fetcher=_spy)
    probe_server_entry('mts0', proxy_url='http://proxy:7890', fetcher=_spy)
    assert seen[0][1] == '', '回环地址不应带代理'
    assert seen[1][1] == 'http://proxy:7890', '公网地址必须保留代理'


# --- API 端点 ---------------------------------------------------------------------

def test_put_config_accepts_valid_server_list(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    resp = client.put('/api/config', json={
        'tile_servers': 'mts0.google.cn,http://192.168.1.10:8080/{z}/{x}/{y}.png'})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True


def test_put_config_rejects_invalid_server_list(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    resp = client.put('/api/config', json={
        'tile_servers': 'https://t.example.com/{z}/{x}.png'})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_verify_endpoint_rejects_bad_entry_with_400(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    resp = client.post('/api/config/verify_tile_url',
                       json={'server': 'ftp://t.example.com/{z}/{x}/{y}.png'})
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_verify_endpoint_success_with_mocked_fetch(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr('src.services.tile_url_probe._fetch_tile', _fake_ok)
    resp = client.post('/api/config/verify_tile_url',
                       json={'server': 'https://t.example.com/{z}/{x}/{y}.png'})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data['success'] is True
    assert data['tile'] == '3/6/3'


def test_verify_endpoint_unreachable_host_returns_failure_result(monkeypatch, tmp_path):
    """本地 9 号端口（discard）必然拒绝连接：无网环境下的真实失败路径。"""
    client = _load_app(monkeypatch, tmp_path)
    resp = client.post('/api/config/verify_tile_url',
                       json={'server': 'http://127.0.0.1:9/{z}/{x}/{y}.png'})
    data = resp.get_json()
    assert resp.status_code == 200      # 连不上也是一次成功的探测
    assert data['success'] is False
    assert data['error']


# --- 页面渲染与前端接线 --------------------------------------------------------------

def test_config_partial_renders_server_row_editor(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    for path in ('/', '/config'):
        html = client.get(path).get_data(as_text=True)
        assert 'id="tileServerRows"' in html, f'{path} 缺少服务器行容器'
        assert 'id="tileServerAdd"' in html, f'{path} 缺少添加按钮'
        assert 'tile-server-row' in html, f'{path} 没有用现有配置渲染初始行'
        assert 'tile-server-verify' in html, f'{path} 缺少逐条验证按钮'
        assert 'id="map_tile_url"' not in html, f'{path} 不该再有独立的底图地址字段'
        # 默认配置 mts0-mts3 应渲染成 4 行
        assert html.count('tile-server-input') >= 4


def test_map_js_consumes_server_resolved_basemap():
    """底图不再从 tile_servers 推导 —— 它有自己的配置项 basemap_source。

    分开的理由是**用途**不同，不是出网路径不同：底图给页面看、tile_servers 是
    下载源。自 0.2.12 起底图瓦片由 routes/basemap_static 在服务端同源转发，
    与下载走同一条出网路径、同样吃 proxy_url —— 这条注释此前写的「底图走浏览器
    直连、不吃 proxy_url」是转发之前的旧事实。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', 'map.js'), encoding='utf-8') as f:
        src = f.read()
    assert 'config.tile_servers' not in src, (
        '底图已改由 basemap_source 决定，map.js 不该再读下载源列表'
    )
    assert 'map_tile_url' not in src, '独立的 map_tile_url 已并入 tile_servers'
    assert 'basemap' in src, 'map.js 必须消费服务端解析好的 basemap'


def test_index_route_injects_resolved_basemap(monkeypatch, tmp_path):
    """首页把底图图层描述渲染进页面，但地址是**同源**的转发路径。

    上游地址不下发：浏览器直连上游会撞 CORS（上游 4xx 时真实状态码被埋成
    一句 CORS 报错），而且浏览器不吃项目的 proxy_url。瓦片由
    routes/basemap_static.py 转发，行为细节见 tests/test_basemap_proxy_route.py。
    """
    client = _load_app(monkeypatch, tmp_path)
    html = client.get('/').get_data(as_text=True)
    assert 'initMap(config,' in html, '首页必须把 basemap 传给 initMap'
    assert '/basemap/{z}/{x}/{y}' in html
    # 默认预设是 Esri，但它的地址只存在于服务端。
    assert '"source": "esri"' in html
    assert 'arcgisonline' not in html, '上游地址不该出现在页面里'


def test_config_partial_renders_basemap_selector(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    for path in ('/', '/config'):
        html = client.get(path).get_data(as_text=True)
        assert 'id="basemap_source_preset"' in html, f'{path} 缺少底图源下拉'
        assert 'id="basemap_source_custom"' in html, f'{path} 缺少自定义模板输入框'
        assert 'value="download_source"' in html, f'{path} 缺少「跟随下载源」选项'
        # 默认值应当预选中 Esri
        assert 'value="esri" selected' in html, f'{path} 未按已保存值预选'


def test_config_js_collects_basemap_source():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', 'config.js'), encoding='utf-8') as f:
        src = f.read()
    assert 'function collectBasemapSource(' in src, (
        '下拉 + 自定义输入框两个控件必须合回一个字符串再提交'
    )
    assert 'basemap_source: collectBasemapSource()' in src, (
        'saveConfig 漏了 basemap_source —— 改了下拉点保存不会生效'
    )


def test_config_js_row_editor_and_verify_wiring():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', 'config.js'), encoding='utf-8') as f:
        src = f.read()
    assert 'function addTileServerRow(' in src
    assert 'function collectTileServers(' in src, '保存时必须把行合并回逗号分隔'
    assert 'verifyTileServerRow' in src
    assert '/api/config/verify_tile_url' in src
    assert 'map_tile_url' not in src
