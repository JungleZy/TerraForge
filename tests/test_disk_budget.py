"""disk_budget —— 估算的形制与**全局感知**的准入判决。

两条被反复踩到的规则在这里立成回归：

1. **每瓦片字节数是「图源 × 层级」的函数，不是常量。** GeoD #30 的 17 倍偏差
   （23.91 GB 对 408.11 GB）就来自一个常数 20 KB —— 用低层级路网的经验值去乘
   高层级卫星影像的瓦片数。所以这里断言的是「随层级变、随图源变」这个形制，
   不是具体数字（数字是保守假设，会调）。
2. **复查必须排除调用方自己的预留。** 不排除的话，一个独占整台机器的任务需要
   2 倍于自身预算的空闲空间才跑得下去：物理 70 MiB 空闲、任务需要 40 MiB，
   准入通过并预留了自己那 40 MiB，跑到中途复查看到「可用 30 MiB、还缺 10 MiB」
   直接判死 —— 而 reason 还会把用户支去找一个并不存在的并发任务。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

MIB = 1024 * 1024


class FakeConfig:
    """`get_many` / `get` 两个方法的配置替身，不碰 sqlite。"""

    def __init__(self, **values):
        self.values = {str(k): str(v) for k, v in values.items()}

    def get_many(self, keys):
        return {k: self.values[k] for k in keys if k in self.values}

    def get(self, key, default=None):
        return self.values.get(key, default)


#: 判决算式里没有余量、没有安全系数 —— 让断言里的数字就是估算值本身。
STRICT_BUDGET = dict(disk_budget_enabled='true', disk_reserve_mb='0',
                     disk_safety_factor='1.0')

SCHEDULER_LIMITS = dict(max_concurrent_tasks=4, max_network_connections=64,
                        max_cpu_workers=4, max_gdal_slots=4)


@pytest.fixture
def scheduler():
    """进程单例换成一个喂了假配置的实例，用完丢弃。

    `_reserved_by_others` 走的是这个单例；不接管它就会让判决用例去读真实的
    配置库（甚至在纯 clone 上根本读不到）。
    """
    from src.services import resource_scheduler as rs

    rs.reset_scheduler()
    sched = rs.get_scheduler(config_manager=FakeConfig(**SCHEDULER_LIMITS))
    try:
        yield sched
    finally:
        rs.reset_scheduler()


def _reserve_disk(sched, owner, nbytes):
    from src.contracts.reservation import ResourceKind, ResourceRequest

    return sched.reserve(owner, [ResourceRequest(
        kind=ResourceKind.DISK_BYTES, requested=nbytes, minimum=nbytes)])


def _region():
    from src.contracts.region import RegionSpec

    return RegionSpec.from_bbox(north=39.9, south=39.1, east=116.9, west=116.1)


# ---------------------------------------------------------------------------
# 估算
# ---------------------------------------------------------------------------

def test_map_estimate_is_non_zero_on_every_axis():
    """「预估 0 字节」这种明显错误的判决会静默通过，比估错更糟。"""
    from src.services.disk_budget import estimate_map_task

    est = estimate_map_task(_region(), 8, 12, 'both', 's')
    assert est.tile_count > 0
    for name in ('network_bytes', 'cache_bytes', 'temp_bytes',
                 'output_bytes', 'peak_bytes'):
        assert getattr(est, name) > 0, f'{name} 估成了 0'


def test_peak_is_the_sum_of_things_that_really_coexist():
    """峰值 = 缓存 + 产物 + 工作区。三者确实同时在盘上（缓存不会在拼接前删）。"""
    from src.services.disk_budget import estimate_map_task

    est = estimate_map_task(_region(), 8, 11, 'both', 's')
    assert est.peak_bytes == est.cache_bytes + est.output_bytes + est.temp_bytes


def test_map_estimate_grows_with_the_zoom_range():
    """多下四层不能估出同一个数 —— 单调性是估算器还活着的最低证据。"""
    from src.services.disk_budget import estimate_map_task

    small = estimate_map_task(_region(), 8, 10, 'both', 's')
    large = estimate_map_task(_region(), 8, 14, 'both', 's')
    assert large.tile_count > small.tile_count
    assert large.peak_bytes > small.peak_bytes


def test_cached_tiles_reduce_the_network_estimate_but_not_the_output():
    """缓存命中不走网络，但照样要拷进输出目录 —— 产物一个字节都不少。"""
    from src.services.disk_budget import estimate_map_task

    cold = estimate_map_task(_region(), 8, 11, 'both', 's')
    warm = estimate_map_task(_region(), 8, 11, 'both', 's',
                             cached_tiles=cold.tile_count)
    assert warm.network_bytes == 0
    assert warm.output_bytes == cold.output_bytes


def test_export_mbtiles_adds_the_container_to_the_estimate():
    """**回归**：容器体积从前根本没被算进预算（那个分支是恒假的死代码）。

    MBTiles 与 output_format 正交，从 output_format 推不出来；不算进预算就等于
    把「盘写满」推迟到跑了几小时之后的最后一步。
    """
    from src.services.disk_budget import estimate_map_task

    plain = estimate_map_task(_region(), 8, 11, 'tiles_only', 's')
    with_container = estimate_map_task(_region(), 8, 11, 'tiles_only', 's',
                                       export_mbtiles=True)
    assert with_container.output_bytes > plain.output_bytes
    assert with_container.peak_bytes > plain.peak_bytes
    assert with_container.detail['container_bytes'] > 0
    assert plain.detail['container_bytes'] == 0


# ---------------------------------------------------------------------------
# avg_tile_bytes —— GeoD #30 的教训
# ---------------------------------------------------------------------------

def test_avg_tile_bytes_varies_with_zoom():
    """同一图源，高层级必须比低层级贵。常数化就是 #30 的根因。"""
    from src.services.disk_budget import avg_tile_bytes

    assert avg_tile_bytes('s', 3) < avg_tile_bytes('s', 12) < avg_tile_bytes('s', 20)


