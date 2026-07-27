"""style.css 结构契约测试。

这些是**文本级**断言：它们守住 CSS 源码的形态（哪条规则声明了什么字号、
有没有人用 !important 重新覆盖），**守不住**「渲染出来好不好看」——后者
由 docs/images/phase2-baseline/ 的截图 + 计算值对拍覆盖。

为什么需要这些断言：style.css 曾经有一整块「统一字体大小系统」，用
!important 重新声明前面已定义过的选择器（.form-label 在 :902 是 .9rem、
在 :1338 变 .875rem!important）。后果是改前面的规则不生效。本文件的核心
断言就是防止这种自我覆盖的形态复活。
"""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

CSS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'static', 'css', 'style.css',
)


def _css():
    with open(CSS_PATH, encoding='utf-8') as f:
        return f.read()


def _norm_selector(sel):
    """空白折叠 + 逗号规范化，让 `.a,\n.b {` 和 `.a, .b {` 视为同一选择器。"""
    sel = re.sub(r'\s+', ' ', sel).strip()
    return re.sub(r'\s*,\s*', ', ', sel)


def _rules_ctx(css):
    """扫描出全部 (选择器, 规则体, 外层 at-rule 上下文)，包含 @media 内部的规则。

    用花括号深度扫描而不是单条正则——正则 `([^{}]+)\\{([^{}]*)\\}` 会被
    @media 的嵌套花括号带偏，漏掉媒体查询里的规则（Phase 1 的教训：
    只匹配第一条 / 只匹配顶层的正则等于静默漏检）。

    第三个元素是该规则所有外层 at-rule 的列表（顶层规则为空列表），
    用于区分「顶层的 `*`」和「@media 里的 `*`」——两者语义完全不同，
    前者是全局重置，后者可能是响应式或无障碍覆盖。
    """
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    out = []
    stack = []
    token = ''
    for ch in css:
        if ch == '{':
            stack.append(_norm_selector(token))
            token = ''
        elif ch == '}':
            sel = stack.pop() if stack else ''
            if sel and not sel.startswith('@'):
                at_ctx = [s for s in stack if s.startswith('@')]
                out.append((sel, token, at_ctx))
            token = ''
        else:
            token += ch
    return out


def _rules(css):
    """扫描出全部 (选择器, 规则体)。见 _rules_ctx。"""
    return [(sel, body) for sel, body, _ctx in _rules_ctx(css)]


def _font_size_decls(body):
    """规则体里的 font-size 声明列表（原样返回值，含可能的 !important）。

    大小写无关：`FONT-SIZE:` 是浏览器认的合法 CSS，漏掉它等于留一个绕过口。
    """
    return [
        m.group(1).strip()
        for m in re.finditer(r'(?<![-\w])font-size\s*:\s*([^;}]+)', body, re.I)
    ]


# `!important` 的合法书写形态：大小写任意，`!` 与关键字之间允许空白。
# 三种都被浏览器接受，只认字面小写 `!important` 会静默漏检。
_IMPORTANT_RE = re.compile(r'!\s*important', re.I)


# --------------------------------------------------------------------------
# 核心断言 1：不再存在「用 !important 声明 font-size」这个形态
# --------------------------------------------------------------------------

def test_no_font_size_uses_important():
    """整个 style.css 里不允许有任何 font-size 带 !important。

    这是本文件最重要的一条。它守的是**形态**而不是某段注释文本：
    原计划写的 `assert '统一字体大小系统' not in css` 只要删掉注释头、
    保留下面全部 !important 规则就能通过，而那些规则正是要消除的东西。
    """
    offenders = []
    for sel, body in _rules(_css()):
        for decl in _font_size_decls(body):
            if _IMPORTANT_RE.search(decl):
                offenders.append(f'{sel} {{ font-size: {decl} }}')
    assert not offenders, (
        '发现用 !important 声明的 font-size —— 这会让后续字号/密度调整改了不生效：\n'
        + '\n'.join('  ' + o for o in offenders)
    )


