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
6. toast 容器是**常驻**的 aria-live 区（模块初始化就挂好，不是第一条提示时才建，
   否则 live 区与内容同一 tick 出现、读屏不播首条），进度框自身也是一个 live 区。
7. base.html 里 panels.js 必须排在 command_palette.js 之前（后者解析期就 register）。
8. 参与层栈的浮层入场动画只有一套时长与曲线。
9. 对话框在场时拖拽遮罩不出场 —— 顶部三层的 z 令牌与栈序是反的，靠这条摆平。

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


# 一个 `/` 起正则字面量还是除号，按前一个非空白字符判：跟在标识符、数字、`)`
# 或 `]` 后面的是除号，跟在下面这些之后的才可能开正则。static/js 里没有
# `return /re/` 这类关键字后接正则的写法（改这条时先 grep 确认），所以不必再
# 认关键字。
_REGEX_PREV = set('(,=:[!&|?{};+-*%~^<>')


def _balanced(src, start, opener='{', closer='}'):
    """从 src[start] 处那个 opener 开始，返回配对结束位置（含 closer）的下标。

    引号与正则字面量整体跳过。正则那一路是后补的：ui.js 的 escapeHtml 里有
    `/"/g` 与 `/'/g`，不跳的话那两个引号会被当成字符串开头，从此括号计数全乱
    —— 表现是扫全文件时这里抛「括号不配对」，而不是安静地切错。
    """
    assert src[start] == opener, f'{start} 处不是 {opener!r}'
    depth = 0
    i = start
    prev = ''  # 上一个非空白字符
    while i < len(src):
        c = src[i]
        if c in ("'", '"', '`'):
            quote = c
            i += 1
            while i < len(src) and src[i] != quote:
                i += 2 if src[i] == '\\' else 1
            prev = quote
        elif c == '/' and (prev == '' or prev in _REGEX_PREV):
            i += 1
            while i < len(src) and src[i] != '/':
                if src[i] == '\\':
                    i += 1
                elif src[i] == '[':
                    # 字符组里的 `/` 不结束正则（`/[/x]/`）
                    while i < len(src) and src[i] != ']':
                        i += 2 if src[i] == '\\' else 1
                i += 1
            prev = '/'
        elif c == opener:
            depth += 1
            prev = c
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
            prev = c
        elif not c.isspace():
            prev = c
        i += 1
    raise AssertionError('括号不配对 —— 本测试的切分器已失效')


def _function_body_at(src, name):
    """(`function <name>(...) { ... }` 的函数体, 它在 src 里的起始下标)。"""
    m = re.search(r'function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', src)
    assert m, f'找不到函数 {name}() —— 本测试已失效'
    open_at = m.end() - 1
    return src[open_at:_balanced(src, open_at) + 1], open_at


def _function_body(src, name):
    """`function <name>(...) { ... }` 的函数体（含外层花括号）。"""
    return _function_body_at(src, name)[0]


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
        mthis = re.match(r'this\.([A-Za-z_$][\w$]*)', tail)
        if mthis:
            # `this._onKeydown` 形态（modal.js 的 TfModal：removeEventListener
            # 要同一引用，处理器只能存成实例字段）—— 裸 ident 会拿 'this'
            # 去查 function this()，直接失效。
            out.append((m.group(1), _class_field_arrow_body(src, mthis.group(1))))
            continue
        out.append((m.group(1), _function_body(src, ident.group(1))))
    return out


def _class_field_arrow_body(src, name):
    """`this._name = (...) => { ... }` 类字段箭头的函数体（含外层花括号）。"""
    m = re.search(r'\b' + re.escape(name) + r'\s*=\s*\([^)]*\)\s*=>\s*\{', src)
    assert m, f'找不到类字段箭头 {name} = (...) => {{ —— 本测试已失效'
    open_at = m.end() - 1
    return src[open_at:_balanced(src, open_at) + 1]


