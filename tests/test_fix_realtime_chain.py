"""2026-08 系统性 review 第三轮：前端实时链路跨层修复的文本级契约测试。

与 test_tasks_js_contract.py 同一套路（项目没有 JS 测试框架，守源码**形态**）。
覆盖条目：
  高  prependStreamRow 无查重 —— 活动任务被 100 条窗口挤掉 / 启动竞态
      两条路径都会在同一时间流里插出第二行。
  高  loadActiveTasks 带 ?status=active（contour 路除外：它的全量响应
      共享给地图预览面板筛 completed）。
  中  断线重连只补拉活动列表，断线窗口内的终态变化让时间流/统计卡永久 stale。
  低  终态事件各自立即 loadStats，批量收官 N 连发 —— 300ms 去抖。
  低  deleteTask 成功后不关那条常驻失败 toast。
  低  详情弹窗 new bootstrap.Modal 叠实例（全站其余各处都是 getOrCreateInstance）。
  低  /api/contour/tasks 首屏拉两遍（loadActiveTasks + initContourPreview）。
  低  toast 容器无总量上限，常驻失败 toast 批量堆高。
"""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用既有测试的源码读取/注释剥离/函数体切分工具，不另写一份
# （理由见 test_tasks_js_contract.py 头部注释）。
from test_css_contract import _js_function_body  # noqa: E402
from test_tasks_js_contract import _js, _strip_js_comments  # noqa: E402


def _body(js_name, fn_name):
    body = _js_function_body(_strip_js_comments(_js(js_name)), fn_name)
    assert body.strip(), f'{js_name} 的 {fn_name} 函数体切出来是空的 —— 本测试已失效'
    return body


def test_prepend_stream_row_dedups_against_existing_row():
    """prependStreamRow 必须查重：行已存在就合并，不插第二行。

    单一时间流「无去重」定稿的前提是四表 UNION 每任务一行；prepend 是
    唯一可能造出重复行的入口（100 条窗口挤掉 + 启动竞态），必须在这里兜住。

    2026-08 Vue 化后有两层保证，两层都钉：
      1. 数据层 —— store 按 key 合并（has → patch），不会出现两条记录；
      2. 渲染层 —— 组件是 keyed v-for，同一个 :key 在结构上不可能渲染两行。
    改造前只有一层，而且是手写的（getElementById 查重 + 改走整行重建），
    查重写在插入之后就会失效。
    """
    body = _body('tasks.js', 'prependStreamRow')
    assert 'TaskStore.has(' in body, (
        'prependStreamRow 没有查 store 里是否已有该 key——'
        '被 100 条窗口挤掉的活动任务会在时间流里出现两行'
    )
    assert 'TaskStore.patch(' in body, (
        '查重命中后没有合并——已存在的行应保持单份并更新到最新形态'
    )
    # 查重必须发生在插入之前
    assert body.index('TaskStore.has(') < body.index('TaskStore.upsert('), (
        '查重发生在插入之后——重复行已经插进去了'
    )
    # 渲染层：keyed v-for
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', 'task_list.js'), encoding='utf-8') as f:
        comp = f.read()
    assert re.search(r'v-for="task in tasks"\s+:key="task\._key"', comp), (
        '时间流不是 keyed v-for —— 少了这层结构性保证，重复行只能靠数据层兜'
    )


def test_load_active_tasks_passes_status_active_except_contour():
    """map/dem/local 三路带 ?status=active；contour 不带（响应共享给预览面板）。

    contour 是**有意**的例外：initContourPreview 要从同一份响应里筛
    completed 任务，带 active 过滤等于把预览面板的数据源掐掉、首屏还得
    再拉一遍全量——那正是「首屏拉两次」要修的问题。
    """
    body = _body('tasks.js', 'loadActiveTasks')
    for url in ("/api/tasks?status=active",
                "/api/dem/tasks?status=active",
                "/api/terrain/local/tasks?status=active"):
        assert f"fetch('{url}')" in body, (
            f'loadActiveTasks 没有给 {url.split("?")[0]} 加 ?status=active——'
            'completed 仍随每次补拉往返'
        )
    assert "fetch('/api/contour/tasks?status=active')" not in body, (
        'contour 路带了 ?status=active——预览面板筛不到 completed 任务，'
        '首屏 /api/contour/tasks 又得拉第二遍'
    )
    assert 'latestContourTasks' in body, (
        'loadActiveTasks 没有把 contour 全量响应存进 latestContourTasks——'
        'initContourPreview 没有可共享的数据源'
    )


def test_reconnect_refreshes_stream_and_stats():
    """断线重连（hasConnectedOnce 分支）除 loadActiveTasks 外，还要补拉
    时间流（loadHistory）与统计卡（loadStats）——断线窗口内的终态变化
    不会补发 socket 事件，不补拉就永久 stale。跨文件全局必须 typeof 守卫。"""
    body = _body('tasks.js', 'initTasks')
    assert 'hasConnectedOnce' in body, 'initTasks 没有 hasConnectedOnce 分支——本测试已失效'
    assert re.search(r"typeof loadHistory === 'function'", body), (
        '重连补拉 loadHistory 没有 typeof 守卫'
    )
    assert re.search(r"typeof loadStats === 'function'", body), (
        '重连补拉 loadStats 没有 typeof 守卫'
    )
    assert 'loadHistory(' in body and 'loadStats()' in body, (
        '重连分支没有补拉 loadHistory/loadStats——'
        '断线窗口内的终态变化会让时间流和统计卡永久停在旧状态'
    )