def test_font_size_override_block_header_removed():
    """「统一字体大小系统」注释头也应随块一起消失（弱断言，仅作补充）。

    单独看这条几乎没有强度（删注释留规则即可通过），真正的守卫是
    test_no_font_size_uses_important。放在这里只是为了让回潮的人看到
    明确的失败信息。
    """
    assert '统一字体大小系统' not in _css(), (
        'style.css 仍有「统一字体大小系统」覆盖块，它会让后续字号改动不生效'
    )


# --------------------------------------------------------------------------
# 核心断言 2：字号确实被合并回了原始规则（存在性契约）
# --------------------------------------------------------------------------

# 选择器 -> 期望的 font-size 值。值取自被删除的覆盖块（那才是当前实际生效的）。
# 只删块不合并 = 页面字号集体回落到 Bootstrap 默认，这张表就是防这个的。
#
# ⚠️ 给后续任务的说明（这条测试变红时先读这里）：
#   本表钉的是「C1 合并当时的值」，用途是防止合并漏条，**不是**禁止后续改字号。
#   Phase 2 后面的任务会**有意**改动其中一些值 —— 例如 A5（密度收紧）会改
#   `.form-control, .form-select`，A7（文字层级）可能改分组标题相关的字号。
#   那属于正常的视觉改动，**改的时候同步更新本表即可**。
#
#   另外：键是按 `_norm_selector` 规范化后的**精确字符串**匹配的，所以
#   `.form-control, .form-select` 的分组与顺序是承重的。若你把这条规则拆成
#   两条、或调换顺序，也要同步改本表，否则会报「没有任何规则声明 font-size」
#   —— 那不是漏条，是选择器写法变了。
MERGED_FONT_SIZES = {
    '.navbar-brand': 'var(--font-size-xl)',
    '.nav-link': 'var(--font-size-base)',
    '.card-header': 'var(--font-size-base)',
    '.card-header h5': 'var(--font-size-base)',
    '.form-label': 'var(--font-size-sm)',
    '.form-control, .form-select': 'var(--font-size-base)',
    '.btn': 'var(--font-size-base)',
    '.btn-sm': 'var(--font-size-sm)',
    '.task-card h6': 'var(--font-size-base)',
    '.task-card .badge': 'var(--font-size-xs)',
    '.task-card .progress-detail': 'var(--font-size-sm)',
    '.table': 'var(--font-size-base)',
    '.table th': 'var(--font-size-sm)',
    '.table small': 'var(--font-size-sm)',
    '.config-section h3': 'var(--font-size-md)',
    '.progress-bar': 'var(--font-size-sm)',
    '.badge': 'var(--font-size-xs)',
    '.status-badge': 'var(--font-size-xs)',
    '.modal-title': 'var(--font-size-lg)',
    '.modal-body': 'var(--font-size-base)',
    '.page-link': 'var(--font-size-sm)',
    '.alert': 'var(--font-size-base)',
    'h3': 'var(--font-size-lg)',
    'h4, h5, h6': 'var(--font-size-base)',
    'small': 'var(--font-size-sm)',
    'code': 'var(--font-size-sm)',
}


def test_every_merged_selector_declares_expected_font_size():
    """覆盖块里的每一条都必须在原始规则里落地，且值与覆盖块一致。

    这条守的是「合并有没有漏条」。漏一条 = 该选择器回落到 Bootstrap 默认
    字号，页面肉眼可见地变形。
    """
    rules = _rules(_css())
    problems = []
    for sel, expected in MERGED_FONT_SIZES.items():
        found = [
            decl
            for rsel, body in rules
            if rsel == sel
            for decl in _font_size_decls(body)
        ]
        if not found:
            problems.append(f'{sel}: 没有任何规则声明 font-size（期望 {expected}）')
        elif len(found) > 1:
            problems.append(f'{sel}: 声明了 {len(found)} 次 font-size {found}，应恰好 1 次')
        elif found[0] != expected:
            problems.append(f'{sel}: font-size 是 {found[0]}，期望 {expected}')
    assert not problems, '字号合并不完整：\n' + '\n'.join('  ' + p for p in problems)


