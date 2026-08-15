"""层叠模型自身的单元测试 —— 模型扩展（2026-08-14）的看守。

这个文件测的不是产品 CSS，是 tests/test_css_contract.py 里那套「算最终颜色」
的模型本身。理由：模型算错的后果是「静默给出错误的信心」，比没有断言更糟
（`test_text_color_model_assumptions_still_hold` 的 docstring 用同一句话记过
这笔学费）。

为什么不通过 `_winning_color_decl` 来测：那个函数有两道**对全量 style.css 才
成立**的护栏 —— `assert scanned > 100`（扫到的 color 规则不足 100 条就判扫描
逻辑失效）与 `_bootstrap_color_competitors`（要跟 Bootstrap 的竞争者比排名）。
拿两条规则的玩具 CSS 喂它只会在护栏上失败，测不到我们要测的东西。所以：
  · 组合符支持 -> 直接测 `_text_branch_applies`（纯函数，只吃 branch + chain）
  · at-rule 判决 -> 直接测 `_color_media_verdict`（与 `_motion_media_verdict`
    同形，可单测），再由 `_winning_color_decl` 在全量 CSS 上消费它。

⚠️ 本文件引用 test_css_contract.py 里的东西一律只写**符号名**，不写行号。
那个文件 8700 多行、天天在动，行号写进注释当天就会过期 —— 第一版写了 7 处
行号，6 处在提交前就已经指错了地方（2026-08-14 修这批假引用时定的规矩）。
"""

import pytest

# 裸模块名 import：tests/ 下没有 __init__.py，conftest.py 已经把仓库根放进
# sys.path（它的 docstring 就是为了终结各文件自己 sys.path.insert 才写的），
# pytest 又会把 tests/ 目录自己前置。写成 `tests.test_css_contract` 会让这个
# 8700 行的模块以两个名字被导入两遍，两份 module 对象各有一套 helper。
# 七个姊妹文件（test_tasks_js_contract.py 等）用的都是裸名，跟着它们。
# 姊妹文件都挂着 `# noqa: E402`（它们 import 前有 sys.path.insert）；本文件删掉了
# 那一块，import 前没有任何语句，所以 E402 不成立，noqa 也就不写。
from test_css_contract import (
    _TextEl,                     # 2026-08-14 随模型扩展一起引入
    _color_media_verdict,
    _text_branch_applies,
)


def _chain(*specs):
    """`'div.card'` / `'span.detail-v'` -> [_TextEl, ...]，末项是被算的那个元素。

    节点类型必须是 `_TextEl`（test_css_contract.py 里的那个类）—— `_text_branch_applies`
    会读 node.pseudos / node.pseudo_element，裸元组喂不进去。
    """
    out = []
    for spec in specs:
        tag, _, rest = spec.partition('.')
        out.append(_TextEl(tag=(tag or 'div'),
                           classes=[c for c in rest.split('.') if c]))
    return out


# ---------------------------------------------------------------- 子组合符

def test_child_combinator_matches_a_direct_parent():
    chain = _chain('div.card', 'div.card-body', 'span.detail-v')
    assert _text_branch_applies('.card-body > .detail-v', chain) is True


def test_child_combinator_rejects_a_skipped_generation():
    """`.card` 是祖父不是直接父，所以 `.card > .detail-v` 必须判 False。

    别把这条当成「修了一个错判」：扩展前的实现是 `parts = branch.split()`，
    `>` 会被切成独立一段，`_parse_compound('>')` 读不懂返回 None，于是**整个
    分支返回 None** —— 一律拒答，从来没给过错答案。
    这条锁的是「拒答 -> 正确答案」这一步不许退化成「拒答 -> 错答案」：
    现在模型敢答了，差一代就必须是 False，不能因为 `.card` 在链里就放行。
    """
    chain = _chain('div.card', 'div.card-body', 'span.detail-v')
    assert _text_branch_applies('.card > .detail-v', chain) is False


def test_descendant_combinator_still_matches_across_generations():
    chain = _chain('div.card', 'div.card-body', 'span.detail-v')
    assert _text_branch_applies('.card .detail-v', chain) is True


def test_mixed_child_and_descendant_chain():
    chain = _chain('section.panel', 'div.card', 'div.card-body', 'span.detail-v')
    assert _text_branch_applies('.panel .card > .detail-v', chain) is False
    assert _text_branch_applies('.panel .card-body > .detail-v', chain) is True


def test_child_combinator_at_the_root_of_the_chain():
    """链首之上没有节点了，`> ` 必须判 False 而不是越界。"""
    chain = _chain('div.card', 'span.detail-v')
    assert _text_branch_applies('.nope > .card > .detail-v', chain) is False


