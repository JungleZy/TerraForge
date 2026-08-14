"""TaskContext：插件在运行期唯一能碰的门面（规格 §5）。

§13-4 契约第 2 条（复用 scheduler / 日志 / Artifact，不许自带并发与缓存
目录）不靠文档约束——插件拿不到任何 manager，只能拿到这个对象。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Optional

from src.contracts.artifact import Artifact
from src.contracts.outcome import TileOutcome
from src.core.database import get_connection, utc_now_iso
from src.services.system_proxy import mask_text_secrets

logger = logging.getLogger(__name__)

_FLUSH_BATCH_SIZE = 200

#: 落库失败时最多留住多少条待重试的 outcome。有界回填的上界：宿主对同一问题
#: 的既有做法是「写盘失败退回队列头部，交给后续 flush 重试」
#: （`task_manager.py:2232-2244` 的 `_restore_progress_batch`），丢批会让
#: `gap_tiles` 偏小、带洞的任务被判成干净的 COMPLETED —— 正是 §13-3 要防的。
#: 但无界回填在持续故障下会把内存吃光，所以设上限，越界才丢并大声报。
_MAX_RETAINED = 5000

#: `log(level=...)` 的允许值。必须是允许清单而不是把插件传进来的字符串直接
#: 喂给 getattr —— `level='__init__'` 会**静默**重建 TaskLogger：`enabled`
#: 变 False、`_logger` 被换成 `task.%s.<消息>`，而文件 handler 还挂在旧 logger
#: 上且 `self._handler` 已置 None，后续 `close()` 直接 return，句柄永久泄漏。
#: 不是「日志被关掉」，是日志被劫持 + handler 泄漏。
#: 顺带修掉另一条：`level='close'` 会抛 TypeError（`close(self)` 不收参数），
#: 捅穿 TaskLogger「所有方法都不抛」的不变量，把异常丢回插件自己的 run()。
#: 不含 `critical`/`fatal`：TaskLogger 没有这两个方法（只有 debug/info/
#: warning/error/exception），列进来就是给「静默降级到 info」制造机会。
_LOG_LEVELS = frozenset({'debug', 'info', 'warning', 'error', 'exception'})


class TaskContext:
    """一个插件任务一次运行的上下文。线程安全：outcome 缓冲有锁。"""

    def __init__(self, *, task_id, plugin_id, region, params, output_dir,
                 snapshot, stop_flag, tlog, emit_progress, granted,
                 config_manager):
        self.task_id = int(task_id)
        self.plugin_id = plugin_id
        self.region = region
        # 防御性拷贝 + 只读视图：params 直接存引用时 `ctx.params is params`，
        # 插件改一个键就渗回宿主（T7 拿它落库、失败重试还要再用一次）。
        # 规格 §5 写的类型是 Mapping，MappingProxyType 满足它，改这里零成本。
        self.params = MappingProxyType(dict(params or {}))
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot = snapshot
        self._stop_flag = stop_flag
        self._tlog = tlog
        self._emit_progress = emit_progress
        # granted 同理拷一份。T7 那边已经传 `dict(reservation.granted)`（配额
        # 账本就在那个 dict 里，改一个数字 = 永久配额泄漏），这里是纵深防御，
        # 顺带盖住不经 T7 直接构造 ctx 的路径（测试、未来的其它宿主）。
        self._granted = dict(granted or {})
        self._config_manager = config_manager
        self._outcome_lock = threading.Lock()
        self._outcome_buffer = []          # (z, x, y, outcome_value, error)
        self._success_buffer = []          # 成功后要删行的 (z, x, y)
        self._closed = False
        self._proxy_cache = None           # proxy_url() 的一次运行内缓存

    # ------------------------------------------------------------ 生命周期

    def stop_requested(self) -> bool:
        return self._stop_flag.is_set()

    def close(self) -> None:
        """收尾：落库剩下的 outcome，之后拒收新的。**不碰 tlog。**

        为什么不关日志（与简报原稿的差别）：`_outcome_buffer` 的生命周期是
        **插件这一次运行**，`tlog` 的生命周期是**整个任务**——管理器要在
        `run()` 返回之后才写终态那几行（`tlog.event('terminal', ...)`，形制见
        `task_manager.py:1735-1736`）。谁在这里关 tlog，谁就把终态日志写进一个
        已经摘掉文件 handler 的 logger，任务日志缺最后一行，正是 §4.5 要防的。
        `src/plugins/task_manager.py:246-249,324-325` 已经据此只调
        `flush_outcomes()`、把 tlog 开关留在最外层——那就更不能让插件通过公开
        面上的 `close()` 把宿主的日志句柄摘掉（与 `log('x','__init__')` 同类）。
        """
        self.flush_outcomes()
        self._closed = True

    # ------------------------------------------------------------ 进度与日志

    def progress(self, done: int, total: int, phase: str = '') -> None:
        if self._emit_progress is not None:
            try:
                self._emit_progress(done, total, phase)
            except Exception as e:
                logger.warning('插件进度回调失败（已忽略）：%r', e)

    def log(self, message: str, level: str = 'info') -> None:
        name = level if level in _LOG_LEVELS else 'info'
        if self._tlog is not None:
            getattr(self._tlog, name)('%s', message)
        else:
            getattr(logger, name)('[plugin:%s #%s] %s', self.plugin_id,
                                  self.task_id, message)

    def log_event(self, kind: str, **fields) -> None:
        """结构化事件。`tlog` 缺席时回落到模块 logger，与 `log()` 一致——
        原来这里是静默丢，同一个插件的 `log()` 看得见、`log_event()` 看不见，
        排查时最难受的那种不一致。"""
        if self._tlog is not None:
            self._tlog.event(kind, **fields)
        else:
            logger.info('[plugin:%s #%s] EVENT %s %s', self.plugin_id,
                        self.task_id, kind, fields)

    # ------------------------------------------------------------ 资源与网络

    def granted(self, kind) -> int:
        return self._granted.get(kind, 0)

    def check_url(self, url: str, allow_private: bool = False) -> str:
        """SSRF 闸（§8.1-3/4 降级后保留的廉价防护）。插件发请求前必须过这道。"""
        from src.services.url_guard import ensure_fetchable_url
        return ensure_fetchable_url(url, allow_private=allow_private)

    def proxy_url(self) -> str:
        """生效代理。一次运行内只解析一次。

        必须缓存：`resolve_from_config` 会阻塞到探测超时
        （`proxy_autodetect.py:525-526` 明确警告），而插件的下载循环里每块瓦片
        问一次是很自然的写法。代理在一次运行内不会变，重复探测纯粹是等待。
        """
        if self._proxy_cache is None:
            from src.services.config_manager import ConfigManager
            from src.services.proxy_autodetect import resolve_from_config
            self._proxy_cache = resolve_from_config(
                self._config_manager or ConfigManager())
        return self._proxy_cache

    def cache_path(self, z: int, x: int, y: int) -> Path:
        """源命名空间下的缓存路径。没有 snapshot 的插件不该调它。"""
        if self.snapshot is None:
            raise RuntimeError('该任务没有绑定数据源，无缓存命名空间')
        from src.services import source_registry
        return source_registry.tile_cache_path(self.snapshot, z, x, y)

    # ------------------------------------------------------------ 缺块记账

    def record_tile_outcome(self, z: int, x: int, y: int,
                            outcome: TileOutcome,
                            error: Optional[str] = None) -> None:
        """攒批记账。success 语义是「消除缺块行」（补漏成功要从表里抹掉）。

        `close()` 之后记的一律丢弃并记 warning——不抛，本模块的记账路径没有
        任何抛出的权力（插件正跑在下载循环里）。
        """
        if self._closed:
            logger.warning('插件任务 %s 已收尾，丢弃 %s/%s/%s 的 outcome %s',
                           self.task_id, z, x, y, outcome)
            return
        with self._outcome_lock:
            if outcome is TileOutcome.SUCCESS:
                self._success_buffer.append((z, x, y))
            else:
                # `error` 是插件给的文本，多半就是它自己的异常 repr——里面可能
                # 整条带 token 的 URL 都在。脱敏在**落库之前**做：这一列会被
                # `gap_summary` 的 samples 原样吐给浏览器，也会进诊断包与备份，
                # 存下来那一刻就已经泄漏了（与 download_engine 同一口径）。
                self._outcome_buffer.append(
                    (z, x, y, outcome.value,
                     mask_text_secrets(error) if error else error))
            if (len(self._outcome_buffer) + len(self._success_buffer)
                    >= _FLUSH_BATCH_SIZE):
                self._flush_locked()

    def flush_outcomes(self) -> None:
        with self._outcome_lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._outcome_buffer and not self._success_buffer:
            return
        upserts, deletes = self._outcome_buffer, self._success_buffer
        self._outcome_buffer, self._success_buffer = [], []
        try:
            conn = get_connection()
            try:
                conn.executemany(
                    'INSERT INTO plugin_task_tiles'
                    ' (task_id, zoom, x, y, status, retry_count, error_message)'
                    ' VALUES (?, ?, ?, ?, ?, 1, ?)'
                    ' ON CONFLICT(task_id, zoom, x, y) DO UPDATE SET'
                    ' status = excluded.status,'
                    ' retry_count = plugin_task_tiles.retry_count + 1,'
                    ' error_message = excluded.error_message',
                    [(self.task_id, z, x, y, status, err)
                     for z, x, y, status, err in upserts])
                conn.executemany(
                    'DELETE FROM plugin_task_tiles'
                    ' WHERE task_id = ? AND zoom = ? AND x = ? AND y = ?',
                    [(self.task_id, z, x, y) for z, x, y in deletes])
                conn.execute(
                    'UPDATE plugin_tasks SET gap_tiles = ('
                    '  SELECT COUNT(*) FROM plugin_task_tiles'
                    '  WHERE task_id = ?) WHERE id = ?',
                    (self.task_id, self.task_id))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            # 记账失败不抛——与 task_manager 的 progress_callback 同一原则：
            # DB 层故障不能回头拖垮下载循环。但**要退回缓冲**：宿主对同一问题
            # 的既有做法就是退回（`task_manager.py:2232-2244`），丢批会让
            # gap_tiles 偏小、带洞的任务被判成干净的 COMPLETED。前插保持相对
            # 顺序，与 `_restore_progress_batch` 一致。
            logger.error('插件任务 %s 缺块记账失败：%r', self.task_id, e)
            retained = len(self._outcome_buffer) + len(self._success_buffer)
            if retained + len(upserts) + len(deletes) <= _MAX_RETAINED:
                self._outcome_buffer[:0] = upserts
                self._success_buffer[:0] = deletes
            else:
                logger.error('插件任务 %s 缺块缓冲已达上限 %d，丢弃 %d 条'
                             '（gap_tiles 将偏小）', self.task_id, _MAX_RETAINED,
                             len(upserts) + len(deletes))

    # ------------------------------------------------------------ 产物登记

    def register_artifact(self, path, kind, has_gaps: bool = False,
                          fmt: str = '', meta: Optional[dict] = None) -> None:
        """登记一件产物。路径必须落在 `output_dir` 内。

        为什么校验归属：`task_cleanup.purge_registered_artifacts:472-498`（四条
        删除路由都在调）对登记行做 `target.unlink()`——它拒符号链接、拒 rmtree
        目录，但**任务目录之外的普通文件无条件删**。对宿主四条管线安全（路径
        都是自己产的），而这个方法把路径的选择权交给了第三方代码：插件登记
        `~/.ssh/id_rsa`，用户删任务时宿主替它删掉。本类的 docstring 自己写的是
        「插件只能拿到这个对象」，那这里就是信任边界，校验只能落在这里。

        比 resolve 后的路径而不是字面路径：`output_dir/link.mbtiles` 指向
        `/etc/passwd` 时字面检查会放行。**抛** ValueError 而不是静默忽略：这是
        插件的编程错误或恶意行为，静默会让它以为登记成功了。
        """
        from src.services import artifact_store
        target = Path(path).expanduser().resolve()
        root = self.output_dir.resolve()
        if target != root and root not in target.parents:
            raise ValueError(f'产物必须落在 output_dir（{root}）内：{target}')
        bytes_total, tile_count, minzoom, maxzoom = _measure(target)
        artifact_store.record_artifact(Artifact(
            pipeline='plugin', task_id=self.task_id, kind=kind,
            path=str(target), fmt=fmt, has_gaps=bool(has_gaps),
            bytes_total=bytes_total, tile_count=tile_count,
            minzoom=minzoom, maxzoom=maxzoom,
            meta={**(meta or {}), 'plugin_id': self.plugin_id},
            created_at=utc_now_iso(),
        ))


def _measure(target: Path):
    """产物规模 → `(bytes_total, tile_count, minzoom, maxzoom)`。绝不抛。

    四条宿主管线都填这几列（`task_manager._register_artifacts:1384-1407`：目录
    走 `measure_dir`、单文件走 `stat().st_size`），插件产物不填就会在缓存管理页
    永远显示 0 B。同一份规矩，同一个 `measure_dir`。
    """
    from src.services import artifact_store
    try:
        if target.is_dir():
            return artifact_store.measure_dir(target)
        return target.stat().st_size, 0, None, None
    except OSError as e:
        # 文件刚被删 / 盘掉线。规模是描述性信息，量不到不该让登记失败。
        logger.warning('产物规模统计失败（%s）：%r', target, e)
        return 0, 0, None, None
