"""maxzoom 三态（int / 'auto' / 未传）的校验、落库表示与两个管理器的落地。"""

import logging
import re
import sqlite3
from pathlib import Path

import pytest

from conftest import fresh_import
from src.services.geo_validation import (AUTO_MAXZOOM, AUTO_MAXZOOM_SENTINEL,
                                         coerce_maxzoom, maxzoom_from_db,
                                         maxzoom_to_db)


def test_auto_literal_passes_through():
    assert coerce_maxzoom('auto') == AUTO_MAXZOOM


def test_missing_means_unset():
    assert coerce_maxzoom(None) is None
    assert coerce_maxzoom('') is None


def test_numbers_still_go_through_validate_zoom():
    assert coerce_maxzoom('12') == 12
    assert coerce_maxzoom(12) == 12
    assert coerce_maxzoom(0) == 0
    assert coerce_maxzoom(21) == 21


@pytest.mark.parametrize('bad', [
    'AUTO',        # 不做大小写归一
    ' auto ',      # 不裁前后空白
    'Auto',
    'automatic',
    '12.5',
    22,
    -1,            # 哨兵不许从外部传进来
    True,          # 布尔不是层级
    [],            # JSON 送得进不可哈希的值，必须是 400 不是 500
])
def test_rejects(bad):
    with pytest.raises(ValueError):
        coerce_maxzoom(bad)


@pytest.mark.parametrize('bad', ['AUTO', 22, '12.5'])
def test_rejecting_a_typo_still_shows_auto_is_legal(bad):
    # 数字分支委托给 validate_zoom，它只会说「不是数字」——拼错大小写的用户
    # 会以为 auto 根本不被支持。报错必须带上合法字面量。
    #
    # 三种拒绝形态各测一遍（非数字 / 越界 / 非整数）：validate_zoom 有三条 raise，
    # 而补句是在 coerce_maxzoom 的 except 里一次性包上去的。只测「非数字」那一条
    # 的话，把补句收进某一个分支（`if 'must be a number' in str(e): ...`）照样
    # 全绿 —— 而填了 22 或 12.5 的用户拿到的报错里，'auto' 这条出路又不见了，
    # 正是本用例声称要防的那件事。
    with pytest.raises(ValueError, match=re.escape("(or the literal 'auto')")):
        coerce_maxzoom(bad)


@pytest.mark.parametrize('bad', ['AUTO', 22, '12.5'])
def test_name_reaches_the_message(bad):
    # name 不透传给 validate_zoom 的话，报错会指错字段名而测试照样全绿。
    with pytest.raises(ValueError, match='terrain_local_maxzoom'):
        coerce_maxzoom(bad, name='terrain_local_maxzoom')


def test_db_roundtrip_for_auto():
    assert maxzoom_to_db(AUTO_MAXZOOM) == AUTO_MAXZOOM_SENTINEL
    assert maxzoom_from_db(AUTO_MAXZOOM_SENTINEL) is None


def test_db_roundtrip_for_a_number():
    assert maxzoom_to_db(12) == 12
    assert maxzoom_from_db(12) == 12
    # z0 是合法层级，不能被当成假值
    assert maxzoom_to_db(0) == 0
    assert maxzoom_from_db(0) == 0


# ---------------------------------------------------------------------------
# 两个管理器：落库存哨兵、起切读回还原
#
# 上面那批钉的是翻译函数自己，这批钉的是两个管理器真的用了它。
# 最容易犯的错是「落库存 -1、构造 TileParams 时也传 -1」：build_terrain 只在
# `max_level is None` 时才按源数据像素尺寸估算基准层级，紧接着那行
# `max(0, min(MAX_ZOOM, int(max_level) + level_offset))` 会把 -1 当成用户指定
# 的层级钳成 0 —— 切出一张 z0 瓦片，作业照报 completed、HTTP 200、前端零报错。
#
# 两个管理器各测一遍：local 侧的档位建任务时落库、起切时从库里读回，DEM 侧
# 没有任务行、起切时当场算，两条路径不对称，一边绿不代表另一边绿。
# ---------------------------------------------------------------------------


