# TerraForge 前端 UI/UX 改版(借鉴 GeoLibre)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 TerraForge「全屏地图 + 浮动 chrome + 滑出面板」身份的前提下,借鉴 GeoLibre 引入玻璃浮层视觉语言、elevation 阶梯、强调色预设、面板拖拽调宽、命令面板(Ctrl/Cmd+K)与(可选)全窗口拖拽打开本地处理。

**Architecture:** 纯令牌层 + 增量组件改造,无布局重构、无构建步骤、零新依赖。设计依据:`docs/superpowers/specs/2026-08-11-geolibre-inspired-ui-design.md`(数值已全部实算)。

**Tech Stack:** Flask + Jinja SSR、原生 CSS 自定义属性、原生 JS(IIFE 挂 window)、pytest 文本级/渲染级契约测试、node --check 语法校验。

## Global Constraints

- **执行期间不做任何 git 提交**(环境规则):每个任务结束把改动留在工作区,跑全量测试验证;最后由用户统一审阅提交。
- 零 CDN / 离线:不引入任何外部资源、npm 包或构建步骤。
- 测试一律用 `.venv/bin/python -m pytest`(系统 python 没有 pytest);JS 语法校验用 `node --check`。
- 新 i18n 文案走 `src/i18n/catalog/`,zh/en 双语;**键必须以完整引号字面量出现在源码里**(`tests/test_i18n.py` 双向闭合按字面量扫描,不许运行时拼 key)。
- 模板与 JS 注释里不许出现裸中文文案(模板禁裸中文由 test_i18n 钉住);CSS/JS 注释用中文、体例跟同文件现有注释。
- 新增 CSS 过渡/动画要同步 `tests/test_css_contract.py` 的 `_MOTION_BRANCH_COUNT`(当前 39)及其上方登记注释;不新增断点;禁 `transition: all`。
- 新脚本在 `templates/base.html` 登记加载顺序依赖注释;`style.css` 必须保持最后一张样式表。
- 工作区有未提交改动(状态栏图标、favicon、底图代理),**不许回滚或改动**那些路径(`templates/base.html` 状态栏段、`scripts/make_icon.py`、`src/routes/basemap_static.py` 等)。base.html 的脚本区可以追加,不要动状态栏 markup。
- 每个任务结束:`.venv/bin/python -m pytest -q` 全绿才算完成。
- 接口风格:infra 类 JS 文件(theme.js / panels.js / command_palette.js / drop_process.js)用 `var` + IIFE 挂 `window`;config.js 内部用 const/箭头函数(跟该文件现状)。

---

### Task 1: Elevation 阶梯第四档 + 玻璃浮层令牌与玻璃化改造

**Files:**
- Modify: `static/css/style.css`(`:root` ~L19-211、亮色块 ~L238-337、`.statusbar-pill` ~L524、`.map-panel-triggers` ~L741、`.map-overlay-chip` ~L925、`.workbench-panel` ~L1022、`.modal-content` ~L2313、`.statusbar-copy:hover` ~L559)
- Test: `tests/test_elevation_glass.py`(新建)

**Interfaces:**
- Consumes: 现有令牌 `--color-overlay-surface` / `--color-bg-*` / `--color-border*`。
- Produces: `--color-bg-elevated`、`--color-glass-surface`、`--color-glass-border`(暗/亮双主题)。Task 2/4/5 的规则会消费这三个令牌。不改任何已有令牌的值。

