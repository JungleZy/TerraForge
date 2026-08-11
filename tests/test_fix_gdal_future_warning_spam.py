"""GDAL 异常模式必须由进程显式声明(FutureWarning 刷屏的回归钉子)。

实测形态(2026-08-11 生产日志):一次地形切片作业固定刷 5 条

    FutureWarning: Neither gdal.UseExceptions() nor gdal.DontUseExceptions()
    has been explicitly called. In GDAL 4.0, exceptions will be enabled by default.

= 主进程 1 条 + 4 个 spawn worker 各 1 条(去重标记是 osgeo 的模块属性,每进程只打
一次;worker 的触发点是 `_worker_init` -> `DemSampler.__init__` -> `gdal.Open`)。

它不只是噪音:GDAL 4.0 会把默认翻成异常模式,而 cesium_terrain 的
`_raise_on_gdal_error`(读 CPL 错误栈)、download_engine 的拼接闸门、hillshade_preview
的 `is None -> raise` 全都以**非异常模式**为前提 —— 默认一翻,这些判错分支集体变
死代码。修法是 `src/core/gdal_mode.pin_gdal_exception_mode()`:在每个 osgeo import
点把既定语义钉死(见该模块 docstring)。

⚠️ `gdal.ExceptionMgr(useExceptions=False)` 不能替代它,也就消不掉告警:那个只改线程
局部状态(实测 GDAL 3.11.4:块内确实不告警),出块即还原,进程仍算「没表态」。所以
下面第一条测试必须跑**真正的多进程切片作业**,而不是在本进程里断言一个标志位。
"""

import os
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from osgeo import gdal  # noqa: E402

from src.core.gdal_mode import pin_gdal_exception_mode  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 两条告警文案(gdal / osr 各一套,共用同一个每进程去重标记)。
_WARNING_MARKERS = ("Neither gdal.UseExceptions", "Neither osr.UseExceptions")

_TILING_JOB = '''
import os, sys
sys.path.insert(0, {repo!r})

import numpy as np
from osgeo import gdal, osr

# 与生产同序:先 import 切片器(声明就发生在这一步),再做 GDAL 活儿。放到
# __main__ 里再 import 的话,下面造 DEM 的 GDAL 调用会先跑,那条告警来自本
# 脚本自己的夹具而不是被测代码。
from src.services.terrain_tiling.cesium_terrain import build_terrain


def make_dem(path, n=600):
    ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((86.0, 1.0 / 3600, 0, 42.0, 0, -1.0 / 3600))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    yy, xx = np.mgrid[0:n, 0:n]
    ds.GetRasterBand(1).WriteArray(
        (1000 + 300 * np.sin(xx / 40.0) * np.cos(yy / 37.0)).astype(np.float32))
    ds.FlushCache()


if __name__ == "__main__":
    work = {work!r}
    dem = os.path.join(work, "dem.tif")
    make_dem(dem)
    # 两幅输入 -> 走 build_input_raster 的物化分支(主进程侧的 GDAL 调用),
    # workers=2 且 total>4 -> 走 ProcessPoolExecutor 的 spawn 分支(worker 侧)。
    r = build_terrain([dem, dem], os.path.join(work, "out"), min_level=0,
                      max_level=4, tile_size=65, workers=2,
                      triangulator="grid", normals=False)
    print("RESULT", r["total"], r["rendered"], r["failed"])
'''


