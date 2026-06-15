"""
API routes

Handles RESTful API endpoints for task management, history, and configuration.
"""

import logging
from flask import Blueprint, request, jsonify
from typing import Optional
from database import get_connection
from services.config_manager import ConfigManager

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
    logger.info("Task manager initialized in API routes")


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

        # Get request data
        data = request.get_json()

        # Validate required fields
        required_fields = ['name', 'north', 'south', 'east', 'west',
                          'zoom_min', 'zoom_max', 'style', 'output_format', 'output_path']

        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            return jsonify({
                'error': f'Missing required fields: {", ".join(missing_fields)}'
            }), 400

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

        # Cap limit at 100
        if limit > 100:
            limit = 100

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

    except Exception as e:
        logger.error(f"Error cancelling task {task_id}: {e}")
        return jsonify({'error': 'Failed to cancel task'}), 500


@api_bp.route('/tasks/<int:task_id>', methods=['DELETE'])
def delete_task(task_id: int):
    """
    Delete a task and its associated tiles

    Args:
        task_id: Task ID to delete

    Returns:
        JSON response with success status
    """
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()

            # Check if task exists
            cursor.execute('SELECT id, status FROM tasks WHERE id = ?', (task_id,))
            row = cursor.fetchone()

            if not row:
                return jsonify({'error': f'Task {task_id} not found'}), 404

            # Prevent deletion of running tasks
            if row['status'] == 'running':
                return jsonify({
                    'error': 'Cannot delete running task. Please pause or cancel it first.'
                }), 400

            # Delete task (tiles will be deleted via CASCADE)
            cursor.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
            conn.commit()

            logger.info(f"Task {task_id} deleted via API")

            return jsonify({
                'success': True,
                'message': f'Task {task_id} deleted'
            })

        finally:
            conn.close()

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
    Get combined history for map tasks and DEM tasks with pagination.

    Returns a normalized task list with task_type in {'map','dem'}.
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

        conn = get_connection()
        try:
            cursor = conn.cursor()

            if status_filter:
                cursor.execute('SELECT COUNT(*) AS c FROM tasks WHERE status = ?', (status_filter,))
                map_count = cursor.fetchone()['c']
                cursor.execute('SELECT COUNT(*) AS c FROM dem_tasks WHERE status = ?', (status_filter,))
                dem_count = cursor.fetchone()['c']
                cursor.execute('SELECT COUNT(*) AS c FROM local_terrain_tasks WHERE status = ?', (status_filter,))
                local_count = cursor.fetchone()['c']
            else:
                cursor.execute('SELECT COUNT(*) AS c FROM tasks')
                map_count = cursor.fetchone()['c']
                cursor.execute('SELECT COUNT(*) AS c FROM dem_tasks')
                dem_count = cursor.fetchone()['c']
                cursor.execute('SELECT COUNT(*) AS c FROM local_terrain_tasks')
                local_count = cursor.fetchone()['c']

            total_count = int(map_count or 0) + int(dem_count or 0) + int(local_count or 0)

            params = []
            where_map = ""
            where_dem = ""
            where_local = ""
            if status_filter:
                where_map = "WHERE status = ?"
                where_dem = "WHERE status = ?"
                where_local = "WHERE status = ?"
                params.extend([status_filter, status_filter, status_filter])

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
                    COALESCE(completed_at, created_at) AS sort_key
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
                    COALESCE(completed_at, created_at) AS sort_key
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
                    COALESCE(completed_at, created_at) AS sort_key
                FROM local_terrain_tasks
                {where_local}
                ORDER BY sort_key DESC
                LIMIT ? OFFSET ?
            '''

            params.extend([per_page, offset])
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            tasks = []
            for r in rows:
                d = dict(r)
                d.pop('sort_key', None)
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
    """Aggregate task counts and download totals across all three task tables."""
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()

            def _counts(table):
                cursor.execute(
                    f"SELECT COUNT(*) AS total, "
                    f"SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed, "
                    f"SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
                    f"FROM {table}"
                )
                row = cursor.fetchone()
                return (int(row['total'] or 0), int(row['completed'] or 0), int(row['failed'] or 0))

            def _sum(table, col):
                cursor.execute(f"SELECT COALESCE(SUM({col}), 0) AS s FROM {table}")
                return int(cursor.fetchone()['s'] or 0)

            m_total, m_done, m_fail = _counts('tasks')
            d_total, d_done, d_fail = _counts('dem_tasks')
            l_total, l_done, l_fail = _counts('local_terrain_tasks')

            total_downloaded = (
                _sum('tasks', 'downloaded_tiles')
                + _sum('dem_tasks', 'downloaded_files')
                + _sum('local_terrain_tasks', 'uploaded_files')
            )

            resp = jsonify({
                'success': True,
                'stats': {
                    'total_tasks': m_total + d_total + l_total,
                    'completed': m_done + d_done + l_done,
                    'failed': m_fail + d_fail + l_fail,
                    'total_downloaded': total_downloaded,
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
        data = request.get_json()

        if not data:
            return jsonify({'error': 'No configuration data provided'}), 400

        # Update each configuration key
        updated_keys = []
        errors = []

        for key, value in data.items():
            try:
                config_manager.set(key, str(value))
                updated_keys.append(key)
            except ValueError as e:
                errors.append(f"{key}: {str(e)}")
            except Exception as e:
                logger.error(f"Error updating config {key}: {e}")
                errors.append(f"{key}: Failed to update")

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
