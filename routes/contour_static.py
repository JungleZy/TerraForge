"""
Static contour tile serving — XYZ raster PNG tiles for contour tasks.
"""

import logging
from pathlib import Path

from flask import Blueprint, abort, current_app, send_file

from core.config import Config
from core.database import get_connection
from routes.terrain_static import _resolve_safe_file

logger = logging.getLogger(__name__)

contour_static_bp = Blueprint("contour_static", __name__, url_prefix="/contour")

# 任务存在性缓存：瓦片请求量大，存在性永真（任务只在删除时消失），没必要每瓦片
# 查一次 SELECT id。只缓存正结果（查不到不缓存，新任务立即可见，不存在的 id
# 每次都落 DB 返 404 —— 行为与直查 DB 一致）；删除任务时路由层必须调
# invalidate_known_task，否则 delete_files=false（磁盘瓦片保留）时已删任务的
# 瓦片仍可访问。缓存挂 app.extensions 而非模块级：测试 fresh-import app 时拿到
# 干净缓存，避免跨用例串库（生产单 app 语义相同）。
_CACHE_KEY = "contour_static_known_tasks"


def _known_tasks() -> set:
    return current_app.extensions.setdefault(_CACHE_KEY, set())


def invalidate_known_task(task_id: int) -> None:
    """任务删除时由路由层调用（请求上下文内），清掉该任务的存在性缓存项。"""
    _known_tasks().discard(task_id)


def _task_exists(task_id: int) -> bool:
    known = _known_tasks()
    if task_id in known:
        return True
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM contour_tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    known.add(task_id)
    return True


@contour_static_bp.route("/<int:task_id>/<path:subpath>", methods=["GET"])
def contour_tile_static(task_id: int, subpath: str):
    # 与 tiles_static/terrain_static 一致:先查任务存在性再发文件
    if not _task_exists(task_id):
        abort(404)

    base_dir = Path(Config.DOWNLOADS_DIR) / "dem" / f"contour_task_{task_id}" / "contour_tiles"
    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    response = send_file(str(target))
    # task_id 是 AUTOINCREMENT 不复用，同一 URL 内容永不变，可 immutable 长缓存
    # （参照 app.py /static/vendor/ 钩子）
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response
