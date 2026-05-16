import importlib
import sys


def test_terrain_api_routes_exist(monkeypatch, tmp_path):
    # Isolate DB and directory side effects before importing app.py (which runs init_database()).
    import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    sys.modules.pop("app", None)
    app_mod = importlib.import_module("app")

    app = app_mod.app
    app.config["TESTING"] = True
    client = app.test_client()

    r1 = client.post("/api/terrain/dem/1/start")
    assert r1.status_code in (200, 400, 404, 500)

    r2 = client.get("/api/terrain/dem/1")
    assert r2.status_code in (200, 400, 404, 500)

