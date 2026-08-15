"""间距刻度契约（设计稿 §3.1 / plan 2026-08-14 Task 2）。

为什么单独一份文件而不是塞进 tests/test_css_contract.py：那份文件是色彩 /
按钮 / 动画三套层叠模型的载体，本文件钉的是**几何**，两者的失效模式没有交集。
分开之后「间距刻度红了」不会淹没在 86 个节点里。

本文件守四件事：
  1. 七级刻度都在 `:root`，且都是**扁平 px 字面量**；
  2. `--pad-card` / `--gap-field` 两个语义别名指向刻度（而不是各写一个数）；
  3. 全文 padding / margin / gap 声明里不许再有裸长度（白名单见下）；
  4. 七级刻度每一级都真的被 `var()` 消费（不许铸了不用）。

没有 sys.path.insert：tests/conftest.py 已经把仓库根放进 sys.path。
"""

import os
import re

CSS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'static', 'css', 'style.css',
)


# 设计稿 §3.1 定下的七级。**值必须是扁平 px 字面量**，理由是承重的：
# tests/test_css_contract.py 的 `_resolve_length_px` 只认 `12px` / `0.75rem` /
# 单层 `var(--x)`，遇到 `calc()` 或带回退的 `var(--x, 8px)` 一律返回 None，
# 而它的十来个调用方（控件高度模型、按钮高度模型、`#createTaskBtn` 的折叠线
# 模型、`.bounds-grid` 列间距上界……）拿到 None 之后报的是「本测试已失效」——
# 那不是通过。写成 `calc(var(--space-unit) * 3)` 会把那些断言集体变成失效状态。
SCALE = {
    '--space-hair': '2px',
    '--space-1': '4px',
    '--space-2': '8px',
    '--space-3': '12px',
    '--space-4': '16px',
    '--space-5': '24px',
    '--space-6': '32px',
}

# 语义别名 -> 它必须指向的刻度级。保留别名而不是全站直接写 `var(--space-3)`：
# test_css_contract.py 有两条断言只认这两个名字 —— `--gap-field` 的 8px 上界
# （FIELD_GAP_MAX_PX）与「`.mb-3` 的 margin-bottom 必须字面包含
# `var(--gap-field)`」。所以这里断言的是**原始声明文本**，别名的间接层本身
# 就是被测对象。
ALIASES = {
    '--pad-card': 'var(--space-3)',
    '--gap-field': 'var(--space-2)',
}

# 控件密度令牌：本刻度**不管**控件内部。它们参与
# `2 * --ctl-pad-y + --ctl-line-h + 2 * 边框 = --ctl-h`（3/20/1/28）这条算术
# 契约（test_css_contract.py::test_control_density_tokens_are_self_consistent）。
# 把 `--ctl-pad-y: 3px` 迁到 `--space-1`(4px) 会让等式变成 30 != 28。
# 这里正向钉住「控件内边距仍然走密度令牌」，免得有人顺手把它们也刻度化。
CTL_PADDING_SITES = (
    ('.form-control, .form-select', 'padding-top', '--ctl-pad-y'),
    ('.form-control, .form-select', 'padding-bottom', '--ctl-pad-y'),
    ('.form-control, .form-select', 'padding-left', '--ctl-pad-x'),
    ('.form-control, .form-select', 'padding-right', '--ctl-pad-x'),
    ('.form-control::file-selector-button', 'padding', '--ctl-pad-'),
)

