"""按钮几何契约（设计稿 §3.2–§3.4 / plan 2026-08-14 Task 4）。

为什么单独一份文件而不是塞进 tests/test_css_contract.py：与
tests/test_geometry_scales.py / tests/test_spacing_scale.py 同一个理由 ——
那份文件是颜色 / 按钮外观 / 动画三套层叠模型的载体（它管的是「这颗按钮**看
起来**对不对」：对比度、hover 提亮、禁用不像可点）；本文件管的是「这颗按钮
**量出来**对不对」：高度、内边距、焦点环、过渡属性表。两者的失效模式没有交集。

本文件守五件事，每一件都对应 2026-08-14 审计里的一处「同一种东西 N 份写法」：
  1. **高度只有两档**：密档 --ctl-h(28px) 给一切与输入行同行的按钮与图标钮，
     主档 --ctl-h-lg(36px) 给底条主操作。审计实测改前 5 套几何
     （36.5 / 39 / 28 / 36 / app-confirm 的 39）。
  2. **内边距只从刻度取值**：--space-* 或控件密度令牌 --ctl-pad-*，不许裸长度。
  3. **焦点环只有一份配方**：一个 outline-offset + 一个颜色令牌。
     `.map-panel-btn` 的负 offset 是有记录的例外，白名单收录并复述理由。
  4. **transition 属性表只有一份**，且动 `background-color` 而不是 `background`。
  5. `.app-confirm__btn` 不再绕开 `.btn` 自己写一套数值。

⚠️ 断言口径：**一律读层叠之后的值**，不读源码子串。
`_effective_button_height` / `_btn_computed` 就是 tests/test_css_contract.py 里
那套按钮层叠模型 —— 直接复用它，不在这里再写一套解析器。理由是承重的：
Task 3 的收尾轮出过一次「文本级断言放过了一个一像素也没渲染出来的令牌」
（`--ctl-h-lg` 的 min-height 加在自然高 39px 的按钮上，`substring in rule`
全绿而浏览器 39/39）。本文件因此**只**在层叠模型算不出来的地方才退回声明级，
每一处都在下面 _UNMODELLABLE 里写明为什么算不出来、以及退回后由谁接住。

tests/ 目录在 pytest 的 prepend 导入模式下位于 sys.path 首位，所以
`from test_css_contract import ...` 直接可用（那份文件自己就是这样被 import
的）。没有 sys.path.insert、没有 sys.modules.pop。

--------------------------------------------------------------------------
⚠️ 清单里**故意不含**地图搜索胶囊那一族（2026-08-15 登记，与
tests/test_geometry_scales.py 同一条约定）
--------------------------------------------------------------------------
那是**另一位开发者尚未提交**的组件（`git show HEAD:static/css/style.css` 里
一处都没有）。本文件的按钮宇宙按「类名里含 btn」构造（见 `_button_rules`），
那个组件的类名一个 btn 都没有，所以它**天然**不在宇宙里 —— 这不是靠一张
排除名单办到的，本文件里也因此没有任何地方写出它的选择器。它的焦点环
offset 是 +1（与本文件钉的 +2 不同），等它进了 commit 再一起收。

--------------------------------------------------------------------------
不在按钮宇宙里、且**故意不收**的三个 `<button>`（登记在此，免得下一个人
以为是漏了）
--------------------------------------------------------------------------
  · `.statusbar-pill` —— 这个类同时打在 `<button>`（三个可点读数）和
    `<span>`（其余读数）上，高度是 `100%`（撑满 --statusbar-h），几何属于
    状态栏而不是按钮档。皮肤由 tests/test_fix_templates_a11y.py::
    test_statusbar_pill_neutralises_the_ua_button_skin 与
    tests/test_elevation_glass.py（玻璃化 chrome）钉住。
  · `.task-name` —— 任务名从 `<span>` 改成的按钮，清壳（无底色/无边框/
    padding 0），几何跟着表格行走，不是控件档。
  · 那个未提交搜索组件里的下拉结果行 —— 同上（清壳、跟着列表行走），
    而且按上面那条约定，本文件不写出它的选择器。
"""

import re

import pytest

from test_css_contract import (
    _BtnCtx,
    _btn_computed,
    _btn_decls,
    _css,
    _decl_map,
    _effective_button_height,
    _norm_selector,
    _px,
    _resolve_length_px,
    _rules_ctx,
    _selector_parts,
    _token_px,
)


def _offset_px(css, value):
    """`outline-offset` 的值 -> px。负值也要认。

    `_resolve_length_px` 的正则不吃负号（它服务的是高度/内边距，那些不该为负），
    而 `.map-panel-btn` 的例外恰恰是 `-2px`：只用它的话那条白名单会被报成
    「解析不了」而不是「按例外通过」。先用 `_px`（认负号的字面量），
    再退回 `_resolve_length_px`（认 var() 链）。
    """
    got = _px(_IMPORTANT_STRIP.sub('', value).strip())
    if got is None:
        got = _resolve_length_px(css, value)
    return got


_IMPORTANT_STRIP = re.compile(r'!\s*important', re.I)

# --------------------------------------------------------------------------
# 按钮宇宙
# --------------------------------------------------------------------------

# 「按钮态选择器」= 某个分支里出现了含 `btn` 的类名。正向构造而不是列名单：
# 列名单的话，新增一个 `.xxx-btn` 规则不会有任何东西变红。
_BTN_CLASS_RE = re.compile(r'\.[-\w]*btn[-\w]*(?![-\w])')

# 从宇宙里排掉的分支 -> 理由。**只许排「不是按钮盒子」的东西**，
# 不许排「按钮盒子但不合契约」的东西。
_NOT_A_BUTTON_BOX = {
    '.btn-close':
        'Bootstrap 自带的关闭组件，不带 `.btn` 类、有自己的尺寸规则；'
        'style.css 只覆盖它的 filter/opacity（图标反白），一条几何都没写。'
        '同一条判断在 tests/test_css_contract.py 的纯图标按钮那节也写过。',
    '.btn-close:hover':
        '同上（只有 opacity）。',
    '.task-line1 .btn-group':
        '按钮**组**容器，只声明 flex 不参与按钮盒模型。',
    # 2026-08-21 删除登记：'.map-panel-btn span'（标签样式）与
    # 'html[lang="en"] .map-panel-btn'（英文宽度放宽）两条规则已随
    # 工具条纯图标化删除，排除项一并移除。
}


