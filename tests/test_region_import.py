"""region_import —— 上传的区域文件 → RegionSpec，含解析炸弹与坐标系闸门。

威胁模型（§13-5）里最现实的攻击面之一就是「用户打开一个恶意文件」。这个模块
的每一道闸都对应一种已经在别处出过事的形态：

- **DOCTYPE**：十来层互相引用的内部实体能让几 KB 的 KML 展开成几 GB 的字符串，
  ElementTree 会老老实实展开到内存耗尽 —— 进程被一个上传打死。
- **32 MiB 上限**：全局的 `MAX_CONTENT_LENGTH` 是 2 GiB（给本地地形上传留的），
  套在这里等于允许对方先让服务端把 2 GiB 缓进内存再被拒。
- **非 WGS84 的 crs 成员**：一份 EPSG:4490 的面**数值上完全像 WGS84**，静默接受
  的结果是整个区域平移几十到几百米，下载完成、图看着也对，错位要到叠加时才发现。

同时钉住「多要素合并成一个 MultiPolygon」和「洞环真的传下去」—— 只取外环 /
只取第一个要素都是 GeoDownloader 踩过的形态。
"""
import io
import json
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.region_import import (  # noqa: E402
    MAX_IMPORT_BYTES,
    RegionImportError,
    import_region,
)


def _has_ogr():
    """osgeo.ogr 在不在。

    **不能**在模块级 importorskip：那会把 GeoJSON / KML / 闸门这一大半纯 Python
    的用例一起跳掉，而它们恰恰是没有 GDAL 的机器上唯一还能守住的部分。
    """
    try:
        from osgeo import ogr  # noqa: F401
    except Exception:
        return False
    return True


requires_gdal = pytest.mark.skipif(
    not _has_ogr(), reason="osgeo.ogr 不可用，跳过需要真 shapefile 读取的断言")


def _zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# GeoJSON
# ---------------------------------------------------------------------------

GEOJSON_WITH_HOLE = json.dumps({
    'type': 'Feature',
    'properties': {'name': '带洞的区域'},
    'geometry': {
        'type': 'Polygon',
        'coordinates': [
            [[116.0, 39.0], [117.0, 39.0], [117.0, 40.0], [116.0, 40.0], [116.0, 39.0]],
            [[116.3, 39.3], [116.7, 39.3], [116.7, 39.7], [116.3, 39.7], [116.3, 39.3]],
        ],
    },
}).encode('utf-8')


def test_geojson_hole_reaches_the_region_spec():
    """洞环必须一路传到 RegionSpec —— 「只取外环」是 GeoD 那个 bug 的根因。"""
    spec, warnings = import_region('district.geojson', GEOJSON_WITH_HOLE)
    assert spec.hole_count == 1
    assert spec.contains_point(116.5, 39.5) is False    # 洞里
    assert spec.contains_point(116.1, 39.1) is True     # 洞外
    assert spec.source == 'imported'
    assert spec.display_name == 'district'
    assert warnings == ()


