import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.contour_engine import (
    ORIGIN_SHIFT, deg2num, tile_bounds_meters, tiles_for_bbox_xyz,
    count_tiles, is_index_contour, ContourStyle,
)


def test_tile_bounds_meters_world_at_z0():
    xmin, ymin, xmax, ymax = tile_bounds_meters(0, 0, 0)
    assert abs(xmin + ORIGIN_SHIFT) < 1e-6
    assert abs(ymax - ORIGIN_SHIFT) < 1e-6
    assert abs(xmax - ORIGIN_SHIFT) < 1e-6
    assert abs(ymin + ORIGIN_SHIFT) < 1e-6


def test_deg2num_center_z1():
    assert deg2num(0.0, 0.0, 1) == (1, 1)


def test_tiles_for_bbox_xyz_single_small_area():
    tiles = tiles_for_bbox_xyz(north=39.95, south=39.90, east=116.45, west=116.40, zoom=12)
    assert len(tiles) >= 1
    n = 2 ** 12
    for (x, y) in tiles:
        assert 0 <= x < n and 0 <= y < n


def test_count_tiles_monotonic():
    a = count_tiles(39.95, 39.90, 116.45, 116.40, 12, 12)
    b = count_tiles(39.95, 39.90, 116.45, 116.40, 12, 14)
    assert b > a


def test_is_index_contour():
    assert is_index_contour(500, 50, 5) is True
    assert is_index_contour(250, 50, 5) is True
    assert is_index_contour(550, 50, 5) is False
    assert is_index_contour(300, 50, 5) is False


def test_contour_style_from_config():
    cfg = {
        "contour_color_intermediate": "#111111",
        "contour_color_index": "#222222",
        "contour_color_label": "#333333",
        "contour_width_intermediate": "0.7",
        "contour_width_index": "1.5",
        "contour_background": "transparent",
        "contour_index_step": "5",
    }

    class FakeConfig:
        def get(self, k, default=None):
            return cfg.get(k, default)

    style = ContourStyle.from_config(FakeConfig())
    assert style.color_index == "#222222"
    assert style.width_intermediate == 0.7
    assert style.index_step == 5
    assert style.background == "transparent"


def test_interval_for_zoom_standard():
    from services.contour_engine import interval_for_zoom
    base = 50
    # detail band: z >= 14 all use base
    for z in (14, 15, 16, 19):
        assert interval_for_zoom(base, z, detail_zoom=14) == 50
    # coarsens below detail_zoom on the 1-2-5 ladder, one step per zoom
    assert interval_for_zoom(base, 13, detail_zoom=14) == 100
    assert interval_for_zoom(base, 12, detail_zoom=14) == 250
    assert interval_for_zoom(base, 11, detail_zoom=14) == 500
    assert interval_for_zoom(base, 10, detail_zoom=14) == 1000
    assert interval_for_zoom(base, 9, detail_zoom=14) == 2500
    assert interval_for_zoom(base, 8, detail_zoom=14) == 5000


def test_interval_for_zoom_gentle_coarsens_slower():
    from services.contour_engine import interval_for_zoom
    # gentle steps once per two zoom levels -> at z13 still base, coarser only deeper
    assert interval_for_zoom(50, 13, detail_zoom=14, scaling="gentle") == 50
    assert interval_for_zoom(50, 12, detail_zoom=14, scaling="gentle") == 100
    # standard is strictly coarser than gentle at the same low zoom
    assert interval_for_zoom(50, 10, detail_zoom=14, scaling="standard") >= interval_for_zoom(50, 10, detail_zoom=14, scaling="gentle")


def test_build_contour_tiles_warp_tmpdir_from_config(monkeypatch, tmp_path):
    """contour_warp_tmpdir 配置键指定 warp 产物临时目录(大区域可达数十 GB,
    默认留空走系统临时目录)。假 gdal + 假 _build_render_ctx,只验证 tmpdir 选址
    与结束后的清理。"""
    import types

    import pytest

    import services.config_manager as cm
    import services.contour_engine as ce

    warp_root = tmp_path / "warp_tmp"
    warp_root.mkdir()
    monkeypatch.setattr(
        cm.ConfigManager, "get",
        lambda self, k, d=None: str(warp_root) if k == "contour_warp_tmpdir" else d)

    fake_gdal = types.SimpleNamespace(
        UseExceptions=lambda: None,
        BuildVRT=lambda *a, **k: object(),
        Warp=lambda *a, **k: None,
    )
    monkeypatch.setitem(sys.modules, "osgeo", types.SimpleNamespace(gdal=fake_gdal))
    monkeypatch.setitem(sys.modules, "osgeo.gdal", fake_gdal)

    seen = {}

    class _Sentinel(Exception):
        pass

    def fake_ctx(dem_path, att_path, style, interval, shade, water, out_dir):
        seen["dem_path"] = dem_path
        raise _Sentinel

    monkeypatch.setattr(ce, "_build_render_ctx", fake_ctx)

    with pytest.raises(_Sentinel):
        ce.build_contour_tiles(
            dem_tifs=[tmp_path / "A_dem.tif"], out_dir=tmp_path / "out",
            interval=50, zoom_min=12, zoom_max=12, style=ContourStyle(), workers=1)

    # dem_3857.tif 落在 <warp_root>/contour_warp_*/ 下
    assert Path(seen["dem_path"]).parent.parent == warp_root
    # finally 清理:配置目录下不留 contour_warp_ 残留
    assert list(warp_root.iterdir()) == []
