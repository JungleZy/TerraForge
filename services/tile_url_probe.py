"""底图瓦片服务地址的校验与通联探测（配置页「验证通联」按钮）。

拆成独立模块而不是塞进 routes/api.py：模板校验与样例瓦片坐标换算都是
纯函数，单测不需要起 Flask；HTTP 抓取收敛在 _fetch_tile 一处，
probe_tile_url 接受 fetcher 注入，成功路径可以无网测试。
"""
import asyncio
import ipaddress
import logging
import math
import time
from urllib.parse import urlsplit

import aiohttp

logger = logging.getLogger(__name__)

# 前端（map.js initMap）与这里共用的内置底图源；改要一起改。
DEFAULT_MAP_TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'

# 探测只取前几十 KB，确认链路通即可，不把整张瓦片读进来。
_MAX_PROBE_BYTES = 64 * 1024


def validate_tile_url_template(url):
    """校验 XYZ 瓦片 URL 模板。返回 (ok, error_message)。

    规则：http/https 协议，且 {z} {x} {y} 三个占位符齐全
    （Cesium 的 UrlTemplateImageryProvider 按这三个占位符展开）。
    """
    url = (url or '').strip()
    if not url:
        return False, '地址不能为空（留空请使用内置 OSM 源）'
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https'):
        return False, f'只支持 http/https 协议（当前是 {parts.scheme or "无"}）'
    if not parts.netloc:
        return False, 'URL 缺少主机名'
    missing = [p for p in ('{z}', '{x}', '{y}') if p not in url]
    if missing:
        return False, '模板缺少占位符 ' + ' '.join(missing)
    return True, None


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


def probe_tile_url(template, proxy_url='', center_lng=106.55, center_lat=29.56,
                   timeout_s=10, fetcher=None):
    """校验模板并探测通联。返回可 JSON 化的结果 dict。

    fetcher 可注入（默认 _fetch_tile），测试用假 fetcher 覆盖成功/失败路径，
    不需要真实网络。
    """
    ok, err = validate_tile_url_template(template)
    if not ok:
        return {'success': False, 'status_code': None, 'content_type': '',
                'elapsed_ms': 0, 'bytes_read': 0, 'tile': None, 'error': err}

    url, (z, x, y) = build_probe_url(template.strip(), center_lng, center_lat)
    if should_bypass_proxy(url):
        proxy_url = ''
    fetcher = fetcher or _fetch_tile
    result = asyncio.run(fetcher(url, proxy_url, timeout_s))
    result['tile'] = f'{z}/{x}/{y}'
    logger.info(
        f'Tile URL probe {url} (proxy={"direct" if not proxy_url else "on"}) -> '
        f'success={result["success"]} status={result["status_code"]} '
        f'elapsed={result["elapsed_ms"]}ms'
    )
    return result