def test_exactly_one_escape_closer_in_the_whole_frontend():
    """全站只许有「panels.js 层栈 + TfModal 自关」两处 Esc 关浮层。

    判据是**结构**不是字面量：先找 document/window 上的 keydown 注册（元素级的
    不算），再解出它的处理器源码，看它是否既判 `'Escape'` 又执行关闭动作
    （closeTop / close* / reset / hide）。三份实现的时代这条会数出 3。

    第二处是 2026-08-19 Task 8 登记进来的：自研 modal.js 替掉 bootstrap.Modal
    之后，「弹窗按 Esc 关自己」这份处理器从 vendor/（本扫描天然看不见）搬进了
    static/js/，必须显式记账。它不算竞争者是两点保证的：只在弹窗开着时挂
    （hide 同步 removeEventListener），且 panels.js 的 onKey 用
    body.modal-open 对它整体让位 —— 「一次 Esc 两层全关」由那道让位防，
    不由「只有一处」防。
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
    assert closers == ['modal.js (document 级)', 'panels.js (document 级)'], (
        '「按 Esc 关浮层」全站只许 panels.js 层栈与 modal.js 自关两处，'
        f'实际：{closers or "一处都没有"}'
    )


def _element_keydown_handlers(src):
    """[(挂载目标, 处理器源码, 处理器体在 src 里的起始下标)] —— 元素级的 keydown。

    与 _global_keydown_handlers 互补：那边只收 document / window，这边收其余
    一切（`input.` / `overlay.` / `boundsInfo.` / `document.body.` …）。具名处理器
    （`boundsInfo.addEventListener('keydown', _handleManualBoundsKeydown)`）必须跟进
    函数体 —— 本仓最典型的那处漏 stopPropagation 正是这种写法，只认内联函数
    就会正好把它放过去。
    """
    out = []
    for m in re.finditer(r'\.addEventListener\(\s*[\'"]keydown[\'"]\s*,\s*', src):
        owner = re.search(r'([A-Za-z_$][\w$.]*)$', src[:m.start()])
        target = owner.group(1) if owner else '<表达式>'
        if target in ('document', 'window'):
            continue
        tail = src[m.end():]
        assert not re.match(r'[A-Za-z_$][\w$]*\s*=>', tail), (
            f'{target}.addEventListener 的处理器是裸参数箭头函数 —— 本测试的切分器'
            '不认这种形态，会把它整个漏掉（= 断言悄悄变弱），请在这里补上'
        )
        if tail.startswith('function') or tail.startswith('('):
            open_at = src.index('{', m.end())
            out.append((target, src[open_at:_balanced(src, open_at) + 1], open_at))
            continue
        ident = re.match(r'([A-Za-z_$][\w$]*)', tail)
        assert ident, f'认不出 {target}.addEventListener 的处理器形态 —— 本测试已失效'
        body, at = _function_body_at(src, ident.group(1))
        out.append((target, body, at))
    return out


_IF_HEAD = re.compile(r'\bif\s*\(')


def _escape_branches(body):
    """[(分支在 body 里的起始下标, 分支源码)] —— 每个「判 Escape」的分支体。

    三种形态都要认，漏认哪一种，那一种就等于没被断言到：
      `if (e.key === 'Escape') { … }`     花括号块
      `if (e.key === 'Escape') foo();`    无花括号的单条语句
      `if (e.key !== 'Escape') return;`   反向守卫 —— Escape 走的是「不 return」
                                          那条路，分支体是函数剩下的全部
    """
    out = {}
    for lit in re.finditer(r'[\'"]Escape[\'"]', body):
        paren = None
        for m in _IF_HEAD.finditer(body[:lit.start()]):
            at = m.end() - 1
            if _balanced(body, at, '(', ')') > lit.start():
                paren = at        # 取最后一个「条件跨过该字面量」的 if = 最内层
        assert paren is not None, (
            f'{body[max(0, lit.start() - 80):lit.end()]!r} 里的 Escape 不在某条 if '
            '的条件里 —— 本测试的切分器已失效'
        )
        cond_end = _balanced(body, paren, '(', ')')
        cond = body[paren:cond_end + 1]
        i = cond_end + 1
        while i < len(body) and body[i].isspace():
            i += 1
        if re.search(r'!==?\s*[\'"]Escape[\'"]', cond):
            out.setdefault(i, body[i:])
        elif body[i] == '{':
            out.setdefault(i, body[i:_balanced(body, i) + 1])
        else:
            out.setdefault(i, body[i:body.index(';', i) + 1])
    return sorted(out.items())


# 分支里「做了事」= 调了这几个之外的任何函数。用「调用」而不是白名单函数名
# （close* / cancel* / commit …）判定：白名单一改名就静默放行，而重命名恰恰是
# 最容易发生的事。
_EVENT_ONLY = frozenset((
    'preventDefault', 'stopPropagation', 'stopImmediatePropagation',
    'if', 'for', 'while', 'switch', 'catch', 'return', 'function',
))


def _branch_actions(branch):
    names = re.findall(r'([A-Za-z_$][\w$.]*)\s*\(', branch)
    return [n for n in names if n.rsplit('.', 1)[-1] not in _EVENT_ONLY]


def test_element_scoped_escape_branches_stop_propagation():
    """元素级 keydown 用 Esc 关/取消了什么，就必须在同一分支里 stopPropagation()。

    这不是风格，是 panels.js 选了 **bubble** 相位的配套条件。层栈的 Esc 挂在
    document 的 bubble 上（capture 会抢在输入框自己的 Esc 之前，把「收起我自己
    那个下拉」全部截胡），代价是元素级处理器跑完之后事件照样冒到 document，
    层栈会把栈顶那层再关一次 —— 一次 Esc 关两层，正是 panels.js 声称已消灭的
    那个缺陷。panels.js:167 那段注释把「元素级都会 stopPropagation」当成既成
    事实，2026-08-15 之前三处里只有地名搜索成立。

    复现路径（修复前，键盘用户）：#createPanel 开着 → Tab 到未被 inert 的
    #boundsManualBtn → 展开手动四至 → Esc → 四至取消 + 整张面板一起关掉。
    鼠标用户碰不到，靠的是「面板入口先 window.closePanel()」这类位置依赖。

    判据是结构不是字面量：解出每个元素级 keydown 处理器（具名的跟进函数体）
    → 切出判 Escape 的分支 → 分支里只要调了 preventDefault / stopPropagation
    以外的函数（= 它做了事），同分支就必须有 stopPropagation()。
    """
    offenders = []
    acting = []
    for name in _js_files():
        src = _strip_comments(_js(name))
        for target, body, at in _element_keydown_handlers(src):
            for rel, branch in _escape_branches(body):
                actions = _branch_actions(branch)
                if not actions:
                    continue                      # 只 return / 只 preventDefault：没关东西
                line = src.count('\n', 0, at + rel) + 1
                acting.append(f'{name}:{line}')
                if 'stopPropagation' in branch:
                    continue
                offenders.append(
                    f'{name}:{line}（{target} 上的 keydown，Esc 分支调了 '
                    f'{", ".join(sorted(set(actions)))}）'
                )
    assert len(acting) >= 3, (
        f'只扫到 {len(acting)} 个「元素级 Esc 分支且真的做了事」的分支：{acting}。'
        '本仓已知有三处 —— 地名搜索下拉、手动四至面板、四至点击编辑输入框。'
        '数目掉下来通常不是删了功能，而是切分器认不出新写法，这条断言正在变成恒真'
    )
    assert not offenders, (
        '元素级 keydown 的 Esc 分支必须 e.stopPropagation()，否则同一次 Esc 冒到 '
        'document 后会被 panels.js 的层栈当成「关最上层」再执行一次：\n  '
        + '\n  '.join(offenders)
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


# ------------------------------------------------- 4. Tab 焦点环与确认框选项契约

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

    钉的是三元表达式的**形状**，不是子串。原判据 `'danger' in landing and
    'cancelBtn' in landing` 分不出正反：把三元写反成 `danger ? okBtn : cancelBtn`
    时两个子串照样都在，断言照样绿 —— 而那正是本条要拦的缺陷。
    """
    body = _function_body(_strip_comments(_js('ui.js')), 'showConfirm')
    m = re.search(r'\(\s*selectEl\s*\|\|([\s\S]*?)\)\.focus\(\)', body)
    assert m, '认不出 showConfirm 的焦点落点 —— 本测试已失效'
    landing = m.group(1)
    assert re.search(r'danger\s*\?\s*cancelBtn\s*:\s*okBtn', landing), (
        f'danger 确认框的静息焦点不是取消键，实际落点表达式：{landing.strip()!r}'
    )


