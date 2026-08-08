"""瓦片访问日志过滤 —— TileAccessLogFilter。

真实症状:控制台被这种行刷屏,任务进度和错误全被顶出屏幕:

    2026-08-07 19:12:19,295 - http - INFO - 127.0.0.1 "GET /basemap/3/4/4 HTTP/1.1" 200 -

一次首屏或一次拖动地图就是几十上百条,每条本身零信息量。过滤器只丢**成功**的
瓦片请求,失败的必须留下 —— 底图蓝球、地形不显示的时候,那一行常常是唯一线索。

构造记录时刻意照抄 werkzeug 的真实调用形态(`'... "%s" %s %s'` + args,
非 200 的请求行还带 ANSI 色码),别用手工拼好的字符串:两处细节都真的会
把过滤器打成「看似生效、实则只对 200 生效」。
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.core.logging_setup import TileAccessLogFilter


def _access_record(method, path, status, styles=()):
    """复刻 werkzeug WSGIRequestHandler.log_request/log 的记录形态。

    - 消息是 `%s` 模板 + args,不是拼好的字符串(过滤器必须走 getMessage())。
    - 非 200 的请求行被 _ansi_style 套在**引号里面**
      (`"\\x1b[33mGET /x HTTP/1.1\\x1b[0m" 404 -`),这是 werkzeug 3.x 的行为。
    """
    request_line = f'{method} {path} HTTP/1.1'
    for code in styles:
        request_line = f'\x1b[{code}m{request_line}'
    if styles:
        request_line += '\x1b[0m'
    return logging.LogRecord(
        'werkzeug', logging.INFO, __file__, 1,
        '127.0.0.1 - - [07/Aug/2026 19:12:19] "%s" %s %s',
        (request_line, str(status), '-'), None)


@pytest.fixture(autouse=True)
def _root_at_info():
    """过滤器在根级别为 DEBUG 时整体让路,所以每个用例都要从确定的级别出发。"""
    root = logging.getLogger()
    original = root.level
    root.setLevel(logging.INFO)
    yield root
    root.setLevel(original)


@pytest.mark.parametrize('path', [
    '/basemap/3/4/4',
    '/tiles/12/3421/1739.png',
    '/terrain/base/5/12/9.terrain',
    '/terrain/dem/7/8/1/2.terrain',
    '/contour/3/10/512/300.png',
])
def test_successful_tile_requests_are_dropped(path):
    f = TileAccessLogFilter()
    assert f.filter(_access_record('GET', path, 200)) is False, path


def test_conditional_tile_hit_is_dropped_despite_ansi_styling():
    """304 被 werkzeug 染成青色 —— 色码夹在引号里,不剥就会漏过整类噪音。

    瓦片带 Cache-Control,304 在实际浏览里出现得很频繁。
    """
    f = TileAccessLogFilter()
    rec = _access_record('GET', '/basemap/3/4/4', 304, styles=(36,))
    assert '\x1b[36m' in rec.getMessage(), '用例前提坏了:记录里没有 ANSI 色码'
    assert f.filter(rec) is False


@pytest.mark.parametrize('status, styles', [
    (403, (1, 31)),   # 上游风控:底图变蓝球的头号原因
    (404, (33,)),     # 瓦片不存在
    (504, (1, 35)),   # 上游超时 / 代理不通
])
def test_failed_tile_requests_are_kept(status, styles):
    """失败的瓦片请求是**唯一**的线索来源,必须留下。

    /basemap 路由自己会额外打一条 WARNING,但 /tiles、/terrain、/contour 不会 ——
    把这些一起丢掉,地形不显示的时候控制台会干净得毫无线索。
    werkzeug 恰好只给非 200 加 ANSI 色码,所以「忘了剥色码」的实现会在这里
    全线失配 —— 只丢 200、把该留的也留下,看起来「能用」,实际漏掉一半噪音。
    """
    f = TileAccessLogFilter()
    assert f.filter(_access_record('GET', '/tiles/12/3421/1739.png',
                                   status, styles=styles)) is True


@pytest.mark.parametrize('path', ['/', '/api/tasks', '/config', '/static/js/map.js'])
def test_non_tile_requests_are_untouched(path):
    f = TileAccessLogFilter()
    assert f.filter(_access_record('GET', path, 200)) is True, path


def test_non_access_log_lines_pass_through():
    """werkzeug 也用同一个 logger 打非访问日志,过滤器只认自己看得懂的行。"""
    f = TileAccessLogFilter()
    for msg in ('code 400, message Bad request version',
                ' * Restarting with stat',
                'Error on request:'):
        rec = logging.LogRecord('werkzeug', logging.INFO, __file__, 1, msg, (), None)
        assert f.filter(rec) is True, msg


def test_debug_level_restores_tile_logs(_root_at_info):
    """LOG_LEVEL=DEBUG 是「我要看全部」的开关,不再另开一个环境变量。

    没有这条逃生口,想看瓦片流量就只能改代码。
    """
    f = TileAccessLogFilter()
    rec = _access_record('GET', '/basemap/3/4/4', 200)
    assert f.filter(rec) is False
    _root_at_info.setLevel(logging.DEBUG)
    assert f.filter(_access_record('GET', '/basemap/3/4/4', 200)) is True


def test_filter_is_installed_on_the_console_handler_not_the_logger():
    """端到端:过滤器挂在**控制台 handler** 上,不是 werkzeug logger 上。

    两种典型失败,断言各拦一条:
    - 只写了过滤器、忘了挂上去 —— 用户那边一条噪音都没少;
    - 挂到了 werkzeug logger 上 —— 控制台一样安静,但记录在分发给 handler 之前
      就没了,日志文件也跟着少掉瓦片记录(落盘的意义有一半在这)。
      文件侧的正面断言在 tests/test_log_file_sink.py。
    """
    import io

    from src.core.logging_setup import configure_logging
    from src.core.server_runner import _silence_duplicate_startup_lines

    import flask.cli
    root = logging.getLogger()
    werkzeug_logger = logging.getLogger('werkzeug')
    saved = (root.handlers[:], root.level, list(werkzeug_logger.filters),
             flask.cli.show_server_banner)
    root.handlers = []
    try:
        configure_logging(log_to_file=False)
        _silence_duplicate_startup_lines()

        assert not any(isinstance(x, TileAccessLogFilter)
                       for x in werkzeug_logger.filters), (
            'TileAccessLogFilter 挂回 werkzeug logger 了 —— 日志文件会跟着丢瓦片记录'
        )
        console = root.handlers[0]
        assert any(isinstance(x, TileAccessLogFilter) for x in console.filters), (
            'TileAccessLogFilter 没有挂到控制台 handler 上'
        )

        console.stream = io.StringIO()
        werkzeug_logger.handle(_access_record('GET', '/basemap/3/4/4', 200))
        werkzeug_logger.handle(_access_record('GET', '/basemap/3/4/4', 403,
                                              styles=(1, 31)))
        out = console.stream.getvalue()
    finally:
        root.handlers, root.level = saved[0], saved[1]
        werkzeug_logger.filters = saved[2]
        flask.cli.show_server_banner = saved[3]

    assert '" 200' not in out, '成功的瓦片请求还在往控制台打'
    assert '" 403' in out, '失败的瓦片请求被一起丢了 —— 那是唯一的排查线索'
    assert '[07/Aug/2026 19:12:19]' not in out, (
        '内嵌日期没被 WerkzeugAccessLogFilter 剥掉 —— 和 asctime 重复'
    )
