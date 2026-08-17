"""style.css 结构契约测试。

这些是**文本级**断言：它们守住 CSS 源码的形态（哪条规则声明了什么字号、
有没有人用 !important 重新覆盖），**守不住**「渲染出来好不好看」——后者
由 docs/assets/images/phase2-baseline/ 的截图 + 计算值对拍覆盖。

为什么需要这些断言：style.css 曾经有一整块「统一字体大小系统」，用
!important 重新声明前面已定义过的选择器（`.form-label` 先声明成 .9rem、
后面那一块又把它改成 .875rem!important）。后果是改前面的规则不生效。本文件的核心
断言就是防止这种自我覆盖的形态复活。
"""

import collections
import os
import re
import subprocess
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
    # 剥掉 at-**语句**（以 `;` 收尾、不带花括号的 at-rule：@import / @charset /
    # @namespace）。不剥的后果是实测出来的，不是假设：`@import url(...);` 不带
    # 花括号，于是它会被并进**下一条规则**的选择器 token（`_norm_selector` 折完
    # 空白后长成 `@import url(...); :root`），而下面 `sel.startswith('@')` 那句
    # 又把整条规则丢掉 —— style.css 顶部原先那句 @import 就这样让紧随其后的
    # `:root { ... }`（全站设计令牌的唯一定义处）对本文件**每一条**基于
    # _rules/_rules_ctx 的断言隐身。vendor 本地化删掉 @import 的那一刻才暴露：
    # `:root` 一进扫描范围，按钮层叠模型立刻报「读不懂 :root」，10 条断言变红。
    # 用 `[^;{}]*` 限定，保证不会跨进任何花括号块。
    css = re.sub(r'@(?:import|charset|namespace)\b[^;{}]*;', '', css, flags=re.I)
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
    # 活动任务卡片 -> 统一任务表实时行（.task-card -> .task-row，2026-07 改版）：
    # h6 标题 -> .task-name 名称单元格，计数/时长文本类名不变、宿主变了。
    # 2026-08 统一流式列表重设计：这两类文本仍在 .task-row 内
    # （名称在行1，计数/时间在行2/行1），选择器与值不变，本表条目不动。
    '.task-row .task-name': 'var(--font-size-base)',
    '.task-row .progress-detail': 'var(--font-size-sm)',
    # 登记（2026-08 统一流式列表重设计）——本表删除 4 条，全部随 9 列
    # .task-table 与徽章 pill 一起成为死代码（不是「合并漏条」）：
    #   '.task-row .badge'      —— 行内徽章 pill 废除（状态点 + 状态小字替代）；
    #   '.table' / '.table th' / '.table small'
    #                           —— 全站最后一张 <table> 废除，.table* 规则
    #                              整段从 style.css 删除（见 style.css Table
    #                              Styles 段的删除登记）。
    '.config-section h3': 'var(--font-size-md)',
    # A2 / Task 7 把这一条从 `.progress-bar` **搬到** `.progress__label`：
    # 百分比数字不再是进度条自己的子元素了，条里一个字都没有，给一个空元素
    # 声明字号是死代码。值原样不变（0.875rem），承载它的元素换了个。
    '.progress__label': 'var(--font-size-sm)',
    '.badge': 'var(--font-size-xs)',
    # 这里原本还有 '.status-badge': 'var(--font-size-xs)'。整个 .status-badge
    # 组件（基规则 + 五个状态分支前缀）已从 style.css 删除：任务行改用
    # .task-dot + .task-status-text 之后没有任何 markup 会带上这个类，
    # 全仓只剩 history.js / tasks.js 两处注释在提它。同组的 .badge.bg-* 是活的，
    # 由 getStatusColor() 映射出来，仍受上面这条 '.badge' 保护。
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
# 中文回退链：vendor 的 Inter / JetBrains Mono 都不含汉字
# --------------------------------------------------------------------------

# 每个平台至少要点名一个中文 UI 字体。分组而不是拉平成一个集合，是因为漏的
# 恰恰是**某一个平台** —— 改造前 --font-display 点了 macOS 与 Windows、漏了
# Linux，拉平的断言（「至少有一个中文字体」）对那种形态完全失明。
_CJK_FAMILIES_BY_PLATFORM = {
    'macOS':   ('PingFang SC', 'Hiragino Sans GB'),
    'Windows': ('Microsoft YaHei UI', 'Microsoft YaHei'),
    'Linux':   ('Noto Sans CJK SC', 'Source Han Sans SC', 'Noto Sans SC',
                'WenQuanYi Micro Hei'),
}

# 通用族。它一定命中，所以必须排在全部具体字体之后。
_GENERIC_FAMILIES = ('sans-serif', 'serif', 'monospace', 'cursive', 'fantasy',
                     'system-ui', 'ui-monospace', 'ui-sans-serif', 'ui-serif')


def _resolved_font_stack(css: str, name: str) -> str:
    """把 `--font-display` / `--font-mono` 里的 var(--font-cjk) 展开成一条链。"""
    m = re.search(re.escape(name) + r'\s*:\s*([^;]+);', css)
    assert m, f'{name} 未定义'
    stack = m.group(1).strip()
    for _ in range(4):  # 允许嵌套，但不允许成环
        refs = re.findall(r'var\(\s*(--[\w-]+)\s*\)', stack)
        if not refs:
            return stack
        for ref in refs:
            sub = re.search(re.escape(ref) + r'\s*:\s*([^;]+);', css)
            assert sub, f'{name} 引用了未定义的 {ref}'
            stack = stack.replace(f'var({ref})', sub.group(1).strip())
    raise AssertionError(f'{name} 的 var() 展开没有收敛，疑似成环')


@pytest.mark.parametrize('token', ['--font-display', '--font-mono'])
def test_font_stack_names_a_cjk_family_for_every_platform(token):
    """两条字体栈都必须给三大平台各点名一个中文 UI 字体。

    ⚠️ 这条守的是一个**只有中文界面才看得见**的缺陷，而且英文界面全绿。
    vendor 的 Inter 与 JetBrains Mono 只带 latin / latin-ext 两个子集
    （static/vendor/fonts/fonts.css 的文件头写明了），一个汉字都没有 ——
    每个汉字都要靠逐字回退往后找。栈里没有中文字体时选谁**由浏览器的最后
    兜底决定**，Windows 上是宋体，Linux 上看 fontconfig 心情。

    改造前实测：--font-mono 里一个中文字体都没有，而它盖着失败原因
    （.task-error）、任务行行2、任务详情面板、TIF 信息、命令面板 —— 那些中文
    在 Windows 上是宋体，混在周围的黑体系 UI 里一眼就看得出不对。
    """
    stack = _resolved_font_stack(_css(), token)
    for platform, families in _CJK_FAMILIES_BY_PLATFORM.items():
        assert any(f"'{f}'" in stack or f'"{f}"' in stack for f in families), (
            f'{token} 没有为 {platform} 点名任何中文字体，'
            f'该平台的汉字会掉进浏览器兜底。候选：{list(families)}\n实际：{stack}'
        )


@pytest.mark.parametrize('token', ['--font-display', '--font-mono'])
def test_generic_family_comes_last_in_every_font_stack(token):
    """通用族必须排在最后 —— 它一定命中，排在中文字体前面等于把它们全废掉。

    单看上一条断言挡不住这个形态：`'JetBrains Mono', monospace, 'Microsoft YaHei'`
    照样「点名了 Windows 的中文字体」，而那个名字永远轮不到。
    """
    families = [f.strip().strip('\'"')
                for f in _resolved_font_stack(_css(), token).split(',')]
    generics = [i for i, f in enumerate(families) if f in _GENERIC_FAMILIES]
    assert generics, f'{token} 没有通用族兜底'
    # ui-monospace 等 `ui-*` 是通用族，但它们**不保证**命中（不支持的浏览器直接
    # 跳过），所以只要求最后一项是通用族，不要求它们连续。
    assert families[-1] in _GENERIC_FAMILIES, (
        f'{token} 的最后一项是 {families[-1]!r}，不是通用族')
    last_concrete = max(i for i, f in enumerate(families)
                        if f not in _GENERIC_FAMILIES)
    hard_generics = [i for i, f in enumerate(families)
                     if f in _GENERIC_FAMILIES and not f.startswith('ui-')]
    assert all(i > last_concrete for i in hard_generics), (
        f'{token} 里有通用族排在具体字体前面，后面那些名字永远轮不到：{families}')

# --------------------------------------------------------------------------
# 核心断言 3：!important 总量不许回潮
# --------------------------------------------------------------------------

def test_important_count_under_control():
    """!important 声明总量上界 = 37（历史记账见下，headline 随棘轮走）。

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

        （⚠️ 下面这段是 Task 9 当时的记账，其中 #1 #5 已在 C1 收尾随兜底重置
          一起删除 —— 见本 docstring 末尾的「- 2 处」那一条。保留原文是为了
          让「当初为什么加」和「后来为什么能删」对得上。）

        压的是谁、为什么非 !important 不可：
          #1 #4 #5 压的是**本文件自己**的兜底重置
             `div:not(.card):not(...)...{background:transparent}`，特异度 (0,11,1)。
             这三个 Leaflet 元素都是 <div> 且不在那串 :not() 白名单里，
             改前 CDP 实测三者的 computed background-color 全是 rgba(0,0,0,0)。
             不用 !important 的唯一替代是往白名单里再塞三个类 —— 那是在给
             已知的结构债继续加码，明确不做（见
             test_leaflet_div_controls_keep_their_background）。
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
      - 2 处：**C1 收尾「根治 div 兜底重置」删除**（清理型任务）。两处都是
              Leaflet 段的 background-color，删掉的理由是它们压的对象没了 ——
              `div:not(...)...{background:transparent}`(0,11,1) 已整条删除：
          1. `.leaflet-bar, .leaflet-draw-toolbar { background-color }`
             —— 原 !important #1。leaflet.css **没有**任何规则给 `.leaflet-bar`
             容器设背景（它只给 `.leaflet-bar a` 设 #fff），(0,1,0) 无对手。
          2. `.leaflet-draw-tooltip { background-color }` —— 原 !important #5。
             对手只剩 leaflet.draw.css 的
             `.leaflet-draw-tooltip{background:rgb(54,54,54)}` (0,1,0)，
             同分、style.css 排在后面，源码顺序即取胜。
        **保留**的是 `.leaflet-control-attribution`（原 #4）：它还要压
        leaflet.css 的 `.leaflet-container .leaflet-control-attribution
        {background:rgba(255,255,255,.8)}` (0,2,0)，(0,1,0) 赢不了。
        三处的删 / 留逐个做过 CDP 对拍：删掉那两个 !important 前后，
        1600x1000 首页整页截图差异 **0 像素**（`.leaflet-bar` 仍是
        rgb(21,23,28)、`.leaflet-draw-tooltip` 仍是 rgba(12,13,16,0.92)）。
        本次**新增 0 处**。
      = 66 处（本次实测）
      + 1 处 / - 1 处：**统一任务表改版新增、2026-08 扁平化又删除**，净 0，登记如下——
              `.task-row.status-failed td { background-color: var(--color-danger-bg) !important }`
              （统一任务表改版时新增：压 `.table td` 的 `background: transparent !important`，
              失败行整行红洗。2026-08 扁平化随「失败行不铺底色」整条删除：
              整行红洗 + 红框错误框是用户实测否掉的「模板味」形态，
              状态识别改由左条/徽章/错误行承担，不再需要这处 !important。）
      = 66 处（本次实测）
      - 10 处：**2026-08 统一流式列表重设计删除**（清理型任务）。记录面板的
              9 列 .task-table 是全站最后一张 <table>，`.table*` 规则整段
              从 style.css 删除，连带 10 处 !important：
          1. `.table { color / background }`                    2 处
          2. `.table thead / tbody { background }`              2 处
          3. `.table tr { background / border-color }`          2 处
          4. `.table td, .table th { background / border-color }` 2 处
          5. `.table-hover tbody tr:hover { background }`       1 处
          6. `.table-responsive { background }`                 1 处
        删除理由：规则服务的 DOM 不存在了（templates/ 与 static/js/ 已无任何
        表格标记，grep 可证）。其中 `.table td` 的 color 不带 !important
        那条（A7 / Task 12 的修复）随表格一并成为历史。
        本次**新增 0 处**。
      = 56 处（本次实测）

    ⚠️ 本次上界按棘轮规则（清理型）下调：**59 = 实测 56 + 3**。

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
          `.task-row.status-running .task-row__bar::before` (0,3,1) …）都比它强。去掉
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

    ⚠️ 当前余量为 **3**（实测 34 / 上界 37）。

      - 3 处：**2026-08 评审 P2「死 CSS」删除**（清理型任务）。
              `.text-success` / `.text-warning` / `.text-info` 各一条 color
              !important，三个类在 templates/ static/js/ src/ app.py 全部零引用
              （`bg-${getStatusColor(...)}` 只到 `.bg-*`，`'app-toast--' + type`
              只到 `.app-toast--*`，两条动态拼接路径都够不到 `.text-*`）。
              同批删掉的 `.alert-success` / `.alert-warning` 不带 !important。
              本次**新增 0 处**。

    ⚠️ 上面那串从 92 一路减到 56 的账**对不上今天的实测**：本次改动前
    `git show HEAD:static/css/style.css` 实测就是 37，不是 56。那 19 处的去向
    没有登记，不是本次删的，这里不追溯 —— 但上界必须按**实测**重设，否则
    22 个空名额会被后来的人悄悄填回去，棘轮就白装了。

    余下 34 处几乎全是压 Bootstrap 背景/文字色的历史债
    （`background: transparent !important`、`color: ... !important`），
    属于 Phase 2 其他任务的范围，本次不动。

    注意：注释里被剥掉了才计数——否则一句提到 !important 的说明文字就能
    把数字顶上去（本条测试自己的实现就踩过这个坑）。
    """
    css = re.sub(r'/\*.*?\*/', '', _css(), flags=re.S)
    count = css.count('!important')
    assert count <= 37, (
        f'!important 声明有 {count} 处，应 <= 37（上界按棘轮规则从 59 降到 37：'
        '改动前实测 37，本次删掉 .text-success/.text-warning/.text-info 三条'
        '零引用的 color !important 后实测 34，余量 3）'
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


def test_text_center_is_not_redefined_locally():
    """style.css 不许自己定义 `.text-center` —— 一条都不许。

    这条从「.text-center 不该设 color」收紧而来，防线只增不减。
    原因：Bootstrap 的 `.text-center{text-align:center!important}` 自带
    !important，与源码顺序无关地压过任何不带 !important 的同名声明。所以
    「style.css 排在 vendor 之后」这条全站惯例在工具类上**不成立**，本地手抄
    的 `.text-center` 从来没有生效过（2026-08 随另外 8 条工具类复制品一并删除）。
    既然本地规则一律无效，「不许设 color」就退化成「不许存在」：
    后者是前者的超集 —— 有人重新写 `.text-center { color: red }`，这里照样翻红，
    而且连「写了个永远不生效的布局声明」也一起拦下。

    要真的让 .text-center 带上颜色，只能显式写 !important 并说明理由；
    那也会被这条拦住，届时请连同本 docstring 一起改口径。
    """
    matched = [
        (sel, body)
        for sel, body in _rules(_css())
        if re.search(r'\.text-center(?![-\w])', sel)
    ]
    assert not matched, (
        'style.css 里又出现了 .text-center 规则；Bootstrap 的同名工具类带 '
        '!important，本地这份压不过它，等于死代码：\n'
        + '\n'.join(f'  {sel} {{ {body.strip()} }}' for sel, body in matched)
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
#   2. 有效性自检——两条都找不到就说明选择器写法变了，测试已失效。
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
# ⚠️ 2026-08 又删掉一行（同型的第二笔）：
#    `.config-section .form-control:focus, .config-section .form-select:focus`。
#    它与全局 `.form-control:focus, .form-select:focus` 逐条重复（全局那条还
#    多给一条 color，是它的超集），是上面 Task 10 那笔清理自己记下的「遗留」。
#    挡路的 (0,2,0) 规则那时已经删掉，全局规则现在正常命中配置页，留着两份
#    只是让「改焦点色要改两处」。同批删除的还有
#    `.config-section .form-control::placeholder`（与全局完全相同）。
FORM_SELECT_BG_COLORS = {
    '.form-control, .form-select':
        'var(--color-bg-tertiary)',
    '.form-control:focus, .form-select:focus':
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


# 状态映射的唯一来源。改前 getStatusColor 在 tasks.js / history.js 各有一份，
# 本文件因此到处 `_status_color_names('tasks.js') | _status_color_names('history.js')`
# 求并集。两份实现已收口到 static/js/task_status.js（首页两文件同时加载、
# 后者静默遮蔽前者，且两份 getStatusText 查不同的 i18n 前缀），并集随之退化
# 成单文件解析 —— 但**不要**把它内联回字面量清单：从源码解析才能在有人加
# 状态时红。
STATUS_JS = 'task_status.js'


def _status_color_names(js_name=STATUS_JS):
    """getStatusColor 可能返回的全部 Bootstrap 颜色名。

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
         规则在、变量对、测试全绿，而那一档的进度条与轨道同色，
         肉眼完全看不见。这一条就是为了拦住这种「写了等于没写」。
         更一般的可辨识度下限见 test_progress_bar_fill_has_sufficient_contrast
         （同色只是对比度 1:1 的极端情形）。

    覆盖范围（诚实说明）：这条守的是「CSS 源码的形态」。它保证不了
    「浏览器最终算出来是什么颜色」——那部分由 CDP 实测覆盖。
    """
    css = _css()
    required = _status_color_names()
    # 自检：running 走 info（复用早就存在的 .progress-bar.bg-info），
    # secondary 是 A1/Task 5 补上的（'dark' 随 cancelled 退出状态机一起
    # 不再被 getStatusColor 返回）。解析要是出了岔子导致 required 变小，
    # 负向遍历会静默变绿。
    assert {'info', 'secondary'} <= required, (
        f'从 getStatusColor 解析出的颜色名是 {sorted(required)}，'
        '缺了 info/secondary —— 解析逻辑已失效'
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

    而 bg-secondary 恰恰是最承重的一格：改前 `static/js/history.js` 的
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
    required = _status_color_names()
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
    """任务区元素实际压在什么底色上 —— **从 `.card` 解析，不许硬编码调色板变量**。

    名字里的 task_card 是历史遗留：当时它算的是 .task-card 卡片的背衬。
    卡片已改为统一任务表里的 .task-row 行（单元格背景透明，透出面板 .card），
    背衬仍然是 `.card`，函数保留原名以免无谓的连锁改名。

    ⚠️ 这个函数存在的唯一理由，是本文件上一版把背衬写死成
    `_palette_var(css, '--color-bg-secondary')` 并注释「.task-card 的底色」，
    而那是**巧合**：真正的背衬是祖先面板 `.card`，它**恰好**也用
    `--color-bg-secondary`。后果：后面任何一个视觉任务改 `.card` 的底色，
    浏览器里的真实对比度就变了，而拿 `--color-bg-secondary` 算的断言照旧全绿
    —— 与 A1b 抓到的「红色底纹被压成透明而测试全绿」是同一类失明。
    所以这里顺着**真实渲染链**去取：`.card` 声明什么，就用什么。
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
    """失败行里的 `.task-error` 必须存在、在顶层、且文字真的看得清。

    2026-08 扁平化（用户实测反馈整行红洗 + 红框错误框「太像模板」）：
    错误框从「红底 + 整圈红边框的盒子」改为「2px 红色左边条 + 红色文字」
    的引文式排版。本断言跟着换形态，强度不降级：

      1. 顶层恰好一条规则（包进 `@media (min-width: 3000px)` 的规则在文件里
         存在但永不生效——Task 5 评审实测出来的坑）。
      2. 必须声明 `color` 与 `border-left`（新形态的两个可读性来源），
         且**不得**声明 background（铺底色 = 回潮到盒子形态）。
      3. 文字对真实背衬 >= 4.5:1（正文），左边条对背衬 >= 3:1（图形）。
         背衬顺着真实渲染链取（`_effective_task_card_backdrop`：从 `.card`
         的声明解析，不是硬编码调色板变量——硬编码会随 `.card` 改色静默漂移，
         理由见那个函数的 docstring）。

    覆盖范围（诚实说明）：这条守的是 CSS 源码里能算出来的色值关系。
    它保证不了「这段文字在浏览器里真的显示出来了」——那由 CDP 实测覆盖。
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
    for prop in ('color', 'border-left'):
        assert prop in decls, (
            f'.task-error 没有声明 {prop} —— 扁平形态下它是可读性的唯一来源'
        )
    assert 'background' not in decls and 'background-color' not in decls, (
        '.task-error 又铺了底色 —— 2026-08 扁平化后它是无边底引文式；'
        '要恢复盒子形态请先评审（整行红洗 + 红框是实测被否掉的形态）'
    )

    card = _effective_task_card_backdrop(css)

    text = _flatten(_resolve_color(css, decls['color']), card)
    ratio = _contrast_ratio(text, card)
    assert ratio >= ERROR_TEXT_MIN_CONTRAST, (
        f'.task-error 的文字 {text} 对真实背衬(.card) {card} 只有 {ratio:.2f}:1，'
        f'低于 WCAG 正文 {ERROR_TEXT_MIN_CONTRAST}:1 —— 失败原因会「在但看不清」'
    )

    m = re.search(r'(#[0-9a-fA-F]{6}|rgba?\([^)]*\)|var\(\s*--[-\w]+\s*\))',
                  decls['border-left'])
    assert m, (
        f'.task-error 的 border-left 里解析不出颜色：{decls["border-left"]!r} '
        '—— 本测试已失效'
    )
    border = _flatten(_resolve_color(css, m.group(1)), card)
    bratio = _contrast_ratio(border, card)
    assert bratio >= ERROR_BORDER_MIN_CONTRAST, (
        f'.task-error 的左边条 {border} 对真实背衬(.card) {card} 只有 {bratio:.2f}:1，'
        f'低于图形元素 {ERROR_BORDER_MIN_CONTRAST}:1'
    )


# --------------------------------------------------------------------------
# 曾经的 `div:not(...)` 兜底重置：已整条删除（见本文件末尾「根治」一节）。
#
# 这里原先有两条断言，钉的都是「为了绕开兜底重置而必须写成某个样子」：
#   test_task_error_survives_the_blanket_div_reset
#       —— 要求 `.task-error` 出现在那串 :not() 白名单里
#   test_leaflet_div_controls_survive_the_blanket_div_reset
#       —— 要求四个 Leaflet 容器的 background-color 必须带 !important
#
# 兜底重置删掉之后，这两个「怎么绕」的要求全部失效：白名单不存在了，
# 那几处 !important 也不再必要（清理型任务本来就该删）。但它们真正想守的东西
# —— **这些元素的底色在浏览器里必须真的出现** —— 一条都不能丢。
# 所以两条都改成用层叠模型算**最终生效值**：无论将来是靠 !important、靠特异度、
# 还是靠源码顺序赢，只要结果对就绿；结果坏了就红。
# --------------------------------------------------------------------------


def _effective_bg_for(chain):
    """算出这个元素最终拿到的背景声明。找不到返回 None。见文件末尾的层叠模型。"""
    cands, unsupported = _bg_candidates(chain, _all_sheets())
    assert not unsupported, (
        f'{_describe(chain)}: 有背景规则命中它但模型读不懂 —— 结论不可信：'
        + '、'.join(f'{sh}:{br}' for sh, br in unsupported[:5])
    )
    return _winning_bg(cands)


# `.task-error` 在真实 DOM 里的祖先链（history.js createTaskRow 生成——
# 2026-08 单一时间流定稿后全站唯一行实现，失败行的行2 就是它，
# 由 test_runtime_injected_div_table_is_grounded 同源的那张表描述错误框本身）。
# 2026-08 两轮演进：错误框从错误表格行
# （tbody#activeTasksBody > tr.task-error-row > td > .task-error）
# → 列表里的错误节点（div#activeTasksBody > div.task-error-row > div.task-error）
# → 单一时间流失败行的行2（div#historyTableBody > div.task-row.status-failed
#   > div.task-error；实时区与独立错误行节点都随三分区删除）。
def _task_error_chain():
    return _PAGE_CHAIN_PREFIX + (
        ('section', {'workbench-panel', 'workbench-panel--wide'}, 'historyPanel', {}),
        ('div', {'workbench-panel__body'}, '', {}),
        ('div', {'card'}, '', {}),
        ('div', {'card-body'}, '', {}),
        ('div', set(), 'historyTableBody', {}),
        ('div', {'task-row', 'status-failed'}, '', {}),
        ('div', {'task-error'}, '', {}),
    )


def test_task_error_box_background_actually_reaches_the_screen():
    """扁平化形态契约：`.task-error` 不得再靠底色呈现（2026-08）。

    这条的前身守的是「红色底纹必须真的渲染出来」（底纹曾被兜底重置压成
    透明，源码存在、浏览器里没有）。2026-08 扁平化把错误框从红底盒子改为
    无边底引文式（2px 左边条 + 红字），这条跟着翻面：

      1. `.task-error` 的最终生效背景必须是不存在或透明——有人把底色写回来
         就是回潮到「盒子里的盒子」形态；
      2. 文字色必须解析为 --color-danger（可读性由文字色承担，
         对比度由 test_task_error_box_exists_and_is_readable 按背衬实算）。
    """
    win = _effective_bg_for(_task_error_chain())
    assert win is None or _bg_is_transparent(_css(), win.value), (
        f'.task-error 的最终生效背景是 {win.value}（来自 {win.sheet} 的 `{win.branch}`）'
        '—— 扁平化后它不该有底色。要恢复盒子形态请先评审'
    )
    css = _css()
    rules = [
        (sel, body) for sel, body, at_ctx in _rules_ctx(css)
        if not at_ctx and '.task-error' in _selector_parts(sel)
    ]
    assert len(rules) == 1, '顶层 .task-error 规则必须是恰好 1 条 —— 本测试已失效'
    color = _decl_map(rules[0][1]).get('color')
    assert color is not None and _resolve_color(css, color) == \
        _palette_var(css, '--color-danger'), (
        f'.task-error 的文字色是 {color}，不是 --color-danger —— '
        '无边底形态下文字色是唯一的可读性来源'
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


# 时间流里会横向排布的四个 flex 容器。每一个都必须允许换行 —— 只要有一个漏掉，
# 它就是那条横向滚动条的来源（成员大多 flex:0 0 auto，不换行就只能溢出）。
WRAPPING_STREAM_ROWS = (
    '.task-line1',            # 行1：状态点 / 名称 / 元信息 / 状态小字 …… 行尾
    '.task-line1__tail',      # 行尾：耗时 + 动作组（手机档下它自己也要能拆）
    '.task-progress-line',    # 行2 活动态：进度条 + 百分比 / 计数 / 速度
    '.task-gap-line',         # 行2 缺口态：分档读数 + 两颗决策按钮
)


def test_task_stream_rows_cannot_grow_a_horizontal_scrollbar():
    """任务时间流在任何面板宽度下都不许出现横向滚动条。

    机制有两层，缺一条就回到原样（实测：面板 602px 时列表内容宽 600 / 可视
    555，一条横向滚动条压在列表底部，右侧动作按钮被切掉一半）：

      1. 四个横向 flex 容器都 `flex-wrap: wrap`。行内成员大多是
         `flex: 0 0 auto` 的不折行小字（#类型:id、元信息、状态小字、缺块徽章、
         耗时、按钮组、三段读数），单行 flex 下它们既不收缩也不换行，宽度之和
         一超过行宽就直接溢出；
      2. `.card--grow #historyTableBody` 显式 `overflow-x: hidden` 兜底 ——
         它的 `overflow-y: auto` 会把 overflow-x 一并算成 auto，任何一处没料到
         的溢出都会长出滚动条（本项目的老熟人，见 .tif-info--scroll 与
         #app-toast-container）。

    另外钉住贴右的 `margin-left: auto` 在**行尾包裹元素**上而不是 `.task-time`
    上：两个兄弟各挂一个 auto 会平分剩余空间，把耗时推到行中间。
    markup 侧的对应断言：
    tests/test_tasks_js_contract.py::test_row_tail_keeps_time_and_actions_together。
    """
    css = _css()
    top = [(sel, body) for sel, body, at_ctx in _rules_ctx(css) if not at_ctx]
    problems = []
    for part in WRAPPING_STREAM_ROWS:
        decls = {}
        for sel, body in top:
            if part in _selector_parts(sel):
                decls.update(_decl_map(body))
        if not decls:
            problems.append(f'{part} 没有顶层规则 —— 本断言已失效（类名改了？）')
        elif decls.get('flex-wrap', '').strip().lower() != 'wrap':
            problems.append(
                f'{part} 的 flex-wrap 是 {decls.get("flex-wrap")!r}，必须是 wrap'
            )
    stream = {}
    for sel, body in top:
        if '.card--grow #historyTableBody' in _selector_parts(sel):
            stream.update(_decl_map(body))
    assert stream, '.card--grow #historyTableBody 没有顶层规则 —— 本断言已失效'
    if stream.get('overflow-x', '').strip().lower() != 'hidden':
        problems.append(
            f'.card--grow #historyTableBody 的 overflow-x 是 '
            f'{stream.get("overflow-x")!r}，必须显式 hidden（overflow-y:auto '
            '会把它算成 auto）'
        )
    tail, time_el = {}, {}
    for sel, body in top:
        parts = _selector_parts(sel)
        if '.task-line1__tail' in parts:
            tail.update(_decl_map(body))
        if '.task-line1 .task-time' in parts:
            time_el.update(_decl_map(body))
    if tail.get('margin-left', '').strip() != 'auto':
        problems.append('.task-line1__tail 不再 margin-left:auto —— 行尾不贴右了')
    if time_el.get('margin-left', '').strip() == 'auto':
        problems.append(
            '.task-line1 .task-time 又挂上了 margin-left:auto —— 与行尾包裹元素'
            '的 auto 平分剩余空间，耗时会飘到行中间'
        )
    assert not problems, '任务时间流的行布局会溢出：\n' + '\n'.join('  ' + p for p in problems)


def _px(value):
    """`-12px` / `12px` / `0` -> float。看不懂的返回 None（响亮失败优于放行）。"""
    m = re.match(r'^(-?[\d.]+)(px|rem)?$', value.strip().lower())
    if not m:
        return None
    if m.group(2) is None:
        return 0.0 if float(m.group(1)) == 0 else None
    return float(m.group(1)) * (16 if m.group(2) == 'rem' else 1)


def test_config_footer_is_a_real_bottom_bar_inside_the_panel():
    """配置面板的底部操作条必须真的贴在面板的左/右/下三条边上，
    且两颗按钮不许锁死高度。

    改前它只是**声称**自己是「一条贴边的横带」（CSS 注释原文）：宿主
    `.workbench-panel__body` 有 12px 内边距，深色带被框在里面 —— 480px 面板
    实测带 933~1388、底 888，而面板是 920~1400、底 900，左右各露 12px、
    下面再露 12px 的面板底色，看着是一块悬空的深色矩形；基础规则的
    `padding: 10px 6px 0 0` 又让两颗按钮的下沿正好压在带的下边缘上。

    三条：
      1. 面板变体的负外边距 = 宿主内边距的相反数（右、下两侧）。宿主的
         12px 改了而这里忘了跟，横带会重新缩回去，或者反过来溢出面板；
      2. 基础规则的上下内边距相等 —— 按钮不再贴着带的下边缘；
      3. 按钮只给 min-height。面板拖到下限 320px 时英文标签折成两行
         （"Reset to defaults" / "Save settings"），锁死的 34px 会把第二行
         整行裁掉（`.btn` 带 overflow: hidden，实测「settings」只剩上半截）。
    """
    css = _css()
    top = [(sel, body) for sel, body, at_ctx in _rules_ctx(css) if not at_ctx]

    def decls_for(part):
        out = {}
        for sel, body in top:
            if part in _selector_parts(sel):
                out.update(_decl_map(body))
        return out

    host = decls_for('.workbench-panel__body')
    host_pad = _px((host.get('padding') or '').split()[0] if host.get('padding') else '')
    assert host_pad, (
        f'.workbench-panel__body 的 padding 读不出来（{host.get("padding")!r}）—— 本断言已失效'
    )
    footer_in_panel = decls_for('.workbench-panel__body--fill .config-footer')
    margin = (footer_in_panel.get('margin') or '').split()
    assert len(margin) == 3, (
        f'面板变体的 margin 期望三值简写（上 / 左右 / 下），实际 {margin!r} —— 本断言已失效'
    )
    problems = []
    for side, raw in (('左右', margin[1]), ('下', margin[2])):
        got = _px(raw)
        if got is None or got != -host_pad:
            problems.append(
                f'操作条{side}外边距是 {raw!r}，应为 {-host_pad:g}px（抵消宿主'
                f' .workbench-panel__body 的 {host_pad:g}px 内边距，否则横带不贴边）'
            )
    base_pad = (decls_for('.config-footer').get('padding') or '').split()
    if len(base_pad) != 2 or _px(base_pad[0]) is None:
        problems.append(
            f'.config-footer 的 padding 期望「上下 左右」两值简写，实际 {base_pad!r}'
        )
    btn = decls_for('.config-footer .btn')
    if 'height' in btn:
        problems.append(
            f'.config-footer .btn 又锁死了 height: {btn["height"]} —— 标签折行时'
            '第二行会被 .btn 的 overflow:hidden 裁掉'
        )
    # `_resolve_length_px` 而不是 `_px`（2026-08-15 Task 3）：这个下限从字面量
    # 34px 换成了 `var(--ctl-h-lg)`（控件高度两级刻度的主操作档），而 `_px` 只
    # 认字面量、遇到 var() 返回 None —— 它当场响亮地红了，正是这个设计想要的
    # 结果。契约本身没变：仍然是「min-height 必须读得出一个 px 下限」。
    if _resolve_length_px(css, btn.get('min-height', '')) is None:
        problems.append(
            f'.config-footer .btn 的 min-height 读不出来（{btn.get("min-height")!r}）'
            '—— 两颗按钮的等高下限没了'
        )
    assert not problems, '配置面板底部操作条的布局不合契约：\n' + '\n'.join('  ' + p for p in problems)


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
         8px 高，放不下 0.875rem 的文字」。**这个前提是错的**：CDP 实测当时
         任务卡的 .progress 的 computed height 是 **28px**（两处渲染点都内联
         `style="height: 28px"`，压过了 CSS 里那条不生效的 8px）。
         照抄那个方案会把当时就能看见的百分比直接删掉。
         （后注：统一任务表改版后，活动任务行改用 14px 紧凑条 + 条外
         .task-pct，不再使用 .progress/.progress__label；详情模态框仍是
         28px 轨道 + 覆盖层。）

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
    required = _status_color_names()
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


_INCLUDE_RE = re.compile(r'\{%\s*include\s+"([^"]+)"\s*%\}')


def _template(name):
    """读模板源码，并文本级展开 `{% include "..." %}`。

    工作台改版把 history/config 的内容抽成 _history_content.html /
    _config_content.html 两个 partial，独立页与首页覆盖面板共享同一份标记。
    这里的 include 是无参纯包含，文本级展开与 Jinja 渲染结果等价 ——
    不展开的话，所有针对 history.html / config.html 内容的断言扫到的都是
    空壳，测试全绿而页面内容无人守卫。
    """
    with open(os.path.join(_TEMPLATES_DIR, name), encoding='utf-8') as f:
        src = f.read()

    def _sub(m):
        return _template(m.group(1))

    prev = None
    while prev != src:
        prev = src
        src = _INCLUDE_RE.sub(_sub, src)
    return src


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
    （已核对 CDN 源码 bootstrap@5.3.0/dist/css/bootstrap.css 的
    `[data-bs-theme=dark]` 块），
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
    「浏览器真的拿到了这个文件」。**vendor 本地化之后这层已经补上了**：
    Bootstrap 落在 static/vendor/bootstrap/5.3.0/，由
    test_vendor_tree_matches_the_manifest（文件在、字节数对）+
    test_vendor_builds_match_the_version_in_their_path（文件里真有
    `[data-bs-theme=dark]`）+ test_no_template_references_an_external_url
    （没人把 <link> 改回 CDN）三条合起来钉住。本条只剩「模板声明的版本号够新」
    这一层语义。（本地化之前这里写的是「属于本项目既有的 CDN 依赖问题」，
    并指向 style.css 里那段 .row/.col-* 离线兜底注释 —— 那段已随本地化删除。）
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
    # `_` 开头的是被 include 的内容 partial（_history_content.html /
    # _config_content.html），不是页面：它们渲染时永远嵌在 extends base.html
    # 的页面里，主题由宿主页面提供，不需要（也不能）自己带 <html>。
    names = sorted(
        n for n in os.listdir(_TEMPLATES_DIR)
        if n.endswith('.html') and n != 'base.html' and not n.startswith('_')
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


def _class_tokens(text):
    """选择器片段里的类名 token 集合。

    必须按 token 比而不是子串比：`'leaflet-draw' in '.leaflet-draw-actions'`
    是真的，但 `.leaflet-draw-actions a` 命中的是操作条按钮、**不是**雪碧图按钮，
    在它身上写 background-color 完全合法。子串匹配会把它误判成违规。
    """
    return set(re.findall(r'\.([\w-]+)', text))


def _transparent(value):
    """这个 background-color 的值等价于「透明」吗？"""
    v = _IMPORTANT_RE.sub('', value or '').strip().lower()
    if v in ('transparent', 'initial', 'unset', 'revert', 'revert-layer'):
        return True
    m = re.fullmatch(r'rgba?\(\s*[\d.]+[\s,]+[\d.]+[\s,]+[\d.]+\s*[,/]\s*0*(?:\.0+)?\s*\)', v)
    return bool(m)

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


# 键名快照是对着这个版本提取的。cdnjs 与 npm 两种 URL 写法都认。
LEAFLET_DRAW_PINNED_VERSION = (1, 0, 4)
_LEAFLET_DRAW_VERSION_RES = (
    re.compile(r'leaflet[.-]?draw@(\d+)\.(\d+)\.(\d+)', re.I),
    re.compile(r'/leaflet\.draw/(\d+)\.(\d+)\.(\d+)/', re.I),
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

    ⚠️ 覆盖边界（2026-08-15 Task 3 记）：`re.search` 取的是**全文第一处**定义，
    不区分 `:root` 与主题覆盖块（`:root[data-bs-theme="light"]`）。今天无害 ——
    本轮新加的令牌（`--ctl-h-lg` / `--weight-*` / `--z-*` / `--dur-*` / `--ease`）
    每个都只在 `:root` 定义一次，而主题块里只有颜色。但形态是脆的：往亮色块里
    补一条 `--z-modal` 或 `--dur-base`，**所有**顺着本函数取值的解析器
    （`_resolve_length_px` / `_resolve_z_index` / `_token_px` / `_time_to_seconds`）
    都会读到「先出现的那一份」，而全套断言仍然全绿 —— 它们比的是同一个错值。
    要按主题取值必须走 `_theme_var()`（它显式在 `:root[data-bs-theme="light"]`
    的前/后切分文本）。本轮没有顺手改成 `:root` 限定，因为那要同时决定
    「主题块允许定义哪些类别的令牌」，是一条独立决策；已记进 Task 3 的账本。
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


def _resolve_z_index(css, value, _depth=0):
    """`1000` / `var(--z-modal)` -> int。解析不了返回 None。

    为什么不复用上面的 `_resolve_length_px`（2026-08-15 Task 3 令牌化层栈时
    实测过）：那是**长度**解析器，`z-index` 的值是无单位整数，不是长度 ——
    `_length_to_px('1000')` 返回 None（只有 `'0'` 因为「无单位 0 是合法长度」
    那条特例侥幸返回 0.0）。所以这里是尽可能小的一个专用解析器：同一套
    「只跟 var() 链、不支持回退值和 calc()、解析不了就返回 None 让调用方响亮
    失败」的口径，只是终点换成 int。

    三处消费者（都是 Task 3 之前直接对字面量做字符串比对的断言）：
      tests/test_command_palette.py      —— .cmdk 必须是 13100
      tests/test_drop_process.py         —— .drop-veil 必须是 13000
      tests/test_fix_terrain_preview_transition.py —— 薄雾必须低于工具条
    """
    if value is None:
        return None
    value = _IMPORTANT_RE.sub('', value).strip()
    m = re.fullmatch(r'var\(\s*(--[-\w]+)\s*\)', value)
    if m:
        if _depth > 4:
            return None                      # 自引用/环，别死循环
        return _resolve_z_index(css, _custom_property_raw(css, m.group(1)), _depth + 1)
    return int(value) if re.fullmatch(r'[+-]?\d+', value) else None


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
    # 顶层恰好两个元素：读数网格 + 操作行（「下载」按钮 / 调整提示）。
    # 操作行是 2026-07 UX 改版加的（下载不再常驻 dock，入口挂在选区上），
    # 它在网格**下方**，不影响上面「8 格 / 4 列 = 2 行」的算式；
    # 这条白名单仍然堵「在网格上面加标题/说明」那个真实逃逸。
    assert parser.top == [('div', {'bounds-grid'}), ('div', {'bounds-actions'})], (
        f'「已框选」分支的顶层元素是 {parser.top}，'
        '期望 <div class="bounds-grid"> + <div class="bounds-actions"> 两个。'
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
    显示出来并撑破 2 行网格；而且它必须由 **style.css 自己**定义。
    原因在 vendor 本地化时换过一次：以前是「CDN 不可达时 Bootstrap 的
    .visually-hidden 不存在」，Bootstrap 随包发布后这条不再成立；
    现在的理由是**不受上游版本影响** —— Bootstrap 哪天重命名了
    .visually-hidden，代价是屏读用户听到的坐标，而那种回归没有任何测试看得见。
    """
    branch, _markup = _bounds_readout_markup(
        _js_function_body(_js('map.js'), 'updateBoundsInfo')
    )
    problems = []
    # i18n 改造后方位词不再是源码里的中文字面量，而是
    # `${t('js.map.bounds.sr_north')}`。分两段查：源码里方位 key 必须紧挨着对应
    # 坐标（守配对），目录里那条 key 的中文必须是对应方位词（守文案）。
    from src.i18n.catalog import MESSAGES

    for letter, field in sorted(BOUNDS_LABEL_FIELDS.items()):
        word = BOUNDS_SR_WORDS[field]
        key = f'js.map.bounds.sr_{field}'
        if MESSAGES.get(key, {}).get('zh') != word:
            problems.append(
                f'{letter}/{field}: 文案 {key} 的中文是 '
                f'{MESSAGES.get(key, {}).get("zh")!r}，期望 {word!r}')
            continue
        if not re.search(
            r'<span class="bounds-sr">\s*\$\{\s*t\(\s*[\'"]' + re.escape(key)
            + r'[\'"]\s*\)\s*\}\s*</span>\s*'
            r'\$\{\s*f\(\s*currentBounds\.' + field + r'\s*\)\s*\}', branch
        ):
            problems.append(
                f'{letter}/{field}: 缺少 `<span class="bounds-sr">${{t(\'{key}\')}}</span>`'
                f' 或它没有紧跟 currentBounds.{field}')
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
        '这个类必须由本站自己定义 —— 改用 Bootstrap 的 .visually-hidden，'
        '上游哪天重命名它，方位词就会直接显示出来并撑破 2 行网格'
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
# 模型思路与 A4 / Task 9 的渲染链断言同源：把源码里的量算成一个可对拍的数字。
# dock 时代拿 CDP 实测的按钮 bottom 校准；2026-07 弹窗化之后从 vendor
# bootstrap.min.css 与 style.css 解析弹窗框架度量；2026-08-15 Task 5 两个弹窗
# 合成一个 #createPanel 之后，「提交钮在折叠线以上吗」不再是一道算术题（见下）。
# --------------------------------------------------------------------------

VIEWPORT_1366_HEIGHT_PX = 768

# --------------------------------------------------------------------------
# 2026-08-15 Task 5：两个弹窗 -> 一个 #createPanel，**折叠线从算术题变成结构题**。
#
# 沿革（三代模型，问的是三个不同的问题）：
#   1. dock 时代 —— 「常驻面板里提交钮的 bottom <= 768 吗」。CDP 实测 949.34，
#      溢出 181.3px。
#   2. 2026-07 弹窗时代 —— 「弹窗在默认态下要不要内部滚动才看得到提交钮」。
#      纵向起点由 Bootstrap 的 .modal-dialog margin 决定，所以锚点从「CDP 实测
#      的常量」换成「解析出来的弹窗度量」（_modal_metrics：style.css 优先、
#      vendor 兜底 —— 只读 vendor 会漏掉 style.css 把 .modal-header /
#      .modal-body 内边距从 16 覆盖到 24 那一手，模型比浏览器矮 24px）。
#      这一代的结论是**红的**：headless 实测 #createTaskBtn 的 bottom = 901.50，
#      视口 768，提交钮在折叠线下 133.5px；模型算 865.50（下界，差 36 = 两处
#      文案折行）。1366x720 一档更差，实测溢出 91px。
#   3. 现在 —— 提交钮**不在滚动区里**。#createPanel 是 position: fixed 且
#      top/bottom 都为 0（满视口高），内部三层 flex：.config-layout 列容器 /
#      .config-scroll 吃满剩余高度并 overflow-y: auto / .config-footer
#      flex: 0 0 auto 贴在底部。#createTaskBtn 在 .config-footer 里、在
#      #taskForm **之外**（靠 form="taskForm" 关联）。
#
# 于是它的 bottom **与表单内容完全无关**：
#       bottom = 视口高 - .config-footer 的下内边距
# 而 .workbench-panel__body 的 12px 下内边距被 .workbench-panel__body--fill
# .config-footer 的 -12px 下外边距**恰好抵掉**（底条贴边），所以中间不再有任何
# 会随内容变化的项。浏览器实测（headless Chromium，四条管线各量一次）：
#       1366x768 -> bottom = 756.00（视口 768，余 12）
#       1600x900 -> bottom = 888.00（视口 900，余 12）
#   四条管线、明暗两主题、两个视口共 16 组，bottom 全部相同，
#   elementFromPoint 命中提交钮本体，.config-scroll 的 scrollTop 全为 0。
#   其中 map / local_terrain / contour 三条在 768 下**表单确实在滚**
#   （scrollHeight > clientHeight）—— 表单滚而提交钮不动，正是这次要的形态。
#
# 所以这一节现在分成两条互补的断言，各自问一个能被破坏的问题：
#   - test_submit_button_fits_at_1366x768 —— 结构前提是否还成立
#     （满高面板 + 底条在滚动区外 + 底条不随内容流动）。它算的是同一个公式，
#     但公式里没有内容项，所以它是**恒等式**而不是拟合。
#   - test_create_panel_form_content_does_not_grow_further —— 表单内容栈高的
#     **棘轮**。折叠线不再受它影响，但「有人往表单里塞 200px 字段」仍然是
#     真实的退化（滚动条越长、首屏能看到的字段越少），而它是唯一还会变的量。
#     结构模型对它是完全失明的，所以棘轮必须留着，只是换了被测的量。
#
# _FormStructureParser 的整棵子树解析原样保留（锚点 #downloadForm -> #taskForm）：
# 「加字段 / 加 height:44px / 往既有字段组里嵌一组 label+select 都会被算进来」
# 这条能力现在服务于内容棘轮。
# --------------------------------------------------------------------------

_BOOTSTRAP_MIN_CSS = os.path.join(
    os.path.dirname(os.path.dirname(CSS_PATH)),
    'vendor', 'bootstrap', '5.3.0', 'bootstrap.min.css')


def _bootstrap_modal_metrics():
    """从 vendor 的 bootstrap.min.css 解析弹窗布局度量（全部 fail-loud）。

    解析 vendor 文件而不是手写常量的理由与本文件其它 vendor 探针一致：
    Bootstrap 升级改了这些值时，这里要么跟着算出新的（正确的）结果，
    要么因为模式匹配不上而报「测试已失效」——两种情况都不会静默放行。

    ⚠️ 本函数返回的是**兜底值**，调用方一律走 `_modal_metrics(css)`。理由与
       BS_BTN_PADDING_Y_PX 那段注释完全同构（`.btn` 的纵向内边距已经为此翻过
       一次车）：style.css 排在 bootstrap.min.css 之后、同名属性上特异度相同，
       **浏览器里 style.css 赢**。拿 vendor 当真值 = 拿一个和模型同样过期的
       常量当真值。

    ⚠️ --bs-modal-margin 在 vendor 里出现**两次**：
           .modal{--bs-modal-margin:0.5rem}                        基础
           @media (min-width:576px){.modal{--bs-modal-margin:1.75rem}}
       1366px 视口命中后者（28px）。第一版用 `re.search` 取了第一条（8px），
       弹窗的纵向起点因此低算 20px（实测 .modal-dialog 的 margin-top = 28px）。
       现在取最后一条，并要求这两条递增 —— 条数或顺序变了就响亮失败。
    """
    with open(_BOOTSTRAP_MIN_CSS, encoding='utf-8') as f:
        src = f.read()

    def grab_all(pattern, label):
        vals = re.findall(pattern, src)
        assert vals, f'vendor bootstrap.min.css 里找不到 {label} —— 构建变了，本测试已失效'
        return vals

    def grab(pattern, label):
        return grab_all(pattern, label)[0]

    def to_px(raw):
        m = re.match(r'^([\d.]+)(px|rem)$', raw)
        assert m, f'{raw!r} 不是 px/rem 字面量 —— 本测试已失效'
        return float(m.group(1)) * (16 if m.group(2) == 'rem' else 1)

    margins = [to_px(v) for v in grab_all(
        r'--bs-modal-margin:([\d.]+rem)', '弹窗 margin')]
    assert len(margins) == 2 and margins[1] > margins[0], (
        f'--bs-modal-margin 不再是「基础 + ≥576px 断点」两条递增的声明（解析到 '
        f'{margins}）—— 1366px 视口该用哪一条说不清了，本测试已失效'
    )

    # .modal-header .btn-close 的纵向内边距与负外边距在 vendor 里是同一个变量的
    # +.5 / -.5，**精确抵消**，所以它只按 1em 的图标盒参与标题行高度。
    # 实测（1366x768 headless Chromium）：btn-close 外框 31px（15 + 2×8），标题
    # 行盒 27px，btn-close 上下各溢出 2px，而 .modal-header 高度 = 24+24+27+1 = 76
    # —— 那 31px 从来没进过高度。旧常量 BS_MODAL_BTN_CLOSE_PX = 22.5 把 1.5em
    # 当成布局高度，是个恰好不吃紧（27 > 22.5）所以一直没被发现的虚构值。
    close_rule = grab(r'\.modal-header \.btn-close\{([^}]*)\}', '.modal-header .btn-close')
    assert ('padding:calc(var(--bs-modal-header-padding-y) * .5)' in close_rule
            and 'margin:calc(-.5 * var(--bs-modal-header-padding-y))' in close_rule), (
        '.modal-header .btn-close 的「内边距 = 负外边距」抵消关系变了'
        f'（{close_rule[:120]!r}）—— btn-close 会开始参与标题行高度，本测试已失效'
    )

    return {
        # ≥576px 的 .modal-dialog 上外边距（1366px 视口命中这条）
        'margin_top': margins[-1],
        'padding': to_px(grab(r'--bs-modal-padding:([\d.]+rem)', '弹窗 body padding')),
        'header_padding': to_px(grab(
            r'--bs-modal-header-padding:([\d.]+rem)', '弹窗 header padding')),
        'border_width': to_px(grab(r'--bs-border-width:([\d.]+px)', '边框宽度')),
        'title_line_height': float(grab(
            r'--bs-modal-title-line-height:([\d.]+)', '标题行高')),
        # btn-close 参与布局的高度 = 图标盒 1em（内边距被负外边距抵掉，见上）
        'btn_close_em': 1.0,
    }


def _modal_metrics(css):
    """弹窗框架度量：**style.css 优先，vendor 兜底**。

    形状照抄同文件里 `.btn` 那条已经修好的路 —— `_effective_button_height`
    从 style.css 解析纵向内边距，只在 style.css 沉默时回落到 BS_BTN_PADDING_Y_PX。
    那段注释里记着这个盲区第一次发作的样子：`.btn{padding:1rem 2rem}` 时
    「真实 bottom 735.53 而模型仍算 715.53 …… 那条断言比的是『模型 vs 一个和
    模型同样过期的常量』，两边一起错，差值当然是 0」。弹窗这一环犯的是同一个
    错（D3），所以修法也一样。

    实测确认的三处覆盖（各自与 vendor 同特异度、style.css 在后所以赢）：
        .modal-header  { padding: var(--space-5) }  -> 24px（vendor 16）
        .modal-body    { padding: var(--space-5) }  -> 24px（vendor 16）
        .modal-content { border: 1px solid … }      -> 1px（与 vendor 同值）
    `.modal-dialog` 的外边距 style.css 目前没有覆盖，仍走 vendor 的 28px ——
    但下面照样查，将来有人覆盖了模型要自动跟上。

    这个盲区的指纹：修好之前把 `--space-5` 扰动 1px，模型输出变化恰好 0.00。
    """
    metrics = dict(_bootstrap_modal_metrics())

    def own_body(selector):
        bodies = [b for sel, b, ctx in _rules_ctx(css) if sel == selector and not ctx]
        assert len(bodies) <= 1, (
            f'期望至多 1 条 `{selector}` 规则，实际 {len(bodies)} 条 —— '
            '哪一条赢说不清了，本模型已失效'
        )
        return bodies[0] if bodies else None

    def own_pad_top(selector):
        body = own_body(selector)
        if body is None:
            return None
        decls = dict((n, v) for n, v, _i in _expanded_box_decls(body))
        raw = decls.get('padding-top')
        if raw is None:
            return None
        v = _resolve_length_px(css, raw)
        assert v is not None, (
            f'`{selector}` 的 padding-top = {raw!r}，解析不了 —— 本模型已失效（不是通过）'
        )
        return v

    for key, selector in (('header_padding', '.modal-header'), ('padding', '.modal-body')):
        got = own_pad_top(selector)
        if got is not None:
            metrics[key] = got

    content = own_body('.modal-content')
    if content is not None:
        raw = _decl_map(content).get('border')
        if raw:
            m = re.match(r'^([\d.]+)(px|rem)\b', _IMPORTANT_RE.sub('', raw).strip())
            assert m, (
                f'`.modal-content` 的 border = {raw!r} 里读不出宽度 —— 本模型已失效'
            )
            metrics['border_width'] = float(m.group(1)) * (16 if m.group(2) == 'rem' else 1)

    dialog = own_body('.modal-dialog')
    if dialog is not None:
        decls = _decl_map(dialog)
        raw = decls.get('margin-top') or decls.get('margin')
        if raw:
            top = _IMPORTANT_RE.sub('', raw).strip().split()[0]
            v = _resolve_length_px(css, top)
            assert v is not None, (
                f'`.modal-dialog` 的上外边距 {top!r} 解析不了 —— 本模型已失效'
            )
            metrics['margin_top'] = v

    return metrics


def _bootstrap_utility_margins():
    """vendor 的 `.mt-N` / `.mb-N` 间距工具类 -> px。

    从 vendor 解析而不是写死「.mt-2 == 8」：Bootstrap 的 $spacer 一改这里跟着
    变，解析不到就响亮失败。style.css 覆盖了同名工具类（`.mb-3` 就是）时由
    调用方优先取 style.css。
    """
    with open(_BOOTSTRAP_MIN_CSS, encoding='utf-8') as f:
        src = f.read()
    out = {}
    for cls, raw in re.findall(
            r'\.(m[tb]-\d+)\{margin-(?:top|bottom):(-?[\d.]+rem|0)!important\}', src):
        out[cls] = 0.0 if raw == '0' else float(raw[:-3]) * 16
    assert {'mt-2', 'mb-2', 'mb-3'} <= set(out), (
        'vendor bootstrap.min.css 里解析不到 .mt-2 / .mb-2 / .mb-3 间距工具类 '
        '—— 本模型已失效'
    )
    return out


def _bootstrap_horizontal_classes():
    """vendor 里带 `display: flex / inline-flex` 的类名集合。

    高度模型要靠它区分「横排容器」（子元素挤在一行，高度取最高的那个）和
    「竖排容器」（子元素各占一行，高度累加）。`.row` / `.d-flex` 写死在模型里
    也能跑，但 Bootstrap 哪天把 `.row` 改成 grid，写死的版本会静默算错。
    """
    with open(_BOOTSTRAP_MIN_CSS, encoding='utf-8') as f:
        src = f.read()
    out = {cls for cls, body in re.findall(r'\.([-\w]+)\{([^}]*)\}', src)
           if re.search(r'display:\s*(?:inline-)?flex', body)}
    assert {'d-flex', 'row'} <= out, (
        'vendor bootstrap.min.css 里 .d-flex / .row 不再是 display:flex —— 本模型已失效'
    )
    return frozenset(out)


def _assert_bootstrap_btn_is_inline_block():
    """vendor 的 `.btn` 必须是 inline-block —— 高度模型「两颗按钮并排」的依据。

    只在 vendor 里验，不写死：Bootstrap 哪天把 `.btn` 改成 `display:block`，
    #createBoundsEntries 里那两颗入口就各占一行，模型按一行算会**低估**一整行。
    那时这里响亮失败，而不是静默算错。style.css 侧没有覆盖 `.btn` 的 display
    （只有 `.btn.btn-icon` / `.btn.path-browse` 那组把它换成 inline-flex，
    仍是行内级），所以 vendor 这一条就是终值。
    """
    with open(_BOOTSTRAP_MIN_CSS, encoding='utf-8') as f:
        src = f.read()
    assert re.search(r'\.btn\{[^}]*display:\s*inline-block', src), (
        'vendor bootstrap.min.css 里 `.btn` 不再是 display:inline-block —— '
        '两颗按钮还并不并排说不准了，本模型已失效（不是通过）'
    )


def _bootstrap_form_check_metrics():
    """vendor `.form-check` 的 (min-height, margin-bottom)（px）。

    勾选行的高度由 Bootstrap 的 `min-height:1.5rem`(24px) 决定，**不是**由本站
    放大到 1.25rem(20px) 的 .form-check-input 决定（20 < 24 顶不动）。实测：
    一行勾选 24px、行下 2px；两个 .form-check-inline 并排那层 div 实测 26 = 24+2。
    """
    with open(_BOOTSTRAP_MIN_CSS, encoding='utf-8') as f:
        src = f.read()
    m = re.search(r'\.form-check\{([^}]*)\}', src)
    assert m, 'vendor bootstrap.min.css 里找不到 .form-check —— 本模型已失效'
    out = []
    for prop, label in (('min-height', '勾选行最小高度'), ('margin-bottom', '勾选行下外边距')):
        mm = re.search(re.escape(prop) + r':([\d.]+rem|[\d.]+px)', m.group(1))
        assert mm, f'vendor .form-check 里读不出 {label} —— 本模型已失效'
        raw = mm.group(1)
        out.append(float(raw[:-3]) * 16 if raw.endswith('rem') else float(raw[:-2]))
    return tuple(out)


# ---- 来自 Bootstrap、本文件不控制的几个数 --------------------------------
# 每一条都标了 CDP 实测值。它们是模型里唯一「不是从 style.css 解析出来」的输入，
# 所以单独列出来：Bootstrap 大版本升级时这几个数要重测。
# `test_bootstrap_build_is_new_enough_to_have_dark_theme` 已经钉住 >= 5.3。

# Bootstrap 的 --bs-body-line-height。用于所有没有显式声明 line-height 的文字
# （.form-label / .form-group-label / .bounds-grid / .btn）。实测：14px 字 -> 21px 行高。
BS_BODY_LINE_HEIGHT = 1.5
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


def _effective_button_height(css, ctx=None):
    """模拟层叠，算出一颗按钮的**最终**外框高度（默认 `#createTaskBtn`）。

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

    `ctx` 给别的按钮上下文用：首页表单里除了提交按钮还有 `#outputPathBrowse`
    （`.btn.btn-outline-primary.path-browse`），它被 `.btn.path-browse
    { height: var(--ctl-h) }` 钉在 28px，与提交按钮的高度不是一个数。
    高度模型必须按各自的类去层叠，不能拿提交按钮的高度当所有按钮的高度。

    2026-08-15（Task 4）**盲区已补**：纵向内边距现在由 `_btn_decls` 把
    `padding` / `padding-block` / `padding-inline` 三个简写按位展开成长写、
    并把 `padding-block-start` 那四条逻辑长写映到物理长写之后，统一走
    `(important, 特异度, 规则序号, 声明序号)` 那把键决出胜负 —— 改前
    `padding-block` 在模型里完全不存在（静默回落 Bootstrap 的 6px），
    四值 `padding` 的下内边距也取错位。详见 `_PADDING_SHORTHANDS` /
    `_PADDING_LOGICAL_LONGHANDS` 上方的登记与
    tests/test_button_geometry.py::test_button_height_model_sees_logical_padding。

    同轮（收尾）**只读长写**：本函数不再向 `_btn_computed` 要 `padding` 简写，
    也不再对它取 `raw.split()[0]`。那段取第 0 位当**纵向**内边距的代码是
    Task 4 首版宣称已修的错位本身，展开之后它变成死代码，而且只是**碰巧**
    安全 —— 靠 Python「默认实参急求值」这个语言细节：
    `px('padding-top', default=px('padding', ...))` 里内层那句无条件先跑，
    切不开时当场炸。谁把它「整理」成惰性的 `px('padding-top') or px('padding')`，
    静默就回来了（实测：`.btn { padding: calc(1px + 2px) 4px }` 的计算表里
    `padding-top` 是**过期**的 `var(--space-1)`(4px)，浏览器是 3px）。
    删掉它是安全的，因为另外两处同轮改动保证了「简写在场则长写必在场」：
    `_split_padding` 切不开时毒化每一条边（不再返回 None），
    `_btn_decls` 末尾的安全网拦住任何模型不认得的 `padding-*` 名字。
    三处是一个设计，不是三个补丁。
    """
    if ctx is None:
        ctx = _BtnCtx({'btn', 'btn-primary', 'w-100'}, element_id='createTaskBtn',
                      label='#createTaskBtn（高度模型）')
    got = _btn_computed(css, ctx, 'base', {
        'padding-top', 'padding-bottom', 'height', 'min-height',
        'max-height', 'line-height', 'font-size', 'border', 'border-width',
        'border-top-width', 'border-bottom-width', 'border-style',
    })

    def px(name, default=None, required=False):
        if name not in got:
            assert not required, f'`.btn` 没有任何规则声明 {name} —— 高度模型算不出来，测试已失效'
            return default
        raw = got[name][0]
        # 这里**没有** padding 简写的分支：简写由 `_btn_decls` 展开（切不开就
        # 毒化），所以简写在场时长写必在场，取分量这件事不该在本函数里再做一遍。
        v = _resolve_length_px(css, raw)
        assert v is not None, (
            f'{name} 的胜出值来自 `{got[name][1]}` 的 {raw!r}，不是 px/rem/var(px) —— '
            '高度模型解析不了，测试已失效（不是通过）'
        )
        return v

    font_size = px('font-size', required=True)
    # line-height 可以是无单位倍数（CSS 规范里那才是推荐写法）。
    # `.btn.path-browse { line-height: 1 }` 就是 —— 第一版只认 px/rem，
    # 碰到它直接报「测试已失效」，等于把 #outputPathBrowse 这颗按钮排除在
    # 高度模型之外。倍数按 font-size 折算，与浏览器一致。
    line_h = None
    if 'line-height' in got:
        raw_lh = got['line-height'][0].strip()
        if re.fullmatch(r'[\d.]+', raw_lh):
            line_h = font_size * float(raw_lh)
        else:
            line_h = px('line-height')
    if line_h is None:
        line_h = font_size * BS_BODY_LINE_HEIGHT

    # 纵向内边距：只读长写（简写已由 `_btn_decls` 展开成长写），
    # 一条都没有才回落到 Bootstrap 默认。
    pad_t = px('padding-top', default=BS_BTN_PADDING_Y_PX)
    pad_b = px('padding-bottom', default=BS_BTN_PADDING_Y_PX)

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

class _FormNode:
    """`#taskForm` 子树里的一个元素：标签、class 集合、id、可见性、子元素。"""

    __slots__ = ('tag', 'classes', 'el_id', 'invisible', 'kids')

    def __init__(self, tag, classes, el_id, invisible):
        self.tag = tag
        self.classes = classes
        self.el_id = el_id
        self.invisible = invisible
        self.kids = []

    def desc(self):
        bits = f'<{self.tag}'
        if self.el_id:
            bits += f' id="{self.el_id}"'
        if self.classes:
            bits += f' class="{" ".join(sorted(self.classes))}"'
        return bits + '>'


class _FormStructureParser(HTMLParser):
    """扒出 `#taskForm` 的**整棵**子树（直接子元素 + 它们各自的后代）。

    为什么从模板解析而不是把「6 个字段组 + 3 个分组标题」写死：写死的话，
    有人往 index.html 里加一个字段，模型算出来的高度不变，测试全绿而页面
    已经溢出了。

    ⚠️ 为什么记整棵树、而不是只记 `depth == 0` 的那一层（D1）：
       第一版只 append `depth == 0` 的子元素，后代**在解析时就丢了**，于是
       消费方只能把每个 `.mb-3` 一律当成「一个 label + 一个控件」= 57px。
       实测（1366x768 headless Chromium）那是假的：
           #mapStyleField        真实 122px（里面是**两组** label + select）
           输出格式那个 .mb-3    真实 127px（还嵌着一个 .form-check 和一个
                                 .form-text 说明）
       两处合计少算约 135px。这不是算错，是**看不见** —— 信息根本没进过旧
       解析器的输入通道，所以任何变异测试都测不出来。

    ⚠️「不可见」有两种写法，两种都要认（D2）：
           内联 style="display:none"
           裸 hidden 属性             —— #sourceField / #demOptions /
                                        #localTerrainOptions / #contourOptions
       第一版只认前者，于是 #demOptions 这一整组（实测 65px）被算进了默认视图
       的高度；而它的 docstring 还写着「按 display:none 跳过 #demOptions」——
       注释与代码同时错、方向相反。这个 +65 的悲观误差正好抵掉了 D1 的一截
       少算，两个缺陷互相遮掩，模型于是「以 4.5px 余量通过」。本文件
       BS_BTN_PADDING_Y_PX 那段注释里的「两边一起错，差值当然是 0」就是这个形态。

    ⚠️ 2026-08-15 Task 5：锚点从 #downloadForm 换成 #taskForm，被测对象换了但
       解析器一个字没改 —— 四条管线合并后全站只剩这一张表单，而模板里那些
       `hidden` 的字段组现在由 map.js 的 applyPipeline() 按 [data-pipeline] 切换
       （改前是 initDownloadTypeToggle / initProcessTypeToggle 两个函数各切一半）。
       「按管线切换」与「按下载类型切换」对本解析器是同一件事：模板里带 hidden
       的就不占默认视图的高度。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = None
        self.stack = []

    # 模板里带 hidden、但**建模状态下由 JS 放出来**的元素。
    #
    # 1. #tileEstimate —— map.js 的 _paintTileEstimate() 在「瓦片管线 + 已框选」下
    #    `el.hidden = false`，而那正是本模型建的状态（见
    #    _create_panel_form_content_height 的 docstring）。改前它住在 .modal-body 里、
    #    由模型末尾单独加一项，所以模板上的 hidden 与解析器无关；Task 5 之后它是
    #    #taskForm 的子树成员，不在这里开个口子就会被当成不占高度，模型少算一整行。
    #
    # 2. #createBoundsEntries（2026-08-15 工具条瘦身后加入）—— 「去框选 / 手动输入
    #    范围」两颗入口。**这一项以前刻意不在表里**，当时的理由（原注释）是
    #    「updateCreatePanelBounds 只在无选区时放它出来，已框选状态下确实是收起的」。
    #    那条理由随本次改动作废：工具条那颗 #mapDrawRect 删掉之后这里成了重新框选
    #    的唯一入口，map.js 的 updateCreatePanelBounds() 现在写的是
    #    `entries.hidden = !['map', 'dem'].includes(_currentPipeline())`
    #    （static/js/map.js:3315-3319）—— 只跟管线走，有没有选区都显示。建模状态是
    #    瓦片管线，所以它可见。继续把它当不可见就是模型少算两颗按钮那一行。
    #
    # 每加一项都必须能说出「是哪一行 JS 在什么状态下把它放出来」——上面两项各自
    # 指到了具体函数。这张表刻意不写成「凡是 aria-live 的都算可见」之类的规则。
    _VISIBLE_DESPITE_HIDDEN = frozenset({'tileEstimate', 'createBoundsEntries'})

    @staticmethod
    def _invisible(attrs):
        if attrs.get('id') in _FormStructureParser._VISIBLE_DESPITE_HIDDEN:
            return False
        if 'hidden' in attrs:
            return True
        return 'display:none' in (attrs.get('style') or '').replace(' ', '')

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get('id') == 'taskForm':
            assert self.root is None, 'index.html 里出现了两个 #taskForm —— 本测试已失效'
            self.root = _FormNode(tag, set(), 'taskForm', False)
            self.stack = [self.root]
            return
        if not self.stack:
            return
        node = _FormNode(tag, set((a.get('class') or '').split()),
                         a.get('id'), self._invisible(a))
        self.stack[-1].kids.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if not self.stack or tag in _VOID_TAGS:
            return
        self.stack.pop()


def _index_form_children():
    """`#taskForm` 在「地图瓦片 + 已框选」下可见的直接子元素（含各自子树）。

    不可见的块（#sourceField / #demOptions / #localTerrainOptions /
    #contourOptions）在这里就剔掉：它们由 applyPipeline() 按 [data-pipeline]
    切换，默认（瓦片）视图里不占高度。
    """
    parser = _FormStructureParser()
    parser.feed(_template('index.html'))
    assert parser.root is not None, '解析不到 index.html 的 #taskForm —— 本测试已失效'
    assert not parser.stack, (
        'index.html 的 #taskForm 没有正常闭合（解析结束时栈里还剩 '
        f'{[n.desc() for n in parser.stack]}）—— 本测试已失效'
    )
    rows = [k for k in parser.root.kids if not k.invisible]
    assert rows, '解析不出 #taskForm 的子元素 —— 本测试已失效'
    # 模型只在「解析到的确实是那张四管线表单」的前提下成立。
    #
    # 2026-08-15 Task 5：这里**不能再**断言「直接子元素里有 <button>」。
    # 提交钮已经搬出表单、住进 .config-footer（靠 form="taskForm" 关联）——
    # 那正是折叠线缺陷被结构性消掉的原因，见本节顶部那段说明。用它当解析
    # 自检就成了「断言缺陷还在」。
    # 换成断言四条管线的段控在场：它是这张表单里独一无二的标记，解析器
    # 抓错了元素（或哪天有人把段控挪出表单）都会当场响亮失败。
    def _has_pipeline_chip(node):
        if node.tag == 'div' and 'status-chips' in node.classes \
                and node.el_id == 'createPipeline':
            return True
        return any(_has_pipeline_chip(k) for k in node.kids)

    assert any(_has_pipeline_chip(k) for k in rows), (
        '#taskForm 的可见子树里找不到 #createPipeline 段控 —— 解析漏了，'
        '或者四条管线的段控被挪出了表单，本测试已失效'
    )
    return rows


def _create_panel_form_content_height(css):
    """算出 #taskForm 在「地图瓦片 + 已框选」下的**内容栈高**（px）。

    2026-08-15 Task 5 之前这个函数叫 `_index_form_vertical_model`，返回的是
    「1366x768 下 #createTaskBtn 的 bottom」。被测对象变了，不是阈值变了：
    提交钮搬进了 .config-footer（在 .config-scroll 之外），它的 bottom 已经
    与表单内容无关（结构公式见本节顶部）。剩下的这个量 —— 表单内容有多高 ——
    仍然会变、仍然会退化（滚动条越长、首屏能看到的字段越少），所以模型留着，
    只是不再冒充折叠线判据。

    结构从 templates/index.html 解析（**整棵子树**，见 `_FormStructureParser`），
    尺寸从 style.css 解析。只有 BS_BODY_LINE_HEIGHT / BS_BTN_PADDING_Y_PX /
    BS_ALERT_MARGIN_BOTTOM_PX 三个数来自 Bootstrap。**不再读 _modal_metrics**：
    弹窗框架整个不存在了，而 .modal-header / .modal-title / .modal-content 三条
    规则**仍在** style.css 里（#taskDetailModal 与 #pathBrowserModal 还在用，
    tests/test_elevation_glass.py 钉着不许删）—— 也就是说继续读它们不会响亮
    失败，只会安静地给面板凭空加一个它没有的标题栏。这是本文件反复踩过的
    「模型遇到看不懂的东西时回落到一个恰好还在的旧值」，所以必须显式删掉。

    **建模的状态：地图瓦片 + 已框选。** 这个状态的可达性理由变了、结论没变：
    改前 `openDownloadModal()` 在 `!currentBounds` 时直接 return，未框选的弹窗
    根本打不开，所以「已框选」是唯一可达状态；现在 `openCreatePanel()` 没有
    选区也照样打开（缺选区拦在提交那一刻），但瓦片管线是段控的默认选中项、
    也是唯一会放出 #tileEstimate 的那条，所以它仍然是首屏那一屏。
    在这个状态下 map.js 的 `_paintTileEstimate()` 会 `el.hidden = false` 放出
    #tileEstimate、`updateCreatePanelBounds()` 会按管线放出 #createBoundsEntries，
    而 #sourceField / #demOptions / #localTerrainOptions /
    #contourOptions 仍被 `applyPipeline()` 按住 —— 所以模板里的 hidden 属性
    对前两者不算「不可见」、对后四者算。

    2026-08-15（工具条瘦身）：选区那一格的内容整块换了宿主 —— 地图右上的
    `#boundsInfo` 浮层删除，四至读数改由 `updateBoundsInfo()` 渲进面板里的
    `.bounds-readout#createBoundsReadout`，而面板里那句只读摘要
    （`.modal-bounds-summary#createPanelBounds`）连同 CSS 规则一起退役。
    模型里对应的一项跟着换：叶子从 `.modal-bounds-summary`（一行等宽文字 +
    内边距 + 边框）变成 `.bounds-readout`（`.bounds-grid` 那 2 行网格 +
    `.bounds-actions` 那一行）。**判据没变**：仍是「这一格在已框选状态下占多高」。

    **模型是实际值的下界，不是等值。** 所有文本块按**一行**计：一段 i18n 文案
    在给定宽度下折几行需要字体度量，CSS + 模板算不出来。方向是安全的：模型
    永远 <= 实际。

    相邻块级元素的**外边距合并**是模型里最容易漏的一环：
    `.mb-3`(8px) 后面跟 `.form-group-label`(margin-top 16px) 时，两者合并成
    max(8,16)=16 而不是 24。
    """
    def rule_body(selector):
        bodies = [b for sel, b, ctx in _rules_ctx(css) if sel == selector and not ctx]
        assert len(bodies) <= 1, (
            f'期望至多 1 条 `{selector}` 规则，实际 {len(bodies)} 条 —— 本模型已失效'
        )
        return bodies[0] if bodies else None

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

    def own_len(classes, prop):
        """这些 class 里在 style.css 顶层声明的 prop（px）；取最后命中的一条。"""
        got = None
        for c in sorted(classes):
            body = rule_body('.' + c)
            if body is None:
                continue
            raw = _decl_map(body).get(prop)
            if raw is None:
                continue
            v = _resolve_length_px(css, raw)
            assert v is not None, f'`.{c}` 的 {prop} = {raw!r}，解析不了 —— 本模型已失效'
            got = v
        return got

    # ---- 尺寸来源，全部解析出来 -------------------------------------------
    ctl_h = _effective_form_control_height(css)

    label_font = rule_px('.form-label', 'font-size')
    label_line = label_font * BS_BODY_LINE_HEIGHT
    label_mb = rule_px('.form-label', 'margin-bottom')

    gl_font = rule_px('.form-group-label', 'font-size')
    gl_line = gl_font * BS_BODY_LINE_HEIGHT
    gl_pad_b = rule_px('.form-group-label', 'padding-bottom')
    gl_border = border_px('.form-group-label', 'border-bottom')
    gl_mt, gl_mb = margin_parts('.form-group-label')
    gl_h = gl_line + gl_pad_b + gl_border

    text_font = rule_px('.form-text', 'font-size')
    text_line = text_font * BS_BODY_LINE_HEIGHT
    text_mt = rule_px('.form-text', 'margin-top')

    alert_pad = rule_px('.alert', 'padding-top')
    alert_border = border_px('.alert', 'border')
    grid_font = rule_px('.bounds-grid', 'font-size')
    grid_line = grid_font * BS_BODY_LINE_HEIGHT
    grid_row_gap = margin_parts('.bounds-grid', 'gap')[0]
    grid_h = 2 * grid_line + grid_row_gap          # 恰好 2 行，见下一节的断言
    alert_h = 2 * alert_pad + 2 * alert_border + grid_h

    utils = _bootstrap_utility_margins()
    horizontal_classes = _bootstrap_horizontal_classes()
    _assert_bootstrap_btn_is_inline_block()
    check_h, check_mb = _bootstrap_form_check_metrics()
    check_own = rule_body('.form-check')
    if check_own is not None:
        decls = _decl_map(check_own)
        for prop in ('min-height', 'margin-bottom'):
            if prop not in decls:
                continue
            v = _resolve_length_px(css, decls[prop])
            assert v is not None, (
                f'style.css 的 `.form-check` 把 {prop} 写成 {decls[prop]!r}，'
                '解析不了 —— 本模型已失效'
            )
            if prop == 'min-height':
                check_h = v
            else:
                check_mb = v

    def util_margin(classes, side):
        """`mt-N` / `mb-N` 工具类给出的外边距；没有这类 class 返回 None。

        style.css 覆盖了同名工具类时以 style.css 为准 —— `.mb-3` 正是这种：
        vendor 是 `1rem!important`、style.css 是 `var(--gap-field)!important`，
        同特异度同 !important、style.css 在后所以赢（8px 而不是 16px）。
        """
        prefix = 'mt-' if side == 'top' else 'mb-'
        got = None
        for cls in sorted(classes):
            if not cls.startswith(prefix):
                continue
            own = rule_body('.' + cls)
            raw = _decl_map(own).get('margin-' + side) if own is not None else None
            if raw is not None:
                v = _resolve_length_px(css, raw)
                assert v is not None, (
                    f'style.css 的 `.{cls}` 把 margin-{side} 写成 {raw!r}，'
                    '解析不了 —— 本模型已失效'
                )
                got = v
                continue
            assert cls in utils, (
                f'模型不认识间距工具类 `.{cls}`（vendor 与 style.css 都没有）'
                ' —— 本模型已失效（不是通过）'
            )
            got = utils[cls]
        return got

    def is_horizontal(node, kids, where):
        """这个容器把子元素横排（各自不独占一行）吗？

        四种来源：
          1. style.css 给它某个 class 声明了 display:flex/inline-flex（.map-style-row）；
          2. Bootstrap 的 flex 类（.d-flex / .row —— 从 vendor 解析，见
             `_bootstrap_horizontal_classes`，不写死）；
          3. 子元素全是 .form-check-inline —— inline-block 流，也挤在一行里；
          4. 子元素全是 `.btn` —— 同样是 inline-block 流（vendor 的
             `.btn{…display:inline-block…}`，由 `_bootstrap_btn_is_inline_block`
             从 vendor 现读现验，不写死）。2026-08-15 加：#createBoundsEntries
             里「去框选」「手动输入范围」两颗按钮就是这个形态，模板注释也写着
             「.btn 本身是 inline-block，两颗自然并排」。按竖排累加会**高估**，
             而本模型的方向必须是下界（见函数 docstring）。
        混排（既有 inline-block 子元素又有块级子元素）响亮失败：那种形态折几行
        取决于容器宽度，模型算不准，宁可报失效也不猜。
        """
        for c in sorted(node.classes):
            body = rule_body('.' + c)
            if body is not None and _decl_map(body).get('display') in ('flex', 'inline-flex'):
                return True
        if node.classes & horizontal_classes:
            return True
        inline = [k for k in kids
                  if 'form-check-inline' in k.classes or 'btn' in k.classes]
        if inline and len(inline) != len(kids):
            raise AssertionError(
                f'{where}：inline-block 的子元素（.form-check-inline / .btn）'
                '与块级子元素混排 —— '
                '折几行取决于容器宽度，本模型已失效（不是通过）'
            )
        return bool(inline)

    def measure(node, path):
        """(高度, 上外边距, 下外边距)。认不出来的形态一律抛「本模型已失效」。"""
        cls = node.classes
        where = ' > '.join(path + [node.desc()])
        mt = util_margin(cls, 'top')
        mb = util_margin(cls, 'bottom')

        def out(h, def_mt=0.0, def_mb=0.0):
            return h, def_mt if mt is None else mt, def_mb if mb is None else mb

        if 'form-group-label' in cls:
            return out(gl_h, gl_mt, gl_mb)
        if 'form-label' in cls:
            return out(label_line, 0.0, label_mb)
        if 'form-text' in cls:
            return out(text_line, text_mt, 0.0)
        if 'form-check' in cls:
            # 叶子：勾选行的高度是 .form-check 的 min-height（24px），不看里面的
            # input / label —— 实测 1.25rem(20px) 的 .form-check-input 顶不动它。
            return out(check_h, 0.0, check_mb)
        if 'form-control' in cls or 'form-select' in cls:
            return out(ctl_h)
        if 'alert' in cls:
            return out(alert_h, 0.0, BS_ALERT_MARGIN_BOTTOM_PX)
        if 'bounds-readout' in cls:
            # 选区四至读数（#createBoundsReadout）。模板里是个**空 div**，内容由
            # map.js 的 updateBoundsInfo() 在已框选状态下全量重建，所以必须进
            # 叶子表 —— 否则走到下面那句 `assert kids` 会因为「空 div 没有子元素」
            # 报「本模型已失效」。
            #
            # 2026-08-15 换的就是这一项：改前这里是 `.modal-bounds-summary`
            # （#createPanelBounds，那句只读四至摘要 —— 一行等宽文字 + 内边距 +
            # 边框）。那句话连同 CSS 规则与 i18n 键一起退役，因为可编辑的读数
            # 搬进同一格之后它是同一个数字的第二处渲染。守的东西没变：
            # 「选区那一格在已框选状态下占多高」。
            return out(readout_h, 0.0, readout_mb)
        if 'tile-estimate' in cls:
            # 瓦片数预估（#tileEstimate）。同上，改前是末尾单独一项。
            return out(est_line, 0.0, est_mb)
        if node.tag == 'button':
            return out(_effective_button_height(
                css, _BtnCtx(set(cls), element_id=node.el_id, label=where)))
        if node.tag == 'img':
            h = own_len(cls, 'height')
            assert h is not None, (
                f'{where}：图片没有从 style.css 拿到 height，所在行的高度算不出来 '
                '—— 本模型已失效（不是通过）'
            )
            return out(h)

        kids = [k for k in node.kids if not k.invisible]
        assert kids, (
            f'{where}：既不是模型认识的叶子，又没有可见子元素 —— '
            '本模型已失效（不是通过）。请把它的高度加进 _create_panel_form_content_height'
        )
        measured = [measure(k, path + [node.desc()]) for k in kids]
        if is_horizontal(node, kids, where):
            # 横排：行高 = 最高那个子元素的**外边距盒**。实测三处都对得上：
            #   .row            65 = 57 + 8（.col-6.mb-3 的下外边距算在行里）
            #   .map-style-row  28 = max(select 28, 预览图 28)
            #   两个 .form-check-inline 那层 div  26 = 24 + 2
            return out(max(kh + kmt + kmb for kh, kmt, kmb in measured))
        h = sum(kh for kh, _t, _b in measured)
        for prev, nxt in zip(measured, measured[1:]):
            h += max(prev[2], nxt[1])          # 相邻外边距合并取较大者
        return out(h)

    # 两个叶子的尺寸。文本块按**一行**计（见 docstring 里的下界说明）。
    # 放在这里而不是函数开头：measure() 是闭包，名字在**调用时**才解析，而
    # 调用发生在下一行 —— 与 gl_h / label_line 那批放在前面纯粹是读起来顺。
    #
    # `.bounds-readout` 的高度 = updateBoundsInfo() 在已框选分支里渲出来的两块：
    #   `.bounds-grid`    恰好 2 行的四至网格（grid_h，与下一节那条 2 行断言同源）
    #   `.bounds-actions` 网格下方独立一行：上外边距 + 上内边距 + 上边框 +
    #                     max(「清除选区」钮, .bounds-hint 一行文字)
    # 「清除选区」钮按它的真实上下文层叠：`.bounds-actions .btn` 那条把它钉在
    # var(--ctl-h)，不带祖先量出来的是 `.btn` 基几何，两者不是一个数。
    # `.bounds-region`（导入区域时多出的那一行）不计：建模状态是鼠标框选，
    # 那时 _regionSpec 为 null，模型是下界，少算的方向是安全的。
    readout_mb = rule_px('.bounds-readout', 'margin-bottom')
    actions_mt = rule_px('.bounds-actions', 'margin-top')
    actions_pad_t = rule_px('.bounds-actions', 'padding-top')
    actions_border = border_px('.bounds-actions', 'border-top')
    hint_line = rule_px('.bounds-hint', 'font-size') * BS_BODY_LINE_HEIGHT
    clear_btn_h = _effective_button_height(css, _BtnCtx(
        {'btn', 'btn-danger', 'btn-sm'}, ancestors={'bounds-actions'},
        element_id='boundsClearBtn',
        label='#boundsClearBtn（.bounds-actions 里的「清除选区」）'))
    readout_h = (grid_h + actions_mt + actions_pad_t + actions_border
                 + max(clear_btn_h, hint_line))

    est_font = rule_px('.tile-estimate', 'font-size')
    est_line = est_font * BS_BODY_LINE_HEIGHT
    est_mb = rule_px('.tile-estimate', 'margin-bottom')

    items = [measure(node, []) for node in _index_form_children()]
    total = sum(h for h, _mt, _mb in items)
    for prev, nxt in zip(items, items[1:]):
        total += max(prev[2], nxt[1])

    # 就这样。**没有框架项可加了**：改前这里要叠 .modal-dialog 上外边距 +
    # modal-content 上边框 + modal-header + modal-body 上内边距，才能得到按钮
    # 的绝对 bottom。面板时代那些量一个都不参与 —— 提交钮的位置由视口高与
    # .config-footer 的下内边距决定（见本节顶部的结构公式），而表单内容的高度
    # 只影响 .config-scroll 里滚不滚。
    return total


class _IdAncestorParser(HTMLParser):
    """记录每个带 id 元素出现时的 id 祖先栈：{元素 id: [祖先 id, ...]}"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parents = {}

    def handle_starttag(self, tag, attrs):
        el_id = dict(attrs).get('id')
        if el_id:
            self.parents.setdefault(el_id, list(self.stack))
        # ⚠️ 2026-08-15 Task 5 修：**带 id 的自闭合标签以前会永久留在栈上**。
        # 改前的写法是「有 id 就 push、是 void 就 return」—— 于是
        # `<input id="taskName">` 之后 'taskName' 再也不出栈，后面每个元素的
        # 祖先链里都多出一串假祖先，而且外层真祖先的 </tag> 会被这些多余的
        # 栈项吃掉。实测 #createTaskBtn 的祖先链被算成
        # [.., 'createPanel', .., 'taskForm', .., 'localTerrainMaxzoom',
        #  'contourInterval', 'contourBackground', ..] —— 里面既有已经闭合的
        # 表单、也有几个纯 input。
        # 旧断言全是 `X in ancestors` 形态，多出来的假祖先不影响结论，所以这个
        # 缺陷一直是绿的；Task 5 需要断言的是 `'taskForm' not in ancestors`
        # （提交钮必须在表单**之外**），假祖先当场把它判红。
        if tag in _VOID_TAGS:
            return
        self.stack.append(el_id)

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS or not self.stack:
            return
        self.stack.pop()


def test_submit_button_lives_in_the_create_panel_footer():
    """提交按钮必须在 #createPanel 的常驻底条里，且**在 #taskForm 之外**。

    这条替代 `test_submit_button_lives_inside_download_modal`（2026-07 弹窗时代），
    而那条替代的是 dock 时代的「高度模型复现 CDP 实测」自检。三代的共同点是
    「真正要钉住的是结构，不是某个像素数」；换的是结构长什么样。

    弹窗时代钉的是「表单与提交钮都在 #downloadModal 里、而 #downloadModal 带
    modal 类」。那个结构本身就是缺陷：提交钮跟着表单流动，1366x768 实测 bottom
    = 901.50（视口 768，折叠线下 133.5px），1366x720 时溢出 91px。

    现在钉四件事，每一件都是「提交钮恒在视口内」这个结论的必要前提：
      1. #taskForm 与 #createTaskBtn 都是 #createPanel 的后代 —— 表单要是被搬回
         常驻容器，用户又得为它让出整块屏幕；
      2. #createTaskBtn **不是** #taskForm 的后代，而是靠 form="taskForm" 关联
         —— 这是它不进滚动区的唯一原因。照抄 _config_content.html 那颗
         `form="configForm"` 的写法（tests/test_config_form_submittable.py 钉着
         同一个形态）；
      3. #createPanel 带 workbench-panel 类 —— 满高定位与滑出过渡的附着点，
         与「任务」「配置」两个面板同构；
      4. **框选完之后有路可走到创建任务**。判据不变，走法本次换了：
         改前钉的是「updateBoundsInfo 的已框选分支里有 #boundsCreateBtn」——
         地图右上那块浮层上的「新建任务」钮。2026-08-15 浮层整块删除，四至读数
         搬进 #createPanel 的选区格，那颗钮成了「面板里指向面板自己的入口」，
         跟着退役。同一条主流程现在是三段，缺一段就断：
           a. 读数确实在面板的选区格里（#createBoundsReadout 在 #selectionField
              -> #taskForm -> #createPanel 里），且 updateBoundsInfo() 渲的就是
              这个宿主 —— 宿主要是被换回地图浮层，读数与提交钮就又分了家；
           b. 面板开着时下一步是底条那颗常驻提交钮（上面第 1、2 条已钉住它
              在面板里、在表单外）；面板关着时下一步是工具条那颗「新建」
              （`.map-panel-btn[data-panel="create"]`）——工具条的框选钮
              #mapDrawRect 删掉之后，它是打开面板的唯一入口；
           c. 选区落定（LEFT_UP）之后 map.js 按面板开没开 pulse 上面两者之一，
              把「下一步点哪」指出来。这段引导改前 pulse 的正是 #boundsCreateBtn。
         另外 map.js 的 openCreatePanel() 会刷新选区那一格并打开面板。
         **不再要求 getOrCreateInstance**：非模态面板没有 Bootstrap 实例，
         它走 panels.js 的 openPanel('create')。
    """
    html = _template('index.html')
    parser = _IdAncestorParser()
    parser.feed(html)
    for el in ('taskForm', 'createTaskBtn'):
        assert 'createPanel' in parser.parents.get(el, []), (
            f'#{el} 不在 #createPanel 里（祖先：{parser.parents.get(el)}）—— '
            '四条管线的表单必须住在这个面板里，不能回到弹窗或常驻面板'
        )
    assert 'taskForm' not in parser.parents.get('createTaskBtn', []), (
        '#createTaskBtn 又变成 #taskForm 的后代了（祖先：'
        f'{parser.parents.get("createTaskBtn")}）—— 它必须留在 .config-footer 里、'
        '靠 form="taskForm" 关联提交。进了表单就等于进了 .config-scroll，'
        '提交按钮重新跟着内容流动，1366x768 下会回到折叠线以下（弹窗时代实测 '
        'bottom 901.50 / 视口 768）'
    )
    assert re.search(r'<button[^>]*\bform="taskForm"[^>]*id="createTaskBtn"', html) \
        or re.search(r'<button[^>]*id="createTaskBtn"[^>]*\bform="taskForm"', html), (
            '#createTaskBtn 没有 form="taskForm" —— 它在表单之外，缺了这个属性'
            '点下去根本不会派发 submit 事件，四条管线全都提交不了'
        )

    m = re.search(r'<section class="([^"]*)" id="createPanel"', html)
    assert m and 'workbench-panel' in m.group(1).split(), (
        '#createPanel 没有 workbench-panel 类 —— 满高定位、滑出过渡与 panels.js '
        '的开关都挂在这个类上'
    )

    # ---- 第 4 条：框选之后走得到创建任务 ---------------------------------
    src = _js('map.js')
    readout_parents = parser.parents.get('createBoundsReadout')
    assert readout_parents is not None, (
        'index.html 里找不到 #createBoundsReadout —— 四至读数没有宿主，'
        '框选完之后用户在面板里看不到自己选了哪儿'
    )
    for anc in ('createPanel', 'taskForm', 'selectionField'):
        assert anc in readout_parents, (
            f'#createBoundsReadout 不在 #{anc} 里（祖先：{readout_parents}）—— '
            '四至读数必须与提交钮同住「新建任务」面板的选区段：它是框选与创建'
            '之间那一段路。搬回地图浮层就又回到「读数在地图上、按钮在面板里」'
        )
    body = _js_function_body(src, 'updateBoundsInfo')
    assert "getElementById('createBoundsReadout')" in body, (
        'updateBoundsInfo() 渲染的宿主不是 #createBoundsReadout —— '
        '模板里那个空 div 就永远是空的，框选完面板里看不到四至'
    )
    assert re.search(
        r'<button[^>]*class="map-panel-btn"[^>]*data-panel="create"', html
    ), (
        '工具条里没有 `.map-panel-btn[data-panel="create"]` —— 面板关着的时候'
        '框选完就没有入口能打开「新建任务」了（工具条那颗框选钮 #mapDrawRect '
        '2026-08-15 已删，这颗是仅剩的入口）'
    )
    # 下面三条查的是**代码**，所以先剥注释：这一段的沿革就写在 map.js 的注释里
    #（「改前 pulse 的是 #boundsCreateBtn，已随浮层删除」），不剥的话两条否定
    # 断言会被那段注释自己判红，而肯定断言会被一句注释满足。
    code = _strip_js_comments(src)
    assert "document.getElementById('createTaskBtn')" in code \
        and '.map-panel-btn[data-panel="create"]' in code, (
        'map.js 里找不到「选区落定后 pulse 下一步按钮」那段 —— 面板开着 pulse '
        '#createTaskBtn、关着 pulse 工具条那颗「新建」。改前 pulse 的是浮层上的 '
        '#boundsCreateBtn，浮层删了这段引导不能跟着一起没'
    )
    assert 'boundsCreateBtn' not in code, (
        'map.js 里又出现了 #boundsCreateBtn —— 那是面板里指向面板自己的入口，'
        '2026-08-15 随浮层退役。要加回「框选后的下一步」请沿用 pulse 那条路'
    )
    assert 'async function openCreatePanel(' in src, \
        'map.js 应定义 async openCreatePanel(pipeline, prefill)'
    open_body = _js_function_body(src, 'openCreatePanel')
    assert 'updateCreatePanelBounds()' in open_body, (
        'openCreatePanel() 必须刷新面板里的四至摘要 —— 面板非模态，'
        '上一次打开之后选区可能已经被拖角点/改数值改过了'
    )
    assert "openPanel('create')" in open_body, (
        "openCreatePanel() 必须经 panels.js 的 openPanel('create') 开面板 —— "
        '非模态面板没有 Bootstrap 实例，另起一套显隐就又是一份显隐机制'
    )
    summary_body = _js_function_body(src, 'updateCreatePanelBounds')
    assert 'createBoundsEntries' in summary_body, (
        'updateCreatePanelBounds() 没有管 #createBoundsEntries 的显隐 —— '
        '「去框选 / 手动输入范围」两颗入口没人放出来。2026-08-15 起这个函数'
        '只剩这一件事：那句只读四至摘要（#createPanelBounds）与键 '
        '`js.map.download.bounds_summary` 一起退役了，读数归 updateBoundsInfo()'
    )
    assert 'createPanelBounds' not in code, (
        'map.js 里又出现了 #createPanelBounds —— 那是同一个四至的第二处渲染'
        '（可编辑的读数已经在同一格里了），2026-08-15 已退役'
    )


def _create_panel_submit_bottom(css, viewport_h):
    """算出提交钮的 bottom（px）。**公式里没有内容项**，这是本节的整个论点。

    #createPanel 是 `position: fixed; top: 0; bottom: 0`（满视口高），里面三层
    flex：.config-layout 是列容器、.config-scroll 吃满剩余高度并 overflow-y:auto、
    .config-footer 是 `flex: 0 0 auto` 贴在底部。#createTaskBtn 在底条里、在
    #taskForm 之外。于是：

        bottom = 视口高 - .config-footer 的下内边距

    中间为什么没有别的项：宿主 .workbench-panel__body 有 12px 内边距，而
    `.workbench-panel__body--fill .config-footer` 的下外边距是 -12px，两者恰好
    抵掉（底条贴边）。这一对是**必须一起验的不变量** —— 只改一头，底条就不再
    贴底，公式随之作废。所以下面五个前提逐条断言，任何一条被破坏都响亮失败，
    而不是让公式安静地算错。

    浏览器实测（headless Chromium，四条管线 × 明暗两主题 × 两个视口 = 16 组）：
        1366x768 -> 756.00 (= 768 - 12)
        1600x900 -> 888.00 (= 900 - 12)
    16 组全部相同，elementFromPoint 命中提交钮本体，.config-scroll 的 scrollTop
    全为 0；其中 map / local_terrain / contour 三条在 768 下表单确实在滚
    （scrollHeight > clientHeight）—— 表单滚而提交钮不动。
    """
    def one_rule(selector):
        bodies = [b for sel, b, ctx in _rules_ctx(css) if sel == selector and not ctx]
        assert len(bodies) == 1, (
            f'期望恰好 1 条 `{selector}` 规则，实际 {len(bodies)} 条 —— 本模型已失效'
        )
        return _decl_map(bodies[0])

    def parts(raw, selector, prop):
        """把 `margin` / `padding` 简写切成有符号的 px 列表。

        为什么不能直接把每一段交给 `_resolve_length_px`：那个函数**不认负号**
        （它服务的是长度与令牌解析，`-12px` 一律返回 None）。而底条「贴边」正是
        靠负外边距实现的，所以这里必须自己剥一次符号 —— 剥完再交给它，令牌与
        rem 换算仍然走同一份实现，不在这里另抄一套。
        """
        vals = []
        for token in _IMPORTANT_RE.sub('', raw).strip().split():
            sign = -1.0 if token.startswith('-') else 1.0
            v = _resolve_length_px(css, token.lstrip('+-'))
            vals.append(None if v is None else sign * v)
        assert all(v is not None for v in vals), (
            f'`{selector}` 的 {prop} = {raw!r}，解析不了 —— 本模型已失效。'
            'tests/test_spacing_scale.py 把这几条登记成「只许字面量」，'
            '写成 calc() 或 var() 会让本模型整个失明'
        )
        return vals

    # 前提 1：面板满视口高。
    panel = one_rule('.workbench-panel')
    assert panel.get('position') == 'fixed', (
        '.workbench-panel 不再是 position: fixed —— 面板不再钉在视口上，'
        '提交钮的 bottom 就不能由视口高推出来了'
    )
    for side in ('top', 'bottom'):
        raw = panel.get(side)
        assert raw is not None and _resolve_length_px(css, raw) == 0, (
            f'.workbench-panel 的 {side} 不是 0（实际 {raw!r}）—— 面板不再满高，'
            '底条也就不再贴在视口底边'
        )

    # 前提 2：滚动发生在 .config-scroll，而不是底条所在的那一层。
    scroll = one_rule('.config-scroll')
    assert scroll.get('overflow-y') == 'auto', (
        '.config-scroll 不再 overflow-y: auto —— 表单要么整层撑高把底条顶出视口，'
        '要么滚动跑到外层去，两种都会把提交钮带走'
    )
    fill = one_rule('.workbench-panel__body--fill')
    assert fill.get('overflow') == 'hidden', (
        '.workbench-panel__body--fill 不再 overflow: hidden —— 两层都能滚，'
        '会出双滚动条，且外层滚动会把底条滚出视口'
    )

    # 前提 3：底条不参与滚动、不被内容压缩。
    footer = one_rule('.config-footer')
    flex = (footer.get('flex') or '').split()
    assert flex[:2] == ['0', '0'], (
        f'.config-footer 的 flex 是 {footer.get("flex")!r}，不是 `0 0 auto` —— '
        'flex-grow/shrink 一旦不为 0，表单一长底条就会被压扁或被推走'
    )

    # 前提 4：底条的负下外边距恰好抵掉宿主的下内边距（贴边）。
    host_pad = parts(one_rule('.workbench-panel__body')['padding'],
                     '.workbench-panel__body', 'padding')
    host_pad_bottom = host_pad[0] if len(host_pad) < 3 else host_pad[2]
    footer_margin = parts(one_rule('.workbench-panel__body--fill .config-footer')['margin'],
                          '.workbench-panel__body--fill .config-footer', 'margin')
    assert len(footer_margin) == 3, (
        '`.workbench-panel__body--fill .config-footer` 的 margin 不是三值简写 —— '
        '本模型按「上 左右 下」读它，写法一变就读错。'
        'tests/test_css_contract.py::test_config_footer_is_a_real_bottom_bar_inside_the_panel '
        '对同一条规则有同样的形状要求'
    )
    assert footer_margin[2] == -host_pad_bottom, (
        f'底条的下外边距 {footer_margin[2]}px 不再等于宿主下内边距 '
        f'{host_pad_bottom}px 的相反数 —— 底条不贴视口底边了，'
        f'提交钮的 bottom 会比视口低 {host_pad_bottom + footer_margin[2]}px'
    )

    # 前提 5：底条自己的下内边距，就是提交钮到视口底边的全部距离。
    footer_pad = parts(footer['padding'], '.config-footer', 'padding')
    footer_pad_bottom = footer_pad[0] if len(footer_pad) < 3 else footer_pad[2]
    return viewport_h - footer_pad_bottom


def test_submit_button_fits_at_1366x768():
    """1366x768 下提交按钮必须无需滚动即可见。**Task 5 起这是恒等式，不是拟合。**

    **这是 A5 / Task 10 的验收标准。** 三代被测对象：
      - dock 时代：常驻面板里按钮的 bottom <= 768。实测 949.34，溢出 181.3px。
      - 弹窗时代：#downloadModal 在 1366x768 下会不会超视口。实测 bottom 901.50，
        折叠线下 133.5px（模型算 865.50，下界，差 36 = 两处文案折行）；
        1366x720 一档实测溢出 91px。这条断言当时是 `xfail(strict=True)`，
        reason 里写着「归属 Task 5」。
      - 现在：提交钮在 .config-footer 里、在滚动区之外，bottom = 视口高 - 底条
        下内边距，**与表单内容无关**。

    所以本条不再是「算一算够不够」，而是「那套结构前提还在不在」：
    `_create_panel_submit_bottom` 逐条断言五个前提（面板满高 / 滚动在
    .config-scroll / 底条 flex:0 0 auto / 负外边距抵掉宿主内边距 / 底条下内边距），
    任何一条被破坏都响亮失败。断言本身对 1366x768 与 1600x900 各跑一遍 ——
    两个视口给出同一个余量，正是「与视口无关」的证据。

    ⚠️ **xfail(strict=True) 已按它自己的要求删除。** 那个标记的 reason 明写
       「Task 5 落地后本条一旦转绿，strict xfail 会失败，强制把这个标记连同这段
       理由一起删掉」。Task 5 落地了，本条转绿了，标记删了，理由搬进了这段
       docstring 与本节顶部那段沿革 —— 数字一个没丢。

    ⚠️ 结构契约（表单与提交钮确实在面板里、且提交钮**不在**表单里）由
       test_submit_button_lives_in_the_create_panel_footer 钉住；表单内容不许
       悄悄长高由 test_create_panel_form_content_does_not_grow_further 钉住。
       三条各管一段，缺一段就会重新出现「模型说装得下、浏览器里装不下」。
    """
    for viewport in (VIEWPORT_1366_HEIGHT_PX, 900):
        got = _create_panel_submit_bottom(_css(), viewport)
        assert got <= viewport, (
            f'视口 {viewport}px 下 #createTaskBtn 的 bottom 模型值 {got:.2f}px > '
            f'{viewport}px —— 提交按钮在折叠线以下 {got - viewport:.2f}px。'
            '这在面板结构下不该可能发生：要么某个前提断言漏了，要么底条的'
            '下内边距变成了负数'
        )
        assert got == viewport - 12, (
            f'视口 {viewport}px 下模型算出 bottom = {got:.2f}px，'
            f'期望 {viewport - 12}px（= 视口 - 底条 12px 下内边距）。'
            '浏览器实测 1366x768 -> 756.00、1600x900 -> 888.00，四条管线'
            '× 明暗两主题共 16 组全部一致。这个等式变了说明底条的几何被改过，'
            '请重新量一遍真实浏览器再改这里的 12'
        )


# 表单内容栈高的棘轮上界。**实测值，不是设计目标。**
#
# 2026-08-15 Task 5 把被测对象换掉了。改前这条棘轮盯的是
# `_index_form_vertical_model(_css())` —— 「#createTaskBtn 的模型 bottom」——
# 上界 866.0（= 动 Task 3 令牌前实测 865.50 + 0.50 余量）。那个量现在**恒等于**
# 「视口高 - 12」，与源码里任何尺寸都无关，继续棘轮它就是棘轮一个常数。
#
# 换成 `_create_panel_form_content_height(_css())`：#taskForm 在瓦片管线下的
# 内容栈高。折叠线不再受它影响（提交钮在滚动区外），但它仍然会退化 ——
# 内容越高，.config-scroll 的滚动条越长、首屏能看到的字段越少，而这是
# 结构模型完全失明的那一半。
#
# 上界 = 2026-08-15 Task 5 落地时实测 719.0 + 0.50 余量。
# 两个数为什么相差那么多（866.0 -> 719.5）：866 里有约 147px 是**弹窗框架**
# （.modal-dialog 上外边距 28 + modal-content 边框 + .modal-header 整条 +
# .modal-body 上内边距 24），面板里那些一个都不存在；表单内容本身没有变小。
# 这不是「腾出了 147px 竖向空间」，是「这 147px 从此不由这条棘轮管」。
#
# 余量为什么只给 0.50：模型的竖向算术全部落在 0.5px 的格子上
# （行高 = 1.5 × 偶数字号），0.50 正好是一个格子 —— 能吸收浮点尾差，
# 又能让任何 >= 1px 的真实增高当场变红。参考灵敏度（每 +1px 令牌值，
# Task 3 时在弹窗模型上量的，同一批消费者）：
# `--space-2` +17.0 / `--font-size-sm` +10.5 / `--font-size-xs` +9.0 /
# `--ctl-h` +5.0 —— 所以这条棘轮不是形式主义。
#
# 2026-08-15（工具条瘦身）719.5 -> 806.5。上界 = 实测 806.0 + 0.50 余量。
# 涨的 87.0px 分两笔，都是**内容真的进了表单**，不是模型放水：
#
#   +47.0 选区那一格换宿主。地图右上的 `#boundsInfo` 浮层整块删除，四至读数
#         搬进面板的 `.bounds-readout#createBoundsReadout`。模型里这一项从
#         `.modal-bounds-summary`（那句只读摘要，一行 12px 等宽 + 上下 8px
#         内边距 + 1px 边框 = 36.0）换成 `.bounds-readout`：
#             .bounds-grid    2 x 18 + 行间距 2      = 38.0
#             .bounds-actions 8(上外边距) + 8(上内边距) + 1(上边框)
#                             + max(清除选区钮 28, .bounds-hint 18) = 45.0
#         合计 83.0，两者外边距都是 12。83 - 36 = 47。
#         这 47px 不是新增的界面元素，是从地图浮层挪进表单的 —— 换句话说
#         **地图上少了 62px 的浮层**（那块的实测值见 style.css `.bounds-grid`
#         那段注释），代价是表单的滚动区长了 47px。这是用户明确选的取舍
#         （设计稿 §3「面板关着的时候怎么办」），不是悄悄长高。
#
#   +40.0 `#createBoundsEntries`（「去框选 / 手动输入范围」两颗按钮 28.0
#         + `.mb-3` 8.0 + 与上一项 #tileEstimate 之间合并后的 12.0 外边距）
#         **从模型的盲区里被捞出来**。它一直在表单里，只是改前
#         updateCreatePanelBounds() 在有选区时把它 hidden 掉、而本模型建的正是
#         已框选状态，所以不占高度；工具条那颗 #mapDrawRect 删掉之后它成了重新
#         框选的唯一入口，现在只跟管线走（map.js:3315-3319）。
#         这一笔严格说是**修了一处少算**，不是新内容 —— 但棘轮记的是模型值，
#         所以照实记在这里，免得下一个人以为是哪次改动把表单撑高了 40px。
_FORM_CONTENT_HEIGHT_RATCHET_PX = 806.5


def test_create_panel_form_content_does_not_grow_further():
    """内容**棘轮**：`#taskForm` 在瓦片管线下的内容栈高不许超过 806.0px。

    ⚠️ 这是棘轮，**不是目标**。它不问「装不装得下」——
    test_submit_button_fits_at_1366x768 已经从结构上保证提交钮永远够得着，
    而表单本身**允许**滚动（1366x768 实测 map / local_terrain / contour 三条
    管线的 .config-scroll 都在滚，提交钮的 bottom 仍然是 756）。

    这条为什么必须存在：结构断言对「内容长高」是完全失明的 —— 往表单里塞
    200px 字段，那五个前提一条不破，全套测试一条不红，而用户首屏能看到的
    字段少了一半、每次建任务都要多滚一屏。改前那条棘轮盯的是按钮 bottom，
    按钮 bottom 现在是常数，所以棘轮必须跟着搬到唯一还会变的那个量上。

    棘轮规则（与 test_important_count_under_control 的账法同一套）：

      **改小了**（内容变矮）：把 `_FORM_CONTENT_HEIGHT_RATCHET_PX` 降到
      「新实测值 + 0.50」，并在这里登记降了多少、靠什么降的。不降的话，
      腾出来的竖向空间会被后面的任务悄悄吃回去。

      **确实需要更多字段**：允许抬高上界，但必须在本 docstring 里写清
      「加了什么、新实测多少、为什么这些字段是必要的」。模型是下界（所有文本块
      按一行计），所以真实高度只会更高。抬升本身不是失败，**不留痕地抬升**才是。

    历史：
      866.0 —— 2026-08-15 Task 3 前，被测对象是「弹窗时代按钮的模型 bottom」。
      719.5 —— 2026-08-15 Task 5，被测对象换成「表单内容栈高」（实测 719.0）。
               差额约 147px 全是退场的弹窗框架，不是表单变矮了。
      806.5 —— 2026-08-15 工具条瘦身（实测 806.0）。+87.0 = 选区那一格换宿主
               +47.0（地图右上的 #boundsInfo 浮层整块搬进面板：一行只读摘要
               36.0 换成「2 行四至网格 + 操作行」83.0）与
               #createBoundsEntries +40.0（那两颗入口改成只跟管线走、
               已框选状态下也显示，从模型的盲区里被捞出来）。
               逐项账在 `_FORM_CONTENT_HEIGHT_RATCHET_PX` 上方。
               **这一笔不是「加了新字段」**：47 是从地图浮层挪进来的同一块内容
               （地图那边同时少了一块 62px 的浮层），40 是修了一处少算。
    """
    got = _create_panel_form_content_height(_css())
    assert got <= _FORM_CONTENT_HEIGHT_RATCHET_PX, (
        f'#taskForm 的内容栈高从 806.0px 涨到 {got:.2f}px'
        f'（超上界 {got - _FORM_CONTENT_HEIGHT_RATCHET_PX:.2f}px）——'
        '这是**棘轮，不是目标**：提交钮够不够得着由 '
        'test_submit_button_fits_at_1366x768 从结构上保证，本条只管「表单不许'
        '悄悄长高」。若你确实需要更多字段，请在本条 docstring 里写明加了什么、'
        '重新量一遍、再把上界改成新实测值 + 0.50，不要不留痕地把数字往上顶。'
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
    （`static/js/map.js` 里出现在字符串/注释中的 `http://` 就是这个形态）。
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
# 6 -> 4（等高线改上传驱动）：等高线提交 payload 的 bbox 和 coverageBounds()
# 的返回值随旧下载式 submitContour 一并删除；等高线不再有 bbox。
# 4 -> 4（Cesium 换地图）：Leaflet 的两个 getter 构造点变成 Cesium 的
# _rectDegrees（拖拽中按度直写）+ currentBounds（LEFT_UP 落定）两个构造点。
# 4 -> 7（2026-07 框选可调整）：新增 3 处，全部是选区调整链上的构造点——
#   _syncBoundsFromRect() 的 currentBounds（拖角点 / 数值编辑后同步快照）、
#   _applyBoundsEdit() 的中间变量 b 与重建的 _rectDegrees（点击编辑提交）。
#   逐键核对过：全部同名方位引用，检查通过才把计数调上来。
# 7 -> 10（2026-08 等高线预览带 bbox 定位）：等高线预览面板补上 flyTo——
#   registerCompletedContourTask / initContourPreview 两处 contourPreviewTasks
#   条目（值取自 /api/contour/tasks 行的同名字段）、toggleContourPreview 传给
#   previewTask 的条件字面量（info.north 等同名引用）。
# 10 -> 11（i18n 改造）：updateBoundsInfo 的四至读数改成
#   `t('js.map.bounds.readout', {north: f(currentBounds.north), …})`，
#   传给 t() 的参数对象本身就是一个 bbox 字面量。它同样受本条方位配对检查
#   保护（键名与取值必须同名），所以是「多了一个被查到的构造点」，不是漏检。
# 11 -> 12（D1 选区播报解耦）：#boundsInfo 不再是 live region（拖角点时整层每帧
#   重建，polite 队列会积压到松手之后还在念），播报改由 announceBounds() 在
#   LEFT_UP 与 _applyBoundsEdit 校验通过后各写一次。它传给
#   `t('js.map.bounds.announce', {north: f(currentBounds.north), …})` 的参数对象
#   又是一个 bbox 字面量，同样受本条方位配对检查保护 —— 多了一个被查到的构造点。
# 12 -> 13（B4 手动输入范围）：空态浮层新增的键盘可达入口，落定时
#   `_applyManualBounds()` 用校验后的四至重建 `_rectDegrees` —— 与鼠标框选
#   同一套写入路径，所以也是一个受本条方位配对检查保护的构造点。
# 13 -> 16（§5.1 区域导入与地名搜索）：三个新构造点，每一个都受本条方位配对
#   检查保护 —— 这正是它们该被数进来的理由，不是漏检：
#     · `applyImportedRegion()`：把服务端 RegionSpec 的 bbox 落成 currentBounds
#       （`{ north: north, south: south, ... }`，四个同名局部变量来自
#       `const [west, south, east, north] = region.bbox`，解构顺序错一位就是
#       整个选区错位，而这条断言正好钉住键与值同名）；
#     · `applyPlaceResult()` 传给 validateBoundsRules 的四至对象 —— 地名搜索
#       给的 bbox 要过与框选、点读数编辑、手动输入同一道闸门；
#     · `applyPlaceResult()` 重建的 `_rectDegrees` —— 与 _applyManualBounds
#       同一套写入路径。
# 18 -> 17（2026-08-15 工具条瘦身）：**少了一个，而且必须先确认少的是哪一个**。
#   退役的是 `updateCreatePanelBounds()` 里那句只读四至摘要的参数对象
#   （`t('js.map.download.bounds_summary', {north: f(currentBounds.north), …,
#   width: w, height: h})`）。四至读数搬进同一格之后那句话是同一个数字的第二处
#   渲染，连键 `js.map.download.bounds_summary` 一起退役。
#   核对方法（不是拍脑袋）：拿 `git show HEAD:static/js/map.js` 与工作树版本各跑
#   一遍 `_bbox_object_literals`，把两份命中清单做差 —— 只在旧版出现的恰好一条，
#   就是剥注释后第 3125 行那个字面量；只在新版出现的零条。**其余 17 个构造点
#   一个没少**，所以这是「一处渲染退役」，不是扫描失效。
MAP_JS_BBOX_LITERAL_COUNT = 17


def test_bbox_literals_never_swap_directions():
    """map.js 里每个 bbox 字面量的每个方位键，值必须引用**同名**方位。

    覆盖整条数据链上的全部 4 处：
      - `_rectDegrees = { west: ..., east: ..., ... }`          Cesium 拖拽中按度直写
      - `currentBounds = { north: _rectDegrees.north, ... }`    LEFT_UP 落定时构造
      - `taskData = { north: currentBounds.north, ... }`        x2，提交给后端的 bbox
      （等高线改上传驱动后不再有 bbox payload。）

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
#   （见下面那组常量）。原理由是「把 CDN 上的 bootstrap.min.css 拉进单测会
#   引入网络依赖」——**vendor 本地化之后这个理由已经不成立**：
#   static/vendor/bootstrap/5.3.0/bootstrap.min.css 就在仓库里，离线可读。
#   保持现状是**权衡**不是限制：解析完整的 Bootstrap 需要一个真 CSS 解析器，
#   而特异度这一层已经够用 —— 本站的 style.css 排在最后
#   （test_no_stylesheet_can_load_after_style_css 钉住），同特异度即取胜。
#   将来谁想把手抄的特异度常量换成实读本地文件，路已经通了。
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
            # 属性选择器（`:not([hidden])`）记进 neg_attrs，**不在这里**判 None ——
            # 这里是解析期，还不知道这个 compound 会不会真的命中按钮。一律判 None 的
            # 话，subject 明显不是按钮的选择器（style.css 的
            # `.workbench-statusbar:has(#statusBaseUnpack:not([hidden])) .statusbar-tasks`）
            # 也会把整个按钮模型顶成「已失效」—— 那是误报，不是保护。
            # 真正可能命中时仍然响亮判 None，见下面 subject / 祖先两处的 neg_attrs 检查。
            # 这与本函数「先判肯定不命中，再判形态不支持」的既定顺序是同一条原则。
            if not re.fullmatch(r'\s*(?:[.:][-\w]+|\[[^\]]*\])\s*', arg):
                return None                   # :not() 里是别的东西
        neg_pseudos = set(re.findall(r':not\(\s*:([-\w]+)\s*\)', part))
        neg_classes = set(re.findall(r':not\(\s*\.([-\w]+)\s*\)', part))
        neg_attrs = re.findall(r':not\(\s*(\[[^\]]*\])\s*\)', part)
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
                              attrs=attrs, neg_attrs=neg_attrs))
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
    # `:root` 只可能命中 <html>，而 <html> 永远不是按钮 —— 属于「确定不命中」，
    # 必须归第一步。落到第二步会因为 root 不在 _BTN_SUPPORTED_PSEUDOS 里被判成
    # 「模型不支持」，把整个模型顶成失效（vendor 本地化删掉 style.css 顶部那句
    # @import、`:root` 头一次进入扫描范围时，实测 10 条断言就是这么红的）。
    if 'root' in subject['pseudos']:
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
    if subject['neg_attrs']:
        return None                           # 例如 .btn:not([disabled])，模型不认，响亮失败
    for anc in compounds[:-1]:
        if (anc['pseudos'] or anc['neg_pseudos'] or anc['ids']
                or anc['tag'] is not None or anc['neg_attrs']):
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


# padding 的三个简写 -> 长写。**这是 2026-08-15 台账 4 号缺口的修复**（Task 4）。
#
# 修之前：`_btn_decls` 只展开 outline / border，`_effective_button_height` 只向
# `_btn_computed` 要 `padding` / `padding-top` / `padding-bottom`，且对 `padding`
# 简写取 `value.split()[0]` 当纵向内边距。三个后果，全部实测：
#   1. 一条 `.btn { padding-block: 20px }` 在模型眼里**不存在** —— 模型静默回落
#      到 Bootstrap 的 6px，算出 34.5px 而浏览器渲染 62.5px，每条断言都是绿的。
#      Task 3 因此被迫在 `.config-footer .btn` 上写两条 `padding-top/-bottom` 长写，
#      并在 style.css 里留注释解释「为什么不能用 padding-block」——那段注释描述的
#      是模型的缺陷，不是 CSS 的偏好。
#   2. 四值 `padding: 1px 2px 3px 4px` 的下内边距被当成 1px（取了第 0 位）。
#   3. 「先简写、后长写」跨规则的层叠算不对：`.btn { padding-top: 4px }` 之后
#      `.btn-sm { padding: 8px 12px }`（同特异度、源序在后）在浏览器里是 8px，
#      而旧模型无条件优先用长写，答 4px。
# 展开之后这三条都由 `(important, 特异度, 规则序号, 声明序号)` 这把统一的键决出，
# 与 outline / border 同一条路。正面防线：
# tests/test_button_geometry.py::test_button_height_model_sees_logical_padding。
#
# --------------------------------------------------------------------------
# 2026-08-15（收尾轮）**上面那次修复自己留了三条静默通道，一并记账**
# --------------------------------------------------------------------------
# 三条是同一个失效模式：模型遇到看不懂的东西时没有响亮失败，而是回落到一个
# 恰好还在的旧值。数字全部是模型侧实测，探针是 `.btn.btn-primary.w-100#createTaskBtn`
# **不带任何祖先**的那个上下文（量 `.btn` 的基几何），基线 28.0 =
# clamp(4+4+17, min-height 28)，17 = 行高 + 2x边框。这个 28.0 不是页面上那颗按钮的
# 高度 —— 它真实住在 `#createPanel` 的 `.config-footer` 里，`.config-footer .btn`
# 的 `min-height: var(--ctl-h-lg)` 胜出，模型与浏览器都是 36.0。探针不带祖先是有意的，
# 理由记在 tests/test_button_geometry.py 第 5 节的注释里。
#
#   P. `_split_padding` 切不开时返回 None，`_btn_decls` 于是只发简写、不发长写。
#      那时 `_PADDING_ONE_VALUE_RE` 上方的注释宣称「只留简写本身 —— 调用方拿它去
#      `_resolve_length_px` 会响亮失败」。**那句保证是假的**：它只在「没有更早的
#      长写可以回落」这一种形态下成立，而 `.btn` 自己写的就是
#      `padding: var(--space-1) var(--space-3)`，模型把它展成了四条长写 ——
#      「更早的长写」不需要有人手写，天然就在那里。实测：
#        padding: calc(1px + 2px)              -> 响亮（保证唯一成立的形态）
#        padding: 2px 4px 30px calc(1px + 2px) -> **静默 28.0**，正确 49.0
#        padding-block: calc(1px + 2px)        -> **静默 28.0**，正确 23.0
#        padding-inline: calc(1px + 2px)       -> **静默**（纵向不动，左右读旧值）
#      修法：切不开就把原值毒化到每一条边上，见 `_split_padding`。
#   H. 四条逻辑长写 `padding-{block,inline}-{start,end}` 不在任何表和任何 `props`
#      集合里，被 `_btn_computed` 的 `d[0] in props` 无声丢掉。实测
#      `padding-block-start: 20px` -> **静默 28.0**，正确 41.0；
#      `padding-inline-start: 20px` -> **静默 28.0**。
#      修法：`_PADDING_LOGICAL_LONGHANDS` 别名表 + `_btn_decls` 末尾的安全网
#      （关的是整条属性名轴，不是补今天这四个名字）。
#   M. 上面第 2 条宣称修掉的错位回落**代码还在**：`_effective_button_height` 仍
#      向模型要 `padding` 简写并取 `raw.split()[0]`。它不产生错数只是因为展开总会
#      同时给出上下两条长写，属于碰巧安全 —— 实测
#      `.btn { padding: calc(1px + 2px) 4px }` 的计算表里 `padding-top` 是**过期**的
#      `var(--space-1)`(4px)，浏览器 3px，唯一把它变成失败的是 Python 默认实参的
#      急求值。修法：只读长写，删掉简写分支。
#
# 三处必须同轮落地：删掉 M 的前提是「简写在场则长写必在场」，而那正是 P（毒化）
# 与 H（安全网）给出的保证。正面防线（三条，与上面 P/H/M 一一对应）：
# tests/test_button_geometry.py::
#   test_unsplittable_padding_shorthand_fails_loudly_instead_of_losing_silently
#   test_btn_decls_understands_every_padding_property_name
#   test_button_height_model_uses_the_bottom_value_of_a_four_value_padding
_PADDING_SHORTHANDS = {
    'padding': ('padding-top', 'padding-right', 'padding-bottom', 'padding-left'),
    'padding-block': ('padding-top', 'padding-bottom'),
    'padding-inline': ('padding-left', 'padding-right'),
}
# 四条**逻辑长写** -> 物理长写。按 `horizontal-tb` + `ltr` 展开（inline-start =
# 左），与上面 `_PADDING_SHORTHANDS` 把 `padding-inline` 映到左右两边是同一个
# 前提，不是本轮新引进的假设；站点只有 zh-CN / en 两种横排 LTR 文案。
#
# 2026-08-15 收尾轮补的（Task 4 首版的漏网之鱼）：这四个名字既不在
# `_PADDING_SHORTHANDS`，也不在任何调用方的 `props` 集合里，于是被
# `_btn_computed` 的 `d[0] in props` **无声丢掉**。实测（`#createTaskBtn`，
# 内存变异样式表）：`.btn { padding-block-start: 20px }` 模型答 28.0
# （= 基线，等于这条声明在模型眼里不存在），正确值 41.0 = 20(上) + 4(下)
# + 17(行高 + 2x边框)；`padding-inline-start: 20px` 同样 28.0。
# 这条属性名轴是本文件里最后一个「不响亮」的轴：`_btn_branch_applies` 看不懂
# 选择器会响亮失败，`_btn_media_applies` 算不了条件 at-rule 会响亮失败，
# 只有属性名这一轴在静默丢弃 —— 与 `_btn_computed` docstring 自己那句
# 「任何排在安全网前面的 continue 都是一个静默漏检口」直接矛盾。
# 正面防线：tests/test_button_geometry.py::
# test_btn_decls_understands_every_padding_property_name。
_PADDING_LOGICAL_LONGHANDS = {
    'padding-block-start': 'padding-top',
    'padding-block-end': 'padding-bottom',
    'padding-inline-start': 'padding-left',
    'padding-inline-end': 'padding-right',
}

# `_btn_decls` 认得的全部 `padding-*` 属性名。这就是 CSS 里 padding 属性的**全集**
# （4 物理长写 + 3 简写 + 4 逻辑长写），所以末尾那张安全网不是「今天这几个名字」
# 的白名单，而是整条轴的闸门：拼错的 `padding-botom`、将来新出的 padding 属性，
# 都会在 `_btn_decls` 里当场响亮，而不是安静地不生效。
_PADDING_KNOWN_NAMES = (set(_PADDING_SIDES) | set(_PADDING_SHORTHANDS)
                        | set(_PADDING_LOGICAL_LONGHANDS))

# 一个能当**整体**认下来的 padding 分量。只认两种形态：`var(--x)`（不带回退）
# 与「数字 + 可选单位」（含裸 `0`、`.5rem`、负值、大写单位）。
#
# ⚠️ 这条正则比它上一版注释宣称的严得多，写清楚免得下一个人误判：它拒的**不是**
# 「自身含空格的值」，而是「不是上面那两种形态」的一切 ——
#   · `calc(1px + 2px)` 与不含空格的 `calc(1px+2px)` 都拒（拒的是 calc 本身）
#   · `var(--x, 4px)`（带回退）、`env()`、`min()/max()/clamp()` 都拒
#   · CSS 全局关键字 `inherit / initial / unset / revert` 都拒
#   · `1e2px` 这类科学计数法也拒
# 拒掉之后**必须毒化**，见 `_split_padding` 的 docstring。
_PADDING_ONE_VALUE_RE = re.compile(r'^(?:var\(\s*--[-\w]+\s*\)|-?[\d.]+[a-z%]*)$', re.I)


def _split_padding(name, value):
    """`4px 8px` -> {'padding-top': '4px', 'padding-bottom': '8px'}（按 CSS 补位规则）。

    **切不开时不返回 None，而是把原值原样写到它本该设的每一条边上（毒化）。**
    调用方于是拿着一个解析不了的值去 `_resolve_length_px`，当场响亮失败。

    2026-08-15 收尾轮：这里原本返回 `None`，`_btn_decls` 拿到 None 就只发简写、
    一条长写都不发，注释宣称「调用方拿它去 `_resolve_length_px` 会响亮失败」。
    **那个保证是假的**，只在「全站没有任何更早的长写」这一种形态下成立 ——
    而 style.css 的 `.btn` 自己写的就是 `padding: var(--space-1) var(--space-3)`，
    模型把它展成了四条长写，所以「更早的长写」根本不需要有人手写，天然就在那里。
    实测（探针 = `.btn.btn-primary.w-100#createTaskBtn` **不带祖先**，基线 28.0；
    页面上那颗真实按钮在 `.config-footer` 里是 36.0，不是这个数）：
        padding: calc(1px + 2px)              -> 响亮（唯一保证成立的形态：
                                                 简写取分量时 `calc(1px` 解析不了）
        padding: 2px 4px 30px calc(1px + 2px) -> **静默 28.0**，正确值 49.0
                                                 = 2(上) + 30(下) + 17(行高+边框)
        padding-block: calc(1px + 2px)        -> **静默 28.0**，正确值 23.0
        padding-inline: calc(1px + 2px)       -> **静默**（纵向不动，
                                                 左右两边读到更早的旧值）
    也就是说：Task 4 一边补掉三个旧盲区，一边在同一个 helper 里开了一个新的。
    返回 None 这个形态本身就是那个缺陷 —— 现在它不存在了，调用方无从「忘记处理」。

    毒化会不会误伤合法写法：不会。全站 78 条 padding 声明里，三个简写
    （73 条 `padding` + 1 条 `padding-block` + 1 条 `padding-inline`）**全部**切得开；
    唯一过不了上面那条正则的是一条**长写** `padding-right:
    calc(var(--space-3) + var(--space-2))`，长写不走本函数。裸 `0` 是正则认的。
    至于 `inherit` / `var(--x, 4px)` 这些「合法但算不出数」的形态：毒化正是想要的
    结果 —— 本文件的口径是模型宁可响亮拒绝也不猜（见 `_resolve_length_px`）。
    """
    raw = _IMPORTANT_RE.sub('', value or '').strip()
    parts = raw.split()
    sides = _PADDING_SHORTHANDS[name]
    if (not parts or len(parts) > len(sides)
            or not all(_PADDING_ONE_VALUE_RE.match(p) for p in parts)):
        return {side: raw for side in sides}          # 毒化
    if name == 'padding':
        if len(parts) == 1:
            t = r = b = l = parts[0]
        elif len(parts) == 2:
            t, b = parts[0], parts[0]
            r = l = parts[1]
        elif len(parts) == 3:
            t, r, b, l = parts[0], parts[1], parts[2], parts[1]
        else:
            t, r, b, l = parts
        return {'padding-top': t, 'padding-right': r,
                'padding-bottom': b, 'padding-left': l}
    start, end = (parts[0], parts[0]) if len(parts) == 1 else (parts[0], parts[1])
    return {sides[0]: start, sides[1]: end}


def _btn_decls(body):
    """规则体 -> [(属性, 值, 是否!important, 声明序号), ...]，简写已展开成长写。

    **序号是承重的，不是装饰。** 同一条规则里 `outline: 2px solid X` 之后再写
    一行 `outline-width: 0`，两者的选择器/特异度/规则序号完全相同，只有声明先后
    能分出胜负。第一版的比较键是 `(important, 特异度, 规则序号)`，同键时
    `key > best[key]` 为假 —— **先出现的赢**，正好和 CSS 反过来。
    评审实测：补一行 `outline-width: 0` 焦点环消失、补 `border-style: none`
    边框消失，两者都 263 passed。

    末尾那张 `padding-*` 安全网是 2026-08-15 收尾轮补的，见
    `_PADDING_KNOWN_NAMES`：属性名这一轴此前是静默丢弃的。
    """
    out = []
    idx = 0
    unknown_padding = set()
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
        elif name in _PADDING_SHORTHANDS:
            # `_split_padding` 现在**永远**给出每一条边（切不开就毒化），
            # 所以这里没有「拿到 None 怎么办」这个分支可以写错。
            for longhand, side_val in _split_padding(name, val).items():
                out.append((longhand, side_val, important, idx))
            out.append((name, val, important, idx))   # 简写本身也留着，便于报错时引用
        elif name in _PADDING_LOGICAL_LONGHANDS:
            out.append((_PADDING_LOGICAL_LONGHANDS[name], val, important, idx))
            out.append((name, val, important, idx))   # 原属性名也留着，便于报错时引用
        elif name == 'background':
            out.append(('background-color', val, important, idx))
            out.append((name, val, important, idx))
        else:
            if name.startswith('padding') and name not in _PADDING_KNOWN_NAMES:
                unknown_padding.add(name)
            out.append((name, val, important, idx))
        idx += 1
    assert not unknown_padding, (
        f'规则体里出现了模型不认得的 padding 属性 {sorted(unknown_padding)} —— '
        '它会被 `_btn_computed` 的 `d[0] in props` 无声丢掉，于是模型算一个数、'
        '浏览器渲染另一个数而断言全绿。要么是拼错了，要么是 CSS 又出了个新的 '
        'padding 属性，后者请补进 `_PADDING_LOGICAL_LONGHANDS` / '
        f'`_PADDING_SHORTHANDS`。出问题的规则体：{body.strip()!r}'
    )
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
#
# ⚠️ 2026-08-15（Task 4）`'btn-info': '--color-info'` **已删除**，与 style.css 的
# 四条 `.btn-info` 规则、tests/test_fix_templates_a11y.py 那条双向跨文件锁
# 同一轮落地。它零引用（唯一的用例「历史表查看详情」2026-08 随任务名按钮化
# 下线），唯一的存在理由是给本模型当被测对象 —— 而模型里还有 4 个真实变体
# 逐格计算，够用了。连带的三个数：`len(ALL_BTN_VARIANTS)` 8 -> 7、
# `len(BUTTON_CONTEXTS)` 11 -> 10、上下文 x 状态矩阵 55 -> 50 格。
FILLED_BTN_VARIANTS = {
    'btn-primary': '--color-accent-strong',
    'btn-success': '--color-success',
    'btn-warning': '--color-warning',
    'btn-danger':  '--color-danger',
}
# ⚠️ `.btn-outline-danger` **刻意不在册**（U8）。它的五态已在 style.css 里补全
# （base / hover / focus-visible / active），但入册会撞上一个无解的约束：
# 暗色档启用墨 --color-danger(#f87171) 的相对亮度是 0.3296，
#   · BTN_DISABLED_MIN_INK_DIMMING 要求禁用墨 L <= 0.3296 - 0.15 = 0.1796；
#   · BTN_INK_MIN_CONTRAST 要求禁用墨对按钮底 #1c2027 达 4.5:1，即 L >= 0.2392。
# 两个区间为空 —— 除非把危险色本身调暗（改变语义）或放宽某条阈值，否则
# 无论选什么禁用墨都必红。要入册请先决定动哪一条，不要直接往元组里加。
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
    # templates/index.html 的 #createTaskBtn —— 提交按钮，**默认 disabled**，
    # 本任务的核心缺陷所在
    _BtnCtx({'btn', 'btn-primary', 'w-100'}, element_id='createTaskBtn',
            label='#createTaskBtn（首页提交按钮）'),
    # tasks.js —— 活动任务行图标按钮，祖先是 .btn-group.btn-group-sm
    _BtnCtx({'btn', 'btn-icon', 'btn-danger'}, {'btn-group', 'btn-group-sm'},
            label='任务行 .btn-icon.btn-danger（.btn-group-sm 内）'),
    # （曾有一条「历史表 .btn-icon.btn-sm.btn-info（无 btn-group 祖先）」——
    #  「查看详情」图标按钮的上下文；2026-08 详情入口改为任务名按钮
    #  <button class="task-name">（不走 .btn 体系），该上下文随之移除。）
    # templates/_config_content.html 的 #configResetBtn —— .config-section 内的按钮
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
    assert len(BUTTON_CONTEXTS) == 10, (
        f'真实上下文有 {len(BUTTON_CONTEXTS)} 个，期望 10 —— 本测试已失效'
        '（2026-08-15 Task 4：btn-info 变体删除，11 -> 10）'
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
    assert len(BUTTON_CONTEXTS) == 10, '上下文表变了 —— 本测试已失效'
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
    assert len(FILLED_BTN_VARIANTS) == 4, (
        '变体表变了 —— 本测试已失效（2026-08-15 Task 4：btn-info 删除，5 -> 4）')
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
    assert len(FILLED_BTN_VARIANTS) == 4, (
        '变体表变了 —— 本测试已失效（2026-08-15 Task 4：btn-info 删除，5 -> 4）')
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
    assert len(BUTTON_CONTEXTS) == 10, '上下文表变了 —— 本测试已失效'
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

    其中 `.btn-success` / `.btn-danger` 在任务行里至今仍是**纯图标**按钮，
    SVG 用 `stroke="currentColor"` 描边 —— 1.92:1 意味着图标近乎看不见。
    （第三行那个 `#fff on #60a5fa` 是 `.btn-info` 的实测。该变体已于 2026-08-15
    Task 4 随按钮几何合并删除 —— 零引用，且唯一的存在理由是给本模型当被测
    对象；数字留在这里是**历史记账**，不是现役被测项。）
    """
    css = _css()
    backdrop = _btn_backdrop(css)
    cells = [(c, s) for c in BUTTON_CONTEXTS for s in _BTN_STATES]
    assert len(cells) == 50, (
        f'上下文 x 状态 = {len(cells)} 格，期望 10 x 5 = 50 —— 本测试已失效'
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
        + '\n其中 success/danger 是纯图标按钮（SVG 走 currentColor），'
        '墨色不达标 = 图标消失'
    )


# --------------------------------------------------------------------------
# 纯图标按钮：尺寸走密度令牌 + 无障碍名称
# --------------------------------------------------------------------------

# **全站**纯图标按钮（无可见文本）的数量，JS 模板与 HTML 模板一起扫。
#   static/js/history.js 启动/暂停/恢复/取消/移除/删除/预览  7
#     （**该行是历史记录**：扫描列表现为 task_list.js / map.js / config.js，
#     见 _icon_only_buttons；「取消」「移除」两颗也已随「取消任务」下线，
#     账本按下面的增减量逐条推到最终值，不要拿这一行当现状）
#     （2026-08 单一时间流定稿：行渲染收口 history.js createTaskRow，
#     tasks.js 的 5 颗任务控制按钮随迁——tasks.js 不再有任何 <button>，
#     也从 _icon_only_buttons 的扫描列表移除，见那里的说明；
#     2026-08「查看详情」图标按钮移除，详情入口改为任务名按钮
#     <button class="task-name">——它的可见文本是 ${escapeHtml(task.name)}
#     插值，_MARKUP_NOISE_RE 认得这种「文本插值」，不会把它误扫进本表，
#     见那里的登记）
#   templates/base.html 详情弹窗的 .btn-close                    1（2026-08 起弹窗
#     标记收口到 base.html <body> 直下——曾经嵌在记录面板里被遮罩盖住，
#     也曾在 index/history 两页各放一份拷贝；单出处见
#     test_task_detail_modal_is_not_trapped_inside_workbench_panel）
#   templates/index.html 下载/处理两个弹窗的 .btn-close      0（2026-07 弹窗化时
#     +2，2026-08-15 Task 5 两个弹窗退场时 -2。#createPanel 的关闭钮来自
#     _macros.html 的 panel_header 宏，宏体只计一次，所以合并没有新增
#     纯图标按钮；新增的那颗 rail「新建」带可见 <span> 文字，不算纯图标。
#     替代 dock 的 dock-collapse-btn / dock-reopen-handle 两颗更早已随 dock 移除）
#   templates/_macros.html panel_header 宏里的关闭钮              1（2026-08 抽宏：
#     记录/配置两个面板的头部改前是逐字重复的两段、各带一颗关闭钮，
#     现在收进 {% macro panel_header %}。宏定义算一次，宏调用不产生
#     <button>（同 _config_content.html 的 hint 宏，见下面 15->19 的登记）。
#     ⚠️ _macros.html 必须在 _icon_only_buttons 的扫描列表里，否则这颗
#     没有可见文本的按钮会整个逃出无障碍名称断言。
#   templates/_path_browser_modal.html 目录选择弹窗的 .btn-close  1（2026-08
#     保存路径「浏览」功能新增;经 base.html 的 {% include %} 文本展开扫到,
#     已带 aria-label="关闭"）
#   _config_content.html 瓦片服务器行的「删除该服务器」     2（2026-07 行编辑器新增；
#     Jinja include 展开后 index.html 与 config.html 各扫到一次，预期重复；
#     Jinja for 循环在源码里只出现一次，动态增删的行由 JS 模板生成、不在静态扫描内）
# 合计 15。
#
# ⚠️ 第一版只扫两个 JS 文件、常量写 6，读起来像「全站都覆盖了」而实际漏了
# 模板。评审实测：当时 `templates/base.html` 的 navbar-toggler 在 900px 视口下
# `display: block`、56x40、**`aria-label` 为 null** —— 一颗真实存在、真实可见、
# 真实缺无障碍名称的纯图标按钮，就在断言的扫描范围外（该按钮已随顶部
# 工具栏一并移除）。
# 注：地图左上角的 map-panel-btn（数据下载/数据处理/历史记录/配置）曾按纯
# 图标按钮计入；实机反馈「纯图标必须悬停才知道功能」后已改为图标+文字，
# 有可见文本，不再计入本表。
# `.btn-close` 也算进来：它确实是一颗没有可见文本的按钮。它的 aria-label 原本
# 是 Bootstrap 默认的英文 "Close"，在整站中文界面里读屏会念出 "Close"，
# 已一并改成「关闭」。它不走 `.btn-icon`（有自己的尺寸规则），所以只参与标签断言，不参与下面的尺寸断言。
# 15 -> 19（配置页说明图标 .hint）：config.html 与 index.html 各扫出 2 颗——
# `_config_content.html` 里 `{% macro hint() %}` 宏体那一颗（宏**调用**是
# `{{ hint(...) }}`，源码里不产生 <button>，所以每个模板只计宏定义一次），
# 加代理状态图标 #proxyStatusIcon。两者都带 aria-label，且都**不走 .btn 体系**
# （class 是 hint，不是 btn），所以只参与标签断言、不参与 .btn-icon 尺寸断言。
# 19 -> 20（把 static/js/map.js 与 static/js/config.js 补进扫描列表）：
# 净增的只有 `config.js` 动态渲染的瓦片服务器行里那颗「删除该服务器」
# （`.btn.btn-icon.btn-outline-danger.tile-server-remove`，已带 aria-label）——
# 它与上面 `_config_content.html` 里静态那两颗是同一个功能的两种渲染路径：
# 首屏由 Jinja for 出，用户点「添加」新增的行由 config.js 出。之前只钉住了
# 静态那份，JS 那份漏在扫描外。
# **map.js 净增 0**：它的按钮（框选下载/删除、预览停止、手动输入范围的
# 确定/取消等）文字都写成 `${t('...')}`，是有可见文本的按钮 —— 这也正是
# 必须先让 _MARKUP_NOISE_RE 认得 `${t(...)}` 才能扩大扫描列表的原因，
# 否则它们会集体变成假的「纯图标按钮」。
# 20 -> 18（task_list.js 的 TaskRow 删掉「取消」与「移除」两颗叉号）：
# 「取消任务」整条链下线后任务行只剩 开始/暂停/恢复/预览/删除 五颗。
# 18 -> 17（面板头部抽宏）：index.html 两颗覆盖面板关闭钮（记录/配置，改前
# 是逐字重复的两段头部）收进 _macros.html 的 panel_header 宏 —— 2 颗变 1 处
# 宏定义。净 -1。
# 17 -> 18（任务行加「处理」钮）：已完成的高程下载任务行在预览旁多一颗
# 扳手钮，打开处理弹窗预选该任务（task_list.js TaskRow）。
# 18 -> 19（命令面板）：base.html 新增 _command_palette.html 的速查表关闭钮
# （.cmdk__help-close，× 字符 + aria-label，-labelled 合规）。
# 19 -> 20（§5.3 导出 MBTiles）：task_list.js 的 TaskRow 在预览/处理旁多一颗
# 「导出 MBTiles」立方体钮（已完成的地图/等高线任务，带 title + aria-label）。
# MBTiles 是**通用产物容器**而不是第四种 output_format，所以它是完成后的一个
# 动作而不是创建表单里的一个值 —— 那一勾在 templates/index.html 里是带
# <label> 的复选框，不是按钮，不进本计数。
# 2026-08-15 Task 5：21 -> 19。两个参数弹窗各带一颗 .btn-close，随弹窗一起退场；
# 面板的关闭钮走 panel_header 宏（已计在宏体那一行里），rail 的「新建」有可见
# 文字。所以净 -2，见上面账本里 index.html 那一行。
ICON_ONLY_BUTTON_COUNT = 19

_JS_BUTTON_RE = re.compile(r'<button\b([^>]*)>(.*?)</button>', re.S)

# 纯图标按钮里允许出现的「不是可见文本」的东西：标签、HTML 实体、以及
# **既不是 escapeHtml 也不是 t 的**模板插值。两个负向前瞻各有来由：
#
#   `${escapeHtml(...)}` 是项目里「插值一段服务端文本」的固定写法(task.name
#   等)——2026-08 任务名按钮化后，<button class="task-name"> 的内容就是
#   ${escapeHtml(task.name)}；把它当噪声剥掉会将这颗有可见文本的按钮误扫成
#   纯图标（占掉一个计数名额、还缺 aria-label）。
#
#   `${t(...)}` 是 i18n 改造**之后** JS 模板里可见文本的主要形态 —— map.js /
#   config.js 里的按钮文字几乎全写成 ${t('...')}。正则原先不认它，于是这两个
#   文件里 4 颗有可见文本的按钮会被判成纯图标。这正是它们一直进不了下面
#   扫描列表的真实原因：不先认下 t(，一加进来就是一批假失败，而假失败会
#   逼着后来的人去调 ICON_ONLY_BUTTON_COUNT，把计数账本彻底搞脏。
#
#   `{{ icon_close(14) }}` 是 Jinja 侧的图标插值（_macros.html 的 panel_header
#   宏体）。它**必须**被当成噪声剥掉：那颗按钮的内容只有一个 SVG 图标，
#   剥不掉的话残留一段 `{{ ... }}` 文本，按钮会被判成「有可见文本」而整个
#   逃出无障碍名称断言 —— 实测就是这样漏掉的（宏抽取后计数从 20 掉到 18
#   而不是预期的 19）。图标宏一律以 icon_ 开头，只认这个前缀：把所有
#   `{{ }}` 都当噪声会把内容是 `{{ t('...') }}` 的按钮误判成纯图标。
_MARKUP_NOISE_RE = re.compile(
    r'<[^>]*>|\$\{(?!escapeHtml\(|t\()[^}]*\}|\{\{\s*m?\.?icon_[^}]*\}\}|&[a-zA-Z]+;|&#\d+;')


def _icon_only_buttons():
    """全站（JS 模板 + HTML 模板）里所有「没有可见文本」的 <button>。

    返回 [(来源, 属性串)]。**每个来源都断言至少扫到一个 <button>**，
    正则失配时响亮失败而不是退化成空循环。

    base.html 在扫描列表里：2026-08 起任务详情弹窗标记收口在
    base.html <body> 直下（.btn-close 是纯图标按钮）。
    """
    sources = []
    # map.js / config.js 是 2026-08 才补进来的：在 _MARKUP_NOISE_RE 认得
    # ${t(...)} 之前，它们里面用 ${t('...')} 写可见文本的按钮会被误判成纯
    # 图标按钮，加进来就是一批假失败 —— 见那条正则的注释。
    # tasks.js / history.js 仍然不在列表里：任务行的按钮模板 2026-08 Vue 化后
    # 收口到 task_list.js 的 TaskRow 组件 template，那两个文件已经没有任何
    # <button> 标记，留下只会触发上面那条响亮失败。
    for name in ('task_list.js', 'map.js', 'config.js'):
        sources.append((f'static/js/{name}', _strip_js_comments(_js(name))))
    # _macros.html：panel_header 宏体里那颗关闭钮只在这里出现（调用点展开后
    # 不在模板源码里），漏掉它就等于把那颗按钮移出无障碍名称断言。
    for name in ('base.html', 'index.html', 'history.html', 'config.html', '_macros.html'):
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
    # `.btn-close` 不带 `btn` 类，是 Bootstrap 的独立组件，
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

    选择器必须是 `.btn.btn-icon`(0,2,0)：任务行的动作按钮容器是
    `.btn-group.btn-group-sm`，而 `.btn-group-sm .btn { padding: .4rem .9rem }`
    也是 (0,2,0) —— 裸 `.btn-icon`(0,1,0) 的 `padding: 0` 会输给它，
    按钮被撑成胶囊。曾注册的第二条路径「历史表 .btn-icon.btn-sm、
    没有 btn-group 祖先」是「查看详情」图标按钮的上下文，2026-08 随
    任务名按钮化移除；现存图标按钮（任务行/历史表）全在
    `.btn-group.btn-group-sm` 内，只剩一个真实上下文。
    """
    css = _css()
    ctl_h = _token_px(css, '--ctl-h')
    contexts = [c for c in BUTTON_CONTEXTS if 'btn-icon' in c.classes]
    assert len(contexts) == 1, (
        f'带 .btn-icon 的真实上下文有 {len(contexts)} 个，期望 1'
        '（任务行/历史表图标按钮都在 btn-group-sm 内）—— 本测试已失效'
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
    """一个复合选择器（不含组合符）-> dict；读不懂返回 None（= 模型不支持）。

    2026-08-15 修复 —— **属性选择器不再当噪声抹掉**。改前 `leftover` 那条正则
    把 `\\[[^\\]]*\\]` 一起吃了、且不记在任何字段里，于是 `[data-bogus]` 解析成
    tag/ids/classes 全空的 dict，`_compound_structurally_matches` 对它一律为真 ——
    **等价于 `*`**。实测：`_text_branch_applies('[data-bogus]', [span.detail-v])`
    返回 True（应为 None），`'[data-x] .detail-v'` 返回 False（祖先链同样不记属性，
    那个 False 也是猜的）。这不是拒答而是**静默多/少匹配**：一条属性选择器规则
    会参与 `_winning_color_decl` 的胜负比较，赢家算错 -> 对比度数字算错 -> 全绿。
    style.css 现有 19 条带属性选择器的规则。

    这里只**记下来**（`attrs`），不在解析期判 None —— 口径与 `_btn_branch_applies`
    里那段同样理由的注释一致：解析期还不知道这个 compound 会不会真的命中，
    一律判 None 会把 `.workbench-panel[hidden]` 这种和正文八竿子打不着的规则
    顶成「模型已失效」，那是误报不是保护。真正的拒答排在「确定不命中」之后，
    见 `_text_node_verdict` 与 `_text_branch_applies` 第二步的 `attrs` 检查。
    `test_text_color_model_assumptions_still_hold` 的第五条前提再从另一头把
    「声明 color 的规则里有属性选择器」这件事本身钉住。
    """
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
    attrs = re.findall(r'\[[^\]]*\]', rest)
    leftover = re.sub(r'([#.][-\w]+|:[-\w]+(?:\([^)]*\))?|\[[^\]]*\]|\*)', '', rest).strip()
    if leftover and not re.fullmatch(r'[a-zA-Z][-\w]*', leftover):
        return None
    return dict(tag=(leftover.lower() or None), ids=ids, classes=classes, pseudos=pseudos,
                attrs=attrs, neg_pseudos=neg_pseudos, neg_classes=neg_classes,
                pseudo_element=(pseudo_elements[0] if pseudo_elements else None))


def _compound_structurally_matches(comp, node):
    """只看标签/类/id/伪元素这些**确定**的部分。伪类与属性选择器的支持性另判
    （`comp['attrs']` 在这里**故意不看** —— 它说不清，看了就变成猜）。"""
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


def _split_branch(branch):
    """`.a > .b .c` -> [('.a', None), ('.b', '>'), ('.c', ' ')]，组合符跟在**右**边那项上。

    返回 None 表示写法看不懂。
    """
    toks = re.findall(r'[>+~]|[^\s>+~]+', branch)
    if not toks or toks[0] in '>+~':
        return None
    out = [(toks[0], None)]
    i = 1
    while i < len(toks):
        if toks[i] in '>+~':
            if i + 1 >= len(toks) or toks[i + 1] in '>+~':
                return None
            out.append((toks[i + 1], toks[i]))
            i += 2
        else:
            out.append((toks[i], ' '))
            i += 1
    return out


def _color_media_verdict(at_rule, reduced=False):
    """条件组 at-rule 在颜色模型的建模环境下成立吗？True / False / None(模型不支持)。

    **与 `_motion_media_verdict` 同形，但是另一份拷贝**（同文件，动画模型的同名
    判决）：同一个 `_PREFERS_REDUCED_MOTION_RE`，命中就把 `reduce`/`no-preference`
    与 `reduced` 比对，不命中一律返回 None。

    ⚠️ 颜色模型**只算 `reduced=False` 这一种环境** —— 唯一的生产调用方
    `_winning_color_decl` 走的是默认参数，从不传 True。`reduced` 这个参数存在
    只为两件事：与供体保持同形（改一边时另一边的差异看得见），以及能被
    tests/test_css_cascade_model.py 单独喂两种环境做单测。
    后果是 reduce 块里的 color 在本模型眼里判 False = 在建模环境下是死代码，
    整条规则被**静默跳过**。这不是漏洞而是有人看着的：
    `test_text_color_model_assumptions_still_hold` 的 `env_offenders` 那条断言
    专门禁止 prefers 块里出现 color，并说明了真要在里面改颜色得先让颜色模型
    像 `_motion_computed` 那样把两种环境各算一遍。

    宽度类断点（`min-width` / `max-width`）与 `@supports` 都落在「不命中」这条路上，
    也就是**仍然不支持**。理由不是懒：模型只建「一个视口宽度、一份用户偏好」这
    一种环境，它不知道断点两侧该把哪一边当事实，`@supports` 更要求知道浏览器
    支持什么。往这两类 at-rule 里塞 color，`_winning_color_decl` 会响亮失败 ——
    宁可报「算不了」，也不给一个只在某个视口宽度下才对的对比度数字。
    """
    m = _PREFERS_REDUCED_MOTION_RE.match(re.sub(r'\s+', ' ', at_rule).strip())
    if not m:
        return None
    want_reduce = m.group(1).lower() == 'reduce'
    return want_reduce == bool(reduced)


def _text_node_verdict(comp, node):
    """祖先链上的一个位置：这个复合项命中这个节点吗？
    True / False / None(伪类或属性选择器不支持)。

    伪类必须**和结构一起**判，不能等祖先链走完再补判：`.card:hover .detail-v`
    对 [div.card, div.card(hover), span.detail-v] 这种链，只按结构挑位置会挑到
    最近那个没 hover 的 `.card`、然后判不命中 —— 和下面那个贪心漏判是同一类错。
    返回 None = 「这个位置说不清」，由调用方汇总成「模型不支持」。

    `attrs` 那条排在**全部确定性判决之后**（2026-08-15 加）：`_TextEl` 不记属性，
    所以属性选择器一律说不清；但说不清只在「其它部分都对得上」时才要紧。
    排前面的话 `html[lang="en"] .map-panel-btn` 这类祖先项会把和正文无关的
    规则顶成「模型已失效」。改前这里根本没有这一条 —— `_parse_compound` 把
    `[...]` 当噪声抹了，属性选择器等价于 `*`，静默多匹配。
    """
    if not _compound_structurally_matches(comp, node):
        return False
    if not (comp['pseudos'] | comp['neg_pseudos']) <= _TEXT_SUPPORTED_PSEUDOS:
        return None
    if not comp['pseudos'] <= node.pseudos:
        return False
    if comp['neg_pseudos'] & node.pseudos:
        return False
    if comp['attrs']:
        return None
    return True


def _text_branch_applies(branch, chain):
    """这个选择器分支命中链尾那个节点吗？True / False / None(模型不支持)。

    判定顺序与 `_btn_branch_applies` 一致，而且是承重的：**先判「肯定不命中」，
    再判「形态不支持」**。反过来写的话，`div:not(.card):not(...)...` 这种
    与目标元素八竿子打不着的规则会把整个模型顶成「已失效」。

    组合符：后代（空格）与子（`>`）都能从祖先链**精确**判定，拆分复用 div 背景
    模型的 `_split_branch`（同一个拆分器，不是抄一份 —— 抄两份就会分叉）。
    兄弟组合符（`+` `~`）返回 None：祖先链里不记兄弟节点，当成后代会**多**匹配
    （可能把一条其实管不到的规则算成赢家），当成不命中又会漏。

    ⚠️ 共用的是拆分器 `_split_branch`，不是匹配器 `_branch_matches` —— 后者的节点是
    `(tag, classes, id, attrs)` 四元组、且按**静止态**建模（`:hover` 一律判不成立、
    带 `::` 的一律判不命中）。本模型的节点是 `_TextEl`，hover/focus 态是要算的
    （`_TEXT_SUPPORTED_PSEUDOS`），伪元素还要跟 `node.pseudo_element` 比。
    拿 `_branch_matches` 来算文字颜色会同时废掉这两件事。从它那里搬过来的只有
    **判定顺序**，不是代码。

    at-rule 里的 color 由 `_color_media_verdict` 判环境，本函数只管选择器形态。
    color 规则里不许出现 `+` `~` 这条前提由
    `test_text_color_model_assumptions_still_hold` 钉住，前提被打破时是那条测试
    变红，而不是本函数悄悄按后代关系算错。

    2026-08-14 修复：祖先链的后代那一步改成**带回溯**。第一版是右往左贪心，
    `.a > .b .c` 对 [div.a, div.b, div.b, span.c] 会判 False（先抓最近的 `.b`，
    `>` 那一步再看 chain[1] 发现不是 `.a` 就收工），而 CSS 的答案是命中。
    这种漏判会把一条**真命中**的规则踢出候选集 -> 赢家算错 -> 对比度数字算错
    -> 全绿，正是本模型最怕的失效形态。看守：tests/test_css_cascade_model.py。
    """
    parts = _split_branch(branch)
    if parts is None:
        return None
    subject = _parse_compound(parts[-1][0])
    if subject is None:
        return None

    # ---- 第一步：能否**确定地**判为不命中 ----
    # 顺序照抄 `_branch_matches`：主体复合项先判，`+`/`~` 的「形态不支持」排在它
    # 之后。反过来的话 `.btn-check:checked+.btn` 这类根本打不到本元素的规则会把
    # 整个模型顶成「已失效」。祖先的复合项也**在这道判决之后**才解析（供体是在
    # 走链时逐个懒解析）—— 主体已经明确不命中时，一个读不懂的祖先不该把这条
    # 规则升级成「模型已失效」。
    if not _compound_structurally_matches(subject, chain[-1]):
        return False
    if any(comb in ('+', '~') for _sel, comb in parts):
        return None
    compounds = []
    for sel, _comb in parts[:-1]:
        c = _parse_compound(sel)
        if c is None:
            return None
        compounds.append(c)
    compounds.append(subject)

    def walk(k, i):
        """compounds[k] 及其左边全部落在 chain[i] 及其之上吗？True/False/None。

        parts[k+1] 上挂的组合符描述的是 compounds[k] 与 compounds[k+1] 的关系。
        后代那一步要**回溯**：最近的候选走不通就退回来试更上面的。`>` 那一步
        位置定死，不许回溯 —— 它说的就是「上一代」，换个位置就不是它了。
        """
        if k < 0:
            return True
        if parts[k + 1][1] == '>':
            if i < 0:                     # 链首之上没有节点了（Python 负下标会静默回绕）
                return False
            v = _text_node_verdict(compounds[k], chain[i])
            return walk(k - 1, i - 1) if v is True else v
        unknown = False
        while i >= 0:                     # 同上：i 见底就停，不许回绕
            v = _text_node_verdict(compounds[k], chain[i])
            if v is None:
                unknown = True            # 这个位置说不清 -> 整体不敢判 False
            elif v:
                r = walk(k - 1, i - 1)
                if r is True:
                    return True
                if r is None:
                    unknown = True
            i -= 1
        return None if unknown else False

    verdict = walk(len(parts) - 2, len(chain) - 2)
    if verdict is not True:
        return verdict

    # ---- 第二步：祖先链确实能对上，此时才允许报主体的「形态不支持」----
    if not (subject['pseudos'] | subject['neg_pseudos']) <= _TEXT_SUPPORTED_PSEUDOS:
        return None
    if not subject['pseudos'] <= chain[-1].pseudos:
        return False
    if subject['neg_pseudos'] & chain[-1].pseudos:
        return False
    if subject['attrs']:
        # `[aria-pressed="true"]` 这类：结构、伪类全对上了，只剩属性说不清 ——
        # 此时才拒答（与 `_text_node_verdict` 里那条同理同序）。
        return None
    return True


# Bootstrap 5.3.0 里可能与 style.css 抢同一个元素 `color` 的规则。
#
# ⚠️ 这张表**不用来算颜色**，只用来做「style.css 必须赢」的下界检查。
# 原写法是「Bootstrap 是 CDN 引入的，本仓库里没有它的源码，拿它的值算最终色
# = 拿一份手抄的常量冒充事实」。**vendor 本地化之后前半句已经不成立**：源码就在
# static/vendor/bootstrap/5.3.0/。但结论不变——真要算最终色得先有个完整的 CSS
# 解析器（变量解析 + 层叠 + 继承），那是另一件事。所以做法仍然是：一旦模型算出
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
                # at_ctx 是这条规则外面套的**每一层** at-rule（可能嵌套多层），
                # 所以逐条判而不是整体判：全部成立才算候选，任一条不成立就整条
                # 规则跳过（环境不成立 = 这条规则在建模环境下是死的，不是放行），
                # 出现判不了的才响亮失败。
                verdicts = [_color_media_verdict(a) for a in at_ctx]
                for at_rule, verdict in zip(at_ctx, verdicts):
                    if verdict is None:
                        raise AssertionError(
                            f'[{label}] 命中的规则 `{branch}` 外面套着 at-rule '
                            f'`{at_rule}`，本模型判不了这种环境（只判 '
                            'prefers-reduced-motion，宽度断点与 @supports 不建模）'
                            ' —— 已失效。要么把 color 挪到这个 at-rule 外面，'
                            '要么给 `_color_media_verdict` 补上这类环境的判决'
                            '（同时得决定断点两侧把哪一边当事实）—— 别让它给一个'
                            '只在某个视口宽度下才对的对比度数字。'
                        )
                if not all(verdicts):
                    break               # 环境不成立：整条规则在建模环境下是死的
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


# 声明 color 的规则里，允许出现属性选择器的那几条分支 -> 为什么模型可以不判它。
#
# 这**不是**放行证。`_TextEl` 不记属性，所以任何属性选择器对模型都是「说不清」，
# `_text_node_verdict` / `_text_branch_applies` 在确定不命中之外一律返回 None，
# `_winning_color_decl` 拿到 None 就判「模型已失效」。登记在这里只是要求作者
# 显式回答一个问题：为什么模型走不到那一步？下面第五条前提会**重新证明**这个
# 回答 —— 逐条已登记文字上下文跑一遍，必须条条确定判 False，证不出来就红。
_ATTR_COLOR_WHITELIST = {
    '.map-search__chip[aria-pressed="true"]':
        '地图搜索胶囊的按下态。`.map-search__chip` 不出现在任何一条已登记文字'
        '上下文的链上（它是地图浮层里的筛选钮，不是正文），主体复合项在类这一'
        '步就确定不命中，模型永远走不到「判不了属性」那一步。要把胶囊文字纳入'
        '对比度覆盖，得先给 `_TextEl` 补属性字段、给 `_text_node_verdict` 补真'
        '判决，再往 `_text_contexts` 加链 —— 不是把它从这张表里删掉了事。',
}


def test_text_color_model_assumptions_still_hold():
    """层叠模型的五条前提：无兄弟组合符、无判不了的 at-rule 声明 color、
    没有**只在某种用户偏好下才成立**的 color、属性选择器不落在模型算得着的
    地方、style.css 排在最后。

    2026-08-14 登记 —— `>` 与 `prefers-*` at-rule 已支持。三样东西的复用程度
    刻意不同，别当成一回事：
      · `_split_branch`：**真共用**（全文件一个定义，div 背景模型与本模型两个
        消费者），改它会同时改到两边，这正是要的。
      · `_branch_matches`：只搬了**判定顺序**（主体先判、形态不支持后判），
        代码刻意没复用 —— 它的节点是四元组、按静止态建模（`:hover` 一律判不
        成立、带 `::` 的一律判不命中），拿来算文字颜色会同时废掉 hover/focus
        和 `::placeholder`。
      · `_color_media_verdict`：与 `_motion_media_verdict` **同形但是另一份
        拷贝**（同一个 `_PREFERS_REDUCED_MOTION_RE`、同一套判决），不是共用。
    `+` / `~` 与宽度断点仍不支持，原因见各自失败消息。

    第五条（2026-08-15 新增）单独说一句：`_parse_compound` 改前把属性选择器
    当噪声抹掉且不记录，`[data-bogus]` 于是解析成一个全空 compound = **等价于
    `*`**，实测 `_text_branch_applies('[data-bogus]', [span.detail-v])` 返回
    True。那是静默多匹配，会让一条根本管不到的规则参与 `_winning_color_decl`
    的胜负比较、赢下来、把对比度算错，而所有颜色断言照样绿。现在改成拒答，
    这条前提负责保证「拒答」不会天天发生在正经规则上。

    这条不是产品契约，是**模型的自检**。五条前提任何一条被打破，
    下面那些「算最终颜色」的断言就是在给一个错数字背书 ——
    「静默给出错误的信心」比没有断言更糟（这是 Task 10/11 反复付过学费的地方）。
    """
    css = _css()
    combinator_offenders = []
    media_offenders = []
    env_offenders = []
    attr_offenders = []
    attr_seen = set()
    color_branches = 0
    for sel, body, at_ctx in _rules_ctx(css):
        if 'color' not in _decl_map(body):
            continue
        for branch in _selector_parts(sel):
            color_branches += 1
            if re.search(r'[+~]', branch):
                combinator_offenders.append(branch)
            if '[' in branch:
                if branch in _ATTR_COLOR_WHITELIST:
                    attr_seen.add(branch)
                else:
                    attr_offenders.append(branch)
            for at_rule in at_ctx:
                if _color_media_verdict(at_rule) is None:
                    media_offenders.append(f'{at_rule} {{ {branch} }}')
                else:
                    env_offenders.append(f'{at_rule} {{ {branch} }}')
    assert color_branches > 100, (
        f'只扫到 {color_branches} 个声明 color 的选择器分支 —— 扫描逻辑已失效'
    )
    assert not combinator_offenders, (
        '发现带兄弟组合符（`+` / `~`）的 color 规则。子组合符 `>` 现在已支持，'
        '兄弟组合符仍不支持：`_text_branch_applies` 拿到的 chain 只有祖先链、'
        '不记兄弟节点，当成后代会**多**匹配（把一条其实管不到的规则算成赢家），'
        '当成不命中又会漏。所以它一律返回 None，调用方会报「模型已失效」：\n'
        + '\n'.join('  ' + o for o in combinator_offenders)
        + '\n要么改掉选择器（用后代或子组合符表达），要么给模型补上兄弟链信息——'
        '别让它继续拒答。'
    )
    assert not media_offenders, (
        '发现在**本模型判不了的 at-rule** 里声明 color 的规则。`prefers-reduced-motion` '
        '现在能判（`_color_media_verdict`），宽度断点与 `@supports` 仍不能：模型只建'
        '「一个视口宽度、一份用户偏好」这一种环境，不知道断点两侧该把哪边当事实：\n'
        + '\n'.join('  ' + o for o in media_offenders)
        + '\n要么把 color 挪出断点，要么给模型补上视口环境——别让它给一个只在某个'
        '宽度下才对的对比度数字。'
    )
    assert not env_offenders, (
        '发现**判得了但依赖用户偏好**的 color 规则（`prefers-reduced-motion` 块里的 '
        'color）。这一条和上一条说的不是一回事：上面那批是「模型判不了、会响亮'
        '失败」，这批是「模型判得了，然后**静默跳过**」—— `_winning_color_decl` '
        '只算 `reduced=False` 这一种环境（它调 `_color_media_verdict` 用的是默认'
        '参数），reduce 块里的规则在它眼里判 False = 这条规则在建模环境下是死的，'
        '整条被跳过，没有任何断言看过它：\n'
        + '\n'.join('  ' + o for o in env_offenders)
        + '\n要在 prefers 块里改颜色，得先让颜色模型像 `_motion_computed` 那样把'
        '**两种**环境都算一遍（`reduced=True` / `False` 各跑一次对比度），否则'
        '开了「减少动画」偏好的用户会拿到一个谁都没检查过的墨色。'
        '要么把 color 挪出 prefers 块。'
    )
    # --- 第五条前提：属性选择器 ---
    assert not attr_offenders, (
        '发现**未登记**的、带属性选择器的 color 规则。`_TextEl` 不记属性，所以'
        '模型对属性选择器只有两种答案：确定不命中（靠标签/类/id 判掉）或者拒答。'
        '一旦某条已登记文字上下文能走到「拒答」，`_winning_color_decl` 会判'
        '「模型已失效」，整批颜色断言集体变红：\n'
        + '\n'.join('  ' + o for o in attr_offenders)
        + '\n要么把 color 挪到不带属性选择器的规则上，要么给 `_TextEl` 补属性字段'
        '和真判决，要么登记进 `_ATTR_COLOR_WHITELIST` 并写明「模型为什么走不到'
        '那一步」—— 登记之后下面那条会去验这个理由，不是签个字就完。'
    )
    stale_attr = sorted(set(_ATTR_COLOR_WHITELIST) - attr_seen)
    assert not stale_attr, (
        f'`_ATTR_COLOR_WHITELIST` 里这几条已经不在 style.css 的 color 规则里了：'
        f'{stale_attr} —— 发霉的豁免会替下一条同名规则挡下本前提，删掉它们。'
    )
    # 白名单里的理由必须**当场证明**，不是签字放行：逐条已登记文字上下文跑一遍，
    # 必须条条确定判 False（= 模型压根走不到「判不了属性」那一步）。哪天有人给
    # 某条上下文的链加上 `.map-search__chip`，或者新登记一条胶囊上下文，这里立刻
    # 变红，而不是等 `_winning_color_decl` 在 20 条上下文上一起炸。
    reachable = []
    for branch in sorted(_ATTR_COLOR_WHITELIST):
        for label, chain, _bg, _want in _text_contexts(css):
            if _text_branch_applies(branch, chain) is not False:
                reachable.append(f'{branch}  @  {label}')
    assert not reachable, (
        '`_ATTR_COLOR_WHITELIST` 的豁免理由不成立了 —— 下面这些组合里，模型不再'
        '「确定不命中」，也就是它真的要去判那个属性了：\n'
        + '\n'.join('  ' + o for o in reachable)
        + '\n豁免的前提就是这一步走不到。给 `_TextEl` 补属性字段、给'
        ' `_text_node_verdict` 补真判决，别把这条断言放宽。'
    )
    # style.css 必须是最后加载的样式表——`.table td` 去掉 !important 之后
    # 靠的就是「同特异度、后来者赢」压住 Bootstrap 的 `.table > :not(caption) > * > *`。
    # 顺序契约由 test_no_stylesheet_can_load_after_style_css 独立守住，这里只做交叉引用。
    assert 'def test_no_stylesheet_can_load_after_style_css' in open(
        os.path.abspath(__file__), encoding='utf-8').read(), (
        '样式表顺序的断言不见了 —— 本模型「style.css 排在最后」这条前提没人守了'
    )


# --- 页面里真实存在的文字上下文 -------------------------------------------
#
# 每条 = (标签, 从祖先到目标元素的节点链, 背衬来源, 期望的颜色变量或 None)
# 链的形状照抄真实 markup（templates/*.html + 两个 JS 的模板字符串）。
#
# 登记（2026-08 统一流式列表重设计）：
#   · `_BS_TABLE_HOVER_INSET` 常量和 4 条「历史表单元格（±hover）」上下文
#     随 9 列 .task-table 一起删除——表格不存在了，Bootstrap 的行 hover
#     内阴影层（rgba(0,0,0,0.075)）也不存在；统一流式行没有行 hover 底色。
#   · 「表格内 <small>（含挂 .text-danger 的不变量）」两条一并删除：
#     `.table td small` 规则已随 `.table*` 整段移除，不变量失去对象。
#     错误文字变红的守卫没有丢，搬到「历史流『加载失败』提示」这一条
#     （挂在 .text-danger 上的错误提示，与 A7 修的是同一语义）。
#   · 「活动任务空态提示」删除：定稿设计里实时区无活动任务时整个留空，
#     不渲染空态；空态只剩历史流的「暂无历史记录」（.task-empty），已收进来。


class _DivClassCollector(HTMLParser):
    """收集一段 markup 里所有 <div> 的 class 集合。

    （前身 _CellClassCollector 收 <td>/<th>——表格废除后，「加载失败」
    提示从 td 变成 div，解析器跟着换。）
    """

    def __init__(self):
        super().__init__()
        self.divs = []

    def handle_starttag(self, tag, attrs):
        if tag == 'div':
            cls = dict(attrs).get('class', '')
            self.divs.append(frozenset(cls.split()))


def _history_error_div():
    """从 **history.js 实际出货的那段模板字符串**里解析出加载失败提示节点。

    ⚠️ 这个函数存在的唯一理由，是评审实测出来的一个 Critical 逃逸：
    把 history.js 里那行的 `class="text-center text-danger"` 改成
    `class="text-center"`（CSS 一个字不动）→ 全套全绿，而 CDP 走真实失败
    路径实测提示渲染成普通灰白色 —— 逐位就是修复前的那个 bug。

    根因是「层叠模型推理的是一个假想元素，不是实际出货的 markup」。这与
    Task 10 补链时的教训同型：配对断言守住了「标签 ↔ 变量」，守不住
    「变量 ↔ 数据源」。这里把数据源那一端接上：类名从 markup 里消失，
    模型就会对着一个没有 .text-danger 的节点算，直接给出
    --color-text-primary 而不是 --color-danger，断言立刻变红。
    """
    src = _strip_js_comments(_js('task_list.js'))
    # Vue 化后加载失败提示是组件的 ERROR_TEMPLATE 常量（改造前是 history.js
    # loadHistory 的 catch 分支里一次 innerHTML 赋值）。节点本身没变：
    # 一个 .text-center.text-danger 的 div。
    m = re.search(r'ERROR_TEMPLATE\s*=\s*`(.*?)`', src, re.S)
    assert m, 'task_list.js 里找不到 ERROR_TEMPLATE —— 加载失败提示的 markup 变形了，本测试已失效'
    markup = m.group(1)
    assert '<div' in markup, (
        f'从 loadHistory 的 innerHTML 里解析不出 <div>（拿到 {markup[:80]!r}）—— 本测试已失效'
    )
    p = _DivClassCollector()
    p.feed(markup)
    assert len(p.divs) == 1, (
        f'加载失败提示里解析出 {len(p.divs)} 个 <div>（期望 1）—— 本测试已失效'
    )
    return _TextEl('div', p.divs[0])


def _text_contexts(css):
    """页面上真实存在的文字上下文。

    ⚠️ 链的写法：每条链都要带上**承重**的祖先层（容器 id、组件类），
    不是照模板猜个大概——第一版按记忆写链，漏了 `tbody#historyTableBody`
    这个 id，任何写成 `#historyTableBody td { color: ... !important }` 的
    新规则在浏览器里会赢，在模型里却根本不命中，等于给自己开了个盲区。
    2026-08 重设计后改 markup 要同步重抓这里（面板路径
    section#historyPanel > .workbench-panel__body > .card > .card-body）。
    """
    panel = _effective_task_card_backdrop(css)          # `.card` 的底色，实测 #15171c
    modal = _modal_backdrop(css)
    control = _flatten(_resolve_color(css, _branch_background(css, '.form-control')), panel)
    toast = _flatten(_resolve_color(css, _branch_background(css, '.app-toast')),
                     _palette_var(css, '--color-bg-primary'))
    # M18 新增的三处背衬：状态栏每颗胶囊的底色在 `.statusbar-pill` 共享规则
    # 上（--color-bg-secondary 实心底；外壳本身透明）；地图浮层是
    # --color-overlay-surface（半透明，压在地图瓦片上 —— 取 bg-primary 作最坏
    # 近似的下层，与 toast 同一套做法）；弹窗复用上面的 modal。
    statusbar = _flatten(_resolve_color(css, _branch_background(css, '.statusbar-pill')),
                         _palette_var(css, '--color-bg-primary'))
    # 底色在 .map-overlay-chip 基类上（2026-08 抽出：右上范围浮层与右下预览
    # 提示条共用同一套定位/底色/描边/圆角/阴影，改前是复制粘贴的两份）。
    overlay = _flatten(_resolve_color(css, _branch_background(css, '.map-overlay-chip')),
                       _palette_var(css, '--color-bg-primary'))

    body = _TextEl('body')
    main = _TextEl('main', {'main-content'})

    # --- 记录面板统一流式列表（首页覆盖面板路径） ---
    panel_top = [body, main,
                 _TextEl('section', {'workbench-panel', 'workbench-panel--wide'},
                         element_id='historyPanel'),
                 _TextEl('div', {'workbench-panel__body'}),
                 _TextEl('div', {'card'}), _TextEl('div', {'card-body'})]
    hist_row = panel_top + [_TextEl('div', element_id='historyTableBody'),
                            _TextEl('div', {'task-row', 'status-completed'})]
    line1 = hist_row + [_TextEl('div', {'task-line1'})]

    # **从 history.js 实际出货的 markup 解析**，不写死 —— 见 _history_error_div 的说明。
    err_div = _history_error_div()

    # --- 详情弹窗（2026-08 起在 base.html 的 <body> 直下、.workbench 之外——
    # 曾经嵌在记录面板 .workbench-panel 里被 body 级遮罩盖住，见
    # tests/test_records_panel_structure.py 的 modal 层叠测试） ---
    modal_top = [body,
                 _TextEl('div', {'modal', 'fade', 'show'}, element_id='taskDetailModal'),
                 _TextEl('div', {'modal-dialog', 'modal-lg'}),
                 _TextEl('div', {'modal-content'}), _TextEl('div', {'modal-body'}),
                 _TextEl('div', {'detail-grid'}), _TextEl('div', {'detail-item'})]

    # --- 首页表单（2026-08-15 Task 5：在 #createPanel 里，不再是弹窗） ---
    # 背衬换了一层：弹窗时代文字压在 .modal-content 上（走 modal 背衬），
    # 现在压在 .workbench-panel 自己的表面上 —— 面板里**没有 .card**，
    # 所以不能沿用 panel_top 那条链用的 `.card` 底色。
    create_surface = _flatten(
        _resolve_color(css, _branch_background(css, '.workbench-panel')),
        _palette_var(css, '--color-bg-primary'))
    form_top = [body, main,
                _TextEl('section', {'workbench-panel'}, element_id='createPanel'),
                _TextEl('div', {'workbench-panel__body', 'workbench-panel__body--fill'}),
                _TextEl('div', {'config-layout'}),
                _TextEl('form', {'config-scroll'}, element_id='taskForm')]

    return [
        # (标签, 链, 背衬, 期望的调色板变量或 None)
        ('历史流「加载失败」提示',
         panel_top + [_TextEl('div', element_id='historyTableBody'), err_div],
         panel, '--color-danger'),
        ('任务行名称（2026-08 起是 button.task-name，点击开详情弹窗）',
         line1 + [_TextEl('button', {'task-name'})], panel, '--color-text-primary'),
        ('任务行 #类型:id',
         line1 + [_TextEl('span', {'task-id'})], panel, '--color-text-secondary'),
        ('任务行元信息（样式/缩放）',
         line1 + [_TextEl('span', {'task-meta'})], panel, '--color-text-secondary'),
        ('任务行状态小字',
         line1 + [_TextEl('span', {'task-status-text'})], panel, '--color-text-secondary'),
        ('任务行耗时（等宽弱化）',
         line1 + [_TextEl('span', {'task-time', 'progress-detail'})],
         panel, '--color-text-secondary'),
        ('历史行行2 摘要（已完成 · 数量 · 区域）',
         hist_row + [_TextEl('div', {'task-line2'})], panel, '--color-text-secondary'),
        ('历史流空态提示（暂无历史记录）',
         panel_top + [_TextEl('div', element_id='historyTableBody'),
                      _TextEl('div', {'task-empty'})], panel, '--color-text-secondary'),
        ('状态筛选 chip（未选中）',
         panel_top + [_TextEl('div', {'task-filter-bar'}),
                      _TextEl('div', {'status-chips'}),
                      _TextEl('button', {'status-chip'})], panel, '--color-text-secondary'),
        ('状态筛选 chip（选中）',
         panel_top + [_TextEl('div', {'task-filter-bar'}),
                      _TextEl('div', {'status-chips'}),
                      _TextEl('button', {'status-chip', 'active'})], panel, '--color-accent-hover'),
        ('首页表单分组标题',
         form_top + [_TextEl('div', {'form-group-label'})], panel, None),
        ('首页表单说明文字',
         form_top + [_TextEl('div', {'mb-3'}, element_id='demOptions'),
                     _TextEl('small', {'form-text', 'text-muted', 'd-block', 'mb-2'})], panel, None),
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
        # --- M18 补入的三条：整条状态栏、整个地图浮层体系、下载弹窗的
        # 非表单内容，此前在模型里【没有任何入口】。三处新增回归全部从这个
        # 缺口逃出去（且都晚于确立「muted 不是文字色」的 A7/Task 12）。
        ('状态栏最近事件',
         [body, _TextEl('footer', {'workbench-statusbar'}),
          _TextEl('span', {'statusbar-item', 'statusbar-event'}, element_id='statusEvent')],
         statusbar, None),
        ('地图浮层的编辑提示',
         [body, main, _TextEl('div', {'index-map'}),
          _TextEl('div', {'map-overlay-chip', 'bounds-overlay'}, element_id='boundsInfo'),
          _TextEl('span', {'bounds-hint'})], overlay, None),
        ('新建任务面板里的瓦片预估',
         [body, main,
          _TextEl('section', {'workbench-panel'}, element_id='createPanel'),
          _TextEl('div', {'workbench-panel__body', 'workbench-panel__body--fill'}),
          _TextEl('div', {'config-layout'}),
          _TextEl('form', {'config-scroll'}, element_id='taskForm'),
          _TextEl('div', element_id='selectionField'),
          _TextEl('div', {'tile-estimate'}, element_id='tileEstimate')],
         create_surface, None),
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

    上下文清单（20 条）的边界：覆盖三个页面上**由 style.css 上色的**全部
    正文类文字位置 —— 单一时间流的行内文本（名称/#类型:id/元信息/状态小字/
    耗时/行2 摘要）、时间流的加载失败提示与空态、状态筛选 chips
    （选中/未选中两态）、首页分组标题、表单说明、详情弹窗的键与值、
    首页与配置页各自的输入框占位符（两处走不同规则，只覆盖一处会漏）、
    toast 的关闭按钮。
    没进清单的三类各有归属：徽章文字 -> test_status_badge_text_is_readable_in_every_state
    （背衬是每个状态自己的半透明填充，不是面板底色）；按钮文字 ->
    test_button_ink_is_readable_in_every_state；JS 模板里的内联色 ->
    test_inline_colors_in_js_templates_meet_wcag_aa（它们不在 style.css 里，
    本模型扫不到）。三者合起来才叫「全覆盖」，单看本条不叫。
    （2026-08 两轮演进：第一轮原 14 条里的 6 条历史表格单元格上下文与
    1 条活动空态上下文随 9 列 .task-table / 活动空态一起删除，替换为
    流式列表上下文；第二轮「分组头（活动/失败/历史）」上下文随三分区
    删除，18 → 17，登记在 _text_contexts 上方的注释块。）
    """
    css = _css()
    contexts = _text_contexts(css)
    assert len(contexts) == 20, (
        f'上下文清单变成 {len(contexts)} 条（期望 20）—— 增删了要同步更新本断言，'
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
    names = _status_color_names()
    assert names == {'secondary', 'info', 'warning', 'success', 'danger'}, (
        f'从 getStatusColor 解析出的颜色名是 {sorted(names)}，'
        "期望 {'danger','info','secondary','success','warning'} —— "
        '五态映射变了，先确认是有意的再改本断言'
    )
    body = _js_function_body(_js(STATUS_JS), 'getStatusColor')
    fallbacks = set(re.findall(r"\|\|\s*'([a-z]+)'", body))
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

    命中数的边界：当前 3 处，全部在 static/js（tasks.js 2 处——活动行行2
    「| 失败: N」的 danger 计数，createTaskRow 与 updateTaskProgressPartial
    各一；history.js 1 处——历史小地图弹窗标题的 accent-hover），templates 0 处。
    （2026-08 统一流式列表重设计前是 10 处：9 列历史行里的四至箭头
    ▲▼▶◀ 四处 accent-hover、「本地文件」、两处空态提示等随表格一起删除，
    颜色全部收进 CSS 类——这正是内联色该去的方向。）
    断言只要求「两个 JS 都被扫到、templates 目录被扫到、且总数 >= 3」——
    钉死具体数字会在无关 UI 改动时误红，钉 0 则负向遍历永真。
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
                    # `${…}` 是 JS 模板插值：值在运行期从令牌解析（history.js 的
                    # InfoBox description 就是这样，因为令牌不跨 iframe 的
                    # document）。静态算不了它，也不该按「写不出对比度」判违规 ——
                    # 那条路由 test_infobox_description_token_pair_meets_wcag_aa
                    # （令牌对本身的对比度）与
                    # tests/test_tasks_js_contract.py 的
                    # test_cesium_infobox_description_carries_resolved_tokens
                    # （值必须是插值、不许写死）两条一起守。
                    if '${' in raw:
                        continue
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
    # >= 1：这个自检防的是「正则失效 -> 下面的负向遍历永真」。
    # 数字沿革：Vue 化前「失败: N」的红字在 history.js createTaskRow 与
    # tasks.js updateTaskProgressPartial 里各写一遍（同一段 markup 的两份实现），
    # 收口到 TaskRow 组件后只剩一处 -> 3 处降到 2 处。
    # 2026-08-15 再降到 1 处：history.js 那处 InfoBox 标题色从内联
    # `var(--color-accent-hover)` 改成运行期解析的 `${infoTitleColor}`（令牌不跨
    # iframe），被上面那句 `${` 跳过了。它没有失去守卫，换成了
    # test_infobox_description_token_pair_meets_wcag_aa 按**它真正所在的面**
    # （--color-bg-elevated）算 —— 而本条以前拿它跟任务卡面板底比，那个数字
    # 从来就没有对应任何真实渲染。
    # 除了数字，再钉一条「那处已知命中必须还在」：光看总数的话，正则坏掉后
    # 只要还剩一处能匹配上就照样过。
    assert len(hits) >= 1, (
        f'只扫到 {len(hits)} 处内联 color（期望 >= 1）—— '
        '正则失效的话下面的负向断言就是永真\n' + '\n'.join('  ' + h for h in hits)
    )
    assert any('task_list.js' in h for h in hits), (
        '扫不到 task_list.js 里「失败: N」那处内联 danger 色 —— 它是本条现在'
        f'唯一的活样本，扫不到就说明正则坏了（实际命中：{hits}）'
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
# `None` = 中性档：pending **故意**没有语义色（等待中还没开始）。对它的要求
# 反过来：最终色**不许**等于四个语义色中的任何一个 —— 中性态被画成青绿品牌色
# 正是本轮补掉的缺陷之一。
#
# 2026-08 §13-3 新增三态，**复用**已有的四档语义色，一个新颜色名都不加：
#   retrying            -> --color-info    与 running 同族：它就是在下载，只是第二遍。
#   pending_decision    -> --color-warning 与 paused 同族：都是「停下来等你」。
#   completed_with_gaps -> --color-warning **不是** success —— 产物带洞是一个必须
#                          被看见的事实，画成绿色等于把它伪装成干净的成功。
# 复用不是省事：每个新颜色名都要在 style.css 里配一条
# `.progress-bar.bg-X { ... !important }`（压 Bootstrap 自带 !important 的工具类，
# 见 test_important_count_under_control），而 !important 的上界是 37、实测 34 ——
# 三个新颜色名会把余量一次吃光。
# 一色多态因此是**允许**的：本表只要求「每个状态解析出它自己那一档」，不要求
# 八个状态互不同色。区分 paused / pending_decision / completed_with_gaps 的不是
# 颜色，而是 .task-status-text 的文字与带数字的 .task-gap-chip
#（WCAG 1.4.1「不只靠颜色」由它们承担，见 test_task_row_status_dot_covers_every_status）。
# --------------------------------------------------------------------------

_STATUS_SEMANTIC_TOKEN = {
    'pending': None,
    'running': '--color-info',
    'retrying': '--color-info',
    'paused': '--color-warning',
    'pending_decision': '--color-warning',
    'completed': '--color-success',
    'completed_with_gaps': '--color-warning',
    'failed': '--color-danger',
}


def _semantic_palette_values(css):
    return {
        _palette_var(css, t)
        for t in ('--color-info', '--color-warning', '--color-success', '--color-danger')
    }


def _status_color_map(js_name=STATUS_JS):
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
    中性档（pending）反向断言 —— 不许等于四个语义色中的任何一个。

    覆盖边界：8 个状态 = 8 组，先钉组数再逐组比对。
    （改前是 2 个文件 × 6 = 12 组：getStatusColor 在 tasks.js / history.js
    各有一份、且各含 cancelled；实现已收口到 static/js/task_status.js，
    状态随「取消任务」下线减为五态，2026-08 §13-3 又加回三态
    —— retrying / pending_decision / completed_with_gaps，见 _STATUS_SEMANTIC_TOKEN
    上方那段「为什么复用已有颜色名」。）
    """
    css = _css()
    semantic = _semantic_palette_values(css)
    checked, problems = [], []
    cmap = _status_color_map()
    assert set(cmap) == set(_STATUS_SEMANTIC_TOKEN), (
        f'{STATUS_JS} 的 getStatusColor 键集合是 {sorted(cmap)}，'
        f'期望 {sorted(_STATUS_SEMANTIC_TOKEN)} —— 先修 '
        'test_status_map_covers_every_backend_status'
    )
    for status, token in _STATUS_SEMANTIC_TOKEN.items():
        name = cmap[status]
        chain = [_TextEl('div', {'card'}), _TextEl('div', {'card-body'}),
                 _TextEl('span', {'badge', f'bg-{name}'})]
        _branch, raw = _winning_color_decl(css, chain, f'{status} 徽章')
        got = _resolve_color(css, raw)
        checked.append(f'{status} -> bg-{name} -> {got}')
        if token is None:
            if got in semantic:
                problems.append(
                    f'{status!r} 是中性档，却映射到 bg-{name}，'
                    f'解析出语义色 {got} —— 它会冒充「运行中/已完成/失败/已暂停」')
        else:
            want = _palette_var(css, token)
            if got != want:
                problems.append(
                    f'{status!r} -> bg-{name} -> {got}，'
                    f'期望 {token}({want})')
    assert len(checked) == 8, f'只检查了 {len(checked)} 组（期望 8）—— 本测试已失效'
    assert not problems, (
        '状态与语义色的配对错了：\n' + '\n'.join('  ' + p for p in problems)
        + '\n\n全部映射：\n' + '\n'.join('  ' + c for c in checked)
    )


def test_task_row_status_dot_covers_every_status():
    """行1 的 8px 状态点：八态**每一态**都要有自己的规则，且色对、够看得见。

    （前身 test_task_row_status_bar_covers_every_status。2026-08 统一流式
    列表重设计：4px 状态左条随 9 列表格一起废除，状态识别改由行1 的
    .task-dot 圆点 + 小字状态文本承担。接替的这条同时是 WCAG 1.4.1
    「不只靠颜色」链条的图形侧——文字侧由 getStatusText 的八态词表断言守，
    原徽章 SVG 图标表断言 test_status_icons_are_real_
    distinct_glyphs 随徽章 pill 删除，登记在 tests/test_tasks_js_contract.py。）

    三件事一起断言：
      1. 八态各有一条顶层规则（缺一条就会落到 .task-dot 的兜底色——
         兜底是品牌色，「失败」掉到品牌蓝 = 状态信号丢失）；
      2. 语义档的色值等于对应令牌，中性档不许等于任何语义色；
      3. 对面板底 >= 3:1 —— 它是图形元素不是文字，走 WCAG 1.4.11 的下限。

    ⚠️ 本条**不**要求八种颜色互不相同：§13-3 的三个新态刻意复用已有语义色
    （retrying=info，pending_decision / completed_with_gaps=warning），理由见
    _STATUS_SEMANTIC_TOKEN 上方。同色两态的区分由 .task-status-text 的文字与带
    数字的 .task-gap-chip 承担 —— 那才是 1.4.1 要的「不只靠颜色」，多凑三个
    互不相同的色号并不能替代它。
    """
    css = _css()
    panel = _effective_task_card_backdrop(css)
    semantic = _semantic_palette_values(css)
    fallback = _resolve_color(css, _branch_background(css, '.task-dot'))
    problems, report = [], []
    for status, token in _STATUS_SEMANTIC_TOKEN.items():
        branch = f'.task-row.status-{status} .task-dot'
        rules = [
            (sel, body) for sel, body, at in _rules_ctx(css)
            if not at and branch in _selector_parts(sel)
            and ('background' in _decl_map(body) or 'background-color' in _decl_map(body))
        ]
        if len(rules) != 1:
            problems.append(
                f'`{branch}` 有 {len(rules)} 条声明了背景色的顶层规则（应恰好 1 条）。'
                f'一条都没有 = 落到 `.task-dot` 的兜底 {fallback}，'
                '该态的状态点与「等待中」同色，状态信号丢失')
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
    assert len(report) + len([p for p in problems if '条声明了背景色' in p]) == 8, (
        '没有恰好检查 8 个状态 —— 本测试已失效'
    )
    assert not problems, (
        '状态点配色有问题：\n' + '\n'.join('  ' + p for p in problems)
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


def _time_to_seconds(tok, css=None, _depth=0):
    """`0.2s` / `150ms` / `0.01ms` / `var(--dur-base)` -> 秒（float）。不是时间返回 None。

    2026-08-15 Task 3：动效令牌化之后 `transition` 里的时长不再是字面量，而是
    `var(--dur-fast|-base|-slow)`。这不是「放宽」，是**补上模型看不懂的写法** ——
    改前这个函数遇到 `var()` 返回 None，而 `_motion_computed` 对 None 是响亮
    assert，所以令牌化的第一次运行就红在
    test_reduced_motion_actually_stops_every_animated_element 上（实测，不是推算）。
    这正是本文件想要的形态：模型宁可拒绝也不猜。这里把「跟一层层 var()」这个
    能力补进去，口径与 `_resolve_length_px` 完全一致 —— 不支持 `var(--x, 回退)`
    与 `calc()`，跟不下去仍然返回 None。

    不给 `css` 时行为与改前逐字相同（返回 None），所以老调用方不受影响。
    """
    tok = tok.strip()
    m = re.fullmatch(r'var\(\s*(--[-\w]+)\s*\)', tok)
    if m:
        if css is None or _depth > 4:
            return None                      # 自引用/环，别死循环
        return _time_to_seconds(_custom_property_raw(css, m.group(1)) or '', css, _depth + 1)
    m = _TIME_RE.match(tok)
    if not m:
        return None
    return float(tok[:-len(m.group(1))]) / (1000.0 if m.group(1).lower() == 'ms' else 1.0)


def _is_time_token(tok, css=None):
    """这个 token 是不是一个 <time>（含解析得开的 `var(--dur-*)`）。

    单独一个函数而不是各处写 `_TIME_RE.match(...)`：简写展开时「哪个 token 是
    时长」这个判断出现在 4 处（animation 的 duration / name，transition 的
    duration / property），漏改一处的表现是「某条过渡在模型里凭空消失」——
    不报错，只是断言少覆盖一条规则。
    """
    return _time_to_seconds(tok, css) is not None


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


def _expand_motion_decls(body, css=None):
    """规则体 -> [(长写属性名, 值, important, 声明序号)]，简写已展开。

    简写**必须**展开，理由与 _BTN_SHORTHAND_EXPANSIONS 完全相同：
    `animation: fadeInUp .5s` 和 `animation-name: fadeInUp` 在浏览器里等价，
    只认其中一种写法的断言，换另一种写法就能绕过去。

    简写里 <time> 的顺序按规范：**第一个是 duration，第二个是 delay**。
    只要认「有个 s 结尾的 token」而不管顺序，`animation: pulse 0s 2s` 这种
    会被读成 2s，正好把「时长压到 0」的检查骗过去。

    `css`（2026-08-15 Task 3）：只用来让 `_is_time_token` 跟得开
    `var(--dur-*)`。不传时行为与改前逐字相同 —— 时长令牌会被当成「不是时长」，
    于是 `durs` 拿到 `'0s'`，那条过渡在模型里凭空消失。所以**每个调用方都要传**，
    两处调用方（`_motion_computed` / `_motion_rule_index`）都已经手里有 css。
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
                times = [t for t in toks if _is_time_token(t, css)]
                iters = [t for t in toks
                         if t.lower() == 'infinite' or re.fullmatch(r'\d+\.?\d*', t)]
                names = [t for t in toks
                         if not _is_time_token(t, css)
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
                times = [t for t in toks if _is_time_token(t, css)]
                names = [t for t in toks
                         if not _is_time_token(t, css)
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
        decls = _expand_motion_decls(body, css)
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
    anim_dur = _time_to_seconds(_split_commas_outside_parens(dur_raw)[0], css)
    assert anim_dur is not None, f'读不懂的 animation-duration: {dur_raw!r}'

    tprops = [p.strip().lower() for p in
              _split_commas_outside_parens(val_of('transition-property', 'all'))]
    tdurs_raw = _split_commas_outside_parens(val_of('transition-duration', '0s'))
    tdurs = []
    for d in tdurs_raw:
        s = _time_to_seconds(d, css)
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
        if not _expand_motion_decls(body, css):
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
#
# 28 -> 31（GIS 工作台改版）：新增 3 个分支，都有过渡且都在 reduce 块的
# 通用选择器 `*` 覆盖范围内，无需豁免登记：
#   1. `.index-right` —— dock 收起的 margin-right 0.2s 过渡
#   2. `.dock-collapse-btn` / 3. `.dock-reopen-handle` —— hover 底色 0.15s
# 31 -> 33（覆盖面板）：`.panel-backdrop` 的 opacity 0.2s、`.workbench-panel`
# 的 transform 0.22s，同样在 `*` 覆盖范围内。
# 33 -> 32（移除顶部工具栏）：删 `.navbar-brand` / `.nav-link` /
# `.nav-link::after` 三个分支，加 `.map-panel-btn` / `.back-home-link`
# 两个（hover 0.15s，在 `*` 覆盖范围内）。
# 32 -> 33（地图样式缩略图）：`.map-style-preview` 的 transform 0.15s。
# 33 -> 39（2026-07 UX 改版）：dock 移除 + splash/状态栏/bounds 交互新增。
#   删 3 个分支：`.index-right`、`.index-right::-webkit-scrollbar-thumb`、
#     `.dock-reopen-handle`（dock 整体移除，下载/处理改为按需弹窗）。
#   加 9 个分支：`.splash-screen`（opacity 淡出）、`.splash-grid` /
#     `.splash-scan` / `.splash-logo` / `.splash-logo-dot`（splash 动效）、
#     `.splash-bar`（进度条 width）、`.bounds-v`（点击编辑 hover 底色）、
#     `.statusbar-tasks`（hover 底色）、`.statusbar-progress__bar`
#     （状态栏聚合进度 width）。全部在 reduce 块通用选择器 `*` 覆盖范围内，
#     无需豁免登记。
# 39 -> 37（统一任务表改版，活动任务卡片 -> 实时行）：
#   删 3 个分支：`.task-card`（交互过渡）、`.task-card::before`（左边条
#     width/底色过渡）、`.task-card.status-running::before`（pulse）。
#   加 1 个分支：`.task-row.status-running .task-row__bar::before`（同一个
#     pulse，从卡片边条迁到行首单元格边条；行本身不声明任何过渡——
#     行随任务增删整行重建，过渡没有意义）。
# 37 -> 37（2026-08 统一流式列表重设计，纯改名）：
#   删 1 个分支：`.task-row.status-running .task-row__bar::before`
#     （4px 状态左条随 9 列表格废除）。
#   加 1 个分支：`.task-row.status-running .task-dot`（同一个 pulse，
#     从左条迁到行1 的 8px 状态点——「任务还活着」的信号保留，形态换成
#     脉冲环）。列表其它元素（行/chips）刻意不声明任何过渡/动画，
#     行随 socket 事件整体重建（outerHTML 原地替换），过渡没有意义。
#     （2026-08 第二轮：分组头随三分区删除，从列举中去掉；分支数不变。）
# 37 -> 38（2026-08 状态栏完善）：`.statusbar-copy`（坐标/选区四至读数项
#   的 hover 底色 0.15s，与 `.statusbar-tasks` 同款，在 reduce 块 `*`
#   覆盖范围内）。
# 38 -> 36（U10 删死代码）：`.history-table tbody tr`（表格时代残留，模板/JS
#   零引用）与 `.action-buttons .btn`（同批残留）随整段删除一并消失。
# 36 -> 38（配置页说明图标 .hint + 代理状态转圈）：
#   删 1 个分支：`.config-section .btn`（那条把配置页按钮撑成 47px 粗体的大
#     padding 规则整条移除，它带一条 background-color/border-color/... 过渡）。
#   加 3 个分支：`.hint`（图标色 0.15s）、`.hint::after`（气泡 opacity/
#     visibility 0.12s）、`.hint-spin`（检测中的 rotate 无限循环）。
#     前两个在 reduce 块通用选择器 `*` / `*::after` 覆盖范围内；`.hint-spin`
#     被 `*` 覆盖。都无需豁免登记。
#     ⚠️ `.hint-spin` 刻意写成单类而不是 `.hint.is-busy > svg`。
#     （2026-08-14 更新理由：层叠模型 `_text_branch_applies` 现在支持子组合符了，
#     动画模型也在消费它，所以原来写的「子组合符会让本节 20 条断言集体判模型已
#     失效」这个机制**已经不存在**。结论不变，但卡点换了人：
#     `_motion_contexts_from_stylesheet` 仍然用 `branch.split()` 拆选择器、再对
#     每段断言 `_parse_compound(...) is not None`，而 `_parse_compound('>')` 返回
#     None —— 所以 `>` 会在**构造上下文**的时候就炸，表现为
#     test_reduced_motion_actually_stops_every_animated_element 这一条失败，
#     不再是 20 条集体失效。要在动画规则里写子组合符，先把那个函数改成用
#     `_split_branch`。）
# 38 -> 37（删死代码）：`.status-badge` 基规则带一条 background-color/
#   border-color/color/box-shadow 过渡，随整个组件删除。任务行早已改用
#   `.task-dot` + `.task-status-text`，全仓只剩 history.js / tasks.js 两处
#   注释在提这个类名，没有任何 markup 会带上它。少掉的分支就是 `.status-badge`。
# 37 -> 39（地形预览的进场薄雾）：`.map-transition-veil`（opacity 0.35s，
#   淡出档）与 `.map-transition-veil--in`（transition-duration 0.18s，淡入档）。
#   换 viewer.terrainProvider 是一次没有中间态的整球几何重建，影像层那种
#   alpha 淡入在地形上没有对应物，只能罩一层雾盖过去（map.js _showMapVeil）。
#   两条都在 reduce 块 `*` 覆盖范围内，无需豁免登记。
# 39 -> 40（全窗口拖拽遮罩）：`.drop-veil`（opacity 0.15s,P2 拖拽提示）。
#   纯展示无 pointer-events,在 reduce 块 `*` 覆盖范围内,无需豁免登记。
# 40 -> 39（取消面板遮罩层）：`.panel-backdrop` 随遮罩删除,其 opacity 0.2s
#   过渡一并移除(2026-08-11 面板非模态化)。
# 42 -> 42（2026-08-15 Task 3 动效令牌化）：**一个分支都没增删**，本次只把时长
#   字面量换成 `var(--dur-fast|-base|-slow)`、把 `ease` 换成 `var(--ease)`，
#   并把三条「条子在长」的过渡（.progress-bar / .splash-bar /
#   .statusbar-progress__bar）统一到 --dur-base。锚点因此不动，这行只为下一个
#   读者留个交待：**时长现在是 var()，不是字面量**。
#   连带的模型改动（不改这个数，但会影响下面每一条断言的读数）：
#   `_time_to_seconds` / `_expand_motion_decls` 现在跟一层 var()。不补这个能力
#   的话，`_is_time_token` 认不出 `var(--dur-base)`，`durs` 拿到 '0s'，那条过渡
#   会在模型里**凭空消失** —— 实测就是响亮的红
#   （test_reduced_motion_actually_stops_every_animated_element：
#    「读不懂的 transition-duration: 'var(--dur-base)'」），不是静默失覆盖。
# 42 -> 45（2026-08-15 Task 6 浮层入场统一成一套）：层栈里的浮层此前有四种入场
#   写法（面板 transform/--dur-base、confirm 遮罩 opacity/--dur-base + 卡片
#   transform/--dur-base、拖拽遮罩 opacity/**--dur-fast**、命令面板与速查表
#   **完全没有**入场），现在统一为「遮罩担 opacity、卡片担 transform，两条都是
#   --dur-base + --ease」。
#   加 3 个分支：`.cmdk`（opacity，改前刻意不加 transition）、`.cmdk__dialog`
#     （translateY(-8px) -> 0）、`.drop-veil__tip`（scale(0.96) -> 1，transform
#     落在提示胶囊上而不是 inset:0 的满屏遮罩上——缩放满屏层会在四边露底）。
#   零删除。另有两处**不增删分支**的就地修改：`.workbench-panel` 补上 opacity
#     （它是唯一「遮罩与卡片同体」的浮层，两条属性都在自己身上），`.drop-veil`
#     的 --dur-fast 改 --dur-base（它做的是整层显隐，不是 hover 反馈）。
#   三个新分支都是纯类选择器，落在 reduce 块 `*` 的覆盖范围内，无需豁免登记。
# 45 -> 46（2026-08-17 液态玻璃 Task 1）：加 1 个分支 `.tf-glass`
#   （box-shadow + border-color 两条过渡，时长走 `var(--liquid-motion)`——
#   该令牌是「时长 + 缓动」的整体，不是单独的 --dur-* 档位）。
#   它是纯类选择器，落在 reduce 块 `*` 的覆盖范围内，无需豁免登记。
#   ⚠️ 液态玻璃自带的 `@media (prefers-reduced-motion: reduce)` 块**刻意只写**
#   `.tf-glass::after { content: none }`（关流光），**不写** transition 覆盖：
#   统一的 `*, *::before, *::after` 块已经把 transition-duration 压到 0.01ms，
#   再写一条既是死声明，又会让下面那条「reduce 块里正好 3 个分支」变红 ——
#   那条断言正是用来守「减少动态集中在一处、不许各组件各写各的」。
_MOTION_BRANCH_COUNT = 46


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
            '交互反馈请写在具体的交互元素上（.btn / .form-control / .card …）'
        )
        assert got['animation_name'] == 'none', (
            f'裸 <{node.tag}> 身上挂了动画 {got["animation_name"]!r}'
        )


def test_cards_have_no_entrance_animation_but_running_dot_still_pulses():
    """两件事一起钉：入场动画没了，**而状态动画还在**。

    只断言「.task-row 没有 animation」是不够的 —— 把整节动画全删光也能通过，
    而那样会连「运行中的任务状态点在呼吸」这个**传达状态**的信号一起丢掉。
    所以第二半是正面断言：.task-row.status-running 行1 的 .task-dot
    必须仍然跑 pulse、仍然是无限循环。
    （2026-08 统一流式列表重设计：pulse 的宿主从 4px 左条
    .task-row__bar::before 迁到 8px 状态点 .task-dot——左条随 9 列表格废除，
    信号本身保留，断言跟着结构走。）

    算的是层叠胜出值，不是「文件里有没有 fadeInUp 这个词」：把
    `.task-row { animation: fadeInUp .5s }` 换个地方重写一遍照样会红。
    """
    css = _css()
    # 静态面板 .card（div）与任务行 .task-row（div）都不许有入场动画
    card = _motion_computed(css, [
        _TextEl(tag='body'),
        _TextEl(tag='div', classes=('card',))])
    assert card['animation_name'] == 'none', (
        f'.card 又挂上了入场动画 {card["animation_name"]!r}'
    )
    for classes in (('task-row',), ('task-row', 'status-running'),
                    ('task-row', 'status-failed')):
        got = _motion_computed(css, [
            _TextEl(tag='body'),
            _TextEl(tag='div', classes=('card',)),
            _TextEl(tag='div', element_id='historyTableBody'),
            _TextEl(tag='div', classes=classes)])
        assert got['animation_name'] == 'none', (
            f'.{".".join(classes)} 又挂上了入场动画 {got["animation_name"]!r}'
            f'（{got["animation_duration"]}s）。任务列表每次成员变化都整体重建 '
            'innerHTML，入场动画会被集体重放：CDP 实测 5 张卡的场景下，'
            '「新任务到达」和「任一任务完成」各触发 fadeInUp × 5'
        )

    pulse = _motion_computed(css, [
        _TextEl(tag='body'),
        _TextEl(tag='div', classes=('card',)),
        _TextEl(tag='div', element_id='historyTableBody'),
        _TextEl(tag='div', classes=('task-row', 'status-running')),
        _TextEl(tag='div', classes=('task-line1',)),
        _TextEl(tag='span', classes=('task-dot',))])
    assert pulse['animation_name'] == 'pulse', (
        '运行中任务的状态点不再跑 pulse —— 这是「任务还活着」的唯一视觉信号，'
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
#   src/services/task_manager.py 的 progress_callback 历史上**每下载一块瓦片**就
#   `socketio.emit('task_progress', ...)` 一次（现已按
#   PROGRESS_EMIT_MIN_INTERVAL=0.5s 时间节流，推送频率只会更低，本约束更宽松），
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
    # 真实 markup（tasks.js createTaskRow，统一流式行）：
    #   div.task-row > .task-progress-line > .task-progress > .progress-bar
    # 祖先必须建模到位，否则 `.progress .progress-bar { transition: width 1s }`
    # 这条压回去的规则算不进来 —— 变异 M8 实测过，当时本断言是假绿。
    got = _motion_computed(_css(), [
        _TextEl(tag='div', classes=('task-row',)),
        _TextEl(tag='div', classes=('task-progress-line',)),
        _TextEl(tag='div', classes=('task-progress',)),
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
#   ::-webkit-scrollbar-thumb（`*::-webkit-scrollbar-thumb` 一条，0.15s 的
#   background-color 过渡；dock 时代的 `.index-right::-webkit-scrollbar-thumb`
#   已随 dock 移除删除）
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
_REDUCED_MOTION_EXEMPT = {'::-webkit-scrollbar-thumb'}


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
    # 25 -> 28（GIS 工作台改版）：新增 .index-right / .dock-collapse-btn /
    # .dock-reopen-handle 三个上下文，均为普通过渡，走通用豁免之外的正常检查。
    # 28 -> 30（覆盖面板）：.panel-backdrop / .workbench-panel 两个上下文，同理。
    # 30 -> 29（移除顶部工具栏）：删 .navbar-brand / .nav-link / .nav-link::after
    # 三个上下文，加 .map-panel-btn / .back-home-link 两个。
    # 29 -> 30（地图样式缩略图）：.map-style-preview 的 transform 过渡。
    # 30 -> 36（2026-07 UX 改版）：删 .index-right（含 scrollbar-thumb 伪元素）/
    # .dock-reopen-handle 三个上下文（dock 移除）；加 .splash-screen /
    # .splash-grid / .splash-scan / .splash-logo / .splash-logo-dot /
    # .splash-bar / .bounds-v / .statusbar-tasks / .statusbar-progress__bar
    # 九个（splash 与状态栏/bounds 交互，均在 reduce 块 `*` 覆盖范围内）。
    # 36 -> 34（统一任务表改版）：删 .task-card / .task-card::before /
    # .task-card.status-running::before 三个卡片上下文，加
    # .task-row.status-running .task-row__bar::before 一个行上下文。
    # 34 -> 34（2026-08 统一流式列表重设计，纯改名）：
    # .task-row.status-running .task-row__bar::before 换成
    # .task-row.status-running .task-dot（pulse 从左条迁到状态点）。
    # 34 -> 35（2026-08 状态栏完善）：加 .statusbar-copy（坐标/选区四至
    # 读数项的 hover 底色，在 reduce 块 `*` 覆盖范围内）。
    # 35 -> 33（U10 删死代码）：.history-table tbody tr 与 .action-buttons .btn
    # 两个上下文随零引用的表格时代残留一起删除。
    # 33 -> 35：`.hint`（+ `::after` 伪元素上下文）与 `.hint-spin` 三个新分支
    # 反解出 2 个新元素上下文（button.hint 与它的 ::after）。
    # 35 -> 34（删死代码）：`.status-badge` 随整个零引用组件从 style.css 删除，
    # 它反解出的那一个元素上下文（span.status-badge）一并消失。
    # 34 -> 36（地形预览的进场薄雾）：`.map-transition-veil` 与它的亮起态
    # `.map-transition-veil--in` 各反解出一个上下文，都是普通 opacity 过渡，
    # 在 reduce 块 `*` 覆盖范围内，不进豁免清单。
    # 36 -> 37（全窗口拖拽遮罩）：`.drop-veil` 反解出一个上下文，
    # 普通 opacity 过渡，在 reduce 块 `*` 覆盖范围内，不进豁免清单。
    # 37 -> 36（取消面板遮罩层）：`.panel-backdrop` 上下文随遮罩删除。
    # 36 -> 37（地点搜索挪到顶部居中）：`.map-search__panel` 反解出一个上下文
    # （下拉面板的开合过渡，opacity + transform），在 reduce 块 `*` 覆盖范围内，
    # 不进豁免清单。亮起态 `--in` 只改终值、不声明 transition，不额外反解。
    # 37 -> 38（搜索结果区的换内容淡入）：`.place-search__results` 反解出一个
    # 上下文。面板已开时它的开合过渡不再触发，换内容是硬切 —— 这一条补的就是
    # 那一半（用户反馈「搜索还是没有动画」）。同样是普通 opacity 过渡。
    # 38 -> 39（同上的归零态）：`.place-search__results--fresh` 声明的是
    # `transition: none`，扫描器按「出现了 transition 声明」计数，所以它也算
    # 一个上下文。它本来就没有动效，reduce 块对它是恒真的。
    # 39 -> 42（2026-08-15 Task 6 浮层入场统一成一套）：`.cmdk`、`.cmdk__dialog`、
    # `.drop-veil__tip` 三个新分支各反解出一个上下文（opacity / transform 的
    # 普通过渡），都在 reduce 块 `*` 覆盖范围内，不进豁免清单。`.workbench-panel`
    # 补 opacity、`.drop-veil` 换时长档都是就地改，不新增上下文；亮起态
    # `.cmdk--in` / `.drop-veil--in .drop-veil__tip` 只改终值、不声明 transition，
    # 同样不额外反解。
    # 42 -> 43（2026-08-17 液态玻璃 Task 1）：`.tf-glass` 反解出一个上下文
    # （box-shadow + border-color 的普通过渡），纯类选择器，在 reduce 块 `*`
    # 覆盖范围内，不进豁免清单。档位类 `.tf-glass--1/--3` 只翻转自定义属性、
    # 不声明 transition，不额外反解。
    assert len(ctxs) == 43, (
        f'反解出 {len(ctxs)} 个带动效的元素上下文，锚点是 43：\n'
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


# --------------------------------------------------------------------------
# vendor 本地化：离线可用性护栏
#
# 背景：本项目是 PyInstaller 打包的**离线桌面工具**，第三方前端资源（Bootstrap /
# Leaflet / Leaflet.draw / Socket.IO / Google Fonts）全部落到 static/vendor/。
# 在此之前它们走 6 个 CDN，断网时界面大面积降级：弹窗打不开、绘制工具条三个
# 按钮变空白、栅格塌掉、字体退回系统默认。
#
# 这一节钉的是**「离线还能不能用」这件事本身**——在本节之前，没有任何一条断言
# 看得见它：把某个 <link> 改回 CDN、漏掉一个二级资源（雪碧图 / woff2）、
# 或者让 .gitignore 把 vendor 文件吃掉，测试全绿而用户拿到的是裸页面。
#
# 底图瓦片（map.js / history.js 里的 OSM 公网瓦片）**不在本节范围**：
# 用户明确决定「先只做库本地化，底图单独议」。所以断网时地图区域仍是灰格子，
# 那是**预期行为**，不是本节该拦的缺陷。下面的扫描只覆盖 templates/ 与
# static/**/*.css，不覆盖 static/js/。
# --------------------------------------------------------------------------

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static'
)
_VENDOR_DIR = os.path.join(_STATIC_DIR, 'vendor')

# vendor 清单：相对 static/vendor/ 的路径 -> 精确字节数。
#
# 为什么钉**精确字节数**而不是「文件存在就行」：存在性挡不住「下成了未压缩版」
# 「下成了别的构建」「下到一半截断」这三种。它们都不会报错，只会让页面悄悄变样。
# 与 test_font_size_scale_variables_unchanged 同一逻辑——拦的是「悄悄改」，
# 不是「不许改」：真要升版本，改路径里的版本号 + 改这里的数字，一眼可见。
#
# fonts/fonts.css 不在这里：它是**本仓库生成的**（上游 40 个 @font-face 裁成
# latin/latin-ext 两个子集、URL 改成本地相对路径），改一行注释字节数就变，
# 钉死只会制造无谓的红。它由 test_local_font_css_is_self_contained 按结构校验。
VENDOR_MANIFEST = {
    'bootstrap/5.3.0/bootstrap.min.css': 232914,
    'bootstrap/5.3.0/bootstrap.bundle.min.js': 80421,
    'socket.io/4.5.4/socket.io.min.js': 44191,
    # Vue 3 global build（含 runtime compiler —— 组件用 template 字符串写，
    # 全站无构建步骤，这是选 global build 而不是 runtime-only 的唯一理由）。
    # 渲染任务时间流，见 static/js/task_list.js。
    'vue/3.5.13/vue.global.prod.js': 157924,
    'fonts/inter-latin.woff2': 48256,
    'fonts/inter-latin-ext.woff2': 85068,
    'fonts/jetbrains-mono-latin.woff2': 31432,
    'fonts/jetbrains-mono-latin-ext.woff2': 11624,
    # 各组件自带的上游许可证全文，从各自上游仓库取回（此前 static/vendor/ 下
    # 一个许可证文件都没有）。Apache-2.0 与 OFL 1.1 都要求随附全文，漏一个就是
    # 分发不合规；钉字节数同样是为了拦「下到一半」与「下成了别的版本」。
    # 索引与说明见根目录 THIRD_PARTY_NOTICES.md。
    'bootstrap/5.3.0/LICENSE': 1093,
    'cesium/1.143.0/LICENSE.md': 55506,
    'fonts/LICENSE-Inter.txt': 4380,
    'fonts/LICENSE-JetBrainsMono.txt': 4399,
    'socket.io/4.5.4/LICENSE': 1096,
    'vue/3.5.13/LICENSE': 1112,
}

# CesiumJS 1.143.0（157 个文件，上游发行版是 390 个）：workers / assets /
# widgets 全部由 Cesium.js 运行时按 CESIUM_BASE_URL 动态拉取，模板 grep
# 不出来，必须登记。
#
# 2026-08 瘦身：删掉 233 个本项目**实测零请求**的文件（14 MB -> 11 MB）。
# 判定方法不是看名字猜，是用 CDP 监听 network，把首页地图 / 框选绘制 /
# 地形加载（quantized-mesh 真解码）/ 光照 / 历史小地图 全流程走一遍，
# 记录实际请求过的 URL；再逐条回到 Cesium.js 里确认触发条件。删掉的是：
#   ThirdParty/ 全部 —— basis(KTX2) / draco / wasm_splats / zip(KMZ) /
#     google-earth-dbroot，都是 glTF·3D Tiles·KMZ·Google Earth Enterprise
#     的依赖，本项目一个都不用
#   Assets/Textures/NaturalEarthII —— BaseLayerPicker 的 Cesium ion 底图，
#     而两个 Viewer 都是 baseLayerPicker:false
#   Assets/Textures/maki —— PinBuilder 的图标集，本项目用 point 不用 pin
#   Assets/Textures/LensFlare —— 镜头光晕后处理，默认关闭
#   Assets/Textures/waterNormals.jpg —— Globe 只引用 waterNormalsSmall.jpg
#   Widgets/Images/{ImageryProviders,TerrainProviders,NavigationHelp} ——
#     对应的 widget 全部 false
#   Workers/{decodeGoogleEarthEnterprisePacket,transcodeKTX2}.js —— 同上
# ⚠️ Workers/ 的其余文件**刻意保留**：实测全流程零请求（1.143 把 worker
# 内联进了 Cesium.js），但 `_defaultWorkerModulePrefix="Workers/"` 说明
# TaskProcessor 仍可能在未覆盖到的路径上按这个前缀取模块，1.2 MB 不值得赌。
#
# ⚠️ Assets/Textures/SkyBox 与 moonSmall.jpg 已删 —— 两个 Viewer 都传
# `skyBox: false` 并把 scene.moon 置空（见 map.js / history.js）。
# skyAtmosphere（地球边缘的蓝色大气辉光）**保留**：它是 shader 算的、不吃贴图。
#
# ⚠️ Assets/IAU2006_XYS 只保留 18–27 号，删掉 0–17。这是**按时间分段**裁的，
# 不是拍脑袋：Cesium 的 Iau2006XysData 参数为
# sampleZeroJulianEphemerisDate=2442396.5 / stepSizeDays=1 / samplesPerXysFile=1000，
# 即每个文件覆盖 1000 天（约 2.74 年），28 个文件覆盖 1974-12 → 2050-01。
# 本工具算的永远是「当前时间」的太阳位置（地形光照），不查历史日期，
# 所以 0–17（1974-12 → 2024-03）是死数据。保留的 18 号覆盖
# 2024-03-27 → 2026-12-22，与实测请求的文件号吻合；末号 27 到 2050-01。
# ⚠️⚠️ 这批文件**不能全删**：实测（必须禁用浏览器缓存，否则是假绿灯）
# 显示 Cesium 会真的去请求当前时间对应的那一个，全删就是每次启动一个
# 404 + 一条控制台红字。加载失败处 Cesium 是空 `catch{}`，功能会静默降级
# 而不报错 —— 正因为不报错，只靠「功能还正常」判断会漏掉这个 404。
#
# ⚠️ Cesium.js 有一处**有意的本地补丁**（2026-08，用户要求）：CesiumWidget
# 构造时创建的 .cesium-widget-credits div 不再挂载进 DOM（删了
# c.appendChild(a)，原位留有 TERRAFORGE-PATCH 注释标记）——去掉左下角的
# Cesium ion logo、底图版权文字和「Data attribution」链接（离线桌面工具，
# 用户明确不保留）。上游原版是 5909848 B，补丁后为下方数字；
# 升级 Cesium 版本时这个补丁需要重新打。
VENDOR_MANIFEST.update({
    'cesium/1.143.0/Assets/IAU2006_XYS/IAU2006_XYS_18.json': 65310,
    'cesium/1.143.0/Assets/IAU2006_XYS/IAU2006_XYS_19.json': 65537,
    'cesium/1.143.0/Assets/IAU2006_XYS/IAU2006_XYS_20.json': 65328,
    'cesium/1.143.0/Assets/IAU2006_XYS/IAU2006_XYS_21.json': 64843,
    'cesium/1.143.0/Assets/IAU2006_XYS/IAU2006_XYS_22.json': 64977,
    'cesium/1.143.0/Assets/IAU2006_XYS/IAU2006_XYS_23.json': 66084,
    'cesium/1.143.0/Assets/IAU2006_XYS/IAU2006_XYS_24.json': 64894,
    'cesium/1.143.0/Assets/IAU2006_XYS/IAU2006_XYS_25.json': 64953,
    'cesium/1.143.0/Assets/IAU2006_XYS/IAU2006_XYS_26.json': 65311,
    'cesium/1.143.0/Assets/IAU2006_XYS/IAU2006_XYS_27.json': 27595,
    'cesium/1.143.0/Assets/Images/bing_maps_credit.png': 18831,
    'cesium/1.143.0/Assets/Images/cesium_credit.png': 4242,
    'cesium/1.143.0/Assets/Images/google_earth_credit.png': 7703,
    'cesium/1.143.0/Assets/Images/ion-credit.png': 6028,
    'cesium/1.143.0/Assets/Textures/pin.svg': 348,
    'cesium/1.143.0/Assets/Textures/waterNormalsSmall.jpg': 34121,
    'cesium/1.143.0/Assets/approximateTerrainHeights.json': 299471,
    'cesium/1.143.0/Cesium.js': 5909997,
    'cesium/1.143.0/Widgets/Animation/Animation.css': 2748,
    'cesium/1.143.0/Widgets/Animation/lighter.css': 1919,
    'cesium/1.143.0/Widgets/BaseLayerPicker/BaseLayerPicker.css': 2544,
    'cesium/1.143.0/Widgets/BaseLayerPicker/lighter.css': 734,
    'cesium/1.143.0/Widgets/Cesium3DTilesInspector/Cesium3DTilesInspector.css': 2431,
    'cesium/1.143.0/Widgets/CesiumInspector/CesiumInspector.css': 2633,
    'cesium/1.143.0/Widgets/CesiumWidget/CesiumWidget.css': 2265,
    'cesium/1.143.0/Widgets/CesiumWidget/lighter.css': 307,
    'cesium/1.143.0/Widgets/FullscreenButton/FullscreenButton.css': 193,
    'cesium/1.143.0/Widgets/Geocoder/Geocoder.css': 1811,
    'cesium/1.143.0/Widgets/Geocoder/lighter.css': 495,
    'cesium/1.143.0/Widgets/I3SBuildingSceneLayerExplorer/I3SBuildingSceneLayerExplorer.css': 636,
    'cesium/1.143.0/Widgets/Images/TimelineIcons.png': 781,
    'cesium/1.143.0/Widgets/Images/info-loading.gif': 723,
    'cesium/1.143.0/Widgets/InfoBox/InfoBox.css': 1950,
    'cesium/1.143.0/Widgets/InfoBox/InfoBoxDescription.css': 4675,
    'cesium/1.143.0/Widgets/NavigationHelpButton/NavigationHelpButton.css': 2074,
    'cesium/1.143.0/Widgets/NavigationHelpButton/lighter.css': 1063,
    'cesium/1.143.0/Widgets/PerformanceWatchdog/PerformanceWatchdog.css': 371,
    'cesium/1.143.0/Widgets/ProjectionPicker/ProjectionPicker.css': 1216,
    'cesium/1.143.0/Widgets/SceneModePicker/SceneModePicker.css': 1794,
    'cesium/1.143.0/Widgets/SelectionIndicator/SelectionIndicator.css': 472,
    'cesium/1.143.0/Widgets/Timeline/Timeline.css': 2910,
    'cesium/1.143.0/Widgets/Timeline/lighter.css': 467,
    'cesium/1.143.0/Widgets/VRButton/VRButton.css': 169,
    'cesium/1.143.0/Widgets/Viewer/Viewer.css': 1958,
    'cesium/1.143.0/Widgets/VoxelInspector/VoxelInspector.css': 449,
    'cesium/1.143.0/Widgets/lighter.css': 6142,
    'cesium/1.143.0/Widgets/lighterShared.css': 1062,
    'cesium/1.143.0/Widgets/shared.css': 1952,
    'cesium/1.143.0/Widgets/widgets.css': 30710,
    'cesium/1.143.0/Workers/chunk-2AIOP76V.js': 20253,
    'cesium/1.143.0/Workers/chunk-37ETYCYM.js': 7545,
    'cesium/1.143.0/Workers/chunk-3E7FIXV7.js': 8890,
    'cesium/1.143.0/Workers/chunk-3N6OW3OY.js': 5614,
    'cesium/1.143.0/Workers/chunk-3VUCSHGU.js': 931,
    'cesium/1.143.0/Workers/chunk-4KY4VMEH.js': 3381,
    'cesium/1.143.0/Workers/chunk-4WQ4VT5S.js': 2442,
    'cesium/1.143.0/Workers/chunk-5AAMOBJK.js': 2907,
    'cesium/1.143.0/Workers/chunk-5DGDPBQU.js': 10016,
    'cesium/1.143.0/Workers/chunk-5VFNV3LW.js': 3077,
    'cesium/1.143.0/Workers/chunk-5Z36UAB7.js': 7241,
    'cesium/1.143.0/Workers/chunk-6AEO73KW.js': 5570,
    'cesium/1.143.0/Workers/chunk-6DLS2UKD.js': 1961,
    'cesium/1.143.0/Workers/chunk-6EINM7EY.js': 1073,
    'cesium/1.143.0/Workers/chunk-6W2XFGWI.js': 1748,
    'cesium/1.143.0/Workers/chunk-7GCQCPLT.js': 5098,
    'cesium/1.143.0/Workers/chunk-7TN3TOVQ.js': 2666,
    'cesium/1.143.0/Workers/chunk-A4I25VN7.js': 4215,
    'cesium/1.143.0/Workers/chunk-AJH5KBOO.js': 922,
    'cesium/1.143.0/Workers/chunk-AKELQO2L.js': 2262,
    'cesium/1.143.0/Workers/chunk-ALIHIWSS.js': 8096,
    'cesium/1.143.0/Workers/chunk-ATKJRN2G.js': 4312,
    'cesium/1.143.0/Workers/chunk-BDWA46XV.js': 3759,
    'cesium/1.143.0/Workers/chunk-BKIYVF74.js': 1644,
    'cesium/1.143.0/Workers/chunk-BPABSUDY.js': 27384,
    'cesium/1.143.0/Workers/chunk-D3TVNJ6W.js': 11036,
    'cesium/1.143.0/Workers/chunk-DM7KUSL2.js': 14561,
    'cesium/1.143.0/Workers/chunk-E7EKLP3B.js': 147462,
    'cesium/1.143.0/Workers/chunk-G2QPRBZU.js': 1375,
    'cesium/1.143.0/Workers/chunk-G3GDHHWO.js': 1240,
    'cesium/1.143.0/Workers/chunk-G72JFEXW.js': 6489,
    'cesium/1.143.0/Workers/chunk-IAE6APK2.js': 20558,
    'cesium/1.143.0/Workers/chunk-IEITL4VO.js': 12020,
    'cesium/1.143.0/Workers/chunk-LHBFSFJE.js': 7214,
    'cesium/1.143.0/Workers/chunk-LN2UT4R3.js': 1982,
    'cesium/1.143.0/Workers/chunk-LNJEJFV5.js': 124894,
    'cesium/1.143.0/Workers/chunk-ML6MR2RA.js': 915,
    'cesium/1.143.0/Workers/chunk-MOHWP7VV.js': 2198,
    'cesium/1.143.0/Workers/chunk-NZBME2JK.js': 2936,
    'cesium/1.143.0/Workers/chunk-P5EFSOUF.js': 10761,
    'cesium/1.143.0/Workers/chunk-P7N43FDO.js': 3320,
    'cesium/1.143.0/Workers/chunk-PEDE2P3Q.js': 1275,
    'cesium/1.143.0/Workers/chunk-PSIOCLX7.js': 5669,
    'cesium/1.143.0/Workers/chunk-PYMQNHFO.js': 11819,
    'cesium/1.143.0/Workers/chunk-QJTXFT4R.js': 16488,
    'cesium/1.143.0/Workers/chunk-QNPYEODC.js': 31687,
    'cesium/1.143.0/Workers/chunk-QS7V5G6Y.js': 5986,
    'cesium/1.143.0/Workers/chunk-RDX4QSUS.js': 14260,
    'cesium/1.143.0/Workers/chunk-RESOCDYZ.js': 58499,
    'cesium/1.143.0/Workers/chunk-SRA5MBUT.js': 15910,
    'cesium/1.143.0/Workers/chunk-TCOMWBL2.js': 12777,
    'cesium/1.143.0/Workers/chunk-UA7PAMQQ.js': 3177,
    'cesium/1.143.0/Workers/chunk-W5OEMTMB.js': 4601,
    'cesium/1.143.0/Workers/chunk-WGIRJIIK.js': 4891,
    'cesium/1.143.0/Workers/chunk-XRJOFXJF.js': 1351,
    'cesium/1.143.0/Workers/chunk-YP4SXJYZ.js': 4788,
    'cesium/1.143.0/Workers/chunk-Z7Q4J7AE.js': 20747,
    'cesium/1.143.0/Workers/combineGeometry.js': 1671,
    'cesium/1.143.0/Workers/createBoxGeometry.js': 1444,
    'cesium/1.143.0/Workers/createBoxOutlineGeometry.js': 3981,
    'cesium/1.143.0/Workers/createCircleGeometry.js': 3799,
    'cesium/1.143.0/Workers/createCircleOutlineGeometry.js': 2856,
    'cesium/1.143.0/Workers/createCoplanarPolygonGeometry.js': 6901,
    'cesium/1.143.0/Workers/createCoplanarPolygonOutlineGeometry.js': 3577,
    'cesium/1.143.0/Workers/createCorridorGeometry.js': 15298,
    'cesium/1.143.0/Workers/createCorridorOutlineGeometry.js': 7550,
    'cesium/1.143.0/Workers/createCylinderGeometry.js': 1500,
    'cesium/1.143.0/Workers/createCylinderOutlineGeometry.js': 3907,
    'cesium/1.143.0/Workers/createEllipseGeometry.js': 1751,
    'cesium/1.143.0/Workers/createEllipseOutlineGeometry.js': 1555,
    'cesium/1.143.0/Workers/createEllipsoidGeometry.js': 1472,
    'cesium/1.143.0/Workers/createEllipsoidOutlineGeometry.js': 1453,
    'cesium/1.143.0/Workers/createFrustumGeometry.js': 1444,
    'cesium/1.143.0/Workers/createFrustumOutlineGeometry.js': 3603,
    'cesium/1.143.0/Workers/createGeometry.js': 6366,
    'cesium/1.143.0/Workers/createGroundPolylineGeometry.js': 16535,
    'cesium/1.143.0/Workers/createPlaneGeometry.js': 3216,
    'cesium/1.143.0/Workers/createPlaneOutlineGeometry.js': 2123,
    'cesium/1.143.0/Workers/createPolygonGeometry.js': 18690,
    'cesium/1.143.0/Workers/createPolygonOutlineGeometry.js': 7757,
    'cesium/1.143.0/Workers/createPolylineGeometry.js': 6887,
    'cesium/1.143.0/Workers/createPolylineVolumeGeometry.js': 5643,
    'cesium/1.143.0/Workers/createPolylineVolumeOutlineGeometry.js': 4314,
    'cesium/1.143.0/Workers/createRectangleGeometry.js': 15013,
    'cesium/1.143.0/Workers/createRectangleOutlineGeometry.js': 6225,
    'cesium/1.143.0/Workers/createSimplePolylineGeometry.js': 5892,
    'cesium/1.143.0/Workers/createSphereGeometry.js': 2324,
    'cesium/1.143.0/Workers/createSphereOutlineGeometry.js': 2268,
    'cesium/1.143.0/Workers/createTaskProcessorWorker.js': 932,
    'cesium/1.143.0/Workers/createVectorTileClampedPolylines.js': 6040,
    'cesium/1.143.0/Workers/createVectorTileGeometries.js': 5765,
    'cesium/1.143.0/Workers/createVectorTilePoints.js': 1956,
    'cesium/1.143.0/Workers/createVectorTilePolygons.js': 5413,
    'cesium/1.143.0/Workers/createVectorTilePolylines.js': 3617,
    'cesium/1.143.0/Workers/createVerticesFromCesium3DTilesTerrain.js': 2081,
    'cesium/1.143.0/Workers/createVerticesFromGoogleEarthEnterpriseBuffer.js': 7863,
    'cesium/1.143.0/Workers/createVerticesFromHeightmap.js': 28260,
    'cesium/1.143.0/Workers/createVerticesFromQuantizedTerrainMesh.js': 5846,
    'cesium/1.143.0/Workers/createWallGeometry.js': 6500,
    'cesium/1.143.0/Workers/createWallOutlineGeometry.js': 4861,
    'cesium/1.143.0/Workers/decodeDraco.js': 5045,
    'cesium/1.143.0/Workers/decodeI3S.js': 17157,
    'cesium/1.143.0/Workers/gaussianSplatSorter.js': 1266,
    'cesium/1.143.0/Workers/gaussianSplatTextureGenerator.js': 1304,
    'cesium/1.143.0/Workers/incrementallyBuildTerrainPicker.js': 2098,
    'cesium/1.143.0/Workers/transferTypedArrayTest.js': 979,
    'cesium/1.143.0/Workers/upsampleQuantizedTerrainMesh.js': 9688,
    'cesium/1.143.0/Workers/upsampleVerticesFromCesium3DTilesTerrain.js': 2241,
})

_VENDOR_GENERATED = ('fonts/fonts.css',)

# 本地化之前 base.html + style.css 依赖的 6 个域名。留在这里当**具名黑名单**：
# 通用的「不许有 http(s):// 」已经能拦住它们，但错误信息里点名域名 + 对应的
# 本地替代路径，比一句「发现外链」有用得多。
_FORMER_CDN_HOSTS = {
    'cdn.jsdelivr.net': 'static/vendor/bootstrap/5.3.0/',
    'unpkg.com': 'static/vendor/leaflet/1.9.4/',
    'cdnjs.cloudflare.com': 'static/vendor/leaflet.draw/1.0.4/',
    'cdn.socket.io': 'static/vendor/socket.io/4.5.4/',
    'fonts.googleapis.com': 'static/vendor/fonts/fonts.css',
    'fonts.gstatic.com': 'static/vendor/fonts/*.woff2',
}

# 会真正触发一次网络请求的属性。`href` 覆盖 <link>（含 preconnect /
# dns-prefetch —— 离线时它们是白白发出去的 DNS 查询）与 <a>；
# `src` 覆盖 <script>/<img>/<iframe>；`action` 覆盖 <form>。
_FETCHING_ATTRS = ('href', 'src', 'action', 'poster', 'data-src')

_EXTERNAL_URL_RE = re.compile(r'^\s*(?:[a-z][a-z0-9+.-]*:)?//', re.I)


def _static_refs_in_templates():
    """templates/ 里全部 `url_for('static', filename='...')` 的 filename 值。

    直接读 Jinja 源码而不是渲染结果：渲染需要起 Flask app、要 DB、要 Config，
    而本文件全程只读文件。代价是识别不了动态拼接的 filename —— 这正是
    base.html 顶部那条注释要求「版本号写成字面量」的原因之一。
    """
    out = []
    rx = re.compile(r"""url_for\(\s*['"]static['"]\s*,\s*filename\s*=\s*['"]([^'"]+)['"]""")
    for fn, markup in _all_templates():
        for m in rx.finditer(markup):
            out.append((fn, m.group(1)))
    return out


def test_no_template_references_an_external_url():
    """templates/ 下任何标签都不许指向站外 URL —— 断网必须能用。

    这条是整个 vendor 本地化的**唯一一条形态护栏**，也是最容易被无声撤销的
    那一环：把 base.html 里任意一个 `<link>` / `<script>` 改回 CDN，页面在
    开发机上（联网）看不出任何区别，全部既有断言照旧全绿，而打包出去的 exe
    在断网环境里就少一个库。少 Bootstrap = 弹窗打不开 + 栅格塌掉；
    少 Leaflet.draw = 框选工具条三个按钮变空白；少 Socket.IO = 进度不动。

    连 `rel="preconnect"` 一起拦：它不加载资源，但离线时是一次白白发出去的
    DNS 查询 + TCP 连接，而它存在的唯一理由（给 CDN 预热）已经消失。

    ⚠️ 范围（诚实说明，别扩大解读）：
      - 只扫 templates/。底图瓦片写在 static/js/map.js 与 history.js 里，
        是**公网 OSM 瓦片**，用户明确决定不在本次范围内 —— 断网时地图区域
        仍是灰格子，那是预期行为。本条拦不到、也不该拦。
      - 只扫标签属性。JS 里 `fetch('https://...')`、CSS 里的 url() 不归它管；
        CSS 那半边由 test_no_css_under_static_reaches_out_to_the_network 覆盖。
    """
    templates = _all_templates()
    # 4 -> 6（GIS 工作台改版）：新增 _history_content.html / _config_content.html
    # 两个 include partial，内容与原页面相同，均无外链。
    # 6 -> 7（保存路径「浏览」）：新增 _path_browser_modal.html 目录选择弹窗
    # partial，无外链。
    # 7 -> 8（组件化）：新增 _macros.html 共用小组件宏（图标 + 面板头部），
    # 只有本地 SVG 标记，无外链。
    # 8 -> 9（命令面板）：新增 _command_palette.html 命令面板/速查表外壳
    # partial，纯静态 markup，无外链。
    # 9 -> 10（插件管理面板）：新增 _plugins_content.html 插件面板骨架 partial
    # （列表由 static/js/plugins.js 拉 /api/plugins 渲染），无外链。
    assert len(templates) == 10, (
        f'templates/ 下有 {len(templates)} 个 .html，本断言写下时是 10 个 —— '
        '新增页面不需要改本断言（它按目录遍历），但请确认新页面也没有外链，'
        '然后把这个数字更新掉'
    )
    scanned = 0
    offenders = []
    for fn, markup in templates:
        for tag, attrs in _start_tags(markup):
            for attr in _FETCHING_ATTRS:
                val = attrs.get(attr)
                if val is None:
                    continue
                scanned += 1
                if not _EXTERNAL_URL_RE.match(val):
                    continue
                host = re.sub(r'^\s*(?:[a-z][a-z0-9+.-]*:)?//', '', val, flags=re.I)
                host = host.split('/')[0].split('?')[0].lower()
                hint = _FORMER_CDN_HOSTS.get(host)
                offenders.append(
                    f'{fn}: <{tag} {attr}="{val}">'
                    + (f'  —— 本地副本在 {hint}' if hint else '')
                )
    # 扫描到的属性数量必须像样，否则「0 个外链」可能只是因为**什么都没扫到**
    # （例如 HTMLParser 换了行为、或 _FETCHING_ATTRS 被清空）。
    # 写下时实测 18 个：base.html 14（5 张表 + 5 个脚本 + 4 个导航 <a>）、
    # index/config/history 各 1-2 个页面级 <script>。
    assert scanned >= 17, (
        f'四个模板里只扫出 {scanned} 个可能发起请求的属性（写下时是 17 个），太少了 —— '
        '本断言的 0 offender 很可能是扫描器坏了而不是真的没有外链'
    )
    assert not offenders, (
        '模板里出现了指向站外的引用，断网时这些资源全部拿不到：\n'
        + '\n'.join('  ' + o for o in offenders)
        + '\n本项目是 PyInstaller 打包的离线桌面工具，第三方资源一律走 '
          "{{ url_for('static', filename='vendor/...') }}。"
    )


def test_every_static_reference_in_templates_exists_on_disk():
    """模板里引的每一个 static 文件都必须真的在磁盘上。

    这条把「markup 说要加载什么」和「仓库里真有什么」**绑在一起**。
    单独看，上一条只保证「没指向站外」，这条只保证「文件在」；合起来才等于
    「离线时这些资源真的加载得到」。

    它专治两类静默失败：
      1. 改了 vendor 目录名 / 版本号，忘了同步模板（或反过来）——
         Flask 的 url_for('static', ...) **不检查文件是否存在**，照样吐出 URL，
         浏览器拿到 404，页面静默降级。
      2. 新加一个 `<script src=...vendor/x.js>` 却忘了把文件提交进来。
    """
    refs = _static_refs_in_templates()
    # 14 -> 17（GIS 工作台改版）：首页 extra_js 新增 history.js / config.js /
    # panels.js 三处引用（覆盖面板），均为本地文件。
    # 17 -> 18（Cesium 换地图）：-2（leaflet.draw css/js）+1（widgets.css）
    # +1（Cesium.js）+1（CESIUM_BASE_URL 的 url_for）。
    # 18 -> 16（Leaflet 全量移除）：历史小地图也迁到 Cesium 后，leaflet css/js 删除。
    # 16 -> 17（明亮模式 + 跟随系统）：base.html 在 ui.js 之后全局引入
    # js/theme.js（window.TerraTheme，运行期主题切换/跟随实现）。
    # 17 -> 18（保存路径「浏览」）：base.html 引入 js/path_browser.js
    # （目录选择弹窗交互），字符串字面量。
    # 18 -> 19（地形光照开关）：base.html 在 theme.js 之后引入
    # js/terrain_lighting.js（window.TerrainLighting，scene.globe.enableLighting
    # 的开关，偏好只存 localStorage）。必须排在 index.html 的 extra_js（map.js）
    # 之前，顺序由 tests/test_terrain_lighting_frontend.py 钉住。
    # 19 -> 20（i18n 改造）：base.html 新增 static/js/i18n.js（全局 t() 查表，
    # 必须排在所有业务脚本之前加载）。
    # 20 -> 21（底图解压进度）：base.html 新增 static/js/socket.js
    # （window.TerraSocket 全局单例；socket 实例原本由 tasks.js 独占创建，
    # 只有首页有，而解压进度要求全站可见）。
    # 21 -> 22（底图解压进度）：base.html 新增 static/js/base_terrain_status.js
    # （底部状态栏最右的解压进度，监听 base_unpack_progress）。
    # 25（原 22）：base.html 新增 vue.global.prod.js / task_store.js /
    # task_list.js 三个 <script>（任务时间流的渲染层）。
    # 25 -> 26（状态映射收口）：base.html 新增 static/js/task_status.js
    # （getStatusColor / getStatusText / getStatusStroke 的唯一实现；改前
    # tasks.js 与 history.js 各有一份，首页同时加载时后者静默遮蔽前者）。
    # 26 -> 27（高程切片 TIF 信息卡）：index.html 新增 static/js/geotiff_meta.js
    # （浏览器侧 GeoTIFF 头部解析；只在首页的处理弹窗里用到，故挂在
    # index.html 的 extra_js 而不是 base.html）。
    # 27 -> 28（应用图标）：base.html 新增 <link rel="icon"> 指向
    # static/img/favicon.ico（生成脚本 scripts/make_icon.py；打包 exe 的
    # --windows-icon-from-ico 用的是同一个文件）。
    # 28 -> 29（命令面板）：base.html 新增 static/js/command_palette.js。
    # 29 -> 30（全窗口拖拽 .tif）：index.html 的 extra_js 新增
    # static/js/drop_process.js（仅首页，defer 跟在 panels.js 之后）。
    # 30 -> 31（§6.1 统一任务中心）：base.html 的 vendor_task_list_js block 新增
    # static/js/task_center.js —— tasks.js 里与首页地图无关的那一半（socket 接线、
    # 四路活动列表、进度合并、终态处理、耗时/速度、启动/暂停/恢复、缺口决策）
    # 全部搬进去。它必须在**公共**脚本区加载：那些能力过去只在首页有，因为实现
    # 整块躺在 index.html 才加载的 tasks.js 里，于是 /history 上没有按钮、没有
    # 实时更新、没有耗时也没有速度。/config 仍不付这份代价（它把整个 block 覆盖成空）。
    # 31 -> 32（插件管理面板）：index.html 的 extra_js 新增 static/js/plugins.js
    # （插件列表/启停/声明式新建任务表单；只有首页有插件面板，故挂在 index.html
    # 的 extra_js 而不是 base.html）。
    assert len(refs) == 32, (
        f"模板里解析出 {len(refs)} 处 url_for('static', ...)，本断言写下时是 32 处。"
        '数量变了不一定是错（加页面就会变），但请确认解析逻辑还认得出全部写法 —— '
        '尤其是：filename 必须是**字符串字面量**，写成变量拼接这里就看不见了'
    )
    missing = [
        f'{fn}: static/{name}'
        for fn, name in refs
        # CESIUM_BASE_URL 指向的是目录（Cesium 运行时按它拼 worker/asset 路径），
        # 所以目录也算有效引用。
        if not os.path.isfile(os.path.join(_STATIC_DIR, *name.split('/')))
        and not os.path.isdir(os.path.join(_STATIC_DIR, *name.split('/')))
    ]
    assert not missing, (
        '模板引用了不存在的 static 文件，浏览器会拿到 404 而页面不会报错：\n'
        + '\n'.join('  ' + m for m in missing)
    )


def _vendor_files_on_disk():
    """static/vendor/ 下全部文件的相对路径（正斜杠）。"""
    out = []
    for root, _dirs, files in os.walk(_VENDOR_DIR):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), _VENDOR_DIR)
            out.append(rel.replace(os.sep, '/'))
    return sorted(out)


def test_vendor_tree_matches_the_manifest():
    """static/vendor/ 的文件集合与字节数必须与清单逐一对上。

    为什么要有一份显式清单：二级资源（3 张雪碧图 + 4 个 woff2）**grep 模板
    grep 不出来**——它们是被 leaflet.draw.css / fonts.css 里的 url() 引的。
    漏掉雪碧图，首页框选工具条的三个按钮就是三个空白方块；漏掉 woff2，
    字体静默退回系统默认。这两种失败都不会在控制台留下任何红色。

    字节数钉死的理由见 VENDOR_MANIFEST 上方的注释。
    """
    on_disk = set(_vendor_files_on_disk())
    expected = set(VENDOR_MANIFEST) | set(_VENDOR_GENERATED)
    missing = sorted(expected - on_disk)
    extra = sorted(on_disk - expected)
    assert not missing, (
        'vendor 清单里的文件在磁盘上找不到 —— 离线时这些资源直接 404：\n'
        + '\n'.join('  static/vendor/' + m for m in missing)
    )
    assert not extra, (
        'static/vendor/ 下有清单外的文件。要么是忘了登记（请补进 '
        'VENDOR_MANIFEST），要么是残留物（请删）：\n'
        + '\n'.join('  static/vendor/' + e for e in extra)
    )
    wrong = []
    for rel, size in sorted(VENDOR_MANIFEST.items()):
        actual = os.path.getsize(os.path.join(_VENDOR_DIR, *rel.split('/')))
        if actual != size:
            wrong.append(f'static/vendor/{rel}: {actual} B，清单是 {size} B')
    assert not wrong, (
        'vendor 文件的字节数与清单对不上 —— 可能是下成了别的构建'
        '（未压缩版 / 别的版本）、下到一半截断，或者有人手改了第三方源码：\n'
        + '\n'.join('  ' + w for w in wrong)
    )


# 每个 vendor 库在自己文件里声明的版本，用来和**路径里的版本号**对账。
# 键 = 相对 static/vendor/ 的文件路径；值 = (提取版本的正则, 该文件必须含有的内容标记们)。
# 标记一律挑「站内真的依赖它」的东西，不挑随便一个字符串。
_VENDOR_VERSION_PROBES = {
    'bootstrap/5.3.0/bootstrap.min.css': (
        re.compile(r'Bootstrap\s+v(\d+\.\d+\.\d+)'),
        # `[data-bs-theme=dark]` 就是 MIN_BOOTSTRAP_VERSION 的**事实依据**：
        # <html data-bs-theme="dark"> 得有人消费才不是装饰属性。本地化之前这
        # 只能靠一个手抄常量间接主张（那条断言的注释自己承认「拿一份手抄的常量
        # 冒充事实」），现在源码在仓库里，直接读文件。
        # `.modal-backdrop` / `.row` 各代表一类站内依赖：弹窗遮罩、栅格
        # （style.css 里那份残缺的 .row/.col-* 兜底已随本地化删除）。
        ('[data-bs-theme=dark]', '.modal-backdrop', '.row'),
    ),
    'bootstrap/5.3.0/bootstrap.bundle.min.js': (
        re.compile(r'Bootstrap\s+v(\d+\.\d+\.\d+)'),
        # Modal：history 页任务详情弹窗（history.js 里 new bootstrap.Modal）。
        # Collapse：导航栏折叠按钮。
        # createPopper：**bundle 版才有**，用它区分 bootstrap.bundle.min.js
        # 和体积相近的 bootstrap.min.js —— 后者不带 Popper，Dropdown/Tooltip
        # 会在运行时抛错，而文件名/版本号看起来一切正常。
        ('Modal', 'Collapse', 'createPopper'),
    ),
    'cesium/1.143.0/Cesium.js': (
        re.compile(r'CESIUM_VERSION="(\d+\.\d+\.\d+)"'),
        # UrlTemplateImageryProvider / WebMercatorTilingScheme：首页 OSM 底图与
        # 等高线瓦片叠加都走这两个类（map.js）。
        ('UrlTemplateImageryProvider', 'WebMercatorTilingScheme'),
    ),
    'socket.io/4.5.4/socket.io.min.js': (
        re.compile(r'Socket\.IO\s+v(\d+\.\d+\.\d+)'),
        # engine.io / EIO：传输层。服务端是 python-engineio 4.7.1（Engine.IO v4），
        # 客户端换大版本会直接握不上手，而页面只表现为「进度条永远不动」。
        ('engine.io', 'EIO'),
    ),
}


def test_vendor_builds_match_the_version_in_their_path():
    """路径里的版本号必须和文件里自报的版本一致。

    路径版本号不是装饰：`test_bootstrap_build_is_new_enough_to_have_dark_theme`
    和 `test_leaflet_draw_build_matches_the_locale_key_snapshot` 都从 base.html
    的 URL 里正则抠版本号下结论。本地化之后那个版本号只是一个**目录名**——
    谁都可以把一份 4.x 的 socket.io 放进 `socket.io/4.5.4/`，两条断言照旧全绿，
    而运行时握手直接失败（服务端 python-socketio 5.9.0 = 协议 v5 / Engine.IO v4，
    只吃 4.x 客户端）。

    这条把「声明」对上「实物」：读文件里的版本 banner，和路径比。
    同时各查一个**内容标记**，挡住「版本对但内容被替换/裁剪」——
    尤其是 Bootstrap 的 `[data-bs-theme=dark]`：整站深色主题就靠它，
    在本地化之前这一条只能靠手抄的 MIN_BOOTSTRAP_VERSION 常量间接主张。
    """
    assert len(_VENDOR_VERSION_PROBES) == 4, '探针表被改动过，请同步本断言的说明'
    problems = []
    for rel, (rx, markers) in sorted(_VENDOR_VERSION_PROBES.items()):
        path = os.path.join(_VENDOR_DIR, *rel.split('/'))
        assert os.path.isfile(path), f'static/vendor/{rel} 不存在 —— 本测试已失效'
        with open(path, encoding='utf-8', errors='replace') as f:
            text = f.read()
        path_version = rel.split('/')[1]
        m = rx.search(text)
        if not m:
            problems.append(
                f'static/vendor/{rel}: 文件里读不出版本 banner（正则 {rx.pattern!r}）。'
                '换了上游构建就要同步 _VENDOR_VERSION_PROBES，别直接删探针'
            )
        elif m.group(1) != path_version:
            problems.append(
                f'static/vendor/{rel}: 文件自报 {m.group(1)}，路径写的是 {path_version}'
            )
        for marker in markers:
            if marker not in text:
                problems.append(
                    f'static/vendor/{rel}: 文件里找不到内容标记 {marker!r} —— '
                    '版本号对得上但内容不是预期的那份构建'
                )
    assert not problems, (
        'vendor 库的实物和路径声明对不上：\n' + '\n'.join('  ' + p for p in problems)
    )


def _css_files_under_static():
    """static/ 下全部 .css 的 (相对 static 的路径, 绝对路径, 内容)。"""
    out = []
    for root, _dirs, files in os.walk(_STATIC_DIR):
        for f in sorted(files):
            if not f.lower().endswith('.css'):
                continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, _STATIC_DIR).replace(os.sep, '/')
            with open(path, encoding='utf-8', errors='replace') as fh:
                out.append((rel, path, fh.read()))
    return sorted(out)


def _css_asset_targets(css):
    """一份 CSS 里全部会发起请求的目标：url(...) 与 @import 的参数。

    先剥注释——本仓库的 CSS 注释在**逐字讨论**被删掉的那些 URL（style.css 顶部
    那段就写着 fonts.googleapis.com），不剥的话通用扫描器会把注释当成违规。
    """
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    targets = [m.group(1).strip().strip('\'"') for m in re.finditer(r'url\(([^)]*)\)', css)]
    targets += [
        m.group(1).strip()
        for m in re.finditer(r'@import\s+(?:url\()?\s*[\'"]([^\'"]+)[\'"]', css, re.I)
    ]
    return targets


def test_no_css_under_static_reaches_out_to_the_network():
    """static/ 下任何一份 CSS 都不许引用站外资源。

    上一批断言只看 templates/ 的标签属性，对 CSS **完全失明**。而本次本地化
    里第 9 个 CDN 依赖恰恰就藏在 CSS 里：style.css 第 2 行原本是
        @import url('https://fonts.googleapis.com/css2?family=Inter...');
    与 base.html 那条 `<link>` 指向**同一个 URL**。只改模板会把它留下，
    而 @import 在样式表顶部是**阻塞渲染**的 —— 离线时首屏要被它拖到超时。

    连 vendor/ 自己的 CSS 一起扫：Google Fonts 的 CSS 天生就是一堆
    `src: url(https://fonts.gstatic.com/...)`，本地化时必须逐条改写成相对路径，
    漏改一条就是一个字重悄悄走网络。
    """
    files = _css_files_under_static()
    assert len(files) >= 4, (
        f'static/ 下只扫到 {len(files)} 份 CSS（style.css + vendor 里 3 份），'
        '太少了 —— 扫描器很可能坏了'
    )
    offenders = []
    for rel, _path, css in files:
        for target in _css_asset_targets(css):
            if target.startswith('data:') or target.startswith('#'):
                continue
            if _EXTERNAL_URL_RE.match(target):
                host = re.sub(r'^\s*(?:[a-z][a-z0-9+.-]*:)?//', '', target, flags=re.I)
                host = host.split('/')[0].split('?')[0].lower()
                hint = _FORMER_CDN_HOSTS.get(host)
                offenders.append(
                    f'static/{rel}: {target}'
                    + (f'  —— 本地副本在 {hint}' if hint else '')
                )
    assert not offenders, (
        'CSS 里出现了指向站外的引用，断网时拿不到（@import 还会阻塞首屏渲染）：\n'
        + '\n'.join('  ' + o for o in offenders)
    )


def test_every_relative_css_url_resolves_next_to_its_own_stylesheet():
    """CSS 里的相对 url() 必须能相对**该 CSS 自身的位置**解析到真实文件。

    这条钉的是一个只有一种改法会踩、但踩了必坏的坑：浏览器解析 CSS 里的相对
    路径**以 CSS 文件自己的 URL 为基准**，不是以页面 URL。
    `leaflet.draw.css` 里写的是 `url('images/spritesheet.png')`，所以
    `images/` 必须与 `leaflet.draw.css` **同级**。把三张雪碧图放平到
    `vendor/leaflet.draw/1.0.4/spritesheet.png`，或者把几份 vendor CSS 合并成
    一个大文件，图标就全丢 —— 而「文件都在仓库里」这类断言依然全绿。

    fonts.css 的 4 个 woff2 同理。

    _VENDOR_UNSHIPPED 里那 3 个是**故意没下载**的，理由见那张表；
    下一条断言负责保证那个理由还成立。
    """
    files = _css_files_under_static()
    checked = 0
    broken = []
    for rel, path, css in files:
        base = os.path.dirname(path)
        for target in _css_asset_targets(css):
            if _EXTERNAL_URL_RE.match(target) or target.startswith(('data:', '#', '/')):
                continue
            checked += 1
            clean = target.split('?')[0].split('#')[0]
            if f'{rel}::{clean}' in _VENDOR_UNSHIPPED:
                continue
            if not os.path.isfile(os.path.join(base, *clean.split('/'))):
                broken.append(f'static/{rel} -> {target}（相对该 CSS 自身解析不到）')
    # 站内实际有 3(leaflet.draw 雪碧图) + 4(fonts woff2) + 3(leaflet.css 里
    # 故意没下载的 layers/marker 图) = 10 个相对目标。少于这个数 = 扫描器坏了。
    assert checked >= 10, (
        f'只扫到 {checked} 个相对 url() 目标，本断言写下时是 10 个 —— 扫描器可能坏了'
    )
    assert not broken, (
        'CSS 里的相对 url() 解析不到文件。浏览器按**该 CSS 自身的 URL** 解析相对'
        '路径，所以图片目录必须与引用它的 CSS 同级：\n'
        + '\n'.join('  ' + b for b in broken)
        + '\n如果是故意不发布这个资源（例如站内根本不会渲染出对应元素），'
          '请登记进 _VENDOR_UNSHIPPED 并写清理由。'
    )


# 上游 CSS 里引了、但本仓库**故意不发布**的资源。键是 `<相对 static 的 CSS 路径>::<url 目标>`。
#
# 这 3 个都是 Leaflet 的：`.leaflet-control-layers-toggle` 的图层切换图标、
# `.leaflet-default-icon-path` 的默认标记图标。站内从不创建图层控件、也从不放
# marker（map.js 明确 `marker: false, circlemarker: false`，history.js 只用
# L.rectangle —— 纯 SVG 矢量，不吃图片），对应元素在 DOM 里根本不存在，
# 于是浏览器**不会**为这几条规则发起任何请求。不下载不产生任何视觉差异，
# 也不会在控制台留下 404。
_VENDOR_UNSHIPPED = {
    'vendor/leaflet/1.9.4/leaflet.css::images/layers.png',
    'vendor/leaflet/1.9.4/leaflet.css::images/layers-2x.png',
    'vendor/leaflet/1.9.4/leaflet.css::images/marker-icon.png',
}

# 一旦站内开始用这些 API，上面「元素不存在所以不会发请求」的理由就失效了。
_LEAFLET_IMAGE_CONSUMING_APIS = (
    'L.marker',
    'L.Marker',
    'L.control.layers',
    'L.Control.Layers',
    'Icon.Default',
)


def test_local_font_css_is_self_contained():
    """vendor/fonts/fonts.css 必须是一份完整、全本地、结构正确的字体表。

    fonts.css 是本仓库**唯一一份自己生成的 vendor 文件**（上游 40 个
    @font-face 裁成 latin / latin-ext 两个子集、URL 全部改写成同目录相对路径），
    所以它不像别的 vendor 文件那样能靠字节数对账。这条按结构校验。

    为什么值得单列一条：字体是**二级资源**里最容易漏的一类 —— grep 模板扫不出
    woff2，漏掉一个子集的表现只是「某几个字重悄悄退回系统字体」，没有报错、
    没有布局塌陷，一眼看不出来。另外抓上游 CSS 时若忘了带浏览器 User-Agent，
    fonts.googleapis.com 会返回一份指向 **.ttf** 的表（体积大好几倍），
    那种情况下 format('woff2') 会一个都不剩 —— 下面第三条断言就是钉这个的。
    """
    path = os.path.join(_VENDOR_DIR, 'fonts', 'fonts.css')
    with open(path, encoding='utf-8') as f:
        raw = f.read()
    css = re.sub(r'/\*.*?\*/', '', raw, flags=re.S)

    blocks = re.findall(r'@font-face\s*\{[^}]*\}', css, re.S)
    # Inter 4 个字重 + JetBrains Mono 2 个字重，各 2 个子集 = (4+2)*2 = 12。
    assert len(blocks) == 12, (
        f'fonts.css 里有 {len(blocks)} 个 @font-face 块，期望 12 个'
        '（Inter 400/500/600/700 + JetBrains Mono 400/600，各 latin 与 latin-ext 两份）。'
        '少了就是某个字重悄悄退回系统字体 —— 页面不会报任何错。'
    )
    families = {m.group(1) for m in re.finditer(r"font-family:\s*'([^']+)'", css)}
    assert families == {'Inter', 'JetBrains Mono'}, (
        f'fonts.css 声明的字族是 {sorted(families)}，期望 Inter + JetBrains Mono。'
        'style.css 的 --font-* 令牌指名要这两个。'
    )
    assert 'format(' in css and css.count("format('woff2')") == 12, (
        f"fonts.css 里 format('woff2') 出现 {css.count(chr(39) + 'woff2' + chr(39))} 次，"
        '期望 12 次。**抓上游 CSS 时必须带浏览器 User-Agent** —— '
        'fonts.googleapis.com/css2 会嗅探 UA，裸 curl 拿到的是 .ttf。'
    )
    weights = sorted({int(m.group(1)) for m in re.finditer(r'font-weight:\s*(\d+)', css)})
    assert weights == [400, 500, 600, 700], (
        f'fonts.css 覆盖的字重是 {weights}，期望 [400, 500, 600, 700]。'
        'style.css 里 .btn/.card-header/h3 等处用到 500/600/700，缺哪个就由浏览器'
        '合成假粗体（形态明显变糙）。'
    )
    # unicode-range 是子集机制的开关：删掉它，第一个 @font-face 就会吃掉全部
    # 码位，latin-ext 的那份永远不会被下载。
    assert len(re.findall(r'unicode-range:', css)) == 12, (
        'fonts.css 里 unicode-range 声明数与 @font-face 块数对不上 —— '
        '子集机制会失效（浏览器只下第一份，另一个子集永远拿不到）'
    )


def test_vendor_files_are_not_swallowed_by_gitignore():
    """vendor 文件必须真的进得了 git —— 这是本次任务最容易无声炸掉的一环。

    `.gitignore` 里的 `build/`、`dist/`、`lib/` **没有路径锚定**，按 gitignore
    语义它们匹配**任意层级**的同名目录。实测 `git check-ignore -v`：
        static/vendor/leaflet/dist/leaflet.js  -> IGNORED (.gitignore:13)
        static/vendor/x/lib/y.js               -> IGNORED (.gitignore:17)
        static/vendor/x/build/y.js             -> IGNORED (.gitignore:11)
    也就是说，**照搬 npm 包的目录结构**（`leaflet/dist/`、`leaflet-draw/lib/`）
    的话，文件根本进不了仓库。

    这个失败模式全程无声，是它值得单独一条断言的理由：
      本机开发一切正常（文件就在磁盘上，dev server 和本地 PyInstaller 都读得到）
      -> CI checkout 出来少这些文件
      -> PyInstaller **不报错**（`datas` 是整目录 os.walk，少几个文件它不知道）
      -> 构建成功
      -> 用户拿到的 exe 前端全裸。

    所以现有的目录结构是**扁平 + 版本号**（`vendor/leaflet/1.9.4/leaflet.js`），
    不是 npm 那套。谁要改回 npm 结构，这条会红。

    实现说明：以 `git ls-files` 为准（那才是「新 checkout 里有没有」的事实）。
    拿不到 git 时退回一份 .gitignore 目录模式扫描 —— 不是跳过：静默跳过的护栏
    等于没有护栏。
    """
    repo_root = os.path.dirname(_STATIC_DIR)
    on_disk = _vendor_files_on_disk()
    assert len(on_disk) == 172, (
        f'static/vendor/ 下有 {len(on_disk)} 个文件，本断言写下时是 172 个 —— '
        '本条按目录遍历，数量本身会变，但请顺手确认 VENDOR_MANIFEST 也同步了'
    )
    try:
        out = subprocess.run(
            ['git', 'ls-files', '-z', '--', 'static/vendor'],
            cwd=repo_root, capture_output=True, timeout=30,
        )
        tracked = None if out.returncode != 0 else {
            p.decode('utf-8')[len('static/vendor/'):]
            for p in out.stdout.split(b'\0') if p
        }
    except (OSError, subprocess.SubprocessError):
        tracked = None

    if tracked is not None:
        untracked = sorted(set(on_disk) - tracked)
        assert not untracked, (
            'vendor 文件在磁盘上但 git 不认（多半是被 .gitignore 吃掉了）。'
            '本机看不出任何问题，CI checkout 出来就少文件，'
            '打包出的 exe 前端全裸：\n'
            + '\n'.join(f'  static/vendor/{u}    # git check-ignore -v 这个路径看看' for u in untracked)
            + '\n对策：别用 npm 的目录结构（dist/ lib/ build/ 三个名字都被 '
              '.gitignore 无锚定匹配），改用 `<库名>/<版本号>/<文件>` 扁平结构。'
        )
        return

    # 退路：git 不可用（源码 tarball / 无 git 的环境）时，直接按 .gitignore 里
    # 的**无锚定目录模式**扫。覆盖不如 git 全（不处理 ! 反向规则、通配符），
    # 但足以拦住本条真正要拦的那一类。
    patterns = set()
    with open(os.path.join(repo_root, '.gitignore'), encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('!'):
                continue
            if line.endswith('/') and '/' not in line[:-1] and '*' not in line:
                patterns.add(line[:-1])
    assert patterns, '.gitignore 里解析不出任何无锚定目录模式 —— 退路逻辑已失效'
    hits = [
        f'static/vendor/{rel}    # 目录名 {seg!r} 命中 .gitignore 的无锚定模式'
        for rel in on_disk for seg in rel.split('/')[:-1] if seg in patterns
    ]
    assert not hits, (
        'vendor 路径里出现了会被 .gitignore 无锚定匹配的目录名：\n' + '\n'.join('  ' + h for h in hits)
    )


def test_rule_scanner_is_not_blinded_by_an_at_statement():
    """`@import` / `@charset` 这类**不带花括号**的 at-语句不许吞掉后面那条规则。

    这条是本文件的**自检**，来历是一个真实存在过、且骗过了全部 283 条断言的
    静默漏检：style.css 第 2 行原本是
        @import url('https://fonts.googleapis.com/css2?...');
    `_rules_ctx` 按花括号深度扫描，而 at-语句以 `;` 收尾、没有花括号，于是它
    整段被并进**下一条规则**的选择器 token（折完空白后长成
    `@import url(...); :root`），再被 `sel.startswith('@')` 那句丢掉。
    结果：紧随其后的 `:root { ... }` —— 全站设计令牌（--color-* / --font-size-*
    / --space-*）的唯一定义处 —— 对本文件**每一条**基于 _rules/_rules_ctx 的
    断言完全隐身。

    没人发现是因为它的表现是「少扫一条规则」，而少扫永远不会让断言变红。
    直到 vendor 本地化删掉那句 @import，`:root` 头一次进入扫描范围，
    按钮层叠模型立刻报「读不懂 :root」，10 条断言同时红 —— 那是这个漏检
    唯一一次现形。

    所以这条钉两件事：(1) 解析器对 at-语句免疫；(2) `:root` 确实在扫描范围内。
    """
    probe = (
        "@charset \"UTF-8\";\n"
        "@import url('https://example.com/x.css');\n"
        ":root { --k: 1px; }\n"
        ".a { color: red; }\n"
        "@media (min-width: 768px) { .b { color: blue; } }\n"
    )
    got = [(sel, ctx) for sel, _body, ctx in _rules_ctx(probe)]
    assert got == [(':root', []), ('.a', []), ('.b', ['@media (min-width: 768px)'])], (
        f'带 at-语句的探针解析成 {got}，期望 :root / .a / .b 三条都在。'
        'at-语句一旦被并进下一条规则的选择器，那条规则会被整个丢掉 —— '
        '静默少扫，没有任何断言会红。'
    )
    live = [sel for sel, _body, _ctx in _rules_ctx(_css())]
    assert ':root' in live, (
        'style.css 的 `:root` 不在 _rules_ctx 的扫描结果里 —— '
        '全站设计令牌对本文件所有基于 _rules 的断言隐身了。'
        '最可能的原因：有人在它前面加了一条不带花括号的 at-语句'
        '（@import / @charset / @namespace），而 _rules_ctx 的剥离正则没覆盖到。'
    )


# ==========================================================================
# C1 收尾：根治 `div:not(...)` 兜底重置
#
# 被删掉的那条规则长这样（style.css，删除前）：
#     div:not(.card):not(.modal-content):not(.alert):not(.badge):not(.btn)
#        :not(.progress):not(.progress-bar):not(.app-confirm)
#        :not(.app-confirm-overlay):not(.app-toast):not(.task-error)
#     { background: transparent; }
# 特异度 (0,11,1)。11 个 `:not(.class)` 各贡献一个类，于是它压得过**任何**
# `.xxx { background: ... }`(0,1,0)。那 11 项白名单是历次撞上它之后逐个补的豁免。
#
# 它压掉的东西（CDP 实测，Chrome 148，三页 6 场景 1699 个元素逐元素对拍）：
#   .index-right / .config-section x6 / .stat-card x8 / .modal-header x2 /
#   .task-card x8 / div.modal-backdrop —— 共 28 个元素、13 个类组合的
#   computed background-color 从 rgba(0,0,0,0) 变回实色。
#   其中 `div.modal-backdrop` 是本次的硬验收点：Bootstrap 的
#   `.modal-backdrop{--bs-backdrop-bg:#000}` 只有 (0,1,0)，被压成透明之后
#   **这个应用从上线到现在，打开任何弹窗背后都没有变暗过**。
#
# 本节四条断言分两类：
#   1) 结果类（主力）—— 模拟层叠算出「元素最终拿到哪条背景声明」，
#      钉的是**用户看到的结果**，不是源码里有没有某个字符串。
#      这四类断言共同的设计要求：任何一次「让背景消失」的改动都必须变红，
#      无论它是靠特异度、靠 !important、还是靠删掉声明本身做到的。
#   2) 形态类（补位）—— 正面禁止兜底重置这种「不认识元素却夺走它背景」
#      的规则形态回潮，即使当天恰好没有受害者在 markup 里。
# ==========================================================================


def _stylesheet_load_order():
    """base.html 里 `<link rel=stylesheet>` 的真实顺序 -> 仓库内相对路径列表。

    **不硬编码**：顺序本身是承重的（style.css 靠最后取胜是好几条断言的前提），
    抄一份到测试里就等于给它开了个静默漂移的口子。这里直接读 base.html，
    顺序一改，下面所有层叠计算跟着改，该红的会红。
    """
    links = re.findall(r"<link[^>]*rel=[\"']stylesheet[\"'][^>]*>", _template('base.html'))
    out = []
    for tag in links:
        m = re.search(r"filename=['\"]([^'\"]+)['\"]", tag)
        assert m, f'base.html 里有一条 <link rel=stylesheet> 不是 url_for 形态：{tag[:80]}'
        out.append(os.path.join(_STATIC_DIR, *m.group(1).split('/')))
    return out


# 只有 `.css` 会参与层叠计算；`fonts.css` 里全是 @font-face，没有选择器规则，
# 留着也无害，一并读进来省得维护第二张名单。
def _all_sheets():
    """[(短名, [(选择器, 规则体, at 上下文, 表内序号)])]，按 <link> 顺序。"""
    out = []
    for path in _stylesheet_load_order():
        with open(path, encoding='utf-8') as f:
            css = f.read()
        rules = [
            (sel, body, at_ctx, i)
            for i, (sel, body, at_ctx) in enumerate(_rules_ctx(css))
        ]
        out.append((os.path.basename(path), rules))
    return out


def _bg_decl(body):
    """规则体里最后一次 background / background-color 声明 -> (值, 是否!important)。

    两个属性都要看：`background` 简写会把 `background-color` 一起覆盖，
    只认其中一个等于留一半盲区（Task 9 在 select 箭头和 Leaflet 雪碧图上
    分别踩过这个坑的另一面）。同名属性取最后一次 —— 与浏览器一致。
    """
    last = None
    for m in re.finditer(r'(?<![-\w])(background|background-color)\s*:\s*([^;}]+)',
                         body, re.I):
        last = m.group(2).strip()
    if last is None:
        return None
    return (_IMPORTANT_RE.sub('', last).strip(), bool(_IMPORTANT_RE.search(last)))


def _bg_is_transparent(css, value):
    """这个背景值渲染出来是「什么都没有」吗？

    `transparent` / `none` / `rgba(...,0)` 三种写法都算。先解一层本站的 var()
    —— 本项目的背景值大量写成 var(--color-bg-secondary)。

    **解不出来的 var() 一律判为「非透明」**，这是刻意选的保守方向：那多半是
    Bootstrap 的 `var(--bs-card-bg)` 之类，含义是「有人在这里明确要一个底色」。
    判成透明的话，Bootstrap 组件会被当作「没人想给它底色」，主断言就看不见
    `.modal-backdrop` 这种**只有 vendor 声明过背景**的受害者 —— 而那正是本次
    最硬的那个缺陷。不用 _resolve_color：它对本文件之外的变量会直接断言失败。
    """
    v = _IMPORTANT_RE.sub('', value).strip().lower()
    m = re.fullmatch(r'var\(\s*(--[-\w]+)\s*\)', v)
    if m:
        hit = re.search(re.escape(m.group(1)) + r'\s*:\s*([^;]+);', css)
        if hit is None:
            return False                   # 外部变量：保守当成「有人要底色」
        v = hit.group(1).strip().lower()
    if v in ('transparent', 'none', 'initial', 'unset', 'revert', '0 0'):
        return True
    m = re.match(r'^rgba?\(([^)]*)\)', v)
    if m:
        parts = [p.strip() for p in re.split(r'[,/]', m.group(1)) if p.strip()]
        if len(parts) == 4:
            try:
                return float(parts[3]) == 0.0
            except ValueError:
                return False
    return False


# 单冒号写法的伪元素（CSS2 遗留，leaflet.css 通篇在用）。它们样式化的是另一个
# 盒子，不参与宿主元素自身的背景层叠 —— 与 `::` 同等对待，判「不命中」。
_LEGACY_PSEUDO_ELEMENTS = frozenset({'before', 'after', 'first-line', 'first-letter'})

# 这些伪类描述的是「用户正在操作」的瞬时状态。本模型算的是**静止态**
# （页面刚打开、鼠标不在上面）的渲染结果，它们一律判不成立。
# 代价说清楚：悬停 / 聚焦 / 禁用态的背景不在本模型覆盖范围内。
_INTERACTIVE_PSEUDOS = frozenset({
    'hover', 'focus', 'focus-visible', 'focus-within', 'active',
    'disabled', 'checked', 'target', 'visited', 'link', 'placeholder-shown',
    'indeterminate', 'valid', 'invalid',
})

# 静止态一定成立、且本模型能判定的伪类。
_RESTING_PSEUDOS = frozenset({'root'})


def _compound_matches(compound, node):
    """单个复合选择器命中这个元素吗？True / False / None(模型不支持)。

    `node` = (标签, 类集合, id, 属性字典)。属性字典是从模板**原样**收上来的，
    所以 `[data-bs-target]`、`[type=checkbox]` 这类判定是精确的，不是猜的。

    看不懂的写法一律返回 None，由调用方报「模型已失效」——**绝不当成不匹配
    放过去**。这是本文件反复出现的教训：静默少扫一条规则，等于给一个 bug
    发免死金牌。
    """
    tag, classes, elem_id, attrs = node
    if '::' in compound:
        return False                       # 伪元素是另一个盒子
    rest = compound
    negatives = []
    while True:
        m = re.search(r':not\(([^()]*)\)', rest)
        if not m:
            break
        negatives.append(m.group(1).strip())
        rest = rest[:m.start()] + ' ' + rest[m.end():]
    if '(' in rest or ')' in rest:
        return None                        # 嵌套 / 函数式伪类，模型不支持
    pseudos = re.findall(r':([-\w]+)', rest)
    # 顺序是承重的：先看有没有「静止态一定不成立」的伪类，再挑剔看不懂的。
    # 反过来的话 `.btn:first-child:active` 会因为 :first-child 被判「模型不支持」，
    # 而它其实根本就是个按下态规则，静止态一定不命中。
    if any(p in _LEGACY_PSEUDO_ELEMENTS or p in _INTERACTIVE_PSEUDOS for p in pseudos):
        return False
    for p in pseudos:
        if p not in _RESTING_PSEUDOS:
            return None
        if p == 'root' and tag != 'html':
            return False
    rest = re.sub(r':[-\w]+', ' ', rest)
    for attr in re.findall(r'\[([^\]]*)\]', rest):
        m = re.fullmatch(r'([-\w]+)\s*(?:([~^$*|]?=)\s*[\'"]?([^\'"]*)[\'"]?)?',
                         attr.strip())
        if m is None:
            return None
        name, op, want = m.group(1).lower(), m.group(2), m.group(3)
        have = attrs.get(name)
        if op is None:                     # `[attr]` 存在性
            if have is None:
                return False
            continue
        if have is None:
            return False
        if op == '=':
            ok = have == want
        elif op == '~=':
            ok = want in have.split()
        elif op == '*=':
            ok = want in have
        elif op == '^=':
            ok = have.startswith(want)
        elif op == '$=':
            ok = have.endswith(want)
        else:
            return None                    # `|=`，本站没用到，不猜
        if not ok:
            return False
    rest = re.sub(r'\[[^\]]*\]', ' ', rest)
    ids = re.findall(r'#([-\w]+)', rest)
    sel_classes = set(re.findall(r'\.([-\w]+)', rest))
    bare = re.sub(r'[#.][-\w]+', ' ', rest).strip()
    if bare and bare != '*':
        if not re.fullmatch(r'[a-zA-Z][-\w]*', bare):
            return None                    # 组合符残渣等，模型不支持
        if bare.lower() != tag:
            return False
    if any(i != elem_id for i in ids):
        return False
    if not sel_classes <= classes:
        return False
    for neg in negatives:
        sub = _compound_matches(neg, node)
        if sub is None:
            return None
        if sub:
            return False                   # :not() 的参数命中了 = 整体不命中
    return True


def _branch_matches(branch, chain):
    """选择器分支命中 chain 末尾那个元素吗？chain = [(tag, classes, id), ...]。

    支持后代（空格）与子（`>`）组合符 —— 两者都能从祖先链精确判定。
    兄弟组合符（`+` `~`）返回 None：本模型不记兄弟节点，当成后代会**多**匹配
    （可能把一条其实管不到的规则算成赢家），当成不匹配又会漏。响亮失败是唯一出路。

    **判定顺序是承重的：先判主体复合项「肯定不命中」，再判形态不支持。**
    反过来写的话，Bootstrap 里一大堆打在 <a>/<label>/伪元素上、与 div 八竿子
    打不着的规则（`.btn-check:checked+.btn`、`.form-floating>.form-control~label::after`）
    会把模型整个顶成「已失效」。第一版就是这么炸的：21 条不支持里没有一条
    真能命中 div。
    """
    parts = _split_branch(branch)
    if parts is None:
        return None
    subject = _compound_matches(parts[-1][0], chain[-1])
    if subject is not True:
        return subject
    if any(comb in ('+', '~') for _c, comb in parts):
        return None
    ancestors = list(chain[:-1])
    # parts[i] 上挂的组合符描述的是 parts[i-1] 与 parts[i] 的关系，
    # 所以从右往左走时要用 parts[i].comb 决定 parts[i-1] 匹配「直接父」还是「任意祖先」。
    for i in range(len(parts) - 1, 0, -1):
        comp, comb = parts[i - 1][0], parts[i][1]
        if comb == '>':
            if not ancestors:
                return False
            r = _compound_matches(comp, ancestors.pop())
            if r is None:
                return None
            if not r:
                return False
            continue
        while ancestors:
            r = _compound_matches(comp, ancestors.pop())
            if r is None:
                return None
            if r:
                break
        else:
            return False
    return True


class _DomChainCollector(HTMLParser):
    """把模板解析成一棵树，产出每个元素的 (tag, classes, id) 祖先链（含自身）。

    Jinja 的 `{% ... %}` / `{{ ... }}` 对 HTMLParser 来说只是文本，不影响标签树。
    void 元素单独列出来，否则 `<img>` 会被当成开标签一直挂在栈上，
    把它后面所有兄弟节点都错算成它的后代。
    """

    VOID = frozenset({
        'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'param', 'source', 'track', 'wbr',
    })

    def __init__(self, prefix):
        super().__init__(convert_charrefs=True)
        self.stack = list(prefix)
        self.chains = []

    def _node(self, tag, attrs):
        a = {k.lower(): (v or '') for k, v in attrs}
        # 第四位是**完整**属性字典。少了它，`[data-bs-target]` / `[type=checkbox]`
        # 这类选择器只能判「模型不支持」，Bootstrap 里一大批规则会把自检顶红。
        return (tag.lower(), set(a.get('class', '').split()), a.get('id', ''), a)

    def handle_starttag(self, tag, attrs):
        node = self._node(tag, attrs)
        self.stack.append(node)
        self.chains.append(list(self.stack))
        if tag.lower() in self.VOID:
            self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.stack.append(self._node(tag, attrs))
        self.chains.append(list(self.stack))
        self.stack.pop()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


# base.html 把页面内容放进 `<main class="main-content">`。
# 页面模板里的元素在浏览器里的真实祖先链因此多这两层。
_PAGE_CHAIN_PREFIX = (
    ('html', set(), '', {}),
    ('body', set(), '', {}),
    ('main', {'main-content'}, '', {}),
)

# 运行时才出现、grep 模板永远看不到的 <div>。
#
# 为什么必须显式登记：`.modal-backdrop` 由 Bootstrap 的 Modal 组件插到
# <body> 末尾，`.task-error` 错误框由 task_list.js 的 TaskRow 组件渲染进
# 时间流——只扫模板的话，这两类一个都看不见，而「弹窗遮罩
# 从来没暗过」正是 C1 收尾要修的那个缺陷。祖先链按真实 DOM 写（CDP 实测
# 确认过层级）。
# （演进：原先的 .task-card 条目随卡片删除，换成同样运行时注入、
# 同样带底色的 .task-error 错误框，链与 _task_error_chain() 一致；
# 2026-08 单一时间流定稿后行实现从 tasks.js 收口到 history.js。）
#
# 每一条都由 test_runtime_injected_div_table_is_grounded 反查来源，防止表烂掉。
_RUNTIME_INJECTED_DIVS = (
    (
        'TaskRow 组件渲染的失败原因框（时间流失败行的行2 div）',
        'static/js/task_list.js', 'task-error',
        _task_error_chain(),
    ),
    (
        'Bootstrap Modal 运行时插到 <body> 末尾的遮罩',
        'static/vendor/bootstrap/5.3.0/bootstrap.bundle.min.js', 'modal-backdrop',
        (
            ('html', set(), '', {}),
            ('body', {'modal-open'}, '', {}),
            ('div', {'modal-backdrop', 'fade', 'show'}, '', {}),
        ),
    ),
    (
        'ui.js showToast 生成的常驻提示条',
        'static/js/ui.js', 'app-toast',
        (
            ('html', set(), '', {}),
            ('body', set(), '', {}),
            ('div', set(), 'app-toast-container', {}),
            ('div', {'app-toast', 'app-toast--info'}, '', {}),
        ),
    ),
    (
        'ui.js showConfirm 生成的确认框与它的遮罩',
        'static/js/ui.js', 'app-confirm',
        (
            ('html', set(), '', {}),
            ('body', set(), '', {}),
            ('div', {'app-confirm-overlay'}, '', {}),
            ('div', {'app-confirm'}, '', {}),
        ),
    ),
)


def _modeled_div_chains():
    """本模型覆盖的全部 <div>：三个页面模板 + base.html + 运行时注入表。

    返回 [(来源说明, chain)]，chain 末尾就是那个 div。
    """
    out = []
    base_chains = _DomChainCollector(())
    base_chains.feed(_template('base.html'))
    base_chains.close()
    for chain in base_chains.chains:
        if chain[-1][0] == 'div':
            out.append(('base.html', chain))
    for name in sorted(n for n in os.listdir(_TEMPLATES_DIR)
                       if n.endswith('.html') and n != 'base.html'):
        c = _DomChainCollector(_PAGE_CHAIN_PREFIX)
        c.feed(_template(name))
        c.close()
        for chain in c.chains:
            if chain[-1][0] == 'div':
                out.append((name, chain))
    for label, _src, _marker, chain in _RUNTIME_INJECTED_DIVS:
        out.append(('运行时: ' + label, list(chain)))
    return out


# 层叠里的一条候选背景声明。
#   sheet_i / rule_i  = 样式表顺序 / 表内规则顺序，用来在同特异度时判先后
#   targeted          = 这条规则的**主体复合项**带不带类/id
#                       —— 即它到底「认不认识」这个元素，见下面主断言的说明
_BgCand = collections.namedtuple(
    '_BgCand', 'sheet sheet_i rule_i sel branch value important spec targeted')


# 在模型假设的渲染环境（现代 Chromium、无 reduce 偏好）下**必然不成立**的
# 条件组 at-rule 白名单：其内部规则在默认环境里必然落选，跳过不算「读不懂」。
# 唯一消费者是 2026-08-11 的玻璃降级块：
#   @supports not (backdrop-filter: blur(1px))    —— Chromium 必然支持，not 为假
#   @media (prefers-reduced-transparency: reduce) —— 默认偏好不是 reduce
# 新增条目意味着「该条件组在建模环境里恒假」，登记前先确认这一点。
# 字符串形态与 _rules_ctx 的规范化输出逐字一致（空白折叠后的 at-rule 头）。
_KNOWN_INACTIVE_AT_RULES = (
    '@supports not (backdrop-filter: blur(1px))',
    '@media (prefers-reduced-transparency: reduce)',
)


def _bg_candidates(chain, sheets):
    """命中 chain 末尾那个元素的全部背景声明。返回 (候选列表, 不支持的规则列表)。"""
    cands, unsupported = [], []
    for sheet_i, (sheet, rules) in enumerate(sheets):
        for sel, body, at_ctx, rule_i in rules:
            decl = _bg_decl(body)
            if decl is None:
                continue
            for branch in _selector_parts(sel):
                hit = _branch_matches(branch, chain)
                if hit is None:
                    unsupported.append((sheet, branch))
                    continue
                if not hit:
                    continue
                if at_ctx:
                    # 白名单内的条件组在建模环境里恒假（见上方常量注释），
                    # 其内部规则必然落选 —— 跳过是正确语义，不是「读不懂」。
                    if any(ctx in _KNOWN_INACTIVE_AT_RULES for ctx in at_ctx):
                        continue
                    # 条件组 at-rule：默认渲染环境（1600x1000、无 reduce 偏好）
                    # 下是否成立没建模。命中了就必须响亮失败，不能当没看见。
                    unsupported.append((sheet, f'{branch} @ {at_ctx}'))
                    continue
                subject = branch.split()[-1]
                targeted = bool(re.search(r'[.#][-\w]+', re.sub(r':not\([^)]*\)', '', subject)))
                cands.append(_BgCand(
                    sheet, sheet_i, rule_i, sel, branch, decl[0], decl[1],
                    _btn_specificity(branch), targeted))
    return cands, unsupported


def _winning_bg(cands):
    """按 CSS 层叠规则挑赢家：先 !important，再特异度，再出现顺序。"""
    if not cands:
        return None
    return max(cands, key=lambda c: (c.important, c.spec, c.sheet_i, c.rule_i))


def _describe(chain):
    tag, classes, elem_id, _attrs = chain[-1]
    bits = tag + ('#' + elem_id if elem_id else '')
    if classes:
        bits += '.' + '.'.join(sorted(classes))
    return bits


def test_the_div_background_cascade_model_still_understands_every_rule():
    """层叠模型的自检：不许有「模型看不懂」的规则悄悄被跳过。

    这条是下面三条结果类断言的**地基**。模型跳过一条它读不懂的规则时不会
    报错，只会少算一个候选者——赢家可能因此算错，而断言全绿。
    Phase 2 的教训（`_rules_ctx` 被一句 `@import` 蒙住 283 条断言）就是这么来的：
    静默少扫没有任何症状。所以把「跳过了什么」单独提出来当一条断言。

    扫描范围：base.html + 三个页面模板里的每一个 <div>（含 base 前缀链），
    外加 _RUNTIME_INJECTED_DIVS 里 4 条运行时注入的 div，
    对照 base.html 里 <link> 顺序加载的全部样式表。
    """
    sheets = _all_sheets()
    chains = _modeled_div_chains()
    assert len(chains) >= 60, (
        f'模型只扫到 {len(chains)} 个 div —— 模板解析多半坏了（正常在 90 上下）'
    )
    bad = collections.Counter()
    for _src, chain in chains:
        _c, unsupported = _bg_candidates(chain, sheets)
        for sheet, branch in unsupported:
            bad[(sheet, branch)] += 1
    assert not bad, (
        '有带背景声明的规则命中了模型里的 div，但模型读不懂它的选择器 / at-rule 上下文。\n'
        '在扩模型之前，下面三条结果类断言算出来的「赢家」都不可信：\n'
        + '\n'.join(f'  {s}: {b}' for (s, b), _n in sorted(bad.items())[:20])
    )


def test_no_div_loses_its_background_to_a_rule_that_does_not_know_it():
    """**本次的主断言**：没有任何 div 的背景可以被一条「不认识它」的规则夺走。

    「不认识它」= 那条规则的**主体复合项里一个类 / id 都没有**（`div`、`*`、
    `div:not(.a):not(.b)...`）。这种规则是按标签一刀切的，它不针对任何具体组件，
    却因为堆 `:not()` 能把特异度堆到任意高，从而压掉每一个组件自己的背景声明。
    被删掉的那条兜底重置正是这个形态：(0,11,1)，比 `.stat-card`(0,1,0) 高 10 个类。

    判定式：某个 div 只要有**任何**一张样式表用「带类 / id 的选择器」给它声明过
    非透明背景（= 有人明确想让这个组件有底色），那么层叠的赢家也必须是一条
    带类 / id 的规则。赢家是「一刀切」规则 = 这个组件的底色被一条不认识它的
    规则抢走了 = 红。

    为什么用这个判定式而不是「赢家必须非透明」：`.leaflet-container` 是反例 ——
    leaflet.css 给它 `#ddd` 浅灰，本站**故意**用 `.leaflet-container{background:
    transparent}` 盖掉，让地图空白处露出宿主面板。那是一个针对性的、写明理由的
    本地决定，不该判红。而兜底重置压 `.modal-backdrop` 不是决定，是误伤。

    改前这条断言会红在哪（CDP 实测同步确认，Chrome 148）：
        div.modal-backdrop.fade.show     rgba(0,0,0,0) -> rgb(0,0,0)
        div.index-right                  rgba(0,0,0,0) -> rgb(12,13,16)
        div.config-section         x6    rgba(0,0,0,0) -> rgb(21,23,28)
        div.stat-card              x8    rgba(0,0,0,0) -> rgb(21,23,28)
        div.modal-header           x2    rgba(0,0,0,0) -> rgb(21,23,28)
        div.task-card              x8    rgba(0,0,0,0) -> rgb(21,23,28)
    其中 modal-backdrop 是硬缺陷：`.modal-backdrop{--bs-backdrop-bg:#000}` 只有
    (0,1,0)，被 (0,11,1) 压成透明，**这个应用从上线到现在打开弹窗背后都没暗过**。

    覆盖范围（诚实说明）：只算静止态（无 hover / focus）、只算默认视口
    （不进 @media，进了会被上面那条自检顶红）、只覆盖
    _modeled_div_chains() 圈定的 div。悬停态与响应式断点下的背景不在范围内。
    """
    sheets = _all_sheets()
    css = _css()
    problems = []
    for src, chain in _modeled_div_chains():
        cands, _ = _bg_candidates(chain, sheets)
        wanted = [c for c in cands
                  if c.targeted and not _bg_is_transparent(css, c.value)]
        if not wanted:
            continue
        win = _winning_bg(cands)
        if win.targeted:
            continue
        problems.append(
            f'{src} 的 {_describe(chain)}\n'
            f'      想要的底色：{wanted[-1].sheet} `{wanted[-1].branch}` '
            f'{{ background: {wanted[-1].value} }} 特异度 {wanted[-1].spec}\n'
            f'      实际赢家：  {win.sheet} `{win.branch}` '
            f'{{ background: {win.value} }} 特异度 {win.spec}'
            + ('（!important）' if win.important else '')
        )
    assert not problems, (
        '这些 div 的背景被一条「主体选择器里没有任何类 / id」的规则夺走了 —— '
        '源码里写着，浏览器里没有：\n' + '\n'.join('  ' + p for p in problems)
    )


def test_no_blanket_type_selector_may_outrank_a_component_background():
    """形态补位：**一刀切**的背景重置，特异度不许 >= (0,1,0)。

    上一条只在「模型里恰好有受害者」时才红。这条不依赖受害者：只要有人再写出
    被删掉的那条兜底重置的形态，当场红，即使那天页面上没有任何 div 受影响。

    「一刀切」的精确定义（四条同时成立才算）：
      1. 单个复合项，没有后代 / 子 / 兄弟组合符 —— `.leaflet-bar a` 有祖先类
         限定，它知道自己在管谁，不算；
      2. 主体不是伪元素（`::-webkit-scrollbar-thumb` 样式化的是另一个盒子，
         夺不走宿主的背景）；
      3. 不带交互伪类（`:hover` 之流只在用户操作时成立，不是静止态的一刀切）；
      4. 除去 `:not()` 之后，选择器里**一个类 / id / 属性都没有** ——
         也就是它只按标签名筛元素，对具体组件一无所知。
         `[class*="col-"]` 有属性限定，它知道自己在管栅格列，不算。

    门槛为什么划在 (0,1,0)：`div{...}` 是 (0,0,1)，输给每一条
    `.foo{background}`(0,1,0)，改不了任何东西；加**一个** `:not(.x)` 就变成
    (0,1,1)，已经压得过所有组件的背景声明。被删掉的兜底重置是 (0,11,1) ——
    11 个 `:not(.class)` 各贡献一个类，纯粹为了赢层叠而堆出来的。

    ⚠️ 也正因为 (0,0,1) 的 `div { background: transparent }` 改不了任何东西，
    本次选的是**整条删除**而不是降级：CDP 实测（三页 6 场景 1699 个元素逐一
    对拍）两种形态渲染结果差异为 0，留着只是把结构债换个写法。

    扫描范围：style.css 顶层 + 全部 @media 内的规则（用 _rules_ctx，
    媒体查询里藏一条同样有效）。
    """
    css = _css()
    offenders = []
    for sel, body, at_ctx in _rules_ctx(css):
        if _bg_decl(body) is None:
            continue
        for branch in _selector_parts(sel):
            parts = _split_branch(branch)
            if parts is None or len(parts) != 1:
                continue                    # 有组合符 = 有上下文限定
            subject = parts[0][0]
            if '::' in subject:
                continue
            pseudos = re.findall(r':([-\w]+)', subject)
            if any(p in _LEGACY_PSEUDO_ELEMENTS or p in _INTERACTIVE_PSEUDOS
                   for p in pseudos):
                continue
            if re.search(r'[.#][-\w]+|\[', re.sub(r':not\([^)]*\)', '', subject)):
                continue                    # 带类 / id / 属性 = 有针对性
            spec = _btn_specificity(branch)
            if spec >= (0, 1, 0):
                offenders.append(
                    f'{branch}  特异度 {spec}'
                    + (f'  （在 {at_ctx} 里）' if at_ctx else '')
                )
    assert not offenders, (
        '这些规则只按标签名一刀切地设背景，特异度却压得过组件自己的 '
        '`.foo{background}`(0,1,0)。\n'
        '这正是被删掉的 `div:not(...)...` 兜底重置的形态，不要让它换个写法回来：\n'
        + '\n'.join('  ' + o for o in offenders)
    )


def test_no_stylesheet_gives_a_bare_div_a_background():
    """前提钉子：四张样式表里没有任何一条用**裸 div 类型选择器**给背景。

    这是「整条删掉兜底重置」这个决定的依据。若哪天 vendor 升级后冒出
    `div{background:#fff}` 之类的规则，本站就真的需要一条 (0,0,1) 的
    `div{background:transparent}` 去压它 —— 那时这条断言变红，逼一次显式决策，
    而不是让首页在某次升级后静默变白。

    扫描范围：base.html 里 <link> 进来的每一张 .css，含 @media 内部。
    """
    hits = []
    for path in _stylesheet_load_order():
        with open(path, encoding='utf-8') as f:
            sheet = f.read()
        for sel, body, at_ctx in _rules_ctx(sheet):
            if _bg_decl(body) is None:
                continue
            for branch in _selector_parts(sel):
                parts = _split_branch(branch)
                if parts is None:
                    continue
                subject = re.sub(r':not\([^)]*\)|::?[-\w]+', '', parts[-1][0]).strip()
                if subject == 'div':
                    hits.append(f'{os.path.basename(path)}: {branch}'
                                + (f' （在 {at_ctx} 里）' if at_ctx else ''))
    assert not hits, (
        '有样式表用裸 `div` 类型选择器给背景。style.css 删掉兜底重置的前提'
        '（「div 的默认背景本来就是透明，没人动它」）不再成立，需要重新决策：\n'
        + '\n'.join('  ' + h for h in hits)
    )


def test_runtime_injected_div_table_is_grounded():
    """`_RUNTIME_INJECTED_DIVS` 的每一条都必须在真实源码里找得到出处。

    这张表是**手写**的 —— 模板里 grep 不到 `.task-error` / `.modal-backdrop`，
    它们由 JS 在运行时插进 DOM，只能人工登记。手写表的通病是烂掉：类名改了、
    组件删了，表还在，上面那条主断言就对着一个不存在的元素空转，全绿。

    这里反查两件事：
      1. 每条登记的类名在它声明的源文件里真的出现过；
      2. 每条链末尾那个 div 确实带着这个类（防止抄错行）。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert len(_RUNTIME_INJECTED_DIVS) == 4, (
        f'运行时注入表有 {len(_RUNTIME_INJECTED_DIVS)} 条，与 docstring 描述的 4 条不符'
    )
    problems = []
    for label, src, marker, chain in _RUNTIME_INJECTED_DIVS:
        path = os.path.join(root, *src.split('/'))
        if not os.path.exists(path):
            problems.append(f'{label}: 声称的出处 {src} 不存在')
            continue
        with open(path, encoding='utf-8', errors='ignore') as f:
            text = f.read()
        if marker not in text:
            problems.append(f'{label}: {src} 里已经找不到类名 `{marker}`')
        if marker not in chain[-1][1]:
            problems.append(f'{label}: 祖先链末尾的 div 没带 `{marker}`')
    assert not problems, (
        '运行时注入 div 表和源码对不上了 —— 主断言正在对着不存在的元素空转：\n'
        + '\n'.join('  ' + p for p in problems)
    )


# Bootstrap 里给容器上背景、但本站**不打算**在 style.css 里覆盖的类，
# 逐条写明为什么可以不管。每一条都是 CDP 实测过的（Chrome 148，删除兜底重置之后）。
_BOOTSTRAP_BG_CLASSES_INTENTIONALLY_UNSTYLED = {
    'btn-close':
        '弹窗右上角的关闭叉。它是 <button>，且 Bootstrap 给的是 '
        '`transparent var(--bs-btn-close-bg) center/1em` —— 一张 data:URI 的 SVG 图标，'
        '不是底色。实测 computed background-color = rgba(0,0,0,0)。',
    'modal-footer':
        'Bootstrap 5.3.0 只写了 `background-color: var(--bs-modal-footer-bg)`，'
        '而这个变量在 5.3.0 里**从未被赋值**，实测 computed 是 rgba(0,0,0,0)。'
        '它落在 .modal-content 的 #15171c 上，视觉上就是弹窗底色，正确。',
    'alert':
        'Bootstrap 的 `.alert{background-color:var(--bs-alert-bg)}` 里那个变量'
        '由变体类赋值，而本站在 style.css 里覆盖的正是变体（.alert-info / '
        '.alert-danger）。实测 #boundsInfo = rgba(59,130,246,0.1)，是本站的值。',
}


def test_no_bootstrap_component_background_reaches_a_div_unreviewed():
    """在用的 Bootstrap 背景组件，要么本站覆盖过，要么写明为什么不覆盖。

    **这条守的是删掉兜底重置之后新出现的长期风险。** 兜底重置在的时候，
    Bootstrap 给任何 div 的背景都被 (0,11,1) 一律压平；删掉之后它们全部生效。
    本站已经覆盖过的组件没问题，但 Bootstrap 5.3.0 里还有一批容器自带
    `#212529` / `#2b3035` / `rgba(33,37,41,.85)` 这类灰，与本站调色板的
    `#15171c` 不同源：

        .dropdown-menu    #212529        .offcanvas        #212529
        .popover          #212529        .toast            rgba(33,37,41,.85)
        .input-group-text #2b3035

    这些类今天在模板和 JS 里一个都不存在，所以不进受害者清单。但如果没有这条
    断言，**将来任何人往页面里加一个这样的组件，拿到的就是 Bootstrap 的灰，
    而且没有任何测试会红**。

    判定方式：把 bootstrap.min.css 里「主体是单个类、且声明了背景」的类，与
    模板 + JS 里真实出现过的类求交集；交集里的每一个类，要么 style.css 里
    有一条选择器带上它的背景规则（= 本站做过决定），要么在
    _BOOTSTRAP_BG_CLASSES_INTENTIONALLY_UNSTYLED 里写明理由。

    JS 也要扫：`.app-toast` 那批就是 JS 拼出来的，只扫模板会漏掉一半界面。
    扫的是 `class="..."`、`className = "..."`、`classList.add('...')` 三种形态。

    覆盖范围（诚实说明）：这条按**类名**判定，不判层叠 —— 「做过决定」不等于
    「决定生效了」。生效与否由上面那条主断言负责。两条一起才是完整的。
    """
    bs_path = os.path.join(_VENDOR_DIR, 'bootstrap', '5.3.0', 'bootstrap.min.css')
    with open(bs_path, encoding='utf-8') as f:
        bs = f.read()
    bs_bg = {}
    for sel, body, _ctx in _rules_ctx(bs):
        decl = _bg_decl(body)
        if decl is None:
            continue
        for branch in _selector_parts(sel):
            parts = _split_branch(branch)
            if parts is None:
                continue
            if re.fullmatch(r'\.[-\w]+', parts[-1][0]):
                bs_bg.setdefault(parts[-1][0][1:], decl[0])
    assert len(bs_bg) > 50, (
        f'只从 bootstrap.min.css 里扫出 {len(bs_bg)} 个带背景的类 —— 解析多半坏了'
    )

    used = set()
    for name in os.listdir(_TEMPLATES_DIR):
        if name.endswith('.html'):
            for attr in re.findall(r'class="([^"]*)"', _template(name)):
                used |= set(attr.split())
    for name in sorted(n for n in os.listdir(_JS_DIR) if n.endswith('.js')):
        src = re.sub(r'/\*.*?\*/', '', _js(name), flags=re.S)
        src = re.sub(r'(?m)^\s*//.*$', '', src)
        for attr in re.findall(r'class="([^"]*)"', src):
            used |= set(re.sub(r'\$\{[^}]*\}', ' ', attr).split())
        for attr in re.findall(r"className\s*=\s*['\"]([^'\"]*)['\"]", src):
            used |= set(attr.split())
        for call in re.findall(r"classList\.(?:add|toggle)\(([^)]*)\)", src):
            used |= set(re.findall(r"['\"]([-\w]+)['\"]", call))
    assert len(used) > 80, f'只从模板 + JS 里扫出 {len(used)} 个类名 —— 解析多半坏了'

    styled = set()
    for sel, body, _ctx in _rules_ctx(_css()):
        if _bg_decl(body) is None:
            continue
        for branch in _selector_parts(sel):
            parts = _split_branch(branch)
            if parts is None:
                continue
            styled |= set(re.findall(r'\.([-\w]+)', parts[-1][0]))

    unreviewed = sorted(
        c for c in (set(bs_bg) & used)
        if c not in styled and c not in _BOOTSTRAP_BG_CLASSES_INTENTIONALLY_UNSTYLED
    )
    assert not unreviewed, (
        '这些 Bootstrap 组件类在模板 / JS 里用上了，Bootstrap 会给它们上背景，'
        '而 style.css 里没有任何背景规则提到它们 —— 界面上会出现 Bootstrap 自己的灰，'
        '不是本站调色板的颜色。\n'
        '要么在 style.css 里覆盖，要么登记进 '
        '_BOOTSTRAP_BG_CLASSES_INTENTIONALLY_UNSTYLED 并写明理由：\n'
        + '\n'.join(f'  .{c}  Bootstrap 给的是 {bs_bg[c]}' for c in unreviewed)
    )

    stale = sorted(
        c for c in _BOOTSTRAP_BG_CLASSES_INTENTIONALLY_UNSTYLED
        if c not in (set(bs_bg) & used)
    )
    assert not stale, (
        '豁免表里有已经不成立的条目（组件不再使用，或 Bootstrap 不再给它背景），'
        '留着只会让下一个人以为这里被想过：\n' + '\n'.join('  .' + c for c in stale)
    )


# 本次「根治兜底重置」修复的**全部受害者**，逐个登记期望值。
#
# 为什么在通用层叠断言之外还要这张表：变异实验 M4 暴露的盲区 ——
# 通用断言的判定式是「有人声明了背景，就必须是它赢」，那么**把声明本身删掉**
# 就没有受害者可查，测试全绿而 `.stat-card` 在页面上重新变透明。
# 这张表把「这些底色必须存在」写死，堵住那条路。
#
# 值全部来自 CDP 实测（Chrome 148，1600x1000，删除兜底重置之后）。
# 「改前」一栏是同一台浏览器在删除之前读到的 computed background-color。
_DIV_BACKGROUNDS_THAT_MUST_RENDER = {
    # 类名/说明: (取链的方式, 期望的最终色, 改前实测值)
    # 2026-07 UX 改版：.index-right（dock 面板）随 dock 整体移除，
    # 不再是受害者，从清单删除。
    '.config-section（配置页 6 个分区）':
        ('config.html', {'config-section'}, '--color-bg-secondary', 'rgba(0, 0, 0, 0)'),
    '.stat-card（历史页 4 张统计卡）':
        ('history.html', {'stat-card'}, '--color-bg-secondary', 'rgba(0, 0, 0, 0)'),
    '.modal-header（详情弹窗标题栏，2026-08 起标记在 base.html；2026-08-11 改版随 .modal-content 升入 elevated 档）':
        ('base.html', {'modal-header'}, '--color-bg-elevated', 'rgba(0, 0, 0, 0)'),
}


def _chains_with_classes(template, classes):
    """模板里所有 class 包含 `classes` 的 div 的祖先链。"""
    return [chain for src, chain in _modeled_div_chains()
            if src == template and classes <= chain[-1][1]]


def test_every_victim_of_the_deleted_blanket_reset_renders_its_background():
    """兜底重置的每一个受害者，底色都必须**存在**且是登记的那个值。

    这条与上面的通用层叠断言是互补的，不是重复：
      - 通用断言查「声明存在时，它有没有赢」——挡住「有人又写了条更强的规则」；
      - 这条查「声明还在不在」——挡住「有人直接把声明删了」。
        变异 M4（删掉 `.stat-card { background }`）在只有通用断言时**全绿**，
        因为没有声明就没有受害者。这条会红。

    受害者清单是实测出来的，不是抄文档的：删除兜底重置前后，三个页面 6 个场景
    共 1699 个元素逐元素对拍 computed background-color，28 个元素发生变化。
    去重后的 6 类里，这里登记 4 个模板里的；另外两个在运行时才存在，
    由下面两条单独查（`.task-error` 与 `div.modal-backdrop`；
    `.task-error` 是统一任务表改版后的接替者——原来的条目是已删除的 `.task-card`）。

    `.card-header` 也在那 28 个里，但它声明的值本来就是 transparent，
    视觉零差异，不登记。
    """
    css = _css()
    problems = []
    for label, (template, classes, expect, before) in \
            sorted(_DIV_BACKGROUNDS_THAT_MUST_RENDER.items()):
        chains = _chains_with_classes(template, classes)
        if not chains:
            problems.append(f'{label}: {template} 里已经找不到这个 div —— 清单过期')
            continue
        want = _palette_var(css, expect) if expect.startswith('--') else expect
        for chain in chains:
            win = _effective_bg_for(chain)
            if win is None or _bg_is_transparent(css, win.value):
                problems.append(
                    f'{label}: 最终底色又变回透明了（改前实测就是 {before}）'
                    + (f'，赢家是 {win.sheet} 的 `{win.branch}`' if win else '，没有任何声明命中它')
                )
                continue
            got = _resolve_color(css, win.value)
            if got.replace(' ', '') != want.replace(' ', ''):
                problems.append(
                    f'{label}: 最终底色是 {got}（来自 {win.sheet} 的 `{win.branch}`），'
                    f'登记的期望是 {want}'
                )
    assert not problems, (
        '兜底重置的受害者又失去底色了 —— 这些正是这次修复要让它们出现的：\n'
        + '\n'.join('  ' + p for p in problems)
    )


def test_task_error_and_modal_backdrop_render_their_background():
    """两个运行时注入元素的背景契约：错误框**不得有底色** + 弹窗遮罩必须变暗。

    （前身是 test_task_card_and_modal_backdrop_render_their_background。
    统一任务表改版把 .task-card 换成 .task-error 错误框；2026-08 扁平化
    再把错误框从红底盒子改为无边底引文式——所以这一条对 .task-error
    的期望从「底纹必须是 --color-danger-bg」翻面为「不得有不透明底色」，
    可读性改由文字色承担，见 test_task_error_box_exists_and_is_readable。）

    `div.modal-backdrop` 是本次最硬的验收点。Bootstrap 的
    `.modal-backdrop{--bs-backdrop-bg:#000; background-color:var(--bs-backdrop-bg)}`
    只有 (0,1,0)，被兜底重置的 (0,11,1) 压成透明 —— **这个应用从上线到现在，
    打开任何弹窗背后都没有暗过**。元素在、opacity 是 0.5，就是没有颜色。

    CDP 合成像素实测（Chrome 148，1600x1000，history 页详情弹窗，
    取 x=60 / x=1540 两处——对话框居中占 x∈[400,1200]，这两点只有遮罩）：
        改前：不开弹窗 rgb(12,13,16) -> 开弹窗 rgb(12,13,16)   零变化
        改后：不开弹窗 rgb(12,13,16) -> 开弹窗 rgb(6,6,8)      正好压暗一半
    6 ≈ 12*0.5、8 = 16*0.5，与 `#000` @ opacity .5 的合成结果逐通道吻合。
    """
    css = _css()
    lookup = {}
    for src, chain in _modeled_div_chains():
        for cls in ('task-error', 'modal-backdrop'):
            if cls in chain[-1][1]:
                lookup[cls] = chain
    assert set(lookup) == {'task-error', 'modal-backdrop'}, (
        f'运行时注入表里找不到 task-error / modal-backdrop —— 本测试已失效，'
        f'只找到 {sorted(lookup)}'
    )
    problems = []
    win = _effective_bg_for(lookup['task-error'])
    if win is not None and not _bg_is_transparent(css, win.value):
        problems.append(
            f'.task-error 又有了不透明底色 {win.value} —— 2026-08 扁平化后'
            '它是无边底引文式（红字 + 2px 左边条），铺底色就是回潮'
        )
    win = _effective_bg_for(lookup['modal-backdrop'])
    if win is None:
        problems.append('div.modal-backdrop 没有任何背景声明命中 —— 弹窗遮罩不会变暗')
    elif _bg_is_transparent(css, win.value):
        problems.append(
            f'div.modal-backdrop 的最终背景是 {win.value}（来自 {win.sheet} 的 '
            f'`{win.branch}`）—— 遮罩透明，打开弹窗背后不会变暗。'
            '这正是 vendor 本地化交付时唯一未达标的那一项。'
        )
    assert not problems, '\n'.join('  ' + p for p in problems)


# ---------------------------------------------------------------------------
# M16 / M17：可交互控件的「边界看得见」与「焦点看得见」
# ---------------------------------------------------------------------------

CONTROL_BORDER_MIN_CONTRAST = 3.0   # WCAG 1.4.11 非文本对比，与本文件另外三条同档


def _theme_var(css, name, theme):
    """取某个主题块里的令牌值。theme='dark' 读 :root，'light' 读 light 覆盖块。

    ⚠️ 2026-08-15 修：切点必须落在**去注释之后**的文本上。style.css:349 那段
    注释里原样引用了 `:root[data-bs-theme="light"]` 这个选择器，拿原文
    `css.index(...)` 找到的是注释里那一处 —— 于是 dark 分支被截断在 349 行，
    凡是定义在那之后的令牌（--color-control-hover、--color-backdrop*、
    --color-overlay-surface、棋盘格那几个）一律报「dark 主题里找不到」。
    现有消费者读的令牌都定义在 349 之前，所以这个 bug 一直没露头；
    tests/test_elevation_glass.py 的 `_regions()` 同一个坑同日一起修的。
    """
    stripped = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    marker = ':root[data-bs-theme="light"]'
    assert marker in stripped, f'{marker} 不在样式表里 —— 本测试已失效'
    block = stripped[:stripped.index(marker)] if theme == 'dark' \
        else stripped[stripped.index(marker):]
    m = re.search(re.escape(name) + r'\s*:\s*([^;]+);', block)
    assert m, f'{theme} 主题里找不到 {name} —— 本测试已失效'
    return m.group(1).strip().lower()


def test_infobox_description_token_pair_meets_wcag_aa():
    """Cesium InfoBox 描述区的「面 + 字」令牌对，明暗两套都要过 4.5:1。

    这条是 history.js 那段 description 的**对比度守卫**，它替代了旧口径：
    改前 InfoBox 标题色是内联的 `var(--color-accent-hover)`，由
    test_inline_style_colors_meet_wcag_aa_everywhere 扫到，但那条把它拿去和
    **任务卡面板底色**比 —— InfoBox 根本不长在那个面上，比出来的数字没有意义。

    2026-08-15 起那段 HTML 自己带一段 <style>，把面设成 --color-bg-elevated、
    字设成 --color-text-primary（值在 JS 里从令牌解析后拼进去，因为令牌不跨
    iframe 的 document）。所以要守的就是这两对令牌本身：
      · 正文：--color-text-primary on --color-bg-elevated
      · 标题：--color-accent-hover on --color-bg-elevated
    实测（真浏览器，2026-08-15）暗色 #e8eaed / #7dd3fc on #242a33、
    亮色 rgb(28,33,40) / #0c4a6e on #ffffff，三行全部可读；改前 Cesium 的近白
    文字压在 iframe 的白画布上，状态与张数那两行**肉眼看不见**。
    """
    css = _css()
    for theme in ('dark', 'light'):
        surface = _theme_var(css, '--color-bg-elevated', theme)
        base = _flatten(surface, surface)
        for token in ('--color-text-primary', '--color-accent-hover'):
            fg = _flatten(_theme_var(css, token, theme), surface)
            ratio = _contrast_ratio(fg, base)
            assert ratio >= WCAG_AA_TEXT_CONTRAST, (
                f'{theme} 主题：{token}({fg}) 压在 --color-bg-elevated({base}) 上'
                f'只有 {ratio:.2f}:1，低于 {WCAG_AA_TEXT_CONTRAST} —— '
                'Cesium InfoBox 的描述区就是这个面，改令牌前先看这条'
            )


def test_unchecked_form_check_border_meets_graphic_contrast():
    """未勾选的复选框/单选框，其边界对周围表面必须 >= 3:1（WCAG 1.4.11）。

    为什么这条最要紧：复选框除了那个方框什么都没有 —— 边界看不见就等于控件
    看不见。改前用的是 --color-border（rgba 弱分隔线级别），合成后暗色
    **1.35:1**、亮色 **1.36:1**，比 Bootstrap 自己的 --bs-border-color(#495057,
    2.19:1) 还差，站内覆盖把它压成了原来的 62%。而本项目对图形元素反复声明过
    3:1 下限（PROGRESS_FILL_MIN_CONTRAST / ERROR_BORDER_MIN_CONTRAST /
    BTN_RING_MIN_CONTRAST 三条都是 3.0），唯独 form-check 一条覆盖都没有
    （改前 `grep -c "form-check" 本文件` = 0）。

    取值必须实算：常见的 #9ca3af 对白底只有 2.54:1，照抄会写出一个通不过本
    断言的令牌。
    """
    css = _css()
    m = re.search(r'\.form-check-input\s*\{([^}]*)\}', css)
    assert m, '找不到 .form-check-input 规则 —— 本测试已失效'
    decls = _decl_map(m.group(1))
    border_decl = decls.get('border', '')
    assert '--color-control-border' in border_decl, (
        f'.form-check-input 的边框应使用专用令牌 --color-control-border，'
        f'实际: {border_decl!r}。--color-border 是分隔线级别的弱边框，'
        f'合成后只有 1.35:1。'
    )

    for theme in ('dark', 'light'):
        border = _theme_var(css, '--color-control-border', theme)
        surface = _theme_var(css, '--color-bg-secondary', theme)
        fill = _theme_var(css, '--color-bg-tertiary', theme)
        border_rgb = _flatten(border, surface)
        ratio = _contrast_ratio(border_rgb, _flatten(surface, surface))
        assert ratio >= CONTROL_BORDER_MIN_CONTRAST, (
            f'{theme} 主题：未勾选控件的边界对周围表面只有 {ratio:.2f}:1'
            f'（要求 >= {CONTROL_BORDER_MIN_CONTRAST}:1）。边界看不见 = 控件看不见。'
        )
        inner = _contrast_ratio(border_rgb, _flatten(fill, surface))
        assert inner >= CONTROL_BORDER_MIN_CONTRAST, (
            f'{theme} 主题：边界对控件自身填充只有 {inner:.2f}:1'
            f'（要求 >= {CONTROL_BORDER_MIN_CONTRAST}:1）'
        )


def test_map_panel_button_focus_ring_is_not_clipped_by_its_container():
    """工具条按钮的键盘焦点圈必须画在 padding box 之内。

    容器 `.map-panel-triggers` 有 `overflow: hidden`（为了让圆角裁掉首尾按钮的
    hover 底色），且无内边距、内容盒宽度正好等于按钮的 40px。全站通配的
    `*:focus-visible { outline-offset: 2px }` 画出的整圈都落在 padding box 之外，
    而 outline 不计入 scrollable overflow region —— 会被直接剪掉。Playwright
    实测：单按钮分组（「框选」）聚焦后屏幕上一条焦点线都没有；三按钮分组的
    中间按钮只剩两截压在邻居身上的水平线。

    既有的 test_focus_visible_has_a_visible_outline 照不到它：那条只遍历
    BUTTON_CONTEXTS 里的 11 个 `.btn` 上下文，且不建模祖先裁剪。
    """
    css = _css()
    container = re.search(r'\.map-panel-triggers\s*\{([^}]*)\}', css)
    assert container, '找不到 .map-panel-triggers 规则 —— 本测试已失效'
    clips = _decl_map(container.group(1)).get('overflow', 'visible') != 'visible'

    rule = re.search(r'\.map-panel-btn:focus-visible\s*\{([^}]*)\}', css)
    if not clips:
        return  # 容器不再裁剪，通配的正 offset 就够用了
    assert rule, (
        '.map-panel-btn 没有自己的 :focus-visible 规则 —— 会落到通配的 '
        'outline-offset: 2px，整圈被容器的 overflow:hidden 剪掉'
    )
    decls = _decl_map(rule.group(1))
    offset = decls.get('outline-offset', '')
    assert offset.strip().startswith('-'), (
        f'outline-offset 是 {offset!r}，非负值会把焦点圈画到 padding box 之外，'
        f'被 .map-panel-triggers 的 overflow:hidden 裁掉'
    )
    outline = decls.get('outline', '')
    assert outline and 'none' not in outline.lower(), (
        f'.map-panel-btn:focus-visible 必须画出可见轮廓，实际: {outline!r}')
