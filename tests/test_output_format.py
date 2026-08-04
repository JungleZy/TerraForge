"""
OutputFormat enum and semantics tests
"""

import ast
import os
import re
import sys
from html.parser import HTMLParser

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.models.task import OutputFormat


def test_image_only_enum_member_exists():
    """Task 6 会直接引用 OutputFormat.IMAGE_ONLY,枚举成员本身必须在"""
    assert OutputFormat.IMAGE_ONLY.value == 'image_only'


def test_image_only_is_a_valid_format():
    """枚举必须继续认 image_only:存量任务和 API 客户端还在用它(下拉已移除该选项)"""
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
    from src.models.task import Task

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
    'image_only': {'stitch', 'copy'},
    'png': {'stitch', 'copy'},        # 遗留值,只作为 image_only 的同义词
    'jpg': {'stitch', 'copy'},        # 同上
    'tiles_only': {'copy'},
}

# 分支体内出现这些调用,就认定它执行了对应动作
ACTION_MARKERS = {
    'stitch': 'stitch_tiles_with_gdal',
    'copy': 'copy2',
}

TASK_MANAGER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'src', 'services', 'task_manager.py',
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
    """所有格式都复制瓦片(历史预览依赖产物目录),tiles_only 只复制不拼接"""
    actions = set()
    for branch in _output_format_branches():
        if fmt in branch['formats']:
            actions |= branch['actions']

    assert actions == expected, (
        f"output_format={fmt!r} 实际会执行 {sorted(actions)},契约要求 {sorted(expected)}"
    )


# ---------------------------------------------------------------------------
# 前后端契约:输出格式多选(checkbox)映射出的每个值,后端枚举都必须认
#
# image_only 这个 bug 的根源就是前端加了选项、后端枚举没跟上。控件形态从
# <select> → radio → checkbox 多选(瓦片 / GeoTIFF)之后,提交值不再直接来自
# 控件,而是 static/js/map.js 的 _outputFormatValue() 映射出来的:
#   都勾 → both   只勾瓦片 → tiles_only   只勾 GeoTIFF → image_only
# 所以契约拆成两半:
#   1. index.html 里两个 checkbox 必须在(少一个,映射就少一条腿);
#   2. _outputFormatValue() 返回的每个字面量,OutputFormat 都必须认。
#
# 用 stdlib 的 HTMLParser 真解析,而不是正则匹配标签文本:属性顺序、引号风格、
# 多余属性、跨行写法都不会让这个测试失效或误判。
#
# 契约是单向的:只要求「映射值 ⊆ 枚举」。反向不成立 —— png/jpg 是遗留值,
# 故意不由 UI 产出,所以不断言「枚举里的值都得被映射到」。
# ---------------------------------------------------------------------------

INDEX_HTML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'templates', 'index.html',
)
MAP_JS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'static', 'js', 'map.js',
)

OUTPUT_FORMAT_CHECKBOX_IDS = {'outputFormatTiles', 'outputFormatGeoTiff'}


class _CheckboxIdCollector(HTMLParser):
    """收集指定 name 的 <input type="checkbox"> 的 id(附行号,便于定位)"""

    def __init__(self, checkbox_name):
        super().__init__(convert_charrefs=True)
        self.checkbox_name = checkbox_name
        self.ids = []             # [(id_or_None, lineno)]

    def handle_starttag(self, tag, attrs):
        if tag != 'input':
            return
        attr = dict(attrs)
        if attr.get('type') == 'checkbox' and attr.get('name') == self.checkbox_name:
            self.ids.append((attr.get('id'), self.getpos()[0]))


def _output_format_value_body():
    """取出 map.js 里 _outputFormatValue 的函数体(到下一个顶层函数为止)"""
    with open(MAP_JS_PATH, encoding='utf-8') as f:
        src = f.read()
    start = src.index('function _outputFormatValue(')
    next_fn = src.index('\nfunction ', start)
    return src[start:next_fn]


def test_output_format_checkboxes_exist_in_index_html():
    """index.html 必须有 瓦片/GeoTIFF 两个输出格式 checkbox"""
    with open(INDEX_HTML_PATH, encoding='utf-8') as f:
        html = f.read()

    collector = _CheckboxIdCollector('outputFormat')
    collector.feed(html)
    collector.close()

    found = {cid for cid, _ in collector.ids}
    assert found == OUTPUT_FORMAT_CHECKBOX_IDS, (
        f'templates/index.html 的 outputFormat checkbox 集合是 {sorted(found)},'
        f'期望 {sorted(OUTPUT_FORMAT_CHECKBOX_IDS)}。'
        '如果控件改名或换了形态,这个契约测试必须同步改,否则它就白站岗了'
    )


def test_output_format_mapping_returns_only_valid_enum_values():
    """_outputFormatValue() 返回的每个值,OutputFormat 都必须认"""
    body = _output_format_value_body()

    returned = set(re.findall(r"return '([^']+)'", body))
    assert returned, (
        '_outputFormatValue() 里没有 return 字面量。'
        '如果映射改成动态计算,这个测试已经守不住契约了,必须改成执行 JS 或打接口验证'
    )

    rejected = []
    for value in sorted(returned):
        try:
            OutputFormat.from_shorthand(value)
        except ValueError as exc:
            rejected.append(f'map.js _outputFormatValue 返回 {value!r} 后端不认 —— {exc}')

    assert not rejected, (
        '输出格式多选映射出了 OutputFormat 不认的值,用户选中即报错:\n  '
        + '\n  '.join(rejected)
    )


def test_output_format_mapping_has_none_guard():
    """两个都不勾时必须返回 null —— 不可能有没有产出的下载,提交处靠它拦截"""
    body = _output_format_value_body()
    assert 'return null' in body, (
        '_outputFormatValue() 缺少全不勾时的 return null 兜底,'
        '提交校验(showNotification 拦截)会失效'
    )