# 主断言的白名单。**只有三类半**，每条都写明理由；键是 (选择器原文, 属性)。
#
# 一类：`0` 与 `0 auto` 这种纯关键字/零值 —— 不在下面列举，扫描器本身就只挑
#       带 px/rem 单位的分量（`padding: 0 12px` 仍然会因为 12px 被抓）。
# 二类：控件密度令牌那一组 —— 见 CTL_PADDING_SITES，它们已经是 var()，
#       扫描器抓不到，无需豁免。
# 三类：组件专有的固定尺寸，本质不是「容器与组之间的间距」，刻度管不着。
# 四类（**计划里没有，是读 test_css_contract.py 读出来的**）：值被那边只认
#       字面量的解析器**直读**的声明。`_length_to_px` / `_px` 都不跟 var()。
#
# ⚠️ 2026-08-15 更正：四类原先写的理由是「换成 var() 不会让那些断言变红，而是
#    让它们报『本断言已失效』—— 静默失去覆盖」。**「静默」是错的。**
#    逐条把这 6 处（5 条四类 + `.form-select`）换成 var(--space-N) 实跑过一遍，
#    6/6 都是**响亮的红**，且失败消息就点在被换掉的那个值上，例如：
#      .workbench-panel__body padding -> var(--space-3)
#          test_config_footer_is_a_real_bottom_bar_inside_the_panel
#          AssertionError: .workbench-panel__body 的 padding 读不出来
#          （'var(--space-3)'）—— 本断言已失效
#      .form-select padding-right -> var(--space-6)
#          test_form_select_reserves_room_for_the_arrow
#          AssertionError: 胜出的 `.form-select` 右内边距是 'var(--space-6)'，
#          不是 px/rem 字面量，解析不了      （四个上下文各报一条）
#    「本断言已失效」在这个仓库里就是 assert 失败，从来不是 pass。
#
#    所以留字面量的真实理由不是「怕静默」，而是：迁过去之后那条断言**不再检查
#    它本来要检查的东西**。它从「底栏是否真的贴着面板三条边 / 箭头有没有让位 /
#    色块看不看得见 / chip 装不装得下」退化成「我解析不了这个值」——
#    换到的是一个红的 CI 和一个失去守卫的不变量，两头都亏。
#    这几处保留字面量 = 保住那几条断言干活的能力。
#
#    这 6 条一共只喂 4 条断言（原报告写的「5 条对 4 条」也不对）：
#      test_config_footer_is_a_real_bottom_bar_inside_the_panel  <- 3 条
#          (.workbench-panel__body padding / --fill .config-footer margin /
#           .config-footer padding)
#      test_color_picker_swatch_is_big_enough_to_see             <- 1 条
#      test_every_progress_height_fits_the_label                 <- 1 条
#      test_form_select_reserves_room_for_the_arrow              <- 1 条（三类那条）
#    即：5 条四类 -> 3 条断言；连 `.form-select` 一起算才是 6 条 -> 4 条断言。
WHITELIST = {
    # 三类
    ('.tint-stop input[type="color"]', 'padding'):
        '1px 是色块内衬（发丝级），刻度地板是 2px：28px 方框里内衬每加 1px，'
        '色块少 2px。1px 属于边框刻度，不属于间距刻度',
    ('.cmdk__item kbd, .cmdk__help-row kbd', 'padding'):
        '上下 1px 是键帽内衬（发丝级，字形已占满行高），同上不属于间距刻度；'
        '左右已迁入刻度',
    ('.app-toast__icon', 'margin-top'):
        '1px 是图标与文字的光学基线微调，不是间距（改成 2px 图标就低于文字中线）',
    ('.bounds-sr', 'margin'):
        '-1px 是屏读专用隐藏配方的一部分（1px 见方 + margin:-1px + '
        'clip-path: inset(50%)），抵的是自己那 1px 尺寸，不是间距',
    ('.form-select', 'padding-right'):
        '36px 是下拉箭头的让位空间（箭头占右起 12~28px），不在刻度上，'
        '下限 28px 由 test_css_contract.py::test_form_select_reserves_room_for_the_arrow '
        '钉住；那条断言还用只认字面量的 `_length_to_px` 直读它（四类）',
    # 四类
    ('.form-control-color', 'padding'):
        '色块宽度模型（test_color_picker_swatch_is_big_enough_to_see）用 '
        '`_length_to_px` 直读胜出的 padding-left/right，不跟 var()',
    ('.progress__label', 'padding'):
        'chip 高度模型（`_progress_label_chip_height_px`，'
        'test_every_progress_height_fits_the_label 的输入）用 `_length_to_px` '
        '直读 padding 的纵向分量，不跟 var()',
    ('.workbench-panel__body', 'padding'):
        'test_config_footer_is_a_real_bottom_bar_inside_the_panel 用只认字面量的 '
        '`_px()` 读它，再要求面板变体的负外边距恰为它的相反数',
    ('.workbench-panel__body--fill .config-footer', 'margin'):
        '同上：三值简写的左右/下两位必须是字面量，且等于宿主内边距的相反数 —— '
        '写成 calc(-1 * var(--space-3)) 会让那条断言报「已失效」',
    ('.config-footer', 'padding'):
        '同上：`base_pad[0]` 必须是 `_px()` 读得懂的字面量',
}

