"""startup_banner 测试 —— 验证横幅内容、颜色退化与环境变量开关。"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.startup_banner import format_banner, print_banner, use_color


def test_plain_banner_contains_key_info():
    text = format_banner('0.1.0', port=5000, debug=True,
                         downloads_dir='/tmp/downloads',
                         database_path='/tmp/data/app.db',
                         color=False)
    assert 'TerraForge' in text
    assert 'v0.1.0' in text
    assert 'http://127.0.0.1:5000' in text
    assert '/tmp/downloads' in text
    assert '/tmp/data/app.db' in text
    assert 'DEBUG' in text


def test_plain_banner_has_no_ansi_escapes():
    text = format_banner('0.1.0', color=False)
    assert '\033[' not in text


def test_color_banner_has_ansi_escapes_and_resets():
    text = format_banner('0.1.0', color=True)
    assert '\033[' in text
    # 每个上色片段都必须闭合,避免污染后续终端输出
    assert text.count('\033[0m') >= 6


def test_production_mode_label_when_debug_off():
    text = format_banner('0.1.0', debug=False, color=False)
    assert 'PRODUCTION' in text
    assert 'DEBUG' not in text


def test_ascii_art_is_twelve_lines():
    text = format_banner('0.1.0', color=False)
    art_lines = [l for l in text.splitlines() if any(ch in l for ch in '█╗╝║╔╚═')]
    assert len(art_lines) == 12


def test_use_color_respects_no_color_env(monkeypatch):
    monkeypatch.setenv('NO_COLOR', '1')
    monkeypatch.setenv('FORCE_COLOR', '1')  # NO_COLOR 优先
    assert use_color(io.StringIO()) is False


def test_use_color_non_tty_defaults_false(monkeypatch):
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.delenv('FORCE_COLOR', raising=False)
    assert use_color(io.StringIO()) is False


def test_use_color_force_color_overrides_non_tty(monkeypatch):
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setenv('FORCE_COLOR', '1')
    assert use_color(io.StringIO()) is True


def test_print_banner_writes_to_stream(monkeypatch):
    monkeypatch.setenv('NO_COLOR', '1')
    buf = io.StringIO()
    print_banner('0.1.0', port=5000, stream=buf)
    out = buf.getvalue()
    assert 'TerraForge' in out
    assert '\033[' not in out


def _make_record(msg):
    import logging
    return logging.LogRecord('werkzeug', logging.INFO, __file__, 1, msg, (), None)


def test_werkzeug_filter_drops_startup_lines():
    from core.startup_banner import WerkzeugStartupFilter
    f = WerkzeugStartupFilter()
    for msg in (
        ' * Running on http://127.0.0.1:5000',
        ' * Running on all addresses (0.0.0.0)',
        'Press CTRL+C to quit',
        'WARNING: This is a development server. Do not use it in a production deployment.',
        'Werkzeug appears to be used in a production deployment.',
        ' * Debugger is active!',
        ' * Debugger PIN: 123-456-789',
    ):
        assert f.filter(_make_record(msg)) is False, msg


def test_werkzeug_filter_keeps_request_logs():
    from core.startup_banner import WerkzeugStartupFilter
    f = WerkzeugStartupFilter()
    assert f.filter(_make_record('127.0.0.1 - - [29/Jul/2026 12:00:00] "GET / HTTP/1.1" 200 -')) is True
    assert f.filter(_make_record('127.0.0.1 - - [29/Jul/2026 12:00:01] "POST /api/tasks HTTP/1.1" 201 -')) is True


class _FakeTty(io.StringIO):
    def isatty(self):
        return True


def test_spinner_non_tty_prints_static_hint_once(monkeypatch):
    from core.startup_banner import Spinner
    monkeypatch.delenv('NO_COLOR', raising=False)
    buf = io.StringIO()  # 非 TTY
    sp = Spinner('  正在加载组件,请稍候…', stream=buf).start()
    sp.stop()  # no-op,不应抛异常
    out = buf.getvalue()
    assert out == '  正在加载组件,请稍候…\n'
    assert '\r' not in out  # 非 TTY 不写动画转义


def test_spinner_tty_animates_and_clears_on_stop(monkeypatch):
    import time
    from core.startup_banner import Spinner
    monkeypatch.delenv('NO_COLOR', raising=False)
    buf = _FakeTty()
    sp = Spinner('加载中', stream=buf, interval=0.02).start()
    time.sleep(0.15)
    sp.stop()
    out = buf.getvalue()
    assert '\r' in out                  # 有回行刷帧
    assert '加载中' in out
    # 停帧后必须清行:\r + 空格 + \r 收尾
    assert out.endswith('\r')
    assert ' ' * 4 in out.split('\r')[-2] or out.split('\r')[-1] == ''


def test_spinner_tty_respects_no_color_env(monkeypatch):
    from core.startup_banner import Spinner
    monkeypatch.setenv('NO_COLOR', '1')
    buf = _FakeTty()  # 即使是 TTY,NO_COLOR 也退化为静态提示
    sp = Spinner('加载中', stream=buf).start()
    sp.stop()
    assert buf.getvalue() == '加载中\n'


def test_spinner_context_manager(monkeypatch):
    from core.startup_banner import Spinner
    monkeypatch.delenv('NO_COLOR', raising=False)
    buf = _FakeTty()
    with Spinner('加载中', stream=buf, interval=0.02):
        pass
    assert buf.getvalue().endswith('\r')  # 退出 with 时自动 stop 清行
