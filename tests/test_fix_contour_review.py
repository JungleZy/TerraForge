"""2026-07-29 swarm review 等高线管线修复的回归测试。

I10  _render_contour_tile_core 每瓦片容错被 try 块位置架空
I19  skipped 瓦片不计入进度;rendered_tiles 列实存 rendered+failed
I15  interval 裸 float()(NaN/inf 可入库)/zoom 范围校验
I3   DELETE /api/contour/tasks/<id> 绕开 manager 锁,可删实际在跑的任务
minors: 瓦片失败无日志;att warp 失败静默;fmt='%d' 截断非整数 interval 标签
"""

import asyncio
import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import fresh_import


def _setup_db(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    db = fresh_import(monkeypatch, "core.database")
    db.init_database()
    ctm_mod = fresh_import(monkeypatch, "services.contour_task_manager")
    return db, ctm_mod


def _load_app(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    # routes / routes.contour_api 也必须重导入:app.py 是
    # `from routes import contour_api_bp` + `from routes.contour_api import
    # init_contour_task_manager`,若 sys.modules 里还留着其他测试 pop 后残留的
    # 旧实例,蓝图用的模块和 `import routes.contour_api` 拿到的就不是同一份
    # (模块双实例)。fresh_import 在 teardown 时恢复原有 sys.modules 条目,
    # 本文件也不会再把残留泄漏给后面的测试。
    app_mod = fresh_import(
        monkeypatch,
        "app", "core.database", "services.contour_task_manager",
        "routes", "routes.contour_api",
    )[0]
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _post_task(client, name="t", files=None, **fields):
    if files is None:
        files = [("dem1.tif", b"fake-tif-bytes")]
    data = dict(fields)
    data["name"] = name
    data["files"] = [(io.BytesIO(content), fname) for fname, content in files]
    return client.post("/api/contour/tasks", data=data,
                       content_type="multipart/form-data")


def _fake_world_ctx(np, band_arr=None, band_raises=None, **over):
    """覆盖全球 Web Mercator 范围的假渲染 ctx(替代 _build_render_ctx 的 GDAL 部分),
    用于直接驱动 _render_contour_tile_core。"""
    from types import SimpleNamespace
    from matplotlib.colors import to_rgba
    from services.contour_engine import ContourStyle, ORIGIN_SHIFT

    class _Band:
        def ReadAsArray(self, xoff, yoff, xsize, ysize):
            if band_raises is not None:
                raise band_raises
            return band_arr[yoff:yoff + ysize, xoff:xoff + xsize]

    n = 1024
    ctx = SimpleNamespace(
        originX=-ORIGIN_SHIFT, pxW=2 * ORIGIN_SHIFT / n,
        originY=ORIGIN_SHIFT, pxH=-2 * ORIGIN_SHIFT / n,
        nx=n, ny=n, band=_Band(), nodata=None,
        style=ContourStyle(), interval=50.0, shade=False, water=False,
        att_band=None, transparent=False, bg_rgba=to_rgba("#FAF6EC"),
        out_dir=None,
    )
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


def _write_dem(gdal, np, path, flat=False):
    """1° 合成 DEM(116E/39N 起),flat=True 时全幅同值(无等高线可画)。"""
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(path), 60, 60, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((116.0, 1.0 / 60, 0, 40.0, 0, -1.0 / 60))
    srs = gdal.osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    if flat:
        arr = np.full((60, 60), 500.0, dtype="float32")
    else:
        arr = np.tile(np.linspace(0, 6000, 60).astype("float32"), (60, 1))
    ds.GetRasterBand(1).WriteArray(arr)
    ds.FlushCache()
    ds = None


# ---------------------------------------------------------------------------
# I10: 读窗口/ReadAsArray/level 计算在 try 外,单瓦片 I/O 失败炸掉整个任务
# ---------------------------------------------------------------------------

def test_fix_i10_tile_read_failure_returns_failed_not_crash(tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("matplotlib")
    from services.contour_engine import _render_contour_tile_core

    ctx = _fake_world_ctx(np, band_raises=RuntimeError("simulated raster I/O failure"),
                          out_dir=tmp_path)
    # 契约(docstring):单瓦片失败计入 'failed' 继续,而不是把异常抛给整个任务
    assert _render_contour_tile_core(2, 1, 1, ctx) == "failed"


def test_fix_minor_tile_failure_logs_warning(tmp_path, caplog):
    np = pytest.importorskip("numpy")
    pytest.importorskip("matplotlib")
    import logging
    from services.contour_engine import _render_contour_tile_core

    ctx = _fake_world_ctx(np, band_raises=RuntimeError("io boom"), out_dir=tmp_path)
    with caplog.at_level(logging.WARNING, logger="services.contour_engine"):
        assert _render_contour_tile_core(2, 1, 1, ctx) == "failed"
    # 瓦片失败必须有日志,且带瓦片坐标便于排查缺片
    assert any("z=2" in r.getMessage() and "x=1" in r.getMessage()
               for r in caplog.records)


# ---------------------------------------------------------------------------
# I19: skipped 瓦片不计入进度;rendered_tiles/failed_tiles 列语义不实
# ---------------------------------------------------------------------------

def test_fix_i19_skipped_tiles_count_toward_progress(tmp_path):
    gdal = pytest.importorskip("osgeo.gdal")
    np = pytest.importorskip("numpy")
    pytest.importorskip("matplotlib")
    from services.contour_engine import build_contour_tiles, ContourStyle

    dem = tmp_path / "ASTGTMV003_N39E116_dem.tif"
    _write_dem(gdal, np, dem, flat=True)  # 平坦 DEM + 纯线模式 -> 全部瓦片 skipped

    progress = []
    counts = build_contour_tiles(
        dem_tifs=[dem], out_dir=tmp_path / "tiles", interval=50,
        zoom_min=10, zoom_max=11, style=ContourStyle(), workers=1,
        progress_cb=lambda done, total: progress.append((done, total)),
    )
    assert counts["total"] >= 1
    assert counts["rendered"] == 0
    # skipped 也是处理完的瓦片:进度必须按 processed 走到 total,
    # 而不是停在 0/total(如 72%)就直接 completed
    assert progress and progress[-1] == (counts["total"], counts["total"])
    assert counts["skipped"] == counts["total"]


def test_fix_i19_final_render_counts_written_honestly(monkeypatch, tmp_path):
    db, ctm_mod = _setup_db(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    # 下载驱动 create_task 已删除:直接 SQL 造旧版下载驱动的 running 任务行
    conn = db.get_connection()
    try:
        from core import config
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO contour_tasks (
                name, status, north, south, east, west, dataset,
                contour_interval, background, terrain_shade, water,
                zoom_min, zoom_max, output_path,
                total_files, downloaded_files, failed_files,
                total_tiles, rendered_tiles, failed_tiles
            )
            VALUES ('t', 'running', 1.0, 0.0, 1.0, 0.0, 'COP-DEM-GLO-30', 50,
                    '#FAF6EC', 1, 0, 12, 12, ?, 1, 0, 0, 0, 0, 0)
            """,
            (str(Path(config.Config.DOWNLOADS_DIR) / "dem"),),
        )
        task_id = cur.lastrowid
        cur.execute(
            "INSERT INTO contour_files (task_id, granule_id, kind, status, retry_count)"
            " VALUES (?, ?, 'dem', 'pending', 0)",
            (task_id, "Copernicus_DSM_COG_10_N00_00_E000_00_DEM/Copernicus_DSM_COG_10_N00_00_E000_00_DEM.tif"),
        )
        conn.commit()
    finally:
        conn.close()

    async def fake_download(dataset, granules, output_dir, progress_callback=None, stop_flag=None):
        for g in granules:
            if progress_callback:
                await progress_callback(g, "completed", None, 1)
    monkeypatch.setattr(mgr.engine, "download_files", fake_download)

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None, stop_flag=None):
        if progress_cb:
            progress_cb(10, 10)  # 进度按 processed(rendered+skipped+failed)上报
        return {"total": 10, "rendered": 6, "failed": 1, "skipped": 3}
    import services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, None))

    task = mgr.get_task(task_id)
    assert task["status"] == "completed"
    # 列语义如实:收尾后 rendered_tiles/failed_tiles 是真实渲染/失败数,
    # 不是进度用的 processed 计数
    assert task["rendered_tiles"] == 6
    assert task["failed_tiles"] == 1


# ---------------------------------------------------------------------------
# I15: interval 裸 float()(NaN/inf 可入库)/ zoom 0-21 校验
# ---------------------------------------------------------------------------

def test_fix_i15_create_task_with_files_rejects_non_finite_interval(monkeypatch, tmp_path):
    from werkzeug.datastructures import FileStorage
    db, ctm_mod = _setup_db(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    for bad in ("nan", "inf", "-inf"):
        with pytest.raises(ValueError):
            mgr.create_task_with_files(
                name="x",
                files=[FileStorage(stream=io.BytesIO(b"fake"), filename="a.tif")],
                contour_interval=bad)


def test_fix_i15_api_rejects_non_finite_interval(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    for bad in ("nan", "inf"):
        resp = _post_task(client, contour_interval=bad)
        assert resp.status_code == 400, bad


def test_fix_i15_api_rejects_zoom_out_of_range(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    assert _post_task(client, zoom_max="30").status_code == 400
    assert _post_task(client, zoom_min="-1").status_code == 400
    assert _post_task(client, zoom_min="14", zoom_max="10").status_code == 400


def test_fix_i15_style_preview_rejects_non_finite_interval(monkeypatch, tmp_path):
    pytest.importorskip("matplotlib")  # 修复前会真的走进渲染;修复后在校验处 400 短路
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/api/contour/style_preview?interval=nan")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# I3: DELETE 绕开 manager 锁,可删实际在跑的任务
# ---------------------------------------------------------------------------

def test_fix_i3_delete_task_with_active_thread_rejected(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    tid = _post_task(client).get_json()["task_id"]

    # _load_app 已把 routes / routes.contour_api 一并重导入,这里正常 import
    # 拿到的就是蓝图实际在用的同一个模块实例
    from routes import contour_api
    mgr = contour_api.contour_task_manager

    class _Alive:
        def is_alive(self):
            return True

    # DB 状态还是 pending,但执行线程已活(check-then-act 竞态窗口):
    # 绕开 manager 锁的删除会把实际在跑的任务行删掉
    mgr.active_tasks[tid] = _Alive()
    try:
        resp = client.delete(f"/api/contour/tasks/{tid}")
    finally:
        mgr.active_tasks.pop(tid, None)
    assert resp.status_code == 400
    assert client.get(f"/api/contour/tasks/{tid}").status_code == 200


def test_fix_i3_delete_pending_task_still_works(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    tid = _post_task(client).get_json()["task_id"]
    resp = client.delete(f"/api/contour/tasks/{tid}")
    assert resp.status_code == 200
    assert client.get(f"/api/contour/tasks/{tid}").status_code == 404


# ---------------------------------------------------------------------------
# minors: att warp 失败静默;fmt='%d' 截断非整数 interval 标签
# ---------------------------------------------------------------------------

def test_fix_minor_att_warp_failure_logs_warning(tmp_path, caplog):
    gdal = pytest.importorskip("osgeo.gdal")
    np = pytest.importorskip("numpy")
    pytest.importorskip("matplotlib")
    import logging
    from services.contour_engine import build_contour_tiles, ContourStyle

    dem = tmp_path / "ASTGTMV003_N39E116_dem.tif"
    _write_dem(gdal, np, dem)
    with caplog.at_level(logging.WARNING, logger="services.contour_engine"):
        counts = build_contour_tiles(
            dem_tifs=[dem], out_dir=tmp_path / "tiles", interval=50,
            zoom_min=10, zoom_max=11, style=ContourStyle(), workers=1,
            water=True, att_tifs=[tmp_path / "missing_att.tif"],
        )
    assert counts["rendered"] >= 1  # att 失败是 best-effort,任务继续
    assert any("att" in r.getMessage().lower() for r in caplog.records)


def test_fix_minor_contour_label_fmt_keeps_non_integer(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("matplotlib")
    import matplotlib.axes
    from services.contour_engine import _render_contour_tile_core

    captured = {}

    def fake_contour(self, *a, **k):
        return object()

    def fake_clabel(self, cs, *a, **k):
        captured.update(k)
        return []

    monkeypatch.setattr(matplotlib.axes.Axes, "contour", fake_contour)
    monkeypatch.setattr(matplotlib.axes.Axes, "clabel", fake_clabel)

    n = 1024
    arr = np.tile(np.linspace(0, 100, n).astype("float64"), (n, 1))
    ctx = _fake_world_ctx(np, band_arr=arr, interval=12.5, out_dir=tmp_path)
    # z=14 == detail_zoom -> 有效 interval 就是非整数的 12.5(计曲线 62.5)
    assert _render_contour_tile_core(14, 8192, 8192, ctx) == "rendered"
    fmt = captured.get("fmt")
    assert fmt is not None
    # '%d' 会把 62.5 截断成 '62';标签格式必须保留非整数
    assert fmt % 62.5 == "62.5"