_SPACING_PROP = re.compile(
    r'^(?:padding|margin)(?:-(?:top|right|bottom|left|inline|block)'
    r'(?:-(?:start|end))?)?$|^(?:row-|column-)?gap$'
)

# 负号要一起吃掉：负外边距是间距决策的一部分（`.config-footer` 用 -12px 抵消
# 宿主内边距、`.bounds-v` 用 -2px 抵掉自己的内边距），漏掉它们等于允许一半的
# 间距继续散着写。
_LENGTH = re.compile(r'(?<![\w.])(-?\d*\.?\d+)(px|rem)\b')


def _css():
    with open(CSS_PATH, encoding='utf-8') as f:
        return f.read()


def _strip_comments_keep_lines(css):
    """剥注释但保留行号：失败消息要能直接跳到出问题的那一行。

    注释必须剥 —— 本仓库的 CSS 注释**逐字复述**被改掉的旧值
    （「A5 / Task 10：1rem 1.25rem(16/20px) -> 8px/--pad-card」），
    拿原文扫会把改动记录当成回潮。
    """
    return re.sub(
        r'/\*.*?\*/',
        lambda m: re.sub(r'[^\n]', ' ', m.group(0)),
        css,
        flags=re.S,
    )


def _spacing_declarations(css):
    """[(行号, 选择器原文, 属性, 值), ...]，只含 padding/margin/gap 家族。

    自己按花括号深度扫而不复用 test_css_contract.py 的 `_rules_ctx`：那个函数
    返回的是规则体整块，本断言要报的是**行号**（31 处散落全文，报不出行号的
    清单没法当工单用）。at-rule 层（`@media`）记进栈但不参与选择器匹配，
    取的是最内层那个普通选择器。
    """
    out = []
    stack = []
    token = ''
    line = 1
    for ch in _strip_comments_keep_lines(css):
        if ch == '{':
            stack.append(' '.join(token.split()))
            token = ''
        elif ch == '}':
            # 先 `_collect` 再清 token：CSS 允许块里最后一条声明不写分号，
            # `.probe { padding: 13px }` 是完全合法的写法。改前这里直接
            # `token = ''`，那条声明从没进过 `_collect` —— 实测
            # `_spacing_declarations('.probe { padding: 13px }')` 返回 `[]`，
            # 带分号的同一条返回 `[(1, '.probe', 'padding', '13px')]`。
            # 后果是主闸门 test_no_spacing_declaration_carries_a_bare_length
            # 和 test_whitelist_has_no_stale_entries 都**静默**看不见它（两条用
            # 的是同一个解析器），谁写了个不带分号的裸长度就白拿一张通行证。
            # pop 必须排在 _collect 之后：选择器是从 stack 顶上取的。
            # 看守：test_spacing_parser_reads_every_shape。
            _collect(out, line, stack, token)
            if stack:
                stack.pop()
            token = ''
        elif ch == ';':
            _collect(out, line, stack, token)
            token = ''
        else:
            token += ch
        if ch == '\n':
            line += 1
    return out