def _isolate(monkeypatch, tmp_path, *modules):
    """DB 与产物目录都指到 tmp_path，重新建库并新鲜导入被测模块。

    走 conftest.fresh_import 而不是裸 sys.modules.pop：裸 pop 不恢复，会给后跑
    的文件留下第二份模块（见 conftest 顶部与 test_conftest_isolation_contract）。
    """
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    # 全球底图可用性闸门会去看 BASE_DIR/assets/terrain/base_z8（同
    # test_local_terrain_api._reload 的注释）：不 patch 的话，本机有没有解压过
    # 底图就成了测试结果的一部分。
    monkeypatch.setattr(config.Config, "BASE_DIR", tmp_path)

    modules = fresh_import(monkeypatch, "src.core.database", *modules)
    db = modules[0]
    db.init_database()
    return modules


def _record_tile_params(monkeypatch, mod):
    """把管理器模块里的 tile_dem_task_dir 换成录参替身，返回收参 dict。"""
    seen = {}

    def fake_tile(task_dir, out_dir, params):
        seen["maxzoom"] = params.maxzoom
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mod, "tile_dem_task_dir", fake_tile)
    return seen


def _join(mgr, task_id):
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=10)


def _local_maxzoom(db, task_id):
    """直接读列值：落库形态本身就是被测对象，不经 get_task 的字典加工。"""
    conn = db.get_connection()
    try:
        return conn.execute(
            "SELECT maxzoom FROM local_terrain_tasks WHERE id=?",
            (task_id,)).fetchone()["maxzoom"]
    finally:
        conn.close()


def _job_maxzoom(db, task_id):
    conn = db.get_connection()
    try:
        return conn.execute(
            "SELECT maxzoom FROM dem_terrain_jobs WHERE task_id=?",
            (task_id,)).fetchone()["maxzoom"]
    finally:
        conn.close()


