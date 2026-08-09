"""地形光照 + 逐顶点法线的**前端契约**测试（源码级 + 渲染级文本断言）。

为什么这个文件必须存在
----------------------
这条链路上的每一环坏掉都是**静默**的：HTTP 全 200、瓦片照常下发、
控制台零报错、按钮照常能点 —— 只是地形不亮。本项目在同一条地形链路上
已经栽过五次同形态的坑（triangleCount 写错导致地形从未真正渲染过、
RTIN 绕向反了、boundingSphere 包不住瓦片、triangulator 拼错静默走 else、
uint32 索引段缺对齐 padding），共同点都是「没有任何自动化信号」。

本文件钉住的是**契约点**（选项名、key 名、id 名、类名、调用存在性），
不是某段具体实现文本。真实渲染效果由人工/Playwright 验证覆盖
（项目刻意没有 JS 测试框架，见 tests/test_map_js_contract.py 头注）。

四条承重断言，每条对应一种「点了没反应」：
  1. fromUrl 必须传 requestVertexNormals: true —— 少了它，vendored Cesium
     1.143.0 的解码 worker 会跳过 oct 法线扩展段
     （`extensionId === OCT_VERTEX_NORMALS && _requestVertexNormals` 双条件）、
     `provider.hasVertexNormals` getter（`_hasVertexNormals &&
     _requestVertexNormals`）恒 false、着色器于是选 ENABLE_DAYNIGHT_SHADING
     而非 ENABLE_VERTEX_LIGHTING。开关变成只有一层随太阳方位的明暗渐变。
  2. fromUrl 的第一个实参必须是目录而不是 `.../layer.json` —— 它内部
     appendForwardSlash 后再拼 layer.json，传后者会请求
     `.../layer.json/layer.json` 得 404，**且不 reject 而是静默降级**。
  3. 切换后必须 requestRender() —— map.js 把 requestRenderMode 设成 true、
     maximumRenderTimeChange 设成 Infinity，而 Globe.enableLighting 是普通
     数据字段（Cesium 构造函数直接赋值、无 setter），改了不会自己重绘。
  4. 按下态的类名必须是 style.css 里真有规则的那个 —— 写成通用的
     `.active` 不报错也不生效，按钮变哑开关。
"""

import importlib
import logging
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAP_JS = os.path.join(ROOT, 'static', 'js', 'map.js')
LIGHTING_JS = os.path.join(ROOT, 'static', 'js', 'terrain_lighting.js')
BASE_HTML = os.path.join(ROOT, 'templates', 'base.html')
INDEX_HTML = os.path.join(ROOT, 'templates', 'index.html')
STYLE_CSS = os.path.join(ROOT, 'static', 'css', 'style.css')

STORAGE_KEY = 'tf-terrain-lighting'


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _map_js():
    return _read(MAP_JS)


def _lighting_js():
    return _read(LIGHTING_JS)


def _strip_js_comments(src):
    """剥掉 // 与 /* */ 注释。

    本文件几乎每条断言都要在**代码**里找一个字符串，而这些字符串在注释里
    也被逐字讨论过（本仓 tests/test_css_contract.py 的
    style-css-comments-break-naive-greps 教训：注释里逐字复述代码会让裸
    grep 全部假绿）。剥注释后再断言，删掉真实代码就一定红。
    """
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'(^|[^:])//[^\n]*', r'\1', src)


def _load_app(monkeypatch, tmp_path):
    """Config 副作用全部重定向到 tmp_path，再新鲜 import app（项目统一套路）。"""
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _from_url_call(src):
    """抠出 `Cesium.CesiumTerrainProvider.fromUrl(` 的完整实参串（按括号配对）。

    正则匹配不到就响亮失败——调用形态变了本文件即失效，不能退化成空断言。
    """
    marker = 'Cesium.CesiumTerrainProvider.fromUrl('
    assert src.count(marker) == 1, (
        f'map.js 里 {marker} 出现 {src.count(marker)} 次，预期恰好 1 次 —— '
        '多处创建地形 provider 时本文件只覆盖得到其中一处，必须同步更新'
    )
    i = src.index(marker) + len(marker)
    depth = 1
    for k in range(i, len(src)):
        if src[k] == '(':
            depth += 1
        elif src[k] == ')':
            depth -= 1
            if depth == 0:
                return src[i:k]
    raise AssertionError('fromUrl 调用的括号不配对——本测试已失效')


# ---------------------------------------------------------------------------
# 1) map.js：请求法线数据（本文件最承重的一条）
# ---------------------------------------------------------------------------

