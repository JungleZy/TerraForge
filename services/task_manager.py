"""
Task Manager Service

Manages download task lifecycle including creation, execution, pause/resume, and cancellation.
Coordinates between database, download engine, and WebSocket notifications.
"""

import logging
import shutil
import threading
import asyncio
import time
from typing import Optional, Dict, Any, List, Tuple

from core.database import get_connection, parse_db_timestamp, utc_now, utc_now_iso
from models.task import Task, Tile
from services.download_engine import DownloadEngine, WARN_TILES_THRESHOLD
from services.config_manager import ConfigManager
from services.geo_validation import require_absolute_output_dir, sanitize_filename
from services.task_cleanup import resolve_stored_output_dir

logger = logging.getLogger(__name__)

# Style mapping from full names to Google Maps style codes
STYLE_MAP = {
    'roadmap': 'm',      # Standard roadmap
    'satellite': 's',    # Satellite imagery
    'hybrid': 'y',       # Hybrid (satellite + labels)
    'terrain': 't'       # Terrain map
}

# How often the tile-copy stage reports progress, in tiles. The copy runs *after*
# the download progress bar already reached 100%, so with no events at all the UI
# freezes for as long as the copy takes (minutes at 100k tiles) and looks hung.
COPY_PROGRESS_INTERVAL = 200

# 下载进度计数批量落库的间隔(块)。tasks 表的 downloaded/failed 计数攒到
# 这个批次才 UPDATE 一次 —— 旧实现每块瓦片 3-4 条 SQL,是大任务的性能瓶颈。
PROGRESS_DB_FLUSH_INTERVAL = 200

