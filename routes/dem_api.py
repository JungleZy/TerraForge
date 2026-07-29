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
        tasks = dem_task_manager.list_tasks(limit=limit)
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

        # Prevent deletion of running tasks
        task = dem_task_manager.get_task(task_id)
        if task.get("status") == "running":
            return jsonify({"error": "Cannot delete running DEM task. Please pause or cancel it first."}), 400

        # Serialize with start_task via the manager lock: a paused task can be
        # flipped back to running (thread spawned) while we hold no lock, so the
        # check + delete must happen under the same lock start_task uses.
        with dem_task_manager._state_lock:
            active_thread = dem_task_manager.active_tasks.get(task_id)
            if active_thread and active_thread.is_alive():
                return jsonify({"error": "Cannot delete running DEM task. Please pause or cancel it first."}), 400

            from database import get_connection
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM dem_tasks WHERE id = ?", (task_id,))
                if cur.rowcount == 0:
                    return jsonify({"error": f"DEM task {task_id} not found"}), 404
                conn.commit()
            finally:
                conn.close()

        # Optional best-effort artifact cleanup (downloads/dem/dem_task_<id>/)
        # after the row is gone; only removed when inside DOWNLOADS_DIR.
        if request.args.get("delete_files", "").lower() in ("1", "true", "yes"):
            remove_task_dir_if_safe(Path(task["output_path"]) / f"dem_task_{task_id}")

        return jsonify({"success": True, "message": f"DEM task {task_id} deleted"})

    except ValueError as e:
        return jsonify({"error": str(e)}), 404
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
