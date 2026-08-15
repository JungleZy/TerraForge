"""Elevation 阶梯与玻璃浮层的契约测试(2026-08-11 UI 改版,设计 §3.1/§3.2)。

设计来源:docs/superpowers/specs/2026-08-11-geolibre-inspired-ui-design.md
- --color-bg-elevated:背景三层之上的第四档(越高越亮),给模态/滑出面板/下拉。
- --color-glass-surface / --color-glass-border:浮在地图上的元素(左列工具条
  胶囊、状态栏胶囊、地图浮层 chip、地图搜索框)统一半透明 + backdrop-filter;
  不支持 backdrop-filter 或用户要求降低透明度时退回不透明。
"""

import os
import re

CSS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'static', 'css', 'style.css',
)

# 玻璃化四对象:浮在地图之上的全部常驻 chrome。这份名单只用来正向钉住
# 「这几个已知表面必须仍是玻璃」;降级块的名单是从 CSS 自动发现的
# (_glass_surfaces),加第 5 个玻璃面时不改这里也会红。
GLASS_SELECTORS = ('.map-panel-triggers', '.statusbar-pill', '.map-overlay-chip',
                   '.map-search__field')

# 令牌在暗/亮两个区段都要有定义。
GLASS_TOKENS = ('--color-bg-elevated', '--color-glass-surface', '--color-glass-border')


def _css():
    with open(CSS_PATH, encoding='utf-8') as f:
        return f.read()


def _strip_comments(css):
    return re.sub(r'/\*.*?\*/', '', css, flags=re.S)


def _regions(css):
    """(暗色区, 亮色区)。

    2026-08-15:改前直接在原文上 `css.index(':root[data-bs-theme="light"]')`
    (与 test_css_contract._theme_var 同一个切法),但 style.css:349 的注释里
    原样引用了这个选择器 —— 切点会落在注释里、暗色区被截断在 :349,凡是定义在
    那之后的令牌(--color-overlay-surface 在 :377)一律「未定义」。先去注释再切。
    """
    stripped = _strip_comments(css)
    idx = stripped.index(':root[data-bs-theme="light"]')
    return stripped[:idx], stripped[idx:]


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


_BACKDROP_RE = re.compile(r'backdrop-filter\s*:\s*([^;}]+)')
_BG_VAR_RE = re.compile(r'background(?:-color)?\s*:\s*var\(\s*(--[\w-]+)\s*\)')


def _rules(css):
    """逐条产出 (选择器列表, 规则体)。

    与 _rule_body 同一个「最内层规则」正则,所以 @-块里的规则也会被产出
    (取到的是块内那条规则自己的选择器,不带 @ 头)。
    """
    for m in re.finditer(r'([^{}@]+)\{([^{}]*)\}', _strip_comments(css)):
        sels = [s.strip() for s in m.group(1).split(',') if s.strip()]
        if sels:
            yield sels, m.group(2)


def _glass_surfaces(css):
    """从 CSS 自动发现「真玻璃面」:同时用两个 glass 令牌 **且** 声明了
    backdrop-filter 的规则的选择器。

    为什么把「有 backdrop-filter」也算进入选条件:.drop-veil__tip 同样吃这两个
    glass 令牌,但它是压在自己那层实底遮罩(--color-backdrop-strong)上的提示牌,
    底下不是影像瓦片,既不需要模糊也不需要降级 —— 只按令牌入选会把它误判成
    第 5 个玻璃面、逼着往降级块里加一条没有意义的规则。
    真玻璃面的定义就是「半透明压在地图瓦片上、靠模糊拿回对比度」,而
    backdrop-filter 正是这个定义里那个「靠模糊」。
    """
    found = []
    for sels, body in _rules(css):
        if ('var(--color-glass-surface)' in body
                and 'var(--color-glass-border)' in body
                and _BACKDROP_RE.search(body)):
            found.extend(sels)
    return found


def _block_rules(block):
    """降级块内 选择器 -> 规则体(一个选择器出现多次时保留最后一条)。"""
    out = {}
    for sels, body in _rules(block):
        for sel in sels:
            out[sel] = body
    return out


def _alpha(color):
    """颜色的 alpha。#rrggbb 与 rgb() 都算 1.0。"""
    m = _RGBA_RE.match(color.strip())
    if not m:
        return 1.0
    return 1.0 if m.group(4) is None else float(m.group(4))


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
    """降级名单从 CSS 自动发现,不是硬编码 —— 加第 N 个玻璃面时本项自己会响。

    2026-08-15 改:改前遍历硬编码的三元组,所以第 4 个玻璃面
    (.map-search__field)漏进降级块时全绿。现在的判据是「凡是 glass 令牌 +
    backdrop-filter 的规则,都必须在两个降级块里各有一条自己的降级支」。
    """
    css = _css()
    glass = _glass_surfaces(css)
    # 发现逻辑本身要有下界:正则失效时 glass 会变空,循环空转就成恒真断言了。
    missing = [s for s in GLASS_SELECTORS if s not in glass]
    assert not missing, f'玻璃面发现逻辑失效,已知玻璃面没被认出来:{missing}'

    for header, label in (
        ('@supports not (backdrop-filter: blur(1px))', '@supports'),
        ('@media (prefers-reduced-transparency: reduce)', 'prefers-reduced-transparency'),
    ):
        rules = _block_rules(_at_block(css, header))
        for sel in glass:
            assert sel in rules, (
                f'{label} 降级块缺 {sel} —— 它是玻璃面(glass 令牌 + backdrop-filter),'
                f'没有降级支时会半透明且不模糊地压在影像瓦片上'
            )
            body = rules[sel]
            bf = _BACKDROP_RE.search(body)
            assert bf and bf.group(1).strip() == 'none', (
                f'{label} 降级块的 {sel} 应显式 backdrop-filter: none'
            )
            bg = _BG_VAR_RE.search(body)
            assert bg, f'{label} 降级块的 {sel} 没给回落底色令牌'
            assert bg.group(1) != '--color-glass-surface', (
                f'{label} 降级块的 {sel} 回落到了玻璃底色本身,等于没降级'
            )
            # 回落底色必须「不透明」。下界取 0.92 而不是 1.0:地图浮层 chip 回落到
            # --color-overlay-surface(暗 0.92 / 亮 0.94),那是它玻璃化之前的原值;
            # 玻璃底本身是 0.72/0.78,离 0.92 有整档距离,拦得住「回落回半透明」。
            for region, theme in zip(_regions(css), ('暗色', '亮色')):
                value = _token(region, bg.group(1))
                assert _alpha(value) >= 0.92, (
                    f'{label} 降级块的 {sel} 回落到 {bg.group(1)}({theme}={value}),'
                    f'alpha {_alpha(value)} < 0.92 —— 降级后仍是半透明,而此时'
                    f'已经没有模糊兜底,文字对比度没有下界'
                )


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
