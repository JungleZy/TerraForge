"""M18: build_terrain 多输入时 build_input_vrt 落在 tempfile.mkstemp 的
临时 .vrt，此前从不删除，每次多输入切片泄漏一个临时文件。修复后临时 VRT
必须在进程池消费完之后被删除（worker 在 initializer 里各自打开该文件，
所以删除时机挂在整个切片过程的 finally 上）。
"""

import glob
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from osgeo import gdal

from services.terrain_tiling.cesiumlab_terrain import build_terrain


def _write_tif(path, west, south, px=40, deg_per_px=0.05):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), px, px, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((west, deg_per_px, 0.0, south + px * deg_per_px, 0.0, -deg_per_px))
    yy, xx = np.mgrid[0:px, 0:px].astype(np.float32)
    ds.GetRasterBand(1).WriteArray(100.0 + xx + yy)
    ds.FlushCache()
    ds = None


def _temp_vrts():
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "cesiumlab_terrain_*.vrt")))


def test_build_terrain_multi_input_removes_temp_vrt(tmp_path):
    tif1 = tmp_path / "a.tif"
    tif2 = tmp_path / "b.tif"
    _write_tif(tif1, west=100.0, south=30.0)
    _write_tif(tif2, west=102.0, south=30.0)
    out = tmp_path / "tiles"

    before = _temp_vrts()
    result = build_terrain(
        [str(tif1), str(tif2)], str(out), max_level=2, workers=1
    )

    # 切片真实发生（临时 VRT 确实被进程消费过）
    assert (out / "layer.json").is_file()
    assert result["rendered"] > 0
    # 临时 VRT 已删除，无泄漏
    assert _temp_vrts() == before


def test_build_terrain_multi_input_parallel_removes_temp_vrt(tmp_path):
    """并行路径：进程池退出后临时 VRT 同样要删掉。"""
    tif1 = tmp_path / "a.tif"
    tif2 = tmp_path / "b.tif"
    _write_tif(tif1, west=100.0, south=30.0)
    _write_tif(tif2, west=102.0, south=30.0)
    out = tmp_path / "tiles"

    before = _temp_vrts()
    result = build_terrain(
        [str(tif1), str(tif2)], str(out), max_level=2, workers=2
    )

    assert result["rendered"] > 0
    assert _temp_vrts() == before


def test_build_terrain_single_input_creates_no_temp_vrt(tmp_path):
    tif1 = tmp_path / "a.tif"
    _write_tif(tif1, west=100.0, south=30.0)
    out = tmp_path / "tiles"

    before = _temp_vrts()
    build_terrain([str(tif1)], str(out), max_level=2, workers=1)

    assert (out / "layer.json").is_file()
    assert _temp_vrts() == before
