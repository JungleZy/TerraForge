"""几何刻度契约（设计稿 §3.2–§3.7 / plan 2026-08-14 Task 3）。

为什么单独一份文件而不是塞进 tests/test_css_contract.py：与
tests/test_spacing_scale.py 同一个理由 —— 那份文件是色彩 / 按钮 / 动画三套
层叠模型的载体，本文件钉的是**几何与令牌覆盖率**，两者的失效模式没有交集。

本文件守六件事：
  1. 圆角只剩 4 级，退役的三级零引用；「浮起表面」一律 12px、「控件」一律 6px；
     且全文 `border-radius` 不许有裸长度（豁免见 RADIUS_LITERAL_EXEMPTIONS）；
  2. 控件高度只有两级（`--ctl-h` 28px 密集档 / `--ctl-h-lg` 36px 主操作档），
     同一行上的控件同高，工具条按钮有**一个**固定高度；
  3. 全文 `font-size` 不许有裸 px/rem，六级字号每级都被消费；
  4. 全文 `font-weight` 不许有裸整数，三级字重每级都被消费；
  5. 全文 `z-index` 不许有裸整数，层栈令牌的值与改前实测**逐一相等**；
  6. 全文 `transition` 的时长只许取 `--dur-fast/-base/-slow`，`--ease` 被消费。

⚠️ 直接复用 tests/test_css_contract.py 的解析器（`_rules` / `_decl_map` /
`_resolve_length_px`），不在这里再写一套。理由是承重的：本文件对圆角的判断
必须与那边的按钮/表单几何模型**同一个口径** —— 各写一套解析器时，两边对
`var()` 链、`!important`、简写的处理一旦分叉，就会出现「一边说 12px、
另一边说 10px，两边都绿」。tests/ 目录在 pytest 的 prepend 导入模式下位于
sys.path 首位，所以这里 `import test_css_contract` 直接可用（那份文件自己
就是这样被 import 的）。

没有 sys.path.insert：tests/conftest.py 已经把仓库根放进 sys.path。
"""

import os
import re

from test_css_contract import (
    _BtnCtx,
    _css,
    _custom_property_raw,
    _decl_map,
    _effective_button_height,
    _norm_selector,
    _resolve_length_px,
    _resolve_z_index,
    _rules,
)

# --------------------------------------------------------------------------
# 2026-08-15：`.map-search*` 三条摘出 -> 同日放回
# --------------------------------------------------------------------------
#
# 那天早些时候，`.map-search__panel` / `.map-search__input` / `--z-map-search`
# 三条被从下面三份清单里摘掉，理由是地图搜索胶囊当时是**另一位开发者尚未提交**
# 的组件（`git show HEAD:static/css/style.css | grep -c map-search` == 0）。
# 同日该组件随 commit 59459b1 进了 master（同一条命令现在数到 32 处），放回条件
# 达成，三条已各自回到原清单（原地都留了一行说明）。
#
# ⚠️ 留下来的是方法论，下一个人遇到同类情况照着做：
#   **本目录的测试不许依赖只存在于工作区的选择器。** 判定办法不是靠读 git 状态，
#   而是模拟一次干净 checkout —— 把样式表副本里的块名改掉（当时是把
#   `map-search` 全文替换成 `zzz-absent-search`），用 monkeypatch 把 `_css()`
#   指到那份副本再跑。若有断言变红，且红的原因是「测试引用了不存在的东西」而
#   不是刻度契约被破，那条断言就依赖了未提交代码，必须摘出并在原地登记放回条件。
#   反过来同样有用：放回之后拿同一招把副本里的声明改坏（例如把面板圆角换成
#   `--radius-xs`），确认**会红** —— 否则放回的是一条恒真断言。

# --------------------------------------------------------------------------
# 1. 圆角：7 级 -> 4 级
# --------------------------------------------------------------------------

# 退役的三级。测试只管**零 `var()` 引用**，定义留不留随意 —— 2026-08-15 的
# 落地选择是连定义一起删（tests/test_spacing_scale.py 的
# test_every_scale_step_is_actually_used 立过规矩：铸了不用的令牌比字面量更坏，
# 它看着像家规，实际没有任何东西按它渲染）。
RETIRED_RADIUS_TOKENS = ('--radius-sm', '--radius', '--radius-lg')

# 保留的四级。语义分工（2026-08-15 定，落地时逐条判过每一个消费者）：
#   4px  贴在文字上的小块：内联 <code>、kbd 键帽、滚动条滑块、点击编辑的高亮底
#   6px  **一切控件**：按钮、输入、选择、可点列表行，以及与控件同行同高的缩略图
#   12px **一切浮起表面**：带阴影或浮在内容之上的东西（卡片、弹层、toast、
#        气泡、浮在地图上的信息条）
#   999px 胶囊
KEPT_RADIUS_TOKENS = {
    '--radius-xxs': 4.0,
    '--radius-xs': 6.0,
    '--radius-card': 12.0,
    '--radius-pill': 999.0,
}

# 「浮起表面」= 12px。判据是**带阴影 / 浮在内容之上**，不是「看着像块面板」。
#
# ⚠️ 这份清单是 2026-08-15 用 `_rules` + `_decl_map` 把全文 border-radius
#    声明列出来后逐条判的，与 plan 里那份 2026-08-14 写的清单有两处不同：
#      · plan 列了 `.workbench-panel__body` —— 它**一条 border-radius 都没有**
#        （只有 flex / overflow / padding），圆角来自宿主 `.card`。不在清单里。
#      · plan 没列 `.alert` / `.hint::after` / `.history-map` /
#        `.config-section` / `.page-content .config-footer` —— 前两个都带
#        box-shadow（`.alert` 是 --shadow-sm、`.hint::after` 是 --shadow-md），
#        按同一判据必须在内；后三个改前就已经是 `--radius-card`。
RAISED_SURFACES = (
    '.map-overlay-chip',
    '.history-map',
    '.alert',
    '.card',
    '.card-header',
    '.stat-card',
    '.config-section',
    '.modal-content',
    '.app-toast',
    '.app-confirm',
    '.cmdk__dialog',
    '.tif-info',
    '.hint::after',
    '.page-content .config-footer',
    # 2026-08-15 曾因组件未提交摘出、同日随组件进 commit 后放回：它带
    # box-shadow: var(--shadow-md)，按同一判据（带阴影 / 浮在内容之上）必须在内。
    '.map-search__panel',
)

