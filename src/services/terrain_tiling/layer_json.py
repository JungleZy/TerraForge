import json
from pathlib import Path


def patch_layer_json_parent(layer_json_path: Path, parent_url: str) -> None:
    data = json.loads(layer_json_path.read_text(encoding="utf-8"))
    data["parentUrl"] = parent_url
    layer_json_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

