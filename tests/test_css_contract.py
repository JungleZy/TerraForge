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
from html.parser import HTMLParser

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
    # A2 / Task 7 把这一条从 `.progress-bar` **搬到** `.progress__label`：
    # 百分比数字不再是进度条自己的子元素了，条里一个字都没有，给一个空元素
    # 声明字号是死代码。值原样不变（0.875rem），承载它的元素换了个。
    '.progress__label': 'var(--font-size-sm)',
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
    """!important 声明总量上界 = 71.

    阈值构成（全部实测，不是估的）：
      Task 2 清理前 92 处
      - 24 处：被删除的「统一字体大小系统」覆盖块里的 font-size !important
      -  1 处：.form-text 的 font-size !important（同一形态，一并清掉；
               它的 color !important 保留，不在本次范围）
      = 67 处（Task 2 后实测）
      -  1 处：Task 3 删掉的 .text-center 的 color !important（布局类不该管颜色）
      = 66 处（Task 3 后实测）
      +  2 处：**A1 / Task 5 新增**，登记如下——
               .progress-bar.bg-secondary / .bg-dark 各 1 处。
               压的是 Bootstrap 5.3.0 的
               `.bg-secondary{...background-color:...!important}` 等工具类。
               为什么非 !important 不可：important 声明之间先比来源、再比特异性，
               我们的 `.progress-bar.bg-secondary`(0,2,0) 若不带 !important，
               就输给带 !important 的 `.bg-secondary`(0,1,0)，颜色仍是 Bootstrap 的。
               （运行中不占额度：getStatusColor 把 running 映射成 'info'，
               复用早就存在的 `.progress-bar.bg-info`，新增 0 条。若当初按
               计划映射成 'primary'，这里就要多写一条、多占 1 处。）
      = 68 处（Task 5 后实测的真实值）
      + 3 处余量：留给后续任务里个别确实必须压 Bootstrap 的新规则
      = 71

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
    assert count <= 71, (
        f'!important 声明有 {count} 处，应 <= 71（Task 2 前 92 → Task 2 后 67 → '
        'Task 3 后 66 → Task 5 +2 条进度条覆盖后实测 68，余量 3）'
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

    用 _rules() 的花括号深度扫描 + 逗号拆分，而不是计划原文给的
    `re.finditer(r'\\.form-select[^{]*\\{([^}]*)\\}', css)`。

    ⚠️ 订正（原注释写错过，别再照抄）：那条正则**不存在**「只匹配 .form-select
    打头的选择器」的问题。`re.finditer` 会从每一处 `.form-select` 起头，`[^{]*`
    再吞掉选择器剩余部分直到 `{`，没有任何行首锚点——实测拿它跑基线 CSS
    （`git show 14ad54031:static/css/style.css`）4 条规则全部命中、内层断言
    触发 4 次。换掉它的真实理由是下面三条：

      1. 零匹配时永真。没有 `assert matches`，正则一旦完全失配，for 循环不执行，
         负向断言直接绿。（这是 p2-assertion-review.md 的 H 条。）
      2. 缺词边界，会误吃无关选择器。实测 `.form-select-lg { background: red }`
         和 `.form-selected { background: red }` 都会被判成违规——而它们没有
         Bootstrap 箭头，用 background 简写完全无害。假阳性。
      3. 不剥注释。实测 `/* 别再给 .form-select 写 { background: red } 了 */`
         这样一句说明文字会被当成一条真规则匹配上并报违规。假阳性。
         （本文件其它断言已经踩过这个坑两次，见 test_dead_rules_removed。）

    _rules() 在同样这三个输入上分别是：0 条、0 条、1 条（只命中真规则），
    且 `(?![-\\w])` 词边界挡住了 -lg / -ed 后缀。
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


# `background-image` 写成这些值，计算值都是 none，箭头都会消失。
# 只认字面 `none` 会漏掉后四种（实测 initial / unset 在浏览器里确实让箭头消失）。
_ARROW_KILLING_IMAGE_VALUES = frozenset({
    'none', 'initial', 'unset', 'revert', 'revert-layer',
})


def test_form_select_never_uses_background_shorthand():
    """任何命中 .form-select 的规则都不许用 `background:` 简写，也不许把
    `background-image` 置为 none。

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

    第二条（`background-image` 的计算值为 none）是补漏：只禁简写的话，直接写
    `.form-select { background-image: none }` 照样能让箭头消失，而其余
    断言全绿——禁止性契约留了个正门。

    禁的是**一组等价写法**而不是字面量 `none`，因为下面这些的计算值都是 none、
    箭头都会消失（见 _ARROW_KILLING_IMAGE_VALUES）：
      none / initial / unset / revert / revert-layer
    `background-image` 是非继承属性，所以 `unset` == `initial` == `none`；
    `revert` 会退回作者层之前的来源，Bootstrap 的声明同属作者层，一并被撤掉。
    """
    rules = _form_select_rules(_css())
    assert rules, (
        '没有匹配到任何 .form-select 规则——选择器写法变了，本测试已失效'
    )
    offenders = []
    for sel, body in rules:
        decls = _decl_map(body)
        if 'background' in decls:
            offenders.append(
                f'{sel} {{ background: {decls["background"]} }}  ← 简写会重置全部子属性'
            )
        img = decls.get('background-image')
        if img is not None:
            # 用 _IMPORTANT_RE 剥 !important：它认 `!important` / `! important` /
            # 任意大小写三种写法。原来这里写的是 `.rstrip('!important')`——
            # str.rstrip 是**字符集**剥离不是后缀剥离，既漏掉 `none ! important`
            # （剥完是 'none !'），又会误伤别的值（`'inherit'.rstrip('!important')`
            # 实测得到 `'inhe'`）。
            value = _IMPORTANT_RE.sub('', img).strip().lower()
            if value in _ARROW_KILLING_IMAGE_VALUES:
                offenders.append(
                    f'{sel} {{ background-image: {img} }}  ← 计算值为 none，直接把箭头图去掉了'
                )
    assert not offenders, (
        '.form-select 的背景写法会让 Bootstrap 下拉箭头消失\n'
        '（简写 background 会连带重置 background-image/-repeat/-position/-size；\n'
        ' background-image:none 则是直接去掉箭头图）。请改用 background-color：\n'
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


ARROW_MIN_PADDING_RIGHT_PX = 28   # = 12px 右偏移 + 16px 图标宽，见下方 docstring


def _length_to_px(value):
    """`2.25rem` / `36px` -> 36.0。看不懂的（calc()/em/%/var()）返回 None。

    返回 None 会让调用方报「本测试已失效」而不是静默通过——响亮失败优于
    默默放行（这是 Phase 1 反复出现的教训）。
    """
    m = re.match(r'^([\d.]+)(px|rem)$', value.strip().lower())
    if not m:
        return None
    return float(m.group(1)) * (16 if m.group(2) == 'rem' else 1)


def _right_padding_in_body(body):
    """按声明先后顺序求值，返回该规则体设定的右内边距 (值, 是否!important)。

    **必须同时认 `padding` 简写和 `padding-right` 长写**，且要处理同一规则里
    两者的先后覆盖——不能用 _decl_map（它丢掉了不同属性名之间的顺序）。

    `padding` 简写取右分量：1 值 -> 该值；2/3/4 值 -> 第 2 个。
    """
    out = None
    for chunk in body.split(';'):
        if ':' not in chunk:
            continue
        name, _, raw = chunk.partition(':')
        name = name.strip().lower()
        raw = raw.strip()
        important = bool(_IMPORTANT_RE.search(raw))
        val = _IMPORTANT_RE.sub('', raw).strip()
        if name == 'padding':
            parts = val.split()
            if len(parts) == 1:
                out = (parts[0], important)
            elif 2 <= len(parts) <= 4:
                out = (parts[1], important)
        elif name == 'padding-right':
            out = (val, important)
    return out


def _branch_applies(branch, ancestor_classes, element_classes, focused):
    """这个选择器分支是否作用于「给定祖先类 + 给定元素类 + 焦点态」的元素？

    只支持「后代组合符 + 类选择器 + :focus」——本文件涉及 .form-select 的
    选择器全是这个形态。遇到子/兄弟组合符、id、属性选择器、其它伪类一律
    返回 None，调用方据此报「本测试已失效」，绝不当成"不匹配"放过去。
    """
    if re.search(r'[>+~#\[*]', branch):
        return None
    compounds = []
    for part in branch.split():
        pseudos = re.findall(r':{1,2}([-\w]+)', part)
        if any(p != 'focus' for p in pseudos):
            return None
        if re.sub(r'(\.[-\w]+|:{1,2}[-\w]+)', '', part).strip():
            return None            # 还剩下东西 = 元素选择器等，不支持
        compounds.append((set(re.findall(r'\.([-\w]+)', part)), 'focus' in pseudos))
    if not compounds:
        return None
    target_classes, target_focus = compounds[-1]
    if target_focus and not focused:
        return False
    if not target_classes <= element_classes:
        return False
    for cls, foc in compounds[:-1]:
        if foc or not cls <= ancestor_classes:
            return False
    return True


def _branch_specificity(branch):
    """(类 + 伪类) 计数。id / 元素选择器已被 _branch_applies 拒绝，
    所以类计数就足以排序。"""
    return len(re.findall(r'\.[-\w]+', branch)) + len(re.findall(r':{1,2}[-\w]+', branch))


# 页面里真实存在的两种 select 上下文（`grep -rn "form-select" templates/` 确认）。
FORM_SELECT_CONTEXTS = {
    '首页 .form-select': frozenset(),
    '配置页 .config-section .form-select': frozenset({'config-section'}),
}


def test_form_select_reserves_room_for_the_arrow():
    """每一种 select 上下文里，**最后生效**的右内边距都必须 >= 28px。

    几何依据（取自 Bootstrap 5.3.0 的 `.form-select`，已核对 CDN 源码）：
        background-position: right .75rem center   → 箭头右缘距 padding 框右缘 12px
        background-size:     16px 12px             → 箭头宽 16px
      ⇒ 箭头占据「右起 12px ~ 28px」，右内边距至少 28px 才不叠字。
      Bootstrap 自己用 2.25rem(36px)，比下限多 8px 呼吸位。

    **这个下限是与视口无关的充分条件，不是抽样结论。** 当选项文字长到被裁切时，
    文字右缘恰好落在内容框右缘，即距控件右缘 `padding-right`；而箭头左缘距控件
    右缘恒为 28px。于是余量恒等于 `padding-right - 28`，**与视口宽度、字体度量、
    文案长度全部无关**。所以只要本断言为真，就不存在能触发重叠的视口；文字没被
    裁切时余量只会更大。（Task 4 首版靠抽 7 个视口来"证明"不重叠，那才是抽样；
    这条是证明。）

    为什么需要这条：本站 `.form-control, .form-select` 的
    `padding: 0.6rem 0.85rem` 把右内边距压到 13.6px。箭头丢失期间这不构成问题
    （没有箭头就不会叠印），**箭头一修好碰撞立刻出现**——Task 4 首版就漏了，
    实测 900px 视口下 #downloadType 重叠 14.4px、末尾字符被切断。

    ⚠️ 本条为什么要模拟层叠，而不是只查 `padding-right` 声明在不在：
    因为**简写会静默覆盖长写**——这正是本任务存在的原因，在同一个文件里的复刻。
    只查 `'padding-right' in decls` 的话，追加一条
    `.form-select { padding: 0.4rem 0.7rem }`（右内边距 11.2px）测试仍然全绿，
    因为那句 padding-right 的字符串还在文件里。实测发现该漏洞时，
    `.config-section` 那条 16px 的规则已经在违反下限，而测试是绿的。

    ⚠️ 给 Task 10（密度收紧）的说明：可以改这些 padding 值，但任一上下文的
    **有效**右内边距不得低于 28px——除非同时改掉 Bootstrap 的箭头位置/尺寸。
    另外本断言只认 `px` / `rem` 字面量；写成 `calc()` / `em` / `var()` 会报
    「本测试已失效」而不是通过（响亮失败优于静默放行）。真要用 calc()，
    请连同本断言的解析逻辑一起改。
    """
    css = _css()
    all_rules = _rules(css)          # 保持源码顺序，层叠要用
    assert all_rules, 'CSS 解析不出任何规则——本测试已失效'

    unsupported, problems = [], []
    checked = 0
    for ctx_name, ancestors in FORM_SELECT_CONTEXTS.items():
        for focused in (False, True):
            candidates = []
            for order, (sel, body) in enumerate(all_rules):
                rp = _right_padding_in_body(body)
                if rp is None:
                    continue
                for branch in _selector_parts(sel):
                    if not re.search(r'\.form-select(?![-\w])', branch):
                        continue
                    applies = _branch_applies(branch, ancestors, {'form-select'}, focused)
                    if applies is None:
                        unsupported.append(f'{sel}   （分支 {branch!r} 形态不支持）')
                    elif applies:
                        candidates.append(
                            (rp[1], _branch_specificity(branch), order, sel, rp[0])
                        )
            state = ':focus' if focused else '常态'
            if not candidates:
                problems.append(
                    f'{ctx_name}[{state}]: 没有任何规则给它设右内边距——'
                    'Bootstrap 的 padding-right:2.25rem 若也没生效，箭头就会压字'
                )
                continue
            _imp, _spec, _order, sel, value = max(candidates)
            px = _length_to_px(value)
            if px is None:
                unsupported.append(
                    f'{ctx_name}[{state}]: 胜出的 `{sel}` 右内边距是 {value!r}，'
                    '不是 px/rem 字面量，解析不了'
                )
                continue
            checked += 1
            if px < ARROW_MIN_PADDING_RIGHT_PX:
                problems.append(
                    f'{ctx_name}[{state}]: 最终生效的右内边距 {px:g}px < '
                    f'{ARROW_MIN_PADDING_RIGHT_PX}px 下限（来自 `{sel}` 的 {value}）'
                    f'——选项文字会压在下拉箭头上，叠印 '
                    f'{ARROW_MIN_PADDING_RIGHT_PX - px:g}px'
                )
    assert not unsupported, (
        '出现本断言的层叠模型处理不了的写法，测试已失效（不是通过）：\n'
        + '\n'.join('  ' + u for u in unsupported)
    )
    assert checked == len(FORM_SELECT_CONTEXTS) * 2, (
        f'只算出 {checked} 个「上下文×状态」的有效内边距，'
        f'期望 {len(FORM_SELECT_CONTEXTS) * 2} 个——本测试已失效'
    )
    assert not problems, '.form-select 没给箭头留够位置：\n' + '\n'.join('  ' + p for p in problems)


# ⚠️ 这里原本有 test_form_select_arrow_stroke_matches_palette 和
# test_form_select_arrow_has_sufficient_contrast 两条断言，守的是 style.css 里
# `.form-select { --bs-form-select-bg-img: ...stroke='%239aa0aa'... }` 那条硬编码
# 覆盖：它把 Bootstrap 浅色主题写死在 data URI 里的 #343a40 箭头（对面板底
# #1c2027 只有 1.42:1，等于看不见）换成调色板的 #9aa0aa（6.21:1）。
#
# A3 / Task 8 给 <html> 加上 data-bs-theme="dark" 之后，那条站内覆盖成了死代码：
# Bootstrap 自带的 `[data-bs-theme=dark] .form-select`（特异性 0,2,0）压过站内的
# `.form-select`（0,1,0），箭头改用它的 #adb5bd —— 对 #1c2027 实算 7.87:1，
# 比站内那版还高。所以站内覆盖连同这两条断言一并删除（删除方案与理由写在
# 两条断言原本的 docstring 里，是 C1/Task 4 留给 Task 8 的交接）。
#
# 箭头**存在**这件事仍然守着：test_form_select_never_uses_background_shorthand
# 禁止 `background:` 简写与 `background-image: none` 一族写法，
# test_form_select_reserves_room_for_the_arrow 守住 28px 的几何让位。
# 箭头**颜色**改由 Bootstrap 深色主题提供，站内不再有可漂移的硬编码色号，
# 所以不需要（也无法）用文本断言去钉它 —— 实测值记在 p2-task-8-report.md。


# --------------------------------------------------------------------------
# A1 / Task 5：进度条状态色
# --------------------------------------------------------------------------

_JS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'js'
)


def _js(name):
    with open(os.path.join(_JS_DIR, name), encoding='utf-8') as f:
        return f.read()


def _js_function_body(src, name):
    """按花括号配对切出 `function <name>(...)` 的函数体。

    不用「下一个函数名的 index」做切片终点——那依赖函数在文件里的先后顺序，
    顺序一调 `end < start`，切片返回空串，负向断言集体永真
    （p2-assertion-review.md 的 E 条）。这里配对花括号，与顺序无关。
    """
    m = re.search(r'function\s+' + re.escape(name) + r'\s*\(', src)
    assert m, f'找不到 function {name}( —— 本测试已失效'
    start = src.index('{', m.end() - 1)
    depth = 0
    for j in range(start, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[start + 1:j]
    raise AssertionError(f'function {name} 花括号不配对 —— 本测试已失效')


def _status_color_names(js_name):
    """某个 JS 文件里 getStatusColor 可能返回的全部 Bootstrap 颜色名。

    直接从 JS 源码解析（映射表的值 + `|| 'xxx'` 兜底），而不是在测试里
    手抄一份清单——手抄的清单会在有人给 getStatusColor 加状态时静默过期，
    新状态没有对应的 .progress-bar 覆盖，进度条回落到 Bootstrap 默认色。
    """
    body = _js_function_body(_js(js_name), 'getStatusColor')
    names = set(re.findall(r":\s*'([a-z]+)'", body))
    fallback = set(re.findall(r"\|\|\s*'([a-z]+)'", body))
    assert names, f'{js_name} 的 getStatusColor 里解析不出颜色名 —— 本测试已失效'
    assert fallback, f"{js_name} 的 getStatusColor 没有 `|| 'xxx'` 兜底 —— 本测试已失效"
    return names | fallback


def _progress_bar_color_rules(css):
    """**顶层**的 `.progress-bar.bg-XXX` 规则 -> {颜色名: [(选择器, 规则体), ...]}。

    ⚠️ 必须用 `_rules_ctx` 并要求 at-rule 上下文为空，不能用 `_rules`。
    实测：把整条 `.progress-bar.bg-info` 包进 `@media (min-width: 3000px)`，
    用 `_rules` 的版本**仍然通过**——规则在文件里，但在任何正常视口下都不生效，
    进度条照样回落到 Bootstrap 默认色。这与 test_universal_selector_declared_exactly_once
    区分「顶层的 `*`」和「@media 里的 `*`」是同一个理由。
    """
    out = {}
    for sel, body, at_ctx in _rules_ctx(css):
        if at_ctx:
            continue
        for part in _selector_parts(sel):
            m = re.fullmatch(r'\.progress-bar\.bg-([a-z]+)', part.strip())
            if m:
                out.setdefault(m.group(1), []).append((sel, body))
    return out


# 进度条填充色对轨道底色的最低对比度。
#
# 3:1 是 WCAG 2.x 对「图形对象与用户界面组件」的下限，也是本项目在 C1/Task 4
# 已经立起来的同一条线（见 test_form_select_arrow_has_sufficient_contrast）。
# 进度条填充是承载状态信息的图形对象，适用同一条下限。
PROGRESS_FILL_MIN_CONTRAST = 3.0


def _progress_track_color(css):
    """`.progress` 轨道底色的字面值（进度条填充色不能等于它，否则等于看不见）。"""
    tracks = [body for sel, body in _rules(css) if '.progress' in _selector_parts(sel)]
    assert len(tracks) == 1, (
        f'期望恰好 1 条裸 `.progress` 规则，实际 {len(tracks)} 条 —— 本测试已失效'
    )
    value = _decl_map(tracks[0]).get('background') or _decl_map(tracks[0]).get('background-color')
    assert value, '`.progress` 没有声明背景色 —— 本测试已失效'
    return _IMPORTANT_RE.sub('', value).strip()


def test_every_status_color_has_a_progress_bar_override():
    """getStatusColor 能返回的**每一个**颜色名都必须有 .progress-bar 覆盖，
    且该覆盖真的声明了背景色、带 !important、用调色板变量、不等于轨道底色。

    强度说明（计划原文给的是 `assert '.progress-bar.bg-primary' in css`）：
    那条只查字符串存在——写一条空规则 `.progress-bar.bg-primary {}` 就能过，
    而进度条颜色仍然回落到 Bootstrap 默认蓝 #0d6efd。这里逐项落地：

      1. **规则存在于顶层且恰好一条**。两条同名规则说明有人在别处又覆盖了一次，
         改前面那条不生效（这正是 Task 2 清掉的那种形态）。
         「顶层」是必须的：实测把整条 `.progress-bar.bg-info` 包进
         `@media (min-width: 3000px)`，用 `_rules()` 的版本**仍然通过**——
         规则在文件里、正常视口下却不生效。改用 `_rules_ctx()` 并要求
         at-rule 上下文为空，见 `_progress_bar_color_rules`。
      2. **规则体里真的有背景色声明**。空规则 / 只写 color 都算失败。
      3. **必须带 !important**。这不是洁癖：Bootstrap 5.3.0 的
         `.bg-info{...background-color:rgba(var(--bs-info-rgb),...)!important}`
         自带 !important，important 声明之间先比来源再比特异性——我们的
         `.progress-bar.bg-info`(0,2,0) 只有同样带 !important 才赢得过
         `.bg-info`(0,1,0)。不带的话规则在、颜色还是 Bootstrap 的。
      4. **值必须是 var(--color-*)**。硬编码色号会在调色板改动时静默漂移。
      5. **解析出的色值不能等于 `.progress` 轨道底色**。计划原文建议
         `.progress-bar.bg-dark { background: var(--color-bg-tertiary) }`，
         而 `.progress` 的底色恰恰就是 var(--color-bg-tertiary)——
         规则在、变量对、测试全绿，而已取消任务的进度条与轨道同色，
         肉眼完全看不见。这一条就是为了拦住这种「写了等于没写」。
         更一般的可辨识度下限见 test_progress_bar_fill_has_sufficient_contrast
         （同色只是对比度 1:1 的极端情形）。

    覆盖范围（诚实说明）：这条守的是「CSS 源码的形态」。它保证不了
    「浏览器最终算出来是什么颜色」——那部分由 CDP 实测覆盖。
    """
    css = _css()
    required = _status_color_names('tasks.js') | _status_color_names('history.js')
    # 自检：running 走 info（复用早就存在的 .progress-bar.bg-info），
    # secondary/dark 是本任务补上的。解析要是出了岔子导致 required 变小，
    # 负向遍历会静默变绿。
    assert {'info', 'secondary', 'dark'} <= required, (
        f'从 getStatusColor 解析出的颜色名是 {sorted(required)}，'
        '缺了 info/secondary/dark —— 解析逻辑已失效'
    )

    rules = _progress_bar_color_rules(css)
    track = _progress_track_color(css)
    problems = []
    for name in sorted(required):
        found = rules.get(name, [])
        if not found:
            problems.append(
                f'.progress-bar.bg-{name}: 没有这条规则 —— '
                f'getStatusColor 会返回 {name!r}，进度条会用 Bootstrap 默认色'
            )
            continue
        if len(found) > 1:
            problems.append(f'.progress-bar.bg-{name}: 有 {len(found)} 条同名规则，应恰好 1 条')
            continue
        decls = _decl_map(found[0][1])
        raw = decls.get('background') or decls.get('background-color')
        if raw is None:
            problems.append(
                f'.progress-bar.bg-{name}: 规则体里没有背景色声明 —— '
                '空规则同样让颜色回落到 Bootstrap 默认色'
            )
            continue
        if not _IMPORTANT_RE.search(raw):
            problems.append(
                f'.progress-bar.bg-{name}: 背景色 {raw!r} 没带 !important —— '
                f'压不过 Bootstrap 的 .bg-{name}{{...!important}}'
            )
        value = _IMPORTANT_RE.sub('', raw).strip()
        m = re.fullmatch(r'var\(\s*(--[-\w]+)\s*\)', value)
        if not m:
            problems.append(
                f'.progress-bar.bg-{name}: 背景色 {value!r} 不是单个 var(--color-*)，'
                '硬编码色号会在调色板改动时静默漂移'
            )
            continue
        if value == track:
            problems.append(
                f'.progress-bar.bg-{name}: 背景色 {value} 与 `.progress` 轨道底色相同，'
                '进度条与轨道同色 = 完全看不见'
            )
        resolved = _palette_var(css, m.group(1))
        track_var = re.fullmatch(r'var\(\s*(--[-\w]+)\s*\)', track)
        if track_var and resolved == _palette_var(css, track_var.group(1)):
            problems.append(
                f'.progress-bar.bg-{name}: {value} 解析出的色值 {resolved} '
                f'与轨道底色相同 = 完全看不见'
            )
    assert not problems, (
        '进度条状态色覆盖不完整：\n' + '\n'.join('  ' + p for p in problems)
    )


def test_progress_bar_fill_has_sufficient_contrast():
    """getStatusColor 能返回的每一个颜色，其进度条填充对轨道底色都必须 >= 3:1。

    这条守的是**渲染出来分不分得清**，不是「代码里写了这个字符串」——两个颜色
    都在 CSS 里，对比度可以纯文本算出来，所以它和
    test_form_select_arrow_has_sufficient_contrast 一样，是本文件里少数真正
    守住视觉结果的断言。

    为什么需要它（实测经过，不是假想）：本任务首版把 bg-secondary 设成
    --color-text-muted(#5f6670)，对轨道 --color-bg-tertiary(#1c2027) 实算
    **2.82:1**，低于本项目自己在 C1/Task 4 刚立起来的 3:1 图形元素下限；
    同批的另外两条是 6.21:1 和 3.94:1，只有它掉在门槛下。

    而 bg-secondary 恰恰是最承重的一格：`static/js/history.js` 的
    getStatusColor 只映射 completed/failed/cancelled，**running / pending /
    paused 全部兜底成 secondary**，而 `/api/history_all` 默认不过滤非终态。
    所以历史页详情里一个跑到 63% 的运行中任务，进度条就是这一格。改前那条
    路径走百分比阶梯渲染 Bootstrap 蓝(#0d6efd 对轨道 3.63:1)，
    也就是说 2.82:1 会让这一格**比修复前更难辨认**。

    它与 test_every_status_color_has_a_progress_bar_override 的第 5 项互补：
    那一项只拦「填充色 == 轨道色」这个极端情形，本条给出一般下限。

    ⚠️ 给后续视觉任务的说明：调色板里的灰只有 --color-text-secondary(6.21:1)
    和 --color-neutral(3.94:1) 过线，--color-text-muted(2.82:1) 不过线。
    要给进度条换灰色之前先在这里算一下。
    """
    css = _css()
    required = _status_color_names('tasks.js') | _status_color_names('history.js')
    rules = _progress_bar_color_rules(css)

    track_raw = _progress_track_color(css)
    track_var = re.fullmatch(r'var\(\s*(--[-\w]+)\s*\)', track_raw)
    assert track_var, f'`.progress` 轨道底色 {track_raw!r} 不是 var(--*) —— 本测试已失效'
    track = _palette_var(css, track_var.group(1))
    assert re.fullmatch(r'#[0-9a-f]{6}', track), (
        f'轨道底色解析成 {track!r}，不是 6 位十六进制，算不了对比度 —— 本测试已失效'
    )

    checked, unsupported, problems = 0, [], []
    for name in sorted(required):
        found = rules.get(name, [])
        if len(found) != 1:
            # 规则缺失/重复由 test_every_status_color_has_a_progress_bar_override
            # 报告，这里只需保证自己不静默跳过
            unsupported.append(f'.progress-bar.bg-{name}: 顶层规则有 {len(found)} 条，期望 1 条')
            continue
        decls = _decl_map(found[0][1])
        raw = decls.get('background') or decls.get('background-color')
        if raw is None:
            unsupported.append(f'.progress-bar.bg-{name}: 没有背景色声明')
            continue
        m = re.fullmatch(r'var\(\s*(--[-\w]+)\s*\)', _IMPORTANT_RE.sub('', raw).strip())
        if not m:
            unsupported.append(f'.progress-bar.bg-{name}: 背景色 {raw!r} 不是 var(--*)')
            continue
        fill = _palette_var(css, m.group(1))
        if not re.fullmatch(r'#[0-9a-f]{6}', fill):
            # 响亮失败优于静默放行：rgba()/color-mix() 需要合成才能算对比度，
            # 本断言不支持，直接报「已失效」而不是当成通过
            unsupported.append(
                f'.progress-bar.bg-{name}: {m.group(1)} = {fill!r}，'
                '不是 6 位十六进制，本断言算不了它的对比度'
            )
            continue
        checked += 1
        ratio = _contrast_ratio(fill, track)
        if ratio < PROGRESS_FILL_MIN_CONTRAST:
            problems.append(
                f'.progress-bar.bg-{name}: {m.group(1)}({fill}) 对轨道 {track} '
                f'只有 {ratio:.2f}:1，低于 {PROGRESS_FILL_MIN_CONTRAST}:1'
            )
    assert not unsupported, (
        '出现本断言处理不了的写法，测试已失效（不是通过）：\n'
        + '\n'.join('  ' + u for u in unsupported)
    )
    assert checked == len(required), (
        f'只算出 {checked} 个状态色的对比度，期望 {len(required)} 个 —— 本测试已失效'
    )
    assert not problems, (
        '进度条填充色对轨道底色的对比度不足，该状态"在但看不清"：\n'
        + '\n'.join('  ' + p for p in problems)
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


# --------------------------------------------------------------------------
# A1b / Task 6：失败卡片里的错误框
# --------------------------------------------------------------------------

_RGBA_RE = re.compile(
    r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)', re.I
)


def _resolve_color(css, raw):
    """声明值 -> 字面色值（剥 !important、解一层 var(--x)）。

    只解一层是够的：本项目的调色板变量都直接指向字面值，没有变量指变量。
    解不出来的写法（color-mix()、多层 var()）会原样返回，由 `_flatten`
    的「算不了就报测试已失效」断言接住——静默放行比算错更危险。
    """
    value = _IMPORTANT_RE.sub('', raw).strip()
    m = re.fullmatch(r'var\(\s*(--[-\w]+)\s*\)', value)
    if m:
        value = _palette_var(css, m.group(1))
    return value.strip().lower()


def _flatten(color, backdrop_hex):
    """半透明色压到不透明背景上，得到肉眼**实际看到**的 #rrggbb。

    为什么必须合成：本项目的状态底色全是 `rgba(...,0.12)`。
    直接拿 rgba 的 RGB 分量去算对比度会得到一个与屏幕上完全无关的数字
    —— rgba(239,68,68,0.12) 的「原色」是亮红，压到 #15171c 上之后
    实际是很深的 #2f1c21，两者的对比度差着好几倍。
    """
    if re.fullmatch(r'#[0-9a-f]{6}', color):
        return color
    m = _RGBA_RE.fullmatch(color)
    assert m, f'{color!r} 既不是 #rrggbb 也不是 rgb(a)() —— 本测试算不了它，已失效'
    a = float(m.group(4)) if m.group(4) is not None else 1.0
    fg = [int(m.group(i)) for i in (1, 2, 3)]
    bg = _hex_to_rgb(backdrop_hex)
    return '#%02x%02x%02x' % tuple(
        min(255, max(0, round(a * f + (1 - a) * b))) for f, b in zip(fg, bg)
    )


# WCAG 2.x：正文文字 4.5:1（`.task-error` 用 var(--font-size-sm) = 0.875rem = 14px，
# 属于「正常文字」，拿不到大字号 18.66px/24px 的 3:1 优惠）；边框属于图形对象，
# 下限 3:1——与 test_form_select_arrow_has_sufficient_contrast、
# test_progress_bar_fill_has_sufficient_contrast 是同一条线。
ERROR_TEXT_MIN_CONTRAST = 4.5
ERROR_BORDER_MIN_CONTRAST = 3.0


def _effective_task_card_backdrop(css):
    """失败卡片实际压在什么底色上 —— **从 `.card` 解析，不许硬编码调色板变量**。

    ⚠️ 这个函数存在的唯一理由，是本文件上一版把背衬写死成
    `_palette_var(css, '--color-bg-secondary')` 并注释「.task-card 的底色」，
    而那是**巧合**：

      - `.task-card { background: var(--color-bg-secondary) }` 是**死声明**，
        被 `div:not(...)` 兜底重置（特异度 0,10,1）压掉了。CDP 实测
        `getComputedStyle(.task-card).backgroundColor === 'rgba(0, 0, 0, 0)'`。
      - 真正的背衬是祖先面板 `.card`（CDP 实测 `rgb(21, 23, 28)`），
        它**恰好**也用 `--color-bg-secondary`。

    后果：后面任何一个视觉任务改 `.card` 的底色，浏览器里的真实对比度就变了，
    而拿 `--color-bg-secondary` 算的断言照旧全绿 —— 与 A1b 抓到的
    「红色底纹被压成透明而测试全绿」是同一类失明。所以这里改成顺着**真实
    渲染链**去取：`.card` 声明什么，就用什么。

    `.task-card` 自己不参与计算（它的 background 是死的）。想让它复活，
    得先把它加进兜底重置的 `:not()` 白名单——那属于 C1 类清理，见
    test_task_error_survives_the_blanket_div_reset 的说明。
    """
    rules = [
        (sel, body) for sel, body, at_ctx in _rules_ctx(css)
        if not at_ctx and '.card' in _selector_parts(sel)
        and ('background' in _decl_map(body) or 'background-color' in _decl_map(body))
    ]
    assert len(rules) == 1, (
        f'期望顶层恰好 1 条声明了背景色的 `.card` 规则，实际 {len(rules)} 条：'
        f'{[s for s, _ in rules]} —— 面板底色的来源变了，本测试已失效'
    )
    decls = _decl_map(rules[0][1])
    raw = decls.get('background') or decls.get('background-color')
    value = _resolve_color(css, raw)
    assert re.fullmatch(r'#[0-9a-f]{6}', value), (
        f'`.card` 的背景色解析成 {value!r}，不是 6 位十六进制 —— '
        '本断言算不了它的对比度，测试已失效（不是通过）'
    )
    return value


def test_task_error_box_exists_and_is_readable():
    """失败卡片里的 `.task-error` 必须存在、在顶层、且文字真的看得清。

    强度说明：只断言 `'.task-error' in css` 的话，一条空规则
    `.task-error {}` 就能过——而错误文本会以「深色卡片上的默认色」渲染，
    很可能是 Bootstrap 继承来的深灰，等于把失败原因写成隐形字。
    这里逐项落地：

      1. 顶层恰好一条规则（用 `_rules_ctx` 并要求 at-rule 上下文为空——
         包进 `@media (min-width: 3000px)` 的规则在文件里存在但永不生效，
         这是 Task 5 评审实测出来的坑）。
      2. 规则体里真的声明了 background / border / color。
      3. **半透明底色先合成再算对比度**：`--color-danger-bg` 是
         rgba(239,68,68,0.12)，压在真实背衬上才是屏幕上的实际底色。
      4. **背衬顺着真实渲染链取**（`_effective_task_card_backdrop`：从 `.card`
         的声明解析，不是硬编码 `--color-bg-secondary`）。硬编码那版是巧合，
         改 `.card` 底色时会静默漂移，理由见那个函数的 docstring。
      5. 文字对合成底色 >= 4.5:1（正文），边框对背衬 >= 3:1（图形）。

    覆盖范围（诚实说明）：这条守的是 CSS 源码里能算出来的色值关系。
    它保证不了「这个框在浏览器里真的显示出来了」——那由 CDP 实测覆盖
    （p2-task-6-report.md 里有 getComputedStyle 取到的实际颜色与实算比值）。
    """
    css = _css()
    rules = [
        (sel, body) for sel, body, at_ctx in _rules_ctx(css)
        if not at_ctx and '.task-error' in _selector_parts(sel)
    ]
    assert len(rules) == 1, (
        f'期望顶层恰好 1 条 `.task-error` 规则，实际 {len(rules)} 条：'
        f'{[s for s, _ in rules]}'
    )
    decls = _decl_map(rules[0][1])
    for prop in ('background', 'border', 'color'):
        assert prop in decls, f'.task-error 没有声明 {prop}'

    card = _effective_task_card_backdrop(css)

    box_bg = _flatten(_resolve_color(css, decls['background']), card)
    text = _flatten(_resolve_color(css, decls['color']), box_bg)
    ratio = _contrast_ratio(text, box_bg)
    assert ratio >= ERROR_TEXT_MIN_CONTRAST, (
        f'.task-error 的文字 {text} 对合成后的底色 {box_bg} 只有 {ratio:.2f}:1，'
        f'低于 WCAG 正文 {ERROR_TEXT_MIN_CONTRAST}:1 —— 失败原因会「在但看不清」'
    )

    m = re.search(r'(#[0-9a-fA-F]{6}|rgba?\([^)]*\)|var\(\s*--[-\w]+\s*\))',
                  decls['border'])
    assert m, f'.task-error 的 border 里解析不出颜色：{decls["border"]!r} —— 本测试已失效'
    border = _flatten(_resolve_color(css, m.group(1)), card)
    bratio = _contrast_ratio(border, card)
    assert bratio >= ERROR_BORDER_MIN_CONTRAST, (
        f'.task-error 的边框 {border} 对真实背衬(.card) {card} 只有 {bratio:.2f}:1，'
        f'低于图形元素 {ERROR_BORDER_MIN_CONTRAST}:1'
    )


# 兜底重置规则的形态：`div:not(.a):not(.b)... { background: transparent }`。
_BLANKET_DIV_RESET = re.compile(r'^div(?::not\([^)]*\))+$')


def test_task_error_survives_the_blanket_div_reset():
    """`.task-error` 必须列进 `div:not(...)...{background:transparent}` 的白名单。

    这条是 CDP 实测逼出来的，**上一条对比度断言拦不住它**：

    style.css 里有一条兜底重置
        div:not(.card):not(.modal-content):not(.alert):not(.badge)...
        { background: transparent; }
    特异度 (0,10,1) —— 10 个 `:not(.class)` 各贡献一个类。任何
    `.task-error { background: ... }`(0,1,0) 都打不过它。

    实测证据（Chrome 148，CDP `CSS.getMatchedStylesForNode`）：
    加白名单之前，`.task-error` 的 `getComputedStyle().backgroundColor`
    是 `rgba(0, 0, 0, 0)` —— 红色底纹**在源码里存在、在浏览器里完全不出现**，
    而 test_task_error_box_exists_and_is_readable（只读源码算色值）依然全绿。
    这正是 p2-assertion-review.md 反复强调的「写了断言 ≠ 断言守住了我以为的东西」。

    ⚠️ 顺带记录一个本任务**没有**修的既有缺陷：`.task-card { background:
    var(--color-bg-secondary) }` 同样被这条重置压掉，卡片底色一直是透明的。
    目前肉眼看不出来，因为它背后的面板 `.card` 恰好也是 --color-bg-secondary，
    两者同色。面板色一改，卡片就不会跟着走。修它属于 C1 类死规则清理，
    不在 A1b 范围内。

    覆盖范围（诚实说明）：兜底重置若被整条删掉，这条测试就没有东西可查、
    自然通过 —— 那时确实也不需要白名单了，所以不算静默失效。
    """
    css = _css()
    blankets = [
        (sel, body) for sel, body, at_ctx in _rules_ctx(css)
        if not at_ctx
        for part in _selector_parts(sel)
        if _BLANKET_DIV_RESET.fullmatch(part)
        and 'transparent' in (_decl_map(body).get('background') or
                              _decl_map(body).get('background-color') or '')
    ]
    problems = []
    for sel, _body in blankets:
        excluded = set(re.findall(r':not\(\s*([^)]*?)\s*\)', sel))
        if '.task-error' not in excluded:
            problems.append(
                f'{sel[:60]}... 的 :not() 白名单里没有 .task-error'
            )
    assert not problems, (
        '失败原因框的红色底纹会被 div 兜底重置压成透明（源码里有、浏览器里没有）：\n'
        + '\n'.join('  ' + p for p in problems)
    )


def test_toast_container_cannot_grow_past_the_viewport():
    """`#app-toast-container` 必须有 max-height + 可滚动的 overflow-y。

    背景：A1b 之前应用里**没有**常驻 toast——`showToast` 的默认 duration 是
    3500ms，容器永远堆不高，所以它原本只有 `max-width`、没有任何高度约束。
    A1b 引入了全应用第一个 `duration: 0`（任务失败原因，见
    tests/test_tasks_js_contract.py::test_failed_task_pops_a_persistent_toast）。

    没有这条约束会怎样：N 个任务同时失败 → N 条永不消失的 toast 向下堆叠。
    容器是 `position: fixed`，**不产生页面滚动条**，所以超出视口底部的那几条
    连 × 按钮都点不到，用户只能刷新页面；而且它们会盖住 index.html 右侧那一列
    ——正好是 A1b 要让用户看清的任务列表。

    覆盖范围（诚实说明）：这条只保证「声明了高度上限并且可滚」。
    「滚轮真的能滚动一个 pointer-events: none 的容器」「所有 × 都点得到」
    这两件事文本断言证明不了，由 CDP 实测覆盖（8 条 toast 的实测见
    p2-task-6-report.md 的 I2 一节）。
    """
    rules = [
        (sel, body) for sel, body, at_ctx in _rules_ctx(_css())
        if not at_ctx and '#app-toast-container' in _selector_parts(sel)
    ]
    assert len(rules) == 1, (
        f'期望顶层恰好 1 条 `#app-toast-container` 规则，实际 {len(rules)} 条 —— 本测试已失效'
    )
    decls = _decl_map(rules[0][1])
    assert 'max-height' in decls, (
        '#app-toast-container 没有 max-height —— 常驻 toast 会一路堆出视口，'
        '超出部分的 × 点不到（fixed 容器不产生页面滚动条）'
    )
    overflow = (decls.get('overflow-y') or decls.get('overflow') or '').strip().lower()
    assert overflow in ('auto', 'scroll'), (
        f'#app-toast-container 的 overflow-y 是 {overflow!r}，必须是 auto 或 scroll ——'
        '否则光有 max-height 只会把超出的 toast 直接裁掉，更糟'
    )


# --------------------------------------------------------------------------
# A2 / Task 7：进度条百分比覆盖层
# --------------------------------------------------------------------------

# 覆盖层文字属于「正常文字」（0.875rem = 14px，够不上大字号 18.66px/24px
# 的 3:1 优惠），下限 4.5:1 —— 与 ERROR_TEXT_MIN_CONTRAST 同一条线。
PROGRESS_LABEL_MIN_CONTRAST = 4.5


def _top_rules(css, part):
    """顶层（不在任何 at-rule 里）且选择器某个分支恰好等于 `part` 的规则。

    要求「顶层」的理由与 `_progress_bar_color_rules` 相同：把规则包进
    `@media (min-width: 3000px)` 之后，只查 `part in css` 的断言仍然全绿，
    而规则在任何正常视口下都不生效。
    """
    return [
        (sel, body) for sel, body, at_ctx in _rules_ctx(css)
        if not at_ctx and part in _selector_parts(sel)
    ]


def _progress_fill_hexes(css):
    """getStatusColor 能产出的每一档进度条填充色 -> {颜色名: '#rrggbb'}。

    直接顺着 `.progress-bar.bg-XXX` 的声明解析，不在测试里手抄六个色号
    —— 手抄的表会在调色板改动时静默过期，而对比度断言照旧全绿。
    """
    out = {}
    for name, found in _progress_bar_color_rules(css).items():
        if len(found) != 1:
            continue
        decls = _decl_map(found[0][1])
        raw = decls.get('background') or decls.get('background-color')
        if not raw:
            continue
        value = _resolve_color(css, raw)
        if re.fullmatch(r'#[0-9a-f]{6}', value):
            out[name] = value
    return out


def test_progress_track_is_a_positioning_context():
    """`.progress` 必须声明 position: relative。

    覆盖层用的是 position: absolute。`absolute` 是相对**最近的已定位祖先**
    定位的，`.progress` 本身不定位的话就会一路上溯到 `.card` /
    `.modal-content`（两者都有 position），百分比数字会跑到卡片左上角或者
    模态框边上去 —— 而所有「文字色/对比度」断言依然全绿，因为颜色确实是对的。

    Bootstrap 5.3 的 `.progress` 不带 position，所以这条必须由我们自己声明；
    也因此不需要 !important。
    """
    rules = _top_rules(_css(), '.progress')
    assert len(rules) == 1, (
        f'期望顶层恰好 1 条裸 `.progress` 规则，实际 {len(rules)} 条 —— 本测试已失效'
    )
    pos = _decl_map(rules[0][1]).get('position', '').strip().lower()
    assert pos == 'relative', (
        f'`.progress` 的 position 是 {pos!r}，必须是 relative —— '
        '否则 .progress__label 会相对 .card / .modal-content 定位，跑出进度条'
    )


def test_progress_label_is_an_overlay_that_always_renders():
    """`.progress__label` 必须是顶层唯一一条、绝对定位、且**不被隐藏**的规则。

    三件事各有来历：

      1. **顶层恰好一条。** 两条同名规则说明有人在别处又覆盖了一次。

      2. **position: absolute。** 覆盖层的全部意义就在这里：数字不再是
         `.progress-bar` 的子元素，于是不受 `.progress-bar` 宽度为 0 的影响。
         改前 progress=0 时 CDP 实测数字画出 **0 个像素**（截图法：藏掉文字
         前后两张图差异为 0），progress=1 时只剩 230 个像素（50% 时是 2037）。

      3. **display 不能是 none。** 简报原方案是「基础规则 display:none，
         只在 `.modal .progress__label` 里显示」，理由是「卡片里的进度条只有
         8px 高，放不下 0.875rem 的文字」。**这个前提是错的**：CDP 实测
         `#activeTasks .progress` 的 computed height 是 **28px** —— 两处
         渲染点（`tasks.js` 的 createTaskCard、`history.js` 的详情模态框）
         都带 `style="height: 28px"` 内联样式，压过了 CSS 里那条
         `.progress { height: 8px }`（它其实不作用于任何元素）。
         照抄那个方案会把卡片上**现在就能看见**的百分比直接删掉。

      另外禁 mix-blend-mode：实测是负优化（over bg-primary 4.50 -> 1.48，
      over bg-danger 2.77 -> 1.88）。
    """
    rules = _top_rules(_css(), '.progress__label')
    assert len(rules) == 1, (
        f'期望顶层恰好 1 条 `.progress__label` 规则，实际 {len(rules)} 条：'
        f'{[s for s, _ in rules]}'
    )
    decls = _decl_map(rules[0][1])

    pos = decls.get('position', '').strip().lower()
    assert pos == 'absolute', (
        f'.progress__label 的 position 是 {pos!r}，必须是 absolute —— '
        '不绝对定位就还是跟着 .progress-bar 的宽度走，progress=0 时照样消失'
    )
    display = decls.get('display', '').strip().lower()
    assert display != 'none', (
        '.progress__label 的基础规则把自己 display:none 了 —— '
        '卡片里的进度条实测 28px（不是简报说的 8px），装得下 0.875rem 的文字，'
        '隐藏它等于删掉卡片上现有的百分比'
    )
    assert 'mix-blend-mode' not in decls, (
        '.progress__label 用了 mix-blend-mode —— 实测是负优化：'
        'over bg-primary 对比度从 4.50 掉到 1.48，over bg-danger 从 2.77 掉到 1.88'
    )


def test_progress_label_readability_does_not_depend_on_the_fill():
    """百分比文字对它**实际压着的**底色必须 >= 4.5:1，六档填充与轨道都算在内。

    ⚠️ 这条是本任务最重要的断言，也是简报原方案过不了的那条。

    **为什么单纯换个深色文字色解决不了**（可以算出来，不是意见）：
    覆盖层是 `inset` 在整条轨道上、居中显示的，所以它压着什么完全取决于
    progress：0% 时压在轨道 `--color-bg-tertiary`(#1c2027) 上，100% 时压在
    填充色上，50% 时**正好横跨填充边界**，左半在填充上、右半在轨道上。
    于是文字色必须同时对轨道和最亮的填充（`--color-warning` #fbbf24）达标。
    而轨道与 warning 之间的对比度只有 **9.79:1** < 4.5 x 4.5 = 20.25，
    按 WCAG 对比度公式这就意味着**不存在**任何单一颜色能对两者都拿到 4.5:1
    （需要亮度 L >= 0.2392 同时 L <= 0.0898，空集）。
    简报建议的 `#0b1220` 对六档填充确实全部 >= 4.5（4.51 ~ 11.22），但它对
    轨道只有 **1.15:1** —— progress=0 时数字从「被裁掉看不见」变成
    「画出来了但看不见」，本任务要修的缺陷原样留着，而按简报写的断言全绿。

    所以做法是给覆盖层**自带一块不透明的底**（chip），让可读性与背后是
    填充还是轨道无关。本断言按实际情况二选一：

      - 覆盖层声明了不透明背景 -> 文字只需对**那块底**达标（背后是什么无所谓）；
      - 没声明背景 -> 文字直接压在轨道和六档填充上，**每一个**都要达标
        （按上面的证明，这条路走不通，会响亮失败并给出具体数字）。

    覆盖范围（诚实说明）：这条守的是 CSS 源码里算得出来的色值关系。
    「浏览器里真的画出来了」由 CDP 截图实测覆盖（藏掉文字前后的像素差 +
    差异像素对同位置背景的实测对比度，见 p2-task-7-report.md）。
    """
    css = _css()
    rules = _top_rules(css, '.progress__label')
    assert len(rules) == 1, '`.progress__label` 顶层规则不唯一 —— 本测试已失效'
    decls = _decl_map(rules[0][1])

    assert 'color' in decls, '.progress__label 没有声明 color —— 会继承 Bootstrap 的白字'
    text = _resolve_color(css, decls['color'])
    assert re.fullmatch(r'#[0-9a-f]{6}', text), (
        f'.progress__label 的文字色解析成 {text!r}，不是 6 位十六进制 —— '
        '本断言算不了它的对比度，测试已失效（不是通过）'
    )

    fills = _progress_fill_hexes(css)
    required = _status_color_names('tasks.js') | _status_color_names('history.js')
    missing = sorted(required - set(fills))
    assert not missing, (
        f'解析不出 {missing} 的填充色 —— 六档没算全，本测试已失效'
    )

    track_raw = _progress_track_color(css)
    track = _resolve_color(css, track_raw)
    assert re.fullmatch(r'#[0-9a-f]{6}', track), (
        f'轨道底色解析成 {track!r} —— 本测试已失效'
    )

    raw_bg = decls.get('background') or decls.get('background-color')
    backdrops = {}
    if raw_bg is not None:
        chip = _resolve_color(css, raw_bg)
        assert re.fullmatch(r'#[0-9a-f]{6}', chip), (
            f'.progress__label 的背景 {chip!r} 不是不透明的 #rrggbb —— '
            '半透明底会把背后的填充色透上来，可读性重新变成「看运气」。'
            '要么改成不透明色，要么删掉背景让本断言走「直接压在填充上」那条路'
        )
        backdrops['覆盖层自带底'] = chip
    else:
        backdrops['轨道'] = track
        for name, hexv in sorted(fills.items()):
            backdrops[f'填充 bg-{name}'] = hexv

    problems = []
    for what, bg in backdrops.items():
        ratio = _contrast_ratio(text, bg)
        if ratio < PROGRESS_LABEL_MIN_CONTRAST:
            problems.append(
                f'{what}({bg}) 上只有 {ratio:.2f}:1，'
                f'低于 WCAG 正文 {PROGRESS_LABEL_MIN_CONTRAST}:1'
            )
    assert not problems, (
        f'进度条百分比文字 {text} 在这些底色上读不出来：\n'
        + '\n'.join('  ' + p for p in problems)
        + '\n提示：覆盖层横跨整条轨道，progress 一变底色就变；'
          '单一文字色对「轨道 + 最亮填充」不可能同时达标（见本测试 docstring 的算式）'
    )


def _progress_label_chip_height_px(css):
    """`.progress__label` 那块 chip 的高度（px）= 行高 + 上下内边距。

    从 CSS 里算出来而不是写死 18 —— 写死的话，谁把字号或内边距调大，
    高度下限就静默过期了，而那正是这条约束要防的事。
    算不出来的写法（calc()/em/百分比行高）返回 None，让调用方报「测试已失效」。
    """
    rules = _top_rules(css, '.progress__label')
    assert len(rules) == 1, '`.progress__label` 顶层规则不唯一 —— 本测试已失效'
    decls = _decl_map(rules[0][1])

    raw_fs = decls.get('font-size')
    assert raw_fs, '.progress__label 没有声明 font-size —— 本测试已失效'
    m = re.fullmatch(r'var\(\s*(--[-\w]+)\s*\)', raw_fs.strip())
    font_px = _length_to_px(_palette_var(css, m.group(1)) if m else raw_fs)
    if font_px is None:
        return None

    lh = decls.get('line-height', '').strip()
    if re.fullmatch(r'[\d.]+', lh):          # 无单位行高 = 倍数
        line_px = font_px * float(lh)
    else:
        line_px = _length_to_px(lh)
        if line_px is None:
            return None

    # padding 简写取上下分量：1 值 -> 该值；2/3/4 值 -> 第 1 个
    pad = decls.get('padding', '0').strip().split()
    pad_px = _length_to_px(pad[0]) if pad else 0.0
    if pad_px is None:
        return None
    return line_px + 2 * pad_px


def test_every_progress_height_fits_the_label():
    """任何给 `.progress` 设高度的规则，高度都不能矮于覆盖层 chip。

    ⚠️ 这条是评审实测逼出来的，前面几条断言对它完全失明。

    `.progress` 有 `overflow: hidden`，而覆盖层是它的绝对定位子元素。
    评审 CDP 实测：让原来那条 `.progress { height: 8px }` 生效之后，
    **18px 高的 chip 被上下各裁掉 5px，数字被拦腰切断** —— 而
    test_progress_label_readability_does_not_depend_on_the_fill 算的是色值、
    test_progress_label_is_an_overlay_that_always_renders 查的是 position 和
    display，两条都照旧全绿。

    改之前那条 8px 是无害的（条里的文字跟着 .progress-bar 走，轨道细就细）；
    **改之后它会毁掉这个功能**。当时不发作，只是因为两处 markup 恰好都内联了
    `height: 28px` 把它盖住 —— 也就是说功能正确性静默依赖「每个未来的
    `.progress` 都记得写内联高度」。现在高度收回 CSS、内联删掉，由这条断言兜底。

    覆盖范围（诚实说明）：只查 CSS 里的 height 声明。markup 侧不许再内联
    height 由 tests/test_tasks_js_contract.py::test_progress_track_markup_is_nested_and_heightless
    守住；「浏览器里 chip 真的没被裁」由 CDP 实测覆盖（label 的 rect 完全落在
    .progress 的裁剪框内，clippedOut = false）。
    """
    css = _css()
    chip = _progress_label_chip_height_px(css)
    assert chip is not None, (
        '.progress__label 的 font-size / line-height / padding 里有本断言算不了的写法 '
        '—— chip 高度求不出来，测试已失效（不是通过）'
    )

    # 选择器最后一个复合部分是 `.progress` 的规则：裸 `.progress`、
    # `.modal .progress`、`.zoom-level .progress` 都算，`.progress-bar` 不算。
    checked, problems = 0, []
    for sel, body, at_ctx in _rules_ctx(css):
        for part in _selector_parts(sel):
            if not re.search(r'(^|[\s>+~])\.progress$', part):
                continue
            raw = _decl_map(body).get('height')
            if raw is None:
                continue
            px = _length_to_px(_IMPORTANT_RE.sub('', raw).strip())
            if px is None:
                problems.append(
                    f'{part} {{ height: {raw} }}: 本断言算不了这个写法 —— 测试已失效')
                continue
            checked += 1
            if px < chip:
                problems.append(
                    f'{part} {{ height: {raw} }} = {px:g}px，矮于覆盖层 chip 的 '
                    f'{chip:g}px（{at_ctx or "顶层"}）—— .progress 的 overflow: hidden '
                    f'会把百分比数字上下裁断'
                )
    assert checked >= 1, (
        '一条给 `.progress` 设高度的规则都没找到 —— 高度没了会回落到 Bootstrap 的 '
        '1rem(16px)，同样矮于 chip。本测试已失效'
    )
    assert not problems, (
        '有 `.progress` 高度装不下百分比覆盖层：\n' + '\n'.join('  ' + p for p in problems))


def test_progress_label_chip_is_seamless_against_the_track():
    """覆盖层 chip 的底色必须**等于**轨道底色。

    这是本设计的立身之本，也是唯一一条守「好不好看」的断言：
    低进度时 chip 压在轨道上，两者同色 -> 看不出有块底，视觉上就是
    「轨道上的一行字」（CDP 截图复核：0% / 1% / 35% 三档完全看不出 chip 存在）。
    颜色一旦分家，0% 时会变成轨道上一块**突兀的浮动药丸** —— 而
    test_progress_label_readability_does_not_depend_on_the_fill 只比
    「文字 vs chip」，chip 换成任何颜色它都照旧全绿。

    比较的是**解析后的字面色值**而不是变量名：写 `var(--color-bg-tertiary)`
    还是直接写 `#1c2027` 都算相同，换成另一个碰巧同值的变量也算相同。
    真正要拦的是「两者渲染出来不是一个颜色」。
    """
    css = _css()
    rules = _top_rules(css, '.progress__label')
    assert len(rules) == 1, '`.progress__label` 顶层规则不唯一 —— 本测试已失效'
    raw = _decl_map(rules[0][1]).get('background') or \
        _decl_map(rules[0][1]).get('background-color')
    assert raw, (
        '.progress__label 没有背景 —— 无缝性无从谈起，而且可读性会退回'
        '「看背后是什么」（见 test_progress_label_readability_does_not_depend_on_the_fill）'
    )
    chip = _resolve_color(css, raw)
    track = _resolve_color(css, _progress_track_color(css))
    assert chip == track, (
        f'覆盖层 chip 的底色 {chip} 与 `.progress` 轨道底色 {track} 不同 —— '
        f'低进度时数字下面会出现一块突兀的浮动药丸（对比度 '
        f'{_contrast_ratio(chip, track):.2f}:1）。'
        '要么改回同色，要么这是一次有意的视觉改动，请连同本断言一起更新'
    )


# --------------------------------------------------------------------------
# A3 / Task 8：Bootstrap 深色主题总开关
# --------------------------------------------------------------------------

_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates'
)


def _template(name):
    with open(os.path.join(_TEMPLATES_DIR, name), encoding='utf-8') as f:
        return f.read()


class _StartTagCollector(HTMLParser):
    """收集 (标签名, 属性字典)。只有**真正的开始标签**会进来。

    这正是本节需要它的理由：`handle_starttag` 对注释、纯文本、属性值里的
    字符串一概不触发，所以「把 data-bs-theme=\"dark\" 写进注释」或
    「写在别的标签上」都不会被误判成通过。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag.lower(), {k.lower(): v for k, v in attrs}))


def _start_tags(markup):
    """markup 里的全部开始标签（含自闭合标签，HTMLParser 默认会转发过来）。"""
    parser = _StartTagCollector()
    parser.feed(markup)
    parser.close()
    return parser.tags


def test_bootstrap_dark_theme_is_enabled_on_the_html_element():
    """`<html>` 标签上必须有 data-bs-theme="dark"。

    为什么这一个属性是整个任务的核心：Bootstrap 5.3 的
    `[data-bs-theme=dark]` 选择器块里第一条声明就是 `color-scheme: dark`
    （已核对 CDN 源码 bootstrap@5.3.0/dist/css/bootstrap.css:127-128），
    浏览器据此把**原生控件**——select 弹层、number 微调箭头、文件选择按钮
    ——整体渲染成深色。同时它把 `--bs-tertiary-bg` 从 #f8f9fa 翻成 #2b3035，
    修掉 `.form-control::file-selector-button` 的灰白底。
    缺了它，自定义 CSS 只是在亮色 Bootstrap 上刷了一层深色漆，
    凡是我们没逐条覆盖到的地方都会漏白。

    ⚠️ 强度说明（p2-assertion-review.md 的 I 条）：计划原文写的是
        assert 'data-bs-theme="dark"' in html
    那条**写在注释里、写在任意 `<div>` 上都能通过**。这里改用标准库
    HTMLParser 真正解析标签树，只认 `<html>` 这一个开始标签上的属性。
    变异实验（报告里有输出）：把该属性挪进 HTML 注释 / 挪到 `<body>` 上，
    本断言都会变红，而计划原文那条两次都是绿的。

    只查 base.html 是够的，因为另外三个模板都 `{% extends %}` 它 ——
    由 test_every_page_template_inherits_the_themed_html_element 钉住。
    """
    tags = _start_tags(_template('base.html'))
    assert tags, 'base.html 解析不出任何开始标签 —— 本测试已失效'
    html_tags = [attrs for name, attrs in tags if name == 'html']
    assert len(html_tags) == 1, (
        f'base.html 里解析出 {len(html_tags)} 个 <html> 开始标签，期望恰好 1 个 '
        '—— 本测试已失效'
    )
    theme = html_tags[0].get('data-bs-theme')
    assert theme == 'dark', (
        f'<html> 的 data-bs-theme 是 {theme!r}，必须是 "dark"。\n'
        '没有它，Bootstrap 整体仍处于亮色模式（实测 --bs-body-bg: #fff、'
        '--bs-tertiary-bg: #f8f9fa），原生控件会在深色界面上漏出白块。'
    )


def test_every_page_template_inherits_the_themed_html_element():
    """每个页面模板要么 extends base.html，要么自带一个带 data-bs-theme 的 `<html>`。

    上一条只查 base.html。若有人新加一个**自带 `<html>` 标签**的页面模板
    （不继承 base.html），那一页会静默回到亮色 Bootstrap，而上一条全绿。
    这条把「主题覆盖了全部页面」这个真正的意图钉住。
    """
    names = sorted(
        n for n in os.listdir(_TEMPLATES_DIR)
        if n.endswith('.html') and n != 'base.html'
    )
    assert names, 'templates/ 下除 base.html 外没有别的页面模板 —— 本测试已失效'
    problems = []
    for name in names:
        markup = _template(name)
        own_html = [attrs for tag, attrs in _start_tags(markup) if tag == 'html']
        if own_html:
            if any(a.get('data-bs-theme') != 'dark' for a in own_html):
                problems.append(f'{name}: 自带 <html> 标签但没有 data-bs-theme="dark"')
        elif not re.search(r'{%-?\s*extends\s+["\']base\.html["\']', markup):
            problems.append(
                f'{name}: 既没有 extends base.html，也没有自带带主题的 <html> 标签'
            )
    assert not problems, (
        '有页面模板拿不到 data-bs-theme="dark"，那一页会回到亮色 Bootstrap：\n'
        + '\n'.join('  ' + p for p in problems)
    )


# --------------------------------------------------------------------------
# A3 / Task 8：取色器色块尺寸
# --------------------------------------------------------------------------

# 色块（`<input type="color">` 的内容框）最小可视宽度。
#
# 30px 的来历：实测缺陷是色块被 `.form-control` 的 `padding: 0.6rem 0.85rem`
# 挤成 **18.8 x 15.3px**，「几乎看不出颜色」。30px 是任务验收表给的下限。
# 它和 ARROW_MIN_PADDING_RIGHT_PX 一样是**几何**下限，不是抽样结论：
# 色块宽度 = 外框 width - 左右内边距 - 两条边框，三项全部在 CSS 里写着，
# 与视口宽度无关（元素上的内联 `max-width:60px` 大于外框宽度，不参与）。
COLOR_SWATCH_MIN_WIDTH_PX = 30

_PADDING_SIDES = ('padding-top', 'padding-right', 'padding-bottom', 'padding-left')


def _expanded_box_decls(body):
    """规则体 -> [(属性名, 值, 是否!important), ...]，保持声明顺序，
    并把 `padding` 简写展开成四条长写。

    只处理本节关心的 width / padding*。和 `_right_padding_in_body` 同一个
    理由：**不能用 `_decl_map`**，它按属性名去重，丢掉了「简写与长写谁在后面」
    这个决定胜负的信息 —— 而「简写静默覆盖长写」正是 C1/Task 4 那个缺陷的形态。
    """
    out = []
    for chunk in body.split(';'):
        if ':' not in chunk:
            continue
        name, _, raw = chunk.partition(':')
        name = name.strip().lower()
        raw = raw.strip()
        important = bool(_IMPORTANT_RE.search(raw))
        val = _IMPORTANT_RE.sub('', raw).strip()
        if name == 'padding':
            parts = val.split()
            if len(parts) == 1:
                sides = parts * 4
            elif len(parts) == 2:
                sides = [parts[0], parts[1], parts[0], parts[1]]
            elif len(parts) == 3:
                sides = [parts[0], parts[1], parts[2], parts[1]]
            elif len(parts) == 4:
                sides = list(parts)
            else:
                continue
            out.extend(zip(_PADDING_SIDES, sides, [important] * 4))
        elif name in _PADDING_SIDES or name == 'width':
            out.append((name, val, important))
    return out


def _form_control_border_px(css):
    """`.form-control, .form-select` 声明的边框宽度（px）—— 色块要减掉两条边。

    从 CSS 解析而不是写死 1px：边框宽度一改，色块就跟着变窄，写死的话
    下限断言会静默过期。
    """
    bodies = [body for sel, body in _rules(css) if sel == '.form-control, .form-select']
    assert len(bodies) == 1, (
        f'期望恰好 1 条 `.form-control, .form-select` 规则，实际 {len(bodies)} 条 '
        '—— 本测试已失效'
    )
    border = _decl_map(bodies[0]).get('border')
    assert border, '`.form-control, .form-select` 没有声明 border —— 本测试已失效'
    m = re.match(r'^([\d.]+)(px|rem)\b', _IMPORTANT_RE.sub('', border).strip())
    assert m, f'border 简写 {border!r} 里读不出宽度 —— 本测试已失效'
    return float(m.group(1)) * (16 if m.group(2) == 'rem' else 1)


def test_color_picker_swatch_is_big_enough_to_see():
    """`<input type="color" class="form-control form-control-color">` 的色块
    必须至少 30px 宽。

    缺陷（Phase 2 视觉基线实测）：`.form-control-color` 继承了
    `.form-control, .form-select` 的 `padding: 0.6rem 0.85rem`（左右各 13.6px），
    而它的外框只有 Bootstrap 给的 3rem(48px) —— 48 - 27.2 - 2 = **18.8px**，
    连同 15.3px 的高度，色块小到看不出选的是什么颜色。

    ⚠️ 本断言为什么要模拟层叠，而不是只查 `.form-control-color` 里有没有
    padding 声明：因为决定胜负的是**顺序**。`.form-control-color` 与
    `.form-control` 特异性相同（都是 (0,1,0)），谁写在后面谁生效。只查
    「声明存在」的话，把 `.form-control-color` 挪到 `.form-control` 前面，
    padding 立刻回到 13.6px，而测试全绿。这与 C1/Task 4
    test_form_select_reserves_room_for_the_arrow 是同一套模型。

    覆盖范围（诚实说明）：
      1. 只模拟 **style.css 内部**的层叠。外框 `width` 必须由 style.css 自己
         声明——拿不到就报「本测试已失效」，不会去猜 Bootstrap 的默认值。
         这也是把 `width: 3rem` 显式写进 style.css 的理由：让色块宽度的三个
         输入（width / padding / border）都只有一个来源。
      2. 只看类选择器写法的规则（与 `_form_select_rules` 同款前置过滤）。
         有人写 `input[type=color] { padding: 1rem }` 这种属性选择器绕过，
         本断言看不见 —— 由 CDP 实测兜底。
      3. 高度不在本断言范围内（Bootstrap 用 calc(1.5em + ...) 表达，
         `_length_to_px` 解析不了 calc）。高度由 CDP 实测记录。
    """
    css = _css()
    element_classes = {'form-control', 'form-control-color'}
    wanted = {'width', 'padding-left', 'padding-right'}

    best, unsupported = {}, []
    for order, (sel, body, at_ctx) in enumerate(_rules_ctx(css)):
        if at_ctx:
            continue
        decls = [d for d in _expanded_box_decls(body) if d[0] in wanted]
        if not decls:
            continue
        for branch in _selector_parts(sel):
            if not re.search(r'\.form-control(-color)?(?![-\w])', branch):
                continue
            applies = _branch_applies(branch, frozenset(), element_classes, False)
            if applies is None:
                unsupported.append(f'{sel}   （分支 {branch!r} 形态不支持）')
                continue
            if not applies:
                continue
            spec = _branch_specificity(branch)
            for name, val, imp in decls:
                key = (imp, spec, order)
                if name not in best or key > best[name][0]:
                    best[name] = (key, val, sel)
    assert not unsupported, (
        '出现本断言的层叠模型处理不了的写法，测试已失效（不是通过）：\n'
        + '\n'.join('  ' + u for u in unsupported)
    )

    missing = sorted(wanted - set(best))
    assert not missing, (
        f'style.css 里没有任何规则给 .form-control-color 设定 {missing} —— '
        '色块宽度算不出来。请在 style.css 里显式声明（尤其是 width：'
        '不写就只能依赖 Bootstrap 的 3rem 默认值，本断言拿不到它，'
        '色块尺寸会变成一个没人守得住的数字）'
    )

    px = {}
    for name, (_key, val, sel) in best.items():
        value = _length_to_px(val)
        assert value is not None, (
            f'{name} 的胜出值来自 `{sel}` 的 {val!r}，不是 px/rem 字面量，'
            '本断言解析不了 —— 测试已失效（不是通过）'
        )
        px[name] = value

    border = _form_control_border_px(css)
    swatch = px['width'] - px['padding-left'] - px['padding-right'] - 2 * border
    assert swatch >= COLOR_SWATCH_MIN_WIDTH_PX, (
        f'取色器色块只有 {swatch:g}px 宽，低于 {COLOR_SWATCH_MIN_WIDTH_PX}px 下限'
        f'（外框 {px["width"]:g}px - 左右内边距 {px["padding-left"]:g}/'
        f'{px["padding-right"]:g}px - 两条 {border:g}px 边框）—— 色块太小，'
        '看不出选的是什么颜色'
    )
