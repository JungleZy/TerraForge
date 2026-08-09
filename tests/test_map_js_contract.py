"""
map.js behavioural contract tests (text-level regression guards).

本项目没有 JS 测试框架(无 package.json/vitest,且不打算引入——会破坏
PyInstaller 离线打包形态)。这些测试用文本断言守住关键契约,真实行为
由 playwright 手工实测覆盖(见计划 Task 10)。
"""

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _map_js():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', 'map.js'), encoding='utf-8') as f:
        return f.read()


def _fn_body(src, name):
    """按花括号配对提取 `function name(...)` 的整个函数体（含外层 {}）。"""
    i = src.index(f'function {name}(')
    j = src.index('{', i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == '{':
            depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
    raise AssertionError(f'{name} 函数体花括号不配对')


def _strip_comments(src):
    """剥掉 // 与 /* */ 注释。

    本仓库的注释**逐字讨论**被删掉的那些代码（map.js 底图那段就写着
    `_baseMapUrl` / `lyrs=m`），不剥的话「这个符号不许再出现」类断言会把
    注释当成违规。与 test_socket_singleton_contract.py 同一路数。
    """
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'(?m)//.*$', '', src)


def test_reset_form_helper_exists():
    """三处重复的表单重置逻辑必须收敛成一个函数"""
    src = _map_js()
    assert 'function resetForm(' in src, "map.js 应定义 resetForm()"


def test_reset_form_is_used_at_every_call_site():
    """三处提交成功分支都必须调 resetForm(),两条处理类分支都必须保留 bbox"""
    src = _map_js()
    # 1 处函数定义 + 3 处调用(map/dem、contour、local_terrain)
    assert src.count('resetForm(') >= 4, (
        "resetForm() 应有 1 处定义 + 3 处调用;少于 4 次说明某个提交分支没走统一重置"
    )
    # 数据下载/数据处理拆成两张独立表单后,resetForm 用 formId 指明复位哪一张;
    # 两条处理类分支（等高线、本地高程）都是上传驱动、没有 bbox,都必须
    # clearBounds:false 复位 #processForm——清框会删掉用户为下一个任务画好的选区。
    assert src.count("resetForm({ clearBounds: false, formId: 'processForm' })") >= 2, (
        "等高线与本地高程两条分支都必须 resetForm({ clearBounds: false, formId: 'processForm' });"
        "等高线分支漏了 clearBounds:false 会在任务创建成功后清掉用户选区"
    )


def test_submit_button_state_is_centralised():
    """按钮启用/禁用必须走统一函数,避免只加不减"""
    src = _map_js()
    assert 'function refreshSubmitButtonState(' in src, (
        "map.js 应定义 refreshSubmitButtonState()"
    )
    assert 'if (btn && isLocal) btn.disabled = false;' not in src, (
        "apply() 里只加不减的按钮解禁应改为走 refreshSubmitButtonState()"
    )
    # finally 里若写回 btn.disabled = false,会覆盖 resetForm() 刚摆好的状态
    # (例如非 local_terrain 模式重置后本该禁用,却被解禁)。
    assert not re.search(r'\}\s*finally\s*\{[^}]*btn\.disabled\s*=\s*false', src), (
        "finally 块里不能直接写 btn.disabled = false,会覆盖 resetForm() 的重置状态;"
        "应只调 refreshSubmitButtonState()"
    )
    # 绘制事件 handler 里直接禁用按钮会让 local_terrain 模式永久卡死
    # (该模式不需要 bbox,删框后没有任何东西会重新启用按钮)。
    assert "createTaskBtn').disabled = true" not in src, (
        "L.Draw.Event.DELETED 里不能直接禁用按钮,应走 refreshSubmitButtonState()"
    )


