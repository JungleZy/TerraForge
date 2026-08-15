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
#
# 登记（2026-08 Vue 化）：行渲染从 history.js 的 createTaskRow 收口到
# task_list.js 的 TaskRow 组件，模板插值 `bg-${getStatusColor(...)}` 随之
# 变成响应式绑定 `:class="'bg-' + statusColor"`（statusColor 是 computed）。
#   tasks.js   1 -> 0：updateTaskProgressPartial 整个删除，增量写 DOM 由
#              Vue diff 接管，不再有任何 className 字符串拼接。
#   history.js 2 -> 1：只剩 viewTaskDetails 详情模态框那处。
# 行2 那条发丝进度条的配色契约改由 test_progress_bar_color_comes_from_status
# 的组件分支来守（绑定 + computed 两端都钉）。
PROGRESS_BAR_CALL_SITES = {
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
    # task_center.js 一并扫：实时链搬过去之后，只扫 tasks.js 等于放过了
    # 现在真正持有那批代码的文件（详见 _fn 的登记）。
    for name in ('tasks.js', 'task_center.js', 'history.js'):
        src = _strip_js_comments(_js(name))
        assert 'function getProgressColor(' not in src, (
            f'{name} 仍定义 getProgressColor()，应改用 getStatusColor(status)'
        )
        assert 'getProgressColor(' not in src, f'{name} 仍有 getProgressColor() 调用'


def test_progress_bar_color_comes_from_status():
    """进度条配色必须由**状态**决定，不是按百分比。

    这条是 J 条要求补的**存在性**契约，守三件事：

      1. 调用点没被删掉（每个文件的处数不低于 PROGRESS_BAR_CALL_SITES）。
      2. 插值里确实调的是 getStatusColor，不是别的什么函数或裸变量。
         history.js 原来就是裸变量 `bg-${progressColor}`（一个本地算出来的
         百分比阶梯），只断言「没有 getProgressColor( 字样」的话它照样全绿。
      3. 传进去的是 task.status 而不是 progress——`getStatusColor(progress)`
         能编译、能跑，返回永远是兜底的 'secondary'，肉眼看是一条灰条。

    Vue 化后行2 那条发丝条走响应式绑定，两端分开钉：模板里必须是
    `:class="'bg-' + statusColor"`，statusColor computed 里必须是
    `getStatusColor(this.task.status)`。少任何一端都会让配色悄悄退回默认。
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

    tpl = _tpl('TaskRow')
    if not re.search(r""":class="'bg-'\s*\+\s*statusColor\"""", tpl):
        problems.append(
            "TaskRow 模板里的 .progress-bar 没有绑定 :class=\"'bg-' + statusColor\""
            '——进度条配色会退回 Bootstrap 默认')
    comp = _js('task_list.js')
    if not re.search(r'statusColor\(\)\s*\{\s*return getStatusColor\(this\.task\.status\)',
                     _strip_js_comments(comp)):
        problems.append(
            'statusColor computed 不是 getStatusColor(this.task.status)——'
            '传 progress 进去能跑，但返回永远是兜底的 secondary，是一条灰条')
    assert not problems, '进度条配色调用点不合契约：\n' + '\n'.join('  ' + p for p in problems)


def test_socketio_incremental_path_is_wired():
    """Socket.IO 增量刷新必须真的能改到进度条。

    改造前这里点名钉 updateTaskProgressPartial 里那行
    `progressBar.className = \\`progress-bar bg-${getStatusColor(task.status)}\\``
    ——它是任务跑起来之后真正决定颜色的那一行，漏改的话页面初次渲染正常、
    一跑起来颜色就错。

    Vue 化后那行 DOM 写入不存在了，链路变成
    `socket → updateTaskProgress → TaskStore.commit → 响应式 → 组件重渲染`。
    钉这条链的两个端点：推送处理器必须把结果写进 store（而不是又去摸 DOM），
    模板里的进度条必须绑在响应式数据上。

    ⚠️ 登记（2026-08 phase-3 §6.1）：实时链搬进 task_center.js（见 _fn 的
    登记），所以「不许写 HTML」这条禁令要同时压住两个文件 —— 只扫 tasks.js
    的话，把整行 `row.innerHTML = ...` 写回 task_center.js 依然全绿。
    """
    src = ''.join(_strip_js_comments(_js(n))
                  for n in ('tasks.js', 'task_center.js'))
    body = _fn('updateTaskProgress')
    assert 'TaskStore.commit(' in body, (
        'updateTaskProgress 没有把推送写进 TaskStore —— Socket.IO 增量刷新断链'
    )
    assert 'getElementById' not in body and 'querySelector' not in body, (
        'updateTaskProgress 又开始直接摸 DOM 了 —— 渲染归组件，这里只写数据。'
        '双写正是 Vue 化要消灭的东西'
    )
    assert '.innerHTML' not in src and '.outerHTML' not in src, (
        'tasks.js / task_center.js 又出现了 innerHTML/outerHTML —— 任务行的'
        '渲染必须留在 task_list.js 的组件里'
    )
    tpl = _tpl('TaskRow')
    assert ':style="{ width: progress + \'%\' }"' in tpl, (
        '进度条宽度不再绑定 progress —— 增量刷新推不动它'
    )


