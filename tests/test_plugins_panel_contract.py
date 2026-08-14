"""插件面板的接线契约：PANELS 注册、懒初始化、模板包含、脚本加载、i18n 域。

为什么是「接线」契约而不是渲染契约：面板列表是浏览器里 fetch 回来现渲染的，
Python 侧看不到它。这里钉的是**四段线有没有接上** —— 入口按钮 → panels.js 的
PANELS 表 → 懒初始化 → plugins.js，任何一环断掉面板都打不开，而不会有任何
既有用例变红。渲染与启停/建任务的行为由隔离环境的真实端到端验证覆盖
（见 .superpowers/sdd/2026-08-12-plugin-system/task-10-report.md）。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def test_panel_registered():
    src = _read('static/js/panels.js')
    assert "plugins: 'pluginsPanel'" in src, (
        "panels.js 的 PANELS 表里没有 plugins → pluginsPanel，"
        'data-panel="plugins" 的按钮点了什么都不会发生')
    assert 'initPlugins' in src, 'panels.js 没有在面板首次打开时懒初始化插件列表'


def test_template_included():
    src = _read('templates/index.html')
    assert 'id="pluginsPanel"' in src
    assert '_plugins_content.html' in src
    assert 'js/plugins.js' in src
    assert 'data-panel="plugins"' in src


def test_i18n_domains_registered():
    init = _read('src/i18n/catalog/__init__.py')
    assert 'tpl_plugins' in init and 'js_plugins' in init, (
        '两个域没在 catalog/__init__.py 登记 —— 键取不到，界面上直接显示键名')
    assert "'tpl.plugins.title'" in _read('src/i18n/catalog/tpl_plugins.py')
    assert "'js.plugins.new_task'" in _read('src/i18n/catalog/js_plugins.py')


def test_browser_side_wording_is_not_in_a_template_domain():
    """plugins.js 渲染的文案必须全部是 `js.` 键。

    这条不是形式主义：`client_catalog()`（src/i18n/__init__.py）只把 `js.`
    前缀那部分内联给浏览器，模板文案在服务端就渲染完了。plugins.js 里写
    `t('tpl.plugins.enable')` 不会报错 —— t() 查不到就返回键本身，用户看到的是
    界面上一颗写着「tpl.plugins.enable」的按钮。
    """
    stray = sorted(set(re.findall(r"t\('(tpl\.[\w.]+)'", _read('static/js/plugins.js'))))
    assert not stray, (
        'plugins.js 引用了模板域的键，它们不会内联到浏览器，界面上会显示键名本身：\n'
        + '\n'.join('  ' + k for k in stray))


def test_declarative_form_wired():
    src = _read('static/js/plugins.js')
    assert 'data-newtask' in src, '卡片上没有新建任务入口'
    assert "'/schema'" in src, '表单不是按 /api/plugins/<pid>/schema 渲染的'
    assert 'plugin-task-form' in src
    # bbox 的四个数字输入（v1 刻意不接地图框选）。
    for field in ('north', 'south', 'east', 'west'):
        assert f"'{field}'" in src, f'表单缺 {field} 输入'
    # auto_start 是请求上的动作开关（T8 修好的那条路），响应的 started
    # 决定提示「已创建并启动」还是「已创建，启动失败」。
    assert 'auto_start: true' in src
    assert 'started === false' in src, (
        '没有按响应的 started 区分「已创建并启动」与「已创建但启动失败」')


def test_third_party_strings_are_escaped():
    """插件名/描述/参数标签来自 plugin.toml 与插件代码，是第三方字符串。

    它们不进 i18n catalog（翻译归插件作者），但每一处进 innerHTML 之前都必须
    过 esc() —— 一个插件把名字写成 `<img onerror=…>` 就是宿主页面上的 XSS。
    """
    src = _read('static/js/plugins.js')
    for expr in ('esc(p.name)', 'esc(p.description)', 'esc(p.load_error)',
                 'esc(p.id)', 'esc(s.label || s.key)'):
        assert expr in src, f'第三方字符串没转义：缺 {expr}'
