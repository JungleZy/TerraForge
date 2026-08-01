"""并发下载数「测速推荐」回归测试。

设计口径:
- 推荐依据是实测吞吐阶梯(默认 10/25/50 三档),不是拍脑袋公式 ——
  真实链路(代理、运营商、对端限速)只有实测才知道;
- _pick_concurrency 取「达到最高吞吐 90% 的最小并发」(膝点),顶格仍
  明显上升时标记 rising,提示用户可再手动调高;
- 探测全失败回退保守值 20(fallback=True),绝不让推荐流程报错出去;
- 每档测量有硬时间窗,窗口结束未完成的请求取消,只计完成的瓦片。
"""
import asyncio
import importlib
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.config import Config
from services.tile_url_probe import (
    RECOMMEND_FALLBACK,
    _measure_throughput,
    _pick_concurrency,
    recommend_concurrency,
)


def _s(concurrency, tps, ok=10):
    return {'concurrency': concurrency, 'ok': ok, 'attempted': max(ok, 1),
            'seconds': 8.0, 'tiles_per_sec': tps}


def _load_app(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


# --- _pick_concurrency 纯函数 -------------------------------------------------

def test_pick_latency_bound_recommends_top_and_rising():
    """高延迟链路:吞吐随并发近似线性增长 → 推荐顶格,标记仍在上升。"""
    picked = _pick_concurrency([_s(10, 2.0), _s(25, 5.0), _s(50, 10.0)])
    assert picked == (50, True)


def test_pick_saturated_recommends_knee():
    """25 档已达最高吞吐的 95% → 膝点是 25,不盲目顶格。"""
    picked = _pick_concurrency([_s(10, 5.0), _s(25, 9.5), _s(50, 10.0)])
    assert picked == (25, False)


def test_pick_flat_recommends_lowest():
    """三档吞吐基本持平(链路早饱和) → 推荐最小档,省连接。"""
    picked = _pick_concurrency([_s(10, 9.8), _s(25, 10.0), _s(50, 9.9)])
    assert picked == (10, False)


def test_pick_all_failed_returns_none():
    assert _pick_concurrency([_s(10, 0.0, ok=0), _s(25, 0.0, ok=0)]) is None


# --- recommend_concurrency 编排(measure 注入,无真实网络) --------------------

def _stub_measure(samples):
    async def measure(urls, concurrency, proxy_url, window_s, fetch=None):
        return next(s for s in samples if s['concurrency'] == concurrency)
    return measure


def test_recommend_returns_knee_with_samples():
    samples = [_s(10, 5.0), _s(25, 9.5), _s(50, 10.0)]
    result = recommend_concurrency(['mts0'], measure=_stub_measure(samples))
    assert result['recommended'] == 25
    assert result['fallback'] is False
    assert result['rising'] is False
    assert result['samples'] == samples
    assert result['note']


def test_recommend_rising_note_when_top_still_growing():
    samples = [_s(10, 2.0), _s(25, 5.0), _s(50, 10.0)]
    result = recommend_concurrency(['mts0'], measure=_stub_measure(samples))
    assert result['recommended'] == 50
    assert result['rising'] is True
    assert '高' in result['note'], "仍在上升时提示可再手动调高"


def test_recommend_fallback_when_all_levels_fail():
    samples = [_s(10, 0.0, ok=0), _s(25, 0.0, ok=0), _s(50, 0.0, ok=0)]
    result = recommend_concurrency(['mts0'], measure=_stub_measure(samples))
    assert result['recommended'] == RECOMMEND_FALLBACK
    assert result['fallback'] is True
    assert result['note']


def test_recommend_result_within_validation_range():
    """推荐值必须落在配置校验的 1-100 内,填进表单就能保存。"""
    samples = [_s(10, 2.0), _s(25, 5.0), _s(50, 10.0)]
    result = recommend_concurrency(['mts0'], measure=_stub_measure(samples))
    assert 1 <= result['recommended'] <= 100


# --- _measure_throughput 真实事件循环 + 假 fetch ------------------------------

def test_measure_counts_completed_within_window():
    async def fast_fetch(url, proxy_url, timeout_s):
        await asyncio.sleep(0.02)
        return b'x' * 100

    urls = ['http://t.example.com/%d.png' % i for i in range(200)]
    result = asyncio.run(
        _measure_throughput(urls, 10, '', 0.4, fetch=fast_fetch))
    # 10 条通道 × 0.4s / 0.02s ≈ 200 上限 200;宽松下限防 CI 抖动
    assert result['concurrency'] == 10
    assert result['ok'] >= 20
    assert result['tiles_per_sec'] > 0


def test_measure_deadline_cancels_pending_requests():
    async def hanging_fetch(url, proxy_url, timeout_s):
        await asyncio.sleep(30)
        return b''

    urls = ['http://t.example.com/%d.png' % i for i in range(50)]
    started = time.monotonic()
    result = asyncio.run(
        _measure_throughput(urls, 10, '', 0.2, fetch=hanging_fetch))
    elapsed = time.monotonic() - started
    assert result['ok'] == 0
    assert elapsed < 5, "窗口结束后挂起的请求必须被取消,不能等齐"


def test_measure_failed_requests_not_counted():
    async def flaky_fetch(url, proxy_url, timeout_s):
        raise OSError('boom')

    urls = ['http://t.example.com/%d.png' % i for i in range(50)]
    result = asyncio.run(
        _measure_throughput(urls, 5, '', 0.2, fetch=flaky_fetch))
    assert result['ok'] == 0
    assert result['attempted'] > 0


# --- API 端点与前端接线 --------------------------------------------------------

def test_recommend_endpoint_returns_recommendation(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)

    def fake_recommend(servers, **kwargs):
        assert servers, "端点必须把配置的 tile_servers 解析后传入"
        return {'recommended': 30, 'fallback': False, 'rising': False,
                'note': 'ok', 'samples': []}

    monkeypatch.setattr('services.tile_url_probe.recommend_concurrency', fake_recommend)
    resp = client.post('/api/config/recommend_concurrency')
    assert resp.status_code == 200
    assert resp.get_json()['recommended'] == 30


def test_recommend_endpoint_survives_probe_exception(monkeypatch, tmp_path):
    """推荐流程内部异常不能 500 白页 —— 回退值也要 200 返回。"""
    client = _load_app(monkeypatch, tmp_path)

    def boom(servers, **kwargs):
        raise RuntimeError('network gone')

    monkeypatch.setattr('services.tile_url_probe.recommend_concurrency', boom)
    resp = client.post('/api/config/recommend_concurrency')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['recommended'] == RECOMMEND_FALLBACK
    assert data['fallback'] is True


def test_config_page_has_recommend_button(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    for path in ('/', '/config'):
        html = client.get(path).get_data(as_text=True)
        assert 'id="concurrencyRecommend"' in html, f'{path} 缺少测速推荐按钮'
        assert 'id="concurrent_downloads"' in html


def test_config_js_recommend_wiring():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', 'config.js'), encoding='utf-8') as f:
        src = f.read()
    assert '/api/config/recommend_concurrency' in src
    assert 'concurrencyRecommend' in src
    # 推荐值只填进输入框,保存仍走既有 saveConfig 流程
    assert 'concurrent_downloads' in src
