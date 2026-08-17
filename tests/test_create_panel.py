"""四条管线并入一个「新建任务」面板（#createPanel）—— Task 5 的契约。

被这一整套钉住的东西，一句话：**四条管线（地图瓦片 / 高程 / 本地地形切片 /
等高线）只有一个入口、一张表单、一颗提交钮、一张显隐表，而四条载荷契约一个
字段名都没变。**

改前是两个 Bootstrap 弹窗：`#downloadModal`（瓦片 + 高程）与 `#processModal`
（地形切片 + 等高线），后者的入口藏在任务面板筛选行右端。四条管线零条在首屏
可达，弹窗还带来一个实测缺陷：dialog 高 742px、1366x768 余量仅 23px、
1366x720 时提交按钮落在折叠线下 91px。

为什么这一套测试要存在，而不是靠 test_css_contract.py 那边的几何模型：
几何模型答的是「像素够不够」，这里答的是「结构与接线对不对」——
显隐表漏一格、载荷少一个字段、入口留了两条，都不会让任何一个像素变化。

⚠️ 本文件的断言一律**从源码解析**，不写死条数：写死的话，有人往表单里加一条
管线、往显隐表里漏一格，模型算出来一模一样，测试全绿而界面已经错了。
唯一写死的是设计稿 §2.4 那张字段矩阵（`_DESIGN_MATRIX`）与四条路由的必填
清单（`_PAYLOAD_KEYS`）—— 它们是**外部事实**，抄进来才能双向核对。
"""

import json
import os
import re
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, 'templates')
_JS = os.path.join(_ROOT, 'static', 'js')
_CSS_PATH = os.path.join(_ROOT, 'static', 'css', 'style.css')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _template(name):
    return _read(os.path.join(_TEMPLATES, name))


def _js(name):
    return _read(os.path.join(_JS, name))


def _css():
    return _read(_CSS_PATH)


# ---------------------------------------------------------------------------
# 外部事实一：设计稿 §2.4 的字段矩阵，逐格抄进来。
#
# 行 = 字段组（模板里的容器 id），列 = 四条管线。抄的是设计稿
# docs/superpowers/specs/2026-08-14-frontend-system-ia-redesign-design.md 的
# §2.4 那张表，**不是**从 map.js 反推 —— 反推的话表和断言同源，改错了一起错。
# ---------------------------------------------------------------------------
_PIPELINES = ('map', 'dem', 'local_terrain', 'contour')

_DESIGN_MATRIX = {
    # 字段组 id            瓦片   高程   地形切片  等高线
    'selectionField':     ('map', 'dem'),
    'sourceField':        ('local_terrain', 'contour'),
    'outputFormatField':  ('map',),
    'mapStyleField':      ('map',),
    'zoomField':          ('map', 'contour'),
    'demOptions':         ('dem',),
    'localTerrainOptions': ('local_terrain',),
    'contourOptions':     ('contour',),
    'outputPathField':    ('map', 'dem'),
}

# 矩阵之外还有几行，条件里多一层「数据来源」——那正是改前 initProcessTypeToggle
# 里的第二个维度（「处理类型 × 数据来源」）。设计稿把它写在矩阵下面的散文里。
_SOURCE_CONDITIONED_ROWS = {
    'sourceUploadRow':   (('local_terrain', 'contour'), 'upload'),
    'contourSourceHint': (('contour',), 'upload'),
    'processDemTaskRow': (('local_terrain', 'contour'), 'dem_task'),
}

# 「zoom_max 留空 = 自动」只对等高线成立（contour_api 把空值当未表态），
# 瓦片那条腿空值会 parseInt 出 NaN。
_EXTRA_ROWS = {
    'zoomAutoHint': ('contour',),
}


# ---------------------------------------------------------------------------
# 外部事实二：四条路由的载荷键。**从 src/routes/ 的必填清单反向核对**。
#
# 这一组是本文件里最重要的断言：IA 可以随便重排，后端在消费这些字段名，
# 少一个、改一个字母都是 400 或者静默丢参数。
#   POST /api/tasks               src/routes/api.py::create_task
#   POST /api/dem/tasks           src/routes/dem_api.py::create_dem_task
#   POST /api/terrain/local/tasks src/routes/local_terrain_api.py
#   POST /api/contour/tasks       src/routes/contour_api.py
# ---------------------------------------------------------------------------
_PAYLOAD_KEYS = {
    # 瓦片：api.py 的 required_fields 十项 + 正交的 export_mbtiles。
    # source_plugin_id / source_id 是**条件附加**（选了插件源才带），
    # region 同理（导入多边形才带），所以不在这个「恒定键集」里，
    # 由 test_plugin_source_fields_still_conditional 单独钉。
    'map': {
        'name', 'north', 'south', 'east', 'west',
        'zoom_min', 'zoom_max', 'style', 'output_format', 'output_path',
        'export_mbtiles',
    },
    # 高程：dem_api.py 的 required 六项 + dataset + 两个颗粒开关。
    'dem': {
        'name', 'north', 'south', 'east', 'west',
        'dataset', 'output_path', 'download_num', 'download_swb',
    },
    # 本地地形切片：FormData。local_terrain_api 读的就是这四个 + files|dem_task_id。
    'local_terrain': {
        'name', 'maxzoom', 'quality', 'vertex_normals',
    },
    # 等高线：FormData 十项 + files|dem_task_id。
    'contour': {
        'name', 'contour_interval', 'zoom_min', 'zoom_max', 'background',
        'terrain_shade', 'line_color_intermediate', 'line_color_index',
        'tint_breaks', 'tint_colors',
    },
}

# 两条上传管线的来源是**互斥**的一对（后端：两个都给就 400）。
_SOURCE_EXCLUSIVE_KEYS = {'files', 'dem_task_id'}

# 四个退役的 id。全仓零命中（注释也算：留一个在注释里，下一个人 grep 它会找到
# 一段讲历史的散文，然后花十分钟确认那不是活代码）。
_RETIRED_IDS = ('downloadModal', 'processModal', 'processOpenBtn', 'processNameRow')


def _code_lines(src):
    """去掉整行注释。断言「源码里没有某个字面量」时必须先过这一道。

    否则一段解释「改前这里有 X」的注释会让「X 不许出现」的断言永远红 ——
    而唯一的修法就是把注释删掉，等于用测试逼着人删掉历史说明。
    只剥**整行**注释，不碰行尾注释：判据要可预测，不做半个 JS 词法分析。
    """
    keep = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith(('//', '*', '/*', '{#', '#}', '<!--')):
            continue
        keep.append(line)
    return '\n'.join(keep)


# ---------------------------------------------------------------------------
# 解析器（都做成纯函数吃源码字符串：末尾那几条红态探针要拿变异过的源码喂它们）
# ---------------------------------------------------------------------------

_ATTR_RE = re.compile(r'([-\w]+)(?:\s*=\s*"([^"]*)")?')


def _attrs_of(tag_text):
    """从一个开标签的属性串里扒 {name: value}；无值属性（hidden）值为 ''。"""
    return {m.group(1): (m.group(2) or '') for m in _ATTR_RE.finditer(tag_text)}


def _open_tag(html, tag, el_id):
    """找 `<tag ... id="el_id" ...>` 的属性字典；找不到返回 None。"""
    for m in re.finditer(r'<' + tag + r'\b([^>]*)>', html):
        attrs = _attrs_of(m.group(1))
        if attrs.get('id') == el_id:
            return attrs
    return None


def _section_of(html, el_id):
    """`<section ... id="el_id">` 到它的 `</section>` 之间的文本。

    section 不嵌套（四个面板都是 <body> 直下的兄弟），所以「下一个 </section>」
    就是收尾 —— 与 tests/test_records_panel_structure.py 的切片同一个前提。
    """
    m = re.search(r'<section\b[^>]*\bid="' + el_id + r'"[^>]*>', html)
    assert m, f'index.html 里找不到 <section id="{el_id}"> —— 本测试已失效'
    end = html.index('</section>', m.end())
    assert '<section' not in html[m.end():end], (
        f'#{el_id} 里出现了嵌套 <section> —— 「下一个 </section> 即收尾」这个'
        '切片前提失效了，本测试已失效'
    )
    return html[m.start():end]


def _js_block(src, opener):
    """从 `opener` 起、按花括号配平截出一段（函数体或数组字面量）。"""
    i = src.index(opener)
    open_ch = '[' if opener.rstrip().endswith('[') else '{'
    close_ch = ']' if open_ch == '[' else '}'
    depth = 0
    start = src.index(open_ch, i)
    for j in range(start, len(src)):
        if src[j] == open_ch:
            depth += 1
        elif src[j] == close_ch:
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f'`{opener}` 的括号不配平 —— 本测试已失效')


def parse_pipeline_table(map_js):
    """把 map.js 的 `PIPELINE_FIELDS` 解析成 {id: (pipelines, source_or_None)}。

    从源码解析而不是 import：这是浏览器侧的 JS，没有 Node 也要能跑。
    """
    block = _js_block(map_js, 'const PIPELINE_FIELDS = [')
    table = {}
    for row in re.finditer(r'\{\s*id:\s*\'([^\']+)\'\s*,\s*'
                           r'pipelines:\s*\[([^\]]*)\]\s*'
                           r'(?:,\s*source:\s*\'([^\']+)\'\s*)?,?\s*\}', block):
        el_id, pipes, source = row.group(1), row.group(2), row.group(3)
        table[el_id] = (tuple(re.findall(r"'([^']+)'", pipes)), source)
    assert table, 'PIPELINE_FIELDS 解析不出任何一行 —— 本测试已失效'
    return table


