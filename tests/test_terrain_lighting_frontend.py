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

    瓦片里的 oct 法线段是无条件落盘的，所以少了这个选项**不会**有任何
    错误信号：瓦片照下、解析照过、layer.json 照样声明 octvertexnormals，
    只是 Cesium 把那段字节跳过去，光照开关退化成全球日夜渐变。
    这一行被「顺手清理」掉不会红任何别的测试。
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
    subprocess.run(
        ['node', '--check', LIGHTING_JS],
        capture_output=True, text=True, check=True, timeout=30,
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
