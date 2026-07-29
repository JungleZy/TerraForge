import importlib
import sys


def _load_client(monkeypatch, tmp_path):
    # Isolate DB and directory side effects before importing app.py (which runs init_database()).
    from core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    sys.modules.pop("app", None)
    app_mod = importlib.import_module("app")

    app = app_mod.app
    app.config["TESTING"] = True
    return app.test_client()


def test_terrain_base_serves_existing_layer_json(monkeypatch, tmp_path):
    client = _load_client(monkeypatch, tmp_path)

    # Default terrain_global_base_path is ./downloads/terrain/base_z8, rebased
    # onto Config.DOWNLOADS_DIR.
    base_dir = tmp_path / "downloads" / "terrain" / "base_z8"
    base_dir.mkdir(parents=True)
    (base_dir / "layer.json").write_text('{"tilejson":"2.1.0"}', encoding="utf-8")

    r = client.get("/terrain/base/layer.json")
    assert r.status_code == 200
    assert b'"tilejson"' in r.data


def test_terrain_base_missing_file_returns_404(monkeypatch, tmp_path):
    client = _load_client(monkeypatch, tmp_path)

    r = client.get("/terrain/base/layer.json")
    assert r.status_code == 404