def _button_rules(css):
    """[(分支, 规则体, at-rule 上下文)]：全站每一条按钮态规则分支。"""
    out = []
    for sel, body, at_ctx in _rules_ctx(css):
        for branch in _selector_parts(_norm_selector(sel)):
            if not _BTN_CLASS_RE.search(branch):
                continue
            if branch in _NOT_A_BUTTON_BOX:
                continue
            out.append((branch, body, tuple(at_ctx)))
    return out


# --------------------------------------------------------------------------
# 1. 两档几何
# --------------------------------------------------------------------------

# 层叠模型算不出来的按钮类 -> (为什么算不出来, 退回后由谁接住)。
# 这不是豁免，是**记账**：两条都不是「模型偷懒」，而是模型有意拒绝的两种形态。
_UNMODELLABLE = {
    'app-confirm__btn': (
        '它有一条 `@media (max-width: 480px)` 分支（窄屏改成整宽）。'
        '`_btn_media_applies` 对宽度类断点一律返回 None，`_btn_computed` 于是'
        '判「模型算不了它的条件」并响亮失败 —— 那是 Task 1 定下的口径，'
        '不能为了让本文件好过而放宽（评审实测过：一旦模型开始猜宽度断点，'
        '往 @media 里塞一条 `.btn:disabled{background:#0d6efd}` 就能静默撤回修复）。',
        'test_app_confirm_button_consumes_the_shared_button_tokens：'
        '逐个属性比对它与公共档的**令牌表达式**是否逐字相同。',
    ),
    'map-panel-btn': (
        '它有 `:last-child`（收最后一条分隔线）形态，`_btn_branch_applies`'
        '对不支持的伪类返回 None。（原先还有 `html[lang="en"] ` 前缀形态，'
        '2026-08-21 工具条纯图标化后随文字标签一起删除。）'
        '它本来就**不在两档里**：工具条按钮是纯图标竖排按钮，'
        '高度走组件专有令牌 --map-panel-btn-h(40px)，由 '
        'tests/test_geometry_scales.py::test_map_toolbar_buttons_have_one_fixed_height '
        '钉住「只有一个高度来源」。本文件只管它的焦点环与 transition。',
    ),
}

_DENSE = '--ctl-h'
_PRIMARY = '--ctl-h-lg'

# 站内真实的按钮几何上下文。**每一个都对应一处真实标记**：
#   grep -oh "class=[\"'][^\"']*btn[^\"']*" templates/*.html static/js/*.js
# 期望档位是**设计决策**，不是抄现状：密档给「与输入行同行的按钮 + 图标钮」，
# 主档给「底条主操作」。
_FOOTER_ANCESTORS = frozenset({
    'page-content', 'page-content--fill', 'config-layout', 'config-footer',
})
# 面板内底条的第二条祖先链（2026-08-15 Task 5 新增）。为什么必须有两条：
# `.config-footer` 这套常驻底条现在有两处宿主，`.config-footer` 那一层相同、
# 上面的容器不同 ——
#   独立 /config 页面：.page-content.page-content--fill > .config-layout > .config-footer
#   新建任务面板：    .workbench-panel > .workbench-panel__body.workbench-panel__body--fill
#                     > .config-layout > .config-footer   （templates/index.html 的 #createPanel）
# 主档规则 `.config-footer .btn` 对两处同样生效，所以写成一条最小祖先集
# （只放 config-footer）也能过 —— 但那样就看不出这颗按钮真实挂在哪，
# 下次有人把它挪回滚动区里，表里的模型不会有任何反应。两条链照抄真实标记。
_PANEL_FOOTER_ANCESTORS = frozenset({
    'workbench-panel', 'workbench-panel__body', 'workbench-panel__body--fill',
    'config-layout', 'config-footer',
})

