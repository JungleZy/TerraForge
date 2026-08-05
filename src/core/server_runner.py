"""开发/单机模式下把服务跑起来(只有 __main__ 路径会用到)。

除了 socketio.run() 本身,这里还负责三件收尾工作:压掉与启动横幅重复的输出、
给 reloader 子进程装看门狗、打「组件加载完成」。
"""

import flask.cli
import logging
import os

from flask import Flask
from flask_socketio import SocketIO

from src.core.logging_setup import WerkzeugAccessLogFilter
from src.core.process_watchdog import PARENT_PID_ENV, start_parent_watchdog
from src.core.runtime_mode import SERVER_HOST, SERVER_PORT
from src.core.startup_banner import (WerkzeugStartupFilter, safe_print,
                                     use_color)


def _silence_duplicate_startup_lines():
    """压掉 werkzeug / flask 里与启动横幅重复的那几行。

    werkzeug 的启动行(Running on xxx / dev-server 警告 / debugger PIN)用过滤器
    精确拦掉;HTTP 访问日志(GET /... 200)保留,但剥掉和 asctime 重复的内嵌日期,
    并把日志名 werkzeug 换成 http。

    Flask 的 " * Serving Flask app / * Debug mode" 两行由 app.run ->
    flask.cli.show_server_banner 用 click.echo 直接写 stdout,日志级别拦不住,
    只能替换成 no-op(flask.app 是通过 cli.show_server_banner 模块属性调用的,
    所以 patch flask.cli 即可生效)。
    """

    werkzeug_logger = logging.getLogger('werkzeug')
    werkzeug_logger.addFilter(WerkzeugStartupFilter())
    werkzeug_logger.addFilter(WerkzeugAccessLogFilter())

    flask.cli.show_server_banner = lambda *args, **kwargs: None


def _install_reloader_watchdog():
    """reloader 父进程被强杀时,别把占着端口的孤儿子进程留下。

    werkzeug 不处理这种情况。父进程先把 pid 写进环境变量传给子进程;子进程起看门狗
    线程轮询,父进程没了就退出。
    """
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_parent_watchdog()
    else:
        os.environ.setdefault(PARENT_PID_ENV, str(os.getpid()))


def _print_ready():
    """组件加载完毕的反馈 —— werkzeug 的服务行已被压掉,没有这行的话用户不知道
    「正在加载组件」已经结束。"""
    ready = '  ✓ 组件加载完成,服务已启动'
    safe_print(f'\033[32m{ready}\033[0m' if use_color() else ready)


def run_server(app, socketio, *, debug, show_startup_output,
               host=SERVER_HOST, port=SERVER_PORT):
    """阻塞式启动服务。app 为 None 时(reloader 的 watcher 父进程)起占位 app。

    父进程并不真正服务 —— werkzeug run_simple 在父进程只把 application 包进一个
    从不调用的 lambda,然后 spawn 子进程(WERKZEUG_RUN_MAIN=true)重新执行入口模块,
    由子进程的完整 app 提供服务。所以这里只需一个轻量占位 app 驱动文件监听。
    """
    _silence_duplicate_startup_lines()
    _install_reloader_watchdog()

    if show_startup_output:
        _print_ready()

    if app is None:
        app = Flask(__name__)
        socketio = SocketIO(app)

    socketio.run(
        app,
        host=host,
        port=port,
        debug=debug,
        use_reloader=debug,
        allow_unsafe_werkzeug=True,
    )
