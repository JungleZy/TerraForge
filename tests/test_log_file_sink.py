"""日志落盘 —— `<BASE_DIR>/logs/terraforge.log`,按天轮转保留 7 天。

以前日志只有一个 StreamHandler(stderr),关掉窗口就没了:用户报「地形不显示」
时拿不到任何历史记录,只能让他复现一遍。

本文件钉的核心契约是**两个 sink 内容故意不一样**:控制台丢掉成功的瓦片请求
(否则刷屏),文件全留(事后排查第一件事就是看那批瓦片到底请求过没有)。
这个区别完全靠「过滤器挂在 handler 上而不是 logger 上」实现 —— 挂错位置的
代码在控制台侧看起来完全正常,只有文件是空的,所以必须钉。
"""
import io
import logging
import logging.handlers
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.core.logging_setup import (LOG_BACKUP_DAYS, LOG_FILE_NAME,
                                    PlainFormatter, _add_file_handler,
                                    configure_logging, log_dir)


@pytest.fixture
def root_logger():
    """快照并复原根 logger。

    收尾要 close 掉新装的 handler:文件 handler 攥着句柄,不关的话 Windows 上
    后续用例删 tmp_path 会失败,Linux 上则是慢性 fd 泄漏。
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield root
    for h in root.handlers:
        if h not in saved_handlers:
            h.close()
    root.handlers, root.level = saved_handlers, saved_level


def _configure(root, log_to_file):
    """清空 handler 再配置。

    清空**必须在测试体里**做,不能放进 fixture:pytest 的 logging 插件会在每个
    测试阶段前后往根 logger 上装自己的捕获 handler,fixture 里清掉的会在进入
    测试体之前被重新装回来,而 configure_logging 是「根 logger 已有 handler
    就整体跳过」——那样它一行都不会执行,用例会以最难懂的方式全绿或全红。
    """
    root.handlers = []
    configure_logging(log_to_file=log_to_file)
    return root


@pytest.fixture
def base_dir(tmp_path, monkeypatch):
    """把 Config.BASE_DIR 指到 tmp_path —— 别在真仓库里生成 logs/。"""
    from src.core.config import Config
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    return tmp_path


def _file_handler(root):
    for h in root.handlers:
        if isinstance(h, logging.handlers.TimedRotatingFileHandler):
            return h
    return None


def _console_handler(root):
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(
                h, logging.handlers.TimedRotatingFileHandler):
            return h
    return None


def _werkzeug_access_record(path, status, styles=()):
    """复刻 werkzeug WSGIRequestHandler.log_request/log 的记录形态。

    消息是 `%s` 模板 + args(不是拼好的字符串),非 200 的请求行还被
    _ansi_style 套在引号**里面** —— 两处细节都真的会影响过滤与落盘结果。
    """
    request_line = f'GET {path} HTTP/1.1'
    for code in styles:
        request_line = f'\x1b[{code}m{request_line}'
    if styles:
        request_line += '\x1b[0m'
    return logging.LogRecord(
        'werkzeug', logging.INFO, __file__, 1,
        '127.0.0.1 - - [07/Aug/2026 19:12:19] "%s" %s %s',
        (request_line, str(status), '-'), None)


def test_log_file_lands_under_logs_dir(root_logger, base_dir):
    root = _configure(root_logger, log_to_file=True)
    handler = _file_handler(root)
    assert handler is not None, 'log_to_file=True 却没装文件 handler'
    assert log_dir() == base_dir / 'logs'
    assert handler.baseFilename == str(base_dir / 'logs' / LOG_FILE_NAME)


def test_rotation_is_daily_and_keeps_a_week(root_logger, base_dir):
    handler = _file_handler(_configure(root_logger, log_to_file=True))
    assert handler.when == 'MIDNIGHT'
    assert handler.backupCount == LOG_BACKUP_DAYS == 7
    # 中文日志 + Windows 默认 cp936 = emit 里抛 UnicodeEncodeError,
    # 表现成日志里突然插进一段 --- Logging error --- 回溯。
    assert (handler.encoding or '').lower().replace('-', '') == 'utf8'


def test_file_is_not_created_until_something_is_logged(root_logger, base_dir):
    """delay=True:只 import 不打日志的进程不该留下一个空文件。"""
    root = _configure(root_logger, log_to_file=True)
    path = base_dir / 'logs' / LOG_FILE_NAME
    assert path.parent.is_dir(), '目录要先建好,不能等到第一条日志才发现建不了'
    assert not path.exists()
    logging.getLogger('t').info('第一条')
    _file_handler(root).flush()
    assert path.exists()


def test_no_file_handler_when_disabled(root_logger, base_dir):
    """非服务进程(reloader 父、mp worker、WSGI worker)一律不碰文件。

    TimedRotatingFileHandler 轮转要重命名文件,多进程同时持有必然打架。
    """
    root = _configure(root_logger, log_to_file=False)
    assert _file_handler(root) is None
    assert not (base_dir / 'logs').exists()


def test_tile_success_is_dropped_from_console_but_kept_in_file(
        root_logger, base_dir):
    """本次改动的核心契约,端到端走一遍真实的过滤器链。

    过滤器一旦被挂回 werkzeug logger(而不是控制台 handler),记录会在分发给
    handler 之前就被丢掉 —— 控制台表现完全一样,只有文件少了东西。这条是
    唯一能拦住那种改法的断言。
    """
    import flask.cli

    from src.core.server_runner import _silence_duplicate_startup_lines

    werkzeug_logger = logging.getLogger('werkzeug')
    saved_filters = list(werkzeug_logger.filters)
    saved_banner = flask.cli.show_server_banner
    try:
        root = _configure(root_logger, log_to_file=True)
        _silence_duplicate_startup_lines()
        console = _console_handler(root)
        console.stream = io.StringIO()

        werkzeug_logger.handle(_werkzeug_access_record('/basemap/3/4/4', 200))
        werkzeug_logger.handle(_werkzeug_access_record('/api/tasks', 200))
        _file_handler(root).flush()

        console_out = console.stream.getvalue()
        file_out = (base_dir / 'logs' / LOG_FILE_NAME).read_text(encoding='utf-8')
    finally:
        werkzeug_logger.filters = saved_filters
        flask.cli.show_server_banner = saved_banner

    assert '/basemap/3/4/4' not in console_out, '瓦片噪音又回到控制台了'
    assert '/basemap/3/4/4' in file_out, (
        '文件里没有瓦片日志 —— 过滤器多半被挂回了 logger,'
        '控制台看起来一切正常,但落盘内容被一起砍掉了'
    )
    # 非瓦片请求两边都要有,排除「文件其实什么都没写」这种伪通过。
    assert '/api/tasks' in console_out and '/api/tasks' in file_out


def test_file_output_has_no_ansi_escapes(root_logger, base_dir):
    """werkzeug 把色码塞在消息内容里,写进文件就是一堆 ^[[33m,grep 都难受。"""
    root = _configure(root_logger, log_to_file=True)
    rec = _werkzeug_access_record('/tiles/7/12/34.png', 404, styles=(33,))
    assert '\x1b[' in rec.getMessage(), '用例前提坏了:记录里没有 ANSI 色码'
    logging.getLogger().handle(rec)
    _file_handler(root).flush()

    text = (base_dir / 'logs' / LOG_FILE_NAME).read_text(encoding='utf-8')
    assert '\x1b[' not in text
    assert '/tiles/7/12/34.png' in text and '404' in text


def test_chinese_log_lines_survive_the_round_trip(root_logger, base_dir):
    """项目日志基本都是中文,编码错了会在 emit 里抛,而不是安静地写坏。"""
    root = _configure(root_logger, log_to_file=True)
    logging.getLogger('t').warning('底图上游 3/4/4 返回 403（源：esri）')
    _file_handler(root).flush()
    text = (base_dir / 'logs' / LOG_FILE_NAME).read_text(encoding='utf-8')
    assert '底图上游 3/4/4 返回 403（源：esri）' in text


def test_unwritable_location_warns_but_keeps_the_app_running(
        root_logger, tmp_path, monkeypatch):
    """只读安装目录(Program Files、只读介质)是真实部署方式。

    日志写不了是次要功能失效,不该让程序起不来;但也不能静默 —— 否则用户
    事后去 logs/ 找日志会发现空无一物且毫无解释。
    这里把 BASE_DIR 指到一个**文件**上,mkdir 会抛 NotADirectoryError
    (OSError 的子类),比 chmod 更确定(以 root 跑测试时权限位是无效的)。
    """
    from src.core.config import Config
    blocker = tmp_path / 'blocker'
    blocker.write_text('not a directory')
    monkeypatch.setattr(Config, 'BASE_DIR', blocker)

    root = _configure(root_logger, log_to_file=True)

    assert _file_handler(root) is None
    console = _console_handler(root)
    assert console is not None, '控制台不能跟着一起没了'

    # 警告必须真的到达用户眼前,不只是「调用过 logger.warning」。第一次的警告
    # 已经写进真实 stderr 了,换掉 stream 再触发一次同一个分支来观察。
    console.stream = io.StringIO()
    assert _add_file_handler(root) is None
    assert '日志无法落盘' in console.stream.getvalue(), (
        '落盘失败必须有一条说明原因的警告'
    )


def test_plain_formatter_matches_console_format_minus_color():
    """两个 sink 的行格式必须一致,否则同一条日志在两处长得不一样,难以对照。"""
    from src.core.logging_setup import ColoredFormatter
    rec = logging.LogRecord('t', logging.INFO, __file__, 1, '\x1b[33m消息\x1b[0m',
                            (), None)
    plain = PlainFormatter().format(rec)
    colored_off = ColoredFormatter(color=False).format(rec)
    assert plain == colored_off.replace('\x1b[33m', '').replace('\x1b[0m', '')
    assert plain.endswith(' - t - INFO - 消息')
