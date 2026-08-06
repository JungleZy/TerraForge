"""
Basic wiring tests for src/routes/socketio_events.py (review I21 — zero coverage).

Uses a fake socketio object to capture registered handlers, then invokes the
handlers inside a Flask request context with a fake request.sid; the module's
`emit` is monkey-patched so no real Socket.IO server is needed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.routes.socketio_events as events  # noqa: E402


class FakeSocketIO:
    """Stand-in for flask_socketio.SocketIO: records @socketio.on handlers."""

    def __init__(self):
        self.handlers = {}

    def on(self, event):
        def decorator(fn):
            self.handlers[event] = fn
            return fn
        return decorator


@pytest.fixture
def registered():
    events.connected_clients.clear()
    sio = FakeSocketIO()
    events.register_socketio_events(sio)
    yield sio
    events.connected_clients.clear()


@pytest.fixture
def flask_app():
    from flask import Flask
    return Flask("socketio_events_test")


def test_registers_connect_and_disconnect_handlers(registered):
    assert set(registered.handlers) == {"connect", "disconnect"}


def test_connect_tracks_client_and_emits_welcome(registered, flask_app, monkeypatch):
    emitted = []
    monkeypatch.setattr(events, "emit", lambda event, payload: emitted.append((event, payload)))

    with flask_app.test_request_context("/"):
        from flask import request
        request.sid = "sid-abc"
        registered.handlers["connect"]()

    assert "sid-abc" in events.connected_clients
    # connect 之后还会追加一条底图预热快照(见
    # test_connect_pushes_the_base_unpack_snapshot),所以这里不再对整个列表做全等,
    # 只钉住欢迎消息本身与它的位置 —— 欢迎消息必须是第一条,附加信息排在后面。
    assert emitted[0] == ("connected", {
        "message": "Connected to TerraForge",
        "client_id": "sid-abc",
    })
    # 数量也要钉:光断言 emitted[0] 的话,「connect 里又多塞了几条广播」这类回归
    # 一条都拦不住。两条 emit 在 emit 被 patch 之后都是无条件执行的,确定性断言。
    assert len(emitted) == 2, f"connect 发了多余的事件:{[n for n, _ in emitted]}"


def test_disconnect_removes_client(registered, flask_app, monkeypatch):
    monkeypatch.setattr(events, "emit", lambda *a, **kw: None)
    events.connected_clients.add("sid-abc")

    with flask_app.test_request_context("/"):
        from flask import request
        request.sid = "sid-abc"
        registered.handlers["disconnect"]()

    assert "sid-abc" not in events.connected_clients


def test_handlers_swallow_internal_errors(registered, flask_app, monkeypatch, caplog):
    """A failure inside a handler (e.g. emit raising) must be logged, not raised."""
    def boom(*a, **kw):
        raise RuntimeError("emit failed")

    monkeypatch.setattr(events, "emit", boom)
    with flask_app.test_request_context("/"):
        from flask import request
        request.sid = "sid-err"
        registered.handlers["connect"]()  # must not propagate

    assert "Error handling client connection" in caplog.text


def test_connect_pushes_the_base_unpack_snapshot(registered, flask_app, monkeypatch):
    """新客户端连上时必须收到一次底图状态快照。

    没有这一步，两个真实场景失效：用户在解压跑到一半才打开浏览器（要等下一个
    节流窗口）；以及用户在**解压失败几小时后**才打开浏览器 —— 终态事件早发完了，
    他永远看不到那条失败标记，而「失败要一直显示」是既定要求。
    """
    from src.services import base_terrain_warmup as w

    sent = []
    monkeypatch.setattr(events, "emit", lambda name, payload=None: sent.append((name, payload)))
    monkeypatch.setattr(w, "snapshot", lambda: {"phase": "running", "fraction": 0.4, "error": None})

    with flask_app.test_request_context():
        from flask import request
        request.sid = "sid-1"
        registered.handlers["connect"]()

    names = [n for n, _ in sent]
    assert w.EVENT_NAME in names, f"connect 没推底图快照，只发了 {names}"
    payload = dict(sent)[w.EVENT_NAME]
    assert payload["phase"] == "running" and payload["fraction"] == 0.4


def test_connect_snapshot_failure_does_not_break_the_connection(
        registered, flask_app, monkeypatch, caplog):
    """快照取不到时连接照常建立，且异常必须被**内层** try 就地吃掉。

    这是个附加信息，不是连接的前提条件。让它把 connect 打挂的话，一个底图相关的
    小毛病会变成「整个实时推送用不了」。

    ⚠️ 只断言 `'connected' in names` 是**假守卫**：欢迎消息在 snapshot() 爆炸之前
    就已经进 sent 了，而即使内层 try 不存在，异常也会被 handler 最外层那个
    `except Exception` 接住 —— handler 照样不抛，那条断言照样成立，于是「快照推送
    必须单独套一层 try」这条硬约束零覆盖。判据只能是**外层错误处理有没有被惊动**：
    落到外层就会打一条 ERROR，断言它不出现，删掉内层 try 这条用例才会红。
    （范式取自同文件的 test_handlers_swallow_internal_errors。）
    """
    from src.services import base_terrain_warmup as w

    sent = []
    monkeypatch.setattr(events, "emit", lambda name, payload=None: sent.append((name, payload)))

    def boom():
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(w, "snapshot", boom)

    with flask_app.test_request_context():
        from flask import request
        request.sid = "sid-2"
        registered.handlers["connect"]()

    assert "connected" in [n for n, _ in sent], "欢迎消息仍必须发出去"
    assert "Error handling client connection" not in caplog.text, (
        "快照失败落到了外层错误处理 —— 内层 try 没了，"
        "一个底图小毛病会被当成整个 connect 失败")
