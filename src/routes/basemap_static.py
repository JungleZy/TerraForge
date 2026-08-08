"""底图瓦片的后端转发 —— /basemap/<z>/<x>/<y>。

## 为什么底图不让浏览器直连上游

**1. CORS 会把真实错误埋掉。** Cesium 用 XHR 取瓦片，跨域就要求上游返回
Access-Control-Allow-Origin。上游一旦返回 4xx，错误页通常不带这个头，浏览器
于是报成一句 "blocked by CORS policy" —— 真正的状态码（实测是 Esri 的 403）
被盖住，看报错的人会以为是 CORS 配置问题，去查一个根本不存在的问题。

**2. 代理。这条是决定性的。** 浏览器**不吃**项目里的 proxy_url / 代理自动
发现，那套只作用于后端的下载路径。也就是说底图和下载走的是两条完全不同的
出网路径：给下载配好代理、瓦片哗哗下，底图照样可以是一个蓝色球体；反过来
后端连不上而浏览器能连也一样割裂。转发之后两者共用 proxy_autodetect
的同一个入口，配好一个就都通。

同源之后 CORS 这件事从根上不存在了，不管底图源是谁、上游返回什么状态码。

## 范围

只转发瓦片字节。这是一个本机单用户的桌面工具，上游地址由用户自己在配置页
填写，因此这里不做 URL 白名单 —— 但仍然只接受 http(s)，并且 z/x/y 必须是
合法瓦片坐标，不让路径参数直接拼进上游地址。
"""

import logging
import urllib.error
import urllib.request

from flask import Blueprint, Response, abort

from src.services.basemap_source import resolve_basemap
from src.services.config_manager import ConfigManager
from src.services.proxy_autodetect import resolve_from_config

logger = logging.getLogger(__name__)

basemap_static_bp = Blueprint("basemap_static", __name__, url_prefix="/basemap")

config_manager = ConfigManager()

_TIMEOUT_S = 15.0

# 代理自动发现还没跑完时最多等这么久。刻意远小于 proxy_autodetect 的默认
# 25 秒：一次首屏是几十上百个瓦片请求，每个都阻塞 25 秒会把线程池坐满，
# 表现成整个界面卡死。探测在启动时就已后台跑起来，这里等不到就先直连。
_PROXY_WAIT_S = 3.0

# 上游把 UA 当风控信号（Esri 与 Google 都会）。urllib 默认的
# "Python-urllib/3.x" 是最容易吃 403 的一种。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_MAX_ZOOM = 24


@basemap_static_bp.route("/<int:z>/<int:x>/<int:y>", methods=["GET"])
def basemap_tile(z: int, x: int, y: int):
    # 坐标合法性先于一切：z/x/y 会被拼进上游 URL，越界值既没有意义，
    # 也不该让它有机会跑到上游去。
    if not 0 <= z <= _MAX_ZOOM:
        abort(404)
    limit = 1 << z
    if not (0 <= x < limit and 0 <= y < limit):
        abort(404)

    resolved = resolve_basemap(
        config_manager.get("basemap_source", ""),
        tile_servers=config_manager.get("tile_servers", ""),
        default_style=config_manager.get("default_style", "m"),
    )
    upstream = (resolved["upstream"]
                .replace("{z}", str(z))
                .replace("{x}", str(x))
                .replace("{y}", str(y)))
    if not upstream.startswith(("http://", "https://")):
        logger.error(f"底图上游地址不是 http(s)：{upstream!r}")
        abort(502)

    proxy = resolve_from_config(config_manager, wait_s=_PROXY_WAIT_S)
    # 传空 dict 而不是不装 handler：ProxyHandler({}) 会**关掉** urllib 对
    # HTTP_PROXY 等环境变量的隐式读取。代理的唯一事实源是 proxy_autodetect
    # （它本来就把环境变量算作候选之一），这里再隐式吃一次会造成
    # 「配置页显示直连、实际走了环境变量代理」的分叉。
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    )
    request = urllib.request.Request(upstream, headers={"User-Agent": _UA})

    try:
        with opener.open(request, timeout=_TIMEOUT_S) as upstream_response:
            body = upstream_response.read()
            content_type = upstream_response.headers.get("Content-Type", "image/jpeg")
    except urllib.error.HTTPError as e:
        # 原样透传上游状态码。同源之后浏览器能如实看到 403/404，而不是
        # 被 CORS 消息盖住 —— 这正是本模块要解决的问题之一。
        logger.warning(f"底图上游 {z}/{x}/{y} 返回 {e.code}（源：{resolved['source']}）")
        abort(e.code if 400 <= e.code < 600 else 502)
    except Exception as e:
        logger.warning(f"底图上游 {z}/{x}/{y} 取瓦片失败（源：{resolved['source']}）：{e}")
        abort(504)

    response = Response(body, mimetype=content_type)
    # 底图瓦片内容基本不变，但源可以被用户随时改掉，所以不能 immutable。
    # 一天的浏览器缓存足够把平移/缩放的重复请求挡在本机。
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response
