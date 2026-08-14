"""两道核心缝：create_task 的 source_snapshot 覆盖、get_tile_url 的 {credential}。

两道缝都是**通用**形状（§13-4：核心只认合同，不认具体数据源）：
任何调用方冻结好的快照都能覆盖建任务时的取源；任何带 credential_reference
的快照都能用 {credential} 占位符。插件源只是第一批调用方。

本文件同时钉住三条硬要求：
  · 凭据真值只在发请求那一瞬出现——任务行/指纹里只有键名；
  · token 轮换不改指纹（否则缓存命名空间整体失效、已下瓦片全废）；
  · 不带 source_snapshot 的存量路径行为一字不变。
"""

import json
import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.contracts.source import SourceSnapshot

#: 天地图形状的假源：{s} 与 {credential} 同时出现——两个替换互不破坏是本
#: 任务的第五条要求。真正的天地图插件在 Task 12，这里只借它的模板形状。
FAKE_PLUGIN = '''
from src.plugins.protocols import PluginDefinition, SourceDescriptor

def register():
    return PluginDefinition(
        sources=(SourceDescriptor(
            source_id="img", name="假影像源",
            url_template="https://t{s}.example.gov.cn/img_w/wmts"
                         "?tk={credential}&x={x}&y={y}&z={z}",
            max_zoom=18, attribution="示例",
            subdomains=("0", "1", "2"),
            credential_key="token"),),
    )
'''


