"""LOW 级审查修复回归测试 —— 地图管线(task_manager / routes/api)。

覆盖:
- L2-6: resume/重试不再覆写 started_at(仅首次启动写入)
- L2-8: 下载循环收尾 flush 抛异常不掩盖原始异常,连接仍关闭;
        socketio 广播故障与 DB 进度落库分层,互不影响
- L2-9: cache_enabled=false 明确拒绝执行(任务 failed + 如实错误信息),
        不再零产出却标 completed
- L2-12: POST /api/tasks 的 name 为 list/dict → 400(不是 sqlite
        InterfaceError → 500)
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """把 Config 落盘路径 + 数据库全部指向 tmp_path 并建库(项目测试规约)。"""
    from src.core.config import Config
    from src.core import database

    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'config.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    database.init_database()
    return tmp_path


def _seed_task_row(status='pending', output_format='tiles_only',
                   started_at=None, zoom=10, total=0):
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
               downloaded_tiles, failed_tiles, started_at)
            VALUES ('t', ?, 1, 0, 1, 0, ?, ?, 'satellite', ?, ?, ?, 0, 0, ?)
            """,
            (status, zoom, zoom, output_format, str(Config.DOWNLOADS_DIR),
             total, started_at),
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


class _FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload):
        self.events.append((event, payload))


def _fake_batch_writing_cache(tm):
    """fake 下载:落 cache(完成判定/复制阶段的真实输入)再逐块报完成。"""

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        results = []
        for tile in tiles:
            cache_path = tile.cache_path(style)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b'fresh-tile')
            await progress_callback(tile, 'completed', None)
            results.append({'tile': tile, 'status': 'completed'})
        return results

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch


# ---------- L2-6: started_at 仅首次启动写入 ----------

def test_first_start_sets_started_at(isolated_config):
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=_FakeSocketIO())
    _fake_batch_writing_cache(tm)
    task_id = _seed_task_row(status='pending')

    tm.start_task(task_id)
    thread = tm.active_tasks.get(task_id)
    if thread:
        thread.join(timeout=30)

    assert _fetch_task_row(task_id)['started_at'] is not None


def test_resume_does_not_overwrite_started_at(isolated_config):
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=_FakeSocketIO())
    _fake_batch_writing_cache(tm)
    first_start = '2026-07-01T00:00:00+00:00'
    task_id = _seed_task_row(status='paused', started_at=first_start)

    tm.start_task(task_id)  # resume 走 start_task
    thread = tm.active_tasks.get(task_id)
    if thread:
        thread.join(timeout=30)

    row = _fetch_task_row(task_id)
    assert row['started_at'] == first_start, (
        f"resume 不得覆写首次启动时间,实际: {row['started_at']}"
    )
    assert row['status'] == 'completed'


# ---------- L2-9: cache_enabled=false 明确拒绝 ----------

def test_execute_task_rejects_cache_disabled(isolated_config):
    from src.services.config_manager import ConfigManager
    from src.services.task_manager import TaskManager

    ConfigManager().set('cache_enabled', 'false')

    tm = TaskManager(socketio=_FakeSocketIO())
    downloaded = []

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        downloaded.extend(tiles)
        return []

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch
    task_id = _seed_task_row(status='running', total=1)

    asyncio.run(tm._execute_task(task_id))

    row = _fetch_task_row(task_id)
    assert row['status'] == 'failed', (
        f"cache_enabled=false 零产出任务不得标 completed,实际: {row['status']}"
    )
    assert 'cache' in row['error_message']
    assert downloaded == [], "拒绝必须发生在下载之前"
    assert any(e == 'task_failed' for e, _ in tm.socketio.events)


# ---------- L2-8: 收尾 flush 异常不掩盖原始异常、连接必关闭 ----------