def _collect(out, line, stack, chunk):
    if not stack or ':' not in chunk:
        return
    selector = next((s for s in reversed(stack) if not s.startswith('@')), '')
    if not selector:
        return
    name, _, value = chunk.partition(':')
    name = ' '.join(name.split()).lower()
    if not _SPACING_PROP.match(name):
        return
    out.append((line, selector, name, ' '.join(value.split())))


# 解析器自检的输入。行号是**断言的一部分**（下面那条用例逐字写死），改动这段
# 就得同步改期望表 —— 这正是要的：解析器的输入不许无声漂移。
#   1  无尾分号（改前静默丢弃的那一种）
#   2  !important 原样留在值里
#   3-5 @media 嵌套 + 块内也无尾分号
#   6  逗号选择器（选择器原文整条留着，不拆）
#   7  大写属性名
#   8-11 跨行的值
#   12-15 注释里的伪声明
#   16 非间距属性 + 长得像但不是间距的属性名
_PARSER_PROBE_CSS = """\
.no-semi { padding: 13px }
.bang { margin: 4px !important; }
@media (min-width: 900px) {
    .nested-no-semi { gap: 8px }
}
.one, .two { padding-inline: 2px }
.upper { PADDING-TOP: 5px; }
.multi {
    margin: 1px
        2px;
}
.commented {
    /* padding: 999px; */
    row-gap: 3px;
}
.not-spacing { border-radius: 4px; padding-x: 9px }
"""


def test_spacing_parser_reads_every_shape():
    """`_spacing_declarations` 对合法 CSS 的各种写法都不许漏抓、也不许误抓。

    为什么单独立这一条：本文件三条主断言
    （test_no_spacing_declaration_carries_a_bare_length /
    test_whitelist_has_no_stale_entries / test_negative_margins_are_declared）
    **共用**这一个解析器，解析器漏一种写法 = 三条一起静默失明，而且症状是
    「全绿」。2026-08-15 实测过一次真的：`}` 分支不 `_collect` 就清 token，
    `.probe { padding: 13px }`（无尾分号，合法）返回 `[]`，主闸门抓不到；
    同一条加个分号就抓得到。那次漏检没有任何断言揭发得了它。

    期望表整表写死（含行号），不是「抓到几条」这种弱判据：只断言条数的话，
    把 `.upper` 的属性名忘了小写、或者把逗号选择器拆成两条，条数一个都不差。

    ⚠️ 行号语义是「声明**结束**的那一行」，不是 `属性:` 所在的那一行 ——
    `.multi` 那条跨两行，分号落在第 10 行，报的就是 10。31 处散落全文的清单
    要能跳过去，跳到结束行同样跳得到，所以这个语义是接受的，不是 bug；
    写在这里是免得下一个人以为它坏了顺手"修"成起始行。
    """
    got = _spacing_declarations(_PARSER_PROBE_CSS)
    assert got == [
        (1, '.no-semi', 'padding', '13px'),          # 无尾分号：改前这条整个丢失
        (2, '.bang', 'margin', '4px !important'),    # !important 留在值里，不剥
        (4, '.nested-no-semi', 'gap', '8px'),        # @media 记进栈但不当选择器
        (6, '.one, .two', 'padding-inline', '2px'),  # 逗号选择器不拆
        (7, '.upper', 'padding-top', '5px'),         # 属性名小写归一
        (10, '.multi', 'margin', '1px 2px'),         # 跨行值压成单空格
        (14, '.commented', 'row-gap', '3px'),        # 注释里的 padding 不算数
    ], f'解析器读出来的是 {got}'



def _px(number, unit):
    return float(number) * (16.0 if unit == 'rem' else 1.0)


_STEPS = sorted((float(v[:-2]), k) for k, v in SCALE.items())


def nearest_step(px):
    """离 px 最近的一级；正好落在两级中间时**取大的那一级**。

    向上取整不是审美：设计稿 §3.1 自己就是这么解 6px 这个平局的
    （6px 到 4px 与 8px 各 2px，表里写的是 `--space-2`）。同一个计算值必须
    落到同一级，否则 `6px` 与 `0.375rem` 会分家，迁移就不自洽了。
    """
    target = abs(px)
    best = min(_STEPS, key=lambda s: (abs(s[0] - target), -s[0]))
    return best[1], best[0]