# 「控件」= 6px。
#
# `.btn` 基规则也在内：改前它**一条 border-radius 都没有**，靠 Bootstrap 的
# `--bs-btn-border-radius: var(--bs-border-radius)`（.375rem = 6px）恰好等于
# `--radius-xs`。「恰好等于」不是契约 —— 换个 Bootstrap 小版本、或者有人改
# `--bs-border-radius`，全站按钮圆角就静默漂走。所以本站自己声明一次。
CONTROLS = (
    '.btn',
    '.btn-sm',
    '.btn.btn-icon',
    ('.btn.btn-compact, .btn.tile-server-verify, .btn.concurrency-recommend, '
     '.btn.path-browse, .bounds-actions .btn'),
    '.app-confirm__btn',
    '.dock-collapse-btn',
    '.form-control, .form-select',
    '.page-link',
    '.bounds-edit-input',
    '.cmdk__item',
    '.modal .progress',
    '.map-style-preview',
    '.contour-style-preview',
    '.detail-gap-samples',
    '.detail-artifact-problems',
    '.task-log__body',
    '.modal-bounds-summary',
)


def _radius_px(css, selector):
    """规范化后**恰好等于** `selector` 的规则里声明的 border-radius -> px。

    只取第一个分量（`var(--radius-card) var(--radius-card) 0 0` 这种四角写法
    取左上角）。找不到声明返回 ('missing', None)，解析不了返回 ('unparsed', 原值)。
    """
    for sel, body in _rules(css):
        if _norm_selector(sel) != selector:
            continue
        raw = _decl_map(body).get('border-radius')
        if raw is None:
            continue
        first = re.sub(r'!important', '', raw, flags=re.I).strip().split()[0]
        px = _resolve_length_px(css, first)
        return ('ok', px) if px is not None else ('unparsed', raw)
    return ('missing', None)


def test_kept_radius_tokens_have_the_designed_values():
    """保留的四级圆角就是设计稿 §3.2 定的四个数。"""
    css = _css()
    got = {name: _resolve_length_px(css, _custom_property_raw(css, name))
           for name in KEPT_RADIUS_TOKENS}
    assert got == KEPT_RADIUS_TOKENS, f'圆角刻度是 {got}，期望 {KEPT_RADIUS_TOKENS}'


def test_retired_radius_tokens_have_zero_references():
    r"""`--radius-sm` / `--radius` / `--radius-lg` 不许再被任何 `var()` 引用。

    先剥注释：本仓库的 CSS 注释**逐字复述**被改掉的旧值（「原 `--radius-sm`(8px)」
    这种），不剥的话一句说明文字就能让本条永远红。

    `--radius` 的匹配写成完整的 `var(--radius)` 形态：`--radius-card` 是
    `--radius` 的前缀扩展，只搜 `--radius` 会把 4 个活着的引用全抓进来。

    ⚠️ 这条断言的覆盖边界（2026-08-15 记，别以为它比实际管得多）：它只查
    **引用**（`var(--radius-sm)`），不查**定义**。把 `--radius-sm: 8px;` 加回
    `:root` 而一处不引用，本条照样绿 —— 而且全套测试里没有第二条能接住它
    （`test_kept_radius_tokens_have_the_designed_values` 只比清单内那四个键，
    `tests/test_spacing_scale.py::test_every_scale_step_is_actually_used` 管的是
    间距刻度）。也就是说「圆角的孤儿定义」今天无人看守。
    没有顺手补上的原因是它需要一条「`:root` 里的 `--radius-*` 恰好是这四个」的
    全量断言，那会连带决定「主题覆盖块里能不能再定义圆角」，是一条独立的决策，
    不搭本轮的车 —— 但它已经记进 Task 3 的账本，不是被忘了。
    """
    css = re.sub(r'/\*.*?\*/', '', _css(), flags=re.S)
    leftovers = {}
    for name in RETIRED_RADIUS_TOKENS:
        hits = len(re.findall(r'var\(\s*' + re.escape(name) + r'\s*\)', css))
        if hits:
            leftovers[name] = hits
    assert not leftovers, (
        f'退役的圆角令牌还在被引用：{leftovers} —— 圆角还是 7 级，不是 4 级'
    )


def test_every_raised_surface_resolves_to_the_card_radius():
    """每一个「浮起表面」的 border-radius 都解析成 12px。

    用 `_resolve_length_px` 跟着 `var()` 链算到 px，**不做字符串比对**：
    改前 `.card` 写 `var(--radius-card)`、`.card-header` 写 `var(--radius)`，
    两条都「引用了圆角令牌」，字符串比对看不出同一张卡外框 12px 表头 10px。
    """
    css = _css()
    problems = []
    for sel in RAISED_SURFACES:
        state, px = _radius_px(css, sel)
        if state == 'missing':
            problems.append(f'{sel}: 没有 border-radius 声明')
        elif state == 'unparsed':
            problems.append(f'{sel}: border-radius 是 {px!r}，解析不出 px')
        elif px != 12.0:
            problems.append(f'{sel}: 解析成 {px}px，期望 12px')
    assert not problems, '浮起表面的圆角不齐：\n' + '\n'.join('  ' + p for p in problems)


