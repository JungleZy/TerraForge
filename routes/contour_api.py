"""
Contour API routes — create/run contour tile download tasks.
"""

import logging
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

contour_api_bp = Blueprint("contour_api", __name__, url_prefix="/api/contour")

contour_task_manager = None


def init_contour_task_manager(tm):
    global contour_task_manager
    contour_task_manager = tm
    logger.info("Contour task manager initialized in contour API routes")


@contour_api_bp.route("/tasks", methods=["POST"])
def create_contour_task():
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        data = request.get_json() or {}
        required = ["name", "north", "south", "east", "west", "contour_interval", "zoom_min", "zoom_max"]
        missing = [k for k in required if k not in data]
        if missing:
            return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400
        task_id = contour_task_manager.create_task(data)
        return jsonify({"success": True, "task_id": task_id}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error creating contour task: {e}")
        return jsonify({"error": "Failed to create contour task"}), 500


@contour_api_bp.route("/tasks", methods=["GET"])
def list_contour_tasks():
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        limit = request.args.get("limit", 100, type=int)
        tasks = contour_task_manager.list_tasks(limit=limit)
        return jsonify({"success": True, "tasks": tasks, "count": len(tasks)})
    except Exception as e:
        logger.error(f"Error listing contour tasks: {e}")
        return jsonify({"error": "Failed to list contour tasks"}), 500


@contour_api_bp.route("/tasks/<int:task_id>", methods=["GET"])
def get_contour_task(task_id: int):
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        task = contour_task_manager.get_task(task_id)
        return jsonify({"success": True, "task": task})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error getting contour task {task_id}: {e}")
        return jsonify({"error": "Failed to get contour task"}), 500


@contour_api_bp.route("/tasks/<int:task_id>", methods=["DELETE"])
def delete_contour_task(task_id: int):
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        task = contour_task_manager.get_task(task_id)
        if task.get("status") == "running":
            return jsonify({"error": "Cannot delete running contour task. Pause or cancel it first."}), 400
        from database import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM contour_tasks WHERE id = ?", (task_id,))
            if cur.rowcount == 0:
                return jsonify({"error": f"Contour task {task_id} not found"}), 404
            conn.commit()
        finally:
            conn.close()
        return jsonify({"success": True, "message": f"Contour task {task_id} deleted"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error deleting contour task {task_id}: {e}")
        return jsonify({"error": "Failed to delete contour task"}), 500


@contour_api_bp.route("/tasks/<int:task_id>/start", methods=["POST"])
def start_contour_task(task_id: int):
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        contour_task_manager.start_task(task_id)
        return jsonify({"success": True, "message": f"Contour task {task_id} started"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error starting contour task {task_id}: {e}")
        return jsonify({"error": "Failed to start contour task"}), 500


@contour_api_bp.route("/tasks/<int:task_id>/pause", methods=["POST"])
def pause_contour_task(task_id: int):
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        contour_task_manager.pause_task(task_id)
        return jsonify({"success": True, "message": f"Contour task {task_id} paused"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error pausing contour task {task_id}: {e}")
        return jsonify({"error": "Failed to pause contour task"}), 500


@contour_api_bp.route("/tasks/<int:task_id>/resume", methods=["POST"])
def resume_contour_task(task_id: int):
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        contour_task_manager.resume_task(task_id)
        return jsonify({"success": True, "message": f"Contour task {task_id} resumed"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error resuming contour task {task_id}: {e}")
        return jsonify({"error": "Failed to resume contour task"}), 500


@contour_api_bp.route("/tasks/<int:task_id>/cancel", methods=["POST"])
def cancel_contour_task(task_id: int):
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        contour_task_manager.cancel_task(task_id)
        return jsonify({"success": True, "message": f"Contour task {task_id} cancelled"})
    except Exception as e:
        logger.error(f"Error cancelling contour task {task_id}: {e}")
        return jsonify({"error": "Failed to cancel contour task"}), 500
