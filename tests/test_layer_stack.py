"""层栈契约（2026-08-15 Task 6：显隐机制 12 套 → 1 套，「关最上层」3 份 → 1 份）。

改前「按 Esc 关最上层」在 static/js 里被独立实现了三份，事件相位各不相同：
command_palette.js（document capture + stopPropagation）、ui.js 的两个自绘对话框
（capture + stopImmediatePropagation）、panels.js（bubble，靠 `body.modal-open` 与
`.app-confirm-overlay` 两道 DOM 查询给前两者让位）。三者的先后不是设计出来的，
是「谁挂在哪个相位、谁先注册」碰出来的 —— 加一层就得回头改另外两份的让位判据，
漏改的表现是「一次 Esc 关掉两层」。

现在只有 panels.js 一个 keydown 监听，其余浮层向 `window.TerraLayers.register()`
报到。本文件锁的就是这个形态本身，**逐条结构化断言，不靠数关键字**：

1. static/js 全仓只有一处「document/window 级、判 Escape 并关浮层」的 keydown。
2. 层栈 API 齐全（register / closeTop / topName），八层各有一处注册。
3. 注册项自身完整：isOpen 必给；close 与 dismissible:false 二选一。
4. ui.js 两个自绘对话框：声明了 aria-modal 就必须拦 Tab（改前是「声明了、零拦截」）。
5. 进度框显式声明 dismissible: false 并给出理由（改前是静默吞掉 Esc）。
6. toast 容器与进度框各有一个 aria-live 区。
7. base.html 里 panels.js 必须排在 command_palette.js 之前（后者解析期就 register）。
8. 参与层栈的浮层入场动画只有一套时长与曲线。

`_MOTION_BRANCH_COUNT` 与实际分支数的一致性不在这里断言 —— 它是
tests/test_css_contract.py::test_motion_rule_index_is_complete 的职责，
那条的失败消息会把 45 个分支逐个打出来，这里再抄一份只会分叉。
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, 'static', 'js')
CSS_PATH = os.path.join(ROOT, 'static', 'css', 'style.css')
BASE_HTML = os.path.join(ROOT, 'templates', 'base.html')

# 必须进层栈的八层。create/records/config 是三个 .workbench-panel 抽屉，
# cmdk/cmdkHelp 是命令面板与速查表，confirm/progress 是 ui.js 的两个自绘对话框，
# dropVeil 是全窗口拖拽提示遮罩。
#
# **刻意不在这张表里**（它们是局部状态，从不参与「谁是最上层」之争）：原生
# <details>、Vue 的 v-if、CSS 的 attr(data-hint) 气泡、Cesium 自带的 infoBox、
# 字段级的裸 hidden 翻转。Bootstrap 弹窗也不在：它自带 Esc 关闭，层栈只对它
# 整体让位（判据 body.modal-open，由 test_fix_frontend_hardening.py 钉）。
LAYER_NAMES = ('create', 'records', 'config', 'cmdk', 'cmdkHelp',
               'confirm', 'progress', 'dropVeil')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _js(name):
    return _read(os.path.join(JS_DIR, name))


def _js_files():
    return sorted(n for n in os.listdir(JS_DIR) if n.endswith('.js'))


_LINE_COMMENT = re.compile(r'(^|[^:\\])//[^\n]*')
_BLOCK_COMMENT = re.compile(r'/\*.*?\*/', re.S)


def _strip_comments(src):
    """去掉注释，但保留行数（换行不动）—— 失败消息里的行号才对得上。

    近似实现：正则会误伤字符串字面量里的 `//`（比如 URL）。本仓的这几个文件里
    没有裸 `//` 出现在字符串中间（协议前缀一律带 `:` 前缀，已在模式里排除），
    真出现了也只是多删几个字符，不会把 `'Escape'` 这类判据吃掉。
    """
    src = _BLOCK_COMMENT.sub(lambda m: '\n' * m.group(0).count('\n'), src)
    return _LINE_COMMENT.sub(lambda m: m.group(1), src)


def _balanced(src, start, opener='{', closer='}'):
    """从 src[start] 处那个 opener 开始，返回配对结束位置（含 closer）的下标。"""
    assert src[start] == opener, f'{start} 处不是 {opener!r}'
    depth = 0
    i = start
    while i < len(src):
        c = src[i]
        if c in ("'", '"', '`'):
            quote = c
            i += 1
            while i < len(src) and src[i] != quote:
                i += 2 if src[i] == '\\' else 1
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise AssertionError('括号不配对 —— 本测试的切分器已失效')


def _function_body(src, name):
    """`function <name>(...) { ... }` 的函数体（含外层花括号）。"""
    m = re.search(r'function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', src)
    assert m, f'找不到函数 {name}() —— 本测试已失效'
    open_at = m.end() - 1
    return src[open_at:_balanced(src, open_at) + 1]


def _object_arg_after(src, at):
    """src[at:] 里第一个 `{` 起的对象字面量文本。"""
    open_at = src.index('{', at)
    return src[open_at:_balanced(src, open_at) + 1]


# ---------------------------------------------------------------- 1. 唯一的 Esc

def _global_keydown_handlers(src):
    """(target, 处理器源码) —— 只收 document/window 级的 keydown 注册。

    元素级的（`input.addEventListener('keydown', …)`、`overlay.addEventListener`）
    一律不算：它们处理的是自己那个控件里的按键，不参与「谁是最上层」。
    """
    out = []
    for m in re.finditer(
            r'\b(document|window)\.addEventListener\(\s*[\'"]keydown[\'"]\s*,\s*', src):
        tail = src[m.end():]
        if tail.startswith('function') or tail.startswith('('):
            open_at = src.index('{', m.end())
            out.append((m.group(1), src[open_at:_balanced(src, open_at) + 1]))
            continue
        ident = re.match(r'([A-Za-z_$][\w$]*)', tail)
        assert ident, f'认不出 {m.group(1)}.addEventListener 的处理器形态 —— 本测试已失效'
        out.append((m.group(1), _function_body(src, ident.group(1))))
    return out


def test_exactly_one_escape_closer_in_the_whole_frontend():
    """全仓只许有一处「document/window 级 keydown 判 Escape 并关浮层」。

    判据是**结构**不是字面量：先找 document/window 上的 keydown 注册（元素级的
    不算），再解出它的处理器源码，看它是否既判 `'Escape'` 又执行关闭动作
    （closeTop / close* / reset / hide）。三份实现的时代这条会数出 3。
    """
    closers = []
    for name in _js_files():
        src = _strip_comments(_js(name))
        for target, body in _global_keydown_handlers(src):
            if "'Escape'" not in body and '"Escape"' not in body:
                continue
            if not re.search(r'\b(closeTop|close[A-Z]\w*|reset|hide)\s*\(', body):
                continue
            closers.append(f'{name} ({target} 级)')
    assert closers == ['panels.js (document 级)'], (
        '「按 Esc 关最上层」必须全站只有一处、且在 panels.js 的层栈里，'
        f'实际：{closers or "一处都没有"}'
    )


# ---------------------------------------------------------------- 2/3. 注册面

def test_panels_js_exposes_the_layer_stack_api():
    src = _strip_comments(_js('panels.js'))
    m = re.search(r'window\.TerraLayers\s*=\s*\{', src)
    assert m, 'panels.js 没有暴露 window.TerraLayers'
    exposed = src[m.end() - 1:_balanced(src, m.end() - 1) + 1]
    for fn in ('register', 'closeTop', 'topName'):
        assert re.search(r'\b' + fn + r'\s*:', exposed), (
            f'window.TerraLayers 缺 {fn} —— 契约是 {{ register, closeTop, topName }}'
        )
        assert re.search(r'function\s+' + fn + r'\s*\(', src), (
            f'panels.js 暴露了 {fn} 却没有定义它'
        )


def _panel_names_registered_in_loop(src):
    """panels.js 里「遍历 PANELS 表逐个 register」那条路径覆盖到的面板名。

    面板共用一个 `current` 槽（互斥），所以它们是在一个 forEach 里注册的 ——
    名字的唯一来源是 PANELS 表本身，逐个写死一遍反而会分叉。这里把那条循环
    解出来，好让「八层各有一处注册」这条断言对面板也成立。
    """
    loop = re.search(r'Object\.keys\(PANELS\)\.forEach\(function\s*\(\s*(\w+)\s*\)\s*\{', src)
    if not loop:
        return set()
    var = loop.group(1)
    open_at = loop.end() - 1
    body = src[open_at:_balanced(src, open_at) + 1]
    if not re.search(r'\bregister\(\s*' + re.escape(var) + r'\s*,', body):
        return set()
    table = re.search(r'var PANELS\s*=\s*\{', src)
    assert table, 'panels.js 里找不到 PANELS 表 —— 本测试已失效'
    open_at = table.end() - 1
    return set(re.findall(r'(\w+)\s*:', src[open_at:_balanced(src, open_at) + 1]))


def _explicit_registrations():
    """{层名: (文件名, 注册项对象字面量)} —— 写死名字的那些 register 调用。"""
    found = {}
    for name in _js_files():
        src = _strip_comments(_js(name))
        for m in re.finditer(r'register\(\s*[\'"](\w+)[\'"]\s*,', src):
            found[m.group(1)] = (name, _object_arg_after(src, m.end()))
    return found


def test_every_layer_registers_itself():
    explicit = _explicit_registrations()
    from_loop = _panel_names_registered_in_loop(_strip_comments(_js('panels.js')))
    registered = set(explicit) | from_loop
    missing = [n for n in LAYER_NAMES if n not in registered]
    assert not missing, (
        f'这些浮层没有向层栈报到：{missing}。没报到 = Esc 关不掉它，'
        f'已注册的是 {sorted(registered)}'
    )


def test_every_registration_is_complete():
    """注册项必须给得出「我开着没」和「怎么关」，否则层栈调度不了它。

    close 与 dismissible: false 二选一：不可关闭的层不该给一个只能骗人的
    close，可关闭的层不给 close 就是注册了个哑巴。
    """
    problems = []
    for layer, (fname, spec) in sorted(_explicit_registrations().items()):
        if not re.search(r'\bisOpen\s*:', spec):
            problems.append(f'{fname}: {layer} 没给 isOpen —— 栈顶判定不了它')
        has_close = bool(re.search(r'\bclose\s*:', spec))
        locked = bool(re.search(r'\bdismissible\s*:\s*false\b', spec))
        if has_close == locked:
            problems.append(
                f'{fname}: {layer} 的 close / dismissible:false 必须**恰好**给一个，'
                f'实际 close={has_close} dismissible:false={locked}')
    assert not problems, '\n'.join(problems)


def test_progress_dialog_is_registered_non_dismissible_with_a_reason():
    """进度框必须**显式声明**关不掉，并说出为什么。

    改前它在捕获阶段把 Esc 吞掉、界面上什么都不发生 —— 与「这个键坏了」完全
    无法区分。删除到一半没有回滚，所以「关不掉」本身是对的，错的是不吭声。
    """
    _, spec = _explicit_registrations()['progress']
    assert re.search(r'\bdismissible\s*:\s*false\b', spec), (
        '进度框没有声明 dismissible: false —— 它要么被 Esc 关掉（删除中途没有回滚），'
        '要么又变回静默吞键'
    )
    assert re.search(r"\breason\s*:\s*t\(\s*'[\w.]+'\s*\)", spec), (
        '进度框声明了关不掉却没给理由（reason 必须是一条 i18n 键的完整字面量）—— '
        '不吭声的拒绝就是原缺陷本身'
    )


# ---------------------------------------------------------------- 4. Tab 焦点环

def test_self_drawn_dialogs_trap_tab_or_drop_the_aria_modal_claim():
    """自报 aria-modal 就必须拦 Tab。改前两个框都是「声明了、零拦截」。

    aria-modal="true" 是向读屏承诺「遮罩之外的一切已经冻结」。承诺了却不拦
    Tab，键盘/读屏用户会一路 Tab 到身后那半个界面上 —— 那比不声明更糟：读屏
    按承诺把外面的内容从虚拟缓冲里摘掉了，用户却把焦点送了进去。
    """
    src = _strip_comments(_js('ui.js'))
    problems = []
    for fn in ('showConfirm', 'showProgressDialog'):
        body = _function_body(src, fn)
        claims_modal = bool(re.search(
            r"setAttribute\(\s*'aria-modal'\s*,\s*'true'\s*\)", body))
        traps = bool(re.search(r"'Tab'", body) and re.search(r'\btrapTab\(', body))
        if claims_modal and not traps:
            problems.append(f'{fn}: 声明了 aria-modal="true" 却不拦 Tab')
    assert not problems, '\n'.join(problems)
    assert re.search(r'function trapTab\(', src), (
        'trapTab 必须是 ui.js 里的一份公共实现（原本只长在 command_palette.js 里，'
        '两个自绘对话框零拦截）'
    )
    assert 'window.trapTab(' in _strip_comments(_js('command_palette.js')), (
        'command_palette.js 没有改用公共的 trapTab —— 全站又变回两份'
    )


def test_danger_confirm_rests_on_cancel():
    """`danger: true` 的静息焦点必须是取消键，不是确认键。

    改前一律 focus 确认键，于是一发回车就把东西删了（审查记为暗模式）。
    Enter 仍然是确认（层栈的 accept），只是要用户主动按。
    """
    body = _function_body(_strip_comments(_js('ui.js')), 'showConfirm')
    m = re.search(r'\(\s*selectEl\s*\|\|([\s\S]*?)\)\.focus\(\)', body)
    assert m, '认不出 showConfirm 的焦点落点 —— 本测试已失效'
    landing = m.group(1)
    assert 'danger' in landing and 'cancelBtn' in landing, (
        f'danger 确认框的静息焦点不是取消键，实际落点表达式：{landing.strip()!r}'
    )


# ---------------------------------------------------------------- 5. live 区

def test_toast_container_and_progress_dialog_are_live_regions():
    """两处播报区都必须是**常驻** live 区。

    toast 改前只在每个 toast 节点上设 role="alert"（节点先建后插，靠读屏把
    「刚插入的节点」当警报处理，各家实现不一）；进度框改前只有 role=progressbar
    + aria-valuenow，而属性变化不进 live 区 —— 百分比从头到尾一声不响。
    """
    src = _strip_comments(_js('ui.js'))
    container = _function_body(src, 'ensureToastContainer')
    assert re.search(r"setAttribute\(\s*'aria-live'\s*,\s*'polite'\s*\)", container), (
        'toast 容器不是 live 区 —— 提示对读屏用户等于不存在'
    )
    assert re.search(r"setAttribute\(\s*'aria-atomic'\s*,\s*'false'\s*\)", container), (
        'toast 容器缺 aria-atomic="false" —— 每来一条都会把堆着的十条重念一遍'
    )
    progress = _function_body(src, 'showProgressDialog')
    assert re.search(r"setAttribute\(\s*'aria-live'\s*,\s*'polite'\s*\)", progress), (
        '进度框缺 aria-live —— 百分比变化从不播报'
    )
    assert not re.search(r"label\.setAttribute\(\s*'aria-hidden'", progress), (
        '进度百分比又被 aria-hidden 了 —— 它是这个 live 区里唯一会变的文本，'
        '藏起来等于 aria-live 白加'
    )


# ---------------------------------------------------------------- 6. 入场动画

# 参与层栈的浮层入场：遮罩担 opacity、卡片担 transform。面板是唯一「遮罩与卡片
# 同体」的，两条都在自己身上。改前是四种写法（含一个 --dur-fast 孤儿档，以及
# 完全没有入场的命令面板）。
ENTRANCE_SELECTORS = (
    '.workbench-panel', '.app-confirm-overlay', '.app-confirm',
    '.cmdk', '.cmdk__dialog', '.drop-veil', '.drop-veil__tip',
)


def _rule_body(css, selector):
    m = re.search(r'(?:^|\})\s*' + re.escape(selector) + r'\s*\{([^}]*)\}', css)
    assert m, f'找不到 {selector} 的规则 —— 本测试已失效'
    return m.group(1)


def test_layer_entrances_share_one_duration_and_one_curve():
    css = _BLOCK_COMMENT.sub('', _read(CSS_PATH))
    recipes = {}
    for sel in ENTRANCE_SELECTORS:
        body = _rule_body(css, sel)
        decl = re.search(r'transition:\s*([^;]+);', body)
        assert decl, f'{sel} 没有入场过渡 —— 层栈里的浮层必须有一套统一入场'
        parts = [p.strip() for p in decl.group(1).split(',')]
        for part in parts:
            bits = part.split()
            assert len(bits) == 3, f'{sel} 的过渡 {part!r} 不是「属性 时长 曲线」三段'
            prop, dur, ease = bits
            assert prop in ('opacity', 'transform'), (
                f'{sel} 过渡了 {prop} —— 统一入场只有 opacity 与 transform 两条'
            )
            recipes.setdefault((dur, ease), []).append(f'{sel}:{prop}')
    assert list(recipes) == [('var(--dur-base)', 'var(--ease)')], (
        '层栈浮层的入场必须只有一套时长与曲线（var(--dur-base) + var(--ease)），实际：\n'
        + '\n'.join(f'  {k}: {v}' for k, v in recipes.items())
    )


# ---------------------------------------------------------------- 7. 加载顺序

def test_panels_js_loads_globally_and_before_the_command_palette():
    """层栈的宿主必须全站加载，且排在 command_palette.js 之前。

    两条都是硬要求：confirm / progress / cmdk / cmdk help 四层在 /config 与
    /history 上照样会出现，层栈只挂 index.html 的话那两页按 Esc 关不掉确认框；
    而 command_palette.js 在**解析期**就 register，排在它后面等于 register
    的时候 window.TerraLayers 还不存在。
    """
    base = _read(BASE_HTML)
    panels_at = base.find("filename='js/panels.js'")
    cmdk_at = base.find("filename='js/command_palette.js'")
    assert panels_at != -1, (
        'base.html 没有加载 panels.js —— 层栈只挂首页的话，/config 与 /history 上'
        '按 Esc 关不掉确认框'
    )
    assert cmdk_at != -1, 'base.html 没有加载 command_palette.js —— 本测试已失效'
    assert panels_at < cmdk_at, (
        'panels.js 必须排在 command_palette.js 之前 —— 后者解析期就 register，'
        '顺序反了 window.TerraLayers 还不存在'
    )
    index = _read(os.path.join(ROOT, 'templates', 'index.html'))
    assert "filename='js/panels.js'" not in index, (
        'index.html 又加载了一遍 panels.js —— 两份 IIFE 会挂两个 keydown 监听，'
        '一次 Esc 关两层'
    )
