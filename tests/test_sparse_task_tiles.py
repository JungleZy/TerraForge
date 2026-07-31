"""task_tiles 稀疏失败表重构的语义测试。

新设计(依据见 services/task_manager.py 与 services/download_engine.py 的注释):
- 瓦片集合是 bbox+zoom 的纯函数,由 DownloadEngine.iter_tiles 确定性枚举,
  create_task 只记 total_tiles,不再向 task_tiles 写「每块瓦片一行」;
- 完成态以磁盘 cache 文件为准(cache 存在且非空 = 已完成),恢复任务时
  枚举全量瓦片、跳过 cache 命中的,天然只补缺口;
- task_tiles 只存失败瓦片(失败 UPSERT、成功 DELETE 历史失败行);
- tasks 表的进度计数批量落库(每 PROGRESS_DB_FLUSH_INTERVAL 块 + 下载结束),
  socketio 的 task_progress 保持每块瓦片都发;
- init_database 迁移清掉旧版本写入的 pending/completed 全量行。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload):
        self.events.append((event, payload))


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """把 Config 落盘路径 + 数据库全部指向 tmp_path 并建库(项目测试规约)。"""
    from core.config import Config
    from core import database

    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'config.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    database.init_database()
    return tmp_path


def _params(**overrides):
    p = dict(
        name='t', north=1.0, south=0.0, east=1.0, west=0.0,
        zoom_min=10, zoom_max=10, style='roadmap',
        output_format='tiles_only', output_path='downloads',
    )
    p.update(overrides)
    return p


def _task_row(task_id):
    from core.database import get_connection

    conn = get_connection()
    try:
        return conn.cursor().execute(
            'SELECT * FROM tasks WHERE id = ?', (task_id,)
        ).fetchone()
    finally:
        conn.close()


def _tile_rows(task_id):
    from core.database import get_connection

    conn = get_connection()
    try:
        return conn.cursor().execute(
            'SELECT * FROM task_tiles WHERE task_id = ? ORDER BY zoom, x, y', (task_id,)
        ).fetchall()
    finally:
        conn.close()


def _mark_running(task_id):
    """_execute_task 直接跑时需要任务处于 running(正常路径由 start_task 置位)。"""
    from core.database import get_connection

    conn = get_connection()
    try:
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()


# ---------- create_task 不再写全量行 ----------

def test_create_task_writes_no_task_tiles_rows(isolated_config):
    from services.download_engine import DownloadEngine
    from services.task_manager import TaskManager

    tm = TaskManager()
    task_id = tm.create_task(_params(zoom_min=10, zoom_max=11))

    engine = DownloadEngine()
    expected = len(engine.calculate_tiles(1.0, 0.0, 1.0, 0.0, 10, 11))
    assert engine.count_tiles(1.0, 0.0, 1.0, 0.0, 10, 11) == expected

    row = _task_row(task_id)
    assert row['total_tiles'] == expected > 0
    assert _tile_rows(task_id) == [], "create_task 不应再向 task_tiles 写全量行"


# ---------- iter_tiles 与 calculate_tiles 同序同内容 ----------

def test_iter_tiles_matches_calculate_tiles():
    from services.download_engine import DownloadEngine

    engine = DownloadEngine()
    cases = [
        (1.0, 0.0, 1.0, 0.0, 10, 11),      # 多 zoom
        (40.0, 39.0, 117.0, 116.0, 12, 12),
        (0.5, -0.5, 0.5, -0.5, 3, 5),      # 跨赤道/本初子午线
    ]
    for north, south, east, west, zoom_min, zoom_max in cases:
        materialised = [
            (t.zoom, t.x, t.y)
            for t in engine.calculate_tiles(north, south, east, west, zoom_min, zoom_max, task_id=7)
        ]
        iterated = [
            (t.zoom, t.x, t.y)
            for t in engine.iter_tiles(north, south, east, west, zoom_min, zoom_max, task_id=7)
        ]
        assert iterated == materialised, "iter_tiles 必须与 calculate_tiles 同序同内容"
        assert engine.count_tiles(north, south, east, west, zoom_min, zoom_max) == len(materialised)


# ---------- 恢复:只下载 cache 里缺的部分 ----------

def test_resume_downloads_only_missing_tiles(isolated_config):
    from services.download_engine import DownloadEngine
    from services.task_manager import TaskManager

    socketio = FakeSocketIO()
    tm = TaskManager(socketio=socketio)
    task_id = tm.create_task(_params())  # zoom 10,1°x1° → 12 块

    all_tiles = list(tm.download_engine.iter_tiles(1.0, 0.0, 1.0, 0.0, 10, 10, task_id=task_id))
    cached, missing = all_tiles[:5], all_tiles[5:]
    for tile in cached:
        cache_path = tile.cache_path('m')  # roadmap → style code 'm'
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b'cached-tile')

    _mark_running(task_id)

    downloaded = []

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        downloaded.extend(tiles)
        for tile in tiles:
            await progress_callback(tile, 'completed', None)
        return [{'tile': t, 'status': 'completed'} for t in tiles]

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    assert [(t.zoom, t.x, t.y) for t in downloaded] == [(t.zoom, t.x, t.y) for t in missing], (
        "恢复时只应下载 cache 里缺失的瓦片,已缓存的不应重复下载"
    )
    row = _task_row(task_id)
    assert row['status'] == 'completed'
    assert row['downloaded_tiles'] == len(all_tiles), (
        "cache 命中的瓦片(对账) + 本次下载的瓦片 = 总数"
    )
    assert row['failed_tiles'] == 0


# ---------- 计数批量落库 + 成功清掉历史失败行 ----------

def test_progress_counts_flushed_in_batches(isolated_config, monkeypatch):
    import services.task_manager as tm_mod

    socketio = FakeSocketIO()
    tm = tm_mod.TaskManager(socketio=socketio)
    task_id = tm.create_task(_params(zoom_min=12, zoom_max=13))  # 708 块,跨过多个批次

    _mark_running(task_id)

    # 给第一块瓦片预置历史失败行 —— 本次成功后必须被 DELETE 掉
    all_tiles = list(tm.download_engine.iter_tiles(1.0, 0.0, 1.0, 0.0, 12, 13, task_id=task_id))
    victim = all_tiles[0]
    from core.database import get_connection
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO task_tiles (task_id, zoom, x, y, status, retry_count, error_message)"
            " VALUES (?, ?, ?, ?, 'failed', 3, 'boom')",
            (task_id, victim.zoom, victim.x, victim.y),
        )
        conn.commit()
    finally:
        conn.close()

    # 用连接代理统计 tasks 表的批量计数 UPDATE 次数
    batch_updates = []
    real_get_connection = tm_mod.get_connection

    class SpyingConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            if 'UPDATE tasks' in sql and 'downloaded_tiles = MAX' in sql:
                batch_updates.append(sql)
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(tm_mod, 'get_connection', lambda: SpyingConnection(real_get_connection()))

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        for tile in tiles:
            await progress_callback(tile, 'completed', None)
        return [{'tile': t, 'status': 'completed'} for t in tiles]

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    total = len(all_tiles)
    interval = tm_mod.PROGRESS_DB_FLUSH_INTERVAL
    expected_flushes = total // interval + (1 if total % interval else 0)
    assert len(batch_updates) == expected_flushes, (
        f"{total} 块瓦片只应批量落库 {expected_flushes} 次(旧实现是每块一次)"
    )

    row = _task_row(task_id)
    assert row['downloaded_tiles'] == total
    assert row['failed_tiles'] == 0
    assert _tile_rows(task_id) == [], "成功后历史失败行必须被清掉"

    progress_events = [p for name, p in socketio.events if name == 'task_progress']
    assert len(progress_events) == total, "socketio 进度事件必须保持每块瓦片一发"
    assert progress_events[-1]['downloaded_tiles'] == total


# ---------- 失败 UPSERT 稀疏行 + 终态判定 ----------

def test_failed_tile_upsert_marks_task_failed(isolated_config):
    from services.task_manager import TaskManager

    socketio = FakeSocketIO()
    tm = TaskManager(socketio=socketio)
    task_id = tm.create_task(_params())  # zoom 10 → 12 块

    _mark_running(task_id)

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        for idx, tile in enumerate(tiles):
            if idx == 0:
                await progress_callback(tile, 'failed', 'boom-1')
                # 同一块瓦片重复失败:retry_count 在旧行基础上累加,计数不重复
                await progress_callback(tile, 'failed', 'boom-2')
            else:
                await progress_callback(tile, 'completed', None)
        return [
            {'tile': t, 'status': 'failed' if i == 0 else 'completed'}
            for i, t in enumerate(tiles)
        ]

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    rows = _tile_rows(task_id)
    assert len(rows) == 1, "task_tiles 只存失败瓦片"
    assert rows[0]['status'] == 'failed'
    assert rows[0]['retry_count'] == 2, "同一块瓦片重复失败 retry_count 应累加"
    assert rows[0]['error_message'] == 'boom-2'

    row = _task_row(task_id)
    assert row['status'] == 'failed'
    assert row['failed_tiles'] == 1
    assert row['downloaded_tiles'] == row['total_tiles'] - 1
    assert '1 tile(s) failed' in row['error_message']
    assert not any(name == 'task_completed' for name, _ in socketio.events)


# ---------- init_database 迁移清理非 failed 行 ----------

def test_init_database_migration_keeps_only_failed_rows(isolated_config):
    from core import database
    from core.database import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles, downloaded_tiles,
               failed_tiles)
            VALUES ('t', 'completed', 1, 0, 1, 0, 10, 10, 'roadmap',
                    'tiles_only', '/tmp', 4, 3, 1)
            """
        )
        task_id = cur.lastrowid
        # 模拟旧版本写入的全量行(各种非 failed 状态)+ 一行失败行
        for x, status in enumerate(('pending', 'completed', 'downloading', 'failed')):
            cur.execute(
                "INSERT INTO task_tiles (task_id, zoom, x, y, status, retry_count)"
                " VALUES (?, 10, ?, 0, ?, 0)",
                (task_id, x, status),
            )
        conn.commit()
    finally:
        conn.close()

    database.init_database()  # 再次初始化触发迁移

    rows = _tile_rows(task_id)
    assert [r['status'] for r in rows] == ['failed'], (
        "迁移后 task_tiles 只保留失败行,pending/completed 全量行必须清掉"
    )
