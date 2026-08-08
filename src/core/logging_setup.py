"""控制台日志配置 + 日志落盘 —— 按级别着色、清理 werkzeug 访问日志、按天轮转。

两个 sink,内容**故意不一样**:

- 控制台(stderr):给人看的,着色,瓦片请求的成功日志被丢掉(见 TileAccessLogFilter)。
- 文件(`<BASE_DIR>/logs/terraforge.log`):给排查用的,纯文本无色码,**瓦片日志全留**。
  文件不怕刷屏,而「那批瓦片到底请求过没有」恰恰是事后排查要看的第一件事。

解决的问题:
1. werkzeug 访问日志自带 `[01/Aug/2026 19:52:37]` 内嵌日期,和 Formatter 的
   asctime 重复 —— WerkzeugAccessLogFilter 把内嵌日期剥掉,只留一个时间。
2. 日志名 `werkzeug` 对用户没有意义 —— 访问日志统一改名为 `http`。
3. 浏览地图时的瓦片请求量级远大于其他请求,成功的那些会把有用的日志顶出屏幕 ——
   TileAccessLogFilter 挂在**控制台 handler** 上把它们丢掉,文件侧不受影响。
4. 按日志级别着色:DEBUG 暗青 / INFO 绿 / WARNING 黄 / ERROR 红 / CRITICAL 粗红。
   非 TTY(重定向到文件、CI)或 NO_COLOR=1 时自动退化为纯文本。
5. 关掉窗口日志就没了 —— 按天轮转落盘,保留 7 天。

时间戳用暗色弱化,让级别和信息本身更醒目。
"""

import logging
import logging.handlers
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

# werkzeug 3.x 会给非 200 的请求行套 ANSI 颜色(404 黄、4xx 红、5xx 品红,
# 见 werkzeug/serving.py 的 log_request),色码就夹在引号里面 ——
# `"\x1b[33mGET /tiles/1/2/3 HTTP/1.1\x1b[0m" 404 -`。两处要用它:
# TileAccessLogFilter 匹配前要剥,PlainFormatter 写文件前也要剥。
_ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')

# 落盘位置。BASE_DIR 在打包模式下是可执行文件所在目录(见 core/config.py),
# 所以 exe 用户的日志就在程序旁边的 logs/ 里,不会被写进只读的安装源目录。
LOG_DIR_NAME = 'logs'
LOG_FILE_NAME = 'terraforge.log'
# 按天轮转保留的份数。轮转后的文件名形如 terraforge.log.2026-08-07。
LOG_BACKUP_DAYS = 7


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


class PlainFormatter(ColoredFormatter):
    """落盘用 Formatter:格式与控制台完全一致,但剥掉一切 ANSI 色码。

    自己不上色是不够的 —— werkzeug 把色码塞在**消息内容里**(见 _ANSI_ESCAPE),
    直接写进文件就是一堆 `^[[33m`,grep 和肉眼都难受。
    """

    def __init__(self, datefmt=None):
        super().__init__(color=False, datefmt=datefmt)

    def format(self, record):
        return _ANSI_ESCAPE.sub('', super().format(record))


def configure_logging(log_to_file=False):
    """配置根日志:控制台 StreamHandler(+ 可选的按天轮转文件),级别取 LOG_LEVEL。

    与 basicConfig 一样是"先到先得":根 logger 已有 handler(reloader 父进程
    提前 basicConfig 过)时整体跳过,不会把父进程故意抬高的级别降回去。

    TileAccessLogFilter 挂在**控制台 handler** 上而不是 werkzeug logger 上 ——
    挂 logger 会在分发给 handler 之前就丢掉记录,文件也拿不到。控制台安静、
    文件完整,这个区别就是靠挂载位置实现的,别把它挪回 logger。

    log_to_file 由调用方按启动身份决定(见 app.py):只有**真正提供服务**的那个
    进程写文件。TimedRotatingFileHandler 轮转时要重命名文件,多进程同时持有
    必然打架(Windows 上直接 PermissionError,轮转失败、日志丢一整天),而
    multiprocessing 的 worker、reloader 的 watcher 父进程、WSGI 的多 worker
    都会把 app.py 重跑一遍 —— 它们一律只写控制台(stderr 本来就继承给了终端)。
    """
    root = logging.getLogger()
    if root.handlers:
        return
    _enable_windows_ansi()
    handler = logging.StreamHandler()
    handler.setFormatter(ColoredFormatter(color=use_color(handler.stream)))
    handler.addFilter(TileAccessLogFilter())
    root.addHandler(handler)
    root.setLevel(_parse_log_level(os.environ.get('LOG_LEVEL')))
    if log_to_file:
        _add_file_handler(root)


