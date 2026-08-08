"""
API routes

Handles RESTful API endpoints for task management, history, and configuration.
"""

import logging
import os
from flask import Blueprint, request, jsonify
from pathlib import Path
from typing import Optional
from src.core.database import get_connection, DEFAULT_CONFIGS
from src.i18n import t
from src.services.config_manager import ConfigManager
from src.services.task_cleanup import resolve_stored_output_dir
from src.routes import tiles_static

logger = logging.getLogger(__name__)

# Create API blueprint with /api prefix
api_bp = Blueprint('api', __name__, url_prefix='/api')

# Global task manager instance (injected via init_task_manager)
task_manager = None

# Initialize config manager
config_manager = ConfigManager()


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
            "output_path": "./downloads"
        }

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


@api_bp.route('/tasks/<int:task_id>/cancel', methods=['POST'])
def cancel_task(task_id: int):
    """
    Cancel a task

    Args:
        task_id: Task ID to cancel

    Returns:
        JSON response with success status
    """
    try:
        if not task_manager:
            return jsonify({'error': 'Task manager not initialized'}), 500

        task_manager.cancel_task(task_id)

        logger.info(f"Task {task_id} cancelled via API")

        return jsonify({
            'success': True,
            'message': f'Task {task_id} cancelled'
        })

    except ValueError as e:
        logger.error(f"Error cancelling task {task_id}: {e}")
        return jsonify({'error': str(e)}), 400

    except Exception as e:
        logger.error(f"Error cancelling task {task_id}: {e}")
        return jsonify({'error': 'Failed to cancel task'}), 500


