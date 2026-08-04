"""主题切换（暗黑 / 明亮 / 跟随系统）的设计契约测试。

团队统一设计契约（2026-08 主题功能定稿，多代理并行施工共用）：

  - 开关机制：``<html>`` 的 ``data-bs-theme`` 属性是唯一开关，取值 dark / light。
    Bootstrap 5.3 原生响应它；自定义组件全部走 style.css ``:root`` 里的
    ``--color-*`` 令牌（``:root`` 保留暗色为 SSR 默认值，
    ``[data-bs-theme="light"]`` 块覆盖同名令牌）。
  - 偏好存储：localStorage key ``tf-theme``，值为 ``dark`` | ``light`` |
    ``system``，缺省 ``dark``。``system`` =
    ``window.matchMedia('(prefers-color-scheme: light)')``，且监听其 change
    事件实时跟随。
  - base.html ``<head>`` 内联引导脚本在首帧绘制前同步设置属性（避免闪烁）；
    ``<html>`` 标签上必须保留 ``data-bs-theme="dark"`` 字面量作为 SSR 默认
    （tests/test_css_contract.py 也钉着它）。
  - 切换 UI 在配置面板：「外观」分区，三枚带可见文字的按钮
    暗黑 / 明亮 / 跟随系统。
  - 运行逻辑在 static/js/theme.js；引导脚本只负责首帧前定属性，两者都要
    认得同一套 key / 属性 / 三个模式值。

本文件全部是**源码级 + 渲染级**断言，守的是上面的契约点（key 名
``tf-theme``、属性 ``data-bs-theme``、三个模式值），不是某段具体实现文本。
实现由并行代理施工；本文件先落地，实现未落地时它就该红。
"""

import importlib
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_HTML = os.path.join(ROOT, 'templates', 'base.html')
CONFIG_PARTIAL = os.path.join(ROOT, 'templates', '_config_content.html')
THEME_JS = os.path.join(ROOT, 'static', 'js', 'theme.js')

THEME_VALUES = ('dark', 'light', 'system')
THEME_BUTTON_LABELS = ('暗黑', '明亮', '跟随系统')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _base_html():
    return _read(BASE_HTML)


def _theme_js():
    return _read(THEME_JS)


def _load_app(monkeypatch, tmp_path):
    """与 tests/test_statusbar_inside_workbench.py 同一个套路：Config 副作用
    全部重定向到 tmp_path，再新鲜 import app。"""
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


# ---------------------------------------------------------------------------
# 1) base.html 的 <head> 内联引导脚本（首帧绘制前同步定属性，避免闪烁）
# ---------------------------------------------------------------------------

def _head_section(src):
    """base.html 的 <head>…</head> 区段。引导脚本契约要求落在这个区段里。"""
    m = re.search(r'<head>.*?</head>', src, re.S)
    assert m, 'base.html 里找不到 <head>…</head>——本测试已失效'
    return m.group(0)


def test_base_head_inline_bootstrap_reads_tf_theme_from_localstorage():
    """引导脚本必须读 localStorage key tf-theme（缺省 dark）。

    key 名是契约承重位：theme.js、引导脚本、以及任何将来清理偏好的代码
    都认这一个名字，写错一处就会出现「设置了但不生效 / 生效了但下次丢失」。
    """
    head = _head_section(_base_html())
    assert 'localStorage' in head, (
        '<head> 引导脚本没有读 localStorage——偏好无法跨会话保持'
    )
    assert 'tf-theme' in head, (
        '<head> 引导脚本没有读 localStorage key "tf-theme"（契约 key 名）'
    )


def test_base_head_inline_bootstrap_knows_all_three_mode_values():
    """引导脚本必须支持 dark / light / system 三个模式值。"""
    head = _head_section(_base_html())
    for value in THEME_VALUES:
        assert re.search(r'["\']' + value + r'["\']', head), (
            f'<head> 引导脚本里找不到模式值 {value!r}（契约三值：'
            'dark / light / system）'
        )