def test_every_control_resolves_to_the_control_radius():
    """每一个控件类的 border-radius 都解析成 6px。"""
    css = _css()
    problems = []
    for sel in CONTROLS:
        state, px = _radius_px(css, sel)
        if state == 'missing':
            problems.append(f'{sel}: 没有 border-radius 声明')
        elif state == 'unparsed':
            problems.append(f'{sel}: border-radius 是 {px!r}，解析不出 px')
        elif px != 6.0:
            problems.append(f'{sel}: 解析成 {px}px，期望 6px')
    assert not problems, '控件圆角不齐：\n' + '\n'.join('  ' + p for p in problems)


# 2026-08-15 Task 5 删除登记：这里原有
# `test_no_local_radius_patch_for_a_single_button_instance` ——
# 它断言 `.btn.task-filter-bar__action` 的 border-radius 是 'missing'。
#
# 原来的理由（保留下来，免得历史丢了）：那条规则改前是
# `{ margin-left: auto; border-radius: var(--radius-sm) }`，存在的唯一理由是
# 「紧凑档按钮没声明圆角、落回 Bootstrap 的 6px，而同行输入框是 8px」那 2px 的
# 缝（注释里逐字写着）。`.btn.btn-compact` 补上显式 --radius-xs、`.form-control`
# 也收到 6px 之后，这个补丁修的问题就不存在了；留着它的代价不是多一行，而是
# 「一个实例一条特例」这种形态的样板会被下一个人照抄。
#
# 现在连选择器本身都没了：四条管线的入口收敛成左列工具条那颗「新建」，
# 任务筛选行右端的 #processOpenBtn（_history_content.html）退场，
# `.btn.task-filter-bar__action` 的那条 CSS 规则也一起删掉了。
# 于是 `_radius_px(css, '.btn.task-filter-bar__action')` 会因为**规则整条不在**
# 而返回 'missing' —— 断言照样绿，但守的是空气。这种「因为空所以过」的断言比
# 没有断言更坏：它占着一条测试名，让人以为这块还有人看着。所以删，不是放宽。


# 圆角的「不许裸长度」总禁令（2026-08-15 补）。
#
# ⚠️ 补它的理由是一处**不对称**：五套刻度里，font-size / font-weight / z-index
#    各有一条 blanket「不许裸字面量」的断言，圆角**一条都没有**。上面那三条
#    per-selector 断言只查清单里点到名的选择器，清单外随便写。后果不是学术的：
#    `html[lang="en"] .map-panel-triggers { border-radius: 17px }` 这个 17px
#    不是被**豁免**的，它是**没人管**的 —— 今天往任何地方新加一条
#    `border-radius: 9px`，全套测试全绿。本轮就地补上，而不是记账留给 Task 4：
#    Task 4 不碰圆角，留着等于让 Task 3 声称收敛完成的那套刻度继续开着口子。
#
# 值级豁免（**不按选择器**，按值的语义 —— 这两种写法压根不是刻度上的长度）：
#   · `50%`      圆形。半径由盒子尺寸决定，不是刻度上的一档（状态点、清除叉
#                `.map-search__clear`、.hint 的问号圆底）。按值豁免而不是按选择器：
#                圆形是值的语义，按选择器就得给每个新画圆的组件补一条白名单
#                （2026-08-15 摘出那三条时还有一层理由 —— 按选择器会把当时尚未
#                提交的 `.map-search*` 类名写进本文件；组件已进 commit，这层不再
#                适用，但「圆形不是刻度」这层本身就够了）。
#   · `0` / `0px` 清零。把继承/vendor 给的圆角抹掉，同样不是取值。
_RADIUS_SHAPE_KEYWORDS = frozenset({'50%', '0', '0px'})

# 选择器级豁免，只有一条，写明理由。键是 (规范化选择器, 值)。
RADIUS_LITERAL_EXEMPTIONS = {
    ('html[lang="en"] .map-panel-triggers', '17px'):
        '17px 不是刻度上的一档，它就是 --radius-pill(999px) 在**中文**工具条'
        '（组宽 34px）上被浏览器夹出来的那段弧。英文工具条组宽 72px，同一条'
        '`--radius-pill` 变成 36px 的半圆端头，会啃掉首尾按钮的标签'
        '（「Zoom out」两端各需让出 11px、实际只有 8px，被 overflow: hidden '
        '裁掉尾字母，style.css 那里有实测记录）。钉死 17px = 与中文端头同一段弧，'
        '下面 :first-child / :last-child 那几条按 17px 算出的让位余量继续成立。'
        '⚠️ 这是**豁免**，与「没人管」是两件事：删掉本条目它立刻变红。',
}

# 长写也要覆盖：只认简写的话，换个属性名就能绕过去（同 _height_px 的理由）。
_RADIUS_PROPS = (
    'border-radius',
    'border-top-left-radius', 'border-top-right-radius',
    'border-bottom-left-radius', 'border-bottom-right-radius',
)


def test_no_border_radius_declaration_carries_a_bare_length():
    """全文 `border-radius` 家族不许出现裸长度（形状关键字与登记过的豁免除外）。

    与字号 / 字重 / 层栈那三条 blanket 断言同一个形态、同一个理由：per-selector
    的清单只管清单内，刻度的**边界**要靠一条「清单外一律不许」来守。
    """
    offenders = []
    for prop in _RADIUS_PROPS:
        for sel, val in _decls(_css(), prop):
            clean = re.sub(r'!\s*important', '', val, flags=re.I)
            for part in re.split(r'[\s/]+', clean.strip()):
                if not part or part.startswith('var('):
                    continue
                if part in _RADIUS_SHAPE_KEYWORDS:
                    continue
                if (sel, part) in RADIUS_LITERAL_EXEMPTIONS:
                    continue
                offenders.append(f'{sel} {prop}: {part}（整条 {val!r}）')
    assert not offenders, (
        '这些圆角还是裸长度：\n' + '\n'.join('  ' + o for o in offenders)
        + '\n四级刻度在 :root；确实不该上刻度的，往 RADIUS_LITERAL_EXEMPTIONS '
          '加一条并写明理由'
    )


