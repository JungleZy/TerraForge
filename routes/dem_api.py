"""
DEM API routes

Endpoints for creating and running DEM download tasks (e.g., ASTGTM.003).
"""

import logging
from pathlib import Path
from flask import Blueprint, jsonify, request

from services.task_cleanup import remove_task_dir_if_safe

logger = logging.getLogger(__name__)

dem_api_bp = Blueprint("dem_api", __name__, url_prefix="/api/dem")

dem_task_manager = None


def init_dem_task_manager(tm):
    global dem_task_manager
    dem_task_manager = tm
    logger.debug("DEM task manager initialized in DEM API routes")


@dem_api_bp.route("/tasks", methods=["POST"])
def create_dem_task():
    try:
        if not dem_task_manager:
            return jsonify({"error": "DEM task manager not initialized"}), 500

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Request body must be a JSON object"}), 400
        required = ["name", "north", "south", "east", "west", "output_path"]
        missing = [k for k in required if k not in data]
        if missing:
            return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

        task_id = dem_task_manager.create_task(data)
        return jsonify({"success": True, "task_id": task_id}), 201

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error creating DEM task: {e}")
        return jsonify({"error": "Failed to create DEM task"}), 500


@dem_api_bp.route("/tasks", methods=["GET"])
def list_dem_tasks():
    try:
        if not dem_task_manager:
            return jsonify({"error": "DEM task manager not initialized"}), 500

        limit = request.args.get("limit", 100, type=int)
        # ?status=active 是契约特殊值（同 /api/tasks、/api/history_all）：只回
        # 活动三态；不传 status 时行为完全不变。
        status = request.args.get("status", None, type=str)
        tasks = dem_task_manager.list_tasks(limit=limit, status=status)
        return jsonify({"success": True, "tasks": tasks, "count": len(tasks)})
    except Exception as e:
        logger.error(f"Error listing DEM tasks: {e}")
        return jsonify({"error": "Failed to list DEM tasks"}), 500


@dem_api_bp.route("/tasks/<int:task_id>", methods=["GET"])
def get_dem_task(task_id: int):
    try:
        if not dem_task_manager:
            return jsonify({"error": "DEM task manager not initialized"}), 500
        task = dem_task_manager.get_task(task_id)
        return jsonify({"success": True, "task": task})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error getting DEM task {task_id}: {e}")
        return jsonify({"error": "Failed to get DEM task"}), 500


@dem_api_bp.route("/tasks/<int:task_id>", methods=["DELETE"])
def delete_dem_task(task_id: int):
    try:
        if not dem_task_manager:
            return jsonify({"error": "DEM task manager not initialized"}), 500

        task = dem_task_manager.get_task(task_id)
        # 锁内复查下载线程 + 任务状态 + tiling job(范本: contour_task_manager.delete_task),
        # 下载中或 tiling 中的任务拒绝删除 —— 此前这里只查下载线程,tiling 中删除会
        # rmtree 正在写入的 terrain_tiles/,job 行被 CASCADE 静默删除。
        dem_task_manager.delete_task(task_id)

        # Optional best-effort artifact cleanup (downloads/dem/dem_task_<id>/)
        # after the row is gone; only removed when inside DOWNLOADS_DIR.
        if request.args.get("delete_files", "").lower() in ("1", "true", "yes"):
            remove_task_dir_if_safe(Path(task["output_path"]) / f"dem_task_{task_id}")

        return jsonify({"success": True, "message": f"DEM task {task_id} deleted"})

    except ValueError as e:
        # get_task/delete_task 的 "not found" -> 404;下载中/tiling 中拒绝删除 -> 400
        msg = str(e)
        return jsonify({"error": msg}), (404 if "not found" in msg else 400)
    except Exception as e:
        logger.error(f"Error deleting DEM task {task_id}: {e}")
        return jsonify({"error": "Failed to delete DEM task"}), 500


@dem_api_bp.route("/tasks/<int:task_id>/start", methods=["POST"])
def start_dem_task(task_id: int):
    try:
        if not dem_task_manager:
            return jsonify({"error": "DEM task manager not initialized"}), 500
        dem_task_manager.start_task(task_id)
        return jsonify({"success": True, "message": f"DEM task {task_id} started"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error starting DEM task {task_id}: {e}")
        return jsonify({"error": "Failed to start DEM task"}), 500


@dem_api_bp.route("/tasks/<int:task_id>/pause", methods=["POST"])
def pause_dem_task(task_id: int):
    try:
        if not dem_task_manager:
            return jsonify({"error": "DEM task manager not initialized"}), 500
        dem_task_manager.pause_task(task_id)
        return jsonify({"success": True, "message": f"DEM task {task_id} paused"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error pausing DEM task {task_id}: {e}")
        return jsonify({"error": "Failed to pause DEM task"}), 500


@dem_api_bp.route("/tasks/<int:task_id>/resume", methods=["POST"])
def resume_dem_task(task_id: int):
    try:
        if not dem_task_manager:
            return jsonify({"error": "DEM task manager not initialized"}), 500
        dem_task_manager.resume_task(task_id)
        return jsonify({"success": True, "message": f"DEM task {task_id} resumed"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error resuming DEM task {task_id}: {e}")
        return jsonify({"error": "Failed to resume DEM task"}), 500


@dem_api_bp.route("/tasks/<int:task_id>/cancel", methods=["POST"])
def cancel_dem_task(task_id: int):
    try:
        if not dem_task_manager:
            return jsonify({"error": "DEM task manager not initialized"}), 500
        dem_task_manager.cancel_task(task_id)
        return jsonify({"success": True, "message": f"DEM task {task_id} cancelled"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error cancelling DEM task {task_id}: {e}")
        return jsonify({"error": "Failed to cancel DEM task"}), 500
