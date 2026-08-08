"""
Earthdata Login (URS) helper for downloading LP DAAC Earthdata Cloud protected files.

This is intentionally pragmatic: it follows the same redirect chain we validated with curl.
Security hardening is out of scope per project instructions.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)


class EarthdataAuthError(RuntimeError):
    """不可重试的认证失败（401 / 缺凭据 / 登录后仍 401）。

    与可重试的网络/5xx 错误区分开:坏凭据重试多少次都是必败,引擎捕获后
    直接判颗粒失败,不进指数退避（见 dem_download_engine）。
    """


def _redact_url(url: str) -> str:
    """剥掉 URL 的 query —— 签名/授权 URL 的凭据参数不能进异常消息（会落日志/DB）。"""
    return str(url).split("?", 1)[0]


# 只有这个域下的主机才配收到 BasicAuth 明文凭据。
_URS_HOST_SUFFIX = ".earthdata.nasa.gov"


def _is_urs_host(url: str) -> bool:
    """URL 是否指向 Earthdata Login(URS)—— 即「要不要把凭据发给它」。

    此前判据是 `"urs.earthdata.nasa.gov" in loc` 的**子串**匹配,
    `https://attacker.example/cb?next=https://urs.earthdata.nasa.gov/oauth`
    照样通过,随后 BasicAuth 明文凭据就发给了攻击者的主机(前置条件是上游存在
    开放重定向或 TLS 被攻破)。这里按解析出的 hostname 判,子串再也骗不进来。

    要求 https:BasicAuth 在 http 上就是明文口令。
    取域后缀白名单而不是钉死单个主机名:凭据本来就只在 *.earthdata.nasa.gov
    内有效(URS 另有 uat.urs.earthdata.nasa.gov 这类部署),钉死一个主机名会在
    上游换 URS 主机时把流程掐死在含糊的 "Unexpected redirect chain" 上,指错方向。
    """
    parts = urlparse(url)
    if parts.scheme != "https":
        return False
    host = parts.hostname or ""
    return host == "earthdata.nasa.gov" or host.endswith(_URS_HOST_SUFFIX)


class EarthdataClient:
    def __init__(self, username: str, password: str, proxy_url: str = ""):
        self.username = username or ""
        self.password = password or ""
        self.proxy_url = proxy_url or ""

    def _auth(self) -> Optional[aiohttp.BasicAuth]:
        if not self.username or not self.password:
            return None
        return aiohttp.BasicAuth(self.username, self.password)

    async def get_signed_url(self, session: aiohttp.ClientSession, file_url: str) -> str:
        """
        Resolve an LP DAAC protected file URL to a signed CloudFront/S3 URL (303 Location).

        This performs a URS OAuth authorize round-trip if the first request is redirected to URS.
        """
        # First request: expect 302 to URS (if not already logged in) or 303 to CloudFront (if logged in)
        async with session.get(file_url, allow_redirects=False, proxy=self.proxy_url or None) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location") or resp.headers.get("location")
                if not loc:
                    raise RuntimeError(f"Redirect ({resp.status}) without Location header for {_redact_url(file_url)}")

                # 303 + Location 即签名 URL —— 不认 host 白名单:LP DAAC 也
                # 可能签 S3 预签名 URL（非 cloudfront.net）,只认 cloudfront 会
                # 多走一跳中间重定向,甚至误报 "Unexpected redirect chain"。
                if resp.status == 303:
                    return loc

                # If redirected to URS, do login flow
                if _is_urs_host(loc):
                    return await self._login_and_resolve(session=session, file_url=file_url, authorize_url=loc)

                # Some intermediate redirects (rare): follow one step and retry.
                async with session.get(loc, allow_redirects=False, proxy=self.proxy_url or None) as resp2:
                    loc2 = resp2.headers.get("Location") or resp2.headers.get("location")
                    # 同上:303 + Location 即签名 URL,不限定 host。
                    if resp2.status == 303 and loc2:
                        return loc2
                    raise RuntimeError(f"Unexpected redirect chain while resolving signed URL for {_redact_url(file_url)}: {resp.status}->{resp2.status}")

            if resp.status == 200:
                # Not expected for LP DAAC protected URL, but handle it.
                return file_url

            if resp.status == 401:
                raise EarthdataAuthError("Earthdata 401 Unauthorized (check username/password)")

            raise RuntimeError(f"Unexpected response while resolving signed URL for {_redact_url(file_url)}: HTTP {resp.status}")

    async def _login_and_resolve(self, session: aiohttp.ClientSession, file_url: str, authorize_url: str) -> str:
        # 凭据要不要发,由主机名说了算 —— 这道闸放在真正带 auth 发请求的这一侧,
        # 不管调用方是 get_signed_url 还是别处,凭据都不会离开 Earthdata 域。
        if not _is_urs_host(authorize_url):
            raise EarthdataAuthError(
                "Refusing to send Earthdata credentials to non-URS host: "
                f"{_redact_url(authorize_url)}")
        auth = self._auth()
        if not auth:
            # 缺凭据同样不可重试 —— 配置没填,重试不会凭空变出凭据。
            raise EarthdataAuthError("Missing Earthdata credentials (earthdata_username/earthdata_password)")

        # Step 1: hit authorize URL with BasicAuth, expect redirect back to data.lpdaac.../login?code=...
        async with session.get(
            authorize_url,
            allow_redirects=False,
            auth=auth,
            proxy=self.proxy_url or None,
        ) as resp:
            if resp.status not in (301, 302, 303, 307, 308):
                if resp.status == 401:
                    raise EarthdataAuthError("Earthdata 401 Unauthorized (check username/password)")
                raise RuntimeError(f"Unexpected URS authorize response: HTTP {resp.status}")

            loc = resp.headers.get("Location") or resp.headers.get("location")
            if not loc:
                raise RuntimeError("URS authorize redirect missing Location header")

        # Step 2: follow the redirect to LP DAAC /login with the code. This sets cookies in the session jar.
        async with session.get(loc, allow_redirects=True, proxy=self.proxy_url or None) as resp:
            # 不查状态会把登录链失败吞掉（最终表现为 step 3 含糊的 302 循环）。
            if not (200 <= resp.status < 300):
                raise RuntimeError(
                    f"Earthdata login follow-up failed for {_redact_url(file_url)}: HTTP {resp.status}"
                )

        # Step 3: request the original file URL again; should now yield 303 with signed URL.
        async with session.get(file_url, allow_redirects=False, proxy=self.proxy_url or None) as resp2:
            if resp2.status in (301, 302, 303):
                loc2 = resp2.headers.get("Location") or resp2.headers.get("location")
                # 303 + Location 即签名 URL,不限定 host（S3 预签名同样合法）。
                if resp2.status == 303 and loc2:
                    return loc2
            if resp2.status == 401:
                raise EarthdataAuthError("Earthdata auth loop: still unauthorized after login")
            raise RuntimeError(f"Failed to resolve signed URL after login: HTTP {resp2.status}")