def test_spacing_scale_tokens_are_flat_px_literals():
    """七级刻度都在 `:root`，值恰为设计稿定的数，且都是扁平 px 字面量。

    「扁平」是本条真正在守的东西。tests/test_css_contract.py 的
    `_resolve_length_px` 只认 px/rem 字面量与**单层** `var(--x)`；`calc()` 和
    `var(--x, 回退值)` 返回 None，它的十来个调用方拿到 None 之后报的是
    「本测试已失效（不是通过）」。也就是说把刻度写成
    `calc(var(--space-unit) * 3)` 不会让谁变红，只会静默抽掉控件高度、按钮
    高度、折叠线位置、`.bounds-grid` 列间距这几套模型的地板。
    """
    css = _strip_comments_keep_lines(_css())
    for name, expected in sorted(SCALE.items()):
        m = re.search(re.escape(name) + r'\s*:\s*([^;]+);', css)
        assert m, (
            f'{name} 没有定义。引用未定义的自定义属性**不报错**，只会让整条'
            '声明失效、静默退回 initial —— 表现为「改了没反应」'
        )
        raw = m.group(1).strip()
        assert raw == expected, f'{name} = {raw!r}，期望 {expected!r}（设计稿 §3.1）'
        assert re.fullmatch(r'\d+px', raw), (
            f'{name} = {raw!r} 不是扁平 px 字面量。`calc()` / `var(--x, 回退)` '
            '会让 test_css_contract.py 的 `_resolve_length_px` 返回 None，'
            '十来处调用方随之报「本测试已失效」'
        )


def test_semantic_aliases_point_at_the_scale():
    """`--pad-card` / `--gap-field` 必须是指向刻度的别名，而不是自己写一个数。

    断言原始文本而不是解析后的 px：这一层间接**本身**就是被测对象 ——
    别名回退成字面量的话，改刻度不会影响卡片内边距与字段间距，刻度就成了摆设。
    两个名字都还有下游断言认（FIELD_GAP_MAX_PX 的 8px 上界、以及
    「`.mb-3` 的 margin-bottom 必须字面包含 `var(--gap-field)`」），
    所以别名只能改指向、不能改名。
    """
    css = _strip_comments_keep_lines(_css())
    for name, expected in sorted(ALIASES.items()):
        m = re.search(re.escape(name) + r'\s*:\s*([^;]+);', css)
        assert m, f'{name} 没有定义 —— 两条下游断言（字段间距上界、.mb-3）会一起失效'
        raw = m.group(1).strip()
        assert raw == expected, (
            f'{name} = {raw!r}，期望恰为 {expected!r}。写死数字的话，'
            '改刻度不会影响它，令牌就成了摆设'
        )


def test_control_density_tokens_still_own_the_inside_of_controls():
    """控件内部的内边距仍然走 `--ctl-pad-*`，没有被间距刻度顺手吞掉。

    反向断言，堵的是一个很自然的手误：把「所有 padding 都换成 --space-N」
    执行到控件规则上。`--ctl-pad-y: 3px` 迁到 `--space-1`(4px) 会让
    `2*4 + 20 + 2*1 = 30 != 28 = --ctl-h`，那条算术契约立刻变红；更糟的是
    迁到 `--space-hair`(2px) 时等式变成 26 != 28，同样红，但顺手把 `--ctl-h`
    也改一下就「绿」了 —— 而 28px 是实测选的密度（QGIS / VS Code 区间）。
    刻度只管容器与组之间的间距。
    """
    decls = _spacing_declarations(_css())
    for selector, prop, token in CTL_PADDING_SITES:
        got = [v for _l, s, p, v in decls if s == selector and p == prop]
        assert got, f'`{selector}` 不再声明 {prop} —— 本断言已失效（选择器改名了？）'
        assert all(token in v for v in got), (
            f'`{selector}` 的 {prop} = {got!r}，不再引用 {token} —— '
            '控件内部密度被间距刻度吞掉了。那一组参与 '
            '2*--ctl-pad-y + --ctl-line-h + 2*边框 = --ctl-h 的算术契约'
        )