def test_avg_tile_bytes_varies_with_the_source_class():
    """同一层级，卫星影像必须比路网矢量渲染贵 —— 一个是照片，一个是有限调色板。"""
    from src.services.disk_budget import avg_tile_bytes

    assert avg_tile_bytes('m', 18) < avg_tile_bytes('t', 18) < avg_tile_bytes('s', 18)


@pytest.mark.parametrize('code, zoom', [
    ('', 0), ('s', -5), ('s', 99), ('zzz', 12), (None, 12), ('m', 'x'),
])
def test_avg_tile_bytes_is_always_positive(code, zoom):
    """未登记的图源 / 越界层级按最贵的一类算，绝不返回 0（估算不是校验）。"""
    from src.services.disk_budget import avg_tile_bytes

    assert avg_tile_bytes(code, zoom) > 0


def test_unknown_style_code_is_priced_as_the_most_expensive_class():
    """估少了会让任务在半路写满盘；估多了只是拦得早一点。方向必须是后者。"""
    from src.services.disk_budget import avg_tile_bytes

    assert avg_tile_bytes('custom-source', 18) == avg_tile_bytes('s', 18)


# ---------------------------------------------------------------------------
# 判决
# ---------------------------------------------------------------------------

def _estimate(peak):
    from src.services.disk_budget import DiskEstimate

    return DiskEstimate(network_bytes=peak, cache_bytes=peak, temp_bytes=0,
                        output_bytes=0, peak_bytes=peak, tile_count=1)


def test_check_budget_subtracts_what_other_tasks_reserved(monkeypatch, scheduler):
    """**这一行是本模块与「每个任务各查一次 disk_usage」的全部区别。**

    没有它，四个并发任务会各自看到同一份剩余空间、各自通过预检，然后一起把盘写满。
    """
    from src.services import disk_budget

    monkeypatch.setattr(disk_budget, 'free_bytes', lambda path: 100 * MIB)
    config = FakeConfig(**STRICT_BUDGET)

    before = disk_budget.check_budget('/anywhere', _estimate(60 * MIB), config)
    assert before.ok and before.free_bytes == 100 * MIB

    _reserve_disk(scheduler, ('dem', 9, 'download'), 50 * MIB)
    after = disk_budget.check_budget('/anywhere', _estimate(60 * MIB), config)
    assert after.free_bytes == 50 * MIB
    assert after.ok is False
    assert after.shortfall_bytes == 10 * MIB


