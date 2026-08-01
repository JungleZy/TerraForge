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

from core.startup_banner import _enable_windows_ansi, use_color

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
    root.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())


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
