"""Minor: 签名 URL 的 query（含凭据/签名）不能进日志或 DB。

- earthdata_client 的异常消息中 URL 必须剥掉 query；
- dem_download_engine 的 last_err（进 dem_files.error_message）与日志必须脱敏。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_engine_redact_strips_url_query():
    from src.services.dem_download_engine import _redact_url_query

    msg = "Download HTTP 500 for https://x.cloudfront.net/f.tif?Signature=abc&Key-Pair-Id=def"
    out = _redact_url_query(msg)
    assert "Signature=abc" not in out
    assert "Key-Pair-Id=def" not in out
    assert "https://x.cloudfront.net/f.tif" in out


def test_engine_redact_leaves_plain_messages_untouched():
    from src.services.dem_download_engine import _redact_url_query

    assert _redact_url_query("Download HTTP 404") == "Download HTTP 404"


class _FakeResp:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url, allow_redirects=True, proxy=None, auth=None):
        return self._resp


def test_earthdata_error_message_redacts_url_query():
    from src.services.earthdata_client import EarthdataClient

    client = EarthdataClient(username="u", password="p")
    session = _FakeSession(_FakeResp(status=500))

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(client.get_signed_url(
            session=session,
            file_url="https://data.lpdaac.earthdatacloud.nasa.gov/x/f.tif?token=secret",
        ))

    assert "token=secret" not in str(exc_info.value), (
        f"异常消息会进引擎日志和 dem_files.error_message，必须剥掉 query: {exc_info.value}"
    )