def test_terrain_provider_requests_vertex_normals():
    """`CesiumTerrainProvider.fromUrl` 必须传 `requestVertexNormals: true`。

    随包底图与显式开了法线的任务瓦片都带 oct 法线段（应用侧 TileParams.normals
    默认关，但 build_terrain 默认仍出），所以少了这个选项**不会**有任何错误
    信号：瓦片照下、解析照过、layer.json 照样声明 octvertexnormals，只是
    Cesium 把那段字节跳过去，光照开关退化成全球日夜渐变。
    更糟的是 hasVertexNormals 是 provider 级单一标志：一个不带法线的任务图层
    就能把随包底图的法线一起作废，所以这一行只会更重要，不会更可有可无。
    它被「顺手清理」掉不会红任何别的测试。
    """
    args = _from_url_call(_strip_js_comments(_map_js()))
    assert re.search(r'requestVertexNormals\s*:\s*true', args), (
        'fromUrl 没有传 requestVertexNormals: true —— Cesium 会跳过瓦片里的 '
        'oct 法线扩展段，hasVertexNormals 恒 false，地形光照开关只剩一层'
        '随太阳方位的明暗渐变，且全程不报错'
    )


def test_terrain_provider_url_is_the_directory_not_layer_json():
    """fromUrl 的第一个实参必须是目录（`base`），不能是 `${base}/layer.json`。

    fromUrl 内部 appendForwardSlash 后再拼 layer.json；传 layer.json 会去请求
    `.../layer.json/layer.json` 得 404，**而且它不 reject** —— 静默按默认假设
    建 provider，随后瓦片全 404，前端毫无提示。加了 options 参数后这一行更
    容易被顺手改写，所以钉住它。
    """
    args = _from_url_call(_strip_js_comments(_map_js()))
    first = args.split(',')[0].strip()
    assert first == 'base', (
        f'fromUrl 的第一个实参是 {first!r}，必须是目录变量 `base`；'
        '传 `${base}/layer.json` 会请求 .../layer.json/layer.json 得 404，'
        '而 fromUrl 不 reject，会静默降级成一个瓦片全 404 的 provider'
    )


def test_map_js_hands_the_viewer_to_the_lighting_module():
    """map.js 必须把 viewer 交给 TerrainLighting —— 否则模块加载了也是空转。

    没有这一行：按钮点得动、aria-pressed 也会翻、localStorage 也写得进，
    但 `scene.globe.enableLighting` 永远没人设。纯粹的哑开关。
    """
    src = _strip_js_comments(_map_js())
    assert re.search(r'TerrainLighting\s*\.\s*init\s*\(\s*viewer\s*\)', src), (
        'map.js 没有 TerrainLighting.init(viewer) —— 光照模块拿不到 viewer，'
        '按钮状态会切、渲染不会变'
    )


# ---------------------------------------------------------------------------
# 2) terrain_lighting.js：开关真的作用到 Cesium 上
# ---------------------------------------------------------------------------

def test_lighting_module_sets_globe_enable_lighting():
    """模块必须写 `scene.globe.enableLighting` —— 这是 Cesium 侧唯一的开关。"""
    src = _strip_js_comments(_lighting_js())
    assert re.search(r'globe\s*\.\s*enableLighting\s*=', src), (
        'terrain_lighting.js 没有给 scene.globe.enableLighting 赋值 —— '
        '开关没有作用到渲染上'
    )


def test_lighting_module_requests_a_render_after_toggling():
    """改完 enableLighting 必须显式 `requestRender()`。

    map.js 设了 `requestRenderMode = true` + `maximumRenderTimeChange =
    Infinity`：场景只在被请求时重绘，也不会因为时间流逝自己重绘。而
    `Globe.enableLighting` 在 Cesium 里是普通数据字段（构造函数直接赋值，
    没有触发 requestRender 的 setter）。漏掉这一句的症状是「点了要等拖动
    地图才变」—— 用户会当成开关坏了。

    断言 requestRender 与 enableLighting 落在**同一个函数体**内，
    防止有人把它挪到某个不会被切换路径走到的地方。
    """
    src = _strip_js_comments(_lighting_js())
    # 从 enableLighting 赋值处向后找到本函数结束（下一个顶格 `}` 之前）
    m = re.search(r'globe\s*\.\s*enableLighting\s*=.*?\n\s*\}', src, re.S)
    assert m, 'terrain_lighting.js 里找不到 enableLighting 赋值所在的块——本测试已失效'
    assert re.search(r'requestRender\s*\(\s*\)', m.group(0)), (
        'enableLighting 赋值之后没有 scene.requestRender() —— '
        'requestRenderMode=true 下场景不会自己重绘，开关看起来像坏了'
    )


