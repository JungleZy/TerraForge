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
    assert 'resetForm({ clearBounds: false })' in src, (
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


def test_edit_events_are_wired():
    """
    编辑工具栏开着(edit.featureGroup),就必须监听编辑事件,
    否则用户拖拽改框后提交的还是旧 bbox。
    """
    src = _map_js()
    assert 'L.Draw.Event.EDITED' in src, "未监听 draw:edited,保存编辑后 bbox 不更新"
    assert 'L.Draw.Event.EDITRESIZE' in src, "未监听 draw:editresize,拖角时四至不实时更新"
    assert 'L.Draw.Event.EDITSTOP' in src, "未监听 draw:editstop,取消编辑后 bbox 不还原"


def test_bounds_sync_helper_exists():
    src = _map_js()
    assert 'function syncBoundsFromDrawnItems(' in src, (
        "map.js 应定义 syncBoundsFromDrawnItems()"
    )