def test_status_map_lives_in_exactly_one_file():
    """getStatusColor / getStatusText 只能有**一份**定义，在 task_status.js。

    改前这条测试要求 tasks.js 与 history.js **各有一份**，理由写的是
    「两个页面不会同时加载」—— 那个前提是错的：index.html 的 extra_js 把
    map.js / tasks.js / history.js / config.js / panels.js 全部一起加载，
    共享同一个全局作用域，后加载的 history.js 静默遮蔽 tasks.js 那份。
    两份 getStatusText 查的还是不同的 i18n 前缀（js.tasks.status.* vs
    js.history.status.*），改任一份在首页都不生效。

    所以现在反过来钉：唯一实现在 task_status.js，两个业务文件都不许再长出
    自己的那一份（第二份必然与第一份漂移，而遮蔽是静默的）。
    """
    canonical = _strip_js_comments(_js('task_status.js'))
    for fn in ('getStatusColor', 'getStatusText', 'getStatusStroke'):
        assert f'function {fn}(' in canonical, (
            f'task_status.js 没有定义 {fn}() —— 调用点会抛 ReferenceError'
        )
    for name in ('tasks.js', 'task_center.js', 'history.js'):
        src = _strip_js_comments(_js(name))
        for fn in ('getStatusColor', 'getStatusText', 'getStatusStroke'):
            assert f'function {fn}(' not in src, (
                f'{name} 又长出了第二份 {fn}() —— 首页两文件同时加载，'
                '后者会静默遮蔽 task_status.js 那份，两份必然漂移'
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

def _fn(name, js_name='task_center.js'):
    """先剥注释、再按花括号配对切出函数体。

    顺序不能反：注释里出现的 `{` / `}` 会把花括号配对带偏（本任务的
    handleTaskFailed 注释里就写了「不 card.remove()」这类字样，不剥注释
    的话负向断言还会被自己的解释性注释误伤——test_css_contract.py 里
    的同类断言已经踩过两次这个坑）。

    ⚠️ 登记（2026-08 phase-3 §6.1 任务中心抽取）：默认文件从 tasks.js 改成
    task_center.js。tasks.js 曾经是 749 行、装着整条实时链，但它挂在
    index.html 的 extra_js 里 —— **首页专属**，于是 /history 上同一批任务
    没有按钮、没有实时更新。修法是把与首页那张地图无关的一切搬进
    static/js/task_center.js（base.html 全局加载），函数名与函数体一个字
    没改。tasks.js 现在只剩 initTasks / updateStatusTasks / pushStatusEvent
    三个**只有首页 DOM 才有意义**的函数。

    默认值钉的是 task_center.js 而不是「两个文件里随便哪个」：本文件用默认
    值切的每一个函数（handleTaskFailed / updateTaskProgress / prependStreamRow
    / startTask …）守的都是**实时层**的行为契约。允许它在 tasks.js 里也算数
    的话，这些断言就退化成「代码在前端某处」—— 那种断言无论把实现搬到哪个
    文件都是绿的，等于不再守任何东西。
    """
    body = _js_function_body(_strip_js_comments(_js(js_name)), name)
    assert body.strip(), f'{js_name} 的 {name} 函数体切出来是空的 —— 本测试已失效'
    return body


# Vue 化（2026-08）后行 markup 不再是「一个叫 createTaskRow 的函数返回的模板
# 字符串」，而是 task_list.js 里的 `const XXX_TEMPLATE = \`...\`` 常量，由
# TaskRow / TaskList 组件的 template 字段消费。本文件原先那批「切
# createTaskRow 函数体再逐字 grep」的断言，钉点整体迁到这里 —— 守的契约一条
# 没少（类名、动作的状态门控、转义、进度条 aria），只是 markup 换了个出处。
_TEMPLATE_CONSTS = {
    'TaskRow': 'ROW_TEMPLATE',
    'empty': 'EMPTY_TEMPLATE',
    'error': 'ERROR_TEMPLATE',
}


def _tpl(name='TaskRow', js_name='task_list.js'):
    """切出组件模板常量的内容。

    刻意**不剥 JS 注释**：模板体是 HTML，`_strip_js_comments` 会把
    `<path d="M19 6v14a2 2 0 0 1-2 2H7..."/>` 这类路径数据里的 `//` 误当成
    行注释吃掉半个模板。
    """
    const = _TEMPLATE_CONSTS[name]
    src = _js(js_name)
    m = re.search(const + r'\s*=\s*`(.*?)`;', src, re.S)
    assert m, f'{js_name} 里找不到模板常量 {const} —— 本测试已失效（不是通过）'
    body = m.group(1)
    assert body.strip(), f'{const} 切出来是空的 —— 本测试已失效'
    return body


def test_failed_task_row_is_not_removed():
    """任务失败时不许删行、也不许把任务从 activeTasks 里摘掉。

    （前身是 test_failed_task_card_is_not_removed。统一任务表改版把卡片改成
    表格行，契约不变：失败行必须留在页面上——转红 + 错误行，
    清理只能由用户点「删除」触发（deleteTask）。）

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
        '清理只能由用户点「删除」触发（deleteTask）。'
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
    """错误文本绝不能当 HTML 解析。

    error_message 是后端异常的字符串化结果（URL、文件路径、第三方库的
    报错原文都可能在里面）。改造前的做法是让行模板只吐一个**空**的
    `.task-error` 容器，文本事后用 textContent 填 —— 因为模板字符串最终
    会进 `container.innerHTML`，拼进去等于把 `<img onerror=...>` 当标签解析。

    Vue 化后这条路整个消失：模板用 `{{ errorText }}` 插值，Vue 自动 HTML
    转义。所以契约改成钉住「用的是插值不是 v-html」——**整个组件文件禁止
    出现 v-html**，那是唯一能把字符串当 HTML 塞进去的口子。
    """
    # 只扫模板体：v-html 只可能出现在模板里，而本文件的说明性注释里就写着
    # 「禁止出现 v-html」这句话 —— 扫全文会被自己的注释误伤（同样的坑在
    # test_css_contract 的 `.config-section .btn` 断言上踩过一次）。
    for tpl_name in _TEMPLATE_CONSTS:
        assert 'v-html' not in _tpl(tpl_name), (
            f'{tpl_name} 模板里出现了 v-html —— 它会把字符串当 HTML 解析，'
            '而任务名/错误原文都是不可信输入。一律用 {{ }} 插值（Vue 自动转义）'
        )
    tpl = _tpl('TaskRow')
    assert '{{ errorText }}' in tpl, (
        '失败行不再用 {{ errorText }} 插值渲染错误原文 —— 换成别的写法前'
        '先确认它同样会转义'
    )
    # 反向：整个 static/js 下不许再有人把 error_message 拼进 innerHTML。
    # task_center.js 在列：handleTaskFailed（error_message 的唯一消费者）
    # 就住在那里，漏掉它这条反向断言等于零轮空转。
    for name in ('tasks.js', 'task_center.js', 'history.js',
                 'task_list.js', 'task_store.js'):
        body = _strip_js_comments(_js(name))
        for m in re.finditer(r'\.innerHTML\s*=([^;]*);', body, re.S):
            assert 'error_message' not in m.group(1), (
                f'{name} 把 error_message 拼进了 innerHTML —— 后端错误原文里的 '
                'HTML 会被当标签解析（XSS）'
            )


def test_row_rebuild_refills_the_error_text():
    """状态跃迁后失败行的错误文本必须还在。

    改造前 socket 事件对行做的是 outerHTML 原地重建，重建出来的
    `.task-error` 是空容器，必须再调一次 applyTaskErrorText 回填 —— 漏掉
    这一步的话：失败当场看得见错误，之后随便一个进度事件触发重建，红框就
    变空了，而所有文本断言依然全绿。

    Vue 化后错误文本是 store 里的字段、模板里的插值，重建这个概念没了，
    契约相应变成：handleTaskFailed 必须把 error_message 写进 store，
    组件必须有兜底文案（空字符串会渲染成一个空红框，比没有红框更让人困惑）。
    """
    body = _fn('handleTaskFailed')
    assert 'error_message' in body and 'TaskStore.commit(' in body, (
        'handleTaskFailed 没有把 error_message 写进 store —— 失败行的红框会是空的'
    )
    comp = _strip_js_comments(_js('task_list.js'))
    assert re.search(r'errorText\(\)\s*\{[^}]*task\.error_message\s*\|\|', comp), (
        'errorText computed 没有兜底文案 —— 后端没给原因时会渲染一个空红框'
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
}

_STATUS_GUARD_RE = re.compile(r"task\.status\s*(===|!==)\s*'(\w+)'")


def test_card_actions_are_gated_by_the_right_status():
    """行上每个动作按钮都必须挂在正确的状态分支下。

    实现方式：对 TaskRow 模板里每一处 `@click="act('xxxTask')"`，在**同一个
    <button> 标签内**找它的 v-if，与 _ACTION_GUARDS 对表。改造前钉的是
    `onclick="xxxTask(`「往前找最近的 task.status 判断」那套启发式；Vue 的
    v-if 与按钮同在一个标签里，比「最近的前置判断」精确得多。

    这条同时守两件事：
      1. 失败行**没有**重试按钮（后端会抛 ValueError，见上面表里的注释）。
      2. 失败行的清理入口只剩 🗑（deleteTask，没有 v-if 所以恒在），
         它不走 act() 转发，因此不在这张表里。
    """
    tpl = _tpl('TaskRow')
    # 按 <button ...> 切标签，每个动作的 v-if 必须与它同标签
    buttons = re.findall(r'<button\b(.*?)>', tpl, re.S)
    assert buttons, 'TaskRow 模板里一个 <button> 都没有 —— 本测试已失效'
    problems = []
    for action, expected in _ACTION_GUARDS.items():
        owners = [b for b in buttons if f"act('{action}')" in b]
        if not owners:
            problems.append(f'{action}: TaskRow 模板里一处调用都没有')
            continue
        for b in owners:
            guards = _STATUS_GUARD_RE.findall(b)
            if not guards:
                problems.append(f'{action}: 按钮上找不到任何 task.status 判断（v-if 丢了？）')
                continue
            if expected not in guards:
                problems.append(
                    f'{action}: 按钮的状态门控是 {guards}，'
                    f"期望包含 task.status {expected[0]} '{expected[1]}'"
                )
    assert not problems, '行动作按钮的状态门控不对：\n' + '\n'.join('  ' + p for p in problems)


def test_failure_toasts_are_deduped_per_task_not_globally():
    """同一个任务的常驻 toast 只留一条，**不同任务的必须各留一条**。

    失败 toast 是 duration: 0 的，不会自己消失。等高线任务在下载阶段和渲染
    阶段各有一个失败出口（`src/services/contour_task_manager.py` 有 3 处
    `emit("task_failed")`），同一个 task 重复发事件会让永不消失的提示白白堆高。

    但**不能**退化成「全局只留一条」：8 个任务失败就是 8 个不同的原因，
    合并掉等于把前 7 条错误信息扔了。所以这里同时钉两件事：
      1. 合并逻辑存在（`closeFailureToast(key)` 在 set 之前被调用）；
      2. 合并的键是 `key`（taskType:taskId），不是常量、不是全局单例。
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
# task_center.js 同为 **0**：实时链搬过去之后它一行 DOM 都不写（渲染归
# task_list.js 的组件），漏掉它这条「0 处轨道」的期望就少压了一个文件。
PROGRESS_TRACK_MARKUP_SITES = {'tasks.js': 0, 'task_center.js': 0, 'history.js': 1}


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
        # tasks.js 一处 bar 模板都没有是**对的**（行渲染收口 history.js 后，
        # 它只剩 updateTaskProgressPartial 的 className 赋值，没有模板）；
        # history.js 必须至少有一处（createTaskRow 行2 发丝条 + 详情模态框），
        # 找不到说明解析失效或模板被删。
        if sites == 0:
            if bars:
                problems.append(
                    f'{name}: 找到 {len(bars)} 处 `<div class="progress-bar ...>` 模板，'
                    '期望 0 处——行渲染已收口 history.js，tasks.js 不该再有 bar 模板'
                )
        elif not bars:
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
    """百分比显示在条**外**，不在条里。

    漏改这里的后果很具体：行初次渲染是对的（条外一个百分比），第一个
    task_progress 事件一到，又往条里塞回一个 —— 同一条进度条上出现**两个**
    百分比，而所有只看模板的断言依然全绿。改造前这个坑在
    updateTaskProgressPartial（`progressBar.textContent = '37%'`）；Vue 化后
    模板只有一份，坑变成「有人往 .progress-bar 里加插值」。

    （统一任务表改版：百分比从 .progress 里的覆盖层 .progress__label
    改成紧凑条右边的 .task-pct——14px 的条装不下 18px 的 chip。
    守护的语义不变：更新的是条外文本，不是条。）
    """
    tpl = _tpl('TaskRow')
    assert '<span class="task-pct" aria-hidden="true">{{ progress }}%</span>' in tpl, (
        '条外百分比 .task-pct 不再渲染 {{ progress }}% —— 进度数字不会更新'
    )
    bar = re.search(r'<div class="progress-bar"(.*?)></div>', tpl, re.S)
    assert bar, 'TaskRow 模板里找不到 .progress-bar —— 本测试已失效'
    assert '{{' not in bar.group(0), (
        '.progress-bar 里出现了文本插值 —— 条外文本 + 条内文字会同时存在，'
        '同一条进度条上出现两个百分比'
    )


def test_progress_bar_keeps_a_programmatic_value_after_losing_its_text():
    """文字搬走之后，`role="progressbar"` 必须靠 aria-valuenow 报数值。

    改之前进度条元素里就是那串「37%」文本，屏幕阅读器至少还能读到点东西。
    文字搬到兄弟节点之后，`.progress-bar` 变成一个**空**的 progressbar，
    没有 aria-valuenow 就等于什么值都不报。

    Vue 化后 aria-valuenow 是响应式绑定（`:aria-valuenow="progress"`），
    它和视觉宽度绑的是同一个 computed，「视觉在涨、报给辅助技术的值停在
    初始值」这种分叉在结构上不再可能 —— 改造前那是两行独立的 DOM 写入。
    """
    problems = []
    # history.js 详情模态框里还有一处手写的 progress-bar
    for name in ('tasks.js', 'task_center.js', 'history.js'):
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

    tpl = _tpl('TaskRow')
    bar = re.search(r'<div class="progress-bar"(.*?)></div>', tpl, re.S)
    assert bar, 'TaskRow 模板里找不到 .progress-bar —— 本测试已失效'
    attrs = bar.group(1)
    if ':aria-valuenow="progress"' not in attrs:
        problems.append('TaskRow: aria-valuenow 没有绑定 progress computed')
    for attr in ('aria-valuemin="0"', 'aria-valuemax="100"', 'role="progressbar"'):
        if attr not in attrs:
            problems.append(f'TaskRow: .progress-bar 缺 {attr}')
    assert problems == [], (
        '空进度条没有可编程的数值：\n' + '\n'.join('  ' + p for p in problems))


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
#   - `/api/history_all`（src/routes/api.py 的四路 UNION ALL）**没有 status 谓词**，
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
# 词表的真值来自 src/models/task.py 的 TaskStatus 枚举（用 ast 解析，不 import，
# 避免 config.py 的导入副作用）。**不在测试里手抄一份六元组** —— 手抄的清单
# 会在有人给后端加状态时静默过期，而那正是这条断言唯一要拦的事。
# --------------------------------------------------------------------------

import ast  # noqa: E402


def _task_status_values():
    """从 src/models/task.py 的 TaskStatus 枚举解析出全部状态字面量。"""
    path = os.path.join(ROOT, 'src', 'models', 'task.py')
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
    raise AssertionError('src/models/task.py 里找不到 class TaskStatus —— 本测试已失效')


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
# 不要加进 TaskStatus：那会连带要求两个 JS 的状态词表覆盖一个永远到不了
# 任务徽章的状态。
_FILE_LEVEL_STATUSES = frozenset({'downloading', 'skipped'})
# 仅作查询过滤的伪状态（永远不会写进任务行）：?status=active 是路由/列表
# 接口的特殊筛选值（contracts.outcome.ACTIVE_STATE_VALUES 五态的并集），
# 管理器里只出现在 `status == 'active'` 比较与 `WHERE status IN (...)` 过滤
# 分支，不是任务状态。与 _FILE_LEVEL_STATUSES 同理：加进这里而不是 TaskStatus。
_FILTER_ONLY_STATUSES = frozenset({'active'})
# 瓦片级的结局词表（task_tiles.status 列，src/contracts/outcome.py:TileOutcome）。
# 与文件级同理：这些值不进任务徽章。
#
# 只列 'cancelled'：TileOutcome 的五个值（success / no_data / retryable_failure /
# permanent_failure / cache_failure）在管理器里一律走 `TileOutcome.X.value`，
# 不写字面量，正则扫不到；'cancelled' 是**唯一**的例外，因为它压根不是一个
# 结局 —— 引擎在取消出口对那块瓦片什么都不知道，回调里的
# `if status == 'cancelled': continue` 就是把它当作「这一轮不存在」。
# 不要把它加进 TaskStatus：任务级的 cancelled 已由
# database.migrate_cancelled_tasks_to_failed 迁成 failed，状态机里没有它。
_TILE_LEVEL_STATUSES = frozenset({'cancelled'})
# 每任务日志（src/services/task_logging）的事件字段值。`tlog.event('terminal',
# status='stopped', ...)` 写的是**日志行**里的一个 kv，不是任何一张表的 status
# 列 —— 'stopped' 的意思是「因为 stop_flag 提前退出，任务行的状态保持不变」，
# 正因为不动状态才需要在日志里记一笔。四个管理器都有这一句，所以只能按词表剔除。
_LOG_EVENT_STATUSES = frozenset({'stopped'})
_NON_TASK_STATUSES = (_FILE_LEVEL_STATUSES | _FILTER_ONLY_STATUSES
                      | _TILE_LEVEL_STATUSES | _LOG_EVENT_STATUSES)

_STATUS_LITERAL_RE = re.compile(
    r"""(?:SET\s+status\s*=\s*|(?<![-\w])status\s*(?:=|==|!=)\s*|['"]status['"]\s*:\s*)['"]([a-z_]+)['"]""",
    re.I,
)
_STATUS_IN_RE = re.compile(r"(?<![-\w])status\s+(?:NOT\s+)?IN\s*\(([^)]*)\)", re.I)
# 枚举成员引用：`TaskState.PENDING_DECISION.value`。
# 2026-08 §13-3 之后，管理器写状态的**主要**形态就是这个而不是字面量 ——
# `contracts.outcome.TaskState` 成了状态机的权威定义，四条管线一律引它。
# 只扫字面量的话，新加的三态（retrying / pending_decision /
# completed_with_gaps）一个都扫不到，下面的自检会当成「正则失效」。
_STATUS_MEMBER_RE = re.compile(r'(?<![-\w])TaskState\.([A-Z_]+)\.value')


def test_task_status_enum_covers_what_the_managers_actually_write():
    """四个管理器里出现的每一个状态取值都必须在 TaskStatus 枚举里。

    为什么需要这条：下面那条断言拿 TaskStatus 当真值，去要求 JS 的状态词表
    覆盖全部状态。可管理器不一定用这个枚举 —— 改造前它们直接写字符串字面量
    （`UPDATE dem_tasks SET status='paused' ...`），也就是说枚举本身可能是
    过期的，那样上面那条断言就是在拿一份不完整的清单验收「全覆盖」，全绿而漏。
    这条把数据源那一端接上。

    ⚠️ 登记（2026-08 §13-3 缺块状态机）：管理器写状态的主要形态从字面量变成
    `TaskState.X.value`（`src/contracts/outcome.py` 成了权威状态机，带转换
    规则）。两种形态都要扫：
      · 只扫字面量 —— 新增的 retrying / pending_decision / completed_with_gaps
        一个都扫不到，自检 `len(found) >= len(enum)` 反而会把「管理器已经全部
        改用枚举」这件好事误判成「正则失效」；
      · 只扫枚举成员 —— 三个老管理器里剩下的裸字面量（`status='paused'`）
        重新逃出检查。

    并且直接钉住两张表的一致：`TaskStatus`（models 层，只服务
    `Task.__post_init__` 的成员检查）必须与 `TaskState`（contracts 层，权威）
    取值相同。models/task.py 的 docstring 写的就是「两处的取值必须一致，由
    TaskState 的定义为准」—— 那句话此前没有任何机器检查。
    """
    from src.contracts.outcome import TaskState

    enum_values = _task_status_values()
    assert enum_values == {s.value for s in TaskState}, (
        f'TaskStatus（models）= {sorted(enum_values)} 与 TaskState（contracts）'
        f'= {sorted(s.value for s in TaskState)} 不一致。权威在 contracts，'
        '照它改 src/models/task.py —— 两张表一漂移，Task.__post_init__ 会拒绝'
        '一个状态机认可的状态，或者放行一个它不认的。'
    )
    found = {}
    for fn in _STATUS_WRITERS:
        path = os.path.join(ROOT, 'src', 'services', fn)
        assert os.path.exists(path), f'{fn} 不存在 —— 本测试已失效（管理器改名/搬家了？）'
        with open(path, encoding='utf-8') as f:
            src = f.read()
        for m in _STATUS_LITERAL_RE.finditer(src):
            lit = m.group(1)
            if lit in _NON_TASK_STATUSES:
                continue
            found.setdefault(lit, set()).add(fn)
        for m in _STATUS_IN_RE.finditer(src):
            for lit in re.findall(r"'([a-z_]+)'", m.group(1)):
                if lit in _NON_TASK_STATUSES:
                    continue
                found.setdefault(lit, set()).add(fn)
        for m in _STATUS_MEMBER_RE.finditer(src):
            member = m.group(1)
            assert hasattr(TaskState, member), (
                f'{fn} 引用了 TaskState.{member}，而 contracts 里没有这个成员'
            )
            found.setdefault(getattr(TaskState, member).value, set()).add(fn)
    # 自检：扫空/漏扫的话下面的子集断言永真
    missing = enum_values - set(found)
    assert not missing, (
        f'枚举里的 {sorted(missing)} 在四个管理器里一次都没被写过（扫到的是 '
        f'{sorted(found)}）—— 要么正则失效、本测试已无效，要么那几个状态是'
        '前端凭空多出来的分支。'
    )
    extra = set(found) - enum_values
    assert not extra, (
        '管理器写了 TaskStatus 里没有的状态：\n'
        + '\n'.join(f'  {k!r} <- {sorted(found[k])}' for k in sorted(extra))
        + '\n枚举是前端词表断言的真值来源，漏一个状态 = 界面上冒出一个英文字面量。'
        '任务级状态：先补进 src/contracts/outcome.py 的 TaskState、再补进 '
        'src/models/task.py 的 TaskStatus，最后补进 task_status.js 的三张表；'
        '文件级状态（dem_files / contour_files / tiles 的 status 列，不进任务徽章）：'
        '补进本文件上方的 _FILE_LEVEL_STATUSES；'
        '瓦片级结局 / 日志事件字段：_TILE_LEVEL_STATUSES / _LOG_EVENT_STATUSES。'
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
# 小字状态文本，行里不再有徽章。全状态覆盖的守卫相应搬家：
#   · getStatusColor / getStatusText 的五态覆盖仍由本节的下面两条守；
#   · 状态点的五态配色（图形侧，替代图标的 WCAG 1.4.1 职责）改由
#     tests/test_css_contract.py::test_task_row_status_dot_covers_every_status 守；
#   原 test_status_icons_are_real_distinct_glyphs 随之删除（无表可查）。
# 状态映射的唯一来源（改前 tasks.js / history.js 各有一份）。
STATUS_JS = 'task_status.js'

_STATUS_MAPS = (
    ('getStatusColor', 'colors', None),
    ('getStatusText', 'texts', None),
)


def test_status_map_covers_every_backend_status():
    """两张状态表都必须覆盖 TaskStatus 的全部**八**态。

    强度说明 —— 为什么不写成 `assert "'paused'" in src`：
    那种断言查的是「文件里有没有出现过这个词」，而 getStatusColor 与
    getStatusText 是两张独立的表，补了一张漏了另一张照样绿。这里按函数体
    逐张表解析键集合，并要求**等于**枚举（不是「包含」）—— 多一个不在后端
    存在的状态同样报错，因为那说明有人在前端凭空造了一个界面上永远到不了
    的分支。

    覆盖数的边界：2 张表 = 2 组，每组 8 个键。三次翻面各有来由：
      · 组数 4 -> 2：两个业务文件各有一份实现，已收口到 task_status.js；
      · 键数 6 -> 5：cancelled 随「取消任务」退出状态机；
      · 键数 5 -> 8（2026-08 §13-3 缺块状态机）：新增 retrying（补漏重跑）、
        pending_decision（有缺块、等用户决定）、completed_with_gaps（用户已
        显式接受缺块，产物出但永久带标记）。**真值来源是
        `src/contracts/outcome.py:TaskState`** —— models 层的 TaskStatus 只是
        它的镜像（一致性由 test_task_status_enum_covers_what_the_managers_actually_write
        钉住），本条仍从 TaskStatus 解析，因为它是那条链上离 JS 最近的一环。
    断言先钉住组数，再逐组比对 —— 只比对不钉组数的话，解析逻辑挂掉返回空列表
    时是永真。
    """
    enum_values = _task_status_values()
    assert len(enum_values) == 8, (
        f'TaskStatus 现在有 {len(enum_values)} 个成员：{sorted(enum_values)}。'
        '不是 8 个不一定是错，但下面每张表的期望值要跟着改，先确认是有意的'
    )
    checked = []
    problems = []
    src = _strip_js_comments(_js(STATUS_JS))
    for var_label, var_name, _holders in _STATUS_MAPS:
        body = _js_function_body(src, var_label)
        keys, _inner = _js_object_literal_keys(body, var_name)
        checked.append(var_label)
        if keys != enum_values:
            missing = sorted(enum_values - keys)
            extra = sorted(keys - enum_values)
            problems.append(
                f'{var_label} 键集合 {sorted(keys)}'
                + (f'，缺 {missing}' if missing else '')
                + (f'，多出 {extra}' if extra else '')
            )
    assert len(checked) == 2, f'只检查了 {checked}（期望 2 组）—— 本测试已失效'
    assert not problems, (
        '状态词表没覆盖后端全部状态：\n' + '\n'.join('  ' + p for p in problems)
        + f'\n真值来自 src/models/task.py 的 TaskStatus = {sorted(enum_values)}。'
        '\n漏掉的状态会走 `|| status` 兜底：中文界面里冒出英文字面量。'
    )


# i18n 改造后，状态词表的值不再是 JS 里的中文字面量，而是
# `'running': t('js.tasks.status.running')`。真值搬到了 src/i18n/catalog/。
# 下面三条断言相应改成两段式：先从 JS 里解析出 状态 -> i18n key 的配对
# （守「这个状态确实去查了那条文案」），再去目录里取值检查中文
# （守「那条文案确实是对的中文词」）。只查一半都能被绕过：
#   · 只查 JS：key 在、目录里的词被改成别的意思 —— 绿。
#   · 只查目录：词对、JS 里根本没引用它 —— 绿。
def _status_text_keys(js_name=STATUS_JS):
    """`getStatusText` 里 状态值 -> i18n key 的映射。"""
    body = _js_function_body(_strip_js_comments(_js(js_name)), 'getStatusText')
    return dict(re.findall(r"'([a-z_]+)'\s*:\s*t\('([\w.]+)'\)", body)), body


def test_status_labels_are_never_the_raw_backend_literal():
    """`getStatusText` 的每一个值都必须是中文，不能是英文原值。

    为什么单独一条：上一条只查「键齐不齐」。补一行 `'paused': 'paused'`
    就能让键集合合格，而界面上仍然显示英文 —— 中英混杂原样保留，测试全绿。
    这条把值也钉住：必须含中日韩统一表意文字，且不得等于键本身。
    """
    from src.i18n.catalog import MESSAGES

    enum_values = _task_status_values()
    problems = []
    checked = 0
    pairs, _body = _status_text_keys()
    assert len(pairs) == len(enum_values), (
        f'{STATUS_JS} 的 getStatusText 解析出 {len(pairs)} 对 状态->i18n key 映射，'
        f'期望 {len(enum_values)} 对 —— 本测试已失效'
    )
    for status, key in pairs.items():
        checked += 1
        entry = MESSAGES.get(key)
        if entry is None:
            problems.append(f'{status!r} -> {key!r} 在文案目录里不存在')
            continue
        value = entry['zh']
        if value == status or not re.search(r'[一-鿿]', value):
            problems.append(f'{status!r} -> {key} = {value!r}')
    assert checked == len(enum_values), f'只检查了 {checked} 条映射 —— 本测试已失效'
    assert not problems, (
        '状态文案不是中文（界面会中英混杂）：\n' + '\n'.join('  ' + p for p in problems)
    )


# --------------------------------------------------------------------------
# A7 / Task 12（评审第二轮）：补掉评审实测出来的 4 个逃逸
#   R2/R3  statusIcons 的**值**没被检查（`'cancelled': ''` 能恢复缺陷症状）
#   R10    状态 -> 文案的映射可以整体互换（键集合齐全就绿）
#   新     getStatusStroke 是第四处状态映射点，改前只覆盖 2/6
# --------------------------------------------------------------------------
#
# ⚠️ 下面两张表的键集合都是**八**态，真值来源是
# `src/contracts/outcome.py:TaskState`（权威状态机，带转换规则与每一条的
# 存在理由）。2026-08 §13-3 新增的三态一并入表：
#   retrying            补漏重跑；没有它，补漏与首次下载在历史里长得一样。
#   pending_decision    有缺块、等用户决定（补漏 / 接受并导出）。
#   completed_with_gaps 用户已显式接受缺块：产物出了、可用，但永久带标记。

# 每个状态的文案里必须出现的关键词。钉关键词而不是整句：
# 「已完成」改成「完成了」不该误红，「failed -> 已完成」必须红。
#
# 三个新态的关键词刻意避开与老态重合的那半句：completed_with_gaps 的文案是
# 「已完成（有缺块）」，钉「完成」的话它与 completed 可以整体互换而不被发现，
# 所以钉「缺块」；pending_decision 是「等待决定」，同理钉「决定」而不是
# 「等待」（那是 pending 的）。
_STATUS_LABEL_KEYWORD = {
    'pending': '等待',
    'running': '运行',
    'retrying': '补漏',
    'paused': '暂停',
    'pending_decision': '决定',
    'completed': '完成',
    'completed_with_gaps': '缺块',
    'failed': '失败',
}

# 每个状态在历史地图上的描边色应该走哪个调色板令牌。
# pending 是唯一的中性档，与徽章的中性档一致（见
# test_status_badge_color_matches_the_semantic_token 的说明）。
#
# 三个新态**复用已有的四档语义色**，一个新颜色名都不加（理由见
# task_status.js 的注释：每个新颜色名都要在 style.css 里配一条压 Bootstrap
# 的 `!important`，而 !important 总量有上界）：
#   retrying            -> info    与 running 同族：它就是在下载，只是第二遍。
#   pending_decision    -> warning 与 paused 同族：都是「停下来等你」。
#   completed_with_gaps -> warning **不是** success：产物带洞是必须被看见的
#                          事实，画成绿色等于把它伪装成干净的成功。
_STATUS_STROKE_TOKEN = {
    'pending': '--color-text-secondary',
    'running': '--color-info',
    'retrying': '--color-info',
    'paused': '--color-warning',
    'pending_decision': '--color-warning',
    'completed': '--color-success',
    'completed_with_gaps': '--color-warning',
    'failed': '--color-danger',
}


def test_status_labels_are_paired_with_the_right_status():
    """状态与文案的**配对**，不只是「八个键都在」。

    评审实测的逃逸（R10）：把 getStatusText 的六个值整体轮换一位 ——
    键集合齐全、每个值都是中文，上一版两条断言全绿，
    而界面上失败的任务写着「已完成」。这与 Task 10 的教训同型：
    **守了集合，没守配对。**

    钉关键词而不是整句：文案微调（「已完成」->「完成了」）不该误红，
    整体互换必须红。
    """
    from src.i18n.catalog import MESSAGES

    problems, checked = [], 0
    pairs, _body = _status_text_keys()
    assert set(pairs) == set(_STATUS_LABEL_KEYWORD), (
        f'{STATUS_JS} 的 getStatusText 键集合是 {sorted(pairs)} —— '
        '先修 test_status_map_covers_every_backend_status'
    )
    for status, keyword in _STATUS_LABEL_KEYWORD.items():
        checked += 1
        label = MESSAGES[pairs[status]]['zh']
        if keyword not in label:
            problems.append(
                f'{status!r} -> {pairs[status]} = {label!r}，应含 {keyword!r}')
    assert checked == 8, f'只检查了 {checked} 组（期望 8）—— 本测试已失效'
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
#   · 图形侧（五态状态点配色 + 对比度）：
#     tests/test_css_contract.py::test_task_row_status_dot_covers_every_status
#   · 文字侧（小字状态文本必须来自 getStatusText 的中文词表）：
#     本文件 test_status_labels_are_paired_with_the_right_status 等 A7 断言
# 与图形侧的区别说明：状态点比 SVG 图标信息量低，所以活动行行1 与历史行
# 行2 都带 getStatusText 的状态文字，颜色不再是唯一通道。

def test_map_rectangle_stroke_covers_every_status():
    """历史地图矩形的描边色是**第四处**状态映射点，同样要覆盖八态、走调色板令牌。

    ⚠️ 登记（2026-08 §13-3）：期望从五态改成八态。真值来源是
    `src/contracts/outcome.py:TaskState`（models 的 TaskStatus 是它的镜像），
    对表用的 `_STATUS_STROKE_TOKEN` 见上方注释 —— 三个新态复用已有语义色，
    其中 completed_with_gaps 走 warning 而不是 success：地图上一块带洞的成品
    画成绿色，等于在最一眼的那个通道上把它伪装成干净的成功。

    评审找到的漏网：改前 `renderHistoryMap` 里是一条内联三元阶梯，
    只认 completed / failed，其余三态（pending / running / paused）
    全折叠成同一个蓝色 —— 与徽章那三张表是完全同型的缺陷，只是发生在第四处。

    而且三个色号是**硬编码且离调色板**的：`#10b981` 是 emerald-500，
    本项目的 `--color-success` 是 emerald-400 `#34d399`，改调色板时这里会静默漂移。

    这条同时守两件事：五态全覆盖、且每一态指向正确的语义令牌（配对，不只是集合）。
    """
    # 令牌表在模块级常量 _STATUS_STROKE_TOKENS（getStatusStroke 只做求值+缓存），
    # 兜底仍在函数体里。
    src = _strip_js_comments(_js(STATUS_JS))
    table = re.search(r'_STATUS_STROKE_TOKENS\s*=\s*\{(.*?)\}', src, re.S)
    assert table, '解析不出 _STATUS_STROKE_TOKENS 表 —— 本测试已失效'
    pairs = dict(re.findall(r"'([a-z_]+)'\s*:\s*'(--[-\w]+)'", table.group(1)))
    assert pairs, '_STATUS_STROKE_TOKENS 里解析不出 {状态: 令牌} 映射 —— 本测试已失效'
    body = _js_function_body(src, 'getStatusStroke')
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
    # renderHistoryMap 仍在 history.js（只有映射表收口到 task_status.js）。
    render = _js_function_body(_strip_js_comments(_js('history.js')), 'renderHistoryMap')
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
    """start/pause/resume 非 2xx 时必须透出服务端 error，三个函数同一口径。

    改前三个函数都是 `throw new Error('启动任务失败')`，后端给的具体原因
    （如"任务已在运行"）被整个丢掉，用户只看到一句套话。
    """
    for fn in ('startTask', 'pauseTask', 'resumeTask'):
        body = _fn(fn)
        assert 'result.error' in body, (
            f'{fn} 没有读响应体里的 result.error——服务端错误原因被丢弃，'
            '三个函数都应统一读 `result.error || response.status`'
        )


def test_upload_contour_task_shows_render_phase_not_fake_download():
    """上传来源的等高线任务（dataset='upload'）没有下载阶段，必须按渲染阶段显示。

    后端创建上传任务时 downloaded_files == total_files（上传即落盘），
    contourPhaseCounts 回落到文件计数会让 pending 任务一出现就显示
    100%「下载 DEM」。判定口径与后端 is_upload 一致（dataset == "upload"，
    见 src/services/contour_task_manager.py）。
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

    已知五态由词表覆盖（见上面的 A7 断言）；词表外的状态统一显示「未知」，
    与 A7 修过的中英混杂问题保持同一方案。
    """
    from src.i18n.catalog import MESSAGES

    body = _fn('getStatusText', STATUS_JS)
    assert not re.search(r"\|\|\s*status\b", body), (
        'getStatusText 仍用 `|| status` 兜底——'
        '未知英文状态会被原样渲染进中文界面'
    )
    fallback = re.search(r"\|\|\s*t\('([\w.]+)'\)", body)
    assert fallback, (
        "getStatusText 没有 `|| t('…')` 形态的兜底文案"
    )
    assert MESSAGES[fallback.group(1)]['zh'] == '未知', (
        f"getStatusText 兜底文案 {fallback.group(1)} 的中文是 "
        f"{MESSAGES[fallback.group(1)]['zh']!r}，应统一显示 '未知'"
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


def test_history_map_goes_through_the_server_side_basemap_proxy():
    """历史小地图必须走服务端 /basemap 代理，前端不许自己拼上游地址。

    改前 history.js 有一份 _historyBaseMapUrl 平行实现：拿不到配置回退外网
    OSM、拿到别名拼 `//host/vt?lyrs=m`。三条硬约束一次全破 —— 离线（断网即
    白屏）、服务端代理（浏览器直连撞 CORS 且不吃 proxy_url）、WGS-84
    （lyrs=m 在中国区是 GCJ-02 偏移，叠在上面的任务矩形必然错位）。

    「服务端」而不是「同源」：这条应用内路径 0.3 起默认由瓦片专用端口出图
    （src/core/tile_server.py，那个端口自己发 CORS 头），只有降级时才真是同源。
    要守的是「这一跳在服务端」——CORS 与 proxy_url 两条理由都系在这上面。
    """
    src = _strip_js_comments(_js('history.js'))
    assert '_historyBaseMapUrl' not in src, (
        'history.js 又出现了浏览器侧拼底图地址的平行实现'
    )
    assert 'tile.openstreetmap.org' not in src, (
        'history.js 仍引用外网 OSM 底图 —— 违反离线约束'
    )
    assert 'lyrs=' not in src, (
        'history.js 仍自己拼上游瓦片参数（lyrs=）—— 上游地址不该出现在前端'
    )
    assert '/basemap/{z}/{x}/{y}' in src, (
        'history.js 没有同源底图路径回退'
    )
    body = _js_function_body(src, 'initHistoryMap')
    assert '/api/basemap' in src and '_resolveHistoryBasemap' in body, (
        'initHistoryMap 必须消费服务端下发的底图描述符（内联 basemap 或 /api/basemap）'
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
# 单一时间流（2026-08 定稿，第二轮）
#
# 本节的前身有两代：「统一任务表（2026-07，9 列表格）」→「统一流式列表
# （2026-08 第一轮，活动/失败/历史三分区 + 失败组折叠 + 精确去重）」。
# 用户明确不要三个分区后定稿：**按创建时间倒序的单一时间流 + 顶部状态
# 筛选**——无分组头、无折叠、无去重，每个任务天然只出现一次；行渲染
# 从 tasks.js / history.js 两套近似实现收口为 history.js createTaskRow
# 一处。本节的锚点随之再次翻面，每条都登记了翻面理由。
# --------------------------------------------------------------------------

def test_row_rendering_is_unified_in_history_js():
    """行渲染全站只有一处：task_list.js 的 TaskRow 组件。

    （前身守 tasks.js 不再持有行模板。2026-08 Vue 化后锚点再挪一次：
    history.js 的 createTaskRow 也删了，模板收口到组件。）

    两套近似实现必然漂移（历史上 tasks.js createTaskRow / history.js
    createHistoryRow 的按钮组、时间语义就各不相同），所以任何一个业务文件
    都不许再长出行模板。
    """
    for name in ('tasks.js', 'task_center.js', 'history.js'):
        src = _strip_js_comments(_js(name))
        for fn in ('createTaskRow', 'createTaskErrorRow', 'taskMetaText',
                   'renderActiveTasks', 'toggleFailedTaskGroup'):
            assert f'function {fn}(' not in src, (
                f'{name} 仍定义 {fn}()——行渲染已收口到 task_list.js 的 TaskRow '
                '组件，留着第二份实现必然漂移（「两种行语言」是「太乱」的根因之一）'
            )
        assert 'task-row' not in src, (
            f'{name} 里出现了 task-row 类名 —— 行 markup 只能在组件模板里'
        )

    # 渲染层锚点：组件挂到 #historyTableBody
    comp = _strip_js_comments(_js('task_list.js'))
    assert "getElementById('historyTableBody')" in comp, (
        'task_list.js 没有以 #historyTableBody 为挂载点'
    )
    assert re.search(r'v-for="task in tasks"\s+:key="task\._key"', _js('task_list.js')), (
        '时间流不是 keyed v-for —— :key 是「同一个任务不会渲染出两行」的'
        '结构保证，改造前那套 getElementById 查重就是因为没有它'
    )
    # 数据入口：history.js 只写 store，不碰 DOM
    render_body = _js_function_body(_strip_js_comments(_js('history.js')),
                                    'renderHistoryTable')
    assert 'TaskStore.replaceAll(' in render_body, (
        'renderHistoryTable 没有把数据交给 store —— 时间流不会更新'
    )
    assert 'innerHTML' not in render_body, (
        'renderHistoryTable 又开始直接写 innerHTML 了 —— 渲染归组件'
    )


def test_page_refresh_preserves_the_fetched_gap_breakdown():
    """整页替换（replaceAll）必须把已经拉回来的 gap_summary 搬到新行上。

    实测缺陷（缺块面 §13-3 的第二个洞）：GET /api/tasks/<id>/gaps 明明 200
    回来了，行上却永久停在「正在读取缺块明细…」。链路是这样断的 ——
    loadHistory 每次都 replaceAll，新行是从接口响应重建的，里面没有
    gap_summary（那是客户端 ensureGapSummary 挂上去的）；而组件重新拉取的
    三个触发点在「同一个 key 原地换了个对象、status 与 gap_tiles 都没变」时
    **一个都不响**：keyed diff 复用实例所以不 mount，两条 watch 求值结果不变
    所以不触发。task_list.js 那三个触发点的注释写着它们的存在正是为了防住这
    个结局 —— 它们防不住，因为它们看不见「摘要消失了」。

    另一半同样要守：缺块数**变了**的时候必须让旧明细消失。补漏跑完后数变了，
    搬过去的旧分档就是错的；而且不让它消失的话，task.gap_tiles 那条 watch
    即便触发也白搭 —— syncGaps 见到 gap_summary 还在就早退了。
    """
    src = _strip_js_comments(_js('task_store.js'))
    replace_body = _js_function_body(src, 'replaceAll')
    assert 'carryClientState(' in replace_body, (
        'replaceAll 直接整行替换，把客户端拉回来的 gap_summary 抹掉了 —— '
        '缺块明细会永久停在「正在读取…」，而接口早就 200 回过了'
    )
    carry = _js_function_body(src, 'carryClientState')
    assert 'gap_summary' in carry, (
        'carryClientState 没有搬 gap_summary —— 它是唯一一个「接口不回、'
        '客户端拉回来」的字段，不搬就等于没修'
    )
    assert 'gap_tiles' in carry, (
        'carryClientState 没有拿缺块数当判据 —— 补漏改变缺块数之后，'
        '过期的分档明细会被一路搬下去，而且再也不会重拉'
    )

    # 组件侧的重拉触发点：缺块数变化那一条是本修复的配套，删了它「数变了要
    # 重拉」就断了（store 让摘要消失，但没有任何人去拉新的）。
    comp = _strip_js_comments(_js('task_list.js'))
    assert re.search(r"['\"]task\.gap_tiles['\"]\s*:", comp), (
        'TaskRow 没有 watch task.gap_tiles —— 缺块数变化后不会重新拉分档明细'
    )
    # syncGaps 是对象字面量里的简写方法，_js_function_body 只认
    # `function <name>(`，这里自己按花括号配对切。
    sm = re.search(r'\bsyncGaps\s*\(\s*\)\s*\{', comp)
    assert sm, 'task_list.js 里找不到 syncGaps() —— 本测试已失效'
    depth, sync = 0, ''
    for j in range(sm.end() - 1, len(comp)):
        if comp[j] == '{':
            depth += 1
        elif comp[j] == '}':
            depth -= 1
            if depth == 0:
                sync = comp[sm.end():j]
                break
    assert sync, 'syncGaps 花括号不配对 —— 本测试已失效'
    assert 'gap_summary' in sync and 'ensureGapSummary(' in sync, (
        'syncGaps 不再按「摘要还没到」拉取 —— 要么永远不拉，要么每次轮询都拉'
    )


def test_task_row_is_the_unified_two_line_structure():
    """时间流行是同一种「统一流式行」结构，且覆盖全部状态变体。

    这条守四件事：
      1. 结构锚点存在（行1 = task-line1：状态点/名称/#类型:id/元信息/
         状态小字/时间；行2 按状态变体）；
      2. 稳定类名没有改名——tests/test_css_contract.py 的层叠模型按
         `.task-row > .task-line1 > .task-dot` 这样的祖先链算最终样式，
         模板里类名一换，那边整片断言就对着不存在的元素空转；
      3. 三种行2 变体齐全：进度行（task-progress-line，活动态）/
         引文式错误（task-error，failed）/ 单行摘要（task-line2，终态）。
      4. 徽章 pill 不再存在（状态识别 = 状态点 + 小字状态文本）。
    """
    body = _tpl('TaskRow')
    for anchor in ('task-row', 'task-line1', 'task-dot', 'task-name',
                   'task-id', 'task-meta', 'task-status-text', 'task-time'):
        assert anchor in body, (
            f'TaskRow 模板缺 {anchor} —— 统一流式行的结构锚点（见 docstring）'
        )
    # 状态类名是绑定形态：:class="'status-' + task.status"
    assert re.search(r""":class="'status-'\s*\+\s*task\.status\"""", body), (
        'TaskRow 根节点没有绑定 status-* 类 —— 状态点配色（.task-row.status-x '
        '.task-dot）会全部失效'
    )
    for cls in ('task-pct', 'task-count', 'progress-bar'):
        assert cls in body, f'TaskRow 模板缺稳定类名 {cls}'
    for variant in ('task-progress-line', 'task-error', 'task-line2'):
        assert variant in body, (
            f'TaskRow 模板缺行2 变体 {variant} —— 进度/错误/摘要三种形态要齐全'
        )
    for forbidden in ('<tr', '<td', 'colspan', 'badge'):
        assert forbidden not in body, (
            f'TaskRow 模板里还有 {forbidden} —— 退回 9 列网格/徽章形态了'
        )


def test_stream_has_no_grouping_or_collapsing_machinery():
    """三分区治理机制整体消失：无分组头、无失败组折叠、无 sessionStorage 记忆。

    （前身 test_failed_group_collapses_beyond_five_and_remembers_the_choice：
    守「失败组 >5 折叠显示 3 个」。用户明确不要分区后，红墙问题由
    「单一时间流 + 状态筛选」根治——失败任务按创建时间散在流里，
    要看失败点「失败」chip——折叠机制本身成了要删的东西。锚点翻面。）
    """
    for name in ('tasks.js', 'task_center.js'):
        src = _strip_js_comments(_js(name))
        for gone in ('FAILED_GROUP_COLLAPSE_THRESHOLD', 'FAILED_GROUP_PREVIEW_COUNT',
                     'failedGroupCollapsed', 'taskFailedGroupCollapsed',
                     'task-group-header', 'sessionStorage'):
            assert gone not in src, (
                f'{name} 里还有 {gone} —— 失败组折叠机制应随三分区整体删除'
            )
    hist_src = _strip_js_comments(_js('history.js'))
    assert 'task-group-header' not in hist_src, (
        'history.js 里还有分组头——单一时间流没有「活动/失败/历史」分区'
    )


def test_history_stream_has_no_dedup_logic():
    """单一时间流无去重：renderHistoryTable 不再读 activeTasks / #activeTasksBody。

    （前身 test_history_stream_dedups_exactly_against_active_tasks：守
    「历史流按 activeTasks 精确去重」。去重是三分区时代的补丁——同一任务
    可能同时挂在实时区和历史流里；分区废掉后四表 UNION 每任务一行，
    天然只出现一次，去重失去存在理由。锚点翻面的理由与上一条同源。）
    """
    body = _fn('renderHistoryTable', 'history.js')
    assert 'activeTasks' not in body, (
        'renderHistoryTable 还在读 activeTasks Map——去重逻辑应随三分区删除'
    )
    assert 'activeTasksBody' not in body, (
        'renderHistoryTable 还在检查 #activeTasksBody——实时区已不存在'
    )
    assert '.filter(' not in body, (
        'renderHistoryTable 还有 filter——时间流应原样渲染传入的任务页'
    )


def test_status_chips_filter_the_whole_stream():
    """状态筛选 chips 作用于整个时间流：loadHistory 把取值透传给
    /api/history_all 的 ?status= 参数；chips 点击后回到第 1 页刷新。

    （前身 test_status_chips_filter_only_the_history_stream：chips 只筛
    历史流，活动/失败分组不受影响。活动任务进流之后，「只筛历史」
    与「筛全部」变成同一件事，语义锚点不变、覆盖范围变了。）
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


def test_terminal_row_shows_created_at_and_live_row_shows_elapsed():
    """行1 右侧时间的语义：终态显示创建时间短日期，非终态显示耗时。

    列表按创建时间倒序（/api/history_all ORDER BY created_at DESC），
    行上展示创建时间才自洽；完成时间在详情模态里。非终态的耗时文本
    来自 calculateTimeInfo（tasks.js），独立页 /history 不加载它，
    所以调用点必须带 typeof 守卫。
    """
    comp = _strip_js_comments(_js('task_list.js'))
    assert 'formatShortDate(this.task.created_at)' in comp, (
        '终态行的行1 时间不是创建时间短日期——列表按创建排序，展示完成时间不自洽'
    )
    assert "typeof calculateTimeInfo !== 'function'" in comp, (
        '非终态耗时没有 typeof 守卫——独立页 /history 不加载 tasks.js，'
        '直接调用会 ReferenceError 让整列渲染挂掉'
    )
    assert 'store.state.tick' in comp, (
        'timeText 没有依赖 store.state.tick —— 耗时不会每秒刷新，'
        '会一直停在首次渲染的值'
    )
    src = _strip_js_comments(_js('history.js'))
    assert 'function formatShortDate(' in src, (
        'history.js 没有 formatShortDate——短日期格式化函数没了'
    )


def test_history_error_text_only_lands_via_textcontent():
    """失败行的错误原文不会被当 HTML 解析（与上一条同一条规矩的时间流侧）。

    /api/history_all 返回的 error_message 同样是后端异常的字符串化结果。
    改造前 createTaskRow 只吐一个**空**的 .task-error 容器，文本由
    renderHistoryTable 在渲染后用 textContent 补；Vue 化后统一走
    `{{ errorText }}` 插值（自动转义），那条两步路没了。

    这里守的是：数据入口不许自己动手渲染错误文本。
    """
    src = _strip_js_comments(_js('history.js'))
    render_body = _js_function_body(src, 'renderHistoryTable')
    assert 'innerHTML' not in render_body and 'textContent' not in render_body, (
        'renderHistoryTable 又在自己写 DOM 了 —— 错误文本的渲染归组件，'
        '这里只负责把数据交给 store'
    )
    assert 'TaskStore' in render_body, (
        'renderHistoryTable 没有把数据交给 store'
    )


def test_pagination_bar_is_hidden_when_only_one_page():
    """总页数 <= 1 时不渲染分页条（孤零零一个「1」按钮没有交互价值）。"""
    body = _fn('renderPagination', 'history.js')
    assert re.search(r'totalPages\s*<=\s*1', body), (
        'renderPagination 没有「<= 1 页直接返回」的分支——单页时会显示孤按钮'
    )
    assert re.search(r"innerHTML\s*=\s*''", body), (
        'renderPagination 的单页分支没有清空分页条'
    )


def test_completed_task_is_rebuilt_in_place_not_removed():
    """task_completed：行原地重建为**载荷给的那个终态** + loadStats()，不删行、不重拉。

    （前身 test_completed_task_refreshes_initialized_history_panel：守
    「删实时行 + loadHistory(1) + loadStats()」。那是活动/历史分区时代的
    做法——完成的任务要从活动区「搬」进历史区。单一时间流里没有分区，
    换状态只是换这一行的形态；删行会让任务在界面上凭空消失。）
    """
    body = _fn('handleTaskCompleted')
    assert not re.search(r'\.remove\(\s*\)', body), (
        'handleTaskCompleted 里出现了 .remove()——完成的任务要留在时间流里，'
        '只换形态，不是删除'
    )
    assert 'loadHistory(' not in body, (
        'handleTaskCompleted 还在 loadHistory 重拉——改数据已经够了，'
        '重拉会把用户正在看的页码/滚动位置冲掉'
    )
    # 终态**不许写死**。后端 _complete_task 发的是 final_status，带缺块时它是
    # completed_with_gaps；写死 'completed' 的那一版实测把行降级过 ——
    # task_gap_decision 与 task_completed 是同一毫秒里前后脚发的两发，后到的
    # 这一发赢，缺块标记要到整页刷新才回来（§13-3 要求它永久跟着任务走）。
    # 断言形状而不是断言字面量：允许任何「从参数取值 + 兜底 'completed'」的写法，
    # 只禁掉「常量 'completed'」这一种。
    m = re.search(r"TaskStore\.commit\(\s*key\s*,\s*\{\s*status:\s*([^,}]+)", body)
    assert m, 'handleTaskCompleted 没有把状态写进 store —— 行不会换形态'
    status_expr = m.group(1).strip()
    assert status_expr != "'completed'", (
        "handleTaskCompleted 把终态写死成 'completed' —— 后端发的 "
        'completed_with_gaps 会被静默降级成干净成品'
    )
    assert 'status' in status_expr and "'completed'" in status_expr, (
        f'终态表达式是 {status_expr!r}，应当「取载荷里的 status，缺失时兜底 '
        "'completed'」"
    )
    # 载荷里的 status 必须真的送进来，否则上面那个参数永远是 undefined。
    handler = _strip_js_comments(_js('task_center.js'))
    assert re.search(
        r"handleTaskCompleted\(\s*data\.task_id,[^)]*data\.status", handler
    ), 'socket 处理器没有把 data.status 传给 handleTaskCompleted'
    assert re.search(r'loadStats\(\s*\)', body), (
        'handleTaskCompleted 没有调 loadStats()——统计卡还是旧数字'
    )


def test_failed_task_is_rebuilt_in_place_with_error_refilled():
    """task_failed：行原地重建为 failed 态（含引文式错误行）+ loadStats()。

    与 completed 同一形态（见上一条）。失败行额外的硬性要求：
    错误原文走 textContent 回填（rebuildStreamRow 内部调 applyTaskErrorText，
    由 test_row_rebuild_refills_the_error_text 钉住），这里钉「失败事件的
    处理路径确实走原地重建」。
    """
    body = _fn('handleTaskFailed')
    assert re.search(r"TaskStore\.commit\([^)]*status:\s*'failed'", body), (
        'handleTaskFailed 没有把状态改成 failed——失败的任务会停留在 running 形态'
    )
    assert re.search(r'loadStats\(\s*\)', body), (
        'handleTaskFailed 没有调 loadStats()——统计卡的失败计数还是旧数字'
    )


def test_new_task_prepends_only_on_first_page_and_matching_chip():
    """未知任务（新建）prepend 到流顶部的条件：第 1 页 + chip 为 全部/进行中。

    其它页码/其它筛选下硬插会破坏「按创建时间倒序 + 状态筛选」的语义
    （任务会出现在它不该出现的页里）。不满足时不插：翻页/切 chip 会从
    /api/history_all 重拉，任务自然出现。
    """
    body = _fn('prependStreamRow')
    assert re.search(r'currentPage\s*!==\s*1', body), (
        'prependStreamRow 没有「只在第 1 页」的门禁——深页里会插进不属于那里的行'
    )
    assert "currentStatusFilter !== ''" in body and "'active'" in body, (
        'prependStreamRow 没有「chip 为 全部/进行中」的门禁——'
        '失败/已完成/已取消筛选下会插进不符合筛选条件的行'
    )
    assert 'TaskStore.upsert(' in body, (
        'prependStreamRow 没有把新任务写进 store——新任务不会出现在流顶'
    )
    # 重复推送必须合并而不是插第二行。改造前靠 getElementById 查重；
    # 现在 store 按 key 合并 + 组件 keyed v-for，两层结构性保证。
    assert 'TaskStore.has(' in body and 'TaskStore.patch(' in body, (
        'prependStreamRow 没有「已在流里就合并」的分支——'
        '活动任务被分页窗口挤掉后又收到推送时会插出第二行'
    )
    # updateTaskProgress 的未知 key 分支必须走它
    progress_body = _fn('updateTaskProgress')
    assert 'prependStreamRow(' in progress_body, (
        'updateTaskProgress 的未知 key 分支没有调 prependStreamRow——'
        '新建任务不会出现在时间流里'
    )


def test_delete_also_drops_the_row_from_the_store():
    """deleteTask 删成功后，除了重拉分页，还必须把行从 store 本地摘掉。

    （前身是 test_dismiss_removes_the_row_purely_on_the_frontend。「移除」按钮
    随「取消任务」一并下线后，「把失败行从眼前拿走」这件事由 🗑 承担。）

    名字里刻意**没有** purely-on-the-frontend：那是 dismiss 时代的语义，
    deleteTask 既打 DELETE 也调 loadHistory 重新分页（后端行真的没了，页码
    可能要回退）。这条钉的是**本地摘行必须额外发生**：只靠 loadHistory 不够，
    状态栏的活动任务聚合读的是 store.active，那份不会被重拉刷新，被删任务会
    一直算在「N 个活动任务」里。

    「删成功后一并关掉那条常驻失败 toast」是同一分支里的另一半契约，由
    test_fix_realtime_chain.py::test_delete_task_closes_the_persistent_failure_toast
    守着（那条还额外钉了 typeof 守卫，独立页 /history 不加载 tasks.js）。
    """
    body = _fn('deleteTask', 'history.js')
    assert 'TaskStore.remove(' in body, (
        'deleteTask 没有把任务从 store 摘掉——行要等下一次整页刷新才消失，'
        '状态栏的活动任务计数也会一直多算它一个'
    )
    assert 'updateStatusTasks(' in body, (
        'deleteTask 摘完行没有刷新状态栏——底部「N 个活动任务 X%」会原地冻结'
    )


def test_panel_reopen_refreshes_timeline():
    """记录面板重开必须重新拉取时间流 + 统计,不能只吃懒初始化那一遍。

    回归场景:面板打开过(inited 标记已置位)→ 关闭 → 新建任务 →
    _afterTaskCreated 重开面板。旧实现重开只 resize 小地图,时间流停在
    上一次 loadHistory 的内容 —— 新建的 pending 任务没有 socket 进度事件
    可触发 prependStreamRow,列表里看不到,要刷新页面才出现。
    """
    src = _strip_js_comments(_js('panels.js'))
    body = _js_function_body(src, 'openPanel')
    # 已初始化分支(else if)也要刷新:断言 loadHistory 的调用点在
    # inited 判定之后仍能到达 —— 直接要求 openPanel 体内存在
    # 「已初始化时」的 loadHistory 调用(懒初始化分支只调 initHistory)。
    assert 'loadHistory(' in body, (
        'openPanel 没有调 loadHistory——面板重开不会刷新时间流,'
        '新建任务要等到整页刷新才看得见'
    )
    assert 'loadStats(' in body, (
        'openPanel 重开时应一并刷新统计卡片,否则总数与列表口径不一致'
    )


# --------------------------------------------------------------------------
# 删除流程：两级确认合成一个框（v0.2.12 final review 的 UX 三条）
#
# 旧形态是串起来的两个 showConfirm。第二个框问的是**另一个维度**的问题
# （产物删不删），于是三条路殊途同归：「删除产物」/「保留产物」/ ESC 全都
# 照发 DELETE，区别只在 ?delete_files=。用户按 ESC 以为自己撤销了删除，
# 任务照删不误 —— 而这一版删除已经能杀 running（v0.2.11 那层 400 拒绝没了），
# 🗑 成了唯一的销毁入口，承重比以前大得多。
# --------------------------------------------------------------------------


def _active_statuses():
    """`?status=active` 的未终结状态集合。

    ⚠️ 登记（2026-08 §13-3）：来源从「api.py 里那条 SQL 谓词的字面量」改成
    `src.contracts.outcome.ACTIVE_STATE_VALUES`。api.py 现在**自己就是**从那
    个常量拼谓词的（`'status IN (%s)' % ', '.join('?' * len(...))`），谓词里
    只剩占位符，一个字面量都没有 —— 继续正则切那条 SQL 只能切出空集合。

    仍然不在测试里硬写 {'pending','running','paused'}：那样后端哪天多一个
    未终结状态（缺块改造就一次加了 retrying / pending_decision 两个），前端
    漏了它的警告文案，这里照样全绿。真值搬到 contracts 之后这条更结实：
    路由与本测试读的是同一份常量，路由改口径也不会与它脱节。
    """
    from src.contracts.outcome import ACTIVE_STATE_VALUES

    # api.py 必须真的在用它 —— 否则本函数守的是一份没人消费的清单。
    with open(os.path.join(ROOT, 'src', 'routes', 'api.py'), encoding='utf-8') as f:
        src = f.read()
    assert 'ACTIVE_STATE_VALUES' in src, (
        'src/routes/api.py 不再引用 ACTIVE_STATE_VALUES —— ?status=active 的'
        '口径又长回了本地字面量，本测试已失效'
    )
    vals = set(ACTIVE_STATE_VALUES)
    assert vals, 'ACTIVE_STATE_VALUES 是空的 —— 本测试已失效'
    assert vals <= _task_status_values(), (
        f'ACTIVE_STATE_VALUES 里的 {sorted(vals - _task_status_values())} 不在 TaskStatus 里'
    )
    return vals


def test_delete_asks_once_and_a_cancel_sends_nothing():
    """删除只弹**一个**确认框，且取消 / ESC / 点遮罩之后**不发 DELETE**。

    这是本次修复的核心：旧实现里 `const deleteFiles = await showConfirm(...)`
    的返回值只被拿去拼 `?delete_files=`，false 分支照样往下走到 fetch。也就是
    说第二个框根本没有「不删」这个出口 —— 而它长得就像一个「要不要删除」的
    确认框（左边那颗按钮写着「保留产物」，占的是取消位）。

    断言的三件事缺一不可：
      1. 只有一次 showConfirm —— 否则「合成一个框」这件事就没发生；
      2. 有一条以确认结果为条件的 return；
      3. 那条 return 出现在 fetch 之前 —— 位置颠倒的话它拦不住任何请求。
    """
    body = _fn('deleteTask', 'history.js')
    assert body.count('showConfirm(') == 1, (
        f'deleteTask 里有 {body.count("showConfirm(")} 处 showConfirm —— '
        '删除流程只许问一次；串起来的第二个框，它的取消位问的是另一个维度，'
        '用户按 ESC 时以为撤销了删除，实际上主动作照做'
    )
    guard = re.search(r'if\s*\(\s*!\s*(\w+)\.confirmed\s*\)\s*\{?\s*return', body)
    assert guard, (
        'deleteTask 里找不到「用户没确认就 return」的门禁 —— '
        '取消 / ESC / 点遮罩会继续往下走，照样发 DELETE'
    )
    fetch_at = body.find('fetch(')
    assert fetch_at >= 0, 'deleteTask 里找不到 fetch( —— 本测试已失效'
    assert guard.end() < fetch_at, (
        '「没确认就 return」的门禁排在 fetch 之后 —— 请求已经发出去了才判断，'
        '拦不住任何东西'
    )


def test_delete_files_checkbox_defaults_to_unchecked():
    """「同时删除磁盘产物」默认**不勾**，且勾选值真的驱动 ?delete_files=。

    默认不勾是在延续旧行为的安全侧：旧的第二个框里 ESC 与取消位都落在
    delete_files=false。产物是用户花了几小时下下来的，默认值只能站在
    「少删」这边。

    第二半断言（勾选值 -> 查询参数）不能省：只钉 `checked: false` 的话，
    把 deleteFiles 写死成 true 也全绿 —— 勾选框成了个装饰，而默认行为变成
    了最具破坏性的那一种。
    """
    body = _fn('deleteTask', 'history.js')
    m = re.search(r'checkbox:\s*\{(.*?)\}', body, re.S)
    assert m, (
        'deleteTask 的 showConfirm 没有传 checkbox —— 产物删不删这个问题没地方问了'
    )
    assert re.search(r'checked:\s*false', m.group(1)), (
        f'确认框的勾选框默认值不是 false（实际写的是 {m.group(1).strip()!r}）—— '
        '默认勾上等于替用户选了最具破坏性的那一边'
    )
    assert re.search(r'deleteFiles\s*=\s*\w+\.checked', body), (
        'deleteFiles 不是从勾选结果取的 —— 勾选框成了装饰品'
    )
    assert re.search(r'delete_files=\$\{\s*deleteFiles\s*\?', body), (
        '?delete_files= 不是由 deleteFiles 拼出来的 —— 用户勾没勾传不到后端'
    )


def test_confirm_checkbox_only_serves_the_delete_flow():
    """`opts.checkbox` 只许 history.js 的删除流程用。

    为什么要钉这条：带 checkbox 时 showConfirm 改 resolve `{confirmed, checked}`，
    而对象**恒为真**。既有调用点全是 `if (!await showConfirm(...)) return;` 的
    写法，谁顺手给自己加一个 checkbox 又忘了改判断，那个确认框就再也拦不住人 ——
    静默失效，没有任何报错。
    """
    js_dir = os.path.join(ROOT, 'static', 'js')
    users = set()
    for name in sorted(n for n in os.listdir(js_dir) if n.endswith('.js')):
        if name == 'ui.js':      # 定义端
            continue
        if 'checkbox:' in _strip_js_comments(_js(name)):
            users.add(name)
    assert users == {'history.js'}, (
        f'showConfirm 的 checkbox 选项被 {sorted(users)} 使用 —— 期望只有 history.js。\n'
        '带 checkbox 时 resolve 的是对象（恒为真），`if (!await showConfirm(...))` '
        '那种写法会静默失效'
    )
    assert 'checkbox:' in _fn('deleteTask', 'history.js'), (
        'history.js 里的 checkbox 不在 deleteTask 内 —— 钉点跑偏了，本测试已失效'
    )


def test_cancelling_the_confirm_never_reports_a_checked_box():
    """取消时 checked 必须被压成 false —— 「什么都不做」不能漏出勾选值。

    调用方只判 confirmed 的话这条无关紧要；可一旦有人写成
    `if (answer.checked) cleanupFiles();`，一个「用户勾了框又按 ESC」的操作
    就会把产物删掉。让 ui.js 在源头保证，比要求每个调用方记得判断可靠。
    """
    body = _js_function_body(_strip_js_comments(_js('ui.js')), 'showConfirm')
    assert body.strip(), 'ui.js 的 showConfirm 函数体切出来是空的 —— 本测试已失效'
    assert re.search(
        r'checked:\s*result\s*&&', body,
    ), (
        'showConfirm 的 resolve 没有把 checked 与 result 相与 —— '
        '用户勾了框再按 ESC，取消的结果里仍带着 checked: true'
    )
    assert re.search(r'confirmed:\s*result', body), (
        'showConfirm 带 checkbox 时没有下发 confirmed —— 调用方判不出用户到底点了哪颗'
    )


def test_deleting_an_unfinished_task_says_what_will_be_lost():
    """未终结的任务：确认文案必须点明「删了会怎样」；终态走通用文案，不带警告。

    v0.2.11 里删一个正在跑的任务会被后端 400 挡下（用户看到「删除失败」），
    那层拒绝事实上在替用户兜底。这一版放开了 —— 拒绝没了，文案就得补上。

    活动态各说各的，不合并成一句「该任务尚未结束」：pending 什么都还没跑
    （只是排队），对它说「正在运行」是撒谎；running / retrying / paused 有
    已下载的进度会丢；pending_decision 还攒着一份等用户决定的缺块清单。
    用户按下删除前要判断的正是「我会失去什么」。

    ⚠️ 登记（2026-08 §13-3）：活动态从三个变成五个
    （contracts.outcome.ACTIVE_STATE_VALUES 加了 retrying / pending_decision）。
    这不是期望值换个数字：本条当时**抓到了一个真缺陷** —— history.js 的
    DELETE_CONFIRM_KEYS 没跟着补，那两个状态掉进通用文案「记录不可恢复」，
    删一个正在补漏的任务不会有任何警告。补文案的同时把这段登记留下：这张表
    的键集合与后端活动态是**等号**，不是「包含」。
    """
    from src.i18n.catalog import MESSAGES

    active = _active_statuses()
    src = _strip_js_comments(_js('history.js'))
    m = re.search(r'const DELETE_CONFIRM_KEYS\s*=\s*\{(.*?)\};', src, re.S)
    assert m, 'history.js 里找不到 DELETE_CONFIRM_KEYS —— 状态感知的文案表没了'
    pairs = dict(re.findall(r"(\w+)\s*:\s*'([\w.]+)'", m.group(1)))
    assert set(pairs) == active, (
        f'DELETE_CONFIRM_KEYS 覆盖的是 {sorted(pairs)}，后端的未终结状态是 '
        f'{sorted(active)} —— 对不上的那些状态会拿到不带警告的通用文案'
    )

    # 每个活动态都得说到自己那件事，且三句彼此不同（共用一句 = 状态感知白做）
    keyword = {'running': '正在运行', 'retrying': '补漏', 'pending': '排队',
               'paused': '已暂停', 'pending_decision': '等你决定'}
    assert set(keyword) == active, (
        f'关键词表 {sorted(keyword)} 与后端活动态 {sorted(active)} 脱节 —— 本测试已失效'
    )
    seen = set()
    for status, key in sorted(pairs.items()):
        assert key in MESSAGES, f'DELETE_CONFIRM_KEYS[{status}] 指向不存在的键 {key}'
        zh = MESSAGES[key]['zh']
        assert keyword[status] in zh, (
            f'{status} 的确认文案里没有「{keyword[status]}」，实际是 {zh!r} —— '
            '用户看不出这个任务现在处于什么处境'
        )
        assert MESSAGES[key]['en'], f'{key} 缺英文'
        seen.add(zh)
    assert len(seen) == len(pairs), (
        f'{len(pairs)} 个活动态只用了 {len(seen)} 句文案 —— 共用一句就等于没有状态感知'
    )

    # 终态（completed / failed）走通用文案，且通用文案里不许混进活动态的字眼
    generic = 'js.history.confirm.delete_task'
    assert generic in MESSAGES, f'通用删除文案 {generic} 不在 catalog 里'
    assert f"'{generic}'" in src, (
        f'history.js 不再引用 {generic} —— 终态任务没有兜底文案'
    )
    for word in sorted(set(keyword.values())):
        assert word not in MESSAGES[generic]['zh'], (
            f'通用文案里出现了「{word}」：{MESSAGES[generic]["zh"]!r} —— '
            '已完成 / 失败的任务会被警告「它还在跑」'
        )
    body = _fn('deleteTask', 'history.js')
    assert 'DELETE_CONFIRM_KEYS[' in body, (
        'deleteTask 没有查 DELETE_CONFIRM_KEYS —— 表定义了但没人用，文案不会变'
    )


def test_background_artifact_cleanup_is_reported_to_the_user():
    """响应带 files_deferred 时要换一句 toast，告诉用户产物在后台清。

    不告知的后果：删掉一个跑了两小时的等高线任务、勾了删产物，界面说
    「任务已删除」，用户转头去文件管理器发现几十 GB 还在 —— 他分不清该等
    还是该手删。

    末尾那条负向断言钉的是**判据形态**：files_deferred 的语义是「有没有产物
    要延后删」（后端判据是 artifact_dir is not None），没要求删产物时这个字段
    **根本不下发**。写成 `=== false` / `!== false` 的话，最常见的那条路径
    （键不存在）会掉进错误的分支。
    """
    body = _fn('deleteTask', 'history.js')
    assert 'response.json()' in body, (
        'deleteTask 没有解析响应体 —— files_deferred 拿不到，后台清理无从告知'
    )
    assert 'files_deferred' in body, (
        'deleteTask 没有读 files_deferred —— 产物在后台删这件事用户看不见'
    )
    assert "t('js.history.toast.deleted_files_deferred')" in body, (
        '延后清理没有专属 toast —— 用户看到的还是那句平淡的「任务已删除」'
    )
    assert "t('js.history.toast.deleted')" in body, (
        '普通删除的 toast 没了 —— 快路径（同步删完）不该说成「正在后台清理」'
    )
    assert not re.search(r'files_deferred\s*[!=]==', body), (
        'files_deferred 被拿去做全等比较 —— 这个字段在「没要求删产物」时根本'
        '不出现，必须按「键不存在 = 默认路径」处理'
    )


def test_delete_confirm_texts_are_bilingual_and_distinct():
    """删除流程新增的每条文案中英都要有，且英文不是把中文抄过去。

    tests/test_i18n.py 的双向闭合检查只管「键有没有人引用」，管不了文案本身。
    """
    from src.i18n.catalog import MESSAGES

    src = _strip_js_comments(_js('history.js'))
    keys = sorted(set(re.findall(
        r"t\('(js\.history\.(?:confirm\.delete|toast\.deleted)[\w.]*)'\)", src,
    )) | set(re.findall(r"'(js\.history\.confirm\.delete_task_\w+)'", src)))
    assert len(keys) >= 6, (
        f'只从 history.js 里扫到 {len(keys)} 个删除相关的文案键（{keys}）—— '
        '本测试已失效（单框方案有 1 个标题 + 4 句正文 + 1 个勾选框标签 + 2 条 toast）'
    )
    for key in keys:
        entry = MESSAGES.get(key)
        assert entry, f'{key} 被 history.js 引用但 catalog 里没有'
        assert entry['zh'] and entry['en'], f'{key} 中英缺一'
        assert entry['zh'] != entry['en'], f'{key} 的英文就是中文原文'
        assert not re.search(r'[\u4e00-\u9fff]', entry['en']), (
            f'{key} 的英文里还有中文：{entry["en"]!r}'
        )


def test_terrain_detail_shows_the_preset_actually_used():
    """本地地形任务详情必须显示实际用的档位与法线状态。

    上传表单/「处理」弹窗是用户**唯一能亲手选档位**的入口，切完回来查不到自己
    当时选了什么更说不过去。渲染收口在 terrainPresetRowsHtml。
    （DEM 下载任务详情曾有同款面板，随「切片收敛成独立任务」整块撤掉。）

    只断言字符串 "quality" 太弱：history.js 里另有底图/样式相关的同名词。
    这里锁的是**字段读取形态** `row.quality` / `row.vertex_normals`，
    并且要在 terrainPresetRowsHtml 这一个函数体内。
    """
    body = _fn('terrainPresetRowsHtml', 'history.js')

    assert re.search(r'\brow\.quality\b', body), (
        'terrainPresetRowsHtml 没有读 quality 字段 —— 面板不显示实际档位'
    )
    assert re.search(r'\brow\.vertex_normals\b', body), (
        'terrainPresetRowsHtml 没有读 vertex_normals 字段 —— 法线开关看不见'
    )
    # 光有「读了字段」还不够：把模板改成 `${maxzoom}` 也能让上面两条全绿，
    # 而面板上两行显示的都是层级数。这里钉「算出来的值确实进了返回的 HTML」，
    # 且各自跟在自己的标题词后面（换成对方的值同样是错的）。
    assert re.search(r"quality_label'\)\}: \$\{quality\}", body), (
        '档位那一行没有插值算出来的 quality —— 显示的是别的东西'
    )
    assert re.search(r"normals_label'\)\}: \$\{normals\}", body), (
        '法线那一行没有插值算出来的 normals —— 显示的是别的东西'
    )
    # 详情面板得真的调它，且结果要落到容器上：`'detailTerrainInfo' in body`
    # 只证明这个 id 被提过一次，容器与载荷必须钉在同一条断言里。
    assert 'terrainPresetRowsHtml(task)' in _fn('viewTaskDetails', 'history.js'), (
        '本地地形任务详情没有渲染档位/法线两行 —— 用户唯一能选档位的入口回显不了'
    )
    assert re.search(
        r"getElementById\('detailTerrainInfo'\)\.innerHTML\s*=\s*"
        r'terrainPresetRowsHtml\(task\)',
        _fn('viewTaskDetails', 'history.js'),
    ), (
        'viewTaskDetails 没有把 terrainPresetRowsHtml(task) 赋给 detailTerrainInfo —— '
        '本地地形详情的档位/法线两行是空的'
    )


def _inline_js_locals(expr, body):
    """把 `${localFoo}` 展开成 localFoo 在函数体里的定义式（只展开一层）。

    详情面板的两段值都先落到局部常量上（`localTerrainActualMaxzoom` /
    `localTerrainBaseMaxzoom`，那样读起来才清楚），拿分支原文做断言只看得见
    别名，看不见到底插的是哪一列 —— 把别名改指到 task.maxzoom 上，下面
    「实际层级那一段不许出现 task.maxzoom」照样全绿。
    """
    out = expr
    for name in re.findall(r'\$\{([A-Za-z_$][\w$]*)\}', expr):
        init = re.search(
            r'\b(?:const|let)\s+%s\s*=\s*(.*?);' % re.escape(name), body, re.S)
        if init:
            out = out.replace('${%s}' % name, '${%s}' % init.group(1))
    return out


def test_terrain_detail_shows_the_level_actually_tiled():
    """本地地形详情显示的层级必须是产物事实（effective_maxzoom），不是请求的基准值。

    `maxzoom` 那一列存的是用户填的基准层级，precision/speed 两档下它比实际切到
    的层级差一级。改动前面板把 `0 - ${task.maxzoom}` 当成一个精确范围显示 ——
    那是具体的错数字，不是「可以推导」。

    落库那一半由 tests/test_terrain_api.py::
    test_speed_preset_persists_the_level_it_actually_tiled 钉（它还顺带核对
    layer.json）；这里钉前端真的读了那一列。

    登记（自动层级）：退回分支自己又分了两态（手填的数 / 「自动」挡的哨兵），
    两段值于是都走局部常量，下面先展开别名再断言。哨兵那一态由
    test_terrain_detail_translates_the_auto_maxzoom_sentinel 单独钉。
    """
    # 层级住在通用的 Zoom 那一格（`0 - N`）：必须优先取实际值，而且拿不到
    # 实际值、退回基准值时，**文字本身**必须说出这是基准值。那一格的标签写死在
    # templates/base.html 里换不掉，只能把限定词缀在值后面。只挂一句 title 不算数：
    # 悬停在触摸设备上根本不存在、键盘也够不着，而两种情况都渲染成 `0 - 14`，
    # 用户连「这里有话要说」都看不出来 —— 那正是「看起来确定的错值」。
    view = _fn('viewTaskDetails', 'history.js')
    zoom_exprs = [
        expr for expr in re.findall(
            r"getElementById\('detailZoom'\)\.textContent\s*=(.*?);", view, re.S)
        # 大小写不敏感：值走局部别名之后，这条表达式里出现的是
        # `localTerrainActualMaxzoom` 这种驼峰名，按 'maxzoom' 原样筛一处都筛
        # 不到。那不是静默空转 —— 下一行的 `len(zoom_exprs) == 1` 当场就红并
        # 报「本测试已失效」；筛多了也撞在同一条断言上。
        if 'maxzoom' in expr.lower()
    ]
    assert len(zoom_exprs) == 1, (
        f'viewTaskDetails 里涉及 maxzoom 的 detailZoom 赋值有 {len(zoom_exprs)} 处，'
        '期望 1 处 —— 本测试已失效'
    )
    branches = re.findall(r'`[^`]*`', zoom_exprs[0])
    assert len(branches) == 2, (
        f'本地地形的层级只拼出 {len(branches)} 段文本 —— 实际层级与基准层级必须是'
        '两段**不同的文字**；`0 - ${task.effective_maxzoom ?? task.maxzoom}` 这种'
        '写法两种情况长得一模一样，只有 title 能区分'
    )
    branches = [_inline_js_locals(b, view) for b in branches]
    labelled = [b for b in branches if 'js.history.terrain.maxzoom_base_label' in b]
    assert len(labelled) == 1, (
        '恰好一段文字要带「基准层级」限定词：两段都带 = 把实际值也说成基准值，'
        '一段都不带 = 退回「只有悬停能区分」'
    )
    assert 'task.maxzoom' in labelled[0], (
        '带「基准层级」限定词的那一段显示的不是 task.maxzoom —— 标签与值对不上'
    )
    # 上一条被别名展开放松了：labelled[0] 展开后连内层三元的**条件**
    # （`task.maxzoom === TERRAIN_AUTO_MAXZOOM_SENTINEL`）一起进来了，于是把手动挡
    # 那一支的显示值换成 `${task.effective_maxzoom} (${t(base_label)})` 照样全绿 ——
    # 而那正是这条断言声称要防的「标签与值对不上」。判定限回模板串本身：
    # `[^`]*` 不跨反引号，值与限定词必须落在同一个 template literal 里。
    assert re.search(r'\$\{task\.maxzoom\}[^`]*maxzoom_base_label', labelled[0]), (
        '「基准层级」限定词旁边插的不是 ${task.maxzoom} —— 那一格会顶着'
        f'「基准层级」的名头显示别的列：{labelled[0]}'
    )
    actual_branch = [b for b in branches if b is not labelled[0]][0]
    assert 'task.maxzoom' not in actual_branch, (
        '不带限定词的那一段插的仍是基准值 —— 它顶着「实际层级」的名头显示错数字'
    )
    ref = re.search(r'\$\{([\w.]+)\}', actual_branch)
    assert ref, '实际层级那一段不是插值 —— 本测试已失效'
    if ref.group(1) != 'task.effective_maxzoom':
        # _inline_js_locals 只展开一层；再深一层的别名链在这里兜底 ——
        # 允许取局部别名（读起来更清楚），但别名必须真的来自那一列。
        assert re.search(
            r'\b(?:const|let)\s+%s\s*=\s*task\.effective_maxzoom\b' % re.escape(ref.group(1)),
            view,
        ), (
            f'实际层级那一段插的是 {ref.group(1)}，而它不是从 task.effective_maxzoom 取的'
        )


def test_terrain_detail_translates_the_auto_maxzoom_sentinel():
    """自动挡的哨兵 -1 不许原样渲染成 `0 - -1`。

    「最大切片层级」多了「自动」一挡（按源数据分辨率现算基准层级），落库表示
    是哨兵 -1（`geo_validation.AUTO_MAXZOOM_SENTINEL`），而且它已经是**出厂
    默认** —— 配置里填了坏值软退回自动，落库的同样是这个数。上一条用例只钉了
    「拿不到实际值就退回 task.maxzoom」，退回的那一格于是把哨兵直接印在界面上。

    哨兵值必须是一个**具名常量**、且与后端那份逐字相等（ui.js 的
    TILE_PATH_PREFIXES / TILE_HEALTH_PATH 是同一套写法：镜像常量 + 相等性断言
    防漂移）。散在表达式里的裸 -1 改后端时没人搜得到，而漂移的后果就是界面上
    冒出 `0 - -1`。
    """
    from src.i18n.catalog import MESSAGES
    from src.services.geo_validation import AUTO_MAXZOOM_SENTINEL

    src = _strip_js_comments(_js('history.js'))
    m = re.search(r'\bconst\s+([A-Z][A-Z0-9_]*SENTINEL)\s*=\s*(-?\d+)\s*;', src)
    assert m, (
        'history.js 里没有具名的自动层级哨兵常量 —— 裸 -1 散在表达式里，'
        '后端改哨兵时没人搜得到'
    )
    assert int(m.group(2)) == AUTO_MAXZOOM_SENTINEL, (
        f'JS 侧哨兵是 {m.group(2)}，后端 AUTO_MAXZOOM_SENTINEL 是 '
        f'{AUTO_MAXZOOM_SENTINEL} —— 两边一漂，自动挡的作业就把哨兵当层级显示'
    )

    view = _fn('viewTaskDetails', 'history.js')
    assert re.search(r'task\.maxzoom\s*===\s*%s\b' % m.group(1), view), (
        f'详情面板没有拿 {m.group(1)} 认过 task.maxzoom —— 自动挡的作业'
        '（出厂默认）会把哨兵当成基准层级显示'
    )
    # 认出来之后显示的必须是文案。键写成完整字面量：拼接出来的键会被
    # tests/test_i18n.py 的双向闭合当成无人引用判死。
    assert "t('js.history.terrain.maxzoom_auto')" in view, (
        '认出哨兵之后没换成文案 —— 那一格显示的还是那个数'
    )
    # 三元的**极性**同样要钉：哨兵比较在、两个显示值也都在，把两支对调照样
    # 全绿 —— 而界面会在自动挡（出厂默认！）上显示 `0 - -1 (基准层级)`，
    # 手填层级的作业反倒显示「按源数据自动」。两种都是看起来确定的错值。
    assert re.search(
        r"task\.maxzoom\s*===[^?]*\?\s*t\('js\.history\.terrain\.maxzoom_auto'\)\s*:\s*`",
        view,
    ), (
        '哨兵那个三元的两支对调了：认出哨兵的那一支必须是 maxzoom_auto 文案、'
        '另一支才是带层级数的模板串'
    )
    entry = MESSAGES['js.history.terrain.maxzoom_auto']
    assert entry['zh'] and entry['en'], 'js.history.terrain.maxzoom_auto 中英缺一'
    assert '-1' not in entry['zh'] and '-1' not in entry['en'], (
        f'文案里直接写着哨兵本身，等于换了个地方漏出来：{entry}'
    )


def test_terrain_detail_hint_follows_which_base_is_shown():
    """悬停说明必须跟着值走：自动挡下不存在「提交时填的那个数」。

    maxzoom_base_hint 整句是围绕「这是你提交时填的基准值、不是产物事实」写的。
    自动挡下基准根本不是一个数字（切片时按源数据分辨率现算），那句话逐字都不
    成立 —— 把它挂在「自动」上，等于告诉用户他填过一个他没填过的数。
    """
    from src.i18n.catalog import MESSAGES

    view = _fn('viewTaskDetails', 'history.js')
    m = re.search(r"getElementById\('detailZoom'\)\.title\s*=(.*?);", view, re.S)
    assert m, 'viewTaskDetails 里找不到 detailZoom 的 title 赋值 —— 本测试已失效'
    title_expr = m.group(1)
    for key in ('js.history.terrain.maxzoom_base_hint',
                'js.history.terrain.maxzoom_auto_hint'):
        assert f"'{key}'" in title_expr, (
            f'title 表达式里没有 {key} —— 两种基准来源共用同一句说明，'
            '必有一种是假的'
        )
        entry = MESSAGES[key]
        assert entry['zh'] and entry['en'], f'{key} 中英缺一'
    auto = MESSAGES['js.history.terrain.maxzoom_auto_hint']
    base = MESSAGES['js.history.terrain.maxzoom_base_hint']
    assert auto['zh'] != base['zh'], '两句说明逐字相同 —— 那就没必要分成两个键'
    assert '填' not in auto['zh'], (
        f'自动挡的说明里还在说用户「填」过基准层级：{auto["zh"]}'
    )
    # 「两个键都在」不管极性：把两支对调，两个键照样都在，而每一挡挂到的都是
    # 另一挡的说明 —— 自动挡的作业被告知「这是你提交时填的那个数」，而他没填过。
    assert re.search(
        r"\?\s*t\('js\.history\.terrain\.maxzoom_auto_hint'\)\s*:\s*"
        r"t\('js\.history\.terrain\.maxzoom_base_hint'\)",
        title_expr,
    ), (
        f'哨兵三元的两支说明对调了：认出哨兵的那一支必须挂 maxzoom_auto_hint：'
        f'{title_expr}'
    )
    # 外层 guard 也无人钉。删掉 `task.effective_maxzoom == null` 这半句，切完的
    # 作业（有实际层级、值那一格显示的是产物事实）会挂上「作业切完后这里会换成
    # 实际层级」这句已经不成立的话。既有的 test_zoom_tooltip_reset_is_unconditional
    # 只钉这条赋值的花括号深度，对条件本身一无所知。
    # `==` 不是笔误：null 与 undefined 都算「还不知道切到第几级」。
    assert re.search(
        r"taskType\s*===\s*'local_terrain'\s*&&\s*task\.effective_maxzoom\s*==\s*null",
        title_expr,
    ), (
        f'title 的外层 guard 不再是「本地地形 且 还没有实际层级」—— 少了后半句，'
        f'切完的作业也会挂上「切完后会换成实际层级」这句假话：{title_expr}'
    )


def test_the_auto_sentinel_is_recognised_in_the_value_and_the_tooltip_alike():
    """哨兵比较在详情面板里有两处（值那一格、title 那一格），必须一起在。

    上面两条用例各看各的一格：删掉另一格的哨兵比较，两条都还是绿的。而这两格
    说的是同一件事，改一处漏一处就是自相矛盾 —— 值显示 `0 - 14 (基准层级)`、
    悬停却说「这一挡的基准是切片时按源数据现算的，你没填过这个数」；反过来则是
    值显示「按源数据自动」、悬停说「这是你提交时填的那个数」。两种都零报错。
    """
    view = _fn('viewTaskDetails', 'history.js')

    value_exprs = [
        expr for expr in re.findall(
            r"getElementById\('detailZoom'\)\.textContent\s*=(.*?);", view, re.S)
        # 与 test_terrain_detail_shows_the_level_actually_tiled 同一把筛子：
        # 值走局部别名，按 'maxzoom' 原样筛一处都筛不到（下一行会当场报「已失效」）。
        if 'maxzoom' in expr.lower()
    ]
    assert len(value_exprs) == 1, (
        f'涉及 maxzoom 的 detailZoom 赋值有 {len(value_exprs)} 处，期望 1 处 —— '
        '本测试已失效'
    )
    title = re.search(
        r"getElementById\('detailZoom'\)\.title\s*=(.*?);", view, re.S)
    assert title, 'viewTaskDetails 里找不到 detailZoom 的 title 赋值 —— 本测试已失效'

    # 值那一格的哨兵比较藏在局部常量的定义式里，先展开一层别名再判。
    places = (('值那一格', _inline_js_locals(value_exprs[0], view)),
              ('title 那一格', title.group(1)))
    for label, expr in places:
        assert re.search(r'task\.maxzoom\s*===\s*[A-Z][A-Z0-9_]*SENTINEL\b', expr), (
            f'{label}没有拿具名哨兵认过 task.maxzoom —— 两格必须同时认，'
            f'只认一格的话自动挡的作业会一边说自动、一边把 -1 当层级：{expr}'
        )


def test_terrain_preset_is_shown_as_words_not_the_raw_enum():
    """三个档位值必须经查表换成人话，不能把 precision/balanced/speed 直接吐给用户。

    后端存的是枚举字面量（TILING_QUALITY_OFFSETS 的键）。直接插进 innerHTML
    的话，中文界面上会冒出三个英文单词，而且 "balanced" 对用户毫无信息量 ——
    他要知道的是「比基准层级多切一级 / 少切一级」（参照点必须是基准层级：
    「比默认」会随 terrain_quality_preset 配成哪一档而漂移，见 catalog 里的说明）。
    """
    from src.i18n.catalog import MESSAGES

    src = _strip_js_comments(_js('history.js'))

    # 查表必须覆盖全部三档，且每一档都映到一个完整的键字面量
    # （拼接式的键会被 tests/test_i18n.py 的孤儿检查判死）。
    for preset in ('precision', 'balanced', 'speed'):
        m = re.search(
            r"\b%s\s*:\s*'(js\.history\.terrain\.[\w.]+)'" % preset, src,
        )
        assert m, f'档位 {preset} 没有对应的文案键 —— 它会以英文原文漏给用户'
        entry = MESSAGES.get(m.group(1))
        assert entry, f'{m.group(1)} 被 history.js 引用但 catalog 里没有'
        assert entry['zh'] and entry['en'], f'{m.group(1)} 中英缺一'
        assert preset not in entry['zh'], (
            f'{m.group(1)} 的中文就是枚举字面量本身：{entry["zh"]!r}'
        )

    # 查表的下标必须**就是** row.quality。只钉 `\brow\.quality\b` 挡不住把这里
    # 拼错成 row.qualityy：下一行的兜底表达式里还有一个 row.quality，正则照样
    # 命中，而每个作业都会渲染出生英文枚举 —— 正是本用例声称要防的那件事。
    body = _fn('terrainPresetRowsHtml', 'history.js')
    assert re.search(r'TERRAIN_QUALITY_KEYS\[row\.quality\]', body), (
        '档位查表的下标不是 row.quality —— 查不到就走兜底，界面上是英文枚举原文'
    )
    # 裸下标会命中 Object.prototype：quality === 'constructor' 时取到构造函数、
    # 被当成「认得出的档位」，最后绕过 escapeHtml 插进 innerHTML。
    assert re.search(
        r'hasOwnProperty\.call\(\s*TERRAIN_QUALITY_KEYS\s*,\s*row\.quality\s*\)', body,
    ), '档位查表没有挡原型链 —— constructor/__proto__/toString 会走进「认得出」分支'
    # 查到了键就必须走 t()。缺这一条的话，把显示表达式改回
    # `escapeHtml(String(row.quality))` 也全绿 —— 查表建好了却没人用，
    # 界面上照样是三个英文枚举词。
    assert re.search(r'const quality = qualityKey \?\s*t\(qualityKey\)', body), (
        '认出来的档位没有过 t() —— 查表白建了，界面上还是 precision/balanced/speed'
    )

    # vertex_normals 是**三态**，不是布尔：NULL = 这一行没有记录过法线状态
    # （列是后加的，加列之前切的作业整列为 NULL），0 = 明确关闭，1 = 明确开启。
    # 改前这里是 `!!row.vertex_normals`，NULL 被压成 false，面板于是用
    # 「未开启（无光照数据）」这种确定语气去描述一件没有记录的事，而且方向恰好
    # 说反 —— 加列之前法线是默认开着的。
    assert '!!row.vertex_normals' not in body, (
        'vertex_normals 又被强转成布尔 —— NULL（没记录）会被说成「未开启」，'
        '那是一个看起来确定的错值'
    )
    recorded = re.search(r'const (\w+) = row\.vertex_normals != null', body)
    assert recorded, (
        '法线状态没有把「没记录」与「明确关闭」分开 —— 三态塌回两态，'
        'NULL 会被当成关闭'
    )
    off = re.search(
        r'const (\w+) = %s && !row\.vertex_normals' % recorded.group(1), body,
    )
    assert off, '「明确关闭」没有以「这一行有记录」为前提 —— 没记录的行会走进关闭分支'
    assert re.search(
        r"!%s\s*\n?\s*\?\s*'js\.history\.terrain\.normals_unknown'" % recorded.group(1),
        body,
    ), '没记录的行没有落到「未知」文案上'

    # 「未开启」不能只是一个状态词：法线是烘焙进瓦片的，关掉之后 Cesium 的
    # 光照开关对整幅场景失效，事后改配置也救不回来。这个后果必须出现在面板上，
    # 且**只**在明确关闭时出现 —— 挂到「未知」那一档，就是拿一件没记录的事
    # 去吓用户。
    hint = 'js.history.terrain.normals_off_hint'
    assert MESSAGES.get(hint) and MESSAGES[hint]['zh'] and MESSAGES[hint]['en'], (
        f'{hint} 在 catalog 里缺失或缺语种'
    )
    assert re.search(r"normalsTitle = %s\s*\n?\s*\?\s*` title=" % off.group(1), body), (
        '关闭法线的后果提示不是「仅在明确关闭时」给出 —— 开启或未记录的作业也会被警告'
    )
    assert hint in body, f'terrainPresetRowsHtml 没有引用 {hint} —— 关掉法线的后果没人告诉用户'

    # 法线三态各有各的文案：塌成两态就等于用确定语气说不知道的事。
    normals_keys = ('js.history.terrain.normals_on',
                    'js.history.terrain.normals_off',
                    'js.history.terrain.normals_unknown')
    for key in normals_keys:
        assert f"'{key}'" in src, f'history.js 没有引用 {key} —— 法线状态缺一态'
        entry = MESSAGES.get(key)
        assert entry and entry['zh'] and entry['en'], f'{key} 在 catalog 里缺失或缺语种'
    assert len({MESSAGES[k]['zh'] for k in normals_keys}) == 3, (
        '法线三态里有两态文案一模一样 —— 显示了等于没显示'
    )
    # 「未知」那一档只描述这一行的记录状态，不许顺带对产物下结论：
    # 写成「未知（可能未开启）」就又变成一个看起来确定的猜测。
    assert '开启' not in MESSAGES['js.history.terrain.normals_unknown']['zh'], (
        '「未知」的文案里出现了「开启」—— 不知道就别对法线开关下任何结论'
    )

    # 两行都得有标题词，否则面板上只是两个孤零零的值。
    for key in ('js.history.terrain.quality_label', 'js.history.terrain.normals_label'):
        assert f"'{key}'" in src, f'history.js 没有引用 {key} —— 显示的值没有标题'
        assert MESSAGES.get(key), f'{key} 被 history.js 引用但 catalog 里没有'


# 全部「状态/样式值 -> 显示值」的查表点。表是对象字面量，继承 Object.prototype，
# 所以下标必须先过 hasOwnProperty —— 这条约定 history.js 的档位表
# （terrainPresetRowsHtml）与删除确认表（confirmDeleteTask）已经在守，
# 这里把剩下四处拉齐。
#
# 不是 XSS：颜色落在 `class="badge bg-${...}"` 里，`String(Object)` 不含引号、
# 闭不掉属性；状态文案两个调用点都过了 escapeHtml；样式文案进的是 textContent。
# 是**正确性**缺陷：`|| 兜底` 拦不住原型上取到的函数值（函数是真值），
# 徽章 class 变成 `bg-function Object() { [native code] }` 静默退化成无色，
# 描边色查缓存得到 undefined，样式格里则是一段函数源码冒充样式名。
_PROTOTYPE_SAFE_LOOKUPS = (
    ('task_status.js', 'getStatusColor', 'colors', 'status'),
    ('task_status.js', 'getStatusText', 'texts', 'status'),
    ('task_status.js', 'getStatusStroke', '_STATUS_STROKE_TOKENS', 'status'),
    ('history.js', 'getStyleText', 'styles', 'style'),
)


def test_display_lookups_guard_the_prototype_chain():
    """每一处查表的下标都必须先过 hasOwnProperty，且不许留下第二处裸下标。

    只断言「函数体里出现过 hasOwnProperty」是不够的：补一句守卫、旧的那行
    `return colors[status] || 'secondary'` 留着不删，函数照样走裸下标，
    而断言全绿。所以按语句（分号切）逐条查：凡是出现 `表[下标]` 的语句，
    同一条语句里必须有对同一张表的守卫。
    """
    for js_name, fn, table, key in _PROTOTYPE_SAFE_LOOKUPS:
        body = _fn(fn, js_name)
        subscript = f'{table}[{key}]'
        # 自检：查表点本身还在。表被改名/查表被删的话下面的循环会零轮空转。
        assert subscript in body, (
            f'{js_name} 的 {fn} 里找不到 {subscript} —— 查表点变了，本测试已失效'
        )
        for stmt in body.split(';'):
            if subscript not in stmt:
                continue
            assert re.search(
                r'hasOwnProperty\.call\(\s*%s\s*,\s*%s\s*\)' % (re.escape(table), key),
                stmt,
            ), (
                f'{js_name} 的 {fn} 里有一处 {subscript} 没挡原型链：\n'
                f'{stmt.strip()}\n'
                f"{key} === 'constructor' / '__proto__' / 'toString' 会取到原型上的"
                '成员，那是个真值，`||` 兜底永远轮不上'
            )


def test_zoom_tooltip_reset_is_unconditional():
    """详情模态框的 Zoom 那一格，title 的清空必须在任务类型分支**之外**。

    模态框复用同一棵 DOM：先看一个本地地形任务（title 是「这是基准层级…」），
    再看一个 DEM 任务，如果清空那句被挪进 local_terrain 分支，DEM 那一行就
    顶着上一个任务留下的悬停说明 —— 一句针对别的任务的解释，粘在一个完全
    不适用的数字上。

    源码里有注释说明这件事，但此前没有任何断言：把那两行挪进分支里，
    整个测试目录全绿。这里按花括号深度钉「它是 if/else 链的兄弟，不是某一
    支的孩子」，而不是查字符串在不在 —— 挪位置不会改变字符串。
    """
    view = _fn('viewTaskDetails', 'history.js')

    def depth_at(needle):
        assert view.count(needle) >= 1, f'viewTaskDetails 里找不到 {needle} —— 本测试已失效'
        i = view.index(needle)
        return view.count('{', 0, i) - view.count('}', 0, i)

    chain = depth_at("if (taskType === 'dem')")
    # 自检：分支体确实比链头深一层，否则下面的比较量的不是「在不在分支里」。
    inside = depth_at("t('js.history.meta.local_terrain')")
    assert inside == chain + 1, (
        f'分支体深度 {inside} 不等于链头深度 {chain} + 1 —— 花括号计数已失效'
        '（有人给分支加了块级作用域？）'
    )
    reset = depth_at("getElementById('detailZoom').title")
    assert reset == chain, (
        f'detailZoom 的 title 赋值在深度 {reset}（if/else 链在 {chain}）—— '
        '它被关进某一个任务类型分支里了。其余类型不再清空，上一个本地地形任务'
        '留下的「这是基准层级」会粘在 DEM/等高线/瓦片任务的层级上'
    )


# ---------------------------------------------------------------------------
# 缺块面收尾：带洞成品的按钮组 + 明细读取失败的出路
# ---------------------------------------------------------------------------

def _row_template():
    """TaskRow 的 ROW_TEMPLATE（反引号模板串），**已剥掉 HTML 注释**。

    与本文件 _strip_js_comments 同一理由，只是注释语法换成了 `<!-- -->`：
    模板里的注释逐字讨论被改掉的那些判据（「不许再写 status === 'completed'」
    本身就要把那个字面量打出来），不剥的话「模板里不许出现 X」类断言会把
    解释 X 的那句话当成违规。
    """
    src = _js('task_list.js')
    i = src.index('const ROW_TEMPLATE = `')
    j = src.index('`', i + len('const ROW_TEMPLATE = `'))
    return re.sub(r'<!--.*?-->', '', src[i:j], flags=re.S)


def test_row_buttons_never_hardcode_the_completed_status():
    """行上的按钮不许再拿 `status === 'completed'` 当判据。

    实测缺陷：预览按钮写死 `task.status === 'completed'`，而同一行的导出按钮
    走 isExportable（白名单含 completed_with_gaps）。一条 completed_with_gaps
    的行因此「导出 MBTiles」在、「在地图上预览」不在 —— 同一份产物，两颗
    按钮给出互相矛盾的答案。而带洞成品的瓦片目录是真的出了的
    （accept_gaps 会把严格模式当初拒绝的拼接/复制补跑完），
    task_store.js 自己的注释就写着排除它等于让「接受缺块」毫无意义。

    钉两件事：模板里一个 'completed' 字面量都不许有（下一颗按钮不会重蹈），
    且判据必须是 store 那份唯一的成功态集合。
    """
    tmpl = _row_template()
    assert "'completed'" not in tmpl, (
        'ROW_TEMPLATE 里还有 `completed` 状态字面量 —— 状态集合散进模板，'
        '加一个成功态就要满模板 grep，而漏掉一处的表现是「按钮不出现」，'
        '没有任何报错'
    )
    comp = _strip_js_comments(_js('task_list.js'))
    assert re.search(r'isSuccessful\s*\(\s*\)\s*\{[^}]*SUCCESSFUL_STATUSES', comp), (
        'TaskRow 没有一个读 store.SUCCESSFUL_STATUSES 的 isSuccessful —— '
        '「产出可用（可能带洞）」的口径必须只有一份，在 task_store.js'
    )
    for label, marker in (
        ('预览', 'canPreview'),
        ('处理（DEM 转切片）', 'canProcessDem'),
    ):
        m = re.search(r'v-if="' + marker + r'[^"]*"', tmpl)
        assert m, f'{label}按钮的 v-if 找不到 —— 本测试已失效'
        assert 'isSuccessful' in m.group(0), (
            f'{label}按钮的判据是 {m.group(0)} —— 没走 isSuccessful，'
            '它迟早与导出那颗给出互相矛盾的答案'
        )


def test_a_failed_gap_fetch_leaves_the_row_an_exit():
    """GET /gaps 失败必须落进 store，并在行上换出错误态 + 「重试」。

    实测缺陷：ensureGapSummary 的 catch 只 console.error 一句就 return null。
    它的调用方只有行组件那三个触发点（mounted 与 task.status /
    task.gap_tiles 两条 watch），一次超时之后它们谁都不会再响 —— keyed diff
    复用组件实例所以不 mount，两条 watch 的值也没变。于是那一行永久停在
    「正在读取缺块明细…」，只有整页刷新救得回来。

    修法是「记下失败 + 用户手动重试」，**不是**退避轮询：缺块明细是读一次
    就够的静态数据，为一次超时给每条带缺块的行挂一个后台定时请求不成比例。
    这条同时守住「不许改成无条件轮询」——  syncGaps 仍必须按「摘要还没到」
    才拉（由 test_page_refresh_preserves_the_fetched_gap_breakdown 钉）。

    第三件事：整页替换要把失败标记一起搬过去。不搬的话 replaceAll 会把行打回
    「正在读取…」，而上面那三个触发点在这种替换下同样一个都不响 —— 等于换条
    路径把永久转圈放回来。
    """
    center = _strip_js_comments(_js('task_center.js'))
    ensure = _js_function_body(center, 'ensureGapSummary')
    catch = ensure[ensure.index('} catch'):]
    assert 'gap_summary_error' in catch and 'commit(' in catch, (
        'ensureGapSummary 的 catch 没把失败写进 store —— 行上看不出「读失败」'
        '与「还在读」有任何区别，而没有任何人会再拉一次'
    )
    assert 'setTimeout' not in ensure and 'setInterval' not in ensure, (
        'ensureGapSummary 里出现了定时器 —— 明细是读一次就够的静态数据，'
        '退避轮询等于给每条带缺块的行挂一个常驻后台请求'
    )

    tmpl = _row_template()
    assert 'gapLoadError' in tmpl and 'retryGaps' in tmpl, (
        '行上没有错误态与「重试」入口 —— 一次超时之后用户除了整页刷新无路可走'
    )
    assert "t('js.gaps.action.retry')" in tmpl, '「重试」按钮没有文案 key'
    comp = _strip_js_comments(_js('task_list.js'))
    assert re.search(r'retryGaps\s*\(\s*\)\s*\{[^}]*ensureGapSummary\(', comp), (
        'retryGaps 没有真的再打一次 GET /gaps'
    )

    carry = _js_function_body(_strip_js_comments(_js('task_store.js')), 'carryClientState')
    assert 'gap_summary_error' in carry, (
        'carryClientState 没搬 gap_summary_error —— loadHistory 一整页替换就把'
        '错误态与「重试」按钮抹掉，行打回「正在读取缺块明细…」并永久卡在那里'
    )


# ---------------------------------------------------------------------------
# 导出格式选择器
#
# 改造前的事实：`POST /api/export/<pipeline>/<id>` 的 body 写死
# `{format:'mbtiles'}`，而后端早已把插件注册的导出器并进了同一条路由 ——
# gpkg 有货也没有任何入口点得到。全仓也没有任何 GET 能列出可用格式（格式表
# 只在 400 的响应体里出现），所以「让用户选」在补上读端点之前做不出来。
# ---------------------------------------------------------------------------


def _pipeline_tile_layout_keys():
    """`artifact_export._PIPELINE_TILE_LAYOUT` 的键，用 ast 解析不 import。

    与 _task_status_values 同一理由：import src.services 会拖 config 的副作用。
    """
    path = os.path.join(ROOT, 'src', 'services', 'artifact_export.py')
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, 'id', None) == '_PIPELINE_TILE_LAYOUT'
                   for t in node.targets):
            continue
        assert isinstance(node.value, ast.Dict), (
            '_PIPELINE_TILE_LAYOUT 不再是字面量 dict —— 本测试已失效')
        return {k.value for k in node.value.keys}
    raise AssertionError(
        'src/services/artifact_export.py 里找不到 _PIPELINE_TILE_LAYOUT')


def test_export_button_pipelines_match_the_backend_table():
    """行上「哪些管线给导出按钮」必须与后端那张瓦片布局表逐字一致。

    改前是 `task.task_type === 'map' || task.task_type === 'contour'` 两个字面量
    写在 computed 里。后端 `_PIPELINE_TILE_LAYOUT` 加一条管线时这里不会报错，
    表现是**按钮不出现** —— 用户产出了成品但没有导出入口，而且没有任何日志。
    """
    comp = _strip_js_comments(_js('task_list.js'))
    m = re.search(r'const\s+EXPORTABLE_PIPELINES\s*=\s*\[([^\]]*)\]', comp)
    assert m, (
        'task_list.js 里没有 EXPORTABLE_PIPELINES 常量 —— 管线清单又散回 '
        'computed 里的状态字面量了'
    )
    listed = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert listed == _pipeline_tile_layout_keys(), (
        f'导出按钮的管线白名单是 {sorted(listed)}，'
        f'后端 _PIPELINE_TILE_LAYOUT 是 {sorted(_pipeline_tile_layout_keys())}'
    )
    assert re.search(
        r'isExportable\s*\(\s*\)\s*\{[^}]*EXPORTABLE_PIPELINES', comp), (
        'isExportable 没读那个常量 —— 常量在、判据还是字面量，等于白建'
    )


def test_export_asks_the_server_which_formats_before_posting():
    """导出前必须先问 `GET .../formats`，且 body 里不许再出现写死的格式名。

    写死的后果不是「默认值选得不好」：插件注册的导出器在界面上**没有入口**，
    后端接了、用户点不到。而格式表只有服务端知道（宿主自带的一种 + 已启用插件
    的 format_id()，还要拿这个任务的产物登记行对照每个导出器的 accepts()），
    前端一半都推不出来。
    """
    body = _fn('exportTask', 'task_center.js')
    assert body.strip(), 'task_center.js 里切不出 exportTask —— 本测试已失效'
    assert '/formats' in _fn('fetchExportFormats', 'task_center.js'), (
        'fetchExportFormats 没打那条格式端点'
    )
    assert 'fetchExportFormats(' in body, (
        'exportTask 没有先问格式就 POST 了'
    )
    assert "'mbtiles'" not in body and '"mbtiles"' not in body, (
        'exportTask 里还有写死的 mbtiles —— 格式必须来自服务端那份清单'
    )
    assert re.search(r'body:\s*JSON\.stringify\(\{\s*format\s*\}\)', body), (
        'POST 的 body 不是把选中的 format 原样发出去'
    )


def test_a_single_format_exports_without_a_dialog():
    """只有一种格式时不许弹框 —— 改造前点一下就导出，那个手感不能因为「以后
    可能有第二种格式」而退化成每次都要确认一遍。"""
    body = _fn('exportTask', 'task_center.js')
    assert re.search(r'formats\.length\s*>\s*1', body), (
        '弹框没有挂在「多于一种」上 —— 一种格式也弹框，或者多种格式也不弹'
    )
    assert re.search(r'if\s*\(\s*!formats\.length\s*\)', body), (
        '零种格式没有单独出口 —— 会拿 undefined 当 format POST 上去，'
        '换来一个 400'
    )
    assert "t('js.export.toast.nothing_to_export')" in body, (
        '零种格式时没有任何提示，按钮看着像没反应'
    )


def test_confirm_select_only_serves_the_export_flow():
    """`opts.select` 只许 task_center.js 的导出流程用。

    与 checkbox 那条同一个失效机制：带 select 时 showConfirm 改 resolve
    `{confirmed, selected}`，而对象**恒为真**。`if (!await showConfirm(...))`
    那种既有写法会静默失效 —— 确认框再也拦不住人，且没有任何报错。
    """
    js_dir = os.path.join(ROOT, 'static', 'js')
    users = set()
    for name in sorted(n for n in os.listdir(js_dir) if n.endswith('.js')):
        if name == 'ui.js':      # 定义端
            continue
        # `select:\s*{` 而不是裸 `select:` —— command_palette.js 的
        # FOCUSABLE 常量里有 `'select:not([disabled])'`，那是 CSS 选择器串。
        if re.search(r'\bselect:\s*\{', _strip_js_comments(_js(name))):
            users.add(name)
    assert users == {'task_center.js'}, (
        f'showConfirm 的 select 选项被 {sorted(users)} 使用 —— 期望只有 '
        'task_center.js。带 select 时 resolve 的是对象（恒为真），'
        '`if (!await showConfirm(...))` 那种写法会静默失效'
    )
    body = _fn('exportTask', 'task_center.js')
    assert 'answer.confirmed' in body, (
        'exportTask 没判 .confirmed —— 取消 / ESC / 点遮罩之后照样会发 POST'
    )


def test_cancelling_the_confirm_never_reports_a_selection():
    """取消时 selected 必须是 null，与 checked 压成 false 同一条规矩。

    调用方只判 confirmed 的话这条无关紧要；可一旦有人写成
    `if (answer.selected) exportIt(answer.selected)`，一个「用户拉了下拉再按
    ESC」的操作就会真的导出。让 ui.js 在源头保证，比要求每个调用方记得判断可靠。
    """
    body = _js_function_body(_strip_js_comments(_js('ui.js')), 'showConfirm')
    assert body.strip(), 'ui.js 的 showConfirm 函数体切出来是空的 —— 本测试已失效'
    assert re.search(r'selected:\s*result\s*\?', body), (
        'showConfirm 的 resolve 没有把 selected 挂在 result 上 —— '
        '用户拉了下拉再按 ESC，取消的结果里仍带着一个选中值'
    )


def test_the_plugin_export_toast_never_claims_a_tile_count():
    """插件导出器的响应里**没有** tile_count，不许套那条带 {count} 的文案。

    套上去的表现是「已导出 MBTiles（0 块瓦片）：/path/x.gpkg」—— 格式名和块数
    两处都在撒谎。分档判据必须是响应里到底有没有那个字段。
    """
    body = _fn('exportTask', 'task_center.js')
    assert re.search(r'result\.tile_count\s*!=\s*null', body), (
        '没有按 tile_count 在不在分档 —— 两条成功文案要么共用一条（插件分支'
        '显示 0 块瓦片），要么按格式名硬编码（下一个插件格式又漏一条）'
    )
    assert "t('js.export.toast.exported')" not in body, (
        '插件分支的文案没带参数 —— 那条模板要 {format} 与 {path}'
    )
    assert "js.export.toast.exported" in body and "js.gaps.toast.exported" in body, (
        '两条成功文案不是都在 —— 少一条就说明有一个分支在用错的模板'
    )