"""瓦片服务器条目的展开、校验与通联探测。

tile_servers 配置（逗号分隔列表）的统一语义，下载引擎 / 底图 / 配置页
「验证」按钮共用这一份，避免三处各写一套解析：

  条目三种形态：
    1. Google 别名   —— `mts0`（不含点），展开为 `mts0.googleapis.com`
    2. 主机名        —— `mts0.google.cn`，按 Google vt 格式拼 lyrs URL
    3. 完整 XYZ 模板 —— `https://.../{z}/{x}/{y}.png`，可选 `{style}`
                        占位符（Google 兼容镜像用），没有 {style} 的模板
                        样式由地址自身决定，下载时忽略样式选择

通联探测给配置页「验证」按钮用：拆成独立模块而不是塞进 routes/api.py，
模板校验与坐标换算是纯函数，单测不需要起 Flask；HTTP 抓取收敛在
_fetch_tile 一处，probe_server_entry 接受 fetcher 注入，成功路径无网可测。
"""
import asyncio
import ipaddress
import logging
import math
import re
import time
from urllib.parse import urlsplit

import aiohttp

from services.system_proxy import mask_url_userinfo

logger = logging.getLogger(__name__)

# 配置里 tile_servers 为空时的回退（与 core/database.py 的默认值一致）。
DEFAULT_TILE_SERVERS = 'mts0,mts1,mts2,mts3'

# 探测只取前几十 KB，确认链路通即可，不把整张瓦片读进来。
_MAX_PROBE_BYTES = 64 * 1024

# 主机/别名形态：字母数字、点、连字符（mts0 / mts0.google.cn / mt0.l.google.com）
_HOST_ENTRY_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9.-]*$')

# 模板里出现的 {占位符}。下载引擎（download_engine.py get_tile_url）只替换
# {z}/{x}/{y}，expand_server_entry 替换 {style}；含 {s} 等其它占位符的模板
# 能过校验但探测/下载必然失败，校验阶段直接拒绝。
_PLACEHOLDER_RE = re.compile(r'\{[^{}]*\}')
_ALLOWED_PLACEHOLDERS = frozenset(('{z}', '{x}', '{y}', '{style}'))


def expand_server_entry(entry, style='m'):
    """把一个服务器条目展开成带 {x}/{y}/{z} 占位符的 URL 模板。

    别名/主机形态按 Google vt 格式拼 lyrs；完整模板原样返回，
    其中的 {style} 占位符替换为当前样式码（模板没有 {style} 则样式固定）。
    """
    entry = (entry or '').strip()
    if entry.startswith(('http://', 'https://')):
        return entry.replace('{style}', style)
    host = entry if '.' in entry else entry + '.googleapis.com'
    return f'http://{host}/vt?lyrs={style}&x={{x}}&y={{y}}&z={{z}}'


def validate_server_entry(entry):
    """校验单个服务器条目。返回 (ok, error_message)。"""
    entry = (entry or '').strip()
    if not entry:
        return False, '条目不能为空'
    if entry.startswith(('http://', 'https://')):
        parts = urlsplit(entry)
        if not parts.netloc:
            return False, 'URL 缺少主机名'
        missing = [p for p in ('{z}', '{x}', '{y}') if p not in entry]
        if missing:
            return False, '模板缺少占位符 ' + ' '.join(missing)
        unknown = [p for p in _PLACEHOLDER_RE.findall(entry)
                   if p not in _ALLOWED_PLACEHOLDERS]
        if unknown:
            return False, '模板包含不支持的占位符 ' + ' '.join(unknown)
        return True, None
    if '://' in entry:
        return False, '只支持 http/https 协议'
    if not _HOST_ENTRY_RE.match(entry):
        return False, f'无法识别的主机/别名：{entry}'
    return True, None


def validate_server_list(value):
    """校验逗号分隔的整个列表（ConfigManager.validate_config 用）。"""
    entries = [s.strip() for s in (value or '').split(',') if s.strip()]
    if not entries:
        return False, '瓦片服务器列表不能为空'
    for entry in entries:
        ok, err = validate_server_entry(entry)
        if not ok:
            return False, f'{entry}: {err}'
    return True, None