# 「所在函数名带破坏性语义」的判据。按函数名而不是按文案筛：文案是 i18n 键，
# 键名改了判据就瞎；函数名是调用点自己的身份。accept\w*gap 覆盖 acceptTaskGaps
# （接受缺口导出是不可撤销的：产物与历史永久带缺块标记）。
_DESTRUCTIVE_HOLDER = (r'delete', r'reset', r'clear', r'accept\w*gap')


def _split_top_level_args(text):
    """按最外层逗号切开 `a, b, c` —— 跳过嵌套括号/花括号/方括号与字符串。"""
    args = []
    depth = 0
    start = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c in ("'", '"', '`'):
            quote = c
            i += 1
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == '\\' else 1
        elif c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif c == ',' and depth == 0:
            args.append(text[start:i])
            start = i + 1
        i += 1
    args.append(text[start:])
    return args


def _enclosing_function(src, at):
    """src[at] 所在的最近一个**具名** function 声明的名字。

    调用点几乎都埋在 `guard(trigger, async function () {…})` 的匿名函数里，所以
    取「最后一个起始下标在 at 之前的具名声明」，而不是最近的 function 关键字。
    """
    name = None
    for m in re.finditer(r'\bfunction\s+([A-Za-z_$][\w$]*)\s*\(', src):
        if m.start() > at:
            break
        name = m.group(1)
    return name


