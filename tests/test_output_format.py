"""
OutputFormat enum and semantics tests
"""

import ast
import os
import sys
from html.parser import HTMLParser

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models.task import OutputFormat


def test_image_only_enum_member_exists():
    """Task 6 会直接引用 OutputFormat.IMAGE_ONLY,枚举成员本身必须在"""
    assert OutputFormat.IMAGE_ONLY.value == 'image_only'


def test_image_only_is_a_valid_format():
    """index.html 提供了 image_only 选项,枚举必须认它"""
    assert OutputFormat.from_shorthand('image_only') == 'image_only'


def test_image_only_shorthand():
    assert OutputFormat.from_shorthand('i') == 'image_only'


def test_existing_formats_still_work():
    """回归保护:原有四个值不能被破坏"""
    assert OutputFormat.from_shorthand('both') == 'both'
    assert OutputFormat.from_shorthand('b') == 'both'
    assert OutputFormat.from_shorthand('tiles_only') == 'tiles_only'
    assert OutputFormat.from_shorthand('t') == 'tiles_only'
    assert OutputFormat.from_shorthand('png') == 'png'
    assert OutputFormat.from_shorthand('jpg') == 'jpg'


def test_unknown_format_still_raises():
    with pytest.raises(ValueError):
        OutputFormat.from_shorthand('definitely_not_a_format')


def test_task_accepts_image_only():
    """真正会崩的入口是 Task.__post_init__ —— 它在构造时调 from_shorthand"""
    from models.task import Task

    task = Task(name='t', north=1.0, south=0.0, east=1.0, west=0.0,
                output_format='image_only')
    assert task.output_format == 'image_only'


# ---------------------------------------------------------------------------
# 输出语义契约
#
# 跑真实拼接需要 GDAL + 完整任务链路,所以这里用 AST 静态读取
# task_manager.py 的分支结构来断言语义,而不是执行它。
#   stitch = 拼接 GeoTIFF   copy = 把瓦片复制到 output_path
# ---------------------------------------------------------------------------

EXPECTED_ACTIONS = {
    'both': {'stitch', 'copy'},
    'image_only': {'stitch'},
    'png': {'stitch'},        # 遗留值,只作为 image_only 的同义词
    'jpg': {'stitch'},        # 同上
    'tiles_only': {'copy'},
}

# 分支体内出现这些调用,就认定它执行了对应动作
ACTION_MARKERS = {
    'stitch': 'stitch_tiles_with_gdal',
    'copy': 'copy2',
}

TASK_MANAGER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'services', 'task_manager.py',
)


def _formats_in_test(test):
    """从 `task.output_format == 'x'` / `... in ['x', 'y']` 抽出格式集合

    与 output_format 无关的条件返回 None。
    """
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    if not (isinstance(test.left, ast.Attribute) and test.left.attr == 'output_format'):
        return None

    op, comparator = test.ops[0], test.comparators[0]
    if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
        return {comparator.value}
    if isinstance(op, ast.In) and isinstance(comparator, (ast.List, ast.Tuple, ast.Set)):
        return {e.value for e in comparator.elts if isinstance(e, ast.Constant)}
    return None


def _output_format_branches():
    """收集 task_manager.py 中所有以 output_format 为条件的分支

    每项:formats=命中该分支的格式,actions=分支内的动作,
    chained_after=该分支是否挂在另一个 output_format 分支的 else 上(即排他 elif)。
    """
    with open(TASK_MANAGER_PATH, encoding='utf-8') as f:
        tree = ast.parse(f.read())

    ifs = [n for n in ast.walk(tree)
           if isinstance(n, ast.If) and _formats_in_test(n.test) is not None]

    chained = {
        id(sibling)
        for node in ifs
        for sibling in node.orelse
        if isinstance(sibling, ast.If) and _formats_in_test(sibling.test) is not None
    }

    branches = []
    for node in ifs:
        dumped = '\n'.join(ast.dump(stmt) for stmt in node.body)
        branches.append({
            'formats': _formats_in_test(node.test),
            'actions': {name for name, marker in ACTION_MARKERS.items() if marker in dumped},
            'chained_after': id(node) in chained,
        })
    return branches


def test_output_format_branches_are_independent_ifs():
    """拼接与复制必须是两个独立 if —— 排他 elif 会让 both 永远走不到复制分支"""
    for branch in _output_format_branches():
        assert not branch['chained_after'], (
            f"分支 {sorted(branch['formats'])} 挂在另一个 output_format 分支的 else 上,"
            "both 无法同时拼接和复制"
        )


