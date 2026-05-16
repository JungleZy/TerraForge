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


def test_run_cmd_raises_on_failure(monkeypatch):
    from services.terrain_tiling.ctb_runner import run_cmd
    from services.terrain_tiling import ctb_runner

    def fake_run(*args, **kwargs):
        return ctb_runner.subprocess.CompletedProcess(
            args=["fake"], returncode=2, stdout="", stderr="boom"
        )

    monkeypatch.setattr(ctb_runner.subprocess, "run", fake_run)

    try:
        run_cmd(["ctb-tile"])
    except RuntimeError as e:
        assert "returncode=2" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