def test_terminal_event_loadstats_is_debounced():
    """task_completed/task_failed 的 loadStats 必须 300ms 去抖，不能每事件立即发。

    批量收官（N 个任务相近时间到终态）时立即刷新就是 N 连发。字面量
    loadStats() 必须保留在函数体内——test_tasks_js_contract.py 的两条
    既有断言按正则点名它。
    """
    for fn in ('handleTaskCompleted', 'handleTaskFailed'):
        body = _body('tasks.js', fn)
        assert re.search(r'loadStats\(\s*\)', body), (
            f'{fn} 里的 loadStats() 字面量没了——先把既有契约测试修回来'
        )
        assert 'clearTimeout(' in body and re.search(r'setTimeout\(', body), (
            f'{fn} 的 loadStats 没有去抖——批量收官时统计卡刷新 N 连发'
        )


def test_delete_task_closes_the_persistent_failure_toast():
    """deleteTask 成功后必须 closeFailureToast(key)：记录删了，常驻提示不该留着。"""
    body = _body('history.js', 'deleteTask')
    assert re.search(r"typeof closeFailureToast === 'function'", body), (
        'deleteTask 调 closeFailureToast 没有 typeof 守卫——'
        '独立页 /history 不加载 tasks.js，会 ReferenceError'
    )
    assert 'closeFailureToast(' in body, (
        'deleteTask 成功分支没有关掉常驻失败 toast——记录没了提示还留在右上角'
    )


def test_detail_modal_uses_get_or_create_instance():
    """详情弹窗必须 getOrCreateInstance（与 map.js 一致），不许 new 叠实例。"""
    src = _strip_js_comments(_js('history.js'))
    assert 'new bootstrap.Modal' not in src, (
        'history.js 仍在 new bootstrap.Modal——重复打开会叠多个实例/多层遮罩'
    )
    body = _body('history.js', 'viewTaskDetails')
    assert 'bootstrap.Modal.getOrCreateInstance(' in body, (
        'viewTaskDetails 没有用 getOrCreateInstance 打开详情弹窗'
    )


def test_contour_preview_shares_the_first_load_instead_of_refetching():
    """initContourPreview 不再自己 fetch /api/contour/tasks，改为等
    firstActiveTasksLoad resolve 后消费 latestContourTasks（首屏只拉一遍）。

    消费动作本体在 syncContourPreviewFromLatest（2026-08 抽出）：断线重连时
    loadActiveTasks 也要调它补录，两个调用点必须是同一份实现。
    """
    body = _body('map.js', 'initContourPreview')
    assert 'fetch(' not in body, (
        'initContourPreview 仍在 fetch——/api/contour/tasks 首屏拉两遍'
    )
    assert 'firstActiveTasksLoad' in body, (
        'initContourPreview 没有等 loadActiveTasks 的首屏共享 promise'
    )
    assert 'syncContourPreviewFromLatest' in body, (
        'initContourPreview 没有消费 loadActiveTasks 共享的 contour 数据'
    )
    sync = _body('map.js', 'syncContourPreviewFromLatest')
    assert 'latestContourTasks' in sync, (
        'syncContourPreviewFromLatest 的数据源不是 latestContourTasks'
    )
    assert 'fetch(' not in sync, (
        'syncContourPreviewFromLatest 在自己拉接口——它只该消费共享数据'
    )


def test_reconnect_backfills_contour_preview_registry():
    """断线重连必须补录预览注册表。

    socket.io 不重放错过的事件：断线窗口内完成的等高线任务收不到
    task_completed，只靠事件注册的话那些任务要到整页刷新才会出现预览按钮。
    补录挂在 loadActiveTasks（它本来就是 connect 重连分支补拉的一环）。
    """
    body = _body('tasks.js', 'loadActiveTasks')
    assert 'syncContourPreviewFromLatest' in body, (
        'loadActiveTasks 没有补录等高线预览注册表——断线期间完成的任务不会出现预览按钮'
    )
    assert "typeof syncContourPreviewFromLatest === 'function'" in body, (
        '补录调用缺 typeof 守卫——独立页不加载 map.js，会 ReferenceError'
    )


def test_toast_container_has_a_total_cap():
    """toast 必须有总量上限：超限时最旧的自动收起（常驻失败 toast 设计不变）。"""
    body = _body('ui.js', 'showToast')
    assert 'MAX_TOASTS' in body, (
        'showToast 没有总量上限——批量失败时常驻 toast 无限堆高'
    )
    assert re.search(r'openToasts\[0\]\.close\(\)', body), (
        '超上限时没有收起最旧的 toast'
    )