**背景知识(实现者必读):**
- `tests/test_css_contract.py` 的 `_palette_var` 取令牌**第一次出现**处的值,`:root` 块必须保持在新覆盖块之前;`_theme_var` 以 `:root[data-bs-theme="light"]` 第一次出现的位置切分暗/亮区。
- 文字层叠模型(`test_every_text_context_meets_wcag_aa`)把 `.statusbar-pill` / `.map-overlay-chip` 的底色合成到 `--color-bg-primary` 上算对比度,半透明 rgba 它处理得了(现 `--color-overlay-surface` 就是 0.92 alpha 的既有先例)。
- 文字模型假设:`color` 声明只出现在顶层规则(不在 @media/@supports 内)、选择器无 `>+~` 组合符。本任务所有新声明只涉及 background/border/backdrop-filter,不碰 color。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_elevation_glass.py`,完整内容:

```python
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
        assert 'var(--color-overlay-surface)' in block, (
            f'{label} 降级块应退回不透明的 --color-overlay-surface'
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
```

- [ ] **Step 2: 跑测试确认红**

Run: `.venv/bin/python -m pytest tests/test_elevation_glass.py -q`
Expected: FAIL(令牌未定义/规则不存在)

- [ ] **Step 3: 实现 —— style.css 令牌与规则**

依次编辑 `static/css/style.css`(用 Edit,每处先 Read 确认):

**3a. `:root` 背景三层后加 elevated 档**(锚点 `--color-bg-tertiary:  #1c2027;` 行之后):

```css
    /* Elevation 第四档(最高):模态 / 滑出面板 / 下拉等「浮在内容之上」的表面。
       借鉴 GeoLibre 的暗色 elevation 阶梯 —— 层级越高越亮。嵌在面板里的控件
       仍是 tertiary,面板本身要比它亮一档,否则控件跟面板糊成一片。 */
    --color-bg-elevated:  #242a33;
```

**3b. `:root` 遮罩与浮层表面段加玻璃令牌**(锚点 `--color-overlay-surface:  rgba(21,23,28,0.92);` 那行之后):

```css
    /* 玻璃浮层令牌(2026-08-11 设计 §3.1,借鉴 GeoLibre 的 map-glass 语言):
       浮在地图上的元素 = 半透明 + backdrop-filter;停靠/覆盖内容层的不透明。
       alpha 0.72 是下限:合成到 --color-bg-primary 后 text-secondary 仍 > 4.5:1
       (tests/test_elevation_glass.py 钉住)。 */
    --color-glass-surface: rgba(21,23,28,0.72);
    --color-glass-border:  rgba(255,255,255,0.14);
```

**3c. 亮色块对应位置加**:在 `:root[data-bs-theme="light"]` 块的 `--color-bg-tertiary:  #f1f3f5;` 后加 `    --color-bg-elevated:  #ffffff;`(亮色下面板/模态本来就是白,令牌只承担语义);在 `--color-overlay-surface:  rgba(255,255,255,0.94);` 后加:

```css
    --color-glass-surface: rgba(255,255,255,0.78);
    --color-glass-border:  rgba(15,23,42,0.14);
```

**3d. 玻璃化三对象**:

`.map-panel-triggers`(现 ~L741)的 `background-color: var(--color-bg-secondary);` 改为 `background-color: var(--color-glass-surface);`,`border: 1px solid var(--color-border);` 改为 `border: 1px solid var(--color-glass-border);`,并在 `overflow: hidden;` 行前加 `backdrop-filter: blur(12px) saturate(140%);`。

`.statusbar-pill`(现 ~L524)的 `background: var(--color-bg-secondary);` → `background: var(--color-glass-surface);`,`border: 1px solid var(--color-border);` → `border: 1px solid var(--color-glass-border);`,`box-shadow: var(--shadow-md);` 行后加 `backdrop-filter: blur(12px) saturate(140%);`。

`.map-overlay-chip`(现 ~L925)的 `background: var(--color-overlay-surface);` → `background: var(--color-glass-surface);`,`border: 1px solid var(--color-border-strong);` → `border: 1px solid var(--color-glass-border);`,`box-shadow: var(--shadow-md);` 行后加 `backdrop-filter: blur(12px) saturate(140%);`。

`.statusbar-copy:hover`(现 ~L559)的 `background: var(--color-bg-tertiary);` → `background: var(--color-control-hover);`(玻璃面上 hover 用半透明叠层,不闪不透明块)。

**3e. 降级块**:紧跟 `.bounds-overlay` 规则(现 ~L982-987)之后插入:

```css
/* 玻璃降级:不支持 backdrop-filter,或用户系统开了「降低透明度」时,
   三处玻璃面退回不透明实底(与改造前一致的观感)。
   选择器名单必须与上面玻璃化三对象逐字一致 —— 同特异度、源码靠后者胜。 */
@supports not (backdrop-filter: blur(1px)) {
    .map-panel-triggers,
    .statusbar-pill,
    .map-overlay-chip {
        background-color: var(--color-overlay-surface);
        border-color: var(--color-border-strong);
        backdrop-filter: none;
    }
}

@media (prefers-reduced-transparency: reduce) {
    .map-panel-triggers,
    .statusbar-pill,
    .map-overlay-chip {
        background-color: var(--color-overlay-surface);
        border-color: var(--color-border-strong);
        backdrop-filter: none;
    }
}
```

**3f. elevated 应用**(三处精确替换,选择器内其余声明不动):
- `.workbench-panel`(现 ~L1022):`background: var(--color-bg-secondary);` → `background: var(--color-bg-elevated);`
- `.modal-content`(现 ~L2313):`background: var(--color-bg-secondary);` → `background: var(--color-bg-elevated);`
- `.modal-header`(现 ~L2321):`background: var(--color-bg-secondary);` → `background: var(--color-bg-elevated);`(header 是模态表面的一部分,与 content 同档;`.modal-footer` 无 background 声明,不动)

注意:设计文档 §3.2 提到的「下拉菜单」**不做** —— 全站 markup 没有任何 `.dropdown-menu` 消费者(grep 零命中),为它写规则是死代码。这是对设计文档的一处有意偏离,已在此登记。

**3g. 阴影语义注释**(只改注释不动数值):`--shadow-sm` 注释改为「控件级:表单控件/小按钮」、`--shadow-md` 改为「浮层级:下拉/地图浮层/胶囊」、`--shadow-lg` 改为「模态级:模态框/滑出面板」。

- [ ] **Step 4: 跑新测试确认绿**

Run: `.venv/bin/python -m pytest tests/test_elevation_glass.py -q`
Expected: 5 passed

- [ ] **Step 5: 契约回归 + 全量**

Run: `.venv/bin/python -m pytest tests/test_css_contract.py tests/test_i18n.py -q`
Expected: 全绿。若 `test_every_text_context_meets_wcag_aa` 红:看失败信息里哪条上下文、实测比值 —— 玻璃令牌 alpha 就是下限来源,把 `--color-glass-surface` 的 alpha 从 0.72 往上抬(每次 +0.04)直到通过,并同步改 `test_glass_text_contrast_holds_in_both_themes` 的注释说明。

Run: `.venv/bin/python -m pytest -q`
Expected: 全绿

---

### Task 2: 强调色预设(模式 × accent)

**Files:**
- Modify: `templates/base.html`(`<head>` 引导脚本 ~L34-43)
- Modify: `static/js/theme.js`(全文 97 行,扩展 accent 机制)
- Modify: `static/css/style.css`(亮色块之后追加 4 预设 × 2 主题覆盖块)
- Modify: `templates/_config_content.html`(「外观」区 ~L41-72,加 accent 组)
- Modify: `static/js/config.js`(`initThemeSwitcher` ~L350 后加 `initAccentSwitcher` + 调用点)
- Modify: `src/i18n/catalog/tpl_config.py`(appearance 段加 7 条键)
- Test: `tests/test_accent_switch.py`(新建)

**Interfaces:**
- Consumes: `window.TerraTheme`(theme.js)、`.status-chip` 组件样式、`hint` 宏、`t()`。
- Produces: `TerraTheme.getAccent()` / `TerraTheme.setAccent(accent)` / `TerraTheme.ACCENTS`;`localStorage tf-accent`;`<html data-accent="teal|violet|rose|orange">`(sky 缺省不落属性)。Task 4 的命令面板消费 `TerraTheme.setAccent` 不消费主题命令外的接口。

**背景知识:**
- 数值已按 Tailwind 档位实算 WCAG(设计文档 §3.5 表格),照抄即可,不要另调。
- sky 是缺省品牌色:不落 `data-accent` 属性、不写覆盖块,现状逐字不变 —— 这是「默认渲染零变化」的保证。
- 覆盖块只允许自定义属性声明(与亮色块同一规矩,层叠模型才能继续判定 `:root` 系选择器不命中组件元素);只许覆盖 accent 族令牌,不许碰状态色。
- base.html 引导脚本段的注释里不能出现 html 尖括号写法(tests 用 HTMLParser 扫模板源码)。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_accent_switch.py`,完整内容:

```python
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
```

- [ ] **Step 2: 跑测试确认红**

Run: `.venv/bin/python -m pytest tests/test_accent_switch.py -q`
Expected: FAIL(tf-accent / 覆盖块尚不存在)

- [ ] **Step 3: 实现**

**3a. `templates/base.html` 引导脚本**:把整个 IIFE 替换为(保持原有主题逻辑逐字不变,只追加 accent 段;⚠️ 中文说明**必须写进 script 上方的 Jinja 注释块** —— script 体内的中文 `//` 注释会撞 test_i18n 的裸中文扫描,它只剥 Jinja/HTML 注释):

```html
    {# 强调色预设(2026-08-11 设计 §3.5):与主题同一套机制,随引导脚本首帧前同步。
       sky 是缺省品牌色 —— 不落 data-accent 属性,:root 现有令牌原样生效,
       默认渲染零变化;只有非缺省预设才写属性,覆盖块见 style.css 亮色令牌块之后。
       (本注释块可以出现中文;下面 script 体内不许有中文注释 —— 裸中文扫描不剥 JS 注释。) #}
    <script>
        (function () {
            var mode = 'dark';
            try { mode = localStorage.getItem('tf-theme') || 'dark'; } catch (e) {}
            if (mode !== 'dark' && mode !== 'light' && mode !== 'system') mode = 'dark';
            document.documentElement.dataset.bsTheme = (mode === 'system')
                ? (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
                : mode;
            var accent = 'sky';
            try { accent = localStorage.getItem('tf-accent') || 'sky'; } catch (e) {}
            if (['sky', 'teal', 'violet', 'rose', 'orange'].indexOf(accent) === -1) accent = 'sky';
            if (accent !== 'sky') document.documentElement.setAttribute('data-accent', accent);
        })();
    </script>
```

**3b. `static/js/theme.js`**:在 `var LIGHT_QUERY = ...` 行后加:

```js
    var ACCENT_KEY = 'tf-accent';
    var ACCENTS = ['sky', 'teal', 'violet', 'rose', 'orange'];
```

在 `set` 函数后加:

```js
    // ---- 强调色预设(2026-08-11 设计 §3.5)---------------------------------
    // 与主题同一套机制:localStorage `tf-accent` + <html> data-accent。
    // sky 是缺省 —— 删属性而不是写 "sky",保持「缺省 = 零覆盖」。

    // 当前强调色偏好。非法值/读不到一律回退 sky。
    function getAccent() {
        var accent = 'sky';
        try {
            accent = window.localStorage.getItem(ACCENT_KEY) || 'sky';
        } catch (e) { /* 隐私模式等 localStorage 不可用时按缺省 sky */ }
        return ACCENTS.indexOf(accent) !== -1 ? accent : 'sky';
    }

    function applyAccentToDom() {
        var accent = getAccent();
        var el = document.documentElement;
        var before = el.getAttribute('data-accent') || 'sky';
        if (before === accent) return;
        if (accent === 'sky') el.removeAttribute('data-accent');
        else el.setAttribute('data-accent', accent);
        // 与主题切换广播同一个事件:history.js 的 _statusStrokeCache 这类
        // 「缓存了 CSS 变量求值结果」的模块无需区分是哪种换肤。
        try {
            document.dispatchEvent(new CustomEvent('terraforge:themechange', {
                detail: { theme: resolved(), accent: accent }
            }));
        } catch (e) { /* 老浏览器没有 CustomEvent 构造器,忽略 */ }
    }

    // 写强调色偏好并立即应用。非法值直接忽略。
    function setAccent(accent) {
        if (ACCENTS.indexOf(accent) === -1) return;
        try {
            window.localStorage.setItem(ACCENT_KEY, accent);
        } catch (e) { /* 写不进也先把本次会话的应用上 */ }
        applyAccentToDom();
    }
```

`init()` 改为:

```js
    function init() {
        apply();
        applyAccentToDom();
    }
```

返回对象改为:

```js
    return { get: get, set: set, resolved: resolved, init: init,
             getAccent: getAccent, setAccent: setAccent, ACCENTS: ACCENTS };
```

**3c. `static/css/style.css`**:在 `:root[data-bs-theme="light"]` 块的**结束之后**追加(四预设 × 双主题,数值取自设计文档 §3.5,已实算 WCAG):

```css
/* ==========================================================================
   强调色预设(2026-08-11 设计 §3.5,借鉴 GeoLibre 的「模式 × 强调色」)

   机制:localStorage `tf-accent` + <html data-accent>,由 base.html 引导脚本
   首帧前同步,运行期 theme.js 的 TerraTheme.setAccent 切换。sky 是缺省品牌色,
   不落属性、无覆盖块 —— 本区只覆盖其余四套。

   规矩与亮色令牌块相同:只许自定义属性声明,只许覆盖 accent 族令牌
   (accent/hover/strong/muted、on-accent、accent-border、splash 四件套),
   不许碰状态色 —— 色相刻意避开 success=emerald / warning=amber / danger=red /
   info=blue,徽章与按钮一眼可分。档位沿用现有规则:暗色 400 基 -> 300 hover
   (提亮)、on-accent 近黑墨;亮色 800 基 -> 900 hover(压暗)、填充 700 白墨。
   全部对比度由 tests/test_accent_switch.py 逐套钉住。
   ========================================================================== */
:root[data-accent="teal"] {
    --color-accent: #2dd4bf;
    --color-accent-hover: #5eead4;
    --color-accent-strong: #14b8a6;
    --color-accent-muted: rgba(45,212,191,0.12);
    --color-on-accent: #020617;
    --color-accent-border: rgba(45,212,191,0.25);
    --color-splash-grid: rgba(45,212,191,0.06);
    --color-splash-scan: rgba(45,212,191,0.09);
    --color-splash-glow: rgba(45,212,191,0.45);
    --color-splash-bar-glow: rgba(45,212,191,0.5);
}

:root[data-accent="teal"][data-bs-theme="light"] {
    --color-accent: #115e59;
    --color-accent-hover: #134e4a;
    --color-accent-strong: #0f766e;
    --color-accent-muted: rgba(15,118,110,0.10);
    --color-on-accent: #ffffff;
    --color-accent-border: rgba(15,118,110,0.35);
    --color-splash-grid: rgba(15,118,110,0.07);
    --color-splash-scan: rgba(15,118,110,0.10);
    --color-splash-glow: rgba(15,118,110,0.35);
    --color-splash-bar-glow: rgba(15,118,110,0.40);
}

:root[data-accent="violet"] {
    --color-accent: #a78bfa;
    --color-accent-hover: #c4b5fd;
    --color-accent-strong: #8b5cf6;
    --color-accent-muted: rgba(167,139,250,0.12);
    --color-on-accent: #020617;
    --color-accent-border: rgba(167,139,250,0.25);
    --color-splash-grid: rgba(167,139,250,0.06);
    --color-splash-scan: rgba(167,139,250,0.09);
    --color-splash-glow: rgba(167,139,250,0.45);
    --color-splash-bar-glow: rgba(167,139,250,0.5);
}

:root[data-accent="violet"][data-bs-theme="light"] {
    --color-accent: #5b21b6;
    --color-accent-hover: #4c1d95;
    --color-accent-strong: #6d28d9;
    --color-accent-muted: rgba(109,40,217,0.10);
    --color-on-accent: #ffffff;
    --color-accent-border: rgba(109,40,217,0.35);
    --color-splash-grid: rgba(109,40,217,0.07);
    --color-splash-scan: rgba(109,40,217,0.10);
    --color-splash-glow: rgba(109,40,217,0.35);
    --color-splash-bar-glow: rgba(109,40,217,0.40);
}

:root[data-accent="rose"] {
    --color-accent: #fb7185;
    --color-accent-hover: #fda4af;
    --color-accent-strong: #f43f5e;
    --color-accent-muted: rgba(251,113,133,0.12);
    --color-on-accent: #020617;
    --color-accent-border: rgba(251,113,133,0.25);
    --color-splash-grid: rgba(251,113,133,0.06);
    --color-splash-scan: rgba(251,113,133,0.09);
    --color-splash-glow: rgba(251,113,133,0.45);
    --color-splash-bar-glow: rgba(251,113,133,0.5);
}

:root[data-accent="rose"][data-bs-theme="light"] {
    --color-accent: #9f1239;
    --color-accent-hover: #881337;
    --color-accent-strong: #be123c;
    --color-accent-muted: rgba(190,18,60,0.10);
    --color-on-accent: #ffffff;
    --color-accent-border: rgba(190,18,60,0.35);
    --color-splash-grid: rgba(190,18,60,0.07);
    --color-splash-scan: rgba(190,18,60,0.10);
    --color-splash-glow: rgba(190,18,60,0.35);
    --color-splash-bar-glow: rgba(190,18,60,0.40);
}

:root[data-accent="orange"] {
    --color-accent: #fb923c;
    --color-accent-hover: #fdba74;
    --color-accent-strong: #f97316;
    --color-accent-muted: rgba(251,146,60,0.12);
    --color-on-accent: #020617;
    --color-accent-border: rgba(251,146,60,0.25);
    --color-splash-grid: rgba(251,146,60,0.06);
    --color-splash-scan: rgba(251,146,60,0.09);
    --color-splash-glow: rgba(251,146,60,0.45);
    --color-splash-bar-glow: rgba(251,146,60,0.5);
}

:root[data-accent="orange"][data-bs-theme="light"] {
    --color-accent: #9a3412;
    --color-accent-hover: #7c2d12;
    --color-accent-strong: #c2410c;
    --color-accent-muted: rgba(194,65,12,0.10);
    --color-on-accent: #ffffff;
    --color-accent-border: rgba(194,65,12,0.35);
    --color-splash-grid: rgba(194,65,12,0.07);
    --color-splash-scan: rgba(194,65,12,0.10);
    --color-splash-glow: rgba(194,65,12,0.35);
    --color-splash-bar-glow: rgba(194,65,12,0.40);
}
```

**3d. `templates/_config_content.html`**:在主题开关的 `.mb-3` 块(L41-54)之后、语言开关块之前插入:

```html
                    {# 强调色预设:与主题开关同一套 .status-chips 风格。sky 是缺省
                       品牌色;偏好存 localStorage `tf-accent`,不进服务端配置表。
                       aria-pressed 初值同主题组:一律 false,由 config.js 的
                       initAccentSwitcher 挂载时摆正。 #}
                    <div class="mb-3">
                        <div class="label-row">
                            <label class="form-label" id="accentModeLabel">{{ t('tpl.config.appearance.accent') }}</label>
                            {{ hint(t('tpl.config.appearance.accent_hint')) }}
                        </div>
                        <div class="status-chips" id="accentModeGroup" role="group" aria-labelledby="accentModeLabel">
                            <button type="button" class="status-chip" data-accent="sky" aria-pressed="false">{{ t('tpl.config.appearance.accent_sky') }}</button>
                            <button type="button" class="status-chip" data-accent="teal" aria-pressed="false">{{ t('tpl.config.appearance.accent_teal') }}</button>
                            <button type="button" class="status-chip" data-accent="violet" aria-pressed="false">{{ t('tpl.config.appearance.accent_violet') }}</button>
                            <button type="button" class="status-chip" data-accent="rose" aria-pressed="false">{{ t('tpl.config.appearance.accent_rose') }}</button>
                            <button type="button" class="status-chip" data-accent="orange" aria-pressed="false">{{ t('tpl.config.appearance.accent_orange') }}</button>
                        </div>
                    </div>
```

**3e. `src/i18n/catalog/tpl_config.py`**:在 appearance 段的 `tpl.config.appearance.theme_system` 条目之后追加(照该文件现有的多行 dict 体例):

```python
    'tpl.config.appearance.accent': {
        'zh': '强调色',
        'en': 'Accent',
    },
    'tpl.config.appearance.accent_hint': {
        'zh': '界面强调色预设,默认为品牌蓝;即时生效,偏好只保存在本机。',
        'en': 'Accent color preset. Defaults to brand sky blue; applies instantly and is stored locally only.',
    },
    'tpl.config.appearance.accent_sky': {
        'zh': '品牌蓝',
        'en': 'Sky',
    },
    'tpl.config.appearance.accent_teal': {
        'zh': '青',
        'en': 'Teal',
    },
    'tpl.config.appearance.accent_violet': {
        'zh': '紫',
        'en': 'Violet',
    },
    'tpl.config.appearance.accent_rose': {
        'zh': '玫红',
        'en': 'Rose',
    },
    'tpl.config.appearance.accent_orange': {
        'zh': '橙',
        'en': 'Orange',
    },
```

**3f. `static/js/config.js`**:在 `initThemeSwitcher` 函数之后加(与该函数同体例):

```js
function initAccentSwitcher() {
    const group = document.getElementById('accentModeGroup');
    if (!group || !window.TerraTheme || !TerraTheme.getAccent) return;
    const chips = [...group.querySelectorAll('[data-accent]')];

    function refresh() {
        const accent = TerraTheme.getAccent();
        chips.forEach(chip => {
            const on = chip.dataset.accent === accent;
            chip.classList.toggle('active', on);
            // aria-pressed 与 .active 必须同步翻(与主题组同一写法)。
            chip.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
    }

    group.addEventListener('click', function (e) {
        const chip = e.target.closest('[data-accent]');
        if (!chip) return;
        TerraTheme.setAccent(chip.dataset.accent);
        refresh();
    });

    refresh();
}
```

在 `static/js/config.js` L10(文件顶部初始化序列)的 `initThemeSwitcher();` 之后加一行 `initAccentSwitcher();`。

- [ ] **Step 4: 跑新测试确认绿**

Run: `.venv/bin/python -m pytest tests/test_accent_switch.py -q`
Expected: 7 passed(node 不可用时 6 passed + 1 skipped)

- [ ] **Step 5: 契约回归 + 全量**

Run: `.venv/bin/python -m pytest tests/test_css_contract.py tests/test_i18n.py tests/test_theme_switch.py -q`
Expected: 全绿
Run: `.venv/bin/python -m pytest -q`
Expected: 全绿

---

### Task 3: 任务/配置面板拖拽调宽

**Files:**
- Modify: `static/css/style.css`(`:root` 加两个宽度令牌、`.workbench-panel` ~L1022 与 `.workbench-panel--wide` ~L1039 改消费、新增 resizer 规则)
- Modify: `templates/index.html`(两个面板 ~L449-468,各加一个 resizer 子元素)
- Modify: `static/js/panels.js`(DOMContentLoaded 回调里挂 `initResizers()`)
- Test: `tests/test_panel_resize.py`(新建)

**Interfaces:**
- Consumes: `.workbench-panel` / `.workbench-panel--wide` 现状宽 480/920px;panels.js 的 DOMContentLoaded 初始化段。
- Produces: `:root` 令牌 `--panel-tasks-w: 920px` / `--panel-config-w: 480px`;localStorage `tf-panel-w-tasks` / `tf-panel-w-config`(像素数字符串);元素 `[data-panel-resizer]`。不改动 panels.js 的 openPanel/closePanel/focus 语义。

**背景知识:**
- 拖拽中只写 CSS 变量(rAF 节流),松手才写 localStorage —— 借鉴 GeoLibre,避免拖拽期反复 layout 之外的副作用。
- resizer 是**不可聚焦**的 div(纯鼠标交互):面板的焦点环(panels.js FOCUSABLE)不含无 tabindex 的 div,键盘语义零变化。
- `test_records_panel_structure.py` 钉了面板 DOM 结构,resizer 是新增子元素;若该测试因此变红,把 resizer 登记进它的结构模型(优先改测试模型,不要为测试删功能)。
- 不加任何 transition(resizer hover 直接换色),`_MOTION_BRANCH_COUNT` 不动。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_panel_resize.py`,完整内容:

```python
"""面板拖拽调宽的契约测试(2026-08-11 UI 改版,设计 §3.4)。

任务面板(默认 920px)与配置面板(默认 480px)的左缘 8px 热区可拖拽调宽;
拖拽中只写 CSS 变量,松手写 localStorage;窄屏(<768px,面板已全屏覆盖)禁用。
"""

import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS_PATH = os.path.join(ROOT, 'static', 'css', 'style.css')
INDEX_HTML = os.path.join(ROOT, 'templates', 'index.html')
PANELS_JS = os.path.join(ROOT, 'static', 'js', 'panels.js')

WIDTH_VARS = {'--panel-tasks-w': '920px', '--panel-config-w': '480px'}
STORAGE_KEYS = ('tf-panel-w-tasks', 'tf-panel-w-config')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _rule_body(css, selector):
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    for m in re.finditer(r'([^{}@]+)\{([^{}]*)\}', css):
        if m.group(1).strip() == selector:
            return m.group(2)
    return None


def test_width_tokens_defined_and_consumed():
    css = _read(CSS_PATH)
    for name, default in WIDTH_VARS.items():
        m = re.search(re.escape(name) + r'\s*:\s*([^;]+);', css)
        assert m, f':root 缺宽度令牌 {name}'
        assert m.group(1).strip() == default, (
            f'{name} 默认值是 {m.group(1).strip()!r},期望 {default!r}(现状宽度)'
        )
    panel = _rule_body(css, '.workbench-panel')
    assert panel and 'width: var(--panel-config-w)' in panel, (
        '.workbench-panel 的宽度应消费 var(--panel-config-w)'
    )
    wide = _rule_body(css, '.workbench-panel--wide')
    assert wide and 'width: var(--panel-tasks-w)' in wide, (
        '.workbench-panel--wide 的宽度应消费 var(--panel-tasks-w)'
    )


def test_resizer_css_exists():
    css = _read(CSS_PATH)
    body = _rule_body(css, '.workbench-panel__resizer')
    assert body, '缺 .workbench-panel__resizer 规则'
    assert 'col-resize' in body, 'resizer 必须是 col-resize 光标'
    assert 'position: absolute' in body, 'resizer 应绝对定位在面板左缘'


def test_resizer_markup_present_in_both_panels():
    src = _read(INDEX_HTML)
    assert src.count('data-panel-resizer') == 2, (
        'index.html 的两个面板里应各有一个 data-panel-resizer 元素'
    )


def test_panels_js_resize_wiring():
    src = _read(PANELS_JS)
    for key in STORAGE_KEYS:
        assert key in src, f'panels.js 缺 localStorage key {key!r}'
    for token in WIDTH_VARS:
        assert token in src, f'panels.js 没有写 CSS 变量 {token}'
    assert 'setPointerCapture' in src, '拖拽应用 pointer capture(防拖出窗口丢事件)'
    assert 'requestAnimationFrame' in src, '拖拽中宽度写入应走 rAF 节流'
    assert 'matchMedia' in src and '768' in src, '窄屏(<768px)应禁用拖拽'


@pytest.mark.skipif(shutil.which('node') is None, reason='node 不可用')
def test_panels_js_passes_node_syntax_check():
    subprocess.run(['node', '--check', PANELS_JS],
                   capture_output=True, text=True, check=True, timeout=120)
```

- [ ] **Step 2: 跑测试确认红**

Run: `.venv/bin/python -m pytest tests/test_panel_resize.py -q`
Expected: FAIL(令牌/resizer 尚不存在)

- [ ] **Step 3: 实现**

**3a. `static/css/style.css` `:root`**:在状态栏尺寸令牌(`--statusbar-clearance` 行)之后加:

```css
    /* 面板宽度(2026-08-11 设计 §3.4):panels.js 拖拽调宽的默认值与样式入口。
       拖拽中 JS 只改这两个变量(元素级 setProperty,继承覆盖这里的缺省),
       松手才写 localStorage;`max-width: 94vw` 仍是最终上限。 */
    --panel-tasks-w: 920px;
    --panel-config-w: 480px;
```

**3b. 宽度规则改消费变量**:`.workbench-panel` 的 `width: 480px;` → `width: var(--panel-config-w);`;`.workbench-panel--wide` 的 `width: 920px;` → `width: var(--panel-tasks-w);`。

**3c. resizer 规则**:在 `.workbench-panel[hidden]` 规则之后加:

```css
/* 面板左缘的 8px 调宽热区(panels.js)。不可聚焦、不加 transition:
   焦点环不含它,动画索引不动。左缘探出 4px,好抓。 */
.workbench-panel__resizer {
    position: absolute;
    left: -4px;
    top: 0;
    bottom: 0;
    width: 8px;
    cursor: col-resize;
    z-index: 2;
}

.workbench-panel__resizer:hover,
.workbench-panel__resizer--active {
    background: var(--color-accent-muted);
    box-shadow: inset 1px 0 0 var(--color-accent);
}
```

**3d. `templates/index.html`**:两个面板各加一个 resizer 子元素(放在 `</section>` 前的最后一个子元素位置):

`#historyPanel` 面板(`{% include "_history_content.html" %}` 所在的 `.workbench-panel__body` div 之后):

```html
    <div class="workbench-panel__resizer" data-panel-resizer aria-hidden="true"></div>
```

`#configPanel` 面板同样位置加同一行。

**3e. `static/js/panels.js`**:在 IIFE 内、`document.addEventListener('DOMContentLoaded', ...)` 之前加:

```js
    // ---- 面板调宽(2026-08-11 设计 §3.4,借鉴 GeoLibre)--------------------
    // 左缘 8px 热区;拖拽中只写 CSS 变量(rAF 节流),松手写 localStorage。
    // 窄屏(<768px)面板是全屏覆盖,调宽无意义,整个不启用。
    var RESIZE_CONFIGS = [
        { id: 'historyPanel', varName: '--panel-tasks-w', key: 'tf-panel-w-tasks', min: 560, max: 1100 },
        { id: 'configPanel', varName: '--panel-config-w', key: 'tf-panel-w-config', min: 320, max: 640 }
    ];

    function clampWidth(v, min, max) { return Math.min(max, Math.max(min, v)); }

    function applyPanelWidth(cfg, px) {
        var el = document.getElementById(cfg.id);
        if (el) el.style.setProperty(cfg.varName, px + 'px');
    }

    function initResizers() {
        if (!window.matchMedia('(min-width: 768px)').matches) return;
        RESIZE_CONFIGS.forEach(function (cfg) {
            var el = document.getElementById(cfg.id);
            var handle = el && el.querySelector('[data-panel-resizer]');
            if (!handle) return;
            var stored = NaN;
            try { stored = parseFloat(window.localStorage.getItem(cfg.key)); } catch (e) {}
            if (!isNaN(stored)) applyPanelWidth(cfg, clampWidth(stored, cfg.min, cfg.max));

            handle.addEventListener('pointerdown', function (e) {
                e.preventDefault();
                handle.setPointerCapture(e.pointerId);
                handle.classList.add('workbench-panel__resizer--active');
                var startX = e.clientX;
                var startW = el.getBoundingClientRect().width;
                var raf = 0;
                function widthAt(clientX) {
                    // 面板钉在视口右缘:指针往左 = 变宽。
                    return clampWidth(startW + (startX - clientX), cfg.min, cfg.max);
                }
                function onMove(ev) {
                    if (raf) return;
                    raf = requestAnimationFrame(function () {
                        raf = 0;
                        applyPanelWidth(cfg, widthAt(ev.clientX));
                    });
                }
                function onUp(ev) {
                    handle.removeEventListener('pointermove', onMove);
                    handle.removeEventListener('pointerup', onUp);
                    handle.removeEventListener('pointercancel', onUp);
                    if (raf) { cancelAnimationFrame(raf); raf = 0; }
                    handle.classList.remove('workbench-panel__resizer--active');
                    var w = widthAt(ev.clientX);
                    applyPanelWidth(cfg, w);
                    try { window.localStorage.setItem(cfg.key, String(w)); } catch (e2) {}
                }
                handle.addEventListener('pointermove', onMove);
                handle.addEventListener('pointerup', onUp);
                handle.addEventListener('pointercancel', onUp);
            });
        });
    }
```

在 DOMContentLoaded 回调的末尾(`if (PANELS[h]) openPanel(h, false);` 之后)加一行 `initResizers();`。

- [ ] **Step 4: 跑新测试确认绿**

Run: `.venv/bin/python -m pytest tests/test_panel_resize.py -q`
Expected: 5 passed(node 不可用时 4 + 1 skipped)

- [ ] **Step 5: 契约回归 + 全量**

Run: `.venv/bin/python -m pytest tests/test_css_contract.py tests/test_records_panel_structure.py -q`
Expected: 全绿;若结构测试因新增 resizer 子元素变红,把 resizer 登记进该测试的结构模型(改测试,不删 resizer)。
Run: `.venv/bin/python -m pytest -q`
Expected: 全绿

---

### Task 4: 命令面板(Ctrl/Cmd+K)+ 快捷键速查(`?`)

**Files:**
- Create: `templates/_command_palette.html`
- Create: `static/js/command_palette.js`
- Create: `src/i18n/catalog/js_commands.py`
- Modify: `src/i18n/catalog/__init__.py`(注册新域)
- Modify: `src/i18n/catalog/tpl_base.py`(加 cmdk 外壳键)
- Modify: `templates/base.html`(include 新 partial + 加载新脚本,均带依赖注释)
- Modify: `static/css/style.css`(新增 `.cmdk` 一节)
- Test: `tests/test_command_palette.py`(新建)

**Interfaces:**
- Consumes: `window.t()`(i18n.js)、`window.TerraTheme`(Task 2 之后含 setAccent,本任务只用 set)、`window.openPanel`/`window.closePanel`(panels.js)、`window.openDownloadModal`(map.js 顶层函数)、现有按钮元素 `#mapDrawRect` / `#boundsClearBtn` / `#boundsDownloadBtn` / `#processOpenBtn` / `#statusCoords` / `#historyPanel` / `#configPanel`、`window.__LANG__`。
- Produces: `window.TerraCommands`(对象 `{ open, close, openHelp, closeHelp, isOpen }`,供将来状态栏/帮助入口复用);localStorage/cookie 只写 `tf-lang`(语言切换)。

**背景知识:**
- 文案键**必须**以完整字面量出现在源码(`t('js.cmdk.start_bounds')` 这种形态),test_i18n 双向闭合按字面量扫描 —— 注册表里用 `titleKey: 'js.cmdk.xxx'` 完整字面量,渲染时 `t(cmd.titleKey)`,**不许** `t('js.cmdk.' + id)` 拼接。
- Esc 层级:ui.js 的 confirm 用 capture + stopImmediatePropagation;panels.js 用 bubble + `body.modal-open` 判据。本组件:全局键走 window bubble(尊重 `defaultPrevented`、豁免输入控件、confirm/modal 在场不抢),自己的 Esc 走 document **capture** + `stopPropagation()` —— 面板开着时 Esc 只关面板,背后的工作台面板/Bootstrap 弹窗(bubble 监听)收不到。
- z-index 层级现状:面板 1401 < modal-backdrop 1450 < modal 1500 < toast 11000 < confirm 12000。命令面板取 **13100**(confirm 在场时全局键让位,两者不会同时出现;13000 预留给 Task 5 的拖拽遮罩)。
- 不加任何 transition(即开即关),`_MOTION_BRANCH_COUNT` 不动。
- base.html 的脚本区注释即依赖图:新脚本排在 i18n.js / ui.js / theme.js 之后(命令要用 t() 与 TerraTheme),并写明这条理由。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_command_palette.py`,完整内容:

```python
"""命令面板(Ctrl/Cmd+K)与快捷键速查(?)的契约测试(2026-08-11 设计 §3.3)。

- 单一命令注册表驱动面板列表与速查表;键字面量完整出现在源码(i18n 双向闭合)。
- 全局键:Ctrl/Cmd+K 开关面板、`?` 开速查;输入控件与 defaultPrevented 豁免;
  confirm / Bootstrap modal 在场时不抢。
- Esc 走 capture 阶段:只关最上层,不穿透到工作台面板 / 弹窗。
"""

import importlib
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_HTML = os.path.join(ROOT, 'templates', 'base.html')
PARTIAL = os.path.join(ROOT, 'templates', '_command_palette.html')
CMD_JS = os.path.join(ROOT, 'static', 'js', 'command_palette.js')
CSS_PATH = os.path.join(ROOT, 'static', 'css', 'style.css')

# 注册表全量命令 id —— 每个 id 都必须有对应的 i18n 键字面量。
COMMAND_IDS = (
    'open_palette', 'show_help', 'esc_close',
    'start_bounds', 'clear_bounds', 'new_download',
    'open_tasks', 'open_config', 'open_process', 'copy_coords',
    'goto_history', 'goto_config', 'theme_dark', 'theme_light', 'lang_switch',
)


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _load_app(monkeypatch, tmp_path):
    """与 tests/test_theme_switch.py 同一个套路。"""
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


# ---------------------------------------------------------------- 结构

def test_partial_has_palette_and_help_markup():
    src = _read(PARTIAL)
    for attr in ('id="cmdk"', 'id="cmdkInput"', 'id="cmdkList"',
                 'id="cmdkHelp"', 'id="cmdkHelpList"'):
        assert attr in src, f'_command_palette.html 缺 {attr}'
    assert 'role="combobox"' in src, '输入框缺 combobox 角色'
    assert 'role="listbox"' in src, '列表缺 listbox 角色'
    assert 'hidden' in src, '面板默认必须隐藏'


def test_base_html_includes_partial_and_loads_script():
    src = _read(BASE_HTML)
    assert '{% include "_command_palette.html" %}' in src, (
        'base.html 没有 include _command_palette.html'
    )
    assert re.search(r'<script[^>]+src=[^>]*js/command_palette\.js', src), (
        'base.html 没有加载 js/command_palette.js'
    )
    # 必须在 i18n.js 之后(命令面板解析期/运行期都调 t())
    assert src.index('js/i18n.js') < src.index('js/command_palette.js'), (
        'command_palette.js 必须排在 i18n.js 之后'
    )


def test_index_page_renders_palette(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    html = client.get('/').get_data(as_text=True)
    assert 'id="cmdk"' in html, '首页渲染结果里没有命令面板'


# ---------------------------------------------------------------- JS 行为

def test_every_command_id_has_literal_i18n_key():
    src = _read(CMD_JS)
    for cid in COMMAND_IDS:
        key = f"'js.cmdk.{cid}'"
        assert key in src, (
            f'注册表缺 {key} —— 键必须以完整字面量出现(i18n 双向闭合按字面量扫)'
        )


def test_keyboard_layering_rules():
    src = _read(CMD_JS)
    assert 'defaultPrevented' in src, '全局键必须尊重 defaultPrevented'
    assert 'isContentEditable' in src, '必须豁免 contenteditable'
    for tag in ("'INPUT'", "'TEXTAREA'", "'SELECT'"):
        assert tag in src, f'必须豁免 {tag}'
    assert "ctrlKey" in src and "metaKey" in src, 'Ctrl/Cmd+K 两个修饰键都要认'
    assert re.search(r"addEventListener\(\s*['\"]keydown['\"][\s\S]*?true\s*\)", src), (
        'Esc 必须走 capture 阶段(第三个参数 true),只关最上层'
    )
    assert 'stopPropagation' in src, 'capture 段关掉面板后要 stopPropagation'
    assert 'app-confirm-overlay' in src, 'confirm 在场时必须让位'
    assert 'modal-open' in src, 'Bootstrap 弹窗在场时必须让位'


@pytest.mark.skipif(shutil.which('node') is None, reason='node 不可用')
def test_command_palette_js_passes_node_syntax_check():
    subprocess.run(['node', '--check', CMD_JS],
                   capture_output=True, text=True, check=True, timeout=120)


# ---------------------------------------------------------------- 样式

def test_palette_is_an_opaque_docked_layer():
    css = re.sub(r'/\*.*?\*/', '', _read(CSS_PATH), flags=re.S)
    m = re.search(r'\.cmdk__dialog\s*\{([^}]*)\}', css)
    assert m, '缺 .cmdk__dialog 规则'
    body = m.group(1)
    assert 'var(--color-bg-elevated)' in body, '面板是不透明的停靠层,用 elevated 底'
    assert 'var(--shadow-lg)' in body, '面板是模态级阴影'
    assert 'backdrop-filter' not in body, '停靠层不做玻璃(玻璃只给浮在地图上的元素)'
    m = re.search(r'\.cmdk\s*\{([^}]*)\}', css)
    assert m and 'z-index: 13100' in m.group(1), '.cmdk 的 z-index 应为 13100'
```

- [ ] **Step 2: 跑测试确认红**

Run: `.venv/bin/python -m pytest tests/test_command_palette.py -q`
Expected: FAIL(partial / js / css 尚不存在)

- [ ] **Step 3: 实现**

**3a. `templates/_command_palette.html`**(新建):

```html
{# 命令面板(Ctrl/Cmd+K)与快捷键速查(?):外壳静态渲染,列表由
   command_palette.js 按注册表动态填充。放在 <body> 直下、.workbench 之外 ——
   与任务详情模态框同一个理由(面板恒带 transform,会劫持 fixed 后代的
   包含块与层叠上下文)。 #}
<div class="cmdk" id="cmdk" hidden>
    <div class="cmdk__backdrop" data-cmdk-close></div>
    <div class="cmdk__dialog" role="dialog" aria-modal="true" aria-label="{{ t('tpl.base.cmdk.title') }}">
        <input type="text" class="cmdk__input" id="cmdkInput"
               role="combobox" aria-expanded="true" aria-controls="cmdkList" aria-autocomplete="list"
               placeholder="{{ t('tpl.base.cmdk.placeholder') }}">
        <ul class="cmdk__list" id="cmdkList" role="listbox" aria-label="{{ t('tpl.base.cmdk.title') }}"></ul>
    </div>
</div>

<div class="cmdk" id="cmdkHelp" hidden>
    <div class="cmdk__backdrop" data-cmdk-help-close></div>
    <div class="cmdk__dialog cmdk__dialog--help" role="dialog" aria-modal="true" aria-label="{{ t('tpl.base.cmdk.help_title') }}">
        <div class="cmdk__help-head">
            <span>{{ t('tpl.base.cmdk.help_title') }}</span>
            <button type="button" class="cmdk__help-close" data-cmdk-help-close
                    aria-label="{{ t('tpl.base.cmdk.help_close') }}">&times;</button>
        </div>
        <ul class="cmdk__help-list" id="cmdkHelpList"></ul>
    </div>
</div>
```

**3b. `static/js/command_palette.js`**(新建,完整内容):

```js
/**
 * 命令面板(Ctrl/Cmd+K)+ 快捷键速查(`?`)。借鉴 GeoLibre:
 * 单一命令注册表同时驱动面板列表、全局快捷键与速查表。
 *
 * 契约(改之前先读):
 * - 零依赖:IIFE 挂 window.TerraCommands;文案一律 t('js.cmdk.*'),
 *   键以**完整字面量**写在注册表的 titleKey 里 —— tests/test_i18n.py 的
 *   双向闭合按字面量扫描,运行时拼 key 会养出假孤儿。
 * - 全局键走 window bubble:尊重 e.defaultPrevented;input/textarea/select/
 *   contenteditable 豁免;confirm(.app-confirm-overlay)或 Bootstrap 弹窗
 *   (body.modal-open)在场时不抢。
 * - Esc 走 document **capture**:面板开着时只关面板并 stopPropagation(),
 *   背后的工作台面板 / 弹窗(bubble 监听)收不到 —— 永远「先关最上层」。
 * - 命令的 guard() 决定它在当前页面是否出现(独立页没有地图/面板元素)。
 * 加载顺序(base.html 依赖图):i18n.js、ui.js、theme.js 之后。
 */
window.TerraCommands = (function () {
    'use strict';

    function el(id) { return document.getElementById(id); }

    /* 命令注册表。listed:false 只进速查表;info:true 是纯说明行(无动作);
       带 keys 的进速查表。 */
    var REGISTRY = [
        { id: 'open_palette', titleKey: 'js.cmdk.open_palette', keys: 'Ctrl/⌘+K', listed: false,
          run: function () { toggle(); } },
        { id: 'show_help', titleKey: 'js.cmdk.show_help', keys: '?',
          run: function () { closePalette(); openHelp(); } },
        { id: 'esc_close', titleKey: 'js.cmdk.esc_close', keys: 'Esc', listed: false, info: true },
        { id: 'start_bounds', titleKey: 'js.cmdk.start_bounds',
          guard: function () { return !!el('mapDrawRect'); },
          run: function () { el('mapDrawRect').click(); } },
        { id: 'clear_bounds', titleKey: 'js.cmdk.clear_bounds',
          guard: function () { return !!el('boundsClearBtn'); },
          run: function () { el('boundsClearBtn').click(); } },
        { id: 'new_download', titleKey: 'js.cmdk.new_download',
          guard: function () {
              return !!el('boundsDownloadBtn') && typeof window.openDownloadModal === 'function';
          },
          run: function () { window.openDownloadModal(); } },
        { id: 'open_tasks', titleKey: 'js.cmdk.open_tasks',
          guard: function () { return !!el('historyPanel') && typeof window.openPanel === 'function'; },
          run: function () { window.openPanel('records'); } },
        { id: 'open_config', titleKey: 'js.cmdk.open_config',
          guard: function () { return !!el('configPanel') && typeof window.openPanel === 'function'; },
          run: function () { window.openPanel('config'); } },
        { id: 'open_process', titleKey: 'js.cmdk.open_process',
          guard: function () { return !!el('processOpenBtn'); },
          run: function () { el('processOpenBtn').click(); } },
        { id: 'copy_coords', titleKey: 'js.cmdk.copy_coords',
          guard: function () { return !!el('statusCoords'); },
          run: function () { el('statusCoords').click(); } },
        { id: 'goto_history', titleKey: 'js.cmdk.goto_history',
          run: function () { window.location.href = '/history'; } },
        { id: 'goto_config', titleKey: 'js.cmdk.goto_config',
          run: function () { window.location.href = '/config'; } },
        { id: 'theme_dark', titleKey: 'js.cmdk.theme_dark',
          guard: function () { return !!(window.TerraTheme && window.TerraTheme.set); },
          run: function () { window.TerraTheme.set('dark'); } },
        { id: 'theme_light', titleKey: 'js.cmdk.theme_light',
          guard: function () { return !!(window.TerraTheme && window.TerraTheme.set); },
          run: function () { window.TerraTheme.set('light'); } },
        { id: 'lang_switch', titleKey: 'js.cmdk.lang_switch',
          run: function () {
              var next = (window.__LANG__ === 'zh') ? 'en' : 'zh';
              document.cookie = 'tf-lang=' + next + ';path=/;max-age=31536000';
              window.location.reload();
          } },
    ];

    var palette = el('cmdk');
    var input = el('cmdkInput');
    var list = el('cmdkList');
    var help = el('cmdkHelp');
    var helpList = el('cmdkHelpList');
    if (!palette || !input || !list || !help || !helpList) {
        // 没有外壳的页面(理论上不会 —— base.html 全站 include):整个空载。
        return { open: function () {}, close: function () {}, openHelp: function () {},
                 closeHelp: function () {}, isOpen: function () { return false; } };
    }

    var items = [];       // 当前过滤结果(REGISTRY 条目)
    var active = 0;       // 高亮下标
    var restoreFocus = null;

    function title(cmd) { return t(cmd.titleKey); }
    function isPaletteOpen() { return !palette.hidden; }
    function isHelpOpen() { return !help.hidden; }
    function isOpen() { return isPaletteOpen() || isHelpOpen(); }

    function isEditable(target) {
        return target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA'
            || target.tagName === 'SELECT' || target.isContentEditable);
    }

    function overlayBusy() {
        return !!document.querySelector('.app-confirm-overlay')
            || document.body.classList.contains('modal-open');
    }

    function visibleCommands() {
        return REGISTRY.filter(function (c) {
            if (c.listed === false || c.info) return false;
            return !c.guard || c.guard();
        });
    }

    function setActive(i) {
        if (!items.length) return;
        active = (i + items.length) % items.length;
        [].forEach.call(list.children, function (li, j) {
            var on = j === active;
            li.classList.toggle('cmdk__item--active', on);
            li.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        input.setAttribute('aria-activedescendant', 'cmdk-item-' + items[active].id);
    }

    function run(cmd) {
        closePalette();
        if (cmd.run) cmd.run();
    }

    function render(query) {
        var q = query.trim().toLowerCase();
        items = visibleCommands().filter(function (c) {
            return !q || title(c).toLowerCase().indexOf(q) !== -1;
        });
        active = 0;
        list.textContent = '';
        if (!items.length) {
            var empty = document.createElement('li');
            empty.className = 'cmdk__empty';
            empty.textContent = t('js.cmdk.empty');
            list.appendChild(empty);
            return;
        }
        items.forEach(function (c, i) {
            var li = document.createElement('li');
            li.className = 'cmdk__item' + (i === active ? ' cmdk__item--active' : '');
            li.id = 'cmdk-item-' + c.id;
            li.setAttribute('role', 'option');
            li.setAttribute('aria-selected', i === active ? 'true' : 'false');
            var label = document.createElement('span');
            label.textContent = title(c);
            li.appendChild(label);
            if (c.keys) {
                var kbd = document.createElement('kbd');
                kbd.textContent = c.keys;
                li.appendChild(kbd);
            }
            li.addEventListener('click', function () { run(c); });
            li.addEventListener('mousemove', function () { setActive(i); });
            list.appendChild(li);
        });
        input.setAttribute('aria-activedescendant', 'cmdk-item-' + items[active].id);
    }

    function openPalette() {
        if (overlayBusy()) return;
        restoreFocus = document.activeElement;
        palette.hidden = false;
        input.value = '';
        render('');
        try { input.focus(); } catch (e) { /* 元素可能已不在文档里 */ }
    }

    function closePalette() {
        if (palette.hidden) return;
        palette.hidden = true;
        if (restoreFocus && typeof restoreFocus.focus === 'function') {
            try { restoreFocus.focus(); } catch (e) { /* 同上 */ }
        }
        restoreFocus = null;
    }

    function toggle() {
        if (isHelpOpen()) closeHelp();
        else if (isPaletteOpen()) closePalette();
        else openPalette();
    }

    // ---- 速查表:注册表里带 keys 的条目 ----------------

    function renderHelp() {
        helpList.textContent = '';
        REGISTRY.filter(function (c) { return c.keys; }).forEach(function (c) {
            var li = document.createElement('li');
            li.className = 'cmdk__help-row';
            var label = document.createElement('span');
            label.textContent = title(c);
            var kbd = document.createElement('kbd');
            kbd.textContent = c.keys;
            li.appendChild(label);
            li.appendChild(kbd);
            helpList.appendChild(li);
        });
    }

    function openHelp() {
        if (overlayBusy()) return;
        renderHelp();
        help.hidden = false;
        var btn = help.querySelector('.cmdk__help-close');
        try { (btn || help).focus(); } catch (e) { /* 忽略 */ }
    }

    function closeHelp() { help.hidden = true; }

    // ---- 事件接线 ----------------

    input.addEventListener('input', function () { render(input.value); });
    input.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowDown') { e.preventDefault(); setActive(active + 1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(active - 1); }
        else if (e.key === 'Enter') {
            e.preventDefault();
            if (items[active]) run(items[active]);
        }
        // Esc 不在这里处理 —— 统一走下面的 document capture(面板/速查同一层)。
    });

    palette.querySelector('[data-cmdk-close]').addEventListener('click', closePalette);
    [].forEach.call(help.querySelectorAll('[data-cmdk-help-close]'), function (n) {
        n.addEventListener('click', closeHelp);
    });

    // 全局键(bubble):Ctrl/Cmd+K 开关面板,`?` 开速查。
    window.addEventListener('keydown', function (e) {
        if (e.defaultPrevented) return;
        if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'k' || e.key === 'K')) {
            // 面板开着时焦点在自家输入框里,也要能 toggle 关闭 —— 所以可编辑
            // 豁免只挡「面板没开」的情况(别把正文输入框里的 Ctrl+K 抢过来)。
            if (!isOpen() && isEditable(e.target)) return;
            e.preventDefault();
            toggle();
            return;
        }
        if (isOpen() || overlayBusy() || isEditable(e.target)) return;
        if (e.key === '?') {
            e.preventDefault();
            openHelp();
        }
    });

    // Esc(capture):只关最上层,拦截穿透 —— 工作台面板 / Bootstrap 弹窗
    // 都在 bubble 段监听,stopPropagation 后它们收不到这次 Esc。
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        if (isHelpOpen()) { e.stopPropagation(); closeHelp(); }
        else if (isPaletteOpen()) { e.stopPropagation(); closePalette(); }
    }, true);

    return { open: openPalette, close: closePalette, openHelp: openHelp,
             closeHelp: closeHelp, isOpen: isOpen };
})();
```

**3c. `src/i18n/catalog/js_commands.py`**(新建):

```python
"""static/js/command_palette.js 的界面文案。key 命名:`js.cmdk.<命令id>`,
与注册表条目的 id 一一对应(完整字面量写在 titleKey 里)。"""

