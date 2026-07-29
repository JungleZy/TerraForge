"""
Tests for services/earthdata_client.py (review I21 — previously zero coverage
for the only module that handles credentials).

Network layer (aiohttp) is fully faked: FakeSession.get returns canned
async-context-manager responses, so no real HTTP happens.
"""

import asyncio
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.earthdata_client import EarthdataClient  # noqa: E402

USERNAME = "test-user"
PASSWORD = "s3cret-password-do-not-leak"
FILE_URL = "https://data.lpdaac.earthdatacloud.nasa.gov/protected/file.hdf"
URS_URL = "https://urs.earthdata.nasa.gov/oauth/authorize?client_id=x"
SIGNED_URL = "https://abc123.cloudfront.net/file.hdf?Signature=signed"
LOGIN_CB = "https://data.lpdaac.earthdatacloud.nasa.gov/login?code=authcode"


class FakeResp:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Pops one canned response per get() call; records every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # list of (url, kwargs)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert self._responses, f"unexpected extra GET {url}"
        return self._responses.pop(0)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_direct_303_returns_signed_url():
    session = FakeSession([FakeResp(303, {"Location": SIGNED_URL})])
    client = EarthdataClient(USERNAME, PASSWORD)
    assert run(client.get_signed_url(session, FILE_URL)) == SIGNED_URL


def test_urs_oauth_redirect_flow():
    """302 -> URS authorize (with BasicAuth) -> login callback -> 303 signed."""
    session = FakeSession([
        FakeResp(302, {"Location": URS_URL}),          # initial request
        FakeResp(302, {"Location": LOGIN_CB}),          # URS authorize w/ auth
        FakeResp(200),                                   # login callback (cookies)
        FakeResp(303, {"Location": SIGNED_URL}),         # re-request file
    ])
    client = EarthdataClient(USERNAME, PASSWORD)
    assert run(client.get_signed_url(session, FILE_URL)) == SIGNED_URL

    # Credentials (BasicAuth) must be sent ONLY to the URS authorize URL.
    authorize_calls = [kw for url, kw in session.calls if url == URS_URL]
    assert len(authorize_calls) == 1 and authorize_calls[0].get("auth") is not None
    for url, kw in session.calls:
        if url != URS_URL:
            assert kw.get("auth") is None


def test_urs_redirect_without_credentials_raises():
    session = FakeSession([FakeResp(302, {"Location": URS_URL})])
    client = EarthdataClient("", "")
    with pytest.raises(RuntimeError, match="Missing Earthdata credentials"):
        run(client.get_signed_url(session, FILE_URL))


def test_401_raises_unauthorized():
    session = FakeSession([FakeResp(401)])
    client = EarthdataClient(USERNAME, PASSWORD)
    with pytest.raises(RuntimeError, match="401"):
        run(client.get_signed_url(session, FILE_URL))


def test_redirect_without_location_raises():
    session = FakeSession([FakeResp(302, {})])
    client = EarthdataClient(USERNAME, PASSWORD)
    with pytest.raises(RuntimeError, match="without Location"):
        run(client.get_signed_url(session, FILE_URL))


def test_credentials_never_appear_in_logs_or_errors(caplog):
    """Password must not leak into log records or exception messages."""
    scenarios = [
        ([FakeResp(401)], EarthdataClient(USERNAME, PASSWORD)),
        ([FakeResp(302, {})], EarthdataClient(USERNAME, PASSWORD)),
        ([FakeResp(302, {"Location": URS_URL})], EarthdataClient("", "")),
        ([FakeResp(500)], EarthdataClient(USERNAME, PASSWORD)),
    ]
    for responses, client in scenarios:
        session = FakeSession(responses)
        with caplog.at_level(logging.DEBUG, logger="services.earthdata_client"):
            try:
                run(client.get_signed_url(session, FILE_URL))
            except RuntimeError as e:
                assert PASSWORD not in str(e)
                assert USERNAME not in str(e)
    assert PASSWORD not in caplog.text
    assert USERNAME not in caplog.text
