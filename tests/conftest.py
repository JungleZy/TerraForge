"""
Shared pytest fixtures for the test suite.

Review I21: the 38 test files each duplicate the same sys.path.insert +
Config monkey-patch + module-pop dance. This conftest provides a reusable
fixture for NEW tests. It is intentionally NOT autouse: existing test files
keep their own isolation logic and their behavior must not change.

Importing this module also guarantees the project root is on sys.path before
any test module is collected (some existing files rely on another test's
sys.path.insert having run first — fragile when tests run in isolation).
"""

import importlib
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def isolated_app(monkeypatch, tmp_path):
    """
    Import (fresh) the Flask app with all Config side effects redirected to
    tmp_path: DATABASE_PATH / DOWNLOADS_DIR / OUTPUT_DIR / CACHE_DIR.

    Returns the imported `app` module (app_mod.app is the Flask instance,
    already in TESTING mode). Modules that bind Config at import time
    ("app", "database") are popped first so the re-import picks up the
    patched values.
    """
    import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in ("app", "database"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod
