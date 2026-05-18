"""
Static terrain serving routes

Serve CesiumJS terrain resources (layer.json + .terrain files) from local downloads.
"""

import logging
from pathlib import Path

from flask import Blueprint, abort, send_file

from config import Config
from services.config_manager import ConfigManager

logger = logging.getLogger(__name__)

terrain_static_bp = Blueprint("terrain_static", __name__, url_prefix="/terrain")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_config_path(path_str: str) -> Path:
    """
    Resolve config path to an absolute Path.

    Rule:
      - Absolute paths stay absolute.
      - Relative paths starting with ./downloads (or downloads) are rebased onto Config.DOWNLOADS_DIR.
      - Other relative paths are resolved against Config.BASE_DIR.
    """
    p = Path((path_str or "").strip())
    if p.is_absolute():
        return p.resolve()

    # Normalize common "downloads-relative" forms.
    s = (path_str or "").strip().replace("\\", "/")
    if s.startswith("./downloads/"):
        return (Path(Config.DOWNLOADS_DIR) / s[len("./downloads/"):]).resolve()
    if s == "./downloads":
        return Path(Config.DOWNLOADS_DIR).resolve()
    if s.startswith("downloads/"):
        return (Path(Config.DOWNLOADS_DIR) / s[len("downloads/"):]).resolve()
    if s == "downloads":
        return Path(Config.DOWNLOADS_DIR).resolve()

    return (Path(Config.BASE_DIR) / p).resolve()


def _resolve_safe_file(base_dir: Path, subpath: str) -> Path:
    downloads_root = Path(Config.DOWNLOADS_DIR).resolve()

    base_dir = base_dir.resolve()
    if not _is_within(base_dir, downloads_root):
        abort(400, description="base_dir must be under downloads")

    # Guard against backslash-based traversal on Windows.
    subpath = (subpath or "").replace("\\", "/")
    target = (base_dir / subpath).resolve()

    if not _is_within(target, base_dir):
        abort(400, description="path traversal blocked")
    if not _is_within(target, downloads_root):
        abort(400, description="file must be under downloads")

    return target


@terrain_static_bp.route("/base/<path:subpath>", methods=["GET"])
def terrain_base_static(subpath: str):
    cfg = ConfigManager()
    base_path = cfg.get("terrain_global_base_path", "./downloads/terrain/base_z8")
    base_dir = _resolve_config_path(base_path)

    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    return send_file(str(target))


@terrain_static_bp.route("/dem/<task_id>/<path:subpath>", methods=["GET"])
def terrain_dem_static(task_id: str, subpath: str):
    base_dir = Path(Config.DOWNLOADS_DIR) / "dem" / f"dem_task_{task_id}" / "terrain_tiles"
    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    return send_file(str(target))

