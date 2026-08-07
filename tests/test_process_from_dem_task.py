"""已完成的 DEM（高程）下载任务直接进处理流程 —— 不再要求上传文件。

两条来源分支各有一条端到端用例（真 GeoTIFF、真渲染、真落盘）：

- 等高线：`POST /api/contour/tasks` 带 `dem_task_id`，源目录零拷贝指向
  `dem_task_<id>/`，产物落在等高线任务自己的目录。这里必须端到端跑完 ——
  只断言建表成功挡不住「源目录传错 → list_dem_tifs 空 → 渲染 0 张瓦片」
  这类静默失败（contour_task_tiler 对空输入不抛异常）。
- 本地高程切片：来源为已下载的高程任务时复用既有的
  `POST /api/terrain/dem/<id>/start`，新增可选 `maxzoom` 覆盖配置默认值。
"""

import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

gdal = pytest.importorskip("osgeo.gdal")


# 0.1°×0.1°、120×120 像素（≈92 m/px）的斜坡，高程 0~600 m —— 保证 50 m
# 等高距下有线穿过，同时把瓦片数压在个位数量级，用例才跑得快。
_LON0, _LAT0, _SPAN, _PIX = 116.0, 39.0, 0.1, 0.1 / 120


def _make_dem(path):
    import numpy as np

    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(path), 120, 120, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((_LON0, _PIX, 0, _LAT0 + _SPAN, 0, -_PIX))
    srs = gdal.osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ramp = np.tile(np.linspace(0.0, 600.0, 120, dtype="float32"), (120, 1))
    ds.GetRasterBand(1).WriteArray(ramp)
    ds.FlushCache()
    ds = None


def _completed_dem_task(app_mod):
    """建一个 status='completed' 的 DEM 任务行 + 它已下载好的那张 tif。

    返回 (task_id, dem_tif_path)。文件名用 ASTER 的 `*_dem.tif` 形态 ——
    vrt_builder.list_dem_tifs 就是按这个 glob 找源文件的。
    """
    from src.core.config import Config

    output_path = os.path.join(str(Config.DOWNLOADS_DIR), "dem")
    os.makedirs(output_path, exist_ok=True)

    conn = sqlite3.connect(str(Config.DATABASE_PATH))
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks (name, status, north, south, east, west,
                                   dataset, output_path, total_files,
                                   downloaded_files, failed_files)
            VALUES (?, 'completed', ?, ?, ?, ?, 'ASTGTM.003', ?, 1, 1, 0)
            """,
            ("dem-src", _LAT0 + _SPAN, _LAT0, _LON0 + _SPAN, _LON0, output_path),
        )
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    task_dir = os.path.join(output_path, f"dem_task_{task_id}")
    os.makedirs(task_dir, exist_ok=True)
    dem_tif = os.path.join(task_dir, "ASTGTMV003_N39E116_dem.tif")
    _make_dem(dem_tif)
    return task_id, dem_tif


def _wait(fn, timeout=180.0):
    """轮询到 fn() 返回真值为止，返回它；超时返回 None。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.25)
    return None


# --- 等高线：源是已完成的 DEM 任务 ------------------------------------------


def test_contour_from_dem_task_renders_without_upload(isolated_app):
    client = isolated_app.app.test_client()
    dem_id, dem_tif = _completed_dem_task(isolated_app)

    resp = client.post("/api/contour/tasks", data={
        "name": "从高程任务出等高线",
        "dem_task_id": str(dem_id),
        "contour_interval": "50",
        "zoom_min": "11",
        "zoom_max": "12",
        "terrain_shade": "0",
    }, content_type="multipart/form-data")
    assert resp.status_code == 201, resp.get_data(as_text=True)
    cid = resp.get_json()["task_id"]

    task = client.get(f"/api/contour/tasks/{cid}").get_json()["task"]
    assert task["dataset"] == "dem_task"
    assert task["source_dem_task_id"] == dem_id
    assert task["total_files"] == 1 and task["downloaded_files"] == 1
    # bbox 从源 tif 实读，不是 0（历史记录地图靠它画框）
    assert task["west"] == pytest.approx(_LON0, abs=1e-3)
    assert task["north"] == pytest.approx(_LAT0 + _SPAN, abs=1e-3)

    # 零拷贝：等高线任务目录里不得出现源 DEM 的副本
    from src.core.config import Config
    ctask_dir = os.path.join(str(Config.DOWNLOADS_DIR), "dem", f"contour_task_{cid}")
    assert [f for f in os.listdir(ctask_dir) if f.endswith(".tif")] == []

    assert client.post(f"/api/contour/tasks/{cid}/start").status_code == 200

    final = _wait(lambda: (lambda s: s if s in ("completed", "failed") else None)(
        client.get(f"/api/contour/tasks/{cid}").get_json()["task"]["status"]))
    detail = client.get(f"/api/contour/tasks/{cid}").get_json()["task"]
    assert final == "completed", detail.get("error_message")
    assert detail["rendered_tiles"] > 0

    pngs = []
    for root, _dirs, files in os.walk(os.path.join(ctask_dir, "contour_tiles")):
        pngs += [os.path.join(root, f) for f in files if f.endswith(".png")]
    assert pngs, "渲染报 completed 却没有瓦片落盘"
    assert os.path.exists(dem_tif), "源 DEM 任务的文件被动过了"


