import os
import sys

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
