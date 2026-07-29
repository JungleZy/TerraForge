"""地图瓦片任务的历史预览静态路由（/tiles/<task_id>/<z>/<x>/<y>.png）。"""

import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "database", "services.task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _make_task_with_tile(tmp_path):
    """tasks 表插一行（output_path 指向 tmp 的 downloads/map），并在
    <output_path>/task_1/10/757/380.png 放一张瓦片。"""
    from database import get_connection
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO tasks (
                name, status, north, south, east, west, zoom_min, zoom_max,
                style, output_format, output_path,
                total_tiles, downloaded_tiles, failed_tiles, created_at
            ) VALUES ('t3', 'completed', 40, 39, 117, 116, 10, 12,
                      'm', 'both', './downloads/map', 3, 3, 0, '2026-01-01')
            """
        )
        conn.commit()
    finally:
        conn.close()
    tile = tmp_path / "downloads" / "map" / "task_1" / "10" / "757" / "380.png"
    tile.parent.mkdir(parents=True, exist_ok=True)
    tile.write_bytes(b"\x89PNG\r\n\x1a\n")


def test_serve_map_task_tile(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    _make_task_with_tile(tmp_path)
    resp = client.get("/tiles/1/10/757/380.png")
    assert resp.status_code == 200
    assert resp.data.startswith(b"\x89PNG")


def test_missing_task_404(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/tiles/999/10/757/380.png")
    assert resp.status_code == 404


def test_missing_tile_404(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    _make_task_with_tile(tmp_path)
    resp = client.get("/tiles/1/10/757/999.png")
    assert resp.status_code == 404


def test_path_traversal_blocked(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    _make_task_with_tile(tmp_path)
    resp = client.get("/tiles/1/..%2f..%2f..%2fetc%2fpasswd")
    assert resp.status_code in (400, 404)
