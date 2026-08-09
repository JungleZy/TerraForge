"""高程切片三档预设：取值表、校验与层级偏移语义。

选型依据见 docs/reference/terrain/tiling-presets-measured.md。
"""
import pytest

from conftest import fresh_import  # noqa: E402


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


def _fresh_db(monkeypatch, tmp_path):
    """新库 + 建表。禁止手写 sys.modules.pop（conftest.py:10-12 的规矩）。"""
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "t.db")
    db = fresh_import(monkeypatch, "src.core.database")
    db.init_database()
    return db


def test_config_defaults_are_shipped(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    defaults = dict(db.DEFAULT_CONFIGS)

    assert defaults["terrain_quality_preset"] == "balanced"
    # 布尔配置在本仓一律存字符串 'true'/'false'（config_manager:294-295）
    assert defaults["terrain_vertex_normals"] == "false"


@pytest.mark.parametrize("table", ["dem_terrain_jobs", "local_terrain_tasks"])
def test_preset_columns_exist_with_defaults(monkeypatch, tmp_path, table):
    """新列必须带 DEFAULT —— tests/test_terrain_api.py 有不列新列的裸 INSERT。"""
    db = _fresh_db(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        cols = {r[1]: r for r in cur.fetchall()}  # (cid,name,type,notnull,dflt,pk)
    finally:
        conn.close()

    assert "quality" in cols, f"{table} 缺 quality 列"
    assert "vertex_normals" in cols, f"{table} 缺 vertex_normals 列"
    assert cols["quality"][4] == "'balanced'"
    assert cols["vertex_normals"][4] == "0"
