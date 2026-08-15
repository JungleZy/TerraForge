"""面板拖拽调宽的契约测试(2026-08-11 UI 改版,设计 §3.4)。

新建面板 / 任务面板 / 配置面板的左缘 8px 热区可拖拽调宽(缺省 480 / 920 / 480px);
拖拽中只写 CSS 变量,松手写 localStorage;窄屏(<768px,面板已全屏覆盖)禁用。
#pluginsPanel 故意不可调宽(单列插件卡,没有横向内容要腾)。
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
# 2026-08-15 Task 5:#createPanel 进了 RESIZE_CONFIGS,key 从两个变三个。
# 三个 key 必须各自独立,见 test_resize_configs_cover_every_resizable_panel。
STORAGE_KEYS = ('tf-panel-w-tasks', 'tf-panel-w-config', 'tf-panel-w-create')


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


def test_resizer_markup_present_in_every_resizable_panel():
    src = _read(INDEX_HTML)
    # 旧值是 2(historyPanel / configPanel 各一个)。2026-08-15 Task 5 起
    # #createPanel 也进了 panels.js 的 RESIZE_CONFIGS,标记跟着多一个。
    # 不变式没变:仍然是「可调宽面板数 == resizer 元素数」,只是可调宽面板
    # 从两个变成三个 —— 少一个 resizer 就是有面板拖不动。
    got = src.count('data-panel-resizer')
    assert got == 3, (
        f'index.html 里有 {got} 个 data-panel-resizer,期望 3 个:'
        '#createPanel / #historyPanel / #configPanel 各一个;'
        '#pluginsPanel 故意没有(它不在 panels.js 的 RESIZE_CONFIGS 里)'
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


def _resize_configs():
    """从 panels.js 解出 RESIZE_CONFIGS 的每一项 (id, varName, key)。"""
    src = _read(PANELS_JS)
    m = re.search(r'var RESIZE_CONFIGS = \[(.*?)\];', src, re.S)
    assert m, 'panels.js 里找不到 RESIZE_CONFIGS —— 本测试已失效'
    rows = re.findall(
        r"\{\s*id:\s*'(\w+)',\s*varName:\s*'([^']+)',\s*key:\s*'([^']+)'",
        m.group(1))
    assert len(rows) == 3, (
        f'解出 {len(rows)} 条 RESIZE_CONFIGS,期望 3 条 —— 正则失配或漏了面板')
    return rows


def test_resize_configs_cover_every_resizable_panel():
    """三个可调宽面板都在 RESIZE_CONFIGS 里,且 localStorage key 互不相同。

    2026-08-15 Task 5:#createPanel 加入(旧表只有 historyPanel / configPanel)。

    这里钉 **key 唯一** 而不是 varName 唯一:createPanel 与 configPanel 故意
    共用 `--panel-config-w`。applyPanelWidth 是 `el.style.setProperty(varName,…)`
    —— 变量**内联写在面板元素上**而不是 :root,所以同名令牌在两个面板上各是
    一份独立的内联值,互不干扰,还共享 :root 里那条 480px 缺省(少铸一个令牌、
    少一条 CSS 消费点)。真正会串味的是 localStorage:两个面板若共用一个 key,
    拖窄新建面板下次会把配置面板也开成那个宽度,而且两边的 min/max
    (380–720 vs 320–640)会互相夹断。
    """
    rows = _resize_configs()
    ids = sorted(r[0] for r in rows)
    assert ids == ['configPanel', 'createPanel', 'historyPanel'], (
        f'RESIZE_CONFIGS 的面板是 {ids},期望 createPanel / historyPanel / '
        'configPanel(#pluginsPanel 故意不可调宽)')
    keys = [r[2] for r in rows]
    assert len(set(keys)) == len(keys), (
        f'RESIZE_CONFIGS 的 localStorage key 有重复:{keys} —— '
        '共用 key 的两个面板会互相覆盖对方记住的宽度')
    assert set(keys) == set(STORAGE_KEYS), (
        f'panels.js 的 key 集合 {sorted(keys)} 与本文件 STORAGE_KEYS '
        f'{sorted(STORAGE_KEYS)} 不一致 —— 两头有一头没跟上')
    for _id, var, _key in rows:
        assert var in WIDTH_VARS, (
            f'#{_id} 用了未登记的宽度令牌 {var} —— :root 里没有它的缺省值,'
            '没拖过的面板会退化成 CSS 里的 auto/0')


@pytest.mark.skipif(shutil.which('node') is None, reason='node 不可用')
def test_panels_js_passes_node_syntax_check():
    subprocess.run(['node', '--check', PANELS_JS],
                   capture_output=True, text=True, check=True, timeout=120)
