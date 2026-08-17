"""启动画面 —— 开场动画 + 横幅 + 加载转圈。

原先 app.py 的启动输出是一长串零散的 logger.info("xxx registered") 日志,这里
改成一张居中的 ASCII 艺术横幅 + 关键信息(访问地址 / 数据目录 / 运行模式),
细节日志降级为 DEBUG(设 LOG_LEVEL=DEBUG 仍能看到)。

三样东西(开场动画、横幅、转圈)由 StartupConsole 串在同一个后台线程里播,与
主线程那几秒重量级 import 并行,所以动画基本不占启动时间。

无颜色环境(非 TTY、NO_COLOR=1)自动退化为纯文本一次性打印,Windows 下会尝试
开启控制台 VT 模式以支持 ANSI 颜色。
"""

import logging
import os
import shutil
import socket
import sys
import threading
import time
import unicodedata

_RESET = "\033[0m"
_BOLD = "1"
_DIM = "2"
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

# 配色:24 位真彩,终端不认就量化到 256 色立方(_fg)。
# 两族保留品牌语义(TERRA 大地/海洋、FORGE 炉火),但都统一成「上亮下深」,像从
# 上方打光 —— 原来 TERRA 由亮到深、FORGE 由深到亮,两块的光向互相打架。
# 饱和度也整体压下来了:满饱和的青蓝配满饱和的橙,吵。
_PALETTE = (
    (165, 243, 252), (103, 232, 249), (34, 211, 238),
    (6, 182, 212), (8, 145, 178), (14, 116, 144),
    (253, 230, 138), (252, 211, 77), (251, 191, 36),
    (245, 158, 11), (217, 119, 6), (180, 83, 9),
)
_ACCENT = (34, 211, 238)      # 信息区里唯一的高亮色(访问地址)
_VOID = (17, 24, 39)          # 上电前的近黑
_SLATE = (71, 85, 105)        # 成形前的冷灰:先立住轮廓,再谈颜色
_WHITE = (255, 255, 255)

# 三段式开场动画,总时长 _ANIM_DURATION 内按帧均分:
#   1 上电:整块从近黑浮起到冷灰,轮廓先立住
#   2 扫描成形:一道竖直光带斜着从左扫到右,扫过之处由冷灰转成本行颜色
#   3 定格呼吸:整块轻轻提亮一次再落回
# 成形和高光是同一件事(光带走过 = 这一列被点亮),不是两段特效叠着放。
_ANIM_DURATION = 2.2   # 整段动画时长(秒);与重量级 import 并行,通常不占启动时间
_ANIM_HOLD = 0.35      # 全部放完后定格这么久再让信息区上来,收尾才不赶
_ANIM_FASTFORWARD = 0.3         # 被叫停后放完剩余帧的时间上限 —— 唯一的净增等待
_ANIM_FASTFORWARD_FRAMES = 18   # 快进时抽样保留的帧数;配合上面约等于正常帧率
_BOOT_FRAMES = 12
_SWEEP_BAND = 12       # 光带从白热落回本色的宽度(列)
_SWEEP_LEAD = 3        # 光带前方的微光宽度:光要来了
_SWEEP_STEP = 1        # 光带每帧推进的列数
_SKEW = 2              # 光带每往下一行左移的列数 -> 斜切而不是竖直
_BREATH_FRAMES = 18
_BREATH_LIFT = 0.22    # 呼吸最高点朝白色提亮的比例
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"
_ANIM_OFF = ('0', 'false', 'no', 'off')

_TAGLINE = 'GIS 数据获取与加工 · 地图瓦片 · DEM · 3D 地形 · 等高线'
_BASE_INDENT = 2


def _display_width(text):
    """终端显示宽度:CJK 全角算两列。标签列要按它对齐,不能按 len()。"""
    return sum(2 if unicodedata.east_asian_width(ch) in 'WF' else 1
               for ch in text)


_ART_WIDTH = max(len(line) for line in _BANNER_LINES)
# 整块的宽度由最宽的一行内容决定;logo 比它窄,所以再往右挪半个差值居中。
# 「大气」靠的就是这两条:整块在终端里居中,logo 在整块里居中。
_BLOCK_WIDTH = max(_ART_WIDTH, _display_width(_TAGLINE))
_ART_OFFSET = (_BLOCK_WIDTH - _ART_WIDTH) // 2


