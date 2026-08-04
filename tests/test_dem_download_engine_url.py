import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.dem_download_engine import DemDownloadEngine

# _dataset_base_url does not use self, so pass None to avoid constructing
# ConfigManager (which would need a database).


def test_dataset_base_url_astgtm():
    url = DemDownloadEngine._dataset_base_url(None, "ASTGTM.003")
    assert url.endswith("/ASTGTM.003/")
    assert "lp-prod-protected" in url


def test_dataset_base_url_astwbd():
    url = DemDownloadEngine._dataset_base_url(None, "ASTWBD.001")
    assert url.endswith("/ASTWBD.001/")
    assert "lp-prod-protected" in url


def test_dataset_base_url_copernicus_glo30():
    url = DemDownloadEngine._dataset_base_url(None, "COP-DEM-GLO-30")
    assert url == "https://copernicus-dem-30m.s3.amazonaws.com/"


def test_dataset_requires_auth():
    assert DemDownloadEngine._dataset_requires_auth("ASTGTM.003") is True
    assert DemDownloadEngine._dataset_requires_auth("ASTWBD.001") is True
    # Copernicus GLO-30 is a public AWS bucket — no Earthdata signing.
    assert DemDownloadEngine._dataset_requires_auth("COP-DEM-GLO-30") is False


def test_client_timeout_has_no_total_cap():
    # Large DEM COGs must not be killed by a total timeout; only stalls abort.
    t = DemDownloadEngine._client_timeout(30)
    assert t.total is None
    assert t.sock_read == 30
    assert t.sock_connect == 30


def test_dataset_base_url_unknown_raises():
    with pytest.raises(ValueError):
        DemDownloadEngine._dataset_base_url(None, "FOO.001")
