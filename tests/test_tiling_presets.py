"""高程切片三档预设：取值表、校验与层级偏移语义。

选型依据见 docs/reference/terrain/tiling-presets-measured.md。
"""
import sqlite3

import pytest

from conftest import fresh_import


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


# --------------------------------------------------------------------------
# 落库结构：建表语句与 ALTER 迁移是**两条**必须各自成立的路径
# --------------------------------------------------------------------------
#
# 两条路径在同一个 init_database() 里先后跑完，所以「列在不在」这一个断言同时
# 被两边喂饱：删掉建表语句里的两列（迁移补上）、或删掉四条迁移（新库自带）都
# 照样绿。判别点是**位置**：ALTER TABLE ADD COLUMN 只能追加到末尾，而建表语句
# 里这两列写在 maxzoom 之后、parent_url 之前。于是
#   quality 在 parent_url 之前 ⟺ 它来自建表语句
#   quality 在 error_message 之后 ⟺ 它来自 ALTER 迁移
# 两条用例各钉一边，任一边被删都当场红。

# 老库形态：Task 5 之前的建表语句原样去掉两个新列。init_database 的
# CREATE TABLE IF NOT EXISTS 不会重建已存在的表，所以这两张表只能靠迁移补列。
# 外键刻意不写：这里只关心列结构，dem_tasks 由 init_database 自己建。
_LEGACY_SCHEMA = (
    """
    CREATE TABLE dem_terrain_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        output_dir TEXT NOT NULL,
        maxzoom INTEGER NOT NULL,
        parent_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        started_at TIMESTAMP,
        completed_at TIMESTAMP,
        error_message TEXT,
        UNIQUE(task_id)
    )
    """,
    """
    CREATE TABLE local_terrain_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        status TEXT NOT NULL,
        output_path TEXT NOT NULL,
        source_dir TEXT NOT NULL,
        output_dir TEXT NOT NULL,
        total_files INTEGER DEFAULT 0,
        uploaded_files INTEGER DEFAULT 0,
        failed_files INTEGER DEFAULT 0,
        maxzoom INTEGER NOT NULL,
        parent_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        started_at TIMESTAMP,
        completed_at TIMESTAMP,
        error_message TEXT
    )
    """,
)

# 老库里各插一行，用来验证迁移把默认值真的回填进了存量行
_LEGACY_ROWS = (
    ("INSERT INTO dem_terrain_jobs (task_id, status, output_dir, maxzoom) "
     "VALUES (1, 'completed', '/tmp/out', 12)"),
    ("INSERT INTO local_terrain_tasks "
     "(name, status, output_path, source_dir, output_dir, maxzoom) "
     "VALUES ('old', 'completed', '/tmp', '/tmp/src', '/tmp/out', 12)"),
)


def _init_db(monkeypatch, tmp_path):
    """指库到 tmp_path 后跑 init_database。禁止手写 sys.modules.pop
    （conftest.py:10-12 的规矩）。BASE_DIR / DOWNLOADS_DIR 一并锁进 tmp_path：
    init_database 会调 migrate_base_path_to_assets，那个函数会搬底图目录
    （见 tests/test_fix_base_path_migration.py 的模块 docstring）。"""
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    db = fresh_import(monkeypatch, "src.core.database")
    db.init_database()
    return db


def _legacy_db(monkeypatch, tmp_path):
    """先造一个没有新列的老库，再跑 init_database —— 只有迁移能补上这两列。"""
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    try:
        for stmt in _LEGACY_SCHEMA:
            conn.execute(stmt)
        for stmt in _LEGACY_ROWS:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()
    return _init_db(monkeypatch, tmp_path)


def _columns(db, table):
    """-> (列名顺序, {列名: PRAGMA 行})；行是 (cid,name,type,notnull,dflt,pk)。"""
    conn = db.get_connection()
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return [r[1] for r in rows], {r[1]: r for r in rows}


def test_config_defaults_are_shipped(monkeypatch, tmp_path):
    db = _init_db(monkeypatch, tmp_path)
    defaults = dict(db.DEFAULT_CONFIGS)

    assert defaults["terrain_quality_preset"] == "balanced"
    # 布尔配置在本仓一律存字符串 'true'/'false'（config_manager:294-295）
    assert defaults["terrain_vertex_normals"] == "false"


