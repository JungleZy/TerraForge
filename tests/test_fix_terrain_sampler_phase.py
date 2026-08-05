"""DemSampler 的降采样相位：同一经纬点的采样值必须与传入的 bbox 无关。

根因：sample() 的读窗口由传入 lons/lats 的**包围盒**决定，再交给 GDAL 降采样
到输出网格密度。相邻瓦片的包围盒不同 ⇒ 降采样格子落在源像素的不同相位上 ⇒
同一个经纬点采出不同的高程。

后果是真实可见的裂缝，且**与三角化、法线都无关**（grid 分支同样吃）：
  - 几何：z12 山地相邻瓦片公共边平均差 3.4 m、最大 16.8 m（z14 精确为 0，
    因为那一层读窗口小于 buf、不触发降采样）
  - 法线：ghost 环取到的高程两侧不一致 ⇒ 中间层的法线接缝无法归零
    （z11 夹角中位只能压到 0.238°，而 z14 是逐位相同）

设计稿曾断言「相邻瓦片公共边顶点集合逐点一致，几何上不可能开缝」——
顶点集合确实一致，但**顶点的高程值**不一致。
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from osgeo import gdal, osr

from src.services.terrain_tiling.cesiumlab_terrain import DemSampler

# 源像素 1/3600 度（1 弧秒，与 ASTER/GLO-30 同量级），覆盖 1°×1°
_PX = 1.0 / 3600.0
_N = 3600
_LON0, _LAT1 = 86.0, 43.0          # 左上角


def _write_dem(path):
    """高频起伏的合成 DEM —— 相位错位在高频下才暴露得出来。"""
    ii, jj = np.meshgrid(np.arange(_N), np.arange(_N))
    z = (1500.0
         + 400.0 * np.sin(ii * 0.11) * np.cos(jj * 0.09)
         + 150.0 * np.sin(ii * 0.37 + jj * 0.23))
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(path), _N, _N, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((_LON0, _PX, 0.0, _LAT1, 0.0, -_PX))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(z.astype(np.float32))
    ds.FlushCache()
    ds = None
    return path


def _tile_grid(west, south, east, north, n=65):
    lons = np.linspace(west, east, n, dtype=np.float64)
    lats = np.linspace(south, north, n, dtype=np.float64)
    return np.meshgrid(lons, lats)


@pytest.fixture(scope="module")
def dem(tmp_path_factory):
    return _write_dem(tmp_path_factory.mktemp("phase") / "dem.tif")


@pytest.mark.parametrize("span", [0.2, 0.1, 0.05])
def test_adjacent_tiles_agree_on_the_shared_edge(dem, span):
    """东西相邻两瓦片，公共边（同一条经线上的同样 65 个点）必须采出同样的高程。

    span 覆盖三种降采样倍率：0.2° → win≈720/buf 67（10.7x）、
    0.1° → 5.4x、0.05° → 2.7x。倍率越大相位错位越明显。
    """
    s = DemSampler(str(dem))
    w = 86.2
    a_lon, a_lat = _tile_grid(w, 42.3, w + span, 42.3 + span)
    b_lon, b_lat = _tile_grid(w + span, 42.3, w + 2 * span, 42.3 + span)

    ha = s.sample(a_lon, a_lat)
    hb = s.sample(b_lon, b_lat)

    east_edge = ha[:, -1]      # A 的东边
    west_edge = hb[:, 0]       # B 的西边 —— 同一条经线上的同样一组点
    diff = np.abs(east_edge - west_edge)
    assert diff.max() == 0.0, (
        f"span={span}: 公共边最大差 {diff.max():.3f} m（均值 {diff.mean():.3f}）—— "
        f"读窗口相位随 bbox 变了")


@pytest.mark.parametrize("span", [0.2, 0.1])
def test_north_south_neighbours_agree_too(dem, span):
    """南北方向同理 —— 相位在两个轴上各错各的。"""
    s = DemSampler(str(dem))
    lat = 42.3
    a_lon, a_lat = _tile_grid(86.2, lat, 86.2 + span, lat + span)
    c_lon, c_lat = _tile_grid(86.2, lat + span, 86.2 + span, lat + 2 * span)

    ha = s.sample(a_lon, a_lat)
    hc = s.sample(c_lon, c_lat)

    diff = np.abs(ha[-1, :] - hc[0, :])
    assert diff.max() == 0.0, f"span={span}: 南北公共边最大差 {diff.max():.3f} m"


def test_same_points_sampled_through_different_windows_agree(dem):
    """同一组点，一次单独采、一次夹在更大的网格里采，结果必须一致。

    这条剥掉了「相邻瓦片」的外壳，直接钉住 sample() 的语义：返回值只能取决于
    问的是哪些经纬点，不能取决于这一批点的包围盒有多大。ghost 环就吃这个亏 ——
    它把 65² 的网格扩成 67²，于是同一批点被换了一个窗口去采。
    """
    s = DemSampler(str(dem))
    inner_lon, inner_lat = _tile_grid(86.3, 42.4, 86.4, 42.5, n=65)

    step = (86.4 - 86.3) / 64
    outer_lon, outer_lat = _tile_grid(86.3 - step, 42.4 - step,
                                      86.4 + step, 42.5 + step, n=67)
    h_inner = s.sample(inner_lon, inner_lat)
    h_outer = s.sample(outer_lon, outer_lat)[1:-1, 1:-1]

    diff = np.abs(h_inner - h_outer)
    # 容差取 float32 的机器精度（高程 ~2000 m 时 1 ULP ≈ 0.00024 m），不是 0：
    # 换窗口会改变 buffer 尺寸，插值的中间浮点运算路径随之不同，实测 5/4225
    # 个点差 1 ULP（0.000122 m，相对 6.2e-08）。相位错位则是**米级**的
    # （修复前实测 20 m），两者差五个数量级，这个阈值抓得住。
    # 真正要精确为 0 的是相邻瓦片公共边 —— 那是同一个 buffer 格网内的相同位置，
    # 上面两条测试断言的就是 == 0.0。
    assert diff.max() < 1e-3, (
        f"同一批点换个窗口就变了：最大差 {diff.max():.6f} m —— "
        f"这正是 ghost 环让法线接缝在中间层无法归零的原因")


def test_downsampling_actually_happens_in_these_cases(dem):
    """守卫上面几条的前提：这些配置真的触发了降采样。

    如果哪天 buf 的算法改成不降采样了，上面几条会变成恒真的空测试。
    """
    s = DemSampler(str(dem))
    calls = []
    real = s.band.ReadAsArray

    def spy(xoff, yoff, win_x, win_y, buf_xsize=None, buf_ysize=None, **kw):
        calls.append((win_x, win_y, buf_xsize, buf_ysize))
        return real(xoff, yoff, win_x, win_y,
                    buf_xsize=buf_xsize, buf_ysize=buf_ysize, **kw)

    s.band.ReadAsArray = spy
    lon, lat = _tile_grid(86.2, 42.3, 86.4, 42.5)
    s.sample(lon, lat)
    assert calls, "没有发生 ReadAsArray"
    win_x, win_y, buf_x, buf_y = calls[0]
    assert buf_x < win_x and buf_y < win_y, (
        f"窗口 {win_x}x{win_y} 未被降采样到 {buf_x}x{buf_y} —— 这几条测试失去意义")
