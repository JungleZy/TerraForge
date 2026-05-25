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

logger = logging.getLogger(__name__)


def apply_system_proxy() -> dict:
    """Detect the OS system proxy and export it into HTTP_PROXY/HTTPS_PROXY
    if those env vars aren't already set. Returns the proxies dict that was
    applied (may be empty).

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
    for scheme in ('http', 'https', 'all'):
        if scheme not in proxies:
            continue
        env_key = f'{scheme.upper()}_PROXY'
        if os.environ.get(env_key):
            # User explicitly set it — respect that.
            continue
        os.environ[env_key] = proxies[scheme]
        applied[env_key] = proxies[scheme]

    if applied:
        logger.info(f"Applied system proxy on {sys.platform}: {applied}")
    else:
        logger.info("No system proxy detected (or env already set).")
    return applied