BUTTON_GEOMETRY_CONTEXTS = (
    # ---- 密档 ----
    ('index.html 浏览钮 #outputPathBrowse',
     _BtnCtx({'btn', 'btn-outline-primary', 'path-browse'},
             element_id='outputPathBrowse'), _DENSE),
    ('history 刷新钮 .btn.btn-sm.btn-outline-secondary',
     _BtnCtx({'btn', 'btn-sm', 'btn-outline-secondary'}), _DENSE),
    ('path_browser 弹窗取消钮 .btn.btn-secondary',
     _BtnCtx({'btn', 'btn-secondary'}), _DENSE),
    ('任务行图标钮 .btn.btn-icon.btn-danger（.btn-group-sm 内）',
     _BtnCtx({'btn', 'btn-icon', 'btn-danger'}, {'btn-group', 'btn-group-sm'}), _DENSE),
    ('历史表图标钮 .btn.btn-icon.btn-sm.btn-primary（无按钮组祖先）',
     _BtnCtx({'btn', 'btn-icon', 'btn-sm', 'btn-primary'}), _DENSE),
    ('配置页验证钮 .btn.btn-outline-primary.tile-server-verify',
     _BtnCtx({'btn', 'btn-outline-primary', 'tile-server-verify'}), _DENSE),
    ('配置页测速推荐钮 .btn.btn-outline-primary.concurrency-recommend',
     _BtnCtx({'btn', 'btn-outline-primary', 'concurrency-recommend'}), _DENSE),
    # （曾有一条「任务筛选行动作钮 .btn.btn-primary.btn-compact.task-filter-bar__action」
    #   —— 2026-08-15 Task 5 删除，不是放宽。四条管线的入口收敛到左列工具条那颗
    #   「新建」之后，任务筛选行右端那颗「处理」按钮（_history_content.html 的
    #   #processOpenBtn）整个退场，`.btn.task-filter-bar__action { margin-left: auto }`
    #   也随之从 style.css 删掉（删除登记见 style.css 该规则原位的注释）。
    #   这个类名现在零消费者：继续留在表里是给**不存在的标记**建模 ——
    #   模型全绿，而页面上没有任何一个像素受它管。）
    ('手动四至面板确定/取消 .btn.btn-sm（.bounds-actions 内）',
     _BtnCtx({'btn', 'btn-sm'}, {'bounds-actions'}), _DENSE),
    # ---- 主档 ----
    # 2026-08-15 Task 5：#createTaskBtn 从密档挪到主档，并补上祖先链。
    # 改前这一行是 `_BtnCtx({'btn','btn-primary','w-100'}, element_id='createTaskBtn'), _DENSE`
    # —— 无祖先、密档，因为它当时挂在 #downloadModal 的 .modal-body 里，
    # 页面上没有任何祖先选择器管得着它，只吃 `.btn` 基几何。
    # 现在它在 #createPanel 的 .config-footer 里（靠 form="taskForm" 关联表单，
    # 结构上位于 .config-scroll 之外），`.config-footer .btn { padding:
    # var(--space-1) var(--space-4); min-height: var(--ctl-h-lg) }` 胜出。
    # 档位跟着**角色**走而不是跟着 id 走：它与配置页底条的保存/重置是同一个角色
    # （常驻底条主操作），同一个角色只许有一份几何。
    # 浏览器实测（Task 5 的 E2E）：1366×768 下 rect 高 36px、bottom 756；
    # 1600×900 下 bottom 888；--ctl-h-lg = 36px —— 与主档逐像素一致。
    ('index.html 提交钮 #createTaskBtn（#createPanel 常驻底条内）',
     _BtnCtx({'btn', 'btn-primary', 'w-100'}, _PANEL_FOOTER_ANCESTORS,
             element_id='createTaskBtn'), _PRIMARY),
    ('配置页底条重置钮 #configResetBtn',
     _BtnCtx({'btn', 'btn-secondary', 'btn-sm'}, _FOOTER_ANCESTORS,
             element_id='configResetBtn'), _PRIMARY),
    ('配置页底条保存钮 .btn.btn-primary.btn-sm',
     _BtnCtx({'btn', 'btn-primary', 'btn-sm'}, _FOOTER_ANCESTORS), _PRIMARY),
)


def test_every_button_state_resolves_to_one_of_two_height_tiers():
    """每一个真实按钮上下文，**层叠之后**的高度都必须恰好落在两档之一。

    读的是 `_effective_button_height`（层叠 + 盒模型），不是「源码里有没有
    var(--ctl-h)」。这个区别是承重的：Task 3 的 `--ctl-h-lg` 第一版就是
    「源码里有、浏览器里 39px」——地板托不起自然高 39px 的按钮。

    改前实测（2026-08-15，本 Task 开工时用同一个模型量的）：
        .btn 基态 / #createTaskBtn      36.5px   （6+6 内边距 + 22.5 行高 + 1x2 边框）
        .btn-sm 一族                    39.0px   （8+8 + 21 + 2）
        .btn.btn-icon / 紧凑档 / 四至钮  28.0px
        .config-footer .btn             36.0px
        .app-confirm__btn               39.0px（声明级量的，见 _UNMODELLABLE）
      —— 一屏之内 5 个数，而它们全是「一颗按钮」。
    """
    css = _css()
    tiers = {name: _token_px(css, name) for name in (_DENSE, _PRIMARY)}
    problems, seen = [], set()
    for label, ctx, tier in BUTTON_GEOMETRY_CONTEXTS:
        got = _effective_button_height(css, ctx)
        seen.add(tier)
        if abs(got - tiers[tier]) > 0.01:
            other = [n for n, v in tiers.items() if abs(got - v) <= 0.01]
            problems.append(
                f'{label}：层叠后高度 {got}px，期望 {tier} = {tiers[tier]}px'
                + (f'（它落在了另一档 {other[0]} 上）' if other
                   else '（两档都不是 —— 这就是又长出第三套几何）'))
    assert not problems, (
        '按钮高度不止两档：\n' + '\n'.join('  ' + p for p in problems)
        + '\n密档 --ctl-h 给与输入行同行的按钮与图标钮，'
          '主档 --ctl-h-lg 给底条主操作，没有第三档'
    )
    assert seen == {_DENSE, _PRIMARY}, (
        f'上下文表只覆盖了 {sorted(seen)} —— 两档必须各有真实消费者，'
        '否则「铸了不用」的令牌又会靠一条只查子串的断言蒙过去'
    )


# 允许出现在按钮内边距里的取值。三类，每类一个理由：
#   `0`             图标钮 / 紧凑档靠固定高度定盒子，纵向内边距为 0
#   --space-*       间距刻度（tests/test_spacing_scale.py 铸的 7 级）
#   --ctl-pad-*     控件密度令牌（与 .form-control 同一组，同行同高靠它）
_PADDING_TOKEN_RE = re.compile(r'^var\(--(?:space-[-\w]+|ctl-pad-[xy])\)$')

_PADDING_LONGHANDS = ('padding-top', 'padding-bottom', 'padding-left', 'padding-right')


def _off_scale_paddings(css, label, ctx):
    """层叠之后不是刻度令牌 / 0 的那几条内边距 -> 问题描述列表。

    抽成 helper 的理由是承重的：文件末尾那条「毒化」自检必须驱动**同一个**
    消费者，而不是复刻一份判断 —— 复刻的那一份不会跟着这条一起演进，
    等于给自己发一张过期的通行证。
    """
    got = _btn_computed(css, ctx, 'base', set(_PADDING_LONGHANDS) | {'padding'})
    problems = []
    for prop in _PADDING_LONGHANDS:
        if prop not in got:
            continue          # 没有任何本站规则声明它 -> 走 Bootstrap 默认，
                              # 高度那条断言已经把纵向那两条算进去了
        raw, branch, _spec = got[prop]
        val = raw.strip()
        if val == '0' or _PADDING_TOKEN_RE.match(val):
            continue
        problems.append(
            f'{label} 的 {prop} 层叠赢家是 `{branch} {{ {prop}: {val} }}` —— '
            '期望 0 / var(--space-*) / var(--ctl-pad-*)')
    return problems


