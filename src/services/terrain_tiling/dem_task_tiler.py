from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from src.services.terrain_tiling.layer_json import patch_layer_json_parent
from src.services.terrain_tiling.vrt_builder import list_dem_tifs


def terrain_output_dir_for_task(task_output_path: str, task_id: int) -> Path:
    return Path(task_output_path) / f"dem_task_{task_id}" / "terrain_tiles"


@dataclass(frozen=True)
class TileParams:
    maxzoom: int
    parent_url: str
    # 65x65 vertex grid: at z14 this samples ~19 m spacing, matching 30 m DEMs
    # (Copernicus GLO-30 / ASTER). estimate_max_level in cesiumlab_terrain.py
    # derives the per-tile interval from tile_size (180/(tile_size-1) deg).
    tile_size: int = 65
    workers: int = 0
    # 三角化后端与误差系数。默认即最终值 —— UI/DB/API 都不暴露，这两个字段
    # 只为排障与测试注入而存在（出问题时切 'grid' / 'martini' 做对比）。
    # 'auto' = 逐瓦片择优：grid 与 martini 都编一遍，取 gzip 后更小的那个落盘。
    # 瓦片是 gzip 落盘、gzip 原样上线，所以 gzip 后的字节才是磁盘与传输的真实
    # 成本；实测山地上 martini 反而 +17.6%，择优后全局净省 27.6% 且每张瓦片
    # 严格不劣于两者（详见 cesiumlab_terrain._choose_tile_bytes）。
    # 注意 'auto'/'martini' 要求 tile_size = 2^k+1（65 满足），build_terrain
    # 入口会校验并报错，不会静默降级。
    triangulator: str = "auto"
    max_error_k: float = 0.15
    # 进度回调/协作停止透传给 build_terrain（默认 None = 关闭）。放在 params
    # 而不是 tile_dem_task_dir 的独立参数：多个契约测试用 (task_dir, out_dir,
    # params) 三参替身钉住管理器到 tiler 的调用形态，加独立参数会破坏它们。
    progress_cb: Optional[Callable[[int, int], None]] = None
    # stage_cb(phase, fraction)：瓦片循环之前那些耗时阶段（多幅 DEM 物化成单
    # 文件、建金字塔）的进度。不能并进 progress_cb —— 那一段发生在 total 算出来
    # 之前，没有分母（详见 build_terrain 的注释）。
    stage_cb: Optional[Callable[[str, float], None]] = None
    stop_flag: Optional[threading.Event] = None


def tile_dem_task_dir(
    task_dir: Path,
    out_dir: Path,
    params: TileParams,
    build_terrain_fn: Optional[Callable[..., None]] = None,
) -> dict:
    """切片一个 DEM 任务目录，返回 build_terrain 的计数 dict。

    Returns:
        {"total", "rendered", "failed", "chose_martini", "chose_grid"}。后两个是
        逐瓦片择优的落点统计（哪个三角化后端赢了），排障用：全 grid 说明这批
        DEM 是山地/粗糙地形，全 martini 说明是平缓地形。M11 之前这里丢弃返回值
        （签名 `-> None`），于是 build_terrain 的逐瓦片容错（异常只记 warning）
        变成纯静默：缺瓦片的作业照报 completed，layer.json 还按完整矩形声明
        available，Cesium 请求后拿 404 且父层不兜底。极端情况下所有瓦片都失败、
        terrain_tiles/ 一片没有，job 仍标 completed。

        注入的 build_terrain_fn 返回 None（老测试替身）时归一成全 0 计数，
        调用方按「无计数信息」处理，行为与改动前一致。
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    dem_tifs = list_dem_tifs(task_dir)
    if not dem_tifs:
        raise ValueError(f"No DEM tifs found under {task_dir}")

    # Use cesiumlab_terrain.py as the source of truth for tiling behavior.
    # Import lazily so unit tests can inject a stub without requiring numpy/GDAL.
    if build_terrain_fn is None:
        try:
            from src.services.terrain_tiling.cesiumlab_terrain import build_terrain as build_terrain_fn  # type: ignore[assignment]
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "Terrain tiling runtime deps missing (need numpy + GDAL bindings). "
                "Install them, or inject build_terrain_fn for tests."
            ) from e

    counts = build_terrain_fn(
        inputs=[str(p) for p in dem_tifs],
        output_dir=str(out_dir),
        min_level=0,
        max_level=int(params.maxzoom),
        tile_size=int(params.tile_size),
        workers=int(params.workers),
        progress_cb=params.progress_cb,
        stage_cb=params.stage_cb,
        stop_flag=params.stop_flag,
        triangulator=params.triangulator,
        max_error_k=params.max_error_k,
    )

    layer_json_path = out_dir / "layer.json"
    if not layer_json_path.is_file():
        raise FileNotFoundError(f"Missing layer.json at {layer_json_path}")

    patch_layer_json_parent(layer_json_path, params.parent_url)

    keys = ("total", "rendered", "failed", "chose_martini", "chose_grid")
    if not isinstance(counts, dict):
        return dict.fromkeys(keys, 0)
    return {k: int(counts.get(k, 0) or 0) for k in keys}