def test_submit_button_is_always_unlocked_in_finally():
    """
    上面几条断言全是禁止性的("不能写回 btn.disabled = false"),
    没有一条要求解锁调用**必须存在**——守「不能写 X」而不守「必须调用 Y」
    是不对称的。

    四处提交处理器开头都会 btn.disabled = true 给按钮上锁,唯一的解锁路径
    就是 finally 里那一行 refreshSubmitButtonState()。删掉它,提交失败后
    按钮会永久卡死,而其他断言全绿。这里补上存在性断言。
    """
    src = _map_js()

    # finally 块内目前没有嵌套花括号,所以 [^}]* 足够界定块体
    finally_blocks = re.findall(r'\}\s*finally\s*\{([^}]*)\}', src)
    assert len(finally_blocks) == 4, (
        "预期 4 处提交处理器各有一个 finally 块(map/dem、contour、local_terrain 上传、"
        "local_terrain 复用已下载 DEM 任务);"
        f"实际找到 {len(finally_blocks)} 个。块结构变了就要同步更新本测试"
    )
    for block in finally_blocks:
        assert 'refreshSubmitButtonState()' in block, (
            "每个 finally 块都必须调 refreshSubmitButtonState() 解锁按钮,"
            "否则提交失败后按钮永久禁用"
        )

    # 10 = 1 处定义 + apply() + CREATED + DELETED + syncBoundsFromDrawnItems()
    #      + resetForm() + 4 处 finally
    assert src.count('refreshSubmitButtonState(') >= 10, (
        "refreshSubmitButtonState 的定义/调用点少于 10 处,说明某个状态变更路径"
        "(绘制、编辑、类型切换、表单重置、提交收尾)漏了统一刷新"
    )


def test_tile_estimate_uses_backend_formula():
    """瓦片预估必须与后端 calculate_tiles 同口径（同一 Web Mercator 公式）。
    0.1.4 起它是软阈值：按钮不被禁用，但提交时必须二次确认。"""
    src = _map_js()
    assert 'function estimateTileCount(' in src, 'map.js 应定义 estimateTileCount()'
    assert 'const TASK_TILE_LIMIT = 100000' in src, (
        '前端软阈值必须与后端 WARN_TILES_THRESHOLD(100000) 一致'
    )
    assert 'Math.tan' in src and 'Math.cos' in src, (
        '预估公式必须是与后端一致的 Mercator deg2num（tan/sec），'
        '换成线性插值会让预估与后端口径脱节'
    )
    assert 'function updateTileEstimate(' in src
    # i18n 改造后标题不再是源码里的中文字面量：JS 里是
    # `t('js.map.download.confirm_large_title')`，中文在文案目录里。
    # 两段都查，只查一段都能被绕过（key 在但文案被改掉 / 文案在但没人引用）。
    from src.i18n.catalog import MESSAGES

    assert "t('js.map.download.confirm_large_title')" in src and 'showConfirm(' in src, (
        '瓦片数超软阈值时，提交前必须弹大任务确认框（0.1.4 放开硬上限后的把关）'
    )
    assert MESSAGES['js.map.download.confirm_large_title']['zh'] == '大任务确认', (
        'js.map.download.confirm_large_title 的中文不再是「大任务确认」'
    )


def test_rectangle_selection_is_wired():
    """Cesium 框选：ScreenSpaceEventHandler 必须接全按下/拖动/抬起三段，
    并提供统一的清除入口。

    Cesium 没有 Leaflet.draw 那样的现成绘制控件，框选是 map.js 自己实现的：
    「框选」按钮进绘制态，LEFT_DOWN 记起点、MOUSE_MOVE 实时更新矩形、
    LEFT_UP 落定写 currentBounds。少任何一段，用户就画不出选区，
    而「创建下载任务」永远禁用 —— 没有其他测试会红。
    文本级契约（与 resetForm 那几条同口径，项目刻意没有 JS 测试框架）。
    """
    src = _map_js()
    for event in ('LEFT_DOWN', 'MOUSE_MOVE', 'LEFT_UP'):
        assert f'Cesium.ScreenSpaceEventType.{event}' in src, (
            f"框选缺少 {event} 监听 —— 拖不出选区"
        )
    assert 'function _initMapTools(' in src, (
        "map.js 应定义 _initMapTools()（框选 + 工具条按钮接线）"
    )
    assert 'function clearSelection(' in src, (
        "map.js 应定义 clearSelection()（删除选区的统一入口）"
    )
    assert 'function syncBoundsFromDrawnItems(' not in src, (
        "syncBoundsFromDrawnItems 是 Leaflet.draw 时代的残留，"
        "Cesium 框选在 LEFT_UP 里直接写 currentBounds，不该再需要它"
    )


# ---------------------------------------------------------------------------
# MEDIUM #3：_wrapLngEast(180) 必须保持 180
# ---------------------------------------------------------------------------

def test_wrap_lng_east_keeps_180_structure():
    """_wrapLngEast 必须把 -180 的结果翻到 180（东边界口径 (-180,180]）。

    无 node 环境下的兜底结构断言；真实数值行为由下面 node 用例覆盖。
    """
    src = _map_js()
    body = _fn_body(src, '_wrapLngEast')
    assert '_wrapLngWest' in body, (
        '_wrapLngEast 应复用 _wrapLngWest 的基础 wrap，再修正端点'
    )
    assert re.search(r'===\s*-180\s*\?\s*180', body), (
        '_wrapLngEast 必须把 wrap 结果 -180 翻成 180，'
        '否则东经 180 被折成 -180 → east < west → 后端 400'
    )


