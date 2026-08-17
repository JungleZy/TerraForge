"""startup_banner 测试 —— 验证横幅内容、颜色退化与环境变量开关。"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.core.startup_banner import format_banner, print_banner, use_color


def test_plain_banner_contains_key_info():
    text = format_banner('0.1.0', port=5000, debug=True,
                         downloads_dir='/tmp/downloads',
                         color=False)
    assert 'TerraForge' in text
    assert 'v0.1.0' in text
    assert 'http://127.0.0.1:5000' in text
    assert '/tmp/downloads' in text
    assert '数据库' not in text
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
    from src.core.startup_banner import WerkzeugStartupFilter
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
    from src.core.startup_banner import WerkzeugStartupFilter
    f = WerkzeugStartupFilter()
    assert f.filter(_make_record('127.0.0.1 - - [29/Jul/2026 12:00:00] "GET / HTTP/1.1" 200 -')) is True
    assert f.filter(_make_record('127.0.0.1 - - [29/Jul/2026 12:00:01] "POST /api/tasks HTTP/1.1" 201 -')) is True


class _FakeTty(io.StringIO):
    def isatty(self):
        return True


def test_spinner_non_tty_prints_static_hint_once(monkeypatch):
    from src.core.startup_banner import Spinner
    monkeypatch.delenv('NO_COLOR', raising=False)
    buf = io.StringIO()  # 非 TTY
    sp = Spinner('  正在加载组件,请稍候…', stream=buf).start()
    sp.stop()  # no-op,不应抛异常
    out = buf.getvalue()
    assert out == '  正在加载组件,请稍候…\n'
    assert '\r' not in out  # 非 TTY 不写动画转义


def test_spinner_tty_animates_and_clears_on_stop(monkeypatch):
    import time
    from src.core.startup_banner import Spinner
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
    from src.core.startup_banner import Spinner
    monkeypatch.setenv('NO_COLOR', '1')
    buf = _FakeTty()  # 即使是 TTY,NO_COLOR 也退化为静态提示
    sp = Spinner('加载中', stream=buf).start()
    sp.stop()
    assert buf.getvalue() == '加载中\n'


def test_spinner_context_manager(monkeypatch):
    from src.core.startup_banner import Spinner
    monkeypatch.delenv('NO_COLOR', raising=False)
    buf = _FakeTty()
    with Spinner('加载中', stream=buf, interval=0.02):
        pass
    assert buf.getvalue().endswith('\r')  # 退出 with 时自动 stop 清行


def _visible(text):
    """剥掉 ANSI 转义,只留肉眼能看到的字符。"""
    import re
    return re.sub(r'\033\[[0-9;?]*[A-Za-z]', '', text)


def _frame_lines(frame):
    """一帧文本 -> 12 行肉眼可见字符(含左侧缩进)。"""
    return _visible(frame).split('\n')[:-1]


def test_every_frame_covers_exactly_the_art_footprint():
    from src.core import startup_banner as sb
    # 每帧都恰好画满每行原有列数(加同一段缩进):多了会溢出、少了上一帧的残留
    # 擦不掉,而收尾帧写的是不带 ESC[K 的静态 art,清不掉任何残留。
    pad = 2 + sb._ART_OFFSET
    widths = [pad + len(line) for line in sb._BANNER_LINES]
    for n, frame in enumerate(sb._frames(indent=2)):
        assert [len(l) for l in _frame_lines(frame)] == widths, f'frame {n}'


def test_only_light_moves_never_the_glyphs():
    from src.core import startup_banner as sb
    # 三段自始至终只有颜色在变,字形一帧都不许动 —— 这正是这套编排与旧的
    # 「噪点溶解」的根本区别:先立住轮廓,再谈光。
    art = list(sb._BANNER_LINES)
    for rows in (list(sb._boot_frames()) + list(sb._sweep_frames())
                 + list(sb._breath_frames())):
        assert [''.join(ch for ch, _ in row) for row in rows] == art


def test_boot_rises_from_near_black_to_slate():
    from src.core import startup_banner as sb
    frames = list(sb._boot_frames())
    assert {c for row in frames[0] for _, c in row} == {sb._VOID}
    assert {c for row in frames[-1] for _, c in row} == {sb._SLATE}


def test_sweep_starts_cold_and_ends_on_the_palette():
    from src.core import startup_banner as sb
    frames = list(sb._sweep_frames())
    assert {c for row in frames[0] for _, c in row} == {sb._SLATE}
    # 扫完整块就是本行颜色,不留任何高光残余
    assert all(c == sb._PALETTE[r]
               for r, row in enumerate(frames[-1]) for _, c in row)


def test_sweep_band_is_slanted():
    from src.core import startup_banner as sb
    frames = list(sb._sweep_frames())
    # 挑一帧:光带头部在每行都还落在字块内(edge 30 时最下面一行在第 8 列)
    mid = frames[30 + sb._SWEEP_LEAD]
    heads = [max(range(len(row)), key=lambda i: sum(row[i][1])) for row in mid]
    assert heads == [heads[0] - r * sb._SKEW for r in range(len(heads))]


def test_breath_lifts_once_and_returns_to_the_palette():
    from src.core import startup_banner as sb
    frames = list(sb._breath_frames())
    for edge in (frames[0], frames[-1]):
        assert all(c == sb._PALETTE[r]
                   for r, row in enumerate(edge) for _, c in row)
    peak = frames[len(frames) // 2]
    for r, row in enumerate(peak):
        lifted = row[0][1]
        assert lifted != sb._PALETTE[r]           # 中途确实提亮了
        assert sum(lifted) < sum(sb._WHITE)       # 但不到白:是呼吸,不是闪光


def test_paint_merges_runs_of_the_same_color():
    from src.core import startup_banner as sb
    rows = list(sb._breath_frames())[-1]   # 整块已是本色,每行只该有一段转义
    text = sb._paint(rows, truecolor=True)
    assert text.count('\033[38;2;') == len(sb._BANNER_LINES)
    assert _visible(text).split('\n')[0] == sb._BANNER_LINES[0]


def test_paint_falls_back_to_the_256_cube_without_truecolor():
    from src.core import startup_banner as sb
    text = sb._paint(list(sb._breath_frames())[-1], truecolor=False)
    assert '\033[38;2;' not in text
    assert '\033[38;5;' in text


def test_layout_centres_the_block_on_a_tty(monkeypatch):
    from src.core import startup_banner as sb
    buf = _wide_tty(monkeypatch, columns=120)
    assert sb.layout_indent(buf) == (120 - sb._BLOCK_WIDTH) // 2
    # 非 TTY(日志/CI)固定缩进,输出才可预期
    assert sb.layout_indent(io.StringIO()) == sb._BASE_INDENT


def test_info_labels_align_by_display_width():
    from src.core import startup_banner as sb
    lines = sb._info_block('0.1.0', port=5000, downloads_dir='/tmp/downloads',
                           color=False, indent=0)
    starts = set()
    for line in lines:
        for label in ('本地访问', '局域网访问', '下载目录', '运行模式'):
            if line.startswith(label):
                rest = line[len(label):]
                starts.add(sb._display_width(label)
                           + len(rest) - len(rest.lstrip()))
    # 中英混排下按 len() 对齐必然错位,必须按显示宽度
    assert len(starts) == 1, starts


def test_info_block_has_a_single_hairline_rule():
    from src.core import startup_banner as sb
    lines = sb._info_block('0.1.0', color=False, indent=0)
    rules = [l for l in lines if l and set(l) == {'─'}]
    assert len(rules) == 1                                  # 一条,不是三条
    assert sb._display_width(rules[0]) == sb._BLOCK_WIDTH   # 与整块同宽才对得齐


def test_animated_banner_settles_to_static_banner(monkeypatch):
    from src.core import startup_banner as sb
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(sb, '_ANIM_DURATION', 0.01)
    buf = _FakeTty()
    sb.print_banner('0.1.0', port=5000, stream=buf, animate=True)
    # 动画只是抵达静态横幅的过程,最终画面必须与不加动画逐字节一致
    static = sb.format_banner('0.1.0', port=5000, color=True,
                              indent=sb.layout_indent(buf))
    assert buf.getvalue().endswith(static[1:] + '\n')


def test_animated_banner_redraws_in_place_and_restores_cursor(monkeypatch):
    from src.core import startup_banner as sb
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(sb, '_ANIM_DURATION', 0.01)
    buf = _FakeTty()
    sb.print_banner('0.1.0', stream=buf, animate=True)
    out = buf.getvalue()
    assert out.count(f'\033[{len(sb._BANNER_LINES)}A') >= 10  # 每帧回块首重画
    assert out.count('\033[?25l') == 1                        # 藏光标
    assert out.count('\033[?25h') == 1                        # 且必须还回来
    assert out.index('\033[?25l') < out.index('\033[?25h')


def test_static_banner_path_writes_no_animation_escapes(monkeypatch):
    from src.core import startup_banner as sb
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setenv('FORCE_COLOR', '1')
    buf = io.StringIO()  # 有颜色但非 TTY:走静态路径
    sb.print_banner('0.1.0', port=5000, stream=buf)
    out = buf.getvalue()
    assert out == sb.format_banner('0.1.0', port=5000, color=True) + '\n'
    assert '\033[?25l' not in out
    assert f'\033[{len(sb._BANNER_LINES)}A' not in out  # 没有光标上移重画


def _wait_until(predicate, timeout=3.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_startup_console_plays_banner_then_spins_on_one_thread(monkeypatch):
    from src.core import startup_banner as sb
    buf = _wide_tty(monkeypatch)
    monkeypatch.setattr(sb, '_ANIM_DURATION', 0.05)
    show = sb.StartupConsole('0.1.0', '加载中', port=5000, stream=buf,
                             interval=0.01).start()
    assert _wait_until(lambda: any(f in buf.getvalue() for f in sb.Spinner.FRAMES))
    show.stop()
    out = buf.getvalue()
    # 顺序:动画帧 -> 横幅信息 -> 转圈,三样共用一个线程,不会交错
    assert out.index(f'\033[{len(sb._BANNER_LINES)}A') < out.index('本地访问')
    assert out.index('本地访问') < min(out.index(f) for f in sb.Spinner.FRAMES
                                       if f in out)
    assert out.endswith('\r')   # 转圈行已清掉


def test_startup_console_stop_fast_forwards_instead_of_hard_cutting(monkeypatch):
    from src.core import startup_banner as sb
    buf = _wide_tty(monkeypatch)
    monkeypatch.setattr(sb, '_ANIM_DURATION', 30)   # 慢到不可能自己放完
    up = f'\033[{len(sb._BANNER_LINES)}A'
    show = sb.StartupConsole('0.1.0', '加载中', port=5000, stream=buf,
                             interval=0.01).start()
    assert _wait_until(lambda: buf.getvalue().count(up) >= 2)
    drawn = buf.getvalue().count(up)
    show.stop()
    out = buf.getvalue()
    # stop() 必须等到那条线程真的收工才返回。这里刻意**不**拿墙钟量它:
    # Spinner.stop() 是 set + join(timeout=1),耗时被那 1s 硬顶住,于是任何
    # 「elapsed < 1s 以上的数」都不可能失败 —— 是条永真断言。原判据
    # `elapsed < 0.5` 是唯一还 falsify 得了的写法,而它在 macOS CI 上实测 1.109s
    # 变红(run 31997345537)。它红得对:那正是 join 超时、stop() 带着还在写帧的
    # 线程回来了,当时屏上只落了 6 帧、连收尾的定帧都还没写。判据因此换成线程
    # 状态本身 —— 确定,且与机器快慢无关。
    assert not show._thread.is_alive(), (
        'stop() 返回时动画线程还活着 —— join(timeout=1) 超时了,剩下的帧与收尾'
        '定帧会插到随后的启动日志中间。快进的总时长必须留在 _ANIM_FASTFORWARD '
        '预算内(它按绝对进度表收口,不是每帧 sleep 累加)')
    # 不是硬切:剩下的帧抽样快放完,三段编排看得完整
    assert out.count(up) >= drawn + sb._ANIM_FASTFORWARD_FRAMES
    # 但也确实是**抽样**,不是把剩余帧一股脑倒完 —— 倒完等于没有编排,屏上只是
    # 一闪。抽样档位 _ANIM_FASTFORWARD_FRAMES 取 ceil 后最多翻一倍(步长是整除),
    # 留 +2 给端点,再钉住这条上界确实远低于全量帧数,否则这个判据本身就没意义。
    total = len(list(sb._frames(truecolor=sb.use_truecolor(),
                                indent=sb.layout_indent(buf))))
    sampled_cap = drawn + 2 * sb._ANIM_FASTFORWARD_FRAMES + 2
    assert sampled_cap < total, (
        f'本判据已失效:抽样上界 {sampled_cap} 不再低于全量 {total} 帧')
    assert out.count(up) <= sampled_cap, (
        f'快进放了 {out.count(up) - drawn} 帧,抽样档位是 '
        f'{sb._ANIM_FASTFORWARD_FRAMES} 帧(全量 {total})—— 剩余帧被一股脑倒完了')
    # 横幅照样完整落地:定帧 + 信息区一个都不能少
    assert out.count('\033[?25h') == 1
    for line in sb._art_block(True, indent=sb.layout_indent(buf),
                              truecolor=sb.use_truecolor()):
        assert line in out
    assert '本地访问' in out and 'PRODUCTION' in out


def test_startup_console_without_animation_prints_banner_before_returning(monkeypatch):
    from src.core import startup_banner as sb
    monkeypatch.setenv('NO_COLOR', '1')
    buf = io.StringIO()   # 非 TTY:横幅当场打完,不开线程
    show = sb.StartupConsole('0.1.0', '加载中', port=5000, stream=buf).start()
    out = buf.getvalue()
    # 转圈那行跟着横幅一起缩进,构图才不散(非 TTY 下就是 _BASE_INDENT)
    assert out == (sb.format_banner('0.1.0', port=5000, color=False)
                   + '\n' + ' ' * sb._BASE_INDENT + '加载中\n')
    assert show._thread is None
    show.stop()
    assert buf.getvalue() == out   # stop() 不往日志里塞转义
    assert '\033[?25l' not in out
    assert f'\033[{len(sb._BANNER_LINES)}A' not in out  # 没有光标上移重画


def _wide_tty(monkeypatch, columns=100):
    import shutil
    monkeypatch.setattr(shutil, 'get_terminal_size',
                        lambda *a, **k: os.terminal_size((columns, 30)))
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.delenv('TERRAFORGE_BANNER_ANIM', raising=False)
    return _FakeTty()


def test_should_animate_on_wide_color_tty(monkeypatch):
    from src.core.startup_banner import should_animate_banner
    assert should_animate_banner(_wide_tty(monkeypatch)) is True


def test_should_not_animate_when_terminal_too_narrow(monkeypatch):
    from src.core.startup_banner import should_animate_banner
    # 宽度不够会折行,ESC[12A 的行数就算错了,必须退化为静态横幅
    assert should_animate_banner(_wide_tty(monkeypatch, columns=40)) is False


def test_should_not_animate_when_env_switch_off(monkeypatch):
    from src.core.startup_banner import should_animate_banner
    buf = _wide_tty(monkeypatch)
    for value in ('0', 'false', 'off', 'NO'):
        monkeypatch.setenv('TERRAFORGE_BANNER_ANIM', value)
        assert should_animate_banner(buf) is False, value


def test_should_not_animate_on_non_tty_or_no_color(monkeypatch):
    from src.core.startup_banner import should_animate_banner
    _wide_tty(monkeypatch)
    monkeypatch.setenv('FORCE_COLOR', '1')
    assert should_animate_banner(io.StringIO()) is False   # 重定向到文件
    monkeypatch.setenv('NO_COLOR', '1')
    assert should_animate_banner(_FakeTty()) is False


def test_enable_windows_ansi_reports_capability():
    from src.core.startup_banner import _enable_windows_ansi
    result = _enable_windows_ansi()
    assert isinstance(result, bool)
    if os.name != 'nt':
        assert result is True   # 非 Windows 上 ANSI 恒可用,不该挡掉动画
