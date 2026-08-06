"""底部状态栏的底图解压进度：markup / JS / CSS / i18n 的源码契约。

⚠️ 本文件所有针对源码的断言**一律先剥注释再匹配**，复用
tests/test_socket_singleton_contract.py 里的 `_strip_js_comments` /
`_strip_template_comments`（那两个函数在原处有自检用例 `test_strip_js_comments`，
不在这里重复）。

为什么必须剥：本项目的注释逐字讨论代码，裸 grep 会被注释满足而给出假绿 ——
把被测代码整段删掉照样通过。三个被测文件全都踩在这个坑上：
  - `static/js/base_terrain_status.js` 的文件头注释里原样写着
    `base_unpack_progress`、`running`、`failed`；
  - `templates/base.html` 的注释里写着元素的用途与位置要求；
  - `static/css/style.css` 那条窄屏规则的注释里**原样写着**
    `.statusbar-item:last-child`（在告诫「不要写回去」）——
    本文件的 `test_narrow_screen_rule_no_longer_depends_on_last_child` 是条否定
    断言，不剥注释的话它永远是红的（注释满足了它），一红就会有人把注释删掉了事。

⚠️⚠️ 剥注释是**必要不充分**条件。本文件初版有三条断言剥了注释仍是假守卫，因为它们
钉的是「全文出现过某个字面量」，而同一个字面量在无关分支里也有一份：
  - `.title` 在三个分支各出现一次 → 删掉 failed 分支那一次（功能的核心卖点）照样绿；
  - `'failed'` 在 `onProgress` 的终态判定里也有一份 → 删光 `render` 的 failed
    分支照样绿；
  - `hidden = true` 出现三次 → 只把「收起」分支那一次改坏照样绿。
所以现在的写法是：**先把被测函数的函数体抠出来，再在函数体内钉赋值/分支的完整形态**
（见 `_function_body`）。变异实测的口径也随之改成「只坏一处」—— 全局替换会连累别的
断言，抓不到真实的故障形态。

CSS 也走 `_strip_js_comments`：CSS 的 `/* */` 与 JS 同形。已逐行比对过它与
`re.sub(r'/\\*.*?\\*/')` 在 style.css 上的输出：行数相同、逐行完全一致。
它在 CSS 上有**两类已知失败**，一响一静，都实测确认过：
  1. `%` 后面跟 `/`（`calc(100% / 3)`）—— `%` 在它的启发式里属于「后面可以跟正则」，
     于是 `/` 被当成正则开头、本行内找不到收尾而**抛 AssertionError**。响亮，安全。
  2. **不加引号**的 `url(...)` 里出现 `//`（`url(http://h/a.png)`、`url(//cdn/x.png)`）
     —— CSS 允许 url 不加引号，而剥离器只在带引号的字符串里保护 `//`，于是本行剩余
     被当行注释**静默删光**（实测第二例连它后面那条 `/* */` 注释一起吃掉）。
     **静默**，而且正是「剥过头 → 否定断言假绿」的路径。
第 2 类由 `test_css_stripper_preconditions_hold` 直接钉住前提；每条 CSS 断言另配
「被测规则块还在」的正向哨兵作为第二道网。
"""
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_socket_singleton_contract import (  # noqa: E402
    _jinja_block_spans, _strip_js_comments, _strip_template_comments)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# get() 与 socket.on() 之间一旦出现这些，注册就跨出了同一个同步块（见 socket.js 文件头）
_DEFERRAL_TOKENS = ("await", "setTimeout", "setInterval", "requestAnimationFrame",
                    "queueMicrotask", "addEventListener", ".then(", "import(")

# 窄屏隐藏规则里不许出现的**位置相关**伪类。原来的 `.statusbar-item:last-child`
# 只是这一类里的一个实例，钉住整类才不会换个巧合再犯。
_POSITIONAL_PSEUDO_RE = re.compile(r":(?:last|first|only)-(?:child|of-type)|:nth-")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _base_html():
    return _strip_template_comments(_read("templates", "base.html"))


