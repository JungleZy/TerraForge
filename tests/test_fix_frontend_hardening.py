"""2026-08-08 评审 FrontendJS 分片的修复契约（源码级）。

本项目刻意没有 JS 测试框架（无 package.json/vitest —— 引入会破坏 PyInstaller
的离线打包形态，见 tests/test_map_js_contract.py 头注），前端行为靠源码形态
断言守住。切函数体一律复用 test_css_contract._js_function_body：它按花括号
配对切，与函数在文件里的先后顺序无关。**不要**换回
`src[src.index('function A('):src.index('function B(')]` —— 顺序一调
`end < start`，切片返回空串，本文件所有 `x not in body` 会集体永真
（p2-assertion-review.md 的 E 条）。

每条断言对应的**旧行为**：

1. P1#14 —— `initHistory` 是同步函数，`initHistoryMap(); loadHistory(1);`
   背靠背发出。`initHistoryMap` 在 `await _resolveHistoryBasemap()` 处挂起，
   之后才给 `historyViewer` 赋值；`loadHistory` 结尾的 `renderHistoryMap`
   第一句就是 `if (!historyViewer) return`。独立 /history 页不加载 tasks.js、
   没有面板重开钩子，小地图于是一直空白到用户点 chip / 翻页 / 删除为止。
2. P1#17 —— `showToast(t('js.map.copy.failed'), 'error')`，而 ui.js 的
   VALID_TYPES 里没有 'error'，被**静默**降级成 'info'：剪贴板降级失败
   （局域网 http://IP 这条非安全上下文路径）给出蓝色 ⓘ，读起来像成功。
3. aria-pressed —— 状态 chip / 主题 chip 的选中态只有 CSS class，读屏用户
   听不出当前选的是哪一档。
4. splash —— `initSplash` 注册的 window error 监听是内联匿名函数，无从
   removeEventListener；它闭包持有 splash 子树，而 splashReady 只
   `splash.remove()`，脱离文档的整棵子树被这个常驻监听器钉到页面关闭。
5. `allTasks` —— loadHistory 的响应快照，与 store 双写而只有 store 收 socket
   增量。`renderHistoryMap(allTasks)` 与 task_status.js 的主题重画都读它。
6. 死代码 —— `HISTORY_UNKNOWN_ERROR` 无读者；`typeof basemap !== 'undefined'`
   快路径在每个页面都是死的（`basemap` 是 initMap 的函数参数不是全局）。
7. `openPathBrowser` 不重置 `currentPath`：请求目录失败 + 回退根级也失败时
   `_render` 一次都不跑，「选择此目录」写回上一次会话的路径。
"""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_css_contract import _js_function_body  # noqa: E402
from test_tasks_js_contract import _strip_js_comments  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _js(name):
    with open(os.path.join(ROOT, 'static', 'js', name), encoding='utf-8') as f:
        return f.read()


def _clean(name):
    """剥掉注释的源码。

    本文件每条修复旁边都写了「为什么」的中文注释，而那些注释必然复述被删掉的
    旧写法（'error'、allTasks、typeof basemap …）。拿原文匹配的话，解释性注释
    会被当成回潮，负向断言全红。
    """
    return _strip_js_comments(_js(name))


# --- 1. P1#14：地图与时间流并行启动，在共同屏障后渲染 ---------------------------

def test_init_history_starts_map_and_timeline_before_joint_barrier():
    body = _js_function_body(_clean('history.js'), 'initHistory')
    map_at = body.index('initHistoryMap()')
    history_at = body.index('loadHistory(1, false)')
    barrier_at = body.index('Promise.all')
    assert map_at < barrier_at
    assert history_at < barrier_at
    assert not re.search(r'await\s+initHistoryMap\s*\(', body[:barrier_at]), (
        'initHistoryMap 不能在共同屏障前被 await，否则时间流仍被地图串行阻塞'
    )
    assert 'renderHistoryMap()' in body[barrier_at:]


def test_init_history_map_awaits_tile_origin_before_viewer():
    body = _js_function_body(_clean('history.js'), 'initHistoryMap')
    descriptor_at = body.index('await _resolveHistoryBasemap()')
    probe_at = body.index('await initTileOrigin(bm.tile_port)')
    viewer_at = body.index("new Cesium.Viewer('historyMap'")
    assert descriptor_at < probe_at < viewer_at