def parse_server_list(value):
    """逗号分隔字符串 -> 条目列表；空/全空白回退默认 mts0-3。"""
    entries = [s.strip() for s in (value or '').split(',') if s.strip()]
    return entries or DEFAULT_TILE_SERVERS.split(',')


def _tile_xy(lng, lat, z):
    """标准 Web 墨卡托 XYZ 换算：经纬度 -> (x, y)。"""
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    lat_rad = math.radians(max(-85.05112878, min(85.05112878, lat)))
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return min(n - 1, max(0, x)), min(n - 1, max(0, y))


def build_probe_url(template, center_lng, center_lat, zoom=3):
    """把模板展开成一张用户实际会看到的样例瓦片 URL（默认 z3 地图中心）。"""
    x, y = _tile_xy(center_lng, center_lat, zoom)
    return (template
            .replace('{z}', str(zoom))
            .replace('{x}', str(x))
            .replace('{y}', str(y))), (zoom, x, y)


def should_bypass_proxy(url):
    """本机/内网地址不走代理。

    proxy_url 是给「访问 Google/OSM 等公网源」配置的；把它套在
    127.0.0.1 / 192.168.x.x 这类目标上，探测请求会被代理转发到
    它自己根本到不了的地方（WSL 里尤其明显：代理在 Windows 宿主机上，
    回环地址各是各的），表现为长时间挂起后超时。
    """
    host = (urlsplit(url).hostname or '').lower()
    if host in ('localhost', 'localhost.localdomain'):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False            # 域名：无法判断是不是内网，按公网走代理
    return ip.is_loopback or ip.is_private or ip.is_link_local


async def _fetch_tile(url, proxy_url, timeout_s):
    """抓取样例瓦片的前 _MAX_PROBE_BYTES 字节。

    返回 dict(success, status_code, content_type, elapsed_ms, bytes_read, error)。
    所有 aiohttp/超时异常在这里归一成 success=False —— 调用方拿到的永远是
    可 JSON 化的结果 dict。
    """
    started = time.monotonic()
    result = {'success': False, 'status_code': None, 'content_type': '',
              'elapsed_ms': 0, 'bytes_read': 0, 'error': None}
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        # trust_env=True：读 HTTP(S)_PROXY 环境变量，app.py 的
        # apply_system_proxy() 会把 Windows 系统代理灌进去（与下载引擎同款）。
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, proxy=proxy_url or None) as resp:
                result['status_code'] = resp.status
                result['content_type'] = resp.headers.get('Content-Type', '')
                data = await resp.content.read(_MAX_PROBE_BYTES)
                result['bytes_read'] = len(data)
                result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
                if resp.status != 200:
                    result['error'] = f'HTTP {resp.status}'
                elif not data:
                    result['error'] = '响应为空（0 字节）'
                else:
                    result['success'] = True
    except asyncio.TimeoutError:
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        result['error'] = f'连接超时（{timeout_s}s）'
    except aiohttp.ClientError as e:
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        result['error'] = f'连接失败：{e}'
    return result


def probe_server_entry(entry, proxy_url='', center_lng=106.55, center_lat=29.56,
                       timeout_s=10, fetcher=None):
    """校验条目并探测通联（样式固定用 m 标准图）。返回可 JSON 化的结果 dict。

    fetcher 可注入（默认 _fetch_tile），测试用假 fetcher 覆盖成功/失败路径，
    不需要真实网络。
    """
    ok, err = validate_server_entry(entry)
    if not ok:
        return {'success': False, 'status_code': None, 'content_type': '',
                'elapsed_ms': 0, 'bytes_read': 0, 'tile': None, 'error': err}

    template = expand_server_entry(entry.strip(), style='m')
    url, (z, x, y) = build_probe_url(template, center_lng, center_lat)
    if should_bypass_proxy(url):
        proxy_url = ''
    fetcher = fetcher or _fetch_tile
    result = asyncio.run(fetcher(url, proxy_url, timeout_s))
    result['tile'] = f'{z}/{x}/{y}'
    result['url'] = url
    logger.info(
        f'Tile server probe {mask_url_userinfo(url)} (proxy={"direct" if not proxy_url else "on"}) -> '
        f'success={result["success"]} status={result["status_code"]} '
        f'elapsed={result["elapsed_ms"]}ms'
    )
    return result


