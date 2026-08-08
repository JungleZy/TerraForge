"""界面语言（zh / en）契约。

机制见 src/i18n/__init__.py。这里钉四层：
1. 目录本身的完整性（key 唯一、每条都有两种语言）——漏翻在测试期就红，
   而不是让用户在英文界面上看到一句中文；
2. key ↔ 引用的**双向闭合**：引用了不存在的键、以及定义了没人用的键，两个
   方向都红。第 1 层全是「对已有键做检查」，删键/漏删引用一概不报警；
3. 语种解析（cookie → 校验 → 缺省 zh）与占位符；
4. 端到端：同一个页面，带不带 cookie 渲染出的确实是两种语言，且**中文那份
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


# ---------------------------------------------------------------- 双向闭合
#
# 为什么按「key 形状的字面量」扫而不是按 `t(` 调用点扫：取文案有三种形状 ——
# JS 的 `t('k')`、Jinja 的 `{{ t('k') }}`、Python 的 `t('k')` —— 但**键字面量出现在
# 源码里**才是三者唯一的共同特征。有一批键根本不紧跟在 `t(` 后面：
#   - static/js/config.js:246 的 `{ env: 'js.config.proxy.source_env', … }[source]`
#   - src/routes/api.py:874 的 `_ACTIVE_TASK_TABLES` 元组（`t(label_key)` 在 27 行外）
#   - static/js/map.js:1499 的同行三元 `t(cond ? 'a' : 'b')`
#   - src/services/tile_url_probe.py:372 跨行三元的 else 分支
# 实测：按 `t(` 扫只够得到 466 个键里的 457 个，**方向二会凭空多出 9 个假孤儿**
# （上面四处共 9 个键）。那不是少一层保护，是会诱导下一个人真去删掉在用的键。
#
# 代价：任何 key 形状的字面量都算引用。api/app/js/tpl/val 都是普通英文词，
# `'app.py'`、`'api.v1.users'` 这种普通常量一样会命中 —— 今天没炸只是因为
# nuitka_build.py（`ENTRY='app.py'`）恰好在扫描面外，是**边界的运气不是正则的
# 性质**。伤到的只有方向一，逃生口是下面的 `_NOT_A_KEY`；方向二反而需要这份
# 宽松，收紧正则就是上面那 9 个假孤儿。
#
# 今天的实测口径：467 个不同字面量 / 531 处出现；467 - 1（下面登记的拼接前缀）
# = 466 = MESSAGES 全量 —— 键与引用严格双射。

_CATALOG_DIR = os.path.join(PROJECT_ROOT, 'src', 'i18n', 'catalog')

# 域名单从 MESSAGES 现算，不写死 —— 写死就是第二份目录知识，迟早跟 catalog 漂。
_KEY_LITERAL = re.compile(
    r"""['"]((?:%s)(?:\.[A-Za-z0-9_]+)+)['"]"""
    % '|'.join(sorted({key.split('.')[0] for key in MESSAGES}))
)

# 运行时才拼出完整键的地方。
#
# **往这张表加东西请照抄下面的体例：前缀 → 完整后缀清单。**
# 不许退化成前缀通配 / startswith 一拳打死。理由：清单展开出的键一边算「已引用」，
# 一边被 test_dynamic_key_sites_expand_to_real_keys 从**两头**反过来断言 —— 拼接点
# 还在源码里、展开出的键还在 catalog 里。这张表因此不是消音器，而是一条**额外**的
# 活契约：谁删了 js.map.bounds.sr_east、或者重构掉了那个拼接点，红的都是这张表
# 本身，且失败信息直接点到位。换成通配就会被整片吞掉，拼接点在运行时把键名原样
# 显示给用户，而测试一片绿。
_DYNAMIC_KEY_SITES = {
    # static/js/map.js:_renderManualBounds —— 手动录入四个边界时，输入框的
    # aria-label 由 `t('js.map.bounds.sr_' + field)` 拼出，field 只可能是这四个方向。
    'js.map.bounds.sr_': ('north', 'south', 'east', 'west'),
}


# 长得像 i18n 键、但其实不是的普通字符串常量。
#
# 只对方向一开口（方向二不需要：它本来就只认 catalog 里有的键）。
# _KEY_LITERAL 的域名单 api/app/js/tpl/val 全是普通英文词，`'app.py'`、
# `'app.config'`、`'api.v1.users'`、`'js.min'` 这类常量一样会命中。
# **撞上了往这里加，别去动 _KEY_LITERAL** —— 把正则收紧到只认 `t(` 调用点，
# 方向二会凭空多出 9 个假孤儿（见本节开头），那一侧的代价大得多。
_NOT_A_KEY = frozenset()


