"""GeoPackage 导出器：accepts 过滤、真实导出、两道静默失败闸门、驱动缺失的明确报错。

真导出那条用例是本文件的主体：造一幅 32×32 的 Float32 DEM，走完整 `export()`，
再用 `gdal.Open` 把产物读回来对尺寸 / 投影 / 仿射 / 像元值。只断言「文件存在」
不够 —— GPKG 的栅格是瓦片化存储（Float32 走 gridded coverage 扩展），写出一个
打得开但地理信息丢了的库完全可能。

另外两条钉的是本仓反复吃过的静默失败（见
`tests/test_fix_gdal_silent_failure_gaps.py`）：磁盘满 / 配额 / 超 4 GiB 时
`gdal.Translate` 照样返回**非 None** 的 dataset、尺寸照样对，只有数据被截断。
所以 `ds is None` 之外还有两道闸门——CPL 错误栈与产物体积——它们必须真会抛。

GPKG 驱动缺失时 skip 而不红：GDAL 构建可裁，这个环境事实不是本插件的缺陷。
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.contracts.artifact import Artifact, ArtifactKind  # noqa: E402
from src.plugins import registry  # noqa: E402
from src.plugins.builtin.gpkg_exporter import GpkgExporter  # noqa: E402
from src.plugins.builtin import gpkg_exporter  # noqa: E402
from src.plugins.manifest import manifest_from_dict  # noqa: E402
from src.plugins.protocols import Exporter, ExportContext  # noqa: E402

GEOTRANSFORM = (116.0, 0.01, 0.0, 40.0, 0.0, -0.01)


def _require_gpkg():
    """没有 GPKG 驱动就 skip —— 那是 GDAL 构建的事实，不是插件的缺陷。"""
    gdal = pytest.importorskip('osgeo.gdal')
    if gdal.GetDriverByName('GPKG') is None:
        pytest.skip('GPKG 驱动不可用')
    return gdal


def _geotiff(tmp_path):
    from osgeo import gdal, osr

    path = tmp_path / 'src.tif'
    ds = gdal.GetDriverByName('GTiff').Create(
        str(path), 32, 32, 1, gdal.GDT_Float32)
    ds.SetGeoTransform(GEOTRANSFORM)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).Fill(100.0)
    ds.FlushCache()
    ds = None
    return path


def _artifact(src):
    return Artifact(pipeline='plugin', task_id=1, kind=ArtifactKind.GEOTIFF,
                    path=str(src), fmt='tif', minzoom=3, maxzoom=9)


def _ctx():
    """(ctx, 日志列表, 进度列表)。导出必须有日志与进度，界面靠它们。

    ExportContext 是 frozen dataclass，记账只能放在闭包捕获的列表里。
    """
    messages, ticks = [], []
    ctx = ExportContext(task_id=1,
                        log=lambda m, l='info': messages.append(m),
                        progress=lambda d, t: ticks.append((d, t)))
    return ctx, messages, ticks


def test_accepts_only_geotiff():
    e = GpkgExporter()
    assert e.format_id() == 'gpkg'
    assert e.accepts(ArtifactKind.GEOTIFF)
    assert not e.accepts(ArtifactKind.MBTILES)
    assert not e.accepts(ArtifactKind.XYZ_DIR)
    assert not e.accepts(ArtifactKind.TERRAIN_DIR)
    # 协议实现要能被宿主的签名闸放行（registry._check_definition 拒载签名不符者）
    assert isinstance(e, Exporter)


def test_export_produces_readable_gpkg(tmp_path):
    gdal = _require_gpkg()
    src = _geotiff(tmp_path)
    ctx, messages, ticks = _ctx()
    dest = tmp_path / 'out.gpkg'

    result = GpkgExporter().export(_artifact(src), dest, ctx)

    assert dest.exists()
    # kind 复用 GEOTIFF（GPKG 语义上仍是单文件栅格数据集），fmt 才是区分位
    assert result.kind is ArtifactKind.GEOTIFF and result.fmt == 'gpkg'
    assert result.pipeline == 'plugin' and result.task_id == 1
    assert result.path == str(dest)
    assert result.bytes_total == dest.stat().st_size > 0
    assert (result.minzoom, result.maxzoom) == (3, 9)
    assert result.meta['exported_from'] == str(src)
    assert result.created_at                       # 落库要有时间戳
    assert messages and ticks[-1] == (1, 1)

    ds = gdal.Open(str(dest))
    assert ds is not None
    assert (ds.RasterXSize, ds.RasterYSize) == (32, 32)
    assert ds.GetGeoTransform() == pytest.approx(GEOTRANSFORM)
    srs = ds.GetSpatialRef()
    assert srs is not None and srs.GetAuthorityCode(None) == '4326'
    band = ds.GetRasterBand(1)
    assert band.ReadAsArray(0, 0, 2, 2).tolist() == [[100.0, 100.0],
                                                     [100.0, 100.0]]
    ds = None


def test_translate_reporting_success_while_gdal_logged_failure_raises(
        tmp_path, monkeypatch):
    """`gdal.Translate` 返回非 None 不等于写成功——CPL 错误栈那道闸门必须真会抛。

    实测形态：磁盘满 / 配额 / 超 4 GiB 时它返回一个完全有效的 dataset，错误只
    登记在 CPL 里。这里用 gdal.Error 复现「Translate 期间登记了 CE_Failure」，
    产物本身是好的——闸门若失效，用例就会拿到一个「成功」的 Artifact。
    """
    gdal = _require_gpkg()
    src = _geotiff(tmp_path)
    real = gdal.Translate

    def _translate_then_log_failure(*a, **kw):
        ds = real(*a, **kw)
        gdal.Error(gdal.CE_Failure, 42, 'simulated disk full')
        return ds

    monkeypatch.setattr(gpkg_exporter.gdal, 'Translate',
                        _translate_then_log_failure)
    with pytest.raises(RuntimeError) as e:
        GpkgExporter().export(_artifact(src), tmp_path / 'out.gpkg', _ctx()[0])
    assert 'simulated disk full' in str(e.value)


def test_empty_output_raises(tmp_path, monkeypatch):
    """0 字节产物必须抛错：最后一次 flush 才失败时 GDAL 连错误记录都不留。"""
    _require_gpkg()
    src = _geotiff(tmp_path)
    dest = tmp_path / 'out.gpkg'

    class _Ds:
        def FlushCache(self):
            dest.write_bytes(b'')

    monkeypatch.setattr(gpkg_exporter.gdal, 'Translate',
                        lambda *a, **kw: _Ds())
    with pytest.raises(RuntimeError, match='空文件'):
        GpkgExporter().export(_artifact(src), dest, _ctx()[0])


def test_missing_gpkg_driver_raises_readable_error(tmp_path, monkeypatch):
    """驱动缺失（裁过的打包产物）时给的是一句人话，不是 ImportError 栈。

    判定必须在 export() 里：模块级判会让插件整个变成 load_error，用户读到
    「插件坏了」，而事实是「这个 GDAL 构建不带 GPKG」。
    """
    src = _geotiff(tmp_path)
    monkeypatch.setattr(gpkg_exporter.gdal, 'GetDriverByName',
                        lambda name: None)
    with pytest.raises(RuntimeError, match='GPKG 驱动不可用'):
        GpkgExporter().export(_artifact(src), tmp_path / 'out.gpkg', _ctx()[0])


def test_manifest_passes_validation():
    m = manifest_from_dict(gpkg_exporter.MANIFEST)
    assert m.plugin_id == 'gpkg'
    assert m.capabilities == ('exporter',) and m.permissions == ('filesystem',)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张真库。写法同 tests/test_plugin_tianditu.py:25-44（conftest 无 db）。

    BASE_DIR 一起指到 tmp_path：registry._plugins_root() 由它派生，否则
    load_all() 会连带扫开发机上真实的 plugins/ 目录。
    """
    from src.core import config as config_mod

    path = tmp_path / 'data' / 'map_downloader.db'
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(config_mod.Config, 'DATABASE_PATH', path)
    monkeypatch.setattr(config_mod.Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(config_mod.Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(config_mod.Config, 'CACHE_DIR', tmp_path / 'cache')

    from src.core.database import init_database

    init_database()
    return path


def test_plugin_loads_and_registers_format(db):
    """builtin 名单里这一条必须干净加载，启用后 gpkg 出现在导出格式表里。"""
    registry.reset_for_tests()
    try:
        registry.load_all()
        rec = registry.get_record('gpkg')
        assert rec is not None and rec.origin == 'builtin'
        assert rec.load_error == ''
        assert 'exporter' in rec.manifest.capabilities
        assert registry.exporter_for('gpkg') is None      # 缺省关闭
        registry.set_enabled('gpkg', True)
        exporter = registry.exporter_for('gpkg')
        assert exporter is not None and exporter.format_id() == 'gpkg'
        assert 'gpkg' in registry.list_export_formats()
    finally:
        registry.reset_for_tests()
