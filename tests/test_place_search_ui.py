"""地点搜索的界面契约（2026-08 四项改造）。

用户实测提的四条：搜索没有动画、位置在左上工具条里不该在那、类型不够多样、
没有历史记录。改造后的形态：

1. 输入框常驻**顶部居中**，工具条里那颗按钮删除；
2. 下拉面板带开合过渡（`hidden` 不能过渡，所以 rAF 加类 / transitionend 摘）；
3. 关键词先本地过一遍**坐标识别**（十进制 / 度分秒 / 四至），命中就不打上游；
   结果再按 kind 生成筛选片；
4. 最近 10 条搜索存 localStorage，可一键清除。

本仓没有 JS 运行时测试设施（见 tests/test_map_js_contract.py 头注），所以这里
钉的是**源码形态**，行为由真实浏览器实测覆盖。每一条都对着一个具体的、改坏了
不会被其它测试发现的形态写，不做「这个字符串在不在」的凑数断言。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from test_css_contract import _js_function_body  # noqa: E402
from test_tasks_js_contract import _strip_js_comments  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def _map_js():
    return _strip_js_comments(_read('static', 'js', 'map.js'))


def _index_html():
    return _read('templates', 'index.html')


def _css():
    return _read('static', 'css', 'style.css')


# ---------------------------------------------------------------- 2. 位置

def test_search_left_the_toolbar_for_the_top_center():
    """搜索不再是工具条里的按钮，而是顶部居中的常驻输入框。

    双向断言：新落点存在 **且** 旧按钮消失。只查前者的话，两套入口并存也能过 ——
    那正是「挪动」类改动最常见的半吊子结局（用户会在工具条里看到一颗死按钮）。
    """
    html = _index_html()
    assert 'id="mapSearch"' in html, '顶部搜索容器 #mapSearch 不在模板里'
    assert 'id="placeSearchInput"' in html
    assert 'id="mapPlaceSearch"' not in html, (
        '工具条里那颗搜索按钮还在 —— 搜索已经挪到顶部，两个入口并存会让用户困惑')
    assert 'tpl.index.toolbar.place_search' not in html, (
        '工具条搜索的旧文案 key 还被引用着')


def test_search_box_is_actually_centred_at_the_top():
    """`.map-search` 必须真的锚在顶部居中，而不是只叫这个名字。

    钉的是 `left: 50%` + `translateX(-50%)` 这一对：只写 left:50% 会让盒子的
    **左边**在中线上，视觉上整体偏右半个宽度 —— 这种错法截图不细看发现不了。
    """
    css = _css()
    m = re.search(r'\.map-search\s*\{([^}]*)\}', css)
    assert m, 'style.css 里没有 .map-search 规则'
    body = m.group(1)
    assert re.search(r'position:\s*absolute', body), '.map-search 必须绝对定位'
    assert re.search(r'left:\s*50%', body), '.map-search 没有水平居中锚点'
    assert re.search(r'transform:\s*translateX\(-50%\)', body), (
        '只有 left:50% 而没有 translateX(-50%)：盒子会整体偏右半个宽度')
    assert re.search(r'top:\s*\d', body), '.map-search 没有顶部偏移'


# ---------------------------------------------------------------- 1. 动画

def test_search_panel_declares_a_real_transition():
    """下拉面板必须声明过渡，且过渡的是 opacity/transform 而不是尺寸。

    过渡 height/width 会在每一帧触发重排，而这个面板压在 Cesium 画布上 ——
    合成器友好的两个属性是硬要求，不是偏好。
    """
    css = _css()
    m = re.search(r'\.map-search__panel\s*\{([^}]*)\}', css)
    assert m, '没有 .map-search__panel 规则'
    body = m.group(1)
    trans = re.search(r'transition:\s*([^;]+);', body)
    assert trans, '.map-search__panel 没有 transition —— 用户报的就是「没有动画」'
    props = trans.group(1)
    assert 'opacity' in props and 'transform' in props, (
        f'过渡的属性是 {props!r}，期望 opacity + transform')
    for banned in ('height', 'width', 'top', 'margin'):
        assert not re.search(rf'\b{banned}\b', props), (
            f'过渡里出现了会触发重排的 {banned} —— 面板压在 Cesium 画布上')


def test_panel_opens_before_the_debounce_and_the_network():
    """敲字就开面板，**不等**去抖、更不等上游 —— 这是「延迟」那条反馈的正解。

    改造前面板的可见性绑在结果到达上：实测从按键到面板可见 2.3 秒（300ms 去抖
    + 约 2s 上游往返），这段时间屏幕上一点变化都没有。顺带把开合过渡也废了 ——
    那 160ms 淡入被推到 2.3 秒空白之后、还与六行结果同时出现，根本注意不到。

    判据是「`_renderPlaceSearching` 在 setTimeout 之前调用」：写在 setTimeout
    回调里同样能出「搜索中…」，但那就又晚了 300ms，而且坐标输入也白等一轮。
    """
    body = _js_function_body(_map_js(), 'initRegionTools')
    m = re.search(r"input\.addEventListener\('input',\s*function[^{]*\{(.*?)\n    \}\);",
                  body, re.S)
    assert m, "找不到 input 的去抖监听 —— 本测试已失效"
    handler = m.group(1)
    assert '_renderPlaceSearching' in handler, (
        '敲字时没有立刻给反馈：面板会一直等到结果回来才出现')
    assert handler.index('_renderPlaceSearching') < handler.index('setTimeout'), (
        '「搜索中…」被放进了去抖回调里 —— 又晚 300ms，等于没修')


def test_the_reset_half_of_the_swap_fade_is_instant():
    """`--fresh` 必须自带 `transition: none`。

    `.place-search__results` 的过渡是**双向**的：不关掉的话，加 `--fresh` 时
    opacity 也会用 140ms 慢慢往 0 走，而 JS 下一步就把类摘了 —— 于是从 0.99
    折回 1，净位移几乎为零（实测：加类两帧后才 0.87，摘类后立刻 0.999，
    采样到的 distinct opacity 只有 1 个，与完全没动画一模一样）。
    这条错法最阴的地方是 CSS 看起来完全正确，测试里也能查到 transition 存在。
    """
    css = _css()
    base = re.search(r'\.place-search__results\s*\{([^}]*)\}', css)
    assert base and 'transition' in base.group(1), (
        '结果区没有换内容淡入 —— 面板已开时连着搜第二个词是硬切')
    fresh = re.search(r'\.place-search__results--fresh\s*\{([^}]*)\}', css)
    assert fresh, '没有 --fresh 归零态'
    assert re.search(r'opacity:\s*0', fresh.group(1)), '--fresh 没有把 opacity 置 0'
    assert re.search(r'transition:\s*none', fresh.group(1)), (
        '--fresh 少了 transition: none —— 归零会被同一条过渡拖慢，净效果是没有动画')


def test_the_swap_fade_commits_the_zero_state_before_releasing_it():
    """`_flashPlaceResults` 必须在加类与摘类之间强制一次样式落地。

    这里**不能**照抄面板那套「加类 / rAF 摘类」：面板能成立是因为它同时从
    `hidden` 变可见，起始态随「元素首次参与渲染」被提交；结果区一直在渲染树里，
    加类与摘类落在同一帧会被合并成一次样式变更，起点终点都是 1。
    """
    body = _js_function_body(_map_js(), '_flashPlaceResults')
    assert 'offsetWidth' in body or 'offsetHeight' in body, (
        '没有强制重排：加类与摘类会被合并，过渡不会发生')
    add_at = body.index('classList.add')
    flush_at = max(body.find('offsetWidth'), body.find('offsetHeight'))
    remove_at = body.index('classList.remove')
    assert add_at < flush_at < remove_at, (
        f'顺序必须是 加类 → 强制重排 → 摘类，实际是 {add_at}/{flush_at}/{remove_at}')


def test_panel_closes_when_focus_leaves_the_whole_widget():
    """焦点移出**整个部件**才收，判据必须是 relatedTarget 在不在 root 里。

    两个方向都要钉：
    - 少了 focusout：鼠标那条 mousedown 只覆盖鼠标，用 Tab 把焦点移出去不产生
      任何 mousedown —— 实测 Tab 五次焦点已经到 #manualBounds_north 上，面板
      还开着，键盘用户被一个盖住地图的下拉框困住（这条就是用户报的缺陷）。
    - 写成输入框的 blur：Tab 到结果行、到「清除」按钮时输入框确实失焦，但人还在
      这个部件里操作，那时收起来等于键盘根本没法选结果 —— 修一个缺陷造一个更大的。
    """
    body = _js_function_body(_map_js(), 'initRegionTools')
    m = re.search(r"root\.addEventListener\('focusout',\s*function[^{]*\{(.*?)\n    \}\);",
                  body, re.S)
    assert m, 'root 上没有 focusout 监听 —— 键盘移出焦点时面板不会收起'
    handler = m.group(1)
    assert 'relatedTarget' in handler, (
        'focusout 没有看 relatedTarget：会在 Tab 到结果行时就把面板收掉，键盘选不了结果')
    assert 'root.contains' in handler, '判据必须是「新焦点在不在整个部件里」'
    assert not re.search(r"input\.addEventListener\('blur'", body), (
        "用 input 的 blur 收面板会让键盘无法 Tab 到结果行 —— 必须用 root 的 focusout")


def test_clicking_back_into_an_already_focused_input_reopens_the_panel():
    """点回一个**已经聚焦**的输入框必须能重新打开面板。

    `focus` 事件只在焦点真正改变时触发。用户点地图收起面板之后再点回搜索框，
    输入框可能仍是 document.activeElement —— 没有 focus 事件，屏幕上毫无反应，
    得删一个字再补回去才出得来。所以另外挂一发 mousedown 补这一跳。
    """
    body = _js_function_body(_map_js(), 'initRegionTools')
    m = re.search(r"input\.addEventListener\('mousedown',\s*function[^{]*\{(.*?)\n    \}\);",
                  body, re.S)
    assert m, '输入框上没有 mousedown 监听 —— 点回已聚焦的搜索框不会重开面板'
    handler = m.group(1)
    assert 'panel.hidden' in handler, (
        '没有「只在面板关着时才重开」这道闸：面板开着时每点一下都会重绘闪一次')
    assert '_renderPlaceHistory' in handler and '_runPlaceSearch' in handler, (
        '重开时要按输入框有没有内容分流（空 -> 历史，有内容 -> 结果）')


def test_opening_waits_a_frame_and_closing_waits_the_transition():
    """开合的两半都要按过渡的节奏来。

    - 开：`hidden` 与加类必须**跨帧**（rAF）。同一帧内摘 hidden 再加类，浏览器
      合并成一次样式计算，没有起始状态可插值，过渡一帧都不会发生 —— 表现就是
      「没有动画」，与改造前一模一样，而 CSS 里的 transition 看起来是对的。
    - 关：必须等 transitionend 再挂回 hidden，否则元素当帧消失，退场动画同样
      看不到；并且要有超时兜底（prefers-reduced-motion 把时长压到 0.01ms 时
      transitionend 可能不来，没有兜底面板就永远挂在地图上）。
    """
    src = _map_js()
    opener = _js_function_body(src, '_openPlaceSearch')
    assert 'requestAnimationFrame' in opener, (
        '_openPlaceSearch 没有跨帧加类 —— 过渡不会发生')
    assert opener.index('hidden = false') < opener.index('requestAnimationFrame'), (
        '必须先摘 hidden 再在下一帧加类')

    closer = _js_function_body(src, '_closePlaceSearch')
    assert 'transitionend' in closer, '_closePlaceSearch 没有等退场过渡结束'
    assert 'setTimeout' in closer, (
        'transitionend 没有超时兜底 —— reduce-motion 或过渡被打断时面板会永远开着')
    assert closer.index('classList.remove') < closer.index('transitionend'), (
        '要先摘 --in 触发退场，再等它结束')


def test_ambiguous_two_number_input_has_an_explicit_axis_guard():
    """两个裸数字的判序必须有自证，不能只靠一句约定。

    默认「纬度在前」（地图应用复制出来的都是这个序），但第一个数绝对值 > 90 时
    它只可能是经度 —— 少了这道判据，`114.31, 30.55` 会被读成纬度 114.31，
    越界后整条结果被丢弃，用户看到的是「搜不到」而不是「顺序反了」。

    ⚠️ 只查 `parsePlaceQueryAsCoords` 全身是**没有区分力**的（实测变异存活）：
    判序那行与紧随其后的取值域校验都写着 `Math.abs(a.value) > 90`，删掉判序
    照样能匹配到校验那一行。判序因此单独抽成 orderLatLonParts，这里只扫它。
    """
    body = _js_function_body(_map_js(), 'orderLatLonParts')
    assert re.search(r'Math\.abs\(\s*a\.value\s*\)\s*>\s*90', body), (
        '缺少「第一个数超出纬度值域就当经度」的自动判序')
    assert "axis === 'lon'" in body and "axis === 'lat'" in body, (
        '缺少半球字母（N/S/E/W）的判序分支')


@pytest.mark.parametrize('needle,why', [
    ('parsePlaceQueryAsCoords', '坐标识别入口'),
    ('orderLatLonParts', '经纬判序'),
    ("kind: 'bbox'", '四至形态'),
    ("kind: 'point'", '坐标点形态'),
])
def test_coordinate_recognition_exists(needle, why):
    """关键词先过本地坐标识别 —— 这条路完全不依赖外部地名服务。"""
    assert needle in _map_js(), f'缺少{why}（{needle}）'


def test_coordinate_search_short_circuits_before_the_network():
    """坐标命中必须**在 fetch 之前** return，不能先打一次上游再说。

    顺序反了功能看起来一样（结果照出），但每敲一组坐标都白白消耗一次上游配额，
    而且地名服务没配时坐标定位会跟着不可用 —— 那正是这条路要绕开的依赖。
    """
    body = _js_function_body(_map_js(), '_runPlaceSearch')
    coord_at = body.index('parsePlaceQueryAsCoords')
    fetch_at = body.index('fetch(')
    assert coord_at < fetch_at, '坐标识别排在了网络请求后面'
    between = body[coord_at:fetch_at]
    assert 'return' in between, '坐标命中之后没有 return，会继续往下打网络'


def test_result_filters_are_derived_from_the_results():
    """筛选片按本次结果实际出现的 kind 生成，不写死清单。

    Photon 给 state/city/house/other，Nominatim 给 administrative/boundary ——
    写死一份必然在另一家上失灵（要么显示一堆 0 条的空片，要么漏掉真实类型）。
    """
    body = _js_function_body(_map_js(), '_renderPlaceFilters')
    assert 'list.forEach' in body or 'list.map' in body, (
        '_renderPlaceFilters 没有遍历结果 —— 疑似写死了一份类型清单')
    assert 'kinds.length < 2' in body, (
        '只有一种类型时应整行隐藏：点哪个片结果都一样，留着是噪声')


# ---------------------------------------------------------------- 4. 历史

def test_history_is_capped_at_ten_and_lives_in_local_storage():
    """最近 10 条，存 localStorage。

    存 localStorage 而不是 config 表是有意的：它是这台机器上这个人的浏览痕迹，
    跟着配置走会把一个人的搜索历史同步给同一台服务的其他访问者。
    """
    src = _map_js()
    assert re.search(r"PLACE_HISTORY_KEY\s*=\s*'tf-place-history'", src), (
        '历史的 localStorage 键名变了或不存在')
    assert re.search(r'PLACE_HISTORY_MAX\s*=\s*10', src), '历史上限不是 10 条'
    remember = _js_function_body(src, '_rememberPlaceQuery')
    assert 'PLACE_HISTORY_MAX' in remember, '_rememberPlaceQuery 没有按上限截断'
    assert 'filter' in remember and 'unshift' in remember, (
        '重复关键词必须去重并置顶，否则 10 条容量会被同一个词占满')


def test_history_is_only_written_on_an_explicit_submit():
    """**只有明确提交才记历史** —— 去抖搜索里绝不能记。

    这是本次最容易写错的一处：把 `_rememberPlaceQuery` 放进 input 的去抖回调里，
    敲「重庆」会依次记下「重」「重庆」两条前缀，10 条容量三次搜索就满了，
    而且历史列表里全是半截词。功能看起来完全正常，只有用心看历史才发现。
    """
    body = _js_function_body(_map_js(), 'initRegionTools')
    m = re.search(r"input\.addEventListener\('input',\s*function[^{]*\{(.*?)\n    \}\);",
                  body, re.S)
    assert m, "找不到 input 的去抖监听 —— 本测试已失效"
    assert '_rememberPlaceQuery' not in m.group(1), (
        '去抖搜索里记了历史：敲一个词会把它的每个前缀都记进去')
    assert '_rememberPlaceQuery' in body, '一处都没记 —— 历史永远是空的'


def test_history_can_be_cleared():
    """必须有清除入口，且清除后立刻重绘（不能等下次打开才生效）。"""
    src = _map_js()
    body = _js_function_body(src, '_clearPlaceHistory')
    assert '_savePlaceHistory' in body, '_clearPlaceHistory 没有落盘'
    assert '_renderPlaceHistory' in body, '清完没有重绘，列表会停在旧内容上'
    assert 'map-search__history-clear' in src, '没有渲染清除按钮'


def test_history_reader_rejects_junk_from_local_storage():
    """localStorage 是用户/扩展可写的，读出来必须逐项校验。

    不校验的话一个混进来的对象会在列表里渲染成 `[object Object]`，
    点一下还会拿它当关键词发出去。
    """
    body = _js_function_body(_map_js(), '_loadPlaceHistory')
    assert 'Array.isArray' in body, '没有校验顶层是不是数组'
    assert "typeof v === 'string'" in body, '没有逐项校验元素类型'
    assert 'catch' in body, 'localStorage 在隐私模式下会抛，必须兜住'
