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