MESSAGES = {
    'js.cmdk.open_palette': {
        'zh': '命令面板',
        'en': 'Command palette',
    },
    'js.cmdk.show_help': {
        'zh': '快捷键速查',
        'en': 'Keyboard shortcuts',
    },
    'js.cmdk.esc_close': {
        'zh': '关闭最上层浮层',
        'en': 'Close topmost overlay',
    },
    'js.cmdk.start_bounds': {
        'zh': '开始框选',
        'en': 'Draw a selection',
    },
    'js.cmdk.clear_bounds': {
        'zh': '清除选区',
        'en': 'Clear selection',
    },
    'js.cmdk.new_download': {
        'zh': '新建下载任务',
        'en': 'New download task',
    },
    'js.cmdk.open_tasks': {
        'zh': '打开任务面板',
        'en': 'Open tasks panel',
    },
    'js.cmdk.open_config': {
        'zh': '打开配置面板',
        'en': 'Open config panel',
    },
    'js.cmdk.open_process': {
        'zh': '打开本地处理',
        'en': 'Open local processing',
    },
    'js.cmdk.copy_coords': {
        'zh': '复制当前坐标',
        'en': 'Copy current coordinates',
    },
    'js.cmdk.goto_history': {
        'zh': '前往历史记录页',
        'en': 'Go to history page',
    },
    'js.cmdk.goto_config': {
        'zh': '前往配置页',
        'en': 'Go to config page',
    },
    'js.cmdk.theme_dark': {
        'zh': '切换到暗黑主题',
        'en': 'Switch to dark theme',
    },
    'js.cmdk.theme_light': {
        'zh': '切换到明亮主题',
        'en': 'Switch to light theme',
    },
    'js.cmdk.lang_switch': {
        'zh': '切换界面语言',
        'en': 'Switch interface language',
    },
    'js.cmdk.empty': {
        'zh': '无匹配命令',
        'en': 'No matching commands',
    },
}
```

**3d. `src/i18n/catalog/__init__.py`**:import 行加 `js_commands`(`js_base_terrain` 之后),`_DOMAINS` 元组在 `js_base_terrain,` 之后加一行 `    js_commands,`。

**3e. `src/i18n/catalog/tpl_base.py`**:追加(照该文件体例):

```python
    'tpl.base.cmdk.title': {
        'zh': '命令面板',
        'en': 'Command Palette',
    },
    'tpl.base.cmdk.placeholder': {
        'zh': '输入命令…',
        'en': 'Type a command…',
    },
    'tpl.base.cmdk.help_title': {
        'zh': '快捷键',
        'en': 'Keyboard Shortcuts',
    },
    'tpl.base.cmdk.help_close': {
        'zh': '关闭',
        'en': 'Close',
    },
