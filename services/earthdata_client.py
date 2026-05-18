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
                    raise RuntimeError(f"Redirect ({resp.status}) without Location header for {file_url}")

                # If we already got the signed URL, return it
                if resp.status == 303 and "cloudfront.net" in loc:
                    return loc

                # If redirected to URS, do login flow
                if "urs.earthdata.nasa.gov" in loc:
                    return await self._login_and_resolve(session=session, file_url=file_url, authorize_url=loc)

                # Some intermediate redirects (rare): follow one step and retry.
                async with session.get(loc, allow_redirects=False, proxy=self.proxy_url or None) as resp2:
                    loc2 = resp2.headers.get("Location") or resp2.headers.get("location")
                    if resp2.status == 303 and loc2 and "cloudfront.net" in loc2:
                        return loc2
                    raise RuntimeError(f"Unexpected redirect chain while resolving signed URL for {file_url}: {resp.status}->{resp2.status}")

            if resp.status == 200:
                # Not expected for LP DAAC protected URL, but handle it.
                return file_url

            if resp.status == 401:
                raise RuntimeError("Earthdata 401 Unauthorized (check username/password)")

            raise RuntimeError(f"Unexpected response while resolving signed URL for {file_url}: HTTP {resp.status}")

    async def _login_and_resolve(self, session: aiohttp.ClientSession, file_url: str, authorize_url: str) -> str:
        auth = self._auth()
        if not auth:
            raise RuntimeError("Missing Earthdata credentials (earthdata_username/earthdata_password)")

        # Step 1: hit authorize URL with BasicAuth, expect redirect back to data.lpdaac.../login?code=...
        async with session.get(
            authorize_url,
            allow_redirects=False,
            auth=auth,
            proxy=self.proxy_url or None,
        ) as resp:
            if resp.status not in (301, 302, 303, 307, 308):
                if resp.status == 401:
                    raise RuntimeError("Earthdata 401 Unauthorized (check username/password)")
                raise RuntimeError(f"Unexpected URS authorize response: HTTP {resp.status}")

            loc = resp.headers.get("Location") or resp.headers.get("location")
            if not loc:
                raise RuntimeError("URS authorize redirect missing Location header")

        # Step 2: follow the redirect to LP DAAC /login with the code. This sets cookies in the session jar.
        async with session.get(loc, allow_redirects=True, proxy=self.proxy_url or None) as _:
            pass

        # Step 3: request the original file URL again; should now yield 303 with signed URL.
        async with session.get(file_url, allow_redirects=False, proxy=self.proxy_url or None) as resp2:
            if resp2.status in (301, 302, 303):
                loc2 = resp2.headers.get("Location") or resp2.headers.get("location")
                if resp2.status == 303 and loc2 and "cloudfront.net" in loc2:
                    return loc2
            if resp2.status == 401:
                raise RuntimeError("Earthdata auth loop: still unauthorized after login")
            raise RuntimeError(f"Failed to resolve signed URL after login: HTTP {resp2.status}")