@pytest.mark.parametrize("table", ["dem_terrain_jobs", "local_terrain_tasks"])
def test_new_db_gets_preset_columns_from_the_create_statement(
        monkeypatch, tmp_path, table):
    """新库的两列必须由**建表语句**给出，不能只靠迁移补。

    新列必须带 DEFAULT —— tests/test_terrain_api.py:97-99 有不列新列的裸 INSERT。
    位置断言的理由见本节顶部注释。
    """
    names, cols = _columns(_init_db(monkeypatch, tmp_path), table)

    assert cols["quality"][4] == "'balanced'"
    assert cols["vertex_normals"][4] == "0"
    assert names.index("quality") < names.index("parent_url"), \
        f"{table} 的 quality 排在末尾，说明建表语句里没有它、是迁移补的"
    assert names.index("vertex_normals") < names.index("parent_url"), \
        f"{table} 的 vertex_normals 排在末尾，说明建表语句里没有它、是迁移补的"


@pytest.mark.parametrize("table", ["dem_terrain_jobs", "local_terrain_tasks"])
def test_new_db_gets_the_effective_level_column_from_the_create_statement(
        monkeypatch, tmp_path, table):
    """实际层级列同样要走建表语句，且默认值必须是 NULL 而不是 0。

    这一列的 DEFAULT 是有语义的：0 是合法层级（maxzoom<=1 配 speed 档真的只切
    到 z0），把「还没切完 / 存量行」也存成 0 就分不出「未知」与「切到了 z0」，
    详情面板会拿 0 冒充产物事实。
    """
    names, cols = _columns(_init_db(monkeypatch, tmp_path), table)

    assert "effective_maxzoom" in cols, f"{table} 没有 effective_maxzoom 列"
    assert cols["effective_maxzoom"][4] in (None, "NULL"), (
        f"{table}.effective_maxzoom 的默认值是 {cols['effective_maxzoom'][4]!r}，"
        "不是 NULL —— 0 是合法层级，不能当「未知」用")
    assert cols["effective_maxzoom"][3] == 0, "这一列必须允许 NULL"
    assert names.index("effective_maxzoom") < names.index("parent_url"), \
        f"{table} 的 effective_maxzoom 排在末尾，说明建表语句里没有它、是迁移补的"


@pytest.mark.parametrize("table,row_sql", [
    ("dem_terrain_jobs", "SELECT quality, vertex_normals FROM dem_terrain_jobs"),
    ("local_terrain_tasks",
     "SELECT quality, vertex_normals FROM local_terrain_tasks"),
])
def test_legacy_db_gets_preset_columns_from_the_migration(
        monkeypatch, tmp_path, table, row_sql):
    """存量库靠 ALTER 迁移补列，且默认值回填进已有行。

    没有这条，把四条迁移元组删光也全绿 —— 新库自带列会替它遮住。
    """
    db = _legacy_db(monkeypatch, tmp_path)
    names, cols = _columns(db, table)

    assert cols["quality"][4] == "'balanced'"
    assert cols["vertex_normals"][4] == "0"
    assert names.index("quality") > names.index("error_message"), \
        f"{table} 的 quality 没排在末尾，这张表被重建了，没走到迁移"

    conn = db.get_connection()
    try:
        row = conn.execute(row_sql).fetchone()
    finally:
        conn.close()
    assert (row[0], row[1]) == ("balanced", 0), f"{table} 存量行没拿到默认值"


@pytest.mark.parametrize("table", ["dem_terrain_jobs", "local_terrain_tasks"])
def test_legacy_db_gets_the_effective_level_column_from_the_migration(
        monkeypatch, tmp_path, table):
    """存量库靠 ALTER 补出实际层级列，存量行读出来是 NULL（不是 0）。

    删掉这条迁移元组时新库自带列会替它遮住 —— 存量用户的详情面板会
    OperationalError（no such column）。
    """
    db = _legacy_db(monkeypatch, tmp_path)
    names, cols = _columns(db, table)

    assert "effective_maxzoom" in cols, f"{table} 的迁移没补出 effective_maxzoom"
    assert names.index("effective_maxzoom") > names.index("error_message"), \
        f"{table} 的 effective_maxzoom 没排在末尾，这张表被重建了，没走到迁移"

    conn = db.get_connection()
    try:
        value = conn.execute(
            f"SELECT effective_maxzoom FROM {table}").fetchone()[0]
    finally:
        conn.close()
    assert value is None, (
        f"{table} 存量行的 effective_maxzoom 回填成了 {value!r} —— 那些作业当年"
        "切到哪一层没人知道，只能是 NULL")
