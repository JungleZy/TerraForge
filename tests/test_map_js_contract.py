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


def _ui_js():
    """guard() 住在 ui.js：提交锁的唯一实现从 2026-08-15 起在那里。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', 'ui.js'), encoding='utf-8') as f:
        return f.read()


def _fn_body(src, name):
    """按花括号配对提取 `function name(...)` 的整个函数体（含外层 {}）。

    先按圆括号配对跳过参数表，再去找函数体那个 `{`：解构参数里的花括号排在函数
    体之前（`function resetForm({ clearBounds = true } = {})`），直接
    `src.index('{', i)` 会把参数表当成函数体切出来 —— 切片里除了参数名什么都
    没有，于是「函数体里必须有 X」永假、「函数体里不许有 X」永真。

    2026-08-15 登记：引文里原来还有 `formId = 'downloadForm'`。两张表单
    （#downloadForm / #processForm）合并成一张 #taskForm 之后那个参数没了，
    但解构参数本身还在（`{ clearBounds = true } = {}`），所以这段跳参数表的
    逻辑一个字都不能省 —— 去掉它切出来的仍旧是参数表。
    """
    i = src.index(f'function {name}(')
    args_open = src.index('(', i)
    depth = 0
    args_close = None
    for k in range(args_open, len(src)):
        if src[k] == '(':
            depth += 1
        elif src[k] == ')':
            depth -= 1
            if depth == 0:
                args_close = k
                break
    assert args_close is not None, f'{name} 参数表的圆括号不配对——本测试已失效'
    j = src.index('{', args_close)
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
    # 2026-08-15：两张表单合并成一张 #taskForm，resetForm 的 formId 参数随之
    # 删除，锚点从 `resetForm({ clearBounds: false, formId: 'processForm' })`
    # 换成 `resetForm({ clearBounds: false })`。守的是同一件事 —— 两条处理类
    # 分支（等高线、本地高程）都是上传驱动、没有 bbox，都必须 clearBounds:false，
    # 清框会删掉用户为下一个任务画好的选区。实测 map.js 里正是 2 处。
    assert src.count("resetForm({ clearBounds: false })") >= 2, (
        "等高线与本地高程两条分支都必须 resetForm({ clearBounds: false });"
        "等高线分支漏了 clearBounds:false 会在任务创建成功后清掉用户选区"
    )


def test_submit_button_state_is_centralised():
    """按钮启用/禁用必须走统一函数,避免只加不减"""
    src = _map_js()
    assert 'function refreshSubmitButtonState(' in src, (
        "map.js 应定义 refreshSubmitButtonState()"
    )
    # 文案里原来写的是 apply()：那是 initDownloadTypeToggle / initProcessTypeToggle
    # 各自的同名闭包，2026-08-15 合并成模块级 applyPipeline()。被禁的字面量本身
    # 一字未改，钉的仍是「按钮解禁不许绕开统一函数」。
    assert 'if (btn && isLocal) btn.disabled = false;' not in src, (
        "applyPipeline() 里只加不减的按钮解禁应改为走 refreshSubmitButtonState()"
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


def test_submit_button_is_always_unlocked_by_the_single_lock_owner():
    """
    上面几条断言全是禁止性的("不能写回 btn.disabled = false"),
    没有一条要求解锁调用**必须存在**——守「不能写 X」而不守「必须调用 Y」
    是不对称的。守的东西一个字没变：**提交钮不许在提交失败后永久卡死**。

    2026-08-15 换了证据的位置。改前三处提交处理器各自 `btn.disabled = true`
    上锁、各自在 `finally` 里 `refreshSubmitButtonState()` 解锁,本条数的就是
    那三个 finally 块。现在整条 #taskForm 的 submit 走 ui.js 的 `guard()`
    (锁点必须在任何 await 之前:改前 submitDownload 的锁排在
    `await currentTileEstimate()` 之后,那段往返里连点两次就建两个任务),
    三份手写锁连同它们的 finally 一起删了。

    于是不变量的守法反过来:**装配里一处锁都不许有**(有就是第二个锁主,
    两个锁会互相还原对方的状态),而唯一的锁主 guard 必须在 finally 里还原
    `disabled`——那是异常路径也走得到的唯一位置。
    """
    src = _strip_comments(_map_js())

    for fn_name in ('submitDownload', 'submitContour', 'submitLocalTerrain'):
        body = _fn_body(src, fn_name)
        assert 'btn.disabled' not in body, (
            f'{fn_name} 里出现了手写的 btn.disabled —— 提交在飞期间的上锁只能有'
            '一处(ui.js 的 guard),两处会互相还原对方的状态,而装配自己那把锁'
            '没有 finally 兜异常路径'
        )

    guard_body = _fn_body(_strip_comments(_ui_js()), 'guard')
    assert 'finally' in guard_body, (
        'ui.js 的 guard() 没有 finally —— 它是全站唯一的提交锁主,不在 finally 里'
        '还原就等于「动作抛异常之后按钮永久禁用」'
    )
    tail = guard_body[guard_body.index('finally'):]
    assert 'disabled = originalDisabled' in tail, (
        f'guard() 的 finally 没有把 disabled 还原成进入时的值,实际块体：{tail!r}'
    )

    # 10 = 1 处定义 + applyPipeline() + CREATED + DELETED
    #      + syncBoundsFromDrawnItems() + resetForm() + openCreatePanel + 选区各出口
    # 2026-08-15（其一）：apply() 是那两个 init 函数各自的闭包，已并成模块级
    # applyPipeline()；openProcessForDemTask 收成一行转调 openCreatePanel()，
    # 那处刷新随之落在 openCreatePanel 里。
    # 2026-08-15（其二）：三处 finally 里的那三个调用随手写锁一起删了（解锁归
    # guard），枚举里去掉它们。下界仍是 10（实测 14 处）—— 下界守的是「选区/
    # 编辑/切管线/重置这些状态变更路径没漏刷新」，与提交收尾那三处无关。
    assert src.count('refreshSubmitButtonState(') >= 10, (
        "refreshSubmitButtonState 的定义/调用点少于 10 处,说明某个状态变更路径"
        "(绘制、编辑、管线切换、表单重置、提交收尾)漏了统一刷新"
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
    """每一个「把外来四至落成选区」的入口都必须调同一个闸门。

    ⚠️ 登记（2026-08 §5.2 地名搜索）：入口从三个变成**四个** ——
    applyPlaceResult（点搜索结果落成选区）是新的一个。它比前三个更需要这道
    闸：地理编码服务给的 bbox 完全不受本应用控制，跨反经线的国家、退化成一
    个点的地名都真实存在，放行一个就是一次必然 400 的提交。名字里的
    「three」是历史遗留，钉点以下面这张表为准。
    """
    src = _map_js()
    code = _strip_comments(src)
    entries = {
        'LEFT_UP 框选落定': _left_up_handler(code),
        '_applyBoundsEdit（点四至读数编辑）': _fn_body(code, '_applyBoundsEdit'),
        '_readManualBounds（手动输入范围面板）': _fn_body(code, '_readManualBounds'),
        'applyPlaceResult（地名搜索结果）': _fn_body(code, 'applyPlaceResult'),
    }
    for label, body in entries.items():
        assert 'validateBoundsRules(' in body, (
            f'{label} 没走 validateBoundsRules —— 又是一个自带口径的入口'
        )
    # 1 处定义 + 4 处调用。多出来的调用点不一定是错，但必须有人看过：
    # 第五个入口（例如拖角点手柄）走的是几何钳位而不是闸门，见 map.js 里的说明。
    expected = 1 + len(entries)
    assert code.count('validateBoundsRules(') == expected, (
        f'validateBoundsRules 的定义/调用点共 {code.count("validateBoundsRules(")} 处，'
        f'期望 {expected} 处（1 定义 + {len(entries)} 入口）'
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
        # encoding 必须显式给：Windows 上 text=True 默认按 locale（cp1252）解码，
        # node 输出里只要有一个中文字，读取线程就抛 UnicodeDecodeError，
        # stdout 静默变成 None。
        out = subprocess.run(
            ['node', '-e', script], capture_output=True, text=True,
            encoding='utf-8', errors='replace', check=True,
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
        # encoding 必须显式给：Windows 上 text=True 默认按 locale（cp1252）解码，
        # node 输出里只要有一个中文字，读取线程就抛 UnicodeDecodeError，
        # stdout 静默变成 None。
        out = subprocess.run(
            ['node', '-e', script], capture_output=True, text=True,
            encoding='utf-8', errors='replace', check=True,
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

def test_pipeline_toggle_refreshes_tile_estimate():
    """切换管线（瓦片/高程/地形切片/等高线）必须刷新瓦片预估读数。

    改前 apply() 只摆字段可见性不调 updateTileEstimate()：切到 DEM 时
    #tileEstimate 残留上一次地图模式的旧读数（DEM 按颗粒计、不用瓦片数）。

    2026-08-15 锚点搬家：旧锚是 `_fn_body(src, 'initDownloadTypeToggle')` 里的
    闭包 `function apply(`，切到 `typeRadios.forEach(` 为止。单选按钮组已换成
    段控（[data-pipeline] chips），字段显隐收敛成**模块级** applyPipeline() +
    PIPELINE_FIELDS 一张表 —— apply() 不再是任何函数的闭包，在 init 函数体里
    切必然找不到。所以直接取 applyPipeline 的函数体。

    多钉一条「它确实在跑那张表」：否则「刷新预估」这句话可以挂在一个与字段显隐
    毫无关系的函数上，断言就不再证明「切管线 → 重算预估」这条链。
    """
    src = _map_js()
    body = _fn_body(src, 'applyPipeline')
    assert 'PIPELINE_FIELDS' in body, (
        'applyPipeline() 没有消费 PIPELINE_FIELDS —— 字段显隐不在它手上，'
        '下面那条断言就证明不了「切管线会重算预估」'
    )
    assert 'updateTileEstimate()' in body, (
        'applyPipeline() 没有调 updateTileEstimate()——'
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


def _local_terrain_submit_body():
    """submitLocalTerrain 的函数体（已剥注释）。

    「本地高程切片」的两种来源（上传 / 零拷贝复用 DEM 任务）都走这一个
    函数、同一条 FormData 链路 —— 早前 dem_task 分支另有
    startDemTaskTerrainTiling 打 JSON 到 /api/terrain/dem/<id>/start
    （复用 DEM 任务自己的切片作业、不新建任务，进度只能在详情弹窗里看），
    已随「任务行处理按钮把高程任务转出新切片任务」一并删除。
    """
    return _fn_body(_strip_comments(_map_js()), 'submitLocalTerrain')


def test_terrain_submit_sends_the_preset_fields():
    """上传与「DEM 任务转切片」两条来源都必须带上档位，否则用户选的档位静默丢失。

    只接一条来源是这条链路最像「已完成」的半成品：上传 DEM 时档位生效、
    对已下载的 DEM 任务起切时不生效（或反过来），两条路径的产物不一样大、
    不一样细，而界面上是同一个下拉框、同一个按钮，零报错。
    """
    body = _local_terrain_submit_body()
    assert 'localTerrainQuality' in body, 'submitLocalTerrain 没读档位下拉'
    assert 'localTerrainNormals' in body, 'submitLocalTerrain 没读法线复选框'
    # 字段名要和后端对上：FormData 的 append('quality', ...)。
    assert re.search(r"append\(\s*'quality'\s*,", body), (
        "submitLocalTerrain 的 FormData 里没有 quality 字段 —— "
        "POST /api/terrain/local/tasks 会退回配置默认档")
    assert re.search(r"append\(\s*'dem_task_id'\s*,", body), (
        "submitLocalTerrain 的 FormData 里没有 dem_task_id 分支 —— "
        "任务行「处理」按钮/表单的 DEM 任务来源会无处可去")


def test_normals_checkbox_is_submitted_as_its_checked_state():
    """法线开关必须提交 checked 状态，不能提交 checkbox 的 .value。

    后端的 `src/services/geo_validation.py` 里 `coerce_vertex_normals` 是**严格
    白名单**：只认真布尔与字面量 'true'/'false'，'on' 一律 400。而
    - checkbox 的 `.value` 恒为 'on'，与勾没勾无关（照抄本文件其它字段的
      `el?.value || '默认'` 写法就是这个下场）；
    - 把原生 checkbox 直接塞进 FormData 同样送 'on'、没勾时干脆不发字段。
    两种写法都是每次提交 400，而错误只在通知条上一闪。
    """
    body = _local_terrain_submit_body()

    form = re.search(r"append\(\s*'vertex_normals'\s*,([^\n]*)\)", body)
    assert form, "submitLocalTerrain 的 FormData 里没有 vertex_normals 字段"

    expr = form.group(1)
    assert '.checked' in expr, (
        f'提交的不是 checkbox 的 checked 状态：{expr.strip()}')
    assert '.value' not in expr, (
        f'读了 checkbox 的 .value（恒为 on，后端 400）：{expr.strip()}')
    assert "'on'" not in expr, (
        f"出现了 'on' —— 后端白名单只认 true/false：{expr.strip()}")


def test_terrain_submit_lets_the_backend_supply_the_defaults():
    """三个字段的兜底一律是空串，前端不许自己抄一份默认值。

    空串 = 未传 = 走配置默认，这是后端定的三态语义（local_terrain_api
    的 create_local_terrain_task 把空串当未传）。前端写 `|| '14'` /
    `|| 'balanced'` 的后果不是「多一层保险」：控件缺席或被清空时，前端抄的
    那份默认会**显式**盖掉运维在配置页配的 terrain_local_maxzoom /
    terrain_quality_preset —— 配置那一项就成了「改了没反应的假旋钮」。
    层级和法线都不可逆，发现时只能重切。
    """
    body = _local_terrain_submit_body()
    for field in ('maxzoom', 'quality'):
        m = re.search(r"append\(\s*'" + field + r"'\s*,([^\n]*)\)", body)
        assert m, f'submitLocalTerrain 的 FormData 里没有 {field} 字段'
        assert "|| ''" in m.group(1), (
            f"submitLocalTerrain 的 {field} 兜底不是空串 —— 前端在抄一份默认值，"
            f"会盖掉配置：{m.group(1).strip()}")


def test_terrain_submit_sends_the_auto_literal_when_the_box_is_checked():
    """勾了「自动」就送字面量 'auto'，不能送数字框里那个陈旧的数。

    数字框在自动挡下是 disabled 的，它的 value 只是用户取消勾选后的起点。
    照发那个数，用户选的「按源数据分辨率决定」就静默变成一个写死的层级 ——
    HTTP 200、作业照跑、前端零报错，只有切出来的层级不是他要的那个，
    而层级不可逆，发现时只能重切。

    字面量必须逐字是 'auto'：后端 geo_validation.coerce_maxzoom 刻意不做
    大小写归一、不裁空白，'AUTO' 当场 400。
    """
    body = _local_terrain_submit_body()
    assert 'localTerrainMaxzoomAuto' in body, (
        'submitLocalTerrain 没读「自动层级」复选框 —— 勾了也送不出去，'
        '用户选的自动挡在请求里根本不存在')
    m = re.search(r"append\(\s*'maxzoom'\s*,([^\n]*)\)", body)
    assert m, 'submitLocalTerrain 的 FormData 里没有 maxzoom 字段'
    assert "'auto'" in m.group(1), (
        f"maxzoom 那一行没有 'auto' 字面量 —— 勾上自动挡送出去的还是数字框里"
        f"那个陈旧的数：{m.group(1).strip()}")
    # 方向也得钉死。上面两条（读了复选框 + 有 'auto' 字面量）对三元的**极性**
    # 一无所知：写反成 `checked ? (maxzoomEl?.value || '') : 'auto'` 同样全绿，
    # 而行为恰好对调 —— 勾了自动挡送数字框里那个陈旧的数，没勾反倒送 'auto'
    # 把用户填的层级整个作废。两种都不可逆，发现时只能重切。
    assert re.search(r"\.checked\s*\?\s*'auto'\s*:", m.group(1)), (
        f"「勾上」那一支送的不是 'auto' —— 三元写反了，自动/手动两挡的行为对调："
        f"{m.group(1).strip()}")


def test_maxzoom_auto_disabled_state_survives_a_form_reset():
    """勾选态 -> 禁用态的同步必须收敛成一个函数，resetForm 也要调它。

    form.reset() 把「自动」复选框拨回服务端渲染的那个默认值，但**不触发
    change** —— 只挂 change 监听的话，用户取消过勾选、再建完一个任务之后，
    界面就停在「自动挡勾着、数字框却能填」：填进去的数被 submitLocalTerrain
    直接跳过（勾了就送 'auto'），没有任何提示。resetForm 里两张 tif 信息卡
    跟着收起是同一条理由。
    """
    src = _strip_comments(_map_js())
    assert 'function syncLocalTerrainMaxzoomDisabled(' in src, (
        'map.js 应定义 syncLocalTerrainMaxzoomDisabled()')
    # 1 处定义 + change 监听 + resetForm，共 3 次；少于 3 次说明有一侧没走同步。
    assert src.count('syncLocalTerrainMaxzoomDisabled') >= 3, (
        'change 监听与 resetForm 必须走同一个同步函数 —— 少一处就是勾选态与'
        '禁用态脱节，而两者不一致时提交送的是勾选态')
    # 计数守不住**位置**：把 resetForm 里那一句挪进任何别的函数，总数仍是 3，
    # 而本用例整条标题（「survives a form reset」）说的正是 form.reset() 之后
    # 那一次同步。按函数体钉，与本文件其它用例同一路数（_fn_body）。
    assert 'syncLocalTerrainMaxzoomDisabled()' in _fn_body(src, 'resetForm'), (
        'resetForm 里没有调 syncLocalTerrainMaxzoomDisabled() —— form.reset() '
        '把复选框拨回默认值却不触发 change，界面会停在「自动挡勾着、数字框却能填」，'
        '填进去的数被提交那侧直接跳过（勾了就送 auto），零提示')
    # 计数与位置都对**极性**一无所知：`numEl.disabled = !autoEl.checked` 一字之差，
    # 三次调用一次不少、resetForm 那句也还在，全套照绿 —— 而它产出的正是上面这
    # 两条断言、syncLocalTerrainMaxzoomDisabled 自己的注释、以及模板里那句
    # 「勾上时禁用数字框」共同警告的那个状态，只是这次是常态而非 reset 后的一瞬。
    # 与本文件 test_terrain_submit_sends_the_auto_literal_when_the_box_is_checked
    # 里那条三元极性断言同一路数：形状钉死，不靠计数。
    assert re.search(r'\.disabled\s*=\s*[A-Za-z_$][\w$]*\.checked\b',
                     _fn_body(src, 'syncLocalTerrainMaxzoomDisabled')), (
        '禁用态的极性不是 `numEl.disabled = autoEl.checked` —— 写成 `= !autoEl.checked` '
        '的话自动挡勾着数字框反倒能填，用户填的数在提交那侧被直接跳过（勾了就送 '
        "'auto'），而这个状态本仓三处注释都点名警告过")


def test_initial_and_runtime_basemaps_use_session_tile_url():
    """首屏与换源重建的底图都只把内部绝对路径交给 tileUrl()。

    端口是 initTileOrigin() 在页面启动时一次性定下的会话状态，URL 拼接点
    不该再各自读 bm.tile_port —— 读了就意味着「探测还没落定也照拼」，
    而探测失败时那个端口是死的，整屏底图会静默变白。
    """
    src = _strip_comments(_map_js())
    init_body = _fn_body(src, 'initMap')
    rebuild_body = _fn_body(src, '_rebuildBaseImagery')
    assert 'url: tileUrl(bm.url)' in init_body
    assert 'url: tileUrl(bm.url)' in rebuild_body
    assert 'bm.tile_port' not in init_body
    assert 'bm.tile_port' not in rebuild_body


def test_preview_routes_every_application_tile_path_through_tile_url():
    """预览的四类瓦片路径 + 晕渲回退那张 PNG 全部走同一个解析器。

    最容易漏的是最后那张 hillshade PNG：前面 /hillshade 元数据请求改对了，
    返回体里的 hs.url 直接塞进 SingleTileImageryProvider 的话，图片仍从主
    端口取 —— 而它恰恰是「没有地形切片」时用户唯一看得见的东西，绕回主端
    口既吃 6 连接上限、又让整条隔离链路只差最后一跳失效。
    """
    body = _fn_body(_strip_comments(_map_js()), 'previewTask')
    for anchor in (
        'tileUrl(`/tiles/${task.id}`)',
        'tileUrl(`/contour/${task.id}`)',
        'tileUrl(`/terrain/local/${task.id}`)',
        'tileUrl(`/terrain/dem/${task.id}`)',
        'fetch(`${base}/layer.json`)',
        'Cesium.CesiumTerrainProvider.fromUrl(base',
        'fetch(`${base}/hillshade`)',
        'url: tileUrl(hs.url)',
    ):
        assert anchor in body, f'previewTask 里找不到锚点：{anchor}'


# previewTask 里所有内部瓦片路径字面量：`/tiles/... `/contour/... `/terrain/...
# 反引号模板串里不会再出现反引号，所以 [^`]* 切得干净。
_TILE_PATH_LITERAL_RE = re.compile(r'`/(?:tiles|contour|terrain)/[^`]*`')


def test_preview_never_builds_a_tile_path_outside_the_resolver():
    """负向守卫：previewTask 里每一个瓦片路径字面量都必须紧跟在 tileUrl( 后面。

    正向锚点只证明「现有这四条改对了」，证明不了「下一条也会走解析器」。
    新增一类预览（比如 `/tiles/${id}/preview` 或又一种 terrain 变体）时，
    照着旁边抄一行却漏掉 tileUrl 包裹是最自然的写法 —— 它不会报错、
    不会少显示，只是那一类瓦片重新绕回主端口去吃 6 连接上限，
    而正向锚点全绿。
    """
    body = _fn_body(_strip_comments(_map_js()), 'previewTask')
    unwrapped = [
        m.group(0) for m in _TILE_PATH_LITERAL_RE.finditer(body)
        if not body[:m.start()].rstrip().endswith('tileUrl(')
    ]
    assert not unwrapped, (
        f'previewTask 里有没经过 tileUrl() 的瓦片路径字面量：{unwrapped} —— '
        '内部绝对路径一律交给页面级解析器，别再自己拼')


def test_preview_never_feeds_a_raw_response_field_into_an_imagery_url():
    """负向守卫：provider 的 url 只接受 tileUrl(...) 或基于已解析 base 的模板串。

    这条钉的正是本任务修的那个缺陷的形状：接口返回体里的字段（hs.url 是
    /hillshade 给的 /terrain/.../hillshade.png）直接塞进 provider。它长得跟
    正常代码一模一样、跑起来图也出得来，只是那一跳退回主端口。白名单写死
    两种合法形态后，任何新的 `url: <接口字段>` 都会立刻红。
    """
    body = _fn_body(_strip_comments(_map_js()), 'previewTask')
    offenders = []
    for m in re.finditer(r'url:\s*([^,\n]+)', body):
        value = m.group(1).strip()
        if value.startswith('tileUrl(') or value.startswith('`${base}'):
            continue
        offenders.append(value)
    assert not offenders, (
        f'previewTask 里有绕过解析器的 provider url：{offenders} —— '
        '只允许 tileUrl(...) 或 `${base}/...`（base 本身已由 tileUrl 解析）')



# ---------------------------------------------------------------------------
# 闸门 9：导入的多边形几何必须两条下载分支都送出去
# ---------------------------------------------------------------------------

def _task_submit_handler(src):
    """两条下载管线的装配体 —— `async function submitDownload(downloadType)`
    的函数体（含外层 {}）。

    2026-08-15 之前这段装配整体写在 `#downloadForm` 的**匿名** submit 监听里，
    所以这个辅助函数当初按「getElementById('downloadForm') 之后
    addEventListener('submit' 之后的第一个 {」花括号配对切。两张表单合并成
    #taskForm 之后，唯一的 submit 监听收成一个按 _currentPipeline() 分派的三行
    调度器，装配搬进了具名函数 submitDownload —— 匿名切法既找不到旧锚点，切到
    调度器也只会拿到三行 await。

    锚点跟着装配搬：下面那些深度断言量的是「taskData.region 挂在 if/else 链
    外面还是关进某一条分支里」，而那条链（`if (downloadType === 'dem')`）连形参
    名都一字未动，就在 submitDownload 里。
    """
    return _fn_body(src, 'submitDownload')


def test_imported_region_is_attached_on_both_download_branches():
    """导入的多边形几何必须同时挂到地图任务与 DEM 任务的载荷上。

    实测缺陷：`taskData.region = _regionSpec` 那一行写在 else（地图）分支
    **里面**，DEM 分支只送 north/south/east/west。于是导入一条 L 形省界建
    DEM 任务时后端只看得见外接矩形 —— DemTaskManager.create_task 里那段
    `if not region.is_rectangle` 的按真实几何过滤（src/services/
    dem_task_manager.py）从界面上根本走不到，颗粒数与外接矩形一个不差。
    浏览器实测：同一条 L 形边界（100..102E / 30..32N，缺右上角那格），
    带 region 建出 3 个颗粒，不带 region 是 4 个。

    钉的是**位置**而不是「文件里有没有这行」：写在任一分支内部都能让字面量
    搜索变绿，而那正是缺陷本身。判据 = 赋值落在 if/else 链闭合之后的
    submitDownload 函数顶层（深度与 `if (downloadType === 'dem')` 那一行相同）。
    """
    body = _strip_comments(_task_submit_handler(_map_js()))

    def depth_at(needle):
        assert needle in body, f'提交处理器里找不到 {needle} —— 本测试已失效'
        i = body.index(needle)
        return body.count('{', 0, i) - body.count('}', 0, i)

    assert body.count('taskData.region = _regionSpec') == 1, (
        'taskData.region 的赋值不止一处 —— 两条分支各写一份就是第二份口径，'
        '下一个分支（等高线？）照样会漏'
    )
    chain = depth_at("if (downloadType === 'dem')")
    # 自检：分支体确实比链头深一层，否则下面的比较量的不是「在不在分支里」。
    inside = depth_at("apiUrl = '/api/dem/tasks';")
    assert inside == chain + 1, (
        f'分支体深度 {inside} 不等于链头深度 {chain} + 1 —— 花括号计数已失效'
    )
    attach = depth_at('taskData.region = _regionSpec')
    assert attach == chain, (
        f'region 的挂载在深度 {attach}（if/else 链在 {chain}）—— 它被关进某一条'
        '下载分支里了。落在 DEM 分支外面就等于「导入多边形建 DEM 任务时几何'
        '静默丢失」，后端按真实几何裁颗粒的那套代码从界面上永远走不到'
    )


def test_the_js_created_map_overlays_all_build_on_the_glass_base_class():
    """由 JS 创建的地图浮层都必须建在 `.map-overlay-chip` 上。

    基类持有「浮在地图上」那一整套：玻璃底色 + 描边 + --radius-card + 阴影 +
    backdrop-filter + 文字色 + 内边距。而三道闸门都是**按选择器点名它**的 ——
    tests/test_elevation_glass.py 的两个降级块名单、tests/test_geometry_scales.py
    的 RAISED_SURFACES 圆角、tests/test_css_contract.py 的文字对比度上下文。
    一个浮层不进这个基类，就同时逃掉这三道，而且**没有任何断言会红**。

    等高线预览面板（地图左下「已完成的等高线瓦片」）改前就是这么逃掉的：它自己
    吐了个 Bootstrap `.alert.alert-info` 当底座，于是左下角是一块 Bootstrap 蓝，
    右上范围浮层与右下预览提示条是玻璃面 —— 整轮系统层收敛（59459b1）一条断言
    都没红，最后靠人眼发现「和系统不协调」。
    """
    src = _strip_comments(_map_js())
    for fn_name, what in (('_renderPreviewChip', '右下预览提示条'),
                          ('contourPreviewPanel', '左下等高线预览面板')):
        body = _fn_body(src, fn_name)
        m = re.search(r"\.className\s*=\s*'([^']*)'", body)
        assert m, (
            f'{fn_name}（{what}）不再给浮层设 className —— 本断言已失效，'
            '去看它现在怎么建这个节点'
        )
        assert 'map-overlay-chip' in m.group(1).split(), (
            f'{what}的 className 是 {m.group(1)!r}，不含 map-overlay-chip —— '
            '它会同时逃掉玻璃降级、圆角与对比度三道闸门（改前那块 Bootstrap '
            '蓝的 alert 就是这么来的）'
        )


def test_the_contour_preview_rows_are_the_shared_chip_not_bootstrap_buttons():
    """预览面板的行按钮用全站通用的 `.status-chip`，不用 Bootstrap 按钮变体。

    `.status-chip` 是本仓的通用 chip（主题开关、强调色、语种、四条管线段控、
    任务状态筛选五处同一套），它的选中态是**安静的**：accent 描边 + accent
    文字，没有饱和填充。改前这里是 `.btn-primary` / `.btn-outline-primary`
    —— 一块蓝底色块压在地图上，与同屏两个玻璃浮层不是一套东西。

    `.active` 与 `aria-pressed` 必须同时翻：只给 CSS 类时读屏用户听不出哪一个
    正在预览（与 #createPipeline 段控同一条纪律，templates/index.html 那里
    有同样的注释）。
    """
    body = _fn_body(_strip_comments(_map_js()), 'updateContourPreviewButtons')
    for banned in ('alert', 'btn-primary', 'btn-outline-primary', "btn btn-sm"):
        assert banned not in body, (
            f'updateContourPreviewButtons 又吐出了 {banned!r} —— 浮在地图上的'
            '面板不许用 Bootstrap 的组件类当底座/按钮，底座走 .map-overlay-chip、'
            '行按钮走 .status-chip'
        )
    assert 'status-chip' in body, '行按钮不再是通用 .status-chip'
    assert 'aria-pressed' in body and 'active' in body, (
        '选中态必须同时写 .active 与 aria-pressed —— 只写类名时读屏用户听不出'
        '哪一个任务正在地图上预览'
    )


# ---------------------------------------------------------------------------
# 2026-08-15 工具条瘦身（9 颗 → 6 颗）+ 四至读数收编进 #createPanel 的新增契约。
# 三条都自带「扫不到东西时响亮失败」的自检：这一类文本断言最常见的死法不是
# 判错，而是锚点没了以后扫出空集合、于是「集合里每一项都满足 X」永真。
# ---------------------------------------------------------------------------


def _js_dir():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, 'static', 'js')


def _all_js_code():
    """{文件名: 已剥注释的源码} —— static/js 下每个 .js。

    「某个 id 全仓零命中」必须扫全目录：退役的节点 id 会残留在别的文件里
    （command_palette.js 就曾 guard 在 #mapDrawRect 上），只扫 map.js 会漏。
    """
    out = {}
    for name in sorted(os.listdir(_js_dir())):
        if not name.endswith('.js'):
            continue
        with open(os.path.join(_js_dir(), name), encoding='utf-8') as f:
            out[name] = _strip_comments(f.read())
    assert out, 'static/js 下一个 .js 都没扫到 —— 本测试已失效'
    return out


def _strip_template_comments(src):
    """剥掉 HTML 注释与 Jinja 注释。

    与 _strip_comments 同一路数、同一个理由：本仓的「删除登记」注释**逐字写着
    被删掉的那些 id**（templates/index.html:53-61 就写着 #mapZoomIn /
    #mapZoomOut / #mapDrawRect，_macros.html:92 还解释了为什么新建图标不复用
    #mapZoomIn 那个裸加号）。不剥注释，「这些 id 不许再出现」就永远红，而修法
    会变成「把登记注释删掉」—— 正好把这次改动的理由抹掉。
    """
    src = re.sub(r'<!--.*?-->', '', src, flags=re.S)
    return re.sub(r'\{#.*?#\}', '', src, flags=re.S)


def _all_template_code():
    """{文件名: 已剥注释的模板源码} —— templates 下每个 .html。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tdir = os.path.join(root, 'templates')
    out = {}
    for name in sorted(os.listdir(tdir)):
        if not name.endswith('.html'):
            continue
        with open(os.path.join(tdir, name), encoding='utf-8') as f:
            out[name] = _strip_template_comments(f.read())
    assert out, 'templates 下一个 .html 都没扫到 —— 本测试已失效'
    return out


def _match_brace(src, j):
    """src[j] 那个 `{` 配对结束位置的下标。"""
    depth = 0
    for k in range(j, len(src)):
        if src[k] == '{':
            depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0:
                return k
    raise AssertionError('花括号不配对 —— 本测试已失效')


def _named_function_spans(src):
    """[(名字, 体起下标, 体止下标)] —— 所有 `function NAME(...) {...}` 声明。

    用来回答「这段源码住在哪个函数里」。跳参数表的理由与 _fn_body 一样：解构
    参数里的花括号排在函数体之前，直接 index('{') 会把参数表当函数体。
    """
    spans = []
    for m in re.finditer(r'function\s+([A-Za-z_$][\w$]*)\s*\(', src):
        depth = 0
        args_close = None
        for k in range(src.index('(', m.end() - 1), len(src)):
            if src[k] == '(':
                depth += 1
            elif src[k] == ')':
                depth -= 1
                if depth == 0:
                    args_close = k
                    break
        assert args_close is not None, f'{m.group(1)} 参数表不配对 —— 本测试已失效'
        j = src.index('{', args_close)
        spans.append((m.group(1), j, _match_brace(src, j)))
    assert spans, '一个具名函数都没扫到 —— 本测试已失效'
    return spans


def _owner_function(spans, at):
    """src[at] 所在的最内层具名函数名（不在任何函数里则 None）。"""
    holding = sorted((b - a, n) for n, a, b in spans if a <= at <= b)
    return holding[0][1] if holding else None


_KEYDOWN_INLINE = re.compile(
    r"addEventListener\(\s*'keydown'\s*,\s*(?:async\s*)?(?:function\s*)?"
    r"\([^)]*\)\s*(?:=>\s*)?\{")
_KEYDOWN_BY_NAME = re.compile(
    r"addEventListener\(\s*'keydown'\s*,\s*([A-Za-z_$][\w$]*)\s*[,)]")


def _keydown_handlers(src):
    """[(注册它的函数名, 处理器体)] —— 每一个 keydown 处理器。

    两种注册形态都要认：内联函数（`addEventListener('keydown', function (e) {…})`）
    与具名引用（`addEventListener('keydown', _handleManualBoundsKeydown)`，
    手动四至面板走的就是这一路）。只认内联的话，缩放消费者哪天改成具名函数就
    会扫成空集合 —— 于是「有且只有一个消费者」静默变成「一个都没有也算过」。
    """
    spans = _named_function_spans(src)
    found = []
    for m in _KEYDOWN_INLINE.finditer(src):
        j = src.index('{', m.end() - 1)
        found.append((_owner_function(spans, j), src[j:_match_brace(src, j) + 1]))
    for m in _KEYDOWN_BY_NAME.finditer(src):
        for name, a, b in spans:
            if name == m.group(1):
                found.append((name, src[a:b + 1]))
    return found


def test_the_keyboard_zoom_path_survives_the_deleted_toolbar_buttons():
    """`+` / `-` 快捷键是键盘用户**唯一**的地图缩放路径，必须存在且不吃输入。

    根因（这条断言存在的全部理由）：**Cesium 自带的相机控制器
    （ScreenSpaceEventHandler / ScreenSpaceCameraController）只处理鼠标与触摸，
    没有任何键盘相机控制**。2026-08-15 工具条瘦身把 #mapZoomIn / #mapZoomOut
    两颗按钮删了 —— 在那之前它们是键盘可达的缩放入口，删掉之后如果 map.js 里
    没有键盘消费者，地图缩放就只剩鼠标滚轮：这不是「界面简化」，是把一项能力
    从无障碍路径上删掉。删按钮不许删能力。

    三件事一起守，缺一件这条快捷键就是坏的：
      1. 有一个 keydown 处理器同时出现 `'+'`、`'-'` 与 zoomMapBy( —— 能力在。
      2. 处理器里有可编辑元素的排除判据（closest('input, textarea, select,
         [contenteditable]')），**而且排在两个按键分支之前**。排在后面等于没有：
         分支先跑完、preventDefault 也发了，用户在「最大层级」数字框里打一个
         负号，地图当场缩小、负号还打不进去。
      3. 带 Ctrl/Meta/Alt 时早退。Ctrl+`+` / Ctrl+`-` 是浏览器自己的页面缩放，
         抢过来就是改掉用户的系统习惯。
    """
    src = _strip_comments(_map_js())
    assert 'function zoomMapBy(' in src, (
        'map.js 没有 zoomMapBy() —— 相机缩放的唯一实现不在了，本测试已失效')

    handlers = _keydown_handlers(src)
    assert handlers, (
        'map.js 里一个 keydown 处理器都没扫到 —— 注册写法变了，本测试已失效'
        '（扫不到就没得可判，下面几条会全部永真）')
    consumers = [(owner, body) for owner, body in handlers if 'zoomMapBy(' in body]
    assert len(consumers) == 1, (
        f'zoomMapBy( 的 keydown 消费者有 {len(consumers)} 个'
        f'（{[o for o, _ in consumers]}）—— 恰好要一个：0 个意味着工具条那两颗'
        '按钮删了之后键盘用户彻底没有缩放路径（Cesium 不带键盘相机控制），'
        '多个意味着同一次按键会缩放两档')
    owner, body = consumers[0]

    for key, what in (r"'\+'", '放大'), ("'-'", '缩小'):
        assert re.search(r'===\s*' + key, body), (
            f'{owner} 里没有 `=== {key}` 分支 —— {what}这一半没有键盘路径')
    # 极性也要钉：两个分支的实参写反（`+` 传 -1）时，上面几条一条都不会红，
    # 而按键行为整个对调。与本文件 test_terrain_submit_sends_the_auto_literal…
    # 那条三元极性断言同一路数。
    plus_at = re.search(r"===\s*'\+'", body).start()
    minus_at = re.search(r"===\s*'-'", body).start()
    assert re.search(r'zoomMapBy\(\s*1\s*\)', body[plus_at:minus_at]), (
        f"{owner} 的 `+` 分支没有 zoomMapBy(1) —— 极性写反了，按 `+` 会缩小")
    assert re.search(r'zoomMapBy\(\s*-1\s*\)', body[minus_at:]), (
        f"{owner} 的 `-` 分支没有 zoomMapBy(-1) —— 极性写反了，按 `-` 会放大")

    editable = re.search(
        r"closest\(\s*'([^']*\[contenteditable\][^']*)'\s*\)", body)
    assert editable, (
        f'{owner} 里没有 closest(\'…[contenteditable]…\') 排除判据 —— 用户在'
        '「最大层级」数字框里打负号、在搜索框里打字都会缩放地图')
    selector = editable.group(1)
    for part in ('input', 'textarea', 'select', '[contenteditable]'):
        assert part in selector, (
            f'排除判据的选择器是 {selector!r}，漏了 {part} —— 漏掉的那类控件'
            '里打字仍会缩放地图')
    assert editable.start() < min(plus_at, minus_at), (
        f'{owner} 里可编辑元素的排除判据排在按键分支**之后**（判据 '
        f'@{editable.start()}，最早的分支 @{min(plus_at, minus_at)}）—— '
        '排在后面等于没有：分支已经 preventDefault 并缩放完了才轮到它')

    modifier = re.search(r'if\s*\(([^)]*)\)\s*return', body)
    assert modifier and all(k in modifier.group(1)
                            for k in ('ctrlKey', 'metaKey', 'altKey')), (
        f'{owner} 里没有 `if (e.ctrlKey || e.metaKey || e.altKey) return;` —— '
        'Ctrl+`+` / Ctrl+`-` 是浏览器自己的页面缩放，抢过来等于改掉用户的系统习惯')
    assert modifier.start() < min(plus_at, minus_at), (
        f'{owner} 里修饰键早退排在按键分支之后 —— 同样等于没有')


# 工具条按钮的实测数：2026-08-15 瘦身 9 → 6（删 #mapZoomIn / #mapZoomOut /
# #mapDrawRect）。剩四组六颗：新建 / 导入区域 / 光照 / (任务·配置·插件)。
# 这个数是 `grep -c 'class="map-panel-btn"' templates/index.html` 的实测值，
# 不是从设计稿抄的。它是**棘轮**：再加一颗就要在这里改数并解释为什么，
# 「东西太多了」是用户实测反馈的原话。
_TOOLBAR_BUTTON_COUNT = 6


def test_the_slimmed_toolbar_stays_at_six_buttons_and_the_deleted_ids_are_gone():
    """工具条恰好 6 颗 .map-panel-btn，且退役的三个 id 全仓零命中。

    两半各守一件事：
      · 计数守「瘦下来了、而且不会一颗一颗涨回去」。9 → 6 是这次改动的可见
        成果，没有棘轮的话下一个功能顺手加一颗，三次之后就回到 9 颗。
      · 零命中守「删干净了」。#mapDrawRect 是最容易留活口的一个：
        command_palette.js 的 start_bounds 曾 guard 在这颗按钮上，节点删了、
        guard 返回 false，命令会**静默从命令面板里消失**（本仓 new_download
        已经栽过一次，command_palette.js:37-45 记着）。所以判据不是「模板里没
        这颗按钮」，是「模板与 static/js 里都不再有人提这个 id」。

    注释里的删除登记要排除（_strip_template_comments / _strip_comments）：
    本仓的登记注释逐字写着被删的 id，不剥注释这条断言永远红，而唯一的「修法」
    是把登记删掉 —— 正好把改动理由抹掉。
    """
    # 剥注释的自检：剥错方向（把正文也剥了）时，下面的零命中断言会静默全过。
    assert 'KEEP' in _strip_template_comments('<b>KEEP</b>{# G #}<i>KEEP</i>')
    assert 'G' not in _strip_template_comments('<!-- G -->{# G #}')
    assert 'KEEP' in _strip_comments('KEEP // G\n/* G */')
    assert 'G' not in _strip_comments('// G\n/* G */')

    templates = _all_template_code()
    index = templates['index.html']
    assert 'id="mapToolbar"' in index, (
        '#mapToolbar 不在 templates/index.html 里了 —— 本测试已失效')
    buttons = re.findall(r'class="map-panel-btn[^"]*"', index)
    assert buttons, (
        '一颗 .map-panel-btn 都没扫到 —— class 写法变了，本测试已失效'
        '（扫成空集合时下面的计数断言会变成「0 == 6」而不是永真，但错因不同，'
        '这里先把它说清楚）')
    assert len(buttons) == _TOOLBAR_BUTTON_COUNT, (
        f'工具条现在有 {len(buttons)} 颗 .map-panel-btn，实测基线是 '
        f'{_TOOLBAR_BUTTON_COUNT} 颗（2026-08-15 从 9 颗瘦到 6 颗）—— '
        '多出来的那颗要么是缩放/框选按钮回来了，要么是新入口没走「先问一句'
        '这颗是不是第二个入口」。真要加，请在 _TOOLBAR_BUTTON_COUNT 那里改数'
        '并写下理由')

    js = _all_js_code()
    for retired, why in (
        ('mapZoomIn', '放大按钮 —— 缩放改由 `+` 快捷键与命令面板 zoom_in 承担'),
        ('mapZoomOut', '缩小按钮 —— 改由 `-` 快捷键与命令面板 zoom_out 承担'),
        ('mapDrawRect', '框选按钮 —— 改由面板选区段的 #createDrawBtn 承担'),
    ):
        stale = sorted(n for n, s in list(templates.items()) + list(js.items())
                       if retired in s)
        assert not stale, (
            f'{stale} 里还在引用 #{retired}（{why}）—— 节点已经不存在，'
            'getElementById 会拿到 null：命令面板的 guard 会让命令静默消失，'
            '直接 .click() 的地方会当场抛 TypeError')


def test_the_bounds_readout_is_rendered_in_exactly_one_place():
    """四至读数只有一处渲染，宿主是面板里的 #createBoundsReadout。

    这条守的是 2026-08-15 那次信息架构改动的核心成果。改前同一个四至在两处
    各渲染一遍：地图右上浮层 #boundsInfo 里的 .bounds-grid（可编辑）与
    #createPanel 里的 #createPanelBounds（只读句子）。两处渲染意味着两处都要
    跟着选区刷新，漏一处就是屏幕上两个数字不一致 —— 而用户没有办法知道哪个是
    真的。改后浮层整块退场、只读句子删除，读数进 #createBoundsReadout。

    判据分三层：
      1. 生成 `class="bounds-v"`（四至数值格）的函数恰好一个，就是
         updateBoundsInfo。数值格是「渲染四至」的唯一标志物。
      2. 生成 `class="bounds-grid"` 的函数恰好两个，实测是 updateBoundsInfo 与
         _renderManualBounds。为什么 2 不是 1：后者是**手动输入态**那张表单
         （4 个 input 复用同一套网格外观），它不渲染 currentBounds 的值，
         所以不构成第二处读数 —— 这里额外钉住它体内没有 .bounds-v，防止哪天
         有人往手动输入面板里塞一份读数副本。
      3. 宿主是 #createBoundsReadout，且 #boundsInfo 这个 id 在 static/js 里
         零命中（局部变量名 boundsInfo 还在，不判它 —— 判的是 id 字面量与
         选择器）。宿主换回地图浮层就是把这次收敛整个回退。
    """
    js = _all_js_code()
    spans = {name: _named_function_spans(src) for name, src in js.items()}

    def producers(literal):
        out = []
        for name, src in js.items():
            for m in re.finditer(re.escape(literal), src):
                out.append((name, _owner_function(spans[name], m.start())))
        return out

    value_cells = producers('class="bounds-v"')
    assert value_cells, (
        'static/js 里一处 class="bounds-v" 都没扫到 —— 四至数值格的 markup '
        '变了，本测试已失效（去看 updateBoundsInfo 现在怎么渲读数）')
    assert set(value_cells) == {('map.js', 'updateBoundsInfo')}, (
        f'四至数值格（.bounds-v）现在由 {sorted(set(value_cells))} 生成 —— '
        '恰好要 map.js::updateBoundsInfo 一处。第二处渲染就是改前那个缺陷本体：'
        '同一个四至两个地方各画一遍，刷新漏一处屏幕上就出现两个不一样的数字')

    grids = producers('class="bounds-grid"')
    assert set(grids) == {('map.js', 'updateBoundsInfo'),
                          ('map.js', '_renderManualBounds')}, (
        f'.bounds-grid 现在由 {sorted(set(grids))} 生成 —— 实测基线是'
        ' updateBoundsInfo（读数）与 _renderManualBounds（手动输入表单）两处，'
        '多出来的那处要么是又冒出一份读数副本，要么是本测试已失效')
    manual = _fn_body(js['map.js'], '_renderManualBounds')
    assert 'class="bounds-v"' not in manual, (
        '_renderManualBounds 里出现了 .bounds-v —— 手动输入表单只该有 input，'
        '塞进读数就是第二处渲染')

    readout = _fn_body(js['map.js'], 'updateBoundsInfo')
    assert "getElementById('createBoundsReadout')" in readout, (
        'updateBoundsInfo 的宿主不是 #createBoundsReadout —— 读数 2026-08-15 '
        '从地图右上的浮层搬进了 #createPanel 的选区段，宿主搬回去等于把这次'
        '收敛整个回退（浮层里那份可编辑读数与面板里的数字会再次并存）')
    for name, src in js.items():
        hit = re.search(r"""['"]boundsInfo['"]|#boundsInfo""", src)
        assert not hit, (
            f'{name} 里还在按 id 找 #boundsInfo（{src[hit.start():hit.start() + 40]!r}）'
            '—— 那层地图浮层整块删了，这里拿到的永远是 null')