def test_map_failure_does_not_block_initial_timeline_load():
    body = _js_function_body(_clean('history.js'), 'initHistory')
    isolated = re.search(
        r'initHistoryMap\s*\(\s*\)\s*\.catch\s*\(\s*function\s*\([^)]*\)'
        r'\s*\{[^}]*\}\s*\)\s*;', body, re.S)
    assert isolated, '失败隔离必须直接挂在 initHistoryMap() 返回的 promise 上'
    history_at = body.index('loadHistory(1, false)', isolated.end())
    assert not re.search(r'\bawait\b', body[isolated.end():history_at]), (
        '地图 catch 与 loadHistory 之间不能 await，否则时间流仍被串行阻塞'
    )
    assert 'Promise.all' in body[history_at:]


def test_post_barrier_map_render_failure_is_swallowed():
    body = _js_function_body(_clean('history.js'), 'initHistory')
    tail = body[body.index('await Promise.all'):]
    protected = re.search(
        r'try\s*\{\s*renderHistoryMap\s*\(\s*\)\s*;?\s*\}'
        r'\s*catch\s*\(\s*\w+\s*\)\s*\{([^}]*)\}', tail, re.S)
    assert protected, '共同屏障后的 renderHistoryMap() 必须由 try/catch 隔离'
    assert 'console.error' in protected.group(1), '最终渲染失败必须记录后再吞掉'


# --- 2. P1#17：无效 toast type ---------------------------------------------------

def test_copy_failure_uses_a_valid_danger_toast():
    """剪贴板降级失败必须用 'danger'，不能用 ui.js 不认识的 'error'。"""
    body = _js_function_body(_clean('map.js'), '_copyText')
    assert "'error'" not in body, (
        "_copyText 里还有 'error' —— ui.js 的 VALID_TYPES 不认它，会静默变蓝色 ⓘ"
    )
    assert re.search(r"showToast\(\s*t\('js\.map\.copy\.failed'\)\s*,\s*'danger'\s*\)", body), (
        "复制失败必须 showToast(t('js.map.copy.failed'), 'danger')"
    )


def test_show_toast_warns_instead_of_silently_downgrading():
    """无效 type 仍降级为 info，但必须 console.warn。

    降级本身不能删（拼错一个 type 不该让提示整个消失），能删的是「悄无声息」：
    正因为静默，'error' 这个错值在源码里活到了评审才被发现。
    """
    body = _js_function_body(_clean('ui.js'), 'showToast')
    assert 'console.warn' in body, 'showToast 无效 type 时必须 console.warn'
    warn_at = body.index('console.warn')
    coerce = re.search(r"type\s*=\s*VALID_TYPES\.indexOf\(type\)\s*!==\s*-1", body)
    assert coerce, 'showToast 的 info 降级不见了 —— 本测试已失效'
    assert warn_at < coerce.start(), '警告必须发生在降级把原值覆盖掉之前'
    assert 'VALID_TYPES' in body[:warn_at], '警告分支必须以 VALID_TYPES 判据触发'


def test_no_js_call_site_passes_an_unknown_toast_type():
    """全站 showToast 的字面量 type 必须都在 VALID_TYPES 里。

    逐个文件手抄一份 grep 会漏；这里从 ui.js 解析出合法集合，再扫全部 JS 的
    调用点 —— 有人再写一个 'error' / 'primary' 会当场红。
    """
    valid = set(re.findall(
        r"'([a-z]+)'", re.search(r'VALID_TYPES\s*=\s*\[([^\]]*)\]', _clean('ui.js')).group(1)))
    assert valid, 'ui.js 里解析不出 VALID_TYPES —— 本测试已失效'
    bad = []
    for name in sorted(os.listdir(os.path.join(ROOT, 'static', 'js'))):
        if not name.endswith('.js'):
            continue
        for used in re.findall(r"showToast\([^;]*?,\s*'([a-z]+)'", _clean(name)):
            if used not in valid:
                bad.append((name, used))
    assert not bad, f'这些 showToast 调用点传了 VALID_TYPES 之外的 type：{bad}'


