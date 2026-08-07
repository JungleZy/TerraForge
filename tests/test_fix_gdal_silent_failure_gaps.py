"""三个「GDAL 报成功、产物是坏的」缺口的回归钉子。

三条都**与 GDAL 版本无关** —— 3.8.4 上同样存在，是在 3.11.4 升级审计里顺带挖出来的，
不是升级引入的。共同形态是:任务 completed、无 warning、文件打得开、尺寸看着对，
而数据缺了一块。这类失败前端一条错误都收不到，只能靠这些断言守住。

1. download_engine 的 GTiff 拼接缺 BIGTIFF=IF_SAFER
   实测(3.11.4, 68000x68000 Byte + COMPRESS=DEFLATE):产物停在 4294967275 字节、
   version=42，而 gdal.Translate 返回**非 None** 的 dataset、RasterXSize/YSize
   报 68000x68000 全对、左上角与源逐字节相等 —— 只有右下角全 0。
   地形侧 cesiumlab_terrain 早就写了 IF_SAFER，这条路径此前一直敞着。

2. download_engine 只用 `output_ds is None` 判失败
   返回值挡不住 I/O 写失败。同一次 4 GiB 截断，写入期间 CPL 错误栈堆了 10073 条
   `TIFFAppendToStrip:Maximum TIFF file size exceeded`，而调用方一条都没捞，
   坏产物就这么 os.replace 成了正式文件。

3. cesiumlab_terrain 的 BuildVRT 丢源不报错
   BuildVRT 对打不开的源只打一行 `Warning 1: ... Skipping it`，返回有效 dataset。
   而下游 _verify_materialised 是拿产物跟**这个已经缺了源的 VRT** 比，处处自洽。
   ⚠️ 触发条件很具体，实测区分过(见 test_build_input_raster_* 的 docstring):
   垃圾内容/0 字节/文件不存在会被跳过;头部完好的**截断**文件不会被跳过，
   它坏在读取阶段，由既有的 Translate 失败 + _verify_materialised 抓住。

外加一条:cesiumlab_terrain 的物化段必须自我隔离 gdal.UseExceptions() 的全局污染。

⚠️ **没有测试守着 download_engine 里那句 gdal.ErrorReset()** —— 不是漏了，是写不出来。
实测(3.11.4):gdal.Translate 内部自己会先重置错误状态，所以在它之前注入的脏错误
活不到 GetLastErrorType 那一行；把 ErrorReset 删掉、或把阈值放宽成 CE_Warning，
两种变异都抓不住(都试过)。那句 ErrorReset 保留纯属防御(不依赖这个未文档化的内部
行为)，删了不会有测试报警。这里写明，免得后人误以为有守卫。
「正常路径不被新检查误杀」由 test_gtiff_stitch_passes_bigtiff_if_safer 兜着 ——
它跑完整拼接并断言产物落地，检查一旦误报那条就红。

本文件每条断言都做过变异验证(逐个破坏对应修复，确认测试变红)，唯一的例外是上面
这段说明的 ErrorReset。
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from osgeo import gdal, osr


# --------------------------------------------------------------------------
# 公共夹具
# --------------------------------------------------------------------------

def _write_png_tile(path, size: int = 64, value: int = 128) -> None:
    """在 path 写一张真实可被 GDAL 打开的 PNG，模拟下载好的瓦片缓存。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    mem = gdal.GetDriverByName('MEM').Create('', size, size, 3, gdal.GDT_Byte)
    for band_idx in range(1, 4):
        mem.GetRasterBand(band_idx).Fill(value)
    png = gdal.GetDriverByName('PNG').CreateCopy(str(path), mem)
    assert png is not None, f"无法写入测试瓦片 {path}"
    png = None
    mem = None


def _write_dem(path, west, north=40.0, px=120, deg=0.001):
    """一幅带起伏的小 DEM，EPSG:4326。"""
    ds = gdal.GetDriverByName("GTiff").Create(
        str(path), px, px, 1, gdal.GDT_Int16, options=["TILED=YES"])
    ds.SetGeoTransform((west, deg, 0.0, north, 0.0, -deg))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    yy, xx = np.mgrid[0:px, 0:px].astype(np.float32)
    ds.GetRasterBand(1).WriteArray((100.0 + xx + yy).astype(np.int16))
    ds.FlushCache()
    ds = None


