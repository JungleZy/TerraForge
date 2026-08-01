"""tasks.js / history.js / ui.js 前端行为契约测试（文本级）。

本项目没有 JS 测试框架（无 package.json/vitest，且不打算引入——会破坏
PyInstaller 离线打包形态）。这些断言守住源码的**形态**：进度条颜色只能
由任务状态推导，不能由完成百分比推导；任务失败时卡片不能消失。

「颜色渲染出来是什么样」「toast 到底有没有在 10 秒后还在」这些断言守不住，
那部分由 CDP 实测覆盖（见 .superpowers/sdd/p2-task-5-report.md 的六态实测色值、
p2-task-6-report.md 的失败态实测）。

背景 1（Task 5）：原实现 getProgressColor(progress) 的映射是反的——
    >=100 success / >=75 info / >=50 primary / >=25 warning / 其余 danger
刚启动的健康任务（0%）立刻显示红色；而 success 永远出现不了，因为
handleTaskCompleted 在进度到 100% 之前就把卡片 remove() 了。

背景 2（Task 6）：handleTaskFailed 原本直接 card.remove()，错误信息只进
console.error。用户盯着 63% 的进度条，卡片突然消失，零提示——他分不清
是失败了、被别人取消了、还是自己看花了眼。
"""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用 Task 5 建立的花括号配对切函数体工具，**不另写一份**。
# 为什么不用简报给的 `src[src.index('function A('):src.index('function B(')]`：
# 那依赖两个函数在文件里的先后顺序，顺序一调 end < start，切片返回空串，
# 于是 `'card.remove()' not in ''` 永真——断言在完全没检查任何东西的情况下
# 通过（p2-assertion-review.md 的 E 条，本任务是该条点名的对象）。
from test_css_contract import _js_function_body  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _js(name):
    with open(os.path.join(ROOT, 'static', 'js', name), encoding='utf-8') as f:
        return f.read()