def test_button_padding_is_drawn_from_the_spacing_scale():
    """每一档按钮的四条内边距，**层叠之后**都必须是刻度令牌或 0。

    与上一条配对：高度对了但内边距是 `padding: 7px 13px` 的话，两档几何仍然
    是假的 —— 密档的按钮与同行的 `.form-control` 左右对不齐，而那正是
    「一屏 6 种控件高度」这个缺陷的另一半。
    """
    css = _css()
    problems = []
    for label, ctx, _tier in BUTTON_GEOMETRY_CONTEXTS:
        problems += _off_scale_paddings(css, label, ctx)
    assert not problems, (
        '按钮内边距没有全部走刻度：\n' + '\n'.join('  ' + p for p in problems))


# --------------------------------------------------------------------------
# 2. 焦点环：一个 offset + 一个颜色令牌
# --------------------------------------------------------------------------

FOCUS_RING_OFFSET_PX = 2.0
FOCUS_RING_COLOR_TOKEN = 'var(--color-accent)'

# 唯一的例外，按名字收录并复述理由（style.css 的 `.map-panel-btn:focus-visible`
# 注释里记着同一段）。
FOCUS_OFFSET_EXCEPTIONS = {
    '.map-panel-btn:focus-visible': (
        -2.0,
        '工具条按钮的容器 .map-panel-triggers 带 overflow:hidden（为了让胶囊圆角'
        '裁掉首尾按钮的 hover 底色），内容盒宽度正好等于按钮宽 —— 正 offset 画出的'
        '整圈都落在 padding box 之外，而 outline 不计入 scrollable overflow region，'
        '会被直接剪掉。实测：单按钮分组聚焦后屏幕上一条焦点线都没有。'
        '负 offset 把圈画进内部，不受祖先裁剪影响。',
    ),
}


def test_button_focus_ring_offset_has_exactly_one_value():
    """全站按钮态的 `outline-offset` 只许有一个值（例外见白名单）。

    声明级扫描 —— 它接的是「新写一条按钮规则、随手给个别的 offset」。
    层叠级由下一条接住，两条都需要：只扫声明会漏掉「声明对了但被压掉」，
    只算层叠会漏掉「写在源码里、今天恰好没人命中」。
    """
    css = _css()
    offenders = []
    for branch, body, _at in _button_rules(css):
        decls = _decl_map(body)
        if 'outline-offset' not in decls:
            continue
        want = FOCUS_OFFSET_EXCEPTIONS.get(branch, (FOCUS_RING_OFFSET_PX, None))[0]
        got = _offset_px(css, decls['outline-offset'])
        if got is None or abs(got - want) > 0.01:
            offenders.append(
                f'`{branch} {{ outline-offset: {decls["outline-offset"]} }}` -> {got}px，'
                f'期望 {want}px')
    assert not offenders, (
        '焦点环 offset 不止一个值：\n' + '\n'.join('  ' + o for o in offenders)
        + '\n只有 .map-panel-btn 的负 offset 是有记录的例外（理由见 '
          'FOCUS_OFFSET_EXCEPTIONS）'
    )


def test_button_focus_ring_uses_one_colour_token():
    """按钮态的焦点环颜色只许有**一个**令牌，声明级 + 层叠级各查一遍。

    改前两个：全局 `*:focus-visible` 与三处组件级用 --color-accent，
    而 `.btn:not(:disabled):not(.disabled):focus-visible` 用 --color-accent-hover
    —— 同一个键盘焦点，按钮上是一种蓝、按钮旁边的输入框上是另一种。
    """
    css = _css()
    offenders = []
    for branch, body, _at in _button_rules(css):
        for prop, val, _imp, _idx in _btn_decls(body):
            if prop != 'outline-color':
                continue
            if val.strip() != FOCUS_RING_COLOR_TOKEN:
                offenders.append(
                    f'`{branch}` 的 outline 颜色是 {val.strip()!r}，'
                    f'期望 {FOCUS_RING_COLOR_TOKEN}')
    assert not offenders, (
        '焦点环颜色令牌不止一个：\n' + '\n'.join('  ' + o for o in offenders))

    # 层叠级：每个真实上下文在 focus-visible 态**实际拿到**的颜色与 offset。
    problems = []
    for label, ctx, _tier in BUTTON_GEOMETRY_CONTEXTS:
        got = _btn_computed(css, ctx, 'focus-visible',
                            {'outline-color', 'outline-offset'})
        if 'outline-color' not in got:
            problems.append(f'{label} 在焦点态没有任何规则给出 outline 颜色')
        elif got['outline-color'][0].strip() != FOCUS_RING_COLOR_TOKEN:
            problems.append(
                f'{label} 焦点环颜色层叠赢家是 `{got["outline-color"][1]}` 的 '
                f'{got["outline-color"][0]!r}，期望 {FOCUS_RING_COLOR_TOKEN}')
        if 'outline-offset' not in got:
            problems.append(f'{label} 在焦点态没有任何规则给出 outline-offset')
        else:
            px = _offset_px(css, got['outline-offset'][0])
            if px is None or abs(px - FOCUS_RING_OFFSET_PX) > 0.01:
                problems.append(
                    f'{label} 焦点环 offset 层叠赢家是 `{got["outline-offset"][1]}` 的 '
                    f'{got["outline-offset"][0]!r} -> {px}px，'
                    f'期望 {FOCUS_RING_OFFSET_PX}px')
    assert not problems, (
        '焦点环层叠之后不是同一份配方：\n' + '\n'.join('  ' + p for p in problems))


# --------------------------------------------------------------------------
# 3. transition：一份属性表
# --------------------------------------------------------------------------

