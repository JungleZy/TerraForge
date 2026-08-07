"""地图底图源（basemap_source 配置）的预设表、解析与校验。

底图和下载源是**两件事**，这个模块存在的理由就是把它们分开：

  - 下载源（tile_servers）决定你**下载到**什么瓦片，走 Python + 代理；
  - 底图决定你在框选时**看到**什么，走浏览器直连，不吃项目里的 proxy_url。

两者网络路径不同，可达性也就可能不同：给下载配好代理，底图照样可能是
一个蓝色球体。改造前底图写死取 tile_servers 的第一条并强制 lyrs=m，
在连不上 Google 的网络里就是这个下场，而且它显示的是路网图不是卫星图。

坐标系是选预设时的硬约束：本工具下载的 Google 影像是 WGS-84/EPSG:3857，
底图必须同属这个坐标系，框选的 bbox 才和下载到的瓦片对得上。
**不要加高德/腾讯的卫星预设** —— 它们按 GCJ-02 切片，在中国境内会偏移
100-700 米，底图上框住的山谷，下载下来是隔壁那个。

取值形态三种（与 tile_servers 的条目语义保持同一路数）：
  1. 预设别名     —— 'esri' / 'google_satellite' / 'google_roadmap'
  2. 'download_source' —— 跟随 tile_servers 第一条 + default_style，
                          即改造前的行为（所见即所得）
  3. 完整 XYZ 模板 —— 'https://.../{z}/{x}/{y}.png'，校验复用
                      tile_url_probe.validate_server_entry
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from src.i18n import t
from src.services.tile_url_probe import (
    expand_server_entry,
    parse_server_list,
    validate_server_entry,
)

# 跟随下载源的哨兵值。
DOWNLOAD_SOURCE = 'download_source'

# 配置缺省。选 Esri 而不是 Google：Google 在国内直连不通（底图不走代理），
# 而 Esri World Imagery 直连可达，且同为 WGS-84，与下载的 Google 影像对齐。
DEFAULT_BASEMAP_SOURCE = 'esri'

# max_level：超出这一层瓦片服务器返回 404，Cesium 会画成空白。不设上限的话
# 用户一放大就是一片黑，且看不出是缩放过头还是底图挂了。
# credit：Esri 的影像署名是使用条款要求的，不是可选装饰。
BASEMAP_PRESETS: Dict[str, Dict[str, Any]] = {
    'esri': {
        'url': 'https://server.arcgisonline.com/ArcGIS/rest/services'
               '/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        'max_level': 19,
        'credit': 'Esri, Maxar, Earthstar Geographics',
    },
    'google_satellite': {
        # 协议相对：页面走 https 时硬编码 http:// 会被混合内容拦截。
        'url': '//mts0.googleapis.com/vt?lyrs=s&x={x}&y={y}&z={z}',
        'max_level': 21,
        'credit': '© Google',
    },
    'google_roadmap': {
        'url': '//mts0.googleapis.com/vt?lyrs=m&x={x}&y={y}&z={z}',
        'max_level': 21,
        'credit': '© Google',
    },
}


def resolve_basemap(value: Optional[str], *, tile_servers: Optional[str] = None,
                    default_style: str = 'm') -> Dict[str, Any]:
    """把 basemap_source 的配置值解析成前端直接可用的图层描述。

    返回 {'url', 'max_level', 'credit', 'source'}。max_level 为 None 表示
    不限制（自定义模板：我们不知道对方支持到几级，交给服务器去 404）。

    空值/未知值回落到默认预设而不是抛错 —— 这条路径跑在渲染首页的途中，
    一个坏掉的配置值不该让整个页面 500；校验拦在写入侧（见下面的
    validate_basemap_source 与 ConfigManager.validate_config）。
    """
    raw = (value or '').strip()

    if raw.startswith(('http://', 'https://')):
        return {'url': raw, 'max_level': None, 'credit': '', 'source': raw}

    if raw == DOWNLOAD_SOURCE:
        first = parse_server_list(tile_servers)[0]
        return {
            'url': expand_server_entry(first, default_style or 'm'),
            'max_level': 21,
            'credit': '© Google',
            'source': DOWNLOAD_SOURCE,
        }

    preset = BASEMAP_PRESETS.get(raw) or BASEMAP_PRESETS[DEFAULT_BASEMAP_SOURCE]
    name = raw if raw in BASEMAP_PRESETS else DEFAULT_BASEMAP_SOURCE
    return dict(preset, source=name)


def validate_basemap_source(value: Optional[str]) -> Tuple[bool, Optional[str]]:
    """校验 basemap_source 的写入值。返回 (ok, error_message)。"""
    raw = (value or '').strip()
    if not raw:
        return False, t('val.basemap.empty')
    if raw in BASEMAP_PRESETS or raw == DOWNLOAD_SOURCE:
        return True, None
    if raw.startswith(('http://', 'https://')):
        return validate_server_entry(raw)
    return False, t('val.basemap.unknown', value=raw)