def _snapshot():
    return SourceSnapshot(
        source_id='plugin:fake:img',
        url_template='https://t{s}.example.gov.cn/img_w/wmts?tk={credential}'
                     '&x={x}&y={y}&z={z}',
        style='p', subdomains=('0', '1'),
        credential_reference='plugin:fake:token')


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张真库。写法同 tests/test_plugin_task_manager.py:111-130（conftest 无 db）。"""
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


@pytest.fixture
def fake_plugin(db, tmp_path, monkeypatch):
    """装好并启用上面那个假插件，返回 registry 模块。"""
    from src.plugins import credentials, registry

    monkeypatch.setattr(registry, '_plugins_root', lambda: tmp_path / 'plugins')
    d = tmp_path / 'plugins' / 'fake'
    d.mkdir(parents=True)
    (d / 'plugin.toml').write_text(
        'id="fake"\nname="fake"\nversion="0.1"\napi_version="1"\n'
        'capabilities=["sources"]\n', encoding='utf-8')
    (d / 'plugin.py').write_text(FAKE_PLUGIN, encoding='utf-8')
    registry.reset_for_tests()
    registry.load_all()
    registry.set_enabled('fake', True)
    credentials.invalidate()
    yield registry
    registry.reset_for_tests()
    credentials.invalidate()


def _params(tmp_path, **extra):
    params = {
        'name': 'seam', 'north': 40.0, 'south': 39.0,
        'east': 117.0, 'west': 116.0, 'zoom_min': 3, 'zoom_max': 4,
        'style': 'satellite', 'output_format': 'tiles_only',
        'output_path': str(tmp_path / 'out'),
    }
    params.update(extra)
    return params


def _stored_snapshot(db, task_id):
    conn = sqlite3.connect(db)
    try:
        return conn.execute('SELECT source_snapshot FROM tasks WHERE id = ?',
                            (task_id,)).fetchone()[0]
    finally:
        conn.close()


# ---- 缝 1：get_tile_url 的 {credential} ------------------------------


def test_get_tile_url_resolves_credential(monkeypatch):
    from src.plugins import credentials
    from src.services.download_engine import DownloadEngine

    monkeypatch.setattr(credentials, 'resolve_reference',
                        lambda ref: 'SEKRET' if ref == 'plugin:fake:token' else '')
    engine = DownloadEngine.__new__(DownloadEngine)  # 不跑 __init__，只测 URL 拼装
    url = engine.get_tile_url(5, 6, 3, 'p', 1, source=_snapshot())

    assert 'tk=SEKRET' in url and '{credential}' not in url
    assert url.startswith('https://t1.example.gov.cn/')  # server_index=1 轮换子域
    assert 'x=5' in url and 'y=6' in url and 'z=3' in url


def test_get_tile_url_missing_credential_leaves_it_empty(monkeypatch):
    """解析不到 → 空串（不抛）。URL 那段变空，上游 401，瓦片层记账。"""
    from src.plugins import credentials
    from src.services.download_engine import DownloadEngine

    monkeypatch.setattr(credentials, 'resolve_reference', lambda ref: '')
    engine = DownloadEngine.__new__(DownloadEngine)
    url = engine.get_tile_url(1, 2, 3, 'p', 0, source=_snapshot())

    assert 'tk=&' in url and '{credential}' not in url


def test_get_tile_url_without_placeholder_never_touches_credentials(monkeypatch):
    """存量模板（无 {credential}）一次都不该去解析凭据。"""
    from src.plugins import credentials
    from src.services.download_engine import DownloadEngine

    calls = []
    monkeypatch.setattr(credentials, 'resolve_reference',
                        lambda ref: calls.append(ref) or '')
    snap = SourceSnapshot(source_id='builtin', style='s',
                          url_template='https://mt0.example.com/vt/x={x}&y={y}&z={z}')
    engine = DownloadEngine.__new__(DownloadEngine)
    url = engine.get_tile_url(1, 2, 3, 's', 0, source=snap)

    assert url == 'https://mt0.example.com/vt/x=1&y=2&z=3'
    assert calls == []


# ---- 指纹 ------------------------------------------------------------


def test_snapshot_fingerprint_excludes_credential_value():
    a = _snapshot()
    b = SourceSnapshot(**{k: v for k, v in a.to_dict().items()
                          if k != 'fingerprint'} | {
        'url_template': a.url_template.replace('{credential}', 'SEKRET')})
    # 占位符形态与实值形态指纹不同——这正是设计：任务行存的是占位符形态
    assert a.fingerprint != b.fingerprint
    assert '{credential}' in a.url_template


def test_fingerprint_survives_token_rotation(fake_plugin):
    """换 token 不改指纹——否则缓存命名空间整体翻新，已下的瓦片全部失效。"""
    from src.plugins import credentials

    fake_plugin.set_config('fake', {'token': 'TOKEN-A'})
    credentials.invalidate()
    before = fake_plugin.build_source_snapshot('fake', 'img')

    fake_plugin.set_config('fake', {'token': 'TOKEN-B'})
    credentials.invalidate()
    after = fake_plugin.build_source_snapshot('fake', 'img')

    assert before.fingerprint == after.fingerprint
    assert before.cache_namespace == after.cache_namespace
    assert credentials.resolve_reference('plugin:fake:token') == 'TOKEN-B'


# ---- 缝 2：create_task 的 source_snapshot 覆盖 ------------------------


def test_create_task_snapshot_override(db, tmp_path):
    from src.services.task_manager import TaskManager

    mgr = TaskManager(socketio=None)
    tid = mgr.create_task(_params(tmp_path, source_snapshot=_snapshot().to_json()))

    stored = _stored_snapshot(db, tid)
    assert stored and 'plugin:fake:img' in stored
    assert json.loads(stored)['fingerprint'] == _snapshot().fingerprint


@pytest.mark.parametrize('override', [None, '', 'not json at all', '{"broken":'])
def test_create_task_falls_back_to_style_snapshot(db, tmp_path, override):
    """没给（或给坏了）快照 → 按 style 现算，与改造前逐字一致。"""
    from src.services import source_registry
    from src.services.task_manager import TaskManager

    mgr = TaskManager(socketio=None)
    extra = {} if override is None else {'source_snapshot': override}
    tid = mgr.create_task(_params(tmp_path, **extra))

    expected = source_registry.snapshot_for_style('s', mgr.config_manager)
    stored = json.loads(_stored_snapshot(db, tid))
    assert stored['fingerprint'] == expected.fingerprint
    assert stored['url_template'] == expected.url_template


# ---- 路由接线 --------------------------------------------------------


def test_api_injects_snapshot_for_plugin_source(fake_plugin, tmp_path, monkeypatch):
    from src.routes import api as api_mod

    captured = {}

    class _Mgr:
        def create_task(self, params):
            captured.update(params)
            return 7

    monkeypatch.setattr(api_mod, 'task_manager', _Mgr())
    app = _flask_app(api_mod)
    resp = app.test_client().post('/api/tasks', json=_params(
        tmp_path, source_plugin_id='fake', source_id='img'))

    assert resp.status_code == 201
    snap = SourceSnapshot.from_json(captured['source_snapshot'])
    assert snap.source_id == 'plugin:fake:img'
    assert snap.credential_reference == 'plugin:fake:token'


def test_api_unknown_plugin_source_is_400(fake_plugin, tmp_path, monkeypatch):
    from src.routes import api as api_mod

    class _Mgr:
        def create_task(self, params):  # pragma: no cover - 不该被调到
            raise AssertionError('未知插件源不该走到建任务')

    monkeypatch.setattr(api_mod, 'task_manager', _Mgr())
    app = _flask_app(api_mod)
    resp = app.test_client().post('/api/tasks', json=_params(
        tmp_path, source_plugin_id='fake', source_id='nope'))

    assert resp.status_code == 400


def test_api_without_plugin_source_adds_no_field(db, tmp_path, monkeypatch):
    """存量请求体：一个新字段都不加。"""
    from src.routes import api as api_mod

    captured = {}

    class _Mgr:
        def create_task(self, params):
            captured.update(params)
            return 9

    monkeypatch.setattr(api_mod, 'task_manager', _Mgr())
    app = _flask_app(api_mod)
    resp = app.test_client().post('/api/tasks', json=_params(tmp_path))

    assert resp.status_code == 201
    assert 'source_snapshot' not in captured


def _flask_app(api_mod):
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(api_mod.api_bp, url_prefix='/api')
    return app


# ---- 端到端：注册表 → 建任务 → 落库 → 请求 URL ----------------------


def test_full_chain_credential_never_lands_in_the_task_row(fake_plugin, db, tmp_path):
    """一条真链路：冻结快照 → 建任务 → 从库里读回 → 拼 URL。

    三条断言合起来就是本任务的安全边界：URL 里有真 token、库里没有、
    换 token 指纹不变。
    """
    from src.plugins import credentials
    from src.services.download_engine import DownloadEngine
    from src.services.task_manager import TaskManager

    fake_plugin.set_config('fake', {'token': 'TOP-SECRET-TOKEN'})
    credentials.invalidate()

    snapshot = fake_plugin.build_source_snapshot('fake', 'img')
    mgr = TaskManager(socketio=None)
    tid = mgr.create_task(_params(tmp_path, source_snapshot=snapshot.to_json()))

    stored_text = _stored_snapshot(db, tid)
    assert 'TOP-SECRET-TOKEN' not in stored_text          # ① 库里没有真值
    assert '{credential}' in stored_text
    assert 'plugin:fake:token' in stored_text             # 只有键名

    restored = SourceSnapshot.from_json(stored_text)
    engine = DownloadEngine.__new__(DownloadEngine)
    url = engine.get_tile_url(11, 22, 5, restored.style, 4, source=restored)

    assert 'tk=TOP-SECRET-TOKEN' in url                   # ② 请求时才展开
    assert url.startswith('https://t1.example.gov.cn/')   # {s}: 4 % 3 == 1
    assert 'x=11&y=22&z=5' in url

    fingerprint_before = restored.fingerprint
    fake_plugin.set_config('fake', {'token': 'ROTATED'})
    credentials.invalidate()
    assert fake_plugin.build_source_snapshot(
        'fake', 'img').fingerprint == fingerprint_before  # ③ 轮换不动指纹

    # 整行任务记录（含日志会打印的 summary）里都不该出现真 token。
    conn = sqlite3.connect(db)
    try:
        row = conn.execute('SELECT * FROM tasks WHERE id = ?', (tid,)).fetchone()
    finally:
        conn.close()
    assert 'TOP-SECRET-TOKEN' not in ' '.join(str(v) for v in row)
    assert 'TOP-SECRET-TOKEN' not in restored.summary()
