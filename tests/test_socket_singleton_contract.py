"""socket 实例必须是全局单例，且每个页面都拿得到。

本项目没有 JS 测试框架（无 package.json，且不打算引入 —— 会破坏离线打包形态），
所以这些断言守的是源码**形态**，与 tests/test_tasks_js_contract.py 同一路数。

⚠️ 针对 JS 的断言一律**先剥注释再匹配**。本项目的注释里逐字讨论代码
（socket.js 的文件头就写着 `socket = io()`、`window.socket = io()`），裸 grep 会
命中注释给出假绿 —— 本仓库在 style.css 上已经吃过一次同样的亏。
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 模板里引用 socket.io vendor 的形态（版本号是字面量，见 base.html 的注释）
_SOCKET_IO_SRC_RE = re.compile(r"vendor/socket\.io/[\d.]+/socket\.io(?:\.min)?\.js")
# 裸 io(...) 调用：排除 foo.io( 这种属性访问，也排除 audio( / scenario( 这种词尾
_IO_CALL_RE = re.compile(r"(?<![.\w$])io\s*\(")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


# 出现在 `/` 之前就说明它是**正则字面量**而不是除号（标准启发式：除号前面只能是
# **值**，正则前面只能是运算符/分隔符/关键字）。写下这条是被 ui.js 的 escapeHtml 里
# `.replace(/"/g, '&quot;')` 逼出来的 —— 正则里的裸引号会把纯字符串扫描器带偏。
#
# ⚠️ 判错的两个方向后果**不对称**，两边都得防，不能靠取舍：
#   - 把除号当正则（`a++ / b; // 尾注释`）：正则一路扫到 `//` 的第一个 `/` 收尾，
#     尾注释以「代码」身份留在输出里 —— 栈平衡、不抛、**静默假绿**；
#   - 把正则当除号（`a + /\//.test(s); var hidden = io();`）：`/` 之后被当成行注释，
#     本行剩余代码被删光 —— 一个**真实的** io() 从输出里凭空消失，同样**静默**。
#     （注意：此时正则分支根本没进，「正则本行未闭合 → 抛」那条安全网对它永远不会
#     触发。那条网只兜相反方向。）
# 所以按尾部 token 区分，而不是把 `+ -` 一删了之：`++`/`--` 是**后缀自增自减**、
# 结果是值 → 后面的 `/` 是除号；单个 `+ -` 是二元/一元运算符 → 后面的 `/` 是正则。
# 见 _regex_allowed 里的 endswith("++") / endswith("--")。
# ⚠️ 说准一点：这是**按已输出文本回看最后 2 个字符**的近似，不是真的词法分析。
# 已知它判错的形态：`a+++/re/`（三连符，JS 词法切成 `a++ + /re/`，而这里只看到结尾的
# `++` 就判成除号）。本仓库没有这种写法，风险实测为零；但别把它当成通用 JS 分词器。
# 关键字前缀是 `[^\w$.]` 而不是 `[^\w$]`：`o.of / 2` 里的 `.of` 是属性访问不是关键字，
# 少了那个 `.` 会把 `/` 误判成正则、把同行尾注释救活。
_REGEX_CAN_FOLLOW = set("(,=:[!&|?{};+-*%~^<>")
_REGEX_CAN_FOLLOW_WORD_RE = re.compile(
    r"(?:^|[^\w$.])(return|typeof|case|in|of|new|delete|void|do|else|instanceof|yield|await|throw)\s*$")


def _strip_js_comments(src):
    """删掉 // 与 /* */ 注释，保留字符串/模板串/正则字面量里的内容。

    用状态机而不是正则：正则要么把字符串里的 `//`（`http://`）当成注释吃掉半行，
    要么被字符串里的引号带偏。前两种坑在本项目的 js 里真实存在，第三种由夹具钉住，
    缺一条就会被带偏：
      - 字符串/注释里的 `//`（history.js 里 `//${host}/vt?...` 那种地址形态）；
      - 含裸引号的正则字面量（ui.js 的 escapeHtml 里 `.replace(/"/g, '&quot;')`）；
      - **嵌套模板串**（`${}` 里又开一个反引号串；仓库当前没有实例，
        见 test_strip_js_comments 里的 `var tpl = ...` 夹具）。
    `/` 是正则开头还是除号，按前一个有效字符判定（见 _REGEX_CAN_FOLLOW）。
    换行一律保留，行号不乱。扫完之后状态没回到顶层就直接抛：宁可红，也不要拿一份
    被带偏的源码去做断言 —— 那种假绿正是本文件要防的东西。
    """
    out = []
    i, n = 0, len(src)
    # 状态栈：['code', 大括号深度] / ['template', 0]。模板串里的 ${ 压一层 code，
    # 该层深度回到 0 时遇 } 就弹回模板串 —— 嵌套模板串靠这个才不串味。
    stack = [["code", 0]]

    def _regex_allowed():
        tail = "".join(out).rstrip()
        if not tail:
            return True
        if tail.endswith("++") or tail.endswith("--"):
            return False  # 后缀自增自减的结果是值 → 后面的 `/` 是除号，不是正则
        return tail[-1] in _REGEX_CAN_FOLLOW or bool(_REGEX_CAN_FOLLOW_WORD_RE.search(tail))

    while i < n:
        ch = src[i]
        kind = stack[-1][0]

        if kind == "template":  # 反引号模板串内部
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(src[i + 1])
                i += 2
                continue
            if ch == "`":
                out.append(ch)
                stack.pop()
                i += 1
                continue
            if ch == "$" and i + 1 < n and src[i + 1] == "{":
                out.append("${")
                stack.append(["code", 0])
                i += 2
                continue
            out.append(ch)
            i += 1
            continue

        if ch == "`":  # 进入模板串
            out.append(ch)
            stack.append(["template", 0])
            i += 1
            continue
        if ch in "\"'":  # 普通字符串字面量
            quote = ch
            out.append(ch)
            i += 1
            closed = False
            while i < n:
                c = src[i]
                out.append(c)
                if c == "\\" and i + 1 < n:
                    out.append(src[i + 1])
                    i += 2
                    continue
                i += 1
                if c == quote:
                    closed = True
                    break
            if not closed:
                raise AssertionError(
                    f"扫到文件尾字符串 {quote!r} 仍未闭合 —— 扫描器被某个字面量带偏了。"
                    "本文件所有断言的结论都不可信，先修扫描器。")
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":  # 行注释
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":  # 块注释
            end = src.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("\n" * src.count("\n", i, end))  # 保住行号
            i = end
            continue
        if ch == "/" and _regex_allowed():  # 正则字面量 /.../flags
            out.append(ch)
            i += 1
            in_class = False
            closed = False
            while i < n and src[i] != "\n":
                c = src[i]
                out.append(c)
                if c == "\\" and i + 1 < n:
                    out.append(src[i + 1])
                    i += 2
                    continue
                i += 1
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    closed = True
                    break
            if not closed:
                raise AssertionError(
                    "正则字面量在本行内没有闭合 —— 扫描器把某个除号当成了正则开头。"
                    "本文件所有断言的结论都不可信，先修扫描器。")
            continue
        if ch == "{":
            stack[-1][1] += 1
        elif ch == "}":
            if stack[-1][1] == 0 and len(stack) > 1:
                out.append(ch)
                stack.pop()  # ${} 结束，回到模板串
                i += 1
                continue
            stack[-1][1] = max(0, stack[-1][1] - 1)
        out.append(ch)
        i += 1

    if len(stack) != 1:
        raise AssertionError(
            f"扫完之后状态没回到顶层（残留 {[s[0] for s in stack[1:]]}）—— "
            "扫描器被某个字面量带偏了。本文件所有断言的结论都不可信，先修扫描器。")
    return "".join(out)


def _strip_template_comments(markup):
    """删掉 Jinja `{# #}` 与 HTML `<!-- -->` 注释（换行保留，位置比较不失真）。

    和 js 侧同一个坑，只是挪到了模板：base.html 的 `{# #}` 注释里**已经在逐字讨论
    socket.io** 了，只差把完整版本路径写进那段注释，「socket.io 存在」「排在
    socket.js 之前」两条断言就会被注释满足而永远绿。模板侧没有字符串/正则歧义，
    正则替换就够 —— 用等量换行填回去，保住行号与相对位置。
    """
    def _blank(m):
        return "\n" * m.group(0).count("\n")

    markup = re.sub(r"{#.*?#}", _blank, markup, flags=re.S)
    return re.sub(r"<!--.*?-->", _blank, markup, flags=re.S)


def _jinja_block_spans(markup):
    """返回 [(block 名, 起, 止)]，止是 {% endblock %} 的结束位置。带栈，容忍嵌套。"""
    token = re.compile(r"{%-?\s*(block\s+(\w+)|endblock\s*\w*)\s*-?%}")
    stack, spans = [], []
    for m in token.finditer(markup):
        if m.group(2):
            stack.append((m.group(2), m.start()))
        elif stack:
            name, start = stack.pop()
            spans.append((name, start, m.end()))
    assert not stack, f"模板里有未闭合的 block：{[s[0] for s in stack]}"
    return spans


def test_strip_js_comments():
    """扫描器自检 —— 它是下面所有 JS 断言的地基，坏了会静默放行。"""
    stripped = _strip_js_comments(
        "// io() 在注释里\n"
        "/* 块注释里的 io() */\n"
        "var url = 'http://x/y'; // 尾注释\n"
        "instance = io();\n"
        "var s = \"/* 不是注释 */\";\n"
        "var q = x.replace(/\"/g, '&quot;');\n"  # 正则里的裸引号（ui.js 的 escapeHtml 就是这个写法）
        "var half = total / 2; // 除号不是正则\n"
        "var cls = /[/\"']+/.test(s);\n"          # 字符类里的 / 与引号
        "var tpl = `a//b ${x ? `<i>${y}</i>` : ''} c`; // 嵌套模板串\n"
        # 下面两组是已知的失败样本（两轮复审各挖出一组），互为反方向，必须同时钉住：
        # A. 除号被当成正则：`/` 一路吞到尾注释的第一个 `/` 当收尾，注释以「代码」
        #    身份存活 —— 栈平衡、不抛、假绿。
        "var c = a++ / b; // instance = io()\n"
        "var z = o.of / 2; // io()\n"
        # B. 正则被当成除号：`/` 之后被当成行注释，本行剩余代码被删光 —— 一个**真实
        #    的** io() 从输出里凭空消失，同样静默。修 A 时若把 `+ -` 一删了之就会开这个洞。
        "var ok = a + /\\/\\//.test(u); var hidden = io(); // io()\n"
    )
    assert "注释里" not in stripped, "注释没剥干净"
    assert "块注释" not in stripped
    assert "尾注释" not in stripped and "除号不是正则" not in stripped
    assert "http://x/y" in stripped, "字符串里的 // 被当成注释吃掉了"
    assert "/* 不是注释 */" in stripped, "字符串里的块注释符号被误删"
    assert "total / 2" in stripped, "除号被当成正则开头，后面的代码被吞了"
    assert "`a//b ${x ? `<i>${y}</i>` : ''} c`" in stripped, "嵌套模板串被剥坏了"
    assert "嵌套模板串" not in stripped, "嵌套模板串之后的行注释没剥掉（状态串味了）"
    # A 方向：除号没被当成正则，尾注释确实被剥掉了
    assert "a++ / b" in stripped and "o.of / 2" in stripped, "自增/属性访问后的除号被吞了"
    # B 方向：正则没被当成除号 —— 注释剥掉了，而**本行后面的真代码还在**。
    # 只断言前半句会漏掉整条 B（代码被删光时注释当然也不在了）。
    assert "var hidden = io();" in stripped, (
        "`a + /re/` 里的正则被当成了除号，本行剩余代码被当行注释删光 —— "
        "一个真实的 io() 从剥离结果里消失了，唯一创建点那条断言会绿着放行第二个连接")
    assert "/\\/\\//.test(u)" in stripped, "正则字面量本身被剥坏了"
    # 全文只应剩代码里那一次 io()（`instance = io();`）：A 方向的两条尾注释、
    # B 方向那行的尾注释都必须已经剥掉，而 B 行的 `var hidden = io();` 是代码…
    # 所以这里期望 2 次：instance = io() 与 var hidden = io()。
    assert len(_IO_CALL_RE.findall(stripped)) == 2, (
        f"剥完之后 io() 应当恰好剩 2 次（两处代码），实际 {len(_IO_CALL_RE.findall(stripped))} 次 —— "
        "多了说明某条尾注释以「代码」身份活了下来（A 方向），"
        "少了说明某行真代码被当注释删光了（B 方向）")
    # 真实文件全部能扫完（不抛）
    for path in sorted(glob.glob(os.path.join(ROOT, "static", "js", "*.js"))):
        with open(path, encoding="utf-8") as f:
            _strip_js_comments(f.read())


def test_socket_js_exposes_a_lazy_singleton():
    """window.TerraSocket.get() 是唯一的创建点，且是**惰性**的。

    断言全部跑在剥注释后的代码上：socket.js 的文件头注释里就有 `socket = io()`
    字面量，裸 grep 的话把 `instance = io()` 整行删掉都还是绿的。
    """
    code = _strip_js_comments(_read("static", "js", "socket.js"))

    assert re.search(r"window\.TerraSocket\s*=", code), (
        "socket.js 没有把单例挂到 window.TerraSocket 上（只在注释里提到不算）")
    assert re.search(r"\bget\b\s*:", code) or re.search(r"\bget\s*\(\s*\)\s*{", code), (
        "window.TerraSocket 上找不到 get")

    calls = _IO_CALL_RE.findall(code)
    assert len(calls) == 1, (
        f"socket.js 里的 io() 调用有 {len(calls)} 次，必须恰好 1 次 —— "
        "0 次说明根本不建连接，2 次以上说明单例被绕过了")

    assert re.search(r"if\s*\(\s*instance\s*\)\s*return\s+instance", code), (
        "缺少惰性/复用守卫 `if (instance) return instance` —— 每次 get() 都会新建连接")


def test_socket_js_does_not_connect_at_parse_time():
    """socket.js 不许在解析期就建连接。

    解析期建连会早于所有消费者注册监听：socket.io **不重放**错过的事件，而
    connect 是一次性事件 —— tasks.js 的 hasConnectedOnce 会永远停在 false
    （断线重连后不补拉数据），服务端在 connect 时推的底图状态快照也会永远错过
    （解压失败是终态，没有增量事件、也没有 REST 端点能补拿）。
    连接必须由第一个消费者触发，并在同一个同步块里立刻注册监听。
    """
    code = _strip_js_comments(_read("static", "js", "socket.js"))
    assert not re.search(r"get\s*\(\s*\)\s*;", code), (
        "socket.js 在解析期调用了 get() —— 建连早于消费者注册监听，会漏掉 connect")


def test_tasks_js_reuses_the_singleton():
    """tasks.js 必须复用单例，不能自己 io()。

    自己建一个的话首页会开出两个 WebSocket 连接：服务端 connected_clients 计数
    翻倍，每条广播被处理两遍，而现象只是「偶尔重复刷新」，极难归因。
    """
    code = _strip_js_comments(_read("static", "js", "tasks.js"))
    assert "window.TerraSocket.get()" in code, "tasks.js 没有复用全局单例"
    assert not _IO_CALL_RE.search(code), "tasks.js 仍在直接 io() —— 会开出第二个连接"


def test_tasks_js_seeds_has_connected_once_from_current_state():
    """连接不是本页建的时候，hasConnectedOnce 要靠 socket.connected 兜住。

    若别的消费者先 get() 过、连接已经建好，tasks.js 的 connect 回调就永远不触发，
    hasConnectedOnce 卡在 false —— 断线重连后不补拉 loadActiveTasks/loadHistory/
    loadStats，数据永久停在断线前的值（非确定性，极难归因）。
    """
    code = _strip_js_comments(_read("static", "js", "tasks.js"))
    assert re.search(r"if\s*\(\s*socket\.connected\s*\)\s*hasConnectedOnce\s*=\s*true",
                     code), (
        "tasks.js 缺少 `if (socket.connected) hasConnectedOnce = true;` —— "
        "连接由别人先建时，断线重连不会补拉数据")


def test_socket_io_vendor_is_loaded_on_every_page():
    """socket.io 必须存在于 base.html，且不在**任何** Jinja block 里。

    「不在任何 block 里」等价于「每个页面都加载」：子模板只能覆盖 block，覆盖不到
    block 之外的东西。/config 曾经把 vendor block（Cesium 5.7 MB + socket.io 绑在
    一起）覆盖成空，于是那一页收不到任何实时推送 —— 而底图解压进度是全站可见的要求。
    """
    base = _strip_template_comments(_read("templates", "base.html"))

    hits = [m.start() for m in _SOCKET_IO_SRC_RE.finditer(base)]
    assert hits, (
        "base.html 里根本找不到 socket.io 的 vendor 引用 —— "
        "没有它，任何页面都收不到实时推送")

    spans = _jinja_block_spans(base)
    for pos in hits:
        inside = [name for name, start, end in spans if start <= pos < end]
        assert not inside, (
            f"socket.io 的 <script> 落在 block {inside} 里 —— "
            "子模板一覆盖该 block 就会连它一起省掉，那一页失去实时推送")

    # Cesium 必须还在一个独立的、可被覆盖的 block 里（/config 靠它省掉 5.7 MB）
    map_block = re.search(r"{%\s*block vendor_map_js\s*%}(.*?){%\s*endblock\s*%}",
                          base, re.S)
    assert map_block, "找不到 vendor_map_js block —— Cesium 应当单独放在这里"
    assert "Cesium.js" in map_block.group(1), "vendor_map_js 里没有 Cesium"
    assert not _SOCKET_IO_SRC_RE.search(map_block.group(1)), (
        "socket.io 还在 vendor_map_js 里 —— /config 覆盖该 block 时会连它一起省掉")


def test_config_page_only_drops_cesium():
    """/config 只许覆盖 Cesium 那个 block，且不许自己重新引入 socket.io。"""
    config = _strip_template_comments(_read("templates", "config.html"))
    assert re.search(r"{%\s*block vendor_map_js\s*%}\s*{%\s*endblock\s*%}", config), (
        "config.html 应当把 vendor_map_js 覆盖成空（只省 Cesium）")
    assert not re.search(r"{%\s*block vendor_js\s*%}", config), (
        "config.html 还在覆盖旧的 vendor_js block，socket.io 仍会被省掉")
    assert not _SOCKET_IO_SRC_RE.search(config), (
        "config.html 自己又引了一次 socket.io —— 会加载两份库")


def test_socket_io_vendor_loads_before_socket_js():
    """socket.io 库必须排在 js/socket.js 之前。

    顺序反了 socket.js 解析时 `typeof io !== 'function'`，get() 只会返回 null，
    页面**静默**失去实时推送（只有一条 console.warn，没有任何可见报错）。
    """
    base = _strip_template_comments(_read("templates", "base.html"))
    vendor = _SOCKET_IO_SRC_RE.search(base)
    assert vendor, "base.html 里找不到 socket.io 的 vendor 引用"
    assert vendor.start() < base.index("js/socket.js"), (
        "socket.io 库排在了 js/socket.js 之后 —— get() 会拿不到 io，静默返回 null")


def test_base_html_loads_socket_js_after_ui_js():
    """socket.js 要排在 ui.js 之后 —— 它调 initConnectionStatus（ui.js 定义的）。"""
    base = _strip_template_comments(_read("templates", "base.html"))
    ui = base.index("js/ui.js")
    sock = base.index("js/socket.js")
    assert ui < sock, "socket.js 必须排在 ui.js 之后，否则 initConnectionStatus 还没定义"


def test_socket_js_is_the_only_place_that_creates_a_connection():
    """全站 static/js 里只有 socket.js 允许出现 io() 调用。

    下一个任务要新增消费者，正是最容易冒出第二个 io() 的时候：多一个连接不会报错，
    只会让服务端计数翻倍、每条广播被处理两遍，现象是「偶尔重复刷新」。
    """
    offenders = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "static", "js", "*.js"))):
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            code = _strip_js_comments(f.read())
        hits = _IO_CALL_RE.findall(code)
        if not hits:
            continue
        if name == "socket.js":
            assert len(hits) == 1, f"socket.js 里有 {len(hits)} 次 io()，必须恰好 1 次"
            continue
        offenders[name] = len(hits)
    assert not offenders, (
        f"这些文件自己调了 io()，会开出额外的 WebSocket 连接：{offenders} —— "
        "一律改用 window.TerraSocket.get()")


def test_socket_io_stays_on_page_origin_and_ignores_tile_helpers():
    """Socket.IO 连接必须留在页面 origin，一个字都不许沾瓦片端口。

    瓦片端口那个 HTTP 服务只挂了瓦片路径与 /tile-health（src/core/tile_server.py
    的 tile gate），没有 socket.io 端点、也不做 WebSocket 升级。把连接改指过去
    的表现是长轮询一路 404 后彻底沉默：任务进度不再更新，而页面本身照常渲染。
    """
    socket_code = _strip_js_comments(_read("static", "js", "socket.js"))
    assert re.search(r"instance\s*=\s*io\s*\(\s*\)", socket_code), (
        "socket.js 的连接不再是无参 io() —— 无参才等于「跟着页面 origin 走」")
    assert "tileUrl" not in socket_code
    assert "initTileOrigin" not in socket_code
    assert "tile_port" not in socket_code


def test_task_api_requests_never_use_tile_origin():
    """任务 REST 调用留在主端口：瓦片端口不认 /api/*，且没有会话。"""
    tasks_code = _strip_js_comments(_read("static", "js", "tasks.js"))
    assert "tileUrl(" not in tasks_code
    assert "initTileOrigin(" not in tasks_code
