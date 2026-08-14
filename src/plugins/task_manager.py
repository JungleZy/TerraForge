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

from src.contracts.outcome import (ACTIVE_STATE_VALUES, GAP_OUTCOMES,
                                   SUCCESSFUL_STATE_VALUES, TaskState,
                                   TileOutcome, outcome_from_db)
from src.contracts.region import RegionSpec
from src.contracts.reservation import ResourceKind, ResourceRequest
from src.core.config import Config
from src.core.database import get_connection, utc_now_iso
from src.plugins import registry
from src.plugins.params import validate_params
from src.plugins.protocols import PluginOutcome, TaskEvent
from src.plugins.task_context import TaskContext
from src.services.config_manager import ConfigManager
from src.services.system_proxy import mask_text_secrets

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

#: 会在稀疏表里留行的结局（= 除 SUCCESS 之外全部）。`gap_summary` 的
#: `by_outcome` 按它**恒定给出四个键**：前端拿不到键和拿到 0 是两回事。
#: 排序固定，好让响应的键序稳定（便于人读与 diff）。
_GAP_VALUES = tuple(sorted(o.value for o in GAP_OUTCOMES))

#: `gap_summary().samples` 的上限，与瓦片管线的 20 条同值。一个大区域任务可以
#: 有几万个洞，全序列化进一个 JSON 只会把浏览器卡死。
_GAP_SAMPLE_LIMIT = 20


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

        # ⚠️ `params_json` 原样落库整份参数。`ParamSpec.type` 允许
        # `'credential'`，而 `credentials.py` 的口径是「凭据不进哈希、不进日志、
        # **不进任务行**」——今天没有插件声明这类参数，所以还不是已发生的缺陷，
        # 但也**没有**机制拦住第一个声明它的插件。这里刻意不自作聪明地把这类键
        # 剥掉：剥了插件在 run() 里就拿不到值（参数来自任务行，重跑/重启都得从
        # 那儿读），等于换一个静默坏掉的方向。真正的解法是让凭据走 T5 的
        # `plugins.config_json` + `credentials.resolve_reference`（键名进任务行、
        # 值留在配置里），第一个需要它的插件落地时一并设计。
        # 在那之前：T8 序列化 get_task/list_tasks 的返回值给前端时不许原样吐
        # `params_json`。
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

        任务日志的句柄归这里（开在最外层、关在最外层）：`tlog` 的生命周期是
        **整个任务**，终态那几行（`terminal` 事件）在 `run()` 返回之后才写。
        `TaskContext.close()`（T6 f5f78b3 之后）明确不碰 tlog，所以 ctx 那边
        照常 close——它只负责「插件这一次运行」的缓冲。
        """
        from src.services.task_logging import open_task_log
        tlog = open_task_log(_PIPELINE, task_id, self.config_manager)
        started = time.monotonic()
        try:
            self._run_task(task_id, tlog, started)
        except Exception as e:
            # **不用 `logger.exception`**：`logging_setup` 没给 app 日志挂任何
            # 脱敏 filter（挂了的是 tlog 那条），完整 traceback 里的局部变量
            # 与消息文本会把插件请求过的 URL（含 token）逐字写进
            # `logs/terraforge.log`。栈要留，但留在**有脱敏的**任务日志里；
            # app 日志只留一句掩过的摘要，够指路到那份任务日志。
            logger.error('插件任务 %s 运行期异常：%s', task_id,
                         mask_text_secrets(repr(e)))
            tlog.exception('运行期未捕获异常：%r', e)
            self._finish(task_id, TaskState.FAILED.value,
                         f'{type(e).__name__}: {e}',
                         elapsed=time.monotonic() - started)
            self._emit('plugin_task_failed', task_id, self.get_task(task_id), {})
        finally:
            tlog.close()
            with self._state_lock:
                # 两个摘除都必须带身份判据。`_finish` 已经把本线程的登记摘干净
                # 了，所以走到这里还能命中的**只有**「新一轮已经登记进来」那种
                # 情形——无判据地 pop 就是把下一轮的 Event 偷走：新线程读到 None
                # 会判成「删除请求先到」直接 return，行永久停在 running 谁也起
                # 不动；读在偷之前则这一轮再也停不下来，delete_task 置不了标志、
                # delete_task_row 判它在跑、后台 join 600 秒超时后产物清理整支
                # 跳过。窗口不是理论值：`_finish` 到这里之间隔着 `_emit` 与钩子
                # 分发（第三方代码，可以任意慢），而 `accept_gaps()` 正是收到
                # `plugin_task_completed` 之后用户点下来的那一步。
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
        network = self._network_request(plugin_id)
        if network is not None:
            requests.append(network)

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

        # 凭据一旦拿到手，**从这里到 release() 之间不许有一行裸代码**：
        # `_task_output_dir` / `_snapshot_for` / `TaskContext.__init__`
        # （它会 mkdir 用户填的 output_path，路径上有一段是文件就 NotADirectoryError、
        # 只读目录就 PermissionError）/ `region.summary()` 都能抛。抛在 try 外面的
        # 后果不是「这次失败」而是「这条任务永久锁死」：TASK_SLOT 不回收，而
        # owner ('plugin', id, 'run') 是确定性的，它留在 _owners 里之后每一次
        # start 都撞 `owner ... already holds a reservation`，进程重启前救不回来
        # （模块 docstring 那句「凭据漏一张这个任务就永远起不来了」说的就是这里）。
        ctx = None
        try:
            ctx = TaskContext(
                task_id=task_id, plugin_id=plugin_id, region=region,
                params=params, output_dir=self._task_output_dir(row),
                snapshot=self._snapshot_for(row, params),
                stop_flag=stop_flag, tlog=tlog,
                emit_progress=self._make_progress_callback(task_id),
                # 防御性拷贝：reservation.granted 就是调度器账本里的那个 dict，
                # `release()` 遍历它回退 _in_use —— 插件改一个数字就是一次永久
                # 配额泄漏。T6 那边也拷了一份（纵深防御），这一层离账本最近。
                granted=dict(reservation.granted),
                config_manager=self.config_manager)
            tlog.event('start', plugin=plugin_id, region=region.summary())
            outcome = pipeline.run(ctx)
        finally:
            # 归还必须**独立于** close()：`ctx.close()` 今天吞掉自己的一切异常
            # （task_context._flush_locked 兜底 except），但它排在 release()
            # 前面，一旦哪天它会抛，漏的就不是一批记账而是这条任务永久锁死
            # （owner 确定性地留在 _owners 里）。嵌一层 finally 让「谁先谁后」
            # 与「归还是否发生」彻底解耦。
            try:
                if ctx is not None:
                    # close 而不是只 flush：T6 f5f78b3 之后 close() 不碰 tlog，只
                    # flush + 置 _closed。置位是有意义的——插件残留的后台线程在这之后
                    # 的记账会被丢弃并记 warning，而不是静默插进一个已经算完
                    # failed_items（甚至已经被删）的任务。
                    ctx.close()
            finally:
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
        # 钩子只在**产出可用**的终态发。v1 只有 `task_completed` 这一个 kind
        # （protocols.py），钩子收到它就等于「任务成功了，去写 sidecar / 触发
        # 后续导出」——发给 failed 是在骗它，发给 pending_decision 是提前
        # （产物还没出，用户还没决定）。两条 failed 路径（返回值不合法、run()
        # 抛异常）因此行为一致：都不发。
        if status in SUCCESSFUL_STATE_VALUES:
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

    def _network_request(self, plugin_id: str):
        """声明了 `network` 权限的插件 → 一条 NETWORK 预留请求；否则 None。

        **manifest 的 `permissions` 在这里第一次承担真实职责。** 在这之前它只被
        清单层校验拼写，宿主没有任何地方读它，于是 `_run_task` 只请求
        TASK_SLOT（+DISK_BYTES），`ctx.granted(NETWORK)` 在生产里恒为 0，
        `mvt_pipeline._concurrency` 恒等于它的兜底常量——契约第 2 条
        「不许自带并发」实质失守：插件的连接不进 `max_network_connections`
        这本全局账本，`ResourceScheduler` 在第五条管线上形同虚设。

        口径与 `plan_download_reservation` 逐字一致（同一份 `concurrent_downloads`
        配置、同样的 `minimum=1`）：一条连接也能把任务跑完，只是慢——宁可慢，
        不可不起。不能用 `plan_download_reservation` 本身：它连 TASK_SLOT 一起
        造，而这里 TASK_SLOT 已经在请求列表里了，重复请求会被调度器求和成 2。
        """
        rec = registry.get_record(plugin_id)
        perms = (rec.manifest.permissions or ()) if rec is not None else ()
        if 'network' not in perms:
            return None
        conns = int((self.config_manager or ConfigManager())
                    .get('concurrent_downloads', 8))
        return ResourceRequest(kind=ResourceKind.NETWORK,
                               requested=max(1, conns), minimum=1)

    def _make_progress_callback(self, task_id: int):
        """`ctx.progress()` 的落地：计数落库 + 节流广播。

        完成那一发必发，其余按 2Hz 上限节流——与核心管线
        `PROGRESS_EMIT_MIN_INTERVAL` 同一口径。

        「完成」的判据必须带 `total > 0`：调用方是第三方代码，`progress(0, 0)`
        （总量还没算出来时的自然写法）在 `done >= total` 下恒为真，节流会整个
        失效，每一次调用换来一次写库加一次广播。
        """
        state = {'last': float('-inf')}

        def emit_progress(done, total, phase=''):
            now = time.monotonic()
            final = total > 0 and done >= total
            if (not final
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

        `error` **落库前过脱敏**。这是唯一的写入端：`_run_task_entry` 用
        `f'{type(e).__name__}: {e}'` 造它，而插件抛的异常里常常整条 URL 都在
        （`mvt_pipeline` 就把 `tilejson_url` 逐字嵌进 RuntimeError，而那是
        用户填的地址——Mapbox 的 TileJSON 长这样 `…?access_token=pk…`）。
        在这里掩而不是在 `_emit` 掩：这一列会被 `plugins_api._TASK_PUBLIC_COLUMNS`
        与统一任务列表的 UNION 原样吐给浏览器，也会进诊断包与备份——存进去
        那一刻就已经泄漏了。
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
                    (status, utc_now_iso(), mask_text_secrets(error or ''),
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
                   # 少了这个键，前端 `handleTaskFailed(..., data.error_message)`
                   # 拿到 undefined，常驻失败 toast 与任务行都写「未知错误」，
                   # 而真原因就躺在 `plugin_tasks.error_message` 里。四条核心
                   # 管线的 `task_failed` 都带这一个字段，插件这条不带就是唯一
                   # 的例外。这里**不再重复脱敏**：`_finish` 是这一列唯一的
                   # 写入端，落库前就掩过了（一件事一处实现）。
                   'error_message': row.get('error_message') or '',
                   'started_at': row.get('started_at'),
                   'created_at': row.get('created_at'),
                   **extra}
        try:
            self.socketio.emit(event, payload)
        except Exception as e:
            logger.warning('插件事件广播失败（%s）：%r', event, e)

    # ------------------------------------------------------------ 缺块决策

    def gap_summary(self, task_id: int) -> dict:
        """缺块摘要。与瓦片管线的 `TaskManager.gap_summary` **逐键同形**
        （`src/services/task_manager.py:1420-1482`）：
        `task_id / total / by_outcome / explained / decision / status / samples`。

        为什么必须同形（§13-3）：整个决策流建立在「已解释的缺块 vs 没交代的
        缺块」这个区分上——`no_data` 是上游权威地说「那里没有数据」（海面、
        境外未覆盖），其余三种是我们自己失败了。`explained` 为真的任务**不该
        问用户**「补漏还是接受」，核心管线正是据此自动判 completed_with_gaps
        而不是 pending_decision。不给这个字段就等于让前端面板自己再推一遍判据，
        那就是同一条规则的第二套实现。

        三条口径细节：
        - `by_outcome` **四个键恒存**（没有的补 0）。前端拿不到键和拿到 0 是两
          回事，恒存才免得每个消费者写 `|| 0`。
        - `explained` 复用 `TileOutcome.is_explained`，**不**手写
          `status == 'no_data'`：那个判据的唯一定义在 `contracts/outcome.py`，
          抄一遍就是等着两处漂移。
        - `samples` 最多 20 条。一个大区域任务可以有几万个洞，全序列化进响应只
          会把浏览器卡死，而用户要的是「大概在哪一片、什么原因」。

        Raises:
            KeyError: 任务行不存在（与 `start_task` / `accept_gaps` 同一档）。
        """
        task_id = int(task_id)
        conn = get_connection()
        try:
            row = conn.execute(
                f'SELECT status, gap_decision FROM {_TABLE} WHERE id = ?',
                (task_id,)).fetchone()
            if row is None:
                raise KeyError(f'插件任务不存在：{task_id}')
            placeholders = ', '.join('?' for _ in _GAP_VALUES)
            counts = {r['status']: r['n'] for r in conn.execute(
                'SELECT status, COUNT(*) AS n FROM plugin_task_tiles'
                f' WHERE task_id = ? AND status IN ({placeholders})'
                ' GROUP BY status', (task_id, *_GAP_VALUES)).fetchall()}
            samples = [{'zoom': r['zoom'], 'x': r['x'], 'y': r['y'],
                        'outcome': r['status'],
                        'error': r['error_message'] or ''}
                       for r in conn.execute(
                           'SELECT zoom, x, y, status, error_message'
                           ' FROM plugin_task_tiles'
                           f' WHERE task_id = ? AND status IN ({placeholders})'
                           ' ORDER BY zoom, y, x LIMIT ?',
                           (task_id, *_GAP_VALUES, _GAP_SAMPLE_LIMIT)).fetchall()]
        finally:
            conn.close()
        by_outcome = {value: int(counts.get(value, 0)) for value in _GAP_VALUES}
        return {
            'task_id': task_id,
            'total': sum(by_outcome.values()),
            'by_outcome': by_outcome,
            'explained': all(outcome_from_db(value).is_explained
                             for value, n in by_outcome.items() if n),
            'decision': row['gap_decision'] or '',
            'status': row['status'],
            'samples': samples,
        }

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
        # 先落 gap_decision 再 start：不是原子的，`start_task` 若因并发改状态
        # 抛 ValueError，库里会留下一条「已接受缺块但没跑」的行。**有意不回滚**
        # ——回滚会把用户刚做的决定丢掉，而留着的这一行是幂等的：状态仍是
        # pending_decision，用户再按一次就接着跑，`_gap_accepted` 已经在里面了。
        self.start_task(task_id)

    # ------------------------------------------------------------ 删除与恢复

    def delete_task(self, task_id: int, delete_files: bool = False):
        """删除即取消：先置停止标志，再走四条管线共用的删除实现。

        行不存在**不抛**，返回 `row_deleted=False` 的 DeleteOutcome——四条核心
        管线的删除路由都是靠这个字段翻 404 的（`dem_api.py:132-133`），这里改抛
        KeyError 等于让 T8 单独为插件写一套 try/except。`delete_task_row` 自己
        有一道「行本来就不存在时一片磁盘都不能碰」的闸，产物目录算不出来时传
        None 正合它的语义。
        """
        from src.services.task_deletion import delete_task_row
        task_id = int(task_id)
        row = self.get_task(task_id)
        with self._state_lock:
            flag = self.stop_flags.get(task_id)
            if flag is not None:
                flag.set()
        artifact_dir = (self._task_output_dir(row)
                        if delete_files and row is not None else None)
        outcome = delete_task_row(manager=self, task_id=task_id, table=_TABLE,
                                  artifact_dir=artifact_dir)
        if outcome.row_deleted:
            # 缺块行跟着任务行走。这张表刻意没有外键（删除路径是「先停线程再
            # 删行」，工作线程还可能在 flush），不在这里删的话它们要等到**下次
            # 启动**的孤儿扫描才消失 —— 用户删了任务，磁盘上的行还在。
            self._purge_tile_rows(task_id)
        return outcome

    @staticmethod
    def _purge_tile_rows(task_id: int) -> None:
        """删掉某任务的缺块行。**不抛**：任务行已经删了，这只是收尾。"""
        try:
            conn = get_connection()
            try:
                conn.execute('DELETE FROM plugin_task_tiles WHERE task_id = ?',
                             (int(task_id),))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning('插件任务 %s 的缺块行清理失败（留给启动扫描）：%r',
                           task_id, e)

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

        # 第二层：任务行已经不存在的 plugin_task_tiles 残留。删除路径现在会
        # 就地清（见 `delete_task`），这里是兜底：commit 失败、或者进程在
        # 「删任务行」与「删缺块行」之间死掉时留下的行。代价只是行堆积——
        # `plugin_tasks.id` 是 AUTOINCREMENT，id 不复用，这些行不会被将来的
        # 任务算成自己的洞。
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