def test_no_spacing_declaration_carries_a_bare_length():
    """padding / margin / gap 家族的声明里不许再出现裸 px/rem 长度。

    这是本文件的主断言，也是「31 个离散字面量」这个缺陷的守门人：改前全文
    padding/margin/gap 用出 12 个不同的 px 值加 21 个不同的 rem 值，同一种
    「组间距」在不同组件里是 6/8/10/12/14px 五种写法。散着写的代价不是丑，
    是**没人能改**：调密度要逐处试，漏一处就在同一屏里出现两种间距。

    白名单只有上面 WHITELIST 那几条，每条写明理由（发丝级内衬、组件专有
    固定尺寸、以及值被 test_css_contract.py 只认字面量的解析器直读的那几处）。
    失败消息逐条给出「行号 / 声明 / 该迁到哪一级」，直接当工单用。
    """
    problems = []
    for line, selector, prop, value in _spacing_declarations(_css()):
        hits = _LENGTH.findall(value)
        if not hits:
            continue
        if (selector, prop) in WHITELIST:
            continue
        for number, unit in hits:
            px = _px(number, unit)
            token, token_px = nearest_step(px)
            sign = '-' if px < 0 else ''
            delta = (abs(px) - token_px) * (-1 if px < 0 else 1)
            problems.append(
                f'style.css:{line}  `{selector}` {prop}: {value}'
                f'\n      {number}{unit}（{px:g}px）-> {sign}var({token})'
                f'（{sign}{token_px:g}px，差 {delta:+g}px）'
            )
    assert not problems, (
        f'{len(problems)} 处间距字面量还没迁入刻度（设计稿 §3.1）：\n'
        + '\n'.join('  ' + p for p in problems)
        + '\n\n刻度：' + '  '.join(f'{k}={v}' for k, v in sorted(
            SCALE.items(), key=lambda kv: float(kv[1][:-2])))
    )


def test_whitelist_has_no_stale_entries():
    """白名单里的每一条都必须真的还是一处「带裸长度的间距声明」。

    白名单会腐烂：某处改成 var() 之后豁免还留着，下一个人照抄这条豁免，
    就把一处本该迁移的声明放行了。这条让腐烂的豁免自己响。
    """
    live = {
        (selector, prop)
        for _l, selector, prop, value in _spacing_declarations(_css())
        if _LENGTH.search(value)
    }
    stale = sorted(k for k in WHITELIST if k not in live)
    assert not stale, (
        '白名单里这些条目已经不是「带裸长度的间距声明」了，删掉它们：\n'
        + '\n'.join(f'  {sel} {{ {prop} }} —— {WHITELIST[(sel, prop)]}'
                    for sel, prop in stale)
    )


def test_every_scale_step_is_actually_used():
    """七级刻度每一级都至少被一处 `var()` 消费。

    铸了不用的令牌比字面量更坏：它看着像家规，实际没有任何东西按它渲染，
    下一个人照它写出来的间距与屏幕上的不一样。真有一级用不上，说明刻度分级
    错了，该删的是那一级，不是把断言放宽。
    """
    css = _strip_comments_keep_lines(_css())
    unused = []
    for name in sorted(SCALE):
        # 排除 `:root` 里的定义行本身，只数 var() 引用
        if len(re.findall(r'var\(\s*' + re.escape(name) + r'\s*\)', css)) == 0:
            unused.append(name)
    assert not unused, (
        f'这些刻度级一次都没被 var() 引用：{unused} —— '
        '铸了不用的令牌是假家规，要么用它，要么删这一级'
    )
