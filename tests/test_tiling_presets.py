"""高程切片三档预设：取值表、校验与层级偏移语义。

选型依据见 docs/reference/terrain/tiling-presets-measured.md。
"""
import pytest


def test_quality_offsets_are_a_one_step_ladder():
    """三档必须是「基准 ±1」的等距梯子 —— 实测每级 3.3 倍体积换 2.8 倍精度。"""
    from src.services.geo_validation import TILING_QUALITY_OFFSETS

    assert TILING_QUALITY_OFFSETS == {
        "precision": 1, "balanced": 0, "speed": -1}


def test_default_quality_is_balanced():
    from src.services.geo_validation import (DEFAULT_TILING_QUALITY,
                                             TILING_QUALITY_OFFSETS)

    assert DEFAULT_TILING_QUALITY == "balanced"
    assert TILING_QUALITY_OFFSETS[DEFAULT_TILING_QUALITY] == 0


@pytest.mark.parametrize("value", ["precision", "balanced", "speed"])
def test_validate_accepts_every_preset(value):
    from src.services.geo_validation import validate_tiling_quality

    assert validate_tiling_quality(value) == value


@pytest.mark.parametrize("value", ["", "fast", "BALANCED", None, 0, True])
def test_validate_rejects_anything_else(value):
    """拼错必须当场报错，不能静默退回默认档 —— 那是本仓栽过三次的失效形态。"""
    from src.services.geo_validation import validate_tiling_quality

    with pytest.raises(ValueError):
        validate_tiling_quality(value)