def parse_payload_keys(map_js):
    """四条管线各自实际发出去的键集合。

    瓦片 / 高程走 JSON 对象字面量（`taskData = { ... }`），两条处理管线走
    FormData（`fd.append('key', ...)`）。两种形态各自解析，不合并成一套正则 ——
    合并只会让「哪一条管线漏了字段」这个信息在失败信息里消失。
    """
    def json_branch(body, api_url):
        """`apiUrl = '<api_url>'` 那一支里 taskData 的键。"""
        # 先按 apiUrl 定位分支，再回头找它那一段 `taskData = {`。
        at = body.index(f"apiUrl = '{api_url}'")
        obj_start = body.rindex('taskData = {', 0, at)
        obj = _js_block(body[obj_start:], 'taskData = {')
        return set(re.findall(r'^\s*([a-z_]+)\s*:', obj, re.M))

    def form_branch(body):
        return set(re.findall(r"fd\.append\('([^']+)'", body))

    submit_download = _js_block(map_js, 'async function submitDownload(')
    return {
        'map': json_branch(submit_download, '/api/tasks'),
        'dem': json_branch(submit_download, '/api/dem/tasks'),
        'local_terrain': form_branch(
            _js_block(map_js, 'async function submitLocalTerrain(')),
        'contour': form_branch(_js_block(map_js, 'async function submitContour(')),
    }


# ---------------------------------------------------------------------------
# 1. 结构：面板本体
# ---------------------------------------------------------------------------

def test_create_panel_is_a_workbench_panel_like_its_two_siblings():
    """#createPanel 与「任务」「配置」两个面板**同构**，不是第三种形态。

    同构的判据不是「看起来像」，是逐条属性对齐：`.workbench-panel` 类（满高
    定位、滑出过渡、panels.js 的开关都挂在它上面）、`role="dialog"`、
    `tabindex="-1"`（供 panels.js 程序化聚焦）、`aria-label`、初始 `hidden`、
    以及一个 `[data-panel-resizer]` 拖宽热区。

    **不许有 aria-modal**：面板是非模态的，那个属性宣称「页面其余部分已冻结」，
    在这里是假话 —— 而「地图始终可交互」正是这次从弹窗换成面板的第二条理由
    （设计稿 §2.3）。这一条与 tests/test_fix_templates_a11y.py 的四面板
    parametrize 重叠，是有意的：那边守全站一致，这边守本面板的存在。
    """
    attrs = _open_tag(_template('index.html'), 'section', 'createPanel')
    assert attrs is not None, 'index.html 里没有 <section id="createPanel">'
    assert 'workbench-panel' in attrs.get('class', '').split(), (
        f'#createPanel 的 class 是 {attrs.get("class")!r}，缺 workbench-panel —— '
        '满高定位与滑出过渡都挂在这个类上'
    )
    assert attrs.get('role') == 'dialog', '#createPanel 缺 role="dialog"'
    assert attrs.get('tabindex') == '-1', (
        '#createPanel 缺 tabindex="-1" —— panels.js 打开面板时要程序化聚焦它'
    )
    assert attrs.get('aria-label'), '#createPanel 缺 aria-label —— 读屏用户听不出这是什么'
    assert 'hidden' in attrs, '#createPanel 没有初始 hidden —— 首屏会直接摊开一张表单'
    assert 'aria-modal' not in attrs, (
        '#createPanel 自报了 aria-modal —— 它是非模态的（地图始终可交互），'
        '这个属性是假话'
    )

    section = _section_of(_template('index.html'), 'createPanel')
    assert 'data-panel-resizer' in section, (
        '#createPanel 里没有 [data-panel-resizer] —— 拖拽调宽是它与另两个面板'
        '同构的一部分（panels.js 的 RESIZE_CONFIGS 里有它）'
    )
    # 关闭钮来自 _macros.html 的 panel_header 宏，所以模板源码里看不到那个属性
    # 字面量 —— 断言走「调用点 + 宏体」两段，而不是在渲染后的 HTML 上找。
    # 这也顺便钉住「不许绕开宏手写一份面板头」：那是 _macros.html 存在之前的状态
    # （关闭图标曾在三处各写一遍）。
    assert 'panel_header(' in section, (
        '#createPanel 没有走 _macros.html 的 panel_header 宏 —— 面板头部（图标 + '
        '标题 + 关闭钮）在三个面板之间是逐字重复的，宏是唯一的存放处'
    )
    assert 'data-panel-close' in _template('_macros.html'), (
        'panel_header 宏里没有 [data-panel-close] —— panels.js 的 openPanel 靠它'
        '给键盘用户一个明确的退出落点，本测试已失效'
    )


def test_form_and_submit_button_live_in_the_panel_with_the_button_outside_the_form():
    """`#taskForm` 与 `#createTaskBtn` 都在面板里，而**提交钮在表单之外**。

    第二半才是这次的关键。提交钮住在 `.config-footer` 里、靠 `form="taskForm"`
    关联 —— 照抄 `_config_content.html` 那颗 `form="configForm"` 的写法。它是
    「提交按钮永远够得着」这个结论的**唯一**原因：底条在 `.config-scroll`
    之外，所以表单多长都不会把它推走。

    弹窗时代提交钮是表单的直接子元素，跟着内容流动，实测 bottom 901.50 /
    视口 768（1366x720 一档溢出 91px）。像素侧的证明在
    tests/test_css_contract.py::test_submit_button_fits_at_1366x768。
    """
    html = _template('index.html')
    section = _section_of(html, 'createPanel')
    assert 'id="taskForm"' in section, '#taskForm 不在 #createPanel 里'
    assert 'id="createTaskBtn"' in section, '#createTaskBtn 不在 #createPanel 里'

    form_start = section.index('id="taskForm"')
    form_end = section.index('</form>', form_start)
    btn_at = section.index('id="createTaskBtn"')
    assert not (form_start < btn_at < form_end), (
        '#createTaskBtn 落在 <form id="taskForm"> 里面了 —— 它必须留在'
        '.config-footer（表单之外），否则重新跟着内容滚动，提交钮会回到折叠线下'
    )

    btn = _open_tag(section, 'button', 'createTaskBtn')
    assert btn.get('type') == 'submit', '#createTaskBtn 不是 type="submit"'
    assert btn.get('form') == 'taskForm', (
        f'#createTaskBtn 的 form 属性是 {btn.get("form")!r} —— 它在表单之外，'
        '缺了 form="taskForm" 点下去根本不派发 submit 事件，四条管线全提交不了'
    )
    assert 'disabled' not in btn, (
        '#createTaskBtn 带 disabled —— disabled 的按钮不可聚焦、不在 tab 序里，'
        '键盘用户碰不到它也读不到「为什么不能提交」。缺选区/缺文件由 submit '
        '处理器当场 toast'
    )


def test_footer_is_a_bottom_bar_by_flex_layout_not_by_position_sticky():
    """底条「永不落到折叠线下」靠的是 **flex 布局**，不是 `position: sticky`。

    ⚠️ 计划书原话写的是「底条是 .config-footer 形态（position: sticky，由 CSS
       断言）」。**那不是实现的机制**，照它写会得到一条假绿断言：
       `.config-footer` 从来没有 position 声明，它是三层 flex 的第三层 ——
       `.config-layout` 列容器 / `.config-scroll { flex: 1; overflow-y: auto }`
       吃满剩余高度并自己滚 / `.config-footer { flex: 0 0 auto }` 占住剩下的。
       这比 sticky **更强**：sticky 的元素仍在滚动流里、仍可能被祖先的 overflow
       裁掉；flex 的底条根本不在滚动容器里。

    所以这里断言真实机制的三个环节。像素结论（bottom = 视口高 - 12）在
    tests/test_css_contract.py::_create_panel_submit_bottom，那边还额外验了
    「宿主下内边距被底条的负下外边距恰好抵掉」。
    """
    css = _css()

    def decls(selector):
        m = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
        assert m, f'style.css 里找不到 `{selector}` —— 本测试已失效'
        out = {}
        for part in m.group(1).split(';'):
            if ':' in part:
                k, v = part.split(':', 1)
                out[k.strip()] = v.split('/*')[0].strip()
        return out

    assert decls('.config-layout').get('flex-direction') == 'column', (
        '.config-layout 不是 flex 列容器 —— 底条与滚动区的上下关系没了'
    )
    scroll = decls('.config-scroll')
    assert scroll.get('overflow-y') == 'auto', (
        '.config-scroll 不再 overflow-y: auto —— 表单要么整层撑高把底条顶出'
        '视口，要么滚动跑到外层去，两种都会把提交钮带走'
    )
    footer = decls('.config-footer')
    assert footer.get('flex', '').split()[:2] == ['0', '0'], (
        f'.config-footer 的 flex 是 {footer.get("flex")!r}，不是 `0 0 auto` —— '
        'flex-grow/shrink 一旦不为 0，表单一长底条就会被压扁或被推走'
    )
    assert 'position' not in footer, (
        '有人给 .config-footer 加了 position —— 它不需要：底条不在滚动容器里，'
        'sticky/fixed 只会在拖宽面板或窄屏全屏覆盖时引入新的定位问题。'
        '真要改，先把本测试的 docstring 一起改掉'
    )


# ---------------------------------------------------------------------------
# 2 & 3. rail 入口与三个触发器
# ---------------------------------------------------------------------------

