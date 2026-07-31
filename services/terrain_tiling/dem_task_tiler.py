from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from services.terrain_tiling.layer_json import patch_layer_json_parent
from services.terrain_tiling.vrt_builder import list_dem_tifs


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


def tile_dem_task_dir(
    task_dir: Path,
    out_dir: Path,
    params: TileParams,
    build_terrain_fn: Optional[Callable[..., None]] = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    dem_tifs = list_dem_tifs(task_dir)
    if not dem_tifs:
        raise ValueError(f"No DEM tifs found under {task_dir}")

    # Use cesiumlab_terrain.py as the source of truth for tiling behavior.
    # Import lazily so unit tests can inject a stub without requiring numpy/GDAL.
    if build_terrain_fn is None:
        try:
            from services.terrain_tiling.cesiumlab_terrain import build_terrain as build_terrain_fn  # type: ignore[assignment]
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "Terrain tiling runtime deps missing (need numpy + GDAL bindings). "
                "Install them, or inject build_terrain_fn for tests."
            ) from e

    build_terrain_fn(
        inputs=[str(p) for p in dem_tifs],
        output_dir=str(out_dir),
        min_level=0,
        max_level=int(params.maxzoom),
        tile_size=int(params.tile_size),
        workers=int(params.workers),
    )

    layer_json_path = out_dir / "layer.json"
    if not layer_json_path.is_file():
        raise FileNotFoundError(f"Missing layer.json at {layer_json_path}")

    patch_layer_json_parent(layer_json_path, params.parent_url)
