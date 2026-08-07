"""SpeedMeter（下载吞吐计）单元测试。

时钟全程注入，不依赖 sleep —— 速率断言若靠真实时间就是不确定性测试。

覆盖：
  - 稳定速率下 bps() 的准确值
  - 变速后窗口滚出旧样本，速率跟着变
  - record(0)（缓存命中 / 失败 / 本地渲染）让速率如实回落，不冻在高值
  - 样本稀疏（间隔远大于窗口）时仍算得出，不退化成除零
  - 边界：未 record、同一时刻 record、非法 window
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.services.download_speed import SpeedMeter


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_no_record_yields_zero():
    """构造后未 record：只有一个初始样本，算不出速率，返回 0 而不是抛除零。"""
    assert SpeedMeter(clock=FakeClock()).bps() == 0.0


def test_steady_rate():
    """每 0.5s 收 1000 字节 -> 2000 B/s。"""
    clock = FakeClock()
    meter = SpeedMeter(window=3.0, clock=clock)
    for _ in range(4):
        clock.advance(0.5)
        meter.record(1000)
    assert meter.bps() == pytest.approx(2000.0)


def test_same_instant_records_do_not_divide_by_zero():
    """时钟没走就连续 record：跨度为 0，返回 0 而不是 ZeroDivisionError。"""
    meter = SpeedMeter(clock=FakeClock())
    meter.record(5000)
    meter.record(5000)
    assert meter.bps() == 0.0


def test_window_rolls_off_old_samples():
    """先快后慢：窗口滚过之后，速率必须反映**近期**而不是全程平均。

    前 2 秒 10000 B/s，后 4 秒 500 B/s。全程平均是 3666 B/s，
    3 秒窗口应当只看到接近 500 B/s 的那一段。
    """
    clock = FakeClock()
    meter = SpeedMeter(window=3.0, clock=clock)
    for _ in range(4):
        clock.advance(0.5)
        meter.record(5000)
    for _ in range(8):
        clock.advance(0.5)
        meter.record(250)

    assert meter.bps() == pytest.approx(500.0, rel=0.2)


def test_zero_byte_records_decay_the_rate():
    """下载停了但回调还在（缓存命中 / 失败）：速率必须回落，不能冻在高值。

    这是 record() 契约里最容易漏的一条 —— 只在有字节时才 record，
    界面会一直显示最后那个高速度。
    """
    clock = FakeClock()
    meter = SpeedMeter(window=3.0, clock=clock)
    for _ in range(4):
        clock.advance(0.5)
        meter.record(10000)
    fast = meter.bps()

    for _ in range(8):
        clock.advance(0.5)
        meter.record(0)

    assert fast == pytest.approx(20000.0)
    assert meter.bps() == 0.0


def test_sparse_samples_still_measurable():
    """样本间隔远大于窗口（慢速下载，10s 才落一个文件）。

    左端至少保留一个窗外样本，否则窗口内只剩一个样本、跨度为 0，
    速率会算不出来 —— 恰恰是最需要看到速度的场景。
    """
    clock = FakeClock()
    meter = SpeedMeter(window=3.0, clock=clock)
    clock.advance(10.0)
    meter.record(20000)
    clock.advance(10.0)
    meter.record(20000)

    assert meter.bps() == pytest.approx(2000.0)


def test_window_keeps_at_most_one_stale_sample():
    """驱逐不能无界堆积：长时间高频 record 后样本数保持有界。"""
    clock = FakeClock()
    meter = SpeedMeter(window=1.0, clock=clock)
    for _ in range(500):
        clock.advance(0.1)
        meter.record(100)

    # 1s 窗口 / 0.1s 步长 ≈ 10 个窗内样本，加左端一个窗外样本。
    # 只要不随 record 次数线性增长即可。
    assert len(meter._samples) <= 13


def test_negative_or_zero_window_rejected():
    for bad in (0, -1.0):
        with pytest.raises(ValueError):
            SpeedMeter(window=bad, clock=FakeClock())
