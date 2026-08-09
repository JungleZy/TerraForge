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
import time
import urllib.error
import urllib.request

from flask import Blueprint, Response, abort, current_app
from werkzeug.exceptions import default_exceptions

from src.services.basemap_source import fallback_candidates, resolve_basemap
from src.services.config_manager import ConfigManager
from src.services.proxy_autodetect import resolve_from_config
from src.services.tile_url_probe import is_link_local_url

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

# 底图源解析结果的短 TTL 缓存。这是瓦片热路径:改造前每张瓦片要读三个配置项
# (basemap_source / tile_servers / default_style),而 ConfigManager.get 每次都
# 新开一条 sqlite 连接 —— 首屏几十上百张瓦片就是几百次连接开销,还与
# _PROXY_WAIT_S 的线程占用叠加。缓存挂 app.extensions 而非模块级:测试
# fresh-import app 时能拿到干净缓存,routes 模块本身不会被重导入
# (口径与 terrain_static._base_path_cached 一致,那边为同一个问题写过同一套)。
# TTL 到期即失效,所以配置页改完底图源最多 _SOURCE_TTL_S 秒生效,不需要
# 失效钩子;缓存的是解析【结果】而不是三个原始值,顺带省掉每瓦片一次
# resolve_basemap。
_CACHE_KEY_SOURCE = "basemap_static_resolved_source"
_SOURCE_TTL_S = 5.0


def _resolved_basemap_cached():
    now = time.monotonic()
    entry = current_app.extensions.get(_CACHE_KEY_SOURCE)
    if entry is not None and now - entry[0] < _SOURCE_TTL_S:
        return entry[1]
    resolved = resolve_basemap(
        config_manager.get("basemap_source", ""),
        tile_servers=config_manager.get("tile_servers", ""),
        default_style=config_manager.get("default_style", "m"),
    )
    current_app.extensions[_CACHE_KEY_SOURCE] = (now, resolved)
    return resolved


# 取不到瓦片的源的冷却表：{source: 冷却到期的 monotonic 时刻}。
#
# 没有它的话每张瓦片都要把挂掉的源重试一遍 —— 实测 Esri 被 CDN 封时每次是
# 一整个 TCP+TLS 往返，首屏几十张瓦片就是几十次白等。冷却到期后会再试一次
# 配置的源，所以上游恢复了不需要用户做任何事（代价是每 _COOLDOWN_S 一次
# 失败请求）。
#
# 与 _CACHE_KEY_SOURCE 一样挂在 app.extensions 上：测试 fresh-import app
# 时拿到的是干净状态，模块级 dict 会跨用例串味。
_CACHE_KEY_COOLDOWN = "basemap_static_source_cooldown"
_COOLDOWN_S = 60.0

# 回退瓦片的浏览器缓存时长。与冷却期同量级：冷却一到期配置的源就会被再试一次，
# 缓存也该在同一时间尺度上过期，否则「上游恢复了」和「用户看到恢复」之间会隔着
# 一天的缓存。
_FALLBACK_MAX_AGE_S = 60


def _cooldown_map():
    return current_app.extensions.setdefault(_CACHE_KEY_COOLDOWN, {})


def _marks_source_down(code: int) -> bool:
    """这个状态码说的是「这个源现在整个不能用」，还是只是「这一次不行」。

    只有 403（风控/封出口 IP）、429（限流）和 5xx（上游自己崩了）是源级信号。
    404 在取瓦片那里已经原样透传掉了；其余 4xx（400/410/451…）是针对**这一次
    请求**的答复，拿它冷却整个源等于让一块坏瓦片决定其余几十块去哪儿取。
    """
    return code in (403, 429) or 500 <= code < 600


def _fetch_upstream(url: str, proxy):
    """取一块上游瓦片，返回 (body, content_type)。失败抛异常。"""
    # 传空 dict 而不是不装 handler：ProxyHandler({}) 会**关掉** urllib 对
    # HTTP_PROXY 等环境变量的隐式读取。代理的唯一事实源是 proxy_autodetect
    # （它本来就把环境变量算作候选之一），这里再隐式吃一次会造成
    # 「配置页显示直连、实际走了环境变量代理」的分叉。
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    )
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with opener.open(request, timeout=_TIMEOUT_S) as response:
        return response.read(), response.headers.get("Content-Type", "image/jpeg")


