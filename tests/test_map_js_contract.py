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
# 2026-08-09 全项目评审 P1：选区四至「一条规则，三个入口，三种答案」
#
# 后端 geo_validation.validate_bbox 只有四条规则（north > south、|lat| <= 90、
# |lon| <= 180、east > west）。改前前端三个入口各判各的：框选落定层零面积照收
#（单击就能造出 0°×0° 的选区，预估框还报「约 6 张瓦片」）、点读数编辑与手动
# 输入面板只校纬度不校经度量级（east=400 收下，状态栏读「300.000° × 20.000°」）、
# 三处又都刻意放行跨反经线矩形（状态栏读出负宽度 -340.000°、按钮不禁用、点下去
# 把后端英文原文弹进中文界面）。这一组把「只有一份口径、三个入口都走它」钉住。
# ---------------------------------------------------------------------------

# 闸门里的四条判据，与后端 validate_bbox 逐条对应；顺序即首个失败的报错顺序。
_BOUNDS_RULE_EXPRS = (
    'b.north > b.south',
    'b.south >= -90 && b.south <= 90 && b.north >= -90 && b.north <= 90',
    'b.west >= -180 && b.west <= 180 && b.east >= -180 && b.east <= 180',
    'b.east > b.west',
)
_BOUNDS_RULE_KEYS = (
    'js.map.edit.north_gt_south',
    'js.map.edit.lat_range',
    'js.map.edit.lon_range',
    'js.map.edit.east_gt_west',
)


def _squeeze(text):
    return re.sub(r'\s+', ' ', text)


def _left_up_handler(src):
    """_initMapTools 里注册到 LEFT_UP 的那个 handler 的源码。

    它是匿名函数，_fn_body 够不着，按「上一个 handler.setInputAction( 到
    ScreenSpaceEventType.LEFT_UP 之间」截取。
    """
    body = _fn_body(src, '_initMapTools')
    end = body.index('Cesium.ScreenSpaceEventType.LEFT_UP')
    start = body.rindex('handler.setInputAction(', 0, end)
    return body[start:end]


def test_bounds_rules_live_in_exactly_one_function():
    """四条规则与四句报错各只有一份，且都在 validateBoundsRules 里。

    第二份拷贝正是这次要拆的缺陷本体：三个入口曾经各写各的判据，
    「同一个选区在两个入口得到两种答案」比「哪条判据写错了」难查得多。
    """
    src = _map_js()
    assert 'function validateBoundsRules(' in src, (
        'map.js 应定义 validateBoundsRules() —— 选区四至的唯一校验口径'
    )
    code = _squeeze(_strip_comments(src))
    gate = _squeeze(_fn_body(src, 'validateBoundsRules'))
    for expr in _BOUNDS_RULE_EXPRS:
        assert code.count(expr) == 1, (
            f'判据 `{expr}` 在 map.js 里出现 {code.count(expr)} 次，应恰好 1 次：'
            '0 次 = 规则丢了，>1 次 = 又长出了第二份口径'
        )
        assert expr in gate, f'判据 `{expr}` 不在 validateBoundsRules 里'
    for key in _BOUNDS_RULE_KEYS:
        assert code.count(f"'{key}'") == 1, (
            f'文案 key {key} 不止一处引用 —— 报错也要只有一个出处'
        )
        assert key in gate, f'{key} 不是 validateBoundsRules 返回的'


def test_all_three_selection_entries_go_through_the_gate():
    """框选落定 / 点读数编辑 / 手动输入面板，三个入口都必须调同一个闸门。"""
    src = _map_js()
    code = _strip_comments(src)
    entries = {
        'LEFT_UP 框选落定': _left_up_handler(code),
        '_applyBoundsEdit（点四至读数编辑）': _fn_body(code, '_applyBoundsEdit'),
        '_readManualBounds（手动输入范围面板）': _fn_body(code, '_readManualBounds'),
    }
    for label, body in entries.items():
        assert 'validateBoundsRules(' in body, (
            f'{label} 没走 validateBoundsRules —— 又是一个自带口径的入口'
        )
    # 1 处定义 + 3 处调用。多出来的调用点不一定是错，但必须有人看过：
    # 第四个入口（例如拖角点手柄）走的是几何钳位而不是闸门，见 map.js 里的说明。
    assert code.count('validateBoundsRules(') == 4, (
        f'validateBoundsRules 的定义/调用点共 {code.count("validateBoundsRules(")} 处，'
        '期望 4 处（1 定义 + 3 入口）'
    )


