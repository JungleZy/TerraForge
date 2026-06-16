from __future__ import annotations

from pathlib import Path
from typing import List


def list_dem_tifs(task_dir: Path) -> List[Path]:
    """
    Return sorted DEM GeoTIFF paths under task_dir.

    Matches "*_dem.tif" (ASTER) and "*_DEM.tif" (Copernicus GLO-30); "*_num.tif"
    and "*_att.tif" do not match. Deduped (case-insensitive filesystems may match
    both patterns to the same file).
    """
    found = {}
    for pattern in ("*_dem.tif", "*_DEM.tif"):
        for p in task_dir.glob(pattern):
            if p.is_file():
                found[str(p)] = p
    return sorted(found.values(), key=lambda p: p.name)


def list_att_tifs(task_dir: Path) -> List[Path]:
    """
    Return sorted ASTWBD water-body attribute GeoTIFF paths under task_dir
    ("*_att.tif"). Kept separate from the DEM VRT — used only as a water mask.
    """
    tifs = [p for p in task_dir.glob("*_att.tif") if p.is_file()]
    return sorted(tifs, key=lambda p: p.name)


def build_vrt_command(vrt_path: Path, tif_paths: List[Path]) -> str:
    """
    Build a gdalbuildvrt command string. Quote Windows paths.
    """
    args = ['gdalbuildvrt', f'"{str(vrt_path)}"']
    args.extend(f'"{str(p)}"' for p in tif_paths)
    return " ".join(args)

