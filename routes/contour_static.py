"""
Static contour tile serving — XYZ raster PNG tiles for contour tasks.
"""

import logging
from pathlib import Path

from flask import Blueprint, abort, send_file

from core.config import Config
from core.database import get_connection
from routes.terrain_static import _resolve_safe_file

logger = logging.getLogger(__name__)

contour_static_bp = Blueprint("contour_static", __name__, url_prefix="/contour")


@contour_static_bp.route("/<int:task_id>/<path:subpath>", methods=["GET"])
def contour_tile_static(task_id: int, subpath: str):
    # 与 tiles_static/terrain_static 一致:先查任务存在性再发文件
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM contour_tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)

    base_dir = Path(Config.DOWNLOADS_DIR) / "dem" / f"contour_task_{task_id}" / "contour_tiles"
    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    return send_file(str(target))
