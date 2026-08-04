import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.dem_granules import (
    coverage_bbox, tiles_for_bbox, astwbd_v1_att_granules_for_tile,
    copernicus_glo30_granules_for_tile, LatLonTile,
)


def test_astwbd_att_granule_name():
    assert astwbd_v1_att_granules_for_tile(LatLonTile(lat=0, lon=6)) == ["ASTWBDV001_N00E006_att.tif"]
    assert astwbd_v1_att_granules_for_tile(LatLonTile(lat=-4, lon=120)) == ["ASTWBDV001_S04E120_att.tif"]


def test_copernicus_glo30_granule_path():
    # GLO-30 tiles are nested: <name>/<name>.tif on the AWS open bucket.
    assert copernicus_glo30_granules_for_tile(LatLonTile(lat=40, lon=116)) == [
        "Copernicus_DSM_COG_10_N40_00_E116_00_DEM/Copernicus_DSM_COG_10_N40_00_E116_00_DEM.tif"
    ]
    assert copernicus_glo30_granules_for_tile(LatLonTile(lat=-4, lon=-115)) == [
        "Copernicus_DSM_COG_10_S04_00_W115_00_DEM/Copernicus_DSM_COG_10_S04_00_W115_00_DEM.tif"
    ]


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
