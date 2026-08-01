"""
Contract: third-party assets under /static/vendor/ ship with a version number
in their path (e.g. /static/vendor/cesium/1.143.0/Cesium.js), so their content
never changes under a given URL and browsers may cache them immutably.

app.py's after_request hook must answer those responses with
  Cache-Control: public, max-age=31536000, immutable
while business assets that change in place (/static/js/, /static/css/) keep
Flask's default headers (no immutable long caching).
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

VENDOR_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _load_app(monkeypatch, tmp_path):
    from core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in ("app", "core.database"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def test_vendor_assets_get_immutable_cache(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)

    for url in (
        "/static/vendor/cesium/1.143.0/Cesium.js",
        "/static/vendor/bootstrap/5.3.0/bootstrap.min.css",
    ):
        resp = client.get(url)
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == VENDOR_CACHE_CONTROL


def test_business_static_assets_keep_default_headers(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)

    resp = client.get("/static/js/config.js")
    assert resp.status_code == 200
    assert "immutable" not in (resp.headers.get("Cache-Control") or "")