def test_lighting_preference_lives_in_localstorage_only():
    """偏好只存 localStorage key `tf-terrain-lighting`，不进 config 表。

    与 theme.js 同款：纯客户端渲染偏好，切换不需要重切片，也不该影响
    别的客户端。写进 config 表就成了全局设置，还要过一次 HTTP。
    """
    src = _strip_js_comments(_lighting_js())
    assert STORAGE_KEY in src, (
        f'terrain_lighting.js 里没有 localStorage key {STORAGE_KEY!r}（契约 key 名）'
    )
    assert 'setItem' in src and 'getItem' in src, (
        'terrain_lighting.js 没有 localStorage 读写 —— 偏好无法跨会话保持'
    )
    assert '/api/config' not in src and 'fetch(' not in src, (
        '光照偏好不该走服务端 config 表 —— 它是纯客户端渲染偏好（同 theme.js）'
    )


def test_lighting_defaults_to_off_when_localstorage_is_unreadable():
    """localStorage 读不到（隐私模式等）时必须回退到「关」，不能抛异常。

    get() 抛出去会打断 init()，map.js 里 `TerrainLighting.init(viewer)` 就在
    initMap 末尾 —— 抛在那儿会连带打断后续初始化。
    """
    src = _strip_js_comments(_lighting_js())
    m = re.search(r'function get\s*\([^)]*\)\s*\{(.*?)\n    \}', src, re.S)
    assert m, 'terrain_lighting.js 里找不到 get() 函数体——本测试已失效'
    body = m.group(1)
    assert 'try' in body and 'catch' in body, (
        'get() 没有 try/catch —— localStorage 不可用时会抛异常，'
        '连带打断 initMap 末尾的 TerrainLighting.init(viewer)'
    )
    assert re.search(r'return\s+false', body), (
        'get() 的 catch 分支没有 return false —— 默认必须是「关」'
    )


@pytest.mark.skipif(shutil.which('node') is None, reason='node 不可用，跳过 JS 语法校验')
def test_lighting_js_passes_node_syntax_check():
    """terrain_lighting.js 必须通过 node --check（项目 JS 验证套路）。"""
    assert os.path.exists(LIGHTING_JS), 'static/js/terrain_lighting.js 不存在——实现未落地'
    # timeout 放宽到 120 秒：Windows runner 上 node 冷启动 + 杀毒扫描能吃掉几十秒，
    # 30 秒曾把 v0.2.12 的发版构建打断过一次（同批的 test_map_js_contract）。
    # 这里保留「超时即失败」——它是语法校验，没有不依赖 node 的兜底断言。
    subprocess.run(
        ['node', '--check', LIGHTING_JS],
        capture_output=True, text=True, check=True, timeout=120,
    )


# ---------------------------------------------------------------------------
# 3) 按钮：id 两侧对得上、按下态的类名 style.css 里真有规则
# ---------------------------------------------------------------------------

def _button_id_from_js():
    """terrain_lighting.js 里 getElementById 查的那个 id。"""
    src = _strip_js_comments(_lighting_js())
    ids = set(re.findall(r'getElementById\(\s*["\']([^"\']+)["\']', src))
    ids |= set(re.findall(r'BUTTON_ID\s*=\s*["\']([^"\']+)["\']', src))
    assert len(ids) == 1, (
        f'terrain_lighting.js 里查了 {sorted(ids)} 这些 id，预期恰好 1 个——本测试已失效'
    )
    return ids.pop()


def test_lighting_button_id_matches_on_both_sides():
    """JS 查的 id 与 index.html 上的 id 必须是同一个。

    两边对不上不会报任何错：getElementById 返回 null，模块里每处都有
    `if (btn)` 守卫，按钮就成了一颗永远不接线的死按钮。
    """
    button_id = _button_id_from_js()
    html = _read(INDEX_HTML)
    assert re.search(r'id\s*=\s*"' + re.escape(button_id) + r'"', html), (
        f'index.html 里没有 id="{button_id}" 的元素 —— '
        'terrain_lighting.js 查的 id 与模板对不上，按钮永远不接线且零报错'
    )


def _button_static_classes():
    """index.html 上那颗光照按钮**静态**带的 class 集合。"""
    html = _read(INDEX_HTML)
    button_id = _button_id_from_js()
    m = re.search(r'<button\b[^>]*id\s*=\s*"' + re.escape(button_id) + r'"[^>]*>', html)
    assert m, f'index.html 里找不到 id="{button_id}" 的 <button> 开标签'
    cls = re.search(r'class\s*=\s*"([^"]*)"', m.group(0))
    assert cls, '光照按钮没有 class 属性——本测试已失效'
    return set(cls.group(1).split())