# ------------------------------------------------------ 回溯（2026-08-14 修复）
#
# 从右往左走祖先链时，后代组合符那一步不能贪心：取了最近的候选之后如果剩下
# 的部分走不通，必须退回来试更上面的候选。贪心版本会把**真命中**的规则判成
# 不命中 —— 规则被踢出候选集 -> 赢家算错 -> 对比度数字算错 -> 全绿。

def test_descendant_step_backtracks_when_the_nearest_candidate_dead_ends():
    """`.a > .b .c` 对 [div.a, div.b, div.b, span.c] 必须判 True。

    贪心版本在这里判 False：后代那一步先抓最近的 `.b`（chain[2]），`>` 那一步
    再去看 chain[1]，发现是 `.b` 不是 `.a`，直接收工。CSS 的答案是**命中** ——
    chain[1] 的 `.b` 确实是 chain[0] 的 `.a` 的直接子元素，`span.c` 是它的后代。
    """
    chain = _chain('div.a', 'div.b', 'div.b', 'span.c')
    assert _text_branch_applies('.a > .b .c', chain) is True


def test_backtracking_does_not_invent_a_match():
    """同一个形态，链首换成 `.x`：链里没有 `.a`，回溯不许凭空造出命中。"""
    chain = _chain('div.x', 'div.b', 'div.b', 'span.c')
    assert _text_branch_applies('.a > .b .c', chain) is False


def test_backtracking_tries_every_candidate_before_giving_up():
    """要试 4 个 `.b` 才碰到能让 `>` 成立的那个（最上面那个）。"""
    chain = _chain('div.a', 'div.b', 'div.b', 'div.b', 'div.b', 'span.c')
    assert _text_branch_applies('.a > .b .c', chain) is True


def test_child_step_stays_position_exact_under_backtracking():
    """`>` 那一步不许回溯 —— 它说的就是「上一代」，换个位置就不是它了。

    `.a > .b > .c` 对 [div.a, div.b, div.b, span.c]：`span.c` 的父是 chain[2]
    的 `.b`（成立），但它的父是 chain[1] 的 `.b` 不是 `.a`，所以整体不命中。
    回溯如果放宽到 `>` 就会把这条错判成 True。
    """
    chain = _chain('div.a', 'div.b', 'div.b', 'span.c')
    assert _text_branch_applies('.a > .b > .c', chain) is False


def test_child_combinator_after_a_repeated_descendant_step():
    """`.a .b > .c`：`>` 定死 chain[-2]，`.a` 那一步要越过重复的 `.b` 往上找。"""
    hit = _chain('div.a', 'div.b', 'div.b', 'span.c')
    assert _text_branch_applies('.a .b > .c', hit) is True
    miss = _chain('div.x', 'div.b', 'div.b', 'span.c')
    assert _text_branch_applies('.a .b > .c', miss) is False


def test_ancestor_pseudo_is_judged_together_with_position():
    """祖先的伪类必须参与「挑哪个位置」，不能等链走完再补判。

    链里两个 `.card`，只有外面那个是 hover 态。`.card:hover .detail-v` 的答案
    是命中；先按结构挑到最近那个（没 hover）再补判伪类的写法会判 False ——
    与上面那个贪心漏判同一类错，只是错在伪类维度。
    """
    chain = [_TextEl(tag='div', classes=['card'], pseudos=['hover']),
             _TextEl(tag='div', classes=['card']),
             _TextEl(tag='span', classes=['detail-v'])]
    assert _text_branch_applies('.card:hover .detail-v', chain) is True


def test_unparseable_ancestor_does_not_outrank_a_subject_miss():
    """主体明确不命中时，读不懂的**祖先**不许把结论升级成「模型已失效」。

    `:not(caption)` 是真读不懂的写法（`_parse_compound` 只认参数是单个类或单个
    伪类的 `:not()`，元素名一律返回 None），而且不是编的 —— Bootstrap 的
    `.table > :not(caption) > * > *` 就长这样，它正是本模型要跟它比排名的竞争者。
    主体 `.something-else` 根本打不到 `span.detail-v`，答案就是确定的不命中。
    反过来写（先解析全部复合项，再判主体）会让这类与本元素无关、只是祖先侧
    写法花哨的规则把整个颜色模型顶成「已失效」。
    """
    chain = _chain('div.card', 'span.detail-v')
    assert _text_branch_applies(':not(caption) .something-else', chain) is False
    # 反证：同一个读不懂的祖先，主体命中时就必须响亮失败，不许悄悄放过。
    assert _text_branch_applies(':not(caption) .detail-v', chain) is None


