"""多幅输入必须物化成单幅栅格 —— 多源 VRT + 源自带的非标准 overview 会开接缝。

## 实测到的故障

6 幅 ASTER 拼的天山 VRT，z10 有 16 对南北相邻瓦片的公共边高程对不上，最大
**50.9 m**；同一条纬线在 z11/z12 却逐点精确一致，单幅 DEM 的 543 对也全为 0。

## 根因（逐条实测，不是推断）

ASTER 源 GeoTIFF 自带 8 层内嵌 overview，倍率是 2 / 3 / 4 / **7.98** / **8.98**
/ 15.9 / 63 / 80 —— **不是 2 的幂**。请求降采样读取时 GDAL 会自动切到 overview，
而在多源 VRT 上，请求要先按 source 切分再分别转发，每段的 win/buf 比值不再整齐，
挑中的 overview 层随读窗口漂移。于是同一个经纬点，在南瓦片的窗口下和在北瓦片的
窗口下采出不同高程。

对照实验（真实 2 幅，同一源块 [3552,3568)，四个不同读窗口）：

| overview 层级                  | 四窗口最大差 |
|--------------------------------|--------------|
| ASTER 官方 8 层（含 3x/9x/63x）| 19.0 m       |
| 复刻 ASTER 层级 [2,3,4,8,9,16] | 24.0 m       |
| 标准幂次 [2,4,8,16,32]         | **0.0 m**    |
| 只有 [8]                       | **0.0 m**    |
| 无 overview                    | **0.0 m**    |
| 物化成单幅 GeoTIFF             | **0.0 m**    |

单幅路径免疫：同一幅在 scale=8/16/32 三档、带与不带 overview 各四个窗口，全部
0.0 m。所以修法是把多幅**物化成单幅**，并补一套 2 的幂 overview。

## 为什么必须补 overview

物化后若不建 overview，低层级瓦片退化成全分辨率读取 —— 实测 z6 从 0.2 ms/张
掉到 13.0 ms/张（慢 30 倍以上）。补上标准幂次 overview 后反而快过现状。

## 修复效果（真实数据，6 幅天山 ASTER）

z6–z12 共 **8565 对**公共边（南北 + 东西），超差 **0** 对；修复前 z10 是 16 对、
最大 50.928 m。物化产物与原单幅逐点一致：3 处 × 4225 点密集网格，**0.000000 m**。
成本：源 118 MB → 物化 + 8 层 overview 共 91.8 MB（78%）、13.6 秒。
性能反而变好：z6 从裸 VRT 的 2.3 ms/张 到 0.2 ms/张。

## 关于复现门槛的一条订正

本文件早先的版本声称「可靠复现需要 3601² 两幅（84.8 MB），压窄任一维度都不触发」
—— **归因是错的**。触发开关不是数据量，而是**降采样比值**（瓦片采样步长 ÷ 源像素）
有没有落进两个挨得近的 overview 档之间。把 fixture 的像素大小调到让 z8 的比值等于
9.89（夹在 ASTER 的 7.98x 与 8.98x 中间），4.5 MB 的合成数据就能让
`test_sampling_is_independent_of_window` 在修复前红到 318 m。

所以本文件的护栏有两层，**都在修复前会红**：
  - 契约层：不得交出多源 VRT；overview 必须是精确的 2 的幂尺寸
  - 不变量层：同一条公共边在南北两个瓦片的窗口下必须采出相同高程

（真实数据的完整实验记录在本地 SDD ledger 里，但那个目录是 gitignore 的，所以
上面的数字都直接写在这儿 —— 证据链不该指向新克隆的人打不开的文件。）
"""

import glob
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from osgeo import gdal, osr

from src.services.terrain_tiling.cesiumlab_terrain import (
    DemSampler,
    GeographicTilingScheme,
    build_input_raster,
)

ASTER_LIKE_OVERVIEWS = [2, 3, 4, 8, 9, 16]


def _write_dem(path, west, north, rows, cols, deg, overviews=None):
    """一幅带起伏的 Int16 DEM；overviews 给的是重采样倍率列表。"""
    ds = gdal.GetDriverByName("GTiff").Create(
        str(path), cols, rows, 1, gdal.GDT_Int16,
        options=["TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256"])
    ds.SetGeoTransform((west, deg, 0.0, north, 0.0, -deg))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    yy, xx = np.mgrid[0:rows, 0:cols].astype(np.float64)
    h = 2500 + 600 * np.sin(xx / 3.0) + 600 * np.cos(yy / 2.5)
    ds.GetRasterBand(1).WriteArray(h.astype(np.int16))
    ds.FlushCache()
    ds = None
    if overviews:
        d = gdal.Open(str(path), gdal.GA_Update)
        d.BuildOverviews("AVERAGE", overviews)
        d = None