def test_lighting_button_active_class_actually_reaches_this_button():
    """按下态切的类名，必须真有一条**打得到这颗按钮**的 style.css 规则。

    「style.css 里存在 .X 规则」是不够的断言 —— 本测试第一版就是这么写的，
    结果把类名改成 Bootstrap 通用的 `.active` 后**照样绿**：style.css 里确实
    有 `.status-chip.active` 和 `.page-item.active .page-link`，只是它们
    永远打不到 `.map-panel-btn`。地图工具条的点亮类是 `.map-panel-btn--active`
    （style.css 里唯一给这族按钮上色的规则）。切错类名不报错、不影响功能，
    只是用户看不出开关是开是关 —— 典型的哑状态。

    所以这里判的是**可达性**：把选择器按后代/子代拆成复合选择器，取作用
    对象（最后一个复合），要求它用到的每个类都在「按钮静态 class ∪ 被切的类」
    里。`.status-chip.active` 需要按钮同时有 status-chip，不满足 => 判红。
    """
    src = _strip_js_comments(_lighting_js())
    classes = set(re.findall(r'classList\.(?:toggle|add|remove)\(\s*["\']([^"\']+)["\']', src))
    classes |= set(re.findall(r'ACTIVE_CLASS\s*=\s*["\']([^"\']+)["\']', src))
    assert classes, (
        'terrain_lighting.js 没有切换任何 class —— 按钮不会显示开/关状态'
    )

    css = re.sub(r'/\*.*?\*/', '', _read(STYLE_CSS), flags=re.S)
    selectors = []
    for m in re.finditer(r'([^{}]+)\{', css):
        sel = ' '.join(m.group(1).split())
        if sel.startswith('@'):
            continue
        selectors.extend(s.strip() for s in sel.split(','))

    static = _button_static_classes()
    for cls in classes:
        reachable = []
        for sel in selectors:
            # 作用对象 = 最后一个复合选择器（后代 / 子代 / 兄弟组合符之后那段）
            subject = re.split(r'\s*[>+~]\s*|\s+', sel)[-1]
            used = set(re.findall(r'\.([A-Za-z0-9_-]+)', subject))
            if cls in used and used <= (static | {cls}):
                reachable.append(sel)
        assert reachable, (
            f'terrain_lighting.js 切的类名 .{cls} 在 style.css 里没有任何'
            f'能打到这颗按钮的规则（按钮静态 class = {sorted(static)}）—— '
            '按下态是隐形的。地图工具条的点亮类是 .map-panel-btn--active'
        )


def test_lighting_button_reports_pressed_state_to_screen_readers():
    """按钮必须维护 aria-pressed（切换类按钮的无障碍状态）。

    模板上的初值与 JS 的更新缺任一半，读屏就会一直念旧状态。
    """
    html = _read(INDEX_HTML)
    button_id = _button_id_from_js()
    m = re.search(r'<button\b[^>]*id\s*=\s*"' + re.escape(button_id) + r'"[^>]*>', html)
    assert m, f'index.html 里找不到 id="{button_id}" 的 <button> 开标签'
    assert 'aria-pressed' in m.group(0), (
        '光照按钮的模板上缺 aria-pressed 初值 —— 读屏读不出这是个切换按钮'
    )
    assert re.search(r'aria-pressed', _strip_js_comments(_lighting_js())), (
        'terrain_lighting.js 没有更新 aria-pressed —— 状态变了读屏还念旧值'
    )


# ---------------------------------------------------------------------------
# 4) 脚本引入与加载顺序（源码级 + 渲染级）
# ---------------------------------------------------------------------------

def test_base_html_loads_terrain_lighting_js():
    """base.html 必须以 <script src> 引入 terrain_lighting.js。"""
    src = _read(BASE_HTML)
    assert re.search(r'<script[^>]+src=[^>]*terrain_lighting\.js', src), (
        'base.html 没有引入 static/js/terrain_lighting.js'
    )