def _stitch_engine(tmp_path, monkeypatch):
    """一个 CACHE_DIR 指向 tmp、且**不依赖配置库**的 DownloadEngine + 两张相邻瓦片。

    ⚠️ 架空 ConfigManager.get 是必须的，不是图省事。CI 是全新克隆、没有 data/
    目录，而 `ConfigManager.get` 对 sqlite 错误是**有意重抛**的（同类注释见
    contour_engine），`stitch_tiles_with_gdal` 读 gdal_compression 那处没有兜底
    —— 于是这几个用例在 CI 上全部
    `sqlite3.OperationalError: unable to open database file`（实测 macOS job，
    5 failed）。本机看不出来：data/map_downloader.db 早就建好了。

    套件里其他 stitch 用例（test_tile_georeference.py）在 CI 上能过，靠的是按
    字母序排在它们之前的用例导入过 app、顺带把库建了出来 —— 那是**隐式的执行
    顺序依赖**，单独跑那个文件同样会炸。不要跟着学。

    这些用例测的是 GDAL 行为、不是配置读取，所以让 config 恒返回调用方给的
    默认值（gdal_compression 默认 'LZW'），彻底断开对数据库的依赖。
    """
    from src.core.config import Config
    from src.services.config_manager import ConfigManager
    from src.services.download_engine import DownloadEngine, Tile

    monkeypatch.setattr(ConfigManager, 'get',
                        lambda self, key, default=None: default)

    cache_dir = tmp_path / 'cache'
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Config, 'CACHE_DIR', cache_dir)

    engine = DownloadEngine()
    zoom, x, y = 5, 3, 7
    tiles = [
        Tile(task_id=0, zoom=zoom, x=x, y=y),
        Tile(task_id=0, zoom=zoom, x=x + 1, y=y),
    ]
    for idx, tile in enumerate(tiles):
        _write_png_tile(tile.cache_path('m'), value=40 + idx * 60)
    return engine, tiles, zoom


def _capture_translate_options(monkeypatch):
    """拦 gdal.TranslateOptions，把每次调用的 creationOptions 记下来。

    断言 creationOptions 的**实参**而不是源码文本 —— 文本断言("源码里有
    BIGTIFF 这个词")能被一行注释骗过，实参断言不能。
    """
    from src.services import download_engine as de

    seen = []
    real = de.gdal.TranslateOptions

    def spy(*args, **kwargs):
        seen.append(kwargs.get('creationOptions'))
        return real(*args, **kwargs)

    monkeypatch.setattr(de.gdal, 'TranslateOptions', spy)
    return seen


# --------------------------------------------------------------------------
# 1 + 2. download_engine 拼接
# --------------------------------------------------------------------------

def test_gtiff_stitch_passes_bigtiff_if_safer(tmp_path, monkeypatch):
    """GTiff 分支必须带 BIGTIFF=IF_SAFER，否则超 4 GiB 静默截断成半张图。

    GTiff 默认的 IF_NEEDED 只在**不压缩**时按未压缩体积决定是否升级 BigTIFF；
    一旦带了 COMPRESS(这里恒定带)就一律按经典 TIFF 建文件，4 GiB 封顶。
    """
    engine, tiles, zoom = _stitch_engine(tmp_path, monkeypatch)
    seen = _capture_translate_options(monkeypatch)

    out = tmp_path / 'out.tif'
    engine.stitch_tiles_with_gdal(tiles, 'm', str(out), zoom,
                                  extra_allowed_dir=str(tmp_path))

    assert seen, "没有捕获到任何 TranslateOptions 调用，测试自身失效了"
    opts = seen[-1]
    assert opts is not None, "GTiff 分支必须传 creationOptions"
    assert 'BIGTIFF=IF_SAFER' in opts, (
        f"GTiff 拼接的 creationOptions 缺 BIGTIFF=IF_SAFER: {opts} —— "
        "超 4 GiB 的产物会被静默截断，而 Translate 照样返回非 None、尺寸照样报对")
    assert out.exists() and out.stat().st_size > 0


def test_png_stitch_keeps_georeference(tmp_path, monkeypatch):
    """PNG 产物必须带地理配准 —— 边车得跟着主文件一起改名。

    PNG/JPEG 内部没有地理配准字段，GDAL 把 geotransform + 投影写在同名
    `.aux.xml` 边车里。产物走的是「先写 <out>.part.<pid> 再 os.replace」的
    原子写，此前只搬主文件，边车留在原地被 finally 的残件清理无条件删掉
    (成功路径也删)。实测后果:打开产物 geotransform 是默认的
    (0,1,0,0,0,1)、投影为空 —— 一张没有地理信息的普通图片，而任务报成功、
    文件也确实在。GTiff 不受影响(地理信息在文件内部)。
    """
    engine, tiles, zoom = _stitch_engine(tmp_path, monkeypatch)
    out = tmp_path / 'out.png'
    engine.stitch_tiles_with_gdal(tiles, 'm', str(out), zoom,
                                  extra_allowed_dir=str(tmp_path))

    assert out.exists(), "PNG 产物没生成"
    ds = gdal.Open(str(out))
    try:
        assert ds is not None, "PNG 产物打不开"
        gt = ds.GetGeoTransform()
        assert gt != (0.0, 1.0, 0.0, 0.0, 0.0, 1.0), (
            "PNG 产物的 geotransform 是默认值 —— .aux.xml 边车丢了，"
            "用户拿到的是一张没有地理信息的图片")
        assert ds.GetProjection(), "PNG 产物没有投影信息"
    finally:
        ds = None