```

**3f. `templates/base.html`**:
- 在任务详情模态框(`#taskDetailModal` 的 `</div>` 收尾,现 ~L193)之后加:

```html
    <!-- 命令面板(Ctrl/Cmd+K)与快捷键速查:外壳在此,列表由 command_palette.js
         按注册表填充。留在 <body> 直下 —— 与 #taskDetailModal 同一个理由。 -->
    {% include "_command_palette.html" %}
```

- 在 `theme.js` 加载与 `TerraTheme.init();` 之后加:

```html
    <!-- 命令面板(Ctrl/Cmd+K):必须排在 i18n.js(解析期就调 t())与
         theme.js(主题命令调 TerraTheme.set)之后。无地图的页面空载可用,
         地图类命令由注册表 guard() 自动隐藏。 -->
    <script src="{{ url_for('static', filename='js/command_palette.js') }}"></script>
```

**3g. `static/css/style.css`**:在 `.app-confirm-overlay` 一节附近(现 ~L2811 之后)追加:

```css
/* ---- 命令面板(Ctrl/Cmd+K)与快捷键速查 ----------------------------------
   停靠层规则:不透明、elevated 底、模态级阴影 —— 它盖住的是整个工作台,
   不是直接浮在地图上,所以**不做玻璃**。即开即关,不加 transition
   (动画索引不动)。z-index 13100:高于 confirm(12000) —— 但 confirm 在场时
   全局键让位(command_palette.js),两者不会同框;13000 预留给拖拽遮罩。 */
.cmdk {
    position: fixed;
    inset: 0;
    z-index: 13100;
}

.cmdk[hidden] {
    display: none;
}

.cmdk__backdrop {
    position: absolute;
    inset: 0;
    background: var(--color-backdrop);
}

.cmdk__dialog {
    position: relative;
    margin: 15vh auto 0;
    width: min(560px, calc(100vw - 32px));
    background: var(--color-bg-elevated);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-card);
    box-shadow: var(--shadow-lg);
    overflow: hidden;
}

.cmdk__input {
    width: 100%;
    min-height: 44px;
    padding: 0 14px;
    font-size: var(--font-size-md);
    color: var(--color-text-primary);
    background: transparent;
    border: none;
    border-bottom: 1px solid var(--color-border);
    outline: none;
}

.cmdk__list {
    max-height: 50vh;
    overflow-y: auto;
    margin: 0;
    padding: 6px;
    list-style: none;
}

.cmdk__item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 7px 10px;
    border-radius: var(--radius-sm);
    color: var(--color-text-secondary);
    cursor: pointer;
}

.cmdk__item--active {
    background: var(--color-accent-muted);
    color: var(--color-text-primary);
}

.cmdk__item kbd,
.cmdk__help-row kbd {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--color-text-secondary);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-xxs);
    padding: 1px 6px;
    white-space: nowrap;
}

.cmdk__empty {
    padding: 12px;
    text-align: center;
    color: var(--color-text-secondary);
}

.cmdk__help-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    border-bottom: 1px solid var(--color-border);
    font-weight: 600;
    color: var(--color-text-primary);
}

.cmdk__help-close {
    border: none;
    background: transparent;
    color: var(--color-text-secondary);
    font-size: var(--font-size-lg);
    line-height: 1;
    padding: 2px 6px;
    cursor: pointer;
    border-radius: var(--radius-xxs);
}

.cmdk__help-close:hover {
    background: var(--color-control-hover);
    color: var(--color-text-primary);
}

.cmdk__help-list {
    margin: 0;
    padding: 8px 14px 12px;
    list-style: none;
}

.cmdk__help-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 5px 0;
    color: var(--color-text-secondary);
}
```

