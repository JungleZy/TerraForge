"""ResourceScheduler —— 全进程资源配额中介（§4.1）。

## 改造前的实际数字

四个 manager 各有独立的 `active_tasks` / `_state_lock`，**唯一**的准入判断是
「同 task_id 是否已经在跑」。真正决定机器负载的四个数字全部长在任务体内部，
彼此不知道对方存在：

    download_engine.py:878       asyncio.Semaphore(concurrent_downloads)
    dem_download_engine.py:181   asyncio.Semaphore(concurrent_downloads)
    contour_engine.py:846        ProcessPoolExecutor(min(4, cpu_count))
    terrain_tiling/cesium_terrain.py:1657
                                 ProcessPoolExecutor(min(4, cpu_count))

`concurrent_downloads` 的出厂值是 50（DEFAULT_CONFIGS）。于是 N 个任务同时跑
= N 套全部叠加：两个地图任务 + 一个 DEM 任务就是 150 条并发 HTTP 连接，再加
一个等高线任务就是额外 4 个 spawn 出来的 Python 解释器进程。**没有任何上界**
—— 不是「上界配错了」，是代码里根本不存在「总量」这个概念。表现出来就是
家用带宽被自己打满导致大面积超时重试、内存被四套 GDAL 峰值顶爆 OOM。

GeoDownloader 是同一个形状（6 处彼此独立的 `tokio::spawn`，唯一准入是同
task_id 去重），所以这是**两方共同的短板**，照抄谁都补不上，必须自己补。

## 为什么发配额，而不是用阻塞信号量

阻塞式准入（`semaphore.acquire()` 挂住直到有名额）是最直觉的做法，在这里
是**死锁**：

1. 四条管线的执行体形态互不相同 —— 地图/DEM 在 asyncio 事件循环里，等高线
   与地形切片在 `ProcessPoolExecutor` 里，GDAL 调用是彻底同步的 C 扩展。
   没有任何一个共同的 await 点可以把「等名额」挂上去。
2. 更要命的是准入发生的位置：`start_task` 是在各 manager 的 `_state_lock`
   **里面**同步跑的。在那里阻塞 = 把整个 manager 锁死 —— 连「查询任务列表」
   「停止另一个任务」都会跟着挂住，而能解锁的恰恰是那些被挡在门外的操作。
   用户看到的是整个页面转圈，不是「任务在排队」。

所以 `reserve()` **永不阻塞**：立刻返回一张 `ResourceReservation`（`granted`
可能小于 `requested`）或者 `None`。调用方拿 `granted` 去构造自己的信号量 /
进程池，结束时 `release()`。

## 为什么允许部分授予

拿 12 条连接的任务会跑完，只是慢一点；拿不到名额而根本没起的任务永远不会
跑完。对下载这种「并发度只影响吞吐、不影响正确性」的资源，部分授予严格优于
排队。所以除 `TASK_SLOT` / `DISK_BYTES`（见 `ResourceKind.all_or_nothing`）
之外一律 `min(requested, available)`，只在低于 `minimum` 时才整单拒绝。

## DISK_BYTES 为什么在这里没有上限

磁盘的天花板是**物理剩余空间**，那份判断归 `disk_budget` 管，抄到这里就是
第二处事实来源。但磁盘仍然必须在本模块记账：两个任务如果各自对着同一份
「剩余 20 GB」做预检，两边都会通过，然后一起把盘写满。所以 `DISK_BYTES` 是
一个**只记账、不设限**的种类 —— `reserve` 永远全额授予它，`snapshot()` 里
的 limit/available 报 `None`，而 `in_use` 是真实的未归还预留量，供
`disk_budget.check_budget` 从剩余空间里先扣掉。

## limits() 每次都读库

`limits()` 每次调用都走一次 `get_many`（一条 SQL、四个键）。这是有意的：
用户在设置页把并发从 64 改到 16，下一个任务就该按 16 走，不需要重启服务。
`reserve()` 的频率是「每个任务一到两次」，不是每块瓦片一次，这点开销可以忽略。
读库在锁**外**完成 —— 把 sqlite 的 I/O 放进全局锁里会让所有 reserve 串行地
等磁盘。代价是「刚改小上限」的瞬间可能多批出一份配额，下一次 reserve 就会
收敛，可接受。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict, Iterable, List, Optional, Tuple

from src.contracts.reservation import (
    ResourceKind,
    ResourceRequest,
    ResourceReservation,
)
from src.core.database import DEFAULT_CONFIGS

logger = logging.getLogger(__name__)

__all__ = [
    'ResourceScheduler',
    'get_scheduler',
    'plan_download_reservation',
    'plan_tiling_reservation',
    'reset_scheduler',
]


# 资源种类 -> 配置键。这张表是「哪个设置项管哪种资源」的唯一映射，
# 设置页、状态接口与本模块都从这里取，不各写一份。
_CONFIG_KEY_BY_KIND: Dict[ResourceKind, str] = {
    ResourceKind.TASK_SLOT: 'max_concurrent_tasks',
    ResourceKind.NETWORK: 'max_network_connections',
    ResourceKind.CPU_WORKER: 'max_cpu_workers',
    ResourceKind.GDAL_SLOT: 'max_gdal_slots',
}

# 只记账、不设限的种类（理由见模块 docstring「DISK_BYTES 为什么在这里没有上限」）
_UNCAPPED_KINDS = frozenset({ResourceKind.DISK_BYTES})

_CONFIG_KEYS: Tuple[str, ...] = tuple(_CONFIG_KEY_BY_KIND.values())

# 兜底值直接从 DEFAULT_CONFIGS 取，不在这里抄第二份数字 —— 抄一份就意味着
# 「改了出厂默认但忘了改兜底」时，配置库坏掉的降级路径会用一个谁也没审过的值。
_DEFAULT_CONFIG_VALUES: Dict[str, str] = dict(DEFAULT_CONFIGS)

# 各配置键允许的下界。`max_cpu_workers` 的 0 是合法特值（= 自动挡），其余三个
# 「0 个任务槽 / 0 条连接 / 0 个 GDAL 槽」等价于服务停摆，当脏值处理。
_MIN_CONFIG_VALUE: Dict[str, int] = {
    'max_concurrent_tasks': 1,
    'max_network_connections': 1,
    'max_cpu_workers': 0,
    'max_gdal_slots': 1,
}

# 自动挡 CPU 工作进程数的上界。与改造前每个任务各自的 min(4, cpu_count) 逐字
# 一致 —— 区别只在于这个数现在是**全局**的，不再按任务数叠加。
_AUTO_CPU_CAP = 4

# 「同一个坏值只警告一次」的去重集合，键是 (config_key, raw_value)。
# limits() 每次 reserve 都会调，不去重的话一个拼错的配置值会把日志刷爆；
# 用 raw 值入键，所以用户把坏值改成另一个坏值时仍会再警告一次。
_WARNED_VALUES: set = set()
_WARN_LOCK = threading.Lock()


def _warn_once(dedupe_key, message: str) -> None:
    with _WARN_LOCK:
        if dedupe_key in _WARNED_VALUES:
            return
        _WARNED_VALUES.add(dedupe_key)
    logger.warning(message)


def _coerce_limit(key: str, raw) -> int:
    """把一个配置值解析成资源上界；脏值 / 缺失一律退回 DEFAULT_CONFIGS 的值。

    这里**绝不能抛**：配置库是用户可写的，一个手输的 'abc' 不该让所有任务都
    起不来。降级路径是「记一次 warning + 用出厂默认」，出厂默认本身由
    config_manager 的 _VALUE_RULES 保证在合法区间内。
    """
    fallback = int(_DEFAULT_CONFIG_VALUES[key])
    floor = _MIN_CONFIG_VALUE[key]
    if raw is None:
        # 配置行不存在 = 库比代码旧（该批键是 §4.1 新增的，存量库首次启动
        # 前没有这些行）。init_database 的 INSERT OR IGNORE 会补上，在那之前
        # 按出厂默认跑是正确行为，但仍要留痕，否则「设置页改了没反应」无从查起。
        _warn_once((key, None),
                   f'config {key} missing, falling back to default {fallback}')
        return fallback
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        _warn_once((key, raw),
                   f'config {key}={raw!r} is not an integer, '
                   f'falling back to default {fallback}')
        return fallback
    if value < floor:
        _warn_once((key, raw),
                   f'config {key}={raw!r} is below the minimum {floor}, '
                   f'falling back to default {fallback}')
        return fallback
    return value


def _auto_cpu_workers() -> int:
    """自动挡的 CPU 工作进程数。`os.cpu_count()` 在容器里可能返回 None。"""
    return min(_AUTO_CPU_CAP, os.cpu_count() or 1)


class ResourceScheduler:
    """全进程配额中介。非阻塞，线程安全，无自己的持久化状态。

    生命周期与进程一致（见 `get_scheduler`）。**它不知道任务是什么**：只认
    `owner` 这个不透明的可哈希键和一组 `ResourceRequest`。进程重启后所有配额
    自动归零 —— 这是正确的，因为持有配额的线程也一起没了。
    """

    def __init__(self, config_manager=None):
        # ConfigManager 惰性构造：本模块会被 routes / manager 在 import 期拉起，
        # 那时 data/ 目录与 config 表可能都还不存在。构造一个 ConfigManager 本身
        # 不碰库（它的方法才碰），但保持惰性可以让调用方随时注入测试替身。
        self._config_manager = config_manager
        self._config_lock = threading.Lock()

        # 一把 RLock 护住 _in_use 与 _owners 两份状态：release 回调是在
        # ResourceReservation.release() 里被调的，而 release_owner 可能已经
        # 持有本锁，所以必须可重入。
        self._lock = threading.RLock()
        self._in_use: Dict[ResourceKind, int] = {}
        self._owners: Dict[tuple, ResourceReservation] = {}

    # ---------------------------------------------------------------- config

    def _config(self):
        if self._config_manager is None:
            with self._config_lock:
                if self._config_manager is None:
                    from src.services.config_manager import ConfigManager
                    self._config_manager = ConfigManager()
        return self._config_manager

    def limits(self) -> Dict[ResourceKind, int]:
        """当前各**有上限**种类的天花板。每次调用都读一次库（理由见模块 docstring）。

        `DISK_BYTES` 不在返回值里 —— 它没有本模块能给出的上界。
        """
        try:
            raw = self._config().get_many(_CONFIG_KEYS)
        except Exception as e:
            # ConfigManager.get_many 对 sqlite 错误是**有意重抛**的（不静默吞成
            # 默认值）。但在调度器这一层「库读不到」不该等于「任务全部起不来」：
            # 退回出厂默认让服务继续跑，比让整个下载功能瘫掉更接近用户预期。
            _warn_once(('__read__', str(e)),
                       f'failed to read scheduler limits from config ({e}); '
                       f'falling back to DEFAULT_CONFIGS')
            raw = {}

        limits: Dict[ResourceKind, int] = {}
        for kind, key in _CONFIG_KEY_BY_KIND.items():
            value = _coerce_limit(key, raw.get(key))
            if kind is ResourceKind.CPU_WORKER and value == 0:
                # 0 = 自动挡（DEFAULT_CONFIGS 的出厂值就是 0，_VALUE_RULES 也把
                # 0 列为合法），在这里展开成具体数字，让 snapshot() 报的是用户
                # 真正能拿到的名额，而不是一个需要二次解释的 0。
                value = _auto_cpu_workers()
            limits[kind] = value
        return limits

    # ----------------------------------------------------------- reservation

    def reserve(self, owner: tuple, requests) -> Optional[ResourceReservation]:
        """一次性、原子地申请一组资源。拿不到就返回 `None`，**绝不半批**。

        owner
            `(pipeline, task_id, purpose)` 之类的可哈希元组。同一个 owner 只能
            有一张未归还的凭据 —— 重复申请是调用方 bug（意味着上一张泄漏了，
            那份配额永远回不来），直接 `ValueError`，不静默覆盖。
        requests
            `ResourceRequest` 的可迭代对象。同种类出现多次会**求和**后统一裁决
            —— 凭据里每种资源只有一个数字，求和是唯一不丢信息的合并方式，也让
            `plan_download_reservation() + 额外请求` 这种拼装能正常工作。

        返回 `None` 的两种情形：全额或不给的种类（`TASK_SLOT` / `DISK_BYTES`）
        余额不足；可分割种类算出来的份额低于 `minimum`。两种情形下 `_in_use`
        与 `_owners` 都不会被改动 —— 授予量先攒在局部 dict 里，全部通过后才落账。
        """
        try:
            hash(owner)
        except TypeError as e:
            raise ValueError(f'reservation owner must be hashable, got {owner!r}') from e

        folded = self._fold(requests)

        # 读库放在锁外：sqlite 的 I/O 挤进全局锁会让所有 reserve 排队等磁盘。
        limits = self.limits()

        with self._lock:
            if owner in self._owners:
                raise ValueError(
                    f'owner {owner!r} already holds a reservation '
                    f'({self._owners[owner].summary()}); release it first')

            granted: Dict[ResourceKind, int] = {}
            for kind, (requested, minimum) in folded.items():
                if kind in _UNCAPPED_KINDS:
                    # 只记账：全额授予，唯一作用是让并发的预检互相看得见。
                    granted[kind] = requested
                    continue

                available = max(0, limits[kind] - self._in_use.get(kind, 0))
                if kind.all_or_nothing:
                    if requested > available:
                        logger.debug(
                            'reserve %r denied: %s needs %d, only %d free',
                            owner, kind.value, requested, available)
                        return None
                    granted[kind] = requested
                else:
                    amount = min(requested, available)
                    if amount < minimum:
                        logger.debug(
                            'reserve %r denied: %s could only offer %d, '
                            'below minimum %d', owner, kind.value, amount, minimum)
                        return None
                    granted[kind] = amount

            for kind, amount in granted.items():
                self._in_use[kind] = self._in_use.get(kind, 0) + amount

            reservation = ResourceReservation(
                owner=owner,
                granted=granted,
                _release_cb=self._on_release,
            )
            self._owners[owner] = reservation
            logger.debug('reserved %s', reservation.summary())
            return reservation

    @staticmethod
    def _fold(requests: Iterable[ResourceRequest]) -> Dict[ResourceKind, Tuple[int, int]]:
        """把请求列表按种类合并成 `{kind: (requested, minimum)}`。"""
        folded: Dict[ResourceKind, Tuple[int, int]] = {}
        for req in requests:
            if not isinstance(req, ResourceRequest):
                raise ValueError(
                    f'requests must contain ResourceRequest, got {type(req).__name__}')
            prev = folded.get(req.kind)
            if prev is None:
                folded[req.kind] = (req.requested, req.minimum)
            else:
                folded[req.kind] = (prev[0] + req.requested, prev[1] + req.minimum)
        return folded

    def _on_release(self, reservation: ResourceReservation) -> None:
        """归还回调。由 `ResourceReservation.release()` 调用，已保证只触发一次。"""
        with self._lock:
            for kind, amount in reservation.granted.items():
                remaining = self._in_use.get(kind, 0) - amount
                if remaining < 0:
                    # 走到这里说明有人绕过凭据直接改了账本，或者同一张凭据被
                    # 两个 scheduler 实例登记过。夹到 0 保证后续 reserve 还能用，
                    # 但必须留 error —— 负配额会让上限失效，是静默的产能过载。
                    logger.error(
                        'resource accounting underflow: %s went to %d while '
                        'releasing %s; clamping to 0',
                        kind.value, remaining, reservation.summary())
                    remaining = 0
                self._in_use[kind] = remaining
            # 身份比较而非按键删除：release_owner 可能已经把这张摘掉、同一个
            # owner 又登记了新的一张，那张不能被这次归还误删。
            if self._owners.get(reservation.owner) is reservation:
                del self._owners[reservation.owner]
        logger.debug('released %s', reservation.summary())

    def release_owner(self, owner: tuple, reservation: ResourceReservation) -> int:
        """归还**这一张**凭据，返回 1（确实归还了）或 0（它已经不是登记在册的那张）。

        `reservation` 是**必填**的，而且必须是同一个对象 —— 这不是防御性编程，
        是一次线上级缺陷的修复。

        原先的签名是 `release_owner(owner)`：按键查、按键放。四条管线都把它当
        「兜底清理」写进 except 分支里，于是出现了这样一类确定性 bug：

            1. 任务 1 正在跑，`_owners[('map',1,'download')] = R1`；
            2. 用户重复点一次「开始」——最普通的操作；
            3. 准入闸门正确地抛出「已在运行」；
            4. except 里那句无条件的 `release_owner(('map',1,'download'))`
               把**还在服役的 R1** 释放了；
            5. 任务 1 继续用着 50 条连接跑，调度器账上一格没占，全局上界失效。

        同一形状还有第二种触发：线程 A 收尾（已 release）与用户立刻恢复（拿到
        R2）之间有一个可长达数十秒的窗口（`fail_stranded_running_task` 会新开
        一条 busy_timeout=30s 的 sqlite 连接），A 迟到的按键归还会摘掉 R2。

        `_on_release` 早就用身份比较挡住了后半段（见其注释），入口这边却没有。
        现在两边一致：**没有凭据对象就没有归还权**。一个 except 分支如果自己
        没申请到过凭据，它在语法上就拿不出这个参数，也就写不出这个 bug。

        「那真泄漏了怎么办」：不会。每一次 `reserve` 成功都由同一个 `finally`
        配对归还；配不上对的唯一情形是进程死掉，而配额是进程内状态，跟着一起
        没了。为「可能泄漏」保留一个能误删活凭据的后门，代价远大于收益。
        """
        if reservation is None:
            raise ValueError(
                'release_owner requires the reservation object; releasing by owner '
                'key alone can revoke a live reservation (see this docstring)')
        with self._lock:
            registered = self._owners.get(owner)
        if registered is not reservation:
            return 0
        # 在锁外调 release()：它会回头拿本锁（RLock，同线程重入也安全），
        # 放锁外是为了不把 reservation 自己那把锁套在全局锁里面。
        return 1 if reservation.release() else 0

    # -------------------------------------------------------------- 观测

    def snapshot(self) -> dict:
        """可直接 `json.dumps` 的当前配额视图，供状态接口使用。

        `ResourceKind` 一律渲染成 `.value` 字符串；`DISK_BYTES` 的 limit 与
        available 是 `None`（它没有本模块给出的上界，报 0 会被读成「满了」）。
        """
        limits = self.limits()
        with self._lock:
            in_use = dict(self._in_use)
            owners = [
                {
                    'owner': list(owner),
                    'granted': {k.value: v for k, v in reservation.granted.items()},
                }
                for owner, reservation in self._owners.items()
            ]

        limits_out = {}
        in_use_out = {}
        available_out = {}
        for kind in ResourceKind:
            used = in_use.get(kind, 0)
            in_use_out[kind.value] = used
            if kind in _UNCAPPED_KINDS:
                limits_out[kind.value] = None
                available_out[kind.value] = None
            else:
                limit = limits[kind]
                limits_out[kind.value] = limit
                available_out[kind.value] = max(0, limit - used)

        return {
            'limits': limits_out,
            'in_use': in_use_out,
            'available': available_out,
            'owners': owners,
        }


# ---------------------------------------------------------------- 进程单例

_SCHEDULER: Optional[ResourceScheduler] = None
_SCHEDULER_LOCK = threading.Lock()


def get_scheduler(config_manager=None) -> ResourceScheduler:
    """进程单例。**必须**共享 —— 每个 manager 各造一个等于没有全局上界。

    `config_manager` 只在第一次调用（真正构造时）生效，之后传入会被忽略：
    单例已经在服务别人了，中途换配置源会让不同任务按不同上限记账。
    """
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None:
            _SCHEDULER = ResourceScheduler(config_manager=config_manager)
        elif config_manager is not None and config_manager is not _SCHEDULER._config_manager:
            logger.debug('get_scheduler: ignoring config_manager, singleton already built')
        return _SCHEDULER


def reset_scheduler() -> None:
    """丢弃单例。**仅供测试**：生产代码里调它等于把在跑任务的配额账本清零。"""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        old = _SCHEDULER
        _SCHEDULER = None
    if old is not None:
        with old._lock:
            leaked = len(old._owners)
        if leaked:
            logger.warning('reset_scheduler dropped %d outstanding reservation(s)', leaked)


# ------------------------------------------------------------ 标准请求组合

def plan_download_reservation(requested_connections: int) -> List[ResourceRequest]:
    """下载类任务（地图 / DEM）的标准请求：一个任务槽 + 想要的连接数。

    连接数的 `minimum=1`：一条连接也能把任务跑完，只是慢。宁可慢，不可不起
    —— 这正是「部分授予」存在的理由（见模块 docstring）。
    """
    return [
        ResourceRequest(kind=ResourceKind.TASK_SLOT, requested=1, minimum=1),
        ResourceRequest(kind=ResourceKind.NETWORK,
                        requested=max(1, int(requested_connections)), minimum=1),
    ]


def plan_tiling_reservation(requested_workers: int) -> List[ResourceRequest]:
    """切片 / 渲染类任务（等高线、Cesium 地形）的标准请求。

    GDAL 槽请求 requested=minimum=1：GDAL_SLOT 在合同里是可分割种类，但
    「半个槽」没有意义 —— Warp/Translate 的内存峰值取决于单次操作的栅格尺寸
    而非线程数，所以把 minimum 顶到 requested，语义上等价于全额或不给。
    """
    return [
        ResourceRequest(kind=ResourceKind.TASK_SLOT, requested=1, minimum=1),
        ResourceRequest(kind=ResourceKind.CPU_WORKER,
                        requested=max(1, int(requested_workers)), minimum=1),
        ResourceRequest(kind=ResourceKind.GDAL_SLOT, requested=1, minimum=1),
    ]