@pytest.mark.skipif(shutil.which('node') is None, reason='node 不可用，跳过 JS 行为断言')
def test_wrap_lng_east_numeric_behavior():
    """把 map.js 里真实的 _wrapLngWest/_wrapLngEast 抠出来用 node 跑数值断言。

    超时按「环境不可用」处理而不是失败：v0.2.12 的发版构建在 Windows runner 上
    被这条 `node -e`（脚本只有几行）的 30 秒超时打断过一次 —— 那是 node 冷启动
    加杀毒扫描的代价，不是产品缺陷。上面那条结构断言无条件跑，覆盖的是同一个
    契约（`=== -180 ? 180`），所以这里跳过不会留下无人看守的行为。
    超时上限一并放宽到 120 秒。
    """
    src = _map_js()
    # 按花括号配对抠出两个函数定义
    west_def = 'function _wrapLngWest(lng) ' + _fn_body(src, '_wrapLngWest')
    east_def = 'function _wrapLngEast(lng) ' + _fn_body(src, '_wrapLngEast')
    script = (
        west_def + '\n' + east_def + '\n'
        'const cases = [180, -180, 179.9, 180.1, 540, -540, 0, 360, -360];\n'
        'console.log(JSON.stringify(cases.map(c => [_wrapLngEast(c), _wrapLngWest(c)])));\n'
    )
    try:
        out = subprocess.run(
            ['node', '-e', script], capture_output=True, text=True, check=True,
            timeout=120,
        ).stdout.strip()
    except subprocess.TimeoutExpired:
        pytest.skip('node 启动超过 120 秒（CI runner 冷启动），'
                    '契约由 test_wrap_lng_east_keeps_180_structure 无条件守着')
    results = json.loads(out)
    east_expected = [180, 180, 179.9, -179.9, 180, 180, 0, 0, 0]
    west_expected = [-180, -180, 179.9, -179.9, -180, -180, 0, 0, 0]
    for (east, west), e, w, c in zip(
            results, east_expected, west_expected,
            [180, -180, 179.9, 180.1, 540, -540, 0, 360, -360]):
        assert abs(east - e) < 1e-9, f'_wrapLngEast({c}) = {east}，期望 {e}'
        assert abs(west - w) < 1e-9, f'_wrapLngWest({c}) = {west}，期望 {w}'


# ---------------------------------------------------------------------------
# MEDIUM #17：previewTask 地形分支竞态 + 未捕获 Promise 拒绝
# ---------------------------------------------------------------------------

def test_preview_task_uses_sequence_token_against_race():
    """地形分支有 await，期间切换/关闭预览后，过期结果不得落地。"""
    src = _map_js()
    assert re.search(r'let _previewSeq\s*=\s*0', src), (
        'map.js 应定义预览调用序号 _previewSeq'
    )
    stop_body = _fn_body(src, 'stopTaskPreview')
    assert re.search(r'_previewSeq\s*\+=\s*1', stop_body), (
        'stopTaskPreview 必须递增 _previewSeq，作废在途的 previewTask'
    )
    body = _fn_body(src, 'previewTask')
    assert re.search(r'const seq\s*=\s*_previewSeq', body), (
        'previewTask 必须在 await 之前捕获当前序号'
    )
    # 两处 await（HEAD 探测、fromUrl）之后都必须比对序号
    assert body.count('seq !== _previewSeq') >= 2, (
        '每次 await 返回后都要比对 seq !== _previewSeq，过期直接 return'
    )
    # fromUrl 结果须先落局部变量，序号仍有效才赋给 viewer.terrainProvider
    assign = re.search(
        r'const provider\s*=\s*await Cesium\.CesiumTerrainProvider\.fromUrl', body)
    assert assign, 'fromUrl 结果应先 await 到局部变量再落地'
    land = body.index('viewer.terrainProvider = provider')
    last_check = body.rindex('seq !== _previewSeq', 0, land)
    assert assign.start() < last_check < land, (
        '赋值 viewer.terrainProvider 前必须有序号比对，否则过期预览会覆盖当前地形'
    )


