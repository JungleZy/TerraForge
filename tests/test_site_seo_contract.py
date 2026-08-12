"""官网 SEO / GEO 契约测试（site/ 是纯静态站，没有运行期可测的行为）。

守住的是「机器读这个站时拿到的事实」这一层，它比版面更容易在无人察觉的情况下
腐烂：改一句文案忘了重新生成英文页、发版忘了改 JSON-LD 里的 softwareVersion、
换域名漏掉 sitemap —— 这些都不会让页面看起来有问题，但会让搜索引擎与生成式
引擎拿到互相打架的两份说法。

三条主要不变量：

1. `site/en/index.html` 是 `site/index.html` + `scripts/site_i18n.json` 生成的，
   必须与源同步（`build_site_en.py --check` 那条路径）。
2. 中文字典与中文页逐字一致 —— 用 zh 字典跑一遍翻译必须还原出页面本身。
   这条一红，说明有人只改了 HTML 没改字典，英文页会悄悄留着旧句子。
3. 版本号、canonical / hreflang、JSON-LD、sitemap、llms.txt 之间不许有第二个
   事实源。
"""

import importlib.util
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, 'site')
BASE = 'https://terraforge-gis.pages.dev/'
EN_URL = BASE + 'en/'

# 中文页与英文页在磁盘上的相对路径，以及它们各自应有的 canonical 与 <html lang>。
PAGES = {
    'zh': (os.path.join(SITE, 'index.html'), BASE, 'zh-CN'),
    'en': (os.path.join(SITE, 'en', 'index.html'), EN_URL, 'en'),
}

