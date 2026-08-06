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
本改动的前三个任务各抓到过一次同类假守卫，这不是假想的风险。

CSS 也走 `_strip_js_comments`：CSS 的 `/* */` 与 JS 同形，而那个状态机比
`re.sub(r'/\\*.*?\\*/')` 更稳（不会被字符串里的注释符号带偏）。已逐行比对过两者
在 style.css 上的输出完全一致。为防它哪天在某个 CSS 构造上剥过头（剥过头会让
否定断言假绿），每条 CSS 断言都配一条「被测规则块还在」的正向断言。
"""
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_socket_singleton_contract import (  # noqa: E402
    _jinja_block_spans, _strip_js_comments, _strip_template_comments)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _base_html():
    return _strip_template_comments(_read("templates", "base.html"))


def _status_js():
    return _strip_js_comments(_read("static", "js", "base_terrain_status.js"))


def _css():
    return _strip_js_comments(_read("static", "css", "style.css"))


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


def test_js_handles_running_failed_and_collapses_otherwise():
    """running / failed 各有自己的分支，其余（idle / ready）收起来。

    漏掉 failed 分支就等于失败静默 —— 那正是这个功能要消灭的东西。
    只钉这两个字面量：idle 与 ready 共用「收起」的 else 分支，没有各自的字面量
    可查，所以改钉那条分支的存在（box.hidden = true）。
    """
    src = _status_js()
    assert "base_unpack_progress" in src, "没有监听事件"
    assert "window.TerraSocket" in src, "应当复用全局 socket 单例"
    for phase in ("running", "failed"):
        assert f"'{phase}'" in src or f'"{phase}"' in src, f"没处理 phase={phase}"
    assert re.search(r"hidden\s*=\s*true", src), "没有「收起」分支，就绪后元素会一直占着状态栏"


def test_failure_reason_goes_into_the_title_attribute():
    """失败原因必须挂到 title 上。

    状态栏只有「底图不可用」四个字，用户无从下手；原因（多半是 assets/ 不可写）
    是他唯一能据以行动的信息。
    """
    src = _status_js()
    assert ".title" in src, "失败原因没有写进 title 属性"


def test_terminal_phase_is_sticky():
    """收到 ready / failed 之后不再接受降级回 running。

    这是一个**真实存在的跨线程窗口**，不是防御性编程：服务端的 connect handler
    在请求线程里读 `snapshot()` 再 `emit` 给本 sid，而后台解压线程同时在广播。
    交错顺序可以是「handler 读到 running(0.7) → 后台把状态置成 ready 并广播 →
    handler 才把那份**陈旧的** running 发出去」。客户端于是先收 ready 后收
    running。不做粘性的话进度条会永远转下去 —— 解压已经结束，**不会再有任何事件
    来纠正它**（没有增量事件，也没有 REST 端点能补拿）。

    终态之间允许互相覆盖（failed 之后又来 ready 要认），所以守卫只挡非终态。
    """
    src = _status_js()
    assert re.search(r"\bsettled\b", src), "没有记录「已进入终态」的标志位"
    assert re.search(r"if\s*\(\s*settled\b[^)]*\)\s*(?:\{[^}]*)?return", src), (
        "缺少「已终态则忽略」的早退守卫 —— 陈旧的 running 会把进度条永远点亮")
    assert re.search(r"settled\s*=\s*true", src), "标志位从来没被置位，守卫等于不存在"


def test_narrow_screen_rule_no_longer_depends_on_last_child():
    """窄屏隐藏规则不能再靠 :last-child。

    原规则 `.statusbar-item:last-child { display: none }` 的正确性依赖「时钟恰好
    排最后」这个巧合。新元素插到末尾之后它一个都选不中，时钟在窄屏不再隐藏 ——
    既有行为被静默改掉。改成按语义选中时钟本身。
    """
    css = _css()
    # 正向哨兵：剥注释后被测的规则块必须还在。少了它，一个剥过头的剥离器会让下面
    # 那条否定断言假绿（什么都没剩，当然搜不到 :last-child）。
    assert "@media (max-width: 576px)" in css, "剥注释剥过头了：窄屏 @media 块整个不见了"
    assert ".statusbar-item {" in css, "剥注释剥过头了：.statusbar-item 规则块不见了"

    assert ".statusbar-item:last-child" not in css, (
        "窄屏规则仍依赖 :last-child —— 往 statusbar 末尾加任何东西都会踩到")
    assert ".statusbar-clock" in css, "窄屏规则应当按语义选中时钟"


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
