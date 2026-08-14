"""插件任务管理器——全部插件管线共用的一份（§13-4：不许自带任务表/并发）。

生命周期照抄现有管线的语义：删除即取消（stop_flags，没有 cancel）、孤儿
running 启动时判 failed、缺块走 §13-3 的 pending_decision / accept 流程。

## 资源为什么只预留一次，而且在工作线程里预留

一次 `reserve()` 拿全 TASK_SLOT（有估算时再加 DISK_BYTES），插件从
`ctx.granted(kind)` 读配额、自己去开信号量/进程池。单张凭据、单线程持有
（申请与 `release()` 都在同一个 `_run_task` 里），没有跨线程的凭据字典要
对账——`ResourceScheduler` 对「同一个 owner 重复 reserve」是直接抛，
凭据漏一张这个任务就永远起不来了。

预留放在工作线程而不是 `start_task`（核心管线是在 `_state_lock` 里预留的）
的理由是准入要先拿到估算，而 `pipeline.estimate()` 是**插件代码**：让它跑在
HTTP 请求线程上，一个第三方插件里的慢 I/O 就能挂住 Flask worker。代价是
「配额拿不到」这件事只能表现成一次 running → failed 的翻转，而不是启动请求
直接被拒（见 `_run_task` 里那段注释）。

## 磁盘判决只记日志，不拦

与四条核心管线逐字同口径（`disk_budget` 模块 docstring：2026-08 起拦截语义
整体移除）。估算是保守假设，把它变成启动闸门等于让一个算不准的数字否掉用户
明确的选择。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from src.contracts.outcome import (ACTIVE_STATE_VALUES, TaskState, TileOutcome)
from src.contracts.region import RegionSpec
from src.contracts.reservation import ResourceKind, ResourceRequest
from src.core.config import Config
from src.core.database import get_connection, utc_now_iso
from src.plugins import registry
from src.plugins.params import validate_params
from src.plugins.protocols import PluginOutcome, TaskEvent
from src.plugins.task_context import TaskContext

logger = logging.getLogger(__name__)

_PIPELINE = 'plugin'
_TABLE = 'plugin_tasks'

#: 与 `task_manager.PROGRESS_EMIT_MIN_INTERVAL` 同值：进度广播 2Hz 上限，
#: 完成那一发（done >= total）不受节流。
_PROGRESS_EMIT_MIN_INTERVAL = 0.5

#: 可启动的状态。**刻意含 `failed` 与 `pending_decision`**，与
#: `outcome.RESUMABLE_TASK_STATES` 不同——那份白名单管的是核心管线的
#: 「续跑」，插件任务没有断点续跑，一次 start 就是完整重跑一遍：
#: - `failed`：重跑是唯一的修复动作（同 local terrain 的 retile）。
#: - `pending_decision`：`accept_gaps()` 写完 `_gap_accepted` 就靠这条路
#:   把收尾那一趟跑起来。误点「继续」的后果也只是插件读不到
#:   `_gap_accepted`、再返回一次 PENDING_DECISION，决策不会被洗掉。
_STARTABLE_STATES = (TaskState.PENDING.value, TaskState.FAILED.value,
                     TaskState.PENDING_DECISION.value,
                     TaskState.COMPLETED_WITH_GAPS.value)

#: 宿主自己解释的参数键。它们不进插件 schema 的未知键闸门（schema 自己
#: 声明了同名键时以 schema 为准，见 `_validate_plugin_params`）。
_HOST_PARAM_KEYS = frozenset({'name', 'bbox', 'output_path', 'zoom_min',
                              'zoom_max', 'source_id', '_gap_accepted'})

#: 「没被上游解释过」的缺块值——failed_items 的口径，与
#: `task_manager._is_unexplained` 同一份（no_data 不算失败）。
_UNEXPLAINED_VALUES = tuple(sorted(o.value for o in TileOutcome
                                   if not o.is_explained))


class PluginTaskManager:
    """插件任务的唯一入口。线程模型与四条核心管线一致：

    - `active_tasks: {task_id: Thread}` —— **必须是 dict**，
      `task_deletion.delete_task_row` 靠 `.get(task_id)` 拿线程判「在跑」。
    - `stop_flags: {task_id: Event}` —— 置位的唯一入口是 `delete_task`。
    - 三者的读写都在 `_state_lock` 里，且「翻 running + 登记线程」与删除路径
      的「判在跑 + DELETE」是同一把锁（理由见 `task_deletion` 头部注释）。
    """

    def __init__(self, socketio=None, config_manager=None):
        self.socketio = socketio
        self.config_manager = config_manager
        self.active_tasks: Dict[int, threading.Thread] = {}
        self.stop_flags: Dict[int, threading.Event] = {}
        self._state_lock = threading.Lock()
        self._recover_orphan_running_tasks()

    # ------------------------------------------------------------ 查询

    def get_task(self, task_id: int) -> Optional[dict]:
        conn = get_connection()
        try:
            row = conn.execute(f'SELECT * FROM {_TABLE} WHERE id = ?',
                               (int(task_id),)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_tasks(self, active_only: bool = False) -> List[dict]:
        sql = f'SELECT * FROM {_TABLE}'
        args: tuple = ()
        if active_only:
            # 正向清单，且用四条管线共用的那一份常量（见 outcome.py 的注释）。
            sql += (' WHERE status IN ('
                    + ', '.join('?' for _ in ACTIVE_STATE_VALUES) + ')')
            args = ACTIVE_STATE_VALUES
        sql += ' ORDER BY id DESC'
        conn = get_connection()
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    # ------------------------------------------------------------ 创建

    def create_task(self, plugin_id: str, params: dict) -> int:
        """建一行 pending。KeyError = 插件不可用；ValueError = 参数非法。"""
        pipeline = registry.get_pipeline(plugin_id)
        if pipeline is None:
            raise KeyError(f'插件管线不可用：{plugin_id!r}')
        if not isinstance(params, dict):
            raise ValueError('params 必须是对象')
        bbox = params.get('bbox')  # [north, south, east, west]
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            raise ValueError('params.bbox 必须是 [north, south, east, west]')
        try:
            north, south, east, west = (float(v) for v in bbox)
        except (TypeError, ValueError) as e:
            raise ValueError(f'params.bbox 不是四个数字：{e}') from e
        region = RegionSpec.from_bbox(north, south, east, west)

        stored = dict(params)
        stored.update(self._validate_plugin_params(pipeline, plugin_id, params))
        output_path = str(params.get('output_path')
                          or Path(Config.DOWNLOADS_DIR) / 'plugins' / plugin_id)
        conn = get_connection()
        try:
            cur = conn.execute(
                f'INSERT INTO {_TABLE} (plugin_id, name, status,'
                ' north, south, east, west, zoom_min, zoom_max,'
                ' region_json, params_json, output_path)'
                ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (plugin_id, str(params.get('name') or f'{plugin_id} 任务'),
                 TaskState.PENDING.value, north, south, east, west,
                 params.get('zoom_min'), params.get('zoom_max'),
                 region.to_json(), json.dumps(stored, ensure_ascii=False),
                 output_path))
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _validate_plugin_params(self, pipeline, plugin_id: str,
                                params: dict) -> dict:
        """插件自己声明的那部分参数过 T3 的校验器，返回清洗值。

        只把**插件的**键喂进去：`validate_params` 对未知键报错（与
        `PUT /api/config` 同一个理由），而 name/bbox/output_path 这些是宿主
        解释的，schema 里当然没有。schema 自己声明了同名键时那个键归插件——
        插件想自己收 `output_path` 就该由它校验。

        `params_schema()` 是插件代码，抛异常等于这个插件不可用（而不是
        「参数非法」），所以按 KeyError 报出去，与 `get_pipeline` 返回 None
        同一档。
        """
        try:
            schema = pipeline.params_schema()
            known = set(schema.keys())
        except Exception as e:
            raise KeyError(f'插件 {plugin_id!r} 的参数 schema 不可用：{e!r}') from e
        subset = {k: v for k, v in params.items()
                  if k in known or k not in _HOST_PARAM_KEYS}
        clean, errors = validate_params(schema, subset)
        if errors:
            raise ValueError('参数非法：' + '; '.join(
                f'{k}={v}' for k, v in sorted(errors.items())))
        return clean

    # ------------------------------------------------------------ 启动

    def start_task(self, task_id: int) -> None:
        """幂等（已在跑就直接返回）。pending/failed/pending_decision/
        completed_with_gaps 可起。

        状态检查、翻 running、登记线程全在 `_state_lock` 的**同一个**临界区里
        （核心管线 `TaskManager.start_task` 同形）：删除路径在同一把锁里「判
        在跑 + DELETE」，两边分属两个临界区就会开出「删除判它没在跑 → 同步
        rmtree → 线程随后启动把目录重建出来」的窗口。
        """
        task_id = int(task_id)
        row = self.get_task(task_id)
        if row is None:
            raise KeyError(f'插件任务不存在：{task_id}')
        if row['status'] not in _STARTABLE_STATES:
            raise ValueError(f'状态 {row["status"]} 不可启动')
        if registry.get_pipeline(row['plugin_id']) is None:
            raise ValueError(f'插件 {row["plugin_id"]!r} 未启用或加载失败')

        thread = threading.Thread(target=self._run_task_entry, args=(task_id,),
                                  daemon=True, name=f'plugin-task-{task_id}')
        with self._state_lock:
            active = self.active_tasks.get(task_id)
            if active is not None and (active.is_alive()
                                       or active.ident is None):
                return
            conn = get_connection()
            try:
                # 条件 UPDATE：拿锁前读到的状态可能已经被另一条请求改了。
                cur = conn.execute(
                    f'UPDATE {_TABLE} SET status = ?, started_at = ?,'
                    " error_message = '' WHERE id = ? AND status IN ("
                    + ', '.join('?' for _ in _STARTABLE_STATES) + ')',
                    (TaskState.RUNNING.value, utc_now_iso(), task_id,
                     *_STARTABLE_STATES))
                conn.commit()
                claimed = cur.rowcount > 0
            finally:
                conn.close()
            if not claimed:
                raise ValueError(f'插件任务 {task_id} 已不在可启动状态')
            self.active_tasks[task_id] = thread
            self.stop_flags[task_id] = threading.Event()
        try:
            thread.start()
        except Exception:
            # 线程建不起来（can't start new thread）：登记与 running 都要撤，
            # 否则这一行永远 running 而没有线程，而删除路径会把它判成在跑。
            with self._state_lock:
                if self.active_tasks.get(task_id) is thread:
                    self.active_tasks.pop(task_id, None)
                    self.stop_flags.pop(task_id, None)
            self._finish(task_id, TaskState.FAILED.value, '无法启动工作线程')
            raise

    # ------------------------------------------------------------ 运行

    def _run_task_entry(self, task_id: int) -> None:
        """线程外壳：任何异常 → failed + error_message；收尾必清 active/stop。

        任务日志的句柄归这里（开在最外层、关在最外层）：`TaskContext.close()`
        会顺手关掉传进去的 tlog，如果让它在 `run()` 的 finally 里关，终态那几
        行日志就都写进一个已经关掉的句柄。所以 ctx 那边只 `flush_outcomes()`。
        """
        from src.services.task_logging import open_task_log
        tlog = open_task_log(_PIPELINE, task_id, self.config_manager)
        started = time.monotonic()
        try:
            self._run_task(task_id, tlog, started)
        except Exception as e:
            logger.exception('插件任务 %s 运行期异常', task_id)
            tlog.exception('运行期未捕获异常：%r', e)
            self._finish(task_id, TaskState.FAILED.value,
                         f'{type(e).__name__}: {e}',
                         elapsed=time.monotonic() - started)
            self._emit('plugin_task_failed', task_id, self.get_task(task_id), {})
        finally:
            tlog.close()
            with self._state_lock:
                if self.active_tasks.get(task_id) is threading.current_thread():
                    self.active_tasks.pop(task_id, None)
                self.stop_flags.pop(task_id, None)

    def _run_task(self, task_id: int, tlog, started: float) -> None:
        row = self.get_task(task_id)
        if row is None:
            # 起线程与线程真正跑起来之间被删掉了。行都没了，没有终态可写。
            tlog.warning('任务行已不存在（启动瞬间被删除），本轮不执行')
            return
        plugin_id = row['plugin_id']
        pipeline = registry.get_pipeline(plugin_id)
        if pipeline is None:
            raise RuntimeError(f'插件 {plugin_id!r} 在启动后被停用')
        params = json.loads(row['params_json'] or '{}')
        region = RegionSpec.from_json(row['region_json'])
        with self._state_lock:
            stop_flag = self.stop_flags.get(task_id)
        if stop_flag is None:                     # 已被删除请求置停
            tlog.warning('停止标志已被摘除（删除请求先到），本轮不执行')
            return

        # 准入：估算 → 磁盘判决（只记日志）→ 资源预留。
        estimate = self._estimate(pipeline, params, region, tlog)
        requests = [ResourceRequest(kind=ResourceKind.TASK_SLOT,
                                    requested=1, minimum=1)]
        if estimate is not None:
            requests.append(self._disk_request(task_id, row, estimate, tlog))

        from src.services.resource_scheduler import get_scheduler
        owner = (_PIPELINE, task_id, 'run')
        reservation = get_scheduler(self.config_manager).reserve(owner, requests)
        if reservation is None:
            # 没有排队机制（`reserve` 立刻返回，见 contracts/reservation.py 的
            # 「配额，不是信号量」），所以拿不到名额只能判 failed 让用户重试：
            # 挂在 pending 需要一个把它唤醒的调度循环，这套架构里没有。
            tlog.event('admission_denied', reason='no_task_slot')
            self._finish(task_id, TaskState.FAILED.value,
                         '资源配额不足（任务槽/磁盘预留），请稍后重试',
                         elapsed=time.monotonic() - started)
            self._emit('plugin_task_failed', task_id, self.get_task(task_id), {})
            return

        ctx = TaskContext(
            task_id=task_id, plugin_id=plugin_id, region=region,
            params=params, output_dir=self._task_output_dir(row),
            snapshot=self._snapshot_for(row, params),
            stop_flag=stop_flag, tlog=tlog,
            emit_progress=self._make_progress_callback(task_id),
            # 防御性拷贝：TaskContext 不拷 granted，而 reservation.granted
            # 就是调度器账本里的那个 dict —— 插件改一个数字就是一次永久配额
            # 泄漏。拷一份 5 个键的 dict 比那个后果便宜得多。
            granted=dict(reservation.granted),
            config_manager=self.config_manager)
        tlog.event('start', plugin=plugin_id, region=region.summary())
        try:
            outcome = pipeline.run(ctx)
        finally:
            # 只 flush 不 close：tlog 的生命周期归 `_run_task_entry`（见那里）。
            ctx.flush_outcomes()
            reservation.release()

        if self.get_task(task_id) is None:
            # 运行期被删掉了：删除即取消，插件是被 stop_flag 叫停的。终态无处可写。
            tlog.warning('任务行在运行期被删除，不写终态')
            return

        status = self._status_for(outcome)
        error = ('' if status != TaskState.FAILED.value
                 else f'插件 {plugin_id!r} 的 run() 返回了 {outcome!r}，'
                      '不是 PluginOutcome')
        self._finish(task_id, status, error, elapsed=time.monotonic() - started)
        final_row = self.get_task(task_id)
        tlog.event('terminal', status=status)
        self._emit('plugin_task_failed' if status == TaskState.FAILED.value
                   else 'plugin_task_completed', task_id, final_row, {})
        registry.dispatch_event(TaskEvent(
            kind='task_completed', pipeline=_PIPELINE, task_id=task_id,
            plugin_id=plugin_id))

    @staticmethod
    def _status_for(outcome) -> str:
        """`run()` 的返回值 → 落库状态。

        非 `PluginOutcome` 一律 failed，**不能**默认当成 pending_decision：
        插件忘了 return 时 `outcome` 是 None，判成 pending_decision 会让一个
        压根没决策可做的任务永远挂在「等你决定」上（那还是个 active 状态，
        清缓存和退出确认都会被它拦住）。
        """
        if outcome is PluginOutcome.COMPLETED:
            return TaskState.COMPLETED.value
        if outcome is PluginOutcome.COMPLETED_WITH_GAPS:
            return TaskState.COMPLETED_WITH_GAPS.value
        if outcome is PluginOutcome.PENDING_DECISION:
            return TaskState.PENDING_DECISION.value
        return TaskState.FAILED.value

    @staticmethod
    def _estimate(pipeline, params, region, tlog):
        """`estimate()` 是插件代码，抛了只当「没有估算」——磁盘判决本来就不拦，
        没有理由让一个估不出来的数字把任务打死。"""
        try:
            return pipeline.estimate(params, region)
        except Exception as e:
            logger.warning('插件估算失败（按无估算处理）：%r', e)
            tlog.warning('插件 estimate() 抛异常，按无估算处理：%r', e)
            return None

    def _disk_request(self, task_id: int, row, estimate,
                      tlog) -> ResourceRequest:
        """磁盘判决 + DISK_BYTES 预留请求。

        判决**不拦**（`disk_budget` 模块 docstring：2026-08 起拦截语义整体
        移除，四条核心管线都是不通过也只记 warning 照常启动）。预留照做：
        不预留的话这次判决对下一个任务不可见，并发任务看到的可用空间偏乐观。
        """
        from src.services.disk_budget import check_budget
        verdict = check_budget(self._task_output_dir(row), estimate,
                              self.config_manager)
        (logger.info if verdict.ok else logger.warning)(
            '插件任务 %s 磁盘预检：%s', task_id, verdict.reason)
        tlog.event('disk_check', ok=bool(verdict.ok),
                   required=verdict.required_bytes, free=verdict.free_bytes)
        return ResourceRequest(kind=ResourceKind.DISK_BYTES,
                               requested=verdict.required_bytes,
                               minimum=verdict.required_bytes)

    def _make_progress_callback(self, task_id: int):
        """`ctx.progress()` 的落地：计数落库 + 节流广播。

        完成那一发（done >= total）必发，其余按 2Hz 上限节流——与核心管线
        `PROGRESS_EMIT_MIN_INTERVAL` 同一口径。
        """
        state = {'last': float('-inf')}

        def emit_progress(done, total, phase=''):
            now = time.monotonic()
            if (done < total
                    and now - state['last'] < _PROGRESS_EMIT_MIN_INTERVAL):
                return
            state['last'] = now
            self._update_counts(task_id, done, total)
            self._emit('plugin_task_progress', task_id, self.get_task(task_id),
                       {'phase': phase})

        return emit_progress

    def _task_output_dir(self, row) -> Path:
        # resolve_stored_output_dir 是本仓对 output_path 的唯一口径（存量相对
        # 路径也归一成绝对路径）；产物删除那条路要求绝对路径，相对值会被拒收。
        from src.services.task_cleanup import resolve_stored_output_dir
        return (resolve_stored_output_dir(row['output_path'])
                / f'plugin_task_{row["id"]}')

    def task_output_dir(self, task_id: int) -> Optional[Path]:
        """产物目录，行不存在时 None。**给路由层用**：删除端点要用它调
        `task_cleanup.purge_registered_artifacts`（delete_files=True）与
        `record_retained_output`（delete_files=False），四条核心管线的路由
        各自重算了一遍这个布局，而 local_terrain_api.py:177-178 那条注释
        （「与 manager.delete_task / terrain_static 同一布局」）就是重算三份
        的代价。插件这条只有管理器知道布局，所以由它给出。
        """
        row = self.get_task(task_id)
        return None if row is None else self._task_output_dir(row)

    def _snapshot_for(self, row, params):
        source_id = params.get('source_id')
        if not source_id:
            return None
        try:
            return registry.build_source_snapshot(row['plugin_id'], source_id)
        except KeyError:
            return None

    def _update_counts(self, task_id, done, total) -> None:
        try:
            conn = get_connection()
            try:
                conn.execute(
                    f'UPDATE {_TABLE} SET downloaded_items = ?,'
                    ' total_items = ? WHERE id = ?',
                    (int(done), int(total), int(task_id)))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning('插件任务 %s 进度落库失败：%r', task_id, e)

    def _finish(self, task_id: int, status: str, error: str,
                elapsed: Optional[float] = None) -> None:
        """写终态，**并在同一个临界区里摘掉本线程的登记**。

        两件事必须原子，否则有一个真实的窗口：终态已经落库、而线程还在跑
        （它还要走完 emit / 钩子分发 / finally）。那段时间里
        `accept_gaps()` → `start_task()` 会看到 `active_tasks` 里那个
        `is_alive()` 为真的线程，按「已经在跑」静默返回 —— 用户点的「接受
        缺块」于是只写了 gap_decision，收尾那一趟压根没起来。合成一个临界区
        之后，任何观察者要么看到「running 行 + 有登记」，要么看到「终态行 +
        无登记」。

        `failed_items` 在这里现算——缺块表是稀疏的，「没被上游解释过的洞」
        只有终态这一刻需要一个数字（历史卡片上的「失败 N」）。
        """
        with self._state_lock:
            conn = get_connection()
            try:
                conn.execute(
                    f'UPDATE {_TABLE} SET status = ?, completed_at = ?,'
                    ' error_message = ?,'
                    ' total_running_seconds = total_running_seconds + ?,'
                    f' failed_items = (SELECT COUNT(*) FROM plugin_task_tiles'
                    '  WHERE task_id = ? AND status IN ('
                    + ', '.join('?' for _ in _UNEXPLAINED_VALUES) + '))'
                    ' WHERE id = ?',
                    (status, utc_now_iso(), error,
                     int(max(0.0, elapsed or 0.0)),
                     int(task_id), *_UNEXPLAINED_VALUES, int(task_id)))
                conn.commit()
            finally:
                conn.close()
            if self.active_tasks.get(task_id) is threading.current_thread():
                self.active_tasks.pop(task_id, None)
                self.stop_flags.pop(task_id, None)

    def _emit(self, event: str, task_id: int, row: Optional[dict],
              extra: dict) -> None:
        if self.socketio is None:
            return
        row = row or {}
        payload = {'task_id': int(task_id), 'id': int(task_id),
                   'plugin_id': row.get('plugin_id', ''),
                   'task_type': _PIPELINE,
                   'name': row.get('name', ''),
                   'status': row.get('status', ''),
                   'downloaded_items': row.get('downloaded_items') or 0,
                   'total_items': row.get('total_items') or 0,
                   'failed_items': row.get('failed_items') or 0,
                   'gap_tiles': row.get('gap_tiles') or 0,
                   'total_running_seconds': row.get('total_running_seconds') or 0,
                   'phase': '',
                   'output_path': row.get('output_path', ''),
                   'started_at': row.get('started_at'),
                   'created_at': row.get('created_at'),
                   **extra}
        try:
            self.socketio.emit(event, payload)
        except Exception as e:
            logger.warning('插件事件广播失败（%s）：%r', event, e)

    # ------------------------------------------------------------ 缺块决策

    def gap_summary(self, task_id: int) -> dict:
        conn = get_connection()
        try:
            rows = conn.execute(
                'SELECT status, COUNT(*) AS n FROM plugin_task_tiles'
                ' WHERE task_id = ? GROUP BY status',
                (int(task_id),)).fetchall()
        finally:
            conn.close()
        return {'task_id': int(task_id),
                'by_outcome': {r['status']: r['n'] for r in rows}}

    def accept_gaps(self, task_id: int) -> None:
        """§13-3 的「接受缺块」：`params['_gap_accepted'] = True` 回写
        params_json，然后重跑——收尾产出由插件自己在 `run()` 里读这个键决定。

        为什么是回写参数而不是宿主直接出产物：宿主不知道这个插件的成品长什么
        样（§13-4 契约：产出格式归插件）。回写 + 重跑是唯一能让插件自己决定
        「带洞也导出」的形状，也让这个决定跟着任务行持久化——进程重启后重跑
        仍然是「已接受缺块」的那一趟。
        """
        task_id = int(task_id)
        row = self.get_task(task_id)
        if row is None:
            raise KeyError(f'插件任务不存在：{task_id}')
        if row['status'] != TaskState.PENDING_DECISION.value:
            raise ValueError('只有 pending_decision 状态能接受缺块')
        params = json.loads(row['params_json'] or '{}')
        params['_gap_accepted'] = True
        conn = get_connection()
        try:
            conn.execute(
                f"UPDATE {_TABLE} SET gap_decision = 'accept',"
                ' params_json = ? WHERE id = ?',
                (json.dumps(params, ensure_ascii=False), task_id))
            conn.commit()
        finally:
            conn.close()
        self.start_task(task_id)

    # ------------------------------------------------------------ 删除与恢复

    def delete_task(self, task_id: int, delete_files: bool = False):
        """删除即取消：先置停止标志，再走四条管线共用的删除实现。"""
        from src.services.task_deletion import delete_task_row
        task_id = int(task_id)
        row = self.get_task(task_id)
        if row is None:
            raise KeyError(f'插件任务不存在：{task_id}')
        with self._state_lock:
            flag = self.stop_flags.get(task_id)
            if flag is not None:
                flag.set()
        artifact_dir = self._task_output_dir(row) if delete_files else None
        return delete_task_row(manager=self, task_id=task_id, table=_TABLE,
                               artifact_dir=artifact_dir)

    def _recover_orphan_running_tasks(self) -> None:
        """启动时：running → failed，再清缺块表里的孤儿行。

        语义照抄 `local_terrain_task_manager._recover_orphan_running_tasks`：
        插件任务没有断点续跑（一次 `run()` 跑完），进程死掉留下的 running 行
        只能重跑，所以判 failed 而不是尝试恢复。解释同时落进任务自己的日志
        （§4.5）——`failed` 是硬终态，用户点开详情看到的不能只有「失败」两个字
        加一片在崩溃那一瞬间戛然而止的日志。
        """
        ids: List[int] = []
        conn = get_connection()
        try:
            ids = [r['id'] for r in conn.execute(
                f'SELECT id FROM {_TABLE} WHERE status = ?',
                (TaskState.RUNNING.value,)).fetchall()]
            if ids:
                now = utc_now_iso()
                conn.executemany(
                    f'UPDATE {_TABLE} SET status = ?, completed_at = ?,'
                    " error_message = '进程在任务运行期间退出（崩溃 / 断电 /"
                    " 关窗口）：插件任务没有断点续跑，重新启动即可重跑一遍'"
                    ' WHERE id = ? AND status = ?',
                    [(TaskState.FAILED.value, now, i, TaskState.RUNNING.value)
                     for i in ids])
                conn.commit()
                logger.warning('插件孤儿任务已判 failed：%s', ids)
        except Exception as e:
            logger.error('插件孤儿任务恢复失败：%r', e)
            conn.rollback()
        finally:
            conn.close()
        for task_id in ids:
            self._log_recovery(task_id)

        # 第二层：任务行已经不存在的 plugin_task_tiles 残留。T1 刻意不给这张
        # 表加外键（删除路径是「先停线程再删行」，工作线程还可能在 flush），
        # 兜底就在这里——不清的话这些行会被将来复用同一个 id 的任务算成自己的洞。
        try:
            conn = get_connection()
            try:
                cur = conn.execute(
                    'DELETE FROM plugin_task_tiles WHERE task_id NOT IN'
                    f' (SELECT id FROM {_TABLE})')
                conn.commit()
                if cur.rowcount:
                    logger.info('清理插件缺块孤儿行：%d 行', cur.rowcount)
            finally:
                conn.close()
        except Exception as e:
            logger.warning('插件缺块孤儿行清理失败：%r', e)

    def _log_recovery(self, task_id: int) -> None:
        """把一次孤儿恢复写进**这个任务自己的**日志。绝不抛：调用点在
        `__init__` 里，一个次要 sink 的环境问题没有资格让服务起不来。"""
        try:
            from src.services.task_logging import open_task_log
            tlog = open_task_log(_PIPELINE, task_id, self.config_manager)
            try:
                tlog.event('terminal', status=TaskState.FAILED.value,
                           reason='process_restart')
                tlog.warning(
                    '进程在插件任务运行期间退出：重启时发现库里还写着 running '
                    '而没有任何线程，已判为 failed。插件管线是一次性的 run() '
                    '调用，没有断点续跑——重新启动这个任务即可重跑一遍。')
            finally:
                tlog.close()
        except Exception as e:
            logger.warning('插件任务 %s 孤儿恢复日志写入失败（忽略）：%r',
                           task_id, e)


_MANAGER: Optional[PluginTaskManager] = None
_MANAGER_LOCK = threading.Lock()


def init_plugin_task_manager(socketio=None) -> PluginTaskManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = PluginTaskManager(socketio=socketio)
        elif socketio is not None and _MANAGER.socketio is None:
            _MANAGER.socketio = socketio
        return _MANAGER


def get_plugin_task_manager() -> PluginTaskManager:
    if _MANAGER is None:
        raise RuntimeError('插件任务管理器未初始化（init_plugin_task_manager）')
    return _MANAGER
