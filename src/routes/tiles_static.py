"""
Map-task XYZ tile serving — 已完成地图瓦片任务的历史预览。

瓦片在磁盘上的布局是 <output_path>/task_<id>/<z>/<x>/<y>.png（task_manager
按 output_format 复制 raw tiles）。output_path 是创建时存的，可能是相对路径
（./downloads/map）或绝对路径 —— 用 terrain_static 的 _resolve_config_path
解析（打包后 DOWNLOADS_DIR 锚在 exe 目录，不能信存的绝对路径）。
"""

import logging
from pathlib import Path

from flask import Blueprint, abort, current_app, send_file

from src.core.database import get_connection
from src.routes.terrain_static import _resolve_config_path, _resolve_safe_file

logger = logging.getLogger(__name__)

tiles_static_bp = Blueprint("tiles_static", __name__, url_prefix="/tiles")

# task_id -> output_path 缓存：一次地图浏览就是几百上千次瓦片请求，output_path
# 创建后永不变，没必要每瓦片新开连接查一次。只缓存正结果（查不到不缓存，新任务
# 立即可见）；删除任务时路由层必须调 invalidate_output_path_cache —— 任务行没了
# 但磁盘瓦片可能还在（delete_files 默认 false），不失效的话已删任务的瓦片还能
# 访问到，与直查 DB 的行为不一致。
# 缓存挂在 app.extensions 上而非模块级：测试每次 fresh-import app 都得到干净
# 缓存，避免跨用例串库（模块不会被重导入）；生产单 app 语义相同。
_CACHE_KEY = "tiles_static_output_path"


def _cache() -> dict:
    return current_app.extensions.setdefault(_CACHE_KEY, {})


def invalidate_output_path_cache(task_id: int) -> None:
    """任务删除时由路由层调用（请求上下文内），清掉该任务的 output_path 缓存项。"""
    _cache().pop(task_id, None)


def _get_output_path(task_id: int):
    cache = _cache()
    if task_id in cache:
        return cache[task_id]
    conn = get_connection()
    try:
        row = conn.execute("SELECT output_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    cache[task_id] = row["output_path"]
    return row["output_path"]


@tiles_static_bp.route("/<int:task_id>/<path:subpath>", methods=["GET"])
def map_task_tile_static(task_id: int, subpath: str):
    output_path = _get_output_path(task_id)
    if output_path is None:
        abort(404)

    base_dir = _resolve_config_path(output_path) / f"task_{task_id}"
    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    response = send_file(str(target))
    # task_id 是 AUTOINCREMENT 不复用，同一 URL 内容永不变，可 immutable 长缓存
    # （参照 app.py /static/vendor/ 钩子）；地图浏览重复拉取历史瓦片直接走浏览器缓存
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response