def _status_js():
    return _strip_js_comments(_read("static", "js", "base_terrain_status.js"))


def _css():
    return _strip_js_comments(_read("static", "css", "style.css"))


def _function_body(code, name):
    """从剥完注释的 JS 里抠出 `function name(...) { ... }` 的函数体（大括号配对）。

    存在的理由见文件头：`'failed'` / `.title` / `hidden = true` 这些字面量在别的
    函数、别的分支里都另有一份，只在全文里搜「有没有」等于没搜。
    """
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", code)
    assert m, f"找不到函数 {name}（被删了，还是改成了别的形态？）"
    body = _brace_body(code, m.end() - 1, f"函数 {name}")
    assert body.strip(), f"函数 {name} 的函数体是空的"
    return body


def _brace_body(text, open_idx, what):
    """从 `text[open_idx] == '{'` 起做大括号配对，返回大括号之间的内容。"""
    assert text[open_idx] == "{", f"{what}：给定位置不是左大括号"
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
    raise AssertionError(f"{what} 的大括号没有配平 —— 剥离器可能把源码带偏了")


def _phase_branch_body(render_body, phase):
    """抠出 render 里 `if (phase === '<phase>') { ... }` 那条分支的分支体。

    存在的理由和 `_function_body` 同源，只是又深了一层：文案 key、`hidden` 赋值这些
    在**别的分支**里也各有一份，只在整个 render 体内搜「有没有」，等于允许把 failed
    分支的文案换成 running 的（界面在失败时显示「底图解压 %」）而测试照绿。
    """
    m = re.search(r"if\s*\(\s*phase\s*===\s*'" + re.escape(phase) + r"'\s*\)\s*\{", render_body)
    assert m, f"render 里没有 phase === '{phase}' 分支"
    return _brace_body(render_body, m.end() - 1, f"phase === '{phase}' 分支")


def _braced_block(css, marker):
    """抠出 `marker { ... }` 的块内容（大括号配对），用于 @media。"""
    i = css.index(marker)
    return _brace_body(css, css.index("{", i), marker)


def _hidden_selectors_in_narrow_screen_block():
    """窄屏 @media 块里所有带 `display: none` 的选择器（已剥注释）。"""
    block = _braced_block(_css(), "@media (max-width: 576px)")
    assert "display" in block, "剥注释剥过头了：窄屏 @media 块里什么都不剩了"
    selectors = [sel.strip() for sel, decls in re.findall(r"([^{}]+)\{([^{}]*)\}", block)
                 if re.search(r"display\s*:\s*none", decls)]
    assert selectors, "窄屏 @media 块里已经没有任何 display: none 规则了"
    return selectors


def _template_class_tokens():
    """所有模板里 class="..." 出现过的类名（已剥注释）。"""
    tokens = set()
    tpl_dir = os.path.join(ROOT, "templates")
    for name in sorted(os.listdir(tpl_dir)):
        if not name.endswith(".html"):
            continue
        markup = _strip_template_comments(_read("templates", name))
        for attr in re.findall(r'class="([^"]*)"', markup):
            tokens.update(attr.split())
    assert "workbench-statusbar" in tokens, (
        "扫不出 workbench-statusbar —— class 扫描器坏了，本文件的类名对账不可信")
    return tokens


def test_statusbar_element_sits_after_the_page_block():
    """元素要在 {% block statusbar %} 之后 —— 那才是「右侧」。

    statusbar 是 flex 且 gap: 0，位置完全由 DOM 顺序决定。放在 block 之前就跑到
    页面自己的读数左边去了。
    """
    base = _base_html()
    block = base.index("{% block statusbar %}")
    elem = base.index('id="statusBaseUnpack"')
    assert block < elem, "解压进度元素必须排在页面 statusbar block 之后（最右）"


def test_statusbar_element_is_hidden_by_default():
    """默认 hidden：99% 的启动底图已就位，状态栏不该多出一块常驻的空位。"""
    base = _base_html()
    m = re.search(r'<span[^>]*id="statusBaseUnpack"[^>]*>', base)
    assert m, "找不到 statusBaseUnpack 元素"
    assert "hidden" in m.group(0)


