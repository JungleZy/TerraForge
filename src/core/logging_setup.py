"""控制台日志配置 —— 按级别着色、清理 werkzeug 访问日志。

解决三个问题:
1. werkzeug 访问日志自带 `[01/Aug/2026 19:52:37]` 内嵌日期,和 Formatter 的
   asctime 重复 —— WerkzeugAccessLogFilter 把内嵌日期剥掉,只留一个时间。
2. 日志名 `werkzeug` 对用户没有意义 —— 访问日志统一改名为 `http`。
3. 按日志级别着色:DEBUG 暗青 / INFO 绿 / WARNING 黄 / ERROR 红 / CRITICAL 粗红。
   非 TTY(重定向到文件、CI)或 NO_COLOR=1 时自动退化为纯文本。

时间戳用暗色弱化,让级别和信息本身更醒目。
"""

import logging
import os
import re

from src.core.startup_banner import _enable_windows_ansi, use_color

_RESET = "\033[0m"
_DIM = "\033[2m"

_LEVEL_COLORS = {
    logging.DEBUG: "\033[2;36m",   # 暗青
    logging.INFO: "\033[32m",      # 绿
    logging.WARNING: "\033[33m",   # 黄
    logging.ERROR: "\033[31m",     # 红
    logging.CRITICAL: "\033[1;31m",  # 粗红
}


class ColoredFormatter(logging.Formatter):
    """按级别着色的控制台 Formatter;color=False 时输出与默认格式一致。"""

    def __init__(self, color=True, datefmt=None):
        super().__init__(datefmt=datefmt)
        self.color = color

    def format(self, record):
        record.message = record.getMessage()
        asctime = self.formatTime(record, self.datefmt)
        levelname = record.levelname
        if self.color:
            color = _LEVEL_COLORS.get(record.levelno, '')
            asctime = f'{_DIM}{asctime}{_RESET}'
            levelname = f'{color}{levelname}{_RESET}'
        s = f'{asctime} - {record.name} - {levelname} - {record.message}'
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            s += '\n' + record.exc_text
        if record.stack_info:
            s += '\n' + self.formatStack(record.stack_info)
        return s


def configure_logging():
    """配置根日志:StreamHandler + ColoredFormatter,级别取 LOG_LEVEL(默认 INFO)。

    与 basicConfig 一样是"先到先得":根 logger 已有 handler(reloader 父进程
    提前 basicConfig 过)时整体跳过,不会把父进程故意抬高的级别降回去。
    """
    root = logging.getLogger()
    if root.handlers:
        return
    _enable_windows_ansi()
    handler = logging.StreamHandler()
    handler.setFormatter(ColoredFormatter(color=use_color(handler.stream)))
    root.addHandler(handler)
    root.setLevel(_parse_log_level(os.environ.get('LOG_LEVEL')))


def quiet_reloader_parent():
    """让 dev reloader 的 watcher 父进程闭嘴 —— 启动输出只由服务子进程负责。

    debug 模式下 werkzeug reloader 会先以「watcher 父进程」身份执行一遍入口模块,
    再 fork 子进程(WERKZEUG_RUN_MAIN=true)重跑。父进程只是文件监听器,但它的
    import 副作用和 run_simple 在父进程侧打的启动行(Serving Flask app / Running
    on / Restarting with stat)都会打一遍,子进程再打一遍,启动输出又乱又重复。

    必须赶在 import config/routes(会触发日志)之前调用。werkzeug 的 _log 首次使用
    时会把 werkzeug logger 显式设为 INFO(盖过根级别),所以要单独压它。之后的
    configure_logging() 对已配置过 handler 的父进程是 no-op,不会把级别降回去。
    """
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger('werkzeug').setLevel(logging.ERROR)


_VALID_LOG_LEVELS = ('CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG', 'NOTSET')


def _parse_log_level(raw, default='INFO'):
    """把 LOG_LEVEL 环境变量解析成合法级别名，非法值回退默认并给出提示。

    U11：`logging` 的 _checkLevel 对未知字符串抛 `ValueError: Unknown level`，
    而 configure_logging() 在 app.py 顶层无保护调用 —— 一个手滑的
    `LOG_LEVEL=verbose` 会让启动直接崩在裸 traceback 上，且报错文案不含变量名，
    用户完全不知道是哪个环境变量的问题。与 src/core/config.py 的
    _parse_max_content_length 已确立的「非法值 warning + 回退」口径对齐。
    """
    value = (raw or '').strip().upper()
    if not value:
        return default
    if value in _VALID_LOG_LEVELS:
        return value
    # handler 已装好（addHandler 在本函数调用点之前），warning 出得来。
    logging.getLogger(__name__).warning(
        "环境变量 LOG_LEVEL=%r 不是合法日志级别（可选：%s），已回退 %s",
        raw, '/'.join(_VALID_LOG_LEVELS), default)
    return default


class WerkzeugAccessLogFilter(logging.Filter):
    """整理 werkzeug 访问日志:剥掉内嵌日期、去掉尾部换行、改名为 http。

    werkzeug 的 WSGIRequestHandler.log 拼的消息形如
    `127.0.0.1 - - [01/Aug/2026 19:52:37] "GET / HTTP/1.1" 200 -\n`,
    其中 `[...]` 日期和 Formatter 的 asctime 重复,尾部 \\n 还会和
    StreamHandler 的换行叠加出空行。这里统一清理成
    `127.0.0.1 "GET / HTTP/1.1" 200 -`。
    """

    _EMBEDDED_TS = re.compile(r' - - \[[^\]]*\]')

    def filter(self, record):
        record.msg = self._EMBEDDED_TS.sub('', record.getMessage()).rstrip('\n')
        record.args = ()
        record.name = 'http'
        return True
