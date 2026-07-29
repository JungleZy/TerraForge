"""等高线最高层级自动计算：zoom_max 留空时按 DEM 原始分辨率估算，
显式填写时走 0-21 校验（允许设得比自动值更高）。"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.contour_task_manager import estimate_max_zoom


def test_estimate_max_zoom_30m_dem():
    # 30m DEM（GLO-30）：像素匹配层 13，+1 过采样 = 14
    assert estimate_max_zoom(30.0, 10) == 14


def test_estimate_max_zoom_90m_dem():
    # 90m DEM（SRTM）：像素匹配层 11，+1 = 12
    assert estimate_max_zoom(90.0, 10) == 12


def test_estimate_max_zoom_clamped_to_zoom_min():
    # 粗 DEM + 用户给的高 zoom_min：不能低于 zoom_min
    assert estimate_max_zoom(90.0, 16) == 16


def test_estimate_max_zoom_capped_at_21():
    assert estimate_max_zoom(0.01, 10) == 21


def test_estimate_max_zoom_unknown_pixel_size():
    assert estimate_max_zoom(0, 10) == 21
    assert estimate_max_zoom(-1, 10) == 21


gdal = pytest.importorskip("osgeo.gdal")


def _make_dem(path, pixel_deg, lon0=116.0, lat0=39.0):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(path), 10, 10, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((lon0, pixel_deg, 0, lat0, 0, -pixel_deg))
    srs = gdal.osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.FlushCache()
    ds = None


def test_finest_pixel_size_geographic(tmp_path):
    from services.contour_task_manager import _finest_pixel_size_3857
    p = tmp_path / "dem.tif"
    _make_dem(p, 30.0 / 111320.0)  # ≈30m
    px = _finest_pixel_size_3857([p])
    assert px == pytest.approx(30.0, rel=0.01)
    # 与 estimate_max_zoom 联动：30m → 14 级
    assert estimate_max_zoom(px, 10) == 14


def test_finest_pixel_size_picks_finest(tmp_path):
    from services.contour_task_manager import _finest_pixel_size_3857
    p30 = tmp_path / "a.tif"
    p90 = tmp_path / "b.tif"
    _make_dem(p30, 30.0 / 111320.0)
    _make_dem(p90, 90.0 / 111320.0)
    px = _finest_pixel_size_3857([p90, p30])
    assert px == pytest.approx(30.0, rel=0.01)


def test_finest_pixel_size_unreadable_returns_none(tmp_path):
    from services.contour_task_manager import _finest_pixel_size_3857
    bad = tmp_path / "bad.tif"
    bad.write_bytes(b"not a geotiff")
    assert _finest_pixel_size_3857([bad]) is None


# ---- API 层：留空走自动，显式填校验 0-21 ----

def _load_app(monkeypatch, tmp_path):
    import importlib

    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _post_task(client, **fields):
    data = dict(fields)
    data.setdefault("name", "auto-zoom")
    data["files"] = [(io.BytesIO(b"fake-tif-bytes"), "dem1.tif")]
    return client.post("/api/contour/tasks", data=data,
                       content_type="multipart/form-data")


def test_empty_zoom_max_uses_fallback_when_unreadable(monkeypatch, tmp_path):
    """zoom_max 留空 → 自动；假 tif 读不出分辨率 → 兜底 15，zoom_min 默认 10。"""
    client = _load_app(monkeypatch, tmp_path)
    resp = _post_task(client)
    assert resp.status_code == 201
    tid = resp.get_json()["task_id"]
    task = client.get(f"/api/contour/tasks/{tid}").get_json()["task"]
    assert task["zoom_min"] == 10
    assert task["zoom_max"] == 15


def test_empty_zoom_max_auto_from_real_dem(monkeypatch, tmp_path):
    """真 30m DEM：zoom_max 自动算成 14。"""
    client = _load_app(monkeypatch, tmp_path)
    import numpy as np
    dem_path = tmp_path / "real_dem.tif"
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(dem_path), 60, 60, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((116.0, 30.0 / 111320.0, 0, 40.0, 0, -30.0 / 111320.0))
    srs = gdal.osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.zeros((60, 60), dtype="float32"))
    ds.FlushCache()
    ds = None

    with open(dem_path, "rb") as f:
        content = f.read()
    resp = client.post("/api/contour/tasks", data={
        "name": "real",
        "files": [(io.BytesIO(content), "real_dem.tif")],
    }, content_type="multipart/form-data")
    assert resp.status_code == 201
    tid = resp.get_json()["task_id"]
    task = client.get(f"/api/contour/tasks/{tid}").get_json()["task"]
    assert task["zoom_max"] == 14


def test_explicit_higher_zoom_max_honored(monkeypatch, tmp_path):
    """显式填更高层级（高于自动值）按用户值入库。"""
    client = _load_app(monkeypatch, tmp_path)
    tid = _post_task(client, zoom_min="10", zoom_max="18").get_json()["task_id"]
    task = client.get(f"/api/contour/tasks/{tid}").get_json()["task"]
    assert task["zoom_min"] == 10
    assert task["zoom_max"] == 18


def test_zoom_max_out_of_range_400(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    assert _post_task(client, zoom_max="22").status_code == 400
    assert _post_task(client, zoom_min="-1").status_code == 400


def test_zoom_min_greater_than_zoom_max_400(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    assert _post_task(client, zoom_min="15", zoom_max="12").status_code == 400
