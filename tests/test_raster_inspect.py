"""「选完 tif 立刻看到有效信息」的契约（本地高程切片 + 等高线共用）。

三层：
1. src/services/raster_probe.describe_headers —— 纯解释层，输入是
   static/js/geotiff_meta.js 读出的 GeoTIFF 头部标签；
2. POST /api/raster/inspect —— 路由外壳（成功形状、坏输入 400）；
3. node 端到端 —— 用真 GeoTIFF 跑一遍浏览器侧解析器，再把它的输出喂给解释层。
   JS 与 Python 之间那份字段契约没有类型系统看管，只有这一条守着：改了任一侧
   的字段名，这里就红。

为什么这条链上没有「上传文件」：DEM 动辄几百 MB，为了看一眼元信息先整包传一遍
是不可接受的。浏览器只读几 KB 的 TIFF 目录，发过来的是标签而不是文件。

mode 是这里最值得盯的一件事：两条切片管线的分块方式不同（高程走 Cesium 的
经纬度分块、等高线走 Web Mercator 瓦片），同一份 DEM 给出的建议层级不一样。
传错 mode 的卡片会写一个与实际切片对不上的数字 —— 比不显示更糟。
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import geotiff_bytes  # noqa: E402


def _has_osr():
    """osgeo 在不在。

    **不能**在模块级 importorskip：那会把纯 Python 的输入校验与路由外壳用例
    一起跳掉 —— 400 语义在没有 GDAL 的机器上就此无人看管。只有真需要重投影的
    断言才挂 requires_gdal。
    """
    try:
        from osgeo import osr  # noqa: F401
    except Exception:
        return False
    return True


requires_gdal = pytest.mark.skipif(
    not _has_osr(), reason="osgeo.osr 不可用，跳过需要真重投影的断言")


def _node_runs_the_harness():
    """端到端 harness 用了全局 File 构造器 —— 那是 Node >= 20 才有的。

    只探 shutil.which("node") 的话，Node 18 上失败的是一个看不出所以然的
    CalledProcessError，而不是一条「版本不够」的跳过原因。
    """
    if shutil.which("node") is None:
        return False
    try:
        probe = subprocess.run(["node", "-p", "typeof File"],
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.stdout.strip() == "function"


from src.i18n import t  # noqa: E402
from src.services.raster_probe import (  # noqa: E402
    MAX_INSPECT_BODY, MAX_INSPECT_FILES, MAX_ZOOM, InspectError, describe_headers,
)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 1 弧秒 —— SRTM / Copernicus GLO-30 / ASTER 的标称格网
_ARCSEC = 1.0 / 3600.0


def _geographic(**over):
    """一块 EPSG:4326 的 1 弧秒 DEM 头部，形状与 geotiff_meta.js read() 一致。"""
    entry = {
        "name": "n40e110_dem.tif",
        "size": 26_000_000,
        "big_tiff": False,
        "width": 3601,
        "height": 3601,
        "samples": 1,
        "bits": 32,
        "sample_format": 3,
        "compression": 8,
        "pixel_scale": [_ARCSEC, _ARCSEC, 0],
        "tie_point": [0, 0, 0, 110.0, 41.0, 0],
        "geo_keys": {"1024": 2, "2048": 4326, "2049": "WGS 84"},
        "nodata": -32768.0,
        "statistics": None,
    }
    entry.update(over)
    return entry


def _projected(**over):
    """一块 EPSG:32650（WGS84 / UTM 50N）的 30 m DEM 头部。"""
    entry = dict(_geographic(),
                 name="utm50n_dem.tif",
                 width=1000,
                 height=1000,
                 pixel_scale=[30.0, 30.0, 0],
                 tie_point=[0, 0, 0, 500000.0, 4400000.0, 0],
                 geo_keys={"1024": 1, "3072": 32650, "3076": 9001})
    entry.update(over)
    return entry


def _antimeridian(**over):
    """一块 EPSG:32660（WGS84 / UTM 60N）的 30 m DEM，横跨 180° 经线。

    60 带的中央经线是 177°E，东移到 640 km 处正好骑在 180° 上 —— 楚科奇、
    白令海、斐济、太平洋岛链的 DEM 都长这样。
    """
    entry = dict(_projected(),
                 name="utm60n_dem.tif",
                 tie_point=[0, 0, 0, 640000.0, 7030000.0, 0],
                 geo_keys={"1024": 1, "3072": 32660, "3076": 9001})
    entry.update(over)
    return entry


# ------------------------------------------------------------------ 解释层

@requires_gdal
def test_geographic_dem_reports_crs_bounds_resolution_and_zoom():
    file = describe_headers([_geographic()])["files"][0]

    assert file["warnings"] == []
    assert file["epsg"] == 4326
    assert file["crs_name"] == "WGS 84"
    assert file["crs_unit"] == "degree"
    assert (file["width"], file["height"]) == (3601, 3601)
    assert file["dtype"] == "Float32"
    assert file["bands"] == 1
    assert file["nodata"] == -32768.0
    assert file["compression"] == "Deflate"

    west, south, east, north = file["bounds_wgs84"]
    assert (west, north) == pytest.approx((110.0, 41.0))
    # 3601 个 1 弧秒像元正好 1 度 —— granule 的标准尺寸
    assert (east, south) == pytest.approx((110.0 + 3601 * _ARCSEC,
                                           41.0 - 3601 * _ARCSEC))

    # 一度纬度处处 ≈111 km：1 弧秒就该显示成 ~30 m，这是用户认得出的标称值
    assert file["pixel_meters"] == pytest.approx(30.9, abs=0.1)
    # 与切片管线同一算法：tile_size=65 的顶点网格追上 1 弧秒源像素正好是 z14
    assert file["recommended_maxzoom"] == 14


@requires_gdal
def test_projected_dem_is_reported_in_wgs84_and_flagged_reprojected():
    file = describe_headers([_projected()])["files"][0]

    assert file["epsg"] == 32650
    assert file["crs_name"] == "WGS 84 / UTM zone 50N"
    assert file["crs_unit"] in ("metre", "meter", "m")
    # 原生范围是米，界面另外还要能给出经纬度 —— 否则用户看不出数据在哪
    assert file["bounds_native"][0] == pytest.approx(500000.0)
    west, south, east, north = file["bounds_wgs84"]
    # 1000 × 30 m = 30 km，从北边 4400000 m 往南铺
    assert west == pytest.approx(117.0, abs=0.01)
    assert south == pytest.approx(39.479, abs=0.01)
    assert east > west and north > south

    assert file["pixel_meters"] == pytest.approx(30.0)
    # 切片前 gdalwarp 会重投影，建议层级必须按重投影后的度像素算
    assert "reprojected" in file["warnings"]
    assert file["recommended_maxzoom"] == 13


# ------------------------------------------------------------------ mode

@requires_gdal
def test_contour_mode_uses_the_contour_pipelines_own_formula(monkeypatch):
    """等高线的建议层级必须由 contour_task_manager 自己那套算出来。

    今天两条管线算出来的数**恰好一样** —— 65 顶点网格的 180/64 = 2.8125 度每
    瓦片像素，与 Web Mercator z0 的 156543.03 米每像素 + 1 级过采样在数值上
    等价（156543.03/111320 ≈ 2.8125/2）。所以只钉数字什么也钉不住：写死一条
    公式给两边用，今天照样全绿，等哪天 tile_size 或过采样级数改了才悄悄错开。
    这里直接把等高线那个函数换掉，看结果跟不跟着走。
    """
    import src.services.contour_task_manager as ctm
    monkeypatch.setattr(ctm, "estimate_max_zoom", lambda px, zoom_min: 7)

    assert describe_headers([_geographic()], mode="contour")["files"][0][
        "recommended_maxzoom"] == 7
    # 高程切片走另一条，不受影响
    assert describe_headers([_geographic()])["files"][0]["recommended_maxzoom"] == 14


@requires_gdal
def test_contour_mode_is_fed_pixel_size_in_web_mercator_metres():
    """等高线按 EPSG:3857 的米/像素定层级，喂错单位会差好几级。"""
    from src.services.contour_task_manager import estimate_max_zoom

    file = describe_headers([_geographic()], mode="contour")["files"][0]
    assert file["pixel_3857"] == pytest.approx(30.9, abs=0.5)
    assert file["recommended_maxzoom"] == estimate_max_zoom(file["pixel_3857"], 0)


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        describe_headers([_geographic()], mode="voxel")


# ------------------------------------------------------------------ 解释层（续）

def test_missing_georeference_is_fatal():
    """没有像元大小/绑定点 = GDAL 会把它当像素坐标，切出来落在几内亚湾。"""
    file = describe_headers([_geographic(pixel_scale=None, tie_point=None)])["files"][0]

    assert "no_georeference" in file["warnings"]
    assert "bounds_wgs84" not in file
    assert "recommended_maxzoom" not in file
    # 尺寸/数据类型这些不依赖地理参考的信息照旧要报出来
    assert file["width"] == 3601
    assert file["dtype"] == "Float32"


def test_user_defined_crs_is_reported_as_unknown():
    """32767 是 GeoTIFF 的「用户自定义」哨兵值，等于没有 EPSG 码。"""
    file = describe_headers([_geographic(geo_keys={"1024": 1, "3072": 32767})])["files"][0]

    assert "unknown_crs" in file["warnings"]
    assert "epsg" not in file


def test_missing_geokeys_is_reported_as_unknown_crs():
    file = describe_headers([_geographic(geo_keys=None)])["files"][0]
    assert "unknown_crs" in file["warnings"]


@requires_gdal
def test_model_transformation_matches_the_pixel_scale_form():
    """两种写法（33550+33922 与 34264）必须解出同一个范围。

    GDAL 在 y 轴朝上等不能用像元大小表达的情形下改写 ModelTransformation，
    只认前一种就会把这些文件误判成「缺少地理参考」。
    """
    scale_form = describe_headers([_geographic()])["files"][0]
    matrix_form = describe_headers([_geographic(
        pixel_scale=None,
        tie_point=None,
        transform=[_ARCSEC, 0, 0, 110.0,
                   0, -_ARCSEC, 0, 41.0,
                   0, 0, 0, 0,
                   0, 0, 0, 1],
    )])["files"][0]

    assert matrix_form["bounds_wgs84"] == pytest.approx(scale_form["bounds_wgs84"])
    assert matrix_form["recommended_maxzoom"] == scale_form["recommended_maxzoom"]


@requires_gdal
def test_rotated_raster_is_a_warning_not_a_rejection():
    file = describe_headers([_geographic(
        pixel_scale=None,
        tie_point=None,
        transform=[_ARCSEC, 1e-5, 0, 110.0,
                   1e-5, -_ARCSEC, 0, 41.0,
                   0, 0, 0, 0,
                   0, 0, 0, 1],
    )])["files"][0]

    assert "rotated" in file["warnings"]
    assert file["bounds_wgs84"]


@requires_gdal
def test_multi_band_is_a_warning_not_an_error():
    """切片只读第 1 波段（DemSampler），多波段能切但结果可能不是用户想的。"""
    file = describe_headers([_geographic(samples=3)])["files"][0]

    assert file["bands"] == 3
    assert "multi_band" in file["warnings"]
    assert file["recommended_maxzoom"] == 14


def test_gdal_statistics_become_an_elevation_range():
    file = describe_headers([_geographic(
        statistics={"min": -12.5, "max": 8848.86})])["files"][0]
    assert file["elevation"] == {"min": -12.5, "max": 8848.86}


def test_no_statistics_means_no_elevation_row():
    """GDAL 没算过统计的 tif 里就是没有这个数 —— 不能编一个出来。"""
    assert "elevation" not in describe_headers([_geographic()])["files"][0]


def test_integer_dem_dtype_is_named():
    """SRTM 原始 hgt 转出来的 tif 是 Int16，不能显示成 `16-bit`。"""
    file = describe_headers([_geographic(bits=16, sample_format=2)])["files"][0]
    assert file["dtype"] == "Int16"


@requires_gdal
def test_unreadable_entry_does_not_break_its_siblings():
    """浏览器读不出头部时只发文件名和大小，其余文件照常解释。"""
    result = describe_headers([{"name": "broken.tif", "size": 10}, _geographic()])

    broken, good = result["files"]
    assert broken["warnings"] == ["header_unreadable"]
    assert good["recommended_maxzoom"] == 14
    assert "some_unusable" in result["summary"]["warnings"]


# ------------------------------------------------------------------ 多文件总览

@requires_gdal
def test_summary_unions_bounds_and_keeps_the_finest_pixel():
    """切片按并集切，粗的那份被拉伸，细的那份不能被牺牲 —— 建议层级取最细。"""
    coarse = _geographic(name="coarse.tif", pixel_scale=[3 * _ARCSEC, 3 * _ARCSEC, 0],
                         tie_point=[0, 0, 0, 111.0, 41.0, 0], size=3_000_000)
    result = describe_headers([_geographic(), coarse])
    summary = result["summary"]

    assert summary["count"] == 2
    assert summary["total_size"] == 26_000_000 + 3_000_000
    assert summary["bounds_wgs84"][0] == pytest.approx(110.0)
    assert summary["bounds_wgs84"][2] == pytest.approx(111.0 + 3601 * 3 * _ARCSEC)
    assert summary["pixel_deg"] == pytest.approx(_ARCSEC)
    assert summary["recommended_maxzoom"] == 14
    assert summary["warnings"] == []


def test_summary_flags_mixed_crs():
    """混着不同坐标系的文件多半是选错了，切片本身不会报错所以更要提前说。"""
    summary = describe_headers([_geographic(), _projected()])["summary"]
    assert "mixed_crs" in summary["warnings"]


def test_single_file_summary_has_no_mixed_crs_warning():
    assert describe_headers([_geographic()])["summary"]["warnings"] == []


# ------------------------------------------------------------------ 输入校验

@pytest.mark.parametrize("payload", [None, [], "files", {}])
def test_bad_payloads_raise_value_error(payload):
    with pytest.raises(ValueError):
        describe_headers(payload)


def test_too_many_files_is_rejected():
    with pytest.raises(ValueError):
        describe_headers([_geographic()] * (MAX_INSPECT_FILES + 1))


def test_non_object_entry_is_rejected():
    with pytest.raises(ValueError):
        describe_headers(["n40e110.tif"])


def test_garbage_field_values_do_not_crash():
    """字段来自浏览器，坏值只能降级不能 500。"""
    file = describe_headers([{
        "name": "junk.tif", "size": "big", "width": float("nan"),
        "height": None, "pixel_scale": ["a", "b", "c"], "geo_keys": "nope",
    }])["files"][0]
    assert file["warnings"] == ["header_unreadable"]
    assert file["size"] == 0


# ------------------------------------------------------------------ 路由

def _client(isolated_app):
    return isolated_app.app.test_client()


def test_inspect_route_returns_the_described_files(isolated_app):
    resp = _client(isolated_app).post(
        "/api/raster/inspect", json={"files": [_geographic()]})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["files"][0]["epsg"] == 4326
    assert body["summary"]["count"] == 1


@requires_gdal
def test_inspect_route_defaults_to_terrain_mode(isolated_app):
    """省略 mode 时按高程切片算 —— 那是这条接口的第一个调用方。"""
    body = _client(isolated_app).post(
        "/api/raster/inspect", json={"files": [_geographic()]}).get_json()
    assert body["files"][0]["recommended_maxzoom"] == 14


@requires_gdal
def test_inspect_route_honours_contour_mode(isolated_app, monkeypatch):
    """路由必须真的把 mode 传下去。

    原来这里断言 14 —— 那也是 terrain 的答案（两条管线今天数值上撞在一起，
    见 test_contour_mode_uses_the_contour_pipelines_own_formula），路由把 mode
    整个丢掉照样绿。换成一个两条管线都算不出来的数，手法与那条单测相同。
    """
    import src.services.contour_task_manager as ctm
    monkeypatch.setattr(ctm, "estimate_max_zoom", lambda px, zoom_min: 7)

    client = _client(isolated_app)
    contour = client.post("/api/raster/inspect",
                          json={"files": [_geographic()],
                                "mode": "contour"}).get_json()
    terrain = client.post("/api/raster/inspect",
                          json={"files": [_geographic()]}).get_json()

    assert contour["files"][0]["recommended_maxzoom"] == 7
    assert terrain["files"][0]["recommended_maxzoom"] == 14


def test_inspect_route_rejects_an_unknown_mode(isolated_app):
    resp = _client(isolated_app).post(
        "/api/raster/inspect", json={"files": [_geographic()], "mode": "voxel"})
    assert resp.status_code == 400


def test_inspect_route_rejects_an_empty_payload(isolated_app):
    resp = _client(isolated_app).post("/api/raster/inspect", json={})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_inspect_route_does_not_accept_uploads(isolated_app):
    """这条路由刻意不收文件 —— 收了就等于默认「先整包上传一遍」。"""
    resp = _client(isolated_app).post(
        "/api/raster/inspect",
        data={"files": (__import__("io").BytesIO(b"x"), "a.tif")},
        content_type="multipart/form-data")
    assert resp.status_code == 400


# ------------------------------------------------------------------ 端到端

_HARNESS = """
const fs = require('fs');
require(process.argv[2]);
(async () => {
  const buf = fs.readFileSync(process.argv[3]);
  const meta = await globalThis.GeoTiffMeta.read(new File([buf], 'dem.tif'));
  process.stdout.write(JSON.stringify(meta));
})();
"""


@requires_gdal
@pytest.mark.skipif(not _node_runs_the_harness(),
                    reason="node 缺席或版本低于 20（没有全局 File），跳过端到端断言")
def test_browser_parser_output_feeds_the_backend(tmp_path):
    """真 GeoTIFF -> geotiff_meta.js -> describe_headers 全链路。

    这是 JS 侧字段名与 Python 侧读取名唯一的对账点：任一侧改名，这里就红。
    """
    tif = tmp_path / "dem.tif"
    tif.write_bytes(geotiff_bytes(pixel_deg=_ARCSEC, lon0=110.0, lat0=41.0,
                                  width=64, height=64))
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    parser = os.path.join(PROJECT_ROOT, "static", "js", "geotiff_meta.js")

    try:
        out = subprocess.run(
            ["node", str(harness), parser, str(tif)],
            capture_output=True, text=True, check=True, timeout=120,
        ).stdout
    except subprocess.TimeoutExpired:
        pytest.skip("node 启动超过 120 秒（CI runner 冷启动）")

    meta = json.loads(out)
    assert meta["width"] == 64 and meta["height"] == 64
    assert meta["geo_keys"]["2048"] == 4326

    file = describe_headers([meta])["files"][0]
    assert file["warnings"] == []
    assert file["epsg"] == 4326
    assert file["bounds_wgs84"][0] == pytest.approx(110.0)
    assert file["bounds_wgs84"][3] == pytest.approx(41.0)
    assert file["recommended_maxzoom"] == 14



# --------------------------------------------------- 回归：坏输入不能变成 500

def test_a_json_array_body_is_a_400_not_a_500(isolated_app):
    """`[1,2,3]` 是合法 JSON 而且为真，但它没有 .get —— `or {}` 接不住。"""
    resp = _client(isolated_app).post(
        "/api/raster/inspect", data=json.dumps([1, 2, 3]),
        content_type="application/json")

    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_a_json_string_body_is_a_400_not_a_500(isolated_app):
    resp = _client(isolated_app).post(
        "/api/raster/inspect", data=json.dumps("files"),
        content_type="application/json")
    assert resp.status_code == 400


def test_an_arbitrary_precision_integer_degrades_instead_of_500(isolated_app):
    """JSON 的整数字面量是任意精度的：1 后面 400 个 0 解码出来是 int，
    float() 对它抛的是 OverflowError（**不是** ValueError）。

    必须用原始报文构造 —— 在 python 里写成 float 就退化成 inf 了，那是另一件事。
    """
    huge = "1" + "0" * 400
    body = ('{"files": [{"name": "huge.tif", "size": %s, "width": 8, '
            '"height": 8, "nodata": %s}]}' % (huge, huge))

    resp = _client(isolated_app).post(
        "/api/raster/inspect", data=body, content_type="application/json")

    assert resp.status_code == 200
    file = resp.get_json()["files"][0]
    assert file["size"] == 0
    assert file["nodata"] is None


def test_oversized_body_is_rejected_before_it_is_parsed(isolated_app):
    """MAX_INSPECT_FILES 要等 get_json 把整个体缓冲并解析完才生效，而上游只有
    2 GiB 的全局上限（那是给真上传定的）。这条接口一个文件都不收。"""
    body = '{"files": [{"name": "' + "x" * (MAX_INSPECT_BODY + 64) + '"}]}'
    resp = _client(isolated_app).post(
        "/api/raster/inspect", data=body, content_type="application/json")

    assert resp.status_code == 413
    assert resp.get_json()["error"] == t("api.raster.body_too_large", locale="zh")


# --------------------------------------------------- 回归：错误文案必须是译文

def test_input_errors_carry_a_catalog_key_not_english_prose():
    """服务层不许再抛英文原文：键在 catalog 里，参数分开带。"""
    with pytest.raises(InspectError) as exc:
        describe_headers([_geographic()], mode="voxel")

    assert exc.value.key == "api.raster.unknown_mode"
    # 继承 ValueError，既有的 except ValueError 调用方照旧接得住
    assert isinstance(exc.value, ValueError)


def test_too_many_files_reports_the_translated_message(isolated_app):
    """map.js 把这段字直接写进信息卡 —— 回英文原文就是中文界面上一句生英文。"""
    resp = _client(isolated_app).post(
        "/api/raster/inspect",
        json={"files": [_geographic()] * (MAX_INSPECT_FILES + 1)})

    assert resp.status_code == 400
    error = resp.get_json()["error"]
    assert error == t("api.raster.too_many_files", locale="zh",
                      max=MAX_INSPECT_FILES)
    assert str(MAX_INSPECT_FILES) in error
    assert "Too many files" not in error


def test_exactly_the_file_cap_is_accepted(isolated_app):
    """上限那一格从来没被测过 —— off-by-one 会把合法的一百个文件整批拒掉。"""
    resp = _client(isolated_app).post(
        "/api/raster/inspect",
        json={"files": [_geographic()] * MAX_INSPECT_FILES})

    assert resp.status_code == 200
    assert resp.get_json()["summary"]["count"] == MAX_INSPECT_FILES


# --------------------------------------------------- 回归：坐标系解释

def test_projected_user_defined_crs_never_borrows_the_base_geographic_crs():
    """1024=1（投影）+ 3072=32767（自定义）时，2048 说的是投影所基于的大地
    坐标系，不是像素单位 —— 而它在投影栅格上**永远**在。

    国产 GIS 导出的自定义 Albers/兰勃特/高斯克吕格 DEM 全长这样。拿 2048 顶
    上去，米级东北坐标会被当成度解释，报出一个自信的、完全错的 WGS84 范围，
    还一条警告都没有。诚实的 unknown_crs 才是对的。
    """
    file = describe_headers([_projected(
        geo_keys={"1024": 1, "3072": 32767, "2048": 4490, "3076": 9001},
    )])["files"][0]

    assert "unknown_crs" in file["warnings"]
    assert "bounds_wgs84" not in file
    assert "epsg" not in file


def test_geographic_model_type_still_reads_the_geographic_key():
    """别矫枉过正：1024=2 时 2048 说的就是这个栅格自己的坐标系。"""
    assert describe_headers([_geographic()])["files"][0]["epsg"] == 4326


@requires_gdal
def test_antimeridian_crossing_is_a_narrow_extent_not_a_global_one():
    """osr 把经度规整进 [-180,180]：跨 180° 的栅格角点是 179.8 与 -179.6，
    naive min/max 报出来是 359 度宽 —— 界面上等于「这份 DEM 覆盖全球」。"""
    file = describe_headers([_antimeridian()])["files"][0]
    west, _south, east, _north = file["bounds_wgs84"]

    assert "antimeridian" in file["warnings"]
    assert west > 179.0
    assert east > 180.0            # 东边界按 +360 展开，180.4 即 -179.6
    assert east - west < 1.0       # 30 km 宽，不是 359 度


@requires_gdal
def test_the_summary_unions_across_the_antimeridian():
    """两个各自不跨界的文件（179..180 与 -180..-179）合起来是跨界的 ——
    合并那一步要独立地过同一关。"""
    summary = describe_headers([
        _antimeridian(name="east.tif",
                      tie_point=[0, 0, 0, 600000.0, 7030000.0, 0]),
        _antimeridian(name="west.tif",
                      tie_point=[0, 0, 0, 680000.0, 7030000.0, 0]),
    ])["summary"]
    west, _south, east, _north = summary["bounds_wgs84"]

    assert "antimeridian" in summary["warnings"]
    assert east - west < 3.0


@requires_gdal
def test_an_unresolvable_epsg_does_not_blame_a_healthy_gdal():
    """12345 不在 PROJ 库里。以前这里报的是「服务端缺少 GDAL」，等于让一个
    装得好好的用户去查一个健康的安装。"""
    file = describe_headers([_projected(
        geo_keys={"1024": 1, "3072": 12345, "3076": 9001})])["files"][0]

    assert "crs_unresolved" in file["warnings"]
    assert "gdal_unavailable" not in file["warnings"]
    assert "bounds_wgs84" not in file
    # 原生范围照报 —— 此时它是唯一还说得准的东西
    assert file["bounds_native"][0] == pytest.approx(500000.0)


def test_a_genuinely_missing_gdal_still_says_gdal_is_missing(monkeypatch):
    """另一半：真的 import 不到 osgeo 时，文案必须还是「缺少 GDAL」。"""
    import builtins
    real_import = builtins.__import__

    def no_osgeo(name, *args, **kwargs):
        if name == "osgeo" or name.startswith("osgeo."):
            raise ImportError("no osgeo here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_osgeo)
    file = describe_headers([_projected()])["files"][0]

    assert "gdal_unavailable" in file["warnings"]
    assert "crs_unresolved" not in file["warnings"]


# --------------------------------------------------- 回归：范围本身要讲得通

_BOUNDS_CASES = {
    "geographic": lambda: [_geographic()],
    "projected": lambda: [_projected()],
    "antimeridian": lambda: [_antimeridian()],
    "rotated": lambda: [_geographic(
        pixel_scale=None, tie_point=None,
        transform=[_ARCSEC, 1e-5, 0, 110.0,
                   1e-5, -_ARCSEC, 0, 41.0,
                   0, 0, 0, 0,
                   0, 0, 0, 1])],
    "mixed_crs": lambda: [_geographic(), _projected()],
    "antimeridian_pair": lambda: [
        _antimeridian(name="east.tif",
                      tie_point=[0, 0, 0, 600000.0, 7030000.0, 0]),
        _antimeridian(name="west.tif",
                      tie_point=[0, 0, 0, 680000.0, 7030000.0, 0]),
    ],
}


@requires_gdal
@pytest.mark.parametrize("case", sorted(_BOUNDS_CASES))
def test_every_reported_bounds_is_a_sane_wgs84_box(case):
    """不钉任何一个具体数字，只钉「这个盒子讲得通」。

    钉死数字的用例改一处口径就得跟着改一处，而轴序颠倒（osr 对 4326 的缺省
    是 lat,lon）、经度环绕这类回归恰恰不会被某一个具体数字抓住。上界取 360
    而不是 180：跨 180° 的东边界是刻意 +360 展开的（见 warn_antimeridian）。
    """
    result = describe_headers(_BOUNDS_CASES[case]())
    boxes = [f["bounds_wgs84"] for f in result["files"] if f.get("bounds_wgs84")]
    if result["summary"].get("bounds_wgs84"):
        boxes.append(result["summary"]["bounds_wgs84"])
    assert boxes, "这批输入本该报得出范围"

    for west, south, east, north in boxes:
        assert -180.0 <= west < east <= 360.0, f"经度不成立: {west}..{east}"
        assert -90.0 <= south < north <= 90.0, f"纬度不成立: {south}..{north}"

# --------------------------------------------------- 规模预告：逐层瓦片数

@requires_gdal
def test_summary_reports_per_level_tile_counts():
    """逐层、不累加 —— 累加区间留给消费方，因为随包底图可用与否会改变起点。"""
    counts = describe_headers([_geographic()], mode="terrain")["summary"]["tile_counts"]

    assert len(counts) == MAX_ZOOM + 1
    # z0 的瓦片方案是 2x1 全球，任何 bbox 至少落在一张里
    assert counts[0] >= 1
    # 每加一级，x/y 各翻倍 → 张数趋近 4 倍
    assert counts[14] > counts[13] > counts[12]


@requires_gdal
def test_tile_counts_match_the_tiler_geometry():
    """与切片器用的是同一套 intersecting_tile_range，不许各算各的。

    这条走的是完整链路（头部 -> 并集 bounds -> 表），因此能抓住「换了分块方案」
    「乘积公式写错」「bounds 换了来源或轴序」这几类退化。

    它**抓不住** floor/ceil-1 之争：`ceil(t)-1` 只在 t 恰为整数时才与 `floor(t)`
    不同，而 _geographic() 的四至（110, 39.9997.., 111.0002.., 41）z0..z21 每一级
    都不落在瓦片边界上，两种取整结果处处相同。那个 hazard 由下面那条用例看管。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import (
        GeographicTilingScheme, intersecting_tile_range)

    summary = describe_headers([_geographic()], mode="terrain")["summary"]
    west, south, east, north = summary["bounds_wgs84"]
    # tile_count 只看 level，tile_size 不参与计数；给 65 是与切片管线同口径
    scheme = GeographicTilingScheme(tile_size=65)

    for z in (8, 12, 14):
        nx, ny = scheme.tile_count(z)
        ix0, ix1, iy0, iy1 = intersecting_tile_range(nx, ny, west, south, east, north)
        assert summary["tile_counts"][z] == (ix1 - ix0 + 1) * (iy1 - iy0 + 1), \
            f"z{z} 的预告与切片器的几何对不上"