# 唯一的属性表。为什么是这五个（而不是取所有按钮的交集）：按钮的状态样式**真的**
# 会动这五个属性 —— 填充变体动 background-color/color，描边变体动 border-color，
# `:focus-visible` 把 box-shadow 清成 none，`:active` 用 filter: brightness(0.85)
# 压暗。少一个就有一处状态切换是硬跳的。
BUTTON_TRANSITION_PROPERTIES = (
    'background-color', 'border-color', 'color', 'box-shadow', 'filter',
)
BUTTON_TRANSITION_TIMING = 'var(--dur-fast) var(--ease)'

# 选择器级豁免，写明理由。键是 _button_rules 给出的规范化选择器。
BUTTON_TRANSITION_EXEMPTIONS = {
    # .tf-btn 不动背景/描边/文字色（它的状态反馈是 hover 抬升 + 边缘高光），
    # 强行套五属性表等于让它过渡一堆自己从不会变的属性；--liquid-motion 是
    # 液态玻璃签名动效令牌，时长口径由 test_geometry_scales 的动效断言另行守。
    '.tf-btn': '液态玻璃签名动效 --liquid-motion，用户裁决 2026-08-17（Task 9b）',
}


def _transition_segments(value):
    """`a 1s e, b 1s e` -> [('a', '1s e'), ('b', '1s e')]。"""
    out = []
    for seg in value.split(','):
        parts = seg.split()
        if not parts:
            continue
        out.append((parts[0].lower(), ' '.join(parts[1:])))
    return out


def test_every_button_transition_has_the_same_property_list():
    """全站按钮态的 transition 属性表必须逐字是同一份，且动 `background-color`。

    改前 5 种写法（1/2/3/4/5 个属性）。`background` 与 `background-color` 的
    区别不是洁癖：`background` 是简写，会把 background-image / -position /
    -size 一起纳入过渡；`.app-confirm__btn` 用的正是它。

    动效时长/缓动本身由 tests/test_geometry_scales.py 那条「transition 只许取
    --dur-* + --ease」钉住，这里只钉**属性表**与**每段的时长表达式一致**。
    """
    css = _css()
    offenders = []
    for branch, body, _at in _button_rules(css):
        if branch in BUTTON_TRANSITION_EXEMPTIONS:
            continue
        decls = _decl_map(body)
        if 'transition' not in decls:
            continue
        segs = _transition_segments(re.sub(r'\s+', ' ', decls['transition']).strip())
        props = tuple(p for p, _t in segs)
        if props != BUTTON_TRANSITION_PROPERTIES:
            offenders.append(
                f'`{branch}` 的属性表是 {props}，期望 {BUTTON_TRANSITION_PROPERTIES}')
        for prop, timing in segs:
            if timing != BUTTON_TRANSITION_TIMING:
                offenders.append(
                    f'`{branch}` 的 {prop} 段时长是 {timing!r}，'
                    f'期望 {BUTTON_TRANSITION_TIMING!r}')
    assert offenders == [], (
        '按钮的 transition 不是同一份属性表：\n' + '\n'.join('  ' + o for o in offenders))


def test_no_button_animates_the_background_shorthand():
    """没有任何按钮规则拿 `background` 简写做过渡。

    与上一条分开写：上一条比的是整张表，一旦有人把某一段写成
    `background var(--dur-fast) var(--ease)`，上一条的报错会指向「属性表不对」
    而不是「你动的是简写」。这条给出正确的诊断。
    """
    css = _css()
    offenders = [
        f'`{branch}` 的 transition: {_decl_map(body)["transition"].strip()}'
        for branch, body, _at in _button_rules(css)
        if 'transition' in _decl_map(body)
        and any(p == 'background' for p, _t in _transition_segments(
            re.sub(r'\s+', ' ', _decl_map(body)['transition'])))
    ]
    assert not offenders, (
        'transition 里出现了 `background` 简写（应为 background-color）：\n'
        + '\n'.join('  ' + o for o in offenders))


# --------------------------------------------------------------------------
# 4. .app-confirm__btn 回到公共几何
# --------------------------------------------------------------------------

# `.app-confirm__btn` 与主档必须逐字相同的几何属性。
# 为什么比**令牌表达式**而不是比算出来的 px：那两颗按钮由 ui.js 动态创建，
# 不带 `.btn` 类，层叠模型算不到它（理由见 _UNMODELLABLE）。比表达式能守住
# 「同一个决策只有一个来源」——数值相等但令牌不同，下次调档位就会漂开。
APP_CONFIRM_SHARED_GEOMETRY = {
    'min-height': 'var(--ctl-h-lg)',
    'padding': 'var(--space-1) var(--space-4)',
    'border-radius': 'var(--radius-xs)',
    'font-weight': 'var(--weight-medium)',
    'border': '1px solid transparent',
}

PRIMARY_TIER_SELECTOR = '.config-footer .btn'
BTN_BASE_SELECTOR = '.btn'


def _decls_of(css, selector):
    """顶层规则里，某个**分支恰好等于** selector 的那条规则的声明表。"""
    bodies = [
        body for sel, body, at_ctx in _rules_ctx(css)
        if not at_ctx and selector in _selector_parts(_norm_selector(sel))
    ]
    assert len(bodies) == 1, (
        f'`{selector}` 有 {len(bodies)} 条顶层规则，期望 1 条 —— 本测试已失效')
    return _decl_map(bodies[0])