def test_lighting_module_loads_before_map_js(monkeypatch, tmp_path):
    """渲染级：首页里 terrain_lighting.js 的 <script> 必须排在 map.js 之前。

    map.js 在 initMap 末尾调 `window.TerrainLighting.init(viewer)`，且那句
    有 `if (window.TerrainLighting)` 守卫 —— 顺序反了不会报错，只是守卫
    恒假，光照开关静默失效。base.html 的公共脚本区在 `{% block extra_js %}`
    之前，本测试钉住这个前后关系不被某次「脚本整理」打乱。
    """
    client = _load_app(monkeypatch, tmp_path)
    html = client.get('/').get_data(as_text=True)
    lighting = html.find('terrain_lighting.js')
    map_js = html.find('js/map.js')
    assert lighting != -1, '首页渲染结果里没有 terrain_lighting.js 的 <script src>'
    assert map_js != -1, '首页渲染结果里没有 map.js 的 <script src>——本测试已失效'
    assert lighting < map_js, (
        'terrain_lighting.js 排在 map.js 之后 —— map.js 里 '
        '`if (window.TerrainLighting)` 守卫会恒假，光照开关静默失效'
    )


def test_index_page_renders_the_lighting_button(monkeypatch, tmp_path):
    """渲染级：首页真的渲染出光照按钮（模板块被挪走/条件化会红）。"""
    button_id = _button_id_from_js()
    client = _load_app(monkeypatch, tmp_path)
    html = client.get('/').get_data(as_text=True)
    assert re.search(r'<button\b[^>]*id\s*=\s*"' + re.escape(button_id) + r'"', html), (
        f'首页渲染结果里没有 id="{button_id}" 的按钮 —— 光照开关没有入口'
    )


# ---------------------------------------------- 切片档位 / 法线开关的表单契约

def _local_terrain_options_block(html):
    """截出 #localTerrainOptions 这一段。

    为什么不在整页上断言：`js.` 前缀的 catalog 会被整份下发到页面里
    （client_catalog），而 js_history.py 的任务详情文案逐字讨论过同一批后果
    （「全球日夜渐变」「烘焙进瓦片」）。在整页上 grep 这些词是恒真的 —— 把
    表单里的提示整段删掉也照样绿。必须锁到控件自己那一段。
    """
    start = html.find('id="localTerrainOptions"')
    assert start != -1, '页面里没有 #localTerrainOptions —— 本地高程选项块没了'
    end = html.find('id="contourOptions"', start)
    assert end != -1, '#localTerrainOptions 之后找不到 #contourOptions —— 本测试的截取边界已失效'
    return html[start:end]


def test_tiling_preset_controls_exist_in_the_process_form():
    """档位下拉与法线复选框的 id 是接线契约 —— 改名会让提交静默丢参数。

    map.js 用 getElementById 取值，id 对不上时 `?.value` 返回 undefined，
    请求里就没有这个字段，后端取配置默认 —— 全程零报错，用户选的档位悄悄
    不生效。本仓栽过同形态的坑（见本文件头部清单）。
    """
    html = _read(INDEX_HTML)
    block = _local_terrain_options_block(html)
    # 必须落在 #localTerrainOptions 里：放进 #contourOptions 或表单别处，控件
    # 会跟着「等高线」显隐，选本地高程时根本看不见。
    assert 'id="localTerrainQuality"' in block, (
        '#localTerrainOptions 里没有 id=localTerrainQuality 的档位下拉')
    # 遍历取值表而不是写死三个名字：模板那三条 option 现在都是等值判断（兜底档
    # 已经收进 main._terrain_form_defaults），新增第四档时漏加一条 option，
    # 就是「配置里存着 X、下拉里没有 X、浏览器自动选中第一个（精细，体积 3.3 倍）」。
    # 写死名字的话这条断言对新增档位一无所知，只会一直绿。
    from src.services.geo_validation import TILING_QUALITY_OFFSETS
    for value in TILING_QUALITY_OFFSETS:
        assert f'value="{value}"' in block, f'档位下拉缺 {value} 选项'
    # 不能只断言 'type="checkbox"' 存在 —— index.html 本来就有别的复选框，
    # 那样写恒真、什么都保不住。必须锁定这一个控件本身。
    tag = re.search(r'<input[^>]*id="localTerrainNormals"[^>]*>', block)
    assert tag, '#localTerrainOptions 里找不到 id=localTerrainNormals 的输入控件'
    assert 'type="checkbox"' in tag.group(0), (
        f'法线控件不是复选框：{tag.group(0)}')


def test_preset_control_ids_match_on_both_sides():
    """两边各写各的 id 不会报任何错：getElementById 返回 null，参数悄悄不发。

    与 test_lighting_button_id_matches_on_both_sides 同一路数 —— 单边改名是
    这类文本契约最常见的回归。
    """
    js = _strip_js_comments(_map_js())
    html = _read(INDEX_HTML)
    for el_id in ('localTerrainQuality', 'localTerrainNormals'):
        assert f"getElementById('{el_id}')" in js, (
            f'map.js 没有取 {el_id} —— 提交时不会带上这个参数')
        assert f'id="{el_id}"' in html, (
            f'index.html 上没有 id="{el_id}" —— map.js 那一侧会取到 null')


