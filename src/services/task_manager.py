"""
Task Manager Service

Manages download task lifecycle including creation, execution, pause/resume, and deletion.
Coordinates between database, download engine, and WebSocket notifications.
"""

import logging
import os
import shutil
import threading
import asyncio
import time
from typing import Optional, Dict, Any, List, Tuple

from src.contracts.artifact import Artifact, ArtifactKind
from src.contracts.outcome import (ACTIVE_STATE_VALUES, GAP_OUTCOMES,
                                   RESUMABLE_STATE_VALUES, RETRYABLE_OUTCOMES,
                                   TaskState, TileOutcome, outcome_from_db)
from src.contracts.region import RegionSpec, RegionValidationError
from src.contracts.region_tiles import count_region_tiles
from src.contracts.reservation import ResourceKind, ResourceRequest
from src.contracts.source import SourceSnapshot
from src.core.database import get_connection, parse_db_timestamp, utc_now, utc_now_iso
from src.models.task import Task, Tile
from src.services import artifact_store, disk_budget, source_registry
from src.services.download_engine import (DownloadEngine, StitchCancelled,
                                          WARN_TILES_THRESHOLD)
from src.services.config_manager import ConfigManager
from src.services.geo_validation import require_absolute_output_dir, sanitize_filename
from src.services.resource_scheduler import (get_scheduler,
                                             plan_download_reservation)
from src.services.source_registry import STYLE_CODES
from src.services.task_cleanup import (fail_stranded_running_task,
                                       resolve_stored_output_dir)
from src.services.task_logging import open_task_log
from src.services.download_speed import SpeedMeter

logger = logging.getLogger(__name__)

# 人类样式名 → Google vt 的 lyrs 码。**表本身住在 source_registry**,这里只是
# 别名 —— 缓存目录名由这张表的值派生(`cache/<style>-<fingerprint>/`,见
# SourceSnapshot.cache_namespace),两处各存一份就等于两套缓存命名空间:
# 建任务时按 A 表算目录、下载时按 B 表算目录,瓦片写进一个目录、读的是另一个,
# 而任何一层日志都解释不了「明明下完了却说缺块」。别名保留是因为本文件里
# STYLE_MAP 的引用点分散在建任务与执行两处,改名的爆炸半径大于收益。
STYLE_MAP = STYLE_CODES

# 兜底 except 里允许被改写成 'failed' 的状态。**正向清单**(本仓约定:
# status 查询从不写反向清单)。= 活动态减去 paused 与 pending_decision:
#   · paused 是用户的明确意图,收尾兜底不该把它抢走;
#   · pending_decision 已经**落库了一份结算**(gap_tiles + task_tiles 里逐块的
#     结局分类),它和终态一样是「话已说完、等人回话」。收尾阶段任何一处抛异常
#     都会把这份结算洗成一条只剩 error_message 的 failed 行 —— 缺了几块、
#     哪几块是 404 哪几块是超时,全部丢失,而用户恰恰要靠这些才能决定补漏还是
#     接受。今天这条路径不可达(提交之后只剩两个自带 try 的调用),但它不可达
#     是巧合不是设计:在那之后加一行没包 try 的代码就会踩中。
#   · 三个终态(completed / completed_with_gaps / failed)绝不可被改写 ——
#     终态记录被兜底改写过一次就再也说不清任务到底成没成。
#   · retrying 留在清单里:补漏跑挂了就该是 failed,它没有已落库的结算。
FAILABLE_STATE_VALUES = tuple(
    v for v in ACTIVE_STATE_VALUES
    if v not in (TaskState.PAUSED.value, TaskState.PENDING_DECISION.value))

# 补漏 / 接受缺块的准入状态。**不含** pending_decision 以外的活动态 ——
# 一个还在跑的任务没有「缺块」可言,它的 task_tiles 每秒都在变。
REFILLABLE_STATE_VALUES = (
    TaskState.COMPLETED_WITH_GAPS.value,
    TaskState.PENDING_DECISION.value,
    TaskState.FAILED.value,
)

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


def _is_unexplained(outcome: TileOutcome) -> bool:
    """这块缺块算不算「没交代的失败」。

    `failed_tiles` 这一列的语义在改造后收窄成「没交代的失败数」,而不是
    「所有缺块数」—— 缺块总数另有 `gap_tiles`。为什么必须分成两列:一片
    海域的瓦片全是 404,上游明确说过「这里没有数据」,把它算进 failed_tiles
    会让进度条显示「12000 失败」,用户点一百次重试也不会变成 0,而这恰恰是
    唯一正确的结果。反过来,把真实的超时/写盘失败混进 gap_tiles 不再单独
    计数,任务就会带着可修复的洞被当成正常成品。
    """
    return outcome.is_gap and not outcome.is_explained


class TaskStillStoppingError(ValueError):
    """上一轮执行线程尚未收尾时的启动拒绝。

    继承 ValueError 是刻意的:routes/api.py 的 start/resume 两个端点都按
    ValueError 统一映射成 4xx + str(e),这条错误因此不需要改路由层就能带着
    自己的文案回到前端。区别只在文案 —— 见 start_task 里的判定注释。
    """