def test_app_confirm_button_consumes_the_shared_button_tokens():
    """确认弹窗的两颗按钮必须消费公共几何令牌，不再自己写一套。

    改前它整套自己写：内边距 --space-2/--space-4、没有任何高度档（自然高
    39px）、transition 三个属性且动 `background` 简写、焦点环用另一个颜色令牌。
    style.css 里当时明写着「ui.js 的 confirm 按钮不走 .btn 体系」——那句话
    描述的是 markup 事实（它确实不带 .btn 类），但被当成了「所以它可以有
    自己的几何」的许可证：同一个 `#fff` 对比度 bug 就是这样在 .btn-* 修完
    之后还在它身上留了三个月。

    钉的是**令牌表达式逐字相同**，且主档的两个高度/内边距令牌与
    `.config-footer .btn` 是同一个来源。
    """
    css = _css()
    confirm = _decls_of(css, '.app-confirm__btn')
    problems = [
        f'.app-confirm__btn 的 {prop} 是 {confirm.get(prop)!r}，期望 {want!r}'
        for prop, want in APP_CONFIRM_SHARED_GEOMETRY.items()
        if (confirm.get(prop) or '').strip() != want
    ]
    # 主档那两条必须真的来自同一个决策：底条按钮怎么写，它就怎么写。
    footer = _decls_of(css, PRIMARY_TIER_SELECTOR)
    for prop in ('min-height', 'padding'):
        if (footer.get(prop) or '').strip() != APP_CONFIRM_SHARED_GEOMETRY[prop]:
            problems.append(
                f'{PRIMARY_TIER_SELECTOR} 的 {prop} 是 {footer.get(prop)!r}，'
                f'与 .app-confirm__btn 的 {APP_CONFIRM_SHARED_GEOMETRY[prop]!r} 不是'
                '同一个表达式 —— 主档被写成了两份')
    # 圆角/字重/边框来自 `.btn` 基规则，同样比表达式。
    base = _decls_of(css, BTN_BASE_SELECTOR)
    for prop in ('border-radius', 'font-weight', 'border'):
        if (base.get(prop) or '').strip() != APP_CONFIRM_SHARED_GEOMETRY[prop]:
            problems.append(
                f'{BTN_BASE_SELECTOR} 的 {prop} 是 {base.get(prop)!r}，'
                f'与 .app-confirm__btn 的 {APP_CONFIRM_SHARED_GEOMETRY[prop]!r} 不同')
    assert not problems, (
        '确认弹窗按钮还在自己写一套几何：\n' + '\n'.join('  ' + p for p in problems))


# --------------------------------------------------------------------------
# 5. .btn-info 两头一起删
# --------------------------------------------------------------------------

def test_btn_info_has_no_rule_branches():
    """`.btn-info` 的规则分支数为 0。

    它零引用（templates/ static/js/ src/ app.py 全仓无命中），唯一的存在理由
    是给 tests/test_css_contract.py 的按钮层叠模型当被测对象 —— 而模型里还有
    4 个真实变体（primary/success/warning/danger）够用。

    这条与三处改动**同一轮**落地，缺一处就是「一头删一头留」：
      · tests/test_css_contract.py 的 FILLED_BTN_VARIANTS 去掉 'btn-info'
        （BUTTON_CONTEXTS 11 -> 10、矩阵 55 -> 50 格随之）；
      · tests/test_fix_templates_a11y.py 那条双向跨文件锁整条删除，
        `.btn-info` 改记进同文件 `_DELETED_DEAD_CLASSES`；
      · style.css 四条规则分支 + `--color-info-hover`（删后零引用）一并删除。
    """
    css = _css()
    branches = [b for b, _body, _at in _button_rules(css)
                if re.search(r'\.btn-info(?![-\w])', b)]
    assert branches == [], (
        f'.btn-info 还有 {len(branches)} 条规则分支：{branches}\n'
        '它零引用，且模型侧的登记（FILLED_BTN_VARIANTS）已经撤掉 —— '
        '两头必须一起删'
    )


# --------------------------------------------------------------------------
# 6. 模型自检：padding-block / padding-inline 不再是盲区
# --------------------------------------------------------------------------

def test_button_height_model_sees_logical_padding():
    """`_effective_button_height` 必须看得见 `padding-block` / `padding-inline`。

    **这条是 2026-08-15 台账里 4 号结构性缺口的正面防线。** 修之前：
    `_effective_button_height` 只向 `_btn_computed` 要
    `padding` / `padding-top` / `padding-bottom`，而 `_btn_decls` 只展开
    outline / border 两个简写。于是一条 `padding-block: 20px` 的按钮规则
    在模型眼里**不存在**，模型静默回落到 Bootstrap 的 6px：模型说一个数、
    浏览器渲染另一个数，而每一条断言都是绿的。Task 3 因此被迫在
    `.config-footer .btn` 上写 `padding-top/-bottom` 两条长写，并在 style.css
    里留了一段注释解释「为什么不用 padding-block」——那段注释描述的是模型的
    缺陷，不是 CSS 的偏好。

    Task 4 选的是**修模型**（另一条路是继续用长写并断言 `padding-block`
    不出现）。理由：本 Task 要重写每一颗按钮的内边距，而「模型看不见的写法」
    是一个会静默生效的陷阱，留着它等于把下一个人推下去。

    自检用的是内存里的变异样式表，不落盘。
    """
    css = _css()

    # ① 声明展开：两个逻辑简写都要落成四条长写，且两值形态要分得清上下。
    got = dict((p, v) for p, v, _i, _o in _btn_decls('padding-block: 4px 8px'))
    assert got.get('padding-top') == '4px' and got.get('padding-bottom') == '8px', (
        f'`padding-block: 4px 8px` 展开成 {got} —— 模型仍看不见逻辑内边距')
    got = dict((p, v) for p, v, _i, _o in _btn_decls('padding-inline: 4px 8px'))
    assert got.get('padding-left') == '4px' and got.get('padding-right') == '8px', (
        f'`padding-inline: 4px 8px` 展开成 {got}')
    # 四值 padding 简写也要按位展开（改前 `_effective_button_height` 拿
    # `value.split()[0]` 当下内边距，四值形态下算错）。
    got = dict((p, v) for p, v, _i, _o in _btn_decls('padding: 1px 2px 3px 4px'))
    assert (got.get('padding-top'), got.get('padding-right'),
            got.get('padding-bottom'), got.get('padding-left')) == \
        ('1px', '2px', '3px', '4px'), f'四值 padding 展开成 {got}'

    # ② 层叠 + 盒模型：变异样式表里那条 padding-block 必须真的抬高模型算出的高度。
    ctx = _BtnCtx({'btn', 'btn-primary', 'w-100'}, element_id='createTaskBtn')
    before = _effective_button_height(css, ctx)
    mutated = css + '\n.btn { padding-block: 20px; }\n'
    after = _effective_button_height(mutated, ctx)
    assert after - before > 20.0, (
        f'给 `.btn` 追加 `padding-block: 20px` 之后，模型算出的高度从 {before} 变成 '
        f'{after} —— 差值 {after - before} 说明模型仍在忽略它（盲区还在）')


