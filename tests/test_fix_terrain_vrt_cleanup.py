"""M18: build_terrain 多输入时的中间输入文件必须被清理，不能每切一次泄漏一份。

worker 在 initializer 里各自打开该文件，所以删除时机挂在整个切片过程的 finally 上。

中间产物形态已从「临时 .vrt」换成「物化的 .tif + overview 边车」—— 多源 VRT 会让
GDAL 的 overview 选层随读窗口漂移，实测开出 50.9 m 的瓦片接缝，详见
test_fix_terrain_vrt_overview_seam.py。因此这里要清理的体量也从几 KB 的 XML 变成了
与源数据同量级的栅格，泄漏的代价比原来大得多。
"""

import glob
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from osgeo import gdal

from src.services.terrain_tiling.cesiumlab_terrain import build_terrain


def _write_tif(path, west, south, px=40, deg_per_px=0.05):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), px, px, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((west, deg_per_px, 0.0, south + px * deg_per_px, 0.0, -deg_per_px))
    yy, xx = np.mgrid[0:px, 0:px].astype(np.float32)
    ds.GetRasterBand(1).WriteArray(100.0 + xx + yy)
    ds.FlushCache()
    ds = None


def _leftovers(work_dir):
    """work_dir 下残留的中间输入（含 .tif / .vrt / .ovr / .aux.xml 各种边车）。

    产物形态从临时 .vrt 换成物化的 .tif 之后，原来那个「只 glob 系统临时目录里
    cesiumlab_terrain_*.vrt」的检查恒为空集 —— 三条断言全部恒真、无声变绿。
    所以这里改成按**实际落盘位置**（build_terrain 传的 work_dir = 输出目录的
    父目录）去查，并且不限扩展名。
    """
    return set(glob.glob(os.path.join(str(work_dir), "cesiumlab_terrain_*")))


def _system_temp_leftovers():
    """系统临时目录里的残留 —— 守住「别改回去往 /tmp 写」。

    这里必须用 before/after **差集**而不是绝对集合：系统临时目录是共享的，
    别的进程（包括历史遗留）留下的同名文件会把绝对断言污染成假红。
    """
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "cesiumlab_terrain_*")))


def test_materialisation_really_lands_in_work_dir(tmp_path):
    """先证明中间产物确实落在 work_dir —— 否则上面那些「已清理」断言是空的。

    这条是前车之鉴：清理测试一旦对着一个永远为空的位置检查，它就再也不会红。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import build_input_raster

    tif1 = tmp_path / "a.tif"
    tif2 = tmp_path / "b.tif"
    _write_tif(tif1, west=100.0, south=30.0)
    _write_tif(tif2, west=102.0, south=30.0)

    work = tmp_path / "work"
    work.mkdir()
    assert _leftovers(work) == set()

    out = build_input_raster([str(tif1), str(tif2)], work_dir=str(work))

    assert _leftovers(work), "物化产物没有落在 work_dir —— 清理断言会变成空断言"
    assert os.path.dirname(os.path.abspath(out)) == str(work)


def test_build_terrain_multi_input_removes_temp_input(tmp_path):
    tif1 = tmp_path / "a.tif"
    tif2 = tmp_path / "b.tif"
    _write_tif(tif1, west=100.0, south=30.0)
    _write_tif(tif2, west=102.0, south=30.0)
    out = tmp_path / "tiles"

    before = _system_temp_leftovers()
    result = build_terrain(
        [str(tif1), str(tif2)], str(out), max_level=2, workers=1
    )

    # 切片真实发生（中间产物确实被进程消费过）
    assert (out / "layer.json").is_file()
    assert result["rendered"] > 0
    # 中间产物及其边车已删除，无泄漏
    assert _leftovers(tmp_path) == set()
    assert _system_temp_leftovers() == before


def test_build_terrain_multi_input_parallel_removes_temp_input(tmp_path):
    """并行路径：进程池退出后中间产物同样要删掉。"""
    tif1 = tmp_path / "a.tif"
    tif2 = tmp_path / "b.tif"
    _write_tif(tif1, west=100.0, south=30.0)
    _write_tif(tif2, west=102.0, south=30.0)
    out = tmp_path / "tiles"

    before = _system_temp_leftovers()
    result = build_terrain(
        [str(tif1), str(tif2)], str(out), max_level=2, workers=2
    )

    assert result["rendered"] > 0
    assert _leftovers(tmp_path) == set()
    assert _system_temp_leftovers() == before


def test_build_terrain_single_input_creates_no_temp_input(tmp_path):
    tif1 = tmp_path / "a.tif"
    _write_tif(tif1, west=100.0, south=30.0)
    out = tmp_path / "tiles"

    before = _system_temp_leftovers()
    build_terrain([str(tif1)], str(out), max_level=2, workers=1)

    assert (out / "layer.json").is_file()
    # 单幅直通，不该产生任何中间产物
    assert _leftovers(tmp_path) == set()
    assert _system_temp_leftovers() == before