- [ ] **Step 4: 跑新测试确认绿**

Run: `.venv/bin/python -m pytest tests/test_command_palette.py -q`
Expected: 7 passed(node 不可用时 6 + 1 skipped)

- [ ] **Step 5: 契约回归 + 全量**

Run: `.venv/bin/python -m pytest tests/test_css_contract.py tests/test_i18n.py -q`
Expected: 全绿(重点看:i18n 双向闭合影不误报孤儿键;文字模型不因 `.cmdk` 新规则报「模型失效」—— 全部 color 声明都在顶层、无组合符)
Run: `.venv/bin/python -m pytest -q`
Expected: 全绿

---

### Task 5(P2 可选): 全窗口拖拽 .tif 打开本地处理

**Files:**
- Create: `static/js/drop_process.js`
- Create: `src/i18n/catalog/js_drop_process.py`
- Modify: `src/i18n/catalog/__init__.py`(注册新域)
- Modify: `templates/index.html`(extra_js 块加脚本)
- Modify: `static/css/style.css`(`.drop-veil` 一节)
- Modify: `tests/test_css_contract.py`(`_MOTION_BRANCH_COUNT` 39 → 40 + 登记注释)
- Test: `tests/test_drop_process.py`(新建)

**Interfaces:**
- Consumes: `#processModal` / `#processType` / `#processSource` / `#localTerrainFiles`(index.html),`window.showToast`(ui.js),`bootstrap.Modal`,`t()`。
- Produces: 无新全局(纯 IIFE 自接线)。`localStorage` 不写任何东西。

