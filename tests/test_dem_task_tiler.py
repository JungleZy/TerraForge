"""
Tests for DEM task tiler helpers.
"""

import os
import sys
from pathlib import Path


# Add parent directory to path for imports (match repo test style)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_list_dem_tifs_filters_num(tmp_path: Path):
    from services.terrain_tiling.vrt_builder import list_dem_tifs

    (tmp_path / "A_dem.tif").write_text("", encoding="utf-8")
    (tmp_path / "A_num.tif").write_text("", encoding="utf-8")

    assert list_dem_tifs(tmp_path) == [tmp_path / "A_dem.tif"]


def test_terrain_output_dir_for_task(tmp_path: Path):
    from services.terrain_tiling.dem_task_tiler import terrain_output_dir_for_task

    out = terrain_output_dir_for_task(str(tmp_path), 123)
    assert out == tmp_path / "dem_task_123" / "terrain_tiles"


def test_tile_dem_task_dir_calls_external_tools(tmp_path: Path):
    from services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "A_dem.tif").write_text("", encoding="utf-8")

    out_dir = tmp_path / "out"

    captured = {}

    def fake_build_terrain(**kwargs):
        captured.update(kwargs)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "layer.json").write_text('{"parentUrl":"OLD","available":[]}\n', encoding="utf-8")

    params = TileParams(maxzoom=0, parent_url="https://example.com/parent.json")
    # Don't run the real GDAL/numpy pipeline in unit tests.
    tile_dem_task_dir(task_dir, out_dir, params, build_terrain_fn=fake_build_terrain)

    layer = (out_dir / "layer.json").read_text(encoding="utf-8")
    assert '"parentUrl": "https://example.com/parent.json"' in layer
    # 65x65 grid matches 30 m DEM resolution at the estimated maxzoom (z14).
    assert captured["tile_size"] == 65
