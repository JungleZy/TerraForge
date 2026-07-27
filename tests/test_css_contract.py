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


def _rules(css):
    """扫描出全部 (选择器, 规则体)，包含 @media 内部的规则。

    用花括号深度扫描而不是单条正则——正则 `([^{}]+)\\{([^{}]*)\\}` 会被
    @media 的嵌套花括号带偏，漏掉媒体查询里的规则（Phase 1 的教训：
    只匹配第一条 / 只匹配顶层的正则等于静默漏检）。
    """
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    out = []
    stack = []
    token = ''
    for ch in css:
        if ch == '{':
            stack.append(token)
            token = ''
        elif ch == '}':
            sel = _norm_selector(stack.pop()) if stack else ''
            if sel and not sel.startswith('@'):
                out.append((sel, token))
            token = ''
        else:
            token += ch
    return out


def _font_size_decls(body):
    """规则体里的 font-size 声明列表（原样返回值，含可能的 !important）。"""
    return [
        m.group(1).strip()
        for m in re.finditer(r'(?<![-\w])font-size\s*:\s*([^;}]+)', body)
    ]


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
            if '!important' in decl:
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
    """!important 声明总量上界 = 70。

    阈值构成（全部实测，不是估的）：
      清理前 92 处
      - 24 处：被删除的「统一字体大小系统」覆盖块里的 font-size !important
      -  1 处：.form-text 的 font-size !important（同一形态，一并清掉；
               它的 color !important 保留，不在本次范围）
      = 67 处（实测清理后的真实值）
      + 3 处余量：留给后续任务里个别确实必须压 Bootstrap 的新规则
      = 70

    余下 67 处几乎全是压 Bootstrap 背景/文字色的历史债
    （`background: transparent !important`、`color: ... !important`），
    属于 Phase 2 其他任务的范围，本次不动。

    注意：注释里被剥掉了才计数——否则一句提到 !important 的说明文字就能
    把数字顶上去（本条测试自己的实现就踩过这个坑）。
    """
    css = re.sub(r'/\*.*?\*/', '', _css(), flags=re.S)
    count = css.count('!important')
    assert count <= 70, (
        f'!important 声明有 {count} 处，应 <= 70（清理前 92，本次清理后实测 67，余量 3）'
    )
