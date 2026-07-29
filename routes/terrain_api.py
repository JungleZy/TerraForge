"""
Terrain API routes

Endpoints for starting and querying DEM terrain tiling jobs.
"""

import logging

from flask import Blueprint, jsonify

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
        dem_task_manager.start_tiling(task_id)
        return jsonify({"success": True, "message": f"DEM tiling started for task {task_id}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@terrain_api_bp.route("/dem/<int:task_id>", methods=["GET"])
def get_dem_tiling_job(task_id: int):
    if not dem_task_manager:
        return jsonify({"error": "DEM task manager not initialized"}), 500
    job = dem_task_manager.get_tiling_job(task_id)
    return jsonify({"success": True, "job": job}), 200

