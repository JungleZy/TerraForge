"""等高线按任务自定义配色：validate_tint / style_for_task 契约。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.contour_task_manager import style_for_task, validate_tint


class _FakeConfig:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.get(key, default)


# ---- validate_tint -----------------------------------------------------------

def test_validate_tint_empty_means_default():
    assert validate_tint("", "") == ("", "")
    assert validate_tint(None, None) == ("", "")


def test_validate_tint_rejects_one_sided():
    with pytest.raises(ValueError):
        validate_tint("0,500", "")
    with pytest.raises(ValueError):
        validate_tint("", "#fff,#aaa")


def test_validate_tint_rejects_non_ascending_breaks():
    # 颜色用合法值：颜色校验发生在断点检查之前，用 '#1' 之类的占位色会让这条
    # 用例其实是在测颜色而不是断点顺序。
    with pytest.raises(ValueError):
        validate_tint("0,500,200", "#111111,#222222,#333333,#444444")


def test_validate_tint_rejects_count_mismatch():
    with pytest.raises(ValueError):
        validate_tint("0,500", "#111111,#222222")          # 2 断点要 3 色
    with pytest.raises(ValueError):
        validate_tint("0,500", "#111111,#222222,#333333,#444444")


def test_validate_tint_rejects_bad_color():
    """判据换成渲染器自己的解析器（matplotlib.colors.to_rgba），不再是 `#` 前缀。

    原用例断言 `validate_tint("0", "red,#fff")` 报错 —— 那钉的是旧判据的**错误
    一侧**：'red' 渲染器认得，拒它只是把 `#` 前缀这条规则写进了测试。真正要拦的
    是「带 `#` 但渲染器不认」的值，那才是评审 P1#10：`#zzzzzz` 一路通到 per-tile
    渲染，把每张瓦片吞成 failed。
    """
    with pytest.raises(ValueError):
        validate_tint("0", "#zzzzzz,#fff")
    with pytest.raises(ValueError):
        validate_tint("0", "#12345,#fff")
    # 渲染器认得的写法必须放行（旧判据会把这两个都拒掉）
    assert validate_tint("0", "red,#fff") == ("0.0", "red,#fff")
    assert validate_tint("0", "#aabbccdd,#fff")[1] == "#aabbccdd,#fff"


def test_validate_tint_normalizes():
    breaks, colors = validate_tint("0, 500 ,1000", "#111111, #222222,#333333,#444444")
    assert breaks == "0.0,500.0,1000.0"
    assert colors == "#111111,#222222,#333333,#444444"


# ---- style_for_task ----------------------------------------------------------

def _task(**over):
    base = {
        "background": "#FAF6EC",
        "line_color_intermediate": "",
        "line_color_index": "",
        "tint_breaks": "",
        "tint_colors": "",
    }
    base.update(over)
    return base


def test_style_for_task_defaults_when_no_overrides():
    style = style_for_task(_FakeConfig(), _task())
    assert style.color_intermediate == "#9C6B3F"
    assert style.color_index == "#7A4F2A"
    assert len(style.hypsometric_colors) == len(style.hypsometric_breaks) + 1


def test_style_for_task_applies_line_colors_and_label_follows_index():
    style = style_for_task(_FakeConfig(), _task(
        line_color_intermediate="#111111", line_color_index="#222222"))
    assert style.color_intermediate == "#111111"
    assert style.color_index == "#222222"
    assert style.color_label == "#222222"


def test_style_for_task_applies_tint_overrides():
    style = style_for_task(_FakeConfig(), _task(
        tint_breaks="0,1000", tint_colors="#111111,#222222,#333333"))
    assert style.hypsometric_breaks == (0.0, 1000.0)
    assert style.hypsometric_colors == ("#111111", "#222222", "#333333")


def test_style_for_task_keeps_task_background():
    style = style_for_task(_FakeConfig(), _task(background="transparent"))
    assert style.background == "transparent"
