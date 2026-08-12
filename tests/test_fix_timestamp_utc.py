"""MEDIUM #4 修复回归 —— 任务时间戳全栈统一为 UTC ISO 8601。

修复前:started_at/completed_at 用 datetime.now() 经 sqlite3 默认适配器存成
空格分隔的本地时间(Python 3.12 起该适配器有 DeprecationWarning),前端
new Date() 一律按本地时区解析,Safari 对空格格式直接 Invalid Date。
修复后:所有手写时间戳统一走 src.core.database.utc_now_iso()(UTC、带 +00:00
时区标记、存 str);读取侧的时长计算走 parse_db_timestamp(),历史裸格式
(无时区)按 UTC 兜底,不会 aware/naive 相减 TypeError。
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# utc_now_iso() 用 timespec='seconds':无小数秒,带 +00:00 时区标记
ISO_TZ_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$')


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """把 Config 落盘路径 + 数据库全部指向 tmp_path 并建库(项目测试规约)。"""
    from src.core.config import Config
    from src.core import database
    from src.services.resource_scheduler import reset_scheduler

    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'config.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    database.init_database()
    # 调度器是进程单例:`start_task` 现在先要申请任务位 + 网络额度,上一条
    # 用例(哪怕在别的文件)留下的 owner 键会撞「already holds a reservation」,
    # 或把全局 max_concurrent_tasks(默认 2)吃满。清单例而不是抬高上限。
    reset_scheduler()
    yield tmp_path
    reset_scheduler()


def _seed_task_row(status='pending', total=0):
    from src.core.config import Config
    from src.core.database import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles,
               downloaded_tiles, failed_tiles)
            VALUES ('t', ?, 1, 0, 1, 0, 10, 10, 'satellite', 'tiles_only', ?, ?, 0, 0)
            """,
            (status, str(Config.DOWNLOADS_DIR), total),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _fetch_task_row(task_id):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        return conn.cursor().execute(
            'SELECT * FROM tasks WHERE id = ?', (task_id,)
        ).fetchone()
    finally:
        conn.close()


def _task_source(task_id):
    """任务行 → SourceSnapshot(= 这条任务的缓存命名空间)。与 _execute_task 同源。"""
    from src.services.source_registry import snapshot_for_task_row

    return snapshot_for_task_row(_fetch_task_row(task_id))


# ---------- utc_now_iso / parse_db_timestamp 单元行为 ----------

def test_utc_now_iso_returns_tz_aware_utc_string():
    from src.core.database import utc_now_iso

    value = utc_now_iso()
    assert isinstance(value, str)
    assert ISO_TZ_RE.match(value), f'应是带 +00:00 的 ISO 8601 秒级字符串: {value!r}'

    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    # 与真实 UTC 当前时间的偏差应在秒级以内
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 5


def test_parse_db_timestamp_handles_new_and_legacy_formats():
    from src.core.database import parse_db_timestamp

    # 新格式:带时区标记,原样解析
    aware = parse_db_timestamp('2026-07-31T09:00:00+00:00')
    assert aware.tzinfo is not None and aware.utcoffset() == timedelta(0)

    # 历史格式 1:datetime 默认适配器的空格分隔本地时间 —— 按 UTC 兜底,
    # 不抛 TypeError(旧本地时间行有一个时区的偏移,可接受)
    legacy = parse_db_timestamp('2026-07-31 09:00:00.123456')
    assert legacy.tzinfo is not None and legacy.utcoffset() == timedelta(0)
    assert (legacy.year, legacy.hour, legacy.microsecond) == (2026, 9, 123456)

    # 历史格式 2:CURRENT_TIMESTAMP 的 UTC 秒级格式
    ct = parse_db_timestamp('2026-07-31 09:00:00')
    assert ct.utcoffset() == timedelta(0)

    # 两种格式解析后可以安全相减(修复前 aware/naive 混合会 TypeError)
    assert (aware - legacy).total_seconds() == pytest.approx(-0.123456)


# ---------- TaskManager 写入点:started_at / completed_at / time_records ----------