# 让 z8 的「瓦片采样步长 ÷ 源像素」= 9.89，正好卡进 ASTER overview 的 7.98x 与
# 8.98x 之间。这个比值才是复现的开关，**不是数据量** —— 见 _DEM_PAIR_DEG 下方。
_DEM_PAIR_N = 761
_DEM_PAIR_DEG = 2.8125 / (2 ** 8 * 9.89)


@pytest.fixture
def dem_pair(tmp_path):
    """两幅上下相邻、边缘共享一像素的 DEM，带 ASTER 那套非标准 overview。

    尺寸和像素大小都不是随手取的。触发接缝的条件是**降采样比值**
    （瓦片采样步长 ÷ 源像素）落在两个挨得近的 overview 档之间，这里调到 z8 上
    的 9.89，夹在 ASTER 的 7.98x 与 8.98x 中间。

    实测这套 fixture：修复前（多源 VRT）z7–z9 的 27 对公共边有 2 对超差、最大
    **318.0 m**；修复后（物化）全部 0.000 m。两幅源数据含 overview 共 4.5 MB。

    ⚠️ 比值对了也不一定触发 —— 瓦片边界与两幅接缝的相对位置同样参与，机理没有
    完全摸清。所以这套参数是实验找出来的，改动它之前先跑一遍变异确认还会红。
    """
    n = _DEM_PAIR_N
    deg = _DEM_PAIR_DEG
    top_north = 43.0
    bot_north = top_north - n * deg + deg      # 上下相邻，共享一行
    top = tmp_path / "top_dem.tif"
    bot = tmp_path / "bot_dem.tif"
    _write_dem(top, 86.0, top_north, n, n, deg, ASTER_LIKE_OVERVIEWS)
    _write_dem(bot, 86.0, bot_north, n, n, deg, ASTER_LIKE_OVERVIEWS)
    return [str(top), str(bot)]


# ---------------------------------------------------------------------------
# 契约：多输入不得再交出多源 VRT
# ---------------------------------------------------------------------------


def test_multi_input_is_materialised_not_a_vrt(dem_pair, tmp_path):
    """多输入必须物化成单幅栅格。

    这条是接缝的直接修复：只要交出去的还是多源 VRT，GDAL 的 overview 选层就会
    随读窗口漂移。修复前此测试红（返回 .vrt）。
    """
    out = build_input_raster(dem_pair, work_dir=str(tmp_path))

    assert out not in dem_pair, "多输入不应直接返回某个源文件"
    ds = gdal.Open(out, gdal.GA_ReadOnly)
    assert ds is not None, f"产物打不开: {out}"
    assert ds.GetDriver().ShortName != "VRT", (
        f"多输入仍然交出了 VRT（{out}）—— 多源 VRT 的 overview 选层随读窗口漂移，"
        "正是 50.9 m 接缝的根因")

    # 产物必须是自足的单个文件，不再引用源栅格
    files = [os.path.abspath(f) for f in (ds.GetFileList() or [])]
    for src in dem_pair:
        assert os.path.abspath(src) not in files, (
            f"产物仍引用源文件 {src} —— 说明只是换了层包装，多源转发还在")


def test_materialised_raster_has_power_of_two_overviews(dem_pair, tmp_path):
    """物化产物必须带 2 的幂 overview。

    两个理由，都实测过：
      - 缺 overview：低层级瓦片全分辨率读，z6 慢 30 倍（13.0 vs 0.2 ms/张）
      - 非 2 的幂（如 ASTER 的 3x/9x/63x）：正是接缝的来源
    """
    out = build_input_raster(dem_pair, work_dir=str(tmp_path))
    ds = gdal.Open(out, gdal.GA_ReadOnly)
    band = ds.GetRasterBand(1)
    count = band.GetOverviewCount()
    assert count > 0, "物化产物没有 overview —— 低层级切片会退化成全分辨率读取"

    for i in range(count):
        ovr = band.GetOverview(i)
        factor = 2 ** (i + 1)
        # 必须**精确**相等，不能给容差。GTiff 的 BuildOverviews 用的就是精确 ceil
        # （实测各层 delta 全是 0），而多源 VRT 的虚拟 overview 用 floor(size/2)，
        # 与 ceil 恒差 1 像素 —— 一个 `<= 1` 的容差就恰好把「仍然交出多源 VRT」
        # 这种回归全部放过（实测 361/761/1201/3601 各种尺寸下 VRT 一律 delta=(1,1)）。
        expect_y = math.ceil(ds.RasterYSize / factor)
        expect_x = math.ceil(ds.RasterXSize / factor)
        assert (ovr.XSize, ovr.YSize) == (expect_x, expect_y), (
            f"overview[{i}] 是 {ovr.XSize}x{ovr.YSize}，不是 {factor} 倍的 "
            f"{expect_x}x{expect_y} —— 采样器请求的降采样比值恒为 2 的幂，"
            "层级对不上就会落到比要求更粗的那一层")


