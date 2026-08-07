"""下载吞吐计 —— 滑动窗口字节速率，供任务行显示「当前下载速度」。

为什么在后端算而不是前端：task_progress 的 emit 本来就按 0.5-1s 节流，但
Socket.IO 送达有抖动，而且浏览器**后台标签页会节流事件循环** —— 切回前台时
积压的事件一次性涌入，前端按「到达时间」算出的速率会瞬间飙到几百 MB/s。
后端用 time.monotonic() 采样不受这两者影响，map / DEM / 等高线三条下载路径
也能共用同一份实现，页面刷新后也不必等两发事件才有数。

**口径：只记真正走网络的字节。** 磁盘缓存命中的字节不能计入 —— 那是本地
读盘，算进去会让「网速」虚高一个数量级。过滤在调用方做（各 task_manager
的接线处有注释说明各自怎么识别缓存命中），这里只负责算。

速度是瞬时量，**不落库**：tasks / dem_tasks / contour_tasks 都没有对应列，
它只活在 task_progress 的推送载荷里。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Deque, Tuple


class SpeedMeter:
    """滑动窗口字节吞吐计。

    非线程安全：每个任务一个实例，只在该任务自己的下载事件循环里调用。

        meter = SpeedMeter()
        meter.record(len(data))   # 每笔**网络**字节；非网络的进度事件传 0
        meter.bps()               # 当前速率，字节/秒
    """

    __slots__ = ('_window', '_clock', '_total', '_samples')

    def __init__(
        self,
        window: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if window <= 0:
            raise ValueError(f'window must be positive, got {window!r}')
        self._window = window
        self._clock = clock
        self._total = 0
        # (时刻, 该时刻的累计字节)；左端是窗口起点，右端是最近一次 record。
        self._samples: Deque[Tuple[float, int]] = deque()
        self._samples.append((clock(), 0))

    def record(self, n_bytes: int) -> None:
        """累加一笔网络字节并打一个时间样本。

        **每次进度回调都要调**，哪怕这一笔没有网络字节（缓存命中、失败、
        本地渲染）—— 传 0 让时间窗照常前进，速率才会在下载变慢或停滞时
        如实回落。只在有字节时不调，速率会一直冻在最后那个高值上。
        """
        if n_bytes > 0:
            self._total += n_bytes
        now = self._clock()
        self._samples.append((now, self._total))

        # 驱逐超窗样本，但**至少留两个**，且左端只保留一个窗外样本：
        # 慢速下载（几秒才一笔）时样本本就稀疏，把窗外的全丢掉会只剩一个
        # 样本、时间跨度为 0，bps() 直接退化成算不出来。
        cutoff = now - self._window
        while len(self._samples) > 2 and self._samples[1][0] <= cutoff:
            self._samples.popleft()

    def bps(self) -> float:
        """窗口内平均速率（字节/秒）。样本不足或时间跨度非正时返回 0.0。"""
        if len(self._samples) < 2:
            return 0.0
        t0, b0 = self._samples[0]
        t1, b1 = self._samples[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        return (b1 - b0) / dt
