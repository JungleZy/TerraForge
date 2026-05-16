from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from services.terrain_tiling.layer_json import compute_available_from_tiles
from services.terrain_tiling.vrt_builder import list_dem_tifs


def terrain_output_dir_for_task(task_output_path: str, task_id: int) -> Path:
    return Path(task_output_path) / f"dem_task_{task_id}" / "terrain_tiles"


@dataclass(frozen=True)
class TileParams:
    maxzoom: int
    parent_url: str


def tile_dem_task_dir(
    task_dir: Path,
    out_dir: Path,
    params: TileParams,
    run_argv: Callable[[List[str], Optional[str]], object],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    dem_tifs = list_dem_tifs(task_dir)
    if not dem_tifs:
        raise ValueError(f"No DEM tifs found under {task_dir}")

    vrt_path = out_dir / "dem.vrt"
    run_argv(
        ["gdalbuildvrt", str(vrt_path), *[str(p) for p in dem_tifs]],
        None,
    )

    run_argv(
        [
            "ctb-tile",
            "-f",
            "Mesh",
            "-C",
            "-N",
            "-l",
            "-o",
            str(out_dir),
            str(vrt_path),
        ],
        None,
    )

    layer_json_path = out_dir / "layer.json"
    if not layer_json_path.is_file():
        raise FileNotFoundError(f"Missing layer.json at {layer_json_path}")

    data = json.loads(layer_json_path.read_text(encoding="utf-8"))
    data["parentUrl"] = params.parent_url
    data["available"] = compute_available_from_tiles(out_dir, 0, params.maxzoom)
    layer_json_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