def test_base_head_inline_bootstrap_evaluates_prefers_color_scheme():
    """system 模式必须经 matchMedia('(prefers-color-scheme: light)') 求值。"""
    head = _head_section(_base_html())
    assert 'matchMedia' in head, (
        '<head> 引导脚本没有调用 matchMedia——system 模式无从求值'
    )
    assert 'prefers-color-scheme' in head, (
        '<head> 引导脚本没有查 prefers-color-scheme——'
        '跟随系统必须读系统配色媒体查询'
    )


# 「写 documentElement 的 data-bs-theme」的两种等价合法写法：
#   el.setAttribute('data-bs-theme', v)  或  el.dataset.bsTheme = v
# （dataset.bsTheme 映射的就是 data-bs-theme 属性，契约守的是属性名与
# 写入位置，不是某个 API 调用形态。）
_WRITES_DATA_BS_THEME = r"(?:setAttribute\(\s*[\"']data-bs-theme[\"']|dataset\.bsTheme\b)"


def test_base_head_inline_bootstrap_sets_data_bs_theme_on_document_element():
    """引导脚本必须把求值结果写到 documentElement 的 data-bs-theme 属性上。

    ``<html data-bs-theme>`` 是全站唯一开关（Bootstrap 5.3 原生响应，
    style.css 的 ``[data-bs-theme="light"]`` 令牌覆盖块也挂它）。
    setAttribute('data-bs-theme', …) 与 dataset.bsTheme = … 都合法。
    """
    head = _head_section(_base_html())
    assert 'documentElement' in head, (
        '<head> 引导脚本没有操作 document.documentElement'
    )
    assert re.search(_WRITES_DATA_BS_THEME, head), (
        '<head> 引导脚本没有写 data-bs-theme 属性（setAttribute 或 '
        'dataset.bsTheme 均可）——data-bs-theme 是唯一开关，首帧前必须同步落上它'
    )


# ---------------------------------------------------------------------------
# 2) <html> 标签保留 data-bs-theme="dark" 字面量（SSR / 无 JS 回退默认）
# ---------------------------------------------------------------------------

def test_html_tag_keeps_dark_theme_literal_as_ssr_default():
    """<html> 上必须保留 data-bs-theme="dark" 字面量。

    无 JS / JS 未跑时的回退默认；tests/test_css_contract.py 也钉着它。
    引导脚本只能**改**这个属性的值，不能依赖「属性一开始不存在」。
    """
    m = re.search(r'<html\b[^>]*>', _base_html())
    assert m, 'base.html 里找不到 <html> 开标签——本测试已失效'
    assert 'data-bs-theme="dark"' in m.group(0), (
        '<html> 开标签丢了 data-bs-theme="dark" 字面量——'
        'SSR / 无 JS 回退默认必须是 dark'
    )


# ---------------------------------------------------------------------------
# 3) base.html 引入 static/js/theme.js
# ---------------------------------------------------------------------------

def test_base_html_loads_theme_js():
    """base.html 必须引入 static/js/theme.js（全部页面共用同一套主题逻辑）。"""
    src = _base_html()
    assert re.search(r"""js/theme\.js""", src), (
        'base.html 没有引入 static/js/theme.js'
    )
    assert re.search(r'<script[^>]+src=[^>]*theme\.js', src), (
        'theme.js 必须通过 <script src> 加载，不能内联或拼进别的包'
    )


def test_theme_js_is_actually_served_on_config_page(monkeypatch, tmp_path):
    """渲染级：/config 页面真的带上了 theme.js 的 <script> 标签。"""
    client = _load_app(monkeypatch, tmp_path)
    html = client.get('/config').get_data(as_text=True)
    assert re.search(r'<script[^>]+src="[^"]*js/theme\.js[^"]*"', html), (
        '/config 渲染结果里没有 theme.js 的 <script src>——'
        '切换 UI 在配置页，这页没有主题逻辑等于功能没接上'
    )


# ---------------------------------------------------------------------------
# 4) theme.js 源码：三模式、matchMedia change 监听、localStorage 持久化
# ---------------------------------------------------------------------------

def test_theme_js_supports_all_three_modes():
    """theme.js 必须支持 dark / light / system 三个模式值。"""
    src = _theme_js()
    for value in THEME_VALUES:
        assert re.search(r'["\']' + value + r'["\']', src), (
            f'theme.js 里找不到模式值 {value!r}（契约三值：dark / light / system）'
        )


