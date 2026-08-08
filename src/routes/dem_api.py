"""
DEM API routes

Endpoints for creating and running DEM download tasks (e.g., ASTGTM.003).
"""

import logging
from pathlib import Path
from flask import Blueprint, jsonify, request

from src.services.task_cleanup import resolve_stored_output_dir
from src.routes.api import _delete_payload
from src.routes import terrain_static

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
    """删除 DEM 任务。

    正在下载/切片的任务**可以**直接删：删除会自己置停止标志、当场删行、把产物
    清理交给后台线程（见 services/task_deletion）。响应里的 files_deferred=true
    表示产物还没删完 —— 后台在等工作线程收工。
    """
    try:
        if not dem_task_manager:
            return jsonify({"error": "DEM task manager not initialized"}), 500

        task = dem_task_manager.get_task(task_id)

        # Optional best-effort artifact cleanup (<output_path>/dem_task_<id>/)。
        # 边界见 services/task_cleanup.remove_task_dir_if_safe 的 docstring
        # （0.2.4 起不再要求落在 DOWNLOADS_DIR 内）。
        # M10：路径必须走 resolve_stored_output_dir —— 裸 Path() 对存量相对值
        # 按【进程 CWD】解析，打包 exe 从快捷方式启动时删的是另一个目录（且照回
        # 200 success）。
        artifact_dir = None
        if request.args.get("delete_files", "").lower() in ("1", "true", "yes"):
            artifact_dir = (
                resolve_stored_output_dir(task["output_path"]) / f"dem_task_{task_id}")

        outcome = dem_task_manager.delete_task(
            task_id,
            artifact_dir=artifact_dir,
            # 行删掉后同步清 /terrain/dem 静态路由的 output_path 缓存，否则
            # delete_files=false（磁盘切片保留）时已删任务的瓦片仍能被访问到。
            # hook 留在路由层：它走 current_app.extensions，只在请求上下文里有效。
            on_row_gone=lambda: terrain_static.invalidate_dem_task(task_id),
        )
        if not outcome.row_deleted:
            return jsonify({"error": f"DEM task {task_id} not found"}), 404

        return jsonify(_delete_payload(
            f"DEM task {task_id} deleted", outcome.files_removed,
            files_deferred=outcome.files_deferred))

    except ValueError as e:
        # get_task 的 "not found" -> 404。运行中不再拒删，这里没有 400 分支了。
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
