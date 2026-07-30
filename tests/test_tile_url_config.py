"""底图瓦片服务地址（map_tile_url）的配置与通联验证测试。

覆盖：
  - 默认值播种（DEFAULT_CONFIGS 含 map_tile_url，存量库靠 INSERT OR IGNORE 补齐）
  - 模板校验（validate_tile_url_template / ConfigManager.validate_config）
  - PUT /api/config 接受合法模板、拒绝非法模板
  - POST /api/config/verify_tile_url：模板非法 400；通联成功/失败（fake fetcher 与
    本地拒绝连接两条无网路径）
  - 页面渲染：配置面板与首页都带 #map_tile_url；map.js 的底图源走 config.map_tile_url
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.database import DEFAULT_CONFIGS
from services.config_manager import ConfigManager
from services.tile_url_probe import (
    DEFAULT_MAP_TILE_URL,
    build_probe_url,
    probe_tile_url,
    should_bypass_proxy,
    validate_tile_url_template,
)


def _load_app(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


# --- 默认值播种 -------------------------------------------------------------

def test_default_configs_seed_map_tile_url():
    pairs = dict(DEFAULT_CONFIGS)
    assert pairs.get('map_tile_url') == DEFAULT_MAP_TILE_URL


# --- 模板校验（纯函数 + ConfigManager 入口） ---------------------------------

@pytest.mark.parametrize('url,ok', [
    ('https://tile.openstreetmap.org/{z}/{x}/{y}.png', True),
    ('http://192.168.1.10:8080/tiles/{z}/{x}/{y}.png', True),
    ('', False),                                   # 探测侧：空地址拒绝
    ('ftp://tiles.example.com/{z}/{x}/{y}.png', False),
    ('https://tiles.example.com/{z}/{x}.png', False),   # 缺 {y}
    ('https://tiles.example.com/{z}/{y}/{y}.png', False),  # 缺 {x}
    ('not-a-url', False),
])
def test_validate_tile_url_template(url, ok):
    assert validate_tile_url_template(url)[0] is ok


def test_config_manager_validation_allows_empty_but_rejects_bad_template():
    cm = ConfigManager()
    # 留空 = 前端回退内置 OSM 源，是合法配置
    assert cm.validate_config('map_tile_url', '') is True
    assert cm.validate_config('map_tile_url', 'https://t.example.com/{z}/{x}/{y}.png') is True
    assert cm.validate_config('map_tile_url', 'https://t.example.com/{z}/{x}.png') is False
    assert cm.validate_config('map_tile_url', 'ftp://t.example.com/{z}/{x}/{y}.png') is False


# --- 样例瓦片 URL 展开 --------------------------------------------------------

def test_build_probe_url_expands_center_tile_at_z3():
    url, (z, x, y) = build_probe_url(
        'https://t.example.com/{z}/{x}/{y}.png', 106.55, 29.56)
    assert (z, x, y) == (3, 6, 3)
    assert url == 'https://t.example.com/3/6/3.png'


# --- 探测（fake fetcher，无网） -----------------------------------------------

async def _fake_ok(url, proxy_url, timeout_s):
    assert url.endswith('/3/6/3.png')
    return {'success': True, 'status_code': 200, 'content_type': 'image/png',
            'elapsed_ms': 12, 'bytes_read': 1024, 'error': None}


async def _fake_refused(url, proxy_url, timeout_s):
    return {'success': False, 'status_code': None, 'content_type': '',
            'elapsed_ms': 3, 'bytes_read': 0, 'error': '连接失败：Connection refused'}


def test_probe_success_path_reports_tile_and_status():
    result = probe_tile_url(
        'https://t.example.com/{z}/{x}/{y}.png', fetcher=_fake_ok)
    assert result['success'] is True
    assert result['status_code'] == 200
    assert result['tile'] == '3/6/3'


def test_probe_failure_path_is_a_result_not_an_exception():
    result = probe_tile_url(
        'https://t.example.com/{z}/{x}/{y}.png', fetcher=_fake_refused)
    assert result['success'] is False
    assert '连接失败' in result['error']


def test_probe_rejects_invalid_template_before_fetching():
    called = []

    async def _spy(url, proxy_url, timeout_s):
        called.append(url)
        return {}

    result = probe_tile_url('https://t.example.com/{z}/{x}.png', fetcher=_spy)
    assert result['success'] is False
    assert result['tile'] is None
    assert called == [], '模板非法时不许发起任何网络请求'


# --- 代理绕过（本机/内网地址不走 proxy_url） ------------------------------------

@pytest.mark.parametrize('url,bypass', [
    ('http://127.0.0.1:8765/{z}/{x}/{y}.png', True),
    ('http://localhost:8765/{z}/{x}/{y}.png', True),
    ('http://192.168.1.10:8080/{z}/{x}/{y}.png', True),
    ('http://10.0.0.5/{z}/{x}/{y}.png', True),
    ('https://tile.openstreetmap.org/{z}/{x}/{y}.png', False),
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

    probe_tile_url('http://127.0.0.1:8765/{z}/{x}/{y}.png',
                   proxy_url='http://proxy:7890', fetcher=_spy)
    probe_tile_url('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                   proxy_url='http://proxy:7890', fetcher=_spy)
    assert seen[0][1] == '', '回环地址不应带代理'
    assert seen[1][1] == 'http://proxy:7890', '公网地址必须保留代理'


# --- API 端点 -----------------------------------------------------------------

def test_put_config_accepts_valid_tile_url(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    resp = client.put('/api/config', json={
        'map_tile_url': 'http://192.168.1.10:8080/{z}/{x}/{y}.png'})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True


def test_put_config_rejects_invalid_tile_url(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    resp = client.put('/api/config', json={
        'map_tile_url': 'https://t.example.com/{z}/{x}.png'})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_verify_endpoint_rejects_bad_template_with_400(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    resp = client.post('/api/config/verify_tile_url',
                       json={'url': 'ftp://t.example.com/{z}/{x}/{y}.png'})
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_verify_endpoint_success_with_mocked_fetch(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr('services.tile_url_probe._fetch_tile', _fake_ok)
    resp = client.post('/api/config/verify_tile_url',
                       json={'url': 'https://t.example.com/{z}/{x}/{y}.png'})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data['success'] is True
    assert data['tile'] == '3/6/3'


def test_verify_endpoint_unreachable_host_returns_failure_result(monkeypatch, tmp_path):
    """本地 9 号端口（discard）必然拒绝连接：无网环境下的真实失败路径。"""
    client = _load_app(monkeypatch, tmp_path)
    resp = client.post('/api/config/verify_tile_url',
                       json={'url': 'http://127.0.0.1:9/{z}/{x}/{y}.png'})
    data = resp.get_json()
    assert resp.status_code == 200      # 连不上也是一次成功的探测
    assert data['success'] is False
    assert data['error']


# --- 页面渲染与前端接线 --------------------------------------------------------

def test_config_partial_and_index_render_tile_url_field(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    for path in ('/', '/config'):
        html = client.get(path).get_data(as_text=True)
        assert 'id="map_tile_url"' in html, f'{path} 缺少底图地址输入框'
        assert 'id="verifyTileUrlBtn"' in html, f'{path} 缺少验证通联按钮'
        assert 'id="tileUrlVerifyResult"' in html, f'{path} 缺少验证结果容器'


def test_map_js_base_layer_uses_configured_tile_url():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', 'map.js'), encoding='utf-8') as f:
        src = f.read()
    assert 'config.map_tile_url' in src, (
        'map.js 的底图源必须读 config.map_tile_url（留空回退内置 OSM）'
    )
    assert DEFAULT_MAP_TILE_URL in src, 'map.js 里找不到内置 OSM 回退地址'


def test_config_js_saves_tile_url_and_wires_verify_button():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', 'config.js'), encoding='utf-8') as f:
        src = f.read()
    assert 'map_tile_url' in src, 'saveConfig 必须提交 map_tile_url'
    assert 'async function verifyTileUrl(' in src
    assert '/api/config/verify_tile_url' in src