def _enable_windows_ansi():
    """尝试开启 Windows 控制台 VT 序列;返回 ANSI 是否可用(非 Windows 恒 True)。

    开不起来时静态横幅只是吐一屏转义码,开场动画会吐上百屏 —— 所以动画必须
    看这个返回值,见 should_animate_banner()。
    """
    if os.name != 'nt':
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x4))
    except Exception:
        return False


def use_color(stream=None):
    """是否输出 ANSI 颜色。NO_COLOR 优先,FORCE_COLOR 可强制开启。"""
    stream = stream if stream is not None else sys.stdout
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    return hasattr(stream, 'isatty') and stream.isatty()


def use_truecolor():
    """终端认不认 24 位色。

    认就用 —— 256 色立方只有 6x6x6,一条白到青的十档色阶里有五组是重复的,渐变
    是「跳」的;真彩才谈得上平滑。不认就在 _fg 里量化回色立方。

    只认可靠的正信号:判错成 True 是一屏乱码,判错成 False 只是颜色粗一点。
    Windows Terminal 支持真彩但不设 COLORTERM,所以单列一条 WT_SESSION。
    """
    if os.environ.get('COLORTERM', '').strip().lower() in ('truecolor', '24bit'):
        return True
    if os.environ.get('WT_SESSION'):
        return True
    return 'direct' in os.environ.get('TERM', '')


def _fg(rgb, truecolor=True):
    """前景色转义;truecolor=False 时量化到 256 色立方。"""
    if truecolor:
        return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    r, g, b = (round(c / 255 * 5) for c in rgb)
    return f"\033[38;5;{16 + 36 * r + 6 * g + b}m"


def _mix(a, b, t):
    """两色之间取一档(t<=0 取 a,t>=1 取 b)。"""
    if t <= 0:
        return a
    if t >= 1:
        return b
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _ease(t):
    """smoothstep:慢起、缓收。线性推进显得机械,这是「优雅」最便宜的来源。"""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return t * t * (3 - 2 * t)


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


