"""task_tiles 稀疏失败表重构的语义测试。

新设计(依据见 services/task_manager.py 与 services/download_engine.py 的注释):
- 瓦片集合是 bbox+zoom 的纯函数,由 DownloadEngine.iter_tiles 确定性枚举,
  create_task 只记 total_tiles,不再向 task_tiles 写「每块瓦片一行」;
- 完成态以磁盘 cache 文件为准(cache 存在且非空 = 已完成),恢复任务时
  枚举全量瓦片、跳过 cache 命中的,天然只补缺口;
- task_tiles 只存失败瓦片(失败 UPSERT、成功 DELETE 历史失败行);
- tasks 表的进度计数批量落库(每 PROGRESS_DB_FLUSH_INTERVAL 块 + 下载结束),
  socketio 的 task_progress 按 PROGRESS_EMIT_MIN_INTERVAL 时间节流
  (首块与末块必发,中间按间隔,计数始终取内存实时值);
- init_database 迁移清掉旧版本写入的 pending/completed 全量行(一次性,
  用 SQLite user_version pragma 做幂等标记,跑过即跳过)。
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
    assert 2 <= len(progress_events) <= total, (
        "socketio 进度事件按时间节流(PROGRESS_EMIT_MIN_INTERVAL):"
        "不再逐瓦片一发,但首块与末块必发"
    )
    assert progress_events[0]['downloaded_tiles'] == 1, "首块瓦片必发"
    assert progress_events[-1]['downloaded_tiles'] == total, (
        "末块瓦片必发,且计数取内存实时值(不等批量落库)"
    )


def test_progress_emit_throttled_but_first_and_last_always_sent(isolated_config, monkeypatch):
    """进度广播按时间节流:极端间隔下只剩首发与末发,两发都不能丢。"""
    import services.task_manager as tm_mod

    # 极端节流间隔:中间的逐瓦片广播全部被压掉
    monkeypatch.setattr(tm_mod, 'PROGRESS_EMIT_MIN_INTERVAL', 3600)

    socketio = FakeSocketIO()
    tm = tm_mod.TaskManager(socketio=socketio)
    task_id = tm.create_task(_params())  # zoom 10 → 12 块

    _mark_running(task_id)

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        for tile in tiles:
            await progress_callback(tile, 'completed', None)
        return [{'tile': t, 'status': 'completed'} for t in tiles]

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    progress_events = [p for name, p in socketio.events if name == 'task_progress']
    assert len(progress_events) == 2, (
        f"极端节流下应只剩首发与末发两发,实际 {len(progress_events)} 发"
    )
    assert progress_events[0]['downloaded_tiles'] == 1
    assert progress_events[-1]['downloaded_tiles'] == 12, "完成那一发必须带出最终计数"

    row = _task_row(task_id)
    assert row['downloaded_tiles'] == 12, "节流只影响广播,不影响 DB 计数"


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
        # 模拟旧库:fixture 建库时已置 user_version=1,清回 0 才会触发迁移
        cur.execute('PRAGMA user_version = 0')
        conn.commit()
    finally:
        conn.close()

    database.init_database()  # 再次初始化触发迁移

    rows = _tile_rows(task_id)
    assert [r['status'] for r in rows] == ['failed'], (
        "迁移后 task_tiles 只保留失败行,pending/completed 全量行必须清掉"
    )

    conn = get_connection()
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 1, (
            "迁移完成后必须写入 user_version 幂等标记"
        )
    finally:
        conn.close()


# ---------- init_database 迁移只执行一次(user_version 幂等标记) ----------

def test_init_database_migration_runs_only_once(isolated_config):
    """迁移跑过后不再执行:已迁移库里的非 failed 行(运行中正常写入的)
    不应在后续启动时被 DELETE。"""
    from core import database
    from core.database import get_connection

    # fixture 已完成首次 init_database,新库应直接带上版本标记
    conn = get_connection()
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 1, (
            "新建库初始化后应立即带上 user_version=1 标记"
        )
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles, downloaded_tiles,
               failed_tiles)
            VALUES ('t', 'running', 1, 0, 1, 0, 10, 10, 'roadmap',
                    'tiles_only', '/tmp', 1, 0, 0)
            """
        )
        task_id = cur.lastrowid
        # 已迁移库里的非 failed 行:再次启动必须原样保留
        cur.execute(
            "INSERT INTO task_tiles (task_id, zoom, x, y, status, retry_count)"
            " VALUES (?, 10, 0, 0, 'pending', 0)",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    database.init_database()  # 再次初始化,迁移已被标记执行过

    rows = _tile_rows(task_id)
    assert [r['status'] for r in rows] == ['pending'], (
        "迁移只应执行一次:已迁移库里的非 failed 行不应在后续启动时被清掉"
    )


# ---------- cache 命中瓦片的残留 failed 行必须在对账时清掉 ----------

def test_stale_failed_rows_cleared_when_all_tiles_cached(isolated_config):
    """暂停/崩溃瞬间「cache 已写、failed 行未清」的瓦片,恢复执行时必须自愈。

    场景还原:瓦片下载成功、cache 落盘,但进度回调因 stop 检查 return(或进程
    崩溃)没跑到 DELETE —— 稀疏表里留下 failed 行。旧代码:枚举遇 cache 命中
    直接 continue,全库没有任何路径再清这枚行,完成判定 failed_count>0 恒真,
    任务重试多少次都失败。
    """
    from services.task_manager import TaskManager

    socketio = FakeSocketIO()
    tm = TaskManager(socketio=socketio)
    task_id = tm.create_task(_params())  # zoom 10 → 12 块

    # 全部瓦片已在 cache,且每块都留一枚残留 failed 行
    all_tiles = list(tm.download_engine.iter_tiles(1.0, 0.0, 1.0, 0.0, 10, 10, task_id=task_id))
    from core.database import get_connection
    conn = get_connection()
    try:
        for tile in all_tiles:
            cache_path = tile.cache_path('m')  # roadmap → style code 'm'
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b'cached-tile')
            conn.execute(
                "INSERT INTO task_tiles (task_id, zoom, x, y, status, retry_count, error_message)"
                " VALUES (?, ?, ?, ?, 'failed', 2, 'boom')",
                (task_id, tile.zoom, tile.x, tile.y),
            )
        conn.commit()
    finally:
        conn.close()

    _mark_running(task_id)

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        raise AssertionError("全部瓦片命中 cache,不应触发任何下载")

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    assert _tile_rows(task_id) == [], "cache 命中的残留 failed 行必须被对账清掉"
    row = _task_row(task_id)
    assert row['status'] == 'completed', "残留失败行不应再把任务拖成 failed"
    assert row['failed_tiles'] == 0
    assert row['downloaded_tiles'] == len(all_tiles)
    assert any(name == 'task_completed' for name, _ in socketio.events)