def test_zero_area_drag_is_discarded_instead_of_becoming_a_selection():
    """鼠标单击（按下抬起同一像素）落成的零面积矩形必须被丢弃。

    改前它照样写进 currentBounds：浮层读 0.00000 四个相同的数、状态栏
    「已选区域 0.000° × 0.000°」、预估框还报「约 6 张瓦片」，而后端必然 400。
    """
    handler = _strip_comments(_left_up_handler(_map_js()))
    m = re.search(r'if \(validateBoundsRules\(_rectDegrees\)\) \{(.*?)\}', handler, re.S)
    assert m, 'LEFT_UP 落定必须先过 validateBoundsRules(_rectDegrees)'
    branch = m.group(1)
    assert 'clearSelection()' in branch, (
        '不合法的落定必须 clearSelection() 丢弃 —— 留着它就是一个点下去必然 400 的选区'
    )
    assert "js.map.bounds.no_area" in branch, (
        '丢弃时要 toast 告诉用户「单击不构成选区」，否则框没了也没人知道为什么'
    )
    assert branch.index('return') > branch.index('clearSelection()'), (
        '丢弃分支必须 return，不能继续往下写 currentBounds'
    )


def test_submit_payload_does_not_rewrite_the_users_longitudes():
    """提交前不许再 wrap 经度。

    改前 payload 走 _wrapLngEast(east)：用户填 190（越界，本该当场拒绝），
    被改写成 -170 送给后端，于是报错里出现一个用户从来没输入过的数字。
    现在 |lon| <= 180 由闸门在输入时就挡下，wrap 恒等于原值 —— 是一条永远
    不会生效的改写，删掉。
    """
    code = _strip_comments(_map_js())
    assert '_wrapLng' not in code, (
        'map.js 又出现了 _wrapLngWest/_wrapLngEast —— 经度量级现在由 '
        'validateBoundsRules 在入口挡下，提交前的 wrap 只会改写用户输入'
    )


@pytest.mark.skipif(shutil.which('node') is None, reason='node 不可用，跳过 JS 行为断言')
def test_bounds_rules_accept_exactly_what_the_backend_accepts():
    """把 map.js 里真实的 validateBoundsRules 抠出来用 node 跑，
    逐格与后端 geo_validation.validate_bbox 对拍。

    这条是「前端放行的后端必然也放行」的直接证据，也是四条规则各自的行为看守：
    删掉任何一条，网格里都有一批四至只有一侧接受，用例即红。
    顺带钉住状态栏那条不变量 —— 凡是被放行的四至，宽高都是正数。
    """
    from src.services.geo_validation import validate_bbox

    src = _map_js()
    gate_def = 'function validateBoundsRules(b) ' + _fn_body(src, 'validateBoundsRules')
    lats = [-91, -90, -89.5, -45, 0, 45, 89.5, 90, 91]
    lons = [-400, -181, -180, -179.5, -170, 0, 170, 179.5, 180, 181, 400]
    cases = [
        {'north': n, 'south': s, 'east': e, 'west': w}
        for n in lats for s in lats for e in lons for w in lons
    ]
    # 网格在 node 里现搭（两边同序四重循环），不把 6 千多个 case 展开成 argv：
    # `node -e` 的命令行有长度上限，展开后直接 E2BIG。
    script = (
        gate_def + '\n'
        'const lats = ' + json.dumps(lats) + ';\n'
        'const lons = ' + json.dumps(lons) + ';\n'
        'const out = [];\n'
        'for (const north of lats) for (const south of lats)\n'
        '  for (const east of lons) for (const west of lons)\n'
        '    out.push(validateBoundsRules({ north, south, east, west }));\n'
        'console.log(JSON.stringify(out));\n'
    )
    try:
        out = subprocess.run(
            ['node', '-e', script], capture_output=True, text=True, check=True,
            timeout=120,
        ).stdout.strip()
    except subprocess.TimeoutExpired:
        pytest.skip('node 启动超过 120 秒（CI runner 冷启动），'
                    '结构契约由 test_bounds_rules_live_in_exactly_one_function 守着')
    reasons = json.loads(out)
    assert len(reasons) == len(cases)

    # 后端报错文本 -> 前端应给出的同一类文案 key。
    def backend_key(msg):
        if 'must be greater than south' in msg:
            return 'js.map.edit.north_gt_south'
        if 'between -90 and 90' in msg:
            return 'js.map.edit.lat_range'
        if 'between -180 and 180' in msg:
            return 'js.map.edit.lon_range'
        if 'must be greater than west' in msg:
            return 'js.map.edit.east_gt_west'
        raise AssertionError(f'后端多了一条前端没有的规则: {msg}')

    mismatched = []
    for case, reason in zip(cases, reasons):
        try:
            validate_bbox(**case)
            expected = None
        except ValueError as exc:
            expected = backend_key(str(exc))
        if reason != expected:
            mismatched.append(f'{case}: 前端 {reason!r}，后端 {expected!r}')
        if reason is None:
            assert case['east'] - case['west'] > 0 and case['north'] - case['south'] > 0, (
                f'放行了一个零/负面积四至 {case} —— 状态栏会读出负数宽高'
            )
    assert not mismatched, (
        '前后端口径不一致（前 12 条）：\n  ' + '\n  '.join(mismatched[:12])
        + f'\n共 {len(mismatched)} 格不一致'
    )


