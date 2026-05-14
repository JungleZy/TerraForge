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

        logger.info("TaskManager initialized")

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

        Process:
            1. Validate task status (must be pending or paused)
            2. Update status to 'running' and set started_at
            3. Create stop_flag (threading.Event)
            4. Start background thread running _run_task
            5. Track in active_tasks dict
        """
        logger.info(f"Starting task {task_id}")

        conn = get_connection()
        try:
            cursor = conn.cursor()

            # Get task status
            cursor.execute('SELECT status FROM tasks WHERE id = ?', (task_id,))
            row = cursor.fetchone()

            if not row:
                raise ValueError(f"Task {task_id} not found")

            status = row['status']

            # Validate status
            if status not in ['pending', 'paused']:
                raise ValueError(
                    f"Cannot start task {task_id} with status '{status}'. "
                    f"Task must be 'pending' or 'paused'."
                )

            # Update task status to running
            cursor.execute('''
                UPDATE tasks
                SET status = 'running', started_at = ?
                WHERE id = ?
            ''', (datetime.now(), task_id))

            conn.commit()

            # Create stop flag for this task
            stop_flag = threading.Event()
            self.stop_flags[task_id] = stop_flag

            # Start background thread
            thread = threading.Thread(
                target=self._run_task,
                args=(task_id,),
                daemon=True,
                name=f"Task-{task_id}"
            )
            self.active_tasks[task_id] = thread
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
        """
        logger.info(f"Pausing task {task_id}")

        # Set stop flag
        if task_id in self.stop_flags:
            self.stop_flags[task_id].set()
            logger.debug(f"Stop flag set for task {task_id}")

        # Update database status
        conn = get_connection()
        try:
            cursor = conn.cursor()

            cursor.execute('''
                UPDATE tasks
                SET status = 'paused'
                WHERE id = ? AND status = 'running'
            ''', (task_id,))

            if cursor.rowcount == 0:
                logger.warning(f"Task {task_id} was not running, cannot pause")

            conn.commit()
            logger.info(f"Task {task_id} paused")

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

        # Set stop flag
        if task_id in self.stop_flags:
            self.stop_flags[task_id].set()
            logger.debug(f"Stop flag set for task {task_id}")

        # Update database status
        conn = get_connection()
        try:
            cursor = conn.cursor()

            cursor.execute('''
                UPDATE tasks
                SET status = 'cancelled'
                WHERE id = ?
            ''', (task_id,))

            conn.commit()
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

    def _run_task(self, task_id: int):
        """
        Wrapper for running task in background thread

        Args:
            task_id: Task ID to execute

        Process:
            Creates new event loop and runs _execute_task coroutine
        """
        logger.info(f"Task {task_id} thread started")
        try:
            asyncio.run(self._execute_task(task_id))
        except Exception as e:
            logger.error(f"Task {task_id} thread failed: {e}")
        finally:
            # Clean up task from active tasks
            if task_id in self.active_tasks:
                del self.active_tasks[task_id]
            if task_id in self.stop_flags:
                del self.stop_flags[task_id]
            logger.info(f"Task {task_id} thread finished")

    async def _execute_task(self, task_id: int):
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
                    # Check stop flag
                    if task_id in self.stop_flags and self.stop_flags[task_id].is_set():
                        logger.info(f"Task {task_id}: Stop flag detected in progress callback")
                        return

                    # Update tile status in database
                    tile_conn = get_connection()
                    try:
                        tile_cursor = tile_conn.cursor()

                        tile_cursor.execute('''
                            UPDATE task_tiles
                            SET status = ?, error_message = ?
                            WHERE task_id = ? AND zoom = ? AND x = ? AND y = ?
                        ''', (status, error, tile.task_id, tile.zoom, tile.x, tile.y))

                        # Update task counters
                        if status == 'completed':
                            tile_cursor.execute('''
                                UPDATE tasks
                                SET downloaded_tiles = downloaded_tiles + 1
                                WHERE id = ?
                            ''', (task_id,))
                        elif status == 'failed':
                            tile_cursor.execute('''
                                UPDATE tasks
                                SET failed_tiles = failed_tiles + 1
                                WHERE id = ?
                            ''', (task_id,))

                        tile_conn.commit()

                        # Get updated task progress
                        tile_cursor.execute('''
                            SELECT downloaded_tiles, failed_tiles, total_tiles
                            FROM tasks WHERE id = ?
                        ''', (task_id,))
                        progress_row = tile_cursor.fetchone()

                        if progress_row and self.socketio:
                            downloaded = progress_row['downloaded_tiles']
                            failed = progress_row['failed_tiles']
                            total = progress_row['total_tiles']
                            progress_percent = (downloaded / total * 100) if total > 0 else 0

                            # Emit progress update via socketio
                            self.socketio.emit('task_progress', {
                                'task_id': task_id,
                                'downloaded_tiles': downloaded,
                                'failed_tiles': failed,
                                'total_tiles': total,
                                'progress_percent': round(progress_percent, 2)
                            })

                    finally:
                        tile_conn.close()

                except Exception as e:
                    logger.error(f"Progress callback error for tile {tile.zoom}/{tile.x}/{tile.y}: {e}")

            # Download tiles
            if len(tiles) > 0:
                # Check stop flag before downloading
                if task_id in self.stop_flags and self.stop_flags[task_id].is_set():
                    logger.info(f"Task {task_id}: Stop flag detected before download")
                    return

                logger.info(f"Task {task_id}: Starting tile download")
                # Convert style name to style code
                style_code = STYLE_MAP.get(task.style, 'm')  # Default to roadmap if not found
                await self.download_engine.download_tiles_batch(
                    tiles=tiles,
                    style=style_code,
                    progress_callback=progress_callback
                )

                logger.info(f"Task {task_id}: Tile download completed")

            # Check stop flag before stitching
            if task_id in self.stop_flags and self.stop_flags[task_id].is_set():
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
                    if task_id in self.stop_flags and self.stop_flags[task_id].is_set():
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

            # Check stop flag before marking as completed
            if task_id in self.stop_flags and self.stop_flags[task_id].is_set():
                logger.info(f"Task {task_id}: Stop flag detected, not marking as completed")
                return

            # Update task status to completed
            cursor.execute('''
                UPDATE tasks
                SET status = 'completed', completed_at = ?
                WHERE id = ?
            ''', (datetime.now(), task_id))

            conn.commit()

            logger.info(f"Task {task_id}: Completed successfully")

            # Emit completion notification
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
                    WHERE id = ?
                ''', (str(e), datetime.now(), task_id))

                conn.commit()

                # Emit failure notification
                if self.socketio:
                    self.socketio.emit('task_failed', {
                        'task_id': task_id,
                        'status': 'failed',
                        'error_message': str(e)
                    })

            except Exception as update_error:
                logger.error(f"Failed to update task {task_id} status to failed: {update_error}")

        finally:
            conn.close()