def test_rail_has_a_create_entry_in_its_first_group():
    """左侧工具条**第一组**是「新建」，图标来自 _macros.html。

    为什么必须是第一组：四条管线现在只有这一个入口，它是首屏唯一的「开始做事」
    按钮。为什么必须落在 rail 里而不是顶栏 / 常驻侧栏 / 挤占式 dock：那三条都在
    设计稿 §6 的已否决清单里（2026-07 试过并删掉）。

    图标走宏而不是内联 SVG：tests/test_fix_templates_a11y.py 的
    test_no_icon_geometry_is_duplicated_across_templates 禁止模板树里出现两处
    同几何体，宏是唯一的存放处。
    """
    html = _template('index.html')
    toolbar_at = html.index('<div class="map-toolbar"')
    groups = list(re.finditer(r'<div class="map-panel-triggers"[^>]*>', html[toolbar_at:]))
    assert groups, '.map-toolbar 里一个 .map-panel-triggers 组都没有 —— 本测试已失效'
    first_group_start = toolbar_at + groups[0].end()
    first_group_end = (toolbar_at + groups[1].start()) if len(groups) > 1 else len(html)
    first_group = html[first_group_start:first_group_end]
    assert 'data-panel="create"' in first_group, (
        '[data-panel="create"] 不在工具条的第一组里（第一组内容：'
        f'{first_group.strip()[:120]!r}）—— 「新建」是首屏唯一的创建入口，'
        '排在缩放/框选之后就等于把它降级成一个次要动作'
    )

    btn = _open_tag(html, 'button', None)   # 「新建」按钮没有 id，按属性找
    for m in re.finditer(r'<button\b([^>]*)>', html):
        attrs = _attrs_of(m.group(1))
        if attrs.get('data-panel') == 'create':
            btn = attrs
            break
    assert btn.get('aria-label'), '「新建」按钮缺 aria-label'
    assert btn.get('title'), '「新建」按钮缺 title'
    assert btn.get('aria-expanded') == 'false', (
        '「新建」按钮缺 aria-expanded="false" 初值 —— 它是 toggle，读屏用户'
        '要能听出它控制的面板现在开着没有'
    )
    assert 'map-panel-btn' in btn.get('class', '').split(), (
        '「新建」按钮不是 .map-panel-btn —— 它要与「任务」「配置」同一套几何'
    )

    # 图标必须是宏调用，不是手写 SVG。
    create_btn_html = html[html.index('data-panel="create"'):]
    create_btn_html = create_btn_html[:create_btn_html.index('</button>')]
    assert '<svg' not in create_btn_html, (
        '「新建」按钮里手写了内联 SVG —— 图标要走 _macros.html 的宏'
        '（几何体重复会被 test_no_icon_geometry_is_duplicated_across_templates 判红）'
    )
    assert 'icon_create' in create_btn_html, (
        '「新建」按钮没有调用 m.icon_create() —— 图标从哪来？'
    )
    assert 'macro icon_create(' in _template('_macros.html'), (
        '_macros.html 里没有 icon_create 宏 —— 本测试已失效'
    )


def test_every_panel_trigger_declares_aria_expanded():
    """**每一个** [data-panel] 触发器都有 aria-expanded 初值。

    改前三处全缺（rail 的「任务」「配置」，以及状态栏的任务胶囊）：面板开着时
    只有一个 `.map-panel-btn--active` 类在变，而那个类只差 color/border-color
    —— 读屏用户听不出哪个面板正开着，也听不出这颗按钮再点一次会关掉它。

    这一条与 panels.js 侧的同步（openPanel/closePanel 一起翻 class 与
    aria-expanded）是一对，缺任何一半都会退回「只有视觉反馈」。
    """
    html = _template('index.html')
    missing = []
    for m in re.finditer(r'<button\b([^>]*)>', html):
        attrs = _attrs_of(m.group(1))
        if 'data-panel' not in attrs:
            continue
        if attrs.get('aria-expanded') != 'false':
            missing.append((attrs.get('data-panel'), attrs.get('id') or attrs.get('class')))
    assert not missing, (
        f'这些 [data-panel] 触发器缺 aria-expanded="false" 初值：{missing} —— '
        '读屏用户听不出它控制的面板开着没有'
    )

    panels_js = _js('panels.js')
    for fn in ('openPanel', 'closePanel'):
        body = _js_block(panels_js, f'function {fn}(')
        assert "setAttribute('aria-expanded'" in body, (
            f'panels.js 的 {fn}() 没有同步 aria-expanded —— 模板给了初值但'
            '运行时不翻，等于永远停在 false'
        )


# ---------------------------------------------------------------------------
# 4. 面板是真 toggle
# ---------------------------------------------------------------------------

def test_rail_buttons_are_real_toggles():
    """同名再点关闭。改前 `openPanel` 里有一句 `if (current === name) return;`。

    那句话的后果：点开的按钮再点一次毫无反应 —— 一颗高亮着、看起来「按下」的
    按钮，唯一的关闭路径却是 Esc 或面板里的关闭钮。

    实现上关闭语义**不能**塞回 openPanel：程序化入口（map.js 的
    openCreatePanel、_afterTaskCreated）调的就是 openPanel，把关闭塞进去会让
    「从选区浮层点新建任务」在面板已开时把面板关掉。所以判据住在 togglePanel
    里，只有 [data-panel] 的点击委托走它。
    """
    src = _js('panels.js')
    assert 'if (current === name) return;' not in _code_lines(src), (
        'panels.js 里还留着 `if (current === name) return;` —— 工具条按钮'
        '不是 toggle，点开之后再点一次没有反应'
    )
    assert 'function togglePanel(' in src, 'panels.js 里没有 togglePanel()'
    toggle = _js_block(src, 'function togglePanel(')
    assert 'current === name' in toggle and 'closePanel()' in toggle, (
        'togglePanel() 里没有「同名就 closePanel()」—— 它不是 toggle'
    )
    assert 'openPanel(name)' in toggle, 'togglePanel() 不会打开面板'

    # 点击委托必须走 togglePanel，而不是直接 openPanel。
    delegation = src[src.index("querySelectorAll('[data-panel]')"):]
    delegation = delegation[:delegation.index('data-panel-close')]
    assert 'togglePanel(name)' in delegation, (
        '[data-panel] 的点击委托没有调 togglePanel —— 按钮又变成「只开不关」'
    )

    # openPanel 仍必须是幂等的「打开」：已开着时不重开、也不关。
    open_body = _js_block(src, 'function openPanel(')
    assert 'closePanel()' not in open_body.replace('closePanel(true)', ''), (
        'openPanel() 里出现了非静默的 closePanel() —— 它必须是幂等的「打开」，'
        '否则程序化入口（openCreatePanel 等）会把面板关掉'
    )


def test_create_panel_is_registered_in_panels_js():
    """`create` 进了 PANELS 与 RESIZE_CONFIGS 两张表。

    PANELS 决定 `openPanel('create')` / `#create` hash 直达能不能用；
    RESIZE_CONFIGS 决定模板里那个 [data-panel-resizer] 热区是不是死代码。
    """
    src = _js('panels.js')
    panels = _js_block(src, 'var PANELS = {')
    assert "create: 'createPanel'" in panels, (
        "panels.js 的 PANELS 里没有 create: 'createPanel' —— openPanel('create') "
        '与 #create hash 直达都不成立'
    )
    resize = _js_block(src, 'var RESIZE_CONFIGS = [')
    assert "id: 'createPanel'" in resize, (
        'RESIZE_CONFIGS 里没有 createPanel —— 模板里那个 [data-panel-resizer] '
        '热区没有人接线，是死标记'
    )
    assert "'tf-panel-w-create'" in resize, (
        'createPanel 没有自己的 localStorage key —— 与配置面板共用一个 key 会'
        '让拖窄一个面板改掉另一个记住的宽度，而两者的 min/max 还不一样'
    )


# ---------------------------------------------------------------------------
# 5. 四条管线段控
# ---------------------------------------------------------------------------

def test_pipeline_segmented_control_has_exactly_the_four_backend_pipeline_names():
    """`#taskForm` 里恰好四枚 [data-pipeline] chip，值就是后端管线名。

    四个值不是 UI 自造的枚举，是路由分派的依据（map -> /api/tasks，
    dem -> /api/dem/tasks，local_terrain -> /api/terrain/local/tasks，
    contour -> /api/contour/tasks）。改一个字母就等于把一条管线接到空气上。

    `type="button"` 不能省：表单里不写 type 的按钮默认是提交钮，点一下管线
    就等于点了「创建任务」。
    """
    section = _section_of(_template('index.html'), 'createPanel')
    form = section[section.index('id="taskForm"'):section.index('</form>')]

    chips = []
    for m in re.finditer(r'<button\b([^>]*)>', form):
        attrs = _attrs_of(m.group(1))
        if 'data-pipeline' in attrs:
            chips.append(attrs)
    assert [c['data-pipeline'] for c in chips] == list(_PIPELINES), (
        f'#taskForm 里的 [data-pipeline] 值是 '
        f'{[c["data-pipeline"] for c in chips]}，期望 {list(_PIPELINES)} —— '
        '这四个值是后端管线名，顺序也是设计稿 §2.4 那张矩阵的列序'
    )
    for c in chips:
        name = c['data-pipeline']
        assert c.get('type') == 'button', (
            f'管线 chip `{name}` 没有 type="button" —— 表单里默认是提交钮，'
            '点一下管线就等于点了「创建任务」'
        )
        assert c.get('aria-pressed') in ('true', 'false'), (
            f'管线 chip `{name}` 缺 aria-pressed —— 选中态只有一个 CSS 类时，'
            '读屏用户听不出当前停在哪条管线'
        )
        assert 'status-chip' in c.get('class', '').split(), (
            f'管线 chip `{name}` 不是 .status-chip —— 段控复用全站既有的那一套'
            '（主题开关 / 状态筛选），不另起一份 CSS'
        )
    pressed = [c['data-pipeline'] for c in chips if c['aria-pressed'] == 'true']
    assert pressed == ['map'], (
        f'默认选中的管线是 {pressed}，期望恰好 ["map"] —— 段控是单选，'
        '而瓦片是唯一在「选区上下文」里说得通的默认'
    )
    active = [c['data-pipeline'] for c in chips if 'active' in c.get('class', '').split()]
    assert active == pressed, (
        f'.active 落在 {active} 而 aria-pressed 落在 {pressed} —— 两者必须同步，'
        '否则视觉与读屏说的是两条管线'
    )