# --------------------------------------------------------------------------
# 5. padding 剩下的三条静默通道（2026-08-15 Task 4 收尾轮）
# --------------------------------------------------------------------------
# Task 4 的首版补上了三个盲区（逻辑简写不存在 / 四值取错位 / 简写长写跨规则
# 层叠），但它自己引进并留下了三条新的静默通道。三条都是**同一个失效模式**：
# 模型遇到看不懂的东西时没有响亮失败，而是回落到一个恰好存在的旧值。
#
#   P（毒化缺失）：`_split_padding` 切不开时 `_btn_decls` 只留简写、不发长写，
#     于是那条简写**静默输给**任何更早的长写 —— 而 style.css 的 `.btn` 本身写的
#     就是 `padding: var(--space-1) var(--space-3)`，模型把它展成了长写，
#     所以「更早的长写」根本不需要有人手写，天然就在那里。
#   H（属性名轴静默）：四条逻辑长写 `padding-{block,inline}-{start,end}` 既不在
#     `_PADDING_SHORTHANDS` 也不在任何 `props` 集合里，被 `_btn_computed` 的
#     `d[0] in props` 无声丢掉。
#   M（错位回落仍在）：`_effective_button_height` 里 `padding` 简写取
#     `raw.split()[0]` 当纵向内边距那段死代码，只是**恰好**安全。
#
# 实测探针见下方 `_MODEL_CTX`：`.btn.btn-primary.w-100#createTaskBtn`，**故意不带
# 任何祖先**，量的是 `.btn` 的基几何。基线 28.0 = clamp(4+4+17, min-height 28)，
# 其中 17 = line-height + 2x边框。
# ⚠️ 这个 28.0 不是页面上那颗按钮的高度：#createTaskBtn 真实位置在 `#createPanel`
# 的 `.config-footer` 里，`.config-footer .btn { min-height: var(--ctl-h-lg) }` 胜出，
# 模型与浏览器都是 36.0（见 BUTTON_GEOMETRY_CONTEXTS 的主档那一行）。探针不带祖先
# 是有意的：本节要钉的是「简写/别名/毒化」这条解析路径，越少无关规则参与越好，
# 换档位时这三条自检也不用跟着改数。以下每一行都是探针上下文里的数：
#   padding: calc(1px + 2px)                 -> 响亮（唯一本来就对的形态）
#   padding: 2px 4px 30px calc(1px + 2px)    -> 静默 28.0（浏览器 2+30+17=49）
#   padding-block: calc(1px + 2px)           -> 静默 28.0（浏览器 3+3+17=23）
#   padding-inline: calc(1px + 2px)          -> 静默（纵向不动，横向读到旧值）
#   padding-block-start: 20px                -> 静默 28.0（浏览器 20+4+17=41）
#   padding-inline-start: 20px               -> 静默 28.0
# 修法与「为什么三条一起改」记在 tests/test_css_contract.py 的
# `_PADDING_SHORTHANDS` / `_PADDING_LOGICAL_LONGHANDS` 上方。

# 自身含 `calc()` 的值：`_PADDING_ONE_VALUE_RE` 只认 `var(--x)` 与「数字+单位」
# 两种整体形态，所以它连不含空格的 `calc(1px+2px)` 也不认 —— 这里两种都用上。
_UNSPLITTABLE = 'calc(1px + 2px)'
_UNSPLITTABLE_TIGHT = 'calc(1px+2px)'

_MODEL_CTX = _BtnCtx({'btn', 'btn-primary', 'w-100'}, element_id='createTaskBtn')
#     ↑ 不带祖先集合，也不带 .config-footer —— 见上方 ⚠️。


def _mutated(css, decl):
    """给 `.btn` 追加一条声明的**内存**样式表。落盘是禁止的（见文件头）。"""
    return css + '\n.btn { %s; }\n' % decl


def _sides(decl):
    """`_btn_decls` 展开结果 -> {属性: 值}（同名取最后一条，与层叠同向）。"""
    return dict((p, v) for p, v, _i, _o in _btn_decls(decl))


def test_unsplittable_padding_shorthand_fails_loudly_instead_of_losing_silently():
    """切不开的 padding 简写必须**毒化**它本该设的每一条边，而不是静默让位。

    这是上面登记的 P。判据不是「抛异常」而是「异常里念得出那个值」——
    一条读不懂的报错和一个静默的错数字，对下一个人来说差别就在这句话上。
    """
    css = _css()

    # ① 声明级：四条边都要带上那个切不开的原值，一条都不许缺。
    for name, want in (
        ('padding', ('padding-top', 'padding-right', 'padding-bottom', 'padding-left')),
        ('padding-block', ('padding-top', 'padding-bottom')),
        ('padding-inline', ('padding-left', 'padding-right')),
    ):
        for value in (_UNSPLITTABLE, _UNSPLITTABLE_TIGHT):
            got = _sides(f'{name}: {value}')
            assert [got.get(s) for s in want] == [value] * len(want), (
                f'`{name}: {value}` 展开成 {got} —— 切不开的简写没有毒化 {want}，'
                '它会静默输给更早的长写（`.btn` 自己那条 padding 展出来的就是）')
            assert got.get(name) == value, (
                f'`{name}: {value}` 没留下简写本身 —— 报错时引用不到出问题的那一句')

    # ② 纵向：高度模型必须响亮，且报错里念得出那个值。
    #
    # ⚠️ 兜底走 `pytest.fail` 而不是 `raise AssertionError` —— 这是条通用陷阱，
    # 值得记一行：**兜底断言的消息绝不能落进它自己的 match**。这里的兜底消息里
    # 含 `decl`，而 `decl` 逐字含 `calc(1px`，正好命中外层 `match=r'calc\(1px'`，
    # 于是「模型退化了」这个坏消息被 `pytest.raises` 当成期望结果吞掉。
    # 2026-08-15 实测：把 `_split_padding` 猴补丁回旧行为（切不开就一条长写都不
    # 发），这 4 条 decl **全部通过**，看守是假的。`pytest.fail` 抛的是
    # `Failed`（`OutcomeException` 下的 `BaseException`，不是 `AssertionError`），
    # 穿得过 `pytest.raises(AssertionError, ...)`，同一个猴补丁下 4 条全红。
    for decl in (f'padding: {_UNSPLITTABLE}',                     # 对照：本来就对的形态
                 f'padding: 2px 4px 30px {_UNSPLITTABLE}',
                 f'padding-block: {_UNSPLITTABLE}',
                 f'padding-block: 2px {_UNSPLITTABLE}'):
        with pytest.raises(AssertionError, match=r'calc\(1px'):
            got = _effective_button_height(_mutated(css, decl), _MODEL_CTX)
            pytest.fail(                     # pragma: no cover - 只在回归时走到
                f'`{decl}` 没有让高度模型失败，它安静地算出了 {got} —— '
                '静默通道 P 又回来了')

    # ③ 横向：高度模型不读左右两边，接住它的是刻度那条消费者（同一个 helper）。
    for decl in (f'padding-inline: {_UNSPLITTABLE}',
                 f'padding: 2px {_UNSPLITTABLE}'):
        problems = _off_scale_paddings(_mutated(css, decl), '毒化自检', _MODEL_CTX)
        assert any(_UNSPLITTABLE in p for p in problems), (
            f'`{decl}` 之后刻度检查报的问题是 {problems} —— 没有一条念到 '
            f'{_UNSPLITTABLE!r}，说明左右两边读到的还是更早的旧值')


