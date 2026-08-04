"""API 层 bbox 边界测试 —— 三条管线创建任务入口对非法四至/非法 JSON 的响应。"""
import importlib
import os
import sys

import pytest

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


def _map_task_payload(**overrides):
    from src.core import config

    payload = {
        "name": "t", "north": 40.0, "south": 39.0, "east": 117.0, "west": 116.0,
        "zoom_min": 10, "zoom_max": 11, "style": "roadmap",
        # 保存路径新口径:一律绝对路径(相对值 → 400),默认 DOWNLOADS_DIR 下子目录
        "output_format": "tiles_only",
        "output_path": str(config.Config.DOWNLOADS_DIR / "downloads"),
    }
    payload.update(overrides)
    return payload


def _dem_task_payload(**overrides):
    from src.core import config

    payload = {
        "name": "t", "north": 40.0, "south": 39.0, "east": 117.0, "west": 116.0,
        "output_path": str(config.Config.DOWNLOADS_DIR / "downloads"),
        "dataset": "ASTGTM.003",
    }
    payload.update(overrides)
    return payload


# ---------- /api/tasks(地图瓦片) ----------

def test_map_task_valid_bbox_201(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/tasks", json=_map_task_payload())
    assert resp.status_code == 201


@pytest.mark.parametrize('overrides, match', [
    (dict(north=999), 'between -90 and 90'),
    (dict(north=38), 'must be greater than south'),
    (dict(east=115), 'must be greater than west'),      # east < west 不再静默交换
    (dict(north='abc'), 'must be a number'),
    (dict(north=None), 'must be a number'),             # None 过去是 TypeError -> 500
    (dict(north=[40]), 'must be a number'),
    (dict(north=float('nan')), 'must be a finite number'),
    (dict(zoom_min=25), 'must be between 0 and 21'),
    (dict(zoom_min='abc'), 'must be a number'),
])
def test_map_task_bad_bbox_400(monkeypatch, tmp_path, overrides, match):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/tasks", json=_map_task_payload(**overrides))
    assert resp.status_code == 400, resp.get_json()
    assert match in resp.get_json()['error']


def test_map_task_non_object_json_400(monkeypatch, tmp_path):
    """JSON 数组/字符串 body 不能 500。"""
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/tasks", json=[1, 2, 3])
    assert resp.status_code == 400
    resp = client.post("/api/tasks", data='"just a string"',
                       content_type='application/json')
    assert resp.status_code == 400


# ---------- /api/dem/tasks(DEM) ----------

def test_dem_task_valid_bbox_201(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/dem/tasks", json=_dem_task_payload())
    assert resp.status_code == 201, resp.get_json()


@pytest.mark.parametrize('overrides, match', [
    (dict(north=999), 'between -90 and 90'),   # 过去不查范围,会生成一堆不存在的颗粒
    (dict(south=-999), 'between -90 and 90'),
    (dict(east=-170, west=170), 'must be greater than west'),
    (dict(north='abc'), 'must be a number'),
    (dict(north=None), 'must be a number'),
])
def test_dem_task_bad_bbox_400(monkeypatch, tmp_path, overrides, match):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/dem/tasks", json=_dem_task_payload(**overrides))
    assert resp.status_code == 400, resp.get_json()
    assert match in resp.get_json()['error']


def test_dem_task_non_object_json_400(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/dem/tasks", json=[1, 2, 3])
    assert resp.status_code == 400
