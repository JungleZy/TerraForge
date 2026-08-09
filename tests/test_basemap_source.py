"""底图源（basemap_source）的预设解析与校验。

底图与下载源（tile_servers）是两个独立配置。底图瓦片由后端转发
（routes/basemap_static.py），所以前端只拿到同源路径、真实上游不出服务端 ——
这些断言守的就是这条边界，以及各预设的坐标系/署名/层级上限没被悄悄改坏。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.services.basemap_source import (  # noqa: E402
    BASEMAP_PRESETS,
    BASEMAP_TILE_PATH,
    DEFAULT_BASEMAP_SOURCE,
    DOWNLOAD_SOURCE,
    client_descriptor,
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
    assert 'server.arcgisonline.com' in bm['upstream']
    assert 'World_Imagery' in bm['upstream']


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
    assert bm['upstream'] == BASEMAP_PRESETS[name]['url']


def test_upstream_urls_carry_an_explicit_scheme():
    """上游地址只在服务端用，必须带 scheme。

    改造前这里是协议相对（//host/...），那是为了让浏览器直连时不触发混合
    内容拦截。现在瓦片走后端转发，浏览器根本看不到上游地址，而 urllib 是
    打不开 `//host/...` 的 —— 留着协议相对会让每一块瓦片 502。
    """
    for name, preset in BASEMAP_PRESETS.items():
        assert preset['url'].startswith(('http://', 'https://')), (
            f'{name} 的上游地址缺少 scheme，服务端 urllib 打不开'
        )


def test_google_satellite_uses_lyrs_s():
    """卫星是 lyrs=s，路网是 lyrs=m。改造前底图写死 lyrs=m，选了卫星也是路网。"""
    assert 'lyrs=s' in BASEMAP_PRESETS['google_satellite']['url']
    assert 'lyrs=m' in BASEMAP_PRESETS['google_roadmap']['url']


def test_download_source_follows_tile_servers_and_style():
    """跟随下载源：取列表第一条 + 当前默认样式（不是写死 m）。"""
    bm = resolve_basemap(DOWNLOAD_SOURCE, tile_servers='mts2,mts3', default_style='s')
    assert bm['source'] == DOWNLOAD_SOURCE
    assert 'mts2.googleapis.com' in bm['upstream']
    assert 'lyrs=s' in bm['upstream']


def test_download_source_with_empty_list_falls_back():
    bm = resolve_basemap(DOWNLOAD_SOURCE, tile_servers='')
    assert 'mts0.googleapis.com' in bm['upstream']


def test_download_source_does_not_invent_a_credit_or_a_max_level():
    """跟随下载源时 tile_servers 可以是任何一条 XYZ 模板（自建镜像是文档里
    的一等用法），此时「最高 21 级、署名 © Google」是编造出来的事实：界面会
    按一个不存在的层级上限建图层，并挂上一家与这张图无关的署名。同一函数的
    自定义模板分支对同一个未知量报 None/''，两支必须同口径。
    """
    bm = resolve_basemap(DOWNLOAD_SOURCE,
                         tile_servers='https://mirror.example.com/t/{z}/{x}/{y}.png')
    assert bm['max_level'] is None, '不知道镜像支持到几级就不许报一个数'
    assert bm['credit'] == '', '不知道是谁的图就不许署名'


def test_custom_template_passes_through_untouched():
    url = 'https://example.com/t/{z}/{x}/{y}.png'
    bm = resolve_basemap(url)
    assert bm['upstream'] == url
    # 自定义源不知道对方支持到几级，交给服务器去 404，不替用户猜。
    assert bm['max_level'] is None


# --- 下发给浏览器的描述 -------------------------------------------------------

@pytest.mark.parametrize('value', list(BASEMAP_PRESETS) + [
    DOWNLOAD_SOURCE, 'https://example.com/secret/{z}/{x}/{y}.png', None,
])
def test_client_descriptor_never_leaks_the_upstream_url(value):
    """前端只能拿到同源路径。

    这不是保密，是架构约束：前端一旦拿到上游地址，早晚有人图省事直连回去，
    CORS（上游 4xx 时真实状态码被埋掉）和「底图不吃 proxy_url」这两个坑
    立刻复活 —— 那正是这次故障的两个成因。
    """
    resolved = resolve_basemap(value, tile_servers='mts0')
    desc = client_descriptor(resolved)
    assert desc['url'].startswith(BASEMAP_TILE_PATH + '?v='), desc['url']
    assert 'upstream' not in desc
    for host in ('arcgisonline', 'googleapis', 'example.com'):
        assert host not in repr(desc), f'客户端描述里泄露了上游地址：{host}'


def test_client_descriptor_keeps_level_and_credit():
    desc = client_descriptor(resolve_basemap('esri'))
    assert desc['max_level'] == 19
    assert 'Esri' in desc['credit']
    assert desc['source'] == 'esri'


@pytest.mark.parametrize('a,b', [
    ('esri', 'google_satellite'),
    ('https://a.example.com/t/{z}/{x}/{y}.png', 'https://b.example.com/t/{z}/{x}/{y}.png'),
])
def test_switching_the_source_switches_the_url_the_browser_is_given(a, b):
    """换源必须换 URL 空间。

    瓦片带 24 小时浏览器缓存，同源路径里一旦没有源标识，用户在配置页换完源
    之后，已经浏览过的区域会继续显示旧那家的影像整整一天 —— 缓存命中不回源，
    界面上没有任何补救手段，表现成「这个设置项坏了」。

    第二组参数钉的是「按上游算而不是按 source 名算」：两条自定义模板的
    source 都是 'custom'，只认名字的话它们共用同一条 URL。
    """
    ua = client_descriptor(resolve_basemap(a))['url']
    ub = client_descriptor(resolve_basemap(b))['url']
    assert ua != ub, f'{a} 与 {b} 下发了同一条 URL：{ua}'
    for url in (ua, ub):
        # 同源、且 {z}/{x}/{y} 仍留给 Cesium 代入 —— 版本串不能破坏这两条。
        assert url.startswith(BASEMAP_TILE_PATH + '?v=')


def test_the_url_is_stable_for_the_same_source():
    """同一个源两次解析必须给出同一条 URL：版本串一抖动，浏览器缓存全作废，
    每次刷新都要把整屏瓦片重下一遍。"""
    assert (client_descriptor(resolve_basemap('esri'))['url']
            == client_descriptor(resolve_basemap('esri'))['url'])


def test_unknown_value_falls_back_instead_of_raising():
    """坏值不能让首页 500 —— 解析跑在渲染途中，校验拦在写入侧。"""
    bm = resolve_basemap('this-is-not-a-source')
    assert bm['source'] == DEFAULT_BASEMAP_SOURCE
    assert 'arcgisonline' in bm['upstream']


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
