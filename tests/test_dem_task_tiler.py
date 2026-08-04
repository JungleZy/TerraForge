"""
Tests for DEM task tiler helpers.
"""

import os
import sys
from pathlib import Path


# Add parent directory to path for imports (match repo test style)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_list_dem_tifs_filters_num(tmp_path: Path):
    from src.services.terrain_tiling.vrt_builder import list_dem_tifs

    (tmp_path / "A_dem.tif").write_text("", encoding="utf-8")
    (tmp_path / "A_num.tif").write_text("", encoding="utf-8")

    assert list_dem_tifs(tmp_path) == [tmp_path / "A_dem.tif"]


def test_terrain_output_dir_for_task(tmp_path: Path):
    from src.services.terrain_tiling.dem_task_tiler import terrain_output_dir_for_task

    out = terrain_output_dir_for_task(str(tmp_path), 123)
    assert out == tmp_path / "dem_task_123" / "terrain_tiles"


def test_tile_dem_task_dir_calls_external_tools(tmp_path: Path):
    from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

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
    # 自适应三角化默认开启，且必须真的透传到 build_terrain —— 少了这两条，
    # 把 tile_dem_task_dir 里的两行 triangulator/max_error_k 删掉全量照样绿。
    # 65 = 2^6+1，满足自适应路径对 tile_size 的要求。
    # 'auto' = 逐瓦片择优（grid/martini 都编一遍，取 gzip 后更小的）：实测
    # 山地上 martini 的 gzip 字节反而 +17.6%，全局择优净省 27.6% 且每张瓦片
    # 字节严格 ≤ min(两者)。这里是这个字面量在全仓的唯一副本，
    # test_rtin.test_triangulation_defaults_agree_across_every_copy 负责把它
    # 传导到 DEFAULT_MAX_ERROR_K / build_terrain / CLI 三处。
    assert captured["triangulator"] == "auto"
    assert captured["max_error_k"] == 0.15