def test_preview_task_catches_rejections_with_user_feedback():
    """previewTask 整体 try/catch：fromUrl reject 等失败要给用户可见反馈，
    调用方（history.js previewHistoryTask、map.js toggleContourPreview）
    都不 await，未捕获的 rejection 会变成 unhandled。"""
    src = _map_js()
    body = _fn_body(src, 'previewTask')
    assert re.search(r'\}\s*catch\s*\(', body), 'previewTask 必须整体包 try/catch'
    catch_body = body[body.rindex('catch'):]
    assert 'showNotification(' in catch_body and "'danger'" in catch_body, (
        'catch 里必须 showNotification(..., danger) 给用户可见反馈'
    )
    assert 'seq === _previewSeq' in catch_body, (
        '过期调用的报错不应打扰当前预览，catch 里要按序号过滤'
    )


# ---------------------------------------------------------------------------
# LOW 批次（2026-07-31 code-only review，前端杂项）
# ---------------------------------------------------------------------------

def test_download_type_toggle_refreshes_tile_estimate():
    """切换下载类型（地图/DEM）必须刷新瓦片预估读数。

    改前 apply() 只摆字段可见性不调 updateTileEstimate()：切到 DEM 时
    #tileEstimate 残留上一次地图模式的旧读数（DEM 按颗粒计、不用瓦片数）。
    """
    src = _map_js()
    body = _fn_body(src, 'initDownloadTypeToggle')
    apply_start = body.index('function apply(')
    apply_body = body[apply_start:body.index('typeRadios.forEach(', apply_start)]
    assert 'updateTileEstimate()' in apply_body, (
        'initDownloadTypeToggle 的 apply() 没有调 updateTileEstimate()——'
        '切到高程模式会残留旧的瓦片预估读数'
    )


def test_tile_estimate_rejects_antimeridian_selection():
    """跨反经线选区（wrap 后 east < west）不许静默 swap 东西边界算瓦片数。

    与后端拒绝语义对齐：这种选区提交必然 400，预估应显示「不可用」提示，
    而不是拿交换后的边界算一个注定提交失败的数字。
    """
    src = _map_js()
    body = _fn_body(src, 'updateTileEstimate')
    assert '_wrapLngWest(' in body and '_wrapLngEast(' in body, (
        'updateTileEstimate 必须按提交口径 wrap 经度后再判断/计算'
    )
    assert re.search(r'east\s*<\s*west\s*\)\s*\{[^}]*return null', body), (
        '检测到跨反经线（wrap 后 east < west）后必须 return null（不算数），'
        '而不是继续用 estimateTileCount 的静默 swap 算一个必然 400 的瓦片数'
    )


def test_map_js_does_not_resolve_the_basemap_url_itself():
    """底图地址的解析必须只有一份，在服务端（src/services/basemap_source.py）。

    改造前 map.js 里有一个 _baseMapUrl 平行实现，与 services/tile_url_probe
    的条目语义各写各的 —— 而且它写死 lyrs=m（选卫星也给你路网图）、写死
    署名 © OpenStreetMap（实际加载的是 Google 瓦片）。协议相对、层级上限、
    署名这些不变量现在由 tests/test_basemap_source.py 守。

    这条守的是「别再长回来」：map.js 不许再出现拼 URL 的痕迹。
    """
    src = _map_js()
    # 先剥注释：上面那段代码的**注释里**逐字写着 _baseMapUrl / lyrs=m，
    # 解释它们为什么被删（见 _strip_comments 的说明）。
    code = _strip_comments(src)
    assert '_baseMapUrl' not in code, (
        'map.js 又出现了 _baseMapUrl —— 底图地址解析已收敛到服务端，'
        '这里只消费 initMap(config, basemap) 传进来的结果'
    )
    assert 'googleapis.com' not in code, (
        'map.js 不该再硬编码瓦片主机：别名展开在 services/basemap_source.py'
    )
    assert 'lyrs=' not in code, (
        'map.js 不该再硬编码 lyrs 样式码 —— 卫星/路网由配置项 basemap_source 决定'
    )
    assert re.search(r'function initMap\(\s*config\s*,\s*basemap\s*\)', src), (
        'initMap 必须接收服务端解析好的 basemap（templates/index.html 传入）'
    )


def _preset_submit_bodies():
    """两个提交入口的函数体（已剥注释）。上传走 FormData，DEM 走 JSON body。"""
    code = _strip_comments(_map_js())
    return {
        'submitLocalTerrain': _fn_body(code, 'submitLocalTerrain'),
        'startDemTaskTerrainTiling': _fn_body(code, 'startDemTaskTerrainTiling'),
    }