def test_tiling_job_emits_no_gdal_future_warning(tmp_path):
    """整条切片作业(主进程 + spawn worker)不得再往 stderr 上刷 FutureWarning。

    断言 rendered==total 是必要的第二半:worker 里但凡因为告警->异常之类的原因崩掉,
    池会回退串行重跑(build_terrain 的 BrokenProcessPool 分支),那时 stderr 干净只是
    因为 worker 根本没活到打印告警,测试不能因此假绿。
    """
    script = tmp_path / "tiling_job.py"
    script.write_text(_TILING_JOB.format(repo=REPO_ROOT, work=str(tmp_path)),
                      encoding="utf-8")

    proc = subprocess.run([sys.executable, str(script)], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=600)

    assert proc.returncode == 0, f"切片作业本身失败了:\n{proc.stdout}\n{proc.stderr}"
    for marker in _WARNING_MARKERS:
        assert marker not in proc.stderr, (
            f"进程没有显式声明 GDAL 异常模式,stderr 上又出现了 {marker!r} —— "
            f"检查 src/core/gdal_mode.pin_gdal_exception_mode() 是否在每个 "
            f"osgeo import 点被调到:\n{proc.stderr}")

    result = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
    assert result, f"没拿到切片结果:\n{proc.stdout}\n{proc.stderr}"
    total, rendered, failed = (int(v) for v in result[0].split()[1:4])
    assert total > 4, (
        f"total={total} <= 4,build_terrain 会走串行分支,这条测试就没覆盖到 worker 进程")
    assert (rendered, failed) == (total, 0), (
        f"切片结果不完整 rendered={rendered} failed={failed} total={total}"
        f"(worker 崩溃回退串行也是这个形态):\n{proc.stderr}")


def test_pin_never_overrides_an_explicit_exception_mode():
    """已经有人开了异常模式时,pin 必须让位。

    异常模式是**进程全局**的,contour_engine 无条件调 gdal.UseExceptions()
    (contour_engine.py:367/:659),而四条流水线共用一个 Flask 进程 —— pin 要是无条件
    DontUseExceptions,一个地形/地图任务的惰性 import 就能把正在跑的等高线作业从异常
    模式扳回去。那时 contour_engine.py:369 的裸 `gdal.Open` 失败会返回 None,
    下一行 `ds.GetRasterBand(1)` 变成 AttributeError,真实原因(哪个文件、为什么打不开)
    整个丢掉。
    """
    had = gdal.GetUseExceptions()
    gdal.UseExceptions()
    try:
        pin_gdal_exception_mode()
        assert gdal.GetUseExceptions() == 1, (
            "pin 把别人显式开着的异常模式关掉了")
    finally:
        if not had:
            gdal.DontUseExceptions()


def test_every_osgeo_import_site_declares_the_mode():
    """src/ 下每个 import osgeo 的模块都必须表态,否则告警会从新入口漏回来。

    告警每进程只打一次、由**最先**碰 GDAL 的那处触发,所以漏掉任何一个入口都可能让
    整条修复对某类任务失效(例如用户先开一张晕渲预览,再跑切片)。
    """
    src_root = os.path.join(REPO_ROOT, "src")
    offenders = []
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            if "from osgeo import" not in text and "import osgeo" not in text:
                continue
            # 两种合法的表态:调 pin(非异常模式),或自己无条件调 UseExceptions()
            # (contour_engine 就是后者,它的判错逻辑建立在异常之上)。
            if "pin_gdal_exception_mode(" in text or "gdal.UseExceptions()" in text:
                continue
            offenders.append(os.path.relpath(path, REPO_ROOT))

    assert not offenders, (
        "这些模块 import 了 osgeo 却没声明 GDAL 异常模式,第一次碰 GDAL 的进程会吃一条 "
        "FutureWarning,GDAL 4.0 起还会连语义一起换掉 —— "
        f"在 osgeo import 之后调 pin_gdal_exception_mode():{offenders}")


def test_pinned_mode_is_non_exception():
    """钉出来的默认值必须是**非异常**模式 —— 三条流水线的判错逻辑都建立在它上面。

    在子进程里验:异常模式是进程全局的,同一批测试里别人可能已经调过 UseExceptions()。
    """
    code = textwrap.dedent(f'''
        import sys
        sys.path.insert(0, {REPO_ROOT!r})
        from osgeo import gdal
        from src.core.gdal_mode import pin_gdal_exception_mode
        pin_gdal_exception_mode()
        assert gdal.GetUseExceptions() == 0, "pin 之后不是非异常模式"
        assert gdal.Open("/definitely/not/a/raster.tif") is None, \\
            "非异常模式下打不开的文件应返回 None 而不是抛异常"
        print("OK")
    ''')
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0 and "OK" in proc.stdout, (
        f"{proc.stdout}\n{proc.stderr}")
    for marker in _WARNING_MARKERS:
        assert marker not in proc.stderr, proc.stderr
