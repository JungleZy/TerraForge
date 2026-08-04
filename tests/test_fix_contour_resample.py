"""
收尾3(contour I17):contour 低层级读全幅 OOM 的修复测试。

_render_contour_tile_core 低 zoom 时单瓦片窗口覆盖整幅 DEM,按原始分辨率
ReadAsArray 会把整幅读进内存(OOM)。修复:_read_band_window 在窗口超过
_MAX_READ_DIM 时传 buf_xsize/buf_ysize 让 GDAL 端重采样;窗口不超限时
不传任何 buf 参数,行为与原 ReadAsArray 完全一致。

用记录调用参数的 fake band 即可验证,无需真 GDAL/大文件。
"""

import os
import sys

import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.services.contour_engine import _MAX_READ_DIM, _read_band_window


class FakeBand:
    """记录 ReadAsArray 调用参数,并按请求尺寸返回零数组。"""

    def __init__(self):
        self.calls = []

    def ReadAsArray(self, xoff, yoff, win_x, win_y, **kwargs):
        self.calls.append((xoff, yoff, win_x, win_y, kwargs))
        buf_x = kwargs.get("buf_xsize", win_x)
        buf_y = kwargs.get("buf_ysize", win_y)
        return np.zeros((buf_y, buf_x), dtype="float32")


def test_small_window_reads_at_native_resolution():
    """窗口不超上限:不传 buf 参数,原样读取(行为不变)。"""
    band = FakeBand()
    arr = _read_band_window(band, 10, 20, 100, 80)
    assert band.calls == [(10, 20, 100, 80, {})]
    assert arr.shape == (80, 100)


def test_window_at_limit_reads_at_native_resolution():
    """窗口恰好等于上限:仍原样读取。"""
    band = FakeBand()
    arr = _read_band_window(band, 0, 0, _MAX_READ_DIM, _MAX_READ_DIM)
    assert band.calls == [(0, 0, _MAX_READ_DIM, _MAX_READ_DIM, {})]
    assert arr.shape == (_MAX_READ_DIM, _MAX_READ_DIM)


def test_oversized_window_resamples_to_limit():
    """窗口超上限(低 zoom 全幅场景):传 buf_xsize/buf_ysize 让 GDAL 重采样。"""
    band = FakeBand()
    arr = _read_band_window(band, 5, 7, 5000, 3000)
    assert len(band.calls) == 1
    xoff, yoff, win_x, win_y, kwargs = band.calls[0]
    assert (xoff, yoff, win_x, win_y) == (5, 7, 5000, 3000)
    assert kwargs["buf_xsize"] == _MAX_READ_DIM
    assert kwargs["buf_ysize"] == _MAX_READ_DIM
    assert arr.shape == (_MAX_READ_DIM, _MAX_READ_DIM)


def test_mixed_window_clamps_only_oversized_axis():
    """仅单轴超上限:只钳制该轴,另一轴按原尺寸传 buf。"""
    band = FakeBand()
    arr = _read_band_window(band, 0, 0, 1000, 100)
    _, _, _, _, kwargs = band.calls[0]
    assert kwargs["buf_xsize"] == _MAX_READ_DIM
    assert kwargs["buf_ysize"] == 100
    assert arr.shape == (100, _MAX_READ_DIM)


def test_resample_alg_forwarded_only_when_given():
    """resample_alg 仅在显式传入时转发(DEM 用双线性,分类属性栅格默认最近邻)。"""
    band = FakeBand()
    _read_band_window(band, 0, 0, 1000, 1000, resample_alg="bilinear-sentinel")
    assert band.calls[0][4]["resample_alg"] == "bilinear-sentinel"

    band2 = FakeBand()
    _read_band_window(band2, 0, 0, 1000, 1000)
    assert "resample_alg" not in band2.calls[0][4]


def test_max_read_dim_aligns_with_output_tile():
    """上限取 258 = 256 输出像素 + 2 余量,与输出瓦片对齐。"""
    assert _MAX_READ_DIM == 258
