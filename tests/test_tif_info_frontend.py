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
# 渲染一次首页要把 Config 的副作用重定向到 tmp_path，姊妹用例里也已经写好了那套
# 装配（理由同上）。
from test_terrain_lighting_frontend import _load_app  # noqa: E402

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
    # 3 处：两张信息卡 + 起切前的规模预告行（#localTerrainEstimate 复用同一张卡
    # 的盒子样式）。数字写死是有意的 —— 多出来的第四处多半是有人把 tif-info
    # 当通用盒子在用，那会让这三处的样式改动牵连到不相干的地方。
    assert html.count('class="tif-info"') == 3


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
    assert "url: tileUrl(bm.url)" in rebuild
    assert "maximumLevel: bm.max_level" in rebuild
    assert "credit: bm.credit" in rebuild
    # 底图只能走 /basemap/{z}/{x}/{y} 这条代理路径：上游地址和 proxy_url 不进浏览器。
    # tileUrl 只把它改指到瓦片专用端口（src/core/tile_server.py），不含任何上游地址。
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


# ------------------------------------------------------------ 起切前的规模预告


def _estimate_body():
    return _js_function_body(_map_js(), "renderTerrainTileEstimate")


def _if_conditions(body):
    """函数体里每条 `if (...)` 的条件文本，按括号配对切。

    不能图省事写 `if\\s*\\(([^)]*)\\)`：条件里带函数调用时（`Array.isArray(counts)`）
    它在第一个右括号就断了，切出来是半截条件 —— 「某个标识符在不在这条守卫里」
    这类断言会全部永假，而永假的断言在这里比没有断言更糟（它看起来有人守着）。
    """
    out = []
    for m in re.finditer(r"\bif\s*\(", body):
        depth, i = 1, m.end()
        while i < len(body) and depth:
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
            i += 1
        out.append(body[m.end():i - 1])
    return out


def test_preset_options_carry_the_offset_from_the_single_source_of_truth(monkeypatch, tmp_path):
    """偏移表不许在 JS 里抄第二份 —— 由服务端渲染进 option 的 data-offset。

    断言的是**渲染结果**而不是模板文本：模板里写着 `{{ terrain_quality_offsets[...] }}`
    但路由忘了传这个变量时，Jinja 的 undefined 会把属性渲染成空串，页面照常
    出得来，预告只是永远按偏移 0 算 —— 精细档少预告一级（体积差 3.3 倍）。
    """
    from src.services.geo_validation import TILING_QUALITY_OFFSETS

    html = _load_app(monkeypatch, tmp_path).get('/').get_data(as_text=True)
    for preset, offset in TILING_QUALITY_OFFSETS.items():
        tag = re.search(r'<option[^>]*value="%s"[^>]*>' % re.escape(preset), html)
        assert tag, f'档位下拉里没有 {preset} 这条 option'
        assert f'data-offset="{offset}"' in tag.group(0), (
            f'{preset} 档的 option 上没有 data-offset="{offset}" —— '
            f'JS 只能从这里读偏移：{tag.group(0)}')


def test_index_renders_the_estimate_container(monkeypatch, tmp_path):
    """预告行要落在档位下拉之后、法线复选框之前 —— 它讲的正是这几个控件的后果。"""
    html = _load_app(monkeypatch, tmp_path).get('/').get_data(as_text=True)
    assert (html.index('id="localTerrainQuality"')
            < html.index('id="localTerrainEstimate"')
            < html.index('id="localTerrainNormals"'))


def test_map_js_does_not_hardcode_the_offset_table():
    """取值表只有 geo_validation.TILING_QUALITY_OFFSETS 一份。

    在 JS 里抄第二份的代价是静默漂移：将来改了 speed 的偏移或加了第四档，
    页面上的预告仍按旧表算，而切出来的是新表 —— 两个数对不上，用户只会
    认为预告是假的。
    """
    from src.services.geo_validation import TILING_QUALITY_OFFSETS

    src = _map_js()
    for preset in TILING_QUALITY_OFFSETS:
        assert f"'{preset}'" not in src, (
            f'map.js 里出现了档位字面量 {preset!r} —— 偏移表在这里抄了第二份')
        # 单引号那条盖不住第二份表最像样的写法：对象字面量的键不带引号
        # （`{ <档位>: 1, ... }`）。两条都留着才闭合 —— 只禁引号形的话，把整张
        # 取值表原样抄成一个 const 对象照样全绿。
        assert not re.search(rf'{re.escape(preset)}["\']?\s*:\s*-?\d', src), (
            f'map.js 里出现了 `{preset}: <数>` 这样的取值对 —— 偏移表抄了第二份，'
            f'改了 geo_validation.TILING_QUALITY_OFFSETS 之后预告会静默按旧表算')
    # 读到 data-offset 还不够，得读**选中那一项**的：写成
    # `qualityEl.options[0].dataset.offset` 恒取第一条 option（精细档），既满足
    # 「没抄第二份表」，也满足 test_the_estimate_level_is_the_base_plus_the_preset_offset
    # 那条算术断言（`base + offset` 一字未动）—— 两条一起绿，而档位下拉对预告
    # 已经彻底空转：用户切到快速档，预告仍按精细档算，正是那条用例声称要防的失效。
    body = _estimate_body()
    used = re.search(r"(\w+)\??\.dataset\.offset", body)
    assert used, (
        'map.js 没有从 <option data-offset> 读偏移 —— 那它是从哪儿知道档位偏移的？')
    assert re.search(rf"\b{re.escape(used.group(1))}\s*=[^;\n]*selectedOptions", body), (
        f'{used.group(1)} 不是从 selectedOptions 来的 —— 偏移读的不是用户选中的'
        f'那一档，档位下拉对预告静默空转')