**背景知识:**
- 仅首页生效:没有 `#processModal` 的页面直接 return 空载。
- `dragover` 必须 `preventDefault`,否则 drop 不触发(浏览器默认禁止投放)。
- 遮罩 `pointer-events: none`:纯展示,事件始终落 window,避免「拖到遮罩上触发 leave」的抖动;卡死自救靠 `blur` 与 Esc(capture)。
- 文件赋值走 `new DataTransfer()` 过滤后构造 FileList(只留 .tif/.tiff);`input.files = dt.files` 现代浏览器允许。
- 本任务给 `.drop-veil` 加一条 `transition: opacity`,必须把 `tests/test_css_contract.py` 的 `_MOTION_BRANCH_COUNT` 从 39 改成 40,并在其上方登记注释里加一行(照 6107-6112 行段的体例)。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_drop_process.py`,完整内容:

```python
"""全窗口拖拽 .tif 打开本地处理的契约测试(2026-08-11 设计 §3.6,P2)。

借鉴 GeoLibre 的窗口级 drag-drop:拖入时全屏遮罩提示,松手把过滤后的
.tif/.tiff 喂给 #processForm 的 #localTerrainFiles 并打开 #processModal。
"""

import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DROP_JS = os.path.join(ROOT, 'static', 'js', 'drop_process.js')
INDEX_HTML = os.path.join(ROOT, 'templates', 'index.html')
CSS_PATH = os.path.join(ROOT, 'static', 'css', 'style.css')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def test_index_page_loads_drop_process():
    src = _read(INDEX_HTML)
    assert re.search(r'<script[^>]+src=[^>]*js/drop_process\.js', src), (
        'index.html 没有加载 js/drop_process.js'
    )