CJK = re.compile(r'[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _builder():
    """按路径加载 scripts/build_site_en.py（scripts/ 不是包）。"""
    path = os.path.join(ROOT, 'scripts', 'build_site_en.py')
    spec = importlib.util.spec_from_file_location('build_site_en', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dicts():
    return json.loads(_read(os.path.join(ROOT, 'scripts', 'site_i18n.json')))


def _app_version():
    """单一事实源：src/core/config.py 的 Config.APP_VERSION。"""
    m = re.search(r"APP_VERSION\s*=\s*'([0-9.]+)'",
                  _read(os.path.join(ROOT, 'src', 'core', 'config.py')))
    assert m, 'src/core/config.py 中未找到 Config.APP_VERSION'
    return m.group(1)


def _norm(html):
    """折叠空白后比较：翻译落地是单行，源文件里同一句可能跨行缩进。"""
    html = re.sub(r'\s+', ' ', html)
    html = re.sub(r' *> *', '>', html)
    html = re.sub(r' *< *', '<', html)
    return html.strip()


def _jsonld(html):
    m = re.search(r'<script type="application/ld\+json">\n(.*?)\n</script>', html, re.S)
    assert m, '页面缺少 JSON-LD'
    return json.loads(m.group(1))


def _node(graph, typ):
    hits = [n for n in graph['@graph'] if n['@type'] == typ]
    assert len(hits) == 1, f'JSON-LD 里 {typ} 节点应恰好一个，实际 {len(hits)}'
    return hits[0]


def _meta(html, selector_attr, name):
    m = re.search(r'<meta %s="%s"[^>]*\scontent="([^"]*)"' % (selector_attr, re.escape(name)),
                  html)
    assert m, f'页面缺少 meta {selector_attr}={name}'
    return m.group(1)


# ── 生成物与源的同步 ─────────────────────────────────────────────────────

def test_en_page_is_generated_from_the_current_source():
    """英文页必须是当前 index.html + 字典生成出来的那一份。

    这条红了不是要你手改 site/en/index.html（它头两行就写着 DO NOT EDIT），
    而是跑 `uv run python scripts/build_site_en.py`。
    """
    want = _builder().build()
    have = _read(PAGES['en'][0])
    assert have == want, ('site/en/index.html 与源不同步，'
                          '跑 `uv run python scripts/build_site_en.py` 重新生成')


def test_zh_dictionary_reproduces_the_chinese_page_verbatim():
    """用 zh 字典翻译中文页 = 中文页本身（只差空白折叠）。

    往返成立才说明「字典里的 zh 值」与「页面上的原文」是同一句话；不成立就意味着
    英文页那一侧还挂着某句已经被改掉的旧中文的译文。
    """
    mod = _builder()
    src = _read(PAGES['zh'][0])
    assert _norm(mod.translate(src, _dicts()['zh'])) == _norm(src)


def test_both_dictionaries_cover_exactly_the_same_keys():
    d = _dicts()
    assert set(d['zh']) == set(d['en']), '两侧字典键不一致：' + str(
        sorted(set(d['zh']) ^ set(d['en'])))


def test_no_dead_or_undeclared_translation_keys():
    """字典里的键与页面上的标注必须一一对应。"""
    src = _read(PAGES['zh'][0])
    used = set(re.findall(r'data-i18n(?:-html)?="([^"]+)"', src))
    for spec in re.findall(r'data-i18n-attr="([^"]+)"', src):
        for pair in spec.split(','):
            used.add(pair.split(':', 1)[1].strip())
    declared = set(_dicts()['zh'])
    assert not (used - declared), '页面用了字典里没有的键：' + str(sorted(used - declared))
    assert not (declared - used), '字典里有页面不再使用的键：' + str(sorted(declared - used))


# ── 运行期 i18n 已经拆干净 ───────────────────────────────────────────────

def test_runtime_language_switching_is_gone():
    """语言切换必须是两条真链接，不是运行期 DOM 替换。

    运行期替换那套对 SEO 是净损失：英文内容没有自己的 URL，而 Googlebot 的
    navigator.language 是 en-US —— 同一个 URL 抓到的是英文、源码与 canonical
    说的是中文。这条测试钉住不要退回去。
    """
    assert not os.path.exists(os.path.join(SITE, 'assets', 'i18n.js')), \
        'site/assets/i18n.js 应已删除（译文搬到 scripts/site_i18n.json）'
    for lang, (path, _, _) in PAGES.items():
        # 注释里会提到这些名字（解释为什么不再用它们），只看真标记。
        html = re.sub(r'<!--.*?-->', '', _read(path), flags=re.S)
        assert not re.search(r'src="[^"]*i18n\.js"', html), f'{lang} 页仍在加载 i18n.js'
        assert 'i18n-pending' not in html, f'{lang} 页仍有防闪烁遗留'
        assert 'aria-pressed' not in html, f'{lang} 页语言切换仍是按钮语义'
    css = _read(os.path.join(SITE, 'assets', 'style.css'))
    assert 'i18n-pending' not in css, 'style.css 仍留着 .i18n-pending 规则'


@pytest.mark.parametrize('lang', sorted(PAGES))
def test_language_switcher_links_to_both_urls(lang):
    # 注释里也写着 aria-current（解释为什么用它），计数前先把注释摘掉。
    html = re.sub(r'<!--.*?-->', '', _read(PAGES[lang][0]), flags=re.S)
    assert '<a class="lang-btn" data-lang="zh"' in html
    assert '<a class="lang-btn" data-lang="en"' in html
    assert html.count('aria-current="page"') == 1, '当前语言必须且只能标一个'
    current = re.search(r'<a class="lang-btn" data-lang="(\w+)"[^>]*aria-current="page"', html)
    assert current and current.group(1) == lang, f'{lang} 页标错了当前语言'


def test_english_page_carries_no_leftover_chinese():
    """英文页除了语言切换里的「中文」两个字，不该有任何中文。"""
    html = _read(PAGES['en'][0])
    html = html.replace('>中文</a>', '></a>')
    leftovers = sorted(set(CJK.findall(html)))
    assert not leftovers, '英文页残留中文字符：' + ''.join(leftovers)


# ── 索引指令 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('lang', sorted(PAGES))
def test_canonical_lang_and_hreflang(lang):
    path, url, html_lang = PAGES[lang]
    html = _read(path)
    assert f'<html lang="{html_lang}">' in html
    assert f'<link rel="canonical" href="{url}">' in html, f'{lang} 页 canonical 应指向自己'
    # 每页都要列全三条 alternate（含指向自己那条），否则关系不成立。
    alts = dict(re.findall(r'<link rel="alternate" hreflang="([\w-]+)" href="([^"]+)">', html))
    assert alts == {'zh-CN': BASE, 'en': EN_URL, 'x-default': BASE}, alts
    robots = _meta(html, 'name', 'robots')
    assert 'index' in robots and 'max-image-preview:large' in robots


@pytest.mark.parametrize('lang', sorted(PAGES))
def test_open_graph_is_complete(lang):
    path, url, _ = PAGES[lang]
    html = _read(path)
    assert f'<meta property="og:url" content="{url}">' in html
    for prop in ('og:type', 'og:site_name', 'og:title', 'og:description',
                 'og:image', 'og:image:width', 'og:image:height', 'og:image:alt',
                 'og:locale', 'og:locale:alternate'):
        assert _meta(html, 'property', prop), f'{lang} 页缺 {prop}'
    locale = _meta(html, 'property', 'og:locale')
    alt = _meta(html, 'property', 'og:locale:alternate')
    assert {locale, alt} == {'zh_CN', 'en_US'} and locale != alt


def test_sitemap_matches_the_pages_on_disk():
    xml = _read(os.path.join(SITE, 'sitemap.xml'))
    locs = re.findall(r'<loc>([^<]+)</loc>', xml)
    assert locs == [BASE, EN_URL], locs
    # 每条 <url> 的 alternate 组必须与页面 <head> 里的那组一模一样。
    for block in re.findall(r'<url>(.*?)</url>', xml, re.S):
        alts = dict(re.findall(r'hreflang="([\w-]+)" href="([^"]+)"', block))
        assert alts == {'zh-CN': BASE, 'en': EN_URL, 'x-default': BASE}, alts
        assert re.search(r'<lastmod>\d{4}-\d{2}-\d{2}</lastmod>', block), '缺 lastmod'


def test_robots_allows_crawling_and_points_at_the_sitemap():
    txt = _read(os.path.join(SITE, 'robots.txt'))
    assert re.search(r'^User-agent: \*$', txt, re.M)
    assert re.search(r'^Allow: /$', txt, re.M)
    assert f'Sitemap: {BASE}sitemap.xml' in txt
    assert 'Disallow:' not in txt, '全站可抓，不该出现 Disallow'
    # 生成式引擎的 UA 组：只认自己名字的那几个必须被明确点名。
    for ua in ('GPTBot', 'ClaudeBot', 'PerplexityBot', 'Google-Extended',
               'Applebot-Extended', 'OAI-SearchBot'):
        assert re.search(r'^User-agent: %s$' % re.escape(ua), txt, re.M), f'robots 未点名 {ua}'


# ── 结构化数据与版本号 ───────────────────────────────────────────────────

@pytest.mark.parametrize('lang', sorted(PAGES))
def test_jsonld_agrees_with_the_page_it_sits_on(lang):
    path, url, html_lang = PAGES[lang]
    html = _read(path)
    graph = _jsonld(html)
    app = _node(graph, 'SoftwareApplication')
    assert app['@id'] == url + '#app'
    assert app['url'] == url
    assert app['inLanguage'] == html_lang
    # 结构化数据与 meta description 打架时爬虫按哪份走没有保证 —— 不许打架。
    assert app['description'] == _meta(html, 'name', 'description')
    assert app['softwareVersion'] == _app_version()
    assert app['downloadUrl'].endswith('v' + _app_version())
    assert app['offers']['price'] == '0' and app['isAccessibleForFree'] is True
    assert app['license'].startswith('https://opensource.org/licenses/MIT')
    # 站点与作者的身份两页共用同一个 @id，不跟着页面语言分叉。
    assert _node(graph, 'WebSite')['@id'] == BASE + '#website'
    assert _node(graph, 'Person')['@id'] == BASE + '#author'
    assert app['author']['@id'] == BASE + '#author'


def test_jsonld_feature_list_follows_the_page_language():
    mod = _builder()
    d = _dicts()
    for lang, (path, _, _) in PAGES.items():
        app = _node(_jsonld(_read(path)), 'SoftwareApplication')
        assert app['featureList'] == [d[lang][k] for k in mod.FEATURE_KEYS], \
            f'{lang} 页 JSON-LD 的 featureList 与页面上的四条管线标题不一致'


def test_version_number_is_the_same_everywhere_on_the_site():
    """站上出现的三段式版本号只允许有一个值 —— 就是 Config.APP_VERSION。

    发版漏改的经典表现：hero 按钮写新版本，Release 下载链接还指向旧 tag。
    """
    version = _app_version()
    targets = [PAGES['zh'][0], PAGES['en'][0],
               os.path.join(SITE, 'llms.txt'),
               os.path.join(SITE, 'llms-full.txt'),
               os.path.join(ROOT, 'scripts', 'site_i18n.json')]
    for path in targets:
        # 前后不许再接数字或点 —— 否则 0.0.0.0:5000 里的 IP 会被当成版本号。
        found = set(re.findall(r'(?<![\d.])v?(\d+\.\d+\.\d+)(?![\d.])', _read(path)))
        rel = os.path.relpath(path, ROOT)
        assert found, f'{rel} 里没有版本号 —— 是不是被改没了？'
        assert found == {version}, f'{rel} 里的版本号 {sorted(found)} 与 APP_VERSION {version} 不一致'


# ── GEO：给生成式引擎的纯文本 ────────────────────────────────────────────

def test_llms_txt_follows_the_convention():
    txt = _read(os.path.join(SITE, 'llms.txt'))
    lines = txt.splitlines()
    assert lines[0] == '# TerraForge', 'llms.txt 首行必须是 H1 项目名'
    assert any(l.startswith('> ') for l in lines[:6]), 'H1 之后要有一段 > 摘要'
    assert f'{BASE}llms-full.txt' in txt, 'llms.txt 要指向 llms-full.txt'
    for url in (BASE, EN_URL, 'https://github.com/JungleZy/TerraForge'):
        assert url in txt


def test_llms_full_txt_states_the_load_bearing_facts():
    """全文版是要被原样引用的，几条最容易被模型说错的事实必须在里面。"""
    txt = _read(os.path.join(SITE, 'llms-full.txt'))
    for fact in ('quantized-mesh 1.0', 'Copernicus GLO-30', 'GEBCO 2024 Grid',
                 'MIT', 'Apple Silicon', 'no login or authentication',
                 'XYZ tile directory'):
        assert fact in txt, f'llms-full.txt 缺少关键事实：{fact}'
    assert 'http://localhost:5000' in txt


def test_llms_files_are_served_as_utf8_plain_text():
    headers = _read(os.path.join(SITE, '_headers'))
    for name in ('/llms.txt', '/llms-full.txt'):
        block = re.search(r'^%s\n((?:  .*\n)+)' % re.escape(name), headers, re.M)
        assert block, f'_headers 缺少 {name} 的规则'
        assert 'Content-Type: text/plain; charset=utf-8' in block.group(1)


# ── 站内引用的文件真的存在 ───────────────────────────────────────────────

@pytest.mark.parametrize('lang', sorted(PAGES))
def test_every_local_asset_reference_resolves(lang):
    path, _, _ = PAGES[lang]
    html = _read(path)
    refs = set(re.findall(r'(?:href|src)="(/?assets/[^"#?]+)"', html))
    assert refs, '页面一个本地资源都没引用？'
    for ref in refs:
        target = os.path.join(SITE, ref.lstrip('/'))
        assert os.path.exists(target), f'{lang} 页引用了不存在的文件：{ref}'


@pytest.mark.parametrize('lang', sorted(PAGES))
def test_declared_image_sizes_match_the_files(lang):
    """<img> 上的 width/height 必须等于图片真实像素尺寸。

    这两个属性是浏览器在图片到货前占位用的宽高比。换了截图不改数字，比例一变
    就是一次可见的版面跳动（CLS），而它算进 Core Web Vitals。
    """
    from PIL import Image

    path, _, _ = PAGES[lang]
    imgs = re.findall(r'<img src="(/?assets/img/[^"]+)" width="(\d+)" height="(\d+)"',
                      _read(path))
    assert len(imgs) == 6, f'{lang} 页图片数变了（{len(imgs)}），核对一下再改这条断言'
    for src, w, h in imgs:
        with Image.open(os.path.join(SITE, src.lstrip('/'))) as im:
            assert im.size == (int(w), int(h)), \
                f'{lang} 页 {src} 声明 {w}×{h}，文件实际 {im.size[0]}×{im.size[1]}'


def test_english_page_uses_english_screenshots_where_available():
    """有英文版截图就必须用上 —— 英文页配中文界面的图，说服力直接归零。"""
    en_dir = os.path.join(SITE, 'assets', 'img', 'en')
    available = {f for f in os.listdir(en_dir) if f.endswith('.webp')}
    assert available, 'site/assets/img/en/ 空了？'
    used = set(re.findall(r'src="/assets/img/(?:en/)?([^"/]+)"', _read(PAGES['en'][0])))
    for name in sorted(available & used):
        assert f'src="/assets/img/en/{name}"' in _read(PAGES['en'][0]), \
            f'英文页还在用中文截图 {name}，跑一次 build_site_en.py'


@pytest.mark.parametrize('lang', sorted(PAGES))
def test_jsonld_screenshot_is_the_hero_image_of_that_page(lang):
    """JSON-LD 的 screenshot 与页面首屏那张必须是同一张（英文页用英文截图）。"""
    html = _read(PAGES[lang][0])
    hero = re.search(r'<figure class="shot-hero">\s*<img src="(/?assets/img/[^"]+)"', html)
    assert hero, f'{lang} 页找不到首屏截图'
    shot = _node(_jsonld(html), 'SoftwareApplication')['screenshot']
    assert shot == BASE + hero.group(1).lstrip('/'), \
        f'{lang} 页 JSON-LD 的 screenshot（{shot}）与首屏截图不是同一张'
