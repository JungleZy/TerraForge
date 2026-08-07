"""底图源（basemap_source）的预设解析与校验。

底图与下载源（tile_servers）是两个独立配置 —— 底图走浏览器直连、不吃
proxy_url，下载走 Python、吃。这些断言守的就是「两者不再共用一份地址」
以及各预设的坐标系/署名/层级上限没被悄悄改坏。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.services.basemap_source import (  # noqa: E402
    BASEMAP_PRESETS,
    DEFAULT_BASEMAP_SOURCE,
    DOWNLOAD_SOURCE,
    resolve_basemap,
    validate_basemap_source,
)
from src.services.config_manager import ConfigManager  # noqa: E402


# --- 预设表本身 ---------------------------------------------------------------

def test_default_is_esri_satellite():
    """默认必须是 Esri 卫星影像。

    不是审美选择：Google 在国内直连不通，而底图**没有代理可用**（浏览器
    直连，不读项目里的 proxy_url）。默认给 Google 等于开箱即蓝球。
    """
    assert DEFAULT_BASEMAP_SOURCE == 'esri'
    bm = resolve_basemap(None)
    assert 'server.arcgisonline.com' in bm['url']
    assert 'World_Imagery' in bm['url']


def test_every_preset_has_placeholders_level_and_credit():
    for name, preset in BASEMAP_PRESETS.items():
        url = preset['url']
        for ph in ('{z}', '{x}', '{y}'):
            assert ph in url, f'{name} 的模板缺少 {ph}'
        # 不设层级上限时放大过头是一片黑，看不出是缩放过头还是底图挂了。
        assert isinstance(preset['max_level'], int) and preset['max_level'] > 0
        # Esri 的影像署名是使用条款要求的，不是可选装饰。
        assert preset['credit'].strip(), f'{name} 缺少署名'


def test_esri_uses_zyx_order():
    """Esri 的 REST 瓦片是 /tile/{z}/{y}/{x} —— 行在列前。

    写成 {z}/{x}/{y} 不会报错，只会安静地给出错误位置的影像：
    赤道附近看不出来，高纬度就完全跑偏。
    """
    assert BASEMAP_PRESETS['esri']['url'].endswith('/tile/{z}/{y}/{x}')


def test_no_gcj02_source_is_preset():
    """预设里不许出现 GCJ-02 偏移坐标系的源（高德/腾讯）。

    本工具下载的 Google 影像是 WGS-84；底图若是 GCJ-02，在中国境内框选会
    偏移 100-700 米 —— 底图上框住的山谷，下载下来是隔壁那个。
    """
    banned = ('autonavi.com', 'amap.com', 'qq.com', 'gtimg.com', 'tencent')
    for name, preset in BASEMAP_PRESETS.items():
        low = preset['url'].lower()
        for host in banned:
            assert host not in low, f'{name} 指向了 GCJ-02 源 {host}'


# --- 解析 ---------------------------------------------------------------------

@pytest.mark.parametrize('name', sorted(BASEMAP_PRESETS))
def test_preset_resolves_to_itself(name):
    bm = resolve_basemap(name)
    assert bm['source'] == name
    assert bm['url'] == BASEMAP_PRESETS[name]['url']


def test_google_presets_are_protocol_relative():
    """页面走 https 时硬编码 http:// 会被混合内容策略拦掉，底图直接不加载。"""
    for name in ('google_satellite', 'google_roadmap'):
        assert BASEMAP_PRESETS[name]['url'].startswith('//')


def test_google_satellite_uses_lyrs_s():
    """卫星是 lyrs=s，路网是 lyrs=m。改造前底图写死 lyrs=m，选了卫星也是路网。"""
    assert 'lyrs=s' in BASEMAP_PRESETS['google_satellite']['url']
    assert 'lyrs=m' in BASEMAP_PRESETS['google_roadmap']['url']


def test_download_source_follows_tile_servers_and_style():
    """跟随下载源：取列表第一条 + 当前默认样式（不是写死 m）。"""
    bm = resolve_basemap(DOWNLOAD_SOURCE, tile_servers='mts2,mts3', default_style='s')
    assert bm['source'] == DOWNLOAD_SOURCE
    assert 'mts2.googleapis.com' in bm['url']
    assert 'lyrs=s' in bm['url']


def test_download_source_with_empty_list_falls_back():
    bm = resolve_basemap(DOWNLOAD_SOURCE, tile_servers='')
    assert 'mts0.googleapis.com' in bm['url']


def test_custom_template_passes_through_untouched():
    url = 'https://example.com/t/{z}/{x}/{y}.png'
    bm = resolve_basemap(url)
    assert bm['url'] == url
    # 自定义源不知道对方支持到几级，交给服务器去 404，不替用户猜。
    assert bm['max_level'] is None


def test_unknown_value_falls_back_instead_of_raising():
    """坏值不能让首页 500 —— 解析跑在渲染途中，校验拦在写入侧。"""
    bm = resolve_basemap('this-is-not-a-source')
    assert bm['source'] == DEFAULT_BASEMAP_SOURCE
    assert 'arcgisonline' in bm['url']


# --- 校验（写入侧） -----------------------------------------------------------

@pytest.mark.parametrize('value', list(BASEMAP_PRESETS) + [DOWNLOAD_SOURCE,
                                                           'https://a.b/{z}/{x}/{y}.png'])
def test_valid_values_accepted(value):
    ok, err = validate_basemap_source(value)
    assert ok, err


@pytest.mark.parametrize('value,reason', [
    ('', '空值'),
    ('   ', '全空白'),
    ('nope', '未知预设名'),
    ('ftp://a.b/{z}/{x}/{y}.png', '非 http(s) 协议'),
    ('https://a.b/{z}/{x}.png', '缺 {y} 占位符'),
    ('https://a.b/{z}/{x}/{y}/{q}.png', '不支持的占位符'),
])
def test_invalid_values_rejected(value, reason):
    ok, err = validate_basemap_source(value)
    assert not ok, f'应当拒绝（{reason}）：{value!r}'
    assert err


def test_config_manager_routes_basemap_key_to_validator():
    """ConfigManager 必须接上这个校验 —— 否则坏值能写进库。"""
    cm = ConfigManager()
    assert cm.validate_config('basemap_source', 'esri') is True
    assert cm.validate_config('basemap_source', 'https://a.b/{z}/{x}/{y}.png') is True
    assert cm.validate_config('basemap_source', 'nope') is False
    assert cm.validate_config('basemap_source', '') is False