def test_png_stitch_does_not_pass_bigtiff(tmp_path, monkeypatch):
    """PNG 分支**不能**带 BIGTIFF —— PNG 驱动不认这个创建选项。

    加 BIGTIFF 是对的，但只对 GTiff 对。顺手把它塞进 PNG 分支会换来一串
    `Driver PNG does not support creation option BIGTIFF` 警告。
    """
    engine, tiles, zoom = _stitch_engine(tmp_path, monkeypatch)
    seen = _capture_translate_options(monkeypatch)

    out = tmp_path / 'out.png'
    engine.stitch_tiles_with_gdal(tiles, 'm', str(out), zoom,
                                  extra_allowed_dir=str(tmp_path))

    assert seen, "没有捕获到任何 TranslateOptions 调用，测试自身失效了"
    for opts in seen:
        if opts:
            assert not any('BIGTIFF' in str(o) for o in opts), (
                f"PNG 分支不该带 BIGTIFF: {opts}")


def test_stitch_raises_when_gdal_logs_failure_despite_non_none_return(
        tmp_path, monkeypatch):
    """Translate 返回非 None 但 CPL 记了 CE_Failure ⇒ 必须抛，不能 os.replace。

    这正是 4 GiB 截断的形态:返回值、尺寸、左上角像素全对，右下角全 0，
    唯一的信号是错误栈里那 10073 条 TIFFAppendToStrip。此前这个信号没人看。
    """
    from src.services import download_engine as de

    engine, tiles, zoom = _stitch_engine(tmp_path, monkeypatch)
    real_translate = de.gdal.Translate

    def poisoned(dst, src, **kwargs):
        ds = real_translate(dst, src, **kwargs)
        # 模拟「写到一半 I/O 失败」：产物已生成，错误只登记在 CPL 栈里
        gdal.Error(gdal.CE_Failure, 1,
                   "TIFFAppendToStrip:Maximum TIFF file size exceeded")
        return ds

    monkeypatch.setattr(de.gdal, 'Translate', poisoned)

    out = tmp_path / 'out.tif'
    with pytest.raises(Exception) as ei:
        engine.stitch_tiles_with_gdal(tiles, 'm', str(out), zoom,
                                  extra_allowed_dir=str(tmp_path))
    assert 'GDAL' in str(ei.value) or 'Maximum TIFF' in str(ei.value), (
        f"异常信息应指向 GDAL 记录的失败，实际: {ei.value!r}")

    assert not out.exists(), (
        "GDAL 记了失败却仍把半成品 os.replace 成了正式产物 —— "
        "断点判据是「存在且非空就跳过重拼」，这份坏文件会被当成完成品")


def test_stitch_error_gate_survives_global_use_exceptions(tmp_path, monkeypatch):
    """地图拼接的错误闸门也必须免疫 contour 的全局 gdal.UseExceptions()。

    与地形侧同一个道理:四条流水线共用一个 Flask 进程，用户先跑一个等高线任务
    再跑地图任务时，异常模式下 GDAL 把 CE_Failure 直接抛出、不回填 CPL 错误栈，
    上面那道 GetLastErrorType 检查就永远读到 0 —— 白装。
    地形侧 2026-08-07 用 ExceptionMgr 钉死了模式，地图侧一开始漏了。
    """
    from src.services import download_engine as de

    engine, tiles, zoom = _stitch_engine(tmp_path, monkeypatch)
    real_translate = de.gdal.Translate

    def poisoned(dst, src, **kwargs):
        ds = real_translate(dst, src, **kwargs)
        gdal.Error(gdal.CE_Failure, 1,
                   "TIFFAppendToStrip:Maximum TIFF file size exceeded")
        return ds

    monkeypatch.setattr(de.gdal, 'Translate', poisoned)

    had = gdal.GetUseExceptions()
    gdal.UseExceptions()          # 模拟「用户先跑过等高线任务」
    try:
        out = tmp_path / 'out.tif'
        with pytest.raises(Exception) as ei:
            engine.stitch_tiles_with_gdal(tiles, 'm', str(out), zoom,
                                          extra_allowed_dir=str(tmp_path))
        # 必须断言异常**出自我们的闸门**，不能只断言「抛了异常」——
        # 没有 ExceptionMgr 时 gdal.Error 在异常模式下会自己抛出来，
        # 那样测试照样绿，却完全没验证到闸门(第一版就是这么假绿的)。
        assert 'reported success but GDAL logged a failure' in str(ei.value), (
            f"异常不是出自 GetLastErrorType 闸门，而是 GDAL 自己抛的 —— "
            f"说明这段没被 ExceptionMgr 钉成非异常模式: {ei.value!r}")
        assert not out.exists(), "全局异常模式下坏产物仍被当成正品落地"
        assert gdal.GetUseExceptions() == 1, (
            "stitch 把调用方的全局异常模式改掉了 —— ExceptionMgr 应只在自己作用域内生效")
    finally:
        if not had:
            gdal.DontUseExceptions()