def test_drop_process_behavior_contract():
    src = _read(DROP_JS)
    assert "getElementById('processModal')" in src, '缺首页守卫(无弹窗页空载)'
    assert "'dragenter'" in src and "'dragleave'" in src and "'drop'" in src, (
        'dragenter/dragleave/drop 三个事件都要接'
    )
    assert 'DataTransfer' in src, '应用 DataTransfer 过滤构造 FileList(只留 .tif)'
    assert re.search(r'tiff?', src), '文件过滤必须认 .tif / .tiff'
    assert "getElementById('localTerrainFiles')" in src, '文件要喂给 #localTerrainFiles'
    assert 'showToast' in src, '失败/无 tif 路径要走 showToast 提示'
    for key in ("'js.drop.hint'", "'js.drop.no_tif'", "'js.drop.failed'"):
        assert key in src, f'缺 i18n 键字面量 {key}(双向闭合按字面量扫)'
    assert 'blur' in src, '窗口失焦(blur)是遮罩卡死的自救路径'


@pytest.mark.skipif(shutil.which('node') is None, reason='node 不可用')
def test_drop_process_js_passes_node_syntax_check():
    subprocess.run(['node', '--check', DROP_JS],
                   capture_output=True, text=True, check=True, timeout=120)


def test_drop_veil_css():
    css = re.sub(r'/\*.*?\*/', '', _read(CSS_PATH), flags=re.S)
    m = re.search(r'\.drop-veil\s*\{([^}]*)\}', css)
    assert m, '缺 .drop-veil 规则'
    body = m.group(1)
    assert 'pointer-events: none' in body, '遮罩必须纯展示(事件始终落 window)'
    assert 'z-index: 13000' in body, '遮罩 z-index 应为 13000(命令面板 13100 之下)'
    m = re.search(r'\.drop-veil--in\s*\{([^}]*)\}', css)
    assert m and 'opacity: 1' in m.group(1), '缺 .drop-veil--in 的显示态'
