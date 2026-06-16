import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.terrain_tiling.vrt_builder import list_dem_tifs, list_att_tifs


def test_list_dem_and_att_tifs_are_separate(tmp_path):
    (tmp_path / "ASTGTMV003_N00E000_dem.tif").write_bytes(b"x")
    (tmp_path / "ASTGTMV003_N00E000_num.tif").write_bytes(b"x")
    (tmp_path / "ASTWBDV001_N00E000_att.tif").write_bytes(b"x")

    dems = [p.name for p in list_dem_tifs(tmp_path)]
    atts = [p.name for p in list_att_tifs(tmp_path)]

    assert dems == ["ASTGTMV003_N00E000_dem.tif"]  # _num and _att excluded
    assert atts == ["ASTWBDV001_N00E000_att.tif"]


def test_list_dem_tifs_matches_uppercase_copernicus(tmp_path):
    # Copernicus GLO-30 files end in uppercase _DEM.tif; aux files (_EDM) must not match.
    (tmp_path / "ASTGTMV003_N00E000_dem.tif").write_bytes(b"x")
    (tmp_path / "Copernicus_DSM_COG_10_N40_00_E116_00_DEM.tif").write_bytes(b"x")
    (tmp_path / "Copernicus_DSM_COG_10_N40_00_E116_00_EDM.tif").write_bytes(b"x")

    names = sorted(p.name for p in list_dem_tifs(tmp_path))
    assert names == [
        "ASTGTMV003_N00E000_dem.tif",
        "Copernicus_DSM_COG_10_N40_00_E116_00_DEM.tif",
    ]