# --- 3. aria-pressed 与 .active 同步 ---------------------------------------------

def test_status_chips_toggle_aria_pressed():
    """历史状态筛选 chip 切换时必须同时翻 aria-pressed。"""
    body = _js_function_body(_clean('history.js'), 'initHistory')
    assert "setAttribute('aria-pressed', 'false')" in body, (
        '取消选中的 chip 没有把 aria-pressed 置 false'
    )
    assert "setAttribute('aria-pressed', 'true')" in body, (
        '选中的 chip 没有把 aria-pressed 置 true'
    )
    assert body.count("classList.remove('active')") == body.count(
        "setAttribute('aria-pressed', 'false')"), 'class 与 aria-pressed 的清除没有配对'


def test_theme_chips_toggle_aria_pressed():
    """主题分段开关刷新时必须同时写 aria-pressed。"""
    body = _js_function_body(_clean('config.js'), 'initThemeSwitcher')
    assert "classList.toggle('active'" in body, (
        'initThemeSwitcher 不再切换 .active —— 本测试已失效'
    )
    assert re.search(r"setAttribute\('aria-pressed'", body), (
        '主题 chip 只改了 CSS class，读屏用户听不出当前生效的是哪一档'
    )


def test_every_active_toggle_also_writes_aria_pressed():
    """凡是用 .active 表达「已选中」的 JS 切换点，都必须同时写 aria-pressed。

    不点名文件，扫全部 static/js：只钉住已知的两处，下一个新增的 chip 组会
    照样漏掉无障碍属性而测试全绿。判据是同一函数体内出现 aria-pressed。
    """
    missing = []
    for name in sorted(os.listdir(os.path.join(ROOT, 'static', 'js'))):
        if not name.endswith('.js'):
            continue
        src = _clean(name)
        for m in re.finditer(r"classList\.(?:toggle|add|remove)\((['\"])active\1", src):
            # 往回找最近的函数起始，往后到该函数结束 —— 用「上下各 20 行」这种
            # 窗口会在 chip 循环体较长时误判，这里直接取所在语句块。
            start = src.rfind('function', 0, m.start())
            block = src[start:m.start() + 800]
            if 'aria-pressed' not in block:
                missing.append((name, src[:m.start()].count('\n') + 1))
    assert not missing, f"这些 .active 切换点没有同步 aria-pressed：{missing}"


# --- 4. splash 监听器泄漏 --------------------------------------------------------

def test_splash_error_listener_is_named_and_removed():
    """splash 的 window error 监听必须具名注册、由 splashReady 摘掉。

    匿名内联注册没有引用可传给 removeEventListener，监听器活到页面关闭；
    它闭包持有的 splash 子树在 splash.remove() 后已脱离文档却仍被钉住。
    """
    src = _clean('map.js')
    init = _js_function_body(src, 'initSplash')
    ready = _js_function_body(src, 'splashReady')

    assert re.search(r"addEventListener\('error',\s*_onSplashError\)", init), (
        'initSplash 必须用具名 _onSplashError 注册，匿名函数摘不掉'
    )
    assert not re.search(r"addEventListener\('error',\s*function", init), (
        'initSplash 又出现了内联匿名的 error 监听'
    )
    assert re.search(r"removeEventListener\('error',\s*_onSplashError\)", ready), (
        'splashReady 没有摘掉 error 监听 —— 脱离文档的 splash 子树整段泄漏'
    )
    remove_at = ready.index('removeEventListener')
    early_return = re.search(r'if\s*\(!splash\)\s*return;', ready)
    assert early_return, 'splashReady 的 `if (!splash) return` 不见了 —— 本测试已失效'
    assert remove_at < early_return.start(), (
        '摘监听必须排在 `if (!splash) return` 之前，否则那条路径照样泄漏'
    )


# --- 5. allTasks 双写 ------------------------------------------------------------

