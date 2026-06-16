import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

gdal = pytest.importorskip("osgeo.gdal")
np = pytest.importorskip("numpy")
pytest.importorskip("matplotlib")

from services.contour_engine import build_contour_tiles, ContourStyle


def _make_dem(path, lon0=116.0, lat0=39.0):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(path), 60, 60, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((lon0, 1.0 / 60, 0, lat0 + 1.0, 0, -1.0 / 60))
    srs = gdal.osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    arr = np.tile(np.linspace(0, 6000, 60).astype("float32"), (60, 1))
    ds.GetRasterBand(1).WriteArray(arr)
    ds.FlushCache()
    ds = None


def _make_att(path, lon0=116.0, lat0=39.0):
    """Synthetic ASTWBD att: west half ocean (1), east half land (0)."""
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(path), 60, 60, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((lon0, 1.0 / 60, 0, lat0 + 1.0, 0, -1.0 / 60))
    srs = gdal.osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    att = np.zeros((60, 60), dtype="uint8")
    att[:, :30] = 1  # west half = ocean
    ds.GetRasterBand(1).WriteArray(att)
    ds.FlushCache()
    ds = None


def test_build_contour_tiles_emits_png(tmp_path):
    dem = tmp_path / "ASTGTMV003_N39E116_dem.tif"
    _make_dem(dem)
    out = tmp_path / "contour_tiles"

    counts = build_contour_tiles(
        dem_tifs=[dem], out_dir=out, interval=50,
        zoom_min=10, zoom_max=11, style=ContourStyle(),
    )

    assert counts["total"] >= 1
    assert counts["rendered"] >= 1
    pngs = list(out.rglob("*.png"))
    assert len(pngs) >= 1
    assert pngs[0].stat().st_size > 0


def test_build_contour_tiles_shade_and_water(tmp_path):
    from PIL import Image

    dem = tmp_path / "ASTGTMV003_N39E116_dem.tif"
    _make_dem(dem)
    att = tmp_path / "ASTWBDV001_N39E116_att.tif"
    _make_att(att)
    out = tmp_path / "tiles"

    counts = build_contour_tiles(
        dem_tifs=[dem], out_dir=out, interval=50,
        zoom_min=10, zoom_max=11, style=ContourStyle(),
        shade=True, water=True, att_tifs=[att],
    )

    assert counts["rendered"] >= 1
    pngs = list(out.rglob("*.png"))
    assert pngs

    # Hypsometric fill -> tiles are colored & opaque, not blank/transparent.
    colored = False
    for p in pngs:
        a = np.asarray(Image.open(p).convert("RGBA"))
        if (a[:, :, 3] > 0).any() and len(np.unique(a.reshape(-1, 4), axis=0)) > 2:
            colored = True
            break
    assert colored

    # Water layer actually paints ocean blue (107,174,214) somewhere.
    ocean = np.array([107, 174, 214])
    found_water = False
    for p in pngs:
        a = np.asarray(Image.open(p).convert("RGBA"))
        opaque = a[:, :, 3] > 200
        if opaque.any():
            diff = np.abs(a[:, :, :3].astype(int) - ocean).sum(axis=2)
            if ((diff < 12) & opaque).any():
                found_water = True
                break
    assert found_water