# --------------------------------------------------------------------------
# 3. cesiumlab_terrain 的 BuildVRT 丢源
# --------------------------------------------------------------------------

@pytest.mark.parametrize("corrupt,label", [
    (lambda p: open(p, 'wb').write(b'NOT A TIFF' * 100), "垃圾内容"),
    (lambda p: open(p, 'wb').close(), "0 字节"),
    (lambda p: os.remove(p), "文件不存在"),
])
def test_build_input_raster_rejects_source_buildvrt_would_drop(
        tmp_path, corrupt, label):
    """BuildVRT 会静默跳过的三种坏源，必须在物化前就抛。

    实测(3.11.4，两幅相邻 120x120 本应拼成 240x120):这三种都让 BuildVRT 打
    `Warning 1: Can't open ... Skipping it` 然后返回有效 dataset，VRT 只剩
    一幅的宽度，build_input_raster 静默返回 —— **少切 50% 的地**，
    而任务报 completed、瓦片全 200。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import build_input_raster

    good = tmp_path / 'good.tif'
    bad = tmp_path / 'bad.tif'
    _write_dem(good, 100.0)
    _write_dem(bad, 100.12)
    corrupt(str(bad))

    with pytest.raises(RuntimeError) as ei:
        build_input_raster([str(good), str(bad)], work_dir=str(tmp_path))
    assert 'bad.tif' in str(ei.value), (
        f"[{label}] 异常应点名坏掉的那个文件，实际: {ei.value!r}")


def test_build_input_raster_still_rejects_truncated_source(tmp_path):
    """头部完好的截断文件走的是**另一条**防线，一并钉住。

    它不会被 BuildVRT 跳过(gdal.Open 成功、尺寸照样报 120x120、VRT 宽度完整)，
    坏在读取阶段，由 Translate 失败 + _verify_materialised 接住。
    这条与上一条区分开，是为了防止有人把 _assert_no_input_dropped 误当成
    「所有坏源的唯一防线」而删掉后面的校验。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import build_input_raster

    good = tmp_path / 'good.tif'
    bad = tmp_path / 'bad.tif'
    _write_dem(good, 100.0)
    _write_dem(bad, 100.12)
    full = bad.stat().st_size
    with open(bad, 'r+b') as f:
        f.truncate(full // 3)

    with pytest.raises(RuntimeError):
        build_input_raster([str(good), str(bad)], work_dir=str(tmp_path))


@pytest.mark.parametrize("bands,dtype,label", [
    (3, gdal.GDT_Int16, "波段数不同"),
    (1, gdal.GDT_Float32, "数据类型不同"),
])
def test_build_input_raster_catches_source_dropped_from_inside_the_bbox(
        tmp_path, bands, dtype, label):
    """被跳过的源位于并集**内部**时，包围盒检查看不见 —— 必须靠 GetFileList 对账。

    BuildVRT 要求所有源的波段数/数据类型/投影一致，不一致的会打一行
    `does not support heterogeneous band numbers` 之类的警告然后跳过。
    排 A|B|C 三幅、坏的是中间那幅 B 时:
      - 第一道(逐个 gdal.Open):B 完全打得开，看不见;
      - 第三道(四至并集):A 和 C 把包围盒撑满了，宽度分毫不差，也看不见。
    实测后果:产物宽度 600 完全正确，**中间 200 列整块是 0**，任务报 completed。
    唯一抓得住的是拿 VRT 的 GetFileList() 与 inputs 对账。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import build_input_raster

    a, b, c = tmp_path / 'a.tif', tmp_path / 'b.tif', tmp_path / 'c.tif'
    _write_dem(a, 100.0)
    _write_dem(c, 100.24)
    # 中间那幅:同位置、同分辨率，只有波段数或数据类型不同
    ds = gdal.GetDriverByName("GTiff").Create(
        str(b), 120, 120, bands, dtype, options=["TILED=YES"])
    ds.SetGeoTransform((100.12, 0.001, 0.0, 40.0, 0.0, -0.001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    for bi in range(1, bands + 1):
        ds.GetRasterBand(bi).Fill(500)
    ds.FlushCache()
    ds = None

    with pytest.raises(RuntimeError) as ei:
        build_input_raster([str(a), str(b), str(c)], work_dir=str(tmp_path))
    msg = str(ei.value)
    assert 'b.tif' in msg, f"[{label}] 异常应点名被排除的源，实际: {msg!r}"
    assert '排除' in msg or '打不开' in msg, f"[{label}] 异常措辞意外: {msg!r}"


def test_build_input_raster_accepts_healthy_multi_input(tmp_path):
    """正常多幅不能被误伤 —— 这是上面三条断言的对照组。

    没有这条，把 _assert_no_input_dropped 写成 `raise` 也能让上面全绿。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import build_input_raster

    a, b = tmp_path / 'a.tif', tmp_path / 'b.tif'
    _write_dem(a, 100.0)
    _write_dem(b, 100.12)

    out = build_input_raster([str(a), str(b)], work_dir=str(tmp_path))
    ds = gdal.Open(out)
    try:
        assert ds is not None, "正常多幅物化失败"
        assert ds.RasterXSize == 240, (
            f"两幅 120px 应拼成 240px，实际 {ds.RasterXSize} —— 丢源了")
    finally:
        ds = None


# --------------------------------------------------------------------------
# 4. 物化段必须免疫 gdal.UseExceptions() 的全局污染
# --------------------------------------------------------------------------

def test_build_input_raster_immune_to_global_use_exceptions(tmp_path):
    """contour_engine 无条件开的全局异常模式，不能改变物化段的错误语义。

    gdal.UseExceptions() 是**进程全局**的，四条流水线共用一个 Flask 进程:
    用户跑过任意一个等高线任务后，地形侧的语义就被换掉了。异常模式下 GDAL 把
    CE_Failure 直接抛成 Python 异常、不回填 CPL 错误栈，而 _raise_on_gdal_error
    捞的正是那个栈 —— 第一道闸门会退化成空转，只剩 _verify_materialised 兜底。

    物化段用 `with gdal.ExceptionMgr(useExceptions=False)` 就地钉死所需模式。
    本测试同时守住两件事:污染下功能正确，且**退出后不改变调用方的全局状态**。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import build_input_raster

    a, b = tmp_path / 'a.tif', tmp_path / 'b.tif'
    _write_dem(a, 100.0)
    _write_dem(b, 100.12)

    had = gdal.GetUseExceptions()
    gdal.UseExceptions()          # 模拟「用户先跑了一个等高线任务」
    try:
        out = build_input_raster([str(a), str(b)], work_dir=str(tmp_path))
        ds = gdal.Open(out)
        try:
            assert ds is not None and ds.RasterXSize == 240, "污染下物化结果不对"
        finally:
            ds = None

        assert gdal.GetUseExceptions() == 1, (
            "build_input_raster 把调用方的全局异常模式改掉了 —— "
            "ExceptionMgr 应当只在自己的作用域内生效")

        # 污染下坏源仍要被拦住
        bad = tmp_path / 'bad.tif'
        _write_dem(bad, 100.24)
        open(bad, 'wb').close()
        with pytest.raises(RuntimeError):
            build_input_raster([str(a), str(b), str(bad)], work_dir=str(tmp_path))
    finally:
        if not had:
            gdal.DontUseExceptions()


def test_materialisation_is_wrapped_in_exception_manager():
    """形态钉:物化段必须真的包在 ExceptionMgr(useExceptions=False) 里。

    上面那条行为测试有个盲点 —— 如果全局恰好本来就是非异常模式，
    去掉 ExceptionMgr 它也照样绿。这条直接钉源码形态，两条合起来才守得住。
    """
    import inspect
    from src.services.terrain_tiling import cesiumlab_terrain

    src = inspect.getsource(cesiumlab_terrain.build_input_raster)
    assert 'ExceptionMgr(useExceptions=False)' in src, (
        "build_input_raster 的物化段必须显式声明非异常模式，"
        "否则 contour_engine 的全局 gdal.UseExceptions() 会让 "
        "_raise_on_gdal_error 退化成空转")