def test_normals_checkbox_spells_out_what_turning_it_off_costs(monkeypatch, tmp_path):
    """渲染级：中英两种语种下，界面上都必须写明关掉法线的两条后果。

    这个勾选框是**不可逆**的：法线烘焙进瓦片，切完再想开只能重切（见
    `database.DEFAULT_CONFIGS` 里 terrain_vertex_normals 上方那条 ⚠️
    注释）。而且 Cesium 的 hasVertexNormals 是 provider 级单一
    标志 —— 这份地形没有法线，地图上的光照按钮就对整幅场景失效，连随包底图
    自带的法线也一并作废。用户在勾选那一刻看不到这两条，就会在几小时的切片
    之后才发现按钮点不亮。

    两种语种都要查：只查中文的话，把英文那份后果删光仍然全绿 —— 英文用户
    照样会在几小时之后才发现，而失效是静默的。语种走 cookie tf-lang
    （`src/i18n/__init__.py` 的 `get_locale`），且必须用 client.set_cookie 换：
    Werkzeug 3.1 的测试客户端有自己的 cookie jar，手写 headers={'Cookie': ...}
    会被它盖掉，页面照样渲染中文（本测试第一版就是这么写的，英文那半边恒真）。
    """
    client = _load_app(monkeypatch, tmp_path)

    zh = _local_terrain_options_block(client.get('/').get_data(as_text=True))
    assert '地形光照' in zh and '失效' in zh, (
        '中文：法线开关旁没有写「不勾选则地形光照按钮失效」—— 用户会以为只是省体积')
    assert '重新切片' in zh, (
        '中文：法线开关旁没有写「事后想开只能重新切片」—— 这个选择是不可逆的')

    client.set_cookie('tf-lang', 'en')
    en = _local_terrain_options_block(client.get('/').get_data(as_text=True))
    assert 'lighting' in en and 'stops working' in en, (
        f'英文：法线开关旁没有写清光照按钮会失效：{en}')
    assert 're-tiling' in en, (
        f'英文：法线开关旁没有写「事后只能重切」：{en}')


def test_preset_wording_anchors_to_the_base_level_like_the_detail_panel():
    """档位文案的参照物必须是「基准层级」，且与任务详情面板用同一套词。

    偏移表（`geo_validation.TILING_QUALITY_OFFSETS`）的 +1/0/-1 是相对**基准层级**
    算的，与 terrain_quality_preset 当前配成哪一档无关。写成「比默认多一级」的话，
    运维把默认改成 speed 之后这句话就是假的（`js_history.py` 里 quality_label
    上方那段注释已为详情面板定过同一口径）。两处用不同说法，用户会以为是两回事。
    """
    from src.i18n.catalog import MESSAGES
    from src.services.geo_validation import TILING_QUALITY_OFFSETS
    # 遍历取值表：新增第四档却忘了写文案时，这里 KeyError 报出缺的是哪一档，
    # 而不是让那一档带着空文案（或英文 key）上线。
    for suffix in TILING_QUALITY_OFFSETS:
        entry = MESSAGES[f'tpl.index.process.terrain_quality_{suffix}']
        assert '基准层级' in entry['zh'], (
            f'{suffix} 档的中文文案没写参照物「基准层级」：{entry["zh"]}')
        assert 'base level' in entry['en'], (
            f'{suffix} 档的英文文案没写参照物 base level：{entry["en"]}')
    hint = MESSAGES['tpl.index.process.terrain_quality_hint']
    # build_terrain 把结果钳到 [0, 21]：maxzoom=21 选精度档切出来还是 21。
    # 概率极低，但文案不能把「一定多一级」说死。
    # 断言整句钳位说明，而不是「文案里出现过 21 这两个数字」：层级上限、体积倍数
    # 里到处是数字，只查 '21' 的话，把钳位那半句整段删掉仍然全绿。
    assert '0 或 21 上限时不再偏移' in hint['zh'], (
        f'中文档位说明没有交代 0/21 边界会被钳住 —— 边界上选了档位却毫无变化，'
        f'用户只会当成 bug：{hint["zh"]}')
    assert '0 / 21 limits' in hint['en'] and 'offset is clamped' in hint['en'], (
        f'英文档位说明没有交代 0/21 边界会被钳住：{hint["en"]}')


def _option_tag(block, value):
    m = re.search(r'<option[^>]*value="' + re.escape(value) + r'"[^>]*>', block)
    assert m, f'档位下拉里找不到 value="{value}" 的 option'
    return m.group(0)