def test_materialised_raster_is_faithful_to_source(dem_pair, tmp_path):
    """物化产物必须与源逐项一致：尺寸、数据类型、仿射变换、**像素值**。

    没有这条，在 `gdal.Translate` 上加一个 `outputType=gdal.GDT_Byte`（很容易被
    当成「省空间」优化误加）就能让整幅 DEM 塌成常数 255，而本文件的用例、连同
    153 个切片相关测试**全部通过** —— 因为其余断言查的都是产物的内部自洽性
    （驱动名、GetFileList、overview 相对自身尺寸成幂次），没有一条回头看源。

    同理，`widthPct=50` 把物化分辨率砍半也是全绿：overview 断言拿产物自己的
    RasterXSize 当基准，砍半后依然自洽。

    比对必须走**全分辨率**：多源 VRT 的降采样读正是本次修复的坏路径，拿它当
    基准等于要求产物复现同一个 bug。
    """
    out = build_input_raster(dem_pair, work_dir=str(tmp_path))

    ref_vrt = str(tmp_path / "reference.vrt")
    v = gdal.BuildVRT(ref_vrt, dem_pair,
                      options=gdal.BuildVRTOptions(resampleAlg="bilinear",
                                                   addAlpha=False))
    v.FlushCache()
    v = None

    src = gdal.Open(ref_vrt, gdal.GA_ReadOnly)
    dst = gdal.Open(out, gdal.GA_ReadOnly)

    assert (dst.RasterXSize, dst.RasterYSize) == (src.RasterXSize, src.RasterYSize), \
        "物化改变了栅格尺寸"

    sb, db = src.GetRasterBand(1), dst.GetRasterBand(1)
    assert db.DataType == sb.DataType, (
        f"物化把数据类型从 {gdal.GetDataTypeName(sb.DataType)} 改成了 "
        f"{gdal.GetDataTypeName(db.DataType)} —— 高程会被静默压平/截断")

    assert max(abs(a - b) for a, b in
               zip(src.GetGeoTransform(), dst.GetGeoTransform())) < 1e-12, \
        "物化改变了仿射变换，整幅地形会偏移"

    got = db.ReadAsArray()
    ref = sb.ReadAsArray()
    assert np.array_equal(got, ref), "物化产物的像素与源不一致 —— 不是忠实副本"
    # 断言不能是空的：源本身若是常数，上面的比对放过任何常数产物
    assert got.min() != got.max(), "测试数据是常数，像素比对形同虚设"


def test_bigtiff_is_requested(dem_pair, tmp_path, monkeypatch):
    """物化必须显式要求 BIGTIFF。

    GTiff 默认的 `IF_NEEDED` 只在**不压缩**时按未压缩体积判断是否升级 BigTIFF；
    一旦带了 `COMPRESS`，GDAL 一律按经典 TIFF（4 GiB 上限）建文件。实测约 92 幅
    Copernicus granule（10°×10° 的框，用户随手能画）就写到 4,294,967,296 字节
    截断，而 `gdal.Translate` 照样返回非 None、`BuildOverviews` 照样返回 0 ——
    两道返回值闸门一个都不触发，截断区读出全 0（海平面平板）。

    行为测试要造 4 GB 才能复现，所以这里钉参数：`contour_engine` 的两处 Translate
    早就写了 `BIGTIFF=IF_SAFER`，是项目现成惯例。
    """
    from src.services.terrain_tiling import cesiumlab_terrain

    captured = {}
    real_translate = cesiumlab_terrain.gdal.Translate

    def spy(dst, src, **kwargs):
        captured.update(kwargs)
        return real_translate(dst, src, **kwargs)

    monkeypatch.setattr(cesiumlab_terrain.gdal, "Translate", spy)
    build_input_raster(dem_pair, work_dir=str(tmp_path))

    opts = captured.get("creationOptions") or []
    assert any(str(o).upper().startswith("BIGTIFF=") for o in opts), (
        f"creationOptions 没有 BIGTIFF：{opts} —— 压缩 + 默认 IF_NEEDED 会在 "
        "4 GiB 处静默截断")


