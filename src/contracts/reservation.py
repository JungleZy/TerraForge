"""ResourceReservation —— 全局资源准入凭据。

## 它解决的问题

改造前**没有任何跨任务的资源上界**。四个 manager 各有独立线程注册表，唯一
守卫是「同 task_id 是否已在跑」；真正的限流全在任务内部：

    asyncio.Semaphore(concurrent_downloads)  download_engine / dem_download_engine
    ProcessPoolExecutor(min(4, cpu_count))   contour_engine / cesium_terrain
    ThreadPoolExecutor(min(4, cpu_count))    download_engine 的逐块 georef

默认 `concurrent_downloads = 50`。四个任务并行 = 四套全部叠加 = 200 条连接
加 8 个工作进程，无上界。GeoDownloader 是同一个形状（6 处彼此独立的
`tokio::spawn`，唯一准入是同 task_id 去重），所以这是**两方共同短板**，
照抄谁都补不上。

## 形制：配额，不是信号量

`ResourceScheduler` 不持有会阻塞的信号量，它发**配额**。理由：

- 四条管线的执行体形态完全不同（asyncio 事件循环 / 进程池 / GDAL 同步调用），
  没有一个共同的 await 点可以挂;
- 阻塞式准入会让「起任务」这个 HTTP 请求挂住，而现有 `start_task` 是在
  `_state_lock` 里同步跑的 —— 挂住等于锁住整个 manager;
- 部分授予比排队更有用：网络连接数从 50 降到 12 仍然能跑完，只是慢一点。

所以 `reserve()` 立刻返回，要么给一个 `ResourceReservation`（`granted` 可能
小于 `requested`，但不小于 `minimum`），要么给 `None`（真的一格都没有）。
调用方拿 `granted` 去构造自己的信号量 / 进程池，结束时 `release()`。

## 磁盘是特例

磁盘预算是**预留**而不是限流：多个任务不能各自通过同一份剩余空间的预检
（§4.2）。所以 `DISK_BYTES` 的 `granted` 必须等于 `requested`，拿不到全额就
拿不到 —— 半个磁盘预算没有意义。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional

__all__ = ['ResourceKind', 'ResourceRequest', 'ResourceReservation']


class ResourceKind(Enum):
    """可预留的资源种类。"""

    #: 并发 HTTP 连接数（aiohttp 连接池 + 信号量的尺寸）
    NETWORK = 'network'
    #: CPU 工作进程数（ProcessPoolExecutor 的 max_workers）
    CPU_WORKER = 'cpu_worker'
    #: 并发 GDAL 重活槽位（Warp / Translate / BuildVRT）
    GDAL_SLOT = 'gdal_slot'
    #: 磁盘字节数（全额或不给）
    DISK_BYTES = 'disk_bytes'
    #: 同时在跑的任务数
    TASK_SLOT = 'task_slot'

    @property
    def all_or_nothing(self) -> bool:
        return self in (ResourceKind.DISK_BYTES, ResourceKind.TASK_SLOT)


@dataclass(frozen=True)
class ResourceRequest:
    """一次准入请求里的一项。

    minimum
        低于它就宁可不给。网络连接的 minimum 通常是 1（一条连接也能跑完），
        磁盘的 minimum 等于 requested（见模块 docstring）。
    """

    kind: ResourceKind
    requested: int
    minimum: int = 1

    def __post_init__(self):
        if self.requested < 0:
            raise ValueError(f"requested must be >= 0, got {self.requested}")
        if self.minimum < 0:
            raise ValueError(f"minimum must be >= 0, got {self.minimum}")
        if self.minimum > self.requested:
            raise ValueError(
                f"minimum ({self.minimum}) must not exceed requested ({self.requested})")
        if self.kind.all_or_nothing and self.minimum != self.requested:
            raise ValueError(
                f"{self.kind.value} is all-or-nothing: minimum must equal requested")


@dataclass
class ResourceReservation:
    """一次成功准入的凭据。**必须** release，否则配额永久泄漏。

    owner
        `(pipeline, task_id, purpose)`，例如 `('map', 12, 'download')`。
        同一个任务可以同时持有多张（下载一张、切片一张），purpose 区分。
    granted
        实际授予量，`{ResourceKind: int}`。可能小于请求量（磁盘与任务槽除外）。
    released
        幂等标记。`_run_task` 的 finally 与异常补偿路径都会调 release，
        不能第二次把配额还回去。
    """

    owner: tuple
    granted: Dict[ResourceKind, int]
    _release_cb: Optional[Callable[['ResourceReservation'], None]] = field(
        default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    released: bool = False

    def get(self, kind: ResourceKind, default: int = 0) -> int:
        return self.granted.get(kind, default)

    @property
    def network(self) -> int:
        return self.granted.get(ResourceKind.NETWORK, 0)

    @property
    def cpu_workers(self) -> int:
        return self.granted.get(ResourceKind.CPU_WORKER, 0)

    @property
    def disk_bytes(self) -> int:
        return self.granted.get(ResourceKind.DISK_BYTES, 0)

    def release(self) -> bool:
        """归还配额。已归还时返回 False（不是错误）。"""
        with self._lock:
            if self.released:
                return False
            self.released = True
            cb = self._release_cb
        if cb is not None:
            cb(self)
        return True

    def __enter__(self) -> 'ResourceReservation':
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    def summary(self) -> str:
        parts = ', '.join(f'{k.value}={v}' for k, v in sorted(
            self.granted.items(), key=lambda kv: kv[0].value) if v)
        return f'{"/".join(str(p) for p in self.owner)}: {parts or "nothing"}'
