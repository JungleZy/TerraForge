"""提交守卫（in-flight guard）与静默 catch 的定策 —— 前端源码级契约。

守的是两件互相独立、但根子相同的事：**动作发出去之后，界面必须说话**。

1. 11 处 POST / DELETE 的 in-flight 守卫（2026-08-09 评审 P3#675）。
   改前只有 map.js 的下载提交那一处写对了（存原文案 → disabled → 换 spinner
   → finally 复原），另外 11 处零守卫：「开始」连点三次就是三发 start，删除
   连点三次就是三发 DELETE（后两发撞 404 再弹两条红字，用户以为自己删错了
   东西），导出连点三次让后端把同一个 MBTiles 重打三遍。现在那一份提成了
   `window.guard(triggerEl, asyncFn)`，11 处逐个接上。

2. static/js 下每一个 `catch` 块都必须有明确归属：
     A 用户可见 —— 调反馈原语（showToast / showConfirm / 行内错误文本），
                   或者错误经 store 落到组件上（那种带 `// 用户可见：`）；
     B 仅日志   —— `// 仅日志：`；
     C 明确忽略 —— `// 明确忽略：`。
   改前 46 处静默、8 处只有 console —— 「点了没反应」的一半来源在这里。
   分档是**可 grep 的**：`grep -rn '仅日志：' static/js` 与
   `grep -rn '明确忽略：' static/js` 出来的每一行都在某个 catch 体里，
   本文件的 test_class_markers_are_greppable 双向钉住这一条。

⚠️ 不许给任务加「重试」按钮（两次被否：三个 manager 的 start_task 只收
pending/paused；失败任务的设计是删掉重建）。列表加载失败那颗「重新加载列表」
重发的是 /api/history_all 那一次 GET，与任务状态机无关 —— 键名与文案都刻意
不含 retry / 重试，test_the_reload_key_cannot_be_mistaken_for_a_task_retry
钉住它。
"""
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.i18n.catalog import MESSAGES  # noqa: E402
from test_tasks_js_contract import _strip_js_comments  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _js(name):
    with open(os.path.join(JS_DIR, name), encoding='utf-8') as f:
        return f.read()


def _brace_body(src, open_at):
    """从 `open_at` 处那个 `{` 起按花括号配对，返回大括号之间的内容。"""
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[open_at + 1:i]
    raise AssertionError('花括号没有配平 —— 源码坏了或本测试已失效')


def _fn_body(src, name):
    """切出函数体。`function f(...)` 与对象方法 `f(...) {` 两种形态都认。

    对象方法那种是给 task_list.js 的 Vue 组件用的：`act` 是 methods 里的
    方法，不是顶层函数，而它恰恰是行上那几颗动作按钮唯一的上锁点。
    """
    pattern = re.compile(r'(?:function\s+)?\b' + re.escape(name) + r'\s*\([^()]*\)\s*\{')
    matches = list(pattern.finditer(src))
    assert matches, f'源码里找不到 {name}(...) 的定义 —— 本测试已失效'
    assert len(matches) == 1, (
        f'{name} 匹配到 {len(matches)} 处定义，切不准 —— 本测试已失效'
    )
    return _brace_body(src, matches[0].end() - 1)


# ---------------------------------------------------------------------------
# 1. guard 本体
# ---------------------------------------------------------------------------

def test_guard_locks_swaps_and_always_restores():
    """`guard` 必须齐三件事：上锁、换 spinner、finally 复原。

    少哪一件都会退化成一个更糟的形态：
      - 不上锁 = 白写；
      - 不换 spinner = 按钮变灰但不说在干什么（慢请求下与「坏了」无法区分）；
      - 不在 finally 里复原 = 请求一失败，按钮永久卡在禁用态，只能刷新页面。
    """
    src = _strip_js_comments(_js('ui.js'))
    body = _fn_body(src, 'guard')
    assert re.search(r'disabled\s*=\s*true', body), 'guard 没有把触发钮禁掉'
    assert 'finally' in body, 'guard 没有 finally —— 失败时按钮会永久卡在禁用态'
    assert re.search(r'originalHtml\s*=\s*\w+\.innerHTML', body), 'guard 没有存下原文案'
    restore = body[body.index('finally'):]
    assert re.search(r'innerHTML\s*=\s*originalHtml', restore), (
        'finally 里没有把原文案换回来'
    )
    assert re.search(r'disabled\s*=\s*originalDisabled', restore), (
        'finally 里没有恢复原来的 disabled —— 本来就禁着的按钮会被解禁'
    )
    assert 'GUARD_SPINNER' in body and 'hint-spin' in src, (
        "guard 没有换 spinner。注意别抄 map.js 那句 `animation: spin` —— "
        '全仓没有 @keyframes spin，那个圈一动不动；.hint-spin 才是真在转的那条'
    )
    assert re.search(r'window\.guard\s*=\s*guard', src), 'guard 没有挂到全局'


