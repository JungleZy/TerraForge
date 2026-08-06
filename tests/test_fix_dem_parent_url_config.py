"""M20: layer.json 的 parentUrl 不再写死 localhost:5000，走配置键。

新增配置键 terrain_base_parent_url；未配置时用默认值
"http://localhost:5000/terrain/base"（**目录形式**，2026-08-05 从
".../base/layer.json" 改过来 —— 带 /layer.json 会让 Cesium 整个 provider
降级成 heightmap、高程全错且不报错），
配置后 start_tiling 入库的 parent_url 用配置值（非 5000 端口/反代部署可用）。
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 2026-08-05：默认值去掉了尾部 /layer.json。Cesium 对 parentUrl 做
# appendForwardSlash() 后再拼 layer.json，带着它会请求
# .../layer.json/layer.json → 404，而它的 404 处理是塞一个假 heightmap
# 图层并污染共享 builder ⇒ 本任务自己的 quantized-mesh 瓦片也按 heightmap
# 解析，高程全错且全程不报错（实测 4154 m 山峰解成 -744 m）。
# 详见 src/services/terrain_tiling/layer_json.normalize_parent_url。
DEFAULT_PARENT_URL = "http://localhost:5000/terrain/base"


def _setup(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    # 全球底图缓存自 user_version=3 起落在 BASE_DIR/assets/terrain/base_z8；
    # 不 patch BASE_DIR 的话可用性闸门会去看真实仓库根，本机有没有解压过底图
    # 就成了测试结果的一部分。
    monkeypatch.setattr(config.Config, "BASE_DIR", tmp_path)
    for mod in ("app", "src.core.database", "src.services.dem_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    dtm = importlib.import_module("src.services.dem_task_manager")
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


def _make_base_terrain(tmp_path):
    """造一份最小 base_z8 —— 闸门要求 base 真的存在才写 parentUrl。

    2026-08-05：base 不可达时 manager 会返回 None 而不是写一个 404 的 URL
    （见 layer_json.parent_url_if_base_available）。这些用例要测的是「配置值
    被正确使用」，所以先把前提摆上；「base 不存在」那条另有专门用例。
    """
    base = tmp_path / "assets" / "terrain" / "base_z8"
    base.mkdir(parents=True, exist_ok=True)
    (base / "layer.json").write_text('{"tilejson":"1.0"}', encoding="utf-8")
    return base


def _no_op_tiling(monkeypatch, dtm):
    monkeypatch.setattr(
        dtm.DemTaskManager, "_run_tiling_job",
        lambda self, task_id, task_dir, output_dir, maxzoom, parent_url: None,
    )


def test_parent_url_defaults_to_localhost(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    _make_base_terrain(tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    _no_op_tiling(monkeypatch, dtm)
    task_id = _seed_completed_task(db, tmp_path / "out")

    mgr.start_tiling(task_id)

    assert mgr.get_tiling_job(task_id)["parent_url"] == DEFAULT_PARENT_URL


def test_parent_url_uses_config_value(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    _make_base_terrain(tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    _no_op_tiling(monkeypatch, dtm)
    task_id = _seed_completed_task(db, tmp_path / "out")

    mgr.config.set("terrain_base_parent_url",
                   "http://terrain.internal:9000/terrain/base/layer.json")

    mgr.start_tiling(task_id)

    # 配置值被采用，且尾部的 /layer.json 在写入前就被剥掉
    assert mgr.get_tiling_job(task_id)["parent_url"] == "http://terrain.internal:9000/terrain/base"


def test_parent_url_is_none_when_base_terrain_was_never_built(monkeypatch, tmp_path):
    """没建 base_z8（默认装机的常态）时必须不写 parentUrl。

    写一个指向 404 的 parentUrl 会让 Cesium 塞假 heightmap 图层并污染共享
    builder，本任务自己的 quantized-mesh 瓦片也按 heightmap 解析 ——
    实测高程 -859 / -956 / -744（真值 2672 / 1086 / 4154），且全程不报错。
    """
    db, dtm = _setup(monkeypatch, tmp_path)          # 刻意不建 base
    mgr = dtm.DemTaskManager(socketio=None)
    _no_op_tiling(monkeypatch, dtm)
    task_id = _seed_completed_task(db, tmp_path / "out")

    mgr.start_tiling(task_id)

    assert mgr.get_tiling_job(task_id)["parent_url"] is None