def test_status_script_is_loaded_on_every_page_after_socket_js():
    """脚本必须全局加载（不在任何 Jinja block 里），且排在 socket.js 之后。

    这条比看上去要紧：socket.js 现在**不在解析期建连**（连接由第一个消费者触发），
    而 /config、/history 两页没有别的消费者 —— 本脚本是那两页唯一的连接触发者。
    一旦它落进某个 block（子模板一覆盖就没了）或排到 socket.js 之前
    （`window.TerraSocket` 还没定义，守卫直接 return），那两页会**静默**失去全部
    实时推送：没有报错，只是什么都不再更新。
    """
    base = _base_html()
    m = re.search(r"js/base_terrain_status\.js", base)
    assert m, "base.html 里没有引入 js/base_terrain_status.js"

    inside = [name for name, start, end in _jinja_block_spans(base)
              if start <= m.start() < end]
    assert not inside, (
        f"base_terrain_status.js 的 <script> 落在 block {inside} 里 —— "
        "子模板覆盖该 block 时它会一起消失，那一页再没有任何东西触发 socket 连接")

    assert base.index("js/socket.js") < m.start(), (
        "base_terrain_status.js 排在了 js/socket.js 之前 —— "
        "window.TerraSocket 还没定义，判空守卫直接 return，全站失去实时推送")


def test_listener_registers_in_the_same_synchronous_block_as_get():
    """socket.on(...) 必须与 window.TerraSocket.get() 在**同一个同步块**里。

    socket.io **不重放**已经错过的事件，而服务端只在 connect 时推一次底图状态快照
    （解压失败是终态，之后没有增量事件、也没有 REST 端点能补拿）。第一次 get() 才
    真正 io() 建连；get() 内部 io() 之后是同步返回的，中间没有让出事件循环的机会，
    所以在同一个同步块里注册就是零窗口。把注册推迟到 setTimeout / .then / await /
    DOMContentLoaded 里，事件循环就有机会先派发 connect —— 后果不是「晚一点收到」
    而是**永远收不到**，且没有任何报错。理由完整写在 static/js/socket.js 的文件头。

    钉三样：顺序、两者之间的括号/大括号净深度为 0（被包进任何回调都会破坏它）、
    以及中间不出现推迟类 token。两条网互补：包进 `setTimeout(() => { ... })` 破坏
    大括号深度，包进不带大括号的 `setTimeout(() => socket.on(...))` 破坏括号深度，
    而 token 名单兜住那些深度恰好平衡的写法。
    """
    src = _status_js()
    g = re.search(r"window\.TerraSocket(?:\s*&&\s*window\.TerraSocket)?\.get\s*\(\s*\)", src)
    assert g, "找不到 window.TerraSocket.get() 调用"
    on = re.search(r"socket\.on\s*\(", src)
    assert on, "找不到 socket.on(...) 注册"
    assert g.end() <= on.start(), "socket.on(...) 出现在 get() 之前，不可能拿到实例"

    span = src[g.end():on.start()]
    assert span.count("{") - span.count("}") == 0 and span.count("(") - span.count(")") == 0, (
        "get() 与 socket.on() 之间括号/大括号深度不平 —— 注册被包进了某个回调里，"
        "已经不在同一个同步块，可能错过 connect 时那次一次性的状态快照")
    offenders = [tok for tok in _DEFERRAL_TOKENS if tok in span]
    assert not offenders, (
        f"get() 与 socket.on() 之间出现了 {offenders} —— 注册被推迟到下一轮事件循环，"
        "connect 时那次一次性的状态快照会永远错过（无报错、无兜底）")


