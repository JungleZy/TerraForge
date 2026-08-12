#!/usr/bin/env python3
"""从中文页生成英文静态页 site/en/index.html。

为什么要有这个脚本
------------------
官网原来只有一份 HTML，英文靠 `assets/i18n.js` 在运行期替换文案。那套做法对
用户没问题，对搜索引擎和生成式引擎是净损失：

* 英文内容没有自己的 URL（`?lang=en` 只是同一个 URL 的运行期状态），
  hreflang 指过去的地址返回的仍是中文 HTML，这组关系搜索引擎不会认；
* 判定语言用的是 `navigator.language`，而 Googlebot 的 navigator.language 是
  en-US —— 抓取时页面被脚本换成英文，源码、`<html lang>`、canonical 说的却是
  中文。同一个 URL 两副面孔，这正是「内容不一致」类信号。

现在改成两个真实页面：`/` 中文、`/en/` 英文，语言切换是两条链接。英文页由这个
脚本生成，译文单一事实源是 `scripts/site_i18n.json`。

用法
----
    uv run python scripts/build_site_en.py            # 生成/更新 site/en/index.html
    uv run python scripts/build_site_en.py --check    # 只校验是否与源同步，不写盘

`--check` 是 tests/test_site_seo_contract.py 用的那条路径：改了 index.html 或
译文却忘了重新生成，测试会红。

工作方式
--------
`translate()` 只做一件事：把带 `data-i18n` / `data-i18n-html` /
`data-i18n-attr` 标注的元素内容或属性换成字典里的值。它对语言无感 —— 用 zh
字典跑一遍必须还原出源文件本身（只差空白折叠），这条往返等式由测试钉住，也就
钉住了「index.html 的文案与字典逐字一致」。

`localize_en()` 做剩下的页面级差异：`<html lang>`、canonical / og:url、
资源路径转根绝对、语言切换的选中态、JSON-LD 里 #app 那个节点、去掉中文注释。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_HTML = os.path.join(ROOT, 'site', 'index.html')
DICT_JSON = os.path.join(ROOT, 'scripts', 'site_i18n.json')
OUT_HTML = os.path.join(ROOT, 'site', 'en', 'index.html')

# HTML5 空元素：没有结束标签，因此不能承载 data-i18n 文本（只能挂 -attr）。
VOID = frozenset((
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
))

# JSON-LD 的 featureList 用四条管线的标题，跟着语言走。
FEATURE_KEYS = (
    'pipe.tiles.title',
    'pipe.dem.title',
    'pipe.terrain.title',
    'pipe.contour.title',
)

BANNER = (
    '<!-- GENERATED FILE - DO NOT EDIT.\n'
    '     Source: site/index.html + scripts/site_i18n.json\n'
    '     Regenerate: uv run python scripts/build_site_en.py -->'
)


def _escape_text(value: str) -> str:
    """写进元素文本的转义。`data-i18n-html` 的值是有意的标记，不走这里。"""
    return value.replace('&', '&amp;').replace('<', '&lt;')


def _escape_attr(value: str) -> str:
    return value.replace('&', '&amp;').replace('"', '&quot;')


class _Translator(HTMLParser):
    """按 data-i18n* 标注替换文案，其余字节原样透传。

    透传是刻意的：起始标签直接回写 `get_starttag_text()`，属性顺序、引号写法、
    注释、脚本体全部保持原样 —— 生成页与源页 diff 出来只有真正翻译的那些行。
    """

    def __init__(self, dic, strip_comments=False):
        super().__init__(convert_charrefs=False)
        self.dic = dic
        self.strip_comments = strip_comments
        self.out = []
        self.missing = []
        self._skip_tag = None   # 正在丢弃内容的元素名
        self._depth = 0         # 同名元素嵌套计数

    # ── 取值 ────────────────────────────────────────────────────────────
    def _value(self, key):
        if key not in self.dic:
            self.missing.append(key)
            return ''
        return self.dic[key]

    def _sub_attrs(self, raw, spec):
        """按 `attr:key,attr2:key2` 重写起始标签里的属性值。"""
        for pair in spec.split(','):
            attr, _, key = pair.strip().partition(':')
            new = _escape_attr(self._value(key))
            pattern = re.compile(r'(\s%s=")[^"]*(")' % re.escape(attr))
            raw, n = pattern.subn(lambda m: m.group(1) + new + m.group(2), raw, count=1)
            if n != 1:
                raise SystemExit(
                    'data-i18n-attr 指向了不存在的属性 %r：%s' % (attr, raw))
        return raw

    # ── 事件 ────────────────────────────────────────────────────────────
    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        if self._skip_tag is not None:
            if tag == self._skip_tag and tag not in VOID:
                self._depth += 1
            return
        a = dict(attrs)
        if 'data-i18n-attr' in a:
            raw = self._sub_attrs(raw, a['data-i18n-attr'])
        self.out.append(raw)

        key = a.get('data-i18n')
        raw_html = False
        if key is None:
            key = a.get('data-i18n-html')
            raw_html = key is not None
        if key is None:
            return
        if tag in VOID:
            raise SystemExit('空元素 <%s> 不能挂 data-i18n（只能挂 -attr）' % tag)
        value = self._value(key)
        self.out.append(value if raw_html else _escape_text(value))
        self._skip_tag = tag
        self._depth = 1

    def handle_startendtag(self, tag, attrs):
        # <x /> 写法：本项目不用，但别在遇到时把标签吃掉。
        if self._skip_tag is not None:
            return
        raw = self.get_starttag_text()
        a = dict(attrs)
        if 'data-i18n-attr' in a:
            raw = self._sub_attrs(raw, a['data-i18n-attr'])
        self.out.append(raw)

    def handle_endtag(self, tag):
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._depth -= 1
                if self._depth == 0:
                    self._skip_tag = None
                    self.out.append('</%s>' % tag)
            return
        self.out.append('</%s>' % tag)

    def handle_data(self, data):
        if self._skip_tag is None:
            self.out.append(data)

    def handle_comment(self, data):
        if self._skip_tag is None and not self.strip_comments:
            self.out.append('<!--%s-->' % data)

    def handle_decl(self, decl):
        if self._skip_tag is None:
            self.out.append('<!%s>' % decl)

    def handle_pi(self, data):
        if self._skip_tag is None:
            self.out.append('<?%s>' % data)

    def unknown_decl(self, data):
        if self._skip_tag is None:
            self.out.append('<![%s]>' % data)

    def handle_entityref(self, name):
        if self._skip_tag is None:
            self.out.append('&%s;' % name)

    def handle_charref(self, name):
        if self._skip_tag is None:
            self.out.append('&#%s;' % name)


def translate(html, dic, strip_comments=False):
    """把 html 里所有带 data-i18n* 标注的位置换成 dic 里的值。"""
    p = _Translator(dic, strip_comments=strip_comments)
    p.feed(html)
    p.close()
    if p.missing:
        raise SystemExit(
            'scripts/site_i18n.json 缺少这些键：%s' % ', '.join(sorted(set(p.missing))))
    return ''.join(p.out)


def _replace_once(text, old, new, what):
    if text.count(old) != 1:
        raise SystemExit('生成英文页失败：%s 期望恰好出现一次，实际 %d 次'
                         % (what, text.count(old)))
    return text.replace(old, new)


def base_url(src):
    m = re.search(r'<link rel="canonical" href="([^"]+)">', src)
    if not m:
        raise SystemExit('site/index.html 里找不到 canonical')
    return m.group(1)


def _has_english_image(name):
    return os.path.exists(os.path.join(ROOT, 'site', 'assets', 'img', 'en', name))


def _patch_jsonld(html, dic, base, en_url):
    m = re.search(r'(<script type="application/ld\+json">\n)(.*?)(\n</script>)',
                  html, re.S)
    if not m:
        raise SystemExit('site/index.html 里找不到 JSON-LD 块')
    graph = json.loads(m.group(2))
    hit = 0
    for node in graph['@graph']:
        # 只有 #app 这个节点是「这一页讲的这个软件」，跟着页面语言走；
        # WebSite 与 Person 是站点与作者的身份，两页共用同一个 @id。
        if node.get('@id') != base + '#app':
            continue
        hit += 1
        node['@id'] = en_url + '#app'
        node['url'] = en_url
        node['description'] = dic['meta.desc']
        node['inLanguage'] = 'en'
        node['featureList'] = [dic[k] for k in FEATURE_KEYS]
        # screenshot 是绝对 URL，不走页面里那轮相对路径改写，单独换。
        shot = re.match(r'(.*/assets/img/)([^/]+)$', node.get('screenshot', ''))
        if shot and _has_english_image(shot.group(2)):
            node['screenshot'] = '%sen/%s' % (shot.group(1), shot.group(2))
    if hit != 1:
        raise SystemExit('JSON-LD 里 #app 节点数应为 1，实际 %d' % hit)
    body = json.dumps(graph, ensure_ascii=False, indent=2)
    return html[:m.start(2)] + body + html[m.end(2):]


def _swap_english_screenshots(html):
    """截图有英文版就换成英文版：/assets/img/x.webp → /assets/img/en/x.webp。

    按文件是否存在决定，缺哪张就继续用中文那张 —— 英文界面的截图是一张张补的，
    不能因为少一张就卡住整页生成。补齐之后重新跑一次生成器即可（不跑的话
    tests/test_site_seo_contract.py 会因为生成物与源不一致而红）。

    英文截图必须与中文那张同尺寸：页面上的 width/height 是写死的，换一张比例
    不同的图会当场变形。
    """
    missing = []

    def swap(m):
        attr, name = m.group(1), m.group(2)
        if _has_english_image(name):
            return '%s="/assets/img/en/%s"' % (attr, name)
        missing.append(name)
        return m.group(0)

    html = re.sub(r'\b(href|src)="/assets/img/([^/"]+)"', swap, html)
    if missing:
        print('提示：以下截图还没有英文版，英文页暂用中文那张：%s'
              % ', '.join(sorted(set(missing))), file=sys.stderr)
    return html


def localize_en(html, dic, base):
    """页面级的中英差异 —— 与逐句翻译无关的那部分。"""
    en_url = base + 'en/'

    html = _replace_once(html, '<html lang="zh-CN">', '<html lang="en">', '<html lang>')
    html = _replace_once(html, '<link rel="canonical" href="%s">' % base,
                         '<link rel="canonical" href="%s">' % en_url, 'canonical')
    html = _replace_once(html, '<meta property="og:url" content="%s">' % base,
                         '<meta property="og:url" content="%s">' % en_url, 'og:url')

    # /en/index.html 深了一层，相对路径会解析成 /en/assets/…；转根绝对最省事。
    html, n = re.subn(r'\b(href|src)="assets/', r'\1="/assets/', html)
    if n < 6:
        raise SystemExit('资源路径改写次数异常：%d' % n)

    html = _swap_english_screenshots(html)

    # 站标回首页 —— 英文页要回英文首页。
    html = _replace_once(html, '<a class="wordmark" href="/">',
                         '<a class="wordmark" href="/en/">', '站标链接')
    # 语言切换的选中态换边。
    html = _replace_once(html, ' aria-current="page"', '', 'aria-current')
    html = _replace_once(html, '<a class="lang-btn" data-lang="en"',
                         '<a class="lang-btn" data-lang="en" aria-current="page"',
                         '英文语言链接')

    html = _patch_jsonld(html, dic, base, en_url)

    html = _replace_once(html, '<!DOCTYPE html>', '<!DOCTYPE html>\n' + BANNER, 'DOCTYPE')

    # 注释已在 translate() 阶段丢掉，这里把留下的空行收干净。
    html = '\n'.join(line.rstrip() for line in html.split('\n'))
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html


def build(src=None):
    if src is None:
        with open(SRC_HTML, encoding='utf-8') as f:
            src = f.read()
    with open(DICT_JSON, encoding='utf-8') as f:
        dic = json.load(f)
    en = dic['en']
    return localize_en(translate(src, en, strip_comments=True), en, base_url(src))


def main(argv=None):
    ap = argparse.ArgumentParser(description='生成 site/en/index.html')
    ap.add_argument('--check', action='store_true',
                    help='只校验已生成的文件是否与源同步，不写盘')
    args = ap.parse_args(argv)

    want = build()
    if args.check:
        if not os.path.exists(OUT_HTML):
            print('site/en/index.html 不存在，跑 '
                  '`uv run python scripts/build_site_en.py` 生成', file=sys.stderr)
            return 1
        with open(OUT_HTML, encoding='utf-8') as f:
            have = f.read()
        if have != want:
            print('site/en/index.html 与 site/index.html + scripts/site_i18n.json '
                  '不同步，跑 `uv run python scripts/build_site_en.py` 重新生成',
                  file=sys.stderr)
            return 1
        print('site/en/index.html 是最新的')
        return 0

    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    with open(OUT_HTML, 'w', encoding='utf-8', newline='\n') as f:
        f.write(want)
    print('已写出 %s（%d 字节）' % (os.path.relpath(OUT_HTML, ROOT), len(want.encode())))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