def _seed_dem_task(db, tmp_path, status="completed", with_tif=True):
    """造一个已完成的 DEM 下载任务与目录里的 tif，返回 (dem_task_id, tif_dir)。

    形状照抄 tests/test_local_terrain_from_dem_task.py 的同名 helper。
    """
    out_base = tmp_path / "downloads" / "dem"
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('dem-src', ?, 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 1, 1, 0)
            """,
            (status, str(out_base)),
        )
        dem_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    tif_dir = out_base / f"dem_task_{dem_id}"
    if with_tif:
        tif_dir.mkdir(parents=True, exist_ok=True)
        (tif_dir / "N47E006_dem.tif").write_bytes(b"fake-dem-bytes")
    return dem_id, tif_dir


# ---- local 侧 -------------------------------------------------------------


def test_local_manager_stores_the_sentinel_and_tiles_with_none(monkeypatch, tmp_path):
    """自动挡：库里存 -1，起切时 TileParams.maxzoom 必须是 None。"""
    db, mgr_mod = _isolate(monkeypatch, tmp_path,
                           "src.services.local_terrain_task_manager")
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    seen = _record_tile_params(monkeypatch, mgr_mod)

    # create_task_with_files 末尾直接 start_tiling，落库与起切一次走完。
    task_id = mgr.create_task_with_files(
        name="auto", files=[("a.tif", b"fake")], maxzoom=AUTO_MAXZOOM)
    _join(mgr, task_id)

    assert _local_maxzoom(db, task_id) == AUTO_MAXZOOM_SENTINEL
    assert seen["maxzoom"] is None, "哨兵没被还原成 None：build_terrain 会切出一张 z0"


@pytest.mark.parametrize("given", [12, 0])
def test_local_manager_keeps_explicit_levels(monkeypatch, tmp_path, given):
    """数字挡不受影响；z0 是合法层级，不能被当成假值退回自动。"""
    db, mgr_mod = _isolate(monkeypatch, tmp_path,
                           "src.services.local_terrain_task_manager")
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    seen = _record_tile_params(monkeypatch, mgr_mod)

    task_id = mgr.create_task_with_files(
        name="explicit", files=[("a.tif", b"fake")], maxzoom=given)
    _join(mgr, task_id)

    assert _local_maxzoom(db, task_id) == given
    assert seen["maxzoom"] == given
    assert seen["maxzoom"] is not None


def test_local_manager_from_dem_task_stores_the_sentinel(monkeypatch, tmp_path):
    """零拷贝入口走的是**另一条 INSERT**，绑定参数要各改各的。

    只改 create_task_with_files 那条的实现，本用例是唯一会红的。
    """
    db, mgr_mod = _isolate(monkeypatch, tmp_path,
                           "src.services.local_terrain_task_manager")
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    seen = _record_tile_params(monkeypatch, mgr_mod)
    dem_id, _tif_dir = _seed_dem_task(db, tmp_path)

    task_id = mgr.create_task_from_dem_task(
        name="auto-from-dem", dem_task_id=dem_id, maxzoom=AUTO_MAXZOOM)
    _join(mgr, task_id)

    assert _local_maxzoom(db, task_id) == AUTO_MAXZOOM_SENTINEL
    assert seen["maxzoom"] is None


def test_local_manager_validates_the_config_before_the_db_form(
        monkeypatch, tmp_path, caplog):
    """配置回落值必须先过 coerce_maxzoom，不能把原始字符串喂给 maxzoom_to_db。

    maxzoom_to_db 对越界值静默放行（`int('-1')` = -1），而 -1 正是自动挡的哨兵：
    直接喂原始配置值的话，库里那个 -1 与「用户真的选了自动」完全无从分辨，
    日志里也一个字都没有。软退回可以，静默退回不行。
    """
    from src.services.config_manager import ConfigManager

    db, mgr_mod = _isolate(monkeypatch, tmp_path,
                           "src.services.local_terrain_task_manager")
    ConfigManager().set("terrain_local_maxzoom", "-1")
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    _record_tile_params(monkeypatch, mgr_mod)

    logger_name = "src.services.local_terrain_task_manager"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        task_id = mgr.create_task_with_files(
            name="bad-cfg", files=[("a.tif", b"fake")])
    _join(mgr, task_id)

    assert _local_maxzoom(db, task_id) == AUTO_MAXZOOM_SENTINEL
    dropped = [r.getMessage() for r in caplog.records
               if "terrain_local_maxzoom" in r.getMessage()]
    assert dropped, "坏配置被丢弃却没留下任何日志"
    assert "-1" in dropped[0]


# ---- DEM 侧 ---------------------------------------------------------------


def test_dem_manager_stores_the_sentinel_and_tiles_with_none(monkeypatch, tmp_path):
    """DEM 侧没有任务行可读回，起切时当场算 —— 同样要落哨兵、传 None。"""
    db, dem_mod = _isolate(monkeypatch, tmp_path, "src.services.dem_task_manager")
    mgr = dem_mod.DemTaskManager(socketio=None)
    seen = _record_tile_params(monkeypatch, dem_mod)
    dem_id, _tif_dir = _seed_dem_task(db, tmp_path)

    mgr.start_tiling(dem_id, maxzoom=AUTO_MAXZOOM)
    _join(mgr, dem_id)

    assert _job_maxzoom(db, dem_id) == AUTO_MAXZOOM_SENTINEL
    assert seen["maxzoom"] is None, "哨兵没被还原成 None：build_terrain 会切出一张 z0"


@pytest.mark.parametrize("given", [12, 0])
def test_dem_manager_keeps_explicit_levels(monkeypatch, tmp_path, given):
    db, dem_mod = _isolate(monkeypatch, tmp_path, "src.services.dem_task_manager")
    mgr = dem_mod.DemTaskManager(socketio=None)
    seen = _record_tile_params(monkeypatch, dem_mod)
    dem_id, _tif_dir = _seed_dem_task(db, tmp_path)

    mgr.start_tiling(dem_id, maxzoom=given)
    _join(mgr, dem_id)

    assert _job_maxzoom(db, dem_id) == given
    assert seen["maxzoom"] == given
    assert seen["maxzoom"] is not None


def test_dem_manager_validates_the_config_before_the_db_form(
        monkeypatch, tmp_path, caplog):
    """理由同 local 侧那条：原始配置值不许直接进 maxzoom_to_db。"""
    db, dem_mod = _isolate(monkeypatch, tmp_path, "src.services.dem_task_manager")
    mgr = dem_mod.DemTaskManager(socketio=None)
    mgr.config.set("terrain_local_maxzoom", "-1")
    _record_tile_params(monkeypatch, dem_mod)
    dem_id, _tif_dir = _seed_dem_task(db, tmp_path)

    with caplog.at_level(logging.WARNING, logger="src.services.dem_task_manager"):
        mgr.start_tiling(dem_id)
    _join(mgr, dem_id)

    assert _job_maxzoom(db, dem_id) == AUTO_MAXZOOM_SENTINEL
    dropped = [r.getMessage() for r in caplog.records
               if "terrain_local_maxzoom" in r.getMessage()]
    assert dropped, "坏配置被丢弃却没留下任何日志"
    assert "-1" in dropped[0]


# ---- 两侧对称：空串也是「未表态」 ----------------------------------------
#
# coerce_maxzoom 把 None 与空串一并收成「未表态」，两个管理器就都得按这个
# 契约回落到配置默认。少收一态的后果不是 400 而是 500：空串绕过归一之后，
# maxzoom_to_db 拿到的是 coerce_maxzoom 返回的 None，`int(None)` 当场 TypeError。
# 两条路由眼下都已经先过一次 coerce_maxzoom（空串到不了管理器），所以这两条
# 钉的是管理器自己的契约 —— 下一个直调管理器的调用方不该踩这个坑。


def test_local_manager_treats_an_empty_string_as_unset(monkeypatch, tmp_path):
    from src.services.config_manager import ConfigManager

    db, mgr_mod = _isolate(monkeypatch, tmp_path,
                           "src.services.local_terrain_task_manager")
    ConfigManager().set("terrain_local_maxzoom", "9")
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    seen = _record_tile_params(monkeypatch, mgr_mod)

    task_id = mgr.create_task_with_files(
        name="empty", files=[("a.tif", b"fake")], maxzoom="")
    _join(mgr, task_id)

    assert _local_maxzoom(db, task_id) == 9
    assert seen["maxzoom"] == 9


def test_dem_manager_treats_an_empty_string_as_unset(monkeypatch, tmp_path):
    db, dem_mod = _isolate(monkeypatch, tmp_path, "src.services.dem_task_manager")
    mgr = dem_mod.DemTaskManager(socketio=None)
    mgr.config.set("terrain_local_maxzoom", "9")
    seen = _record_tile_params(monkeypatch, dem_mod)
    dem_id, _tif_dir = _seed_dem_task(db, tmp_path)

    mgr.start_tiling(dem_id, maxzoom="")
    _join(mgr, dem_id)

    assert _job_maxzoom(db, dem_id) == 9
    assert seen["maxzoom"] == 9


# ---- 出厂默认与存量库迁移 -------------------------------------------------
#
# 把出厂默认从 '14' 换成 'auto' 是**两件事**：DEFAULT_CONFIGS 走
# INSERT OR IGNORE，只对新建的库生效；存量库那行还是 '14'，不迁就永远停在
# 固定 14 —— 而那正是本轮要修的缺陷（非 30 m 源会超建 / 欠建）。
# 新库与存量库两条路径各钉一条用例，任一边被删都当场红。


def _fresh_db_module(monkeypatch, tmp_path):
    """库与产物目录都指到 tmp_path，新鲜导入 src.core.database，**不**建库。

    形态照 tests/test_tiling_presets.py 的 _init_db：BASE_DIR / DOWNLOADS_DIR
    一并锁进 tmp_path，因为 init_database 会调 migrate_base_path_to_assets，
    那个函数会搬底图目录。
    """
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    return fresh_import(monkeypatch, "src.core.database")


def _init_fresh_db(monkeypatch, tmp_path):
    db = _fresh_db_module(monkeypatch, tmp_path)
    db.init_database()
    return db


def _legacy_db_with(monkeypatch, tmp_path, values):
    """造一个 user_version 还没到 4 的存量库，config 表里先有这些行。

    必须裸 sqlite3 建，不能「先 init_database 再改值」：那样 user_version 已经
    被推到 4，迁移的闸门会提前返回，用例就永远绿 —— 测到的是闸门不是迁移。
    表结构照抄 database.py 的 CREATE TABLE config。
    """
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    try:
        conn.execute(
            "CREATE TABLE config ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.executemany("INSERT INTO config (key, value) VALUES (?, ?)",
                         sorted(values.items()))
        conn.commit()
    finally:
        conn.close()
    return _fresh_db_module(monkeypatch, tmp_path)


def _config_value(db, key):
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT value FROM config WHERE key=?",
                           (key,)).fetchone()
    finally:
        conn.close()
    return None if row is None else row["value"]


def _set_config(db, key, value):
    conn = db.get_connection()
    try:
        conn.execute("UPDATE config SET value=? WHERE key=?", (value, key))
        conn.commit()
    finally:
        conn.close()


def _user_version(db):
    conn = db.get_connection()
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_fresh_db_ships_auto_as_the_factory_default(monkeypatch, tmp_path):
    """新库的出厂默认必须是 'auto'：固定 14 只对 30 m 源正确。

    断言用 AUTO_MAXZOOM 而不是裸字面量 —— 出厂默认与 coerce_maxzoom 认的那个
    字面量必须是同一个，差一个字（'AUTO'）就是每次启动一条「改用出厂默认」。
    """
    db = _init_fresh_db(monkeypatch, tmp_path)

    assert dict(db.DEFAULT_CONFIGS)["terrain_local_maxzoom"] == AUTO_MAXZOOM
    assert _config_value(db, "terrain_local_maxzoom") == AUTO_MAXZOOM


def test_legacy_db_with_the_factory_14_is_migrated_to_auto(monkeypatch, tmp_path):
    """存量库那行还是 '14'：INSERT OR IGNORE 不覆盖它，只有迁移能改。

    库里同时放一行 contour_detail_zoom='14' —— 它在 database.DEFAULT_CONFIGS 里的
    出厂值恰好也是 '14'。这一行钉的是那句 UPDATE 的 `key = 'terrain_local_maxzoom'`
    半句：删掉它，迁移就按 value='14' 扫过整张 config 表，把等高线的细节层级一并
    写成 'auto'（那一项只认数字）。而且不可逆 —— user_version 一旦推到 4 就永不
    重试，新库同样中招（新库 user_version=0，迁移照跑）。库里只塞被测的那一个键，
    这半句谓词就没有任何用例钉着。
    """
    db = _legacy_db_with(monkeypatch, tmp_path,
                         {"terrain_local_maxzoom": "14",
                          "contour_detail_zoom": "14"})
    db.init_database()

    assert _config_value(db, "terrain_local_maxzoom") == AUTO_MAXZOOM
    assert _user_version(db) >= 4, \
        "迁移没推 user_version —— 下次启动会再改写用户一次"
    assert _config_value(db, "contour_detail_zoom") == "14", \
        "迁移改的不止 terrain_local_maxzoom —— 出厂值同样是 '14' 的 " \
        "contour_detail_zoom 被一并写成了 'auto'，而这条改写不可逆"


def test_a_deliberately_configured_level_is_left_alone(monkeypatch, tmp_path):
    """12 是用户自己设的，不许被改写 —— 只有恰好等于出厂 14 的才动。"""
    db = _legacy_db_with(monkeypatch, tmp_path, {"terrain_local_maxzoom": "12"})
    db.init_database()

    assert _config_value(db, "terrain_local_maxzoom") == "12"
    # 前提断言，与 test_legacy_db_with_the_factory_14_is_migrated_to_auto 那条同款：
    # 「12 没被改写」这半句在**迁移压根没跑**时同样成立（提前 return、整个函数
    # 炸掉被吞、调用点被删都长这样）。少了这一行，本用例声称守的那句 WHERE 谓词
    # 就没有被执行过的证据 —— 一条永远绿的用例。
    assert _user_version(db) >= 4, \
        "迁移没推 user_version —— 那句 UPDATE 根本没跑，「12 没被改写」是空转"


def test_migration_is_idempotent(monkeypatch, tmp_path):
    """迁完之后用户手动改回 14，第二次启动不许再动他。

    钉的是 user_version 闸门，不是那句 SQL：`WHERE value='14'` 自己并不幂等
    —— 「用户特意设成 14」与「出厂没动过的 14」在库里长得一模一样，只有
    「这条迁移已经跑过」这个事实能把两者分开。
    """
    db = _legacy_db_with(monkeypatch, tmp_path, {"terrain_local_maxzoom": "14"})
    db.init_database()
    assert _config_value(db, "terrain_local_maxzoom") == AUTO_MAXZOOM, \
        "前提不成立：第一次启动就没迁"

    _set_config(db, "terrain_local_maxzoom", "14")
    db.init_database()

    assert _config_value(db, "terrain_local_maxzoom") == "14"


def test_both_gated_migrations_run_on_the_same_legacy_db(monkeypatch, tmp_path):
    """两条带闸门的迁移必须都生效 —— 钉的是 init_database 里那几行的**调用顺序**。

    每条闸门都是「user_version >= 自己那个目标版本就跳过」，而版本号无条件推进
    （migrate_base_path_to_assets 推 3，migrate_local_maxzoom_to_auto 推 4）。把
    靠后的那条排到前面，一个还没迁过的老库会被它一步推到 4，前面那条的闸门当场
    满足 —— 迁移被静默跳过，且因为版本已经到位而**永不重试**。跳过底图路径搬迁
    的后果是那条老链：底图判为不可用 → parentUrl 兜底 → 404 → Cesium 塞假
    heightmap 图层污染共享 builder → 任务自己的瓦片高程全错且零报错（见
    migrate_base_path_to_assets 的 docstring）。

    所以这条用例必须**一个库同时带上两条迁移的原料**：各测一条的用例在任何排列
    下都是绿的 —— 被跳过的那条自己还在、代码一个字没少，只是没被执行过。
    """
    db = _legacy_db_with(monkeypatch, tmp_path, {
        "terrain_global_base_path": "./downloads/terrain/base_z8",
        "terrain_local_maxzoom": "14",
    })
    db.init_database()

    assert _config_value(db, "terrain_global_base_path") == "./assets/terrain/base_z8", \
        "底图路径没迁 —— 多半是 migrate_base_path_to_assets 被排到了 " \
        "migrate_local_maxzoom_to_auto 之后：user_version 已被推到 4，它的闸门把" \
        "自己整条跳过了，而且永不重试"
    assert _config_value(db, "terrain_local_maxzoom") == AUTO_MAXZOOM, \
        "层级没迁 —— migrate_local_maxzoom_to_auto 被排到了闸门更高的迁移之后"
    assert _user_version(db) >= 4, \
        "两条迁移都真跑过的话 user_version 至少是 4"