def test_the_event_actually_reaches_render():
    """「收到事件 → 画出来」这条主链路必须完整接上。

    这是整个功能的**全部价值**所在，而它一度没有任何断言钉住：把 `onProgress` 里的
    `render(state);` 整行删掉，组件彻底死掉（事件照收，界面什么都不画），11 条测试
    全绿。分支形态、文案 key、粘性守卫钉得再细，主链路断了全是空的。

    钉两截：
      1. 监听回调确实接到了 `onProgress`（而不是别的东西 —— 直接接 `render` 会绕开
         粘性守卫，接 `null` 之类则等于没接）；
      2. `onProgress` 的函数体里确实调用了 `render(`。
    """
    src = _status_js()
    assert re.search(r"socket\.on\s*\(\s*'base_unpack_progress'\s*,\s*onProgress\s*\)", src), (
        "socket.on 没有接上 onProgress —— 要么没监听，要么绕开了粘性守卫直接接 render")
    body = _function_body(src, "onProgress")
    assert re.search(r"\brender\s*\(", body), (
        "onProgress 里没有调用 render() —— 事件照收，界面什么都不画，"
        "这个功能的全部价值就没了（且没有任何报错）")


def test_render_has_running_failed_and_collapse_branches():
    """render 里 running / failed / 收起 三条分支都必须在，且各钉各的形态。

    漏掉 failed 分支就等于失败静默 —— 那正是这个功能要消灭的东西。
    ⚠️ 断言必须在 **render 的函数体内**：`phase === 'failed'` 在 onProgress 的终态
    判定里另有一份，在全文里搜的话，把 render 的 failed 分支整段删光照样绿
    （本文件初版就是这么假绿的）。
    收起分支钉 `box.hidden = true` 而不是任意 `hidden = true`：紧邻的还有一行
    `prog.hidden = true`，钉宽了的话只把 box 那行改成 false 也能过。
    """
    src = _status_js()
    body = _function_body(src, "render")

    assert re.search(r"phase\s*===\s*'running'", body), "render 里没有 running 分支"
    assert re.search(r"phase\s*===\s*'failed'", body), (
        "render 里没有 failed 分支 —— 解压失败在界面上完全不可见")
    assert re.search(r"\bbox\.hidden\s*=\s*true", body), (
        "render 里没有把 box 收起来的分支，idle / ready 时元素会一直占着状态栏")
    assert re.search(r"\bprog\.hidden\s*=\s*true", body), "进度条没有跟着收起"

    assert "base_unpack_progress" in src, "没有监听事件"
    assert "window.TerraSocket" in src, "应当复用全局 socket 单例"


def test_each_branch_uses_its_own_i18n_key():
    """running / failed 两条分支各自用**对应的**文案 key。

    没有这条的话，把 failed 分支的 key 换成 `js.base_unpack.running`，界面会在解压
    失败时显示「底图解压 %」（还是个没填的占位符），而所有分支/形态断言照绿 ——
    「失败要看得见」这个需求被静默改成了「失败伪装成进行中」。

    两个方向都钉（各自的 key 必须在、对方的 key 必须不在），这样对调两处也拦得住。
    ⚠️ `js.base_unpack.failed` 的正则要带上收尾引号，否则 `failed_title` 也会命中。
    """
    body = _function_body(_status_js(), "render")

    running = _phase_branch_body(body, "running")
    assert re.search(r"text\.textContent\s*=\s*t\(\s*'js\.base_unpack\.running'", running), (
        "running 分支没有用 js.base_unpack.running 渲染文案")
    assert "js.base_unpack.failed" not in running, (
        "running 分支里出现了 failed 的文案 key —— 两条分支的 key 被对调了？")

    failed = _phase_branch_body(body, "failed")
    assert re.search(r"text\.textContent\s*=\s*t\(\s*'js\.base_unpack\.failed'", failed), (
        "failed 分支没有用 js.base_unpack.failed 渲染文案 —— "
        "界面在解压失败时会显示成别的东西（比如「底图解压 %」）")
    assert "js.base_unpack.running" not in failed, (
        "failed 分支里出现了 running 的文案 key —— 失败被伪装成了进行中")


