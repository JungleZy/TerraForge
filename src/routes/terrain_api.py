"""
Terrain API routes

Endpoints for starting and querying DEM terrain tiling jobs.
"""

import logging

from flask import Blueprint, jsonify, request

from src.services.geo_validation import coerce_maxzoom, coerce_vertex_normals

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
        # 处理弹窗可以指定切片最大层级、档位与法线开关（JSON body 或表单皆可）；
        # 不传则沿用配置默认。
        # quality 的合法性交给 manager（validate_tiling_quality）；连不可哈希的
        # {"quality": []} 也走这条。
        # maxzoom 则在这里过 coerce_maxzoom（三态收参，理由见下）。
        # 两者的非法值都抛 ValueError，由下面同一个 except 一并转 400 ——
        # 那个 except 不区分是谁抛的（与 local_terrain_api 的对应注释同口径）。
        # vertex_normals 没有第二道网：manager 只做 bool()，唯一把关点就是
        # 这里的 coerce_vertex_normals。
        payload = request.get_json(silent=True) or {}
        maxzoom = payload.get("maxzoom")
        if maxzoom is None:
            maxzoom = request.form.get("maxzoom")
        # 此前这里不校验、原样交给 manager；'auto' 落地后必须在入口就分清
        # 「自动」与「脏值」，否则拼错的 'AUTO' 会被 manager 的 validate_zoom
        # 报成「不是数字」，把人指向一个不存在的数字问题。
        maxzoom = coerce_maxzoom(maxzoom, "maxzoom")
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