# --------------------------------------------------------------------------
# 2. 控件高度：两级 + 同行同高
# --------------------------------------------------------------------------

# 密集档 28px 由 tests/test_css_contract.py::
# test_control_density_tokens_are_self_consistent 的算术等式钉住，这里不重复。
# 主操作档 36px = 28 + 一级间距刻度（--space-2 8px）。
CTL_H_LG_PX = 36.0

# 工具条按钮的固定高度。**不在间距刻度上，也不在控件两级上** —— 它是
# 「图标 + 标签两层信息」这个组件自己的尺寸（20px 图标 + 8px 间距 + 14px 标签
# = 42px 内容，上下各 8px 让位给胶囊端头那段 17px 深的圆弧）。
# 组件专有尺寸单独命名，正是为了不让它假装是刻度的一部分。
MAP_PANEL_BTN_H_PX = 58.0

# 「同一行上的控件必须同高」的实测违例（2026-08-14 量的，第三处是 2026-08-15）：
#   .bounds-edit-input      20px  点击编辑 / 手动四至面板的输入框
#   .bounds-actions .btn    36px  手动四至面板的确定/取消，坐在 20px 输入框旁边
#   .map-search__input      23px  地图搜索胶囊里的裸 input —— 宿主
#                                 `.map-search__field` 是 --ctl-h(28px) 的胶囊、
#                                 上下无内边距，裸 input 只按内容撑到 23px，
#                                 差出来的 5px 是「看着是输入框、点上去不聚焦」
#                                 的死区（上下各 2.5px）。这一条 2026-08-15 曾因
#                                 组件未提交摘出、同日随组件进 commit 后放回。
# 统一到 --ctl-h。属性名不限 height / min-height：两种都是「高度从哪来」，
# 只认一种的话换个属性就能绕过去。
DENSE_CONTROL_HEIGHT_SITES = (
    '.bounds-edit-input',
    ('.btn.btn-compact, .btn.tile-server-verify, .btn.concurrency-recommend, '
     '.btn.path-browse, .bounds-actions .btn'),
    '.map-search__input',
)

# `--ctl-h-lg` 的消费者：常驻底条上的主操作按钮。
# 2026-08-15 Task 5 之前只有配置页那一对（templates/_config_content.html 的
# #configResetBtn 与那颗 submit）；现在多了 #createTaskBtn —— 两个 Bootstrap
# 参数弹窗合并成非模态的 #createPanel 之后，提交钮从 `.modal-body` 挪进了面板的
# `.config-footer`（靠 form="taskForm" 关联表单，结构上在滚动区之外），
# 吃的是同一条 `.config-footer .btn { min-height: var(--ctl-h-lg) }`。
#
# 两条祖先链，因为 `.config-footer` 现在有两处宿主，链的上半截不同：
#   独立 /config 页面：.page-content.page-content--fill > .config-layout > .config-footer
#   新建任务面板：    .workbench-panel > .workbench-panel__body.workbench-panel__body--fill
#                     > .config-layout > .config-footer
# 祖先链一律照抄模板，不写「够用就行」的最小集（只放 config-footer 也能过）：
# 写最小集的话，按钮哪天被挪出底条，这里的模型不会有任何反应。
# 同一对链在 tests/test_button_geometry.py 里也有一份，两边管的事不同 ——
# 那边管「每颗按钮落在两档的哪一档」，这边管「--ctl-h-lg 这一档真的渲染得出来」。
CONFIG_FOOTER_ANCESTORS = frozenset({
    'page-content', 'page-content--fill', 'config-layout', 'config-footer',
})
PANEL_FOOTER_ANCESTORS = frozenset({
    'workbench-panel', 'workbench-panel__body', 'workbench-panel__body--fill',
    'config-layout', 'config-footer',
})
CONFIG_FOOTER_BTNS = (
    ('configResetBtn', frozenset({'btn', 'btn-secondary', 'btn-sm'}),
     CONFIG_FOOTER_ANCESTORS),
    (None, frozenset({'btn', 'btn-primary', 'btn-sm'}), CONFIG_FOOTER_ANCESTORS),
    # 浏览器实测（Task 5 的 E2E）：1366×768 下 rect 高 36px、bottom 756；
    # 1600×900 下 bottom 888 —— 与 --ctl-h-lg(36px) 逐像素一致。
    ('createTaskBtn', frozenset({'btn', 'btn-primary', 'w-100'}),
     PANEL_FOOTER_ANCESTORS),
)


def _height_px(css, selector):
    """规则里 height / min-height 声明出的高度 -> px。同上，三态返回。"""
    for sel, body in _rules(css):
        if _norm_selector(sel) != selector:
            continue
        decls = _decl_map(body)
        for prop in ('height', 'min-height'):
            if prop in decls:
                px = _resolve_length_px(css, decls[prop])
                return ('ok', px) if px is not None else ('unparsed', decls[prop])
    return ('missing', None)