def test_single_input_is_passed_through(tmp_path):
    """单幅直通，不物化。

    单幅路径实测免疫（scale=8/16/32 三档、带与不带 overview 各四个窗口全 0.0 m），
    平白复制一份既慢又占盘。
    """
    n = 129
    deg = 1.0 / (n - 1)
    only = tmp_path / "only_dem.tif"
    _write_dem(only, 86.0, 43.0, n, n, deg, ASTER_LIKE_OVERVIEWS)

    out = build_input_raster([str(only)], work_dir=str(tmp_path))
    assert out == str(only), "单幅输入不应被复制/物化"


def test_missing_work_dir_is_loud(dem_pair):
    """work_dir 不存在必须抛异常（在 mkstemp 阶段就炸，还没写盘）。"""
    with pytest.raises(Exception):
        build_input_raster(dem_pair, work_dir="/nonexistent-dir-for-terrain-test/xyz")


def _leftover_products(work_dir):
    return glob.glob(os.path.join(str(work_dir), "cesiumlab_terrain_*"))


def test_translate_failure_is_loud(dem_pair, tmp_path, monkeypatch):
    """物化本身失败时必须抛异常，不得静默回退到多源 VRT。

    静默回退 = 出一份带 50 m 接缝的地形，而任务标记成功、瓦片全 200、前端一条
    错误都没有 —— 这个项目已经吃过六次同形态的静默失败。

    必须 monkeypatch 才能测到：给一个不存在的 work_dir 只会在 mkstemp 阶段就炸，
    根本走不到 Translate 这一步（变异测试里「Translate 返回 None 就 return
    vrt_path」正是靠这个漏网的）。
    """
    from src.services.terrain_tiling import cesiumlab_terrain

    monkeypatch.setattr(cesiumlab_terrain.gdal, "Translate",
                        lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="Translate"):
        build_input_raster(dem_pair, work_dir=str(tmp_path))

    # 失败路径不得留下与源同量级的半成品：清理原本挂在 build_terrain 的 finally
    # 上，而那个 finally 够不着 —— build_input_raster 是在它的 try 之外调用的，
    # 抛出时 input_path 根本没赋值。磁盘不足时用户会重试，每重试一次多留一份。
    assert not _leftover_products(tmp_path), \
        f"失败后残留：{_leftover_products(tmp_path)}"


def test_corrupt_product_is_caught_even_when_gdal_reports_success(
        dem_pair, tmp_path, monkeypatch):
    """GDAL 报成功但产物是坏的时候，必须被产物校验拦下。

    这是最危险的一类：`gdal.Translate` 在磁盘满 / 配额 / RLIMIT_FSIZE / 超 4 GiB
    时**照样返回非 None 的 dataset**，`BuildOverviews` **照样返回 0**，交出去的却是
    数据被截断的栅格 —— 打开正常、尺寸正常、overview 层数正常，只是读回来是常数。
    端到端实测过后果：693 张瓦片全部"渲染成功"、作业 completed、瓦片全 200、
    前端零报错，而 4000 m 的天山在 Cesium 里是一块海平面平板。

    这里用 `outputType=GDT_Byte` 模拟「产物与源不一致」这一类（整幅 DEM 塌成
    常数 255），它是同一个失败面上最容易构造的一点。
    """
    from src.services.terrain_tiling import cesiumlab_terrain

    real_translate = cesiumlab_terrain.gdal.Translate

    def corrupting(dst, src, **kwargs):
        kwargs["outputType"] = gdal.GDT_Byte
        return real_translate(dst, src, **kwargs)

    monkeypatch.setattr(cesiumlab_terrain.gdal, "Translate", corrupting)

    with pytest.raises(RuntimeError):
        build_input_raster(dem_pair, work_dir=str(tmp_path))
    assert not _leftover_products(tmp_path), \
        f"坏产物没被删除：{_leftover_products(tmp_path)}"