@api_bp.route('/tasks/<int:task_id>', methods=['DELETE'])
def delete_task(task_id: int):
    """
    Delete a task and its associated tiles

    Query Parameters:
        delete_files: Optional (1/true/yes). Also remove the task's on-disk
            artifact directory (output_path/task_<id>). Defaults to false.
            删除边界见 services/task_cleanup.remove_task_dir_if_safe。

    正在运行的任务**可以**直接删：删除会自己置停止标志、当场删行、把产物清理
    交给后台线程（见 services/task_deletion）。响应里的 files_deferred=true
    表示产物还没删完 —— 后台在等工作线程收工。
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

        artifact_dir = None
        if request.args.get('delete_files', '').lower() in ('1', 'true', 'yes'):
            # 存量行的 output_path 可能是相对路径(旧版本只校验不改写)——先归一化
            # 成绝对路径;否则 Path.resolve() 按进程 CWD 解析,CWD≠BASE_DIR 时会
            # 误判成"越界"而拒删,接口却已经返回 success。
            artifact_dir = resolve_stored_output_dir(row['output_path']) / f"task_{task_id}"

        outcome = task_manager.delete_task(
            task_id,
            artifact_dir=artifact_dir,
            # 行删掉后同步清 /tiles 静态路由的 output_path 缓存，否则
            # delete_files=false（磁盘瓦片保留）时已删任务的瓦片仍能被访问到。
            # hook 留在路由层：它走 current_app.extensions，只在请求上下文里有效。
            on_row_gone=lambda: tiles_static.invalidate_output_path_cache(task_id),
        )
        if not outcome.row_deleted:
            return jsonify({'error': f'Task {task_id} not found'}), 404

        logger.info(f"Task {task_id} deleted via API")
        return jsonify(_delete_payload(
            f'Task {task_id} deleted', outcome.files_removed,
            files_deferred=outcome.files_deferred))

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
    Get combined history for all four task tables with pagination.

    Returns a normalized task list with task_type in
    {'map','dem','local_terrain','contour'}, ordered strictly by
    created_at DESC (single time stream). ?status= filters by a single
    status value; the special value 'active' means
    status IN ('pending','running','paused').
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

        # 2026-08 单一时间流定稿：?status=active 是特殊值，表示「进行中」
        # （pending/running/paused 三态）——记录面板不再有独立的活动分区，
        # 活动任务也在时间流里，靠这个筛选值把它们单独滤出来。
        # 其它取值维持原来的单值等值语义。
        active_clause = "status IN ('pending','running','paused')"
        if status_filter == 'active':
            where_sql = f'WHERE {active_clause}'
            count_params = ()
        else:
            where_sql = 'WHERE status = ?' if status_filter else ''
            count_params = (status_filter,) if status_filter else ()

        conn = get_connection()
        try:
            cursor = conn.cursor()

            # 4 个相互独立的 COUNT 合并成一条标量子查询 SQL:同连接下每次
            # execute 仍是一次完整的解析+执行往返,合一后只跑一趟。
            # where_sql/count_params 四表相同,参数按出现顺序重复 4 份。
            cursor.execute(
                f'SELECT '
                f'(SELECT COUNT(*) FROM tasks {where_sql}) AS map_c, '
                f'(SELECT COUNT(*) FROM dem_tasks {where_sql}) AS dem_c, '
                f'(SELECT COUNT(*) FROM local_terrain_tasks {where_sql}) AS local_c, '
                f'(SELECT COUNT(*) FROM contour_tasks {where_sql}) AS contour_c',
                count_params * 4,
            )
            row = cursor.fetchone()
            map_count = row['map_c']
            dem_count = row['dem_c']
            local_count = row['local_c']
            contour_count = row['contour_c']

            total_count = int(map_count or 0) + int(dem_count or 0) + int(local_count or 0) + int(contour_count or 0)

            params = []
            where_map = where_sql
            where_dem = where_sql
            where_local = where_sql
            where_contour = where_sql
            if status_filter and status_filter != 'active':
                params.extend([status_filter, status_filter, status_filter, status_filter])

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
                    total_running_seconds
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
                    NULL AS total_running_seconds
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
                    NULL AS total_running_seconds
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
                    NULL AS total_running_seconds
                FROM contour_tasks
                {where_contour}
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
    """Aggregate task counts and download totals across all four task tables."""
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()

            # 原来 4 条 COUNT + 4 条 SUM 串行执行，每次 execute 都是一次完整的
            # 解析+执行往返。合并成 1 条：4 个聚合子查询交叉连接（无 GROUP BY 的
            # 聚合在空表上也恒返回一行），每表仍只扫一遍，总共只有一次往返
            # （思路同 history_all 已合并的 4 路 COUNT）。
            cursor.execute('''
                SELECT
                    m.t AS m_total, m.c AS m_done, m.f AS m_fail, m.s AS m_sum,
                    d.t AS d_total, d.c AS d_done, d.f AS d_fail, d.s AS d_sum,
                    l.t AS l_total, l.c AS l_done, l.f AS l_fail, l.s AS l_sum,
                    c.t AS c_total, c.c AS c_done, c.f AS c_fail, c.s AS c_sum
                FROM
                    (SELECT COUNT(*) AS t,
                            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS c,
                            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS f,
                            COALESCE(SUM(downloaded_tiles), 0) AS s
                     FROM tasks) m,
                    (SELECT COUNT(*) AS t,
                            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS c,
                            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS f,
                            COALESCE(SUM(downloaded_files), 0) AS s
                     FROM dem_tasks) d,
                    (SELECT COUNT(*) AS t,
                            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS c,
                            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS f,
                            COALESCE(SUM(uploaded_files), 0) AS s
                     FROM local_terrain_tasks) l,
                    (SELECT COUNT(*) AS t,
                            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS c,
                            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS f,
                            COALESCE(SUM(rendered_tiles), 0) AS s
                     FROM contour_tasks) c
            ''')
            row = cursor.fetchone()

            resp = jsonify({
                'success': True,
                'stats': {
                    'total_tasks': int(row['m_total'] or 0) + int(row['d_total'] or 0)
                                   + int(row['l_total'] or 0) + int(row['c_total'] or 0),
                    'completed': int(row['m_done'] or 0) + int(row['d_done'] or 0)
                                 + int(row['l_done'] or 0) + int(row['c_done'] or 0),
                    'failed': int(row['m_fail'] or 0) + int(row['d_fail'] or 0)
                              + int(row['l_fail'] or 0) + int(row['c_fail'] or 0),
                    'total_downloaded': int(row['m_sum'] or 0) + int(row['d_sum'] or 0)
                                        + int(row['l_sum'] or 0) + int(row['c_sum'] or 0),
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

    Returns:
        JSON response with all configuration key-value pairs
    """
    try:
        config = config_manager.get_all()

        return jsonify({
            'success': True,
            'config': config
        })

    except Exception as e:
        logger.error(f"Error getting config: {e}")
        return jsonify({'error': 'Failed to get config'}), 500


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


