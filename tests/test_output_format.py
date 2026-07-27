"""
OutputFormat enum and semantics tests
"""

import ast
import os
import sys

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
