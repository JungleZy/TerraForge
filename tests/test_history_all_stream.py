"""/api/history_all 的单一时间流语义（API 级，2026-08 定稿）。

两件事：
  1. 排序严格按创建时间倒序（created_at DESC，并列 id DESC）——
     旧排序 COALESCE(completed_at, created_at) 会让「刚完成的老任务」
     插队到「刚创建的新任务」前面，与「时间流按创建排列」的行上时间
     展示（终态显示 created_at 短日期）不自洽；
  2. ?status=active 是特殊值，展开成 status IN ('pending','running','paused')
     ——活动任务进了时间流之后，「进行中」chip 靠它把活动任务单独滤出来；
     其它单值（如 failed）维持等值过滤语义不变。

风格照 tests/test_history_stats.py（Config 副作用重定向 + 新鲜 import app）。
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database", "services.dem_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _insert_map(cur, name, status, created_at, completed_at=None):
    cur.execute(
        "INSERT INTO tasks (name, status, north, south, east, west, "
        "zoom_min, zoom_max, style, output_format, total_tiles, downloaded_tiles, "
        "output_path, created_at, completed_at) "
        "VALUES (?,?,1,0,1,0,1,2,'m','png',10,10,'/x',?,?)",
        (name, status, created_at, completed_at))


def _insert_dem(cur, name, status, created_at):
    cur.execute(
        "INSERT INTO dem_tasks (name, status, north, south, east, west, "
        "dataset, total_files, downloaded_files, output_path, created_at) "
        "VALUES (?,?,1,0,1,0,'ASTGTM.003',2,2,'/x',?)",
        (name, status, created_at))


def _insert_contour(cur, name, status, created_at):
    cur.execute(
        "INSERT INTO contour_tasks (name, status, north, south, east, west, "
        "contour_interval, zoom_min, zoom_max, total_tiles, rendered_tiles, "
        "output_path, created_at) "
        "VALUES (?,?,1,0,1,0,50,12,14,8,8,'/x',?)",
        (name, status, created_at))


def test_history_all_orders_by_created_at_desc_not_completed_at(monkeypatch, tmp_path):
    """老任务刚完成也不许插队到新建任务前面。

    old_completed：created 很早、completed 刚刚——旧排序（COALESCE 取
    completed_at）会把它放第一；新排序必须让 created 最新的 new_running
    排第一，哪怕它还没有 completed_at。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("core.database")
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        _insert_map(cur, 'old_completed', 'completed',
                    '2026-07-01 08:00:00', completed_at='2026-08-01 09:00:00')
        _insert_map(cur, 'new_running', 'running', '2026-08-01 08:00:00')
        _insert_dem(cur, 'mid_failed', 'failed', '2026-07-15 08:00:00')
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/api/history_all?page=1&per_page=50")
    assert resp.status_code == 200
    tasks = resp.get_json()["tasks"]
    assert [t["name"] for t in tasks] == ['new_running', 'mid_failed', 'old_completed'], (
        '排序不是严格 created_at DESC——刚完成的老任务插队到了新建任务前面'
    )


def test_history_all_tie_breaks_by_id_desc(monkeypatch, tmp_path):
    """created_at 并列时按 id DESC（id 近似时序，保证同秒创建顺序稳定）。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("core.database")
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        _insert_map(cur, 'tie_first', 'completed', '2026-08-01 08:00:00')
        _insert_map(cur, 'tie_second', 'completed', '2026-08-01 08:00:00')
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/api/history_all?page=1&per_page=50")
    assert resp.status_code == 200
    tasks = resp.get_json()["tasks"]
    assert [t["name"] for t in tasks] == ['tie_second', 'tie_first'], (
        'created_at 并列时没有按 id DESC 破并列'
    )


def test_history_all_status_active_covers_three_live_states(monkeypatch, tmp_path):
    """?status=active → 四表合一只含 pending/running/paused，total_count 同步。

    「进行中」chip 的后端语义。活动任务在时间流里（默认不带 status 时
    也会返回），active 只是把它们单独滤出来。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("core.database")
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        _insert_map(cur, 'm_pending', 'pending', '2026-08-01 01:00:00')
        _insert_dem(cur, 'd_running', 'running', '2026-08-01 02:00:00')
        _insert_contour(cur, 'c_paused', 'paused', '2026-08-01 03:00:00')
        _insert_map(cur, 'm_completed', 'completed', '2026-08-01 04:00:00')
        _insert_map(cur, 'm_failed', 'failed', '2026-08-01 05:00:00')
        _insert_map(cur, 'm_cancelled', 'cancelled', '2026-08-01 06:00:00')
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/api/history_all?status=active&page=1&per_page=50")
    assert resp.status_code == 200
    body = resp.get_json()
    names = sorted(t["name"] for t in body["tasks"])
    assert names == ['c_paused', 'd_running', 'm_pending'], (
        f'status=active 返回了 {names}——应只含 pending/running/paused 三态'
    )
    assert body["pagination"]["total_count"] == 3, (
        'active 过滤下 total_count 不是 3——翻页器会多出空白页'
    )
    # 顺序仍是 created_at DESC
    assert [t["name"] for t in body["tasks"]] == ['c_paused', 'd_running', 'm_pending']


def test_history_all_single_status_filter_unchanged(monkeypatch, tmp_path):
    """其它单值过滤语义不变：?status=failed 只回 failed（跨四表等值过滤）。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("core.database")
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        _insert_map(cur, 'm_failed', 'failed', '2026-08-01 01:00:00')
        _insert_dem(cur, 'd_failed', 'failed', '2026-08-01 02:00:00')
        _insert_map(cur, 'm_running', 'running', '2026-08-01 03:00:00')
        _insert_contour(cur, 'c_completed', 'completed', '2026-08-01 04:00:00')
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/api/history_all?status=failed&page=1&per_page=50")
    assert resp.status_code == 200
    body = resp.get_json()
    assert sorted(t["name"] for t in body["tasks"]) == ['d_failed', 'm_failed']
    assert all(t["status"] == 'failed' for t in body["tasks"])
    assert body["pagination"]["total_count"] == 2
