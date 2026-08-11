"""
I17 fix: DemSampler.sample() read the whole intersected source window at
native resolution (band.ReadAsArray(xoff, yoff, w, h)) even for low-zoom tiles
whose sampling grid is only 65x65 — with a large DEM and multiple tiling
workers this balloons memory (OOM).

The fix passes buf_xsize/buf_ysize so GDAL resamples the window down to about
the sampling-grid density, and scales the local pixel coordinates accordingly.
Sampling semantics must stay correct: values sampled at a given lon/lat must
still match the DEM at that position.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from osgeo import gdal

from src.services.terrain_tiling.cesium_terrain import DemSampler


class _FakeBand:
    def __init__(self):
        self.calls = []

    def ReadAsArray(self, xoff, yoff, win_x, win_y, buf_xsize=None, buf_ysize=None, **kwargs):
        self.calls.append(
            dict(xoff=xoff, yoff=yoff, win_x=win_x, win_y=win_y,
                 buf_xsize=buf_xsize, buf_ysize=buf_ysize, **kwargs)
        )
        bw = buf_xsize or win_x
        bh = buf_ysize or win_y
        return np.zeros((bh, bw), dtype=np.float32)


def _make_sampler(cols, rows, gt, band, nodata=None):
    s = DemSampler.__new__(DemSampler)
    s.gt = gt
    s.cols = cols
    s.rows = rows
    s.nodata = nodata
    s.band = band
    return s


def test_sample_downsamples_large_windows_via_gdal_buffer():
    # 1 source pixel per degree; sample a 17x17 grid spanning 100..200 deg.
    band = _FakeBand()
    s = _make_sampler(10000, 10000, (0.0, 1.0, 0.0, 0.0, 0.0, 1.0), band)
    lons, lats = np.meshgrid(np.linspace(100, 200, 17), np.linspace(100, 200, 17))
    s.sample(lons, lats)

    assert len(band.calls) == 1
    call = band.calls[0]
    assert call["win_x"] > 17 and call["win_y"] > 17
    # Buffer must be capped near the sampling grid density, not native res.
    #
    # 上界从 (n+2) 放宽到 2*(n+2)：降采样倍率现在取 2 的幂并**向下**取整，
    # 好处是 buffer 永远不稀疏于输出网格（采样精度），代价是最多比目标大一倍。
    # 取 2 的幂是为了让降采样格网锚定在源像素上、与请求 bbox 无关 —— 否则
    # 相邻瓦片相位不同，同一经纬点采出不同高程（实测公共边差 20 m，
    # 见 test_fix_terrain_sampler_phase.py）。
    # OOM 防护的实质不变：这里窗口是 100+ 像素，buffer 仍在 30 上下。
    assert call["buf_xsize"] is not None and call["buf_xsize"] <= 2 * (17 + 2)
    assert call["buf_ysize"] is not None and call["buf_ysize"] <= 2 * (17 + 2)
    assert call["buf_xsize"] < call["win_x"] and call["buf_ysize"] < call["win_y"]


def test_sample_small_windows_keep_native_resolution():
    band = _FakeBand()
    s = _make_sampler(10000, 10000, (0.0, 1.0, 0.0, 0.0, 0.0, 1.0), band)
    lons, lats = np.meshgrid(np.linspace(100, 105, 17), np.linspace(100, 105, 17))
    s.sample(lons, lats)

    call = band.calls[0]
    # Window (~7x7) is smaller than the sampling grid: read it as-is.
    assert call["buf_xsize"] == call["win_x"]
    assert call["buf_ysize"] == call["win_y"]


def test_resampled_sampling_is_numerically_correct(tmp_path):
    # Linear field over 2000x2000 px, 0.01 deg/px: h = 0.5*px + 0.25*py.
    # Bilinear sampling of a linear field must reproduce the field, whether or
    # not GDAL downsamples the read window first.
    cols = rows = 2000
    west0, north0, deg_per_px = 100.0, 40.0, 0.01
    tif = tmp_path / "big_dem.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(tif), cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((west0, deg_per_px, 0.0, north0, 0.0, -deg_per_px))
    py_px, px_px = np.mgrid[0:rows, 0:cols].astype(np.float32)
    ds.GetRasterBand(1).WriteArray(0.5 * px_px + 0.25 * py_px)
    ds.FlushCache()
    ds = None

    sampler = DemSampler(str(tif))
    n = 65  # forces ~1000px window -> ~67px buffer (real downsampling)
    lons = np.linspace(105.0, 115.0, n)
    lats = np.linspace(25.0, 35.0, n)
    llon, llat = np.meshgrid(lons, lats)

    got = sampler.sample(llon, llat)
    px = (llon - west0) / deg_per_px
    py = (north0 - llat) / deg_per_px
    # arr[j,i] 是【像素中心】的取值，中心在边坐标 (i+0.5, j+0.5)。
    # 所以把场写成边坐标的函数要减半像素。
    expected = 0.5 * (px - 0.5) + 0.25 * (py - 0.5)

    assert got.shape == (n, n)
    # 梯度 0.5/0.25 per px:半像素偏移会产生 0.375 的系统误差。
    # 收紧容差钉住无偏行为 —— 帐篷滤波对线性场在窗口内部精确复现。
    np.testing.assert_allclose(got, expected, atol=0.05)


def test_sampling_has_no_half_pixel_bias(tmp_path):
    """采样坐标不得偏移半个源像素。

    历史：M19 复核提出「公式应改成 (px - x0c)/sx - 0.5」，当时被本测试的旧版
    驳回。**旧版是错的** —— 它用 `WriteArray(a*i + b*j)` 造线性场，却拿
    `expected = a*px_edge + b*py_edge` 对账，等于把「arr[j,i] 位于边坐标 i」
    这个错误前提写进了基准，于是与同样错的实现自洽、测得过。

    2026-08-08 用 GDAL 自己当裁判重判（见 test_sampling_matches_gdal_warp）：
    中心制 RMS 0.0000 m、旧式 RMS 8.0 m。M19 是对的，已修。
    """
    cols = rows = 2000
    west0, north0, deg_per_px = 100.0, 40.0, 0.01
    tif = tmp_path / "steep_dem.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(tif), cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((west0, deg_per_px, 0.0, north0, 0.0, -deg_per_px))
    py_px, px_px = np.mgrid[0:rows, 0:cols].astype(np.float32)
    ds.GetRasterBand(1).WriteArray(1.0 * px_px + 0.7 * py_px)
    ds.FlushCache()
    ds = None

    sampler = DemSampler(str(tif))
    n = 65  # forces downsampling: ~1000px window -> ~67px buffer
    lons = np.linspace(105.0, 115.0, n)
    lats = np.linspace(25.0, 35.0, n)
    llon, llat = np.meshgrid(lons, lats)

    got = sampler.sample(llon, llat)
    px = (llon - west0) / deg_per_px
    py = (north0 - llat) / deg_per_px
    expected = 1.0 * (px - 0.5) + 0.7 * (py - 0.5)

    err = got - expected
    np.testing.assert_allclose(got, expected, atol=0.05)
    # 系统性偏移检查：无偏采样的平均误差应趋于 0（半像素偏移则 ~+0.85）
    assert abs(float(err.mean())) < 0.01


def test_sampling_matches_gdal_warp(tmp_path):
    """用 GDAL 自己当预言机 —— 不依赖任何人对「边/中心」约定的解读。

    把源栅格 warp 到一个**像素中心恰好落在查询点上**的网格，GRA_Bilinear。
    DemSampler 在同样这批经纬点上的输出必须与之一致。

    这条测试存在的理由：半像素约定在本仓库上被判过两次、反过一次（见
    test_sampling_has_no_half_pixel_bias 的 docstring）。合成线性场 + 手算
    expected 的测法容易把错误前提写进基准；拿 GDAL 的重采样结果对账则不会。
    """
    cols = rows = 400
    west0, north0, deg_per_px = 100.0, 40.0, 0.01
    tif = tmp_path / "wavy_dem.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(tif), cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((west0, deg_per_px, 0.0, north0, 0.0, -deg_per_px))
    py_px, px_px = np.mgrid[0:rows, 0:cols].astype(np.float32)
    # 非线性场：线性场对任何相位偏移都只产生常数误差，验不出方向性问题。
    ds.GetRasterBand(1).WriteArray(
        100.0 * np.sin(px_px / 7.0) + 60.0 * np.cos(py_px / 5.0))
    ds.FlushCache()
    ds = None

    src = gdal.Open(str(tif))
    n = 65
    # 查询网格必须比源【更细】：gdal.Warp 在降采样时会对源footprint做核平均，
    # 与「纯双线性点采样」不可比（实测 1.3x 粗网格上两者差 1.97，那不是相位问题）。
    # 上采样时 GRA_Bilinear 就是纯双线性，此时两者只剩相位这一个变量。
    # 步长取源像素的非整数倍，避免与源网格同相而掩盖偏移。
    step = deg_per_px * 0.7
    lons = west0 + 50 * deg_per_px + np.arange(n) * step
    lats = north0 - 50 * deg_per_px - np.arange(n) * step
    llon, llat = np.meshgrid(lons, lats)

    # 目标栅格的像素中心 = 上面那批点
    dst = gdal.GetDriverByName("GTiff").Create(
        str(tmp_path / "warped.tif"), n, n, 1, gdal.GDT_Float64)
    dst.SetGeoTransform((lons[0] - step / 2, step, 0.0,
                         lats[0] + step / 2, 0.0, -step))
    dst.SetProjection(src.GetProjection())
    gdal.Warp(dst, src, resampleAlg=gdal.GRA_Bilinear)
    ref = dst.GetRasterBand(1).ReadAsArray().astype(float)
    dst = None

    got = DemSampler(str(tif)).sample(llon, llat).astype(float)

    # 去掉最外一圈，避开 warp 的边界处理差异
    inner = (slice(1, -1), slice(1, -1))
    np.testing.assert_allclose(got[inner], ref[inner], atol=1e-3)