def test_control_height_scale_has_exactly_two_steps():
    """`--ctl-h`(28px) 与 `--ctl-h-lg`(36px)，且 lg 真的**渲染得出来**。

    ⚠️ 2026-08-15 二次收紧。本条原来的后半段是
    `assert 'var(--ctl-h-lg)' in stripped` —— 一个**文本级**检查，它绿着，而
    令牌一个像素也没渲染出来：唯一的消费者
    `.config-footer .btn { min-height: var(--ctl-h-lg) }` 当时的自然高度是
    39px（8+8 内边距 + 21 行高 + 1x2 边框，`.btn-sm` 给的 14px 字），
    36px 的地板托不起任何东西 —— CSSOM 实测两颗都是 39px，
    **把那行 min-height 整条删掉仍然是 39/39**。
    「铸了不用比字面量更坏」这条家规就这样被一个 substring 绕过去了，而它正是
    tests/test_spacing_scale.py::test_every_scale_step_is_actually_used
    立规矩要防的失效模式。

    现在算的是**层叠之后的最终外框高度**，用按钮模型自己的
    `_effective_button_height`（#createTaskBtn 折叠线、#outputPathBrowse 走的
    是同一套模型、同一个口径 —— 两边对 `var()` 链 / 简写 / `!important` 的处理
    必须同源，各写一套就会出现「一边说 36、一边说 39，两边都绿」）。
    要求**恰好等于** --ctl-h-lg：大于说明自然高度已经越过地板、`min-height`
    是一条死声明；小于在 `max()` 语义下不可能，出现了就是模型坏了。
    """
    css = _css()
    lg = _resolve_length_px(css, _custom_property_raw(css, '--ctl-h-lg'))
    assert lg == CTL_H_LG_PX, f'--ctl-h-lg = {lg}，期望 {CTL_H_LG_PX}'
    problems = []
    for el_id, classes, ancestors in CONFIG_FOOTER_BTNS:
        label = ('.config-footer ' + '.'.join(sorted(classes))
                 + (f'#{el_id}' if el_id else ''))
        ctx = _BtnCtx(set(classes), ancestors,
                      element_id=el_id, label=label)
        got = _effective_button_height(css, ctx)
        if got != lg:
            why = ('—— 自然高度已经越过地板，min-height 是一条死声明'
                   if got > lg else '—— 比地板还低，高度模型坏了')
            problems.append(f'{label}: 层叠后外框高 {got}px != --ctl-h-lg({lg}px) {why}')
    assert not problems, (
        '--ctl-h-lg 这一档没有任何东西真的按它渲染（控件高度实际只有一级）：\n'
        + '\n'.join('  ' + p for p in problems)
    )


def test_same_row_controls_share_the_dense_height():
    """同一行上的密集控件都从 `--ctl-h` 取高度。"""
    css = _css()
    ctl_h = _resolve_length_px(css, _custom_property_raw(css, '--ctl-h'))
    problems = []
    for sel in DENSE_CONTROL_HEIGHT_SITES:
        state, px = _height_px(css, sel)
        if state == 'missing':
            problems.append(f'{sel}: 既没有 height 也没有 min-height')
        elif state == 'unparsed':
            problems.append(f'{sel}: 高度是 {px!r}，解析不出 px')
        elif px != ctl_h:
            problems.append(f'{sel}: 解析成 {px}px，期望 --ctl-h({ctl_h}px)')
    assert not problems, (
        '同行控件高度不齐：\n' + '\n'.join('  ' + p for p in problems)
    )


def test_map_toolbar_buttons_have_one_fixed_height():
    """`.map-panel-btn` 只有**一个**高度来源，值是 `--map-panel-btn-h`。

    改前是四条：基规则 54px、`:first-child` 58px、`:last-child` 60px、
    `:first-child:last-child` 64px。四个数不是审美，是在用高度给胶囊端头那段
    17px 深的圆弧腾让位 —— 但「让位」的正确工具是内边距 / 对齐，用高度做会
    让同一排按钮高低不齐，而且每加一种分组形态就要多算一个数。

    改后：一个高度 + `justify-content: center`，让位由居中自动给到上下两端。
    """
    css = _css()
    token = _resolve_length_px(css, _custom_property_raw(css, '--map-panel-btn-h'))
    assert token == MAP_PANEL_BTN_H_PX, (
        f'--map-panel-btn-h = {token}，期望 {MAP_PANEL_BTN_H_PX}'
    )
    state, px = _height_px(css, '.map-panel-btn')
    assert state == 'ok' and px == MAP_PANEL_BTN_H_PX, (
        f'.map-panel-btn 的高度是 {state}/{px}，期望 var(--map-panel-btn-h)'
    )
    extra = [
        _norm_selector(sel)
        for sel, body in _rules(css)
        if _norm_selector(sel).startswith('.map-panel-btn')
        and _norm_selector(sel) != '.map-panel-btn'
        and ('height' in _decl_map(body) or 'min-height' in _decl_map(body))
    ]
    assert not extra, (
        f'.map-panel-btn 又有了第二个高度来源：{extra} —— 工具条按钮高低不齐'
    )


def test_bounds_edit_input_does_not_kill_the_focus_ring():
    """`.bounds-edit-input` 的**基态**不许有无替代的 `outline: none`。

    这是全仓唯一一处压掉全局焦点环的地方，而且压得比全局强：
    `.bounds-edit-input`(0,1,0) > `*:focus-visible`(0,0,0)，所以键盘用户进到
    四至输入框之后屏幕上一条焦点线都没有。基态压掉 + 无替代 = 纯 a11y 缺陷。

    放行两种写法：删掉，或者同一规则块里给出 `outline` / `box-shadow` 替代。
    """
    css = _css()
    seen = False
    for sel, body in _rules(css):
        if _norm_selector(sel) != '.bounds-edit-input':
            continue
        seen = True
        decls = _decl_map(body)
        outline = (decls.get('outline') or '').strip().lower()
        if outline in ('none', '0'):
            assert 'box-shadow' in decls, (
                '.bounds-edit-input 基态仍然是 `outline: none` 且没有任何替代 —— '
                '它比全局 `*:focus-visible` 特异度高，键盘焦点在这个输入框上不可见'
            )
    assert seen, '.bounds-edit-input 规则不见了 —— 本断言已失效（不是通过）'