def test_recheck_does_not_count_the_callers_own_reservation(monkeypatch, scheduler):
    """**回归**：不排除自己 = 单个任务需要 2 倍自身预算才跑得下去。

    实测形态：物理 70 MiB 空闲、任务需要 40 MiB，准入通过并预留了自己那份，
    跑到中途复查看到「可用 30 MiB、还缺 10 MiB」直接判死。
    """
    from src.services import disk_budget

    monkeypatch.setattr(disk_budget, 'free_bytes', lambda path: 70 * MIB)
    config = FakeConfig(**STRICT_BUDGET)
    owner = ('map', 1, 'download')
    remaining = _estimate(40 * MIB)

    assert disk_budget.check_budget('/anywhere', remaining, config).ok
    _reserve_disk(scheduler, owner, 40 * MIB)

    mine_excluded = disk_budget.recheck_remaining('/anywhere', remaining, config,
                                                  owner=owner)
    assert mine_excluded.free_bytes == 70 * MIB
    assert mine_excluded.ok is True

    # 不传 owner 就退化成「把自己也算进别人」—— 正是被修掉的那个判死。
    mine_counted = disk_budget.recheck_remaining('/anywhere', remaining, config)
    assert mine_counted.free_bytes == 30 * MIB
    assert mine_counted.ok is False


def test_recheck_still_sees_other_owners_while_excluding_its_own(monkeypatch, scheduler):
    """排除的只是**自己那一张**，别人的预留照扣 —— 否则全局性就没了。"""
    from src.services import disk_budget

    monkeypatch.setattr(disk_budget, 'free_bytes', lambda path: 100 * MIB)
    config = FakeConfig(**STRICT_BUDGET)
    owner = ('map', 1, 'download')
    _reserve_disk(scheduler, owner, 40 * MIB)
    _reserve_disk(scheduler, ('dem', 2, 'download'), 30 * MIB)

    verdict = disk_budget.recheck_remaining('/anywhere', _estimate(10 * MIB),
                                            config, owner=owner)
    assert verdict.free_bytes == 70 * MIB


def test_a_refusal_always_carries_the_numbers(monkeypatch, scheduler):
    """「空间不足」这四个字对用户毫无操作性 —— shortfall 必须为正且可直接照做。"""
    from src.services import disk_budget

    monkeypatch.setattr(disk_budget, 'free_bytes', lambda path: 10 * MIB)
    verdict = disk_budget.check_budget('/anywhere', _estimate(100 * MIB),
                                       FakeConfig(**STRICT_BUDGET))
    assert verdict.ok is False
    assert verdict.shortfall_bytes == 90 * MIB
    assert verdict.required_bytes == 100 * MIB


def test_disabling_the_check_still_reports_that_it_would_have_blocked(monkeypatch,
                                                                      scheduler):
    """关掉只跳过拦截，不该让用户在**不知情**的情况下把盘写满。"""
    from src.services import disk_budget

    monkeypatch.setattr(disk_budget, 'free_bytes', lambda path: 10 * MIB)
    config = FakeConfig(disk_budget_enabled='false', disk_reserve_mb='0',
                        disk_safety_factor='1.0')
    verdict = disk_budget.check_budget('/anywhere', _estimate(100 * MIB), config)
    assert verdict.ok is True
    assert verdict.shortfall_bytes == 90 * MIB     # 「本来会被拦下」照样出数


def test_safety_factor_and_reserve_both_enter_the_requirement(monkeypatch, scheduler):
    """required = peak × 系数；余量是永远不给任务用的系统地板，另算。"""
    from src.services import disk_budget

    monkeypatch.setattr(disk_budget, 'free_bytes', lambda path: 1000 * MIB)
    config = FakeConfig(disk_budget_enabled='true', disk_reserve_mb='100',
                        disk_safety_factor='2.0')
    verdict = disk_budget.check_budget('/anywhere', _estimate(100 * MIB), config)
    assert verdict.required_bytes == 200 * MIB
    assert verdict.reserve_bytes == 100 * MIB


def test_a_safety_factor_below_one_is_ignored(monkeypatch, scheduler):
    """< 1 的系数是把安全余量用成折扣，退回出厂默认。"""
    from src.services import disk_budget

    monkeypatch.setattr(disk_budget, 'free_bytes', lambda path: 1000 * MIB)
    config = FakeConfig(disk_budget_enabled='true', disk_reserve_mb='0',
                        disk_safety_factor='0.1')
    verdict = disk_budget.check_budget('/anywhere', _estimate(100 * MIB), config)
    assert verdict.required_bytes >= 100 * MIB


def test_free_bytes_never_raises_on_a_path_that_does_not_exist():
    """探测失败返回 0，不抛 —— 它挂在建任务这条同步路径上。"""
    from src.services.disk_budget import free_bytes

    assert free_bytes('/definitely/not/a/real/mount/point/xyz') >= 0
