"""模板无障碍与死 CSS 收口（2026-08-08 评审 P1#15 / P1#16 markup 半 / 四条 P2）。

为什么新开一个文件而不是塞进 `tests/test_css_contract.py`：那个文件 8000+ 行、
守的是**层叠模型**（特异度 / 对比度 / 盒模型的机器化重算），而这里守的是
**渲染出来的标记语义**（元素是不是可聚焦、选中态有没有播报、面板是不是模态）
外加几条「这段代码不许回来」的存在性断言。两类断言的失效方式完全不同：
层叠模型失效表现为「算不出来」，标记断言失效表现为「找不到元素」。混在一起
只会让下一个人在 8000 行里找不到该改哪一条。

四条 a11y 断言全部跑**真实渲染结果**（Flask test client）而不是模板源码：
Jinja 里写对了但 include/宏没被调用，是这类改动最典型的失败形态，
扫源码看不出来。

每条 docstring 记「旧行为是什么」+「朴素断言为什么是空的」——本仓惯例，
见 test_css_contract.py 各条。
"""
import os
import re
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, 'templates')
CSS_PATH = os.path.join(PROJECT_ROOT, 'static', 'css', 'style.css')


# --------------------------------------------------------------------- 工具

class _Tags(HTMLParser):
    """收集 (标签名, 属性 dict)，保持文档顺序。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v or '') for k, v in attrs}))

    handle_startendtag = handle_starttag


def _parse(html):
    p = _Tags()
    p.feed(html)
    return p.tags


def _by_id(tags, element_id):
    hits = [(tag, attrs) for tag, attrs in tags if attrs.get('id') == element_id]
    assert len(hits) == 1, (
        f'#{element_id} 在渲染结果里出现 {len(hits)} 次，期望恰好 1 次 —— 本测试已失效'
    )
    return hits[0]


def _classes(attrs):
    return set(attrs.get('class', '').split())


def _render(isolated_app, path):
    client = isolated_app.app.test_client()
    resp = client.get(path)
    assert resp.status_code == 200, f'GET {path} -> {resp.status_code}，本测试已失效'
    return resp.get_data(as_text=True)


def _css_no_comments():
    with open(CSS_PATH, encoding='utf-8') as f:
        return re.sub(r'/\*.*?\*/', '', f.read(), flags=re.S)


def _top_level_rules(css):
    """[(选择器, 规则体)]，跳过 @media 等 at-rule 的**外壳**但保留其内部规则。

    自己写而不是 import test_css_contract 的 `_rules`：那个文件是另一条测试链
    的载体，跨测试文件 import 会让「改 A 的 helper 悄悄改了 B 的判据」成为可能。
    这里只需要按花括号深度切规则，十几行的事。
    """
    out = []
    sel_start = 0
    depth = 0
    i = 0
    while i < len(css):
        ch = css[i]
        if ch == '{':
            if depth == 0:
                sel = css[sel_start:i].strip()
                body_start = i + 1
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                body = css[body_start:i]
                if sel.startswith('@'):
                    # at-rule：递归进它的内容，取里面的规则
                    out.extend(_top_level_rules(body))
                else:
                    out.append((sel, body))
                sel_start = i + 1
        i += 1
    return out


def _selector_parts(sel):
    return [p.strip() for p in sel.split(',') if p.strip()]


def _svg_geometries():
    """templates/ 下每个 `<svg>` 字面量的**几何体**（剥掉 svg 开标签与空白）。

    返回 {几何体: [来源 file:line]}。用几何体而不是整段文本比对：同一个图标
    在不同调用点尺寸/间距类不同，比整段文本永远「不重复」。
    """
    geo = {}
    for fn in sorted(os.listdir(TEMPLATES_DIR)):
        if not fn.endswith('.html'):
            continue
        with open(os.path.join(TEMPLATES_DIR, fn), encoding='utf-8') as f:
            src = f.read()
        for m in re.finditer(r'<svg\b.*?</svg>', src, re.S):
            body = re.sub(r'\s+', '', re.sub(r'<svg[^>]*>', '', m.group(0)))
            geo.setdefault(body, []).append(f'{fn}:{src[:m.start()].count(chr(10)) + 1}')
    assert len(geo) > 15, f'只扫到 {len(geo)} 个 SVG 几何体 —— 正则失配，本测试已失效'
    return geo


# ------------------------------------------------- P1#15 状态栏三个可点读数

# id -> 它点下去干什么（失败信息里直接告诉人少了哪条键盘路径）
_CLICKABLE_READOUTS = {
    'statusTasks': '打开任务面板（data-panel=records）',
    'statusCoords': '复制光标经纬度到剪贴板',
    'statusSelection': '复制选区 bbox 到剪贴板',
}


def test_clickable_statusbar_readouts_are_focusable_controls(isolated_app):
    """状态栏三个绑了 click 的读数项必须是 <button>，不能是 <span>。

    旧行为（templates/index.html 里 #statusTasks / #statusCoords /
    #statusSelection 三处）：三者都是
    `<span class="statusbar-item statusbar-pill ...">`，没有 tabindex、
    没有 role、没有 keydown 分支，而 `.statusbar-copy { cursor: pointer }`
    只对鼠标兑现「这里能点」。键盘用户既开不了任务面板，也复制不到坐标。

    朴素断言为什么是空的：
      · `assert 'statusbar-copy' in html` —— 旧标记也有这个类，恒真；
      · `assert 'tabindex' in html` —— 页面别处（.modal / 面板）本来就有
        tabindex，恒真；
      · 只查 `<button` 出现次数 —— 首页本来就有二十多颗按钮。
    所以这里按 **id 定位到具体元素**再看它的标签名，并显式禁掉
    「加个 tabindex 糊过去」的写法：span+tabindex 拿不到 Enter/Space 的
    默认激活，仍然要自己写 keydown，不是同一件事。
    """
    tags = _parse(_render(isolated_app, '/'))
    problems = []
    for element_id, what in _CLICKABLE_READOUTS.items():
        tag, attrs = _by_id(tags, element_id)
        if tag != 'button':
            problems.append(
                f'#{element_id}（{what}）渲染成 <{tag}>，键盘不可达；应为 <button>')
        elif attrs.get('type') != 'button':
            problems.append(
                f'#{element_id} 缺 type="button" —— 将来被挪进 <form> 会变成提交钮')
    assert not problems, '状态栏可点读数项不可用键盘操作：\n' + '\n'.join('  ' + p for p in problems)


def test_statusbar_pill_neutralises_the_ua_button_skin():
    """`.statusbar-pill` 必须把 UA 的按钮字体与墨色 inherit 掉。

    这条与上一条是一对：把 <span> 换成 <button> 之后，若不抹平 UA 样式，
    那三颗胶囊会用系统按钮字体（非 Inter）、系统 buttontext 墨色（非
    --color-text-secondary），与旁边同为胶囊的 <span> 读数明显不是一套 ——
    「改标签不改 CSS」正是这条要拦的半成品状态。

    底色/边框不在断言里：`.statusbar-pill` 本来就显式声明了它们，UA 的
    button 皮肤已经被压掉，再断言一遍是凑数。

    判据是 `font: inherit` 而不是「命中 .statusbar-pill 的规则数」：2026-08-11
    玻璃化改造新增的两个降级块（@supports not (backdrop-filter) /
    prefers-reduced-transparency）用联合选择器命中 .statusbar-pill，但只覆盖
    底色/边框/backdrop-filter，不碰字体与墨色 —— 皮肤规则仍应只有 1 条。
    """
    rules = [
        body for sel, body in _top_level_rules(_css_no_comments())
        if '.statusbar-pill' in _selector_parts(sel) and 'font: inherit' in body
    ]
    assert len(rules) == 1, f'.statusbar-pill 的皮肤规则有 {len(rules)} 条，期望 1 条 —— 本测试已失效'
    decls = {
        k.strip().lower(): v.strip()
        for k, _, v in (c.partition(':') for c in rules[0].split(';')) if v.strip()
    }
    assert decls.get('font') == 'inherit', (
        f'.statusbar-pill 的 font 是 {decls.get("font")!r}，应为 inherit —— '
        '三颗按钮胶囊会用系统按钮字体')
    assert decls.get('color') == 'inherit', (
        f'.statusbar-pill 的 color 是 {decls.get("color")!r}，应为 inherit —— '
        '三颗按钮胶囊会用系统 buttontext 墨色')


# --------------------------------------------- P1#16a 选中态：aria-pressed

def _status_chips(html):
    chips = [
        attrs for tag, attrs in _parse(html)
        if tag == 'button' and 'status-chip' in _classes(attrs)
    ]
    assert chips, '一颗 .status-chip 都没扫到 —— 本测试已失效'
    return chips


@pytest.mark.parametrize('path,expected', [('/', 19), ('/config', 10)])
def test_every_status_chip_declares_its_selected_state(isolated_app, path, expected):
    """每一颗 .status-chip 都要有 aria-pressed，且与 .active 一致。

    旧行为：三组 chips（任务状态筛选、主题 3 颗、语言 2 颗）的选中态
    **只有** CSS 类。`.status-chip.active` 与基态只差 color / border-color，
    读屏用户完全读不出当前筛的是哪一档、界面是什么主题、什么语种。

    朴素断言为什么是空的：`assert 'aria-pressed' in html` 在**旧标记上就
    通过** —— 地图左列的「框选」`#mapDrawRect` 与「地形光照」
    `#mapTerrainLighting` 一直带着 aria-pressed。必须逐颗看，并且把
    `.active` 与 `aria-pressed="true"` **配对**钉住：只查「属性存在」的话，
    全部写死 false 也能绿，而那正是「选中态读不出来」这个缺陷本身。

    首页 19 颗 = 管线 4 + 筛选 5 + 主题 3 + 语言 2 + 强调色 5(配置面板 include
    进首页,强调色组 2026-08-11 新增;筛选组 2026-08-12 随 §13-3 加了「有缺块」
    一枚;管线组 2026-08-15 Task 5 新增 —— #createPanel 里的
    [data-pipeline] map/dem/local_terrain/contour 四枚分段开关,取代了原先
    下载弹窗的 input[name="downloadType"] 单选与处理弹窗的 #processType 下拉,
    单选/下拉自带原生选中态,换成 .status-chip 之后选中态必须自己声明);
    /config 独立页 10 颗 = 主题 3 + 语言 2 + 强调色 5(没有任务筛选行,
    也没有新建面板)。
    先钉数量,
    正则/选择器失配时响亮失败而不是退化成空循环。
    """
    chips = _status_chips(_render(isolated_app, path))
    assert len(chips) == expected, (
        f'{path} 扫到 {len(chips)} 颗 .status-chip，期望 {expected} —— 本测试已失效')
    problems = []
    for attrs in chips:
        # 2026-08-15 Task 5:末位 fallback 前插一层 data-pipeline,否则四枚新的
        # 管线 chip 在失败信息里全叫 '?',读不出是哪一枚少了 aria-pressed。
        label = attrs.get(
            'data-status',
            attrs.get('data-theme-mode',
                      attrs.get('data-lang',
                                attrs.get('data-accent',
                                          attrs.get('data-pipeline', '?')))))
        pressed = attrs.get('aria-pressed')
        active = 'active' in _classes(attrs)
        if pressed not in ('true', 'false'):
            problems.append(f'chip[{label}] 的 aria-pressed = {pressed!r}，应为 "true"/"false"')
        elif (pressed == 'true') != active:
            problems.append(
                f'chip[{label}] aria-pressed={pressed} 与 class active={active} 不一致')
    assert not problems, (
        f'{path} 的分段开关读不出选中态：\n' + '\n'.join('  ' + p for p in problems))


# ------------------------------------------------- P1#16b 面板的非模态语义

# 2026-08-15 Task 5:第四个面板 #createPanel(新建任务)。它替掉了
# #downloadModal / #processModal 两个 .modal.fade —— 那两个是**真模态**
# (Bootstrap 自带遮罩 + aria-modal),收敛成一个 .workbench-panel 之后必须
# 兑现与另外三个面板一样的非模态契约,否则就是把模态语义偷偷带进了工作台。
@pytest.mark.parametrize(
    'panel_id', ['createPanel', 'historyPanel', 'configPanel', 'pluginsPanel'])
def test_slide_out_panels_declare_themselves_nonmodal(isolated_app, panel_id):
    """四个滑出面板都是非模态 dialog:role="dialog" 保留,**不许**再有 aria-modal。

    2026-08-11 起面板取消遮罩层:面板打开时地图保持可见可交互(非模态工作台,
    与 GeoLibre/QGIS 的停靠面板同模式)。aria-modal="true" 会向读屏宣称
    「页面其余部分已冻结」—— 遮罩没了之后这是一句假话,必须摘掉。
    role="dialog" 保留:它仍是一层自含功能区(非模态 dialog 是 APG 合法形态)。

    tabindex="-1" 与 [data-panel-close] 一并钉住:它们是焦点管理 JS 的**落点**
    (panels.js 打开时把焦点收进面板、关闭时归还)。少了它们,
    role="dialog" 就只是一句读屏听得见、键盘兑现不了的声明。
    """
    html = _render(isolated_app, '/')
    tag, attrs = _by_id(_parse(html), panel_id)
    assert tag == 'section', f'#{panel_id} 是 <{tag}> —— 本测试已失效'
    assert attrs.get('role') == 'dialog', (
        f'#{panel_id} 缺 role="dialog"(实际 {attrs.get("role")!r})')
    assert attrs.get('aria-modal') is None, (
        f'#{panel_id} 还带 aria-modal={attrs.get("aria-modal")!r} —— '
        '面板已非模态(遮罩层 2026-08-11 取消),aria-modal 宣称「页面其余部分'
        '已冻结」,而地图现在保持可见可交互,这是假话')
    assert attrs.get('tabindex') == '-1', (
        f'#{panel_id} 缺 tabindex="-1":焦点管理没有可程序化聚焦的落点')
    assert attrs.get('aria-label'), f'#{panel_id} 没有无障碍名称'

    # 面板内必须有一颗 [data-panel-close](panel_header 宏出的关闭钮)——
    # 打开时的初始焦点就落在它上面。section 不嵌套,取到下一个 </section> 为止。
    start = html.index(f'id="{panel_id}"')
    end = html.index('</section>', start)
    assert 'data-panel-close' in html[start:end], (
        f'#{panel_id} 里没有 [data-panel-close] 按钮 —— 焦点管理没有初始落点,'
        'Esc 之外没有键盘关闭路径')


def test_panels_have_no_backdrop_anymore(isolated_app):
    """遮罩层已取消:首页不再渲染 #panelBackdrop(2026-08-11 非模态化)。

    面板打开时地图保持可见可交互;关闭路径 = 关闭钮 / Esc / 工具条按钮。
    """
    html = _render(isolated_app, '/')
    assert 'id="panelBackdrop"' not in html, (
        '#panelBackdrop 还在渲染 —— 遮罩层已取消(面板非模态化)'
    )


# ----------------------------------------------------- P2 图标宏 / 内联 style

def test_no_icon_geometry_is_duplicated_across_templates():
    """模板树里不许有两处画同一个图标。

    旧行为：31 处 SVG 字面量收敛为 23 个几何体，6 个重复 —— 其中
    icon_tasks / icon_config / icon_close 三个**_macros.html 已经拥有的**图标
    被在 index.html（×2）与 _config_content.html（×1）重新贴了一遍；
    另外三组（下载箭头 ×3、扳手 ×3、说明 ⓘ ×2）当时还没有宏。

    朴素断言为什么是空的：`assert '<svg' 出现次数 == N` 只钉总量，把两处
    重复挪成一处重复 + 一处新图标照样通过；`assert 'polyline points="3 7 5 9 9 5"'
    只出现一次` 又会把宏定义本身算进去。这里比的是**几何体**（剥掉 svg 开
    标签，所以尺寸与 icon-inline 档位不参与），重复即红。
    """
    dupes = {g: src for g, src in _svg_geometries().items() if len(src) > 1}
    assert not dupes, (
        '同一个图标几何体在多处内联，改一处会漏改另几处（该收进 _macros.html）：\n'
        + '\n'.join(f'  {src}' for src in dupes.values()))


# 宏名 -> 该几何体唯一允许出现的文件。
#
# 2026-08-15 Task 5:`icon_process`(扳手,几何体特征标签 'path')换成
# `icon_create`(圆角方框里一个加号,特征标签 'rect')。icon_process 宏本身已从
# _macros.html 删掉:它在模板树里唯一的调用点是 _history_content.html 的
# #processOpenBtn,那颗按钮随入口收敛退役了(处理任务现在从新建面板的
# local_terrain 管线进)。扳手几何体在 static/js/task_list.js 里还有一份,
# 但那是 JS 字符串,不在本扫描器(只扫 templates/*.html)的射程内。
# icon_create 有三处调用点(左列工具条按钮 / 面板头 / 提交按钮),正是这条
# 「有宏就不许手写第二份」要守的形状。
# 表仍然是 6 条 —— 下面 `len(macro_geos) >= len(_MACRO_OWNED_ICONS)` 的下限
# 没有被放宽。
_MACRO_OWNED_ICONS = {
    'icon_close': 'line',
    'icon_tasks': 'polyline',
    'icon_config': 'circle',
    'icon_download': 'polyline',
    'icon_create': 'rect',
    'icon_info': 'circle',
}


def test_macro_owned_icons_live_only_in_the_macro_file():
    """六个有宏的图标，几何体只许出现在 _macros.html 里。

    与上一条不同：那条禁「重复」，这条禁「有宏还手写一遍」。只有一处
    调用点时上一条是绿的，而那一处若绕开宏手写，宏就成了没人用的死代码 ——
    这正是 _macros.html 存在之前的状态。

    先断言六个宏都还在（宏被删掉 / 改名时响亮失败），再断言它们的几何体
    在别的模板里零出现。
    """
    with open(os.path.join(TEMPLATES_DIR, '_macros.html'), encoding='utf-8') as f:
        macros_src = f.read()
    missing = [n for n in _MACRO_OWNED_ICONS if f'macro {n}(' not in macros_src]
    assert not missing, f'_macros.html 里找不到这些宏：{missing} —— 本测试已失效'

    geo = _svg_geometries()
    macro_geos = {
        g for g, src in geo.items() if all(s.startswith('_macros.html:') for s in src)
    }
    assert len(macro_geos) >= len(_MACRO_OWNED_ICONS), (
        f'_macros.html 独占的几何体只有 {len(macro_geos)} 个，'
        f'期望至少 {len(_MACRO_OWNED_ICONS)} 个 —— 有宏的图标被在别处重新内联了')

    strays = {
        g: src for g, src in geo.items()
        if any(s.startswith('_macros.html:') for s in src) and len(set(src)) > 1
    }
    assert not strays, ('宏里的图标在别的模板里又内联了一份：\n'
                        + '\n'.join(f'  {src}' for src in strays.values()))


def test_task_detail_modal_no_longer_carries_an_inline_style_block():
    """整棵模板树只剩 1 个内联 `style=`，且不在 base.html 里。

    旧行为：18 个内联 style 里有 17 个挤在 base.html 的任务详情弹窗
    （`font-family: var(--font-mono)` ×4、`border-color: var(--color-border)` ×4，
    加一批一次性 margin / flex）。style.css 自己写着「颜色走 CSS 类，
    不写内联 style」，而这一块是整棵树里唯一的反例。

    留下的那 1 个是 index.html 的取色器 `max-width:60px`：
    test_css_contract.py 的 test_color_picker_swatch_is_big_enough_to_see
    在注释里明确按它建模（「元素上的内联 max-width:60px 大于外框宽度，
    不参与」），搬走会让那条断言的前提悄悄改变，属于另一件事。

    朴素断言为什么是空的：`assert 'style=' not in base.html` 在**这次改动
    之后**才成立，但它同时也会在「有人把 17 条 style 搬进 index.html」时
    通过。所以钉的是**全树总数**，不是单文件。
    """
    hits = []
    for fn in sorted(os.listdir(TEMPLATES_DIR)):
        if not fn.endswith('.html'):
            continue
        with open(os.path.join(TEMPLATES_DIR, fn), encoding='utf-8') as f:
            src = f.read()
        for m in re.finditer(r'\bstyle\s*=\s*"([^"]*)"', src):
            hits.append(f'{fn}:{src[:m.start()].count(chr(10)) + 1} style="{m.group(1)}"')
    assert len(hits) == 1, (
        f'模板树里有 {len(hits)} 个内联 style=，期望恰好 1 个（取色器的 max-width）：\n'
        + '\n'.join('  ' + h for h in hits))
    assert hits[0].startswith('index.html:') and 'max-width' in hits[0], (
        f'剩下的那个内联 style 不是取色器的 max-width，而是 {hits[0]}')


# ------------------------------------------------------------- P2 死 CSS

# 零引用的「Bootstrap 覆盖类」里，**必须删掉**的六个。
#
# `.btn-info` 是 2026-08-15（Task 4）加进来的第六个。它此前不在这张表里，
# 而是被下面那条**双向跨文件锁**（`test_btn_info_is_kept_only_because_a_test_
# still_models_it`，已随本次改动整条删除）钉在原地：CSS 里少一条 `.btn-info`
# 规则分支会红，tests/test_css_contract.py 的 FILLED_BTN_VARIANTS 里摘掉它
# 也会红。那条锁记的理由是「它零引用，但按钮层叠模型拿它当被测对象」——
# 本次两头一起删（模型里还有 4 个真实填充变体在被逐格计算，够用了），
# 于是它降级成一个普通的「删掉就不许长回来」的零引用类，与另外五个同列。
# 删除的完整记账在 static/css/style.css 里 `.btn-danger` 之后那段登记注释，
# 以及 tests/test_button_geometry.py::test_btn_info_has_no_rule_branches。
# 2026-08-20 Task 9b：`.alert-warning` 移出本表 —— _plugins_content.html 的
# 「完全权限」提示一直在用它（此前由 Bootstrap 的同名规则渲染，「零引用」的
# 结论先于该页面），清退后已在 style.css 自有化，转入下面的存活断言清单。
_DELETED_DEAD_CLASSES = ('.alert-success',
                         '.text-success', '.text-warning', '.text-info',
                         '.btn-info')


def test_zero_reference_bootstrap_overrides_are_deleted():
    """六个零引用的覆盖类不许回来。

    旧行为：`.btn-info`(4 条) / `.alert-success` / `.alert-warning` /
    `.text-success` / `.text-warning` / `.text-info` 在 templates/、static/js/、
    src/、app.py 里全部零引用（两条动态拼接路径都够不到：`getStatusColor` 只
    返回 secondary|info|warning|success|danger，进的是 `bg-*`；toast 拼的是
    `app-toast--*`），约 40 行读起来像「调色板覆盖齐全」。

    朴素断言为什么是空的：`assert '.text-info' not in css` 会被**解释为什么
    删掉它**的注释打回来（本仓 test_dead_rules_removed 踩过两次）。所以先剥
    注释，再按规则扫描器逐条选择器比对。

    `.btn-info` 2026-08-15（Task 4）加入本表：它此前靠一条双向跨文件锁留在
    style.css 里（「模型还拿它当被测对象」），那条锁与它的 CSS 规则、
    FILLED_BTN_VARIANTS 里的登记同一轮一起删除。记账见 `_DELETED_DEAD_CLASSES`
    上方那段。
    """
    css = _css_no_comments()
    alive = {
        part
        for sel, _ in _top_level_rules(css)
        for part in _selector_parts(sel)
        for dead in _DELETED_DEAD_CLASSES
        if re.search(re.escape(dead) + r'(?![-\w])', part)
    }
    assert not alive, (
        '这些类在 templates/ static/js/ src/ app.py 零引用，规则应已删除：\n'
        + '\n'.join('  ' + s for s in sorted(alive)))
    # 活着的兄弟必须还在 —— 否则上面那条会在「整段调色板被删光」时也变绿。
    survivors = {
        part
        for sel, _ in _top_level_rules(css)
        for part in _selector_parts(sel)
    }
    for keeper in ('.alert-info', '.alert-danger', '.alert-warning',
                   '.text-danger', '.text-muted'):
        assert any(re.fullmatch(re.escape(keeper), s) for s in survivors), (
            f'{keeper} 有真实引用，不该被一起删掉 —— 本测试已失效')


def test_bounds_v_is_declared_exactly_once():
    """`.bounds-v` 只许有一条基态规则。

    旧行为：两条 `.bounds-v` 隔着 451 行分别待在「四至排版」和「数值可点击
    编辑」两节里。两者无属性冲突，所以任何层叠断言都不会红；真正的代价是
    第二块的 `padding: 0 2px` / `margin: 0 -2px` 直接参与第一块那个
    `auto 1fr auto 1fr` 网格的几何 —— 调 column-gap 的人（那里有一条量到
    1.6px 余量的实测上界）翻不到 451 行外的另一半。

    朴素断言为什么是空的：`assert css.count('.bounds-v') == 1` 会被
    `.bounds-v:hover`、被注释里的交叉引用、被 `.bounds-value` 这类前缀撞上。
    这里按规则扫描器取**选择器分支恰好等于 `.bounds-v`** 的规则。

    并且断言合并后两组声明都在：只删掉其中一块也能让计数变成 1，
    那是丢功能不是合并。
    """
    rules = [
        (sel, body) for sel, body in _top_level_rules(_css_no_comments())
        if '.bounds-v' in _selector_parts(sel)
    ]
    assert len(rules) == 1, (
        f'.bounds-v 基态有 {len(rules)} 条规则，期望 1 条：{[s for s, _ in rules]}')
    body = rules[0][1]
    for prop in ('font-variant-numeric', 'cursor', 'padding', 'margin'):
        assert re.search(r'(?<![-\w])' + prop + r'\s*:', body), (
            f'合并后的 .bounds-v 丢了 {prop} —— 合并不是删声明')