# --------------------------------------------------------------------------
# 3. 字号：裸字面量清零
# --------------------------------------------------------------------------

FONT_SIZE_TOKENS = (
    '--font-size-xs', '--font-size-sm', '--font-size-base',
    '--font-size-md', '--font-size-lg', '--font-size-xl',
)


def _decls(css, prop):
    """[(选择器, 值), ...]，注释已剥。`prop` 取长写属性名。"""
    stripped = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    out = []
    for sel, body in _rules(stripped):
        decls = _decl_map(body)
        if prop in decls:
            out.append((_norm_selector(sel), decls[prop]))
    return out


def test_no_font_size_declaration_carries_a_bare_literal():
    """全文 `font-size` 声明不许出现裸 px/rem。

    改前有五处，每一处都是「这个位置我觉得该细一点 / 大一点」的一次性判断：
      .workbench-statusbar 13px / .tint-stop-label 10px /
      .detail-terrain-info 0.9rem / .app-toast__close 1.15rem /
      .splash-wordmark 1.35rem
    五个数没有一个落在六级刻度上，也就是说全站实际有 11 档字号。
    """
    bare = [(sel, val) for sel, val in _decls(_css(), 'font-size')
            if re.search(r'\d\s*(px|rem|em)\b', val)]
    assert not bare, (
        '这些 font-size 还是裸字面量：\n'
        + '\n'.join(f'  {s}: {v}' for s, v in bare)
        + '\n六级刻度在 :root，映到最近的一级'
    )


def test_every_font_size_step_is_actually_used():
    """六级字号每级都至少被一处 `var()` 消费。

    `--font-size-xl` 是这一条真正的对象：改前它**零引用** —— 铸了一级最大号
    字，全站没有任何一处按它渲染。splash 的 wordmark（改前 1.35rem）是它的
    自然消费者。
    """
    css = re.sub(r'/\*.*?\*/', '', _css(), flags=re.S)
    unused = [t for t in FONT_SIZE_TOKENS if f'var({t})' not in css]
    assert not unused, (
        f'这些字号级铸了没人用：{unused} —— 令牌看着像家规，实际没有任何东西按它渲染'
    )


# --------------------------------------------------------------------------
# 4. 字重：三级
# --------------------------------------------------------------------------

WEIGHT_TOKENS = {
    '--weight-normal': '400',
    '--weight-medium': '500',
    '--weight-strong': '600',
}


def test_weight_tokens_exist_and_are_all_used():
    """三级字重都在 `:root`，值是 400/500/600，且每级都被消费。

    只有三级是刻意的：改前全文用出 400/500/600/700 四档，而 700 那一档的四处
    （`.progress__label` / `.modal-title` / `.page-item.active .page-link` /
    `.bounds-k`）没有一处需要比 600 更重 —— 700 在 15px 的界面文字上只是更黑，
    不带来更多层级信息。fonts.css 仍然覆盖 400/500/600/700 四个字面
    （test_css_contract.py 那条断言不动），只是 style.css 不再点用 700。
    """
    css = _css()
    got = {name: (_custom_property_raw(css, name) or '').strip()
           for name in WEIGHT_TOKENS}
    assert got == WEIGHT_TOKENS, f'字重令牌是 {got}，期望 {WEIGHT_TOKENS}'
    stripped = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    unused = [t for t in WEIGHT_TOKENS if f'var({t})' not in stripped]
    assert not unused, f'这些字重级铸了没人用：{unused}'


def test_no_font_weight_declaration_carries_a_bare_integer():
    """全文 `font-weight` 声明不许出现裸整数。

    这一条守的不是「数字难看」，是**同一个东西三个字重**：改前 `.btn` 是 500、
    `.btn-primary` 是 600、`.btn-compact` 是 400 —— 三条规则打在同一排按钮上，
    谁重谁轻取决于它带的是哪个变体类。
    """
    bare = [(sel, val) for sel, val in _decls(_css(), 'font-weight')
            if re.fullmatch(r'\d+', val.strip())]
    assert not bare, (
        '这些 font-weight 还是裸整数：\n'
        + '\n'.join(f'  {s}: {v}' for s, v in bare)
    )


# --------------------------------------------------------------------------
# 5. 层栈：令牌化，**值照抄**
# --------------------------------------------------------------------------

# ⚠️ 全部照抄 2026-08-15 改前实测值，本次**不重排层序**。
# 出处：改前的 style.css 用 `_rules` 扫一遍 z-index 声明 —— 18 处声明、
# 14 个不同的值。
#
# ⚠️ plan 写的是「九个 --z-* 令牌」，**这个数字是错的**（它是 2026-08-14 量的）。
# 按角色命名后 style.css 里是 15 个令牌，本清单登记全部 15 个 —— `--z-map-search`
# 2026-08-15 曾因组件未提交摘出、同日随组件进 commit 后放回（见文件开头那段）。
# 清单内 `--z-modal` 与 `--z-map-preview-zoom` 同值 1500 —— 这是现状，照抄不
# 合并：合并等于宣布「缩略图 hover 放大」与「弹窗」是同一层，那是层序决策，
# Task 3 不做。14 个不同的值全部有引用，没有孤儿层。
Z_LADDER = {
    # 组件内叠层（宿主自己不建层，这几个数只在局部有意义）
    '--z-inline': '1',                 # .progress__label 盖在轨道上
    '--z-inline-handle': '2',          # .workbench-panel__resizer 拖拽把手
    '--z-tooltip': '20',               # .hint::after 气泡
    # 地图层
    '--z-map-search': '500',           # .map-search 搜索胶囊（在瓦片之上、薄雾之下）
    '--z-map-veil': '900',             # 必须 < --z-map-overlay，见下
    '--z-map-overlay': '1000',         # 状态栏 / 工具条 / 浮层信息条 / 等高线预览
    '--z-map-preview-zoom': '1500',    # 缩略图 hover 放大
    # 面板 / 弹窗
    '--z-panel': '1401',
    '--z-modal-backdrop': '1450',
    '--z-modal': '1500',
    # 全屏覆盖物
    '--z-splash': '3000',
    '--z-toast': '11000',
    '--z-confirm': '12000',
    '--z-drop-veil': '13000',
    '--z-cmdk': '13100',
}


