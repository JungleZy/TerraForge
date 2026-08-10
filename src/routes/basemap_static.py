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

import hashlib
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, request
from werkzeug.exceptions import default_exceptions

from src.core.config import Config
from src.services.basemap_source import (
    fallback_candidates,
    resolve_basemap,
    source_version,
)
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


# --- 瓦片磁盘缓存（cache/basemap/） ------------------------------------------
#
# 为什么要有它：每张瓦片都经代理回源，上游 RTT 秒级（实测与连接复用无关，
# keep-alive 无改善）。首屏几十张瓦片的风暴占满浏览器对单源的 6 条
# HTTP/1.1 连接长达 15-30 秒，期间页面上一切 API 操作（配置保存等）都要
# 在浏览器连接池里排队 —— 「配置页第一次点保存要等很久」的根因。这个
# 阻塞的**结构性**修复在 src/core/tile_server.py（瓦片走独立端口、换连接池）；
# 磁盘缓存管的是另一半：浏览器缓存只挡一天（回退期出的图更是只配 60s），
# 风暴天天重演。瓦片按 URL 内容基本不变，服务端落盘一次，之后任何浏览器
# 任何一天都是毫秒级命中。
#
# 口径与下载瓦片缓存（download_engine）一致：不做自动清理，配置页「缓存
# 管理」按 cache 顶层子目录自动成类、手动清；受 cache_enabled 同一个开关
# 管。键是完整上游 URL 的 sha1 —— 回退候选的图各存各的，互不挤占。
_CT_TO_EXT = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}
_EXT_TO_CT = {ext: ct for ct, ext in _CT_TO_EXT.items()}

# 开关读取的短 TTL 缓存，与 _CACHE_KEY_SOURCE 同理由：瓦片热路径上不能
# 每张都新开一条 sqlite 连接。TTL 内改开关最多晚 5 秒生效，与底图源一致。
_CACHE_KEY_DISK_ENABLED = "basemap_static_disk_cache_enabled"


def _disk_cache_enabled() -> bool:
    now = time.monotonic()
    entry = current_app.extensions.get(_CACHE_KEY_DISK_ENABLED)
    if entry is not None and now - entry[0] < _SOURCE_TTL_S:
        return entry[1]
    enabled = (config_manager.get('cache_enabled', 'true') or 'true').lower() == 'true'
    current_app.extensions[_CACHE_KEY_DISK_ENABLED] = (now, enabled)
    return enabled


def _tile_cache_path(upstream: str) -> Path:
    return Path(Config.CACHE_DIR) / 'basemap' / hashlib.sha1(
        upstream.encode('utf-8')).hexdigest()


def _cache_lookup(upstream: str):
    """命中返回 (body, content_type)，未命中返回 None。"""
    base = _tile_cache_path(upstream)
    for ext, ct in _EXT_TO_CT.items():
        try:
            return base.with_suffix(ext).read_bytes(), ct
        except OSError:
            continue
    return None


def _cache_store(upstream: str, body: bytes, content_type) -> None:
    # 只缓存认识的图片类型：上游风控/运营商劫持回的错误页（text/html）
    # 一旦落盘就把「瓦片坏了」钉成「永远是坏的」。
    ext = _CT_TO_EXT.get(str(content_type or '').split(';')[0].strip().lower())
    if ext is None:
        return
    directory = _tile_cache_path(upstream).parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # tmp + replace：werkzeug 多线程下两个线程可以同时回源同一张瓦片，
        # 直接写目标名会让读者看到半个文件。
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix='.tmp_')
        with os.fdopen(fd, 'wb') as f:
            f.write(body)
        os.replace(tmp, str(_tile_cache_path(upstream)) + ext)
    except OSError as e:
        logger.warning(f'底图瓦片写缓存失败（不影响本次出图）：{e}')


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

