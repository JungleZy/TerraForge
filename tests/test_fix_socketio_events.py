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
    assert emitted == [("connected", {
        "message": "Connected to TerraForge",
        "client_id": "sid-abc",
    })]


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
