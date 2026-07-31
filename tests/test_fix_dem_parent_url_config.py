"""M20: layer.json 的 parentUrl 不再写死 localhost:5000，走配置键。

新增配置键 terrain_base_parent_url；未配置时保持旧默认值
"http://localhost:5000/terrain/base/layer.json"（现状兼容），
配置后 start_tiling 入库的 parent_url 用配置值（非 5000 端口/反代部署可用）。
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

DEFAULT_PARENT_URL = "http://localhost:5000/terrain/base/layer.json"


def _setup(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database", "services.dem_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("core.database")
    db.init_database()
    dtm = importlib.import_module("services.dem_task_manager")
    return db, dtm


def _seed_completed_task(db, output_path):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path, total_files)
            VALUES ('t', 'completed', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 1)
            """,
            (str(output_path),),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _no_op_tiling(monkeypatch, dtm):
    monkeypatch.setattr(
        dtm.DemTaskManager, "_run_tiling_job",
        lambda self, task_id, task_dir, output_dir, maxzoom, parent_url: None,
    )


def test_parent_url_defaults_to_localhost(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    _no_op_tiling(monkeypatch, dtm)
    task_id = _seed_completed_task(db, tmp_path / "out")

    mgr.start_tiling(task_id)

    assert mgr.get_tiling_job(task_id)["parent_url"] == DEFAULT_PARENT_URL


def test_parent_url_uses_config_value(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    _no_op_tiling(monkeypatch, dtm)
    task_id = _seed_completed_task(db, tmp_path / "out")

    custom = "http://terrain.internal:9000/terrain/base/layer.json"
    mgr.config.set("terrain_base_parent_url", custom)

    mgr.start_tiling(task_id)

    assert mgr.get_tiling_job(task_id)["parent_url"] == custom