def test_all_tasks_snapshot_is_gone():
    """`allTasks` 这份与 store 并行的快照必须彻底消失。

    留一个读者就够坏事：socket 增量只写 store，读快照的地方看到的是上一次
    /api/history_all 的颜色与行集合。

    扫的是剥注释后的源码：修复处的 WHY 注释要交代「旧代码读的是 allTasks、
    所以阈值当年才写 <= 1」，那些历史称述不算回潮。
    """
    for name in ('history.js', 'task_status.js'):
        assert not re.search(r'\ballTasks\b', _clean(name)), (
            f'{name} 里还有 allTasks —— 这份快照必须整个删掉'
        )


def test_render_history_map_reads_the_store():
    """renderHistoryMap 自己从 store 取数，不再接收快照入参。"""
    src = _clean('history.js')
    assert re.search(r'function\s+renderHistoryMap\s*\(\s*\)', src), (
        'renderHistoryMap 仍带入参 —— 说明还在被人喂快照'
    )
    body = _js_function_body(src, 'renderHistoryMap')
    assert 'window.TaskStore' in body and 'state.tasks' in body, (
        'renderHistoryMap 没有从 TaskStore.state.tasks 取数据'
    )
    load = _js_function_body(src, 'loadHistory')
    assert re.search(r'renderHistoryMap\s*\(\s*\)', load), (
        'loadHistory 必须无参调用 renderHistoryMap'
    )
    theme = _clean('task_status.js')
    assert re.search(r'renderHistoryMap\s*\(\s*\)', theme), (
        '主题切换重画必须无参调用 renderHistoryMap'
    )


def test_delete_task_page_rollback_reads_the_store():
    """删除后的「本页已空就回退一页」判据必须读 store 的当前长度。

    旧代码读 allTasks（loadHistory 的快照，remove 不改它）所以写 `<= 1`；
    改读 store 后 remove 已经生效，阈值必须是 0，否则每删一条都会回退一页。
    """
    body = _js_function_body(_clean('history.js'), 'deleteTask')
    m = re.search(r'(\w+)\s*===\s*0\s*&&\s*currentPage\s*>\s*1', body)
    assert m, '回退判据不再是「删完为空 && 不在第一页」—— 本测试已失效'
    assert re.search(re.escape(m.group(1)) + r'\s*=\s*store\s*\?\s*store\.state\.tasks\.length',
                     body), '回退判据的长度必须取自 store.state.tasks'


# --- 6. 死代码 -------------------------------------------------------------------

def test_unused_history_unknown_error_constant_is_gone():
    """HISTORY_UNKNOWN_ERROR 及其 4 行注释必须删干净。

    它零读者，注释却在为一条不再保护任何东西的命名约束辩护（真正的读者是
    task_list.js 的组件，它直接查 i18n key）。
    """
    assert 'HISTORY_UNKNOWN_ERROR' not in _js('history.js')
    assert "this.t('js.history.unknown_error')" in _js('task_list.js'), (
        'i18n key 的真实读者不见了 —— 说明删过头了'
    )


def test_dead_inline_basemap_fast_path_is_gone():
    """`typeof basemap !== 'undefined'` 这条永远为假的快路径必须删掉。

    唯一的生产者 index.html 把描述符当**实参**传给 initMap(config, basemap)，
    函数参数不是全局；这条分支从来没命中过，却多出一句「首页已内联下发」的
    假注释，还让人以为省掉了那次 /api/basemap 往返。
    """
    src = _clean('history.js')   # 剥注释：删除记录本身会复述这条旧写法
    assert 'typeof basemap' not in src, (
        'history.js 里还有 typeof basemap 快路径 —— 它在每个页面都是死的'
    )
    body = _js_function_body(_clean('history.js'), '_resolveHistoryBasemap')
    assert "fetch('/api/basemap'" in body, (
        '_resolveHistoryBasemap 必须走 /api/basemap —— 本测试已失效'
    )
    assert 'HISTORY_BASEMAP_FALLBACK' in body, (
        '接口失败时必须回退到同源路径，不能绕过 /basemap 代理'
    )


# --- 7. path_browser 陈旧 currentPath --------------------------------------------

