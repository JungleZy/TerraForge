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


def test_build_contour_tiles_parallel_matches_serial(tmp_path):
    """workers>1 的并行渲染必须与串行(workers=1)产出完全一致:相同的瓦片计数、
    相同的 PNG 文件集合。证明并行只是加速,不改变结果。"""
    dem = tmp_path / "ASTGTMV003_N39E116_dem.tif"
    _make_dem(dem)

    def run(workers, sub):
        out = tmp_path / sub
        counts = build_contour_tiles(
            dem_tifs=[dem], out_dir=out, interval=50,
            zoom_min=10, zoom_max=11, style=ContourStyle(), shade=True,
            workers=workers,
        )
        rels = sorted(str(p.relative_to(out)) for p in out.rglob("*.png"))
        return counts, rels

    c_serial, r_serial = run(1, "serial")
    c_par, r_par = run(2, "parallel")

    # total>4 才会真正走并行分支(否则回退串行),确保测到的是并行路径
    assert c_serial["total"] > 4
    assert c_serial["rendered"] == c_par["rendered"] >= 1
    assert c_serial["failed"] == c_par["failed"] == 0
    assert c_serial["total"] == c_par["total"]
    assert r_serial == r_par  # 渲染出的瓦片集合逐一相同


def test_absolute_hillshade_independent_of_neighborhood():
    """同级瓦片色差根因回归:光照强度必须只由局部坡度坡向决定,与瓦片内是否
    存在更陡/更平的邻域无关 —— matplotlib LightSource 的逐瓦片 min/max 拉伸会
    违反这一点,造成相邻瓦片明暗基准不一致。"""
    from services.contour_engine import absolute_hillshade_intensity

    flat = np.full((10, 10), 500.0)
    slope = np.tile(np.linspace(0.0, 600.0, 10), (10, 1))  # 东向斜坡
    big = np.hstack([flat, slope])  # 平坦 + 斜坡拼接(模拟带陡峭邻域的瓦片)

    i_big = absolute_hillshade_intensity(big, 315.0, 45.0, 1.0, 30.0, 30.0)
    i_flat = absolute_hillshade_intensity(flat, 315.0, 45.0, 1.0, 30.0, 30.0)

    # 平坦半区的强度不应因相邻斜坡而改变(避开拼接边界最后一列)
    assert np.allclose(i_big[:, :9], i_flat[:, :9], atol=1e-6)
    # 平地强度统一归到 0.5(中性),与任何邻域无关
    assert np.allclose(i_flat, 0.5, atol=1e-6)


def test_build_contour_tiles_no_facecolor_bleed_at_edges(tmp_path):
    """方案(a)回归:slippy 瓦片网格与 DEM 矩形不对齐时,最外圈瓦片的出界部分
    必须透明,而不是被 figure facecolor 填成背景色(高 zoom '四周白边')。"""
    from PIL import Image

    dem = tmp_path / "ASTGTMV003_N39E116_dem.tif"
    _make_dem(dem)
    out = tmp_path / "tiles"

    # 醒目的非透明背景色当哨兵:出界若被 facecolor 填充会留下该色 opaque 像素。
    style = ContourStyle(background="#FF00FF")  # 品红,不与 hypsometric/水色撞色
    counts = build_contour_tiles(
        dem_tifs=[dem], out_dir=out, interval=50,
        zoom_min=10, zoom_max=11, style=style, shade=True,
    )
    assert counts["rendered"] >= 1
    pngs = list(out.rglob("*.png"))
    assert pngs

    sentinel = np.array([255, 0, 255])
    has_transparent = False
    for p in pngs:
        a = np.asarray(Image.open(p).convert("RGBA"))
        # 出界区域应透明(存在 alpha==0 像素 = 边缘瓦片被正确透明化)
        if (a[:, :, 3] == 0).any():
            has_transparent = True
        # 不得出现哨兵背景色的不透明像素(facecolor 渗透到出界区域)
        opaque = a[:, :, 3] > 200
        diff = np.abs(a[:, :, :3].astype(int) - sentinel).sum(axis=2)
        assert not ((diff < 12) & opaque).any(), f"facecolor bleed in {p.name}"

    # shade 模式下 DEM 矩形必然有部分出界的边缘瓦片 -> 必须出现透明像素
    assert has_transparent, "expected transparent out-of-coverage pixels at tile edges"


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
