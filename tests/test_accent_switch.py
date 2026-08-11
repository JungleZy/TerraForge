"""强调色预设(sky / teal / violet / rose / orange)的设计契约测试。

机制与主题(tf-theme)完全同构:localStorage `tf-accent` + <html> 的
`data-accent` 属性 + base.html 引导脚本首帧前同步;sky 是缺省品牌色,
不落属性、不写覆盖块。数值见设计文档 §3.5(已实算 WCAG)。
"""

import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_HTML = os.path.join(ROOT, 'templates', 'base.html')
CONFIG_PARTIAL = os.path.join(ROOT, 'templates', '_config_content.html')
THEME_JS = os.path.join(ROOT, 'static', 'js', 'theme.js')
CSS_PATH = os.path.join(ROOT, 'static', 'css', 'style.css')

ACCENTS = ('sky', 'teal', 'violet', 'rose', 'orange')
OVERRIDDEN = ('teal', 'violet', 'rose', 'orange')   # sky 无覆盖块

# (accent, hover, strong, on-accent),暗色 / 亮色。设计文档 §3.5 表格。
EXPECTED = {
    'teal':   {'dark': ('#2dd4bf', '#5eead4', '#14b8a6', '#020617'),
               'light': ('#115e59', '#134e4a', '#0f766e', '#ffffff')},
    'violet': {'dark': ('#a78bfa', '#c4b5fd', '#8b5cf6', '#020617'),
               'light': ('#5b21b6', '#4c1d95', '#6d28d9', '#ffffff')},
    'rose':   {'dark': ('#fb7185', '#fda4af', '#f43f5e', '#020617'),
               'light': ('#9f1239', '#881337', '#be123c', '#ffffff')},
    'orange': {'dark': ('#fb923c', '#fdba74', '#f97316', '#020617'),
               'light': ('#9a3412', '#7c2d12', '#c2410c', '#ffffff')},
}

# 覆盖块允许声明的令牌全集(accent 族 + splash 四件套,不许碰状态色)。
ALLOWED_TOKENS = {
    '--color-accent', '--color-accent-hover', '--color-accent-strong',
    '--color-accent-muted', '--color-on-accent', '--color-accent-border',
    '--color-splash-grid', '--color-splash-scan', '--color-splash-glow',
    '--color-splash-bar-glow',
}


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


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


def _accent_block(css, name, light):
    """取 `:root[data-accent="x"]` / `...[data-bs-theme="light"]` 块的声明表。"""
    sel = r':root\[data-accent="%s"\]' % name
    if light:
        sel += r'\[data-bs-theme="light"\]'
    m = re.search(sel + r'\s*\{([^}]*)\}', css)
    assert m, f'找不到 {name} 的{"亮" if light else "暗"}色覆盖块'
    return dict(
        (k.strip(), v.strip().lower())
        for k, v in re.findall(r'(--[\w-]+)\s*:\s*([^;]+);', m.group(1))
    )


# ---------------------------------------------------------------- 引导脚本

def test_boot_script_reads_tfAccent_and_writes_data_accent():
    head = re.search(r'<head>.*?</head>', _read(BASE_HTML), re.S).group(0)
    assert 'tf-accent' in head, '引导脚本没有读 localStorage key "tf-accent"(契约 key 名)'
    assert re.search(r"setAttribute\(\s*[\"']data-accent[\"']|dataset\.accent\b", head), (
        '引导脚本没有把 accent 写到 documentElement 的 data-accent 属性'
    )
    for value in ACCENTS:
        assert re.search(r'["\']' + value + r'["\']', head), (
            f'引导脚本里找不到预设值 {value!r} —— 非法值白名单必须含全部五套'
        )


# ---------------------------------------------------------------- theme.js