# --- 并发数「测速推荐」 ---------------------------------------------------------
#
# 推荐的依据是实测吞吐阶梯，不是公式：真实链路（代理、运营商、对端限速）
# 只有实测才知道。逐级（RECOMMEND_LEVELS）在固定时间窗内按该档并发下载
# 真实瓦片，计完成数得吞吐；_pick_concurrency 取膝点（达到最高吞吐 90%
# 的最小并发）。CPU 不参与 —— 瓦片下载是纯 IO 等待，核数不决定吞吐。

# 测量阶梯与每档时间窗。三档 × 8s ≈ 半分钟，是按钮点击可接受的等待上限。
RECOMMEND_LEVELS = (10, 25, 50)
RECOMMEND_WINDOW_S = 8.0
# 探测全失败（断网/代理不可用/瓦片 404）时的保守回退值。
RECOMMEND_FALLBACK = 20
# 探测瓦片层级与数量：z12 一块约几度见方，以地图中心为锚铺一圈网格，
# 循环取用（worker 循环消耗，URL 不够时重复请求同一瓦片，CDN 命中即可，
# 测的是链路吞吐不是内容）。
_RECOMMEND_ZOOM = 12
_RECOMMEND_URL_COUNT = 64


def _pick_concurrency(samples, knee_ratio=0.9, rise_ratio=1.15):
    """从阶梯测量结果挑推荐并发。samples: [{concurrency, tiles_per_sec, ok, ...}]。

    返回 (recommended, rising)；全部档位零成功返回 None。
      - 膝点：tiles_per_sec >= knee_ratio × 最高吞吐 的最小并发档 —— 再加
        并发收益不到 10% 就不值得多占连接；
      - rising：膝点就是顶格且顶格比次档仍快 rise_ratio 以上 —— 链路还没
        吃饱，提示用户可以再手动调高。
    """
    valid = [s for s in samples if s.get('ok', 0) > 0]
    if not valid:
        return None
    best = max(s['tiles_per_sec'] for s in valid)
    knee = min(s['concurrency'] for s in valid
               if s['tiles_per_sec'] >= knee_ratio * best)
    top = max(s['concurrency'] for s in valid)
    rising = False
    if knee == top and len(valid) > 1:
        below = max((s['tiles_per_sec'] for s in valid if s['concurrency'] < top),
                    default=0.0)
        rising = below > 0 and best >= rise_ratio * below
    return knee, rising


async def _measure_throughput(urls, concurrency, proxy_url, window_s, fetch=None):
    """在 window_s 时间窗内以 concurrency 条通道循环下载 urls，计完成数。

    fetch 可注入（测试用假 fetch）：async (url, proxy_url, timeout_s) -> bytes，
    抛异常即失败。默认 fetch 建带连接池的 session（与下载引擎同款的
    limit/limit_per_host=concurrency、trust_env），测出的才是引擎真实能
    跑到的吞吐。窗口一到取消所有在途请求，只计完成的。
    """
    result = {'concurrency': concurrency, 'ok': 0, 'attempted': 0,
              'seconds': 0.0, 'tiles_per_sec': 0.0}

    session = None
    if fetch is None:
        connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency)
        session = aiohttp.ClientSession(connector=connector, trust_env=True)

        async def _default_fetch(url, proxy, timeout_s):
            async with session.get(
                url, proxy=proxy or None,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status != 200:
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=resp.status)
                return await resp.read()
        fetch = _default_fetch

    stop = False

    async def worker():
        nonlocal stop
        while not stop:
            for url in urls:
                if stop:
                    return
                result['attempted'] += 1
                try:
                    await fetch(url, proxy_url, window_s + 10)
                    result['ok'] += 1
                except Exception:
                    pass  # 单请求失败不算测量失败，窗口内继续
                # 同步返回/立即抛错的 fetch(测试替身、连不上的秒失败)不给
                # 事件循环让路会把 asyncio.sleep(window_s) 饿死成忙等 ——
                # 每轮强制让出一次。
                await asyncio.sleep(0)

    started = time.monotonic()
    workers = [asyncio.ensure_future(worker()) for _ in range(concurrency)]
    try:
        await asyncio.sleep(window_s)
    finally:
        stop = True
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if session is not None:
            await session.close()
    elapsed = time.monotonic() - started
    result['seconds'] = round(elapsed, 2)
    result['tiles_per_sec'] = round(result['ok'] / elapsed, 2) if elapsed > 0 else 0.0
    return result