def test_the_estimate_accumulates_from_a_clamped_start_level():
    """累加起点必须跟着钳：`Math.min(TERRAIN_MIN_LEVEL, level)`。

    8 是随包底图可用时 dem_task_tiler 恒传的 min_level，但基准 8 配快速档
    （-1）实际切到 z7 —— 起点死守 8 的话 `for (z = 8; z <= 7; ...)` 一轮都不
    进，预告会写成「约 0 张」。build_terrain 里那句
    `min_level = min(min_level, max_level)` 是同一条钳位。
    """
    body = _estimate_body()
    assert "Math.min(TERRAIN_MIN_LEVEL, level)" in body, (
        '累加起点没有跟着 level 钳下来 —— 基准 8 配快速档会预告成 0 张')


def test_the_estimate_hides_itself_on_an_antimeridian_crossing_dem():
    """跨 180° 的 DEM 不预告：判据取 summary.bounds_wgs84 的**东界**，且真接进守卫。

    raster_probe._tile_counts_per_level 在跨界数据上会少算约六成
    （intersecting_tile_range 把超出 180 的整段钳掉），那是刻意不补偿的已知
    边界。拿 warnings 当判据是借位：单文件跨界时 'antimeridian' 只落在
    files[i].warnings 上，summary.warnings 是干净的；而 summary 上的那条说的
    是「并集做过 wrap」，不是「这张表少算了」。

    只查 "bounds_wgs84" 与 "> 180" 两个子串是守不住的：它们都住在**声明行**上，
    而真正起作用的是早退守卫里那句 `|| crossesAntimeridian` —— 把它删掉，两个
    子串原地不动，用例照样全绿，跨界 DEM 于是拿到一个少算约六成的张数。下标同
    理：东界是 [2]，写成 [0] 判的是西界（跨界与否跟它无关），子串断言一样看不
    出来。所以这里钉两件事：声明取的是 [2]，且那个标识符出现在守卫的条件里。
    """
    body = _estimate_body()
    decl = re.search(r"(\w+)\s*=[^;\n]*bounds_wgs84[^;\n]*\[2\][^;\n]*>\s*180", body)
    assert decl, (
        '预告没有按 summary.bounds_wgs84 的**东界**（下标 [2]）判跨界 —— 跨 180° '
        ' 的 DEM 会拿到一个少算约六成的张数；写成 [0] 判的是西界，与跨界无关')
    guards = [c for c in _if_conditions(body) if 'Array.isArray(counts)' in c]
    assert guards, (
        '找不到那条早退守卫（条件里应有 Array.isArray(counts)）—— 本测试已失效')
    assert any(decl.group(1) in c for c in guards), (
        f'{decl.group(1)} 算出来了却没接进早退守卫 —— 跨界判据成了一个没人读的'
        f'局部变量，预告照旧按少算约六成的张数出：{guards}')
    assert "warnings" not in body, (
        '跨界判据不能读 warnings：单文件跨界时 summary.warnings 是干净的，'
        '而它上面的 antimeridian 讲的是并集 wrap，不是这张表少算了')


def test_the_estimate_is_redrawn_when_any_of_its_inputs_change():
    """预告有五个输入：inspect 汇总、自动开关、层级数字框、档位、法线。

    少挂一个监听不会报错，只是那个控件改了之后预告停在上一次的数上 ——
    用户按着一个过时的体积做决定，比不显示更糟。
    """
    src = _map_js()
    assert "addEventListener('change', renderTerrainTileEstimate)" in src, (
        '三个控件没有挂 change 监听 —— 改档位/层级/法线之后预告不会重算')
    # 三个 id 必须出现在同一条接线语句里：分开查的话，它们在 submitLocalTerrain
    # 里本来就各出现过一次，断言恒真。
    wiring = re.search(
        r"\[[^\]]*\]\s*\.forEach\([^)]*\)\s*=>\s*\{[^}]*"
        r"addEventListener\('change', renderTerrainTileEstimate\)", src, re.S)
    assert wiring, '找不到把三个控件接到 renderTerrainTileEstimate 的那条语句'
    for id_ in ('localTerrainMaxzoom', 'localTerrainQuality', 'localTerrainNormals'):
        assert f"'{id_}'" in wiring.group(0), f'{id_} 没有参与预告的重画'
    auto = _js_function_body(src, "initProcessTypeToggle")
    # 定位到「自动层级」复选框自己的回调再查：数据来源的 change 回调里现在也有
    # 一次重画（来源切走时要收起预告），拿整个 initProcessTypeToggle 查的话这条
    # 断言会被那一次顶住 —— 复选框漏挂监听也照样全绿。
    auto_cb = re.search(r"maxzoomAutoToggle\.addEventListener\('change'.*?\}\);", auto, re.S)
    assert auto_cb, '找不到「自动层级」复选框的 change 回调 —— 本测试已失效'
    assert "renderTerrainTileEstimate()" in auto_cb.group(0), (
        '「自动层级」复选框的回调里没有重画预告 —— 自动/手动切换会换掉基准'
        '层级的来源，预告必须跟着变')