def test_theme_js_has_accent_api():
    src = _read(THEME_JS)
    assert 'tf-accent' in src, 'theme.js 里没有 localStorage key "tf-accent"'
    for fn in ('getAccent', 'setAccent'):
        assert re.search(r'function\s+' + fn + r'\b', src), f'theme.js 缺 {fn}()'
    assert 'setAttribute' in src and 'removeAttribute' in src, (
        'theme.js 应能写/删 data-accent(sky 缺省要删属性,回到零覆盖)'
    )
    for value in ACCENTS:
        assert re.search(r'["\']' + value + r'["\']', src), (
            f'theme.js 的 ACCENTS 白名单缺 {value!r}'
        )


@pytest.mark.skipif(shutil.which('node') is None, reason='node 不可用')
def test_theme_js_passes_node_syntax_check():
    subprocess.run(['node', '--check', THEME_JS],
                   capture_output=True, text=True, check=True, timeout=120)


# ---------------------------------------------------------------- 配置 UI

def test_config_partial_has_accent_group_with_five_chips():
    from src.i18n.catalog import MESSAGES

    src = _read(CONFIG_PARTIAL)
    assert 'id="accentModeGroup"' in src, '_config_content.html 缺强调色开关组'
    for value in ACCENTS:
        assert re.search(r'data-accent="' + value + r'"', src), (
            f'强调色组缺 data-accent="{value}" 的 chip'
        )
    label_keys = {
        'tpl.config.appearance.accent': '强调色',
        'tpl.config.appearance.accent_sky': '品牌蓝',
        'tpl.config.appearance.accent_teal': '青',
        'tpl.config.appearance.accent_violet': '紫',
        'tpl.config.appearance.accent_rose': '玫红',
        'tpl.config.appearance.accent_orange': '橙',
    }
    for key, zh in label_keys.items():
        assert MESSAGES[key]['zh'] == zh, (
            f'{key} 的中文是 {MESSAGES.get(key, {}).get("zh")!r},期望 {zh!r}'
        )
        assert "t('" + key + "')" in src, f'模板没有引用 {key}'


# ---------------------------------------------------------------- CSS 覆盖块

def test_every_preset_has_both_theme_blocks_with_expected_values():
    css = _read(CSS_PATH)
    for name in OVERRIDDEN:
        for theme in ('dark', 'light'):
            decls = _accent_block(css, name, theme == 'light')
            assert set(decls) == ALLOWED_TOKENS, (
                f'{name}/{theme} 覆盖块的令牌集合 {sorted(decls)} 越界或不全,'
                f'只允许 {sorted(ALLOWED_TOKENS)}'
            )
            accent, hover, strong, on_accent = EXPECTED[name][theme]
            assert decls['--color-accent'] == accent, f'{name}/{theme} accent'
            assert decls['--color-accent-hover'] == hover, f'{name}/{theme} hover'
            assert decls['--color-accent-strong'] == strong, f'{name}/{theme} strong'
            assert decls['--color-on-accent'] == on_accent, f'{name}/{theme} on-accent'


def test_preset_contrast_meets_wcag():
    """填充按钮墨(strong 底 + on-accent 墨)>= 4.5:1,两主题逐套断言。"""
    css = _read(CSS_PATH)
    for name in OVERRIDDEN:
        for theme in ('dark', 'light'):
            decls = _accent_block(css, name, theme == 'light')
            ratio = _contrast(decls['--color-accent-strong'], decls['--color-on-accent'])
            assert ratio >= 4.5, (
                f'{name}/{theme}: 填充按钮 {decls["--color-accent-strong"]} + '
                f'{decls["--color-on-accent"]} 只有 {ratio:.2f}:1'
            )


def test_sky_default_has_no_override_block():
    """sky 是缺省品牌色:有覆盖块就说明默认值被改了 —— 默认渲染必须零变化。"""
    css = _read(CSS_PATH)
    assert '[data-accent="sky"]' not in css, (
        'sky 不该有覆盖块 —— 缺省预设靠 :root 的现有令牌原样生效'
    )