def _recommend_probe_urls(servers, style, center_lng, center_lat):
    """以地图中心为锚，在 _RECOMMEND_ZOOM 层铺一圈瓦片 URL（服务器逐条轮换）。"""
    cx, cy = _tile_xy(center_lng, center_lat, _RECOMMEND_ZOOM)
    n = 2 ** _RECOMMEND_ZOOM
    half = int(math.ceil(math.sqrt(_RECOMMEND_URL_COUNT))) // 2
    urls = []
    for dx in range(-half, half + 1):
        for dy in range(-half, half + 1):
            x, y = cx + dx, cy + dy
            if not (0 <= x < n and 0 <= y < n):
                continue
            template = expand_server_entry(servers[len(urls) % len(servers)], style)
            urls.append(template
                        .replace('{z}', str(_RECOMMEND_ZOOM))
                        .replace('{x}', str(x))
                        .replace('{y}', str(y)))
    return urls


def recommend_concurrency(servers, style='s', proxy_url='',
                          center_lng=106.55, center_lat=29.56,
                          measure=None):
    """实测吞吐阶梯，给出 concurrent_downloads 推荐值。返回可 JSON 化 dict。

    与 probe_server_entry 同款约定：sync 包装（内部 asyncio.run），任何
    故障都归一成 fallback 结果而不是抛给路由。measure 可注入（测试用
    假测量覆盖各档吞吐形态，不需要真实网络）。
    """
    measure = measure or _measure_throughput
    result = {'recommended': RECOMMEND_FALLBACK, 'fallback': True,
              'rising': False, 'note': '', 'samples': []}
    try:
        urls = _recommend_probe_urls(servers, style, center_lng, center_lat)
        if not urls:
            result['note'] = '瓦片服务器列表为空，无法测速，给出保守值'
            return result
        if should_bypass_proxy(urls[0]):
            proxy_url = ''

        async def _run():
            samples = []
            for level in RECOMMEND_LEVELS:
                samples.append(await measure(urls, level, proxy_url,
                                             RECOMMEND_WINDOW_S))
            return samples

        samples = asyncio.run(_run())
        result['samples'] = samples

        picked = _pick_concurrency(samples)
        if picked is None:
            result['note'] = ('测速样本全部失败（网络/代理不可用或瓦片不存在），'
                              f'给出保守值 {RECOMMEND_FALLBACK}')
            return result

        recommended, rising = picked
        recommended = max(1, min(100, recommended))  # 配置校验域 1-100
        best = max(s['tiles_per_sec'] for s in samples if s['ok'] > 0)
        result.update(recommended=recommended, fallback=False, rising=rising)
        result['note'] = (
            f'实测最高 {best:.1f} 块/秒；推荐并发 {recommended}'
            + ('，且顶格仍在上升，可再手动调高试试' if rising else '（膝点：再加并发收益不足 10%）')
        )
        logger.info(
            f'Concurrency recommendation: {recommended} '
            f'(samples={[(s["concurrency"], s["tiles_per_sec"]) for s in samples]})'
        )
        return result
    except Exception as e:
        logger.warning(f'Concurrency recommendation failed ({e!r}), fallback')
        result['note'] = f'测速出错（{type(e).__name__}），给出保守值 {RECOMMEND_FALLBACK}'
        return result
