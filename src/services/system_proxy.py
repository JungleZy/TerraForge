"""System proxy detection — injects Windows registry / OS-detected proxy
settings into HTTP(S)_PROXY env vars so aiohttp(trust_env=True) can use them.

Why this exists: PowerShell/curl on Windows use WinINET which auto-reads the
"system proxy" registry setting. aiohttp does NOT — even with trust_env=True
it only reads environment variables. End users running our packaged exe in
mainland China typically have Clash/V2Ray configured as a Windows system
proxy (not as env vars), so without this shim every Google Maps tile request
silently times out at 30s.

本模块**同时**是「URL 脱敏」的唯一实现处（`mask_url_secrets` /
`mask_text_secrets`）。它住在这里不是巧合：脱敏的第一个调用方是代理日志
（代理 URL 带 user:pass@），而这个模块只依赖标准库，日志、下载引擎、准入闸、
插件事件广播都能无环地 import 它。**凡是把 URL、或者可能内嵌 URL 的异常文本，
写进日志 / 数据库 / HTTP 响应的地方，都必须先过这两个函数之一。**
"""
import logging
import os
import re
import sys
import urllib.request
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

MASKED = '***'

#: 值必须掩掉的查询参数名。两类写法：
#: - `_SENSITIVE_QUERY_NAMES` 是**整名**匹配的短名字（`tk` 是天地图的 token，
#:   `sig` 是各家签名）——它们太短，按子串匹配会误伤 `network`、`design` 之类。
#: - `_SENSITIVE_QUERY_SUBSTRINGS` 是按**子串**认的词根，`apikey` / `api_key` /
#:   `access_token` / `X-Amz-Signature` 这一类各家自造的名字穷举不完。
#: 口径与 `task_logging._SENSITIVE_KEY` 一致：宁可多盖（`monkey=1` 也会变
#: `***`）也不能少盖——少盖一次就是一次凭据泄漏，多盖一次只是少一点排查信息。
_SENSITIVE_QUERY_NAMES = frozenset(('tk', 'sig'))
_SENSITIVE_QUERY_SUBSTRINGS = (
    'token', 'key', 'secret', 'signature', 'password', 'passwd',
    'credential', 'auth')

#: 匹配文本里嵌着的 URL。脱敏不能假设入参**整体**是一个 URL：真正的入口是
#: 一整行日志或一条异常消息（`aiohttp.ClientResponseError` 的 str() 就是
#: `403, message='Forbidden', url=URL('https://…?tk=真token')`）。
#:
#: **逗号不在排除集里**，这一条与它的出处（`task_logging` 那份旧正则）不同，
#: 是有意的：旧口径只掩 `user:pass@`，userinfo 在 netloc 里，永远排在查询串
#: 之前，逗号截断无害；新口径要掩的是**查询串尾部**的参数值，而带 bbox 的
#: WMS/ArcGIS URL 长这样 `…?bbox=1,2,3,4&tk=<真 token>` —— 在第一个逗号处
#: 截断就等于这条 URL 完全不脱敏。引号与括号仍然排除，所以 dict repr 与
#: `url=URL('…')` 这类包裹照样收得住边界；代价只是散文里 `见 https://h/a, 然后`
#: 会把那个逗号算进 URL —— 它原样出现在替换结果里，文本不变。
URL_IN_TEXT = re.compile(r'\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\'"<>)\]}]+')


def _is_sensitive_query_key(name: str) -> bool:
    lowered = (name or '').strip().lower()
    return (lowered in _SENSITIVE_QUERY_NAMES
            or any(s in lowered for s in _SENSITIVE_QUERY_SUBSTRINGS))


def _mask_query(query: str) -> str:
    """查询串里敏感参数的值 → `***`，其余部分**逐字**保留。

    刻意不走 `parse_qsl` + `urlencode`：那一趟会把没碰过的参数重新编码
    （`,` → `%2C`、`+` 与 `%20` 互换），日志里的 URL 就不再是发出去的那一条，
    排查时对不上。
    """
    if not query or '=' not in query:
        return query
    out = []
    for part in query.split('&'):
        name, sep, _ = part.partition('=')
        out.append(f'{name}={MASKED}'
                   if sep and _is_sensitive_query_key(name) else part)
    return '&'.join(out)


def mask_url_secrets(url) -> str:
    """把 URL 里的凭据掩掉：`user:pass@` → `***:***@`，敏感查询参数值 → `***`。

    host、路径与其余参数原样保留（排查要靠它们）。无法解析、或者没有任何东西
    需要掩时**原样返回**——不做规范化，日志里的 URL 与真正发出去的那条一致。
    只用于日志/响应/落库，不影响实际生效的值。
    """
    text = str(url)
    try:
        parts = urlsplit(text)
    except Exception:
        return text
    netloc = parts.netloc
    if '@' in netloc:
        netloc = f'{MASKED}:{MASKED}@{netloc.rsplit("@", 1)[1]}'
    query = _mask_query(parts.query)
    if netloc == parts.netloc and query == parts.query:
        return text
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


#: 匹配到的 URL 末尾要还回去的标点。`URL_IN_TEXT` 有意把逗号算进 URL（见那边
#: 的注释），于是散文里的 `见 https://h/a?tk=x, 然后` 会把句读吃掉；在这里剥
#: 回来，脱敏不改动它不该改的字符。**只剥尾部**，URL 中段的逗号（bbox）一个
#: 不动。
_TRAILING_PUNCT = ',.;:!?'


def mask_text_secrets(text) -> str:
    """把一段**任意文本**里每一条 URL 过一遍 `mask_url_secrets`。

    这是异常对象的必经之路：`f'{e!r}'` 里的 URL 不是整串而是嵌在中间的一段。
    掩不动就整段丢掉——那一段里可能就是 token。
    """
    def _one(m):
        raw = m.group(0)
        cut = len(raw)
        while cut and raw[cut - 1] in _TRAILING_PUNCT:
            cut -= 1
        url, tail = raw[:cut], raw[cut:]
        try:
            return mask_url_secrets(url) + tail
        except Exception:
            return MASKED
    return URL_IN_TEXT.sub(_one, str(text))


def apply_system_proxy() -> dict:
    """Detect the OS system proxy and export it into HTTP_PROXY/HTTPS_PROXY/
    NO_PROXY if those env vars aren't already set. Returns the env vars that
    were applied (may be empty).

    - On Windows urllib.request.getproxies() reads the registry, which is
      where Clash/V2Ray/etc. write the system proxy.
    - On macOS it reads scutil --proxy.
    - On Linux it just reads env vars (so this becomes a no-op).
    """
    try:
        proxies = urllib.request.getproxies() or {}
    except Exception as e:
        logger.warning(f"Failed to read system proxy: {e}")
        return {}

    applied = {}
    for scheme in ('http', 'https'):
        # 'all' 只作 http/https 缺失时的回退：aiohttp(trust_env=True) 不读
        # ALL_PROXY，直接导出 ALL_PROXY 是死写入。
        proxy = proxies.get(scheme) or proxies.get('all')
        if not proxy:
            continue
        env_key = f'{scheme.upper()}_PROXY'
        if os.environ.get(env_key):
            # User explicitly set it — respect that.
            continue
        os.environ[env_key] = proxy
        applied[env_key] = proxy

    # bypass 列表（getproxies() 的 'no' 键，来自 Windows ProxyOverride /
    # macOS scutil / no_proxy 环境变量）：导出为 NO_PROXY，aiohttp
    # trust_env 会读它；不导出的话系统设置里排除的内网地址会被错误走代理。
    bypass = proxies.get('no')
    if bypass and not os.environ.get('NO_PROXY'):
        os.environ['NO_PROXY'] = bypass
        applied['NO_PROXY'] = bypass

    if applied:
        # 代理 URL 可能带 user:pass@ 凭据，掩码后再进日志
        logger.info(
            f"Applied system proxy on {sys.platform}: "
            f"{ {k: mask_url_secrets(v) for k, v in applied.items()} }"
        )
    else:
        logger.debug("No system proxy detected (or env already set).")
    return applied
