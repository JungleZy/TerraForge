import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.contour_engine import ContourStyle
from services.contour_task_tiler import (
    ContourParams, contour_output_dir_for_task, tile_contour_task_dir,
)


def test_contour_output_dir_for_task(tmp_path: Path):
    out = contour_output_dir_for_task(str(tmp_path), 7)
    assert out == tmp_path / "contour_task_7" / "contour_tiles"


def test_tile_contour_task_dir_injects_and_filters_dem(tmp_path: Path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "ASTGTMV003_N39E116_dem.tif").write_text("", encoding="utf-8")
    (task_dir / "ASTGTMV003_N39E116_num.tif").write_text("", encoding="utf-8")
    out_dir = tmp_path / "out"

    seen = {}

    def fake_build(dem_tifs, out_dir, interval, zoom_min, zoom_max, style,
                   progress_cb=None, stop_flag=None, shade=False, water=False, att_tifs=None,
                   workers=0):
        seen["dem_tifs"] = list(dem_tifs)
        seen["interval"] = interval
        seen["zooms"] = (zoom_min, zoom_max)
        seen["shade"] = shade
        seen["water"] = water
        seen["workers"] = workers
        return {"total": 3, "rendered": 3, "failed": 0}

    params = ContourParams(interval=50, zoom_min=12, zoom_max=13, style=ContourStyle(), workers=3)
    counts = tile_contour_task_dir(task_dir, out_dir, params, build_contour_fn=fake_build)

    assert counts == {"total": 3, "rendered": 3, "failed": 0}
    assert seen["dem_tifs"] == [task_dir / "ASTGTMV003_N39E116_dem.tif"]
    assert seen["interval"] == 50
    assert seen["zooms"] == (12, 13)
    assert seen["shade"] is False and seen["water"] is False  # defaults off
    assert seen["workers"] == 3  # workers 透传到引擎


def test_tile_contour_task_dir_forwards_shade_water_and_att(tmp_path: Path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "ASTGTMV003_N39E116_dem.tif").write_text("", encoding="utf-8")
    (task_dir / "ASTWBDV001_N39E116_att.tif").write_text("", encoding="utf-8")
    out_dir = tmp_path / "out"

    seen = {}

    def fake_build(dem_tifs, out_dir, interval, zoom_min, zoom_max, style,
                   progress_cb=None, stop_flag=None, shade=False, water=False, att_tifs=None,
                   workers=0):
        seen["shade"] = shade
        seen["water"] = water
        seen["att_tifs"] = list(att_tifs or [])
        return {"total": 1, "rendered": 1, "failed": 0}

    params = ContourParams(interval=50, zoom_min=12, zoom_max=12, style=ContourStyle(),
                           shade=True, water=True)
    tile_contour_task_dir(task_dir, out_dir, params, build_contour_fn=fake_build)

    assert seen["shade"] is True and seen["water"] is True
    assert seen["att_tifs"] == [task_dir / "ASTWBDV001_N39E116_att.tif"]


def test_tile_contour_task_dir_no_dem_returns_zero(tmp_path: Path):
    task_dir = tmp_path / "empty"
    task_dir.mkdir()
    out_dir = tmp_path / "out"
    params = ContourParams(interval=50, zoom_min=12, zoom_max=12, style=ContourStyle())

    def fake_build(*a, **k):
        raise AssertionError("should not be called without DEM")

    counts = tile_contour_task_dir(task_dir, out_dir, params, build_contour_fn=fake_build)
    assert counts == {"total": 0, "rendered": 0, "failed": 0}
