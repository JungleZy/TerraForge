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