# ---------------------------------------------------------------------------
# 6. 一张显隐表（本文件的「状态节点计数」在这里）
# ---------------------------------------------------------------------------

def test_visibility_is_one_table_driven_apply():
    """显隐只有**一张表 + 一个 apply()**，且表里的状态节点数逐个对上。

    改前是两个函数里的两个同名 `apply()` 闭包：initDownloadTypeToggle 一维
    （下载类型）与 initProcessTypeToggle 二维（处理类型 × 数据来源）。加一条
    管线要在两处各改一遍，而两处的写法还不一样（`!(isMap)` vs `hidden = isDem`）。

    状态节点计数（三个数都从源码数出来，不是写死的期望值互相印证）：
      - 管线状态 4 个（PIPELINES）
      - 显隐表 13 行 = 设计稿矩阵 9 行 + 来源条件 3 行 + 「留空=自动」提示 1 行
      - 表驱动的 apply 恰好 1 个
    """
    src = _js('map.js')

    assert src.count('function applyPipeline(') == 1, (
        f'map.js 里有 {src.count("function applyPipeline(")} 个 applyPipeline —— '
        '显隐必须只有一个入口'
    )
    for gone in ('function initDownloadTypeToggle(', 'function initProcessTypeToggle('):
        assert gone not in src, (
            f'map.js 里还有 `{gone}` —— 两个显隐函数必须合成一个 initPipelineToggle'
        )
    assert 'function initPipelineToggle(' in src, 'map.js 里没有 initPipelineToggle()'

    pipelines = re.search(r'const PIPELINES = \[([^\]]*)\]', src)
    assert pipelines, 'map.js 里没有 const PIPELINES —— 管线名散在各处就是第二处事实'
    assert tuple(re.findall(r"'([^']+)'", pipelines.group(1))) == _PIPELINES, (
        f'map.js 的 PIPELINES 是 {pipelines.group(1)!r}，与后端管线名不一致'
    )

    table = parse_pipeline_table(src)
    expected_rows = (len(_DESIGN_MATRIX) + len(_SOURCE_CONDITIONED_ROWS)
                     + len(_EXTRA_ROWS))
    assert len(table) == expected_rows, (
        f'显隐表有 {len(table)} 行，期望 {expected_rows} 行'
        f'（矩阵 {len(_DESIGN_MATRIX)} + 来源条件 {len(_SOURCE_CONDITIONED_ROWS)}'
        f' + 额外 {len(_EXTRA_ROWS)}）。多出来的：'
        f'{sorted(set(table) - set(_DESIGN_MATRIX) - set(_SOURCE_CONDITIONED_ROWS) - set(_EXTRA_ROWS))}；'
        f'少掉的：{sorted(set(_DESIGN_MATRIX) | set(_SOURCE_CONDITIONED_ROWS) | set(_EXTRA_ROWS) - set(table))}'
    )

    # applyPipeline 必须真的消费这张表，而不是另写一堆 if。
    apply_body = _js_block(src, 'function applyPipeline(')
    assert 'PIPELINE_FIELDS' in apply_body, (
        'applyPipeline() 没有消费 PIPELINE_FIELDS —— 表在那儿摆着，显隐另走一套'
    )
    for wired in ('updateTileEstimate()', 'updateCreatePanelBounds()',
                  'renderTerrainTileEstimate()', 'refreshSubmitButtonState()'):
        assert wired in apply_body, (
            f'applyPipeline() 里没有 {wired} —— 改前那两个 apply() 的接线清单'
            '一个都不能漏（切管线时预估/摘要/预告/按钮态都要跟着刷）'
        )


def test_visibility_table_matches_the_design_matrix_cell_by_cell():
    """显隐表与设计稿 §2.4 的字段矩阵**逐格**一致（9 组 × 4 管线 = 36 格）。

    矩阵抄在本文件顶部的 `_DESIGN_MATRIX`，是**外部事实**：从设计稿抄，不从
    map.js 反推。反推的话表与断言同源，漏一格会一起漏。
    """
    table = parse_pipeline_table(_js('map.js'))

    wrong = []
    for el_id, expected in _DESIGN_MATRIX.items():
        assert el_id in table, (
            f'显隐表里没有字段组 `{el_id}` —— 设计稿 §2.4 的矩阵有它这一行'
        )
        pipes, source = table[el_id]
        assert source is None, (
            f'`{el_id}` 在显隐表里带了 source 条件（{source!r}）—— '
            '矩阵那 9 行只按管线切换，多一层条件就是多一个隐藏状态'
        )
        for p in _PIPELINES:
            if (p in pipes) != (p in expected):
                wrong.append(f'{el_id} × {p}: 表里{"可见" if p in pipes else "隐藏"}'
                             f'，矩阵要求{"可见" if p in expected else "隐藏"}')
    assert not wrong, (
        '显隐表与设计稿 §2.4 的矩阵有 %d 格对不上：\n  %s'
        % (len(wrong), '\n  '.join(wrong))
    )

    for el_id, (expected_pipes, expected_source) in _SOURCE_CONDITIONED_ROWS.items():
        assert el_id in table, f'显隐表里没有来源条件行 `{el_id}`'
        pipes, source = table[el_id]
        assert tuple(pipes) == tuple(expected_pipes), (
            f'`{el_id}` 的管线集合是 {pipes}，期望 {expected_pipes}'
        )
        assert source == expected_source, (
            f'`{el_id}` 的 source 条件是 {source!r}，期望 {expected_source!r} —— '
            '这一层就是改前 initProcessTypeToggle 里那个第二维度'
        )

    for el_id, expected in _EXTRA_ROWS.items():
        assert el_id in table, f'显隐表里没有 `{el_id}`'
        assert tuple(table[el_id][0]) == tuple(expected), (
            f'`{el_id}` 的管线集合是 {table[el_id][0]}，期望 {expected}'
        )


def test_every_table_row_has_a_container_in_the_template():
    """表里的每个 id 在模板里都有对应容器 —— 拼错一个字母不会静默失效。

    `applyPipeline` 对拿不到的 id 是 `if (!el) return;` 静默跳过（那是对的：
    独立页没有这个面板）。代价是表里拼错一个 id 完全无声 —— 那个字段组从此
    永远可见，四条管线都看得到它。
    """
    html = _template('index.html')
    table = parse_pipeline_table(_js('map.js'))
    missing = [el_id for el_id in table if f'id="{el_id}"' not in html]
    assert not missing, (
        f'显隐表里这些 id 在 index.html 里不存在：{missing} —— applyPipeline '
        '会静默跳过它们，那些字段组会在四条管线下全部常显'
    )


# ---------------------------------------------------------------------------
# 7. 四条载荷契约
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('pipeline', _PIPELINES)
def test_payload_keys_are_unchanged(pipeline):
    """四条管线的载荷键与合并前**逐字相同**。后端在消费它们。

    期望值抄自 src/routes 的必填清单（本文件顶部 `_PAYLOAD_KEYS`），是外部
    事实。IA 可以随便重排，字段名不行 —— 少一个是 400，改一个字母是「后端拿到
    未表态、静默回落到配置默认」，而后者要等作业真跑起来才发现。
    """
    got = parse_payload_keys(_js('map.js'))[pipeline]
    expected = _PAYLOAD_KEYS[pipeline]
    # 两条上传管线还会带互斥的 files / dem_task_id 之一。
    got_core = got - _SOURCE_EXCLUSIVE_KEYS
    assert got_core == expected, (
        f'{pipeline} 管线的载荷键变了：多出 {sorted(got_core - expected)}，'
        f'少掉 {sorted(expected - got_core)}。这些键名是后端契约'
        '（src/routes/ 里逐个 request.form.get / required_fields 在读），'
        'Task 5 的硬约束是「一个字段名都不许改」'
    )
    if pipeline in ('local_terrain', 'contour'):
        assert _SOURCE_EXCLUSIVE_KEYS <= got, (
            f'{pipeline} 管线没有同时保留 files 与 dem_task_id 两条来源分支'
            f'（实际 {sorted(got & _SOURCE_EXCLUSIVE_KEYS)}）—— 后端两条都认，'
            '且给两个就 400，所以前端必须二选一地发'
        )