def test_font_size_scale_variables_unchanged():
    """字号刻度变量本身不许被悄悄改。

    上面那张表全部用 var(--font-size-*) 表达，如果有人改了变量的值，
    表还是全绿而页面已经变了。这条把变量值钉住，让上面的断言真正有意义。

    ⚠️ 同样地：「不许**悄悄**改」不等于「不许改」。后续视觉任务若要调整字号
    刻度（例如 A7 收敛字阶、制造层级断层），那是有意的视觉改动，同步更新
    本处期望值即可。这条断言拦的是「改了变量但没人注意到全站字号都变了」。
    """
    css = _css()
    expected = {
        '--font-size-xs': '0.75rem',
        '--font-size-sm': '0.875rem',
        '--font-size-base': '0.9375rem',
        '--font-size-md': '1rem',
        '--font-size-lg': '1.125rem',
        '--font-size-xl': '1.25rem',
    }
    for name, value in expected.items():
        m = re.search(re.escape(name) + r'\s*:\s*([^;]+);', css)
        assert m, f'{name} 未定义'
        assert m.group(1).strip() == value, (
            f'{name} = {m.group(1).strip()}，期望 {value}；'
            '改动字号刻度会同时改变全站字号，属于视觉改动，不能悄悄进行'
        )


# --------------------------------------------------------------------------
# 核心断言 3：!important 总量不许回潮
# --------------------------------------------------------------------------

def test_important_count_under_control():
    """!important 声明总量上界 = 69。

    阈值构成（全部实测，不是估的）：
      Task 2 清理前 92 处
      - 24 处：被删除的「统一字体大小系统」覆盖块里的 font-size !important
      -  1 处：.form-text 的 font-size !important（同一形态，一并清掉；
               它的 color !important 保留，不在本次范围）
      = 67 处（Task 2 后实测）
      -  1 处：Task 3 删掉的 .text-center 的 color !important（布局类不该管颜色）
      = 66 处（Task 3 后实测的真实值）
      + 3 处余量：留给后续任务里个别确实必须压 Bootstrap 的新规则
      = 69

    ⚠️ 棘轮规则（分两种任务，别混用）：

      **清理型任务**（删掉了 !important）：把上界降到「新实测值 + 3」。
      不降的话，前面清出来的空间会被后面的任务悄悄填回去。

      **新增型任务**（确实需要压第三方样式）：允许抬高上界，但必须在本
      docstring 里逐条登记「新增几处、压的是谁、为什么非 !important 不可」。
      抬升本身不是失败，**悄悄抬升**才是。

    已知的计划内新增（这就是上界不能设死的原因）：
      - Leaflet 控件主题化：约 13 处。Leaflet 自带样式的选择器特异度更高
        （如 `.leaflet-touch .leaflet-bar a`），同名属性下我们赢不了。
      - 动画降噪的 prefers-reduced-motion 重置块：约 4 处（这是 W3C 推荐写法）。
      - 进度条 / 滚动条覆盖：约 3 处。
    合计约 20 处，届时上界会被抬到 86 上下。**这不代表清理白做了** —— Task 2/3
    清掉的 26 处是「自我覆盖的死规则」，而这些新增是「压第三方库的必要手段」，
    两者性质不同。

    余下 66 处几乎全是压 Bootstrap 背景/文字色的历史债
    （`background: transparent !important`、`color: ... !important`），
    属于 Phase 2 其他任务的范围，本次不动。

    注意：注释里被剥掉了才计数——否则一句提到 !important 的说明文字就能
    把数字顶上去（本条测试自己的实现就踩过这个坑）。
    """
    css = re.sub(r'/\*.*?\*/', '', _css(), flags=re.S)
    count = css.count('!important')
    assert count <= 69, (
        f'!important 声明有 {count} 处，应 <= 69（Task 2 前 92 → Task 2 后 67 → '
        'Task 3 后实测 66，余量 3）'
    )


# --------------------------------------------------------------------------
# C1 / Task 3：死代码、重复规则、别名污染
# --------------------------------------------------------------------------

def _selector_parts(sel):
    """规范化后的选择器按逗号拆成各个分支。"""
    return [p.strip() for p in sel.split(',') if p.strip()]


def _decl_map(body):
    """规则体 -> {属性名(小写): 值}，同名属性取最后一次声明。"""
    out = {}
    for chunk in body.split(';'):
        if ':' not in chunk:
            continue
        name, _, value = chunk.partition(':')
        name = name.strip().lower()
        if name:
            out[name] = value.strip()
    return out