def test_open_path_browser_resets_current_path():
    """打开弹窗必须先清掉上一次会话的 currentPath。

    请求的目录失败、回退根级也失败时 `_render` 一次都不跑，弹窗顶着错误横幅
    显示上一次浏览的目录，「选择此目录」把那个陈旧路径写回输入框 —— 与
    `_reqSeq` 守卫防的是同一类「写回用户没在看的值」。
    """
    body = _js_function_body(_clean('path_browser.js'), 'openPathBrowser')
    m = re.search(r'currentPath\s*=\s*null', body)
    assert m, 'openPathBrowser 没有重置 currentPath'
    load_at = re.search(r'load\(targetInput\.value', body)
    assert load_at, 'openPathBrowser 不再触发 load() —— 本测试已失效'
    assert m.start() < load_at.start(), '重置必须发生在发出 load() 之前'



# --- 8. 面板焦点管理（P1#16 的 JS 半边） -------------------------------------
# 模板半边（role="dialog" / aria-modal / tabindex="-1" / [data-panel-close]）由
# tests/test_fix_templates_a11y.py 钉，这里只钉 panels.js 该做的三件事。

def test_open_panel_pulls_focus_into_the_panel():
    """openPanel 必须把焦点送进面板，落点是关闭钮。

    旧行为：面板只是 `hidden = false`，焦点原地不动 —— 面板行为上是模态
    （遮罩 + Esc 关闭），焦点却还在被遮罩盖住的那半个界面上。
    """
    body = _js_function_body(_clean('panels.js'), 'openPanel')
    assert "querySelector('[data-panel-close]')" in body, (
        'openPanel 没有去找 [data-panel-close] —— 焦点落点是模板约定的关闭钮'
    )
    assert '.focus()' in body, 'openPanel 没有把焦点移进面板'


def test_close_panel_returns_focus_to_the_opener():
    """closePanel 必须把焦点还给打开面板的控件，且只在焦点确实在面板里时还。

    无条件还原会在面板互切时把刚点下的触发钮的焦点抢走再塞进新面板；
    不还原则焦点停在即将 hidden 的子树上，浏览器把它甩回 <body>，键盘用户
    要从头 Tab 一遍。
    """
    src = _clean('panels.js')
    body = _js_function_body(src, 'closePanel')
    assert 'el.contains(document.activeElement)' in body, (
        'closePanel 的焦点归还没有「焦点确实在面板里」这道判据'
    )
    assert 'restoreFocus.focus()' in body, 'closePanel 没有把焦点还回去'
    opener = _js_function_body(src, 'openPanel')
    assert 'document.activeElement' in opener, 'openPanel 没有记住打开前的焦点元素'
    assert opener.index('document.activeElement') < opener.index('closePanel(true)'), (
        '记焦点必须早于 closePanel(true)：互切时它会把焦点交还出去'
    )


def test_panel_traps_tab_but_yields_to_the_confirm_overlay():
    """Tab 必须在面板内成环，但自定义确认框开着时让位。

    旧行为：onKey 只认 Escape，Tab 照样走到被遮罩盖住、看不见却仍可聚焦的
    控件上。而焦点环不能做成「给面板之外的兄弟节点批量 inert」—— showToast /
    showConfirm 的浮层是运行时 append 到 document.body 的，会一起被冻住。
    """
    body = _js_function_body(_clean('panels.js'), 'onKey')
    assert "'Tab'" in body, 'onKey 不处理 Tab —— 焦点环不存在'
    assert 'preventDefault' in body, '焦点环必须拦下浏览器默认的 Tab 移动'
    assert 'app-confirm-overlay' in body, (
        '确认框开着时焦点环必须让位，否则从面板里弹出的 confirm 按不了'
    )
    assert 'inert' not in _clean('panels.js'), (
        'panels.js 不该给面板外的节点上 inert —— 会连带冻住 body 上的 toast/confirm'
    )


