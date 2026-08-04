"""I11: ASTGTM.003 不存在 _swb 颗粒，创建/枚举时必须明确拒绝。

真正的水体数据在 ASTWBD.001（astwbd_v1_att_granules_for_tile）。
过去勾选 swb 会生成一串必然 404 的颗粒名，拖垮整个任务。
"""

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database", "src.services.dem_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    dtm = importlib.import_module("src.services.dem_task_manager")
    return db, dtm


def test_astgtm_granules_reject_swb():
    from src.services.dem_granules import LatLonTile, astgtm_v3_granules_for_tile

    with pytest.raises(ValueError, match="(?i)swb"):
        astgtm_v3_granules_for_tile(LatLonTile(lat=0, lon=0), include_num=False, include_swb=True)


def test_astgtm_granules_still_allow_dem_and_num():
    from src.services.dem_granules import LatLonTile, astgtm_v3_granules_for_tile

    assert astgtm_v3_granules_for_tile(LatLonTile(lat=0, lon=0), include_num=True, include_swb=False) == [
        "ASTGTMV003_N00E000_dem.tif",
        "ASTGTMV003_N00E000_num.tif",
    ]


def test_create_task_rejects_swb_with_clear_error(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)

    with pytest.raises(ValueError, match="(?i)swb"):
        mgr.create_task({
            "name": "x", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
            "dataset": "ASTGTM.003", "download_swb": "true",
        })

    # 拒绝时不能留下半截任务
    conn = db.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM dem_tasks").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM dem_files").fetchone()["c"] == 0
    finally:
        conn.close()