def test_cpl_error_is_not_swallowed(dem_pair, tmp_path, monkeypatch):
    """CPL 错误栈里登记的失败必须变成异常。

    模块顶部有意不开 `gdal.UseExceptions()`（见文件头注释：它会去 import
    osgeo.gdal_array，某些绑定里没有），所以写失败只落到 CPL 错误栈、不抛。
    只查返回值就会把它整个漏掉。

    必须**紧接着 Translate** 检查，不能只在末尾查一次：`ErrorReset()` 会把错误栈
    清空，所以晚一步就什么都看不到了。下面的桩精确复刻这个语义 —— Translate 期间
    置错、ErrorReset 清错 —— 于是漏掉 Translate 后那一处检查的实现会当场变红。
    """
    from src.services.terrain_tiling import cesiumlab_terrain

    real_translate = cesiumlab_terrain.gdal.Translate
    real_reset = cesiumlab_terrain.gdal.ErrorReset
    state = {"failed": False}

    def translate_that_fails_writing(dst, src, **kwargs):
        result = real_translate(dst, src, **kwargs)
        state["failed"] = True          # 写盘出错，但返回值一切正常
        return result

    def reset():
        state["failed"] = False         # 真实 ErrorReset 的语义：清空错误栈
        real_reset()

    monkeypatch.setattr(cesiumlab_terrain.gdal, "Translate",
                        translate_that_fails_writing)
    monkeypatch.setattr(cesiumlab_terrain.gdal, "ErrorReset", reset)
    monkeypatch.setattr(cesiumlab_terrain.gdal, "GetLastErrorType",
                        lambda: gdal.CE_Failure if state["failed"] else gdal.CE_None)
    monkeypatch.setattr(cesiumlab_terrain.gdal, "GetLastErrorMsg",
                        lambda: "simulated disk-full write failure")
    monkeypatch.setattr(cesiumlab_terrain.gdal, "GetLastErrorNo", lambda: 3)

    with pytest.raises(RuntimeError, match="simulated disk-full"):
        build_input_raster(dem_pair, work_dir=str(tmp_path))
    assert not _leftover_products(tmp_path), \
        f"失败后残留：{_leftover_products(tmp_path)}"


def test_build_overviews_failure_is_loud(dem_pair, tmp_path, monkeypatch):
    """建 overview 失败同样要响：静默少一层 overview = 低层级切片慢 30 倍。"""
    from src.services.terrain_tiling import cesiumlab_terrain

    real_open = cesiumlab_terrain.gdal.Open

    class _NoOverviews:
        """转发一切，只让 BuildOverviews 报错。"""

        def __init__(self, ds):
            self._ds = ds

        def __getattr__(self, name):
            return getattr(self._ds, name)

        def BuildOverviews(self, *a, **k):
            return 1  # 非 0 = 失败

    def fake_open(path, *a, **k):
        ds = real_open(path, *a, **k)
        return _NoOverviews(ds) if ds is not None else None

    monkeypatch.setattr(cesiumlab_terrain.gdal, "Open", fake_open)

    with pytest.raises(RuntimeError, match="BuildOverviews"):
        build_input_raster(dem_pair, work_dir=str(tmp_path))

    assert not _leftover_products(tmp_path), \
        f"失败后残留：{_leftover_products(tmp_path)}"


# ---------------------------------------------------------------------------
# 不变量：采样结果不得依赖读窗口
# ---------------------------------------------------------------------------


def test_sampling_is_independent_of_window(dem_pair, tmp_path):
    """同一条公共边，在南瓦片与北瓦片的窗口下必须采出相同高程。

    这是**真的会红**的护栏（早先版本的自我标注说它不会，那个归因是错的，见模块
    docstring）：把 build_input_raster 换回交出多源 VRT，本用例在 z8 报 318 m。
    """
    out = build_input_raster(dem_pair, work_dir=str(tmp_path))
    sampler = DemSampler(out)
    scheme = GeographicTilingScheme(tile_size=65)
    w0, s0, e0, n0 = sampler.bounds

    def grid(z, x, y):
        w, s, e, n = scheme.tile_extent_deg(z, x, y)
        return np.meshgrid(np.linspace(w, e, 65, dtype=np.float64),
                           np.linspace(s, n, 65, dtype=np.float64))

    checked = 0
    for z in range(6, 12):
        nx, ny = scheme.tile_count(z)
        xs = range(max(0, int((w0 + 180) * nx / 360)),
                   int((e0 + 180) * nx / 360) + 1)
        ys = list(range(max(0, int((s0 + 90) * ny / 180)),
                        int((n0 + 90) * ny / 180) + 1))
        for x in xs:
            for y in ys[:-1]:
                south_tile = sampler.sample(*grid(z, x, y))
                north_tile = sampler.sample(*grid(z, x, y + 1))
                worst = float(np.abs(south_tile[-1, :] - north_tile[0, :]).max())
                assert worst < 1e-3, (
                    f"z={z} x={x} y={y}/{y+1} 公共边最大差 {worst:.3f} m —— "
                    "同一经纬点在不同读窗口下采出了不同高程")
                checked += 1

    assert checked > 0, "一对相邻瓦片都没测到，断言是空的"
