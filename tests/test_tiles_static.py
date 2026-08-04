"""地图瓦片任务的历史预览静态路由（/tiles/<task_id>/<z>/<x>/<y>.png）。"""

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database", "src.services.task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _make_task_with_tile(tmp_path):
    """tasks 表插一行（output_path 指向 tmp 的 downloads/map），并在
    <output_path>/task_1/10/757/380.png 放一张瓦片。"""
    from src.core.database import get_connection
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
    task_dir = tmp_path / "downloads" / "map" / "task_1"
    tile = task_dir / "10" / "757" / "380.png"
    tile.parent.mkdir(parents=True, exist_ok=True)
    tile.write_bytes(b"\x89PNG\r\n\x1a\n")
    return task_dir


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
    """U13：穿越必须被守卫【正向】拦下，而不是碰巧因为目标不存在而 404。

    旧断言是 `status_code in (400, 404)` —— 而 404 恰恰是这条路径的默认结果。
    实测：把 src/routes/terrain_static.py 里的 `_is_within` 守卫拆掉，
    `/tiles/1/..%2f..%2f..%2fetc%2fpasswd` 照旧返回 404，用例照旧通过；而同一
    拆守卫状态下 `/tiles/1/..%2fcanary.txt` 能拿到 **200 + 明文内容**。守卫本身
    是有效的，空洞的是这条断言。（`test_fix_terrain_traversal.py` 已经重写过
    terrain 链路的同款空洞，tiles 被漏下了。）

    改法照那份范本：在任务目录**之外**放 canary，断言拿不到它。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    task_dir = _make_task_with_tile(tmp_path)

    # canary 放在任务目录的兄弟位置：守卫失效时 `..%2fcanary.txt` 正好命中它
    canary = task_dir.parent / "canary.txt"
    canary.write_text("CANARY-LEAK", encoding="utf-8")

    resp = client.get("/tiles/1/..%2fcanary.txt")
    assert resp.status_code == 400, (
        f"穿越到任务目录之外必须被守卫拒为 400，实际 {resp.status_code}")
    assert b"CANARY-LEAK" not in resp.data, "穿越读到了任务目录之外的文件"

    # 经典 /etc 目标：即使 /etc/passwd 真实存在也不得可达
    resp = client.get("/tiles/1/..%2f..%2f..%2fetc%2fpasswd")
    assert resp.status_code in (400, 404)
    assert b"root:" not in resp.data


def test_resolve_safe_file_rejects_traversal_with_400(monkeypatch, tmp_path):
    """直调 _resolve_safe_file 断言 HTTPException.code == 400 —— 不经 HTTP 层，
    排除「404 来自别的原因」这种可能性。"""
    from werkzeug.exceptions import HTTPException
    from src.routes.tiles_static import _resolve_safe_file

    app_mod, _ = _load_app(monkeypatch, tmp_path)
    with app_mod.app.test_request_context("/"):
        with pytest.raises(HTTPException) as excinfo:
            _resolve_safe_file(tmp_path / "downloads" / "task_1", "../../outside")
    assert excinfo.value.code == 400
