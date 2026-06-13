"""
Local Terrain API routes

Endpoints for uploading GeoTIFF files and tiling them into Cesium terrain.
"""

import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

local_terrain_api_bp = Blueprint("local_terrain_api", __name__, url_prefix="/api/terrain/local")

local_terrain_task_manager = None


def init_local_terrain_task_manager(tm):
    global local_terrain_task_manager
    local_terrain_task_manager = tm
    logger.info("Local terrain task manager initialized in local terrain API routes")


@local_terrain_api_bp.route("/tasks", methods=["POST"])
def create_local_terrain_task():
    if not local_terrain_task_manager:
        return jsonify({"error": "Local terrain task manager not initialized"}), 500
    try:
        name = request.form.get("name") or "Local Terrain Task"
        maxzoom_raw = request.form.get("maxzoom")
        maxzoom = int(maxzoom_raw) if maxzoom_raw not in (None, "") else None

        uploads = request.files.getlist("files")

        # Validate cheaply BEFORE reading any bytes into memory: cap the file
        # count and reject non-tif extensions up front. The total request size
        # is already capped by Config.MAX_CONTENT_LENGTH (Flask aborts oversized
        # bodies with 413 before this handler runs). The manager re-validates.
        if not uploads:
            return jsonify({"error": "No files uploaded"}), 400
        if len(uploads) > 100:
            return jsonify({"error": "Too many files (max 100 per task)"}), 400
        allowed_ext = (".tif", ".tiff")
        for f in uploads:
            ext = os.path.splitext(f.filename or "")[1].lower()
            if ext not in allowed_ext:
                return jsonify({"error": f"Unsupported file type: {f.filename} (only .tif/.tiff)"}), 400

        files = [(f.filename, f.read()) for f in uploads]

        task_id = local_terrain_task_manager.create_task_with_files(
            name=name, files=files, maxzoom=maxzoom
        )
        return jsonify({"success": True, "task_id": task_id}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error creating local terrain task: {e}")
        return jsonify({"error": "Failed to create local terrain task"}), 500


@local_terrain_api_bp.route("/tasks", methods=["GET"])
def list_local_terrain_tasks():
    if not local_terrain_task_manager:
        return jsonify({"error": "Local terrain task manager not initialized"}), 500
    try:
        limit = request.args.get("limit", 100, type=int)
        tasks = local_terrain_task_manager.list_tasks(limit=limit)
        return jsonify({"success": True, "tasks": tasks, "count": len(tasks)})
    except Exception as e:
        logger.error(f"Error listing local terrain tasks: {e}")
        return jsonify({"error": "Failed to list local terrain tasks"}), 500


@local_terrain_api_bp.route("/tasks/<int:task_id>", methods=["GET"])
def get_local_terrain_task(task_id: int):
    if not local_terrain_task_manager:
        return jsonify({"error": "Local terrain task manager not initialized"}), 500
    try:
        task = local_terrain_task_manager.get_task(task_id)
        files = local_terrain_task_manager.list_files(task_id)
        layer_url = f"{request.host_url.rstrip('/')}/terrain/local/{task_id}/layer.json"
        return jsonify({"success": True, "task": task, "files": files, "layer_url": layer_url})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error getting local terrain task {task_id}: {e}")
        return jsonify({"error": "Failed to get local terrain task"}), 500


@local_terrain_api_bp.route("/tasks/<int:task_id>/cancel", methods=["POST"])
def cancel_local_terrain_task(task_id: int):
    if not local_terrain_task_manager:
        return jsonify({"error": "Local terrain task manager not initialized"}), 500
    try:
        local_terrain_task_manager.cancel_task(task_id)
        return jsonify({"success": True, "message": f"Local terrain task {task_id} cancelled"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error cancelling local terrain task {task_id}: {e}")
        return jsonify({"error": "Failed to cancel local terrain task"}), 500


@local_terrain_api_bp.route("/tasks/<int:task_id>", methods=["DELETE"])
def delete_local_terrain_task(task_id: int):
    if not local_terrain_task_manager:
        return jsonify({"error": "Local terrain task manager not initialized"}), 500
    try:
        local_terrain_task_manager.delete_task(task_id)
        return jsonify({"success": True, "message": f"Local terrain task {task_id} deleted"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error deleting local terrain task {task_id}: {e}")
        return jsonify({"error": "Failed to delete local terrain task"}), 500