# 短缓存时长。回退瓦片、以及请求的 URL 空间与真正出图的源对不上的瓦片都用它
# （判定见 basemap_tile 结尾）。与冷却期同量级：冷却一到期配置的源就会被再试
# 一次，缓存也该在同一时间尺度上过期，否则「上游恢复了」和「用户看到恢复」
# 之间会隔着一天的缓存。
_SHORT_MAX_AGE_S = 60


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
    # 没有显式代理时**不装** ProxyHandler，让 urllib 照默认行为读
    # HTTP(S)_PROXY。这条口径必须与下载路径一致：download_engine 的 aiohttp
    # 开着 trust_env=True，proxy_url 为空时照样吃环境变量。这里曾经传
    # ProxyHandler({})（关掉环境变量），于是「export 了 HTTP_PROXY 又关掉代理
    # 自动发现」的 WSL 用户得到的是「下载正常、底图一颗蓝球」—— 正是本路由
    # 要消灭的那种分叉。自动探测验不通某个环境变量代理，也不代表它对底图上游
    # 不通，不该主动掐掉（同 download_engine 里 trust_env 那段理由）。
    handlers = ([urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
                if proxy else [])
    opener = urllib.request.build_opener(*handlers)
    # 局部名不叫 request：模块级的 request 是 flask 的请求对象（basemap_tile
    # 读它的查询参数），同名会让读到这里的人以为拿的是同一个东西。
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with opener.open(req, timeout=_TIMEOUT_S) as response:
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

    version 跟的始终是**配置的**源（记录里没有它，upstream 被特意剥掉了）：
    浏览器要请求的 URL 空间由用户配的那张图决定，回退只是这条空间里临时出的
    图。跟着回退源走的话，配置的源一恢复，页面手里的 URL 就成了旧空间。
    """
    entry = current_app.extensions.get(_CACHE_KEY_ACTIVE)
    if not entry:
        return resolved
    configured_source, active = entry
    if configured_source != resolved["source"] or active["source"] == resolved["source"]:
        return resolved
    return {**active, "configured_source": resolved["source"],
            "version": resolved["version"], "fallback": True}


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
    # 代理**惰性**解析：磁盘缓存命中的请求整条跳过网络路径，不能再为它们
    # 支付 resolve_from_config 的 _PROXY_WAIT_S 阻塞（缓存的全部意义就是
    # 毫秒级出图）。_proxy_unset 作哨兵：代理值本身可以是 ''（直连）。
    _proxy_unset = object()
    proxy = _proxy_unset
    disk_cache_on = _disk_cache_enabled()

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

        # 磁盘缓存先于网络：命中即出图，不碰代理、不写冷却、不算上游故障。
        # 404 不透缓存（_cache_store 只收 200 的图片），所以「覆盖空洞」永远
        # 实时问上游，不会被钉死。
        cached = _cache_lookup(upstream) if disk_cache_on else None
        if cached is not None:
            body, content_type = cached
        else:
            if proxy is _proxy_unset:
                proxy = resolve_from_config(config_manager, wait_s=_PROXY_WAIT_S)
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
            if disk_cache_on:
                _cache_store(upstream, body, content_type)

        cooldown.pop(candidate["source"], None)
        _remember_fallback(configured, candidate)

        response = Response(body, mimetype=content_type)
        # 一天的浏览器缓存足够把平移/缩放的重复请求挡在本机，但只发给「请求
        # 所在的 URL 空间 == 这张图真正的出处」的那些瓦片：判据是请求里的 v
        # 与**本次真正出图的那个源**的 version 相等。v 由 client_descriptor 按
        # 当次实时解析的配置源算出（见 basemap_source.source_version），所以
        # 三种「存下来就撤不回」的情形自动落到短缓存：
        #   - 回退出的图：上游抖动 30 秒，浏览器把另一家的影像烤进缓存一整天，
        #     配置的源恢复后缓存命中不再回源，地图永远是两家拼图，界面上没有
        #     任何补救手段；
        #   - 用户刚改完配置、页面已经拿到新 v，而这里还在 _SOURCE_TTL_S 的
        #     窗口里按旧配置出图 —— 拿旧源的图占住新 URL 空间一整天；
        #   - 不带 v 的请求（旧页面、手输地址）：它的 URL 空间不随源变化，
        #     长缓存在那里同样撤不回。
        # 用 v 比对而不是「重读一次配置」：后者是每张瓦片再开一条 sqlite 连接，
        # 正是 _SOURCE_TTL_S 那份缓存要避免的开销。
        response.headers["Cache-Control"] = (
            "public, max-age=86400"
            if request.args.get("v") == source_version(candidate["upstream"])
            else f"public, max-age={_SHORT_MAX_AGE_S}")
        return response

    abort(first_error or 502)
