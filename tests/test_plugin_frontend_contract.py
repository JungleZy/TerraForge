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

#: 两条下载管线的装配体 —— `async function submitDownload(downloadType)` 的函数体。
#:
#: 2026-08-15：这段装配原来整体写在 `#downloadForm` 的**匿名** submit 监听里，
#: 所以旧锚点按「getElementById('downloadForm') 之后 addEventListener('submit'」
#: 切。两张表单合并成一张 #taskForm 之后，唯一的 submit 监听收成一个按
#: _currentPipeline() 分派的三行调度器，装配整体搬进了具名的 submitDownload。
#: 下面几条断言守的是插件源的凭据闸门与样式取值 —— 那两段都在 submitDownload
#: 里，一字未动，所以锚点跟着装配搬。
def _task_submit_body():
    return _fn_body(_js('map.js'), 'async function submitDownload(downloadType)')


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
    校验拦住（dataset 停在 false）。所以 openCreatePanel 每次都重填 ——
    重填不清旧项就是每开一次面板多一份重复选项。

    2026-08-15：入口从 openDownloadModal 换成
    `async function openCreatePanel(pipeline, prefill)`（两个弹窗合成一个非模态
    面板）。重填仍是开面板那一刻同步发起的第一件事，所以「每次开都刷新」这条
    契约一字未变。
    """
    body = _init_source_options_body()
    assert 'opt.remove()' in body and 'if (opt.value)' in body, (
        '重填没有先清旧选项（或清得不带 value 判断，会把模板里的内置源清掉）'
    )
    panel = _js('map.js')
    panel = panel[panel.index('function openCreatePanel('):]
    panel = panel[:panel.index('\n}\n')]
    assert 'initPluginSourceOptions()' in panel, (
        'openCreatePanel 没有重填数据源下拉 —— 刚在插件面板填好的 token '
        '要刷新整页才认'
    )


def test_submit_blocks_a_source_whose_credential_is_missing():
    """校验必须排在 source_plugin_id 之前，并且是 return 而不是只 toast。

    放行下去的后果不是报错而是**静默的一屏 401**：URL 模板里 `tk={credential}`
    被替换成空串，每块瓦片都失败，而没有任何地方说「你没填 key」。
    """
    body = _task_submit_body()
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
    body = _task_submit_body()
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


#: 顶层函数体。`_init_source_options_body` 是同一手法的手写特例（它要跳 async
#: 关键字），`_task_submit_body` 2026-08-15 起直接转调本函数（旧的
#: `_download_submit_body` 切的是匿名 submit 监听，那个监听已经不装装配了）；
#: 这里参数化，因为下面要按名字取三个不同的函数。切到第一个行首的 `}` 为止 ——
#: 本仓 JS 全是 4 空格缩进，顶层函数的闭合花括号是唯一顶格的那一行。
def _fn_body(src, header):
    assert header in src, f'源码里没有 {header}'
    body = src[src.index(header):]
    return body[:body.index('\n}\n')]


# --------------------------------------------- 插件源 × 样式下拉（源码级契约）

def test_style_select_is_locked_while_a_plugin_source_is_selected():
    """选了插件源，样式下拉必须置灰 —— 它对取哪张瓦片毫无影响
    （download_engine.get_tile_url 带快照时走 source.url_template）。
    留着可拨等于让用户白拨。
    """
    src = _js('map.js')
    body = _fn_body(src, 'function initPluginSourceStyleLock()')
    assert "getElementById('downloadPluginSource')" in body
    assert "getElementById('mapStyle')" in body
    assert 'style.disabled = locked' in body, '没有真的禁用样式下拉'
    assert "addEventListener('change'" in body, '没挂 change 监听'


def test_style_lock_shows_a_reason_and_hides_the_misleading_thumbnail():
    """置灰不给理由，用户只知道点不动、不知道为什么。缩略图同样要收起来：
    那五张是内置源的样例瓦片，插件源下它展示的是一张无关的图。
    """
    body = _fn_body(_js('map.js'), 'function initPluginSourceStyleLock()')
    assert 'hint.hidden = !locked' in body
    assert 'preview.hidden = locked' in body

    html = (ROOT / 'templates/index.html').read_text(encoding='utf-8')
    assert 'id="mapStyleLockHint"' in html, '模板里没有说明容器'
    assert "tpl.index.download.style_locked_hint" in html
    assert "_boot('pluginSourceStyleLock', initPluginSourceStyleLock)" in html, (
        '函数写了但没人调 —— 联动永远不会生效'
    )


def test_style_lock_hint_element_can_actually_be_hidden():
    """`hidden` 是本仓切换可见性的唯一手法（mapStyleField / demOptions / swbWrap
    都是它），而这个提示是全文件里**第一个条件显示的 form-text** —— 同目录其它
    提示都常显，照抄它们的 `<small class="... d-block">` 会把这一句变成常显：

      - `.d-block{display:block!important}` 压过 `[hidden]` 的 UA 规则。实测
        hidden 属性照样设、computed display 仍是 block、高度 18px，选内置源时
        这句话也在那儿 —— 一条永远为真的「不适用」说明比不写更糟。
      - 去掉 d-block 换回 `<small>` 也不行：它是 inline，紧跟的
        `<label class="form-label">` 在 Bootstrap 里同样没有 display，实测两者
        挤在同一行（提示 top 与「数据源」标签 top 都是 623）。

    所以元素必须是天生块级的标签、且不带 d-block。两个坑都踩过，钉住。
    """
    html = (ROOT / 'templates/index.html').read_text(encoding='utf-8')
    line = next(ln for ln in html.splitlines() if 'id="mapStyleLockHint"' in ln)
    assert 'd-block' not in line, (
        'd-block 的 !important 压过 [hidden] —— 这句提示会常显'
    )
    assert line.strip().startswith('<div'), (
        f'提示容器必须是块级标签（inline 的 <small> 会与「数据源」标签同行）：{line.strip()}'
    )


def test_submit_reads_the_style_value_off_the_element_not_the_form():
    """禁用之所以安全，全靠提交体是**手拼**的：`disabled` 只挡原生表单提交与
    用户交互，`select.value` 照样读得到最后一次选中的码。

    这一条钉的是那个前提。哪天有人把 style 改成从 FormData / form.elements 取，
    禁用态下它就会缺席 → 请求体里 `style` 变 undefined → 后端
    `MapStyle.from_shorthand` 抛 ValueError 打成 400，而界面上只显示一句
    「创建失败」，没有任何线索指回这个下拉。

    只看 submitDownload 的函数体（改前是 #downloadForm 的 submit 体）：区域导入
    那条路确实用 FormData 传文件，它与这个下拉无关。
    """
    body = _task_submit_body()
    assert "style: document.getElementById('mapStyle').value" in body
    assert 'FormData' not in body, (
        '下载表单开始用 FormData 了 —— 禁用的样式下拉会静默缺席'
    )


def test_map_source_text_does_not_trust_the_style_column():
    """历史里那句谎话的修法：地图行的来源文案读 source_id，不是无条件读 style。

    插件源任务的 style 列存的是提交那一刻下拉的值（后端只用它算了个缓存前缀），
    于是天地图任务显示成「路线图」。
    """
    src = _js('history.js')
    body = _fn_body(src, 'function mapSourceText(task)')
    assert 'task.source_id' in body
    assert "'plugin:'" in body
    assert "t('js.history.style.plugin_source')" in body
    # 内置源与存量行（source_id 空）必须一字不变地走原路。
    assert 'getStyleText(task.style)' in body
    # 调用点接上了才算生效。
    meta = _fn_body(src, 'function historyMetaText(task)')
    assert 'mapSourceText(task)' in meta


# ------------------------------------------------------ 真实链路：source_id

def _create_map_task(client, tmp_path, **extra):
    payload = {
        'name': 'B-债', 'north': 40.0, 'south': 39.0,
        'east': 117.0, 'west': 116.0, 'zoom_min': 3, 'zoom_max': 3,
        'style': 'm', 'output_format': 'tiles_only',
        'output_path': str(tmp_path / 'out'),
    }
    payload.update(extra)
    resp = client.post('/api/tasks', json=payload)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()['task_id']


def test_history_all_map_row_carries_the_real_source_not_just_the_style(
        isolated_app, tmp_path):
    """真实链路：内置源任务 + 插件源任务各一条 → GET /api/history_all。

    插件源那条的样式下拉根本没动过，于是它的 `style` 列存着 'm' 归一后的
    'roadmap' —— 与一条真正的内置路线图任务**逐字相同**。style 一列因此
    区分不出它们，这正是历史里把天地图任务显示成「路线图」的全部原因。
    区分只能靠 source_id。
    """
    from src.plugins import registry

    client = isolated_app.app.test_client()
    try:
        assert client.post('/api/plugins/tianditu/enable').status_code == 200
        builtin_id = _create_map_task(client, tmp_path, style='s')
        plugin_id = _create_map_task(
            client, tmp_path, source_plugin_id='tianditu', source_id='img')

        rows = {r['id']: r for r in client.get('/api/history_all').get_json()
                ['tasks'] if r['task_type'] == 'map'}
        assert set(rows) >= {builtin_id, plugin_id}

        # 建任务时 MapStyle.from_shorthand 把 'm' 归一成 'roadmap' 才落库。
        assert rows[plugin_id]['style'] == 'roadmap', (
            '前提变了：插件源任务的 style 列不再是下拉那个值，本测试要重写'
        )
        assert rows[builtin_id]['style'] == 'satellite'
        assert rows[builtin_id]['source_id'] == 'satellite'
        assert rows[plugin_id]['source_id'] == 'plugin:tianditu:img'

        # 快照原文不外发：它还含 url_template 与 credential_reference 键名，
        # 渲染一行元信息用不着，每条历史行都胖一圈。
        assert 'source_snapshot' not in rows[plugin_id]
    finally:
        registry.reset_for_tests()


def test_history_all_other_pipelines_keep_the_column_aligned(isolated_app,
                                                             tmp_path):
    """UNION 加一列必须五段逐位对齐 —— 少一段直接是 SQL 错、整个时间流 500。

    四条非 map 管线没有源快照，那一列是 NULL，前端拿到空串走原路。
    """
    from src.core.database import get_connection

    client = isolated_app.app.test_client()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO dem_tasks (name, status, north, south, east, west,"
            " dataset, output_path) VALUES ('d','pending',40,39,117,116,"
            "'COP-DEM-GLO-30','/tmp/d')")
        conn.execute(
            "INSERT INTO local_terrain_tasks (name, status, output_path,"
            " source_dir, output_dir, maxzoom) VALUES"
            " ('l','pending','/tmp/l','/tmp/src','/tmp/out',12)")
        conn.execute(
            "INSERT INTO contour_tasks (name, status, north, south, east, west,"
            " contour_interval, zoom_min, zoom_max, output_path) VALUES"
            " ('c','pending',40,39,117,116,50,3,4,'/tmp/c')")
        conn.commit()
    finally:
        conn.close()

    body = client.get('/api/history_all')
    assert body.status_code == 200, body.get_data(as_text=True)
    rows = body.get_json()['tasks']
    types = {r['task_type'] for r in rows}
    assert {'dem', 'local_terrain', 'contour'} <= types
    for r in rows:
        assert r['source_id'] == '', f'{r["task_type"]} 行的 source_id 不是空串'


def test_active_map_task_carries_source_id_too(isolated_app, tmp_path):
    """两条路给前端的键名必须一致：新建的任务先经 /api/tasks（Task.to_dict）
    进时间流，刷新页面后才换成 /api/history_all。少一边，同一条任务在刷新
    前后显示两种来源。
    """
    from src.plugins import registry

    client = isolated_app.app.test_client()
    try:
        client.post('/api/plugins/tianditu/enable')
        tid = _create_map_task(
            client, tmp_path, source_plugin_id='tianditu', source_id='cia')
        rows = {r['id']: r for r in
                client.get('/api/tasks?status=active').get_json()['tasks']}
        assert rows[tid]['source_id'] == 'plugin:tianditu:cia'
    finally:
        registry.reset_for_tests()