def test_plugin_source_fields_stay_conditional():
    """插件源那两个字段仍然是**条件附加**，不进恒定键集。

    没选插件源时请求体必须与改造前逐字一致（一个字段都不多）—— 后端的快照
    冻结逻辑靠 `source_plugin_id and source_id` 同时在场才触发。
    """
    body = _js_block(_js('map.js'), 'async function submitDownload(')
    for key in ('source_plugin_id', 'source_id'):
        assert f'taskData.{key} =' in body, (
            f'submitDownload 里没有条件附加 taskData.{key} —— 插件源的快照'
            '冻结不会触发'
        )
    assert 'if (_regionSpec) taskData.region = _regionSpec;' in body, (
        '导入多边形的 region 字段没了 —— 只送 bbox 会把 L 形省界外面那一半'
        '也下下来，而那正是导入多边形要避免的事'
    )


# ---------------------------------------------------------------------------
# 8. 四个退役 id 全仓零命中
# ---------------------------------------------------------------------------

def test_retired_ids_are_gone_everywhere():
    """`downloadModal` / `processModal` / `processOpenBtn` / `processNameRow`
    在 templates/ 与 static/js/ 全仓零命中 —— **包括注释**。

    为什么连注释也不放过：留一个 `#processModal` 在注释里，下一个人 grep 它
    会找到一段讲历史的散文，然后花十分钟确认那不是活代码。历史该留在 commit
    message 与设计稿里，那里有完整上下文；源码里的 id 字面量只该指向活元素。
    """
    hits = []
    for root, _dirs, files in os.walk(_TEMPLATES):
        for name in sorted(files):
            path = os.path.join(root, name)
            text = _read(path)
            for dead in _RETIRED_IDS:
                if dead in text:
                    hits.append(f'{os.path.relpath(path, _ROOT)}: {dead}')
    for name in sorted(os.listdir(_JS)):
        if not name.endswith('.js'):
            continue
        text = _js(name)
        for dead in _RETIRED_IDS:
            if dead in text:
                hits.append(f'static/js/{name}: {dead}')
    assert not hits, (
        '这些退役 id 还有残留：\n  ' + '\n  '.join(hits)
        + '\n四条管线的入口已经收敛成一个 rail 按钮 + 一个 #createPanel，'
        '这四个名字不该再出现在源码里（注释也算）'
    )


def test_merged_synonym_fields_have_exactly_one_survivor():
    """四组同义字段各只剩一个，且退役的那半在模板里零命中。

    改前每一组都是两个「同 min/max 同语义」的控件，分别长在两张表单上：
      #taskName / #processTaskName、#zoomMin+#zoomMax / #processZoomMin+Max、
      #createTaskBtn / #createProcessBtn、#localTerrainFiles / #contourFiles。
    归一顺带修掉一个假旋钮：配置里的 default_zoom_min/max 此前只覆盖前一对
    （map.js 的 initMap），等高线那一腿改了没反应。
    """
    html = _template('index.html')
    js_sources = {name: _js(name) for name in sorted(os.listdir(_JS))
                  if name.endswith('.js')}
    merges = {
        'taskName': 'processTaskName',
        'zoomMin': 'processZoomMin',
        'zoomMax': 'processZoomMax',
        'createTaskBtn': 'createProcessBtn',
        'sourceFiles': 'localTerrainFiles',
        'sourceTifInfo': 'localTerrainTifInfo',
    }
    for survivor, retired in merges.items():
        assert f'id="{survivor}"' in html, f'合并后的 #{survivor} 不在模板里'
        assert html.count(f'id="{survivor}"') == 1, (
            f'#{survivor} 在模板里出现了 {html.count(f"id={survivor!r}")} 次 —— '
            'id 必须唯一'
        )
        assert retired not in _code_lines(html), (
            f'#{retired} 还在模板里 —— 同义字段没归一'
        )
        stale = [n for n, s in js_sources.items() if retired in _code_lines(s)]
        assert not stale, f'{stale} 里还在引用 #{retired}'
    # 第二张文件选择框与第二张信息卡一并退场。
    for retired in ('contourFiles', 'contourTifInfo'):
        assert retired not in html, f'#{retired} 还在模板里 —— 两张卡没合成一张'
    assert html.count('class="tif-info"') == 2, (
        f'index.html 里有 {html.count(chr(34).join(["class=", "tif-info", ""]))} '
        '处 class="tif-info"，期望 2（1 张信息卡 + #localTerrainEstimate 预告行）'
    )


def test_tif_info_mode_still_follows_the_pipeline():
    """一张卡，但 `updateTifInfo` 的 mode 仍**按管线**选。

    这是合并里最容易悄悄丢掉的一件事：地形切片按 Cesium 经纬度分块估建议层级、
    等高线按 Web Mercator 瓦片估，同一份 DEM 两者给出的数不一样
    （raster_probe._estimate_maxzoom）。传错的话卡片上写的层级与真正切出来的
    对不上 —— 比不显示更糟。
    """
    src = _js('map.js')
    body = _js_block(src, 'function updateSourceTifInfo(')
    assert "updateTifInfo('sourceFiles', 'sourceTifInfo'" in body, (
        'updateSourceTifInfo 没有指向合并后的 #sourceFiles / #sourceTifInfo'
    )
    assert '_currentPipeline()' in body, (
        'updateSourceTifInfo 没有读当前管线 —— mode 是写死的，一条管线的建议'
        '层级会用另一条的口径算'
    )
    assert "'contour'" in body and "'terrain'" in body, (
        f'updateSourceTifInfo 的两个 mode 字面量不全（函数体：{body!r}）'
    )
    for gone in ('updateLocalTerrainTifInfo', 'updateContourTifInfo'):
        assert gone not in src, f'map.js 里还有 {gone} —— 两个入口没合成一个'


def test_six_preflight_surfaces_survive():
    """六个预检面一个都没丢。

    它们是这张表单唯一的「提交前能看到后果」的地方，而合并最容易顺手丢掉的
    就是这类只在某一条管线下出现的东西：
      1. #tileEstimate           瓦片数预估
      2. 服务端多边形估算         含 _regionEstimateSeq 竞态闸门
      3. #createBoundsReadout    选区四至读数（见下）
      4. #sourceTifInfo          TIF 信息卡（两张合成一张）
      5. #localTerrainEstimate   起切规模预告
      6. 大任务二次确认           超软阈值时把预计耗时摆给用户

    2026-08-15 工具条瘦身，第 3 面换锚点：
    · 改前钉 `#createPanelBounds` —— 面板选区段里那句**只读**四至摘要。
    · 为什么换：地图右上角的 `#boundsInfo` 浮层整块退场，可编辑的四至读数
      搬进了同一格。同一个四至原本在两处各渲染一遍（浮层可编辑 / 面板只读），
      读数进来之后那句只读摘要就是第二处渲染，连同 `.modal-bounds-summary`
      与 `js.map.download.bounds_summary` 一起删了。
    · 现在钉 `#createBoundsReadout`（`.bounds-readout`），并且额外要求它真的
      被 `updateBoundsInfo()` 填、填进去的是**可点编辑**的 `data-field` 单元格
      —— 模板里它是个空 div，只钉 id 存在等于钉一个空壳。
    预检面从「一句只读摘要」升级成「可编辑读数」，守的东西
    （提交前看得到选区四至）一个字没变。
    """
    html = _template('index.html')
    src = _js('map.js')
    for el_id in ('tileEstimate', 'createBoundsReadout', 'sourceTifInfo',
                  'localTerrainEstimate'):
        assert f'id="{el_id}"' in html, f'预检面 #{el_id} 从模板里消失了'

    readout = _js_block(src, 'function updateBoundsInfo(')
    assert "getElementById('createBoundsReadout')" in readout, (
        'updateBoundsInfo() 不再往 #createBoundsReadout 里渲染 —— 模板里那个 div'
        '是空的，宿主对不上就等于提交前看不到选区四至'
    )
    for field in ('north', 'south', 'east', 'west'):
        assert f'data-field="{field}"' in readout, (
            f'四至读数里缺 data-field="{field}" 的单元格 —— 这一面是**可编辑**读数，'
            '缺一格就既读不到也改不了那一边'
        )

    assert '_regionEstimateSeq' in src, (
        '服务端多边形估算的竞态闸门 _regionEstimateSeq 没了 —— 用户连着改层级时'
        '晚发的请求可能先回来，旧数字会盖在新选区上'
    )
    assert 'let _regionEstimateSeq = 0;' in src, '_regionEstimateSeq 的定义没了'

    forecast = _js_block(src, 'function renderTerrainTileEstimate(')
    assert "opt?.dataset.offset" in forecast, (
        '起切规模预告不再从 <option data-offset> 读档位偏移 —— 取值表'
        '（geo_validation.TILING_QUALITY_OFFSETS）只许有一份，'
        '在 map.js 里抄第二份之后改了档位偏移，预告与产物会静默对不上'
    )

    submit = _js_block(src, 'async function submitDownload(')
    assert 'showConfirm(' in submit and 'confirm_large' in submit, (
        '大任务二次确认没了 —— 瓦片数超软阈值时用户会在毫无预告的情况下'
        '建出一个几小时的任务'
    )
    assert 'await currentTileEstimate()' in submit, (
        '二次确认没有 await currentTileEstimate() —— 多边形区域的张数来自服务端，'
        '同步读会拿到 null 而把整条确认静默跳过'
    )


# ---------------------------------------------------------------------------
# 9. 入口收敛
# ---------------------------------------------------------------------------