def test_guard_degrades_instead_of_dropping_the_action_without_a_button():
    """拿不到触发元素时必须照常执行，不能悄悄不做事。

    键盘触发、事件代理够不到按钮的调用点会传 null 进来。那种情况下退化成
    「没有视觉锁的直接调用」是对的；退化成「什么都不发生」就是把一个动作
    静默吃掉 —— 与本任务要修的病症一模一样。
    """
    body = _fn_body(_strip_js_comments(_js('ui.js')), 'guard')
    head = body[:body.index('dataset')]
    assert re.search(r'if\s*\(\s*!\s*\w+\s*\)\s*return\s+\w+\(\s*\)', head), (
        'guard 在 triggerEl 为空时没有直接执行 asyncFn'
    )


# ---------------------------------------------------------------------------
# 2. 11 处动作
# ---------------------------------------------------------------------------

#: 动作函数 -> 它所在的文件。**11 条，逐个按名字钉**。
#:
#: 这张表就是「哪些动作必须防连点」的清单。名字对不上（被重命名/删掉）时下面
#: 的断言会报「找不到定义」，而不是静默变绿；新写一个 POST/DELETE 动作的人
#: 要顺手加一行 —— 表长在下一条断言里锁着，加行时会被迫看见那个数字。
_GUARDED_ACTIONS = {
    'startTask': 'task_center.js',
    'pauseTask': 'task_center.js',
    'resumeTask': 'task_center.js',
    'refillTask': 'task_center.js',
    'acceptTaskGaps': 'task_center.js',
    'exportTask': 'task_center.js',
    'saveConfig': 'config.js',
    'resetConfig': 'config.js',
    'clearCacheCategory': 'config.js',
    'deleteTask': 'history.js',
    'act': 'task_list.js',
}


def test_the_action_registry_still_has_eleven_entries():
    """节点数 = 11。改这个数字必须是**有意**的一次动作。"""
    assert len(_GUARDED_ACTIONS) == 11, (
        f'守卫清单现在有 {len(_GUARDED_ACTIONS)} 条。加动作请连同这个数字一起改，'
        '并说明新动作为什么需要（或不需要）守卫'
    )


def test_every_guarded_action_routes_through_guard():
    """11 个动作函数体里都必须出现 `guard(`。"""
    problems = []
    for name, js_name in sorted(_GUARDED_ACTIONS.items()):
        body = _fn_body(_strip_js_comments(_js(js_name)), name)
        if 'guard(' not in body:
            problems.append(f'{js_name}:{name} 的函数体里没有 guard( —— 可以被连点')
    assert not problems, '以下动作没有 in-flight 守卫：\n' + '\n'.join(
        '  ' + p for p in problems)


def test_export_no_longer_hand_rolls_its_own_lock():
    """导出那处手写的 disabled 一对必须让位给 guard —— 一个概念一份实现。

    留着的话它就是第二份守卫：换 spinner 的是 guard，解禁的是手写那半，
    两边对同一颗按钮各写各的，下一个人改哪边都对不上。
    """
    body = _fn_body(_strip_js_comments(_js('task_center.js')), 'exportTask')
    assert not re.search(r'button\.disabled\s*=', body), (
        'exportTask 里还有手写的 button.disabled —— 上锁只许有 guard 一份'
    )


# ---------------------------------------------------------------------------
# 3. 行上的动作按钮：触发元素必须真的传得到
# ---------------------------------------------------------------------------