def test_feature_collection_merges_into_one_multipolygon():
    """用户选了一个文件，要的就是这个文件覆盖的整个范围，不是「第一个要素」。"""
    payload = json.dumps({
        'type': 'FeatureCollection',
        'features': [
            {'type': 'Feature', 'geometry': {
                'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}},
            {'type': 'Feature', 'geometry': {
                'type': 'Polygon', 'coordinates': [[[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]]}},
        ],
    }).encode('utf-8')
    spec, _warnings = import_region('two.geojson', payload)
    assert spec.polygon_count == 2
    assert spec.bbox == (6.0, 0.0, 6.0, 0.0)


def test_geojson_without_any_polygon_is_refused():
    """一个面都没有就抛错，绝不静默产出空区域（那会一路走到下载阶段才暴露）。"""
    payload = json.dumps({'type': 'Feature', 'geometry': {
        'type': 'Point', 'coordinates': [116.0, 39.0]}}).encode('utf-8')
    with pytest.raises(RegionImportError):
        import_region('point.geojson', payload)


@pytest.mark.parametrize('crs_member', [
    {'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:EPSG::3857'}},
    {'type': 'name', 'properties': {'name': 'EPSG:4490'}},
    {'type': 'EPSG', 'properties': {'code': 4214}},
])
def test_a_declared_non_wgs84_crs_is_refused(crs_member):
    """EPSG:4490 / 北京54 的面数值上完全像 WGS84 —— 放行就是静默平移几百米。"""
    obj = json.loads(GEOJSON_WITH_HOLE)
    obj['crs'] = crs_member
    with pytest.raises(RegionImportError, match='WGS84'):
        import_region('projected.geojson', json.dumps(obj).encode('utf-8'))


@pytest.mark.parametrize('crs_member', [
    {'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:OGC:1.3/CRS84'}},
    {'type': 'name', 'properties': {'name': 'EPSG:4326'}},
    {'type': 'name', 'properties': {}},          # 空壳：按 RFC 7946 默认走
])
def test_wgs84_synonyms_are_accepted(crs_member):
    """2008 版的 crs 成员仍在流通（QGIS 至今还写 CRS84），不能一刀切拒绝。"""
    obj = json.loads(GEOJSON_WITH_HOLE)
    obj['crs'] = crs_member
    spec, _warnings = import_region('ok.geojson', json.dumps(obj).encode('utf-8'))
    assert spec.hole_count == 1


def test_invalid_json_is_a_user_input_error():
    with pytest.raises(RegionImportError, match='invalid GeoJSON'):
        import_region('broken.geojson', b'{"type": "Feature"')


# ---------------------------------------------------------------------------
# KML / KMZ
# ---------------------------------------------------------------------------

KML_WITH_HOLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>\xe6\xb5\x8b\xe8\xaf\x95\xe5\x8c\xba</name>
    <Placemark>
      <Polygon>
        <outerBoundaryIs><LinearRing><coordinates>
          116.0,39.0 117.0,39.0 117.0,40.0 116.0,40.0 116.0,39.0
        </coordinates></LinearRing></outerBoundaryIs>
        <innerBoundaryIs><LinearRing><coordinates>
          116.3,39.3 116.7,39.3 116.7,39.7 116.3,39.7 116.3,39.3
        </coordinates></LinearRing></innerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""

KML_MULTIGEOMETRY = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <MultiGeometry>
        <Polygon><outerBoundaryIs><LinearRing><coordinates>
          0,0 1,0 1,1 0,1 0,0
        </coordinates></LinearRing></outerBoundaryIs></Polygon>
        <Polygon><outerBoundaryIs><LinearRing><coordinates>
          5,5 6,5 6,6 5,6 5,5
        </coordinates></LinearRing></outerBoundaryIs></Polygon>
      </MultiGeometry>
    </Placemark>
  </Document>
</kml>
"""


def test_kml_inner_boundary_becomes_a_hole():
    """`<innerBoundaryIs>` 必须变成真的洞，不是被当成第二个外环。"""
    spec, _warnings = import_region('area.kml', KML_WITH_HOLE)
    assert spec.hole_count == 1
    assert spec.contains_point(116.5, 39.5) is False
    assert spec.contains_point(116.1, 39.1) is True


def test_kml_multigeometry_yields_every_polygon():
    spec, _warnings = import_region('multi.kml', KML_MULTIGEOMETRY)
    assert spec.polygon_count == 2
    assert spec.bbox == (6.0, 0.0, 6.0, 0.0)


def test_kml_coordinates_tolerate_spaces_around_the_comma():
    """导出器在逗号两侧塞空格是常见现象；直接 split() 会把整个环报废。"""
    payload = KML_MULTIGEOMETRY.replace(b'0,0 1,0', b'0, 0  1, 0')
    spec, _warnings = import_region('spaced.kml', payload)
    assert spec.polygon_count == 2


def test_kmz_is_read_through_its_doc_kml():
    """KMZ 就是装了 doc.kml 的 zip；图标之类的伴随文件不该妨碍解析。"""
    payload = _zip_bytes({'doc.kml': KML_WITH_HOLE,
                          'files/icon.png': b'\x89PNG\r\n\x1a\nx'})
    spec, _warnings = import_region('area.kmz', payload)
    assert spec.hole_count == 1


def test_a_kmz_renamed_to_zip_still_works():
    """扩展名从来只是提示 —— 分派看魔数与条目内容。"""
    payload = _zip_bytes({'doc.kml': KML_WITH_HOLE})
    spec, _warnings = import_region('area.zip', payload)
    assert spec.hole_count == 1


def test_a_geojson_saved_as_txt_is_parsed_by_content_with_a_warning():
    """扩展名说谎时按内容解析，并把这件事**回给调用方**（只写日志等于没说）。"""
    spec, warnings = import_region('area.kml', GEOJSON_WITH_HOLE)
    assert spec.hole_count == 1
    assert 'extension_content_mismatch' in warnings


# ---------------------------------------------------------------------------
# 闸门
# ---------------------------------------------------------------------------

def test_a_doctype_in_the_prolog_is_refused():
    """billion laughs：几 KB 的文档展开成几 GB，进程被一个上传打死。"""
    bomb = (b'<?xml version="1.0"?>\n'
            b'<!DOCTYPE kml [<!ENTITY a "aaaaaaaaaa">'
            b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>\n'
            b'<kml><Document><Placemark><Polygon><outerBoundaryIs><LinearRing>'
            b'<coordinates>0,0 1,0 1,1 0,0</coordinates>'
            b'</LinearRing></outerBoundaryIs></Polygon></Placemark></Document></kml>')
    with pytest.raises(RegionImportError, match='DOCTYPE'):
        import_region('bomb.kml', bomb)


def test_a_doctype_after_a_comment_is_still_caught():
    """序言里可以先来一段注释；逐段跳过才能精确停在根元素上。"""
    bomb = (b'<?xml version="1.0"?>\n<!-- exported by hand -->\n'
            b'<!DOCTYPE kml [<!ENTITY a "x">]>\n<kml/>')
    with pytest.raises(RegionImportError, match='DOCTYPE'):
        import_region('bomb.kml', bomb)


def test_the_word_doctype_inside_the_body_is_not_a_false_positive():
    """`<description>` 里贴着一段 HTML 教程的合法文件是存在的，不能误杀。"""
    payload = KML_WITH_HOLE.replace(
        b'<Placemark>',
        b'<Placemark><description>write &lt;!DOCTYPE html&gt; first</description>')
    spec, _warnings = import_region('doc.kml', payload)
    assert spec.hole_count == 1


def test_an_oversize_upload_is_refused_before_it_is_parsed():
    """先量大小再碰内容；放到解析里面去检查等于这道闸门不存在。"""
    oversize = b'{' + b'\x20' * MAX_IMPORT_BYTES
    with pytest.raises(RegionImportError, match='too large'):
        import_region('huge.geojson', oversize)


def test_an_empty_upload_is_refused():
    with pytest.raises(RegionImportError):
        import_region('empty.geojson', b'')
    with pytest.raises(RegionImportError):
        import_region('none.geojson', None)


def test_a_corrupt_archive_is_a_user_input_error_not_a_500():
    """`BadZipFile` 不是 ValueError，漏出去就是 HTTP 500 —— 而截断的上传常见。"""
    with pytest.raises(RegionImportError, match='corrupt|truncated'):
        import_region('area.kmz', b'PK\x03\x04' + b'garbage' * 20)


def test_an_archive_with_nothing_usable_is_refused():
    payload = _zip_bytes({'readme.txt': b'hello', 'data.csv': b'a,b\n1,2\n'})
    with pytest.raises(RegionImportError):
        import_region('bundle.zip', payload)


def test_an_archive_with_too_many_entries_is_refused():
    """条目数挡的是「上万个空文件」型的目录炸弹。"""
    payload = _zip_bytes({f'f{i}.txt': b'x' for i in range(300)})
    with pytest.raises(RegionImportError, match='files'):
        import_region('bomb.zip', payload)


def test_an_unrecognised_file_is_refused_with_the_supported_list():
    with pytest.raises(RegionImportError, match='unrecognised'):
        import_region('notes.doc', b'\xd0\xcf\x11\xe0binary junk')


def test_the_error_message_does_not_echo_an_unbounded_filename():
    """报错消息会被路由原样写进轮转日志；整段回显一个超长文件名 = 日志失效。"""
    with pytest.raises(RegionImportError) as excinfo:
        import_region('x' * 5000 + '.doc', b'\xd0\xcf\x11\xe0junk')
    assert len(str(excinfo.value)) < 1000


# ---------------------------------------------------------------------------
# Shapefile（需要 GDAL）
# ---------------------------------------------------------------------------

def _shapefile_zip(tmp_path, *, with_prj=True):
    """用 OGR 真造一个 shapefile 包，再打成 zip。"""
    from osgeo import ogr, osr

    folder = tmp_path / 'shp'
    folder.mkdir()
    driver = ogr.GetDriverByName('ESRI Shapefile')
    source = driver.CreateDataSource(str(folder / 'area.shp'))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    layer = source.CreateLayer('area', srs, ogr.wkbPolygon)

    ring = ogr.Geometry(ogr.wkbLinearRing)
    for lon, lat in [(116.0, 39.0), (117.0, 39.0), (117.0, 40.0),
                     (116.0, 40.0), (116.0, 39.0)]:
        ring.AddPoint_2D(lon, lat)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(polygon)
    layer.CreateFeature(feature)
    feature = None
    source = None

    members = {}
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() == '.prj' and not with_prj:
            continue
        members[path.name] = path.read_bytes()
    return _zip_bytes(members)


@requires_gdal
def test_a_zipped_shapefile_with_a_prj_imports_cleanly(tmp_path):
    spec, warnings = import_region('area.zip', _shapefile_zip(tmp_path))
    assert spec.polygon_count == 1
    assert spec.bbox_west == pytest.approx(116.0)
    assert spec.bbox_north == pytest.approx(40.0)
    assert 'missing_crs' not in warnings


@requires_gdal
def test_a_shapefile_without_a_prj_reports_the_missing_crs(tmp_path):
    """**没有 .prj 就没有坐标系可言。**

    GeoDownloader 把这种坐标当经纬度用，用户只看到「下载出来全是海」。
    我们照样按 WGS84 读（这是唯一可得的假设），但必须把这件事回给调用方 ——
    只写 logger.warning 等于坑还在，只是换了个没人看的地方摆着。
    """
    spec, warnings = import_region('area.zip',
                                   _shapefile_zip(tmp_path, with_prj=False))
    assert spec.polygon_count == 1
    assert 'missing_crs' in warnings
