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


@terrain_api_bp.route("/dem/<int:task_id>/start", methods=["POST"])
def start_dem_tiling(task_id: int):
    if not dem_task_manager:
        return jsonify({"error": "DEM task manager not initialized"}), 500
    try:
        # 处理弹窗可以指定切片最大层级（JSON body 或表单皆可）；不传则沿用配置默认。
        # 具体校验交给 manager 的 validate_zoom，非法值抛 ValueError → 下面转 400。
        payload = request.get_json(silent=True) or {}
        maxzoom = payload.get("maxzoom")
        if maxzoom is None:
            maxzoom = request.form.get("maxzoom")
        if maxzoom == "":
            maxzoom = None
        dem_task_manager.start_tiling(task_id, maxzoom=maxzoom)
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