def test_stale_failed_rows_cleared_mixed_with_real_download(isolated_config):
    """混合场景:cache 命中的残留失败行清掉,未命中的失败行保留并重下。"""
    from services.task_manager import TaskManager

    socketio = FakeSocketIO()
    tm = TaskManager(socketio=socketio)
    task_id = tm.create_task(_params())  # zoom 10 → 12 块

    all_tiles = list(tm.download_engine.iter_tiles(1.0, 0.0, 1.0, 0.0, 10, 10, task_id=task_id))
    cached_with_stale_row = all_tiles[:3]   # cache 已写 + 残留 failed 行
    missing_with_row = all_tiles[3]         # 无 cache + failed 行(本次应重下成功)
    from core.database import get_connection
    conn = get_connection()
    try:
        for tile in cached_with_stale_row:
            cache_path = tile.cache_path('m')
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b'cached-tile')
            conn.execute(
                "INSERT INTO task_tiles (task_id, zoom, x, y, status, retry_count, error_message)"
                " VALUES (?, ?, ?, ?, 'failed', 2, 'boom')",
                (task_id, tile.zoom, tile.x, tile.y),
            )
        conn.execute(
            "INSERT INTO task_tiles (task_id, zoom, x, y, status, retry_count, error_message)"
            " VALUES (?, ?, ?, ?, 'failed', 1, 'boom')",
            (task_id, missing_with_row.zoom, missing_with_row.x, missing_with_row.y),
        )
        conn.commit()
    finally:
        conn.close()

    _mark_running(task_id)

    downloaded = []

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        downloaded.extend(tiles)
        for tile in tiles:
            await progress_callback(tile, 'completed', None)
        return [{'tile': t, 'status': 'completed'} for t in tiles]

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    assert len(downloaded) == len(all_tiles) - 3, "cache 命中的瓦片不应重下"
    assert _tile_rows(task_id) == [], (
        "cache 命中的残留行与重下成功的失败行都应被清掉"
    )
    row = _task_row(task_id)
    assert row['status'] == 'completed'
    assert row['failed_tiles'] == 0
    assert row['downloaded_tiles'] == len(all_tiles)
