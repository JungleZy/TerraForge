"""
Contour API routes — 从本地 DEM 渲染等高线瓦片的任务。

数据处理不下载：远程 DEM 的下载统一在「数据下载」的 DEM 任务里
（/api/dem/tasks）。等高线任务的源有两种、二者互斥：用户上传的 .tif/.tiff，
或 dem_task_id 指向的某个已完成 DEM 下载任务的目录（零拷贝复用它已经下好的
tif，不必再上传一遍）。
"""

import logging
import os
from functools import lru_cache
from pathlib import Path
from flask import Blueprint, current_app, jsonify, request

from src.i18n import t
from src.services.config_manager import ConfigManager
from src.services.geo_validation import coerce_number, validate_zoom
from src.services.task_cleanup import resolve_stored_output_dir
from src.routes.api import _delete_payload
from src.routes import contour_static

logger = logging.getLogger(__name__)


@lru_cache(maxsize=32)
def _render_style_preview_cached(style, interval, shade):
    """style_preview 的服务端缓存：输出 PNG 对 (style, interval, shade) 是纯函数，
    而前端随取色器实时刷新会高频打这个端点，matplotlib 渲染是最贵的一步。
    ContourStyle 是 frozen dataclass（字段全为 str/float/int/tuple），可直接作
    缓存键；配置改动会体现为不同的 style 值，旧条目自然淘汰，无需手动失效。"""
    from src.services.contour_engine import render_style_preview
    return render_style_preview(style, interval, shade=shade)

contour_api_bp = Blueprint("contour_api", __name__, url_prefix="/api/contour")

contour_task_manager = None


def init_contour_task_manager(tm):
    global contour_task_manager
    contour_task_manager = tm
    logger.debug("Contour task manager initialized in contour API routes")


@contour_api_bp.route("/style_preview", methods=["GET"])
def contour_style_preview():
    """配色样式预览 PNG：合成 DEM + 当前配色参数走渲染管线出图。
    全部参数可选，缺省即现行默认方案（与 style_for_task 同一套回退）。"""
    try:
        from src.services.contour_task_manager import style_for_task

        config = contour_task_manager.config if contour_task_manager else ConfigManager()
        background = request.args.get("background") or "#FAF6EC"
        shade = str(request.args.get("terrain_shade", "1")).strip().lower() in ("1", "true", "yes", "on")
        interval_raw = request.args.get("interval") or config.get("contour_default_interval", "50")
        interval = coerce_number(interval_raw, "interval")  # 拒绝 NaN/inf/非数字
        if interval <= 0:
            raise ValueError(f"interval must be > 0, got {interval}")

        task = {
            "background": background,
            "line_color_intermediate": request.args.get("line_color_intermediate", ""),
            "line_color_index": request.args.get("line_color_index", ""),
            "tint_breaks": request.args.get("tint_breaks", ""),
            "tint_colors": request.args.get("tint_colors", ""),
        }
        style = style_for_task(config, task)
        png = _render_style_preview_cached(style, interval, shade)
        resp = current_app.response_class(png, mimetype="image/png")
        resp.headers["Cache-Control"] = "no-cache"
        return resp
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error rendering contour style preview: {e}")
        return jsonify({"error": "Failed to render style preview"}), 500


