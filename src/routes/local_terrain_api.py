"""
Local Terrain API routes

Endpoints for uploading GeoTIFF files and tiling them into Cesium terrain.
"""

import logging
import os
from pathlib import Path

from flask import Blueprint, jsonify, request

from src.core.config import Config
from src.services.geo_validation import (coerce_maxzoom, coerce_vertex_normals,
                                         validate_tiling_quality)
from src.services.task_cleanup import record_retained_output
from src.routes.api import _delete_payload
from src.routes import terrain_static

logger = logging.getLogger(__name__)

local_terrain_api_bp = Blueprint("local_terrain_api", __name__, url_prefix="/api/terrain/local")

local_terrain_task_manager = None


def init_local_terrain_task_manager(tm):
    global local_terrain_task_manager
    local_terrain_task_manager = tm
    logger.debug("Local terrain task manager initialized in local terrain API routes")


@local_terrain_api_bp.route("/tasks", methods=["POST"])
def create_local_terrain_task():
    if not local_terrain_task_manager:
        return jsonify({"error": "Local terrain task manager not initialized"}), 500
    try:
        name = request.form.get("name") or "Local Terrain Task"
        # 三态：'auto'（按源分辨率现算）/ 0–21 / 未传（空串 → None → 配置默认）。
        # coerce_maxzoom 抛带字段名的 ValueError -> 400。
        maxzoom = coerce_maxzoom(request.form.get("maxzoom"), "maxzoom")
        quality_raw = request.form.get("quality")
        # 档位跟 maxzoom 同形：路由先挡一道（管理器的 create_task_with_files
        # 里那次 validate_tiling_quality 还会再校验一次），非法值在任何
        # 文件落盘之前就 400。
        quality = (validate_tiling_quality(quality_raw)
                   if quality_raw not in (None, "") else None)
        # 法线没有第二道网：管理器只做 bool()（create_task_with_files 里的
        # `vertex_normals = bool(vertex_normals)`），而
        # bool('false') is True。这里的 coerce_vertex_normals 是唯一把关点，
        # 它同时区分「未传（None/空串）走配置默认」与「显式 false 是用户关掉」。
        vertex_normals = coerce_vertex_normals(request.form.get("vertex_normals"))

        uploads = request.files.getlist("files")

        # dem_task_id：零拷贝复用某个已完成 DEM 下载任务的目录当源（任务行上的
        # 「处理」按钮），与 contour_api 同一约定 —— 与 files 互斥。
        dem_task_id_raw = (request.form.get("dem_task_id") or "").strip()
        if dem_task_id_raw and uploads:
            return jsonify({"error": "Provide either files or dem_task_id, not both"}), 400
        if dem_task_id_raw:
            try:
                dem_task_id = int(dem_task_id_raw)
            except ValueError:
                return jsonify({"error": "Invalid dem_task_id"}), 400
            task_id = local_terrain_task_manager.create_task_from_dem_task(
                name=name, dem_task_id=dem_task_id, maxzoom=maxzoom,
                quality=quality, vertex_normals=vertex_normals,
            )
            return jsonify({"success": True, "task_id": task_id}), 201

        # Validate cheaply BEFORE touching the payload: cap the file count and
        # reject non-tif extensions up front. The total request size is already
        # capped by Config.MAX_CONTENT_LENGTH (Flask aborts oversized bodies
        # with 413 before this handler runs). The manager re-validates.
        if not uploads:
            return jsonify({"error": "No files uploaded"}), 400
        if len(uploads) > 100:
            return jsonify({"error": "Too many files (max 100 per task)"}), 400
        allowed_ext = (".tif", ".tiff")
        for f in uploads:
            ext = os.path.splitext(f.filename or "")[1].lower()
            if ext not in allowed_ext:
                return jsonify({"error": f"Unsupported file type: {f.filename} (only .tif/.tiff)"}), 400

        # FileStorage 流直传 manager：分块写盘，不再把全部上传一次性读进内存
        # （此前 f.read() 全量物化，单请求峰值内存可能拖垮本机，M5）。
        files = [(f.filename, f.stream) for f in uploads]

        task_id = local_terrain_task_manager.create_task_with_files(
            name=name, files=files, maxzoom=maxzoom,
            quality=quality, vertex_normals=vertex_normals
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
        # ?status=active 是契约特殊值（同 /api/tasks、/api/history_all）：只回
        # 活动三态；不传 status 时行为完全不变。
        status = request.args.get("status", None, type=str)
        tasks = local_terrain_task_manager.list_tasks(limit=limit, status=status)
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


@local_terrain_api_bp.route("/tasks/<int:task_id>", methods=["DELETE"])
def delete_local_terrain_task(task_id: int):
    """删除本地地形任务。

    正在切片的任务**可以**直接删：删除会自己置停止标志、当场删行、把产物清理
    交给后台线程（见 services/task_deletion）。响应里的 files_deferred=true
    表示产物还没删完 —— 后台在等切片线程收工。
    """
    if not local_terrain_task_manager:
        return jsonify({"error": "Local terrain task manager not initialized"}), 500
    try:
        # Local terrain historically always cleaned up on delete; keep that as
        # the default, but honor an explicit delete_files=false from the UI.
        delete_files = request.args.get("delete_files", "true").lower() in ("1", "true", "yes")
        outcome = local_terrain_task_manager.delete_task(
            task_id,
            delete_files=delete_files,
            # 行删掉后同步清 /terrain/local 静态路由的存在性缓存，否则
            # delete_files=false（磁盘瓦片保留）时已删任务的瓦片仍能被访问到。
            # hook 留在路由层：它走 current_app.extensions，只在请求上下文里有效。
            on_row_gone=lambda: terrain_static.invalidate_known_task(task_id),
        )
        if not outcome.row_deleted:
            return jsonify({"error": f"Local terrain task {task_id} not found"}), 404
        payload = _delete_payload(
            f"Local terrain task {task_id} deleted", outcome.files_removed,
            files_deferred=outcome.files_deferred)

        # delete_files=false 时行一走，产物目录就没有任何 DB 引用了 —— 启动清扫
        # 只认 pending_deletions 和任务表，从此谁都找不回它。登记一行把引用接
        # 回来；【只登记，不删文件】。
        # 路径按固定布局从当前 DOWNLOADS_DIR 重算，与 manager.delete_task 和
        # terrain_static 同一套口径（库存 output_path 在 exe 搬迁后指向旧位置）。
        if not delete_files:
            task_dir = (
                Path(Config.DOWNLOADS_DIR) / "terrain" / f"local_task_{task_id}")
            try:
                if task_dir.exists():
                    record_retained_output(task_dir)
                    payload["files_retained_path"] = str(task_dir)
            except OSError as e:
                logger.warning(
                    f"Local terrain task {task_id}: cannot stat retained dir: {e}")

        return jsonify(payload)
    except Exception as e:
        logger.error(f"Error deleting local terrain task {task_id}: {e}")
        return jsonify({"error": "Failed to delete local terrain task"}), 500