def layout_indent(stream=None):
    """整块横幅的左缩进:TTY 上按终端宽度居中,否则固定 _BASE_INDENT。

    居中是「大气」的主要来源 —— 顶在左边缘的一小块永远显得局促。非 TTY(日志
    / CI)固定缩进,输出才可预期。
    """
    stream = stream if stream is not None else sys.stdout
    if not (hasattr(stream, 'isatty') and stream.isatty()):
        return _BASE_INDENT
    columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    return max(_BASE_INDENT, (columns - _BLOCK_WIDTH) // 2)


def _art_block(color=True, indent=_BASE_INDENT, truecolor=True):
    """logo 那 12 行(color=False 时不含 ANSI 转义)。"""
    pad = ' ' * (indent + _ART_OFFSET)
    if not color:
        return [pad + line for line in _BANNER_LINES]
    return [f"{pad}{_fg(_PALETTE[i], truecolor)}{line}{_RESET}"
            for i, line in enumerate(_BANNER_LINES)]


def _info_block(version, port=5000, debug=False, downloads_dir=None,
                color=True, indent=_BASE_INDENT, truecolor=True):
    """logo 下面的信息区:标题 / 访问地址 / 下载目录 / 运行模式。

    一条细线 + 一列对齐的标签,而不是三条一模一样的分隔线夹着几行 —— 后者是
    「热闹」不是「大气」。标签按显示宽度对齐(_display_width),中英混排下
    len() 会错位。
    """
    pad = ' ' * indent
    c = lambda code, text: f"\033[{code}m{text}{_RESET}" if color else text

    def accent(text):
        if not color:
            return text
        return f"{_fg(_ACCENT, truecolor)}\033[{_BOLD}m{text}{_RESET}"

    rows = [('本地访问', accent(f"http://127.0.0.1:{port}"))]
    lan = _lan_ip()
    if lan:
        rows.append(('局域网访问', f"http://{lan}:{port}"))
    if downloads_dir:
        rows.append(('下载目录', c(_DIM, str(downloads_dir))))
    if debug:
        rows.append(('运行模式',
                     c(_YELLOW, 'DEBUG') + c(_DIM, ' · 热重载已开启')))
    else:
        rows.append(('运行模式', c(_GREEN, 'PRODUCTION')))

    label_width = max(_display_width(label) for label, _ in rows)
    lines = [
        f"{pad}{c(_BOLD, 'TerraForge')}  {c(_DIM, '· v' + version)}",
        f"{pad}{c(_DIM, _TAGLINE)}",
        '',
        f"{pad}{c(_DIM, '─' * _BLOCK_WIDTH)}",
        '',
    ]
    for label, value in rows:
        gap = ' ' * (label_width - _display_width(label) + 3)
        lines.append(f"{pad}{c(_DIM, label)}{gap}{value}")
    lines.append('')
    return lines


def format_banner(version, host='0.0.0.0', port=5000, debug=False,
                  downloads_dir=None, color=True, indent=_BASE_INDENT,
                  truecolor=None):
    """生成启动横幅文本(color=False 时不含 ANSI 转义,便于测试/日志)。"""
    truecolor = use_truecolor() if truecolor is None else truecolor
    return '\n'.join(
        [''] + _art_block(color, indent=indent, truecolor=truecolor) + [''] +
        _info_block(version, port=port, debug=debug,
                    downloads_dir=downloads_dir, color=color,
                    indent=indent, truecolor=truecolor))


def _paint(rows, truecolor=True, pad=''):
    """把一帧的 (字符, 颜色) 矩阵压成文本。

    相邻同色字符合并成一段转义 —— 不合并的话 12x42 每格一段,一帧就是 10KB、
    整段动画好几 MB,慢终端(串口/SSH)会被刷屏拖住。
    """
    parts = []
    for cells in rows:
        parts.append(pad)
        current = None
        for ch, rgb in cells:
            if rgb != current:
                parts.append(_fg(rgb, truecolor))
                current = rgb
            parts.append(ch)
        parts.append(_RESET)
        parts.append('\n')
    return ''.join(parts)


def _boot_frames():
    """第一段:整块从近黑浮起到冷灰。先立住轮廓,不做花活。"""
    for f in range(_BOOT_FRAMES):
        shade = _mix(_VOID, _SLATE, _ease(f / (_BOOT_FRAMES - 1)))
        yield [[(ch, shade) for ch in line] for line in _BANNER_LINES]


def _sweep_frames():
    """第二段:一道光带斜着从左扫到右,扫过之处由冷灰转成本行颜色。

    成形和高光合成同一件事 —— 光带走过 = 这一列被点亮。分成「先随机成形、再单独
    扫一道光」两段的话,两件事互相抢戏,看着就是乱。
    """
    span = _ART_WIDTH + _SKEW * (len(_BANNER_LINES) - 1) + _SWEEP_BAND
    for edge in range(-_SWEEP_LEAD, span + 1, _SWEEP_STEP):
        rows = []
        for r, line in enumerate(_BANNER_LINES):
            base = _PALETTE[r]
            head = edge - r * _SKEW
            cells = []
            for c, ch in enumerate(line):
                d = head - c
                if d < -_SWEEP_LEAD:
                    cells.append((ch, _SLATE))                  # 还没轮到
                elif d < 0:
                    # 带前微光:光要来了,不是一刀切上去
                    cells.append((ch, _mix(_SLATE, _WHITE,
                                           0.35 * (d + _SWEEP_LEAD) / _SWEEP_LEAD)))
                else:
                    cells.append((ch, _mix(_WHITE, base, _ease(d / _SWEEP_BAND))))
            rows.append(cells)
        yield rows


def _breath_frames():
    """第三段:整块轻轻提亮一次再落回。一次呼吸收住,不撒火星。"""
    for f in range(_BREATH_FRAMES):
        lift = _BREATH_LIFT * _ease(1 - abs(2 * f / (_BREATH_FRAMES - 1) - 1))
        yield [[(ch, _mix(_PALETTE[r], _WHITE, lift)) for ch in line]
               for r, line in enumerate(_BANNER_LINES)]


def _frames(truecolor=True, indent=_BASE_INDENT):
    """整段动画的每一帧(已压成文本)。纯计算不碰 IO,便于测试。

    每帧都恰好画满每行原有的列数 —— 不多不少,所以不需要 ESC[K 清行,收尾那帧
    也就能是逐字节的静态 art。
    """
    pad = ' ' * (indent + _ART_OFFSET)
    for phase in (_boot_frames(), _sweep_frames(), _breath_frames()):
        for rows in phase:
            yield _paint(rows, truecolor=truecolor, pad=pad)


def _wait(stop_event, seconds):
    """等 seconds 秒;有 stop_event 就等在它上面,叫停能立刻醒。"""
    if seconds <= 0:
        return
    if stop_event is None:
        time.sleep(seconds)
    else:
        stop_event.wait(seconds)


def _animate_art(stream, duration=None, stop_event=None, truecolor=True,
                 indent=_BASE_INDENT):
    """播放开场动画:上电 -> 扫描成形 -> 定格呼吸。

    每帧重画整块再用 ESC[<n>A 把光标移回块首,所以这 12 行期间必须独占 stdout。
    正常路径由 StartupConsole 保证:动画、横幅、转圈串在同一个后台线程里,与之
    并行的主线程只做 import,日志走 stderr。终端太窄会折行、把上移的行数算错,
    由 should_animate_banner() 事先挡掉。

    帧先全部算好再播:渲染耗时(整段几十毫秒)否则会摊在帧间隔上,帧率忽快忽慢。
    stop_event 一置位就把剩余帧抽样放完(上限 _ANIM_FASTFORWARD,帧率约等于正常),
    三段编排照样看得完整,追加的等待有硬上限。
    收尾(含异常/Ctrl-C)一定写一遍静态 art:动画只是抵达它的过程,画面必须收敛
    到与无动画时逐字节相同的横幅。
    """
    duration = _ANIM_DURATION if duration is None else duration
    frames = list(_frames(truecolor=truecolor, indent=indent))
    interval = duration / max(len(frames), 1)
    up = f"\033[{len(_BANNER_LINES)}A"
    started = time.monotonic()
    last = len(frames) - 1
    safe_write(stream, _HIDE_CURSOR)
    try:
        for i, frame in enumerate(frames):
            if stop_event is not None and stop_event.is_set():
                # 被叫停:剩下的帧抽样压进 _ANIM_FASTFORWARD 里放完,不硬切。
                # 硬切的话热启动(import 只要几百毫秒)永远只能看到开头那点,
                # 三段编排等于白做;抽样把追加的等待钉死在上限内。
                rest = frames[i::max(1, (len(frames) - i) // _ANIM_FASTFORWARD_FRAMES)]
                gap = _ANIM_FASTFORWARD / len(rest)
                for tail in rest:
                    safe_write(stream, tail)
                    safe_write(stream, up)
                    stream.flush()
                    time.sleep(gap)
                break
            # 按截止时间走,落后就丢帧:主线程那几秒 import 是 CPU 密集的,本线程
            # 抢不到 GIL 时固定 sleep 会把整段编排拖长一倍。每帧都是一张完整画面、
            # 不含增量状态,丢掉任意一帧都不影响后面。
            deadline = started + (i + 1) * interval
            if time.monotonic() > deadline and i != last:
                continue
            safe_write(stream, frame)
            safe_write(stream, up)
            stream.flush()
            _wait(stop_event, deadline - time.monotonic())
        else:
            # 没被中途叫停才走这里(for/else):定格留一拍再让信息区上来
            _wait(stop_event, _ANIM_HOLD)
    finally:
        # 先还光标再写定帧:定帧后面紧跟信息区,中间不能夹转义,否则整块横幅
        # 与静态路径就不是逐字节相同了(test_animated_banner_settles_to_static_banner)
        safe_write(stream, _SHOW_CURSOR)
        safe_write(stream, ''.join(
            line + '\n'
            for line in _art_block(True, indent=indent, truecolor=truecolor)))
        stream.flush()


def should_animate_banner(stream=None):
    """这个终端够不够格放开场动画。

    要求:允许颜色 + 真 TTY + ANSI 真的可用 + 宽度装得下不折行,且没被
    TERRAFORGE_BANNER_ANIM=0 关掉。任一条不满足都退化为静态横幅。
    """
    stream = stream if stream is not None else sys.stdout
    if os.environ.get('TERRAFORGE_BANNER_ANIM', '').strip().lower() in _ANIM_OFF:
        return False
    if not use_color(stream):
        return False
    if not (hasattr(stream, 'isatty') and stream.isatty()):
        return False
    if not _enable_windows_ansi():
        return False
    columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    return columns >= _BLOCK_WIDTH + 2 * _BASE_INDENT


def print_banner(version, host='0.0.0.0', port=5000, debug=False,
                 downloads_dir=None, stream=None, animate=None,
                 stop_event=None, indent=None):
    """打印启动横幅。自动处理 Windows ANSI 兼容与无颜色环境退化。

    animate=None 时自行判断能否放开场动画(见 should_animate_banner);放不了
    就一次性静态打印。stop_event 透传给动画,用于中途叫停(见 StartupConsole)。
    """
    stream = stream if stream is not None else sys.stdout
    _enable_windows_ansi()
    if animate is None:
        animate = should_animate_banner(stream)
    if indent is None:
        indent = layout_indent(stream)
    truecolor = use_truecolor()
    if animate:
        # 与静态路径拼出完全相同的字节:'\n' + art 各行 + 空行 + 信息区
        safe_write(stream, '\n')
        _animate_art(stream, stop_event=stop_event, truecolor=truecolor,
                     indent=indent)
        safe_write(stream, '\n'.join([''] + _info_block(
            version, port=port, debug=debug, downloads_dir=downloads_dir,
            color=True, indent=indent, truecolor=truecolor)))
    else:
        safe_write(stream, format_banner(
            version, host=host, port=port, debug=debug,
            downloads_dir=downloads_dir, color=use_color(stream),
            indent=indent, truecolor=truecolor,
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
    THREAD_NAME = 'startup-spinner'

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
        _enable_windows_ansi()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=self.THREAD_NAME)
        self._thread.start()
        return self

    def _run(self):
        """后台线程主体。子类可以覆盖,在转圈之前先放别的东西。"""
        self._spin()

    def _spin(self):
        """转圈到 _stop_event 置位为止;每帧 \\r 回行首重写。"""
        i = 0
        while not self._stop_event.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            safe_write(self.stream, f'\r\033[36m{frame}\033[0m {self.message}')
            self.stream.flush()
            i += 1
            self._stop_event.wait(self.interval)

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


class StartupConsole(Spinner):
    """整套启动画面:开场动画 -> 横幅信息 -> 加载转圈,全在同一个后台线程里。

    为什么串成一个线程:三样东西都往 stdout 写、都靠原地重绘(动画 ESC[nA、转圈
    \\r),两个线程各写各的会把光标位置算乱。

    为什么放后台:主线程紧接着要跑 app_factory 那几秒重量级 import。先播完再
    import 的话动画时长就是净增的启动耗时;并行就基本白送 —— import 先结束时
    stop() 把剩余帧快放完(上限 _ANIM_FASTFORWARD),日志走 stderr,不跟画面抢行。

    转圈那行跟着横幅一起缩进,否则横幅在屏幕中间、转圈贴在最左边,构图是散的。

    放不了动画的环境(非 TTY / NO_COLOR / 终端太窄 / Windows VT 开不起来)退化成
    当场同步打印静态横幅,不开线程。
    """

    THREAD_NAME = 'startup-console'

    def __init__(self, version, message, host='0.0.0.0', port=5000, debug=False,
                 downloads_dir=None, stream=None, interval=0.1):
        stream = stream if stream is not None else sys.stdout
        indent = layout_indent(stream)
        super().__init__(' ' * indent + message.strip(), stream=stream,
                         interval=interval)
        self._indent = indent
        self._banner = dict(version=version, host=host, port=port, debug=debug,
                            downloads_dir=downloads_dir, indent=indent)
        self._animate = self._animated and should_animate_banner(self.stream)

    def start(self):
        if not self._animate:
            # 画不了动画:横幅当场打完,再交给 Spinner 原来那套(转圈 / 一行静态提示)
            print_banner(stream=self.stream, animate=False, **self._banner)
        return super().start()

    def _run(self):
        if self._animate:
            print_banner(stream=self.stream, animate=True,
                         stop_event=self._stop_event, **self._banner)
        self._spin()


def start_startup_console(version, message, host='0.0.0.0', port=5000,
                          debug=False, downloads_dir=None, stream=None):
    """起一整套启动画面,返回句柄(重量级 import 干完调 stop())。"""
    return StartupConsole(version, message, host=host, port=port, debug=debug,
                          downloads_dir=downloads_dir, stream=stream).start()


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
