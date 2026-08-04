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
    workers: int = 0  # 0 = auto (min(4, os.cpu_count())，见 contour_engine); 1 = serial


def contour_output_dir_for_task(task_output_path: str, task_id: int) -> Path:
    return Path(task_output_path) / f"contour_task_{task_id}" / "contour_tiles"


def tile_contour_task_dir(
    task_dir,
    out_dir,
    params: ContourParams,
    build_contour_fn: Optional[Callable] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
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

    return build_contour_fn(
        dem_tifs=dem_tifs,
        out_dir=out_dir,
        interval=params.interval,
        zoom_min=params.zoom_min,
        zoom_max=params.zoom_max,
        style=params.style,
        progress_cb=progress_cb,
        stop_flag=stop_flag,
        shade=params.shade,
        water=params.water,
        att_tifs=att_tifs,
        workers=params.workers,
    )