# 最近一次成功出图的记录：(当时配置的源, 实际出图的源的描述子集)。/api/basemap
# 与首页内联的描述符都读它，好让界面说得出「底图已回退到 OSM」——底图默默换了
# 一张而用户不知道，正是本项目最不能接受的那种静默。
_CACHE_KEY_ACTIVE = "basemap_static_active_fallback"


def _remember_fallback(configured, candidate):
    """记下「刚才是哪个源真的出了图」。

    存的是**去掉 upstream 的子集**：active_basemap 是被 routes/api.py 与
    routes/main.py 两处 import 的公开函数，上游地址一旦进了它的返回值，
    离哪天有人直接 jsonify 它、把上游地址漏回浏览器就只差一次手滑。

    连当时配置的源一起存：这条记录只对**那次配置**有意义。用户把底图从 esri
    改成 google_roadmap 之后，「已回退到 osm」说的是旧配置下的事，新配置下
    一张瓦片都还没取过 —— 不带配置源比对的话，界面会挂着一条根本没发生过的
    回退提示，直到下一张瓦片落地才自己消失。
    """
    current_app.extensions[_CACHE_KEY_ACTIVE] = (configured["source"], {
        "source": candidate["source"],
        "max_level": candidate["max_level"],
        "credit": candidate["credit"],
    })


def active_basemap(resolved):
    """把「实际在用的源」叠加到 resolve_basemap 的结果上。

    还没取过任何瓦片时（首次打开页面），以及配置的源在记录之后被改过时，
    回退状态都是未知的，如实报配置的源；第一批瓦片取完状态就落定了，
    下次刷新页面描述符就是准的。
    """
    entry = current_app.extensions.get(_CACHE_KEY_ACTIVE)
    if not entry:
        return resolved
    configured_source, active = entry
    if configured_source != resolved["source"] or active["source"] == resolved["source"]:
        return resolved
    return {**active, "configured_source": resolved["source"], "fallback": True}


