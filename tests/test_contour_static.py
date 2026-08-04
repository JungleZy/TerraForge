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
    for mod in ("app", "core.database"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _seed_task(task_id_name="t"):
    """插一条 contour_tasks 行(静态路由先查任务存在性再发文件),返回 task_id。"""
    from core.database import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO contour_tasks (name, status, north, south, east, west,"
            " contour_interval, zoom_min, zoom_max)"
            " VALUES (?, 'completed', 1, 0, 1, 0, 50, 12, 12)",
            (task_id_name,),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_serve_contour_tile(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    task_id = _seed_task()
    tile = tmp_path / "downloads" / "dem" / f"contour_task_{task_id}" / "contour_tiles" / "12" / "5" / "6.png"
    tile.parent.mkdir(parents=True, exist_ok=True)
    tile.write_bytes(b"\x89PNG\r\n\x1a\n")

    resp = client.get(f"/contour/{task_id}/12/5/6.png")
    assert resp.status_code == 200
    assert resp.data.startswith(b"\x89PNG")


def test_unknown_task_404_even_if_file_on_disk(monkeypatch, tmp_path):
    # 与 tiles_static/terrain_static 一致:任务行不存在直接 404,
    # 即使磁盘上恰好有同 id 目录的瓦片
    app_mod, client = _load_app(monkeypatch, tmp_path)
    tile = tmp_path / "downloads" / "dem" / "contour_task_999" / "contour_tiles" / "12" / "5" / "6.png"
    tile.parent.mkdir(parents=True, exist_ok=True)
    tile.write_bytes(b"\x89PNG\r\n\x1a\n")

    resp = client.get("/contour/999/12/5/6.png")
    assert resp.status_code == 404


def test_missing_tile_404(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    task_id = _seed_task()
    resp = client.get(f"/contour/{task_id}/0/0/0.png")
    assert resp.status_code == 404


def test_path_traversal_blocked(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    task_id = _seed_task()
    resp = client.get(f"/contour/{task_id}/..%2f..%2f..%2fetc%2fpasswd")
    assert resp.status_code in (400, 404)
