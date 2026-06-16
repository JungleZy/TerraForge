import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.dem_download_engine import DemDownloadEngine

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


def test_dataset_base_url_unknown_raises():
    with pytest.raises(ValueError):
        DemDownloadEngine._dataset_base_url(None, "FOO.001")
