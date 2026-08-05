"""界面语言（zh / en）契约。

机制见 src/i18n/__init__.py。这里钉三层：
1. 目录本身的完整性（key 唯一、每条都有两种语言）——漏翻在测试期就红，
   而不是让用户在英文界面上看到一句中文；
2. 语种解析（cookie → 校验 → 缺省 zh）与占位符；
3. 端到端：同一个页面，带不带 cookie 渲染出的确实是两种语言，且**中文那份
   与改造前逐字一致**（大量既有测试断言中文原文，这条是它们的护栏）。
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.i18n import (CLIENT_PREFIX, DEFAULT_LOCALE, LOCALES, client_catalog,
                      normalize_locale, t)
from src.i18n.catalog import MESSAGES

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_HAN = re.compile(r'[\u4e00-\u9fff]')


# ---------------------------------------------------------------- 目录完整性

def test_every_key_has_both_locales():
    """缺一种语言就等于界面上会漏出另一种语言的字。"""
    missing = {
        key: [loc for loc in LOCALES if not entry.get(loc)]
        for key, entry in MESSAGES.items()
        if any(not entry.get(loc) for loc in LOCALES)
    }
    assert not missing, f'以下 key 缺语种: {missing}'


def test_placeholders_match_across_locales():
    """占位符两边必须完全一致 —— 少一个就是 KeyError/漏值，多一个就是原样漏出。"""
    bad = {}
    for key, entry in MESSAGES.items():
        names = {loc: set(re.findall(r'\{(\w+)\}', entry[loc])) for loc in LOCALES}
        if len(set(map(frozenset, names.values()))) > 1:
            bad[key] = names
    assert not bad, f'占位符不一致: {bad}'


def test_english_values_are_not_chinese():
    """en 一栏必须真的翻过 —— 直接抄中文过去是本次改造最容易偷懒的地方。

    例外：产品名、语言自称（「中文」在英文界面上仍应显示为「中文」）。
    """
    allowed = {'app.suffix', 'app.language.zh', 'app.language.en'}
    leftovers = [
        key for key, entry in MESSAGES.items()
        if key not in allowed and _HAN.search(entry['en'])
    ]
    assert not leftovers, f'这些 key 的英文还是中文: {leftovers}'


# ---------------------------------------------------------------- 语种解析

@pytest.mark.parametrize('raw,expected', [
    ('zh', 'zh'),
    ('en', 'en'),
    ('EN', 'en'),
    ('zh-CN', 'zh'),      # 带地区的写法取主语言
    ('en-US', 'en'),
    ('fr', 'zh'),         # 不支持的语言回落缺省
    ('', 'zh'),
    (None, 'zh'),
    (123, 'zh'),          # 非字符串不能炸
])
def test_normalize_locale(raw, expected):
    assert normalize_locale(raw) == expected


def test_missing_key_returns_the_key_itself():
    """查不到时返回 key，界面上一眼能看出漏翻；静默回落中文反而藏问题。"""
    assert t('no.such.key') == 'no.such.key'


def test_placeholder_substitution():
    assert t('app.title', locale='zh') == 'TerraForge —— GIS 数据获取与加工'
    assert 'TerraForge' in t('app.title', locale='en')


def test_client_catalog_only_ships_browser_strings():
    """模板文案在服务端就渲染完了，再内联给浏览器是白付流量。"""
    catalog = client_catalog('zh')
    assert catalog, '客户端文案表为空 —— js.* 的 key 一条都没有？'
    assert all(k.startswith(CLIENT_PREFIX) for k in catalog)
    assert not any(k.startswith('tpl.') for k in catalog)


# ---------------------------------------------------------------- 端到端

def _client(monkeypatch, tmp_path):
    """Config 副作用重定向到 tmp_path，再新鲜 import app（项目统一套路）。"""
    import importlib

    from src.core import config
    monkeypatch.setattr(config.Config, 'DATABASE_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(config.Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(config.Config, 'CACHE_DIR', tmp_path / 'cache')
    for mod in ('app', 'src.core.database'):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module('app')
    app_mod.app.config['TESTING'] = True
    return app_mod.app.test_client()


def test_default_locale_is_chinese(monkeypatch, tmp_path):
    """没有 cookie 就是中文 —— 缺省语种不能靠浏览器 Accept-Language 猜。"""
    html = _client(monkeypatch, tmp_path).get('/').get_data(as_text=True)
    assert '<title>TerraForge —— GIS 数据获取与加工</title>' in html
    assert 'lang="zh-CN"' in html
    assert 'window.__LANG__ = "zh"' in html


def test_english_cookie_switches_the_whole_page(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    client.set_cookie('tf-lang', 'en')
    html = client.get('/').get_data(as_text=True)
    assert 'lang="en"' in html
    assert 'window.__LANG__ = "en"' in html
    assert 'GIS Data Acquisition' in html


def test_bogus_cookie_falls_back_to_chinese(monkeypatch, tmp_path):
    """cookie 是用户可写的，垃圾值不能让页面变成半成品。"""
    client = _client(monkeypatch, tmp_path)
    client.set_cookie('tf-lang', 'klingon')
    html = client.get('/').get_data(as_text=True)
    assert 'lang="zh-CN"' in html
    assert 'window.__LANG__ = "zh"' in html


@pytest.mark.parametrize('path', ['/', '/history', '/config'])
def test_every_page_ships_the_client_catalog(monkeypatch, tmp_path, path):
    """i18n.js 必须在所有业务脚本之前加载，否则它们解析期调 t() 会炸。"""
    html = _client(monkeypatch, tmp_path).get(path).get_data(as_text=True)
    assert 'window.__I18N__' in html
    i18n_at = html.find('js/i18n.js')
    assert i18n_at != -1, f'{path} 没有加载 js/i18n.js'
    for later in ('js/ui.js', 'js/theme.js'):
        pos = html.find(later)
        if pos != -1:
            assert i18n_at < pos, f'{path} 的 {later} 排在 i18n.js 之前'


# ---------------------------------------------------------------- 覆盖率闸门

def _strip_comments(html):
    html = re.sub(r'\{#.*?#\}', '', html, flags=re.S)   # Jinja 注释
    html = re.sub(r'<!--.*?-->', '', html, flags=re.S)  # HTML 注释
    return html


def test_no_untranslated_chinese_left_in_templates():
    """模板里不该再有裸中文 —— 有就是漏翻，英文界面上会直接漏出来。

    只扫注释之外的部分：本项目的注释语言就是中文，那是给维护者看的。
    """
    tpl_dir = os.path.join(PROJECT_ROOT, 'templates')
    leftovers = {}
    for name in sorted(os.listdir(tpl_dir)):
        if not name.endswith('.html'):
            continue
        path = os.path.join(tpl_dir, name)
        with open(path, encoding='utf-8') as fh:
            body = _strip_comments(fh.read())
        hits = [
            f'{i}: {line.strip()[:80]}'
            for i, line in enumerate(body.splitlines(), 1)
            if _HAN.search(line)
        ]
        if hits:
            leftovers[name] = hits
    assert not leftovers, (
        '模板里还有没走 t() 的中文:\n'
        + '\n'.join(f'  {f}\n    ' + '\n    '.join(v) for f, v in leftovers.items())
    )
