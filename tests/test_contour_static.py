import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _seed_task(task_id_name="t"):
    """插一条 contour_tasks 行(静态路由先查任务存在性再发文件),返回 task_id。"""
    from src.core.database import get_connection
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


def test_delete_task_invalidates_existence_cache(monkeypatch, tmp_path):
    """删任务后同一块瓦片必须立刻 404 —— 即使磁盘文件还在。

    存在性缓存只存正结果，删除时不清就会一直命中：任务记录没了，/contour/<id>/
    却还在照常发瓦片（delete_files 默认 false，文件确实还在）。清缓存的 hook 由
    DELETE 路由传给 ContourTaskManager.delete_task；它没被接上时这条会绿着骗人
    —— 所以第一次 GET 是必需的，它把缓存喂热。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    task_id = _seed_task()
    tile = tmp_path / "downloads" / "dem" / f"contour_task_{task_id}" / "contour_tiles" / "12" / "5" / "6.png"
    tile.parent.mkdir(parents=True, exist_ok=True)
    tile.write_bytes(b"\x89PNG\r\n\x1a\n")

    assert client.get(f"/contour/{task_id}/12/5/6.png").status_code == 200

    assert client.delete(f"/api/contour/tasks/{task_id}").status_code == 200
    assert tile.exists(), (
        "delete_files 默认 false，磁盘瓦片本就该留着 —— 留着才测得出缓存有没有清")

    resp = client.get(f"/contour/{task_id}/12/5/6.png")
    assert resp.status_code == 404, "任务已删，缓存没清，瓦片仍可访问"


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
