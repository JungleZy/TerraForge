"""2026-08-08 评审「安全姿态」第 1 项：凭据与上游地址不得进页面级 JS 全局。

原状：`templates/index.html` 是 `const config = {{ config|tojson }};`，把
`config_manager.get_all()` 的全部 45 个键灌进一个页面级全局 —— 里面有
`earthdata_password`、`proxy_url`（含 `user:pass@`）和 `tile_servers`（上游地址）。
最后一项正好绕开 `basemap_source.client_descriptor` 特意剥掉 `upstream` 的那道门，
它的 docstring 写着「前端一旦拿到上游地址就会有人图省事直连回去」。
而 `map.js` 实际只读 6 个键。

修法两部分：
- JS 全局收敛到 `routes/main.py:MAP_CONFIG_KEYS` 白名单；
- 密码不再回填真值，改回填哨兵 `SECRET_UNCHANGED`；`PUT /api/config` 收到哨兵跳过该键。

**未闭合的部分（有意）**：配置**表单**仍然把 `proxy_url` 与 `tile_servers` 渲进
可编辑输入框 —— 那是用户必须看得见才能改的值。要把它们也拿掉，得先有鉴权
（`docs/reviews/2026-08-08-full-project-review.md` 的 S1），不是这一条的范围。
"""
import json
import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.routes.main import MAP_CONFIG_KEYS  # noqa: E402
from src.services.config_manager import SECRET_UNCHANGED  # noqa: E402


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding='utf-8') as f:
        return f.read()


@pytest.fixture
def client(isolated_app):
    return isolated_app.app.test_client()


def _js_global(html):
    """抓出 `const config = {...};` 里的对象。"""
    m = re.search(r'const config = (\{.*?\});', html, re.S)
    assert m, '首页没有 `const config = {...};`'
    return json.loads(m.group(1))


# --------------------------------------------------------------------------
# 白名单
# --------------------------------------------------------------------------

def test_index_js_global_carries_only_the_whitelist(client):
    cfg = _js_global(client.get('/').get_data(as_text=True))
    assert set(cfg) == set(MAP_CONFIG_KEYS), (
        'JS 全局的键集合与 MAP_CONFIG_KEYS 不一致')


@pytest.mark.parametrize('key', ['earthdata_password', 'earthdata_username',
                                 'proxy_url', 'tile_servers', 'basemap_source'])
def test_secrets_and_upstreams_are_absent_from_the_js_global(client, key):
    assert key not in _js_global(client.get('/').get_data(as_text=True))


def test_whitelist_covers_every_key_map_js_actually_reads():
    """棘轮：map.js 新读一个键就必须同步扩白名单，否则那个键静默变 undefined。

    反向也钉住：白名单里塞了 map.js 不读的键，就是又在往页面里多送东西。
    """
    code = '\n'.join(re.sub(r'//.*$', '', ln)
                     for ln in _read(os.path.join('static', 'js', 'map.js')).splitlines())
    # 排除 `app.config.x` 这类成员访问，只取自由变量 config 的读取
    read = set(re.findall(r'(?<![\w.])config\.([A-Za-z_]\w*)', code))
    assert read == set(MAP_CONFIG_KEYS), (
        f'map.js 读的键={sorted(read)}，白名单={sorted(MAP_CONFIG_KEYS)}')


def test_index_template_does_not_dump_the_whole_config():
    """模板里不能再出现全量下发；注释里也不行 —— 它照样会被 Jinja 求值。"""
    html = _read(os.path.join('templates', 'index.html'))
    assert 'map_config|tojson' in html
    assert not re.search(r'\{\{\s*config\s*\|\s*tojson', html), (
        '又出现了 `{{ config|tojson }}` —— 45 个键连密码一起进页面')


# --------------------------------------------------------------------------
# 密码哨兵
# --------------------------------------------------------------------------

def _set_password(isolated_app, value):
    from src.services.config_manager import ConfigManager
    ConfigManager().set('earthdata_password', value)


def test_password_never_reaches_the_page(isolated_app, client):
    _set_password(isolated_app, 'S3cr3t-NASA-pw')
    html = client.get('/').get_data(as_text=True)
    assert 'S3cr3t-NASA-pw' not in html, '密码被渲进了页面'
    assert SECRET_UNCHANGED in html, '密码框应回填哨兵，让用户知道已设置过'


def test_password_never_reaches_the_config_endpoint(isolated_app, client):
    _set_password(isolated_app, 'S3cr3t-NASA-pw')
    got = client.get('/api/config').get_json()['config']['earthdata_password']['value']
    assert got == SECRET_UNCHANGED


def test_unset_password_is_reported_as_empty_not_as_the_sentinel(isolated_app, client):
    _set_password(isolated_app, '')
    got = client.get('/api/config').get_json()['config']['earthdata_password']['value']
    assert got == '', '没设过密码时回哨兵会让 UI 显示成「已设置」'


def test_echoing_the_sentinel_back_does_not_overwrite_the_password(isolated_app, client):
    """前端每次保存都提交**全部**键，所以哨兵一定会被原样回传。"""
    from src.services.config_manager import ConfigManager

    _set_password(isolated_app, 'S3cr3t-NASA-pw')
    resp = client.put('/api/config', json={'earthdata_password': SECRET_UNCHANGED,
                                           'concurrent_downloads': '7'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert 'earthdata_password' not in body['updated'], '哨兵不该算作一次更新'

    mgr = ConfigManager()
    assert mgr.get('earthdata_password') == 'S3cr3t-NASA-pw', (
        '哨兵被当成新值写库了 —— 下一次 Earthdata 登录会 401')
    assert mgr.get('concurrent_downloads') == '7', '同批的其他键必须照常生效'


def test_a_real_new_password_is_saved(isolated_app, client):
    from src.services.config_manager import ConfigManager

    _set_password(isolated_app, 'old-pw')
    client.put('/api/config', json={'earthdata_password': 'brand-new-pw'})
    assert ConfigManager().get('earthdata_password') == 'brand-new-pw'


def test_clearing_the_password_still_works(isolated_app, client):
    """空串不等于哨兵 —— 清空必须真的落库，否则密码删不掉。"""
    from src.services.config_manager import ConfigManager

    _set_password(isolated_app, 'old-pw')
    client.put('/api/config', json={'earthdata_password': ''})
    assert ConfigManager().get('earthdata_password') == ''


def test_sentinel_only_applies_to_secret_keys(isolated_app, client):
    """哨兵字面量出现在普通键上时是**普通值**，不能被当成「跳过」。"""
    from src.services.config_manager import ConfigManager

    client.put('/api/config', json={'default_save_path': SECRET_UNCHANGED})
    # default_save_path 要求绝对路径，所以这里应当被校验拒绝而不是被静默跳过
    assert ConfigManager().get('default_save_path') != SECRET_UNCHANGED
