from __future__ import annotations

from pathlib import Path
from typing import List


def list_dem_tifs(task_dir: Path) -> List[Path]:
    """
    Return sorted DEM GeoTIFF paths under task_dir.

    Only files matching "*_dem.tif" are returned; "*_num.tif" is ignored.
    """
    tifs = [p for p in task_dir.glob("*_dem.tif") if p.is_file()]
    return sorted(tifs, key=lambda p: p.name)


def build_vrt_command(vrt_path: Path, tif_paths: List[Path]) -> str:
    """
    Build a gdalbuildvrt command string. Quote Windows paths.
    """
    args = ['gdalbuildvrt', f'"{str(vrt_path)}"']
    args.extend(f'"{str(p)}"' for p in tif_paths)
    return " ".join(args)

