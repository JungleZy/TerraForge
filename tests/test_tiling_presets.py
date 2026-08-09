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
    （tests/conftest.py 模块 docstring 里 fresh_import 那条规矩）。
    BASE_DIR / DOWNLOADS_DIR 一并锁进 tmp_path：
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

    新列必须带 DEFAULT —— tests/test_terrain_api.py 的
    test_terrain_api_get_existing_job 有不列新列的裸 INSERT。
    位置断言的理由见本节顶部注释。
    """
    names, cols = _columns(_init_db(monkeypatch, tmp_path), table)

    assert cols["quality"][4] == "'balanced'"
    # vertex_normals 的默认值不在这里断言：它是三态列（NULL = 未知），由
    # test_vertex_normals_can_say_unknown_on_both_paths 专门钉。
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
    ("dem_terrain_jobs", "SELECT quality FROM dem_terrain_jobs"),
    ("local_terrain_tasks", "SELECT quality FROM local_terrain_tasks"),
])
def test_legacy_db_gets_preset_columns_from_the_migration(
        monkeypatch, tmp_path, table, row_sql):
    """存量库靠 ALTER 迁移补列，且默认值回填进已有行。

    没有这条，把四条迁移元组删光也全绿 —— 新库自带列会替它遮住。
    vertex_normals 的存量行取值另有专门用例：那一列**不该**被回填成 0。
    """
    db = _legacy_db(monkeypatch, tmp_path)
    names, cols = _columns(db, table)

    assert cols["quality"][4] == "'balanced'"
    assert names.index("quality") > names.index("error_message"), \
        f"{table} 的 quality 没排在末尾，这张表被重建了，没走到迁移"

    conn = db.get_connection()
    try:
        row = conn.execute(row_sql).fetchone()
    finally:
        conn.close()
    assert row[0] == "balanced", f"{table} 存量行没拿到默认档位"


# --------------------------------------------------------------------------
# vertex_normals 的三态：NULL = 未知，0 = 明确关，1 = 明确开
# --------------------------------------------------------------------------
#
# 与 effective_maxzoom 同一条理由，而且更硬：0 是这一列的**合法取值**（用户
# 真的把法线关了），所以它不能再兼职「未知」。本列出现之前切的作业，切片器的
# 默认恰恰是法线**开**（docs/reference/terrain/tiling-presets-measured.md
# 第三节的「现行默认 = auto + 法线开」），把存量行回填成 0 等于让详情面板对着
# 一批带光照的产物断言「未开启（无光照数据）」——正好说反，用户无从分辨。

_CREATE_PATH = ("建表语句", _init_db, "parent_url", False)
_MIGRATION_PATH = ("ALTER 迁移", _legacy_db, "error_message", True)


@pytest.mark.parametrize("table", ["dem_terrain_jobs", "local_terrain_tasks"])
@pytest.mark.parametrize("label,make_db,anchor,after_anchor",
                         [_CREATE_PATH, _MIGRATION_PATH])
def test_vertex_normals_can_say_unknown_on_both_paths(
        monkeypatch, tmp_path, table, label, make_db, anchor, after_anchor):
    """建表与迁移两条路径都要把 vertex_normals 声明成可空、默认 NULL。

    只改一条路径就发布过一次同类事故：新库一套语义、存量库另一套，而两边的
    详情面板读的是同一段代码。位置断言（本文件第 44 行起那段注释）在这里还多
    一层用处 —— 它保证这一条真的检查到了 `label` 说的那条路径产出的列，而不是
    被另一条路径的同名列糊弄过去。
    """
    names, cols = _columns(make_db(monkeypatch, tmp_path), table)

    assert (names.index("vertex_normals") > names.index(anchor)) is after_anchor, \
        f"{table} 的 vertex_normals 不是由{label}给出的，这一条没测到它该测的路径"
    assert cols["vertex_normals"][4] in (None, "NULL"), (
        f"{table}.vertex_normals（{label}）的默认值是 "
        f"{cols['vertex_normals'][4]!r} —— 0 是「明确关闭」这个合法取值，"
        "不能拿它当「未知」用")
    assert cols["vertex_normals"][3] == 0, \
        f"{table}.vertex_normals（{label}）不允许 NULL，「未知」就无处安放了"


@pytest.mark.parametrize("table", ["dem_terrain_jobs", "local_terrain_tasks"])
def test_legacy_rows_keep_an_unknown_normals_state(monkeypatch, tmp_path, table):
    """存量行的法线状态读出来必须是 None，不能被迁移回填成 0。

    回填 0 的后果不是「少一条信息」而是「多一条假信息」：那些作业当年是**开**
    着法线切的，面板却会写「未开启（无光照数据）」。宁可说未知。
    """
    db = _legacy_db(monkeypatch, tmp_path)

    conn = db.get_connection()
    try:
        value = conn.execute(f"SELECT vertex_normals FROM {table}").fetchone()[0]
    finally:
        conn.close()
    assert value is None, (
        f"{table} 存量行的 vertex_normals 回填成了 {value!r} —— 那批作业到底开没开"
        "法线没人记录过，回填任何一个值都是在替它们撒谎")


def _scalar(db, sql, params):
    conn = db.get_connection()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _new_dem_job_normals(monkeypatch, tmp_path, normals):
    """走真实的 DemTaskManager.start_tiling 建一条新作业，返回落库的法线值。"""
    db = _init_db(monkeypatch, tmp_path)
    dtm = fresh_import(monkeypatch, "src.services.dem_task_manager")
    # 切片线程本身与本用例无关：只看 start_tiling 的那条 upsert 写了什么。
    monkeypatch.setattr(dtm.DemTaskManager, "_run_tiling_job",
                        lambda self, *a, **k: None)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO dem_tasks (name, status, north, south, east, west, "
            "dataset, output_path, total_files) "
            "VALUES ('dem', 'completed', 1, 0, 1, 0, 'ASTGTM.003', ?, 1)",
            (str(tmp_path / "downloads"),),
        )
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    dtm.DemTaskManager(socketio=None).start_tiling(
        task_id, maxzoom=12, vertex_normals=normals)
    return _scalar(db, "SELECT vertex_normals FROM dem_terrain_jobs WHERE task_id=?",
                   (task_id,))


def _new_local_task_normals(monkeypatch, tmp_path, normals):
    """走真实的 LocalTerrainTaskManager.create_task_with_files，同上。"""
    db = _init_db(monkeypatch, tmp_path)
    mgr_mod = fresh_import(monkeypatch, "src.services.local_terrain_task_manager")
    # 「创建即切片」：建完直接起切。这里只关心建任务那条 INSERT。
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling",
                        lambda self, task_id: None)

    task_id = mgr_mod.LocalTerrainTaskManager(socketio=None).create_task_with_files(
        name="normals-probe", files=[("a.tif", b"fake-tif-bytes")],
        maxzoom=12, vertex_normals=normals)
    return _scalar(db, "SELECT vertex_normals FROM local_terrain_tasks WHERE id=?",
                   (task_id,))


@pytest.mark.parametrize("make_job", [_new_dem_job_normals, _new_local_task_normals],
                         ids=["dem_terrain_jobs", "local_terrain_tasks"])
@pytest.mark.parametrize("requested,expected", [
    (None, 0),   # 没传 → 走配置默认（出厂 'false'）
    (False, 0),  # 用户明确关
    (True, 1),   # 用户明确开
])
def test_a_new_job_always_records_an_explicit_normals_state(
        monkeypatch, tmp_path, make_job, requested, expected):
    """刚起的作业必须落一个明确的 0/1，永远不许是 NULL。

    这是「默认值改成 NULL」这笔改动的护栏：NULL 只允许表示「本列存在之前建的
    行」。写路径要是漏掉这一列（靠 DEFAULT 兜底），每一个新作业的详情面板都会
    显示「法线未知」—— 而这一刻我们明明知道，作业就是按这个值切的。
    """
    value = make_job(monkeypatch, tmp_path, requested)

    assert value is not None, (
        "新作业把法线状态落成了 NULL（未知）——写路径漏了 vertex_normals，"
        "退回了 DEFAULT NULL")
    assert value == expected, f"落库 {value!r}，期望 {expected!r}"


def test_retiling_an_unknown_row_resolves_normals_instead_of_assuming_off(
        monkeypatch, tmp_path):
    """本地地形的起切要从库里读回法线，读到 NULL 不许折成「关」。

    LocalTerrainTaskManager.start_tiling 不带参，法线只能从任务行读回。存量行
    是 NULL（未知），`bool(None)` 会把它静默解释成「关」—— 而这一列出现之前
    切片器的默认是**开**，等于按相反的设定重切一遍，全程零报错。未知就走配置
    默认，与建任务时「未传 → 配置默认」同一条规矩。

    切完之后行里必须留下明确的 0/1：本轮的产物事实是知道的，继续显示「未知」
    是同一个谎的反面。
    """
    db = _init_db(monkeypatch, tmp_path)
    mgr_mod = fresh_import(monkeypatch, "src.services.local_terrain_task_manager")

    seen = []
    monkeypatch.setattr(
        mgr_mod.LocalTerrainTaskManager, "_run_tiling_job",
        lambda self, task_id, source_dir, output_dir, maxzoom, parent_url,
        stop_flag, quality=None, vertex_normals=None: seen.append(vertex_normals))

    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    # 配置拨到「开」：实现要是 bool(None) 折成 False，下面两条都会红。
    mgr.config.set("terrain_vertex_normals", "true")

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        # 不列 vertex_normals —— 这正是存量行的形态：那一列没被记录过。
        cur.execute(
            "INSERT INTO local_terrain_tasks (name, status, output_path, "
            "source_dir, output_dir, maxzoom, quality) "
            "VALUES ('legacy', 'pending', '', '', '', 12, 'balanced')")
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    assert _scalar(db, "SELECT vertex_normals FROM local_terrain_tasks WHERE id=?",
                   (task_id,)) is None, "前提不成立：这一行本该是未知"

    mgr.start_tiling(task_id)
    thread = mgr.active_tasks.get(task_id)
    if thread is not None:
        thread.join(timeout=10)
        assert not thread.is_alive(), "切片线程没在 10s 内收工"

    assert seen == [True], (
        f"未知的法线状态被解释成了 {seen!r} —— 配置说开，就该按开切")
    assert _scalar(db, "SELECT vertex_normals FROM local_terrain_tasks WHERE id=?",
                   (task_id,)) == 1, (
        "切完之后行里还是未知 —— 本轮按什么设定烘的瓦片是知道的，必须落库")


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