def test_flush_failure_does_not_mask_download_error(isolated_config, monkeypatch):
    """finally 里 flush_progress_counts 抛异常时:原始下载异常照常决定任务
    结局(error_message 是原始错误),progress_conn 仍被关闭。"""
    import sqlite3

    import src.services.task_manager as tm_mod

    tm = tm_mod.TaskManager(socketio=_FakeSocketIO())

    # 包一层 get_connection 记下所有连接:进入下载前最后建的那条就是
    # progress_conn(回调里的 get_current_running_time 会再开连接,所以
    # 必须在调回调之前取其引用)。
    opened = []
    real_get_connection = tm_mod.get_connection

    def tracking_get_connection(*args, **kwargs):
        # 必须透传参数:progress_conn 是用 check_same_thread=False 建的
        # (批次 flush 的写盘走 asyncio.to_thread,见 task_manager 的 M3 注释)。
        conn = real_get_connection(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(tm_mod, 'get_connection', tracking_get_connection)

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        progress_conn = opened[-1]
        # 先报一块完成,让 unflushed 非零(finally 的 flush 才会真的执行 SQL),
        # 再关掉 progress_conn 让 flush 必炸,最后抛原始下载错误。
        await progress_callback(next(iter(tiles)), 'completed', None)
        progress_conn.close()
        raise RuntimeError('download boom')

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch
    task_id = _seed_task_row(status='running', total=1)

    asyncio.run(tm._execute_task(task_id))

    row = _fetch_task_row(task_id)
    assert row['status'] == 'failed'
    assert 'download boom' in row['error_message'], (
        f"flush 异常掩盖了原始下载错误,error_message: {row['error_message']}"
    )

    # 所有开过的连接(含 progress_conn)最终都是关闭态
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')


def test_emit_failure_does_not_break_progress_recording(isolated_config):
    """广播层故障与 DB 层分层:socketio.emit 炸了,进度照常落库、任务照常完成。"""
    from src.services.task_manager import TaskManager

    class ExplodingSocketIO:
        def emit(self, event, payload):
            # 只炸进度回调那一发,聚焦广播层与 DB 层的分层语义;复制阶段的
            # emit 防护由 test_copy_progress_emit_failure_does_not_break_copy 覆盖。
            if event == 'task_progress':
                raise RuntimeError('socketio boom')

    tm = TaskManager(socketio=ExplodingSocketIO())
    _fake_batch_writing_cache(tm)
    # zoom 0 全球 1 块瓦片,计数断言最直白
    task_id = _seed_task_row(status='running', zoom=0, total=1)

    asyncio.run(tm._execute_task(task_id))

    row = _fetch_task_row(task_id)
    assert row['status'] == 'completed'
    assert row['downloaded_tiles'] == 1


def test_copy_progress_emit_failure_does_not_break_copy(isolated_config):
    """复制阶段 task_copy_progress emit 故障不得打断复制本身(遗留项①)。"""
    from src.core.config import Config
    from src.services.task_manager import TaskManager

    class ExplodingSocketIO:
        def emit(self, event, payload):
            if event == 'task_copy_progress':
                raise RuntimeError('socketio boom')

    tm = TaskManager(socketio=ExplodingSocketIO())
    _fake_batch_writing_cache(tm)
    task_id = _seed_task_row(status='running', zoom=0, total=1)

    asyncio.run(tm._execute_task(task_id))

    assert _fetch_task_row(task_id)['status'] == 'completed'
    copied = [p for p in Config.DOWNLOADS_DIR.rglob('*.png')]
    assert copied, '复制阶段应产出瓦片文件'


# ---------- L2-12: name 类型校验 → 400 ----------

def _map_payload(**overrides):
    from src.core.config import Config

    payload = {
        'name': 't', 'north': 40.0, 'south': 39.0, 'east': 117.0, 'west': 116.0,
        'zoom_min': 10, 'zoom_max': 11, 'style': 'roadmap',
        # 保存路径新口径:一律绝对路径(相对值 → 400),默认 DOWNLOADS_DIR 下子目录
        'output_format': 'tiles_only',
        'output_path': str(Config.DOWNLOADS_DIR / 'downloads'),
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize('bad_name', [['a', 'b'], {'x': 1}, 42])
def test_create_task_non_string_name_400(isolated_app, bad_name):
    client = isolated_app.app.test_client()
    resp = client.post('/api/tasks', json=_map_payload(name=bad_name))
    assert resp.status_code == 400, resp.get_json()
    assert 'name' in resp.get_json()['error']


def test_create_task_string_name_still_201(isolated_app):
    client = isolated_app.app.test_client()
    resp = client.post('/api/tasks', json=_map_payload())
    assert resp.status_code == 201, resp.get_json()