@contour_api_bp.route("/tasks", methods=["POST"])
def create_contour_task():
    if not contour_task_manager:
        return jsonify({"error": "Contour task manager not initialized"}), 500
    try:
        name = request.form.get("name") or t('api.contour.default_task_name')
        zoom_min_raw = request.form.get("zoom_min")
        zoom_max_raw = request.form.get("zoom_max")
        # zoom_max 留空 = 按 DEM 分辨率自动计算；显式填写时校验 0-21，
        # 允许设得比自动值更高
        zoom_min = validate_zoom(zoom_min_raw, "zoom_min") if zoom_min_raw not in (None, "") else 10
        zoom_max = validate_zoom(zoom_max_raw, "zoom_max") if zoom_max_raw not in (None, "") else None

        # dem_task_id：零拷贝复用某个已完成 DEM 下载任务的目录当源，不必再传
        # 一遍文件。与 files 互斥 —— 两个都给说明前端状态错乱，宁可报错。
        dem_task_id_raw = (request.form.get("dem_task_id") or "").strip()
        uploads = request.files.getlist("files")
        if dem_task_id_raw and uploads:
            return jsonify({"error": "Provide either files or dem_task_id, not both"}), 400
        if dem_task_id_raw:
            try:
                dem_task_id = int(dem_task_id_raw)
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid dem_task_id"}), 400
            task_id = contour_task_manager.create_task_from_dem_task(
                name=name,
                dem_task_id=dem_task_id,
                contour_interval=request.form.get("contour_interval"),
                zoom_min=zoom_min,
                zoom_max=zoom_max,
                background=request.form.get("background"),
                terrain_shade=request.form.get("terrain_shade", "1"),
                line_color_intermediate=request.form.get("line_color_intermediate"),
                line_color_index=request.form.get("line_color_index"),
                tint_breaks=request.form.get("tint_breaks"),
                tint_colors=request.form.get("tint_colors"),
            )
            return jsonify({"success": True, "task_id": task_id}), 201

        # 与 local_terrain_api 同款的廉价前置校验：先卡数量和扩展名，
        # 请求体总尺寸由 Config.MAX_CONTENT_LENGTH 在前面挡（413）。
        if not uploads:
            return jsonify({"error": "No files uploaded"}), 400
        if len(uploads) > 100:
            return jsonify({"error": "Too many files (max 100 per task)"}), 400
        allowed_ext = (".tif", ".tiff")
        for f in uploads:
            ext = os.path.splitext(f.filename or "")[1].lower()
            if ext not in allowed_ext:
                return jsonify({"error": f"Unsupported file type: {f.filename} (only .tif/.tiff)"}), 400

        # 流式落盘：FileStorage 直接交给 manager（save() 分块拷贝写盘），
        # 不再 f.read() 把最多 100 个文件一次性读进内存。
        task_id = contour_task_manager.create_task_with_files(
            name=name,
            files=uploads,
            contour_interval=request.form.get("contour_interval"),
            zoom_min=zoom_min,
            zoom_max=zoom_max,
            background=request.form.get("background"),
            terrain_shade=request.form.get("terrain_shade", "1"),
            line_color_intermediate=request.form.get("line_color_intermediate"),
            line_color_index=request.form.get("line_color_index"),
            tint_breaks=request.form.get("tint_breaks"),
            tint_colors=request.form.get("tint_colors"),
        )
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
        # ?status=active 是契约特殊值（同 /api/tasks、/api/history_all）：只回
        # 活动三态；不传 status 时行为完全不变。
        status = request.args.get("status", None, type=str)
        tasks = contour_task_manager.list_tasks(limit=limit, status=status)
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
    """删除等高线任务。

    正在跑的任务**可以**直接删：删除会自己置停止标志、当场删行、把产物清理交给
    后台线程（见 services/task_deletion）。响应里的 files_deferred=true 表示产物
    还没删完 —— 后台在等工作线程收工。
    """
    try:
        if not contour_task_manager:
            return jsonify({"error": "Contour task manager not initialized"}), 500
        task = contour_task_manager.get_task(task_id)

        # Optional best-effort artifact cleanup (output_path/contour_task_<id>/)。
        # 边界见 remove_task_dir_if_safe 的 docstring。M10：走
        # resolve_stored_output_dir（裸 Path() 对存量相对值按进程 CWD 解析）。
        artifact_dir = None
        if (request.args.get("delete_files", "").lower() in ("1", "true", "yes")
                and task.get("output_path")):
            artifact_dir = (
                resolve_stored_output_dir(task["output_path"]) / f"contour_task_{task_id}")

        outcome = contour_task_manager.delete_task(
            task_id,
            artifact_dir=artifact_dir,
            # 行删掉后同步清 /contour 静态路由的存在性缓存，否则 delete_files=false
            # （磁盘瓦片保留）时已删任务的瓦片仍能被访问到。
            # hook 留在路由层：它走 current_app.extensions，只在请求上下文里有效。
            on_row_gone=lambda: contour_static.invalidate_known_task(task_id),
        )
        if not outcome.row_deleted:
            return jsonify({"error": f"Contour task {task_id} not found"}), 404

        return jsonify(_delete_payload(
            f"Contour task {task_id} deleted", outcome.files_removed,
            files_deferred=outcome.files_deferred))
    except ValueError as e:
        # get_task 的 "not found" -> 404。运行中不再拒删，这里没有 400 分支了。
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