@pytest.mark.parametrize('fmt, expected', sorted(EXPECTED_ACTIONS.items()))
def test_output_format_actions_match_contract(fmt, expected):
    """both=拼接+复制,image_only(含 png/jpg)=只拼接,tiles_only=只复制"""
    actions = set()
    for branch in _output_format_branches():
        if fmt in branch['formats']:
            actions |= branch['actions']

    assert actions == expected, (
        f"output_format={fmt!r} 实际会执行 {sorted(actions)},契约要求 {sorted(expected)}"
    )


# ---------------------------------------------------------------------------
# 前后端契约:index.html 的 #outputFormat 下拉里每个 value,后端枚举都必须认
#
# image_only 这个 bug 的根源就是前端加了选项、后端枚举没跟上。static/js/map.js
# 直接把 document.getElementById('outputFormat').value 当 output_format 提交,
# 所以「下拉里出现但枚举不认」= 用户一选就崩。
#
# 用 stdlib 的 HTMLParser 真解析,而不是正则匹配标签文本:属性顺序、引号风格、
# 多余属性、跨行写法都不会让这个测试失效或误判。
#
# 契约是单向的:只要求「下拉 ⊆ 枚举」。反向不成立 —— png/jpg 是遗留值,
# 故意不出现在 UI 上,所以不断言「枚举里的值都得在下拉里」。
# ---------------------------------------------------------------------------

INDEX_HTML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'templates', 'index.html',
)


class _SelectOptionCollector(HTMLParser):
    """收集指定 id 的 <select> 下所有 <option> 的 value(附行号,便于定位)

    values 里的 value 为 None 表示该 <option> 没写 value 属性。
    """

    def __init__(self, select_id):
        super().__init__(convert_charrefs=True)
        self.select_id = select_id
        self.found_select = False
        self.values = []          # [(value_or_None, lineno)]
        self._inside = False

    def handle_starttag(self, tag, attrs):
        if tag == 'select':
            # <select> 不能嵌套,所以遇到任何 select 开标签都重新判定
            self._inside = dict(attrs).get('id') == self.select_id
            self.found_select = self.found_select or self._inside
        elif tag == 'option' and self._inside:
            self.values.append((dict(attrs).get('value'), self.getpos()[0]))

    def handle_endtag(self, tag):
        if tag == 'select':
            self._inside = False


def _collect_options(html, select_id):
    collector = _SelectOptionCollector(select_id)
    collector.feed(html)
    collector.close()
    return collector


def test_every_option_in_index_html_is_a_valid_output_format():
    """index.html 的 #outputFormat 下拉里每个 value,OutputFormat 都必须认"""
    with open(INDEX_HTML_PATH, encoding='utf-8') as f:
        html = f.read()

    collector = _collect_options(html, 'outputFormat')

    assert collector.found_select, (
        'templates/index.html 里找不到 <select id="outputFormat">。'
        '如果下拉框改名或挪走了,这个契约测试必须同步改,否则它就白站岗了'
    )
    assert collector.values, (
        '#outputFormat 下拉里一个 <option> 都没有。'
        '如果选项改成了 JS 动态注入,这个测试已经守不住契约了,'
        '必须改成从 JS 取值或直接打 /api/tasks 接口验证'
    )

    rejected = []
    for value, lineno in collector.values:
        if value is None:
            rejected.append(
                f'index.html:{lineno} 的 <option> 没有 value 属性,'
                '提交上来会是 option 的文字内容'
            )
            continue
        try:
            OutputFormat.from_shorthand(value)
        except ValueError as exc:
            rejected.append(f'index.html:{lineno} value={value!r} 后端不认 —— {exc}')

    assert not rejected, (
        '#outputFormat 下拉里有 OutputFormat 不认的值,用户选中即报错:\n  '
        + '\n  '.join(rejected)
    )


def test_option_collector_survives_real_world_markup():
    """守护上面那个解析器本身 —— 它认错了 option,契约测试就成了空壳"""
    html = """
    <select id="other"><option value="noise">别的下拉</option></select>
    <select
        class="form-select"
        data-role='x'
        id="outputFormat"
        required>
      <option value="both">瓦片+拼接图</option>
      <option selected value='tiles_only'>仅瓦片</option>
      <option data-hint="1" value="not_a_format">坏值</option>
      <option>没有 value</option>
    </select>
    <select id="later"><option value="noise2">又一个</option></select>
    """
    collector = _collect_options(html, 'outputFormat')

    assert collector.found_select
    assert [v for v, _ in collector.values] == [
        'both', 'tiles_only', 'not_a_format', None,
    ]
