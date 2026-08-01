"""四个任务列表接口的 ?status=active 契约（本轮跨层修复新增）。

契约（与前端组对齐）：
  - 不传 status：行为与修复前完全一致（全量、按 created_at 倒序、limit 生效）；
  - ?status=active：特殊值（同 /api/history_all），只回活动三态
    pending/running/paused。

覆盖 /api/tasks、/api/dem/tasks、/api/terrain/local/tasks、/api/contour/tasks。
风格照 tests/test_history_all_stream.py（Config 副作用重定向 + 新鲜 import app）。
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database", "services.dem_task_manager",
                "services.local_terrain_task_manager", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _seed(cur):
    """每张任务表插一条 running + 一条 completed。"""
    cur.execute(
        "INSERT INTO tasks (name, status, north, south, east, west, "
        "zoom_min, zoom_max, style, output_format, total_tiles, downloaded_tiles, "
        "output_path) VALUES ('m-run','running',1,0,1,0,1,2,'m','png',10,3,'/x')")
    cur.execute(
        "INSERT INTO tasks (name, status, north, south, east, west, "
        "zoom_min, zoom_max, style, output_format, total_tiles, downloaded_tiles, "
        "output_path) VALUES ('m-done','completed',1,0,1,0,1,2,'m','png',10,10,'/x')")
    cur.execute(
        "INSERT INTO dem_tasks (name, status, north, south, east, west, "
        "dataset, total_files, downloaded_files, output_path) "
        "VALUES ('d-run','running',1,0,1,0,'ASTGTM.003',2,1,'/x')")
    cur.execute(
        "INSERT INTO dem_tasks (name, status, north, south, east, west, "
        "dataset, total_files, downloaded_files, output_path) "
        "VALUES ('d-done','completed',1,0,1,0,'ASTGTM.003',2,2,'/x')")
    cur.execute(
        "INSERT INTO local_terrain_tasks (name, status, output_path, "
        "source_dir, output_dir, maxzoom, total_files, uploaded_files) "
        "VALUES ('l-run','running','/x','/src','/out',14,5,3)")
    cur.execute(
        "INSERT INTO local_terrain_tasks (name, status, output_path, "
        "source_dir, output_dir, maxzoom, total_files, uploaded_files) "
        "VALUES ('l-done','completed','/x','/src','/out',14,5,5)")
    cur.execute(
        "INSERT INTO contour_tasks (name, status, north, south, east, west, "
        "contour_interval, zoom_min, zoom_max, total_tiles, rendered_tiles, "
        "output_path) VALUES ('c-run','running',1,0,1,0,50,12,14,8,4,'/x')")
    cur.execute(
        "INSERT INTO contour_tasks (name, status, north, south, east, west, "
        "contour_interval, zoom_min, zoom_max, total_tiles, rendered_tiles, "
        "output_path) VALUES ('c-done','completed',1,0,1,0,50,12,14,8,8,'/x')")


_URLS = (
    "/api/tasks",
    "/api/dem/tasks",
    "/api/terrain/local/tasks",
    "/api/contour/tasks",
)


def test_list_endpoints_status_active_filters_to_three_active_states(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("core.database")
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        _seed(cur)
        conn.commit()
    finally:
        conn.close()

    for url in _URLS:
        # 不传 status：两条都回，行为不变
        resp = client.get(url)
        assert resp.status_code == 200, f"{url} -> {resp.status_code}"
        names = sorted(t["name"] for t in resp.get_json()["tasks"])
        assert len(names) == 2, f"{url} 不传 status 应回全量,实际 {names}"

        # ?status=active：只回 running 那条
        resp = client.get(f"{url}?status=active")
        assert resp.status_code == 200, f"{url}?status=active -> {resp.status_code}"
        body = resp.get_json()
        names = [t["name"] for t in body["tasks"]]
        assert len(names) == 1 and names[0].endswith("-run"), (
            f"{url}?status=active 应只回活动任务,实际 {names}")
        assert body["count"] == 1


# --------------------------------------------------------------------------
# terrain_static 热路径修复的回归钉点：
#   - .terrain 响应带 immutable 长缓存；layer.json 不带（重切片后会变）；
#   - local 任务删除后，存在性缓存必须失效——delete_files=false（磁盘瓦片
#     保留）时已删任务的瓦片不得继续 200。
# --------------------------------------------------------------------------

import gzip  # noqa: E402


def _write(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_terrain_tile_has_immutable_cache_but_layer_json_not(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    base = tmp_path / "downloads" / "terrain" / "base_z8"
    _write(base / "0" / "0" / "0.terrain", gzip.compress(b"fake-quantized-mesh"))
    _write(base / "layer.json", b'{"ok":true}')

    tile = client.get("/terrain/base/0/0/0.terrain")
    assert tile.status_code == 200
    assert tile.headers.get("Cache-Control") == "public, max-age=31536000, immutable"

    layer = client.get("/terrain/base/layer.json")
    assert layer.status_code == 200
    # layer.json 重切片后会变，不给 immutable 长缓存（Flask 默认 no-cache 即可）
    assert "immutable" not in (layer.headers.get("Cache-Control") or "")


def test_local_task_delete_invalidates_existence_cache(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("core.database")

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO local_terrain_tasks (name, status, output_path, "
            "source_dir, output_dir, maxzoom) "
            "VALUES ('lt','completed','/x','/src','/out',14)")
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    tiles = tmp_path / "downloads" / "terrain" / f"local_task_{task_id}" / "terrain_tiles"
    _write(tiles / "0" / "0" / "0.terrain", gzip.compress(b"fake-quantized-mesh"))

    # 第一次访问：200，并把任务 id 填进存在性缓存
    url = f"/terrain/local/{task_id}/0/0/0.terrain"
    assert client.get(url).status_code == 200

    # delete_files=false：磁盘瓦片保留。若缓存不失效，第二次访问仍会 200
    resp = client.delete(f"/api/terrain/local/tasks/{task_id}?delete_files=false")
    assert resp.status_code == 200, resp.get_json()

    assert client.get(url).status_code == 404, (
        "任务删除后存在性缓存未失效，已删任务的瓦片仍在 200")