def log_dir():
    """日志目录 `<BASE_DIR>/logs`。

    Config 在这里**局部** import:模块级 import 会让 app.py 第 32 行的
    `from src.core.logging_setup import ...` 顺带把 config 拉起来,而 config 在
    import 期就可能 logger.warning(MAX_CONTENT_LENGTH 非法值)—— 那时 handler
    还没装,警告会掉进 logging 的 lastResort 里,无格式、无颜色。app.py 顶部
    「日志配置要赶在 import config 之前」那条注释说的就是这件事。
    """
    from src.core.config import Config
    return Config.BASE_DIR / LOG_DIR_NAME


def _add_file_handler(root):
    """给根 logger 加按天轮转的文件 handler;装不上就退回只有控制台。

    只读安装目录(Program Files、只读介质)是真实存在的部署方式,日志写不了
    是**次要功能失效**,不该让整个程序起不来 —— 但也不能静默,否则用户事后
    去 logs/ 找日志会发现空无一物且毫无解释。
    """
    path = log_dir() / LOG_FILE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.TimedRotatingFileHandler(
            str(path), when='midnight', backupCount=LOG_BACKUP_DAYS,
            # encoding 必填:不给的话 Windows 用 cp936 写中文日志,遇到
            # emoji / 生僻字直接 UnicodeEncodeError,而且是在 emit 里抛,
            # 表现成日志里突然多出一段 --- Logging error --- 回溯。
            encoding='utf-8',
            # 到第一条记录才建文件:import 本模块但从不打日志的进程
            # (测试、脚本)不该留下一个空文件。
            delay=True)
    except OSError as e:
        logging.getLogger(__name__).warning(
            "日志无法落盘(%s):%s。本次运行只有控制台输出。", path, e)
        return None
    handler.setFormatter(PlainFormatter())
    root.addHandler(handler)
    return handler


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


# 地图瓦片路径前缀。这四条都是「浏览地图」直接产生的高频请求:
#   /basemap  底图转发(routes/basemap_static.py)
#   /tiles    地图下载任务的瓦片(routes/tiles_static.py)
#   /terrain  地形瓦片与全球底座(routes/terrain_static.py)
#   /contour  等高线瓦片(routes/contour_static.py)
# 判定用前缀而不是路由名:过滤器只拿得到一行文本,拿不到 Flask 的路由对象。
_TILE_PATH_PREFIXES = ('/basemap/', '/tiles/', '/terrain/', '/contour/')

# 匹配访问日志里的 `"GET /path HTTP/1.1" 200`,取出路径与状态码。
# 非访问日志(werkzeug 也会打 "code 400, message Bad request" 之类)匹配不上,
# 一律放行 —— 这个过滤器只认自己确定认识的行。
_ACCESS_LINE = re.compile(r'"[A-Z]+ (\S+) [^"]*" (\d{3})')


class TileAccessLogFilter(logging.Filter):
    """丢掉瓦片请求中**成功**的那些访问日志,失败的照常打印。

    一次首屏加载或一次拖动就是几十上百条
    `127.0.0.1 "GET /basemap/3/4/4 HTTP/1.1" 200 -`,它们会把真正有用的日志
    (任务进度、错误、警告)顶出屏幕,而每一条本身都不含任何信息。

    **只丢 2xx/3xx**:瓦片的 403/404/504 恰恰是最需要看见的东西 —— 底图变蓝球、
    地形不显示,访问日志那一行常常是唯一的线索(basemap 路由自己会额外打一条
    WARNING,但 /tiles、/terrain、/contour 不会)。

    需要连成功的瓦片请求一起看时用 `LOG_LEVEL=DEBUG` 启动,本过滤器整体让路 ——
    不再单开一个环境变量,LOG_LEVEL 已经是「我要看全部」的那个开关。
    """

    def filter(self, record):
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            return True
        match = _ACCESS_LINE.search(_ANSI_ESCAPE.sub('', record.getMessage()))
        if not match:
            return True
        path, status = match.group(1), int(match.group(2))
        if not path.startswith(_TILE_PATH_PREFIXES):
            return True
        return not 200 <= status < 400