# socketio task_progress 广播的最小间隔(秒)。旧实现每块瓦片都 emit 一次,
# 且每次都另开连接查 get_current_running_time —— 百万级瓦片就是百万次
# 同步 DB 查询 + 广播,全部跑在下载事件循环里把它堵死。节流后前端进度
# 仍以 2Hz 刷新,末块瓦片(完成那一发)不受节流限制。
PROGRESS_EMIT_MIN_INTERVAL = 0.5


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

        logger.debug("TaskManager initialized")

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

            now = utc_now_iso()
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

    def _stream_copy_tile(self, tile: Tile, style_code: str, output_base,
                          made_dirs: set, made_dirs_lock: threading.Lock) -> bool:
        """边下边复制的单瓦片写入:cache -> 产物目录,原子 .part + replace。

        与结尾复制阶段共用「同尺寸已存在即跳过」的幂等判定 —— 恢复/对账重跑
        不会复写。保留 copy2 字面量:取消钩子测试 monkeypatch copy2 作为取消
        触发器(见结尾复制段注释),即时复制与补拷线程同样走它,取消语义不变。
        临时名带线程 id:下载回调(事件循环线程)与补拷线程临时件不互踩。
        """
        src = tile.cache_path(style_code)
        dest = output_base / str(tile.zoom) / str(tile.x) / f"{tile.y}.png"
        with made_dirs_lock:
            if dest.parent not in made_dirs:
                dest.parent.mkdir(parents=True, exist_ok=True)
                made_dirs.add(dest.parent)
        try:
            if dest.exists() and dest.stat().st_size == src.stat().st_size:
                return False
        except OSError:
            pass  # stat 竞态(文件刚好被删):按需要复制处理,失败由外层记 warning
        tmp = dest.with_name(f"{dest.name}.part.{threading.get_ident()}")
        shutil.copy2(src, tmp)
        tmp.replace(dest)
        return True

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
            ''', (task_id, action, utc_now_iso()))
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
                last_start = parse_db_timestamp(row['timestamp'])
                elapsed_seconds = int((utc_now() - last_start).total_seconds())

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
                    last_start = parse_db_timestamp(start_row['timestamp'])
                    current_segment = int((utc_now() - last_start).total_seconds())
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
                - output_format: Output format — both (stitched image + tiles),
                  image_only (same products as both; png/jpg are legacy synonyms),
                  tiles_only (tiles only). All formats copy raw tiles to the
                  output dir — the /tiles/<id>/ preview serves from there.
                - output_path: Output directory path

        Returns:
            Task ID of the created task

        Raises:
            ValueError: If task parameters are invalid
            sqlite3.Error: If database operation fails

        Process:
            1. Count tiles using download_engine (no per-tile rows are written)
            2. Insert task into tasks table
            3. Return task_id
        """
        logger.info(f"Creating task: {params.get('name', 'Unnamed')}")

        # output_path 校验:必须是绝对路径且落在 Config.DOWNLOADS_DIR 内,
        # 否则 require_absolute_output_dir 抛 ValueError(API 层映射 400)。
        # 入库的是校验后的绝对路径(与 dem_task_manager.create_task 同口径)——
        # 存原始相对值的话,_execute_task 里 Path(task.output_path) 会按进程
        # CWD 解析,打包 exe 从快捷方式启动(CWD≠BASE_DIR)时文件写到校验范围外。
        output_path = require_absolute_output_dir(params['output_path'])

        # Create Task object for validation
        # 传原始值,由 Task.__post_init__ 里的 validate_bbox/validate_zoom 统一
        # 转换+校验 —— 在这里先 float()/int() 的话,None/列表会抛 TypeError
        # 变成 500,"abc" 的报错也不带字段名。
        task = Task(
            name=params['name'],
            status='pending',
            north=params['north'],
            south=params['south'],
            east=params['east'],
            west=params['west'],
            zoom_min=params['zoom_min'],
            zoom_max=params['zoom_max'],
            style=params['style'],
            output_format=params['output_format'],
            output_path=output_path
        )

        # 瓦片总数只计数、不物化,也不向 task_tiles 写任何行。
        # 为什么不再每块瓦片存一行:瓦片集合是 bbox+zoom 的纯函数,可由
        # DownloadEngine.iter_tiles 按确定性顺序随时重建;完成态以磁盘
        # cache 文件为准(cache 即真相),task_tiles 退化为只存失败瓦片的
        # 稀疏表。旧实现 50 万块瓦片就是 50 万次 INSERT,是建任务的主要瓶颈。
        total_tiles = self.download_engine.count_tiles(
            north=task.north,
            south=task.south,
            east=task.east,
            west=task.west,
            zoom_min=task.zoom_min,
            zoom_max=task.zoom_max,
        )
        logger.info(f"Calculated {total_tiles} tiles for task")

        # 瓦片数软阈值:超过 WARN_TILES_THRESHOLD 只记警告,不拒绝创建。
        # 0.1.4 起放开硬上限 —— 是否继续由用户在前端确认(下载弹窗会显示
        # 预计瓦片数与耗时,超阈值时要求二次确认);服务端不替用户做决定。
        if total_tiles > WARN_TILES_THRESHOLD:
            logger.warning(
                f"Task tile count {total_tiles} exceeds soft threshold "
                f"{WARN_TILES_THRESHOLD}; allowed (user confirmed in UI)"
            )

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

            conn.commit()

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
            ValueError: If task status is not pending, paused or failed
            sqlite3.Error: If database operation fails
        """
        logger.info(f"Starting task {task_id}")

        # 状态翻转 commit 之后、thread.start() 之前还有若干可能抛异常的调用
        # (get_current_running_time、socketio.emit)。一旦抛出,except 里的
        # rollback 对已 commit 的事务无效,会留下 status='running' 但线程从未
        # 启动的假运行任务 —— 用这两个标志在 except 里显式回补状态。
        status_committed = False
        thread_started = False

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

                # 'failed' 也在准许列表里:_execute_task 本就按 pending/failed
                # 捞瓦片(续传语义写好了),入口不能把失败任务关死 —— 否则一次
                # 网络波动失败的任务永远无法重试。
                status = row['status']
                if status not in ['pending', 'paused', 'failed']:
                    raise ValueError(
                        f"Cannot start task {task_id} with status '{status}'. "
                        f"Task must be 'pending', 'paused' or 'failed'."
                    )

                # started_at 只在首次启动时写入(COALESCE):resume/重试不再
                # 覆写 —— 字段语义是「首次开始时间」,运行时长另有
                # task_time_records + total_running_seconds 跟踪,不依赖它。
                cursor.execute('''
                    UPDATE tasks
                    SET status = 'running', started_at = COALESCE(started_at, ?)
                    WHERE id = ? AND status IN ('pending', 'paused', 'failed')
                ''', (utc_now_iso(), task_id))
                if cursor.rowcount != 1:
                    raise ValueError(f"Task {task_id} could not be started because its status changed")

                conn.commit()
                status_committed = True

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
            thread_started = True
            logger.info(f"Task {task_id} started in background thread")

        except Exception as e:
            conn.rollback()
            if status_committed and not thread_started:
                # commit 之后、thread.start() 之前的异常(get_current_running_time、
                # socketio.emit 等)会留下 status='running' 但线程从未启动的假
                # 运行任务;rollback 对已 commit 无效,显式把状态回补为 failed,
                # 并清掉 active_tasks/stop_flags 里从未启动的登记。
                with self._state_lock:
                    self.active_tasks.pop(task_id, None)
                    self.stop_flags.pop(task_id, None)
                try:
                    cursor.execute('''
                        UPDATE tasks
                        SET status = 'failed', error_message = ?
                        WHERE id = ? AND status = 'running'
                    ''', (f'Failed to start task: {e}', task_id))
                    conn.commit()
                except Exception as revert_error:
                    logger.error(
                        f"Failed to revert task {task_id} status after start failure: {revert_error}"
                    )
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

            # 状态翻转、时长累计、pause 时间记录必须在同一事务:旧实现先
            # commit 'paused' 再另开连接 _update_total_running_time,窗口内
            # 并发 resume(start_task)会先写入新的 'resume' 记录,使时长累计
            # 取到新时间戳、elapsed≈0,整段运行时长被吞掉。这里先 UPDATE
            # 拿到写锁再读最近的 start/resume 记录 —— resume 的时间记录在
            # 其自身 commit 之后才写入,必然排在本事务之后;而 resume 若先
            # 抢到写锁,说明任务此前已是 paused(时长早在上次 pause 累计过),
            # 本事务读到的就是正确的段起点。
            now = utc_now()

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

            cursor.execute('''
                SELECT timestamp FROM task_time_records
                WHERE task_id = ? AND action IN ('start', 'resume')
                ORDER BY timestamp DESC
                LIMIT 1
            ''', (task_id,))
            row = cursor.fetchone()
            if row:
                last_start = parse_db_timestamp(row['timestamp'])
                elapsed_seconds = int((now - last_start).total_seconds())
                if elapsed_seconds > 0:
                    cursor.execute('''
                        UPDATE tasks
                        SET total_running_seconds = total_running_seconds + ?
                        WHERE id = ?
                    ''', (elapsed_seconds, task_id))

            cursor.execute('''
                INSERT INTO task_time_records (task_id, action, timestamp)
                VALUES (?, 'pause', ?)
            ''', (task_id, now.isoformat(timespec='seconds')))

            conn.commit()

            with self._state_lock:
                if task_id in self.stop_flags:
                    self.stop_flags[task_id].set()
                    logger.debug(f"Stop flag set for task {task_id}")

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
            ValueError: If task not found, or already in a terminal state
                (completed/failed/cancelled) — 终态任务无可取消,如实抛错
                而不是什么都不改却报 'cancelled'
            sqlite3.Error: If database operation fails

        Process:
            1. Set stop_flag to signal task to stop
            2. Update status to 'cancelled' in database
        """
        logger.info(f"Cancelling task {task_id}")

        conn = get_connection()
        try:
            cursor = conn.cursor()

            # Only pending/running/paused may transition to cancelled — a
            # terminal completed/failed record must never be rewritten.
            cursor.execute('''
                UPDATE tasks
                SET status = 'cancelled'
                WHERE id = ? AND status IN ('pending', 'running', 'paused')
            ''', (task_id,))

            if cursor.rowcount == 0:
                cursor.execute('SELECT status FROM tasks WHERE id = ?', (task_id,))
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"Task {task_id} not found")
                raise ValueError(
                    f"Cannot cancel task {task_id} with status '{row['status']}'. "
                    f"Task must be 'pending', 'running' or 'paused'."
                )

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

            # Convert row to Task object。读取路径用 from_row(跳过
            # __post_init__ 校验):历史遗留的非法行不应让查询接口 500。
            task = Task.from_row(row)

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
                # 同 get_task_status:读取路径走 from_row,一条历史非法行
                # 不能把整个列表接口打成 500。
                tasks.append(Task.from_row(row).to_dict())

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
            1. Enumerate the tile set once: pending (download) vs cache hits
            2. Stream-copy to the output dir as tiles land (progress_callback),
               plus a backfill thread for cache-hit tiles — both concurrent
               with the download
            3. Call download_engine.download_tiles_batch()
            4. Check stop_flag between operations
            5. If output_format includes image, call stitch_tiles_with_gdal for each zoom
            6. Final copy pass — mostly a same-size reconciliation after step 2
            7. Update task status to 'completed'

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

            # 同读取路径:_execute_task 恢复历史任务时不能因遗留非法行
            # 在构造 Task 时抛校验错误(from_row 跳过 __post_init__)。
            task = Task.from_row(task_row)

            # Convert style name to style code once — the download, the stitching
            # and the tile copy all key the shared tile cache off this same value,
            # so it must not be recomputed (and possibly diverge) per stage.
            style_code = STYLE_MAP.get(task.style, 'm')  # Default to roadmap if not found

            # 存量任务行的 output_path 可能是相对路径(旧版本只校验不改写,入库的
            # 是用户原始值)—— 归一化成绝对路径再落盘;直接 Path(task.output_path)
            # 会按进程 CWD 解析,exe 换目录启动时写到校验范围外。
            output_dir = resolve_stored_output_dir(task.output_path)

            cache_enabled = (
                self.config_manager.get('cache_enabled', 'true') or 'true'
            ).lower() == 'true'

            # cache_enabled=false 时任务注定零产出:下载不落盘,而拼接
            # (png/jpg/both/image_only)和瓦片复制(所有格式)的输入
            # 都是 cache 文件 —— 修复前这种任务会空跑一遍下载再被标
            # 'completed'。明确拒绝(走通用 except 标 failed + 如实错误信息),
            # 不为它保留「全量重下、完成态无法推导」的旧行为。
            if not cache_enabled:
                raise ValueError(
                    "cache_enabled=false 时任务无法产出任何结果"
                    "(拼接与瓦片复制都依赖 tile cache),请开启缓存后重试"
                )

            # 待下载集合不再查 task_tiles(它现在只是失败瓦片的稀疏表):瓦片集合
            # 是 bbox+zoom 的纯函数,用 iter_tiles 按确定性顺序重建;磁盘 cache
            # 里已存在且非空的文件就是已完成 —— cache 文件才是完成态的真相。
            # 失败瓦片天然没有 cache 文件,暂停/崩溃后恢复自然只补缺口。
            #
            # 顺带把稀疏失败表的现有行读成一个集合(它是稀疏的,行数 = 失败瓦片数,
            # 不会随选区膨胀):枚举遇 cache 命中且该瓦片在集合里时,记入待清理清单
            # —— 这枚 failed 行的瓦片其实已成功(cache 落盘),只是当时回调没跑到
            # (stop 检查/进程崩溃),清理见枚举后的对账段。
            failed_tile_keys = set()
            if cache_enabled:
                cursor.execute('''
                    SELECT zoom, x, y FROM task_tiles
                    WHERE task_id = ? AND status = 'failed'
                ''', (task_id,))
                failed_tile_keys = {
                    (row['zoom'], row['x'], row['y']) for row in cursor.fetchall()
                }

            # 单遍枚举同时产出「待下载」与「已完成」两份清单(互不重叠,每块
            # 瓦片只物化一次)。旧实现下载前只建待下载列表,下载后再第二遍
            # 全量 stat 重建 completed 列表 —— 两遍枚举 + 部分瓦片两份对象;
            # 现在下载结束后直接把本次下载成功的瓦片并入 completed_tiles
            # (见下载调用处),省掉第二遍。
            tiles: List[Tile] = []
            completed_tiles: List[Tile] = []
            cache_hits = 0
            stale_failed_keys: List[Tuple[int, int, int]] = []
            for tile in self.download_engine.iter_tiles(
                north=task.north,
                south=task.south,
                east=task.east,
                west=task.west,
                zoom_min=task.zoom_min,
                zoom_max=task.zoom_max,
                task_id=task_id,
            ):
                if cache_enabled:
                    try:
                        # 单次 stat 同时回答「存在吗」与「非空吗」—— 旧写法
                        # exists() + stat() 每块瓦片两次 syscall,恢复枚举
                        # 大任务时是纯浪费。判定语义不变:存在且非空才算命中。
                        # FileNotFoundError 就是「未命中」,不是故障,不打 warning。
                        if tile.cache_path(style_code).stat().st_size > 0:
                            cache_hits += 1
                            completed_tiles.append(tile)
                            key = (tile.zoom, tile.x, tile.y)
                            if key in failed_tile_keys:
                                stale_failed_keys.append(key)
                            continue
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        logger.warning(
                            f"Task {task_id}: Cache check failed for tile "
                            f"{tile.zoom}/{tile.x}/{tile.y}: {e}"
                        )
                tiles.append(tile)

            logger.info(
                f"Task {task_id}: {len(tiles)} tiles to download "
                f"({cache_hits} already in cache)"
            )

            # 命中清单快照:补拷线程遍历它往产物目录复制,与下载并行(见下文
            # 「边下边复制」)。必须快照 —— 下载回调还会往 completed_tiles
            # 追加本次下载的瓦片,迭代中不能共享同一列表。
            cache_hit_tiles = list(completed_tiles)

            if len(tiles) == 0:
                logger.info(f"Task {task_id}: No tiles to download, proceeding to stitching")

            # 对账:清掉 cache 命中瓦片残留在稀疏表里的 failed 行。
            # 留下它们的场景:瓦片下载成功、cache 已落盘,但进度回调因 stop 检查
            # (暂停/取消)或进程崩溃没能执行 —— 回调里的 DELETE 是唯一清行路径,
            # 它没跑,失败行就永远留着;而枚举遇 cache 命中直接 continue,不再有
            # 任何路径清它,完成判定的 failed_count>0 恒真,任务反复失败不自愈。
            # cache 文件才是完成态真相(见上文),cache 命中即视为成功,在这里
            # 批量补清。executemany 单事务一次 commit,不是逐瓦片开事务。
            if stale_failed_keys:
                cursor.executemany(
                    '''
                    DELETE FROM task_tiles
                    WHERE task_id = ? AND zoom = ? AND x = ? AND y = ?
                    ''',
                    [(task_id, zoom, x, y) for zoom, x, y in stale_failed_keys]
                )
                conn.commit()
                logger.info(
                    f"Task {task_id}: Cleared {len(stale_failed_keys)} stale failed "
                    f"tile row(s) already present in cache"
                )

            # 计数对账:枚举时已经 stat 过每个 cache 文件,顺带把 tasks 计数校准成
            # 「cache 命中数 + 稀疏失败表失败数」。批量落库在进程崩溃时最多丢一个
            # 批次的计数,恢复时这次对账把它追回来,进度不会少算。
            if cache_enabled:
                cursor.execute('''
                    SELECT COUNT(*) AS c FROM task_tiles
                    WHERE task_id = ? AND status = 'failed'
                ''', (task_id,))
                failed_rows = cursor.fetchone()['c']
                cursor.execute('''
                    UPDATE tasks SET downloaded_tiles = ?, failed_tiles = ?
                    WHERE id = ?
                ''', (cache_hits, failed_rows, task_id))
                conn.commit()
                base_downloaded, base_failed = cache_hits, failed_rows
            else:
                base_downloaded = task.downloaded_tiles
                base_failed = task.failed_tiles

            # --- 边下边复制:产物目录 = 已下载内容的镜像 ----------------------
            # 两条写入路径,与下载并行,结尾复制阶段(下方)退化为同尺寸对账:
            #   ① 下载回调:瓦片落 cache 成功后立即复制一份到产物目录;
            #   ② 补拷线程:枚举出的 cache 命中瓦片(不经回调)开案就复制。
            # 两份清单天然不相交(命中 vs 待下载),写盘不冲突;取消时保留已
            # 复制部分 —— 与 cache 的状态一致,取消任务的产物即部分下载内容。
            stream_output_base = output_dir / f"task_{task_id}"
            stream_made_dirs: set = set()
            stream_dirs_lock = threading.Lock()

            def _stream_copy_quiet(tile: Tile) -> None:
                # 单块失败(如磁盘满)不拖垮下载/补拷:记 warning,结尾复制
                # 阶段的对账会按「同尺寸跳过」判定重试这块。
                try:
                    self._stream_copy_tile(
                        tile, style_code,
                        stream_output_base, stream_made_dirs, stream_dirs_lock,
                    )
                except Exception as e:
                    logger.warning(
                        f"Task {task_id}: stream copy failed for "
                        f"{tile.zoom}/{tile.x}/{tile.y}: {e}"
                    )

            def _backfill_cache_hits() -> None:
                copied = 0
                for tile in cache_hit_tiles:
                    if self._is_stop_requested(task_id, stop_flag):
                        break
                    _stream_copy_quiet(tile)
                    copied += 1
                logger.info(
                    f"Task {task_id}: cache-hit backfill copied "
                    f"{copied}/{len(cache_hit_tiles)} tiles"
                )

            backfill_thread = threading.Thread(
                target=_backfill_cache_hits,
                name=f"task-{task_id}-backfill",
                daemon=True,
            )
            backfill_thread.start()

            # --- 进度回调:稀疏失败表 + 计数批量落库 ---
            # session_status 记录本次运行里每块瓦片已上报的状态,和稀疏表里的
            # 历史失败行一起还原 _status_count_deltas 需要的 old_status ——
            # 同一块瓦片重复上报(如同一次运行里 completed 两次)不会重复计数,
            # 任务暂停/取消时计数语义与旧版一致。
            session_status: Dict[Tuple[int, int, int], str] = {}
            progress_counts = {'downloaded': base_downloaded, 'failed': base_failed}
            unflushed = {'downloaded': 0, 'failed': 0}
            processed_since_flush = 0
            # 上次 socketio 广播的 monotonic 时间戳;初始 -inf 让首块瓦片必发
            # (见 PROGRESS_EMIT_MIN_INTERVAL)。
            last_emit_at = float('-inf')
            progress_conn = None  # 下载循环开启时建立,结束(finally)时关闭

            # 稀疏失败表的内存镜像 + 攒批写队列。回调不再逐瓦片 INSERT/DELETE
            # + commit(每块瓦片一个事务,全部跑在下载事件循环线程上同步执行),
            # 改为只登记到三个列表,随 flush_progress_counts 一起 executemany +
            # 单次 commit。
            # failed_keys 的初始真值 = 枚举时读入的历史失败行 - 对账已清掉的
            # 残留行;之后由回调自行维护 —— 本任务只有这一个写方,集合裁决与
            # 旧实现逐瓦片 rowcount 判定等价(失败:行已存在则 UPDATE 累加
            # retry_count,否则 INSERT;成功:行存在才需要 DELETE)。
            failed_keys = failed_tile_keys - set(stale_failed_keys)
            pending_tile_inserts: List[Tuple[int, int, int, int, Optional[str]]] = []
            pending_tile_updates: List[Tuple[Optional[str], int, int, int, int]] = []
            pending_tile_deletes: List[Tuple[int, int, int, int]] = []

            def flush_progress_counts():
                """把攒批的稀疏失败表写操作和计数增量一次性落库。

                每 PROGRESS_DB_FLUSH_INTERVAL 块以及下载循环结束时各刷一次 ——
                旧实现每块瓦片都对 tasks 表做 UPDATE、对 task_tiles 做一次
                INSERT/DELETE + commit,是高频小事务瓶颈。
                一个批次窗口内同一块瓦片只会被上报一次(每次运行每块瓦片只
                下载一回),所以按操作类型分组 executemany 与逐条执行的最终
                结果一致。崩溃最多丢一个批次的失败行:对应瓦片没有 cache
                文件,恢复时自然重下,失败了会重新登记,语义与计数攒批相同。
                """
                nonlocal processed_since_flush
                wrote_anything = False
                if pending_tile_inserts:
                    progress_conn.executemany('''
                        INSERT OR IGNORE INTO task_tiles
                            (task_id, zoom, x, y, status, retry_count, error_message)
                        VALUES (?, ?, ?, ?, 'failed', 1, ?)
                    ''', pending_tile_inserts)
                    pending_tile_inserts.clear()
                    wrote_anything = True
                if pending_tile_updates:
                    progress_conn.executemany('''
                        UPDATE task_tiles
                        SET retry_count = retry_count + 1, error_message = ?
                        WHERE task_id = ? AND zoom = ? AND x = ? AND y = ?
                    ''', pending_tile_updates)
                    pending_tile_updates.clear()
                    wrote_anything = True
                if pending_tile_deletes:
                    progress_conn.executemany('''
                        DELETE FROM task_tiles
                        WHERE task_id = ? AND zoom = ? AND x = ? AND y = ?
                    ''', pending_tile_deletes)
                    pending_tile_deletes.clear()
                    wrote_anything = True
                if unflushed['downloaded'] or unflushed['failed']:
                    progress_conn.execute('''
                        UPDATE tasks
                        SET downloaded_tiles = MAX(downloaded_tiles + ?, 0),
                            failed_tiles = MAX(failed_tiles + ?, 0)
                        WHERE id = ?
                    ''', (unflushed['downloaded'], unflushed['failed'], task_id))
                    unflushed['downloaded'] = 0
                    unflushed['failed'] = 0
                    wrote_anything = True
                if wrote_anything:
                    progress_conn.commit()
                processed_since_flush = 0

            # Define progress callback
            async def progress_callback(tile: Tile, status: str, error: Optional[str]):
                """维护稀疏失败表、累计计数增量,并按时间节流 emit socketio 事件

                为什么不再每块瓦片写一行:瓦片集合是 bbox+zoom 的纯函数(见
                iter_tiles),完成态以磁盘 cache 文件为准;task_tiles 只存失败
                瓦片 —— 失败时 UPSERT 一行,成功时 DELETE 掉历史失败行(均
                攒批落库,见 flush_progress_counts)。
                tasks 表的进度计数攒批落库;socketio 的 task_progress 按
                PROGRESS_EMIT_MIN_INTERVAL 节流(计数始终取内存实时值),
                末块瓦片必发 —— 逐瓦片广播 + 每次另开连接查运行时长会在
                百万级瓦片下堵死下载事件循环。
                """
                nonlocal processed_since_flush, last_emit_at
                if self._is_stop_requested(task_id, stop_flag):
                    logger.info(f"Task {task_id}: Stop flag detected in progress callback")
                    return

                key = (tile.zoom, tile.x, tile.y)
                old_status = session_status.get(key)

                # DB 层:稀疏失败表攒批登记 + 计数累计/攒批落库。这一层的故障
                # 必须显眼地记 error —— 失败瓦片若静默丢记录,完成判定的
                # failed_count>0 就守不住,任务会被误判成 completed。
                try:
                    if status == 'failed':
                        # 失败:登记写入/更新稀疏失败行。行是否已存在由内存镜像
                        # failed_keys 裁决(与旧实现 INSERT OR IGNORE 的 rowcount
                        # 判定等价):已存在 → UPDATE 在旧行基础上 retry_count +1,
                        # 否则 INSERT(retry_count=1)。
                        row_existed = key in failed_keys
                        if row_existed:
                            pending_tile_updates.append(
                                (error, task_id, tile.zoom, tile.x, tile.y)
                            )
                        else:
                            pending_tile_inserts.append(
                                (task_id, tile.zoom, tile.x, tile.y, error)
                            )
                            failed_keys.add(key)
                    else:
                        # 成功:清掉历史失败行;只有行真的存在才需要登记 DELETE,
                        # 对不存在的行 DELETE 是 no-op,跳过不改语义。
                        row_existed = key in failed_keys
                        if row_existed:
                            pending_tile_deletes.append(
                                (task_id, tile.zoom, tile.x, tile.y)
                            )
                            failed_keys.discard(key)

                    if old_status is None and row_existed:
                        old_status = 'failed'
                    downloaded_delta, failed_delta = self._status_count_deltas(old_status, status)
                    session_status[key] = status
                    progress_counts['downloaded'] += downloaded_delta
                    progress_counts['failed'] += failed_delta
                    unflushed['downloaded'] += downloaded_delta
                    unflushed['failed'] += failed_delta

                    # 本次下载成功的瓦片直接并入 completed 清单(替代旧的
                    # 「下载后从 results 再筛一遍 completed」):回调本来就逐块
                    # 上报,results 不必再为这件事全量保留。重复上报同一块
                    # (如测试里的 completed 两连发)只并一次。
                    if status == 'completed' and old_status != 'completed':
                        completed_tiles.append(tile)
                        # 边下边复制①:cache 落盘即镜像一份到产物目录,下载结束
                        # ≈ 产物就绪,不再在 100% 后整段等待结尾复制阶段。
                        _stream_copy_quiet(tile)

                    processed_since_flush += 1
                    if processed_since_flush >= PROGRESS_DB_FLUSH_INTERVAL:
                        flush_progress_counts()
                except Exception as e:
                    logger.error(
                        f"Progress callback DB error for tile "
                        f"{tile.zoom}/{tile.x}/{tile.y} (status={status}): {e}"
                    )
                    return

                # 广播层:与 DB 层解耦 —— socketio 故障只影响这一发实时推送,
                # 不能回头拖垮已经落库的进度,也不能中断下载循环。
                try:
                    # 时间节流(见 PROGRESS_EMIT_MIN_INTERVAL):done 达到
                    # 总数那一发(完成进度)必发,其余距上次不足间隔只记内存。
                    done_tiles = progress_counts['downloaded'] + progress_counts['failed']
                    now = time.monotonic()
                    if self.socketio and (
                        done_tiles >= task.total_tiles
                        or now - last_emit_at >= PROGRESS_EMIT_MIN_INTERVAL
                    ):
                        last_emit_at = now
                        # 运行时长口径:发 tasks 表的列累计值(不含当前段),
                        # 当前段由前端 calculateTimeInfo 按 started_at/服务端
                        # 时间统一叠加 —— 旧实现每发广播都调
                        # get_current_running_time 另开一条 SQLite 连接查库,
                        # 是事件循环阻塞点之一。列值在本次执行期间不会变
                        # (只有 pause/complete 才累计落库,而那意味着本次
                        # 执行已收尾),直接取入口已读出的 task_row,零查询。
                        total_running_seconds = task_row['total_running_seconds'] or 0

                        # Emit full task progress update via socketio。载荷字段与
                        # 旧版完全一致;计数取内存累计值(DB 按批落库,实时进度
                        # 不能等批次)。
                        self.socketio.emit('task_progress', {
                            'task_id': task_id,
                            'id': task_id,
                            'name': task.name,
                            'status': task.status,
                            'downloaded_tiles': progress_counts['downloaded'],
                            'failed_tiles': progress_counts['failed'],
                            'total_tiles': task.total_tiles,
                            'north': task.north,
                            'south': task.south,
                            'east': task.east,
                            'west': task.west,
                            'zoom_min': task.zoom_min,
                            'zoom_max': task.zoom_max,
                            'style': task.style,
                            'output_format': task.output_format,
                            'output_path': task.output_path,
                            'started_at': task_row['started_at'],
                            'created_at': task_row['created_at'],
                            'total_running_seconds': total_running_seconds
                        })
                except Exception as e:
                    logger.error(
                        f"Progress callback emit error for tile "
                        f"{tile.zoom}/{tile.x}/{tile.y}: {e}"
                    )

            # Download tiles
            # 补拷线程随下载并行跑;finally 里 join,保证「下载块的所有出口
            # (完成/stop return/异常)都在拼接与结尾对账前收尾」—— 此后产物
            # 目录只剩零头缺口,结尾复制阶段基本退化为 stat 对账。
            try:
                if len(tiles) > 0:
                    # Check stop flag before downloading
                    if self._is_stop_requested(task_id, stop_flag):
                        logger.info(f"Task {task_id}: Stop flag detected before download")
                        return

                    logger.info(f"Task {task_id}: Starting tile download")
                    progress_conn = get_connection()
                    try:
                        # 返回值(每块瓦片一条结果)刻意不接收:completed 清单由
                        # progress_callback 逐块并入(见回调里的注释),全量 results
                        # 列表在这里没有任何消费方,接住它只是白白占内存。
                        # 置 _collect_batch_results=False 让引擎连物化都不做
                        # (为什么是实例属性而不是 kwarg:tests/ 里多处把
                        # download_tiles_batch 换成四参替身,见引擎 __init__ 注释)。
                        self.download_engine._collect_batch_results = False
                        await self.download_engine.download_tiles_batch(
                            tiles=tiles,
                            style=style_code,
                            progress_callback=progress_callback,
                            stop_flag=stop_flag
                        )
                    finally:
                        # 下载循环结束时把最后不满一批的计数增量落库 —— 暂停/取消/
                        # 异常都不能丢这部分进度。flush 失败只记 error:收尾异常不能
                        # 掩盖下载循环抛出的原始异常,且连接无论如何都要关闭。
                        try:
                            flush_progress_counts()
                        except Exception as flush_error:
                            logger.error(
                                f"Task {task_id}: Failed to flush progress counts: {flush_error}"
                            )
                        progress_conn.close()

                    # 本次下载成功的瓦片已在回调里并入 completed 清单 —— 与枚举段的
                    # cache 命中清单互补,替代旧的「下载后第二遍全量 stat 枚举」。
                    logger.info(f"Task {task_id}: Tile download completed")
            finally:
                backfill_thread.join()

            # Check stop flag before stitching
            if self._is_stop_requested(task_id, stop_flag):
                logger.info(f"Task {task_id}: Stop flag detected before stitching")
                return

            # Stitching results, consumed by the completion logic further down.
            # A stitch failure used to be swallowed here, which meant a task whose
            # mosaics all failed still emitted task_completed — the user had no way
            # to find out short of opening the output directory.
            stitched_zooms: List[int] = []
            stitch_failures: List[Tuple[int, str]] = []

            # Stitch tiles if output format includes image
            # NOTE: 'png'/'jpg' are legacy synonyms of 'image_only' — the output
            # path below is hardcoded to .tif, so they never produce PNG/JPG.
            if task.output_format in ['png', 'jpg', 'both', 'image_only']:
                logger.info(f"Task {task_id}: Starting tile stitching")

                # Stitch tiles for each zoom level
                zoom_levels = sorted(set(tile.zoom for tile in completed_tiles))
                logger.info(f"Task {task_id}: Stitching {len(zoom_levels)} zoom levels")

                for zoom in zoom_levels:
                    # Check stop flag before each zoom level
                    if self._is_stop_requested(task_id, stop_flag):
                        logger.info(f"Task {task_id}: Stop flag detected during stitching")
                        return

                    # task.name 是用户输入,直接拼进文件名可携 '..' / 路径分隔符
                    # 逃逸出任务目录 —— 先消毒再拼。
                    safe_name = sanitize_filename(task.name)
                    output_path = output_dir / f"task_{task_id}" / f"{safe_name}_zoom_{zoom}.tif"

                    # 拼接断点:任务重试/恢复时,已产出且非空的 zoom mosaic 直接
                    # 保留不重算(大 zoom 单层拼接是十分钟级活)。判定口径与瓦片
                    # cache「存在且非空即完成」一致。已知取舍:进程在 Translate
                    # 写盘中途被杀会留下非空半成品 tif,这里无法区分,会当作完成
                    # 保留 —— 用户删掉该层文件重跑即可强制重算。
                    if output_path.exists() and output_path.stat().st_size > 0:
                        logger.info(
                            f"Task {task_id}: Zoom level {zoom} output already exists "
                            f"({output_path}), skipping stitch"
                        )
                        stitched_zooms.append(zoom)
                        if self.socketio:
                            self.socketio.emit('task_stitch_progress', {
                                'task_id': task_id,
                                'zoom_level': zoom,
                                'output_path': str(output_path)
                            })
                        continue

                    logger.info(f"Task {task_id}: Stitching zoom level {zoom} to {output_path}")

                    try:
                        # 拼接开始也发一次：旧事件只在拼完才发，单个大 zoom
                        # 一拼就是几十分钟起步，期间界面零反馈，看起来像「卡
                        # 100%」。phase='start' 让前端能把任务行切到「拼接中」。
                        if self.socketio:
                            try:
                                self.socketio.emit('task_stitch_progress', {
                                    'task_id': task_id,
                                    'zoom_level': zoom,
                                    'phase': 'start',
                                })
                            except Exception as e:
                                logger.warning(f"Task {task_id}: stitch start emit failed: {e!r}")

                        # to_thread 把 GDAL 拼接挪出事件循环:大 mosaic 拼接是
                        # 分钟级 CPU/IO 活,同步调用的期间暂停/取消/进度回调
                        # 全被堵死,只能等拼完才生效。
                        await asyncio.to_thread(
                            self.download_engine.stitch_tiles_with_gdal,
                            tiles=completed_tiles,
                            style=style_code,
                            output_path=str(output_path),
                            zoom_level=zoom,
                            # 保存路径全盘化后,拼接白名单要认该任务的注册产物根
                            extra_allowed_dir=str(output_dir),
                        )
                        logger.info(f"Task {task_id}: Zoom level {zoom} stitched successfully")
                        stitched_zooms.append(zoom)

                        # Emit stitching progress
                        if self.socketio:
                            self.socketio.emit('task_stitch_progress', {
                                'task_id': task_id,
                                'zoom_level': zoom,
                                'output_path': str(output_path)
                            })

                    except Exception as e:
                        logger.error(f"Task {task_id}: Failed to stitch zoom level {zoom}: {e}")
                        # Keep going: the remaining zoom levels are independent and
                        # the user is better off with the ones that do work. But the
                        # failure is *recorded* now — see the completion logic below,
                        # which turns an all-failed stitch into a failed task and a
                        # partially-failed one into a warning on the task row.
                        stitch_failures.append((zoom, str(e)))
                        if self.socketio:
                            self.socketio.emit('task_stitch_failed', {
                                'task_id': task_id,
                                'zoom_level': zoom,
                                'error_message': str(e)
                            })

            # Copy tiles to output_path — 所有格式都复制:历史预览的
            # /tiles/<id>/ 瓦片服务以产物目录为来源,image_only 不复制的话
            # 已完成任务的预览只能定位到区域、看不到任何已下载内容。
            # 边下边复制(回调即时复制 + cache 命中补拷线程)之后,这里绝大多数
            # 瓦片命中「同尺寸跳过」,本阶段实质是对账:补即时复制失败/补拷被
            # 取消打断留下的缺口,并继续提供复制进度事件与取消检查。
            # NOTE: this is a separate `if`, not `elif` — 'both' must do both.
            if task.output_format in ['png', 'jpg', 'both', 'image_only', 'tiles_only']:
                logger.info(f"Task {task_id}: Copying tiles to output path ({task.output_format} mode)")

                # Copy tiles from cache to output_path/task_{id}/
                output_base = output_dir / f"task_{task_id}"
                output_base.mkdir(parents=True, exist_ok=True)

                total_to_copy = len(completed_tiles)
                copied_count = 0
                # mkdir 按目录去重:瓦片按 zoom/x 分目录,每个目录只需建一次,
                # 旧实现每块瓦片都 mkdir(parents=True, exist_ok=True) ——
                # 10 万块瓦片就是 10 万次多余的 syscall。
                made_dirs = set()
                for copy_index, tile in enumerate(completed_tiles, start=1):
                    # 'both' is the default output format, so this loop runs for
                    # most tasks and at 100k tiles it takes minutes. Without a stop
                    # check inside it, cancelling only takes effect once the whole
                    # copy has finished.
                    if self._is_stop_requested(task_id, stop_flag):
                        logger.info(
                            f"Task {task_id}: Stop flag detected during tile copy "
                            f"({copied_count}/{total_to_copy} copied)"
                        )
                        return

                    # Source: cache path
                    cache_path = tile.cache_path(style_code)

                    # Destination: output_path/{zoom}/{x}/{y}.png
                    dest_path = output_base / str(tile.zoom) / str(tile.x) / f"{tile.y}.png"
                    dest_parent = dest_path.parent
                    if dest_parent not in made_dirs:
                        dest_parent.mkdir(parents=True, exist_ok=True)
                        made_dirs.add(dest_parent)

                    try:
                        if cache_path.exists():
                            # 复制断点:dest 已存在且大小与 cache 一致即视为已
                            # 复制,任务重试不再全量复写。判定可信的依据:cache
                            # 瓦片落盘即不可变(原子 .part + rename,见
                            # _download_single_tile),同 key 内容不会变;而
                            # copy2 中途被打断留下的截断文件必然比源小,会重新
                            # 复制。
                            # 保留 shutil.copy2 逐瓦片调用点:AST 契约
                            # (tests/test_output_format.py)钉字面量 'copy2',
                            # 取消钩子测试(test_task_lifecycle_state.py)
                            # monkeypatch copy2 作为取消触发器 —— 同卷硬链
                            # 零拷贝(_link_or_copy)与这两个钉点冲突,未采用。
                            if dest_path.exists() and (
                                dest_path.stat().st_size == cache_path.stat().st_size
                            ):
                                logger.debug(
                                    f"Task {task_id}: Tile {tile.zoom}/{tile.x}/{tile.y} "
                                    f"already copied, skipping"
                                )
                                copied_count += 1
                            else:
                                shutil.copy2(cache_path, dest_path)
                                copied_count += 1
                        else:
                            logger.warning(f"Task {task_id}: Cache file not found: {cache_path}")
                    except Exception as e:
                        logger.error(f"Task {task_id}: Failed to copy tile {tile.zoom}/{tile.x}/{tile.y}: {e}")

                    # Keep the UI alive during the copy — see COPY_PROGRESS_INTERVAL.
                    if self.socketio and (
                        copy_index % COPY_PROGRESS_INTERVAL == 0 or copy_index == total_to_copy
                    ):
                        # emit 故障（客户端断开等）不应打断复制本身
                        try:
                            self.socketio.emit('task_copy_progress', {
                                'task_id': task_id,
                                'copied_tiles': copied_count,
                                'processed_tiles': copy_index,
                                'total_tiles': total_to_copy
                            })
                        except Exception as e:
                            logger.warning(f"Task {task_id}: copy progress emit failed: {e!r}")

                logger.info(f"Task {task_id}: Copied {copied_count}/{total_to_copy} tiles to {output_base}")

            if self._is_stop_requested(task_id, stop_flag):
                logger.info(f"Task {task_id}: Stop flag detected, not marking as completed")
                return

            cursor.execute('SELECT status FROM tasks WHERE id = ?', (task_id,))
            current_row = cursor.fetchone()
            if not current_row or current_row['status'] in ('cancelled', 'paused'):
                logger.info(f"Task {task_id}: Current status prevents completion")
                return

            # 完成判定:稀疏失败表里还有失败行 → 任务失败;否则下载循环已正常
            # 走完(未下载的瓦片都在 cache 里,否则上面不会走到这里),pending
            # 概念已随 task_tiles 全量行一起消失。
            cursor.execute('''
                SELECT COUNT(*) AS failed_count FROM task_tiles
                WHERE task_id = ? AND status = 'failed'
            ''', (task_id,))
            failed_count = cursor.fetchone()['failed_count']

            if failed_count > 0:
                error_message = f"{failed_count} tile(s) failed"
                cursor.execute('''
                    UPDATE tasks
                    SET status = 'failed', error_message = ?, completed_at = ?
                    WHERE id = ? AND status = 'running'
                ''', (error_message, utc_now_iso(), task_id))
                conn.commit()
                logger.warning(f"Task {task_id}: {error_message}")
                if cursor.rowcount and self.socketio:
                    self.socketio.emit('task_failed', {
                        'task_id': task_id,
                        'status': 'failed',
                        'error_message': error_message
                    })
                return

            # Stitched images are a deliverable of their own: every tile row can be
            # 'completed' while not a single mosaic got produced. The tile counts
            # above cannot see that, so judge the stitching separately.
            stitch_detail = '; '.join(f"zoom {zoom}: {err}" for zoom, err in stitch_failures)

            if stitch_failures and not stitched_zooms:
                # Nothing to show for the stitching at all. Tiles are copied for
                # every format now, but the stitched mosaic is still the point of
                # picking image_only/both — calling this "completed" would be a
                # lie the user can only catch by browsing the output directory.
                error_message = (
                    f"拼接全部失败({len(stitch_failures)} 个缩放级别): {stitch_detail}"
                )
                cursor.execute('''
                    UPDATE tasks
                    SET status = 'failed', error_message = ?, completed_at = ?
                    WHERE id = ? AND status = 'running'
                ''', (error_message, utc_now_iso(), task_id))
                conn.commit()
                logger.error(f"Task {task_id}: {error_message}")
                if cursor.rowcount and self.socketio:
                    self.socketio.emit('task_failed', {
                        'task_id': task_id,
                        'status': 'failed',
                        'error_message': error_message
                    })
                return

            # Some zoom levels stitched, some did not. The successful ones are real
            # output the user wants to keep, so the task still completes — but the
            # warning is persisted on the task row (and carried on task_completed)
            # so it is not something they have to guess at.
            stitch_warning = None
            if stitch_failures:
                stitch_warning = (
                    f"部分缩放级别拼接失败"
                    f"({len(stitch_failures)}/{len(stitch_failures) + len(stitched_zooms)}): "
                    f"{stitch_detail}"
                )

            cursor.execute('''
                UPDATE tasks
                SET status = 'completed', error_message = ?, completed_at = ?
                WHERE id = ? AND status = 'running'
            ''', (stitch_warning, utc_now_iso(), task_id))

            conn.commit()

            if cursor.rowcount:
                self._update_total_running_time(task_id)
                self._record_time_action(task_id, 'complete')

                if stitch_warning:
                    logger.warning(f"Task {task_id}: Completed with warning — {stitch_warning}")
                else:
                    logger.info(f"Task {task_id}: Completed successfully")

                if self.socketio:
                    self.socketio.emit('task_completed', {
                        'task_id': task_id,
                        'status': 'completed',
                        'warning': stitch_warning
                    })

        except Exception as e:
            logger.error(f"Task {task_id} execution failed: {e}")

            # Update task status to failed
            try:
                cursor.execute('''
                    UPDATE tasks
                    SET status = 'failed', error_message = ?, completed_at = ?
                    WHERE id = ? AND status NOT IN ('cancelled', 'paused')
                ''', (str(e), utc_now_iso(), task_id))

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