```

- [ ] **Step 2: 跑测试确认红**

Run: `.venv/bin/python -m pytest tests/test_drop_process.py -q`
Expected: FAIL(js / css 尚不存在)

- [ ] **Step 3: 实现**

**3a. `static/js/drop_process.js`**(新建,完整内容):

```js
/**
 * 全窗口拖拽 .tif/.tiff 打开「本地处理」(2026-08-11 设计 §3.6,
 * 借鉴 GeoLibre 的窗口级 drag-drop)。
 *
 * - 只在首页生效:没有 #processModal 的页面(/config、/history)整个空载。
 * - 遮罩纯展示(pointer-events:none),拖放事件始终落 window;
 *   卡死自救:窗口失焦(blur)或 Esc(capture)强制复位。
 * - 文件经 DataTransfer 过滤(只留 .tif/.tiff)后喂给 #localTerrainFiles,
 *   并 dispatch change 让 map.js 的 updateTifInfo 等既有接线照常跑。
 */
(function () {
    'use strict';

    var modalEl = document.getElementById('processModal');
    if (!modalEl) return;

    var depth = 0;    // dragenter/dragleave 深度计数(进出子元素会成对触发)
    var veil = null;

    function buildVeil() {
        if (veil) return veil;
        veil = document.createElement('div');
        veil.className = 'drop-veil';
        var tip = document.createElement('span');
        tip.className = 'drop-veil__tip';
        tip.textContent = t('js.drop.hint');
        veil.appendChild(tip);
        document.body.appendChild(veil);
        return veil;
    }

    function show() { buildVeil().classList.add('drop-veil--in'); }
    function hide() { if (veil) veil.classList.remove('drop-veil--in'); }
    function reset() { depth = 0; hide(); }

    function hasFiles(e) {
        var types = e.dataTransfer && e.dataTransfer.types;
        return !!(types && [].indexOf.call(types, 'Files') !== -1);
    }

    window.addEventListener('dragenter', function (e) {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth += 1;
        show();
    });
    // dragover 必须 preventDefault,drop 才会触发(浏览器默认不许投放)。
    window.addEventListener('dragover', function (e) {
        if (depth > 0) e.preventDefault();
    });
    window.addEventListener('dragleave', function () {
        depth = Math.max(0, depth - 1);
        if (depth === 0) hide();
    });
    window.addEventListener('drop', function (e) {
        if (depth === 0 && !hasFiles(e)) return;
        e.preventDefault();
        reset();
        var files = [].filter.call(e.dataTransfer.files, function (f) {
            return /\.tiff?$/i.test(f.name);
        });
        if (!files.length) {
            window.showToast(t('js.drop.no_tif'), 'warning');
            return;
        }
        var input = document.getElementById('localTerrainFiles');
        try {
            var dt = new DataTransfer();
            files.forEach(function (f) { dt.items.add(f); });
            input.files = dt.files;
        } catch (err) {
            window.showToast(t('js.drop.failed'), 'danger');
            return;
        }
        // 先摆正两个下拉(触发既有 change 接线刷新行显隐),再喂文件、开弹窗。
        var typeSel = document.getElementById('processType');
        var srcSel = document.getElementById('processSource');
        typeSel.value = 'local_terrain';
        typeSel.dispatchEvent(new Event('change', { bubbles: true }));
        srcSel.value = 'upload';
        srcSel.dispatchEvent(new Event('change', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        bootstrap.Modal.getOrCreateInstance(modalEl).show();
    });

    // 卡死自救:拖出窗口松手时 dragleave/drop 可能整个丢,blur 兜底。
    window.addEventListener('blur', reset);
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && veil && veil.classList.contains('drop-veil--in')) {
            e.stopPropagation();
            reset();
        }
    }, true);
})();
```

**3b. `src/i18n/catalog/js_drop_process.py`**(新建):

```python
"""static/js/drop_process.js 的界面文案。"""

MESSAGES = {
    'js.drop.hint': {
        'zh': '松开鼠标,用这些文件打开本地处理',
        'en': 'Drop to open local processing with these files',
    },
    'js.drop.no_tif': {
        'zh': '未找到 .tif / .tiff 文件',
        'en': 'No .tif / .tiff files found',
    },
    'js.drop.failed': {
        'zh': '无法读取拖入的文件',
        'en': 'Could not read the dropped files',
    },
}
```

**3c. `src/i18n/catalog/__init__.py`**:import 行加 `js_drop_process`,`_DOMAINS` 在 `js_commands,` 后加 `    js_drop_process,`。

**3d. `templates/index.html`**:extra_js 块在 `panels.js` 那行之后加:

```html
<script src="{{ url_for('static', filename='js/drop_process.js') }}" defer></script>
```

**3e. `static/css/style.css`**:在 `.cmdk` 一节之后追加:

```css
/* ---- 全窗口拖拽提示遮罩(P2)----------------------------------------------
   纯展示(pointer-events:none),事件始终落 window;直接浮在地图上,
   用玻璃令牌 + accent 虚线框。opacity 过渡一条(动画索引已登记)。 */
.drop-veil {
    position: fixed;
    inset: 0;
    z-index: 13000;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--color-backdrop-strong);
    border: 2px dashed var(--color-accent);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.15s ease;
}

.drop-veil--in {
    opacity: 1;
}

.drop-veil__tip {
    padding: 10px 18px;
    border-radius: var(--radius-pill);
    background: var(--color-glass-surface);
    border: 1px solid var(--color-glass-border);
    color: var(--color-text-primary);
    font-size: var(--font-size-md);
}
```

**3f. `tests/test_css_contract.py` 动画锚点**:`_MOTION_BRANCH_COUNT = 39` 改为 `= 40`,并在其上方登记注释段(现 ~L6107-6112)末尾照体例加一行:

```python
# 39 -> 40(全窗口拖拽遮罩):`.drop-veil`(opacity 0.15s,P2 拖拽提示)。
#   纯展示无 pointer-events,在 reduce 块 `*` 覆盖范围内,无需豁免登记。
```

- [ ] **Step 4: 跑新测试确认绿**

Run: `.venv/bin/python -m pytest tests/test_drop_process.py -q`
Expected: 4 passed(node 不可用时 3 + 1 skipped)

- [ ] **Step 5: 契约回归 + 全量**

Run: `.venv/bin/python -m pytest tests/test_css_contract.py tests/test_i18n.py -q`
Expected: 全绿(重点:`test_motion_rule_index_is_complete` 应认到 40 个分支)
Run: `.venv/bin/python -m pytest -q`
Expected: 全绿

---

## 收尾(全部任务完成后)

1. `.venv/bin/python -m pytest -q` 全绿。
2. 人工核对清单(启动开发服务器目测):
   - 首页:工具条/状态栏/框选浮层呈玻璃质感;面板、模态不透明且底色为 elevated。
   - 配置「外观」组:强调色五预设即时生效,splash、选区描边、滚动条滑块跟随。
   - 明暗两主题 × 五预设各扫一眼(重点:填充按钮墨色、状态徽章与 accent 的区分)。
   - 面板左缘拖拽调宽,刷新后保持;窗口缩到 <768px 拖拽消失。
   - Ctrl/Cmd+K 开面板,过滤/方向键/Enter/Esc;`?` 开速查;面板开在工作台面板之上时 Esc 只关它。
   - (P2)拖 .tif 进窗口出遮罩,松手打开本地处理且文件已就位;拖非 tif 出提示。
3. 更新 `CHANGELOG.md` 与 `docs/README.md` 索引(如维护者认为需要)。
4. 把 `docs/superpowers/specs/2026-08-11-geolibre-inspired-ui-design.md`、本计划、全部代码改动一并交给用户审阅,由用户决定提交粒度(本环境规则:agent 不主动 git 提交)。
