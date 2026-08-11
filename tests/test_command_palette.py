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


def test_help_close_restores_focus_and_dialogs_trap_tab():
    """aria-modal 的两个 dialog 必须有 Tab 焦点环;closeHelp 必须恢复焦点
    (终审修复,防回归)。"""
    src = _read(CMD_JS)
    assert 'trapTab' in src and "'Tab'" in src, (
        '两个 aria-modal dialog 缺 Tab 焦点环(trapTab)——Tab 会逃出遮罩'
    )
    assert re.search(r'function closeHelp\(\)[\s\S]*?focus\(\)', src), (
        'closeHelp 必须恢复焦点(与 closePalette 的 restoreFocus 闭环同构)'
    )