def test_command_palette_entries_converge_on_the_panel():
    """命令面板不再列 `goto_history` / `goto_config`，两条创建命令直接调函数。

    改前它同时列「打开任务面板」+「前往历史记录页」、「打开配置面板」+「前往
    配置页」—— 同一件事的两种形态。`/history` 与 `/config` 两条路由**保留**
    （深链与打包可达性需要），只是不再从命令面板露出第二条路。

    `open_process` 改前是 `el('processOpenBtn').click()` 转点。转点式的 run 在
    那颗按钮被删掉之后会让整条命令变成死代码：guard 返回 false，命令从列表里
    静默消失，而只检查「i18n key 在不在」的测试照样绿。
    """
    src = _js('command_palette.js')
    registry = _js_block(src, 'var REGISTRY = [')
    for gone in ('goto_history', 'goto_config'):
        assert f"id: '{gone}'" not in registry, (
            f'命令面板里还有 {gone} —— 与「打开面板」那条是同一件事的两种形态'
        )
    for cmd in ('new_download', 'open_process'):
        assert f"id: '{cmd}'" in registry, f'命令 {cmd} 不在注册表里'

    # 两条都必须直接调 openCreatePanel，并预选管线。
    assert "window.openCreatePanel('map')" in registry, (
        "new_download 没有 window.openCreatePanel('map')"
    )
    assert "window.openCreatePanel('local_terrain')" in registry, (
        "open_process 没有 window.openCreatePanel('local_terrain')"
    )
    assert 'processOpenBtn' not in src, (
        'command_palette.js 还在引用 #processOpenBtn —— 那颗按钮已经删了，'
        'guard 会永远返回 false，命令静默消失'
    )
    assert 'openDownloadModal' not in src, 'command_palette.js 还在引用 openDownloadModal'


def test_task_row_deep_link_and_window_drop_both_preselect():
    """任务行深链与窗口拖放两条快捷路径都保留，且都**预选管线**。

    两条都不是唯一路径（rail 的「新建」是），但都是最短的一条：
      - 任务行「用它切地形」-> 预选「本地地形切片 + 已有高程任务 + 那个任务」
      - 把 .tif 拖进窗口     -> 预选「本地地形切片 + 上传文件」并喂好文件

    拖放那条的顺序不能反：#sourceFiles 装在 #sourceUploadRow 里，而那一行的
    可见性由显隐表按「管线 × 来源」决定 —— 默认管线是瓦片，那时整个
    #sourceField 是 hidden 的，先喂文件就是把它塞进一个用户看不见的控件里。
    """
    src = _js('map.js')
    deep = _js_block(src, 'async function openProcessForDemTask(')
    assert "openCreatePanel('local_terrain'" in deep, (
        'openProcessForDemTask 没有转给 openCreatePanel 并预选地形切片'
    )
    assert 'demTaskId' in deep, 'openProcessForDemTask 没有把任务 id 传下去'

    open_panel = _js_block(src, 'async function openCreatePanel(')
    assert "sourceEl.value = 'dem_task'" in open_panel, (
        'openCreatePanel 的预填分支没有把来源摆到「已有高程任务」'
    )
    assert 'await loadProcessDemTasks()' in open_panel, (
        'openCreatePanel 没有 await loadProcessDemTasks() —— 下拉还没填完选项'
        '就去 sel.value = id，选不中'
    )

    drop = _js('drop_process.js')
    assert "getElementById('createPanel')" in drop, (
        'drop_process.js 的首页守卫还指着退役的弹窗 —— 它会在首页空载'
    )
    body = _js_block(drop, 'function openLocalProcess(')
    assert "openCreatePanel('local_terrain')" in body, (
        '投放没有预选「本地地形切片」—— 文件会落进一个 hidden 的控件里'
    )
    assert body.index("openCreatePanel('local_terrain')") < body.index('input.files ='), (
        '投放先喂文件、后切管线 —— 顺序反了：喂的那一刻 #sourceUploadRow 还是'
        'hidden 的（默认管线是瓦片），用户看不到自己拖进去的文件'
    )


def test_one_submit_handler_dispatches_all_four_pipelines():
    """一个 submit 处理器，按管线分派到四条既有装配逻辑。"""
    src = _js('map.js')
    assert src.count("getElementById('taskForm')?.addEventListener('submit'") == 1, (
        '#taskForm 的 submit 监听不是恰好一个'
    )
    for gone in ("getElementById('downloadForm')", "getElementById('processForm')"):
        assert gone not in src, f'map.js 里还有 {gone} —— 两张表单必须合成一张'
    handler = src[src.index("getElementById('taskForm')?.addEventListener('submit'"):]
    handler = handler[:handler.index('\n});')]
    assert '_currentPipeline()' in handler, 'submit 分派器没有读当前管线'
    for fn in ('submitLocalTerrain()', 'submitContour()', 'submitDownload('):
        assert fn in handler, f'submit 分派器没有分派到 {fn}'


# ---------------------------------------------------------------------------
# 10. 标签与行为对齐
# ---------------------------------------------------------------------------

def test_selection_overlay_labels_match_what_the_buttons_do():
    """选区那一格的按钮，文案与行为一致 —— 而且只剩一颗。

    改前钉两颗：地图右上角选区浮层上的「下载」其实只是打开一张表单（标签承诺
    了一个它不做的动作）、「删除」清的是选区而 title 写「清除选区」（两个动词
    说同一件事，而「删除」还暗示删掉的是数据）。上一轮把它们改成
    「新建任务」（js.map.bounds.create_task）+「清除选区」（js.map.bounds.clear）。

    2026-08-15 工具条瘦身后为什么要换判据：浮层整块退场、读数搬进 #createPanel
    的选区段之后，「新建任务」成了**面板里指向面板自己**的入口，连同
    js.map.bounds.create_task / create_task_title 两个键一起删了。原判据
    `js_map['js.map.bounds.create_task']` 直接 KeyError —— 它守的那半（主按钮
    别拿动词撒谎）失去了对象。

    现在钉两件事，合起来还是同一个不变量「这一格里的每颗钮都名副其实」：
    1. 剩下那颗「清除选区」：可见文案与 title 同一个键 js.map.bounds.clear，
       而委托点击真的调 clearSelection() —— 说清除就只清除。
    2. 接住原来那半的语义：这一格里**不许再出现指向面板自己的入口**
       （updateBoundsInfo 生成的 markup 里不许有 openCreatePanel( 或
       #boundsCreateBtn）。文案与行为最彻底的不一致就是这一种：一颗写着
       「新建任务」的钮，点下去用户已经在的那张表单原地不动。这正是这次删它
       的理由，也是唯一能让它悄悄长回来的地方（markup 是 JS 拼的字符串，
       模板扫不到）。
    """
    from src.i18n.catalog.js_map import MESSAGES as js_map

    for gone in ('js.map.bounds.download', 'js.map.bounds.delete',
                 'js.map.bounds.clear_title',
                 # 2026-08-15 退役:「新建任务」那颗钮本身没了
                 'js.map.bounds.create_task', 'js.map.bounds.create_task_title',
                 # 同批退役:空态里的「手动输入范围」入口(面板里
                 # #createManualBoundsBtn 是同一个动作的另一处渲染)
                 'js.map.bounds.manual'):
        assert gone not in js_map, f'退役的键 {gone} 还在 js_map.py 里'

    clear = js_map['js.map.bounds.clear']
    assert clear['zh'] and clear['en'], 'js.map.bounds.clear 有一侧为空'

    # 可见文案与 title 必须是**同一个键**：两个键早晚会漂开。
    src = _js('map.js')
    overlay = _js_block(src, 'function updateBoundsInfo(')
    clear_btn = overlay[overlay.index('id="boundsClearBtn"'):]
    clear_btn = clear_btn[:clear_btn.index('</button>')]
    assert clear_btn.count("t('js.map.bounds.clear')") == 2, (
        '清除钮的可见文案与 title 不是同一个键（在它的标记里出现了 '
        f'{clear_btn.count(chr(39).join(["t(", "js.map.bounds.clear", ")"]))} 次）'
        ' —— 两个键早晚会漂开，那正是改前「删除」/「清除选区」的成因'
    )
    # 文案说「清除选区」，行为就得是清除选区：这块每次整块重渲染，按钮上挂不了
    # 直接监听，接线在 #createBoundsReadout 的委托点击里。
    delegated = _js_block(src, "boundsReadout.addEventListener('click'")
    assert re.search(r"closest\('#boundsClearBtn'\)[\s\S]{0,200}?clearSelection\(\)",
                     delegated), (
        '#boundsClearBtn 的委托点击没有落到 clearSelection() —— 文案承诺清除选区，'
        f'实际接线是别的东西（委托处理器：{delegated!r}）'
    )

    # 只看 markup，不看注释：这条钉的是「生成出来的按钮」，而不是禁止在注释里
    # 复述这段沿革（上面那段 docstring 本身就在复述）。
    markup = re.sub(r'/\*.*?\*/', '', re.sub(r'//[^\n]*', '', overlay), flags=re.S)
    for reentry in ('openCreatePanel(', 'boundsCreateBtn'):
        assert reentry not in markup, (
            f'updateBoundsInfo 生成的 markup 里又出现了 {reentry} —— 读数已经在'
            '#createPanel 里了，这一格里再放一颗「新建任务」就是一颗指向面板'
            '自己的钮：文案承诺打开表单，点下去什么都不动'
        )
    assert 'boundsDownloadBtn' not in src, (
        'map.js 里还有 #boundsDownloadBtn —— 主按钮的 id 也要跟着行为改名'
    )