@requires_gdal
def test_tile_counts_use_ceil_minus_one_on_a_tile_boundary():
    """四至压在瓦片边界上时，上界必须是 ceil-1 而不是 floor。

    这是「预告的数 == 实际切出来的数」的判别性证据。北界取 45.0：
    (45+90)/180 * 2^z = 0.75 * 2^z 对 z>=2 恒为整数，正好落在瓦片行边界上，
    floor 会把一整行零重叠的瓦片算进来（论证见 intersecting_tile_range 的
    docstring）—— 两种取整在 z2..z21 每一级都分道扬镳。

    **刻意不走 describe_headers**：那条链上 TransformBounds 会把 45.0 抖成
    44.999999..，边界条件当场消失，这条用例就又变回没牙的了。

    顺带钉住三个绝对值 —— 其余用例全是相对断言（长度、单调、>=1），bounds 口径
    整体漂移时它们一致地跟着漂，不会叫。
    """
    from src.services.raster_probe import _tile_counts_per_level

    counts = _tile_counts_per_level((110.0, 40.0, 111.0, 45.0))

    assert len(counts) == MAX_ZOOM + 1
    # 左边是 ceil-1（正确），右边是 floor 会给出的数：多出的正是贴着北界那一行
    for z, correct, floor_would_give in ((8, 16, 18), (12, 2622, 2645), (14, 41952, 42044)):
        assert counts[z] != floor_would_give, \
            f"z{z} 报了 {counts[z]}，正是 floor 的结果 —— 北界那一整行与 DEM 零重叠"
        assert counts[z] == correct, f"z{z} 期望 {correct}，实得 {counts[z]}"


@requires_gdal
def test_contour_mode_does_not_report_tile_counts():
    """等高线走 Web Mercator，这张表对它没有意义，给了只会被误用。"""
    entries = [_geographic()]
    # 同一份输入在高程模式下是有这张表的 —— 差别只能来自 mode，不能来自环境
    assert "tile_counts" in describe_headers(entries, mode="terrain")["summary"]
    assert "tile_counts" not in describe_headers(entries, mode="contour")["summary"]


def test_tile_counts_are_omitted_without_a_georeference():
    """没有地理参考就没有并集范围 —— 不给这张表，也不能因此抛异常。"""
    summary = describe_headers(
        [_geographic(pixel_scale=None, tie_point=None)], mode="terrain")["summary"]

    assert "bounds_wgs84" not in summary
    assert "tile_counts" not in summary