def test_deleting_contour_task_never_touches_the_source_dem_task(isolated_app):
    """delete_files=true 只清等高线任务自己的目录 —— 源是别人的下载产物。"""
    client = isolated_app.app.test_client()
    dem_id, dem_tif = _completed_dem_task(isolated_app)

    cid = client.post("/api/contour/tasks", data={
        "name": "x", "dem_task_id": str(dem_id), "zoom_min": "11", "zoom_max": "11",
    }, content_type="multipart/form-data").get_json()["task_id"]

    resp = client.delete(f"/api/contour/tasks/{cid}?delete_files=true")
    assert resp.status_code == 200
    assert os.path.exists(dem_tif)


def test_contour_rejects_both_sources_and_unfinished_dem_task(isolated_app):
    import io

    client = isolated_app.app.test_client()
    dem_id, _ = _completed_dem_task(isolated_app)

    both = client.post("/api/contour/tasks", data={
        "name": "x", "dem_task_id": str(dem_id),
        "files": [(io.BytesIO(b"fake"), "a.tif")],
    }, content_type="multipart/form-data")
    assert both.status_code == 400
    assert "not both" in both.get_json()["error"]

    bad = client.post("/api/contour/tasks", data={"name": "x", "dem_task_id": "abc"},
                      content_type="multipart/form-data")
    assert bad.status_code == 400

    missing = client.post("/api/contour/tasks", data={"name": "x", "dem_task_id": "9999"},
                          content_type="multipart/form-data")
    assert missing.status_code == 400
    assert "not found" in missing.get_json()["error"]

    # 下载还没跑完的任务不能当源：数据残缺，渲染会在不完整输入上「成功」
    from src.core.config import Config
    conn = sqlite3.connect(str(Config.DATABASE_PATH))
    try:
        conn.execute("UPDATE dem_tasks SET status='running' WHERE id=?", (dem_id,))
        conn.commit()
    finally:
        conn.close()
    unfinished = client.post("/api/contour/tasks", data={
        "name": "x", "dem_task_id": str(dem_id),
    }, content_type="multipart/form-data")
    assert unfinished.status_code == 400
    assert "running" in unfinished.get_json()["error"]


# --- 本地高程切片：复用已完成 DEM 任务的地形切片管线 --------------------------


def test_dem_tiling_honours_maxzoom_from_the_process_form(isolated_app):
    client = isolated_app.app.test_client()
    dem_id, _ = _completed_dem_task(isolated_app)

    resp = client.post(f"/api/terrain/dem/{dem_id}/start", json={"maxzoom": 9})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    job = _wait(lambda: (lambda j: j if j["status"] in ("completed", "failed") else None)(
        client.get(f"/api/terrain/dem/{dem_id}").get_json()["job"]))
    assert job is not None and job["status"] == "completed", job
    assert job["maxzoom"] == 9
    assert os.path.exists(os.path.join(job["output_dir"], "layer.json"))


def test_dem_tiling_without_maxzoom_still_uses_the_configured_default(isolated_app):
    """不传 maxzoom 时行为不变 —— 仍取配置 terrain_local_maxzoom。"""
    client = isolated_app.app.test_client()
    dem_id, _ = _completed_dem_task(isolated_app)
    isolated_app.dem_task_manager.config.set("terrain_local_maxzoom", "9")

    assert client.post(f"/api/terrain/dem/{dem_id}/start").status_code == 200
    job = _wait(lambda: (lambda j: j if j["status"] in ("completed", "failed") else None)(
        client.get(f"/api/terrain/dem/{dem_id}").get_json()["job"]))
    assert job is not None and job["status"] == "completed", job
    assert job["maxzoom"] == 9


def test_dem_tiling_rejects_out_of_range_maxzoom(isolated_app):
    client = isolated_app.app.test_client()
    dem_id, _ = _completed_dem_task(isolated_app)
    resp = client.post(f"/api/terrain/dem/{dem_id}/start", json={"maxzoom": 99})
    assert resp.status_code == 400
