"""
API routes

Handles RESTful API endpoints for task management, history, and configuration.
"""

import logging
import os
from dataclasses import asdict
from flask import Blueprint, Response, request, jsonify
from pathlib import Path
from src.contracts.artifact import PIPELINES, Artifact, ArtifactKind
from src.contracts.outcome import ACTIVE_STATE_VALUES, SUCCESSFUL_STATE_VALUES
from src.contracts.region import RegionSpec, RegionValidationError
from src.contracts.region_tiles import bbox_tile_range, validate_zoom_range
from src.core.config import Config
from src.core.database import get_connection, DEFAULT_CONFIGS
from src.core.tile_server import current_tile_port
from src.i18n import t
from src.services import (artifact_export, artifact_store, cache_exclusive,
                          disk_budget, geocoding, region_import, source_wizard,
                          task_logging)
from src.services.basemap_source import client_descriptor, resolve_basemap
from src.services.config_manager import (ConfigManager, is_unchanged_secret,
                                         redact_secret_value)
# 「很大」的事实来源只此一份（download_engine 的软阈值），预检的硬上限按它
# 定倍数，不在路由层另立一个数。
from src.services.download_engine import WARN_TILES_THRESHOLD
from src.services.raster_probe import (
    MAX_INSPECT_BODY, InspectError, describe_headers,
)
from src.services.resource_scheduler import get_scheduler
from src.services.source_registry import style_code_for
from src.services.task_cleanup import (purge_registered_artifacts,
                                       record_retained_output,
                                       resolve_stored_output_dir)
from src.routes.basemap_static import active_basemap
from src.routes import mbtiles_static, tiles_static

logger = logging.getLogger(__name__)

# Create API blueprint with /api prefix
api_bp = Blueprint('api', __name__, url_prefix='/api')

# Global task manager instance (injected via init_task_manager)
task_manager = None

# Initialize config manager
config_manager = ConfigManager()

# `?status=` 里代表**一组**状态的筛选值。其余取值走单值等值（前端可以拿
# 'failed' / 'completed_with_gaps' / 'pending_decision' 这类精确状态直接做 chip）。
#
# · active    —— 「进行中」。这里曾经手写 ('pending','running','paused')，而缺块
#   改造新增了 retrying / pending_decision 两个同样「还没完」的状态；手写名单漏掉
#   它们的表现是：一个正在补漏的任务从「进行中」筛选里凭空消失，用户以为它结束了。
# · completed —— 「完成」。**必须同时含 completed_with_gaps**：§13-3 允许用户
#   「接受缺块、导出部分成果」，那条产品决定做出来的成品，状态就叫
#   completed_with_gaps。写 `status = 'completed'` 的实测后果是它一个 chip 都不匹配
#   ——「完成」里找不到、`history_stats.completed` 少算一个 —— 用户找不到自己的成品，
#   等于那条产品决定白做。
#
# 两份名单都取自 contracts.outcome，不在路由层抄第二份：状态机加一个状态时，
# 唯一需要改的地方是那个枚举。
_HISTORY_STATUS_GROUPS = {
    'active': ACTIVE_STATE_VALUES,
    'completed': SUCCESSFUL_STATE_VALUES,
}


def init_task_manager(tm):
    """
    Initialize the global task_manager instance

    Args:
        tm: TaskManager instance to use for API operations
    """
    global task_manager
    task_manager = tm
    logger.debug("Task manager initialized in API routes")


@api_bp.route('/tasks', methods=['POST'])
def create_task():
    """
    Create a new download task

    Request Body:
        {
            "name": "Task name",
            "north": 40.0,
            "south": 39.0,
            "east": 117.0,
            "west": 116.0,
            "zoom_min": 10,
            "zoom_max": 15,
            "style": "roadmap",
            "output_format": "png",
            "output_path": "./downloads",
            "export_mbtiles": false,
            "region": {...}
        }

    `export_mbtiles` 与 `output_format` **正交**，不是它的第四个取值：MBTiles 是
    从松散 XYZ 目录打包出来的追加产物（§5.3），做成 output_format 的一个值就会
    连原料和 /tiles 预览一起砍掉。这里不校验、不解释，整个 body 原样交给
    TaskManager.create_task —— 校验规则住在那里，路由层再抄一份就是第二处事实。

    Returns:
        JSON response with task_id and success status
    """
    try:
        if not task_manager:
            return jsonify({'error': 'Task manager not initialized'}), 500

        # Get request data(silent=True:解析失败/空 body 返回 None,统一按 400 处理;
        # 非对象 JSON(数组/字符串/数字)直接拒绝,避免 "in" 判断抛 TypeError 变 500)
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'Request body must be a JSON object'}), 400

        # Validate required fields
        required_fields = ['name', 'north', 'south', 'east', 'west',
                          'zoom_min', 'zoom_max', 'style', 'output_format', 'output_path']

        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            return jsonify({
                'error': f'Missing required fields: {", ".join(missing_fields)}'
            }), 400

        # name 必须是字符串:list/dict 会一路带到 sqlite 绑定参数抛
        # InterfaceError,经通用 except 变成 500 —— 在入口处拦成 400。
        if not isinstance(data['name'], str):
            return jsonify({'error': 'name must be a string'}), 400

        # 插件源:下载弹窗选了插件提供的数据源时,由注册表在**建任务这一刻**
        # 冻结一张快照随任务落库,create_task 的通用覆盖缝会认它。核心不认
        # 具体数据源,只认这份快照合同 —— 插件只是第一批调用方。
        # 快照里的 credential_reference 是键名不是值,凭据不进任务行。
        #
        # **合同不能由客户端签**:`source_snapshot` 无条件从 body 里摘掉,
        # 只有 `build_source_snapshot` 的产出能落库。少了这一句,一个不带
        # `source_plugin_id` 的建任务请求就能自带一张 url_template 指向攻击者
        # 主机、credential_reference 指向 `plugin:tianditu:token` 的快照 ——
        # 宿主会拿着用户的真 token 逐块瓦片请求那台服务器(`{credential}`
        # 的替换发生在 download_engine.get_tile_url,它只看快照怎么写)。
        # pop 排在读 source_plugin_id 之后、写回之前:两条路径共用一个键。
        source_plugin_id = str(data.get('source_plugin_id') or '')
        source_id = str(data.get('source_id') or '')
        data.pop('source_snapshot', None)
        if source_plugin_id and source_id:
            from src.plugins import registry as plugin_registry
            try:
                snapshot = plugin_registry.build_source_snapshot(
                    source_plugin_id, source_id)
            except KeyError as e:
                return jsonify({'error': str(e)}), 400
            data['source_snapshot'] = snapshot.to_json()

        # Create task
        task_id = task_manager.create_task(data)

        logger.info(f"Task {task_id} created via API")

        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': 'Task created successfully'
        }), 201

    except ValueError as e:
        logger.error(f"Validation error creating task: {e}")
        return jsonify({'error': str(e)}), 400

    except Exception as e:
        logger.error(f"Error creating task: {e}")
        return jsonify({'error': 'Failed to create task'}), 500


@api_bp.route('/tasks', methods=['GET'])
def get_tasks():
    """
    Get all tasks with optional limit

    Query Parameters:
        limit: Maximum number of tasks to return (default: 100)

    Returns:
        JSON response with list of tasks
    """
    try:
        # Get limit from query parameters
        limit = request.args.get('limit', 100, type=int)

        # Clamp limit into [1, 100] — a negative limit means "no limit" in
        # SQLite and 0 returns nothing; both are caller bugs, answer with the
        # default window instead (same convention as /history's per_page).
        if limit > 100:
            limit = 100
        if limit < 1:
            limit = 100

        # ?status=active 是契约特殊值（与 /api/history_all 对齐）：只回活动三态
        # (pending/running/paused)，直接复用 TaskManager.get_active_tasks 的现成
        # 查询。不传 status 时行为完全不变；其它取值本接口不做过滤（契约只有
        # active 这一个特殊值）。
        if request.args.get('status') == 'active':
            if not task_manager:
                return jsonify({'error': 'Task manager not initialized'}), 500
            tasks = task_manager.get_active_tasks()
            return jsonify({
                'success': True,
                'tasks': tasks,
                'count': len(tasks)
            })

        conn = get_connection()
        try:
            cursor = conn.cursor()

            cursor.execute('''
                SELECT * FROM tasks
                ORDER BY created_at DESC
                LIMIT ?
            ''', (limit,))

            rows = cursor.fetchall()

            tasks = []
            for row in rows:
                task_dict = dict(row)
                tasks.append(task_dict)

            return jsonify({
                'success': True,
                'tasks': tasks,
                'count': len(tasks)
            })

        finally:
            conn.close()

    except Exception as e:
        logger.error(f"Error getting tasks: {e}")
        return jsonify({'error': 'Failed to get tasks'}), 500


@api_bp.route('/tasks/<int:task_id>', methods=['GET'])
def get_task(task_id: int):
    """
    Get task details by ID

    Args:
        task_id: Task ID to retrieve

    Returns:
        JSON response with task details
    """
    try:
        if not task_manager:
            return jsonify({'error': 'Task manager not initialized'}), 500

        task = task_manager.get_task_status(task_id)

        return jsonify({
            'success': True,
            'task': task
        })

    except ValueError as e:
        logger.error(f"Task {task_id} not found: {e}")
        return jsonify({'error': str(e)}), 404

    except Exception as e:
        logger.error(f"Error getting task {task_id}: {e}")
        return jsonify({'error': 'Failed to get task'}), 500


@api_bp.route('/tasks/<int:task_id>/start', methods=['POST'])
def start_task(task_id: int):
    """
    Start a task

    Args:
        task_id: Task ID to start

    Returns:
        JSON response with success status
    """
    try:
        if not task_manager:
            return jsonify({'error': 'Task manager not initialized'}), 500

        task_manager.start_task(task_id)

        logger.info(f"Task {task_id} started via API")

        return jsonify({
            'success': True,
            'message': f'Task {task_id} started'
        })

    except ValueError as e:
        logger.error(f"Error starting task {task_id}: {e}")
        return jsonify({'error': str(e)}), 400

    except Exception as e:
        logger.error(f"Error starting task {task_id}: {e}")
        return jsonify({'error': 'Failed to start task'}), 500


@api_bp.route('/tasks/<int:task_id>/pause', methods=['POST'])
def pause_task(task_id: int):
    """
    Pause a running task

    Args:
        task_id: Task ID to pause

    Returns:
        JSON response with success status
    """
    try:
        if not task_manager:
            return jsonify({'error': 'Task manager not initialized'}), 500

        task_manager.pause_task(task_id)

        logger.info(f"Task {task_id} paused via API")

        return jsonify({
            'success': True,
            'message': f'Task {task_id} paused'
        })

    except ValueError as e:
        logger.error(f"Error pausing task {task_id}: {e}")
        return jsonify({'error': str(e)}), 400

    except Exception as e:
        logger.error(f"Error pausing task {task_id}: {e}")
        return jsonify({'error': 'Failed to pause task'}), 500


@api_bp.route('/tasks/<int:task_id>/resume', methods=['POST'])
def resume_task(task_id: int):
    """
    Resume a paused task

    Args:
        task_id: Task ID to resume

    Returns:
        JSON response with success status
    """
    try:
        if not task_manager:
            return jsonify({'error': 'Task manager not initialized'}), 500

        task_manager.resume_task(task_id)

        logger.info(f"Task {task_id} resumed via API")

        return jsonify({
            'success': True,
            'message': f'Task {task_id} resumed'
        })

    except ValueError as e:
        logger.error(f"Error resuming task {task_id}: {e}")
        return jsonify({'error': str(e)}), 400

    except Exception as e:
        logger.error(f"Error resuming task {task_id}: {e}")
        return jsonify({'error': 'Failed to resume task'}), 500


