"""
Terrain API routes

Endpoints for starting and querying DEM terrain tiling jobs.
"""

import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

terrain_api_bp = Blueprint("terrain_api", __name__, url_prefix="/api/terrain")

dem_task_manager = None


def init_terrain_dem_task_manager(tm):
    global dem_task_manager
    dem_task_manager = tm
    logger.debug("DEM task manager initialized in terrain API routes")


def coerce_vertex_normals(raw):
    """把请求里的法线开关收成 True / False / None。None 表示「未传」，走配置默认。

    两种形态都要收：JSON body 给的是真布尔 `true`，multipart 表单给的是字符串
    `'true'`/`'false'`（布尔在本仓一律以这两个字面量传递，见 database.py:99）。
    空串按未传处理 —— 表单里没填的控件送上来就是空串。

    「未传」与「传了 false」必须分开：前者该沿用配置默认，后者是用户明确关掉。
    认不出来的值当场 ValueError（路由转 400），不静默折成 False —— 「开关传了个
    'on'、瓦片却没烘法线、全程零报错」正是本仓栽过的那类假象，与
    validate_tiling_quality 同一约定：拼错就报错，不猜。
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    # 不用 in {...}：JSON 可能送来不可哈希的值（如 []），元组比较走 == 不会炸。
    if raw in ("true", "false"):
        return raw == "true"
    raise ValueError(f"vertex_normals ({raw!r}) must be one of: true, false")


@terrain_api_bp.route("/dem/<int:task_id>/start", methods=["POST"])
def start_dem_tiling(task_id: int):
    if not dem_task_manager:
        return jsonify({"error": "DEM task manager not initialized"}), 500
    try:
        # 处理弹窗可以指定切片最大层级、档位与法线开关（JSON body 或表单皆可）；
        # 不传则沿用配置默认。
        # 具体校验交给 manager 的 validate_zoom / validate_tiling_quality，非法值
        # 抛 ValueError → 下面转 400（连不可哈希的 {"quality": []} 也走这条）。
        payload = request.get_json(silent=True) or {}
        maxzoom = payload.get("maxzoom")
        if maxzoom is None:
            maxzoom = request.form.get("maxzoom")
        if maxzoom == "":
            maxzoom = None
        quality = payload.get("quality")
        if quality is None:
            quality = request.form.get("quality")
        if quality == "":
            quality = None
        raw_normals = payload.get("vertex_normals")
        if raw_normals is None:
            raw_normals = request.form.get("vertex_normals")
        vertex_normals = coerce_vertex_normals(raw_normals)
        dem_task_manager.start_tiling(task_id, maxzoom=maxzoom, quality=quality,
                                      vertex_normals=vertex_normals)
        return jsonify({"success": True, "message": f"DEM tiling started for task {task_id}"}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        # 服务器内部错误不能谎报成 400 客户端错误
        logger.error(f"Error starting DEM tiling for task {task_id}: {e}")
        return jsonify({"error": "Failed to start DEM tiling"}), 500


@terrain_api_bp.route("/dem/<int:task_id>", methods=["GET"])
def get_dem_tiling_job(task_id: int):
    if not dem_task_manager:
        return jsonify({"error": "DEM task manager not initialized"}), 500
    try:
        job = dem_task_manager.get_tiling_job(task_id)
        return jsonify({"success": True, "job": job}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error getting DEM tiling job for task {task_id}: {e}")
        return jsonify({"error": "Failed to get DEM tiling job"}), 500