def _iter_source_files():
    """扫描面：产品代码。

    刻意不含两处：catalog 自身是定义端，算进去每个键都会「自证被引用」；
    tests 算进去等于让测试给测试盖章 —— 只被测试引用的键就是死重量。
    """
    for root, suffix in (
        (os.path.join(PROJECT_ROOT, 'static', 'js'), '.js'),
        (os.path.join(PROJECT_ROOT, 'templates'), '.html'),
        (os.path.join(PROJECT_ROOT, 'src'), '.py'),
    ):
        for dirpath, dirnames, filenames in os.walk(root):
            if os.path.abspath(dirpath).startswith(_CATALOG_DIR + os.sep):
                dirnames[:] = []
                continue
            dirnames[:] = sorted(d for d in dirnames if d != '__pycache__')
            for name in sorted(filenames):
                if name.endswith(suffix):
                    yield os.path.join(dirpath, name)
    yield os.path.join(PROJECT_ROOT, 'app.py')


def _scan_key_literals(paths):
    """key 形状的字面量 -> [(相对路径:行号)]，行号是为了让失败信息能直接跳过去。"""
    found = {}
    for path in paths:
        rel = os.path.relpath(path, PROJECT_ROOT).replace(os.sep, '/')
        with open(path, encoding='utf-8') as fh:
            for lineno, line in enumerate(fh, 1):
                for key in _KEY_LITERAL.findall(line):
                    found.setdefault(key, []).append(f'{rel}:{lineno}')
    return found


def _catalog_definitions():
    """key -> 定义它的 catalog 文件:行号。删孤儿键时不用再自己 grep。"""
    paths = [os.path.join(_CATALOG_DIR, n)
             for n in sorted(os.listdir(_CATALOG_DIR)) if n.endswith('.py')]
    return {k: v[0] for k, v in _scan_key_literals(paths).items()}


def test_dynamic_key_sites_expand_to_real_keys():
    """动态拼接表两头都得对得上 —— 对不上它就成了一张过期的免死金牌。

    两头指：拼接点还在源码里（否则这条豁免白吊着一批键，孤儿检查从此抓不到
    它们，而三条用例全绿）、展开出的键确实在 catalog 里。
    """
    sanity = _catalog_definitions()
    assert sanity, '定义端一个键都扫不出来，_KEY_LITERAL 或 _CATALOG_DIR 写错了'

    literals = _scan_key_literals(_iter_source_files())
    stale = [prefix for prefix in _DYNAMIC_KEY_SITES if prefix not in literals]
    assert not stale, (
        '_DYNAMIC_KEY_SITES 登记的拼接点在源码里已经找不到了 —— 这条豁免会白白\n'
        '吊着它展开的那些键，孤儿检查从此抓不到它们。删掉这条:\n  '
        + '\n  '.join(stale)
    )

    bad = [
        f'{prefix}{suffix}（登记在 _DYNAMIC_KEY_SITES 的 {prefix!r} 一条下）'
        for prefix, suffixes in _DYNAMIC_KEY_SITES.items()
        for suffix in suffixes
        if prefix + suffix not in MESSAGES
    ]
    assert not bad, (
        '_DYNAMIC_KEY_SITES 展开出 catalog 里没有的键 —— 拼接点会在运行时把\n'
        '键名原样显示给用户。要么把键补回 catalog，要么改拼接点、同步这张表:\n  '
        + '\n  '.join(bad)
    )


def test_no_reference_to_a_missing_key():
    """引用了不存在的键 = 用户看到 `js.foo.bar` 这种原始键名（见 t() 的回落口径）。

    砍「取消任务」时删掉 9 个 catalog 键，全靠人工 grep 确认没漏引用，测试
    全程没出过声。这一侧就是为那次改动补的。
    """
    missing = []
    for key, sites in sorted(_scan_key_literals(_iter_source_files()).items()):
        if key in MESSAGES or key in _DYNAMIC_KEY_SITES or key in _NOT_A_KEY:
            continue
        missing.append(f'{key}\n      ' + '\n      '.join(sites))
    assert not missing, (
        '这些键被引用但 catalog 里没有 —— 界面上会原样漏出键名。\n'
        '修法三选一：补进 src/i18n/catalog/ 对应模块；删掉这些引用处；\n'
        '若它是动态拼接的前缀，登记进本文件的 _DYNAMIC_KEY_SITES。\n'
        '第四种可能：它根本不是 i18n 键（只是长得像的普通常量）—— 登记进\n'
        '本文件的 _NOT_A_KEY，别去改 _KEY_LITERAL。\n  '
        + '\n  '.join(missing)
    )


def test_no_unreferenced_catalog_key():
    """定义了没人用的键 = 死重量，`js.` 那批还会被 client_catalog 白塞进每个页面。"""
    referenced = set(_scan_key_literals(_iter_source_files()))
    referenced.update(
        prefix + suffix
        for prefix, suffixes in _DYNAMIC_KEY_SITES.items()
        for suffix in suffixes
    )
    defined_at = _catalog_definitions()
    orphans = [
        f'{key}\n      定义于 {defined_at.get(key, "src/i18n/catalog/?")}'
        for key in sorted(set(MESSAGES) - referenced)
    ]
    assert not orphans, (
        '这些 catalog 键全仓无人引用 —— 删掉它们。\n'
        '若确属运行时拼接（错误码→文案之类），把拼接点连同**完整后缀清单**\n'
        '登记进本文件的 _DYNAMIC_KEY_SITES，并写清是哪一行在拼。\n  '
        + '\n  '.join(orphans)
    )


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
