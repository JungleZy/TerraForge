"""TaskContext：插件在运行期唯一能碰的门面（规格 §5）。

§13-4 契约第 2 条（复用 scheduler / 日志 / Artifact，不许自带并发与缓存
目录）不靠文档约束——插件拿不到任何 manager，只能拿到这个对象。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from src.contracts.artifact import Artifact
from src.contracts.outcome import TileOutcome
from src.core.database import get_connection, utc_now_iso

logger = logging.getLogger(__name__)

_FLUSH_BATCH_SIZE = 200

#: `log(level=...)` 的允许值。必须是允许清单而不是直接 getattr——level 是插件
#: 传进来的字符串，裸 getattr 会让 `level='close'` 把任务日志关掉。
_LOG_LEVELS = frozenset({'debug', 'info', 'warning', 'error', 'exception'})


class TaskContext:
    """一个插件任务一次运行的上下文。线程安全：outcome 缓冲有锁。"""

    def __init__(self, *, task_id, plugin_id, region, params, output_dir,
                 snapshot, stop_flag, tlog, emit_progress, granted,
                 config_manager):
        self.task_id = int(task_id)
        self.plugin_id = plugin_id
        self.region = region
        self.params = params
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot = snapshot
        self._stop_flag = stop_flag
        self._tlog = tlog
        self._emit_progress = emit_progress
        self._granted = granted or {}
        self._config_manager = config_manager
        self._outcome_lock = threading.Lock()
        self._outcome_buffer = []          # (z, x, y, outcome_value, error)
        self._success_buffer = []          # 成功后要删行的 (z, x, y)

    # ------------------------------------------------------------ 生命周期

    def stop_requested(self) -> bool:
        return self._stop_flag.is_set()

    def close(self) -> None:
        self.flush_outcomes()
        if self._tlog is not None:
            self._tlog.close()

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
        if self._tlog is not None:
            self._tlog.event(kind, **fields)

    # ------------------------------------------------------------ 资源与网络

    def granted(self, kind) -> int:
        return self._granted.get(kind, 0)

    def check_url(self, url: str, allow_private: bool = False) -> str:
        """SSRF 闸（§8.1-3/4 降级后保留的廉价防护）。插件发请求前必须过这道。"""
        from src.services.url_guard import ensure_fetchable_url
        return ensure_fetchable_url(url, allow_private=allow_private)

    def proxy_url(self) -> str:
        from src.services.config_manager import ConfigManager
        from src.services.proxy_autodetect import resolve_from_config
        return resolve_from_config(self._config_manager or ConfigManager())

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
        """攒批记账。success 语义是「消除缺块行」（补漏成功要从表里抹掉）。"""
        with self._outcome_lock:
            if outcome is TileOutcome.SUCCESS:
                self._success_buffer.append((z, x, y))
            else:
                self._outcome_buffer.append((z, x, y, outcome.value, error))
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
            # DB 层故障不能回头拖垮下载循环。
            logger.error('插件任务 %s 缺块记账失败：%r', self.task_id, e)

    # ------------------------------------------------------------ 产物登记

    def register_artifact(self, path, kind, has_gaps: bool = False,
                          fmt: str = '', meta: Optional[dict] = None) -> None:
        from src.services import artifact_store
        artifact_store.record_artifact(Artifact(
            pipeline='plugin', task_id=self.task_id, kind=kind,
            path=str(path), fmt=fmt, has_gaps=bool(has_gaps),
            meta={**(meta or {}), 'plugin_id': self.plugin_id},
            created_at=utc_now_iso(),
        ))
