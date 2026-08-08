"""存量 'cancelled' 行的一次性迁移（「取消任务」被移除之后的收尾）。

后端读这些老行本来就不会报错：`Task.from_row` 走 `cls.__new__` 不校验枚举，
所有状态判定都是 `IN ('pending','running','paused')` 的正列表语义，孤儿恢复只
认 'running'。坏的是**渲染层** —— 前端两张状态词表跟着 TaskStatus 收敛到五态，
老行落到 `|| '未知'` 兜底，用户打开历史页看到一列「未知」。

迁移把它们改判 'failed'：终态、语义最接近「没跑完」，而且 start_task 收回
failed 白名单之后不会诈尸回活动列表。**不能迁成 paused** —— 那会被 start_task
认成「可恢复」，一批陈年任务集体复活。
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_TABLES = ("tasks", "dem_tasks", "contour_tasks", "local_terrain_tasks")

_SEEDS = {
    "tasks": (
        "INSERT INTO tasks (name, status, north, south, east, west, zoom_min,"
        " zoom_max, style, output_format, output_path)"
        " VALUES ('m', 'cancelled', 1, 0, 1, 0, 10, 12, 'satellite',"
        " 'tiles_only', '/tmp/x')"),
    "dem_tasks": (
        "INSERT INTO dem_tasks (name, status, north, south, east, west,"
        " dataset, output_path)"
        " VALUES ('d', 'cancelled', 1, 0, 1, 0, 'ASTGTM.003', '/tmp/x')"),
    "contour_tasks": (
        "INSERT INTO contour_tasks (name, status, north, south, east, west,"
        " contour_interval, zoom_min, zoom_max, output_path)"
        " VALUES ('c', 'cancelled', 1, 0, 1, 0, 10, 10, 12, '/tmp/x')"),
    "local_terrain_tasks": (
        "INSERT INTO local_terrain_tasks (name, status, output_path,"
        " source_dir, output_dir, maxzoom)"
        " VALUES ('l', 'cancelled', '/tmp/x', '/tmp/x/s', '/tmp/x/o', 12)"),
}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from src.core import config as config_mod

    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_mod.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config_mod.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config_mod.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config_mod.Config, "CACHE_DIR", tmp_path / "cache")

    sys.modules.pop("src.core.database", None)
    db_mod = importlib.import_module("src.core.database")
    db_mod.init_database()
    return db_mod


def _seed_cancelled(db_mod):
    conn = db_mod.get_connection()
    try:
        for sql in _SEEDS.values():
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _rows(db_mod):
    conn = db_mod.get_connection()
    try:
        return {
            table: conn.execute(
                f"SELECT status, error_message FROM {table}").fetchall()
            for table in _TABLES
        }
    finally:
        conn.close()


def test_startup_migrates_cancelled_rows_in_all_four_tables(db):
    _seed_cancelled(db)

    db.init_database()

    for table, rows in _rows(db).items():
        assert len(rows) == 1, table
        assert rows[0]["status"] == "failed", (
            f"{table} 里的 cancelled 行没被迁移 —— 前端词表已无该状态，"
            f"这行会渲染成「未知」")
        assert rows[0]["error_message"], (
            f"{table}: 迁移后的失败任务必须带来历说明，否则用户看到一条没有"
            f"原因的失败")


def test_migration_never_produces_a_restartable_status(db):
    """迁移落点必须是终态：迁成 pending/paused 会让陈年任务被 start_task 复活。"""
    _seed_cancelled(db)

    db.init_database()

    for table, rows in _rows(db).items():
        assert rows[0]["status"] not in ("pending", "paused", "running"), table


def test_migration_is_idempotent(db):
    """`WHERE status='cancelled'` 天然幂等：第二次启动一行都不该再碰。

    直接验「不再改写」而不是只看计数：把迁移写的 error_message 换掉再跑一次，
    它要是被重置回来，说明 WHERE 条件选错了对象。
    """
    _seed_cancelled(db)
    db.init_database()

    conn = db.get_connection()
    try:
        for table in _TABLES:
            conn.execute(f"UPDATE {table} SET error_message='用户后来自己写的'")
        conn.commit()
        assert db.migrate_cancelled_tasks_to_failed(conn.cursor()) == 0
        conn.commit()
    finally:
        conn.close()

    for table, rows in _rows(db).items():
        assert rows[0]["error_message"] == "用户后来自己写的", table


def test_migration_leaves_other_statuses_alone(db):
    """只动 cancelled：completed/paused 行原样保留。"""
    conn = db.get_connection()
    try:
        conn.execute(_SEEDS["tasks"].replace("'cancelled'", "'completed'"))
        conn.execute(_SEEDS["tasks"].replace("'cancelled'", "'paused'"))
        conn.commit()
    finally:
        conn.close()

    db.init_database()

    statuses = sorted(r["status"] for r in _rows(db)["tasks"])
    assert statuses == ["completed", "paused"]
