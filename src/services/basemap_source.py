"""地图底图源（basemap_source 配置）的预设表、解析与校验。

底图和下载源是**两件事**，这个模块存在的理由就是把它们分开：

  - 下载源（tile_servers）决定你**下载到**什么瓦片；
  - 底图决定你在框选时**看到**什么。

改造前底图写死取 tile_servers 的第一条并强制 lyrs=m —— 显示的是路网图不是
卫星图，而且在连不上 Google 的网络里就是一个蓝色球体。

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
    is_link_local_url,
    parse_server_list,
    validate_server_entry,
)

# 跟随下载源的哨兵值。
DOWNLOAD_SOURCE = 'download_source'

# 自定义模板的 source 标签。**不能**把模板原文当 source —— 它会随
# client_descriptor 下发到浏览器，等于把上游地址又漏回前端（见该函数的说明）。
CUSTOM_SOURCE = 'custom'

# 配置缺省。选 Esri 而不是 Google：Google 在国内直连不通，而 Esri World
# Imagery 可达，且同为 WGS-84，与下载的 Google 影像对齐。
DEFAULT_BASEMAP_SOURCE = 'esri'

# 前端拿到的永远是这条**同源**路径，真实上游地址只存在于服务端。
# 理由见 src/routes/basemap_static.py：浏览器直连上游会撞 CORS（上游返回 4xx
# 时错误页没有 CORS 头，真实状态码被埋成一句 CORS 报错），而且浏览器不吃项目
# 的 proxy_url —— 底图与下载走两条不同的出网路径，配好代理底图照样可能是蓝球。
BASEMAP_TILE_PATH = '/basemap/{z}/{x}/{y}'

# url：**上游**地址模板，只在服务端使用，不下发给浏览器。
# max_level：超出这一层瓦片服务器返回 404，Cesium 会画成空白。不设上限的话
#   用户一放大就是一片黑，且看不出是缩放过头还是底图挂了。
# credit：Esri 的影像署名是使用条款要求的，不是可选装饰。
# wgs84：这张图的瓦片格网是不是 WGS-84。**决定它能不能进自动回退链**，
#   见下面 AUTO_FALLBACK_ORDER 的说明。
BASEMAP_PRESETS: Dict[str, Dict[str, Any]] = {
    'esri': {
        'url': 'https://server.arcgisonline.com/ArcGIS/rest/services'
               '/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        'max_level': 19,
        'credit': 'Esri, Maxar, Earthstar Geographics',
        'wgs84': True,
    },
    'google_satellite': {
        'url': 'https://mts0.googleapis.com/vt?lyrs=s&x={x}&y={y}&z={z}',
        'max_level': 21,
        'credit': '© Google',
        'wgs84': True,
    },
    'google_roadmap': {
        'url': 'https://mts0.googleapis.com/vt?lyrs=m&x={x}&y={y}&z={z}',
        'max_level': 21,
        'credit': '© Google',
        # lyrs=m 的中国区路网按 GCJ-02 绘制，与 WGS-84 的框选矩形错位数百米。
        # 用户在配置页显式选它是他自己的决定；自动回退**不许**走到这里。
        'wgs84': False,
    },
    'osm': {
        'url': 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
        'max_level': 19,
        'credit': '© OpenStreetMap contributors',
        'wgs84': True,
    },
}

# 自动回退链：配置的源取不到瓦片时，按这个顺序往下试（见
# routes/basemap_static.py）。三条硬约束决定了这张表的内容：
#
# 1. **只放 WGS-84 的源。** 底图是用来框选下载范围的，静默换上一张 GCJ-02 的
#    图等于让用户框错地方（中国境内偏移 100-700 米），而且没有任何提示。
#    google_roadmap 因此不在链里。
# 2. **同主机的源不算冗余。** google_roadmap 与 google_satellite 同为
#    mts0.googleapis.com：卫星取不到时它也取不到，放进来是拿坐标系风险换零可用性。
# 3. **链尾要换一条网络路径。** 实测过一种真实故障：Esri 的 CDN(Akamai) 封了
#    代理出口 IP（403），而 Google 只有走代理才通 —— 两张卫星图分属两条路径，
#    互为备份。osm 是第三条路径上的路网图（矢量风格、WGS-84），两张卫星图
#    都不通时它还有机会出图，总比一颗蓝球强。
AUTO_FALLBACK_ORDER = ('esri', 'google_satellite', 'osm')


def resolve_basemap(value: Optional[str], *, tile_servers: Optional[str] = None,
                    default_style: str = 'm') -> Dict[str, Any]:
    """把 basemap_source 的配置值解析成**服务端**用的图层描述。

    返回 {'upstream', 'max_level', 'credit', 'source'}。upstream 是真实的上游
    地址模板，只给 routes/basemap_static.py 取瓦片用；下发给浏览器的描述由
    client_descriptor() 生成，那里的地址永远是同源的 BASEMAP_TILE_PATH。

    max_level 为 None 表示不限制（自定义模板：我们不知道对方支持到几级，
    交给服务器去 404）。

    空值/未知值回落到默认预设而不是抛错 —— 这条路径跑在渲染首页和取每一块
    底图瓦片的途中，一个坏掉的配置值不该让页面 500；校验拦在写入侧（见下面的
    validate_basemap_source 与 ConfigManager.validate_config）。
    """
    raw = (value or '').strip()

    if raw.startswith(('http://', 'https://')):
        return {'upstream': raw, 'max_level': None, 'credit': '',
                'source': CUSTOM_SOURCE}

    if raw == DOWNLOAD_SOURCE:
        first = parse_server_list(tile_servers)[0]
        return {
            'upstream': expand_server_entry(first, default_style or 'm'),
            'max_level': 21,
            'credit': '© Google',
            'source': DOWNLOAD_SOURCE,
        }

    name = raw if raw in BASEMAP_PRESETS else DEFAULT_BASEMAP_SOURCE
    preset = BASEMAP_PRESETS[name]
    return {
        'upstream': preset['url'],
        'max_level': preset['max_level'],
        'credit': preset['credit'],
        'source': name,
    }


def fallback_candidates(resolved: Dict[str, Any]) -> list:
    """配置的源 + 自动回退候选，按尝试顺序排列（第一个永远是用户配置的那个）。

    候选只从 AUTO_FALLBACK_ORDER 里取，且跳过与配置源同名的那条。用户显式
    选的源哪怕不是 WGS-84（google_roadmap / 自定义模板）也照样排第一 —— 那是
    他自己的决定；自动**追加**的候选则一律是 WGS-84，理由见 AUTO_FALLBACK_ORDER。
    """
    chain = [resolved]
    for name in AUTO_FALLBACK_ORDER:
        preset = BASEMAP_PRESETS[name]
        if name == resolved['source'] or not preset['wgs84']:
            continue
        chain.append({
            'upstream': preset['url'],
            'max_level': preset['max_level'],
            'credit': preset['credit'],
            'source': name,
        })
    return chain


def client_descriptor(resolved: Dict[str, Any]) -> Dict[str, Any]:
    """resolve_basemap 的结果 -> 下发给浏览器的图层描述。

    **不含 upstream**：前端只知道同源路径，不知道真实上游是谁。这不是保密，
    是架构约束 —— 前端一旦拿到上游地址就会有人图省事直连回去，CORS 与
    「底图不吃代理」这两个坑立刻复活。
    """
    return {
        'url': BASEMAP_TILE_PATH,
        'max_level': resolved['max_level'],
        'credit': resolved['credit'],
        'source': resolved['source'],
        # 实际在用的源与配置的源不一致时，前端要能说出来 —— 底图默默换了一张
        # 而用户不知道，正是本项目最不能接受的那种「静默」。
        'configured_source': resolved.get('configured_source', resolved['source']),
        'fallback': resolved.get('fallback', False),
    }


def validate_basemap_source(value: Optional[str]) -> Tuple[bool, Optional[str]]:
    """校验 basemap_source 的写入值。返回 (ok, error_message)。"""
    raw = (value or '').strip()
    if not raw:
        return False, t('val.basemap.empty')
    if raw in BASEMAP_PRESETS or raw == DOWNLOAD_SOURCE:
        return True, None
    if raw.startswith(('http://', 'https://')):
        ok, err = validate_server_entry(raw)
        if not ok:
            return ok, err
        # 链路本地段(169.254.0.0/16、fe80::/10)拒收。这个值与其他配置项不同:
        # /basemap/{z}/{x}/{y} 会**由服务端**去取它并把响应体原样回吐给浏览器,
        # 所以一个指向 169.254.169.254 的模板等于把服务端当跳板去读云实例元数据。
        # 有意只拦链路本地,不拦回环/私网 —— 自建瓦片镜像住在 127.0.0.1 或
        # 192.168.x.x 是项目文档里就有的正当用法,而 169.254.x.x 从来不是一个
        # 瓦片服务地址。取瓦片时另有一道同样的闸(routes/basemap_static.py),
        # 因为存量库里可能已经存着这样的值 —— 校验只管新写入。
        if is_link_local_url(raw):
            return False, t('val.basemap.link_local', value=raw)
        return True, None
    return False, t('val.basemap.unknown', value=raw)
