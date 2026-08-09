"""高程切片 TIF 信息卡的前端契约（文本级）。

本项目没有 JS 测试框架（理由见 tests/test_map_js_contract.py 头注释），所以
这些断言守的是「接线还在、且没有退化成上传整包」这类结构性质，数值行为由
tests/test_raster_inspect.py 的后端用例覆盖。

最要紧的一条是 test_inspect_is_never_an_upload：整个特性的存在理由就是
「不为看一眼元信息而先传一遍几百 MB 的 DEM」。哪天有人图省事把它改成
multipart 上传，功能看起来照常，代价却全落在用户的等待上。
"""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.i18n.catalog import MESSAGES  # noqa: E402

# 同目录的姊妹用例已经写好了 CSS 花括号扫描和 JS 函数体切分，重写一份只会
# 各自漂移（理由见 tests/test_tasks_js_contract.py 头部注释）。
from test_css_contract import (  # noqa: E402
    _decl_map, _js_function_body, _rules, _rules_ctx)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(*parts):
    with open(os.path.join(PROJECT_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _strip_js_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//.*$", "", src)


def _map_js():
    return _strip_js_comments(_read("static", "js", "map.js"))


def _reset_form_body():
    """resetForm 的函数体。不能用 _js_function_body：它取「函数名之后第一个 `{`」
    起手配对，而 resetForm 的参数表本身就是解构默认值（`{ clearBounds = true }`），
    切出来的是参数表 —— 断言会全部落在一段不含函数体的字符串上（永假）。"""
    src = _map_js()
    start = src.index("function resetForm(")
    open_at = src.index(") {", start) + 2
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at + 1:i]
    raise AssertionError("resetForm 花括号不配对 —— 本测试已失效")


# ------------------------------------------------------------------ 模板接线

def test_index_renders_the_tif_info_card_next_to_each_file_input():
    html = _read("templates", "index.html")
    # 信息卡必须紧跟在文件选择框之后、紧邻的层级/间距输入之前：它就是给
    # 「这些数填多少」提供依据的，排到后面等于用户已经填完才看到建议。
    assert (html.index('id="localTerrainFiles"')
            < html.index('id="localTerrainTifInfo"')
            < html.index('id="localTerrainMaxzoom"'))
    assert (html.index('id="contourFiles"')
            < html.index('id="contourTifInfo"')
            < html.index('id="contourInterval"'))
    assert html.count('class="tif-info"') == 2


def test_index_loads_the_geotiff_parser():
    html = _read("templates", "index.html")
    assert "js/geotiff_meta.js" in html


# ------------------------------------------------------------------ map.js 接线

def test_both_file_inputs_trigger_the_probe():
    src = _map_js()
    assert "addEventListener('change', updateLocalTerrainTifInfo)" in src
    assert "addEventListener('change', updateContourTifInfo)" in src


def test_each_form_asks_for_its_own_pipelines_zoom_estimate():
    """两条切片管线的分块方式不同，mode 传错的卡片会写一个与实际切片对不上的
    层级 —— 比不显示更糟。见 raster_probe._estimate_maxzoom。"""
    src = _map_js()
    assert "updateTifInfo('localTerrainFiles', 'localTerrainTifInfo', 'terrain')" in src
    assert "updateTifInfo('contourFiles', 'contourTifInfo', 'contour')" in src


def test_reset_form_clears_both_cards():
    """form.reset() 只清空文件选择框：卡片是普通 div，而且程序清空 input 不会
    触发 change，updateTifInfo 不会自己重跑。不显式重跑这两个更新函数，提交
    成功后卡片还挂着上一个任务那份 tif 的范围和层级，旁边是空的选择框。"""
    body = _reset_form_body()
    assert "form.reset()" in body
    branch = body[body.index("if (formId === 'processForm')"):]
    assert "updateLocalTerrainTifInfo()" in branch
    assert "updateContourTifInfo()" in branch
    # 顺序反了等于没改：reset() 会把刚重跑出来的状态再清一遍
    assert body.index("form.reset()") < body.index("updateLocalTerrainTifInfo()")


def test_stale_responses_cannot_overwrite_a_newer_selection():
    """连着改选文件时晚发的请求可能先回来 —— 必须有序号闸门，且两张卡各记各的
    （共用一个计数器的话，在一张卡上选文件会作废另一张卡正在进行的请求）。"""
    src = _map_js()
    assert "_tifInfoSeq = new Map()" in src
    body = src[src.index("async function updateTifInfo("):]
    assert "_tifInfoSeq.get(cardId) !== seq" in body
    assert body.count("if (stale()) return;") >= 3


def test_inspect_is_never_an_upload():
    """/api/raster/inspect 只收标签，不收文件。"""
    src = _map_js()
    head = src[src.index("'/api/raster/inspect'"):][:600]
    assert "application/json" in head
    assert "FormData" not in head


def test_a_superseded_inspect_request_is_aborted():
    """seq 闸门只保证晚到的响应盖不掉新渲染，被作废的那个请求还在跑 —— 连着
    改选几次就是几份几百 MB DEM 的头部在后端白解释。取消是主动的，不能当失败
    渲染成红字。"""
    body = _js_function_body(_map_js(), "updateTifInfo")
    assert "_tifInfoAbort.get(cardId)?.abort()" in body
    assert "signal: ctrl.signal" in body
    assert "err.name === 'AbortError'" in body


# ------------------------------------------------------------------ 解析器性质

def test_parser_reads_ranges_not_whole_files():
    """整个特性靠 File.slice 成立：一旦有人改成整包 arrayBuffer()，
    选一个 2 GB 的 DEM 就会把它读进内存。"""
    src = _strip_js_comments(_read("static", "js", "geotiff_meta.js"))
    assert "file.slice(" in src
    assert not re.search(r"\bfile\.arrayBuffer\s*\(", src)


def test_parser_supports_bigtiff():
    """>4 GB 的 DEM 用 BigTIFF（magic 43），只认 42 会把它们判成「不是 TIFF」。
    断言的是那个比较式本身：`"43" in src` 任何含 43 的字面量都能满足。"""
    src = _strip_js_comments(_read("static", "js", "geotiff_meta.js"))
    assert "magic === 43" in src and "getBigUint64" in src


def test_a_file_too_short_to_hold_a_header_is_not_a_range_error():
    """5 字节的文件走到 readAt(0, 16) 抛的是 RangeError: out of range，
    控制台里看着像解析器坏了 —— 该报的是「不是 TIFF」。"""
    src = _strip_js_comments(_read("static", "js", "geotiff_meta.js"))
    body = src[src.index("async function read(file)"):][:400]
    assert "file.size < 16" in body
    assert "not a TIFF file" in body


# ------------------------------------------------------------------ 两侧对账

def test_every_backend_warning_code_has_a_catalog_entry():
    """警告码在 raster_probe 里产生、在 map.js 里拼成 i18n 键。
    后端加一个码却忘了加文案，界面上就原样漏出 `js.map.tifinfo.warn_xxx`。"""
    src = _read("src", "services", "raster_probe.py")
    codes = set()
    for line in src.splitlines():
        if 'warnings"].append(' in line:
            args = line.split('.append(', 1)[1]
            codes.update(re.findall(r'"([a-z][a-z_]+)"', args))
    assert codes, "raster_probe.py 里一个警告码都扫不到 —— 断言的抓取方式过期了"

    missing = [c for c in sorted(codes) if f"js.map.tifinfo.warn_{c}" not in MESSAGES]
    assert not missing, f"这些后端警告码没有文案: {missing}"


def test_card_styles_exist():
    """按真实选择器断言，不按子串：style.css 里好几段注释就写着这些类名
    （.tif-info--scroll 和 .tif-info__file--sep 上面各有一段），`cls in css`
    被注释满足 —— 规则整条删掉测试照样绿。"""
    selectors = set()
    for sel, _body in _rules(_read("static", "css", "style.css")):
        selectors.update(part.strip() for part in sel.split(","))
    for cls in (".tif-info", ".tif-info__item--wide", ".tif-info__warn--fatal",
                ".tif-info__file--sep", ".tif-info--scroll", ".tif-info__filename"):
        assert cls in selectors, f"style.css 缺少 {cls} 规则（注释里提到不算）"


def test_the_filename_truncates_instead_of_hiding_the_file_size():
    """text-overflow 只作用于块容器自己的行内内容：写在 flex 容器
    .tif-info__name 上完全不生效。更糟的是裸文本节点变成匿名 flex item，
    min-width:auto + nowrap 让它拒绝收缩，把 .tif-info__size 顶到卡片外面
    被 overflow:hidden 裁掉 —— 长文件名下文件大小整个看不见。"""
    decls = {}
    for sel, body in _rules(_read("static", "css", "style.css")):
        for part in sel.split(","):
            decls.setdefault(part.strip(), {}).update(_decl_map(body))
    assert "text-overflow" not in decls.get(".tif-info__name", {}), (
        "截断声明又回到了 flex 容器 .tif-info__name 上 —— 那里不生效")
    filename = decls.get(".tif-info__filename", {})
    assert filename.get("text-overflow") == "ellipsis"
    assert filename.get("white-space") == "nowrap"
    assert filename.get("min-width") == "0", (
        "flex item 默认 min-width:auto，不显式归零就不会收缩，省略号永远不出现")
    # 文件名必须是自己的元素：匿名 flex item 上挂不住任何类
    assert "'tif-info__filename'" in _map_js()


def test_the_metadata_grid_tracks_cannot_be_widened_by_their_content():
    """1fr 就是 minmax(auto, 1fr)：某一格的 min-content（长坐标串、长文件名）
    超过轨道时整张网格被撑宽，而 .tif-info--scroll 的 overflow-y:auto 会把
    overflow-x 一并算成 auto —— 本项目踩过的那条幽灵横向滚动条。
    两处（含 576px 分支）都必须封死。"""
    values = []
    for sel, body, _ctx in _rules_ctx(_read("static", "css", "style.css")):
        if ".detail-grid" not in [p.strip() for p in sel.split(",")]:
            continue
        cols = _decl_map(body).get("grid-template-columns")
        if cols:
            values.append(cols)
    assert len(values) == 2, (
        f"期望 .detail-grid 有 2 处 grid-template-columns（基础 + 576px 分支），"
        f"实际 {len(values)} 处 —— 本断言已失效")
    for cols in values:
        bare = re.sub(r"minmax\(\s*0\s*,\s*1fr\s*\)", "", cols)
        assert "1fr" not in bare, f"{cols!r} 里还有裸 1fr，应写成 minmax(0, 1fr)"


def test_only_multi_file_cards_get_a_height_cap():
    """单文件（最常见）不该在已经会滚的弹窗里再套一层滚动条。
    断言整条表达式：只断言前缀的话，把条件反成 `<= 1` 照样通过 —— 而那正是
    本条标题禁止的东西。"""
    src = _map_js()
    assert "classList.toggle('tif-info--scroll', (data.files || []).length > 1)" in src


# ------------------------------------------------------------------ 底图自动回退

def test_the_fallback_watcher_starts_after_the_viewer_exists():
    """首屏描述符 fallback 已经为 true 时 _watchBasemapFallback 是**同步**
    通告的，而通告要重建底图图层（viewer.imageryLayers）—— 排在 Viewer 构造
    之前，那条分支就是对 null 取属性。"""
    body = _js_function_body(_map_js(), "initMap")
    assert "_watchBasemapFallback(bm)" in body
    assert body.index("viewer = new Cesium.Viewer(") < body.index("_watchBasemapFallback(bm)")


def test_a_runtime_fallback_rebuilds_the_base_layer():
    """maximumLevel 和 credit 在 UrlTemplateImageryProvider 构造完之后是只读的，
    只弹一条 toast 解决不了任何一半问题：配置 Google（21 层）回退到 Esri
    （19 层）后 Cesium 照旧请求 z20+，后端 _MAX_ZOOM 是 24 会放行、上游 404，
    用户看到一片黑；署名也还挂着没在放的那张源，而 Esri/OSM 的署名是许可要求。"""
    src = _map_js()
    assert "_rebuildBaseImagery(bm)" in _js_function_body(src, "_announceBasemapFallback")
    rebuild = _js_function_body(src, "_rebuildBaseImagery")
    assert "viewer.imageryLayers.add(" in rebuild
    assert "viewer.imageryLayers.remove(" in rebuild
    assert "url: bm.url" in rebuild
    assert "maximumLevel: bm.max_level" in rebuild
    assert "credit: bm.credit" in rebuild
    # 底图只能走同源的 /basemap/{z}/{x}/{y}：上游地址和 proxy_url 不进浏览器
    assert "http" not in rebuild and "proxy" not in rebuild


def test_the_fallback_is_watched_for_the_whole_session():
    """一次性 setTimeout 只覆盖首屏。上游在会话中途挂掉（用户平移了一小时）
    的回退要到刷新才被发现 —— 与 _watchBasemapFallback 自己那句「换了必须说」
    矛盾。轮询的代价是每轮都得判「变了没」，否则同一条 toast 每 30 秒弹一次。"""
    src = _map_js()
    watch = _js_function_body(src, "_watchBasemapFallback")
    assert "setInterval(_checkBasemapFallback" in watch
    assert "5000" in watch                       # 首屏那次检查的时机不变
    check = _js_function_body(src, "_checkBasemapFallback")
    assert "bm.source === _announcedBasemapSource" in check
    assert "_announceBasemapRestored(bm)" in check   # 换回配置源同样要说