def test_failure_reason_goes_into_the_title_attribute():
    """失败原因必须写进 title，且是 failed 分支里那一次赋值。

    状态栏只有「底图不可用」四个字，用户无从下手；原因（多半是 assets/ 不可写）
    是他唯一能据以行动的信息 —— 这是本功能的核心卖点。
    ⚠️ 钉的是**赋值形态**（`.title = t('js.base_unpack.failed_title'...`），不是
    `'.title' in src`：`.title` 在另两个分支里各有一次 `= ''` 清空，钉宽了的话把
    failed 分支这一行整个删掉照样绿（本文件初版就是这么假绿的）。
    """
    body = _function_body(_status_js(), "render")
    assert re.search(r"\.title\s*=\s*t\(\s*['\"]js\.base_unpack\.failed_title['\"]", body), (
        "failed 分支没有把失败原因写进 title —— 用户只看到「底图不可用」，"
        "拿不到任何可据以行动的信息")


def test_terminal_phase_is_sticky():
    """收到 ready / failed 之后不再接受降级回 running / idle。

    这是一个**真实存在的跨线程窗口**，不是防御性编程：服务端的 connect handler 在
    请求线程里先读 `snapshot()` 再 `emit` 给本 sid，而后台解压线程同时在广播
    （python-socketio 在触发 connect handler **之前**就把 sid 加进了房间，所以广播
    确实可能先于那份快照送达；反方向不可达）。交错顺序可以是「handler 读到
    running(0.7) → 后台置 ready 并广播 → handler 才把那份**陈旧的** running 发出去」。
    不做粘性的话进度条会永远转下去 —— 解压已经结束，**不会再有任何事件来纠正它**。

    ⚠️ 三条断言钉的都是**语义**而不是「有没有 settled 这个词」：
      - `terminal` 的定义里**只能**有 ready 与 failed（把 running 也算进终态，守卫
        就形同虚设 —— 那正是它要挡的东西）；
      - 守卫必须是 `settled && !terminal`（反转成 `settled && terminal` 语义完全相反：
        终态被丢弃、陈旧的 running 反而放行，而只钉「有 if(settled…)return」拦不住）；
      - 守卫必须排在 `render(` **之前**（挪到后面：形态一字不差、语义完全失效，
        陈旧的 running 照样被画出去 —— 只钉形态不钉顺序是拦不住的）；
      - 终态到达时必须置位。
    终态之间仍允许互相覆盖（failed 之后又来 ready 要认），所以守卫只挡非终态。
    """
    body = _function_body(_status_js(), "onProgress")

    m = re.search(r"\bterminal\s*=\s*([^;]+);", body)
    assert m, "onProgress 里找不到 terminal 的定义"
    phases = set(re.findall(r"phase\s*===\s*'(\w+)'", m.group(1)))
    assert phases == {"ready", "failed"}, (
        f"terminal 的定义里出现的 phase 是 {sorted(phases)}，必须恰好是 ready 与 failed —— "
        "多算一个（比如 running）守卫就形同虚设，少算一个则对应的终态挡不住陈旧事件")

    guard = re.search(
        r"if\s*\(\s*(?:settled\s*&&\s*!\s*terminal|!\s*terminal\s*&&\s*settled)\s*\)\s*return",
        body)
    assert guard, (
        "缺少 `if (settled && !terminal) return;` 形态的守卫 —— "
        "写成 `settled && terminal` 语义正好相反（终态被丢弃、陈旧的 running 反而放行）")

    render_call = re.search(r"\brender\s*\(", body)
    assert render_call, "onProgress 里没有调用 render()"
    assert guard.start() < render_call.start(), (
        "粘性守卫排在了 render() 之后 —— 形态一字不差，语义完全失效："
        "陈旧的 running 照样被画出去，进度条仍然永远转下去")

    assert re.search(r"if\s*\(\s*terminal\s*\)\s*settled\s*=\s*true", body), (
        "终态到达时没有置位 settled，守卫等于不存在")


