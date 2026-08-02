"""
C3 fix: `.terrain` tiles are written gzip-compressed to disk
(cesiumlab_terrain.py uses gzip.open), but the static terrain routes served
them with a bare send_file and no Content-Encoding, so browsers delivered the
raw gzip bytes to Cesium instead of decompressing them.

The routes must detect the gzip magic (1f 8b) and answer with
Content-Encoding: gzip, on all three terrain static routes:
  /terrain/base/<path>, /terrain/dem/<task_id>/<path>, /terrain/local/<id>/<path>
Plain files (e.g. layer.json) must NOT get the header.
"""

import gzip
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

GZ_PAYLOAD = gzip.compress(b"fake-quantized-mesh")


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
    return app_mod, app_mod.app.test_client()


def _write(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_base_route_gzip_tile_has_content_encoding(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    # ConfigManager default for terrain_global_base_path is ./downloads/terrain/base_z8
    base = tmp_path / "downloads" / "terrain" / "base_z8"
    _write(base / "0" / "0" / "0.terrain", GZ_PAYLOAD)
    _write(base / "layer.json", b'{"ok":true}')

    resp = client.get("/terrain/base/0/0/0.terrain")
    assert resp.status_code == 200
    assert resp.headers.get("Content-Encoding") == "gzip"
    assert resp.data == GZ_PAYLOAD

    plain = client.get("/terrain/base/layer.json")
    assert plain.status_code == 200
    assert plain.headers.get("Content-Encoding") is None


def test_dem_route_gzip_tile_has_content_encoding(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    # /terrain/dem 按 dem_tasks.output_path 解析并要求任务行存在(与
    # tiles/contour/local 三路一致):先插行,路径指向默认 downloads/dem。
    db = importlib.import_module("core.database")
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks (name, status, north, south, east, west, dataset, output_path)
            VALUES ('dem', 'completed', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?)
            """,
            (str(tmp_path / "downloads" / "dem"),),
        )
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    tiles = tmp_path / "downloads" / "dem" / f"dem_task_{task_id}" / "terrain_tiles"
    _write(tiles / "0" / "0" / "0.terrain", GZ_PAYLOAD)

    resp = client.get(f"/terrain/dem/{task_id}/0/0/0.terrain")
    assert resp.status_code == 200
    assert resp.headers.get("Content-Encoding") == "gzip"
    assert resp.data == GZ_PAYLOAD


def test_local_route_gzip_tile_has_content_encoding(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("core.database")

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO local_terrain_tasks
              (name, status, output_path, source_dir, output_dir, maxzoom)
            VALUES ('lt', 'completed', '', '', '', 14)
            """
        )
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    tiles = tmp_path / "downloads" / "terrain" / f"local_task_{task_id}" / "terrain_tiles"
    _write(tiles / "0" / "0" / "0.terrain", GZ_PAYLOAD)
    _write(tiles / "layer.json", b'{"ok":true}')

    resp = client.get(f"/terrain/local/{task_id}/0/0/0.terrain")
    assert resp.status_code == 200
    assert resp.headers.get("Content-Encoding") == "gzip"
    assert resp.data == GZ_PAYLOAD

    plain = client.get(f"/terrain/local/{task_id}/layer.json")
    assert plain.status_code == 200
    assert plain.headers.get("Content-Encoding") is None
