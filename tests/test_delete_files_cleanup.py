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


def test_delete_map_task_outside_downloads_dir_removes_files(monkeypatch, tmp_path):
    """0.2.4 护栏放开:DOWNLOADS_DIR 之外的注册任务目录,delete_files=true 也删
    (护栏:非符号链接、深度足够、非根目录/家目录/cache)"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere"
    db = importlib.import_module("core.database")
    task_id = _seed_map_task(db, output_path=outside)
    artifact = _make_artifact(outside / f"task_{task_id}")

    resp = client.delete(f"/api/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200
    assert not artifact.exists(), "注册任务目录即使在 DOWNLOADS_DIR 之外,delete_files=true 也应删除"
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


def test_delete_dem_task_outside_downloads_dir_removes_files(monkeypatch, tmp_path):
    """0.2.4 护栏放开:DOWNLOADS_DIR 之外的注册任务目录,delete_files 也删"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere"
    db = importlib.import_module("core.database")
    task_id = _seed_dem_task(db, output_path=outside)
    artifact = _make_artifact(outside / f"dem_task_{task_id}")

    resp = client.delete(f"/api/dem/tasks/{task_id}?delete_files=1")

    assert resp.status_code == 200
    assert not artifact.exists(), "注册任务目录即使在 DOWNLOADS_DIR 之外,delete_files=true 也应删除"
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


def test_delete_contour_task_outside_downloads_dir_removes_files(monkeypatch, tmp_path):
    """0.2.4 护栏放开:DOWNLOADS_DIR 之外的注册任务目录,delete_files 也删"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere"
    db = importlib.import_module("core.database")
    task_id = _seed_contour_task(db, output_path=outside)
    artifact = _make_artifact(outside / f"contour_task_{task_id}")

    resp = client.delete(f"/api/contour/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200
    assert not artifact.exists(), "注册任务目录即使在 DOWNLOADS_DIR 之外,delete_files=true 也应删除"
    assert _task_row(db, "contour_tasks", task_id) is None, "DB 记录仍应删除"


def test_delete_map_task_legacy_relative_output_path_removes_artifacts(monkeypatch, tmp_path):
    """存量行的相对 output_path:delete_files=true 时必须归一化后删除产物。

    旧版本只校验不改写,库里存的是相对原始值;删除路径按进程 CWD resolve,
    CWD≠BASE_DIR 时会误判越界拒删,接口却已返回 success(删了个寂寞)。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from core import config
    db = importlib.import_module("core.database")
    # 存量行:相对路径原始值(旧版本 create_task 入库的形态)
    task_id = _seed_map_task(db, output_path="legacy_out")
    artifact = _make_artifact(
        Path(config.Config.DOWNLOADS_DIR) / "legacy_out" / f"task_{task_id}"
    )

    resp = client.delete(f"/api/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200
    assert not artifact.exists(), (
        "相对路径存量行必须相对 DOWNLOADS_DIR 归一化后再删产物,不能按进程 CWD"
    )
    assert _task_row(db, "tasks", task_id) is None


# ---------------------------------------------------------------------------
# remove_task_dir_if_safe 护栏（0.2.4 全盘放开后重定的边界）
# ---------------------------------------------------------------------------


def _cleanup_mod(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    import services.task_cleanup as tc
    return tc


def test_cleanup_allows_registered_dir_outside_downloads(monkeypatch, tmp_path):
    tc = _cleanup_mod(monkeypatch, tmp_path)
    artifact = tmp_path / "elsewhere" / "task_1"
    artifact.mkdir(parents=True)
    (artifact / "f.txt").write_text("x")

    assert tc.remove_task_dir_if_safe(artifact) is True
    assert not artifact.exists()


def test_cleanup_refuses_symlink_component(monkeypatch, tmp_path):
    tc = _cleanup_mod(monkeypatch, tmp_path)
    real = tmp_path / "real" / "task_1"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path / "real", target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        import pytest
        pytest.skip(f"无法创建符号链接: {e}")

    assert tc.remove_task_dir_if_safe(link / "task_1") is False
    assert real.exists(), "符号链接路径必须拒删,真实目录不能被动到"


def test_cleanup_refuses_shallow_and_home(monkeypatch, tmp_path):
    import pytest
    tc = _cleanup_mod(monkeypatch, tmp_path)
    assert tc.remove_task_dir_if_safe(Path(os.path.abspath(os.sep))) is False
    assert tc.remove_task_dir_if_safe(Path.home()) is False


def test_cleanup_refuses_cache_and_downloads_root(monkeypatch, tmp_path):
    tc = _cleanup_mod(monkeypatch, tmp_path)
    assert tc.remove_task_dir_if_safe(tmp_path / "downloads") is False
    assert tc.remove_task_dir_if_safe(tmp_path / "cache") is False
    assert tc.remove_task_dir_if_safe(tmp_path) is False, "包含 cache 的上级目录也拒删"