def test_row_actions_hand_the_trigger_button_to_the_dispatcher():
    """模板里每一处动作调用都要带 `$event`，否则守卫拿到的是 null。

    这是本任务最容易静默退化的一处：不带 `$event` 时 `guard(null, fn)` 照常
    把动作发出去，功能看着完全正常，只是锁没了 —— 没有任何报错。
    """
    src = _js('task_list.js')
    calls = re.findall(r"act\('(\w+)'([^)]*)\)", src)
    assert calls, 'task_list.js 里一处 act(...) 都没有 —— 本测试已失效'
    problems = []
    for name, rest in calls:
        if '$event' not in rest and 'event' not in rest:
            problems.append(f"act('{name}') 没有把触发钮传下去")
    for method in ('remove', 'refill', 'acceptGaps', 'reloadList'):
        if not re.search(r'@click="' + method + r'\(\$event\)"', src):
            problems.append(f'@click="{method}($event)" 不见了 —— 那颗按钮锁不上')
    assert not problems, '行动作没有把触发钮交给守卫：\n' + '\n'.join(
        '  ' + p for p in problems)


def test_dispatcher_locks_the_button_and_does_not_nest_a_second_guard():
    """`act` 自己上锁，并且**不**把按钮再转发下去。

    转发下去就是同一颗按钮上套两层守卫：里层看见 dataset.guardBusy 已经是 1，
    直接返回 undefined —— 动作一次都发不出去，而界面上只是「点了没反应」。
    """
    body = _fn_body(_strip_js_comments(_js('task_list.js')), 'act')
    assert 'currentTarget' in body, (
        'act 没有用 $event.currentTarget 取按钮（target 会是按钮里的那个 svg）'
    )
    assert 'guard(' in body, 'act 没有上锁'
    forwarded = re.search(r'fn\(([^)]*)\)', body)
    assert forwarded, 'act 里找不到转发调用 —— 本测试已失效'
    args = [a.strip() for a in forwarded.group(1).split(',')]
    assert len(args) == 2, (
        f'act 转发了 {len(args)} 个实参 {args} —— 第三个（触发钮）会让动作函数'
        '在同一颗按钮上再套一层守卫，结果是一次都不发'
    )


# ---------------------------------------------------------------------------
# 4. catch 定策
# ---------------------------------------------------------------------------

#: A 档的判据：这些调用出现在 catch 体里，就说明用户看得见这次失败。
#: `.textContent =` / `.innerHTML =` 是「行内错误文本」那一类（估算读数、
#: 目录浏览的错误条、产物清单…），它们不走 toast。
_FEEDBACK_CALLS = (
    'showToast(', 'showNotification(', 'showConfirm(', 'showProgressDialog(',
    'setLoadError(', 'toast(', 'showConfigErrors(', '_tifInfoMessage(',
    '_renderPlaceSearchHint(', '_renderWizardMessage(', '_renderTaskLogNote(',
    'setProxyIcon(', '.textContent =', '.innerHTML =',
)

#: 三档的标记。`//` 与 `/* */` 两种注释都认：一行就能说清的 catch
#: （`catch (e) { /* 明确忽略：元素可能已不在文档里 */ }`）不该被逼成五行。
_MARKER_RE = re.compile(r'(?://|/\*)\s*(用户可见|仅日志|明确忽略)：')


def _catch_blocks(src):
    """(行号, catch 体) 列表。只认语句形态的 `} catch (e) { ... }`。

    判据是 catch 前面那个非空字符必须是 `}`（try 的收尾）。这样
    `promise.catch(function () { ... })` 这类**回调**不会被算进来 —— 它是
    Promise 链的一环，不是异常处理块，与 `.catch(() => ({}))` 那种写法混在
    一张表里只会让归档标准变得说不清（同一个语义，一个被扫到、一个扫不到）。
    """
    out = []
    for m in re.finditer(r'\bcatch\b\s*(?:\([^)]*\))?\s*\{', src):
        before = src[:m.start()].rstrip()
        if not before.endswith('}'):
            continue
        open_at = src.index('{', m.end() - 1)
        out.append((src.count('\n', 0, m.start()) + 1, _brace_body(src, open_at)))
    return out


def _all_catches():
    for fn in sorted(os.listdir(JS_DIR)):
        if not fn.endswith('.js'):
            continue
        src = _js(fn)          # ⚠️ 不剥注释：标记就在注释里
        for line, body in _catch_blocks(src):
            yield fn, line, body


def test_the_scanner_actually_sees_the_catch_blocks():
    """自检：扫不到东西的话下面两条负向断言就是永真。"""
    found = list(_all_catches())
    assert len(found) > 40, f'只扫到 {len(found)} 个 catch 块 —— 本测试已失效'
    files = {fn for fn, _line, _body in found}
    assert {'map.js', 'history.js', 'task_center.js', 'config.js'} <= files, (
        f'几个大文件一个 catch 都没扫到（实际 {sorted(files)}）—— 本测试已失效'
    )


