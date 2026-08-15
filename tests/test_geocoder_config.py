"""地名搜索的服务地址必须能在界面上填进去。

真实故障（用户报告）：地图上的地名搜索框显示「地名搜索未启用：请在配置页填写
geocoder_url（地理编码服务地址）」，而**配置页里根本没有这个输入框** ——
0.3.3 把配置键（`DEFAULT_CONFIGS`）、校验（`_validate_geocoder_url`）、
服务端读取（`geocoding.geocoder_configured`）、API（`/api/places/search`）和
前端提示都做了，唯独漏掉最后一步：模板里的 `<input>` 和 `saveConfig` 的提交
字段。用户被一句提示指到一个死胡同，只能改数据库或直接调 API 才能启用。

出厂为空是**产品决定**（不内置任何地名服务，理由见 services/geocoding 的模块
docstring），这条不变。「默认关闭」和「压根没法打开」是两回事，本文件钉的是
后者：只要还有一句话叫用户去配置页填它，配置页就必须真的能填。
"""
import importlib
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core.config import Config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_KEY = 'geocoder_url'


@pytest.fixture
def client(monkeypatch, tmp_path):
    # 同 test_config_form_submittable：数据目录指到 tmp_path，否则 app_factory
    # 的单实例锁会撞上开发机上正在跑的实例。
    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    for mod in ('app', 'src.core.database'):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module('app')
    app_mod.app.config['TESTING'] = True
    return app_mod.app.test_client()


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


@pytest.mark.parametrize('path', ['/', '/config'])
def test_config_page_renders_the_geocoder_field(client, path):
    """两个入口（首页配置面板 / 独立 /config 页）都必须渲染出这个输入框。

    只查其中一个是不够的：配置内容是 `_config_content.html` 一份 include 进两处，
    但历史上出现过某一页把整块 block 覆盖掉的情况（config.html 就把
    vendor_task_list_js 覆盖成空）。
    """
    html = client.get(path).get_data(as_text=True)
    assert f'id="{CONFIG_KEY}"' in html, (
        f'{path} 的配置页没有 geocoder_url 输入框 —— '
        '而地图搜索面板正叫用户「去配置页填 geocoder_url」')


def test_save_config_submits_the_geocoder_field():
    """`saveConfig` 的 payload 必须带上它。

    与上一条是**两件事**，缺任何一件功能都不通：模板渲染了但不提交，用户填完
    点保存，值静默丢弃、搜索框依旧是灰的，而界面没有任何异常表现 —— 这是本类
    缺陷里最难自查的形态。
    """
    js = _read('static', 'js', 'config.js')
    payload_start = js.index('const configData = {')
    payload = js[payload_start:js.index('};', payload_start)]
    assert re.search(rf'\b{CONFIG_KEY}\s*:', payload), (
        'saveConfig 的 configData 里没有 geocoder_url —— 输入框填了也存不进去')


def test_the_hint_that_points_users_at_the_config_page_is_not_a_dead_end():
    """指路文案与落点必须同时存在 —— 这条就是本次缺陷的正面防线。

    判据故意是**双向**的：文案里点名了 `geocoder_url`，配置页就必须有那个 id。
    删掉输入框会红；把文案改成不点名配置页也不算过（那只是把死胡同藏起来，
    用户依然不知道去哪填）。
    """
    hint = _read('src', 'i18n', 'catalog', 'js_region.py')
    assert CONFIG_KEY in hint, (
        'js.search.disabled_hint 不再点名 geocoder_url —— 本测试的前提变了，'
        '请确认用户仍有办法知道去哪里启用它')
    tpl = _read('templates', '_config_content.html')
    assert f'id="{CONFIG_KEY}"' in tpl, (
        '提示叫用户「去配置页填 geocoder_url」，而配置页模板里没有这个 id')


def test_geocoder_url_round_trips_through_the_config_page(client):
    """存进去 → 页面上读得回来 → 功能真的亮了。

    三段一起断言才有意义：只验 PUT 200 会漏掉「存了但页面不回显」（用户下次
    打开看到空白，以为没存上）；只验回显会漏掉「存进去但服务端仍判未配置」。
    """
    url = 'https://nominatim.example.org/search?format=json&q={q}'
    resp = client.put('/api/config', json={CONFIG_KEY: url})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    html = client.get('/config').get_data(as_text=True)
    field = re.search(rf'<input[^>]*id="{CONFIG_KEY}"[^>]*>', html)
    assert field, '存完之后配置页没有这个输入框'
    assert 'nominatim.example.org' in field.group(0), (
        f'输入框没有回显已保存的值：{field.group(0)}')

    # 服务端侧：从「出厂关闭」翻成「已启用」
    from src.services import geocoding
    assert geocoding.geocoder_configured() is True
    assert client.get('/api/places/search?q=&limit=1').get_json()['enabled'] is True


def test_a_url_without_the_q_placeholder_is_rejected(client):
    """缺 `{q}` 必须在保存时就拒掉。

    这是 `_validate_geocoder_url` 已有的硬约束，这里钉的是它**经由配置页这条
    路**仍然生效：没有占位符的地址每次都返回同一批结果，而用户会以为是
    「搜不到」，排查方向完全错。
    """
    resp = client.put('/api/config', json={CONFIG_KEY: 'https://nominatim.example.org/search'})
    assert resp.status_code == 400, '缺 {q} 的地址被接受了'

    from src.services import geocoding
    assert geocoding.geocoder_configured() is False, '被拒的值不该落库'


def test_empty_value_keeps_the_feature_off(client):
    """留空仍然是合法输入，且等于关闭 —— 出厂状态必须能被显式恢复。

    没有这条，「填错了想清空」会撞上校验（空串若被当成非法就再也清不掉），
    用户只能去改数据库。
    """
    from src.services import geocoding
    assert client.put('/api/config', json={
        CONFIG_KEY: 'https://nominatim.example.org/search?q={q}'}).status_code == 200
    assert geocoding.geocoder_configured() is True

    assert client.put('/api/config', json={CONFIG_KEY: ''}).status_code == 200
    assert geocoding.geocoder_configured() is False
    assert client.get('/api/places/search?q=x').get_json()['enabled'] is False
