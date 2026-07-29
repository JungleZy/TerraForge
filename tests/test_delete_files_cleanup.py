import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in (
        "app",
        "core.database",
        "models.task",
        "services.config_manager",
        "services.task_manager",
        "services.dem_task_manager",
        "services.contour_task_manager",
        "routes.api",
        "routes.dem_api",
        "routes.contour_api",
    ):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _seed_map_task(db, output_path, status="completed"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles,
               downloaded_tiles, failed_tiles)
            VALUES ('map-task', ?, 1, 0, 1, 0, 0, 0, 'satellite',
                    'tiles_only', ?, 0, 0, 0)
            """,
            (status, str(output_path)),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _seed_dem_task(db, output_path, status="completed"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('dem-task', ?, 1, 0, 1, 0, 'ASTGTM.003', ?, 0, 0, 0)
            """,
            (status, str(output_path)),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _seed_contour_task(db, output_path, status="completed"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO contour_tasks
              (name, status, north, south, east, west, dataset,
               contour_interval, zoom_min, zoom_max, output_path, total_files)
            VALUES ('contour-task', ?, 1, 0, 1, 0, 'ASTGTM.003',
                    50, 12, 14, ?, 0)
            """,
            (status, str(output_path)),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _make_artifact(dir_path):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "artifact.bin").write_bytes(b"payload")
    return dir_path


def _task_row(db, table, task_id):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {table} WHERE id=?", (task_id,))
        return cur.fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DELETE ...?delete_files=true：删除任务时可选清理磁盘产物
#
# 安全边界：只删 resolve 后严格位于 Config.DOWNLOADS_DIR 之内的任务目录；
# 产物落在 DOWNLOADS_DIR 之外（用户自定义 output_path）时跳过文件删除、
# 但 DB 记录照常删除。共享瓦片缓存 cache/ 绝不可删。
# ---------------------------------------------------------------------------


def test_delete_map_task_with_delete_files_removes_artifacts(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from core import config
    db = importlib.import_module("core.database")
    task_id = _seed_map_task(db, output_path=config.Config.DOWNLOADS_DIR)
    artifact = _make_artifact(Path(config.Config.DOWNLOADS_DIR) / f"task_{task_id}")

    resp = client.delete(f"/api/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200
    assert not artifact.exists(), "delete_files=true 应删除产物目录"
    assert _task_row(db, "tasks", task_id) is None


def test_delete_map_task_default_keeps_artifacts(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from core import config
    db = importlib.import_module("core.database")
    task_id = _seed_map_task(db, output_path=config.Config.DOWNLOADS_DIR)
    artifact = _make_artifact(Path(config.Config.DOWNLOADS_DIR) / f"task_{task_id}")

    resp = client.delete(f"/api/tasks/{task_id}")

    assert resp.status_code == 200
    assert artifact.exists(), "缺省 delete_files=false 必须保留产物目录"
    assert _task_row(db, "tasks", task_id) is None


def test_delete_map_task_outside_downloads_dir_keeps_files(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere"
    db = importlib.import_module("core.database")
    task_id = _seed_map_task(db, output_path=outside)
    artifact = _make_artifact(outside / f"task_{task_id}")

    resp = client.delete(f"/api/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200
    assert artifact.exists(), "产物在 DOWNLOADS_DIR 之外时不得删文件"
    assert _task_row(db, "tasks", task_id) is None, "DB 记录仍应删除"


def test_delete_dem_task_with_delete_files_removes_artifacts(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from core import config
    db = importlib.import_module("core.database")
    dem_root = Path(config.Config.DOWNLOADS_DIR) / "dem"
    task_id = _seed_dem_task(db, output_path=dem_root)
    artifact = _make_artifact(dem_root / f"dem_task_{task_id}")

    resp = client.delete(f"/api/dem/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200
    assert not artifact.exists(), "delete_files=true 应删除产物目录"
    assert _task_row(db, "dem_tasks", task_id) is None


def test_delete_dem_task_default_keeps_artifacts(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from core import config
    db = importlib.import_module("core.database")
    dem_root = Path(config.Config.DOWNLOADS_DIR) / "dem"
    task_id = _seed_dem_task(db, output_path=dem_root)
    artifact = _make_artifact(dem_root / f"dem_task_{task_id}")

    resp = client.delete(f"/api/dem/tasks/{task_id}")

    assert resp.status_code == 200
    assert artifact.exists(), "缺省 delete_files=false 必须保留产物目录"
    assert _task_row(db, "dem_tasks", task_id) is None


def test_delete_dem_task_outside_downloads_dir_keeps_files(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere"
    db = importlib.import_module("core.database")
    task_id = _seed_dem_task(db, output_path=outside)
    artifact = _make_artifact(outside / f"dem_task_{task_id}")

    resp = client.delete(f"/api/dem/tasks/{task_id}?delete_files=1")

    assert resp.status_code == 200
    assert artifact.exists(), "产物在 DOWNLOADS_DIR 之外时不得删文件"
    assert _task_row(db, "dem_tasks", task_id) is None, "DB 记录仍应删除"


def test_delete_contour_task_with_delete_files_removes_artifacts(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from core import config
    db = importlib.import_module("core.database")
    dem_root = Path(config.Config.DOWNLOADS_DIR) / "dem"
    task_id = _seed_contour_task(db, output_path=dem_root)
    artifact = _make_artifact(dem_root / f"contour_task_{task_id}")

    resp = client.delete(f"/api/contour/tasks/{task_id}?delete_files=yes")

    assert resp.status_code == 200
    assert not artifact.exists(), "delete_files=true 应删除产物目录"
    assert _task_row(db, "contour_tasks", task_id) is None


def test_delete_contour_task_default_keeps_artifacts(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from core import config
    db = importlib.import_module("core.database")
    dem_root = Path(config.Config.DOWNLOADS_DIR) / "dem"
    task_id = _seed_contour_task(db, output_path=dem_root)
    artifact = _make_artifact(dem_root / f"contour_task_{task_id}")

    resp = client.delete(f"/api/contour/tasks/{task_id}")

    assert resp.status_code == 200
    assert artifact.exists(), "缺省 delete_files=false 必须保留产物目录"
    assert _task_row(db, "contour_tasks", task_id) is None


def test_delete_contour_task_outside_downloads_dir_keeps_files(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere"
    db = importlib.import_module("core.database")
    task_id = _seed_contour_task(db, output_path=outside)
    artifact = _make_artifact(outside / f"contour_task_{task_id}")

    resp = client.delete(f"/api/contour/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200
    assert artifact.exists(), "产物在 DOWNLOADS_DIR 之外时不得删文件"
    assert _task_row(db, "contour_tasks", task_id) is None, "DB 记录仍应删除"