def test_btn_decls_understands_every_padding_property_name():
    """`padding-*` 这条属性名轴上不许有静默丢弃：认得的映射掉，不认的响亮。

    这是上面登记的 H。`_btn_computed` 的 docstring 说「任何排在安全网前面的
    continue 都是一个静默漏检口」—— `d[0] in props` 那个过滤正是这样一个口子，
    只不过它筛的是属性名而不是选择器。安全网补在 `_btn_decls` 末尾，所以是
    **整条轴**关上了，不是把今天这四个名字列一遍。

    别名表按 `horizontal-tb` + `ltr` 展开（inline-start = 左）。这个前提不是
    本轮新引进的：`_PADDING_SHORTHANDS` 把 `padding-inline` 映到左右两边时就
    已经这么假设了，站点也只有 zh-CN / en 两种横排 LTR 文案。
    """
    css = _css()

    # ① 四条逻辑长写。期望表在这里**手写**，不从被测模块 import ——
    #    import 过来的话，表写错了测试会跟着一起错。
    for logical, physical in (('padding-block-start', 'padding-top'),
                              ('padding-block-end', 'padding-bottom'),
                              ('padding-inline-start', 'padding-left'),
                              ('padding-inline-end', 'padding-right')):
        got = _sides(f'{logical}: 20px')
        assert got.get(physical) == '20px', (
            f'`{logical}: 20px` 展开成 {got} —— 模型看不见它，'
            f'`d[0] in props` 会把它无声丢掉')
        assert got.get(logical) == '20px', (
            f'`{logical}: 20px` 没留下原属性名 —— 报错时引用不到出问题的那一句')

    # ② 层叠 + 盒模型：`padding-block-start` 必须真的抬高模型算出的高度。
    #    实测 41.0 = 20(上) + 4(下，来自 `.btn` 那条 padding) + 17(行高+边框)，
    #    与浏览器一致；改前是静默的 28.0（min-height 撑出来的基线）。
    got = _effective_button_height(_mutated(css, 'padding-block-start: 20px'), _MODEL_CTX)
    assert got == pytest.approx(41.0, abs=0.01), (
        f'追加 `padding-block-start: 20px` 之后模型算出 {got}，期望 41.0'
        '（28.0 = 它又被无声丢掉了）')

    # ③ 安全网：既不是长写、也不在两张表里的 `padding-*` 必须响亮。
    #    这一条接的是拼错和「CSS 又出了个新 padding 属性」两种情况。
    for bogus in ('padding-botom', 'padding-block-middle', 'padding-inline-centre'):
        with pytest.raises(AssertionError, match=re.escape(bogus)):
            _btn_decls(f'{bogus}: 4px')


def test_button_height_model_uses_the_bottom_value_of_a_four_value_padding():
    """四值 `padding` 的**下**内边距必须取第 3 位，模型级、不只是展开级。

    这是上面登记的 M 的正面防线。上一条 Task 4 的自检只验到 `_btn_decls` 的
    展开结果，模型自己那段 `raw.split()[0]`（取第 0 位当纵向内边距）因此可以
    继续留在文件里当死代码 —— 它不产生错数只是因为展开总会同时给出上下两条
    长写，属于**碰巧**安全而不是设计安全。本条把模型算出来的那个数钉住，
    删掉那段死代码之后它守的就是同一个事实。

    为什么这里不去断言「源码里没有 `split()[0]`」：本文件的口径是一律读层叠
    之后的值、不读源码子串（见文件头）。那段死代码的惰性化变体在行为上与
    正确实现**不可区分**（长写永远存在，回落分支永远走不到），能接住它的是
    毒化那条自检，而不是一句字符串检查。
    """
    css = _css()
    # 四值形态：下内边距必须是第 3 位（30px），不是第 0 位（2px）。
    #   取对 -> 2 + 30 + 17 = 49.0；取成第 0 位 -> 2 + 2 + 17 = 21 -> 被
    #   min-height 28 撑成 28.0。两个数分得开，这条断言才有意义。
    got = _effective_button_height(_mutated(css, 'padding: 2px 4px 30px 8px'), _MODEL_CTX)
    assert got == pytest.approx(49.0, abs=0.01), (
        f'`padding: 2px 4px 30px 8px` 之后模型算出 {got}，期望 49.0'
        '（28.0 = 下内边距取成了第 0 位）')
