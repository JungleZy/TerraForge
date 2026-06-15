"""
Task Manager Service

Manages download task lifecycle including creation, execution, pause/resume, and cancellation.
Coordinates between database, download engine, and WebSocket notifications.
"""

import logging
import threading
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime
from pathlib import Path

from database import get_connection
from models.task import Task, Tile
from services.download_engine import DownloadEngine
from services.config_manager import ConfigManager

logger = logging.getLogger(__name__)

# Style mapping from full names to Google Maps style codes
STYLE_MAP = {
    'roadmap': 'm',      # Standard roadmap
    'satellite': 's',    # Satellite imagery
    'hybrid': 'y',       # Hybrid (satellite + labels)
    'terrain': 't'       # Terrain map
}


class TaskManager:
    """
    Task manager for orchestrating map download tasks

    Manages task lifecycle from creation through execution to completion.
    Provides pause/resume/cancel capabilities with real-time progress updates.

    Features:
        - Task creation with tile calculation
        - Background task execution with threading
        - Pause/resume/cancel controls with stop flags
        - Real-time progress updates via WebSocket
        - Database persistence for task state
        - GDAL tile stitching for image output
    """

    def __init__(self, socketio=None):
        """
        Initialize task manager

        Args:
            socketio: Flask-SocketIO instance for real-time updates (optional)
        """
        self.socketio = socketio
        self.download_engine = DownloadEngine()
        self.config_manager = ConfigManager()

        # Track active tasks and their stop flags
        self.active_tasks: Dict[int, threading.Thread] = {}
        self.stop_flags: Dict[int, threading.Event] = {}
        self._state_lock = threading.Lock()

        # Any task still marked 'running' in the DB at this point must be an
        # orphan from a previous process — no thread can have survived a restart.
        # Demote them so the UI stops reporting them as live, and so their
        # accumulated running time doesn't keep ticking against wall-clock.
        self._recover_orphan_running_tasks()

        logger.info("TaskManager initialized")

    def _recover_orphan_running_tasks(self) -> None:
        """Mark any 'running' task as 'paused' on startup.

        At __init__ time self.active_tasks is empty, so any tasks.status='running'
        row is guaranteed to be a leftover from a crashed/restarted process.
        We flip them to 'paused' (the existing pause_task semantics) and append a
        'pause' time record so _update_total_running_time on a future resume
        doesn't fold the dead-process gap into total_running_seconds.
        We deliberately do NOT call _update_total_running_time here: we cannot
        know when the process actually died, so adding wall-clock since the last
        'start' would overstate the runtime.
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM tasks WHERE status = 'running'")
            orphan_ids = [row['id'] for row in cursor.fetchall()]
            if not orphan_ids:
                return

            now = datetime.now()
            cursor.executemany(
                "UPDATE tasks SET status = 'paused' WHERE id = ? AND status = 'running'",
                [(tid,) for tid in orphan_ids],
            )
            cursor.executemany(
                "INSERT INTO task_time_records (task_id, action, timestamp) VALUES (?, 'pause', ?)",
                [(tid, now) for tid in orphan_ids],
            )
            conn.commit()
            logger.warning(
                f"Recovered {len(orphan_ids)} orphan 'running' task(s) to 'paused': {orphan_ids}"
            )
        except Exception as e:
            logger.error(f"Failed to recover orphan running tasks: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _is_stop_requested(self, task_id: int, stop_flag: Optional[threading.Event] = None) -> bool:
        flag = stop_flag or self.stop_flags.get(task_id)
        return bool(flag and flag.is_set())

    @staticmethod
    def _status_count_deltas(old_status: Optional[str], new_status: str) -> tuple[int, int]:
        downloaded_delta = int(new_status == 'completed') - int(old_status == 'completed')
        failed_delta = int(new_status == 'failed') - int(old_status == 'failed')
        return downloaded_delta, failed_delta

    def _record_time_action(self, task_id: int, action: str):
        """
        Record a time tracking action (start, pause, resume, complete)

        Args:
            task_id: Task ID
            action: Action type ('start', 'pause', 'resume', 'complete')
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO task_time_records (task_id, action, timestamp)
                VALUES (?, ?, ?)
            ''', (task_id, action, datetime.now()))
            conn.commit()
            logger.debug(f"Recorded time action '{action}' for task {task_id}")
        except Exception as e:
            logger.error(f"Failed to record time action for task {task_id}: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _update_total_running_time(self, task_id: int):
        """
        Update total_running_seconds by calculating time since last 'start' or 'resume'

        Args:
            task_id: Task ID
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()

            # Get the last start/resume record
            cursor.execute('''
                SELECT timestamp FROM task_time_records
                WHERE task_id = ? AND action IN ('start', 'resume')
                ORDER BY timestamp DESC
                LIMIT 1
            ''', (task_id,))

            row = cursor.fetchone()
            if row:
                last_start = datetime.fromisoformat(row['timestamp'])
                elapsed_seconds = int((datetime.now() - last_start).total_seconds())

                # Add to total running time
                cursor.execute('''
                    UPDATE tasks
                    SET total_running_seconds = total_running_seconds + ?
                    WHERE id = ?
                ''', (elapsed_seconds, task_id))

                conn.commit()
                logger.debug(f"Updated total running time for task {task_id}: +{elapsed_seconds}s")
        except Exception as e:
            logger.error(f"Failed to update total running time for task {task_id}: {e}")
            conn.rollback()
        finally:
            conn.close()

    def get_current_running_time(self, task_id: int) -> int:
        """
        Get current total running time in seconds

        Args:
            task_id: Task ID

        Returns:
            Total running time in seconds
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()

            # Get task info
            cursor.execute('''
                SELECT status, total_running_seconds FROM tasks WHERE id = ?
            ''', (task_id,))

            row = cursor.fetchone()
            if not row:
                return 0

            total_seconds = row['total_running_seconds'] or 0

            # If task is running, add current segment
            if row['status'] == 'running':
                cursor.execute('''
                    SELECT timestamp FROM task_time_records
                    WHERE task_id = ? AND action IN ('start', 'resume')
                    ORDER BY timestamp DESC
                    LIMIT 1
                ''', (task_id,))

                start_row = cursor.fetchone()
                if start_row:
                    last_start = datetime.fromisoformat(start_row['timestamp'])
                    current_segment = int((datetime.now() - last_start).total_seconds())
                    total_seconds += current_segment

            return total_seconds
        finally:
            conn.close()

    def create_task(self, params: dict) -> int:
        """
        Create a new download task in database

        Args:
            params: Task parameters dictionary containing:
                - name: Task name
                - north, south, east, west: Geographic bounds
                - zoom_min, zoom_max: Zoom level range
                - style: Map style (roadmap, satellite, hybrid, terrain)
                - output_format: Output format (png, jpg, both)
                - output_path: Output directory path

        Returns:
            Task ID of the created task

        Raises:
            ValueError: If task parameters are invalid
            sqlite3.Error: If database operation fails

        Process:
            1. Calculate tiles using download_engine
            2. Insert task into tasks table
            3. Insert all tiles into task_tiles table
            4. Return task_id
        """
        logger.info(f"Creating task: {params.get('name', 'Unnamed')}")

        # Create Task object for validation
        task = Task(
            name=params['name'],
            status='pending',
            north=float(params['north']),
            south=float(params['south']),
            east=float(params['east']),
            west=float(params['west']),
            zoom_min=int(params['zoom_min']),
            zoom_max=int(params['zoom_max']),
            style=params['style'],
            output_format=params['output_format'],
            output_path=params['output_path']
        )

        # Calculate tiles for the task
        tiles = self.download_engine.calculate_tiles(
            north=task.north,
            south=task.south,
            east=task.east,
            west=task.west,
            zoom_min=task.zoom_min,
            zoom_max=task.zoom_max,
            task_id=0  # Will be updated after task insertion
        )

        total_tiles = len(tiles)
        logger.info(f"Calculated {total_tiles} tiles for task")

        # Insert task into database
        conn = get_connection()
        try:
            cursor = conn.cursor()

            # Insert task
            cursor.execute('''
                INSERT INTO tasks (
                    name, status, north, south, east, west,
                    zoom_min, zoom_max, style, output_format, output_path,
                    total_tiles, downloaded_tiles, failed_tiles
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                task.name, task.status, task.north, task.south, task.east, task.west,
                task.zoom_min, task.zoom_max, task.style, task.output_format, task.output_path,
                total_tiles, 0, 0
            ))

            task_id = cursor.lastrowid
            logger.info(f"Task created with ID: {task_id}")

            # Update tile task_id and insert into database
            tile_data = [
                (task_id, tile.zoom, tile.x, tile.y, tile.status, tile.retry_count)
                for tile in tiles
            ]

            cursor.executemany('''
                INSERT INTO task_tiles (task_id, zoom, x, y, status, retry_count)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', tile_data)

            conn.commit()
            logger.info(f"Inserted {len(tile_data)} tiles for task {task_id}")

            return task_id

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to create task: {e}")
            raise
        finally:
            conn.close()

    def start_task(self, task_id: int):
        """
        Start a download task

        Args:
            task_id: Task ID to start

        Raises:
            ValueError: If task status is not pending or paused
            sqlite3.Error: If database operation fails
        """
        logger.info(f"Starting task {task_id}")

        conn = get_connection()
        try:
            cursor = conn.cursor()

            with self._state_lock:
                active_thread = self.active_tasks.get(task_id)
                if active_thread and active_thread.is_alive():
                    raise ValueError(f"Task {task_id} is already running")

                cursor.execute('SELECT status FROM tasks WHERE id = ?', (task_id,))
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"Task {task_id} not found")

                status = row['status']
                if status not in ['pending', 'paused']:
                    raise ValueError(
                        f"Cannot start task {task_id} with status '{status}'. "
                        f"Task must be 'pending' or 'paused'."
                    )

                cursor.execute('''
                    UPDATE tasks
                    SET status = 'running', started_at = ?
                    WHERE id = ? AND status IN ('pending', 'paused')
                ''', (datetime.now(), task_id))
                if cursor.rowcount != 1:
                    raise ValueError(f"Task {task_id} could not be started because its status changed")

                conn.commit()

                stop_flag = threading.Event()
                self.stop_flags[task_id] = stop_flag
                thread = threading.Thread(
                    target=self._run_task,
                    args=(task_id, stop_flag),
                    daemon=True,
                    name=f"Task-{task_id}"
                )
                self.active_tasks[task_id] = thread

            action = 'start' if status == 'pending' else 'resume'
            self._record_time_action(task_id, action)

            cursor.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
            task_row = cursor.fetchone()
            if task_row and self.socketio:
                total_running_seconds = self.get_current_running_time(task_id)
                self.socketio.emit('task_progress', {
                    'task_id': task_id,
                    'id': task_id,
                    'name': task_row['name'],
                    'status': task_row['status'],
                    'downloaded_tiles': task_row['downloaded_tiles'],
                    'failed_tiles': task_row['failed_tiles'],
                    'total_tiles': task_row['total_tiles'],
                    'north': task_row['north'],
                    'south': task_row['south'],
                    'east': task_row['east'],
                    'west': task_row['west'],
                    'zoom_min': task_row['zoom_min'],
                    'zoom_max': task_row['zoom_max'],
                    'style': task_row['style'],
                    'output_format': task_row['output_format'],
                    'output_path': task_row['output_path'],
                    'started_at': task_row['started_at'],
                    'created_at': task_row['created_at'],
                    'total_running_seconds': total_running_seconds
                })

            thread.start()
            logger.info(f"Task {task_id} started in background thread")

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to start task {task_id}: {e}")
            raise
        finally:
            conn.close()

    def pause_task(self, task_id: int):
        """
        Pause a running task

        Args:
            task_id: Task ID to pause

        Raises:
            ValueError: If task is not running
            sqlite3.Error: If database operation fails

        Process:
            1. Set stop_flag to signal task to stop
            2. Update status to 'paused' in database
            3. Emit status update via socketio
        """
        logger.info(f"Pausing task {task_id}")

        conn = get_connection()
        try:
            cursor = conn.cursor()

            cursor.execute('''
                UPDATE tasks
                SET status = 'paused'
                WHERE id = ? AND status = 'running'
            ''', (task_id,))

            if cursor.rowcount == 0:
                cursor.execute('SELECT status FROM tasks WHERE id = ?', (task_id,))
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"Task {task_id} not found")
                raise ValueError(f"Cannot pause task {task_id} with status '{row['status']}'")

            conn.commit()

            with self._state_lock:
                if task_id in self.stop_flags:
                    self.stop_flags[task_id].set()
                    logger.debug(f"Stop flag set for task {task_id}")

            self._update_total_running_time(task_id)
            self._record_time_action(task_id, 'pause')

            logger.info(f"Task {task_id} paused")

            # Get updated task info and emit via socketio
            cursor.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
            task_row = cursor.fetchone()

            if task_row and self.socketio:
                # Get current running time
                total_running_seconds = self.get_current_running_time(task_id)

                self.socketio.emit('task_progress', {
                    'task_id': task_id,
                    'id': task_id,
                    'name': task_row['name'],
                    'status': task_row['status'],
                    'downloaded_tiles': task_row['downloaded_tiles'],
                    'failed_tiles': task_row['failed_tiles'],
                    'total_tiles': task_row['total_tiles'],
                    'north': task_row['north'],
                    'south': task_row['south'],
                    'east': task_row['east'],
                    'west': task_row['west'],
                    'zoom_min': task_row['zoom_min'],
                    'zoom_max': task_row['zoom_max'],
                    'style': task_row['style'],
                    'output_format': task_row['output_format'],
                    'output_path': task_row['output_path'],
                    'started_at': task_row['started_at'],
                    'created_at': task_row['created_at'],
                    'total_running_seconds': total_running_seconds
                })

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to pause task {task_id}: {e}")
            raise
        finally:
            conn.close()

    def resume_task(self, task_id: int):
        """
        Resume a paused task

        Args:
            task_id: Task ID to resume

        Raises:
            ValueError: If task is not paused
            sqlite3.Error: If database operation fails

        Process:
            Simply calls start_task() which handles paused tasks
        """
        logger.info(f"Resuming task {task_id}")
        self.start_task(task_id)

    def cancel_task(self, task_id: int):
        """
        Cancel a task

        Args:
            task_id: Task ID to cancel

        Raises:
            sqlite3.Error: If database operation fails

        Process:
            1. Set stop_flag to signal task to stop
            2. Update status to 'cancelled' in database
        """
        logger.info(f"Cancelling task {task_id}")

        conn = get_connection()
        try:
            cursor = conn.cursor()

            cursor.execute('''
                UPDATE tasks
                SET status = 'cancelled'
                WHERE id = ? AND status != 'cancelled'
            ''', (task_id,))

            if cursor.rowcount == 0:
                cursor.execute('SELECT status FROM tasks WHERE id = ?', (task_id,))
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"Task {task_id} not found")

            conn.commit()

            with self._state_lock:
                if task_id in self.stop_flags:
                    self.stop_flags[task_id].set()
                    logger.debug(f"Stop flag set for task {task_id}")

            logger.info(f"Task {task_id} cancelled")

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to cancel task {task_id}: {e}")
            raise
        finally:
            conn.close()

    def get_task_status(self, task_id: int) -> dict:
        """
        Get task status and details

        Args:
            task_id: Task ID to query

        Returns:
            Task dictionary with all fields

        Raises:
            ValueError: If task not found
            sqlite3.Error: If database operation fails
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()

            cursor.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
            row = cursor.fetchone()

            if not row:
                raise ValueError(f"Task {task_id} not found")

            # Convert row to Task object
            task = Task(
                id=row['id'],
                name=row['name'],
                status=row['status'],
                north=row['north'],
                south=row['south'],
                east=row['east'],
                west=row['west'],
                zoom_min=row['zoom_min'],
                zoom_max=row['zoom_max'],
                style=row['style'],
                output_format=row['output_format'],
                output_path=row['output_path'],
                total_tiles=row['total_tiles'],
                downloaded_tiles=row['downloaded_tiles'],
                failed_tiles=row['failed_tiles'],
                created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else None,
                started_at=datetime.fromisoformat(row['started_at']) if row['started_at'] else None,
                completed_at=datetime.fromisoformat(row['completed_at']) if row['completed_at'] else None,
                error_message=row['error_message']
            )

            return task.to_dict()

        finally:
            conn.close()

    def get_active_tasks(self) -> List[dict]:
        """
        Get all active tasks (pending, running, or paused)

        Returns:
            List of task dictionaries

        Raises:
            sqlite3.Error: If database operation fails
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()

            cursor.execute('''
                SELECT * FROM tasks
                WHERE status IN ('pending', 'running', 'paused')
                ORDER BY created_at DESC
            ''')

            rows = cursor.fetchall()

            tasks = []
            for row in rows:
                task = Task(
                    id=row['id'],
                    name=row['name'],
                    status=row['status'],
                    north=row['north'],
                    south=row['south'],
                    east=row['east'],
                    west=row['west'],
                    zoom_min=row['zoom_min'],
                    zoom_max=row['zoom_max'],
                    style=row['style'],
                    output_format=row['output_format'],
                    output_path=row['output_path'],
                    total_tiles=row['total_tiles'],
                    downloaded_tiles=row['downloaded_tiles'],
                    failed_tiles=row['failed_tiles'],
                    created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else None,
                    started_at=datetime.fromisoformat(row['started_at']) if row['started_at'] else None,
                    completed_at=datetime.fromisoformat(row['completed_at']) if row['completed_at'] else None,
                    error_message=row['error_message']
                )
                tasks.append(task.to_dict())

            return tasks

        finally:
            conn.close()

    def _run_task(self, task_id: int, stop_flag: Optional[threading.Event] = None):
        """
        Wrapper for running task in background thread

        Args:
            task_id: Task ID to execute

        Process:
            Creates new event loop and runs _execute_task coroutine
        """
        logger.info(f"Task {task_id} thread started")
        try:
            asyncio.run(self._execute_task(task_id, stop_flag))
        except Exception as e:
            logger.error(f"Task {task_id} thread failed: {e}")
        finally:
            with self._state_lock:
                if self.active_tasks.get(task_id) is threading.current_thread():
                    self.active_tasks.pop(task_id, None)
                if stop_flag is None or self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)
            logger.info(f"Task {task_id} thread finished")

    async def _execute_task(self, task_id: int, stop_flag: Optional[threading.Event] = None):
        """
        Execute download task asynchronously

        Args:
            task_id: Task ID to execute

        Process:
            1. Get pending/failed tiles from database
            2. Define progress_callback that updates database and emits socketio
            3. Call download_engine.download_tiles_batch()
            4. Check stop_flag between operations
            5. If output_format includes image, call stitch_tiles_with_gdal for each zoom
            6. Update task status to 'completed'

        Error Handling:
            - Catches exceptions and updates task status to 'failed'
            - Logs error message to database
            - Emits error notification via socketio
        """
        logger.info(f"Executing task {task_id}")

        conn = get_connection()
        try:
            cursor = conn.cursor()

            # Get task details
            cursor.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
            task_row = cursor.fetchone()

            if not task_row:
                raise ValueError(f"Task {task_id} not found")

            task = Task(
                id=task_row['id'],
                name=task_row['name'],
                status=task_row['status'],
                north=task_row['north'],
                south=task_row['south'],
                east=task_row['east'],
                west=task_row['west'],
                zoom_min=task_row['zoom_min'],
                zoom_max=task_row['zoom_max'],
                style=task_row['style'],
                output_format=task_row['output_format'],
                output_path=task_row['output_path'],
                total_tiles=task_row['total_tiles'],
                downloaded_tiles=task_row['downloaded_tiles'],
                failed_tiles=task_row['failed_tiles']
            )

            # Get pending and failed tiles
            cursor.execute('''
                SELECT task_id, zoom, x, y, status, retry_count, error_message
                FROM task_tiles
                WHERE task_id = ? AND status IN ('pending', 'failed')
                ORDER BY zoom, x, y
            ''', (task_id,))

            tile_rows = cursor.fetchall()
            tiles = [
                Tile(
                    task_id=row['task_id'],
                    zoom=row['zoom'],
                    x=row['x'],
                    y=row['y'],
                    status=row['status'],
                    retry_count=row['retry_count'],
                    error_message=row['error_message']
                )
                for row in tile_rows
            ]

            logger.info(f"Task {task_id}: {len(tiles)} tiles to download")

            if len(tiles) == 0:
                logger.info(f"Task {task_id}: No tiles to download, proceeding to stitching")

            # Define progress callback
            async def progress_callback(tile: Tile, status: str, error: Optional[str]):
                """Update database and emit socketio event for tile progress"""
                try:
                    if self._is_stop_requested(task_id, stop_flag):
                        logger.info(f"Task {task_id}: Stop flag detected in progress callback")
                        return

                    tile_conn = get_connection()
                    try:
                        tile_cursor = tile_conn.cursor()

                        tile_cursor.execute('''
                            SELECT status FROM task_tiles
                            WHERE task_id = ? AND zoom = ? AND x = ? AND y = ?
                        ''', (tile.task_id, tile.zoom, tile.x, tile.y))
                        existing = tile_cursor.fetchone()
                        old_status = existing['status'] if existing else None

                        tile_cursor.execute('''
                            UPDATE task_tiles
                            SET status = ?, error_message = ?
                            WHERE task_id = ? AND zoom = ? AND x = ? AND y = ?
                        ''', (status, error, tile.task_id, tile.zoom, tile.x, tile.y))

                        downloaded_delta, failed_delta = self._status_count_deltas(old_status, status)
                        if downloaded_delta or failed_delta:
                            tile_cursor.execute('''
                                UPDATE tasks
                                SET downloaded_tiles = MAX(downloaded_tiles + ?, 0),
                                    failed_tiles = MAX(failed_tiles + ?, 0)
                                WHERE id = ?
                            ''', (downloaded_delta, failed_delta, task_id))

                        tile_conn.commit()

                        # Get updated task info
                        tile_cursor.execute('''
                            SELECT * FROM tasks WHERE id = ?
                        ''', (task_id,))
                        task_row = tile_cursor.fetchone()

                        if task_row and self.socketio:
                            # Get current running time
                            total_running_seconds = self.get_current_running_time(task_id)

                            # Emit full task progress update via socketio
                            self.socketio.emit('task_progress', {
                                'task_id': task_id,
                                'id': task_id,
                                'name': task_row['name'],
                                'status': task_row['status'],
                                'downloaded_tiles': task_row['downloaded_tiles'],
                                'failed_tiles': task_row['failed_tiles'],
                                'total_tiles': task_row['total_tiles'],
                                'north': task_row['north'],
                                'south': task_row['south'],
                                'east': task_row['east'],
                                'west': task_row['west'],
                                'zoom_min': task_row['zoom_min'],
                                'zoom_max': task_row['zoom_max'],
                                'style': task_row['style'],
                                'output_format': task_row['output_format'],
                                'output_path': task_row['output_path'],
                                'started_at': task_row['started_at'],
                                'created_at': task_row['created_at'],
                                'total_running_seconds': total_running_seconds
                            })

                    finally:
                        tile_conn.close()

                except Exception as e:
                    logger.error(f"Progress callback error for tile {tile.zoom}/{tile.x}/{tile.y}: {e}")

            # Download tiles
            if len(tiles) > 0:
                # Check stop flag before downloading
                if self._is_stop_requested(task_id, stop_flag):
                    logger.info(f"Task {task_id}: Stop flag detected before download")
                    return

                logger.info(f"Task {task_id}: Starting tile download")
                # Convert style name to style code
                style_code = STYLE_MAP.get(task.style, 'm')  # Default to roadmap if not found
                await self.download_engine.download_tiles_batch(
                    tiles=tiles,
                    style=style_code,
                    progress_callback=progress_callback,
                    stop_flag=stop_flag
                )

                logger.info(f"Task {task_id}: Tile download completed")

            # Check stop flag before stitching
            if self._is_stop_requested(task_id, stop_flag):
                logger.info(f"Task {task_id}: Stop flag detected before stitching")
                return

            # Stitch tiles if output format includes image
            if task.output_format in ['png', 'jpg', 'both']:
                logger.info(f"Task {task_id}: Starting tile stitching")

                # Get all completed tiles for stitching
                cursor.execute('''
                    SELECT task_id, zoom, x, y, status, retry_count
                    FROM task_tiles
                    WHERE task_id = ? AND status = 'completed'
                    ORDER BY zoom, x, y
                ''', (task_id,))

                completed_tile_rows = cursor.fetchall()
                completed_tiles = [
                    Tile(
                        task_id=row['task_id'],
                        zoom=row['zoom'],
                        x=row['x'],
                        y=row['y'],
                        status=row['status'],
                        retry_count=row['retry_count']
                    )
                    for row in completed_tile_rows
                ]

                # Stitch tiles for each zoom level
                zoom_levels = sorted(set(tile.zoom for tile in completed_tiles))
                logger.info(f"Task {task_id}: Stitching {len(zoom_levels)} zoom levels")

                for zoom in zoom_levels:
                    # Check stop flag before each zoom level
                    if self._is_stop_requested(task_id, stop_flag):
                        logger.info(f"Task {task_id}: Stop flag detected during stitching")
                        return

                    output_path = Path(task.output_path) / f"task_{task_id}" / f"{task.name}_zoom_{zoom}.tif"
                    logger.info(f"Task {task_id}: Stitching zoom level {zoom} to {output_path}")

                    try:
                        # Convert style name to style code
                        style_code = STYLE_MAP.get(task.style, 'm')  # Default to roadmap if not found
                        self.download_engine.stitch_tiles_with_gdal(
                            tiles=completed_tiles,
                            style=style_code,
                            output_path=str(output_path),
                            zoom_level=zoom
                        )
                        logger.info(f"Task {task_id}: Zoom level {zoom} stitched successfully")

                        # Emit stitching progress
                        if self.socketio:
                            self.socketio.emit('task_stitch_progress', {
                                'task_id': task_id,
                                'zoom_level': zoom,
                                'output_path': str(output_path)
                            })

                    except Exception as e:
                        logger.error(f"Task {task_id}: Failed to stitch zoom level {zoom}: {e}")
                        # Continue with other zoom levels even if one fails

            # Handle tiles_only format: copy tiles to output_path
            elif task.output_format == 'tiles_only':
                logger.info(f"Task {task_id}: Copying tiles to output path (tiles_only mode)")

                # Get all completed tiles
                cursor.execute('''
                    SELECT task_id, zoom, x, y, status, retry_count
                    FROM task_tiles
                    WHERE task_id = ? AND status = 'completed'
                    ORDER BY zoom, x, y
                ''', (task_id,))

                completed_tile_rows = cursor.fetchall()

                # Convert style name to style code
                style_code = STYLE_MAP.get(task.style, 'm')

                # Copy tiles from cache to output_path/task_{id}/
                output_base = Path(task.output_path) / f"task_{task_id}"
                output_base.mkdir(parents=True, exist_ok=True)

                copied_count = 0
                for row in completed_tile_rows:
                    tile = Tile(
                        task_id=row['task_id'],
                        zoom=row['zoom'],
                        x=row['x'],
                        y=row['y'],
                        status=row['status'],
                        retry_count=row['retry_count']
                    )

                    # Source: cache path
                    cache_path = tile.cache_path(style_code)

                    # Destination: output_path/{zoom}/{x}/{y}.png
                    dest_path = output_base / str(tile.zoom) / str(tile.x) / f"{tile.y}.png"
                    dest_path.parent.mkdir(parents=True, exist_ok=True)

                    try:
                        if cache_path.exists():
                            import shutil
                            shutil.copy2(cache_path, dest_path)
                            copied_count += 1
                        else:
                            logger.warning(f"Task {task_id}: Cache file not found: {cache_path}")
                    except Exception as e:
                        logger.error(f"Task {task_id}: Failed to copy tile {tile.zoom}/{tile.x}/{tile.y}: {e}")

                logger.info(f"Task {task_id}: Copied {copied_count}/{len(completed_tile_rows)} tiles to {output_base}")

            if self._is_stop_requested(task_id, stop_flag):
                logger.info(f"Task {task_id}: Stop flag detected, not marking as completed")
                return

            cursor.execute('SELECT status FROM tasks WHERE id = ?', (task_id,))
            current_row = cursor.fetchone()
            if not current_row or current_row['status'] in ('cancelled', 'paused'):
                logger.info(f"Task {task_id}: Current status prevents completion")
                return

            cursor.execute('''
                SELECT
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count
                FROM task_tiles
                WHERE task_id = ?
            ''', (task_id,))
            counts = cursor.fetchone()
            failed_count = counts['failed_count'] or 0
            pending_count = counts['pending_count'] or 0

            if failed_count > 0 or pending_count > 0:
                error_message = f"{failed_count} tile(s) failed, {pending_count} tile(s) pending"
                cursor.execute('''
                    UPDATE tasks
                    SET status = 'failed', error_message = ?, completed_at = ?
                    WHERE id = ? AND status = 'running'
                ''', (error_message, datetime.now(), task_id))
                conn.commit()
                logger.warning(f"Task {task_id}: {error_message}")
                if cursor.rowcount and self.socketio:
                    self.socketio.emit('task_failed', {
                        'task_id': task_id,
                        'status': 'failed',
                        'error_message': error_message
                    })
                return

            cursor.execute('''
                UPDATE tasks
                SET status = 'completed', completed_at = ?
                WHERE id = ? AND status = 'running'
            ''', (datetime.now(), task_id))

            conn.commit()

            if cursor.rowcount:
                self._update_total_running_time(task_id)
                self._record_time_action(task_id, 'complete')

                logger.info(f"Task {task_id}: Completed successfully")

                if self.socketio:
                    self.socketio.emit('task_completed', {
                        'task_id': task_id,
                        'status': 'completed'
                    })

        except Exception as e:
            logger.error(f"Task {task_id} execution failed: {e}")

            # Update task status to failed
            try:
                cursor.execute('''
                    UPDATE tasks
                    SET status = 'failed', error_message = ?, completed_at = ?
                    WHERE id = ? AND status NOT IN ('cancelled', 'paused')
                ''', (str(e), datetime.now(), task_id))

                conn.commit()

                if cursor.rowcount and self.socketio:
                    self.socketio.emit('task_failed', {
                        'task_id': task_id,
                        'status': 'failed',
                        'error_message': str(e)
                    })

            except Exception as update_error:
                logger.error(f"Failed to update task {task_id} status to failed: {update_error}")

        finally:
            conn.close()