def _strip_js_comments(src):
    """剥掉 /* */ 与 // 注释。

    为什么要剥：说明「为什么删掉了百分比映射」的注释里必然会复述
    `progress >= 100 ? 'success'`，拿原文匹配会把解释性注释当成回潮。
    test_css_contract.py 里的同类断言已经踩过这个坑两次。

    实现上先剥块注释再剥行注释，并且跳过字符串/模板字面量里的 `//`
    （本文件涉及的 JS 里有 URL 字面量）。
    """
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    out = []
    quote = None
    i = 0
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == '\\':
                out.append(src[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            out.append(ch)
        elif ch in '\'"`':
            quote = ch
            out.append(ch)
        elif ch == '/' and i + 1 < len(src) and src[i + 1] == '/':
            j = src.find('\n', i)
            i = len(src) if j < 0 else j
            continue
        else:
            out.append(ch)
        i += 1
    return ''.join(out)


# 每个 JS 文件里「进度条至少要有几处 bg-${...} 插值」。
#
# ⚠️ 这是**下限**不是等号：后续任务新增进度条不会被挡住，但删掉任何一处
# 已有的调用点会立刻变红。J 条要求的就是这个存在性契约——Phase 1 反复出现
# 「守禁止性契约、不守存在性契约」的不对称：只断言 getProgressColor 消失的话，
# 把整行 className 赋值删掉也能全绿，而进度条会永远停在 Bootstrap 默认蓝。
PROGRESS_BAR_CALL_SITES = {
    'tasks.js': 2,      # createTaskRow 的模板 + updateTaskProgressPartial 的 className
    'history.js': 1,    # viewTaskDetails 详情模态框
}

# `progress-bar bg-${<表达式>}` 里允许出现的表达式（唯一形态）。
_ALLOWED_COLOR_EXPR = re.compile(r'^getStatusColor\(\s*task\.status\s*\)$')

_PROGRESS_BAR_BG_RE = re.compile(r'progress-bar\s+bg-\$\{([^}]+)\}')


def test_get_progress_color_is_gone():
    """按百分比给进度条上色的 getProgressColor 必须整体消失（定义 + 全部调用）。

    这是**禁止性**契约。单独看它强度不够——把调用点连同函数一起删掉、
    让进度条回落到 Bootstrap 默认色也能通过。配套的存在性契约见
    test_progress_bar_color_comes_from_status。
    """
    for name in ('tasks.js', 'history.js'):
        src = _strip_js_comments(_js(name))
        assert 'function getProgressColor(' not in src, (
            f'{name} 仍定义 getProgressColor()，应改用 getStatusColor(status)'
        )
        assert 'getProgressColor(' not in src, f'{name} 仍有 getProgressColor() 调用'


def test_progress_bar_color_comes_from_status():
    """每一处 `progress-bar bg-${...}` 插值都必须是 getStatusColor(task.status)。

    这条是 J 条要求补的**存在性**契约，守三件事：

      1. 调用点没被删掉（每个文件的处数不低于 PROGRESS_BAR_CALL_SITES）。
         漏改/删掉 tasks.js 里 updateTaskProgressPartial 那处是最可能的失误——
         它是 Socket.IO 增量刷新的实际主路径，页面初次渲染看不出问题，
         任务跑起来才暴露。
      2. 插值里确实调的是 getStatusColor，不是别的什么函数或裸变量。
         history.js 原来就是裸变量 `bg-${progressColor}`（一个本地算出来的
         百分比阶梯），只断言「没有 getProgressColor( 字样」的话它照样全绿。
      3. 传进去的是 task.status 而不是 progress——`getStatusColor(progress)`
         能编译、能跑，返回永远是兜底的 'secondary'，肉眼看是一条灰条。
    """
    problems = []
    for name, min_sites in PROGRESS_BAR_CALL_SITES.items():
        src = _strip_js_comments(_js(name))
        exprs = [m.group(1).strip() for m in _PROGRESS_BAR_BG_RE.finditer(src)]
        if len(exprs) < min_sites:
            problems.append(
                f'{name}: 只找到 {len(exprs)} 处 `progress-bar bg-${{...}}` 插值，'
                f'至少应有 {min_sites} 处——有调用点被删掉或写法变了'
            )
        for expr in exprs:
            if not _ALLOWED_COLOR_EXPR.match(expr):
                problems.append(
                    f'{name}: `progress-bar bg-${{{expr}}}` 不是 '
                    'getStatusColor(task.status)——进度条颜色必须由状态决定'
                )
    assert not problems, '进度条配色调用点不合契约：\n' + '\n'.join('  ' + p for p in problems)


def test_socketio_incremental_path_is_wired():
    """点名守住 Socket.IO 增量刷新路径上那行 className 赋值。

    上一条已经覆盖了它，这里再钉一次只是为了让回潮时的失败信息直接指向
    正确的地方——这行是任务跑起来之后真正决定进度条颜色的那一行，
    createTaskRow 只在状态切换时才重建整行。
    """
    src = _strip_js_comments(_js('tasks.js'))
    assert re.search(
        r'progressBar\.className\s*=\s*`progress-bar\s+bg-\$\{getStatusColor\(\s*task\.status\s*\)\}`',
        src,
    ), (
        'updateTaskProgressPartial 里的 progressBar.className 没有用 '
        'getStatusColor(task.status)——这是 Socket.IO 增量刷新的主路径，'
        '漏改它的话页面初次渲染正常、任务一跑起来颜色就错'
    )


def test_get_status_color_defined_in_both_files():
    """两个文件各自都要有 getStatusColor 定义（它们没有共享模块）。

    没有构建工具、没有 ES module，history.js 与 tasks.js 不会同时加载，
    所以各自都得有一份。少一份 = 详情模态框直接抛 ReferenceError。
    """
    for name in ('tasks.js', 'history.js'):
        src = _strip_js_comments(_js(name))
        assert 'function getStatusColor(' in src, (
            f'{name} 没有定义 getStatusColor()，插值会抛 ReferenceError'
        )


# 「数值比较 + Bootstrap 颜色名」同处一条语句 = 百分比阶梯。
#
# 两种写法都要抓：
#   函数版  `if (progress >= 100) return 'success';`
#   内联版  `const c = progress >= 100 ? 'success' : ...;`（history.js 原来就是这个）
# 中间隔着 `) return ` 或 ` ? `，都不含分号，所以用 `[^;]{0,200}` 连接。
_COLOR_NAME = r"'(?:success|info|primary|warning|danger|secondary|dark)'"
_LADDER_RE = re.compile(
    r'\bprogress\w*\s*[<>]=?\s*\d+[^;]{0,200}?' + _COLOR_NAME,
    re.S | re.I,
)


def test_no_percentage_to_color_ladder_anywhere_in_frontend_js():
    """static/js 下任何文件都不许再出现「按百分比挑颜色」的阶梯。

    强度说明（计划原文给的是 `assert 'getProgressColor(' not in hist`）：
    **那条断言在改动前就已经是绿的** —— 实测 history.js 从来没有
    getProgressColor 这个函数，它是把同一个阶梯**内联复制**了一份
    （`const progressColor = progress >= 100 ? 'success' : ...`）。
    照抄计划的话，history.js 的缺陷会原封不动留下来而测试全绿。
    所以这里改成按**形态**扫描全部 JS，而不是查某个函数名。

    ⚠️ 给后续任务的说明：这条禁的是「数值比较的结果被拿来当颜色名」。
    如果你确实需要 `if (progress >= 100)` 做别的事（比如切文案），
    只要 200 字符内不出现 Bootstrap 颜色字面量就不会被误伤。
    真被误伤了，说明写法确实容易被误读成配色阶梯，换个写法或改本断言。
    """
    js_dir = os.path.join(ROOT, 'static', 'js')
    offenders = []
    scanned = []
    for fn in sorted(os.listdir(js_dir)):
        if not fn.endswith('.js'):
            continue
        with open(os.path.join(js_dir, fn), encoding='utf-8') as f:
            src = _strip_js_comments(f.read())
        scanned.append(fn)
        for m in _LADDER_RE.finditer(src):
            line = src[:m.start()].count('\n') + 1
            snippet = re.sub(r'\s+', ' ', m.group(0))[:90]
            offenders.append(f'{fn}:{line} {snippet}')
    # 自检：目录扫空的话下面的负向断言就是永真
    assert {'tasks.js', 'history.js'} <= set(scanned), (
        f'没扫到 tasks.js / history.js（实际扫到 {scanned}）——本测试已失效'
    )
    assert not offenders, (
        '发现按完成百分比挑颜色的阶梯——进度条颜色必须由任务状态决定：\n'
        + '\n'.join('  ' + o for o in offenders)
    )


# --------------------------------------------------------------------------
# A1b / Task 6：任务失败后保留卡片并显示原因
# --------------------------------------------------------------------------

def _fn(name, js_name='tasks.js'):
    """先剥注释、再按花括号配对切出函数体。

    顺序不能反：注释里出现的 `{` / `}` 会把花括号配对带偏（本任务的
    handleTaskFailed 注释里就写了「不 card.remove()」这类字样，不剥注释
    的话负向断言还会被自己的解释性注释误伤——test_css_contract.py 里
    的同类断言已经踩过两次这个坑）。
    """
    body = _js_function_body(_strip_js_comments(_js(js_name)), name)
    assert body.strip(), f'{js_name} 的 {name} 函数体切出来是空的 —— 本测试已失效'
    return body


def test_failed_task_row_is_not_removed():
    """任务失败时不许删行、也不许把任务从 activeTasks 里摘掉。

    （前身是 test_failed_task_card_is_not_removed。统一任务表改版把卡片改成
    表格行，契约不变：失败行必须留在页面上——转红 + 错误行，
    清理只能由用户点「移除」触发（dismissTask）。）

    这是本任务的核心禁止性契约。原实现两件事一起做：
        activeTasks.delete(key);  →  卡片下次整体重绘时不会再出现
        card.remove();            →  当场消失
    用户盯着 63% 的进度条，卡片突然没了，什么提示都没有。

    强度说明（简报原文用 `src[src.index(A):src.index(B)]` 切片）：
    那种切法依赖 handleTaskFailed 排在 getStatusColor 前面。顺序一调
    `end < start`，`src[start:end]` 返回空串，`'card.remove()' not in ''`
    直接为真——**断言在完全没检查任何东西的情况下通过**
    （p2-assertion-review.md 的 E 条）。这里改用 `_fn()`（花括号配对，
    与函数先后顺序无关）并加了「函数体非空」自检。

    覆盖范围（诚实说明）：这条守的是源码形态。它保证不了「行在浏览器里
    真的还在」——那由 CDP 实测覆盖（见 p2-task-6-report.md）。
    """
    body = _fn('handleTaskFailed')

    assert not re.search(r'\.remove\(\s*\)', body), (
        'handleTaskFailed 里出现了 .remove() 调用——失败行必须留在页面上，'
        '清理只能由用户点「移除」触发（dismissTask）。'
        '（错误行的去重要用 outerHTML 原位重建，不许走 .remove()。）'
    )
    assert 'activeTasks.delete(' not in body, (
        'handleTaskFailed 仍把任务从 activeTasks 摘掉——下一次整体重绘'
        '（renderActiveTasks）就会让失败行再次凭空消失'
    )


def test_failed_task_pops_a_persistent_toast():
    """handleTaskFailed 必须弹 danger toast，且显式传 duration: 0。

    为什么要显式 0：ui.js 的默认 duration 是 3500ms。3.5 秒之后提示自己
    消失，用户离开座位一趟回来还是什么都不知道——和原来的静默消失只差
    3.5 秒。duration: 0 的语义由下一条测试钉住。
    """
    body = _fn('handleTaskFailed')

    m = re.search(r'showToast\s*\((.*?)\)\s*;', body, re.S)
    assert m, 'handleTaskFailed 没有调用 showToast —— 失败必须有全局提示'
    call = m.group(1)
    assert "'danger'" in call, f'showToast 的 type 不是 danger：{call!r}'
    assert re.search(r'duration\s*:\s*0\b', call), (
        f'showToast 没有传 duration: 0，提示会在 3.5 秒后自己消失：{call!r}'
    )


def test_toast_duration_zero_really_means_persistent():
    """ui.js 里 duration: 0 必须真的等于「不自动消失」。

    上一条只能证明 tasks.js **写了** `duration: 0`。「0 表示常驻」是 ui.js
    的实现细节，而且是**易碎**的：把 `if (duration > 0)` 改成
    `setTimeout(remove, duration || 3500)`，或者把默认值判断从
    `opts.duration != null` 改成 `opts.duration ||`，0 就会被当成
    「没传」而套上 3.5 秒默认值——tasks.js 一个字都不用改，常驻悄悄失效。
    这条把那两处语义一起钉住。
    """
    body = _fn('showToast', 'ui.js')

    assert re.search(r'opts\.duration\s*!=\s*null\s*\?\s*opts\.duration\s*:', body), (
        'showToast 的默认 duration 不再是用 `opts.duration != null ? ... : ...` '
        '取的——写成 `opts.duration || 3500` 会把显式的 0 当成没传，'
        '常驻 toast 会退化成 3.5 秒后消失'
    )
    assert re.search(r'if\s*\(\s*duration\s*>\s*0\s*\)\s*timer\s*=\s*setTimeout\(', body), (
        'showToast 不再用 `if (duration > 0)` 守住定时器——'
        'duration: 0 会重新变成「自动消失」'
    )


def test_error_message_never_reaches_innerhtml():
    """错误文本只能经 textContent 落地，绝不进 HTML 模板。

    error_message 是后端异常的字符串化结果（URL、文件路径、第三方库的
    报错原文都可能在里面）。它一旦被拼进 createTaskErrorRow 的模板字符串，
    `tbody.innerHTML = ...` 就会把里面的 `<img onerror=...>` 当标签解析。
    ui.js 里同样的地方已经写了 `// textContent 防 XSS` 的注释，这里是同一条规矩。

    做法上让 createTaskRow / createTaskErrorRow 只吐一个**空**的 `.task-error`
    容器（连 error_message 都不引用），文本由 applyTaskErrorText 单独填。
    所以这条可以直接断言「两个模板函数里根本没有 error_message 这个词」。
    """
    src = _strip_js_comments(_js('tasks.js'))
    for fn_name in ('createTaskRow', 'createTaskErrorRow'):
        row_body = _js_function_body(src, fn_name)
        assert 'error_message' not in row_body, (
            f'{fn_name} 里出现了 error_message——它的返回值会被塞进 innerHTML，'
            '后端错误原文里的 HTML 会被当标签解析（XSS）。'
            '错误文本请交给 applyTaskErrorText 用 textContent 填'
        )

    apply_body = _js_function_body(src, 'applyTaskErrorText')
    assert re.search(r'\.textContent\s*=', apply_body), (
        'applyTaskErrorText 没有用 textContent 赋值'
    )
    assert 'innerHTML' not in apply_body, (
        'applyTaskErrorText 用了 innerHTML——错误原文必须走 textContent'
    )


def test_full_rerender_keeps_the_error_text():
    """整体重绘（renderActiveTasks）之后错误文本必须被重新填回去。

    存在性契约。renderActiveTasks 是 `tbody.innerHTML = tasks.map(...)`，
    一次性重建全部行；失败行现在会留在 activeTasks 里，所以任何一个
    新任务到达都会触发重绘。漏掉这一步的话：失败当场看得见错误，之后
    随便来一个新任务，红框就变空了——而所有文本断言依然全绿。
    """
    render_body = _fn('renderActiveTasks')
    assert 'applyTaskErrorText' in render_body, (
        'renderActiveTasks 重建 innerHTML 之后没有回填错误文本，'
        '失败卡片的红框会在下一次整体重绘时被清空'
    )


# createTaskRow 里每个 onclick 动作，期望它**最近的前置**状态判断是哪一个。
#
# 这张表是「不许加重试按钮」这条硬约束的机器检查：三个 manager 的 start_task
# 都要求 status in ('pending','paused')，对 failed 调用直接抛 ValueError。
# 谁要是在失败分支里塞一个 `onclick="startTask(...)"`，startTask 最近的前置
# 判断就变成 `=== 'failed'`，这条立刻变红。
_ACTION_GUARDS = {
    'startTask': ('===', 'pending'),
    'pauseTask': ('===', 'running'),
    'resumeTask': ('===', 'paused'),
    'cancelTask': ('!==', 'failed'),   # 失败任务不能再调后端 cancel
    'dismissTask': ('===', 'failed'),  # 只有失败卡片给「移除」
}

_STATUS_GUARD_RE = re.compile(r"task\.status\s*(===|!==)\s*'(\w+)'")


def test_card_actions_are_gated_by_the_right_status():
    """行上每个动作按钮都必须挂在正确的状态分支下。

    实现方式：对 createTaskRow 体内每一处 `onclick="xxxTask(`，往前找
    **最近**的 `task.status === / !== '...'`，与 _ACTION_GUARDS 对表。

    这条同时守三件事：
      1. 失败行**没有**重试按钮（后端会抛 ValueError，见上面表里的注释）。
      2. 失败行**有**「移除」按钮。行不再自动消失了，没有这个按钮
         用户就没有任何办法清掉它，只能刷新页面。
      3. 失败行不再显示「取消」——对一个已经 failed 的任务调
         /cancel 是无意义的后端往返。

    已知弱点（诚实说明）：「最近的前置判断」是启发式的。若有人把按钮写成
    多层嵌套三元、或者用 `${cond ? ... : ...}` 之外的方式生成 onclick，
    这条可能失配。所以每个动作还额外断言了「至少出现一次」——完全找不到
    比匹配错更危险。
    """
    body = _fn('createTaskRow')
    problems = []
    for action, expected in _ACTION_GUARDS.items():
        hits = list(re.finditer(r'onclick="' + action + r'\(', body))
        if not hits:
            problems.append(f'{action}: createTaskRow 里一处调用都没有')
            continue
        for h in hits:
            guards = _STATUS_GUARD_RE.findall(body[:h.start()])
            if not guards:
                problems.append(f'{action}: 前面找不到任何 task.status 判断')
                continue
            if guards[-1] != expected:
                problems.append(
                    f'{action}: 最近的状态判断是 '
                    f"task.status {guards[-1][0]} '{guards[-1][1]}'，"
                    f"期望 task.status {expected[0]} '{expected[1]}'"
                )
    assert not problems, '行动作按钮的状态门控不对：\n' + '\n'.join('  ' + p for p in problems)


def test_dismiss_is_purely_local():
    """「移除」只清前端卡片，不许打后端。

    失败任务在后端已经是终态，dismissTask 若像 cancelTask 那样 POST
    /cancel，三个 manager 的 cancel_task 对 failed 的反应各不相同
    （最好的情况是白跑一趟，最坏是 500），而用户想要的只是「把这张卡片
    从我眼前拿走」。
    """
    body = _fn('dismissTask')
    assert 'fetch(' not in body, (
        'dismissTask 里有 fetch()——「移除」应当只是前端清卡片，不碰后端'
    )
    assert 'activeTasks.delete(' in body, (
        'dismissTask 没有把任务从 activeTasks 摘掉，卡片会在下次重绘时回来'
    )


def test_failure_toasts_are_deduped_per_task_not_globally():
    """同一个任务的常驻 toast 只留一条，**不同任务的必须各留一条**。

    失败 toast 是 duration: 0 的，不会自己消失。等高线任务在下载阶段和渲染
    阶段各有一个失败出口（`services/contour_task_manager.py` 有 3 处
    `emit("task_failed")`），同一个 task 重复发事件会让永不消失的提示白白堆高。

    但**不能**退化成「全局只留一条」：8 个任务失败就是 8 个不同的原因，
    合并掉等于把前 7 条错误信息扔了。所以这里同时钉两件事：
      1. 合并逻辑存在（`closeFailureToast(key)` 在 set 之前被调用）；
      2. 合并的键是 `key`（taskType:taskId），不是常量、不是全局单例。

    另外钉住「点移除时顺手关掉那条 toast」——卡片都不要了还留一条永久提示
    占着右上角，等于把 I2 的堆叠问题换个地方保留。
    """
    body = _fn('handleTaskFailed')
    assert re.search(r'closeFailureToast\(\s*key\s*\)', body), (
        'handleTaskFailed 没有先关掉同一任务的旧 toast，常驻提示会重复堆叠'
    )
    assert re.search(r'failureToasts\.set\(\s*key\s*,\s*showToast\(', body), (
        'handleTaskFailed 没有按 key 记录 toast 句柄（或者合并键不是 key）——'
        '合并键不是 taskType:taskId 的话，不同任务的失败原因会被互相顶掉'
    )

    close_body = _fn('closeFailureToast')
    assert '.close()' in close_body and 'failureToasts.delete(' in close_body, (
        'closeFailureToast 必须既关 toast 又清 Map，否则句柄会一直攒着'
    )

    dismiss_body = _fn('dismissTask')
    assert 'closeFailureToast(' in dismiss_body, (
        '点「移除」之后那条常驻 toast 还留在右上角——卡片都清了，提示也该走'
    )


# --------------------------------------------------------------------------
# A2 / Task 7：百分比从 .progress-bar 的子元素改成 .progress 里的覆盖层
# --------------------------------------------------------------------------

# `<div class="progress-bar ...>` 一直到它的 `</div>`。
# 属性值里有 `${progress}%` 和引号，但没有 `>`，所以 `[^>]*>` 切得干净。
_PROGRESS_BAR_ELEMENT_RE = re.compile(
    r'<div\s+class="progress-bar[^>]*>(.*?)</div>', re.S)

# `class="progress"`（带结束引号）——不会误伤 progress-bar / progress-detail /
# progress__label / progress-container。
_PROGRESS_TRACK_ATTR_RE = re.compile(r'class="progress"')
_PROGRESS_LABEL_ATTR_RE = re.compile(r'class="progress__label"')

# 两个文件里各有几处 `.progress` 轨道模板。等号不是下限：多出来的一处大概率是
# 复制粘贴出来的第二套渲染路径，那正是本任务要防的「漏改一处出重复标签」。
#
# ⚠️ tasks.js 是 **0**（统一任务表改版，有意如此）：活动任务行改用 14px 紧凑条
# （.task-progress），装不下 18px 的覆盖层 chip，百分比移到条右边的 .task-pct。
# 那一处的结构契约由下面三条里的「条内必须是空的」+ 
# test_socketio_incremental_path_updates_the_label_not_the_bar（.task-pct 同步）守住。
# 覆盖层体系（.progress + .progress__label）只剩 history.js 详情模态框一处。
PROGRESS_TRACK_MARKUP_SITES = {'tasks.js': 0, 'history.js': 1}


def test_percentage_is_an_overlay_not_a_child_of_the_bar():
    """在仍使用 `.progress` 轨道的渲染点，百分比必须是 `.progress` 里的独立
    `<span class="progress__label">`；**任何** `.progress-bar` 元素自己都不能
    再有文字内容（含 tasks.js 的紧凑条）。

    这是本任务的结构契约，守三件事：

      1. **`.progress-bar` 标签之间是空的。** 数字留在里面的话，
         `.progress { overflow: hidden }` + 宽度为 0 会把它整个裁掉 ——
         CDP 实测 progress=0 时数字画出 0 个像素（截图差异法）。
         这一条对 tasks.js 的紧凑条同样成立（它没有覆盖层，数字在条外
         .task-pct，条里一样不许有字）。
      2. **每个 `.progress` 容器恰好一个 `.progress__label`。**
         0 个 = 这一处渲染点漏改了，百分比直接消失；
         2 个 = 同一条进度条上两个百分比。tasks.js 期望 0 处轨道
         （见 PROGRESS_TRACK_MARKUP_SITES 的注释），多个出来就是
         有人把两套体系混着用了。
      3. **覆盖层必须是 `<span>` 不能是 `<div>`。** style.css 里曾有一条
         `div:not(.card):not(.modal-content)...{background:transparent}`
         兜底重置（已删），特异度 (0,10,1)，会把**任何**不在白名单里的
         div 背景压成透明。覆盖层的可读性正是靠自带的那块不透明底撑着（见
         test_progress_label_readability_does_not_depend_on_the_fill）。
    """
    problems = []
    for name, sites in PROGRESS_TRACK_MARKUP_SITES.items():
        src = _strip_js_comments(_js(name))

        tracks = len(_PROGRESS_TRACK_ATTR_RE.findall(src))
        if tracks != sites:
            problems.append(
                f'{name}: 找到 {tracks} 处 `class="progress"` 模板，期望 {sites} 处 —— '
                '渲染点被删掉、或者多了一套没人维护的副本'
            )
        labels = len(_PROGRESS_LABEL_ATTR_RE.findall(src))
        if labels != tracks:
            problems.append(
                f'{name}: {tracks} 个 .progress 容器却有 {labels} 个 .progress__label —— '
                '少了会让百分比消失，多了会在同一条进度条上出现两个百分比'
            )

        bars = _PROGRESS_BAR_ELEMENT_RE.findall(src)
        if not bars:
            problems.append(f'{name}: 一处 `<div class="progress-bar ...>` 都找不到 —— 本测试已失效')
        for inner in bars:
            if inner.strip():
                problems.append(
                    f'{name}: `.progress-bar` 标签之间还有内容 {inner.strip()[:40]!r} —— '
                    '百分比必须搬到 .progress 下的覆盖层，留在条里会被 overflow:hidden 裁掉'
                )

        for m in _PROGRESS_LABEL_ATTR_RE.finditer(src):
            head = src[max(0, m.start() - 40):m.start()]
            if '<span' not in head:
                problems.append(
                    f'{name}: `class="progress__label"` 前面 40 字符里没有 `<span` '
                    f'（实际是 {head[-25:]!r}）—— 覆盖层必须是 span，'
                    'div 会被 `div:not(...)` 兜底重置把自带底色压成透明'
                )
    assert not problems, '进度条百分比覆盖层的结构不合契约：\n' + '\n'.join('  ' + p for p in problems)


def test_socketio_incremental_path_updates_the_label_not_the_bar():
    """Socket.IO 增量刷新路径必须改条外百分比的文字，且**不能**再往进度条里写文字。

    `updateTaskProgressPartial` 是任务跑起来之后真正在刷新界面的那条路
    （createTaskRow 只在状态切换时重建整行）。漏改这里的后果很具体：
    行初次渲染是对的（条外一个百分比），第一个 task_progress 事件一到，
    `progressBar.textContent = '37%'` 又在条里塞回一个 —— 同一条进度条上
    出现**两个**百分比，而所有只看模板的断言依然全绿。

    （统一任务表改版：百分比从 .progress 里的覆盖层 .progress__label
    改成紧凑条右边的 .task-pct——14px 的条装不下 18px 的 chip。
    守护的语义不变：增量路径更新的是条外文本，不是条。）
    """
    body = _fn('updateTaskProgressPartial')

    assert re.search(r"querySelector\(\s*'\.task-pct'\s*\)", body), (
        'updateTaskProgressPartial 没有取 .task-pct —— '
        '进度数字在 Socket.IO 刷新时不会更新（会一直停在初次渲染的值）'
    )
    assert re.search(r'\.textContent\s*=\s*`\$\{progress\}%`', body), (
        'updateTaskProgressPartial 没有把 `${progress}%` 写进条外百分比'
    )
    assert not re.search(r'progressBar\.textContent\s*=', body), (
        'updateTaskProgressPartial 仍在给 progressBar.textContent 赋值 —— '
        '条外文本 + 条内文字会同时存在，同一条进度条上出现两个百分比'
    )


def test_progress_bar_keeps_a_programmatic_value_after_losing_its_text():
    """文字搬走之后，`role="progressbar"` 必须靠 aria-valuenow 报数值。

    改之前进度条元素里就是那串「37%」文本，屏幕阅读器至少还能读到点东西。
    文字搬到兄弟节点之后，`.progress-bar` 变成一个**空**的 progressbar，
    没有 aria-valuenow 就等于什么值都不报。history.js 那处原本就漏了
    aria-valuenow（只有 role 和内联 width），这条一并钉住。
    """
    problems = []
    for name in ('tasks.js', 'history.js'):
        src = _strip_js_comments(_js(name))
        for m in _PROGRESS_BAR_ELEMENT_RE.finditer(src):
            tag = src[m.start():m.start() + m.group(0).index('>') + 1]
            for attr in ('aria-valuenow', 'aria-valuemin', 'aria-valuemax'):
                if attr not in tag:
                    problems.append(
                        f'{name}: `<div class="progress-bar ...>` 缺 {attr} —— '
                        '条里已经没有文字了，值只能靠 aria-* 报出去'
                    )
            if 'aria-valuenow="${progress}"' not in tag:
                problems.append(
                    f'{name}: aria-valuenow 不是 `${{progress}}` —— 报出去的值和画面对不上'
                )
    assert problems == [], (
        '空进度条没有可编程的数值：\n' + '\n'.join('  ' + p for p in problems))

    body = _fn('updateTaskProgressPartial')
    assert re.search(r"setAttribute\(\s*'aria-valuenow'\s*,\s*progress\s*\)", body), (
        'updateTaskProgressPartial 没有同步 aria-valuenow —— '
        '视觉上在涨，报给辅助技术的值停在初始值'
    )


def _element_inner_html(src, open_tag_start):
    """从 `<div ...>` 的起始下标切出它与配对 `</div>` 之间的内容。

    按 `<div` / `</div>` 计深度配对，不用「下一个 `</div>`」——那会在
    `.progress` 里嵌着 `.progress-bar` 的时候提前收尾，把覆盖层判成「在外面」。
    只数 div：`<span>` 不影响 div 的配对。
    """
    gt = src.index('>', open_tag_start)
    depth, i = 1, gt + 1
    pat = re.compile(r'<div\b|</div>')
    while True:
        m = pat.search(src, i)
        if not m:
            return None
        if m.group(0) == '</div>':
            depth -= 1
            if depth == 0:
                return src[gt + 1:m.start()]
        else:
            depth += 1
        i = m.end()


def test_progress_track_markup_is_nested_and_heightless():
    """`.progress__label` 必须**嵌在** `.progress` 元素里面，且轨道不许内联 height。

    ⚠️ 这条补的是 test_percentage_is_an_overlay_not_a_child_of_the_bar 的洞。
    那条是按**文件**统计 `class="progress"` 与 `class="progress__label"` 的
    出现次数再比相等，**从不检查 span 在不在 track 里面**：把 span 挪到
    `.progress` 外面一行，计数依旧 1 : 1、测试全绿，而
    `position: absolute` 会改为相对 `.card` 定位，数字直接飞出进度条 ——
    正是 test_progress_track_is_a_positioning_context 要防的失败模式，
    只是从 JS 侧绕了进来。这里按 `<div>` 深度配对切出 track 的内容再查。

    顺带钉住第二件事：**轨道的内联 style 里不许再有 height**。
    高度是承重的（矮于 chip 会被 `overflow: hidden` 上下裁断数字），
    只能有一个来源 —— CSS 的 `.progress { height: 28px }`，那里有
    test_every_progress_height_fits_the_label 守下限。内联写一个 8px
    就能绕过那条断言，而所有 CSS 断言照旧全绿。
    """
    problems = []
    for name, sites in PROGRESS_TRACK_MARKUP_SITES.items():
        if sites == 0:
            # tasks.js 改版后没有 .progress 轨道（紧凑条 + 条外 .task-pct），
            # 本条不适用；它的进度条结构由上一条的「条内必须为空」守住。
            continue
        src = _strip_js_comments(_js(name))
        hits = list(_PROGRESS_TRACK_ATTR_RE.finditer(src))
        assert hits, f'{name}: 找不到 `class="progress"` 的模板 —— 本测试已失效'
        for h in hits:
            start = src.rfind('<div', 0, h.start())
            assert start != -1, f'{name}: `class="progress"` 前面没有 `<div` —— 本测试已失效'
            open_tag = src[start:src.index('>', start) + 1]
            if re.search(r'style="[^"]*\bheight\s*:', open_tag):
                problems.append(
                    f'{name}: `.progress` 的内联 style 里有 height '
                    f'({open_tag.strip()[:70]!r}) —— 高度只能由 CSS 提供，'
                    '内联值绕过 test_every_progress_height_fits_the_label 的下限检查'
                )
            inner = _element_inner_html(src, start)
            if inner is None:
                problems.append(f'{name}: `.progress` 的 <div> 配不上 </div> —— 本测试已失效')
                continue
            n = len(_PROGRESS_LABEL_ATTR_RE.findall(inner))
            if n != 1:
                problems.append(
                    f'{name}: `.progress` 元素**内部**有 {n} 个 .progress__label（应为 1）'
                    ' —— 挪到外面的话 position: absolute 会相对 .card 定位，'
                    '数字飞出进度条，而按文件计数的那条断言依然全绿'
                )
    assert not problems, '进度条轨道的 markup 结构不合契约：\n' + '\n'.join('  ' + p for p in problems)


# --------------------------------------------------------------------------
# A7 / Task 12：状态词表必须覆盖后端真实存在的每一个状态
#
# 改前的事实（实测，不是推测）：
#   - `/api/history_all`（routes/api.py 的四路 UNION ALL）**没有 status 谓词**，
#     history.js 也不传 status 参数 —— pending / running / paused 的任务照样
#     出现在历史表格里。
#   - history.js 的 getStatusColor / getStatusText 只映射 completed / failed /
#     cancelled 三态，`return texts[status] || status` 把剩下三态**原样**渲染成
#     后端的英文字面量。CDP 实测徽章里写的就是 `pending` / `running` / `paused`，
#     旁边一行是「✓ 已完成」—— 这就是历史页中英混杂的根源。
#   - statusIcons 同样只有三态，`statusIcons[status] || ''` 静默吐空串：
#     那三态既没有中文、也没有图标，只剩一块与「已取消」完全同色的灰底，
#     等于「运行中」和「已取消」在界面上分不出来。
#
# 词表的真值来自 models/task.py 的 TaskStatus 枚举（用 ast 解析，不 import，
# 避免 config.py 的导入副作用）。**不在测试里手抄一份六元组** —— 手抄的清单
# 会在有人给后端加状态时静默过期，而那正是这条断言唯一要拦的事。
# --------------------------------------------------------------------------

import ast  # noqa: E402


def _task_status_values():
    """从 models/task.py 的 TaskStatus 枚举解析出全部状态字面量。"""
    path = os.path.join(ROOT, 'models', 'task.py')
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == 'TaskStatus':
            vals = {
                st.value.value
                for st in node.body
                if isinstance(st, ast.Assign) and isinstance(st.value, ast.Constant)
                and isinstance(st.value.value, str)
            }
            assert vals, 'TaskStatus 里解析不出任何字符串成员 —— 本测试已失效'
            return vals
    raise AssertionError('models/task.py 里找不到 class TaskStatus —— 本测试已失效')


# 会把状态字面量写进数据库的四个管理器。engine 层不在内（它写的是瓦片/文件级
# 的状态词表 'downloading' / 'rendered' / 'uploaded'，那些列不进任务徽章）。
_STATUS_WRITERS = (
    'task_manager.py', 'dem_task_manager.py',
    'contour_task_manager.py', 'local_terrain_task_manager.py',
)

# 文件级状态词表（dem_files / contour_files / tiles 的 status 列）——与 engine 层
# 被排除是同一理由：这些状态不列进任务徽章，不属于 TaskStatus。管理器自己也会
# 写文件级状态，不能按文件排除，只能按词表剔除：
#   - dem_task_manager 恢复入队：`UPDATE dem_files SET status='pending'
#     WHERE task_id=? AND status='downloading'`（dem_files 粒度的下载中标记）；
#   - I12 新增的 404 无数据颗粒：dem_download_engine 回调把颗粒标 'skipped'
#     （无数据跳过，不算失败），dem_task_manager 的终态统计因此出现
#     `status NOT IN ('completed','skipped','failed')`（FROM dem_files）。
# 新增文件级状态时下面会响亮失败（extra 非空），把新状态加进这里即可——
# 不要加进 TaskStatus：那会连带要求两个 JS 的六态词表覆盖一个永远到不了
# 任务徽章的状态。
_FILE_LEVEL_STATUSES = frozenset({'downloading', 'skipped'})

_STATUS_LITERAL_RE = re.compile(
    r"""(?:SET\s+status\s*=\s*|(?<![-\w])status\s*(?:=|==|!=)\s*|['"]status['"]\s*:\s*)['"]([a-z_]+)['"]""",
    re.I,
)
_STATUS_IN_RE = re.compile(r"(?<![-\w])status\s+(?:NOT\s+)?IN\s*\(([^)]*)\)", re.I)


def test_task_status_enum_covers_what_the_managers_actually_write():
    """四个管理器里出现的每一个状态字面量都必须在 TaskStatus 枚举里。

    为什么需要这条：下面那条断言拿 TaskStatus 当真值，去要求两个 JS 覆盖全部状态。
    可四个管理器里有三个**根本不用这个枚举**，它们直接写字符串字面量
    （`UPDATE dem_tasks SET status='paused' ...`）。也就是说枚举本身可能是过期的
    —— 那样上面那条断言就是在拿一份不完整的清单验收「全覆盖」，全绿而漏。
    这正是 Task 10 补链时点名的那类缺口：配对断言守住了「标签 <-> 变量」，
    守不住「变量 <-> 数据源」。这条把数据源那一端接上。

    做法：扫四个管理器里所有 `SET status='x'` / `status = 'x'` / `'status': 'x'` /
    `status IN ('a','b')` 的字面量，剔除 _FILE_LEVEL_STATUSES（dem_files 等
    文件级状态，见上方注释）后，要求剩下的是枚举的子集。
    """
    enum_values = _task_status_values()
    found = {}
    for fn in _STATUS_WRITERS:
        path = os.path.join(ROOT, 'services', fn)
        assert os.path.exists(path), f'{fn} 不存在 —— 本测试已失效（管理器改名/搬家了？）'
        with open(path, encoding='utf-8') as f:
            src = f.read()
        for m in _STATUS_LITERAL_RE.finditer(src):
            lit = m.group(1)
            if lit in _FILE_LEVEL_STATUSES:
                continue
            found.setdefault(lit, set()).add(fn)
        for m in _STATUS_IN_RE.finditer(src):
            for lit in re.findall(r"'([a-z_]+)'", m.group(1)):
                if lit in _FILE_LEVEL_STATUSES:
                    continue
                found.setdefault(lit, set()).add(fn)
    # 自检：扫空的话下面的子集断言永真
    assert len(found) >= len(enum_values), (
        f'只从四个管理器里扫到 {sorted(found)}，比枚举 {sorted(enum_values)} 还少 —— '
        '正则失效了，本测试已无效'
    )
    extra = set(found) - enum_values
    assert not extra, (
        '管理器写了 TaskStatus 里没有的状态：\n'
        + '\n'.join(f'  {k!r} <- {sorted(found[k])}' for k in sorted(extra))
        + '\n枚举是前端词表断言的真值来源，漏一个状态 = 界面上冒出一个英文字面量。'
        '任务级状态：把它补进 models/task.py 的 TaskStatus，再补进两个 JS 的三张表；'
        '文件级状态（dem_files / contour_files / tiles 的 status 列，不进任务徽章）：'
        '补进本文件上方的 _FILE_LEVEL_STATUSES。'
    )


def _js_object_literal_keys(body, var_name):
    """切出 `const <var_name> = { ... }` 的键集合。

    只认单引号包起来的键 —— 本项目三张状态表都是这个写法，
    换写法（裸键 / 计算键）会让键集合变小、断言变红，那是**响亮失败**，
    不是静默放行。
    """
    m = re.search(r'\b(?:const|let|var)\s+' + re.escape(var_name) + r'\s*=\s*\{', body)
    assert m, f'找不到 `{var_name} = {{` —— 本测试已失效'
    start = body.index('{', m.end() - 1)
    depth = 0
    for j in range(start, len(body)):
        if body[j] == '{':
            depth += 1
        elif body[j] == '}':
            depth -= 1
            if depth == 0:
                inner = body[start + 1:j]
                break
    else:
        raise AssertionError(f'{var_name} 的花括号不配对 —— 本测试已失效')
    keys = re.findall(r"'([a-z_]+)'\s*:", inner)
    assert keys, f'{var_name} 里解析不出任何键 —— 本测试已失效'
    return set(keys), inner


# 状态表在哪里：两张都是顶层函数。
#
# 登记（2026-08 统一流式列表重设计）：第三张表 statusIcons（行内徽章 SVG
# 图标表）随徽章 pill 一并删除——定稿设计的状态识别 = 状态点配色 +
# 小字状态文本，行里不再有徽章。六态覆盖的守卫相应搬家：
#   · getStatusColor / getStatusText 的六态覆盖仍由本节的下面两条守；
#   · 状态点的六态配色（图形侧，替代图标的 WCAG 1.4.1 职责）改由
#     tests/test_css_contract.py::test_task_row_status_dot_covers_every_status 守；
#   原 test_status_icons_are_real_distinct_glyphs 随之删除（无表可查）。
_STATUS_MAPS = (
    ('getStatusColor', 'colors', None),
    ('getStatusText', 'texts', None),
)


def test_both_js_files_map_every_backend_status():
    """history.js 与 tasks.js 的两张状态表都必须覆盖 TaskStatus 的全部六态。

    强度说明 —— 为什么不写成 `assert "'paused'" in src`：
    那种断言在 history.js 里查的是「文件里有没有出现过这个词」，
    而 getStatusColor 与 getStatusText 是两张独立的表，补了一张漏了另一张
    照样绿。这里按函数体逐张表解析键集合，并要求**等于**枚举
    （不是「包含」）—— 多一个不在后端存在的状态同样报错，因为那说明
    有人在前端凭空造了一个界面上永远到不了的分支。

    覆盖数的边界：2 个文件 x 2 张表 = 4 组（2026-08 前是 x3：第三张
    statusIcons 随徽章 pill 删除，见 _STATUS_MAPS 的登记），每组 6 个键。
    断言先钉住组数，再逐组比对 —— 只比对不钉组数的话，解析逻辑挂掉
    返回空列表时是永真。
    """
    enum_values = _task_status_values()
    assert len(enum_values) == 6, (
        f'TaskStatus 现在有 {len(enum_values)} 个成员：{sorted(enum_values)}。'
        '不是 6 个不一定是错，但下面每张表的期望值要跟着改，先确认是有意的'
    )
    checked = []
    problems = []
    for js_name in ('tasks.js', 'history.js'):
        src = _strip_js_comments(_js(js_name))
        for var_label, var_name, holders in _STATUS_MAPS:
            body = src if holders is None else _js_function_body(src, holders[js_name])
            if holders is None:
                body = _js_function_body(src, var_label)
            keys, _inner = _js_object_literal_keys(body, var_name)
            checked.append(f'{js_name}:{var_label}')
            if keys != enum_values:
                missing = sorted(enum_values - keys)
                extra = sorted(keys - enum_values)
                problems.append(
                    f'{js_name} 的 {var_label} 键集合 {sorted(keys)}'
                    + (f'，缺 {missing}' if missing else '')
                    + (f'，多出 {extra}' if extra else '')
                )
    assert len(checked) == 4, f'只检查了 {checked}（期望 4 组）—— 本测试已失效'
    assert not problems, (
        '状态词表没覆盖后端全部状态：\n' + '\n'.join('  ' + p for p in problems)
        + f'\n真值来自 models/task.py 的 TaskStatus = {sorted(enum_values)}。'
        '\n漏掉的状态会走 `|| status` / `|| \'\'` 兜底：'
        '中文界面里冒出英文字面量，徽章没有图标。'
    )


def test_status_labels_are_never_the_raw_backend_literal():
    """`getStatusText` 的每一个值都必须是中文，不能是英文原值。

    为什么单独一条：上一条只查「键齐不齐」。补一行 `'paused': 'paused'`
    就能让键集合合格，而界面上仍然显示英文 —— 中英混杂原样保留，测试全绿。
    这条把值也钉住：必须含中日韩统一表意文字，且不得等于键本身。
    """
    enum_values = _task_status_values()
    problems = []
    checked = 0
    for js_name in ('tasks.js', 'history.js'):
        body = _js_function_body(_strip_js_comments(_js(js_name)), 'getStatusText')
        pairs = re.findall(r"'([a-z_]+)'\s*:\s*'([^']*)'", body)
        assert len(pairs) == len(enum_values), (
            f'{js_name} 的 getStatusText 解析出 {len(pairs)} 对映射，'
            f'期望 {len(enum_values)} 对 —— 本测试已失效'
        )
        for key, value in pairs:
            checked += 1
            if value == key or not re.search(r'[一-鿿]', value):
                problems.append(f'{js_name}: {key!r} -> {value!r}')
    assert checked == 2 * len(enum_values), f'只检查了 {checked} 条映射 —— 本测试已失效'
    assert not problems, (
        '状态文案不是中文（界面会中英混杂）：\n' + '\n'.join('  ' + p for p in problems)
    )


def test_terrain_job_status_is_translated_too():
    """详情弹窗的地形切片状态也要走 getStatusText，不许把 job.status 原样插进徽章。

    改前 history.js 的 refreshTerrainDetail 是
        `statusEl.innerHTML = \\`<span class="badge bg-${color}">${status}</span>\\``
    —— 与历史表格是同一个毛病的另一处实例：中文弹窗里显示 `running`。
    地形作业的词表（running / completed / failed）是 TaskStatus 的子集，
    所以直接复用两个函数即可，不需要第二份映射。

    ⚠️ 强度自评（诚实）：这是一条**结构**断言 —— 它检查徽章的文本位置来自
    getStatusText 调用而不是裸变量，守不住「getStatusText 返回了什么」
    （那部分由上面两条守）。它拦得住的是「有人把这里改回内联三元阶梯」。

    **已知绕过（评审实测，本条拦不住）**：保留 `getStatusText(...)` 模板不动，
    在下一行补一句
        `statusEl.querySelector('.badge').textContent = status;`
    徽章会重新显示英文 `running`，而本条断言全绿。
    根因是它只看**构建 markup 的那一行**，看不到之后对 DOM 的再次赋值。
    要堵住这个口子需要一个 JS 运行时（本项目没有：无 package.json / vitest，
    引入会破坏 PyInstaller 的离线打包形态），或者一次 CDP 实测。
    下一个动这块的人：知道边界在这里，别把这条当成语义保障。
    """
    body = _js_function_body(_strip_js_comments(_js('history.js')), 'refreshTerrainDetail')
    badges = re.findall(r'<span class="badge bg-([^"]*)">([^<]*)</span>', body)
    assert len(badges) == 3, (
        f'refreshTerrainDetail 里解析出 {len(badges)} 个徽章模板（期望 3：'
        '未开始 / 作业状态 / 加载失败）—— 本测试已失效'
    )
    def resolve(expr, want):
        """`${x}` 里要么直接是 want(...) 调用，要么是同一函数体内 `const x = ...`
        的局部变量，再看它的右值。只解一层 —— 解不动就判不合格，不静默放行。"""
        if want in expr:
            return True
        m = re.fullmatch(r'\$\{\s*([A-Za-z_$][\w$]*)\s*\}', expr.strip())
        if not m:
            return False
        d = re.search(r'\b(?:const|let|var)\s+' + re.escape(m.group(1)) + r'\s*=([^;]*);', body)
        return bool(d) and want in d.group(1)

    problems = []
    for color_expr, label_expr in badges:
        if '${' not in label_expr:
            continue                      # 中文字面量（「未开始」「加载失败」），合格
        if not resolve(label_expr, 'getStatusText('):
            problems.append(f'徽章文案 `{label_expr}` 追不到 getStatusText(...)')
        if not resolve('${' + color_expr.strip('${}') + '}', 'getStatusColor('):
            problems.append(f'徽章配色 `{color_expr}` 追不到 getStatusColor(...)')
    # 自检：三个徽章里必须恰好有一个是插值的，否则上面的循环什么都没查
    interpolated = [b for b in badges if '${' in b[1]]
    assert len(interpolated) == 1, (
        f'期望恰好 1 个插值徽章（作业状态），实际 {len(interpolated)} 个 —— 本测试已失效'
    )
    assert not problems, (
        '地形切片状态没走统一词表：\n' + '\n'.join('  ' + p for p in problems)
    )


# --------------------------------------------------------------------------
# A7 / Task 12（评审第二轮）：补掉评审实测出来的 4 个逃逸
#   R2/R3  statusIcons 的**值**没被检查（`'cancelled': ''` 能恢复缺陷症状）
#   R10    状态 -> 文案的映射可以整体互换（键集合齐全就绿）
#   新     getStatusStroke 是第四处状态映射点，改前只覆盖 2/6
# --------------------------------------------------------------------------

# 每个状态的文案里必须出现的关键词。钉关键词而不是整句：
# 「已完成」改成「完成了」不该误红，「failed -> 已完成」必须红。
_STATUS_LABEL_KEYWORD = {
    'pending': '等待',
    'running': '运行',
    'paused': '暂停',
    'completed': '完成',
    'failed': '失败',
    'cancelled': '取消',
}

# 每个状态在历史地图上的描边色应该走哪个调色板令牌。
# pending / cancelled 是中性档，与徽章的中性档一致（见
# test_status_badge_color_matches_the_semantic_token 的说明）。
_STATUS_STROKE_TOKEN = {
    'pending': '--color-text-secondary',
    'running': '--color-info',
    'paused': '--color-warning',
    'completed': '--color-success',
    'failed': '--color-danger',
    'cancelled': '--color-neutral',
}


def test_status_labels_are_paired_with_the_right_status():
    """状态与文案的**配对**，不只是「六个键都在」。

    评审实测的逃逸（R10）：把 getStatusText 的六个值整体轮换一位 ——
    键集合齐全、每个值都是中文，上一版两条断言全绿，
    而界面上失败的任务写着「已完成」。这与 Task 10 的教训同型：
    **守了集合，没守配对。**

    钉关键词而不是整句：文案微调（「已完成」->「完成了」）不该误红，
    整体互换必须红。
    """
    problems, checked = [], 0
    for js_name in ('tasks.js', 'history.js'):
        body = _js_function_body(_strip_js_comments(_js(js_name)), 'getStatusText')
        pairs = dict(re.findall(r"'([a-z_]+)'\s*:\s*'([^']*)'", body))
        assert set(pairs) == set(_STATUS_LABEL_KEYWORD), (
            f'{js_name} 的 getStatusText 键集合是 {sorted(pairs)} —— '
            '先修 test_both_js_files_map_every_backend_status'
        )
        for status, keyword in _STATUS_LABEL_KEYWORD.items():
            checked += 1
            if keyword not in pairs[status]:
                problems.append(
                    f'{js_name}: {status!r} -> {pairs[status]!r}，应含 {keyword!r}')
    assert checked == 12, f'只检查了 {checked} 组（期望 12）—— 本测试已失效'
    assert not problems, (
        '状态与文案的配对错了（界面会把失败写成已完成这种）：\n'
        + '\n'.join('  ' + p for p in problems)
    )


# ⚠️ 登记（2026-08 统一流式列表重设计）：这里原本是
# test_status_icons_are_real_distinct_glyphs —— 检查两个文件渲染函数里
# statusIcons 表的值（非空、是 SVG、六个互不相同，守 WCAG 1.4.1
# 「不只靠颜色区分状态」）。定稿设计废掉徽章 pill（状态点 + 小字状态文本
# 承担状态识别），statusIcons 表随之从 createTaskRow / renderHistoryTable
# 删除，该断言失去检查对象，整条删除。
# 「不只靠颜色」的职责没有丢，由两条一起接住：
#   · 图形侧（六态状态点配色 + 对比度）：
#     tests/test_css_contract.py::test_task_row_status_dot_covers_every_status
#   · 文字侧（小字状态文本必须来自 getStatusText 的中文词表）：
#     本文件 test_status_labels_are_paired_with_the_right_status 等 A7 断言
# 与图形侧的区别说明：状态点比 SVG 图标信息量低，所以活动行行1 与历史行
# 行2 都带 getStatusText 的状态文字，颜色不再是唯一通道。

def test_map_rectangle_stroke_covers_every_status():
    """历史地图矩形的描边色是**第四处**状态映射点，同样要覆盖六态、走调色板令牌。

    评审找到的漏网：改前 `renderHistoryMap` 里是一条内联三元阶梯，
    只认 completed / failed，其余四态（pending / running / paused / cancelled）
    全折叠成同一个蓝色 —— 与徽章那三张表是完全同型的缺陷，只是发生在第四处。

    而且三个色号是**硬编码且离调色板**的：`#10b981` 是 emerald-500，
    本项目的 `--color-success` 是 emerald-400 `#34d399`，改调色板时这里会静默漂移。

    这条同时守两件事：六态全覆盖、且每一态指向正确的语义令牌（配对，不只是集合）。
    """
    src = _strip_js_comments(_js('history.js'))
    body = _js_function_body(src, 'getStatusStroke')
    pairs = dict(re.findall(r"'([a-z_]+)'\s*:\s*'(--[-\w]+)'", body))
    assert pairs, 'getStatusStroke 里解析不出 {状态: 令牌} 映射 —— 本测试已失效'
    fallback = re.findall(r"\|\|\s*'(--[-\w]+)'", body)
    assert len(fallback) == 1, (
        f"getStatusStroke 的 `|| '--...'` 兜底解析出 {fallback} —— 本测试已失效"
    )
    assert set(pairs) == set(_STATUS_STROKE_TOKEN), (
        f'getStatusStroke 覆盖 {sorted(pairs)}，期望 {sorted(_STATUS_STROKE_TOKEN)}'
    )
    problems = [
        f'{s!r} -> {pairs[s]}，期望 {want}'
        for s, want in _STATUS_STROKE_TOKEN.items() if pairs[s] != want
    ]
    assert not problems, (
        '地图矩形描边的状态映射错了：\n' + '\n'.join('  ' + p for p in problems)
    )
    # 渲染函数里不许再出现硬编码色号 —— 那是改调色板时静默漂移的入口。
    render = _js_function_body(src, 'renderHistoryMap')
    hardcoded = re.findall(r'#[0-9a-fA-F]{3,8}\b', render)
    assert not hardcoded, (
        f'renderHistoryMap 里还有硬编码色号 {hardcoded} —— 描边色必须走 getStatusStroke'
    )
    assert 'getStatusStroke(' in render, (
        'renderHistoryMap 没有调用 getStatusStroke —— 映射表写了但没人用'
    )


# --------------------------------------------------------------------------
# LOW 批次（2026-07-31 code-only review，前端杂项）
# --------------------------------------------------------------------------

def test_start_pause_resume_surface_server_error_reason():
    """start/pause/resume 非 2xx 时必须透出服务端 error，与 cancelTask 同口径。

    改前三个函数都是 `throw new Error('启动任务失败')`，后端给的具体原因
    （如"任务已在运行"）被整个丢掉，用户只看到一句套话。
    """
    for fn in ('startTask', 'pauseTask', 'resumeTask'):
        body = _fn(fn)
        assert 'result.error' in body, (
            f'{fn} 没有读响应体里的 result.error——服务端错误原因被丢弃，'
            '应与 cancelTask 的 `result.error || response.status` 对齐'
        )


def test_upload_contour_task_shows_render_phase_not_fake_download():
    """上传来源的等高线任务（dataset='upload'）没有下载阶段，必须按渲染阶段显示。

    后端创建上传任务时 downloaded_files == total_files（上传即落盘），
    contourPhaseCounts 回落到文件计数会让 pending 任务一出现就显示
    100%「下载 DEM」。判定口径与后端 is_upload 一致（dataset == "upload"，
    见 services/contour_task_manager.py）。
    """
    body = _fn('contourPhaseCounts')
    assert "task.dataset === 'upload'" in body, (
        "contourPhaseCounts 没有识别 dataset='upload' 的上传来源任务——"
        "pending 状态会显示 100%「下载 DEM」"
    )


def test_time_info_falls_back_when_total_running_seconds_missing():
    """dem/contour/local_terrain 的 manager 不写 total_running_seconds：
    字段缺失时必须回退按 started_at 算墙钟时长，而不是恒显示 0 秒。"""
    body = _fn('calculateTimeInfo')
    assert re.search(r'task\.total_running_seconds\s*!=\s*null', body), (
        'calculateTimeInfo 没有区分「字段缺失」与「累计 0 秒」——'
        'dem/contour 任务的已运行时间会恒显示 0秒'
    )
    assert 'parseTaskDate(task.started_at)' in body, (
        '缺 total_running_seconds 时应回退用 started_at 计算已运行时间'
    )


def test_unknown_status_never_renders_raw_english_literal():
    """getStatusText 的兜底不许把未知英文状态原样渲染进中文界面。

    已知六态由词表覆盖（见上面的 A7 断言）；词表外的状态统一显示「未知」，
    与 A7 修过的中英混杂问题保持同一方案。
    """
    for js_name in ('tasks.js', 'history.js'):
        body = _fn('getStatusText', js_name)
        assert not re.search(r"\|\|\s*status\b", body), (
            f'{js_name} 的 getStatusText 仍用 `|| status` 兜底——'
            '未知英文状态会被原样渲染进中文界面'
        )
        assert "'未知'" in body, (
            f"{js_name} 的 getStatusText 未知状态应统一显示 '未知'"
        )


def test_delete_last_item_on_page_steps_back_a_page():
    """删掉当前页最后一条记录后必须回退一页，而不是停在空白页。"""
    body = _fn('deleteTask', 'history.js')
    assert re.search(r'loadHistory\(\s*currentPage\s*-\s*1\s*\)', body), (
        'deleteTask 没有页码回退——删除当前页最后一条后用户会看到空白页'
    )
    assert 'currentPage > 1' in body, (
        '页码回退必须以 currentPage > 1 为条件，第 1 页无处可退'
    )


def test_history_map_uses_configured_tile_server_with_osm_fallback():
    """历史小地图底图必须与主视图同源（tile_servers 第一条），OSM 只做回退。

    改前硬编码 https://tile.openstreetmap.org/...，与「断网/内网可用」的
    定位矛盾：主视图走内网瓦片正常，历史小地图白屏。
    """
    src = _strip_js_comments(_js('history.js'))
    body = _js_function_body(src, 'initHistoryMap')
    assert 'https://tile.openstreetmap.org' not in body, (
        'initHistoryMap 仍硬编码外网 OSM 底图'
    )
    assert 'tile_servers' in src, (
        'history.js 没有读 tile_servers 配置——历史小地图与主视图不同源'
    )
    fb = _js_function_body(src, '_historyBaseMapUrl')
    assert 'https://tile.openstreetmap.org/{z}/{x}/{y}.png' in fb, (
        '_historyBaseMapUrl 必须保留 OSM 回退（拿不到配置时小地图仍要能用）'
    )


def test_panel_close_guard_compares_elements_not_names():
    """panels.js 关闭守卫必须按元素引用比较，不能按面板名。

    records/history 是同一个面板元素（PANELS 别名）。按名字比较时
    openPanel('records') → openPanel('history') 的延迟 done 回调会把
    刚重开的共享面板重新 hidden。
    """
    src = _strip_js_comments(_js('panels.js'))
    body = _js_function_body(src, 'closePanel')
    assert 'panelEl(current) !== el' in body, (
        'closePanel 的 done 守卫应按元素引用比较（panelEl(current) !== el）'
    )
    assert 'current !== closing' not in body, (
        'closePanel 的 done 守卫仍按面板名比较——records/history 同元素会误藏'
    )


# --------------------------------------------------------------------------
# 统一流式列表（2026-08 重设计定稿）
#
# 前身是「统一任务表（2026-07）」一节：一张 9 列表、两个 tbody。用户连续
# 反馈「太乱」后整体重设计——废掉 9 列 .task-table 与表头，活动/历史任务
# 共用同一种行结构（状态点 + 名称 + #类型:id + 元信息 …… 时间 + 动作组；
# 行2 按状态变体：进度 / 引文式错误 / 单行摘要）。
# 本节的锚点随之从「表格结构」翻面为「列表结构」，每条都登记了翻面理由。
# --------------------------------------------------------------------------

def test_active_tasks_render_into_the_records_panel_list():
    """tasks.js 必须渲染进 #activeTasksBody（记录面板列表顶部的实时区容器）。

    （前身 test_active_tasks_render_into_the_shared_table_tbody：
    容器从 <tbody> 变成 <div>，「空态是 colspan=9 的行」整条翻面——
    定稿设计里无活动任务时实时区整个留空，不渲染「暂无活动任务」。）
    """
    body = _fn('renderActiveTasks')
    assert "getElementById('activeTasksBody')" in body, (
        'renderActiveTasks 没有以 #activeTasksBody 为容器'
    )
    assert "getElementById('activeTasks')" not in body, (
        'renderActiveTasks 仍在渲染旧的 #activeTasks 卡片区'
    )
    assert 'colspan' not in body and '<tr' not in body and '<td' not in body, (
        'renderActiveTasks 还在产出表格行——9 列 .task-table 已随统一流式列表废除'
    )
    assert re.search(r"innerHTML\s*=\s*''", body), (
        '无活动任务时实时区必须整个留空（定稿设计：不显示「活动」分组头、'
        '也没有独立的活动空态）'
    )

    src = _strip_js_comments(_js('tasks.js'))
    row_body = _js_function_body(src, 'createTaskRow')
    assert 'task-row status-' in row_body, (
        'createTaskRow 必须产出带状态类的 <div class="task-row status-*">'
    )
    assert '<tr' not in row_body and '<td' not in row_body, (
        'createTaskRow 还在产出 <tr>/<td>——统一流式列表里没有表格'
    )
    err_body = _js_function_body(src, 'createTaskErrorRow')
    assert 'task-error-row' in err_body, (
        '失败任务的引文式错误行（.task-error-row）没了'
    )
    assert '<tr' not in err_body and '<td' not in err_body and 'colspan' not in err_body, (
        'createTaskErrorRow 还在产出横跨 9 列的表格行'
    )


def test_task_row_is_the_unified_two_line_structure():
    """活动任务行与历史任务行是同一种「统一流式行」结构。

    （前身 test_active_task_row_is_a_single_rich_cell_with_three_lines：
    富行 = colspan=9 单格 + 三行 flex。2026-08 定稿改为两行，且历史行
    用同一结构——「两种行语言硬拼」是「太乱」的根因之一。）

    这条守两件事：
      1. 结构锚点存在（行1 = task-line1：状态点/名称/#类型:id/元信息/
         状态小字/时间；行2 = task-progress-line：发丝条/百分比/计数）；
      2. 增量更新依赖的稳定类名没有改名——updateTaskProgressPartial /
         updateTimeDisplay 按 .progress-bar/.task-pct/.task-count/.task-time
         原地更新，模板里类名一换，Socket.IO 刷新就静默失效而本文件其它
         断言未必红。
      3. 徽章 pill 不再存在（状态识别 = 状态点 + 小字状态文本）。
    """
    body = _fn('createTaskRow')
    for anchor in ('task-line1', 'task-dot', 'task-name', 'task-id',
                   'task-meta', 'task-status-text', 'task-progress-line'):
        assert anchor in body, (
            f'createTaskRow 缺 {anchor} —— 统一流式行的结构锚点（见 docstring）'
        )
    for cls in ('task-pct', 'task-count', 'task-time', 'progress-bar'):
        assert cls in body, (
            f'createTaskRow 缺稳定类名 {cls} —— Socket.IO 增量更新按它定位元素'
        )
    assert 'badge' not in body, (
        'createTaskRow 还在渲染徽章 pill——定稿设计的状态识别是状态点 + 状态小字'
    )

    # 历史行必须说同一种行语言（history.js 独立渲染，最容易走偏的就是它）
    hist_body = _fn('createHistoryRow', 'history.js')
    for anchor in ('task-row status-', 'task-line1', 'task-dot', 'task-name',
                   'task-id', 'task-meta', 'task-time'):
        assert anchor in hist_body, (
            f'createHistoryRow 缺 {anchor} —— 历史行必须与活动行同一种结构'
        )
    for forbidden in ('<tr', '<td', 'colspan', 'badge'):
        assert forbidden not in hist_body, (
            f'createHistoryRow 里还有 {forbidden} —— 历史行退回 9 列网格/徽章形态了'
        )


def test_failed_group_collapses_beyond_five_and_remembers_the_choice():
    """失败组治理（「太乱」根因之二：dev 库 96 个 failed 任务钉在列表顶部
    形成红墙）：>5 个时默认折叠只显示最近 3 个，分组头可点击展开全部，
    折叠态用 sessionStorage 记忆；≤5 个时全部显示、分组头不可折叠。
    """
    src = _strip_js_comments(_js('tasks.js'))
    body = _fn('renderActiveTasks')
    assert 'FAILED_GROUP_COLLAPSE_THRESHOLD' in body, (
        'renderActiveTasks 没有失败组折叠阈值——96 个失败任务又会全部铺开成红墙'
    )
    assert 'FAILED_GROUP_PREVIEW_COUNT' in body and '.slice(0,' in body.replace(' ', ''), (
        '折叠时没有「只显示最近 3 个」的裁剪（slice + FAILED_GROUP_PREVIEW_COUNT）'
    )
    assert 'toggleFailedTaskGroup()' in body, (
        '失败分组头不可点击展开——>5 个时剩下的失败任务永远看不到'
    )
    # 阈值与预览数的字面值钉死（常量被改成 999 的话上面两条照样绿）
    assert re.search(r'FAILED_GROUP_COLLAPSE_THRESHOLD\s*=\s*5', src), (
        'FAILED_GROUP_COLLAPSE_THRESHOLD 不是 5——定稿是「>5 个才折叠」'
    )
    assert re.search(r'FAILED_GROUP_PREVIEW_COUNT\s*=\s*3', src), (
        'FAILED_GROUP_PREVIEW_COUNT 不是 3——定稿是「折叠时只显示最近 3 个」'
    )

    toggle_body = _fn('toggleFailedTaskGroup')
    assert 'sessionStorage' in toggle_body, (
        '折叠态没有用 sessionStorage 记忆——每次重绘都回到默认折叠'
    )
    assert 'renderActiveTasks(' in toggle_body, (
        '展开/收起后没有重绘实时区'
    )


def test_history_stream_dedups_exactly_against_active_tasks():
    """精确去重契约：历史流排除「当前在 activeTasks Map 里的任务」
    （按 `类型:id` 键比对），且只在文档里有 #activeTasksBody 时启用。

    这替代旧的「按状态跳过 pending/running/paused」规则（2026-08 定稿）：
    旧规则会把从未进过实时区的历史非终态任务也一并藏掉，而失败任务
    又在实时区和历史区各出现一次。新规则下：活动区/失败组里显示着的，
    历史区就不显示；dismiss 后下次刷新才在历史出现。
    独立页 /history 不加载 tasks.js（activeTasks 未定义）也没有
    #activeTasksBody，必须照旧全量渲染——所以两个前提条件缺一不可。
    """
    body = _fn('renderHistoryTable', 'history.js')
    assert "getElementById('activeTasksBody')" in body, (
        'renderHistoryTable 没有检查 #activeTasksBody 是否存在——'
        '独立页 /history 的行为无法与首页区分'
    )
    assert 'activeTasks' in body, (
        'renderHistoryTable 没有读 tasks.js 的全局 activeTasks Map——精确去重没了'
    )
    assert re.search(r'activeTasks\.has\(\s*`\$\{t\.task_type\}:\$\{t\.id\}`\s*\)', body), (
        '去重不是按 `task_type:id` 键查 activeTasks——比对键变了会整批误伤/漏排'
    )
    assert re.search(r'\.filter\s*\(', body), (
        'renderHistoryTable 没有 filter——去重逻辑没了'
    )
    # 旧的「按状态跳过」规则必须消失（failed 不再被历史区无条件放行、
    # pending/running/paused 不再被无条件跳过）
    assert "['pending', 'running', 'paused']" not in body, (
        '旧的「按状态跳过 pending/running/paused」规则还在——与精确去重是两套逻辑'
    )


def test_status_chips_filter_only_the_history_stream():
    """状态筛选 chips：只作用于历史流——loadHistory 把取值透传给
    /api/history_all 的 ?status= 参数；chips 点击后回到第 1 页刷新。
    """
    src = _strip_js_comments(_js('history.js'))
    load_body = _js_function_body(src, 'loadHistory')
    assert 'currentStatusFilter' in load_body and '&status=' in load_body, (
        'loadHistory 没有把 chips 取值透传成 ?status= 参数——chips 成了摆设'
    )
    init_body = _js_function_body(src, 'initHistory')
    assert '#statusChips .status-chip' in init_body, (
        'initHistory 没有给 #statusChips .status-chip 挂点击事件'
    )
    assert re.search(r'loadHistory\(\s*1\s*\)', init_body), (
        '切换 chip 后没有 loadHistory(1)——筛选不会生效/停留在旧页码'
    )


def test_history_error_text_only_lands_via_textcontent():
    """失败历史行的错误原文与活动失败行同一条规矩：不进 innerHTML 模板。

    /api/history_all 返回的 error_message 同样是后端异常的字符串化结果。
    createHistoryRow 只吐一个**空**的 .task-error 容器（连 error_message
    都不引用），文本由 renderHistoryTable 在渲染后用 textContent 补。
    """
    src = _strip_js_comments(_js('history.js'))
    row_body = _js_function_body(src, 'createHistoryRow')
    assert 'error_message' not in row_body, (
        'createHistoryRow 里出现了 error_message——它的返回值会进 innerHTML，'
        '后端错误原文里的 HTML 会被当标签解析（XSS）'
    )
    render_body = _js_function_body(src, 'renderHistoryTable')
    assert 'error_message' in render_body and re.search(r'\.textContent\s*=', render_body), (
        'renderHistoryTable 没有在渲染后用 textContent 回填失败历史行的错误文本'
    )
    assert 'innerHTML' not in render_body.split('forEach')[1] if 'forEach' in render_body else True, (
        '错误文本回填走了 innerHTML——必须走 textContent'
    )


def test_pagination_bar_is_hidden_when_only_one_page():
    """总页数 <= 1 时不渲染分页条（孤零零一个「1」按钮没有交互价值）。

    （2026-08 重排配套：实测只有 1 页历史时分页区仍显示一个「1」。）
    """
    body = _fn('renderPagination', 'history.js')
    assert re.search(r'totalPages\s*<=\s*1', body), (
        'renderPagination 没有「<= 1 页直接返回」的分支——单页时会显示孤按钮'
    )
    assert re.search(r"innerHTML\s*=\s*''", body), (
        'renderPagination 的单页分支没有清空分页条'
    )


# ⚠️ 登记（2026-08 统一流式列表重设计）：这里原本是
# test_history_table_skips_non_terminal_rows_only_when_active_tbody_exists
# ——守旧的「有 #activeTasksBody 时按状态跳过 pending/running/paused」
# 去重规则。定稿设计把它换成**精确去重**（按 activeTasks 的 `类型:id`
# 键排除实时区里正显示着的任务），该断言整条删除，
# 接替者是上面的 test_history_stream_dedups_exactly_against_active_tasks。

def test_completed_task_refreshes_initialized_history_panel():
    """task_completed 删掉实时行之后，必须让任务立刻出现在历史区。

    不刷新的后果：实时行删了、历史表还停在旧数据，完成的任务在界面上
    「凭空消失」，要等用户手动翻页/重开面板才回来。
    但只在记录面板的历史已初始化过时才刷（historyViewer 存在或历史表已有
    内容）——面板从没打开过的话，打开时 initHistory 本来就会拉最新数据。
    """
    body = _fn('handleTaskCompleted')
    assert re.search(r'loadHistory\(\s*1\s*\)', body), (
        'handleTaskCompleted 没有调 loadHistory(1)——完成的任务不会立刻出现在历史区'
    )
    assert re.search(r'loadStats\(\s*\)', body), (
        'handleTaskCompleted 没有调 loadStats()——统计卡还是旧数字'
    )
    assert 'historyViewer' in body and 'historyTableBody' in body, (
        '刷新必须以「历史已初始化过」为条件（historyViewer 或 #historyTableBody），'
        '无条件刷新会在面板从未打开时白打两路接口'
    )
