import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.dem_granules import coverage_bbox, tiles_for_bbox


def test_coverage_bbox_small_box_expands_to_full_degree_tile():
    # A tiny sub-degree box inside N00E000 covers the whole 1x1 degree tile.
    north, south, east, west = coverage_bbox(0.95, 0.90, 0.45, 0.40)
    assert (north, south, east, west) == (1, 0, 1, 0)


def test_coverage_bbox_spans_multiple_tiles():
    # Box crossing lon=1 and lat=1 -> union of 4 tiles, extent [0,2] x [0,2].
    north, south, east, west = coverage_bbox(1.2, 0.8, 1.2, 0.8)
    assert (north, south, east, west) == (2, 0, 2, 0)


def test_coverage_bbox_never_smaller_than_input():
    n, s, e, w = coverage_bbox(39.95, 39.90, 116.45, 116.40)
    assert n >= 39.95 and s <= 39.90 and e >= 116.45 and w <= 116.40
    # Bounds are whole-degree (granule) aligned.
    assert n == int(n) and s == int(s) and e == int(e) and w == int(w)


def test_coverage_bbox_matches_downloaded_tiles():
    # Coverage extent must agree with the actual granule tiles selected.
    args = dict(north=2.3, south=1.1, east=3.7, west=2.2)
    tiles = tiles_for_bbox(**args)
    n, s, e, w = coverage_bbox(**args)
    assert s == min(t.lat for t in tiles)
    assert n == max(t.lat for t in tiles) + 1
    assert w == min(t.lon for t in tiles)
    assert e == max(t.lon for t in tiles) + 1
