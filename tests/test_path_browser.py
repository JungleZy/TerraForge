"""保存路径绝对化 + 「浏览」目录选择 —— 入口校验、浏览 API、默认值迁移、前端接线。

口径(2026-08 定):
- 新建任务的 output_path 与配置的 default_save_path 一律要求**绝对路径**,
  且仍须落在 Config.DOWNLOADS_DIR 内(边界不变);相对输入直接 400,
  报错文案指路「浏览」按钮;
- GET /api/fs/browse?path= 给「浏览」按钮的目录选择弹窗供数据:只列
  DOWNLOADS_DIR 内的子目录,越界/不存在/非目录一律 400;
- 存量相对 default_save_path(如 './downloads')在 init_database 一次性
  归一成绝对路径 —— UI 不再有相对值可显示。
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _load_app(monkeypatch, tmp_path):
    from core.config import Config
    monkeypatch.setattr(Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


# --- require_absolute_output_dir ---------------------------------------------

@pytest.fixture()
def downloads(tmp_path, monkeypatch):
    from core import config
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    return tmp_path / "downloads"


def test_relative_input_rejected_with_browse_hint(downloads):
    from services.geo_validation import require_absolute_output_dir
    with pytest.raises(ValueError, match='绝对路径'):
        require_absolute_output_dir('./downloads/map')


def test_absolute_inside_downloads_accepted(downloads):
    from services.geo_validation import require_absolute_output_dir
    assert require_absolute_output_dir(str(downloads / 'map')) == str(downloads / 'map')


def test_absolute_outside_downloads_still_rejected(downloads, tmp_path):
    from services.geo_validation import require_absolute_output_dir
    with pytest.raises(ValueError):
        require_absolute_output_dir(str(tmp_path / 'elsewhere'))


def test_dotdot_absolute_escape_rejected(downloads):
    from services.geo_validation import require_absolute_output_dir
    with pytest.raises(ValueError):
        require_absolute_output_dir(str(downloads / '..' / 'outside'))


# --- create_task / 配置保存 入口的绝对路径要求 ---------------------------------

def _task_params(**overrides):
    p = dict(name='t', north=40.0, south=39.0, east=117.0, west=116.0,
             zoom_min=10, zoom_max=11, style='roadmap',
             output_format='tiles_only')
    p.update(overrides)
    return p


def test_create_task_rejects_relative_output_path(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.post('/api/tasks', json=_task_params(output_path='./downloads/map'))
    assert resp.status_code == 400
    assert '绝对路径' in resp.get_json()['error']


def test_create_task_accepts_absolute_output_path(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.post('/api/tasks',
                       json=_task_params(output_path=str(tmp_path / 'downloads' / 'map')))
    assert resp.status_code == 201, resp.get_json()


def test_put_config_rejects_relative_default_save_path(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.put('/api/config', json={'default_save_path': './downloads'})
    assert resp.status_code == 400


def test_put_config_accepts_absolute_default_save_path(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.put('/api/config',
                      json={'default_save_path': str(tmp_path / 'downloads')})
    assert resp.status_code == 200, resp.get_json()


# --- 存量相对默认值一次性归一 ---------------------------------------------------

def test_init_database_normalizes_relative_default_save_path(monkeypatch, tmp_path):
    from core.config import Config
    monkeypatch.setattr(Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    from core import database
    database.init_database()
    from services.config_manager import ConfigManager
    value = ConfigManager().get('default_save_path')
    assert os.path.isabs(value), f'init 后 default_save_path 仍是相对值: {value!r}'
    assert value == str((tmp_path / 'downloads').resolve())


# --- GET /api/fs/browse ---------------------------------------------------------

def test_browse_defaults_to_downloads_root(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    root = tmp_path / 'downloads'
    (root / 'map').mkdir(parents=True)
    (root / 'dem').mkdir()
    (root / 'afile.txt').write_text('x')          # 文件不该出现在目录列表里
    (root / '.hidden').mkdir()                    # 隐藏目录不列
    resp = client.get('/api/fs/browse')
    data = resp.get_json()
    assert resp.status_code == 200
    assert data['success'] is True
    assert data['path'] == str(root.resolve())
    assert data['parent'] is None, '根目录没有「上一级」可去'
    names = [d['name'] for d in data['dirs']]
    assert names == ['dem', 'map'], f'只列非隐藏子目录: {names}'


def test_browse_subdirectory_and_parent(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    root = tmp_path / 'downloads'
    (root / 'map' / 'sub').mkdir(parents=True)
    resp = client.get('/api/fs/browse', query_string={'path': str(root / 'map')})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data['path'] == str((root / 'map').resolve())
    assert data['parent'] == str(root.resolve())
    assert [d['name'] for d in data['dirs']] == ['sub']


def test_browse_rejects_escape_outside_root(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    (tmp_path / 'downloads').mkdir(exist_ok=True)
    resp = client.get('/api/fs/browse', query_string={'path': str(tmp_path)})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_browse_rejects_nonexistent_and_file(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    root = tmp_path / 'downloads'
    root.mkdir(exist_ok=True)
    (root / 'f.txt').write_text('x')
    assert client.get('/api/fs/browse',
                      query_string={'path': str(root / 'nope')}).status_code == 400
    assert client.get('/api/fs/browse',
                      query_string={'path': str(root / 'f.txt')}).status_code == 400


# --- 前端接线 -------------------------------------------------------------------

def test_task_form_has_browse_button_and_absolute_default(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    html = client.get('/').get_data(as_text=True)
    assert 'id="outputPathBrowse"' in html, '任务表单保存路径旁缺少「浏览」按钮'
    assert 'path_browser.js' in html, '首页必须加载目录浏览组件'
    assert 'pathBrowserModal' in html, '首页必须含目录选择弹窗'


def test_config_page_has_browse_button(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    for path in ('/', '/config'):
        html = client.get(path).get_data(as_text=True)
        assert 'id="defaultSavePathBrowse"' in html, f'{path} 默认保存路径旁缺少「浏览」按钮'


def test_path_browser_js_wiring():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', 'path_browser.js'), encoding='utf-8') as f:
        src = f.read()
    assert '/api/fs/browse' in src
    assert 'openPathBrowser' in src


def test_map_js_form_default_is_absolute():
    """任务表单的默认保存路径必须来自(已归一成绝对值的)default_save_path,
    不再硬编码 './downloads/map' 相对值。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', 'map.js'), encoding='utf-8') as f:
        src = f.read()
    assert "'./downloads/map'" not in src and "'./downloads/dem'" not in src, (
        'map.js 仍硬编码相对保存路径默认值'
    )
    assert 'default_save_path' in src