def _show_confirm_calls():
    """static/js 里每一处 showConfirm( 调用：(文件, 行号, 所在具名函数, 选项对象文本)。

    选项对象**不能**拿「调用括号之后第一个 `{`」来取：第一个实参形如
    `t('key', { n: … })`，里面就有花括号。这里按最外层逗号切实参、取最后一个。
    """
    calls = []
    for name in _js_files():
        src = _strip_comments(_js(name))
        for m in re.finditer(r'\bshowConfirm\s*\(', src):
            # ui.js 里的 `function showConfirm(message, opts)` 是定义，不是调用
            if src[:m.start()].rstrip().endswith('function'):
                continue
            open_at = m.end() - 1
            args = _split_top_level_args(src[open_at + 1:_balanced(src, open_at, '(', ')')])
            opts = args[-1].strip() if len(args) > 1 else ''
            calls.append((name, src.count('\n', 0, m.start()) + 1,
                          _enclosing_function(src, m.start()),
                          opts if opts.startswith('{') else ''))
    return calls


def test_destructive_confirms_actually_pass_danger():
    """破坏性动作的每一处 showConfirm 都必须真的传 `danger: true`。

    改前 task_center.js 的 acceptTaskGaps 传的是 `type: 'warning'` —— 一个
    showConfirm 根本不读的键（见下一条测试的选项表）。写了等于没写：这条自称
    「不可撤销」的动作走了非 danger 分支，静息焦点落在确认键上，一发回车就
    accept，确认键也不带 is-danger 配色。

    判据落在「调用点所在函数名」上，失败消息给出文件:行 + 函数名。
    """
    calls = _show_confirm_calls()
    assert calls, '一处 showConfirm 调用都没扫到 —— 本测试已失效'
    destructive = [c for c in calls if c[2]
                   and any(re.search(p, c[2].lower()) for p in _DESTRUCTIVE_HOLDER)]
    assert destructive, (
        '没认出任何破坏性调用点 —— 判据 _DESTRUCTIVE_HOLDER 已与代码脱节'
    )
    missing = [
        f'{fname}:{line} {holder}() 是破坏性动作，选项里没有 danger: true：'
        f'{opts or "（没有选项对象）"}'
        for fname, line, holder, opts in destructive
        if not re.search(r'\bdanger\s*:\s*true\b', opts)
    ]
    assert not missing, '\n'.join(missing)


def test_show_confirm_call_sites_pass_no_keys_it_ignores():
    """调用点传的每个顶层键都必须是 showConfirm 真的读的键。

    选项表从 ui.js 里 showConfirm 函数体的 `opts.X` 读法**现推**，不写死 —— 写死
    的表会与实现分叉，而分叉的方向恰好就是原缺陷（`type: 'warning'` 静默失效，
    没有任何一处会报错）。
    """
    known = set(re.findall(
        r'\bopts\.([A-Za-z_$][\w$]*)',
        _function_body(_strip_comments(_js('ui.js')), 'showConfirm')))
    assert {'title', 'danger', 'checkbox', 'select'} <= known, (
        f'没从 showConfirm 里认出选项表，实际读到：{sorted(known)}'
    )
    strays = []
    for fname, line, holder, opts in _show_confirm_calls():
        if not opts:
            continue
        for entry in _split_top_level_args(opts[1:-1]):
            key = re.match(r'\s*([A-Za-z_$][\w$]*)\s*:', entry)
            if key and key.group(1) not in known:
                strays.append(
                    f'{fname}:{line} {holder or "?"}() 传了 {key.group(1)}: —— '
                    f'showConfirm 不读这个键，写了等于没写（它认的是 {sorted(known)}）'
                )
    assert not strays, '\n'.join(strays)


