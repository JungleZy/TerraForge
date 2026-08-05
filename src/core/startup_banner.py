"""启动横幅 —— 让控制台启动画面更直观。

原先 app.py 的启动输出是一长串零散的 logger.info("xxx registered") 日志,
这里改成一张带渐变色彩的 ASCII 艺术横幅 + 关键信息(访问地址 / 数据目录 /
运行模式),细节日志降级为 DEBUG(设 LOG_LEVEL=DEBUG 仍能看到)。

无颜色环境(非 TTY、NO_COLOR=1)自动退化为纯文本,Windows 下会尝试开启
控制台 VT 模式以支持 ANSI 颜色。
"""

import logging
import os
import socket
import sys
import threading

# 配色(256 色):TERRA 用青->蓝(大地/海洋),FORGE 用橙->黄(炉火)
_GRADIENT = (51, 45, 39, 33, 27, 21, 208, 214, 220, 226, 227, 229)
_RESET = "\033[0m"
_BOLD = "1"
_DIM = "2"
_CYAN = "36"
_GREEN = "32"
_YELLOW = "33"

# ANSI Shadow 风格 "TERRA / FORGE"(pyfiglet ansi_shadow 生成)
_BANNER_LINES = (
    r"████████╗███████╗██████╗ ██████╗  █████╗ ",
    r"╚══██╔══╝██╔════╝██╔══██╗██╔══██╗██╔══██╗",
    r"   ██║   █████╗  ██████╔╝██████╔╝███████║",
    r"   ██║   ██╔══╝  ██╔══██╗██╔══██╗██╔══██║",
    r"   ██║   ███████╗██║  ██║██║  ██║██║  ██║",
    r"   ╚═╝   ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝",
    r"███████╗ ██████╗ ██████╗  ██████╗ ███████╗",
    r"██╔════╝██╔═══██╗██╔══██╗██╔════╝ ██╔════╝",
    r"█████╗  ██║   ██║██████╔╝██║  ███╗█████╗  ",
    r"██╔══╝  ██║   ██║██╔══██╗██║   ██║██╔══╝  ",
    r"██║     ╚██████╔╝██║  ██║╚██████╔╝███████╗",
    r"╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝",
)


def _enable_windows_ansi():
    """Windows 控制台默认不开 VT 序列,尝试启用;失败则静默退回纯文本。"""
    if os.name != 'nt':
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x4)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def use_color(stream=None):
    """是否输出 ANSI 颜色。NO_COLOR 优先,FORCE_COLOR 可强制开启。"""
    stream = stream if stream is not None else sys.stdout
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    return hasattr(stream, 'isatty') and stream.isatty()


def safe_write(stream, text):
    """写文本;控制台编码(如西欧 Windows 的 cp1252)装不下时降级为替换字符。

    横幅/spinner 含块字符、braille、中文,cp1252 直接 UnicodeEncodeError 把
    进程搞挂——这是打包 exe 在真实用户机器上的崩溃场景,绝不能让装饰性输出
    干掉主程序。
    """
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, 'encoding', None) or 'utf-8'
        stream.write(text.encode(encoding, errors='replace').decode(encoding, errors='replace'))


def safe_print(text, stream=None):
    """print() 的编码安全版本,见 safe_write。"""
    stream = stream if stream is not None else sys.stdout
    safe_write(stream, text + '\n')
    stream.flush()


def _lan_ip():
    """探测局域网 IP(UDP connect 不实际发包);离线环境返回 None。"""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(('10.255.255.255', 1))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        if sock is not None:
            sock.close()


