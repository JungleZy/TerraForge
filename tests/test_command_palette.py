"""命令面板(Ctrl/Cmd+K)与快捷键速查(?)的契约测试(2026-08-11 设计 §3.3)。

- 单一命令注册表驱动面板列表与速查表;键字面量完整出现在源码(i18n 双向闭合)。
- 全局键:Ctrl/Cmd+K 开关面板、`?` 开速查;输入控件与 defaultPrevented 豁免;
  confirm / Bootstrap modal 在场时不抢。
- Esc 不自己监听：向 panels.js 的层栈 register('cmdk'/'cmdkHelp')，全站唯一那个
  「关最上层」的 keydown 在那里（2026-08-15 Task 6）。
"""

import importlib
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# 2026-08-15 Task 3：层栈令牌化之后 `.cmdk` 的 z-index 是 `var(--z-cmdk)`。
# 复用 test_css_contract 的解析器而不是在这里再写一个「跟 var()」的小函数
# —— 三份测试文件（本文件 / test_drop_process.py /
# test_fix_terrain_preview_transition.py）都要跟这一层，各写一份必然分叉。
from test_css_contract import _resolve_z_index

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_HTML = os.path.join(ROOT, 'templates', 'base.html')
PARTIAL = os.path.join(ROOT, 'templates', '_command_palette.html')
CMD_JS = os.path.join(ROOT, 'static', 'js', 'command_palette.js')
CSS_PATH = os.path.join(ROOT, 'static', 'css', 'style.css')
TEMPLATES_DIR = os.path.join(ROOT, 'templates')
JS_DIR = os.path.join(ROOT, 'static', 'js')

