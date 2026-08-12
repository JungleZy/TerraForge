"""
Contour task tiler.

Thin wrapper around contour_engine.build_contour_tiles with a lazy default so
tests can inject build_contour_fn=<fake> without GDAL/matplotlib (mirrors
src/services/terrain_tiling/dem_task_tiler.py). DEM listing reuses the existing
vrt_builder.list_dem_tifs (which filters *_num.tif).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from src.services.contour_engine import ContourStyle
from src.services.terrain_tiling.vrt_builder import list_dem_tifs, list_att_tifs


@dataclass(frozen=True)
class ContourParams:
    interval: float
    zoom_min: int
    zoom_max: int
    style: ContourStyle
    shade: bool = False
    water: bool = False
    # 0 = auto (min(4, os.cpu_count())，见 contour_engine); 1 = serial。
    # 应用侧从 wave B 起恒传 ResourceScheduler 授予的 CPU_WORKER 名额
    # （contour_task_manager.start_task 起任务时 reserve），0 只留给直调与测试：
    # 每个任务各自算 min(4, cpu_count) 等于没有全局上限。
    workers: int = 0
    # 运行中磁盘复查（`disk_budget.RunningRecheck`）。None = 不查：直调、CLI、
    # 测试那一档压根没经过 scheduler，也就没有自己那份 DISK_BYTES 预留可排除。
    # 放在 params 而不是 tile_contour_task_dir 的独立参数：十来个契约测试用
    # (task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
    # stage_cb=None, stop_flag=None) 的替身钉住了本函数的调用形态。
    disk_recheck: object = None


# 曾有一个 contour_output_dir_for_task(task_output_path, task_id) 住在这里，
# 把 `Path(存储值)/contour_task_<id>/contour_tiles` 又写了一遍。它零生产调用
# （writer 在 _execute 里自己算，路由和删除各有一处），但用的是**裸 Path**，
# 也就是那条被 2026-08-08 评审判为错的规则 —— 存量相对值按进程 CWD 解析、
# frozen exe 搬动后按旧绝对路径解析。留一个没人调用、写法又是错的第四份拷贝，
# 只会把下一个人引到错的那条路上。产物根的唯一口径是
# task_cleanup.resolve_stored_output_dir。
def tile_contour_task_dir(
    task_dir,
    out_dir,
    params: ContourParams,
    build_contour_fn: Optional[Callable] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    stage_cb: Optional[Callable[[str, float], None]] = None,
    stop_flag=None,
) -> dict:
    task_dir = Path(task_dir)
    out_dir = Path(out_dir)

    dem_tifs = list_dem_tifs(task_dir)
    if not dem_tifs:
        # 与 build_contour_tiles 的正常返回保持同一 4 键结构
        return {"total": 0, "rendered": 0, "failed": 0, "skipped": 0}

    # Water mask is best-effort: render whatever ASTWBD att tiles downloaded.
    att_tifs = list_att_tifs(task_dir) if params.water else []

    if build_contour_fn is None:
        from src.services.contour_engine import build_contour_tiles as build_contour_fn

    # disk_recheck **只在真的有值时才传**：tests 里的 fake_build 替身是按
    # 老签名（... water=False, att_tifs=None, workers=0）写死的位置/关键字
    # 参数，无条件多塞一个 kwarg 会让它们全部 TypeError。别把它「清理」成
    # 无条件传参（同 task_manager 对 source= / max_concurrency= 的做法）。
    extra = {} if params.disk_recheck is None else {'disk_recheck': params.disk_recheck}

    return build_contour_fn(
        dem_tifs=dem_tifs,
        out_dir=out_dir,
        interval=params.interval,
        zoom_min=params.zoom_min,
        zoom_max=params.zoom_max,
        style=params.style,
        progress_cb=progress_cb,
        stage_cb=stage_cb,
        stop_flag=stop_flag,
        shade=params.shade,
        water=params.water,
        att_tifs=att_tifs,
        workers=params.workers,
        **extra,
    )
