"""socket 实例必须是全局单例，且每个页面都拿得到。

本项目没有 JS 测试框架（无 package.json，且不打算引入 —— 会破坏离线打包形态），
所以这些断言守的是源码**形态**，与 tests/test_tasks_js_contract.py 同一路数。
"""
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_socket_js_exposes_a_lazy_singleton():
    """window.TerraSocket.get() 是唯一的创建点。"""
    src = _read("static", "js", "socket.js")
    assert "window.TerraSocket" in src
    assert re.search(r"\bio\s*\(", src), "socket.js 里应当有唯一的 io() 调用"


def test_tasks_js_no_longer_creates_its_own_socket():
    """tasks.js 必须复用单例，不能自己 io()。

    自己建一个的话首页会开出两个 WebSocket 连接：服务端 connected_clients 计数
    翻倍，每条广播被处理两遍，而现象只是「偶尔重复刷新」，极难归因。
    """
    src = _read("static", "js", "tasks.js")
    assert "window.TerraSocket.get()" in src, "tasks.js 没有复用全局单例"
    assert not re.search(r"socket\s*=\s*io\s*\(", src), (
        "tasks.js 仍在直接 io() —— 会开出第二个连接")


def test_socket_io_vendor_is_loaded_on_every_page():
    """socket.io 库不能再跟 Cesium 绑在同一个 block 里。

    /config 页刻意把 vendor block 覆盖成空以省掉 Cesium 的 5.7 MB，socket.io
    （44 KB）被顺带省掉了 —— 那样它就收不到底图解压进度，而「所有页面都要看得到」
    是既定要求。
    """
    base = _read("templates", "base.html")
    config = _read("templates", "config.html")

    assert "vendor_map_js" in base, "base.html 应当把 Cesium 单独放进 vendor_map_js"
    # socket.io 的 <script> 必须在 vendor_map_js 之外
    map_block = re.search(r"{%\s*block vendor_map_js\s*%}(.*?){%\s*endblock\s*%}",
                          base, re.S)
    assert map_block, "找不到 vendor_map_js block"
    assert "socket.io" not in map_block.group(1), (
        "socket.io 还在 vendor_map_js 里 —— /config 覆盖该 block 时会连它一起省掉")

    assert "vendor_map_js" in config, "config.html 应当只覆盖 vendor_map_js"
    assert "{% block vendor_js %}{% endblock %}" not in config, (
        "config.html 还在覆盖旧的 vendor_js block，socket.io 仍会被省掉")


def test_base_html_loads_socket_js_after_ui_js():
    """socket.js 要排在 ui.js 之后 —— 它调 initConnectionStatus（ui.js 定义的）。"""
    base = _read("templates", "base.html")
    ui = base.index("js/ui.js")
    sock = base.index("js/socket.js")
    assert ui < sock, "socket.js 必须排在 ui.js 之后，否则 initConnectionStatus 还没定义"