def test_css_stripper_preconditions_hold():
    """CSS 剥离器的**静默**失败模式在 style.css 里必须不成立。

    见文件头第 2 类：CSS 允许 `url()` 不加引号，而 `_strip_js_comments` 只在带引号的
    字符串里保护 `//` —— `url(http://h/a.png)` 会让本行剩余被当行注释静默删光，
    连它后面的 `/* */` 注释一起吃掉。这是「剥过头 → 否定断言假绿」的路径，没有任何
    异常可依赖，所以在这里直接钉住前提。
    （第 1 类 `calc(100% / 3)` 会响亮抛 AssertionError，自带红灯，不需要额外断言。）
    """
    raw = _read("static", "css", "style.css")
    bad = re.findall(r"url\(\s*[^'\")]*//[^)]*\)", raw)
    assert not bad, (
        f"style.css 里出现了不加引号且含 // 的 url()：{bad} —— "
        "剥离器会把本行剩余当行注释静默删光，本文件所有 CSS 否定断言随之变成假绿。"
        "改成带引号的 url('...') 即可（带引号实测安全）")


def test_narrow_screen_rule_no_longer_depends_on_last_child():
    """窄屏隐藏规则不能再靠位置相关的伪类。

    原规则 `.statusbar-item:last-child { display: none }` 的正确性依赖「时钟恰好
    排最后」这个巧合。新元素插到末尾之后它一个都选不中，时钟在窄屏不再隐藏 ——
    既有行为被静默改掉。钉的是**整类**位置伪类而不是那一个字面量：换成
    `:nth-last-child(2)` 之类只是换个巧合再犯一次。
    """
    # 正向哨兵：剥注释后被测的规则块必须还在。少了它，一个剥过头的剥离器会让下面
    # 那些否定断言假绿（什么都没剩，当然搜不到）。
    assert ".statusbar-item {" in _css(), "剥注释剥过头了：.statusbar-item 规则块不见了"
    hidden_selectors = _hidden_selectors_in_narrow_screen_block()

    positional = [s for s in hidden_selectors if _POSITIONAL_PSEUDO_RE.search(s)]
    assert not positional, (
        f"窄屏隐藏规则仍靠位置伪类选中元素：{positional} —— "
        "往 statusbar 末尾加任何东西都会让它选中别的东西或一个都选不中")
    assert any(".statusbar-clock" in s for s in hidden_selectors), (
        "窄屏规则应当按语义选中时钟（.statusbar-clock）")


def test_narrow_screen_selectors_match_the_real_markup():
    """窄屏隐藏规则用到的类名，必须在模板里真实存在。

    这条把「CSS 说要隐藏谁」和「markup 上真有这个类」绑在一起。少了它，把
    templates/index.html 上的 `statusbar-clock` 删掉或改名，全套测试照绿而窄屏隐藏
    静默失效 —— 与这次要消灭的 `:last-child` 是同一类问题（依赖一个没人钉住的巧合），
    只是换了个巧合来依赖。
    """
    hidden_selectors = _hidden_selectors_in_narrow_screen_block()
    known = _template_class_tokens()
    used = {cls for sel in hidden_selectors for cls in re.findall(r"\.([A-Za-z0-9_-]+)", sel)}
    assert used, "窄屏隐藏规则里一个类选择器都没有 —— 换成标签/属性选择器了？"
    missing = sorted(used - known)
    assert not missing, (
        f"窄屏隐藏规则用到的类名在模板里根本不存在：{missing} —— "
        "规则选不中任何东西，窄屏隐藏静默失效")


def test_i18n_keys_exist_in_both_locales():
    """三个新 key 的中英文都要在 —— 漏翻会在界面上显示成 key 本身。"""
    from src.i18n.catalog import MESSAGES

    for key, zh in (
        ("js.base_unpack.running", "底图解压"),
        ("js.base_unpack.failed", "底图不可用"),
        ("js.base_unpack.failed_title", "全球底图解压失败"),
    ):
        assert key in MESSAGES, f"缺 i18n key: {key}"
        assert zh in MESSAGES[key]["zh"], f"{key} 的中文不是预期文案"
        assert MESSAGES[key]["en"], f"{key} 缺英文"
