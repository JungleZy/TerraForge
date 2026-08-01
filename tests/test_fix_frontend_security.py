"""2026-07-29 swarm review 前端修复的文本级契约测试。

项目没有 JS 测试框架（无 package.json/vitest，引入会破坏 PyInstaller 离线
打包形态），所以与 test_tasks_js_contract.py 一样守源码**形态**。

覆盖的评审条目：
  C6  存储型 XSS——用户/服务端可控字符串进 innerHTML 前必须过
      escapeHtml()（ui.js 统一导出）。error_message 走 textContent 的
      既有约定不变。
  I11 ASTGTM.003 不发布 _swb 颗粒（水体在 ASTWBD.001），前端必须禁用
      该勾选项，勾选注定 404。
  I18 task_completed 事件带 warning 字段（部分 zoom 拼接失败但任务判
      完成）时，前端必须弹 toast，不能删卡片零提示。
  minor 等高线 start 响应、四路任务列表 fetch 必须检查 response.ok。
"""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用既有测试的花括号配对切函数体工具与注释剥离器，不另写一份
# （理由见 test_tasks_js_contract.py 头部注释）。
from test_css_contract import _js_function_body  # noqa: E402
from test_tasks_js_contract import _js, _strip_js_comments  # noqa: E402


def _body(js_name, fn_name):
    body = _js_function_body(_strip_js_comments(_js(js_name)), fn_name)
    assert body.strip(), f'{js_name} 的 {fn_name} 函数体切出来是空的 —— 本测试已失效'
    return body


# ---------------------------------------------------------------- C6: XSS

def test_escape_html_helper_defined_and_exported():
    """ui.js 必须定义 escapeHtml 并挂到 window（各文件都是无模块的全局脚本）。"""
    src = _strip_js_comments(_js('ui.js'))
    assert 'function escapeHtml(' in src, 'ui.js 没有定义 escapeHtml()'
    assert re.search(r'window\.escapeHtml\s*=\s*escapeHtml', src), (
        'escapeHtml 没有导出到 window——tasks.js/history.js/map.js 拿不到'
    )
    body = _body('ui.js', 'escapeHtml')
    # 五个危险字符一个都不能漏；& 必须最先替换（否则会把后四条的实体二次转义）。
    assert '.replace(/&/g,' in body, 'escapeHtml 漏了 &（且它必须是第一个 replace）'
    assert '&amp;' in body and '&lt;' in body and '&gt;' in body, (
        'escapeHtml 的实体不全（& < >）'
    )
    assert '&quot;' in body, 'escapeHtml 漏了双引号（属性值插值会破壳）'
    assert '&#39;' in body or '&#x27;' in body, 'escapeHtml 漏了单引号'
    assert body.index('/&/g') < body.index('/</g'), (
        '& 必须是第一个替换——放在后面会把 &lt; 之类二次转义成 &amp;lt;'
    )


def test_task_name_never_enters_innerhtml_raw():
    """task.name 是用户提交的任务名——三个文件里不许再出现裸 `${task.name}`。"""
    for name in ('tasks.js', 'history.js', 'map.js'):
        src = _strip_js_comments(_js(name))
        assert '${task.name}' not in src, (
            f'{name} 仍把 task.name 裸拼进 HTML 模板（存储型 XSS）'
        )
    # 2026-08 单一时间流定稿：行渲染收口 history.js createTaskRow，tasks.js
    # 不再有行模板（原先这里断言 tasks.js 的 escapeHtml(task.name)）。
    hist = _strip_js_comments(_js('history.js'))
    assert hist.count('escapeHtml(task.name)') >= 2, (
        'history.js 应至少有两处转义 task.name：时间流行（createTaskRow）'
        ' + 历史地图 InfoBox description'
    )


def test_map_preview_names_escaped():
    """map.js 预览 chip 与等高线预览按钮里的任务名同样来自服务端。"""
    src = _strip_js_comments(_js('map.js'))
    assert '${_previewState.name}' not in src, '_renderPreviewChip 裸拼任务名'
    assert '${info.name' not in src, 'updateContourPreviewButtons 裸拼任务名'
    assert 'escapeHtml(_previewState.name)' in src
    assert 'escapeHtml(info.name' in src