def test_z_ladder_tokens_copy_the_measured_values():
    """15 个 `--z-*` 令牌的值与改前实测逐一相等。

    本条的作用是「令牌化不等于重排」：把 18 处裸整数换成 `var()` 时，最容易
    发生的事故是顺手把两个相邻的数合并、或者把某一层挪到别人上面 —— 而层序
    错了不会报错，只会表现为「某个弹窗被遮罩盖住、点不动」（2026-08 遮罩事故
    就是这个形态，见 tests/test_records_panel_structure.py 的 docstring）。
    """
    css = _css()
    got = {name: (_custom_property_raw(css, name) or '').strip()
           for name in Z_LADDER}
    assert got == Z_LADDER, (
        '层栈令牌的值与改前实测不一致（本次不许重排层序）：\n'
        + '\n'.join(f'  {k}: {got[k]!r} != {v!r}'
                    for k, v in Z_LADDER.items() if got[k] != v)
    )


def test_map_veil_still_sits_below_the_toolbar():
    """薄雾必须在工具条之下 —— 令牌化之后这条关系仍然成立。

    与 tests/test_fix_terrain_preview_transition.py 那条同源（那边算的是
    style.css 里两条规则最终引用到的值，这边算的是令牌本身），两条都留着：
    一条守「层序关系」，一条守「规则真的引用了对的令牌」。
    """
    css = _css()
    veil = _resolve_z_index(css, f'var({"--z-map-veil"})')
    over = _resolve_z_index(css, f'var({"--z-map-overlay"})')
    assert veil is not None and over is not None, (
        '层栈令牌解析不出整数 —— 本断言已失效（不是通过）'
    )
    assert veil < over, '薄雾的层号不再低于工具条 —— 换地形时那层雾会盖住工具条'


def test_no_z_index_declaration_carries_a_bare_integer():
    """全文 `z-index` 声明不许出现裸整数（`0` / `-1` 除外）。

    `0` / `-1` 放行的理由：它们不是「层栈上的一级」，而是「不参与提升」与
    「压到内容之下」两个关键字式的用法，给它们起名字只会让层栈多两个假成员。
    """
    bare = [(sel, val) for sel, val in _decls(_css(), 'z-index')
            if re.fullmatch(r'-?\d+', val.strip()) and val.strip() not in ('0', '-1')]
    assert not bare, (
        '这些 z-index 还是裸整数：\n'
        + '\n'.join(f'  {s}: {v}' for s, v in bare)
        + '\n层栈令牌在 :root，按角色取用'
    )


def test_every_z_ladder_token_is_actually_referenced():
    """清单里那 15 个层栈令牌每个都真的被一条规则引用。

    反向堵「铸了不用」：层栈里一个没人引用的层号，下一个人会以为那一层被占了，
    于是给自己的新覆盖物挑一个更大的数 —— 层栈就是这么一路涨到 13100 的。
    """
    css = re.sub(r'/\*.*?\*/', '', _css(), flags=re.S)
    unused = [t for t in Z_LADDER if f'var({t})' not in css]
    assert not unused, f'这些层栈令牌铸了没人用：{unused}'


# --------------------------------------------------------------------------
# 6. 动效：三级时长 + 一个曲线
# --------------------------------------------------------------------------

MOTION_TOKENS = {
    '--dur-fast': '0.15s',
    '--dur-base': '0.2s',
    '--dur-slow': '0.45s',
    '--ease': 'ease',
}

# `transition` 的时长里唯一允许的字面量。0.01ms 是
# `@media (prefers-reduced-motion: reduce)` 那一块的「几乎立刻结束」写法，
# 它的语义正是「不走刻度」，令牌化它没有意义（见 test_css_contract.py 里
# test_reduced_motion_actually_stops_every_animated_element 的登记）。
REDUCED_MOTION_LITERAL = '0.01ms'


def _split_top_level_commas(value):
    """按顶层逗号拆；`cubic-bezier(0.4, 0, 0.2, 1)` 里的逗号不算。"""
    out, depth, cur = [], 0, ''
    for ch in value:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            out.append(cur)
            cur = ''
        else:
            cur += ch
    out.append(cur)
    return [p for p in out if p.strip()]


def _tokens_outside_parens(part):
    """按空白拆 token，括号内当整体（`var(--dur-fast)` 不被拆散）。"""
    out, depth, cur = [], 0, ''
    for ch in part:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch.isspace() and depth == 0:
            if cur:
                out.append(cur)
            cur = ''
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def test_motion_tokens_exist_and_are_all_used():
    """三级时长 + `--ease` 都在 `:root`，且每个都被消费。"""
    css = _css()
    got = {name: (_custom_property_raw(css, name) or '').strip()
           for name in MOTION_TOKENS}
    assert got == MOTION_TOKENS, f'动效令牌是 {got}，期望 {MOTION_TOKENS}'
    stripped = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    unused = [t for t in MOTION_TOKENS if f'var({t})' not in stripped]
    assert not unused, f'这些动效令牌铸了没人用：{unused}'


