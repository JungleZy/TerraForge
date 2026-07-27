"""
OutputFormat enum and semantics tests
"""

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
