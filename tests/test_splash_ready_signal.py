# -*- coding: utf-8 -*-
"""splash 的就绪信号必须是「瓦片落地」，不是「首帧渲染」。

首帧 postRender 触发时底图瓦片还在网络上（服务端代理转发，秒级），
屏幕上是没有影像的黑色球体 —— skyBox 关着、globe 底色是黑的。
splash 在首帧淡出，等于把 1-2 秒黑屏原样露给用户，开屏动画白做。

正确信号是 scene.globe.tilesLoaded：当前视野的地形/影像瓦片全部落地。
tile 加载完成会自动 requestRender，postRender 持续有帧，轮询不会卡死；
网络全挂时 initSplash 里的 20s 兜底计时器照样放人。
"""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_css_contract import _js_function_body  # noqa: E402
from test_tasks_js_contract import _strip_js_comments  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _clean(name):
    with open(os.path.join(ROOT, 'static', 'js', name), encoding='utf-8') as f:
        return _strip_js_comments(f.read())


def test_splash_ready_is_gated_on_tiles_loaded():
    """postRender 监听里必须先过 tilesLoaded 闸门才许 splashReady()。"""
    body = _js_function_body(_clean('map.js'), 'initMap')

    guard = re.search(r'if\s*\(\s*!viewer\.scene\.globe\.tilesLoaded\s*\)\s*return', body)
    assert guard, (
        'postRender 监听里没有 tilesLoaded 闸门 —— splash 又在首帧（无影像黑球）淡出'
    )
    ready_at = body.find('splashReady()', guard.end())
    assert ready_at > guard.end(), (
        'splashReady() 必须排在 tilesLoaded 闸门之后，否则闸门白加'
    )


def test_splash_has_a_max_wait_cap():
    """tilesLoaded 之外必须有短上限兜底放人。

    tilesLoaded 要等当前视野瓦片**全部**经代理落地（逐级细化、串行往返），
    慢网络下会把用户关在开屏里好几秒 —— 比原来的黑屏更难忍。所以
    tilesLoaded 之外还要一个秒级上限：内容就绪立即放人，再慢也不关过上限。
    """
    body = _js_function_body(_clean('map.js'), 'initMap')
    m = re.search(r'setTimeout\(\s*splashReady\s*,\s*(\d+)\s*\)', body)
    assert m, 'initMap 里没有 splashReady 的短上限计时器 —— 慢网络会久关开屏'
    cap_ms = int(m.group(1))
    assert 1000 <= cap_ms <= 5000, (
        f'上限 {cap_ms}ms 不在 1-5s 区间：太短会露黑屏，太长失去开屏意义'
    )


def test_no_ungated_splash_ready_on_first_frame():
    """负向钉住旧写法：首帧 postRender 直接 splashReady() 的形态不许回潮。"""
    body = _js_function_body(_clean('map.js'), 'initMap')
    listener = re.search(
        r'postRender\.addEventListener\(\s*(\w+)\s*\)', body)
    assert listener, 'initMap 里的 postRender 就绪监听不见了 —— 本测试已失效'
    fn_body = _js_function_body(body, listener.group(1))
    # 监听器体内 splashReady() 之前必须出现 tilesLoaded
    ready_at = fn_body.find('splashReady()')
    assert ready_at != -1, '就绪监听里没有 splashReady() —— 本测试已失效'
    assert 'tilesLoaded' in fn_body[:ready_at], (
        'splashReady() 之前没有任何 tilesLoaded 判断 —— 首帧黑屏回潮'
    )
