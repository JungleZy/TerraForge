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
    'tasks.js': 2,      # createTaskCard 的模板 + updateTaskProgressPartial 的 className
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
    createTaskCard 只在状态切换时才重建卡片。
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


def test_failed_task_card_is_not_removed():
    """任务失败时不许删卡片、也不许把任务从 activeTasks 里摘掉。

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

    覆盖范围（诚实说明）：这条守的是源码形态。它保证不了「卡片在浏览器里
    真的还在」——那由 CDP 实测覆盖（见 p2-task-6-report.md）。
    """
    body = _fn('handleTaskFailed')

    assert 'card.remove()' not in body, (
        'handleTaskFailed 仍在删除卡片，用户看不到失败原因'
    )
    assert not re.search(r'\.remove\(\s*\)', body), (
        'handleTaskFailed 里出现了 .remove() 调用——失败卡片必须留在页面上，'
        '清理只能由用户点「移除」触发（dismissTask）'
    )
    assert 'activeTasks.delete(' not in body, (
        'handleTaskFailed 仍把任务从 activeTasks 摘掉——下一次整体重绘'
        '（renderActiveTasks）就会让失败卡片再次凭空消失'
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
    报错原文都可能在里面）。它一旦被拼进 createTaskCard 的模板字符串，
    `container.innerHTML = ...` 就会把里面的 `<img onerror=...>` 当标签解析。
    ui.js 里同样的地方已经写了 `// textContent 防 XSS` 的注释，这里是同一条规矩。

    做法上让 createTaskCard 只吐一个**空**的 `.task-error` 容器
    （连 error_message 都不引用），文本由 applyTaskErrorText 单独填。
    所以这条可以直接断言「createTaskCard 里根本没有 error_message 这个词」。
    """
    src = _strip_js_comments(_js('tasks.js'))
    card_body = _js_function_body(src, 'createTaskCard')
    assert 'error_message' not in card_body, (
        'createTaskCard 里出现了 error_message——它的返回值会被塞进 innerHTML，'
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

    存在性契约。renderActiveTasks 是 `container.innerHTML = tasks.map(...)`，
    一次性重建全部卡片；失败卡片现在会留在 activeTasks 里，所以任何一个
    新任务到达都会触发重绘。漏掉这一步的话：失败当场看得见错误，之后
    随便来一个新任务，红框就变空了——而所有文本断言依然全绿。
    """
    render_body = _fn('renderActiveTasks')
    assert 'applyTaskErrorText' in render_body, (
        'renderActiveTasks 重建 innerHTML 之后没有回填错误文本，'
        '失败卡片的红框会在下一次整体重绘时被清空'
    )


# createTaskCard 里每个 onclick 动作，期望它**最近的前置**状态判断是哪一个。
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
    """卡片上每个动作按钮都必须挂在正确的状态分支下。

    实现方式：对 createTaskCard 体内每一处 `onclick="xxxTask(`，往前找
    **最近**的 `task.status === / !== '...'`，与 _ACTION_GUARDS 对表。

    这条同时守三件事：
      1. 失败卡片**没有**重试按钮（后端会抛 ValueError，见上面表里的注释）。
      2. 失败卡片**有**「移除」按钮。卡片不再自动消失了，没有这个按钮
         用户就没有任何办法清掉它，只能刷新页面。
      3. 失败卡片不再显示「取消」——对一个已经 failed 的任务调
         /cancel 是无意义的后端往返。

    已知弱点（诚实说明）：「最近的前置判断」是启发式的。若有人把按钮写成
    多层嵌套三元、或者用 `${cond ? ... : ...}` 之外的方式生成 onclick，
    这条可能失配。所以每个动作还额外断言了「至少出现一次」——完全找不到
    比匹配错更危险。
    """
    body = _fn('createTaskCard')
    problems = []
    for action, expected in _ACTION_GUARDS.items():
        hits = list(re.finditer(r'onclick="' + action + r'\(', body))
        if not hits:
            problems.append(f'{action}: createTaskCard 里一处调用都没有')
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
    assert not problems, '卡片动作按钮的状态门控不对：\n' + '\n'.join('  ' + p for p in problems)


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

# 两个文件里各有几处进度条模板。等号不是下限：多出来的一处大概率是
# 复制粘贴出来的第二套渲染路径，那正是本任务要防的「漏改一处出重复标签」。
PROGRESS_TRACK_MARKUP_SITES = {'tasks.js': 1, 'history.js': 1}


def test_percentage_is_an_overlay_not_a_child_of_the_bar():
    """百分比必须是 `.progress` 里的独立 `<span class="progress__label">`，
    `.progress-bar` 元素自己**不能再有任何文字内容**。

    这是本任务的结构契约，守三件事：

      1. **`.progress-bar` 标签之间是空的。** 数字留在里面的话，
         `.progress { overflow: hidden }` + 宽度为 0 会把它整个裁掉 ——
         CDP 实测 progress=0 时数字画出 0 个像素（截图差异法）。
      2. **每个 `.progress` 容器恰好一个 `.progress__label`。**
         0 个 = 这一处渲染点漏改了，百分比直接消失；
         2 个 = 同一条进度条上两个百分比。
      3. **覆盖层必须是 `<span>` 不能是 `<div>`。** style.css 里有一条
         `div:not(.card):not(.modal-content)...{background:transparent}`
         兜底重置，特异度 (0,10,1)，会把**任何**不在白名单里的 div 背景压成
         透明。覆盖层的可读性正是靠自带的那块不透明底撑着（见
         test_progress_label_readability_does_not_depend_on_the_fill），
         改成 div 就会「源码里有底色、浏览器里透明」，而全部 CSS 断言照旧全绿
         —— Task 6 的 `.task-error` 就是这么被咬的。
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
    """Socket.IO 增量刷新路径必须改覆盖层的文字，且**不能**再往进度条里写文字。

    `updateTaskProgressPartial` 是任务跑起来之后真正在刷新界面的那条路
    （createTaskCard 只在状态切换时重建整卡）。漏改这里的后果很具体：
    卡片初次渲染是对的（覆盖层里一个百分比），第一个 task_progress 事件一到，
    `progressBar.textContent = '37%'` 又在条里塞回一个 —— 同一条进度条上
    出现**两个**百分比，而所有只看模板的断言依然全绿。
    """
    body = _fn('updateTaskProgressPartial')

    assert re.search(r"querySelector\(\s*'\.progress__label'\s*\)", body), (
        'updateTaskProgressPartial 没有取 .progress__label —— '
        '进度数字在 Socket.IO 刷新时不会更新（会一直停在初次渲染的值）'
    )
    assert re.search(r'\.textContent\s*=\s*`\$\{progress\}%`', body), (
        'updateTaskProgressPartial 没有把 `${progress}%` 写进覆盖层'
    )
    assert not re.search(r'progressBar\.textContent\s*=', body), (
        'updateTaskProgressPartial 仍在给 progressBar.textContent 赋值 —— '
        '覆盖层 + 条内文字会同时存在，同一条进度条上出现两个百分比'
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
