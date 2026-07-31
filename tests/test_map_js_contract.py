"""
map.js behavioural contract tests (text-level regression guards).

本项目没有 JS 测试框架(无 package.json/vitest,且不打算引入——会破坏
PyInstaller 离线打包形态)。这些测试用文本断言守住关键契约,真实行为
由 playwright 手工实测覆盖(见计划 Task 10)。
"""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _map_js():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', 'map.js'), encoding='utf-8') as f:
        return f.read()


def test_reset_form_helper_exists():
    """三处重复的表单重置逻辑必须收敛成一个函数"""
    src = _map_js()
    assert 'function resetForm(' in src, "map.js 应定义 resetForm()"


def test_reset_form_is_used_at_every_call_site():
    """三处提交成功分支都必须调 resetForm(),本地高程分支还必须保留 bbox"""
    src = _map_js()
    # 1 处函数定义 + 3 处调用(map/dem、contour、local_terrain)
    assert src.count('resetForm(') >= 4, (
        "resetForm() 应有 1 处定义 + 3 处调用;少于 4 次说明某个提交分支没走统一重置"
    )
    # 数据下载/数据处理拆成两张独立表单后,resetForm 用 formId 指明复位哪一张;
    # 两条处理类分支必须复位 #processForm,本地高程还必须 clearBounds:false
    # (该模式没有 bbox,清框会删掉用户为下一个任务画好的选区)。
    assert "resetForm({ formId: 'processForm' })" in src, (
        "等高线分支必须复位处理表单 resetForm({ formId: 'processForm' })"
    )
    assert "resetForm({ clearBounds: false, formId: 'processForm' })" in src, (
        "本地高程切片没有 bbox,重置时必须传 clearBounds:false,"
        "否则会清掉用户为下一个任务画好的框"
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

    三处提交处理器开头都会 btn.disabled = true 给按钮上锁,唯一的解锁路径
    就是 finally 里那一行 refreshSubmitButtonState()。删掉它,提交失败后
    按钮会永久卡死,而其他断言全绿。这里补上存在性断言。
    """
    src = _map_js()

    # finally 块内目前没有嵌套花括号,所以 [^}]* 足够界定块体
    finally_blocks = re.findall(r'\}\s*finally\s*\{([^}]*)\}', src)
    assert len(finally_blocks) == 3, (
        "预期 3 处提交处理器各有一个 finally 块(map/dem、contour、local_terrain);"
        f"实际找到 {len(finally_blocks)} 个。块结构变了就要同步更新本测试"
    )
    for block in finally_blocks:
        assert 'refreshSubmitButtonState()' in block, (
            "每个 finally 块都必须调 refreshSubmitButtonState() 解锁按钮,"
            "否则提交失败后按钮永久禁用"
        )

    # 9 = 1 处定义 + apply() + CREATED + DELETED + syncBoundsFromDrawnItems()
    #     + resetForm() + 3 处 finally
    assert src.count('refreshSubmitButtonState(') >= 9, (
        "refreshSubmitButtonState 的定义/调用点少于 9 处,说明某个状态变更路径"
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
    assert '大任务确认' in src and 'showConfirm(' in src, (
        '瓦片数超软阈值时，提交前必须弹大任务确认框（0.1.4 放开硬上限后的把关）'
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