def test_panel_key_handler_yields_to_a_bootstrap_modal_before_it_acts():
    """面板之上开着 Bootstrap 弹窗时，onKey 必须整个让位 —— Esc 与 Tab 都是。

    让位判据以前排在 Escape 分支【下面】，于是它只管 Tab：从配置面板里点
    「浏览」开出 #pathBrowserModal，按一次 Esc，Bootstrap 在目标阶段 hide 掉
    弹窗，事件继续冒泡到 document，这里再把身后的面板一起关掉。实测（无头
    Chromium 驱动真实服务端）：modalOpen ["pathBrowserModal"]→[]、panelOpen
    ["configPanel"]→[] —— 一次 Esc 两层全没，hash 被 replaceState 抹掉、焦点
    掉回 body。

    判据必须是 body.modal-open：Bootstrap 5.3.0 的 hide() **同步**摘掉
    .show（`this._element.classList.remove(Li)`）再排队做过渡收尾，事件冒泡到
    document 时 `.modal.show` 已经不匹配 —— 实测拿它当判据面板照样被关掉。
    """
    src = _clean('panels.js')
    body = _js_function_body(src, 'onKey')
    assert 'modal-open' in body, (
        'onKey 没有为 Bootstrap 弹窗让位 —— 一次 Esc 会把身后的面板一起关掉'
    )
    escape_at = body.index("'Escape'")
    yield_at = body.index('modal-open')
    confirm_at = body.index('app-confirm-overlay')
    assert yield_at < escape_at and confirm_at < escape_at, (
        '让位判据必须排在 Escape 分支之前，否则它只管 Tab —— 这正是原缺陷'
    )
    assert '.modal.show' not in body, (
        'hide() 同步摘掉 .show，冒泡到 document 时它已经不匹配；判据要用 body.modal-open'
    )


# --- 9. 内联 onclick 收口 ---------------------------------------------------------

def _strip_jinja_comments(html):
    """剥掉 `{# ... #}`。

    删除记录本身会复述 `onclick="resetConfig()"`，拿原文扫会把说明当成回潮。
    """
    return re.sub(r'\{#.*?#\}', '', html, flags=re.S)


def _html(*parts):
    with open(os.path.join(ROOT, 'templates', *parts), encoding='utf-8') as f:
        return f.read()


def test_no_inline_onclick_left_in_templates():
    """模板里不许再有 onclick= 属性。

    内联处理器逼着被调函数必须是全局的，并且与 CSP 的 unsafe-inline 绑死；
    接线全部收到 JS 侧的 addEventListener。
    """
    offenders = []
    for name in sorted(os.listdir(os.path.join(ROOT, 'templates'))):
        if not name.endswith('.html'):
            continue
        for m in re.finditer(r'\bonclick\s*=', _strip_jinja_comments(_html(name))):
            offenders.append((name, m.start()))
    assert not offenders, f'这些模板还留着内联 onclick：{offenders}'


def test_browse_buttons_are_wired_by_data_path_target():
    """两个「浏览」按钮靠 data-path-target 接线，JS 侧只有一处委托监听。

    JS 与模板必须成对：模板留着 onclick 又加了监听 = 点一次弹两次；
    模板删了 onclick 而 JS 没接 = 按钮变哑。
    """
    for tpl, target in (('index.html', 'outputPath'),
                        ('_config_content.html', 'default_save_path')):
        assert f'data-path-target="{target}"' in _html(tpl), (
            f'{tpl} 的浏览按钮少了 data-path-target 钩子'
        )
    pb = _clean('path_browser.js')
    assert "closest('[data-path-target]')" in pb, (
        'path_browser.js 没有按 data-path-target 接线 —— 模板的 onclick 已经删了'
    )
    assert "getAttribute('data-path-target')" in pb, (
        '目标输入框 id 必须从 data-path-target 读，不能在 JS 里再抄一份'
    )


def test_reset_button_is_wired_in_init_config():
    """「恢复默认」按钮由 initConfig 接线，不再靠内联 onclick。"""
    assert 'id="configResetBtn"' in _html('_config_content.html'), (
        '重置按钮少了 id 钩子 —— 本测试已失效'
    )
    body = _js_function_body(_clean('config.js'), 'initConfig')
    assert "getElementById('configResetBtn')" in body, 'initConfig 没有找重置按钮'
    assert re.search(r"addEventListener\('click',\s*resetConfig\)", body), (
        'initConfig 没有把 resetConfig 绑上去 —— 模板的 onclick 已经删了，按钮会变哑'
    )