def test_preset_controls_render_the_configured_defaults(monkeypatch, tmp_path, caplog):
    """渲染级：三个控件（层级 / 档位 / 法线）的初值都必须跟着配置走，不能写死。

    同一个 DEM 任务有**两个**起切入口：这张表单（map.js 显式发 quality /
    vertex_normals）和历史页详情面板的起切按钮（不带 body，走配置默认）。
    初值写死就意味着同一份 DEM 从两个入口切出来的产物不一样 —— 层级不同、
    带不带法线不同，而界面上零提示。本仓给这个形态命过名：
    local_terrain_task_manager._default_quality 的「改了没反应的假旋钮」。

    法线那半边还是**不可逆**的：运维把 terrain_vertex_normals 配成 true，
    用户不动这个复选框，表单就会显式发 false 把它关掉，几小时切完之后光照
    按钮点不亮，只能重切。
    """
    client = _load_app(monkeypatch, tmp_path)
    from src.services.config_manager import ConfigManager
    cm = ConfigManager()
    assert cm.set('terrain_local_maxzoom', '16'), 'ConfigManager 没能写入层级配置'
    assert cm.set('terrain_quality_preset', 'speed'), 'ConfigManager 没能写入档位配置'
    assert cm.set('terrain_vertex_normals', 'true'), 'ConfigManager 没能写入法线配置'

    with caplog.at_level(logging.WARNING, logger='src.routes.main'):
        block = _local_terrain_options_block(client.get('/').get_data(as_text=True))
    # 三个值全合法，就一条 warning 都不许留：修复日志只在真丢了值时才有意义，
    # 每刷一次首页刷一条的话，真正的脏值会被淹在噪声里，等于没有日志。
    repaired = [r.getMessage() for r in caplog.records
                if 'terrain_local_maxzoom' in r.getMessage()
                or 'terrain_quality_preset' in r.getMessage()]
    assert not repaired, f'配置完全合法，却报告了「改用出厂默认」：{repaired}'
    assert 'selected' in _option_tag(block, 'speed'), (
        '配置是 speed，渲染出来的下拉却没选中它 —— 表单会把 balanced 显式发出去，'
        '同一个任务从详情面板起切却是 speed')
    assert 'selected' not in _option_tag(block, 'balanced'), (
        '两个 option 同时 selected，浏览器取最后一个 —— 初值就成了掷骰子')
    maxzoom = re.search(r'<input[^>]*id="localTerrainMaxzoom"[^>]*>', block)
    assert maxzoom and 'value="16"' in maxzoom.group(0), (
        f'配置 terrain_local_maxzoom=16，输入框却不是 16 —— 详情面板起切用 16、'
        f'这张表单发 14，同一份 DEM 两个入口切出不同层级：'
        f'{maxzoom.group(0) if maxzoom else "找不到控件"}')
    normals = re.search(r'<input[^>]*id="localTerrainNormals"[^>]*>', block)
    assert normals and 'checked' in normals.group(0), (
        f'配置开了法线，复选框却没勾上 —— 用户不动它就会显式关掉法线：'
        f'{normals.group(0) if normals else "找不到控件"}')


def test_preset_controls_fall_back_to_balanced_when_config_is_empty(monkeypatch, tmp_path):
    """渲染级：config={} 的异常兜底路径必须落在均衡，不能落在精细。

    main.index() 在渲染首页出任何异常时会用 `config={}` 再渲染一次，档位初值这时
    由 _terrain_form_defaults({}) 给出厂默认。给错的话（或者干脆不传这个变量），
    三条 `{% if %}` 全假，**浏览器会自动选中第一个 option**（精细）—— 默认档位从
    均衡悄悄变成体积 3.3 倍的那一档，没有任何提示。
    """
    client = _load_app(monkeypatch, tmp_path)
    from src.routes import main as main_route
    monkeypatch.setattr(main_route.config_manager, 'get_all',
                        lambda: (_ for _ in ()).throw(RuntimeError('boom')))

    block = _local_terrain_options_block(client.get('/').get_data(as_text=True))
    assert 'selected' in _option_tag(block, 'balanced'), (
        'config={} 兜底渲染时没有任何 option 被选中 —— 浏览器会自动选第一个'
        '（精细），默认档位静默变成体积 3.3 倍的那一档')
    assert 'selected' not in _option_tag(block, 'precision'), (
        'config={} 兜底渲染选中了精细档')
    normals = re.search(r'<input[^>]*id="localTerrainNormals"[^>]*>', block)
    assert normals and 'checked' not in normals.group(0), (
        f'config={{}} 兜底渲染把法线勾上了 —— 出厂默认是关：{normals.group(0) if normals else "找不到控件"}')