# 注册表全量命令 id —— 每个 id 都必须有对应的 i18n 键字面量。
#
# 2026-08-15 Task 5:摘掉 'goto_history' / 'goto_config'。两条注册表条目连同
# 两个目录键(js.cmdk.goto_history / js.cmdk.goto_config)一起删了 —— 命令面板
# 里同时列「打开任务面板」与「前往历史记录页」是同一件事的两种形态,入口收敛
# 之后只留面板那条。/history、/config 两条**路由本身保留**(深链与打包可达性
# 需要),删掉的只是命令面板里那第二条路。
COMMAND_IDS = (
    'open_palette', 'show_help', 'esc_close',
    'start_bounds', 'clear_bounds', 'new_download',
    'open_tasks', 'open_config', 'open_process', 'copy_coords',
    'theme_dark', 'theme_light', 'lang_switch',
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


def _registry_entries():
    """[(命令 id, 该条目的源码片段)]:按 `{ id: '…'` 切 REGISTRY 数组字面量。"""
    src = _read(CMD_JS)
    m = re.search(r'var REGISTRY = \[(.*?)\n    \];', src, re.S)
    assert m, 'command_palette.js 里找不到 REGISTRY 数组字面量 —— 本测试已失效'
    body = m.group(1)
    starts = [(mm.group(1), mm.start())
              for mm in re.finditer(r"\{\s*id:\s*'(\w+)'", body)]
    assert len(starts) > 8, (
        f'REGISTRY 里只解出 {len(starts)} 条命令 —— 正则失配,本测试已失效')
    return [(cid, body[pos:(starts[i + 1][1] if i + 1 < len(starts) else len(body))])
            for i, (cid, pos) in enumerate(starts)]


def _concat(dirpath, suffix):
    return '\n'.join(
        _read(os.path.join(dirpath, fn))
        for fn in sorted(os.listdir(dirpath)) if fn.endswith(suffix))


def test_registry_ids_match_this_files_list():
    """COMMAND_IDS 与 REGISTRY 的 id 集合必须逐一对齐(双向)。

    上一条只单向遍历 COMMAND_IDS:注册表里新增一条命令、忘了登记到这里,
    它就永远不被任何断言看一眼。
    """
    ids = [cid for cid, _ in _registry_entries()]
    assert len(ids) == len(set(ids)), f'REGISTRY 里有重复 id:{ids}'
    assert set(ids) == set(COMMAND_IDS), (
        f'REGISTRY 的 id 集合 {sorted(ids)} 与本文件 COMMAND_IDS '
        f'{sorted(COMMAND_IDS)} 不一致 —— 两头有一头没跟上')


def test_registry_guards_point_at_hooks_that_still_exist():
    """每条 guard 提到的 DOM id / window 函数都必须真的存在(反「死 guard」)。

    2026-08-15 Task 5 新增的断言,不是 re-anchor。它补的是上面那条
    `test_every_command_id_has_literal_i18n_key` 的一个真实盲区:那条只证明
    **i18n 键**以字面量出现在源码里,而命令要不要出现在面板列表里由 `guard`
    说话。guard 返回 false 时命令**静默消失**——没有报错、没有控制台警告、
    列表里就是少一行。

    这个盲区已经吃过一次:入口收敛前 new_download 的 guard 是
    `!!el('boundsDownloadBtn')`、open_process 的是 `!!el('processOpenBtn')`,
    两个 hook 分别随选区浮层改名与「处理」按钮退役而消失,
    于是两条命令永久不可达,而本文件依旧全绿(键还在,只是没人能触发)。

    检查两类引用:
    · `el('someId')`     -> 全仓必须有 `id="someId"`。既扫 templates/*.html,
      也扫 static/js/*.js —— 有些 hook(#boundsClearBtn 在选区浮层模板串里)
      是 JS 注入的,只扫模板会误报。
    · `typeof window.someFn === 'function'` -> static/js 里必须有
      `function someFn(` 的声明。
    · `window.Ns && window.Ns.x` -> static/js 里必须有 `window.Ns =` 的挂载。
    """
    dom_src = _concat(TEMPLATES_DIR, '.html') + '\n' + _concat(JS_DIR, '.js')
    js_src = _concat(JS_DIR, '.js')

    guards = []
    for cid, entry in _registry_entries():
        m = re.search(r'guard:\s*function\s*\([^)]*\)\s*\{(.*?)\}', entry, re.S)
        if m:
            guards.append((cid, m.group(1)))
    assert len(guards) >= 8, (
        f'只解出 {len(guards)} 条 guard,期望至少 8 条 —— 正则失配,本测试已失效')

    problems = []
    for cid, g in guards:
        for dom_id in re.findall(r"el\(\s*'(\w+)'\s*\)", g):
            if f'id="{dom_id}"' not in dom_src:
                problems.append(
                    f"{cid}: guard 查 el('{dom_id}'),但 templates/ 与 static/js/ "
                    f'里都找不到 id="{dom_id}" 的元素')
        for fn in re.findall(r"typeof\s+window\.(\w+)\s*===\s*'function'", g):
            if not re.search(r'\bfunction\s+' + fn + r'\s*\(', js_src):
                problems.append(
                    f'{cid}: guard 查 window.{fn}(),但 static/js/ 里没有 '
                    f'`function {fn}(` 的声明')
        for ns in re.findall(r'window\.(\w+)\s*&&\s*window\.\1\.\w+', g):
            if not re.search(r'window\.' + ns + r'\s*=', js_src):
                problems.append(
                    f'{cid}: guard 查 window.{ns} 命名空间,但 static/js/ 里没有 '
                    f'`window.{ns} =` 的挂载')

    assert not problems, (
        'guard 指着已经不存在的 hook —— 这些命令在面板里**静默消失**'
        '(guard 返回 false 不报错、不打日志,列表里就是少一行):\n'
        + '\n'.join('  ' + p for p in problems))



def test_keyboard_layering_rules():
    """全局键的豁免与让位；Esc 本身**不在这里** —— 见下一条。"""
    src = _read(CMD_JS)
    assert 'defaultPrevented' in src, '全局键必须尊重 defaultPrevented'
    assert 'isContentEditable' in src, '必须豁免 contenteditable'
    for tag in ("'INPUT'", "'TEXTAREA'", "'SELECT'"):
        assert tag in src, f'必须豁免 {tag}'
    assert "ctrlKey" in src and "metaKey" in src, 'Ctrl/Cmd+K 两个修饰键都要认'
    assert 'app-confirm-overlay' in src, 'confirm 在场时必须让位'
    assert 'modal-open' in src, 'Bootstrap 弹窗在场时必须让位'


def test_escape_is_delegated_to_the_layer_stack():
    """2026-08-15 Task 6：Esc 不再由本文件自己监听。

    改前这里是 `document.addEventListener('keydown', …, true)` —— 捕获阶段 +
    stopPropagation，靠「相位比别人早」抢在工作台面板和 Bootstrap 弹窗前面。
    那是全站三份「关最上层」实现里的一份，三者的先后不是设计出来的，是碰出来
    的：加一层就得回头改另外两份的让位判据，漏改的表现是「一次 Esc 关掉两层」。

    现在唯一那个 keydown 在 panels.js 的层栈里，本文件只 register 两层。
    这条锁的就是「不许再长回来」。
    """
    src = _read(CMD_JS)
    assert not re.search(r"addEventListener\(\s*['\"]keydown['\"][^)]*?,\s*true\s*\)", src), (
        '命令面板又自己挂了一个捕获阶段的 keydown —— 全站只许有一个「关最上层」'
        '的监听，它在 panels.js 的层栈里'
    )
    for name in ("register('cmdk'", "register('cmdkHelp'"):
        assert name in src, f'命令面板没有向层栈报到：缺 TerraLayers.{name})'


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
    assert m, '缺 .cmdk 规则'
    # 2026-08-15 Task 3：层栈令牌化，这里的 13100 从字面量变成 var(--z-cmdk)
    # ——「跟一层 var() 再比」，契约本身没有放宽（仍然必须**恰好**是 13100）。
    z = re.search(r'z-index:\s*([^;]+);', m.group(1))
    assert z, '.cmdk 必须显式声明 z-index，否则层序靠源码顺序碰运气'
    got = _resolve_z_index(css, z.group(1))
    assert got == 13100, f'.cmdk 的 z-index 应为 13100，实际 {z.group(1).strip()!r} -> {got}'


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