def test_theme_js_listens_to_prefers_color_scheme_change():
    """theme.js 必须监听 prefers-color-scheme 媒体查询的 change 事件。

    「跟随系统」不是一次性求值：用户在系统设置里切深浅色时页面要实时
    跟着变，不加 change 监听就只有刷新才生效。
    """
    src = _theme_js()
    assert 'matchMedia' in src and 'prefers-color-scheme' in src, (
        'theme.js 没有 matchMedia(prefers-color-scheme)——system 模式无从求值'
    )
    assert re.search(r'addEventListener\(\s*["\']change["\']', src), (
        'theme.js 没有给媒体查询挂 change 事件监听——'
        '系统配色变化时页面不会实时跟随（契约要求监听 change）'
    )


def test_theme_js_persists_preference_to_localstorage():
    """theme.js 必须把用户选择写回 localStorage key tf-theme。"""
    src = _theme_js()
    assert 'tf-theme' in src, (
        'theme.js 里没有 localStorage key "tf-theme"（契约 key 名）'
    )
    assert re.search(r'localStorage[.\[]', src) and 'setItem' in src, (
        'theme.js 没有 localStorage.setItem——用户的选择无法跨会话保持'
    )


def test_theme_js_writes_data_bs_theme_on_document_element():
    """theme.js 切主题时写的也必须是 documentElement 的 data-bs-theme。

    与引导脚本同一个开关：两处写不同地方会出现「首帧一套、切完一套」。
    """
    src = _theme_js()
    assert 'documentElement' in src, (
        'theme.js 没有操作 document.documentElement'
    )
    assert re.search(_WRITES_DATA_BS_THEME, src), (
        'theme.js 没有写 data-bs-theme 属性（setAttribute 或 '
        'dataset.bsTheme 均可）——data-bs-theme 是唯一开关'
    )


@pytest.mark.skipif(shutil.which('node') is None, reason='node 不可用，跳过 JS 语法校验')
def test_theme_js_passes_node_syntax_check():
    """static/js/theme.js 必须通过 node --check 语法校验（项目 JS 验证套路）。"""
    assert os.path.exists(THEME_JS), 'static/js/theme.js 不存在——实现未落地'
    subprocess.run(
        ['node', '--check', THEME_JS],
        capture_output=True, text=True, check=True, timeout=30,
    )


# ---------------------------------------------------------------------------
# 5) _config_content.html 的「外观」切换 UI（源码级 + 渲染级）
# ---------------------------------------------------------------------------

def test_config_partial_has_appearance_section_with_three_buttons():
    """_config_content.html 必须有「外观」分区与三枚带可见文字的按钮。

    三枚按钮的可见文字分别是 暗黑 / 明亮 / 跟随系统——纯图标按钮不行，
    契约明确要求文字标签。
    """
    src = _read(CONFIG_PARTIAL)
    assert '外观' in src, (
        '_config_content.html 缺「外观」分区——主题切换 UI 应落在配置面板'
    )
    for label in THEME_BUTTON_LABELS:
        assert label in src, (
            f'_config_content.html 缺 {label!r} 按钮文字'
        )
        assert re.search(
            r'<button\b[^>]*>(?:(?!</button>).)*' + re.escape(label) + r'(?:(?!</button>).)*</button>',
            src, re.S,
        ), (
            f'{label!r} 不在任何 <button> 元素内——'
            '契约要求三枚**带可见文字**的按钮，不是图标或裸链接'
        )


def test_config_page_renders_appearance_switcher(monkeypatch, tmp_path):
    """渲染级：/config 页面真的渲染出「外观」分区与三枚按钮。"""
    client = _load_app(monkeypatch, tmp_path)
    html = client.get('/config').get_data(as_text=True)
    assert '外观' in html, '/config 渲染结果缺「外观」分区'
    for label in THEME_BUTTON_LABELS:
        assert re.search(
            r'<button\b[^>]*>(?:(?!</button>).)*' + re.escape(label) + r'(?:(?!</button>).)*</button>',
            html, re.S,
        ), (
            f'/config 渲染结果里 {label!r} 不在任何 <button> 元素内'
        )