# ---------------------------------------------------------------- 5. live 区

def _named_function_spans(src):
    """[(名字, 体起, 体止)] —— 所有 `function NAME(...) {...}` 声明的函数体范围。

    只收**具名**声明：判「某处代码是否在模块初始化路径上」时，落在匿名回调里
    （`addEventListener('DOMContentLoaded', function () {...})`）的调用仍然算
    初始化，落在具名工具函数里（showToast / ensureToastContainer）的不算。
    """
    spans = []
    for m in re.finditer(r'function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{', src):
        open_at = m.end() - 1
        spans.append((m.group(1), open_at, _balanced(src, open_at)))
    return spans


def _toast_container_creator(src):
    """(函数名, 函数体) —— 建出 toast 容器并挂进文档的那一个函数。

    按行为认而不按名字认：体里同时出现容器 id、createElement、aria-live 的
    setAttribute 与 document.body.appendChild。showProgressDialog 也建 live 区
    并 appendChild 到 body，容器 id 是把它排除掉的那个判据。
    """
    hits = []
    for name, open_at, close_at in _named_function_spans(src):
        body = src[open_at:close_at + 1]
        if ('app-toast-container' in body or 'TOAST_CONTAINER_ID' in body) \
                and 'createElement' in body \
                and re.search(r"setAttribute\(\s*'aria-live'", body) \
                and 'document.body.appendChild' in body:
            hits.append((name, body))
    assert len(hits) == 1, (
        f'期望恰好一个函数负责建 toast 容器，实际认出 {[n for n, _ in hits]} —— 本测试已失效'
    )
    return hits[0]


def test_toast_container_is_a_resident_live_region():
    """toast 容器必须在**模块初始化**时就建好挂上，不是第一条提示时才建。

    「常驻」是播报的前提：live 区要先落进无障碍树，之后插进去的子节点才被念。
    改前容器是在第一次 showToast 里 createElement + appendChild 的，与 toast
    内容同一个同步块出现 —— 读屏普遍不播，每次页面加载后的第一条提示（含失败
    提示）对读屏用户是静默的。原来这条只查 ensureToastContainer 体里那两个
    setAttribute：属性对了，而它自己 docstring 声明的「常驻」根本没测。

    判据：建容器的那个函数在**具名函数之外**（模块求值 / 初始化回调里）被调到，
    且 showToast 不是它唯一的到达路径。
    """
    src = _strip_comments(_js('ui.js'))
    creator, body = _toast_container_creator(src)
    assert re.search(r"setAttribute\(\s*'aria-live'\s*,\s*'polite'\s*\)", body), (
        'toast 容器不是 live 区 —— 提示对读屏用户等于不存在'
    )
    assert re.search(r"setAttribute\(\s*'aria-atomic'\s*,\s*'false'\s*\)", body), (
        'toast 容器缺 aria-atomic="false" —— 每来一条都会把堆着的十条重念一遍'
    )

    spans = _named_function_spans(src)
    holders = set()
    for m in re.finditer(r'\b' + re.escape(creator) + r'\b', src):
        # 声明处本身不算引用
        if re.search(r'function\s+$', src[:m.start()]):
            continue
        inner = [n for n, a, b in spans if a < m.start() < b]
        holders.add(inner[-1] if inner else None)  # None = 具名函数之外
    assert holders, f'{creator}() 定义了却没人调 —— 容器根本不会出现'
    assert None in holders, (
        f'{creator}() 只在 {sorted(h for h in holders if h)} 里被调到，模块初始化路径上'
        '没有创建/挂载 —— 容器又变回「第一条提示时才建」，live 区与内容同一 tick '
        '出现，读屏不播每页的第一条提示'
    )
    assert holders - {None} != {'showToast'}, (
        'showToast 直接建容器 —— 常驻挂载被它的首次调用顶替了'
    )

    # 兜底路径要留着：showToast 走 ensureToastContainer（先取现成节点，取不到
    # 才建），而不是自己 createElement，也不是假定容器一定在。
    fallback = _function_body(src, 'ensureToastContainer')
    assert 'getElementById' in fallback and creator in fallback, (
        f'ensureToastContainer 不再是「取现成节点 → 取不到才 {creator}()」—— '
        '早于本模块初始化的内联 showToast 调用会拿不到容器'
    )
    assert 'ensureToastContainer(' in _function_body(src, 'showToast'), (
        'showToast 不再经 ensureToastContainer 取容器 —— 容器被摘掉后提示会整个消失'
    )