def test_every_catch_block_is_classified():
    """静默 catch 清零：每一个 catch 要么让用户看得见，要么写明为什么不。"""
    unclassified = []
    for fn, line, body in _all_catches():
        if _MARKER_RE.search(body):
            continue
        if any(call in body for call in _FEEDBACK_CALLS):
            continue
        first = next((l.strip() for l in body.strip().split('\n') if l.strip()), '(空块)')
        unclassified.append(f'{fn}:{line} {first[:70]}')
    assert not unclassified, (
        '以下 catch 块既不给用户任何反馈，也没有说明为什么可以不给。\n'
        '补一句反馈，或者在**块内**加一行注释：\n'
        '  // 仅日志：<为什么控制台足够>\n'
        '  // 明确忽略：<为什么这次失败无关紧要>\n'
        '（错误经 store 落到组件上的那种写 `// 用户可见：<渲染在哪>`）\n'
        + '\n'.join('  ' + u for u in unclassified)
    )


def test_class_markers_are_greppable():
    """标记必须**只**出现在 catch 体里，grep 出来的就是那份档案。

    反向也要成立：一个写在别处的「仅日志：」会让 grep 的结果多出一条查无
    此人的记录，这份档案就不能再当清单用了。
    """
    counts = {'用户可见': 0, '仅日志': 0, '明确忽略': 0}
    for _fn, _line, body in _all_catches():
        for m in _MARKER_RE.finditer(body):
            counts[m.group(1)] += 1
    assert counts['仅日志'] > 0 and counts['明确忽略'] > 0, (
        f'B / C 两档的标记不见了（{counts}）—— 改标记名的话本文件要一起改'
    )

    orphans = []
    for fn in sorted(os.listdir(JS_DIR)):
        if not fn.endswith('.js'):
            continue
        src = _js(fn)
        # 每个 catch 体在文件里的字符区间，用来判「这个标记在不在某个 catch 里」
        spans = []
        for m in re.finditer(r'\bcatch\b\s*(?:\([^)]*\))?\s*\{', src):
            if not src[:m.start()].rstrip().endswith('}'):
                continue
            open_at = src.index('{', m.end() - 1)
            spans.append((open_at, open_at + 1 + len(_brace_body(src, open_at))))
        for m in _MARKER_RE.finditer(src):
            if not any(start < m.start() < end for start, end in spans):
                line = src.count('\n', 0, m.start()) + 1
                orphans.append(f'{fn}:{line} {m.group(0)}')
    assert not orphans, (
        '这些分档标记不在任何 catch 体里 —— grep 出来的档案会多出查无此人的行：\n'
        + '\n'.join('  ' + o for o in orphans)
    )


# ---------------------------------------------------------------------------
# 5. 目录浏览：loading / 焦点 / 回车
# ---------------------------------------------------------------------------

def test_path_browser_says_it_is_loading():
    """读目录期间列表里必须有话说，不能停在上一个目录的内容上。"""
    src = _js('path_browser.js')
    assert 'js.path_browser.loading' in src, (
        '目录浏览没有 loading 占位 —— 慢盘上那几秒里弹窗看着像点了没反应'
    )
    body = _fn_body(_strip_js_comments(src), 'load')
    assert body.index("t('js.path_browser.loading')") < body.index('fetch('), (
        'loading 占位必须在发请求**之前**写进列表，否则它只在响应回来后闪一下'
    )


def test_path_browser_focuses_and_confirms_on_enter():
    """打开就给焦点，回车就是「选择此目录」。"""
    src = _strip_js_comments(_js('path_browser.js'))
    assert re.search(r'selectBtn\.focus\(\)', src), (
        '弹窗打开后没有把焦点交给主按钮 —— 键盘用户要 Tab 过整张目录列表'
    )
    assert "'shown.bs.modal'" in src, (
        '焦点必须挂在 shown.bs.modal 上：Bootstrap 自己会在 show 之后抢一次焦点'
    )
    m = re.search(r"addEventListener\('keydown',\s*function\s*\([^)]*\)\s*\{", src)
    assert m, 'path_browser 没有 keydown 监听 —— 回车不确认'
    handler = _brace_body(src, m.end() - 1)
    assert "'Enter'" in handler, 'keydown 里没有处理 Enter'
    assert 'list-group-item' in handler, (
        '回车必须给目录项让路：焦点在目录项上时回车是「进这个目录」，'
        '抢过来的话键盘用户永远下钻不进去'
    )
    assert '_confirmSelection()' in handler, '回车没有落到「选择此目录」上'


