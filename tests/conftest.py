"""
Shared pytest fixtures for the test suite.

Review I21: many test files each duplicate the same sys.path.insert +
Config monkey-patch + module-pop dance, and a bare `sys.modules.pop(...)`
never restores the popped module — the fresh instance stays registered, so
a later test can import a different module object than the one the Flask
app's blueprints are actually using (module double-instance, test results
depend on execution order). `fresh_import()` below is the unified tool:
pop + re-import + automatic restore via monkeypatch teardown. Existing
test files should migrate their ad-hoc pop loops to it; new tests must use
it (or the `isolated_app` fixture) instead of hand-rolled pop lists.

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


def fresh_import(monkeypatch, *names):
    """
    Pop each module from sys.modules, re-import it, and let monkeypatch
    restore the original sys.modules entries at test teardown
    (monkeypatch.delitem records the evicted module object and puts it
    back on undo), so no double instance leaks into later tests.

    All names are removed before any re-import so the whole set is reloaded
    consistently. Returns the module for a single name, otherwise a list in
    the order given.

    Dotted names ("core.database"): importlib also rebinds the submodule
    attribute on the parent package, which monkeypatch's sys.modules undo
    does NOT cover — without handling it, the parent package keeps pointing
    at the fresh instance after teardown (split-brain: sys.modules and the
    package attribute name different module objects). The pre-import
    `setattr(parent, attr, current_value)` below is a value-no-op whose
    only purpose is to make monkeypatch record the original attribute, so
    teardown restores it after the re-import rebinding.
    """
    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name in names:
        if "." in name:
            parent_name, attr = name.rsplit(".", 1)
            parent = sys.modules.get(parent_name)
            if parent is not None and hasattr(parent, attr):
                monkeypatch.setattr(parent, attr, getattr(parent, attr))
    modules = [importlib.import_module(name) for name in names]
    return modules[0] if len(modules) == 1 else modules


@pytest.fixture
def isolated_app(monkeypatch, tmp_path):
    """
    Import (fresh) the Flask app with all Config side effects redirected to
    tmp_path: DATABASE_PATH / DOWNLOADS_DIR / OUTPUT_DIR / CACHE_DIR.

    Returns the imported `app` module (app_mod.app is the Flask instance,
    already in TESTING mode). Modules that bind Config at import time
    ("app", "core.database") are re-imported via fresh_import so the
    re-import picks up the patched values and the originals are restored
    at teardown.
    """
    from core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    app_mod = fresh_import(monkeypatch, "app", "core.database")[0]
    app_mod.app.config["TESTING"] = True
    return app_mod