def test_progress_dialog_is_a_live_region():
    """进度框自身是 live 区，且百分比文本没被 aria-hidden 藏起来。

    改前只有 role=progressbar + aria-valuenow，而属性变化不进 live 区 ——
    百分比从头到尾一声不响。这个框不需要像 toast 容器那样提前挂：要播的是
    update() 里后续 tick 的变化，那时 dialog 早在无障碍树里了。
    """
    src = _strip_comments(_js('ui.js'))
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


# ------------------------------------------- 9. 顶部三层：视觉在上的必须也在栈顶

def _listener_body(src, target, event):
    """`<target>.addEventListener('<event>', function (e) { … })` 的函数体。"""
    at = src.find(target + ".addEventListener('" + event + "'")
    assert at != -1, f'找不到 {target} 上的 {event} 监听 —— 本测试的切分器已失效'
    return _object_arg_after(src, at)


def test_drop_veil_stands_down_while_a_dialog_is_up():
    """对话框在场时，dragenter 必须在 show() 与 preventDefault() **之前**退出。

    顶部三层的 z 令牌与栈序是反的：--z-drop-veil(13000) 压在 --z-confirm(12000)
    之上，而栈顶按**注册序**算，confirm / progress 是打开时现注册，永远在
    dropVeil 之上。于是零守卫的 dragenter 会这样错：confirm 开着时把 .tif 拖进
    窗口，满屏的雾（--color-backdrop-strong + accent 虚线框）盖住确认卡片，而
    此刻按 Esc 关掉的是身后那张卡片。cmdk 是同一错位的镜像。

    摆平它的是「对话框在场就不接管拖拽」，两条判据都得有：层栈的 topName()
    （confirm / progress / cmdk / cmdkHelp 在场时非空）与 body.modal-open
    （Bootstrap 弹窗不进层栈，与 panels.js 的 onKey 让位同一口径）。
    preventDefault() 也必须排在守卫之后 —— 它就是「这次投放我接管」那句话，
    先说了再退出等于允许投放却不给任何反馈。
    """
    body = _listener_body(_strip_comments(_js('drop_process.js')), 'window', 'dragenter')
    show_at = body.find('show()')
    assert show_at != -1, 'dragenter 分支里找不到 show() —— 本测试的切分器已失效'
    prevent_at = body.find('preventDefault')
    assert prevent_at != -1, (
        'dragenter 分支里找不到 preventDefault() —— 没有它 dragover/drop 整条'
        '投放链就不成立，本测试的切分器或投放实现已换形态'
    )
    gate_at = min(show_at, prevent_at)
    for probe, what in (
        ('topName(', '层栈栈顶（confirm / progress / cmdk / cmdkHelp 在场时非空）'),
        ('modal-open', 'body.modal-open（Bootstrap 弹窗 #pathBrowserModal / 历史详情）'),
    ):
        at = body.find(probe)
        assert at != -1, (
            f'drop_process.js 的 dragenter 没有检查 {what}：缺 `{probe}`。'
            'confirm / cmdk 开着时把 .tif 拖进窗口，拖拽遮罩会盖住对话框，而'
            '这一按 Esc 关掉的是身后那层 —— 用户看着 A、关掉 B。'
            '修法是不接管（既不 show() 也不 preventDefault()），不是抬 z：'
            '抬 z 只换个方向错，Esc 关的仍是身后那层'
        )
        assert at < gate_at, (
            f'drop_process.js 的 dragenter 里 `{probe}`（{what}）排在 '
            f'{"show()" if show_at < prevent_at else "preventDefault()"} 之后 —— '
            '守卫排在接管动作后面等于没守卫：遮罩已经画上去/投放已经被允许了'
        )
