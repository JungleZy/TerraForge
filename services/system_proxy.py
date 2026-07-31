"""System proxy detection — injects Windows registry / OS-detected proxy
settings into HTTP(S)_PROXY env vars so aiohttp(trust_env=True) can use them.

Why this exists: PowerShell/curl on Windows use WinINET which auto-reads the
"system proxy" registry setting. aiohttp does NOT — even with trust_env=True
it only reads environment variables. End users running our packaged exe in
mainland China typically have Clash/V2Ray configured as a Windows system
proxy (not as env vars), so without this shim every Google Maps tile request
silently times out at 30s.
"""
import logging
import os
import sys
import urllib.request
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)


def mask_url_userinfo(url) -> str:
    """把 URL 里的 user:pass@ 凭据掩码成 ***:***@，host 保留便于排查。

    无法解析或不含 userinfo 时原样返回。只用于日志输出，不影响实际生效的值。
    """
    try:
        parts = urlsplit(str(url))
    except Exception:
        return str(url)
    if '@' not in parts.netloc:
        return str(url)
    host = parts.netloc.rsplit('@', 1)[1]
    return urlunsplit((parts.scheme, f'***:***@{host}', parts.path,
                       parts.query, parts.fragment))


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
            f"{ {k: mask_url_userinfo(v) for k, v in applied.items()} }"
        )
    else:
        logger.debug("No system proxy detected (or env already set).")
    return applied
