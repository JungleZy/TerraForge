"""ResourceScheduler —— 全进程配额中介的准入与归还语义。

改造前**没有任何跨任务的资源上界**：四个任务并行 = 四套限流全部叠加。这个模块
是那道全局闸，而它的两条最贵的规则都没有测试盯着：

1. **部分授予**（网络连接从 50 降到 12 仍然能跑完），与**全额或不给**
   （磁盘预算、任务槽）是两套裁决，混了就是「半个磁盘预算」这种没有意义的东西。
2. **`release_owner` 必须认凭据对象**。按键归还曾经造成一次线上级缺陷：用户
   重复点一次「开始」→ 准入闸正确抛「已在运行」→ except 里那句无条件的
   `release_owner(owner)` 把**还在服役的**凭据释放了 → 任务继续用着 50 条连接
   跑，调度器账上一格没占，全局上界失效。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.contracts.reservation import ResourceKind, ResourceRequest  # noqa: E402
from src.services.resource_scheduler import ResourceScheduler  # noqa: E402


class FakeConfig:
    """只回答 `get_many` 的配置替身 —— 不碰 sqlite，测试不依赖任何库文件。"""

    def __init__(self, **values):
        self.values = {str(k): str(v) for k, v in values.items()}
        self.reads = 0

    def get_many(self, keys):
        self.reads += 1
        return {k: self.values[k] for k in keys if k in self.values}

    def get(self, key, default=None):
        return self.values.get(key, default)


def _scheduler(**limits):
    defaults = dict(max_concurrent_tasks=2, max_network_connections=64,
                    max_cpu_workers=4, max_gdal_slots=2)
    defaults.update(limits)
    return ResourceScheduler(config_manager=FakeConfig(**defaults))


def _task_slot(n=1):
    return ResourceRequest(kind=ResourceKind.TASK_SLOT, requested=n, minimum=n)


def _network(requested, minimum=1):
    return ResourceRequest(kind=ResourceKind.NETWORK,
                           requested=requested, minimum=minimum)


def _disk(n):
    return ResourceRequest(kind=ResourceKind.DISK_BYTES, requested=n, minimum=n)


# ---------------------------------------------------------------------------
# 上界
# ---------------------------------------------------------------------------

def test_task_slot_cap_holds_across_owners():
    """上界是**全局**的：第三个任务拿不到槽，哪怕它是另一条管线。"""
    sched = _scheduler(max_concurrent_tasks=2)
    assert sched.reserve(('map', 1, 'download'), [_task_slot()]) is not None
    assert sched.reserve(('dem', 2, 'download'), [_task_slot()]) is not None
    assert sched.reserve(('contour', 3, 'tiling'), [_task_slot()]) is None


def test_releasing_frees_the_slot_for_the_next_owner():
    sched = _scheduler(max_concurrent_tasks=1)
    first = sched.reserve(('map', 1, 'download'), [_task_slot()])
    assert sched.reserve(('map', 2, 'download'), [_task_slot()]) is None
    first.release()
    assert sched.reserve(('map', 2, 'download'), [_task_slot()]) is not None


def test_network_is_granted_partially_down_to_the_minimum():
    """可分割种类给的是**剩下多少给多少**，不是全额或不给。

    12 条连接比不启动强 —— 阻塞式准入会让「起任务」这个 HTTP 请求挂住。
    """
    sched = _scheduler(max_network_connections=10)
    first = sched.reserve(('map', 1, 'download'), [_network(6)])
    assert first.get(ResourceKind.NETWORK) == 6
    second = sched.reserve(('dem', 2, 'download'), [_network(50, minimum=1)])
    assert second.get(ResourceKind.NETWORK) == 4       # 只剩 4，照样给


def test_network_below_the_minimum_is_refused_entirely():
    """低于 minimum 宁可不给 —— 1 条连接能跑完，0 条不能。"""
    sched = _scheduler(max_network_connections=10)
    sched.reserve(('map', 1, 'download'), [_network(10)])
    assert sched.reserve(('dem', 2, 'download'), [_network(50, minimum=4)]) is None


def test_all_or_nothing_kinds_never_come_back_partial():
    """任务槽与磁盘是全额或不给：半个磁盘预算没有意义。"""
    sched = _scheduler(max_concurrent_tasks=3)
    sched.reserve(('map', 1, 'download'), [_task_slot(2)])
    assert sched.reserve(('dem', 2, 'download'), [_task_slot(2)]) is None
    assert sched.reserve(('dem', 2, 'download'), [_task_slot(1)]) is not None


def test_disk_bytes_is_accounted_but_never_capped():
    """DISK_BYTES 只记账、不设限 —— 上界由 disk_budget 按真实剩余空间给。

    但记账必须发生：并发的预检要能互相看见对方预留了多少。
    """
    sched = _scheduler()
    huge = 10 ** 15
    res = sched.reserve(('map', 1, 'download'), [_disk(huge)])
    assert res.disk_bytes == huge
    snap = sched.snapshot()
    assert snap['in_use'][ResourceKind.DISK_BYTES.value] == huge
    assert snap['limits'][ResourceKind.DISK_BYTES.value] is None
    assert snap['available'][ResourceKind.DISK_BYTES.value] is None


def test_a_refused_reservation_mutates_nothing():
    """拒绝必须是**原子**的：授予量先攒在局部，全部通过才落账。

    半批落账会让被拒的那次悄悄占掉网络名额，表现为「有名额却总起不来」。
    """
    sched = _scheduler(max_concurrent_tasks=1, max_network_connections=10)
    sched.reserve(('map', 1, 'download'), [_task_slot(), _network(4)])
    before = sched.snapshot()

    # 任务槽满 → 整批被拒，其中的 network 请求也不许留下痕迹。
    assert sched.reserve(('dem', 2, 'download'), [_task_slot(), _network(4)]) is None
    assert sched.snapshot() == before


def test_reserving_twice_for_one_owner_is_a_caller_bug():
    """同一 owner 只能有一张未归还的凭据；重复申请意味着上一张泄漏了。"""
    sched = _scheduler()
    sched.reserve(('map', 1, 'download'), [_network(2)])
    with pytest.raises(ValueError, match='already holds'):
        sched.reserve(('map', 1, 'download'), [_network(2)])


def test_unhashable_owner_is_rejected():
    sched = _scheduler()
    with pytest.raises(ValueError, match='hashable'):
        sched.reserve(['map', 1], [_network(2)])


# ---------------------------------------------------------------------------
# release_owner 的身份检查（复现过的线上级缺陷）
# ---------------------------------------------------------------------------

def test_release_owner_refuses_a_stale_reservation():
    """**回归**：迟到的归还绝不能摘掉同 owner 的新凭据。

    复现路径：线程 A 收尾（已 release）与用户立刻恢复（拿到 R2）之间有一个
    可长达数十秒的窗口，A 迟到的按键归还会摘掉 R2 —— 于是任务 2 在跑，
    调度器账上一格没占，全局上界失效。
    """
    sched = _scheduler(max_network_connections=64)
    owner = ('map', 1, 'download')
    first = sched.reserve(owner, [_network(8)])
    first.release()
    second = sched.reserve(owner, [_network(8)])

    assert sched.release_owner(owner, first) == 0          # 旧凭据无归还权
    assert second.released is False
    assert sched.snapshot()['in_use'][ResourceKind.NETWORK.value] == 8

    assert sched.release_owner(owner, second) == 1
    assert sched.snapshot()['in_use'][ResourceKind.NETWORK.value] == 0


def test_release_owner_without_a_reservation_object_is_refused():
    """没有凭据对象就没有归还权 —— 一个没申请到凭据的 except 分支在语法上
    就拿不出这个参数，也就写不出那个 bug。"""
    sched = _scheduler()
    owner = ('map', 1, 'download')
    live = sched.reserve(owner, [_network(8)])
    with pytest.raises(ValueError):
        sched.release_owner(owner, None)
    assert live.released is False
    assert sched.snapshot()['in_use'][ResourceKind.NETWORK.value] == 8


def test_double_release_is_a_no_op_not_a_double_refund():
    """`finally` 与异常补偿路径都会 release；第二次不能把配额再还一遍。

    重复退还会让 _in_use 变负，上限就此失效 —— 静默的产能过载。
    """
    sched = _scheduler(max_network_connections=64)
    owner = ('map', 1, 'download')
    res = sched.reserve(owner, [_network(8)])
    assert res.release() is True
    assert res.release() is False
    assert sched.release_owner(owner, res) == 0
    assert sched.snapshot()['in_use'][ResourceKind.NETWORK.value] == 0

    # 归还后名额真的回来了，不是「账面归零、实际被扣」。
    assert sched.reserve(owner, [_network(64)]).get(ResourceKind.NETWORK) == 64


# ---------------------------------------------------------------------------
# 配置降级
# ---------------------------------------------------------------------------

def test_dirty_config_values_fall_back_to_the_factory_defaults():
    """脏值不能等于「0 个名额」—— 那等价于服务停摆。"""
    sched = _scheduler(max_concurrent_tasks='not-a-number', max_gdal_slots='0')
    limits = sched.limits()
    assert limits[ResourceKind.TASK_SLOT] >= 1
    assert limits[ResourceKind.GDAL_SLOT] >= 1


def test_auto_cpu_workers_expands_to_a_real_number():
    """`max_cpu_workers=0` 是自动挡，snapshot 必须报用户真正拿得到的名额。"""
    sched = _scheduler(max_cpu_workers=0)
    assert sched.limits()[ResourceKind.CPU_WORKER] >= 1
    assert sched.snapshot()['limits'][ResourceKind.CPU_WORKER.value] >= 1


def test_limits_are_re_read_so_a_settings_change_takes_effect_immediately():
    """用户在设置页把并发从 64 改到 16，下一个任务就该按 16 走，不需要重启。"""
    config = FakeConfig(max_concurrent_tasks=2, max_network_connections=64,
                        max_cpu_workers=4, max_gdal_slots=2)
    sched = ResourceScheduler(config_manager=config)
    assert sched.limits()[ResourceKind.NETWORK] == 64
    config.values['max_network_connections'] = '16'
    assert sched.limits()[ResourceKind.NETWORK] == 16
