"""
HIGH #6 fix: terrain 链路对齐 contour 已修过的四个坑
(src/services/terrain_tiling/cesiumlab_terrain.py, 范本 src/services/contour_engine.py):

1. build_terrain 不再把全部瓦片任务物化成单个 list(大区域高 max_level OOM),
   而是算术计数 total + 生成器按 batch 分发给进程池(Executor.map 会把传入
   iterable 一次性全 submit 成 Future,直接接生成器等于变相物化,所以必须分批);
2. workers=0(自动)时 worker 数封顶 min(4, cpu_count),显式传参可覆盖;
3. 并行遇 BrokenProcessPool(worker 被 OS 杀)回退串行重跑,不整任务失败;
4. 单瓦片失败(ReadAsArray 返回 None 等)只计入 failed,不炸掉整个任务,
   失败计数体现在返回的 counts dict 里。
"""

import concurrent.futures
import json
import os
import sys
from concurrent.futures.process import BrokenProcessPool

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from osgeo import gdal

from src.services.terrain_tiling import cesiumlab_terrain as ct


def _make_dem(path, cols=60, rows=60, deg_per_px=0.01, west=116.0, north=40.0):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((west, deg_per_px, 0.0, north, 0.0, -deg_per_px))
    ds.GetRasterBand(1).WriteArray(
        np.arange(cols * rows, dtype=np.float32).reshape(rows, cols)
    )
    ds.FlushCache()
    ds = None


class _InProcessPool:
    """记录 max_workers / 每批 map 输入,并在本进程内真正执行任务。"""

    instances = []

    def __init__(self, max_workers=None, initializer=None, initargs=()):
        self.max_workers = max_workers
        self.initializer = initializer
        self.initargs = initargs
        self.map_inputs = []
        _InProcessPool.instances.append(self)

    def __enter__(self):
        if self.initializer is not None:
            self.initializer(*self.initargs)
        return self

    def __exit__(self, *a):
        return False

    def map(self, fn, iterable, chunksize=1):
        self.map_inputs.append(iterable)
        return map(fn, iterable)


def test_serial_build_generates_all_tiles_and_counts(tmp_path):
    dem = tmp_path / "dem.tif"
    _make_dem(dem)
    out = tmp_path / "tiles"

    counts = ct.build_terrain([str(dem)], str(out), min_level=0, max_level=1, workers=1)

    # z0 全球 2x1、z1 全球 4x2(z<=4 不按 bbox 裁剪)-> total = 2 + 8 = 10
    assert counts == {"total": 10, "rendered": 10, "failed": 0}
    assert len(list(out.rglob("*.terrain"))) == 10
    layer = json.loads((out / "layer.json").read_text(encoding="utf-8"))
    assert layer["minzoom"] == 0 and layer["maxzoom"] == 1
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["minHeight"] <= meta["maxHeight"]


def test_auto_workers_capped_at_four_and_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 64)  # 模拟多核机器
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _InProcessPool)
    _InProcessPool.instances.clear()

    dem = tmp_path / "dem.tif"
    _make_dem(dem)

    ct.build_terrain([str(dem)], str(tmp_path / "auto"), min_level=0, max_level=1, workers=0)
    assert _InProcessPool.instances[-1].max_workers == 4

    ct.build_terrain([str(dem)], str(tmp_path / "explicit"), min_level=0, max_level=1, workers=8)
    assert _InProcessPool.instances[-1].max_workers == 8


def test_tasks_streamed_to_pool_in_bounded_batches(tmp_path, monkeypatch):
    """并行分支不许把整张任务列表物化:每次 map 调用只能拿到一个有限的 batch,
    且所有 batch 合起来恰好覆盖 total。"""
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _InProcessPool)
    _InProcessPool.instances.clear()

    # 大范围 DEM(60x30 度),max_level=8 -> 瓦片数 > 2048,必须跨多个 batch。
    dem = tmp_path / "big.tif"
    _make_dem(dem, cols=60, rows=60, deg_per_px=1.0, west=100.0, north=40.0)
    # rows=60 * 1deg = 60 度南北向 -> 100E..160E, 20S..40N

    def fake_worker(task):  # 真渲染几千瓦片太慢,这里只验证分发行为
        return (0.0, 1.0)

    monkeypatch.setattr(ct, "_worker_tile", fake_worker)
    counts = ct.build_terrain([str(dem)], str(tmp_path / "tiles"),
                              min_level=0, max_level=8, workers=2)

    pool = _InProcessPool.instances[-1]
    assert counts["total"] > 2048
    assert len(pool.map_inputs) >= 2, "total 超过单批 2048,必须分多批喂给进程池"
    for batch in pool.map_inputs:
        assert isinstance(batch, list) and 0 < len(batch) <= 2048
    assert sum(len(b) for b in pool.map_inputs) == counts["total"]
    assert counts["rendered"] == counts["total"] and counts["failed"] == 0


def test_broken_process_pool_falls_back_to_serial(tmp_path, monkeypatch):
    """worker 进程崩溃(BrokenProcessPool)时必须回退串行重跑、切片完整,
    而不是整个任务失败(多 worker 内存耗尽被 OS 杀的真实场景)。"""

    class _BoomExecutor:  # 模拟一启动就崩的进程池
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def map(self, *a, **k):
            raise BrokenProcessPool("simulated worker crash")

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _BoomExecutor)

    dem = tmp_path / "dem.tif"
    _make_dem(dem)
    out = tmp_path / "tiles"

    # workers=4 + total>4 -> 走并行分支 -> 触发 BrokenProcessPool -> 回退串行
    counts = ct.build_terrain([str(dem)], str(out), min_level=0, max_level=1, workers=4)

    assert counts == {"total": 10, "rendered": 10, "failed": 0}
    assert len(list(out.rglob("*.terrain"))) == 10, "回退串行后应补齐全部瓦片"


def test_worker_tile_returns_none_on_bad_read(tmp_path):
    """ReadAsArray 返回 None(坏块)时 _worker_tile 捕获异常返回 None,不再
    AttributeError 炸全任务。"""

    class _NoneBand:
        def ReadAsArray(self, *a, **k):
            return None

    sampler = ct.DemSampler.__new__(ct.DemSampler)
    sampler.gt = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    sampler.cols = 100
    sampler.rows = 100
    sampler.nodata = None
    sampler.band = _NoneBand()
    ct._WORKER_SAMPLER = sampler
    try:
        result = ct._worker_tile((0, 0, 0, -180.0, -90.0, 0.0, 0.0, 17, str(tmp_path)))
    finally:
        ct._WORKER_SAMPLER = None
    assert result is None


def test_per_tile_failure_counted_and_excluded_from_meta(tmp_path, monkeypatch):
    """单瓦片失败:failed 计数 +1,任务照常完成,meta.json 的高度范围只统计
    成功瓦片。"""
    dem = tmp_path / "dem.tif"
    _make_dem(dem)
    out = tmp_path / "tiles"

    def fake_worker(task):
        z, x, y = task[0], task[1], task[2]
        if (z, x, y) == (1, 2, 1):
            return None  # 模拟该瓦片失败
        return (100.0, 200.0)

    monkeypatch.setattr(ct, "_worker_tile", fake_worker)
    counts = ct.build_terrain([str(dem)], str(out), min_level=0, max_level=1, workers=1)

    assert counts == {"total": 10, "rendered": 9, "failed": 1}
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["minHeight"] == 100.0 and meta["maxHeight"] == 200.0