# ---------------------------------------------------------------------------
# 红态探针：证明上面那些断言真的会红
#
# 每条探针把源码变异成「缺陷版」再喂给同一个解析器/判据，断言它当场失败。
# 不做这一步的话，一条永远绿的断言与一条真正在守的断言长得一模一样 ——
# 本仓 test_css_contract.py 那段「三个缺陷方向相反、互相抵消，于是假绿」
# 就是这么来的。
# ---------------------------------------------------------------------------

def test_red_probe_dropping_a_matrix_row_is_caught():
    """探针 1：显隐表少一行 -> 计数与逐格核对都必须红。

    这是最可能真实发生的退化：加第五条管线时漏掉某个字段组，界面上那一组
    从此在所有管线下常显，而没有任何像素会变化。
    """
    src = _js('map.js')
    table = parse_pipeline_table(src)
    assert 'mapStyleField' in table, '本探针的锚点没了 —— 探针已失效'

    mutated = re.sub(r"\n\s*\{ id: 'mapStyleField',[^\n]*\n", '\n', src, count=1)
    assert mutated != src, '变异没生效 —— 探针已失效'

    mutated_table = parse_pipeline_table(mutated)
    assert 'mapStyleField' not in mutated_table, '变异后那一行还在 —— 探针已失效'
    assert len(mutated_table) == len(table) - 1

    # 逐格核对必须当场发现缺行。
    with pytest.raises(AssertionError, match='mapStyleField'):
        for el_id in _DESIGN_MATRIX:
            assert el_id in mutated_table, (
                f'显隐表里没有字段组 `{el_id}` —— 设计稿 §2.4 的矩阵有他这一行'
            )


def test_red_probe_flipping_a_matrix_cell_is_caught():
    """探针 2：把一格从「隐藏」翻成「可见」-> 逐格核对必须红。

    比缺行更阴：表还是 13 行、计数照样对，只有那一格错。改前这类错误要靠人
    在浏览器里逐条管线点一遍才发现。
    """
    src = _js('map.js')
    mutated = src.replace(
        "{ id: 'mapStyleField', pipelines: ['map'] }",
        "{ id: 'mapStyleField', pipelines: ['map', 'contour'] }", 1)
    assert mutated != src, '变异没生效 —— 探针已失效'

    table = parse_pipeline_table(mutated)
    assert len(table) == (len(_DESIGN_MATRIX) + len(_SOURCE_CONDITIONED_ROWS)
                          + len(_EXTRA_ROWS)), '行数没变，正是本探针要模拟的情形'

    wrong = []
    for el_id, expected in _DESIGN_MATRIX.items():
        pipes, _source = table[el_id]
        for p in _PIPELINES:
            if (p in pipes) != (p in expected):
                wrong.append(f'{el_id} × {p}')
    assert wrong == ['mapStyleField × contour'], (
        f'逐格核对没抓到那一格翻转（抓到的是 {wrong}）—— 探针已失效'
    )


def test_red_probe_moving_the_submit_button_into_the_form_is_caught():
    """探针 3：把提交钮搬回表单里 -> 「在表单之外」的判据必须红。

    这是折叠线缺陷复发的唯一路径。结构一旦退回去，像素侧
    （test_css_contract.py 的五个前提）**不会**报警：那五条只看 CSS，
    提交钮在哪个父元素里它们一无所知。
    """
    html = _template('index.html')
    section = _section_of(html, 'createPanel')

    # 把整颗按钮从底条搬到 </form> 之前。
    btn_start = section.rindex('<button', 0, section.index('id="createTaskBtn"'))
    btn_end = section.index('</button>', btn_start) + len('</button>')
    btn = section[btn_start:btn_end]
    body = section[:btn_start] + section[btn_end:]
    mutated = body.replace('</form>', btn + '\n</form>', 1)

    form_start = mutated.index('id="taskForm"')
    form_end = mutated.index('</form>', form_start)
    btn_at = mutated.index('id="createTaskBtn"')
    assert form_start < btn_at < form_end, '变异没把按钮搬进表单 —— 探针已失效'

    with pytest.raises(AssertionError, match='config-footer'):
        assert not (form_start < btn_at < form_end), (
            '#createTaskBtn 落在 <form id="taskForm"> 里面了 —— 它必须留在'
            '.config-footer（表单之外），否则重新跟着内容滚动，提交钮会回到折叠线下'
        )


def test_red_probe_renaming_a_payload_key_is_caught():
    """探针 4：改掉一个载荷键 -> 载荷断言必须红。

    「一个字段名都不许改」是 Task 5 的硬约束，而改错的后果是静默的：
    后端把缺席的字段当「未表态」回落到配置默认，用户填的值不算数，
    要等作业真跑起来才发现。
    """
    src = _js('map.js')
    mutated = src.replace("fd.append('vertex_normals'", "fd.append('vertexNormals'", 1)
    assert mutated != src, '变异没生效 —— 探针已失效'

    got = parse_payload_keys(mutated)['local_terrain'] - _SOURCE_EXCLUSIVE_KEYS
    expected = _PAYLOAD_KEYS['local_terrain']
    assert got != expected, '改了键名却没被解析出来 —— 探针已失效'
    assert sorted(expected - got) == ['vertex_normals']
    assert sorted(got - expected) == ['vertexNormals']


# ---------------------------------------------------------------------------
# 合并带来的三个后果，每个一条契约（2026-08-15 定向复审）。
#
# 三条都不是「像素对不对」，是「四条管线并进一张表单之后，原本各表单私有的
# 前提失效了」——review 里三条都实测复现过，所以断言写在行为层而不是文本层。
# ---------------------------------------------------------------------------

_NODE = shutil.which('node')

# 带原生约束的控件：它们是 `hidden` 与 `disabled` 之差能不能咬人的**全部**入口。
# `#taskName` 是刻意的例外：全表单只留这一个 required，而它对四条管线都可见
# （理由写在 templates/index.html 那条注释里）。
_CONSTRAINT_ATTRS = ('required', 'min=', 'max=', 'minlength', 'maxlength',
                     'pattern=', 'step=')
_ALWAYS_VISIBLE_CONSTRAINED = {'taskName'}


def _constrained_controls(html):
    """[(id, 行号, 命中的约束属性)]，只挑真带约束的表单控件。"""
    out = []
    for m in re.finditer(r'<(?:input|select|textarea)\b([^>]*)>', html):
        attrs = m.group(1)
        hits = [c for c in _CONSTRAINT_ATTRS if c in attrs]
        if not hits:
            continue
        el_id = re.search(r'id="([^"]+)"', attrs)
        out.append((el_id.group(1) if el_id else None,
                    html[:m.start()].count('\n') + 1, hits))
    return out


def _div_span(html, el_id):
    """`<div ... id="el_id">` 到它配对 `</div>` 的 [start, end)。

    按 div 深度数，不用 HTML 解析器：本仓没有解析器依赖，而字段组一律是
    `<div id=...>`。找不到返回 None（调用方据此响亮失败，不静默跳过）。
    """
    m = re.search(r'<div\b[^>]*\bid="' + re.escape(el_id) + r'"[^>]*>', html)
    if not m:
        return None
    depth = 0
    for tag in re.finditer(r'<div\b[^>]*>|</div>', html[m.start():]):
        depth += 1 if tag.group(0).startswith('<div') else -1
        if depth == 0:
            return m.start(), m.start() + tag.end()
    return None


def test_every_constrained_control_sits_where_the_table_can_disable_it():
    """带 min/max/required 的控件必须落在显隐表管得着的字段组里。

    `hidden` 不等于 `disabled`：藏起来的受约束控件照样参与原生表单校验，
    浏览器会拦下 submit 事件，而气泡挂不到不渲染的元素上 —— 「创建任务」点了
    完全没反应，且**四条管线一起废**。实测（Chromium 2026-08-15）：`#zoomMax`
    填 25 再切到高程管线（`zoomField` 被藏），`form.checkValidity()` 为 false、
    submit 监听器一次都不触发；值改回 15 立刻恢复。

    弹窗时代这条不会咬人：两张表单各有一对缩放框，跨表单污染不了。合并成一张
    之后，用户在等高线字段里敲的一个数能把瓦片任务的提交废掉。

    所以判据是**位置**：每个受约束控件要么在显隐表的某个组里（`applyPipeline`
    藏它时会连带 disable，那才真的退出校验），要么就是那个对四条管线都可见的
    `#taskName`。加一个受约束控件到别处（比如一个只靠 JS 翻 `hidden` 的容器）
    会在这里响亮失败，而不是等用户点到一颗死按钮。
    """
    html = _template('index.html')
    table = parse_pipeline_table(_js('map.js'))
    spans = {}
    for group_id in table:
        span = _div_span(html, group_id)
        if span:
            spans[group_id] = span

    controls = _constrained_controls(html)
    assert len(controls) >= 5, (
        f'只扒到 {len(controls)} 个受约束控件 —— 本断言已失效（表单里至少有 '
        '#taskName + 两个缩放框 + 层级 + 等高距）'
    )

    orphans = []
    for el_id, line, hits in controls:
        if el_id in _ALWAYS_VISIBLE_CONSTRAINED:
            continue
        at = html.index(f'id="{el_id}"') if el_id else None
        if at is None:
            orphans.append(f'index.html:{line} 无 id 的受约束控件 {hits}')
            continue
        if not any(start <= at < end for start, end in spans.values()):
            orphans.append(f'index.html:{line} #{el_id} {hits}')
    assert not orphans, (
        '这些受约束控件不在显隐表的任何字段组里 —— 它们被藏起来时不会被 '
        'disable，一个越界值就能让「创建任务」静默失效（submit 事件根本不派发）：\n'
        + '\n'.join('  ' + o for o in orphans)
        + f'\n（表里的组：{sorted(spans)}；刻意的例外只有 '
          f'{sorted(_ALWAYS_VISIBLE_CONSTRAINED)}，它对四条管线都可见）'
    )


