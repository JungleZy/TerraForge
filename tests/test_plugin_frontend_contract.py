"""前端 'plugin' task_type 的接线契约。

两类断言，缺一不可：

1. **源码级**（与 test_tasks_js_contract 同款）：分支在不在、URL 对不对。
   前端字段名写错在 JS 里是静默 `undefined`，没有任何运行时报错，所以只能在
   源码上钉住。
2. **真实链路**：造一条插件任务、请求 `/api/history_all`，逐字核对
   `normalizeTask(task, 'plugin')` 读的每个字段名都真的在那一行里。
   第 1 类测不出「字段名存在但服务端不发它」—— 而 UNION 第五段刻意把
   `total_items`/`downloaded_items` 别名成了 `total`/`downloaded`（五段列序
   要对齐），两套名字只吃一半的表现是历史流里插件行进度恒为 0%。
"""

import re
from pathlib import Path

# 真实链路那一条要造一条插件任务：假插件的安装步骤与 test_plugins_api 完全
# 一样，抄第二份就是抄一份会漂的 manifest。
from tests.test_plugins_api import _install_fake

ROOT = Path(__file__).resolve().parent.parent


def _js(name):
    return (ROOT / 'static/js' / name).read_text(encoding='utf-8')


# --------------------------------------------------------------- 源码级契约

def test_normalize_task_has_plugin_branch():
    src = _js('task_center.js')
    assert "type === 'plugin'" in src
    assert '`plugin:${task.id}`' in src


def test_normalize_task_plugin_accepts_both_field_namings():
    """socket 推送给 *_items，/api/history_all 给别名后的 total/downloaded。

    只吃一半的表现：那一半的来源里进度恒为 0%（`undefined || 0`），而 JS 不报错。
    """
    src = _js('task_center.js')
    branch = src[src.index("if (type === 'plugin')"):]
    branch = branch[:branch.index("if (type === 'contour')")]
    assert 'task.total_items || task.total' in branch
    assert 'task.downloaded_items || task.downloaded' in branch


def test_api_prefix_has_plugin_branch():
    src = _js('task_center.js')
    assert "if (taskType === 'plugin') return '/api/plugins/tasks';" in src


def test_socket_events_registered():
    src = _js('task_center.js')
    for event in ('plugin_task_progress', 'plugin_task_completed',
                  'plugin_task_failed'):
        assert f"socket.on('{event}'" in src


def test_plugin_completed_handler_passes_status_through():
    """plugin_task_completed 是插件管线**每一个非 failed 终态**的出口，
    含 pending_decision —— 状态写死就会把「等你决定」静默改判成「已完成」。"""
    src = _js('task_center.js')
    handler = src[src.index("socket.on('plugin_task_completed'"):]
    assert re.search(r'handleTaskCompleted\([^)]*data\.status', handler)


def test_completed_handler_only_drops_terminal_tasks_from_active_set():
    """pending_decision 经 plugin_task_completed 到达，不能被摘出活动集：
    它还占着产物目录、还在等用户决定，摘掉就是状态栏少算一个任务。"""
    src = _js('task_center.js')
    body = src[src.index('function handleTaskCompleted'):]
    body = body[:body.index('\nfunction ')] if '\nfunction ' in body else body
    assert 'ACTIVE_STATUSES' in body, (
        'handleTaskCompleted 还在无条件 dropActive —— 判据必须是新状态本身，'
        '且走 store 那份唯一的活动态清单'
    )


def test_load_active_tasks_fetches_the_plugin_route_without_breaking_others():
    """第五路必须在同一个 Promise.all 里（setActive 是整体替换，独立请求会被
    它抹掉），但**不能**进 badResp 快速失败：插件宿主是可选组件。"""
    src = _js('task_center.js')
    body = src[src.index('async function loadActiveTasks'):]
    body = body[:body.index('\n}\n')]
    assert "fetch('/api/plugins/tasks?active=1')" in body
    assert 'pluginResp' in body and 'Promise.all' in body
    bad = re.search(r'const badResp = \[([^\]]*)\]', body)
    assert bad, 'badResp 快速失败那一行不见了'
    assert 'pluginResp' not in bad.group(1), (
        '插件路进了 badResp 快速失败 —— 插件宿主一次 500 会把另外四条管线的'
        '任务从界面上一起清空'
    )
    assert "normalizeTask(t, 'plugin')" in body


