"""进程级 GDAL 异常模式声明。

GDAL 3.7+ 起,凡是**从没**调过 `gdal.UseExceptions()` / `gdal.DontUseExceptions()`
的进程,第一次碰 `gdal.Open` / `Translate` / `BuildVRT` / `osr.SpatialReference()`
这类 API 时会打一条:

    FutureWarning: Neither gdal.UseExceptions() nor gdal.DontUseExceptions() has
    been explicitly called. In GDAL 4.0, exceptions will be enabled by default.

去重标记是 osgeo 的模块属性,所以**每进程一条**。一次地形切片作业固定刷 5 条:
主进程 1 条 + 4 个 spawn worker 各 1 条(worker 在 `_worker_init` ->
`DemSampler.__init__` -> `gdal.Open` 上第一次碰 GDAL,见 cesium_terrain.py:707)。

它不是噪音,说的是 GDAL 4.0 会把默认翻成**异常模式**,而本仓三条流水线的判错逻辑
建立在「非异常模式」上:失败靠 `gdal.Open(...) is None` 判、靠
`gdal.GetLastErrorType() >= CE_Failure` 读 CPL 错误栈(cesium_terrain
`_raise_on_gdal_error`、download_engine 的 stitch 闸门)。默认一翻,那些分支变死
代码、错误文案集体换人。所以这里把**当前既定语义显式钉死**为非异常模式:告警消失
是副产品,真正买到的是「GDAL 4 不会静悄悄改我们的行为」。将来要整体迁到异常模式,
改这一处 + 那份逐条破坏点清单即可(docs/reviews/2026-08-09-full-project-review.md:268
记了其中最要命的一条:hillshade_preview 的 `gdal.Unlink` 在异常模式下会盖掉真实错误)。

⚠️ 为什么不能用 `gdal.ExceptionMgr(useExceptions=False)` 代替:那个只改线程局部
状态,with 块内确实不告警(实测 GDAL 3.11.4),但出块即还原,进程仍然算「没表态」,
块外下一处调用照样告警。两者互补:ExceptionMgr 守临界区,本模块定进程默认值。
"""


def pin_gdal_exception_mode() -> None:
    """把本进程的 GDAL 异常模式显式钉成「非异常」。幂等,在任何 osgeo import 之后调。

    已经有人显式开了异常模式时**不覆盖** —— contour_engine 无条件调
    `gdal.UseExceptions()`(contour_engine.py:367/:659),而这个开关是**进程全局**的,
    四条流水线共用一个 Flask 进程:把别人正在跑的作业从异常模式扳回去,等于自己造一个
    竞态。这种时候进程本来也已经「表过态」,不会有告警,跳过没有代价。
    (tests/test_fix_gdal_silent_failure_gaps.py 里「全局 UseExceptions 后调用方状态
    不被改」的两条断言,守的就是这个 if。)
    """
    from osgeo import gdal

    if not gdal.GetUseExceptions():
        # 一次调用覆盖 gdal/ogr/osr/gnm 四个模块,见 osgeo/gdal.py DontUseExceptions()。
        gdal.DontUseExceptions()