@basemap_static_bp.route("/<int:z>/<int:x>/<int:y>", methods=["GET"])
def basemap_tile(z: int, x: int, y: int):
    # 坐标合法性先于一切：z/x/y 会被拼进上游 URL，越界值既没有意义，
    # 也不该让它有机会跑到上游去。
    if not 0 <= z <= _MAX_ZOOM:
        abort(404)
    limit = 1 << z
    if not (0 <= x < limit and 0 <= y < limit):
        abort(404)

    configured = _resolved_basemap_cached()
    cooldown = _cooldown_map()
    now = time.monotonic()
    proxy = resolve_from_config(config_manager, wait_s=_PROXY_WAIT_S)

    # 配置值本身不合法是**配置错误，不是上游故障** —— 必须当场 502，绝不能
    # 让回退链把它盖掉。盖掉的后果：用户把底图指到 169.254.169.254（读云实例
    # 元数据的经典 SSRF 目标）却看到一张正常的 OSM，界面上没有任何异样，
    # 他永远不知道自己写错了、也不知道服务端替他挡了一次。
    head = (configured["upstream"]
            .replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y)))
    if not head.startswith(("http://", "https://")):
        logger.error(f"底图上游地址不是 http(s)：{head!r}")
        abort(502)
    # 链路本地段拒取。这条路会把上游响应体**原样回吐**给浏览器,所以一个指向
    # 169.254.169.254 的模板等于把服务端当跳板去读云实例元数据。写入侧
    # (basemap_source.validate_basemap_source) 已经拦了,这里再拦一次是因为
    # 存量库里可能已经存着这样的值 —— 校验只管新写入,不会回溯改写已有配置。
    # 只拦链路本地:自建镜像在 127.0.0.1 / 局域网 IP 是正当用法。
    if is_link_local_url(head):
        logger.error(f"底图上游指向链路本地地址,拒绝取瓦片：{head!r}")
        abort(502)

    chain = fallback_candidates(configured)
    # 冷却中的源排到最后而不是直接剔除：全链都在冷却时仍要有东西可试，
    # 否则一次全网抖动会把底图锁死到冷却期满。
    order = sorted(range(len(chain)), key=lambda i: cooldown.get(chain[i]["source"], 0) > now)

    first_error = None
    for i in order:
        candidate = chain[i]
        upstream = (candidate["upstream"]
                    .replace("{z}", str(z))
                    .replace("{x}", str(x))
                    .replace("{y}", str(y)))
        # 上面已经把配置项那条验过了；自动追加的候选全是本仓写死的 https 预设，
        # 这道检查是「万一预设表被改坏」的兜底，正常永远不触发。
        if not upstream.startswith(("http://", "https://")) or is_link_local_url(upstream):
            logger.error(f"回退候选的上游地址不可用，跳过：{upstream!r}")
            continue

        try:
            body, content_type = _fetch_upstream(upstream, proxy)
        except urllib.error.HTTPError as e:
            # 404 不是故障信号，是每个 XYZ 瓦片服务说「这里没有图」的方式：Esri
            # 的 World Imagery 在覆盖空洞和超出层级上限时就这么答。把它当上游挂
            # 掉的话，一张缺图会让整个源冷却 60 秒、后续每张瓦片都换供应商、界面
            # 弹一次根本没发生过的「底图已切换」。原样透传，不写冷却、不动回退
            # 状态 —— 与回退特性引入之前的行为一致。
            if e.code == 404:
                logger.debug(f"底图上游 {z}/{x}/{y} 无此瓦片（源：{candidate['source']}）")
                abort(404)
            logger.warning(f"底图上游 {z}/{x}/{y} 返回 {e.code}（源：{candidate['source']}）")
            if _marks_source_down(e.code):
                cooldown[candidate["source"]] = now + _COOLDOWN_S
            # 原样透传的是**配置那个源**的状态码：整条链都失败时，用户想知道的
            # 是他选的那张图怎么了，而不是链尾那张。取第一个报错是不够的 ——
            # 配置的源一旦进了冷却就被排到链尾，"第一个"会变成某个替补，用户于是
            # 收到 504 而不是 Esri 真正回的 403。配置的源在链里只出现一次，
            # 所以让它无条件覆盖不会被后面的候选再改掉。
            # werkzeug 的 Aborter 对没有异常类的状态码（499/520/521/522/525/530
            # 这些 Cloudflare 自定义码）抛 LookupError，被 Flask 转成 500 ——
            # 那正是本模块要防的「真实状态码被埋掉」。抬不动的一律记 502。
            code = e.code if e.code in default_exceptions else 502
            if candidate["source"] == configured["source"] or first_error is None:
                first_error = code
            continue
        except Exception as e:
            logger.warning(f"底图上游 {z}/{x}/{y} 取瓦片失败（源：{candidate['source']}）：{e}")
            cooldown[candidate["source"]] = now + _COOLDOWN_S
            if candidate["source"] == configured["source"] or first_error is None:
                first_error = 504
            continue

        cooldown.pop(candidate["source"], None)
        _remember_fallback(configured, candidate)

        response = Response(body, mimetype=content_type)
        # 底图瓦片内容基本不变，但源可以被用户随时改掉，所以不能 immutable。
        # 一天的浏览器缓存足够把平移/缩放的重复请求挡在本机。
        #
        # 回退出来的瓦片**不能**照这个存：上游抖动 30 秒，浏览器就会把另一家的
        # 影像烤进缓存一整天，等配置的源恢复后地图变成两家拼图，且因为缓存命中
        # 不再回源，它永远不会自己好 —— 用户在界面上也没有任何补救手段。短缓存
        # 仍然挡得住一次平移里的重复请求，又能让恢复在一分钟内自动生效。
        response.headers["Cache-Control"] = (
            "public, max-age=86400" if candidate["source"] == configured["source"]
            else f"public, max-age={_FALLBACK_MAX_AGE_S}")
        return response

    abort(first_error or 502)