def test_gap_summary_fetch_is_pipeline_aware():
    """插件的缺块摘要在 /api/plugins/tasks/<id>/gaps，不是 /api/tasks/<id>/gaps。"""
    src = _js('task_center.js')
    body = src[src.index('async function fetchGapSummary'):]
    body = body[:body.index('\n}\n')]
    assert 'apiPrefixForType(taskType)' in body, (
        'fetchGapSummary 还把 /api/tasks 写死 —— 插件任务的缺块明细永远 404'
    )
    ensure = src[src.index('async function ensureGapSummary'):]
    ensure = ensure[:ensure.index('\n}\n')]
    assert "taskType !== 'plugin'" in ensure and "taskType !== 'map'" in ensure


def test_accept_gaps_uses_the_plugin_endpoint_and_defers_to_pushes():
    """插件的接受缺块端点用连字符，且它**重跑任务**（accept_gaps 末尾
    start_task）—— 本地写终态会与随后到达的 plugin_task_progress 打架。"""
    src = _js('task_center.js')
    body = src[src.index('async function acceptTaskGaps'):]
    body = body[:body.index('\n}\n')]
    assert '/api/plugins/tasks/${taskId}/accept-gaps' in body
    assert '/api/tasks/${taskId}/accept_gaps' in body
    plugin_path = body[body.index('if (isPlugin)'):]
    # 插件那条路以早返回收尾 —— 切到它，否则下面量到的是地图那一段。
    plugin_path = plugin_path[:plugin_path.index('return;')]
    assert 'loadActiveTasks()' in plugin_path
    assert 'dropActive' not in plugin_path, (
        '插件的接受缺块路径把任务摘出了活动集 —— 后端下一刻就重跑它'
    )


def test_task_list_start_and_pause_are_separate_switches():
    """插件任务有 start（POST /api/plugins/tasks/<id>/start）但没有
    pause/resume。合成一个开关会把启动按钮一起摘掉。"""
    src = _js('task_list.js')
    assert 'supportsStart' in src and 'supportsPauseResume' in src
    start_btn = re.search(r'<button v-if="hasTaskActions[^"]*'
                          r"task\.status === 'pending'\"", src)
    assert start_btn, '启动按钮的 v-if 不见了'
    assert 'supportsStart' in start_btn.group(0)
    assert 'supportsPauseResume' not in start_btn.group(0)

    body = src[src.index('supportsPauseResume()'):]
    body = body[:body.index('},')]
    assert "'plugin'" in body and "'local_terrain'" in body


def test_task_list_gap_gates_allow_accept_but_not_refill_for_plugin():
    src = _js('task_list.js')
    decide = src[src.index('canDecideGaps()'):]
    decide = decide[:decide.index('},')]
    assert "'plugin'" in decide and "'map'" in decide

    refill = src[src.index('canRefill()'):]
    refill = refill[:refill.index('},')]
    assert "this.task.task_type !== 'plugin'" in refill, (
        '插件任务渲染了补漏按钮 —— 宿主没有 /refill 端点，点下去必然 404'
    )


def test_task_list_does_not_offer_preview_for_plugin_tasks():
    """map.js 的 previewTask 只认四条核心管线，分支链没有 else ——
    不挡就是一颗静默无反应的按钮。"""
    src = _js('task_list.js')
    body = src[src.index('canPreview()'):]
    body = body[:body.index('},')]
    assert "task_type !== 'plugin'" in body


def test_task_list_counts_plugin_items_as_tiles():
    src = _js('task_list.js')
    body = src[src.index('itemLabel()'):]
    body = body[:body.index('},')]
    assert "'plugin'" in body


def test_history_branches():
    src = _js('history.js')
    assert src.count("'plugin'") >= 3