def test_history_secondary_fields_escaped():
    """history.js 其余服务端字段：style 兜底原文、详情徽章状态、地形 job.output_dir。"""
    src = _strip_js_comments(_js('history.js'))
    # 2026-08 统一流式列表重设计：行内样式/缩放文本改由 historyMetaText()
    # 组装、在插值点统一转义。本条的两个旧锚点
    # （escapeHtml(getStyleText(task.style)) / escapeHtml(task.style || '-')）
    # 是 9 列历史行「样式格」的写法，随表格删除；守护的语义不变——
    # getStyleText 的 `|| style` 兜底会把 DB 里的 style 原文带出来，
    # 进模板前必须过 escapeHtml。改为断言「组装函数里有兜底来源 +
    # 插值点确实转义」两个环节，只查一个都会留洞。
    meta_body = _body('history.js', 'historyMetaText')
    assert 'getStyleText(task.style)' in meta_body, (
        'historyMetaText 没有调 getStyleText——样式元信息的来源变了？本测试已失效'
    )
    assert 'escapeHtml(historyMetaText(task))' in src, (
        'createTaskRow 没有转义 historyMetaText 的输出——'
        'getStyleText 的 `|| style` 兜底会把 DB 里的 style 原文渲染进行1'
    )
    assert 'escapeHtml(getStatusText(task.status))' in src, (
        '详情模态框徽章里 getStatusText 的 `|| status` 兜底原文未转义'
    )
    assert re.search(r'const label = escapeHtml\(', src), '地形作业状态徽章的 label 未转义'
    assert 'escapeHtml(outDir)' in src, 'job.output_dir（用户可控路径）未转义'


def test_active_row_meta_text_escaped():
    """tasks.js 不再持有行模板——行渲染收口在 history.js（2026-08 单一时间流定稿）。

    本检查的前身守「tasks.js createTaskRow 里 escapeHtml(taskMetaText(task))」，
    与上面 history.js 那条是对称检查——当时两个文件各自渲染行，最容易漏的
    就是只转义了一边。单一时间流定稿后全站只剩 history.js createTaskRow
    一处行实现（转义由上面那条守），这里改守「tasks.js 没有长回第二份
    行模板」——两份实现必然漂移，包括转义。
    """
    src = _strip_js_comments(_js('tasks.js'))
    for fn in ('createTaskRow', 'taskMetaText'):
        assert f'function {fn}(' not in src, (
            f'tasks.js 又长出了 {fn}()——行渲染已收口 history.js，'
            '第二份实现（含转义义务）必然漂移'
        )


# ---------------------------------------------------------------- I11: _swb

def test_astgtm_swb_checkbox_is_disabled():
    """ASTGTM.003 只发 _dem/_num，_swb 勾选必然 404——前端禁用并强制不勾。"""
    src = _strip_js_comments(_js('map.js'))
    assert "getElementById('demDownloadSwb')" in src, '本测试失效（元素改名了？）'
    assert re.search(r'\.disabled\s*=\s*true', src), (
        'demDownloadSwb 没有被禁用——用户仍能勾上一个注定失败的选项'
    )
    assert re.search(r'\.checked\s*=\s*false', src), (
        '禁用前没有强制取消勾选——已勾状态会随表单提交上去'
    )


# ---------------------------------------------------------------- I18: warning

def test_task_completed_warning_reaches_a_toast():
    """task_completed 的 warning 字段必须变成用户可见的 warning toast。"""
    src = _strip_js_comments(_js('tasks.js'))
    assert re.search(
        r'handleTaskCompleted\(\s*data\.task_id,[^)]*data\.warning', src
    ), 'socket 处理器没有把 data.warning 传给 handleTaskCompleted'
    body = _body('tasks.js', 'handleTaskCompleted')
    assert 'warning' in body, 'handleTaskCompleted 函数体里没有消费 warning'
    m = re.search(r'showToast\s*\((.*?)\)\s*;', body, re.S)
    assert m, 'handleTaskCompleted 没有调 showToast——warning 被静默丢弃'
    assert "'warning'" in m.group(1), (
        f'toast 类型应为 warning：{m.group(1)!r}'
    )


# ---------------------------------------------------------------- minors

def test_contour_start_response_is_checked():
    """submitContour 里创建成功后的 /start 响应必须查 ok，失败要提示。"""
    body = _body('map.js', 'submitContour')
    assert re.search(r'=\s*await\s+fetch\(`/api/contour/tasks/\$\{created\.task_id\}/start`', body), (
        '/start 的 fetch 没有接返回值——本测试失效（写法变了？）'
    )
    assert re.search(r'!\s*startResp\.ok', body), (
        '等高线 start 响应没查 ok——创建成功但启动失败会误报「任务已开始」'
    )


def test_load_active_tasks_checks_response_ok():
    """四路任务列表 fetch 任何一路非 2xx 都不能当空列表渲染（卡片会被整页清空）。"""
    body = _body('tasks.js', 'loadActiveTasks')
    assert re.search(r'!\s*\w+\.ok', body), (
        'loadActiveTasks 没查 response.ok——接口 500 会被当成「没有活动任务」渲染'
    )
