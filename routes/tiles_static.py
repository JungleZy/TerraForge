"""
Map-task XYZ tile serving — 已完成地图瓦片任务的历史预览。

瓦片在磁盘上的布局是 <output_path>/task_<id>/<z>/<x>/<y>.png（task_manager
按 output_format 复制 raw tiles）。output_path 是创建时存的，可能是相对路径
（./downloads/map）或绝对路径 —— 用 terrain_static 的 _resolve_config_path
解析（打包后 DOWNLOADS_DIR 锚在 exe 目录，不能信存的绝对路径）。
"""

import logging
from pathlib import Path

from flask import Blueprint, abort, send_file

from database import get_connection
from routes.terrain_static import _resolve_config_path, _resolve_safe_file

logger = logging.getLogger(__name__)

tiles_static_bp = Blueprint("tiles_static", __name__, url_prefix="/tiles")


@tiles_static_bp.route("/<int:task_id>/<path:subpath>", methods=["GET"])
def map_task_tile_static(task_id: int, subpath: str):
    conn = get_connection()
    try:
        row = conn.execute("SELECT output_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)

    base_dir = _resolve_config_path(row["output_path"]) / f"task_{task_id}"
    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    return send_file(str(target))