def test_transition_durations_only_come_from_the_motion_scale():
    """`transition` / `transition-duration` 的时长只许是三级令牌之一。

    改前全文除了 0.15s / 0.2s 两个主档，还散着六个孤儿时长
    （0.12 / 0.14 / 0.18 / 0.22 / 0.25 / 0.35s），另有三条「条子在长」的过渡
    各写一个数（0.12 / 0.2 / 0.3s）—— 同一个动作三种速度。
    孤儿时长的成本不是不好看：调整全站节奏时要逐处去找，必然漏。

    `animation` 的时长**不在**本条范围内：那是关键帧动画自己的节奏
    （splashGridDrift 12s、hint-spin 0.9s、pulse 2s），与「状态切换要多久」
    不是同一个量，硬塞进三级刻度只会让 12s 的背景漂移变成 0.45s 的抽搐。
    """
    css = _css()
    allowed = {f'var({t})' for t in ('--dur-fast', '--dur-base', '--dur-slow')}
    allowed.add(REDUCED_MOTION_LITERAL)
    offenders = []
    for prop in ('transition', 'transition-duration'):
        for sel, val in _decls(css, prop):
            for part in _split_top_level_commas(val):
                for tok in _tokens_outside_parens(part):
                    tok = tok.strip().rstrip('!important').strip()
                    if not tok:
                        continue
                    is_time = bool(re.fullmatch(r'[\d.]+m?s', tok))
                    is_dur_var = tok.startswith('var(--dur')
                    if (is_time or is_dur_var) and tok not in allowed:
                        offenders.append(f'{sel} {{ {prop}: {val} }} -> {tok}')
    assert not offenders, (
        '这些过渡时长不在三级刻度上：\n' + '\n'.join('  ' + o for o in offenders)
    )


# ---------------------------------------------------------------------------
# 7. 刻度闸门的后门：写在 JS 里的内联样式
#
# 本文件与 tests/test_spacing_scale.py 的全部 blanket 断言都只读
# static/css/style.css。一句 `style="margin: 0 6px 6px 0;"` 写在 JS 的模板
# 字符串里，那几个 px 一个都不会被扫到 —— 等高线预览面板（地图左下角那块
# 「已完成的等高线瓦片」）就是这样带着 6px 和一个 Bootstrap `.alert.alert-info`
# 底座活过了整轮刻度归一（59459b1）：它的样式**不在样式表里**，所以刻度、
# 圆角、按钮几何、玻璃降级四道闸门一条都没红，而它在屏幕上与另外两个玻璃浮层
# 明显不是一套东西。2026-08-15 定向复审时由人眼发现，本条是它的机器闸门。
#
# 门槛是 0，不设白名单：实测把那三处（预览面板 + 任务列表的两处）搬进样式表
# 之后，static/js 下带长度的内联样式命中数就是 0。
# ---------------------------------------------------------------------------

_JS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'static', 'js')

#: 长度：`px` / `rem` / `em`。前置断言排掉 `12.5rem` 里的小数点与
#: `--space-1rem` 这类标识符尾巴（本仓没有，但判据不能靠「碰巧没有」成立）。
_INLINE_LENGTH = re.compile(r'(?<![\w.-])\d+(?:\.\d+)?(?:px|rem|em)\b')

#: `style="…"` / `style='…'`
_INLINE_STYLE_ATTR = re.compile(r"""style\s*=\s*(?P<q>["'])(?P<val>[^"']*)(?P=q)""")

#: `el.style.marginTop = '12px'` 与 `el.style.setProperty('gap', '12px')`
_STYLE_PROP = re.compile(r'\.style\.[A-Za-z]\w*\s*=\s*(?P<val>[^;\n]+)')
_STYLE_SET_PROPERTY = re.compile(
    r"""\.style\.setProperty\(\s*['"][^'"]+['"]\s*,\s*(?P<val>[^)]+)\)""")


def test_no_inline_style_in_js_carries_a_length():
    """`static/js` 里不许再出现带长度的内联样式 —— 刻度闸门的唯一后门。

    ⚠️ 2026-08-15 同日收紧：本条落地时曾**刻意排除** Cesium 实体
    `description:` 那段模板，理由是那段 HTML 活在 InfoBox 的 iframe 里（本站
    样式表与令牌都不跨 document，内联样式是那里唯一能生效的手段，拿刻度去要求
    它是拿错尺子）。当天 history.js 的那段 description 改成「在 JS 里把令牌解析
    成值再拼」之后，它里面已经一个字面量都没有（值全是 `${…}` 插值），排除就
    只剩下「给未来的写死值留个口子」这一个作用 —— 删掉。那段 HTML 现在由两道
    闸门一起罩：本条管长度字面量，
    tests/test_tasks_js_contract.py::test_cesium_infobox_description_carries_resolved_tokens
    管「值必须是运行期解析出来的令牌」（连写死颜色一起拦）。
    """
    offenders = []
    for name in sorted(os.listdir(_JS_DIR)):
        if not name.endswith('.js'):
            continue
        with open(os.path.join(_JS_DIR, name), encoding='utf-8') as f:
            src = f.read()
        for rx in (_INLINE_STYLE_ATTR, _STYLE_PROP, _STYLE_SET_PROPERTY):
            for m in rx.finditer(src):
                hit = _INLINE_LENGTH.search(m.group('val'))
                if not hit:
                    continue
                line = src[:m.start()].count('\n') + 1
                offenders.append(
                    f'{name}:{line} {m.group(0).strip()[:80]} -> {hit.group(0)}')
    assert not offenders, (
        '这些内联样式带着长度字面量，而它们在 static/css/style.css 之外 —— '
        '本文件与 test_spacing_scale.py 的刻度闸门看不到它们：\n'
        + '\n'.join('  ' + o for o in offenders)
        + '\n搬进样式表并改用 --space-* / --ctl-h / --radius-* 令牌。'
    )
