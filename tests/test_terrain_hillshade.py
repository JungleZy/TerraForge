"""
无地形切片任务的源 DEM 晕渲预览（B1：预览要展示真实内容，而不是只定位）。

dem 任务只下载不切片是常态；local_terrain 任务的切片可能被删。这两种情况下
预览不该只 flyTo —— 后端按需把源 *_dem.tif 渲染成晕渲 PNG（VRT 马赛克 ->
gdaldem hillshade，结果缓存在任务目录），前端以单图矩形叠加。

契约：
  GET /terrain/dem/<id>/hillshade   -> {"url": ..., "bounds": [w, s, e, n]} / 404
  GET /terrain/dem/<id>/hillshade.png -> image/png / 404
  GET /terrain/local/<id>/hillshade(.png) 同上（源文件在 local_task_<id>/source/）
"""

import importlib
import sys

import pytest


def _load_client(monkeypatch, tmp_path):
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    sys.modules.pop("app", None)
    app_mod = importlib.import_module("app")

    app = app_mod.app
    app.config["TESTING"] = True
    return app.test_client()


def _make_dem(path, cols=30, rows=30, west=116.0, north=40.0, deg=0.01):
    gdal = pytest.importorskip("osgeo.gdal")
    np = pytest.importorskip("numpy")
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((west, deg, 0.0, north, 0.0, -deg))
    ds.GetRasterBand(1).WriteArray(
        np.arange(cols * rows, dtype=np.float32).reshape(rows, cols)
    )
    ds = None


def _insert_dem_task(output_path: str) -> int:
    db = importlib.import_module("src.core.database")
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks (name, status, north, south, east, west, dataset, output_path)
            VALUES ('dem', 'completed', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?)
            """,
            (output_path,),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _insert_local_task() -> int:
    db = importlib.import_module("src.core.database")
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO local_terrain_tasks
              (name, status, output_path, source_dir, output_dir, maxzoom)
            VALUES ('lt', 'completed', '', '', '', 14)
            """
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# dem
# ---------------------------------------------------------------------------


def test_dem_hillshade_renders_from_source_tifs(monkeypatch, tmp_path):
    client = _load_client(monkeypatch, tmp_path)
    out = tmp_path / "downloads" / "dem"
    task_id = _insert_dem_task(str(out))
    task_dir = out / f"dem_task_{task_id}"
    _make_dem(task_dir / "ASTGTMV003_N40E116_dem.tif")
    # _num.tif 是像元计数文件，绝不能进渲染输入
    _make_dem(task_dir / "ASTGTMV003_N40E116_num.tif", west=0.0, north=10.0)

    r = client.get(f"/terrain/dem/{task_id}/hillshade")
    assert r.status_code == 200
    data = r.get_json()
    assert data["url"] == f"/terrain/dem/{task_id}/hillshade.png"
    west, south, east, north = data["bounds"]
    assert (west, north) == pytest.approx((116.0, 40.0))
    assert east == pytest.approx(116.0 + 30 * 0.01)
    assert south == pytest.approx(40.0 - 30 * 0.01)

    png = client.get(data["url"])
    assert png.status_code == 200
    assert png.content_type == "image/png"
    assert png.data[:4] == b"\x89PNG"

    # 第二次走磁盘缓存，仍然是 200（渲染结果落在任务目录）
    assert (task_dir / "preview_hillshade.png").exists()
    r2 = client.get(f"/terrain/dem/{task_id}/hillshade")
    assert r2.status_code == 200


def test_dem_hillshade_custom_output_path(monkeypatch, tmp_path):
    """自定义保存路径（仍在 downloads 内）的任务也要能渲染 —— 与切片路由同口径"""
    client = _load_client(monkeypatch, tmp_path)
    out = tmp_path / "downloads" / "my_dem"
    task_id = _insert_dem_task(str(out))
    _make_dem(out / f"dem_task_{task_id}" / "x_dem.tif")

    r = client.get(f"/terrain/dem/{task_id}/hillshade")
    assert r.status_code == 200


def test_dem_hillshade_404_without_tifs(monkeypatch, tmp_path):
    client = _load_client(monkeypatch, tmp_path)
    out = tmp_path / "downloads" / "dem"
    task_id = _insert_dem_task(str(out))
    (out / f"dem_task_{task_id}").mkdir(parents=True)

    assert client.get(f"/terrain/dem/{task_id}/hillshade").status_code == 404
    assert client.get(f"/terrain/dem/{task_id}/hillshade.png").status_code == 404


def test_dem_hillshade_404_for_unknown_task(monkeypatch, tmp_path):
    client = _load_client(monkeypatch, tmp_path)
    assert client.get("/terrain/dem/999/hillshade").status_code == 404


# ---------------------------------------------------------------------------
# local_terrain
# ---------------------------------------------------------------------------


def test_local_hillshade_renders_from_source_dir(monkeypatch, tmp_path):
    client = _load_client(monkeypatch, tmp_path)
    task_id = _insert_local_task()
    task_dir = tmp_path / "downloads" / "terrain" / f"local_task_{task_id}"
    _make_dem(task_dir / "source" / "upload_1_dem.tif")

    r = client.get(f"/terrain/local/{task_id}/hillshade")
    assert r.status_code == 200
    data = r.get_json()
    assert data["url"] == f"/terrain/local/{task_id}/hillshade.png"
    assert len(data["bounds"]) == 4

    png = client.get(data["url"])
    assert png.status_code == 200
    assert png.data[:4] == b"\x89PNG"


def test_local_hillshade_404_without_source(monkeypatch, tmp_path):
    client = _load_client(monkeypatch, tmp_path)
    task_id = _insert_local_task()

    assert client.get(f"/terrain/local/{task_id}/hillshade").status_code == 404


def test_local_hillshade_404_for_unknown_task(monkeypatch, tmp_path):
    client = _load_client(monkeypatch, tmp_path)
    assert client.get("/terrain/local/999/hillshade").status_code == 404