@api_bp.route('/tasks/<int:task_id>', methods=['DELETE'])
def delete_task(task_id: int):
    """
    Delete a task and its associated tiles

    Query Parameters:
        delete_files: Optional (1/true/yes). Also remove the task's on-disk
            artifact directory (output_path/task_<id>). Defaults to false.
            删除边界见 services/task_cleanup.remove_task_dir_if_safe。
        clear_cache: Optional (1/true/yes). 顺带删掉**只被这个任务引用**的共享
            瓦片缓存（`cache/<namespace>/z/x/y.png`）。默认 false。
            与 delete_files 是两个独立的勾，因为它们删的是两样东西：产物目录是
            这个任务的成果，共享缓存是所有同源任务共用的下载中间层。被别的任务
            也覆盖到的瓦片一块都不动（独占集算法见 services/cache_exclusive），
            误差方向恒为「多留」——最坏结果是下次同区域下载 cache miss 后重下。

    正在运行的任务**可以**直接删：删除会自己置停止标志、当场删行、把产物清理
    交给后台线程（见 services/task_deletion）。响应里的 files_deferred=true
    表示产物还没删完 —— 后台在等工作线程收工；cache_deferred=true 同理。
    """
    try:
        if not task_manager:
            return jsonify({'error': 'Task manager not initialized'}), 500

        conn = get_connection()
        try:
            row = conn.execute(
                'SELECT id, status, output_path FROM tasks WHERE id = ?',
                (task_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return jsonify({'error': f'Task {task_id} not found'}), 404

        delete_files = request.args.get('delete_files', '').lower() in ('1', 'true', 'yes')
        clear_cache = request.args.get('clear_cache', '').lower() in ('1', 'true', 'yes')
        # 存量行的 output_path 可能是相对路径(旧版本只校验不改写)——先归一化
        # 成绝对路径;否则 Path.resolve() 按进程 CWD 解析,CWD≠BASE_DIR 时会
        # 误判成"越界"而拒删,接口却已经返回 success。
        # 两条路都要算:要删的那条交给 manager,保留的那条得登记(见下)。
        task_dir = None
        if row['output_path']:
            task_dir = resolve_stored_output_dir(row['output_path']) / f"task_{task_id}"

        outcome = task_manager.delete_task(
            task_id,
            artifact_dir=task_dir if delete_files else None,
            # 行删掉后同步清 /tiles 静态路由的 output_path 缓存，否则
            # delete_files=false（磁盘瓦片保留）时已删任务的瓦片仍能被访问到。
            # hook 留在路由层：它走 current_app.extensions，只在请求上下文里有效。
            # /mbtiles 那份产物缓存一并失效：导出过 MBTiles 的任务在这里也有一条
            # 缓存项，不清的话已删任务的 MBTiles 瓦片同样还能取到。
            on_row_gone=lambda: (
                tiles_static.invalidate_output_path_cache(task_id),
                mbtiles_static.invalidate_known_task(task_id),
            ),
            # 快照必须在锁内、删行前拍，清理必须在删行后做 —— 顺序是 manager 与
            # task_deletion 的事，路由层只表达用户的意图。
            clear_cache=clear_cache,
        )
        if not outcome.row_deleted:
            return jsonify({'error': f'Task {task_id} not found'}), 404

        logger.info(f"Task {task_id} deleted via API")
        payload = _delete_payload(
            f'Task {task_id} deleted', outcome.files_removed,
            files_deferred=outcome.files_deferred,
            cache_removed=((outcome.cache_removed_bytes,
                            outcome.cache_removed_files)
                           if clear_cache and not outcome.files_deferred else None),
            cache_deferred=clear_cache and outcome.files_deferred)

        # 文件也删了的那条路上，登记产物跟着走：那些行唯一的用途是「文件还在
        # 哪」，文件没了就是纯垃圾，留着会让 /mbtiles 与产物列表指向不存在的
        # 路径。**只销行是不够的** —— 导出的 MBTiles 落在 `task_<id>/` 的**同级**
        # （它是那个目录的打包结果，装进去会在下次导出时把自己打进自己），
        # rmtree 任务目录碰不到它；销了行又等于把唯一记得它的东西也删掉，几百 MB
        # 从此没人知道。purge_registered_artifacts 先删这类落在目录外的文件、
        # 再销行。反过来 delete_files=false 时【绝不能】动 —— 用户选择保留文件，
        # 产物行是它们仅剩的记录（artifacts 表刻意没有外键，就是为了这一刻能比
        # 任务行活得久，见 contracts/artifact 的模块 docstring）。
        if delete_files:
            purge_registered_artifacts('map', task_id, task_dir)

        # delete_files=false 是删除对话框的默认。行一走，<output_path>/task_<id>/
        # 就没有任何 DB 引用了 —— 启动清扫只认 pending_deletions 和任务表，从此
        # 谁都找不回它。登记一行把引用接回来；【只登记，不删文件】。
        if not delete_files and task_dir is not None:
            try:
                if task_dir.exists():
                    record_retained_output(task_dir)
                    payload['files_retained_path'] = str(task_dir)
            except OSError as e:
                logger.warning(
                    f"Task {task_id}: cannot stat retained output dir: {e}")

        return jsonify(payload)

    except Exception as e:
        logger.error(f"Error deleting task {task_id}: {e}")
        return jsonify({'error': 'Failed to delete task'}), 500


@api_bp.route('/history', methods=['GET'])
def get_history():
    """
    Get task history with pagination

    Query Parameters:
        page: Page number (default: 1)
        per_page: Items per page (default: 20, max: 100)
        status: Filter by status (optional)

    Returns:
        JSON response with paginated task history
    """
    try:
        # Get pagination parameters
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        status_filter = request.args.get('status', None, type=str)

        # Validate and cap per_page
        if per_page > 100:
            per_page = 100
        if per_page < 1:
            per_page = 20
        if page < 1:
            page = 1

        # Calculate offset
        offset = (page - 1) * per_page

        conn = get_connection()
        try:
            cursor = conn.cursor()

            # Build query with optional status filter
            if status_filter:
                count_query = 'SELECT COUNT(*) as count FROM tasks WHERE status = ?'
                data_query = '''
                    SELECT * FROM tasks
                    WHERE status = ?
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                '''
                cursor.execute(count_query, (status_filter,))
                total_count = cursor.fetchone()['count']

                cursor.execute(data_query, (status_filter, per_page, offset))
            else:
                count_query = 'SELECT COUNT(*) as count FROM tasks'
                data_query = '''
                    SELECT * FROM tasks
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                '''
                cursor.execute(count_query)
                total_count = cursor.fetchone()['count']

                cursor.execute(data_query, (per_page, offset))

            rows = cursor.fetchall()

            tasks = []
            for row in rows:
                task_dict = dict(row)
                tasks.append(task_dict)

            # Calculate total pages
            total_pages = (total_count + per_page - 1) // per_page

            return jsonify({
                'success': True,
                'tasks': tasks,
                'pagination': {
                    'page': page,
                    'per_page': per_page,
                    'total_count': total_count,
                    'total_pages': total_pages
                }
            })

        finally:
            conn.close()

    except Exception as e:
        logger.error(f"Error getting history: {e}")
        return jsonify({'error': 'Failed to get history'}), 500


@api_bp.route('/history_all', methods=['GET'])
def get_history_all():
    """
    Get combined history for all five task tables with pagination.

    Returns a normalized task list with task_type in
    {'map','dem','local_terrain','contour','plugin'}, ordered strictly by
    created_at DESC (single time stream). ?status= filters by a single
    status value; the special values 'active' and 'completed' each expand to a
    set of statuses (see _HISTORY_STATUS_GROUPS).
    """
    try:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        status_filter = request.args.get('status', None, type=str)

        if per_page > 100:
            per_page = 100
        if per_page < 1:
            per_page = 20
        if page < 1:
            page = 1

        offset = (page - 1) * per_page

        # 2026-08 单一时间流定稿：?status=active 是特殊值，表示「进行中」——
        # 记录面板不再有独立的活动分区，活动任务也在时间流里，靠这个筛选值把
        # 它们单独滤出来。不在 _HISTORY_STATUS_GROUPS 里的取值维持单值等值语义
        # （前端可以拿 'failed' / 'completed_with_gaps' 这类精确状态直接做 chip）。
        group = _HISTORY_STATUS_GROUPS.get(status_filter)
        if group:
            where_sql = 'WHERE status IN (%s)' % ', '.join('?' * len(group))
            count_params = tuple(group)
        else:
            where_sql = 'WHERE status = ?' if status_filter else ''
            count_params = (status_filter,) if status_filter else ()

        conn = get_connection()
        try:
            cursor = conn.cursor()

            # 5 个相互独立的 COUNT 合并成一条标量子查询 SQL:同连接下每次
            # execute 仍是一次完整的解析+执行往返,合一后只跑一趟。
            # where_sql/count_params 五表相同,参数按出现顺序重复 5 份。
            cursor.execute(
                f'SELECT '
                f'(SELECT COUNT(*) FROM tasks {where_sql}) AS map_c, '
                f'(SELECT COUNT(*) FROM dem_tasks {where_sql}) AS dem_c, '
                f'(SELECT COUNT(*) FROM local_terrain_tasks {where_sql}) AS local_c, '
                f'(SELECT COUNT(*) FROM contour_tasks {where_sql}) AS contour_c, '
                f'(SELECT COUNT(*) FROM plugin_tasks {where_sql}) AS plugin_c',
                count_params * 5,
            )
            row = cursor.fetchone()
            map_count = row['map_c']
            dem_count = row['dem_c']
            local_count = row['local_c']
            contour_count = row['contour_c']
            plugin_count = row['plugin_c']

            total_count = (int(map_count or 0) + int(dem_count or 0)
                           + int(local_count or 0) + int(contour_count or 0)
                           + int(plugin_count or 0))

            params = []
            where_map = where_sql
            where_dem = where_sql
            where_local = where_sql
            where_contour = where_sql
            # 插件段吃**同一个** status 筛选。plugin_tasks 的 status 取值与四条
            # 核心管线同一套（TaskState），漏掉这一份的后果是 ?status=active
            # 把全部插件任务原样带出来，而分页总数是按筛选算的 —— 列表和计数
            # 当场对不上。
            where_plugin = where_sql
            if group:
                # 五张表按出现顺序各要一份占位参数，与上面 count_params * 5 同理。
                params.extend(count_params * 5)
            elif status_filter:
                params.extend([status_filter] * 5)

            query = f'''
                SELECT
                    'map' AS task_type,
                    id,
                    name,
                    status,
                    north, south, east, west,
                    zoom_min, zoom_max,
                    style,
                    downloaded_tiles AS downloaded,
                    total_tiles AS total,
                    output_format,
                    output_path,
                    created_at, started_at, completed_at,
                    error_message,
                    -- L3: 前端 calculateTimeInfo 优先用这个累计值，字段缺失时
                    -- 才回退按 started_at 算墙钟（那个分支本是给不写该列的
                    -- dem/contour/local 三条管线兜底的）。缺了它，paused 任务
                    -- 刷新页面后耗时一直错，拼接/复制阶段的 running 任务 ETA
                    -- 还会被放大。其余三张表没有该列，补 NULL 对齐 UNION。
                    total_running_seconds,
                    -- 缺块标记必须跟着**每一行**走，不能只在详情里查。
                    -- §13-3 的措辞是「成果与历史永久带缺块标记」：任务中心与
                    -- /history 用的是同一份时间流，这里漏掉这两列，
                    -- completed_with_gaps 的行就只剩一个状态文字、没有数字，
                    -- 前端的缺块角标永远渲染不出来（实测如此）。
                    -- 其余三条管线没有瓦片级结局，补 0/'' 对齐 UNION ——
                    -- 用 0 而不是 NULL：前端按真值判断是否渲染角标，
                    -- NULL 与 0 在 JS 里都是假值，但 0 明确表达「查过，没有缺块」。
                    gap_tiles,
                    gap_decision
                FROM tasks
                {where_map}
                UNION ALL
                SELECT
                    'dem' AS task_type,
                    id,
                    name,
                    status,
                    north, south, east, west,
                    NULL AS zoom_min, NULL AS zoom_max,
                    dataset AS style,
                    downloaded_files AS downloaded,
                    total_files AS total,
                    NULL AS output_format,
                    output_path,
                    created_at, started_at, completed_at,
                    error_message,
                    NULL AS total_running_seconds,
                    0 AS gap_tiles, '' AS gap_decision
                FROM dem_tasks
                {where_dem}
                UNION ALL
                SELECT
                    'local_terrain' AS task_type,
                    id,
                    name,
                    status,
                    NULL AS north, NULL AS south, NULL AS east, NULL AS west,
                    NULL AS zoom_min, NULL AS zoom_max,
                    NULL AS style,
                    uploaded_files AS downloaded,
                    total_files AS total,
                    NULL AS output_format,
                    output_path,
                    created_at, started_at, completed_at,
                    error_message,
                    NULL AS total_running_seconds,
                    0 AS gap_tiles, '' AS gap_decision
                FROM local_terrain_tasks
                {where_local}
                UNION ALL
                SELECT
                    'contour' AS task_type,
                    id,
                    name,
                    status,
                    north, south, east, west,
                    zoom_min, zoom_max,
                    'contour' AS style,
                    rendered_tiles AS downloaded,
                    total_tiles AS total,
                    NULL AS output_format,
                    output_path,
                    created_at, started_at, completed_at,
                    error_message,
                    NULL AS total_running_seconds,
                    0 AS gap_tiles, '' AS gap_decision
                FROM contour_tasks
                {where_contour}
                UNION ALL
                SELECT
                    'plugin' AS task_type,
                    id,
                    name,
                    status,
                    north, south, east, west,
                    zoom_min, zoom_max,
                    plugin_id AS style,
                    downloaded_items AS downloaded,
                    total_items AS total,
                    NULL AS output_format,
                    output_path,
                    created_at, started_at, completed_at,
                    error_message,
                    total_running_seconds,
                    gap_tiles, gap_decision
                FROM plugin_tasks
                {where_plugin}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
            '''

            params.extend([per_page, offset])
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            tasks = []
            for r in rows:
                d = dict(r)
                tasks.append(d)

            total_pages = (total_count + per_page - 1) // per_page

            resp = jsonify({
                'success': True,
                'tasks': tasks,
                'pagination': {
                    'page': page,
                    'per_page': per_page,
                    'total_count': total_count,
                    'total_pages': total_pages
                }
            })
            resp.headers['Cache-Control'] = 'no-store'
            return resp

        finally:
            conn.close()

    except Exception as e:
        logger.error(f"Error getting combined history: {e}")
        return jsonify({'error': 'Failed to get combined history'}), 500


@api_bp.route('/history_stats', methods=['GET'])
def get_history_stats():
    """Aggregate task counts and download totals across all five task tables."""
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()

            # 原来 4 条 COUNT + 4 条 SUM 串行执行，每次 execute 都是一次完整的
            # 解析+执行往返。合并成 1 条：5 个聚合子查询交叉连接（无 GROUP BY 的
            # 聚合在空表上也恒返回一行），每表仍只扫一遍，总共只有一次往返
            # （思路同 history_all 已合并的 5 路 COUNT）。
            # 「完成」这一格必须把 completed_with_gaps 也数进来。§13-3 允许用户
            # 「接受缺块、导出部分成果」，那种成品的状态就叫 completed_with_gaps；
            # 只数 status='completed' 的实测后果是它在统计里凭空消失 —— 而同一个
            # 任务在 /history 的「完成」筛选里是看得见的，两处给出互相矛盾的答案。
            # 名单与筛选共用 _HISTORY_STATUS_GROUPS['completed'] 的事实来源
            # （contracts.outcome.SUCCESSFUL_STATE_VALUES），不在这里抄第二份。
            done_clause = 'status IN (%s)' % ', '.join(
                '?' * len(SUCCESSFUL_STATE_VALUES))
            cursor.execute(f'''
                SELECT
                    m.t AS m_total, m.c AS m_done, m.f AS m_fail, m.s AS m_sum,
                    d.t AS d_total, d.c AS d_done, d.f AS d_fail, d.s AS d_sum,
                    l.t AS l_total, l.c AS l_done, l.f AS l_fail, l.s AS l_sum,
                    c.t AS c_total, c.c AS c_done, c.f AS c_fail, c.s AS c_sum,
                    p.t AS p_total, p.c AS p_done, p.f AS p_fail, p.s AS p_sum
                FROM
                    (SELECT COUNT(*) AS t,
                            SUM(CASE WHEN {done_clause} THEN 1 ELSE 0 END) AS c,
                            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS f,
                            COALESCE(SUM(downloaded_tiles), 0) AS s
                     FROM tasks) m,
                    (SELECT COUNT(*) AS t,
                            SUM(CASE WHEN {done_clause} THEN 1 ELSE 0 END) AS c,
                            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS f,
                            COALESCE(SUM(downloaded_files), 0) AS s
                     FROM dem_tasks) d,
                    (SELECT COUNT(*) AS t,
                            SUM(CASE WHEN {done_clause} THEN 1 ELSE 0 END) AS c,
                            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS f,
                            COALESCE(SUM(uploaded_files), 0) AS s
                     FROM local_terrain_tasks) l,
                    (SELECT COUNT(*) AS t,
                            SUM(CASE WHEN {done_clause} THEN 1 ELSE 0 END) AS c,
                            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS f,
                            COALESCE(SUM(rendered_tiles), 0) AS s
                     FROM contour_tasks) c,
                    -- 插件任务同样进统计：它们就在 history_all 的时间流里，
                    -- 漏掉这一段等于「完成」那一格与列表给出互相矛盾的答案
                    -- （completed_with_gaps 当年就是这么消失的）。
                    (SELECT COUNT(*) AS t,
                            SUM(CASE WHEN {done_clause} THEN 1 ELSE 0 END) AS c,
                            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS f,
                            COALESCE(SUM(downloaded_items), 0) AS s
                     FROM plugin_tasks) p
            ''', tuple(SUCCESSFUL_STATE_VALUES) * 5)
            row = cursor.fetchone()

            resp = jsonify({
                'success': True,
                'stats': {
                    'total_tasks': int(row['m_total'] or 0) + int(row['d_total'] or 0)
                                   + int(row['l_total'] or 0) + int(row['c_total'] or 0)
                                   + int(row['p_total'] or 0),
                    'completed': int(row['m_done'] or 0) + int(row['d_done'] or 0)
                                 + int(row['l_done'] or 0) + int(row['c_done'] or 0)
                                 + int(row['p_done'] or 0),
                    'failed': int(row['m_fail'] or 0) + int(row['d_fail'] or 0)
                              + int(row['l_fail'] or 0) + int(row['c_fail'] or 0)
                              + int(row['p_fail'] or 0),
                    'total_downloaded': int(row['m_sum'] or 0) + int(row['d_sum'] or 0)
                                        + int(row['l_sum'] or 0) + int(row['c_sum'] or 0)
                                        + int(row['p_sum'] or 0),
                }
            })
            resp.headers['Cache-Control'] = 'no-store'
            return resp
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error getting history stats: {e}")
        return jsonify({'error': 'Failed to get history stats'}), 500


@api_bp.route('/config', methods=['GET'])
def get_config():
    """
    Get all configuration settings

    密钥类键（earthdata_password）回的是「已保存，未修改」哨兵而不是真值 ——
    这个端点无鉴权,真值一旦回出去就等于交给局域网上的任意主机与任何浏览器扩展。
    PUT 收到哨兵会跳过该键,所以「读回来再原样提交」不会把密码写成哨兵字面量。
    见 src/services/config_manager.py 的 SECRET_UNCHANGED。

    Returns:
        JSON response with all configuration key-value pairs
    """
    try:
        config = {
            key: {**entry, 'value': redact_secret_value(key, entry.get('value'))}
            if isinstance(entry, dict) else redact_secret_value(key, entry)
            for key, entry in config_manager.get_all().items()
        }

        return jsonify({
            'success': True,
            'config': config
        })

    except Exception as e:
        logger.error(f"Error getting config: {e}")
        return jsonify({'error': 'Failed to get config'}), 500


@api_bp.route('/basemap', methods=['GET'])
def get_basemap():
    """独立页（/history）取底图图层描述符。

    首页由 routes/main.py 内联下发，不需要这个接口；/history 的路由不注入
    模板变量，历史小地图只能从这里拿。返回的是 client_descriptor 的结果
    —— url 永远是同源的 /basemap/{z}/{x}/{y}，真实上游地址不出服务端
    （理由见 src/routes/basemap_static.py：浏览器直连上游会撞 CORS，而且
    不吃项目的 proxy_url）。
    """
    try:
        config = config_manager.get_all()

        def _val(key, default=''):
            entry = config.get(key)
            if isinstance(entry, dict):
                return entry.get('value', default)
            return entry if entry is not None else default

        return jsonify({
            'success': True,
            'basemap': client_descriptor(active_basemap(resolve_basemap(
                _val('basemap_source'),
                tile_servers=_val('tile_servers'),
                default_style=_val('default_style', 'm') or 'm',
            )), tile_port=current_tile_port()),
        })

    except Exception as e:
        logger.error(f"Error getting basemap descriptor: {e}")
        return jsonify({'error': 'Failed to get basemap descriptor'}), 500


@api_bp.route('/config', methods=['PUT'])
def update_config():
    """
    Update configuration settings

    Request Body:
        {
            "key1": "value1",
            "key2": "value2",
            ...
        }

    Returns:
        JSON response with success status
    """
    try:
        # silent=True:解析失败/空 body 返回 None,统一按 400 处理 ——
        # 不带 silent 时 get_json 抛的 BadRequest 会被下面的通用
        # except Exception 吞成 500;非对象 JSON(数组等)同理。
        data = request.get_json(silent=True)

        if not isinstance(data, dict) or not data:
            return jsonify({'error': 'No configuration data provided'}), 400

        # 只接受 DEFAULT_CONFIGS 里已知的键 —— 否则任意拼错的键都会被
        # 静默写进 config 表,而读取侧永远读不到它,用户以为设置生效了。
        known_keys = {key for key, _ in DEFAULT_CONFIGS}

        # Update each configuration key
        updated_keys = []
        errors = []

        # 逐键校验、收集错误，合法的一次性交给 set_many 单事务写入 ——
        # 逐键 set() 各自 commit 时，中途失败会留下半更新状态（部分键已生效、
        # 部分没生效），用户难以察觉。
        valid_items = {}
        for key, value in data.items():
            if key not in known_keys:
                errors.append(f"{key}: unknown config key")
                continue
            if is_unchanged_secret(key, value):
                # 密码框回填的是哨兵(见 config_manager.SECRET_UNCHANGED)。前端每次
                # 保存都提交全部键,原样回来的哨兵意思是「没改」—— 写进去就把真密码
                # 换成了哨兵字面量,下一次 Earthdata 登录 401。清空密码不受影响:
                # 空串不等于哨兵,照常落库。
                continue
            try:
                if not config_manager.validate_config(key, str(value)):
                    # 与 ConfigManager.set 的报错口径一致
                    raise ValueError(f'Invalid value for config key {key}: {value}')
                valid_items[key] = str(value)
                updated_keys.append(key)
            except ValueError as e:
                errors.append(f"{key}: {str(e)}")

        # M9：严格全或无 —— 有任何非法键就直接拒绝，**一个键都不写库**。
        # 此前是「先把非法键过滤掉再调 set_many」，等于在路由层反转了
        # ConfigManager.set_many 自己声明并测试过的语义（「任一键非法整批拒绝，
        # callers never observe a half-updated configuration」）。而前端每次保存
        # 都提交全部键，于是**任何单个字段填错都会让其余键在用户被告知失败的
        # 同时静默生效** —— 用户以为并发数没改，下一次下载却按新值跑。
        if errors:
            return jsonify({
                'success': False,
                'updated': [],
                'errors': errors,
                'message': t('api.config.invalid_values_not_saved'),
            }), 400

        if valid_items:
            try:
                config_manager.set_many(valid_items)
            except Exception as e:
                # 整批失败（set_many 内部已回滚，库中无任何半更新）
                logger.error(f"Error updating config batch: {e}")
                errors.append(f"{', '.join(valid_items)}: Failed to update")
                updated_keys = []

        # Return response
        if errors:
            return jsonify({
                'success': False,
                'updated': updated_keys,
                'errors': errors
            }), 400
        else:
            return jsonify({
                'success': True,
                'updated': updated_keys,
                'message': f'Updated {len(updated_keys)} configuration settings'
            })

    except Exception as e:
        logger.error(f"Error updating config: {e}")
        return jsonify({'error': 'Failed to update config'}), 500


@api_bp.route('/config/reset', methods=['POST'])
def reset_config():
    """
    Reset all configuration settings to their default values.

    Deletes every config row and re-seeds DEFAULT_CONFIGS (this also clears
    proxy_url and Earthdata credentials — that is the intended "reset all"
    semantics; the UI confirms with the user before calling this).

    Returns:
        JSON response with success status
    """
    try:
        config_manager.reset_to_defaults()
        return jsonify({
            'success': True,
            'message': 'Configuration reset to defaults'
        })

    except Exception as e:
        logger.error(f"Error resetting config: {e}")
        return jsonify({'error': 'Failed to reset config'}), 500


@api_bp.route('/cache/stats', methods=['GET'])
def get_cache_stats_api():
    """分类统计下载缓存占用（cache 顶层每个子目录一类）。

    只读接口。缓存不做任何自动清理 —— 清理由用户在前端手动触发
    （POST /api/cache/clear，界面带二次确认）。
    """
    from src.services.task_cleanup import get_cache_stats

    try:
        stats = get_cache_stats()
        return jsonify({'success': True, **stats})
    except Exception as e:
        logger.error(f"Error getting cache stats: {e}")
        return jsonify({'error': 'Failed to get cache stats'}), 500


def _delete_payload(message: str, files_removed, files_deferred: bool = False,
                    *, cache_removed=None, cache_deferred: bool = False):
    """DELETE 端点的统一响应体（M10）。

    `remove_task_dir_if_safe` 用返回值区分「已删」与「越界拒删」，但四个调用点
    此前全部丢弃它 —— 任何护栏命中都只写一条 warning，HTTP 仍是
    200 {"success": true}。用户点了「删除并删文件」，看到成功提示，几十 GB
    产物却纹丝不动（存量相对 output_path 尤其容易命中，见 M10）。

    files_removed 为 None 表示调用方没要求删文件，响应里就不带这两个字段。

    files_deferred=True 是删除**正在运行**的任务时的新形态：行已经没了，但产物
    要等工作线程收工才能删（四条管线都有分钟级的 GDAL 阻塞区，见 task_deletion）。
    此时 files_removed 必然是 None —— 还没删，给不出真假。

    cache_removed 是 `?clear_cache=1` 清掉的**共享缓存**（不是产物）
    `(字节数, 文件数)`。它与 files_* 是两码事，所以是两组字段：产物是这个任务
    的成果，缓存是所有同源任务共用的下载中间层。cache_deferred 与 files_deferred
    同理 —— 删一个**正在跑**的任务时缓存要等工作线程收工才能清（它还在往
    cache 里写），这时给不出真数，只说「还在做」而不是回一个 0 骗人。
    """
    payload = {'success': True, 'message': message}
    if cache_deferred:
        payload['cache_deferred'] = True
    elif cache_removed is not None:
        payload['cache_removed_bytes'] = int(cache_removed[0])
        payload['cache_removed_files'] = int(cache_removed[1])
    if files_deferred:
        payload['files_deferred'] = True
        return payload
    if files_removed is not None:
        payload['files_removed'] = bool(files_removed)
        if not files_removed:
            payload['files_message'] = t('api.tasks.files_kept_unsafe_dir')
    return payload


# 四张任务表 → 可读管线名的 i18n 键。顺序即 _unfinished_task_labels 遍历的顺序，
# 也是 409 响应里 active_tasks 列表的顺序（用户看到的先后）。
_ACTIVE_TASK_TABLES = (
    ('tasks', 'api.tasks.pipeline.map'),
    ('dem_tasks', 'api.tasks.pipeline.dem'),
    ('contour_tasks', 'api.tasks.pipeline.contour'),
    ('local_terrain_tasks', 'api.tasks.pipeline.local_terrain'),
)


def _unfinished_task_labels():
    """四条管线里所有未终结任务的可读标签。

    M8：查 DB 而不是查四个 manager 的 active_tasks —— 本蓝图只持有 map 管线的
    manager 全局，查 DB 是唯一不需要额外注入就能覆盖四条管线的口径，且能连
    「进程重启后仍是 paused」的任务一起算进来。

    「未终结」的名单取自 contracts.outcome.ACTIVE_STATE_VALUES，**不在这里
    手写**。这里曾经写死 ('pending','running','paused')，而缺块改造新增了
    retrying / pending_decision —— 漏掉它们的后果不是显示错行，是这道 409 闸
    直接放行：用户在一个正在补漏的任务跑着的时候清空整个缓存，那个任务随后
    静默地少下一片瓦片（cache 命中瓦片从不进 task_tiles，完成判定看不见它们）。

    ⚠️ 这个函数**失败即放行**（返回空列表 = 「没有未完成任务」= 409 闸不拦），
    那是给「配置库暂时读不出来时不要把用户唯一的磁盘回收入口也堵死」留的口子。
    代价是任何落进下面那个 except 的异常都会静默地把这道安全闸关掉。所以：
    **凡是不属于「DB 读不出来」的东西，一律不许进那个 try。** 遍历用的表清单
    先绑成局部变量、放在 try 外面 —— 它要是没了（重构时被删掉过一次，
    NameError 被 except 吃掉，闸门从此永远放行且只留一条 warning），现在会直接
    抛成 500，吵、但看得见。宁可让清理接口报错，也不要让它假装没有任务在跑。
    """
    from src.core.database import get_connection_context

    tables = _ACTIVE_TASK_TABLES
    placeholders = ', '.join('?' * len(ACTIVE_STATE_VALUES))
    labels = []
    try:
        with get_connection_context() as conn:
            for table, label_key in tables:
                try:
                    rows = conn.execute(
                        f"SELECT id FROM {table} "
                        f"WHERE status IN ({placeholders}) ORDER BY id",
                        ACTIVE_STATE_VALUES,
                    ).fetchall()
                except Exception:
                    continue  # 表不存在（旧库）时跳过，不阻断清理
                label = t(label_key)
                labels.extend(f"{label} #{row['id']}" for row in rows)
    except Exception as e:
        # 措辞要说清后果，不能只说「查询失败」：这条 warning 出现的时候，
        # 用户下一秒就能清掉一个正在跑的任务的缓存。
        logger.warning(
            f"Cannot list unfinished tasks; the cache-clear guard will NOT "
            f"block this request: {e}")
    return labels


@api_bp.route('/cache/clear', methods=['POST'])
def clear_cache_api():
    """手动清理一个缓存分类（或 __all__ 全部分类）。

    Request Body:
        {"category": "<key>", "force": false} —— key 取自 GET /api/cache/stats 的
        categories[].key，"__all__" 表示全部分类；force=true 跳过活动任务检查。

    Returns:
        200 {success, cleared: [{key, removed_bytes, removed_files}],
             total_removed_bytes}；非法/不存在的 category 400；
        有未结束任务且未传 force 时 409 {error, active_tasks}。

    为什么要拦（M8）：地图任务在枚举阶段就把 cache 命中的瓦片移出待下载列表并
    计入 downloaded_tiles，产物目录靠补拷线程从 cache 复制。清掉分类目录后这些
    瓦片既不会重下、复制失败也只吞成 warning，而完成判定只看 task_tiles 的失败
    行 —— cache 命中瓦片从不在那张表里。tiles_only 任务因此完全无声：任务
    completed、计数满值、产物目录静默缺瓦片。检查后仍有 check-then-act 残余窗口
    （清理途中仍可能有任务被 start），这是刻意接受的取舍，要彻底消除需拿管理器锁。
    """
    from src.services.task_cleanup import clear_cache_category, get_cache_stats

    try:
        data = request.get_json(silent=True)
        category = data.get('category') if isinstance(data, dict) else None
        if not category or not isinstance(category, str):
            return jsonify({'error': 'Missing category'}), 400

        force = bool(data.get('force')) if isinstance(data, dict) else False
        if not force:
            unfinished = _unfinished_task_labels()
            if unfinished:
                return jsonify({
                    'error': t(
                        'api.tasks.cache_clear_blocked',
                        tasks=t('api.tasks.label_separator').join(unfinished),
                    ),
                    'active_tasks': unfinished,
                }), 409

        if category == '__all__':
            keys = [c['key'] for c in get_cache_stats()['categories']]
        else:
            keys = [category]

        cleared = []
        total_removed_bytes = 0
        for key in keys:
            # force 一路传到底：上面那道 409 闸看的是「有没有未完成任务」（粗，
            # 覆盖四条管线），clear_cache_category 里那道看的是「这个命名空间此刻
            # 有没有活动任务在用」（细，只覆盖地图缓存）。两道判据不同，都要能被
            # 同一个 force 掀掉，否则用户确认过之后仍会撞上第二道，拿到一个
            # 400 而不是他刚刚同意的清理。
            result = clear_cache_category(key, force=force)
            cleared.append({'key': key, **result})
            total_removed_bytes += result['removed_bytes']

        return jsonify({
            'success': True,
            'cleared': cleared,
            'total_removed_bytes': total_removed_bytes,
        })

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Error clearing cache: {e}")
        return jsonify({'error': 'Failed to clear cache'}), 500


@api_bp.route('/fs/browse', methods=['GET'])
def browse_dir():
    """目录选择弹窗的数据源：列出某目录的子目录（0.2.4 起全盘可浏览）。

    Query: ?path=<绝对路径>。缺省时：Windows 返回盘符列表，POSIX 返回
    根目录 /。不存在/不是目录一律 400。只列非隐藏子目录（文件不列，
    弹窗只选目录）。parent 为 null 表示已到根（没有「上一级」可去）。
    """
    raw = (request.args.get('path') or '').strip()

    if not raw:
        # 根级:Windows 给盘符列表(parent=null);POSIX 直接按 / 列目录
        if os.name == 'nt':
            import string
            drives = [
                {'name': f"{d}:\\", 'path': f"{d}:\\"}
                for d in string.ascii_uppercase
                if Path(f"{d}:\\").exists()
            ]
            return jsonify({'success': True, 'path': '', 'parent': None, 'dirs': drives})
        target = Path('/')
    else:
        # U4: 解析与存在性检查都可能抛 —— `~未知用户` 抛 RuntimeError
        # (Could not determine home directory)，含空字节的 path 抛 ValueError
        # (embedded null byte)，都不在下面 iterdir 的 except OSError 覆盖内。
        # 源码运行默认 DEBUG=1 且绑 0.0.0.0，未捕获异常会让 Werkzeug 调试器把
        # 完整堆栈回给浏览器。统一按 400 处理。
        try:
            target = Path(raw).expanduser().resolve()
        except (OSError, ValueError, RuntimeError) as e:
            return jsonify({'success': False,
                            'error': t('api.fs.invalid_path', error=e)}), 400

    try:
        if not target.exists():
            return jsonify({'success': False, 'error': t('api.fs.dir_not_found')}), 400
        if not target.is_dir():
            return jsonify({'success': False, 'error': t('api.fs.not_a_dir')}), 400
    except (OSError, ValueError) as e:
        return jsonify({'success': False,
                        'error': t('api.fs.invalid_path', error=e)}), 400

    try:
        dirs = [
            {'name': e.name, 'path': str(target / e.name)}
            for e in sorted(target.iterdir(), key=lambda e: e.name)
            if e.is_dir() and not e.name.startswith('.')
        ]
    except OSError as e:
        return jsonify({'success': False,
                        'error': t('api.fs.read_dir_failed', error=e)}), 400

    parent = target.parent
    return jsonify({
        'success': True,
        'path': str(target),
        'parent': None if parent == target else str(parent),
        'dirs': dirs,
    })


@api_bp.route('/raster/inspect', methods=['POST'])
def inspect_raster_headers():
    """解释浏览器读出的 GeoTIFF 头部，回给「选完 tif 立刻看到的有效信息」。

    **刻意不接收文件本身。** 前端 static/js/geotiff_meta.js 用 File.slice 只读了
    几 KB 的 IFD，把原始标签发过来，这里只做地理解释（EPSG -> 坐标系名称、
    投影坐标 -> WGS84、像素尺寸 -> 建议层级）。一份 2 GB 的 DEM 因此不会为了
    看一眼元信息先整包上传一遍 —— 真正的上传只发生在创建任务时。

    Body: {"files": [<geotiff_meta.js 的 read() 返回值>, ...],
           "mode": "terrain" | "contour"}
    mode 决定建议层级按哪条管线算（两条管线的分块方式不同，见 raster_probe）。

    放在通用 /api 蓝图而不是某条任务管线下：本地高程切片与等高线两个表单都用
    它，而它不碰任何任务状态。
    """
    # 体积在解析之前挡。全局的 MAX_CONTENT_LENGTH 是 2 GiB（给真上传留的），
    # 而这条接口按设计只收几 KB 标签 —— 不先看 content_length，就等于允许对方
    # 让服务端先把 2 GiB 缓存下来解析完，再被 MAX_INSPECT_FILES 拒掉。
    if (request.content_length or 0) > MAX_INSPECT_BODY:
        return jsonify({'error': t('api.raster.body_too_large')}), 413

    # 数组/字符串体是合法 JSON 且为真，但没有 .get —— `or {}` 接不住它们。
    # 口径与同文件的 verify_tile_url 一致。
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}

    try:
        result = describe_headers(payload.get('files'),
                                  mode=payload.get('mode') or 'terrain')
        return jsonify({'success': True, **result})
    except InspectError as e:
        # 键 + 参数由服务层给，这里只负责按当前语种翻译；异常原文不回显。
        return jsonify({'error': t(e.key, **e.params)}), 400
    except Exception as e:
        logger.error(f"Error inspecting raster headers: {e}")
        return jsonify({'error': t('api.raster.inspect_failed')}), 500


@api_bp.route('/config/recommend_concurrency', methods=['POST'])
def recommend_concurrency_route():
    """按当前网络环境实测吞吐，推荐 concurrent_downloads（配置页「测速推荐」）。

    用已保存的 tile_servers / proxy_url / 地图中心做真实瓦片阶梯测速
    （约 30 秒）。测速流程自身保证不抛（全失败回退保守值），这里只兜
    意料之外的异常，同样 200 + fallback —— 按钮前端按 fallback 展示。
    """
    from src.services.proxy_autodetect import resolve_from_config
    from src.services.tile_url_probe import (
        RECOMMEND_FALLBACK, parse_server_list, recommend_concurrency,
    )

    def _float(key, default):
        try:
            return float(config_manager.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    try:
        result = recommend_concurrency(
            parse_server_list(config_manager.get('tile_servers', '') or ''),
            style=config_manager.get('default_style', 's') or 's',
            proxy_url=resolve_from_config(config_manager),
            center_lng=_float('map_center_lng', 106.55),
            center_lat=_float('map_center_lat', 29.56),
        )
    except Exception as e:
        logger.warning(f'recommend_concurrency route failed: {e!r}')
        result = {'recommended': RECOMMEND_FALLBACK, 'fallback': True,
                  'rising': False,
                  'note': t('api.config.speedtest_fallback_note'),
                  'samples': []}
    return jsonify(result)


@api_bp.route('/config/verify_tile_url', methods=['POST'])
def verify_tile_url():
    """验证单个瓦片服务器条目的通联（配置页每行「验证」按钮）。

    Request Body: {"server": "mts0" | "mts0.google.cn" | "https://.../{z}/{x}/{y}.png"}
    条目校验失败返回 400；通联结果始终 200 + {success, status_code,
    content_type, elapsed_ms, tile, url, error} —— 连不上也是一次成功的探测。
    """
    from src.services.proxy_autodetect import resolve_from_config
    from src.services.tile_url_probe import probe_server_entry, validate_server_entry

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'No JSON body provided'}), 400
    server = str(data.get('server') or '').strip()

    ok, err = validate_server_entry(server)
    if not ok:
        return jsonify({'error': err}), 400

    def _float(key, default):
        try:
            return float(config_manager.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    result = probe_server_entry(
        server,
        proxy_url=resolve_from_config(config_manager),
        center_lng=_float('map_center_lng', 106.55),
        center_lat=_float('map_center_lat', 29.56),
    )
    return jsonify(result)


@api_bp.route('/config/proxy_status', methods=['GET', 'POST'])
def proxy_status():
    """代理自动发现的状态（GET）/ 强制重新探测（POST），配置页用。

    POST 是同步的：清空上一轮结果后重新枚举 + 验证，最坏二十几秒。前端按钮
    自己转圈等 —— 用户点了「立即检测」就是在等一个确定的答复,给异步 202 反而
    要再轮询一遍。

    返回体除 state 快照外附带 effective / manual / auto_enabled 三个字段,
    让配置页能直说「现在实际用的是哪个、为什么」,而不是让用户对着一个开关猜。
    """
    from src.services.proxy_autodetect import (
        auto_detect_enabled, autodetect, get_state, mask_url_secrets,
        reset_state, resolve_from_config,
    )
    from src.app_factory import probe_url_from_config

    manual = (config_manager.get('proxy_url', '') or '').strip()
    auto_enabled = auto_detect_enabled(config_manager)

    if request.method == 'POST':
        if not auto_enabled:
            return jsonify({'error': t('api.config.proxy_autodetect_disabled')}), 400
        reset_state()
        try:
            autodetect(probe_url=probe_url_from_config(config_manager))
        except Exception as e:
            # autodetect 自己已经兜了异常并写进 state.error,这里只防意料之外
            logger.warning(f'proxy_status forced detection failed: {e!r}')

    state = get_state()
    # effective 走与下载引擎同一个解析器,但不等后台探测(wait_s=0)——
    # 配置页只是展示当前事实,不该为了一个状态查询挂住请求。
    effective = resolve_from_config(config_manager, wait_s=0)
    state.update({
        'manual': mask_url_secrets(manual) if manual else '',
        'auto_enabled': auto_enabled,
        'effective': mask_url_secrets(effective) if effective else '',
        'effective_source': ('manual' if manual
                             else (state['source'] if effective else '')),
    })
    return jsonify(state)


# ---------------------------------------------------------------------------
# 区域（§5.1）：文件导入与建任务前的规模/预算预估
# ---------------------------------------------------------------------------


# multipart 信封余量：boundary 行 + Content-Disposition/Content-Type 头，正常
# 几百字节。Content-Length 量的是整个请求体，把它直接对着 MAX_IMPORT_BYTES 卡，
# 一个刚好 32 MiB 的合法文件会被误拒；给 64 KiB 是「宽到不可能误伤、窄到
# 挡住的东西仍然远小于 2 GiB」。
_MULTIPART_ENVELOPE_BYTES = 64 * 1024


@api_bp.route('/region/import', methods=['POST'])
def import_region_route():
    """上传一个区域边界文件（GeoJSON / KML / KMZ / zip 内的 shapefile / 裸 shp）。

    Request: multipart/form-data，字段名 `file`。
    Returns: 200 {region: <RegionSpec.to_dict()>, summary, warnings}；
             缺文件 / 扩展名不受理 / 解析失败一律 400。

    为什么在这里再限一次体积：Flask 的 MAX_CONTENT_LENGTH 是 2 GiB（那是给本地
    地形上传留的），套在区域文件上等于允许对方先让服务端把 2 GiB 收下来再被
    region_import 的 32 MiB 闸拒掉。

    **两道闸的顺序是关键。** `read(MAX_IMPORT_BYTES + 1)` 只限住「读进内存多少」，
    而 `request.files` 这个属性一取，werkzeug 就已经把整个请求体 spool 到临时
    文件了 —— 落盘的仍是完整的 2 GiB，一次 POST 就能在临时盘上写满 2 GiB，而
    返回码是 400：用户看不出发生了什么，盘却真的少了。所以先看
    `Content-Length`，在碰 `request.files` 之前拒。多读的那一个字节仍然留着 ——
    它是「声明值撒谎了没有」的第二道判据，服务层拿到 32 MiB + 1 会给出带具体
    数字的报错，比路由层自己编一句更有用（它知道限额是多少、该怎么办）。
    """
    # Content-Length 是客户端声明值，可以撒谎 —— 但两种谎都不亏：撒小了
    # werkzeug 按 MAX_CONTENT_LENGTH 自己截断（413），撒大了正好被这道闸拦住。
    # 余量是 multipart 信封（boundary + Content-Disposition 头，正常几百字节）：
    # 卡得死紧会把一个刚好 32 MiB 的合法文件误拒。
    declared = request.content_length
    if declared is not None and declared > region_import.MAX_IMPORT_BYTES + _MULTIPART_ENVELOPE_BYTES:
        logger.warning(
            f"Region import rejected before spooling: Content-Length {declared} "
            f"exceeds {region_import.MAX_IMPORT_BYTES} + envelope")
        return jsonify({'error': t('api.region.too_large',
                                   limit_mb=region_import.MAX_IMPORT_BYTES // 1048576)}), 413

    upload = request.files.get('file')
    if upload is None or not (upload.filename or '').strip():
        return jsonify({'error': t('api.region.no_file')}), 400

    filename = upload.filename
    lowered = filename.lower()
    if not any(lowered.endswith(ext) for ext in region_import.SUPPORTED_EXTENSIONS):
        # 扩展名只是**前置**过滤：真正的分派看魔数（浏览器上传的 .zip 里装的是
        # kmz 还是 shapefile 只有看内容才知道）。这道闸挡的是「误把 .tif 拖进
        # 区域导入框」那一类，让用户立刻看到受理清单，而不是先传 32 MiB 再被
        # 一句 "unrecognised region file" 打回。
        # 受理清单**不进译文**，作为独立字段回给前端拼接：它的事实来源是
        # region_import.SUPPORTED_EXTENSIONS，塞进 zh + en 两条译文就变成三份，
        # 加一种格式时必然漏改其中一份。
        return jsonify({
            'error': t('api.region.unsupported'),
            'supported_extensions': list(region_import.SUPPORTED_EXTENSIONS),
        }), 400

    try:
        data = upload.read(region_import.MAX_IMPORT_BYTES + 1)
    except OSError as e:
        logger.warning(f"Region import: cannot read uploaded file: {e}")
        return jsonify({'error': t('api.region.import_failed')}), 400

    try:
        spec, import_warnings = region_import.import_region(filename, data)
    except (region_import.RegionImportError, RegionValidationError) as e:
        # 两个都是 ValueError 的子类，本可以落到蓝图通用的 400 分支，但那条分支
        # 回的是**裸英文原文**，直接进中文界面就是一句没头没脑的话。
        #
        # 反过来只回译文也不行，那是这里之前的做法：七种完全不同的失败
        #   —— 不是 JSON / 空文件 / 传了个 Point 要素 / 坐标越界 / 环退化 /
        #      KML 截断 / 假 zip ——
        # 全塌进「请换一个文件或检查它的坐标系」一句。对上传了点要素的人来说，
        # 「检查坐标系」不只是没用，是把他往错的方向指一整个下午。
        #
        # 所以外壳译、原因不译：`api.region.import_failed_detail` 的中文外壳
        # 说「导入失败」，`{reason}` 原样带上服务层那句知道细节的英文
        # （它知道是第几个环、少了哪个 zip 成员、限额是多少）。日志仍然照记，
        # 因为 reason 已经被 region_import._echo 截短过，日志里那句才是全的。
        logger.warning(f"Region import failed for {filename!r}: {e}")
        return jsonify({'error': t('api.region.import_failed_detail', reason=str(e))}), 400

    # warnings 是「导进来了，但你可能不是这个意思」。两个来源：解析过程中被
    # 降级处理的地方（缺 / 读不出 .prj、GB18030 回退、丢掉的非面要素……由
    # region_import 以 WARNING_CODES 里的码给出），加上这里判的跨反经线 ——
    # 后者不是错误（RegionSpec 支持并会自己拆段），但用户框选时经常是误操作：
    # 一个横跨整个太平洋的区域和一个小岛在缩略图上分不出来，瓦片数差六个量级。
    # 回的一律是**机器码**不是译文：js.region.* 归前端所有，由它按当前语种渲染。
    # 后端往里塞成品中文，等于把一份文案钉死在两个所有者之间；塞英文原句更糟 ——
    # 中文界面上直接漏出一句生英文。
    warnings = list(import_warnings)
    if spec.crosses_antimeridian:
        warnings.append('crosses_antimeridian')

    return jsonify({
        'success': True,
        'region': spec.to_dict(),
        'summary': spec.summary(),
        'warnings': warnings,
    })


def _region_from_payload(payload: dict) -> RegionSpec:
    """请求体 → RegionSpec。`region` 优先，`bbox` 兜底。非法一律抛 ValueError。

    两种入参形态对应界面上的两条路：导入过文件就有完整的 `region`（多边形、
    可能带洞），只在地图上拖了个框就只有 `bbox`。**不做第三种猜测** —— 都没有
    就直接报错，绝不默默按全球算（全球在 z18 是 687 亿张瓦片，静默降级成它
    等于让用户对着一个荒谬的预估数字发呆）。
    """
    region = payload.get('region')
    if isinstance(region, dict):
        return RegionSpec.from_dict(region)

    bbox = payload.get('bbox')
    if isinstance(bbox, dict):
        return RegionSpec.from_bbox(
            north=bbox.get('north'), south=bbox.get('south'),
            east=bbox.get('east'), west=bbox.get('west'))
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        # 顺序是 (west, south, east, north) —— 与 RegionSpec.bounds、GDAL、
        # GeoJSON 的 bbox 成员一致。任务表的列序恰好相反（north/south/east/west），
        # 两者在本仓库里长期并存，所以这里写死并注明，不靠调用方记住。
        west, south, east, north = bbox
        return RegionSpec.from_bbox(north=north, south=south, east=east, west=west)

    raise RegionValidationError(
        "request must carry either a 'region' object or a 'bbox' "
        "[west, south, east, north]")


# /api/region/estimate 的**预检**闸门。这条接口把调用方给的 `region` 直接喂给
# 栅格化扫描线（count_region_tiles），而扫描线的开销是「顶点数 × 扫描行数」——
# 两个量都由请求体决定。任何一个不封顶，一个请求就能把一个 Flask worker 占住
# 几十分钟（本机实测：梳状多边形 z16 = 104 s；全球锯齿 z0..21 = 11.3 s；
# 75 KB 请求体 ≈ 56 min）。三条都不是「大区域」，是**畸形几何** —— 合法用途里
# 没有任何一条长这样，所以拒它们不损失任何真实场景。
#
# 闸门一：顶点数，与文件导入路径**共用同一个常量**
# （`region_import.MAX_TOTAL_VERTICES`）。那条路径早就封了顶，而 `region` 字段
# 是同一份几何的另一个入口 —— 两个入口两套上限，等于第一个上限白设。
#
# 闸门二 / 三：工作量。两个量互不覆盖，都要判：
#   · 瓦片数上界 —— 用**外接矩形**的瓦片数（每层 O(1)，region ⊆ bbox 所以它是
#     真上界）。阈值取 WARN_TILES_THRESHOLD 的 500 倍：项目对「很大」的定义就是
#     那个软阈值（超过只警告不拒），硬上限必须明显高于它，否则就把一个只是
#     「很大但合法」的任务拒了 —— 而这条接口存在的意义恰恰是回答「这么大到底
#     要多少盘」。全球 z0..21 是 5.8e12，差着五个数量级，不会误伤。
#   · 扫描线工作量 = 顶点数 × 外接矩形扫描行数。为什么瓦片数封了顶还要它：
#     一个百万顶点的梳状多边形塞在 0.1° 的小框里，瓦片数只有几十万（轻松过闸），
#     但每一行都要遍历全部活动边。实测吞吐 ≈ 2.3e6 单位/秒（i9-12900H,
#     CPython 3.11），取 2e7 ≈ 最坏 9 秒。预检比这还慢就该让用户先简化几何，
#     而不是让他对着转圈等 —— 报错里两条出路（简化几何 / 降 zoom_max）都写明。
MAX_ESTIMATE_TILES = 500 * WARN_TILES_THRESHOLD
MAX_ESTIMATE_SCAN_WORK = 20_000_000


def _preflight_region_cost(region: RegionSpec, zoom_min: int, zoom_max: int) -> None:
    """栅格化之前给开销封顶。超限抛 `RegionValidationError`（路由映射成 400）。

    只算**上界**：一个 O(层数) 的循环加一次 `vertex_count`，绝不遍历几何 ——
    一道防 DoS 的闸自己先跑上一分钟就没有意义了。
    """
    vertices = region.vertex_count
    if vertices > region_import.MAX_TOTAL_VERTICES:
        raise RegionValidationError(
            f"region has {vertices} vertices, more than the "
            f"{region_import.MAX_TOTAL_VERTICES} supported. Simplify the geometry "
            f"(QGIS: Vector > Geometry Tools > Simplify) and try again.")

    bbox_tiles = 0
    scan_rows = 0
    for zoom in range(zoom_min, zoom_max + 1):
        x_min, x_max, y_min, y_max = bbox_tile_range(
            region.bbox_north, region.bbox_south, region.bbox_east,
            region.bbox_west, zoom)
        # 跨反经线时 x_max 是**未回绕**的列号（east 可以 > 180），相减得到的
        # 跨度仍然是对的 —— 这里要的就是跨度，不是坐标。
        rows = y_max - y_min + 1
        scan_rows += rows
        bbox_tiles += rows * (x_max - x_min + 1)

    if bbox_tiles > MAX_ESTIMATE_TILES:
        raise RegionValidationError(
            f"zoom {zoom_min}-{zoom_max} over this area spans up to {bbox_tiles} "
            f"tiles, more than the {MAX_ESTIMATE_TILES} this pre-flight will count. "
            f"Lower zoom_max or narrow the area.")

    work = vertices * scan_rows
    if work > MAX_ESTIMATE_SCAN_WORK:
        raise RegionValidationError(
            f"counting tiles for this geometry would take too long: {vertices} "
            f"vertices x {scan_rows} tile rows over zoom {zoom_min}-{zoom_max}. "
            f"Simplify the geometry or lower zoom_max.")


@api_bp.route('/region/estimate', methods=['POST'])
def estimate_region_route():
    """建任务之前回答两个问题：这一片有多少张瓦片、盘够不够。

    Body: {region | bbox, zoom_min, zoom_max, style, output_format, output_path}
    Returns: 200 {tile_count, estimate, verdict}；请求体畸形到无法便宜地回答时 400
           （见 _preflight_region_cost）。

    瓦片数走 contracts/region_tiles.count_region_tiles —— 与下载器真正枚举时
    用的是同一个函数，所以「预估的数」和「实际要下的数」不会对不上（这正是
    把瓦片数学收进 contracts 的理由）。多边形区域按栅格化后的实际覆盖算，
    不是外接矩形，两者在细长区域上能差几倍。

    预算判决只是**建议**，这条接口不写任何状态、不拦任何东西。启动时
    （services/disk_budget.check_budget 的各 start_* 调用点）同样只记录不拦
    —— 拦截语义 2026-08 起整体移除，这里是让用户在点「开始」之前就看到
    verdict 里的具体数字。
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        # 数组/字符串体是合法 JSON 且为真，但没有 .get —— 口径与同文件的
        # verify_tile_url / inspect_raster_headers 一致。
        payload = {}

    try:
        region = _region_from_payload(payload)
        zoom_min, zoom_max = validate_zoom_range(
            payload.get('zoom_min'), payload.get('zoom_max'))
        # 顺序要紧：先把层级校验出来，才知道要按几层封顶；预检必须在任何
        # 栅格化动作**之前**（那正是要防的开销）。
        _preflight_region_cost(region, zoom_min, zoom_max)
    except ValueError as e:
        # RegionValidationError 也是 ValueError。两者的原文都已经是可操作的
        # 一句话（"zoom_min must be ..."），直接当 body 返回，与蓝图既有口径一致。
        return jsonify({'error': str(e)}), 400

    style_code = style_code_for(payload.get('style'))
    output_format = str(payload.get('output_format') or 'both')
    # MBTiles 与 output_format **正交**（§5.3：同一任务的第 N 种产物），从
    # output_format 里推不出来，必须由这里显式传。漏传的后果是预检结论**整整
    # 少算一份松散镜像**：容器体积约等于整套瓦片，而它在任务最后一步才生成 ——
    # 于是「盘够」的判决在跑了几小时之后才被现实推翻。
    export_mbtiles = bool(payload.get('export_mbtiles'))

    try:
        estimate = disk_budget.estimate_map_task(
            region, zoom_min, zoom_max, output_format, style_code,
            export_mbtiles=export_mbtiles)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # tile_count 直接取估算器算好的那个数，**不再单独调一次 count_region_tiles**：
    # estimate_map_task 内部已经逐层数过（`tile_count=sum(tiles_by_zoom)`），
    # 两者定义上相等，而多那一次调用就是把整个栅格化跑第二遍 —— 一条本来
    # 9 秒的预检变 18 秒，纯白付。
    tile_count = estimate.tile_count

    # 判决要按**产物真正要落的那块盘**算。没给路径就用出厂下载目录 —— 那是
    # 建任务表单里的默认值，判一块和用户最终选的盘无关的盘会给出误导的结论。
    output_path = (payload.get('output_path') or '').strip()
    target = (resolve_stored_output_dir(output_path) if output_path
              else Path(Config.DOWNLOADS_DIR) / 'map')
    verdict = disk_budget.check_budget(target, estimate, config_manager)

    return jsonify({
        'success': True,
        'tile_count': tile_count,
        'estimate': asdict(estimate),
        'verdict': asdict(verdict),
    })


# ---------------------------------------------------------------------------
# 地名搜索（可选特性）与调度器状态
# ---------------------------------------------------------------------------


@api_bp.route('/places/search', methods=['GET'])
def search_places_route():
    """地名 / 行政区搜索。Query: `q`、`limit`。

    Returns: 200 {enabled, results: [{name, bbox, kind, region}]}

    **没配 geocoder_url 时回 200 + enabled=false，不是 4xx。** 这是一个出厂即
    关闭的可选特性（数据源要用户自己填，见 services/geocoding 的模块 docstring），
    「没开」不是错误。回 4xx 会让前端在控制台里刷红、让搜索框显示成「出错了」，
    而正确的表现是搜索框根本不出现 —— enabled 这个布尔就是给它判的。
    """
    query = (request.args.get('q') or '').strip()
    raw_limit = request.args.get('limit')

    if not geocoding.geocoder_configured(config_manager):
        # 先判开关再判 q：没开的时候连「你没输关键词」都不该说。
        return jsonify({'success': True, 'enabled': False, 'results': [],
                        'message': t('api.places.disabled')})
    if not query:
        return jsonify({'success': True, 'enabled': True, 'results': []})

    # limit 的钳位在服务层（geocoding._clamp_limit 把脏值退回默认、范围钳到
    # 1..50），这里原样透传，不在路由层再写一份区间 —— 两份区间迟早不一致。
    kwargs = {}
    if raw_limit is not None:
        kwargs['limit'] = raw_limit

    try:
        results = geocoding.search_places(query, config_manager=config_manager,
                                          **kwargs)
    except geocoding.GeocodingDisabled:
        # 上面查过一次开关，但配置可以在两次调用之间被改掉（配置页就在隔壁）。
        # 竞态下仍然按「没开」回，语义一致。
        return jsonify({'success': True, 'enabled': False, 'results': [],
                        'message': t('api.places.disabled')})
    except Exception as e:
        # 上游超时 / 返回了非 JSON / DNS 失败。原文可能含服务地址，只进日志；
        # 回给用户的是译文键。502 而不是 500：故障在**上游**，用户能做的是检查
        # 那个地址而不是来查我们的堆栈。
        logger.warning(f"Place search failed for {query!r}: {e}")
        return jsonify({'error': t('api.places.failed')}), 502

    return jsonify({'success': True, 'enabled': True, 'results': results})


@api_bp.route('/scheduler/status', methods=['GET'])
def scheduler_status():
    """全局配额的当前视图：每种资源的上限、已占用、以及占用者。

    这是「为什么我的第三个任务点了开始却不动」唯一说得清的地方 —— 任务列表只
    会显示排队中，看不出它在等哪一种资源。snapshot() 已经是可直接 json.dumps
    的形态（ResourceKind 渲染成 .value 字符串），这里不再加工。
    """
    try:
        return jsonify({'success': True, **get_scheduler().snapshot()})
    except Exception as e:
        logger.error(f"Error getting scheduler status: {e}")
        return jsonify({'error': 'Failed to get scheduler status'}), 500


# ---------------------------------------------------------------------------
# 缺块（§13-3）：查看 / 补漏 / 接受
# ---------------------------------------------------------------------------


# 管线 → 任务表名。**不在这里手写映射** —— 唯一那份住在 contracts/artifact
# （`Artifact.task_table`），这里只是把它按管线摊平成一张查表，import 时算一次。
# 手抄一份的代价很具体：加管线时这里漏改，存在性检查会去查一张不存在的表，
# 于是每一个该 404 的请求都变成 500。
_TASK_TABLES = {
    p: Artifact(pipeline=p, task_id=0, kind=ArtifactKind.MBTILES, path='-').task_table
    for p in PIPELINES
}


def _task_exists(pipeline: str, task_id: int) -> bool:
    """任务行是否存在。给缺块与导出那几条接口分「404」与「400」用。

    没有它，`refill_task` 的 ValueError 就同时代表「任务不存在」和「当前状态
    不允许补漏」，两者一个是 404 一个是 400，糊成一种的话前端没法区分「这条
    记录没了，刷新列表」和「这条记录还在，只是现在不能补」。
    """
    table = _TASK_TABLES[pipeline]
    conn = get_connection()
    try:
        return conn.execute(
            f'SELECT 1 FROM {table} WHERE id = ?', (task_id,)).fetchone() is not None
    finally:
        conn.close()


@api_bp.route('/tasks/<int:task_id>/gaps', methods=['GET'])
def get_task_gaps(task_id: int):
    """这个任务缺了哪些瓦片、为什么缺、缺块是否已被解释。

    Returns: 200 {task_id, total, by_outcome, explained, decision, status, samples}

    `explained=true` 表示所有缺块都是 `no_data`（上游明确说这里没有影像，
    典型是海洋与两极）—— 那不是失败，重试一万次结果一样，UI 应当直接放行而不是
    劝用户补漏。samples 最多 20 条，够定位问题又不会把响应撑大。
    """
    try:
        if not task_manager:
            return jsonify({'error': 'Task manager not initialized'}), 500
        if not _task_exists('map', task_id):
            return jsonify({'error': t('api.gaps.not_found')}), 404
        return jsonify({'success': True, **task_manager.gap_summary(task_id)})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Error getting gaps for task {task_id}: {e}")
        return jsonify({'error': 'Failed to get task gaps'}), 500


@api_bp.route('/tasks/<int:task_id>/refill', methods=['POST'])
def refill_task_route(task_id: int):
    """只重跑记录在案的缺块里**值得重试**的那些（RETRYABLE_OUTCOMES）。

    `no_data` 与 `permanent_failure` 不在其中：前者上游说过没有，后者是 4xx，
    再问一遍只是浪费配额和用户的时间。一条都不值得重试时回 400 —— 让按钮点下去
    转一圈再回来说「补完了，还是缺 837 块」是最糟的交互。
    """
    try:
        if not task_manager:
            return jsonify({'error': 'Task manager not initialized'}), 500
        if not _task_exists('map', task_id):
            return jsonify({'error': t('api.gaps.not_found')}), 404
        task_manager.refill_task(task_id)
        return jsonify({'success': True, 'message': f'Task {task_id} refilling'})
    except ValueError as e:
        # 两种情形：当前状态不允许补漏，或者一块可重试的都没有。原文是英文，
        # 界面要的是译文；真实原因进日志。
        logger.info(f"Refill refused for task {task_id}: {e}")
        return jsonify({'error': t('api.gaps.not_allowed')}), 400
    except Exception as e:
        logger.error(f"Error refilling task {task_id}: {e}")
        return jsonify({'error': 'Failed to refill task'}), 500


@api_bp.route('/tasks/<int:task_id>/accept_gaps', methods=['POST'])
def accept_task_gaps(task_id: int):
    """用户看过缺块清单后决定「就这样」：pending_decision → completed_with_gaps。

    这不只是改个状态：严格模式下拒绝执行的拼接/复制阶段会在这里补跑，所以
    「接受缺块」的产物和「完全成功」的产物是同一种东西，只是带缺块标记
    （标记落在 artifacts.has_gaps 上，跟着产物走而不是跟着任务状态走 ——
    任务可以被删，产物可以被保留）。
    """
    try:
        if not task_manager:
            return jsonify({'error': 'Task manager not initialized'}), 500
        if not _task_exists('map', task_id):
            return jsonify({'error': t('api.gaps.not_found')}), 404
        return jsonify({'success': True, **task_manager.accept_gaps(task_id)})
    except ValueError as e:
        logger.info(f"Accept-gaps refused for task {task_id}: {e}")
        return jsonify({'error': t('api.gaps.not_allowed')}), 400
    except Exception as e:
        logger.error(f"Error accepting gaps for task {task_id}: {e}")
        return jsonify({'error': 'Failed to accept task gaps'}), 500


# ---------------------------------------------------------------------------
# 产物：导出容器与登记查询
# ---------------------------------------------------------------------------


# MBTiles 是宿主自带的唯一容器；其余格式由插件导出器提供（`Exporter` 协议）。
# 写成函数而不是模块级元组：插件可以在运行期被启停/重扫，冻一份常量就等于让
# 400 的 `supported_formats` 显示上一轮的世界。
_HOST_EXPORT_FORMAT = 'mbtiles'


def _export_formats() -> tuple:
    """这一刻能用的导出格式。插件导出器**并进这条路由**而不是自开一条：

    §5.3 禁止按数据类型各开一条导出路由，而插件导出器的真实消费者恰恰是核心
    管线的产物 —— in-tree 的 `GpkgExporter.accepts()` 只收 `GEOTIFF`，产出
    GeoTIFF 的是 map/dem 两条核心管线，插件任务一件都不产。原先那条
    `POST /api/plugins/export/<tid>` 只认插件任务，等于把导出器接在一个永远
    没有货的入口上。
    """
    from src.plugins import registry as plugin_registry
    return (_HOST_EXPORT_FORMAT,) + tuple(
        f for f in plugin_registry.list_export_formats()
        if f != _HOST_EXPORT_FORMAT)


# 能打成 MBTiles 的管线。**有意**引用 artifact_export 的私有表：那张表是
# 「哪条管线有松散瓦片金字塔」的唯一事实来源，在路由层照抄一份
# ('map', 'contour') 的代价很具体 —— 那边加一条管线时这里不会报错，只会静默
# 地把新管线拒在 400 上。本仓库对这种耦合有先例并写明了同样的理由
# （task_logging 引 logging_setup._ANSI_ESCAPE）。
# 插件格式**不看这张表**：它们吃的是 `artifacts` 登记行，不是瓦片目录。
_EXPORTABLE_PIPELINES = tuple(artifact_export._PIPELINE_TILE_LAYOUT)


def _export_dest(source, fmt: str):
    """导出目标路径：源产物的同级，加上目标格式的后缀。

    **不能直接 `with_suffix`**，它替换的是「最后一个点之后的东西」，而产物名里
    的点未必是扩展名：
    - `城区 2024.06`（按月份命名的成果目录）→ `城区 2024.gpkg`，月份没了，
      同一年的两个月份导出**互相覆盖**；
    - `dem_v1.5` → `dem_v1.gpkg`，版本号没了，同上；
    - 源本身就是 `.gpkg` 而目标也是 gpkg → 目标**等于源**，导出器写在自己的
      输入上。

    所以只在「现有后缀正是生产者自己声明的那个格式」时才替换 —— 那种情况下它
    确实是扩展名（`a.tif` + `fmt='tif'` → `a.gpkg`）—— 其余一律追加，宁可
    `城区 2024.06.gpkg` 有点长，也不许丢名字或撞源文件。声明格式与目标格式相同
    时也追加，否则又回到「目标等于源」。
    """
    src = Path(source.path)
    declared = f'.{source.fmt.lower()}' if source.fmt else ''
    if declared and declared != f'.{fmt}' and src.suffix.lower() == declared:
        return src.with_suffix(f'.{fmt}')
    return src.with_name(f'{src.name}.{fmt}')


def _export_via_plugin(pipeline: str, task_id: int, fmt: str):
    """插件导出器分支。返回 `(payload, status)`。

    产物登记走 `artifact_store.record_plugin_artifact` —— 与
    `TaskContext.register_artifact` 同一道归属校验，并强制
    `pipeline`/`task_id` 用宿主这边的取值（理由见那个函数的 docstring）。
    归属根取宿主自己算出来的 `dest.parent`：目标路径是宿主定的，导出器把文件
    写到别处（`~/.ssh/id_rsa`）再登记，就是一条「用户删任务时宿主替它删」的
    路径。
    """
    from src.plugins import registry as plugin_registry
    from src.plugins.protocols import ExportContext

    exporter = plugin_registry.exporter_for(fmt)
    if exporter is None:
        # 走到这里说明插件在「列格式」与「取导出器」之间被禁用了。
        return {'error': t('api.export.unsupported_format'),
                'supported_formats': list(_export_formats())}, 400
    candidates = [a for a in artifact_store.list_artifacts(pipeline, task_id)
                  if exporter.accepts(a.kind)]
    if not candidates:
        return {'error': t('api.export.no_tiles')}, 400
    source = candidates[0]
    dest = _export_dest(source, fmt)
    ctx = ExportContext(
        task_id=task_id,
        log=lambda msg, level='info': logger.info('[export:%s] %s', fmt, msg),
        progress=lambda done, total: None)
    try:
        result = exporter.export(source, dest, ctx)
    except Exception as e:
        logger.exception('插件导出失败（%s/%s → %s）', pipeline, task_id, fmt)
        return {'error': str(e)}, 500
    try:
        artifact_store.record_plugin_artifact(
            result, pipeline=pipeline, task_id=task_id,
            output_root=dest.parent)
    except ValueError as e:
        logger.error('插件导出器登记了越界产物（%s）：%s', fmt, e)
        return {'error': str(e)}, 500
    return {'success': True, 'path': str(dest), 'format': fmt,
            'pipeline': pipeline, 'task_id': task_id}, 200


@api_bp.route('/export/<pipeline>/<int:task_id>', methods=['POST'])
def export_task(pipeline: str, task_id: int):
    """把一个任务的产物导出成单文件容器。Body: `{"format": "mbtiles"}`。

    Returns: 200 {path, tile_count, minzoom, maxzoom, bytes, bounds, has_gaps,
                  validation, pipeline, task_id}（mbtiles）
             200 {path, format, pipeline, task_id}（插件导出器）

    **一条路由服务全部管线与全部格式**，与 `/mbtiles/<pipeline>/...` 那条读取
    路由同一个原则（§5.3 明确禁止按数据类型各开一条）。管线之间的差异全部收在
    `artifact_export._PIPELINE_TILE_LAYOUT` 那一张表里，格式之间的差异收在
    插件注册表里，这里只做校验和分派。

    导出是**追加**不是替代：XYZ 目录原样留着。它同时是 `/mbtiles/` 读取路由的
    数据源 —— 一个几十万文件的目录在 Windows 上光是拷贝就要几小时，单文件则可以
    直接拖给别人，这才是导出的用途。
    """
    # 闸的顺序是有讲究的：都是**URL 与 body 自身**的性质，一次 DB 都不用查，
    # 所以全部排在存在性检查之前。反过来（先查行）会让 POST /api/export/dem/7
    # 在没有 7 号 DEM 任务时回 404，用户于是去找一个并不存在的记录，而真正的
    # 问题是 DEM 管线**根本没有**可打包的瓦片金字塔。
    if pipeline not in PIPELINES:
        # 拼错的管线名。清单作为独立字段回给前端，不进译文（事实来源是
        # contracts.artifact.PIPELINES，塞进 zh+en 就成了三份）。
        return jsonify({'error': t('api.export.unsupported_pipeline'),
                        'supported_pipelines': list(PIPELINES)}), 400

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        # 数组/字符串体是合法 JSON 且为真，但没有 .get —— 口径与同文件的
        # verify_tile_url / inspect_raster_headers 一致。
        payload = {}
    formats = _export_formats()
    fmt = str(payload.get('format') or _HOST_EXPORT_FORMAT).strip().lower()
    if fmt not in formats:
        return jsonify({'error': t('api.export.unsupported_format'),
                        'supported_formats': list(formats)}), 400
    # 管线闸只对 MBTiles 生效，且必须排在格式闸**之后**：dem 没有松散瓦片目录
    # 但有 GeoTIFF 产物，`format=gpkg` 在那条管线上完全合法。
    if fmt == _HOST_EXPORT_FORMAT and pipeline not in _EXPORTABLE_PIPELINES:
        # 管线名对，但这条管线没有松散瓦片目录（dem 下的是颗粒 GeoTIFF，
        # local_terrain 下的是 quantized-mesh，都不是 MBTiles 能装的东西）。
        return jsonify({
            'error': t('api.export.unsupported_pipeline'),
            'supported_pipelines': list(_EXPORTABLE_PIPELINES),
        }), 400

    try:
        if not _task_exists(pipeline, task_id):
            return jsonify({'error': t('api.gaps.not_found')}), 404
    except Exception as e:
        logger.error(f"Error checking {pipeline} task {task_id} before export: {e}")
        return jsonify({'error': 'Failed to export task'}), 500

    if fmt != _HOST_EXPORT_FORMAT:
        body, status = _export_via_plugin(pipeline, task_id, fmt)
        return jsonify(body), status

    # **不传 has_gaps**：那件事的判据是任务行（gap_tiles / 状态），住在
    # artifact_export._infer_has_gaps 里，两个导出入口共用一份。这里曾经什么都
    # 不传而下游默认 False，结果是用户点按钮导出的容器被登记成「无缺块」，
    # 紧挨着同一任务写着 True 的 xyz_dir / geotiff 兄弟行 —— §13-3 要的
    # 「成果与历史永久带缺块标记」被这一件产物作废。
    try:
        result = artifact_export.export_task_mbtiles(pipeline, task_id)
    except artifact_export.ExportError as e:
        # 走到这里只剩一种情形：管线对、任务在，但目录里一块瓦片都没有
        # （任务从没跑过、产物被手工删了、或者 output_path 指向已卸载的盘）。
        # 管线不支持在上面就拦掉了，不必在这里再分一次支。
        logger.info(f"Export refused for {pipeline} task {task_id}: {e}")
        return jsonify({'error': t('api.export.no_tiles')}), 400
    except Exception as e:
        logger.error(f"Error exporting {pipeline} task {task_id}: {e}")
        return jsonify({'error': 'Failed to export task'}), 500

    # 新产物登记完成 —— 清掉 /mbtiles 的产物路径缓存。重复导出会覆盖同一个库，
    # 但任务改名后路径会变，缓存里那条旧路径会让新导出的库取不到。
    mbtiles_static.invalidate_known_task(task_id, pipeline)
    return jsonify({'success': True, **result})


@api_bp.route('/tasks/<int:task_id>/artifacts', methods=['GET'])
def get_task_artifacts(task_id: int):
    """这个任务产出了什么。Query: `pipeline`（默认 map）。

    Returns: 200 {pipeline, artifacts: [<Artifact.to_dict()>...]}

    产物行**可以比任务行活得久**（artifacts 表刻意没有外键，见
    contracts/artifact 的模块 docstring），所以这里不查任务存在性：删了任务但
    保留文件时，这条接口正是「那些文件在哪」的唯一答案。
    """
    pipeline = (request.args.get('pipeline') or 'map').strip()
    if pipeline not in PIPELINES:
        return jsonify({'error': t('api.logs.bad_pipeline'),
                        'supported_pipelines': list(PIPELINES)}), 400
    try:
        artifacts = artifact_store.list_artifacts(pipeline, task_id)
        return jsonify({'success': True, 'pipeline': pipeline,
                        'artifacts': [a.to_dict() for a in artifacts]})
    except Exception as e:
        logger.error(f"Error listing artifacts for {pipeline} task {task_id}: {e}")
        return jsonify({'error': 'Failed to list task artifacts'}), 500


# ---------------------------------------------------------------------------
# 每任务日志与诊断包（§4.5）
# ---------------------------------------------------------------------------


def _bad_pipeline_response():
    """`<pipeline>` 不在 contracts.artifact.PIPELINES 里时的统一 400。

    路径段直接进文件名（`logs/tasks/<pipeline>_<id>.log`），白名单是硬要求而不是
    礼貌 —— 没有它，`../../` 这类段会把读取指到任意文件。task_logging 内部也有
    一道（`_LOG_NAME_RE`），两道都留着：这一道给出可翻译的报错，那一道兜底。
    """
    return jsonify({'error': t('api.logs.bad_pipeline'),
                    'supported_pipelines': list(PIPELINES)}), 400


@api_bp.route('/logs/<pipeline>/<int:task_id>', methods=['GET'])
def get_task_log(pipeline: str, task_id: int):
    """一个任务的日志尾部。Query: `limit`（默认 500）、`errors_only`。

    Returns: 200 {pipeline, task_id, entries: [{ts, level, message}...]}

    **刻意用轮询 REST 而不是 socket 推送。** 这个应用没有 room / namespace，
    每一次 emit 都发给所有连着的客户端 —— 逐行推送日志等于把一个任务的日志广播
    给所有打开着页面的浏览器，其中绝大多数根本没在看这个任务。

    `errors_only` 包含 WARNING（见 task_logging._ERROR_LEVELS）：重试、429、
    无覆盖恰好都是 WARNING，而它们正是「为什么只下到一半」的答案。
    """
    if pipeline not in PIPELINES:
        return _bad_pipeline_response()

    raw_limit = request.args.get('limit')
    try:
        limit = int(raw_limit) if raw_limit else 500
    except (TypeError, ValueError):
        return jsonify({'error': 'limit must be an integer'}), 400
    # 上界挡住 ?limit=99999999：读取是反向按块读文件，limit 直接决定驻留内存的
    # 行数。5000 行已经远超「翻一下最近发生了什么」的用途。
    limit = max(1, min(limit, 5000))
    errors_only = (request.args.get('errors_only') or '').lower() in ('1', 'true', 'yes')

    try:
        entries = task_logging.read_task_log(
            pipeline, task_id, limit=limit, errors_only=errors_only)
    except Exception as e:
        logger.error(f"Error reading {pipeline} task log {task_id}: {e}")
        return jsonify({'error': 'Failed to read task log'}), 500

    # 空数组有三种来源，前端要说的话完全不同（「日志已关闭」/「暂无日志」/
    # 「这一段没有错误」），所以三种都用独立字段区分，不让它去猜：
    #   enabled=false  → task_log_enabled 关着，这个任务从一开始就没在写。
    #   has_log=false  → 开着但文件不存在：任务还没跑过。
    #   两者都真而 entries 为空 → 真的没有符合过滤条件的行。
    # `_bool_config` 是**有意**引用的私有名：布尔配置在库里存的是 'true'/'false'
    # 字面量，在这里手写一遍 `== 'true'` 就是第二份解析规则，而两份解析规则漂移
    # 时的表现是「配置页显示已开启、日志面板说已关闭」。同一个文件自己也这么
    # 干过一次（它 import 了 logging_setup._ANSI_ESCAPE，理由逐字相同）。
    enabled = task_logging._bool_config('task_log_enabled', config_manager)
    has_log = task_logging.task_log_path(pipeline, task_id).exists()
    return jsonify({'success': True, 'pipeline': pipeline, 'task_id': task_id,
                    'errors_only': errors_only, 'enabled': enabled,
                    'has_log': has_log, 'entries': entries})


@api_bp.route('/logs/<pipeline>/<int:task_id>/diagnostics', methods=['GET'])
def get_task_diagnostics(pipeline: str, task_id: int):
    """脱敏的纯文本诊断包，按附件下发（§4.5 的「导出脱敏诊断包」）。

    返回 text/plain 而不是 JSON：这个东西的用途是**贴进 issue**。JSON 包一层
    就要求用户先解转义，而里面本来就是给人读的多行文本。

    脱敏在 task_logging.diagnostics_text 里做（凭据、URL userinfo、家目录路径）。
    路由层不做二次加工 —— 加工一次就有漏一类的机会。
    """
    if pipeline not in PIPELINES:
        return _bad_pipeline_response()
    try:
        text = task_logging.diagnostics_text(pipeline, task_id)
    except Exception as e:
        logger.error(f"Error building diagnostics for {pipeline} {task_id}: {e}")
        return jsonify({'error': 'Failed to build diagnostics'}), 500

    response = Response(text, mimetype='text/plain; charset=utf-8')
    # attachment 而不是 inline：浏览器直接显示一份几百 KB 的纯文本没有意义，
    # 用户要的是一个能拖进 issue 或邮件的文件。文件名只由白名单过的 pipeline
    # 与整数 task_id 拼成，没有用户输入进去，不需要额外转义。
    response.headers['Content-Disposition'] = (
        f'attachment; filename="{pipeline}_{task_id}_diagnostics.txt"')
    return response


# ---------------------------------------------------------------------------
# 瓦片源向导与缓存治理
# ---------------------------------------------------------------------------


@api_bp.route('/config/analyze_tile_url', methods=['POST'])
def analyze_tile_url_route():
    """一条真实瓦片 URL → `{z}/{x}/{y}` 模板 + 检测结果 + 警告。Body: {"url": "..."}

    用户手上有的从来是「在某个网站上右键复制的一张瓦片地址」，不是模板。让他
    自己把 `/12/3413/1663.png` 改成 `/{z}/{x}/{y}.png` 是这个表单最大的一处
    出错点：改错一位不会报错，只会静默下到错误的位置。

    警告里包含「URL 里带着看起来像密钥的参数」—— 它会**原样**存进 url_template
    并进任务快照，用户有权在保存之前知道。
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    url = (payload.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'url is required'}), 400

    try:
        return jsonify({'success': True, **source_wizard.analyze_tile_url(url)})
    except ValueError as e:
        # TemplateDetectionError 也是 ValueError。它的原文是**这个模块里最有用的
        # 一段文字**（「我在 URL 里看到了这些整数，但没有一组能同时满足
        # x,y < 2^z」），原样回给用户，不换成一句笼统的译文。
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Error analyzing tile url: {e}")
        return jsonify({'error': 'Failed to analyze tile URL'}), 500


@api_bp.route('/cache/namespaces', methods=['GET'])
def cache_namespaces():
    """按**源命名空间**看缓存占用：谁在用、多大、此刻是否有活动任务引用。

    与 GET /api/cache/stats 的区别：那条按 cache 顶层目录粗分（含 dem 与散落
    文件），这条只看瓦片命名空间，但多回 `tasks` 与 `active` —— 用户要判断
    「这一坨能不能删」，光看大小是判不了的。
    """
    try:
        return jsonify({'success': True,
                        'namespaces': cache_exclusive.cache_usage_by_namespace()})
    except Exception as e:
        logger.error(f"Error listing cache namespaces: {e}")
        return jsonify({'error': 'Failed to list cache namespaces'}), 500


@api_bp.route('/cache/sweep_orphans', methods=['POST'])
def sweep_orphan_cache_route():
    """删掉**没有任何存活任务引用**的命名空间目录。

    判据是「没有任何任务引用」而不是「没有活动任务引用」：已完成任务的缓存仍然
    有价值（同区域再下一次就是全命中）。真正的孤儿只来自「换过源、旧任务已经
    被删干净」—— 换源会改指纹、改命名空间，旧目录从此再也不会被命中，放着就是
    几十 GB 静默失效。

    因此这条接口**不需要** force：它删的东西按定义没有任何任务在用。
    """
    try:
        result = cache_exclusive.sweep_orphan_cache()
    except Exception as e:
        logger.error(f"Error sweeping orphan cache: {e}")
        return jsonify({'error': 'Failed to sweep orphan cache'}), 500

    # 数字走字段不走译文：`api.cache.sweep_done` 是一句不带占位符的完成语，
    # 具体删了几个、多少字节由前端拼 —— 事实来源是这里的返回值，塞进 zh+en
    # 两条译文就成了三份。
    return jsonify({
        'success': True,
        'message': t('api.cache.sweep_done'),
        'removed': result['removed'],
        'removed_bytes': result['removed_bytes'],
    })