@pytest.mark.parametrize('branch', ('.card + .detail-v', '.card ~ .detail-v'))
def test_sibling_combinators_stay_unsupported(branch):
    """兄弟组合符必须返回 None（响亮失败），不许猜。

    祖先链里没有兄弟节点：当成后代会**多**匹配（可能把管不到的规则算成赢家），
    当成不匹配又会漏。这条锁的是「不猜」这个决定本身。
    """
    chain = _chain('div.card', 'span.detail-v')
    assert _text_branch_applies(branch, chain) is None


def test_subject_miss_is_decided_before_shape_support():
    """主体复合项肯定不命中时先返回 False，别报「形态不支持」。

    判定顺序是承重的 —— `_text_branch_applies` 的「第一步」注释与 `_branch_matches`
    的 docstring 都记过：反过来写会被 Bootstrap 里大量与本元素八竿子打不着的规则
    （`.btn-check:checked+.btn` 这类）把模型整个顶成「已失效」。
    """
    chain = _chain('div.card', 'span.detail-v')
    assert _text_branch_applies('.card + .something-else', chain) is False


# ------------------------------------------------------------ 属性选择器
#
# 2026-08-15 补：改前 `_parse_compound` 的 `leftover` 正则把 `\[[^\]]*\]` 当噪声
# 抹掉、且**不记在任何字段里**，于是 `[data-bogus]` 解析成 tag/ids/classes 全空的
# compound —— `_compound_structurally_matches` 对它一律为真，**等价于 `*`**。
# 实测 `_text_branch_applies('[data-bogus]', [span.detail-v])` 返回 True。
# 那不是拒答，是静默多匹配：一条其实管不到这个元素的规则会参与
# `_winning_color_decl` 的胜负比较、可能赢下来，对比度数字算错而断言全绿 ——
# 与上面兄弟组合符那条锁的是同一个决定（不猜），只是维度换成了属性。

def test_attribute_selector_on_the_subject_stays_unsupported():
    """主体带属性选择器、其余部分都对得上时，必须返回 None（响亮失败）。

    `_TextEl` 不记属性，模型判不了 `[data-x]` 的真假。改前这里返回 True。
    """
    chain = _chain('div.card', 'span.detail-v')
    assert _text_branch_applies('.detail-v[data-x]', chain) is None
    # 光杆属性选择器：改前解析成全空 compound，对任何元素都判命中。
    bare = [_TextEl(tag='span', classes=['detail-v'])]
    assert _text_branch_applies('[data-bogus]', bare) is None


def test_attribute_selector_on_an_ancestor_stays_unsupported():
    """祖先侧的属性选择器同样判不了 —— 改前它被当成 `*`，那个 False 是猜的。"""
    chain = _chain('div.card', 'span.detail-v')
    assert _text_branch_applies('[data-x] .detail-v', chain) is None


def test_attribute_selector_miss_is_still_decided():
    """属性选择器不许把「确定不命中」也升级成「模型已失效」。

    与 `test_unparseable_ancestor_does_not_outrank_a_subject_miss` 同一条原则，
    而且这条是承重的：style.css 有 19 条带属性选择器的规则，其中
    `.map-search__chip[aria-pressed="true"]` 声明了 color。它对每一条已登记文字
    上下文都要在**类**这一步就判掉；一律拒答的话，`_winning_color_decl` 会在
    全部 20 条上下文上判「模型已失效」，那是误报不是保护。
    （另一头由 test_css_contract.py 的 `_ATTR_COLOR_WHITELIST` +
    test_text_color_model_assumptions_still_hold 第五条前提钉住。）
    """
    chain = _chain('div.card', 'span.detail-v')
    assert _text_branch_applies('.map-search__chip[aria-pressed="true"]', chain) is False
    # 伪类维度也一样：结构对得上但静止态没有 hover -> 确定不命中，轮不到属性拒答。
    assert _text_branch_applies('.detail-v:hover[data-x]', chain) is False


# ---------------------------------------------------------------- at-rule 判决

@pytest.mark.parametrize('at_rule, reduced, expected', (
    ('@media (prefers-reduced-motion: reduce)', True, True),
    ('@media (prefers-reduced-motion: reduce)', False, False),
    ('@media (prefers-reduced-motion: no-preference)', False, True),
))
def test_prefers_at_rules_are_evaluated(at_rule, reduced, expected):
    assert _color_media_verdict(at_rule, reduced=reduced) is expected


@pytest.mark.parametrize('at_rule', (
    '@media (max-width: 576px)',
    '@media (min-width: 768px)',
    '@supports not (backdrop-filter: blur(1px))',
))
def test_non_environment_at_rules_stay_unsupported(at_rule):
    """宽度断点与 @supports 必须返回 None —— 模型不建这两种环境。

    与 `_motion_media_verdict` 同一个决定：「宽度类断点一律返回 None
    （模型不支持）—— 只要有人往里塞声明就响亮失败」。
    """
    assert _color_media_verdict(at_rule, reduced=False) is None