def test_terrain_submit_sends_the_preset_fields():
    """两个提交入口都必须带上档位，否则用户选的档位静默丢失。

    只接一个入口是这条链路最像「已完成」的半成品：上传 DEM 时档位生效、
    对已下载的 DEM 任务起切时不生效（或反过来），两条路径的产物不一样大、
    不一样细，而界面上是同一个下拉框、同一个按钮，零报错。
    """
    bodies = _preset_submit_bodies()
    for name, body in bodies.items():
        assert 'localTerrainQuality' in body, f'{name} 没读档位下拉'
        assert 'localTerrainNormals' in body, f'{name} 没读法线复选框'
    # 字段名要和后端对上：表单分支是 append('quality', ...)，JSON 分支是 quality:。
    assert re.search(r"append\(\s*'quality'\s*,", bodies['submitLocalTerrain']), (
        "submitLocalTerrain 的 FormData 里没有 quality 字段 —— "
        "POST /api/terrain/local/tasks 会退回配置默认档")
    assert re.search(r"\bquality\s*:", bodies['startDemTaskTerrainTiling']), (
        "startDemTaskTerrainTiling 的 JSON body 里没有 quality 字段 —— "
        "POST /api/terrain/dem/<id>/start 会退回配置默认档")


def test_normals_checkbox_is_submitted_as_its_checked_state():
    """法线开关必须提交 checked 状态，不能提交 checkbox 的 .value。

    后端的 `src/services/geo_validation.py` 里 `coerce_vertex_normals` 是**严格
    白名单**：只认真布尔与字面量 'true'/'false'，'on' 一律 400。而
    - checkbox 的 `.value` 恒为 'on'，与勾没勾无关（照抄本文件其它字段的
      `el?.value || '默认'` 写法就是这个下场）；
    - 把原生 checkbox 直接塞进 FormData 同样送 'on'、没勾时干脆不发字段。
    两种写法都是每次提交 400，而错误只在通知条上一闪。
    """
    bodies = _preset_submit_bodies()

    form = re.search(r"append\(\s*'vertex_normals'\s*,([^\n]*)\)",
                     bodies['submitLocalTerrain'])
    assert form, "submitLocalTerrain 的 FormData 里没有 vertex_normals 字段"
    json_field = re.search(r"\bvertex_normals\s*:([^\n]*)",
                           bodies['startDemTaskTerrainTiling'])
    assert json_field, "startDemTaskTerrainTiling 的 JSON body 里没有 vertex_normals 字段"

    for label, expr in (('FormData 分支', form.group(1)),
                        ('JSON 分支', json_field.group(1))):
        assert '.checked' in expr, (
            f'{label} 提交的不是 checkbox 的 checked 状态：{expr.strip()}')
        assert '.value' not in expr, (
            f'{label} 读了 checkbox 的 .value（恒为 on，后端 400）：{expr.strip()}')
        assert "'on'" not in expr, (
            f"{label} 出现了 'on' —— 后端白名单只认 true/false：{expr.strip()}")


def test_terrain_submit_lets_the_backend_supply_the_defaults():
    """三个字段的兜底一律是空串，前端不许自己抄一份默认值。

    空串 = 未传 = 走配置默认，这是后端定的三态语义（local_terrain_api
    的 create_local_terrain_task、terrain_api 的 start_dem_tiling
    都把空串当未传）。前端写 `|| '14'` / `|| 'balanced'`
    的后果不是「多一层保险」，是**同一份 DEM 从两个入口切出不同产物**：历史页
    详情面板的起切按钮不带 body，走的是配置里的 terrain_local_maxzoom /
    terrain_quality_preset；这张表单一旦控件缺席或被清空，就用前端抄的那份默认
    盖过去。层级和法线都不可逆，发现时只能重切。
    """
    bodies = _preset_submit_bodies()
    for field in ('maxzoom', 'quality'):
        m = re.search(r"append\(\s*'" + field + r"'\s*,([^\n]*)\)",
                      bodies['submitLocalTerrain'])
        assert m, f'submitLocalTerrain 的 FormData 里没有 {field} 字段'
        assert "|| ''" in m.group(1), (
            f"submitLocalTerrain 的 {field} 兜底不是空串 —— 前端在抄一份默认值，"
            f"会盖掉配置：{m.group(1).strip()}")
        m = re.search(r'\b' + field + r'\s*:([^\n]*)', bodies['startDemTaskTerrainTiling'])
        assert m, f'startDemTaskTerrainTiling 的 JSON body 里没有 {field} 字段'
        assert "|| ''" in m.group(1), (
            f"startDemTaskTerrainTiling 的 {field} 兜底不是空串：{m.group(1).strip()}")