def test_task_lifecycle_writes_utc_iso_timestamps(isolated_config):
    from src.services.download_engine import DownloadEngine
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=None)
    task_id = _seed_task_row(status='pending')

    # 全部瓦片预先命中 cache:任务不起网络直接跑完,走完 completed_at 写入路径。
    # cache 必须写进这条任务自己的命名空间(`<style_code>-<配置指纹8位>`,由任务
    # 行推出的 SourceSnapshot 决定)—— 按旧的裸 's' 写就全部落空,任务会真的去
    # mts0.googleapis.com 拉瓦片并带重试,用例从秒级变成分钟级挂起。
    engine = DownloadEngine()
    source = _task_source(task_id)
    for tile in engine.iter_tiles(1, 0, 1, 0, 10, 10):
        cache_path = tile.cache_path(source)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b'cached-tile')

    tm.start_task(task_id)
    thread = tm.active_tasks.get(task_id)
    assert thread is not None
    thread.join(timeout=30)
    assert not thread.is_alive()

    row = _fetch_task_row(task_id)
    assert row['status'] == 'completed'
    assert ISO_TZ_RE.match(row['started_at']), f"started_at: {row['started_at']!r}"
    assert ISO_TZ_RE.match(row['completed_at']), f"completed_at: {row['completed_at']!r}"
    # created_at 保持 SQLite CURRENT_TIMESTAMP(UTC 秒级,无时区标记)不动
    assert re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$', row['created_at'])

    from src.core.database import get_connection, parse_db_timestamp

    assert parse_db_timestamp(row['started_at']) <= parse_db_timestamp(row['completed_at'])

    conn = get_connection()
    try:
        records = conn.cursor().execute(
            "SELECT action, timestamp FROM task_time_records WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    assert [r['action'] for r in records] == ['start', 'complete']
    for r in records:
        assert ISO_TZ_RE.match(r['timestamp']), f"time record: {r['timestamp']!r}"


def test_running_time_with_legacy_naive_record_does_not_crash(isolated_config):
    """历史裸格式(无时区)的 start 记录与新代码的 aware UTC 当前时间混合,
    get_current_running_time / pause 时长累计不得 TypeError。"""
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=None)  # 先建 manager,再插 running 行,免得被 orphan 回收
    task_id = _seed_task_row(status='running')

    from src.core.database import get_connection

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO task_time_records (task_id, action, timestamp)"
            " VALUES (?, 'start', ?)",
            (task_id, (datetime.now() - timedelta(seconds=10)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    # 不 TypeError 即可;历史本地时间行被按 UTC 解析,elapsed 可能带一个
    # 时区的偏移(非 UTC 机器上甚至为负)—— 这是修复方案接受的存量数据偏差。
    running = tm.get_current_running_time(task_id)
    assert isinstance(running, int)

    tm.pause_task(task_id)
    row = _fetch_task_row(task_id)
    assert row['status'] == 'paused'
    assert row['total_running_seconds'] >= 0


# ---------- DEM / 本地地形 manager 的 orphan 恢复写入点 ----------

def test_orphan_recovery_writes_utc_iso_completed_at(isolated_config):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path)
            VALUES ('dem-orphan', 'running', 1, 0, 1, 0, 'ASTGTM.003', '/tmp')
            """
        )
        dem_id = cur.lastrowid
        cur.execute(
            "INSERT INTO dem_terrain_jobs (task_id, status, output_dir, maxzoom)"
            " VALUES (?, 'running', '/tmp', 12)",
            (dem_id,),
        )
        cur.execute(
            """
            INSERT INTO local_terrain_tasks
              (name, status, output_path, source_dir, output_dir, maxzoom)
            VALUES ('lt-orphan', 'running', '/tmp/x', '/tmp/x/source', '/tmp/x/terrain_tiles', 14)
            """
        )
        lt_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    from src.services.dem_task_manager import DemTaskManager
    from src.services.local_terrain_task_manager import LocalTerrainTaskManager

    DemTaskManager(socketio=None)
    LocalTerrainTaskManager(socketio=None)

    conn = get_connection()
    try:
        cur = conn.cursor()
        dem_job = cur.execute(
            "SELECT completed_at FROM dem_terrain_jobs WHERE task_id = ?", (dem_id,)
        ).fetchone()
        lt_task = cur.execute(
            "SELECT completed_at FROM local_terrain_tasks WHERE id = ?", (lt_id,)
        ).fetchone()
    finally:
        conn.close()

    assert ISO_TZ_RE.match(dem_job['completed_at']), f"dem job: {dem_job['completed_at']!r}"
    assert ISO_TZ_RE.match(lt_task['completed_at']), f"local terrain: {lt_task['completed_at']!r}"


def test_config_updated_at_is_utc_iso(isolated_config):
    """config.updated_at 同样走 utc_now_iso()(原是 datetime.now() 本地时间,
    也是 Python 3.12 sqlite3 默认适配器 DeprecationWarning 的最后来源)。"""
    from src.core.database import get_connection
    from src.services.config_manager import ConfigManager

    ConfigManager().set('request_timeout', '25')

    conn = get_connection()
    try:
        row = conn.cursor().execute(
            "SELECT value, updated_at FROM config WHERE key = 'request_timeout'"
        ).fetchone()
    finally:
        conn.close()

    assert row['value'] == '25'
    assert ISO_TZ_RE.match(row['updated_at']), f"updated_at 非 UTC ISO: {row['updated_at']!r}"


def test_task_from_row_returns_only_aware_datetimes(isolated_config):
    """`Task.from_row` 的三个时间戳必须全是 aware，不能 naive/aware 混着。

    created_at 来自表默认值 CURRENT_TIMESTAMP（朴素 'YYYY-MM-DD HH:MM:SS'），
    started_at/completed_at 由 utc_now_iso() 写入（带 +00:00）。修复前 from_row
    用裸 `datetime.fromisoformat` 逐个解析，于是同一个 Task 对象里两种形状并存，
    `to_dict()` 也吐出两种形状。今天前端的 parseTaskDate 兜住了，所以没有可见
    故障 —— 真正的代价是陷阱：Python 侧任何 `utc_now() - task.started_at` 碰上
    这种行就 TypeError。三个字段现在统一走 parse_db_timestamp。
    """
    from src.core.database import get_connection, utc_now, utc_now_iso
    from src.models.task import Task

    task_id = _seed_task_row(status='running')
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE tasks SET started_at = ?, completed_at = ? WHERE id = ?",
            (utc_now_iso(), utc_now_iso(), task_id))
        conn.commit()
        row = conn.cursor().execute(
            'SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    finally:
        conn.close()

    # created_at 就是表默认值写的朴素格式 —— 前提成立才有这条用例
    assert '+' not in str(row['created_at']), (
        'created_at 已经带时区了，本用例的前提需要重新评估')

    task = Task.from_row(row)
    for field in ('created_at', 'started_at', 'completed_at'):
        value = getattr(task, field)
        assert value is not None and value.tzinfo is not None, (
            f'{field} 是 naive datetime —— from_row 又在用裸 fromisoformat 了')

    # 真正会炸的那个操作:aware 相减。修复前 created_at 这一支必然 TypeError。
    assert (utc_now() - task.created_at).total_seconds() >= 0
