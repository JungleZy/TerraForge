import json
from pathlib import Path


def patch_layer_json_parent(layer_json_path: Path, parent_url: str) -> None:
    data = json.loads(layer_json_path.read_text(encoding="utf-8"))
    data["parentUrl"] = parent_url
    layer_json_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def compute_available_from_tiles(tiles_root: Path, minzoom: int, maxzoom: int) -> list:
    """
    Scan on-disk `{z}/{x}/{y}.terrain` tiles and compute a single bounding rectangle per z.

    Returns a list where each entry corresponds to zoom z in [minzoom, maxzoom], and contains
    either [] (no tiles) or a single rectangle dict.
    """

    available: list[list[dict]] = []
    for z in range(minzoom, maxzoom + 1):
        z_dir = tiles_root / str(z)
        if not z_dir.is_dir():
            available.append([])
            continue

        min_x = None
        max_x = None
        min_y = None
        max_y = None

        for x_dir in z_dir.iterdir():
            if not x_dir.is_dir():
                continue
            try:
                x = int(x_dir.name)
            except ValueError:
                continue

            for tile in x_dir.glob("*.terrain"):
                try:
                    y = int(tile.stem)
                except ValueError:
                    continue

                min_x = x if min_x is None else min(min_x, x)
                max_x = x if max_x is None else max(max_x, x)
                min_y = y if min_y is None else min(min_y, y)
                max_y = y if max_y is None else max(max_y, y)

        if min_x is None:
            available.append([])
            continue

        available.append(
            [
                {
                    "startX": min_x,
                    "startY": min_y,
                    "endX": max_x,
                    "endY": max_y,
                }
            ]
        )

    return available