@pytest.mark.skipif(shutil.which('node') is None, reason='node 不可用，跳过 JS 行为断言')
def test_the_three_measured_defects_are_rejected():
    """评审实测的三个具体输入，逐个必须被拒 —— 上面的对拍是网格，这条是留痕。"""
    src = _map_js()
    gate_def = 'function validateBoundsRules(b) ' + _fn_body(src, 'validateBoundsRules')
    cases = [
        # 单击落成的零面积选区：改前浮层收下，预估框报「约 6 张瓦片」
        {'north': 30, 'south': 30, 'east': 120, 'west': 120},
        # east=400：改前状态栏读「已选区域 300.000° × 20.000°」
        {'north': 40, 'south': 20, 'east': 400, 'west': 100},
        # 跨反经线：改前状态栏读出负宽度「-340.000°」，按钮不禁用
        {'north': 40, 'south': 39, 'east': -170, 'west': 170},
    ]
    expected = ['js.map.edit.north_gt_south',
                'js.map.edit.lon_range',
                'js.map.edit.east_gt_west']
    script = (
        gate_def + '\n'
        'const cases = ' + json.dumps(cases) + ';\n'
        'console.log(JSON.stringify(cases.map(validateBoundsRules)));\n'
    )
    try:
        out = subprocess.run(
            ['node', '-e', script], capture_output=True, text=True, check=True,
            timeout=120,
        ).stdout.strip()
    except subprocess.TimeoutExpired:
        pytest.skip('node 启动超过 120 秒（CI runner 冷启动）')
    assert json.loads(out) == expected


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


def test_tile_estimate_has_no_unreachable_backend_will_reject_branch():
    """预估框不许再留「后端会拒绝该四至」那条分支。

    它是一条只可能在闸门破了以后才生效的兜底：currentBounds 的每个写入点
    都过 validateBoundsRules 之后，跨反经线的选区根本进不来。更糟的是它当初
    对 east=400 也照说「选区跨反经线」——而 400 不是跨反经线，是越界。
    连同 estimateTileCount 里那两处静默 swap 一起清掉（swap 会把一个必然 400
    的四至算成一个像模像样的瓦片数）。
    """
    from src.i18n.catalog import MESSAGES

    assert 'js.map.tile_estimate.antimeridian' not in MESSAGES, (
        '文案还在 —— 分支删了文案不删就是死重量，且下一个人会照它把分支加回来'
    )
    src = _map_js()
    body = _strip_comments(_fn_body(src, 'updateTileEstimate'))
    assert 'east < west' not in body, (
        'updateTileEstimate 又长出了「east < west 就不算数」的兜底分支'
    )
    estimate = _strip_comments(_fn_body(src, 'estimateTileCount'))
    assert '[xMin, xMax] = [xMax, xMin]' not in estimate, (
        'estimateTileCount 又开始静默 swap 东西边界 —— 闸门保证 east > west，'
        'swap 只会把口径错误算成一个看着正常的数字'
    )


def test_status_bar_area_is_east_minus_west():
    """状态栏宽高必须是 east-west / north-south，不许为了躲负数取绝对值。

    改前跨反经线选区在这里读出「已选区域 -340.000° × 1.000°」。正确的修法是
    让负数**不可能出现**（闸门），不是在读数处 Math.abs 掩盖掉 —— 那样用户会
    看到一个 340° 宽的合理读数，然后提交被后端 400。
    """
    body = _strip_comments(_fn_body(_map_js(), 'updateBoundsInfo'))
    assert 'currentBounds.east - currentBounds.west' in body, (
        '状态栏宽度不再是 east - west'
    )
    assert 'currentBounds.north - currentBounds.south' in body, (
        '状态栏高度不再是 north - south'
    )
    assert not re.search(r'Math\.abs\([^)]*currentBounds', body), (
        '状态栏用 Math.abs 掩盖了负宽高 —— 负数应该在入口就不可能产生'
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