def test_quality_select_repairs_an_unknown_config_value_out_loud(monkeypatch, tmp_path,
                                                                 caplog):
    """渲染级：库里是没见过的档位值时仍渲染成均衡，但**必须**同时留下一条 warning。

    两半缺一不可。

    「仍渲染成均衡」：浏览器一定会选中某个 option，三条 `{% if %}` 全假时它自动
    选第一个（精细，体积 3.3 倍）—— 比落在均衡更糟。

    「必须留 warning」：均衡在这里是**修复**，不是用户配的值。同一个脏值从历史页
    详情面板起切（不带 body、走 validate_tiling_quality）是当场 400，从这张表单却
    照切不误 —— 一个入口硬拒、一个静默改写。没有日志的话，运维手里两个入口一个
    报错一个成功，无从判断库里到底存的是什么。修复点必须落在
    main._terrain_form_defaults：模板是这条链路上唯一记不了日志的一环。

    ConfigManager 挡得住走接口写进来的脏值（_VALUE_RULES 里那条
    `v in TILING_QUALITY_OFFSETS`），挡不住有人直接 sqlite3 改库，也挡不住以后
    新增第四档时忘了同步模板。
    """
    client = _load_app(monkeypatch, tmp_path)
    from src.routes import main as main_route
    monkeypatch.setattr(main_route.config_manager, 'get_all',
                        lambda: {'terrain_quality_preset': {'value': 'ultra'}})

    with caplog.at_level(logging.WARNING, logger=main_route.__name__):
        block = _local_terrain_options_block(client.get('/').get_data(as_text=True))

    assert 'selected' in _option_tag(block, 'balanced'), (
        "库里是 'ultra' 时没有任何 option 被选中 —— 浏览器会自动选第一个（精细）")
    assert 'selected' not in _option_tag(block, 'precision'), (
        "库里是 'ultra' 时选中了精细档")
    dropped = [r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING
               and 'terrain_quality_preset' in r.getMessage()]
    assert dropped, (
        "不认识的档位值被换成了均衡却没留下任何日志 —— 这正是本仓最不能容忍的"
        "「作业完成、HTTP 200、前端不报错、就是不对」")
    assert "'ultra'" in dropped[0] and 'balanced' in dropped[0], (
        f'日志里必须同时点名被丢弃的值和替换成的值，否则排查时还得靠猜：{dropped[0]}')


def test_out_of_range_maxzoom_is_clamped_out_loud(monkeypatch, tmp_path, caplog):
    """渲染级：库里的越界层级仍钳回出厂默认 14，但必须留下一条点名 99 的 warning。

    钳位本身是对的、绝不能去掉：terrain_local_maxzoom 登记在
    config_manager._UNCONSTRAINED_KEYS，PUT /api/config 收得下 99，照直渲染成
    value="99" 会违反控件自己的 min/max，让整张 #processForm 变 :invalid，
    「创建」点了没反应（tests/test_config_form_submittable.py 钉的就是这条）。

    问题只在于它此前是**静默**的：运维 PUT 了 99，打开处理表单看到 14，中间没有
    任何信号，一直要等到作业真跑起来才由 local_terrain_task_manager._default_maxzoom
    的那条 warning 吭一声。与 tests/test_terrain_api.py 的
    test_terrain_start_falls_back_when_configured_maxzoom_is_out_of_range 那条同形，
    只是这里守的是渲染入口。
    """
    client = _load_app(monkeypatch, tmp_path)
    from src.routes import main as main_route
    monkeypatch.setattr(main_route.config_manager, 'get_all',
                        lambda: {'terrain_local_maxzoom': {'value': '99'}})

    with caplog.at_level(logging.WARNING, logger=main_route.__name__):
        block = _local_terrain_options_block(client.get('/').get_data(as_text=True))

    maxzoom = re.search(r'<input[^>]*id="localTerrainMaxzoom"[^>]*>', block)
    assert maxzoom and 'value="14"' in maxzoom.group(0), (
        f'越界的 terrain_local_maxzoom=99 必须钳回 14，否则整张表单 :invalid：'
        f'{maxzoom.group(0) if maxzoom else "找不到控件"}')
    dropped = [r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING
               and 'terrain_local_maxzoom' in r.getMessage()]
    assert dropped, (
        '越界层级被丢弃却没留下任何日志 —— 运维看到的只是一个正常的 14，'
        '要等作业跑起来才知道自己配的 99 没生效')
    assert '99' in dropped[0] and '14' in dropped[0], (
        f'日志里必须同时点名被丢弃的值和替换成的值：{dropped[0]}')