def _delete_payload(message: str, files_removed, files_deferred: bool = False):
    """DELETE 端点的统一响应体（M10）。

    `remove_task_dir_if_safe` 用返回值区分「已删」与「越界拒删」，但四个调用点
    此前全部丢弃它 —— 任何护栏命中都只写一条 warning，HTTP 仍是
    200 {"success": true}。用户点了「删除并删文件」，看到成功提示，几十 GB
    产物却纹丝不动（存量相对 output_path 尤其容易命中，见 M10）。

    files_removed 为 None 表示调用方没要求删文件，响应里就不带这两个字段。

    files_deferred=True 是删除**正在运行**的任务时的新形态：行已经没了，但产物
    要等工作线程收工才能删（四条管线都有分钟级的 GDAL 阻塞区，见 task_deletion）。
    此时 files_removed 必然是 None —— 还没删，给不出真假。
    """
    payload = {'success': True, 'message': message}
    if files_deferred:
        payload['files_deferred'] = True
        return payload
    if files_removed is not None:
        payload['files_removed'] = bool(files_removed)
        if not files_removed:
            payload['files_message'] = t('api.tasks.files_kept_unsafe_dir')
    return payload


_ACTIVE_TASK_TABLES = (
    ('tasks', 'api.tasks.pipeline.map'),
    ('dem_tasks', 'api.tasks.pipeline.dem'),
    ('contour_tasks', 'api.tasks.pipeline.contour'),
    ('local_terrain_tasks', 'api.tasks.pipeline.local_terrain'),
)


def _unfinished_task_labels():
    """四条管线里所有未终结（pending/running/paused）任务的可读标签。

    M8：查 DB 而不是查四个 manager 的 active_tasks —— 本蓝图只持有 map 管线的
    manager 全局，查 DB 是唯一不需要额外注入就能覆盖四条管线的口径，且能连
    「进程重启后仍是 paused」的任务一起算进来。
    """
    from src.core.database import get_connection_context

    labels = []
    try:
        with get_connection_context() as conn:
            for table, label_key in _ACTIVE_TASK_TABLES:
                try:
                    rows = conn.execute(
                        f"SELECT id FROM {table} "
                        f"WHERE status IN ('pending','running','paused') ORDER BY id"
                    ).fetchall()
                except Exception:
                    continue  # 表不存在（旧库）时跳过，不阻断清理
                label = t(label_key)
                labels.extend(f"{label} #{row['id']}" for row in rows)
    except Exception as e:
        logger.warning(f"Failed to check unfinished tasks before cache clear: {e}")
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
            result = clear_cache_category(key)
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
        auto_detect_enabled, autodetect, get_state, mask_url_userinfo,
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
        'manual': mask_url_userinfo(manual) if manual else '',
        'auto_enabled': auto_enabled,
        'effective': mask_url_userinfo(effective) if effective else '',
        'effective_source': ('manual' if manual
                             else (state['source'] if effective else '')),
    })
    return jsonify(state)