class TaskManager:
    """
    Task manager for orchestrating map download tasks

    Manages task lifecycle from creation through execution to completion.
    Provides pause/resume capabilities with real-time progress updates.

    Features:
        - Task creation with tile calculation
        - Background task execution with threading
        - Pause/resume controls with stop flags
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

        # 已经被删除、但工作线程还没收工的 task_id。唯一用途见 _write_progress_batch：
        # map 是四条管线里唯一在运行期 INSERT 的（失败瓦片写 task_tiles），父行删掉
        # 后那条 INSERT 会撞外键 —— 实测 INSERT OR IGNORE 不豁免外键约束。
        # 另外三条管线运行期只有 UPDATE ... WHERE id=?，对不存在的行是静默 no-op，
        # 所以它们不需要墓碑，别为了对称加。
        self._deleting: set[int] = set()

        # 全局资源凭据,按 task_id。**必须**在 _state_lock 下读写:准入
        # (start_task)与归还(_run_task 的 finally)在不同线程,而
        # ResourceScheduler 对「同一个 owner 重复 reserve」是直接抛 ValueError
        # 的 —— 上一张没摘干净,下一次启动就永远起不来。
        self._reservations: Dict[int, Any] = {}


        # 本轮准入的结论(磁盘判决 + 授予的配额),按 task_id。它只是**待写入
        # 任务日志的便签**:准入发生在 start_task(还没有任务日志句柄),而
        # §4.5 要求「任何终态都能从日志解释原因」—— 一个因为配额只给到 3 条
        # 连接而跑了十小时的任务,那个 3 必须在它自己的日志里。
        self._admission: Dict[int, dict] = {}
        # 本次运行期间任务在库里的状态。默认 'running';补漏与「接受缺块」
        # 走 'retrying'。收尾的每一条 UPDATE 都用它做 WHERE 条件,所以它必须
        # 与 _start_run 真正写进库的那个值一致 —— 对不上就是「跑完了但状态
        # 没落地」,任务永远停在 retrying。
        self._run_status: Dict[int, str] = {}

        # 补漏的目标瓦片集合 {(zoom, x, y)}。非 None 时 _execute_task 只下载
        # 集合内的瓦片,其余一律不碰 —— 补漏是「补这些洞」,不是「重跑一遍」。
        # 空集合 = 一块都不下(「接受缺块」那条路:只跑拼接与复制)。
        self._refill_targets: Dict[int, set] = {}

        # 已被用户「接受缺块」的任务。它关掉两道严格闸门:逐层的缺块拦截
        # (否则永远拼不出来)与完成判定的 pending_decision(否则决策白做)。
        # §13-3「默认严格,导出显式」的显式那一半就是这个集合。
        self._gap_accepted: set[int] = set()

        # Any task still marked 'running' in the DB at this point must be an
        # orphan from a previous process — no thread can have survived a restart.
        # Demote them so the UI stops reporting them as live, and so their
        # accumulated running time doesn't keep ticking against wall-clock.
        self._recover_orphan_running_tasks()

        logger.debug("TaskManager initialized")

    def _recover_orphan_running_tasks(self) -> None:
        """启动时把孤儿 'running' 降级成 'paused'、孤儿 'retrying' 降级成
        'pending_decision'。

        At __init__ time self.active_tasks is empty, so any tasks.status='running'
        row is guaranteed to be a leftover from a crashed/restarted process.
        We flip them to 'paused' (the existing pause_task semantics) and append a
        'pause' time record so _update_total_running_time on a future resume
        doesn't fold the dead-process gap into total_running_seconds.
        We deliberately do NOT call _update_total_running_time here: we cannot
        know when the process actually died, so adding wall-clock since the last
        'start' would overstate the runtime.

        'retrying' 需要单独一条:它**不在** RESUMABLE_STATE_VALUES 里,降级成
        paused 会让「点恢复」把一个本该等用户决策的任务洗成普通续传,决策界面
        就此消失。回落到 pending_decision 才是它崩溃前的真实处境 —— 洞还在,
        补漏没跑完,决策仍然没做。它也不写 pause 时间记录:那一轮不是「暂停」,
        是没跑完。
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM tasks WHERE status = 'running'")
            orphan_ids = [row['id'] for row in cursor.fetchall()]
            cursor.execute("SELECT id FROM tasks WHERE status = ?",
                           (TaskState.RETRYING.value,))
            retry_ids = [row['id'] for row in cursor.fetchall()]
            if not orphan_ids and not retry_ids:
                return

            now = utc_now_iso()
            if orphan_ids:
                cursor.executemany(
                    "UPDATE tasks SET status = 'paused' WHERE id = ? AND status = 'running'",
                    [(tid,) for tid in orphan_ids],
                )
                cursor.executemany(
                    "INSERT INTO task_time_records (task_id, action, timestamp) VALUES (?, 'pause', ?)",
                    [(tid, now) for tid in orphan_ids],
                )
            if retry_ids:
                cursor.executemany(
                    "UPDATE tasks SET status = ?, error_message = ? WHERE id = ? AND status = ?",
                    [(TaskState.PENDING_DECISION.value, '补漏被进程中断,缺块仍待处理',
                      tid, TaskState.RETRYING.value) for tid in retry_ids],
                )
            conn.commit()
            if orphan_ids:
                logger.warning(
                    f"Recovered {len(orphan_ids)} orphan 'running' task(s) to 'paused': {orphan_ids}"
                )
            if retry_ids:
                logger.warning(
                    f"Recovered {len(retry_ids)} orphan 'retrying' task(s) to "
                    f"'pending_decision': {retry_ids}"
                )
            # 上面两条 warning 只进**全局**日志,而进程崩溃 / 断电 / 关窗口是
            # 四条管线里最常发生的真实终态转移。用户排查时打开的是
            # logs/tasks/map_<id>.log —— 那份日志在崩溃的那一瞬间戛然而止,最后
            # 一行是某个 zoom 的进度,既没有终态也没有任何解释,任务看起来是凭空
            # 消失的。§4.5 的门槛是「任何终态都能从**任务自己的**日志解释原因」,
            # 所以这一笔必须补在这里。
            # 这里没有现成的日志句柄:恢复跑在 __init__、按 id 列表批处理,那时
            # 一个任务线程都还不存在。按 id 各开一个短命句柄写完就关是可以接受
            # 的 —— open_task_log 用 delay=True,不写就不建文件,而这里必写。
            for tid in orphan_ids:
                self._log_recovery(
                    tid, 'paused',
                    '进程在本任务运行期间退出(崩溃 / 断电 / 关窗口):重启时'
                    '发现库里还写着 running 而没有任何线程,已降级为 paused。'
                    '已下载的瓦片都在缓存里,点「恢复」从断点继续。')
            for tid in retry_ids:
                self._log_recovery(
                    tid, TaskState.PENDING_DECISION.value,
                    '进程在补漏 /「接受缺块」那一轮运行期间退出:重启时发现库里'
                    '还写着 retrying 而没有任何线程,已回落为 pending_decision。'
                    '缺块还在,决策界面会重新出现。')
        except Exception as e:
            logger.error(f"Failed to recover orphan running tasks: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _log_recovery(self, task_id: int, status: str, note: str) -> None:
        """把一次「启动时孤儿恢复」写进**这个任务自己的**日志。绝不抛。

        绝不抛是硬要求:调用点在 `__init__` 里,一个次要 sink 的环境问题没有
        资格让整个 TaskManager 构造不出来 —— 那等于一条日志写不动就让服务起
        不来(同 `open_task_log` 类 docstring 的论证)。
        """
        try:
            tlog = open_task_log('map', task_id, self.config_manager)
            try:
                tlog.event('terminal', status=status, reason='process_restart')
                tlog.warning('%s', note)
            finally:
                tlog.close()
        except Exception as e:
            logger.warning(f"Task {task_id}: 孤儿恢复日志写入失败(忽略): {e!r}")

    def _is_stop_requested(self, task_id: int, stop_flag: Optional[threading.Event] = None) -> bool:
        flag = stop_flag or self.stop_flags.get(task_id)
        return bool(flag and flag.is_set())

    def _stream_copy_tile(self, tile: Tile, source, output_base,
                          made_dirs: set, made_dirs_lock: threading.Lock) -> bool:
        """边下边复制的单瓦片写入:cache -> 产物目录,原子 .part + replace。

        与结尾复制阶段共用「同尺寸已存在即跳过」的幂等判定 —— 恢复/对账重跑
        不会复写。保留 copy2 字面量:取消钩子测试 monkeypatch copy2 作为取消
        触发器(见结尾复制段注释),即时复制与补拷线程同样走它,取消语义不变。
        临时名 `.part.<pid>.<thread_ident>`:pid 段是 task_cleanup 那张
        `*.part.*` 登记表的解析口径(`_part_owner_pid` 只认 pid 在前),线程 id
        段让下载回调(事件循环线程)与补拷线程的临时件不互踩。
        """
        src = tile.cache_path(source)
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
        tmp = dest.with_name(f"{dest.name}.part.{os.getpid()}.{threading.get_ident()}")
        try:
            shutil.copy2(src, tmp)
            tmp.replace(dest)
        except BaseException:
            # 任务产物目录不在任何启动清扫根里(清扫只走 CACHE_DIR),生产者
            # 自己不清就没有第二次机会 —— 磁盘满/目标盘掉线会在每个
            # {z}/{x} 目录下留一堆 .part 文件,且下次复制的同尺寸判定看的是
            # dest 不是它们,永远不会被覆盖。
            tmp.unlink(missing_ok=True)
            raise
        return True

    @staticmethod
    def _status_count_deltas(old_status, new_status) -> tuple[int, int]:
        """(downloaded_delta, failed_delta)。两个参数都是 `TileOutcome` 值字符串
        或 None(= 这块瓦片本次运行前没有任何已计数状态)。

        `failed_delta` 只数**没交代的**缺块(见模块级 `_is_unexplained`):
        `no_data` 进 gap_tiles 但不进 failed_tiles,否则一片全是 404 的海域会
        让进度条永远显示一个用户无论如何都消不掉的失败数。
        """
        old = outcome_from_db(old_status) if old_status is not None else None
        new = outcome_from_db(new_status)
        downloaded_delta = (int(new is TileOutcome.SUCCESS)
                            - int(old is TileOutcome.SUCCESS))
        failed_delta = (int(_is_unexplained(new))
                        - int(old is not None and _is_unexplained(old)))
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

    @staticmethod
    def _region_from_param(raw) -> RegionSpec:
        """`params['region']` → `RegionSpec`。三种写法都收。

        - `RegionSpec.to_dict()`(前端把上一次的选区原样传回来);
        - GeoJSON(Feature / FeatureCollection / Geometry,导入面板的产物);
        - 以上任意一种的 JSON 字符串(表单字段只能是字符串)。

        分派靠 `type`:只有 `MultiPolygon` 走 `from_dict`。
        `from_dict` 与 `RegionSpec.to_dict()` 是一对 —— 它读 `coordinates`
        (三层嵌套:多边形 → 环 → 点)并把同级的 `bbox` 当**权威值**,而
        `from_geojson` 会丢掉 bbox 从顶点重算,跨反经线的选区两者结果不同。
        裸 GeoJSON 的 `MultiPolygon` 嵌套层数与它一致,所以同一条路能收两种。

        其它 type(`Polygon` 只有两层嵌套、`Feature`/`FeatureCollection` 连
        `coordinates` 都没有)一律走 `from_geojson`。**不能靠「先试 from_dict、
        失败再退」**:少一层嵌套时它抛的是 `TypeError`('int' object is not
        subscriptable),不是 `RegionValidationError`,一个裸 Polygon 会直接
        变成 500 而不是被正确解析。

        Raises:
            RegionValidationError: 解析不出任何面。它是 ValueError 的子类,
                路由层按 ValueError 映射成 400 + 原文。
        """
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode('utf-8')
        if isinstance(raw, str):
            import json as _json
            try:
                raw = _json.loads(raw)
            except ValueError as exc:
                raise RegionValidationError(f"invalid region JSON: {exc}") from None
        if not isinstance(raw, dict):
            raise RegionValidationError("region must be a GeoJSON object or a RegionSpec dict")
        if raw.get('type') == 'MultiPolygon' and raw.get('coordinates'):
            try:
                return RegionSpec.from_dict(raw)
            except (RegionValidationError, TypeError, IndexError) as exc:
                raise RegionValidationError(
                    f"invalid MultiPolygon coordinates: {exc}") from None
        return RegionSpec.from_geojson(raw, source='imported')

    def create_task(self, params: dict) -> int:
        """
        Create a new download task in database

        Args:
            params: Task parameters dictionary containing:
                - name: Task name
                - north, south, east, west: Geographic bounds
                - region: 可选。GeoJSON / `RegionSpec.to_dict()` / 以上的 JSON
                  字符串。给了它就以它为准,四至列改由它的外接矩形派生;
                  没给就按四至造一个矩形 RegionSpec —— **任何任务都有区域,
                  区别只在几何是不是矩形**,这样下游只有一条枚举路径。
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
            1. 归一出 RegionSpec(矩形或多边形)
            2. 冻结 SourceSnapshot(URL 模板 + 服务器列表 + 指纹)
            3. 按区域真实几何计数(不物化,不写 task_tiles 行)
            4. 算一份磁盘估算并记日志 —— **只提示,不拒绝**
            5. 插入 tasks 行
        """
        logger.info(f"Creating task: {params.get('name', 'Unnamed')}")

        # output_path 校验:必须是绝对路径且至少两级深度(0.2.4 起放开全盘,
        # 不再要求落在 Config.DOWNLOADS_DIR 内 —— 见 require_absolute_output_dir),
        # 否则抛 ValueError(API 层映射 400)。
        # 入库的是校验后的绝对路径(与 dem_task_manager.create_task 同口径)——
        # 存原始相对值的话,_execute_task 里 Path(task.output_path) 会按进程
        # CWD 解析,打包 exe 从快捷方式启动(CWD≠BASE_DIR)时文件写到校验范围外。
        output_path = require_absolute_output_dir(params['output_path'])

        # 区域先归一,四至后派生。顺序反过来(先造 Task 再解析 region)会让
        # 「前端传了多边形、四至字段却是旧值」这种载荷静默按旧四至下载。
        raw_region = params.get('region')
        spec = self._region_from_param(raw_region) if raw_region else None
        if spec is not None:
            # 跨反经线**不再拒绝**。以前这里直接抛「请拆成东西两个任务」,而
            # 同一个 179..181E 的选区在 /api/region/import(200 + 警告)、
            # /api/region/estimate(200)、/api/dem/tasks(201,两颗颗粒)都是收的
            # —— 四个入口对同一块地给三种答案,只有地图管线是那个异类。
            #
            # 现在四至列写的是 spec.bbox,跨界时 east 落在 (180, 360](例如
            # 181.0)。**这个形状是合法的**,dem_tasks 早就在库里这么存了;
            # 看到 east=181.0 的人请顺着这三跳看下去:
            #   · 校验:models/task.py 的 Task.__post_init__ 拿本行的 region_spec
            #     当判据放行(并对账 east 必须逐字等于 RegionSpec.bbox 的东界);
            #   · 枚举:contracts/region_tiles.py 的 iter_region_tile_spans 按
            #     RegionSpec.antimeridian_parts 拆成东西两段、按行合并再产出
            #     (z0 不会重复出同一块瓦片,y 仍严格升序);
            #   · 落盘:Tile.cache_path 拿到的 x 已经是 `x % n` 回绕后的合法瓦片号。
            north, south, east, west = spec.bbox
        else:
            north, south, east, west = (params['north'], params['south'],
                                        params['east'], params['west'])

        # Create Task object for validation
        # 传原始值,由 Task.__post_init__ 里的 validate_bbox/validate_zoom 统一
        # 转换+校验 —— 在这里先 float()/int() 的话,None/列表会抛 TypeError
        # 变成 500,"abc" 的报错也不带字段名。
        #
        # region_spec 必须在**构造时**就交给 Task,不能等 INSERT 时再单独写列:
        #   · 它是 __post_init__ 判「east>180 合不合法」的唯一判据(见
        #     models/task.py),晚一步就等于跨界任务永远过不了自己的校验;
        #   · 在此之前这个对象生下来就与它写出去的行不一致 —— 行里有 region,
        #     内存里的 task.region_spec 是空串,而 to_dict() 会把这个空串当成
        #     「这个任务没有区域」发给前端。
        task = Task(
            name=params['name'],
            status='pending',
            north=north,
            south=south,
            east=east,
            west=west,
            zoom_min=params['zoom_min'],
            zoom_max=params['zoom_max'],
            style=params['style'],
            output_format=params['output_format'],
            output_path=output_path,
            region_spec=spec.to_json() if spec is not None else ''
        )

        if spec is None:
            # 画框任务:几何**就是**那个矩形。source='drawn' 让日志与 UI 能
            # 区分「用户画的」和「导入的」—— 两者的可信度不同(导入的边界
            # 可能有几万个顶点、可能自相交)。
            # 派生完立刻回填 task.region_spec:下面 INSERT 写的就是 spec.to_json(),
            # 内存态与落库态从这一刻起逐字一致(理由同上)。
            spec = RegionSpec.from_bbox(task.north, task.south, task.east,
                                        task.west, source='drawn')
            task.region_spec = spec.to_json()

        # 下载源在**建任务这一刻**冻结:URL 模板、服务器列表、样式码、指纹。
        # 改造前 tasks 表关于源只有一列 style,真实 URL 是请求时现展开的 ——
        # 用户跑到一半在设置页换了 tile_servers,同一个成品里就混进了两个来源
        # 的瓦片,事后无法回答「这块瓦片是谁给的」。指纹还决定缓存命名空间,
        # 所以换源自动换目录,两个源不会互相投毒。
        # 通用缝:调用方可以把**已经冻结好的**快照直接传进来(params 的
        # source_snapshot,JSON 文本),核心只认这份合同、不认它出自哪个数据源;
        # 没传就按 style 从 tile_servers 配置现算,与改造前逐字一致。
        # SourceSnapshot.from_json 对空串/坏 JSON 返回 None,回落是天然的。
        style_code = STYLE_MAP.get(task.style, 'm')
        snapshot = (
            SourceSnapshot.from_json(params.get('source_snapshot') or '')
            or source_registry.snapshot_for_style(style_code, self.config_manager)
        )

        # 瓦片总数只计数、不物化,也不向 task_tiles 写任何行。
        # 为什么不再每块瓦片存一行:瓦片集合是 (区域, zoom) 的纯函数,可由
        # DownloadEngine.iter_region_tiles 按确定性顺序随时重建;完成态以磁盘
        # cache 文件为准(cache 即真相),task_tiles 退化为只存缺块的稀疏表。
        # 旧实现 50 万块瓦片就是 50 万次 INSERT,是建任务的主要瓶颈。
        #
        # **按区域真实几何计数**,不按外接矩形。多边形任务因此「算出来的数」
        # 就是「真正要下的数」—— GeoD 那条「按 bbox 计费、按多边形出图」的
        # 裂缝在这里合上:用户看到的预估不再是实际的好几倍,而下载与预估共用
        # count_region_tiles / iter_region_tile_spans 同一份扫描线实现。
        total_tiles = count_region_tiles(spec, task.zoom_min, task.zoom_max)
        logger.info(f"Calculated {total_tiles} tiles for task ({spec.summary()})")

        # 瓦片数软阈值:超过 WARN_TILES_THRESHOLD 只记警告,不拒绝创建。
        # 0.1.4 起放开硬上限 —— 是否继续由用户在前端确认(下载弹窗会显示
        # 预计瓦片数与耗时,超阈值时要求二次确认);服务端不替用户做决定。
        if total_tiles > WARN_TILES_THRESHOLD:
            logger.warning(
                f"Task tile count {total_tiles} exceeds soft threshold "
                f"{WARN_TILES_THRESHOLD}; allowed (user confirmed in UI)"
            )

        # 磁盘估算:**只记录,不拒绝**。软阈值那一段的理由在这里同样成立 ——
        # 服务端不替用户做决定,而且此刻用户可能正打算腾空间。真正的硬闸门在
        # start_task 里(那时用户已经按下开始,拒绝才有意义,而且拒绝发生在
        # 状态翻转之前、不需要任何补偿)。估算失败也不拦:一个算不出来的数字
        # 不该挡住建任务。
        try:
            estimate = disk_budget.estimate_map_task(
                spec, task.zoom_min, task.zoom_max, task.output_format, style_code)
            logger.info(
                f"Disk estimate for new task: peak={estimate.peak_bytes} B "
                f"(cache={estimate.cache_bytes}, output={estimate.output_bytes}, "
                f"temp={estimate.temp_bytes}, network={estimate.network_bytes}) "
                f"over {estimate.tile_count} tiles")
        except Exception as e:
            logger.warning(f"Disk estimate failed for new task (ignored): {e!r}")

        # MBTiles 是**正交的追加产物**,不是第四种 output_format(理由见
        # src/services/artifact_export.py 的模块 docstring:容器是从松散 XYZ
        # 目录打包来的,把它做成 output_format 的一个取值就等于为了拿容器
        # 把原料和 /tiles/<id>/ 预览一起砍掉)。所以它是一个独立开关列,
        # output_format 的语义一个字都没变。
        export_mbtiles = 1 if params.get('export_mbtiles') else 0

        # Insert task into database
        conn = get_connection()
        try:
            cursor = conn.cursor()

            # Insert task。四至列继续写(spec.bbox 派生):历史列表、统计与
            # 足迹渲染都读它们,而**存量行只有它们** —— region_spec 是本次
            # 改造新加的列,老任务那里是空串,靠 RegionSpec.from_row 从四至
            # 兜底还原。两者同时写,读侧才有「新列优先、旧列兜底」可用。
            cursor.execute('''
                INSERT INTO tasks (
                    name, status, north, south, east, west,
                    zoom_min, zoom_max, style, output_format, output_path,
                    total_tiles, downloaded_tiles, failed_tiles,
                    region_spec, source_snapshot, source_fingerprint,
                    gap_tiles, gap_decision, export_mbtiles
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                task.name, task.status, task.north, task.south, task.east, task.west,
                task.zoom_min, task.zoom_max, task.style, task.output_format, task.output_path,
                total_tiles, 0, 0,
                spec.to_json(), snapshot.to_json(), snapshot.fingerprint,
                0, '', export_mbtiles
            ))

            task_id = cursor.lastrowid
            logger.info(
                f"Task created with ID: {task_id} "
                f"(source {snapshot.fingerprint}, namespace {snapshot.cache_namespace})")

            conn.commit()

            return task_id

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to create task: {e}")
            raise
        finally:
            conn.close()

    def _estimate_disk_verdict(self, task_row) -> Any:
        """启动前的磁盘估算判决。**只记录,不拒绝**:不通过也返回 verdict 照常启动
        (拦截语义 2026-08 起移除,见 disk_budget 模块 docstring),但 reason 里那
        四个数字(可用 / 需要 / 保留 / 缺口)必须进日志 —— 真写满盘时那就是现场。

        估算按**剩余工作量**:总量减去已经在缓存里的部分(用 downloaded_tiles
        这个已对账过的计数当命中数,而不是在这里 stat 几十万个文件 —— 那会让
        「点开始」卡住整个 _state_lock)。续传一个 95% 完成的任务不该被按全量
        报数。

        估算本身失败(配置脏、区域还原不出来)时返回 None 并记警告:一个算不出来
        的数字连记录的价值都没有。

        返回值随后被 `_reserve_download_quota` 兑换成一笔 DISK_BYTES 预留,让
        并发任务的判决互相看得见(数字不失真)。

        `export_mbtiles` 必须原样传给估算器:勾了「同时导出 MBTiles」的任务会在
        收尾时再写一个**与整份松散镜像同量级**的容器文件,不算进去的话日志里的
        「需要」会比真实峰值小一倍。
        """
        task_id = task_row['id']
        try:
            spec = RegionSpec.from_row(task_row, source='drawn')
            if spec is None:
                logger.warning(f"Task {task_id}: 区域还原不出来,跳过磁盘估算")
                return None
            cached = max(0, int(task_row['downloaded_tiles'] or 0))
            estimate = disk_budget.estimate_map_task(
                spec, task_row['zoom_min'], task_row['zoom_max'],
                task_row['output_format'], STYLE_MAP.get(task_row['style'], 'm'),
                cached_tiles=cached,
                export_mbtiles=bool(task_row['export_mbtiles']))
        except Exception as e:
            logger.warning(f"Task {task_id}: 磁盘估算失败({e!r}),跳过磁盘估算")
            return None

        output_dir = resolve_stored_output_dir(task_row['output_path'])
        verdict = disk_budget.check_budget(output_dir, estimate, self.config_manager)
        if verdict.ok:
            logger.info(f"Task {task_id} 磁盘估算:{verdict.reason}")
        else:
            # 不拦 —— 用户有权在快满的盘上硬跑,但不能是不知情地跑。
            logger.warning(f"Task {task_id} 磁盘估算超出可用空间,照常启动:{verdict.reason}")
        return verdict

    def _reserve_download_quota(self, task_id: int, verdict) -> Any:
        """向调度器申请任务槽 + 连接数 + **磁盘字节**。拿不到就抛 `ValueError` 并说清是谁满了。

        改造前没有任何跨任务上界:每个任务各自开满 concurrent_downloads 条连接,
        四个任务并行就是四倍,机器被自己打死。凭据里的 NETWORK 份额随后经
        `download_tiles_batch(max_concurrency=...)` 落到信号量与 TCPConnector 上。

        `verdict` 是 `_estimate_disk_verdict` 刚给出的磁盘判决(估算失败时是
        None)。它**必须**在这里被兑换成一笔 DISK_BYTES 预留 —— `check_budget`
        的 docstring 把话说死了:判决之后不预留,这次判决对下一个任务就不可见。
        不预留的话两个地图任务会对着同一份「剩余 20 GB」各报各的乐观数字,
        真写满盘时两边的任务日志都写着「空间够」,事后谁也解释不了。

        DISK_BYTES 在调度器里是**只记账、不设限**的种类(见 resource_scheduler
        模块 docstring),所以它永远全额授予,不会成为拒绝的原因;它的全部作用
        是让**下一个**任务的 check_budget 从剩余空间里先把这一份扣掉。

        Raises:
            ValueError: 配额不足。**必须点名是哪一种资源满了** —— 「启动失败」
                四个字会让用户去关防火墙、重启程序,而真正该做的是等一个任务跑完。
        """
        scheduler = get_scheduler(self.config_manager)
        try:
            requested = max(1, int(self.config_manager.get('concurrent_downloads', '10')))
        except (TypeError, ValueError):
            requested = 10
        owner = ('map', task_id, 'download')
        plan = plan_download_reservation(requested)

        required_bytes = 0
        if verdict is not None:
            try:
                required_bytes = max(0, int(verdict.required_bytes or 0))
            except (TypeError, ValueError):
                required_bytes = 0
        if required_bytes:
            # DISK_BYTES 是「全额或不给」的种类,minimum 必须等于 requested,
            # 否则 ResourceRequest.__post_init__ 直接拒收 —— 半个磁盘预算既
            # 拦不住写满,又会让下一个任务读到一个偏小的已预留量。
            plan = plan + [ResourceRequest(kind=ResourceKind.DISK_BYTES,
                                           requested=required_bytes,
                                           minimum=required_bytes)]

        reservation = scheduler.reserve(owner, plan)
        if reservation is not None:
            return reservation

        snapshot = scheduler.snapshot()
        saturated = [
            f"{kind}({snapshot['in_use'].get(kind, 0)}/{limit})"
            for kind, limit in snapshot['limits'].items()
            if limit is not None and snapshot['available'].get(kind) == 0
        ]
        detail = '、'.join(saturated) if saturated else '可分配份额低于最低要求'
        raise ValueError(
            f"资源配额已满,任务 {task_id} 暂时无法启动:{detail}。"
            f"等一个正在运行的任务结束后重试,或在设置里调高对应上限。")

    def start_task(self, task_id: int):
        """
        Start a download task

        Args:
            task_id: Task ID to start

        Raises:
            TaskStillStoppingError: If the previous run's thread is still
                winding down (row already flipped to paused/pending)
            ValueError: If the task is genuinely running, its status is neither
                pending nor paused, or the global scheduler has no quota left
            sqlite3.Error: If database operation fails
        """
        self._start_run(task_id, RESUMABLE_STATE_VALUES, TaskState.RUNNING.value)

    def _start_run(self, task_id: int, allowed_statuses, run_status: str,
                   *, synchronous: bool = False,
                   refill_targets: Optional[set] = None,
                   gap_accepted: bool = False):
        """启动一轮执行。`start_task` / `refill_task` / `accept_gaps` 的共同实现。

        allowed_statuses
            准入白名单(**正向清单**)。普通启动是 `RESUMABLE_STATE_VALUES`;
            补漏与「接受缺块」各自带自己的那一份。
        run_status
            本轮期间写进库的状态(`running` / `retrying`)。收尾的每一条 UPDATE
            都以它做 WHERE 条件,所以它同时被记进 `self._run_status`。
        synchronous
            True 时在**调用线程**里跑完整轮执行,而不是起后台线程。
            「接受缺块」用它 —— 那条路要在返回前给出最终状态(见 accept_gaps),
            而它跑的只有拼接与复制,没有下载。
        refill_targets
            本轮的待下目标集合;`None` = 不限目标(全量),空集合 = 一块都不下。
            **由本方法在锁里装进 `self._refill_targets`**,不接受调用方先装好
            再进来 —— 理由见下面安装点那一段。
        gap_accepted
            本轮是否关掉严格缺块闸门(「接受缺块」那条路)。同上,由本方法装。

        准入顺序是刻意的:磁盘估算与配额都在 `_state_lock` 里、在
        `UPDATE ... status=<run_status>` **之前**做。配额拒绝时不需要任何
        补偿动作 —— 状态没改、时间记录没写、线程没建,库里干干净净。
        (下面那段 status_committed 的补偿路径因此在拒绝路径上永远走不到,
        它只服务「commit 之后、线程启动之前」那个窄窗口。)
        """
        logger.info(f"Starting task {task_id} (run_status={run_status})")

        # 状态翻转 commit 之后、thread.start() 之前还有若干可能抛异常的调用
        # (get_current_running_time、socketio.emit)。一旦抛出,except 里的
        # rollback 对已 commit 的事务无效,会留下 status=running 但线程从未
        # 启动的假运行任务 —— 用这两个标志在 except 里显式回补状态。
        status_committed = False
        thread_started = False
        reservation = None
        # 线程对象要在 except 里做身份比较(「登记在册的是不是我这一轮」),
        # 所以名字必须在 try 之前绑定:锁段里任何一行抛出都不能让它未绑定。
        thread = None

        conn = get_connection()
        try:
            cursor = conn.cursor()

            with self._state_lock:
                active_thread = self.active_tasks.get(task_id)
                thread_alive = bool(active_thread and active_thread.is_alive())

                cursor.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"Task {task_id} not found")
                status = row['status']

                # 「线程还活着」有两种完全不同的含义,必须分开报:
                #  · 库里也是 running —— 真的在跑,拒绝重复启动(旧文案不变);
                #  · 库里已是 paused/pending —— pause_task 已提交状态并置了停止
                #    标志,但上一轮线程还卡在没有取消点的收尾步骤里(最长的是
                #    单个大 zoom 的 gdal.Translate)。此时沿用「already running」
                #    会让界面显示「已暂停」而每次点恢复都收到一条与之直接矛盾
                #    的报错,用户只能盲目重试十几分钟;报「正在停止,稍后重试」
                #    才与界面一致,也才提示了正确的动作。
                if thread_alive:
                    if status == 'running':
                        raise ValueError(f"Task {task_id} is already running")
                    raise TaskStillStoppingError(
                        f"Task {task_id} is still stopping from its previous run "
                        f"(finishing the current stitch/copy step); "
                        f"please retry in a few seconds"
                    )

                # 只准白名单里的状态。默认那份(RESUMABLE_STATE_VALUES)是
                # pending/paused:'failed' 曾经也放行,为的是让失败任务直接重按
                # 开始就续传。但失败是终态,重跑一条终态记录等于把它曾经失败过
                # 这件事从历史里擦掉;'pending_decision' 也刻意不在里面 —— 它
                # 必须走专门的决策端点(补漏 / 接受缺块),否则一次误点「继续」
                # 就把「等你决定」洗成普通 running,用户再也看不到这个任务需要
                # 决策(理由与形制见 contracts/outcome.py 的 RESUMABLE_TASK_STATES)。
                if status not in allowed_statuses:
                    raise ValueError(
                        f"Cannot start task {task_id} with status '{status}'. "
                        f"Task must be one of {', '.join(allowed_statuses)}."
                    )

                # ---- 磁盘估算(只记录) → 配额(真的会拒)。都在状态翻转之前 ----
                verdict = self._estimate_disk_verdict(row)
                reservation = self._reserve_download_quota(task_id, verdict)
                self._admission[task_id] = {
                    'verdict': verdict,
                    'reservation': reservation,
                }

                # started_at 只在首次启动时写入(COALESCE):resume/重试不再
                # 覆写 —— 字段语义是「首次开始时间」,运行时长另有
                # task_time_records + total_running_seconds 跟踪,不依赖它。
                placeholders = ', '.join('?' for _ in allowed_statuses)
                cursor.execute(f'''
                    UPDATE tasks
                    SET status = ?, started_at = COALESCE(started_at, ?)
                    WHERE id = ? AND status IN ({placeholders})
                ''', (run_status, utc_now_iso(), task_id, *allowed_statuses))
                if cursor.rowcount != 1:
                    raise ValueError(f"Task {task_id} could not be started because its status changed")

                conn.commit()
                status_committed = True

                self._reservations[task_id] = reservation
                self._run_status[task_id] = run_status

                stop_flag = threading.Event()
                self.stop_flags[task_id] = stop_flag
                if synchronous:
                    # 同步路径:本线程就是执行线程。登记它,_run_task 的 finally
                    # 才认得出「注销的是自己」(它比较的是 current_thread)。
                    thread = threading.current_thread()
                else:
                    thread = threading.Thread(
                        target=self._run_task,
                        args=(task_id, stop_flag),
                        daemon=True,
                        name=f"Task-{task_id}"
                    )
                self.active_tasks[task_id] = thread

                # 「本轮只补这些洞 / 本轮接受缺块」与「这一轮归我」是**同一个
                # 事实**,所以两个开关装在这里 —— 与状态翻转、线程登记同一把锁、
                # 同一段临界区。曾经它们由 accept_gaps / refill_task 在自己那把
                # 锁里先装、再调本方法,于是两段临界区之间隔着 gap_decision 的
                # commit、一次时间记录、一次 SELECT 和一次 socketio.emit,而线程
                # 登记在这一行。第二次点击落进这个窗口时,对面那道「上一轮线程还
                # 活着吗」的闸门读到的线程尚未登记,放行 —— 它覆写
                # `_refill_targets[task_id]` 装上自己那份,随后被下面的状态白名单
                # 拒绝,而它 except 里的身份比较**认出的正是自己刚装的那一份**,
                # 于是撤掉。撤的是第一轮正在用的开关。实测 6 次并发命中 3 次:
                # 严格闸门重新武装(有洞的 zoom 全部跳过拼接)、产物一个不出、
                # 任务从 completed_with_gaps 退回 pending_decision、所有缺块被
                # 重新登记,而 tasks.gap_decision 已经写着 'accept'。
                # 挪到这里之后,拒绝路径在拿到锁之前就被挡住,根本走不到安装,
                # 也就没有什么需要撤 —— 那两个 except 分支因此整段删掉了。
                # `None` / False 也**显式**写进去而不是「不管」:上一轮若因进程
                # 异常没走 finally 而留下残余,一次普通启动会继承它的目标集合,
                # 变成「只下上一轮那几块」的静默半量运行。
                if refill_targets is None:
                    self._refill_targets.pop(task_id, None)
                else:
                    self._refill_targets[task_id] = refill_targets
                if gap_accepted:
                    self._gap_accepted.add(task_id)
                else:
                    self._gap_accepted.discard(task_id)

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

            if synchronous:
                thread_started = True
                self._run_task(task_id, stop_flag)
                logger.info(f"Task {task_id} finished (synchronous run)")
            else:
                thread.start()
                thread_started = True
                logger.info(f"Task {task_id} started in background thread")

        except Exception as e:
            conn.rollback()
            if status_committed and not thread_started:
                # commit 之后、thread.start() 之前的异常(get_current_running_time、
                # socketio.emit 等)会留下 status=running 但线程从未启动的假
                # 运行任务;rollback 对已 commit 无效,显式把状态回补为 failed,
                # 并清掉 active_tasks/stop_flags 里从未启动的登记。
                with self._state_lock:
                    self.active_tasks.pop(task_id, None)
                    self.stop_flags.pop(task_id, None)
                    # 两个开关与线程登记同生同死:装它们的那段临界区就在上面,
                    # 而这条补偿路径只服务「commit 之后、线程启动之前」那个窄
                    # 窗口 —— 那时行已经是 run_status(running / retrying),
                    # 任何一个白名单都不含它,别人起不了新的一轮,所以撤掉的
                    # 必定是本次调用自己装的那一份。
                    self._refill_targets.pop(task_id, None)
                    self._gap_accepted.discard(task_id)
                try:
                    # 回补成**启动前的那个状态**,而不是 failed。两条理由:
                    #  · failed 是终态,start_task 的白名单里没有它 —— 一次
                    #    socketio 抽风就能把一个 pending 任务永久钉死,用户只剩
                    #    「删了重建」。dem(:356)与 contour(:805)在同一位置回落到
                    #    可恢复的状态,地图这里必须对齐。
                    #  · 补漏 / 接受缺块跑的那两轮,行原本是 pending_decision。把
                    #    它洗成 failed 会连带毁掉已经落库的缺块结算(哪几块是
                    #    404、哪几块是超时),而 accept_gaps 只认 pending_decision
                    #    —— 洗过之后「接受缺块」这条路永久不可达。本文件顶上
                    #    FAILABLE_STATE_VALUES 那 15 行注释论证的就是这件事,
                    #    这段代码曾经是它的反例。
                    # 回补的是 status 一列:error_message 不动。pending_decision
                    # 行的 error_message 装着缺块摘要,那是决策界面唯一的说明文字;
                    # 而「为什么没起来」这句已经随异常抛回给调用方、也进了日志。
                    # WHERE 只认 run_status —— 正向清单,且只可能命中本次调用刚
                    # 写进去的那个状态,绝不会改到别人翻过的行。
                    cursor.execute('''
                        UPDATE tasks
                        SET status = ?
                        WHERE id = ? AND status = ?
                    ''', (status, task_id, run_status))
                    conn.commit()
                except Exception as revert_error:
                    logger.error(
                        f"Failed to revert task {task_id} status after start failure: {revert_error}"
                    )
            if not thread_started:
                # 拒绝路径与「起不来」路径共用这一段,但**只还本次调用自己申请到
                # 的那一张**:拒绝路径上 reservation 是 None,那就一张都不还。
                # 旧实现在这里按 owner 键做「兜底回收」,于是用户重复点一次
                # 「开始」被正确拒绝之后,顺手把**正在服役的**那张凭据释放了 ——
                # 任务继续用着 50 条连接跑,调度器账上一格没占,全局上界失效
                # (完整论证见 ResourceScheduler.release_owner 的 docstring)。
                self._release_reservation(task_id, reservation)
            logger.error(f"Failed to start task {task_id}: {e}")
            raise
        finally:
            conn.close()

    def _release_reservation(self, task_id: int, reservation) -> None:
        """归还**这一次执行自己拿到的那张**凭据,并清掉与之配对的本轮状态。

        幂等,绝不抛。`reservation` 是必填的:

        * `None` = 本次调用一格资源都没申请到(准入拒绝路径)。那就**什么都不
          还、什么都不清**。这不是防御性编程,是一条线上级缺陷的修复:旧实现
          先 `self._reservations.pop(task_id)` 再按 owner 键调
          `release_owner(owner)`,于是 —— 任务 1 正在跑,用户重复点一次「开始」,
          闸门正确地抛出「已在运行」,而这个 except 顺手把**还在服役的**那张
          凭据释放了。实测形状:`IN_USE {'network':50,'task_slot':1}` → 重复
          点击 → `IN_USE {'network':0,'task_slot':0}`,任务照跑不误。
        * 非 None = 拿**身份**(不是键)去比。登记在册的不是这一张,就说明它属于
          别人那一轮,一个字节都不许动。

        旧 docstring 把「按键兜底」说成是防泄漏的必需品。那个角色**故意取消了**:
        每一次成功的 reserve 都由同一个 finally 配对归还,配不上对的唯一情形是
        进程死掉,而配额是进程内状态,跟着一起没了。为一个不存在的泄漏保留一个
        能误删活凭据的后门,代价远大于收益(见 release_owner 的 docstring)。
        """
        with self._state_lock:
            # `_reservations` / `_run_status` / `_admission` 三份是同一轮的账,
            # 在 _start_run 的同一个锁段里一起写入,所以它们要么一起属于本轮、
            # 要么一起属于别人 —— 身份比对一次,三份一起清。
            if self._reservations.get(task_id) is reservation:
                self._reservations.pop(task_id, None)
                self._run_status.pop(task_id, None)
                self._admission.pop(task_id, None)
        if reservation is None:
            return
        try:
            # release_owner 在调度器那一侧再做一次身份比较,所以即便本轮凭据
            # 早已被别处还过、owner 上挂的是新的一张,这一句也只是无害的 no-op。
            get_scheduler(self.config_manager).release_owner(
                ('map', task_id, 'download'), reservation)
        except Exception as e:
            logger.warning(f"Task {task_id}: 归还资源凭据失败(忽略): {e!r}")

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

            # 'retrying' 也要能暂停:补漏可以是几万块瓦片、跑上几十分钟,
            # 只认 'running' 就等于告诉用户「补漏一旦开始就停不下来」。
            # 暂停之后状态是 paused,走的是普通续传路径 —— 那是对的,
            # 剩下的洞在 task_tiles 里原样留着,续传完照样走完成判定。
            cursor.execute('''
                UPDATE tasks
                SET status = 'paused'
                WHERE id = ? AND status IN (?, ?)
            ''', (task_id, TaskState.RUNNING.value, TaskState.RETRYING.value))

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

    def delete_task(self, task_id: int, artifact_dir=None, on_row_gone=None,
                    clear_cache: bool = False):
        """删除任务。没在跑就同步删，在跑就置停止标志 + 后台收尾。

        砍掉「取消」之后这是唯一的销毁动作，任何状态都能调 —— 不再要求调用方
        先把运行中的任务停下来。

        on_row_gone 由调用方给：清 /tiles 静态路由缓存的那个 hook 依赖 Flask
        请求上下文（走 current_app.extensions），放在这里等于让服务层持有一个
        只对路由调用方有效的回调 —— 非路由调用方那里它会静默失效，比不放更糟。
        另外三条管线的清缓存也都留在路由层，这里跟着同一套约定。

        clear_cache 原样转给 `delete_task_row`：它在 `_state_lock` 里、DELETE
        **之前**取任务行与幸存任务行的快照,行没了之后再让
        `cache_exclusive.clear_task_exclusive_cache` 只删「只有这个任务在用」的
        瓦片。判独占必须在删行前取样 —— 行一旦没了就再也算不出它占了哪些瓦片,
        而按 bbox 硬删会把邻居任务的缓存一起清掉。
        """
        from src.services.task_deletion import delete_task_row

        return delete_task_row(
            manager=self,
            task_id=task_id,
            table="tasks",
            artifact_dir=artifact_dir,
            tombstone=self._deleting,
            on_row_gone=on_row_gone,
            clear_cache=clear_cache,
        )

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

            # 活动态清单来自 contracts/outcome.ACTIVE_STATE_VALUES,不在这里
            # 抄字面量:它现在含 retrying 与 pending_decision —— 后者尤其重要,
            # 一个等待用户决策的任务占着产物目录和缓存引用,从活动列表里消失
            # 就等于让用户永远看不到「有东西在等你」。
            placeholders = ', '.join('?' for _ in ACTIVE_STATE_VALUES)
            cursor.execute(f'''
                SELECT * FROM tasks
                WHERE status IN ({placeholders})
                ORDER BY created_at DESC
            ''', ACTIVE_STATE_VALUES)

            rows = cursor.fetchall()

            tasks = []
            for row in rows:
                # 同 get_task_status:读取路径走 from_row,一条历史非法行
                # 不能把整个列表接口打成 500。
                tasks.append(Task.from_row(row).to_dict())

            return tasks

        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 缺块决策与产物导出(§13-3:默认严格,导出显式)
    # ------------------------------------------------------------------

    def _emit_gap_decision(self, task_id: int, status: str, gap_tiles: int,
                           by_outcome: dict) -> None:
        """广播 `task_gap_decision`。emit 故障降级成日志,绝不影响已落库的状态。

        这是本次改造**唯一**新增的 socket 事件。刻意不加「每条日志一发」那种
        事件:本应用没有 room 也没有 namespace,一次 emit 就是发给所有客户端,
        日志尾随只能走 REST 轮询(见 task_logging 与本批次的接口约定)。
        """
        if not self.socketio:
            return
        try:
            self.socketio.emit('task_gap_decision', {
                'task_id': task_id,
                'task_type': 'map',
                'status': status,
                'gap_tiles': gap_tiles,
                'by_outcome': dict(by_outcome),
            })
        except Exception as e:
            logger.warning(f"Task {task_id}: emit task_gap_decision failed (ignored): {e!r}")

    def _wants_mbtiles(self, task_id: int) -> bool:
        """建任务时是否勾了「同时导出 MBTiles」。读不出来按否 —— 少一个附加
        产物是小事,为一次读库失败把已完成的任务判失败不是。"""
        conn = get_connection()
        try:
            row = conn.execute(
                'SELECT export_mbtiles FROM tasks WHERE id = ?', (task_id,)).fetchone()
            return bool(row and row['export_mbtiles'])
        except Exception as e:
            logger.warning(f"Task {task_id}: 读 export_mbtiles 失败(按未勾选处理): {e!r}")
            return False
        finally:
            conn.close()

    def _register_artifacts(self, task_id: int, output_dir, task,
                            stitched_zooms, *, has_gaps: bool, tlog=None) -> None:
        """登记本次产出的 XYZ 目录与每层 GeoTIFF。**绝不抛。**

        `has_gaps` 跟着**产物**走而不是跟着任务状态走:任务行可以被删、产物
        可以被保留,而「这张图上有洞」这件事必须活得比任务行久(§13-3
        「成果与历史永久带缺块标记」)。

        登记失败只记警告 —— 产物文件已经在盘上了,少一行索引不该把一个跑完的
        任务改写成失败。
        """
        try:
            task_dir = output_dir / f"task_{task_id}"
            artifacts = []

            if task_dir.is_dir():
                total_bytes, file_count, minzoom, maxzoom = artifact_store.measure_dir(
                    task_dir, extensions=('.png',))
                if file_count:
                    artifacts.append(Artifact(
                        pipeline='map', task_id=task_id, kind=ArtifactKind.XYZ_DIR,
                        path=str(task_dir), fmt='png',
                        bytes_total=total_bytes, tile_count=file_count,
                        minzoom=minzoom, maxzoom=maxzoom, has_gaps=has_gaps,
                        meta={'output_format': task.output_format},
                        created_at=utc_now_iso(),
                    ))

            safe_name = sanitize_filename(task.name)
            for zoom in stitched_zooms:
                tif = task_dir / f"{safe_name}_zoom_{zoom}.tif"
                try:
                    size = tif.stat().st_size
                except OSError:
                    # 拼接报成功但文件不在了(被用户删了、盘掉线)。不登记,
                    # 也不因此判任务失败 —— 登记表描述的是「现在盘上有什么」。
                    continue
                artifacts.append(Artifact(
                    pipeline='map', task_id=task_id, kind=ArtifactKind.GEOTIFF,
                    path=str(tif), fmt='tif', bytes_total=size,
                    minzoom=zoom, maxzoom=zoom, has_gaps=has_gaps,
                    meta={'zoom': zoom, 'epsg': 4326},
                    created_at=utc_now_iso(),
                ))

            recorded = artifact_store.record_artifacts(artifacts)
            if tlog is not None:
                tlog.event('artifacts', recorded=recorded, total=len(artifacts),
                           has_gaps=has_gaps)
        except Exception as e:
            logger.warning(f"Task {task_id}: 产物登记失败(不影响任务状态): {e!r}")

    def gap_summary(self, task_id: int) -> dict:
        """任务的缺块摘要。给决策界面与 `GET /api/tasks/<id>/gaps` 用。

        返回:
            task_id / total / by_outcome / explained / decision / status / samples

        `by_outcome` 的四个键**恒定存在**(零也写出来):前端渲染一张固定的
        分类表,键时有时无会让它每次都要判空。
        `explained` 为真 = 所有洞都是 `no_data`,即上游明确回答过「这里没有」
        —— 那种任务不需要决策,补漏也补不出东西来。
        `samples` 最多 20 条:一个百万瓦片的任务可能有几万个洞,把它们全塞进
        一个 JSON 响应只会把浏览器卡死,而用户要的是「大概是哪一片、什么原因」。

        Raises:
            ValueError: 任务不存在。
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT status, gap_decision FROM tasks WHERE id = ?', (task_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Task {task_id} not found")

            gap_values = tuple(o.value for o in GAP_OUTCOMES)
            placeholders = ', '.join('?' for _ in gap_values)
            cursor.execute(
                f'''SELECT status, COUNT(*) AS c FROM task_tiles
                    WHERE task_id = ? AND status IN ({placeholders})
                    GROUP BY status''',
                (task_id, *gap_values))
            counts = {r['status']: r['c'] for r in cursor.fetchall()}
            by_outcome = {value: int(counts.get(value, 0)) for value in gap_values}
            total = sum(by_outcome.values())

            cursor.execute(
                f'''SELECT zoom, x, y, status, error_message FROM task_tiles
                    WHERE task_id = ? AND status IN ({placeholders})
                    ORDER BY zoom, y, x LIMIT 20''',
                (task_id, *gap_values))
            samples = [
                {
                    'zoom': r['zoom'], 'x': r['x'], 'y': r['y'],
                    'outcome': r['status'],
                    'error': r['error_message'] or '',
                }
                for r in cursor.fetchall()
            ]

            explained = all(
                outcome_from_db(value).is_explained
                for value, count in by_outcome.items() if count
            )
            return {
                'task_id': task_id,
                'total': total,
                'by_outcome': by_outcome,
                'explained': bool(explained),
                'decision': row['gap_decision'] or '',
                'status': row['status'],
                'samples': samples,
            }
        finally:
            conn.close()

    def _retryable_gap_keys(self, task_id: int) -> set:
        """稀疏表里值得重试的缺块坐标。

        `RETRYABLE_OUTCOMES` = retryable_failure + cache_failure。
        `no_data` 与 `permanent_failure` 不在里面:前者上游说过没有,后者是
        403/400 这类改不了的答复,再问一遍只是把用户的时间与上游的配额一起烧掉。
        """
        values = tuple(o.value for o in RETRYABLE_OUTCOMES)
        placeholders = ', '.join('?' for _ in values)
        conn = get_connection()
        try:
            rows = conn.execute(
                f'''SELECT zoom, x, y FROM task_tiles
                    WHERE task_id = ? AND status IN ({placeholders})''',
                (task_id, *values)).fetchall()
            return {(r['zoom'], r['x'], r['y']) for r in rows}
        finally:
            conn.close()

    def refill_task(self, task_id: int) -> None:
        """补漏:只重下**值得重试**的那些洞,然后走完正常的拼接与完成判定。

        允许的起点是 `REFILLABLE_STATE_VALUES`(completed_with_gaps /
        pending_decision / failed)。状态置 `retrying`,待下集合被限制成
        `_retryable_gap_keys` 的那一批,其余一切(准入、日志、进度、拼接、
        完成判定、产物登记)**复用同一条执行路径** —— 不另起一个执行器。
        另起一个的代价不是重复代码,而是两套完成判定:补漏那套迟早会漏掉
        某个分支,然后出现「补完漏了但任务还停在 pending_decision」。

        它**是一次运行**,所以照样要过磁盘估算与配额(`_start_run` 里那一套):
        补漏可以是几万块瓦片,凭「这是补漏」就免配额等于给资源上界开后门。

        幂等:任务已经在跑(含正在补漏)时,`_start_run` 的线程存活检查会拒绝
        第二次调用;没有可重试的洞时直接拒绝,不空跑一轮。

        Raises:
            ValueError: 状态不允许,或没有任何值得重试的缺块。
        """
        conn = get_connection()
        try:
            row = conn.execute(
                'SELECT status FROM tasks WHERE id = ?', (task_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            raise ValueError(f"Task {task_id} not found")
        if row['status'] not in REFILLABLE_STATE_VALUES:
            raise ValueError(
                f"Cannot refill task {task_id} with status '{row['status']}'. "
                f"Task must be one of {', '.join(REFILLABLE_STATE_VALUES)}.")

        targets = self._retryable_gap_keys(task_id)
        if not targets:
            raise ValueError(
                f"Task {task_id} 没有可重试的缺块:剩下的洞要么是 no_data"
                f"(上游明确没有数据),要么是永久性错误,重试不会有不同结果。"
                f"如果要带着这些洞出成品,请使用「接受缺块」。")

        # 这道闸门只为**报一句对的话**而存在,它一个字节的状态都不动。本轮的
        # `_refill_targets` 由 `_start_run` 在翻状态、登记线程的同一段临界区里装
        # (论证见那里的注释):安装与「这一轮归我」必须是同一个事实,否则第二次
        # 点击会在两段临界区之间的窗口里装上自己那份、被拒、再按身份比较撤掉 ——
        # 撤的是第一轮正在用的那份,那一轮从此 targets 为 None = 不限目标的全量
        # 重下。既然这里不装,拒绝路径上也就没有什么需要撤,except 整段删掉。
        with self._state_lock:
            active = self.active_tasks.get(task_id)
            if active is not None and active.is_alive():
                raise TaskStillStoppingError(
                    f"Task {task_id} 的上一轮执行还没结束,补漏现在起不来;"
                    f"请过几秒再试。")
        self._set_gap_decision(task_id, 'refill')
        self._start_run(task_id, REFILLABLE_STATE_VALUES,
                        TaskState.RETRYING.value, refill_targets=targets)
        logger.info(f"Task {task_id}: refill started over {len(targets)} tile(s)")

    def accept_gaps(self, task_id: int) -> dict:
        """接受缺块:把 `pending_decision` 推进到 `completed_with_gaps`,并跑完
        严格模式当初拒绝跑的拼接与复制。

        §13-3 的「导出显式」就是这个动作。严格模式默认不出带洞的产物,因为一张
        「打得开、看着正常、四至却偏小」的 GeoTIFF 是最难被发现的一类缺陷;
        但用户看过缺块摘要之后有权说「我知道,就要它」—— 那时产物出,并且
        **永久带 has_gaps 标记**(标记在 Artifact 上,跟着文件走)。

        **同步执行**:它跑完拼接与复制才返回,返回的摘要因此是最终状态而不是
        一个中间态。待下集合是空的(不下任何瓦片),所以这一轮的耗时就是拼接
        与复制本身。

        Returns:
            `gap_summary(task_id)`,状态已是终态。

        Raises:
            ValueError: 状态不是 `pending_decision`。
        """
        allowed = (TaskState.PENDING_DECISION.value,)
        with self._state_lock:
            # 这道闸门只为**报一句对的话**而存在,它一个字节的状态都不动
            # (原来它顺手装 `_refill_targets` / `_gap_accepted`,那正是竞态的
            #  源头 —— 完整机制见下面那段和 `_start_run` 里的安装点)。
            active = self.active_tasks.get(task_id)
            if active is not None and active.is_alive():
                # 明确报错,而**不是**幂等地回一份「进行中的摘要」。本方法的契约
                # 是同步跑完再返回,返回值被 docstring 和前端当作**终态**摘要;
                # 此刻行还是 retrying,把它当终态渲染就是撒谎(用户会看到一个
                # 「已完成、带缺块」的面板,而拼接还在跑,产物尚未落地)。
                # 文案也刻意不提「重试」:决定已经落库,唯一该做的动作是等。
                raise ValueError(
                    f"Task {task_id} 的上一轮执行还没结束,现在不能接受缺块;"
                    f"请等它跑完再看结果 —— 如果这是重复点击,这个决定已经"
                    f"记下了,不需要再点一次。")
        # 两个开关交给 `_start_run` 在它翻状态、登记线程的那同一段临界区里装:
        #  · 空集合(而不是 None)= 一块瓦片都不下 —— 这一轮只跑拼接与复制;
        #  · gap_accepted 关掉逐层缺块拦截与完成判定的 pending_decision。
        # 上面那把锁与 `_start_run` 的锁之间隔着 gap_decision 的 commit、一次
        # 时间记录、一次 SELECT 和一次 socketio.emit,线程登记在第二段末尾;
        # 在这里装开关就等于把它们暴露在这个窗口里,而第二次点击一旦落进去就会
        # 装上自己那份、被拒、再撤掉第一轮正在用的那份(实测 6 次命中 3 次)。
        self._set_gap_decision(task_id, 'accept')
        self._start_run(task_id, allowed, TaskState.RETRYING.value,
                        synchronous=True, refill_targets=set(),
                        gap_accepted=True)
        return self.gap_summary(task_id)

    def _set_gap_decision(self, task_id: int, decision: str) -> None:
        """记下用户对缺块做了什么决定。它是**历史**的一部分,不是临时状态 ——
        三个月后回头看「这张图为什么有洞」,答案是「当时我按了接受」。"""
        conn = get_connection()
        try:
            conn.execute('UPDATE tasks SET gap_decision = ? WHERE id = ?',
                         (decision, task_id))
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Task {task_id}: 写 gap_decision 失败: {e}")
            raise
        finally:
            conn.close()

    def export_mbtiles(self, task_id: int, *, has_gaps=None) -> dict:
        """把任务的 XYZ 产物目录打包成 MBTiles,登记 Artifact,返回摘要。

        **薄委托**:真正的打包在 `src/services/artifact_export.py`,影像与等高线
        共用那一个写入端(§10 阶段 2 的门槛原话是「同一套写入端能产出影像库与
        等高线库」)。把它实现在这里就等于让等高线要么没有、要么是第二份。

        has_gaps 不传就按任务行推断,推断规则住在 `artifact_export`(见那里的
        `_infer_has_gaps`)。标记落在 Artifact 上,跟着文件走。

        Returns:
            `{path, tile_count, minzoom, maxzoom, bytes, has_gaps, ...}`

        Raises:
            ValueError: 任务不存在、没有瓦片目录、目录里一块瓦片都没有
                (`ExportError` 是 ValueError 的子类,路由层的 400 映射原样生效)。
        """
        from src.services import artifact_export

        # has_gaps 原样转发(**不要 bool()**,那会把 None 压成 False):一条规则
        # 两个调用方 —— 跑完自动导出(建任务时勾的 tasks.export_mbtiles)和成果页
        # 的手动导出按钮 POST /api/export/...。两边各自推断时就分叉过:按钮那条路
        # 什么都不传,导出的容器被登记成无缺块,紧挨着写着 True 的 xyz_dir /
        # geotiff 兄弟行。规则只留一份,在写入端。
        return artifact_export.export_task_mbtiles(
            'map', task_id, has_gaps=has_gaps)

    def _run_task(self, task_id: int, stop_flag: Optional[threading.Event] = None):
        """一轮执行的外壳:开每任务日志 → 跑协程 → 注销 + 归还配额 + 关日志。

        Args:
            task_id: Task ID to execute

        每任务日志在这里开、在这里关(而不是在 `_execute_task` 里):§4.5 的门槛是
        「任何终态都能从日志解释原因」,而**线程本身异常退出**也是一种终态 ——
        那条 `except` 就在这一层,协程里的 `with` 语句根本轮不到。
        `open_task_log` 永不返回 None,所以下面不需要一个 `if tlog:`。
        """
        logger.info(f"Task {task_id} thread started")
        failure = None
        tlog = open_task_log('map', task_id, self.config_manager)
        # 准入结论在进 try 之前就取出来:下面的 finally 要拿 reservation 做
        # 身份比较的归还,而 try 里任何一行抛出都不能让这个名字变成未绑定。
        admission = self._admission.get(task_id) or {}
        reservation = admission.get('reservation')
        try:
            # 准入结论先落日志:配额与磁盘判决决定了这一轮能跑多快、为什么能跑,
            # 而它们是在 start_task 里(还没有日志句柄时)算出来的。
            if reservation is not None:
                tlog.event('reservation',
                           granted=reservation.summary(),
                           network=reservation.network)
            verdict = admission.get('verdict')
            if verdict is not None:
                tlog.event('disk_budget', ok=verdict.ok,
                           free=verdict.free_bytes, required=verdict.required_bytes,
                           reserve=verdict.reserve_bytes, reason=verdict.reason)
            tlog.event('state', status=self._run_status.get(task_id, 'running'))
            asyncio.run(self._execute_task(task_id, stop_flag, tlog=tlog))
        except Exception as e:
            logger.error(f"Task {task_id} thread failed: {e}")
            tlog.exception('执行线程异常退出:%s: %s', type(e).__name__, e)
            failure = e
        finally:
            with self._state_lock:
                deregistered = (
                    self.active_tasks.get(task_id) is threading.current_thread())
                run_status = self._run_status.get(task_id, TaskState.RUNNING.value)
                if deregistered:
                    self.active_tasks.pop(task_id, None)
                if stop_flag is None or self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)
                self._refill_targets.pop(task_id, None)
                self._gap_accepted.discard(task_id)
            if deregistered:
                # 配额必须在注销的同一时刻还回去。传进去的是**本轮自己拿到的
                # 那张**凭据:调度器按身份比较,所以一个迟到收尾的线程不会把
                # 用户刚刚恢复的那一轮的凭据摘掉。这个窗口不是理论上的 ——
                # 紧接着的 fail_stranded_running_task 会新开一条
                # busy_timeout=30s 的 sqlite 连接,慢盘上它能撑几十秒。
                self._release_reservation(task_id, reservation)
                if run_status == TaskState.RUNNING.value:
                    # 行还停在 running 就是搁死了(理由与竞态分析见 helper 的 docstring)。
                    stranded_reason = (f'线程异常: {failure}' if failure is not None
                                       else '')
                    if fail_stranded_running_task('tasks', task_id, stranded_reason):
                        # 补偿**真的改了行**(running → failed)才写这一笔。
                        # 那个 helper 只写全局日志,而 §4.5 的门槛是「任何终态都
                        # 能从**任务自己的**日志解释原因」—— 没有下面两行,任务
                        # 日志的最后一句是 `EVENT thread_finished failed=False`,
                        # 库里却写着 failed:两份记录当面打架,排查无从下手。
                        # 用返回值而不是猜:helper 的 UPDATE 带 WHERE status=
                        # 'running',正常收尾时它是无害的 no-op,那种情况下多写
                        # 一条「已判 failed」比不写更糟。
                        tlog.event('terminal', status='failed',
                                   reason='thread_stranded',
                                   detail=stranded_reason or 'worker exited without settling the row')
                        tlog.error(
                            '线程退出时任务行仍停在 running,已由兜底判为 failed:%s',
                            stranded_reason or 'worker 没有走到任何终态写入')
                else:
                    # 补漏 / 接受缺块跑的是 'retrying',而
                    # `fail_stranded_running_task` 只认 'running'(它是四条管线
                    # 共用的兜底,状态清单不归本文件管)。搁死的 retrying 行同样
                    # 需要一张网:没有它,一次线程异常就把任务永久钉在 retrying
                    # —— 那个状态既不在 RESUMABLE 里也不在 REFILLABLE 里,用户
                    # 每个按钮都被拒,只剩重启进程。
                    #
                    # 回落到 pending_decision 而不是 failed:洞在补漏之前就存在,
                    # 补漏没跑成不代表任务失败,它只是回到了「等你决定」。
                    self._recover_stranded_retry(task_id, failure, tlog=tlog)
            tlog.event('thread_finished', failed=failure is not None)
            tlog.close()
            logger.info(f"Task {task_id} thread finished")

    def _recover_stranded_retry(self, task_id: int, failure=None, *, tlog) -> None:
        """线程退出时行仍停在 'retrying' → 回落到 'pending_decision'。绝不抛。

        竞态与 `fail_stranded_running_task` 同源:行只要还是 retrying 就不可能
        有新 worker 被登记(`_start_run` 的白名单里没有 retrying),所以
        `WHERE status='retrying'` 命中的必然是搁死的那一行;正常收尾已经把它
        改成终态了,是无害的 no-op。

        `tlog` 是必填的本轮每任务日志句柄:这条补偿**真的改了行**就是一次终态
        写入,而它以前只写全局日志 —— 用户打开任务日志,最后一句是
        `EVENT thread_finished failed=False`,库里却写着 pending_decision 和一句
        「补漏未完成」,两份记录当面打架,排查无从下手(§4.5)。写不写看
        rowcount 而不是猜:no-op 时多写一条「已回落」比不写更糟。
        """
        note = f"补漏未完成({failure})" if failure is not None else "补漏未完成"
        conn = get_connection()
        try:
            cur = conn.execute(
                "UPDATE tasks SET status = ?, error_message = ? "
                "WHERE id = ? AND status = ?",
                (TaskState.PENDING_DECISION.value, note, task_id,
                 TaskState.RETRYING.value))
            conn.commit()
            if cur.rowcount:
                logger.warning(
                    f"Task {task_id}: 线程已退出而行仍是 retrying,已回落 pending_decision")
                tlog.event('terminal', status=TaskState.PENDING_DECISION.value,
                           reason='retry_thread_stranded', detail=note)
                tlog.warning(
                    '线程退出时任务行仍停在 retrying,已回落 pending_decision:%s', note)
        except Exception as e:
            conn.rollback()
            logger.error(f"Task {task_id}: retrying 搁死补偿失败: {e!r}")
        finally:
            conn.close()

    def _write_progress_batch(self, progress_conn, task_id, batch) -> None:
        """纯 IO:把摘下来的批次落库。跑在工作线程(见 flush_progress_async)。

        一个批次窗口内同一块瓦片只会被上报一次(每次运行每块瓦片只
        下载一回),所以按操作类型分组 executemany 与逐条执行的最终
        结果一致。崩溃最多丢一个批次的缺块行:对应瓦片没有 cache
        文件,恢复时自然重下,失败了会重新登记,语义与计数攒批相同。

        它本来是 _execute_task 里的闭包。提成方法只为了让墓碑短路可测 ——
        闭包够不着，短路被误删时没有任何用例会红。自由变量只有
        progress_conn 和 task_id 两个，搬运是机械的。
        """
        if task_id in self._deleting:
            # 任务已被删除，父行不在了。这批进度写不进去也不该写进去 ——
            # 直接丢弃，不要走 _restore_progress_batch 退回队列（那会让
            # pending_tile_inserts 单调增长到下载结束）。
            return
        inserts, updates, deletes, downloaded, failed = batch
        if inserts:
            # status 写**真实的 outcome**,不再是恒定的 'failed' 字面量。
            # 那个字面量把「上游说这里没有数据」和「我们的盘写不进去」压成了
            # 同一个词,于是补漏只能二选一:要么一遍遍去问上游明确说过没有的
            # 坐标,要么放着真实故障不管。批次元组因此多带一列。
            progress_conn.executemany('''
                INSERT OR IGNORE INTO task_tiles
                    (task_id, zoom, x, y, status, retry_count, error_message)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            ''', inserts)
        if updates:
            # 重试同一块瓦片:结局可能变了(上次超时、这次 404),所以 status
            # 一起更新 —— 只累加 retry_count 会让缺块摘要停留在第一次的分类上。
            progress_conn.executemany('''
                UPDATE task_tiles
                SET retry_count = retry_count + 1, status = ?, error_message = ?
                WHERE task_id = ? AND zoom = ? AND x = ? AND y = ?
            ''', updates)
        if deletes:
            progress_conn.executemany('''
                DELETE FROM task_tiles
                WHERE task_id = ? AND zoom = ? AND x = ? AND y = ?
            ''', deletes)
        if downloaded or failed:
            progress_conn.execute('''
                UPDATE tasks
                SET downloaded_tiles = MAX(downloaded_tiles + ?, 0),
                    failed_tiles = MAX(failed_tiles + ?, 0)
                WHERE id = ?
            ''', (downloaded, failed, task_id))
        if inserts or deletes:
            # gap_tiles 恒等于稀疏表的行数,所以**重算而不是增量累加**:增量
            # 在「INSERT OR IGNORE 撞上已存在的行」和「DELETE 打在不存在的行上」
            # 这两种 no-op 上都会算多,而计数一旦偏了就再也回不来(它是缺块
            # 决策的输入)。一条相关子查询的代价远低于一个会漂移的计数器。
            progress_conn.execute('''
                UPDATE tasks
                SET gap_tiles = (SELECT COUNT(*) FROM task_tiles WHERE task_id = ?)
                WHERE id = ?
            ''', (task_id, task_id))
        progress_conn.commit()

    async def _execute_task(self, task_id: int, stop_flag: Optional[threading.Event] = None,
                            *, tlog=None):
        """
        Execute download task asynchronously

        Args:
            task_id: Task ID to execute

        Process:
            1. Classify the tile set: cache hits are materialised into
               completed_tiles, the rest is only counted — the pending set is
               re-derived lazily as a generator (see _iter_pending_tiles)
            2. Stream-copy to the output dir as tiles land (progress_callback),
               plus a backfill thread for cache-hit tiles — both concurrent
               with the download
            3. Call download_engine.download_tiles_batch() with that generator
            4. Check stop_flag between operations
            5. If output_format includes image, call stitch_tiles_with_gdal for
               each zoom (the stop flag is threaded in — it has its own
               cancellation point)。缺块**不再**一律跳过整层:只有「没交代的」
               缺块才跳,`no_data` 是上游明确的答复,那一层照拼并永久标记
            6. Final copy pass — mostly a same-size reconciliation after step 2
            7. 完成判定:无缺块 → completed;只有 no_data → completed_with_gaps;
               有没交代的缺块 → pending_decision(**不是** failed,产物不出)

        Error Handling:
            - Catches exceptions and updates task status to 'failed'
            - Logs error message to database
            - Emits error notification via socketio
        """
        logger.info(f"Executing task {task_id}")
        if tlog is None:
            # 直调路径(测试、将来的其它入口)。open_task_log 永不返回 None,
            # 所以下游一律不需要 `if tlog:`。这里开出来的句柄没人关 —— 直调
            # 本来就是例外路径,正常路径由 _run_task 负责成对开关。
            tlog = open_task_log('map', task_id, self.config_manager)

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

            # 下载源:优先用建任务时冻结的快照,存量行(source_snapshot 为空)
            # 现推一份。**下载、拼接、复制三段共用它** —— 缓存命名空间由它的
            # 指纹决定,三段各算一次就可能算出两个目录(比如中途有人改了
            # tile_servers),那正是「下载完了却说缺块」的成因。
            source = source_registry.snapshot_for_task_row(task_row, self.config_manager)
            style_code = STYLE_MAP.get(task.style, 'm')  # Default to roadmap if not found
            tlog.event('source', summary=source.summary(),
                       namespace=source.cache_namespace)

            # 区域:region_spec 列优先,存量行从四至还原(RegionSpec.from_row
            # 已实现这条兜底)。还原不出来才退回四至枚举 —— 那说明这一行连
            # 四至都是坏的,任务本来就跑不了。
            region = RegionSpec.from_row(task_row, source='drawn')
            if region is None:
                raise ValueError(f"Task {task_id} 的区域无法还原(region_spec 与四至都不可用)")
            tlog.event('region', summary=region.summary(),
                       polygons=region.polygon_count, holes=region.hole_count)

            # 本轮的运行态与两个开关。补漏只下 refill_targets 里的瓦片;
            # 「接受缺块」把严格闸门关掉(见 _gap_accepted 的说明)。
            run_status = self._run_status.get(task_id, TaskState.RUNNING.value)
            refill_targets = self._refill_targets.get(task_id)
            gaps_accepted = task_id in self._gap_accepted
            if refill_targets is not None:
                tlog.event('refill', targets=len(refill_targets))
            if gaps_accepted:
                tlog.event('gap_decision', decision='accept')

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

            # 待下载集合不再查 task_tiles(它现在只是缺块的稀疏表):瓦片集合
            # 是 (区域, zoom) 的纯函数,用 iter_region_tiles 按确定性顺序重建;
            # 磁盘 cache 里已存在且非空的文件就是已完成 —— cache 文件才是完成态
            # 的真相。缺块天然没有 cache 文件,暂停/崩溃后恢复自然只补缺口。
            #
            # 顺带把稀疏缺块表的现有行读成 {key: outcome}(它是稀疏的,行数 =
            # 缺块数,不会随选区膨胀):
            #   · 枚举遇 cache 命中且该瓦片在表里 → 记入待清理清单。这一行的
            #     瓦片其实已成功(cache 落盘),只是当时回调没跑到(stop 检查 /
            #     进程崩溃),清理见枚举后的对账段。**与它记的是哪种 outcome
            #     无关** —— 盘上有文件就不是洞,这是 cache 即真相的直接推论。
            #   · 回调需要旧 outcome 才能算计数增量(见 _status_count_deltas),
            #     所以存的是字典而不是集合。
            gap_rows: Dict[Tuple[int, int, int], str] = {}
            if cache_enabled:
                cursor.execute('''
                    SELECT zoom, x, y, status FROM task_tiles
                    WHERE task_id = ?
                ''', (task_id,))
                gap_rows = {
                    (row['zoom'], row['x'], row['y']): row['status']
                    for row in cursor.fetchall()
                }

            # 单遍枚举产出「已完成」清单与「待下载」计数。**待下载清单刻意不
            # 物化**:它以生成器形态直接喂给 download_tiles_batch(引擎按
            # DOWNLOAD_BATCH_SIZE islice 惰性消费,签名文档写明可传生成器,
            # create_task 也早就用 count_tiles 而不是 calculate_tiles)。硬上限
            # 改成软告警之后,百万级瓦片是被文档化的用法,而一份全网格
            # List[Tile] 就是几百 MB,两个并发任务直接翻倍。
            #
            # completed_tiles 是**唯一**保留的全网格列表,别顺手把它也改成
            # 生成器:拼接要按 zoom 分组遍历它、结尾复制阶段要再遍历一次对账,
            # 而下载回调还在往它追加本次下载成功的瓦片 —— 它必须是一份可以
            # 重复遍历的实体清单。
            def _iter_all_tiles():
                return self.download_engine.iter_region_tiles(
                    region, task.zoom_min, task.zoom_max, task_id=task_id)

            def _wanted(tile: Tile) -> bool:
                """这块瓦片本轮要不要下。

                普通运行:全都要(缓存命中会在下面被剔掉)。
                补漏:只要 refill_targets 里的那些 —— 补漏是「补这些洞」,不是
                「重跑一遍」。剩下的洞里有 no_data(上游说过没有)和
                permanent_failure(403/400,重试只是浪费配额),再问一遍不会有
                不同的答案,而每问一遍都要走完整的超时 × 重试预算。
                空集合 = 一块都不下(「接受缺块」那条路只跑拼接与复制)。
                """
                if refill_targets is None:
                    return True
                return (tile.zoom, tile.x, tile.y) in refill_targets

            completed_tiles: List[Tile] = []
            # 本次运行**新下载**到的瓦片所在的 zoom。拼接短路（产物已存在就跳过）
            # 只有在「这一层一块新瓦片都没有」时才成立 —— 上一轮如果是在缺瓦片的
            # 情况下拼的（旧版本没有下面那道 failed 闸门时会发生），恢复后补齐的
            # 瓦片必然落在这里，于是强制重拼，而不是把那张残缺 mosaic 当成品留下。
            zooms_with_fresh_tiles: set = set()
            # 最后一次进度落库的异常。完成判定必须知道它 —— 见下载循环 finally
            # 里那段说明：计数与失败行都在那一次 flush 里。
            progress_flush_error: Optional[Exception] = None
            cache_hits = 0
            pending_count = 0
            stale_gap_keys: List[Tuple[int, int, int]] = []
            for tile in _iter_all_tiles():
                if cache_enabled:
                    try:
                        # 单次 stat 同时回答「存在吗」与「非空吗」—— 旧写法
                        # exists() + stat() 每块瓦片两次 syscall,恢复枚举
                        # 大任务时是纯浪费。判定语义不变:存在且非空才算命中。
                        # FileNotFoundError 就是「未命中」,不是故障,不打 warning。
                        if tile.cache_path(source).stat().st_size > 0:
                            cache_hits += 1
                            completed_tiles.append(tile)
                            key = (tile.zoom, tile.x, tile.y)
                            if key in gap_rows:
                                stale_gap_keys.append(key)
                            continue
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        logger.warning(
                            f"Task {task_id}: Cache check failed for tile "
                            f"{tile.zoom}/{tile.x}/{tile.y}: {e}"
                        )
                if _wanted(tile):
                    pending_count += 1

            logger.info(
                f"Task {task_id}: {pending_count} tiles to download "
                f"({cache_hits} already in cache)"
            )

            # 命中清单的边界。旧实现在这里 list(completed_tiles) 拷了**第二份
            # 全网格列表**给补拷线程;只记长度即可 —— 下载回调只会往
            # completed_tiles 尾部 append,前 cache_hit_count 项此后不再变动,
            # 按下标读它们与读快照等价(list.append 与按下标读在 GIL 下都是
            # 原子操作,不会读到半个元素)。
            cache_hit_count = len(completed_tiles)

            # 待下载瓦片生成器:按同一确定性顺序重跑枚举,与上面产出的命中
            # 清单做归并 —— 跳过的正是 completed_tiles 的前 cache_hit_count
            # 项,产出集合与旧实现物化的那份 tiles 列表逐块相同(iter_tiles 是
            # bbox+zoom 的纯函数,两遍顺序必然一致,见其 docstring)。
            # 为什么归并而不在这里重新 stat 一遍 cache:重新 stat 会把命中判定
            # 推迟到下载**期间**才发生,另一个并发任务此刻往共享 cache 写入同一
            # 块瓦片,这块就会被静默跳过、永不上报,downloaded_tiles 收尾时少算。
            # 归并只读内存,无此风险,也不多一次 syscall。
            def _iter_pending_tiles():
                hit_index = 0
                for tile in _iter_all_tiles():
                    if hit_index < cache_hit_count:
                        hit = completed_tiles[hit_index]
                        if (hit.zoom, hit.x, hit.y) == (tile.zoom, tile.x, tile.y):
                            hit_index += 1
                            continue
                    # 与上面那趟枚举用**同一个**谓词。两趟的产出必须逐块一致,
                    # 否则 pending_count 与实际下载数对不上,进度条永远差一截。
                    if _wanted(tile):
                        yield tile

            if pending_count == 0:
                logger.info(f"Task {task_id}: No tiles to download, proceeding to stitching")

            # 对账:清掉 cache 命中瓦片残留在稀疏表里的缺块行。
            # 留下它们的场景:瓦片下载成功、cache 已落盘,但进度回调因 stop 检查
            # (暂停/取消)或进程崩溃没能执行 —— 回调里的 DELETE 是唯一清行路径,
            # 它没跑,缺块行就永远留着;而枚举遇 cache 命中直接 continue,不再有
            # 任何路径清它,完成判定的「还有缺块」恒真,任务反复卡住不自愈。
            #
            # **与那行记的是哪种 outcome 无关**:盘上有非空文件就不是洞。哪怕
            # 上一轮把它记成 no_data(上游那次确实说没有),这一轮缓存里躺着一份
            # 有效瓦片,说明它后来是拿到了的(另一个重叠任务下的,或上游补了数据)。
            # cache 文件才是完成态真相(见上文)。executemany 单事务一次 commit。
            if stale_gap_keys:
                cursor.executemany(
                    '''
                    DELETE FROM task_tiles
                    WHERE task_id = ? AND zoom = ? AND x = ? AND y = ?
                    ''',
                    [(task_id, zoom, x, y) for zoom, x, y in stale_gap_keys]
                )
                conn.commit()
                for key in stale_gap_keys:
                    gap_rows.pop(key, None)
                logger.info(
                    f"Task {task_id}: Cleared {len(stale_gap_keys)} stale gap "
                    f"tile row(s) already present in cache"
                )
                tlog.event('gap_rows_cleared', count=len(stale_gap_keys))

            # 计数对账:枚举时已经 stat 过每个 cache 文件,顺带把 tasks 的三个
            # 计数校准成「cache 命中数 / 没交代的缺块数 / 缺块总数」。批量落库
            # 在进程崩溃时最多丢一个批次的计数,恢复时这次对账把它追回来。
            #
            # failed_tiles 与 gap_tiles 分开数,理由见模块级 _is_unexplained:
            # 一片全是 404 的海域进 gap_tiles 但不进 failed_tiles,否则进度条
            # 会显示一个用户无论如何都消不掉的失败数。
            if cache_enabled:
                gap_rows_total = len(gap_rows)
                failed_rows = sum(1 for value in gap_rows.values()
                                  if _is_unexplained(outcome_from_db(value)))
                cursor.execute('''
                    UPDATE tasks
                    SET downloaded_tiles = ?, failed_tiles = ?, gap_tiles = ?
                    WHERE id = ?
                ''', (cache_hits, failed_rows, gap_rows_total, task_id))
                conn.commit()
                base_downloaded, base_failed = cache_hits, failed_rows
            else:
                base_downloaded = task.downloaded_tiles
                base_failed = task.failed_tiles

            tlog.event('enumerated', total=task.total_tiles, cached=cache_hits,
                       pending=pending_count, gaps=len(gap_rows))

            # --- 边下边复制:产物目录 = 已下载内容的镜像 ----------------------
            # 两条写入路径,与下载并行,结尾复制阶段(下方)退化为同尺寸对账:
            #   ① 下载回调:瓦片落 cache 成功后立即复制一份到产物目录;
            #   ② 补拷线程:枚举出的 cache 命中瓦片(不经回调)开案就复制。
            # 两份清单天然不相交(命中 vs 待下载),写盘不冲突;中断时保留已
            # 复制部分 —— 与 cache 的状态一致,半途停下的任务产物即部分下载内容。
            stream_output_base = output_dir / f"task_{task_id}"
            stream_made_dirs: set = set()
            stream_dirs_lock = threading.Lock()

            def _stream_copy_quiet(tile: Tile) -> None:
                # 单块失败(如磁盘满)不拖垮下载/补拷:记 warning,结尾复制
                # 阶段的对账会按「同尺寸跳过」判定重试这块。
                try:
                    self._stream_copy_tile(
                        tile, source,
                        stream_output_base, stream_made_dirs, stream_dirs_lock,
                    )
                except Exception as e:
                    logger.warning(
                        f"Task {task_id}: stream copy failed for "
                        f"{tile.zoom}/{tile.x}/{tile.y}: {e}"
                    )

            def _backfill_cache_hits() -> None:
                # 按下标只走命中前缀,不 for-in 列表对象本身:下载回调会并发
                # 往 completed_tiles 尾部 append,而 list 的迭代器会一路跟进
                # 新元素 —— 补拷线程会把回调刚复制过的瓦片再复制一遍,并被
                # 拖到下载结束才停。旧实现靠 list(completed_tiles) 快照规避,
                # 代价是第二份全网格列表(见 cache_hit_count 处的注释)。
                copied = 0
                for index in range(cache_hit_count):
                    if self._is_stop_requested(task_id, stop_flag):
                        break
                    _stream_copy_quiet(completed_tiles[index])
                    copied += 1
                logger.info(
                    f"Task {task_id}: cache-hit backfill copied "
                    f"{copied}/{cache_hit_count} tiles"
                )

            backfill_thread = threading.Thread(
                target=_backfill_cache_hits,
                name=f"task-{task_id}-backfill",
                daemon=True,
            )
            backfill_thread.start()

            # --- 进度回调:稀疏缺块表 + 计数批量落库 ---
            # _status_count_deltas 需要的 old_status 完全由稀疏缺块表的内存镜像
            # live_gaps 给出(在表里 → 那一行记的 outcome;不在 → None)。旧实现
            # 另外维护一份 session_status: Dict[(z,x,y), str],为的是「同一块瓦片
            # 在一次运行里重复上报成功不重复计数」—— 那是一份**每块瓦片一项、
            # 从不裁剪**的全网格字典,而它防的情况在真实链路上不存在:
            # _download_single_tile 的每个出口都是「回调一次后立刻 return」,
            # 每块瓦片每次运行恰好上报一次(由
            # tests/test_fix_pause_resume_and_memory.py 钉住)。
            progress_counts = {'downloaded': base_downloaded, 'failed': base_failed}
            unflushed = {'downloaded': 0, 'failed': 0}
            processed_since_flush = 0
            # 上次 socketio 广播的 monotonic 时间戳;初始 -inf 让首块瓦片必发
            # (见 PROGRESS_EMIT_MIN_INTERVAL)。
            last_emit_at = float('-inf')
            # 下载吞吐计。只吃真正走网络的字节(缓存命中在上面的枚举段就已
            # 剔出待下清单,引擎那条兜底缓存分支也传 None),所以它反映的是
            # 网速而不是读盘速度。瞬时量,不落库。
            speed_meter = SpeedMeter()
            progress_conn = None  # 下载循环开启时建立,结束(finally)时关闭

            # 稀疏缺块表的内存镜像 + 攒批写队列。回调不再逐瓦片 INSERT/DELETE
            # + commit(每块瓦片一个事务,全部跑在下载事件循环线程上同步执行),
            # 改为只登记到三个列表,随 flush_progress_counts 一起 executemany +
            # 单次 commit。
            # live_gaps 的初始真值 = 枚举时读入的历史缺块行 - 对账已清掉的
            # 残留行(上面那段已经从 gap_rows 里 pop 掉了);之后由回调自行维护
            # —— 本任务只有这一个写方,字典裁决与旧实现逐瓦片 rowcount 判定
            # 等价(缺块:行已存在则 UPDATE 累加 retry_count 并刷新 status,
            # 否则 INSERT;成功:行存在才需要 DELETE)。
            #
            # 为什么是 {key: outcome} 而不是 set:计数增量要知道**旧的**结局
            # (见 _status_count_deltas)—— 一块瓦片从 retryable_failure 变成
            # no_data,gap_tiles 不变但 failed_tiles 要减一。只记「在不在表里」
            # 就算不出这个 -1,进度条会永远停在一个偏大的失败数上。
            live_gaps: Dict[Tuple[int, int, int], str] = dict(gap_rows)
            pending_tile_inserts: List[Tuple[int, int, int, int, str, Optional[str]]] = []
            pending_tile_updates: List[Tuple[str, Optional[str], int, int, int, int]] = []
            pending_tile_deletes: List[Tuple[int, int, int, int]] = []

            # M3: flush 的 sqlite 写被挪到工作线程执行,所以同一时刻只允许一个
            # flush 在跑 —— progress_conn 是 check_same_thread=False 建立的,
            # Python 层不再拦跨线程使用,串行化得由这里保证。事件循环是单线程,
            # 「检查 + 置位」天然原子,不需要额外的锁。
            flush_in_flight = False

            def _drain_progress_batch():
                """在事件循环线程上原子摘批:换走待写队列,返回快照(无事可做→None)。

                摘批必须留在事件循环上做。若让工作线程直接读那三个列表,
                executemany 执行期间 sqlite3 会释放 GIL,回调此刻 append 进来的
                新登记会被随后的 clear() 一起抹掉 —— 失败瓦片静默丢记录,完成
                判定的 failed_count>0 就守不住,任务被误判 completed。
                """
                if not (pending_tile_inserts or pending_tile_updates
                        or pending_tile_deletes
                        or unflushed['downloaded'] or unflushed['failed']):
                    return None
                batch = (
                    pending_tile_inserts[:],
                    pending_tile_updates[:],
                    pending_tile_deletes[:],
                    unflushed['downloaded'],
                    unflushed['failed'],
                )
                pending_tile_inserts.clear()
                pending_tile_updates.clear()
                pending_tile_deletes.clear()
                unflushed['downloaded'] = 0
                unflushed['failed'] = 0
                return batch

            def _restore_progress_batch(batch):
                """写盘失败时把批次退回队列头部,交给后续 flush 重试。

                同步版的语义是「executemany 成功后才 clear」——失败时数据留在
                队列里。摘批式实现必须显式退回才等价,否则一次写盘异常就静默
                吞掉整批失败行。前插保持相对顺序。
                """
                inserts, updates, deletes, downloaded, failed = batch
                pending_tile_inserts[:0] = inserts
                pending_tile_updates[:0] = updates
                pending_tile_deletes[:0] = deletes
                unflushed['downloaded'] += downloaded
                unflushed['failed'] += failed

            def flush_progress_counts():
                """同步落库 —— 只给下载循环收尾(finally)用,那时已无并发回调。

                异常路径上不该再引入新的挂起点,收尾也不存在阻塞事件循环的问题
                (下载已结束)。批次内的高频 flush 走 flush_progress_async。
                """
                nonlocal processed_since_flush
                processed_since_flush = 0
                batch = _drain_progress_batch()
                if batch is None:
                    return
                try:
                    self._write_progress_batch(progress_conn, task_id, batch)
                except Exception:
                    _restore_progress_batch(batch)
                    raise

            async def flush_progress_async():
                """批次 flush:摘批留在事件循环上,写盘交给工作线程(M3)。

                每 PROGRESS_DB_FLUSH_INTERVAL 块刷一次 —— 旧实现每块瓦片都对
                tasks 表做 UPDATE、对 task_tiles 做 INSERT/DELETE + commit,是
                高频小事务瓶颈;攒批之后瓶颈变成「攒批那一下的同步写盘卡住下载
                事件循环」,在 SMB/VPN 网络共享上尤其明显(M3 已把同一回调里的
                瓦片复制挪走,这里补上剩下的一半)。
                """
                nonlocal processed_since_flush, flush_in_flight
                if flush_in_flight:
                    # 已有批次在写盘。不清零 processed_since_flush,让下一块瓦片
                    # 立刻再试 —— 本批登记留在队列里,不会丢。
                    return
                processed_since_flush = 0
                batch = _drain_progress_batch()
                if batch is None:
                    return
                flush_in_flight = True
                try:
                    await asyncio.to_thread(
                        self._write_progress_batch, progress_conn, task_id, batch)
                except Exception:
                    _restore_progress_batch(batch)
                    raise
                finally:
                    flush_in_flight = False

            # Define progress callback
            async def progress_callback(
                tile: Tile,
                status: str,
                error: Optional[str],
                size_bytes: Optional[int] = None,
            ):
                """维护稀疏缺块表、累计计数增量,并按时间节流 emit socketio 事件

                status 是 `TileOutcome` 的值字符串,或 `'cancelled'`(引擎的
                取消出口 —— 那块瓦片没有结局,本轮当它不存在)。

                为什么不再每块瓦片写一行:瓦片集合是 (区域, zoom) 的纯函数
                (见 iter_region_tiles),完成态以磁盘 cache 文件为准;task_tiles
                只存缺块 —— 非 success 时 UPSERT 一行(带上真实 outcome),
                success 时 DELETE 掉历史缺块行(均攒批落库,见
                flush_progress_counts)。
                tasks 表的进度计数攒批落库;socketio 的 task_progress 按
                PROGRESS_EMIT_MIN_INTERVAL 节流(计数始终取内存实时值),
                末块瓦片必发 —— 逐瓦片广播 + 每次另开连接查运行时长会在
                百万级瓦片下堵死下载事件循环。
                """
                nonlocal processed_since_flush, last_emit_at
                if self._is_stop_requested(task_id, stop_flag):
                    logger.info(f"Task {task_id}: Stop flag detected in progress callback")
                    return

                # 吞吐计:**每次回调都记**,没有网络字节时记 0 —— 传 0 让时间窗
                # 照常前进,速率才会在下载变慢/停滞时如实回落;只在有字节时才记
                # 会让界面一直冻在最后那个高速度上。缓存命中与失败的 size_bytes
                # 是 None(见 download_engine 的回调契约),自然只推进时间。
                speed_meter.record(size_bytes or 0)

                key = (tile.zoom, tile.x, tile.y)
                if status == 'cancelled':
                    # 引擎在取消出口本来就不调回调,这一支是防御性的:'cancelled'
                    # 不是 TileOutcome,按 outcome_from_db 会被当成未知值落成
                    # retryable_failure,于是一次暂停就在稀疏表里凭空造出一批
                    # 需要用户决策的假缺块。
                    return
                outcome = outcome_from_db(status)

                # DB 层:稀疏缺块表攒批登记 + 计数累计/攒批落库。这一层的故障
                # 必须显眼地记 error —— 缺块若静默丢记录,完成判定就守不住,
                # 任务会被误判成 completed(而 completed 是终态,不能重启)。
                try:
                    old_status = live_gaps.get(key)
                    if outcome.is_gap:
                        # 缺块(含 no_data):登记写入/更新稀疏行。行是否已存在由
                        # 内存镜像 live_gaps 裁决(与旧实现 INSERT OR IGNORE 的
                        # rowcount 判定等价):已存在 → UPDATE 在旧行基础上
                        # retry_count +1 并**刷新 outcome**(上次超时、这次 404,
                        # 分类必须跟着变,否则缺块摘要停留在第一次的判断上),
                        # 否则 INSERT(retry_count=1)。
                        if old_status is not None:
                            pending_tile_updates.append(
                                (outcome.value, error, task_id, tile.zoom, tile.x, tile.y)
                            )
                        else:
                            pending_tile_inserts.append(
                                (task_id, tile.zoom, tile.x, tile.y, outcome.value, error)
                            )
                        live_gaps[key] = outcome.value
                    else:
                        # 成功:清掉历史缺块行;只有行真的存在才需要登记 DELETE,
                        # 对不存在的行 DELETE 是 no-op,跳过不改语义。
                        if old_status is not None:
                            pending_tile_deletes.append(
                                (task_id, tile.zoom, tile.x, tile.y)
                            )
                            live_gaps.pop(key, None)

                    # old_status = 这块瓦片在稀疏表里留着的历史 outcome,没有行
                    # 就是 None(本次运行前它没有任何已计数状态)。每块瓦片每次
                    # 运行只上报一次,不存在「上一发的状态」。
                    downloaded_delta, failed_delta = self._status_count_deltas(old_status, status)
                    progress_counts['downloaded'] += downloaded_delta
                    progress_counts['failed'] += failed_delta
                    unflushed['downloaded'] += downloaded_delta
                    unflushed['failed'] += failed_delta

                    # 本次下载成功的瓦片直接并入 completed 清单(替代旧的
                    # 「下载后从 results 再筛一遍 completed」):回调本来就逐块
                    # 上报,results 不必再为这件事全量保留。
                    if outcome is TileOutcome.SUCCESS:
                        completed_tiles.append(tile)
                        zooms_with_fresh_tiles.add(tile.zoom)
                        # 边下边复制①:cache 落盘即镜像一份到产物目录,下载结束
                        # ≈ 产物就绪,不再在 100% 后整段等待结尾复制阶段。
                        # M3: 必须 to_thread —— 这里跑在下载的 asyncio 事件循环
                        # 线程上,而 _stream_copy_tile 是 mkdir + exists + stat*2
                        # + copy2 + replace 六个阻塞 syscall。本地盘影响接近零,
                        # 但 0.2.4「保存目录全盘可选」鼓励的 SMB/VPN 网络共享上
                        # 每次各一个往返,累计 10-30ms/块 -> 吞吐被钉在 30-100
                        # 块/秒,concurrent_downloads 调多少都没用,同时 stop_flag
                        # 检查被推迟、暂停/取消响应变慢。下载引擎那侧连一次
                        # cache_path.stat() 都特意挪出了事件循环。
                        await asyncio.to_thread(_stream_copy_quiet, tile)
                    elif outcome is TileOutcome.CACHE_FAILURE:
                        # 写盘失败必须进任务日志:它是**本机**故障(盘满、只读、
                        # Windows 上被占用),而用户看到的是「下载失败」。不点名
                        # 的话排查方向会全跑到代理和网络上去。
                        tlog.warning('缓存写入失败 %d/%d/%d:%s',
                                     tile.zoom, tile.x, tile.y, error)

                    processed_since_flush += 1
                    if processed_since_flush >= PROGRESS_DB_FLUSH_INTERVAL:
                        await flush_progress_async()
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
                    #
                    # stop 已被请求(pause_task / delete_task 置了 stop flag)
                    # 时**一律不再广播进度**。下载循环不是立刻停的 —— 它要跑到
                    # 当前批次边界,期间本回调仍会被调用,而下面载荷里的
                    # `task.status` 取自内存对象:pause_task 只改库、不碰它,
                    # 所以它仍然是 'running'。发出去的后果是把前端刚显示的
                    # 「已暂停」覆盖回「运行中」,而 _complete_task 见库里已是
                    # 终态会直接 return、再也不发 task_completed —— 界面永久停在
                    # 错误状态,且没有任何自愈路径。
                    #
                    # 状态迁移本来就不该走进度流:pause 自己广播库里的真值,
                    # delete 连行都不留。计数在收尾时落库,前端下次拉列表即准
                    # —— 已停的任务少几发实时进度没有任何影响。
                    done_tiles = progress_counts['downloaded'] + progress_counts['failed']
                    now = time.monotonic()
                    if self.socketio and not self._is_stop_requested(task_id, stop_flag) and (
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
                            'total_running_seconds': total_running_seconds,
                            # 瞬时网络吞吐(字节/秒)。不落库 —— tasks 表没有这
                            # 一列,它只活在这发推送里;页面刷新后等下一发
                            # (<=PROGRESS_EMIT_MIN_INTERVAL)就有了。
                            'download_speed_bps': round(speed_meter.bps()),
                        })
                except Exception as e:
                    logger.error(
                        f"Progress callback emit error for tile "
                        f"{tile.zoom}/{tile.x}/{tile.y}: {e}"
                    )

            # 运行中磁盘复查的句柄。在下载块**之外**先绑上:下面要在下载收尾之后
            # 读它的 blocked 判决,而下载块可能整个被跳过(pending_count == 0,
            # 全部命中缓存)。
            disk_recheck = None

            # Download tiles
            # 补拷线程随下载并行跑;finally 里 join,保证「下载块的所有出口
            # (完成/stop return/异常)都在拼接与结尾对账前收尾」—— 此后产物
            # 目录只剩零头缺口,结尾复制阶段基本退化为 stat 对账。
            try:
                if pending_count > 0:
                    # Check stop flag before downloading
                    if self._is_stop_requested(task_id, stop_flag):
                        logger.info(f"Task {task_id}: Stop flag detected before download")
                        return

                    logger.info(f"Task {task_id}: Starting tile download")
                    tlog.event('stage_start', stage='download', pending=pending_count)
                    # check_same_thread=False:批次 flush 的写盘被 asyncio.to_thread
                    # 挪到工作线程执行(M3)。同一时刻只有一个线程用它 —— 由
                    # flush_progress_async 的 flush_in_flight 标志串行化。
                    progress_conn = get_connection(check_same_thread=False)
                    try:
                        # 返回值(每块瓦片一条结果)刻意不接收:completed 清单由
                        # progress_callback 逐块并入(见回调里的注释),全量 results
                        # 列表在这里没有任何消费方,接住它只是白白占内存。
                        # 置 _collect_batch_results=False 让引擎连物化都不做
                        # (为什么是实例属性而不是 kwarg:tests/ 里多处把
                        # download_tiles_batch 换成四参替身,见引擎 __init__ 注释)。
                        self.download_engine._collect_batch_results = False
                        # source / max_concurrency 是关键字参数,且只在真的有值
                        # 时才传:tests/ 里多处把 download_tiles_batch 换成
                        # (tiles, style, progress_callback, stop_flag=None) 四参
                        # 替身,无条件多传两个 kwarg 会让它们全部 TypeError。
                        extra_kw = {'source': source}
                        reservation = self._reservations.get(task_id)
                        if reservation is not None and reservation.network > 0:
                            extra_kw['max_concurrency'] = reservation.network
                        # ---- 运行中磁盘复查(§4.2,纯观测)-------------------
                        # 启动时的估算(_estimate_disk_verdict)只是**按下开始那一
                        # 刻**的一张快照。地图是四条管线里最容易把盘写满的那条
                        # (百万级瓦片 + 每层 GeoTIFF),也是跑得最久的 —— 几小时
                        # 里另一个任务、另一个进程、用户自己拷东西都能把盘吃掉。
                        # 复查不叫停任务(拦截语义已移除);它的全部价值是:真写到
                        # ENOSPC 时,任务日志里最后一条 disk_recheck 事件就是
                        # 「还差多少」的现场数字,而不是一句没头没尾的 I/O error。
                        #
                        # 整任务的估算只算一次,循环里按已下张数折成剩余量:
                        # count_region_tiles 对大多边形不便宜,而复查每十几秒一
                        # 次;折算口径见 remaining_map_estimate(为什么不能直接
                        # 用 cached_tiles 那条路,那里写了)。
                        # 估算算不出来就不复查 —— 与 _estimate_disk_verdict 同一
                        # 立场:一个算不出来的数字连记录的价值都没有。
                        # 构造见下:估算失败时留 None(不复查)。
                        try:
                            full_estimate = disk_budget.estimate_map_task(
                                region, task.zoom_min, task.zoom_max,
                                task.output_format, style_code,
                                export_mbtiles=bool(task_row['export_mbtiles']))
                        except Exception as estimate_error:
                            logger.warning(
                                f"Task {task_id}: 磁盘估算失败({estimate_error!r}),"
                                f"本轮不做运行中复查")
                            full_estimate = None
                        if full_estimate is not None:
                            disk_recheck = disk_budget.RunningRecheck(
                                output_dir,
                                lambda: disk_budget.remaining_map_estimate(
                                    full_estimate, progress_counts['downloaded']),
                                owner=('map', task_id, 'download'),
                                config_manager=self.config_manager,
                                # 通过与否都记一行:估算错的时候第一件事就是回头
                                # 看这行的数字。
                                on_verdict=lambda v: tlog.event(
                                    'disk_recheck', ok=v.ok, free=v.free_bytes,
                                    required=v.required_bytes,
                                    shortfall=v.shortfall_bytes, reason=v.reason))
                            extra_kw['disk_recheck'] = disk_recheck
                        await self.download_engine.download_tiles_batch(
                            tiles=_iter_pending_tiles(),
                            style=style_code,
                            progress_callback=progress_callback,
                            stop_flag=stop_flag,
                            **extra_kw
                        )
                    finally:
                        # 下载循环结束时把最后不满一批的计数增量落库 —— 暂停/取消/
                        # 异常都不能丢这部分进度。这里不能抛:收尾异常会掩盖下载
                        # 循环抛出的原始异常,且连接无论如何都要关闭。
                        #
                        # 但**也不能只记一条 log 就算了**：完成判定读的是库
                        # （task_tiles 的 failed 行 + tasks 的计数），而这一批
                        # 失败瓦片的行就在这次 flush 里。丢了它，「有瓦片失败」
                        # 这件事对完成判定就不存在了 —— 小任务（< 一个批次）
                        # 整轮进度只有这一次 flush，一次 database is locked 就能
                        # 把「N 块瓦片失败」写成「completed，无 error_message」，
                        # 而 completed 是终态、start_task 拒绝重启，用户没有自愈
                        # 路径。所以记下来，交给下面的完成判定判失败。
                        try:
                            flush_progress_counts()
                        except Exception as flush_error:
                            progress_flush_error = flush_error
                            logger.error(
                                f"Task {task_id}: Failed to flush progress counts: {flush_error}"
                            )
                        progress_conn.close()

                    # 本次下载成功的瓦片已在回调里并入 completed 清单 —— 与枚举段的
                    # cache 命中清单互补,替代旧的「下载后第二遍全量 stat 枚举」。
                    logger.info(f"Task {task_id}: Tile download completed")
                    tlog.event('stage_end', stage='download',
                               downloaded=progress_counts['downloaded'],
                               failed=progress_counts['failed'],
                               gaps=len(live_gaps))
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
            # 因为本层还有**没交代的**缺块而没有拼的 zoom。与 stitch_failures
            # 分开记:那是「拼了但炸了」,这是「不该拼」。
            unstitchable_zooms: List[int] = []
            # 拼进产物、但产物上确实有洞的 zoom。它决定 Artifact.has_gaps ——
            # §13-3 要求「成果与历史永久带缺块标记」,而标记跟着产物走。
            zooms_with_gaps: set = set()

            # Stitch tiles if output format includes image
            # NOTE: 'png'/'jpg' are legacy synonyms of 'image_only' — the output
            # path below is hardcoded to .tif, so they never produce PNG/JPG.
            if task.output_format in ['png', 'jpg', 'both', 'image_only']:
                logger.info(f"Task {task_id}: Starting tile stitching")
                tlog.event('stage_start', stage='stitch')

                # Stitch tiles for each zoom level
                zoom_levels = sorted(set(tile.zoom for tile in completed_tiles))
                logger.info(f"Task {task_id}: Stitching {len(zoom_levels)} zoom levels")

                for zoom in zoom_levels:
                    # Check stop flag before each zoom level
                    if self._is_stop_requested(task_id, stop_flag):
                        logger.info(f"Task {task_id}: Stop flag detected during stitching")
                        return

                    # 本层还有**没交代的**缺块就整层不拼。拼接段拿到的是
                    # completed_tiles,缺块根本不在里面 —— 于是 BuildVRT 会用一个
                    # 比任务网格小的瓦片集合拼出一张「完整」的图,而引擎侧的
                    # _assert_vrt_covers_tile_grid 期望值正是由**它收到的那批瓦片**
                    # 推出来的(见该函数),边缘瓦片缺失时期望跟着一起缩小,结构上
                    # 不可能发现「任务网格少了瓦片」。产物于是是一张地理范围比
                    # 用户选区小(或内部有洞)的 GeoTIFF:文件打得开、看着正常、
                    # 任务 completed、error_message 为 NULL,唯一的发现途径是自己
                    # 拿 GIS 去量四至。
                    #
                    # 闸门收窄成「没交代的缺块」是这次改造的要点:
                    #   · no_data —— 上游明确说过这里没有数据(海面、境外未覆盖)。
                    #     等它被「补齐」是等一个永远不会到来的东西,而那一层的
                    #     产物对用户完全可用。照拼,产物永久标记 has_gaps。
                    #   · retryable / permanent / cache_failure —— 洞的原因在我们
                    #     这边,产物先不出(§13-3「默认严格」),等用户补漏或显式
                    #     接受(_gap_accepted 关掉本闸门,那是「导出显式」的一半)。
                    gap_values = tuple(o.value for o in GAP_OUTCOMES)
                    placeholders = ', '.join('?' for _ in gap_values)
                    cursor.execute(
                        f"SELECT status, COUNT(*) AS c FROM task_tiles "
                        f"WHERE task_id = ? AND zoom = ? AND status IN ({placeholders}) "
                        f"GROUP BY status",
                        (task_id, zoom, *gap_values),
                    )
                    zoom_gaps = {row['status']: row['c'] for row in cursor.fetchall()}
                    unexplained = sum(
                        c for value, c in zoom_gaps.items()
                        if _is_unexplained(outcome_from_db(value)))
                    if zoom_gaps:
                        zooms_with_gaps.add(zoom)
                    if unexplained > 0 and not gaps_accepted:
                        logger.warning(
                            f"Task {task_id}: zoom {zoom} 仍有 {unexplained} 块未解释的缺块,"
                            f"跳过拼接(拼出来会是一张范围偏小的图)"
                        )
                        tlog.event('stitch_skipped', zoom=zoom, unexplained=unexplained)
                        unstitchable_zooms.append(zoom)
                        continue
                    if zoom_gaps:
                        tlog.warning('zoom %d 带 %d 块缺块拼接(%s):产物将永久标记 has_gaps',
                                     zoom, sum(zoom_gaps.values()),
                                     '已接受' if gaps_accepted else '全部为 no_data')

                    # task.name 是用户输入,直接拼进文件名可携 '..' / 路径分隔符
                    # 逃逸出任务目录 —— 先消毒再拼。
                    safe_name = sanitize_filename(task.name)
                    output_path = output_dir / f"task_{task_id}" / f"{safe_name}_zoom_{zoom}.tif"

                    # 拼接断点:任务重试/恢复时,已产出且非空的 zoom mosaic 直接
                    # 保留不重算(大 zoom 单层拼接是十分钟级活)。
                    #
                    # 但「文件在」不等于「文件对」:本次运行**新下载**到瓦片的
                    # 那些层必须重拼。上一轮如果是在缺瓦片的情况下拼的(旧版本
                    # 没有上面那道 failed 闸门),恢复补齐后短路会把那张残缺
                    # mosaic 原样当成品留下,任务照报 completed —— 这正是这道
                    # 判据要挡的。反过来,纯粹的重跑(全部命中 cache、一块新瓦片
                    # 都没下)仍然短路,十分钟级的重算照样省掉。
                    #
                    # 已知取舍:进程在 Translate 写盘中途被杀不会留下半成品
                    # (产物走 .part + os.replace,见 stitch_tiles_with_gdal)。
                    if (
                        zoom not in zooms_with_fresh_tiles
                        and output_path.exists()
                        and output_path.stat().st_size > 0
                    ):
                        logger.info(
                            f"Task {task_id}: Zoom level {zoom} output already exists "
                            f"({output_path}) and no tile at this zoom was downloaded "
                            f"this run, skipping stitch"
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
                        #
                        # stop_flag 必须传进去:单层 zoom 的拼接本身就是十分钟级,
                        # 而这个循环只在**每层之间**查停止标志。不传的话
                        # pause_task 提交完 'paused' 之后线程还要跑满一整层,
                        # 期间 start_task 只能拒绝恢复 —— 就是界面显示「已暂停」
                        # 却恢复不了的那十几分钟(见 TaskStillStoppingError)。
                        # work_dir_base:与产物**同卷**的工作目录。中间件缓冲进
                        # 系统 TEMP、拼完再整体搬到输出盘,等于把每个字节在两块盘
                        # 之间搬一遍(GeoD #32),而单层 mosaic 的中间件是 GB 级。
                        # 同卷时 os.replace 是元数据操作,零拷贝。
                        # 算不出来(权限、盘掉线)就传 None,回退引擎自己的三级
                        # 优先(stitch_tmpdir → 系统临时目录)—— 为一个优化项
                        # 让拼接起不来是本末倒置。
                        #
                        # ⚠️ 传的是 `.parent`,不是 work_dir_for 的返回值本身。
                        # 这个参数的语义是「工作目录的**父目录**」:引擎会在它
                        # 里面 mkdtemp 出 `map_dl_stitch_*`。原先直接把
                        # `<tmp>/map_stitch_<pid>_<hex>` 整个传进去,真正的工作
                        # 目录就落到了 `<tmp>/map_stitch_*/map_dl_stitch_*`
                        # —— 而 task_cleanup._sweep_tmp_dirs 是**只扫直下一层**
                        # 的(它的 docstring 原话:「不递归匹配」),于是拼接中途
                        # 崩溃留下的 GB 级中间件永远回收不了,外层那个
                        # `map_stitch_*` 目录连正常跑完都没人删。work_dir_for
                        # 在这里的作用只是**挑盘**,目录名归引擎按已注册前缀生成
                        # —— 与 contour_engine 的 `work_dir_for(...).parent`
                        # 完全同形(见那里那段「可清扫前缀注册表」的警告)。
                        try:
                            work_dir_base = str(disk_budget.work_dir_for(
                                output_dir, 'map_stitch_').parent)
                        except Exception as e:
                            logger.warning(
                                f"Task {task_id}: 同卷工作目录算不出来({e!r}),回退配置")
                            work_dir_base = None
                        await asyncio.to_thread(
                            self.download_engine.stitch_tiles_with_gdal,
                            tiles=completed_tiles,
                            style=source,
                            output_path=str(output_path),
                            zoom_level=zoom,
                            # 保存路径全盘化后,拼接白名单要认该任务的注册产物根
                            extra_allowed_dir=str(output_dir),
                            stop_flag=stop_flag,
                            work_dir_base=work_dir_base,
                        )
                        logger.info(f"Task {task_id}: Zoom level {zoom} stitched successfully")
                        tlog.event('stitched', zoom=zoom, path=str(output_path),
                                   has_gaps=zoom in zooms_with_gaps)
                        stitched_zooms.append(zoom)

                        # Emit stitching progress
                        if self.socketio:
                            self.socketio.emit('task_stitch_progress', {
                                'task_id': task_id,
                                'zoom_level': zoom,
                                'output_path': str(output_path)
                            })

                    except StitchCancelled:
                        # 用户主动停的,不是故障:不记 stitch_failures(那会把任务
                        # 标 failed 或挂警告),按与其它停止检查相同的方式收尾。
                        logger.info(
                            f"Task {task_id}: Stop flag detected inside stitching "
                            f"(zoom {zoom})"
                        )
                        return

                    except Exception as e:
                        logger.error(f"Task {task_id}: Failed to stitch zoom level {zoom}: {e}")
                        tlog.error('zoom %d 拼接失败:%s', zoom, e)
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
                tlog.event('stage_start', stage='copy', tiles=len(completed_tiles))

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
                    # 'both' 是默认输出格式,大多数任务都会走这个循环,10 万块
                    # 瓦片要跑几分钟。循环里不查停止标志,暂停/删除就得等整轮
                    # 拷贝跑完才生效。
                    if self._is_stop_requested(task_id, stop_flag):
                        logger.info(
                            f"Task {task_id}: Stop flag detected during tile copy "
                            f"({copied_count}/{total_to_copy} copied)"
                        )
                        return

                    # Source: cache path(按快照的指纹命名空间取,与下载、拼接
                    # 三段同一个 source 对象 —— 各算各的就可能算出两个目录)
                    cache_path = tile.cache_path(source)

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
                tlog.event('stage_end', stage='copy', copied=copied_count,
                           total=total_to_copy, dir=str(output_base))

            if self._is_stop_requested(task_id, stop_flag):
                logger.info(f"Task {task_id}: Stop flag detected, not marking as completed")
                tlog.event('run_end', reason='stopped', status='(unchanged)')
                return

            cursor.execute('SELECT status FROM tasks WHERE id = ?', (task_id,))
            current_row = cursor.fetchone()
            # 只剩 'paused' 要挡:用户明确按了暂停,收尾不得把它改写成终态。
            # 行不在了(删除)同样直接退出。
            if not current_row or current_row['status'] == 'paused':
                logger.info(f"Task {task_id}: Current status prevents completion")
                tlog.event('run_end', reason='status_prevents_completion',
                           status=current_row['status'] if current_row else '(row gone)')
                return

            # 完成判定的输入:稀疏缺块表按 outcome 分组。
            #   洞为 0                      → completed(不变)
            #   洞全是 no_data              → completed_with_gaps,产物照出
            #   有 retryable/permanent/cache→ pending_decision,产物不出
            # 最后一档**不是 failed**:任务本身跑完了,数据也拿到了绝大部分,
            # 「失败」是终态、不能续、把「差 12 块超时」和「GDAL 炸了」判成
            # 同一个词。pending_decision 让用户在「补漏」与「接受缺块」之间选,
            # 这正是 §13-3「默认严格,导出显式」要的形状。真正的 failed 留给
            # 引擎/拼接异常与进度落库失败。
            gap_values = tuple(o.value for o in GAP_OUTCOMES)
            placeholders = ', '.join('?' for _ in gap_values)
            cursor.execute(
                f'''SELECT status, COUNT(*) AS c FROM task_tiles
                    WHERE task_id = ? AND status IN ({placeholders})
                    GROUP BY status''',
                (task_id, *gap_values))
            by_outcome = {row['status']: row['c'] for row in cursor.fetchall()}
            gap_count = sum(by_outcome.values())
            unexplained_count = sum(
                c for value, c in by_outcome.items()
                if _is_unexplained(outcome_from_db(value)))
            # 分类计数进日志:「12 块缺块」解释不了任何事,「no_data×11 +
            # retryable_failure×1」直接告诉用户该去补漏还是该接受。§4.5 的
            # 「任何终态都能从日志解释原因」在这一行兑现。
            tlog.event('gap_tally', total=gap_count, unexplained=unexplained_count,
                       **{value: count for value, count in sorted(by_outcome.items())})

            # 进度落库失败过 → 上面这些计数不可信(那一批缺块行就在丢掉的
            # flush 里)。不能拿一个读不全的库判 completed:那是终态,
            # start_task 拒绝重启,用户没有自愈路径。判 failed 并说清原因,
            # 用户可以删了重来。
            if progress_flush_error is not None:
                error_message = (
                    f"进度落库失败,本轮计数与缺块记录不可信"
                    f"({progress_flush_error});请重新创建该任务"
                )
                cursor.execute('''
                    UPDATE tasks
                    SET status = 'failed', error_message = ?, completed_at = ?
                    WHERE id = ? AND status = ?
                ''', (error_message, utc_now_iso(), task_id, run_status))
                conn.commit()
                logger.error(f"Task {task_id}: {error_message}")
                tlog.event('terminal', status='failed', reason='progress_flush_failed',
                           detail=str(progress_flush_error))
                if cursor.rowcount and self.socketio:
                    self.socketio.emit('task_failed', {
                        'task_id': task_id,
                        'status': 'failed',
                        'error_message': error_message
                    })
                return

            if unexplained_count > 0 and not gaps_accepted:
                # 没交代的缺块 → 等用户决策。产物**不出**(拼接段已经跳过了
                # 这些层),状态不是终态,所以「补漏」与「接受缺块」两条路都还在。
                detail = '、'.join(
                    f"{value}×{c}" for value, c in sorted(by_outcome.items()))
                error_message = f"{gap_count} 块瓦片缺失({detail}),等待处理"
                if unstitchable_zooms:
                    levels = ', '.join(str(z) for z in unstitchable_zooms)
                    error_message += f";缩放级别 {levels} 因此未拼接"
                cursor.execute('''
                    UPDATE tasks
                    SET status = ?, error_message = ?, gap_tiles = ?, completed_at = ?
                    WHERE id = ? AND status = ?
                ''', (TaskState.PENDING_DECISION.value, error_message, gap_count,
                      utc_now_iso(), task_id, run_status))
                conn.commit()
                logger.warning(f"Task {task_id}: {error_message}")
                tlog.event('terminal', status=TaskState.PENDING_DECISION.value,
                           gaps=gap_count, unexplained=unexplained_count,
                           unstitched=len(unstitchable_zooms))
                tlog.warning('任务停在待决策:%s', error_message)
                # 已经拼出来的那些层是**真实存在的产物**,必须登记。逐层的拼接
                # 闸门只跳过「本层有没交代的缺块」的 zoom,所以没洞的那些层此刻
                # 已经躺在盘上了;而 `_register_artifacts` 原先只挂在完成分支上
                # —— 任务停在待决策(用户完全可能永远不来决策)时,那些 .tif 就是
                # 一批不在索引里、也不带任何标记的孤儿文件:磁盘统计看不见它们,
                # 缓存治理不认识它们,导出也找不到它们。
                # 不出这些文件**不是**选项 —— 它们是合法的部分成果,被违反的是
                # 账本而不是写盘。has_gaps 固定为 True:任务级别确实有洞,产物
                # 覆盖的范围小于用户要的范围,而这个标记跟着文件走、活得比任务
                # 行久(§13-3)。后续「接受缺块」跑完会以同样的路径再登记一次,
                # record_artifact 是 INSERT OR REPLACE,覆盖的是同一行。
                self._register_artifacts(
                    task_id, output_dir, task, stitched_zooms,
                    has_gaps=True, tlog=tlog)
                if cursor.rowcount:
                    self._update_total_running_time(task_id)
                    self._emit_gap_decision(task_id, TaskState.PENDING_DECISION.value,
                                            gap_count, by_outcome)
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
                    WHERE id = ? AND status = ?
                ''', (error_message, utc_now_iso(), task_id, run_status))
                conn.commit()
                logger.error(f"Task {task_id}: {error_message}")
                tlog.event('terminal', status='failed', reason='stitch_all_failed',
                           zooms=len(stitch_failures))
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

            # 洞全是 no_data(或用户已显式接受)→ completed_with_gaps。
            # 它**是**成功态(TaskState.is_successful 为真):产物出了、可用、
            # 且永久带缺块标记。等 no_data 被「补齐」是等一个永远不会到来的
            # 东西 —— 上游说过那里没有数据,再问一万次答案不变。
            final_status = (TaskState.COMPLETED_WITH_GAPS.value if gap_count > 0
                            else TaskState.COMPLETED.value)
            if gap_count > 0:
                gap_note = f"{gap_count} 块瓦片无数据,成品带缺块"
                if gaps_accepted:
                    gap_note = f"{gap_count} 块瓦片缺失(用户已接受),成品带缺块"
                stitch_warning = (f"{gap_note};{stitch_warning}" if stitch_warning
                                  else gap_note)

            cursor.execute('''
                UPDATE tasks
                SET status = ?, error_message = ?, gap_tiles = ?, completed_at = ?
                WHERE id = ? AND status = ?
            ''', (final_status, stitch_warning, gap_count, utc_now_iso(),
                  task_id, run_status))

            conn.commit()

            if cursor.rowcount:
                self._update_total_running_time(task_id)
                self._record_time_action(task_id, 'complete')

                if stitch_warning:
                    logger.warning(f"Task {task_id}: Completed with warning — {stitch_warning}")
                else:
                    logger.info(f"Task {task_id}: Completed successfully")
                tlog.event('terminal', status=final_status, gaps=gap_count,
                           stitched=len(stitched_zooms),
                           warning=stitch_warning or '')

                # 产物登记:XYZ 镜像目录 + 每层 GeoTIFF。has_gaps 跟着**产物**走
                # 而不是跟着任务状态走 —— 任务行可以被删,产物可以被保留,而
                # 「这张图上有洞」这件事必须活得比任务行久(§13-3)。
                self._register_artifacts(
                    task_id, output_dir, task, stitched_zooms,
                    has_gaps=gap_count > 0, tlog=tlog)

                # 建任务时勾了「同时导出 MBTiles」就在这里打包。**失败只记日志**:
                # 一个已经下完、拼完、复制完的任务不该因为容器打包出错就被改写
                # 成 failed —— 松散产物完好无损,用户随时可以再点一次导出。
                if self._wants_mbtiles(task_id):
                    try:
                        info = self.export_mbtiles(task_id, has_gaps=gap_count > 0)
                        tlog.event('mbtiles', path=info.get('path'),
                                   tiles=info.get('tile_count'))
                    except Exception as export_error:
                        logger.warning(
                            f"Task {task_id}: MBTiles 导出失败(不影响任务状态): "
                            f"{export_error}")
                        tlog.warning('MBTiles 导出失败(不影响任务状态):%s', export_error)

                if gap_count > 0:
                    self._emit_gap_decision(task_id, final_status, gap_count, by_outcome)

                if self.socketio:
                    # M1: 收尾 emit 必须自带 try —— 它在终态已落库【之后】才执行,
                    # 一旦抛异常就会落到下面的兜底 except,把这条已终结的记录改写
                    # 成 failed。同文件的 copy 进度 emit 早就这么写了
                    # (「emit 故障(客户端断开等)不应打断复制本身」)。
                    try:
                        self.socketio.emit('task_completed', {
                            'task_id': task_id,
                            'status': final_status,
                            'warning': stitch_warning
                        })
                    except Exception as emit_error:
                        logger.warning(
                            f"Task {task_id}: emit task_completed failed "
                            f"(ignored): {emit_error}")

        except Exception as e:
            logger.error(f"Task {task_id} execution failed: {e}")

            # Update task status to failed
            try:
                tlog.event('terminal', status='failed', reason='exception',
                           detail=f'{type(e).__name__}: {e}')
                # 允许被改写的状态是一份**正向清单**(FAILABLE_STATE_VALUES =
                # 活动态减 paused):三个终态绝不可被兜底改写,paused 是用户的
                # 明确意图。与 contour_task_manager.py 的同款收尾对齐。
                placeholders = ', '.join('?' for _ in FAILABLE_STATE_VALUES)
                cursor.execute(f'''
                    UPDATE tasks
                    SET status = 'failed', error_message = ?, completed_at = ?
                    WHERE id = ? AND status IN ({placeholders})
                ''', (str(e), utc_now_iso(), task_id, *FAILABLE_STATE_VALUES))

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