def test_task_list_knows_plugin():
    assert "'plugin'" in _js('task_list.js')


def test_history_detail_and_delete_use_the_plugin_endpoint():
    src = _js('history.js')
    assert src.count(
        "taskType === 'plugin' ? `/api/plugins/tasks/${taskId}`") == 2, (
        '详情与删除两处 URL 必须都有 plugin 分支'
    )


def test_history_detail_reads_plugin_count_columns():
    """plugin_tasks 的计数列是 *_items，不是 tasks 表那套 *_tiles ——
    走 else 分支的话详情弹窗三格全是 undefined。"""
    src = _js('history.js')
    body = src[src.index('async function viewTaskDetails'):]
    branch = body[body.index("} else if (taskType === 'plugin') {"):]
    branch = branch[:branch.index('} else {')]
    for field in ('task.plugin_id', 'task.total_items',
                  'task.downloaded_items', 'task.failed_items'):
        assert field in branch, f'插件详情分支没读 {field}'


# ------------------------------------------------- 缺凭据的源：界面必须说清楚

#: `#downloadForm` 的 submit 监听体。它是匿名监听，没有函数名可以 index。
def _download_submit_body():
    src = _js('map.js')
    body = src[src.index(
        "document.getElementById('downloadForm')?.addEventListener('submit'"):]
    return body[:body.index('\n});\n')]


def _init_source_options_body():
    src = _js('map.js')
    body = src[src.index('async function initPluginSourceOptions'):]
    return body[:body.index('\n}\n')]


def test_source_options_carry_the_credential_state():
    """选项上必须挂 credential_ready，否则提交前那道校验没有判据可读。

    后端把「填没填」算成了布尔（registry.list_sources 的 credential_ready），
    前端不接就等于白算：用户选了没填 token 的源，一路放行到 401 一屏红块。
    """
    body = _init_source_options_body()
    assert 'credential_ready' in body, (
        'initPluginSourceOptions 没读 credential_ready —— 缺凭据的源在界面上'
        '与配好的源一模一样'
    )
    assert 'dataset.credentialReady' in body, (
        '判据没落到选项的 dataset 上 —— submit 处理器读不到它'
    )
    assert 'source_unconfigured_option' in body, (
        '未配置的源没在下拉里打标记 —— 用户要等到提交才知道'
    )


def test_source_options_refill_is_reentrant():
    """重填必须先清掉旧的插件选项，且不能连内置源那一项一起清。

    只在启动时填一次的话，「填完 token 再回来」这条最正常的路径会被提交前的
    校验拦住（dataset 停在 false）。所以 openDownloadModal 每次都重填 ——
    重填不清旧项就是每开一次弹窗多一份重复选项。
    """
    body = _init_source_options_body()
    assert 'opt.remove()' in body and 'if (opt.value)' in body, (
        '重填没有先清旧选项（或清得不带 value 判断，会把模板里的内置源清掉）'
    )
    modal = _js('map.js')
    modal = modal[modal.index('function openDownloadModal('):]
    modal = modal[:modal.index('\n}\n')]
    assert 'initPluginSourceOptions()' in modal, (
        'openDownloadModal 没有重填数据源下拉 —— 刚在插件面板填好的 token '
        '要刷新整页才认'
    )


def test_submit_blocks_a_source_whose_credential_is_missing():
    """校验必须排在 source_plugin_id 之前，并且是 return 而不是只 toast。

    放行下去的后果不是报错而是**静默的一屏 401**：URL 模板里 `tk={credential}`
    被替换成空串，每块瓦片都失败，而没有任何地方说「你没填 key」。
    """
    body = _download_submit_body()
    guard = body.find('dataset.credentialReady')
    assign = body.find('taskData.source_plugin_id')
    assert guard != -1, 'submit 处理器没有读 dataset.credentialReady —— 校验不存在'
    assert assign != -1, '本测试失效：submit 处理器不再写 source_plugin_id'
    assert guard < assign, (
        '凭据校验排在 source_plugin_id 之后 —— 拦不住，任务照样建出来'
    )
    blocked = body[guard:assign]
    assert 'js.map.download.credential_missing' in blocked, (
        '拦下来了但没告诉用户为什么（也没说去哪填）'
    )
    assert re.search(r'return;', blocked), (
        '只 toast 没 return —— 提示弹出来了，任务也建出来了'
    )