# 合并后那条 `*` 规则必须携带的声明。
#
# 为什么需要这张表：单看「`*` 规则只有 1 条」是可以靠**删掉另外两条**来满足的
# —— 那样滚动条配色和全局过渡会一起消失，测试却全绿。这张表把「合并」和
# 「删除」区分开。值取自合并前的三条原始规则，本次只搬不改。
#
# ⚠️ 给后续任务的说明（这条测试变红时先读这里）：
#   本表钉的是「C1 合并当时的值」，用途是防止合并时漏搬，**不是**禁止后续改动。
#   已知的计划内改动：动画降噪任务会**有意删掉** `transition-property` /
#   `transition-duration`（把全局过渡改成只给交互元素加）。那属于正常的视觉改动
#   —— 删的时候把本表对应条目一并移除即可，不要按报错提示去「恢复漏搬的声明」。
MERGED_UNIVERSAL_DECLS = {
    'box-sizing': 'border-box',
    'scrollbar-width': 'thin',
    'scrollbar-color': 'var(--color-accent-strong) var(--color-bg-secondary)',
    'transition-property': 'background-color, border-color, color, fill, stroke',
    'transition-duration': '0.3s',
    'transition-timing-function': 'ease',
}


def test_universal_selector_declared_exactly_once():
    """全站只允许一条裸 `*` 规则，且它必须带齐合并进来的全部声明。

    强度说明（计划原文给的是 `re.findall(r'^\\*\\s*\\{', css, re.M)`）：
    那条正则只匹配**行首**的 `*`，写成 `  * {`（缩进）或 `*, *::before {`
    （分组）就漏了，@media 里的更是完全看不见。这里改用 _rules() 的花括号
    深度扫描 + 逗号拆分，任何位置、任何缩进、任何分组里的裸 `*` 都算一条。
    """
    universal = [
        (sel, body)
        for sel, body, at_ctx in _rules_ctx(_css())
        if '*' in _selector_parts(sel) and not at_ctx
    ]
    assert len(universal) == 1, (
        f'发现 {len(universal)} 条**顶层**裸 `*` 规则，应合并为 1 条：'
        + '; '.join(sel for sel, _ in universal)
    )
    decls = _decl_map(universal[0][1])
    problems = []
    for name, expected in MERGED_UNIVERSAL_DECLS.items():
        actual = decls.get(name)
        if actual is None:
            problems.append(f'{name}: 缺失（期望 {expected}）——合并时漏搬或被误删')
        elif re.sub(r'\s+', ' ', actual) != expected:
            problems.append(f'{name}: 是 {actual!r}，期望 {expected!r}')
    assert not problems, '合并后的 `*` 规则声明不完整：\n' + '\n'.join('  ' + p for p in problems)


FAKE_COLOR_ALIASES = ('--color-accent-amber', '--color-accent-warm', '--color-accent-copper')


