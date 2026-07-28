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

import pytest

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
    # A5 / Task 10 有意从 --font-size-base(15px) 改成 --font-size-sm(14px)：
    # 控件总高收到 28px 之后，15px 文字配 20px 行高只剩 5px 的上下呼吸位，
    # 视觉上是「字撑满了框」。14px 是同类专业工具的输入框字号（VS Code 13px、
    # QGIS 14px）。改的是本条的**值**，不是删条目 —— 断言仍然守着
    # 「这条规则必须恰好声明一次 font-size」。
    '.form-control, .form-select': 'var(--font-size-sm)',
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
    """!important 声明总量上界 = 68.

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
      + 5 处 / - 5 处：**A4 / Task 9（Leaflet 控件主题化）**，净 0，登记如下——

        新增 5 处，全部在 style.css 的 Leaflet 段：
          1. `.leaflet-bar, .leaflet-draw-toolbar { background-color }`
          2. `.leaflet-bar, .leaflet-draw-toolbar { border }`
          3. `.leaflet-bar, .leaflet-draw-toolbar { box-shadow }`
          4. `.leaflet-control-attribution { background-color }`
          5. `.leaflet-draw-tooltip { background-color }`

        压的是谁、为什么非 !important 不可：
          #1 #4 #5 压的是**本文件自己**的兜底重置
             `div:not(.card):not(...)...{background:transparent}`，特异度 (0,11,1)。
             这三个 Leaflet 元素都是 <div> 且不在那串 :not() 白名单里，
             改前 CDP 实测三者的 computed background-color 全是 rgba(0,0,0,0)。
             不用 !important 的唯一替代是往白名单里再塞三个类 —— 那是在给
             已知的结构债继续加码，明确不做（见
             test_leaflet_div_controls_survive_the_blanket_div_reset）。
          #4 还要额外压 leaflet.css 的
             `.leaflet-container .leaflet-control-attribution{background:rgba(255,255,255,.8)}` (0,2,0)。
          #2 #3 压的是 leaflet.css 的 `.leaflet-touch .leaflet-bar{border:2px solid rgba(0,0,0,.2)}`
             和 `.leaflet-touch .leaflet-bar{box-shadow:none}`，都是 (0,2,0)，
             我们的 `.leaflet-bar` (0,1,0) 赢不了。`.leaflet-touch` 在带触摸屏的
             设备上是常态（headless Chrome 实测也带这个类），不是边角情况。

        删除 5 处（原 `.leaflet-control-zoom` 覆盖块整条删掉）：
             `.leaflet-control-zoom a { background / border / color }` 3 处
           + `.leaflet-control-zoom a:hover { background / border-color }` 2 处。
           缩放控件本身就是 `.leaflet-bar`，新的统一规则已经覆盖它；其中
           background / color 那 3 条在新规则之后是**纯死代码**（同特异度、
           同为 important，后者胜）。history 页（无 #map 滤镜）已 CDP 复核外观正常。

        另外登记一个「本可以加、但选择不加」的决定：打在按钮 <a> 上的规则
        （background-color / color / hover / disabled）**一条 !important 都没用**，
        靠的是同特异度 + style.css 排在 leaflet.css 之后。这个前提由
        test_style_css_is_the_last_stylesheet 钉住 —— 顺序被改动时是测试变红，
        而不是界面静默漏白。按简报预估这里本来要花掉约 13 处额度。

      = 68 处（Task 9 后实测，与 Task 5 后持平）
      - 3 处：**A7 / Task 12 删除**（清理型任务，按棘轮规则把上界一并降下来），
              三处都是 color、都在表格段，删掉的理由是它们**压错了对象**：
          1. `.table td, .table th { color: ... !important }` (0,1,1)
             —— 它压死的不是 Bootstrap，是本文件自己的
             `.text-danger` (0,1,0)!important。history.js 的「加载失败」行
             因此从上线起就是普通灰白色（CDP 实测 rgb(232,234,237)）。
             去掉 !important 之后普通单元格颜色不变：唯一同时命中 td 的
             Bootstrap 规则 `.table > :not(caption) > * > *` 同为 (0,1,1)
             且不带 !important，style.css 排在后面，同分后来者赢。
          2. `.table td small { color: ... !important }` (0,1,2) —— 同型，
             压死的是可能挂在 <small> 上的 .text-danger。
          3. `.table-hover tbody tr:hover td { color: ... !important }` (0,2,3)
             —— **整条规则删除**。它是冗余的（Bootstrap 的行 hover 只改
             `--bs-table-bg-state` 自定义属性，上面第 1 条已经赢了），
             而且是有害的：鼠标划过时连修好之后的 .text-danger 也压得住。
             CDP 实测删除前后 hover 行的普通单元格都是 rgb(232,234,237)。
        本次**新增 0 处**。
      = 65 处（Task 12 后实测）
      + 3 处：A8 / Task 13 的 prefers-reduced-motion 重置块（逐条登记见下）
      = 68 处（Task 13 后实测，正好等于上界，余量 0）

    ⚠️ 棘轮规则（分两种任务，别混用）：

      **清理型任务**（删掉了 !important）：把上界降到「新实测值 + 3」。
      不降的话，前面清出来的空间会被后面的任务悄悄填回去。

      **新增型任务**（确实需要压第三方样式）：允许抬高上界，但必须在本
      docstring 里逐条登记「新增几处、压的是谁、为什么非 !important 不可」。
      抬升本身不是失败，**悄悄抬升**才是。

    已知的计划内新增（这就是上界不能设死的原因）：
      - ~~Leaflet 控件主题化：约 13 处~~ —— **已完成，实测只用了 5 处，
        且同时删掉 5 处死规则，净 0**。预估偏高的原因：那 13 处是按「每条
        Leaflet 覆盖都要压高特异度选择器」估的，实际上只有 <div> 容器
        （被本文件的 div 兜底重置压）和 `.leaflet-touch .leaflet-bar`
        （border / box-shadow）两类真的赢不了；打在 <a> 上的规则同特异度、
        style.css 又排在 leaflet.css 之后，靠源码顺序就够了。
      - ~~动画降噪的 prefers-reduced-motion 重置块：约 4 处~~
        —— **A8 / Task 13 已完成，实测只用了 3 处，上界不动**。逐条登记：

            @media (prefers-reduced-motion: reduce) { *, *::before, *::after }
              1. animation-duration: 0.01ms !important
              2. animation-iteration-count: 1 !important
              3. transition-duration: 0.01ms !important

          为什么非 !important 不可：本块选择器特异度 (0,0,0)，是 CSS 里最弱的
          形态，全站每一条声明 transition/animation 的规则（`.btn` (0,1,0)、
          `.task-card.status-running::before` (0,2,0) …）都比它强。去掉
          !important 整块立刻变成死代码 —— 变异实验 M1/M22 实测，
          test_reduced_motion_actually_stops_every_animated_element 会当场变红。
          这是 a11y 社区的标准写法（web.dev/prefers-reduced-motion）。

          比预估少一处的原因（两条都实测过，不是省着用）：
            · `scroll-behavior: auto !important` —— 通行模板里有，这里不写。
              CDP 实测 Bootstrap 5.3.0 的 reboot 自己就把
              `:root{scroll-behavior:smooth}` 关在 `no-preference` 里：
              默认读到 `smooth`，Emulation 打开 reduce 后读到 `auto`。
              本站自己一处 scroll-behavior 都没声明。
            · `transition-property: ...` —— 时长已压到 0.01ms，再限制哪些属性
              参与过渡不产生任何额外效果。

          ⚠️ 已知豁免（**没有**为它花 !important）：`::-webkit-scrollbar-thumb`
          的 0.15s 颜色过渡压不进 reduce 块 —— `*::before/*::after` 覆盖不到这个
          非标准伪元素，而把它并进那个逗号组会让整组选择器在 Firefox 里作废。
          理由见 _REDUCED_MOTION_EXEMPT 的说明。
      - 进度条 / 滚动条覆盖：约 3 处。
    余下合计约 3 处，届时上界会被抬到 71 上下。**这不代表清理白做了** —— Task 2/3
    清掉的 26 处是「自我覆盖的死规则」，而这些新增是「压第三方库的必要手段」，
    两者性质不同。

    ⚠️ 当前余量为 **0**（实测 68 / 上界 68）：Task 13 把 3 处余量正好用完。
    下一个需要新增 !important 的任务会立刻变红 —— 这是有意的，逼一次显式决策，
    而不是让它悄悄溜进去。

    余下 66 处几乎全是压 Bootstrap 背景/文字色的历史债
    （`background: transparent !important`、`color: ... !important`），
    属于 Phase 2 其他任务的范围，本次不动。

    注意：注释里被剥掉了才计数——否则一句提到 !important 的说明文字就能
    把数字顶上去（本条测试自己的实现就踩过这个坑）。
    """
    css = re.sub(r'/\*.*?\*/', '', _css(), flags=re.S)
    count = css.count('!important')
    assert count <= 68, (
        f'!important 声明有 {count} 处，应 <= 68（Task 2 前 92 → Task 2 后 67 → '
        'Task 3 后 66 → Task 5 +2 条进度条覆盖后实测 68 → '
        'Task 9 Leaflet +5 / -5 净 0，仍是 68 → '
        'Task 12 删掉表格段 3 条压错对象的 color 后实测 65，余量 3）'
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
#
#   A8 / Task 13 已按计划删掉 `transition-property` / `transition-duration` /
#   `transition-timing-function` 三条，本表随之移除对应条目。
#   ⚠️ 只删本表条目会留下一个洞：没有任何断言禁止它们**回潮**。补位的是
#   test_no_blanket_motion_on_the_universal_selector —— 那条正面禁止顶层 `*`
#   声明任何 transition-* / animation-*。改本表前先读它。
MERGED_UNIVERSAL_DECLS = {
    'box-sizing': 'border-box',
    'scrollbar-width': 'thin',
    'scrollbar-color': 'var(--color-accent-strong) var(--color-bg-secondary)',
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
#
# ⚠️ A5 / Task 10 从本表**删掉**了一行：
#    `.config-section .form-control, .config-section .form-select`。
#    那条 (0,2,0) 规则整条被删了 —— 逐条比对过，它的 border-radius / border /
#    background-color / color / font-family / transition 与通用规则一字不差，
#    唯一的差异是 `padding: 0.75rem 1rem`（让配置页控件比首页高 5px）。
#    配置页的背景色现在由通用的 `.form-control, .form-select` (0,1,0) 提供。
#    这不是「删声明骗测试变绿」：Bootstrap 那条 (0,2,0) 的
#    `[data-bs-theme=dark] .form-select` 只声明 --bs-form-select-bg-img、不碰
#    background-color，所以通用规则确实赢得下来。CDP 实测配置页 18 个控件
#    computed background-color 全部是 rgb(28,32,39) = --color-bg-tertiary，
#    与首页一致（记在 p2-task-10-report.md）。
FORM_SELECT_BG_COLORS = {
    '.form-control, .form-select':
        'var(--color-bg-tertiary)',
    '.form-control:focus, .form-select:focus':
        'var(--color-bg-secondary)',
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

    **例外：`::` 伪元素判为「不匹配」(False) 而不是「不支持」(None)。**
    伪元素样式化的是另一个盒子，不参与宿主元素自身的 width / padding 层叠 ——
    `.form-control::file-selector-button { padding: ... }`（A5 / Task 10 新增，
    用来对齐文件选择按钮的负外边距）设的是那颗按钮的内边距，跟 <input> 自己的
    内边距毫无关系。把它当「不支持」会让调用方误报「测试已失效」。
    双冒号是无歧义的伪元素写法；单冒号的老式写法（`:before`）仍走 None 分支，
    响亮失败。
    """
    if re.search(r'[>+~#\[*]', branch):
        return None
    compounds = []
    for part in branch.split():
        if '::' in part:
            return False
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


# Bootstrap 首个带 `[data-bs-theme=dark]` 的版本。
# 5.3.0 的 release note 把「color modes」列为该版本的头号新特性；5.2.x 的构建里
# 全文件搜不到 data-bs-theme（实测：5.3.0 的 bootstrap.css 有 13 处）。
MIN_BOOTSTRAP_VERSION = (5, 3)

# CDN 上两种常见的版号写法：
#   npm 系（jsdelivr / unpkg）：.../bootstrap@5.3.0/dist/css/bootstrap.min.css
#   cdnjs 系：                 .../ajax/libs/bootstrap/5.3.0/css/bootstrap.min.css
_BOOTSTRAP_VERSION_RES = (
    re.compile(r'bootstrap@(\d+)\.(\d+)(?:\.(\d+))?', re.I),
    re.compile(r'/bootstrap/(\d+)\.(\d+)(?:\.(\d+))?/', re.I),
)


def _bootstrap_asset_urls(markup):
    """base.html 里引用 Bootstrap 的全部资源 URL（`<link href>` / `<script src>`）。"""
    out = []
    for name, attrs in _start_tags(markup):
        url = attrs.get('href') if name == 'link' else attrs.get('src') if name == 'script' else None
        if url and 'bootstrap' in url.lower():
            out.append((name, url))
    return out


def test_bootstrap_build_is_new_enough_to_have_dark_theme():
    """base.html 引的 Bootstrap 必须 >= 5.3 —— 否则 data-bs-theme 是个没人读的属性。

    ⚠️ 这条是评审逼出来的，**上一条断言对它完全失明**：把 base.html 里的
    `bootstrap@5.3.0` 改成 `5.2.3`（或任何不含 `[data-bs-theme=dark]` 的构建），
    `<html data-bs-theme="dark">` 立刻退化成一个纯装饰属性 —— 三处漏白原样回来、
    select 箭头掉回 Bootstrap 亮色的 #343a40（对面板底 1.42:1，等于看不见），
    **而全部测试照旧全绿**。

    为什么本次提交之后特别需要它：A3/Task 8 删掉了站内那条硬编码
    `--bs-form-select-bg-img` 的覆盖（因为 `[data-bs-theme=dark] .form-select`
    (0,2,0) 无条件压过它），连带删掉了仅有的两条对箭头**渲染结果**敏感的断言
    （test_form_select_arrow_stroke_matches_palette /
    test_form_select_arrow_has_sufficient_contrast）。删除本身是对的 ——
    那两条钉的是一段已成死代码的规则 —— 但删完之后「深色主题真的生效了」
    这个契约只剩上一条断言在守，而它只看得见 HTML 属性、看不见属性有没有人消费。
    这条把另一半补上：**属性在** + **能读懂这个属性的 Bootstrap 也在**。

    为什么不需要联网（报告初稿在这里做了个假二选一，已订正）：版本号是**字面写在
    base.html 里**的，而本文件早就在读这个文件了。真正守不住的是「Bootstrap 在
    5.3.x 内部把 #adb5bd 改成别的颜色」那种漂移 —— 那才需要解析 CDN 上的 CSS，
    代价大于收益，明确放弃（见 p2-task-8-report.md §10）。

    覆盖范围（诚实说明）：这条守的是「声明的版本号够新」。它保证不了
    「CDN 真的把这个文件送到了浏览器」—— 断网 / CDN 故障时整份 Bootstrap 都不在，
    那属于本项目既有的 CDN 依赖问题（style.css 里 `.row/.col-*` 那段注释已点名），
    不是本断言能覆盖的。CDP 实测脚本自带 `--bs-body-font-family` 非空的自检来兜这一层。
    """
    assets = _bootstrap_asset_urls(_template('base.html'))
    assert assets, (
        'base.html 里找不到任何引用 Bootstrap 的 <link>/<script> —— '
        '要么改用了别的引入方式（本测试已失效，请连同解析逻辑一起改），'
        '要么 Bootstrap 被整个拿掉了'
    )
    problems = []
    for tag, url in assets:
        version = None
        for pattern in _BOOTSTRAP_VERSION_RES:
            m = pattern.search(url)
            if m:
                version = tuple(int(g) for g in m.groups()[:2])
                break
        if version is None:
            problems.append(
                f'<{tag}> {url} —— 解析不出版本号。若是改成了本地 vendor 副本，'
                '请把版本校验换成对该文件内容的检查（例如断言文件里有 '
                '`[data-bs-theme=dark]`），不要直接删掉本断言'
            )
        elif version < MIN_BOOTSTRAP_VERSION:
            problems.append(
                f'<{tag}> {url} —— 版本 {version[0]}.{version[1]} < '
                f'{MIN_BOOTSTRAP_VERSION[0]}.{MIN_BOOTSTRAP_VERSION[1]}，'
                '该构建不含 [data-bs-theme=dark]'
            )
    assert not problems, (
        'base.html 引的 Bootstrap 版本读不懂或太旧 —— '
        '<html data-bs-theme="dark"> 会变成一个没人读的属性，'
        '文件选择按钮 / number 微调箭头 / select 弹层三处漏白全部回潮，'
        'select 箭头掉回 #343a40（对面板底 1.42:1）：\n'
        + '\n'.join('  ' + p for p in problems)
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

# 本 helper 收集的长写属性。
#
# ⚠️ A5 / Task 10 在 width 之外加了纵向那几条（height / min-height / max-height /
#    line-height / font-size）。加它们是为了让「模拟层叠算出控件最终高度」成为可能
#    —— 评审实测：只钉 `--ctl-h` 这个令牌的话，往
#    `.form-control, .form-select` 里加一行 `height: 44px` 全套测试仍然全绿，
#    而 1366x768 下提交按钮回到折叠线以下 43.5px。令牌还在源码里，高度已经不是它了。
#    加这几条不影响既有调用方：取色器那条断言自己按 `wanted` 过滤。
_BOX_LONGHANDS = _PADDING_SIDES + (
    'width', 'height', 'min-height', 'max-height', 'line-height', 'font-size',
)


def _expanded_box_decls(body):
    """规则体 -> [(属性名, 值, 是否!important), ...]，保持声明顺序，
    并把 `padding` 简写展开成四条长写。

    只处理 _BOX_LONGHANDS 里那几条。和 `_right_padding_in_body` 同一个
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
        elif name in _BOX_LONGHANDS:
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


# --------------------------------------------------------------------------
# A4 / Task 9：Leaflet 控件主题化 + 绘制提示汉化
#
# 本节守三个「改完看着对、浏览器里是错的」的坑，每一条都对应一次 CDP 实测：
#   坑 1  background 简写 → 雪碧图被一起重置 → 三个没有图标的空白按钮
#   坑 2  给 <a> 同时设深色底和 filter: invert(1) → 两者抵消 → 净效果为零
#   坑 3  .leaflet-* 容器是 <div>，会被 style.css 的兜底重置压成透明
# --------------------------------------------------------------------------

# 绘制工具条按钮 <a> 上的雪碧图（leaflet.draw 1.0.4）：
#   .leaflet-draw-toolbar a { background-image: url(images/spritesheet.svg);
#                             background-repeat: no-repeat;
#                             background-size: 300px 30px; }
#   .leaflet-draw-toolbar .leaflet-draw-draw-rectangle { background-position: -62px -2px }
# 三个图标全靠 background-position 在同一张图上取不同格子。
_SPRITE_BUTTON_CLASSES = (
    'leaflet-draw-draw-rectangle',
    'leaflet-draw-edit-edit',
    'leaflet-draw-edit-remove',
)
# 按钮 <a> 的祖先/自身身上的类 —— 任何一个都能当后代选择器的前缀命中那三个按钮。
# 实测 DOM（leaflet.draw 1.0.4 + leaflet 1.9.4）：
#   <div class="leaflet-top leaflet-left">                     leaflet.css 的 pane
#     <div class="leaflet-draw leaflet-control">               ← leaflet-draw / leaflet-control
#       <div class="leaflet-draw-section">                     ← leaflet-draw-section
#         <div class="leaflet-draw-toolbar leaflet-bar">       ← 两个类在同一个 div 上
#           <a class="leaflet-draw-draw-rectangle">
#
# 只认 leaflet-bar / leaflet-draw-toolbar 是不够的：`.leaflet-draw a`（leaflet.draw
# 自己就用了这个写法）、`.leaflet-draw-section a`、`.leaflet-control a` 同样命中
# 那三个按钮，在它们身上写 `background:` 简写照样把雪碧图清光。
_TOOLBAR_CONTAINER_CLASSES = (
    'leaflet-bar',
    'leaflet-draw-toolbar',
    'leaflet-draw-section',
    'leaflet-draw',
    'leaflet-control',
    'leaflet-left',
    'leaflet-top',
)


def _class_tokens(text):
    """选择器片段里的类名 token 集合。

    必须按 token 比而不是子串比：`'leaflet-draw' in '.leaflet-draw-actions'`
    是真的，但 `.leaflet-draw-actions a` 命中的是操作条按钮、**不是**雪碧图按钮，
    在它身上写 background-color 完全合法。子串匹配会把它误判成违规。
    """
    return set(re.findall(r'\.([\w-]+)', text))


def _matches_sprite_button(part):
    """这一支选择器会不会命中带雪碧图的绘制按钮 <a>？

    两种命中方式：
      1. 直接点名按钮类（`.leaflet-draw-edit-remove` /
         `.leaflet-draw-toolbar .leaflet-draw-draw-rectangle`）
      2. 后代选择器最后一节是 `a`，且祖先部分出现过按钮 <a> 真实祖先链上的类
         （`.leaflet-bar a:hover` / `.leaflet-draw a` / `.leaflet-control a` ...）

    只查「`.leaflet-draw-toolbar a`」这一种字面写法是不够的 ——
    见 _TOOLBAR_CONTAINER_CLASSES 的注释。
    """
    if _class_tokens(part) & set(_SPRITE_BUTTON_CLASSES):
        return True
    compounds = part.split()
    if len(compounds) < 2:
        return False
    last = compounds[-1]
    # 末节必须是 a（允许挂类和伪类：a、a:hover、a.leaflet-disabled:hover）
    if not re.fullmatch(r'a(?:[.:][\w-]+(?:\([^)]*\))?)*', last):
        return False
    ancestors = _class_tokens(' '.join(compounds[:-1]))
    return bool(ancestors & set(_TOOLBAR_CONTAINER_CLASSES))


def _sprite_button_rules(css):
    """全部可能命中绘制按钮 <a> 的规则，含 @media 内、伪类、分组写法。"""
    return [
        (part, body)
        for sel, body in _rules(css)
        for part in _selector_parts(_norm_selector(sel))
        if _matches_sprite_button(part)
    ]


# 与 _ARROW_KILLING_IMAGE_VALUES 同理：这些值的计算值都是 none，图标都会消失。
_ICON_KILLING_IMAGE_VALUES = _ARROW_KILLING_IMAGE_VALUES


def test_leaflet_draw_buttons_never_use_background_shorthand():
    """坑 1：命中绘制按钮 <a> 的规则不许用 `background:` 简写，也不许把
    `background-image` 置成 none 等价物。

    `background` 是简写，写一次会把 background-image / -position / -repeat /
    -size / -clip 全部重置成初始值。leaflet.draw 的矩形 / 编辑 / 垃圾桶三个
    图标全靠这几个子属性从同一张 spritesheet.svg 上取格子画出来，简写一写
    就是**三个没有图标的空白按钮**。

    这不是假想：C1/Task 4 的整个任务就是在修同一类问题——`background` 简写
    把 Bootstrap select 的箭头 SVG 清掉了。本条是同一契约在 Leaflet 上的复制。

    变异实验（报告里有输出）：把 style.css 里
        .leaflet-bar a, .leaflet-bar a.leaflet-disabled { background-color: transparent }
    改成 `background: transparent`，本条立刻变红；而只看 computed
    `backgroundImage !== "none"` 的写法会**通过**——因为简写后的计算值是
    `none`，字符串比较是过了，但屏幕上图标已经没了。所以这里查的是源码形态。
    """
    rules = _sprite_button_rules(_css())
    assert rules, (
        '一条命中绘制按钮 <a> 的规则都没匹配到——选择器写法变了，本测试已失效'
    )
    offenders = []
    for sel, body in rules:
        decls = _decl_map(body)
        if 'background' in decls:
            offenders.append(
                f'{sel} {{ background: {decls["background"]} }}  ← 简写会连 spritesheet 一起重置'
            )
        img = decls.get('background-image')
        if img is not None:
            value = _IMPORTANT_RE.sub('', img).strip().lower()
            if value in _ICON_KILLING_IMAGE_VALUES:
                offenders.append(
                    f'{sel} {{ background-image: {img} }}  ← 计算值为 none，直接去掉了图标'
                )
    assert not offenders, (
        '绘制工具条的三个图标会变成空白按钮（矩形 / 编辑 / 垃圾桶）。\n'
        '请改用 background-color，只覆盖颜色、把 background-image 留给 leaflet.draw：\n'
        + '\n'.join('  ' + o for o in offenders)
    )


def _transparent(value):
    """这个 background-color 的值等价于「透明」吗？"""
    v = _IMPORTANT_RE.sub('', value or '').strip().lower()
    if v in ('transparent', 'initial', 'unset', 'revert', 'revert-layer'):
        return True
    m = re.fullmatch(r'rgba?\(\s*[\d.]+[\s,]+[\d.]+[\s,]+[\d.]+\s*[,/]\s*0*(?:\.0+)?\s*\)', v)
    return bool(m)


def test_leaflet_draw_button_is_transparent_where_it_is_inverted():
    """坑 2：戴着 `filter: invert(...)` 的那个元素，静息态背景必须是透明的。

    雪碧图是深灰 (#464646) 画在透明底上的，为**白底**按钮设计。深色界面上
    要么整张图反色、要么图标看不见。反色只能对元素整体生效，所以设计是：
    深色底放在 `.leaflet-bar` 容器上，`<a>` 自己保持透明，filter 就只作用在
    图标上。

    如果给 `<a>` **同时**设深色背景和 invert(1)，深色背景会被一起反色成浅色，
    净效果等于什么都没改——按钮还是浅底，只是图标从深灰变成了浅灰（更糟）。

    本条断言：
      (a) 确实有规则给绘制按钮 <a> 上了 invert 滤镜；
      (b) 所有命中该 <a> 的**静息态**（不带 :hover/:focus/:active）规则里，
          background-color 一律是透明等价物；
      (c) 容器 (.leaflet-bar / .leaflet-draw-toolbar) 反过来必须有不透明背景。
          少了 (c)，把 (b) 做到极致的结果是「按钮透明、容器也透明」——
          断言全绿而界面上根本没有深色工具条。
    """
    css = _css()
    button_rules = _sprite_button_rules(css)

    inverted = [
        (sel, body) for sel, body in button_rules
        if 'invert(' in (_decl_map(body).get('filter') or '')
    ]
    assert inverted, (
        '没有任何规则给绘制按钮上 filter: invert(...)——'
        '深灰雪碧图在深色底上会看不见，或者本测试已失效'
    )

    opaque = []
    for sel, body in button_rules:
        if re.search(r':(hover|focus|active|focus-visible|focus-within)\b', sel):
            continue
        bg = _decl_map(body).get('background-color')
        if bg is not None and not _transparent(bg):
            opaque.append(f'{sel} {{ background-color: {bg} }}')
    assert not opaque, (
        '绘制按钮 <a> 同时有 filter: invert(1) 和不透明背景色，两者互相抵消、净效果为零。\n'
        '深色底应该放在 .leaflet-bar 容器上，<a> 保持 transparent：\n'
        + '\n'.join('  ' + o for o in opaque)
    )

    container = [
        (part, body) for sel, body in _rules(css)
        for part in _selector_parts(_norm_selector(sel))
        if re.fullmatch(r'\.(?:%s)' % '|'.join(_TOOLBAR_CONTAINER_CLASSES), part)
        and not _transparent(_decl_map(body).get('background-color') or 'transparent')
    ]
    assert container, (
        '.leaflet-bar / .leaflet-draw-toolbar 容器没有任何不透明背景色。\n'
        '按钮 <a> 是透明的，底色全靠容器——容器一没背景，工具条在深色地图上就是「隐形」的。'
    )


# 这四个 Leaflet 元素都是 <div>：
#   .leaflet-bar / .leaflet-draw-toolbar  工具条容器（同一个 div 上的两个类）
#   .leaflet-control-attribution          右下角出处标注
#   .leaflet-draw-tooltip                 跟随鼠标的绘制提示条
# 它们都会被 style.css 的兜底重置 `div:not(...)...{background:transparent}` 命中。
_LEAFLET_DIV_CONTROLS = (
    'leaflet-bar',
    'leaflet-draw-toolbar',
    'leaflet-control-attribution',
    'leaflet-draw-tooltip',
)


def test_leaflet_div_controls_survive_the_blanket_div_reset():
    """坑 3：这四个 Leaflet 容器的背景色必须带 !important，且不许进白名单。

    style.css 里有一条兜底重置
        div:not(.card):not(.modal-content):not(.alert)... { background: transparent }
    特异度 (0,11,1)。上面四个类全是 <div> 且都不在那串 :not() 白名单里，所以
    任何 `.leaflet-bar { background-color: X }`(0,1,0) 都打不过它。

    实测证据（Chrome 148，CDP，改前）：
        .leaflet-bar                  backgroundColor = rgba(0, 0, 0, 0)
        .leaflet-control-attribution  backgroundColor = rgba(0, 0, 0, 0)
        .leaflet-draw-tooltip         backgroundColor = rgba(0, 0, 0, 0)
    三者的背景**在源码里写着、在浏览器里全是透明**。绘制提示条尤其严重：
    白字直接飘在地图瓦片上。而只读 CSS 源码算色值的断言对此完全绿灯。

    本条同时钉住「不要用加白名单来解决」：白名单模式本身是待处理的结构债，
    往里塞 Leaflet 类只会让那串 :not() 继续膨胀。这里用 !important。

    覆盖范围（诚实说明）：兜底重置若被整条删掉，本条第一段就没有对象可查、
    自然通过——那时 !important 也确实不再必要，不算静默失效。第二段
    （必须带 !important）无条件执行。
    """
    css = _css()
    blankets = [
        sel for sel, body, at_ctx in _rules_ctx(css)
        if not at_ctx
        for part in _selector_parts(sel)
        if _BLANKET_DIV_RESET.fullmatch(part)
        and 'transparent' in (_decl_map(body).get('background') or
                              _decl_map(body).get('background-color') or '')
    ]
    leaked = []
    for sel in blankets:
        excluded = set(re.findall(r':not\(\s*([^)]*?)\s*\)', sel))
        for cls in _LEAFLET_DIV_CONTROLS:
            if '.' + cls in excluded:
                leaked.append(f'.{cls} 被塞进了 {sel[:50]}... 的 :not() 白名单')
    assert not leaked, (
        '不要靠扩张 div:not(...) 白名单来给 Leaflet 控件上背景——那串白名单本身是结构债。\n'
        '用 !important：\n' + '\n'.join('  ' + p for p in leaked)
    )

    declared = {}
    for sel, body in _rules(css):
        for part in _selector_parts(_norm_selector(sel)):
            for cls in _LEAFLET_DIV_CONTROLS:
                # 只认「选择器整支就是这一个类」的规则，`.leaflet-bar a` 不算
                if part == '.' + cls:
                    bg = _decl_map(body).get('background-color')
                    if bg is not None:
                        declared.setdefault(cls, []).append((part, bg))

    problems = []
    for cls in _LEAFLET_DIV_CONTROLS:
        hits = declared.get(cls, [])
        if not hits:
            # .leaflet-draw-toolbar 与 .leaflet-bar 在同一个 div 上，
            # 由 .leaflet-bar 那条覆盖即可，不强求两个类各写一遍。
            if cls == 'leaflet-draw-toolbar' and declared.get('leaflet-bar'):
                continue
            problems.append(f'.{cls} 没有任何 background-color 声明——它的底色是兜底重置给的透明')
            continue
        if not any(_IMPORTANT_RE.search(bg) for _part, bg in hits):
            problems.append(
                f'.{cls} 的 background-color 没带 !important：'
                + '、'.join(f'{p} {{ background-color: {b} }}' for p, b in hits)
            )
    assert not problems, (
        'Leaflet 控件的背景色会被 div 兜底重置 (0,11,1) 压成透明（源码里有、浏览器里没有）：\n'
        + '\n'.join('  ' + p for p in problems)
    )


# --------------------------------------------------------------------------
# 汉化：L.drawLocal 的键名不许写错
# --------------------------------------------------------------------------

# leaflet.draw 1.0.4 的 L.drawLocal 全部叶子键路径（38 条）。
# 生成方式：把 CDN 上 leaflet.draw.js 里的 `L.drawLocal={...}` 对象字面量
# 括号配对切出来、转成 JSON、递归收集叶子路径。不是手抄的。
#
# 为什么需要这张快照：给一个不存在的属性赋值在 JS 里完全合法，
# `L.drawLocal.edit.toolbar.buttons.editDisable = '...'`（少个 d）不会报错、
# 不会有警告，只会**静默不生效**——按钮提示还是英文，而所有测试全绿。
_LEAFLET_DRAW_LOCAL_KEYS_1_0_4 = frozenset({
    'L.drawLocal.draw.handlers.circle.radius',
    'L.drawLocal.draw.handlers.circle.tooltip.start',
    'L.drawLocal.draw.handlers.circlemarker.tooltip.start',
    'L.drawLocal.draw.handlers.marker.tooltip.start',
    'L.drawLocal.draw.handlers.polygon.tooltip.cont',
    'L.drawLocal.draw.handlers.polygon.tooltip.end',
    'L.drawLocal.draw.handlers.polygon.tooltip.start',
    'L.drawLocal.draw.handlers.polyline.error',
    'L.drawLocal.draw.handlers.polyline.tooltip.cont',
    'L.drawLocal.draw.handlers.polyline.tooltip.end',
    'L.drawLocal.draw.handlers.polyline.tooltip.start',
    'L.drawLocal.draw.handlers.rectangle.tooltip.start',
    'L.drawLocal.draw.handlers.simpleshape.tooltip.end',
    'L.drawLocal.draw.toolbar.actions.text',
    'L.drawLocal.draw.toolbar.actions.title',
    'L.drawLocal.draw.toolbar.buttons.circle',
    'L.drawLocal.draw.toolbar.buttons.circlemarker',
    'L.drawLocal.draw.toolbar.buttons.marker',
    'L.drawLocal.draw.toolbar.buttons.polygon',
    'L.drawLocal.draw.toolbar.buttons.polyline',
    'L.drawLocal.draw.toolbar.buttons.rectangle',
    'L.drawLocal.draw.toolbar.finish.text',
    'L.drawLocal.draw.toolbar.finish.title',
    'L.drawLocal.draw.toolbar.undo.text',
    'L.drawLocal.draw.toolbar.undo.title',
    'L.drawLocal.edit.handlers.edit.tooltip.subtext',
    'L.drawLocal.edit.handlers.edit.tooltip.text',
    'L.drawLocal.edit.handlers.remove.tooltip.text',
    'L.drawLocal.edit.toolbar.actions.cancel.text',
    'L.drawLocal.edit.toolbar.actions.cancel.title',
    'L.drawLocal.edit.toolbar.actions.clearAll.text',
    'L.drawLocal.edit.toolbar.actions.clearAll.title',
    'L.drawLocal.edit.toolbar.actions.save.text',
    'L.drawLocal.edit.toolbar.actions.save.title',
    'L.drawLocal.edit.toolbar.buttons.edit',
    'L.drawLocal.edit.toolbar.buttons.editDisabled',
    'L.drawLocal.edit.toolbar.buttons.remove',
    'L.drawLocal.edit.toolbar.buttons.removeDisabled',
})

# 本项目 UI 上真正会出现的那些键（map.js 只启用了 rectangle）。
# 少翻其中任何一条，界面上就有一处中英混排。
_REQUIRED_LOCALE_KEYS = frozenset({
    # 绘制：按钮标题、跟随鼠标的提示条、绘制中的「取消」
    'L.drawLocal.draw.toolbar.buttons.rectangle',
    'L.drawLocal.draw.handlers.rectangle.tooltip.start',
    'L.drawLocal.draw.handlers.simpleshape.tooltip.end',
    'L.drawLocal.draw.toolbar.actions.title',
    'L.drawLocal.draw.toolbar.actions.text',
    # 编辑 / 删除：按钮标题（含**首屏默认的禁用态**标题）
    'L.drawLocal.edit.toolbar.buttons.edit',
    'L.drawLocal.edit.toolbar.buttons.editDisabled',
    'L.drawLocal.edit.toolbar.buttons.remove',
    'L.drawLocal.edit.toolbar.buttons.removeDisabled',
    # 编辑 / 删除模式弹出的操作条（删除模式实测是「保存 / 取消 / 全部清除」三个）
    'L.drawLocal.edit.toolbar.actions.save.title',
    'L.drawLocal.edit.toolbar.actions.save.text',
    'L.drawLocal.edit.toolbar.actions.cancel.title',
    'L.drawLocal.edit.toolbar.actions.cancel.text',
    'L.drawLocal.edit.toolbar.actions.clearAll.title',
    'L.drawLocal.edit.toolbar.actions.clearAll.text',
    # 编辑 / 删除模式的提示条
    'L.drawLocal.edit.handlers.edit.tooltip.text',
    'L.drawLocal.edit.handlers.edit.tooltip.subtext',
    'L.drawLocal.edit.handlers.remove.tooltip.text',
})

_CJK_RE = re.compile(r'[一-鿿]')

# `L.drawLocal.a.b.c = '中文';`
_DRAW_LOCAL_ASSIGN_RE = re.compile(
    r"(L\.drawLocal(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\s*=\s*(['\"])(.*?)\2"
)


def _draw_locale_assignments():
    """map.js 里 `L.drawLocal.x.y.z = '...'` 的 {键路径: 文案}（已剥注释）。"""
    src = _js('map.js')
    # 先剥掉注释，否则示例代码 / 说明文字里的键路径会被当成真赋值
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    src = re.sub(r'(?m)//.*$', '', src)
    return {m.group(1): m.group(3) for m in _DRAW_LOCAL_ASSIGN_RE.finditer(src)}


def test_draw_locale_keys_exist_in_pinned_build():
    """map.js 写的每个 L.drawLocal 键，在 leaflet.draw 1.0.4 里都必须真实存在。

    这是本次汉化最容易翻车的地方：**键名写错不报错**。
    `L.drawLocal.edit.toolbar.buttons.removeDisable = '...'`（少个 d）是完全
    合法的 JS——给对象加了个没人读的新属性而已。按钮提示照旧是
    "No layers to delete"，控制台一声不吭，人工不逐个 hover 根本发现不了。

    所以这里拿 _LEAFLET_DRAW_LOCAL_KEYS_1_0_4 这份从 CDN 构建里机械提取的
    键路径快照做白名单校验。配套的
    test_leaflet_draw_build_matches_the_locale_key_snapshot 保证 base.html
    引的还是这个版本，快照不会悄悄过期。
    """
    assigned = _draw_locale_assignments()
    assert assigned, (
        "map.js 里解析不到任何 `L.drawLocal.x.y = '...'` 赋值 —— "
        '要么汉化被删了，要么改成了别名写法（`const d = L.drawLocal.draw`），'
        '后者本测试静态看不见，请写全路径'
    )
    bogus = sorted(k for k in assigned if k not in _LEAFLET_DRAW_LOCAL_KEYS_1_0_4)
    assert not bogus, (
        'leaflet.draw 1.0.4 里没有这些键，赋值不会报错、只会静默不生效：\n'
        + '\n'.join(f'  {k} = {assigned[k]!r}' for k in bogus)
    )


def test_draw_locale_covers_every_user_visible_string():
    """UI 上真会出现的那些文案必须都翻了，而且值确实是中文。

    两段缺一不可：
      - 只查「键在不在」：把值改成 `= 'Draw a rectangle'` 照样绿，等于没翻。
      - 只查「有中文」：漏翻 editDisabled / removeDisabled 这种**首屏默认态**
        的键，剩下的中文照样让测试绿，而用户打开页面看到的两个按钮提示是英文。
    """
    assigned = _draw_locale_assignments()
    missing = sorted(_REQUIRED_LOCALE_KEYS - set(assigned))
    assert not missing, (
        '这些界面上会出现的文案没有汉化（页面是 lang="zh-CN"）：\n'
        + '\n'.join('  ' + k for k in missing)
    )
    not_chinese = sorted(
        f'{k} = {v!r}' for k, v in assigned.items() if not _CJK_RE.search(v)
    )
    assert not not_chinese, (
        'L.drawLocal 这些键赋的不是中文，等于没汉化：\n'
        + '\n'.join('  ' + s for s in not_chinese)
    )


def test_draw_locale_is_applied_before_the_control_is_constructed():
    """汉化必须在 `new L.Control.Draw(...)` **之前**跑。

    Leaflet.draw 的按钮 title 是在 Toolbar.addToolbar() 里一次性从
    L.drawLocal 读走、写进 DOM 的。控件建完之后再改 L.drawLocal，
    对已经渲染出来的按钮没有任何影响——顺序写反了，测试若只查
    「赋值语句存在」依然全绿，而界面上按钮提示仍是英文。
    """
    body = _js_function_body(_js('map.js'), 'initMap')
    # 必须先剥注释再定位：initMap 里那句「必须在 new L.Control.Draw 之前」的
    # 说明注释本身就含这段字面量，不剥的话 find() 命中的是注释、位置比真正的
    # 构造调用还靠前，断言会**假红**。（本条第一次跑就是这么红的。）
    body = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
    body = re.sub(r'(?m)//.*$', '', body)
    call = body.find('localizeDrawControl(')
    ctor = body.find('new L.Control.Draw')
    assert call != -1, (
        'initMap 里没有调用 localizeDrawControl() —— 汉化函数写了但没人调，'
        '所有 L.drawLocal 断言都会绿，界面照旧是英文'
    )
    assert ctor != -1, (
        'initMap 里找不到 `new L.Control.Draw` —— 本测试已失效，请更新'
    )
    assert call < ctor, (
        f'localizeDrawControl() 在 initMap 里的位置（{call}）晚于 '
        f'new L.Control.Draw（{ctor}）。按钮 title 是建控件时一次性读走的，'
        '之后再改 L.drawLocal 不会回写 DOM。'
    )


# 键名快照是对着这个版本提取的。cdnjs 与 npm 两种 URL 写法都认。
LEAFLET_DRAW_PINNED_VERSION = (1, 0, 4)
_LEAFLET_DRAW_VERSION_RES = (
    re.compile(r'leaflet[.-]?draw@(\d+)\.(\d+)\.(\d+)', re.I),
    re.compile(r'/leaflet\.draw/(\d+)\.(\d+)\.(\d+)/', re.I),
)


def test_leaflet_draw_build_matches_the_locale_key_snapshot():
    """base.html 引的 leaflet.draw 必须还是 1.0.4——键名快照才有效。

    ⚠️ 这条是给上面两条汉化断言兜底的，**它们对版本漂移完全失明**：
    把 base.html 的 1.0.4 换成任何重排过 L.drawLocal 结构的版本，
    _LEAFLET_DRAW_LOCAL_KEYS_1_0_4 这份白名单立刻变成一张过期地图 ——
    真正写错的键可能反而在白名单里、真正存在的键反而被判成 bogus，
    而两条断言给出的红/绿都不再对应事实。

    同样的理由，CSS 那边的坑 1 / 坑 2 也钉在这个版本上：
    `background-image: url(images/spritesheet.svg)` + background-position
    取格子、深灰 #464646 描边，都是 1.0.4 的实现细节。

    只查声明的版本号，不联网核对 CDN 真的送了什么——与
    test_bootstrap_build_is_new_enough_to_have_dark_theme 同一权衡。
    """
    urls = []
    for name, attrs in _start_tags(_template('base.html')):
        url = attrs.get('href') if name == 'link' else attrs.get('src') if name == 'script' else None
        if url and 'leaflet' in url.lower() and 'draw' in url.lower():
            urls.append((name, url))
    assert urls, 'base.html 里找不到任何 leaflet.draw 资源引用 —— 本测试已失效'
    problems = []
    for name, url in urls:
        found = None
        for rx in _LEAFLET_DRAW_VERSION_RES:
            m = rx.search(url)
            if m:
                found = tuple(int(g) for g in m.groups())
                break
        if found is None:
            problems.append(f'<{name}> {url} 解析不出版本号')
        elif found != LEAFLET_DRAW_PINNED_VERSION:
            problems.append(
                f'<{name}> {url} 是 {".".join(map(str, found))}，'
                f'键名快照是对着 {".".join(map(str, LEAFLET_DRAW_PINNED_VERSION))} 提取的'
            )
    assert not problems, (
        'leaflet.draw 版本与 _LEAFLET_DRAW_LOCAL_KEYS_1_0_4 快照对不上，'
        '汉化断言的红绿都不再可信：\n' + '\n'.join('  ' + p for p in problems)
    )


def _all_templates():
    """templates/ 下全部 .html 的 (文件名, 内容)。"""
    out = []
    for fn in sorted(os.listdir(_TEMPLATES_DIR)):
        if fn.lower().endswith('.html'):
            with open(os.path.join(_TEMPLATES_DIR, fn), encoding='utf-8') as f:
                out.append((fn, f.read()))
    assert out, 'templates/ 下一个 .html 都没有 —— 本测试已失效'
    return out


def test_no_stylesheet_can_load_after_style_css():
    """全部 `<link rel="stylesheet">` 必须都在 base.html 里、且排在 style.css 之前。

    为什么这条是必需的：本次 Leaflet 主题化里，**打在 <a> 上的那几条规则
    刻意没有用 !important**——它们靠的是「同特异度、源码靠后者胜」赢过
    leaflet.css。例如
        leaflet.css     .leaflet-bar a.leaflet-disabled { background-color:#f4f4f4 }  (0,2,1)
        style.css       .leaflet-bar a, .leaflet-bar a.leaflet-disabled { ...transparent } (0,2,1)
    只要有任何样式表排到 style.css 后面，首屏那两个禁用按钮就会变回
    #f4f4f4 的白块，而所有只读源码的断言全绿。

    ⚠️ 为什么要扫整个 templates/ 而不是只扫 base.html（评审实测出来的盲区）：
    base.html 里 `{% block extra_css %}{% endblock %}` 的位置在 style.css
    **之后**，而「加页面级样式表」在这个代码库里唯一的惯用做法就是覆写这个
    block。变异实验：在 index.html 里写
        {% block extra_css %}<link rel="stylesheet" href="https://example.com/third.css">{% endblock %}
    只扫 base.html 的版本是 **35 passed 全绿**，而那张表会实打实地排在
    style.css 后面、把整套「靠后加载取胜」的方案静默掀翻。

    为什么选「收紧断言」而不是「把 block 挪到 style.css 之前」：
    挪 block 会把「页面级 CSS 覆盖全局 CSS」这个人人都会预期的语义反过来，
    给以后第一个用这个 block 的人埋一个更难查的坑；而且当前**没有任何模板**
    覆写 extra_css（实测只有 extra_js 有人用），挪它换不来任何现实收益。
    收紧断言则把成本放在正确的时刻：真要加页面级样式表时这条会红，
    失败信息里直接给出三个可选做法。

    覆盖范围（诚实说明）：这条查的是 `<link rel=stylesheet>`。
    在 extra_css 里写**内联 `<style>` 块**同样排在 style.css 之后，本条查不到。
    没有一并禁掉是因为内联块是「本项目自己刻意写的页面级样式」，与
    「第三方表静默掀翻层叠顺序」不是同一个风险等级；真要动 Leaflet 控件的人
    会先读到 style.css 段首那三条注释。
    """
    offenders = []
    for fn, markup in _all_templates():
        sheets = [
            attrs.get('href') or ''
            for name, attrs in _start_tags(markup)
            if name == 'link' and (attrs.get('rel') or '').lower() == 'stylesheet'
        ]
        if fn == 'base.html':
            assert sheets, 'base.html 里解析不出任何 <link rel="stylesheet"> —— 本测试已失效'
            own = [i for i, h in enumerate(sheets) if 'style.css' in h]
            assert len(own) == 1, (
                f'base.html 里有 {len(own)} 处引用 style.css，期望恰好 1 处 —— 本测试已失效'
            )
            offenders += [f'base.html: {h}（排在 style.css 之后）' for h in sheets[own[0] + 1:]]
        else:
            # 子模板里的 <link> 只能来自 {% block extra_css %}，而那个 block
            # 在 base.html 里的位置就在 style.css 后面。
            offenders += [f'{fn}: {h}（子模板的样式表一定排在 style.css 之后）' for h in sheets]
    assert not offenders, (
        '有样式表会排到 style.css 后面，Leaflet 覆盖规则（未用 !important 的那些）会被压掉：\n'
        + '\n'.join('  ' + o for o in offenders)
        + '\n可选做法：(a) 把这些样式并进 style.css；(b) 改用内联 <style> 并确认不碰 .leaflet-*；'
          '(c) 给 style.css 的 Leaflet 段落逐条补 !important（会抬高 !important 上界，需登记）。'
    )


# --------------------------------------------------------------------------
# 图标可见性：从源码把整条渲染链算出来
#
# 上面那几条 Leaflet 断言守的都是**形态**（有没有用简写、有没有带 !important）。
# 评审实测出的盲区：把 `brightness(1.25)` 改成 `brightness(0.05)`，图标渲染成
# rgb(14,14,14) 压在 rgb(8,10,15) 上、约 1.04:1 完全看不见，而 8 条形态断言
# **全绿**。本节补上这一半：把源码里的数值代进渲染链，直接算对比度。
#
# 渲染链（每一步都是 CSS 规范定义的确定性运算，不是估的）：
#   1. 雪碧图墨色                     #464646
#   2. 元素自身 filter: invert(1)     255 - v
#   3. 元素自身 filter: brightness(b) v * b，钳到 [0,255]
#   4. 元素自身 opacity α             与容器底色按 α 混合
#   5. 祖先 #map 的 filter            brightness(b2) 再 contrast(c)
#   底色走同一条 5（它也在 #map 里）。
#
# 这个模型是**对着 CDP 实测校准过的**，三个独立数据点全部逐位命中：
#   可用态图标   模型 rgb(216,216,216) / 13.89:1   实测 rgb(216,216,216) / 13.89:1
#   容器底色     模型 rgb(8,10,15)                 实测 rgb(8,10,15)
#   禁用态图标   模型 rgb(122,123,126) / 4.61:1    实测（见报告）
# --------------------------------------------------------------------------

# 雪碧图的墨色。来源：CDN 上 leaflet.draw 1.0.4 的 images/spritesheet.svg，
# 全图 16 处 `style="fill:#464646;fill-opacity:1"`。
#
# ⚠️ 一个容易写错的事实（本报告初版就写错了，评审查实订正）：SVG 里确实有一处
# `<g id="disabled" style="fill:#bbbbbb">`，但组里放的是 `<use xlink:href="#edit">`
# / `<use xlink:href="#remove">`，而被引用的两个 group 自带
# `style="fill:#464646;fill-opacity:1"` —— 子元素自己的 fill 压掉了祖先的 #bbbbbb，
# 那个浅灰**一次都没生效**。实测：把四个格子（可用/禁用 × 编辑/删除）分别画到
# 白底 canvas 上读像素，主导非白色全是 rgb(70,70,70)，没有任何 rgb(187,187,187)。
# 所以禁用态和可用态的雪碧图墨色**相同**，禁用的视觉差异必须由主题自己做出来。
SPRITE_INK_HEX = '#464646'

# 图形元素对比度下限。与 --color-neutral 注释、
# test_progress_bar_fill_has_sufficient_contrast 用的是同一条项目标准（WCAG 1.4.11）。
ICON_MIN_CONTRAST = 3.0


def _filter_ops(value):
    """`invert(1) brightness(1.25)` -> {'invert': 1.0, 'brightness': 1.25}（同名取乘积）。"""
    out = {}
    for name, arg in re.findall(r'([a-z-]+)\(\s*([^)]*)\s*\)', (value or '').lower()):
        arg = arg.strip()
        if arg.endswith('%'):
            num = float(arg[:-1]) / 100
        else:
            try:
                num = float(arg)
            except ValueError:
                continue
        out[name] = out.get(name, 1.0) * num
    return out


def _apply_filter(rgb, ops):
    """按 CSS filter 规范顺序应用 invert / brightness / contrast（本项目只用到这三个）。"""
    out = []
    for v in rgb:
        if 'invert' in ops:
            amt = ops['invert']
            v = v * (1 - amt) + (255 - v) * amt
        if 'brightness' in ops:
            v = v * ops['brightness']
        if 'contrast' in ops:
            v = (v / 255 - 0.5) * ops['contrast'] * 255 + 127.5
        out.append(max(0.0, min(255.0, v)))
    return tuple(out)


def _leaflet_button_state(css, disabled):
    """绘制按钮在指定状态下的 (filter ops, opacity)。

    按 CSS 层叠取值：`.leaflet-draw-toolbar a` 提供基线，
    `.leaflet-draw-toolbar a.leaflet-disabled`（特异度更高）在禁用态覆盖它。
    某个属性没被覆盖就沿用基线 —— 这正是「禁用态只声明 opacity、filter 继续用
    基线那条」能成立的原因。
    """
    ops, opacity = None, 1.0
    for sel, body in _rules(css):
        for part in _selector_parts(_norm_selector(sel)):
            if re.search(r':(hover|focus|active)', part):
                continue
            tokens = _class_tokens(part)
            if 'leaflet-draw-toolbar' not in tokens or not part.split()[-1].startswith('a'):
                continue
            is_disabled_rule = 'leaflet-disabled' in tokens
            if is_disabled_rule and not disabled:
                continue
            decls = _decl_map(body)
            if 'filter' in decls:
                ops = _filter_ops(_IMPORTANT_RE.sub('', decls['filter']))
            if 'opacity' in decls:
                opacity = float(_IMPORTANT_RE.sub('', decls['opacity']).strip())
    return ops, opacity


def test_leaflet_draw_icon_stays_visible_through_the_map_filter():
    """把源码里的数值代进渲染链，算出来的图标对比度必须 >= 3:1。

    这是唯一一条对「图标看不看得见」真正敏感的断言。上面几条守的是形态
    （简写 / !important / transparent），对**数值改坏**完全失明：
    `brightness(1.25)` -> `brightness(0.05)` 会让图标渲染成 rgb(14,14,14) 压在
    rgb(8,10,15) 上（约 1.04:1，人眼完全看不见），而那 8 条断言一条都不红。

    额外还断言「可用态必须比禁用态明显更强」——否则把禁用态的 opacity 调到 1，
    禁用/可用一模一样，首屏那两个按钮看起来是能点的，而对比度断言照样绿。

    模型来源与校准见本节顶部注释：三个独立 CDP 数据点逐位命中，不是估算。
    """
    css = _css()

    # 容器底色（渲染前）
    bg_hex = _palette_var(css, '--color-bg-secondary')
    assert re.fullmatch(r'#[0-9a-f]{6}', bg_hex), (
        f'--color-bg-secondary 是 {bg_hex!r}，本测试只会算 6 位 hex —— 已失效，请更新'
    )
    container = _hex_to_rgb(bg_hex)

    # 工具条容器真的用了这个变量吗？不然算的是一个没人用的颜色
    holder = [
        body for sel, body in _rules(css)
        for part in _selector_parts(_norm_selector(sel))
        if part == '.leaflet-bar'
    ]
    assert holder and any('--color-bg-secondary' in (_decl_map(b).get('background-color') or '')
                          for b in holder), (
        '.leaflet-bar 的 background-color 不再是 var(--color-bg-secondary) —— '
        '本测试算的底色和实际渲染的不是一回事，已失效，请更新'
    )

    # 祖先 #map 的 filter（作用于整个 .leaflet-container，包括所有控件）
    map_rules = [
        _decl_map(body).get('filter') for sel, body, at_ctx in _rules_ctx(css)
        if not at_ctx and '#map' in _selector_parts(_norm_selector(sel))
    ]
    map_ops = _filter_ops(next((f for f in map_rules if f), '') or '')

    rendered_bg = _apply_filter(container, map_ops)
    lum_bg = _relative_luminance(rendered_bg)

    results = {}
    for label, disabled in (('可用态', False), ('禁用态', True)):
        ops, opacity = _leaflet_button_state(css, disabled)
        assert ops is not None, (
            f'{label}：找不到 .leaflet-draw-toolbar a 的 filter 声明 —— 本测试已失效'
        )
        assert ops.get('invert', 0) >= 1.0, (
            f'{label}：filter 里没有 invert(1)。雪碧图墨色是 {SPRITE_INK_HEX}（深灰，'
            f'为白底按钮画的），不反色就是深灰压在近黑的容器上，图标看不见。'
        )
        ink = _apply_filter(_hex_to_rgb(SPRITE_INK_HEX), ops)
        # opacity：与容器底色混合（都还在 #map 的 filter 之前）
        mixed = tuple(ink[i] * opacity + container[i] * (1 - opacity) for i in range(3))
        rendered = _apply_filter(mixed, map_ops)
        lum = _relative_luminance(rendered)
        hi, lo = max(lum, lum_bg), min(lum, lum_bg)
        results[label] = {
            'contrast': (hi + 0.05) / (lo + 0.05),
            'rendered': tuple(round(v) for v in rendered),
            'filter': ops, 'opacity': opacity,
        }

    problems = [
        f'{label}：算出来只有 {r["contrast"]:.2f}:1（低于 {ICON_MIN_CONTRAST}:1）。'
        f'图标渲染成 rgb{r["rendered"]}，容器渲染成 rgb{tuple(round(v) for v in rendered_bg)}；'
        f'filter={r["filter"]}, opacity={r["opacity"]}'
        for label, r in results.items() if r['contrast'] < ICON_MIN_CONTRAST
    ]
    assert not problems, (
        '绘制工具条的图标在深色底上看不见了（矩形 / 编辑 / 垃圾桶）：\n'
        + '\n'.join('  ' + p for p in problems)
    )

    enabled, disabled_r = results['可用态']['contrast'], results['禁用态']['contrast']
    assert enabled >= disabled_r * 1.5, (
        f'可用态 {enabled:.2f}:1 与禁用态 {disabled_r:.2f}:1 差距不足 1.5 倍，'
        '首屏禁用的「编辑」「删除」看起来会像是能点的。\n'
        '注意 leaflet.draw 的禁用雪碧图格子与可用格子**像素级相同**'
        '（它 SVG 里那层 #bbbbbb 被 <use> 引用目标自带的 fill 压掉了，从未生效），'
        '所以这个差异只能由本样式表自己做出来。'
    )


# 被本次覆盖掉的上游颜色。删掉我们的覆盖规则 -> 静默回落到这些值。
# 评审实测：这两条整条删掉，8 条形态断言全绿。
_LEAFLET_UPSTREAM_FALLBACKS = {
    # 选择器: (必须声明的属性, 上游值, 回落后的后果)
    '.leaflet-draw-actions a': (
        'background-color', '#919187 (leaflet.draw.css)',
        '绘制/编辑操作条整条退回出厂橄榄灰 rgb(145,145,135)，在深色界面上是一块脏色',
    ),
    '.leaflet-control-attribution a': (
        'color', '#0078A8 (leaflet.css `.leaflet-container a`)',
        '出处标注里的链接退回 Leaflet 蓝，在我们的深色底上约 3.9:1，低于 AA 的 4.5:1',
    ),
}


def test_leaflet_upstream_colors_are_all_replaced():
    """这几条覆盖规则不许消失——删掉就静默回落到 Leaflet 出厂色。

    与上面那些禁止性断言配对的**存在性**契约（同 test_form_select_still_
    declares_its_background_color 的思路）：只有禁止性断言时，「把规则整条删掉」
    是让测试变绿的合法手段，而屏幕上是回落到第三方默认色。
    """
    rules = _rules(_css())
    problems = []
    for sel, (prop, upstream, consequence) in _LEAFLET_UPSTREAM_FALLBACKS.items():
        found = [
            _decl_map(body).get(prop)
            for rsel, body in rules
            for part in _selector_parts(_norm_selector(rsel))
            if part == sel
        ]
        found = [v for v in found if v is not None]
        if not found:
            problems.append(
                f'{sel} 没有声明 {prop}（会回落到上游的 {upstream}）→ {consequence}'
            )
    assert not problems, (
        'Leaflet 出厂颜色没有被覆盖住：\n' + '\n'.join('  ' + p for p in problems)
    )


# --------------------------------------------------------------------------
# A5 / Task 10：密度令牌
#
# 缺陷（Phase 2 视觉基线 + 本任务 CDP 复测）：表单控件高 43.7px、右栏卡片
# 800.3px。1366x768 上框选之后「创建下载任务」按钮的 bottom 在 949.3px ——
# 折叠线以下 181px，用户必须滚动才能提交。43.7px 是消费级落地页的密度；
# 专业 GIS 工具是 QGIS / ArcGIS Pro 22–26px、VS Code 输入框 26px。
#
# 本节守三件事：
#   1. 令牌之间的算术自洽（否则 min-height 和内容盒子打架）
#   2. 没有悬空的 var() 引用（这是「改了没反应」的头号成因）
#   3. 令牌真的被控件规则消费（否则令牌是摆设，密度写死在别处）
# --------------------------------------------------------------------------

# 控件总高上界。28px 是本任务落地值；30px 是留给后续微调的天花板 ——
# 依据是同类专业工具的实测区间（QGIS / ArcGIS Pro 22–26px、VS Code 输入框 26px），
# 以及 1366x768 的垂直预算（见 test_control_density_tokens_are_self_consistent
# 的 docstring）。
CONTROL_HEIGHT_MAX_PX = 30

# 表单字段之间的纵向间距（.mb-3）上界。改前 12px。
FIELD_GAP_MAX_PX = 8


def _custom_property_raw(css, name):
    """某个自定义属性的原始值文本，未定义返回 None。

    ⚠️ 用正则而不是 `_rules_ctx()` 找 `:root`：文件开头的
    `@import url('...');` 是一条**以分号结尾的 at 语句**，`_rules_ctx` 的花括号
    深度扫描不认分号，会把它连同后面的 `:root` 一起当成选择器
    （实测扫出来的选择器是 `@import url('...'); :root`，按 `== ':root'` 找是 0 条）。
    这与 `_palette_var` 同一个理由、同一个写法。先剥注释，避免注释里提到
    `--ctl-h` 的说明文字被当成定义。
    """
    stripped = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    m = re.search(re.escape(name) + r'\s*:\s*([^;]+);', stripped)
    return None if m is None else m.group(1).strip()


def _resolve_length_px(css, value, _depth=0):
    """`28px` / `1.75rem` / `var(--ctl-h)` -> px。解析不了返回 None。

    只跟一层层 `var()`，不支持 `var(--x, fallback)` 和 `calc()` —— 那两种返回
    None，让调用方报「本测试已失效」而不是静默用一个猜出来的数字。
    """
    if value is None:
        return None
    value = _IMPORTANT_RE.sub('', value).strip()
    if re.fullmatch(r'0+(\.0+)?', value):
        return 0.0                           # 无单位的 0 是合法 CSS
    m = re.fullmatch(r'var\(\s*(--[-\w]+)\s*\)', value)
    if m:
        if _depth > 4:
            return None                      # 自引用/环，别死循环
        return _resolve_length_px(css, _custom_property_raw(css, m.group(1)), _depth + 1)
    return _length_to_px(value)


def _token_px(css, name):
    """某个自定义属性的值（px）。取不到 / 不是 px 字面量就响亮失败。"""
    raw = _custom_property_raw(css, name)
    assert raw is not None, (
        f'{name} 没有定义。引用一个未定义的自定义属性**不会报错**，'
        '只会让引用它的整条声明失效、静默退回 auto/initial —— 表现为「改了没反应」'
    )
    px = _resolve_length_px(css, raw)
    assert px is not None, (
        f'{name} = {raw!r}，不是 px/rem 字面量，本断言算不了 —— 测试已失效（不是通过）'
    )
    return px


def test_control_density_tokens_are_self_consistent():
    """`--ctl-h` 必须等于它的三个分量算出来的盒子高度。

        2 * --ctl-pad-y + --ctl-line-h + 2 * 边框宽 = --ctl-h
        2 *      3      +      20      + 2 *   1    =    28

    为什么要钉这条等式：`.form-control, .form-select` 同时声明了
    `min-height: var(--ctl-h)` 和 padding / line-height。两边不一致时**不会报错**
    —— 内容盒子矮就留一条底边空白，高就把 min-height 顶穿，两种都是「令牌上写着
    28px、量出来不是 28px」的静默漂移。改任何一个分量都要让等式继续成立。

    ⚠️ 这条断言证明的是「令牌内部自洽 + 控件不高于专业工具区间」，
    **不证明**「1366x768 一屏放得下」—— 后者取决于页面上有多少个字段、
    卡片内边距、四至显示占几行，是文本断言够不着的。那个结论由 CDP 实测背书，
    记在 .superpowers/sdd/p2-task-10-report.md：

        1366x768、已框选（按钮可点的那个状态）
        #createTaskBtn 的 bottom：改前 949.3 -> 改后 715.5（视口 768，余 52.5px）
        未框选：859.3 -> 694

    余量 52.5px 是本任务给后续任务留的。上面两个上界（控件 30px、字段间距 8px）
    就是把这个余量钉住：6 个可见字段，控件每长 1px 吃掉 6px 余量。
    """
    css = _css()
    ctl_h = _token_px(css, '--ctl-h')
    line_h = _token_px(css, '--ctl-line-h')
    pad_y = _token_px(css, '--ctl-pad-y')
    pad_x = _token_px(css, '--ctl-pad-x')
    border = _form_control_border_px(css)

    box = 2 * pad_y + line_h + 2 * border
    assert box == ctl_h, (
        f'密度令牌不自洽：2 * --ctl-pad-y({pad_y:g}) + --ctl-line-h({line_h:g}) + '
        f'2 * 边框({border:g}) = {box:g}px，但 --ctl-h = {ctl_h:g}px。'
        '两者不等时 min-height 与内容盒子会打架，实际高度不是令牌上写的那个数'
    )
    assert ctl_h <= CONTROL_HEIGHT_MAX_PX, (
        f'--ctl-h = {ctl_h:g}px，超过 {CONTROL_HEIGHT_MAX_PX}px 上界。'
        '改前 43.7px 正是这个缺陷（1366x768 上提交按钮在折叠线以下 181px）；'
        '专业 GIS 工具是 22–26px。真要放宽，先按 p2-task-10-report.md 的方法'
        '重测 1366x768 下 #createTaskBtn 的 bottom，并同步这里的上界与理由'
    )
    assert pad_x >= 6, (
        f'--ctl-pad-x = {pad_x:g}px，文字会贴着边框。密度收紧不等于取消内边距'
    )


def test_field_gap_stays_tight():
    """`.mb-3`（表单字段间距）必须走 `--gap-field`，且不超过 8px。

    为什么单独钉：首页表单在默认（地图瓦片）模式下有 6 个字段组，间距每放宽
    1px 就吃掉 6px 的垂直预算。改前是 12px。
    """
    css = _css()
    gap = _token_px(css, '--gap-field')
    assert gap <= FIELD_GAP_MAX_PX, (
        f'--gap-field = {gap:g}px，超过 {FIELD_GAP_MAX_PX}px 上界'
    )
    bodies = [body for sel, body, ctx in _rules_ctx(css) if sel == '.mb-3' and not ctx]
    assert len(bodies) == 1, (
        f'期望恰好 1 条顶层 `.mb-3` 规则，实际 {len(bodies)} 条 —— 本测试已失效'
    )
    mb = _decl_map(bodies[0]).get('margin-bottom', '')
    assert 'var(--gap-field)' in mb, (
        f'.mb-3 的 margin-bottom 是 {mb!r}，没有走 --gap-field。'
        '写死数字的话，改令牌不会影响字段间距，令牌就成了摆设'
    )


# `.form-control, .form-select` 必须消费的密度令牌 -> 它负责的那一维。
#
# 为什么需要这张表：上面那条自洽性断言只看 :root 里的四个数字，**把整段
# padding / min-height 从规则里删掉，它照样全绿**（令牌还在，只是没人用了），
# 而浏览器里控件立刻退回 Bootstrap 的 43.7px。这张表把「令牌存在」和
# 「令牌生效」分开。
DENSITY_TOKEN_CONSUMERS = {
    '--ctl-pad-y': '上下内边距',
    '--ctl-pad-x': '左右内边距',
    '--ctl-line-h': '行高',
    '--ctl-h': '最小高度',
}


def test_form_controls_actually_consume_the_density_tokens():
    """`.form-control, .form-select` 必须引用全部四个控件密度令牌。"""
    bodies = [
        body for sel, body, ctx in _rules_ctx(_css())
        if sel == '.form-control, .form-select' and not ctx
    ]
    assert len(bodies) == 1, (
        f'期望恰好 1 条 `.form-control, .form-select` 规则，实际 {len(bodies)} 条 '
        '—— 本测试已失效（选择器分组或顺序被改过？）'
    )
    body = bodies[0]
    missing = [
        f'{name}（{what}）'
        for name, what in DENSITY_TOKEN_CONSUMERS.items()
        if f'var({name})' not in body
    ]
    assert not missing, (
        '`.form-control, .form-select` 没有引用这些密度令牌：\n'
        + '\n'.join('  ' + m for m in missing)
        + '\n令牌定义了却没人消费 = 控件退回 Bootstrap 的默认密度（实测 43.7px），'
        '而 :root 里的数字看着还是 28px'
    )


_VAR_REF_RE = re.compile(r'var\(\s*(--[-\w]+)')
_VAR_DEF_RE = re.compile(r'(?<![-\w])(--[-\w]+)\s*:')


def test_no_dangling_custom_property_references():
    """style.css 里每个 `var(--x)` 引用的 `--x` 都必须在本文件里有定义。

    这是本任务最容易踩的坑，也是它最普适的一条守卫：**引用一个未定义的自定义
    属性不是错误**，浏览器会让引用它的**整条声明**失效（invalid at computed-value
    time），静默退回 auto / initial。表现是「CSS 里明明写了，页面上没反应」——
    和拼错属性名不同，控制台一个字都不会说。

    Task 10 之前 `--ctl-h` 在全仓 0 次命中，如果先写 `min-height: var(--ctl-h)`
    再忘了在 :root 里定义，控件高度会静默退回 Bootstrap 的 43.7px，而源码看着
    完全正确。

    豁免：`--bs-*` 由 Bootstrap 提供，不在本文件定义。豁免它们是有代价的
    （拼错 --bs 名字这条断言看不见），但把 Bootstrap 的整套变量抄进来做白名单
    会立刻过期，代价更大。
    """
    css = re.sub(r'/\*.*?\*/', '', _css(), flags=re.S)
    defined = set(_VAR_DEF_RE.findall(css))
    dangling = sorted({
        name for name in _VAR_REF_RE.findall(css)
        if name not in defined and not name.startswith('--bs-')
    })
    assert not dangling, (
        'style.css 引用了未定义的自定义属性：\n'
        + '\n'.join('  ' + d for d in dangling)
        + '\n引用未定义的自定义属性会让**整条声明**静默失效（退回 auto/initial），'
        '不会报错。请在 :root 里补上定义，或改掉引用'
    )


# --------------------------------------------------------------------------
# A5 / Task 10：四至显示压成 2 行
# --------------------------------------------------------------------------

BOUNDS_READOUT_MAX_ROWS = 2

# 改前那版的形态特征。留在这里是为了让回潮时的报错说人话。
_BOUNDS_LEGACY_MARKERS = ('<br>', '▲', '▼', '▶', '◀')


def _grid_track_count(value):
    """`auto 1fr auto 1fr` -> 4。看不懂的（repeat()/minmax()/var()）返回 None。"""
    value = value.strip()
    if not value or re.search(r'(repeat|minmax|fit-content|var)\s*\(', value):
        return None
    return len(value.split())


# `.bounds-grid` 的列间距上界，量出来的不是审美选的。见 style.css 里那段注释：
# 最窄的双栏视口 769px 下网格容器 199.1px，极端坐标（`-179.99999` 这种带号
# 10 字符的经度）一行在 gap 6px 时需要 202.8px（网页字体）/ 203.5px（回退字形），
# 会吃掉 alert 右内边距 10px 里的 3.7px。收到 4px 才放得下，余量只有 1.6–2.3px。
BOUNDS_GRID_MAX_COLUMN_GAP_PX = 4


def _bounds_readout_markup(js_body):
    """`updateBoundsInfo` 里「已框选」那个分支写进 innerHTML 的 HTML 模板。

    把 `${...}` 插值换成占位符，好让 HTMLParser 能读。
    """
    branch = re.split(r'\n\s*\}\s*else\s*\{', js_body, maxsplit=1)[0]
    m = re.search(r'innerHTML\s*=\s*`(.*?)`', branch, re.S)
    assert m, (
        'updateBoundsInfo 的「已框选」分支里找不到 innerHTML 模板字面量 —— '
        '本测试已失效（不是通过）'
    )
    return branch, re.sub(r'\$\{[^}]*\}', 'X', m.group(1))


# HTML 的**空元素**（没有结束标签）。深度计数必须跳过它们，否则一个 <input>
# 就会让深度只增不减。
#
# ⚠️ 这里**不能**把 SVG 的 <circle>/<line>/<path>/<polyline> 算进来：本仓库的
# 内联 SVG 全部写了显式结束标签（`<circle ...></circle>`），HTMLParser 会照常
# 发 endtag。把它们当空元素会重复减一次，深度变负 —— 首版就踩了这个坑，
# 表现是 #boundsInfo 里的那个 SVG 之后解析直接停摆，`<button>` 整个丢了，
# 模型少算 50.42px（而且是**少**算，方向上会让测试更容易通过，正是最坏的一种错）。
# 真正的自闭合写法 `<x/>` 由 HTMLParser 的 handle_startendtag 自动配平，不用管。
_VOID_TAGS = frozenset({
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
})


class _TopLevelTagParser(HTMLParser):
    """记下深度 0 的元素（标签 + class 集合）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.top = []

    def handle_starttag(self, tag, attrs):
        if self.depth == 0:
            self.top.append((tag, set((dict(attrs).get('class') or '').split())))
        if tag not in _VOID_TAGS:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag not in _VOID_TAGS:
            self.depth = max(0, self.depth - 1)


def test_bounds_readout_is_exactly_two_rows():
    """框选后的四至显示必须恰好排成 2 行，且 alert 里只有这一个网格。

    这是本任务里最值钱的单项（实测 `#boundsInfo` 146.5px -> 62.0px，省 84.5px）。
    改前是 5 行：图标 +「选中区域：」标题 + ▲北 / ▼南 / ▶东 / ◀西 四行 `<br>`。

    **行数是算出来的，不是查字符串**：
        行数 = ceil(网格子元素数 / grid-template-columns 的轨道数)
    两个输入分居两个文件（子元素由 static/js/map.js 的 updateBoundsInfo 生成，
    轨道数在 style.css 的 .bounds-grid），所以只改一边都会被这条接住 ——
    比如有人把列定义改成 `auto 1fr`（2 列），8 个子元素立刻退回 4 行，
    而 JS 一个字没动。

    ⚠️ 「alert 里只有这一个顶层元素」这条是**评审加的**，堵的是一个真实逃逸：
    在 `.bounds-grid` **上面**再加一行标题（就像改前那样），
    「8 格 / 4 列 = 2 行」这个算式完全看不见，全套测试绿，而屏幕上又变回 3 行
    （评审实测 +22.5px）。所以行数算式必须配上「网格之外没有别的东西」。

    覆盖范围（诚实说明）：算的是**网格轨道意义上的行数**，不是渲染出来的
    视觉行数。若某个值长到在自己的格子里折行，实际视觉高度会比 2 行多 ——
    那种情况这条断言看不见，由 CDP 实测兜底（19 个断点边界视口 + 769px 极端坐标，
    记在 p2-task-10-report.md）。列间距上界就是那轮实测的产物。
    """
    js = _js('map.js')
    body = _js_function_body(js, 'updateBoundsInfo')
    branch, markup = _bounds_readout_markup(body)

    legacy = [m for m in _BOUNDS_LEGACY_MARKERS if m in branch]
    assert not legacy, (
        f'updateBoundsInfo 里还有改前那版 5 行布局的痕迹 {legacy} —— '
        '`<br>` 每出现一次就多一行；`▲▼▶◀` 应换成 GIS 惯例的 N/S/E/W'
        '（那四个三角形在等宽字体里宽度还不一致，数字对不齐）'
    )

    parser = _TopLevelTagParser()
    parser.feed(markup)
    assert len(parser.top) == 1 and parser.top[0] == ('div', {'bounds-grid'}), (
        f'「已框选」分支的顶层元素是 {parser.top}，'
        '期望恰好一个 <div class="bounds-grid">。'
        '在网格之外再放任何东西（标题、图标、说明）都会多占行 —— '
        '而下面那个「8 格 / 4 列 = 2 行」的算式看不见它'
    )

    keys = re.findall(r'class="bounds-k"', branch)
    vals = re.findall(r'class="bounds-v"', branch)
    assert len(keys) == 4 and len(vals) == 4, (
        f'期望 4 个 .bounds-k + 4 个 .bounds-v（北南东西各一对），'
        f'实际 {len(keys)} + {len(vals)} —— 本测试已失效，或者四至少渲染了一条'
    )

    bodies = [
        b for sel, b, ctx in _rules_ctx(_css())
        if sel == '.bounds-grid' and not ctx
    ]
    assert len(bodies) == 1, (
        f'期望恰好 1 条 `.bounds-grid` 规则，实际 {len(bodies)} 条 —— 本测试已失效'
    )
    decls = _decl_map(bodies[0])
    assert decls.get('display') == 'grid', (
        f'.bounds-grid 的 display 是 {decls.get("display")!r}，不是 grid —— '
        '不是网格就没有「列」，8 个 span 会横着流成 1 行或竖着堆成 8 行'
    )
    cols = _grid_track_count(decls.get('grid-template-columns', ''))
    assert cols is not None, (
        f'.bounds-grid 的 grid-template-columns 是 '
        f'{decls.get("grid-template-columns")!r}，本断言数不出轨道数 —— '
        '测试已失效（不是通过）。真要用 repeat()/minmax()，请连同这里的解析一起改'
    )

    children = len(keys) + len(vals)
    rows = -(-children // cols)          # ceil
    assert rows <= BOUNDS_READOUT_MAX_ROWS, (
        f'四至显示排成 {rows} 行（{children} 个格子 / {cols} 列），'
        f'超过 {BOUNDS_READOUT_MAX_ROWS} 行上限。改前的 5 行版实测 146.5px，'
        '是 1366x768 放不下提交按钮的主要原因之一'
    )

    gap = _IMPORTANT_RE.sub('', decls.get('gap', '')).strip().split()
    assert len(gap) == 2, (
        f'.bounds-grid 的 gap = {decls.get("gap")!r}，期望「行间距 列间距」两个值 '
        '—— 本测试已失效'
    )
    col_gap = _resolve_length_px(_css(), gap[1])
    assert col_gap is not None, f'.bounds-grid 的列间距 {gap[1]!r} 解析不了 —— 本测试已失效'
    assert col_gap <= BOUNDS_GRID_MAX_COLUMN_GAP_PX, (
        f'.bounds-grid 的列间距 {col_gap:g}px 超过 {BOUNDS_GRID_MAX_COLUMN_GAP_PX}px。'
        '769px（最窄的双栏视口）+ 极端坐标 -179.99999 下，6px 间距实测超出容器 3.7px，'
        '会挤掉 alert 的右内边距。要放宽请先按 p2-task-10-report.md 的方法重测'
    )


# 方位字母 -> 它必须绑定的 currentBounds 字段。
#
# ⚠️ 这张表守的是**数据正确性，不是排版**。评审实测：把 N 和 S 的值对调，
#    全套 247 条断言一条不红，而界面把南纬标成了北纬。这是个 GIS 工具，
#    方位标错和「进度条颜色不好看」不在一个量级上。
BOUNDS_LABEL_FIELDS = {'N': 'north', 'S': 'south', 'E': 'east', 'W': 'west'}


def test_bounds_labels_bind_to_the_right_coordinate():
    """每个方位字母后面跟的必须是**对应**的那个 bounds 字段。

    检查的是「配对」，不是「四个字母都出现过」——后者对调 N/S 的值照样通过。
    """
    branch, _markup = _bounds_readout_markup(
        _js_function_body(_js('map.js'), 'updateBoundsInfo')
    )
    pairs = re.findall(
        r'class="bounds-k"[^>]*>\s*([NSEW])\s*</span>'      # 键
        r'\s*<span class="bounds-v"[^>]*>'                  # 值的开标签
        r'(?:\s*<span[^>]*>[^<]*</span>)?'                  # 可选的读屏文本
        r'\s*\$\{\s*f\(\s*currentBounds\.(\w+)\s*\)\s*\}',  # 值引用的字段
        branch,
    )
    assert len(pairs) == len(BOUNDS_LABEL_FIELDS), (
        f'只解析出 {len(pairs)} 组「方位字母 -> currentBounds 字段」的配对，'
        f'期望 {len(BOUNDS_LABEL_FIELDS)} 组 —— 本测试已失效（不是通过）。'
        'updateBoundsInfo 的标记写法变了？请连同这里的正则一起改'
    )
    got = dict(pairs)
    assert got == BOUNDS_LABEL_FIELDS, (
        f'方位字母绑错了坐标：实际 {got}，期望 {BOUNDS_LABEL_FIELDS}。'
        '这是数据正确性缺陷，不是排版问题 —— 界面会把南纬标成北纬'
    )


# 只给读屏软件的方位词。N/S/E/W 是 GIS 惯例，视觉上够用，
# 但读屏念出来只有四个字母，方位信息丢失。
BOUNDS_SR_WORDS = {'north': '北纬', 'south': '南纬', 'east': '东经', 'west': '西经'}


def test_bounds_readout_is_announced_to_screen_readers():
    """四至的方位必须有读屏可读的中文文本，且键上要有 aria-hidden。

    没有这一层，读屏用户听到的是「N 39.91653」——拿不到方位。
    `.bounds-sr` 必须是脱离文档流的（position: absolute），否则中文方位词会
    显示出来并撑破 2 行网格；而且它必须由 **style.css 自己**定义 ——
    直接用 Bootstrap 的 `.visually-hidden` 的话，CDN 不可达时那个类不存在，
    方位词会直接漏到界面上（本站已有同样理由的离线兜底，见 .row / .col-*）。
    """
    branch, _markup = _bounds_readout_markup(
        _js_function_body(_js('map.js'), 'updateBoundsInfo')
    )
    problems = []
    for letter, field in sorted(BOUNDS_LABEL_FIELDS.items()):
        word = BOUNDS_SR_WORDS[field]
        if not re.search(
            r'<span class="bounds-sr">\s*' + word + r'\s*</span>\s*'
            r'\$\{\s*f\(\s*currentBounds\.' + field + r'\s*\)\s*\}', branch
        ):
            problems.append(f'{letter}/{field}: 缺少 `<span class="bounds-sr">{word}</span>`')
    hidden = re.findall(r'class="bounds-k"\s+aria-hidden="true"', branch)
    if len(hidden) != len(BOUNDS_LABEL_FIELDS):
        problems.append(
            f'只有 {len(hidden)} 个 .bounds-k 带 aria-hidden="true"，'
            f'期望 {len(BOUNDS_LABEL_FIELDS)} 个 —— 否则读屏会念成「N 北纬 39.9…」'
        )
    assert not problems, '四至的读屏可访问性不完整：\n' + '\n'.join('  ' + p for p in problems)

    bodies = [b for sel, b, ctx in _rules_ctx(_css()) if sel == '.bounds-sr' and not ctx]
    assert len(bodies) == 1, (
        f'期望 style.css 里恰好 1 条 `.bounds-sr` 规则，实际 {len(bodies)} 条。'
        '这个类必须由本站自己定义 —— 依赖 Bootstrap 的 .visually-hidden 的话，'
        'CDN 不可达时方位词会直接显示出来并撑破 2 行网格'
    )
    pos = _decl_map(bodies[0]).get('position')
    assert pos == 'absolute', (
        f'.bounds-sr 的 position 是 {pos!r}，不是 absolute —— '
        '不脱离文档流的话中文方位词会参与布局，2 行网格的几何就不成立了'
    )


# --------------------------------------------------------------------------
# A5 / Task 10（评审补强）：1366x768 垂直预算模型
#
# 为什么上面那些断言不够 —— 评审实测的四个逃逸变异：
#   1. 给 `.form-control, .form-select` 加一行 `height: 44px`  -> 全套 247 绿，
#      而实际 ctlH 回到 44、submitBtnBottom 811.53、**溢出 43.5px**。
#      根因：`min-height: var(--ctl-h)` 是**下界**，没有任何东西钉上界；
#      「令牌已被消费」那条只是 grep `var()` 字符串，`height` 和 `min-height`
#      同时存在时前者赢，令牌还在源码里、测试照样绿。
#   2. `--pad-card` 10 -> 40                                  -> 全套绿，745.5；
#      改到 80 就是 785.5，装不下。
#   3. 在 `.bounds-grid` 上面加一行标题                        -> 全套绿，又变 3 行。
#   4. 把 N 和 S 的值对调                                      -> 全套绿，
#      **界面把南边标成北边**。这是数据正确性，不是排版。
#
# 本节用「模拟层叠 + 高度模型」一次性接住 1 和 2；3 和 4 由下一节的两条
# 专用断言接住（它们是结构 / 语义问题，高度模型看不见）。
#
# 模型思路与 A4 / Task 9 的渲染链断言同源：把源码里的量算成一个可对拍的数字，
# 再拿 CDP 实测值校准。校准锚点见 CDP_SUBMIT_BTN_BOTTOM。
# --------------------------------------------------------------------------

VIEWPORT_1366_HEIGHT_PX = 768

# CDP 实测锚点：1366x768、**已框选**（按钮 disabled 解除、四至已渲染）状态下
# `#createTaskBtn.getBoundingClientRect().bottom`。
# 复现方法见 .superpowers/sdd/p2-task-10-report.md。改前是 949.34。
CDP_SUBMIT_BTN_BOTTOM = 715.53

# 模型允许的对拍误差。0.5px 足够吸收浏览器的亚像素舍入。
# 实测（Task 11 复核）：模型 715.61 vs CDP 715.53，差 **0.08px**。
# ⚠️ 订正一处旧注释：这里原本写「模型算出 715.52，差 0.01px」，是不准的 ——
#    把 Task 10 的 CSS（HEAD~1）、Task 11 的 CSS 分别喂给**同一个旧模型**，
#    算出来都是 715.61，说明 0.08 的偏差从 Task 10 就存在，不是谁改出来的。
#    交接说明里「模型对拍 CDP 三点、误差恒定 0.08px」才是对的那个数。
HEIGHT_MODEL_TOLERANCE_PX = 0.5

# ---- 来自 Bootstrap、本文件不控制的几个数 --------------------------------
# 每一条都标了 CDP 实测值。它们是模型里唯一「不是从 style.css 解析出来」的输入，
# 所以单独列出来：Bootstrap 大版本升级时这几个数要重测。
# `test_bootstrap_build_is_new_enough_to_have_dark_theme` 已经钉住 >= 5.3。

# Bootstrap 的 --bs-body-line-height。用于所有没有显式声明 line-height 的文字
# （.form-label / .form-group-label / .bounds-grid / .btn）。实测：14px 字 -> 21px 行高。
BS_BODY_LINE_HEIGHT = 1.5
# `.card-header h5` 的行盒高度。**不是** font-size x line-height：h5 里有一个
# 18px 的内联 SVG（vertical-align: middle），行盒被它撑到 18.91px。
# 这个数没法从 CSS 算出来，只能实测。CDP: hdrInnerH = 18.91。
BS_CARD_HEADER_LINE_BOX_PX = 18.91
# Bootstrap `.btn { padding: .375rem .75rem }` 的纵向内边距。
# ⚠️ 这是**兜底值**，只在 style.css 自己没声明 `.btn` 的纵向内边距时才用。
#    第一版把它当成写死的常量（`btn_h = 2*6.0 + font*1.5`），于是按钮的
#    padding / min-height / height / border 全在模型的视野之外。评审实测三例：
#      `.btn{padding:2.5rem 2rem}` -> 261 passed，按钮 34.5->102.5，bottom 783.53 出视口
#      `.btn{min-height:90px}`     -> 261 passed，bottom 771.03 > 768
#      `.btn{padding:1rem 2rem}`   -> 261 passed，真实 bottom 735.53 而模型仍算 715.53
#    第三例最能说明问题：20px 的误差通过了 0.5px 的容差 —— 因为那条断言比的是
#    「模型 vs 一个和模型同样过期的常量」，两边一起错，差值当然是 0。
#    现在由 `_effective_button_height` 从 style.css 解析，这个常量退居兜底。
BS_BTN_PADDING_Y_PX = 6.0
# Bootstrap `.alert { margin-bottom: 1rem }`。本站没有覆盖它。
BS_ALERT_MARGIN_BOTTOM_PX = 16.0


def _effective_button_height(css):
    """模拟层叠，算出 `#createTaskBtn`（`.btn.btn-primary.w-100`）的**最终**外框高度。

    与 `_effective_form_control_height` 是同一套模型、同一个理由：决定高度的是
    「层叠之后谁赢」，不是源码里有没有那串字符。区别只在于按钮的几个盒模型属性
    **Bootstrap 有默认值而 style.css 可以不声明**，所以取不到时回落到 BS_* 常量。

    计算：
        内容高 = line-height（没声明就用 font-size x BS_BODY_LINE_HEIGHT）
        自然高 = padding-top + padding-bottom + 内容高 + 2 x 边框
        最终高 = height 若有声明；否则 clamp(自然高, min-height, max-height)
    （box-sizing: border-box 由顶层 `*` 规则保证，MERGED_UNIVERSAL_DECLS 钉着。）

    复现评审那三个实测（这是本函数的自检，数字不是凑的）：
        现状                       -> 6+6+22.5+0      = 34.5   （CDP 实测 34.5）
        padding: 2.5rem 2rem       -> 40+40+22.5+0    = 102.5  （CDP 实测 102.5）
        min-height: 90px           -> max(34.5, 90)   = 90
        padding: 1rem 2rem         -> 16+16+22.5+0    = 54.5   （比现状高 20px）
    """
    ctx = _BtnCtx({'btn', 'btn-primary', 'w-100'}, element_id='createTaskBtn',
                  label='#createTaskBtn（高度模型）')
    got = _btn_computed(css, ctx, 'base', {
        'padding', 'padding-top', 'padding-bottom', 'height', 'min-height',
        'max-height', 'line-height', 'font-size', 'border', 'border-width',
        'border-top-width', 'border-bottom-width', 'border-style',
    })

    def px(name, default=None, required=False):
        if name not in got:
            assert not required, f'`.btn` 没有任何规则声明 {name} —— 高度模型算不出来，测试已失效'
            return default
        raw = got[name][0]
        # padding 简写取纵向那一位
        if name == 'padding':
            raw = raw.split()[0]
        v = _resolve_length_px(css, raw)
        assert v is not None, (
            f'{name} 的胜出值来自 `{got[name][1]}` 的 {raw!r}，不是 px/rem/var(px) —— '
            '高度模型解析不了，测试已失效（不是通过）'
        )
        return v

    font_size = px('font-size', required=True)
    line_h = px('line-height')
    if line_h is None:
        line_h = font_size * BS_BODY_LINE_HEIGHT

    # 纵向内边距：长写优先，其次简写，都没有才回落到 Bootstrap 默认
    pad_t = px('padding-top', default=px('padding', default=BS_BTN_PADDING_Y_PX))
    pad_b = px('padding-bottom', default=px('padding', default=BS_BTN_PADDING_Y_PX))

    # 边框：`.btn { border: none }` -> 0；有人写回 1px 也要算进去
    style_raw = got.get('border-style', ('none',))[0].strip().lower()
    if style_raw in ('none', 'hidden'):
        border = 0.0
    else:
        border = (px('border-top-width') or px('border-width')
                  or px('border', default=0.0) or 0.0)

    natural = pad_t + pad_b + line_h + 2 * border
    fixed = px('height')
    if fixed is not None:
        return fixed
    out = natural
    lo = px('min-height')
    if lo is not None:
        out = max(out, lo)
    hi = px('max-height')
    if hi is not None:
        out = min(out, hi)
    return out


def _effective_form_control_height(css):
    """模拟层叠，算出一个 `<input class="form-control">` 的**最终**外框高度。

    这是接住「加一行 `height: 44px`」的那把锁。只查 `--ctl-h` 或者 grep
    `var(--ctl-h)` 都拦不住它 —— 决定高度的是**层叠之后谁赢**，不是源码里
    有没有那串字符。与 `test_form_select_reserves_room_for_the_arrow`
    （最终右内边距）、`test_color_picker_swatch_is_big_enough_to_see`
    （最终宽度）是同一套模型。

    计算：
        内容高 = line-height（没声明就用 font-size x BS_BODY_LINE_HEIGHT）
        自然高 = padding-top + padding-bottom + 内容高 + 2 x 边框
        最终高 = height 若有声明；否则 clamp(自然高, min-height, max-height)
    （box-sizing: border-box 由本文件顶层的 `*` 规则保证，
      MERGED_UNIVERSAL_DECLS 钉着，所以 height 就是外框高。）

    覆盖范围（诚实说明）：只模拟 style.css 内部的层叠、只认类选择器写法。
    有人用 `input[type=text] { height: 44px }` 这种属性选择器绕过，本断言看不见
    —— 由 CDP 实测兜底。
    """
    element_classes = {'form-control'}
    wanted = {'padding-top', 'padding-bottom', 'height', 'min-height',
              'max-height', 'line-height', 'font-size'}
    best, unsupported = {}, []
    for order, (sel, body, at_ctx) in enumerate(_rules_ctx(css)):
        if at_ctx:
            continue
        decls = [d for d in _expanded_box_decls(body) if d[0] in wanted]
        if not decls:
            continue
        for branch in _selector_parts(sel):
            if not re.search(r'\.form-control(?![-\w])', branch):
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
        '控件高度的层叠模型处理不了这些写法，测试已失效（不是通过）：\n'
        + '\n'.join('  ' + u for u in unsupported)
    )

    def px(name, required=False):
        if name not in best:
            assert not required, (
                f'`.form-control` 没有任何规则声明 {name} —— 高度模型算不出来，'
                '测试已失效（不是通过）'
            )
            return None
        _key, val, sel = best[name]
        v = _resolve_length_px(css, val)
        assert v is not None, (
            f'{name} 的胜出值来自 `{sel}` 的 {val!r}，不是 px/rem/var(px) —— '
            '高度模型解析不了，测试已失效（不是通过）'
        )
        return v

    font_size = px('font-size', required=True)
    line_h = px('line-height')
    if line_h is None:
        line_h = font_size * BS_BODY_LINE_HEIGHT
    pad_t = px('padding-top', required=True)
    pad_b = px('padding-bottom', required=True)
    border = _form_control_border_px(css)
    natural = pad_t + pad_b + line_h + 2 * border

    fixed = px('height')
    if fixed is not None:
        return fixed
    out = natural
    lo = px('min-height')
    if lo is not None:
        out = max(out, lo)
    hi = px('max-height')
    if hi is not None:
        out = min(out, hi)
    return out


def test_form_control_height_has_an_upper_bound():
    """**层叠之后**控件的最终高度必须 <= CONTROL_HEIGHT_MAX_PX。

    与 `test_control_density_tokens_are_self_consistent` 的区别是承重的：
    那条只看 `:root` 里四个数字自不自洽，**管不到有没有别的声明把它们架空**。
    评审实测：往 `.form-control, .form-select` 里加一行 `height: 44px`，
    那条断言全绿（令牌没动），而浏览器里控件是 44px、1366x768 溢出 43.5px。
    本条算的是最终生效高度，加 `height` 会被直接抓住。
    """
    css = _css()
    h = _effective_form_control_height(css)
    assert h <= CONTROL_HEIGHT_MAX_PX, (
        f'层叠之后 `.form-control` 的最终高度是 {h:g}px，超过 '
        f'{CONTROL_HEIGHT_MAX_PX}px 上界。改前 43.7px 正是本任务要修的缺陷。'
        '注意：`height` 会压过 `min-height: var(--ctl-h)`，'
        '所以「令牌没变」不代表高度没变'
    )
    ctl_h = _token_px(css, '--ctl-h')
    assert abs(h - ctl_h) < 0.01, (
        f'最终高度 {h:g}px 与令牌 --ctl-h({ctl_h:g}px) 不一致 —— '
        '说明有别的声明把令牌架空了（多半是一条 height/max-height）。'
        '令牌必须是控件高度的**唯一**来源，否则改令牌不管用'
    )


# ---- 首页表单的纵向结构（从模板解析，不写死条数）--------------------------

class _FormStructureParser(HTMLParser):
    """扒出 `#downloadForm` 的直接子元素序列。

    为什么从模板解析而不是把「6 个字段组 + 3 个分组标题」写死：写死的话，
    有人往 index.html 里加一个字段，模型算出来的高度不变，测试全绿而页面
    已经溢出了。

    默认（地图瓦片）模式下不可见的块（`style="display:none"`，即
    #demOptions / #localTerrainOptions / #contourOptions）跳过 —— 它们由
    initDownloadTypeToggle() 按下载类型切换，不占默认视图的高度。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = None          # None = 还没进 / 已经出了那个 <form>
        self.rows = []             # [(标签, class 集合, 是否 display:none)]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get('id') == 'downloadForm':
            assert self.depth is None, 'index.html 里出现了两个 #downloadForm —— 本测试已失效'
            self.depth = 0
            return
        if self.depth is None:
            return
        if self.depth == 0:
            self.rows.append((
                tag,
                set((a.get('class') or '').split()),
                'display:none' in (a.get('style') or '').replace(' ', ''),
            ))
        if tag not in _VOID_TAGS:
            self.depth += 1

    def handle_endtag(self, tag):
        if self.depth is None or tag in _VOID_TAGS:
            return
        if self.depth == 0:
            self.depth = None      # 这是 </form>，出去了
            return
        self.depth -= 1


def _index_form_rows():
    """`#downloadForm` 在默认（地图瓦片）模式下可见的直接子元素。"""
    parser = _FormStructureParser()
    parser.feed(_template('index.html'))
    assert parser.depth is None, (
        'index.html 的 #downloadForm 没有正常闭合（解析结束时深度 '
        f'{parser.depth}）—— 本测试已失效'
    )
    rows = [(tag, cls) for tag, cls, hidden in parser.rows if not hidden]
    assert rows, '解析不出 #downloadForm 的子元素 —— 本测试已失效'
    # 模型只在「有提交按钮 + 有四至提示条」的前提下成立
    assert any(t == 'button' for t, _c in rows), (
        '#downloadForm 的直接子元素里没有 <button> —— 解析漏了，本测试已失效'
    )
    assert any('alert' in c for _t, c in rows), (
        '#downloadForm 的直接子元素里没有 .alert（四至提示条）—— 本测试已失效'
    )
    return rows


def _index_form_vertical_model(css):
    """算出 1366x768 下 `#createTaskBtn` 的 bottom（px）。

    结构从 templates/index.html 解析，尺寸从 style.css 解析，
    只有上面那 4 个 BS_* 常量来自 Bootstrap（各自标了 CDP 实测值）。

    相邻块级元素的**外边距合并**是模型里最容易漏的一环：
    `.mb-3`(8px) 后面跟 `.form-group-label`(margin-top 14px) 时，两者合并成
    max(8,14)=14 而不是 22。漏掉合并会让模型比实际高出 16px（实测差值）。
    """
    def rule_px(selector, prop, required=True):
        bodies = [b for sel, b, ctx in _rules_ctx(css) if sel == selector and not ctx]
        assert len(bodies) == 1, (
            f'期望恰好 1 条 `{selector}` 规则，实际 {len(bodies)} 条 —— 本测试已失效'
        )
        decls = dict((n, v) for n, v, _i in _expanded_box_decls(bodies[0]))
        if prop not in decls:
            decls = _decl_map(bodies[0])
        raw = decls.get(prop)
        if raw is None:
            assert not required, f'`{selector}` 没有声明 {prop} —— 本测试已失效'
            return None
        v = _resolve_length_px(css, raw)
        assert v is not None, (
            f'`{selector}` 的 {prop} = {raw!r}，解析不了 —— 本测试已失效'
        )
        return v

    def margin_parts(selector, prop='margin'):
        bodies = [b for sel, b, ctx in _rules_ctx(css) if sel == selector and not ctx]
        assert len(bodies) == 1, (
            f'期望恰好 1 条 `{selector}` 规则，实际 {len(bodies)} 条 —— 本测试已失效'
        )
        raw = _decl_map(bodies[0]).get(prop)
        assert raw, f'`{selector}` 没有声明 {prop} —— 本测试已失效'
        parts = _IMPORTANT_RE.sub('', raw).strip().split()
        vals = [_resolve_length_px(css, p) for p in parts]
        assert all(v is not None for v in vals), (
            f'`{selector}` 的 {prop} = {raw!r}，解析不了 —— 本测试已失效'
        )
        if len(vals) == 1:
            return vals[0], vals[0]
        if len(vals) == 2:
            return vals[0], vals[0]
        return vals[0], vals[2]              # 3/4 值：上、下

    def border_px(selector, prop):
        bodies = [b for sel, b, ctx in _rules_ctx(css) if sel == selector and not ctx]
        assert len(bodies) == 1, f'`{selector}` 不是恰好 1 条 —— 本测试已失效'
        raw = _decl_map(bodies[0]).get(prop)
        assert raw, f'`{selector}` 没有声明 {prop} —— 本测试已失效'
        m = re.match(r'^([\d.]+)(px|rem)\b', _IMPORTANT_RE.sub('', raw).strip())
        assert m, f'`{selector}` 的 {prop} = {raw!r} 里读不出宽度 —— 本测试已失效'
        return float(m.group(1)) * (16 if m.group(2) == 'rem' else 1)

    navbar = rule_px('.main-content', 'padding-top')
    panel_pad = rule_px('.index-right', 'padding-top')
    card_border = border_px('.card', 'border')
    hdr_pad = rule_px('.card-header', 'padding-top')
    hdr_border = border_px('.card-header', 'border-bottom')
    body_pad = rule_px('.card-body', 'padding-top')

    ctl_h = _effective_form_control_height(css)
    field_gap = rule_px('.mb-3', 'margin-bottom')

    label_font = rule_px('.form-label', 'font-size')
    label_line = label_font * BS_BODY_LINE_HEIGHT
    label_mb = rule_px('.form-label', 'margin-bottom')

    gl_font = rule_px('.form-group-label', 'font-size')
    gl_line = gl_font * BS_BODY_LINE_HEIGHT
    gl_pad_b = rule_px('.form-group-label', 'padding-bottom')
    gl_border = border_px('.form-group-label', 'border-bottom')
    gl_mt, gl_mb = margin_parts('.form-group-label')
    gl_h = gl_line + gl_pad_b + gl_border

    alert_pad = rule_px('.alert', 'padding-top')
    alert_border = border_px('.alert', 'border')
    grid_font = rule_px('.bounds-grid', 'font-size')
    grid_line = grid_font * BS_BODY_LINE_HEIGHT
    grid_row_gap = margin_parts('.bounds-grid', 'gap')[0]
    grid_h = 2 * grid_line + grid_row_gap          # 恰好 2 行，见下一节的断言
    alert_h = 2 * alert_pad + 2 * alert_border + grid_h

    btn_h = _effective_button_height(css)

    field_h = label_line + label_mb + ctl_h        # 一个 .mb-3 字段组的高度

    # 逐个子元素累加，并按 CSS 规则合并相邻外边距
    items = []                                     # [(高度, 下外边距, 上外边距)]
    for tag, classes in _index_form_rows():
        if 'form-group-label' in classes:
            items.append((gl_h, gl_mb, gl_mt))
        elif 'row' in classes:
            # display:flex 的容器，子元素外边距不合并出去：
            # 高度 = 列内容 + 列自己的 .mb-3
            items.append((field_h + field_gap, 0.0, 0.0))
        elif 'alert' in classes:
            items.append((alert_h, BS_ALERT_MARGIN_BOTTOM_PX, 0.0))
        elif tag == 'button':
            items.append((btn_h, 0.0, 0.0))
        elif 'mb-3' in classes:
            items.append((field_h, field_gap, 0.0))
        else:
            raise AssertionError(
                f'#downloadForm 里出现了模型不认识的直接子元素 <{tag} class="'
                f'{" ".join(sorted(classes))}"> —— 本测试已失效（不是通过）。'
                '请把它的高度加进 _index_form_vertical_model'
            )

    total = sum(h for h, _mb, _mt in items)
    for prev, nxt in zip(items, items[1:]):
        total += max(prev[1], nxt[2])              # 相邻外边距合并取较大者

    return (navbar + panel_pad + card_border + 2 * hdr_pad
            + BS_CARD_HEADER_LINE_BOX_PX + hdr_border + body_pad + total)


def test_height_model_reproduces_the_cdp_measurement():
    """高度模型必须复现 CDP 实测的 715.53。

    这条是**模型的自检**，不是产品契约。它在这里的作用：
    `test_submit_button_fits_at_1366x768` 用模型算出的数字下结论，
    模型本身要是错的，那条断言就是在给一个错数字背书 ——
    「静默给出错误的信心」比没有断言更糟。

    ⚠️ 这条变红时怎么办（大概率你是故意改了尺寸）：
      1. 先看 `test_submit_button_fits_at_1366x768` 是不是也红了。红了 = 真放不下。
      2. 按 .superpowers/sdd/p2-task-10-report.md 的方法在 1366x768 **已框选**
         状态下重测 `#createTaskBtn` 的 bottom，把 CDP_SUBMIT_BTN_BOTTOM 换成新值。
      3. **别反过来改模型去凑锚点。** 模型算错和尺寸改动是两回事。
    锚点被人随手改大也不要紧：装不装得下由下面那条独立判断，改锚点绕不过它。
    """
    got = _index_form_vertical_model(_css())
    assert abs(got - CDP_SUBMIT_BTN_BOTTOM) <= HEIGHT_MODEL_TOLERANCE_PX, (
        f'高度模型算出 #createTaskBtn 的 bottom = {got:.2f}px，'
        f'与 CDP 实测锚点 {CDP_SUBMIT_BTN_BOTTOM}px 差 '
        f'{abs(got - CDP_SUBMIT_BTN_BOTTOM):.2f}px（容差 {HEIGHT_MODEL_TOLERANCE_PX}px）。'
        '要么你改了影响高度的尺寸（那就重测并更新锚点），'
        '要么模型该更新了（例如 Bootstrap 升级改了 BS_* 那几个常量）'
    )


def test_submit_button_fits_at_1366x768():
    """1366x768 下「创建下载任务」按钮必须整个在折叠线以上。

    **这是 A5 / Task 10 的验收标准本身。**

    改前实测 949.34（已框选 = 按钮可点的那个状态），溢出 181.3px：用户在
    1366x768 笔记本上必须滚动才能提交。改后 715.53，余 52.47px。

    模型的输入：
      - 结构：从 templates/index.html 解析 `#downloadForm` 的可见直接子元素
        （加一个字段会被算进来）
      - 尺寸：从 style.css 解析，控件高度走**模拟层叠**（加 `height: 44px`
        会被算进来）
      - 只有 4 个 BS_* 常量来自 Bootstrap，各自标了 CDP 实测值
    模型准不准由 test_height_model_reproduces_the_cdp_measurement 自检。

    评审实测的两个逃逸变异现在都会被这条抓住：
      `height: 44px`     -> 模型 811.5 > 768
      `--pad-card` 10->80 -> 模型 785.5 > 768
    """
    got = _index_form_vertical_model(_css())
    assert got <= VIEWPORT_1366_HEIGHT_PX, (
        f'1366x768 下 #createTaskBtn 的 bottom 模型值 {got:.2f}px > '
        f'{VIEWPORT_1366_HEIGHT_PX}px —— 提交按钮在折叠线以下 '
        f'{got - VIEWPORT_1366_HEIGHT_PX:.2f}px，用户必须滚动才能提交。'
        '这正是 A5 / Task 10 要修的缺陷（改前 949.34）'
    )


# --------------------------------------------------------------------------
# A5 / Task 10（评审第二轮）：bbox 的方位不许接反
#
# 这一节守的**不是排版，是数据正确性**，而且是整条链，不是其中一环。
#
# 上一节的 test_bounds_labels_bind_to_the_right_coordinate 只守住了渲染那一环：
#     标签 N  <->  currentBounds.north          ✅
# 评审做的变异在**上一环**：把 map.js 里的 getNorth() 和 getSouth() 全局互换
# （命中 4 处，两个构造点各 2 处）。互换之后 `currentBounds.north` 字段里装的是
# 南纬值，而标签配对纹丝不动 —— 47 条断言一条不红。
#     currentBounds.north  <->  getNorth()      ❌ 当时没人守
#
# 为什么这一环比渲染层更重要：`currentBounds` **同时是提交给后端的 bbox**
# （submitContour / taskData 三处 payload 都从它取值）。构造点接反的后果不只是
# 界面把南标成北，**下载的区域也跟着错**。这条数据链 Phase 1 刚修过一次
# （「拖拽改框后提交的还是旧 bbox」），是敏感区。
#
# 做法：不只盯那两个构造点，而是扫 map.js 里**每一个** bbox 形状的对象字面量
# （四个方位键齐全的），逐键检查「值表达式引用的方位」是否与键名一致。
# 当前命中 6 处：2 个构造点（getter 形态）、3 处提交 payload（字段形态）、
# coverageBounds() 的返回值（`Math.floor(b.north - eps) + 1` 这种包了一层的形态）。
# 新加一处 bbox 字面量会被自动扫进来，不需要改测试。
# --------------------------------------------------------------------------

_DIRECTIONS = ('north', 'south', 'east', 'west')


def _strip_js_comments(src):
    """剥掉 JS 注释。

    行注释的正则带 `(?<!:)`，避免把 `https://...` 的后半截当注释吃掉
    （map.js:64 的 OSM 瓦片 URL 就是这个形态）。
    """
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'(?<![:\\])//[^\n]*', '', src)


def _matching_brace(src, open_idx):
    """`src[open_idx]` 是 `{`，返回配对的 `}` 下标；配不上返回 None。"""
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return i
    return None


def _enclosing_object_literal(src, pos):
    """从 `pos` 往回找最近的未闭合 `{`，返回 (起始下标, 花括号内的文本)。"""
    depth = 0
    for i in range(pos - 1, -1, -1):
        if src[i] == '}':
            depth += 1
        elif src[i] == '{':
            if depth == 0:
                end = _matching_brace(src, i)
                return (i, src[i + 1:end]) if end is not None else (None, None)
            depth -= 1
    return (None, None)


def _split_top_level(body):
    """按**顶层**逗号拆对象字面量的属性（括号/方括号/花括号里的逗号不算）。"""
    parts, depth, cur = [], 0, ''
    for ch in body:
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(cur)
            cur = ''
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _directions_referenced(expr):
    """值表达式里引用了哪些方位。

    认两种形态：
      字段名 —— `currentBounds.north` / `b.north`（`.` 不在词边界字符集里，能匹配上）
      getter —— `bounds.getNorth()`（`north` 被 `getNorth` 粘住，单独匹配 getter 形态）
    """
    found = set()
    for d in _DIRECTIONS:
        if re.search(r'(?<![-\w$])' + d + r'(?![-\w$])', expr, re.I):
            found.add(d)
        elif re.search(r'get' + d.capitalize() + r'\s*\(', expr):
            found.add(d)
    return found


def _bbox_object_literals(src):
    """四个方位键齐全的对象字面量 -> [(起始下标, {键: 值表达式}), ...]。"""
    out, seen = [], set()
    for m in re.finditer(r'(?<![-\w.$])north\s*:', src):
        start, body = _enclosing_object_literal(src, m.start())
        if start is None or start in seen:
            continue
        pairs = {}
        for part in _split_top_level(body):
            key, sep, value = part.partition(':')
            if sep:
                pairs[key.strip()] = value.strip()
        if all(d in pairs for d in _DIRECTIONS):
            seen.add(start)
            out.append((start, pairs))
    return out


# 当前 map.js 里 bbox 字面量的处数。用于防「正则一个都没匹配上 -> for 循环不执行
# -> 断言永真」这个本文件反复踩过的坑（见 _form_select_rules 的注释）。
# 加新的 bbox 字面量时把这个数字调大即可 —— 新字面量会被自动检查，不需要改逻辑。
MAP_JS_BBOX_LITERAL_COUNT = 6


def test_bbox_literals_never_swap_directions():
    """map.js 里每个 bbox 字面量的每个方位键，值必须引用**同名**方位。

    覆盖整条数据链上的全部 6 处（不是只有那两个构造点）：
      - `currentBounds = { north: bounds.getNorth(), ... }`  绘制时构造
      - `currentBounds = { north: found.getNorth(), ... }`   编辑/回填时重建
      - `taskData = { north: currentBounds.north, ... }`     x2，提交给后端的 bbox
      - `body = { north: currentBounds.north, ... }`         等高线任务的 bbox
      - `coverageBounds()` 的返回值 `{ north: Math.floor(b.north - eps) + 1, ... }`

    规则是「值表达式引用的方位 == 键名」，不是「值必须长成某个样子」，
    所以 `Math.floor(b.north - eps) + 1` 这种包了一层的写法照样能查。

    ⚠️ 为什么必须扫全部而不是只扫构造点：`currentBounds` 同时是**提交给后端的
    bbox**。构造点接反 -> 界面和下载区域一起错；payload 接反 -> 界面是对的、
    下载的区域是错的（更难发现）。两种都是数据正确性缺陷。
    """
    src = _strip_js_comments(_js('map.js'))
    literals = _bbox_object_literals(src)
    assert len(literals) == MAP_JS_BBOX_LITERAL_COUNT, (
        f'在 map.js 里扫到 {len(literals)} 个 bbox 对象字面量，'
        f'期望 {MAP_JS_BBOX_LITERAL_COUNT} 个。'
        '少了 = 扫描失效（断言会变成永真，比报错更糟）；'
        '多了 = 新增了 bbox 字面量，确认它被检查到之后把 '
        'MAP_JS_BBOX_LITERAL_COUNT 调大'
    )

    problems = []
    for start, pairs in literals:
        line = src.count('\n', 0, start) + 1
        for d in _DIRECTIONS:
            expr = pairs[d]
            refs = _directions_referenced(expr)
            if refs != {d}:
                wrong = sorted(refs - {d}) or ['（一个方位都没引用）']
                problems.append(
                    f'剥注释后第 {line} 行附近的 bbox 字面量：'
                    f'`{d}: {expr}` 引用的是 {wrong}，不是 {d}'
                )
    assert not problems, (
        'bbox 的方位接反了 —— 这是数据正确性缺陷，不是排版问题：\n'
        + '\n'.join('  ' + p for p in problems)
        + '\ncurrentBounds 既喂给界面也喂给后端，接反会让**下载的区域**也错'
    )


# `currentBounds` 的构造点数量。当前两处：
#   1. L.Draw.Event.CREATED 回调里，用户刚画完框
#   2. syncBoundsFromDrawnItems() 里，拖角/拖动/编辑结束后重读图层
# ⚠️ 只守其中一处等于没守：另一处接反照样全绿（评审就是这么发现漏洞的）。
CURRENT_BOUNDS_CONSTRUCTION_SITES = 2

_LEAFLET_BOUNDS_GETTERS = {d: 'get' + d.capitalize() for d in _DIRECTIONS}


def test_current_bounds_is_built_from_matching_leaflet_getters():
    """每一处 `currentBounds = { ... }` 都必须 `north:` 配 `getNorth()`，以此类推。

    这是 `test_bbox_literals_never_swap_directions` 的**加强档**：那条只要求
    「值引用了同名方位」，本条进一步要求构造点必须直接用 Leaflet 的对应 getter
    —— 构造点是整条链的源头，源头错了下游全错，不该允许中间再包一层加工。

    定位方式（不依赖行号，也不只取第一处）：在剥掉注释的源码里 finditer
    所有 `currentBounds = {`，逐个花括号配对取出字面量。
    `currentBounds = null` 那三处不是构造点，正则天然不匹配。
    """
    src = _strip_js_comments(_js('map.js'))
    sites = []
    for m in re.finditer(r'currentBounds\s*=\s*\{', src):
        open_idx = src.index('{', m.start())
        end = _matching_brace(src, open_idx)
        assert end is not None, (
            f'第 {src.count(chr(10), 0, open_idx) + 1} 行附近的 '
            '`currentBounds = {` 花括号配不上对 —— 本测试已失效（不是通过）'
        )
        pairs = {}
        for part in _split_top_level(src[open_idx + 1:end]):
            key, sep, value = part.partition(':')
            if sep:
                pairs[key.strip()] = value.strip()
        sites.append((src.count('\n', 0, open_idx) + 1, pairs))

    assert len(sites) == CURRENT_BOUNDS_CONSTRUCTION_SITES, (
        f'找到 {len(sites)} 处 `currentBounds = {{...}}` 构造点，'
        f'期望 {CURRENT_BOUNDS_CONSTRUCTION_SITES} 处'
        '（绘制完成时一处、拖角/编辑后重读图层时一处）。'
        '少了 = 扫描失效或构造点被改写；多了 = 新增了构造点，'
        '确认它也被检查到之后把 CURRENT_BOUNDS_CONSTRUCTION_SITES 调大 —— '
        '**只守一处等于没守**'
    )

    problems = []
    for line, pairs in sites:
        missing = [d for d in _DIRECTIONS if d not in pairs]
        if missing:
            problems.append(f'第 {line} 行附近的构造点缺少 {missing} 四至不全')
            continue
        for d in _DIRECTIONS:
            getter = _LEAFLET_BOUNDS_GETTERS[d]
            if not re.fullmatch(r'[\w$.]+\.' + getter + r'\s*\(\s*\)', pairs[d]):
                problems.append(
                    f'第 {line} 行附近：`{d}: {pairs[d]}` —— '
                    f'期望形如 `<bounds>.{getter}()`'
                )
    assert not problems, (
        'currentBounds 的构造点把方位接错了：\n'
        + '\n'.join('  ' + p for p in problems)
        + '\ncurrentBounds 既喂给界面（updateBoundsInfo）也喂给后端（提交的 bbox），'
        '源头接反会让界面把南标成北，**下载的区域也跟着错**'
    )




# ==========================================================================
# A6 / Task 11：按钮状态系统
#
# 这一节守的是「按钮在五个状态下**层叠之后**长什么样」，不是「源码里有没有
# 写那几条规则」。分界线很实：改前 `.btn-success:hover` 与 `.btn-success`
# 逐字相同，规则存在、grep 得到、肉眼零变化 —— 形态断言会给这种空操作发绿灯。
#
# ⚠️ 评审第一轮实测到的三个逃逸，全部出在**模型的盲区**而不是断言的阈值上。
#    改完之后这三条都变红了，但更值得记住的是它们的共性：
#      1. `@media` 里的规则被无声 `continue` 掉 —— 往 @media 里追加一条
#         `.btn:disabled{background:#0d6efd;opacity:.5}` 就能把整套修复撤回，
#         而 261 条测试全绿。
#      2. 预筛 `if 'btn' not in branch` 排在安全网**之前**，而
#         `'btn' in '#createTaskBtn'` 是 False（大写 B）——
#         `#createTaskBtn:disabled{...!important}` 同样全绿。
#      3. 特异度常量比真值低 1（见 BOOTSTRAP_BTN_* 那组常量）。
#    结论写在这里给下一个人：**用自己的模型去设计变异，测不到模型自身的盲区。**
#    补护栏时先问「如果我这一环算错了，什么变异能暴露它」。
#
# 模型的边界（诚实说明，不是免责声明）：
#   只模拟 **style.css 内部**的层叠。Bootstrap 的规则不参与计算，改用
#   「胜出声明的特异度必须 >= Bootstrap 对应规则的特异度」这条独立断言来兜
#   （见下面那组常量）。理由是把 CDN 上的 bootstrap.min.css 拉进单测会引入
#   网络依赖，而特异度这一层是可以离线判定的：本站的 style.css 排在最后
#   （test_no_stylesheet_can_load_after_style_css 钉住），同特异度即取胜。
# ==========================================================================

# Bootstrap 5.3.0 按钮状态规则的特异度。**每一条都是用脚本对 CDN 上的
# bootstrap.min.css 逐个选择器算出来的，不是目测的** —— 第一版目测把 active
# 那条记成了 (0,2,0)，比真值低 1，后果是把 16 条状态规则的 `:not(:disabled)`
# 全删掉（特异度从 (0,3,0) 掉到 (0,2,0)）测试仍然全绿，而浏览器里按下态
# 变回 Bootstrap 深蓝 rgb(10,88,202)。
#
#   .btn:hover                            {background-color:var(--bs-btn-hover-bg)}       (0,2,0)
#   .btn:focus-visible                    {background-color:var(--bs-btn-hover-bg);
#                                          outline:0; box-shadow:...}                     (0,2,0)
#   .btn:disabled                         {background-color:var(--bs-btn-disabled-bg);
#                                          opacity:.65; pointer-events:none}              (0,2,0)
#   fieldset:disabled .btn                {（同上）}                                       (0,2,1)  ← 见盲区说明
#   .btn:first-child:active               {background-color:var(--bs-btn-active-bg)}      (0,3,0)
#   :not(.btn-check)+.btn:active          {（同上）}                                       (0,3,0)
#   .btn:first-child:active:focus-visible {box-shadow:var(--bs-btn-focus-box-shadow)}     (0,4,0)
#   :not(.btn-check)+.btn:active:focus-visible {（同上）}                                  (0,4,0)
#
# 特异度按 (id, class, type) 三元组比较，与 CSS 规范一致；单个整数比不出
# `#createTaskBtn`(1,0,0) 和 `.a.b.c`(0,3,0) 的大小关系。
BOOTSTRAP_BTN_HOVER_SPECIFICITY = (0, 2, 0)
BOOTSTRAP_BTN_DISABLED_SPECIFICITY = (0, 2, 0)
BOOTSTRAP_BTN_FOCUS_BG_SPECIFICITY = (0, 2, 0)
BOOTSTRAP_BTN_ACTIVE_SPECIFICITY = (0, 3, 0)
BOOTSTRAP_BTN_FOCUS_RING_SPECIFICITY = (0, 4, 0)

_BTN_STATES = ('base', 'hover', 'active', 'focus-visible', 'disabled')

# `@media` / `@supports` 这类**条件组 at-rule** 里的规则是会生效的，模型必须
# 看见；`@keyframes` / `@font-face` / `@page` 里的「选择器」（`0%`、`from`）
# 根本不是选择器，跳过它们是安全的。名单之外的 at-rule 一律按条件组处理 ——
# 宁可报「模型不支持」也不要静默放行。
_BTN_NON_SELECTOR_AT_RULES = ('@keyframes', '@-webkit-keyframes', '@font-face', '@page')


def _btn_specificity(branch):
    """选择器分支的 (id, class, type) 特异度三元组。

    两条容易写错、且都被实测证明是承重的规则：
      - `:not()` 本身不计数，只计它的参数（Selectors L4）：
        `.btn-primary:not(:disabled):hover` 是 (0,3,0) 而非 (0,4,0)。
      - `#id` 必须计进第一位。第一版返回单整数、`#id` 记 0，
        于是 `#createTaskBtn:disabled{...}` 被算成特异度 1（只数了 `:disabled`），
        比 `.btn:disabled` 的 2 还低 —— 模型认为它输了，浏览器里它赢。
    """
    inner = re.sub(r':not\(\s*([^)]*)\s*\)', r' \1 ', branch)
    pseudo_elements = len(re.findall(r'::[-\w]+', inner))
    # 伪类要连函数参数一起吃掉（`:nth-child(1)` 记 1 个，不是 1 个伪类 + 一个残渣）
    without_pe = re.sub(r'::[-\w]+(?:\([^)]*\))?', ' ', inner)
    pseudo_classes = len(re.findall(r':[-\w]+(?:\([^)]*\))?', without_pe))
    no_pseudo = re.sub(r':[-\w]+(?:\([^)]*\))?', ' ', without_pe)
    ids = len(re.findall(r'#[-\w]+', no_pseudo))
    attrs = len(re.findall(r'\[[^\]]*\]', no_pseudo))
    class_names = len(re.findall(r'\.[-\w]+', no_pseudo))
    # 摘掉 id/class/属性之后剩下的裸标识符才是类型选择器；`*` 不计入任何一位
    bare = re.sub(r'([#.][-\w]+|\[[^\]]*\])', ' ', no_pseudo)
    types = len(re.findall(r'(?:^|[\s>+~])([a-zA-Z][-\w]*)', bare))
    return (ids, class_names + pseudo_classes + attrs, types + pseudo_elements)


_BTN_SUPPORTED_PSEUDOS = frozenset({'hover', 'active', 'focus-visible', 'disabled'})


# 按钮层叠模型描述的是**默认渲染环境**：没有开启系统「减少动画」偏好的用户
# 看到的那个界面。A8 / Task 13 往文件末尾加了
# `@media (prefers-reduced-motion: reduce)`，它的选择器是 `*, *::before,
# *::after` —— 命中每一颗按钮，于是撞上了 Task 11 建的条件组 at-rule 安全网。
#
# 安全网的报错文案本身给了唯一正确的出路：「要改按钮的媒体查询样式，先把
# 媒体条件求值加进模型」。这里就是那个求值器。
#
# 只支持 prefers-reduced-motion，**故意不支持宽度类条件**：
#   文件里现有 @media (max-width: 768px) / (min-width: 768px) / (max-width: 576px)
#   / (max-width: 480px) 四块，它们至今没有一条命中过按钮上下文，所以从没走到
#   安全网。给它们加求值就得先钉死一个建模视口宽度，而高度模型讲的是 1366px、
#   密度模型讲的是别的场景 —— 用一个数冒充「全部」正是模型撒谎的开始。
#   保持返回 None（= 模型不支持 = 响亮失败），将来真有人往宽度断点里写按钮样式时，
#   测试会当场变红并要求先扩模型。
_PREFERS_REDUCED_MOTION_RE = re.compile(
    r'^@media\s*\(\s*prefers-reduced-motion\s*:\s*(reduce|no-preference)\s*\)$', re.I)


def _btn_media_applies(at_rule):
    """条件组 at-rule 在「默认渲染环境」下成立吗？True / False / None(模型不支持)。

    返回 False 的后果是该规则被整条跳过 —— 这是**正确**的建模，不是放行：
    在没开减少动画偏好的浏览器里，`@media (prefers-reduced-motion: reduce)`
    里的声明确实一条也不生效。

    ⚠️ 代价要说清楚：开了减少动画偏好的用户看到的按钮外观不在本模型覆盖范围内。
    当前那一块只声明 animation-duration / animation-iteration-count /
    transition-duration 三个属性，`props` 里一个都没有，所以两种建模环境下
    算出来的按钮外观完全一致。哪天有人往那一块里塞颜色/边框，这句话就不成立了
    —— test_reduced_motion_block_only_touches_motion 钉的就是这个前提。
    """
    m = _PREFERS_REDUCED_MOTION_RE.match(re.sub(r'\s+', ' ', at_rule).strip())
    if not m:
        return None
    return m.group(1).lower() == 'no-preference'


def _btn_branch_applies(branch, ctx, state):
    """这个分支在给定状态下命中这颗按钮吗？True / False / None(模型不支持)。

    `ctx` 是一个 `_BtnCtx`：祖先类集合、元素类集合、元素 id。

    **判定顺序是承重的：先判「肯定不命中」，再判「形态不支持」。**
    反过来写的话，`div:not(.card):not(...)...` 这种与按钮八竿子打不着的规则
    会把模型整个顶成「已失效」，而为了绕开它加的那句
    `if 'btn' not in branch: continue` 又会把 `#createTaskBtn`
    （小写 btn 不是它的子串）连同真正该管的规则一起无声丢掉。第一版就是这么
    漏的：`#createTaskBtn:disabled{background:#0d6efd!important}` 全绿。

    状态语义按真实浏览器行为建模：
      - 'active' 同时命中 `:active` 和 `:hover` —— 鼠标按住时两者都在，
        这正是「按下去应该更暗，而不是被 hover 的更亮盖住」要算清楚的地方。
      - `:not(:disabled)` 在 disabled 态一律不命中；`:disabled` 只在 disabled 态命中。
    """
    if '::' in branch:
        return False                          # 伪元素是另一个盒子
    if re.search(r'[>+~]', branch):
        return None                           # 子/兄弟组合符，模型不支持
    compounds = []
    for part in branch.split():
        for arg in re.findall(r':not\(([^)]*)\)', part):
            if not re.fullmatch(r'\s*[.:][-\w]+\s*', arg):
                return None                   # :not() 里是别的东西
        neg_pseudos = set(re.findall(r':not\(\s*:([-\w]+)\s*\)', part))
        neg_classes = set(re.findall(r':not\(\s*\.([-\w]+)\s*\)', part))
        rest = re.sub(r':not\([^)]*\)', '', part)
        ids = re.findall(r'#([-\w]+)', rest)
        classes = set(re.findall(r'\.([-\w]+)', rest))
        # 函数式伪类要连参数一起吃掉，否则 `.card:nth-child(1)` 的 `(1)`
        # 会被当成读不懂的残余，把一条明显不命中的规则误判成「模型不支持」。
        pseudos = set(re.findall(r':([-\w]+)(?:\([^)]*\))?', rest))
        attrs = re.findall(r'\[[^\]]*\]', rest)
        leftover = re.sub(r'([#.][-\w]+|:[-\w]+(?:\([^)]*\))?|\[[^\]]*\]|\*)', '', rest).strip()
        tag = leftover.lower() if leftover else None
        if tag is not None and not re.fullmatch(r'[a-z][-\w]*', tag):
            return None                       # 读不懂的残余
        compounds.append(dict(tag=tag, ids=ids, classes=classes, pseudos=pseudos,
                              neg_pseudos=neg_pseudos, neg_classes=neg_classes,
                              attrs=attrs))
    if not compounds:
        return None

    def attrs_match(attr_list, class_set):
        """只求值**针对 class 属性**的属性选择器（站内只有 `[class*="col-"]` 这一种）。
        其它属性选择器（`[disabled]`、`[type=submit]`）返回 None = 模型不支持。"""
        joined = ' '.join(sorted(class_set))
        for a in attr_list:
            m = re.fullmatch(r'\[\s*class\s*([*^$~|]?=)\s*"([^"]*)"\s*\]', a.strip())
            if not m:
                return None
            op, val = m.group(1), m.group(2)
            if op == '*=':
                ok = any(val in c for c in class_set)
            elif op == '^=':
                ok = joined.startswith(val)
            elif op == '$=':
                ok = joined.endswith(val)
            elif op in ('=', '~='):
                ok = val in class_set
            else:
                return None
            if not ok:
                return False
        return True

    subject = compounds[-1]
    # ---- 第一步：能否**确定地**判为不命中（不需要模型支持全部语法）----
    if subject['tag'] is not None and subject['tag'] != 'button':
        return False
    if not subject['classes'] <= ctx.classes:
        return False
    if subject['neg_classes'] & ctx.classes:
        return False
    if subject['ids'] and (len(subject['ids']) > 1 or subject['ids'][0] != ctx.element_id):
        return False
    if subject['attrs']:
        hit = attrs_match(subject['attrs'], ctx.classes)
        if hit is None:
            return None                       # 例如 [disabled]，模型不认，响亮失败
        if not hit:
            return False
    for anc in compounds[:-1]:
        if anc['pseudos'] or anc['neg_pseudos'] or anc['ids'] or anc['tag'] is not None:
            return None                       # 祖先侧只支持纯类选择器 / `*`
        if anc['attrs']:
            hit = attrs_match(anc['attrs'], ctx.ancestors)
            if hit is None:
                return None
            if not hit:
                return False
        if anc['neg_classes'] & ctx.ancestors:
            return False
        if not anc['classes'] <= ctx.ancestors:
            return False

    # ---- 第二步：确实可能命中，此时才允许报「形态不支持」----
    if not (subject['pseudos'] | subject['neg_pseudos']) <= _BTN_SUPPORTED_PSEUDOS:
        return None

    active_pseudos = {
        'base': set(),
        'hover': {'hover'},
        'active': {'hover', 'active'},
        'focus-visible': {'focus-visible'},
        'disabled': {'disabled'},
    }[state]
    if not subject['pseudos'] <= active_pseudos:
        return False
    if subject['neg_pseudos'] & active_pseudos:
        return False
    return True


class _BtnCtx:
    """一颗按钮在页面里的真实上下文：祖先类、自身类、自身 id。"""

    def __init__(self, classes, ancestors=frozenset(), element_id=None, label=None):
        self.classes = frozenset(classes)
        self.ancestors = frozenset(ancestors)
        self.element_id = element_id
        self.label = label or ('.' + '.'.join(sorted(classes)))

    def __repr__(self):
        return self.label


# 简写 -> 本节关心的长写。**必须展开**：评审实测在
# `outline: 2px solid ...` 后面补一行 `outline-width: 0` 会让焦点环消失，
# 而只读简写的断言全绿；`border` + `border-style: none` 同型。
# 展开时保留声明顺序，让「后面的长写压掉前面的简写」这个真实层叠行为被算进去。
_BTN_SHORTHAND_EXPANSIONS = {
    'outline': ('outline-width', 'outline-style', 'outline-color'),
    'border': ('border-width', 'border-style', 'border-color'),
}
_BTN_LINE_STYLES = frozenset({
    'none', 'hidden', 'dotted', 'dashed', 'solid', 'double',
    'groove', 'ridge', 'inset', 'outset', 'auto',
})


def _split_outline_like(value):
    """`2px solid var(--x)` -> {'-width': '2px', '-style': 'solid', '-color': 'var(--x)'}。

    缺省位按 CSS 规范补 initial：宽度 `medium`(3px)、线型 `none`、颜色 `currentcolor`。
    `outline: 0` / `border: none` 这种单值写法也要能解开 —— 那正是 Bootstrap
    用来干掉焦点环的写法。
    """
    value = _IMPORTANT_RE.sub('', value or '').strip().lower()
    out = {'width': None, 'style': None, 'color': None}
    for tok in re.findall(r'var\([^)]*\)|[^\s]+', value):
        if tok in _BTN_LINE_STYLES:
            out['style'] = tok
        elif _length_to_px(tok) is not None or re.fullmatch(r'0+(\.0+)?', tok):
            out['width'] = tok
        elif tok in ('medium', 'thin', 'thick'):
            out['width'] = {'thin': '1px', 'medium': '3px', 'thick': '5px'}[tok]
        else:
            out['color'] = tok
    if out['width'] is None:
        out['width'] = '3px'                  # initial: medium
    if out['style'] is None:
        out['style'] = 'none'                 # initial
    if out['color'] is None:
        out['color'] = 'currentcolor'
    return out


def _btn_decls(body):
    """规则体 -> [(属性, 值, 是否!important, 声明序号), ...]，简写已展开成长写。

    **序号是承重的，不是装饰。** 同一条规则里 `outline: 2px solid X` 之后再写
    一行 `outline-width: 0`，两者的选择器/特异度/规则序号完全相同，只有声明先后
    能分出胜负。第一版的比较键是 `(important, 特异度, 规则序号)`，同键时
    `key > best[key]` 为假 —— **先出现的赢**，正好和 CSS 反过来。
    评审实测：补一行 `outline-width: 0` 焦点环消失、补 `border-style: none`
    边框消失，两者都 263 passed。
    """
    out = []
    idx = 0
    for chunk in body.split(';'):
        if ':' not in chunk:
            continue
        name, _, raw = chunk.partition(':')
        name = name.strip().lower()
        raw = raw.strip()
        if not name:
            continue
        important = bool(_IMPORTANT_RE.search(raw))
        val = _IMPORTANT_RE.sub('', raw).strip()
        if name in _BTN_SHORTHAND_EXPANSIONS:
            parts = _split_outline_like(val)
            for longhand in _BTN_SHORTHAND_EXPANSIONS[name]:
                out.append((longhand, parts[longhand.rsplit('-', 1)[1]], important, idx))
            out.append((name, val, important, idx))   # 简写本身也留着，便于报错时引用
        elif name == 'background':
            out.append(('background-color', val, important, idx))
            out.append((name, val, important, idx))
        else:
            out.append((name, val, important, idx))
        idx += 1
    return out


def _btn_computed(css, ctx, state, props):
    """模拟 style.css 内部的层叠，返回 {属性: (值, 胜出选择器, 特异度)}。

    安全网（`unsupported` / at-rule 检查）**排在所有 continue 之前**，
    这是第一版最致命的结构缺陷：任何为了「跳过明显无关的规则」而写在安全网
    前面的 continue，都是一个静默漏检口。
    """
    best, unsupported, conditional = {}, [], []
    for order, (sel, body, at_ctx) in enumerate(_rules_ctx(css)):
        if any(a.split()[0] in _BTN_NON_SELECTOR_AT_RULES for a in at_ctx):
            continue                          # @keyframes 里的 `0%` 不是选择器
        decls = [d for d in _btn_decls(body) if d[0] in props]
        for branch in _selector_parts(sel):
            applies = _btn_branch_applies(branch, ctx, state)
            if applies is None:
                unsupported.append(f'{sel}   （分支 {branch!r}）')
                continue
            if not applies:
                continue
            if at_ctx:
                verdicts = [_btn_media_applies(a) for a in at_ctx]
                if any(v is None for v in verdicts):
                    conditional.append(f'{" ".join(at_ctx)} 里的 `{sel}`')
                    continue
                if not all(verdicts):
                    continue      # 条件在建模环境下不成立，浏览器里也不生效
            if not decls:
                continue
            spec = _btn_specificity(branch)
            for name, val, imp, decl_idx in decls:
                # 声明序号必须在键里，否则同一条规则内「后面的长写压掉前面的简写」
                # 这个真实层叠行为算不出来（见 _btn_decls 的说明）。
                key = (imp, spec, order, decl_idx)
                if name not in best or key > best[name][0]:
                    best[name] = (key, val, branch, spec)
    assert not unsupported, (
        f'按钮层叠模型处理不了这些写法（上下文 {ctx}），测试已失效（不是通过）：\n'
        + '\n'.join('  ' + u for u in sorted(set(unsupported)))
        + '\n模型只支持「后代组合符 + 类 / #id / button 类型 / '
        ':hover|:active|:focus-visible|:disabled + :not(单个类或伪类)」。'
        '新写法要么扩展 _btn_branch_applies，要么换一种等价写法'
    )
    assert not conditional, (
        f'有按钮规则写在条件组 at-rule 里（上下文 {ctx}），模型算不了它的条件，'
        '测试已失效（不是通过）：\n'
        + '\n'.join('  ' + c for c in sorted(set(conditional)))
        + '\n⚠️ 这条不是洁癖：评审实测往 `@media (min-width:1px)` 里追加一条 '
        '`.btn:disabled{background:#0d6efd;opacity:.5}` 就能把整套修复撤回，'
        '而当时的模型无声跳过 @media、261 条测试全绿。'
        '要改按钮的媒体查询样式，先把媒体条件求值加进模型'
    )
    return {n: (val, branch, spec) for n, (_k, val, branch, spec) in best.items()}


_BTN_COLOR_KEYWORDS = {
    'transparent': 'rgba(0, 0, 0, 0)',
    'white': '#ffffff',
    'black': '#000000',
    'currentcolor': None,                     # 由调用方代入当前 color
}


def _btn_flatten(color, backdrop):
    """`_flatten` 的按钮版：先把 CSS 里合法但 `_flatten` 不认的写法归一。

    需要归一的几种，每一种都在变异实验里真出现过：
      - `transparent` —— outline 变体和 `.btn-secondary` 的底色
      - `#fff` 三位简写 —— **改前 `.btn-success/.btn-danger/.btn-info` 的墨色**
      - `white` / `black`

    为什么在这里补而不是放宽 `_flatten`：放宽会让其它调用方也悄悄接受这些写法。
    更要紧的是**诊断质量**：不归一的话，把墨色改回 `#fff` 这个变异确实会红，
    但红的理由是「本测试算不了它，已失效」——维护者会以为是测试坏了，
    而不是「墨色只有 1.92:1，图标看不见」。红对了理由才算护栏。
    """
    value = color.strip().lower()
    value = _BTN_COLOR_KEYWORDS.get(value, value)
    assert value is not None, 'currentcolor 必须由调用方先代入具体颜色 —— 本测试已失效'
    m = re.fullmatch(r'#([0-9a-f])([0-9a-f])([0-9a-f])', value)
    if m:
        value = '#' + ''.join(c * 2 for c in m.groups())
    return _flatten(value, backdrop)


def _hsl_saturation(rgb):
    """HSL 饱和度（0..1）。禁用态「不像可点」靠的就是它掉下来。"""
    r, g, b = [c / 255 for c in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return 0.0
    lightness = (mx + mn) / 2
    return (mx - mn) / (2 - mx - mn) if lightness > 0.5 else (mx - mn) / (mx + mn)


def _btn_surface(css, ctx, state, backdrop):
    """一颗按钮在某状态下**肉眼看到**的 (底色 hex, 墨色 hex, 胜出信息)。

    合成链按浏览器的真实顺序走，一环都不能省：
      1. 层叠取胜的 background-color / color
      2. 半透明色压到 backdrop 上（本站 outline 变体的底色是 transparent）
      3. `filter: brightness()` 同时作用于底与墨（active 态）
      4. **整组 opacity 合成**：opacity 作用于「元素这个组」，先把底和墨各自
         画好，再整组压到 backdrop 上 —— 所以它会同时拉低底与墨，
         把「不可点」和「看不清」绑在一起。改前禁用态的 2.83:1 正是这么来的，
         漏掉这一步就算不出那个数字。
    """
    got = _btn_computed(css, ctx, state,
                        {'background', 'background-color', 'color', 'opacity', 'filter'})
    assert 'color' in got, (
        f'{ctx} 在 {state} 态没有任何规则声明 color —— 本测试算不了对比度，已失效'
    )
    assert 'background-color' in got, (
        f'{ctx} 在 {state} 态没有任何规则声明 background —— '
        '意味着 Bootstrap 的变体色会漏进来，本测试算不了它，已失效（不是通过）'
    )
    bg = _btn_flatten(_resolve_color(css, got['background-color'][0]), backdrop)
    fg = _btn_flatten(_resolve_color(css, got['color'][0]), bg)

    if 'filter' in got:
        raw = _IMPORTANT_RE.sub('', got['filter'][0]).strip()
        ops = _filter_ops(raw)
        assert ops or raw == 'none', f'filter 值 {raw!r} 解析不出任何函数 —— 本测试已失效'
        if ops:
            hexify = lambda c: '#%02x%02x%02x' % tuple(
                round(v) for v in _apply_filter(_hex_to_rgb(c), ops))
            bg, fg = hexify(bg), hexify(fg)

    opacity = 1.0
    if 'opacity' in got:
        raw = _IMPORTANT_RE.sub('', got['opacity'][0]).strip()
        try:
            opacity = float(raw)
        except ValueError:
            raise AssertionError(f'opacity 值 {raw!r} 解析不了 —— 本测试已失效')
    if opacity < 1.0:
        back = _hex_to_rgb(backdrop)
        blend = lambda c: '#%02x%02x%02x' % tuple(
            round(opacity * a + (1 - opacity) * b) for a, b in zip(_hex_to_rgb(c), back))
        bg, fg = blend(bg), blend(fg)

    return bg, fg, got


# 站内真实存在的按钮变体（`grep -rn "btn-" templates/ static/js/` 核对过）。
# 值是「基态底色应当引用的调色板变量」。
FILLED_BTN_VARIANTS = {
    'btn-primary': '--color-accent-strong',
    'btn-success': '--color-success',
    'btn-warning': '--color-warning',
    'btn-danger':  '--color-danger',
    'btn-info':    '--color-info',
}
TRANSPARENT_BTN_VARIANTS = ('btn-secondary', 'btn-outline-secondary', 'btn-outline-primary')
ALL_BTN_VARIANTS = tuple(FILLED_BTN_VARIANTS) + TRANSPARENT_BTN_VARIANTS

BTN_INK_MIN_CONTRAST = 4.5          # 按钮上的文字/图标，与正文同一条线
BTN_RING_MIN_CONTRAST = 3.0         # 焦点环 / 边框属于图形对象，WCAG 1.4.11
BTN_HOVER_MIN_LUMINANCE_GAIN = 0.06     # hover 必须**看得出**提亮
BTN_ACTIVE_MIN_LUMINANCE_DROP = 0.03    # active 必须**看得出**压暗
BTN_DISABLED_MAX_SATURATION = 0.30      # 禁用态必须是低饱和中性面
BTN_DISABLED_VS_ENABLED_MIN_CONTRAST = 4.5   # 填充型：禁用面 vs 启用面
# 透明型变体的禁用信号是墨色变暗。0.15 的来历：实测 .btn-secondary 启用墨
# #e8eaed(L=0.795) -> 禁用墨 #9aa0aa(L=0.350)，暗了 0.445，留了 3 倍余量。
BTN_DISABLED_MIN_INK_DIMMING = 0.15
BTN_FOCUS_MIN_OUTLINE_WIDTH_PX = 2.0


def _btn_backdrop(css):
    """按钮压在什么底上 —— 走 `.card` 的真实渲染链，与
    `_effective_task_card_backdrop` 同一个理由、同一个来源。"""
    return _effective_task_card_backdrop(css)


# 站内真实的按钮上下文。**每一个都对应一处真实标记**，不是为了凑覆盖率：
#   `grep -n "btn" templates/*.html static/js/tasks.js static/js/history.js`
_ctx_variants = [
    _BtnCtx({'btn', v}, label=f'.{v}（无特殊祖先）') for v in ALL_BTN_VARIANTS
]
BUTTON_CONTEXTS = _ctx_variants + [
    # index.html:168 —— 提交按钮，**默认 disabled**，本任务的核心缺陷所在
    _BtnCtx({'btn', 'btn-primary', 'w-100'}, element_id='createTaskBtn',
            label='#createTaskBtn（首页提交按钮）'),
    # tasks.js —— 任务卡图标按钮，祖先是 .btn-group.btn-group-sm
    _BtnCtx({'btn', 'btn-icon', 'btn-danger'}, {'btn-group', 'btn-group-sm'},
            label='任务卡 .btn-icon.btn-danger（.btn-group-sm 内）'),
    # history.js —— 历史表图标按钮，**没有 .btn-group 祖先**，是裸 flex 容器
    _BtnCtx({'btn', 'btn-icon', 'btn-sm', 'btn-info'},
            label='历史表 .btn-icon.btn-sm.btn-info（无 btn-group 祖先）'),
    # config.html:218 / history.html:171 —— .config-section 内的按钮
    _BtnCtx({'btn', 'btn-secondary'}, {'config-section'},
            label='配置页 .btn-secondary（.config-section 内）'),
]


def test_button_cascade_model_covers_every_real_context():
    """模型必须能算出**每一个真实上下文** x 每一个状态，一格都不许算不出来。

    这条是模型的自检，也是上面那三个逃逸的正面防线：只要
    `_btn_branch_applies` 又出现「读不懂就无声跳过」，或者有人往 `@media`
    里塞按钮规则，这里会先响亮失败，而不是让下游断言拿着残缺的层叠结果
    得出「一切正常」。
    """
    css = _css()
    backdrop = _btn_backdrop(css)
    assert len(BUTTON_CONTEXTS) == 12, (
        f'真实上下文有 {len(BUTTON_CONTEXTS)} 个，期望 12 —— 本测试已失效'
    )
    for ctx in BUTTON_CONTEXTS:
        for state in _BTN_STATES:
            bg, fg, _got = _btn_surface(css, ctx, state, backdrop)
            assert re.fullmatch(r'#[0-9a-f]{6}', bg), f'{ctx}/{state} 底色算成 {bg!r}'
            assert re.fullmatch(r'#[0-9a-f]{6}', fg), f'{ctx}/{state} 墨色算成 {fg!r}'


def test_disabled_button_cannot_be_mistaken_for_clickable():
    """禁用态必须**同时**满足：不像可点、且仍然看得清。

    **这是 A6 / Task 11 的核心缺陷本身。** `#createTaskBtn` 默认就是禁用的
    （要先在地图上框选），是用户打开页面的第一眼。

    改前 CDP 实测（1366x768，Chrome 148 headless）：
        computed background-color = rgb(13, 110, 253)   ← Bootstrap 主色蓝 #0d6efd
        computed color            = rgb(255, 255, 255)
        computed opacity          = 0.5
      整组 50% 压到卡片底 #15171c 之后，屏幕上真正的像素是
        底 rgb(17, 66, 140)   文字 rgb(138, 139, 142)   对比度 2.83:1
      —— 一颗饱和度 78% 的蓝色按钮，看着完全可点，文字还不达标。
      根因：Bootstrap 的 `.btn:disabled`(0,2,0) 读 `--bs-btn-disabled-bg`(#0d6efd)，
      压过本站的 `.btn-primary`(0,1,0)。

    改后：底 #1c2027（饱和度 16.4%）、字 #9aa0aa、opacity 1，
        墨/底 6.21:1，禁用面 vs 启用面 6.56:1。

    四条数值护栏，每一条**单独**都能把改前的形态判红：
        墨/底对比度   >= 4.5    （改前 2.83）
        禁用面饱和度  <= 0.30   （改前 0.78）
        禁用 vs 启用  >= 4.5    （改前 3.87）
        cursor        == not-allowed
    另加特异度护栏：胜出的 background 必须来自 >= (0,2,0) 的选择器。
    """
    css = _css()
    backdrop = _btn_backdrop(css)
    assert len(BUTTON_CONTEXTS) == 12, '上下文表变了 —— 本测试已失效'
    problems = []
    for ctx in BUTTON_CONTEXTS:
        off_bg, off_fg, off_got = _btn_surface(css, ctx, 'disabled', backdrop)
        on_bg, _on_fg, _on = _btn_surface(css, ctx, 'base', backdrop)

        ink = _contrast_ratio(off_fg, off_bg)
        if ink < BTN_INK_MIN_CONTRAST:
            problems.append(
                f'{ctx} 禁用态墨/底 {off_fg} on {off_bg} = {ink:.2f}:1 '
                f'< {BTN_INK_MIN_CONTRAST}（不可点 != 看不清）')

        sat = _hsl_saturation(_hex_to_rgb(off_bg))
        if sat > BTN_DISABLED_MAX_SATURATION:
            problems.append(
                f'{ctx} 禁用面 {off_bg} 饱和度 {sat:.3f} '
                f'> {BTN_DISABLED_MAX_SATURATION} —— 还是一颗「看着可点」的彩色按钮')

        # 「禁用 vs 启用要分得开」按变体形态分两种量法 —— 不是为了让测试变绿，
        # 是因为两种按钮的**可见信号本来就不在同一个通道**：
        #   填充型（启用态有实心底）：信号是底色，量底色分离度。
        #   透明型（.btn-secondary / .btn-outline-*，启用态底色就是卡片底）：
        #     底色天然与背景同色，量它只会得到 1.10:1 这种与设计无关的数字；
        #     真正的信号是墨色变暗（#e8eaed -> #9aa0aa）+ 冒出一块实心底。
        # 改前那个缺陷是填充型（#createTaskBtn），走第一支，3.87 < 4.5 照样判红。
        if on_bg != backdrop:
            sep = _contrast_ratio(off_bg, on_bg)
            if sep < BTN_DISABLED_VS_ENABLED_MIN_CONTRAST:
                problems.append(
                    f'{ctx} 禁用面 {off_bg} 与启用面 {on_bg} 只差 {sep:.2f}:1 '
                    f'< {BTN_DISABLED_VS_ENABLED_MIN_CONTRAST} —— 两个状态分不出来')
        else:
            dim = (_relative_luminance(_hex_to_rgb(_on_fg))
                   - _relative_luminance(_hex_to_rgb(off_fg)))
            if dim < BTN_DISABLED_MIN_INK_DIMMING:
                problems.append(
                    f'{ctx} 是透明底变体，禁用态只能靠墨色变暗表达；'
                    f'启用墨 {_on_fg} -> 禁用墨 {off_fg} 只暗了 {dim:+.4f} '
                    f'< {BTN_DISABLED_MIN_INK_DIMMING} —— 两个状态分不出来')

        _v, branch, spec = off_got['background-color']
        if spec < BOOTSTRAP_BTN_DISABLED_SPECIFICITY:
            problems.append(
                f'{ctx} 禁用态的 background 由 `{branch}` 胜出，特异度 {spec} '
                f'< Bootstrap `.btn:disabled` 的 {BOOTSTRAP_BTN_DISABLED_SPECIFICITY} '
                '—— 浏览器里会被 --bs-btn-disabled-bg 抢回去（改前就是这样）')

        cursor = _btn_computed(css, ctx, 'disabled', {'cursor'})
        assert 'cursor' in cursor, f'{ctx} 禁用态没有声明 cursor —— 本测试已失效'
        if _IMPORTANT_RE.sub('', cursor['cursor'][0]).strip() != 'not-allowed':
            problems.append(f'{ctx} 禁用态 cursor = {cursor["cursor"][0]!r}，应为 not-allowed')

    assert not problems, (
        '禁用态仍然像可点（或已经看不清）：\n' + '\n'.join('  ' + p for p in problems)
        + '\n改前实测：底 rgb(17,66,140) 文字 rgb(138,139,142) 2.83:1，'
        '而 #createTaskBtn 默认就是这个状态'
    )


def test_button_hover_is_a_real_change():
    """每个填充变体的 hover 都必须**看得出**比基态亮。

    守的是一个具体的历史缺陷：改前 `.btn-success` 与 `.btn-success:hover`
    **逐字相同**（warning / danger / info 同样），规则存在、grep 得到、
    浏览器里零变化。任何「这条规则在不在」式的断言都会给它发绿灯，
    只有算出两个状态的最终底色再相减才抓得住 —— 那时的差值是 0.000。

    阈值 0.06（相对亮度绝对差）的来历：本次四档提亮里最小的一档是
    warning #fbbf24 -> #fcd34d，实测 +0.099；accent 是 +0.145。
    留 0.04 的余量给后续微调，同时把「提亮 0.01 装装样子」挡在外面。
    """
    css = _css()
    backdrop = _btn_backdrop(css)
    assert len(FILLED_BTN_VARIANTS) == 5, '变体表变了 —— 本测试已失效'
    problems = []
    for variant in FILLED_BTN_VARIANTS:
        ctx = _BtnCtx({'btn', variant})
        base_bg, _f, _g = _btn_surface(css, ctx, 'base', backdrop)
        hov_bg, hov_fg, hov_got = _btn_surface(css, ctx, 'hover', backdrop)

        gain = _relative_luminance(_hex_to_rgb(hov_bg)) - _relative_luminance(_hex_to_rgb(base_bg))
        if gain < BTN_HOVER_MIN_LUMINANCE_GAIN:
            problems.append(
                f'.{variant}:hover 底色 {hov_bg} 相对基态 {base_bg} 只提亮了 '
                f'{gain:+.4f} < {BTN_HOVER_MIN_LUMINANCE_GAIN} —— '
                + ('两者完全相同，hover 是空操作（改前正是如此）'
                   if hov_bg == base_bg else '肉眼看不出变化'))

        ink = _contrast_ratio(hov_fg, hov_bg)
        if ink < BTN_INK_MIN_CONTRAST:
            problems.append(f'.{variant}:hover 墨/底 {ink:.2f}:1 < {BTN_INK_MIN_CONTRAST}')

        _v, branch, spec = hov_got['background-color']
        if spec < BOOTSTRAP_BTN_HOVER_SPECIFICITY:
            problems.append(
                f'.{variant}:hover 的 background 由 `{branch}` 胜出，特异度 {spec} '
                f'< {BOOTSTRAP_BTN_HOVER_SPECIFICITY} —— 会被 Bootstrap 的 `.btn:hover` 覆盖')

    assert not problems, 'hover 态不是真的变化：\n' + '\n'.join('  ' + p for p in problems)


def test_button_active_is_darker_than_base():
    """按下去必须**看得出**比基态暗，并且按下时的墨仍然读得清。

    模型里 'active' 态同时命中 `:active` 和 `:hover`（鼠标按住时两者都在）。
    这一点是承重的：如果 active 只压暗、不重新声明底色，hover 的提亮会盖过来，
    按下反而更亮。

    特异度这条同样承重，而且第一版把它写错过：Bootstrap 的
    `.btn:first-child:active` / `:not(.btn-check)+.btn:active` 是 **(0,3,0)**，
    第一版记成 (0,2,0)，于是把 16 条状态规则的 `:not(:disabled)` 全删掉
    （(0,3,0) -> (0,2,0)）测试仍然全绿，而浏览器里按下态变回 rgb(10,88,202)。
    """
    css = _css()
    backdrop = _btn_backdrop(css)
    assert len(FILLED_BTN_VARIANTS) == 5, '变体表变了 —— 本测试已失效'
    problems = []
    for variant in FILLED_BTN_VARIANTS:
        ctx = _BtnCtx({'btn', variant})
        base_bg, _f, _g = _btn_surface(css, ctx, 'base', backdrop)
        act_bg, act_fg, act_got = _btn_surface(css, ctx, 'active', backdrop)

        drop = _relative_luminance(_hex_to_rgb(base_bg)) - _relative_luminance(_hex_to_rgb(act_bg))
        if drop < BTN_ACTIVE_MIN_LUMINANCE_DROP:
            problems.append(
                f'.{variant}:active 底色 {act_bg} 相对基态 {base_bg} 只压暗了 '
                f'{drop:+.4f} < {BTN_ACTIVE_MIN_LUMINANCE_DROP} —— 按下去没有反馈'
                + ('（多半是被同时命中的 :hover 提亮盖过去了）' if drop < 0 else ''))

        ink = _contrast_ratio(act_fg, act_bg)
        if ink < BTN_INK_MIN_CONTRAST:
            problems.append(
                f'.{variant}:active 压暗后墨/底 {act_fg} on {act_bg} = {ink:.2f}:1 '
                f'< {BTN_INK_MIN_CONTRAST} —— 压过头了')

        _v, branch, spec = act_got['background-color']
        if spec < BOOTSTRAP_BTN_ACTIVE_SPECIFICITY:
            problems.append(
                f'.{variant}:active 的 background 由 `{branch}` 胜出，特异度 {spec} '
                f'< Bootstrap `.btn:first-child:active` 的 '
                f'{BOOTSTRAP_BTN_ACTIVE_SPECIFICITY} —— 会被 --bs-btn-active-bg 抢走'
                '（改前实测 rgb(10,88,202)，Bootstrap 深蓝）')

    assert not problems, 'active 态没有压暗反馈：\n' + '\n'.join('  ' + p for p in problems)


def test_focus_visible_has_a_visible_outline():
    """键盘焦点必须有**看得见**的轮廓，底色不许被抢，焦点环不许被 box-shadow 反超。

    三件独立的事：

    1. **轮廓真的画出来了。** Bootstrap 的 `.btn:focus-visible` 明写
       `outline: 0`，靠 box-shadow 做焦点环。改前 CDP 实测
       `outline-width: 0px`、底色 rgb(11,94,215)（Bootstrap 蓝）。
       读的是**展开成长写、按声明顺序层叠之后**的宽度/线型/颜色——
       只读 `outline` 简写的话，后面补一行 `outline-width: 0` 就能让环消失
       而断言全绿（评审实测）。

    2. **底色回到基态。** `.btn:focus-visible`(0,2,0) 会把 background 刷成
       `--bs-btn-hover-bg`，本站 `.btn-primary`(0,1,0) 拦不住。

    3. **box-shadow 必须被显式清掉，且特异度够。** Bootstrap 的
       `.btn:first-child:active:focus-visible{box-shadow:var(--bs-btn-focus-box-shadow)}`
       是 **(0,4,0)**（第一版记成 (0,3,0)，低了一位）。特异度不够的话，
       键盘焦点 + 按下时会同时出现我们的青绿环和 Bootstrap 的蓝雾环。
    """
    css = _css()
    backdrop = _btn_backdrop(css)
    assert len(BUTTON_CONTEXTS) == 12, '上下文表变了 —— 本测试已失效'
    problems = []
    for ctx in BUTTON_CONTEXTS:
        got = _btn_computed(css, ctx, 'focus-visible',
                            {'outline', 'outline-width', 'outline-style', 'outline-color',
                             'box-shadow'})
        for prop in ('outline-width', 'outline-style', 'outline-color'):
            if prop not in got:
                problems.append(f'{ctx}:focus-visible 没有任何规则声明 {prop} —— '
                                'Bootstrap 的 `outline: 0` 生效，键盘用户看不到焦点')
        if not {'outline-width', 'outline-style', 'outline-color'} <= set(got):
            continue

        w_raw, w_branch, w_spec = got['outline-width']
        width = _resolve_length_px(css, w_raw)
        if width is None or width < BTN_FOCUS_MIN_OUTLINE_WIDTH_PX:
            problems.append(
                f'{ctx}:focus-visible 层叠之后的 outline-width 是 {w_raw!r}'
                f'（来自 `{w_branch}`），不足 {BTN_FOCUS_MIN_OUTLINE_WIDTH_PX}px')
        s_raw, s_branch, _ = got['outline-style']
        if s_raw.strip().lower() in ('none', 'hidden'):
            problems.append(
                f'{ctx}:focus-visible 层叠之后的 outline-style 是 {s_raw!r}'
                f'（来自 `{s_branch}`）—— 画不出来')
        c_raw, _cb, _cs = got['outline-color']
        ring = _btn_flatten(_resolve_color(css, c_raw), backdrop)
        ratio = _contrast_ratio(ring, backdrop)
        if ratio < BTN_RING_MIN_CONTRAST:
            problems.append(
                f'{ctx}:focus-visible 焦点环 {ring} 对卡片底 {backdrop} '
                f'只有 {ratio:.2f}:1 < {BTN_RING_MIN_CONTRAST}')
        if w_spec < BOOTSTRAP_BTN_FOCUS_RING_SPECIFICITY:
            problems.append(
                f'{ctx}:focus-visible 的 outline 来自 `{w_branch}`，特异度 {w_spec} '
                f'< Bootstrap `.btn:first-child:active:focus-visible` 的 '
                f'{BOOTSTRAP_BTN_FOCUS_RING_SPECIFICITY}')

        if 'box-shadow' not in got:
            problems.append(
                f'{ctx}:focus-visible 没有清掉 box-shadow —— '
                'Bootstrap 的蓝雾焦点环会和我们的青绿环同时出现')
        else:
            b_raw, b_branch, b_spec = got['box-shadow']
            if b_raw.strip().lower() != 'none':
                problems.append(
                    f'{ctx}:focus-visible 的 box-shadow = {b_raw!r}（来自 `{b_branch}`），'
                    '期望 none')
            if b_spec < BOOTSTRAP_BTN_FOCUS_RING_SPECIFICITY:
                problems.append(
                    f'{ctx}:focus-visible 的 box-shadow 清除规则 `{b_branch}` 特异度 '
                    f'{b_spec} < {BOOTSTRAP_BTN_FOCUS_RING_SPECIFICITY} —— 压不住蓝雾环')

        base_bg, _bf, _x = _btn_surface(css, ctx, 'base', backdrop)
        foc_bg, foc_fg, foc_got = _btn_surface(css, ctx, 'focus-visible', backdrop)
        if foc_bg != base_bg:
            problems.append(
                f'{ctx}:focus-visible 底色 {foc_bg} != 基态 {base_bg} —— '
                '键盘焦点不该改变按钮底色')
        _v, bbranch, bspec = foc_got['background-color']
        if bspec < BOOTSTRAP_BTN_FOCUS_BG_SPECIFICITY:
            problems.append(
                f'{ctx}:focus-visible 的 background 由 `{bbranch}` 胜出，'
                f'特异度 {bspec} < {BOOTSTRAP_BTN_FOCUS_BG_SPECIFICITY} —— '
                '浏览器里会被刷成 --bs-btn-hover-bg（改前实测 rgb(11,94,215)）')
        ink = _contrast_ratio(foc_fg, foc_bg)
        if ink < BTN_INK_MIN_CONTRAST:
            problems.append(f'{ctx}:focus-visible 墨/底 {ink:.2f}:1 < {BTN_INK_MIN_CONTRAST}')

    assert not problems, '键盘焦点态不合格：\n' + '\n'.join('  ' + p for p in problems)


def test_outline_button_variants_have_a_real_border():
    """`.btn-outline-*` 必须有真边框和读得清的文字。

    改前的形态：`btn-outline` 在 style.css 里**零定义**，而本文件的
    `.btn { border: none }`(0,1,0) 排在 bootstrap.min.css 之后，吃掉了
    Bootstrap 给 outline 变体的 border。CDP 实测 history.html 的「刷新」按钮：
        border-top-width: 0px   border-top-style: none
        color: rgb(108, 117, 125)   对卡片底 #15171c 只有 3.82:1
    渲染成一坨没有边框的灰字。`.btn-outline-primary` 用在 map.js 的等高线
    预览面板（同样零定义，文字是 Bootstrap 蓝 #0d6efd，对卡片底 3.98:1）。

    读的是**展开成长写之后**的边框：只读 `border` 简写的话，后面补一行
    `border-style: none` 就能让边框消失而断言全绿（评审实测的同型逃逸）。
    """
    css = _css()
    backdrop = _btn_backdrop(css)
    outline_variants = [v for v in TRANSPARENT_BTN_VARIANTS if v.startswith('btn-outline-')]
    assert len(outline_variants) == 2, (
        f'outline 变体有 {len(outline_variants)} 个，期望 2 —— 本测试已失效'
    )
    problems = []
    for variant in outline_variants:
        ctx = _BtnCtx({'btn', variant})
        got = _btn_computed(css, ctx, 'base',
                            {'border', 'border-width', 'border-style', 'border-color'})
        for prop in ('border-width', 'border-style'):
            assert prop in got, f'.{variant} 连一条 {prop} 都没有 —— 本测试已失效'
        w_raw, w_branch, _ = got['border-width']
        width = _resolve_length_px(css, w_raw)
        if width is None or width < 1.0:
            problems.append(
                f'.{variant} 层叠之后的 border-width 是 {w_raw!r}（来自 `{w_branch}`）—— '
                '没有边框的 outline 按钮就是一坨纯文字')
        s_raw, s_branch, _ = got['border-style']
        if s_raw.strip().lower() in ('none', 'hidden'):
            problems.append(
                f'.{variant} 层叠之后的 border-style 是 {s_raw!r}（来自 `{s_branch}`）')
        if 'border-color' in got:
            edge = _btn_flatten(_resolve_color(css, got['border-color'][0]), backdrop)
            _ = _contrast_ratio(edge, backdrop)   # 记录用；下限见报告的遗留条目

        bg, fg, _g = _btn_surface(css, ctx, 'base', backdrop)
        ratio = _contrast_ratio(fg, bg)
        if ratio < BTN_INK_MIN_CONTRAST:
            problems.append(
                f'.{variant} 文字 {fg} 对底 {bg} 只有 {ratio:.2f}:1 '
                f'< {BTN_INK_MIN_CONTRAST}（改前「刷新」按钮实测 3.82:1）')

    assert not problems, (
        'outline 变体没有真边框 / 文字看不清：\n' + '\n'.join('  ' + p for p in problems))


def test_button_ink_is_readable_in_every_state():
    """每个真实上下文 x 每个状态，墨/底对比度都必须 >= 4.5:1。

    这条是兜底网，补的是「按状态分开写的那几条断言各自只看自己那一格」
    留下的洞 —— 尤其是**基态**：改前 `.btn-success` / `.btn-danger` /
    `.btn-info` 的墨色是 `#fff`，实测

        #fff on #34d399 = 1.92:1     #fff on #f87171 = 2.77:1
        #fff on #60a5fa = 2.54:1

    而这三颗按钮在任务卡和历史表里都是**纯图标**按钮，SVG 用
    `stroke="currentColor"` 描边 —— 1.92:1 意味着图标近乎看不见。
    """
    css = _css()
    backdrop = _btn_backdrop(css)
    cells = [(c, s) for c in BUTTON_CONTEXTS for s in _BTN_STATES]
    assert len(cells) == 60, (
        f'上下文 x 状态 = {len(cells)} 格，期望 12 x 5 = 60 —— 本测试已失效'
    )
    problems = []
    for ctx, state in cells:
        bg, fg, _got = _btn_surface(css, ctx, state, backdrop)
        ratio = _contrast_ratio(fg, bg)
        if ratio < BTN_INK_MIN_CONTRAST:
            problems.append(
                f'{ctx} 在 {state} 态：墨 {fg} 压在底 {bg} 上只有 {ratio:.2f}:1 '
                f'< {BTN_INK_MIN_CONTRAST}')
    assert not problems, (
        '按钮上的文字/图标看不清：\n' + '\n'.join('  ' + p for p in problems)
        + '\n其中三颗是纯图标按钮（SVG 走 currentColor），墨色不达标 = 图标消失'
    )


# --------------------------------------------------------------------------
# 纯图标按钮：尺寸走密度令牌 + 无障碍名称
# --------------------------------------------------------------------------

# **全站**纯图标按钮（无可见文本）的数量，JS 模板与 HTML 模板一起扫。
#   static/js/tasks.js   启动 / 暂停 / 恢复 / 取消              4
#   static/js/history.js 查看详情 / 删除                        2
#   templates/base.html  navbar-toggler（汉堡菜单）             1
#   templates/history.html .btn-close（模态框关闭）              1
# 合计 8。
#
# ⚠️ 第一版只扫两个 JS 文件、常量写 6，读起来像「全站都覆盖了」而实际漏了
# 模板。评审实测：`templates/base.html:40` 的 navbar-toggler 在 900px 视口下
# `display: block`、56x40、**`aria-label` 为 null** —— 一颗真实存在、真实可见、
# 真实缺无障碍名称的纯图标按钮，就在断言的扫描范围外。
# `.btn-close` 也算进来：它确实是一颗没有可见文本的按钮。它的 aria-label 原本
# 是 Bootstrap 默认的英文 "Close"，在整站中文界面里读屏会念出 "Close"，
# 已一并改成「关闭」。它不走 `.btn-icon`（有自己的尺寸规则），所以只参与标签断言，不参与下面的尺寸断言。
ICON_ONLY_BUTTON_COUNT = 8

_JS_BUTTON_RE = re.compile(r'<button\b([^>]*)>(.*?)</button>', re.S)

# 纯图标按钮里允许出现的「不是可见文本」的东西：标签、模板插值、HTML 实体。
_MARKUP_NOISE_RE = re.compile(r'<[^>]*>|\$\{[^}]*\}|&[a-zA-Z]+;|&#\d+;')


def _icon_only_buttons():
    """全站（JS 模板 + HTML 模板）里所有「没有可见文本」的 <button>。

    返回 [(来源, 属性串)]。**每个来源都断言至少扫到一个 <button>**，
    正则失配时响亮失败而不是退化成空循环。
    """
    sources = []
    for name in ('tasks.js', 'history.js'):
        sources.append((f'static/js/{name}', _strip_js_comments(_js(name))))
    for name in ('base.html', 'index.html', 'history.html', 'config.html'):
        sources.append((f'templates/{name}', _template(name)))

    found = []
    for label, src in sources:
        matches = list(_JS_BUTTON_RE.finditer(src))
        assert matches, f'{label} 里一个 <button> 都没扫到 —— 本测试已失效（不是通过）'
        for m in matches:
            attrs, inner = m.group(1), m.group(2)
            if not _MARKUP_NOISE_RE.sub('', inner).strip():
                found.append((label, attrs))
    return found


def test_icon_only_buttons_are_labelled():
    """**全站**每一颗纯图标按钮都必须有 aria-label。

    图标按钮没有可见文本，`title` 不是可靠的无障碍名称来源（多数屏幕阅读器
    只在没有别的名称时才回退到它，移动端更是根本没有 hover）。
    先断言扫到的数量 == ICON_ONLY_BUTTON_COUNT，正则失配会响亮失败。
    """
    buttons = _icon_only_buttons()
    assert len(buttons) == ICON_ONLY_BUTTON_COUNT, (
        f'扫到 {len(buttons)} 颗纯图标按钮，期望 {ICON_ONLY_BUTTON_COUNT} —— '
        '要么正则失配（本测试已失效），要么有人加/删了图标按钮（确认后改常量）：\n'
        + '\n'.join(f'  {f}: {a.strip()[:90]}' for f, a in buttons)
    )
    missing = [
        f'{f}: `{a.strip()[:80]}`' for f, a in buttons
        if not re.search(r'aria-label\s*=\s*"[^"]+"', a)
    ]
    assert not missing, (
        '纯图标按钮缺 aria-label（屏幕阅读器只会读出「按钮」）：\n'
        + '\n'.join('  ' + m for m in missing)
    )

    # 每一颗**走 .btn 体系**的纯图标按钮都必须带 .btn-icon，否则下面那条尺寸
    # 断言（按 BUTTON_CONTEXTS 里写死的类组合去算）根本轮不到它 —— 类掉了，
    # 按钮在浏览器里被 `.btn-group-sm .btn` 的 padding 撑成胶囊，而尺寸断言
    # 仍然在给一个「假想的、带 btn-icon 的按钮」发绿灯。
    # `.btn-close` / `.navbar-toggler` 不带 `btn` 类，是 Bootstrap 的独立组件，
    # 有自己的尺寸规则，天然不在此列。
    classless = []
    for f, a in buttons:
        m = re.search(r'class\s*=\s*"([^"]*)"', a)
        tokens = set((m.group(1) if m else '').split())
        if 'btn' in tokens and 'btn-icon' not in tokens:
            classless.append(f'{f}: `{a.strip()[:80]}`')
    assert not classless, (
        '纯图标按钮带 .btn 却没有 .btn-icon —— 尺寸规则打不到它，'
        '会被 `.btn-group-sm .btn` 的 padding 撑成胶囊：\n'
        + '\n'.join('  ' + c for c in classless)
    )


def test_icon_buttons_are_square_via_the_density_token():
    """`.btn-icon` 必须在**每个真实上下文**里都是 `--ctl-h` 见方、内边距 0。

    选择器必须是 `.btn.btn-icon`(0,2,0)：任务卡的容器是
    `.btn-group.btn-group-sm`，而 `.btn-group-sm .btn { padding: .4rem .9rem }`
    也是 (0,2,0) —— 裸 `.btn-icon`(0,1,0) 的 `padding: 0` 会输给它，
    按钮被撑成胶囊。历史表那颗**没有** btn-group 祖先、但带 `.btn-sm`
    （自带 padding），是另一条独立的层叠路径，所以两个上下文都要算。
    """
    css = _css()
    ctl_h = _token_px(css, '--ctl-h')
    contexts = [c for c in BUTTON_CONTEXTS if 'btn-icon' in c.classes]
    assert len(contexts) == 2, (
        f'带 .btn-icon 的真实上下文有 {len(contexts)} 个，期望 2'
        '（任务卡 btn-group-sm 内 + 历史表无 btn-group）—— 本测试已失效'
    )
    problems = []
    for ctx in contexts:
        got = _btn_computed(css, ctx, 'base',
                            {'width', 'height', 'padding', 'padding-top', 'padding-right',
                             'padding-bottom', 'padding-left'})
        for prop in ('width', 'height'):
            if prop not in got:
                problems.append(f'{ctx} 没有声明 {prop} —— 图标按钮不是正方形')
                continue
            px = _resolve_length_px(css, got[prop][0])
            if px is None:
                problems.append(f'{ctx} 的 {prop} = {got[prop][0]!r} 解析不了')
            elif px != pytest.approx(ctl_h, abs=0.01):
                problems.append(
                    f'{ctx} 的 {prop} 是 {px:g}px，与密度令牌 --ctl-h({ctl_h:g}px) 不一致'
                    f'（胜出规则 `{got[prop][1]}`）。按钮尺寸必须走令牌，改令牌才管用')
        if 'padding' not in got:
            problems.append(f'{ctx} 没有声明 padding')
            continue
        pad_raw, pad_branch, pad_spec = got['padding']
        pad_px = _resolve_length_px(css, pad_raw.split()[0])
        if pad_px is None or pad_px != pytest.approx(0.0, abs=0.01):
            problems.append(
                f'{ctx} 最终生效的 padding 是 `{pad_branch}` 的 {pad_raw!r}，期望 0')
        if pad_spec < (0, 2, 0):
            problems.append(
                f'{ctx} 的 padding 来自特异度 {pad_spec} 的 `{pad_branch}`，'
                '低于 `.btn-group-sm .btn` 的 (0,2,0) —— 会被撑成胶囊')
    assert not problems, '图标按钮的尺寸不对：\n' + '\n'.join('  ' + p for p in problems)


# ==========================================================================
# A7 / Task 12：文字对比度与状态语义
#
# 本节要守的两个事实，都是 CDP 实测出来的，不是推的：
#
#   1. history.js 的「加载失败」行写的是
#      `<td class="text-center text-danger">加载失败</td>`，
#      而它**从上线起就没红过**。原因不是 Bootstrap，是本文件自己的
#      `.table td, .table th { color: var(--color-text-primary) !important }`
#      —— (0,1,1)!important 压过 `.text-danger` 的 (0,1,0)!important。
#      改前 CDP 实测 `rgb(232, 234, 237)`，与正常单元格**一模一样**：
#      加载失败和加载成功长得没有任何区别。
#
#   2. `--color-text-muted (#5f6670)` 对三层背景分别只有 3.09 / 3.09 / 2.82:1，
#      全部低于 WCAG AA 正文要求的 4.5:1，而它被用在首页三个分组标题、
#      详情弹窗全部 19 个字段名、4 条表单说明、空态提示和输入框占位符上。
#
# 为什么这里要建一个层叠模型，而不是查「某条规则写了什么颜色」：
# 上面第 1 条恰恰是「规则写对了、渲染出来是错的」——`.text-danger` 的规则
# 一直在文件里、值也一直是 var(--color-danger)，查规则的断言全绿。
# 只有把同一个元素上**所有**命中规则按 (important, 特异度, 源码序) 排一遍、
# 取胜出者，才看得见这个缺陷。
# ==========================================================================

WCAG_AA_TEXT_CONTRAST = 4.5

# 模型支持的伪类 = 「元素的交互状态」这一类。
# `:first-child` / `:nth-child()` 这种结构性伪类**故意不列入** —— 模型不知道
# 节点在父级里排第几，列进来等于允许模型对一条它算不清的规则给出 True/False。
# 不在表里的伪类会让 `_text_branch_applies` 返回 None，调用方响亮失败。
_TEXT_SUPPORTED_PSEUDOS = frozenset({'hover', 'focus', 'focus-visible', 'active', 'disabled'})


class _TextEl:
    """层叠模型里的一个节点：标签、类、id、当前激活的伪类、伪元素。"""

    __slots__ = ('tag', 'classes', 'element_id', 'pseudos', 'pseudo_element')

    def __init__(self, tag=None, classes=(), element_id=None, pseudos=(), pseudo_element=None):
        self.tag = tag
        self.classes = frozenset(classes)
        self.element_id = element_id
        self.pseudos = frozenset(pseudos)
        self.pseudo_element = pseudo_element

    def __repr__(self):
        s = self.tag or ''
        if self.element_id:
            s += '#' + self.element_id
        s += ''.join('.' + c for c in sorted(self.classes))
        s += ''.join(':' + p for p in sorted(self.pseudos))
        if self.pseudo_element:
            s += '::' + self.pseudo_element
        return s or '*'


def _parse_compound(part):
    """一个复合选择器（不含组合符）-> dict；读不懂返回 None（= 模型不支持）。"""
    for arg in re.findall(r':not\(([^)]*)\)', part):
        if not re.fullmatch(r'\s*[.:][-\w]+\s*', arg):
            return None
    neg_pseudos = set(re.findall(r':not\(\s*:([-\w]+)\s*\)', part))
    neg_classes = set(re.findall(r':not\(\s*\.([-\w]+)\s*\)', part))
    rest = re.sub(r':not\([^)]*\)', '', part)
    pseudo_elements = re.findall(r'::([-\w]+)', rest)
    if len(pseudo_elements) > 1:
        return None
    rest = re.sub(r'::[-\w]+', '', rest)
    ids = re.findall(r'#([-\w]+)', rest)
    classes = set(re.findall(r'\.([-\w]+)', rest))
    # 函数式伪类连参数一起吃掉，否则 `(1)` 会变成读不懂的残余
    pseudos = set(re.findall(r':([-\w]+)(?:\([^)]*\))?', rest))
    leftover = re.sub(r'([#.][-\w]+|:[-\w]+(?:\([^)]*\))?|\[[^\]]*\]|\*)', '', rest).strip()
    if leftover and not re.fullmatch(r'[a-zA-Z][-\w]*', leftover):
        return None
    return dict(tag=(leftover.lower() or None), ids=ids, classes=classes, pseudos=pseudos,
                neg_pseudos=neg_pseudos, neg_classes=neg_classes,
                pseudo_element=(pseudo_elements[0] if pseudo_elements else None))


def _compound_structurally_matches(comp, node):
    """只看标签/类/id/伪元素这些**确定**的部分。伪类支持性另判。"""
    if comp['pseudo_element'] != node.pseudo_element:
        return False
    if comp['tag'] is not None and comp['tag'] != node.tag:
        return False
    if not comp['classes'] <= node.classes:
        return False
    if comp['neg_classes'] & node.classes:
        return False
    if comp['ids'] and (len(comp['ids']) > 1 or comp['ids'][0] != node.element_id):
        return False
    return True


def _text_branch_applies(branch, chain):
    """这个选择器分支命中链尾那个节点吗？True / False / None(模型不支持)。

    判定顺序与 `_btn_branch_applies` 一致，而且是承重的：**先判「肯定不命中」，
    再判「形态不支持」**。反过来写的话，`div:not(.card):not(...)...` 这种
    与目标元素八竿子打不着的规则会把整个模型顶成「已失效」。

    style.css 里没有任何 `>` / `+` / `~` 组合符，也没有任何 @media 规则声明
    color —— 这两条前提由 `test_text_color_model_assumptions_still_hold` 钉住，
    前提被打破时是那条测试变红，而不是本函数悄悄按后代关系算错。
    """
    parts = branch.split()
    compounds = []
    for p in parts:
        c = _parse_compound(p)
        if c is None:
            return None
        compounds.append(c)
    if not compounds:
        return None
    subject, ancestors = compounds[-1], compounds[:-1]

    # ---- 第一步：能否**确定地**判为不命中 ----
    if not _compound_structurally_matches(subject, chain[-1]):
        return False
    # 祖先侧从右向左做子序列匹配（纯后代组合符下贪心是正确的）
    matched_ancestors = []
    i = len(chain) - 2
    for comp in reversed(ancestors):
        while i >= 0 and not _compound_structurally_matches(comp, chain[i]):
            i -= 1
        if i < 0:
            return False
        matched_ancestors.append((comp, chain[i]))
        i -= 1

    # ---- 第二步：确实可能命中，此时才允许报「形态不支持」----
    for comp in [subject] + [c for c, _ in matched_ancestors]:
        if not (comp['pseudos'] | comp['neg_pseudos']) <= _TEXT_SUPPORTED_PSEUDOS:
            return None
    if not subject['pseudos'] <= chain[-1].pseudos:
        return False
    if subject['neg_pseudos'] & chain[-1].pseudos:
        return False
    for comp, node in matched_ancestors:
        if not comp['pseudos'] <= node.pseudos:
            return False
        if comp['neg_pseudos'] & node.pseudos:
            return False
    return True


# Bootstrap 5.3.0 里可能与 style.css 抢同一个元素 `color` 的规则。
#
# ⚠️ 这张表**不用来算颜色**，只用来做「style.css 必须赢」的下界检查。
# Bootstrap 是 CDN 引入的（templates/base.html），本仓库里没有它的源码，
# 拿它的值算最终色 = 拿一份手抄的常量冒充事实。所以做法是：一旦模型算出
# Bootstrap 的某条规则赢了 style.css，就**响亮失败**（「本测试算不了」），
# 而不是给出一个可能错的数字。
#
# 表里每条的特异度与 !important 形态取自 bootstrap@5.3.0 的
# dist/css/bootstrap.min.css，并已由 CDP 的 CSS.getMatchedStylesForNode
# 在本项目的历史页 <td> 上逐条核对过（实测命中的就是下面第 1、2 条）。
#
# ⚠️ 版本被钉死在下面这个常量上（test_bootstrap_version_matches_the_modelled_one）。
# 这张表是**按 5.3.0 的源码建的**：升级 Bootstrap 时那条断言会变红，
# 强制重新核对「新版本里还有哪些规则能命中 <td> 且设 color」。
# 不钉的话，升级引入一条新的高特异度规则时，模型不会知道，也不会响亮失败。
_MODELLED_BOOTSTRAP_VERSION = (5, 3, 0)
_BS_TEXT_UTILITIES = (
    'text-danger', 'text-success', 'text-warning', 'text-info',
    'text-primary', 'text-secondary', 'text-muted', 'text-body-secondary',
)


def _bootstrap_color_competitors(chain):
    """返回 [(说明, (是否 important, 特异度))]。"""
    el = chain[-1]
    anc_classes = set()
    for n in chain[:-1]:
        anc_classes |= n.classes
    out = []
    if el.pseudo_element is None and el.tag in ('td', 'th') and 'table' in anc_classes:
        # `.table > :not(caption) > * > *{color:var(--bs-table-color-state,...)}`
        # `:not(caption)` 记它参数的特异度(0,0,1)，两个 `*` 不计 -> (0,1,1)，不带 !important
        out.append(('.table > :not(caption) > * > *', (False, (0, 1, 1))))
    for u in _BS_TEXT_UTILITIES:
        if u in el.classes:
            # `.text-danger{color:rgba(var(--bs-danger-rgb),var(--bs-text-opacity))!important}`
            out.append((f'.{u}', (True, (0, 1, 0))))
    if 'form-text' in el.classes:
        out.append(('.form-text', (False, (0, 1, 0))))
    if 'badge' in el.classes:
        out.append(('.badge', (False, (0, 1, 0))))
    if el.pseudo_element == 'placeholder' and 'form-control' in el.classes:
        out.append(('.form-control::placeholder', (False, (0, 1, 1))))
    return out


def _winning_color_decl(css, chain, label):
    """算出链尾节点最终生效的 `color` 声明原值 + 胜出的选择器分支。

    比较键 = (是否 !important, 特异度三元组, 源码里的规则序号)。
    序号是必须的：`.text-muted` 与 `.text-danger` 特异度完全相同、都带
    !important，同一个元素同时挂着这两个类时，**后声明的那条赢**。
    """
    cands = []
    scanned = 0
    for idx, (sel, body, at_ctx) in enumerate(_rules_ctx(css)):
        raw = _decl_map(body).get('color')
        if raw is None:
            continue
        scanned += 1
        for branch in _selector_parts(sel):
            hit = _text_branch_applies(branch, chain)
            assert hit is not None, (
                f'[{label}] 选择器 `{branch}` 的形态本模型不支持 —— 本测试已失效。'
                '（不是通过：模型算不清的规则可能正是胜出的那条）'
            )
            if hit:
                if at_ctx:
                    raise AssertionError(
                        f'[{label}] 命中的规则 `{branch}` 在 at-rule {at_ctx} 里，'
                        '本模型只算顶层规则 —— 已失效'
                    )
                cands.append((
                    (bool(_IMPORTANT_RE.search(raw)), _btn_specificity(branch), idx),
                    branch, raw,
                ))
                break
    assert scanned > 100, f'只扫到 {scanned} 条声明 color 的规则 —— 扫描逻辑已失效'
    assert cands, (
        f'[{label}] 没有任何 style.css 的规则直接命中这个节点 —— '
        '颜色会走继承或 Bootstrap，本模型算不了，测试已失效'
    )
    key, branch, raw = max(cands)
    own_rank = (key[0], key[1])
    for bs_label, bs_rank in _bootstrap_color_competitors(chain):
        assert own_rank >= bs_rank, (
            f'[{label}] style.css 的胜出规则 `{branch}` 排名 {own_rank}，'
            f'输给 Bootstrap 的 `{bs_label}` {bs_rank} —— 最终颜色由 Bootstrap 决定，'
            '本仓库里没有它的源码，模型算不了，测试已失效'
        )
    return branch, raw


def _modal_backdrop(css):
    """详情弹窗的底色 —— 从 `.modal-content` 解析，不许硬编码调色板变量。"""
    rules = [
        (sel, body) for sel, body, at in _rules_ctx(css)
        if not at and '.modal-content' in _selector_parts(sel)
        and ('background' in _decl_map(body) or 'background-color' in _decl_map(body))
    ]
    assert len(rules) == 1, (
        f'期望顶层恰好 1 条声明了背景色的 `.modal-content` 规则，实际 {len(rules)} 条 —— 已失效'
    )
    decls = _decl_map(rules[0][1])
    value = _resolve_color(css, decls.get('background') or decls.get('background-color'))
    assert re.fullmatch(r'#[0-9a-f]{6}', value), f'.modal-content 底色解析成 {value!r} —— 已失效'
    return value


def _branch_background(css, branch):
    """某个选择器分支声明的背景色原值（要求顶层恰好一条规则声明它）。"""
    rules = [
        (sel, body) for sel, body, at in _rules_ctx(css)
        if not at and branch in _selector_parts(sel)
        and ('background' in _decl_map(body) or 'background-color' in _decl_map(body))
    ]
    assert len(rules) == 1, (
        f'期望顶层恰好 1 条声明了背景色的 `{branch}` 规则，实际 {len(rules)} 条 —— 已失效'
    )
    decls = _decl_map(rules[0][1])
    return decls.get('background') or decls.get('background-color')


def test_text_color_model_assumptions_still_hold():
    """层叠模型的三条前提：无组合符、无 @media 声明 color、style.css 排在最后。

    这条不是产品契约，是**模型的自检**。三条前提任何一条被打破，
    下面那些「算最终颜色」的断言就是在给一个错数字背书 ——
    「静默给出错误的信心」比没有断言更糟（这是 Task 10/11 反复付过学费的地方）。
    """
    css = _css()
    combinator_offenders = []
    media_offenders = []
    color_branches = 0
    for sel, body, at_ctx in _rules_ctx(css):
        if 'color' not in _decl_map(body):
            continue
        for branch in _selector_parts(sel):
            color_branches += 1
            if re.search(r'[>+~]', branch):
                combinator_offenders.append(branch)
            if at_ctx:
                media_offenders.append(f'{at_ctx} {branch}')
    assert color_branches > 100, (
        f'只扫到 {color_branches} 个声明 color 的选择器分支 —— 扫描逻辑已失效'
    )
    assert not combinator_offenders, (
        '发现带子/兄弟组合符的 color 规则，`_text_branch_applies` 只按后代关系拆分，'
        '会把它们算错：\n' + '\n'.join('  ' + o for o in combinator_offenders)
        + '\n要么改掉选择器，要么给模型补上组合符支持——别让它继续静默算错。'
    )
    assert not media_offenders, (
        '发现 @media 里声明 color 的规则，本模型只算顶层规则：\n'
        + '\n'.join('  ' + o for o in media_offenders)
    )
    # style.css 必须是最后加载的样式表——`.table td` 去掉 !important 之后
    # 靠的就是「同特异度、后来者赢」压住 Bootstrap 的 `.table > :not(caption) > * > *`。
    # 顺序契约由 test_no_stylesheet_can_load_after_style_css 独立守住，这里只做交叉引用。
    assert 'def test_no_stylesheet_can_load_after_style_css' in open(
        os.path.abspath(__file__), encoding='utf-8').read(), (
        '样式表顺序的断言不见了 —— 本模型的第三条前提没人守了'
    )


# --- 页面里真实存在的文字上下文 -------------------------------------------
#
# 每条 = (标签, 从祖先到目标元素的节点链, 背衬来源, 期望的颜色变量或 None)
# 链的形状照抄真实 markup（templates/*.html + 两个 JS 的模板字符串）。

# Bootstrap 5.3.0 在行 hover 时给单元格加的**内阴影**：
#   `.table > :not(caption) > * > * { box-shadow: inset 0 0 0 9999px var(--bs-table-bg-state,...) }`
# `background: transparent !important` 压不掉 box-shadow，所以这一层实际存在。
# 常量取自 CDP 实测：hover 时 `getComputedStyle(td).boxShadow` ===
# `rgba(0, 0, 0, 0.075) 0px 0px 0px 9999px inset`。
# 少算这一层的话，hover 行的背衬会算成 #1c1e23，而屏幕上是 #191b20 ——
# 本项目是浅字压深底，漏算会让模型**低估**对比度（保守），但换个配色就会变成
# 高估。宁可把它算进来。
_BS_TABLE_HOVER_INSET = 'rgba(0, 0, 0, 0.075)'


class _CellClassCollector(HTMLParser):
    """收集一段 markup 里所有 <td>/<th> 的 class 集合。"""

    def __init__(self):
        super().__init__()
        self.cells = []

    def handle_starttag(self, tag, attrs):
        if tag in ('td', 'th'):
            cls = dict(attrs).get('class', '')
            self.cells.append(frozenset(cls.split()))


def _history_error_cell():
    """从 **history.js 实际出货的那段模板字符串**里解析出加载失败单元格。

    ⚠️ 这个函数存在的唯一理由，是评审实测出来的一个 Critical 逃逸：
    上一版这里写死成 `_TextEl('td', {'text-center', 'text-danger'})`，于是
    **把 `history.js` 里那行的 `class="text-center text-danger"` 改成
    `class="text-center"`（CSS 一个字不动）→ 271 条全绿**，而 CDP 走真实失败
    路径实测单元格渲染 rgb(232,234,237) / 14.88:1 —— 逐位就是修复前的那个 bug。

    根因是「层叠模型推理的是一个假想元素，不是实际出货的 markup」。这与
    Task 10 补链时的教训同型：配对断言守住了「标签 ↔ 变量」，守不住
    「变量 ↔ 数据源」。这里把数据源那一端接上：类名从 markup 里消失，
    模型就会对着一个没有 .text-danger 的 <td> 算，直接给出
    --color-text-primary 而不是 --color-danger，断言立刻变红。
    """
    src = _strip_js_comments(_js('history.js'))
    body = _js_function_body(src, 'loadHistory')
    # 只认 catch 分支里那次 innerHTML 赋值 —— 正常分支不写 markup。
    m = re.search(r'\.innerHTML\s*=\s*(.*?);', body, re.S)
    assert m, 'loadHistory 里找不到 innerHTML 赋值 —— 加载失败行的 markup 变形了，本测试已失效'
    literal = m.group(1)
    strings = re.findall(r"'([^']*)'|\"([^\"]*)\"|`([^`]*)`", literal, re.S)
    markup = ''.join(a or b or c for a, b, c in strings)
    assert '<td' in markup, (
        f'从 loadHistory 的 innerHTML 里解析不出 <td>（拿到 {markup[:80]!r}）—— 本测试已失效'
    )
    p = _CellClassCollector()
    p.feed(markup)
    assert len(p.cells) == 1, (
        f'加载失败行里解析出 {len(p.cells)} 个单元格（期望 1）—— 本测试已失效'
    )
    return _TextEl('td', p.cells[0])


def _text_contexts(css):
    """页面上真实存在的文字上下文。

    ⚠️ 每条链都是 **CDP 实抓**的祖先链（`el.parentElement` 一路向上），
    不是照着模板猜的。这一点是承重的：第一版按记忆写链，漏了
    `tbody#historyTableBody` 这个 id 和 `form#downloadForm` 这一层 ——
    任何写成 `#historyTableBody td { color: ... !important }` 的新规则
    在浏览器里会赢，在模型里却根本不命中，等于给自己开了个盲区。
    改 markup 之后要重抓（脚本见 p2-task-12-report.md）。
    """
    panel = _effective_task_card_backdrop(css)          # `.card` 的底色，实测 #15171c
    modal = _modal_backdrop(css)
    row_hover = _flatten(
        _BS_TABLE_HOVER_INSET,
        _flatten(_resolve_color(css, _branch_background(css, '.table-hover tbody tr:hover')), panel),
    )
    control = _flatten(_resolve_color(css, _branch_background(css, '.form-control')), panel)
    toast = _flatten(_resolve_color(css, _branch_background(css, '.app-toast')),
                     _palette_var(css, '--color-bg-primary'))

    body = _TextEl('body')
    main = _TextEl('main', {'main-content'})

    # --- 历史页表格 ---
    hist_top = [body, main, _TextEl('div', {'container-fluid'}), _TextEl('div', {'row'}),
                _TextEl('div', {'col-12'}), _TextEl('div', {'card'}),
                _TextEl('div', {'card-body'}), _TextEl('div', {'table-responsive'}),
                _TextEl('table', {'table', 'table-hover'}),
                _TextEl('tbody', element_id='historyTableBody')]

    def cell_chain(cell, hover=False, tail=()):
        return hist_top + [_TextEl('tr', pseudos=({'hover'} if hover else ())), cell] + list(tail)

    # **从 history.js 实际出货的 markup 解析**，不写死 —— 见 _history_error_cell 的说明。
    err_cell = _history_error_cell()
    plain_cell = _TextEl('td')

    # --- 详情弹窗 ---
    modal_top = [body, main,
                 _TextEl('div', {'modal', 'fade', 'show'}, element_id='taskDetailModal'),
                 _TextEl('div', {'modal-dialog', 'modal-lg'}),
                 _TextEl('div', {'modal-content'}), _TextEl('div', {'modal-body'}),
                 _TextEl('div', {'detail-grid'}), _TextEl('div', {'detail-item'})]

    # --- 首页表单 ---
    form_top = [body, main, _TextEl('div', {'index-layout'}), _TextEl('div', {'index-right'}),
                _TextEl('div', {'card'}), _TextEl('div', {'card-body'}),
                _TextEl('form', element_id='downloadForm')]

    return [
        # (标签, 链, 背衬, 期望的调色板变量或 None)
        ('历史表「加载失败」单元格',
         cell_chain(err_cell), panel, '--color-danger'),
        ('历史表「加载失败」单元格（鼠标划过该行）',
         cell_chain(err_cell, hover=True), row_hover, '--color-danger'),
        ('历史表普通单元格',
         cell_chain(plain_cell), panel, '--color-text-primary'),
        ('历史表普通单元格（鼠标划过该行）',
         cell_chain(plain_cell, hover=True), row_hover, '--color-text-primary'),
        ('历史表单元格里的 <small>（四至坐标）',
         cell_chain(plain_cell, tail=[_TextEl('small')]), panel, '--color-text-secondary'),
        # ⚠️ 下面这条守的是**不变量**而不是现有 markup：表格里任何元素挂上
        # .text-danger 都必须变红。当前没有 `<small class="text-danger">` 的
        # 用法，但 `.table td small` 那条规则改前带 !important、特异度 (0,1,2)，
        # 会把它压成灰色 —— 与本任务修的 `.table td` 是同一个坑的另一个入口。
        ('历史表单元格里挂 .text-danger 的 <small>（不变量）',
         cell_chain(plain_cell, tail=[_TextEl('small', {'text-danger'})]), panel, '--color-danger'),
        ('首页表单分组标题',
         form_top + [_TextEl('div', {'form-group-label'})], panel, None),
        ('首页表单说明文字',
         form_top + [_TextEl('div', {'mb-3'}, element_id='demOptions'),
                     _TextEl('small', {'form-text', 'text-muted', 'd-block', 'mb-2'})], panel, None),
        ('活动任务空态提示（首屏静态 markup）',
         [body, main, _TextEl('div', {'index-layout'}), _TextEl('div', {'index-right'}),
          _TextEl('div', {'card', 'mt-3'}), _TextEl('div', {'card-body'}, element_id='activeTasks'),
          _TextEl('p', {'text-muted'})], panel, None),
        ('详情弹窗字段名',
         modal_top + [_TextEl('span', {'detail-k'})], modal, None),
        ('详情弹窗字段值',
         modal_top + [_TextEl('span', {'detail-v'}, element_id='detailId')], modal, None),
        ('首页输入框占位符',
         form_top + [_TextEl('div', {'mb-3'}),
                     _TextEl('input', {'form-control'}, element_id='taskName',
                             pseudo_element='placeholder')], control, None),
        # 配置页的占位符走的是**另一条**规则：`.config-section .form-control::placeholder`
        # (0,2,1)，与全局那条重复（Task 10 报告里记的「遗留」）。少了这一条上下文，
        # 只改全局那条、把配置页留在 2.82:1 的变异会逃逸 —— 实测逃过过一次。
        ('配置页输入框占位符',
         [body, main, _TextEl('div', {'container-fluid'}), _TextEl('div', {'row'}),
          _TextEl('div', {'col-12'}), _TextEl('form', element_id='configForm'),
          _TextEl('div', {'config-section'}), _TextEl('div', {'mb-3'}),
          _TextEl('input', {'form-control'}, element_id='proxy_url',
                  pseudo_element='placeholder')], control, None),
        # toast 的关闭按钮：`×` 是这个可点控件上唯一的可见标识。它压的不是面板底色，
        # 是 toast 自己的 --color-bg-tertiary。改前 2.82:1 —— 连 WCAG 1.4.11
        # 给图形元素的 3:1 都不到。
        ('Toast 关闭按钮',
         [body, _TextEl('div', {'app-toast-container'}), _TextEl('div', {'app-toast'}),
          _TextEl('button', {'app-toast__close'})], toast, None),
    ]


def test_every_text_context_meets_wcag_aa():
    """页面上每一处正文文字，**层叠算完之后**对它真正的背衬都要 >= 4.5:1。

    强度说明 —— 为什么不能写成「查某条规则用了哪个颜色变量」：
    A7 修的两个缺陷里，第一个（表格里的错误文字不是红的）**规则一直是对的**。
    `.text-danger { color: var(--color-danger) !important }` 从项目第一版就在，
    值也没变过；坏的是同一个元素上还有一条 (0,1,1)!important 的
    `.table td` 压着它。查规则的断言在改动前后都是绿的，看不见这个缺陷。
    所以这里把同一个元素上**所有**命中规则排一遍取胜出者。

    背衬也不许硬编码：面板底色从 `.card` 解析（`_effective_task_card_backdrop`），
    弹窗底色从 `.modal-content` 解析，行 hover 的底色是
    `.table-hover tbody tr:hover` 的 rgba 压到面板底色上的合成值，
    控件底色从 `.form-control` 解析。改调色板会让这些数字跟着动。

    上下文清单（14 条）的边界：覆盖三个页面上**由 style.css 上色的**全部
    正文类文字位置 —— 历史表格单元格（普通/错误 x hover/非 hover）、
    表格内的 <small>（含挂 .text-danger 的不变量那条）、首页分组标题、
    表单说明、空态提示、详情弹窗的键与值、首页与配置页各自的输入框占位符
    （两处走不同规则，只覆盖一处会漏）、toast 的关闭按钮。
    没进清单的三类各有归属：徽章文字 -> test_status_badge_text_is_readable_in_every_state
    （背衬是每个状态自己的半透明填充，不是面板底色）；按钮文字 ->
    test_button_ink_is_readable_in_every_state；JS 模板里的内联色 ->
    test_inline_colors_in_js_templates_meet_wcag_aa（它们不在 style.css 里，
    本模型扫不到）。三者合起来才叫「全覆盖」，单看本条不叫。
    """
    css = _css()
    contexts = _text_contexts(css)
    assert len(contexts) == 14, (
        f'上下文清单变成 {len(contexts)} 条（期望 14）—— 增删了要同步更新本断言，'
        '否则「全都覆盖了」是假象'
    )
    problems = []
    report = []
    for label, chain, backdrop, expect_var in contexts:
        branch, raw = _winning_color_decl(css, chain, label)
        literal = _resolve_color(css, raw)
        flat = _flatten(literal, backdrop)
        ratio = _contrast_ratio(flat, backdrop)
        report.append(f'{label}: `{branch}` -> {flat} on {backdrop} = {ratio:.2f}:1')
        if expect_var is not None:
            want = _palette_var(css, expect_var)
            if literal != want:
                problems.append(
                    f'{label}：层叠后生效的是 `{branch}` 的 {literal}，'
                    f'期望 {expect_var}({want})。'
                    '颜色够亮不代表语义对 —— 白字也够亮，但它说不出「这条失败了」')
        if ratio < WCAG_AA_TEXT_CONTRAST:
            problems.append(
                f'{label}：`{branch}` 算出 {flat} 压在 {backdrop} 上只有 {ratio:.2f}:1，'
                f'低于 WCAG AA 的 {WCAG_AA_TEXT_CONTRAST}')
    assert not problems, (
        '文字对比度 / 语义不达标：\n' + '\n'.join('  ' + p for p in problems)
        + '\n\n全部实测值：\n' + '\n'.join('  ' + r for r in report)
    )


def test_status_badge_text_is_readable_in_every_state():
    """`getStatusColor` 能返回的**每一个**颜色名，其徽章文字都要 >= 4.5:1。

    包含 `|| 'xxx'` 那个兜底档 —— 这条是本任务的验收标准之一。
    改前 history.js 的 getStatusColor 只映射三态，pending/running/paused
    全落到兜底的 'secondary'：徽章是同一块灰、文字是后端吐的英文字面量
    （CDP 实测徽章里写的就是 `pending` / `running` / `paused`）。
    补齐映射之后兜底档只在「后端出现了模型不认识的新状态」时才生效，
    但它仍然必须可读 —— 那正是最需要看清楚的时候。

    颜色名不是手抄的，从两个 JS 的 getStatusColor 源码解析（`_status_color_names`）：
    手抄的清单会在有人加状态时静默过期。

    背衬按真实渲染合成：徽章底是 `rgba(...)` 半透明，必须先压到面板底色
    （`.card`，实测 #15171c）上再算 —— 直接拿 rgba 的 RGB 分量算会得到一个
    与屏幕上完全无关的数字。
    """
    css = _css()
    names = _status_color_names('tasks.js') | _status_color_names('history.js')
    assert names == {'secondary', 'info', 'warning', 'success', 'danger', 'dark'}, (
        f'从两个 getStatusColor 解析出的颜色名是 {sorted(names)}，'
        "期望 {'danger','dark','info','secondary','success','warning'} —— "
        '六态 x 两个文件的映射变了，先确认是有意的再改本断言'
    )
    fallbacks = set()
    for js_name in ('tasks.js', 'history.js'):
        body = _js_function_body(_js(js_name), 'getStatusColor')
        fallbacks |= set(re.findall(r"\|\|\s*'([a-z]+)'", body))
    assert fallbacks <= names, f'兜底色 {sorted(fallbacks - names)} 不在被检查的名单里 —— 已失效'

    panel = _effective_task_card_backdrop(css)
    problems = []
    report = []
    for name in sorted(names):
        branch = f'.badge.bg-{name}'
        bg = _flatten(_resolve_color(css, _branch_background(css, branch)), panel)
        chain = [_TextEl('div', {'card'}), _TextEl('div', {'card-body'}),
                 _TextEl('span', {'badge', f'bg-{name}'})]
        win_branch, raw = _winning_color_decl(css, chain, f'徽章 {branch}')
        ink = _flatten(_resolve_color(css, raw), bg)
        ratio = _contrast_ratio(ink, bg)
        tag = '（兜底档）' if name in fallbacks else ''
        report.append(f'{branch}{tag}: `{win_branch}` -> {ink} on {bg} = {ratio:.2f}:1')
        if ratio < WCAG_AA_TEXT_CONTRAST:
            problems.append(
                f'{branch}{tag}：文字 {ink} 压在徽章底 {bg} 上只有 {ratio:.2f}:1，'
                f'低于 WCAG AA 的 {WCAG_AA_TEXT_CONTRAST}')
    assert not problems, (
        '状态徽章文字不可读：\n' + '\n'.join('  ' + p for p in problems)
        + '\n\n全部实测值：\n' + '\n'.join('  ' + r for r in report)
    )


def test_inline_style_colors_meet_wcag_aa_everywhere():
    """`style="..."` 里写死的文字色也要过 4.5:1 —— **JS 模板与 Jinja 模板都扫**。

    为什么单独一条：这些颜色**不在 style.css 里**，上面那个层叠模型扫不到它们。
    改前 history.js 的「暂无历史记录」「本地文件」和 tasks.js 的
    「暂无活动任务」都用 `var(--color-text-muted)`（3.09:1），走的是内联 style。
    内联样式的特异度高于任何选择器，写了就是最终值，不需要跑层叠。

    ⚠️ 扫描范围含 `templates/`，这是评审实测出来的逃逸（R9）修的：
    上一版只扫 `static/js/`，在任意 Jinja 模板里写一个
    `<span style="color: var(--color-text-muted)">` 就能拿到 3.09:1 而全绿。
    模板里当前 0 处内联 color，正是这条要守住的状态。

    做法是先切出 `style="..."` 属性再在里面找 `color:` 声明 —— 不是全文
    grep `color:`，那样会把 CSS 选择器名、JS 变量名、注释一起吃进来。

    命中数的边界：当前 10 处，全部在 static/js（history.js 7、tasks.js 3），
    templates 0 处。断言只要求「两个 JS 都被扫到、templates 目录被扫到、
    且总数 >= 8」—— 钉死具体数字会在无关 UI 改动时误红，钉 0 则负向遍历永真。
    """
    css = _css()
    panel = _effective_task_card_backdrop(css)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pat_style = re.compile(r'style\s*=\s*"([^"]*)"')
    pat_color = re.compile(r'(?<![-\w])color\s*:\s*([^;"]+)')
    hits, problems, scanned = [], [], []
    for sub, ext in (('static/js', '.js'), ('templates', '.html')):
        d = os.path.join(root, *sub.split('/'))
        assert os.path.isdir(d), f'{sub} 目录不存在 —— 本测试已失效'
        scanned.append(sub)
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(ext):
                continue
            with open(os.path.join(d, fn), encoding='utf-8') as f:
                src = re.sub(r'/\*.*?\*/', '', f.read(), flags=re.S)
            scanned.append(f'{sub}/{fn}')
            for sm in pat_style.finditer(src):
                line = src[:sm.start()].count('\n') + 1
                for cm in pat_color.finditer(sm.group(1)):
                    raw = cm.group(1).strip()
                    where = f'{sub}/{fn}:{line}'
                    var = re.fullmatch(r'var\(\s*(--[-\w]+)\s*\)', raw)
                    if var:
                        literal = _palette_var(css, var.group(1))
                    elif re.fullmatch(r'#[0-9a-fA-F]{6}', raw) or _RGBA_RE.fullmatch(raw):
                        literal = raw.lower()
                    else:
                        problems.append(
                            f'{where} 内联 color 写成 {raw!r}，本测试算不了它的对比度 —— '
                            '换成 var(--color-*) 或 #rrggbb，别让它静默逃过检查')
                        continue
                    flat = _flatten(literal, panel)
                    ratio = _contrast_ratio(flat, panel)
                    hits.append(f'{where} {raw} = {flat} -> {ratio:.2f}:1')
                    if ratio < WCAG_AA_TEXT_CONTRAST:
                        problems.append(
                            f'{where} 内联 color: {raw} = {flat}，对面板底 {panel} '
                            f'只有 {ratio:.2f}:1，低于 {WCAG_AA_TEXT_CONTRAST}')
    assert {'static/js/tasks.js', 'static/js/history.js', 'templates'} <= set(scanned), (
        f'扫描范围不完整（实际 {scanned}）—— 本测试已失效'
    )
    assert len(hits) >= 8, (
        f'只扫到 {len(hits)} 处内联 color（期望 >= 8）—— '
        '正则失效的话下面的负向断言就是永真\n' + '\n'.join('  ' + h for h in hits)
    )
    assert not problems, (
        '内联文字颜色不达标：\n' + '\n'.join('  ' + p for p in problems)
        + '\n\n全部命中：\n' + '\n'.join('  ' + h for h in hits)
    )


# --------------------------------------------------------------------------
# A7 / Task 12（评审第二轮）：状态 -> 语义色的**配对**，不只是集合
#
# 评审实测的两个逃逸（R4 / R10）：把 getStatusColor 的六个值整体轮换一位，
# 键集合齐全、颜色名集合也齐全，上一版断言全绿 —— 而失败的任务会显示成绿色。
# 这与 Task 10 补链时的教训完全同型：**守住了集合，没守住配对。**
#
# 这张表是产品决策（哪个状态该用哪种语义色），所以它就该被钉在测试里；
# 但钉的是**语义令牌名**，不是色号 —— 调色板改值时这条不动，
# 有人把「失败」画成绿色时它变红。
#
# `None` = 中性档：这两个状态**故意**没有语义色（等待中还没开始、已取消是
# 无褒贬的终态）。对它们的要求反过来：最终色**不许**等于四个语义色中的任何一个
# —— 「已取消」被画成青绿品牌色正是本轮补掉的缺陷之一。
# --------------------------------------------------------------------------

_STATUS_SEMANTIC_TOKEN = {
    'pending': None,
    'running': '--color-info',
    'paused': '--color-warning',
    'completed': '--color-success',
    'failed': '--color-danger',
    'cancelled': None,
}


def _semantic_palette_values(css):
    return {
        _palette_var(css, t)
        for t in ('--color-info', '--color-warning', '--color-success', '--color-danger')
    }


def _status_color_map(js_name):
    """`getStatusColor` 的 {状态: Bootstrap 颜色名}。"""
    body = _js_function_body(_js(js_name), 'getStatusColor')
    pairs = re.findall(r"'([a-z_]+)'\s*:\s*'([a-z]+)'", body)
    assert pairs, f'{js_name} 的 getStatusColor 解析不出映射 —— 本测试已失效'
    return dict(pairs)


def test_status_badge_color_matches_the_semantic_token():
    """每个状态的徽章文字色必须解析成**它自己那一档**语义色。

    强度说明：上一版只断言「颜色名集合 == 六个」。评审实测把六个值整体轮换一位
    （failed -> 'success'、completed -> 'danger' …）—— 集合纹丝不动，271 条全绿，
    而界面上失败的任务是绿的。这条把配对钉住：`colors['failed']` 指向的那条
    `.badge.bg-X` 规则，其 color 必须解析成 `--color-danger` 的字面值。

    钉的是语义令牌不是色号：调色板改值时本条不动。
    中性档（pending / cancelled）反向断言 —— 不许等于四个语义色中的任何一个。

    覆盖边界：2 个文件 × 6 个状态 = 12 组，先钉组数再逐组比对。
    """
    css = _css()
    semantic = _semantic_palette_values(css)
    checked, problems = [], []
    for js_name in ('tasks.js', 'history.js'):
        cmap = _status_color_map(js_name)
        assert set(cmap) == set(_STATUS_SEMANTIC_TOKEN), (
            f'{js_name} 的 getStatusColor 键集合是 {sorted(cmap)}，'
            f'期望 {sorted(_STATUS_SEMANTIC_TOKEN)} —— 先修 '
            'test_both_js_files_map_every_backend_status'
        )
        for status, token in _STATUS_SEMANTIC_TOKEN.items():
            name = cmap[status]
            chain = [_TextEl('div', {'card'}), _TextEl('div', {'card-body'}),
                     _TextEl('span', {'badge', f'bg-{name}'})]
            _branch, raw = _winning_color_decl(css, chain, f'{js_name} {status} 徽章')
            got = _resolve_color(css, raw)
            checked.append(f'{js_name} {status} -> bg-{name} -> {got}')
            if token is None:
                if got in semantic:
                    problems.append(
                        f'{js_name}: {status!r} 是中性档，却映射到 bg-{name}，'
                        f'解析出语义色 {got} —— 它会冒充「运行中/已完成/失败/已暂停」')
            else:
                want = _palette_var(css, token)
                if got != want:
                    problems.append(
                        f'{js_name}: {status!r} -> bg-{name} -> {got}，'
                        f'期望 {token}({want})')
    assert len(checked) == 12, f'只检查了 {len(checked)} 组（期望 12）—— 本测试已失效'
    assert not problems, (
        '状态与语义色的配对错了：\n' + '\n'.join('  ' + p for p in problems)
        + '\n\n全部映射：\n' + '\n'.join('  ' + c for c in checked)
    )


def test_task_card_status_bar_covers_every_status():
    """任务卡片左侧那条 4px 边条：六态**每一态**都要有自己的规则，且色对、够看得见。

    改前 `.task-card.status-cancelled::before` **根本不存在**，已取消的卡片落到
    `.task-card::before` 的兜底 `var(--color-accent)` —— 一条青绿品牌色边条，
    读起来像「一切正常」。CDP 实测改前 rgb(45, 212, 191)。
    这是评审找到的、与本任务修的缺陷完全同型的第二处漏网。

    三件事一起断言：
      1. 六态各有一条顶层规则（缺一条就会落到兜底色，而兜底色是品牌色）；
      2. 语义档的色值等于对应令牌，中性档不许等于任何语义色；
      3. 对面板底 >= 3:1 —— 它是图形元素不是文字，走 WCAG 1.4.11 的下限。
    """
    css = _css()
    panel = _effective_task_card_backdrop(css)
    semantic = _semantic_palette_values(css)
    fallback = _resolve_color(css, _branch_background(css, '.task-card::before'))
    problems, report = [], []
    for status, token in _STATUS_SEMANTIC_TOKEN.items():
        branch = f'.task-card.status-{status}::before'
        rules = [
            (sel, body) for sel, body, at in _rules_ctx(css)
            if not at and branch in _selector_parts(sel)
            and ('background' in _decl_map(body) or 'background-color' in _decl_map(body))
        ]
        if len(rules) != 1:
            problems.append(
                f'`{branch}` 有 {len(rules)} 条声明了背景色的顶层规则（应恰好 1 条）。'
                f'一条都没有 = 落到 `.task-card::before` 的兜底 {fallback}，'
                '那是品牌强调色，读起来像「一切正常」')
            continue
        decls = _decl_map(rules[0][1])
        got = _resolve_color(css, decls.get('background') or decls.get('background-color'))
        ratio = _contrast_ratio(got, panel)
        report.append(f'{branch} -> {got} on {panel} = {ratio:.2f}:1')
        if token is None:
            if got in semantic:
                problems.append(f'`{branch}` 用了语义色 {got}，但 {status!r} 是中性档')
            if got == fallback:
                problems.append(f'`{branch}` 与兜底色 {fallback} 相同 = 等于没写')
        else:
            want = _palette_var(css, token)
            if got != want:
                problems.append(f'`{branch}` 是 {got}，期望 {token}({want})')
        if ratio < 3.0:
            problems.append(
                f'`{branch}` 的 {got} 对面板底 {panel} 只有 {ratio:.2f}:1，'
                '低于 WCAG 1.4.11 给图形元素的 3:1')
    assert len(report) + len([p for p in problems if '条声明了背景色' in p]) == 6, (
        '没有恰好检查 6 个状态 —— 本测试已失效'
    )
    assert not problems, (
        '任务卡片状态边条有问题：\n' + '\n'.join('  ' + p for p in problems)
        + '\n\n全部实测：\n' + '\n'.join('  ' + r for r in report)
    )


def test_bootstrap_version_matches_the_modelled_one():
    """Bootstrap 的版本必须**恰好**是层叠模型建模时用的那一版。

    `_bootstrap_color_competitors` 那张表是照着 bootstrap@5.3.0 的源码建的：
    我扫了 5.3.0 里所有「能命中 <td>/<th> 且声明 color」的规则，完整集就是表里那两条
    （`.table > :not(caption) > * > *` 和 `.text-*` 工具类），并用 CDP 的
    `CSS.getMatchedStylesForNode` 在真实节点上核对过。

    升级 Bootstrap 时这条会变红 —— **那是设计意图**，不是误报：
    新版本可能引入一条新的高特异度 color 规则，而模型对它一无所知，
    既不会算错也不会响亮失败，只会静默给出一个可能过期的结论。
    变红时的正确做法是重新扫一遍新版本的 <td> 颜色规则、更新那张表，再改这个常量。

    这条与 `test_bootstrap_build_is_new_enough_to_have_dark_theme` 是不同的约束：
    那条守下界（>= 5.3 才有暗色主题），这条守精确匹配（模型的前提）。
    """
    seen = []
    for tpl, markup in _all_templates():
        for _tag, url in _bootstrap_asset_urls(markup):
            for rx in _BOOTSTRAP_VERSION_RES:
                m = rx.search(url)
                if m:
                    seen.append((tpl, url, tuple(int(g or 0) for g in m.groups())))
                    break
    assert seen, 'templates 里找不到任何带版号的 Bootstrap 资源 —— 本测试已失效'
    wrong = [
        f'{tpl}: {url} -> {".".join(map(str, ver))}'
        for tpl, url, ver in seen if ver != _MODELLED_BOOTSTRAP_VERSION
    ]
    assert not wrong, (
        '引用的 Bootstrap 版本与层叠模型建模的版本不符（模型按 '
        f'{".".join(map(str, _MODELLED_BOOTSTRAP_VERSION))} 建的）：\n'
        + '\n'.join('  ' + w for w in wrong)
        + '\n升级是正当的，但要先重新核对 _bootstrap_color_competitors 那张表'
        '（扫新版本里所有能命中 <td> 且设 color 的规则），再改 _MODELLED_BOOTSTRAP_VERSION。'
    )


# ==========================================================================
# A8 / Task 13：动画降噪 + prefers-reduced-motion
# ==========================================================================
# 为什么这一节要建第三个层叠模型（前两个是按钮外观、文字颜色）：
#
# 本任务改的四件事，**每一件都能被无声撤回**，如果只写「文件里有没有这行」
# 的形态断言：
#   1. 全局 `*` 过渡删了 —— 有人再写一条 `body * { transition: ... }`，
#      效果原样回来，查 `*` 规则的断言全绿。
#   2. `.card/.task-card` 的入场动画删了 —— 有人在别处写
#      `.task-card { animation: fadeInUp .5s }`，同样全绿。
#   3. 进度条时长 0.6s -> 0.2s —— 有人补一条
#      `.progress .progress-bar { transition: width 1s }`(0,2,0) 压回去。
#   4. `@media (prefers-reduced-motion: reduce)` 块 —— **这条最危险**：
#      它的选择器是 `*, *::before, *::after`，特异度 (0,0,0)，是 CSS 里最弱的
#      形态。把里面的 `!important` 去掉，整块立刻变成死代码（`.btn` 的 (0,1,0)
#      就能压过它），而「文件里有没有这个 @media 块」的断言一个字都不会红。
#      Task 11 的教训原文：整套修复能被原样撤回而 261 条测试全绿。
#
# 所以下面全部走「算最终生效值」：把命中同一个元素的所有规则按
# (!important, 特异度, 源码序, 声明序号) 排一遍取胜出者 —— 与 _btn_computed /
# _winning_color_decl 同一套比较键。
#
# 建模环境用 `reduced` 参数区分：False = 普通用户，True = 打开了系统
# 「减少动画」偏好的用户。同一套规则在两个环境下都要给出正确答案。

_MOTION_LONGHANDS = (
    'animation-name', 'animation-duration', 'animation-iteration-count',
    'transition-property', 'transition-duration',
)

# CSS 里所有合法的 <time>：数字 + s/ms（大小写不敏感）。
_TIME_RE = re.compile(r'^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?(s|ms)$', re.I)

# animation 简写里可以出现、但**不是** name 的关键字。
# 漏掉任何一个都会让它被当成动画名（例如把 `ease-out` 当名字），
# 于是「.card 没有入场动画」这条断言会因为读错字段而失效。
_ANIMATION_NON_NAME = frozenset({
    'normal', 'reverse', 'alternate', 'alternate-reverse',        # direction
    'none', 'forwards', 'backwards', 'both',                      # fill-mode
    'running', 'paused',                                          # play-state
    'infinite',                                                   # iteration-count
    'linear', 'ease', 'ease-in', 'ease-out', 'ease-in-out',
    'step-start', 'step-end',                                     # timing-function
    'initial', 'inherit', 'unset', 'revert',                      # 全局关键字
})

# transition-property 里出现即「不是属性名」的关键字。
_TRANSITION_NON_PROPERTY = frozenset({
    'linear', 'ease', 'ease-in', 'ease-out', 'ease-in-out',
    'step-start', 'step-end', 'initial', 'inherit', 'unset', 'revert',
})


def _time_to_seconds(tok):
    """`0.2s` / `150ms` / `0.01ms` -> 秒（float）。不是时间返回 None。"""
    m = _TIME_RE.match(tok.strip())
    if not m:
        return None
    return float(tok.strip()[:-len(m.group(1))]) / (1000.0 if m.group(1).lower() == 'ms' else 1.0)


def _split_commas_outside_parens(value):
    """按顶层逗号拆。`cubic-bezier(0.4, 0, 0.2, 1)` 里的逗号不算。"""
    out, depth, cur = [], 0, ''
    for ch in value:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            out.append(cur)
            cur = ''
        else:
            cur += ch
    out.append(cur)
    return [p.strip() for p in out if p.strip()]


def _tokens_outside_parens(part):
    """按空白拆 token，括号内当作一个整体（`cubic-bezier(...)` 不被拆散）。"""
    out, depth, cur = [], 0, ''
    for ch in part:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch.isspace() and depth == 0:
            if cur:
                out.append(cur)
            cur = ''
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _expand_motion_decls(body):
    """规则体 -> [(长写属性名, 值, important, 声明序号)]，简写已展开。

    简写**必须**展开，理由与 _BTN_SHORTHAND_EXPANSIONS 完全相同：
    `animation: fadeInUp .5s` 和 `animation-name: fadeInUp` 在浏览器里等价，
    只认其中一种写法的断言，换另一种写法就能绕过去。

    简写里 <time> 的顺序按规范：**第一个是 duration，第二个是 delay**。
    只要认「有个 s 结尾的 token」而不管顺序，`animation: pulse 0s 2s` 这种
    会被读成 2s，正好把「时长压到 0」的检查骗过去。
    """
    out = []
    for idx, chunk in enumerate(body.split(';')):
        if ':' not in chunk:
            continue
        name, _, value = chunk.partition(':')
        name = name.strip().lower()
        imp = bool(_IMPORTANT_RE.search(value))
        value = _IMPORTANT_RE.sub('', value).strip()
        if name in _MOTION_LONGHANDS:
            out.append((name, value, imp, idx))
        elif name == 'animation':
            # 只处理单条动画（站内没有逗号分隔的多动画写法）；有了再扩。
            for part in _split_commas_outside_parens(value):
                toks = _tokens_outside_parens(part)
                times = [t for t in toks if _TIME_RE.match(t)]
                iters = [t for t in toks
                         if t.lower() == 'infinite' or re.fullmatch(r'\d+\.?\d*', t)]
                names = [t for t in toks
                         if not _TIME_RE.match(t)
                         and t.lower() not in _ANIMATION_NON_NAME
                         and '(' not in t
                         and not re.fullmatch(r'\d+\.?\d*', t)]
                out.append(('animation-name', names[0] if names else 'none', imp, idx))
                out.append(('animation-duration', times[0] if times else '0s', imp, idx))
                out.append(('animation-iteration-count',
                            iters[0] if iters else '1', imp, idx))
        elif name == 'transition':
            props, durs = [], []
            for part in _split_commas_outside_parens(value):
                toks = _tokens_outside_parens(part)
                times = [t for t in toks if _TIME_RE.match(t)]
                names = [t for t in toks
                         if not _TIME_RE.match(t)
                         and t.lower() not in _TRANSITION_NON_PROPERTY
                         and '(' not in t]
                props.append(names[0] if names else 'all')
                durs.append(times[0] if times else '0s')
            out.append(('transition-property', ', '.join(props), imp, idx))
            out.append(('transition-duration', ', '.join(durs), imp, idx))
    return out


def _motion_media_verdict(at_rule, reduced):
    """条件组 at-rule 在给定建模环境下成立吗？True / False / None(模型不支持)。

    与 _btn_media_applies 同一个口径，只是这里把 `reduced` 当参数：
    动画模型必须能算**两种**环境，按钮外观模型只算默认环境。
    宽度类断点一律返回 None（模型不支持）—— 只要有人往里塞动画声明就响亮失败。
    """
    m = _PREFERS_REDUCED_MOTION_RE.match(re.sub(r'\s+', ' ', at_rule).strip())
    if not m:
        return None
    want_reduce = m.group(1).lower() == 'reduce'
    return want_reduce == bool(reduced)


def _motion_computed(css, chain, reduced=False):
    """模拟层叠，返回链尾节点最终生效的动画/过渡。

    返回 dict：
      animation_name / animation_duration(秒) / animation_iterations(str)
      transitions: {属性名: 时长秒}，'all' 原样保留为键
    读不懂的写法一律抛 AssertionError（响亮失败），绝不猜。
    """
    best, unsupported = {}, []
    for order, (sel, body, at_ctx) in enumerate(_rules_ctx(css)):
        if any(a.split()[0] in _BTN_NON_SELECTOR_AT_RULES for a in at_ctx):
            continue                       # @keyframes 里的 `0%` 不是选择器
        decls = _expand_motion_decls(body)
        for branch in _selector_parts(sel):
            applies = _text_branch_applies(branch, chain)
            if applies is None:
                if decls:
                    unsupported.append(f'{sel}   （分支 {branch!r}）')
                continue
            if not applies:
                continue
            if at_ctx:
                verdicts = [_motion_media_verdict(a, reduced) for a in at_ctx]
                if any(v is None for v in verdicts):
                    if decls:
                        unsupported.append(f'{" ".join(at_ctx)} 里的 `{sel}`（条件求不了值）')
                    continue
                if not all(verdicts):
                    continue
            spec = _btn_specificity(branch)
            for name, val, imp, decl_idx in decls:
                key = (imp, spec, order, decl_idx)
                if name not in best or key > best[name][0]:
                    best[name] = (key, val)
    assert not unsupported, (
        f'动画层叠模型处理不了这些写法（节点 {chain[-1]!r}），测试已失效（不是通过）：\n'
        + '\n'.join('  ' + u for u in sorted(set(unsupported)))
        + '\n新写法要么扩展 _text_branch_applies / _motion_media_verdict，要么换等价写法'
    )

    def val_of(name, default):
        return best[name][1] if name in best else default

    dur_raw = val_of('animation-duration', '0s')
    anim_dur = _time_to_seconds(_split_commas_outside_parens(dur_raw)[0])
    assert anim_dur is not None, f'读不懂的 animation-duration: {dur_raw!r}'

    tprops = [p.strip().lower() for p in
              _split_commas_outside_parens(val_of('transition-property', 'all'))]
    tdurs_raw = _split_commas_outside_parens(val_of('transition-duration', '0s'))
    tdurs = []
    for d in tdurs_raw:
        s = _time_to_seconds(d)
        assert s is not None, f'读不懂的 transition-duration: {d!r}'
        tdurs.append(s)
    transitions = {}
    if 'none' not in tprops:
        for i, p in enumerate(tprops):
            # 规范：属性表比时长表长时，时长表循环取用
            d = tdurs[i % len(tdurs)] if tdurs else 0.0
            # 时长 0 = 不过渡。CSS 的初始值就是 `transition-property: all;
            # transition-duration: 0s`，不滤掉的话**每个**元素都会带着
            # {'all': 0.0}，「裸元素身上没有过渡」这条断言永远红。
            if d > 0:
                transitions[p] = d
    return {
        'animation_name': val_of('animation-name', 'none').strip(),
        'animation_duration': anim_dur,
        'animation_iterations': val_of('animation-iteration-count', '1').strip().lower(),
        'transitions': transitions,
    }


def _motion_rule_index(css):
    """全站声明了动画/过渡的规则清单：[(选择器分支, 是否在 reduce 块里)]。

    扫描范围 = style.css 全文（含 @media 内部），@keyframes 里的 `0%`/`from`
    这类非选择器除外。给下面几条遍历型断言当「完整性锚点」用。
    """
    out = []
    for sel, body, at_ctx in _rules_ctx(css):
        if any(a.split()[0] in _BTN_NON_SELECTOR_AT_RULES for a in at_ctx):
            continue
        if not _expand_motion_decls(body):
            continue
        in_reduce = any(
            _PREFERS_REDUCED_MOTION_RE.match(re.sub(r'\s+', ' ', a).strip())
            and 'reduce' in a.lower()
            for a in at_ctx
        )
        for branch in _selector_parts(sel):
            out.append((branch, in_reduce))
    return out


# 全站声明动画/过渡的选择器分支总数（含 reduce 块里那 3 个分支）。
#
# ⚠️ 这个数字的用途是**完整性锚点**，不是洁癖：Task 11 的 `== 6` 曾经制造
# 「全站都覆盖到了」的假象，实际漏了 base.html 里的第 7 颗按钮。下面几条
# 遍历型断言都建立在「我真的扫到了每一条」之上，数字对不上先查是不是漏扫。
# 增删动画规则时，改这个数字**并同时确认**新规则被下面的断言覆盖到了。
_MOTION_BRANCH_COUNT = 28


def test_motion_rule_index_is_complete():
    """先钉住扫描范围本身：动画/过渡声明分布在 28 个选择器分支上。

    扫描范围：static/css/style.css 全文，包含 @media 内部的规则，
    排除 @keyframes/@font-face/@page（里面的 `0%`、`from` 不是选择器）。
    """
    idx = _motion_rule_index(_css())
    assert len(idx) == _MOTION_BRANCH_COUNT, (
        f'全站声明动画/过渡的选择器分支有 {len(idx)} 个，锚点是 {_MOTION_BRANCH_COUNT}：\n'
        + '\n'.join(f'  {b}{"   [reduce 块内]" if r else ""}' for b, r in idx)
        + '\n改动画规则要同步这个锚点，并确认新规则被本节其余断言覆盖'
    )
    assert sum(1 for _b, r in idx if r) == 3, (
        'prefers-reduced-motion: reduce 块里应当正好有 3 个选择器分支'
        '（`*` / `*::before` / `*::after`）'
    )


def test_no_blanket_motion_reaches_an_unstyled_element():
    """一个「什么样式都没有」的元素，最终生效的过渡/动画必须是空的。

    这条守的是「全局 `*` 过渡已删除且不会回潮」，但它**不查 `*` 规则**——
    查规则的写法只堵一种回潮姿势，`body *`、`div`、`:where(*)`、
    `html * { transition: ... }` 每一种都能绕过去，效果却一模一样。
    这里改为算「一个裸 <td> 最终生效的 transition/animation 是什么」：
    只要有任何一条规则把过渡挂到了普通节点上，不管它怎么写，这里都变红。

    为什么这件事值得守：首页 Leaflet 动态生成的节点数以千计，它们一条过渡
    也用不上，`*` 却让样式引擎给每一个都挂上过渡簿记。
    """
    css = _css()
    # 链必须带上 html/body 祖先：变异实验 M5 实测，只放一个孤立节点时
    # `body * { transition: color .3s }` 这种「换个写法重新全局化」的姿势
    # 会因为祖先侧对不上而被判不命中，本条断言当场变成假绿。
    for node in (_TextEl(tag='td'), _TextEl(tag='span'), _TextEl(tag='div')):
        chain = [_TextEl(tag='html'), _TextEl(tag='body'), _TextEl(tag='div'), node]
        got = _motion_computed(css, chain)
        assert not got['transitions'], (
            f'裸 <{node.tag}> 身上还有过渡 {got["transitions"]} —— 全局过渡回潮了。'
            '交互反馈请写在具体的交互元素上（.btn / .form-control / .nav-link …）'
        )
        assert got['animation_name'] == 'none', (
            f'裸 <{node.tag}> 身上挂了动画 {got["animation_name"]!r}'
        )


def test_cards_have_no_entrance_animation_but_running_bar_still_pulses():
    """两件事一起钉：入场动画没了，**而状态动画还在**。

    只断言「.task-card 没有 animation」是不够的 —— 把整节动画全删光也能通过，
    而那样会连「运行中的任务左边条在呼吸」这个**传达状态**的信号一起丢掉。
    所以第二半是正面断言：.task-card.status-running::before 必须仍然跑 pulse、
    仍然是无限循环。

    算的是层叠胜出值，不是「文件里有没有 fadeInUp 这个词」：把
    `.task-card { animation: fadeInUp .5s }` 换个地方重写一遍照样会红。
    """
    css = _css()
    for classes in (('card',), ('task-card',), ('task-card', 'status-running'),
                    ('task-card', 'status-failed')):
        got = _motion_computed(css, [
            _TextEl(tag='body'),
            _TextEl(tag='div', element_id='activeTasks'),
            _TextEl(tag='div', classes=classes)])
        assert got['animation_name'] == 'none', (
            f'.{".".join(classes)} 又挂上了入场动画 {got["animation_name"]!r}'
            f'（{got["animation_duration"]}s）。任务列表每次成员变化都整体重建 '
            'innerHTML，入场动画会被集体重放：CDP 实测 5 张卡的场景下，'
            '「新任务到达」和「任一任务完成」各触发 fadeInUp × 5'
        )

    pulse = _motion_computed(css, [
        _TextEl(tag='body'),
        _TextEl(tag='div', element_id='activeTasks'),
        _TextEl(tag='div', classes=('task-card', 'status-running'), pseudo_element='before')])
    assert pulse['animation_name'] == 'pulse', (
        '运行中任务的左边条不再跑 pulse —— 这是「任务还活着」的唯一视觉信号，'
        f'属于传达状态的动画，不在降噪范围内。实际是 {pulse["animation_name"]!r}'
    )
    assert pulse['animation_iterations'] == 'infinite', (
        f'pulse 不再无限循环（{pulse["animation_iterations"]}），呼吸灯会停在某一帧'
    )
    assert pulse['animation_duration'] == pytest.approx(2.0, abs=0.01), (
        f'pulse 时长变成 {pulse["animation_duration"]}s'
    )


# 进度条 width 过渡的时长上界（秒）。
#
# 定这个数的依据是 CDP 实测，不是手感：
#   services/task_manager.py 的 progress_callback **每下载一块瓦片**就
#   `socketio.emit('task_progress', ...)` 一次，回调体内没有任何攒批或限频，
#   并发下载下 10~20 次/秒。tasks.js 走增量路径只改 bar.style.width，
#   于是这条过渡被反复重启，永远从上一帧位置重新起步。
#   20Hz 驱动 40 次（真值走到 80%）实测最大滞后：
#       0.6s cubic-bezier  57.0pp   ← 改前，用户看到 23% 时真实进度是 80%
#       0.2s cubic-bezier   7.7pp
#       0.2s linear         6.7pp   ← 改后
#       0.12s linear        3.5pp
#       none                0
#   决定性变量是时长，不是缓动曲线（0.2s 下 linear 与 cubic 只差 1pp，
#   10Hz 时 cubic 反而更好），所以这里只钉时长、**不钉 timing-function**。
_PROGRESS_WIDTH_MAX_SECONDS = 0.25


def test_progress_bar_transition_keeps_up_with_the_push_rate():
    """进度条的 width 过渡时长必须撑得住后端的推送频率。

    算的是层叠胜出值：补一条 `.progress .progress-bar { transition: width 1s }`
    (0,2,0) 把它压回去，这里照样变红。
    """
    # 真实 markup（tasks.js createTaskCard）：
    #   .task-card > .progress > .progress-bar
    # 祖先必须建模到位，否则 `.progress .progress-bar { transition: width 1s }`
    # 这条压回去的规则算不进来 —— 变异 M8 实测过，当时本断言是假绿。
    got = _motion_computed(_css(), [
        _TextEl(tag='div', classes=('task-card',)),
        _TextEl(tag='div', classes=('progress',)),
        _TextEl(tag='div', classes=('progress-bar',)),
    ])
    width_dur = got['transitions'].get('width', got['transitions'].get('all'))
    assert width_dur is not None or not got['transitions'], (
        f'.progress-bar 的过渡里既没有 width 也没有 all：{got["transitions"]}'
    )
    if width_dur is not None:
        assert width_dur <= _PROGRESS_WIDTH_MAX_SECONDS, (
            f'.progress-bar 的 width 过渡是 {width_dur}s，上界 '
            f'{_PROGRESS_WIDTH_MAX_SECONDS}s。后端每块瓦片推一次进度'
            '（task_manager.py 的 progress_callback，无攒批），过渡时长超过推送'
            '间隔就永远追不上真值 —— 0.6s 时实测最大滞后 57 个百分点'
        )


# reduce 块把时长压到 0.01ms。判定阈值给到 1ms：
# 目的是区分「压住了」和「没压住」（没压住的最小值是 0.12s = 120ms），
# 不是去校验那个字面量。留出量级余地，换个 0.5ms/1ms 写法不会误红。
_REDUCED_MOTION_MAX_SECONDS = 0.001

# 「减少动画」压不到、且**故意不去压**的豁免清单。
#
# 每条都要写清两件事：为什么压不到、为什么可以不压。清单之外一律必须被压住。
#
#   ::-webkit-scrollbar-thumb（`*::-webkit-scrollbar-thumb` 与
#   `.index-right::-webkit-scrollbar-thumb` 两条，各有一个 0.15s 的
#   background-color 过渡）
#     · 为什么压不到：reduce 块的选择器是 `*, *::before, *::after`，
#       覆盖不到这个非标准伪元素。
#     · 为什么不另写一条去压：CSS 的逗号选择器组里只要有一个浏览器不认识的
#       选择器，**整组作废**。把 `*::-webkit-scrollbar-thumb` 并进那个组，
#       Firefox 会把整块减少动画支持一起丢掉 —— 用一个真缺陷换一个假缺陷。
#       单独再写一条则要多花一个 !important（预算见
#       test_important_count_under_control）。
#     · 为什么可以不压：这是滚动条滑块 hover 时的 150ms **颜色**淡入，
#       没有位移、缩放、旋转，也不循环。WCAG 2.1 SC 2.3.3 管的是
#       「交互引发的动画」造成的前庭不适，颜色渐变不在其列。
_REDUCED_MOTION_EXEMPT_PSEUDOS = frozenset({'-webkit-scrollbar-thumb'})
# 注：`*::-webkit-scrollbar-thumb` 经 _TextEl.__repr__ 规范化后是 `::-webkit-scrollbar-thumb`
# （通配符不进 repr），这里存的是规范化后的形态。
_REDUCED_MOTION_EXEMPT = {'::-webkit-scrollbar-thumb', '.index-right::-webkit-scrollbar-thumb'}


def _motion_contexts_from_stylesheet(css):
    """把每一条动画/过渡规则的选择器**反向变成一个元素上下文**。

    为什么要反向生成而不是手写一张上下文表：手写的表会过期。Task 11 的
    `== 6` 就是手写清单漏了第 7 颗按钮，制造出「全站都覆盖了」的假象。
    这里让样式表自己说出「有哪些元素身上有动画」，新增一条动画规则的人
    不需要记得来补测试 —— 它自动进入下面那条断言的覆盖范围。

    生成的是**整条祖先链**，不是只取最后一个复合选择器。
    这一点是实测踩出来的：第一版只取末端，于是链长恒为 1，
    `_text_branch_applies` 对任何带后代组合符的规则都直接判不命中 ——
    变异实验 M8（`.progress .progress-bar { transition: width 1s }`）
    因此没能让「进度条时长上界」那条变红，M5（`body * { transition: color .3s }`）
    也没能让「裸元素身上没有过渡」那条变红。两条断言当时都是**假绿**。
    生成全链之后，每条规则至少保证能命中它自己生成的那个上下文。

    `*` 这种通配分支跳过（它不描述某个具体元素，reduce 块自己就是这个形态）。
    """
    ctxs = {}
    for branch, in_reduce in _motion_rule_index(css):
        if in_reduce:
            continue
        chain = []
        for part in branch.split():
            comp = _parse_compound(part)
            assert comp is not None, f'动画规则的选择器读不懂，模型已失效：{branch!r}'
            chain.append(_TextEl(
                tag=comp['tag'], classes=comp['classes'],
                element_id=(comp['ids'][0] if comp['ids'] else None),
                pseudo_element=comp['pseudo_element']))
        tail = chain[-1]
        if (not tail.classes and not tail.tag and not tail.element_id
                and not tail.pseudo_element):
            continue                      # 裸 `*`（`*::伪元素` 不算，它描述一个具体的盒子）
        ctxs.setdefault(' '.join(repr(n) for n in chain), chain)
    return [ctxs[k] for k in sorted(ctxs)]


def test_reduced_motion_actually_stops_every_animated_element():
    """开了系统「减少动画」偏好后，**每一个**带动效的元素都必须真的停下来。

    扫描范围：style.css 里全部声明了 transition/animation 的规则（含 @media
    内部），逐条把选择器反解成元素上下文，再在 `reduced=True` 环境下重算层叠。

    这条是本次改动里最容易被无声撤回的一环，所以它算最终生效值：
    `@media (prefers-reduced-motion: reduce)` 块的选择器是 `*, *::before,
    *::after`，特异度 (0,0,0)。**把里面的 !important 去掉，整块立刻变成死代码**
    （`.btn` 的 (0,1,0) 就压过它），而「文件里有没有这个 @media 块」的形态断言
    一个字都不会红。同理：把媒体条件写成 no-preference、把
    animation-duration 写成别的属性名、或者别处新增一条带 !important 的
    动画规则，这里都会变红。

    ⚠️ 无障碍口径：前庭功能障碍用户会因为界面动效产生真实的生理不适
    （WCAG 2.1 SC 2.3.3）。这不是观感偏好，所以断言是「全部」而不是「主要的几个」。
    """
    css = _css()
    ctxs = _motion_contexts_from_stylesheet(css)
    exempt = {repr(c[-1]) for c in ctxs
              if c[-1].pseudo_element in _REDUCED_MOTION_EXEMPT_PSEUDOS}
    assert exempt == _REDUCED_MOTION_EXEMPT, (
        f'减少动画的豁免清单与实际不符：实际 {sorted(exempt)}，清单 '
        f'{sorted(_REDUCED_MOTION_EXEMPT)}。新增豁免必须**逐条写理由**，'
        '清单里有而实际没有的（规则删了/改名了）也要清掉，否则清单会变成一张空头支票'
    )
    assert len(ctxs) == 25, (
        f'反解出 {len(ctxs)} 个带动效的元素上下文，锚点是 25：\n'
        + '\n'.join('  ' + ' '.join(repr(n) for n in c) for c in ctxs)
        + '\n数字对不上说明扫描范围变了，先确认不是漏扫'
    )
    problems = []
    for chain in ctxs:
        node = chain[-1]
        if repr(node) in _REDUCED_MOTION_EXEMPT:
            continue
        normal = _motion_computed(css, chain, reduced=False)
        reduced = _motion_computed(css, chain, reduced=True)
        # 只检查在普通环境下**确实会动**的元素：没动效的元素本来就不需要被压。
        if normal['animation_name'] != 'none' and normal['animation_duration'] > 0:
            if reduced['animation_duration'] > _REDUCED_MOTION_MAX_SECONDS:
                problems.append(
                    f'{node!r}: 动画 {normal["animation_name"]} 在 reduce 下仍有 '
                    f'{reduced["animation_duration"]}s')
            if reduced['animation_iterations'] == 'infinite':
                problems.append(f'{node!r}: 动画在 reduce 下仍是无限循环')
        for prop, dur in normal['transitions'].items():
            got = reduced['transitions'].get(prop, 0.0)
            if got > _REDUCED_MOTION_MAX_SECONDS:
                problems.append(
                    f'{node!r}: 过渡 {prop} 普通环境 {dur}s，reduce 下仍有 {got}s')
    assert not problems, (
        'prefers-reduced-motion: reduce 没有压住下列动效 —— '
        '八成是那一块的 !important 被去掉了（它的选择器特异度是 (0,0,0)，'
        '不带 !important 谁都压不住），或者媒体条件被改写了：\n'
        + '\n'.join('  ' + p for p in problems)
    )


def test_reduced_motion_block_only_touches_motion():
    """reduce 块里只许出现动画相关属性 —— 这是另外三个层叠模型的前提。

    按钮外观模型 / 文字颜色模型 / 高度模型都只建模**默认环境**
    （见 _btn_media_applies）。它们之所以可以直接跳过 reduce 块，
    唯一依据就是那一块不碰颜色、边框、尺寸。前提一旦被打破，
    那三个模型会在无人察觉的情况下漏算一整个用户群看到的界面。

    所以这条不是洁癖：它是把「模型的适用前提」变成一条会响的断言。
    真要在 reduce 下改外观，先给那三个模型加上环境参数。
    """
    offenders = []
    for sel, body, at_ctx in _rules_ctx(_css()):
        if not any(_PREFERS_REDUCED_MOTION_RE.match(re.sub(r'\s+', ' ', a).strip())
                   and 'reduce' in a.lower() for a in at_ctx):
            continue
        for chunk in body.split(';'):
            if ':' not in chunk:
                continue
            name = chunk.partition(':')[0].strip().lower()
            if name and not (name.startswith('animation') or name.startswith('transition')
                             or name == 'scroll-behavior'):
                offenders.append(f'{sel} -> {name}')
    assert not offenders, (
        'prefers-reduced-motion: reduce 块里出现了非动画属性：\n'
        + '\n'.join('  ' + o for o in offenders)
        + '\n按钮/文字/高度三个层叠模型都假设这一块不影响外观（见 _btn_media_applies），'
        '要在 reduce 下改外观，先给那三个模型加环境参数'
    )