def test_the_estimate_level_is_the_base_plus_the_preset_offset():
    """实际层级 = 钳位后的「基准 + 档位偏移」，且基准先截成整数。

    偏移不参与运算的话（`base + offset` 写成 `base`），档位这个控件对预告就彻底
    失效：精细档少预告一级，而它与快速档之间的体积差是 3.3 倍。页面上的数看着
    正常、跟切出来的产物却对不上，本节其余几条断言一个都拦不住。

    截断是同一类「一个数」的问题：数字框收得下 14.5，标题会写「实际 z13.5」，
    下面那个张数却是 counts[13.5] 落空按 0 算之后 z8..z13 的和 —— 两个数出自
    不同的层级。后端起切前自己就是 int(max_level)（cesium_terrain.py）。
    """
    body = _estimate_body()
    assert re.search(
        r'Math\.max\(0,\s*Math\.min\(counts\.length - 1,\s*base \+ offset\)\)', body), (
        '实际层级不是 max(0, min(counts.length - 1, base + offset)) —— 档位偏移没有'
        '参与运算，或钳位换了写法')
    assert re.search(r'base = Math\.trunc\(Number\(numEl\.value\)\)', body), (
        '手动填的基准层级没有截成整数 —— 填 14.5 时标题与张数会出自两个不同的层级')


def test_the_estimate_is_hidden_unless_the_source_is_an_upload():
    """来源切到「已下载的 DEM 任务」时预告必须收起，且得有人触发这次收起。

    预告行在 #localTerrainUploadRow **之外**（模板里它挨着档位下拉），
    initProcessTypeToggle 的 apply() 藏得掉上传行连同那张信息卡，却藏不掉它：
    切过去之后它原地留着上一批上传文件的层级/张数/体积，而那批文件与要切的 DEM
    任务毫无关系。两条可达路径：弹窗里先选文件再改来源；以及任务行「处理」进来的
    openProcessForDemTask —— 它只补发 change 事件，不复位表单也不清缓存。

    dem_task 这条线因此没有预告，那是特性的边界而不是漏了：inspect 的原料由前端
    读本地文件头得来，服务端够不着任务目录里的那些 DEM。
    """
    body = _estimate_body()
    guard = re.search(r"getElementById\('processSource'\)\??\.value\s*!==\s*'upload'", body)
    assert guard, (
        '预告没有判数据来源 —— 切到「已下载的 DEM 任务」之后它会留着上一批上传'
        '文件的张数与体积，讲的却是另一个任务')
    assert 'hidden = true' in body[guard.end():guard.end() + 120], (
        '判了数据来源却没有收起预告行')
    # 判据摆在那儿还得有人来跑：来源的 change 回调必须重画一次。
    toggle = _js_function_body(_map_js(), "initProcessTypeToggle")
    listener = re.search(
        r"sourceEl\.addEventListener\('change'.*?loadProcessDemTasks\(\)", toggle, re.S)
    assert listener and 'renderTerrainTileEstimate()' in listener.group(0), (
        '数据来源的 change 回调里没有重画预告 —— 判据写了也没人触发，预告会停在'
        '上一批上传文件的数上')


def test_the_estimate_only_caches_the_terrain_pipelines_summary():
    """等高线走 Web Mercator，后端压根不给它 tile_counts（raster_probe 里
    `if mode == "terrain"` 那道门）。不判 mode 就缓存的话，选一次等高线 DEM
    会把高程那份汇总冲掉，预告静默消失。"""
    body = _js_function_body(_map_js(), "updateTifInfo")
    assert "mode === 'terrain'" in body, (
        'updateTifInfo 缓存汇总时没有判 mode —— 等高线那次探测会冲掉高程的预告')
    # 陈旧汇总有三个入口，三处都得动：文件清空、探测失败（各清一次）、探测成功
    # （换成新的）。查 `_terrainInspectSummary in src` 是近乎恒真的 —— 那个变量
    # 的声明本身就满足它。
    assert body.count("cacheTerrainInspectSummary(null)") == 2, (
        '文件清空与探测失败这两条路径没有各清一次缓存 —— 上一份 DEM 的张数会挂在'
        '一个空的（或探测失败的）选择框旁边')
    assert "cacheTerrainInspectSummary(data.summary" in body, (
        '探测成功后没有把新汇总写进缓存 —— 预告要么没原料，要么一直用上一批文件的')