def _frontend_files():
    """static/ 与 templates/ 下的全部文本文件。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for sub in ('static', 'templates'):
        for dirpath, _dirnames, filenames in os.walk(os.path.join(root, sub)):
            for fn in sorted(filenames):
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, encoding='utf-8') as f:
                        yield os.path.relpath(path, root), f.read()
                except (UnicodeDecodeError, OSError):
                    continue


def test_no_fake_color_aliases_anywhere_in_frontend():
    """amber/warm/copper 三个别名全指向青绿，是误导性死名——全前端零出现。

    强度说明（计划原文只断言 `alias not in css`）：只查 style.css 的话，
    「CSS 里删了定义、JS 内联样式还在 var(--color-accent-warm)」这种情况
    会让变量变成 undefined、颜色静默丢失，而测试全绿。实测该别名在
    static/js/history.js 和 static/js/map.js 里各有若干引用点，光查 CSS
    确实抓不到。所以这里扫 static/ + templates/ 下**所有**文件。
    """
    files = list(_frontend_files())
    scanned = {rel.replace('\\', '/') for rel, _ in files}
    # 自检：光断言「至少有一个文件含 --color-accent」是不够的——单靠 style.css
    # 就能满足，而 JS 因编码异常被静默跳过（_frontend_files 的 except 会吞掉）
    # 恰恰是这条断言存在的理由。所以点名要求这几个已知含引用的文件都被扫到。
    for must in ('static/css/style.css', 'static/js/map.js', 'static/js/history.js'):
        assert must in scanned, (
            f'{must} 没被扫到（可能因编码异常被跳过），本测试已失效——'
            f'实际扫到 {len(scanned)} 个文件'
        )
    offenders = []
    for rel, text in files:
        for lineno, line in enumerate(text.splitlines(), 1):
            for alias in FAKE_COLOR_ALIASES:
                if alias in line:
                    offenders.append(f'{rel}:{lineno} {alias}')
    assert not offenders, (
        '发现指向青绿的假别名（应换成真实语义名 '
        '--color-accent / --color-accent-hover / --color-accent-strong）：\n'
        + '\n'.join('  ' + o for o in offenders)
    )


def test_text_center_declares_no_color():
    """纯布局类 .text-center 不该管颜色。

    强度说明（计划原文用 `re.search` + `if match:`）：`re.search` 只返回
    第一个匹配——实测 style.css 里 `.text-center` 有两条规则，第二条会被
    漏检；而正则一旦失配（选择器写成 `.text-center, .foo {`）`match` 为
    None，`if match:` 直接跳过，测试变成**永真**。这里遍历全部规则、把
    分组/后代选择器里的 .text-center 也算上，并先断言至少匹配到一条。
    """
    matched = [
        (sel, body)
        for sel, body in _rules(_css())
        if re.search(r'\.text-center(?![-\w])', sel)
    ]
    assert matched, '没有匹配到任何 .text-center 规则——选择器写法变了，本测试已失效'
    offenders = [
        f'{sel} {{ color: {_decl_map(body)["color"]} }}'
        for sel, body in matched
        if 'color' in _decl_map(body)
    ]
    assert not offenders, (
        '.text-center 是布局类，不该设 color：\n' + '\n'.join('  ' + o for o in offenders)
    )


def test_dead_rules_removed():
    """三处已确认零引用的死代码不许回来。

    - `--shadow-glow`：定义了 none 之后全库没有任何 var() 引用它
    - `.leaflet-control-layers-toggle`：map.js 从未调用 L.control.layers，
      该 DOM 元素不存在
    - 重复的 `.mb-3`：第二条被前一条的 !important 完全压死，改它没有效果
    """
    # 先剥注释：说明「为什么删掉了它」的注释里必然要提到这些名字，
    # 拿原文匹配会把解释性注释当成回潮（本条测试自己就先踩了一次）。
    css = re.sub(r'/\*.*?\*/', '', _css(), flags=re.S)
    assert '--shadow-glow' not in css, '--shadow-glow 零引用，应已删除'
    assert 'leaflet-control-layers' not in css, (
        '.leaflet-control-layers-* 对应的 DOM 从不存在（未调用 L.control.layers），应已删除'
    )
    mb3 = [(sel, body) for sel, body in _rules(css) if '.mb-3' in _selector_parts(sel)]
    assert len(mb3) == 1, f'.mb-3 有 {len(mb3)} 条规则，重复的那条是死代码，应只剩 1 条'
    assert 'margin-bottom' in _decl_map(mb3[0][1]), '.mb-3 必须仍然声明 margin-bottom'


# --------------------------------------------------------------------------
# C1 / Task 4：select 下拉箭头
# --------------------------------------------------------------------------

def _form_select_rules(css):
    """全部「选择器里出现 .form-select」的规则，含 @media 内、:focus、后代、分组。

    用 _rules() 的花括号深度扫描 + 逗号拆分，而不是
    `re.finditer(r'\\.form-select[^{]*\\{([^}]*)\\}', css)`：后者只能匹配
    `.form-select` **打头**的选择器，实测漏掉 `.config-section .form-select`
    与 `.form-control, .form-select` 这两种写法——而 style.css 里 4 条规则
    有 3 条是这两种形态。负向断言配上漏检的匹配器，就是一条永真测试。
    """
    return [
        (sel, body)
        for sel, body in _rules(css)
        if any(re.search(r'\.form-select(?![-\w])', part) for part in _selector_parts(sel))
    ]


# 选择器 -> 它必须用 background-color 声明的值。
#
# 这张表同时承担两件事：
#   1. 存在性契约——`.form-select` 必须仍然显式声明背景色。光禁止 background
#      简写的话，「把整条声明删掉」也能通过，而那样背景色会回落到 Bootstrap
#      的 `--bs-body-bg`（实测 #fff）——深色面板上开一块纯白，比丢箭头更糟。
#   2. 有效性自检——四条都找不到就说明选择器写法变了，测试已失效。
#
# ⚠️ 给后续任务的说明：本表钉的是「Task 4 当时的值」，不是禁止后续改配色。
#    A5（密度）/ A7（层级）若要调整表单配色，同步更新本表即可。
FORM_SELECT_BG_COLORS = {
    '.form-control, .form-select':
        'var(--color-bg-tertiary)',
    '.form-control:focus, .form-select:focus':
        'var(--color-bg-secondary)',
    '.config-section .form-control, .config-section .form-select':
        'var(--color-bg-tertiary)',
    '.config-section .form-control:focus, .config-section .form-select:focus':
        'var(--color-bg-secondary)',
}


def test_form_select_never_uses_background_shorthand():
    """任何命中 .form-select 的规则都不许用 `background:` 简写。

    `background` 是简写属性，写一次会把 background-image / -position / -size /
    -repeat / -attachment / -origin / -clip 全部重置成初始值。Bootstrap 的
    下拉箭头正是靠这四个子属性画出来的：

        background-image:    var(--bs-form-select-bg-img)   ← 箭头 SVG
        background-repeat:   no-repeat                      ← 不平铺
        background-position: right .75rem center            ← 贴右侧居中
        background-size:     16px 12px                      ← 缩到 16x12

    所以 `background: <颜色>` 一写，四个全没了，箭头消失。实测清理前
    getComputedStyle(select) 是 backgroundImage:"none" / repeat:"repeat" /
    position:"0% 0%" / size:"auto" —— 四项全被重置，可见不是只丢了图。
    改用 background-color 只覆盖颜色，其余四项让 Bootstrap 的值生效。
    """
    rules = _form_select_rules(_css())
    assert rules, (
        '没有匹配到任何 .form-select 规则——选择器写法变了，本测试已失效'
    )
    offenders = [
        f'{sel} {{ background: {_decl_map(body)["background"]} }}'
        for sel, body in rules
        if 'background' in _decl_map(body)
    ]
    assert not offenders, (
        '.form-select 用了 background 简写，会连带重置 Bootstrap 下拉箭头的\n'
        'background-image/-repeat/-position/-size，导致 select 没有三角指示符。\n'
        '改用 background-color：\n'
        + '\n'.join('  ' + o for o in offenders)
    )


def test_form_select_still_declares_its_background_color():
    """四条规则都必须仍显式声明 background-color，且值不变。

    见 FORM_SELECT_BG_COLORS 的注释：这是与上一条配对的存在性契约，
    防止「用删声明的方式让禁止性断言变绿」。
    """
    rules = _form_select_rules(_css())
    problems = []
    for sel, expected in FORM_SELECT_BG_COLORS.items():
        found = [_decl_map(body).get('background-color')
                 for rsel, body in rules if rsel == sel]
        if not found:
            problems.append(f'{sel}: 找不到这条规则（选择器写法变了？期望 background-color: {expected}）')
        elif len(found) > 1:
            problems.append(f'{sel}: 有 {len(found)} 条同名规则，应恰好 1 条')
        elif found[0] is None:
            problems.append(f'{sel}: 没有声明 background-color（期望 {expected}）'
                            '——背景色会回落到 Bootstrap 的 --bs-body-bg(#fff)')
        elif found[0] != expected:
            problems.append(f'{sel}: background-color 是 {found[0]}，期望 {expected}')
    assert not problems, (
        '.form-select 的背景色契约被破坏：\n' + '\n'.join('  ' + p for p in problems)
    )


def _hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _relative_luminance(rgb):
    """WCAG 2.x 相对亮度。"""
    chan = []
    for c in rgb:
        c = c / 255
        chan.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = chan
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(hex_a, hex_b):
    la, lb = _relative_luminance(_hex_to_rgb(hex_a)), _relative_luminance(_hex_to_rgb(hex_b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _palette_var(css, name):
    m = re.search(re.escape(name) + r'\s*:\s*([^;]+);', css)
    assert m, f'{name} 未定义——本测试已失效'
    return m.group(1).strip().lower()


def _arrow_stroke_hex(css):
    """`.form-select` 覆盖的 --bs-form-select-bg-img 里那个 SVG 描边色。"""
    imgs = [
        (sel, _decl_map(body)['--bs-form-select-bg-img'])
        for sel, body in _form_select_rules(css)
        if '--bs-form-select-bg-img' in _decl_map(body)
    ]
    assert len(imgs) == 1, (
        f'期望恰好 1 条规则覆盖 --bs-form-select-bg-img，实际 {len(imgs)} 条'
        f'（{[s for s, _ in imgs]}）——没有的话箭头会用回 Bootstrap 浅色主题的 '
        '#343a40，深色面板上对比度 1.42:1，等于看不见'
    )
    m = re.search(r"stroke='%23([0-9a-fA-F]{6})'", imgs[0][1])
    assert m, (
        '在 --bs-form-select-bg-img 的 data URI 里找不到 '
        "stroke='%23xxxxxx' —— 写法变了，本测试已失效"
    )
    return '#' + m.group(1).lower()


def test_form_select_arrow_stroke_matches_palette():
    """箭头描边色必须字面等于 --color-text-secondary。

    为什么要单独钉一条：data URI 里不能写 var()，箭头颜色只能硬编码。
    硬编码 + 调色板变量并存 = 典型的静默漂移点——有人改了
    --color-text-secondary，箭头还是老颜色，没人会发现。这条把两者绑死。
    """
    css = _css()
    stroke = _arrow_stroke_hex(css)
    expected = _palette_var(css, '--color-text-secondary')
    assert stroke == expected, (
        f'下拉箭头描边色是 {stroke}，但 --color-text-secondary 是 {expected}。\n'
        'data URI 里不能用 var()，所以两处必须手工保持一致；'
        '改调色板时请同步改 .form-select 的 --bs-form-select-bg-img。'
    )


def test_form_select_arrow_has_sufficient_contrast():
    """箭头描边对面板底色的对比度必须 >= 3:1（WCAG 图形元素下限）。

    这条守的是**渲染出来看不看得见**，不是「代码里写了这个字符串」——
    两个颜色都在 CSS 里，对比度可以纯文本算出来，所以它是本文件里少数
    真正守住视觉结果的断言之一。

    它与上一条互补：上一条只保证「箭头色 == 调色板的次级文字色」，
    如果哪天次级文字色本身被调暗，上一条仍全绿而箭头重新消失；
    这条会拦住。

    实测记录（Task 4）：
      修复前 Bootstrap 默认 #343a40 vs #1c2027 = 1.42:1（不可见）
      修复后 #9aa0aa           vs #1c2027 = 6.21:1
    """
    css = _css()
    stroke = _arrow_stroke_hex(css)
    panel = _palette_var(css, '--color-bg-tertiary')
    ratio = _contrast_ratio(stroke, panel)
    assert ratio >= 3.0, (
        f'下拉箭头 {stroke} 对面板底色 {panel} 的对比度只有 {ratio:.2f}:1，'
        '低于 WCAG 图形元素 3:1 的下限——箭头会"在但看不见"，等于没修'
    )


def test_pulse_keyframe_has_no_offpalette_hardcoded_color():
    """pulse 关键帧不许再硬编码调色板外的蓝色发光。

    背景：`--shadow-glow` 被设成 none 并注上「去发光」，但 pulse 关键帧里
    还留着 `rgba(59,130,246,.3)` 的蓝色光晕——去发光只去了一半，而且
    #3b82f6 不在当前调色板里。
    """
    m = re.search(r'@keyframes\s+pulse\s*\{(.*?)\n\}', _css(), re.S)
    assert m, '找不到 @keyframes pulse——本测试已失效'
    body = m.group(1)
    assert '59, 130, 246' not in body and '59,130,246' not in body, (
        'pulse 关键帧仍硬编码调色板外的蓝色 rgba(59,130,246,...)'
    )
    assert 'var(--color-accent-muted)' in body, (
        'pulse 的发光应改用品牌色柔和版 var(--color-accent-muted)'
    )
