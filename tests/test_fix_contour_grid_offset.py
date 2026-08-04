"""
H1 回归:等高线读窗口降采样后,坐标网格的【窗口偏移项】必须用原始像元 pxW/pxH,
只有窗口内部的步进才用重采样后的 eff_px_*。

坏写法 `(col0 + i + 0.5) * eff_px_w` 把偏移项也缩放了,误差
    col0 * pxW * (win_x - arr.shape[1]) / arr.shape[1]
随瓦片在栅格中越靠东/南线性增大 —— 低 zoom 下等高线整片偏出瓦片,被
ax.set_xlim 裁掉,PNG 里只剩底色,而 drew 仍为 True、任务照报 completed。

本用例构造一个必然触发降采样(win > _MAX_READ_DIM)且 col0/row0 > 0(瓦片不
压在栅格西/北边缘)的场景 —— 现有 test_fix_contour_resample.py 只钉
ReadAsArray 的 buf 参数,test_contour_engine_render.py 的源 DEM 只有 60x60,
两者都进不了这个分支。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

gdal = pytest.importorskip("osgeo.gdal")
np = pytest.importorskip("numpy")
pytest.importorskip("matplotlib")

from src.services.contour_engine import (  # noqa: E402
    ContourStyle,
    _MAX_READ_DIM,
    _build_render_ctx,
    _render_contour_tile_core,
)
from src.services.contour_engine import tile_bounds_meters  # noqa: E402

# 栅格:EPSG:3857,原点 (0, 5000000),100m 像元,2000x2000 (200km x 200km)。
_ORIGIN_X = 0.0
_ORIGIN_Y = 5_000_000.0
_PX = 100.0
_N = 2000
# z10 瓦片宽 ≈ 39.1km = 391 个源像元 > 258 -> 必然降采样;
# 这两个瓦片号使 col0/row0 都远大于 0(瓦片落在栅格内部偏东南)。
_Z, _TX, _TY = 10, 514, 385


def _make_dem_3857(path):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(path), _N, _N, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((_ORIGIN_X, _PX, 0, _ORIGIN_Y, 0, -_PX))
    srs = gdal.osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    ds.SetProjection(srs.ExportToWkt())
    # 双向斜坡:保证任意子窗口内都有高程变化,contour 必定出线。
    ramp = np.linspace(0, 3000, _N, dtype="float32")
    arr = ramp[None, :] + ramp[:, None] * 0.5
    ds.GetRasterBand(1).WriteArray(arr.astype("float32"))
    ds.FlushCache()
    ds = None


def test_downsampled_window_grid_stays_inside_tile(tmp_path, monkeypatch):
    """降采样 + col0/row0 > 0 时,传给 ax.contour 的坐标网格必须落在读窗口的
    地理范围内(等价于落在瓦片附近),而不是被偏移项放大后甩出去。"""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.axes import Axes

    dem = tmp_path / "grid_offset_3857.tif"
    _make_dem_3857(dem)

    captured = []
    _orig_contour = Axes.contour

    def _spy(self, *args, **kwargs):
        if len(args) >= 3:
            captured.append((np.asarray(args[0]), np.asarray(args[1])))
        return _orig_contour(self, *args, **kwargs)

    monkeypatch.setattr(Axes, "contour", _spy)

    ctx = _build_render_ctx(
        str(dem), None, ContourStyle(), interval=50,
        shade=False, water=False, out_dir=tmp_path / "out",
    )
    try:
        status = _render_contour_tile_core(_Z, _TX, _TY, ctx)
    finally:
        ctx.att_ds = ctx.att_band = None
        ctx.band = None
        ctx.ds = None

    assert status == "rendered", f"瓦片应渲染成功,实际 {status}"
    assert captured, "ax.contour 未被调用 —— 用例没走到画线分支"

    # 复算本瓦片的读窗口,得到它的地理范围(与 _render_contour_tile_core 同口径)。
    import math
    xmin, ymin, xmax, ymax = tile_bounds_meters(_Z, _TX, _TY)
    col0 = max(int(math.floor((xmin - _ORIGIN_X) / _PX)) - 1, 0)
    col1 = min(int(math.ceil((xmax - _ORIGIN_X) / _PX)) + 1, _N)
    row0 = max(int(math.floor((ymax - _ORIGIN_Y) / -_PX)) - 1, 0)
    row1 = min(int(math.ceil((ymin - _ORIGIN_Y) / -_PX)) + 1, _N)

    # 前提自检:这个场景必须真的触发降采样,且偏移非零 —— 否则用例失去意义。
    assert col1 - col0 > _MAX_READ_DIM, "窗口未超上限,没触发降采样分支"
    assert row1 - row0 > _MAX_READ_DIM, "窗口未超上限,没触发降采样分支"
    assert col0 > 0 and row0 > 0, "col0/row0 被钳到 0,偏移项误差恒为 0"

    win_x0 = _ORIGIN_X + col0 * _PX
    win_x1 = _ORIGIN_X + col1 * _PX
    win_y1 = _ORIGIN_Y - col0 * 0 - row1 * _PX  # 南边界(y 向下递减)
    win_y0 = _ORIGIN_Y - row0 * _PX             # 北边界

    for X, Y in captured:
        assert X.min() >= win_x0, f"xs 起点 {X.min():.1f} 落在读窗口西界 {win_x0:.1f} 之外"
        assert X.max() <= win_x1, f"xs 终点 {X.max():.1f} 落在读窗口东界 {win_x1:.1f} 之外"
        assert Y.min() >= win_y1, f"ys 落在读窗口南界 {win_y1:.1f} 之外"
        assert Y.max() <= win_y0, f"ys 落在读窗口北界 {win_y0:.1f} 之外"
        # 更强:网格必须与瓦片自身范围相交,否则 set_xlim 会把线全裁掉。
        assert X.max() >= xmin and X.min() <= xmax, "xs 与瓦片范围不相交"
        assert Y.max() >= ymin and Y.min() <= ymax, "ys 与瓦片范围不相交"