def format_banner(version, host='0.0.0.0', port=5000, debug=False,
                  downloads_dir=None, color=True):
    """生成启动横幅文本(color=False 时不含 ANSI 转义,便于测试/日志)。"""
    c = lambda code, text: f"\033[{code}m{text}{_RESET}" if color else text

    lines = ['']
    for i, art in enumerate(_BANNER_LINES):
        if color:
            lines.append(f"\033[38;5;{_GRADIENT[i]}m{art}{_RESET}")
        else:
            lines.append(art)
    lines.append('')

    title = f"TerraForge  v{version}"
    lines.append(f"  {c(_BOLD, title)}")
    lines.append(f"  {c(_DIM, 'GIS 数据获取与加工:地图瓦片 · DEM · 3D 地形 · 等高线')}")
    lines.append('')

    divider = c(_DIM, '  ' + '─' * 46)
    lines.append(divider)
    local_url = f"http://127.0.0.1:{port}"
    url_style = _BOLD + ';36'  # 加粗 + 青色
    lines.append(f"  {c(_GREEN, '➜')} 本地访问    {c(url_style, local_url)}")
    lan = _lan_ip()
    if lan:
        lines.append(f"  {c(_GREEN, '➜')} 局域网访问  http://{lan}:{port}")
    lines.append(divider)

    if downloads_dir:
        lines.append(f"  下载目录   {c(_DIM, str(downloads_dir))}")
        lines.append(divider)

    if debug:
        mode = c(_YELLOW, 'DEBUG') + c(_DIM, '(热重载已开启)')
    else:
        mode = c(_GREEN, 'PRODUCTION')
    lines.append(f"  运行模式   {mode}")
    lines.append('')
    return '\n'.join(lines)


def print_banner(version, host='0.0.0.0', port=5000, debug=False,
                 downloads_dir=None, stream=None):
    """打印启动横幅。自动处理 Windows ANSI 兼容与无颜色环境退化。"""
    stream = stream if stream is not None else sys.stdout
    _enable_windows_ansi()
    safe_write(stream, format_banner(
        version, host=host, port=port, debug=debug,
        downloads_dir=downloads_dir,
        color=use_color(stream),
    ))
    safe_write(stream, '\n')
    stream.flush()


class Spinner:
    """终端加载动画(braille 转圈帧)。

    启动时的重量级 import 会阻塞主线程数秒,动画只能在后台线程里跑:每帧
    用 \\r 回到行首重写。非 TTY(重定向到文件/CI)退化为一次性静态提示,
    stop() 无操作,不会往日志里塞转义序列。可用作上下文管理器。
    """

    FRAMES = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

    def __init__(self, message, stream=None, interval=0.1):
        self.message = message
        self.stream = stream if stream is not None else sys.stdout
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread = None
        self._animated = (
            hasattr(self.stream, 'isatty')
            and self.stream.isatty()
            and not os.environ.get('NO_COLOR')
        )

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()

    def start(self):
        if not self._animated:
            safe_print(self.message, stream=self.stream)
            return self

        def _run():
            i = 0
            while not self._stop_event.is_set():
                frame = self.FRAMES[i % len(self.FRAMES)]
                safe_write(self.stream, f'\r\033[36m{frame}\033[0m {self.message}')
                self.stream.flush()
                i += 1
                self._stop_event.wait(self.interval)

        _enable_windows_ansi()
        self._thread = threading.Thread(
            target=_run, daemon=True, name='startup-spinner')
        self._thread.start()
        return self

    def stop(self):
        """停动画并清掉动画行(非 TTY 模式为 no-op)。"""
        if not self._animated:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        # CJK 字符是双宽,清行空格按两倍字符数给,保证不残留
        safe_write(self.stream, '\r' + ' ' * (len(self.message) * 2 + 4) + '\r')
        self.stream.flush()


def start_spinner(message, stream=None):
    """启动一个加载动画,返回 Spinner 句柄(用完调 stop())。"""
    return Spinner(message, stream=stream).start()


class WerkzeugStartupFilter(logging.Filter):
    """拦掉 werkzeug 的启动噪音,保留 HTTP 请求访问日志。

    werkzeug 的启动行(Running on xxx / dev-server 警告 / debugger PIN 等)和
    启动横幅里的访问地址重复,还会把横幅冲散;但把整个 werkzeug logger 压到
    ERROR 会连请求日志(GET /... 200)一起关掉。用过滤器精确丢弃这几条,
    挂在 werkzeug logger 上即可:
        logging.getLogger('werkzeug').addFilter(WerkzeugStartupFilter())
    """

    _DROP_SUBSTRINGS = (
        'Running on',                 # log_startup 的地址列表(横幅里已有)
        'Press CTRL+C to quit',
        'This is a development server',
        'production deployment',      # flask_socketio 的 allow_unsafe_werkzeug 警告
        'Debugger is active',
        'Debugger PIN',
    )

    def filter(self, record):
        msg = record.getMessage()
        return not any(s in msg for s in self._DROP_SUBSTRINGS)