def test_apply_pipeline_disables_what_it_hides():
    """`applyPipeline` 藏一个组的同一句里必须 disable 它 —— 两件事不许脱钩。

    只断言「文件里出现过 `_setGroupControlsDisabled`」是不够的：它必须收到与
    `el.hidden` **同一个** 判定结果，否则显隐和禁用会各算一遍、迟早分叉。
    """
    apply_body = _js_block(_js('map.js'), 'function applyPipeline(')
    m = re.search(r'el\.hidden\s*=\s*(\w+);\s*\n\s*_setGroupControlsDisabled\('
                  r'el,\s*(\w+)\)', apply_body)
    assert m, (
        'applyPipeline 里没有「藏它 + 用同一个判定结果 disable 它」这对相邻语句 —— '
        'hidden 不等于 disabled，被藏的 min/max 控件会继续参与原生校验并拦下 submit'
    )
    assert m.group(1) == m.group(2), (
        f'el.hidden 用的是 {m.group(1)}、disable 用的是 {m.group(2)} —— '
        '两个判定结果必须是同一个变量，否则显隐与禁用会分叉'
    )


@pytest.mark.skipif(_NODE is None, reason='node 不可用，跳过 JS 行为断言')
def test_group_disable_helper_restores_the_prior_disabled_state():
    """抠出 `_setGroupControlsDisabled` 用 node 跑真行为。

    三件事：藏 -> 全禁用；出来 -> 还原成**进入隐藏态之前**的那个值（不是无条件
    解禁）；重复藏不覆盖已记下的值。第二条是承重的：`#localTerrainMaxzoom` 的
    disabled 归自动挡那套逻辑管，无条件解禁会把它拨反 —— 用户勾着「自动」，
    切一圈管线回来数字框却能填了。
    """
    src = _js('map.js')
    fn = ('function _setGroupControlsDisabled(group, disabled) '
          + _js_block(src, 'function _setGroupControlsDisabled('))
    script = fn + """
function el(disabled) {
    return { disabled: disabled, dataset: {} };
}
const free = el(false);       // 本来可用
const locked = el(true);      // 本来就被别的逻辑禁着（自动挡）
const group = { querySelectorAll: () => [free, locked] };

_setGroupControlsDisabled(group, true);
const hidden = [free.disabled, locked.disabled];
_setGroupControlsDisabled(group, true);   // 再藏一次，不许覆盖记录
_setGroupControlsDisabled(group, false);
const shown = [free.disabled, locked.disabled];
const leftover = [free.dataset.hiddenDisabled, locked.dataset.hiddenDisabled];
console.log(JSON.stringify({ hidden, shown, leftover }));
"""
    out = subprocess.run([_NODE, '-e', script], capture_output=True,
                         text=True, encoding='utf-8', timeout=120)
    assert out.returncode == 0, f'node 跑不起来：{out.stderr}'
    got = json.loads(out.stdout)
    assert got['hidden'] == [True, True], (
        f'进入隐藏态后 disabled 是 {got["hidden"]} —— 组里每个受约束控件都必须被 '
        'disable，否则它继续参与校验'
    )
    assert got['shown'] == [False, True], (
        f'出来之后 disabled 是 {got["shown"]}，期望 [False, True] —— 第二个本来'
        '就被禁着（自动挡），无条件解禁会把它拨反'
    )
    assert got['leftover'] == [None, None], (
        f'dataset 残留 {got["leftover"]} —— 记录必须随还原一起删掉，否则下一轮'
        '读到的是上一轮的旧值'
    )


@pytest.mark.skipif(_NODE is None, reason='node 不可用，跳过 JS 行为断言')
def test_contour_gets_the_auto_max_zoom_back_and_tiles_keep_a_number():
    """等高线的「最大层级」默认留空 = 自动；其它管线必须有数。

    这一对字段是四条管线共用的，但两条腿对空值的解释相反：
    等高线留空 = 按 DEM 分辨率自动算（`contour_api.py` 把空值当未表态），
    瓦片留空会 `parseInt` 出 NaN。

    弹窗时代等高线有自己的 `#processZoomMax`（无 value + placeholder=自动），
    归一成同一个 `#zoomMax`（出厂 `value="15"`）之后自动挡从界面上再也走不到：
    不碰缩放框建等高线任务，从「按分辨率算」变成写死 15 —— 30m 一类的粗源
    多出成十倍瓦片、全是上采样出来的假细节，而 `#zoomAutoHint` 就在旁边写着
    「留空自动」。

    第三条 case 是另一半：用户亲手改过的层级不许被代管覆盖。
    """
    src = _js('map.js')
    fn = ('function _syncZoomMaxDefault(pipeline) '
          + _js_block(src, 'function _syncZoomMaxDefault('))
    script = """
let _zoomMaxFactory = '15';
let box = null;
const document = { getElementById: (id) => (id === 'zoomMax' ? box : null) };
""" + fn + """
function run(pipeline, value, userEdited) {
    box = { value: value, dataset: userEdited ? { userEdited: '1' } : {} };
    _syncZoomMaxDefault(pipeline);
    return box.value;
}
console.log(JSON.stringify({
    contour: run('contour', '15', false),
    map: run('map', '', false),
    dem: run('dem', '', false),
    local_terrain: run('local_terrain', '', false),
    contour_user_edited: run('contour', '9', true),
    map_user_edited: run('map', '21', true),
}));
"""
    out = subprocess.run([_NODE, '-e', script], capture_output=True,
                         text=True, encoding='utf-8', timeout=120)
    assert out.returncode == 0, f'node 跑不起来：{out.stderr}'
    got = json.loads(out.stdout)
    assert got['contour'] == '', (
        f'等高线管线下 #zoomMax 是 {got["contour"]!r} —— 必须留空，那是「按 DEM '
        '分辨率自动算」的唯一表达方式（contour_api 把空值当未表态）'
    )
    for pipeline in ('map', 'dem', 'local_terrain'):
        assert got[pipeline] == '15', (
            f'{pipeline} 管线下 #zoomMax 是 {got[pipeline]!r} —— 非等高线管线'
            '留空会 parseInt 出 NaN，必须写回出厂默认值'
        )
    assert got['contour_user_edited'] == '9' and got['map_user_edited'] == '21', (
        f'亲手改过的层级被代管覆盖了：{got["contour_user_edited"]!r} / '
        f'{got["map_user_edited"]!r} —— 切一次管线就抹掉用户填的数'
    )


def test_contour_submits_the_raw_max_zoom_so_empty_still_means_auto():
    """等高线提交的是 `#zoomMax` 的**原值**，不是 parseInt。

    上面那条保证「默认留空」，这条保证留空能一路传到后端：`parseInt('')` 是
    NaN、`String(NaN)` 是 `'NaN'`，`contour_api.py` 的 `zoom_max_raw not in
    (None, "")` 就不再成立，自动挡照样丢掉，而界面上一切正常。
    """
    src = _js('map.js')
    body = _js_block(src, 'async function submitContour(')
    m = re.search(r"fd\.append\('zoom_max',\s*([^)]+)\)", body)
    assert m, 'submitContour 里找不到 zoom_max 的装配 —— 本断言已失效'
    expr = m.group(1)
    assert 'parseInt' not in expr and 'Number(' not in expr, (
        f'submitContour 把 zoom_max 装成 {expr.strip()} —— 空值经数值转换会变成 '
        'NaN 字面量，后端就不再把它当「未表态」，自动挡静默丢失'
    )


def test_the_form_submit_takes_the_lock_before_any_await():
    """提交在飞守卫必须在**任何 await 之前**上锁，且三条装配不再各写一份。

    改前 `submitDownload` 的锁点排在 `await currentTileEstimate()` 之后（多边形
    选区的张数来自服务端），那段往返里按钮完全可点 —— 连点两次就是两发
    `POST /api/tasks`、两个一模一样的下载任务。现在整条 submit 走 `ui.js` 的
    `guard()`：它先挂 in-flight 标志再执行，标志挡的正是 `disabled` 挡不住的
    那几条路（回车重复提交、程序化调用）。
    """
    src = _code_lines(_js('map.js'))
    handler = _js_block(src, "getElementById('taskForm')?.addEventListener('submit'")
    assert 'guard(' in handler, (
        '#taskForm 的 submit 处理器没有走 guard() —— 提交去重回到了各条装配'
        '自己手写 disabled 的老路'
    )
    assert handler.index('guard(') < handler.index('await'), (
        'guard() 排在第一个 await 之后 —— 锁点必须在任何往返之前，否则那段'
        '往返里连点两次就建两个任务'
    )
    for fn_name in ('async function submitDownload(', 'async function submitContour(',
                    'async function submitLocalTerrain('):
        body = _js_block(src, fn_name)
        assert 'btn.disabled' not in body, (
            f'{fn_name.split()[-1]} 里还留着手写的 btn.disabled —— 上锁只能有一处'
            '（guard），两处会互相还原对方的状态'
        )