# ---------------------------------------------------------------------------
# 6. 列表加载失败：给重载，**不是**任务重试
# ---------------------------------------------------------------------------

_RELOAD_KEY = 'js.history.action.reload_list'


def test_load_failure_offers_reload_not_task_retry():
    """失败分支要有出路，且这条出路只重发列表那一次 GET。"""
    src = _js('task_list.js')
    error_tpl = src[src.index('const ERROR_TEMPLATE'):src.index('const EMPTY_TEMPLATE')]
    assert '<button' in error_tpl, (
        '列表加载失败分支里只有一句红字，没有任何出路 —— 用户只能按 F5，'
        '连带丢掉筛选、页码与已展开的面板'
    )
    assert _RELOAD_KEY in error_tpl, f'重载按钮的文案键不是 {_RELOAD_KEY}'
    assert 'reloadList' in error_tpl, '重载按钮没有接上处理函数'

    body = _fn_body(_strip_js_comments(src), 'reloadList')
    assert 'loadHistory(' in body, 'reloadList 没有重发列表请求'
    assert 'guard(' in body, 'reloadList 自己也要防连点'
    assert not re.search(r'\b(startTask|resumeTask|refillTask)\b', body), (
        'reloadList 里出现了任务动作 —— 它只许重发列表那一次 GET'
    )


def test_the_reload_key_cannot_be_mistaken_for_a_task_retry():
    """键名与两语言文案都不许带「重试」语义。

    ⚠️ 任务级「重试」被否过两次：三个 manager 的 start_task 只收
    pending/paused（对 failed 调用直接抛 ValueError），而任务生命周期的设计
    是「失败就删掉重建」。一个现成的、叫 retry 的键早晚会被人搬到任务行上，
    那时后端只会回 400，而按钮看着理所当然。
    """
    assert 'retry' not in _RELOAD_KEY, (
        f'{_RELOAD_KEY} 里出现了 retry —— 换个不含重试语义的键名'
    )
    entry = MESSAGES[_RELOAD_KEY]
    assert entry['zh'].strip() and entry['en'].strip(), '两语言文案必须都非空'
    assert '重试' not in entry['zh'], (
        f'中文文案「{entry["zh"]}」带「重试」二字 —— 这里重发的是列表那一次 GET'
    )
    assert 'retry' not in entry['en'].lower(), (
        f'英文文案「{entry["en"]}」带 retry —— 同上'
    )


# ---------------------------------------------------------------------------
# 7. 小地图与统计卡不许再悄悄坏掉（原来是四处 console-only）
# ---------------------------------------------------------------------------

def test_history_side_panels_report_their_own_failures():
    """小地图（起不来 / 画不出）、底图描述符、统计卡：四处都要用户可见。"""
    src = _strip_js_comments(_js('history.js'))
    problems = []
    for fn_name, key in (
        ('initHistory', 'js.history.map_failed'),
        ('_resolveHistoryBasemap', 'js.history.basemap_fallback'),
        ('loadStats', 'js.history.stats_failed'),
    ):
        body = _fn_body(src, fn_name)
        if key not in body:
            problems.append(f'{fn_name} 失败时没有 {key} 那句提示')
        if 'showToast(' not in body:
            problems.append(f'{fn_name} 的失败分支只有 console —— 用户看不见')
    # initHistory 里是**两处**：初始化失败与屏障后那次重绘失败。
    init_body = _fn_body(src, 'initHistory')
    if init_body.count("t('js.history.map_failed')") != 2:
        problems.append(
            'initHistory 里 js.history.map_failed 不是两处 —— '
            '小地图初始化与屏障后的最终重绘各有一条失败路径'
        )
    assert not problems, '历史页的旁路失败仍然是静默的：\n' + '\n'.join(
        '  ' + p for p in problems)
    for key in ('js.history.map_failed', 'js.history.basemap_fallback',
                'js.history.stats_failed'):
        entry = MESSAGES[key]
        assert entry['zh'].strip() and entry['en'].strip(), f'{key} 两语言必须齐'
