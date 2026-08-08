"""
Static contour tile serving — XYZ raster PNG tiles for contour tasks.
"""

import logging
from pathlib import Path
from typing import Optional

from flask import Blueprint, abort, current_app, send_file

from src.core.database import get_connection
from src.services.task_cleanup import resolve_stored_output_dir
from src.routes.terrain_static import _resolve_safe_file

logger = logging.getLogger(__name__)

contour_static_bp = Blueprint("contour_static", __name__, url_prefix="/contour")

# 任务缓存：瓦片请求量大，而「任务存不存在 + 它的产物根在哪」在任务生命周期内
# 不变（任务只在删除时消失），没必要每瓦片查一次 SELECT。缓存的是
# task_id -> 已解析的瓦片根目录。只缓存正结果（查不到不缓存，新任务立即可见，
# 不存在的 id 每次都落 DB 返 404 —— 行为与直查 DB 一致）；删除任务时路由层
# 必须调 invalidate_known_task，否则 delete_files=false（磁盘瓦片保留）时
# 已删任务的瓦片仍可访问。缓存挂 app.extensions 而非模块级：测试 fresh-import
# app 时拿到干净缓存，避免跨用例串库（生产单 app 语义相同）。
_CACHE_KEY = "contour_static_known_tasks"


def _known_tasks() -> dict:
    return current_app.extensions.setdefault(_CACHE_KEY, {})


def invalidate_known_task(task_id: int) -> None:
    """任务删除时由路由层调用（请求上下文内），清掉该任务的缓存项。"""
    _known_tasks().pop(task_id, None)


def _tile_root(task_id: int) -> Optional[Path]:
    """该任务的瓦片根目录；任务不存在、或行里没记产物位置时返回 None。

    根目录按**存储的 output_path** 解析，与写入方（contour_task_manager._execute）
    和删除方（task_cleanup.resolve_stored_output_dir）用同一套口径。此前这里重算
    `Config.DOWNLOADS_DIR / "dem" / ...`，只是因为两个构造器恰好写的就是这个值
    才对得上；frozen exe 被搬走后 BASE_DIR 变了（terrain_static 记录为真实场景），
    重跑的老任务把瓦片写在旧绝对路径下，而这里按新根去找 —— 瓦片在盘上却永久 404。
    """
    known = _known_tasks()
    cached = known.get(task_id)
    if cached is not None:
        return cached
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT output_path FROM contour_tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if not row or not str(row["output_path"] or "").strip():
        # output_path 可为 NULL（列是可空的）。空值走 resolve_stored_output_dir
        # 会落到 BASE_DIR 本身 —— 那是安装目录，不是任何任务的产物根，按
        # 「不知道产物在哪」处理，不要拿整个安装目录当瓦片根。
        return None
    root = (resolve_stored_output_dir(row["output_path"])
            / f"contour_task_{task_id}" / "contour_tiles")
    known[task_id] = root
    return root


@contour_static_bp.route("/<int:task_id>/<path:subpath>", methods=["GET"])
def contour_tile_static(task_id: int, subpath: str):
    # 与 tiles_static/terrain_static 一致:先查任务存在性再发文件
    base_dir = _tile_root(task_id)
    if base_dir is None:
        abort(404)

    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    response = send_file(str(target))
    # task_id 是 AUTOINCREMENT 不复用，同一 URL 内容永不变，可 immutable 长缓存
    # （参照 app.py /static/vendor/ 钩子）
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response