def test_credential_warning_uses_a_toast_type_ui_js_knows():
    """`'error'` 不在 ui.js 的 VALID_TYPES 里，会被静默降级成蓝色 ⓘ。"""
    body = _download_submit_body()
    guard = body.find('dataset.credentialReady')
    blocked = body[guard:body.find('taskData.source_plugin_id')]
    assert "'warning'" in blocked, '缺凭据的提示没用 warning'
    assert "'error'" not in blocked, (
        "用了 'error' —— ui.js 不认这个类型，一条该发黄的警告会变成蓝色提示"
    )


# ------------------------------------------------------------- 真实链路核对

#: `normalizeTask(task, 'plugin')` 从任务对象上读的字段，按来源分两组。
#: 交集（两个来源都必须有）单独列出：`_key` 用 id，进度用计数三件套。
_HISTORY_ROW_KEYS = ('task_type', 'id', 'name', 'status', 'style',
                     'downloaded', 'total', 'gap_tiles', 'gap_decision',
                     'output_path', 'created_at', 'started_at',
                     'total_running_seconds')


def test_history_all_plugin_row_has_every_field_the_frontend_reads(
        isolated_app, tmp_path, monkeypatch):
    """真实链路：造一条插件任务 → GET /api/history_all → 逐字核对字段名。

    这一条测的是源码断言测不到的那一半：字段名在 JS 里写对了，但服务端根本
    不发它。UNION 第五段把 `total_items`/`downloaded_items` 别名成
    `total`/`downloaded`，所以 normalizeTask 的 `task.total_items || task.total`
    在这条路上**只有后半句生效** —— 少写后半句，历史流里的插件行进度恒为 0%，
    而 JS 读不到的字段是静默 undefined，没有任何报错。
    """
    from src.core.database import get_connection

    registry = _install_fake(tmp_path, monkeypatch)
    try:
        client = isolated_app.app.test_client()
        client.post('/api/plugins/fake/enable')
        tid = client.post('/api/plugins/fake/tasks', json={
            'name': '真实链路', 'bbox': [40.0, 30.0, 117.0, 116.0],
            'output_path': str(tmp_path / 'out')}).get_json()['task_id']

        # 计数与缺块写成非零：只测键在不在，测不出别名把值串到了隔壁列。
        conn = get_connection()
        try:
            conn.execute(
                'UPDATE plugin_tasks SET total_items = 7, downloaded_items = 3,'
                ' failed_items = 1, gap_tiles = 2 WHERE id = ?', (tid,))
            conn.commit()
        finally:
            conn.close()

        body = client.get('/api/history_all').get_json()
        rows = [r for r in body['tasks'] if r['task_type'] == 'plugin']
        assert len(rows) == 1, f'时间流里没有插件行：{body["tasks"]}'
        row = rows[0]

        missing = [k for k in _HISTORY_ROW_KEYS if k not in row]
        assert not missing, (
            f'/api/history_all 的插件行缺字段 {missing} —— 前端读它们只会拿到'
            f'静默 undefined。整行：{row}'
        )

        # normalizeTask 的两条 `a || b` 回落各走哪一半，在这条路上是确定的。
        assert 'total_items' not in row and 'downloaded_items' not in row, (
            'UNION 第五段不再别名计数列了 —— 那 normalizeTask 的 `|| task.total`'
            '回落就成了死代码，本测试的前提要重写'
        )
        assert row['total'] == 7 and row['downloaded'] == 3
        assert row['gap_tiles'] == 2
        assert row['style'] == 'fake', 'historyMetaText 的插件 id 取自 style 列'
        assert row['id'] == tid
    finally:
        registry.reset_for_tests()
