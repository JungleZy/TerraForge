"""Elevation 阶梯与玻璃浮层的契约测试(2026-08-11 UI 改版,设计 §3.1/§3.2)。

设计来源:docs/superpowers/specs/2026-08-11-geolibre-inspired-ui-design.md
- --color-bg-elevated:背景三层之上的第四档(越高越亮),给模态/滑出面板/下拉。
- --color-glass-surface / --color-glass-border:浮在地图上的元素(左列工具条
  胶囊、状态栏胶囊、地图浮层 chip)统一半透明 + backdrop-filter;
  不支持 backdrop-filter 或用户要求降低透明度时退回不透明。
"""

import os
import re

CSS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'static', 'css', 'style.css',
)

# 玻璃化三对象:浮在地图之上的全部常驻 chrome。
GLASS_SELECTORS = ('.map-panel-triggers', '.statusbar-pill', '.map-overlay-chip')

# 令牌在暗/亮两个区段都要有定义。
GLASS_TOKENS = ('--color-bg-elevated', '--color-glass-surface', '--color-glass-border')


def _css():
    with open(CSS_PATH, encoding='utf-8') as f:
        return f.read()


def _strip_comments(css):
    return re.sub(r'/\*.*?\*/', '', css, flags=re.S)


def _regions(css):
    """(暗色区, 亮色区) —— 与 test_css_contract._theme_var 同一个切法。"""
    idx = css.index(':root[data-bs-theme="light"]')
    return css[:idx], css[idx:]


def _rule_body(css, selector):
    """取某选择器(精确匹配)第一个规则体。"""
    for m in re.finditer(r'([^{}@]+)\{([^{}]*)\}', _strip_comments(css)):
        if m.group(1).strip() == selector:
            return m.group(2)
    return None


def _at_block(css, header):
    """取某个 @-规则块的全文(按花括号配平)。"""
    i = css.index(header)
    start = css.index('{', i)
    depth = 0
    for j in range(start, len(css)):
        if css[j] == '{':
            depth += 1
        elif css[j] == '}':
            depth -= 1
            if depth == 0:
                return css[start:j + 1]
    raise AssertionError(f'{header} 块花括号不配平')


def _token(css, name):
    m = re.search(re.escape(name) + r'\s*:\s*([^;]+);', css)
    assert m, f'{name} 未定义'
    return m.group(1).strip()


# ---- WCAG 对比度(与 test_css_contract 同一套算法,独立副本) ----------------

def _lum(hexc):
    h = hexc.lstrip('#')

    def f(v):
        c = v / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (f(int(h[i:i + 2], 16)) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = _lum(a), _lum(b)
    lo, hi = min(la, lb), max(la, lb)
    return (hi + 0.05) / (lo + 0.05)


_RGBA_RE = re.compile(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)')


def _flatten(color, backdrop_hex):
    """半透明色压到不透明底上,得到肉眼实际看到的 #rrggbb。"""
    color = color.strip()
    if re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        return color.lower()
    m = _RGBA_RE.fullmatch(color)
    assert m, f'{color!r} 既不是 #rrggbb 也不是 rgb(a)() —— 本测试算不了它'
    a = float(m.group(4)) if m.group(4) is not None else 1.0
    fg = [int(m.group(i)) for i in (1, 2, 3)]
    bg = [int(backdrop_hex.lstrip('#')[i:i + 2], 16) for i in (0, 2, 4)]
    return '#%02x%02x%02x' % tuple(round(a * f + (1 - a) * b) for f, b in zip(fg, bg))


# ---------------------------------------------------------------------------

def test_elevation_and_glass_tokens_defined_in_both_themes():
    css = _css()
    dark, light = _regions(css)
    for name in GLASS_TOKENS:
        assert _token(dark, name), f'暗色区缺 {name}'
        assert _token(light, name), f'亮色区缺 {name}'


def test_glass_surfaces_use_glass_tokens_and_backdrop_filter():
    css = _css()
    for sel in GLASS_SELECTORS:
        body = _rule_body(css, sel)
        assert body, f'{sel} 规则不存在'
        assert 'var(--color-glass-surface)' in body, f'{sel} 没用玻璃底色令牌'
        assert 'var(--color-glass-border)' in body, f'{sel} 没用玻璃描边令牌'
        assert 'blur(12px)' in body and 'saturate(140%)' in body, (
            f'{sel} 缺 backdrop-filter: blur(12px) saturate(140%)'
        )


def test_glass_fallback_blocks_exist_and_are_opaque():
    css = _css()
    supports = _at_block(css, '@supports not (backdrop-filter: blur(1px))')
    reduced = _at_block(css, '@media (prefers-reduced-transparency: reduce)')
    for block, label in ((supports, '@supports'), (reduced, 'prefers-reduced-transparency')):
        for sel in GLASS_SELECTORS:
            assert sel in block, f'{label} 降级块缺 {sel}'
        assert 'var(--color-bg-secondary)' in block, (
            f'{label} 降级块:工具条/状态栏胶囊应退回改造前不透明的 --color-bg-secondary'
        )
        assert 'var(--color-overlay-surface)' in block, (
            f'{label} 降级块:地图浮层 chip 应退回 0.92 的 --color-overlay-surface'
        )
        assert 'backdrop-filter: none' in block, f'{label} 降级块应关掉 backdrop-filter'


def test_glass_text_contrast_holds_in_both_themes():
    """玻璃底合成到页面底(最坏近似,与 test_css_contract 同一做法)之后,
    text-primary / text-secondary 仍要 >= 4.5:1 —— 这是玻璃 alpha 的下限。"""
    css = _css()
    dark, light = _regions(css)
    for region, label in ((dark, '暗色'), (light, '亮色')):
        flat = _flatten(_token(region, '--color-glass-surface'),
                        _token(region, '--color-bg-primary'))
        for text_token in ('--color-text-primary', '--color-text-secondary'):
            ink = _token(region, text_token)
            ratio = _contrast(ink, flat)
            assert ratio >= 4.5, (
                f'{label}: {text_token}({ink}) 压在玻璃合成底 {flat} 上只有 '
                f'{ratio:.2f}:1 —— 玻璃 alpha 降过头了'
            )


def test_elevated_token_consumed_by_top_layer_surfaces():
    css = _css()
    # 设计 §3.2 原文含「下拉菜单」,但全站 markup 里没有任何 .dropdown-menu
    # 消费者(零引用)——为它写规则是死代码,本项刻意只钉真实存在的最高层表面。
    for sel in ('.workbench-panel', '.modal-content', '.modal-header'):
        body = _rule_body(css, sel)
        assert body, f'{sel} 规则不存在'
        assert 'var(--color-bg-elevated)' in body, (
            f'{sel} 是最高层表面,应使用 --color-bg-elevated'
        )
