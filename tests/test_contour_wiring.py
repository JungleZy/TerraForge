import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database"):
        sys.modules.pop(mod, None)
    return importlib.import_module("app")


def test_contour_routes_registered(monkeypatch, tmp_path):
    app_mod = _load_app(monkeypatch, tmp_path)
    rules = {r.rule for r in app_mod.app.url_map.iter_rules()}
    assert "/api/contour/tasks" in rules
    assert any(r.startswith("/contour/") for r in rules)
    assert app_mod.contour_task_manager is not None
